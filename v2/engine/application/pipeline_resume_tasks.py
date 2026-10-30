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
from pathlib import Path

from engine.application import task_pipeline as orbit
from engine.config import EngineConfig
from engine.application.owner_approval import PENDING_KIND_WHITELIST
from engine.application.pipeline_base import (
    BUILDER_ROLE, FullOutcome, Run, max_fix_rounds, resumed_plan, task_attempt_committed,
    task_for, unblock_internal_crash,
)
from engine.application.pipeline_stages import station_outcome_to_full
from engine.application.pipeline_tasks import (
    build_task_gate, declined_followup_note, edit_approved_followup_task_text, finish_from_tasks,
    install_followup_task_text, run_acceptance_and_handoff, run_task_attempt,
    task_attempt_config, task_stages,
)
from engine.application.task_stage_runner import HandoffContext, run_task_stages
from engine.application.repair_target import (
    declined_for_attempt, extended_owns_for_task, is_owns_scope_request,
)
from engine.domain.plan import Plan
from engine.application.job_state import (
    JobPosition, current_position_seq, hand_off_later_task_requests, outstanding_requests,
    record_position, stages_from_spec, stages_spec, task_stop_position, widen_stage_owns,
)
from engine.application.steps.open_job import copy_root


def sync_installed_manifests(repo: Path, build_copy: Path,
                             pending_requests: tuple[dict, ...]) -> None:
    """결함 69: copies each `pending_requests` entry whose kind is owner-
    approvable (install/scripts_change/test_config_change -- the SAME
    PENDING_KIND_WHITELIST job_state_requests.request_fulfilled already
    gates on) from `repo`'s own manifest path into this attempt's
    builder copy, byte for byte. `repo`'s manifest now carries the
    owner-approved change AND is committed to HEAD (engine.application.steps.
    install's own commit_manifest step) by the time a task-owner stop
    resumes here -- but `build_copy` was archived from an OLDER HEAD at
    job-open time (engine.application.steps.open_job._make_copies) and is never
    refreshed on its own. Without this, a builder resumed on the SAME
    attempt keeps reading its own stale copy of the manifest and
    reports the identical request again forever (마케팅 채팅 job
    gb2-change-1793123186). A request not in PENDING_KIND_WHITELIST
    (e.g. an `edit`/owns-scope report) has no manifest of its own and
    is skipped here, same as request_fulfilled's own whitelist check."""
    for request in pending_requests:
        if not isinstance(request, dict) or request.get("kind") not in PENDING_KIND_WHITELIST:
            continue
        manifest = request.get("manifest")
        if not isinstance(manifest, str) or not manifest:
            continue
        source = repo / manifest
        if not source.exists():
            continue
        target = build_copy / manifest
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())


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
    base_task_text = str(pos.get("base_task_text", pos.get("task_text", "")))
    # 결함 48: this opens a genuinely NEW attempt (the old one closed
    # without committing) -- same reasoning as rerun_task_owner_stop's
    # own non-crash continuations, a fresh context block belongs here
    # too, built from the clean `base_task_text` the position stored
    # (never the stale `task_text` the OLD attempt's own build baked
    # in). `attempt_id=None` here matches run_tasks' own first-run call
    # (this task's new attempt is not open yet at this point either).
    gate = build_task_gate(run.config, run.ledger, run.project, plan, base_task_text)
    task_text = gate.prompt(base_task_text)
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
    # 결함 48 (appX live #49/#50): every OTHER continuation below used
    # to replay `task_text` exactly as the position stored it -- the
    # "## 주입된 자료" appendix baked in back when this task's FIRST
    # attempt (or fix round) built its prompt, never looked at again.
    # A crash (above) genuinely IS a plain retry of that same submitted
    # request, so it is left alone; every other continuation here is a
    # new one (an install/edit approval, a context gap recheck, a
    # worker_stuck restart), so it gets a FRESH context block -- the
    # SAME build_task_gate() a first run uses, called again with THIS
    # attempt_id so context.injected is recorded against it (never left
    # pointing only at the long-closed original attempt). `base_task_text`
    # (never carrying an old appendix -- pipeline_stages.task_position
    # stores it separately, clean) is what gets a new one appended, not
    # the stale `task_text` -- so the old block is replaced, never
    # duplicated alongside a second one.
    gate = build_task_gate(run.config, run.ledger, run.project, plan, base_task_text, attempt_id)
    task_text = gate.prompt(base_task_text)
    if pos.get("context_gap"):
        if gate.unmet is not None:
            record_position(run.ledger, run.project, task_stop_position(
                pos, attempt_id, gate.unmet, (), context_gap=True))
            return FullOutcome("needs_owner", gate.unmet, attempt_id, None)
        run_station_text = task_text
    elif pos.stop_reason == "worker_stuck":
        run_station_text = task_text
    elif pos.pending_requests and any(
        isinstance(r, dict) and is_owns_scope_request(r) for r in pos.pending_requests
    ):
        declined = (declined_for_attempt(run.ledger, run.project, run.job, pos.task_id, attempt_id)
                    if pos.task_id else ())
        granted_text = edit_approved_followup_task_text(task_text, extra_owns) if extra_owns else task_text
        run_station_text = granted_text + declined_followup_note(declined)
        # a review finding: a mixed stop (e.g. install + edit together)
        # must still carry the install-done note too -- never just the
        # edit-approved one, which used to be the ONLY branch reached
        # once even one request in the stop was owns-scope.
        if any(
            isinstance(r, dict) and not is_owns_scope_request(r) for r in pos.pending_requests
        ):
            build_copy = copy_root(run_config, attempt_id, BUILDER_ROLE)
            sync_installed_manifests(run.repo, build_copy, pos.pending_requests)
            run_station_text = install_followup_task_text(run_station_text, build_copy)
    else:
        build_copy = copy_root(run_config, attempt_id, BUILDER_ROLE)
        sync_installed_manifests(run.repo, build_copy, pos.pending_requests)
        run_station_text = install_followup_task_text(task_text, build_copy)
    merge_retry_n = sum(1 for _seq, event in run.ledger.events(run.project, "merge.recorded")
                        if event.attempt_id == attempt_id)
    # 결함 61 (and its own follow-up, 결함 F1): how many run_station
    # requests this attempt had already submitted by the time THIS
    # stop (the one this resume is answering) was recorded -- busts
    # this resume's own payload hash so it never collides with an
    # earlier resume's followup text that happened to come out
    # byte-identical (same unchanged dirty_paths, same base_task_text --
    # see stages_from_spec's own docstring).
    #
    # 결함 F1: anchored on current_position_seq (this stop's own seq),
    # not "every run_station submit that exists right now" -- a crash
    # between this SAME resume's own submission and the next position
    # being recorded used to make a retry see its own just-submitted
    # request already counted, bumping resume_n by one and producing a
    # different payload hash/request_id on every retry (the builder
    # dispatched again each time). Anything this resume itself submits
    # lands at a seq AFTER this still-current stop, so it is excluded;
    # recomputing the count on any number of retries of the SAME stop
    # therefore always gives back the same resume_n. A genuinely new
    # stop (recorded only once this resume's own request reaches a
    # terminal state) raises the anchor for the NEXT resume, so that
    # one still gets its own distinct resume_n.
    anchor_seq = current_position_seq(run.ledger, run.project, run.job)
    resume_n = sum(
        1 for seq, event in run.ledger.events(run.project, "request.submitted")
        if event.attempt_id == attempt_id and event.payload.get("kind") == "run_station"
        and (anchor_seq is None or seq <= anchor_seq)
    )
    stages = widen_stage_owns(
        stages_from_spec(spec, run_config, attempt_id, task_text=run_station_text,
                         merge_retry_n=merge_retry_n, resume_n=resume_n),
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
    handoff = HandoffContext(plan, pos.task_id or "", later, None)
    task_pos = replace(pos, extra={**{k: v for k, v in pos.extra.items()
                                      if k != "context_gap"}, "task_text": task_text})
    outcome = run_task_stages(run_config, run.ledger, run.fakes_for(attempt_id), run.project,
                              run.job, attempt_id, stages, on_progress=run.on_progress,
                              processor_factory=run.processor_factory, handoff=handoff,
                              task_pos=task_pos)
    full = station_outcome_to_full(outcome, "build")
    if full is not None:
        return full
    return advance_past_task(run, pos, plan, attempt_id)


__all__ = [
    "advance_past_task", "resume_task", "rerun_task_on_new_attempt", "resume_task_owner",
    "rerun_task_owner_stop", "continue_task_stages",
]
