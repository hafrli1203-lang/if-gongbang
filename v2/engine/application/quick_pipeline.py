"""The small-change fast path (docs/v2.md 「궤도 9칸」, speed variant):
for a request small enough to skip the probe station, run ONE
combined planning call (facts.md + plan.json, written by the SAME
worker turn -- station_prompts.small_prompt) instead of engine.
full_orbit's three separate facts/probe/plan stations, then hand off
to exactly the same task-attempt / acceptance / security / release
machinery a full job uses -- this module never reimplements any of
that: every post-planning step below is a direct call into
engine.application.pipeline's own functions (most of them private, reused on
purpose rather than copied -- see each call site's own comment).

The combined call's own merged-but-uncommitted plan.json+facts.md+
scenes is still reviewed (one plain Stage("review"), same as engine.
full_orbit._run_plan_and_review's own plan review) before it is ever
committed, and engine.domain.plan.record_plan_reviewed stamps the exact
digest that review judged, same as a full job's plan station does.

`gb2 change <app> "<request>" --small` (tools/gb2 cmd_change) is the
only intended caller; `gb2 new`/`gb2 fix`/`gb2 continue` never take
--small. This module's own resume entry point is `resume_small_plan`
(position stage "small_plan", dispatched by engine.application.job_state.resume_job),
never by retrying the combined call on the SAME, possibly half-merged
attempt (see resume_small_plan's own
docstring for why a fresh attempt is required). A release-approval
stop is fully resumable, for free, through the ordinary `gb2 ship`/
`gb2 skip-ship` commands -- those look up the pending publish request
straight from the ledger, and the finisher this module calls
(_maybe_release_or_done) is the literal same function a full job
calls, so the request it submits is indistinguishable from one a full
job would have submitted.

Refuses outright, before ever running a task, if the combined
planning call's own facts.md names any "## 외부 대상" (external API/
CLI/file format) -- a job that depends on a real external target must
go through the full job's real probe station, never skip it just
because small mode asked for speed -- or if plan.json sets "ui": true
-- a UI job needs the full job's designer/approval stage, which this
fast path never runs -- or if plan.json has more than one task --
small mode's whole point is ONE combined call for ONE task; a plan
that itself needed more is not a small change, whatever its own
facts.md/ui said. All three refusals COMMIT the combined call's own
facts.md/plan.json/scenes as this job's trail and CLOSE the attempt
first (see `_close_preserving_evidence`), so the request and its
evidence survive in project history and the project returns to CLOSED
-- the one state tools/gb2's own fallback (engine.application.pipeline.
run_full_job, under a fresh job_name) requires before it can open a
new attempt at all. `maybe_full_fallback` then runs that fallback for
any of the three (FULL_FALLBACK_REASONS), so the request still
continues on the full path rather than just failing outright."""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from engine.domain import faults
from engine.application import pipeline as full_orbit
from engine.application import task_pipeline as orbit
from engine.config import EngineConfig
from engine.domain.stage_checks import ProbeTargetsError, check_facts, check_probe_targets
from engine.domain.plan import (
    PlanError, load_plan, ordered_tasks, record_plan_reviewed, skill_catalog_text,
    validate_plan, validate_tools,
)
from engine.application.job_state import (
    STAGE_SMALL_PLAN, JobPosition, outstanding_requests, record_position,
)
from engine.application.processor import Fakes
from engine.application.stage_prompts import small_prompt

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger

PLANNING_ROLE = full_orbit.PLANNER_ROLE
PLANNING_STATION = "plan"
CURSOR_STAGE = STAGE_SMALL_PLAN

ProgressCallback = Callable[[str], None]


class SmallOrbitError(ValueError):
    """Raised by `resume_small_plan` if handed a position that is not its
    own (stage != CURSOR_STAGE) -- a caller/dispatch bug, never a
    runtime job outcome."""


