"""Second sandbox layer for the shell commands a worker CLI runs.

The worker CLI (e.g. `claude`) must keep network access and its own
login file to talk to its model API, so its sandbox cannot drop either.
Code that the CLI's shell tool runs (an allowed `npm test`, a file the
worker wrote, a hook command) must get neither. This host refuses a
nested user namespace from inside a bwrap sandbox, so that code cannot
be wrapped in a second bwrap from inside the first one.

Instead the engine starts TWO sibling bwrap sandboxes from the host:

  - layer A runs the CLI itself (network on, its login file and MCP
    config visible);
  - layer B runs this module's `serve` loop with no network namespace
    shared (`--unshare-all` without `--share-net`: loopback only) and a
    mount view without the CLI's login files, the MCP config, the
    engine's own state or the real HOME.

Layer A's shell entry point is this module's `client` (installed by the
engine as the CLI's shell prefix): it forwards argv, cwd and environment
plus its own stdin/stdout/stderr file descriptors (SCM_RIGHTS) over a
unix socket whose directory is mounted read-only in layer A only, and
layer B runs the command with those descriptors. The command therefore
streams output to the CLI exactly as a local child would, but executes
inside layer B. Commands listed in the client's local-commands file
(the engine's policy-gate hook) run in layer A instead.

`launch` is the host-side parent of both layers: it binds and listens on
the socket itself, starts layer B with only the listening fd, starts
layer A, waits for A, then stops B. All three live in the one process
group the engine's procs.spawn created, so the engine's existing
kill/confirm path covers them, and the launcher dies with its parent.

Stdlib only, no engine imports: every mode runs as `python -I <this
file> ...` inside a sandbox where the engine's own environment and
import path are not available.
"""
from __future__ import annotations

import array
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading

SOCKET_NAME = "s"
# Stands for the listening socket's fd number in the server argv; the
# launcher substitutes it after binding (the number is not known before).
LISTEN_FD_PLACEHOLDER = "@listen-fd@"
SHELL = "/bin/bash"
_HEADER = struct.Struct("!I")
_MAX_REQUEST_BYTES = 64 * 1024 * 1024
_FORWARDED_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT)
_EXIT_BROKER_UNAVAILABLE = 125
_EXIT_EXEC_FAILED = 127


# ---------------------------------------------------------------- protocol

def send_request(sock: socket.socket, request: dict[str, object],
                 fds: tuple[int, int, int]) -> None:
    payload = json.dumps(request).encode("utf-8")
    data = _HEADER.pack(len(payload)) + payload
    sent = sock.sendmsg(
        [data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))])
    if sent < len(data):
        sock.sendall(data[sent:])


def _close_all(fds: list[int]) -> None:
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


def recv_request(sock: socket.socket) -> tuple[dict[str, object], list[int]]:
    fds: list[int] = []
    data, ancdata, _flags, _addr = sock.recvmsg(
        65536, socket.CMSG_SPACE(3 * array.array("i").itemsize))
    for level, kind, cmsg_data in ancdata:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            received = array.array("i")
            usable = len(cmsg_data) - (len(cmsg_data) % received.itemsize)
            received.frombytes(cmsg_data[:usable])
            fds.extend(received)
    try:
        while len(data) < _HEADER.size:
            chunk = sock.recv(65536)
            if not chunk:
                raise ValueError("truncated header")
            data += chunk
        (length,) = _HEADER.unpack(data[:_HEADER.size])
        if length > _MAX_REQUEST_BYTES:
            raise ValueError("request too large")
        body = bytearray(data[_HEADER.size:])
        while len(body) < length:
            chunk = sock.recv(min(1 << 20, length - len(body)))
            if not chunk:
                raise ValueError("truncated body")
            body += chunk
        request = json.loads(bytes(body[:length]).decode("utf-8"))
        if len(fds) != 3:
            raise ValueError("expected exactly three file descriptors")
        return _validated(request), fds
    except (ValueError, UnicodeDecodeError):
        _close_all(fds)
        raise


def _validated(request: object) -> dict[str, object]:
    if not isinstance(request, dict):
        raise ValueError("request is not an object")
    argv = request.get("argv")
    cwd = request.get("cwd")
    env = request.get("env")
    if not (isinstance(argv, list) and argv and all(_plain(a) for a in argv)):
        raise ValueError("argv must be a non-empty list of strings")
    if not _plain(cwd):
        raise ValueError("cwd must be a string")
    if not (isinstance(env, dict)
            and all(_plain(k) and k and "=" not in k and _plain(v) for k, v in env.items())):
        raise ValueError("env must map names to strings")
    return {"argv": argv, "cwd": cwd, "env": env}


