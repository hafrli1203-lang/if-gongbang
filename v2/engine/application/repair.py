"""Fix/resume: continue a job whose latest attempt failed at review or
bench, instead of starting a fresh run from scratch.

Why this needs its own module rather than just calling open_job() again:
open_job()/_make_copies() always builds a fresh builder copy via `git
archive HEAD` -- it never looks at the project repo's current dirty
working tree. By the time a job has failed at review or bench, the
merge stage has ALREADY landed the builder's file changes into the real
repo as uncommitted changes (orbit.default_stages runs merge right
after run_station, before review/bench ever see the tree). HEAD itself
has not moved (commit only runs after review+bench both pass), so a
plain open_job() for a new attempt would archive HEAD -- the pre-change
tree -- and the merged work would simply be lost the moment the next
step (closing the old attempt) resets the repo back to HEAD.

The fix this module implements:
  1. find_fix_target() reads the ledger for the current (failed) attempt:
     the original run_station task, the reviewer's findings (if the job
     failed at review) and the failing test output's tail (if it failed
     at bench).
  2. close_failed_attempt() closes that attempt NORMALLY (the real
     "close" request, same one `gb2 skip-ship` and a plain successful
     job both use) -- this is what actually preserves the merged work:
     close's own archive_tmp/archive_commit steps copy every changed
     path to config.home/archive/{project}/{attempt}/{token}/ BEFORE
     reset_to_head wipes the working tree back to HEAD. Nothing here
     duplicates that archiving logic; it just locates the archive
     close already made (via the archive.done event) once close is
     done.
  3. open_job() for a NEW attempt on the SAME job (same job_name --
     open_job's own re-entry path bumps n and reuses job_id). Its
     builder copy is freshly archived from HEAD, same as any other new
     attempt -- clean, because close() just reset the repo back to HEAD.
  4. The previous attempt's archived files are copied into this new
     attempt's builder copy (not git-added or committed there -- see
     engine.application.steps.merge's own module docstring: collect_copies finds
     candidate files via `git status` in the copy, same as any worker
     edit, and compares their BYTES against base_head directly, never
     against the copy's own git history). Since base_head is the same
     sha for both attempts (HEAD never moved), merge's conflict checks
     see exactly what they would for a worker that was never
     interrupted.
  5. A composed task ("Continue this task. Original request: ... Review
     findings to address: ... Failing test output: ... Extra: ...") is
     handed to a fresh run_station request on the new attempt, and the
     job's normal stage list (engine.application.task_pipeline.fix_stages -- currently
     identical to default_stages) runs the rest of the way.

Every effect here is an existing, already-audited engine entry point
(processor.submit, engine.application.steps.open_job.open_job, orbit.continue_job)
-- this module adds no new state transition. The only genuinely new
filesystem operation is step 4's copy from the archive directory into
the new builder copy, which never touches the project repo itself.

Release: when the attempt being fixed belongs to a full-orbit job (its
position says so, engine.application.job_state.is_full_job), fix() runs with the
project's own `release` removed -- the whole job makes one publish
request at its very end, never one per repaired task. A plain
single-task job keeps its release stop right after its own commit.

Fix-after-a-failed-fix: a fix() attempt can itself fail before its OWN
merge ever completes (the builder crashes at run_station, or merge
itself fails) -- that attempt's own close then archives NOTHING (the
real repo was never touched for it). find_fix_target still accepts this
attempt as fixable as long as an EARLIER attempt of the same job has an
archive that DOES hold merged work (_latest_merged_archive walks back
past any number of empty archives to find it); FixTarget.carried_archive
names that directory, and fix() overlays it instead of the failing
attempt's own (empty) one. See find_fix_target's own docstring.

Resuming an interrupted fix() transition itself: fix() does several
non-idempotent things in sequence (close the old attempt, open a new
one, overlay the archive, submit the new run_station request) before
ever handing off to orbit.continue_job -- whose OWN crash-safety (the
existing "fix after a failed fix" story above, plus every step's own
reconcile()) only covers what happens AFTER that hand-off. A crash
between any two of those four steps used to leave the project stuck:
closed-but-not-reopened refuses the next fix with "not OPEN", and
opened-but-not-submitted leaves no failed request for find_fix_target to
find at all. fix() now journals its own progress through this one
transition as a small sequence of ledger events -- "fix.started"
(everything find_fix_target computed, BEFORE the old attempt is even
closed, so none of it needs recomputing after the old attempt no longer
looks the way find_fix_target saw it), "fix.closed_old" {archive_dir},
"fix.opening_new" {job_name} (this cycle's own fix id for detecting an
already-open attempt after a crash -- see _already_opened_new_attempt --
invalidated by a later "fix.opening_failed" {} if the open_job() call it
precedes itself raises, so a stale marker can never wrongly bound an
unrelated future job.opened for the same job_name), then ONE
`job.position` event (engine.application.job_state) that both names the replacement
attempt and moves the job's position onto it, "fix.restored",
"fix.submitted" {request_id}, "fix.finished" -- and _find_unfinished_fix_cycle looks for
a "fix.started" with no later "fix.finished" at the very top of fix(),
resuming from whichever of those events already exist instead of
calling find_fix_target (which the CLOSED-but-not-reopened case could
never satisfy) or redoing a step that already ran (which the
not-yet-submitted case would otherwise duplicate -- a second open_job
call would silently fork a second, orphaned attempt). Once
"fix.finished" is written (right before orbit.continue_job is ever
called), this journal's job is done -- any later crash is the job's own
ordinary, already-resumable machinery, not this module's concern.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from engine.domain import faults
from engine.application import task_pipeline as orbit
from engine.application import release as release_mod
from engine.config import EngineConfig
from engine.application.repair_target import (
    ARCHIVE_MARKER_FILES, FixTarget, FixupError, OUTSIDE_ALLOWED_PATH_STUCK_THRESHOLD,
    compose_fix_task, extended_owns_for_task, find_fix_target, fix_rounds_used,
    inject_context_into_fix_task, job_cost_usd, outside_allowed_path_denials,
    previous_review_findings, record_owns_extension, requests_for_attempt,
    review_findings_repeat, validate_also_own,
)
from engine.domain.records import Event
from engine.application.processor import Fakes, Processor, submit
from engine.application.job_state import (
    STAGE_TASK, JobPosition, fix_opened_attempt, fix_opened_position, is_full_job,
    load_position, record_position, stages_from_spec, without_release,
)
from engine.application.steps.open_job import copy_root, open_job
from engine.application.task_stage_runner import HandoffContext, run_task_stages
# Split out purely to keep this module under the repo's own line budget
# (see repair_journal's own docstring) -- aliased back to their
# historical bare names since every call site below still uses them.
from engine.application.repair_journal import (
    already_opened_new_attempt as _already_opened_new_attempt,
    append_fix_event as _append_fix_event,
    current_opening_marker_seq as _current_opening_marker_seq,
    find_unfinished_fix_cycle as _find_unfinished_fix_cycle,
    latest_fix_event_after as _latest_fix_event_after,
    latest_fix_event_seq_after as _latest_fix_event_seq_after,
)

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger


# Historical private names: engine.application.pipeline_resume also mirrors
# ARCHIVE_MARKER_FILES under its own name (see that module's own
# docstring); this module's own _overlay_archive_into_builder_copy/
# original_task keep calling them by their historical bare name.
_ARCHIVE_MARKER_FILES = ARCHIVE_MARKER_FILES
_requests_for_attempt = requests_for_attempt


@dataclass(frozen=True)
class FixResult:
    old_attempt_id: str
    new_attempt_id: str
    job_name: str
    task: str
    outcome: orbit.JobOutcome
    # True when the repaired task belongs to a full-orbit job, whose
    # remaining work engine.application.job_state.resume_after_fix continues.
    full_job: bool = False


FakesFor = Callable[[str], Fakes]

# fix()'s own entry-check refusal reason (결함 41, plans/-
# gongbang-entry-unify.md §B): "<this prefix><rounds already spent>".
# Never a new number of its own -- the cap itself is JobPosition.
# max_fix_rounds (운영자 기준 "최대 2바퀴", unchanged, job_state.py's own
# existing default). tools/gb2lib.runner.cmd_fix matches on this exact
# prefix to report a needs_owner shape instead of a bare refusal.
FIX_ROUNDS_EXHAUSTED_PREFIX = "fix_rounds_exhausted:"

# fix()'s own SECOND entry-check refusal reason (a past incident, 반장 지시
#: "<this prefix><rounds already spent>" -- a review that
# comes back naming the EXACT SAME finding (file+severity+note,
# repair_target.review_findings_repeat) it already raised last round,
# meaning the round just spent changed nothing the reviewer cared
# about. Checked even when rounds_used has not yet reached max_rounds
# (FIX_ROUNDS_EXHAUSTED_PREFIX's own gate) -- no amount of headroom left
# in the cap makes restating an unresolved finding worth another round.
# tools/gb2lib.runner.cmd_fix matches on this exact prefix the same way
# it already matches FIX_ROUNDS_EXHAUSTED_PREFIX.
NO_PROGRESS_PREFIX = "no_progress:"

# fix()'s own THIRD entry-check refusal reason (결함 66, same root as
# 결함 58/65: an instruction built in one place drifting from the
# boundary enforced in another): "<this prefix><json list of the
# distinct denied paths>" -- the attempt about to be replaced already
# hit OUTSIDE_ALLOWED_PATH_STUCK_THRESHOLD or more live-gate denials
# for reason "outside_allowed_path" (repair_target.
# outside_allowed_path_denials, reading the engine's own forgery-
# resistant gate_log.decision import -- never the raw worker-writable
# log). Reusing the SAME "refuse before spending another round, report
# a needs_owner shape instead of a bare error" shape
# FIX_ROUNDS_EXHAUSTED_PREFIX/NO_PROGRESS_PREFIX already use (no new
# state machine): a builder that keeps retrying a write the gate
# already denied is not making progress either, and the owner's actual
# next step is narrower than either of those two ("widen owns for this
# one path", not "give up" or "try a smaller instruction") -- see
# tools/gb2lib.runner.cmd_fix's own matching branch for the "gb2 fix
# <app> --also-own <path>" hint this prints.
OWNS_SCOPE_STUCK_PREFIX = "owns_scope_stuck:"

# ---------------------------------------------------------------------
# close the failed attempt, recover its archive
# ---------------------------------------------------------------------

def close_failed_attempt(config: EngineConfig, ledger: Ledger, fakes: Fakes,
                         project: str, attempt_id: str) -> Path:
    """Submit and run the real "close" request for `attempt_id` (the
    same request kind a normal successful job and `gb2 skip-ship` both
    use -- see engine.application.steps.close's module docstring: archive_tmp/
    archive_commit copy every changed path out BEFORE reset_to_head
    wipes the working tree back to HEAD). Returns the archive directory
    close just created (from the archive.done event it appends), so the
    caller can restore those files into the NEW attempt's builder copy.
    Raises FixupError if the close request does not end 'done' (e.g.
    "changed_after_archive") -- the project is left exactly as close
    left it (still OPEN/CLOSING, never force-advanced) for the owner to
    investigate; no new attempt is opened."""
    proc = Processor(config, ledger, fakes)
    proc.register_defaults()
    close_rid = submit(ledger, project, attempt_id, "close", {}, "owner")
    proc.run_once(project)
    request = ledger.request(close_rid)
    if request.status != "done":
        result = request.result if isinstance(request.result, dict) else {}
        reason = result.get("reason", request.status)
        raise FixupError(f"close_failed: {reason}")

    archive_path: Path | None = None
    for _seq, event in ledger.events(project, "archive.done"):
        if event.attempt_id == attempt_id:
            archive_path = Path(str(event.payload["path"]))
    if archive_path is None:
        raise FixupError("archive_missing: no archive.done event for the closed attempt")
    return archive_path


def _overlay_archive_into_builder_copy(config: EngineConfig, attempt_id: str,
                                       builder_role: str, archive_dir: Path) -> None:
    """Copy every archived file (the previous, failed attempt's own
    merged-but-uncommitted work) into the new attempt's builder copy,
    as plain filesystem writes -- no `git add`/`git commit` inside the
    copy. engine.application.steps.merge's own collect_copies finds candidate files
    via `git status` in the copy (same call engine.application.steps.open_job.
    dirty_paths makes for the real repo), so an untracked/modified file
    here is picked up exactly like any worker edit; and it compares
    their BYTES against base_head directly (never against the copy's
    own git history), so no commit inside the copy is needed for merge
    to see these as real changes. Paths close's own archive_tmp
    recorded as deleted (".deleted") are removed from the copy instead
    of being written -- a worker that deleted a file in the previous
    attempt must still see it gone when it resumes."""
    dest_root = copy_root(config, attempt_id, builder_role)

    modes: dict[str, object] = {}
    modes_file = archive_dir / ".modes"
    if modes_file.exists():
        try:
            modes = json.loads(modes_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
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
        if str(rel) in _ARCHIVE_MARKER_FILES:
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


# ---------------------------------------------------------------------
# top-level entry point
# ---------------------------------------------------------------------

def _full_job_task_config(config: EngineConfig, project: str, job_name: str) -> EngineConfig:
    """engine.application.pipeline.task_attempt_config for this job's plan; without a
    loadable plan, only the per-task release is removed."""
    from engine import full_orbit
    from engine.domain.plan import PlanError, load_plan

    try:
        plan = load_plan(config.projects[project].repo, job_name)
    except PlanError:
        return without_release(config, project)
    return full_orbit.task_attempt_config(config, project, job_name, plan)


def _widen_owns(owns: list, extra_owns: tuple[str, ...]) -> list[str]:
    widened = list(owns)
    for path in extra_owns:
        if path not in widened:
            widened.append(path)
    return widened


def _also_own_for_task(ledger: Ledger, project: str, job_name: str) -> tuple[str, ...]:
    """Every path a past `gb2 fix <app> --also-own <path>` call granted
    to the job's CURRENT task position -- () if the position carries no
    task_id at all (a plain single-task job) or nothing was ever
    granted."""
    pos = load_position(ledger, project, job_name)
    if pos is None or pos.task_id is None:
        return ()
    return extended_owns_for_task(ledger, project, job_name, pos.task_id)


def _fix_spec(pos: "JobPosition | None", old_attempt_id: str, config: EngineConfig,
              new_attempt_id: str, task: str,
              extra_owns: tuple[str, ...] = ()) -> list[dict] | None:
    """The replaced task's own recorded stage spec, for the replacement
    attempt, with the composed fix task given to the builder and the
    reviewer -- and, when `extra_owns` is non-empty (결함 26: an owner's
    `--also-own` grant for this task, see repair_target.add_also_own,
    engine.application.repair_target module docstring), that task's own
    run_station "owns" and merge copies' own "owns" widened by exactly
    those paths -- never plan.json's `owns` itself, and never any
    OTHER task's. This is the one place both the live gate (run_station
    payload's "owns", read by engine.application.steps.worker_gate_log.
    task_owns) and merge's own re-check (engine.application.steps.merge.
    _owner_prefixes' "owns") actually see a widened allowance: the SAME
    payload field each already trusted before this grant existed. None
    when the replaced attempt has no recorded spec."""
    if pos is None or pos.attempt_id != old_attempt_id:
        return None
    spec = pos.get("stages")
    if not isinstance(spec, list):
        return None
    stages = stages_from_spec(spec, config, new_attempt_id, task_text=task)
    out: list[dict] = []
    for stage in stages:
        payload = stage.payload
        if stage.kind == "review":
            payload = {**payload, "task": task}
        elif stage.kind == "run_station" and extra_owns and isinstance(payload.get("owns"), list):
            payload = {**payload, "owns": _widen_owns(payload["owns"], extra_owns)}
        elif stage.kind == "merge" and extra_owns:
            payload = {**payload, "copies": [
                ({**copy, "owns": _widen_owns(copy["owns"], extra_owns)}
                 if isinstance(copy.get("owns"), list) else copy)
                for copy in payload.get("copies", [])
            ]}
        out.append({"kind": stage.kind, "payload": payload})
    return out


def _fix_handoff(config: EngineConfig, project: str, job_name: str,
                 pos: object) -> "HandoffContext | None":
    """The same later-task hand-off the first run and resume use (결함 31),
    for the `gb2 fix` path too -- 결함 40 (sample-app T7,: a
    request for a path a later task owns stopped the fix for the owner.
    None for a plain single-task job (no per-task `owns` to hand off
    against -- see is_full_job) or a plan that cannot be loaded; either
    way run_task_stages then runs with no handoff filter at all (the
    notes-never-block rule still applies -- task_pipeline drops those
    unconditionally)."""
    task_id = getattr(pos, "task_id", None)
    if not is_full_job(pos) or not task_id:
        return None
    from engine.domain.plan import PlanError, load_plan
    try:
        plan = load_plan(config.projects[project].repo, job_name)
    except PlanError:
        return None
    later = tuple(t for t in (getattr(pos, "remaining_task_ids", None) or ()) if t != task_id)
    return HandoffContext(plan, task_id, later, None)


def fix(config: EngineConfig, ledger: Ledger, project: str, extra: str,
       fakes_for: FakesFor,
       on_progress: orbit.ProgressCallback | None = None,
       processor_factory: orbit.ProcessorFactory | None = None,
       past_cap: bool = False) -> FixResult:
    """Continue the project's current (failed-at-review-or-bench)
    attempt instead of starting a fresh job -- or, if a PRIOR call to
    this function was itself interrupted partway through its own
    close-old/open-new/restore/submit transition, resume exactly that
    instead (see _find_unfinished_fix_cycle and the module docstring);
    `extra` is then ignored in favor of whatever was journaled the first
    time, since the composed task it would have fed into is already
    fixed by "fix.started". `fakes_for(attempt_id)` builds the Fakes a
    Processor needs for that specific attempt_id (mirrors tools/gb2's
    own `_build_fakes(config, app, attempt_id, None)` -- the installer's
    marker_dir is attempt-scoped, so closing the old attempt and running
    the new one each need their own). `processor_factory`, like
    orbit.continue_job's own parameter of the same name, is forwarded
    only to the NEW attempt's stage list -- closing the old attempt
    always uses the real default steps (every caller, live or test,
    needs the real close behavior; nothing stubs "close"). Raises
    FixupError (no ledger write beyond whatever close_failed_attempt
    itself already committed, and the "fix.started" this function
    itself appended first) if there is nothing eligible to fix, or if
    closing the failed attempt does not end 'done'.

    Entry check (결함 41, plans/-gongbang-entry-unify.md §B):
    only on a FRESH cycle (never while resuming an already-journaled
    one -- `cycle` below is already past this point, and re-checking it
    on resume would refuse a transition that already started) --
    before anything is appended, if fix_rounds_used (repair_target's
    own pure count of this job's earlier attempts, since its last
    commit, that closed on a failed review/bench request) is already at
    or past the position's own `max_fix_rounds` (default 2, unchanged --
    no new number), raise FixupError(f"{FIX_ROUNDS_EXHAUSTED_PREFIX}
    {rounds_used}") with NOTHING recorded, UNLESS `past_cap` is True --
    then one more round is allowed and a "fix.past_cap" ledger event
    records it (tools/gb2lib.runner.cmd_fix's own `--past-cap` flag)."""
    cycle = _find_unfinished_fix_cycle(ledger, project)
    if cycle is not None:
        started_seq, payload = cycle
        old_attempt_id = str(payload["old_attempt_id"])
        job_name = str(payload["job_name"])
        task = str(payload["task"])
        builder_role = str(payload["builder_role"])
        reviewer_role = str(payload["reviewer_role"])
        archive_source = payload.get("archive_source")
    else:
        target = find_fix_target(ledger, project)
        old_attempt_id = target.attempt_id
        job_name = target.job_name
        cap_pos = load_position(ledger, project, job_name)
        # JobPosition.max_fix_rounds' own default (2) when there is no
        # recorded position yet (a plain `gb2 run` single-task job's
        # first-ever fix has none) -- never a second number invented here.
        max_rounds = cap_pos.max_fix_rounds if cap_pos is not None else 2
        rounds_used = fix_rounds_used(ledger, project, job_name, old_attempt_id)
        # 결함 66: checked BEFORE both the no-progress and the round-cap
        # check below, and NEVER bypassed by `past_cap` (unlike those
        # two) -- widening owns is the only thing that changes the
        # outcome of another round once the live gate has already
        # denied this many writes for the SAME reason on the attempt
        # about to be replaced; `--past-cap` means "I accept the risk
        # of another round", not "I accept paying for a round the gate
        # will deny the same way again".
        denied_paths = outside_allowed_path_denials(ledger, project, old_attempt_id)
        if len(denied_paths) >= OUTSIDE_ALLOWED_PATH_STUCK_THRESHOLD:
            distinct_paths = sorted(set(denied_paths))
            raise FixupError(f"{OWNS_SCOPE_STUCK_PREFIX}{json.dumps(distinct_paths)}")
        # a past incident (반장 지시,: checked BEFORE the cap itself,
        # so it also catches a job that has not yet reached max_fix_rounds
        # -- only for "review" (build-task review; engine.application.
        # pipeline_resume's own early-station reopen runs the identical
        # check for a plan review), never "bench" (a failing test's own
        # tail legitimately repeats while a fix is still in progress,
        # that is not "no progress").
        if (target.failing_kind == "review" and target.review_findings
                and not past_cap):
            prior_findings = previous_review_findings(ledger, project, job_name,
                                                       old_attempt_id)
            if review_findings_repeat(target.review_findings, prior_findings):
                raise FixupError(f"{NO_PROGRESS_PREFIX}{rounds_used}")
        if rounds_used >= max_rounds:
            if not past_cap:
                raise FixupError(f"{FIX_ROUNDS_EXHAUSTED_PREFIX}{rounds_used}")
            _append_fix_event(ledger, project, "fix.past_cap", old_attempt_id,
                              {"job_name": job_name, "rounds_used": rounds_used,
                               "max_fix_rounds": max_rounds})
        task = compose_fix_task(target, extra, _also_own_for_task(ledger, project, job_name))
        # 결함 48: compose_fix_task is plain text -- a fix round's own
        # builder instructions never got a "## 주입된 자료" block at
        # all (never mind a stale one) -- see inject_context_into_fix_
        # task's own docstring (repair_target.py).
        task = inject_context_into_fix_task(
            config, ledger, project, job_name, task,
            is_full_job(load_position(ledger, project, job_name)))
        builder_role = target.builder_role
        reviewer_role = target.reviewer_role
        archive_source = str(target.carried_archive) if target.carried_archive is not None else None
        started_seq = _append_fix_event(ledger, project, "fix.started", old_attempt_id, {
            "job_name": job_name, "old_attempt_id": old_attempt_id, "task": task,
            "extra": extra, "builder_role": builder_role, "reviewer_role": reviewer_role,
            "archive_source": archive_source,
        })

    pos = load_position(ledger, project, job_name)
    full_job = is_full_job(pos)
    if full_job:
        # The same task-attempt config the task's own first run used
        # (no per-task release, a UI plan's screens/ allowance).
        config = _full_job_task_config(config, project, job_name)

    # Cancel any publish request still queued for the attempt being
    # replaced BEFORE close_failed_attempt below runs processor.run_once
    # (which drains the WHOLE project queue, including that request) --
    # a fix must never let a stale, now-superseded publish go out just
    # because closing the old attempt happened to touch the queue. Safe
    # to redo on a resume too -- cancelling an already-cancelled (or
    # never-existed) publish request is a no-op.
    release_mod.cancel_pending_publish(ledger, project, old_attempt_id, "superseded_by_fix")

    closed = _latest_fix_event_after(ledger, project, "fix.closed_old", started_seq)
    if closed is None:
        old_fakes = fakes_for(old_attempt_id)
        own_archive_dir = close_failed_attempt(config, ledger, old_fakes, project, old_attempt_id)
        _append_fix_event(ledger, project, "fix.closed_old", old_attempt_id,
                          {"archive_dir": str(own_archive_dir)})
    else:
        own_archive_dir = Path(str(closed.payload["archive_dir"]))
    # carried_archive (see find_fix_target case (b)) means THIS attempt's
    # own merge never completed, so own_archive_dir is necessarily empty
    # -- the earlier attempt's own archive, found by find_fix_target, is
    # what actually holds merged work to continue from.
    archive_dir = Path(str(archive_source)) if archive_source is not None else own_archive_dir

    new_attempt_id = fix_opened_attempt(ledger, project, old_attempt_id, started_seq)
    if new_attempt_id is None:
        # This cycle's own fix id for _already_opened_new_attempt's
        # identity check -- journaled here, immediately before the
        # open_job() call below, rather than reusing "fix.started"'s own
        # seq, so that an unrelated `gb2 change` reopening this same
        # job_name at any point between "fix.started"/"fix.closed_old"
        # and this cycle's own open_job() attempt (an arbitrarily long
        # real-time gap a crash can leave open) can never be mistaken
        # for this cycle's own result (see that function's docstring).
        # Reused as-is on a retry within the SAME cycle -- UNLESS the
        # attempt it was written for is already known to have failed
        # (see "fix.opening_failed" below), in which case it is as
        # worthless as never having been written at all, and a fresh one
        # must replace it before the next open_job() attempt.
        opening_seq = _current_opening_marker_seq(ledger, project, started_seq)
        if opening_seq is None:
            opening_seq = _append_fix_event(ledger, project, "fix.opening_new", old_attempt_id,
                                             {"job_name": job_name})
        # A prior call may have already run open_job() successfully and
        # crashed before this journal write -- detect that already-open
        # attempt instead of calling open_job() again (which would just
        # raise: the project is OPEN, not CLOSED, by then).
        new_attempt_id = _already_opened_new_attempt(
            ledger, project, job_name, old_attempt_id, opening_seq)
        if new_attempt_id is None:
            try:
                # Only the builder role needs a fresh copy -- the reviewer
                # runs against its own exported snapshot (engine.application.steps.
                # review._export_review_snapshot), never a worker copy at
                # all, so making one for it here would just be an unused
                # checkout cost on every fix.
                new_attempt_id = open_job(
                    config, ledger, project, job_name, "full", (builder_role,))
            except Exception:
                # This specific call is now conclusively known to have
                # produced no job.opened of its own (open_job's own first
                # ledger write is that event, and it only raises before or
                # instead of making it -- never after) -- the marker above
                # must never again be treated as a valid bound on a later
                # retry, or an unrelated future job.opened for this same
                # job_name (a legitimate `gb2 change` once this project is
                # genuinely CLOSED again) would wrongly pass this cycle's
                # own identity check.
                _append_fix_event(ledger, project, "fix.opening_failed", old_attempt_id, {})
                raise
        # One event names the replacement attempt AND moves the job's
        # position onto it, so no crash can leave the position on the
        # closed attempt while the replacement is already open.
        faults.flow_boundary("fixup.fix.opened_new")
        extra_owns = _also_own_for_task(ledger, project, job_name)
        record_position(ledger, project, fix_opened_position(
            pos, job_name, old_attempt_id, new_attempt_id, task,
            stages=_fix_spec(pos, old_attempt_id, config, new_attempt_id, task, extra_owns)))
        pos = load_position(ledger, project, job_name)
    # Otherwise the replacement is already open and recorded: NEVER call
    # open_job again, which would fork a second, orphaned attempt.

    restored = _latest_fix_event_after(ledger, project, "fix.restored", started_seq)
    if restored is None:
        _overlay_archive_into_builder_copy(config, new_attempt_id, builder_role, archive_dir)
        _append_fix_event(ledger, project, "fix.restored", old_attempt_id, {})

    # A full job's task replays its own recorded stage spec (owns, tools,
    # review) with the composed task; every recovery of this fix replays
    # the same spec from the position. A plain single-task job has none.
    spec = pos.get("stages") if pos is not None and pos.attempt_id == new_attempt_id else None
    if isinstance(spec, list):
        stages = stages_from_spec(spec, config, new_attempt_id)
    else:
        stages = orbit.fix_stages(config, new_attempt_id, builder_role, reviewer_role, task,
                                  job_name)

    # Submitted here, deliberately OUTSIDE orbit.continue_job, so this
    # journal can record the request_id the moment it exists -- submit()
    # is itself idempotent (same project/attempt/kind/payload always
    # hashes to the same request_id, see engine.application.processor.submit's own
    # docstring), so continue_job's own submit() call for this same
    # first stage, right after, is simply a no-op lookup of what was
    # just created here.
    submitted = _latest_fix_event_after(ledger, project, "fix.submitted", started_seq)
    if submitted is None:
        request_id = submit(ledger, project, new_attempt_id, stages[0].kind,
                            stages[0].payload, "owner")
        _append_fix_event(ledger, project, "fix.submitted", old_attempt_id,
                          {"request_id": request_id})

    # This transition itself is done as of here -- whatever continue_job
    # does next (run the rest of this stage list, including any later
    # review/bench/... failure) is the job's own ordinary, independently
    # resumable machinery, not something this journal needs to cover.
    finished = _latest_fix_event_after(ledger, project, "fix.finished", started_seq)
    if finished is None:
        _append_fix_event(ledger, project, "fix.finished", old_attempt_id, {})

    new_fakes = fakes_for(new_attempt_id)
    # The repaired task stopped for the owner (e.g. another install) is
    # recorded on `pos` ONLY when it is still the project's current
    # full-job task position on this exact replacement attempt -- a
    # plain single-task job, or a position already moved elsewhere,
    # has nothing of this shape to record a stop on.
    stop_pos = (pos if full_job and pos is not None and pos.stage == STAGE_TASK
               and pos.attempt_id == new_attempt_id else None)
    outcome = run_task_stages(
        config, ledger, new_fakes, project, job_name, new_attempt_id, stages,
        on_progress=on_progress, processor_factory=processor_factory,
        handoff=_fix_handoff(config, project, job_name, pos),
        task_pos=stop_pos, flow_boundary_label="fixup.fix.task_stop",
    )
    return FixResult(
        old_attempt_id=old_attempt_id, new_attempt_id=new_attempt_id,
        job_name=job_name, task=task, outcome=outcome, full_job=full_job,
    )


# ---------------------------------------------------------------------
# --also-own: `gb2 fix <app> --also-own <path>` (repeatable), run
# BEFORE fix() itself -- see repair_target's own module-level comment
# for 결함 26, the real incident this exists for.
# ---------------------------------------------------------------------

def add_also_own(config: EngineConfig, ledger: Ledger, project: str,
                 paths: tuple[str, ...]) -> tuple[str, ...]:
    """Owner-only: widen the write prefixes of the task CURRENTLY being
    fixed (the project's current OPEN, active, fixable attempt -- the
    exact eligibility find_fix_target itself requires) by `paths`, on
    top of plan.json's own `owns` for that task -- plan.json itself is
    never edited (frozen evidence). The grant is recorded as its own
    ledger event (repair_target.record_owns_extension) and only takes
    effect the next time fix() opens a NEW replacement attempt for this
    task (_fix_spec folds it into that attempt's own run_station/merge
    "owns") -- it never reaches back into an attempt already open.

    Raises FixupError, with NOTHING recorded, if:
      - there is no current attempt eligible to fix at all (whatever
        find_fix_target itself refuses for);
      - the current attempt belongs to a plain single-task `gb2 run`
        job, or a full job whose plan.json cannot be loaded, or whose
        plan no longer lists this task's own id -- none of these have
        a per-task `owns` to widen in the first place ("no task
        currently being fixed");
      - `paths` is empty, or validate_also_own refuses any one entry
        (a path another task already owns, a path outside the repo/
        absolute/".."/hidden component, or an engine record path --
        see that function's own docstring).

    Returns every path now granted to that task (this call's own, plus
    any earlier one)."""
    if not paths:
        raise FixupError("also_own_no_paths")
    target = find_fix_target(ledger, project)
    pos = load_position(ledger, project, target.job_name)
    if pos is None or pos.attempt_id != target.attempt_id or pos.task_id is None:
        raise FixupError("no_task_being_fixed")
    from engine.domain.plan import PlanError, load_plan

    try:
        plan = load_plan(config.projects[project].repo, target.job_name)
    except PlanError:
        raise FixupError("no_task_being_fixed") from None
    if not any(t.id == pos.task_id for t in plan.tasks):
        raise FixupError("no_task_being_fixed")
    # a review finding: pass the CURRENT remaining_task_ids (this task
    # plus every task still to run) as `active_task_ids`, exactly like
    # repair_approval.approve_pending_edit already does -- a FINISHED
    # task's plan.json `owns` must not block a grant any more than its
    # EXTRA --also-own grants already don't (결함 34/38).
    normalized = validate_also_own(
        ledger, project, target.job_name, plan, pos.task_id, paths,
        tuple(pos.remaining_task_ids or ()),
        station_prefixes=tuple(config.station_owners.get("build", {}).get(target.builder_role, ())))
    record_owns_extension(ledger, project, target.job_name, pos.task_id, target.attempt_id,
                          normalized)
    return extended_owns_for_task(ledger, project, target.job_name, pos.task_id)


# approve_pending_edit (결함 28, sample-app T4, -- a pending
# `kind="edit"` request find_fix_target can never see, since this
# attempt never failed at all) lives in engine.application.repair_approval,
# a separate module purely to keep this one under the repo's 800-line
# budget (docs/standards/architecture.md) -- re-exported here so
# `fixup.approve_pending_edit` (tools/gb2lib/runner.py's own cmd_fix)
# needs no second import path.
from engine.application.repair_approval import approve_pending_edit  # noqa: E402

__all__ = [
    "FixupError", "FixTarget", "FixResult", "find_fix_target",
    "compose_fix_task", "close_failed_attempt", "fix", "original_task", "add_also_own",
    "approve_pending_edit", "FIX_ROUNDS_EXHAUSTED_PREFIX", "NO_PROGRESS_PREFIX",
    "OWNS_SCOPE_STUCK_PREFIX",
]


def original_task(ledger: Ledger, project: str, attempt_id: str) -> str:
    """The task text the attempt's run_station request was given ("" if
    there is none). Used wherever a later stage list is rebuilt without
    the caller holding the task, so the reviewer still sees the request."""
    for request in _requests_for_attempt(ledger, project, attempt_id):
        if request.kind == "run_station":
            return str(request.payload.get("task") or "")
    return ""
