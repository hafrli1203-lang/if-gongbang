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

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from engine.domain import faults
from engine.application import task_pipeline as orbit
from engine.config import EngineConfig
from engine.application.pipeline_base import (
    DESIGNER_ROLE, EARLY_ROLES, FACTS_ROLE, PLANNER_ROLE, PROBER_ROLE,
    FullOrbitError, FullOutcome, ProgressCallback, Run, checked_facts, max_fix_rounds,
    resumed_plan, task_attempt_committed, task_for, unblock_internal_crash,
)
from engine.application.pipeline_planning import (
    close_early_attempt_then_tasks, facts_then_rest, maybe_rerun_plan_review,
    plan_check_failure_note, plan_review_findings_note, probe_plan_design_close,
)
from engine.application.pipeline_release import finalize_release, prepare_release
from engine.application.pipeline_resume_tasks import (
    advance_past_task, continue_task_stages, rerun_task_on_new_attempt, rerun_task_owner_stop,
    resume_task, resume_task_owner,
)
from engine.application.pipeline_stages import (
    attempt_has_requests, close_attempt, commit_paths, open_with_intent,
    station_outcome_to_full,
)
from engine.application.pipeline_tasks import finish_after_handoff, run_acceptance_and_handoff
from engine.domain.stage_checks import (
    ProbeTargetsError, check_design_approved, check_probe_targets, facts_path,
)
from engine.domain.records import Event
from engine.domain.plan import PlanError, load_plan
from engine.application.job_state import (
    STAGE_DESIGN, STAGE_DESIGN_APPROVAL, STAGE_EARLY_CLOSE, STAGE_EARLY_STATION, STAGE_FACTS,
    STAGE_PLAN, STAGE_PLAN_REVIEW, STAGE_POST_TASKS, STAGE_PROBE, STAGE_PROBE_RETRY,
    STAGE_RELEASE_APPROVAL, STAGE_REQUIREMENTS_REVIEW, STAGE_SECURITY_FIX, STAGE_SMALL_PLAN,
    STAGE_TASK, STAGE_TASK_OWNER, JobPosition, outstanding_requests, resume_job, resume_job_from,
)
from engine.application.processor import Fakes
from engine.application.steps.open_job import copy_root, dirty_paths, open_job

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger


def abandon_orphaned_requests(ledger: Ledger, project: str, attempt_id: str) -> None:
    """Reconciles every non-terminal request left over for `attempt_id`
    before this stale attempt is discarded (see reopen_for_retry's own
    docstring) -- a genuine crash can leave one "queued" (submitted but
    never dispatched) or "running" (Processor.run_once's own
    `_set_status(..., "running", {})` runs BEFORE the step that
    actually does the work, so a process death mid-dispatch never gets
    a chance to set a terminal status at all).

    "queued" is always safe to cancel outright (engine.application.processor.
    cancel_request -- it never dispatched, so there is no external
    effect to reconcile). "running" is NOT: a request only reaches
    "running" once some processor's run_once claimed it, and that
    processor may still be alive and mid-step right now -- marking it
    "failed" out from under a LIVE processor would let two processors
    act on the same request's external effect at once. This function
    therefore only ever judges a "running" request orphaned while it
    itself holds this project's processor lock (the same lock run_once/
    run_request take) -- proof no other processor can be running
    anything for this project right this moment. If the lock is already
    held by another live processor (acquire_lock returns False), every
    "running" request is left untouched -- the caller, _reopen_for_
    retry, has nothing useful to do with that either way: trying again
    later is always safe, since this function runs again on the next
    attempt-retry.

    Also resolves an "unknown" request from an engine-internal crash
    (unblock_internal_crash -> "running" first) so the "running" branch
    below fails it out -- else Processor._block_on_unknown would refuse
    every later request this caller submits (e.g. close_attempt's)."""
    from engine.application.processor import cancel_request

    unblock_internal_crash(ledger, project, attempt_id)
    holder = f"full-orbit-orphan-{attempt_id}"
    have_lock = ledger.acquire_lock(project, holder)
    try:
        for _seq, event in ledger.events(project, "request.submitted"):
            if event.attempt_id != attempt_id:
                continue
            request_id = event.payload.get("request_id")
            if not isinstance(request_id, str):
                continue
            request = ledger.request(request_id)
            if request.status == "queued":
                cancel_request(ledger, project, request_id, "crash_resume_abandoned")
            elif request.status == "running" and have_lock:
                faults.flow_boundary("full_orbit.orphan_abandoned")
                ledger.append(Event(
                    project=project, kind="request.status", actor_kind="processor",
                    actor_id="processor", attempt_id=attempt_id,
                    payload={"request_id": request_id, "status": "failed",
                             "result": {"reason": "crash_resume_abandoned"}},
                ))
    finally:
        if have_lock:
            ledger.release_lock(project, holder)


