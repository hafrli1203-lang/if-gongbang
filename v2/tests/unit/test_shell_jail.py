"""Second sandbox layer for worker shell commands (engine.infrastructure.
shell_broker + engine.application.steps.worker_shell_jail).

The bwrap tests run the REAL two-layer launcher on this host with a stub
CLI standing in for `claude`: the stub (layer A) must still start, read
its own login file and MCP config and reach a host TCP listener, while a
command it runs through its shell prefix (layer B) must not see any of
those and must not reach the listener.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.application.acceptance_bench import run_in_bench_sandbox
from engine.application.steps import review, worker_sandbox, worker_shell_jail
from engine.domain import ids
from engine.infrastructure import sandbox, shell_broker
from engine.infrastructure.workers.base import WorkerSpec, gate_env
from engine.infrastructure.workers.claude_cli import ClaudeRunner, _gate_command
from engine.infrastructure.workers.codex_cli import CodexRunner
from engine.config import WorkerConfig, to_json_dict
from tests.conftest import Workspace
from tests.security_gate import bwrap_skipif

_needs_bwrap = bwrap_skipif(sandbox.available(), reason="bwrap not available on this host")

BROKER = str(Path(shell_broker.__file__).resolve())
PY = os.path.realpath(sys.executable)


# ------------------------------------------------------------ protocol / mode none

def test_request_roundtrip_carries_three_fds() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    r, w = os.pipe()
    try:
        big = "x" * 300_000  # larger than one socket buffer: sent in pieces
        sender = threading.Thread(target=shell_broker.send_request, args=(
            left, {"argv": ["echo", big], "cwd": "/", "env": {"A": "1"}}, (r, w, w)))
        sender.start()
        request, fds = shell_broker.recv_request(right)
        sender.join(10)
        assert request["argv"] == ["echo", big]
        assert request["env"] == {"A": "1"}
        assert len(fds) == 3
        os.write(fds[1], b"via-passed-fd")
        assert os.read(r, 100) == b"via-passed-fd"
        for fd in fds:
            os.close(fd)
    finally:
        for fd in (r, w):
            os.close(fd)
        left.close()
        right.close()


def test_request_without_fds_is_rejected() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        payload = json.dumps({"argv": ["true"], "cwd": "/", "env": {}}).encode()
        left.sendall(len(payload).to_bytes(4, "big") + payload)
        with pytest.raises(ValueError):
            shell_broker.recv_request(right)
    finally:
        left.close()
        right.close()


def _start_server(sock_dir: Path, home: Path) -> subprocess.Popen[bytes]:
    listener = shell_broker._listen(str(sock_dir))
    try:
        return subprocess.Popen([PY, "-I", BROKER, "serve", str(listener.fileno()), str(home)],
                                pass_fds=(listener.fileno(),))
    finally:
        listener.close()


def _client(sock_dir: Path, command: str) -> list[str]:
    return [PY, "-I", BROKER, "client", str(sock_dir), str(sock_dir / "no-local.json"), command]


def test_request_with_nul_is_rejected_without_leaking_fds() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    r, w = os.pipe()
    before = len(os.listdir("/proc/self/fd"))
    try:
        for _ in range(5):
            shell_broker.send_request(
                left, {"argv": ["echo", "a\0b"], "cwd": "/", "env": {}}, (r, w, w))
            with pytest.raises(ValueError):
                shell_broker.recv_request(right)
        assert len(os.listdir("/proc/self/fd")) == before
    finally:
        for fd in (r, w):
            os.close(fd)
        left.close()
        right.close()


def test_client_runs_command_through_server(tmp_path: Path) -> None:
    sock_dir, home, cwd = tmp_path / "sock", tmp_path / "home", tmp_path / "cwd"
    for d in (sock_dir, home, cwd):
        d.mkdir()
    server = _start_server(sock_dir, home)
    try:
        result = subprocess.run(
            _client(sock_dir, 'echo "cwd=$(pwd) home=$HOME var=$GBX in=$(cat)"; exit 9'),
            cwd=cwd, input="stdin-data", capture_output=True, text=True, timeout=20,
            env={"PATH": os.environ["PATH"], "GBX": "forwarded", "HOME": "/nowhere"})
    finally:
        server.terminate()
        server.wait(5)
    assert result.returncode == 9
    assert f"cwd={cwd}" in result.stdout
    assert f"home={home}" in result.stdout  # HOME is the command layer's own
    assert "var=forwarded" in result.stdout
    assert "in=stdin-data" in result.stdout


def test_client_killed_takes_command_group_down(tmp_path: Path) -> None:
    sock_dir, home = tmp_path / "sock", tmp_path / "home"
    sock_dir.mkdir()
    home.mkdir()
    marker = tmp_path / "late"
    server = _start_server(sock_dir, home)
    try:
        client = subprocess.Popen(
            _client(sock_dir, f"sleep 3; touch {marker}"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.5)
        client.send_signal(signal.SIGKILL)
        client.wait(5)
        time.sleep(3.5)
    finally:
        server.terminate()
        server.wait(5)
    assert not marker.exists()


def test_pass_fds_for_reads_launcher_tokens() -> None:
    argv = [PY, "-I", BROKER, "launch", "--pass-fd", "7", "--pass-fd", "9", "/x/launch.json"]
    assert sandbox.pass_fds_for(argv) == (7, 9)
    assert sandbox.pass_fds_for(["echo", "--pass-fd", "3"]) == ()


def test_mode_none_keeps_single_layer(tmp_path: Path) -> None:
    calls: list[tuple[tuple[Path, ...], tuple[Path, ...]]] = []

    def wrap_cli(w: tuple[Path, ...], r: tuple[Path, ...]) -> list[str]:
        calls.append((w, r))
        return ["cli"]

    env: dict[str, str] = {}
    argv = worker_shell_jail.wrap_for_runner(
        ClaudeRunner(), mode="none", env=env, wrap_cli=wrap_cli, root=tmp_path / "j",
        policy=lambda: pytest.fail("policy must not be built"), protected_roots=())
    assert argv == ["cli"]
    assert calls == [((), ())]
    assert env == {}
    assert not (tmp_path / "j").exists()


def test_runner_without_prefix_keeps_single_layer(tmp_path: Path) -> None:
    argv = worker_shell_jail.wrap_for_runner(
        CodexRunner(), mode="bwrap", env={}, wrap_cli=lambda w, r: ["cli"],
        root=tmp_path / "j", policy=lambda: pytest.fail("no policy"), protected_roots=())
    assert argv == ["cli"]


def test_codex_reviewer_uses_read_only_landlock_sandbox(tmp_path: Path) -> None:
    spec = WorkerSpec(project="alpha", attempt_id="J-a#1", generation=1, station="review",
                      role="reviewer", copy_root=tmp_path, workdir=tmp_path, task="",
                      result_path=tmp_path / "r.json", config_path=None)
    argv = CodexRunner().argv(spec, WorkerConfig(runner="codex"))
    assert argv[argv.index("-s") + 1] == "read-only"
    assert "features.use_legacy_landlock=true" in argv
    assert "danger-full-access" not in argv


# ------------------------------------------------------------- real two-layer run

_STUB_CLI = textwrap.dedent("""\
    import json, os, socket, subprocess, sys
    login, mcp, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
    def can_read(p):
        try:
            return "SECRET" in open(p).read()
        except OSError:
            return False
    def can_connect():
        try:
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
            return True
        except OSError:
            return False
    print("CLI " + json.dumps({"login": can_read(login), "mcp": can_read(mcp),
                               "net": can_connect()}), flush=True)
    probe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe.py")
    cmd = " ".join([sys.executable, probe, login, mcp, str(port), "; exit 5"])
    rc = subprocess.run([os.environ["CLAUDE_CODE_SHELL_PREFIX"], cmd]).returncode
    print("CMD_RC", rc, flush=True)
    """)

_PROBE = textwrap.dedent("""\
    import json, os, socket, sys
    login, mcp, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
    def can_read(p):
        try:
            return "SECRET" in open(p).read()
        except OSError:
            return False
    def can_connect(host):
        try:
            socket.create_connection((host, port), timeout=2).close()
            return True
        except OSError:
            return False
    ifaces = [l.split(":")[0].strip() for l in open("/proc/net/dev").readlines()[2:]]
    prefix = os.environ.get("CLAUDE_CODE_SHELL_PREFIX", "/none/bin/shell")
    sock = os.path.join(os.path.dirname(os.path.dirname(prefix)), "sock")
    home_login = os.path.join(os.environ.get("HOME", "/"), ".claude", ".credentials.json")
    wrote = True
    try:
        open(os.path.join(os.getcwd(), "written-by-command"), "w").write("x")
    except OSError:
        wrote = False
    print("CMD " + json.dumps({"login": can_read(login), "mcp": can_read(mcp),
                               "home_login": can_read(home_login),
                               "net": can_connect("127.0.0.1"), "ifaces": ifaces,
                               "wrote_copy": wrote, "run": os.listdir("/run"),
                               "sock_visible": os.path.exists(sock)}), flush=True)
    """)


class _StubClaudeRunner:
    """ClaudeRunner's sandbox-relevant attributes, without the real CLI."""
    home_writable_subpaths = ClaudeRunner.home_writable_subpaths
    shell_prefix_env = ClaudeRunner.shell_prefix_env

    def local_shell_commands(self) -> tuple[str, ...]:
        return ClaudeRunner().local_shell_commands()