@dataclass(frozen=True)
class SmallOutcome:
    status: str  # "done" | "needs_owner" | "failed"
    reason: str | None
    attempt_id: str | None
    report_path: str | None
    # Per-station wall-clock milliseconds this call spent, keyed
    # "planning" (the combined facts+plan call), "build"/"review"/
    # "bench" (summed across every task-attempt/fix-round this job
    # ran, read back from engine.application.task_pipeline's own "job.metrics" ledger
    # events rather than timed twice), and "acceptance_and_security"
    # (engine.application.pipeline.run_acceptance_and_handoff's own wall time,
    # minus the build/review/bench share already counted above --
    # acceptance and security cannot be split further without
    # reimplementing that function's own fix-round loop, which this
    # module deliberately never does; see the module docstring).
    durations_ms: dict[str, float]


# Reason returned (never raised) when the combined planning call's own
# facts.md names a real "## 외부 대상" -- the one hand-authored refusal
# this module makes; every other failure path is a plain pass-through
# of whatever engine.application.pipeline/engine.gates/engine.plan already
# decided.
NEEDS_PROBE_REASON = "small_mode_needs_probe"
SINGLE_TASK_REASON = "small_mode_requires_single_task"
REQUIREMENT_REASON = "small_mode_requires_requirement"
# A reviewed, otherwise-valid single-task plan set "ui": true -- small
# mode never runs a designer or a design-approval gate, so this is
# refused the same way NEEDS_PROBE_REASON is: commit+close, then the
# caller (tools/gb2) is expected to fall back to
# engine.application.pipeline.run_full_job for the same request under a fresh
# job_name, which runs the designer and waits for `gb2 approve-design`
# like any other UI job.
UI_PLAN_REASON = "small_mode_requires_full_for_ui"

# The three SmallOutcome.reason values _close_preserving_evidence's own
# callers return -- the only cases a person cannot simply fix and retry
# small mode with (a real external dependency, a UI job, or a plan that
# itself needed more than one task -- all three mean "this request was
# not small enough"); the job must actually continue through the full
# orbit instead. Shared by `maybe_full_fallback` below AND by
# tools/gb2's own initial-call fallback (`_run_small_job_and_report`),
# so a retry/continue landing on the same refusal falls back exactly
# the same way the very first call would have.
FULL_FALLBACK_REASONS = (NEEDS_PROBE_REASON, UI_PLAN_REASON, SINGLE_TASK_REASON)


def fallback_job_name(job_name: str) -> str:
    """The job_name a full-orbit fallback for `job_name` runs under --
    `f"{job_name}-full"`, literally, so both the initial `gb2 change
    --small` call (tools/gb2._run_small_job_and_report) and a later
    `gb2 continue` (resume_small_plan, via maybe_full_fallback) agree
    on which job to look up/open/resume. A single shared function
    rather than each caller hand-writing the suffix again."""
    return f"{job_name}-full"


def maybe_full_fallback(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                        job_name: str, request: str, outcome: SmallOutcome, *,
                        on_progress: ProgressCallback | None = None,
                        processor_factory: orbit.ProcessorFactory | None = None,
                        ) -> full_orbit.FullOutcome | None:
    """None iff `outcome` is not one of the three small-mode refusals
    this module itself hand-authors (FULL_FALLBACK_REASONS) -- the
    caller keeps its own outcome. Otherwise runs the ordinary full job
    (engine.application.pipeline.run_full_job) under `fallback_job_name(job_name)`,
    same request, and returns ITS FullOutcome.

    Called from two places: tools/gb2's own `_run_small_job_and_report`
    (the initial `gb2 change --small` call) and `resume_small_plan`
    below (a later `gb2 continue`, e.g. after an owner fixed something
    unrelated and a planning RETRY's new plan.json/facts.md still names
    a target or sets ui:true). Before this function existed, only the
    first caller ever ran the fallback -- a refusal discovered on a
    resume just sat there forever, reported but never actually
    continued: `resume_small_plan`
    returned the bare refusal as a FullOutcome, and nothing downstream
    of that ever opened the fallback job at all. Idempotent the same
    way engine.application.pipeline.run_full_job already is for any job_name: a
    SECOND call here for a fallback job that already exists/finished
    would hit open_job's own "required state ('CLOSED',)" guard via a
    fresh job_id (a different concern from this function, which only
    ever calls run_full_job once per NEW refusal outcome it is handed)."""
    if outcome.status != "failed" or outcome.reason not in FULL_FALLBACK_REASONS:
        return None
    return full_orbit.run_full_job(
        config, ledger, fakes, project, fallback_job_name(job_name), request,
        on_progress=on_progress, processor_factory=processor_factory)


