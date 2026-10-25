"""The small-change fast path (docs/v2.md 「궤도 9칸」, speed variant):
for a request small enough to skip the probe station, run ONE
combined planning call (facts.md + plan.json, written by the SAME
worker turn -- station_prompts.small_prompt) instead of engine.
full_orbit's three separate facts/probe/plan stations, then hand off
to exactly the same task-attempt / acceptance / security / release
machinery a full job uses -- this module never reimplements any of
that: every post-planning step below is a direct call into
engine.full_orbit's own functions (most of them private, reused on
purpose rather than copied -- see each call site's own comment).

The combined call's own merged-but-uncommitted plan.json+facts.md+
scenes is still reviewed (one plain Stage("review"), same as engine.
full_orbit._run_plan_and_review's own plan review) before it is ever
committed, and engine.plan.record_plan_reviewed stamps the exact
digest that review judged, same as a full job's plan station does.

`gb2 change <app> "<request>" --small` (tools/gb2 cmd_change) is the
only intended caller; `gb2 new`/`gb2 fix`/`gb2 continue` never take
--small. This module's own resume entry point is `resume_small_plan`
(cursor stage "small_plan") -- engine.full_orbit._resume_from_cursor
dispatches to it the same way it dispatches to this module's own
`run_small_job` kind of attempt, never by retrying the combined call
on the SAME, possibly half-merged attempt (see resume_small_plan's own
docstring for why a fresh attempt is required). A release-approval
stop is fully resumable, for free, through the ordinary `gb2 ship`/
`gb2 skip-ship` commands -- those look up the pending publish request
straight from the ledger, never through engine.full_orbit's own
progress-stage bookkeeping, and the finisher this module calls
(_maybe_release_or_done) is the literal same function a full job
calls, so the request it submits is indistinguishable from one a full
job would have submitted.

Refuses outright, before ever running a task, if the combined
planning call's own facts.md names any "## 외부 대상" (external API/
CLI/file format) -- a job that depends on a real external target must
go through the full job's real probe station, never skip it just
because small mode asked for speed -- or if plan.json sets "ui": true
-- a UI job needs the full job's designer/approval stage, which this
fast path never runs. Both refusals COMMIT the combined call's own
facts.md/plan.json/scenes as this job's trail and CLOSE the attempt
first (see `_close_preserving_evidence`), so the request and its
evidence survive in project history and the project returns to CLOSED
-- the one state tools/gb2's own fallback (engine.full_orbit.
run_full_job, under a fresh job_name) requires before it can open a
new attempt at all."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from engine import full_orbit, orbit
from engine.config import EngineConfig
from engine.gates import ProbeTargetsError, check_facts, check_probe_targets
from engine.ledger import Ledger
from engine.plan import (
    PlanError, load_plan, ordered_tasks, record_plan_reviewed, skill_catalog_text,
    validate_plan, validate_tools,
)
from engine.processor import Fakes
from engine.station_prompts import small_prompt
from engine.steps.open_job import open_job

PLANNING_ROLE = full_orbit.PLANNER_ROLE
PLANNING_STATION = "plan"
CURSOR_STAGE = "small_plan"

ProgressCallback = Callable[[str], None]


class SmallOrbitError(ValueError):
    """Raised by `resume_small_plan` if handed a cursor that is not its
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
    # ran, read back from engine.orbit's own "job.metrics" ledger
    # events rather than timed twice), and "acceptance_and_security"
    # (engine.full_orbit._run_acceptance_and_handoff's own wall time,
    # minus the build/review/bench share already counted above --
    # acceptance and security cannot be split further without
    # reimplementing that function's own fix-round loop, which this
    # module deliberately never does; see the module docstring).
    durations_ms: dict[str, float]


