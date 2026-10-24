"""Unit tests for the crash-safe exit-code recovery contract (s10 item
6): engine.procs.spawn/exit_file_for writing a worker's real exit code
to a file, and engine.workers.base.wait_for_exit/wait_while_progressing
reading it back when this process is no longer (or never was) the
worker's actual parent."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from engine import procs
from tests.conftest import Workspace


def test_spawn_writes_exit_file_when_stdout_path_given(workspace: Workspace,
                                                        tmp_path: Path) -> None:
    stdout_path = tmp_path / "out.stdout"
    proc = procs.spawn(
        workspace.ledger, "alpha", "J-alpha-TEST#9", "build",
        ["sh", "-c", "exit 7"], cwd=workspace.alpha, stdout_path=stdout_path,
    )
    proc.wait(timeout=5)
    exit_file = procs.exit_file_for(stdout_path)
    assert exit_file.read_text(encoding="utf-8").strip() == "7"


def test_spawn_without_stdout_path_leaves_argv_unwrapped(workspace: Workspace) -> None:
    """No stdout_path -> no exit-file wrapping at all: argv[0] is still
    the real command, not `sh`."""
    proc = procs.spawn(
        workspace.ledger, "alpha", "J-alpha-TEST#10", "build",
        ["sleep", "0"], cwd=workspace.alpha,
    )
    proc.wait(timeout=5)
    assert proc.args[0] == "sleep"


def test_wait_for_exit_reads_exit_file_when_child_already_gone(tmp_path: Path) -> None:
    """Simulates a crash-and-resume (or an already-reaped same-process
    pid): os.waitpid raises ChildProcessError, the pid itself is dead,
    and the real exit code must come from the exit_file, not a bare
    assumption of success."""
    from engine.workers.base import wait_for_exit

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    dead_pid = proc.pid  # already reaped
    exit_file = tmp_path / "out.stdout.exit"
    exit_file.write_text("3\n", encoding="utf-8")
    assert wait_for_exit(dead_pid, 1.0, exit_file) == 3


def test_wait_for_exit_unknown_when_no_exit_file(tmp_path: Path) -> None:
    """Same dead-pid situation but no exit_file at all (or none passed) --
    returns the distinct _UNKNOWN_EXIT sentinel (-1), never 0, so a
    caller that already treats -1 as abnormal/failed sees it that way
    instead of a silently assumed success."""
    from engine.workers.base import wait_for_exit

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    dead_pid = proc.pid
    assert wait_for_exit(dead_pid, 1.0, tmp_path / "missing.exit") == -1
    assert wait_for_exit(dead_pid, 1.0, None) == -1


def test_wait_while_progressing_derives_exit_file_from_first_progress_path(
    tmp_path: Path,
) -> None:
    """wait_while_progressing must derive the same exit_file path
    engine.procs.spawn writes to from progress_paths[0] -- no separate
    parameter -- so a real worker spawned with stdout_path=progress_paths[0]
    recovers its exit code even when this process never reaped it itself."""
    from engine.procs import exit_file_for
    from engine.workers.base import wait_while_progressing

    stdout_path = tmp_path / "out.stdout"
    exit_file = exit_file_for(stdout_path)
    exit_file.write_text("0", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    dead_pid = proc.pid
    code = wait_while_progressing(dead_pid, 5.0, (stdout_path,))
    assert code == 0
