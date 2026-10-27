"""run_station — launch a worker (claude/codex/scripted) for one station,
wait for it, and record what actually happened.

Request payload contract: {"station": str, "role": str, "task": str}.

spawn_worker    build this role's WorkerSpec (copy_root = the role's own
                copy, from engine.application.steps.open_job.copy_root -- missing ->
                StepFailed("no_copy")), look up its WorkerConfig
                (config.workers[role] -- missing -> StepFailed
                ("no_worker_config")) and WorkerRunner (fakes.runners
                [worker.runner] -- missing -> StepFailed("no_runner")),
                build the full prompt (engine.infrastructure.workers.prompts.build_prompt,
                using config.station_owners/station_terminal_allow for
                this station+role), then procs.spawn() the runner's argv
                with its env (sandboxed by station_sandbox.sandboxed_argv),
                combined stdout+stderr redirected to
                home/jobs/{attempt}/stations/{request_id}.stdout, stdin
                from /dev/null. reconcile: a proc.started event for this
                request_id exists AND (the pid is still alive OR the
                stdout file exists) -- either means the spawn already
                happened, so a resume must not spawn a second worker.
await_worker    wait for the pid to exit for as long as it keeps making
                progress (engine.infrastructure.workers.base.wait_while_progressing: its
                stdout, gate log or result file changed within
                worker.timeout_sec). No total time limit. Stuck ->
                procs.close_station(...) (kills the whole process group,
                including a detached grandchild) then
                StepFailed("worker_stalled"); the orbit then continues the
                same work on the same copy. Non-zero exit ->
                StepFailed("worker_failed"). reconcile: the stdout file
                exists AND the station's own exit file (engine.infrastructure.procs.
                exit_file_for) positively confirms a code-0 exit --
                never just "no live process group left", which a
                worker_failed/worker_stalled close_station() also
                produces before its own StepFailed is raised.
record_station  runner.parse_result(stdout, spec) for the worker's own
                claim, but changed_paths is recomputed from the COPY's
                own `git status --porcelain -z` (engine.application.steps.open_job.
                dirty_paths), never from the worker's result JSON
                (Review Focus 3 -- a worker that lies about which files
                it touched, or claims a path outside its own copy,
                cannot make that lie land in the ledger). Appends
                station.finished {request_id, station, role, ok,
                summary, cost_usd, turns, tokens_out, changed_paths,
                requests} and
                writes the same facts to
                home/jobs/{attempt}/stations/{request_id}.json, THEN (if
                the worker reported failure) raises
                StepFailed("worker_reported_failure") -- the evidence is
                recorded before the request is failed, exactly like
                bench_run's record_bench. reconcile: a station.finished
                event for this request_id already exists AND reported
                ok=True -- a recorded ok=False re-raises the same
                StepFailed rather than reading its own existence as
                success.

The sandbox mount policy (which paths a spawned worker can see, write
to, or not see at all) lives in engine.application.steps.worker_sandbox. Writing
the gate_facts.json/engine_config.json snapshot a gated worker reads
before it runs, and importing its gate.log back into the ledger after,
live in engine.application.steps.worker_gate_log. Pure path builders shared by all
three live in engine.application.steps.worker_paths.
"""
from __future__ import annotations

import dataclasses
import json

from engine.infrastructure import procs
from engine.infrastructure import sandbox
from engine.domain.records import Event
from engine.domain.policy import owns_within_prefixes as _owns_within_prefixes
from engine.application.processor import StepContext, StepDef, StepFailed
from engine.application.steps.open_job import copy_git_config_matches_recorded
from engine.application.steps.open_job import copy_root as _role_copy_root
from engine.application.steps.open_job import dirty_paths
from engine.application.steps.worker_gate_log import (
    allowed_mcp_servers,
    gate_log_already_imported,
    import_gate_log,
    init_gate_log,
    task_owns,
    write_gate_facts,
)
from engine.application.steps.worker_paths import (
    gate_config_copy_path,
    gate_facts_path,
    gate_log_path,
    gate_ro_dir,
    record_path,
    result_path,
    stations_dir,
    stdout_path,
)
from engine.application.steps.worker_sandbox import ENGINE_ROOT, sandboxed_argv
from engine.infrastructure.workers.base import WorkerSpec, wait_for_exit, wait_while_progressing
from engine.infrastructure.workers.prompts import build_prompt


