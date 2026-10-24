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


def test_main_exit_code_2_on_block(monkeypatch, capsys):
    import json
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}})))
    code = bd.main()
    assert code == 2


# --- option-reordering resistance ----------------------------------------------------------

def test_force_push_with_global_option_before_subcommand_is_blocked():
    reason, _ = bd.check("git -C . push --force origin main")
    assert reason is not None


def test_force_push_with_dash_c_override_is_blocked():
    reason, _ = bd.check("git -c user.name=x push --force origin main")
    assert reason is not None


def test_reset_hard_with_split_option_order_is_blocked():
    reason, _ = bd.check("git reset --hard")
    assert reason is not None


def test_rm_rf_root_with_split_flags_is_blocked():
    for cmd in ("rm -f -r /", "rm -fr /", "rm -r -f /"):
        reason, _ = bd.check(cmd)
        assert reason is not None, cmd


def test_rm_rf_subfolder_of_absolute_path_is_not_hard_blocked():
    # rm -rf under an ordinary absolute project subfolder is not hard-blocked here - it goes
    # through scope_guard.py's approval gate instead, same as the relative-path case already
    # covered by test_rm_rf_ordinary_relative_dir_is_not_hard_blocked.
    reason, warn = bd.check("rm -rf /opt/app/build")
    assert reason is None
    assert warn is not None  # the generic "rm used" warning still fires


def test_rm_rf_bare_home_is_still_blocked():
    reason, _ = bd.check("rm -rf /home")
    assert reason is not None


def test_rm_rf_bare_etc_usr_var_boot_is_still_blocked():
    for target in ("/etc", "/usr", "/var", "/boot"):
        reason, _ = bd.check(f"rm -rf {target}")
        assert reason is not None, target


def test_rm_rf_subfolder_under_a_protected_top_level_dir_is_not_hard_blocked():
    reason, _ = bd.check("rm -rf /etc/myapp/old-config")
    assert reason is None


def test_rm_rf_ordinary_relative_dir_is_not_hard_blocked():
    # A generic recursive delete of a normal, non-absolute path is scope_guard's job
    # (approval-gated), not block_dangerous's (which hard-blocks only true catastrophic
    # targets: /, ~, ., *, $VAR, and the bare top-level system/home directories listed above -
    # see test_rm_rf_subfolder_of_absolute_path_is_not_hard_blocked for the absolute-path case).
    reason, _ = bd.check("rm -rf build")
    assert reason is None


# --- catastrophic inner command behind a command-runner prefix chain ----------------------
# A prefix like sudo/env/nice/ionice/timeout/stdbuf must never hide a catastrophic rm/git
# command from the structural check - the inner command is always evaluated, no matter how
# many of these are chained in front of it.

def test_rm_rf_root_behind_sudo_with_option_is_blocked():
    reason, _ = bd.check("sudo -u root rm -rf /")
    assert reason is not None


def test_rm_rf_root_reordered_flags_behind_sudo_is_blocked():
    # -fr (reordered) and split -f -r must still be caught, not just the literal "-rf" substring.
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


def test_rm_rf_root_behind_ionice_with_option_value_is_blocked():
    reason, _ = bd.check("ionice -c 3 rm -rf /")
    assert reason is not None


def test_rm_rf_root_behind_timeout_with_duration_is_blocked():
    reason, _ = bd.check("timeout 10 rm -rf /")
    assert reason is not None


def test_rm_rf_root_behind_stdbuf_with_option_is_blocked():
    reason, _ = bd.check("stdbuf -oL rm -rf /")
    assert reason is not None
    reason, _ = bd.check("stdbuf -o L rm -rf /")
    assert reason is not None


def test_rm_rf_root_behind_chained_prefixes_is_blocked():
    reason, _ = bd.check("sudo -E env X=1 nice -n 5 rm -rf /")
    assert reason is not None


def test_git_push_force_behind_sudo_with_option_is_blocked():
    reason, _ = bd.check("sudo -u root git push --force")
    assert reason is not None


def test_git_reset_hard_behind_sudo_with_option_is_blocked():
    reason, _ = bd.check("sudo -u root git reset --hard")
    assert reason is not None


def test_nice_with_option_value_on_a_harmless_command_is_not_blocked():
    # Negative control: a prefix's own option value must not be mistaken for a dangerous
    # command, and an ordinary command behind a prefix must not be over-blocked.
    reason, warn = bd.check("nice -n 5 echo hello")
    assert reason is None and warn is None


def test_main_fails_closed_on_internal_error(monkeypatch, capsys):
    import json
    import io
    monkeypatch.setattr(bd, "check", lambda cmd: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}})))
    code = bd.main()
    assert code == 2
