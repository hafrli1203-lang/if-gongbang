"""review — launch a reviewer (claude/codex/scripted) read-only against
the project repo's current diff against this attempt's base_head, and
record pass/fail evidence bound to a fingerprint.

Request payload contract: {"role": str}.

compute_fingerprint  fp = evidence.fingerprint(repo); journal {fp}.
                     reconcile False -- a crash mid-run always
                     re-computes rather than trusting a half-written row.
spawn_reviewer       workdir = repo itself (the reviewer is read-only --
                     no worker copy exists for it); prompt =
                     prompts.review_prompt(spec, diff_text), diff_text =
                     `git diff {base_head}..HEAD` (committed changes
                     since the job opened) + `git diff HEAD` (the
                     working tree -- e.g. a merge's uncommitted result)
                     + a pseudo-diff for every untracked (brand-new)
                     file (`git diff` omits those entirely -- a merge
                     that adds a whole new file would otherwise be
                     invisible to the reviewer), truncated to 60k chars.
                     Same spawn/reconcile shape as station.spawn_worker.
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
                     "reviewer_passed") but derived from `findings`:
                     True iff no finding has severity P0 or P1 (a
                     reviewer that returns passed=false over P2/P3
                     findings alone must not block the job).
                     evidence.record(kind="review", fp, payload=
                     {"passed", "reviewer_passed", "findings",
                     "request_id"}) FIRST, then, if not passed, raise
                     StepFailed("review_not_passed") -- same pattern as
                     bench_run.record_bench: the evidence stays visible
                     even though the request ends 'failed', and the
                     orbit stops here rather than continuing on to
                     bench/commit against a tree review never passed.
"""
from __future__ import annotations

import dataclasses
import shutil
import subprocess
from pathlib import Path

from engine import evidence, ids, procs, sandbox
from engine.gitutil import git_argv
from engine.ledger import Event
from engine.processor import StepContext, StepDef, StepFailed
from engine.workers.base import WorkerSpec, wait_for_exit
from engine.workers.prompts import review_prompt

_DIFF_MAX_CHARS = 60_000


def _project_config(ctx: StepContext):
    return ctx.config.projects[ctx.project]


def _repo(ctx: StepContext) -> Path:
    return _project_config(ctx).repo


def _excludes(ctx: StepContext) -> tuple[str, ...]:
    return _project_config(ctx).excludes


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
    """Extracts the tree object `fp` -- the EXACT tree
    evidence.fingerprint/range_tree already wrote into `repo`'s own
    git object database, never a fresh read of the live working tree
    -- into a fresh directory. `git archive` accepts any tree-ish, so
    this reads directly from the object store (already-fixed content,
    immune to anything written to the working tree after fp was
    computed) rather than the files on disk again."""
    dest = _review_snapshot_dir(ctx)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    archive = subprocess.run(
        git_argv(repo, "archive", fp), check=True, capture_output=True,
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)
    return dest


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
    diff = result.stdout if result.returncode == 0 else ""
    return diff[:_DIFF_MAX_CHARS]


def _full_spec(ctx: StepContext) -> tuple[WorkerSpec, object, object]:
    role = str(ctx.request.payload["role"])
    worker = ctx.config.workers.get(role)
    if worker is None:
        raise StepFailed("no_worker_config")
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
    )
    full_task = review_prompt(base_spec, _diff_text(ctx, fp))
    spec = dataclasses.replace(base_spec, task=full_task)
    return spec, worker, runner


def _other_project_repos(ctx: StepContext) -> tuple[Path, ...]:
    return tuple(p.repo for name, p in ctx.config.projects.items()
                 if name != ctx.project)


def _ledger_hidden_root(ctx: StepContext) -> tuple[Path, ...]:
    """The WHOLE directory holding the ledger file -- see
    engine.steps.station's identical helper for why this is a
    directory hide (tmpfs, then remounted read-only) rather than
    masking the ledger file and its SQLite WAL/SHM/rollback-journal
    companions by name, which leaves any other file in that directory
    unmasked. The reviewer (codex, read-only) runs no gate hook of its
    own and never had any business touching the ledger; it is hidden
    here outright the same way station.py's worker sandbox now is."""
    return (ctx.config.ledger_path.parent,)


