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
              need a stable per-target id -- engine.application.acceptance.
              run_probe_scripts, this module's own check_probe --
              derive it from POSITION in this list, 1-based ("t1",
              "t2", ...), never from the target's own name: a name-
              derived id (the old scheme) collapses every Korean-only
              name to the same fallback id and "Naver API"/"Naver_API"
              to the same id too, since both get lowercased and every
              character outside [a-z0-9-] collapsed to "-" -- letting
              one probe script silently stand in for several distinct
              targets). [] only when facts.md itself does not exist yet
              (the facts gate -- check_facts -- already blocks the job
              before this point for that case) OR the section's only
              content is the sentinel "(없음)" (optionally bulleted
              "- (없음)") -- engine.application.pipeline treats either of these as
              "this job touches no external API/CLI/file format" and
              skips the probe station entirely. A facts.md that EXISTS
              but has no "## 외부 대상" heading at all raises
              ProbeTargetsError("probe_targets_section_missing") --
              never treated the same as an explicit "(없음)": a section
              a person simply forgot to write is a gap to fix, not "no
              targets". A line in that section that is neither "(없음)"
              (explicitly "no targets") nor a well-formed "- name: url"
              bullet raises ProbeTargetsError -- a malformed line is
              never silently dropped, since a target engine.application.pipeline
              never saw is a target it never ran a probe script for.
              A section that mixes "(없음)" with one or more real
              target bullets also raises ProbeTargetsError
              ("probe_targets_mixed_empty_marker") -- "no targets" and
              "here are the targets" are contradictory, never resolved
              by quietly picking one. When a bullet's doc value is an
              http(s) URL, only its FIRST whitespace-delimited token is
              kept as the url -- any trailing explanation on the same
              line (결함 19,: a real bullet read
              "https://…/cli-reference — probe t1 PROBE OK(단, …)") is
              dropped, never kept as part of the url a probe script's
              own "PROBE OK <url>" line is matched against. A target
              with no http(s) scheme (an internal/사내 target with no
              public doc) gets the same treatment from
              _cut_doc_value_explanation (결함 23,: a
              trailing explanation appended after a " — "/" -- "/" - "
              separator that sits OUTSIDE any "(...)" the value itself
              opens is dropped the same way; a separator inside that
              value's own parentheses is kept, since the real probe
              script's own "PROBE OK <문서>" line prints that whole
              parenthetical.
check_probe   [] iff every target in a check_probe_targets() list has
              its own .gongbang/<job>/probe/t<N>.log (1-based position
              in the SAME list, see check_probe_targets) containing
              "PROBE OK" -- a missing log or one without that marker is
              a problem, never a crash. Legacy/unused by the live
              orbit flow today (engine.application.pipeline calls
              engine.application.acceptance.run_probe_scripts instead, which
              actually re-executes each script rather than trusting a
              worker-written log; see that module's own docstring) --
              kept here, and fixed to the same id scheme, for whatever
              still calls it directly.
check_facts_trusted   Same check as check_facts, but reads facts.md
              exactly ONCE and hands the caller back an engine.infrastructure.trusted.
              Trusted over those exact bytes alongside the problem
              list (flow-map §⑤: the facts gate used to read facts.md
              here, and a caller that later puts "what facts.md says"
              into a station's instructions -- engine.application.pipeline --
              read it again, independently, on its own schedule; the
              two reads are only guaranteed to see the same content by
              TIMING). A caller that keeps this Trusted and calls
              engine.infrastructure.trusted.verify() on it immediately before using
              facts.md's content is acting on the bytes this check
              actually judged, not on whatever a second, later read of
              the same path happens to find. check_facts (unchanged)
              stays the plain, Trusted-free entry point for any caller
              that only needs the problem list.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from engine.infrastructure import trusted as _trusted

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger

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


def facts_path(repo: Path, job: str) -> Path:
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



def _has_heading(lines: list[str], header_phrase: str) -> bool:
    """True iff some line in `lines` is a heading line containing
    `header_phrase` -- the presence check check_probe_targets uses to
    tell "the section was never written" (an error) apart from "the
    section was written and says (없음)" (no targets)."""
    return any(_is_heading(line) and header_phrase in line for line in lines)


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
            # A header cell names its column when it CONTAINS the word
            # ("출처(파일:줄)" is still the source column), not only when
            # it is exactly that word.
            verdict_at = [k for k, c in enumerate(cells) if "판정" in c]
            source_at = [k for k, c in enumerate(cells) if _SOURCE_MARK in c]
            if verdict_at and source_at:
                verdict_col, source_col = verdict_at[0], source_at[0]
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue  # the |---|---| separator row
        verdict = cells[verdict_col] if verdict_col < len(cells) else ""
        if _PASS_MARK in verdict:
            source = cells[source_col] if source_col < len(cells) else ""
            result[i + 1] = source not in ("", "-")
    return result

def _check_facts_text(text: str) -> list[str]:
    """The structural half of check_facts, over already-read `text` --
    factored out so check_facts and check_facts_trusted (module
    docstring) can share it while each controls its OWN single read of
    facts.md (a plain path.read_text() for the former, engine.infrastructure.trusted.
    capture() for the latter) rather than this function reading
    anything itself."""
    problems: list[str] = []
    lines = text.splitlines()
    table_rows = _table_pass_rows(lines)
    for lineno, line in enumerate(lines, start=1):
        if lineno in table_rows:
            if not table_rows[lineno]:
                problems.append(f"line {lineno}: {_PASS_MARK} without {_SOURCE_MARK}")
            continue
        # Table rows are judged only through a table that has a 판정 column
        # (above). A row of any other table merely MENTIONING the word
        # (e.g. "다시 통과로 올림" in a 확인 필요 table) is not a claim.
        if line.strip().startswith("|"):
            continue
        # Outside tables only a LIST ITEM ("- 주장: 통과 (출처: …)") is a
        # claim; a prose paragraph that merely mentions the word is not
        # (a real facts.md was rejected for "… '통과' 또는 … 로 바뀐다").
        if not line.lstrip().startswith(("- ", "* ")):
            continue
        if _PASS_MARK in line and _SOURCE_MARK not in line:
            problems.append(f"line {lineno}: {_PASS_MARK} without {_SOURCE_MARK}")

    code_values_lines = _code_values_section(lines)
    for line in _needs_check_claims(code_values_lines):
        problems.append(
            f"{_CODE_VALUES_HEADER}: {_NEEDS_CHECK_MARK} remaining ({line.strip()!r})"
        )
    return problems


def _needs_check_claims(lines: list[str]) -> list[str]:
    """Lines that CLAIM 확인 필요: a verdict-table row whose 판정 cell says
    it, or a list item carrying it. Prose, and a pass row whose own text
    merely mentions "별도 확인 필요", are not claims (a real facts.md was
    rejected for exactly that)."""
    found: list[str] = []
    verdict_col: int | None = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = _cells(stripped)
            if verdict_col is None:
                at = [k for k, c in enumerate(cells) if "판정" in c]
                verdict_col = at[0] if at else -1
                continue
            if all(set(c) <= set("-: ") for c in cells):
                continue
            if verdict_col >= 0 and verdict_col < len(cells) and _NEEDS_CHECK_MARK in cells[verdict_col]:
                found.append(line)
            continue
        verdict_col = None
        if stripped.startswith(("- ", "* ")) and _NEEDS_CHECK_MARK in stripped:
            found.append(line)
    return found


def check_facts(repo: Path, job: str) -> list[str]:
    """[] iff facts.md exists, every line claiming 통과 also carries a
    출처 marker on the same line, and the 「코드에 넣는 값」 section (if
    present) contains no 확인 필요 marker anywhere in it."""
    path = facts_path(repo, job)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ["facts_missing"]
    return _check_facts_text(text)


def check_facts_trusted(repo: Path, job: str) -> tuple[list[str], "_trusted.Trusted | None"]:
    """(check_facts(repo, job)'s own problem list, an engine.infrastructure.trusted.
    Trusted over the exact facts.md bytes this check just read) -- see
    module docstring and engine.infrastructure.trusted for why a caller that will
    later USE facts.md's content (rather than just gate on whether it
    passes) should call this instead of check_facts, keep the Trusted,
    and engine.infrastructure.trusted.verify() it immediately before that later use.
    The Trusted is None iff facts.md could not be read at all (problems
    == ["facts_missing"] or a path engine.infrastructure.trusted.capture() itself
    refuses -- a symlinked component, a non-regular file, one over
    engine.infrastructure.trusted.TRUSTED_MAX_BYTES -- folded into the same
    "facts_missing" problem here, since either way there is no content
    for a caller to carry forward)."""
    path = facts_path(repo, job)
    try:
        captured = _trusted.capture(path)
    except _trusted.TrustCaptureError:
        return ["facts_missing"], None
    text = captured.data.decode("utf-8", errors="replace")
    return _check_facts_text(text), captured


def check_design_approved(ledger: Ledger, project: str, job: str) -> bool:
    for _seq, event in ledger.events(project, "design.approved"):
        if event.payload.get("job") == job:
            return True
    return False


def _is_probe_section_note(stripped: str) -> bool:
    return stripped.startswith("> ") or (stripped.startswith("(") and stripped.endswith(")"))


def check_probe_targets(repo: Path, job: str) -> list[tuple[str, str]]:
    """facts.md's "## 외부 대상" section, parsed line by line:
    "- <name>: <공식 문서 URL>" -> (name, url), in the order they
    appear. [] (never raises) only if facts.md does not exist yet (the
    facts gate already blocks the job before this point for that
    case), or the section's only content is the sentinel "(없음)"
    (optionally bulleted "- (없음)") -- engine.application.pipeline treats that
    as "this job touches no external API/CLI/file format" and skips
    the probe station entirely. facts.md existing with NO "## 외부
    대상" heading at all raises ProbeTargetsError
    ("probe_targets_section_missing") -- a forgotten section is never
    silently read as "no targets". A heading that IS present but whose
    section has no real target bullet AND no explicit "(없음)" marker
    (blank, or only whitespace) also raises ProbeTargetsError
    ("probe_targets_section_empty") -- "no targets" must always be
    SAID, never merely implied by writing nothing under the heading;
    without this, a planner who simply forgot to fill the section in
    would silently skip the probe station entirely, the same gap as
    the missing-heading case this already guards against. Any OTHER
    line in a present section that is not a well-formed "- name: url"
    bullet also raises ProbeTargetsError rather than being silently
    skipped -- a malformed target line must stop the job, not quietly
    disappear from the list engine.application.pipeline runs probes against."""
    path = facts_path(repo, job)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    lines = text.splitlines()
    if not _has_heading(lines, _PROBE_TARGETS_HEADER):
        raise ProbeTargetsError("probe_targets_section_missing")
    section = _heading_section(lines, _PROBE_TARGETS_HEADER)
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
        if _is_probe_section_note(stripped):
            # An explicit note -- a whole line in parentheses or a "> "
            # quote -- is not a target (a real facts.md stopped on
            # "(probe = …)"). Any other non-bullet line still raises:
            # a target written in the wrong shape must never vanish.
            continue
        if not stripped.startswith("-"):
            raise ProbeTargetsError(f"malformed_probe_target_line: {line!r}")
        body = stripped[1:].strip()
        name, sep, url = body.partition(":")
        name, url = name.strip(), url.strip()
        if not sep or not name or not url:
            raise ProbeTargetsError(f"malformed_probe_target_line: {line!r}")
        # 결함 19, 마케팅 채팅 job): a real bullet's doc value
        # was "https://.../cli-reference — probe t1 PROBE OK(단, ...)" --
        # an http(s) URL with a human explanation appended on the SAME
        # line, after the url -- so the url this function returned was
        # that whole trailing string, never the probe script's own
        # `PROBE OK <url>` line (which only ever prints the bare
        # address). _probe_observed (engine.application.acceptance_probe)
        # then compared the script's real "PROBE OK https://.../
        # cli-reference" against this longer string and failed every
        # target whose planner wrote a doc value this way, even though
        # the probe itself had actually succeeded. An http(s) url is
        # split on its first run of whitespace -- the url is the FIRST
        # token; anything after it on the same line is an explanation,
        # not part of the address, and is dropped here (never kept as
        # part of url, never raised as malformed -- PROBE_TARGETS_
        # SECTION_FORMAT_RULE already asks for that explanation on its
        # OWN note line instead). A target with no http(s) scheme (an
        # internal/사내 target with no real URL) is never split this
        # way -- its whole value after ":" stays the url, unchanged.
        if url.startswith("http://") or url.startswith("https://"):
            url = url.split(None, 1)[0]
        else:
            # 결함 23: a non-url doc value (a "사내" target
            # with no public doc) can carry the SAME kind of trailing
            # explanation an http(s) url above already strips -- see
            # _cut_doc_value_explanation's own docstring for the real
            # ledger evidence this repairs.
            url = _cut_doc_value_explanation(url)
        targets.append((name, url))
    if saw_empty_mark and targets:
        # "(없음)" and one or more real target bullets both appeared in
        # the same section -- contradictory, never resolved by quietly
        # picking one side.
        raise ProbeTargetsError("probe_targets_mixed_empty_marker")
    if saw_empty_mark:
        return []
    if not targets:
        # The heading exists but the section said nothing at all -- not
        # even the explicit "(없음)" that means "no targets". Writing
        # nothing here must never be read the same as writing "(없음)".
        raise ProbeTargetsError("probe_targets_section_empty")
    return targets


_DOC_VALUE_EXPLANATION_SEPARATORS = (" — ", " -- ", " - ")


def _cut_doc_value_explanation(value: str) -> str:
    """`value` (a non-http(s) "## 외부 대상" doc value, already stripped
    of its "- name:" prefix) with any trailing human explanation cut
    off at the first _DOC_VALUE_EXPLANATION_SEPARATORS match found at
    PAREN DEPTH 0 -- never inside a "(...)" the value itself opens.

    결함 23, 마케팅 채팅 job, ledger seq 603/733, read via
    probe.executed): a real bullet's doc value for an internal target
    with no public doc (an OS library API) was "(공식 문서 URL 없음 —
    사내 API; kit/os 패키지나 sample-app-OS 저장소 안에 있을 것으로
    추정되나 이 샌드박스에서 열람 불가) — probe t5는 다른 경로
    (sample-app-library MCP)를 테스트했던 것이라 이 결정과는 별도로 다시
    probe 필요" -- a parenthetical explanation of WHY there is no url,
    followed by a second, unrelated note about a different probe
    attempt, appended on the SAME line after " — ". The probe script
    itself (seq 603's own stdout_tail) only ever printed "PROBE OK "
    plus the FIRST parenthetical -- "(공식 문서 URL 없음 — 사내 API; …
    열람 불가)" -- never the trailing "— probe t5는 …" clause, so
    check_probe_targets must keep only that first parenthetical as the
    url this gate compares against, exactly the way an http(s) url
    already keeps only its first whitespace token (결함 19 above).

    A plain first-occurrence cut would be WRONG here: the first " — "
    in the real value above sits INSIDE the opening "(...)" (between
    "없음" and "사내"), at paren depth 1 -- cutting there would lose the
    rest of that same parenthetical ("사내 API; … 열람 불가)") that the
    script's own stdout DOES include. Only the " — " that comes AFTER
    the matching ")" (depth back to 0) is a genuine second explanation
    to drop. A value with no parens at all has no depth-1 region, so
    its first separator (if any) is already at depth 0 and is cut
    exactly as the plain reading of the rule says.

    Going forward PROBE_TARGETS_SECTION_FORMAT_RULE instead asks a
    target with no public doc to write only "(사내)" in the doc slot,
    with any explanation on its own note line -- this function is the
    defensive floor under an already-written facts.md (this historical
    one, or a future one that still gets the convention wrong), never
    a reason to skip following that convention."""
    depth = 0
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if depth == 0:
            for sep in _DOC_VALUE_EXPLANATION_SEPARATORS:
                if value.startswith(sep, i):
                    return value[:i]
        i += 1
    return value


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
    exception -- engine.application.pipeline turns a non-empty result into a
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


# ---------------------------------------------------------------------------
# Station -> own record path table (결함 12,: the actual
# incident -- a prober worker appended an explanatory line to facts.md
# ("## 외부 대상" section) during the probe station, which the NEXT
# facts.md read choked on as a malformed target line
# (probe_targets_invalid:malformed_probe_target_line), wedging the job
# with no way forward. The root cause: EngineConfig.station_owners
# grants facts/probe/plan/design every one the WHOLE ".gongbang/" tree
# (see tools/templates/*.json), so merge.py's own config-driven prefix
# check never saw anything wrong with the prober's copy touching
# facts.md -- any early station can write any OTHER early station's
# own record.
#
# This table is the authoritative, config-INDEPENDENT floor underneath
# that shared ".gongbang/" grant (same role _PLANNER_ONLY_PREFIX plays
# for tests/scenes/ in engine.application.steps.merge): station_owners'
# own ".gongbang/" prefix may stay as broad as it already is, but
# engine.application.steps.merge._deny_if_other_station_record checks
# every write a copy makes against THIS table first and refuses
# (StepFailed("wrote_other_station_record:<path>"), never silently
# dropped) any write that lands on a path some OTHER station's own
# entry here claims. A station absent from this table (e.g. "build",
# "review", "bench", "report") is not narrowed by it at all -- merge's
# existing config/owns-driven prefixes keep deciding those, and a merge
# call that carries its own explicit per-request "owns" (e.g. a build
# task's own narrowed paths, or quick_pipeline's combined facts+plan
# turn -- see engine.application.steps.merge._owner_prefixes) is
# like-wise exempt: an explicit owns is already an engine-authored,
# reviewed declaration of exactly what THIS call may touch, never
# something a worker can forge on its own (gate_facts.json's own "owns"
# is set by the engine process before the worker ever runs, see
# engine.application.steps.worker_gate_log.task_owns), so it is trusted
# to state a deliberate cross-station grant (e.g. a UI build task
# also dropping a screenshot under .gongbang/<job>/screens/, which this
# table otherwise reserves for "design").
_STATION_RECORD_TEMPLATES: dict[str, tuple[str, ...]] = {
    "facts": ("facts.md",),
    "probe": ("probe/",),
    "plan": ("plan.json",),
    "design": ("design/", "screens/"),
}


def station_record_prefixes(station: str, job: str) -> tuple[str, ...]:
    """The repo-relative paths `station` owns as ITS OWN record for
    `job`, per _STATION_RECORD_TEMPLATES -- () for a station with no
    entry in that table (every write it makes is judged only by
    merge's existing config/owns-driven prefixes, never by this
    table)."""
    templates = _STATION_RECORD_TEMPLATES.get(station)
    if not templates:
        return ()
    return tuple(f".gongbang/{job}/{t}" for t in templates)


def _covers(prefix: str, rel: str) -> bool:
    if prefix.endswith("/"):
        return rel == prefix.rstrip("/") or rel.startswith(prefix)
    return rel == prefix


def other_station_record_violation(station: str, job: str, rel: str) -> str | None:
    """None iff `rel` is not claimed, by _STATION_RECORD_TEMPLATES, as
    some OTHER station's own record -- i.e. `station` writing `rel` is
    fine as far as this table is concerned (either `rel` is `station`'s
    own record, or no station's record at all). Otherwise the
    offending prefix (e.g. ".gongbang/job1/facts.md"), for the caller's
    StepFailed reason to name."""
    for other, templates in _STATION_RECORD_TEMPLATES.items():
        if other == station:
            continue
        for prefix in (f".gongbang/{job}/{t}" for t in templates):
            if _covers(prefix, rel):
                return prefix
    return None


def station_record_rule_sentence(station: str, job: str) -> str:
    """"이 칸은 <경로>만 쓴다 -- 다른 칸 기록(예: facts.md)은 고치지
    않는다" for `station`, naming station_record_prefixes' own paths
    verbatim -- engine.application.stage_prompts splices this into
    that station's own prompt so the rule a worker is told and the
    rule merge.py actually enforces (other_station_record_violation)
    can never drift apart, same pattern as the other *_RULE sentences
    below. "" for a station with no table entry (nothing to tell it)."""
    prefixes = station_record_prefixes(station, job)
    if not prefixes:
        return ""
    paths = ", ".join(prefixes)
    return f"이 칸은 {paths}만 쓴다 — 다른 칸 기록(예: facts.md)은 고치지 않는다."


# ---------------------------------------------------------------------------
# Rule sentences -- the literal Korean text a worker's own instructions
# (engine.application.stage_prompts) must carry VERBATIM for each check
# above, written once HERE next to the check it describes, so a prompt and
# the judgment code that grades it can never drift apart again (the actual
# incident this guards against: the prober prompt once said only "로그에
# PROBE OK" while run_probe_scripts/_probe_observed in
# engine.application.acceptance_probe required "PROBE OK <문서 URL>" on the
# script's own stdout -- a worker that followed the prompt to the letter
# still failed the probe). stage_prompts.py imports these constants/
# functions and splices them into its own prompt text rather than retyping
# the sentence; tests/unit/test_stage_rule_sentences.py asserts every
# sentence returned by *_rule_sentences() below is a literal substring of
# the station prompt that is supposed to tell the worker about it.

FACTS_PASS_NEEDS_SOURCE_RULE = (
    "「통과」 주장은 판정·출처 칸이 있는 표의 행이나 \"- 주장: 통과 (출처: …)\" "
    "목록 줄로만 쓰고, 반드시 출처(파일:줄, 문서 URL, 실행 로그)를 같은 행에 "
    "적는다 — 출처 없는 「통과」는 없다. 설명 문단은 주장으로 보지 않는다."
)

# Deliberately stops short of saying what happens when 확인 필요 remains
# (that consequence differs: "다음 단계로 넘어가지 못한다" for the full
# facts station, "끝나지 못한다" for quick_pipeline's --small combined
# turn) -- stage_prompts.py appends its own station-specific consequence
# clause right after this, so the mechanism sentence itself (what to do,
# what check_facts actually enforces) stays identical in both prompts.
FACTS_CODE_VALUES_NO_NEEDS_CHECK_RULE = (
    "코드에 그대로 넣을 숫자·기준·API 형식이 있으면 「코드에 넣는 값」이라는 "
    "제목의 절에 모은다 — 그 절에 확인 필요가 남아 있으면 이 일은"
)

PROBE_TARGETS_SECTION_FORMAT_RULE = (
    "\"## 외부 대상\" 절을 쓴다 — 이 일이 의존하는 외부 "
    "API·CLI·파일 형식을 모두 한 줄씩 \"- <이름>: <공식 문서 URL>\" "
    "형태로 적는다. URL은 공백 없는 주소 하나만 적는다 — 같은 줄에 설명을 "
    "덧붙이지 않는다(실측 스크립트는 `PROBE OK <주소>`만 찍으므로, 주소 뒤에 "
    "말을 더 붙이면 그 설명까지 주소로 읽혀 실측 통과와 불일치가 난다). "
    "이 절의 설명은 줄 전체를 괄호로 감싸거나(\"( … )\") "
    "\"> \"로 시작하는 줄로만 쓴다 — 그 밖의 글은 이 절에 쓰지 않는다"
)

PROBE_TARGETS_EMPTY_MARKER_RULE = (
    "의존하는 외부 대상이 없으면 그 절 아래에 \"(없음)\"이라고만 적는다."
)

# 결함 23: 공식 문서가 없는 사내 대상의 긴 설명이 문서 칸에
# 그대로 들어가 실측 통과와 불일치가 난 실제 사고(마케팅 채팅 job) 뒤에
# 추가 -- main의 bd38249 스타일로 체크 바로 옆에 둔다.
PROBE_TARGETS_INTERNAL_DOC_RULE = (
    "공식 문서 URL이 없는 사내 대상은 문서 칸에 \"(사내)\"라고만 적는다 — "
    "왜 없는지·무엇으로 대신했는지 같은 설명은 문서 칸에 붙이지 않고 "
    "별도 줄(괄호로 감싼 줄이나 \"> \"로 시작하는 줄)에 적는다."
)

# check_probe (and run_probe_scripts/_probe_observed in
# engine.application.acceptance_probe, which checks the same two
# conditions against the script's own real stdout rather than a
# worker-written log) -- the pass condition stated to the probe worker.
PROBE_EXIT_AND_OK_LINE_RULE = (
    "통과 조건은 두 가지다: 종료 코드 0(실패면 0이 아닌 값으로 끝낸다), "
    "그리고 성공했을 때 표준 출력 마지막에 정확히 `PROBE OK <문서>` 한 줄을 "
    "찍는다 -- <문서>는 위 목록의 그 대상 \"문서:\" 값을 글자 그대로 "
    "쓴다(바꾸거나 줄이지 않는다)."
)


def probe_log_format_rule(job: str) -> str:
    """check_probe's own log-format expectation (first line names the
    target, then "PROBE OK"/"PROBE FAIL") -- takes `job` since the path
    it names is job-specific, unlike the other rule sentences above."""
    return (
        f".gongbang/{job}/probe/<번호>.log 를 쓴다 -- 첫 줄은 그 대상의 이름과 "
        "공식 문서 URL이고, 성공하면 \"PROBE OK\", 실패하면 \"PROBE "
        "FAIL\"과 에러 내용을 적은 뒤 실제 요청·응답 형태도 남긴다."
    )


def facts_rule_sentences() -> tuple[str, ...]:
    """Every rule sentence a facts-station prompt (facts_prompt,
    small_prompt) must carry verbatim -- check_facts' own two gates plus
    check_probe_targets' section format and empty-marker rule (facts.md
    is the only place that section is written)."""
    return (
        FACTS_PASS_NEEDS_SOURCE_RULE,
        FACTS_CODE_VALUES_NO_NEEDS_CHECK_RULE,
        PROBE_TARGETS_SECTION_FORMAT_RULE,
        PROBE_TARGETS_EMPTY_MARKER_RULE,
        PROBE_TARGETS_INTERNAL_DOC_RULE,
    )


def prober_rule_sentences(job: str) -> tuple[str, ...]:
    """Every rule sentence the prober-station prompt (prober_prompt)
    must carry verbatim -- the exact pass condition run_probe_scripts/
    _probe_observed checks against the script's own stdout, plus
    check_probe's own log-format expectation."""
    return (PROBE_EXIT_AND_OK_LINE_RULE, probe_log_format_rule(job))


__all__ = [
    "check_facts", "check_facts_trusted", "facts_path", "check_design_approved",
    "check_probe_targets", "check_probe", "probe_target_id", "ProbeTargetsError",
    "FACTS_PASS_NEEDS_SOURCE_RULE", "FACTS_CODE_VALUES_NO_NEEDS_CHECK_RULE",
    "PROBE_TARGETS_SECTION_FORMAT_RULE", "PROBE_TARGETS_EMPTY_MARKER_RULE",
    "PROBE_TARGETS_INTERNAL_DOC_RULE",
    "PROBE_EXIT_AND_OK_LINE_RULE", "probe_log_format_rule",
    "facts_rule_sentences", "prober_rule_sentences",
    "station_record_prefixes", "other_station_record_violation",
    "station_record_rule_sentence",
]
