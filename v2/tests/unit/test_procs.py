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
from engine.ledger import Event
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


def test_spawn_adopts_a_live_process_left_by_a_crash_before_proc_started(
    workspace: Workspace, tmp_path: Path,
) -> None:
    """Fault injection for the crash window inside spawn() itself,
    between a successful Popen and its own proc.started append (see
    spawn()'s own docstring): reproduced here by doing exactly what a
    crashed spawn() call would have done up to that point -- append
    proc.launch_intent, then Popen the real process with stdout
    redirected to `stdout_path` -- and stopping there, with no
    proc.started ever appended (the engine crashing right there, never
    getting to that append, is simulated by simply never making the
    call that would have appended it). A later spawn() call for the
    IDENTICAL identity (attempt_id, station, request_id, stdout_path)
    must ADOPT that still-running process -- the same pid, a
    proc.started event now recorded with adopted=True -- rather than
    starting a second, duplicate one."""
    stdout_path = tmp_path / "stations" / "r1.stdout"
    stdout_path.parent.mkdir(parents=True)
    workspace.ledger.append(Event(
        project="alpha", kind="proc.launch_intent", actor_kind="processor",
        actor_id="processor", attempt_id="J-alpha-TEST#5",
        payload={"station": "build", "request_id": "r1",
                 "stdout_path": str(stdout_path)},
    ))
    with open(stdout_path, "wb") as f:
        real_proc = subprocess.Popen(
            ["sleep", "30"], stdout=f, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        assert procs.is_alive(real_proc.pid) is True

        adopted = procs.spawn(
            workspace.ledger, "alpha", "J-alpha-TEST#5", "build",
            ["sleep", "30"], cwd=workspace.alpha, stdout_path=stdout_path,
            request_id="r1",
        )

        assert adopted.pid == real_proc.pid

        started = [e for _s, e in workspace.ledger.events("alpha", "proc.started")
                   if e.payload.get("request_id") == "r1"]
        assert len(started) == 1
        assert started[0].payload["pid"] == real_proc.pid
        assert started[0].payload["adopted"] is True
        # Still only the one real process -- adoption never spawned a
        # second one alongside it.
        assert procs.is_alive(real_proc.pid) is True
    finally:
        try:
            os.killpg(os.getpgid(real_proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        real_proc.wait(timeout=5)


def test_spawn_recovers_a_completed_dispatch_instead_of_discarding_its_receipt(
    workspace: Workspace, tmp_path: Path,
) -> None:
    """Regression for codex-diff-review-s17.md's "Launch recovery can
    execute completed work twice" (engine/procs.py:328): a worker's
    REAL command can finish -- its sh wrapper's exit-writer step
    writes the real exit code to the exit file -- before the engine
    gets to append proc.started for it; the crash window this
    simulates is strictly AFTER that completion, so there is nothing
    live left to adopt (see test_spawn_adopts_a_live_process_left_by_a
    _crash_before_proc_started for the still-live sibling case) and,
    before this fix, the fall-through path unconditionally deleted the
    exit file, truncated the already-written stdout, and launched a
    SECOND, fully redundant process for identical work.

    A later spawn() call for the IDENTICAL identity must now adopt
    that already-finished dispatch instead: no new process launched
    (the given argv, if actually run, would overwrite the stdout
    marker below with different content -- it never gets the chance
    to), the real exit file/stdout left byte-for-byte untouched, and a
    proc.started event recorded (pid=procs._RECOVERED_PID, never a
    real pid this call never forked) immediately paired with a
    matching proc.ended -- so every pid-keyed reader (is_alive,
    wait_for_exit's exit-file fallback) sees exactly the real,
    recovered outcome."""
    stdout_path = tmp_path / "stations" / "r1.stdout"
    stdout_path.parent.mkdir(parents=True)
    stdout_path.write_text("REAL-COMPLETED-OUTPUT\n", encoding="utf-8")
    exit_file = procs.exit_file_for(stdout_path)
    exit_file.parent.mkdir(parents=True, exist_ok=True)
    exit_file.write_text("0", encoding="utf-8")
    workspace.ledger.append(Event(
        project="alpha", kind="proc.launch_intent", actor_kind="processor",
        actor_id="processor", attempt_id="J-alpha-TEST#7",
        payload={"station": "build", "request_id": "r1",
                 "stdout_path": str(stdout_path)},
    ))

    recovered = procs.spawn(
        workspace.ledger, "alpha", "J-alpha-TEST#7", "build",
        [sys.executable, "-c",
         "open(%r, 'w').write('DUPLICATE-RUN')" % str(stdout_path)],
        cwd=workspace.alpha, stdout_path=stdout_path, request_id="r1",
    )

    assert recovered.pid == procs._RECOVERED_PID
    # The receipt is untouched -- no second process ever ran.
    assert stdout_path.read_text(encoding="utf-8") == "REAL-COMPLETED-OUTPUT\n"
    assert exit_file.read_text(encoding="utf-8") == "0"
    assert procs.is_alive(recovered.pid) is False

    started = [e for _s, e in workspace.ledger.events("alpha", "proc.started")
               if e.payload.get("request_id") == "r1"]
    assert len(started) == 1
    assert started[0].payload["pid"] == procs._RECOVERED_PID
    assert started[0].payload["recovered_completed"] is True
    assert started[0].payload["recovered_exit_code"] == 0

    ended = [e for _s, e in workspace.ledger.events("alpha", "proc.ended")
             if e.payload.get("pid") == procs._RECOVERED_PID]
    assert len(ended) == 1
    # Never shows up as something close_station still needs to reap.
    assert workspace.ledger.live_procs("J-alpha-TEST#7", "build") == []


def test_spawn_with_no_unresolved_intent_never_adopts(workspace: Workspace) -> None:
    """A plain, fresh spawn() call (no prior proc.launch_intent for this
    identity at all) must never go anywhere near the adoption path --
    regression guard for _has_unresolved_intent's own "no intents at
    all" case."""
    proc = procs.spawn(
        workspace.ledger, "alpha", "J-alpha-TEST#6", "build",
        ["sleep", "30"], cwd=workspace.alpha, request_id="r1",
    )
    try:
        started = [e for _s, e in workspace.ledger.events("alpha", "proc.started")
                   if e.payload.get("request_id") == "r1"]
        assert len(started) == 1
        assert "adopted" not in started[0].payload
    finally:
        procs.close_station(workspace.ledger, "alpha", "J-alpha-TEST#6", "build")
        proc.wait(timeout=5)


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


@_needs_bwrap
def test_resume_after_a_killed_engine_sh_bwrap_chain_leaves_no_orphan_and_no_duplicate(
    workspace: Workspace, tmp_path: Path,
) -> None:
    """Combines the two guarantees item 3 needs together, against the
    REAL engine->sh->bwrap chain (not a same-process simulation): a
    separate stand-in "engine" process does exactly what a crashed
    spawn() call would have done up to the crash point -- append
    proc.launch_intent, then Popen the real sandboxed command -- and
    is SIGKILLed before it ever appends proc.started (same simulated-
    crash shape as test_engine_death_leaves_no_survivor_under_bwrap,
    but stopped one step earlier). The engine-death chain (--die-with-
    parent + PR_SET_PDEATHSIG) already tears the whole sh/bwrap tree
    down with the stand-in, so by the time a FRESH engine resumes
    (this test process calling spawn() again for the identical
    identity), there is nothing left alive to adopt -- which must
    still mean exactly ONE live process and exactly one proc.started
    event afterward, never a stale ledger row left over from the
    killed attempt colliding with the fresh one, and never two live
    processes running at once."""
    standin_script = tmp_path / "engine_standin_precrash.py"
    argv_json = tmp_path / "argv.json"
    pidfile = tmp_path / "standin.pid"
    stdout_path = tmp_path / "stations" / "r1.stdout"
    stdout_path.parent.mkdir(parents=True)
    repo_root = str(Path(__file__).resolve().parents[2])

    sandboxed_argv = sandbox.wrap(
        ["sleep", "50"], cwd=workspace.alpha, mode="bwrap",
        writable=(workspace.alpha,),
        child_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    argv_json.write_text(json.dumps(sandboxed_argv), encoding="utf-8")
    standin_ledger = tmp_path / "standin_ledger.sqlite"
    standin_script.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {repo_root!r})\n"
        "from engine.ledger import Event, Ledger\n"
        "real_argv = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))\n"
        "stdout_path = Path(sys.argv[4])\n"
        "ledger = Ledger.create(Path(sys.argv[1]))\n"
        "ledger.register_project('alpha')\n"
        "ledger.append(Event(project='alpha', kind='proc.launch_intent',\n"
        "    actor_kind='processor', actor_id='processor', attempt_id='a1',\n"
        "    payload={'station': 'build', 'request_id': 'r1',\n"
        "             'stdout_path': str(stdout_path)}))\n"
        "with open(stdout_path, 'wb') as f:\n"
        "    import subprocess\n"
        "    p = subprocess.Popen(real_argv, stdout=f, stderr=subprocess.STDOUT,\n"
        "                         start_new_session=True)\n"
        "Path(sys.argv[3]).write_text(str(p.pid), encoding='utf-8')\n"
        # Deliberately never appends proc.started -- this IS the crash
        # window being simulated.
        "import time; time.sleep(60)\n",
        encoding="utf-8",
    )
    standin = subprocess.Popen(
        [sys.executable, str(standin_script), str(standin_ledger), str(argv_json),
         str(pidfile), str(stdout_path)],
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

    os.kill(standin.pid, signal.SIGKILL)  # simulated crash, before proc.started
    standin.wait(timeout=5)

    deadline = time.time() + 5
    while time.time() < deadline and any(
        Path("/proc", str(pid)).exists() for pid in all_watched
    ):
        time.sleep(0.05)
    survivors = [pid for pid in all_watched if Path("/proc", str(pid)).exists()]
    assert survivors == [], "no orphan must remain once the whole chain is gone"

    from engine.ledger import Ledger
    resumed_ledger = Ledger.open(standin_ledger)
    try:
        assert not [e for _s, e in resumed_ledger.events("alpha", "proc.started")
                    if e.payload.get("request_id") == "r1"]

        new_proc = procs.spawn(
            resumed_ledger, "alpha", "a1", "build", ["sleep", "5"],
            cwd=workspace.alpha, stdout_path=stdout_path, request_id="r1",
        )
        try:
            started = [e for _s, e in resumed_ledger.events("alpha", "proc.started")
                       if e.payload.get("request_id") == "r1"]
            assert len(started) == 1
            assert "adopted" not in started[0].payload
            assert procs.is_alive(new_proc.pid) is True
        finally:
            os.killpg(os.getpgid(new_proc.pid), signal.SIGKILL)
            new_proc.wait(timeout=5)
    finally:
        resumed_ledger.close()
