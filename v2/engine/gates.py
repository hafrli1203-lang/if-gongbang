"""Door checks between orbit stations (plan §「궤도 9칸」) that read a
plain file or the ledger rather than re-running a worker.

check_facts   facts.md (repo/.gongbang/<job>/facts.md) gate: the file
              must exist, every 통과(pass) line must carry a 출처
              (source) marker, and the 「코드에 넣는 값」(values to put
              in code) table/section must have no remaining 확인 필요
              (needs-check) marker. [] means the facts station may be
              trusted and the job may move on to planning.
check_design_approved   True iff the ledger has a 'design.approved'
              event for this (project, job) -- appended by `gb2
              approve-design` after the owner has seen the designer
              station's own output and said yes.
"""
from __future__ import annotations

from pathlib import Path

from engine.ledger import Ledger

_PASS_MARK = "통과"
_NEEDS_CHECK_MARK = "확인 필요"
_SOURCE_MARK = "출처"
_CODE_VALUES_HEADER = "코드에 넣는 값"


def _facts_path(repo: Path, job: str) -> Path:
    return repo / ".gongbang" / job / "facts.md"


def _is_heading(line: str) -> bool:
    return line.lstrip().startswith("#")


def _code_values_section(lines: list[str]) -> list[str]:
    """Lines belonging to the 「코드에 넣는 값」 section: from the first
    heading line containing that phrase, up to (not including) the
    next heading line, or end of file. [] if no such heading exists."""
    start = None
    for i, line in enumerate(lines):
        if _is_heading(line) and _CODE_VALUES_HEADER in line:
            start = i + 1
            break
    if start is None:
        return []
    end = len(lines)
    for i in range(start, len(lines)):
        if _is_heading(lines[i]):
            end = i
            break
    return lines[start:end]



def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _table_pass_rows(lines: list[str]) -> dict[int, bool]:
    """For Markdown tables whose header has both a 판정 (verdict) and a 출처
    (source) column: {1-based line number: has a non-empty source} for every
    data row whose verdict is 통과. Rows of other tables are not included."""
    result: dict[int, bool] = {}
    verdict_col = source_col = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("|"):
            verdict_col = source_col = None
            continue
        cells = _cells(stripped)
        if verdict_col is None:
            if "판정" in cells and _SOURCE_MARK in cells:
                verdict_col, source_col = cells.index("판정"), cells.index(_SOURCE_MARK)
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue  # the |---|---| separator row
        verdict = cells[verdict_col] if verdict_col < len(cells) else ""
        if _PASS_MARK in verdict:
            source = cells[source_col] if source_col < len(cells) else ""
            result[i + 1] = source not in ("", "-")
    return result

def check_facts(repo: Path, job: str) -> list[str]:
    """[] iff facts.md exists, every line claiming 통과 also carries a
    출처 marker on the same line, and the 「코드에 넣는 값」 section (if
    present) contains no 확인 필요 marker anywhere in it."""
    path = _facts_path(repo, job)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ["facts_missing"]

    problems: list[str] = []
    lines = text.splitlines()
    table_rows = _table_pass_rows(lines)
    for lineno, line in enumerate(lines, start=1):
        if lineno in table_rows:
            if not table_rows[lineno]:
                problems.append(f"line {lineno}: {_PASS_MARK} without {_SOURCE_MARK}")
            continue
        if _PASS_MARK in line and _SOURCE_MARK not in line:
            problems.append(f"line {lineno}: {_PASS_MARK} without {_SOURCE_MARK}")

    code_values_lines = _code_values_section(lines)
    for offset, line in enumerate(code_values_lines):
        if _NEEDS_CHECK_MARK in line:
            problems.append(
                f"{_CODE_VALUES_HEADER}: {_NEEDS_CHECK_MARK} remaining ({line.strip()!r})"
            )
    return problems


def check_design_approved(ledger: Ledger, project: str, job: str) -> bool:
    for _seq, event in ledger.events(project, "design.approved"):
        if event.payload.get("job") == job:
            return True
    return False


__all__ = ["check_facts", "check_design_approved"]
