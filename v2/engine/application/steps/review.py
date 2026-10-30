"""review — launch a reviewer (claude/codex/scripted) read-only against
an isolated snapshot of the project repo's current tree against this
attempt's base_head, and record pass/fail evidence bound to a
fingerprint.

Request payload contract: {"role": str, "task": str (optional, "" if
absent) -- the ORIGINAL request text the builder was given, shown to
the reviewer so it judges the change against what was actually asked
for}.

compute_fingerprint  fp = evidence.fingerprint(repo); journal {fp}.
                     reconcile False -- a crash mid-run always
                     re-computes rather than trusting a half-written row.
spawn_reviewer       _export_review_snapshot(fp) extracts the EXACT
                     fingerprinted tree (git ls-tree + cat-file blob,
                     never `git archive`, which honors .gitattributes
                     export-ignore/export-subst) into a fresh per-
                     request directory outside the live repo; workdir
                     = that snapshot, never the repo itself (isolates
                     the reviewer from a concurrently running bench
                     with write access to the live repo). prompt =
                     prompts.review_prompt(spec, task, changed_files,
                     diff_text, coverage_sent): changed_files from
                     `git diff --name-status base_head fp`; diff_text
                     from one `git diff base_head fp` (two already-
                     fixed tree-ish objects -- fp's own construction
                     already unions committed/staged/untracked content
                     and applies excludes, so this single diff covers
                     everything `git diff`'s separate committed-range/
                     working-tree/untracked-pseudo-diff used to need
                     three steps for); _prepare_diff_for_prompt sends
                     the whole diff (chunked into <=60k-char pieces)
                     when it fits a 240k-char total budget, else only
                     the file list plus as much of the diff as fits,
                     marking coverage_sent "partial" -- the reviewer's
                     own JSON reply is then REQUIRED to echo this back
                     as "coverage". Same spawn/reconcile shape as
                     station.spawn_worker.
await_reviewer       same shape as station.await_worker.
record_review        recompute the fingerprint; if it differs from
                     compute_fingerprint's, StepFailed
                     ("tree_changed_during_review") -- never record
                     evidence against a tree that moved under the
                     reviewer. A missing verdict ->
                     StepFailed("no_verdict"); a present but malformed
                     one (not matching the reviewer's own JSON schema
                     closely enough to derive a verdict from -- see
                     _verdict_is_well_formed) -> StepFailed
                     ("review_malformed"), before anything is derived
                     from it or recorded. `passed` is NOT the
                     reviewer's own verdict boolean (kept separately as
                     "reviewer_passed") but derived from `findings` AND
                     `coverage`: True iff no finding has severity P0 or
                     P1 AND coverage == "full" (a reviewer that returns
                     passed=false over P2/P3 findings alone must not
                     block the job; one that only saw part of the diff
                     can never pass, regardless of findings, since "no
                     blocking findings" from a partial read proves
                     nothing about the part never seen).
                     evidence.record(kind="review", fp, payload=
                     {"passed", "reviewer_passed", "findings",
                     "coverage", "request_id"}) FIRST, then, if not
                     passed, raise StepFailed("review_incomplete") for
                     coverage != "full" or StepFailed
                     ("review_not_passed") otherwise -- same pattern as
                     bench_run.record_bench: the evidence stays visible
                     even though the request ends 'failed', and the
                     orbit stops here rather than continuing on to
                     bench/commit on a tree a review never actually
                     passed (or never actually finished reviewing).
"""
from __future__ import annotations

import dataclasses
import shutil
import subprocess
from pathlib import Path

from engine.infrastructure import evidence
from engine.application import repair as fixup
from engine.domain import ids
from engine.infrastructure import procs
from engine.infrastructure import sandbox
from engine.application.steps import worker_shell_jail
from engine.infrastructure.reference_snapshot import ensure_snapshot_for_attempt
from engine.config import McpServerConfig
from engine.infrastructure.gitutil import git_argv
from engine.domain.records import Event
from engine.application.processor import StepContext, StepDef, StepFailed
from engine.infrastructure.workers.base import WorkerSpec, wait_for_exit, wait_while_progressing
from engine.infrastructure.workers.prompts import review_prompt

_CHUNK_MAX_CHARS = 60_000
# Up to 4 chunks' worth sent in one prompt before the diff counts as too
# large to send in full -- see _prepare_diff_for_prompt.
_TOTAL_DIFF_BUDGET = 4 * _CHUNK_MAX_CHARS


def _project_config(ctx: StepContext):
    return ctx.config.projects[ctx.project]


def _repo(ctx: StepContext) -> Path:
    return _project_config(ctx).repo


def _excludes(ctx: StepContext) -> tuple[str, ...]:
    return _project_config(ctx).excludes


