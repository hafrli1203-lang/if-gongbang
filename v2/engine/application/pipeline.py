"""The full orbit (docs/v2.md 「궤도 9칸」, on top of the existing single-
attempt engine): run one job all the way from a plain-text request to a
usable app with a hand-off report, or stop cleanly for the owner (a
design to approve, an install/approval a builder station asked for) and
resume later.

Stations, in order, each writing only its own files:
  facts    (role "facts")    -> .gongbang/<job>/facts.md
  probe    (role "prober", only if facts.md's own "## 외부 대상"
           section lists at least one external API/CLI/file format
           this job depends on) -> .gongbang/<job>/probe/ -- one real
           call per target, logged with "PROBE OK"/"PROBE FAIL"
           (engine.domain.stage_checks.check_probe_targets/check_probe). Skipped
           entirely (no attempt, just a "probe.skipped" ledger event)
           when facts.md names no such target.
  planner  (role "planner")  -> .gongbang/<job>/plan.json, tests/scenes/
  designer (role "designer", only if plan.ui) -> .gongbang/<job>/design/
  builder  (role "builder", once per plan.tasks entry, in dependency
           order) -> the task's own `owns` paths, narrowed from the
           project's configured build prefixes (engine.domain.policy,
           engine.application.steps.worker_step) -- never tests/scenes/. A UI task
           additionally gets write access to this one job's own
           .gongbang/<job>/screens/ (see engine.application.pipeline_tasks's
           _config_for_build_task) to save screenshots of what it
           built, without widening any other builder-writable path.
  reviewer (role "reviewer") judges each builder task's change, same as
           a plain single-task job (engine.application.steps.review)

Facts/probe/planner/(designer) share ONE attempt (run_station -> merge
-> a gate-specific check -- engine.domain.stage_checks / engine.domain.plan -- for each),
closed once the plan is validated and (if plan.ui) the design is
approved. facts.md/probe logs/plan.json/design.md are committed
directly (see engine.application.pipeline_stages.commit_job_trail) right after
each one's merge, rather than waiting for a worker-gated commit --
nothing a worker wrote is in that diff, only files the orchestrator
itself just wrote, so there is nothing for a reviewer or the test
suite to re-judge.

EACH build task (and each acceptance-fix round) then runs in its OWN
FRESH attempt: open_job -> run_station(build) -> merge -> review ->
bench -> commit -> report -> close (the same seven-stage shape
engine.application.task_pipeline.default_stages() already uses for a plain single-task
job). A fresh attempt per task, rather than reusing one across all of
them, is required by engine.application.steps.merge's own conflict check, which
compares a copy's changes against the ATTEMPT's base_head (fixed at
open_job time), never the project's live HEAD -- reusing one attempt
across several tasks' own commits would make every task after the
first look like it conflicts with "someone else's committed change"
(that change being its OWN earlier sibling task, in the very same
job). engine.application.repair.py already handles this exact base_head fact for a
fix/resume attempt the same way: close, then open_job() again, never
continue on the old attempt.

A design that is not yet approved, or a run_station that asked the
owner for something (e.g. an install), stops the job with
FullOutcome(status="needs_owner", ...). Every stage/task transition and
every stop is recorded as one `job.position` event (engine.application.job_state);
`continue_full_job` is engine.application.job_state.resume_job, which reads the
newest position and dispatches on its stage through RESUME_TABLE below.
Nothing needed to resume lives only in this process's memory.

`processor_factory`, on every entry point, is the same optional seam
engine.application.task_pipeline.run_job/continue_job already expose (a callable building
the engine.application.processor.Processor each stage list runs against) -- plain
production callers (tools/gb2) never pass it (defaults to a real
Processor with register_defaults()); it exists so a test can register
a deterministic run_station/review stub instead of spawning a real
worker/reviewer process.

Every per-task attempt runs with the project's own `release` removed
(engine.application.job_state.without_release): the whole job makes exactly one
publish request, at the very end (engine.application.pipeline_release.
maybe_release_or_done).

This module is a thin facade: the real stations/task-loop/resume logic
lives in engine.application.pipeline_base/_runner/_release/_early/_tasks/_resume
(one responsibility, one file, each under 800 lines -- see those
modules' own docstrings for how they divide the work). Everything is
re-exported here under its original name, including the historical
`_private` names other engine modules (engine.application.quick_pipeline) and the
test suite already call by dotted attribute -- so nothing outside this
module needed to change for the split."""
from __future__ import annotations

