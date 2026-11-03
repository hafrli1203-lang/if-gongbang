# Generated from the private ops workspace at b94af62b97af64e2e58e8c102fa2538f066ae366; do not edit by hand (see harness/export_ops_public.py).
import io
import json
import os

import protect_sensitive_files as psf

DEFAULT_CFG = {
    "protected_paths": [".env", ".env.*", "*.pem", "*.key", "id_rsa*"],
    "protected_exceptions": [".env.example", "*.sample"],
    "hard_protected_patterns": [
        r"/\.config/systemd/user/",
        r"^/etc/systemd/",
        r"Caddyfile$",
    ],
    "work_queue_path": "/apps/_ops/WORK_QUEUE.md",
}

HOOKS_DIR = psf.REAL_HOOKS_DIR
GATE_DIR = psf.REAL_GATE_DIR


# --- check_path: Read/Edit/Write/MultiEdit/NotebookEdit's own file_path ----------------------

def test_bare_env_file_is_protected():
    assert psf.check_path(".env", DEFAULT_CFG) is not None


def test_env_local_matches_env_star_pattern():
    assert psf.check_path(".env.local", DEFAULT_CFG) is not None


def test_env_example_is_an_exception():
    assert psf.check_path(".env.example", DEFAULT_CFG) is None


def test_sample_suffix_is_an_exception():
    assert psf.check_path("config.sample", DEFAULT_CFG) is None


def test_pem_key_file_is_protected():
    assert psf.check_path("myapp/config/server.pem", DEFAULT_CFG) is not None


def test_id_rsa_is_protected():
    assert psf.check_path("~/.ssh/id_rsa", DEFAULT_CFG) is not None


def test_ordinary_source_file_is_not_protected():
    assert psf.check_path("src/main.py", DEFAULT_CFG) is None


def test_empty_path_is_not_protected():
    assert psf.check_path("", DEFAULT_CFG) is None


# --- check_command: Bash tokens -------------------------------------------------------------

def test_cat_env_is_protected():
    assert psf.check_command("cat .env", DEFAULT_CFG) is not None


def test_sed_read_env_is_protected():
    assert psf.check_command("sed -n 1p .env", DEFAULT_CFG) is not None


def test_cat_env_example_is_not_protected():
    assert psf.check_command("cat .env.example", DEFAULT_CFG) is None


def test_flag_token_is_never_treated_as_path():
    assert psf.check_command("ls -la", DEFAULT_CFG) is None


def test_write_redirect_target_is_protected():
    assert psf.check_command("echo FOO=1 > .env", DEFAULT_CFG) is not None


def test_custom_protected_pattern_from_config_is_honored():
    cfg = {**DEFAULT_CFG, "protected_paths": ["*.secret"], "protected_exceptions": []}
    assert psf.check_path("db.secret", cfg) is not None
    assert psf.check_path(".env", cfg) is None


# --- self-protect: hooks/gate dir, settings.json, gongbang.config.json (items 2/7/18/280) ----
# check_self_protect uses HARDCODED real paths (REAL_HOOKS_DIR/REAL_GATE_DIR/
# REAL_PROTECTED_FILES), never the (overridable) loaded config - see module docstring for why.

def test_file_inside_installed_hooks_dir_is_self_protected():
    assert psf.check_self_protect(os.path.join(HOOKS_DIR, "block_dangerous.py")) is not None


def test_file_under_a_same_named_hooks_folder_elsewhere_is_not_self_protected():
    # A staging copy named .claude/hooks OUTSIDE the real home must not be caught.
    assert psf.check_self_protect("/tmp/some-other-project/.claude/hooks/block_dangerous.py") is None


def test_legacy_gate_dir_is_self_protected():
    assert psf.check_self_protect(os.path.join(GATE_DIR, "approved.json")) is not None


def test_settings_json_is_self_protected():
    assert psf.check_self_protect(os.path.join(psf.REAL_CLAUDE_HOME, "settings.json")) is not None


def test_settings_local_json_is_self_protected():
    assert psf.check_self_protect(os.path.join(psf.REAL_CLAUDE_HOME, "settings.local.json")) is not None


def test_gongbang_config_json_is_self_protected():
    # The config file THIS hook's own patterns are loaded from must itself be protected -
    # otherwise an agent could override installed_hooks_dir and defeat self-protection entirely.
    assert psf.check_self_protect(os.path.join(psf.REAL_CLAUDE_HOME, "gongbang.config.json")) is not None