# Backward-compatible aliases: the original module kept every one of
# these as a private (leading-underscore) name on this module, and
# several tests still reach them that way (engine.application.steps.worker_step._xxx)
# to call or monkeypatch them directly rather than going through a
# full steps() run. They are the SAME function objects as the public
# names imported above, not copies -- moving the implementation never
# changes what a caller using either name gets.
_stations_dir = stations_dir
_stdout_path = stdout_path
_result_path = result_path
_record_path = record_path
_gate_log_path = gate_log_path
_gate_facts_path = gate_facts_path
_gate_ro_dir = gate_ro_dir
_gate_config_copy_path = gate_config_copy_path
_write_gate_facts = write_gate_facts
_init_gate_log = init_gate_log
_import_gate_log = import_gate_log
_gate_log_already_imported = gate_log_already_imported
_task_owns = task_owns
_allowed_mcp_servers = allowed_mcp_servers
_sandboxed_argv = sandboxed_argv


def runner_needs_gate(runner: object) -> bool:
    return bool(getattr(runner, "needs_gate", False))


def _full_spec(ctx: StepContext) -> tuple[WorkerSpec, object, object]:
    """Rebuild this request's WorkerSpec, WorkerConfig and WorkerRunner
    from the request payload + config + ledger -- deterministic, so
    every step recomputes it rather than trusting an earlier step's
    journaled payload (ENGINE.md: later steps don't trust self-reports)."""
    station = str(ctx.request.payload["station"])
    role = str(ctx.request.payload["role"])
    task = str(ctx.request.payload["task"])
    builder_test_hint = ctx.config.projects[ctx.project].builder_test_hint
    if builder_test_hint:
        # Plain, config-authored guidance appended verbatim to the task
        # text itself (never templated/parsed) -- e.g. telling the
        # builder to run related_cmd-style tests while editing and the
        # full test_cmd suite at most once at the end. build_prompt's
        # own test_cmd line (engine.infrastructure.workers.prompts, not touched here)
        # is unrelated to this -- this is a task-text addition, not a
        # change to that fixed "run exactly this command" line.
        task = f"{task}\n\n{builder_test_hint}"

    worker = ctx.config.workers.get(role)
    if worker is None:
        raise StepFailed("no_worker_config")
    runner = ctx.fakes.runners.get(worker.runner)
    if runner is None:
        raise StepFailed("no_runner")

    copy = _role_copy_root(ctx.config, ctx.attempt_id, role)
    if not copy.exists():
        raise StepFailed("no_copy")
    if runner.needs_gate and ctx.config.config_path is None:
        raise StepFailed("no_config_path")

    prefixes = ctx.config.station_owners.get(station, {}).get(role, ())
    owns = task_owns(ctx)
    if owns is not None:
        # Fail fast, before a worker is ever spawned: engine.domain.plan.
        # validate_plan already checked this at plan-approval time,
        # but a payload reaching run_station is never trusted a
        # second time without re-verifying (ENGINE.md
        # 「자기신고를 믿지 않는다」) -- the live gate (engine.domain.policy,
        # via gate_facts.json) re-checks the same thing again while
        # the worker is actually running.
        if not owns or not _owns_within_prefixes(owns, prefixes):
            raise StepFailed("owns_escapes_station_prefixes")
    patterns = ctx.config.station_terminal_allow.get(station, {}).get(role, ())
    generation = ctx.ledger.attempt(ctx.attempt_id).generation

    base_spec = WorkerSpec(
        project=ctx.project, attempt_id=ctx.attempt_id, generation=generation,
        station=station, role=role, copy_root=copy, workdir=copy, task=task,
        result_path=result_path(ctx), config_path=ctx.config.config_path,
        env_passthrough=ctx.config.worker_env_passthrough,
        gate_facts_path=(gate_facts_path(ctx) if runner.needs_gate else None),
        gate_log_path=(gate_log_path(ctx) if runner.needs_gate else None),
        gate_config_path=(gate_config_copy_path(ctx) if runner.needs_gate else None),
        mcp_servers=allowed_mcp_servers(ctx, station),
    )
    project_config = ctx.config.projects[ctx.project]
    test_cmd = project_config.test_cmd
    full_task = build_prompt(base_spec, prefixes, patterns, test_cmd,
                              manifest=project_config.manifest)
    spec = dataclasses.replace(base_spec, task=full_task)
    return spec, worker, runner


