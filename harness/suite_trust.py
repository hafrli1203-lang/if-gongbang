# Generated from the private ops workspace at b94af62b97af64e2e58e8c102fa2538f066ae366; do not edit by hand (see harness/export_ops_public.py).
"""Test-run trust checks (묶음 5 — 시험 신뢰성).

A test command that exits 0 is not the same as a test command that proved
anything. This catches the ways a test run can look like an ordinary clean
pass while nothing real happened:

  - 0 tests actually executed, or every selected test was skipped
    (`looks_like_zero_tests_ran` / `looks_like_zero_tests_ran_pytest`) --
    these two regex sets are lifted VERBATIM from
    /apps/if-gongbang-v2/engine/application/steps/suite_zero_tests.py
    (vitest side) and verified against THIS host's real vitest/pytest
    output (see the two probe transcripts in the module docstrings below).
  - the test COUNT dropped sharply vs. the last recorded run of the same
    command (default threshold -30%) -- a suite that quietly lost most of
    its own cases while staying exit-0 is the same forgery, just slower
    (`count_dropped`).
  - a bench/grader case set has no negative control, so it can never be
    shown to fire on a real failure (`run_bench`/`Case`) -- lifted in
    spirit from if-gongbang-v2/engine/domain/suite_trust.py, restated
    without that engine's StepContext/ledger coupling so it works standalone
    here.

Nothing here spawns a process or touches a project's own test command --
callers (review-gate.sh, wt-land.sh, dev-report.js -- see INTEGRATION.md)
hand this module the ALREADY-CAPTURED stdout+stderr and exit code of a run
they made themselves; this module only judges whether that output can be
trusted as evidence of anything.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# 1) zero-tests-ran signal (vitest side -- verbatim regexes, re-verified
#    2026-11-02 against this host's installed vitest 3.2.7 in
#    /apps/exampleapp: `npx vitest run --testNamePattern
#    "zzzznomatch123xyz"` printed
#    "Tests  2579 skipped (2579)" / exit 0 -- matches _VITEST_ALL_SKIPPED_RE)
# ---------------------------------------------------------------------------

VITEST_NO_TESTS_RE = re.compile(r"No test files found", re.I)
_VITEST_ZERO_TESTS_SUMMARY_RE = re.compile(r"^\s*Tests\s+0\s", re.I | re.M)
# A "Tests" summary line that never mentions "passed" or "failed" anywhere on
# it, but does mention "skipped": every selected test was skipped, not run
# and found correct. A genuinely mixed line like "3 passed | 2 skipped (5)"
# does not match (at least one real pass/fail outcome occurred).
_VITEST_ALL_SKIPPED_RE = re.compile(
    r"^\s*Tests\s+(?!.*\b(?:passed|failed)\b).*\bskipped\b", re.I | re.M)


def looks_like_zero_tests_ran_vitest(combined_output: str) -> bool:
    """True iff `combined_output` carries vitest's own "nothing was
    actually exercised" shape: "No test files found" (vitest's own
    passWithNoTests default makes this exit 0, looking exactly like a clean
    pass), a "Tests  0 ..." summary, or a "Tests ... skipped" summary with
    no passed/failed count on that line."""
    return bool(VITEST_NO_TESTS_RE.search(combined_output)
                or _VITEST_ZERO_TESTS_SUMMARY_RE.search(combined_output)
                or _VITEST_ALL_SKIPPED_RE.search(combined_output))


# ---------------------------------------------------------------------------
# pytest side -- verified 2026-11-02 against this host's venv
# (/apps/if-gongbang-v2/.venv): `pytest -k "zzzznomatch123"
# tests/unit/test_evals_schema.py` exits 5 ("no tests collected") -- NOT 0,
# so pytest already fails closed on the common case without any text
# matching. The regex below is a secondary net only, for a wrapper script
# that swallows pytest's exit 5 into its own exit 0 (e.g. `pytest ...; true`
# in a CI step) -- pytest's own documented "no tests ran" wording.
# ---------------------------------------------------------------------------

_PYTEST_NO_TESTS_RE = re.compile(r"no tests ran\b", re.I)
_PYTEST_COLLECTED_ZERO_RE = re.compile(r"collected 0 items", re.I)
# A summary line with a skip count but no passed/failed/error count on it:
# every selected test was skipped outright, same shape as vitest's own
# all-skipped signal above. Verified 2026-11-02 (Codex review P1) against
# this host's real pytest: a run where every test is skip-marked prints
# "22 skipped in 0.06s" / exit 0 -- no "no tests ran", no "collected 0
# items" -- so those two alone missed it.
_PYTEST_SKIPPED_COUNT_RE = re.compile(r"\b\d+\s+skipped\b", re.I)
_PYTEST_PASSED_FAILED_ERROR_RE = re.compile(r"\b(?:passed|failed|error)\b", re.I)


def _pytest_output_all_skipped(combined_output: str) -> bool:
    """Checks only the LAST line carrying a "N skipped" count -- pytest's
    real final summary is always the last such line in the output,
    verified 2026-11-02 against this host's pytest (both `-q` and `-v`:
    "1 passed, 1 skipped in ..." / "2 skipped in ..."). Taking the last
    match (not any match) means an earlier, unrelated line that happens
    to carry a bare skip count can never override a final summary that
    actually shows real passes (Codex review 2026-11-02, second pass:
    a hypothetical mid-run collection line was raised as a false-positive
    risk -- scanning only the last match rules that out regardless of
    what any earlier line says)."""
    last_match_line: str | None = None
    for line in combined_output.splitlines():
        if _PYTEST_SKIPPED_COUNT_RE.search(line):
            last_match_line = line
    if last_match_line is None:
        return False
    return not _PYTEST_PASSED_FAILED_ERROR_RE.search(last_match_line)


def looks_like_zero_tests_ran_pytest(combined_output: str, returncode: int) -> bool:
    """True iff pytest's own output says nothing ran, REGARDLESS of
    returncode -- pytest's documented exit code 5 ("no tests collected")
    already catches the ordinary case; the text checks also catch a
    wrapper that reports its own exit 0 on top of that, AND a run where
    every test was skip-marked (exit 0, no "no tests ran" wording at
    all -- see _pytest_output_all_skipped)."""
    if returncode == 5:
        return True
    return bool(_PYTEST_NO_TESTS_RE.search(combined_output)
                or _PYTEST_COLLECTED_ZERO_RE.search(combined_output)
                or _pytest_output_all_skipped(combined_output))


def looks_like_zero_tests_ran(combined_output: str, *, returncode: int = 0,
                               runner: str = "vitest") -> bool:
    """Dispatches to the vitest or pytest zero-tests signal by `runner`
    ("vitest" or "pytest"); an unknown runner name is checked with BOTH
    signals (never silently skipped just because the caller did not name
    a known runner)."""
    if runner == "vitest":
        return looks_like_zero_tests_ran_vitest(combined_output)
    if runner == "pytest":
        return looks_like_zero_tests_ran_pytest(combined_output, returncode)
    return (looks_like_zero_tests_ran_vitest(combined_output)
            or looks_like_zero_tests_ran_pytest(combined_output, returncode))


# ---------------------------------------------------------------------------
# 2) test-count sharp-drop signal
# ---------------------------------------------------------------------------

_VITEST_TEST_COUNT_RE = re.compile(
    r"^\s*Tests\s+(?:\d+\s+failed\s*\|\s*)?(\d+)\s+passed", re.I | re.M)
_VITEST_TEST_FILES_TOTAL_RE = re.compile(r"\((\d+)\)\s*$", re.M)
_PYTEST_COLLECTED_N_RE = re.compile(r"collected (\d+) items?", re.I)
_PYTEST_SUMMARY_PASSED_RE = re.compile(r"(\d+) passed", re.I)


def extract_test_count(combined_output: str, *, runner: str = "vitest") -> int | None:
    """Best-effort count of tests actually run, from the runner's own
    summary line. None when no recognizable summary is present (caller
    must then skip the drop check rather than treat None as 0 -- a run
    whose summary this parser cannot read is not evidence of a drop)."""
    if runner == "pytest":
        m = _PYTEST_SUMMARY_PASSED_RE.search(combined_output)
        if m:
            return int(m.group(1))
        m = _PYTEST_COLLECTED_N_RE.search(combined_output)
        return int(m.group(1)) if m else None
    m = _VITEST_TEST_COUNT_RE.search(combined_output)
    return int(m.group(1)) if m else None


def count_dropped(previous_count: int, current_count: int, *,
                   threshold: float = 0.30) -> bool:
    """True iff `current_count` fell by `threshold` (default 30%) or more
    from `previous_count`. previous_count <= 0 never counts as a drop (no
    prior record to compare against -- the FIRST recorded run of a command
    cannot be judged against itself)."""
    if previous_count <= 0:
        return False
    if current_count >= previous_count:
        return False
    return (previous_count - current_count) / previous_count >= threshold


@dataclass(frozen=True)
class CountHistory:
    """Append-only record of test counts per command, keyed by the exact
    command string the caller ran. Stored as plain JSON -- no engine/ledger
    dependency, callers (review-gate.sh et al, see INTEGRATION.md) own
    picking a path under their own app's state directory."""
    counts: dict[str, int] = field(default_factory=dict)

    @staticmethod
    def load(path: Path) -> "CountHistory":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return CountHistory()
        if not isinstance(data, dict):
            return CountHistory()
        return CountHistory({k: int(v) for k, v in data.items() if isinstance(v, int)})

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.counts, ensure_ascii=False, indent=2), encoding="utf-8")

    def check_and_record(self, command_key: str, current_count: int | None,
                          *, threshold: float = 0.30) -> bool:
        """True iff this run's count_dropped vs. the stored count for
        `command_key`. Records `current_count` for next time regardless of
        the verdict (a caller that wants to keep an old baseline after a
        detected drop should not call this until the drop is resolved).
        current_count is None -> no verdict (False) and nothing recorded
        (an unparsed summary must never overwrite a real baseline with
        nothing)."""
        if current_count is None:
            return False
        previous = self.counts.get(command_key, 0)
        dropped = count_dropped(previous, current_count, threshold=threshold)
        self.counts[command_key] = current_count
        return dropped


