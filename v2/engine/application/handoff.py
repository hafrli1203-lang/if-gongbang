"""handoff -- writes the job's report.md, the last thing full_orbit does
before report/close (ENGINE.md flow: ... -> acceptance ->
engine.application.handoff.write_handoff -> report/close). Plain function, not an
engine.application.processor step: called once, directly, after
engine.application.acceptance.run_acceptance has already produced an AcceptanceResult
for this attempt.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from engine.application.acceptance import AcceptanceResult, acceptance_path
from engine.config import EngineConfig
from engine.domain.records import Event
from engine.domain.plan import (
    classify_skill_tools, plan_digest, plan_path as _plan_json_path, requirement_coverers,
)

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger
    from engine.domain.plan import Plan

_RUN_SCRIPT_CANDIDATES = ("dev", "start", "build", "test")
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
_SCREEN_SUBDIRS = ("screens", "design")

# write_handoff runs strictly BEFORE the app-wide security check and the
# job's release decision (engine.application.pipeline._finish_after_handoff ->
# finalize_release) -- see write_handoff's own interim note and
# write_final_section's own docstring.
_FINAL_SECTION_HEADING = "## 최종 결과"
_RELEASE_DECISION_TEXT = {
    "shipped": "배포됨", "skipped": "건너뜀",
    "not_configured": "배포 설정 없음 (배포 대상 아님)", "failed": "배포 거부/실패",
}


@dataclass(frozen=True)
class ToolUsageRow:
    """One plan.tools entry's actual-use row for the handoff report's
    "도구표 대 실제 사용" section. `call_count` is the number of
    'policy.decided' events this job's own attempts recorded for an
    mcp tool whose action.target matches this row's name (kind="mcp"),
    or the number of stations that actually received this skill via a
    'context.injected' event (kind="skill", W1's module)."""

    kind: str
    name: str
    stations: tuple[str, ...]
    why: str
    call_count: int
    used_in_stations: tuple[str, ...]
    # True for a "skill" row whose name is not in config.skill_index_path
    # (engine.domain.plan.classify_skill_tools) -- never granted to any
    # station; the handoff names it "설치 필요(사람 승인)" instead of
    # reporting zero use, which would look like a planned tool that
    # simply went unused rather than one that was never installable.
    needs_install: bool = False


@dataclass(frozen=True)
class ToolReconciliation:
    rows: tuple[ToolUsageRow, ...]
    # "mcp:<server>" names a server actually called that no plan.tools
    # entry ever named -- can only happen if a station was independently
    # granted access some other way; listed rather than silently
    # dropped, per plan §W2 "unplanned calls are listed".
    unplanned: tuple[str, ...]


def _job_attempt_ids(ledger: Ledger, project: str, job: str) -> set[str]:
    return {a.attempt_id for a in ledger.attempts_for_job_name(project, job)}


def reconcile_tools(ledger: Ledger, project: str, job: str, plan: "Plan", *,
                    needs_install: "frozenset[str] | None" = None) -> ToolReconciliation:
    """Count actual tool use for this job's own attempts against
    plan.tools: imported gate decisions for mcp calls, and
    'context.injected' events (W1's engine.infrastructure.context, kind="skill") for
    which stations actually received each planned skill. Never trusts
    any OTHER project's/job's events -- only attempts
    ledger.attempts_for_job_name(project, job) itself returns (plus,
    for context.injected events recorded before their own attempt
    existed yet -- see the attempt_id=None note below).

    A worker's own mcp calls never reach the ledger as a literal
    'policy.decided' event: engine.interface.hook_gate (the PreToolUse hook, run
    inside the worker sandbox) has no ledger access at all and records
    through engine.domain.policy.FileSink into the station's own gate.log
    instead; engine.application.steps.worker_step._import_gate_log is what turns each
    line of that log into a ledger event, and it always does so as
    kind='gate_log.decision' with the original kind preserved as that
    event's own payload["orig_kind"] (see _validate_gate_log_payload).
    So an mcp call is counted here from 'gate_log.decision' events
    whose orig_kind is 'policy.decided' and whose tool is 'mcp' -- the
    literal 'policy.decided' kind is never actually written to the
    ledger by anything in this codebase.

    A context.injected event's own attempt_id is None for the "build"
    and "review" stations (engine.application.pipeline._prompt_with_context is
    called with attempt_id=None there: "build" builds a task's prompt
    text before that task's own attempt is opened; "review" is
    deliberately given no attempt_id of its own even though one
    already exists, by the same design). Those events carry no job
    identifier either, so they are attributed to whichever job's own
    'job.opened' event was the most recently seen one in ledger seq
    order at the point they were appended -- correct as long as a
    project never runs two jobs' attempts interleaved (true here: every
    job runs its attempts one at a time, each one opened then run to
    completion before the next)."""
    attempt_ids = _job_attempt_ids(ledger, project, job)

    mcp_calls: dict[str, int] = {}
    for _seq, event in ledger.events(project, "gate_log.decision"):
        if event.attempt_id not in attempt_ids:
            continue
        if event.payload.get("orig_kind") != "policy.decided":
            continue
        if event.payload.get("tool") != "mcp":
            continue
        target = event.payload.get("target")
        if isinstance(target, str) and target:
            mcp_calls[target] = mcp_calls.get(target, 0) + 1

    skill_stations: dict[str, set[str]] = {}
    current_job: str | None = None
    for _seq, event in ledger.events(project):
        if event.kind == "job.opened":
            name = event.payload.get("job_name")
            if isinstance(name, str):
                current_job = name
            continue
        if event.kind != "context.injected":
            continue
        if event.attempt_id not in attempt_ids and not (
            event.attempt_id is None and current_job == job
        ):
            continue
        station = str(event.payload.get("station", ""))
        names = event.payload.get("skills", [])
        if isinstance(names, list):
            for name in names:
                skill_stations.setdefault(str(name), set()).add(station)

    rows: list[ToolUsageRow] = []
    planned_mcp_names: set[str] = set()
    needs_install_names = needs_install or frozenset()
    # getattr, not plan.tools: handoff/acceptance duck-type a
    # Plan-shaped object (see module docstring and tests/plan_stub.py's
    # own docstring) rather than hard-importing engine.domain.plan.Plan, so a
    # caller's Plan-shaped stub that predates this field still works,
    # with zero planned tools.
    for tool in getattr(plan, "tools", ()):
        if tool.kind == "mcp":
            planned_mcp_names.add(tool.name)
            rows.append(ToolUsageRow(
                kind="mcp", name=tool.name, stations=tool.stations, why=tool.why,
                call_count=mcp_calls.get(tool.name, 0), used_in_stations=(),
            ))
        elif tool.name in needs_install_names:
            rows.append(ToolUsageRow(
                kind="skill", name=tool.name, stations=tool.stations, why=tool.why,
                call_count=0, used_in_stations=(), needs_install=True,
            ))
        else:
            used = tuple(sorted(skill_stations.get(tool.name, set())))
            rows.append(ToolUsageRow(
                kind="skill", name=tool.name, stations=tool.stations, why=tool.why,
                call_count=len(used), used_in_stations=used,
            ))

    unplanned = tuple(
        f"mcp:{name}" for name in sorted(mcp_calls) if name not in planned_mcp_names
    )
    return ToolReconciliation(rows=tuple(rows), unplanned=unplanned)


def _tool_table(reconciliation: ToolReconciliation) -> str:
    if not reconciliation.rows and not reconciliation.unplanned:
        return "- 계획된 도구 없음"
    rows = ["| 종류 | 이름 | 대상 칸 | 이유 | 실제 사용 |", "|---|---|---|---|---|"]
    for row in reconciliation.rows:
        if row.kind == "mcp":
            actual = f"{row.call_count}회 호출" if row.call_count else "사용 안 함"
        elif row.needs_install:
            actual = "설치 필요(사람 승인)"
        else:
            actual = (f"주입됨 ({', '.join(row.used_in_stations)})" if row.used_in_stations
                      else "주입 안 됨")
        rows.append(f"| {row.kind} | {row.name} | {', '.join(row.stations)} | {row.why} | {actual} |")
    lines = ["\n".join(rows)]
    if reconciliation.unplanned:
        lines.append("\n계획에 없던 호출: " + ", ".join(reconciliation.unplanned))
    return "\n".join(lines)


def report_path(config: EngineConfig, project: str, job: str) -> Path:
    return config.projects[project].repo / ".gongbang" / job / "report.md"


def _how_to_run(config: EngineConfig, project: str) -> str:
    """package.json's own "scripts" section, when the repo has one --
    the only place this codebase can read a run command FROM without
    guessing or hardcoding a stack-specific default (MASTER_RULES: no
    invented defaults). No package.json, or no scripts, or any read/
    parse failure -> an explicit "확인 필요" line instead of a made-up
    command."""
    package_json = config.projects[project].repo / "package.json"
    if not package_json.exists():
        return "확인 필요: package.json이 없어 실행 방법을 코드에서 읽어내지 못했습니다."
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "확인 필요: package.json을 읽지 못했습니다."
    scripts = data.get("scripts")
    if not isinstance(scripts, dict) or not scripts:
        return "확인 필요: package.json에 scripts가 없습니다."
    lines = []
    for name in _RUN_SCRIPT_CANDIDATES:
        if name in scripts:
            lines.append(f"- `npm run {name}` -- {scripts[name]}")
    if not lines:
        # package.json has scripts, just none of the common names:
        # list every script rather than silently reporting nothing.
        lines = [f"- `npm run {name}` -- {cmd}" for name, cmd in scripts.items()]
    return "\n".join(lines)


def _screens_section(config: EngineConfig, project: str, job: str) -> str:
    """Every image file (png/jpg/jpeg/webp) under
    .gongbang/<job>/screens/ and .gongbang/<job>/design/, as markdown
    image links with paths relative to the repo -- "화면 없음" plainly
    (not hidden/skipped) when neither directory holds one, so a job
    with no actual screenshot evidence reads as missing, not as a
    section that was simply left out."""
    repo = config.projects[project].repo
    job_dir = repo / ".gongbang" / job
    images: list[Path] = []
    for sub in _SCREEN_SUBDIRS:
        base = job_dir / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix.lower() in _IMAGE_EXTENSIONS:
                images.append(path.relative_to(repo))
    if not images:
        return "화면 없음"
    return "\n".join(f"![{p.name}]({p.as_posix()})" for p in images)


def _scene_table(ledger: Ledger, project: str, job: str, plan: "Plan",
                 acc: AcceptanceResult) -> str:
    """requirement(plan.requirements) <-> scene/task <-> pass/fail,
    rendered from the SAME end-to-end evidence graph (engine.
    evidence_graph.build) engine.application.release.job_certificate_status gates
    release on -- P4 (ENGINE.md s20 flow-map (라)): one place computes
    "is this requirement actually covered," never two that could drift
    apart. A plan with no requirements yet (predates the earlier
    requirements/covers fields) falls back to plan.acceptance paired
    positionally with plan.scenes, same as before that field existed --
    and never touches the ledger at all in that case (several callers
    in this module's own test suite pass a hand-built AcceptanceResult
    that was never appended as a real 'acceptance.recorded' event, so
    building a graph for them would just find nothing)."""
    requirements = getattr(plan, "requirements", ())
    if not requirements:
        return _scene_table_legacy(plan, acc)
    from engine.domain import evidence_graph

    graph = evidence_graph.build(ledger, project, job, plan)
    by_requirement = {r.requirement_id: r for r in graph.requirements}
    coverers = requirement_coverers(plan)
    rows = ["| 요구사항(ID) | 덮는 장면/작업 | 상태 | 비고 |", "|---|---|---|---|"]
    for requirement in requirements:
        pairs = coverers.get(requirement.id, ())
        if not pairs:
            rows.append(f"| {requirement.id}: {requirement.text} | - | 덮이지 않음 | - |")
            continue
        covered_by = ", ".join(f"{kind}:{cid}" for kind, cid in pairs)
        ev = by_requirement.get(requirement.id)
        if ev is not None and ev.scenes:
            status = "통과" if all(s.result == "pass" for s in ev.scenes) else "실패"
        else:
            # Covered only by a task, never a scene -- owner decision:
            # NEVER sufficient on its own (the same rule engine.
            # evidence_graph.missing enforces at release time). A
            # task's own attempt reaching "done" only means its own
            # review/bench passed, never that any scene actually
            # exercised the requirement's real behavior.
            status = "실패(장면 없음)"
        rows.append(f"| {requirement.id}: {requirement.text} | {covered_by} | {status} | |")
    return "\n".join(rows)


def _scene_table_legacy(plan: "Plan", acc: AcceptanceResult) -> str:
    by_id = {s.id: s for s in acc.scenes}
    rows = ["| 요구사항(수락 조건) | 장면 | 상태 | 비고 |", "|---|---|---|---|"]
    acceptance = list(plan.acceptance)
    scenes = list(plan.scenes)
    width = max(len(acceptance), len(scenes))
    for i in range(width):
        req = acceptance[i] if i < len(acceptance) else ""
        scene = scenes[i] if i < len(scenes) else None
        if scene is None:
            rows.append(f"| {req} | - | - | - |")
            continue
        result = by_id.get(scene.id)
        status = "통과" if result and result.passed else "실패"
        note = f"{scene.actor_does} -> {scene.app_shows}"
        rows.append(f"| {req} | {scene.id} | {status} | {note} |")
    return "\n".join(rows)


def _open_items(plan: "Plan", acc: AcceptanceResult) -> str:
    failed_scenes = [s for s in acc.scenes if not s.passed]
    lines: list[str] = []
    if not acc.full_suite_passed:
        lines.append("- 전체 시험(test_cmd)이 통과하지 못했습니다.")
    for s in failed_scenes:
        lines.append(f"- 장면 `{s.id}` 실패 (returncode={s.returncode}): "
                     f"{s.tail[-300:] if s.tail else ''}")
    if not plan.scenes:
        lines.append("- 통과 장면이 하나도 없어 수락 자체가 되지 않았습니다.")
    if not lines:
        lines.append("- 없음 (모든 장면과 전체 시험 통과)")
    return "\n".join(lines)


def write_handoff(config: EngineConfig, ledger: Ledger, project: str,
                  attempt_id: str, job: str, plan: "Plan",
                  acc: AcceptanceResult) -> Path:
    path = report_path(config, project, job)
    path.parent.mkdir(parents=True, exist_ok=True)
    acc_path = acceptance_path(config, project, job)
    _usable_skills, needs_install = classify_skill_tools(plan, config)
    reconciliation = reconcile_tools(ledger, project, job, plan, needs_install=needs_install)
    # plan.json's own digest (engine.domain.plan.plan_digest) right now -- the
    # same value engine.application.pipeline's record_plan_reviewed stored as
    # plan.reviewed right after plan review passed, UNLESS a later
    # build task touched plan.json or a scene file after that (then
    # this one differs, which is itself a fact worth a reader seeing
    # plainly rather than a stale "it was reviewed" number).
    digest = plan_digest(_plan_json_path(config.projects[project].repo, job))
    body = f"""# 인계 보고 -- {job}

(진행 중 -- 이 보고는 보안 점검과 배포 결정 전에 쓰였습니다. 둘 다 끝나면
"최종 결과" 절이 아래에 덧붙습니다.)

attempt: {attempt_id}

## 목표
{plan.goal}

## 계획 다이제스트
{digest}

## 요구사항 ↔ 통과 장면
{_scene_table(ledger, project, job, plan, acc)}

## 전체 시험
{"통과" if acc.full_suite_passed else "실패"} (상세: `{acc_path.relative_to(config.projects[project].repo)}`)

## 실행 방법
{_how_to_run(config, project)}

## 화면
{_screens_section(config, project, job)}

## 도구표 대 실제 사용
{_tool_table(reconciliation)}

## 남은 항목
{_open_items(plan, acc)}
"""
    path.write_text(body, encoding="utf-8")
    # The only ledger signal that the WHOLE job's handoff report (not
    # just one of its many attempts -- facts/plan/design/each task each
    # open their own) has been written: every attempt's own "report"
    # stage appends a generic 'report.written' event (engine.application.steps.
    # report), so that kind alone cannot tell a job's final handoff
    # apart from an in-progress task's per-attempt report. This event
    # alone is NOT completion, though -- the app-wide security check
    # and the job's release decision both still run after it
    # (engine.application.pipeline._finish_after_handoff); engine.interface.board's own
    # "완료" status is keyed off a later release_decision/failure event,
    # never off this one by itself (see board._derive_job_status's own
    # docstring for why).
    ledger.append(Event(
        project=project, kind="job.handoff_written", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id, payload={"job": job},
    ))
    return path


def _strip_final_section(body: str) -> str:
    """`body` with everything from `_FINAL_SECTION_HEADING` onward cut
    off -- a no-op if that heading is not present at all (the common
    case: the FIRST call for a job). write_final_section calls this on
    whatever report.md already holds before appending, so a crash-
    resumed re-run of the SAME decision (finalize_release's own
    idempotent retry, see its docstring) rewrites one final section,
    never appends a second one under the first."""
    idx = body.find(f"\n{_FINAL_SECTION_HEADING}")
    return body if idx == -1 else body[:idx]


def write_final_section(config: EngineConfig, ledger: Ledger, project: str,
                        job: str, decision: str) -> Path:
    """Appends report.md's own final section once the job's release
    decision is known for certain -- write_handoff runs strictly
    BEFORE the app-wide security check and
    the release decision (engine.application.pipeline._finish_after_handoff),
    so its own report, by itself, can say "아무 것도 남지 않음" (## 남은
    항목, _open_items) while security or release are, in fact, still
    pending or already failed -- an interim note on write_handoff's own
    body (see its own template) says so plainly; THIS function is what
    turns that into a real answer once there is one.

    Caller contract: engine.application.pipeline.finalize_release is the ONE
    place in this codebase that computes `decision` (shipped/skipped/
    failed/not_configured) for certain -- see that function's own
    docstring. It should call this (passing that same `decision`
    string) right after computing it, before finalize_release itself
    returns -- exactly once per terminal outcome, on all three of its
    own paths (shipped, rejected, and the plain report+close a project
    with no release track takes). Idempotent the same way
    finalize_release's own decide-close-record sequence already is:
    calling this again with the SAME decision (a crash-resumed retry)
    rewrites one identical section, never appends a second one (see
    _strip_final_section).

    Renders from the SAME engine.domain.evidence_graph this module's own
    requirement table already uses, so this section can never disagree
    with engine.application.release.job_certificate_status's own release gate: the
    security line reads graph.certificate_fingerprint/
    certificate_policy_hash (both non-"" iff the job's newest
    app_security.recorded event passed), and "증거 완전성" is literally
    evidence_graph.missing(graph) -- empty only when that SAME gate
    would have allowed release. A missing/unparseable plan.json
    degrades to a Plan with zero requirements (engine.application.release's own
    _plan_or_empty, re-done locally here for the same "that module is
    outside this one's owned scope" reason -- see engine.application.release.
    _plan_or_empty's own docstring)."""
    from engine.domain import evidence_graph
    from engine.domain.plan import Plan as _Plan
    from engine.domain.plan import PlanError, load_plan

    path = report_path(config, project, job)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    body = _strip_final_section(existing)

    try:
        plan = load_plan(config.projects[project].repo, job)
    except PlanError:
        plan = _Plan(goal="", ui=False, scenes=(), tasks=(), acceptance=())
    graph = evidence_graph.build(ledger, project, job, plan)
    gaps = evidence_graph.missing(graph)

    security_line = ("통과" if graph.certificate_fingerprint and graph.certificate_policy_hash
                     else "실패 또는 기록 없음")
    decision_line = _RELEASE_DECISION_TEXT.get(decision, decision)
    evidence_line = "완전함" if not gaps else "; ".join(gaps)

    section = (
        f"\n{_FINAL_SECTION_HEADING}\n\n"
        f"보안 점검: {security_line}\n\n"
        f"배포 결정: {decision_line}\n\n"
        f"증거 완전성: {evidence_line}\n"
    )
    path.write_text(body.rstrip("\n") + "\n" + section, encoding="utf-8")
    return path
