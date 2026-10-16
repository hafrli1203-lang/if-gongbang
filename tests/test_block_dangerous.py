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


def test_main_fails_closed_on_internal_error(monkeypatch, capsys):
    import json
    import io
    monkeypatch.setattr(bd, "check", lambda cmd: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}})))
    code = bd.main()
    assert code == 2
