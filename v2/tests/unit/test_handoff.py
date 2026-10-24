"""Unit tests for engine.handoff.write_handoff: report.md content, built
from a Plan and an already-computed AcceptanceResult."""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

from types import SimpleNamespace

from engine.acceptance import AcceptanceResult, SceneResult, run_acceptance
from engine.handoff import reconcile_tools, report_path, write_handoff
from engine.ledger import Event
from engine.steps.open_job import open_job
from tests.conftest import Workspace
from tests.fixtures.sample_app import make_sample
from tests.plan_stub import Plan, Scene


def _sample_workspace(workspace: Workspace) -> Workspace:
    sample = make_sample(workspace.root / "sample", 1, Path(sys.executable))
    project_config = replace(
        workspace.config.projects["alpha"],
        repo=sample.repo, test_cmd=sample.test_cmd,
    )
    new_config = replace(
        workspace.config,
        projects={**workspace.config.projects, "alpha": project_config},
    )
    return Workspace(workspace.root, new_config, workspace.ledger,
                     sample.repo, workspace.beta)


def _plan() -> Plan:
    return Plan(
        goal="새 앱: 인사말 보여주기",
        ui=False,
        scenes=(
            Scene(id="s1", actor_does="이름을 입력한다", app_shows="Hello, World",
                 where="cli", cmd=(sys.executable, "-c", "pass")),
            Scene(id="s2", actor_does="빈 이름을 입력한다", app_shows="오류 메시지",
                 where="cli", cmd=(sys.executable, "-c", "raise SystemExit(1)")),
        ),
        tasks=(), acceptance=("인사말이 Hello, X 형태로 나온다", "빈 입력은 오류를 보여준다"),
    )


def test_report_has_goal_scene_table_and_run_instructions(workspace: Workspace) -> None:
    ws = _sample_workspace(workspace)
    attempt_id = open_job(ws.config, ws.ledger, "alpha", "job1", "full",
                          roles=("builder",))
    plan = _plan()
    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, plan)

    path = write_handoff(ws.config, ws.ledger, "alpha", attempt_id, "job1", plan, acc)

    assert path == report_path(ws.config, "alpha", "job1")
    text = path.read_text(encoding="utf-8")
    assert "새 앱: 인사말 보여주기" in text
    assert "s1" in text and "s2" in text
    assert "통과" in text and "실패" in text
    assert "인사말이 Hello, X 형태로 나온다" in text
    assert "남은 항목" in text
    # scene s2 failed -> it must show up as an open item with its tail.
    assert attempt_id in text


def test_run_instructions_list_package_json_scripts(workspace: Workspace) -> None:
    ws = _sample_workspace(workspace)
    (ws.alpha / "package.json").write_text(
        '{"scripts": {"dev": "vite", "test": "pytest"}}\n', encoding="utf-8")
    attempt_id = open_job(ws.config, ws.ledger, "alpha", "job1", "full",
                          roles=("builder",))
    plan = replace(_plan(), scenes=(Scene(id="s1", actor_does="x", app_shows="x",
                                           where="cli", cmd=(sys.executable, "-c", "pass")),))
    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, plan)

    path = write_handoff(ws.config, ws.ledger, "alpha", attempt_id, "job1", plan, acc)
    text = path.read_text(encoding="utf-8")

    assert "npm run dev" in text
    assert "npm run test" in text


def test_run_instructions_say_확인_필요_without_package_json(workspace: Workspace) -> None:
    ws = _sample_workspace(workspace)
    attempt_id = open_job(ws.config, ws.ledger, "alpha", "job1", "full",
                          roles=("builder",))
    plan = replace(_plan(), scenes=(Scene(id="s1", actor_does="x", app_shows="x",
                                           where="cli", cmd=(sys.executable, "-c", "pass")),))
    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, plan)

    path = write_handoff(ws.config, ws.ledger, "alpha", attempt_id, "job1", plan, acc)
    text = path.read_text(encoding="utf-8")

    assert "확인 필요" in text


def test_open_items_empty_when_everything_passed(workspace: Workspace) -> None:
    ws = _sample_workspace(workspace)
    attempt_id = open_job(ws.config, ws.ledger, "alpha", "job1", "full",
                          roles=("builder",))
    plan = Plan(
        goal="x", ui=False,
        scenes=(Scene(id="s1", actor_does="x", app_shows="x", where="cli",
                     cmd=(sys.executable, "-c", "pass")),),
        tasks=(), acceptance=("x",),
    )
    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, plan)
    assert acc.passed is True

    path = write_handoff(ws.config, ws.ledger, "alpha", attempt_id, "job1", plan, acc)
    text = path.read_text(encoding="utf-8")

    assert "없음 (모든 장면과 전체 시험 통과)" in text