def _job_metrics_for_attempts(ledger: Ledger, project: str,
                              attempt_ids: set[str]) -> dict[str, float]:
    """Sums engine.application.task_pipeline's own "job.metrics"/"job.metrics.partial"
    durations_ms across every attempt in `attempt_ids` -- each of
    those events already carries one real wall-clock number per stage
    kind ("run_station", "merge", "review", "bench", "commit",
    "report"), recorded by engine.application.task_pipeline._run_stages itself; reading
    them back here is never a second measurement of the same work."""
    totals: dict[str, float] = {}
    for kind in ("job.metrics", "job.metrics.partial"):
        for _seq, event in ledger.events(project, kind):
            if event.attempt_id not in attempt_ids:
                continue
            for k, v in dict(event.payload.get("durations_ms", {})).items():
                totals[k] = totals.get(k, 0.0) + float(v)
    return totals


def _build_review_bench_ms(ledger: Ledger, project: str,
                           attempt_ids: set[str]) -> tuple[dict[str, float], float]:
    """(durations keyed "build"/"review"/"bench", their combined total)
    from `_job_metrics_for_attempts` -- "run_station"+"merge"+"commit"+
    "report" all count as "build" (the task attempt's own build-side
    work), "review"/"bench" stay their own keys."""
    raw = _job_metrics_for_attempts(ledger, project, attempt_ids)
    build_ms = (raw.get("run_station", 0.0) + raw.get("merge", 0.0)
               + raw.get("commit", 0.0) + raw.get("report", 0.0)
               + raw.get("review+bench(parallel)", 0.0))
    review_ms = raw.get("review", 0.0)
    bench_ms = raw.get("bench", 0.0)
    out: dict[str, float] = {}
    if "run_station" in raw or "merge" in raw or "commit" in raw or "report" in raw \
            or "review+bench(parallel)" in raw:
        out["build"] = build_ms
    if "review" in raw:
        out["review"] = review_ms
    if "bench" in raw:
        out["bench"] = bench_ms
    return out, build_ms + review_ms + bench_ms


def _close_preserving_evidence(config: EngineConfig, ledger: Ledger, fakes: Fakes,
                               project: str, job_name: str, attempt_id: str,
                               on_progress: ProgressCallback | None,
                               processor_factory: orbit.ProcessorFactory | None,
                               ) -> full_orbit.FullOutcome | None:
    """Commits whatever the combined planning call's own merge already
    landed (facts.md, and plan.json + tests/scenes/ if the worker got
    that far -- `station`/`role` narrow _commit_job_trail to exactly
    this station's own merged paths, same as every other early-station
    commit in engine.application.pipeline) as this job's ordinary trail, then
    closes the attempt -- the SAME `report -> close` engine.application.pipeline.
    _close_attempt runs for its own facts/planner/designer attempt. A
    no-op commit (nothing was ever merged, e.g. run_station itself
    never finished) still closes cleanly -- the same "no change to
    reset" case `_close_attempt`'s own callers already rely on.

    None on success (the project is now CLOSED again); otherwise the
    "failed" FullOutcome from a close failure, which the caller returns
    as-is -- never silently swallowed."""
    full_orbit.commit_job_trail(
        config, ledger, project, attempt_id, job_name, f"gongbang: {job_name} - small-plan",
        station=PLANNING_STATION, role=PLANNING_ROLE)
    close_outcome = full_orbit.close_attempt(
        config, ledger, fakes, project, attempt_id, job_name, on_progress, processor_factory)
    if close_outcome.status != "done":
        return full_orbit.FullOutcome("failed", f"close:{close_outcome.reason}", attempt_id, None)
    return None


