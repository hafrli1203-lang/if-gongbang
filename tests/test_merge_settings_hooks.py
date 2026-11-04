# Generated from the private ops workspace at 24c1bb68758c8a552514d06d8280463876278f76; do not edit by hand (see harness/export_ops_public.py).
import copy

import merge_settings_hooks as m

BASE_SETTINGS = {
    "env": {"FOO": "1"},
    "permissions": {"allow": ["Read"], "deny": []},
    "hooks": {
        "PreToolUse": [
            {"matcher": "Edit|Write", "hooks": [
                {"type": "command", "command": "python3 /some/other/cwd/.claude/hooks/protect-sensitive-files.py"},
            ]},
        ],
        "Stop": [
            {"matcher": "", "hooks": [
                {"type": "command", "command": "python3 /some/other/cwd/.claude/hooks/verify-claims.py"},
                {"type": "command", "command": "python3 /some/other/cwd/.claude/hooks/notify-complete.py"},
            ]},
        ],
    },
}

HOOKS_DIR = "/some/other/cwd/.claude/hooks"


def test_merge_adds_all_registrations_and_removes_old_one():
    after, changed = m.merge(copy.deepcopy(BASE_SETTINGS), HOOKS_DIR)
    assert changed is True
    pre = after["hooks"]["PreToolUse"]
    commands = [h["command"] for e in pre for h in e["hooks"]]
    assert f"python3 {HOOKS_DIR}/block_dangerous.py" in commands
    assert f"python3 {HOOKS_DIR}/protect_sensitive_files.py" in commands
    assert f"python3 {HOOKS_DIR}/mcp_scope_limit.py" in commands
    assert f"python3 {HOOKS_DIR}/scope_fence.py" in commands
    # old registration is gone
    assert not any(c.endswith("protect-sensitive-files.py") for c in commands)


def test_merge_drops_now_empty_old_entry_entirely():
    after, _ = m.merge(copy.deepcopy(BASE_SETTINGS), HOOKS_DIR)
    pre = after["hooks"]["PreToolUse"]
    # the old Edit|Write entry whose ONLY hook was protect-sensitive-files.py must be gone
    # entirely, not left behind as a dangling {"matcher": "Edit|Write", "hooks": []}
    assert not any(e.get("matcher") == "Edit|Write" and not e.get("hooks") for e in pre)


def test_merge_preserves_unrelated_existing_entries():
    after, _ = m.merge(copy.deepcopy(BASE_SETTINGS), HOOKS_DIR)
    stop_commands = [h["command"] for e in after["hooks"]["Stop"] for h in e["hooks"]]
    assert any(c.endswith("verify-claims.py") for c in stop_commands)
    assert after["env"] == {"FOO": "1"}
    assert after["permissions"]["allow"] == ["Read"] and after["permissions"]["deny"] == ["Bash(run_in_background:true)"]  # required deny is merged in (2026-11-02)


def test_merge_adds_stop_registrations():
    after, _ = m.merge(copy.deepcopy(BASE_SETTINGS), HOOKS_DIR)
    stop_commands = [h["command"] for e in after["hooks"]["Stop"] for h in e["hooks"]]


def test_merge_is_idempotent():
    once, _ = m.merge(copy.deepcopy(BASE_SETTINGS), HOOKS_DIR)
    twice, changed = m.merge(copy.deepcopy(once), HOOKS_DIR)
    assert changed is False
    assert once == twice


def test_merge_on_settings_with_no_hooks_key_at_all():
    settings = {"env": {}}
    after, changed = m.merge(settings, HOOKS_DIR)
    assert changed is True
    pre_commands = [h["command"] for e in after["hooks"]["PreToolUse"] for h in e["hooks"]]
    assert f"python3 {HOOKS_DIR}/block_dangerous.py" in pre_commands


def test_dry_run_mode_writes_nothing(tmp_path, capsys):
    import json
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(BASE_SETTINGS), encoding="utf-8")
    original_mtime = settings_path.stat().st_mtime_ns
    original_text = settings_path.read_text(encoding="utf-8")
    import sys as _sys
    old_argv = _sys.argv
    try:
        _sys.argv = ["merge_settings_hooks.py", str(settings_path), HOOKS_DIR, "--dry"]
        rc = m.main()
    finally:
        _sys.argv = old_argv
    assert rc == 0
    out = capsys.readouterr().out
    assert "block_dangerous.py" in out
    assert settings_path.read_text(encoding="utf-8") == original_text
    assert settings_path.stat().st_mtime_ns == original_mtime


def test_required_deny_added_once(tmp_path):
    from merge_settings_hooks import merge, REQUIRED_DENY
    hooks_dir = str(tmp_path)
    out, changed = merge({"permissions": {"deny": ["Bash(rm -rf /)"]}}, hooks_dir)
    assert changed and all(d in out["permissions"]["deny"] for d in REQUIRED_DENY)
    out2, changed2 = merge(out, hooks_dir)
    assert out2["permissions"]["deny"].count(REQUIRED_DENY[0]) == 1
