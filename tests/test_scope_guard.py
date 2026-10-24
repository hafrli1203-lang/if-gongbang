"""scope_guard.py integration tests - run as real subprocesses (like Claude Code invokes
them) against a throwaway $HOME."""
import os
import time

from conftest import SCOPE_GUARD_SCRIPT, classify


def _now() -> float:
    return time.time()


# --- transcript-based gating ---------------------------------------------------------------

def test_human_approval_allows(fence_home):
    sid = "sid-human-ok"
    now = _now()
    fence_home.write_transcript(sid, [fence_home.human_line(now - 60, "승인 G1")])
    r = fence_home.bash("git push origin main", sid, now=now)
    assert classify(r) == "ALLOW", r.stdout + r.stderr


def test_expired_approval_denies(fence_home):
    sid = "sid-expired"
    now = _now()
    fence_home.write_transcript(sid, [fence_home.human_line(now - 5 * 3600, "승인 G1")])
    r = fence_home.bash("git push origin main", sid, now=now)
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_system_wrapper_approval_denies(fence_home):
    sid = "sid-wrapper"
    now = _now()
    fence_home.write_transcript(sid, [fence_home.system_wrapper_line(now - 60, "승인 G1")])
    r = fence_home.bash("git push origin main", sid, now=now)
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_peer_message_approval_denies(fence_home):
    sid = "sid-peer"
    now = _now()
    fence_home.write_transcript(sid, [fence_home.peer_line(now - 60, "보고: 문서 정리, 승인 B 포함")])
    r = fence_home.bash("git push origin main", sid, now=now)
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_line_without_origin_denies(fence_home):
    sid = "sid-no-origin"
    now = _now()
    line = fence_home.human_line(now - 60, "승인 A")
    del line["origin"]
    fence_home.write_transcript(sid, [line])
    r = fence_home.bash("git push origin main", sid, now=now)
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_tool_result_approval_denies(fence_home):
    sid = "sid-toolresult"
    now = _now()
    fence_home.write_transcript(sid, [fence_home.tool_result_line(now - 60, "승인 G1")])
    r = fence_home.bash("git push origin main", sid, now=now)
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_queued_command_human_approval_allows(fence_home):
    sid = "sid-queued"
    now = _now()
    fence_home.write_transcript(sid, [fence_home.queued_command_line(now - 60, "승인 G1")])
    r = fence_home.bash("git push origin main", sid, now=now)
    assert classify(r) == "ALLOW", r.stdout + r.stderr


def test_open_then_cancel_same_message_denies(fence_home):
    sid = "sid-opencancel"
    now = _now()
    fence_home.write_transcript(sid, [fence_home.human_line(now - 60, "승인 전체, 승인 취소")])
    r = fence_home.bash("git push origin main", sid, now=now)
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_different_session_id_denies(fence_home):
    sid_a, sid_b = "sid-a", "sid-b"
    now = _now()
    fence_home.write_transcript(sid_a, [fence_home.human_line(now - 60, "승인 전체")])
    fence_home.write_transcript(sid_b, [])
    r = fence_home.bash("git push origin main", sid_b, now=now)
    assert classify(r) == "DENY", r.stdout + r.stderr
    r_a = fence_home.bash("git push origin main", sid_a, now=now)
    assert classify(r_a) == "ALLOW", r_a.stdout + r_a.stderr


