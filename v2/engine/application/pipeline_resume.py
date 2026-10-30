"""Crash/stop recovery for the full orbit: `continue_full_job` (tools/
gb2's `continue`/`approve-design`/`approve-install`) reads the newest
`job.position` and dispatches through RESUME_TABLE below to whichever
of these handlers matches that stage -- one per place engine.full_orbit_
early/_tasks/_release can stop a job (an early station's run_station/
content gate, a design approval, a build task, the acceptance/handoff/
security/release tail).

Depends on engine.application.pipeline_base/_runner/_release/_early/_tasks/
_resume_tasks; nothing in those five calls back into this module -- this
is the only one of the six that depends on all the others. The build-
task resume chain itself (resume_task/resume_task_owner and everything
only they call) lives in engine.application.pipeline_resume_tasks, split
out purely to keep this file under the repo's own line budget -- see
that module's own docstring."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable

from engine.application import task_pipeline as orbit
from engine.application.pipeline_base import (
    FullOrbitError, FullOutcome, Run, max_fix_rounds,
    resumed_plan, task_attempt_committed, task_for, unblock_internal_crash,
)
from engine.application.pipeline_planning import (
    close_early_attempt_then_tasks, facts_then_rest, maybe_rerun_plan_review,
    plan_check_failure_note, plan_review_findings_note, probe_plan_design_close,
)
from engine.application.pipeline_release import finalize_release, prepare_release
# Split out purely to keep this module under the repo's own line budget
# (see pipeline_resume_retry's own docstring) -- re-exported in __all__
# below under their historical names.
from engine.application.pipeline_resume_retry import (
    ARCHIVE_MARKER_FILES, RETRY_STAGE_ROLE, abandon_orphaned_requests, archive_dir_for_attempt,
    archive_has_content, continue_full_job, continue_full_job_from, overlay_archive_into_copy,
    reopen_for_retry, resumed_plan_problems, stop_for_resumed_plan_problems,
)
from engine.application.pipeline_resume_tasks import (
    advance_past_task, continue_task_stages, rerun_task_on_new_attempt, rerun_task_owner_stop,
    resume_task, resume_task_owner,
)
from engine.application.pipeline_stages import record_event, station_outcome_to_full
from engine.application.repair import FIX_ROUNDS_EXHAUSTED_PREFIX, NO_PROGRESS_PREFIX
from engine.application.repair_target import (
    job_cost_usd, previous_review_findings, review_findings_for_attempt, review_findings_repeat,
)
from engine.application.pipeline_tasks import finish_after_handoff, run_acceptance_and_handoff
from engine.domain.stage_checks import ProbeTargetsError, check_design_approved, check_probe_targets
from engine.domain.plan import PlanError, load_plan
from engine.application.job_state import (
    STAGE_DESIGN, STAGE_DESIGN_APPROVAL, STAGE_EARLY_CLOSE, STAGE_EARLY_STATION, STAGE_FACTS,
    STAGE_PLAN, STAGE_PLAN_REVIEW, STAGE_POST_TASKS, STAGE_PROBE, STAGE_PROBE_RETRY,
    STAGE_RELEASE_APPROVAL, STAGE_REQUIREMENTS_REVIEW, STAGE_SECURITY_FIX, STAGE_SMALL_PLAN,
    STAGE_TASK, STAGE_TASK_OWNER, JobPosition, early_station_rounds_used, outstanding_requests,
)

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger



# ---------------------------------------------------------------------
# resume handlers -- one per position stage, dispatched by
# engine.application.job_state.resume_job through RESUME_TABLE
# (max_fix_rounds/task_for/resumed_plan/unblock_internal_crash:
# engine.application.pipeline_base; the build-task chain itself --
# resume_task/resume_task_owner and everything only they call --
# engine.application.pipeline_resume_tasks; both imported above)
# ---------------------------------------------------------------------

BEFORE_PLAN_STAGES = frozenset({
    STAGE_FACTS, STAGE_PROBE, STAGE_PLAN, STAGE_PROBE_RETRY, STAGE_PLAN_REVIEW, STAGE_SMALL_PLAN,
})



def resumed_plan_stop(run: Run, pos: JobPosition) -> FullOutcome | None:
    """The check every resume runs before dispatching, from the point the
    job has a committed plan on: the plan must still be the reviewed one,
    with no requirement or task dropped (stop_for_resumed_plan_problems).
    Stages before the plan exists have nothing to check; a design-approval
    resume re-reviews an edited plan first and checks after that
    (resume_design_approval)."""
    if pos.stage in BEFORE_PLAN_STAGES or pos.stage == STAGE_DESIGN_APPROVAL:
        return None
    if pos.stage == STAGE_EARLY_STATION and pos.get("station") != "design":
        return None
    return stop_for_resumed_plan_problems(run.ledger, run.project, run.job,
                                           pos.attempt_id or "", run.repo)



STATION_STAGE = {
    "facts": STAGE_FACTS, "probe": STAGE_PROBE, "plan": STAGE_PLAN, "design": STAGE_DESIGN,
}



REOPEN_STATION = {
    STAGE_FACTS: "facts", STAGE_PROBE: "probe", STAGE_PLAN: "plan", STAGE_DESIGN: "design",
    STAGE_PROBE_RETRY: "probe", STAGE_PLAN_REVIEW: "plan",
}


def _facts_still_invalid(run: Run, pos: JobPosition) -> bool:
    """True iff facts.md, as it stands in the repo RIGHT NOW, still
    raises ProbeTargetsError -- i.e. nobody (owner or a previous facts
    worker) has fixed the malformed "## 외부 대상" line since this
    STAGE_PROBE_RETRY stop was recorded. False (facts.md is fine now,
    or missing -- the facts gate, not this one, handles that) means
    the ordinary "probe" reopen already handles an owner's repair."""
    try:
        check_probe_targets(run.repo, run.job)
    except ProbeTargetsError:
        return True
    return False


