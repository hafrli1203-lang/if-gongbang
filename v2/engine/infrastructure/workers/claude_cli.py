"""Claude CLI builder runner. Every tool call is checked by engine.interface.hook_gate
(a PreToolUse hook), not by the operator's own settings/hooks/CLAUDE.md
-- `--setting-sources ""` means those are never loaded (confirmed by
experiment, plan §0)."""
from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

from engine.config import McpServerConfig, WorkerConfig
from engine.infrastructure.workers.base import (
    WorkerResult, WorkerSpec, parse_cli_shaped_result, worker_env,
)

_BASE_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep", "Bash")

_SYSTEM_PROMPT = (
    "You are an automated worker inside a sandboxed copy of a repository. "
    "Every tool call you make is checked by an external policy gate: "
    "actions outside your allowed write paths or terminal patterns are "
    "denied before they run. Finish the task and reply with exactly one "
    "final JSON object as instructed in the prompt, and nothing else "
    "after it."
)


GATE_HARD_TIMEOUT_SEC = 20  # < hook timeout (30), > gate watchdog (10)


def _gate_command() -> str:
    # -P (isolated/safe path): keeps the gate's cwd and script directory
    # off sys.path, so `import engine.interface.hook_gate` resolves only through
    # PYTHONPATH (the trusted engine root, set by engine.infrastructure.workers.base's
    # worker_env) -- never through a worker copy's own cwd, which could
    # otherwise shadow it with a copy-planted `engine/interface/hook_gate.py` that
    # always allows. PYTHONPATH entries are still honored under -P.
    #
    # `|| exit 2`, inside an `sh -c` wrapper rather than a bare argv:
    # measured directly against this CLI, a PreToolUse hook that exits
    # 1 or times out lets the tool PROCEED (fails OPEN); only exit 2
    # blocks it. `"onFailure": "block"` is NOT honoured by this CLI
    # version, so it buys nothing and is deliberately left off. engine.
    # gate's own main() already exits 2 on any internal exception or
    # watchdog timeout; this wrapper additionally covers a failure
    # BEFORE that code even runs (missing interpreter, import error,
    # the python process itself failing to start) with the same exit 2.
    # `timeout -s KILL 20`: the gate's own 10s SIGALRM watchdog only arms
    # once main() runs; an interpreter/import stall BEFORE that would
    # reach Claude's 30s hook timeout and fail open. coreutils timeout
    # kills it at 20s -> non-zero -> `|| exit 2` -> blocked.
    gate_cmd = f"timeout -s KILL {GATE_HARD_TIMEOUT_SEC} {sys.executable} -P -m engine.gate"
    return f"sh -c {shlex.quote(f'{gate_cmd} || exit 2')}"


def _mcp_config_json(mcp_servers: dict[str, McpServerConfig]) -> str:
    """The `--mcp-config` JSON for the stdio servers named in
    `mcp_servers`: the documented Claude Code mcpServers map shape
    (command/args/env), never a raw byte copy of anything -- built
    fresh from each McpServerConfig. `env` carries only the configured
    env_passthrough NAMES, each resolved from THIS (engine) process's
    own os.environ -- never a worker's own env (worker_env()'s
    deliberately tiny GB_*/passthrough set), and never anything not
    explicitly named by the project's own config.

    Unverified against a real `claude` CLI probe log (MASTER_RULES
    §실측): `--mcp-config`/`--strict-mcp-config` are the flag names the
    plan this was built from specifies; this module keeps the
    separately-measured `--tools` flag (see module docstring) rather
    than the plan's own `--allowedTools` wording, since `--tools` is
    what ENGINE.md records as actually measured against this CLI."""
    servers: dict[str, object] = {}
    for name, cfg in sorted(mcp_servers.items()):
        command, *extra_args = cfg.command
        entry: dict[str, object] = {
            "command": command, "args": list(extra_args) + list(cfg.args),
        }
        env = {var: os.environ[var] for var in cfg.env_passthrough if var in os.environ}
        if env:
            entry["env"] = env
        servers[name] = entry
    return json.dumps({"mcpServers": servers})


def _mcp_config_dir(spec: WorkerSpec) -> Path:
    """The engine-only, per-request gate-ro directory (engine.application.steps.
    station._gate_ro_dir) -- never spec.result_path's own (worker-
    writable) stations directory, which would let a worker (via any
    code it runs, not just its own gated tool calls -- e.g. a
    project's own test suite, once an allowed Bash command invokes it)
    read the mcp_servers config's embedded env_passthrough secrets
    directly off disk. This directory is created by engine.application.steps.
    station._write_gate_facts BEFORE ClaudeRunner.argv() is ever
    called (see _run_spawn_worker's own ordering) and is already
    mounted read-only into the worker's sandbox as a WHOLE directory
    (station._sandboxed_argv: `readonly += (spec.gate_facts_path.
    parent,)`), so writing this file into it needs no separate sandbox
    mount of its own -- the `claude` CLI can still read it from inside
    the sandbox via that existing bind.

    Raises ValueError if spec.gate_facts_path is None: ClaudeRunner's
    own `needs_gate = True` means every real engine.application.steps.worker_step call
    site sets this, but a caller building a WorkerSpec some other way
    (e.g. engine.application.steps.review, which never sets gate_facts_path at
    all) has no engine-only directory the sandbox already exposes
    read-only to fall back to -- silently reusing spec.result_path's
    directory instead would reintroduce exactly the exposure this
    function exists to avoid, so this fails closed rather than doing
    that."""
    if spec.gate_facts_path is None:
        raise ValueError(
            "mcp_servers requires spec.gate_facts_path (an engine-only, "
            "read-only-mounted directory) -- refusing to fall back to the "
            "worker-writable stations directory"
        )
    return spec.gate_facts_path.parent


