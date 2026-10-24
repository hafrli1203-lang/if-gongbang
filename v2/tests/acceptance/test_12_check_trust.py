"""Acceptance test 12_check_trust — see plan §Tasks.

Scenario: the bench itself must not be fooled. An empty suite, an
always-passing grader, a wrong failure reproduction, a failing public
scanner, or a stale result file must never be accepted as a pass.
"""
from __future__ import annotations

from pathlib import Path

from engine.bench import (
    REASON_ALWAYS_PASSES,
    REASON_DUPLICATE_NAME,
    REASON_EMPTY,
    REASON_MISSING_EXPECTED_SIGNATURE,
    REASON_OK,
    REASON_SCANNER,
    REASON_STALE,
    REASON_WRONG_SIGNATURE,
    BenchResult,
    Case,
    FailureRecord,
    ScanResult,
    load_result,
    reproduces,
    run_bench,
    save_result,
)

FP = "c" * 40


def test_empty_suite_not_pass() -> None:
    result = run_bench([], FP)
    assert result.passed is False
    assert result.reason == REASON_EMPTY


def test_duplicate_case_name_not_pass() -> None:
    """D-13: a duplicate case name would silently overwrite an earlier
    case's result in per_case (a dict keyed by name) -- a real failure on
    the first 'check' case could be hidden behind a later, unrelated,
    passing case that happens to share its name. Reject outright."""
    cases = [
        Case("check", run=lambda: False),  # would actually fail the bench
        Case("check", run=lambda: True),  # same name, overwrites it
        Case("negative", run=lambda: False, expect_fail=True),
    ]
    result = run_bench(cases, FP)
    assert result.passed is False
    assert result.reason == REASON_DUPLICATE_NAME


def test_always_passing_grader_not_pass() -> None:
    cases = [
        Case("real_check", run=lambda: True),
        Case("must_fail", run=lambda: True, expect_fail=True),
    ]
    result = run_bench(cases, FP)
    assert result.passed is False
    assert result.reason == REASON_ALWAYS_PASSES


def test_wrong_failure_reproduction_not_accepted() -> None:
    # Expected: the negative-control case fails with an "assertion" error.
    # Actual: it failed because a dependency was missing. That is not a
    # reproduction of the expected failure, and must not be accepted.
    case = Case("negative_control", run=lambda: False, expect_fail=True,
                expected_signature="assertion")
    record = FailureRecord(case_name="negative_control",
                            signature="ModuleNotFoundError")
    assert reproduces(record, case) is False


def test_run_bench_rejects_missing_expected_signature() -> None:
    """#13 remainder: an expect_fail case with no expected_signature at
    all used to skip the reproduction check entirely (reproduces()
    returns False only when BOTH are set, so the old 'if ... is not
    None' guard just never ran it) -- any failure reason would silently
    be accepted as the negative control firing. It must be rejected."""
    cases = [
        Case("real_check", run=lambda: True),
        Case("must_fail", run=lambda: False, expect_fail=True),  # no expected_signature
    ]
    result = run_bench(cases, FP)
    assert result.passed is False
    assert result.reason == REASON_MISSING_EXPECTED_SIGNATURE


def test_run_bench_rejects_wrong_failure_signature() -> None:
    """The same check, but exercised through run_bench() end to end: a
    negative-control case failing for the wrong reason (a missing module,
    not the assertion it was written to catch) must not be accepted as a
    passing bench, even though a case "failed" as expect_fail demands."""
    def boom() -> bool:
        raise ModuleNotFoundError("no such module")

    cases = [
        Case("real_check", run=lambda: True),
        Case("must_fail", run=boom, expect_fail=True, expected_signature="assertion"),
    ]
    result = run_bench(cases, FP)
    assert result.passed is False
    assert result.reason == REASON_WRONG_SIGNATURE


def test_run_bench_accepts_matching_assertion_signature() -> None:
    """The honest-failure path: an expect_fail case that fails by actually
    returning False (an "assertion") matches its declared
    expected_signature and the bench may proceed to pass."""
    cases = [
        Case("real_check", run=lambda: True),
        Case("must_fail", run=lambda: False, expect_fail=True,
             expected_signature="assertion"),
    ]

    def ok_scanner(paths: list[Path]) -> ScanResult:
        return ScanResult(ok=True)

    result = run_bench(cases, FP, scanner=ok_scanner, scan_paths=[])
    assert result.passed is True
    assert result.reason == REASON_OK


def test_scanner_failure_not_pass() -> None:
    honest = [
        Case("real_check", run=lambda: True),
        Case("must_fail", run=lambda: False, expect_fail=True,
             expected_signature="assertion"),
    ]

    def crashing_scanner(paths: list[Path]) -> ScanResult:
        raise RuntimeError("public scanner crashed")

    result_exception = run_bench(honest, FP, scanner=crashing_scanner, scan_paths=[])
    assert result_exception.passed is False
    assert result_exception.reason == REASON_SCANNER

    def unsupported_scanner(paths: list[Path]) -> ScanResult:
        return ScanResult(ok=True, unsupported=("x.bin",))

    result_unsupported = run_bench(honest, FP, scanner=unsupported_scanner,
                                    scan_paths=[])
    assert result_unsupported.passed is False
    assert result_unsupported.reason == REASON_SCANNER


def test_stale_result_file_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bench.json"
    old_fingerprint = "d" * 40
    old_result = BenchResult(True, REASON_OK, old_fingerprint, {"check": True})
    save_result(path, old_result)

    now_fingerprint = "e" * 40
    loaded = load_result(path, now_fingerprint)
    assert loaded.passed is False
    assert loaded.reason == REASON_STALE


def test_honest_suite_passes() -> None:
    cases = [
        Case("real_check", run=lambda: True),
        Case("must_fail", run=lambda: False, expect_fail=True,
             expected_signature="assertion"),
    ]

    def ok_scanner(paths: list[Path]) -> ScanResult:
        return ScanResult(ok=True)

    result = run_bench(cases, FP, scanner=ok_scanner, scan_paths=[])
    assert result.passed is True
    assert result.reason == REASON_OK
