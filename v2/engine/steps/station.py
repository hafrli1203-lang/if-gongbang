"""run_station — launch a worker (claude/codex/scripted) for one station,
wait for it, and record what actually happened.

Request payload contract: {"station": str, "role": str, "task": str}.

spawn_worker    build this role's WorkerSpec (copy_root = the role's own
                copy, from engine.steps.open_job.copy_root -- missing ->
                StepFailed("no_copy")), look up its WorkerConfig
                (config.workers[role] -- missing -> StepFailed
                ("no_worker_config")) and WorkerRunner (fakes.runners
                [worker.runner] -- missing -> StepFailed("no_runner")),
                build the full prompt (engine.workers.prompts.build_prompt,
                using config.station_owners/station_terminal_allow for
                this station+role), then procs.spawn() the runner's argv
                with its env, combined stdout+stderr redirected to
                home/jobs/{attempt}/stations/{request_id}.stdout, stdin
                from /dev/null. reconcile: a proc.started event for this
                request_id exists AND (the pid is still alive OR the
                stdout file exists) -- either means the spawn already
                happened, so a resume must not spawn a second worker.
await_worker    wait for the pid to exit for as long as it keeps making
                progress (engine.workers.base.wait_while_progressing: its
                stdout, gate log or result file changed within
                worker.timeout_sec). No total time limit. Stuck ->
                procs.close_station(...) (kills the whole process group,
                including a detached grandchild) then
                StepFailed("worker_stalled"); the orbit then continues the
                same work on the same copy. Non-zero exit ->
                StepFailed("worker_failed"). reconcile: no live process
                group left for this station AND the stdout file exists.
record_station  runner.parse_result(stdout, spec) for the worker's own
                claim, but changed_paths is recomputed from the COPY's
                own `git status --porcelain -z` (engine.steps.open_job.
                dirty_paths), never from the worker's result JSON
                (Review Focus 3 -- a worker that lies about which files
                it touched, or claims a path outside its own copy,
                cannot make that lie land in the ledger). Appends
                station.finished {request_id, station, role, ok,
                summary, cost_usd, turns, changed_paths, requests} and
                writes the same facts to
                home/jobs/{attempt}/stations/{request_id}.json, THEN (if
                the worker reported failure) raises
                StepFailed("worker_reported_failure") -- the evidence is
                recorded before the request is failed, exactly like
                bench_run's record_bench. reconcile: a station.finished
                event for this request_id already exists.
"""
from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import stat
from pathlib import Path

from engine import ids, procs, sandbox
from engine.config import McpServerConfig, to_json_dict
from engine.ledger import Event, LedgerError
from engine.policy import ProjectFacts, _owns_within_prefixes
from engine.processor import StepContext, StepDef, StepFailed
from engine.steps.open_job import copy_git_config_matches_recorded
from engine.steps.open_job import copy_root as _role_copy_root
from engine.steps.open_job import dirty_paths
from engine.workers.base import WorkerSpec, wait_while_progressing
from engine.workers.prompts import build_prompt


ENGINE_ROOT = Path(__file__).resolve().parents[2]


def runner_needs_gate(runner: object) -> bool:
    return bool(getattr(runner, "needs_gate", False))


def _stations_dir(ctx: StepContext) -> Path:
    return ctx.config.home / "jobs" / ids.attempt_dirname(ctx.attempt_id) / "stations"


def _stdout_path(ctx: StepContext) -> Path:
    return _stations_dir(ctx) / f"{ctx.request.request_id}.stdout"


def _result_path(ctx: StepContext) -> Path:
    return _stations_dir(ctx) / f"{ctx.request.request_id}.result.json"


def _record_path(ctx: StepContext) -> Path:
    return _stations_dir(ctx) / f"{ctx.request.request_id}.json"


def _gate_log_dir(ctx: StepContext) -> Path:
    # Worker-writable (inside the stations dir): the log itself stays
    # audit-only by design (a worker could forge lines into it; see
    # _import_gate_log's docstring), so there is nothing to protect by
    # moving it -- unlike gate_facts.json/engine_config.json below.
    return _stations_dir(ctx) / ctx.request.request_id


def _gate_log_path(ctx: StepContext) -> Path:
    return _gate_log_dir(ctx) / "gate.log"


def _gate_ro_dir(ctx: StepContext) -> Path:
    """An engine-owned directory OUTSIDE every worker-writable tree --
    a sibling of the writable stations dir under the same hidden
    (tmpfs, then --remount-ro'd -- see engine.sandbox.wrap) $HOME, so
    once mounted read-only in the worker's sandbox, nothing the worker
    runs can rename, delete or replace it OR any ancestor directory
    entry up to $HOME itself (verified empirically: mkdir/mv both fail
    anywhere inside a remounted-ro tmpfs, even several plain-directory
    levels deep, while a SEPARATE deeper mount nested in the same tmpfs
    -- this one -- stays exactly as writable/readonly as it was bound).
    The worker CAN rename a plain writable directory it has access to
    (e.g. stations/<R> -> stations/<R>.old, then recreate an empty
    stations/<R> with a fake gate_facts.json at the same pathname the
    gate hook was told to read), so the facts file must never live
    inside such a directory -- its pathname is never inside any
    directory the worker has write access to, at any point in its
    lifetime."""
    return (ctx.config.home / "jobs" / ids.attempt_dirname(ctx.attempt_id)
            / "gate-ro" / ctx.request.request_id)


def _gate_facts_path(ctx: StepContext) -> Path:
    return _gate_ro_dir(ctx) / "gate_facts.json"


def _gate_config_copy_path(ctx: StepContext) -> Path:
    return _gate_ro_dir(ctx) / "engine_config.json"