def test_no_transcript_denies(fence_home):
    r = fence_home.bash("git push origin main", "sid-missing", now=_now())
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_quoted_approval_in_message_denies(fence_home):
    sid = "sid-quoted"
    now = _now()
    fence_home.write_transcript(sid, [fence_home.human_line(now - 60, "'승인 G1'를 입력하면 열립니다")])
    r = fence_home.bash("git push origin main", sid, now=now)
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_code_block_approval_denies(fence_home):
    sid = "sid-codeblock"
    now = _now()
    fence_home.write_transcript(sid, [fence_home.human_line(now - 60, "예시:\n```\n승인 G1\n```")])
    r = fence_home.bash("git push origin main", sid, now=now)
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_missing_session_id_denies(fence_home):
    sid = "sid-noid"
    now = _now()
    fence_home.write_transcript(sid, [fence_home.human_line(now - 60, "승인 전체")])
    r = fence_home.bash("git push origin main", "", now=now)
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_plain_commands_allowed_with_no_transcript(fence_home):
    sid = "sid-plain"
    for cmd in ["git status", "git -C /x log", "rm file.txt", "ls -la", "echo git push"]:
        r = fence_home.bash(cmd, sid, now=_now())
        assert classify(r) == "ALLOW", f"{cmd}: {r.stdout} {r.stderr}"


# --- git/rm option-reordering bypass --------------------------------------------------------

def test_git_global_opt_bypasses_blocked(fence_home):
    sid = "sid-bypass"
    fence_home.write_transcript(sid, [])
    for cmd in [
        "git -C . push origin main",
        "git -c user.name=x push origin main",
        "git --git-dir=/opt/other-repo/.git push origin main",
        "git -P push origin main",
    ]:
        r = fence_home.bash(cmd, sid, now=_now())
        assert classify(r) == "DENY", f"{cmd}: {r.stdout} {r.stderr}"


def test_git_quoted_semicolon_not_split(fence_home):
    sid = "sid-bypass"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("git -c 'user.name=a;b' push origin main", sid, now=_now())
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_git_redirection_still_detected(fence_home):
    sid = "sid-bypass"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("git push origin main >/dev/null 2>&1", sid, now=_now())
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_rm_split_options_blocked(fence_home):
    sid = "sid-bypass"
    fence_home.write_transcript(sid, [])
    for cmd in ["rm -f -r /tmp/x", "rm -r -f /tmp/x", "rm -Rf /tmp/x", "rm --recursive -f /tmp/x", "rm --rec -f /tmp/x"]:
        r = fence_home.bash(cmd, sid, now=_now())
        assert classify(r) == "DENY", f"{cmd}: {r.stdout} {r.stderr}"


def test_branch_delete_force_variants_blocked(fence_home):
    sid = "sid-bypass"
    fence_home.write_transcript(sid, [])
    for cmd in ["git branch -D topic", "git branch -d --force topic"]:
        r = fence_home.bash(cmd, sid, now=_now())
        assert classify(r) == "DENY", f"{cmd}: {r.stdout} {r.stderr}"


def test_chained_command_each_segment_checked(fence_home):
    sid = "sid-bypass"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("echo hi && git push origin main", sid, now=_now())
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_nested_shell_c_detected(fence_home):
    sid = "sid-bypass"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("bash -c 'git push origin main'", sid, now=_now())
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_echo_git_push_not_denied(fence_home):
    sid = "sid-bypass"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("echo git push", sid, now=_now())
    assert classify(r) == "ALLOW", r.stdout + r.stderr


def test_assignment_prefixed_git_push_denied(fence_home):
    sid = "sid-bypass"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("FOO=1 git push origin main", sid, now=_now())
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_sudo_git_push_denied(fence_home):
    sid = "sid-bypass"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("sudo git push origin main", sid, now=_now())
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_sudo_with_user_option_rm_denied(fence_home):
    # sudo's own option VALUE ("root") must not be mistaken for the real command - rm must
    # still be gated even though sudo's "-u root" sits in front of it.
    sid = "sid-bypass"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("sudo -u root rm -rf x", sid, now=_now())
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_env_with_option_value_rm_denied(fence_home):
    # Before the option-arity table, env's "-u FOO" value token was mistaken for the command,
    # so this slipped through completely ungated.
    sid = "sid-bypass"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("env -u FOO rm -rf x", sid, now=_now())
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_nice_with_option_value_rm_denied(fence_home):
    sid = "sid-bypass"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("nice -n 5 rm -rf x", sid, now=_now())
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_ionice_with_option_value_rm_denied(fence_home):
    sid = "sid-bypass"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("ionice -c 3 rm -rf x", sid, now=_now())
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_timeout_with_duration_rm_denied(fence_home):
    sid = "sid-bypass"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("timeout 10 rm -rf x", sid, now=_now())
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_stdbuf_with_option_rm_denied(fence_home):
    sid = "sid-bypass"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("stdbuf -oL rm -rf x", sid, now=_now())
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_chained_prefixes_rm_denied(fence_home):
    sid = "sid-bypass"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("sudo -E env X=1 nice -n 5 rm -rf x", sid, now=_now())
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_nice_with_option_value_on_harmless_command_allowed(fence_home):
    # Negative control: a prefix's option VALUE must not itself be mistaken for a gated
    # command, and an ordinary command behind a prefix must not be over-blocked.
    sid = "sid-bypass"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("nice -n 5 echo hello", sid, now=_now())
    assert classify(r) == "ALLOW", r.stdout + r.stderr


