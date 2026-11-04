#!/usr/bin/env python3
# Generated from private source at 495481ceddef11d8ccd4438d8948d5cc71c484d2; do not edit by hand (see harness/export_v3_public.py).
"""Test-run trust checks for deploy.sh / review-gate.sh / wt-land.sh (gaps 42/46/247/115/117), a thin wrapper over the
harness modules at a configurable path (HARNESS_DIR). The harness owns the rules; this file only
picks the runner and the files.

usage:
  suite-check.py runner <dir>                          vitest | pytest | jest | unknown — read from the app itself
                                                        (package.json dependencies, pyproject.toml / pytest.ini /
                                                        setup.cfg / requirements*.txt naming pytest), never assumed
  suite-check.py zero <runner> <output file> [rc]      exit 1 when the output shows 0 tests ran / all skipped
  suite-check.py count <runner> <output file> <history.json> <key>
                                                        prints "<count> (이전 <n>)"; exit 1 when it fell 30% or more
                                                        from the last recorded count of <key>. Records nothing.
  suite-check.py record <history.json> <key> <count>   records <count> as the baseline (deploy.sh, after a good
                                                        deploy only — a failed deploy never moves the baseline)
Jest is read here (the harness reads vitest/pytest): "No tests found" = 0 ran; "Tests: … N passed" = the count.
  suite-check.py facts <file>                          exit 1 when a facts file still says 확인 필요
  suite-check.py probe <file> [max_age_hours]          exit 1 when a probe log lacks PROBE OK + evidence + a fresh date
Exit 3 = the harness is not installed (callers treat that as "check not available", never as a pass in disguise:
they log it).
"""
import json
import os
import re
import sys
from pathlib import Path

HARNESS = os.environ.get("HARNESS_DIR", "")


def harness(name):
    if not Path(HARNESS, f"{name}.py").is_file():
        print(f"harness 없음: {HARNESS}/{name}.py", file=sys.stderr)
        sys.exit(3)
    sys.path.insert(0, HARNESS)
    return __import__(name)


def runner(d: Path) -> str:
    pkg = d / "package.json"
    if pkg.is_file():
        try:
            p = json.loads(pkg.read_text(encoding="utf-8"))
        except ValueError:
            p = {}
        # the test command itself decides first (an app may install vitest and jest but run only one of them)
        test_cmd = str((p.get("scripts") or {}).get("test") or "")
        for name in ("vitest", "jest", "pytest"):
            if re.search(rf"\b{name}\b", test_cmd):
                return name
        deps = {**(p.get("dependencies") or {}), **(p.get("devDependencies") or {})}
        if "vitest" in deps:
            return "vitest"
        if "jest" in deps:
            return "jest"
    for name in ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini"):
        f = d / name
        if f.is_file() and re.search(r"\bpytest\b", f.read_text(encoding="utf-8", errors="ignore")):
            return "pytest"
    for f in d.glob("requirements*.txt"):
        if re.search(r"^\s*pytest\b", f.read_text(encoding="utf-8", errors="ignore"), re.M):
            return "pytest"
    return "unknown"


JEST_NO_TESTS = re.compile(r"No tests found", re.I)
JEST_COUNT = re.compile(r"^Tests:\s+(?:\d+\s+failed,\s+)?(?:\d+\s+skipped,\s+)?(?:\d+\s+todo,\s+)?(\d+)\s+passed", re.M)
JEST_TOTAL = re.compile(r"^Tests:.*?(\d+)\s+total", re.M)


def jest_count(text: str):
    m = JEST_COUNT.search(text)
    return int(m.group(1)) if m else None


def jest_zero(text: str) -> bool:
    if JEST_NO_TESTS.search(text):
        return True
    total = JEST_TOTAL.search(text)
    return bool(total) and jest_count(text) in (None, 0)


