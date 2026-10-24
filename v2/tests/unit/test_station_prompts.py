"""facts_prompt must tell the facts worker about the "## 외부 대상"
section -- engine.gates.check_probe_targets (and engine.full_orbit's own
probe-skip decision) read ONLY that section of facts.md to decide which
external APIs/CLIs/file formats this job depends on and whether the
probe station runs at all."""
from __future__ import annotations

from engine.plan import Task
from engine.station_prompts import facts_prompt, fix_task_text, planner_prompt


def test_facts_prompt_tells_the_worker_to_write_외부_대상_section() -> None:
    text = facts_prompt("요청 내용", "job1")

    assert "## 외부 대상" in text
    # (없음) is the worker's way of saying "no external target" --
    # engine.gates.check_probe_targets treats a missing/empty section the
    # same way (probe skipped), but the worker must be told to write the
    # heading either way, never leave it out.
    assert "없음" in text


def test_planner_prompt_asks_for_requirements_and_covers() -> None:
    text = planner_prompt("요청 내용", "job1", "facts")

    assert '"requirements"' in text
    assert '"covers"' in text
    assert "덮이지 않은" in text


def test_fix_task_text_with_failing_scenes_names_them() -> None:
    task = Task(id="fix-1", title="고치기", detail="", owns=("src/",), depends_on=())
    text = fix_task_text(task, "목표", ("s1", "s2"), "job1", False)
    assert "s1, s2" in text


def test_fix_task_text_with_no_failing_scene_uses_full_suite_tail() -> None:
    task = Task(id="fix-1", title="고치기", detail="", owns=("src/",), depends_on=())
    text = fix_task_text(task, "목표", (), "job1", False,
                         full_suite_returncode=1, full_suite_tail="AssertionError: boom")
    assert "전체 시험" in text
    assert "AssertionError: boom" in text
    assert "1" in text