def _file_dep_targets(ctx: StepContext) -> tuple[tuple[str, str], ...]:
    """ProjectConfig.file_dep_targets -- fed to evidence.with_file_deps
    so the EVIDENCE fingerprint record_review records (compared by
    three_match at commit time) also covers a `file:../...` shared
    package's own content, never the tree fingerprint used right above
    it for tree_changed_during_review (a review finding,: that
    check, and _export_review_snapshot's git diff/ls-tree calls, must
    stay on the plain git tree hash -- only the evidence.record() call
    gets the combined one)."""
    return _project_config(ctx).file_dep_targets


def _stations_dir(ctx: StepContext) -> Path:
    return ctx.config.home / "jobs" / ids.attempt_dirname(ctx.attempt_id) / "stations"


def _stdout_path(ctx: StepContext) -> Path:
    return _stations_dir(ctx) / f"{ctx.request.request_id}.stdout"


def _result_path(ctx: StepContext) -> Path:
    return _stations_dir(ctx) / f"{ctx.request.request_id}.result.json"


def _journal_payload(ctx: StepContext, step_name: str) -> dict[str, object]:
    row = ctx.ledger.steps(ctx.request.request_id).get(step_name)
    if row is None:
        raise StepFailed(f"missing_step:{step_name}")
    return row.payload


def _base_head(ctx: StepContext) -> str:
    for _seq, event in ctx.ledger.events(ctx.project, "job.opened"):
        if event.payload.get("attempt_id") == ctx.attempt_id:
            base_head = event.payload.get("base_head")
            if base_head:
                return str(base_head)
            break
    raise StepFailed("no_base_head")


def _run_compute_fingerprint(ctx: StepContext) -> dict[str, object]:
    try:
        fp = evidence.fingerprint(_repo(ctx), _excludes(ctx))
    except evidence.FingerprintError as exc:
        raise StepFailed("no_fingerprint") from exc
    submitted = ctx.request.payload.get("fp")
    if submitted is not None and submitted != fp:
        raise StepFailed("tree_changed_since_submit")
    return {"fp": fp}


def _review_snapshot_dir(ctx: StepContext) -> Path:
    """An engine-owned directory OUTSIDE the live repo, one per
    request, holding an extracted copy of the fingerprinted tree
    (see _export_review_snapshot) -- the reviewer's own workdir/
    sandbox root, so its own Read/Grep/Bash tool calls can never
    observe the live repo at all, regardless of what a concurrently
    running bench does to it: bench has write access to the SAME live
    repo a reviewer reading it directly would be exposed to, and a
    test that temporarily changes A->B then restores A->B->A would
    leave identical before/after fingerprints even though the reviewer
    actually read B in between -- isolating the reviewer's filesystem
    view, not merely bookending it with fingerprint checks, closes
    this regardless of timing."""
    return (ctx.config.home / "jobs" / ids.attempt_dirname(ctx.attempt_id)
            / "review-snapshot" / ctx.request.request_id)


def _export_review_snapshot(ctx: StepContext, repo: Path, fp: str) -> Path:
    """Extracts the tree object `fp` into a fresh directory via
    evidence.export_tree -- see that function's own docstring for why
    `git archive` (export-ignore/export-subst) is wrong for this and
    why it now lives in engine.infrastructure.evidence rather than here (engine.
    steps.bench_run reuses it for engine.application.acceptance's and its own
    disposable snapshots)."""
    dest = _review_snapshot_dir(ctx)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    try:
        evidence.export_tree(repo, fp, dest)
    except evidence.TreeExportError as exc:
        raise StepFailed("review_snapshot_path_unsafe") from exc
    _strip_stale_acceptance_records(dest)
    return dest


def _strip_stale_acceptance_records(dest: Path) -> None:
    """Removes every `.gongbang/<job>/acceptance.json` export_tree just
    copied into the reviewer's own snapshot (결함 62): engine.application.
    acceptance.run_acceptance writes that exact file only AFTER
    build+review+bench already passed for some attempt, so whatever
    the live repo tree carries under this name at REVIEW time -- i.e.
    before this diff's own bench has even run -- is always a prior
    attempt's leftover. A reviewer is a real AI worker whose workdir
    IS this snapshot (see _full_spec's WorkerSpec(workdir=snapshot)),
    so it can open this file directly with its own Read/Grep tools,
    not only read the diff text the prompt sends it -- and did exactly
    that in the wild, reporting the current attempt as failing an
    acceptance run that, for it, had not happened yet
    (R-fc1adf80f9d70852). The job's OTHER .gongbang/<job>/ records
    (facts.md, plan.json, scenes) are left untouched -- those stay
    legitimate context for a plan-review diff (see _is_plan_review_diff
    in engine.infrastructure.workers.prompts).

    Imports ACCEPTANCE_JSON_FILENAME lazily (not at module level):
    engine.application.acceptance imports engine.application.steps (for
    bench_env/suite_snapshot helpers), and engine.application.steps'
    own __init__ imports this module -- a module-level import here
    would close that into a circular import."""
    from engine.application.acceptance import ACCEPTANCE_JSON_FILENAME

    gongbang_dir = dest / ".gongbang"
    if not gongbang_dir.is_dir():
        return
    for stale in gongbang_dir.glob(f"*/{ACCEPTANCE_JSON_FILENAME}"):
        stale.unlink()