# Unstable-test detection (P1-2, 반장 2026-11-02): on a first test failure, deploy.sh reruns ONLY the
# file(s) that failed, two more times. If those retries disagree with each other (not all three the same
# result), the deploy still blocks, but the receipt/#경보 say "불안정 시험" instead of an ordinary failure,
# so a person's first question is "why does this file flip" rather than "what broke". These regexes read
# vitest's and pytest's own failure lines - never the jest format, which has no analogous retry wired in
# deploy.sh (jest apps fall back to the "no files found -> ordinary failure" path).
_VITEST_FAIL_LINE = re.compile(r"^\s*(?:❯|FAIL)\s+(\S+\.(?:test|spec)\.[cm]?[jt]sx?)\b", re.M)
_VITEST_SUMMARY_LINE = re.compile(r"^\s*(\S+\.(?:test|spec)\.[cm]?[jt]sx?)\s*\(\d+\s+tests?[^)]*\bfailed\b", re.M)
_PYTEST_FAILED_LINE = re.compile(r"^FAILED\s+(\S+?)(?:::|\s|$)", re.M)


def failed_files(runner: str, text: str) -> list:
    """Sorted, de-duplicated file paths this run's own failure report names - best-effort: an unrecognized
    format yields an empty list (the caller then treats the failure as ordinary, never invents a retry
    target it cannot name)."""
    out = set()
    if runner in ("vitest", "jest"):
        out |= set(_VITEST_FAIL_LINE.findall(text))
        out |= set(_VITEST_SUMMARY_LINE.findall(text))
    elif runner == "pytest":
        out |= set(_PYTEST_FAILED_LINE.findall(text))
    return sorted(out)


def main() -> None:
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "runner":
        print(runner(Path(args[0])))
    elif cmd == "zero":
        st = harness("suite_trust")
        text = Path(args[1]).read_text(encoding="utf-8", errors="replace")
        rc = int(args[2]) if len(args) > 2 else 0
        # any runner's own "nothing ran" signal counts, whatever runner was detected: a wrong guess must never let a
        # zero-test run through (Codex 11-02)
        zero = (jest_zero(text) or st.looks_like_zero_tests_ran(text, returncode=rc, runner="vitest")
                or st.looks_like_zero_tests_ran(text, returncode=rc, runner="pytest"))
        sys.exit(1 if zero else 0)
    elif cmd == "count":
        st = harness("suite_trust")
        text = Path(args[1]).read_text(encoding="utf-8", errors="replace")
        if args[0] == "jest":
            n = jest_count(text)
        else:
            n = st.extract_test_count(text, runner="pytest" if args[0] == "pytest" else "vitest")
        previous = st.CountHistory.load(Path(args[2])).counts.get(args[3])
        dropped = n is not None and st.count_dropped(previous or 0, n)
        print(f"{'?' if n is None else n} (이전 {'-' if previous is None else previous})")
        sys.exit(1 if dropped else 0)
    elif cmd == "record":
        st = harness("suite_trust")
        hist_path = Path(args[0])
        hist = st.CountHistory.load(hist_path)
        hist.counts[args[1]] = int(args[2])
        hist_path.parent.mkdir(parents=True, exist_ok=True)
        hist.save(hist_path)
    elif cmd == "facts":
        text = Path(args[0]).read_text(encoding="utf-8", errors="replace")
        # line numbers only: a facts line can quote a URL with a credential (Codex 11-02)
        hits = [i for i, ln in enumerate(text.splitlines(), 1) if re.search(r"확인\s*필요", ln)]
        if hits:
            print(f"'확인 필요' {len(hits)}줄 — 줄 {', '.join(map(str, hits[:10]))}")
        sys.exit(1 if hits else 0)
    elif cmd == "probe":
        pt = harness("probe_trust")
        age = float(args[1]) if len(args) > 1 else 24.0
        r = pt.check_probe_log_file(Path(args[0]), max_age_hours=age)
        print(", ".join(r.reasons))
        sys.exit(0 if r.ok else 1)
    elif cmd == "failed-files":   # <runner> <output file> - one failing test file path per line, or nothing (exit 1)
        text = Path(args[1]).read_text(encoding="utf-8", errors="replace")
        files = failed_files(args[0], text)
        for f in files:
            print(f)
        sys.exit(0 if files else 1)
    else:
        sys.exit(f"모르는 명령: {cmd}")


if __name__ == "__main__":
    main()
