"""Resume handling for a build task's own attempt (STAGE_TASK/
STAGE_TASK_OWNER): split out of engine.application.pipeline_resume purely to
keep that module under the repo's 800-line file budget (docs/standards/
architecture.md) -- these five functions are a single, self-contained
chain (resume_task/resume_task_owner -> rerun_task_on_new_attempt/
rerun_task_owner_stop -> continue_task_stages -> advance_past_task) with
no caller outside engine.application.pipeline_resume.RESUME_TABLE and no
caller of anything resume-specific that still lives there, so moving it
here needed no new dependency edge: everything it uses (Run,
resumed_plan, task_for, task_attempt_committed, max_fix_rounds,
unblock_internal_crash) already lives in engine.application.pipeline_base,
which both this module and pipeline_resume.py import from, never the
other way."""
from __future__ import annotations

from dataclasses import replace

from engine.application import task_pipeline as orbit
from engine.config import EngineConfig
from engine.application.pipeline_base import (
    BUILDER_ROLE, FullOutcome, Run, max_fix_rounds, resumed_plan, task_attempt_committed,
    task_for, unblock_internal_crash,
)
from engine.application.pipeline_stages import (
    record_task_stop, station_outcome_to_full, stops_for_owner,
)
from engine.application.pipeline_tasks import (
    handoff_request_filter,
    build_task_gate, edit_approved_followup_task_text, finish_from_tasks,
    install_followup_task_text, run_acceptance_and_handoff, run_task_attempt,
    task_attempt_config, task_stages,
)
from engine.application.repair_target import extended_owns_for_task, is_owns_scope_request
from engine.domain.plan import Plan
from engine.application.job_state import (
    JobPosition, hand_off_later_task_requests, outstanding_requests, record_position, stages_from_spec, stages_spec,
    task_stop_position, widen_stage_owns,
)
from engine.application.steps.open_job import copy_root


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


def resume_task(run: Run, pos: JobPosition) -> FullOutcome:
    """A task position with no stop recorded: the task crashed, or its
    attempt ended at an ordinary "failed". A closed attempt either
    committed (the task finished -- advance) or was closed without
    committing (abandoned, or closed for a repair that never opened its
    replacement -- rerun the task from scratch on a new attempt). An
    active attempt reruns its build on the same attempt; the idempotent
    request ids skip whatever already finished. unblock_internal_crash
    runs first (same reason resume_early_station runs it)."""
    stop, plan = resumed_plan(run, pos)
    if plan is None:
        return stop  # type: ignore[return-value]
    attempt_id = pos.attempt_id or ""
    if attempt_id:
        unblock_internal_crash(run.ledger, run.project, attempt_id)
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
      install is done and the already-changed files to read first, OR
      (every outstanding request was instead a `kind="edit"` request an
      owner approved via `--also-own` -- 결함 28, see
      engine.application.repair.approve_pending_edit) a note that the
      owner has widened this task's own owns.
    An owner request that is still outstanding stays needs_owner.

    `extra_owns` (every path a past `--also-own` call ever granted this
    task, repair_target.extended_owns_for_task) is folded into every
    replayed run_station/merge stage's own "owns" via
    engine.application.job_state.widen_stage_owns -- the SAME widening
    engine.application.repair._fix_spec applies when a FIX opens a brand-new
    attempt, reused here because this path never opens one: the owner's
    grant must still reach the live gate and merge's own re-check on
    THIS still-open attempt."""
    hand_off_later_task_requests(run.ledger, run.project, run.job, pos, plan)
    if outstanding_requests(run.ledger, run.project, pos, repo=run.repo):
        return FullOutcome("needs_owner", "owner_request", pos.attempt_id, None)
    attempt_id = pos.attempt_id or ""
    base_task_text = str(pos.get("base_task_text", pos.get("task_text", "")))
    task_text = str(pos.get("task_text", base_task_text))
    task = task_for(plan, pos.task_id or "", pos.fixing)
    run_config = task_attempt_config(run.config, run.project, run.job, plan)
    extra_owns = (
        extended_owns_for_task(run.ledger, run.project, run.job, pos.task_id)
        if pos.task_id else ()
    )
    spec = pos.get("stages")
    if not isinstance(spec, list):
        # A position recorded before stage specs were: rebuild the same list.
        spec = stages_spec(task_stages(run.config, run_config, run.ledger, run.project, run.job,
                                        plan, task, task_text, base_task_text, attempt_id))
    if crash:
        stages = widen_stage_owns(stages_from_spec(spec, run_config, attempt_id), extra_owns)
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
    elif pos.pending_requests and all(
        isinstance(r, dict) and is_owns_scope_request(r) for r in pos.pending_requests
    ):
        run_station_text = edit_approved_followup_task_text(task_text, extra_owns)
    else:
        build_copy = copy_root(run_config, attempt_id, BUILDER_ROLE)
        run_station_text = install_followup_task_text(task_text, build_copy)
    merge_retry_n = sum(1 for _seq, event in run.ledger.events(run.project, "merge.recorded")
                        if event.attempt_id == attempt_id)
    stages = widen_stage_owns(
        stages_from_spec(spec, run_config, attempt_id, task_text=run_station_text,
                         merge_retry_n=merge_retry_n),
        extra_owns,
    )
    return continue_task_stages(run, pos, plan, run_config, stages, task_text)


def continue_task_stages(run: Run, pos: JobPosition, plan: Plan, run_config: EngineConfig,
                          stages: list[orbit.Stage], task_text: str) -> FullOutcome:
    """Runs a resumed task's stages on its own attempt, records a new
    owner stop if it makes one, and otherwise moves past the task."""
    attempt_id = pos.attempt_id or ""
    # The same later-task hand-off the first run uses (결함 31) -- a resumed
    # task once re-stopped on a path T5 owns (sample-app T4, 결함 31b).
    later = tuple(t for t in (pos.remaining_task_ids or ()) if t != pos.task_id)
    request_filter = handoff_request_filter(
        run.ledger, run.project, run.job, plan, pos.task_id or "", attempt_id, later, None)
    outcome = orbit.continue_job(run_config, run.ledger, run.fakes_for(attempt_id), run.project,
                                 attempt_id, stages, processor_factory=run.processor_factory,
                                 on_progress=run.on_progress, request_filter=request_filter)
    if stops_for_owner(outcome):
        task_pos = replace(pos, extra={**{k: v for k, v in pos.extra.items()
                                          if k != "context_gap"}, "task_text": task_text})
        record_task_stop(run.ledger, run.project, task_pos, outcome)
    full = station_outcome_to_full(outcome, "build")
    if full is not None:
        return full
    return advance_past_task(run, pos, plan, attempt_id)


__all__ = [
    "advance_past_task", "resume_task", "rerun_task_on_new_attempt", "resume_task_owner",
    "rerun_task_owner_stop", "continue_task_stages",
]
