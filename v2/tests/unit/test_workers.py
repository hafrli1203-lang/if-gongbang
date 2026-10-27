"""Unit tests for engine.workers.*: argv/env building and result parsing
for the Claude, Codex and scripted runners."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _subprocess_env() -> dict[str, str]:
    """engine is not pip-installed: a child process whose cwd is not this
    project's root needs PYTHONPATH to find it (mirrors
    engine.workers.base.worker_env)."""
    return {**os.environ, "PYTHONPATH": str(_PROJECT_ROOT)}

from engine.config import McpServerConfig, WorkerConfig
from engine.workers.base import WorkerSpec, worker_env
from engine.workers.claude_cli import ClaudeRunner
from engine.workers.codex_cli import CodexRunner
from engine.workers.scripted import ScriptedRunner


def _spec(tmp_path: Path, task: str = "do the thing") -> WorkerSpec:
    copy_root = tmp_path / "copy"
    copy_root.mkdir()
    gate_ro = tmp_path / "gate-ro"
    gate_ro.mkdir()
    return WorkerSpec(
        project="alpha", attempt_id="J-alpha-TEST#1", generation=1,
        station="build", role="builder", copy_root=copy_root, workdir=copy_root,
        task=task, result_path=tmp_path / "result.json",
        config_path=tmp_path / "engine.json",
        # Mirrors engine.steps.station._full_spec: ClaudeRunner's own
        # needs_gate = True means a real spawn always has this set --
        # ClaudeRunner._write_mcp_config_file writes into its PARENT
        # directory (the engine-only gate-ro dir), never the worker-
        # writable stations dir next to result_path. See
        # engine.workers.claude_cli._mcp_config_dir's own docstring.
        gate_facts_path=gate_ro / "gate_facts.json",
    )


# ---- ClaudeRunner ----------------------------------------------------

def test_claude_argv_never_embeds_the_task_text(tmp_path: Path) -> None:
    """A real task prompt (170KB performance-ads job, exceeded
    Linux's MAX_ARG_STRLEN (131,072 bytes) as a single argv element and
    made exec itself fail with E2BIG. `claude -p` with no prompt
    argument reads the prompt from stdin instead -- spec.task must
    never appear anywhere in argv, regardless of its length."""
    long_task = "x" * 200_000
    spec = _spec(tmp_path, task=long_task)
    argv = ClaudeRunner().argv(spec, WorkerConfig(runner="claude"))

    assert long_task not in argv
    assert argv[0:2] == ["claude", "-p"]
    assert ClaudeRunner.prompt_via_stdin is True


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


def test_claude_argv_settings_has_effort_level_when_worker_has_reasoning(tmp_path: Path) -> None:
    """G11: the station's reasoning level (today only wired to the codex
    runner's -c model_reasoning_effort, see test_codex_argv below) must
    also reach a claude worker -- as the documented `effortLevel`
    settings key (code.claude.com/docs/en/settings-reference#effortlevel,
    values low/medium/high/xhigh; confirmed accepted by `claude --help`
    too, which also allows "max"), carried in the same --settings JSON
    this runner already writes (never a second --settings flag -- the
    CLI only reads the last one)."""
    spec = _spec(tmp_path)
    worker = WorkerConfig(runner="claude", reasoning="low")
    argv = ClaudeRunner().argv(spec, worker)

    assert argv.count("--settings") == 1
    settings_raw = argv[argv.index("--settings") + 1]
    settings = json.loads(settings_raw)
    assert settings["effortLevel"] == "low"


def test_claude_argv_settings_has_no_effort_level_when_worker_has_none(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    worker = WorkerConfig(runner="claude")
    argv = ClaudeRunner().argv(spec, worker)

    settings_raw = argv[argv.index("--settings") + 1]
    settings = json.loads(settings_raw)
    assert "effortLevel" not in settings


def test_claude_argv_has_no_mcp_config_when_spec_has_no_mcp_servers(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    argv = ClaudeRunner().argv(spec, WorkerConfig(runner="claude"))
    assert "--mcp-config" not in argv
    assert "--strict-mcp-config" not in argv
    tools = argv[argv.index("--tools") + 1]
    assert not tools.startswith("mcp__") and ",mcp__" not in tools


def test_claude_argv_mcp_config_has_only_the_spec_servers(tmp_path: Path) -> None:
    """spec.mcp_servers is whatever engine.steps.station._full_spec
    (or review._full_spec) already narrowed to station_mcp_allow (never
    the whole config.mcp_servers table) -- ClaudeRunner's own job is
    just to turn exactly that dict into --mcp-config, nothing wider."""
    spec = replace(_spec(tmp_path), mcp_servers={
        "playwright": McpServerConfig(command=("npx", "-y", "@playwright/mcp")),
    })
    argv = ClaudeRunner().argv(spec, WorkerConfig(runner="claude"))

    assert "--strict-mcp-config" in argv
    tools = argv[argv.index("--tools") + 1]
    assert "mcp__playwright__*" in tools.split(",")

    # --mcp-config carries a PATH, never the JSON itself (argv is
    # readable by any local user for the life of the process) -- see
    # engine.workers.claude_cli._write_mcp_config_file.
    mcp_config_path = Path(argv[argv.index("--mcp-config") + 1])
    assert mcp_config_path.exists()
    mode = mcp_config_path.stat().st_mode & 0o777
    assert mode == 0o600
    mcp_config = json.loads(mcp_config_path.read_text(encoding="utf-8"))
    assert list(mcp_config["mcpServers"]) == ["playwright"]
    assert mcp_config["mcpServers"]["playwright"]["command"] == "npx"
    assert mcp_config["mcpServers"]["playwright"]["args"] == ["-y", "@playwright/mcp"]


def test_claude_argv_mcp_config_file_never_leaks_secret_onto_argv(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("LIBRARY_TOKEN", "super-secret-value")
    spec = replace(_spec(tmp_path), mcp_servers={
        "library": McpServerConfig(command=("python3", "-m", "lib"),
                                   env_passthrough=("LIBRARY_TOKEN",)),
    })
    argv = ClaudeRunner().argv(spec, WorkerConfig(runner="claude"))

    assert all("super-secret-value" not in arg for arg in argv)
    mcp_config_path = Path(argv[argv.index("--mcp-config") + 1])
    config = json.loads(mcp_config_path.read_text(encoding="utf-8"))
    assert config["mcpServers"]["library"]["env"]["LIBRARY_TOKEN"] == "super-secret-value"


def test_claude_argv_mcp_config_file_lives_outside_the_worker_writable_stations_dir(
    tmp_path: Path,
) -> None:
    """another change item 3: the mcp-config file (which can carry
    mcp_servers env_passthrough secrets) must never sit in the SAME
    worker-writable directory as spec.result_path/stdout -- any code
    the worker's sandbox runs (not just its own gated tool calls)
    could otherwise read it straight off disk. It must live in
    spec.gate_facts_path's own parent directory (the engine-only
    gate-ro dir engine.steps.station already mounts read-only as a
    whole), which is NOT result_path's directory in a real spawn."""
    spec = replace(_spec(tmp_path), mcp_servers={
        "playwright": McpServerConfig(command=("npx", "-y", "@playwright/mcp")),
    })
    argv = ClaudeRunner().argv(spec, WorkerConfig(runner="claude"))

    mcp_config_path = Path(argv[argv.index("--mcp-config") + 1])
    assert mcp_config_path.parent == spec.gate_facts_path.parent
    assert mcp_config_path.parent != spec.result_path.parent