def test_settings_json_dot_path_segment_is_still_self_protected():
    # `~/.claude/./settings.json` must not defeat the check (Codex-found bypass).
    assert psf.check_self_protect(os.path.join(psf.REAL_CLAUDE_HOME, "./settings.json")) is not None


def test_ordinary_app_file_is_not_self_protected():
    assert psf.check_self_protect("/apps/myapp/src/index.ts") is None


def test_caddyfile_matches_hard_pattern():
    assert psf.check_hard_patterns("/etc/caddy/Caddyfile", DEFAULT_CFG) is not None


def test_systemd_user_unit_matches_hard_pattern():
    assert psf.check_hard_patterns(os.path.expanduser("~/.config/systemd/user/ifapp-x.service"), DEFAULT_CFG) is not None


def test_ordinary_app_file_does_not_match_hard_pattern():
    assert psf.check_hard_patterns("/apps/myapp/src/index.ts", DEFAULT_CFG) is None


# --- WORK_QUEUE.md protection (items 19/281): ANY bash write/delete/truncate is denied --------

def test_work_queue_write_tool_marking_done_is_detected(tmp_path):
    wq = tmp_path / "WORK_QUEUE.md"
    wq.write_text("- [ ] TASK1\n", encoding="utf-8")
    cfg = {**DEFAULT_CFG, "work_queue_path": str(wq)}
    inp = {"file_path": str(wq), "content": "- [x] TASK1\n"}
    assert psf.check_work_queue_edit("Write", inp, cfg) is True


def test_work_queue_edit_split_boundary_is_detected(tmp_path):
    wq = tmp_path / "WORK_QUEUE.md"
    wq.write_text("- [ ] TASK1\n", encoding="utf-8")
    cfg = {**DEFAULT_CFG, "work_queue_path": str(wq)}
    inp = {"file_path": str(wq), "old_string": " ] TASK1", "new_string": "x] TASK1"}
    assert psf.check_work_queue_edit("Edit", inp, cfg) is True


def test_work_queue_edit_that_does_not_increase_x_marks_passes(tmp_path):
    wq = tmp_path / "WORK_QUEUE.md"
    wq.write_text("- [ ] TASK1\n- [ ] TASK2\n", encoding="utf-8")
    cfg = {**DEFAULT_CFG, "work_queue_path": str(wq)}
    inp = {"file_path": str(wq), "old_string": "TASK2", "new_string": "TASK2 renamed"}
    assert psf.check_work_queue_edit("Edit", inp, cfg) is False


def test_edit_of_a_different_file_is_not_a_work_queue_hit(tmp_path):
    other = tmp_path / "NOTES.md"
    other.write_text("hello\n", encoding="utf-8")
    cfg = {**DEFAULT_CFG, "work_queue_path": str(tmp_path / "WORK_QUEUE.md")}
    inp = {"file_path": str(other), "content": "[x] anything\n"}
    assert psf.check_work_queue_edit("Write", inp, cfg) is False


def test_bash_echo_append_to_work_queue_is_denied():
    assert psf.check_work_queue_bash(
        "echo '[x] TASK1' >> /apps/_ops/WORK_QUEUE.md", DEFAULT_CFG) is True


def test_bash_sed_inplace_on_work_queue_is_denied_even_without_literal_x_mark():
    # Codex-found bypass: the old check required the literal text "[x]" in the SAME segment -
    # this command builds the mark from two separate regex pieces and never contains "[x]".
    cmd = "sed -i 's/ ] TASK1/x] TASK1/' /apps/_ops/WORK_QUEUE.md"
    assert psf.check_work_queue_bash(cmd, DEFAULT_CFG) is True


def test_bash_piped_tee_into_work_queue_is_denied():
    # Codex-found bypass: a pipe put the "[x]" text and the write target in DIFFERENT segments.
    cmd = "echo '[x] TASK1' | tee -a /apps/_ops/WORK_QUEUE.md"
    assert psf.check_work_queue_bash(cmd, DEFAULT_CFG) is True


def test_bash_rm_of_work_queue_is_denied():
    assert psf.check_work_queue_bash("rm /apps/_ops/WORK_QUEUE.md", DEFAULT_CFG) is True


def test_bash_truncate_of_work_queue_is_denied():
    assert psf.check_work_queue_bash("truncate -s 0 /apps/_ops/WORK_QUEUE.md", DEFAULT_CFG) is True


def test_bash_mv_of_work_queue_source_is_denied():
    assert psf.check_work_queue_bash("mv /apps/_ops/WORK_QUEUE.md /tmp/x.md", DEFAULT_CFG) is True


