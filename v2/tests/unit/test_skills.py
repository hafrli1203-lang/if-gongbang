from __future__ import annotations

from pathlib import Path

from engine.skills import load_skill


def _write_skill(root: Path, category: str | None, name: str, body: str) -> None:
    base = (root / category / name) if category else (root / name)
    base.mkdir(parents=True)
    (base / "SKILL.md").write_text(body, encoding="utf-8")


def test_found_directly_under_root(tmp_path: Path) -> None:
    _write_skill(tmp_path, None, "크래시-아웃", "증거로 공격한다")
    result = load_skill((str(tmp_path),), "크래시-아웃")
    assert result.body == "증거로 공격한다"
    assert result.path == str(tmp_path / "크래시-아웃" / "SKILL.md")


def test_found_one_category_level_down(tmp_path: Path) -> None:
    _write_skill(tmp_path, "05-디자인", "화면-품질-점검", "화면을 본다")
    result = load_skill((str(tmp_path),), "화면-품질-점검")
    assert result.body == "화면을 본다"
    assert "05-디자인" in str(result.path)


def test_first_root_wins(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _write_skill(root_a, None, "같은이름", "a의 내용")
    _write_skill(root_b, None, "같은이름", "b의 내용")
    result = load_skill((str(root_a), str(root_b)), "같은이름")
    assert result.body == "a의 내용"


def test_missing_skill_returns_none_body(tmp_path: Path) -> None:
    result = load_skill((str(tmp_path),), "없는-스킬")
    assert result.body is None
    assert result.path is None


def test_missing_root_does_not_raise(tmp_path: Path) -> None:
    result = load_skill((str(tmp_path / "nope"),), "아무거나")
    assert result.body is None


def test_symlinked_skill_md_escaping_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    _write_skill(outside, None, "비밀", "root 밖의 비밀 내용")
    skill_dir = root / "탈출"
    skill_dir.mkdir(parents=True)
    # SKILL.md itself is a symlink pointing outside the root.
    (skill_dir / "SKILL.md").symlink_to(outside / "비밀" / "SKILL.md")
    result = load_skill((str(root),), "탈출")
    assert result.body is None
    assert result.path is None


def test_symlinked_category_dir_escaping_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    _write_skill(outside, None, "숨은-스킬", "밖에 있다")
    # A whole category directory under root is itself a symlink to
    # somewhere outside root -- the ancestor-escape case, not just the
    # SKILL.md file itself.
    (root / "카테고리").symlink_to(outside)
    result = load_skill((str(root),), "숨은-스킬")
    assert result.body is None
    assert result.path is None