def _mcp_config_path(spec: WorkerSpec) -> Path:
    return _mcp_config_dir(spec) / "mcp-config.json"


def _write_mcp_config_file(spec: WorkerSpec, mcp_servers: dict[str, McpServerConfig]) -> Path:
    """Writes `_mcp_config_json(mcp_servers)` to a 0600 file and returns
    its path -- `--mcp-config` is then given that PATH, never the JSON
    (and the env_passthrough secrets it can carry) directly on argv,
    which any local user can read off this process's own command line
    (e.g. /proc/<pid>/cmdline) for as long as it runs. O_NOFOLLOW/
    fchmod close the gap between creating the file and locking its
    mode down -- an attacker-planted symlink at this path is refused
    outright rather than written through, and the mode is set on the
    open file descriptor itself before any other process could observe
    a wider one. Lives in _mcp_config_dir(spec) (the engine-only
    gate-ro directory), not spec.result_path's own worker-writable
    stations directory -- see that function's docstring."""
    path = _mcp_config_path(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _mcp_config_json(mcp_servers).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
    finally:
        os.close(fd)
    return path


def _gate_settings(worker: WorkerConfig) -> str:
    """The single --settings JSON this runner passes (never a second
    flag -- the CLI only reads the last --settings it is given).
    `effortLevel` is added only when worker.reasoning is set, so a
    worker with none keeps today's settings JSON byte-identical
    (tested by test_claude_argv_settings_has_no_effort_level_when_worker_has_none).
    Key name and accepted values (low/medium/high/xhigh) are from
    code.claude.com/docs/en/settings-reference#effortlevel (confirmed
   ; `claude --help` additionally accepts "max" for its
    per-session `--effort` flag, so any of worker.reasoning's values is
    passed through verbatim rather than validated against a narrower
    list here."""
    settings: dict[str, object] = {
        "hooks": {
            "PreToolUse": [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": _gate_command(),
                    "timeout": 30,
                }],
            }],
        },
    }
    if worker.reasoning is not None:
        settings["effortLevel"] = worker.reasoning
    return json.dumps(settings)


class ClaudeRunner:
    needs_gate = True
    read_only = False
    # The real `claude` CLI writes its own session/credential state under
    # these -- the only HOME subpaths this runner needs writable (ENGINE.md
    # 3단계 OS 샌드박스 HOME 절).
    home_writable_subpaths = (".claude", ".claude.json")
    # The task prompt reaches this CLI over stdin, never as an argv
    # element -- a real production prompt measured at 169,674 bytes
    # (performance-ads job, exceeded Linux's MAX_ARG_STRLEN
    # (131,072 bytes) and made exec itself fail with E2BIG, surfaced only
    # as the opaque "internal:OSError" engine.application.processor's own
    # catch-all records. `claude -p` with NO prompt argument reads the
    # prompt from stdin (the caller -- engine.application.steps.worker_step/
    # review -- writes spec.task to a file via engine.infrastructure.procs.
    # write_stdin_file and passes it as procs.spawn's stdin_path, never
    # stdin_devnull).
    prompt_via_stdin = True

    def argv(self, spec: WorkerSpec, worker: WorkerConfig) -> list[str]:
        argv = ["claude", "-p"]
        if worker.model:
            argv += ["--model", worker.model]
        if worker.max_turns is not None:
            argv += ["--max-turns", str(worker.max_turns)]
        if worker.budget_usd is not None:
            argv += ["--max-budget-usd", str(worker.budget_usd)]
        tools = ",".join(_BASE_TOOLS)
        if spec.mcp_servers:
            tools += "," + ",".join(f"mcp__{name}__*" for name in sorted(spec.mcp_servers))
        argv += [
            "--output-format", "json",
            "--no-session-persistence",
            "--setting-sources", "",
            "--settings", _gate_settings(worker),
            "--tools", tools,
            "--permission-mode", "manual",
            "--system-prompt", _SYSTEM_PROMPT,
        ]
        if spec.mcp_servers:
            # --strict-mcp-config: only the servers in this generated
            # config are available -- no other .mcp.json/project/user
            # config source is consulted (same "nothing but what we
            # just built" intent as --setting-sources "" above). The
            # config itself (which can carry env_passthrough secrets)
            # is written to a 0600 file and only ITS PATH ever reaches
            # argv -- see _write_mcp_config_file.
            mcp_config_path = _write_mcp_config_file(spec, spec.mcp_servers)
            argv += ["--mcp-config", str(mcp_config_path), "--strict-mcp-config"]
        return argv

    def env(self, spec: WorkerSpec, worker: WorkerConfig) -> dict[str, str]:
        return worker_env(spec)

    def parse_result(self, stdout_path: Path, spec: WorkerSpec) -> WorkerResult:
        return parse_cli_shaped_result(stdout_path)