def _mkdir_chain_no_symlink(base: Path, *parts: str) -> Path:
    """mkdir every missing component under `base` (assumed already a
    safe, pre-existing directory -- created once at config/job-open
    time, never per-request), refusing if any existing component along
    the way is a symlink or not a plain directory. `gate-ro`'s own leaf
    directories are created per request, and request ids are
    deterministic, so an EARLIER request's worker could in principle
    predict a FUTURE one's pathname -- this stays defensive about that
    even though, by construction, the worker never has write access to
    anything under `base` in the first place once the sandbox mounts
    are in place."""
    current = base
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise StepFailed("gate_ro_path_unsafe")
        try:
            current.mkdir()
        except FileExistsError:
            if current.is_symlink() or not current.is_dir():
                raise StepFailed("gate_ro_path_unsafe") from None
        except OSError as exc:
            raise StepFailed("gate_ro_path_unsafe") from exc
    return current


def _write_new_file_no_symlink(path: Path, data: bytes, *, allow_replace_plain: bool) -> None:
    """Write `path` without ever following a symlink at its final
    component (O_NOFOLLOW: a symlink planted there makes the open fail
    rather than writing through it). An existing PLAIN regular file may
    be replaced when `allow_replace_plain` -- a resumed spawn_worker
    re-writing its own facts/config before any worker has ever run for
    this request, the only case this function is called more than once
    for the same path -- but a symlink there is always refused outright
    (opened with O_CREAT|O_EXCL|O_NOFOLLOW)."""
    if path.is_symlink():
        raise StepFailed("gate_ro_path_unsafe")
    if path.exists():
        if not (allow_replace_plain and path.is_file()):
            raise StepFailed("gate_ro_path_unsafe")
        path.unlink()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o644)
    except OSError as exc:
        raise StepFailed("gate_ro_path_unsafe") from exc
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _touch_no_symlink(path: Path) -> None:
    """Create `path` if missing without ever following a symlink --
    same O_NOFOLLOW reasoning as _write_new_file_no_symlink, but for
    the log file, which stays worker-writable for its whole life (so
    idempotent re-touch, never O_EXCL, is correct here)."""
    if path.is_symlink():
        raise StepFailed("gate_ro_path_unsafe")
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o644)
    except OSError as exc:
        raise StepFailed("gate_ro_path_unsafe") from exc
    os.close(fd)


def _task_owns(ctx: StepContext) -> tuple[str, ...] | None:
    """The optional "owns" entry of this run_station request's payload
    (engine.full_orbit's build loop: a per-task narrowing of the
    build station's own configured write prefixes -- see
    engine.policy.ProjectFacts' own docstring). None if the payload
    carries no such key (every run_station caller that predates this
    field, and every non-build station) -- unchanged, unnarrowed
    behavior."""
    raw = ctx.request.payload.get("owns")
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
        raise StepFailed("invalid_owns")
    return tuple(raw)


def _payload_mcp_names(payload: dict[str, object]) -> tuple[str, ...]:
    """The "mcp" list of this request's optional "tools" payload entry
    (engine.plan.tools_for_station's shape: {"skill": [...], "mcp":
    [...]}) -- () for a payload that carries no "tools" key at all
    (every caller that predates this field)."""
    tools = payload.get("tools")
    if not isinstance(tools, dict):
        return ()
    raw = tools.get("mcp", [])
    if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
        raise StepFailed("invalid_tools_mcp")
    return tuple(raw)


def _allowed_mcp_servers(ctx: StepContext, station: str) -> dict[str, McpServerConfig]:
    """This request's payload "mcp" names, narrowed to BOTH
    config.station_mcp_allow[station] (the project-wide upper bound a
    plan's own tool table can never widen past) AND config.mcp_servers
    (a name the operator never configured launch info for is dropped,
    not trusted). The SAME narrowing engine.policy._decide_mcp repeats
    independently from gate_facts.json's "mcp_allow" -- this result
    only controls what the worker CLI is launched *able* to call; the
    gate is what actually blocks an unlisted one at tool-call time."""
    requested = set(_payload_mcp_names(ctx.request.payload))
    station_allow = set(ctx.config.station_mcp_allow.get(station, ()))
    names = sorted(requested & station_allow & set(ctx.config.mcp_servers))
    return {name: ctx.config.mcp_servers[name] for name in names}


def _build_project_facts(ctx: StepContext, owns: tuple[str, ...] | None = None,
                         mcp_allow: tuple[str, ...] = ()) -> ProjectFacts:
    """A ProjectFacts snapshot taken HERE (the engine process, real
    ledger access) for the gate hook (no ledger access at all, see
    engine.gate's module docstring) to decide with instead."""
    row = ctx.ledger.project(ctx.project)
    attempt_active = False
    if row.current_attempt_id is not None:
        try:
            attempt_active = ctx.ledger.attempt(row.current_attempt_id).status == "active"
        except LedgerError:
            attempt_active = False
    return ProjectFacts(
        project=ctx.project, registered=True, state=row.state,
        generation=row.generation, attempt_id=row.current_attempt_id,
        attempt_active=attempt_active, owns=owns, mcp_allow=mcp_allow,
    )