def _prior_plan_notes(ledger: "Ledger", project: str, attempt_id: str) -> str:
    """Both kinds of "carry the last failure forward" note a reopened
    "plan" station's prompt gets prepended with: plan_review_findings_
    note (a reviewer rejected an otherwise-parseable plan.json) and
    plan_check_failure_note (결함 30 -- load_plan/validate_plan itself
    rejected it, before any reviewer ever ran). Only one is ever
    non-empty for a given attempt -- a plan.json that failed to parse
    or validate never reaches the review step at all -- but both are
    concatenated rather than picked between, so neither caller needs to
    know which one applies."""
    review_note = plan_review_findings_note(ledger, project, attempt_id)
    check_note = plan_check_failure_note(ledger, project, attempt_id)
    if review_note and check_note:
        return f"{review_note}\n\n{check_note}"
    return review_note or check_note


def _early_station_round_stop(run: Run, pos: JobPosition, station: str) -> FullOutcome | None:
    """a past incident (반장 지시,: the SAME stop engine.application.
    repair.fix() already enforces for `gb2 fix` (a repeated review
    finding, or JobPosition.max_fix_rounds already spent), enforced
    here for `gb2 continue`'s own early-station reopen too -- which had
    NEITHER before this (appX chunkA-planning-loop,: 5
    consecutive plan_review_failed stops, none of them capped; the
    build-task cap repair.fix() enforces never runs on this path at
    all). None (keep going, reopen_for_retry as before) once a real
    content-failure retry is not actually at stake (`pos.stage` is not
    STAGE_PROBE_RETRY/STAGE_PLAN_REVIEW -- a plain crash resume of
    STAGE_FACTS/STAGE_PROBE/STAGE_PLAN/STAGE_DESIGN is not a round
    spent on feedback, same reasoning repair_target.fix_rounds_used's
    own docstring gives for a run_station/merge crash) or once
    `run.past_cap` is True -- the owner's one escape hatch, the same
    flag `gb2 fix --past-cap` uses (`gb2 continue --past-cap`)."""
    if run.past_cap or pos.stage not in (STAGE_PROBE_RETRY, STAGE_PLAN_REVIEW):
        return None
    max_rounds = max_fix_rounds(pos)
    attempt_id = pos.attempt_id or ""
    if station == "plan":
        current = review_findings_for_attempt(run.ledger, run.project, attempt_id)
        if current:
            prior = previous_review_findings(run.ledger, run.project, run.job, attempt_id)
            if review_findings_repeat(current, prior):
                rounds_used = early_station_rounds_used(run.ledger, run.project, run.job)
                reason = f"{NO_PROGRESS_PREFIX}{rounds_used}"
                record_event(run.ledger, run.project, run.job, pos.stage, pos.attempt_id,
                            stop_reason=reason, repeated_findings=current,
                            cost_usd_so_far=job_cost_usd(run.ledger, run.project, run.job),
                            max_fix_rounds=max_rounds)
                return FullOutcome("needs_owner", reason, pos.attempt_id, None)
    rounds_used = early_station_rounds_used(run.ledger, run.project, run.job)
    if rounds_used >= max_rounds:
        reason = f"{FIX_ROUNDS_EXHAUSTED_PREFIX}{rounds_used}"
        record_event(run.ledger, run.project, run.job, pos.stage, pos.attempt_id,
                    stop_reason=reason,
                    cost_usd_so_far=job_cost_usd(run.ledger, run.project, run.job),
                    max_fix_rounds=max_rounds)
        return FullOutcome("needs_owner", reason, pos.attempt_id, None)
    return None


