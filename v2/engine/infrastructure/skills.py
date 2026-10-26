"""Skill lookup: reads `<name>/SKILL.md` text out of configured skill
roots. A "skill" here is just a plain-text file some station's prompt
gets to see verbatim -- this module never parses or executes anything
inside it.

Lookup order for `load_skill(roots, name)`:
  1. `<root>/<name>/SKILL.md` (a skill directly under the root)
  2. `<root>/<category>/<name>/SKILL.md` for each immediate subdirectory
     `<category>` of the root (one level of category folders, matching
     how ~/.claude/skills-library is laid out)
Roots are searched in order; the first match wins. A name not found in
any root comes back as a SkillText with body=None -- callers (engine.
context) must record that as a missing skill, never silently drop it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkillText:
    name: str
    path: str | None  # absolute path of the SKILL.md found, or None
    body: str | None  # file contents, or None if not found in any root


def skill_path_in_root(root: Path, candidate: Path) -> Path | None:
    """Resolves `candidate` (following symlinks) and returns that
    resolved path only if it stays inside `root`'s own resolved tree --
    a symlinked SKILL.md, or an ancestor directory that is itself a
    symlink, pointing outside the root is rejected the same way a
    simply-missing file is (callers treat None as "not found"). Never
    raises."""
    try:
        resolved_root = root.resolve()
        resolved = candidate.resolve()
    except OSError:
        return None
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        return None
    return resolved


def _skill_md_in(root: Path, name: str) -> Path | None:
    direct = root / name / "SKILL.md"
    if direct.is_file():
        found = skill_path_in_root(root, direct)
        if found is not None:
            return found
    if not root.is_dir():
        return None
    for category in sorted(root.iterdir()):
        if not category.is_dir():
            continue
        candidate = category / name / "SKILL.md"
        if candidate.is_file():
            found = skill_path_in_root(root, candidate)
            if found is not None:
                return found
    return None


def load_skill(roots: tuple[str, ...], name: str) -> SkillText:
    """Search `roots` in order for `<name>/SKILL.md` (directly, or one
    level of category folder down). Returns the first match; a name
    found in no root comes back with path=None, body=None."""
    for root in roots:
        found = _skill_md_in(Path(root).expanduser(), name)
        if found is not None:
            try:
                return SkillText(name=name, path=str(found),
                                  body=found.read_text(encoding="utf-8"))
            except OSError:
                continue
    return SkillText(name=name, path=None, body=None)


__all__ = ["SkillText", "load_skill", "skill_path_in_root"]