def _write_gate_facts(ctx: StepContext) -> None:
    """Writes gate_facts.json (and, if this project has a config file,
    a copy of it as engine_config.json) into the fresh, symlink-checked
    gate-ro dir -- see _gate_ro_dir's docstring for why this directory,
    not the stations dir, is where these live, and why the config is
    copied here too rather than mounted from its original location:
    GB_CONFIG then always points inside the one directory the sandbox
    mounts read-only and remounts immutable, regardless of where the
    original config file happens to sit on the host.

    The config copy is `to_json_dict(ctx.config)` -- the ALREADY
    in-memory-resolved config, every path absolute -- never a raw byte
    copy of the original file. The original file can itself contain
    relative paths (e.g. a `hooks` entry written as a path relative to
    the original config's own directory); copying its bytes verbatim
    into gate-ro and then loading it from THAT directory would resolve
    those same relative paths against the wrong base and silently
    relocate them (e.g. a hook script the gate is supposed to run).
    Serializing the resolved, absolute-path config instead means the
    copy means the same thing regardless of which directory it is
    loaded from."""
    station = str(ctx.request.payload["station"])
    mcp_allow = tuple(sorted(_allowed_mcp_servers(ctx, station)))
    facts = _build_project_facts(ctx, owns=_task_owns(ctx), mcp_allow=mcp_allow)
    gate_ro = _mkdir_chain_no_symlink(
        ctx.config.home, "jobs", ids.attempt_dirname(ctx.attempt_id),
        "gate-ro", ctx.request.request_id,
    )
    data = json.dumps({
        "project": facts.project, "registered": facts.registered,
        "state": facts.state, "generation": facts.generation,
        "attempt_id": facts.attempt_id, "attempt_active": facts.attempt_active,
        "owns": (list(facts.owns) if facts.owns is not None else None),
        "mcp_allow": list(facts.mcp_allow),
    }).encode("utf-8")
    _write_new_file_no_symlink(gate_ro / "gate_facts.json", data, allow_replace_plain=True)
    if ctx.config.config_path is not None:
        config_data = json.dumps(to_json_dict(ctx.config), ensure_ascii=False).encode("utf-8")
        _write_new_file_no_symlink(gate_ro / "engine_config.json", config_data,
                                    allow_replace_plain=True)


def _init_gate_log(ctx: StepContext) -> None:
    gate_log_dir = _mkdir_chain_no_symlink(
        ctx.config.home, "jobs", ids.attempt_dirname(ctx.attempt_id),
        "stations", ctx.request.request_id,
    )
    _touch_no_symlink(gate_log_dir / "gate.log")


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
        # own test_cmd line (engine.workers.prompts, not touched here)
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
    owns = _task_owns(ctx)
    if owns is not None:
        # Fail fast, before a worker is ever spawned: engine.plan.
        # validate_plan already checked this at plan-approval time,
        # but a payload reaching run_station is never trusted a
        # second time without re-verifying (ENGINE.md
        # 「자기신고를 믿지 않는다」) -- the live gate (engine.policy,
        # via gate_facts.json) re-checks the same thing again while
        # the worker is actually running.
        if not owns or not _owns_within_prefixes(owns, prefixes):
            raise StepFailed("owns_escapes_station_prefixes")
    patterns = ctx.config.station_terminal_allow.get(station, {}).get(role, ())
    generation = ctx.ledger.attempt(ctx.attempt_id).generation

    base_spec = WorkerSpec(
        project=ctx.project, attempt_id=ctx.attempt_id, generation=generation,
        station=station, role=role, copy_root=copy, workdir=copy, task=task,
        result_path=_result_path(ctx), config_path=ctx.config.config_path,
        env_passthrough=ctx.config.worker_env_passthrough,
        gate_facts_path=(_gate_facts_path(ctx) if runner.needs_gate else None),
        gate_log_path=(_gate_log_path(ctx) if runner.needs_gate else None),
        gate_config_path=(_gate_config_copy_path(ctx) if runner.needs_gate else None),
        mcp_servers=_allowed_mcp_servers(ctx, station),
    )
    test_cmd = ctx.config.projects[ctx.project].test_cmd
    full_task = build_prompt(base_spec, prefixes, patterns, test_cmd)
    spec = dataclasses.replace(base_spec, task=full_task)
    return spec, worker, runner


def _other_project_repos(ctx: StepContext) -> tuple[Path, ...]:
    return tuple(p.repo for name, p in ctx.config.projects.items()
                 if name != ctx.project)


def _repo(ctx: StepContext) -> Path:
    return ctx.config.projects[ctx.project].repo


def _is_within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _hidden_roots(ctx: StepContext, repo: Path) -> tuple[Path, ...]:
    # hidden_paths (ProjectConfig): operator-named paths outside `repo`
    # itself that must stay invisible too -- e.g. the true original
    # source checkout a generated `repo` worktree was cloned from,
    # which the sandbox's own whole-tree "--ro-bind / /" base would
    # otherwise still expose read-only by its real absolute path.
    return ((ctx.config.home, ctx.config.secrets_dir, repo)
            + _other_project_repos(ctx)
            + ctx.config.projects[ctx.project].hidden_paths)


def _linked_entries_for_role(ctx: StepContext, role: str) -> set[str]:
    """copy_links entries THIS role's copy actually got a symlink for,
    per job.opened.copy_links_skipped -- never the configured list
    itself, which can include an entry open_job._make_copy refused to
    link (missing source, or an unsafe shape: a source escaping the
    repo, or a destination parent already a symlink -- see
    open_job._link_copy_link_entry). A skipped entry is excluded here
    even though this function cannot see WHY it was skipped; the
    separate re-validation below (_safe_copy_link_mounts) is what
    actually checks the shape at mount time."""
    configured = set(ctx.config.projects[ctx.project].copy_links)
    if not configured:
        return set()
    skipped: set[str] = set()
    for _seq, event in ctx.ledger.events(ctx.project, "job.opened"):
        if event.payload.get("attempt_id") != ctx.attempt_id:
            continue
        for row in event.payload.get("copy_links_skipped", []):
            if isinstance(row, dict) and row.get("role") == role:
                skipped.add(str(row.get("entry")))
        break  # exactly one job.opened event per attempt_id
    return configured - skipped


def _journal_copy_link_mount_skipped(ctx: StepContext, entry: str, reason: str) -> None:
    ctx.ledger.append(Event(
        project=ctx.project, kind="copy_link.mount_skipped",
        actor_kind="processor", actor_id="processor", attempt_id=ctx.attempt_id,
        payload={"request_id": ctx.request.request_id, "entry": entry, "reason": reason},
    ))