def _host_listener() -> tuple[socket.socket, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)

    def accept_forever() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            conn.close()

    threading.Thread(target=accept_forever, daemon=True).start()
    return listener, listener.getsockname()[1]


def _parse(tag: str, stdout: str) -> dict[str, object]:
    for line in stdout.splitlines():
        if line.startswith(tag + " "):
            return json.loads(line[len(tag) + 1:])
    raise AssertionError(f"no {tag} line in output:\n{stdout}")


def _run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    fds = sandbox.pass_fds_for(argv)
    try:
        return subprocess.run(argv, pass_fds=fds, capture_output=True, text=True,  # type: ignore[call-overload]
                              timeout=60, env=sandbox.launcher_env(), **kwargs)
    finally:
        sandbox.close_fds(fds)


def _builder_setup(workspace: Workspace) -> tuple[SimpleNamespace, WorkerSpec, Path, Path, Path]:
    attempt_id = "J-alpha-JAIL01#1"
    home = workspace.config.home
    copy = home / "copies" / attempt_id / "builder"
    copy.mkdir(parents=True)
    stations = home / "jobs" / ids.attempt_dirname(attempt_id) / "stations" / "R1"
    stations.mkdir(parents=True)
    gate_ro = home / "jobs" / ids.attempt_dirname(attempt_id) / "gate-ro" / "R1"
    gate_ro.mkdir(parents=True)
    (gate_ro / "gate_facts.json").write_text("{}", encoding="utf-8")
    mcp = gate_ro / "mcp-config.json"
    mcp.write_text('{"mcpServers": {"x": {"env": {"K": "MCP-SECRET"}}}}', encoding="utf-8")
    gate_log = stations / "gate.log"
    gate_log.write_text("", encoding="utf-8")
    worker_home = (home / "jobs" / ids.attempt_dirname(attempt_id) / "worker-home"
                   / "build" / "builder")
    login = worker_home / ".claude" / ".credentials.json"
    login.parent.mkdir(parents=True)
    login.write_text('{"token": "LOGIN-SECRET"}', encoding="utf-8")
    (copy / "fake_cli.py").write_text(_STUB_CLI, encoding="utf-8")
    (copy / "probe.py").write_text(_PROBE, encoding="utf-8")
    config = replace(workspace.config, sandbox="bwrap")
    spec = WorkerSpec(
        project="alpha", attempt_id=attempt_id, generation=1, station="build",
        role="builder", copy_root=copy, workdir=copy, task="",
        result_path=stations / "r.json", config_path=None,
        gate_facts_path=gate_ro / "gate_facts.json", gate_log_path=gate_log,
    )
    ctx = SimpleNamespace(config=config, project="alpha", attempt_id=attempt_id)
    return ctx, spec, copy, login, mcp