def _plain(value: object) -> bool:
    # exec() cannot carry an embedded NUL; reject it here instead of
    # letting Popen raise after the descriptors were received.
    return isinstance(value, str) and "\0" not in value


# ------------------------------------------------------------------ server

def _kill_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _watch_client(conn: socket.socket, proc: subprocess.Popen[bytes],
                  finished: threading.Event) -> None:
    """Forwards signal bytes from the client; a client that disconnects
    before the command finished (killed by its CLI, e.g. on a tool
    timeout) takes the command's whole process group down with it."""
    while True:
        try:
            chunk = conn.recv(64)
        except OSError:
            chunk = b""
        if not chunk:
            if not finished.is_set():
                _kill_group(proc.pid, signal.SIGKILL)
            return
        for signum in chunk:
            _kill_group(proc.pid, signum)


def _spawn(request: dict[str, object], fds: list[int],
           home: str) -> subprocess.Popen[bytes]:
    env = dict(request["env"])  # type: ignore[arg-type]
    env["HOME"] = home
    return subprocess.Popen(
        request["argv"], cwd=request["cwd"], env=env,  # type: ignore[arg-type]
        stdin=fds[0], stdout=fds[1], stderr=fds[2],
        start_new_session=True, close_fds=True,
    )


def _report_spawn_failure(fd: int, exc: Exception) -> None:
    try:
        os.write(fd, f"shell_broker: cannot run command: {exc}\n".encode())
    except OSError:
        pass


def _handle(conn: socket.socket, home: str) -> None:
    with conn:
        try:
            request, fds = recv_request(conn)
        except (OSError, ValueError, UnicodeDecodeError):
            return
        try:
            proc = _spawn(request, fds, home)
        except (OSError, ValueError, TypeError) as exc:
            _report_spawn_failure(fds[2], exc)
            _close_all(fds)
            _reply(conn, _EXIT_EXEC_FAILED)
            return
        _close_all(fds)
        finished = threading.Event()
        threading.Thread(target=_watch_client, args=(conn, proc, finished),
                         daemon=True).start()
        returncode = proc.wait()
        finished.set()
        _reply(conn, returncode)


def _reply(conn: socket.socket, returncode: int) -> None:
    try:
        conn.sendall(json.dumps({"rc": returncode}).encode("utf-8") + b"\n")
    except OSError:
        pass


def serve(listen_fd: int, home: str) -> int:
    """Accepts on a socket the launcher already bound and listened on
    (inherited fd). The command layer never has the socket's directory
    mounted at all, so code running there cannot unlink and rebind the
    socket path to impersonate this server towards the CLI layer."""
    listener = socket.socket(fileno=listen_fd)
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    while True:
        conn, _addr = listener.accept()
        threading.Thread(target=_handle, args=(conn, home), daemon=True).start()


# ------------------------------------------------------------------ client

def _std_fds() -> tuple[int, int, int]:
    fds = []
    for fd in (0, 1, 2):
        try:
            os.fstat(fd)
            fds.append(fd)
        except OSError:
            fds.append(os.open(os.devnull, os.O_RDWR))
    return fds[0], fds[1], fds[2]


def _connect(sock_dir: str) -> socket.socket:
    # A relative name after chdir keeps the socket address short: unix
    # socket paths are limited to 108 bytes and the engine's per-attempt
    # directories can be longer than that.
    here = os.open(".", os.O_RDONLY | os.O_DIRECTORY) if _cwd_ok() else None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        os.chdir(sock_dir)
        sock.connect(SOCKET_NAME)
    finally:
        if here is not None:
            os.fchdir(here)
            os.close(here)
    return sock


def _cwd_ok() -> bool:
    try:
        os.getcwd()
        return True
    except OSError:
        return False