def test_plain_git_status_allowed(fence_home):
    sid = "sid-bypass"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("git status", sid, now=_now())
    assert classify(r) == "ALLOW", r.stdout + r.stderr


def test_git_with_global_opt_log_allowed(fence_home):
    sid = "sid-bypass"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("git -C /opt/other-repo log", sid, now=_now())
    assert classify(r) == "ALLOW", r.stdout + r.stderr


def test_rm_single_file_allowed(fence_home):
    sid = "sid-bypass"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("rm file.txt", sid, now=_now())
    assert classify(r) == "ALLOW", r.stdout + r.stderr


def test_ls_allowed(fence_home):
    sid = "sid-bypass"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("ls -la", sid, now=_now())
    assert classify(r) == "ALLOW", r.stdout + r.stderr


# --- GATED_PATH: read vs write -------------------------------------------------------------

def test_reads_of_sensitive_paths_allowed(fence_home):
    sid = "sid-readwrite"
    fence_home.write_transcript(sid, [])
    for cmd in [
        "cat /etc/hosts", "less /etc/hosts", "grep root /etc/passwd", "head /etc/hosts",
        "ls -la /etc", "stat /etc/hosts", "python3 -c \"open('/etc/hosts').read()\"",
        "cat ~/.claude/settings.json",
    ]:
        r = fence_home.bash(cmd, sid, now=_now())
        assert classify(r) == "ALLOW", f"{cmd}: {r.stdout} {r.stderr}"


def test_echo_of_bash_dash_c_not_executed(fence_home):
    sid = "sid-readwrite"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("echo bash -c 'git push'", sid, now=_now())
    assert classify(r) == "ALLOW", r.stdout + r.stderr


def test_writes_to_sensitive_paths_denied(fence_home):
    sid = "sid-readwrite"
    fence_home.write_transcript(sid, [])
    for cmd in [
        "tee /etc/hosts", "cp source /etc/hosts", "mv source /etc/hosts",
        "sed -i 's/a/b/' /etc/hosts", "echo hi > /etc/hosts", "echo hi >> ~/.claude/settings.json",
        "chmod 644 /etc/hosts",
    ]:
        r = fence_home.bash(cmd, sid, now=_now())
        assert classify(r) == "DENY", f"{cmd}: {r.stdout} {r.stderr}"


def test_bash_dash_c_actually_run_still_detected(fence_home):
    sid = "sid-readwrite"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("bash -c 'git push origin main'", sid, now=_now())
    assert classify(r) == "DENY", r.stdout + r.stderr


# --- malformed stdin ------------------------------------------------------------------------

def test_malformed_stdin_denies(fence_home):
    import subprocess
    import sys
    env = dict(os.environ)
    env["HOME"] = fence_home.home
    env.pop("GONGBANG_CONFIG", None)
    result = subprocess.run(
        [sys.executable, SCOPE_GUARD_SCRIPT],
        input="{not valid json", env=env, capture_output=True, text=True, timeout=10,
    )
    assert classify(result) == "DENY", result.stdout + result.stderr