def test_report_overwritten_by_a_later_write_for_the_same_job(workspace: Workspace) -> None:
    """write_handoff always overwrites report.md -- the file reflects the
    most recent AcceptanceResult passed to it, not the first one."""
    ws = _sample_workspace(workspace)
    attempt_id = open_job(ws.config, ws.ledger, "alpha", "job1", "full",
                          roles=("builder",))
    plan = Plan(
        goal="first", ui=False,
        scenes=(Scene(id="s1", actor_does="x", app_shows="x", where="cli",
                     cmd=(sys.executable, "-c", "pass")),),
        tasks=(), acceptance=("x",),
    )
    acc1 = AcceptanceResult(passed=True, scenes=(SceneResult("s1", True, 0, ""),),
                            full_suite_passed=True)
    write_handoff(ws.config, ws.ledger, "alpha", attempt_id, "job1", plan, acc1)

    plan2 = replace(plan, goal="second")
    acc2 = AcceptanceResult(passed=False, scenes=(SceneResult("s1", False, 1, "boom"),),
                            full_suite_passed=False)
    path = write_handoff(ws.config, ws.ledger, "alpha", attempt_id, "job1", plan2, acc2)

    text = path.read_text(encoding="utf-8")
    assert "second" in text
    assert "first" not in text
    assert "boom" in text


# ---- "## 화면" section --------------------------------------------------

def test_screens_section_says_화면_없음_when_nothing_saved(workspace: Workspace) -> None:
    ws = _sample_workspace(workspace)
    attempt_id = open_job(ws.config, ws.ledger, "alpha", "job1", "full",
                          roles=("builder",))
    plan = replace(_plan(), scenes=(Scene(id="s1", actor_does="x", app_shows="x",
                                           where="cli", cmd=(sys.executable, "-c", "pass")),))
    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, plan)

    path = write_handoff(ws.config, ws.ledger, "alpha", attempt_id, "job1", plan, acc)
    text = path.read_text(encoding="utf-8")

    assert "## 화면" in text
    assert "화면 없음" in text


def test_screens_section_lists_images_under_screens_and_design(workspace: Workspace) -> None:
    ws = _sample_workspace(workspace)
    job_dir = ws.alpha / ".gongbang" / "job1"
    (job_dir / "screens").mkdir(parents=True)
    (job_dir / "screens" / "home.png").write_bytes(b"fake-png")
    (job_dir / "design").mkdir(parents=True)
    (job_dir / "design" / "mock.jpg").write_bytes(b"fake-jpg")
    # A non-image file under the same folder must never show up as a
    # "screen" -- only the configured image extensions count.
    (job_dir / "design" / "design.md").write_text("# 시안\n", encoding="utf-8")

    attempt_id = open_job(ws.config, ws.ledger, "alpha", "job1", "full",
                          roles=("builder",))
    plan = replace(_plan(), scenes=(Scene(id="s1", actor_does="x", app_shows="x",
                                           where="cli", cmd=(sys.executable, "-c", "pass")),))
    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, plan)

    path = write_handoff(ws.config, ws.ledger, "alpha", attempt_id, "job1", plan, acc)
    text = path.read_text(encoding="utf-8")

    assert "화면 없음" not in text
    assert "](.gongbang/job1/screens/home.png)" in text
    assert "](.gongbang/job1/design/mock.jpg)" in text
    assert "design.md" not in text.split("## 화면")[1].split("## 도구표")[0]


def test_reconcile_counts_mcp_calls_from_imported_gate_log_decisions(workspace: Workspace) -> None:
    """A worker's real mcp call never reaches the ledger as a literal
    'policy.decided' event (engine.gate has no ledger access at all --
    see engine.handoff.reconcile_tools' own docstring); it is imported
    as 'gate_log.decision' with orig_kind='policy.decided'. This must
    be what reconcile_tools actually counts."""
    ws = _sample_workspace(workspace)
    attempt_id = open_job(ws.config, ws.ledger, "alpha", "job1", "full",
                          roles=("builder",))
    ws.ledger.append(Event(
        project="alpha", kind="gate_log.decision", actor_kind="gate_log",
        actor_id="builder", attempt_id=attempt_id,
        payload={"orig_kind": "policy.decided", "allowed": True, "reason": "ok",
                 "tool": "mcp", "target": "company-library",
                 "request_id": "r1", "log_line": 1},
    ))
    ws.ledger.append(Event(
        project="alpha", kind="gate_log.decision", actor_kind="gate_log",
        actor_id="builder", attempt_id=attempt_id,
        payload={"orig_kind": "policy.decided", "allowed": True, "reason": "ok",
                 "tool": "mcp", "target": "company-library",
                 "request_id": "r1", "log_line": 2},
    ))
    # A denied decision for an unrelated tool must not be counted as a use.
    ws.ledger.append(Event(
        project="alpha", kind="gate_log.decision", actor_kind="gate_log",
        actor_id="builder", attempt_id=attempt_id,
        payload={"orig_kind": "policy.decided", "allowed": False, "reason": "no",
                 "tool": "read", "path": "/x", "request_id": "r1", "log_line": 3},
    ))
    plan = SimpleNamespace(tools=(
        SimpleNamespace(kind="mcp", name="company-library", stations=("plan",), why="자료"),
    ))

    recon = reconcile_tools(ws.ledger, "alpha", "job1", plan)

    assert recon.rows[0].kind == "mcp"
    assert recon.rows[0].call_count == 2
    assert recon.unplanned == ()


