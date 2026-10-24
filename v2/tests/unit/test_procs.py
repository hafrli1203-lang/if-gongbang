"""Unit tests for engine.procs: process groups, spawn/close_station."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from engine import procs, sandbox
from tests.conftest import Workspace

_needs_bwrap = pytest.mark.skipif(
    not sandbox.available(), reason="bwrap not available on this host",
)


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


def _descendant_pids(root_pid: int) -> set[int]:
    """Every live pid whose ppid chain leads back to `root_pid`, found by
    scanning /proc/*/stat's own ppid field (never pgrep -f, whose
    command-line substring matching is easy to get spurious matches
    from -- e.g. a test's OWN source text happening to contain the
    argv words being searched for)."""
    children_of: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            stat = Path("/proc", entry, "stat").read_text(encoding="utf-8")
        except OSError:
            continue
        # comm is "(...)", which may itself contain ')' -- ppid is the
        # first field after comm's own closing ')'.
        after_comm = stat.rsplit(")", 1)[-1].split()
        ppid = int(after_comm[1])
        children_of.setdefault(ppid, []).append(pid)

    found: set[int] = set()
    frontier = [root_pid]
    while frontier:
        pid = frontier.pop()
        for child in children_of.get(pid, ()):
            if child not in found:
                found.add(child)
                frontier.append(child)
    return found


@_needs_bwrap
def test_sandboxed_spawn_dies_with_parent_through_the_sh_exit_wrapper(
    workspace: Workspace, tmp_path: Path,
) -> None:
    """another change item 2: engine.procs.spawn's unsandboxed `sh` exit-code
    wrapper (see engine.procs.spawn's own docstring) sits BETWEEN this
    process and the bwrap invocation engine.sandbox.wrap builds --
    bwrap's own --die-with-parent ties the sandboxed tree's life to
    THAT immediate parent (the `sh` process), not to this test process
    directly. Killing only the direct Popen child (`sh`) -- never
    procs.close_station's own explicit process-group kill, which this
    test deliberately bypasses to isolate --die-with-parent itself --
    must still bring down the whole bwrap tree, including the
    sandboxed `sleep` at the bottom of it, with no survivor left
    running under init."""
    argv = sandbox.wrap(
        ["sleep", "50"], cwd=workspace.alpha, mode="bwrap",
        writable=(workspace.alpha,),
        child_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    exit_file = tmp_path / "exit" / "r.exit"
    proc = procs.spawn(
        workspace.ledger, "alpha", "J-alpha-TEST#4", "build", argv,
        cwd=workspace.alpha, env=sandbox.exec_env("bwrap", {}),
        exit_file=exit_file, request_id="r1",
    )
    time.sleep(1.0)  # let bwrap finish setting up its own process tree
    descendants = _descendant_pids(proc.pid)
    assert descendants, "expected at least the bwrap launcher as a descendant"

    os.kill(proc.pid, signal.SIGKILL)  # the direct child only -- not the group

    deadline = time.time() + 5
    while time.time() < deadline and any(
        Path("/proc", str(pid)).exists() for pid in descendants
    ):
        time.sleep(0.05)

    survivors = [pid for pid in descendants if Path("/proc", str(pid)).exists()]
    assert survivors == []


@_needs_bwrap
def test_engine_death_leaves_no_survivor_under_bwrap(
    workspace: Workspace, tmp_path: Path,
) -> None:
    """Engine death policy (see procs.spawn's own docstring,
    "Engine death policy" paragraph, and _die_with_engine's docstring):
    the test above kills the DIRECT child spawn() itself returns (the
    `sh` exit-code wrapper), simulating an explicit stop, not an
    engine crash -- the wrapper's own real OS parent in that test IS
    the pytest process, so nothing there actually proves what happens
    if THAT grandparent dies. This test spawns a SEPARATE, real
    Python process to stand in for "the engine" (it imports
    engine.procs and calls spawn() ITSELF, so the worker's real OS
    parent is that stand-in process, never pytest), then SIGKILLs the
    stand-in directly -- a true simulated engine crash -- and asserts
    the whole descendant tree (the `sh` wrapper, the bwrap launcher,
    and the sandboxed `sleep` at the bottom of it) is gone shortly
    after, with no explicit close_station call anywhere in this test."""
    standin_script = tmp_path / "engine_standin.py"
    argv_json = tmp_path / "argv.json"
    pidfile = tmp_path / "standin.pid"
    repo_root = str(Path(__file__).resolve().parents[2])

    sandboxed_argv = sandbox.wrap(
        ["sleep", "50"], cwd=workspace.alpha, mode="bwrap",
        writable=(workspace.alpha,),
        child_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    argv_json.write_text(json.dumps(sandboxed_argv), encoding="utf-8")
    standin_script.write_text(
        "import json, sys, time\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {repo_root!r})\n"
        "from engine import procs\n"
        "from engine.ledger import Ledger\n"
        "real_argv = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))\n"
        "ledger = Ledger.create(Path(sys.argv[1]))\n"
        "ledger.register_project('alpha')\n"
        "proc = procs.spawn(ledger, 'alpha', 'a1', 'build', real_argv, cwd=Path('/tmp'))\n"
        "Path(sys.argv[3]).write_text(str(proc.pid), encoding='utf-8')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    standin_ledger = tmp_path / "standin_ledger.sqlite"
    standin = subprocess.Popen(
        [sys.executable, str(standin_script), str(standin_ledger), str(argv_json),
         str(pidfile)],
        start_new_session=True,
    )

    deadline = time.time() + 5
    while not pidfile.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert pidfile.exists(), "engine stand-in never reported its worker's pid"
    worker_pid = int(pidfile.read_text(encoding="utf-8").strip())

    deadline = time.time() + 2
    descendants: set[int] = set()
    while time.time() < deadline:
        descendants = _descendant_pids(standin.pid)
        if len(descendants) >= 2:  # the sh wrapper AND at least the bwrap launcher
            break
        time.sleep(0.05)
    assert worker_pid in descendants
    all_watched = {standin.pid, *descendants}

    os.kill(standin.pid, signal.SIGKILL)  # simulated engine crash
    standin.wait(timeout=5)

    deadline = time.time() + 5
    while time.time() < deadline and any(
        Path("/proc", str(pid)).exists() for pid in all_watched
    ):
        time.sleep(0.05)

    survivors = [pid for pid in all_watched if Path("/proc", str(pid)).exists()]
    assert survivors == []