def _diff_text(ctx: StepContext, fp: str) -> str:
    """The diff the reviewer should see: base_head's commit against
    `fp` -- ONE git diff between two already-fixed tree-ish objects
    already in the repo's own object database, never a live multi-
    step comparison (a committed-range diff, a separate working-tree
    diff, and a separate untracked-file listing, taken as three
    different snapshots in time, as this used to be) that a
    concurrently running bench could change the live repo in between.
    fp's own tree construction (evidence.range_tree) already unions
    HEAD-tracked, staged and untracked-non-ignored content and applies
    the project's `excludes` (an excluded-but-tracked path keeps its
    HEAD blob rather than its live content; an excluded-and-never-
    tracked path is omitted entirely), so this one diff captures
    committed changes, working-tree edits AND brand-new untracked
    files without any separate pseudo-diff step, and never shows
    anything excludes would have hidden anyway."""
    repo = _repo(ctx)
    base_head = _base_head(ctx)
    result = subprocess.run(
        git_argv(repo, "diff", base_head, fp),
        capture_output=True, text=True,
    )
    return result.stdout if result.returncode == 0 else ""


def _changed_files(ctx: StepContext, fp: str) -> list[str]:
    """One line per changed path from `git diff --name-status base_head
    fp` -- the SAME two already-fixed tree-ish objects _diff_text
    diffs, so this list and the diff text always describe the same
    change. Given to the reviewer as an overview even when the full
    diff cannot be (see _prepare_diff_for_prompt)."""
    repo = _repo(ctx)
    base_head = _base_head(ctx)
    result = subprocess.run(
        git_argv(repo, "diff", "--name-status", base_head, fp),
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def _diff_chunks(text: str, chunk_size: int = _CHUNK_MAX_CHARS) -> list[str]:
    if not text:
        return []
    return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]


def _prepare_diff_for_prompt(full_diff: str) -> tuple[str, str]:
    """(diff_text_to_send, coverage): "full" sends the WHOLE diff --
    however many <=60k-char chunks that takes, concatenated back
    together with chunk headers -- when it fits within the 240k-char
    total budget; "partial" sends only as much as fits that same
    budget when the full diff is larger than it. The reviewer's own
    verdict JSON is REQUIRED to echo this back as "coverage" (see
    review_prompt / _verdict_is_well_formed) -- never silently assumed
    "full" here just because some number of bytes happened to be
    sent, and a "partial" verdict can never pass review regardless of
    what findings it reports (see _run_record_review): a reviewer that
    did not see the whole change proves nothing about the part it
    never saw."""
    if len(full_diff) <= _TOTAL_DIFF_BUDGET:
        sendable, coverage = full_diff, "full"
    else:
        sendable, coverage = full_diff[:_TOTAL_DIFF_BUDGET], "partial"
    chunks = _diff_chunks(sendable)
    if not chunks:
        return "(no diff)", coverage
    labeled = "\n".join(
        f"--- diff chunk {i + 1}/{len(chunks)} ---\n{chunk}"
        for i, chunk in enumerate(chunks)
    )
    return labeled, coverage


def _payload_mcp_names(payload: dict[str, object]) -> tuple[str, ...]:
    """The "mcp" list of this request's optional "tools" payload entry
    (engine.domain.plan.tools_for_station's shape -- same contract as
    engine.application.steps.worker_step's own helper of the same name). () for a
    payload that carries no "tools" key, including orbit.default_
    stages()/stages_after_install()'s own review payload today."""
    tools = payload.get("tools")
    if not isinstance(tools, dict):
        return ()
    raw = tools.get("mcp", [])
    if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
        raise StepFailed("invalid_tools_mcp")
    return tuple(raw)


def _allowed_mcp_servers(ctx: StepContext) -> dict[str, McpServerConfig]:
    """This request's payload "mcp" names, narrowed to BOTH
    config.station_mcp_allow["review"] and config.mcp_servers -- same
    narrowing engine.application.steps.worker_step._allowed_mcp_servers applies for a
    run_station request."""
    requested = set(_payload_mcp_names(ctx.request.payload))
    station_allow = set(ctx.config.station_mcp_allow.get("review", ()))
    names = sorted(requested & station_allow & set(ctx.config.mcp_servers))
    return {name: ctx.config.mcp_servers[name] for name in names}