def _owner_approved_current_plan(run: Run) -> bool:
    """True iff the latest plan.reviewed event for this job was recorded
    by the OWNER (`gb2 approve-plan-edit`, actor_kind "owner") and names
    the CURRENT plan.json digest -- the foreman took the plan over after
    a review stop. A planner/reviewer-recorded plan.reviewed
    (actor "processor") never counts here: that path already has its own
    maybe_rerun_plan_review digest check."""
    from engine.domain.plan_review import plan_digest
    from engine.domain.plan_model import plan_path

    latest = None
    for _seq, event in run.ledger.events(run.project, "plan.reviewed"):
        if event.payload.get("job") == run.job:
            latest = event
    if latest is None or latest.actor_kind != "owner":
        return False
    path = plan_path(run.repo, run.job)
    if not path.is_file():
        return False
    return latest.payload.get("digest") == plan_digest(path)


def _continue_from_owner_approved_plan(run: Run, pos: JobPosition) -> FullOutcome:
    """Same two steps run_plan_and_review takes after a passing review
    (commit the plan trail, then go on to design/close on the SAME
    attempt), minus the planner and the reviewer: the owner's approval
    is the review."""
    from engine.application.pipeline_stages import commit_job_trail
    from engine.application.pipeline_base import PLANNER_ROLE

    attempt_id = pos.attempt_id or ""
    commit_job_trail(run.config, run.ledger, run.project, attempt_id, run.job,
                     f"gongbang: {run.job} - plan (owner approved)", station="plan",
                     role=PLANNER_ROLE)
    return probe_plan_design_close(run.config, run.ledger, run.fakes, run.project, run.job,
                                    attempt_id, str(pos.get("request", "")), max_fix_rounds(pos),
                                    run.on_progress, run.processor_factory, start="design",
                                    probe_text=str(pos.get("probe_text", "")))


