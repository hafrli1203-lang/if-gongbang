"""Claude CLI builder runner. Every tool call is checked by engine.gate
(a PreToolUse hook), not by the operator's own settings/hooks/CLAUDE.md
-- `--setting-sources ""` means those are never loaded (confirmed by
experiment, plan §0)."""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

from engine.config import WorkerConfig
from engine.workers.base import (
    WorkerResult, WorkerSpec, parse_cli_shaped_result, worker_env,
)

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
    # off sys.path, so `import engine.gate` resolves only through
    # PYTHONPATH (the trusted engine root, set by engine.workers.base's
    # worker_env) -- never through a worker copy's own cwd, which could
    # otherwise shadow it with a copy-planted `engine/gate.py` that
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


def _gate_settings() -> str:
    return json.dumps({
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
    })


class ClaudeRunner:
    needs_gate = True
    read_only = False
    # The real `claude` CLI writes its own session/credential state under
    # these -- the only HOME subpaths this runner needs writable (ENGINE.md
    # 3단계 OS 샌드박스 HOME 절).
    home_writable_subpaths = (".claude", ".claude.json")

    def argv(self, spec: WorkerSpec, worker: WorkerConfig) -> list[str]:
        argv = ["claude", "-p", spec.task]
        if worker.model:
            argv += ["--model", worker.model]
        argv += [
            "--max-turns", str(worker.max_turns),
            "--max-budget-usd", str(worker.budget_usd),
            "--output-format", "json",
            "--no-session-persistence",
            "--setting-sources", "",
            "--settings", _gate_settings(),
            "--tools", "Read,Edit,Write,Glob,Grep,Bash",
            "--permission-mode", "manual",
            "--system-prompt", _SYSTEM_PROMPT,
        ]
        return argv

    def env(self, spec: WorkerSpec, worker: WorkerConfig) -> dict[str, str]:
        return worker_env(spec)

    def parse_result(self, stdout_path: Path, spec: WorkerSpec) -> WorkerResult:
        return parse_cli_shaped_result(stdout_path)
