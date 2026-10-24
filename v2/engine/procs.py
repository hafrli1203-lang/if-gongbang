"""Child processes per station, each in its own session/process group,
recorded in the ledger so a closed station can kill everything it spawned.
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from engine.ledger import Event, Ledger

# The standalone writer script (no `engine` package imports of its own
# -- see its module docstring) that spawn()'s unsandboxed `sh` wrapper
# invokes by absolute path to actually record the exit code. A sibling
# of this file, so it is always resolvable regardless of PYTHONPATH.
_EXIT_WRITER = Path(__file__).resolve().with_name("exit_writer.py")


def exit_file_for(stdout_path: Path) -> Path:
    """The file spawn() has the real command write its own exit code
    into, derived deterministically from `stdout_path` -- never a
    separate parameter a caller also has to pass, and never a path inside stdout_path's OWN (worker-writable)
    directory any more either: it is a SIBLING "exit" directory of
    that directory's own parent -- typically .../stations/<request>.
    stdout -> .../exit/<request>.stdout.exit for engine.steps.station/
    review's own stdout_path shape (home/jobs/<attempt>/stations/
    <request>.stdout) -- never the same worker-writable `stations`
    directory stdout_path itself lives in.

    This needs no change to engine.steps.station/review at all to
    become engine-only: "exit" is just another plain sibling of
    "stations"/"gate-ro"/"worker-home" under the SAME `home/jobs/
    <attempt>/` tree those modules' own _sandboxed_argv already hides
    wholesale from every sandboxed child and never re-exposes (neither
    one adds a "exit" entry to any writable/readonly mount list), so
    under sandbox mode="bwrap" this directory does not exist inside
    the sandbox at all -- exactly the same "hidden by not being
    mounted" effect engine.steps.station's own gate-ro directory
    achieves, with no dedicated mount of its own either.

    Before this: the exit file sat right next to
    stdout_path in the SAME worker-writable directory. Any code the
    worker's sandbox runs (not just its own gated tool calls -- e.g.
    the project's own test suite, once an allowed Bash command invokes
    it) could reach that directory and swap the file for a symlink
    before the real command exited (making the unsandboxed `sh`
    wrapper that later writes the exit code write through it instead),
    or pre-plant a forged '0' so a crashed/malicious worker is wrongly
    read back as a success on resume. See exit_writer.py's own module
    docstring for the write-side defense (O_EXCL|O_NOFOLLOW) kept as a
    second layer on top of this directory-level isolation. Every call
    site that spawns a worker gives procs.spawn a stdout_path AND
    always lists that same path first in the progress_paths it later
    hands engine.workers.base.wait_while_progressing, so that function
    derives the identical (now-safe) path from progress_paths[0] with
    no new argument of its own either."""
    return stdout_path.parent.parent / "exit" / (stdout_path.name + ".exit")


def spawn(ledger: Ledger, project: str, attempt_id: str, station: str,
          argv: list[str], cwd: Path, *, env: dict[str, str] | None = None,
          stdout_path: Path | None = None, exit_file: Path | None = None,
          stdin_devnull: bool = False,
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
    e.g. `codex exec`, which otherwise blocks on stdin).

    exit_file defaults to exit_file_for(stdout_path) when not given
    explicitly (None if stdout_path is also None -- no wrapping at
    all, matching every behavior before either parameter existed). A
    caller may still pass an explicit exit_file of its own (independent
    of stdout_path, or even without a stdout_path at all) -- but every
    existing call site (engine.steps.station/review) needs no change
    at all to get the safe, engine-only location: it already always
    gives stdout_path, and exit_file_for's own docstring is what moved
    the derived path out of the worker-writable directory, not a new
    parameter threaded through those modules.

    exit_file, once resolved, must be a path under an engine-only
    directory a worker/reviewer sandbox never has write (or even read)
    access to -- see exit_file_for's docstring. This process unlinks
    any stale file at that path itself (never following a symlink
    there: Path.unlink removes the link, not its target) strictly
    before the real command starts. The real argv is then wrapped as
    `sh -c '"$@"; code=$?; <python> <exit_writer.py> "$0" "$code";
    exit "$code"' <exit_file> <argv...>` -- sh's own convention makes
    the first operand after the script text $0 (never part of "$@"),
    so the wrapped command's own argv[0]/quoting reaches the real
    process completely unchanged. This runs OUTSIDE whatever sandbox
    wrapping (e.g. a bwrap invocation) argv already carries -- it
    wraps the whole spawned command, sandboxed or not, rather than
    reaching inside it -- and the wrapper always `exit`s with the REAL
    command's own exit code regardless of whether the writer itself
    succeeded (never the writer's own exit status), so every existing
    caller of this exit code is unaffected."""
    if exit_file is None and stdout_path is not None:
        exit_file = exit_file_for(stdout_path)
    stdout_handle = None
    real_argv = argv
    if exit_file is not None:
        exit_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(exit_file.parent, 0o700)
        except OSError:
            pass
        try:
            exit_file.unlink()
        except FileNotFoundError:
            pass
        writer_cmd = f'{shlex.quote(sys.executable)} {shlex.quote(str(_EXIT_WRITER))}'
        real_argv = [
            "sh", "-c",
            f'"$@"; code=$?; {writer_cmd} "$0" "$code"; exit "$code"',
            str(exit_file), *argv,
        ]
    try:
        if stdout_path is not None:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stdout_handle = open(stdout_path, "wb")
        proc = subprocess.Popen(
            real_argv, cwd=str(cwd), env=env,
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