@_needs_bwrap
def test_two_layer_builder_hides_login_mcp_and_network_from_commands(
    workspace: Workspace,
) -> None:
    ctx, spec, copy, login, mcp = _builder_setup(workspace)
    listener, port = _host_listener()
    try:
        env = {"PATH": os.environ["PATH"], "HOME": str(workspace.root / "real-home")}
        argv = worker_sandbox.sandboxed_argv(
            ctx, spec, [PY, str(copy / "fake_cli.py"), str(login), str(mcp), str(port)],
            env, _StubClaudeRunner())
        assert argv[3] == "launch"
        result = _run(argv, cwd=str(copy))
    finally:
        listener.close()
    assert result.returncode == 0, result.stdout + result.stderr
    cli = _parse("CLI", result.stdout)
    cmd = _parse("CMD", result.stdout)
    # (c) the CLI layer still starts and keeps what it needs
    assert cli == {"login": True, "mcp": True, "net": True}
    # (a) the command layer sees neither the login file nor the MCP config
    assert cmd["login"] is False
    assert cmd["home_login"] is False
    assert cmd["mcp"] is False
    # (b) the command layer has no network beyond its own loopback
    assert cmd["net"] is False
    assert cmd["ifaces"] == ["lo"]
    assert cmd["run"] == []  # no host service sockets (D-Bus, resolver, DB)
    # the broker socket directory is not mounted in the command layer
    assert cmd["sock_visible"] is False
    # commands still work on the worker's own copy, and the exit code returns
    assert cmd["wrote_copy"] is True
    assert (copy / "written-by-command").exists()
    assert "CMD_RC 5" in result.stdout