# --- hook-name / gate-dir protection, scoped to the REAL installed location only -----------

def test_cd_then_run_hook_blocked_even_with_open_gate(fence_home):
    sid = "sid-hookname"
    now = _now()
    fence_home.write_transcript(sid, [fence_home.human_line(now - 60, "승인 전체")])
    r = fence_home.bash("cd ~/.claude/hooks && python3 approve_gate.py", sid, now=now)
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_underscore_spelling_blocked_inside_real_hooks_dir(fence_home):
    sid = "sid-hookname"
    now = _now()
    fence_home.write_transcript(sid, [fence_home.human_line(now - 60, "승인 전체")])
    cwd = os.path.join(fence_home.home, ".claude", "hooks")
    r = fence_home.bash("cat scope_guard.py", sid, now=now, cwd=cwd)
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_fence_parse_name_protected(fence_home):
    sid = "sid-hookname"
    now = _now()
    fence_home.write_transcript(sid, [fence_home.human_line(now - 60, "승인 전체")])
    r = fence_home.bash("cat ~/.claude/hooks/fence_parse.py", sid, now=now)
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_same_name_file_elsewhere_allowed_bash(fence_home):
    # A repo/staging file that merely shares a hook's name is ordinary work, not a protected
    # install path.
    sid = "sid-hookname"
    now = _now()
    fence_home.write_transcript(sid, [fence_home.human_line(now - 60, "승인 전체")])
    for cmd in ("ls -la /opt/demo-repo/hooks/fence_parse.py", "python3 -m pytest tests/test_scope_guard.py",
                "cat scripts/hooks/library_nudge.py"):
        r = fence_home.bash(cmd, sid, now=now, cwd="/opt/demo-repo")
        assert classify(r) == "ALLOW", f"{cmd}: {r.stdout} {r.stderr}"


def test_same_name_file_elsewhere_allowed_edit(fence_home):
    sid = "sid-hookname"
    fence_home.write_transcript(sid, [fence_home.human_line(_now() - 60, "승인 전체")])
    for p in ("/opt/demo-repo/hooks/scope_guard.py", "/opt/demo-repo/scripts/hooks/lesson_capture.py"):
        r = fence_home.edit(p, sid)
        assert classify(r) == "ALLOW", f"{p}: {r.stdout} {r.stderr}"


def test_any_file_in_real_hooks_dir_blocked(fence_home):
    sid = "sid-hookname"
    fence_home.write_transcript(sid, [fence_home.human_line(_now() - 60, "승인 전체")])
    r = fence_home.edit(os.path.join(fence_home.home, ".claude", "hooks", "unrelated.py"), sid)
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_gate_dir_anywhere_under_home_blocked(fence_home):
    sid = "sid-hookname"
    now = _now()
    fence_home.write_transcript(sid, [fence_home.human_line(now - 60, "승인 전체")])
    cmd = f"echo hi > {fence_home.home}/.claude/gate/approved.json"
    r = fence_home.bash(cmd, sid, now=now)
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_edit_path_hook_name_blocked(fence_home):
    sid = "sid-hookname"
    fence_home.write_transcript(sid, [fence_home.human_line(_now() - 60, "승인 전체")])
    r = fence_home.edit(os.path.join(fence_home.home, ".claude", "hooks", "block_dangerous.py"), sid)
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_unrelated_file_with_open_gate_allowed(fence_home):
    sid = "sid-hookname"
    fence_home.write_transcript(sid, [fence_home.human_line(_now() - 60, "승인 전체")])
    r = fence_home.edit("/opt/other-repo/src/app.py", sid)
    assert classify(r) == "ALLOW", r.stdout + r.stderr