def _exit_code(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def _local_commands(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
    except (OSError, ValueError):
        return []
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def client(sock_dir: str, local_path: str, args: list[str]) -> int:
    """Runs one shell command for the CLI. A command listed verbatim in
    `local_path` (the engine's own policy-gate hook) runs right here in
    the CLI layer instead: it must stay out of reach of code running in
    the command layer (a process there could otherwise open the gate's
    stdout through /proc and answer for it)."""
    if not args:
        sys.stderr.write("shell_broker: no command\n")
        return 2
    if len(args) == 1 and args[0] in _local_commands(local_path):
        os.execv(SHELL, [SHELL, "-c", args[0]])
    argv = [SHELL, "-c", args[0]] if len(args) == 1 else list(args)
    cwd = os.getcwd() if _cwd_ok() else "/"
    try:
        sock = _connect(sock_dir)
        send_request(sock, {"argv": argv, "cwd": cwd, "env": dict(os.environ)}, _std_fds())
    except OSError as exc:
        sys.stderr.write(f"shell_broker: sandbox shell unavailable: {exc}\n")
        return _EXIT_BROKER_UNAVAILABLE
    for signum in _FORWARDED_SIGNALS:
        signal.signal(signum, lambda num, _frame: _forward(sock, num))
    reply = b""
    while not reply.endswith(b"\n"):
        try:
            chunk = sock.recv(4096)
        except InterruptedError:
            continue
        if not chunk:
            sys.stderr.write("shell_broker: sandbox shell closed the connection\n")
            return _EXIT_BROKER_UNAVAILABLE
        reply += chunk
    return _exit_code(int(json.loads(reply.decode("utf-8"))["rc"]))


def _forward(sock: socket.socket, signum: int) -> None:
    try:
        sock.send(bytes([signum]))
    except OSError:
        pass


# ---------------------------------------------------------------- launcher

_PR_SET_PDEATHSIG = 1


def _die_with_parent() -> None:
    """SIGKILL this launcher when its parent (the engine's receipt shell)
    dies, so an engine crash does not leave both sandboxes running
    unsupervised; both bwrap layers already die with this process."""
    parent = os.getppid()
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
    except (OSError, AttributeError):
        return
    if parent == 1 or os.getppid() != parent:
        os._exit(_EXIT_BROKER_UNAVAILABLE)


def _listen(sock_dir: str) -> socket.socket:
    here = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.chdir(sock_dir)
        try:
            os.unlink(SOCKET_NAME)
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(SOCKET_NAME)
        os.chmod(SOCKET_NAME, 0o600)
        listener.listen(64)
    finally:
        os.fchdir(here)
        os.close(here)
    listener.set_inheritable(True)
    return listener


def _stop(server: subprocess.Popen[bytes]) -> None:
    if server.poll() is not None:
        return
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()


def _int_list(value: object) -> tuple[int, ...]:
    if not isinstance(value, list) or not all(isinstance(v, int) for v in value):
        raise ValueError("expected a list of ints")
    return tuple(value)


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("expected a list of strings")
    return list(value)


def launch(spec_path: str) -> int:
    _die_with_parent()
    with open(spec_path, encoding="utf-8") as f:
        spec = json.load(f)
    cli_argv = _str_list(spec["cli_argv"])
    # The listening socket exists before either layer starts: early
    # client connections wait in its backlog, no readiness polling.
    listener = _listen(str(spec["sock_dir"]))
    server_argv = [str(listener.fileno()) if a == LISTEN_FD_PLACEHOLDER else a
                   for a in _str_list(spec["server_argv"])]
    with open(str(spec["log_path"]), "ab") as log:
        server = subprocess.Popen(
            server_argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            pass_fds=_int_list(spec["server_pass_fds"]) + (listener.fileno(),),
            close_fds=True)
    listener.close()
    try:
        cli = subprocess.Popen(cli_argv, pass_fds=_int_list(spec["cli_pass_fds"]),
                               close_fds=True)
        for signum in _FORWARDED_SIGNALS:
            signal.signal(signum, lambda num, _frame: cli.send_signal(num))
        return _exit_code(cli.wait())
    finally:
        _stop(server)


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[0] == "serve":
        return serve(int(argv[1]), argv[2])
    if len(argv) >= 3 and argv[0] == "client":
        return client(argv[1], argv[2], argv[3:])
    if len(argv) == 2 and argv[0] == "launch":
        return launch(argv[1])
    if len(argv) >= 2 and argv[0] == "launch":
        # `launch [--pass-fd N]... <spec>`: the --pass-fd tokens only tell
        # the engine's spawner which inherited fds to keep open.
        return launch(argv[-1])
    sys.stderr.write("usage: shell_broker.py serve|client|launch ...\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
