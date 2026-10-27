"""Tests for the 공방울타리2 hooks: scope fence (G25+G19) and orbit nudge (G05+G13)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sf = _load("scope_fence_hook")
on = _load("orbit_nudge_hook")


def _apps(tmp_path: Path) -> Path:
    apps = tmp_path / "apps"
    (apps / "demo-app" / "src").mkdir(parents=True)
    (apps / "other-app" / "src").mkdir(parents=True)
    return apps


def _scope(tmp_path: Path, allow: list[str]) -> Path:
    p = tmp_path / "CURRENT_SCOPE.json"
    p.write_text(json.dumps({"allow": allow}), encoding="utf-8")
    return p


# ---- scope fence -----------------------------------------------------------

def test_no_scope_file_means_no_restriction(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sf, "SCOPE_FILE", str(tmp_path / "missing.json"))
    out = sf.decide({"tool_name": "Edit", "tool_input": {"file_path": "<apps-root>/other-app/x.py"}})
    assert out is None


def test_edit_outside_scope_is_denied(tmp_path: Path, monkeypatch) -> None:
    apps = _apps(tmp_path)
    monkeypatch.setattr(sf, "APPS_ROOT", str(apps))
    monkeypatch.setattr(sf, "SCOPE_FILE", str(_scope(tmp_path, ["demo-app"])))
    out = sf.decide({"tool_name": "Edit", "tool_input": {"file_path": str(apps / "other-app/src/x.py")}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_edit_inside_scope_passes(tmp_path: Path, monkeypatch) -> None:
    apps = _apps(tmp_path)
    monkeypatch.setattr(sf, "APPS_ROOT", str(apps))
    monkeypatch.setattr(sf, "SCOPE_FILE", str(_scope(tmp_path, ["demo-app"])))
    out = sf.decide({"tool_name": "Edit", "tool_input": {"file_path": str(apps / "demo-app/src/x.py")}})
    assert out is None


def test_gb2_run_outside_scope_is_denied(tmp_path: Path, monkeypatch) -> None:
    apps = _apps(tmp_path)
    monkeypatch.setattr(sf, "APPS_ROOT", str(apps))
    monkeypatch.setattr(sf, "SCOPE_FILE", str(_scope(tmp_path, ["demo-app"])))
    out = sf.decide({"tool_name": "Bash", "tool_input": {"command": "gb2 run other-app"},
                     "cwd": str(apps / "demo-app")})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_gb2_on_a_scratch_sample_app_passes(tmp_path: Path, monkeypatch) -> None:
    """Live: a scratch sample app (its own GB2_APPS_ROOT, no folder under the apps root) was
    fenced like a real app, blocking the engine's own real-condition run."""
    apps = _apps(tmp_path)
    monkeypatch.setattr(sf, "APPS_ROOT", str(apps))
    monkeypatch.setattr(sf, "SCOPE_FILE", str(_scope(tmp_path, ["demo-app"])))
    out = sf.decide({"tool_name": "Bash",
                     "tool_input": {"command": "GB2_APPS_ROOT=/tmp/s/apps gb2 change node-small \"x\" --small"}})
    assert out is None


def test_sed_pattern_word_is_not_an_app_write(tmp_path: Path, monkeypatch) -> None:
    apps = _apps(tmp_path)
    monkeypatch.setattr(sf, "APPS_ROOT", str(apps))
    monkeypatch.setattr(sf, "SCOPE_FILE", str(_scope(tmp_path, ["demo-app"])))
    cmd = f"sed -i 's#{apps}/no-such-app/x#{apps}/demo-app/x#' {apps}/demo-app/src/x.py"
    assert sf.decide({"tool_name": "Bash", "tool_input": {"command": cmd}}) is None


def test_agent_prompt_naming_a_dot_folder_passes(tmp_path: Path, monkeypatch) -> None:
    apps = _apps(tmp_path)
    (apps / ".claude").mkdir()
    monkeypatch.setattr(sf, "APPS_ROOT", str(apps))
    monkeypatch.setattr(sf, "SCOPE_FILE", str(_scope(tmp_path, ["demo-app"])))
    out = sf.decide({"tool_name": "Agent", "tool_input": {"prompt": "작업자 HOME의 .claude 폴더 로그인 파일 크기만 본다"}})
    assert out is None


def test_gb2_run_inside_scope_passes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sf, "SCOPE_FILE", str(_scope(tmp_path, ["demo-app"])))
    out = sf.decide({"tool_name": "Bash", "tool_input": {"command": "gb2 fix demo-app"}})
    assert out is None


def test_gb2_watch_is_exempt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sf, "SCOPE_FILE", str(_scope(tmp_path, ["demo-app"])))
    out = sf.decide({"tool_name": "Bash", "tool_input": {"command": "gb2 watch --loop --resume"}})
    assert out is None


