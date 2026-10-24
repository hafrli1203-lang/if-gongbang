"""A tiny, industry-free "greeter" CLI app used as the sample project for
the owner-task acceptance tests (wording fix / reproducible bug / a
dependency + external call). Three variants (see plan Task 4 / Task 5
§8):

  1 - wording fix: greet() returns "Hello, {name}".
  2 - reproducible bug: initials() is off-by-one (only the first
      initial), plus a negative control that always fails.
  3 - dependency + external integration: fetch_status() needs
      vendored_pkg (not installed) and a status URL from the env.
"""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

from tests.conftest import GIT_ENV

_INIT_PY_V1 = '''\
def greet(name: str) -> str:
    return f"Hello, {name}"
'''

_INIT_PY_V2 = '''\
def greet(name: str) -> str:
    return f"Hello, {name}"


def initials(full_name: str) -> str:
    # Off-by-one: only the first name's initial, not every word's.
    words = full_name.split()
    return "".join(word[0] for word in words[:1]).upper()
'''

_CONTROL_ENV_VAR = "GREETER_CONTROL"

# test_always_fails_control is a negative control: bench.run_bench() needs
# it to prove the trust checks themselves actually fire. But a check that
# ALWAYS fails would also always fail the real test_cmd run (run_tests,
# the full suite that must be green for a passing bench -- plan §5
# "passed = tests_ok and trust.passed"), which the control can never
# satisfy. Gating it behind an env var that only trust_checks' per-case
# run sets (see _case_env / cases.json "env") keeps the control exercised
# under bench while run_tests' full-suite run skips (not fails) it.

_TEST_GREETER_V1 = '''\
import os

import pytest

from greeter import greet

_CONTROL_ON = os.environ.get("GREETER_CONTROL") == "1"


def test_greet():
    assert greet("World") == "Hello, World"


@pytest.mark.skipif(not _CONTROL_ON, reason="negative control; needs GREETER_CONTROL=1")
def test_always_fails_control():
    assert False
'''

_TEST_GREETER_V2 = '''\
import os

import pytest

from greeter import greet, initials

_CONTROL_ON = os.environ.get("GREETER_CONTROL") == "1"


def test_greet():
    assert greet("World") == "Hello, World"


def test_initials():
    assert initials("Ada Lovelace") == "AL"


@pytest.mark.skipif(not _CONTROL_ON, reason="negative control; needs GREETER_CONTROL=1")
def test_always_fails_control():
    assert False
'''

_TEST_REMOTE = '''\
import os

from greeter.remote import fetch_status


def test_fetch_status():
    url = os.environ["SAMPLE_STATUS_URL"]
    assert fetch_status(url) == {"ok": True}
'''

_BENCH_CASES_V2 = [
    {
        "name": "initials_bug",
        "selector": "tests/test_greeter.py::test_initials",
        "expect_fail": False,
        "expected_signature": "assertion",
    },
    {
        "name": "negative_control",
        "selector": "tests/test_greeter.py::test_always_fails_control",
        "expect_fail": True,
        "expected_signature": "assertion",
        "env": {"GREETER_CONTROL": "1"},
    },
]

_BENCH_CASES_SINGLE_REAL = [
    {
        "name": "greet",
        "selector": "tests/test_greeter.py::test_greet",
        "expect_fail": False,
        "expected_signature": "assertion",
    },
    {
        "name": "negative_control",
        "selector": "tests/test_greeter.py::test_always_fails_control",
        "expect_fail": True,
        "expected_signature": "assertion",
        "env": {"GREETER_CONTROL": "1"},
    },
]


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], check=True,
                         capture_output=True, text=True, env=GIT_ENV)
    return out.stdout.strip()


