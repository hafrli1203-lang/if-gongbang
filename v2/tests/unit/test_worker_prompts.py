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