def _resume_reopened_station(run: Run, pos: JobPosition) -> FullOutcome:
    """A facts/probe/plan/design station that crashed, failed, or stopped
    on its own content (probe failure, plan review) reruns on a FRESH
    attempt: the stale one is closed (its merged work archived and
    overlaid into the new copy, see reopen_for_retry) because merge's
    own base_head check refuses a second merge of a path the job already
    committed on that attempt. Every early-station role gets a copy, so
    a small job (planner copy only) resumes here as a full job. A
    "plan" reopen also carries the stale attempt's review findings
    into the new planner's prompt (see plan_review_findings_note).

    A STAGE_PROBE_RETRY position whose stop_reason starts with
    "probe_targets_invalid:" (engine.domain.stage_checks.
    ProbeTargetsError -- facts.md's own "## 외부 대상" section has a
    malformed line) reopens "facts" instead of REOPEN_STATION's plain
    "probe" -- but ONLY if facts.md, as it stands in the repo RIGHT
    NOW, is STILL malformed (check_probe_targets still raises). An
    owner who already hand-repaired facts.md in the repo while the job
    sat needs_owner (reopen_for_retry's own retry_stage=="probe"
    branch below promotes exactly that edit into the job trail) must
    keep reopening "probe" as before -- redirecting that case to
    "facts" too would discard the owner's already-good repair and ask
    a facts worker to redo work that is already done. Only when
    nobody has fixed it yet does reopening "probe" again just
    reproduce the SAME ProbeTargetsError forever, with no progress --
    that is the case this redirects to a facts worker turn instead.
    Every other STAGE_PROBE_RETRY stop (a real probe script failure,
    stage_checks.check_probe's own "PROBE OK" miss) is untouched."""
    station = REOPEN_STATION[pos.stage]
    if (pos.stage == STAGE_PROBE_RETRY and isinstance(pos.stop_reason, str)
            and pos.stop_reason.startswith("probe_targets_invalid:")
            and _facts_still_invalid(run, pos)):
        station = "facts"
    if pos.stage == STAGE_PLAN_REVIEW and _owner_approved_current_plan(run):
        return _continue_from_owner_approved_plan(run, pos)
    stop = _early_station_round_stop(run, pos, station)
    if stop is not None:
        return stop
    request = str(pos.get("request", ""))
    prior_findings_note = (_prior_plan_notes(run.ledger, run.project, pos.attempt_id or "")
                           if station == "plan" else "")
    intent = JobPosition(job=run.job, stage=STATION_STAGE[station], attempt_id=None,
                         extra={k: v for k, v in pos.extra.items()
                                if k in ("request", "probe_text", "max_fix_rounds",
                                         "restore_from")})
    failed, attempt_id = reopen_for_retry(run.config, run.ledger, run.fakes, run.project,
                                           run.job, pos.attempt_id or "", run.on_progress,
                                           run.processor_factory, retry_stage=station,
                                           intent=intent)
    if failed is not None:
        return failed
    if station == "facts":
        return facts_then_rest(run, attempt_id, request, max_fix_rounds(pos))
    return probe_plan_design_close(run.config, run.ledger, run.fakes, run.project, run.job,
                                    attempt_id, request, max_fix_rounds(pos), run.on_progress,
                                    run.processor_factory, start=station,
                                    probe_text=str(pos.get("probe_text", "")),
                                    prior_findings_note=prior_findings_note)


resume_reopened_station = _resume_reopened_station


class ForceReopenError(FullOrbitError):
    """Raised by force_reopen_station -- `gb2 continue <app> --from
    <station>` refused outright, nothing changed. Distinct from
    FullOrbitError's other plain uses so a CLI caller can catch this
    one specifically and print its message rather than a traceback."""


_EARLY_STATION_ORDER = ("facts", "probe", "plan", "design")


def _current_early_station(pos: JobPosition) -> str | None:
    """The station name `pos`'s current stop corresponds to, iff `pos`
    is stopped at an early station (facts/probe/plan/design) -- via
    REOPEN_STATION (STAGE_FACTS/PROBE/PLAN/DESIGN direct, plus
    STAGE_PROBE_RETRY/STAGE_PLAN_REVIEW), via STAGE_EARLY_STATION's own
    "station" field (a run_station owner-request stop), or via
    STAGE_DESIGN_APPROVAL itself ("design" -- a review finding: the
    design comp this gate is waiting on is not yet approved, so it is
    still the owner's to reject and have redone, same as any other
    early station's own output before it is committed past). None for
    every other stage -- everything from the build stage proper
    (STAGE_TASK_OWNER/STAGE_TASK onward) is where force_reopen_station
    refuses outright."""
    if pos.stage in REOPEN_STATION:
        return REOPEN_STATION[pos.stage]
    if pos.stage == STAGE_DESIGN_APPROVAL:
        return "design"
    if pos.stage == STAGE_EARLY_STATION:
        station = str(pos.get("station", ""))
        return station if station in _EARLY_STATION_ORDER else None
    return None


