"""Crash/stop recovery for the full orbit: `continue_full_job` (tools/
gb2's `continue`/`approve-design`/`approve-install`) reads the newest
`job.position` and dispatches through RESUME_TABLE below to whichever
of these handlers matches that stage -- one per place engine.full_
orbit_early/_tasks/_release can stop a job (an early station's
run_station/content gate, a design approval, a build task, the
acceptance/handoff/security/release tail).

Depends on engine.application.pipeline_base/_runner/_release/_early/_tasks;
nothing in those four calls back into this module -- this is the only
one of the five that depends on all the others."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from engine.domain import faults
from engine.application import task_pipeline as orbit
from engine.config import EngineConfig
from engine.application.pipeline_base import (
    BUILDER_ROLE, DESIGNER_ROLE, EARLY_ROLES, FACTS_ROLE, PLANNER_ROLE, PROBER_ROLE,
    FullOrbitError, FullOutcome, ProgressCallback, Run, checked_facts,
)
from engine.application.pipeline_planning import (
    close_early_attempt_then_tasks, facts_then_rest, maybe_rerun_plan_review,
    probe_plan_design_close,
)
from engine.application.pipeline_release import finalize_release, prepare_release
from engine.application.pipeline_stages import (
    attempt_has_requests, close_attempt, commit_paths, open_with_intent,
    record_task_stop, station_outcome_to_full, stops_for_owner,
)
from engine.application.pipeline_tasks import (
    build_task_gate, finish_after_handoff, finish_from_tasks, install_followup_task_text,
    run_acceptance_and_handoff, run_task_attempt, task_attempt_config, task_stages,
)
from engine.domain.stage_checks import check_design_approved, facts_path
from engine.domain.records import Event
from engine.domain.plan import Plan, PlanError, Task, load_plan
from engine.application.job_state import (
    STAGE_DESIGN, STAGE_DESIGN_APPROVAL, STAGE_EARLY_CLOSE, STAGE_EARLY_STATION, STAGE_FACTS,
    STAGE_PLAN, STAGE_PLAN_REVIEW, STAGE_POST_TASKS, STAGE_PROBE, STAGE_PROBE_RETRY,
    STAGE_RELEASE_APPROVAL, STAGE_REQUIREMENTS_REVIEW, STAGE_SECURITY_FIX, STAGE_SMALL_PLAN,
    STAGE_TASK, STAGE_TASK_OWNER, JobPosition, outstanding_requests, record_position,
    resume_job, stages_from_spec, stages_spec, task_stop_position,
)
from engine.application.processor import Fakes
from engine.application.steps.open_job import copy_root, dirty_paths, open_job

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger


def task_attempt_committed(ledger: Ledger, project: str, attempt_id: str) -> bool:
    """True iff `attempt_id`'s own "commit" request reached "done" --
    the one durable signal that a build task actually finished. Needed
    because attempt.status alone cannot tell a GENUINELY finished task
    apart from one fixup.close_failed_attempt closed for a repair (the
    SAME "closed" status either way): a task whose review/bench failed
    never reaches the commit stage at all before fixup closes it to
    archive the work and open a replacement attempt."""
    for _seq, event in ledger.events(project, "request.submitted"):
        if event.attempt_id != attempt_id or event.payload.get("kind") != "commit":
            continue
        request_id = event.payload.get("request_id")
        if isinstance(request_id, str) and ledger.request(request_id).status == "done":
            return True
    return False



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
    attempt-retry."""
    from engine.application.processor import cancel_request

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
    attempt, because the repo's HEAD (carrying that first, already-
    committed probe/plan trail) no longer matches that attempt's fixed
    base_head: "someone else's committed change landed there since this
    copy's base" -- true here even though the "someone else" is this
    very job's own earlier station. A fresh attempt's own base_head is
    the CURRENT repo HEAD (already including that earlier commit), so
    the retry's own new merge sees no conflict. Returns (None, the new
    attempt_id) to keep going, or (the failed FullOutcome to return
    right now, "").

    `retry_stage`, when given ("facts"/"probe"/"plan"/"design"), is
    this retry's OWN station -- close's own archive_tmp step already
    copied out every path THAT station's merge had landed uncommitted
    in the real repo (including, e.g., a probe log an owner hand-edited
    while the job sat STAGE_PROBE_RETRY needs_owner) before reset_to_
    head wiped the real repo back to HEAD; overlay_archive_into_copy
    restores that same content into the fresh attempt's own role copy
    (RETRY_STAGE_ROLE), so the owner's edit is still there for the
    retry to see. Without this, close's own archive preserved it only
    in cold storage -- the owner's edit never actually reached the new
    attempt at all."""
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



# ---------------------------------------------------------------------
# resume handlers -- one per position stage, dispatched by
# engine.application.job_state.resume_job through RESUME_TABLE
# ---------------------------------------------------------------------

def max_fix_rounds(pos: JobPosition) -> int:
    return pos.max_fix_rounds



def task_for(plan: Plan, task_id: str, fixing: bool) -> Task:
    """The Task a position names: an acceptance-fix round's synthesized
    task (owning every plan task's paths), or the plan's own task."""
    if fixing:
        all_owns = tuple(sorted({entry for t in plan.tasks for entry in t.owns}))
        return Task(id=task_id, title="수락 실패 수정", detail="", owns=all_owns, depends_on=())
    by_id = {t.id: t for t in plan.tasks}
    task = by_id.get(task_id)
    if task is None:
        raise FullOrbitError(f"unknown task id for resume: {task_id!r}")
    return task



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



def resumed_plan(run: Run, pos: JobPosition) -> tuple[FullOutcome | None, Plan | None]:
    """The committed plan, or the failed outcome when it cannot be
    loaded. The plan was already checked against its review before the
    resume dispatched (resumed_plan_stop)."""
    try:
        plan = load_plan(run.repo, run.job)
    except PlanError as exc:
        return FullOutcome("failed", f"plan:{exc.reason}", pos.attempt_id, None), None
    return None, plan



def advance_past_task(run: Run, pos: JobPosition, plan: Plan,
                       attempt_id: str | None) -> FullOutcome:
    """The task `pos` names has finished on `attempt_id`: continue with the
    acceptance-fix loop (an acceptance-fix round) or the tasks still
    queued behind it."""
    if pos.fixing:
        return run_acceptance_and_handoff(run.config, run.ledger, run.fakes, run.project,
                                           run.job, plan, max_fix_rounds(pos), run.on_progress,
                                           run.processor_factory, attempt_id)
    by_id = {t.id: t for t in plan.tasks}
    remaining = [by_id[i] for i in pos.remaining_task_ids[1:] if i in by_id]
    return finish_from_tasks(run.config, run.ledger, run.fakes, run.project, run.job, plan,
                              remaining, max_fix_rounds(pos), run.on_progress,
                              run.processor_factory, attempt_id)



STATION_STAGE = {
    "facts": STAGE_FACTS, "probe": STAGE_PROBE, "plan": STAGE_PLAN, "design": STAGE_DESIGN,
}



REOPEN_STATION = {
    STAGE_FACTS: "facts", STAGE_PROBE: "probe", STAGE_PLAN: "plan", STAGE_DESIGN: "design",
    STAGE_PROBE_RETRY: "probe", STAGE_PLAN_REVIEW: "plan",
}



def _resume_reopened_station(run: Run, pos: JobPosition) -> FullOutcome:
    """A facts/probe/plan/design station that crashed, failed, or stopped
    on its own content (probe failure, plan review) reruns on a FRESH
    attempt: the stale one is closed (its merged work archived and
    overlaid into the new copy, see reopen_for_retry) because merge's
    own base_head check refuses a second merge of a path the job already
    committed on that attempt. Every early-station role gets a copy, so
    a small job (planner copy only) resumes here as a full job."""
    station = REOPEN_STATION[pos.stage]
    request = str(pos.get("request", ""))
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
                                    probe_text=str(pos.get("probe_text", "")))


resume_reopened_station = _resume_reopened_station



def resume_early_station(run: Run, pos: JobPosition) -> FullOutcome:
    """A facts/probe/plan/design run_station stop reruns that station on
    the SAME attempt once every owner request it made is fulfilled, with
    a continuation note when there were requests (the earlier run_station
    request is already "done", so an unchanged prompt would never run)."""
    if outstanding_requests(run.ledger, run.project, pos, repo=run.repo):
        return FullOutcome("needs_owner", "owner_request", pos.attempt_id, None)
    station = str(pos.get("station", ""))
    request = str(pos.get("request", ""))
    attempt_id = pos.attempt_id or ""
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



def resume_task(run: Run, pos: JobPosition) -> FullOutcome:
    """A task position with no stop recorded: the task crashed, or its
    attempt ended at an ordinary "failed". A closed attempt either
    committed (the task finished -- advance) or was closed without
    committing (abandoned, or closed for a repair that never opened its
    replacement -- rerun the task from scratch on a new attempt). An
    active attempt reruns its build on the same attempt; the idempotent
    request ids skip whatever already finished."""
    stop, plan = resumed_plan(run, pos)
    if plan is None:
        return stop  # type: ignore[return-value]
    attempt_id = pos.attempt_id or ""
    if attempt_id and run.ledger.attempt(attempt_id).status in ("closed", "abandoned"):
        if task_attempt_committed(run.ledger, run.project, attempt_id):
            return advance_past_task(run, pos, plan, attempt_id)
        return rerun_task_on_new_attempt(run, pos, plan)
    return rerun_task_owner_stop(run, pos, plan, crash=True)



def rerun_task_on_new_attempt(run: Run, pos: JobPosition, plan: Plan) -> FullOutcome:
    task = task_for(plan, pos.task_id or "", pos.fixing)
    task_text = str(pos.get("task_text", ""))
    base_task_text = str(pos.get("base_task_text", task_text))
    outcome = run_task_attempt(run.config, run.ledger, run.fakes, run.project, run.job, task,
                                task_text, plan, run.on_progress, run.processor_factory,
                                base_task_text, fixing=pos.fixing,
                                remaining_task_ids=pos.remaining_task_ids,
                                max_fix_rounds=max_fix_rounds(pos))
    full = station_outcome_to_full(outcome, "build")
    if full is not None:
        return full
    return advance_past_task(run, pos, plan, outcome.attempt_id)



def resume_task_owner(run: Run, pos: JobPosition) -> FullOutcome:
    stop, plan = resumed_plan(run, pos)
    if plan is None:
        return stop  # type: ignore[return-value]
    return rerun_task_owner_stop(run, pos, plan, crash=False)



def rerun_task_owner_stop(run: Run, pos: JobPosition, plan: Plan, *,
                           crash: bool) -> FullOutcome:
    """Continues a build task on its SAME attempt/copy:
    - a required-context gap rebuilds and rechecks the context first;
    - a crash submits the task's own stage list again (task_stages):
      every request that already finished reconciles by its id;
    - worker_stuck reruns the build from scratch;
    - a fulfilled owner request reruns run_station with a note that the
      install is done and the already-changed files to read first.
    An owner request that is still outstanding stays needs_owner."""
    if outstanding_requests(run.ledger, run.project, pos, repo=run.repo):
        return FullOutcome("needs_owner", "owner_request", pos.attempt_id, None)
    attempt_id = pos.attempt_id or ""
    base_task_text = str(pos.get("base_task_text", pos.get("task_text", "")))
    task_text = str(pos.get("task_text", base_task_text))
    task = task_for(plan, pos.task_id or "", pos.fixing)
    run_config = task_attempt_config(run.config, run.project, run.job, plan)
    spec = pos.get("stages")
    if not isinstance(spec, list):
        # A position recorded before stage specs were: rebuild the same list.
        spec = stages_spec(task_stages(run.config, run_config, run.ledger, run.project, run.job,
                                        plan, task, task_text, base_task_text, attempt_id))
    if crash:
        stages = stages_from_spec(spec, run_config, attempt_id)
        return continue_task_stages(run, pos, plan, run_config, stages, task_text)
    if pos.get("context_gap"):
        gap_gate = build_task_gate(run.config, run.ledger, run.project, plan, base_task_text,
                                   attempt_id)
        if gap_gate.unmet is not None:
            record_position(run.ledger, run.project, task_stop_position(
                pos, attempt_id, gap_gate.unmet, (), context_gap=True))
            return FullOutcome("needs_owner", gap_gate.unmet, attempt_id, None)
        task_text = gap_gate.prompt(base_task_text)
        run_station_text = task_text
    elif pos.stop_reason == "worker_stuck":
        run_station_text = task_text
    else:
        build_copy = copy_root(run_config, attempt_id, BUILDER_ROLE)
        run_station_text = install_followup_task_text(task_text, build_copy)
    merge_retry_n = sum(1 for _seq, event in run.ledger.events(run.project, "merge.recorded")
                        if event.attempt_id == attempt_id)
    stages = stages_from_spec(spec, run_config, attempt_id, task_text=run_station_text,
                              merge_retry_n=merge_retry_n)
    return continue_task_stages(run, pos, plan, run_config, stages, task_text)



def continue_task_stages(run: Run, pos: JobPosition, plan: Plan, run_config: EngineConfig,
                          stages: list[orbit.Stage], task_text: str) -> FullOutcome:
    """Runs a resumed task's stages on its own attempt, records a new
    owner stop if it makes one, and otherwise moves past the task."""
    attempt_id = pos.attempt_id or ""
    outcome = orbit.continue_job(run_config, run.ledger, run.fakes_for(attempt_id), run.project,
                                 attempt_id, stages, processor_factory=run.processor_factory,
                                 on_progress=run.on_progress)
    if stops_for_owner(outcome):
        task_pos = replace(pos, extra={**{k: v for k, v in pos.extra.items()
                                          if k != "context_gap"}, "task_text": task_text})
        record_task_stop(run.ledger, run.project, task_pos, outcome)
    full = station_outcome_to_full(outcome, "build")
    if full is not None:
        return full
    return advance_past_task(run, pos, plan, attempt_id)


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
    "STAGE_DESIGN_APPROVAL", "STAGE_TASK_OWNER", "STAGE_PROBE_RETRY", "STAGE_PLAN_REVIEW",
    "STAGE_REQUIREMENTS_REVIEW", "STAGE_SECURITY_FIX", "STAGE_RELEASE_APPROVAL",
    "STAGE_EARLY_STATION",
]
