"""Unit tests for the crash-safe exit-code recovery contract (an earlier round item
6, hardened another change item 1): engine.procs.spawn writing a worker's
real exit code to a file engine.procs.exit_file_for derives from
stdout_path -- now a SIBLING "exit" directory, never the same
worker-writable stations directory stdout_path itself lives in (see
that function's own docstring) -- refusing to follow a symlink or
clobber an existing file there (engine.exit_writer), and engine.
workers.base.wait_for_exit/wait_while_progressing reading it back when
this process is no longer (or never was) the worker's actual parent."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from engine import procs
from tests.conftest import Workspace


def test_spawn_writes_exit_file_when_stdout_path_given(workspace: Workspace,
                                                        tmp_path: Path) -> None:
    stdout_path = tmp_path / "stations" / "r1.stdout"
    proc = procs.spawn(
        workspace.ledger, "alpha", "J-alpha-TEST#9", "build",
        ["sh", "-c", "exit 7"], cwd=workspace.alpha, stdout_path=stdout_path,
    )
    proc.wait(timeout=5)
    exit_file = procs.exit_file_for(stdout_path)
    assert exit_file.read_text(encoding="utf-8").strip() == "7"
    # another change item 1: never in the SAME (worker-writable) directory
    # as stdout_path itself -- a sibling "exit" directory instead.
    assert exit_file.parent != stdout_path.parent
    assert exit_file.parent.name == "exit"


def test_spawn_without_stdout_path_or_exit_file_leaves_argv_unwrapped(
    workspace: Workspace,
) -> None:
    """Neither stdout_path nor exit_file -> no exit-file wrapping at
    all: argv[0] is still the real command, not `sh`."""
    proc = procs.spawn(
        workspace.ledger, "alpha", "J-alpha-TEST#10", "build",
        ["sleep", "0"], cwd=workspace.alpha,
    )
    proc.wait(timeout=5)
    assert proc.args[0] == "sleep"


def test_spawn_explicit_exit_file_overrides_the_stdout_path_derived_one(
    workspace: Workspace, tmp_path: Path,
) -> None:
    stdout_path = tmp_path / "stations" / "r1.stdout"
    explicit = tmp_path / "custom-exit" / "r1.exit"
    proc = procs.spawn(
        workspace.ledger, "alpha", "J-alpha-TEST#11", "build",
        ["sh", "-c", "exit 5"], cwd=workspace.alpha,
        stdout_path=stdout_path, exit_file=explicit,
    )
    proc.wait(timeout=5)
    assert explicit.read_text(encoding="utf-8").strip() == "5"
    assert not procs.exit_file_for(stdout_path).exists()


def test_spawn_removes_a_stale_exit_file_before_running(workspace: Workspace,
                                                        tmp_path: Path) -> None:
    stdout_path = tmp_path / "stations" / "r1.stdout"
    exit_file = procs.exit_file_for(stdout_path)
    exit_file.parent.mkdir(parents=True)
    exit_file.write_text("99", encoding="utf-8")
    proc = procs.spawn(
        workspace.ledger, "alpha", "J-alpha-TEST#12", "build",
        ["sh", "-c", "exit 0"], cwd=workspace.alpha, stdout_path=stdout_path,
    )
    proc.wait(timeout=5)
    assert exit_file.read_text(encoding="utf-8").strip() == "0"


def test_spawn_never_writes_through_a_symlink_planted_mid_run(
    workspace: Workspace, tmp_path: Path,
) -> None:
    """another change item 1: if something with write access to the exit
    file's parent directory plants a symlink there -- pointing at an
    arbitrary target -- strictly AFTER spawn()'s own pre-run unlink
    (simulating a worker's sandboxed code racing to do this while the
    real command is still running), the unsandboxed `sh` wrapper's
    write must never follow it through to that target. engine.
    exit_writer's O_NOFOLLOW makes that create fail outright instead."""
    outside_target = tmp_path / "outside_target.txt"
    stdout_path = tmp_path / "stations" / "r1.stdout"
    exit_file = procs.exit_file_for(stdout_path)

    proc = procs.spawn(
        workspace.ledger, "alpha", "J-alpha-TEST#13", "build",
        # Sleeps briefly so the symlink below is planted strictly AFTER
        # spawn()'s own pre-run unlink and strictly BEFORE the writer
        # tries to record the real code.
        ["sh", "-c", "sleep 0.3; exit 9"], cwd=workspace.alpha, stdout_path=stdout_path,
    )
    exit_file.parent.mkdir(parents=True, exist_ok=True)
    exit_file.symlink_to(outside_target)
    proc.wait(timeout=5)

    assert not outside_target.exists()
    # The write was refused (O_NOFOLLOW), not redirected: the symlink
    # this test planted is exactly what is still there, never replaced
    # by a real exit-code file.
    assert exit_file.is_symlink() is True
    assert not exit_file.exists()  # symlink to a target that was never created