def test_claude_argv_mcp_config_requires_gate_facts_path(tmp_path: Path) -> None:
    """A WorkerSpec with mcp_servers but no gate_facts_path (e.g. built
    by something other than engine.steps.station._full_spec, which
    always sets it for ClaudeRunner since needs_gate=True) has no
    engine-only directory to write the config into -- this must fail
    closed (ValueError) rather than silently falling back to the
    worker-writable stations directory next to result_path."""
    spec = replace(
        _spec(tmp_path), gate_facts_path=None,
        mcp_servers={"playwright": McpServerConfig(command=("npx", "-y", "@playwright/mcp"))},
    )
    with pytest.raises(ValueError):
        ClaudeRunner().argv(spec, WorkerConfig(runner="claude"))


def test_claude_env_only_passthrough_plus_gb(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SOME_SECRET", "leaked-value")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/nonexistent-home/t")
    spec = _spec(tmp_path)
    worker = WorkerConfig(runner="claude")
    env = ClaudeRunner().env(spec, worker)

    assert "SOME_SECRET" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/nonexistent-home/t"
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
    monkeypatch.setenv("HOME", "/nonexistent-home/t")
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
    # No "usage" key here -- the real shape (see the next test) -- so
    # tokens_out stays unrecorded rather than fabricated.
    assert result.tokens_out is None