def _required_unapplied_skills_note(ctx: StepContext) -> str:
    """"" (nothing to add), or a short note naming the station-required
    skills (ContextConfig.required_skills) this attempt's own builder
    station injected but never reported applying -- the ledger's
    "skills.unapplied" event engine.application.steps.worker_step._record_skills_unapplied
    appends at that station's own record_station step (G20: a plan that
    marks a skill "반드시" must have a required-skill gap REACH the
    review, not just sit unseen on the ledger). Filtered to THIS
    attempt_id's own events only (unlike worker_step's own
    _latest_injected_skills, which cannot filter by attempt_id for the
    injected-skills lookup itself -- this event is always appended with
    the real, already-open attempt_id record_station runs under, never
    None, so filtering by it here is exact, not a sequencing guess).
    Only "required_unapplied" ever surfaces here -- a plain, non-
    required unapplied skill stays a ledger-only annotation, same as
    ContextConfig.required_skills' own existing missing-skill contract
    (see engine.infrastructure.context's module docstring)."""
    required: list[str] = []
    for _seq, event in ctx.ledger.events(ctx.project, "skills.unapplied"):
        if event.attempt_id != ctx.attempt_id:
            continue
        names = event.payload.get("required_unapplied", [])
        if isinstance(names, list):
            for name in names:
                if name not in required:
                    required.append(str(name))
    if not required:
        return ""
    return (
        "[필수 스킬 미적용] 이 계획이 반드시 적용하도록 지정한 스킬이 적용 보고 "
        f"없이 지나갔다: {', '.join(required)}. 이를 지적(findings)에 포함해 "
        "판단한다."
    )


def _full_spec(ctx: StepContext) -> tuple[WorkerSpec, object, object, str]:
    role = str(ctx.request.payload["role"])
    # The ORIGINAL request text the builder was given -- "" for a
    # request submitted before this field existed, or one built by a
    # caller that never set it (e.g. stages_after_install()'s own
    # default, or a hand-built request). Never trusted for anything
    # but display: the reviewer judges the change against it, but
    # nothing here re-derives write permissions or scope from it. If
    # the request itself carries none, recover it from the attempt's
    # own run_station request (engine.application.repair.original_task reads the
    # ledger directly, independent of what this particular request
    # happened to be submitted with) rather than silently reviewing
    # against an empty task -- a reviewer with no idea what was asked
    # for cannot meaningfully judge whether the change satisfies it,
    # and an empty task must never look indistinguishable from "no
    # task was needed". If NEITHER source has one, fail closed the
    # same way a partial-coverage verdict does: a review that cannot
    # even state what it is reviewing against is never "done" let
    # alone "passed".
    original_task = str(ctx.request.payload.get("task", ""))
    if not original_task:
        original_task = fixup.original_task(ctx.ledger, ctx.project, ctx.attempt_id)
    if not original_task:
        raise StepFailed("review_incomplete")
    worker = ctx.config.workers.get(role)
    if worker is None:
        raise StepFailed("no_worker_config")
    # codex's own CLI (the only reviewer runner this engine ships today
    # that is read_only) has no --mcp-config of its own here -- a
    # review request that carries mcp tools for a codex reviewer must
    # fail clearly rather than silently run with none of the mcp
    # access the request actually asked for (see
    # _allowed_mcp_servers/_payload_mcp_names: narrowing that down to
    # {} and proceeding would look identical to a request that never
    # asked for mcp at all).
    if worker.runner == "codex" and _payload_mcp_names(ctx.request.payload):
        raise StepFailed("reviewer_mcp_unsupported")
    runner = ctx.fakes.runners.get(worker.runner)
    if runner is None:
        raise StepFailed("no_runner")
    if not runner.read_only:
        # A reviewer runs against a read-only snapshot of the repo,
        # never a writable copy: only a runner that cannot write
        # (codex read-only sandbox, scripted read_only) may be used.
        raise StepFailed("reviewer_not_read_only")
    if runner.needs_gate and ctx.config.config_path is None:
        raise StepFailed("no_config_path")
    fp = str(_journal_payload(ctx, "compute_fingerprint")["fp"])
    snapshot = _review_snapshot_dir(ctx)
    generation = ctx.ledger.attempt(ctx.attempt_id).generation
    base_spec = WorkerSpec(
        project=ctx.project, attempt_id=ctx.attempt_id, generation=generation,
        station="review", role=role, copy_root=snapshot, workdir=snapshot,
        task="", result_path=_result_path(ctx), config_path=ctx.config.config_path,
        env_passthrough=ctx.config.worker_env_passthrough,
        mcp_servers=_allowed_mcp_servers(ctx),
    )
    changed_files = _changed_files(ctx, fp)
    diff_text, coverage_sent = _prepare_diff_for_prompt(_diff_text(ctx, fp))
    full_task = review_prompt(base_spec, original_task, changed_files,
                              diff_text, coverage_sent)
    # The review station's own injected context (its skills, library
    # items), sent by the orbit separately from the task being judged.
    review_context = ctx.request.payload.get("context")
    if isinstance(review_context, str) and review_context.strip():
        full_task = f"{full_task}\n\n{review_context.strip()}"
    required_unapplied_note = _required_unapplied_skills_note(ctx)
    if required_unapplied_note:
        full_task = f"{full_task}\n\n{required_unapplied_note}"
    spec = dataclasses.replace(base_spec, task=full_task)
    return spec, worker, runner, coverage_sent


