# Generated from the private ops workspace at 24c1bb68758c8a552514d06d8280463876278f76; do not edit by hand (see harness/export_ops_public.py).
from __future__ import annotations

from suite_trust import (
    Case,
    CountHistory,
    count_dropped,
    extract_test_count,
    looks_like_zero_tests_ran,
    looks_like_zero_tests_ran_pytest,
    looks_like_zero_tests_ran_vitest,
    run_bench,
    REASON_ALWAYS_PASSES,
    REASON_CASE_FAILED,
    REASON_EMPTY,
    REASON_NO_NEGATIVE_CONTROL,
    REASON_OK,
    REASON_WRONG_SIGNATURE,
)


# -- vitest zero-tests signal, verified against real installed vitest output
# (/apps/exampleapp, 2026-11-02): "Tests  2579 skipped (2579)" / exit 0 --

def test_vitest_no_test_files_found() -> None:
    assert looks_like_zero_tests_ran_vitest("No test files found, exiting with code 1") is True


def test_vitest_zero_tests_summary() -> None:
    assert looks_like_zero_tests_ran_vitest(" Tests  0 passed (0)\n") is True


def test_vitest_all_skipped_real_output() -> None:
    # exact tail captured from `npx vitest run --testNamePattern zzzznomatch123xyz`
    real = " Test Files  160 skipped (160)\n      Tests  2579 skipped (2579)\n   Start at  00:15:57\n"
    assert looks_like_zero_tests_ran_vitest(real) is True


def test_vitest_mixed_skip_is_not_zero() -> None:
    assert looks_like_zero_tests_ran_vitest(" Tests  3 passed | 2 skipped (5)\n") is False


def test_vitest_clean_pass_is_not_zero() -> None:
    assert looks_like_zero_tests_ran_vitest(" Tests  42 passed (42)\n") is False


# -- pytest zero-tests signal, verified against real pytest (if-gongbang-v2
# venv, 2026-11-02): `pytest -k "zzzznomatch123" ...` exits 5 --

def test_pytest_exit_5_is_zero_tests() -> None:
    assert looks_like_zero_tests_ran_pytest("", returncode=5) is True


def test_pytest_no_tests_ran_text() -> None:
    assert looks_like_zero_tests_ran_pytest("no tests ran in 0.01s", returncode=0) is True


def test_pytest_collected_zero_text() -> None:
    assert looks_like_zero_tests_ran_pytest("collected 0 items", returncode=0) is True


def test_pytest_normal_pass_is_not_zero() -> None:
    assert looks_like_zero_tests_ran_pytest("27 passed in 0.5s", returncode=0) is False


def test_pytest_all_skipped_real_output() -> None:
    # Codex review 2026-11-02 (P1): exact shape from a real all-skip-marked
    # pytest run on this host -- exit 0, no "no tests ran"/"collected 0
    # items" wording at all.
    real = "============================= 22 skipped in 0.06s =============================="
    assert looks_like_zero_tests_ran_pytest(real, returncode=0) is True


def test_pytest_mixed_skip_with_passes_is_not_zero() -> None:
    assert looks_like_zero_tests_ran_pytest("3 passed, 2 skipped in 0.1s", returncode=0) is False


def test_pytest_earlier_bare_skip_line_does_not_override_real_final_summary() -> None:
    # Codex review 2026-11-02, second pass: an earlier line mentioning a
    # bare skip count must not out-rank the ACTUAL final summary that
    # shows real passes.
    text = "collected 3 items / 1 skipped\n1 passed, 1 skipped in 0.1s\n"
    assert looks_like_zero_tests_ran_pytest(text, returncode=0) is False


def test_pytest_real_verbose_output_mixed() -> None:
    real = (
        "collecting ... collected 2 items\n"
        "test_skip_mix.py::test_pass PASSED                          [ 50%]\n"
        "test_skip_mix.py::test_skip SKIPPED (x)                     [100%]\n"
        "========================= 1 passed, 1 skipped in 4.36s =========================\n"
    )
    assert looks_like_zero_tests_ran_pytest(real, returncode=0) is False


