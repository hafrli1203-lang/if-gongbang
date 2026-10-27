"""facts_prompt must tell the facts worker about the "## 외부 대상"
section -- engine.gates.check_probe_targets (and engine.full_orbit's own
probe-skip decision) read ONLY that section of facts.md to decide which
external APIs/CLIs/file formats this job depends on and whether the
probe station runs at all."""
from __future__ import annotations

from engine.plan import Task
from engine.station_prompts import (
    _ARCHITECTURE_STANDARD_PATH, _ENGINE_ROOT, designer_prompt, facts_prompt,
    fix_task_text, planner_prompt, prober_prompt, small_prompt,
)

# MASTER_RULES "업종 하드코딩 금지": none of these (or any brand name) may
# appear in a prompt this module builds -- configurable rather than a
# fixed check, so a future reviewer can extend it without touching the
# test bodies below. 2+ different industries, matching the rule's own
# "최소 2개 업종" verification requirement.
_BANNED_INDUSTRY_WORDS = (
    "안경원", "미용실", "병원", "카페", "헬스장", "피트니스", "부동산",
    "옷가게", "쇼핑몰", "레스토랑", "학원", "세탁소",
)


def _assert_no_banned_words(text: str) -> None:
    for word in _BANNED_INDUSTRY_WORDS:
        assert word not in text, f"industry word {word!r} leaked into a prompt"


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
    assert "장면이 없는 requirement" in text


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


def test_planner_prompt_references_the_architecture_standard_by_path() -> None:
    """COMMON_ARCHITECTURE_STANDARD.md item (2): the planner states
    which layer each task's owned files belong to -- referenced by
    file path, never pasted into the prompt (keep it short)."""
    text = planner_prompt("요청 내용", "job1", "facts")
    path_text = str(_ARCHITECTURE_STANDARD_PATH)

    assert path_text in text
    assert "interface" in text and "application" in text
    assert "domain" in text and "infrastructure" in text
    # Pasted-in, not just referenced, would make the prompt far longer
    # than a one-line note -- a rough length guard against regression.
    assert text.count(path_text) == 1


def test_small_prompt_also_references_the_architecture_standard() -> None:
    text = small_prompt("요청 내용", "job1")

    assert str(_ARCHITECTURE_STANDARD_PATH) in text


def test_referenced_standard_file_exists_under_the_engine_root() -> None:
    """A worker's sandbox mounts ENGINE_ROOT read-only but hides the
    rest of the apps root (engine.steps.station's own ENGINE_ROOT
    docstring) -- a prompt pointing anywhere else would name a file no
    worker can actually open. _ARCHITECTURE_STANDARD_PATH must be
    UNDER _ENGINE_ROOT, and the file it names must actually exist
    (docs/standards/architecture.md, the in-repo synced copy)."""
    assert _ARCHITECTURE_STANDARD_PATH.is_relative_to(_ENGINE_ROOT)
    assert _ARCHITECTURE_STANDARD_PATH.is_file()
    assert "kept in sync" in _ARCHITECTURE_STANDARD_PATH.read_text(encoding="utf-8")


def test_no_industry_or_brand_words_in_any_prompt() -> None:
    """Covers every prompt-producing function in this module, with
    representative arguments for each -- MASTER_RULES: 업종 하드코딩
    금지, verified across industries, not just absence of one name."""
    task = Task(id="t1", title="제목", detail="설명", owns=("src/",), depends_on=())
    samples = [
        facts_prompt("요청", "job1"),
        prober_prompt("요청", "job1", (("API", "https://example.com/docs"),)),
        planner_prompt("요청", "job1", "facts"),
        planner_prompt("요청", "job1", "facts", tool_catalog="- skill-a: 설명"),
        small_prompt("요청", "job1"),
        small_prompt("요청", "job1", tool_catalog="- skill-a: 설명"),
        designer_prompt("요청", "job1", "목표"),
        fix_task_text(task, "목표", ("s1",), "job1", False),
    ]
    for text in samples:
        _assert_no_banned_words(text)


def test_prober_prompt_states_the_exact_rule_the_engine_judges() -> None:
    """The prober prompt once asked only for exit 0 and "PROBE OK" in the
    log, while acceptance_probe._probe_observed requires the script's own
    output to carry "PROBE OK <doc>" -- every real probe then failed."""
    from engine.application.acceptance_probe import _PROBE_OK_MARK
    from engine.application.stage_prompts import prober_prompt
    text = prober_prompt("req", "job", (("Some API", "https://docs.example/x"),))
    assert f"`{_PROBE_OK_MARK} <문서>`" in text
    assert "https://docs.example/x" in text