def _run_from_attempt(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                      job_name: str, request: str, attempt_id: str, max_fix_rounds: int,
                      on_progress: ProgressCallback | None,
                      processor_factory: orbit.ProcessorFactory | None, *,
                      needs_resume_note: bool = False) -> SmallOutcome:
    """The combined planning call plus (if it succeeds) the ordinary
    task loop -- the body `run_small_job` and `resume_small_plan` both
    share, differing only in how `attempt_id` came to exist (a brand
    new attempt either way; see each caller's own docstring for why a
    resume never reruns this on the SAME attempt). `needs_resume_note`
    is only ever True for a resume after a fulfilled owner request (see
    resume_small_plan) -- same convention engine.application.pipeline's own
    append_owner_request_resume_note uses for a facts/probe/plan/
    design resume."""
    repo = config.projects[project].repo
    durations: dict[str, float] = {}

    started = time.monotonic()
    prompt = small_prompt(request, job_name, skill_catalog_text(config),
                          full_orbit.build_prefixes(config), config.projects[project].manifest)
    # No planned_skills -- same convention engine.application.pipeline._run_plan_
    # and_review uses for the planner station: the planner has not run
    # yet, so there is no plan to read usable skills back from.
    # The same required-context gate every full-job station runs before
    # dispatching a worker (the planning station and the plan review
    # that follows it): a required skill or company-library lookup the
    # engine could not load stops the job for the owner, and the resume
    # rebuilds and rechecks it.
    plan_gate = full_orbit.station_context_gate(config, ledger, project, attempt_id, "plan",
                                                request, review_query=request)
    if plan_gate.unmet is not None:
        durations["planning"] = (time.monotonic() - started) * 1000
        _record_small(ledger, project, job_name, attempt_id, request, max_fix_rounds,
                      stop_reason=plan_gate.unmet)
        return SmallOutcome("needs_owner", plan_gate.unmet, attempt_id, None, durations)
    task_text = plan_gate.prompt(prompt)
    task_text = full_orbit.append_owner_request_resume_note(
        config, attempt_id, PLANNING_ROLE, task_text, needs_resume_note)
    # This ONE worker turn writes BOTH facts.md (station "facts"'s own
    # record) AND plan.json (+ tests/scenes/, station "plan"'s own
    # record) -- never two separate merge calls. An explicit "owns"
    # naming both is what tells merge.py's own
    # other_station_record_violation (engine.domain.stage_checks) this
    # is a deliberate, engine-declared combined record, not a "plan"
    # copy quietly overwriting "facts"'s record the way a real prober
    # once did by accident (결함 12, -- without it, merge
    # would reject facts.md here as wrote_other_station_record even
    # though writing it is this fast path's whole point.
    combined_owns = [f".gongbang/{job_name}/facts.md", f".gongbang/{job_name}/plan.json",
                      "tests/scenes/"]
    station_outcome = full_orbit.run_and_merge(
        config, ledger, fakes, project, attempt_id, PLANNING_STATION, PLANNING_ROLE,
        task_text, on_progress, processor_factory,
        extra_payload={"owns": combined_owns})

    # One wall-clock bucket for the whole planning phase (the combined
    # worker call plus every gate/validation/review step below, up to
    # the point this job either hands off to the task loop or stops) --
    # set right before every return in this function, so a failure
    # partway through still reports how long planning actually took,
    # never silently 0.
    def _mark_planning_done() -> None:
        durations["planning"] = (time.monotonic() - started) * 1000

    full = full_orbit.station_outcome_to_full(station_outcome, "plan")
    if full is not None:
        _mark_planning_done()
        # Recorded at stage "small_plan", never STAGE_EARLY_STATION:
        # a full job's early-station resume expects the facts/prober/
        # designer copies this PLANNER_ROLE-only attempt never had.
        _record_small(ledger, project, job_name, attempt_id, request, max_fix_rounds,
                      stop_reason=station_outcome.reason,
                      pending_requests=tuple(station_outcome.pending_requests))
        return SmallOutcome(full.status, full.reason, full.attempt_id, None, durations)

    # Every check below reads the merge's own files straight off the
    # working tree (never committed yet) -- same as engine.application.pipeline.
    # _run_plan_and_review, which only commits plan.json once it has
    # been validated (and, there, reviewed). A "failed" branch here
    # never commits or closes this attempt either, matching that same
    # function's own "plan:<problems>" failure branch -- a person (or a
    # later `gb2 fix`) can still inspect the real, uncommitted
    # facts.md/plan.json this worker turn actually wrote. Only the
    # three FULL_FALLBACK_REASONS refusals below commit+close -- those
    # are the cases a person cannot simply edit around; the job must
    # actually continue through the full orbit instead.
    facts_problems = check_facts(repo, job_name)
    if facts_problems:
        _mark_planning_done()
        return SmallOutcome("failed", "facts:" + "; ".join(facts_problems), attempt_id, None,
                           durations)

    try:
        targets = check_probe_targets(repo, job_name)
    except ProbeTargetsError as exc:
        _mark_planning_done()
        return SmallOutcome("failed", f"probe_targets_invalid:{exc}"[:200], attempt_id, None,
                           durations)
    if targets:
        # A real external dependency -- small mode must not skip the
        # real probe station. Commit+close so the project returns to
        # CLOSED and this attempt's own facts.md/plan.json/scenes
        # survive as this job's trail; the caller (tools/gb2) is
        # expected to fall back to engine.application.pipeline.run_full_job for
        # the same request, under its own job_name.
        _mark_planning_done()
        closed = _close_preserving_evidence(
            config, ledger, fakes, project, job_name, attempt_id, on_progress, processor_factory)
        if closed is not None:
            return SmallOutcome(closed.status, closed.reason, closed.attempt_id, None, durations)
        return SmallOutcome("failed", NEEDS_PROBE_REASON, attempt_id, None, durations)

    try:
        plan = load_plan(repo, job_name)
    except PlanError as exc:
        _mark_planning_done()
        return SmallOutcome("failed", f"plan:{exc.reason}", attempt_id, None, durations)

    if plan.ui:
        # A UI job needs the full orbit's designer + design-approval
        # gate -- small mode goes straight from plan review to the
        # build task and never runs either. Checked BEFORE the task-
        # count check below: a ui:true plan must always fall back to
        # the full path regardless of how many tasks it has, never get
        # misreported as SINGLE_TASK_REASON just because it also
        # happens to have more than one task. Same commit+close/
        # fallback story as the external-target refusal above.
        _mark_planning_done()
        closed = _close_preserving_evidence(
            config, ledger, fakes, project, job_name, attempt_id, on_progress, processor_factory)
        if closed is not None:
            return SmallOutcome(closed.status, closed.reason, closed.attempt_id, None, durations)
        return SmallOutcome("failed", UI_PLAN_REASON, attempt_id, None, durations)

    if len(plan.tasks) != 1:
        # More than one task is itself a "this request was not small
        # enough" signal, same as an external target or a UI plan --
        # the full orbit's own task loop (engine.application.pipeline.finish_
        # from_tasks) already runs tasks in dependency order; this fast
        # path never does. Same commit+close/fallback story as the two
        # refusals above, so the request still continues (through the
        # full path) instead of just failing outright.
        _mark_planning_done()
        closed = _close_preserving_evidence(
            config, ledger, fakes, project, job_name, attempt_id, on_progress, processor_factory)
        if closed is not None:
            return SmallOutcome(closed.status, closed.reason, closed.attempt_id, None, durations)
        return SmallOutcome("failed", SINGLE_TASK_REASON, attempt_id, None, durations)
    if not plan.requirements:
        _mark_planning_done()
        return SmallOutcome("failed", REQUIREMENT_REASON, attempt_id, None, durations)

    problems = validate_plan(plan, repo, full_orbit.build_prefixes(config))
    problems += validate_tools(plan, config)
    if problems:
        _mark_planning_done()
        return SmallOutcome("failed", "plan:" + "; ".join(problems), attempt_id, None, durations)

    # Review the merged-but-uncommitted plan.json+facts.md+scenes change
    # BEFORE it is ever committed -- same reasoning engine.application.pipeline.
    # _run_plan_and_review's own review Stage already documents: once
    # committed, this becomes every later reviewer's base_head (nothing
    # to diff against) and would never be judged by anyone.
    plan_review_outcome = orbit.continue_job(
        config, ledger, full_orbit.fakes_for(fakes, attempt_id), project, attempt_id,
        [orbit.Stage("review", full_orbit.review_payload(config, ledger, project, plan, request))],
        processor_factory=processor_factory, on_progress=on_progress,
    )
    if plan_review_outcome.status != "done":
        # Stage "small_plan" for the same reason as the station stop
        # above; no pending requests (a review failure is not an owner
        # request).
        _mark_planning_done()
        _record_small(ledger, project, job_name, attempt_id, request, max_fix_rounds,
                      stop_reason="plan_review_failed")
        return SmallOutcome("needs_owner", "plan_review_failed", attempt_id, None, durations)

    # Validated and reviewed -- commit the combined facts+plan trail
    # now (never before this point, same ordering reasoning as above),
    # record that this exact plan.json+scenes content was the one
    # review actually judged (engine.domain.plan.record_plan_reviewed, read
    # back by a resume to tell a changed plan from an unchanged one),
    # and close this attempt before handing off to the task loop, which
    # opens its own fresh attempt per task (engine.application.pipeline's own
    # fixed base_head reasoning, see that module's docstring).
    _mark_planning_done()
    faults.flow_boundary("small_orbit.plan_reviewed")
    record_plan_reviewed(ledger, project, attempt_id, repo, job_name)
    full_orbit.commit_job_trail(
        config, ledger, project, attempt_id, job_name, f"gongbang: {job_name} - small-plan",
        station=PLANNING_STATION, role=PLANNING_ROLE)
    task_started = time.monotonic()
    # Exactly the same close -> task-attempt / acceptance / requirement-
    # coverage / handoff / security / release machinery a full job uses
    # for the ONE task this plan has (including its STAGE_EARLY_CLOSE
    # position, so a crash in the close resumes the same way) -- see the
    # module docstring for why this is a direct call, never a
    # reimplementation.
    try:
        ordered_tasks(plan)
    except PlanError as exc:
        return SmallOutcome("failed", f"plan:{exc.reason}", attempt_id, None, durations)
    run = full_orbit.Run(config, ledger, fakes, project, job_name, on_progress, processor_factory)
    final = full_orbit.close_early_attempt_then_tasks(run, attempt_id, plan, max_fix_rounds)
    wall_ms = (time.monotonic() - task_started) * 1000

    # Every attempt this job ever opened (the closed planning attempt
    # plus every task/fix attempt _finish_from_tasks opened) -- used to
    # pull engine.application.task_pipeline's own per-stage job.metrics back out below,
    # never to re-run or re-time anything.
    job_attempt_ids = {a.attempt_id for a in ledger.attempts_for_job_name(project, job_name)}
    stage_durations, build_review_bench_ms = _build_review_bench_ms(
        ledger, project, job_attempt_ids)
    durations.update(stage_durations)
    remainder_ms = wall_ms - build_review_bench_ms
    if remainder_ms > 0:
        durations["acceptance_and_security"] = remainder_ms

    return SmallOutcome(final.status, final.reason, final.attempt_id, final.report_path,
                        durations)