def test_spawn_never_overwrites_a_forged_exit_file_planted_mid_run(
    workspace: Workspace, tmp_path: Path,
) -> None:
    """A regular (non-symlink) file planted at the exit_file path AFTER
    spawn() already did its pre-run unlink (simulating a worker racing
    to forge a "0" ahead of the real command finishing) must not be
    silently accepted OR overwritten by the real exit code -- it stays
    exactly as the forger left it, which a caller reading it back must
    treat as a failure to recover a real exit code (not as a trusted
    "0"), never as this run's own success."""
    stdout_path = tmp_path / "stations" / "r1.stdout"
    exit_file = procs.exit_file_for(stdout_path)

    proc = procs.spawn(
        workspace.ledger, "alpha", "J-alpha-TEST#14", "build",
        # Sleeps briefly so the forged file below is planted strictly
        # AFTER spawn()'s own pre-run unlink and strictly BEFORE the
        # writer tries to record the real code.
        ["sh", "-c", "sleep 0.3; exit 9"], cwd=workspace.alpha, stdout_path=stdout_path,
    )
    exit_file.parent.mkdir(parents=True, exist_ok=True)
    exit_file.write_text("0", encoding="utf-8")
    proc.wait(timeout=5)

    # The real exit code (9) was never written through -- the forged
    # "0" is exactly what is still there, untouched.
    assert exit_file.read_text(encoding="utf-8").strip() == "0"


def test_wait_for_exit_reads_exit_file_when_child_already_gone(tmp_path: Path) -> None:
    """Simulates a crash-and-resume (or an already-reaped same-process
    pid): os.waitpid raises ChildProcessError, the pid itself is dead,
    and the real exit code must come from the exit_file, not a bare
    assumption of success."""
    from engine.workers.base import wait_for_exit

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    dead_pid = proc.pid  # already reaped
    exit_file = tmp_path / "exit" / "out.stdout.exit"
    exit_file.parent.mkdir(parents=True)
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
    engine.procs.spawn writes to from progress_paths[0] (engine.procs.
    exit_file_for) -- no separate parameter needed at an existing call
    site -- so a real worker spawned with stdout_path=progress_paths[0]
    recovers its exit code even when this process never reaped it
    itself. exit_file_for now points at a SIBLING "exit" directory, never the same directory as stdout_path."""
    from engine.procs import exit_file_for
    from engine.workers.base import wait_while_progressing

    stdout_path = tmp_path / "stations" / "out.stdout"
    exit_file = exit_file_for(stdout_path)
    exit_file.parent.mkdir(parents=True)
    exit_file.write_text("0", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    dead_pid = proc.pid
    code = wait_while_progressing(dead_pid, 5.0, (stdout_path,))
    assert code == 0


def test_wait_while_progressing_explicit_exit_file_overrides_derivation(
    tmp_path: Path,
) -> None:
    from engine.workers.base import wait_while_progressing

    stdout_path = tmp_path / "stations" / "out.stdout"
    explicit = tmp_path / "custom-exit" / "r.exit"
    explicit.parent.mkdir(parents=True)
    explicit.write_text("2", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    dead_pid = proc.pid
    code = wait_while_progressing(dead_pid, 5.0, (stdout_path,), explicit)
    assert code == 2