def _other_project_repos(ctx: StepContext) -> tuple[Path, ...]:
    return tuple(p.repo for name, p in ctx.config.projects.items()
                 if name != ctx.project)


def _ledger_hidden_root(ctx: StepContext) -> tuple[Path, ...]:
    """The WHOLE directory holding the ledger file -- see
    engine.application.steps.worker_step's identical helper for why this is a
    directory hide (tmpfs, then remounted read-only) rather than
    masking the ledger file and its SQLite WAL/SHM/rollback-journal
    companions by name, which leaves any other file in that directory
    unmasked. The reviewer (codex, read-only) runs no gate hook of its
    own and never had any business touching the ledger; it is hidden
    here outright the same way station.py's worker sandbox now is."""
    return (ctx.config.ledger_path.parent,)


# Same table as engine.application.steps.worker_step._RUNNER_LOGIN_FILES -- the
# SMALLEST set of real-HOME-relative files each real CLI needs copied
# into its own fresh, per-attempt reviewer HOME to authenticate, named
# explicitly by the team lead's own decision. Kept as a
# separate copy (not imported from engine.application.steps.worker_step) so this
# module's own diff stays confined to the functions that build its
# sandbox mounts/HOME, same restriction engine.application.steps.worker_step is under.
_RUNNER_LOGIN_FILES: dict[str, tuple[str, ...]] = {
    "ClaudeRunner": (".claude/.credentials.json", ".claude.json"),
    "CodexRunner": (".codex/auth.json",),
}


def _login_files_for(runner: object) -> tuple[str, ...]:
    return _RUNNER_LOGIN_FILES.get(type(runner).__name__, ())


def _reviewer_home_dir(ctx: StepContext, spec: WorkerSpec) -> Path:
    """A fresh, per-attempt-and-station $HOME for this reviewer's real
    CLI process -- never the real operator HOME. See engine.infrastructure.sandbox.
    prepare_worker_home's docstring and engine.application.steps.worker_step's own
    identically-shaped _worker_home_dir for why. Keyed by station AND
    role, same reasoning as station.py's helper (parallel_review_bench
    can run a builder and a reviewer for the SAME attempt_id
    concurrently)."""
    return (ctx.config.home / "jobs" / ids.attempt_dirname(ctx.attempt_id)
            / "worker-home" / spec.station / spec.role)


