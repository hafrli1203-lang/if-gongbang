"""The ONE place a build task's stage list (run_station -> merge ->
review -> bench -> commit -> report -> close) is actually run.

Why this module exists (flow-map.md "같은 일을 하는 입구가 여럿인 곳"):
three different callers used to each build their own handoff-filter
and stop-recording logic around their own direct `orbit.continue_job`
call -- the first run of a task (engine.application.pipeline_tasks.
run_task_attempt), a resume of a stopped task (engine.application.
pipeline_resume_tasks.continue_task_stages), and `gb2 fix`'s own replay
on a fresh attempt (engine.application.repair.fix's own
`_fix_request_filter`). Because the two concerns -- filtering an
owns-scope report to a later task instead of stopping the job (결함
31), and recording where the attempt stopped for the owner -- lived at
each call site instead of in one function, a fix landed at only some
of the three doors and the same root cause reappeared under a new
number (결함 33b, 40). `run_task_stages` is now that one function; the
three doors call only it, never `orbit.continue_job` directly for a
build task's stages (tests/acceptance/test_t10_single_task_entry.py
enforces this structurally)."""
from __future__ import annotations

from dataclasses import dataclass

from engine.config import EngineConfig
from engine.application.job_state import JobPosition
from engine.application.processor import Fakes
from engine.domain import faults
from engine.domain.plan import Plan
from engine.domain.policy import NOTE_REQUEST_KIND
from engine.application.repair_target import (
    is_owns_scope_request, owning_later_task, record_request_handoff,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger
    # Type-only, like engine.application.pipeline_stages's own `orbit`
    # import -- a module-level import here reopens the exact same
    # steps/__init__ -> review -> repair -> task_stage_runner cycle this
    # module is itself one leg of (tests/unit/test_standalone_imports.py).
    # `run_task_stages` below imports it locally at its one call site.
    from engine.application import task_pipeline as orbit


@dataclass(frozen=True)
class HandoffContext:
    """Everything handoff_request_filter needs to decide whether an
    owns-scope report should be routed to a later task instead of
    stopping the job -- bundled so every caller of run_task_stages
    passes exactly one optional argument instead of four positional
    ones. `notes`, when given, is mutated with any request handed off
    (see handoff_request_filter) so the caller can fold it into that
    later task's own instructions once its turn comes; left out (None)
    when the caller (a resume, or `gb2 fix`) has nowhere to carry that
    forward to."""
    plan: Plan
    task_id: str
    later_task_ids: tuple[str, ...]
    notes: dict[str, list[dict[str, object]]] | None = None


def handoff_request_filter(ledger: "Ledger", project: str, job_name: str, plan: Plan,
                            task_id: str, attempt_id: str, later_task_ids: tuple[str, ...],
                            handoff_notes: dict[str, list[dict[str, object]]] | None,
                            ) -> orbit.RequestFilter:
    """결함 31 (sample-app T4,: an owns-scope report
    (repair_target.is_owns_scope_request) whose own "path" is already
    listed in a LATER, not-yet-run task's own `owns` (`later_task_ids`,
    plan.tasks order) never needs to stop this job for the owner at
    all -- the plan already intends that later task to touch the path.
    Routed instead: record_request_handoff (the ledger's own permanent
    record) and, when `handoff_notes` is given, append the request to
    that task id's own list so a later caller can fold handoff_note_text
    into its builder instructions once its own turn comes. Every OTHER
    request (not an owns-scope report, or one no later task's `owns`
    covers) passes through unchanged -- still stops the job exactly as
    before."""
    def _filter(requests: list[dict[str, object]]) -> list[dict[str, object]]:
        still_pending: list[dict[str, object]] = []
        for request in requests:
            if isinstance(request, dict) and request.get("kind") == NOTE_REQUEST_KIND:
                continue  # informational, never stops the job (결함 33)
            if not isinstance(request, dict) or not is_owns_scope_request(request):
                still_pending.append(request)
                continue
            path = str(request.get("path"))
            target = owning_later_task(plan, later_task_ids, path)
            if target is None:
                still_pending.append(request)
                continue
            record_request_handoff(ledger, project, job_name, attempt_id, task_id, target,
                                   request)
            if handoff_notes is not None:
                handoff_notes.setdefault(target, []).append(request)
        return still_pending

    return _filter


def run_task_stages(config: EngineConfig, ledger: "Ledger", fakes: Fakes, project: str,
                     job_name: str, attempt_id: str, stages: list[orbit.Stage], *,
                     on_progress: orbit.ProgressCallback | None,
                     processor_factory: "orbit.ProcessorFactory | None",
                     handoff: HandoffContext | None = None,
                     task_pos: JobPosition | None = None,
                     stop_extra: dict[str, object] | None = None,
                     flow_boundary_label: str | None = None) -> orbit.JobOutcome:
    """Run `stages` (a build task's attempt) through `orbit.continue_job`
    with the handoff filter `handoff` describes (none, when `handoff`
    is None -- a plain single-task job has no later task to hand off
    to), then, only if the attempt stopped for the owner AND the caller
    gave a `task_pos` to record it on, record that stop
    (engine.application.pipeline_stages.record_task_stop) -- moving the
    task position to STAGE_TASK_OWNER with the raw proposals a later
    resume's gate checks are fulfilled first.

    `task_pos` being None is how a caller opts OUT of recording a stop
    here at all (e.g. `gb2 fix` only records one when the repaired
    attempt is still the project's current full-job task position --
    see engine.application.repair.fix). `flow_boundary_label`, when
    given, is recorded (engine.domain.faults.flow_boundary) immediately
    before the stop is -- the same crash-injection point `gb2 fix`
    always marked at this spot, kept here so moving the call does not
    lose that crash coverage."""
    from engine.application import task_pipeline as orbit
    request_filter = None
    if handoff is not None:
        request_filter = handoff_request_filter(
            ledger, project, job_name, handoff.plan, handoff.task_id, attempt_id,
            handoff.later_task_ids, handoff.notes)
    outcome = orbit.continue_job(config, ledger, fakes, project, attempt_id, stages,
                                 processor_factory=processor_factory, on_progress=on_progress,
                                 request_filter=request_filter)
    if task_pos is not None and stops_for_owner(outcome):
        if flow_boundary_label is not None:
            faults.flow_boundary(flow_boundary_label)
        record_task_stop(ledger, project, task_pos, outcome, **(stop_extra or {}))
    return outcome


# Deliberately placed here, after run_task_stages is already defined
# above (the only place in this module that calls either name), rather
# than with the other imports at the top of the file:
# engine.application.pipeline_stages's own import chain (via
# engine.application.steps.open_job -> steps/__init__ -> review ->
# repair) imports `HandoffContext` and `run_task_stages` back from THIS
# module before this module has finished executing -- moving this
# import below run_task_stages's own definition means that re-entry
# always finds everything it needs already bound
# (tests/unit/test_standalone_imports.py).
from engine.application.pipeline_stages import record_task_stop, stops_for_owner  # noqa: E402

__all__ = ["HandoffContext", "handoff_request_filter", "run_task_stages"]