def test_bash_write_outside_scope_is_denied(tmp_path: Path, monkeypatch) -> None:
    apps = _apps(tmp_path)
    monkeypatch.setattr(sf, "APPS_ROOT", str(apps))
    monkeypatch.setattr(sf, "SCOPE_FILE", str(_scope(tmp_path, ["demo-app"])))
    out = sf.decide({"tool_name": "Bash",
                     "tool_input": {"command": f"echo x > {apps / 'other-app/src/x.py'}"}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_agent_prompt_naming_other_app_is_denied(tmp_path: Path, monkeypatch) -> None:
    apps = _apps(tmp_path)
    monkeypatch.setattr(sf, "SCOPE_FILE", str(_scope(tmp_path, ["demo-app"])))
    monkeypatch.setattr(sf, "APPS_ROOT", str(apps))
    out = sf.decide({"tool_name": "Agent", "tool_input": {"prompt": "other-app 쪽 버그 좀 고쳐줘"}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_agent_prompt_naming_only_scoped_app_passes(tmp_path: Path, monkeypatch) -> None:
    apps = _apps(tmp_path)
    monkeypatch.setattr(sf, "SCOPE_FILE", str(_scope(tmp_path, ["demo-app"])))
    monkeypatch.setattr(sf, "APPS_ROOT", str(apps))
    out = sf.decide({"tool_name": "Agent", "tool_input": {"prompt": "demo-app 쪽 버그 좀 고쳐줘"}})
    assert out is None


def test_current_scope_file_edit_always_denied_even_when_off(tmp_path: Path, monkeypatch) -> None:
    scope = _scope(tmp_path, ["demo-app"])
    monkeypatch.setattr(sf, "SCOPE_FILE", str(scope))
    monkeypatch.setattr(sf, "OFF", str(tmp_path / "off-marker-does-not-exist"))
    out = sf.decide({"tool_name": "Write", "tool_input": {"file_path": str(scope)}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_off_switch_allows_everything_except_scope_file(tmp_path: Path, monkeypatch) -> None:
    apps = _apps(tmp_path)
    off = tmp_path / "scope-fence-off"
    off.write_text("", encoding="utf-8")
    monkeypatch.setattr(sf, "SCOPE_FILE", str(_scope(tmp_path, ["demo-app"])))
    monkeypatch.setattr(sf, "OFF", str(off))
    out = sf.decide({"tool_name": "Edit", "tool_input": {"file_path": str(apps / "other-app/src/x.py")}})
    assert out is None


# ---- orbit nudge ------------------------------------------------------------

def _transcript(tmp: Path, records: list[dict]) -> str:
    p = tmp / "t.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    return str(p)


def _tool(name: str, inp: dict, tid: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def _result(tid: str, text: str, is_error: bool = False) -> dict:
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid,
                                                      "content": text, "is_error": is_error}]}}


def test_three_consecutive_failures_same_command_notifies(tmp_path: Path) -> None:
    records = []
    for i in range(3):
        records.append(_tool("Bash", {"command": "pytest -q tests/x.py"}, f"t{i}"))
        records.append(_result(f"t{i}", "1 FAILED"))
    out = on.decide({"transcript_path": _transcript(tmp_path, records)})
    assert out is not None and "systemMessage" in out


def test_two_failures_then_different_command_is_quiet(tmp_path: Path) -> None:
    records = [
        _tool("Bash", {"command": "pytest -q tests/x.py"}, "t0"), _result("t0", "1 FAILED"),
        _tool("Bash", {"command": "pytest -q tests/x.py"}, "t1"), _result("t1", "1 FAILED"),
        _tool("Bash", {"command": "pytest -q tests/y.py"}, "t2"), _result("t2", "passed"),
    ]
    out = on.decide({"transcript_path": _transcript(tmp_path, records)})
    assert out is None


def test_pending_background_bash_at_turn_end_notifies(tmp_path: Path) -> None:
    """Live shape: a background launch answers at once with its id; the work is pending until a
    queue-operation completion notice for that id arrives."""
    records = [_tool("Bash", {"command": "sleep 1", "run_in_background": True}, "bg1"),
               _result("bg1", "Command running in background with ID: bx1abc. Output is being written to: x")]
    out = on.decide({"transcript_path": _transcript(tmp_path, records)})
    assert out is not None and "systemMessage" in out


def test_pending_async_agent_notifies(tmp_path: Path) -> None:
    records = [_tool("Agent", {"prompt": "x"}, "ag1"),
               _result("ag1", "Async agent launched successfully.\nagentId: a1b2c3 (internal ID)")]
    out = on.decide({"transcript_path": _transcript(tmp_path, records)})
    assert out is not None and "systemMessage" in out


def test_background_work_with_completion_notice_is_quiet(tmp_path: Path) -> None:
    records = [_tool("Bash", {"command": "sleep 1", "run_in_background": True}, "bg1"),
               _result("bg1", "Command running in background with ID: bx1abc. Output is being written to: x"),
               {"type": "queue-operation", "operation": "enqueue",
                "content": "<task-notification>\n<task-id>bx1abc</task-id>\n<status>completed</status>"}]
    out = on.decide({"transcript_path": _transcript(tmp_path, records)})
    assert out is None


def test_foreground_bash_success_is_quiet(tmp_path: Path) -> None:
    records = [_tool("Bash", {"command": "pytest -q"}, "t0"), _result("t0", "passed")]
    out = on.decide({"transcript_path": _transcript(tmp_path, records)})
    assert out is None


def test_stop_hook_active_guard(tmp_path: Path) -> None:
    records = [_tool("Bash", {"command": "sleep 1", "run_in_background": True}, "bg1")]
    out = on.decide({"transcript_path": _transcript(tmp_path, records), "stop_hook_active": True})
    assert out is None