def _sandboxed_argv(ctx: StepContext, spec: WorkerSpec, argv: list[str],
                     env: dict[str, str], runner: object) -> list[str]:
    """Reviewer sandbox policy: writable = the stations dir (its output
    files) and -- only if the real HOME exists -- the runner's own
    `home_writable_subpaths` under THIS REVIEWER'S OWN fresh home
    directory (_reviewer_home_dir), never under the real HOME (same
    reasoning as station.py's worker: a real CLI's credential/session
    state, never the whole real HOME). The ledger directory is NOT
    writable (no gate hook runs for the reviewer, so nothing here ever
    needed to append to it). readonly = _reviewer_home_dir(ctx, spec)
    itself (mounted as $HOME; sandbox.prepare_worker_home seeds it,
    before this mount, with ONLY the real CLI's own login files named
    in _RUNNER_LOGIN_FILES) AND the per-request review SNAPSHOT
    directory (spec.workdir -- the extracted, already-fingerprinted
    tree, never the live project repo: see _export_review_snapshot).
    hidden = engine home (minus the stations dir and the snapshot dir,
    both re-added above/below), secrets_dir, THIS project's own repo
    (the reviewer's own tool calls must never reach the live, mutable
    repo at all -- a concurrently running bench writing to it must not
    be observable through any absolute-path reference, only through
    the frozen snapshot), every OTHER project's repo, the project's
    own `hidden_paths`, PLUS the WHOLE directory holding the ledger
    file (see _ledger_hidden_root), PLUS the real operator HOME outright
    (same reasoning as station.py's worker -- read from THIS process's
    own os.environ via `env.get("HOME")`, never from anything case-
    controlled). net=True (codex/claude need it). `env["HOME"]` is
    overwritten in place, here, to _reviewer_home_dir(ctx, spec) -- the
    SAME dict object the caller (_run_spawn_reviewer) goes on to pass to
    sandbox.exec_env, so the real HOME value never reaches the spawned
    process even under sandbox_mode="none"."""
    home_dir = _reviewer_home_dir(ctx, spec)
    home_env = env.get("HOME")
    real_home = Path(home_env) if home_env else None
    login_files = _login_files_for(runner)
    subpaths = getattr(runner, "home_writable_subpaths", ())
    sandbox.prepare_worker_home(home_dir, real_home, login_files, subpaths)
    env["HOME"] = str(home_dir)
    writable = (_stations_dir(ctx),)
    from engine.application.steps.worker_step import ENGINE_ROOT
    from engine.application.steps.worker_sandbox import CLI_SHELL_SNAPSHOT_SUBDIR
    (home_dir / CLI_SHELL_SNAPSHOT_SUBDIR).mkdir(parents=True, exist_ok=True)

    # The reviewer may import this engine package inside the sandbox too.
    # reference_paths (ProjectConfig): same engine-owned, read-only
    # SNAPSHOT the worker sandbox re-exposes (engine.application.steps.
    # worker_sandbox.sandboxed_argv, engine.infrastructure.
    # reference_snapshot) -- never the configured source path itself --
    # so a reviewer can open and read it too, to actually check a
    # worker's output against the source material rather than take the
    # prompt's own paraphrase on faith.
    snapshot_entries = ensure_snapshot_for_attempt(ctx.config, ctx.project, ctx.attempt_id)
    readonly: tuple[Path, ...] = ((spec.workdir, home_dir, ENGINE_ROOT)
                                  + tuple(entry.path for entry in snapshot_entries))
    # Same reasoning as engine.application.steps.worker_step's own `_sandboxed_argv`: the
    # runner's own CLI, resolved from argv[0] by PATH, must stay visible
    # even though the real HOME (where it is usually installed) is hidden
    # below. See sandbox.resolve_runtime's docstring.
    symlinks: tuple[tuple[Path, Path], ...] = ()
    if argv:
        runtime = sandbox.resolve_runtime(argv[0])
        readonly += runtime.readonly
        symlinks += runtime.symlinks
    writable += tuple(home_dir / sub for sub in subpaths)
    hidden = ((ctx.config.home, ctx.config.secrets_dir, _repo(ctx))
              + _other_project_repos(ctx) + _ledger_hidden_root(ctx)
              + ctx.config.projects[ctx.project].hidden_paths)
    if real_home is not None:
        hidden += (real_home,)

    def wrap_cli(extra_writable: tuple[Path, ...] = (),
                 extra_readonly: tuple[Path, ...] = ()) -> list[str]:
        return sandbox.wrap(
            argv, cwd=spec.workdir, writable=writable + extra_writable, hidden=hidden,
            readonly=readonly + extra_readonly, symlinks=symlinks, net=True,
            mode=ctx.config.sandbox, child_env=env,
            protected_roots=(ctx.config.secrets_dir,),
        )

    # A reviewer CLI with a shell prefix (claude) runs its commands in the
    # network-less second layer: the frozen snapshot, read-only, and none
    # of the CLI's own HOME.
    command_readonly = ((spec.workdir, ENGINE_ROOT, home_dir / CLI_SHELL_SNAPSHOT_SUBDIR)
                        + tuple(entry.path for entry in snapshot_entries))
    try:
        return worker_shell_jail.wrap_for_runner(
            runner, mode=ctx.config.sandbox, env=env, wrap_cli=wrap_cli,
            root=worker_shell_jail.jail_root(
                ctx.config.home, ids.attempt_dirname(ctx.attempt_id), spec.station, spec.role),
            policy=lambda: worker_shell_jail.CommandPolicy(
                cwd=spec.workdir, writable=(), readonly=command_readonly,
                readonly_remap=(), hidden=hidden + sandbox.HOST_SOCKET_DIRS),
            protected_roots=(ctx.config.secrets_dir,))
    except sandbox.SandboxUnavailable as exc:
        raise StepFailed("sandbox_unavailable") from exc


