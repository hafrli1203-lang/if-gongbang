"""Job orbit — runs one job's request kinds in order, from open_job through
close, stopping cleanly when a request fails or a worker station asks the
owner for something (e.g. an install) it cannot do itself.

A Stage is just (kind, payload): the same pair `processor.submit` already
takes. The orbit never imports engine.workers or engine.gate directly — it
only drives the processor through `submit`/`run_once` and reads outcomes
from the ledger (request status, `station.finished` events), so it works
whether `run_station`/`review` are the real worker steps (engine.steps.*,
once Task 3's module lands in engine.steps.KINDS) or a test's own stub
registration.

default_stages() is the normal path for a fresh job: a builder runs the
task, its copy is merged, a reviewer passes judgment, the real test suite
+ trust checks run, then commit/report/close. If the builder's station
asks the owner to do something (its `station.finished` event carries a
non-empty "requests" list -- e.g. an install the worker cannot make
itself because the manifest is protected), the orbit still runs the
*next* stage (merge -- the builder's own file changes, e.g. a new module
that imports a dependency it asked to install, must land in the repo
regardless of whether that import can resolve yet) and only THEN stops
with status "needs_owner" instead of auto-submitting the request: the
owner (a human, or a test standing in for one) decides, submits it, and
then calls continue_job(stages_after_install(...)) to pick up where the
job left off. Any review/bench evidence from before an install is stale
by the time the job resumes -- stages_after_install deliberately starts
over at "review", not after it, so install-stage callers always redo
review+bench rather than trusting evidence that may now be invalidated.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from engine import evidence, gitutil
from engine import release as release_mod
from engine.config import EngineConfig
from engine.ledger import Event, Ledger, RequestRow
from engine.processor import Fakes, Processor, submit
from engine.steps.open_job import copy_git_config_matches_recorded, copy_root, dirty_paths
from engine.steps.open_job import open_job as _open_job

ProcessorFactory = Callable[[EngineConfig, Ledger, Fakes], Processor]


@dataclass(frozen=True)
class Stage:
    kind: str
    payload: dict[str, object]


@dataclass
class JobOutcome:
    attempt_id: str
    status: str  # "done" | "failed" | "needs_owner" | "pending"
    failed_request: str | None
    reason: str | None
    pending_requests: tuple[dict[str, object], ...] = ()
    completed: tuple[str, ...] = field(default_factory=tuple)


def _stage_payload(config: EngineConfig, project: str, stage: Stage) -> dict[str, object]:
    """A bench/review request's identity is the tree it checks: the same
    payload would dedupe a second bench (after a fix) onto the first,
    failed one (request_id = hash of payload). So the payload carries the
    repo fingerprint computed at submit time; bench_run re-derives it and
    refuses if the tree moved in between."""
    if stage.kind not in ("bench", "review"):
        return stage.payload
    project_config = config.projects[project]
    fp = evidence.fingerprint(project_config.repo, project_config.excludes)
    return {**stage.payload, "fp": fp}


def default_stages(config: EngineConfig, attempt_id: str, builder_role: str,
                    reviewer_role: str, task: str, job_name: str) -> list[Stage]:
    """run_station(build, builder, task) -> merge(builder's copy) ->
    review(reviewer, task) -> bench -> commit -> report -> close."""
    build_copy = str(copy_root(config, attempt_id, builder_role))
    return [
        Stage("run_station", {"station": "build", "role": builder_role, "task": task}),
        Stage("merge", {"copies": [
            {"role": builder_role, "station": "build", "path": build_copy},
        ]}),
        *stages_after_install(config, attempt_id, builder_role, reviewer_role,
                              job_name, task),
    ]


def release_followup_stages(job_name: str) -> list[Stage]:
    """report -> close -- the stage list `gb2 ship`/`gb2 skip-ship`
    (tools/gb2) run via continue_job once a release-configured job's
    commit stage already stopped at JobOutcome(status="needs_owner",
    reason="release_approval") (see _run_stages below) and the owner
    has since either approved the publish (engine.release.approve_and_
    run already drove that request to done/failed/unknown by then) or
    explicitly skipped it. Never used for a project with no `release`
    configured -- those jobs reach report/close directly from
    default_stages()/stages_after_install()'s own tail, in the same
    continue_job call that ran commit."""
    return [
        Stage("report", {"summary": f"gongbang: {job_name}"}),
        Stage("close", {}),
    ]


def fix_stages(config: EngineConfig, attempt_id: str, builder_role: str,
              reviewer_role: str, task: str, job_name: str) -> list[Stage]:
    """Same seven-stage sequence as default_stages() (run_station ->
    merge -> review -> bench -> commit -> report -> close) for a
    fix/resume attempt: an attempt that `engine.fixup.fix()` already
    opened itself (NOT this function, and not run_job()/open_job()
    directly) on a job whose previous attempt failed at review or
    bench, after seeding this attempt's builder copy with that previous
    attempt's own merged-but-uncommitted work (archived, never
    discarded, by the close that preceded this new attempt) so
    run_station's edits land on top of it instead of starting from a
    clean slate. The stage shape is identical to default_stages() today
    -- this is a separate function, not a thin default_stages() call
    site, so that fact is documented where fixup.py calls it, and so a
    future divergence (e.g. a fix-specific stage) has a seam to land in
    without reaching into default_stages()'s own contract, which fresh
    jobs still rely on unchanged."""
    return default_stages(config, attempt_id, builder_role, reviewer_role, task, job_name)


def stages_after_install(config: EngineConfig, attempt_id: str, builder_role: str,
                         reviewer_role: str, job_name: str, task: str = "") -> list[Stage]:
    """review(reviewer, task) -> bench -> commit -> report -> close --
    deliberately starting at "review" (not "bench"), since both review
    and bench evidence are invalidated by an install (engine.steps.
    install.invalidate_evidence) and must be redone, never resumed from
    a stale pre-install verdict.

    This is the fixed TAIL every build-task stage list ends with:
    default_stages() appends it straight after its own run_station+
    merge for a fresh task, and engine.full_orbit's own STAGE_TASK_OWNER
    resume appends it too -- but, once the station's own pending
    request (e.g. an install) has actually been fulfilled, ONLY after
    first putting a FRESH run_station (a continuation of the SAME
    builder task, on the same attempt/copy, with a note that the
    install is done and to pick up from where it stopped rather than
    starting over) and its own merge in front of this tail. A builder
    that stopped mid-task to ask for an install was never actually
    finished -- merge having already landed whatever it wrote so far
    (before it asked) does not change that, so jumping straight here
    used to skip the rest of its own work entirely. `task` (the
    original request text, shown to the reviewer -- see engine.steps.
    review) defaults to "" for a caller that resumes an existing
    attempt without re-reading its own run_station request's original
    task from the ledger first."""
    return [
        Stage("review", {"role": reviewer_role, "task": task}),
        Stage("bench", {}),
        Stage("commit", {"message": f"gongbang: {job_name}"}),
        Stage("report", {"summary": f"gongbang: {job_name}"}),
        Stage("close", {}),
    ]


def _default_processor_factory(config: EngineConfig, ledger: Ledger, fakes: Fakes) -> Processor:
    proc = Processor(config, ledger, fakes)
    proc.register_defaults()
    return proc


ProgressCallback = Callable[[str], None]


def run_job(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
           job_name: str, task: str, builder_role: str, reviewer_role: str,
           processor_factory: ProcessorFactory | None = None,
           on_progress: ProgressCallback | None = None) -> JobOutcome:
    """Open a fresh job and run default_stages() through to done/failed/
    needs_owner. `on_progress`, if given, is called with each stage's
    kind right after it finishes (see _run_stages). Only the builder
    role gets a worker copy from open_job -- the reviewer runs against
    its own fresh snapshot (engine.steps.review's own export, spawned
    straight from the repo), never a pre-made copy_root, so making one
    for it here would just be an unused directory."""
    attempt_id = _open_job(config, ledger, project, job_name, "full",
                           (builder_role,))
    stages = default_stages(config, attempt_id, builder_role, reviewer_role,
                            task, job_name)
    return _run_stages(config, ledger, fakes, project, attempt_id, stages,
                       processor_factory, on_progress)


def continue_job(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                 attempt_id: str, stages: list[Stage],
                 processor_factory: ProcessorFactory | None = None,
                 on_progress: ProgressCallback | None = None) -> JobOutcome:
    """Run the given stages on an attempt that is already open (e.g. after
    the owner submitted an install a station asked for). Does not call
    open_job -- the caller is resuming, not starting. Always drive the
    FULL stage list through a single call: calling this once per
    growing stage-list PREFIX instead (one stage, then the first two,
    then the first three, ...) defeats config.parallel_review_bench --
    each such call starts a brand-new Processor from scratch, so a
    review stage finishes to "done" in its own earlier call, long
    before a later call's stage list ever has both review and bench
    present together for _should_run_parallel to interleave them.
    `on_progress`, if given, is called with each stage's kind right
    after it finishes -- use it for incremental progress reporting
    instead of driving the stage list incrementally."""
    return _run_stages(config, ledger, fakes, project, attempt_id, stages,
                       processor_factory, on_progress)


# A station worker that stalled, exited early, or reported a result the
# engine could not parse (StepFailed("worker_reported_failure") -- see
# engine.steps.station.record_station: the worker's own exit was fine,
# but runner.parse_result could not derive a usable verdict from its
# stdout) is not "failed": its copy still holds what it already did, so
# the same station runs again on that copy with the original task plus a
# note listing the files already changed. Only after MAX_STATION_CONTINUES
# such runs in a row does the job stop -- as needs_owner ("worker_stuck"),
# not as a failure.
CONTINUABLE_REASONS = frozenset({
    "worker_stalled", "worker_failed", "worker_timeout", "worker_reported_failure",
})
MAX_STATION_CONTINUES = 3
MAX_REVIEW_STALL_RETRIES = 1


def _continuation_payload(config: EngineConfig, ledger: Ledger, project: str,
                          attempt_id: str, payload: dict[str, object],
                          n: int) -> dict[str, object] | None:
    """None iff the copy's own .git/config no longer matches what
    _make_copy recorded at creation -- same integrity guard
    station._changed_paths applies before its own dirty_paths call. A
    worker that stalls right after planting a clean/smudge filter (or
    other config tampering) in its copy must not get a continuation
    run that trusts a further git call against that copy; the caller
    treats None as "stop the job failed", not as a reason to retry."""
    original = str(payload.get("original_task", payload["task"]))
    role = str(payload["role"])
    root = copy_root(config, attempt_id, role)
    if not copy_git_config_matches_recorded(ledger, project, attempt_id, role, root):
        return None
    try:
        changed = dirty_paths(root, ())
    except Exception:  # noqa: BLE001 - the note is best-effort context
        changed = ()
    listed = "\n".join(f"- {p}" for p in changed) or "- (none yet)"
    note = (
        f"\n\n[이어서 하기 {n}] 앞선 실행이 끝나기 전에 멈췄다. "
        "처음부터 다시 하지 말고, 이미 바뀐 아래 파일을 먼저 읽은 뒤 남은 일을 끝내라.\n"
        f"{listed}"
    )
    return {**payload, "task": original + note, "original_task": original, "continue_n": n}


def _classify(request: RequestRow) -> tuple[str, str | None] | None:
    """None iff `request` is 'done'. Otherwise the (status, reason) a
    JobOutcome should report: ("failed", reason) for 'failed'/'unknown',
    or ("pending", "request_<status>") for 'queued'/'running' (run_once
    or run_request did not actually finish this request -- e.g. an
    unrelated request for the project is 'unknown' and
    Processor._block_on_unknown refuses to touch the queue at all;
    surface it rather than silently retrying forever or treating a
    never-run stage as completed)."""
    if request.status in ("failed", "unknown"):
        reason = request.result.get("reason") if isinstance(request.result, dict) else None
        return "failed", (str(reason) if reason is not None else None)
    if request.status in ("queued", "running"):
        return "pending", f"request_{request.status}"
    return None


def _should_run_parallel(config: EngineConfig, stages: list[Stage], idx: int) -> bool:
    """True iff config.parallel_review_bench and stages[idx] is a
    "review" stage immediately followed by a "bench" stage -- the only
    adjacent pair default_stages()/stages_after_install() ever produce,
    and the only pair _run_review_then_bench knows how to interleave."""
    if not config.parallel_review_bench:
        return False
    if stages[idx].kind != "review":
        return False
    return idx + 1 < len(stages) and stages[idx + 1].kind == "bench"


def _run_review_then_bench(config: EngineConfig, ledger: Ledger, proc: Processor,
                           project: str, attempt_id: str, review_stage: Stage,
                           bench_stage: Stage, completed: list[str],
                           on_progress: ProgressCallback | None) -> JobOutcome | None:
    """Speed default (config.parallel_review_bench): submit review and
    run only its dispatch step (stop_after="spawn_reviewer" -- the step
    that forks codex/claude as a child process), then submit and fully
    run bench while that child process is still running, then resume
    review (await + record) -- see Processor.run_request's docstring
    for the single-processor guarantee this still keeps. Appends
    "review"/"bench" to `completed` (in that order) only for whichever
    of the two actually finished 'done'. Returns None to let the caller
    continue the stage list, or the terminal JobOutcome to stop on."""
    # `review_retry_n` (0 = no retry yet) retries the WHOLE review --
    # spawn through record -- fresh, once, on a worker_stalled stall at
    # EITHER phase (spawn or the later resume), the same one-time
    # retry _run_stages' own serial path already gives a stalled
    # reviewer; see MAX_REVIEW_STALL_RETRIES. It never retries bench --
    # `bench_rid`/`bench_error` stay None until bench actually runs
    # (guarded below), and once it has run (pass or fail) a later
    # review retry never reruns it.
    review_retry_n = 0
    bench_rid: str | None = None
    bench_error: tuple[str, str | None] | None = None
    while True:
        review_payload = _stage_payload(config, project, review_stage)
        if review_retry_n:
            review_payload = {**review_payload, "review_retry_n": review_retry_n}
        review_rid = submit(ledger, project, attempt_id, "review", review_payload, "owner")
        review_status = proc.run_request(project, review_rid, stop_after="spawn_reviewer")
        review_request = ledger.request(review_rid)
        if review_request.status in ("failed", "unknown"):
            classified = _classify(review_request)
            assert classified is not None
            status, reason = classified
            if (status, reason) == ("failed", "worker_stalled") \
                    and review_retry_n < MAX_REVIEW_STALL_RETRIES:
                review_retry_n += 1
                continue
            return JobOutcome(attempt_id=attempt_id, status=status, failed_request=review_rid,
                              reason=reason, completed=tuple(completed))
        if review_request.status == "queued":
            # Never even reached 'running' (_block_on_unknown refused the
            # project outright) -- genuinely stuck, not the intended
            # stop_after pause.
            return JobOutcome(attempt_id=attempt_id, status="pending", failed_request=review_rid,
                              reason="request_queued", completed=tuple(completed))
        # review_request.status is "running" (the intended stop_after pause,
        # after spawn_reviewer) or "done" (a step list with no step named
        # "spawn_reviewer" -- e.g. a test stub -- ran to completion in one
        # call). Either way, bench runs now -- but only on the FIRST pass
        # through this loop; a review retry after bench already ran must
        # never rerun bench too.
        if bench_rid is None:
            bench_payload = _stage_payload(config, project, bench_stage)
            bench_rid = submit(ledger, project, attempt_id, "bench", bench_payload, "owner")
            proc.run_request(project, bench_rid)
            bench_error = _classify(ledger.request(bench_rid))

        if review_status == "running":
            proc.run_request(project, review_rid)
        review_error = _classify(ledger.request(review_rid))
        if review_error is not None:
            status, reason = review_error
            if (status, reason) == ("failed", "worker_stalled") \
                    and review_retry_n < MAX_REVIEW_STALL_RETRIES:
                review_retry_n += 1
                continue
            return JobOutcome(attempt_id=attempt_id, status=status, failed_request=review_rid,
                              reason=reason, completed=tuple(completed))
        break

    completed.append("review")
    if on_progress is not None:
        on_progress("review")
    if bench_error is not None:
        status, reason = bench_error
        return JobOutcome(attempt_id=attempt_id, status=status, failed_request=bench_rid,
                          reason=reason, completed=tuple(completed))
    completed.append("bench")
    if on_progress is not None:
        on_progress("bench")
    return None


def _run_stages(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
               attempt_id: str, stages: list[Stage],
               processor_factory: ProcessorFactory | None,
               on_progress: ProgressCallback | None = None) -> JobOutcome:
    make_processor = processor_factory or _default_processor_factory
    proc = make_processor(config, ledger, fakes)
    completed: list[str] = []
    # Wall-clock duration (ms) spent in each stage kind, accumulated
    # across every time that kind actually ran this call (e.g. a
    # resumed/retried stage after a crash adds to the same bucket) --
    # recorded into a "job.metrics" ledger event once the "report"
    # stage itself completes (see below). Seeded from any
    # "job.metrics.partial" a PRIOR _run_stages call on this same
    # attempt already persisted before stopping at needs_owner (a
    # release approval wait) -- without this, gb2 ship/skip-ship's
    # later continue_job(release_followup_stages(...)) call would
    # start from an empty dict and the eventual job.metrics event
    # would be missing every main-stage timing (run_station, merge,
    # review, bench, commit) from before that stop.
    durations_ms, git_calls_carried = _load_partial_metrics(ledger, project, attempt_id)
    git_calls_at_start = gitutil.call_count()
    # Set by a run_station stage that asked the owner for something; acted
    # on once the very next stage (merge -- the station's own file changes
    # must land in the repo first) has also completed, not immediately --
    # see the module docstring for why the stop is deferred one stage.
    pending_from_station: list[dict[str, object]] = []
    idx = 0
    while idx < len(stages):
        stage = stages[idx]
        if _should_run_parallel(config, stages, idx):
            started = time.monotonic()
            outcome = _run_review_then_bench(
                config, ledger, proc, project, attempt_id, stage, stages[idx + 1],
                completed, on_progress,
            )
            durations_ms["review+bench(parallel)"] = (
                durations_ms.get("review+bench(parallel)", 0.0)
                + (time.monotonic() - started) * 1000
            )
            if outcome is not None:
                failed_stage = (
                    ledger.request(outcome.failed_request).kind
                    if outcome.failed_request is not None else None
                )
                _record_job_metrics_failure(
                    ledger, project, attempt_id, durations_ms,
                    git_calls_carried + (gitutil.call_count() - git_calls_at_start),
                    outcome.status, failed_stage,
                )
                return outcome
            idx += 2
            continue
        started = time.monotonic()
        payload = _stage_payload(config, project, stage)
        rid = submit(ledger, project, attempt_id, stage.kind, payload, "owner")
        proc.run_once(project)
        durations_ms[stage.kind] = (
            durations_ms.get(stage.kind, 0.0) + (time.monotonic() - started) * 1000
        )
        request = ledger.request(rid)
        error = _classify(request)
        if (error is not None and stage.kind == "run_station"
                and error[1] in CONTINUABLE_REASONS):
            n = int(stage.payload.get("continue_n", 0)) + 1
            if n <= MAX_STATION_CONTINUES:
                continuation = _continuation_payload(
                    config, ledger, project, attempt_id, stage.payload, n)
                if continuation is None:
                    _record_job_metrics_failure(
                        ledger, project, attempt_id, durations_ms,
                        git_calls_carried + (gitutil.call_count() - git_calls_at_start),
                        "failed", stage.kind,
                    )
                    return JobOutcome(
                        attempt_id=attempt_id, status="failed", failed_request=rid,
                        reason="copy_git_config_changed", completed=tuple(completed),
                    )
                stages[idx] = Stage("run_station", continuation)
                continue
            _record_job_metrics_failure(
                ledger, project, attempt_id, durations_ms,
                git_calls_carried + (gitutil.call_count() - git_calls_at_start),
                "needs_owner", stage.kind,
            )
            return JobOutcome(
                attempt_id=attempt_id, status="needs_owner", failed_request=rid,
                reason="worker_stuck", completed=tuple(completed),
            )
        if (error is not None and stage.kind == "review" and error[1] == "worker_stalled"):
            # A stalled reviewer is not "failed" either -- its own snapshot/
            # fingerprint are per-request (a fresh review-snapshot dir, see
            # engine.steps.review), so unlike a stalled builder there is no
            # partial progress worth describing in a continuation note; this
            # just retries the SAME review, fresh, once, before giving up.
            # `review_retry_n` busts submit()'s own payload-hash idempotency
            # (the same payload would otherwise dedupe onto the first,
            # already-"failed" request and never actually run again).
            n = int(stage.payload.get("review_retry_n", 0)) + 1
            if n <= MAX_REVIEW_STALL_RETRIES:
                stages[idx] = Stage("review", {**stage.payload, "review_retry_n": n})
                continue
        if error is not None:
            status, reason = error
            _record_job_metrics_failure(
                ledger, project, attempt_id, durations_ms,
                git_calls_carried + (gitutil.call_count() - git_calls_at_start),
                status, stage.kind,
            )
            return JobOutcome(
                attempt_id=attempt_id, status=status, failed_request=rid,
                reason=reason, completed=tuple(completed),
            )
        completed.append(stage.kind)
        if on_progress is not None:
            on_progress(stage.kind)
        if stage.kind == "report":
            _record_job_metrics(ledger, project, attempt_id, durations_ms,
                                git_calls_carried + (gitutil.call_count() - git_calls_at_start))
        if stage.kind == "commit" and config.projects[project].release is not None:
            # The project's commit just landed -- submit its publish
            # request (artifact_path="@HEAD" at this exact commit) and
            # stop here, BEFORE report/close ever run, so a job with
            # `release` configured never reaches "done" on its own.
            # gb2's ship/skip-ship commands resume with
            # release_followup_stages() once the owner has decided --
            # persist what this call accumulated first (see
            # _load_partial_metrics above), or it would be lost.
            _record_job_metrics_partial(
                ledger, project, attempt_id, durations_ms,
                git_calls_carried + (gitutil.call_count() - git_calls_at_start),
            )
            request_id, summary = release_mod.prepare(config, ledger, project, attempt_id)
            pending = {"kind": "publish", "request_id": request_id, **summary}
            return JobOutcome(
                attempt_id=attempt_id, status="needs_owner", failed_request=None,
                reason="release_approval", pending_requests=(pending,),
                completed=tuple(completed),
            )
        if stage.kind == "run_station":
            pending_from_station = _station_pending_requests(ledger, project, rid)
            idx += 1
            continue
        if pending_from_station:
            _record_job_metrics_failure(
                ledger, project, attempt_id, durations_ms,
                git_calls_carried + (gitutil.call_count() - git_calls_at_start),
                "needs_owner", "run_station",
            )
            return JobOutcome(
                attempt_id=attempt_id, status="needs_owner", failed_request=None,
                reason=None, pending_requests=tuple(pending_from_station),
                completed=tuple(completed),
            )
        idx += 1
    return JobOutcome(
        attempt_id=attempt_id, status="done", failed_request=None, reason=None,
        completed=tuple(completed),
    )


def _bench_run_counts(ledger: Ledger, attempt_id: str) -> dict[str, int]:
    """How many non-stale bench evidence rows this attempt has, by
    their own recorded "scope" ("related", "full", "full(fallback)")
    -- e.g. {"related": 2, "full": 1}."""
    counts: dict[str, int] = {}
    for row in ledger.evidence(attempt_id, "bench"):
        if row.stale:
            continue
        scope = str(row.payload.get("scope", ""))
        if scope:
            counts[scope] = counts.get(scope, 0) + 1
    return counts


def _load_partial_metrics(ledger: Ledger, project: str,
                          attempt_id: str) -> tuple[dict[str, float], int]:
    """(durations_ms, git_calls) from the newest "job.metrics.partial"
    event this attempt has, or ({}, 0) if it never stopped at
    needs_owner before (the normal, non-release case) -- see
    _run_stages' own seeding comment."""
    durations: dict[str, float] = {}
    git_calls = 0
    for _seq, event in ledger.events(project, "job.metrics.partial"):
        if event.attempt_id == attempt_id:
            durations = {str(k): float(v)
                         for k, v in dict(event.payload.get("durations_ms", {})).items()}
            git_calls = int(event.payload.get("git_calls", 0))
    return durations, git_calls


