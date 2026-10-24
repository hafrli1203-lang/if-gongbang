"""Unit tests for engine/bench.py trust checks (plan §9)."""
from __future__ import annotations

from pathlib import Path

from engine.bench import (
    REASON_ALWAYS_PASSES,
    REASON_CASE_FAILED,
    REASON_EMPTY,
    REASON_NO_NEGATIVE_CONTROL,
    REASON_OK,
    REASON_SCANNER,
    REASON_STALE,
    BenchResult,
    Case,
    FailureRecord,
    ScanResult,
    load_result,
    reproduces,
    run_bench,
    save_result,
)

FP = "a" * 40


def honest_cases() -> list[Case]:
    return [
        Case("positive", run=lambda: True),
        Case("negative", run=lambda: False, expect_fail=True,
             expected_signature="assertion"),
    ]


def test_empty_cases_rejected() -> None:
    result = run_bench([], FP)
    assert result.passed is False
    assert result.reason == REASON_EMPTY


def test_no_negative_control_rejected() -> None:
    cases = [Case("only_positive", run=lambda: True)]
    result = run_bench(cases, FP)
    assert result.passed is False
    assert result.reason == REASON_NO_NEGATIVE_CONTROL


def test_always_passing_grader_rejected() -> None:
    cases = [
        Case("positive", run=lambda: True),
        Case("negative", run=lambda: True, expect_fail=True),
    ]
    result = run_bench(cases, FP)
    assert result.passed is False
    assert result.reason == REASON_ALWAYS_PASSES


def test_normal_case_failure_rejected() -> None:
    cases = [
        Case("positive", run=lambda: False),
        Case("negative", run=lambda: False, expect_fail=True),
    ]
    result = run_bench(cases, FP)
    assert result.passed is False
    assert result.reason == REASON_CASE_FAILED
    assert result.per_case["positive"] is False


def test_case_exception_counts_as_failure() -> None:
    def boom() -> bool:
        raise RuntimeError("explode")

    cases = [
        Case("positive", run=boom),
        Case("negative", run=lambda: False, expect_fail=True),
    ]
    result = run_bench(cases, FP)
    assert result.passed is False
    assert result.reason == REASON_CASE_FAILED
    assert result.per_case["positive"] is False


def test_scanner_exception_rejected() -> None:
    def bad_scanner(paths: list[Path]) -> ScanResult:
        raise RuntimeError("scanner crashed")

    result = run_bench(honest_cases(), FP, scanner=bad_scanner, scan_paths=[])
    assert result.passed is False
    assert result.reason == REASON_SCANNER


def test_scanner_unsupported_rejected() -> None:
    def partial_scanner(paths: list[Path]) -> ScanResult:
        return ScanResult(ok=True, unsupported=("x.bin",))

    result = run_bench(honest_cases(), FP, scanner=partial_scanner, scan_paths=[])
    assert result.passed is False
    assert result.reason == REASON_SCANNER


def test_scanner_not_ok_rejected() -> None:
    def failing_scanner(paths: list[Path]) -> ScanResult:
        return ScanResult(ok=False, error="disk error")

    result = run_bench(honest_cases(), FP, scanner=failing_scanner, scan_paths=[])
    assert result.passed is False
    assert result.reason == REASON_SCANNER


def test_honest_bench_passes() -> None:
    def good_scanner(paths: list[Path]) -> ScanResult:
        return ScanResult(ok=True)

    result = run_bench(honest_cases(), FP, scanner=good_scanner, scan_paths=[])
    assert result.passed is True
    assert result.reason == REASON_OK
    assert result.per_case["positive"] is True
    assert result.per_case["negative"] is False


def test_reproduces_match() -> None:
    case = Case("c", run=lambda: False, expect_fail=True,
                expected_signature="assertion")
    record = FailureRecord(case_name="c", signature="assertion")
    assert reproduces(record, case) is True


def test_reproduces_dependency_error_not_accepted() -> None:
    case = Case("c", run=lambda: False, expect_fail=True,
                expected_signature="assertion")
    record = FailureRecord(case_name="c", signature="ModuleNotFoundError")
    assert reproduces(record, case) is False


def test_reproduces_no_expected_signature() -> None:
    case = Case("c", run=lambda: False, expect_fail=True)
    record = FailureRecord(case_name="c", signature="assertion")
    assert reproduces(record, case) is False


def test_load_result_stale(tmp_path: Path) -> None:
    path = tmp_path / "bench.json"
    old = BenchResult(True, REASON_OK, "b" * 40, {"x": True})
    save_result(path, old)
    loaded = load_result(path, FP)
    assert loaded.passed is False
    assert loaded.reason == REASON_STALE


def test_load_result_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "missing.json"
    loaded = load_result(path, FP)
    assert loaded.passed is False
    assert loaded.reason == REASON_STALE
    assert loaded.fingerprint == FP


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "bench.json"
    original = BenchResult(True, REASON_OK, FP, {"positive": True, "negative": False})
    save_result(path, original)
    loaded = load_result(path, FP)
    assert loaded.passed is True
    assert loaded.reason == REASON_OK
    assert loaded.fingerprint == FP
    assert loaded.per_case == {"positive": True, "negative": False}