def test_bash_grep_read_of_work_queue_is_not_a_hit():
    assert psf.check_work_queue_bash("grep -F '[x]' /apps/_ops/WORK_QUEUE.md", DEFAULT_CFG) is False


def test_bash_write_to_an_unrelated_file_is_not_a_work_queue_hit():
    assert psf.check_work_queue_bash("echo '[x] TASK2' >> /tmp/other.md", DEFAULT_CFG) is False


# --- self-protect bypasses a Codex adversarial review found in the first version -------------

def test_bash_truncate_of_hook_file_is_denied(monkeypatch, capsys):
    target = os.path.join(HOOKS_DIR, "block_dangerous.py")
    payload = {"tool_name": "Bash", "tool_input": {"command": f"truncate -s 0 {target}"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    psf.main()
    data = json.loads(capsys.readouterr().out.strip())
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_bash_rm_of_hook_file_is_denied(monkeypatch, capsys):
    target = os.path.join(HOOKS_DIR, "block_dangerous.py")
    payload = {"tool_name": "Bash", "tool_input": {"command": f"rm {target}"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    psf.main()
    data = json.loads(capsys.readouterr().out.strip())
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_bash_mv_hook_file_out_of_hooks_dir_is_denied(monkeypatch, capsys):
    target = os.path.join(HOOKS_DIR, "block_dangerous.py")
    payload = {"tool_name": "Bash", "tool_input": {"command": f"mv {target} /tmp/disabled-hook.py"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    psf.main()
    data = json.loads(capsys.readouterr().out.strip())
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_bash_c_wrapped_settings_json_write_is_denied(monkeypatch, capsys):
    payload = {"tool_name": "Bash", "tool_input": {"command": "bash -c 'echo {} > ~/.claude/settings.json'"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    psf.main()
    data = json.loads(capsys.readouterr().out.strip())
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_bash_write_to_gongbang_config_json_is_denied(monkeypatch, capsys):
    target = os.path.join(psf.REAL_CLAUDE_HOME, "gongbang.config.json")
    payload = {"tool_name": "Bash", "tool_input": {"command": f"echo '{{}}' > {target}"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    psf.main()
    data = json.loads(capsys.readouterr().out.strip())
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_edit_tool_write_to_gongbang_config_json_is_denied(monkeypatch, capsys):
    target = os.path.join(psf.REAL_CLAUDE_HOME, "gongbang.config.json")
    payload = {"tool_name": "Write", "tool_input": {"file_path": target, "content": "{}"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    psf.main()
    data = json.loads(capsys.readouterr().out.strip())
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- main(): deny JSON shape + fail-closed ----------------------------------------------------

def test_main_denies_edit_of_env_file(monkeypatch):
    payload = {"tool_name": "Edit", "tool_input": {"file_path": "project/.env", "old_string": "a", "new_string": "b"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    assert psf.main() == 0  # exit 0 with a deny JSON body is this hook's "block" shape


def test_main_allows_ordinary_read(monkeypatch, capsys):
    payload = {"tool_name": "Read", "tool_input": {"file_path": "project/README.md"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    code = psf.main()
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == ""


def test_main_fails_closed_on_bad_json(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert psf.main() == 0


def test_main_denied_json_has_pretooluse_shape(monkeypatch, capsys):
    payload = {"tool_name": "Read", "tool_input": {"file_path": ".env"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    psf.main()
    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert data["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_main_denies_bash_write_to_settings_json(monkeypatch, capsys):
    payload = {"tool_name": "Bash", "tool_input": {"command": "echo '{}' > ~/.claude/settings.json"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    psf.main()
    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_main_allows_read_of_a_hook_file(monkeypatch, capsys):
    # Reviewing a hook's own source is routine, safe work - only a WRITE to the hooks dir is
    # denied.
    target = os.path.join(HOOKS_DIR, "block_dangerous.py")
    payload = {"tool_name": "Read", "tool_input": {"file_path": target}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    code = psf.main()
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == ""


def test_main_denies_bash_relative_write_into_hooks_dir_via_cwd(monkeypatch, capsys):
    # A relative write target must be resolved against the TOOL CALL's cwd (`cd ~/.claude/hooks
    # && echo x > evil.py`), not this hook subprocess's own unrelated cwd.
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo oops > evil.py"},
        "cwd": HOOKS_DIR,
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    psf.main()
    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_main_allows_bash_relative_write_outside_hooks_dir(monkeypatch, capsys):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo ok > notes.txt"},
        "cwd": "/apps/some-app",
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    code = psf.main()
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == ""


def test_installer_script_allowlisted_even_though_it_writes_into_hooks_dir(monkeypatch, capsys):
    payload = {"tool_name": "Bash", "tool_input": {"command": "bash /apps/_ops/hooks-src/apply_hooks_v3.sh"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    code = psf.main()
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == ""


def test_merge_settings_hooks_script_allowlisted(monkeypatch, capsys):
    payload = {"tool_name": "Bash", "tool_input": {
        "command": "python3 /apps/_ops/hooks-src/merge_settings_hooks.py ~/.claude/settings.json ~/.claude/hooks"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    code = psf.main()
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == ""


def test_unrelated_command_mentioning_installer_name_is_not_allowlisted(monkeypatch, capsys):
    # The allowlist matches the ACTUAL script being run, not a free-text mention of its name.
    target = os.path.join(HOOKS_DIR, "block_dangerous.py")
    payload = {"tool_name": "Bash", "tool_input": {
        "command": f"echo 'see apply_hooks_v3.sh for details' > {target}"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    psf.main()
    data = json.loads(capsys.readouterr().out.strip())
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_main_denies_bash_edit_of_hooks_dir(monkeypatch, capsys):
    target = os.path.join(HOOKS_DIR, "block_dangerous.py")
    payload = {"tool_name": "Bash", "tool_input": {"command": f"echo oops > {target}"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    psf.main()
    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- check_wt_state_protect / work-copy state files (.wt.*) ----------------------------------
# team-lead instruction 2026-11-02 (사고①): a hand-written .wt.done once made wt-gc.sh trust a
# forged marker and delete an unlanded, uncommitted copy. These files are only ever written by
# wt-new.sh/wt-land.sh/wt-gc.sh FROM INSIDE their own subprocess - never as a session's own
# top-level Bash/Edit/Write tool call - so no allowlist exception is needed here (unlike the
# apply_hooks_v3.sh installer case above).

WT_DONE = "/apps/_wt/myapp/myname/.wt.done"
WT_SCOPE = "/apps/_wt/myapp/myname/.wt.scope"
WT_JSON = "/apps/_wt/myapp/myname/.wt.json"


def test_check_wt_state_protect_flags_wt_done():
    assert psf.check_wt_state_protect(WT_DONE) is not None


def test_check_wt_state_protect_flags_wt_scope():
    assert psf.check_wt_state_protect(WT_SCOPE) is not None


def test_check_wt_state_protect_ignores_ordinary_file_in_a_copy():
    assert psf.check_wt_state_protect("/apps/_wt/myapp/myname/src/app.ts") is None


def test_check_wt_state_protect_ignores_path_outside_wt_root():
    assert psf.check_wt_state_protect("/apps/myapp/.wt.done") is None


def test_main_denies_write_tool_forging_wt_done(monkeypatch, capsys):
    payload = {"tool_name": "Write", "tool_input": {"file_path": WT_DONE, "content": "2026-11-02T00:00:00"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    psf.main()
    data = json.loads(capsys.readouterr().out.strip())
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert ".wt.*" in data["hookSpecificOutput"]["permissionDecisionReason"]


def test_main_denies_bash_echo_redirect_forging_wt_done(monkeypatch, capsys):
    payload = {"tool_name": "Bash", "tool_input": {"command": f"date -Iseconds > {WT_DONE}"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    psf.main()
    data = json.loads(capsys.readouterr().out.strip())
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_main_denies_bash_sed_edit_of_wt_scope(monkeypatch, capsys):
    payload = {"tool_name": "Bash", "tool_input": {"command": f"sed -i 's/foo/bar/' {WT_SCOPE}"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    psf.main()
    data = json.loads(capsys.readouterr().out.strip())
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_main_denies_bash_rm_of_wt_json(monkeypatch, capsys):
    payload = {"tool_name": "Bash", "tool_input": {"command": f"rm {WT_JSON}"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    psf.main()
    data = json.loads(capsys.readouterr().out.strip())
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_main_allows_invoking_wt_new_script_itself(monkeypatch, capsys):
    # The top-level Bash tool call text for a legitimate `wt-new.sh app name` invocation never
    # itself names .wt.json/.wt.scope/.wt.done as a write target - those writes happen inside
    # wt-new.sh's OWN subprocess, invisible to this hook - so no allowlist entry is needed for it
    # to pass through cleanly.
    payload = {"tool_name": "Bash", "tool_input": {"command": "bash /apps/_shared/wt-new.sh myapp myname"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    code = psf.main()
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == ""


def test_main_allows_reading_wt_done(monkeypatch, capsys):
    # Read is never a write-target scan - inspecting a copy's state file is routine, safe work.
    payload = {"tool_name": "Read", "tool_input": {"file_path": WT_DONE}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    code = psf.main()
    out = capsys.readouterr().out.strip()
    assert code == 0


# --- app-source fence (반장 2026-11-03 08:40, WORK_QUEUE 32번) --------------------------------
# Deny: Edit into an app's src/, Bash echo-redirect into an app's tests/, Bash sed -i into an
# app's scripts/, Bash rm of a file under an app's prisma/. Allow: a write copy under /apps/
# _wt/**, the app's own docs/ or deploy.env, and the WT_LEAD_DIRECT=1 escape hatch.

def test_check_app_source_protect_flags_app_src():
    assert psf.check_app_source_protect("/apps/myapp/src/app.ts") is not None


def test_check_app_source_protect_ignores_work_copy():
    assert psf.check_app_source_protect("/apps/_wt/myapp/myname/src/app.ts") is None


def test_check_app_source_protect_ignores_app_docs():
    assert psf.check_app_source_protect("/apps/myapp/docs/ai/CURRENT_DECISIONS.md") is None


def test_check_app_source_protect_ignores_app_deploy_env():
    assert psf.check_app_source_protect("/apps/myapp/deploy.env") is None


def test_check_app_source_protect_honors_lead_direct_override(monkeypatch):
    monkeypatch.setenv("WT_LEAD_DIRECT", "1")
    assert psf.check_app_source_protect("/apps/myapp/src/app.ts") is None


def test_main_denies_edit_into_app_src(monkeypatch, capsys):
    payload = {"tool_name": "Edit", "tool_input": {
        "file_path": "/apps/myapp/src/app.ts", "old_string": "a", "new_string": "b"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    monkeypatch.delenv("WT_LEAD_DIRECT", raising=False)
    psf.main()
    data = json.loads(capsys.readouterr().out.strip())
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "wt-new.sh" in data["hookSpecificOutput"]["permissionDecisionReason"]


def test_main_denies_bash_echo_redirect_into_app_tests(monkeypatch, capsys):
    payload = {"tool_name": "Bash", "tool_input": {"command": "echo x > /apps/myapp/tests/foo.test.ts"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    monkeypatch.delenv("WT_LEAD_DIRECT", raising=False)
    psf.main()
    data = json.loads(capsys.readouterr().out.strip())
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_main_denies_bash_sed_edit_of_app_scripts(monkeypatch, capsys):
    payload = {"tool_name": "Bash", "tool_input": {"command": "sed -i 's/a/b/' /apps/myapp/scripts/deploy.sh"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    monkeypatch.delenv("WT_LEAD_DIRECT", raising=False)
    psf.main()
    data = json.loads(capsys.readouterr().out.strip())
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_main_denies_bash_rm_of_app_prisma_file(monkeypatch, capsys):
    payload = {"tool_name": "Bash", "tool_input": {"command": "rm /apps/myapp/prisma/schema.prisma"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    monkeypatch.delenv("WT_LEAD_DIRECT", raising=False)
    psf.main()
    data = json.loads(capsys.readouterr().out.strip())
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_main_allows_edit_into_work_copy_src(monkeypatch, capsys):
    payload = {"tool_name": "Edit", "tool_input": {
        "file_path": "/apps/_wt/myapp/myname/src/app.ts", "old_string": "a", "new_string": "b"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    monkeypatch.delenv("WT_LEAD_DIRECT", raising=False)
    code = psf.main()
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == ""


def test_main_allows_edit_into_app_docs(monkeypatch, capsys):
    payload = {"tool_name": "Edit", "tool_input": {
        "file_path": "/apps/myapp/docs/ai/TASKS.md", "old_string": "a", "new_string": "b"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    monkeypatch.delenv("WT_LEAD_DIRECT", raising=False)
    code = psf.main()
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == ""


def test_main_allows_edit_into_app_source_with_lead_direct_override(monkeypatch, capsys):
    payload = {"tool_name": "Edit", "tool_input": {
        "file_path": "/apps/myapp/src/app.ts", "old_string": "a", "new_string": "b"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(psf.gongbang_config, "load", lambda: dict(DEFAULT_CFG))
    monkeypatch.setenv("WT_LEAD_DIRECT", "1")
    code = psf.main()
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == ""
    assert out == ""
