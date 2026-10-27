"""Crash/stop recovery's own retry/reopen machinery -- split out of
engine.application.pipeline_resume purely to keep that module under the
repo's own 800-line budget (see that module's own docstring for the
whole resume-table picture; this module supplies
`reopen_for_retry`/`stop_for_resumed_plan_problems` that
_resume_reopened_station and resume_design_approval call, plus the two
`gb2`-facing entry points `continue_full_job`/`continue_full_job_from`
that just forward to engine.application.job_state.resume_job/
resume_job_from)."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from engine.domain import faults
# Eager, not TYPE_CHECKING-only: task_pipeline's own import chain
# (steps/__init__ -> steps.review -> repair -> task_stage_runner)
# imports pipeline_stages, which itself imports task_pipeline back --
# importing task_pipeline FIRST here (same convention engine.
# application.job_state/repair/pipeline_resume/pipeline_stages already
# follow) means that reentrant import finds task_pipeline already
# registered in sys.modules instead of starting a second, now-circular
# import of it. Every `orbit.X` use below is inside a function body or
# a quoted annotation, never at module level, so this is safe exactly
# the way it is for those other modules.
from engine.application import task_pipeline as orbit
from engine.config import EngineConfig
from engine.application.pipeline_base import (
    DESIGNER_ROLE, EARLY_ROLES, FACTS_ROLE, PLANNER_ROLE, PROBER_ROLE,
    FullOutcome, ProgressCallback, checked_facts, unblock_internal_crash,
)
from engine.application.pipeline_stages import (
    attempt_has_requests, close_attempt, commit_paths, open_with_intent,
)
from engine.domain.stage_checks import facts_path
from engine.domain.records import Event
from engine.application.job_state import JobPosition, resume_job, resume_job_from
from engine.application.processor import Fakes
from engine.application.steps.open_job import copy_root, dirty_paths, open_job

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger


def abandon_orphaned_requests(ledger: "Ledger", project: str, attempt_id: str) -> None:
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


def archive_dir_for_attempt(ledger: "Ledger", project: str, attempt_id: str) -> Path | None:
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


def reopen_for_retry(config: EngineConfig, ledger: "Ledger", fakes: Fakes, project: str,
                      job_name: str, stale_attempt_id: str,
                      on_progress: ProgressCallback | None,
                      processor_factory: "orbit.ProcessorFactory | None",
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


def resumed_plan_problems(ledger: "Ledger", project: str, job_name: str, repo: Path) -> list[str]:
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


def stop_for_resumed_plan_problems(ledger: "Ledger", project: str, job_name: str,
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


def continue_full_job(config: EngineConfig, ledger: "Ledger", fakes: Fakes, project: str,
                      job_name: str, *, on_progress: ProgressCallback | None = None,
                      processor_factory: "orbit.ProcessorFactory | None" = None,
                      past_cap: bool = False) -> FullOutcome:
    """engine.application.job_state.resume_job under the name tools/gb2
    calls. `past_cap` (`gb2 continue --past-cap`, a past incident): see
    resume_job's own docstring."""
    return resume_job(config, ledger, fakes, project, job_name, on_progress=on_progress,
                      processor_factory=processor_factory, past_cap=past_cap)


def continue_full_job_from(config: EngineConfig, ledger: "Ledger", fakes: Fakes, project: str,
                           job_name: str, from_station: str, *,
                           on_progress: ProgressCallback | None = None,
                           processor_factory: "orbit.ProcessorFactory | None" = None,
                           ) -> FullOutcome:
    """engine.application.job_state.resume_job_from under the name
    tools/gb2 calls (`gb2 continue <app> --from <station>`) -- see
    force_reopen_station's own docstring for what this is for."""
    return resume_job_from(config, ledger, fakes, project, job_name, from_station,
                           on_progress=on_progress, processor_factory=processor_factory)


__all__ = [
    "abandon_orphaned_requests", "ARCHIVE_MARKER_FILES", "archive_dir_for_attempt",
    "archive_has_content", "overlay_archive_into_copy", "RETRY_STAGE_ROLE", "reopen_for_retry",
    "resumed_plan_problems", "stop_for_resumed_plan_problems", "continue_full_job",
    "continue_full_job_from",
]