def test_readme_mentioning_hook_name_in_body_not_blocked(fence_home):
    # Hook-name protection applies to the Bash command / Edit file_path, NOT to the
    # new_string/content body - writing docs that mention approve_gate.py is fine.
    sid = "sid-hookname"
    fence_home.write_transcript(sid, [fence_home.human_line(_now() - 60, "승인 전체")])
    r = fence_home.edit(
        "/opt/other-repo/README.md", sid,
        old_string="old", new_string="See approve_gate.py and scope_guard for details.",
    )
    assert classify(r) == "ALLOW", r.stdout + r.stderr


# --- work-queue "[x]" completion-mark detection ---------------------------------------------

def test_multiedit_x_mark_blocked_without_approval(fence_home, tmp_path):
    sid = "sid-wq"
    wq = tmp_path / "WORK_QUEUE.md"
    wq.write_text("[ ] TASK1\n[ ] TASK2\n", encoding="utf-8")
    fence_home.write_transcript(sid, [])
    r = fence_home.multiedit(str(wq), [{"old_string": "[ ] TASK1", "new_string": "[x] TASK1"}], sid)
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_multiedit_x_mark_allowed_with_approval(fence_home, tmp_path):
    sid = "sid-wq"
    wq = tmp_path / "WORK_QUEUE.md"
    wq.write_text("[ ] TASK1\n[ ] TASK2\n", encoding="utf-8")
    now = _now()
    fence_home.write_transcript(sid, [fence_home.human_line(now - 60, "승인 전체")])
    payload = {"tool_name": "MultiEdit", "tool_input": {"file_path": str(wq),
               "edits": [{"old_string": "[ ] TASK1", "new_string": "[x] TASK1"}]}, "session_id": sid}
    r = fence_home.run(SCOPE_GUARD_SCRIPT, payload, now=now)
    assert classify(r) == "ALLOW", r.stdout + r.stderr


def test_multiedit_split_boundary_bypass_blocked(fence_home, tmp_path):
    sid = "sid-wq"
    wq = tmp_path / "WORK_QUEUE.md"
    wq.write_text("[ ] TASK1\n[ ] TASK2\n", encoding="utf-8")
    fence_home.write_transcript(sid, [])
    r2 = fence_home.edit(str(wq), sid, old_string=" ] TASK1", new_string="x] TASK1")
    assert classify(r2) == "DENY", r2.stdout + r2.stderr


def test_unrelated_edit_to_other_file_allowed(fence_home, tmp_path):
    sid = "sid-wq"
    fence_home.write_transcript(sid, [])
    other = tmp_path / "notes.md"
    other.write_text("hello\n", encoding="utf-8")
    r = fence_home.edit(str(other), sid, old_string="hello", new_string="hi [x] done")
    assert classify(r) == "ALLOW", r.stdout + r.stderr


# --- work-queue "[x]" completion-mark via Bash (sed/perl -i, redirection) -------------------

def test_bash_sed_inplace_x_mark_blocked_without_approval(fence_home, tmp_path):
    sid = "sid-wq-bash"
    wq = tmp_path / "WORK_QUEUE.md"
    wq.write_text("[ ] TASK1\n", encoding="utf-8")
    fence_home.write_transcript(sid, [])
    r = fence_home.bash(f"sed -i 's/\\[ \\] TASK1/[x] TASK1/' {wq}", sid)
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_bash_sed_inplace_x_mark_allowed_with_approval(fence_home, tmp_path):
    sid = "sid-wq-bash"
    wq = tmp_path / "WORK_QUEUE.md"
    wq.write_text("[ ] TASK1\n", encoding="utf-8")
    now = _now()
    fence_home.write_transcript(sid, [fence_home.human_line(now - 60, "승인 전체")])
    r = fence_home.bash(f"sed -i 's/\\[ \\] TASK1/[x] TASK1/' {wq}", sid, now=now)
    assert classify(r) == "ALLOW", r.stdout + r.stderr


