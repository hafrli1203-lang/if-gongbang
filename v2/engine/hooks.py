"""Run a policy hook script as a subprocess, fail closed.

Protocol: JSON payload on stdin; the hook must exit 0 and print exactly one
JSON object {"decision": "allow"|"deny", "reason": str} on stdout.
Timeout, non-zero exit, non-JSON stdout, missing/invalid "decision" ->
Decision(False, reason).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from engine.policy import Decision


def run(script: Path, payload: dict[str, object], timeout_sec: float) -> Decision:
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return Decision(False, "hook_timeout")

    if result.returncode != 0:
        return Decision(False, f"hook_nonzero_exit:{result.returncode}")

    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return Decision(False, "hook_stdout_not_json")

    if not isinstance(parsed, dict):
        return Decision(False, "hook_stdout_not_object")

    decision = parsed.get("decision")
    reason = str(parsed.get("reason", ""))
    if decision == "allow":
        return Decision(True, reason)
    if decision == "deny":
        return Decision(False, reason)
    return Decision(False, "hook_invalid_decision")