def _safe_copy_link_mounts(ctx: StepContext, spec: WorkerSpec,
                           repo: Path) -> tuple[tuple[str, Path], ...]:
    """(entry, repo/<entry>) pairs safe to re-expose read-only in this
    role's sandbox -- only for entries _make_copy actually linked (see
    _linked_entries_for_role), AND only if repo/<entry>'s realpath,
    RE-CHECKED here (never trusted from config-load or copy-creation
    time alone), stays inside the repo and outside every hidden root
    (secrets_dir, config.home, the ledger directory, every other
    project's repo). This re-check matters on its own: with
    copy_links=["deps"] and repo/deps a symlink to secrets_dir,
    _make_copy correctly refuses to create that symlink in the copy,
    but mounting the configured `repo/deps` read-only regardless would
    still re-expose secrets_dir itself, entirely independent of
    whether the copy's own symlink existed. An entry failing either check is
    skipped (never mounted) and journaled as copy_link_mount_skipped,
    not silently trusted a second time."""
    linked = _linked_entries_for_role(ctx, spec.role)
    if not linked:
        return ()
    repo_real = Path(os.path.realpath(repo))
    # The repo itself is hidden in the worker sandbox, but repo/<entry> is
    # exactly what this function re-exposes read-only -- so the repo must
    # not be in the "forbidden" roots here (the within-repo check above
    # already requires the target to be inside it). The ledger directory
    # is a writable root, not a hidden one, and in ordinary layouts it is
    # an ancestor of the repo; it must not be in this list either, or a
    # legitimate copy_links entry (e.g. node_modules) would be skipped
    # for that reason instead.
    forbidden = (ctx.config.home, ctx.config.secrets_dir) + _other_project_repos(ctx)
    hidden_roots = tuple(Path(os.path.realpath(root)) for root in forbidden)
    mounts: list[tuple[str, Path]] = []
    for entry in sorted(linked):
        target = repo / entry
        if not target.exists():
            continue
        target_real = Path(os.path.realpath(target))
        if target_real != repo_real and not _is_within(target_real, repo_real):
            _journal_copy_link_mount_skipped(ctx, entry, "copy_link_mount_skipped")
            continue
        if any(target_real == root or _is_within(target_real, root)
               for root in hidden_roots):
            _journal_copy_link_mount_skipped(ctx, entry, "copy_link_mount_skipped")
            continue
        mounts.append((entry, target))
    return tuple(mounts)


def _ledger_hidden_root(ctx: StepContext) -> tuple[Path, ...]:
    """The WHOLE directory holding the ledger file -- not just the
    ledger file itself plus its known SQLite WAL/SHM/rollback-journal
    companions by name. Masking those four names individually leaves a
    gap for anything else that lives alongside them: a future SQLite
    companion file this list does not know about, a hard link or bind
    alias to the same inode under a different name, or simply another
    file in that directory a worker was never meant to see either.
    Hiding the directory itself (a tmpfs, then remounted read-only --
    see engine.sandbox.wrap) closes all of those at once, the same way
    engine.steps.bench_run already hides its own ledger directory
    wholesale. Any deeper path that still needs to be visible or
    writable under it (there is none today for the worker/reviewer
    sandboxes; `config.home`/`stations_dir`/`gate-ro` all live under a
    SEPARATE root) would simply be re-exposed by its own deeper mount,
    same as every other hidden root here -- never by excepting
    specific filenames out of this one."""
    return (ctx.config.ledger_path.parent,)


_RUNNER_LOGIN_FILES: dict[str, tuple[str, ...]] = {
    # The SMALLEST set of real-HOME-relative files each real CLI needs
    # copied into its own fresh, per-attempt worker HOME (see
    # _worker_home_dir/sandbox.prepare_worker_home below) to actually
    # authenticate -- named explicitly by the team lead's own
    # decision, not guessed: everything ELSE under the real HOME (other
    # apps, other CLIs' own credentials, ssh keys, ...) must stay
    # invisible to a worker and to any code it runs, which a wholesale
    # readonly mount of the real HOME (this module's previous approach)
    # did not achieve. A runner class absent from this table (e.g.
    # ScriptedRunner, a test double) needs none.
    "ClaudeRunner": (".claude/.credentials.json", ".claude.json"),
    "CodexRunner": (".codex/auth.json",),
}


def _login_files_for(runner: object) -> tuple[str, ...]:
    return _RUNNER_LOGIN_FILES.get(type(runner).__name__, ())


def _worker_home_dir(ctx: StepContext, spec: WorkerSpec) -> Path:
    """A fresh, per-attempt-and-station $HOME for this worker's real
    CLI process -- never the real operator HOME (see sandbox.
    prepare_worker_home's own docstring for why). Lives under the
    engine's own home, alongside spec.copy_root/_stations_dir, so it is
    a descendant of the `hidden` config.home entry and gets re-exposed
    the same way those already are (a deeper mount always wins -- see
    engine.sandbox.wrap's module docstring). Keyed by station AND role
    (not just attempt_id): engine.config.parallel_review_bench can run
    a builder and a reviewer for the SAME attempt_id concurrently, each
    needing its own isolated CLI identity directory."""
    return (ctx.config.home / "jobs" / ids.attempt_dirname(ctx.attempt_id)
            / "worker-home" / spec.station / spec.role)


