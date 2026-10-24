"""Unit tests for `python -m engine.gate`: the PreToolUse hook CLI that
turns a Claude Code hook call into a Policy decision.

All of these invoke the real CLI as a subprocess (not the `decide()`
function directly) so they also exercise argv/stdin/stdout/exit-code
behavior.

The gate process never opens the ledger (ENGINE.md 「한계」: the worker
sandbox no longer has the ledger directory writable at all) -- it reads
a ProjectFacts snapshot from GB_GATE_FACTS and appends its own decisions
to the plain log file named by GB_GATE_LOG, both written here the same
way engine.steps.station does for a real worker.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from engine.config import EngineConfig
from engine.ledger import Event, Ledger
from engine.state import transition
from tests.conftest import make_repo

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _write_config(tmp_path: Path, extra: dict[str, object]) -> Path:
    base: dict[str, object] = {
        "projects": {"alpha": {"repo": "repo"}},
        "station_owners": {"build": {"builder": ["src/"]}},
        "station_terminal_allow": {"build": {"builder": ["pytest( -q)?"]}},
    }
    base.update(extra)
    config_path = tmp_path / "engine.json"
    config_path.write_text(json.dumps(base), encoding="utf-8")
    return config_path


def _open_attempt(config: EngineConfig, ledger: Ledger, role: str = "builder") -> tuple[str, Path]:
    attempt_id = "J-alpha-GATE#1"
    ledger.append(Event(
        project="alpha", kind="job.opened", actor_kind="owner", actor_id="owner",
        attempt_id=attempt_id,
        payload={
            "job_id": "J-alpha-GATE", "job_name": "gate", "attempt_id": attempt_id,
            "n": 1, "kind": "full", "generation": 1, "dirty_paths": [],
        },
    ))
    transition(ledger, "alpha", "open_job", "owner")
    copy_root = config.home / "copies" / attempt_id / role
    shutil.copytree(config.projects["alpha"].repo, copy_root)
    return attempt_id, copy_root


def _setup(tmp_path: Path, extra: dict[str, object] | None = None) -> tuple[EngineConfig, Path, str, Path]:
    make_repo(tmp_path / "repo", {"src/app.py": "print('x')\n"})
    config_path = _write_config(tmp_path, extra or {})
    config = EngineConfig.from_json(config_path)
    ledger = Ledger.create(config.ledger_path)
    ledger.register_project("alpha")
    attempt_id, copy_root = _open_attempt(config, ledger)
    ledger.close()
    return config, config_path, attempt_id, copy_root


def _write_gate_facts_and_log(tmp_path: Path, *, project: str = "alpha",
                              state: str = "OPEN", generation: int = 1,
                              attempt_id: str = "J-alpha-GATE#1") -> tuple[Path, Path]:
    """The ProjectFacts snapshot + empty decision log engine.steps.
    station writes for a real worker, built here the same shape."""
    facts_path = tmp_path / "gate_facts.json"
    facts_path.write_text(json.dumps({
        "project": project, "registered": True, "state": state,
        "generation": generation, "attempt_id": attempt_id, "attempt_active": True,
    }), encoding="utf-8")
    log_path = tmp_path / "gate.log"
    log_path.write_text("", encoding="utf-8")
    return facts_path, log_path


def _read_log(log_path: Path) -> list[dict[str, object]]:
    text = log_path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _run_gate(stdin: str, env: dict[str, str]) -> tuple[dict[str, object], subprocess.CompletedProcess]:
    full_env = {**env, "PATH": os.environ.get("PATH", "")}
    proc = subprocess.run(
        [sys.executable, "-m", "engine.gate"],
        input=stdin, capture_output=True, text=True, cwd=str(REPO_ROOT), env=full_env,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1]), proc


def _run_gate_expect_block(stdin: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """For the cases main() cannot produce a trustworthy JSON decision
    for (malformed stdin, missing/bad config, ...): exit 2, a stderr
    line, and no stdout -- see engine.gate's module docstring for why
    exit code (not a printed deny JSON) is what actually blocks the
    tool on the real Claude CLI."""
    full_env = {**env, "PATH": os.environ.get("PATH", "")}
    proc = subprocess.run(
        [sys.executable, "-m", "engine.gate"],
        input=stdin, capture_output=True, text=True, cwd=str(REPO_ROOT), env=full_env,
    )
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    return proc


def _base_env(config_path: Path, attempt_id: str, copy_root: Path,
              facts_path: Path, log_path: Path) -> dict[str, str]:
    return {
        "GB_CONFIG": str(config_path), "GB_PROJECT": "alpha", "GB_ATTEMPT": attempt_id,
        "GB_GENERATION": "1", "GB_STATION": "build", "GB_ROLE": "builder",
        "GB_COPY_ROOT": str(copy_root),
        "GB_GATE_FACTS": str(facts_path), "GB_GATE_LOG": str(log_path),
    }


def test_write_inside_prefix_allow(tmp_path: Path) -> None:
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    facts_path, log_path = _write_gate_facts_and_log(tmp_path)
    stdin = json.dumps({
        "tool_name": "Write", "cwd": str(copy_root),
        "tool_input": {"file_path": str(copy_root / "src" / "new.py")},
    })
    output, _ = _run_gate(stdin, _base_env(config_path, attempt_id, copy_root, facts_path, log_path))
    assert output["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_write_outside_prefix_deny(tmp_path: Path) -> None:
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    facts_path, log_path = _write_gate_facts_and_log(tmp_path)
    stdin = json.dumps({
        "tool_name": "Write", "cwd": str(copy_root),
        "tool_input": {"file_path": str(copy_root / "docs" / "new.md")},
    })
    output, _ = _run_gate(stdin, _base_env(config_path, attempt_id, copy_root, facts_path, log_path))
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_bash_allowed_pattern(tmp_path: Path) -> None:
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    facts_path, log_path = _write_gate_facts_and_log(tmp_path)
    stdin = json.dumps({
        "tool_name": "Bash", "cwd": str(copy_root), "tool_input": {"command": "pytest -q"},
    })
    output, _ = _run_gate(stdin, _base_env(config_path, attempt_id, copy_root, facts_path, log_path))
    assert output["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_webfetch_denied_without_policy_call(tmp_path: Path) -> None:
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    facts_path, log_path = _write_gate_facts_and_log(tmp_path)

    stdin = json.dumps({
        "tool_name": "WebFetch", "cwd": str(copy_root), "tool_input": {"url": "http://x"},
    })
    output, _ = _run_gate(stdin, _base_env(config_path, attempt_id, copy_root, facts_path, log_path))
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert output["hookSpecificOutput"]["permissionDecisionReason"] == "tool_not_allowed"

    entries = _read_log(log_path)
    assert len(entries) == 1
    assert entries[0]["kind"] == "gate.denied"
    assert entries[0]["payload"]["reason"] == "tool_not_allowed"
    assert not any(e["kind"] == "policy.decided" for e in entries)


def test_malformed_stdin_blocks_with_exit_2(tmp_path: Path) -> None:
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    facts_path, log_path = _write_gate_facts_and_log(tmp_path)
    proc = _run_gate_expect_block(
        "not json", _base_env(config_path, attempt_id, copy_root, facts_path, log_path))
    assert proc.stdout == ""
    assert "gate_error" in proc.stderr


def test_missing_config_blocks_with_exit_2(tmp_path: Path) -> None:
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    facts_path, log_path = _write_gate_facts_and_log(tmp_path)
    env = _base_env(config_path, attempt_id, copy_root, facts_path, log_path)
    env["GB_CONFIG"] = str(tmp_path / "does-not-exist.json")
    stdin = json.dumps({
        "tool_name": "Write", "cwd": str(copy_root),
        "tool_input": {"file_path": str(copy_root / "src" / "new.py")},
    })
    proc = _run_gate_expect_block(stdin, env)
    assert proc.stdout == ""
    assert "gate_error" in proc.stderr


def test_missing_gate_facts_denies_every_tool(tmp_path: Path) -> None:
    """GB_GATE_FACTS unset, or its file missing/unparseable: a plain
    DENY ("gate_facts_missing"), never a hard exit-2 block -- this is
    a trustworthy decision the gate CAN still produce and print."""
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    _facts_path, log_path = _write_gate_facts_and_log(tmp_path)
    env = _base_env(config_path, attempt_id, copy_root, tmp_path / "does-not-exist.json", log_path)

    for stdin in (
        json.dumps({"tool_name": "Write", "cwd": str(copy_root),
                    "tool_input": {"file_path": str(copy_root / "src" / "new.py")}}),
        json.dumps({"tool_name": "Read", "cwd": str(copy_root),
                    "tool_input": {"file_path": str(copy_root / "src" / "app.py")}}),
        json.dumps({"tool_name": "Bash", "cwd": str(copy_root),
                    "tool_input": {"command": "pytest -q"}}),
    ):
        output, _ = _run_gate(stdin, env)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert output["hookSpecificOutput"]["permissionDecisionReason"] == "gate_facts_missing"


def test_missing_gate_log_denies_with_gate_log_missing(tmp_path: Path) -> None:
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    facts_path, _log_path = _write_gate_facts_and_log(tmp_path)
    env = _base_env(config_path, attempt_id, copy_root, facts_path,
                    tmp_path / "does-not-exist.log")
    stdin = json.dumps({
        "tool_name": "Write", "cwd": str(copy_root),
        "tool_input": {"file_path": str(copy_root / "src" / "new.py")},
    })
    output, _ = _run_gate(stdin, env)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert output["hookSpecificOutput"]["permissionDecisionReason"] == "gate_log_missing"


def test_non_integer_generation_in_facts_denies(tmp_path: Path) -> None:
    """`int(raw["generation"])` would silently truncate a float
    (int(1.9) == 1, no exception -- unlike int("1.9"), which does
    raise) instead of rejecting a facts file whose generation field is
    not actually an int. A tampered or corrupted facts file with
    "generation": 1.9 must deny, the same as any other unusable facts
    file, not be silently accepted as generation 1."""
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    facts_path, log_path = _write_gate_facts_and_log(tmp_path)
    raw = json.loads(facts_path.read_text(encoding="utf-8"))
    raw["generation"] = 1.9
    facts_path.write_text(json.dumps(raw), encoding="utf-8")
    env = _base_env(config_path, attempt_id, copy_root, facts_path, log_path)
    stdin = json.dumps({
        "tool_name": "Write", "cwd": str(copy_root),
        "tool_input": {"file_path": str(copy_root / "src" / "new.py")},
    })
    output, _ = _run_gate(stdin, env)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert output["hookSpecificOutput"]["permissionDecisionReason"] == "gate_facts_missing"


def test_facts_project_mismatch_denies(tmp_path: Path) -> None:
    """The facts file's own "project" field must be compared to the
    trusted GB_PROJECT this gate process was started with -- a facts
    file naming a DIFFERENT project is never a legitimate
    engine-written snapshot for this worker and must deny, not be
    silently accepted."""
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    facts_path, log_path = _write_gate_facts_and_log(tmp_path, project="beta")
    env = _base_env(config_path, attempt_id, copy_root, facts_path, log_path)
    stdin = json.dumps({
        "tool_name": "Write", "cwd": str(copy_root),
        "tool_input": {"file_path": str(copy_root / "src" / "new.py")},
    })
    output, _ = _run_gate(stdin, env)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert output["hookSpecificOutput"]["permissionDecisionReason"] == "gate_facts_missing"


def test_relative_path_resolved_against_cwd(tmp_path: Path) -> None:
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    facts_path, log_path = _write_gate_facts_and_log(tmp_path)
    stdin = json.dumps({
        "tool_name": "Write", "cwd": str(copy_root / "src"),
        "tool_input": {"file_path": "new.py"},
    })
    output, _ = _run_gate(stdin, _base_env(config_path, attempt_id, copy_root, facts_path, log_path))
    assert output["hookSpecificOutput"]["permissionDecision"] == "allow"

    stdin_outside = json.dumps({
        "tool_name": "Write", "cwd": str(copy_root / "docs"),
        "tool_input": {"file_path": "new.md"},
    })
    output2, _ = _run_gate(stdin_outside, _base_env(config_path, attempt_id, copy_root, facts_path, log_path))
    assert output2["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_read_outside_copy_denied_via_gate(tmp_path: Path) -> None:
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    facts_path, log_path = _write_gate_facts_and_log(tmp_path)
    outside = tmp_path / "owner-home"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope", encoding="utf-8")
    stdin = json.dumps({
        "tool_name": "Read", "cwd": str(copy_root),
        "tool_input": {"file_path": str(outside / "secret.txt")},
    })
    output, _ = _run_gate(stdin, _base_env(config_path, attempt_id, copy_root, facts_path, log_path))
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert output["hookSpecificOutput"]["permissionDecisionReason"] == "read_outside_copy"


def test_read_inside_copy_allowed_via_gate(tmp_path: Path) -> None:
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    facts_path, log_path = _write_gate_facts_and_log(tmp_path)
    stdin = json.dumps({
        "tool_name": "Read", "cwd": str(copy_root),
        "tool_input": {"file_path": str(copy_root / "src" / "app.py")},
    })
    output, _ = _run_gate(stdin, _base_env(config_path, attempt_id, copy_root, facts_path, log_path))
    assert output["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_bash_home_reference_denied(tmp_path: Path) -> None:
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    facts_path, log_path = _write_gate_facts_and_log(tmp_path)
    stdin = json.dumps({
        "tool_name": "Bash", "cwd": str(copy_root),
        "tool_input": {"command": "cat ~/.ssh/id_rsa"},
    })
    output, _ = _run_gate(stdin, _base_env(config_path, attempt_id, copy_root, facts_path, log_path))
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert output["hookSpecificOutput"]["permissionDecisionReason"] == "home_reference"


def test_bash_path_traversal_denied(tmp_path: Path) -> None:
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    facts_path, log_path = _write_gate_facts_and_log(tmp_path)
    stdin = json.dumps({
        "tool_name": "Bash", "cwd": str(copy_root),
        "tool_input": {"command": "cat ../../etc/passwd"},
    })
    output, _ = _run_gate(stdin, _base_env(config_path, attempt_id, copy_root, facts_path, log_path))
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert output["hookSpecificOutput"]["permissionDecisionReason"] == "path_traversal"


def test_bash_dollar_variable_denied(tmp_path: Path) -> None:
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    facts_path, log_path = _write_gate_facts_and_log(tmp_path)
    stdin = json.dumps({
        "tool_name": "Bash", "cwd": str(copy_root),
        "tool_input": {"command": "echo $HOME"},
    })
    output, _ = _run_gate(stdin, _base_env(config_path, attempt_id, copy_root, facts_path, log_path))
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert output["hookSpecificOutput"]["permissionDecisionReason"] == "shell_metachar"


def test_ambiguous_tilde_path_denied(tmp_path: Path) -> None:
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    facts_path, log_path = _write_gate_facts_and_log(tmp_path)
    stdin = json.dumps({
        "tool_name": "Read", "cwd": str(copy_root),
        "tool_input": {"file_path": f"{copy_root}/~/secret.txt"},
    })
    output, _ = _run_gate(stdin, _base_env(config_path, attempt_id, copy_root, facts_path, log_path))
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert output["hookSpecificOutput"]["permissionDecisionReason"] == "ambiguous_path"


def test_whitespace_padded_path_stripped_then_allowed(tmp_path: Path) -> None:
    """Claude Code itself trims whitespace from file_path before using
    it, so the gate must strip the same way -- and then evaluate the
    cleaned path normally -- rather than denying every padded value."""
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    facts_path, log_path = _write_gate_facts_and_log(tmp_path)
    stdin = json.dumps({
        "tool_name": "Write", "cwd": str(copy_root),
        "tool_input": {"file_path": f"  {copy_root}/src/new.py  "},
    })
    output, _ = _run_gate(stdin, _base_env(config_path, attempt_id, copy_root, facts_path, log_path))
    assert output["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_empty_after_strip_denied(tmp_path: Path) -> None:
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    facts_path, log_path = _write_gate_facts_and_log(tmp_path)
    stdin = json.dumps({
        "tool_name": "Write", "cwd": str(copy_root),
        "tool_input": {"file_path": "   "},
    })
    output, _ = _run_gate(stdin, _base_env(config_path, attempt_id, copy_root, facts_path, log_path))
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert output["hookSpecificOutput"]["permissionDecisionReason"] == "ambiguous_path"


def test_glob_pattern_escape_denied(tmp_path: Path) -> None:
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    facts_path, log_path = _write_gate_facts_and_log(tmp_path)
    stdin = json.dumps({
        "tool_name": "Glob", "cwd": str(copy_root),
        "tool_input": {"path": str(copy_root), "pattern": "../../etc/passwd"},
    })
    output, _ = _run_gate(stdin, _base_env(config_path, attempt_id, copy_root, facts_path, log_path))
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert output["hookSpecificOutput"]["permissionDecisionReason"] == "pattern_escapes"


def test_grep_glob_field_absolute_denied(tmp_path: Path) -> None:
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    facts_path, log_path = _write_gate_facts_and_log(tmp_path)
    stdin = json.dumps({
        "tool_name": "Grep", "cwd": str(copy_root),
        "tool_input": {"path": str(copy_root), "glob": "/etc/**"},
    })
    output, _ = _run_gate(stdin, _base_env(config_path, attempt_id, copy_root, facts_path, log_path))
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert output["hookSpecificOutput"]["permissionDecisionReason"] == "pattern_escapes"


def test_gate_not_shadowed_by_copy_engine_package(tmp_path: Path) -> None:
    """A worker copy containing its own engine/gate.py (that allows
    everything) must not be able to shadow the trusted gate just because
    the worker process's cwd is inside the copy: `-m engine.gate` is
    invoked with `-P` (safe path: no cwd/script-dir on sys.path), so
    only PYTHONPATH's trusted engine root is ever found."""
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    facts_path, log_path = _write_gate_facts_and_log(tmp_path)
    fake_engine = copy_root / "engine"
    fake_engine.mkdir()
    (fake_engine / "__init__.py").write_text("", encoding="utf-8")
    (fake_engine / "gate.py").write_text(
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'hookSpecificOutput': {"
        "'hookEventName': 'PreToolUse', 'permissionDecision': 'allow', "
        "'permissionDecisionReason': 'shadow'}}))\n",
        encoding="utf-8",
    )
    outside = tmp_path / "owner-home"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope", encoding="utf-8")
    stdin = json.dumps({
        "tool_name": "Read", "cwd": str(copy_root),
        "tool_input": {"file_path": str(outside / "secret.txt")},
    })
    env = {**_base_env(config_path, attempt_id, copy_root, facts_path, log_path),
           "PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.run(
        [sys.executable, "-P", "-m", "engine.gate"],
        input=stdin, capture_output=True, text=True, cwd=str(copy_root), env=env,
    )
    assert proc.returncode == 0, proc.stderr
    output = json.loads(proc.stdout.strip().splitlines()[-1])
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert output["hookSpecificOutput"]["permissionDecisionReason"] == "read_outside_copy"


def test_watchdog_alarm_handler_exits_2(monkeypatch, capsys) -> None:
    """Unit-test the SIGALRM handler directly (rather than trying to
    make the real gate process hang in a subprocess test): it must log
    one line to stderr and call os._exit(2), not return or raise, since
    a signal handler cannot assume the code it interrupted will ever
    hand control back."""
    import engine.gate as gate_module

    calls: list[int] = []
    monkeypatch.setattr(gate_module.os, "_exit", calls.append)
    gate_module._handle_watchdog_alarm(gate_module.signal.SIGALRM, None)
    assert calls == [2]
    assert "gate_timeout" in capsys.readouterr().err


def test_gate_command_wrapped_sh_exit_2() -> None:
    """engine.workers.claude_cli._gate_command's shape: see test_workers
    for the full assertion; this just confirms the gate module itself
    does not depend on that wrapper to reach exit 2 on its own errors
    (test_malformed_stdin_blocks_with_exit_2 already proves that), so
    the two layers are independently verified."""
    import engine.gate as gate_module
    assert gate_module.WATCHDOG_TIMEOUT_SEC < 30  # must fire before the hook's own timeout


def test_every_decision_appended_to_gate_log(tmp_path: Path) -> None:
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    facts_path, log_path = _write_gate_facts_and_log(tmp_path)
    assert _read_log(log_path) == []

    stdin = json.dumps({
        "tool_name": "Write", "cwd": str(copy_root),
        "tool_input": {"file_path": str(copy_root / "src" / "new.py")},
    })
    _run_gate(stdin, _base_env(config_path, attempt_id, copy_root, facts_path, log_path))

    entries = _read_log(log_path)
    assert len(entries) == 1
    assert entries[0]["kind"] == "policy.decided"
    assert entries[0]["payload"]["allowed"] is True

    # The ledger itself was never touched -- the gate opened no ledger
    # at all for this call.
    ledger = Ledger.open(config.ledger_path)
    after = len(ledger.events("alpha", "policy.decided"))
    ledger.close()
    assert after == 0


def test_project_not_open_denied_from_facts_not_ledger(tmp_path: Path) -> None:
    """Rule 2 (project must be OPEN) is decided purely from the
    ProjectFacts snapshot, never a fresh ledger read -- a facts file
    saying CLOSED denies even though the real ledger (unconsulted)
    still says OPEN."""
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    facts_path, log_path = _write_gate_facts_and_log(tmp_path, state="CLOSED")
    stdin = json.dumps({
        "tool_name": "Write", "cwd": str(copy_root),
        "tool_input": {"file_path": str(copy_root / "src" / "new.py")},
    })
    output, _ = _run_gate(stdin, _base_env(config_path, attempt_id, copy_root, facts_path, log_path))
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert output["hookSpecificOutput"]["permissionDecisionReason"] == "project_not_open:CLOSED"