# Marker files close's own archive_tmp writes alongside the archived
# paths themselves (never part of the real diff) -- same constant
# engine.application.repair._ARCHIVE_MARKER_FILES defines, duplicated here rather
# than imported: this module does not depend on fixup.py's internals,
# same reasoning fixup.py's own module docstring gives for duplicating
# engine.application.steps.open_job's equivalent helper instead of reusing it.
ARCHIVE_MARKER_FILES = {".deleted", ".modes"}


def archive_dir_for_attempt(ledger: Ledger, project: str, attempt_id: str) -> Path | None:
    """The archive directory close already created for this attempt (its
    "archive.done" event), or None if it was never closed yet. Mirrors
    engine.application.repair.archive_dir_for_attempt."""
    path: Path | None = None
    for _seq, event in ledger.events(project, "archive.done"):
        if event.attempt_id == attempt_id:
            path = Path(str(event.payload["path"]))
    return path



def archive_has_content(archive_dir: Path) -> bool:
    """True iff `archive_dir` actually holds a change -- mirrors
    engine.application.repair.archive_has_content (see that function's own
    docstring for why an archive that exists but is empty, e.g. an
    attempt that never merged anything into the real repo, must never
    be mistaken for "owner work to restore")."""
    if not archive_dir.exists():
        return False
    deleted_file = archive_dir / ".deleted"
    if deleted_file.exists() and deleted_file.read_text(encoding="utf-8").strip():
        return True
    for path in archive_dir.rglob("*"):
        if path.is_file() and str(path.relative_to(archive_dir)) not in ARCHIVE_MARKER_FILES:
            return True
    return False



