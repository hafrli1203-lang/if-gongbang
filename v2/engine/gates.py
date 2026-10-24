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
              this job depends on, each with its own official-doc URL,
              in the exact order the section lists them (callers that
              need a stable per-target id -- engine.acceptance.
              run_probe_scripts, this module's own check_probe --
              derive it from POSITION in this list, 1-based ("t1",
              "t2", ...), never from the target's own name: a name-
              derived id (the old scheme) collapses every Korean-only
              name to the same fallback id and "Naver API"/"Naver_API"
              to the same id too, since both get lowercased and every
              character outside [a-z0-9-] collapsed to "-" -- letting
              one probe script silently stand in for several distinct
              targets). [] (no such section, or facts.md missing) means
              the probe station is skipped entirely (engine.full_orbit).
              A line in that section that is neither "(없음)"
              (explicitly "no targets") nor a well-formed "- name: url"
              bullet raises ProbeTargetsError -- a malformed line is
              never silently dropped, since a target engine.full_orbit
              never saw is a target it never ran a probe script for.
              A section that mixes "(없음)" with one or more real
              target bullets also raises ProbeTargetsError
              ("probe_targets_mixed_empty_marker") -- "no targets" and
              "here are the targets" are contradictory, never resolved
              by quietly picking one.
check_probe   [] iff every target in a check_probe_targets() list has
              its own .gongbang/<job>/probe/t<N>.log (1-based position
              in the SAME list, see check_probe_targets) containing
              "PROBE OK" -- a missing log or one without that marker is
              a problem, never a crash. Legacy/unused by the live
              orbit flow today (engine.full_orbit calls
              engine.acceptance.run_probe_scripts instead, which
              actually re-executes each script rather than trusting a
              worker-written log; see that module's own docstring) --
              kept here, and fixed to the same id scheme, for whatever
              still calls it directly.
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
_PROBE_EMPTY_MARK = "없음"


class ProbeTargetsError(Exception):
    """facts.md's "## 외부 대상" section has a line that is neither
    "(없음)" nor a well-formed "- name: url" bullet -- see
    check_probe_targets."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


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
    appear. [] (never raises for either of these) if facts.md does not
    exist, has no such section at all, or the section's only content
    is the sentinel "(없음)" (optionally bulleted "- (없음)") -- engine.
    full_orbit treats any of these as "this job touches no external
    API/CLI/file format" and skips the probe station entirely. Any
    OTHER line in the section that is not a well-formed "- name: url"
    bullet raises ProbeTargetsError rather than being silently
    skipped -- a malformed target line must stop the job, not quietly
    disappear from the list engine.full_orbit runs probes against."""
    path = _facts_path(repo, job)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    section = _heading_section(text.splitlines(), _PROBE_TARGETS_HEADER)
    targets: list[tuple[str, str]] = []
    saw_empty_mark = False
    for line in section:
        stripped = line.strip()
        if not stripped:
            continue
        bare = stripped.lstrip("-").strip()
        if bare in (f"({_PROBE_EMPTY_MARK})", _PROBE_EMPTY_MARK):
            saw_empty_mark = True
            continue
        if not stripped.startswith("-"):
            raise ProbeTargetsError(f"malformed_probe_target_line: {line!r}")
        body = stripped[1:].strip()
        name, sep, url = body.partition(":")
        name, url = name.strip(), url.strip()
        if not sep or not name or not url:
            raise ProbeTargetsError(f"malformed_probe_target_line: {line!r}")
        targets.append((name, url))
    if saw_empty_mark and targets:
        # "(없음)" and one or more real target bullets both appeared in
        # the same section -- contradictory, never resolved by quietly
        # picking one side.
        raise ProbeTargetsError("probe_targets_mixed_empty_marker")
    if saw_empty_mark:
        return []
    return targets


def probe_target_id(index: int) -> str:
    """The stable, collision-free id for the target at `index` (0-based
    position in a check_probe_targets() list) -- "t1", "t2", ... in
    1-based list order. See check_probe_targets' own docstring for why
    this replaced deriving an id from the target's own name."""
    return f"t{index + 1}"


def check_probe(repo: Path, job: str, targets: list[tuple[str, str]]) -> list[str]:
    """[] iff every target in `targets` has its own
    .gongbang/<job>/probe/t<N>.log (probe_target_id(i), i = its
    position in `targets`) whose content contains "PROBE OK". A
    missing log or one without that marker becomes a plain problem
    string naming the target (by its real name, for a human reading
    the result -- only the FILE is looked up by id), never an
    exception -- engine.full_orbit turns a non-empty result into a
    needs_owner stop ("probe_failed:<names>"), not a crash."""
    probe_dir = repo / ".gongbang" / job / "probe"
    problems: list[str] = []
    for i, (name, _url) in enumerate(targets):
        log_path = probe_dir / f"{probe_target_id(i)}.log"
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
    "probe_target_id", "ProbeTargetsError",
]
