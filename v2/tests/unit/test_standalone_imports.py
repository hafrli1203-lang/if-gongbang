"""Every module under engine/ and tools/gb2lib/ must import cleanly on
its own, in a fresh subprocess, with no other module of ours already
loaded first. A module-level circular import (A imports B which, through
some chain, imports back a name from A before A has finished executing)
only shows up this way -- the normal pytest run already has every module
loaded in *some* working order by the time any one test runs, so it never
sees the ImportError a bare `import engine.foo` would hit first.

engine/fakes/hooks/*.py are excluded: they are stdin-driven executable
scripts (hook_approve.py etc.), not importable library modules -- run_module
on them would mean a lone `import engine.fakes.hooks.hook_approve` consumes
a real hook invocation from the live board, not a smoke import.
"""
from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _module_name(path: Path) -> str:
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    return ".".join(rel.parts)


def _discover() -> list[str]:
    modules: list[str] = []
    for base in ("engine", "tools/gb2lib"):
        for path in sorted((REPO_ROOT / base).rglob("*.py")):
            if path.name == "__init__.py":
                continue
            if "fakes/hooks" in path.as_posix() or "fakes\\hooks" in path.as_posix():
                continue
            modules.append(_module_name(path))
    return modules


MODULES = _discover()


def _import_alone(module: str) -> tuple[str, int, str]:
    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return module, proc.returncode, proc.stderr


def test_every_module_discovered() -> None:
    assert len(MODULES) > 100
    assert "engine.full_orbit_runner" in MODULES
    assert "engine.application.pipeline_stages" in MODULES


def test_every_module_imports_standalone() -> None:
    failures: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for module, rc, stderr in pool.map(_import_alone, MODULES):
            if rc != 0:
                failures[module] = stderr.strip().splitlines()[-1] if stderr.strip() else "(no stderr)"
    assert not failures, "modules that fail a standalone `import`:\n" + "\n".join(
        f"  {name}: {reason}" for name, reason in sorted(failures.items())
    )