# Reason returned (never raised) when the combined planning call's own
# facts.md names a real "## 외부 대상" -- the one hand-authored refusal
# this module makes; every other failure path is a plain pass-through
# of whatever engine.full_orbit/engine.gates/engine.plan already
# decided.
NEEDS_PROBE_REASON = "small_mode_needs_probe"
SINGLE_TASK_REASON = "small_mode_requires_single_task"
REQUIREMENT_REASON = "small_mode_requires_requirement"
# A reviewed, otherwise-valid single-task plan set "ui": true -- small
# mode never runs a designer or a design-approval gate, so this is
# refused the same way NEEDS_PROBE_REASON is: commit+close, then the
# caller (tools/gb2) is expected to fall back to
# engine.full_orbit.run_full_job for the same request under a fresh
# job_name, which runs the designer and waits for `gb2 approve-design`
# like any other UI job.
UI_PLAN_REASON = "small_mode_requires_full_for_ui"

# The two SmallOutcome.reason values _close_preserving_evidence's own
# callers return -- the only two cases a person cannot simply fix and
# retry small mode with; the job must actually continue through the
# full orbit instead. Shared by `maybe_full_fallback` below AND by
# tools/gb2's own initial-call fallback (`_run_small_job_and_report`),
# so a retry/continue landing on the same refusal falls back exactly
# the same way the very first call would have.
FULL_FALLBACK_REASONS = (NEEDS_PROBE_REASON, UI_PLAN_REASON)


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
    """None iff `outcome` is not one of the two small-mode refusals this
    module itself hand-authors (FULL_FALLBACK_REASONS) -- the caller
    keeps its own outcome. Otherwise runs the ordinary full job
    (engine.full_orbit.run_full_job) under `fallback_job_name(job_name)`,
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
    way engine.full_orbit.run_full_job already is for any job_name: a
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
    """Sums engine.orbit's own "job.metrics"/"job.metrics.partial"
    durations_ms across every attempt in `attempt_ids` -- each of
    those events already carries one real wall-clock number per stage
    kind ("run_station", "merge", "review", "bench", "commit",
    "report"), recorded by engine.orbit._run_stages itself; reading
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
    commit in engine.full_orbit) as this job's ordinary trail, then
    closes the attempt -- the SAME `report -> close` engine.full_orbit.
    _close_attempt runs for its own facts/planner/designer attempt. A
    no-op commit (nothing was ever merged, e.g. run_station itself
    never finished) still closes cleanly -- the same "no change to
    reset" case `_close_attempt`'s own callers already rely on.

    None on success (the project is now CLOSED again); otherwise the
    "failed" FullOutcome from a close failure, which the caller returns
    as-is -- never silently swallowed."""
    full_orbit._commit_job_trail(
        config, ledger, project, attempt_id, job_name, f"gongbang: {job_name} - small-plan",
        station=PLANNING_STATION, role=PLANNING_ROLE)
    close_outcome = full_orbit._close_attempt(
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
    resume_small_plan) -- same convention engine.full_orbit's own
    _append_owner_request_resume_note uses for a facts/probe/plan/
    design resume."""
    repo = config.projects[project].repo
    durations: dict[str, float] = {}

    started = time.monotonic()
    prompt = small_prompt(request, job_name, skill_catalog_text(config))
    # No planned_skills -- same convention engine.full_orbit._run_plan_
    # and_review uses for the planner station: the planner has not run
    # yet, so there is no plan to read usable skills back from.
    task_text = full_orbit._prompt_with_context(
        config, ledger, project, attempt_id, "plan", request, prompt)
    task_text = full_orbit._append_owner_request_resume_note(
        config, attempt_id, PLANNING_ROLE, task_text, needs_resume_note)
    station_outcome = full_orbit._run_and_merge(
        config, ledger, fakes, project, attempt_id, PLANNING_STATION, PLANNING_ROLE,
        task_text, on_progress, processor_factory)

    # One wall-clock bucket for the whole planning phase (the combined
    # worker call plus every gate/validation/review step below, up to
    # the point this job either hands off to the task loop or stops) --
    # set right before every return in this function, so a failure
    # partway through still reports how long planning actually took,
    # never silently 0.
    def _mark_planning_done() -> None:
        durations["planning"] = (time.monotonic() - started) * 1000

    full = full_orbit._station_outcome_to_full(station_outcome, "plan")
    if full is not None:
        _mark_planning_done()
        # Never a `full_orbit.progress`/STAGE_EARLY_STATION stop --
        # that shape is read back by `continue_full_job` as a FULL
        # job's own early-station stop, which would then resume
        # against this attempt's PLANNER_ROLE-only copy expecting the
        # facts/prober/designer copies a full job always has (the exact
        # bug this cursor records against -- see record_cursor's own
        # call right below and resume_small_plan's module-level
        # docstring). The cursor IS the only durable record of this
        # stop: carrying the same stop_reason/pending_requests a
        # STAGE_EARLY_STATION progress event would have, so
        # resume_small_plan can run the same owner-request fulfillment
        # gate continue_full_job's own STAGE_EARLY_STATION dispatch
        # runs, before `gb2 continue` ever reaches this module.
        full_orbit.record_cursor(
            ledger, project, job_name, stage=CURSOR_STAGE, next_action="run_station",
            attempt_id=attempt_id, request=request, max_fix_rounds=max_fix_rounds,
            stop_reason=station_outcome.reason, pending_requests=list(station_outcome.pending_requests))
        return SmallOutcome(full.status, full.reason, full.attempt_id, None, durations)

    # Every check below reads the merge's own files straight off the
    # working tree (never committed yet) -- same as engine.full_orbit.
    # _run_plan_and_review, which only commits plan.json once it has
    # been validated (and, there, reviewed). A "failed" branch here
    # never commits or closes this attempt either, matching that same
    # function's own "plan:<problems>" failure branch -- a person (or a
    # later `gb2 fix`) can still inspect the real, uncommitted
    # facts.md/plan.json this worker turn actually wrote. Only the two
    # NEEDS_PROBE_REASON/UI_PLAN_REASON refusals below commit+close --
    # those are the two cases a person cannot simply edit around; the
    # job must actually continue through the full orbit instead.
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
        # expected to fall back to engine.full_orbit.run_full_job for
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

    if len(plan.tasks) != 1:
        _mark_planning_done()
        return SmallOutcome("failed", SINGLE_TASK_REASON, attempt_id, None, durations)
    if not plan.requirements:
        _mark_planning_done()
        return SmallOutcome("failed", REQUIREMENT_REASON, attempt_id, None, durations)

    problems = validate_plan(plan, repo, full_orbit._build_prefixes(config))
    problems += validate_tools(plan, config)
    if problems:
        _mark_planning_done()
        return SmallOutcome("failed", "plan:" + "; ".join(problems), attempt_id, None, durations)

    if plan.ui:
        # A UI job needs the full orbit's designer + design-approval
        # gate -- small mode goes straight from plan review to the
        # build task and never runs either. Same commit+close/fallback
        # story as the external-target refusal above.
        _mark_planning_done()
        closed = _close_preserving_evidence(
            config, ledger, fakes, project, job_name, attempt_id, on_progress, processor_factory)
        if closed is not None:
            return SmallOutcome(closed.status, closed.reason, closed.attempt_id, None, durations)
        return SmallOutcome("failed", UI_PLAN_REASON, attempt_id, None, durations)

    # Review the merged-but-uncommitted plan.json+facts.md+scenes change
    # BEFORE it is ever committed -- same reasoning engine.full_orbit.
    # _run_plan_and_review's own review Stage already documents: once
    # committed, this becomes every later reviewer's base_head (nothing
    # to diff against) and would never be judged by anyone.
    plan_review_outcome = orbit.continue_job(
        config, ledger, full_orbit._fakes_for(fakes, attempt_id), project, attempt_id,
        [orbit.Stage("review", full_orbit._review_payload(config, ledger, project, plan, request))],
        processor_factory=processor_factory, on_progress=on_progress,
    )
    if plan_review_outcome.status != "done":
        # Same cursor-only story as the station-stop branch above --
        # never `full_orbit.progress`/STAGE_PLAN_REVIEW, which
        # continue_full_job's own dispatch would resume via
        # _reopen_for_retry opening the full facts/prober/planner/
        # designer role set this attempt never had. No pending_requests
        # here (a review failure is not an owner request) -- recorded
        # as an explicit empty list, never left unset, so
        # resume_small_plan's own fulfillment check has a fixed shape
        # to read regardless of which branch wrote this cursor.
        _mark_planning_done()
        full_orbit.record_cursor(
            ledger, project, job_name, stage=CURSOR_STAGE, next_action="run_station",
            attempt_id=attempt_id, request=request, max_fix_rounds=max_fix_rounds,
            stop_reason="plan_review_failed", pending_requests=[])
        return SmallOutcome("needs_owner", "plan_review_failed", attempt_id, None, durations)

    # Validated and reviewed -- commit the combined facts+plan trail
    # now (never before this point, same ordering reasoning as above),
    # record that this exact plan.json+scenes content was the one
    # review actually judged (engine.plan.record_plan_reviewed, read
    # back by a resume to tell a changed plan from an unchanged one),
    # and close this attempt before handing off to the task loop, which
    # opens its own fresh attempt per task (engine.full_orbit's own
    # fixed base_head reasoning, see that module's docstring).
    _mark_planning_done()
    record_plan_reviewed(ledger, project, attempt_id, repo, job_name)
    full_orbit._commit_job_trail(
        config, ledger, project, attempt_id, job_name, f"gongbang: {job_name} - small-plan",
        station=PLANNING_STATION, role=PLANNING_ROLE)
    close_outcome = full_orbit._close_attempt(
        config, ledger, fakes, project, attempt_id, job_name, on_progress, processor_factory)
    if close_outcome.status != "done":
        return SmallOutcome("failed", f"close:{close_outcome.reason}", attempt_id, None,
                           durations)

    task_started = time.monotonic()
    # Exactly the same task-attempt / acceptance / requirement-coverage
    # / handoff / security / release machinery a full job uses for the
    # ONE task this plan has -- see the module docstring for why this
    # is a direct call, never a reimplementation.
    try:
        tasks = ordered_tasks(plan)
    except PlanError as exc:
        return SmallOutcome("failed", f"plan:{exc.reason}", attempt_id, None, durations)
    final = full_orbit._finish_from_tasks(
        config, ledger, fakes, project, job_name, plan, tasks, max_fix_rounds,
        on_progress, processor_factory)
    wall_ms = (time.monotonic() - task_started) * 1000

    # Every attempt this job ever opened (the closed planning attempt
    # plus every task/fix attempt _finish_from_tasks opened) -- used to
    # pull engine.orbit's own per-stage job.metrics back out below,
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


