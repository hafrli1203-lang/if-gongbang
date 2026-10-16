import json
import os
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS_DIR = os.path.join(ROOT, "hooks")
for sub in ("hooks", "metrics", "ops"):
    path = os.path.join(ROOT, sub)
    if path not in sys.path:
        sys.path.insert(0, path)

APPROVE_GATE_SCRIPT = os.path.join(HOOKS_DIR, "approve_gate.py")
SCOPE_GUARD_SCRIPT = os.path.join(HOOKS_DIR, "scope_guard.py")


class FenceHome:
    """Test harness for approve_gate.py/scope_guard.py: a throwaway $HOME containing a fake
    chat transcript at <home>/.claude/projects/x/<session_id>.jsonl. Running the real hook
    scripts as subprocesses with HOME overridden to this directory makes their default config
    (installed_hooks_dir="~/.claude/hooks", transcripts_root="~/.claude/projects",
    legacy_gate_dir="~/.claude/gate") resolve entirely inside the throwaway home - no
    gongbang.config.json needed for the common case."""

    def __init__(self, home: str):
        self.home = home
        self.proj_dir = os.path.join(home, ".claude", "projects", "x")
        os.makedirs(self.proj_dir, exist_ok=True)

    def transcript_path(self, session_id: str) -> str:
        return os.path.join(self.proj_dir, f"{session_id}.jsonl")

    def write_transcript(self, session_id: str, lines: list) -> None:
        path = self.transcript_path(session_id)
        with open(path, "w", encoding="utf-8") as f:
            for obj in lines:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    @staticmethod
    def iso(ts: float) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + ".000Z"

    def human_line(self, ts: float, text: str, **overrides) -> dict:
        obj = {"type": "user", "timestamp": self.iso(ts), "origin": {"kind": "human"},
               "message": {"role": "user", "content": text}}
        obj.update(overrides)
        return obj

    def peer_line(self, ts: float, text: str) -> dict:
        # A subagent report relayed into the main session: a "user" line whose origin kind is "peer".
        return self.human_line(ts, "Another Claude session sent a message:\n" + text,
                               origin={"kind": "peer", "from": "agent-x"})

    def tool_result_line(self, ts: float, text: str) -> dict:
        return {
            "type": "user", "timestamp": self.iso(ts),
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": text, "is_error": False}
            ]},
        }

    def system_wrapper_line(self, ts: float, text: str) -> dict:
        return self.human_line(ts, f"<system-reminder>{text}</system-reminder>")

    def queued_command_line(self, ts: float, prompt: str) -> dict:
        return {
            "type": "attachment", "timestamp": self.iso(ts),
            "attachment": {"type": "queued_command", "commandMode": "prompt",
                            "origin": {"kind": "human"}, "prompt": prompt},
        }

    def run(self, script: str, payload: dict, now: float | None = None, extra_env: dict | None = None):
        env = dict(os.environ)
        env["HOME"] = self.home
        env.pop("GONGBANG_CONFIG", None)  # the fake HOME's defaults are what we want to test
        if now is not None:
            env["FENCE_NOW"] = repr(now)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, script], input=json.dumps(payload), env=env,
            capture_output=True, text=True, timeout=15,
        )

    def bash(self, command: str, session_id: str, now: float | None = None, cwd: str = ""):
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "session_id": session_id, "cwd": cwd}
        return self.run(SCOPE_GUARD_SCRIPT, payload, now=now)

    def edit(self, file_path: str, session_id: str, **extra):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": file_path, **extra}, "session_id": session_id}
        return self.run(SCOPE_GUARD_SCRIPT, payload)

    def multiedit(self, file_path: str, edits: list, session_id: str):
        payload = {"tool_name": "MultiEdit", "tool_input": {"file_path": file_path, "edits": edits}, "session_id": session_id}
        return self.run(SCOPE_GUARD_SCRIPT, payload)

    def approve(self, prompt: str, session_id: str, now: float | None = None):
        payload = {"prompt": prompt, "session_id": session_id}
        return self.run(APPROVE_GATE_SCRIPT, payload, now=now)


def classify(result: subprocess.CompletedProcess) -> str:
    """ALLOW / DENY / ERROR. Non-zero exit -> ERROR. Empty stdout -> ALLOW. Non-empty stdout
    that isn't exactly a PreToolUse deny JSON shape -> ERROR, never ALLOW (an unexpected hook
    output is a bug, not a pass)."""
    if result.returncode != 0:
        return "ERROR"
    out = result.stdout.strip()
    if not out:
        return "ALLOW"
    try:
        data = json.loads(out)
    except Exception:
        return "ERROR"
    hook_out = data.get("hookSpecificOutput")
    if not isinstance(hook_out, dict):
        return "ERROR"
    if hook_out.get("hookEventName") != "PreToolUse":
        return "ERROR"
    return "DENY" if hook_out.get("permissionDecision") == "deny" else "ERROR"


@pytest.fixture
def fence_home(tmp_path):
    return FenceHome(str(tmp_path / "home"))
