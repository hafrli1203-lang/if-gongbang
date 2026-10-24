"""Unit tests for engine.procs: process groups, spawn/close_station."""
from __future__ import annotations

import time
from pathlib import Path

from engine import procs
from tests.conftest import Workspace


def test_spawn_and_close_station_kills_child(workspace: Workspace) -> None:
    proc = procs.spawn(
        workspace.ledger, "alpha", "J-alpha-TEST#1", "build",
        ["sleep", "30"], cwd=workspace.alpha,
    )
    assert procs.is_alive(proc.pid) is True

    killed = procs.close_station(workspace.ledger, "alpha", "J-alpha-TEST#1", "build")
    assert proc.pid in killed
    proc.wait(timeout=5)
    assert procs.is_alive(proc.pid) is False

    events = workspace.ledger.events("alpha", "proc.ended")
    assert any(e.payload["pid"] == proc.pid for _, e in events)


def test_close_station_leaves_other_station_alive(workspace: Workspace) -> None:
    proc_a = procs.spawn(
        workspace.ledger, "alpha", "J-alpha-TEST#2", "build",
        ["sleep", "30"], cwd=workspace.alpha,
    )
    proc_b = procs.spawn(
        workspace.ledger, "alpha", "J-alpha-TEST#2", "review",
        ["sleep", "30"], cwd=workspace.alpha,
    )
    try:
        killed = procs.close_station(workspace.ledger, "alpha", "J-alpha-TEST#2", "build")
        assert proc_a.pid in killed
        proc_a.wait(timeout=5)
        assert procs.is_alive(proc_b.pid) is True
    finally:
        procs.close_station(workspace.ledger, "alpha", "J-alpha-TEST#2", "review")
        proc_b.wait(timeout=5)


def test_close_station_kills_survivor_left_behind_by_dead_leader(
    workspace: Workspace, tmp_path: Path,
) -> None:
    """E3: only the recorded leader pid used to be checked for liveness. A
    descendant that ignores SIGTERM and outlives its leader (the leader
    exits on its own, is reaped, is no longer 'live') used to be missed
    entirely: the old liveness loop saw zero remaining recorded pids and
    never even tried SIGKILL. Liveness must be checked at the process
    GROUP, which the descendant keeps alive after the leader is gone."""
    pidfile = tmp_path / "grandchild.pid"
    leader_script = tmp_path / "leader.py"
    leader_script.write_text(
        "import subprocess, sys\n"
        "p = subprocess.Popen(['python3', '-c',\n"
        "    'import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])\n"
        "with open(sys.argv[1], 'w') as f:\n"
        "    f.write(str(p.pid))\n",
        encoding="utf-8",
    )

    leader = procs.spawn(
        workspace.ledger, "alpha", "J-alpha-TEST#3", "build",
        ["python3", str(leader_script), str(pidfile)], cwd=workspace.alpha,
    )
    leader.wait(timeout=5)  # leader process exits and is reaped right away

    deadline = time.time() + 5
    while not pidfile.exists() and time.time() < deadline:
        time.sleep(0.05)
    grandchild_pid = int(pidfile.read_text(encoding="utf-8").strip())
    assert procs.is_alive(grandchild_pid) is True

    killed = procs.close_station(
        workspace.ledger, "alpha", "J-alpha-TEST#3", "build", grace_sec=0.5,
    )
    assert leader.pid in killed

    deadline = time.time() + 5
    while procs.is_alive(grandchild_pid) and time.time() < deadline:
        time.sleep(0.05)
    assert procs.is_alive(grandchild_pid) is False

    events = workspace.ledger.events("alpha", "proc.ended")
    assert any(e.payload["pid"] == leader.pid for _, e in events)