def _small_intent(job_name: str, request: str, max_fix_rounds: int) -> JobPosition:
    return JobPosition(job=job_name, stage=CURSOR_STAGE, attempt_id=None,
                       extra={"request": request, "max_fix_rounds": max_fix_rounds,
                              "next_action": "run_station"})


def _record_small(ledger: Ledger, project: str, job_name: str, attempt_id: str, request: str,
                  max_fix_rounds: int, *, stop_reason: str | None = None,
                  pending_requests: tuple[dict, ...] = ()) -> None:
    """This job's position at its combined planning call (stage
    "small_plan"), with the stop it made there, if any."""
    record_position(ledger, project, JobPosition(
        job=job_name, stage=CURSOR_STAGE, attempt_id=attempt_id,
        pending_requests=pending_requests, stop_reason=stop_reason,
        extra={"request": request, "max_fix_rounds": max_fix_rounds,
               "next_action": "run_station"},
    ))


def run_small_job(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                  job_name: str, request: str, *, max_fix_rounds: int = 2,
                  on_progress: ProgressCallback | None = None,
                  processor_factory: orbit.ProcessorFactory | None = None) -> SmallOutcome:
    # The opening intent is recorded first, so a crash from here on
    # resumes through `gb2 continue` via resume_small_plan, carrying the
    # original request.
    attempt_id = full_orbit.open_with_intent(
        config, ledger, project, _small_intent(job_name, request, max_fix_rounds),
        (PLANNING_ROLE,))
    _record_small(ledger, project, job_name, attempt_id, request, max_fix_rounds)
    return _run_from_attempt(config, ledger, fakes, project, job_name, request, attempt_id,
                             max_fix_rounds, on_progress, processor_factory)


