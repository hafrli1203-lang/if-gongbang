"""run_probe_scripts' own python-interpreter resolution must never run
`sys.executable` unconditionally for a ".py" target (see
acceptance_probe.py's _PROBE_INTERPRETERS comment: that path lives
under THIS engine's own hidden venv -- a live run against the
퍼포먼스광고 project confirmed `bwrap: execvp .../.venv/bin/python: No
such file or directory` -- never visible inside the probe's own bench
sandbox). It must resolve a system python3 instead (_resolve_system_
python), and refuse -- recording a failure, never running anything --
if that result still resolves under one of this project's own hidden
roots (_probe_python_blocked)."""
from __future__ import annotations

import sys
from pathlib import Path

from engine.application import acceptance_probe
from engine.application.acceptance_probe import run_probe_scripts
from engine.steps.open_job import open_job
from tests.conftest import Workspace
from tests.probe_target import contacting_py_script, serve_target


def _open(workspace: Workspace, job_name: str = "job1") -> str:
    return open_job(workspace.config, workspace.ledger, "alpha", job_name,
                    "full", roles=("builder",))


def test_resolve_system_python_prefers_which_python3(tmp_path: Path, monkeypatch) -> None:
    # A real, non-symlink file -- os.path.realpath on it is a no-op, so
    # the result is exactly this path, proving `which`'s result (not
    # sys.executable) decided it.
    fake_python = tmp_path / "python3"
    fake_python.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(
        acceptance_probe.shutil, "which",
        lambda name: str(fake_python) if name == "python3" else None,
    )
    assert acceptance_probe._resolve_system_python() == str(fake_python)


def test_resolve_system_python_falls_back_to_sys_executable_when_no_which(monkeypatch) -> None:
    monkeypatch.setattr(acceptance_probe.shutil, "which", lambda name: None)
    assert (acceptance_probe._resolve_system_python()
            == acceptance_probe.os.path.realpath(sys.executable))


def test_probe_python_blocked_true_under_hidden_root(tmp_path: Path) -> None:
    hidden_root = tmp_path / "hidden-app-root"
    python_path = hidden_root / ".venv" / "bin" / "python"
    assert acceptance_probe._probe_python_blocked(str(python_path), (hidden_root,)) is True


def test_probe_python_blocked_true_when_equal_to_hidden_root(tmp_path: Path) -> None:
    hidden_root = tmp_path / "hidden-app-root"
    assert acceptance_probe._probe_python_blocked(str(hidden_root), (hidden_root,)) is True


def test_probe_python_blocked_false_outside_hidden_roots(tmp_path: Path) -> None:
    hidden_root = tmp_path / "hidden-app-root"
    python_path = tmp_path / "usr" / "bin" / "python3"
    assert acceptance_probe._probe_python_blocked(str(python_path), (hidden_root,)) is False


def test_probe_py_script_calls_resolve_system_python_not_sys_executable(
    workspace: Workspace, monkeypatch,
) -> None:
    """run_probe_scripts must never pass sys.executable as argv[0] for
    a .py target directly -- it must go through _resolve_system_python
    (recorded here via a spy) and use THAT result instead."""
    attempt_id = _open(workspace)
    probe_dir = workspace.alpha / ".gongbang" / "job1" / "probe"
    probe_dir.mkdir(parents=True)
    calls: list[str] = []
    real_resolve = acceptance_probe._resolve_system_python

    def spy() -> str:
        result = real_resolve()
        calls.append(result)
        return result

    monkeypatch.setattr(acceptance_probe, "_resolve_system_python", spy)

    with serve_target() as url:
        (probe_dir / "t1.py").write_text(contacting_py_script(url), encoding="utf-8")
        results = run_probe_scripts(
            workspace.config, workspace.ledger, "alpha", attempt_id, "job1",
            [("stripe", url)],
        )

    assert len(calls) == 1
    assert results[0].ok is True


def test_probe_py_script_fails_closed_when_resolved_python_is_hidden(
    workspace: Workspace, monkeypatch,
) -> None:
    """A resolved python3 that lands under a hidden root (here:
    config.home, always hidden for every project) must never be run at
    all -- recorded as a failure (`python_runtime_hidden`), not
    silently passed through to the sandbox to fail however bwrap's own
    execvp happens to fail."""
    attempt_id = _open(workspace)
    probe_dir = workspace.alpha / ".gongbang" / "job1" / "probe"
    probe_dir.mkdir(parents=True)
    (probe_dir / "t1.py").write_text(
        "print('PROBE OK https://docs.stripe.com/api')\n", encoding="utf-8",
    )

    hidden_python = str(workspace.config.home / "fake-venv" / "bin" / "python")
    monkeypatch.setattr(acceptance_probe, "_resolve_system_python", lambda: hidden_python)

    results = run_probe_scripts(
        workspace.config, workspace.ledger, "alpha", attempt_id, "job1",
        [("stripe", "https://docs.stripe.com/api")],
    )

    assert results[0].ok is False
    assert results[0].returncode == -1
    assert results[0].stdout_tail == "python_runtime_hidden"

    events = [e for _seq, e in workspace.ledger.events("alpha", "probe.executed")]
    assert events[0].payload["stdout_tail"] == "python_runtime_hidden"