def test_bash_redirect_x_mark_blocked_without_approval(fence_home, tmp_path):
    sid = "sid-wq-bash"
    wq = tmp_path / "WORK_QUEUE.md"
    wq.write_text("[ ] TASK1\n", encoding="utf-8")
    fence_home.write_transcript(sid, [])
    r = fence_home.bash(f"echo '[x] TASK1' >> {wq}", sid)
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_bash_x_mark_without_write_target_is_not_flagged(fence_home, tmp_path):
    # The literal "[x]" text alone, with no write targeting the work-queue file, is ordinary
    # work (e.g. printing or grepping it) and must not be gated.
    sid = "sid-wq-bash"
    wq = tmp_path / "WORK_QUEUE.md"
    wq.write_text("[ ] TASK1\n", encoding="utf-8")
    fence_home.write_transcript(sid, [])
    r = fence_home.bash(f"grep '[x]' {wq}", sid)
    assert classify(r) == "ALLOW", r.stdout + r.stderr


def test_bash_x_mark_read_in_one_segment_does_not_mask_unrelated_write(fence_home, tmp_path):
    # Reading an existing "[x]" mark in one segment, followed by an unrelated write that adds a
    # normal (not-yet-done) task in another segment, must not be judged by the whole command
    # string - only the segment that actually writes to the work-queue file matters, and that
    # segment's own text has no "[x]" in it.
    sid = "sid-wq-bash"
    wq = tmp_path / "WORK_QUEUE.md"
    wq.write_text("[x] TASK1\n", encoding="utf-8")
    fence_home.write_transcript(sid, [])
    r = fence_home.bash(f"grep -F '[x]' {wq}; echo '[ ] TASK2' >> {wq}", sid)
    assert classify(r) == "ALLOW", r.stdout + r.stderr


def test_bash_sed_long_option_inplace_x_mark_blocked_without_approval(fence_home, tmp_path):
    sid = "sid-wq-bash"
    wq = tmp_path / "WORK_QUEUE.md"
    wq.write_text("[ ] TASK1\n", encoding="utf-8")
    fence_home.write_transcript(sid, [])
    r = fence_home.bash(f"sed --in-place 's/\\[ \\] TASK1/[x] TASK1/' {wq}", sid)
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_bash_sed_inplace_with_backup_suffix_x_mark_blocked_without_approval(fence_home, tmp_path):
    sid = "sid-wq-bash"
    wq = tmp_path / "WORK_QUEUE.md"
    wq.write_text("[ ] TASK1\n", encoding="utf-8")
    fence_home.write_transcript(sid, [])
    r = fence_home.bash(f"sed -i.bak 's/\\[ \\] TASK1/[x] TASK1/' {wq}", sid)
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_bash_perl_bundled_pi_x_mark_blocked_without_approval(fence_home, tmp_path):
    sid = "sid-wq-bash"
    wq = tmp_path / "WORK_QUEUE.md"
    wq.write_text("[ ] TASK1\n", encoding="utf-8")
    fence_home.write_transcript(sid, [])
    r = fence_home.bash(f"perl -pi -e 's/\\[ \\] TASK1/[x] TASK1/' {wq}", sid)
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_bash_perl_separate_dash_i_x_mark_blocked_without_approval(fence_home, tmp_path):
    sid = "sid-wq-bash"
    wq = tmp_path / "WORK_QUEUE.md"
    wq.write_text("[ ] TASK1\n", encoding="utf-8")
    fence_home.write_transcript(sid, [])
    r = fence_home.bash(f"perl -i -pe 's/\\[ \\] TASK1/[x] TASK1/' {wq}", sid)
    assert classify(r) == "DENY", r.stdout + r.stderr


# --- opaque constructs (command substitution / eval / backticks) + gate keyword ------------

def test_command_substitution_with_gate_keyword_denied(fence_home):
    sid = "sid-opaque"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("git $(echo push)", sid, now=_now())
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_eval_with_gate_keyword_denied(fence_home):
    sid = "sid-opaque"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash('eval "git push"', sid, now=_now())
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_backtick_command_name_with_gate_keyword_denied(fence_home):
    sid = "sid-opaque"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("`echo rm` -rf x", sid, now=_now())
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_command_substitution_without_gate_keyword_allowed(fence_home):
    sid = "sid-opaque"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("echo $(date)", sid, now=_now())
    assert classify(r) == "ALLOW", r.stdout + r.stderr


