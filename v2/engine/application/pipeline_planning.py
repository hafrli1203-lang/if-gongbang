"""The shared facts -> probe -> plan+review -> (design) pipeline: ONE
attempt (engine.application.pipeline's own module docstring, "Facts/probe/
planner/(designer) share ONE attempt") that run_full_job opens and
every STAGE_FACTS/STAGE_PROBE_RETRY/STAGE_PLAN_REVIEW/STAGE_EARLY_
STATION/STAGE_DESIGN_APPROVAL resume (engine.application.pipeline_resume) re-
enters partway through via probe_plan_design_close/facts_then_rest/
maybe_rerun_plan_review.

Depends on engine.application.pipeline_base and engine.application.pipeline_stages for the
attempt-opening/station-running/context primitives, and on
engine.application.pipeline_tasks for the three helpers (install_followup_task_
text/review_payload/finish_from_tasks) the early stations and the task
loop both need -- never the other way."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from engine.domain import faults
from engine.application import task_pipeline as orbit
from engine.infrastructure import trusted
from engine.config import EngineConfig
from engine.application.pipeline_base import (
    DESIGNER_ROLE, EARLY_ROLES, FACTS_ROLE, PLANNER_ROLE, PROBER_ROLE, PROBE_LOG_CAP,
    FullOutcome, ProgressCallback, Run, checked_facts, fakes_for, trusted_text,
)
from engine.application.pipeline_stages import (
    build_prefixes, close_attempt, commit_job_trail, open_with_intent,
    planned_skills, record_early_station_stop, record_event, run_and_merge,
    station_context_gate, station_outcome_to_full, stop_early_station_for_unmet_context,
)
from engine.application.pipeline_tasks import (
    finish_from_tasks, install_followup_task_text, review_payload,
)
from engine.domain.stage_checks import (
    ProbeTargetsError, check_design_approved, check_probe_targets, facts_path,
)
from engine.domain.records import Event
from engine.domain.plan import (
    Plan, PlanError, classify_skill_tools, load_plan, ordered_tasks, plan_path,
    skill_catalog_text, tools_for_station, validate_plan, validate_tools,
)
from engine.application.job_state import (
    STAGE_DESIGN, STAGE_DESIGN_APPROVAL, STAGE_EARLY_CLOSE, STAGE_EARLY_STATION, STAGE_FACTS,
    STAGE_PLAN, STAGE_PLAN_REVIEW, STAGE_PROBE, STAGE_PROBE_RETRY, JobPosition,
)
from engine.application.processor import Fakes
from engine.application.stage_prompts import (
    designer_prompt, facts_prompt, planner_prompt, prober_prompt,
)
from engine.application.steps.open_job import copy_root

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger


def probe_log_path(repo: Path, job_name: str, index: int) -> Path:
    from engine.domain.stage_checks import probe_target_id

    return repo / ".gongbang" / job_name / "probe" / f"{probe_target_id(index)}.log"



def _capture_probe_logs(repo: Path, job_name: str, targets: list[tuple[str, str]],
                        ) -> list[tuple[str, Path, trusted.Trusted]]:
    """Every target's probe log as checked (engine.infrastructure.trusted.capture), right
    after the engine's own probe run passed; a log that cannot be read
    safely is left out, the same as an unreadable one in the prompt."""
    captured: list[tuple[str, Path, trusted.Trusted]] = []
    for i, (name, _url) in enumerate(targets):
        path = probe_log_path(repo, job_name, i)
        try:
            captured.append((name, path, trusted.capture(path)))
        except OSError:
            continue
    return captured


capture_probe_logs = _capture_probe_logs



def verified_probe_text(captured: list[tuple[str, Path, trusted.Trusted]],
                         ) -> tuple[str, str | None]:
    """("## 실측 결과" text built from the captured logs, None), or ("",
    the first target whose log changed since it was checked)."""
    parts: list[str] = []
    for name, path, t in captured:
        text = trusted_text(t, path)
        if text is None:
            return "", name
        parts.append(f"### {name}\n{text[:PROBE_LOG_CAP]}")
    if not parts:
        return "", None
    return "## 실측 결과\n" + "\n\n".join(parts), None



def probe_logs_text(repo, job_name: str, targets: list[tuple[str, str]]) -> str:
    """Every target's own probe log (capped at PROBE_LOG_CAP chars
    each), under one "## 실측 결과" heading -- "" if `targets` is empty
    or none of the logs can be read (check_probe already gated on
    them existing and saying PROBE OK before this is ever called, so
    a read failure here would mean the file vanished in between)."""
    parts: list[str] = []
    from engine.domain.stage_checks import probe_target_id

    for i, (name, _url) in enumerate(targets):
        path = repo / ".gongbang" / job_name / "probe" / f"{probe_target_id(i)}.log"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        parts.append(f"### {name}\n{text[:PROBE_LOG_CAP]}")
    if not parts:
        return ""
    return "## 실측 결과\n" + "\n\n".join(parts)



def append_owner_request_resume_note(config: EngineConfig, attempt_id: str, role: str,
                                      task_text: str, needs_resume_note: bool) -> str:
    """`task_text` unchanged for a fresh run OR a "worker_stuck" resume
    (submit()'s own with_retry already busts a FAILED request's
    payload automatically, so an unchanged prompt still reruns for
    real -- and orbit never records "worker_stuck" WITH any pending
    requests at all, so `needs_resume_note` is always False there) --
    but with a continuation note appended for an owner-request resume
    (`needs_resume_note` True: this STAGE_EARLY_STATION stop had a
    non-empty, now-fulfilled `pending_requests`): that station's own
    run_station request ended "done", so resubmitting this SAME,
    unchanged prompt would just reconcile onto that already-"done"
    request -- and its already-recorded pending install -- forever,
    rather than ever actually giving the station a chance to finish
    (the exact infinite-stall bug a test caught). Reuses the same note
    engine.application.pipeline's own build-task resume gives a builder after an
    install (install_followup_task_text)."""
    if not needs_resume_note:
        return task_text
    return install_followup_task_text(task_text, copy_root(config, attempt_id, role))



def _run_probe(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
              job_name: str, attempt_id: str, request: str,
              on_progress: ProgressCallback | None,
              processor_factory: orbit.ProcessorFactory | None,
              max_fix_rounds: int, needs_resume_note: bool = False,
              ) -> tuple[FullOutcome | None, str]:
    """Runs the probe station from scratch (a fresh run_station call --
    used for a job's very first probe attempt, a STAGE_PROBE_RETRY
    resume, AND a STAGE_EARLY_STATION owner-request resume -- see
    probe_plan_design_close/append_owner_request_resume_note).
    Returns (None, probe_text) to keep going, or (the needs_owner/
    failed FullOutcome to return right now, "")."""
    record_event(ledger, project, job_name, STAGE_PROBE, attempt_id, next_action="run_station",
            request=request, max_fix_rounds=max_fix_rounds)
    repo = config.projects[project].repo
    try:
        targets = check_probe_targets(repo, job_name)
    except ProbeTargetsError as exc:
        # A malformed 외부 대상 line is never silently skipped: a person
        # fixes facts.md, then continue reruns this station.
        reason = f"probe_targets_invalid:{exc}"[:200]
        record_event(ledger, project, job_name, STAGE_PROBE_RETRY, attempt_id, stop_reason=reason,
                request=request, max_fix_rounds=max_fix_rounds)
        return FullOutcome("needs_owner", reason, attempt_id, None), ""
    if not targets:
        faults.flow_boundary("full_orbit.probe_skipped")
        ledger.append(Event(
            project=project, kind="probe.skipped", actor_kind="processor",
            actor_id="processor", attempt_id=attempt_id, payload={"job": job_name},
        ))
        return None, ""
    base_probe_text = prober_prompt(request, job_name, targets)
    # A STAGE_PROBE_RETRY resume reruns this on the SAME (never-closed)
    # attempt -- engine.domain.ids/submit's own request_id is deterministic from
    # this payload, so an unchanged prompt would collide with the FIRST
    # run's already-"done" request and never actually run again (the
    # RunStationStub-shaped bug this fixed). The previous attempt's own
    # already-committed logs (if any -- "" on a job's first probe call)
    # make the retry's payload genuinely different, and give the worker
    # real context on what failed last time.
    previous_logs = probe_logs_text(repo, job_name, targets)
    probe_prompt_text = (f"{base_probe_text}\n\n이전 시도 로그(다시 시도한다):\n{previous_logs}"
                         if previous_logs else base_probe_text)
    probe_gate = station_context_gate(config, ledger, project, attempt_id, "probe", request)
    if probe_gate.unmet is not None:
        return stop_early_station_for_unmet_context(
            ledger, project, attempt_id, job_name, "probe", request, max_fix_rounds,
            probe_gate.unmet,
        ), ""
    probe_task_text = probe_gate.prompt(probe_prompt_text)
    probe_task_text = append_owner_request_resume_note(
        config, attempt_id, PROBER_ROLE, probe_task_text, needs_resume_note)
    outcome = run_and_merge(config, ledger, fakes, project, attempt_id, "probe",
                             PROBER_ROLE, probe_task_text, on_progress,
                             processor_factory)
    full = station_outcome_to_full(outcome, "probe")
    if full is not None:
        record_early_station_stop(ledger, project, attempt_id, job_name, "probe",
                                   outcome.reason, outcome.pending_requests, request=request,
                                   max_fix_rounds=max_fix_rounds)
        return full, ""
    commit_job_trail(config, ledger, project, attempt_id, job_name,
                      f"gongbang: {job_name} - probe")
    # Pass/fail comes from the engine's own run of each probe script
    # (probe.executed: script, return code, output); the worker's log
    # text ("PROBE OK") is supporting context only.
    from engine import acceptance as acceptance_mod

    evidence = acceptance_mod.run_probe_scripts(config, ledger, project, attempt_id,
                                                job_name, targets)
    failed = [e.name for e in evidence if not e.ok]
    if failed:
        failed_names = ",".join(failed)
        reason = f"probe_failed:{failed_names}"
        record_event(ledger, project, job_name, STAGE_PROBE_RETRY, attempt_id, stop_reason=reason,
                request=request, max_fix_rounds=max_fix_rounds)
        return FullOutcome("needs_owner", reason, attempt_id, None), ""
    probe_text, changed = verified_probe_text(_capture_probe_logs(repo, job_name, targets))
    if changed is not None:
        reason = f"probe_log_changed_after_check:{changed}"[:200]
        record_event(ledger, project, job_name, STAGE_PROBE_RETRY, attempt_id, stop_reason=reason,
                request=request, max_fix_rounds=max_fix_rounds)
        return FullOutcome("needs_owner", reason, attempt_id, None), ""
    return None, probe_text


run_probe = _run_probe


# Generous enough that a realistic finding list always fits whole (a
# reviewer rarely reports more than a handful of P0-P3 items) -- only a
# pathological one is ever cut, and plan_review_findings_note marks the
# cut rather than silently dropping entries.
_FINDINGS_NOTE_CAP = 20_000


def plan_review_findings_note(ledger: Ledger, project: str, attempt_id: str) -> str:
    """"[지난 검토에서 고칠 것]" section built from `attempt_id`'s own
    NEWEST "review" evidence row (Ledger.evidence, oldest-first --
    engine.domain.evidence_graph._task_review_passed's own contract: the
    last row is the newest) -- "" iff that attempt has no review
    evidence at all (a plan that crashed or stopped before review ever
    ran has nothing to carry forward).

    This is the fix for the real defect found testing a performance-ads
    job: a STAGE_PLAN_REVIEW resume (engine.application.pipeline_resume.
    _resume_reopened_station) used to open a fresh planner attempt
    whose own prompt never mentioned a single word of the review that
    just rejected the stale attempt's plan.json -- `gb2 continue`
    reopened the SAME station with a prompt that could, and did,
    reproduce the SAME P1/P2 findings. Every finding's severity/file/
    line/note is kept whole (never summarized), so the planner sees
    exactly what it was rejected for; a note also tells the planner
    where to record what it fixed (plan.json's own task "detail" --
    plan.json has no separate "notes" field) so a later review can
    tell the finding was actually addressed, not just silently
    dropped. Lives here, next to run_plan_and_review (the only caller
    that ever attaches it to a prompt), rather than in
    engine.application.pipeline_resume (whose own _resume_reopened_station calls it) --
    same reasoning bd38249 already applied to facts/probe/plan rule
    sentences: the one place that reads the review's own payload shape
    stays next to the one place that turns it into prompt text."""
    if not attempt_id:
        return ""
    rows = ledger.evidence(attempt_id, "review")
    if not rows:
        return ""
    findings = rows[-1].payload.get("findings")
    if not isinstance(findings, list) or not findings:
        return ""
    lines = [
        f"- [{f.get('severity', '?')}] {f.get('file', '?')}:{f.get('line', '?')} "
        f"— {f.get('note', '')}"
        for f in findings if isinstance(f, dict)
    ]
    if not lines:
        return ""
    header = (
        "[지난 검토에서 고칠 것]\n"
        "아래는 이 계획의 직전 시도가 검토에서 불합격한 지적이다. 하나도 빠짐없이 "
        "반영해 다시 쓴다. 어디서 어떻게 고쳤는지는 plan.json의 해당 task "
        '"detail"에 적는다(예: "P1 fix: <무엇을 어디서 어떻게 고쳤는지>"). 지적을 '
        "반영하지 않은 계획은 같은 지적으로 다시 검토에서 떨어진다.\n"
    )
    text = header + "\n".join(lines)
    if len(text) > _FINDINGS_NOTE_CAP:
        text = (text[:_FINDINGS_NOTE_CAP]
                + f"\n...(생략됨 -- 지적 {len(lines)}건 중 일부만 표시)")
    return text


# The "evidence.recorded" kind a plan-check failure (load_plan's own
# PlanError, or validate_plan/validate_tools returning problems) is
# stored under -- read back by plan_check_failure_note. Distinct from
# "review" (a reviewer's own verdict on an already-parseable plan.json)
# since these two failures happen at a different, EARLIER point (before
# any reviewer ever runs) and carry a different shape of detail.
PLAN_CHECK_EVIDENCE_KIND = "plan_check"


def record_plan_check_failure(ledger: Ledger, project: str, attempt_id: str, *,
                               check: str, detail: str) -> None:
    """Records ONE "plan_check" evidence row for `attempt_id` -- the
    fix for the real defect found testing a marketing-chat job (결함
    30): a plan station stopped twice in a row with "plan:
    plan_unreadable" (a planner-written goal string's own unescaped
    `--tools ""` broke plan.json's JSON syntax, json.JSONDecodeError
    "Expecting ',' delimiter: line 2 column 654"), and `gb2 continue
    --from plan` reran the SAME planner with no idea why -- nothing
    was ever recorded, so the SAME mistake just repeated. `check` is
    PlanError.reason ("plan_unreadable" or a schema token) for a
    load_plan failure, or "plan_invalid" for a validate_plan/
    validate_tools failure; `detail` is json_decode_error_detail's own
    text (JSON errors) or the "; "-joined problem list (validate
    errors) -- "" is never recorded (plan_check_failure_note treats an
    empty detail the same as no row at all, so there is nothing worth
    reading back)."""
    if not detail:
        return
    from engine.infrastructure import evidence as evidence_mod

    evidence_mod.record(ledger, project, attempt_id, PLAN_CHECK_EVIDENCE_KIND, "",
                         payload={"check": check, "detail": detail})


def plan_check_failure_note(ledger: Ledger, project: str, attempt_id: str) -> str:
    """"[지난 계획 파일 문제 -- 고칠 것]" section built from `attempt_id`'s
    own NEWEST "plan_check" evidence row -- "" iff that attempt never
    recorded one (record_plan_check_failure never ran, or ran with an
    empty detail). Same placement/approach as plan_review_findings_note
    (3e95f0d) -- lives here, next to run_plan_and_review (the only
    caller that attaches it to a prompt), rather than in
    engine.application.pipeline_resume (whose own _resume_reopened_station/
    force_reopen_station call it)."""
    if not attempt_id:
        return ""
    rows = ledger.evidence(attempt_id, PLAN_CHECK_EVIDENCE_KIND)
    if not rows:
        return ""
    payload = rows[-1].payload
    detail = payload.get("detail")
    if not isinstance(detail, str) or not detail:
        return ""
    check = payload.get("check", "?")
    header = (
        "[지난 계획 파일 문제 -- 고칠 것]\n"
        f"이 계획의 직전 시도가 쓴 plan.json은 \"{check}\"로 실패했다. 아래 문제를 "
        "고쳐서 다시 쓴다 -- 같은 문제가 그대로면 또 실패한다.\n"
    )
    text = header + str(detail)
    if len(text) > _FINDINGS_NOTE_CAP:
        text = text[:_FINDINGS_NOTE_CAP] + "\n...(생략됨)"
    return text


def run_plan_and_review(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                         job_name: str, attempt_id: str, request: str, probe_text: str,
                         on_progress: ProgressCallback | None,
                         processor_factory: orbit.ProcessorFactory | None,
                         max_fix_rounds: int, needs_resume_note: bool = False,
                         facts: trusted.Trusted | None = None,
                         prior_findings_note: str = "",
                         ) -> tuple[FullOutcome | None, Plan | None]:
    """Runs the plan station, then reviews that same change, from
    scratch -- used for a job's first plan attempt, a STAGE_PLAN_REVIEW
    resume, AND a STAGE_EARLY_STATION owner-request resume. Returns
    (None, plan) to keep going, or (the needs_owner/failed FullOutcome
    to return right now, None).

    `facts` is facts.md as captured when the facts gate passed in this
    same run; a resume that starts here checks and captures it again.
    The planner prompt gets those exact bytes, verified unchanged.

    `prior_findings_note` ("" for a job's first plan attempt -- only a
    STAGE_PLAN_REVIEW resume ever passes a non-empty one, see
    engine.application.pipeline_resume.plan_review_findings_note) is prepended to
    the FRONT of the planner's own prompt, ahead of everything else:
    the stale attempt's own review findings, whole, so the SAME
    rejected plan.json is never handed back to a planner with no idea
    what it was rejected for."""
    record_event(ledger, project, job_name, STAGE_PLAN, attempt_id, next_action="run_station",
            request=request, probe_text=probe_text, max_fix_rounds=max_fix_rounds)
    repo = config.projects[project].repo
    if facts is None:
        problems, facts = checked_facts(repo, job_name)
        if facts is None:
            return FullOutcome("failed", "facts:" + "; ".join(problems), attempt_id, None), None
    facts_md = trusted_text(facts, facts_path(repo, job_name))
    if facts_md is None:
        # facts.md changed after its check: stop for the owner; the
        # resume reruns this station, which checks facts.md again.
        reason = "facts_changed_after_check"
        record_event(ledger, project, job_name, STAGE_PLAN_REVIEW, attempt_id, stop_reason=reason,
                request=request, probe_text=probe_text, max_fix_rounds=max_fix_rounds)
        return FullOutcome("needs_owner", reason, attempt_id, None), None
    base_plan_prompt = planner_prompt(request, job_name, facts_md, skill_catalog_text(config),
                                      probe_text, build_prefixes(config),
                                      config.projects[project].manifest)
    if prior_findings_note:
        base_plan_prompt = f"{prior_findings_note}\n\n{base_plan_prompt}"
    # A STAGE_PLAN_REVIEW resume reruns this on the SAME (never-closed)
    # attempt -- same idempotent-request_id reasoning as _run_probe's own
    # retry note: an unchanged prompt would collide with the first
    # attempt's own already-"done" run_station request. The previous
    # (review-rejected, never committed) plan.json, still sitting in the
    # working tree from that first attempt's own merge, makes the retry's
    # payload different and shows the worker what it wrote last time.
    try:
        previous_plan_json = plan_path(repo, job_name).read_text(encoding="utf-8")
    except OSError:
        previous_plan_json = ""
    plan_prompt_text = (
        f"{base_plan_prompt}\n\n이전 시도의 plan.json(검토를 통과하지 못했다. 고쳐 다시 "
        f"쓴다):\n{previous_plan_json}" if previous_plan_json else base_plan_prompt
    )
    # The plan station's own context, and the plan review's, before the
    # planner runs.
    plan_gate = station_context_gate(config, ledger, project, attempt_id, "plan", request,
                                     review_query=request)
    if plan_gate.unmet is not None:
        return stop_early_station_for_unmet_context(
            ledger, project, attempt_id, job_name, "plan", request, max_fix_rounds,
            plan_gate.unmet, probe_text=probe_text,
        ), None
    plan_task_text = plan_gate.prompt(plan_prompt_text)
    plan_task_text = append_owner_request_resume_note(
        config, attempt_id, PLANNER_ROLE, plan_task_text, needs_resume_note)
    outcome = run_and_merge(config, ledger, fakes, project, attempt_id, "plan",
                             PLANNER_ROLE, plan_task_text,
                             on_progress, processor_factory)
    full = station_outcome_to_full(outcome, "plan")
    if full is not None:
        record_early_station_stop(ledger, project, attempt_id, job_name, "plan",
                                   outcome.reason, outcome.pending_requests, request=request,
                                   max_fix_rounds=max_fix_rounds,
                                   probe_text=probe_text)
        return full, None
    try:
        plan = load_plan(repo, job_name)
    except PlanError as exc:
        reason = f"plan:{exc.reason}"
        record_plan_check_failure(ledger, project, attempt_id, check=exc.reason, detail=exc.detail)
        record_event(ledger, project, job_name, STAGE_PLAN_REVIEW, attempt_id, stop_reason=reason,
                request=request, probe_text=probe_text, max_fix_rounds=max_fix_rounds)
        return FullOutcome("failed", reason, attempt_id, None), None
    problems = validate_plan(plan, repo, build_prefixes(config))
    problems += validate_tools(plan, config)
    if problems:
        reason = "plan:" + "; ".join(problems)
        record_plan_check_failure(ledger, project, attempt_id, check="plan_invalid",
                                   detail="; ".join(problems))
        record_event(ledger, project, job_name, STAGE_PLAN_REVIEW, attempt_id, stop_reason=reason,
                request=request, probe_text=probe_text, max_fix_rounds=max_fix_rounds)
        return FullOutcome("failed", reason, attempt_id, None), None

    # Review the planner's own merged-but-uncommitted change (plan.json +
    # tests/scenes/) BEFORE it is committed as the orchestrator's own plan
    # trail -- otherwise it becomes every later reviewer's base_head
    # (nothing to diff against) and is never judged by anyone (Codex
    # finding). One plain review Stage on this same attempt; the diff it
    # sees is exactly base_head..current (this merge's own files, nothing
    # else has touched the tree yet).
    plan_review_outcome = orbit.continue_job(
        config, ledger, fakes_for(fakes, attempt_id), project, attempt_id,
        [orbit.Stage("review", review_payload(config, ledger, project, plan, request))],
        processor_factory=processor_factory, on_progress=on_progress,
    )
    if plan_review_outcome.status != "done":
        record_event(ledger, project, job_name, STAGE_PLAN_REVIEW, attempt_id,
                stop_reason="plan_review_failed", request=request, probe_text=probe_text,
                max_fix_rounds=max_fix_rounds)
        return FullOutcome("needs_owner", "plan_review_failed", attempt_id, None), None

    commit_job_trail(config, ledger, project, attempt_id, job_name,
                      f"gongbang: {job_name} - plan", station="plan", role=PLANNER_ROLE)
    record_plan_reviewed(ledger, project, attempt_id, job_name, repo)
    return None, plan



def record_plan_reviewed(ledger: Ledger, project: str, attempt_id: str,
                          job_name: str, repo: Path) -> None:
    """The one durable record of which plan.json + scene files a
    reviewer last approved (engine.domain.plan.record_plan_reviewed), read
    back by maybe_rerun_plan_review on a later resume."""
    from engine.domain import plan as plan_mod

    faults.flow_boundary("full_orbit.plan_reviewed")
    plan_mod.record_plan_reviewed(ledger, project, attempt_id, repo, job_name)



def maybe_rerun_plan_review(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                             job_name: str, attempt_id: str, plan: Plan,
                             on_progress: ProgressCallback | None,
                             processor_factory: orbit.ProcessorFactory | None,
                             ) -> FullOutcome | None:
    """None to keep going -- either no "plan.reviewed" event was ever
    recorded for this job, or plan.json's current digest still matches the one
    last reviewed. Otherwise the needs_owner FullOutcome to return
    right now: the owner edited plan.json (e.g. fixing a scene the
    planner got wrong) between a needs_owner stop and this `gb2
    continue` -- that edit was never looked at by a reviewer, so this
    reruns the SAME review stage run_plan_and_review's own first pass
    already runs, on `attempt_id` (still the shared early attempt --
    only ever called from the STAGE_DESIGN_APPROVAL resume, before any
    task attempt opens), and records a fresh "plan.reviewed" once it
    passes."""
    from engine.domain.plan import plan_digest

    repo = config.projects[project].repo
    current_digest = plan_digest(plan_path(repo, job_name))
    reviewed_digest: object = None
    for _seq, event in ledger.events(project, "plan.reviewed"):
        if event.payload.get("job") == job_name:
            reviewed_digest = event.payload.get("digest")
    if reviewed_digest is None or reviewed_digest == current_digest:
        return None
    review_gate = station_context_gate(config, ledger, project, attempt_id, "review", plan.goal,
                                       planned_skills(config, plan, "review"))
    if review_gate.unmet is not None:
        return FullOutcome("needs_owner", review_gate.unmet, attempt_id, None)
    plan_review_outcome = orbit.continue_job(
        config, ledger, fakes_for(fakes, attempt_id), project, attempt_id,
        [orbit.Stage("review", review_payload(config, ledger, project, plan, plan.goal,
                                              context=review_gate.context))],
        processor_factory=processor_factory, on_progress=on_progress,
    )
    if plan_review_outcome.status != "done":
        return FullOutcome("needs_owner", "plan_review_failed", attempt_id, None)
    # Commit the re-reviewed plan.json/scene bytes to the job trail
    # BEFORE recording plan.reviewed -- same ordering _run_plan_and_
    # review's own first pass uses (commit, then record). Without
    # this, the owner's edit that triggered this re-review was never
    # committed anywhere: it only exists as an uncommitted change in
    # the real repo, which engine.application.task_pipeline's own "close" stage (git
    # checkout/clean to HEAD) wipes right back out the moment this
    # attempt closes -- so a later build/merge/commit would run
    # against the OLD, pre-edit plan.json even though plan.reviewed
    # now names the new digest as approved.
    commit_job_trail(config, ledger, project, attempt_id, job_name,
                      f"gongbang: {job_name} - plan", station="plan", role=PLANNER_ROLE)
    record_plan_reviewed(ledger, project, attempt_id, job_name, repo)
    return None



def probe_plan_design_close(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                             job_name: str, attempt_id: str, request: str,
                             max_fix_rounds: int, on_progress: ProgressCallback | None,
                             processor_factory: orbit.ProcessorFactory | None, *,
                             start: str = "probe", probe_text: str = "",
                             needs_resume_note: bool = False,
                             facts: trusted.Trusted | None = None,
                             prior_findings_note: str = "") -> FullOutcome:
    """probe -> plan+review -> (design, if plan.ui) -> close -> build
    loop. `start` is "probe" (a fresh job, or a STAGE_PROBE_RETRY
    resume), "plan" (a STAGE_PLAN_REVIEW resume -- probe already
    succeeded and stays as-is, `probe_text` is its already-committed
    logs, read by the resume caller), or "design" (a STAGE_EARLY_
    STATION resume for station=="design" -- plan.json is already
    committed by then, so the plan is reloaded from disk rather than
    rerunning the planner at all; `probe_text` is unused). `needs_
    resume_note`, only ever True for a STAGE_EARLY_STATION owner-
    request resume, is given ONLY to whichever station `start` names --
    the one THIS resume is actually for -- never to a later station in
    the same call, which runs fresh regardless. `prior_findings_note`
    ("" unless `start == "plan"` from a STAGE_PLAN_REVIEW resume) is
    passed straight to run_plan_and_review -- see that function's own
    docstring."""
    if start == "probe":
        full, probe_text = _run_probe(config, ledger, fakes, project, job_name, attempt_id,
                                      request, on_progress, processor_factory, max_fix_rounds,
                                      needs_resume_note=needs_resume_note)
        if full is not None:
            return full

    if start == "design":
        repo = config.projects[project].repo
        try:
            plan = load_plan(repo, job_name)
        except PlanError as exc:
            return FullOutcome("failed", f"plan:{exc.reason}", attempt_id, None)
    else:
        full, plan = run_plan_and_review(
            config, ledger, fakes, project, job_name, attempt_id, request, probe_text,
            on_progress, processor_factory, max_fix_rounds,
            needs_resume_note=(needs_resume_note if start == "plan" else False), facts=facts,
            prior_findings_note=(prior_findings_note if start == "plan" else ""))
        if full is not None:
            return full
        assert plan is not None

    if plan.ui:
        record_event(ledger, project, job_name, STAGE_DESIGN, attempt_id, next_action="run_station",
                request=request, max_fix_rounds=max_fix_rounds)
        usable_skills, _needs_install = classify_skill_tools(plan, config)
        design_prompt_text = designer_prompt(request, job_name, plan.goal)
        design_gate = station_context_gate(config, ledger, project, attempt_id, "design",
                                           request, planned_skills(config, plan, "design"))
        if design_gate.unmet is not None:
            return stop_early_station_for_unmet_context(
                ledger, project, attempt_id, job_name, "design", request, max_fix_rounds,
                design_gate.unmet,
            )
        design_task_text = design_gate.prompt(design_prompt_text)
        design_task_text = append_owner_request_resume_note(
            config, attempt_id, DESIGNER_ROLE, design_task_text,
            needs_resume_note if start == "design" else False)
        outcome = run_and_merge(config, ledger, fakes, project, attempt_id, "design",
                                 DESIGNER_ROLE, design_task_text,
                                 on_progress, processor_factory,
                                 extra_payload={"tools": tools_for_station(
                                     plan, "design", usable_skills=usable_skills)})
        full = station_outcome_to_full(outcome, "design")
        if full is not None:
            record_early_station_stop(ledger, project, attempt_id, job_name, "design",
                                       outcome.reason, outcome.pending_requests, request=request,
                                       max_fix_rounds=max_fix_rounds)
            return full
        commit_job_trail(config, ledger, project, attempt_id, job_name,
                          f"gongbang: {job_name} - design")
        if not check_design_approved(ledger, project, job_name):
            record_event(ledger, project, job_name, STAGE_DESIGN_APPROVAL, attempt_id,
                    stop_reason="design_approval", max_fix_rounds=max_fix_rounds)
            return FullOutcome("needs_owner", "design_approval", attempt_id, None)

    run = Run(config, ledger, fakes, project, job_name, on_progress, processor_factory)
    return close_early_attempt_then_tasks(run, attempt_id, plan, max_fix_rounds)



def close_early_attempt_then_tasks(run: "Run", attempt_id: str, plan: Plan,
                                    max_fix_rounds: int, *, record: bool = True) -> FullOutcome:
    """Planning (and design approval, for a UI plan) is finished: record
    that, close the shared early attempt, then run the plan's tasks. A
    crash anywhere in the close resumes here (STAGE_EARLY_CLOSE), so the
    same close request is finished, never a new one opened."""
    if record:
        record_event(run.ledger, run.project, run.job, STAGE_EARLY_CLOSE, attempt_id,
                max_fix_rounds=max_fix_rounds)
    close_outcome = close_attempt(run.config, run.ledger, run.fakes, run.project, attempt_id,
                                   run.job, run.on_progress, run.processor_factory)
    if close_outcome.status != "done":
        return FullOutcome("failed", f"close:{close_outcome.reason}", attempt_id, None)
    return finish_from_tasks(run.config, run.ledger, run.fakes, run.project, run.job, plan,
                              ordered_tasks(plan), max_fix_rounds, run.on_progress,
                              run.processor_factory)



def run_facts(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
              job_name: str, attempt_id: str, request: str,
              on_progress: ProgressCallback | None,
              processor_factory: orbit.ProcessorFactory | None,
              max_fix_rounds: int, needs_resume_note: bool = False,
              ) -> FullOutcome | None:
    """Runs the facts station from scratch -- used both for a job's
    very first call (run_full_job) and a STAGE_EARLY_STATION resume.
    Returns None to keep going (facts merged and committed -- check_
    facts' own content validation is the caller's job, same as
    before), or the needs_owner/failed FullOutcome to return right
    now."""
    record_event(ledger, project, job_name, STAGE_FACTS, attempt_id, next_action="run_station",
            request=request, max_fix_rounds=max_fix_rounds)
    facts_gate = station_context_gate(config, ledger, project, attempt_id, "facts", request)
    if facts_gate.unmet is not None:
        return stop_early_station_for_unmet_context(
            ledger, project, attempt_id, job_name, "facts", request, max_fix_rounds,
            facts_gate.unmet)
    facts_task_text = facts_gate.prompt(facts_prompt(request, job_name))
    facts_task_text = append_owner_request_resume_note(
        config, attempt_id, FACTS_ROLE, facts_task_text, needs_resume_note)
    outcome = run_and_merge(config, ledger, fakes, project, attempt_id, "facts",
                             FACTS_ROLE, facts_task_text, on_progress,
                             processor_factory)
    full = station_outcome_to_full(outcome, "facts")
    if full is not None:
        record_early_station_stop(ledger, project, attempt_id, job_name, "facts",
                                   outcome.reason, outcome.pending_requests, request=request,
                                   max_fix_rounds=max_fix_rounds)
        return full
    commit_job_trail(config, ledger, project, attempt_id, job_name, f"gongbang: {job_name} - facts")
    return None



def run_full_job(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                 job_name: str, request: str, *, max_fix_rounds: int = 2,
                 on_progress: ProgressCallback | None = None,
                 processor_factory: orbit.ProcessorFactory | None = None) -> FullOutcome:
    intent = JobPosition(job=job_name, stage=STAGE_FACTS, attempt_id=None,
                         extra={"request": request, "max_fix_rounds": max_fix_rounds,
                                "next_action": "run_station"})
    attempt_id = open_with_intent(config, ledger, project, intent, EARLY_ROLES)
    run = Run(config, ledger, fakes, project, job_name, on_progress, processor_factory)
    return facts_then_rest(run, attempt_id, request, max_fix_rounds)



def facts_then_rest(run: "Run", attempt_id: str, request: str, max_fix_rounds: int, *,
                     needs_resume_note: bool = False) -> FullOutcome:
    """facts -> check_facts -> probe -> plan -> design -> tasks."""
    full = run_facts(run.config, run.ledger, run.fakes, run.project, run.job, attempt_id,
                      request, run.on_progress, run.processor_factory, max_fix_rounds,
                      needs_resume_note=needs_resume_note)
    if full is not None:
        return full
    problems, facts = checked_facts(run.repo, run.job)
    if facts is None:
        return FullOutcome("failed", "facts:" + "; ".join(problems), attempt_id, None)
    return probe_plan_design_close(run.config, run.ledger, run.fakes, run.project, run.job,
                                    attempt_id, request, max_fix_rounds, run.on_progress,
                                    run.processor_factory, start="probe", facts=facts)


__all__ = [
    "run_full_job", "probe_plan_design_close", "close_early_attempt_then_tasks",
    "facts_then_rest", "maybe_rerun_plan_review", "append_owner_request_resume_note",
    "plan_review_findings_note", "plan_check_failure_note", "record_plan_check_failure",
    "PLAN_CHECK_EVIDENCE_KIND",
]