# ---------------------------------------------------------------------------
# 3) bench/grader negative-control check (standalone restatement of
#    if-gongbang-v2/engine/domain/suite_trust.py's Case/run_bench --
#    for a caller building its own case-based grader, e.g. run_evals.py's
#    pass_criteria checks, not for an ordinary vitest/pytest run)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Case:
    name: str
    run: Callable[[], bool]  # True = passed
    expect_fail: bool = False  # negative control: must come back False
    expected_signature: str | None = None
    signature_of: Callable[[], str | None] | None = None


@dataclass(frozen=True)
class BenchResult:
    passed: bool
    reason: str
    per_case: dict[str, bool] = field(default_factory=dict)


REASON_EMPTY = "empty"
REASON_ALWAYS_PASSES = "grader_always_passes"
REASON_NO_NEGATIVE_CONTROL = "no_negative_control"
REASON_CASE_FAILED = "case_failed"
REASON_DUPLICATE_NAME = "duplicate_case_name"
REASON_WRONG_SIGNATURE = "wrong_failure_signature"
REASON_MISSING_EXPECTED_SIGNATURE = "missing_expected_signature"
REASON_OK = "ok"


def _run_case(case: Case) -> tuple[bool, str | None]:
    try:
        passed = bool(case.run())
    except AssertionError:
        return False, "assertion"
    except Exception as exc:  # noqa: BLE001 - signature is the point
        return False, type(exc).__name__
    if passed:
        return True, None
    if case.signature_of is not None:
        return False, case.signature_of()
    return False, "assertion"