def _run_spawn_worker(ctx: StepContext) -> dict[str, object]:
    spec, worker, runner = _full_spec(ctx)
    if runner.needs_gate:
        # Written/created HERE, once, before the worker ever runs -- not
        # inside _full_spec (recomputed by every step, including
        # record_station, which must read the log rather than reset it).
        write_gate_facts(ctx)
        init_gate_log(ctx)
    argv = runner.argv(spec, worker)
    env = runner.env(spec, worker)
    argv = sandboxed_argv(ctx, spec, argv, env, runner)
    # Under mode="bwrap" the child's env reaches it via an inherited
    # pipe fd (engine.infrastructure.sandbox.wrap()'s `--args FD`, never a literal
    # `--setenv NAME VALUE` in argv -- see that module's own
    # pass_fds_for() docstring for why); the Popen env must then be
    # the sandbox launcher's own sanitized env (sandbox.exec_env),
    # never `env` itself -- `env` can carry worker-controlled values
    # that must never reach the process that execs bwrap.
    popen_env = sandbox.exec_env(ctx.config.sandbox, env)
    # A runner whose CLI reads its prompt from stdin (prompt_via_stdin --
    # ClaudeRunner/CodexRunner; see that attribute's own docstring for the
    # MAX_ARG_STRLEN/E2BIG incident this avoids) gets spec.task written to
    # a file instead of /dev/null on stdin; a runner that already embeds
    # the prompt in argv some other way (ScriptedRunner's own --inline)
    # keeps the original stdin_devnull=True behavior unchanged.
    stdin_path = None
    if getattr(runner, "prompt_via_stdin", False):
        stdin_path = procs.stdin_file_for(stdout_path(ctx))
        procs.write_stdin_file(stdin_path, spec.task)
    proc = procs.spawn(
        ctx.ledger, ctx.project, ctx.attempt_id, spec.station, argv,
        cwd=spec.workdir, env=popen_env, stdout_path=stdout_path(ctx),
        stdin_path=stdin_path, stdin_devnull=(stdin_path is None),
        request_id=ctx.request.request_id,
        pass_fds=sandbox.pass_fds_for(argv),
    )
    return {"pid": proc.pid, "station": spec.station}


def _reconcile_spawn_worker(ctx: StepContext) -> bool:
    for _seq, event in ctx.ledger.events(ctx.project, "proc.started"):
        if (event.attempt_id != ctx.attempt_id
                or event.payload.get("request_id") != ctx.request.request_id):
            continue
        pid = event.payload.get("pid")
        if pid is not None and procs.is_alive(int(pid)):
            return True
        if stdout_path(ctx).exists():
            return True
    return False


def _spawned_pid(ctx: StepContext) -> int:
    """pid from the ledger's proc.started event, not spawn_worker's own
    journaled payload -- when spawn_worker is resumed via reconcile
    (process already started before a crash) rather than re-run, its
    journaled 'done' payload is the empty 'started'-row payload (ENGINE.md:
    reconcile does not carry run()'s output), so the pid must come from
    the one place that is never empty: the proc.started event itself."""
    for _seq, event in ctx.ledger.events(ctx.project, "proc.started"):
        if (event.attempt_id == ctx.attempt_id
                and event.payload.get("request_id") == ctx.request.request_id):
            return int(event.payload["pid"])
    raise StepFailed("no_spawned_pid")


