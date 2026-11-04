# Generated from the private ops workspace at 24c1bb68758c8a552514d06d8280463876278f76; do not edit by hand (see harness/export_ops_public.py).
import io
import json

import block_dangerous as bd


def test_rm_rf_root_is_blocked():
    reason, warn = bd.check("rm -rf /")
    assert reason is not None


def test_force_push_is_blocked():
    reason, warn = bd.check("git push origin main --force")
    assert reason is not None


def test_plain_rm_is_warned_not_blocked():
    reason, warn = bd.check("rm old-file.txt")
    assert reason is None
    assert warn is not None


def test_safe_command_passes():
    reason, warn = bd.check("ls -la")
    assert reason is None and warn is None


def test_main_exit_code_2_on_block(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}})))
    assert bd.main() == 2


# --- option-reordering resistance (item 17) ------------------------------------------------

def test_force_push_with_global_option_before_subcommand_is_blocked():
    reason, _ = bd.check("git -C . push --force origin main")
    assert reason is not None


def test_force_push_with_dash_c_override_is_blocked():
    reason, _ = bd.check("git -c user.name=x push --force origin main")
    assert reason is not None


def test_reset_hard_is_blocked():
    reason, _ = bd.check("git reset --hard")
    assert reason is not None


def test_rm_rf_root_with_split_flags_is_blocked():
    for cmd in ("rm -f -r /", "rm -fr /", "rm -r -f /"):
        reason, _ = bd.check(cmd)
        assert reason is not None, cmd


def test_rm_rf_bare_home_etc_usr_var_boot_is_blocked():
    for target in ("/home", "/etc", "/usr", "/var", "/boot"):
        reason, _ = bd.check(f"rm -rf {target}")
        assert reason is not None, target


# --- owner decision 2026-11-02: no approval branch exists anymore, so a recursive force-delete
# is hard-blocked by DEFAULT everywhere except a disposable work-copy (_wt) or scratchpad (/tmp)
# -------------------------------------------------------------------------------------------

def test_rm_rf_ordinary_absolute_project_path_is_blocked_by_default():
    reason, _ = bd.check("rm -rf /opt/app/build")
    assert reason is not None


def test_rm_rf_subfolder_under_a_protected_top_level_dir_is_blocked_by_default():
    reason, _ = bd.check("rm -rf /etc/myapp/old-config")
    assert reason is not None


def test_rm_rf_relative_dir_with_no_cwd_is_blocked_by_default():
    # No cwd means we cannot verify it resolves into a safe zone - fail CLOSED.
    reason, _ = bd.check("rm -rf build")
    assert reason is not None


def test_rm_rf_inside_a_work_copy_is_allowed():
    reason, _ = bd.check("rm -rf node_modules", cwd="/apps/_wt/myapp/some-task")
    assert reason is None


def test_rm_rf_absolute_path_inside_a_work_copy_is_allowed():
    reason, _ = bd.check("rm -rf /apps/_wt/myapp/some-task/dist")
    assert reason is None


def test_rm_rf_inside_scratchpad_tmp_is_allowed():
    reason, _ = bd.check("rm -rf /tmp/claude-1000/scratch/build")
    assert reason is None


def test_rm_rf_relative_dir_with_cwd_in_work_copy_is_allowed():
    reason, _ = bd.check("rm -rf build", cwd="/apps/_wt/myapp/some-task")
    assert reason is None


def test_rm_rf_relative_dir_with_cwd_outside_work_copy_is_blocked():
    reason, _ = bd.check("rm -rf build", cwd="/apps/myapp")
    assert reason is not None


def test_rm_rf_path_traversal_out_of_work_copy_is_blocked():
    # Enough ".." segments to normpath OUT of /apps/_wt entirely - must not be treated as safe.
    reason, _ = bd.check("rm -rf ../../../../etc", cwd="/apps/_wt/myapp/some-task")
    assert reason is not None


def test_rm_rf_path_traversal_still_inside_work_copy_is_allowed():
    reason, _ = bd.check("rm -rf ../../other-app", cwd="/apps/_wt/myapp/some-task")
    assert reason is None


# --- git stash / git add -A (owner decision 2026-11-02) --------------------------------------

def test_git_stash_is_blocked():
    reason, _ = bd.check("git stash")
    assert reason is not None


def test_git_stash_pop_is_blocked():
    reason, _ = bd.check("git stash pop")
    assert reason is not None


def test_git_add_dash_a_is_blocked():
    reason, _ = bd.check("git add -A")
    assert reason is not None


def test_git_add_dash_dash_all_is_blocked():
    reason, _ = bd.check("git add --all")
    assert reason is not None


def test_git_add_dot_is_blocked():
    reason, _ = bd.check("git add .")
    assert reason is not None


def test_git_add_specific_file_is_not_blocked():
    reason, _ = bd.check("git add src/main.py")
    assert reason is None


# --- catastrophic inner command behind a command-runner prefix chain (items 17/21) ---------

def test_rm_rf_root_behind_sudo_with_option_is_blocked():
    reason, _ = bd.check("sudo -u root rm -rf /")
    assert reason is not None


