"""Detecting that a test run exercised nothing real, even though its
own exit code looked like an ordinary clean pass -- shared by
engine.application.steps.suite_run (the real test suite) and engine.application.acceptance
(scenes and the full suite), which both reuse this VERBATIM rather
than inventing a separate heuristic.
"""
from __future__ import annotations

import re


VITEST_NO_TESTS_RE = re.compile(r"No test files found", re.I)
_VITEST_NO_TESTS_RE = VITEST_NO_TESTS_RE  # internal alias used below
_VITEST_ZERO_TESTS_SUMMARY_RE = re.compile(r"^\s*Tests\s+0\s", re.I | re.M)
# A "Tests" summary line that never mentions "passed" or "failed"
# anywhere on it, but does mention "skipped" -- every selected test
# was skipped, not merely run and found correct, so nothing was
# actually exercised even though the file-selection step itself
# matched something (e.g. "Tests  1 skipped (1)"). The negative
# lookahead scans the rest of the line before "skipped" is reached, so
# a genuinely mixed line like "3 passed | 2 skipped (5)" does not
# match this (at least one real pass/fail outcome did occur there).
_VITEST_ALL_SKIPPED_RE = re.compile(
    r"^\s*Tests\s+(?!.*\b(?:passed|failed)\b).*\bskipped\b", re.I | re.M)


def looks_like_zero_tests_ran(combined_output: str) -> bool:
    """True iff `combined_output` carries one of vitest's own "nothing
    was actually exercised" shapes (module docstring on the regexes
    just above this function): "No test files found" (an installed
    vitest's own `passWithNoTests=true` default makes that exit 0, not
    1, looking exactly like an ordinary clean pass), a "Tests  0 ..."
    summary, or a "Tests ... skipped" summary with no passed/failed
    count on that same line -- a selector/suite matching something is
    not the same as anything actually having been exercised. This is
    the one no-tests-ran signal this codebase has; engine.application.acceptance
    reuses it VERBATIM (never a separately invented heuristic) for
    scenes and the full suite, so a skipped test can never stand in
    as evidence -- an exit-0 run
    that executed nothing must never count as having proven anything.
    Vitest-specific wording only: a pytest-based project's own
    "collected 0 items"/all-deselected shape is not recognized here --
    extending this to other runners is new-heuristic territory this
    function deliberately stays out of."""
    return bool(_VITEST_NO_TESTS_RE.search(combined_output)
                or _VITEST_ZERO_TESTS_SUMMARY_RE.search(combined_output)
                or _VITEST_ALL_SKIPPED_RE.search(combined_output))


def ran_zero_tests(scope: str, combined_output: str) -> bool:
    """True iff a "related"-scope run executed ZERO actual tests (see
    looks_like_zero_tests_ran for the actual signal). Only ever checked
    for scope "related" here -- a "full" run legitimately exits 0 on an
    empty/trivial project without THIS caller's own related->full
    fallback applying; bench_run._run_run_tests separately applies
    looks_like_zero_tests_ran to the run's own FINAL output regardless
    of scope (see its own comment), which is the P2 guard."""
    if scope != "related":
        return False
    return looks_like_zero_tests_ran(combined_output)
