"""Unit tests for engine.gates: the facts.md door check and the
design.approved ledger check."""
from __future__ import annotations

from pathlib import Path

from engine.gates import check_design_approved, check_facts
from engine.ledger import Event, Ledger


def _write_facts(repo: Path, job: str, text: str) -> None:
    path = repo / ".gongbang" / job / "facts.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_check_facts_missing_file(tmp_path: Path) -> None:
    assert check_facts(tmp_path, "job") == ["facts_missing"]


def test_check_facts_pass_line_without_source_is_a_problem(tmp_path: Path) -> None:
    _write_facts(tmp_path, "job", "- 버튼 색은 파란색이다: 통과\n")
    problems = check_facts(tmp_path, "job")
    assert len(problems) == 1
    assert "통과" in problems[0]


def test_check_facts_pass_line_with_source_is_fine(tmp_path: Path) -> None:
    _write_facts(tmp_path, "job", "- 버튼 색은 파란색이다: 통과 (출처: design.md:12)\n")
    assert check_facts(tmp_path, "job") == []


def test_check_facts_needs_check_line_outside_code_values_is_fine(tmp_path: Path) -> None:
    # 확인 필요 is only a blocker inside the 「코드에 넣는 값」 section --
    # a plain claim left as 확인 필요 elsewhere in facts.md does not, by
    # itself, block planning (the planner may still proceed around an
    # unresolved claim that never needs to become a literal value in code).
    _write_facts(tmp_path, "job", "- 경쟁사 가격: 확인 필요\n")
    assert check_facts(tmp_path, "job") == []


def test_check_facts_needs_check_inside_code_values_blocks(tmp_path: Path) -> None:
    text = (
        "# 사실확정\n"
        "- 요청 사항 요약: 통과 (출처: request)\n\n"
        "## 코드에 넣는 값\n"
        "- 최대 재시도 횟수: 확인 필요\n"
    )
    _write_facts(tmp_path, "job", text)
    problems = check_facts(tmp_path, "job")
    assert len(problems) == 1
    assert "확인 필요" in problems[0]


def test_check_facts_code_values_section_without_needs_check_passes(tmp_path: Path) -> None:
    text = (
        "# 사실확정\n"
        "- 요청 사항 요약: 통과 (출처: request)\n\n"
        "## 코드에 넣는 값\n"
        "- 최대 재시도 횟수: 3 (출처: 운영 로그)\n\n"
        "## 다음 절\n"
        "- 확인 필요 같은 글자가 여기 있어도 다음 절이라 무관하다\n"
    )
    _write_facts(tmp_path, "job", text)
    assert check_facts(tmp_path, "job") == []


def test_check_facts_multiple_problems_all_reported(tmp_path: Path) -> None:
    text = (
        "- 첫째 주장: 통과\n"
        "- 둘째 주장: 통과\n\n"
        "## 코드에 넣는 값\n"
        "- 값1: 확인 필요\n"
    )
    _write_facts(tmp_path, "job", text)
    problems = check_facts(tmp_path, "job")
    assert len(problems) == 3


def test_check_facts_different_job_names_are_independent(tmp_path: Path) -> None:
    _write_facts(tmp_path, "job-a", "- 모든 것: 통과 (출처: x)\n")
    assert check_facts(tmp_path, "job-a") == []
    assert check_facts(tmp_path, "job-b") == ["facts_missing"]


# ---- check_design_approved ------------------------------------------

def test_check_design_approved_false_with_no_events(tmp_path: Path) -> None:
    ledger = Ledger.create(tmp_path / "ledger.sqlite")
    ledger.register_project("alpha")
    assert check_design_approved(ledger, "alpha", "job") is False


def test_check_design_approved_true_after_matching_event(tmp_path: Path) -> None:
    ledger = Ledger.create(tmp_path / "ledger.sqlite")
    ledger.register_project("alpha")
    ledger.append(Event(
        project="alpha", kind="design.approved", actor_kind="owner",
        actor_id="owner", attempt_id=None, payload={"job": "job"},
    ))
    assert check_design_approved(ledger, "alpha", "job") is True


def test_check_design_approved_false_for_different_job(tmp_path: Path) -> None:
    ledger = Ledger.create(tmp_path / "ledger.sqlite")
    ledger.register_project("alpha")
    ledger.append(Event(
        project="alpha", kind="design.approved", actor_kind="owner",
        actor_id="owner", attempt_id=None, payload={"job": "other-job"},
    ))
    assert check_design_approved(ledger, "alpha", "job") is False


def test_check_design_approved_false_for_different_project(tmp_path: Path) -> None:
    ledger = Ledger.create(tmp_path / "ledger.sqlite")
    ledger.register_project("alpha")
    ledger.register_project("beta")
    ledger.append(Event(
        project="beta", kind="design.approved", actor_kind="owner",
        actor_id="owner", attempt_id=None, payload={"job": "job"},
    ))
    assert check_design_approved(ledger, "alpha", "job") is False


def test_facts_table_source_column_counts_as_source(tmp_path) -> None:
    """In a table with 판정 and 출처 columns, a 통과 row is sourced when its
    출처 cell is filled, and unsourced when that cell is empty."""
    from engine.gates import check_facts
    d = tmp_path / ".gongbang" / "j"
    d.mkdir(parents=True)
    (d / "facts.md").write_text(
        "| 주장 | 판정 | 출처 |\n|---|---|---|\n| a | 통과 | 요구사항 원문 |\n| b | 통과 |  |\n",
        encoding="utf-8")
    assert check_facts(tmp_path, "j") == ["line 4: 통과 without 출처"]