def resume_small_plan(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                      job_name: str, pos: JobPosition, *,
                      on_progress: ProgressCallback | None = None,
                      processor_factory: orbit.ProcessorFactory | None = None,
                      ) -> full_orbit.FullOutcome:
    """The resume handler for position stage "small_plan": a small job
    that crashed, hit an owner-request stop in its own combined planning
    call, or had that call's plan review rejected. `pos` is the newest
    position for `job_name` (attempt_id, request, max_fix_rounds, and,
    for a stop rather than a plain crash, stop_reason/pending_requests).

    NEVER reruns the combined call on the SAME attempt: that attempt's
    own merge may already have landed plan.json+facts.md+scenes
    uncommitted (a review-rejected combined call), and a second
    run_station/merge submission on an unchanged prompt would just
    reconcile onto the first one's already-"done" request without ever
    giving the worker a fresh turn (the exact STAGE_PLAN_REVIEW/
    STAGE_PROBE_RETRY retry-note problem engine.application.pipeline's own
    _run_plan_and_review/_run_probe already solve for a full job, by
    rerunning on a FRESH attempt). Closes the stale attempt (via
    engine.application.pipeline.close_attempt -- a trivial no-op reset if
    nothing was ever merged, e.g. the crash hit before run_station even
    finished) and opens a brand-new one carrying ONLY the PLANNING_ROLE
    copy this fast path ever needed -- never the full facts/prober/
    planner/designer role set engine.application.pipeline._reopen_for_retry opens
    for a full job's own early-station resume, which this attempt's
    copies directory was never made with.

    An owner-request stop is gated by engine.application.job_state's one
    outstanding-request computation, like every other resume: a request
    that was never actually fulfilled (e.g. the install the builder
    asked for) returns needs_owner/"owner_request" again, WITHOUT
    reopening anything -- `gb2 continue` is safe to call again and
    again until the owner actually grants it.

    Reopening restores any owner edit made to this attempt's own
    (uncommitted) facts.md/plan.json/scenes while it sat needs_owner --
    engine.application.pipeline.archive_dir_for_attempt/archive_has_content/
    overlay_archive_into_copy, the SAME three helpers
    engine.application.pipeline._reopen_for_retry itself calls for a full job's
    own facts/probe/plan/design retry (never that function directly:
    it hard-codes opening the full FACTS_ROLE/PROBER_ROLE/PLANNER_ROLE/
    DESIGNER_ROLE set this attempt was never made with -- see this
    function's own earlier paragraph on why a fresh attempt here only
    ever carries PLANNING_ROLE). Without this, close's own archive_tmp
    step (run as part of the close below) still preserves the edit in
    cold storage, but nothing ever copied it back into the fresh
    attempt's own copy -- the owner's fix was silently lost the moment
    the stale attempt closed.

    If the fresh planning call's own outcome is one of this module's
    full-fallback refusals (FULL_FALLBACK_REASONS --
    e.g. the owner's retry still names a real external target),
    `maybe_full_fallback` runs the SAME full-orbit fallback the
    initial `gb2 change --small` call would have -- a refusal
    rediscovered on a resume no longer just sits there reported and
    un-actioned."""
    if pos.stage != CURSOR_STAGE:
        raise SmallOrbitError(f"resume_small_plan given a non-{CURSOR_STAGE!r} position: {pos!r}")
    request = str(pos.get("request", ""))
    max_fix_rounds = pos.max_fix_rounds
    stale_attempt_id = pos.attempt_id
    is_owner_request_stop = bool(pos.pending_requests)

    if outstanding_requests(ledger, project, pos, repo=config.projects[project].repo):
        return full_orbit.FullOutcome("needs_owner", "owner_request", stale_attempt_id, None)

    if (stale_attempt_id is not None and ledger.attempt(stale_attempt_id).status == "active"
            and not full_orbit.attempt_has_requests(ledger, project, stale_attempt_id)):
        # An attempt adopted from this job's opening intent: nothing ran
        # on it yet, so it is used as-is (with any recorded restore redone).
        attempt_id = stale_attempt_id
        restore_from = pos.get("restore_from")
        if isinstance(restore_from, str):
            full_orbit.overlay_archive_into_copy(config, attempt_id, PLANNING_ROLE,
                                                  Path(restore_from))
        return _run_small_and_fall_back(
            config, ledger, fakes, project, job_name, request, attempt_id, max_fix_rounds,
            on_progress, processor_factory, is_owner_request_stop)
    if stale_attempt_id is not None:
        full_orbit.abandon_orphaned_requests(ledger, project, stale_attempt_id)
        close_outcome = full_orbit.close_attempt(
            config, ledger, fakes, project, stale_attempt_id, job_name, on_progress,
            processor_factory)
        if close_outcome.status != "done":
            return full_orbit.FullOutcome(
                "failed", f"close:{close_outcome.reason}", stale_attempt_id, None)
        # Read only AFTER close has run -- same ordering reasoning as
        # _reopen_for_retry's own comment: close's own archive_tmp/
        # archive_commit steps are what create the "archive.done" event
        # this looks up; nothing exists to find before that.
        archive_dir = full_orbit.archive_dir_for_attempt(ledger, project, stale_attempt_id)
    else:
        archive_dir = None

    intent = _small_intent(job_name, request, max_fix_rounds)
    restore = archive_dir is not None and full_orbit.archive_has_content(archive_dir)
    if restore:
        intent = replace(intent, extra={**intent.extra, "restore_from": str(archive_dir)})
    attempt_id = full_orbit.open_with_intent(config, ledger, project, intent, (PLANNING_ROLE,))
    if restore and archive_dir is not None:
        full_orbit.overlay_archive_into_copy(config, attempt_id, PLANNING_ROLE, archive_dir)
    return _run_small_and_fall_back(
        config, ledger, fakes, project, job_name, request, attempt_id, max_fix_rounds,
        on_progress, processor_factory, is_owner_request_stop)


