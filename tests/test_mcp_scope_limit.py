# Generated from the private ops workspace at 24c1bb68758c8a552514d06d8280463876278f76; do not edit by hand (see harness/export_ops_public.py).
import io
import json

import mcp_scope_limit as m


def test_non_mcp_tool_is_not_restricted():
    assert m.decide({"tool_name": "Bash", "cwd": "/apps/_wt/myapp/x"}) is None


def test_mcp_call_outside_work_copy_is_not_restricted():
    assert m.decide({"tool_name": "mcp__claude_ai_Gmail__authenticate", "cwd": "/apps/myapp"}) is None


def test_mcp_call_with_no_cwd_is_not_restricted():
    assert m.decide({"tool_name": "mcp__claude_ai_Gmail__authenticate"}) is None


def test_owner_account_server_in_work_copy_is_denied():
    reason = m.decide({"tool_name": "mcp__claude_ai_Gmail__authenticate", "cwd": "/apps/_wt/myapp/x"})
    assert reason is not None


def test_karrot_ads_server_in_work_copy_is_denied():
    reason = m.decide({"tool_name": "mcp__claude_ai_karrot-ads-mcp__list_ad_accounts", "cwd": "/apps/_wt/myapp/x"})
    assert reason is not None


def test_library_server_in_work_copy_is_allowed():
    assert m.decide({"tool_name": "mcp__company-library__library_search", "cwd": "/apps/_wt/myapp/x"}) is None


def test_playwright_server_in_work_copy_is_allowed():
    assert m.decide({"tool_name": "mcp__plugin_playwright_playwright__browser_navigate",
                      "cwd": "/apps/_wt/myapp/x"}) is None


def test_main_fails_open_on_bad_json(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    code = m.main()
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == ""


def test_main_denies_with_pretooluse_shape(monkeypatch, capsys):
    payload = {"tool_name": "mcp__claude_ai_Notion__authenticate", "cwd": "/apps/_wt/myapp/x"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    m.main()
    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert data["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_main_allows_ordinary_bash(monkeypatch, capsys):
    payload = {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/apps/_wt/myapp/x"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    code = m.main()
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == ""