def _run_await_worker(ctx: StepContext) -> dict[str, object]:
    spec, worker, _runner = _full_spec(ctx)
    pid = _spawned_pid(ctx)
    code = wait_while_progressing(
        pid, worker.timeout_sec,
        (stdout_path(ctx), gate_log_path(ctx), result_path(ctx)))
    if code is None:
        # Stuck (no output, no tool call for timeout_sec): stop it so the
        # orbit can start a continuation run on the same copy.
        procs.close_station(ctx.ledger, ctx.project, ctx.attempt_id, spec.station)
        # record_station, where this import normally happens, is never
        # reached once this raises -- StepFailed ends the request right
        # here, which would otherwise lose a timed-out worker's whole
        # gate-log audit trail. close_station already killed the
        # process group above, so this always runs AFTER it is dead.
        if not gate_log_already_imported(ctx):
            import_gate_log(ctx, spec)
        raise StepFailed("worker_stalled")
    if code != 0:
        # wait_for_exit only reaps the LEADER pid -- a descendant the
        # leader spawned (e.g. a background tool call) can still be
        # running and still writing to gate.log even though the
        # leader itself already exited non-zero. close_station kills
        # and confirms the WHOLE process group gone (raises
        # RuntimeError, never imports, if a survivor cannot be
        # reaped), so the log is quiescent before it is read.
        procs.close_station(ctx.ledger, ctx.project, ctx.attempt_id, spec.station)
        if not gate_log_already_imported(ctx):
            import_gate_log(ctx, spec)
        raise StepFailed("worker_failed")
    return {"returncode": code}


def _reconcile_await_worker(ctx: StepContext) -> bool:
    """True only once a successful (code 0) exit is positively confirmed
    via the recorded exit file (engine.infrastructure.procs.exit_file_for). "no live
    process group + stdout exists" used to count a worker_failed/
    worker_stalled close as success too: close_station's proc.ended
    events land before run()'s own `raise StepFailed`, so a crash in
    that window made live_procs() empty while the real outcome was
    still a failure. False here is always safe: run() is idempotent on
    an already-finished process (wait_for_exit falls back to this same
    exit file) and on an already-closed station (close_station and
    import_gate_log are both no-ops the second time), so letting it
    run again reproduces the true outcome instead of guessing one from
    a ledger side effect that failure paths also produce."""
    if not stdout_path(ctx).exists():
        return False
    pid = _spawned_pid(ctx)
    exit_file = procs.exit_file_for(stdout_path(ctx))
    return wait_for_exit(pid, 0.0, exit_file) == 0


def _changed_paths(ctx: StepContext, spec: WorkerSpec) -> tuple[str, ...]:
    # Fail closed BEFORE the git call dirty_paths makes against the
    # copy: if the copy's own .git/config no longer matches what
    # _make_copy recorded at creation, something altered it after the
    # engine created it. dirty_paths' own git_argv() call already
    # neutralizes core.fsmonitor/core.hooksPath specifically via -c
    # overrides regardless of the config file -- this check is a
    # broader integrity guard against ANY other config tampering
    # (clean/smudge filters, core.worktree, remotes, ...) that a -c
    # override for two specific keys does not reach.
    if not copy_git_config_matches_recorded(
        ctx.ledger, ctx.project, ctx.attempt_id, spec.role, spec.copy_root,
    ):
        raise StepFailed("copy_git_config_changed")
    return dirty_paths(spec.copy_root, ())


def _latest_injected_skills(ctx: StepContext, station: str) -> tuple[str, ...]:
    """The "skills" list of the most recently recorded context.injected
    event for this project+station (engine.infrastructure.context.build's own
    ledger record, appended by engine.application.pipeline_stages.record_context) --
    the station skill names whose body actually reached this worker's
    own prompt. Scans in seq order and keeps the LAST match: a build/
    review task station's context is recorded with attempt_id=None
    (built before/alongside its own attempt opens -- see
    record_context's own docstring), so this can never filter by
    ctx.attempt_id; it relies instead on run_task_stages/task_stages'
    own guarantee that one task's whole stage pipeline runs to
    completion before the next task's begins (the same sequential
    assumption engine.application.handoff.reconcile_tools already makes about
    these same events), so the most recent one recorded for this
    station is always the one THIS call's own run just used."""
    skills: tuple[str, ...] = ()
    for _seq, event in ctx.ledger.events(ctx.project, "context.injected"):
        if event.payload.get("station") == station:
            names = event.payload.get("skills", [])
            if isinstance(names, list):
                skills = tuple(str(n) for n in names)
    return skills