def _write(repo: Path, rel: str, content: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


@dataclass(frozen=True)
class SampleApp:
    repo: Path
    test_cmd: tuple[str, ...]
    install_cmd: tuple[str, ...]
    manifest: str = "requirements.txt"
    bench_cases: str = "bench/cases.json"
    status_url_env: str = "SAMPLE_STATUS_URL"


def make_sample(path: Path, variant: int, python: Path) -> SampleApp:
    """Build a tiny git repo at `path` for one of the three task
    variants. `python` is the interpreter test_cmd/install_cmd run
    under (the engine's own venv python, so no new installs are
    needed to run pytest itself)."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _write(path, ".gitignore", ".deps/\n__pycache__/\n")
    _write(path, "requirements.txt", "")

    if variant == 1:
        _write(path, "src/greeter/__init__.py", _INIT_PY_V1)
        _write(path, "tests/test_greeter.py", _TEST_GREETER_V1)
        _write(path, "bench/cases.json", json.dumps(_BENCH_CASES_SINGLE_REAL))
    elif variant == 2:
        _write(path, "src/greeter/__init__.py", _INIT_PY_V2)
        _write(path, "tests/test_greeter.py", _TEST_GREETER_V2)
        _write(path, "bench/cases.json", json.dumps(_BENCH_CASES_V2))
    elif variant == 3:
        _write(path, "src/greeter/__init__.py", _INIT_PY_V1)
        _write(path, "tests/test_greeter.py", _TEST_GREETER_V1)
        # remote.py is intentionally NOT written: the worker's task is to
        # create it. test_remote.py already expects it to exist.
        _write(path, "tests/test_remote.py", _TEST_REMOTE)
        _write(path, "bench/cases.json", json.dumps(_BENCH_CASES_SINGLE_REAL))
        vendor_dir = path / "vendor"
        vendor_dir.mkdir(parents=True, exist_ok=True)
        make_wheel(vendor_dir)
    else:
        raise ValueError(f"unknown sample variant: {variant}")

    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")

    # No trailing path argument: bench_run's run_tests uses this as-is
    # (pytest's default rootdir discovery finds tests/ with nothing
    # else to collect) and trust_checks appends exactly one selector
    # per case -- a baked-in "tests" directory here would make that
    # append collect the whole suite PLUS the selector, so an unrelated
    # failing test (or, in variant 3, an unrelated collection error)
    # would contaminate every other case's result.
    test_cmd = ("env", "PYTHONPATH=src:.deps", str(python), "-m", "pytest", "-q")
    install_cmd = ("uv", "pip", "install", "--python", str(python),
                   "--target", ".deps", "--no-index", "--find-links",
                   "vendor", "-r", "requirements.txt")
    return SampleApp(repo=path, test_cmd=test_cmd, install_cmd=install_cmd)


_VENDORED_PKG_INIT = '''\
import json


def parse(text: str) -> dict:
    return json.loads(text)
'''


def _record_line(arcname: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return f"{arcname},sha256={digest.decode('ascii')},{len(data)}"


def make_wheel(vendor_dir: Path) -> Path:
    """Build `vendored_pkg-0.1.0-py3-none-any.whl` in `vendor_dir` with
    `zipfile` -- a pure-Python one-file package (`parse(text) -> dict`)
    that `uv pip install --no-index --find-links vendor` can install
    offline, standing in for a real third-party dependency."""
    vendor_dir.mkdir(parents=True, exist_ok=True)
    wheel_path = vendor_dir / "vendored_pkg-0.1.0-py3-none-any.whl"
    dist_info = "vendored_pkg-0.1.0.dist-info"

    init_bytes = _VENDORED_PKG_INIT.encode("utf-8")
    metadata = (
        "Metadata-Version: 2.1\n"
        "Name: vendored_pkg\n"
        "Version: 0.1.0\n"
        "Summary: offline test fixture\n"
    ).encode("utf-8")
    wheel_meta = (
        "Wheel-Version: 1.0\n"
        "Generator: gongbang-test-fixture\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    ).encode("utf-8")

    files: dict[str, bytes] = {
        "vendored_pkg/__init__.py": init_bytes,
        f"{dist_info}/METADATA": metadata,
        f"{dist_info}/WHEEL": wheel_meta,
    }
    record_lines = [_record_line(name, data) for name, data in files.items()]
    record_lines.append(f"{dist_info}/RECORD,,")
    files[f"{dist_info}/RECORD"] = ("\n".join(record_lines) + "\n").encode("utf-8")

    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return wheel_path