def force_reopen_station(run: Run, pos: JobPosition, from_station: str) -> FullOutcome:
    """`gb2 continue <app> --from <station>`: the owner naming an early
    station to reopen from, EARLIER than (or equal to) the one `pos` is
    currently stopped at -- the escape hatch for a real stuck job
    (결함 24,, 마케팅 채팅 job): an owner/foreman decision
    made AFTER facts.md was written dropped one probe target
    (systemctl service status) from scope, but the probe station
    cannot edit facts.md (station_record_prefixes reserves facts.md to
    the "facts" station alone, 결함 12) and the ordinary `gb2 continue`
    only ever reruns "probe" on a probe_failed/STAGE_PROBE_RETRY stop
    (_resume_reopened_station) -- so there was no way to ever remove
    that target from facts.md's own "## 외부 대상" section, and
    reopening "probe" again just reproduces the exact same scope
    forever. This reopens "facts" instead so the owner's edited
    facts.md (with the target removed) becomes the job's real input,
    then runs the rest of the early stations (probe/plan/[design])
    the same way _resume_reopened_station already does.

    Raises ForceReopenError (nothing changed -- no new position is
    recorded) when:
      - `pos` is not currently stopped at an early station at all (the
        build stage proper, STAGE_TASK_OWNER/STAGE_TASK onward -- a
        crashed or stopped build task has its own recovery through
        RESUME_TABLE, never this escape hatch; STAGE_DESIGN_APPROVAL
        itself is NOT this case -- 결함 (a review finding), see
        _current_early_station);
      - `from_station` is not one of _EARLY_STATION_ORDER;
      - `from_station` is LATER than `pos`'s own current station in
        _EARLY_STATION_ORDER (an owner can only go BACK to an earlier
        station they already passed, never skip ahead -- that is what
        the ordinary resume already does)."""
    current = _current_early_station(pos)
    if current is None:
        raise ForceReopenError(
            f"build stage에서는 --from을 쓸 수 없다: stage={pos.stage!r}")
    if from_station not in _EARLY_STATION_ORDER:
        raise ForceReopenError(f"알 수 없는 station: {from_station!r}")
    if _EARLY_STATION_ORDER.index(from_station) > _EARLY_STATION_ORDER.index(current):
        raise ForceReopenError(
            f"{from_station!r}은 현재 위치({current!r})보다 뒤 칸이라 되돌릴 수 없다")
    request = str(pos.get("request", ""))
    prior_findings_note = (_prior_plan_notes(run.ledger, run.project, pos.attempt_id or "")
                           if from_station == "plan" else "")
    intent = JobPosition(job=run.job, stage=STATION_STAGE[from_station], attempt_id=None,
                         extra={k: v for k, v in pos.extra.items()
                                if k in ("request", "probe_text", "max_fix_rounds",
                                         "restore_from")})
    failed, attempt_id = reopen_for_retry(run.config, run.ledger, run.fakes, run.project,
                                           run.job, pos.attempt_id or "", run.on_progress,
                                           run.processor_factory, retry_stage=from_station,
                                           intent=intent)
    if failed is not None:
        return failed
    if from_station == "facts":
        return facts_then_rest(run, attempt_id, request, max_fix_rounds(pos))
    return probe_plan_design_close(run.config, run.ledger, run.fakes, run.project, run.job,
                                    attempt_id, request, max_fix_rounds(pos), run.on_progress,
                                    run.processor_factory, start=from_station,
                                    probe_text=str(pos.get("probe_text", "")),
                                    prior_findings_note=prior_findings_note)


