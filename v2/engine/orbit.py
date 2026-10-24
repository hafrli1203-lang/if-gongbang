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

from dataclasses import dataclass, field
from typing import Callable

from engine import evidence
from engine import release as release_mod
from engine.config import EngineConfig
from engine.ledger import Ledger, RequestRow
from engine.processor import Fakes, Processor, submit
from engine.steps.open_job import copy_root
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
    review(reviewer) -> bench -> commit -> report -> close."""
    build_copy = str(copy_root(config, attempt_id, builder_role))
    return [
        Stage("run_station", {"station": "build", "role": builder_role, "task": task}),
        Stage("merge", {"copies": [
            {"role": builder_role, "station": "build", "path": build_copy},
        ]}),
        *stages_after_install(config, attempt_id, builder_role, reviewer_role, job_name),
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


def stages_after_install(config: EngineConfig, attempt_id: str, builder_role: str,
                         reviewer_role: str, job_name: str) -> list[Stage]:
    """review -> bench -> commit -> report -> close. The stage list a job
    resumes on after the owner submits an install a station asked for --
    deliberately starting at "review" (not "bench"), since both review
    and bench evidence are invalidated by an install (engine.steps.install
    .invalidate_evidence) and must be redone, never resumed from a stale
    pre-install verdict."""
    return [
        Stage("review", {"role": reviewer_role}),
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
    """Open a fresh job (roles = builder, reviewer) and run default_stages()
    through to done/failed/needs_owner. `on_progress`, if given, is called
    with each stage's kind right after it finishes (see _run_stages)."""
    attempt_id = _open_job(config, ledger, project, job_name, "full",
                           (builder_role, reviewer_role))
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
    review_payload = _stage_payload(config, project, review_stage)
    review_rid = submit(ledger, project, attempt_id, "review", review_payload, "owner")
    review_status = proc.run_request(project, review_rid, stop_after="spawn_reviewer")
    review_request = ledger.request(review_rid)
    if review_request.status in ("failed", "unknown"):
        classified = _classify(review_request)
        assert classified is not None
        status, reason = classified
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
    # call). Either way, bench runs now.

    bench_payload = _stage_payload(config, project, bench_stage)
    bench_rid = submit(ledger, project, attempt_id, "bench", bench_payload, "owner")
    proc.run_request(project, bench_rid)
    bench_error = _classify(ledger.request(bench_rid))

    if review_status == "running":
        proc.run_request(project, review_rid)
    review_error = _classify(ledger.request(review_rid))
    if review_error is not None:
        status, reason = review_error
        return JobOutcome(attempt_id=attempt_id, status=status, failed_request=review_rid,
                          reason=reason, completed=tuple(completed))
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
    # Set by a run_station stage that asked the owner for something; acted
    # on once the very next stage (merge -- the station's own file changes
    # must land in the repo first) has also completed, not immediately --
    # see the module docstring for why the stop is deferred one stage.
    pending_from_station: list[dict[str, object]] = []
    idx = 0
    while idx < len(stages):
        stage = stages[idx]
        if _should_run_parallel(config, stages, idx):
            outcome = _run_review_then_bench(
                config, ledger, proc, project, attempt_id, stage, stages[idx + 1],
                completed, on_progress,
            )
            if outcome is not None:
                return outcome
            idx += 2
            continue
        payload = _stage_payload(config, project, stage)
        rid = submit(ledger, project, attempt_id, stage.kind, payload, "owner")
        proc.run_once(project)
        request = ledger.request(rid)
        error = _classify(request)
        if error is not None:
            status, reason = error
            return JobOutcome(
                attempt_id=attempt_id, status=status, failed_request=rid,
                reason=reason, completed=tuple(completed),
            )
        completed.append(stage.kind)
        if on_progress is not None:
            on_progress(stage.kind)
        if stage.kind == "commit" and config.projects[project].release is not None:
            # The project's commit just landed -- submit its publish
            # request (artifact_path="@HEAD" at this exact commit) and
            # stop here, BEFORE report/close ever run, so a job with
            # `release` configured never reaches "done" on its own.
            # gb2's ship/skip-ship commands resume with
            # release_followup_stages() once the owner has decided.
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
    "Stage", "JobOutcome", "ProgressCallback", "default_stages",
    "stages_after_install", "release_followup_stages", "run_job", "continue_job",
]
