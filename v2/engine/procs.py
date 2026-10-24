"""Child processes per station, each in its own session/process group,
recorded in the ledger so a closed station can kill everything it spawned.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from engine.ledger import Event, Ledger


def spawn(ledger: Ledger, project: str, attempt_id: str, station: str,
          argv: list[str], cwd: Path, *, env: dict[str, str] | None = None,
          stdout_path: Path | None = None, stdin_devnull: bool = False,
          request_id: str | None = None) -> subprocess.Popen[bytes]:
    """Popen with start_new_session=True; append proc.started {pid, pgid,
    station, request_id}.

    env=None inherits this process's own environment (Popen's default),
    exactly as before this parameter existed; a worker runner instead
    passes an explicit dict (its own small passthrough allowlist plus
    GB_* identity vars) so the child never inherits the operator's full
    environment. stdout_path, if given, is opened for combined
    stdout+stderr and closed again once Popen has started the child (the
    child already has its own duplicated fd by then); stdin_devnull
    redirects stdin from /dev/null instead of inheriting it (needed by
    e.g. `codex exec`, which otherwise blocks on stdin)."""
    stdout_handle = None
    try:
        if stdout_path is not None:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stdout_handle = open(stdout_path, "wb")
        proc = subprocess.Popen(
            argv, cwd=str(cwd), env=env,
            stdout=stdout_handle if stdout_handle is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if stdout_handle is not None else subprocess.DEVNULL,
            stdin=subprocess.DEVNULL if stdin_devnull else None,
            start_new_session=True,
        )
    finally:
        if stdout_handle is not None:
            stdout_handle.close()
    pgid = os.getpgid(proc.pid)
    ledger.append(Event(
        project=project, kind="proc.started", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"pid": proc.pid, "pgid": pgid, "station": station,
                  "request_id": request_id},
    ))
    return proc


def close_station(ledger: Ledger, project: str, attempt_id: str, station: str,
                  grace_sec: float = 0.5) -> list[int]:
    """SIGTERM every recorded pgid of that station, wait grace_sec, SIGKILL
    any process GROUP still alive, wait up to grace_sec again, append
    proc.ended for each recorded pid. Liveness is checked at the process
    group (not just the recorded leader pid): a descendant that ignores
    SIGTERM, left behind after its leader already exited, keeps the group
    alive and must still be reached by the group SIGKILL. proc.ended is
    recorded only once every group is confirmed gone; otherwise raises
    RuntimeError and records nothing, so a live survivor is never reported
    as closed."""
    live = ledger.live_procs(attempt_id, station)
    if not live:
        return []

    pgids = {pgid for _, pgid in live}
    pids = {pid for pid, _ in live}
    _signal_group(pgids, signal.SIGTERM)
    _wait_until_groups_gone(pgids, pids, grace_sec)

    if _any_group_alive(pgids):
        _signal_group(pgids, signal.SIGKILL)
        _wait_until_groups_gone(pgids, pids, grace_sec)

    if _any_group_alive(pgids):
        raise RuntimeError("station_procs_alive")

    killed: list[int] = []
    for pid, _ in live:
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        killed.append(pid)
        ledger.append(Event(
            project=project, kind="proc.ended", actor_kind="processor",
            actor_id="processor", attempt_id=attempt_id, payload={"pid": pid},
        ))
    return killed


def _signal_group(pgids: set[int], sig: int) -> None:
    for pgid in pgids:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _any_group_alive(pgids: set[int]) -> bool:
    return any(_group_alive(pgid) for pgid in pgids)


def _wait_until_groups_gone(pgids: set[int], recorded_pids: set[int],
                            grace_sec: float) -> None:
    """Poll until every group is gone or grace_sec elapses. Opportunistically
    reaps each recorded pid (WNOHANG) on every tick: a killed direct child
    becomes a zombie — which still answers os.killpg(pgid, 0) as "alive" —
    until its parent reaps it, so a liveness check taken without reaping
    first would spuriously see the group as still alive forever. A
    descendant that is not our child (left behind by a dead leader) is
    reaped by init on its own once killed; this loop just gives that time."""
    deadline = time.time() + grace_sec
    while True:
        for pid in recorded_pids:
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        if not _any_group_alive(pgids) or time.time() >= deadline:
            return
        time.sleep(0.05)


def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
