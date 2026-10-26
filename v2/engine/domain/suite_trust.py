"""Bench (test-stand) trust checks. A bench result is only trusted when the
checks themselves are trustworthy (plan §9).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class Case:
    name: str
    run: Callable[[], bool]  # True = passed
    expect_fail: bool = False  # negative control: must come back False
    expected_signature: str | None = None  # for failure records
    signature_of: Callable[[], str | None] | None = None
    # Optional override consulted only when run() returns False without
    # raising (e.g. a subprocess-backed case, which can never raise a
    # Python exception type for its failure): lets the case derive its
    # own failure signature (e.g. by inspecting captured output) instead
    # of the default "assertion". None (the default) keeps the prior
    # behavior for every existing in-process Case.


@dataclass(frozen=True)
class FailureRecord:
    case_name: str
    signature: str  # e.g. "assertion", "ModuleNotFoundError", "timeout"


@dataclass(frozen=True)
class ScanResult:
    ok: bool
    unsupported: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class BenchResult:
    passed: bool
    reason: str
    fingerprint: str
    per_case: dict[str, bool] = field(default_factory=dict)


REASON_EMPTY = "empty"
REASON_ALWAYS_PASSES = "grader_always_passes"
REASON_NO_NEGATIVE_CONTROL = "no_negative_control"
REASON_CASE_FAILED = "case_failed"
REASON_DUPLICATE_NAME = "duplicate_case_name"
REASON_WRONG_SIGNATURE = "wrong_failure_signature"
REASON_MISSING_EXPECTED_SIGNATURE = "missing_expected_signature"
REASON_SCANNER = "scanner_failed"
REASON_STALE = "stale"
REASON_OK = "ok"


def _run_case(case: Case) -> tuple[bool, str | None]:
    """Run one case. Returns (passed, failure_signature); signature is
    None when the case passed, "assertion" when it returned False or
    raised AssertionError, otherwise the raised exception's type name
    (e.g. "ModuleNotFoundError") -- a dependency/import failure is not
    the same failure as the check actually firing."""
    try:
        passed = bool(case.run())
    except AssertionError:
        return False, "assertion"
    except Exception as exc:
        return False, type(exc).__name__
    if passed:
        return True, None
    if case.signature_of is not None:
        return False, case.signature_of()
    return False, "assertion"


def run_bench(cases: list[Case], fp: str,
              scanner: Callable[[list[Path]], ScanResult] | None = None,
              scan_paths: list[Path] | None = None) -> BenchResult:
    """Order of checks: empty -> duplicate case names -> negative control
    present -> run cases -> expect_fail case passed => always_passes -> a
    normal case failed -> an expect_fail case's observed failure
    signature must match its expected_signature (reproduces()) ->
    scanner ok -> result."""
    if not cases:
        return BenchResult(False, REASON_EMPTY, fp)

    names = [case.name for case in cases]
    if len(set(names)) != len(names):
        # A duplicate name means per_case[name] (a dict) can only hold one
        # case's outcome: an earlier failing case's result would silently
        # be overwritten by a later same-named passing case, hiding the
        # failure. Reject outright instead of risking that collision.
        return BenchResult(False, REASON_DUPLICATE_NAME, fp)

    if not any(case.expect_fail for case in cases):
        return BenchResult(False, REASON_NO_NEGATIVE_CONTROL, fp)

    outcomes = {case.name: _run_case(case) for case in cases}
    per_case = {name: passed for name, (passed, _sig) in outcomes.items()}

    for case in cases:
        if case.expect_fail and per_case[case.name]:
            return BenchResult(False, REASON_ALWAYS_PASSES, fp, per_case)

    for case in cases:
        if not case.expect_fail and not per_case[case.name]:
            return BenchResult(False, REASON_CASE_FAILED, fp, per_case)

    signature_failure = _check_signatures(cases, outcomes, per_case, fp)
    if signature_failure is not None:
        return signature_failure

    scanner_failure = _check_scanner(scanner, scan_paths, per_case, fp)
    if scanner_failure is not None:
        return scanner_failure

    return BenchResult(True, REASON_OK, fp, per_case)


def _check_signatures(
    cases: list[Case], outcomes: dict[str, tuple[bool, str | None]],
    per_case: dict[str, bool], fp: str,
) -> BenchResult | None:
    """None if every expect_fail case's observed failure signature
    matches its expected_signature; otherwise the rejecting BenchResult.
    An expect_fail case with no expected_signature at all would silently
    skip the reproduction check entirely -- any failure reason (a real
    assertion, a missing dependency, a timeout) would be accepted as "the
    negative control fired", which defeats the point of a negative
    control."""
    for case in cases:
        if not case.expect_fail:
            continue
        if case.expected_signature is None:
            return BenchResult(False, REASON_MISSING_EXPECTED_SIGNATURE, fp, per_case)
        _passed, signature = outcomes[case.name]
        record = FailureRecord(case_name=case.name, signature=signature or "")
        if not reproduces(record, case):
            return BenchResult(False, REASON_WRONG_SIGNATURE, fp, per_case)
    return None


def _check_scanner(
    scanner: Callable[[list[Path]], ScanResult] | None,
    scan_paths: list[Path] | None, per_case: dict[str, bool], fp: str,
) -> BenchResult | None:
    """None if there is no scanner or it reports ok with nothing
    unsupported; otherwise the rejecting BenchResult."""
    if scanner is None:
        return None
    try:
        scan = scanner(scan_paths or [])
    except Exception:
        return BenchResult(False, REASON_SCANNER, fp, per_case)
    if not scan.ok or scan.unsupported:
        return BenchResult(False, REASON_SCANNER, fp, per_case)
    return None


def reproduces(record: FailureRecord, case: Case) -> bool:
    """True iff record.signature == case.expected_signature (both set)."""
    if case.expected_signature is None:
        return False
    return record.signature == case.expected_signature


def load_result(path: Path, now_fp: str) -> BenchResult:
    """Read a stored bench.json; if its fingerprint != now_fp return a
    BenchResult(passed=False, reason=REASON_STALE)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fingerprint = data["fingerprint"]
        passed = data["passed"]
        reason = data["reason"]
        per_case = data.get("per_case", {})
    except Exception:
        return BenchResult(False, REASON_STALE, now_fp)

    if fingerprint != now_fp:
        return BenchResult(False, REASON_STALE, fingerprint, per_case)

    return BenchResult(passed, reason, fingerprint, per_case)


def save_result(path: Path, result: BenchResult) -> None:
    data = {
        "passed": result.passed,
        "reason": result.reason,
        "fingerprint": result.fingerprint,
        "per_case": result.per_case,
    }
    path.write_text(json.dumps(data), encoding="utf-8")