def _sandboxed_argv(ctx: StepContext, spec: WorkerSpec, argv: list[str],
                     env: dict[str, str], runner: object) -> list[str]:
    """Builder sandbox policy: writable = the worker's own copy, the
    stations dir (where this request's stdout/result files land, and
    where the gate hook's own decision LOG lives -- worker-writable,
    hence worker-forgeable; see engine.gate/station.record_station),
    and -- only if the real HOME exists -- the
    runner's own `home_writable_subpaths` under THIS WORKER'S OWN
    fresh home directory (_worker_home_dir), never under the real HOME
    (e.g. ClaudeRunner's ".claude"/".claude.json", CodexRunner's
    ".codex": the only state a real CLI needs to write for its own
    credentials/session). readonly = _worker_home_dir(ctx, spec) itself
    (mounted as $HOME; sandbox.prepare_worker_home seeds it, before
    this mount, with ONLY the real CLI's own login files named in
    _RUNNER_LOGIN_FILES -- see that function's docstring for why this
    replaces the previous wholesale readonly mount of the real HOME);
    repo/<entry> for every copy_links
    entry _safe_copy_link_mounts validates as actually linked AND
    safe to re-expose (the symlink target a Bash-run test_cmd/
    install_cmd *inside the copy* needs to resolve -- see
    ProjectConfig.copy_links; a worker's own Read/Glob/Grep is still
    bounded to copy_root by engine.policy's realpath rule regardless of
    this read-only re-exposure); PLUS, for a copy_links entry with
    configured copy_link_writable_subpaths (e.g. node_modules needing
    .vite-temp/.vite writable despite being otherwise read-only),
    copy_root/<entry> itself -- open_job._link_copy_link_entry built
    THAT entry as a real directory inside the copy (per-child
    symlinks to the repo, plus a real subdirectory per configured
    writable subpath), which `spec.copy_root` being writable would
    otherwise leave fully writable (ordinary new files included, not
    just the configured subpaths); this deeper read-only mount locks
    that back down. writable ALSO then re-adds copy_root/<entry>/
    <subpath> for each configured subpath -- deeper still, so it wins
    back over that read-only lock; both subpaths already exist on
    disk (open_job created them), so bwrap never needs to create a
    mountpoint under an already read-only bind (verified impossible --
    the real repo's node_modules could not be used this way, which is
    exactly why open_job builds a real directory in the COPY instead).
    readonly also includes the gate-ro DIRECTORY (spec.gate_facts_path.
    parent, if this runner needs_gate -- the ProjectFacts snapshot plus
    a copy of the engine config, see _gate_ro_dir/_write_gate_facts) --
    a directory ro-bind, a SIBLING of the stations dir (never nested
    inside it), under the same hidden root, so the worker can read it
    (the gate hook does) but never alter, rename or replace it --
    see _gate_ro_dir's own docstring for why this has to be a
    separate directory rather than a file inside the writable stations
    dir. hidden = engine home (minus the worker's own
    copy/stations/worker-home dirs, re-added by the writable/readonly
    binds above/below), secrets_dir, THIS project's own repo (so e.g.
    a gitignored .env there is invisible to code the worker runs --
    the worker only ever touches its own copy, never the repo itself),
    every OTHER project's repo, PLUS the WHOLE directory holding the
    ledger file (see _ledger_hidden_root -- not merely the ledger file
    and its -wal/-shm/-journal companions by name, which would leave
    any other file in that directory, present or future, unmasked),
    PLUS the real operator HOME outright (read from THIS process's own
    os.environ, via `env.get("HOME")` -- `env` here is the runner's
    OWN env dict, built by engine.workers.base.worker_env from
    config.worker_env_passthrough, which only ever widens that set
    from THIS trusted engine process's os.environ, never from
    anything worker/case-controlled). net = True (the claude/codex CLI
    needs it). `env["HOME"]` itself is overwritten in place, here, to
    _worker_home_dir(ctx, spec) -- the SAME dict object the caller
    (_run_spawn_worker) goes on to pass to sandbox.exec_env for the
    actual Popen env, so the real HOME value never reaches the spawned
    process even under sandbox_mode="none" (no bwrap mount involved at
    all there, so hiding it in the mount list would do nothing by
    itself)."""
    repo = _repo(ctx)
    home_dir = _worker_home_dir(ctx, spec)
    home_env = env.get("HOME")
    real_home = Path(home_env) if home_env else None
    login_files = _login_files_for(runner)
    subpaths = getattr(runner, "home_writable_subpaths", ())
    sandbox.prepare_worker_home(home_dir, real_home, login_files, subpaths)
    env["HOME"] = str(home_dir)
    writable = (spec.copy_root, _stations_dir(ctx))
    safe_copy_link_mounts = _safe_copy_link_mounts(ctx, spec, repo)
    readonly = tuple(target for _entry, target in safe_copy_link_mounts) + (home_dir,)
    if spec.gate_facts_path is not None:
        # The whole gate-ro directory (gate_facts.json + a copy of the
        # engine config the gate needs for GB_CONFIG -- see
        # _gate_ro_dir/_write_gate_facts), never spec.gate_facts_path
        # alone: mounting just the one file would still leave its
        # PARENT directory entries (if ever exposed some other way)
        # open to the renameable-ancestor issue this directory exists
        # to avoid. This directory sits outside every writable mount
        # (sibling of the stations dir under the same hidden, then
        # --remount-ro'd, $HOME -- see engine.sandbox.wrap), so the
        # worker can read it but never rename, replace or write it.
        readonly += (spec.gate_facts_path.parent,)
    elif ctx.config.config_path is not None:
        # No gate for this role: nothing reads GB_CONFIG from it, but
        # keep the pre-existing direct mount in case anything else
        # needs config_path readable.
        readonly += (ctx.config.config_path,)
    writable_subpaths_config = ctx.config.projects[ctx.project].copy_link_writable_subpaths
    for entry, _target in safe_copy_link_mounts:
        link_subpaths = writable_subpaths_config.get(entry, ())
        if not link_subpaths:
            continue
        copy_entry = spec.copy_root / entry
        readonly += (copy_entry,)
        writable += tuple(copy_entry / subpath for subpath in link_subpaths)
    if runner_needs_gate(runner):
        # The gate hook imports this engine package inside the sandbox; when
        # the engine itself lives under a hidden tree (e.g. a checkout under
        # /tmp, which the sandbox replaces with an empty tmpfs) it must be
        # re-exposed read-only or every gated tool call fails closed.
        readonly += (ENGINE_ROOT,)
    writable += tuple(home_dir / sub for sub in subpaths)
    hidden = _hidden_roots(ctx, repo) + _ledger_hidden_root(ctx)
    if real_home is not None:
        hidden += (real_home,)
    try:
        return sandbox.wrap(
            argv, cwd=spec.workdir, writable=writable, hidden=hidden,
            readonly=readonly, net=True, mode=ctx.config.sandbox,
            child_env=env,
        )
    except sandbox.SandboxUnavailable as exc:
        raise StepFailed("sandbox_unavailable") from exc