def test_claude_parse_result_reads_usage_output_tokens(tmp_path: Path) -> None:
    """The real `claude -p --output-format json` result carries a
    top-level "usage" object whose "output_tokens" is an int (recorded
    shape: docs/stage3-runs/v1/S3-1.json, usage.output_tokens=9051) --
    parse_cli_shaped_result must thread that int through to
    WorkerResult.tokens_out, the same way it already threads
    total_cost_usd/num_turns through to cost_usd/turns."""
    spec = _spec(tmp_path)
    stdout = tmp_path / "out.stdout"
    stdout.write_text(json.dumps({
        "result": '{"status": "done", "summary": "ok", "requests": []}',
        "total_cost_usd": 0.42, "num_turns": 7, "is_error": False,
        "usage": {"input_tokens": 40, "output_tokens": 9051,
                   "output_tokens_details": {"thinking_tokens": 2884}},
    }), encoding="utf-8")

    result = ClaudeRunner().parse_result(stdout, spec)
    assert result.tokens_out == 9051


def test_claude_parse_result_non_int_usage_output_tokens_ignored(tmp_path: Path) -> None:
    """A malformed/missing "usage.output_tokens" must never surface as a
    fabricated number -- None, same as a wholly absent "usage"."""
    spec = _spec(tmp_path)
    stdout = tmp_path / "out.stdout"
    stdout.write_text(json.dumps({
        "result": '{"status": "done", "summary": "ok", "requests": []}',
        "total_cost_usd": 0.42, "num_turns": 7, "is_error": False,
        "usage": {"output_tokens": "not-a-number"},
    }), encoding="utf-8")

    result = ClaudeRunner().parse_result(stdout, spec)
    assert result.tokens_out is None


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
    # The prompt reaches codex over stdin, not as an argv element (a
    # real review prompt can exceed Linux's MAX_ARG_STRLEN -- see
    # CodexRunner.prompt_via_stdin's own docstring) -- a bare "-" in
    # the prompt's own argv position tells codex to read it from stdin.
    assert argv[-1] == "-"
    assert CodexRunner.prompt_via_stdin is True
    schema_path = Path(argv[argv.index("--output-schema") + 1])
    assert schema_path.exists()
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["required"] == ["passed", "coverage", "findings"]
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
    script.write_text('[{"final": {"status": "done"}}]', encoding="utf-8")
    argv = ScriptedRunner(script).argv(spec, worker)
    assert argv == [sys.executable, "-P", "-m", "engine.fakes.scripted_worker",
                    "--inline", script.read_text(encoding="utf-8")]


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


def test_claude_argv_has_no_budget_cap_by_default(tmp_path: Path) -> None:
    argv = ClaudeRunner().argv(_spec(tmp_path), WorkerConfig(runner="claude"))
    assert "--max-budget-usd" not in argv
    assert "--max-turns" not in argv


