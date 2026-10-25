"""Unit tests for engine.gates: the facts.md door check and the
design.approved ledger check."""
from __future__ import annotations

from pathlib import Path

import pytest

from engine.gates import (
    ProbeTargetsError, check_design_approved, check_facts, check_probe, check_probe_targets,
    probe_target_id,
)
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


# ---- check_probe_targets / check_probe ---------------------------------

def test_check_probe_targets_raises_when_section_missing_entirely(tmp_path: Path) -> None:
    """facts.md existing with no "## 외부 대상" heading at all is an
    error, never treated the same as an explicit "(없음)" -- see
    check_probe_targets' own docstring."""
    _write_facts(tmp_path, "job", "- 요청 사항 요약: 통과 (출처: request)\n")
    with pytest.raises(ProbeTargetsError) as exc:
        check_probe_targets(tmp_path, "job")
    assert exc.value.reason == "probe_targets_section_missing"


def test_check_probe_targets_empty_without_facts_file(tmp_path: Path) -> None:
    assert check_probe_targets(tmp_path, "job") == []


def test_check_probe_targets_parses_name_and_url(tmp_path: Path) -> None:
    text = (
        "# 사실확정\n"
        "- 요청 사항 요약: 통과 (출처: request)\n\n"
        "## 외부 대상\n"
        "- stripe: https://docs.stripe.com/api\n"
        "- slack: https://api.slack.com/web\n\n"
        "## 코드에 넣는 값\n"
        "(없음)\n"
    )
    _write_facts(tmp_path, "job", text)
    assert check_probe_targets(tmp_path, "job") == [
        ("stripe", "https://docs.stripe.com/api"),
        ("slack", "https://api.slack.com/web"),
    ]


def test_check_probe_targets_raises_on_malformed_line(tmp_path: Path) -> None:
    text = (
        "## 외부 대상\n"
        "- stripe: https://docs.stripe.com/api\n"
        "- 이름만 있고 콜론이 없는 줄\n"
    )
    _write_facts(tmp_path, "job", text)
    with pytest.raises(ProbeTargetsError):
        check_probe_targets(tmp_path, "job")


def test_check_probe_targets_raises_on_non_bullet_line(tmp_path: Path) -> None:
    text = (
        "## 외부 대상\n"
        "- stripe: https://docs.stripe.com/api\n"
        "그냥 글 (불릿 아님)\n"
    )
    _write_facts(tmp_path, "job", text)
    with pytest.raises(ProbeTargetsError):
        check_probe_targets(tmp_path, "job")


def test_check_probe_targets_empty_mark_means_no_targets(tmp_path: Path) -> None:
    text = "## 외부 대상\n(없음)\n"
    _write_facts(tmp_path, "job", text)
    assert check_probe_targets(tmp_path, "job") == []


def test_check_probe_targets_bulleted_empty_mark_means_no_targets(tmp_path: Path) -> None:
    text = "## 외부 대상\n- (없음)\n"
    _write_facts(tmp_path, "job", text)
    assert check_probe_targets(tmp_path, "job") == []


def test_check_probe_targets_rejects_empty_mark_mixed_with_real_target(
    tmp_path: Path,
) -> None:
    text = (
        "## 외부 대상\n"
        "- stripe: https://docs.stripe.com/api\n"
        "- (없음)\n"
    )
    _write_facts(tmp_path, "job", text)
    with pytest.raises(ProbeTargetsError):
        check_probe_targets(tmp_path, "job")


def test_check_probe_targets_korean_only_names_stay_distinct(tmp_path: Path) -> None:
    """Different target names must never collapse into the same entry
    -- this is check_probe_targets' own job regardless of id scheme
    (probe_target_id derives ids from POSITION, never from these
    names, precisely so two Korean-only names can never collide)."""
    text = (
        "## 외부 대상\n"
        "- 네이버 API: https://developers.naver.com/docs\n"
        "- 카카오 API: https://developers.kakao.com/docs\n"
    )
    _write_facts(tmp_path, "job", text)
    targets = check_probe_targets(tmp_path, "job")
    assert targets == [
        ("네이버 API", "https://developers.naver.com/docs"),
        ("카카오 API", "https://developers.kakao.com/docs"),
    ]


def test_probe_target_id_is_positional_not_name_derived(tmp_path: Path) -> None:
    """The old name-derived scheme collapsed "Naver API"/"Naver_API"
    (and any Korean-only name) into the same id -- probe_target_id
    depends only on position, so distinct targets always get distinct
    ids regardless of how similar or non-ASCII their names are."""
    assert probe_target_id(0) == "t1"
    assert probe_target_id(1) == "t2"
    assert len({probe_target_id(0), probe_target_id(1)}) == 2


def _write_probe_log(repo: Path, job: str, probe_id: str, content: str) -> None:
    path = repo / ".gongbang" / job / "probe" / f"{probe_id}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_check_probe_passes_when_every_log_has_probe_ok(tmp_path: Path) -> None:
    targets = [("stripe", "https://docs.stripe.com/api")]
    _write_probe_log(tmp_path, "job", "t1",
                     "https://docs.stripe.com/api\nPROBE OK\n{...}\n")
    assert check_probe(tmp_path, "job", targets) == []


def test_check_probe_reports_missing_log(tmp_path: Path) -> None:
    targets = [("stripe", "https://docs.stripe.com/api")]
    assert check_probe(tmp_path, "job", targets) == ["stripe: log missing"]


def test_check_probe_reports_log_without_probe_ok(tmp_path: Path) -> None:
    targets = [("stripe", "https://docs.stripe.com/api")]
    _write_probe_log(tmp_path, "job", "t1",
                     "https://docs.stripe.com/api\nPROBE FAIL\nboom\n")
    problems = check_probe(tmp_path, "job", targets)
    assert len(problems) == 1
    assert "stripe" in problems[0]


def test_check_probe_reports_each_failing_target(tmp_path: Path) -> None:
    targets = [("stripe", "https://a"), ("slack", "https://b")]
    _write_probe_log(tmp_path, "job", "t1", "https://a\nPROBE OK\n")
    problems = check_probe(tmp_path, "job", targets)
    assert problems == ["slack: log missing"]


def test_check_probe_distinguishes_targets_with_colliding_old_style_names(
    tmp_path: Path,
) -> None:
    """Regression for the collision this id scheme exists to prevent:
    two targets whose names would have collapsed to the same id under
    the old name-derived scheme ("Naver API"/"Naver_API") now get
    independent logs (t1/t2) and are checked independently -- one
    passing script can never stand in for the other."""
    targets = [("Naver API", "https://a"), ("Naver_API", "https://b")]
    _write_probe_log(tmp_path, "job", "t1", "https://a\nPROBE OK\n")
    # t2's log is missing -- must be reported even though t1 passed.
    problems = check_probe(tmp_path, "job", targets)
    assert problems == ["Naver_API: log missing"]