def _run_spawn_reviewer(ctx: StepContext) -> dict[str, object]:
    fp = str(_journal_payload(ctx, "compute_fingerprint")["fp"])
    _export_review_snapshot(ctx, _repo(ctx), fp)
    spec, worker, runner, _coverage_sent = _full_spec(ctx)
    argv = runner.argv(spec, worker)
    env = runner.env(spec, worker)
    argv = _sandboxed_argv(ctx, spec, argv, env, runner)
    popen_env = sandbox.exec_env(ctx.config.sandbox, env)
    # Same stdin-vs-argv split as engine.application.steps.worker_step's own
    # _run_spawn_worker -- see WorkerRunner.prompt_via_stdin's docstring.
    stdin_path = None
    if getattr(runner, "prompt_via_stdin", False):
        stdin_path = procs.stdin_file_for(_stdout_path(ctx))
        procs.write_stdin_file(stdin_path, spec.task)
    proc = procs.spawn(
        ctx.ledger, ctx.project, ctx.attempt_id, spec.station, argv,
        cwd=spec.workdir, env=popen_env, stdout_path=_stdout_path(ctx),
        stdin_path=stdin_path, stdin_devnull=(stdin_path is None),
        request_id=ctx.request.request_id,
        pass_fds=sandbox.pass_fds_for(argv),
    )
    return {"pid": proc.pid, "station": spec.station}


def _reconcile_spawn_reviewer(ctx: StepContext) -> bool:
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
    """See engine.application.steps.worker_step._spawned_pid: pid must come from the
    proc.started event, not spawn_reviewer's own journaled payload, which
    is empty when that step is resumed via reconcile."""
    for _seq, event in ctx.ledger.events(ctx.project, "proc.started"):
        if (event.attempt_id == ctx.attempt_id
                and event.payload.get("request_id") == ctx.request.request_id):
            return int(event.payload["pid"])
    raise StepFailed("no_spawned_pid")


def _run_await_reviewer(ctx: StepContext) -> dict[str, object]:
    spec, worker, _runner, _coverage_sent = _full_spec(ctx)
    pid = _spawned_pid(ctx)
    code = wait_while_progressing(pid, worker.timeout_sec, (_stdout_path(ctx),))
    if code is None:
        procs.close_station(ctx.ledger, ctx.project, ctx.attempt_id, spec.station)
        raise StepFailed("worker_stalled")
    if code != 0:
        raise StepFailed("worker_failed")
    return {"returncode": code}


def _reconcile_await_reviewer(ctx: StepContext) -> bool:
    """True only once a successful (code 0) exit is positively confirmed
    via the recorded exit file (engine.infrastructure.procs.exit_file_for) -- see
    engine.application.steps.worker_step._reconcile_await_worker's identical reasoning:
    "no live process group left + stdout exists" also holds right
    after a worker_stalled close_station(), before run()'s own
    StepFailed is raised, so a crash in that window must not read back
    as success. False is always safe here: run() re-polls the same
    pid/exit file either way."""
    if not _stdout_path(ctx).exists():
        return False
    pid = _spawned_pid(ctx)
    exit_file = procs.exit_file_for(_stdout_path(ctx))
    return wait_for_exit(pid, 0.0, exit_file) == 0


_VALID_SEVERITIES = frozenset({"P0", "P1", "P2", "P3"})


def _verdict_is_well_formed(verdict: dict[str, object]) -> bool:
    """True iff `verdict` matches the reviewer's own JSON schema
    (engine.infrastructure.workers.prompts.VERDICT_SCHEMA) closely enough to derive a
    verdict from: "passed" a bool, "coverage" one of the CLOSED set
    {"full","partial"} (review_prompt requires the reviewer to echo
    back how much of the diff it actually saw -- see
    _prepare_diff_for_prompt), "findings" a list of dicts each with
    "severity" in the CLOSED, case-sensitive set {P0,P1,P2,P3}, "file"
    a str and "note" a str. Without this check, a malformed verdict
    shape -- `{}`, a dict-valued "findings", a severity like
    "critical"/"p1", or a missing/garbled "coverage" -- would pass
    through untouched and derive passed=True (no P0/P1 found because
    nothing was recognized as a finding at all); this fails closed on
    any of these instead of silently treating the unknown shape as "no
    blocking findings"."""
    if not isinstance(verdict.get("passed"), bool):
        return False
    if verdict.get("coverage") not in ("full", "partial"):
        return False
    findings = verdict.get("findings")
    if not isinstance(findings, list):
        return False
    for finding in findings:
        if not isinstance(finding, dict):
            return False
        if finding.get("severity") not in _VALID_SEVERITIES:
            return False
        if not isinstance(finding.get("file"), str):
            return False
        if not isinstance(finding.get("note"), str):
            return False
    return True