def _run_small_and_fall_back(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                             job_name: str, request: str, attempt_id: str,
                             max_fix_rounds: int, on_progress: ProgressCallback | None,
                             processor_factory: orbit.ProcessorFactory | None,
                             is_owner_request_stop: bool) -> full_orbit.FullOutcome:
    _record_small(ledger, project, job_name, attempt_id, request, max_fix_rounds)
    outcome = _run_from_attempt(
        config, ledger, fakes, project, job_name, request, attempt_id, max_fix_rounds,
        on_progress, processor_factory, needs_resume_note=is_owner_request_stop)
    fallback = maybe_full_fallback(
        config, ledger, fakes, project, job_name, request, outcome,
        on_progress=on_progress, processor_factory=processor_factory)
    if fallback is not None:
        return fallback
    return full_orbit.FullOutcome(outcome.status, outcome.reason, outcome.attempt_id,
                                  outcome.report_path)


__all__ = [
    "SmallOutcome", "SmallOrbitError", "run_small_job", "resume_small_plan",
    "maybe_full_fallback", "fallback_job_name",
    "NEEDS_PROBE_REASON", "SINGLE_TASK_REASON", "REQUIREMENT_REASON", "UI_PLAN_REASON",
    "FULL_FALLBACK_REASONS", "CURSOR_STAGE",
]