def resume_early_station(run: Run, pos: JobPosition) -> FullOutcome:
    """A facts/probe/plan/design run_station stop reruns that station on
    the SAME attempt once every owner request it made is fulfilled, with
    a continuation note when there were requests (the earlier run_station
    request is already "done", so an unchanged prompt would never run).
    An internal-crash stop (stop_reason "internal:...") unblocks that
    request first (unblock_internal_crash)."""
    attempt_id = pos.attempt_id or ""
    if isinstance(pos.stop_reason, str) and pos.stop_reason.startswith("internal:"):
        unblock_internal_crash(run.ledger, run.project, attempt_id)
    if outstanding_requests(run.ledger, run.project, pos, repo=run.repo):
        return FullOutcome("needs_owner", "owner_request", pos.attempt_id, None)
    station = str(pos.get("station", ""))
    request = str(pos.get("request", ""))
    needs_note = bool(pos.pending_requests)
    if station == "facts":
        return facts_then_rest(run, attempt_id, request, max_fix_rounds(pos),
                                needs_resume_note=needs_note)
    if station not in ("probe", "plan", "design"):
        raise FullOrbitError(f"unknown early station for resume: {station!r}")
    return probe_plan_design_close(run.config, run.ledger, run.fakes, run.project, run.job,
                                    attempt_id, request, max_fix_rounds(pos), run.on_progress,
                                    run.processor_factory, start=station,
                                    probe_text=str(pos.get("probe_text", "")),
                                    needs_resume_note=needs_note)



def resume_small_plan(run: Run, pos: JobPosition) -> FullOutcome:
    from engine.application import quick_pipeline as small_orbit

    return small_orbit.resume_small_plan(run.config, run.ledger, run.fakes, run.project,
                                         run.job, pos, on_progress=run.on_progress,
                                         processor_factory=run.processor_factory)



def resume_design_approval(run: Run, pos: JobPosition) -> FullOutcome:
    """Re-review an owner edit to plan.json first (only then is the plan
    checked against its review), then wait for the approval, then close
    the shared early attempt and run the tasks."""
    attempt_id = pos.attempt_id or ""
    try:
        plan = load_plan(run.repo, run.job)
    except PlanError as exc:
        return FullOutcome("failed", f"plan:{exc.reason}", attempt_id, None)
    stale = maybe_rerun_plan_review(run.config, run.ledger, run.fakes, run.project, run.job,
                                     attempt_id, plan, run.on_progress, run.processor_factory)
    if stale is not None:
        return stale
    stop = stop_for_resumed_plan_problems(run.ledger, run.project, run.job, attempt_id,
                                           run.repo)
    if stop is not None:
        return stop
    if not check_design_approved(run.ledger, run.project, run.job):
        return FullOutcome("needs_owner", "design_approval", attempt_id, None)
    # close_early_attempt_then_tasks itself calls design_approval_
    # still_matches (that ONE shared entry point
    # covers this caller, resume_early_close, AND the first-time path,
    # so there is no second, independent copy of the check to drift
    # out of sync with it here).
    return close_early_attempt_then_tasks(run, attempt_id, plan, max_fix_rounds(pos))



def resume_early_close(run: Run, pos: JobPosition) -> FullOutcome:
    stop, plan = resumed_plan(run, pos)
    if plan is None:
        return stop  # type: ignore[return-value]
    return close_early_attempt_then_tasks(run, pos.attempt_id or "", plan,
                                           max_fix_rounds(pos), record=False)



def resume_acceptance(run: Run, pos: JobPosition) -> FullOutcome:
    """post_tasks (a crash or failure anywhere in acceptance -> handoff ->
    security -> release) and a requirements-review stop both rerun
    acceptance fresh: the owner may have changed the plan or the app."""
    stop, plan = resumed_plan(run, pos)
    if plan is None:
        return stop  # type: ignore[return-value]
    return run_acceptance_and_handoff(run.config, run.ledger, run.fakes, run.project, run.job,
                                       plan, max_fix_rounds(pos), run.on_progress,
                                       run.processor_factory, pos.attempt_id)



def resume_security_fix(run: Run, pos: JobPosition) -> FullOutcome:
    """Rerun only the security check, unless the source acceptance and
    the handoff certified (its content fingerprint, which also sees
    uncommitted edits -- and, now, a shared file_dep_targets package's
    own content, see engine.application.pipeline_tasks.finish_after_handoff's
    own docstring for this same pairing, a review finding (4), s69-certfp
    has changed since -- then acceptance reruns too."""
    stop, plan = resumed_plan(run, pos)
    if plan is None:
        return stop  # type: ignore[return-value]
    from engine.infrastructure import evidence

    project_config = run.config.projects[run.project]
    certified = pos.get("certified_fingerprint")
    plain_fp = evidence.fingerprint(run.repo, project_config.excludes)
    current = evidence.with_file_deps(plain_fp, project_config.file_dep_targets)
    if certified and current != certified:
        return resume_acceptance(run, pos)
    return finish_after_handoff(run.config, run.ledger, run.project, run.job, plan,
                                 pos.attempt_id or "", Path(str(pos.get("report_path", ""))),
                                 max_fix_rounds(pos), fakes=run.fakes,
                                 on_progress=run.on_progress, processor_factory=run.processor_factory)