def _record_job_metrics_partial(ledger: Ledger, project: str, attempt_id: str,
                                durations_ms: dict[str, float], git_calls: int) -> None:
    """Persist what this _run_stages call accumulated so far, right
    before returning a "needs_owner" release stop -- read back by
    _load_partial_metrics the next time this attempt_id resumes
    (gb2 ship/skip-ship's continue_job(release_followup_stages(...)))."""
    ledger.append(Event(
        project=project, kind="job.metrics.partial", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={
            "durations_ms": {k: round(v, 1) for k, v in durations_ms.items()},
            "git_calls": git_calls,
        },
    ))


def _record_job_metrics(ledger: Ledger, project: str, attempt_id: str,
                        durations_ms: dict[str, float], git_calls: int = 0) -> None:
    """One "job.metrics" event per report, carrying this call's own
    per-stage wall-clock durations (ms, see _run_stages) and how many
    bench runs of each scope this attempt recorded (from the ledger's
    own evidence rows, an observable fact -- not a counter this
    function keeps itself). `gb2 log` reads this back for display."""
    ledger.append(Event(
        project=project, kind="job.metrics", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={
            "durations_ms": {k: round(v, 1) for k, v in durations_ms.items()},
            "test_runs": _bench_run_counts(ledger, attempt_id),
            "git_calls": git_calls,
        },
    ))