@_needs_bwrap
def test_two_layer_builder_cli_env_points_at_jail(workspace: Workspace) -> None:
    ctx, spec, copy, _login, _mcp = _builder_setup(workspace)
    env = {"PATH": os.environ["PATH"]}
    worker_sandbox.sandboxed_argv(ctx, spec, ["true"], env, _StubClaudeRunner())
    root = worker_shell_jail.jail_root(ctx.config.home, ids.attempt_dirname(ctx.attempt_id),
                                       "build", "builder")
    assert env["CLAUDE_CODE_SHELL_PREFIX"] == str(root / "bin" / "shell")
    assert env["CLAUDE_CODE_SHELL"] == "/bin/bash"
    assert env["TMPDIR"] == str(root / "tmp")
    spec_json = json.loads((root / "launch.json").read_text(encoding="utf-8"))
    server_argv = spec_json["server_argv"]
    assert "--share-net" not in server_argv
    assert "--share-net" in spec_json["cli_argv"]


@_needs_bwrap
def test_two_layer_reviewer_hides_login_and_network(workspace: Workspace) -> None:
    attempt_id = "J-alpha-JAIL02#1"
    home = workspace.config.home
    snapshot = home / "review-snap" / attempt_id
    snapshot.mkdir(parents=True)
    stations = home / "jobs" / ids.attempt_dirname(attempt_id) / "stations"
    stations.mkdir(parents=True)
    reviewer_home = (home / "jobs" / ids.attempt_dirname(attempt_id) / "worker-home"
                     / "review" / "reviewer")
    login = reviewer_home / ".claude" / ".credentials.json"
    login.parent.mkdir(parents=True)
    login.write_text("LOGIN-SECRET", encoding="utf-8")
    # The reviewer snapshot is read-only, so the stub scripts live there.
    (snapshot / "fake_cli.py").write_text(_STUB_CLI, encoding="utf-8")
    (snapshot / "probe.py").write_text(_PROBE, encoding="utf-8")
    config = replace(workspace.config, sandbox="bwrap")
    spec = WorkerSpec(project="alpha", attempt_id=attempt_id, generation=1,
                      station="review", role="reviewer", copy_root=snapshot,
                      workdir=snapshot, task="", result_path=stations / "r.json",
                      config_path=None)
    ctx = SimpleNamespace(config=config, project="alpha", attempt_id=attempt_id,
                          request=SimpleNamespace(request_id="R1"))
    listener, port = _host_listener()
    try:
        argv = review._sandboxed_argv(
            ctx, spec, [PY, str(snapshot / "fake_cli.py"), str(login), "/nonexistent", str(port)],
            {"PATH": os.environ["PATH"]}, _StubClaudeRunner())
        result = _run(argv, cwd=str(snapshot))
    finally:
        listener.close()
    assert result.returncode == 0, result.stdout + result.stderr
    cli = _parse("CLI", result.stdout)
    cmd = _parse("CMD", result.stdout)
    assert cli["login"] is True and cli["net"] is True
    assert cmd["login"] is False and cmd["home_login"] is False
    assert cmd["net"] is False and cmd["ifaces"] == ["lo"] and cmd["run"] == []
    assert cmd["wrote_copy"] is False  # the snapshot stays read-only