def run_small_job(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                  job_name: str, request: str, *, max_fix_rounds: int = 2,
                  on_progress: ProgressCallback | None = None,
                  processor_factory: orbit.ProcessorFactory | None = None) -> SmallOutcome:
    attempt_id = open_job(config, ledger, project, job_name, "full", (PLANNING_ROLE,))
    # A crash before this job's own progress is recorded resumes through
    # `gb2 continue` via resume_small_plan (the cursor's "small_plan"
    # stage) -- see that function's own docstring -- carrying the
    # original request.
    full_orbit.record_cursor(ledger, project, job_name, stage=CURSOR_STAGE,
                             next_action="run_station", attempt_id=attempt_id,
                             request=request, max_fix_rounds=max_fix_rounds)
    return _run_from_attempt(config, ledger, fakes, project, job_name, request, attempt_id,
                             max_fix_rounds, on_progress, processor_factory)


def resume_small_plan(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                      job_name: str, cursor: dict[str, object], *,
                      on_progress: ProgressCallback | None = None,
                      processor_factory: orbit.ProcessorFactory | None = None,
                      ) -> full_orbit.FullOutcome:
    """engine.full_orbit._resume_from_cursor's stage=="small_plan"
    dispatch (via getattr -- see that function's own module docstring)
    -- the resume entry point for a small job that crashed, hit an
    owner-request stop in its own combined planning call, or had that
    call's plan review rejected. `cursor` is the exact payload
    `record_cursor`/`run_small_job` last wrote for `job_name` (stage,
    attempt_id, request, max_fix_rounds, and, for a stop rather than a
    plain crash, stop_reason/pending_requests).

    NEVER reruns the combined call on the SAME attempt: that attempt's
    own merge may already have landed plan.json+facts.md+scenes
    uncommitted (a review-rejected combined call), and a second
    run_station/merge submission on an unchanged prompt would just
    reconcile onto the first one's already-"done" request without ever
    giving the worker a fresh turn (the exact STAGE_PLAN_REVIEW/
    STAGE_PROBE_RETRY retry-note problem engine.full_orbit's own
    _run_plan_and_review/_run_probe already solve for a full job, by
    rerunning on a FRESH attempt). Closes the stale attempt (via
    engine.full_orbit._close_attempt -- a trivial no-op reset if
    nothing was ever merged, e.g. the crash hit before run_station even
    finished) and opens a brand-new one carrying ONLY the PLANNING_ROLE
    copy this fast path ever needed -- never the full facts/prober/
    planner/designer role set engine.full_orbit._reopen_for_retry opens
    for a full job's own early-station resume, which this attempt's
    copies directory was never made with (the documented bug this
    cursor stage exists to close: see engine.full_orbit._resume_from_
    cursor's own comment on a cursor "belonging to a SMALL job").

    An owner-request stop (stop_reason not "plan_review_failed", a
    non-empty pending_requests) is gated exactly like
    continue_full_job's own STAGE_EARLY_STATION dispatch: a request
    that was never actually fulfilled (e.g. the install the builder
    asked for) returns needs_owner/"owner_request" again, WITHOUT
    reopening anything -- `gb2 continue` is safe to call again and
    again until the owner actually grants it.

    Reopening restores any owner edit made to this attempt's own
    (uncommitted) facts.md/plan.json/scenes while it sat needs_owner --
    engine.full_orbit._archive_dir_for_attempt/_archive_has_content/
    _overlay_archive_into_copy, the SAME three helpers
    engine.full_orbit._reopen_for_retry itself calls for a full job's
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
    two full-fallback refusals (NEEDS_PROBE_REASON/UI_PLAN_REASON --
    e.g. the owner's retry still names a real external target),
    `maybe_full_fallback` runs the SAME full-orbit fallback the
    initial `gb2 change --small` call would have -- a refusal
    rediscovered on a resume no longer just sits there reported and
    un-actioned."""
    if cursor.get("stage") != CURSOR_STAGE:
        raise SmallOrbitError(f"resume_small_plan given a non-{CURSOR_STAGE!r} cursor: {cursor!r}")
    request = str(cursor.get("request", ""))
    max_fix_rounds = int(cursor.get("max_fix_rounds", 2))
    stale_attempt_id = str(cursor.get("attempt_id") or "") or None
    stop_reason = cursor.get("stop_reason")
    pending_requests_raw = cursor.get("pending_requests") or []
    pending_requests = pending_requests_raw if isinstance(pending_requests_raw, list) else []
    is_owner_request_stop = bool(pending_requests) and stop_reason != "plan_review_failed"

    if is_owner_request_stop and not full_orbit._pending_requests_fulfilled(
            ledger, project, pending_requests):
        return full_orbit.FullOutcome("needs_owner", "owner_request", stale_attempt_id, None)

    if stale_attempt_id is not None:
        full_orbit._abandon_orphaned_requests(ledger, project, stale_attempt_id)
        close_outcome = full_orbit._close_attempt(
            config, ledger, fakes, project, stale_attempt_id, job_name, on_progress,
            processor_factory)
        if close_outcome.status != "done":
            return full_orbit.FullOutcome(
                "failed", f"close:{close_outcome.reason}", stale_attempt_id, None)
        # Read only AFTER close has run -- same ordering reasoning as
        # _reopen_for_retry's own comment: close's own archive_tmp/
        # archive_commit steps are what create the "archive.done" event
        # this looks up; nothing exists to find before that.
        archive_dir = full_orbit._archive_dir_for_attempt(ledger, project, stale_attempt_id)
    else:
        archive_dir = None

    attempt_id = open_job(config, ledger, project, job_name, "full", (PLANNING_ROLE,))
    if archive_dir is not None and full_orbit._archive_has_content(archive_dir):
        full_orbit._overlay_archive_into_copy(config, attempt_id, PLANNING_ROLE, archive_dir)
    full_orbit.record_cursor(ledger, project, job_name, stage=CURSOR_STAGE,
                             next_action="run_station", attempt_id=attempt_id,
                             request=request, max_fix_rounds=max_fix_rounds)
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