def _run_record_review(ctx: StepContext) -> dict[str, object]:
    spec, _worker, runner, coverage_sent = _full_spec(ctx)
    fp = str(_journal_payload(ctx, "compute_fingerprint")["fp"])
    try:
        now_fp = evidence.fingerprint(_repo(ctx), _excludes(ctx))
    except evidence.FingerprintError as exc:
        raise StepFailed("no_fingerprint") from exc
    if now_fp != fp:
        raise StepFailed("tree_changed_during_review")
    result = runner.parse_result(_stdout_path(ctx), spec)
    verdict = result.verdict
    if verdict is None:
        raise StepFailed("no_verdict")
    if not isinstance(verdict, dict) or not _verdict_is_well_formed(verdict):
        raise StepFailed("review_malformed")
    reviewer_passed = bool(verdict.get("passed"))
    reviewer_coverage = str(verdict.get("coverage"))
    findings = verdict.get("findings", [])
    # The reviewer's own `passed` boolean is not trusted as-is (live
    # observation: a reviewer returned passed=false with only P2
    # findings, which blocked the whole job for no P0/P1 reason) --
    # `passed` here is instead derived from severity: only a P0 or P1
    # finding blocks. `reviewer_passed` keeps the raw boolean for
    # audit either way.
    blocking = any(
        isinstance(f, dict) and f.get("severity") in ("P0", "P1")
        for f in findings
    )
    # coverage must be "full" on BOTH sides, never the reviewer's
    # claim alone: `coverage_sent` is the ENGINE's own, independently
    # recomputed record of what was actually in the prompt (see
    # _prepare_diff_for_prompt) -- a reviewer that (honestly or not)
    # reports "full" when the engine itself only sent a partial diff
    # must still never pass, since "no blocking findings" over content
    # it was never shown proves nothing about the part it never saw.
    # Trusting the reviewer's own report alone (as a prior version of
    # this function did) let exactly that slip through.
    passed = not blocking and coverage_sent == "full" and reviewer_coverage == "full"
    # a review finding: the EVIDENCE fingerprint (what three_match
    # at commit time compares) also covers this project's own
    # file_dep_targets content -- `fp` itself (the plain git tree hash
    # just verified, above, against a LIVE recompute) stays untouched,
    # since it is also what _export_review_snapshot already used to
    # extract this exact tree via git plumbing. A project with no
    # file_dep_targets gets evidence_fp == fp exactly (evidence.
    # with_file_deps' own no-op case), so this is a pure addition.
    evidence_fp = evidence.with_file_deps(fp, _file_dep_targets(ctx))
    evidence.record(
        ctx.ledger, ctx.project, ctx.attempt_id, "review", evidence_fp,
        payload={
            "passed": passed, "reviewer_passed": reviewer_passed,
            "findings": findings, "coverage": coverage_sent,
            "reviewer_coverage": reviewer_coverage,
            "request_id": ctx.request.request_id,
            "tree_fp": fp,
        },
    )
    if not passed:
        # Evidence is recorded above FIRST, same pattern as
        # bench_run.record_bench: a failed review must stay visible to
        # three_match/record even though this request itself ends
        # 'failed', and orbit must stop here -- not continue on to
        # bench/commit on a tree a review never actually passed.
        if coverage_sent != "full" or reviewer_coverage != "full":
            raise StepFailed("review_incomplete")
        raise StepFailed("review_not_passed")
    return {"fp": fp, "passed": passed}


def _reconcile_record_review(ctx: StepContext) -> bool:
    """True iff the already-recorded review evidence for this request_id
    reported passed=True. A crash after _run_record_review's
    evidence.record but before its own `raise StepFailed(...)` must not
    let that recorded failure's existence read back as success -- it
    re-raises the same failure the run would have raised, so the
    request still terminates 'failed' on resume."""
    for row in ctx.ledger.evidence(ctx.attempt_id, "review"):
        if row.payload.get("request_id") == ctx.request.request_id:
            if row.payload.get("passed"):
                return True
            coverage_sent = row.payload.get("coverage")
            reviewer_coverage = row.payload.get("reviewer_coverage")
            if coverage_sent != "full" or reviewer_coverage != "full":
                raise StepFailed("review_incomplete")
            raise StepFailed("review_not_passed")
    return False


def _reconcile_false(ctx: StepContext) -> bool:
    return False


def steps(ctx: StepContext) -> list[StepDef]:
    return [
        StepDef("compute_fingerprint", _run_compute_fingerprint, _reconcile_false),
        StepDef("spawn_reviewer", _run_spawn_reviewer, _reconcile_spawn_reviewer),
        StepDef("await_reviewer", _run_await_reviewer, _reconcile_await_reviewer),
        StepDef("record_review", _run_record_review, _reconcile_record_review),
    ]