def test_reconcile_skill_use_attributed_via_attempt_id(workspace: Workspace) -> None:
    ws = _sample_workspace(workspace)
    attempt_id = open_job(ws.config, ws.ledger, "alpha", "job1", "full",
                          roles=("builder",))
    ws.ledger.append(Event(
        project="alpha", kind="context.injected", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"station": "plan", "items": [], "skills": ["기획"], "missing": [],
                 "base": [], "planned": ["기획"]},
    ))
    plan = SimpleNamespace(tools=(
        SimpleNamespace(kind="skill", name="기획", stations=("plan",), why="틀"),
    ))

    recon = reconcile_tools(ws.ledger, "alpha", "job1", plan)

    assert recon.rows[0].used_in_stations == ("plan",)


def test_reconcile_attempt_id_none_context_attributed_to_the_right_job(
    workspace: Workspace,
) -> None:
    """engine.full_orbit._prompt_with_context calls context.build for
    the "build"/"review" stations with attempt_id=None (the build
    prompt is built before that task's own attempt opens; review is
    deliberately given no attempt_id of its own). Those events carry
    no job name either, so reconcile_tools must attribute each one to
    whichever job's own job.opened event most recently preceded it in
    ledger order -- and must NOT bleed into a different job's own
    None-attempt events."""
    from engine import state

    ws = _sample_workspace(workspace)
    attempt_1 = open_job(ws.config, ws.ledger, "alpha", "job1", "full",
                         roles=("builder",))
    ws.ledger.append(Event(
        project="alpha", kind="context.injected", actor_kind="processor",
        actor_id="processor", attempt_id=None,
        payload={"station": "build", "items": [], "skills": ["아우라-파밍"],
                 "missing": [], "base": [], "planned": ["아우라-파밍"]},
    ))
    # Close job1's attempt so a second, unrelated job can open -- jobs
    # run one at a time per project (see reconcile_tools' own
    # docstring), so this mirrors the real engine.
    ws.ledger.append(Event(
        project="alpha", kind="report.written", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_1, payload={"request_id": "r0"},
    ))
    state.transition(ws.ledger, "alpha", "close_job", "owner")
    state.transition(ws.ledger, "alpha", "finalized", "processor")
    # A second, unrelated job in the SAME project gets its own
    # None-attempt event for a DIFFERENT skill -- must not be counted
    # against job1.
    attempt_2 = open_job(ws.config, ws.ledger, "alpha", "job2", "full",
                         roles=("builder",))
    ws.ledger.append(Event(
        project="alpha", kind="context.injected", actor_kind="processor",
        actor_id="processor", attempt_id=None,
        payload={"station": "build", "items": [], "skills": ["다른-스킬"],
                 "missing": [], "base": [], "planned": ["다른-스킬"]},
    ))
    assert attempt_1 != attempt_2

    plan = SimpleNamespace(tools=(
        SimpleNamespace(kind="skill", name="아우라-파밍", stations=("build",), why="x"),
        SimpleNamespace(kind="skill", name="다른-스킬", stations=("build",), why="x"),
    ))

    recon_job1 = reconcile_tools(ws.ledger, "alpha", "job1", plan)
    by_name = {row.name: row for row in recon_job1.rows}
    assert by_name["아우라-파밍"].used_in_stations == ("build",)
    assert by_name["다른-스킬"].used_in_stations == ()

    recon_job2 = reconcile_tools(ws.ledger, "alpha", "job2", plan)
    by_name_2 = {row.name: row for row in recon_job2.rows}
    assert by_name_2["다른-스킬"].used_in_stations == ("build",)
    assert by_name_2["아우라-파밍"].used_in_stations == ()


def test_write_handoff_appends_job_handoff_written_event(workspace: Workspace) -> None:
    ws = _sample_workspace(workspace)
    attempt_id = open_job(ws.config, ws.ledger, "alpha", "job1", "full",
                          roles=("builder",))
    plan = _plan()
    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, plan)

    write_handoff(ws.config, ws.ledger, "alpha", attempt_id, "job1", plan, acc)

    events = ws.ledger.events("alpha", "job.handoff_written")
    assert len(events) == 1
    _seq, event = events[0]
    assert event.attempt_id == attempt_id
    assert event.payload["job"] == "job1"
