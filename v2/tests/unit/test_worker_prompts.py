"""Unit tests for engine.workers.prompts: build_prompt/review_prompt
text content -- the COMMON_ARCHITECTURE_STANDARD.md note (plan: C3
item 2) and the "no industry/brand hardcoding" rule (MASTER_RULES)."""
from __future__ import annotations

from pathlib import Path

from engine.workers.base import WorkerSpec
from engine.workers.prompts import (
    _ARCHITECTURE_STANDARD_PATH, _ENGINE_ROOT, build_prompt, review_prompt,
)

# Configurable, not a fixed check -- MASTER_RULES 업종 하드코딩 금지,
# verified across 2+ industries, not just the absence of one name.
_BANNED_INDUSTRY_WORDS = (
    "안경원", "미용실", "병원", "카페", "헬스장", "피트니스", "부동산",
    "옷가게", "쇼핑몰", "레스토랑", "학원", "세탁소",
)


def _spec(tmp_path: Path) -> WorkerSpec:
    copy_root = tmp_path / "copy"
    copy_root.mkdir()
    return WorkerSpec(
        project="alpha", attempt_id="J-alpha-TEST#1", generation=1,
        station="build", role="builder", copy_root=copy_root, workdir=copy_root,
        task="do the thing", result_path=tmp_path / "result.json",
        config_path=tmp_path / "engine.json",
    )


def _assert_no_banned_words(text: str) -> None:
    for word in _BANNED_INDUSTRY_WORDS:
        assert word not in text, f"industry word {word!r} leaked into a prompt"


