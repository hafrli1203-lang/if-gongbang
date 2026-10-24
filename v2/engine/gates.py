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
check_probe_targets  facts.md's optional "## 외부 대상" section ->
              [(name, url), ...] -- the external APIs/CLIs/file formats
              this job depends on, each with its own official-doc URL.
              [] (no such section, or facts.md missing) means the probe
              station is skipped entirely (engine.full_orbit).
check_probe   [] iff every target in a check_probe_targets() list has
              its own .gongbang/<job>/probe/<name>.log containing
              "PROBE OK" -- a missing log or one without that marker is
              a problem, never a crash.
"""
from __future__ import annotations

from pathlib import Path

from engine.ledger import Ledger

_PASS_MARK = "통과"
_NEEDS_CHECK_MARK = "확인 필요"
_SOURCE_MARK = "출처"
_CODE_VALUES_HEADER = "코드에 넣는 값"
_PROBE_TARGETS_HEADER = "외부 대상"
_PROBE_OK_MARK = "PROBE OK"


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



def _heading_section(lines: list[str], header_phrase: str) -> list[str]:
    """Lines belonging to the first heading line containing
    `header_phrase`, up to (not including) the next heading line, or
    end of file. [] if no such heading exists. Same shape as
    _code_values_section above, generalized by the phrase to look
    for -- kept as its own function rather than a shared helper so a
    future change to one section's own matching rule never silently
    changes the other's."""
    start = None
    for i, line in enumerate(lines):
        if _is_heading(line) and header_phrase in line:
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


def check_probe_targets(repo: Path, job: str) -> list[tuple[str, str]]:
    """facts.md's optional "## 외부 대상" section, parsed line by line:
    "- <name>: <공식 문서 URL>" -> (name, url), in the order they
    appear, skipping any line that does not match that shape. []
    (never raises) if facts.md does not exist, or has no such section
    at all -- engine.full_orbit treats that as "this job touches no
    external API/CLI/file format" and skips the probe station
    entirely."""
    path = _facts_path(repo, job)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    section = _heading_section(text.splitlines(), _PROBE_TARGETS_HEADER)
    targets: list[tuple[str, str]] = []
    for line in section:
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        body = stripped[1:].strip()
        name, sep, url = body.partition(":")
        name, url = name.strip(), url.strip()
        if sep and name and url:
            targets.append((name, url))
    return targets


def check_probe(repo: Path, job: str, targets: list[tuple[str, str]]) -> list[str]:
    """[] iff every (name, url) in `targets` has its own
    .gongbang/<job>/probe/<name>.log whose content contains "PROBE OK".
    A missing log or one without that marker becomes a plain problem
    string naming the target, never an exception -- engine.full_orbit
    turns a non-empty result into a needs_owner stop
    ("probe_failed:<names>"), not a crash."""
    probe_dir = repo / ".gongbang" / job / "probe"
    problems: list[str] = []
    for name, _url in targets:
        log_path = probe_dir / f"{name}.log"
        try:
            text = log_path.read_text(encoding="utf-8")
        except OSError:
            problems.append(f"{name}: log missing")
            continue
        if _PROBE_OK_MARK not in text:
            problems.append(f"{name}: {_PROBE_OK_MARK} not found")
    return problems


__all__ = [
    "check_facts", "check_design_approved", "check_probe_targets", "check_probe",
]