def _run_spawn_worker(ctx: StepContext) -> dict[str, object]:
    spec, worker, runner = _full_spec(ctx)
    if runner.needs_gate:
        # Written/created HERE, once, before the worker ever runs -- not
        # inside _full_spec (recomputed by every step, including
        # record_station, which must read the log rather than reset it).
        _write_gate_facts(ctx)
        _init_gate_log(ctx)
    argv = runner.argv(spec, worker)
    env = runner.env(spec, worker)
    argv = _sandboxed_argv(ctx, spec, argv, env, runner)
    # Under mode="bwrap" the child's env is already baked into argv via
    # --setenv (see _sandboxed_argv); the Popen env must then be the
    # sandbox launcher's own sanitized env (sandbox.exec_env), never
    # `env` itself -- `env` can carry worker-controlled values that must
    # never reach the process that execs bwrap.
    popen_env = sandbox.exec_env(ctx.config.sandbox, env)
    proc = procs.spawn(
        ctx.ledger, ctx.project, ctx.attempt_id, spec.station, argv,
        cwd=spec.workdir, env=popen_env, stdout_path=_stdout_path(ctx),
        stdin_devnull=True, request_id=ctx.request.request_id,
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
        if _stdout_path(ctx).exists():
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
        (_stdout_path(ctx), _gate_log_path(ctx), _result_path(ctx)))
    if code is None:
        # Stuck (no output, no tool call for timeout_sec): stop it so the
        # orbit can start a continuation run on the same copy.
        procs.close_station(ctx.ledger, ctx.project, ctx.attempt_id, spec.station)
        # record_station, where this import normally happens, is never
        # reached once this raises -- StepFailed ends the request right
        # here, which would otherwise lose a timed-out worker's whole
        # gate-log audit trail. close_station already killed the
        # process group above, so this always runs AFTER it is dead.
        if not _gate_log_already_imported(ctx):
            _import_gate_log(ctx, spec)
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
        if not _gate_log_already_imported(ctx):
            _import_gate_log(ctx, spec)
        raise StepFailed("worker_failed")
    return {"returncode": code}


def _reconcile_await_worker(ctx: StepContext) -> bool:
    spec, _worker, _runner = _full_spec(ctx)
    live = ctx.ledger.live_procs(ctx.attempt_id, spec.station)
    return not live and _stdout_path(ctx).exists()


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


def _gate_log_already_imported(ctx: StepContext) -> bool:
    for _seq, event in ctx.ledger.events(ctx.project, "gate_log.imported"):
        if event.payload.get("request_id") == ctx.request.request_id:
            return True
    return False


_GATE_LOG_MAX_BYTES = 8 * 1024 * 1024  # 8 MiB

# The ONLY two kinds ever accepted from the worker-writable gate.log,
# and the exact field set/types allowed in each one's payload -- every
# OTHER field is rejected (the line is malformed), never passed
# through, since a worker controls every byte of this file.
# "allowed"/"reason"/"tool"/"path"/"target"/"command"
# mirror engine.policy.Policy.check's own real 'policy.decided' payload
# exactly; "tool_name"/"reason" mirror engine.gate's own real
# 'gate.denied' payload for an unrecognized tool.
_POLICY_DECIDED_FIELDS: dict[str, int] = {
    "reason": 80, "tool": 20, "path": 400, "target": 400, "command": 400,
}
_GATE_DENIED_FIELDS: dict[str, int] = {"reason": 80, "tool_name": 200}


def _validate_gate_log_payload(kind: str, payload: dict[str, object]) -> dict[str, object] | None:
    """A cleaned, type/length-checked payload for the one allowed field
    set of `kind`, or None if `payload` does not match at all (the
    line is then malformed) -- see _import_gate_log."""
    if kind == "policy.decided":
        str_fields = _POLICY_DECIDED_FIELDS
        if "allowed" not in payload or not isinstance(payload["allowed"], bool):
            return None
        cleaned: dict[str, object] = {"allowed": payload["allowed"]}
        required = ("reason",)
    elif kind == "gate.denied":
        str_fields = _GATE_DENIED_FIELDS
        cleaned = {}
        required = ("reason",)
    else:
        return None
    allowed_keys = set(str_fields) | ({"allowed"} if kind == "policy.decided" else set())
    if set(payload) - allowed_keys:
        return None
    for key in required:
        if key not in payload:
            return None
    for key, max_len in str_fields.items():
        if key not in payload:
            continue
        value = payload[key]
        if value is None:
            cleaned[key] = None
            continue
        if not isinstance(value, str) or len(value) > max_len:
            return None
        cleaned[key] = value
    return cleaned


class _GateLogOversize(Exception):
    def __init__(self, size: int) -> None:
        self.size = size
        super().__init__(f"gate log {size} bytes exceeds cap")