def _record_job_metrics_failure(ledger: Ledger, project: str, attempt_id: str,
                                durations_ms: dict[str, float], git_calls: int,
                                outcome: str, failed_stage: str | None) -> None:
    """One "job.metrics" event recorded right before _run_stages returns
    early (a stage failed/stuck pending, or a station asked the owner for
    something) -- same payload shape _record_job_metrics writes at a
    normal "report" completion, plus `outcome` ("failed" | "needs_owner"
    | "pending", mirroring JobOutcome.status for this return) and
    `failed_stage` (the stage kind that caused the stop). Without this, a
    job that never reaches "report" -- the common case for a failure --
    left no job.metrics event at all, silently losing every stage timing
    it DID complete (run_station, merge, review, bench, ...) from
    whatever reads this back (`gb2 log`). Never called for the
    release-approval stop at "commit" (that early return keeps using
    _record_job_metrics_partial instead, unchanged -- it is a resumable
    pause, not a failure, and continue_job(release_followup_stages(...))
    still needs _load_partial_metrics to pick up from it)."""
    ledger.append(Event(
        project=project, kind="job.metrics", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={
            "durations_ms": {k: round(v, 1) for k, v in durations_ms.items()},
            "test_runs": _bench_run_counts(ledger, attempt_id),
            "git_calls": git_calls,
            "outcome": outcome,
            "failed_stage": failed_stage,
        },
    ))


def _station_pending_requests(ledger: Ledger, project: str,
                              request_id: str) -> list[dict[str, object]]:
    """The "requests" a station.finished event (matching request_id) asked
    the owner to submit on its behalf -- empty if the station had nothing
    to ask for, or if no such event exists yet (a kind registered without
    ever appending one, e.g. a test's incomplete stub)."""
    for _seq, event in ledger.events(project, "station.finished"):
        if event.payload.get("request_id") == request_id:
            requests = event.payload.get("requests", [])
            return list(requests) if isinstance(requests, list) else []
    return []


__all__ = [
    "Stage", "JobOutcome", "ProgressCallback", "default_stages", "fix_stages",
    "stages_after_install", "release_followup_stages", "run_job", "continue_job",
]