def _sandboxed_argv(ctx: StepContext, spec: WorkerSpec, argv: list[str],
                     env: dict[str, str], runner: object) -> list[str]:
    """Reviewer sandbox policy: writable = the stations dir (its output
    files) and -- only if the real HOME exists -- the runner's own
    `home_writable_subpaths` under it (same reasoning as station.py's
    worker: a real CLI's credential/session state, never the whole
    HOME). The ledger directory is NOT writable (no gate hook runs for
    the reviewer, so nothing here ever needed to append to it).
    readonly = the real HOME itself (the CLI still needs to READ its
    config/credentials there) AND the per-request review SNAPSHOT
    directory (spec.workdir -- the extracted, already-fingerprinted
    tree, never the live project repo: see _export_review_snapshot).
    hidden = engine home (minus the stations dir and the snapshot dir,
    both re-added above/below), secrets_dir, THIS project's own repo
    (the reviewer's own tool calls must never reach the live, mutable
    repo at all -- a concurrently running bench writing to it must not
    be observable through any absolute-path reference, only through
    the frozen snapshot), every OTHER project's repo, the project's
    own `hidden_paths`, PLUS the WHOLE directory holding the ledger
    file (see _ledger_hidden_root). net=True (codex/claude need it)."""
    home_env = env.get("HOME")
    writable = (_stations_dir(ctx),)
    readonly: tuple[Path, ...] = (spec.workdir,)
    if home_env:
        home_path = Path(home_env)
        readonly += (home_path,)
        subpaths = getattr(runner, "home_writable_subpaths", ())
        writable += tuple(home_path / sub for sub in subpaths)
    hidden = ((ctx.config.home, ctx.config.secrets_dir, _repo(ctx))
              + _other_project_repos(ctx) + _ledger_hidden_root(ctx)
              + ctx.config.projects[ctx.project].hidden_paths)
    try:
        return sandbox.wrap(
            argv, cwd=spec.workdir, writable=writable, hidden=hidden,
            readonly=readonly, net=True, mode=ctx.config.sandbox,
            child_env=env,
        )
    except sandbox.SandboxUnavailable as exc:
        raise StepFailed("sandbox_unavailable") from exc


def _run_spawn_reviewer(ctx: StepContext) -> dict[str, object]:
    fp = str(_journal_payload(ctx, "compute_fingerprint")["fp"])
    _export_review_snapshot(ctx, _repo(ctx), fp)
    spec, worker, runner = _full_spec(ctx)
    argv = runner.argv(spec, worker)
    env = runner.env(spec, worker)
    argv = _sandboxed_argv(ctx, spec, argv, env, runner)
    popen_env = sandbox.exec_env(ctx.config.sandbox, env)
    proc = procs.spawn(
        ctx.ledger, ctx.project, ctx.attempt_id, spec.station, argv,
        cwd=spec.workdir, env=popen_env, stdout_path=_stdout_path(ctx),
        stdin_devnull=True, request_id=ctx.request.request_id,
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
    """See engine.steps.station._spawned_pid: pid must come from the
    proc.started event, not spawn_reviewer's own journaled payload, which
    is empty when that step is resumed via reconcile."""
    for _seq, event in ctx.ledger.events(ctx.project, "proc.started"):
        if (event.attempt_id == ctx.attempt_id
                and event.payload.get("request_id") == ctx.request.request_id):
            return int(event.payload["pid"])
    raise StepFailed("no_spawned_pid")


def _run_await_reviewer(ctx: StepContext) -> dict[str, object]:
    spec, worker, _runner = _full_spec(ctx)
    pid = _spawned_pid(ctx)
    code = wait_for_exit(pid, worker.timeout_sec)
    if code is None:
        procs.close_station(ctx.ledger, ctx.project, ctx.attempt_id, spec.station)
        raise StepFailed("worker_timeout")
    if code != 0:
        raise StepFailed("worker_failed")
    return {"returncode": code}


def _reconcile_await_reviewer(ctx: StepContext) -> bool:
    spec, _worker, _runner = _full_spec(ctx)
    live = ctx.ledger.live_procs(ctx.attempt_id, spec.station)
    return not live and _stdout_path(ctx).exists()


_VALID_SEVERITIES = frozenset({"P0", "P1", "P2", "P3"})


def _verdict_is_well_formed(verdict: dict[str, object]) -> bool:
    """True iff `verdict` matches the reviewer's own JSON schema
    (engine.workers.prompts.VERDICT_SCHEMA) closely enough to derive a
    verdict from: "passed" a bool, "findings" a list of dicts each
    with "severity" in the CLOSED, case-sensitive set {P0,P1,P2,P3},
    "file" a str and "note" a str. Without this check, a malformed
    verdict shape -- `{}`, a dict-valued "findings", or a severity
    like "critical"/"p1" -- would pass through untouched and derive
    passed=True (no P0/P1 found because nothing was recognized as a
    finding at all); this fails closed on any of these instead of
    silently treating the unknown shape as "no blocking findings"."""
    if not isinstance(verdict.get("passed"), bool):
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
    spec, _worker, runner = _full_spec(ctx)
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
    passed = not blocking
    evidence.record(
        ctx.ledger, ctx.project, ctx.attempt_id, "review", fp,
        payload={
            "passed": passed, "reviewer_passed": reviewer_passed,
            "findings": findings, "request_id": ctx.request.request_id,
        },
    )
    if not passed:
        # Evidence is recorded above FIRST, same pattern as
        # bench_run.record_bench: a failed review must stay visible to
        # three_match/record even though this request itself ends
        # 'failed', and orbit must stop here -- not continue on to
        # bench/commit on a tree a review never actually passed.
        raise StepFailed("review_not_passed")
    return {"fp": fp, "passed": passed}


def _reconcile_record_review(ctx: StepContext) -> bool:
    for row in ctx.ledger.evidence(ctx.attempt_id, "review"):
        if row.payload.get("request_id") == ctx.request.request_id:
            return True
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