def _read_gate_log_no_symlink(path: Path) -> bytes | None:
    """The log's raw bytes, or None if it does not exist. Every
    directory component of `path` -- not just the final one -- is
    opened individually with O_NOFOLLOW via `dir_fd`: `os.open(path,
    O_NOFOLLOW)` by itself only refuses a symlink at the LAST path
    element, so a worker that replaces the writable per-request
    directory this log normally lives in (`path.parent`) with a
    symlink could otherwise redirect this read to an attacker-chosen
    file through an unprotected intermediate component. The leaf file
    is opened O_RDONLY|O_NONBLOCK: a FIFO planted there (not a
    symlink -- O_NOFOLLOW does not apply to a FIFO) would otherwise
    block this open indefinitely waiting for a writer. Once `fstat`
    confirms a plain regular file, O_NONBLOCK is cleared via `fcntl`
    before anything is read (regular-file reads are unaffected by it,
    but clearing it keeps the fd in its ordinary blocking state for
    any future use). Capped at _GATE_LOG_MAX_BYTES (raises
    _GateLogOversize rather than reading an unbounded amount of
    worker-controlled data into memory)."""
    # NEVER pre-resolve `path` (e.g. via realpath) before walking it --
    # that would follow exactly the symlink this function exists to
    # refuse. Each raw component is opened one at a time instead; any
    # one of them being a symlink (other than the anchor itself) makes
    # the corresponding O_NOFOLLOW open fail.
    components = path.parts
    try:
        dir_fd = os.open(components[0], os.O_DIRECTORY)
    except FileNotFoundError:
        return None
    try:
        for part in components[1:-1]:
            try:
                next_fd = os.open(part, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise StepFailed("gate_log_path_unsafe") from exc
            os.close(dir_fd)
            dir_fd = next_fd
        try:
            fd = os.open(components[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                        dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StepFailed("gate_log_path_unsafe") from exc
    finally:
        os.close(dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise StepFailed("gate_log_path_unsafe")
        if st.st_size > _GATE_LOG_MAX_BYTES:
            raise _GateLogOversize(st.st_size)
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
        return os.read(fd, st.st_size)
    finally:
        os.close(fd)


def _existing_gate_log_lines(
    ctx: StepContext, request_id: str,
) -> tuple[dict[int, dict[str, object]], set[int]]:
    """(decision_payloads, malformed_line_numbers) for THIS request_id
    only -- loaded from the ledger ONCE per import call, not once per
    log line. Re-querying ctx.ledger.events() (an O(total events in
    the project) scan) inside the per-line loop made a full import
    quadratic in the number of lines, letting a worker with a large
    (still under the 8 MiB cap) gate.log monopolize the processor
    while holding its lock."""
    decisions: dict[int, dict[str, object]] = {}
    for _seq, event in ctx.ledger.events(ctx.project, "gate_log.decision"):
        if event.payload.get("request_id") == request_id:
            line_no = event.payload.get("log_line")
            if isinstance(line_no, int):
                decisions[line_no] = event.payload
    malformed: set[int] = set()
    for _seq, event in ctx.ledger.events(ctx.project, "gate_log.malformed"):
        if event.payload.get("request_id") == request_id:
            line_no = event.payload.get("log_line")
            if isinstance(line_no, int):
                malformed.add(line_no)
    return decisions, malformed


def _gate_log_event_already_recorded(ctx: StepContext, kind: str, request_id: str) -> bool:
    """Idempotency for the one-shot oversize/missing audit events below
    -- a crash between appending one of these and the final
    'gate_log.imported' marker must not duplicate it on resume."""
    for _seq, event in ctx.ledger.events(ctx.project, kind):
        if event.payload.get("request_id") == request_id:
            return True
    return False


def _journal_gate_log_malformed(ctx: StepContext, request_id: str, line_no: int,
                                 raw_line: str) -> None:
    ctx.ledger.append(Event(
        project=ctx.project, kind="gate_log.malformed", actor_kind="processor",
        actor_id="processor", attempt_id=ctx.attempt_id,
        payload={"request_id": request_id, "log_line": line_no, "raw": raw_line[:200]},
    ))


def _import_gate_log(ctx: StepContext, spec: WorkerSpec) -> None:
    """Imports this request's gate.log (the gate hook's own, worker-
    writable decision log -- see engine.gate's module docstring) into
    the real ledger -- but NEVER by trusting a single byte of the log
    line's own 'kind'/'project'/'attempt_id'/'actor_id' fields. Those
    fields, if forwarded verbatim into a real ledger.append() (which
    dispatches purely on `kind`, with no awareness of who is asking),
    would let a worker-forged line like {"kind": "approval.granted",
    ...} or {"kind": "station.finished", ...} trigger that kind's real
    derived-table handler (or satisfy a reconcile() query keyed on that
    kind) -- a worker could grant itself an approval, or make
    record_station's own reconcile believe this request was already
    finished, by writing one line to a file it already had write
    access to for an entirely different reason (the audit log).

    Every imported line is instead stored under ONE single, inert kind
    -- 'gate_log.decision' -- which has no _HANDLERS entry (see
    engine.ledger) and is never matched by any reconcile() query in
    this codebase (none of which ever look for that kind): forging
    ANY content into it can therefore never trigger a derived-table
    effect or make a later step believe something happened that did
    not. engine.ledger.Ledger.append() also independently refuses
    (actor_kind="gate_log") any kind other than 'gate_log.decision' as
    defense in depth, so even a future bug here that reintroduced
    verbatim forwarding would still not reach a real handler.

    project/attempt_id/actor_id/request_id on the appended event all
    come from `ctx`/`spec` -- the trusted engine context -- NEVER from
    the log line, which only survives (if it parses) as 'orig_kind'
    plus a strictly validated payload subset (see
    _validate_gate_log_payload): a worker cannot change what project,
    attempt or request its own forged lines get attributed to either.

    A line that is not valid JSON, not an object, has a 'kind' other
    than 'policy.decided'/'gate.denied', or has a payload that does not
    match that kind's exact allowed-field schema is never appended as
    a decision at all -- it is counted malformed and recorded as its
    own 'gate_log.malformed' event instead (same inert treatment: no
    handler, no reconcile match). The log itself is opened with
    O_NOFOLLOW, must be a plain regular file, and is capped at 8 MiB
    (over that: a single 'gate_log.oversize' audit event, nothing
    imported); a missing log yields one 'gate_log.missing' event.

    The whole import is crash-idempotent: each line is skipped if an
    event already exists for its exact (request_id, log_line) --
    whether from an earlier, interrupted run of this exact function --
    and the final 'gate_log.imported' marker (whose allowed/denied/
    malformed totals are always a fresh tally over the ledger's own
    gate_log.decision/malformed events for this request_id, never
    local counters, so a resumed partial import still produces one
    accurate summary) is always written LAST.

    PLAIN FACT, not a weakened guarantee, even after all of the above:
    this log lives in a worker-writable directory (the stations dir),
    so its very existence and every byte in it is still audit evidence
    a worker could have forged or suppressed -- that no longer lets a
    forged line change what the ledger believes happened, but it does
    not affect ENFORCEMENT either way, which already happened (the
    gate hook's own exit code, decided before the line was ever
    written, is what actually blocked or allowed the tool)."""
    request_id = ctx.request.request_id
    status = "ok"
    lines: list[str] = []
    # Loaded ONCE, not re-queried per line (see _existing_gate_log_lines).
    decisions, malformed_lines = _existing_gate_log_lines(ctx, request_id)
    if spec.gate_log_path is not None:
        try:
            raw = _read_gate_log_no_symlink(spec.gate_log_path)
        except _GateLogOversize as exc:
            status = "oversize"
            if not _gate_log_event_already_recorded(ctx, "gate_log.oversize", request_id):
                ctx.ledger.append(Event(
                    project=ctx.project, kind="gate_log.oversize", actor_kind="processor",
                    actor_id="processor", attempt_id=ctx.attempt_id,
                    payload={"request_id": request_id, "size": exc.size},
                ))
        else:
            if raw is None:
                status = "missing"
                if not _gate_log_event_already_recorded(ctx, "gate_log.missing", request_id):
                    ctx.ledger.append(Event(
                        project=ctx.project, kind="gate_log.missing", actor_kind="processor",
                        actor_id="processor", attempt_id=ctx.attempt_id,
                        payload={"request_id": request_id},
                    ))
            else:
                try:
                    lines = raw.decode("utf-8").splitlines()
                except UnicodeDecodeError:
                    status = "undecodable"
                    if 0 not in malformed_lines:
                        _journal_gate_log_malformed(ctx, request_id, 0, "<undecodable>")
                        malformed_lines.add(0)

    for line_no, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        if line_no in decisions or line_no in malformed_lines:
            continue
        try:
            entry = json.loads(raw_line)
            if not isinstance(entry, dict):
                raise ValueError("gate log line is not a JSON object")
            kind = entry.get("kind")
            payload = entry.get("payload")
            if not isinstance(kind, str) or not isinstance(payload, dict):
                raise ValueError("missing or malformed kind/payload")
            cleaned = _validate_gate_log_payload(kind, payload)
            if cleaned is None:
                raise ValueError("unrecognized kind or payload shape")
        except (json.JSONDecodeError, ValueError, TypeError):
            _journal_gate_log_malformed(ctx, request_id, line_no, raw_line)
            malformed_lines.add(line_no)
            continue
        decided_payload = {"orig_kind": kind, **cleaned,
                           "request_id": request_id, "log_line": line_no}
        ctx.ledger.append(Event(
            project=ctx.project, kind="gate_log.decision",
            actor_kind="gate_log", actor_id=spec.role, attempt_id=ctx.attempt_id,
            payload=decided_payload,
        ))
        decisions[line_no] = decided_payload

    allowed = sum(1 for p in decisions.values()
                 if p.get("orig_kind") == "policy.decided" and p.get("allowed") is True)
    denied = len(decisions) - allowed
    malformed = len(malformed_lines)

    ctx.ledger.append(Event(
        project=ctx.project, kind="gate_log.imported", actor_kind="processor",
        actor_id="processor", attempt_id=ctx.attempt_id,
        payload={"request_id": request_id, "status": status, "lines": len(lines),
                 "allowed": allowed, "denied": denied, "malformed": malformed},
    ))


def _run_record_station(ctx: StepContext) -> dict[str, object]:
    spec, _worker, runner = _full_spec(ctx)
    if not _gate_log_already_imported(ctx):
        _import_gate_log(ctx, spec)
    result = runner.parse_result(_stdout_path(ctx), spec)
    changed = _changed_paths(ctx, spec)
    ctx.ledger.append(Event(
        project=ctx.project, kind="station.finished", actor_kind="processor",
        actor_id="processor", attempt_id=ctx.attempt_id,
        payload={
            "request_id": ctx.request.request_id, "station": spec.station,
            "role": spec.role, "ok": result.ok, "summary": result.summary,
            "cost_usd": result.cost_usd, "turns": result.turns,
            "changed_paths": list(changed), "requests": list(result.requests),
        },
    ))
    record_path = _record_path(ctx)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps({
        "request_id": ctx.request.request_id, "ok": result.ok,
        "summary": result.summary, "changed_paths": list(changed),
        "requests": list(result.requests),
    }), encoding="utf-8")
    if not result.ok:
        raise StepFailed("worker_reported_failure")
    return {"ok": True, "changed_paths": list(changed), "requests": list(result.requests)}


def _reconcile_record_station(ctx: StepContext) -> bool:
    for _seq, event in ctx.ledger.events(ctx.project, "station.finished"):
        if event.payload.get("request_id") == ctx.request.request_id:
            return True
    return False


def steps(ctx: StepContext) -> list[StepDef]:
    return [
        StepDef("spawn_worker", _run_spawn_worker, _reconcile_spawn_worker),
        StepDef("await_worker", _run_await_worker, _reconcile_await_worker),
        StepDef("record_station", _run_record_station, _reconcile_record_station),
    ]
