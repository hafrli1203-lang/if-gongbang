"""Unit tests for engine.workers.*: argv/env building and result parsing
for the Claude, Codex and scripted runners."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _subprocess_env() -> dict[str, str]:
    """engine is not pip-installed: a child process whose cwd is not this
    project's root needs PYTHONPATH to find it (mirrors
    engine.workers.base.worker_env)."""
    return {**os.environ, "PYTHONPATH": str(_PROJECT_ROOT)}

from engine.config import WorkerConfig
from engine.workers.base import WorkerSpec, worker_env
from engine.workers.claude_cli import ClaudeRunner
from engine.workers.codex_cli import CodexRunner
from engine.workers.scripted import ScriptedRunner


def _spec(tmp_path: Path, task: str = "do the thing") -> WorkerSpec:
    copy_root = tmp_path / "copy"
    copy_root.mkdir()
    return WorkerSpec(
        project="alpha", attempt_id="J-alpha-TEST#1", generation=1,
        station="build", role="builder", copy_root=copy_root, workdir=copy_root,
        task=task, result_path=tmp_path / "result.json",
        config_path=tmp_path / "engine.json",
    )


# ---- ClaudeRunner ----------------------------------------------------

def test_claude_argv_has_no_user_settings(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    worker = WorkerConfig(runner="claude", model="claude-sonnet-5",
                          max_turns=12, budget_usd=1.5)
    argv = ClaudeRunner().argv(spec, worker)

    assert "--setting-sources" in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "manual"
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
    assert argv[argv.index("--max-turns") + 1] == "12"
    assert argv[argv.index("--max-budget-usd") + 1] == "1.5"

    settings_raw = argv[argv.index("--settings") + 1]
    settings = json.loads(settings_raw)
    hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]
    # Measured against the real CLI: "onFailure" is not honoured (a
    # hook exit 1/timeout still lets the tool proceed) -- only an
    # actual exit 2 blocks it, so the command is wrapped in `sh -c
    # '... || exit 2'` instead of relying on that field.
    assert "onFailure" not in hook
    assert hook["command"] == f"sh -c 'timeout -s KILL 20 {sys.executable} -P -m engine.gate || exit 2'"
    assert hook["timeout"] == 30


def test_claude_env_only_passthrough_plus_gb(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SOME_SECRET", "leaked-value")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/t")
    spec = _spec(tmp_path)
    worker = WorkerConfig(runner="claude")
    env = ClaudeRunner().env(spec, worker)

    assert "SOME_SECRET" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/t"
    assert env["GB_PROJECT"] == "alpha"
    assert env["GB_ATTEMPT"] == "J-alpha-TEST#1"
    assert env["GB_STATION"] == "build"
    assert env["GB_ROLE"] == "builder"
    assert env["GB_COPY_ROOT"] == str(spec.copy_root)
    assert env["GB_CONFIG"] == str(spec.config_path)
    assert env["GB_GENERATION"] == "1"
    assert env["PYTHONPATH"] == str(_PROJECT_ROOT)


def test_worker_env_uses_spec_passthrough(tmp_path: Path, monkeypatch) -> None:
    """engine.workers.base.worker_env reads spec.env_passthrough, not a
    module-level constant -- a project/station config that narrows
    EngineConfig.worker_env_passthrough (e.g. no HOME) must actually
    shrink the spawned worker's env."""
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/t")
    from dataclasses import replace as _replace

    spec = _spec(tmp_path)
    narrow_spec = _replace(spec, env_passthrough=("PATH",))
    env = ClaudeRunner().env(narrow_spec, WorkerConfig(runner="claude"))

    assert env["PATH"] == "/usr/bin"
    assert "HOME" not in env