def run_bench(cases: list[Case]) -> BenchResult:
    """Order: empty -> duplicate names -> negative control present -> run
    cases -> an expect_fail case that passed => always_passes -> a normal
    case failed -> an expect_fail case's observed signature must match its
    expected_signature -> result."""
    if not cases:
        return BenchResult(False, REASON_EMPTY)

    names = [case.name for case in cases]
    if len(set(names)) != len(names):
        return BenchResult(False, REASON_DUPLICATE_NAME)

    if not any(case.expect_fail for case in cases):
        return BenchResult(False, REASON_NO_NEGATIVE_CONTROL)

    outcomes = {case.name: _run_case(case) for case in cases}
    per_case = {name: passed for name, (passed, _sig) in outcomes.items()}

    for case in cases:
        if case.expect_fail and per_case[case.name]:
            return BenchResult(False, REASON_ALWAYS_PASSES, per_case)

    for case in cases:
        if not case.expect_fail and not per_case[case.name]:
            return BenchResult(False, REASON_CASE_FAILED, per_case)

    for case in cases:
        if not case.expect_fail:
            continue
        if case.expected_signature is None:
            return BenchResult(False, REASON_MISSING_EXPECTED_SIGNATURE, per_case)
        _passed, signature = outcomes[case.name]
        if (signature or "") != case.expected_signature:
            return BenchResult(False, REASON_WRONG_SIGNATURE, per_case)

    return BenchResult(True, REASON_OK, per_case)
