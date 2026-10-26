"""The build-task loop: open a fresh attempt per plan.tasks entry (see
engine.application.pipeline's own module docstring for why), run it through its
seven stages, then acceptance -> fix rounds -> handoff -> the app-wide
security check -> the job's one release request.

Depends on engine.application.pipeline_base and engine.application.pipeline_stages;
engine.application.pipeline_planning calls back into this module for
install_followup_task_text/review_payload/finish_from_tasks (the early
stations and the build loop share those), and engine.application.pipeline_resume
calls run_task_attempt/task_stages/run_acceptance_and_handoff/
finish_after_handoff/finish_from_tasks to resume a task or the
acceptance/handoff tail -- this module never calls back into either of
them."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from typing import TYPE_CHECKING

from engine.application import task_pipeline as orbit
from engine.config import EngineConfig
from engine.application.pipeline_base import (
    BUILDER_ROLE, REVIEWER_ROLE, FullOutcome, ProgressCallback, Run, fakes_for,
)
from engine.application.pipeline_release import maybe_release_or_done
from engine.application.pipeline_stages import (
    ContextGate, build_station_context, commit_job_trail, open_with_intent, planned_skills,
    record_event, record_task_stop, station_context_gate, station_outcome_to_full,
    stop_build_task_for_unmet_context, stops_for_owner, task_position,
)
from engine.domain.plan import Plan, Task, classify_skill_tools, tools_for_station
from engine.application.job_state import (
    STAGE_POST_TASKS, STAGE_REQUIREMENTS_REVIEW, STAGE_SECURITY_FIX, STAGE_TASK_OWNER,
    record_position, stages_spec, without_release,
)
from engine.application.processor import Fakes
from engine.application.stage_prompts import builder_task_text, fix_task_text
from engine.application.steps.open_job import copy_root, dirty_paths

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger
    from engine.infrastructure import context as context_mod


def config_for_build_task(config: EngineConfig, job_name: str, plan: Plan) -> EngineConfig:
    """`config`, unchanged, except (only when plan.ui) the build
    station's builder role additionally owns THIS job's own
    .gongbang/<job_name>/screens/ -- never the rest of
    .gongbang/<job_name>/ (facts.md, plan.json, report.md stay out of
    reach) and never another job's screens/, so a UI task's build
    attempt can save its own screenshots there without widening any
    other builder-writable path. Used only for the run_station/merge
    stages of that one task's own attempt (see run_task_attempt) --
    every other stage (review/bench/commit/...) only reads fields this
    never touches, so passing it there too would be harmless but is
    avoided for clarity."""
    if not plan.ui:
        return config
    screens_prefix = f".gongbang/{job_name}/screens/"
    build_owners = dict(config.station_owners.get("build", {}))
    build_owners[BUILDER_ROLE] = tuple(build_owners.get(BUILDER_ROLE, ())) + (screens_prefix,)
    station_owners = {**config.station_owners, "build": build_owners}
    return replace(config, station_owners=station_owners)



def requirements_without_evidence(ledger: Ledger, project: str, job_name: str,
                                   plan: Plan) -> tuple[str, ...]:
    """Requirement ids whose evidence chain (engine.domain.evidence_graph: a
    covering scene that passed in the newest recorded acceptance run, or
    for a task-only requirement a commit) is incomplete. Only the
    per-requirement links are checked here; the job-wide acceptance and
    security certificates are checked at release."""
    from engine.domain import evidence_graph

    graph = evidence_graph.build(ledger, project, job_name, plan)
    requirement_ids = {r.requirement_id for r in graph.requirements}
    out: list[str] = []
    for line in evidence_graph.missing(graph):
        requirement_id = line.split(":", 1)[0]
        if requirement_id in requirement_ids and requirement_id not in out:
            out.append(requirement_id)
    return tuple(out)



def task_build_owns(task: Task, plan: Plan, job_name: str) -> list[str]:
    """This task's own write prefixes (task.owns) plus, only when
    plan.ui, the engine's own extra screens/ allowance (see
    config_for_build_task) -- never part of the plan's own owns, so
    the planner never has to know or ask for it. Shared by the
    run_station payload (builder_run_station_payload) and the merge
    payload (run_task_attempt/the STAGE_TASK_OWNER worker_stuck
    resume) so merge's own re-check (engine.application.steps.merge._owner_prefixes)
    is narrowed to exactly the same set run_station's own gate already
    enforced while the worker was running -- never the broader,
    unnarrowed station prefix (a per-task merge used to
    accept a write anywhere under the WHOLE build station's own
    prefixes, not just this task's own owns)."""
    owns = list(task.owns)
    if plan.ui:
        owns.append(f".gongbang/{job_name}/screens/")
    return owns



def builder_run_station_payload(task: Task, task_text: str, plan: Plan,
                                 config: EngineConfig, job_name: str) -> dict[str, object]:
    usable_skills, _needs_install = classify_skill_tools(plan, config)
    return {"station": "build", "role": BUILDER_ROLE, "task": task_text,
            "owns": task_build_owns(task, plan, job_name),
            "tools": tools_for_station(plan, "build", usable_skills=usable_skills)}



def build_task_gate(config: EngineConfig, ledger: Ledger, project: str, plan: Plan,
                    base_task_text: str, attempt_id: str | None = None) -> ContextGate:
    """The build station's required-context gate for one task, including
    the task review that will judge it (engine.application.pipeline_stages.
    station_context_gate)."""
    return station_context_gate(config, ledger, project, attempt_id, "build", base_task_text,
                                planned_skills(config, plan, "build"),
                                review_query=base_task_text,
                                review_skills=planned_skills(config, plan, "review"))


def review_payload(config: EngineConfig, ledger: Ledger, project: str, plan: Plan,
                    task: str, *,
                    context: "context_mod.StationContext | None" = None) -> dict[str, object]:
    """The review stage judges against the task itself (without the
    build station's injected skills) and gets its own station context:
    the review base skills plus the skills the planner picked for
    review. Also carries the planner's own `tools_for_station(plan,
    "review")` table -- without it, engine.application.steps.review's own mcp
    narrowing (_payload_mcp_names) always saw an empty "tools" key,
    so neither the codex reviewer's own "reviewer_mcp_unsupported"
    guard (which only fires when the payload actually asks for mcp)
    nor a claude reviewer's actual mcp access ever reflected what the
    plan asked for. `context`, when the caller already gated and recorded
    the review station's context, is used instead of building it again."""
    if context is None:
        context = build_station_context(config, ledger, project, None, "review", task,
                                        planned_skills(config, plan, "review"))
    review_context = context.text
    usable_skills, _needs_install = classify_skill_tools(plan, config)
    payload: dict[str, object] = {
        "role": REVIEWER_ROLE, "task": task,
        "tools": tools_for_station(plan, "review", usable_skills=usable_skills),
    }
    if review_context.strip():
        payload["context"] = review_context.strip()
    return payload



def install_followup_task_text(task_text: str, build_copy: Path) -> str:
    """`task_text` (the builder's own task, with its skill-context
    appendix already attached) plus a note that the install it once
    asked for has since completed -- never start over, pick up from
    where it stopped. `build_copy`'s own dirty_paths (the copy's real
    git status, never the worker's self-report) lists what it had
    already changed before it asked for the install, same evidence
    engine.application.task_pipeline._continuation_payload shows a stalled worker."""
    changed = dirty_paths(build_copy, ())
    listed = "\n".join(f"- {p}" for p in changed) or "- (아직 없음)"
    note = (
        "\n\n[의존성 설치 완료] 의존성이 설치됐다. 처음부터 다시 하지 말고, 멈춘 곳부터 "
        "이어서 끝내라. 이미 바뀐 아래 파일을 먼저 읽는다.\n"
        f"{listed}"
    )
    return f"{task_text}{note}"



def task_attempt_config(config: EngineConfig, project: str, job_name: str,
                         plan: Plan) -> EngineConfig:
    """The one config every stage of a build task's attempt runs with --
    first run and every resume alike: the plan's UI screens/ allowance
    added (config_for_build_task) and the project's own `release`
    removed (engine.application.job_state.without_release)."""
    return without_release(config_for_build_task(config, job_name, plan), project)



def task_stages(config: EngineConfig, build_config: EngineConfig, ledger: Ledger,
                 project: str, job_name: str, plan: Plan, task: Task, task_text: str,
                 review_task_text: str, attempt_id: str) -> list[orbit.Stage]:
    """A build task's seven stages: run_station(build) -> merge (narrowed
    to the task's own owns) -> review -> bench -> commit -> report ->
    close. A crash resume submits exactly this list again, so every
    request that already finished reconciles by its own id and nothing
    runs twice."""
    build_copy = str(copy_root(build_config, attempt_id, BUILDER_ROLE))
    return [
        orbit.Stage("run_station", builder_run_station_payload(task, task_text, plan,
                                                                 build_config, job_name)),
        orbit.Stage("merge", {"copies": [
            {"role": BUILDER_ROLE, "station": "build", "path": build_copy,
             "owns": task_build_owns(task, plan, job_name)},
        ]}),
        orbit.Stage("review", review_payload(config, ledger, project, plan, review_task_text)),
        orbit.Stage("bench", {}),
        orbit.Stage("commit", {"message": f"gongbang: {job_name} - {task.id}"}),
        orbit.Stage("report", {"summary": f"gongbang: {job_name} - {task.id}"}),
        orbit.Stage("close", {}),
    ]



def run_task_attempt(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                      job_name: str, task: Task, task_text: str, plan: Plan,
                      on_progress: ProgressCallback | None,
                      processor_factory: orbit.ProcessorFactory | None,
                      base_task_text: str | None = None, *,
                      fixing: bool = False, remaining_task_ids: tuple[str, ...] = (),
                      max_fix_rounds: int = 2, fix_round: int | None = None,
                      ) -> orbit.JobOutcome:
    """Open a FRESH attempt for this one task (see module docstring for
    why) and run its full run_station(build) -> merge -> review ->
    bench -> commit -> report -> close sequence -- exactly
    engine.application.task_pipeline.default_stages()'s own seven stages, just built
    directly here so the run_station payload can carry this task's own
    `owns`, which default_stages() has no parameter for.

    `fixing`/`remaining_task_ids`/`max_fix_rounds` carry nothing this
    function itself needs -- they are recorded on the task position so a
    later resume knows what this task IS (an acceptance-fix round or a
    plain plan.tasks entry) and what is still queued behind it. If the
    attempt stops for the owner, that stop is recorded here too, as the
    task position moved to STAGE_TASK_OWNER."""
    task_pos = task_position(job_name, None, task.id, task_text,
                              base_task_text or task_text, fixing=fixing,
                              remaining_task_ids=remaining_task_ids,
                              max_fix_rounds=max_fix_rounds)
    attempt_id = open_with_intent(config, ledger, project, task_pos, (BUILDER_ROLE,))
    build_config = task_attempt_config(config, project, job_name, plan)
    stages = task_stages(config, build_config, ledger, project, job_name, plan, task,
                          task_text, base_task_text or task_text, attempt_id)
    # The stage list is recorded once, with the task's position on its
    # attempt: every recovery and a fix of this task replay exactly it.
    task_pos = replace(task_pos, attempt_id=attempt_id,
                       extra={**task_pos.extra, "stages": stages_spec(stages)})
    record_position(ledger, project, task_pos)
    outcome = orbit.continue_job(build_config, ledger, fakes_for(fakes, attempt_id), project,
                                 attempt_id, stages, processor_factory=processor_factory,
                                 on_progress=on_progress)
    if stops_for_owner(outcome):
        stop_extra = {"fix_round": fix_round} if fix_round is not None else {}
        record_task_stop(ledger, project, task_pos, outcome, **stop_extra)
    return outcome



def run_tasks(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
              job_name: str, plan: Plan, tasks: list[Task],
              on_progress: ProgressCallback | None,
              processor_factory: orbit.ProcessorFactory | None,
              last_attempt_id: str | None = None, max_fix_rounds: int = 2,
              ) -> tuple[FullOutcome | None, str | None]:
    """Run every task in `tasks` (already in dependency order), each in
    its own fresh attempt, to completion. (None, last_attempt_id) iff
    all of them finished "done" -- otherwise (the needs_owner/failed
    FullOutcome to return right now, its own attempt_id), having
    already journaled enough progress (remaining task ids) for
    continue_full_job to pick up at the task that stopped.

    `last_attempt_id`, when given, is the attempt that already covers
    whatever `tasks` was narrowed down FROM (e.g. a STAGE_TASK_OWNER
    resume's own already-finished attempt, once the task that stopped
    is dropped off the front of `remaining_task_ids`) -- if `tasks` is
    now empty (that dropped task was the LAST one in the plan), the
    loop below never runs, and this value is returned UNCHANGED rather
    than silently falling back to None: the caller still needs a real
    attempt_id to pass acceptance (a None attempt_id used to crash
    engine.application.acceptance.run_acceptance on exactly this resume path)."""
    for index, task in enumerate(tasks):
        base_task_text = builder_task_text(task, plan.goal, job_name, plan.ui)
        remaining_now = tuple(t.id for t in tasks[index:])
        build_gate = build_task_gate(config, ledger, project, plan, base_task_text)
        if build_gate.unmet is not None:
            return stop_build_task_for_unmet_context(
                config, ledger, project, job_name, task, base_task_text, base_task_text,
                build_gate.unmet, fixing=False, remaining_task_ids=remaining_now,
                max_fix_rounds=max_fix_rounds,
            ), last_attempt_id
        task_text = build_gate.prompt(base_task_text)
        outcome = run_task_attempt(config, ledger, fakes, project, job_name, task,
                                    task_text, plan, on_progress, processor_factory,
                                    base_task_text, remaining_task_ids=remaining_now,
                                    max_fix_rounds=max_fix_rounds)
        last_attempt_id = outcome.attempt_id
        full = station_outcome_to_full(outcome, "build")
        if full is not None:
            return full, last_attempt_id
    return None, last_attempt_id



def run_acceptance_and_handoff(config: EngineConfig, ledger: Ledger, fakes: Fakes,
                                project: str, job_name: str, plan: Plan,
                                max_fix_rounds: int, on_progress: ProgressCallback | None,
                                processor_factory: orbit.ProcessorFactory | None,
                                last_attempt_id: str | None) -> FullOutcome:
    from engine import acceptance as acceptance_mod  # worker C's module; lazy import
    from engine import handoff as handoff_mod  # worker C's module; lazy import

    record_event(ledger, project, job_name, STAGE_POST_TASKS, last_attempt_id,
            next_action="acceptance", max_fix_rounds=max_fix_rounds)
    acc = acceptance_mod.run_acceptance(config, ledger, project, last_attempt_id, plan)
    rounds = 0
    while not acc.passed and rounds < max_fix_rounds:
        failing = tuple(s.id for s in acc.scenes if not s.passed)
        all_owns = tuple(sorted({entry for t in plan.tasks for entry in t.owns}))
        fix_task = Task(id=f"fix-{rounds + 1}", title="수락 실패 수정", detail="",
                        owns=all_owns, depends_on=())
        # getattr with a default: these two AcceptanceResult fields (an earlier round
        # contract item 2) may not exist yet in this worktree's own
        # engine.application.acceptance -- fix_task_text falls back to its own
        # "no failing scene" wording either way.
        base_task_text = fix_task_text(
            fix_task, plan.goal, failing, job_name, plan.ui,
            full_suite_returncode=getattr(acc, "full_suite_returncode", None),
            full_suite_tail=getattr(acc, "full_suite_tail", ""),
        )
        fix_gate = build_task_gate(config, ledger, project, plan, base_task_text)
        if fix_gate.unmet is not None:
            return stop_build_task_for_unmet_context(
                config, ledger, project, job_name, fix_task, base_task_text, base_task_text,
                fix_gate.unmet, fixing=True, remaining_task_ids=(), max_fix_rounds=max_fix_rounds,
            )
        task_text = fix_gate.prompt(base_task_text)
        outcome = run_task_attempt(config, ledger, fakes, project, job_name, fix_task,
                                    task_text, plan, on_progress, processor_factory,
                                    base_task_text, fixing=True,
                                    max_fix_rounds=max_fix_rounds, fix_round=rounds)
        last_attempt_id = outcome.attempt_id
        full = station_outcome_to_full(outcome, "build")
        if full is not None:
            return full
        rounds += 1
        record_event(ledger, project, job_name, STAGE_POST_TASKS, last_attempt_id,
                next_action="acceptance", max_fix_rounds=max_fix_rounds)
        acc = acceptance_mod.run_acceptance(config, ledger, project, last_attempt_id, plan)

    if not acc.passed:
        return FullOutcome("failed", "max_fix_rounds_exceeded", last_attempt_id, None)

    uncovered = requirements_without_evidence(ledger, project, job_name, plan)
    if uncovered:
        reason = f"requirements_uncovered:{','.join(uncovered)}"
        record_event(ledger, project, job_name, STAGE_REQUIREMENTS_REVIEW, last_attempt_id,
                stop_reason=reason, max_fix_rounds=max_fix_rounds)
        return FullOutcome("needs_owner", reason, last_attempt_id, None)

    record_event(ledger, project, job_name, STAGE_POST_TASKS, last_attempt_id,
            next_action="handoff", max_fix_rounds=max_fix_rounds)
    report_path = handoff_mod.write_handoff(config, ledger, project, last_attempt_id,
                                            job_name, plan, acc)
    commit_job_trail(config, ledger, project, last_attempt_id, job_name,
                      f"gongbang: {job_name} - handoff")
    return finish_after_handoff(config, ledger, project, job_name, plan, last_attempt_id,
                                 report_path, max_fix_rounds)



def finish_after_handoff(config: EngineConfig, ledger: Ledger, project: str, job_name: str,
                          plan: Plan, attempt_id: str, report_path: Path,
                          max_fix_rounds: int) -> FullOutcome:
    """Everything after the handoff report is written and committed:
    the app-wide security check  and, only once that passes, the
    job's single release request (maybe_release_or_done)."""
    from engine import app_security  # worker B's module; lazy import

    record_event(ledger, project, job_name, STAGE_POST_TASKS, attempt_id,
            next_action="security", max_fix_rounds=max_fix_rounds)
    security = app_security.check(config, project, attempt_id, plan)
    # The result (findings carry kind:path:line only, never values) is
    # recorded for the release gate and for the person fixing it.
    app_security.record(ledger, project, attempt_id, security)
    if not security.passed:
        findings = "; ".join(list(security.findings)[:5])
        reason = f"security_failed:{findings}"[:300] if findings else "security_failed"
        # The repo's own content fingerprint (engine.infrastructure.evidence.
        # fingerprint, a tree hash covering the working tree -- tracked
        # AND dirty paths alike, same value engine.application.acceptance's own
        # before/after check uses) acceptance/handoff actually
        # certified -- read back on resume (STAGE_SECURITY_FIX) to tell
        # a plain security retry from one where the owner's own fix has
        # since changed the source, which must rerun acceptance/
        # coverage/handoff against the new source before security runs
        # again, not just security itself. A plain `git rev-parse HEAD`
        # (this field's own earlier implementation) missed exactly the
        # case this exists for: an owner fix that EDITS the source
        # WITHOUT committing (`gb2 fix` always commits on a fresh
        # attempt and so moves HEAD, but a direct owner edit to the
        # working tree does not) left HEAD unchanged, so that comparison
        # silently skipped rerunning acceptance/coverage/handoff and let
        # a build-only job report "done" against source acceptance never
        # actually certified.
        from engine.infrastructure import evidence  # worker C's module; lazy import

        excludes = config.projects[project].excludes
        certified_fingerprint = evidence.fingerprint(config.projects[project].repo, excludes)
        record_event(ledger, project, job_name, STAGE_SECURITY_FIX, attempt_id,
                stop_reason=reason, report_path=str(report_path),
                max_fix_rounds=max_fix_rounds, certified_fingerprint=certified_fingerprint)
        return FullOutcome("needs_owner", reason, attempt_id, None)
    return maybe_release_or_done(config, ledger, project, job_name, attempt_id,
                                  report_path, max_fix_rounds)



def finish_from_tasks(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                       job_name: str, plan: Plan, tasks: list[Task], max_fix_rounds: int,
                       on_progress: ProgressCallback | None,
                       processor_factory: orbit.ProcessorFactory | None,
                       last_attempt_id: str | None = None) -> FullOutcome:
    stopped, last_attempt_id = run_tasks(config, ledger, fakes, project, job_name, plan,
                                         tasks, on_progress, processor_factory,
                                         last_attempt_id, max_fix_rounds)
    if stopped is not None:
        return stopped
    return run_acceptance_and_handoff(config, ledger, fakes, project, job_name, plan,
                                       max_fix_rounds, on_progress, processor_factory,
                                       last_attempt_id)


__all__ = [
    "task_attempt_config", "task_stages", "run_task_attempt", "run_acceptance_and_handoff",
    "finish_after_handoff", "finish_from_tasks", "review_payload", "install_followup_task_text",
    "build_task_gate",
]