def test_rm_rf_root_reordered_flags_behind_sudo_is_blocked():
    reason, _ = bd.check("sudo -u root rm -fr /")
    assert reason is not None
    reason, _ = bd.check("sudo -u root rm -f -r /")
    assert reason is not None


def test_rm_rf_root_behind_env_with_option_value_is_blocked():
    reason, _ = bd.check("env -u FOO rm -rf /")
    assert reason is not None


def test_rm_rf_root_behind_nice_with_option_value_is_blocked():
    reason, _ = bd.check("nice -n 5 rm -rf /")
    assert reason is not None


def test_rm_rf_root_behind_timeout_with_duration_is_blocked():
    reason, _ = bd.check("timeout 10 rm -rf /")
    assert reason is not None


def test_rm_rf_root_behind_chained_prefixes_is_blocked():
    reason, _ = bd.check("sudo env X=1 nice -n 5 rm -rf /")
    assert reason is not None


def test_git_push_force_behind_sudo_is_blocked():
    reason, _ = bd.check("sudo git push --force")
    assert reason is not None


def test_nice_on_a_harmless_command_is_not_blocked():
    reason, warn = bd.check("nice -n 5 echo hello")
    assert reason is None and warn is None


def test_main_fails_closed_on_internal_error(monkeypatch):
    monkeypatch.setattr(bd, "check", lambda cmd: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "echo hi"}})))
    assert bd.main() == 2


def test_main_fails_closed_on_bad_json(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert bd.main() == 2


# --- pkill/killall: hard-blocked outright (item: team-lead "pkill·killall 하드 차단") ---------

def test_plain_pkill_is_blocked():
    reason, _ = bd.check("pkill node")
    assert reason is not None


def test_plain_killall_is_blocked():
    reason, _ = bd.check("killall python3")
    assert reason is not None


def test_pkill_behind_sudo_is_blocked():
    reason, _ = bd.check("sudo pkill node")
    assert reason is not None


def test_killall_reordered_options_is_blocked():
    reason, _ = bd.check("killall -v -9 myproc")
    assert reason is not None


def test_pkill_after_pipe_segment_is_blocked():
    reason, _ = bd.check("ps aux | pkill -f foo")
    assert reason is not None


def test_killall_after_and_chain_is_blocked():
    reason, _ = bd.check("echo hi && killall node")
    assert reason is not None


def test_pkill_mentioned_inside_quoted_echo_is_not_blocked():
    reason, warn = bd.check("echo 'do not pkill this process'")
    assert reason is None


def test_killall_mentioned_as_grep_argument_is_not_blocked():
    reason, warn = bd.check("grep -r killall scripts/")
    assert reason is None


# --- command substitution / nested shell (item 20) ------------------------------------------

def test_rm_rf_root_inside_dollar_paren_substitution_is_blocked():
    reason, _ = bd.check("echo $(rm -rf /)")
    assert reason is not None


def test_rm_rf_root_inside_backtick_substitution_is_blocked():
    reason, _ = bd.check("echo `rm -rf /`")
    assert reason is not None


def test_rm_rf_root_inside_bash_dash_c_is_blocked():
    reason, _ = bd.check("bash -c 'rm -rf /'")
    assert reason is not None


def test_pkill_inside_dollar_paren_substitution_is_blocked():
    reason, _ = bd.check("x=$(pkill node)")
    assert reason is not None


def test_ordinary_substitution_is_not_blocked():
    reason, warn = bd.check("echo $(date)")
    assert reason is None and warn is None


# --- work-copy guard (team-lead instruction 2026-11-02, 사고②) -----------------------------
# Another session's work copy (/apps/_wt/<app>/<name>) is never rebased/reset/checked-out/
# stashed by a direct Bash call - landing is wt-land.sh's job alone, no owner exception.

WT_COPY = "/apps/_wt/myapp/myname"


def test_git_rebase_via_cwd_in_wt_copy_is_blocked():
    reason, _ = bd.check("git rebase main", WT_COPY)
    assert reason is not None


def test_git_reset_via_dash_c_into_wt_copy_is_blocked_even_from_elsewhere():
    reason, _ = bd.check(f"git -C {WT_COPY} reset --soft HEAD~1", "/some/other/cwd")
    assert reason is not None


def test_git_checkout_branch_in_wt_copy_is_blocked():
    reason, _ = bd.check("git checkout main", WT_COPY)
    assert reason is not None


def test_git_stash_in_wt_copy_is_blocked():
    reason, _ = bd.check("git stash", WT_COPY)
    assert reason is not None


def test_git_rebase_outside_wt_root_is_only_warned_not_blocked():
    reason, warn = bd.check("git rebase main", "/apps/myapp")
    assert reason is None
    assert warn is not None


def test_git_checkout_outside_wt_root_is_not_blocked():
    reason, _ = bd.check("git checkout main", "/apps/myapp")
    assert reason is None


def test_wt_copy_guard_applies_to_the_copys_own_session_too():
    # No "it's my own copy" exception - landing is wt-land.sh's job alone, regardless of who
    # owns the copy.
    reason, _ = bd.check("git rebase main", WT_COPY)
    assert reason is not None and "wt-land.sh" in reason


def test_main_exit_code_2_on_rebase_in_wt_copy(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": "git rebase main"}, "cwd": WT_COPY})))
    assert bd.main() == 2