def _skills_unapplied(ctx: StepContext, station: str,
                       skills_applied: tuple[dict[str, object], ...],
                       ) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(unapplied, required_unapplied) for this station's own run:
    `unapplied` is every injected skill name (_latest_injected_skills)
    this worker's own final JSON "skills_applied" never named;
    `required_unapplied` narrows that to the names
    ContextConfig.required_skills[station] actually marks mandatory --
    the ones a review of this change needs to be told about (see
    engine.application.steps.review._full_spec's own "skills.unapplied" lookup).
    A skill the worker reports applying is trusted here (same as
    engine.infrastructure.context itself, which never re-verifies a loaded
    skill was actually followed) -- this only catches one the worker's
    own report never mentioned at all."""
    injected = _latest_injected_skills(ctx, station)
    applied_names = {str(s.get("name", "")) for s in skills_applied}
    unapplied = tuple(n for n in injected if n not in applied_names)
    ctx_cfg = getattr(ctx.config, "context", None)
    required = set(ctx_cfg.required_skills.get(station, ())) if ctx_cfg is not None else set()
    required_unapplied = tuple(n for n in unapplied if n in required)
    return unapplied, required_unapplied


def _record_skills_unapplied(ctx: StepContext, station: str,
                              skills_applied: tuple[dict[str, object], ...]) -> None:
    """Appends a "skills.unapplied" event -- never raises, never gates
    anything itself: engine.application.steps.review._full_spec is what later reads
    it and, only for a name in "required_unapplied", folds a note into
    the reviewer's own input (see that module's own docstring on why
    this is the right place for a "반드시" skill gap to actually stop a
    job: the review failing it like any other finding, not a second,
    separate gate here)."""
    unapplied, required_unapplied = _skills_unapplied(ctx, station, skills_applied)
    if not unapplied:
        return
    ctx.ledger.append(Event(
        project=ctx.project, kind="skills.unapplied", actor_kind="processor",
        actor_id="processor", attempt_id=ctx.attempt_id,
        payload={
            "request_id": ctx.request.request_id, "station": station,
            "unapplied": list(unapplied), "required_unapplied": list(required_unapplied),
        },
    ))


def _run_record_station(ctx: StepContext) -> dict[str, object]:
    spec, _worker, runner = _full_spec(ctx)
    if not gate_log_already_imported(ctx):
        import_gate_log(ctx, spec)
    result = runner.parse_result(stdout_path(ctx), spec)
    changed = _changed_paths(ctx, spec)
    _record_skills_unapplied(ctx, spec.station, result.skills_applied)
    ctx.ledger.append(Event(
        project=ctx.project, kind="station.finished", actor_kind="processor",
        actor_id="processor", attempt_id=ctx.attempt_id,
        payload={
            "request_id": ctx.request.request_id, "station": spec.station,
            "role": spec.role, "ok": result.ok, "summary": result.summary,
            "cost_usd": result.cost_usd, "turns": result.turns,
            "tokens_out": result.tokens_out,
            "changed_paths": list(changed), "requests": list(result.requests),
            "skills_applied": list(result.skills_applied),
        },
    ))
    record = record_path(ctx)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({
        "request_id": ctx.request.request_id, "ok": result.ok,
        "summary": result.summary, "changed_paths": list(changed),
        "requests": list(result.requests), "skills_applied": list(result.skills_applied),
    }), encoding="utf-8")
    if not result.ok:
        raise StepFailed("worker_reported_failure")
    return {"ok": True, "changed_paths": list(changed), "requests": list(result.requests)}


def _reconcile_record_station(ctx: StepContext) -> bool:
    """True iff the already-recorded station.finished event for this
    request_id reported ok=True. A crash after _run_record_station's
    ledger.append but before its `raise StepFailed("worker_reported_
    failure")` must not let the recorded failure's own existence read
    back as success -- it re-raises the same failure the run would have
    raised, so the request still terminates 'failed' on resume."""
    for _seq, event in ctx.ledger.events(ctx.project, "station.finished"):
        if event.payload.get("request_id") == ctx.request.request_id:
            if not event.payload.get("ok"):
                raise StepFailed("worker_reported_failure")
            return True
    return False


def steps(ctx: StepContext) -> list[StepDef]:
    return [
        StepDef("spawn_worker", _run_spawn_worker, _reconcile_spawn_worker),
        StepDef("await_worker", _run_await_worker, _reconcile_await_worker),
        StepDef("record_station", _run_record_station, _reconcile_record_station),
    ]
