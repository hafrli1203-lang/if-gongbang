"""Two-layer sandbox for a worker CLI whose shell commands must not see
what the CLI itself needs (see engine.infrastructure.shell_broker for
the mechanism and why it is two sibling sandboxes, not a nested one).

Layer A (the CLI): the caller's own sandbox.wrap() policy, plus the
shared temp directory (writable) and the jail's client script and
socket directory (read-only).

Layer B (every shell command and hook command the CLI runs, except the
runner's `local_shell_commands()`): no network namespace shared, the
same hidden roots as layer A plus the host service-socket dirs, none of
the CLI's HOME, gate-ro dir or the jail's socket dir, a fresh empty HOME
of its own, and only the paths the caller names as writable/read-only.

Applies only under sandbox mode "bwrap" and only to a runner that
declares `shell_prefix_env` (a CLI with a documented shell-prefix
setting). Every other runner/mode keeps its single-layer argv.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from engine.infrastructure import sandbox
from engine.infrastructure import shell_broker

BROKER_SCRIPT = Path(shell_broker.__file__).resolve()
CLIENT_NAME = "shell"

# (extra_writable, extra_readonly) -> the CLI's own wrapped argv.
CliWrap = Callable[[tuple[Path, ...], tuple[Path, ...]], list[str]]


def _python() -> str:
    # The real interpreter, not a venv shim: the broker is stdlib-only and
    # runs with -I, and a venv directory may sit under a hidden root.
    return os.path.realpath(sys.executable)


@dataclass(frozen=True)
class JailPaths:
    root: Path

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"

    @property
    def sock(self) -> Path:
        return self.root / "sock"

    @property
    def bin(self) -> Path:
        return self.root / "bin"

    @property
    def client(self) -> Path:
        return self.bin / CLIENT_NAME

    @property
    def local(self) -> Path:
        return self.bin / "local-commands.json"

    @property
    def spec(self) -> Path:
        return self.root / "launch.json"

    @property
    def log(self) -> Path:
        return self.root / "broker.log"


@dataclass(frozen=True)
class CommandPolicy:
    """What layer B exposes to commands, beyond the jail's own dirs."""
    cwd: Path
    writable: tuple[Path, ...]
    readonly: tuple[Path, ...]
    readonly_remap: tuple[tuple[Path, Path], ...]
    hidden: tuple[Path, ...]
    # True only when the station is granted a browser MCP server: a dev
    # server a command starts must be reachable from that server, which
    # runs under the CLI on the host network. Login/MCP files stay hidden.
    net: bool = False


def jail_root(engine_home: Path, attempt_dirname: str, station: str, role: str) -> Path:
    return engine_home / "jobs" / attempt_dirname / "shell-jail" / station / role


def prepare(root: Path, local_commands: tuple[str, ...] = ()) -> JailPaths:
    """Creates the jail directories, the client script and the list of
    commands the client runs in the CLI layer itself (the policy-gate
    hook command, verbatim)."""
    paths = JailPaths(root)
    for directory in (paths.home, paths.tmp, paths.sock, paths.bin):
        directory.mkdir(parents=True, exist_ok=True)
    _atomic_write(paths.local, json.dumps(list(local_commands)), 0o644)
    script = (
        "#!/bin/sh\n"
        f"exec {shlex.quote(_python())} -I {shlex.quote(str(BROKER_SCRIPT))} "
        f"client {shlex.quote(str(paths.sock))} {shlex.quote(str(paths.local))} \"$@\"\n"
    )
    _atomic_write(paths.client, script, 0o755)
    return paths


def _atomic_write(path: Path, text: str, mode: int) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(mode)
    os.replace(tmp, path)


def cli_env(paths: JailPaths, prefix_var: str) -> dict[str, str]:
    """Env additions for the CLI process: its shell prefix points at the
    jail client, its shell is fixed to bash (the client runs `bash -c`),
    and its temp files land in the directory both layers share (the CLI
    reads back a cwd file and background-task output its commands write
    there)."""
    return {
        prefix_var: str(paths.client),
        "CLAUDE_CODE_SHELL": shell_broker.SHELL,
        "TMPDIR": str(paths.tmp),
    }