from engine.application.pipeline_base import (
    BUILDER_ROLE, DESIGNER_ROLE, EARLY_ROLES, FACTS_ROLE, FullOrbitError, FullOutcome,
    PLANNER_ROLE, PROBER_ROLE, PROBE_LOG_CAP, ProgressCallback, REVIEWER_ROLE, Run,
    checked_facts, fakes_for, trusted_text,
)
from engine.application.pipeline_planning import (
    append_owner_request_resume_note,
    record_plan_reviewed as _record_plan_reviewed, run_facts as _run_facts,
    run_plan_and_review as _run_plan_and_review, run_probe as _run_probe,
    close_early_attempt_then_tasks, facts_then_rest,
    maybe_rerun_plan_review, probe_plan_design_close, run_full_job,
)
from engine.application.pipeline_release import (
    RELEASE_ABANDON_REASONS as _RELEASE_ABANDON_REASONS,
    release_decision_already_recorded as _release_decision_already_recorded,
    release_decision_for_request as _release_decision_for_request,
    write_final_report as _write_final_report, finalize_release, maybe_release_or_done,
    pending_release_progress, prepare_release, record_release_decision,
)
from engine.application.pipeline_resume import (
    ARCHIVE_MARKER_FILES as _ARCHIVE_MARKER_FILES, BEFORE_PLAN_STAGES as _BEFORE_PLAN_STAGES,
    REOPEN_STATION as _REOPEN_STATION, RETRY_STAGE_ROLE as _RETRY_STAGE_ROLE,
    STATION_STAGE as _STATION_STAGE, abandon_orphaned_requests,
    advance_past_task as _advance_past_task, archive_dir_for_attempt, archive_has_content,
    resumed_plan_problems as _check_resumed_plan, continue_task_stages as _continue_task_stages,
    max_fix_rounds as _max_fix_rounds, overlay_archive_into_copy,
    reopen_for_retry as _reopen_for_retry, rerun_task_on_new_attempt as _rerun_task_on_new_attempt,
    rerun_task_owner_stop as _rerun_task_owner_stop, resume_acceptance as _resume_acceptance,
    resume_design_approval as _resume_design_approval, resume_early_close as _resume_early_close,
    resume_early_station as _resume_early_station,
    resume_inflight_publish as _resume_inflight_publish,
    resume_release_approval as _resume_release_approval,
    resume_reopened_station as _resume_reopened_station,
    resume_security_fix as _resume_security_fix, resume_small_plan as _resume_small_plan,
    resume_task as _resume_task, resume_task_owner as _resume_task_owner,
    resumed_plan as _resumed_plan,
    stop_for_resumed_plan_problems as _stop_for_resumed_plan_problems,
    task_attempt_committed as _task_attempt_committed, task_for as _task_for, RESUME_TABLE,
    continue_full_job, resumed_plan_stop, force_reopen_station, ForceReopenError,
    continue_full_job_from,
)
from engine.application.pipeline_stages import (
    ORCHESTRATOR_GIT_IDENTITY as _ORCHESTRATOR_GIT_IDENTITY, attempt_has_requests,
    attempt_opened_after, build_prefixes, build_station_context, close_attempt, commit_job_trail,
    commit_paths, ContextGate, station_context_gate, finish_inflight_close, latest_merge_paths,
    open_with_intent, planned_skills, prompt_with_context, reconcile_opening, record_context,
    record_early_station_stop, record_event, record_task_stop, run_and_merge,
    station_outcome_to_full, stop_build_task_for_unmet_context,
    stop_early_station_for_unmet_context, stops_for_owner, task_position, unmet_context_reason,
)
from engine.application.pipeline_tasks import (
    builder_run_station_payload as _builder_run_station_payload,
    config_for_build_task as _config_for_build_task,
    requirements_without_evidence as _requirements_without_evidence, run_tasks as _run_tasks,
    task_build_owns as _task_build_owns, finish_after_handoff, finish_from_tasks,
    install_followup_task_text, review_payload, run_acceptance_and_handoff, run_task_attempt,
    task_attempt_config, task_stages,
)
from engine.application.job_state import (
    STAGE_DESIGN_APPROVAL, STAGE_EARLY_STATION, STAGE_PLAN_REVIEW, STAGE_PROBE_RETRY,
    STAGE_RELEASE_APPROVAL, STAGE_REQUIREMENTS_REVIEW, STAGE_SECURITY_FIX, STAGE_TASK_OWNER,
    pending_owner_requests,
)
from engine.application.steps.open_job import copy_root, dirty_paths, open_job

