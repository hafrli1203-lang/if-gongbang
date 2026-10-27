"""`gb2 fix <app> --also-own <path>` for a pending `kind="edit"` request
(결함 28, sample-app T4,.

OWNS_SCOPE_REQUEST_RULE (결함 26, engine.domain.policy) tells a build
task to REPORT -- never silently skip or silently write -- a path it
needs outside its own `owns`, as a `{"kind": "edit", "path": ...,
"reason": ...}` entry in its `requests`. That stop lands the job at
job_state.STAGE_TASK_OWNER (needs_owner, reason=None, see
engine.application.task_pipeline's own module docstring), never a FAILED
review/bench request -- the attempt itself stays `active` the whole
time, simply waiting on the owner.

engine.application.repair.find_fix_target (and so add_also_own/fix(), the
classic `--also-own` path) can only ever see a FAILED review/bench
request on an OPEN/active attempt -- a 결함 28 stop satisfies NEITHER,
so find_fix_target raises "no_failed_attempt" for it even though a real,
pending owner request exists. tools/gb2lib/runner.py's cmd_fix tries
approve_pending_edit FIRST and only falls back to the classic
add_also_own+fix() flow when it raises FixupError(NOT_TASK_OWNER_EDIT_STOP)
-- the one reason code meaning "this isn't case 28 at all".

This is its own module (not folded into engine.application.repair) purely to
keep that module under the repo's 800-line budget (docs/standards/
architecture.md) -- repair.py re-exports `approve_pending_edit` so
`fixup.approve_pending_edit` needs no second import path."""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from engine.application import task_pipeline as orbit
from engine.config import EngineConfig
from engine.application.repair_target import (
    FixupError, is_owns_scope_request, record_owns_extension, validate_also_own,
)
from engine.application.processor import Fakes
from engine.application.job_state import (
    STAGE_TASK_OWNER, hand_off_later_task_requests, load_position, outstanding_requests, resume_job,
)

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger
    from engine.application.pipeline import FullOutcome

FakesFor = Callable[[str], Fakes]

# The one reason code that means "the project's current attempt is not
# a 결함 28 case at all" -- cmd_fix matches on exactly this string to
# decide whether to fall back to the classic add_also_own+fix() flow.
NOT_TASK_OWNER_EDIT_STOP = "not_task_owner_edit_stop"


def _job_name_for_current_attempt(ledger: "Ledger", project: str) -> tuple[str, str] | None:
    """(job_name, attempt_id) for the project's current OPEN, active
    attempt -- or None if there is no such attempt. Deliberately its own
    small lookup rather than reusing engine.application.repair_target.find_fix_target
    (whose eligibility -- a FAILED review/bench request -- is exactly
    what 결함 28's own attempt never has) or that module's own
    module-private `_job_name_for_attempt` (see its own docstring for
    why this codebase duplicates this exact lookup per module rather
    than sharing one copy -- engine.interface.cli and
    engine.application.release_certificate each already keep their
    own copy too). Returns None if there is no such attempt, or no
    job.opened event can be found for it."""
    row = ledger.project(project)
    if row.state != "OPEN" or row.current_attempt_id is None:
        return None
    attempt_id = row.current_attempt_id
    if ledger.attempt(attempt_id).status != "active":
        return None
    job_name: str | None = None
    for _seq, event in ledger.events(project, "job.opened"):
        if event.payload.get("attempt_id") == attempt_id:
            job_name = str(event.payload.get("job_name"))
    if job_name is None:
        return None
    return job_name, attempt_id


def approve_pending_edit(config: EngineConfig, ledger: "Ledger", project: str,
                         paths: tuple[str, ...], fakes_for: FakesFor, *,
                         on_progress: orbit.ProgressCallback | None = None,
                         processor_factory: orbit.ProcessorFactory | None = None,
                         ) -> "FullOutcome":
    """Owner-only. Raises FixupError(NOT_TASK_OWNER_EDIT_STOP) -- nothing
    recorded -- unless the project's current attempt is OPEN/active AND
    its own position is stopped at STAGE_TASK_OWNER on THAT attempt with
    at least one outstanding `kind="edit"` request
    (job_state.outstanding_requests, repair_target.EDIT_REQUEST_KIND).
    Raises FixupError("also_own_not_requested:<path>") -- nothing
    recorded -- for any `paths` entry that is not one of those
    outstanding edit requests' own `path` (an owner can only approve
    what was actually asked for here, never widen the task into
    anything else via this entry point). Raises
    FixupError("no_task_being_fixed") if the job's plan.json cannot be
    loaded or no longer lists this task's own id. Otherwise defers to
    validate_also_own for the same syntax/engine-record/other-task-owns
    refusals engine.application.repair.add_also_own itself raises.

    On success: records task.owns_extended (repair_target.
    record_owns_extension, the SAME event classic `--also-own` uses, so
    job_state._edit_request_fulfilled and
    engine.application.pipeline_resume_tasks.rerun_task_owner_stop's own
    widen_stage_owns call both see it the same way), then resumes the
    SAME attempt through job_state.resume_job -- never opens a new one,
    since nothing here ever failed. The approved paths stop appearing in
    a later `pending_owner_requests` call (the P5 single computation,
    job_state.outstanding_requests's own "edit" branch) -- there is no
    separate "fulfilled" ledger to fall out of sync with it."""
    if not paths:
        raise FixupError("also_own_no_paths")
    found = _job_name_for_current_attempt(ledger, project)
    if found is None:
        raise FixupError(NOT_TASK_OWNER_EDIT_STOP)
    job_name, attempt_id = found
    pos = load_position(ledger, project, job_name)
    if pos is None or pos.stage != STAGE_TASK_OWNER or pos.attempt_id != attempt_id \
            or pos.task_id is None:
        raise FixupError(NOT_TASK_OWNER_EDIT_STOP)
    pending_edit_paths = {
        str(r["path"]) for r in outstanding_requests(ledger, project, pos, repo=None)
        if is_owns_scope_request(r)
    }
    if not pending_edit_paths:
        raise FixupError(NOT_TASK_OWNER_EDIT_STOP)
    for path in paths:
        if path not in pending_edit_paths:
            raise FixupError(f"also_own_not_requested:{path}")

    from engine.domain.plan import PlanError, load_plan

    try:
        plan = load_plan(config.projects[project].repo, job_name)
    except PlanError:
        raise FixupError("no_task_being_fixed") from None
    if not any(t.id == pos.task_id for t in plan.tasks):
        raise FixupError("no_task_being_fixed")
    normalized = validate_also_own(
        ledger, project, job_name, plan, pos.task_id, paths, tuple(pos.remaining_task_ids or ()),
        station_prefixes=tuple(config.station_owners.get("build", {}).get("builder", ())))
    record_owns_extension(ledger, project, job_name, pos.task_id, attempt_id, normalized)
    hand_off_later_task_requests(ledger, project, job_name, pos, plan)

    return resume_job(config, ledger, fakes_for, project, job_name,
                      on_progress=on_progress, processor_factory=processor_factory)


__all__ = ["approve_pending_edit", "NOT_TASK_OWNER_EDIT_STOP"]