def _server_argv(paths: JailPaths) -> list[str]:
    return [_python(), "-I", str(BROKER_SCRIPT), "serve",
            shell_broker.LISTEN_FD_PLACEHOLDER, str(paths.home)]


def build(paths: JailPaths, *, wrap_cli: CliWrap, policy: CommandPolicy,
          mode: str, protected_roots: tuple[Path, ...],
          server_path_env: str) -> list[str]:
    """The launcher argv for procs.spawn. `wrap_cli(extra_writable,
    extra_readonly)` is the caller's own single-layer sandbox.wrap() call
    for the CLI with the given mounts added (layer A). Layer B never gets
    the socket directory: the launcher binds the socket on the host and
    hands the server only the listening fd."""
    cli_argv = wrap_cli((paths.tmp,), (paths.bin, paths.sock))
    try:
        server_argv = sandbox.wrap(
            _server_argv(paths), cwd=policy.cwd,
            writable=policy.writable + (paths.home, paths.tmp),
            readonly=policy.readonly, readonly_remap=policy.readonly_remap,
            hidden=policy.hidden, net=policy.net, mode=mode,
            child_env={"PATH": server_path_env, "HOME": str(paths.home)},
            protected_roots=protected_roots,
        )
    except BaseException:
        sandbox.close_fds(sandbox.pass_fds_for(cli_argv))
        raise
    cli_fds = sandbox.pass_fds_for(cli_argv)
    server_fds = sandbox.pass_fds_for(server_argv)
    _write_spec(paths, cli_argv, server_argv, cli_fds, server_fds)
    argv = [_python(), "-I", str(BROKER_SCRIPT), "launch"]
    for fd in cli_fds + server_fds:
        argv += ["--pass-fd", str(fd)]
    return argv + [str(paths.spec)]


def wrap_for_runner(runner: object, *, mode: str, env: dict[str, str],
                    wrap_cli: CliWrap, root: Path,
                    policy: Callable[[], CommandPolicy],
                    protected_roots: tuple[Path, ...]) -> list[str]:
    """The one entry point both the builder and the reviewer sandbox use:
    the two-layer launcher argv when `mode` is "bwrap" and `runner`
    declares a `shell_prefix_env`, otherwise the caller's own
    single-layer argv unchanged. `env` is the CLI's child env dict that
    `wrap_cli` bakes into layer A; it is updated in place first. The
    runner's `local_shell_commands()` (if any) run in layer A."""
    prefix_var = getattr(runner, "shell_prefix_env", None)
    if mode != "bwrap" or not isinstance(prefix_var, str):
        return wrap_cli((), ())
    local = getattr(runner, "local_shell_commands", None)
    local_commands = tuple(local()) if callable(local) else ()
    paths = prepare(root, local_commands)
    env.update(cli_env(paths, prefix_var))
    return build(paths, wrap_cli=wrap_cli, policy=policy(), mode=mode,
                 protected_roots=protected_roots,
                 server_path_env=env.get("PATH", "/usr/bin:/bin"))


def _write_spec(paths: JailPaths, cli_argv: list[str], server_argv: list[str],
                cli_fds: tuple[int, ...], server_fds: tuple[int, ...]) -> None:
    spec = {
        "cli_argv": cli_argv, "server_argv": server_argv,
        "cli_pass_fds": list(cli_fds), "server_pass_fds": list(server_fds),
        "sock_dir": str(paths.sock), "log_path": str(paths.log),
    }
    _atomic_write(paths.spec, json.dumps(spec), 0o600)


__all__ = ["CliWrap", "CommandPolicy", "JailPaths", "build", "cli_env", "jail_root",
           "prepare", "wrap_for_runner"]