@_needs_bwrap
def test_bench_runs_without_network_or_login_files(workspace: Workspace) -> None:
    """(d) a project's test command under the bench sandbox: no network
    beyond loopback, no worker login file, no real-HOME file."""
    config = replace(workspace.config, sandbox="bwrap")
    attempt_id = "J-alpha-JAIL03#1"
    worker_login = (config.home / "jobs" / ids.attempt_dirname(attempt_id) / "worker-home"
                    / "build" / "builder" / ".claude" / ".credentials.json")
    worker_login.parent.mkdir(parents=True)
    worker_login.write_text("LOGIN-SECRET", encoding="utf-8")
    (workspace.alpha / "probe.py").write_text(_PROBE, encoding="utf-8")
    listener, port = _host_listener()
    try:
        rc, tail = run_in_bench_sandbox(
            config, "alpha", attempt_id, workspace.alpha,
            [PY, str(workspace.alpha / "probe.py"), str(worker_login),
             str(config.secrets_dir / "prod.key"), str(port)],
            {"PATH": os.environ["PATH"]}, 60)
    finally:
        listener.close()
    assert rc == 0, tail
    cmd = _parse("CMD", tail)
    assert cmd["login"] is False
    assert cmd["mcp"] is False  # secrets_dir file
    assert cmd["net"] is False
    assert cmd["ifaces"] == ["lo"]
    assert cmd["run"] == []


@_needs_bwrap
def test_killing_launcher_group_stops_command_in_second_layer(workspace: Workspace) -> None:
    """The engine stops a station by killing the launcher's process
    group; a command still running in layer B must die with it."""
    ctx, spec, copy, _login, _mcp = _builder_setup(workspace)
    marker = copy / "late-marker"
    stub = ("import os, subprocess; subprocess.run([os.environ['CLAUDE_CODE_SHELL_PREFIX'], "
            f"'sleep 4; touch {marker}'])")
    argv = worker_sandbox.sandboxed_argv(
        ctx, spec, [PY, "-c", stub], {"PATH": os.environ["PATH"]}, _StubClaudeRunner())
    fds = sandbox.pass_fds_for(argv)
    try:
        proc = subprocess.Popen(argv, pass_fds=fds, env=sandbox.launcher_env(),
                                cwd=str(copy), start_new_session=True)
    finally:
        sandbox.close_fds(fds)
    time.sleep(2)
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(10)
    time.sleep(4)
    assert not marker.exists()


_GATE_CLI = textwrap.dedent("""\
    import os, subprocess, sys
    hook = sys.stdin.read()
    gate = sys.argv[1]
    done = subprocess.run([os.environ["CLAUDE_CODE_SHELL_PREFIX"], gate], input=hook,
                          capture_output=True, text=True)
    print("GATE_RC", done.returncode)
    print("GATE_OUT", done.stdout.strip().splitlines()[-1] if done.stdout.strip() else "")
    sys.stderr.write(done.stderr)
    """)


@_needs_bwrap
def test_gate_hook_runs_in_cli_layer_not_command_layer(workspace: Workspace) -> None:
    """The PreToolUse gate is a hook command, so the CLI hands it to the
    shell prefix too; the client recognizes it verbatim and runs it in
    the CLI layer (out of reach of the worker's commands), where it loads
    its facts/config from gate-ro and appends to the decision log."""
    ctx, spec, copy, _login, _mcp = _builder_setup(workspace)
    assert spec.gate_facts_path is not None and spec.gate_log_path is not None
    gate_ro = spec.gate_facts_path.parent
    config_copy = gate_ro / "engine_config.json"
    config_copy.write_text(json.dumps(to_json_dict(workspace.config)), encoding="utf-8")
    spec.gate_facts_path.write_text(json.dumps({
        "project": "alpha", "registered": True, "state": "OPEN", "generation": 1,
        "attempt_id": spec.attempt_id, "attempt_active": True,
    }), encoding="utf-8")
    spec = replace(spec, gate_config_path=config_copy)
    (copy / "gate_cli.py").write_text(_GATE_CLI, encoding="utf-8")
    env = {**gate_env(spec), "PATH": os.environ["PATH"],
           "PYTHONPATH": str(worker_sandbox.ENGINE_ROOT)}
    argv = worker_sandbox.sandboxed_argv(
        ctx, spec, [PY, str(copy / "gate_cli.py"), _gate_command()], env, _StubClaudeRunner())
    hook = json.dumps({"tool_name": "Read", "cwd": str(copy),
                       "tool_input": {"file_path": str(copy / "fake_cli.py")}})
    result = _run(argv, cwd=str(copy), input=hook)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "GATE_RC 0" in result.stdout, result.stdout + result.stderr
    decision = json.loads(result.stdout.split("GATE_OUT ", 1)[1].splitlines()[0])
    assert decision["hookSpecificOutput"]["permissionDecision"] == "allow", decision
    assert spec.gate_log_path.read_text(encoding="utf-8").strip()