# Historical `_private` names: every one of the functions above that
# used to have a leading underscore before the split kept its PUBLIC
# name in the module that now actually owns it (engine.application.pipeline_base/
# _runner/_release/_early/_tasks, each exporting it in their own
# __all__ for the OTHER full_orbit_* modules that call it across that
# boundary) -- the aliases below exist only so a caller still reaching
# for the old dotted name (engine.application.quick_pipeline, the test suite) sees no
# difference.
_checked_facts = checked_facts
_trusted_text = trusted_text
_fakes_for = fakes_for
_record = record_event
_record_early_station_stop = record_early_station_stop
_task_position = task_position
_record_task_stop = record_task_stop
_stops_for_owner = stops_for_owner
_latest_merge_paths = latest_merge_paths
_commit_job_trail = commit_job_trail
_commit_paths = commit_paths
_build_prefixes = build_prefixes
_record_context = record_context
_planned_skills = planned_skills
_build_station_context = build_station_context
_prompt_with_context = prompt_with_context
_unmet_context_reason = unmet_context_reason
_stop_early_station_for_unmet_context = stop_early_station_for_unmet_context
_stop_build_task_for_unmet_context = stop_build_task_for_unmet_context
_open_with_intent = open_with_intent
_attempt_opened_after = attempt_opened_after
_attempt_has_requests = attempt_has_requests
_run_and_merge = run_and_merge
_station_outcome_to_full = station_outcome_to_full
_close_attempt = close_attempt
_maybe_release_or_done = maybe_release_or_done
_prepare_release = prepare_release
_review_payload = review_payload
_install_followup_task_text = install_followup_task_text
_task_stages = task_stages
_run_task_attempt = run_task_attempt
_run_acceptance_and_handoff = run_acceptance_and_handoff
_finish_after_handoff = finish_after_handoff
_finish_from_tasks = finish_from_tasks
_maybe_rerun_plan_review = maybe_rerun_plan_review
_probe_plan_design_close = probe_plan_design_close
_close_early_attempt_then_tasks = close_early_attempt_then_tasks
_facts_then_rest = facts_then_rest


__all__ = [
    "FullOutcome", "FullOrbitError", "Run", "run_full_job", "continue_full_job",
    "record_release_decision", "finalize_release", "pending_owner_requests",
    "pending_release_progress", "RESUME_TABLE",
    "FACTS_ROLE", "PROBER_ROLE", "PLANNER_ROLE", "DESIGNER_ROLE", "BUILDER_ROLE",
    "REVIEWER_ROLE",
    "STAGE_DESIGN_APPROVAL", "STAGE_TASK_OWNER", "STAGE_PROBE_RETRY", "STAGE_PLAN_REVIEW",
    "STAGE_REQUIREMENTS_REVIEW", "STAGE_SECURITY_FIX", "STAGE_RELEASE_APPROVAL",
    "STAGE_EARLY_STATION", "force_reopen_station", "ForceReopenError", "continue_full_job_from",
]