def test_review_prompt_flags_architecture_violations_as_p1(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    text = review_prompt(spec, "original task", ["src/x.py"], "diff text", "full")
    path_text = str(_ARCHITECTURE_STANDARD_PATH)

    assert path_text in text
    assert "P1" in text
    # Referenced by path, never pasted in full -- a rough length guard.
    assert text.count(path_text) == 1


def test_referenced_standard_file_exists_under_the_engine_root() -> None:
    """A worker's (and the reviewer's own) sandbox mounts ENGINE_ROOT
    read-only but hides the rest of the apps root -- a prompt pointing
    anywhere else would name a file it can never actually open.
    _ARCHITECTURE_STANDARD_PATH must be UNDER _ENGINE_ROOT, and the
    file it names must actually exist."""
    assert _ARCHITECTURE_STANDARD_PATH.is_relative_to(_ENGINE_ROOT)
    assert _ARCHITECTURE_STANDARD_PATH.is_file()
    assert "kept in sync" in _ARCHITECTURE_STANDARD_PATH.read_text(encoding="utf-8")


def test_review_prompt_for_a_plan_only_diff_does_not_fault_missing_implementation(
    tmp_path: Path,
) -> None:
    """결함 53: small/full 모두 plan review 단계는 .gongbang/<job>/plan.json,
    facts.md, scenes/*만 바뀐 상태를 심사한다 -- 코드는 아직 한 줄도 바뀌지
    않은 게 정상이다. 그런데 review_prompt가 평소 빌드-리뷰와 똑같은 문구
    ("diff가 요청을 만족하는가")만 주니, Codex가 실제로
    "요청한 구현이 diff에 없다"(P1)로 불통과시켰다(sample-small3 실행
    로그, R-0109d7e9e000cf30.result.json). 이 프롬프트는 changed_files가
    전부 .gongbang/<job>/ 아래일 때 "이것은 계획 심사이고 코드는 아직
    없는 게 정상, 심사 대상은 계획·장면이 그 요청을 다루는가"라고 명시
    해야 한다. changed_files는 실제 `git diff --name-status` 원본 줄
    ("상태\t경로")로 넘어온다 -- 맨 경로 문자열이 아니다."""
    spec = _spec(tmp_path)
    changed = [
        "A\t.gongbang/gb2-change-1793100351/plan.json",
        "A\t.gongbang/gb2-change-1793100351/facts.md",
    ]
    text = review_prompt(
        spec, "인사말을 'Welcome, {name}!'로 바꾸고 시험도 맞춰라", changed,
        "diff text", "full",
    )
    assert "계획" in text or "plan" in text.lower()
    assert "아직" in text or "has not" in text.lower() or "not yet" in text.lower()


def test_review_prompt_for_a_plan_diff_whose_only_file_is_a_scene_script(
    tmp_path: Path,
) -> None:
    """결함 53, 재발: plan-review 단계의 diff가 .gongbang/<job>/ 파일을
    하나도 바꾸지 않고 tests/scenes/*만 바꿀 때도(계획 자체는 이미
    committed, 이번 변경은 장면 추가뿐인 경우) 여전히 plan-review로
    인식해야 한다 -- sample-small4 실제 재실행에서 changed_files가
    ["tests/scenes/scene-welcome-greeting.sh"] 하나뿐이었는데도 Codex가
    "요청한 구현이 diff에 없다"(P1)로 또 불통과시켰다
    (R-cb8b57faa538f671.result.json). changed_files도 실제 원본 줄
    ("A\t경로")로 넘어온다."""
    spec = _spec(tmp_path)
    text = review_prompt(
        spec, "인사말을 'Welcome, {name}!'로 바꾸고 시험도 맞춰라",
        ["A\ttests/scenes/scene-welcome-greeting.sh"],
        "diff text", "full",
    )
    assert "계획" in text or "plan" in text.lower()
    assert "아직" in text or "has not" in text.lower() or "not yet" in text.lower()


def test_review_prompt_for_a_plan_diff_with_real_name_status_lines(
    tmp_path: Path,
) -> None:
    """결함 53, 세 번째 재발: _is_plan_review_diff가 "상태\t경로" 원본 줄의
    경로 부분이 아니라 줄 전체에 .startswith()를 걸면 "A\t.gongbang/..."
    은 절대 ".gongbang/"로 시작하지 않아 늘 거짓이 된다 -- sample-small4의
    두 번째 재실행(`gb2 continue`)이 실제 codex 세션 로그에서 이 노트가
    통째로 빠진 채 보내졌음을 보여줬다(R-e4b17f2ee77e134e). 세 파일이
    섞인 실제 모양(facts.md 추가, plan.json 추가, scenes 스크립트 추가)
    그대로 재현한다."""
    spec = _spec(tmp_path)
    changed = [
        "A\t.gongbang/gb2-change-1793101630/facts.md",
        "A\t.gongbang/gb2-change-1793101630/plan.json",
        "A\ttests/scenes/welcome_greeting.sh",
    ]
    text = review_prompt(
        spec, "인사말을 'Welcome, {name}!'로 바꾸고 시험도 맞춰라", changed,
        "diff text", "full",
    )
    assert "PLAN review" in text


def test_review_prompt_for_a_code_diff_stays_the_ordinary_build_review(
    tmp_path: Path,
) -> None:
    """changed_files에 .gongbang/ 밖 파일이 하나라도 있으면(=실제 빌드
    리뷰) 평소 문구 그대로 -- plan-review 전용 안내가 끼어들어 코드
    리뷰 기준을 느슨하게 만들면 안 된다."""
    spec = _spec(tmp_path)
    text = review_prompt(
        spec, "원래 요청", ["M\tsrc/greeter/__init__.py", "M\ttests/test_greeter.py"],
        "diff text", "full",
    )
    assert "Original request:" in text
    assert "계획 심사" not in text


def test_build_and_review_prompts_have_no_industry_or_brand_words(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    samples = [
        build_prompt(spec, ("src/",), (r"ls( -la?)?",), ("pytest",)),
        build_prompt(spec, (), (), ()),
        review_prompt(spec, "original task", ["src/x.py"], "diff text", "full"),
        review_prompt(spec, "original task", [], "diff text", "partial"),
    ]
    for text in samples:
        _assert_no_banned_words(text)
