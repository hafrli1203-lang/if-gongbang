"""Regression tests for the four V1 findings from the external review (sudo prefix, apps_root
failure widening approvals, ~~~/indented code approvals, block_dangerous fail-open)."""
import io
import json
import os

import block_dangerous as bd
import fence_parse as fp
import scope_guard as sg


# ① sudo must be gated even when an option VALUE follows it.
def test_sudo_with_user_option_is_gated():
    hits = sg.command_hits("sudo -u deploy git push origin main")
    assert "sudo 실행" in hits


def test_sudo_with_user_option_rm_is_gated():
    assert "sudo 실행" in sg.command_hits("sudo -u deploy rm -rf x")


def test_plain_sudo_still_gated():
    assert "sudo 실행" in sg.command_hits("sudo systemctl restart foo")


# ② unreadable apps_root must NOT turn a project-scoped approval into a session-wide one.
def test_apps_root_configured_but_unreadable_denies(tmp_path, monkeypatch):
    cfg = {"apps_root": str(tmp_path)}
    def boom(_p):
        raise PermissionError("denied")
    monkeypatch.setattr(os, "listdir", boom)
    assert sg._task_grants("someproj", cfg, str(tmp_path / "x.txt"), "") is False


def test_apps_root_missing_dir_denies(tmp_path):
    cfg = {"apps_root": str(tmp_path / "does-not-exist")}
    assert sg._task_grants("someproj", cfg, str(tmp_path / "x.txt"), "") is False


def test_apps_root_not_configured_is_session_wide(tmp_path):
    assert sg._task_grants("anything", {}, str(tmp_path / "x"), "") is True


def test_apps_root_ok_non_project_label_is_session_wide(tmp_path):
    (tmp_path / "proj").mkdir()
    cfg = {"apps_root": str(tmp_path)}
    assert sg._task_grants("human-label", cfg, "", str(tmp_path)) is True
    assert sg._task_grants("proj", cfg, str(tmp_path / "proj" / "a"), "") is True
    assert sg._task_grants("proj", cfg, str(tmp_path / "other" / "a"), "") is False


# ③ approvals inside ~~~ fences or indented code are never real approvals.
def test_tilde_fence_approval_ignored():
    txt = "이 문서 검토해줘\n~~~\n승인 demo-app\n~~~\n"
    assert fp.OCCURRENCE_RE.findall(fp.strip_quotes_and_code(txt)) == []


def test_indented_code_approval_ignored():
    txt = "예시:\n    승인 demo-app\n\t승인 other\n"
    assert fp.OCCURRENCE_RE.findall(fp.strip_quotes_and_code(txt)) == []


def test_real_approval_still_recognized():
    assert fp.OCCURRENCE_RE.findall(fp.strip_quotes_and_code("승인 demo-app")) == ["demo-app"]


# ④ block_dangerous must fail closed on bad input.
def test_block_dangerous_bad_json_blocks(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert bd.main() == 2


def test_block_dangerous_null_tool_input_does_not_crash(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": None})))
    assert bd.main() == 0