def overlay_archive_into_copy(config: EngineConfig, attempt_id: str, role: str,
                               archive_dir: Path) -> None:
    """Copies every archived file (an owner's own uncommitted edit to a
    facts/probe/plan/design station's files, made while the job sat
    needs_owner at STAGE_PROBE_RETRY/STAGE_PLAN_REVIEW/STAGE_EARLY_
    STATION/a plain crash) into the FRESH attempt's own `role` copy, as
    plain filesystem writes -- mirrors engine.application.repair.
    _overlay_archive_into_builder_copy exactly (see that function's own
    docstring), generalized to any role's copy, not just the builder's.
    Without this, reopen_for_retry's own close (archive, THEN reset_to_
    head) preserved the owner's edit only in cold storage -- the real
    repo was wiped back to HEAD and the new attempt's copy was archived
    fresh from that same HEAD, so the edit was never actually seen
    again by anything that runs after this."""
    import shutil

    dest_root = copy_root(config, attempt_id, role)

    modes: dict[str, object] = {}
    modes_file = archive_dir / ".modes"
    if modes_file.exists():
        try:
            modes = json.loads(modes_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            modes = {}

    deleted: list[str] = []
    deleted_file = archive_dir / ".deleted"
    if deleted_file.exists():
        deleted = [
            line for line in deleted_file.read_text(encoding="utf-8").splitlines() if line
        ]

    for src in sorted(archive_dir.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(archive_dir)
        if str(rel) in ARCHIVE_MARKER_FILES:
            continue
        dest = dest_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        if modes.get(str(rel)) == "100755":
            dest.chmod(dest.stat().st_mode | 0o111)

    for rel in deleted:
        target = dest_root / rel
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target, ignore_errors=True)



RETRY_STAGE_ROLE = {
    "facts": FACTS_ROLE, "probe": PROBER_ROLE, "plan": PLANNER_ROLE, "design": DESIGNER_ROLE,
}



def reopen_for_retry(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                      job_name: str, stale_attempt_id: str,
                      on_progress: ProgressCallback | None,
                      processor_factory: orbit.ProcessorFactory | None,
                      *, retry_stage: str | None = None,
                      intent: JobPosition | None = None) -> tuple[FullOutcome | None, str]:
    """`intent` (the position the retry reruns from, with no attempt yet)
    is recorded before the fresh attempt opens, carrying the archive to
    restore (`restore_from`); an attempt adopted from that intent that
    has no request yet is reused as-is, with the restore redone.

    Closes `stale_attempt_id` (report -> close, same as the design-
    approval resume already does on this shared facts/probe/plan/design
    attempt) and opens a brand-new one for the SAME job. Needed before
    a STAGE_PROBE_RETRY/STAGE_PLAN_REVIEW resume reruns a station whose
    own merge already landed and was committed once on `stale_attempt_id`
    -- engine.application.steps.merge's own base_head check (_verify_destinations_
    unchanged) refuses a SECOND merge of the same path on the SAME
    attempt, because the repo's HEAD no longer matches that attempt's
    fixed base_head ("someone else's committed change landed there
    since this copy's base" -- true even though the "someone else" is
    this very job's own earlier station). A fresh attempt's own
    base_head is the CURRENT repo HEAD, so the retry's own new merge
    sees no conflict. Returns (None, the new attempt_id) to keep
    going, or (the failed FullOutcome to return right now, "").

    `retry_stage`, when given ("facts"/"probe"/"plan"/"design"), is
    this retry's OWN station -- close's own archive_tmp step already
    copied out every path THAT station's merge had landed uncommitted
    (e.g. a probe log an owner hand-edited while needs_owner) before
    reset_to_head wiped the repo back to HEAD; overlay_archive_into_copy
    restores it into the fresh role copy (RETRY_STAGE_ROLE) -- else the
    owner's edit never reaches the new attempt."""
    # A genuine crash (as opposed to the two pre-existing callers' own
    # terminal "failed"/"done" stop) can leave ONE request for
    # `stale_attempt_id` still "running" -- engine.application.processor.Processor.
    # run_once treats "running" exactly like "queued" (something still
    # to dispatch), so close_attempt's own report/close requests,
    # submitted right below, would have run_once sweep that orphaned
    # request up and actually run it FIRST -- silently reconciling (and
    # wasting) a worker call for an attempt this resume is about to
    # discard entirely. A no-op for the two pre-existing callers (their
    # stale attempt's own request is already terminal by the time they
    # call this).
    if (intent is not None and stale_attempt_id
            and ledger.attempt(stale_attempt_id).status == "active"
            and not attempt_has_requests(ledger, project, stale_attempt_id)):
        restore_from = intent.get("restore_from")
        if retry_stage is not None and isinstance(restore_from, str):
            overlay_archive_into_copy(config, stale_attempt_id, RETRY_STAGE_ROLE[retry_stage],
                                       Path(restore_from))
        return None, stale_attempt_id
    if retry_stage in ("probe", "plan", "design"):
        # facts.md is this retry's input, read from the repository: an
        # owner's repair of it (made while the job waited) is promoted
        # into the job trail and checked, instead of being archived away
        # with the stale attempt's own output.
        repo = config.projects[project].repo
        facts_rel = str(facts_path(repo, job_name).relative_to(repo))
        if (repo / facts_rel).is_file() and facts_rel in dirty_paths(repo, ()):
            commit_paths(repo, [facts_rel], f"gongbang: {job_name} - facts (owner repair)")
            problems, _facts = checked_facts(repo, job_name)
            if problems:
                return FullOutcome("needs_owner", "facts:" + "; ".join(problems),
                                   stale_attempt_id, None), ""
    abandon_orphaned_requests(ledger, project, stale_attempt_id)
    close_outcome = close_attempt(config, ledger, fakes, project, stale_attempt_id, job_name,
                                   on_progress, processor_factory)
    if close_outcome.status != "done":
        return FullOutcome("failed", f"close:{close_outcome.reason}", stale_attempt_id, None), ""
    # Read only AFTER close has run -- close's own archive_tmp/archive_
    # commit steps are what actually create the "archive.done" event
    # this looks up; nothing exists to find before that.
    archive_dir = (archive_dir_for_attempt(ledger, project, stale_attempt_id)
                   if retry_stage is not None else None)
    restore = (retry_stage is not None and archive_dir is not None
               and archive_has_content(archive_dir))
    if intent is not None:
        extra = {k: v for k, v in intent.extra.items() if k != "restore_from"}
        if restore:
            extra["restore_from"] = str(archive_dir)
        new_attempt_id = open_with_intent(config, ledger, project,
                                           replace(intent, extra=extra), EARLY_ROLES)
    else:
        new_attempt_id = open_job(config, ledger, project, job_name, "full", EARLY_ROLES)
    if restore and archive_dir is not None and retry_stage is not None:
        overlay_archive_into_copy(config, new_attempt_id, RETRY_STAGE_ROLE[retry_stage],
                                   archive_dir)
    return None, new_attempt_id



def resumed_plan_problems(ledger: Ledger, project: str, job_name: str, repo: Path) -> list[str]:
    """engine.domain.plan.check_resumed_plan(ledger, project, job_name, repo),
    with "plan_never_reviewed" filtered out -- that code can be true
    for a job whose plan was reviewed before the "plan.reviewed" ledger
    event existed at all (an older worktree's own job), so on its own
    it is never a reason to stop a resume. Every OTHER code (plan load/
    validate_plan failures, "plan_changed_since_review", "requirement_
    removed_since_review: X", "task_removed_since_review: X") IS.

    Call sites that already run their own re-review before trusting the
    plan (STAGE_DESIGN_APPROVAL's own maybe_rerun_plan_review) call
    this AFTER that re-review, never before: checking first would
    wrongly stop a resume on "plan_changed_since_review" for an edit
    the re-review is about to re-approve anyway (resume_design_approval)."""
    from engine.domain import plan as plan_mod

    return [p for p in plan_mod.check_resumed_plan(ledger, project, job_name, repo)
            if p != "plan_never_reviewed"]



def stop_for_resumed_plan_problems(ledger: Ledger, project: str, job_name: str,
                                    attempt_id: str, repo: Path) -> FullOutcome | None:
    """None iff resumed_plan_problems finds nothing wrong -- the caller
    keeps going. Otherwise returns the needs_owner FullOutcome to
    return right now, WITHOUT recording a new position: a plan.json/
    scene edit made (or a requirement/task silently dropped) while this
    job sat needs_owner is caught before ANY stage reruns work against
    it, and the position that describes the real stop (e.g. a
    STAGE_TASK_OWNER stop with its remaining_task_ids) stays the newest,
    so the next `gb2 continue`, once the edit is fixed, resumes exactly
    where the job was."""
    plan_problems = resumed_plan_problems(ledger, project, job_name, repo)
    if not plan_problems:
        return None
    reason = ("resumed_plan_invalid:" + "; ".join(plan_problems))[:300]
    return FullOutcome("needs_owner", reason, attempt_id, None)



def continue_full_job(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                      job_name: str, *, on_progress: ProgressCallback | None = None,
                      processor_factory: orbit.ProcessorFactory | None = None) -> FullOutcome:
    """engine.application.job_state.resume_job under the name tools/gb2 calls."""
    return resume_job(config, ledger, fakes, project, job_name, on_progress=on_progress,
                      processor_factory=processor_factory)


def continue_full_job_from(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                           job_name: str, from_station: str, *,
                           on_progress: ProgressCallback | None = None,
                           processor_factory: orbit.ProcessorFactory | None = None) -> FullOutcome:
    """engine.application.job_state.resume_job_from under the name
    tools/gb2 calls (`gb2 continue <app> --from <station>`) -- see
    force_reopen_station's own docstring for what this is for."""
    return resume_job_from(config, ledger, fakes, project, job_name, from_station,
                           on_progress=on_progress, processor_factory=processor_factory)



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
    STAGE_PROBE_RETRY/STAGE_PLAN_REVIEW), or via STAGE_EARLY_STATION's
    own "station" field (a run_station owner-request stop). None for
    every other stage -- everything from STAGE_DESIGN_APPROVAL onward
    is the build stage (docs/v2.md 「궤도 9칸」), where force_reopen_
    station refuses outright."""
    if pos.stage in REOPEN_STATION:
        return REOPEN_STATION[pos.stage]
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
        build stage, from STAGE_DESIGN_APPROVAL onward -- a crashed or
        stopped build task has its own recovery through RESUME_TABLE,
        never this escape hatch);
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
    uncommitted edits) has changed since -- then acceptance reruns too."""
    stop, plan = resumed_plan(run, pos)
    if plan is None:
        return stop  # type: ignore[return-value]
    from engine.infrastructure import evidence

    certified = pos.get("certified_fingerprint")
    current = evidence.fingerprint(run.repo, run.config.projects[run.project].excludes)
    if certified and current != certified:
        return resume_acceptance(run, pos)
    return finish_after_handoff(run.config, run.ledger, run.project, run.job, plan,
                                 pos.attempt_id or "", Path(str(pos.get("report_path", ""))),
                                 max_fix_rounds(pos))



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