def test_command_substitution_in_pwd_without_gate_keyword_allowed(fence_home):
    sid = "sid-opaque"
    fence_home.write_transcript(sid, [])
    r = fence_home.bash("ls $(pwd)", sid, now=_now())
    assert classify(r) == "ALLOW", r.stdout + r.stderr


def test_command_substitution_gate_keyword_allowed_with_matching_approval(fence_home):
    sid = "sid-opaque-ok"
    now = _now()
    fence_home.write_transcript(sid, [fence_home.human_line(now - 60, "승인 G1")])
    r = fence_home.bash("git $(echo push)", sid, now=now)
    assert classify(r) == "ALLOW", r.stdout + r.stderr


# --- project-scoped approval -----------------------------------------------------------------

def _make_project(fence_home, name: str) -> str:
    apps_root = os.path.join(fence_home.home, "apps")
    project_dir = os.path.join(apps_root, name)
    os.makedirs(project_dir, exist_ok=True)
    return project_dir


def test_project_name_approval_allows_inside_that_project(fence_home):
    project_dir = _make_project(fence_home, "myproj")
    sid = "sid-scope-ok"
    now = _now()
    fence_home.write_transcript(sid, [fence_home.human_line(now - 60, "승인 myproj")])
    r = fence_home.bash("git push origin main", sid, now=now, cwd=project_dir)
    assert classify(r) == "ALLOW", r.stdout + r.stderr


def test_project_name_approval_case_insensitive(fence_home):
    project_dir = _make_project(fence_home, "MyProj")
    sid = "sid-scope-case"
    now = _now()
    fence_home.write_transcript(sid, [fence_home.human_line(now - 60, "승인 myproj")])
    r = fence_home.bash("git push origin main", sid, now=now, cwd=project_dir)
    assert classify(r) == "ALLOW", r.stdout + r.stderr


def test_project_name_approval_denies_other_project(fence_home):
    _make_project(fence_home, "myproj")
    other_dir = _make_project(fence_home, "otherproj")
    sid = "sid-scope-deny"
    now = _now()
    fence_home.write_transcript(sid, [fence_home.human_line(now - 60, "승인 myproj")])
    r = fence_home.bash("git push origin main", sid, now=now, cwd=other_dir)
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_project_name_approval_denies_when_no_cwd(fence_home):
    _make_project(fence_home, "myproj")
    sid = "sid-scope-nocwd"
    now = _now()
    fence_home.write_transcript(sid, [fence_home.human_line(now - 60, "승인 myproj")])
    r = fence_home.bash("git push origin main", sid, now=now, cwd="")
    assert classify(r) == "DENY", r.stdout + r.stderr


def test_project_name_approval_allows_edit_path_inside_project(fence_home):
    project_dir = _make_project(fence_home, "myproj")
    settings_path = os.path.join(project_dir, ".env")
    sid = "sid-scope-edit"
    now = _now()
    fence_home.write_transcript(sid, [fence_home.human_line(now - 60, "승인 myproj")])
    r = fence_home.run(
        SCOPE_GUARD_SCRIPT,
        {"tool_name": "Edit", "tool_input": {"file_path": settings_path}, "session_id": sid},
        now=now,
    )
    assert classify(r) == "ALLOW", r.stdout + r.stderr


def test_generic_name_approval_stays_session_wide(fence_home):
    _make_project(fence_home, "myproj")
    other_dir = _make_project(fence_home, "otherproj")
    sid = "sid-scope-generic"
    now = _now()
    fence_home.write_transcript(sid, [fence_home.human_line(now - 60, "승인 G1")])
    r = fence_home.bash("git push origin main", sid, now=now, cwd=other_dir)
    assert classify(r) == "ALLOW", r.stdout + r.stderr