def test_pytest_real_verbose_output_all_skipped() -> None:
    real = (
        "collecting ... collected 2 items\n"
        "============================= 2 skipped in 4.37s ==============================\n"
    )
    assert looks_like_zero_tests_ran_pytest(real, returncode=0) is True


def test_dispatch_by_runner_name() -> None:
    assert looks_like_zero_tests_ran("No test files found", runner="vitest") is True
    assert looks_like_zero_tests_ran("collected 0 items", returncode=0, runner="pytest") is True
    assert looks_like_zero_tests_ran("27 passed", runner="pytest") is False


# -- test-count sharp-drop --

def test_count_dropped_threshold() -> None:
    assert count_dropped(100, 69) is True   # -31%
    assert count_dropped(100, 71) is False  # -29%
    assert count_dropped(0, 5) is False     # no prior baseline
    assert count_dropped(100, 150) is False  # grew


def test_count_dropped_boundary_is_inclusive() -> None:
    assert count_dropped(100, 70, threshold=0.30) is True  # exactly -30%


def test_extract_test_count_vitest() -> None:
    assert extract_test_count(" Tests  42 passed (42)\n", runner="vitest") == 42
    assert extract_test_count("nothing recognizable", runner="vitest") is None


def test_extract_test_count_pytest() -> None:
    assert extract_test_count("27 passed in 0.5s", runner="pytest") == 27
    assert extract_test_count("collected 10 items", runner="pytest") == 10


def test_count_history_detects_drop_and_records(tmp_path) -> None:
    path = tmp_path / "counts.json"
    hist = CountHistory()
    assert hist.check_and_record("npm test", 100) is False
    hist.save(path)

    hist2 = CountHistory.load(path)
    assert hist2.check_and_record("npm test", 50) is True  # -50%
    hist2.save(path)

    hist3 = CountHistory.load(path)
    assert hist3.counts["npm test"] == 50


def test_count_history_none_count_does_not_overwrite(tmp_path) -> None:
    path = tmp_path / "counts.json"
    hist = CountHistory()
    hist.check_and_record("npm test", 100)
    hist.save(path)

    hist2 = CountHistory.load(path)
    assert hist2.check_and_record("npm test", None) is False
    assert hist2.counts["npm test"] == 100


# -- bench negative-control (run_bench) --

def test_run_bench_empty_fails() -> None:
    assert run_bench([]).reason == REASON_EMPTY


def test_run_bench_no_negative_control_fails() -> None:
    cases = [Case("a", run=lambda: True)]
    assert run_bench(cases).reason == REASON_NO_NEGATIVE_CONTROL


def test_run_bench_always_passes_grader_rejected() -> None:
    cases = [
        Case("real", run=lambda: True),
        Case("neg", run=lambda: True, expect_fail=True, expected_signature="assertion"),
    ]
    assert run_bench(cases).reason == REASON_ALWAYS_PASSES


def test_run_bench_normal_case_failed() -> None:
    cases = [
        Case("real", run=lambda: False),
        Case("neg", run=lambda: False, expect_fail=True, expected_signature="assertion"),
    ]
    assert run_bench(cases).reason == REASON_CASE_FAILED


def test_run_bench_wrong_signature_rejected() -> None:
    def raise_type_error() -> bool:
        raise TypeError("boom")

    cases = [
        Case("real", run=lambda: True),
        Case("neg", run=raise_type_error, expect_fail=True, expected_signature="assertion"),
    ]
    assert run_bench(cases).reason == REASON_WRONG_SIGNATURE


def test_run_bench_ok() -> None:
    cases = [
        Case("real", run=lambda: True),
        Case("neg", run=lambda: False, expect_fail=True, expected_signature="assertion"),
    ]
    result = run_bench(cases)
    assert result.passed is True
    assert result.reason == REASON_OK
