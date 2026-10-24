"""Deterministic fake "AI" worker process, run as
`python -m engine.fakes.scripted_worker <script.json> [--gate <json argv>]`.

Reads a JSON list of actions from `script.json` and performs them in
order:
  {"tool": "Write", "file_path": str, "content": str}
  {"tool": "Bash", "command": str}
  {"tool": "Spawn", "command": str}   -- a detached child the test uses
                                          to check that close_station
                                          reaches a worker's grandchild
                                          too (Review Focus 1). Started
                                          WITHOUT start_new_session, so
                                          it stays in this process's own
                                          group.
  {"sleep": seconds}
  {"exit": int}                       -- exit immediately with this code,
                                          WITHOUT printing the final JSON
                                          (used to test a worker process
                                          that genuinely fails)
  {"final": {...}}                    -- the status JSON to report

With --gate (a JSON-encoded argv list), every Write/Bash action is first
sent to that argv as a Claude PreToolUse hook would be (stdin JSON
{"tool_name", "tool_input", "cwd"}, env unchanged -- the GB_* vars this
process was itself spawned with), and only applied if the gate's
decision is "allow". Without --gate, every action is applied directly
(used by tests that are not exercising gate-mediated denial).

On exit, prints the same JSON shape ClaudeRunner.parse_result expects:
{"result": <the final status dict, JSON-encoded as a string>,
 "total_cost_usd": 0, "num_turns": <action count>, "is_error": false}.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path


def _resolve(cwd: str, file_path: str) -> Path:
    path = Path(file_path)
    return path if path.is_absolute() else Path(cwd) / path


def _consult_gate(gate_argv: tuple[str, ...], tool_name: str,
                   tool_input: dict[str, object], cwd: str) -> bool:
    payload = {"tool_name": tool_name, "tool_input": tool_input, "cwd": cwd}
    try:
        proc = subprocess.run(
            list(gate_argv), input=json.dumps(payload), capture_output=True,
            text=True, timeout=30,
        )
        out = json.loads(proc.stdout)
        decision = out.get("hookSpecificOutput", {}).get("permissionDecision")
        return decision == "allow"
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError, AttributeError):
        return False


def _do_write(gate_argv: tuple[str, ...] | None, item: dict[str, object], cwd: str) -> None:
    file_path = str(item["file_path"])
    content = str(item.get("content", ""))
    allowed = True
    if gate_argv is not None:
        allowed = _consult_gate(gate_argv, "Write", {"file_path": file_path}, cwd)
    if not allowed:
        return
    target = _resolve(cwd, file_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _do_bash(gate_argv: tuple[str, ...] | None, item: dict[str, object], cwd: str) -> None:
    command = str(item["command"])
    allowed = True
    if gate_argv is not None:
        allowed = _consult_gate(gate_argv, "Bash", {"command": command}, cwd)
    if not allowed:
        return
    subprocess.run(command, shell=True, cwd=cwd, capture_output=True)


def _do_spawn(item: dict[str, object], cwd: str) -> None:
    command = str(item["command"])
    # No start_new_session: the child stays in THIS process's group, the
    # same group procs.close_station(...) signals for the station this
    # worker was spawned under (Review Focus 1).
    subprocess.Popen(shlex.split(command), cwd=cwd)


def run(argv: list[str]) -> int:
    script_path = Path(argv[0])
    gate_argv: tuple[str, ...] | None = None
    if "--gate" in argv:
        idx = argv.index("--gate")
        gate_argv = tuple(json.loads(argv[idx + 1]))

    items_raw = json.loads(script_path.read_text(encoding="utf-8"))
    items = items_raw if isinstance(items_raw, list) else []
    final: dict[str, object] = {"status": "done", "summary": "", "requests": []}
    cwd = os.getcwd()

    for item in items:
        if not isinstance(item, dict):
            continue
        if "exit" in item:
            return int(item["exit"])
        if "sleep" in item:
            time.sleep(float(item["sleep"]))
        elif item.get("tool") == "Write":
            _do_write(gate_argv, item, cwd)
        elif item.get("tool") == "Bash":
            _do_bash(gate_argv, item, cwd)
        elif item.get("tool") == "Spawn":
            _do_spawn(item, cwd)
        elif "final" in item:
            final = item["final"]

    print(json.dumps({
        "result": json.dumps(final), "total_cost_usd": 0,
        "num_turns": len(items), "is_error": False,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
