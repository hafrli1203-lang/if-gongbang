#!/usr/bin/env python3
"""check -- fast local test command for THIS repo's own Python test
suite (engine/ + tools/), separate from the related/full bench_scope
device in engine/application/steps/suite_select.py (that one drives a
MANAGED app's Node/Vitest test_cmd from inside a bench step; it has no
concept of this repo's own pytest tree, so it cannot be reused here --
G45/tools/check decision,.

    tools/check            run only the tests related to files this
                            branch has changed (git diff against the
                            merge-base with main, PLUS the current
                            working tree's own uncommitted changes --
                            same "what did THIS attempt touch" question
                            bench_select._changed_source_paths asks of
                            a managed app, asked here of this repo)
    tools/check --all      run the full suite (same as
                            `.venv/bin/python -m pytest`)

Selection rule (deliberately simple -- no import-graph walk, no AST):
a changed tests/**/*.py file is always included directly. For every
OTHER changed engine/**/*.py or tools/**/*.py file, compute its dotted
module path (e.g. engine/application/steps/suite_select.py ->
engine.application.steps.suite_select) and grep tests/ for any line
that imports that dotted path OR its last component as a bare name
("import engine.application.steps.suite_select", "from
engine.application.steps import suite_select", "from
engine.application.steps.suite_select import ..."). A test file is
selected if ANY such line matches. This is a text grep, not a real
import graph: it can under-select when a test reaches a changed module
only through a re-export or a dynamic import, and can over-select on a
name collision. Both failure modes are caught the same way the T9b
related-tests device above documents its own blind spot being caught
-- by `--all` before anything ships, never by trusting this shortcut
alone for a release decision (see docs/ENGINE.md "속도 기본값").

If nothing changed, or no test file matches any changed module, this
command falls back to the full suite and says so -- an empty "related"
selection would otherwise look like "nothing to check" when it really
means "the grep found no owner for this change," which is a different,
less safe thing to report as zero tests needed.

Runs with `pytest -n auto -m 'not live'` either way (xdist parallel
workers; live AI-CLI tests excluded by the project's own pytest
addopts, same as a plain `pytest` invocation already does).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIRS = ("engine", "tools")
TEST_DIR = "tests"


def _venv_python() -> Path:
    """This repo's own .venv interpreter (where pytest/pytest-xdist
    are installed) -- re-exec into it below if the running
    interpreter isn't already that one, so `tools/check` works whether
    invoked via system python3 or `.venv/bin/python tools/check.py`."""
    return REPO_ROOT / ".venv" / "bin" / "python"


def _ensure_venv() -> None:
    """Re-exec into this repo's .venv interpreter when the current one
    can't import pytest. Deliberately checked by import, not by
    comparing `sys.executable` to `_venv_python()`: .venv/bin/python
    here is a symlink straight to the system python3.12 binary, so
    comparing resolved() paths would make a bare system-python3
    invocation look identical to the venv one even though only the
    venv invocation path carries the sibling pyvenv.cfg that makes
    pytest importable (confirmed directly: `.venv/bin/python -c
    "import pytest"` succeeds, `python3 -c "import pytest"` fails,
   ."""
    try:
        import pytest  # noqa: F401
        return
    except ImportError:
        pass
    venv_python = _venv_python()
    if not venv_python.exists():
        return
    os.execv(str(venv_python), [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]])


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    return result.stdout


def _changed_paths() -> list[str]:
    """Union of (a) this branch's committed diff against main's
    merge-base and (b) the working tree's own uncommitted changes
    (staged, unstaged, and untracked) -- the same "everything this
    attempt touched so far" scope bench_select._changed_source_paths
    asks of a managed app, asked here of this repo's own branch."""
    committed = _git("diff", "--name-only", "main...HEAD")
    working = _git("status", "--porcelain")
    paths: set[str] = set()
    for line in committed.splitlines():
        line = line.strip()
        if line:
            paths.add(line)
    for line in working.splitlines():
        # porcelain format: "XY path" or "XY old -> new" for renames.
        rest = line[3:].strip() if len(line) > 3 else ""
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        if rest:
            paths.add(rest)
    return sorted(paths)


def _module_name(path: Path) -> str | None:
    """Dotted module path for a changed engine/** or tools/** .py
    file, or None if it isn't one (e.g. not a .py file, or outside
    SOURCE_DIRS -- those are left for the caller to decide, not
    silently matched)."""
    if path.suffix != ".py":
        return None
    parts = path.parts
    if not parts or parts[0] not in SOURCE_DIRS:
        return None
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts = parts[:-1] + (path.stem,)
    if not parts:
        return None
    return ".".join(parts)


def _import_pattern(module: str) -> re.Pattern[str]:
    """Regex matching a line that imports `module` by its full dotted
    path, or by its last component via `from <parent> import
    <last>`."""
    last = module.rsplit(".", 1)[-1]
    parent = module.rsplit(".", 1)[0] if "." in module else module
    return re.compile(
        r"(^|\s)import\s+" + re.escape(module) + r"(\s|$|\.)"
        r"|from\s+" + re.escape(module) + r"\s+import"
        r"|from\s+" + re.escape(parent) + r"\s+import\s+[^#\n]*\b" + re.escape(last) + r"\b"
    )


def _related_test_files(changed: list[str]) -> tuple[set[str], list[str]]:
    """(test_files, reasons) for `changed`. reasons is a human-
    readable line per changed path explaining what it selected, for
    the --all-or-related explanation printed below."""
    test_files: set[str] = set()
    reasons: list[str] = []
    all_tests = sorted(
        str(p.relative_to(REPO_ROOT)) for p in (REPO_ROOT / TEST_DIR).rglob("*.py")
    )
    test_sources = {
        t: (REPO_ROOT / t).read_text(encoding="utf-8", errors="replace") for t in all_tests
    }

    for changed_path in changed:
        path = Path(changed_path)
        if path.parts and path.parts[0] == TEST_DIR and path.suffix == ".py":
            if (REPO_ROOT / changed_path).exists():
                test_files.add(changed_path)
                reasons.append(f"{changed_path}: is a test file -> included directly")
            continue
        module = _module_name(path)
        if module is None:
            continue
        pattern = _import_pattern(module)
        matched = [t for t, src in test_sources.items() if pattern.search(src)]
        if matched:
            reasons.append(f"{changed_path} (module {module}): matched {len(matched)} test file(s)")
            test_files.update(matched)
        else:
            reasons.append(f"{changed_path} (module {module}): no test file imports it")
    return test_files, reasons


def main(argv: list[str] | None = None) -> int:
    _ensure_venv()
    argv = sys.argv[1:] if argv is None else argv
    run_all = "--all" in argv

    base_cmd = [sys.executable, "-m", "pytest", "-n", "auto", "-m", "not live"]
    started = time.monotonic()

    if run_all:
        print("tools/check --all: running the full suite.", flush=True)
        cmd = base_cmd
    else:
        changed = _changed_paths()
        if not changed:
            print(
                "tools/check: no changed files against main -- falling back to the full suite.",
                flush=True,
            )
            cmd = base_cmd
        else:
            test_files, reasons = _related_test_files(changed)
            for reason in reasons:
                print(f"  {reason}", flush=True)
            if not test_files:
                print(
                    "tools/check: changed files found but none map to a test file "
                    "-- falling back to the full suite (no safe narrower selection).",
                    flush=True,
                )
                cmd = base_cmd
            else:
                print(f"tools/check: running {len(test_files)} related test file(s).", flush=True)
                cmd = base_cmd + sorted(test_files)

    result = subprocess.run(cmd, cwd=REPO_ROOT)
    elapsed = time.monotonic() - started
    print(f"tools/check: {elapsed:.1f}s, exit code {result.returncode}.", flush=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
