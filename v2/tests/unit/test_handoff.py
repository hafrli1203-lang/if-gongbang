"""Unit tests for engine.handoff.write_handoff: report.md content, built
from a Plan and an already-computed AcceptanceResult."""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

from engine.acceptance import AcceptanceResult, SceneResult, run_acceptance
from engine.handoff import report_path, write_handoff
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