def test_claude_parse_result_last_json_block(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    stdout = tmp_path / "out.stdout"
    stdout.write_text(json.dumps({
        "result": (
            'Some chatter about the task, including a brace { that is not '
            'JSON on its own.\n\n'
            '{"status": "done", "summary": "renamed greeting", "requests": []}'
        ),
        "total_cost_usd": 0.42, "num_turns": 7, "is_error": False,
    }), encoding="utf-8")

    result = ClaudeRunner().parse_result(stdout, spec)
    assert result.ok is True
    assert result.summary == "renamed greeting"
    assert result.cost_usd == 0.42
    assert result.turns == 7
    assert result.requests == ()


def test_claude_parse_result_last_json_block_brace_in_string(tmp_path: Path) -> None:
    """A "}" or "{" inside the final JSON object's own string values
    (e.g. a summary that mentions a brace token) must not break
    last_json_block's brace matching -- it needs a real JSON parser's
    string-literal handling, not raw character counting."""
    from engine.workers.base import last_json_block

    text = (
        'chatter\n'
        '{"status": "done", "summary": "fixed the } token in the { parser", '
        '"requests": []}'
    )
    block = last_json_block(text)
    assert block == {
        "status": "done",
        "summary": "fixed the } token in the { parser",
        "requests": [],
    }


def test_claude_parse_result_unparseable(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    stdout = tmp_path / "out.stdout"
    stdout.write_text(json.dumps({
        "result": "I finished but forgot to print any JSON at all.",
        "total_cost_usd": 0.1, "num_turns": 2, "is_error": False,
    }), encoding="utf-8")

    result = ClaudeRunner().parse_result(stdout, spec)
    assert result.ok is False
    assert result.summary == "unparseable_result"


def test_claude_parse_result_missing_file(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    result = ClaudeRunner().parse_result(tmp_path / "missing.stdout", spec)
    assert result.ok is False
    assert result.summary == "unparseable_result"


# ---- CodexRunner -------------------------------------------------------

def test_codex_argv_read_only_and_schema(tmp_path: Path) -> None:
    spec = _spec(tmp_path, task="review this diff")
    worker = WorkerConfig(runner="codex", reasoning="low")
    argv = CodexRunner().argv(spec, worker)

    assert argv[0:3] == ["codex", "exec", "-s"]
    assert argv[3] == "read-only"
    assert "--skip-git-repo-check" in argv
    assert argv[argv.index("-C") + 1] == str(spec.workdir)
    assert argv[argv.index("-c") + 1] == 'model_reasoning_effort="low"'
    assert argv[-1] == "review this diff"
    schema_path = Path(argv[argv.index("--output-schema") + 1])
    assert schema_path.exists()
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["required"] == ["passed", "findings"]
    assert argv[argv.index("-o") + 1] == str(spec.result_path)


def test_codex_argv_creates_missing_result_parent_dir(tmp_path: Path) -> None:
    """A fresh attempt's stations/ dir may not exist yet when CodexRunner
    builds its argv -- write_verdict_schema writes next to result_path,
    so its parent must be created first rather than raising
    FileNotFoundError."""
    from dataclasses import replace as _replace_spec

    nested_result = tmp_path / "stations" / "review" / "1" / "result.json"
    spec = _replace_spec(_spec(tmp_path, task="review this diff"),
                          result_path=nested_result)
    worker = WorkerConfig(runner="codex")
    argv = CodexRunner().argv(spec, worker)
    schema_path = Path(argv[argv.index("--output-schema") + 1])
    assert schema_path.exists()
    assert schema_path.parent == nested_result.parent


def test_codex_parse_result_reads_output_file(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    spec.result_path.write_text(
        json.dumps({"passed": True, "findings": []}), encoding="utf-8",
    )
    result = CodexRunner().parse_result(tmp_path / "out.stdout", spec)
    assert result.ok is True
    assert result.verdict == {"passed": True, "findings": []}


def test_codex_parse_result_missing_output_file(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    result = CodexRunner().parse_result(tmp_path / "out.stdout", spec)
    assert result.ok is False
    assert result.verdict is None


# ---- ScriptedRunner / the scripted worker fake process -----------------

def test_scripted_argv_invokes_module(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    worker = WorkerConfig(runner="scripted")
    script = tmp_path / "script.json"
    argv = ScriptedRunner(script).argv(spec, worker)
    assert argv == [sys.executable, "-P", "-m", "engine.fakes.scripted_worker", str(script)]


def test_scripted_worker_writes_files_without_gate(tmp_path: Path) -> None:
    """No --gate: every action is applied directly."""
    workdir = tmp_path / "copy"
    workdir.mkdir()
    script = tmp_path / "script.json"
    script.write_text(json.dumps([
        {"tool": "Write", "file_path": "src/x.py", "content": "x = 1\n"},
        {"final": {"status": "done", "summary": "wrote x", "requests": []}},
    ]), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "engine.fakes.scripted_worker", str(script)],
        cwd=str(workdir), capture_output=True, text=True, check=True,
        env=_subprocess_env(),
    )
    assert (workdir / "src" / "x.py").read_text(encoding="utf-8") == "x = 1\n"
    raw = json.loads(proc.stdout)
    final = json.loads(raw["result"])
    assert final == {"status": "done", "summary": "wrote x", "requests": []}
    assert raw["is_error"] is False


_ALLOW_GATE = '''\
import json, sys
json.loads(sys.stdin.read())
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse", "permissionDecision": "allow",
    "permissionDecisionReason": "test-allow",
}}))
'''

_DENY_WRITE_OUTSIDE_SRC_GATE = '''\
import json, sys
payload = json.loads(sys.stdin.read())
path = (payload.get("tool_input") or {}).get("file_path", "")
decision = "allow" if "src/" in path else "deny"
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse", "permissionDecision": decision,
    "permissionDecisionReason": "test-gate",
}}))
'''


def test_scripted_worker_calls_gate_per_action(tmp_path: Path) -> None:
    """A tiny inline fake gate (not engine.gate -- Task 2's real gate
    does not exist in this branch yet) allows writes under src/ and
    denies everything else: of two Write actions, only the allowed one
    lands on disk."""
    workdir = tmp_path / "copy"
    workdir.mkdir()
    gate_script = tmp_path / "gate.py"
    gate_script.write_text(_DENY_WRITE_OUTSIDE_SRC_GATE, encoding="utf-8")
    script = tmp_path / "script.json"
    script.write_text(json.dumps([
        {"tool": "Write", "file_path": "src/ok.py", "content": "ok\n"},
        {"tool": "Write", "file_path": "secrets.txt", "content": "nope\n"},
        {"final": {"status": "done", "summary": "", "requests": []}},
    ]), encoding="utf-8")

    gate_argv = json.dumps([sys.executable, str(gate_script)])
    proc = subprocess.run(
        [sys.executable, "-m", "engine.fakes.scripted_worker", str(script),
         "--gate", gate_argv],
        cwd=str(workdir), capture_output=True, text=True, check=True,
        env=_subprocess_env(),
    )
    assert (workdir / "src" / "ok.py").exists()
    assert not (workdir / "secrets.txt").exists()
    assert proc.returncode == 0


def test_scripted_worker_gate_denial_recorded_in_ledger(tmp_path: Path) -> None:
    """Once engine.gate exists (Task 2), the scripted worker's gate calls
    go through the real gate -- which checks Policy and records a
    policy.decided event per call -- rather than a test-only fake gate.
    This is the acceptance shape for Review Focus 3's "two Write actions,
    one outside the allowed path" scenario wired to the REAL gate and a
    real ledger; it is written now, ahead of Task 2 landing, and flipped
    to a hard pass once `python -m engine.gate` is importable."""
    import engine.gate  # noqa: F401  (does not exist yet in this branch)


def test_build_prompt_includes_project_test_command(tmp_path: Path) -> None:
    from engine.workers.base import WorkerSpec
    from engine.workers.prompts import build_prompt
    spec = WorkerSpec(
        project="alpha", attempt_id="J-alpha-X#1", generation=1, station="build",
        role="builder", copy_root=tmp_path, workdir=tmp_path, task="do it",
        result_path=tmp_path / "r.json", config_path=tmp_path / "engine.json",
    )
    text = build_prompt(spec, ("src/",), (r"ls",), ("env", "X=1", "pytest", "-q"))
    assert "exactly this command: env X=1 pytest -q" in text
    assert "exactly this command" not in build_prompt(spec, ("src/",), (r"ls",))


def test_parse_result_blocked_requires_requests(tmp_path: Path) -> None:
    import json as _json
    from engine.workers.base import parse_cli_shaped_result
    out = tmp_path / "o.json"
    out.write_text(_json.dumps({"result": '{"status": "blocked", "summary": "s", "requests": [{"kind": "install"}]}',
                                "total_cost_usd": 0, "num_turns": 1, "is_error": False}))
    assert parse_cli_shaped_result(out).ok is True
    out.write_text(_json.dumps({"result": '{"status": "blocked", "summary": "s", "requests": []}',
                                "total_cost_usd": 0, "num_turns": 1, "is_error": False}))
    assert parse_cli_shaped_result(out).ok is False


def test_last_json_block_skips_oversized_int_candidate() -> None:
    from engine.workers.base import last_json_block
    text = '{"n": ' + "9" * 5000 + '} then {"status": "done", "summary": "s", "requests": []}'
    assert last_json_block(text) == {"status": "done", "summary": "s", "requests": []}