def resume_inflight_publish(run: Run, attempt_id: str, request_id: str) -> None:
    """An approved publish request left "running" (its process died
    mid-journal, e.g. right after call_publisher was journaled started)
    is resumed as that same request: the processor's own resume rule and
    the publisher's receipt reconcile decide whether anything is
    dispatched again. A request another live processor holds is left
    alone; finalize_release then keeps waiting."""
    from engine.application.processor import ProcessorBusy

    if run.ledger.request(request_id).status != "running":
        return
    factory = run.processor_factory or orbit.default_processor_factory
    processor = factory(run.config, run.ledger, run.fakes_for(attempt_id))
    try:
        processor.run_request(run.project, request_id)
    except ProcessorBusy:
        return



def resume_release_approval(run: Run, pos: JobPosition) -> FullOutcome:
    """finalize_release no-ops while the publish request is still
    queued/running, and finishes report -> close -> decision once it is
    terminal (including after a crash in between)."""
    stop, _plan = resumed_plan(run, pos)
    if stop is not None:
        return stop
    if not pos.get("request_id"):
        return prepare_release(run.ledger, run.config, run.project, pos)
    resume_inflight_publish(run, pos.attempt_id or "", str(pos.get("request_id")))
    return finalize_release(run.config, run.ledger, run.fakes, run.project,
                            pos.attempt_id or "", run.job, str(pos.get("request_id", "")),
                            str(pos.get("report_path", "")), run.on_progress,
                            run.processor_factory)



# resume_task/rerun_task_on_new_attempt/resume_task_owner/
# rerun_task_owner_stop/continue_task_stages now live in
# engine.application.pipeline_resume_tasks (imported above) -- see that
# module's own docstring for why.


RESUME_TABLE: dict[str, Callable[[Run, JobPosition], FullOutcome]] = {
    STAGE_FACTS: _resume_reopened_station,
    STAGE_PROBE: _resume_reopened_station,
    STAGE_PLAN: _resume_reopened_station,
    STAGE_DESIGN: _resume_reopened_station,
    STAGE_PROBE_RETRY: _resume_reopened_station,
    STAGE_PLAN_REVIEW: _resume_reopened_station,
    STAGE_EARLY_STATION: resume_early_station,
    STAGE_SMALL_PLAN: resume_small_plan,
    STAGE_DESIGN_APPROVAL: resume_design_approval,
    STAGE_EARLY_CLOSE: resume_early_close,
    STAGE_TASK: resume_task,
    STAGE_TASK_OWNER: resume_task_owner,
    STAGE_POST_TASKS: resume_acceptance,
    STAGE_REQUIREMENTS_REVIEW: resume_acceptance,
    STAGE_SECURITY_FIX: resume_security_fix,
    STAGE_RELEASE_APPROVAL: resume_release_approval,
}


__all__ = [
    "continue_full_job", "resumed_plan_stop", "RESUME_TABLE", "overlay_archive_into_copy",
    "abandon_orphaned_requests", "archive_dir_for_attempt", "archive_has_content",
    "unblock_internal_crash", "plan_review_findings_note",
    "STAGE_DESIGN_APPROVAL", "STAGE_TASK_OWNER", "STAGE_PROBE_RETRY", "STAGE_PLAN_REVIEW",
    "STAGE_REQUIREMENTS_REVIEW", "STAGE_SECURITY_FIX", "STAGE_RELEASE_APPROVAL",
    "STAGE_EARLY_STATION", "force_reopen_station", "ForceReopenError", "continue_full_job_from",
]
