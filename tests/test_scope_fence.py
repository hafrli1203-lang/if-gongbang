# Generated from the private ops workspace at b94af62b97af64e2e58e8c102fa2538f066ae366; do not edit by hand (see harness/export_ops_public.py).
"""Tests for scope_fence.py Part 2 (work-copy session fence) -- v2 gap bundle 4
(items 105/110/111/158/159/167/214). Part 1 (CURRENT_SCOPE.json app allow-list) keeps its own
small regression check so a Part 2 change cannot silently break it.

Run: /apps/if-gongbang-v2/.venv/bin/python -m pytest hooks-src/tests/test_scope_fence.py -v
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

HOOK_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks", "scope_fence.py")
spec = importlib.util.spec_from_file_location("scope_fence", HOOK_PATH)
sf = importlib.util.module_from_spec(spec)
sys.modules["scope_fence"] = sf
spec.loader.exec_module(sf)


@pytest.fixture
def copy(tmp_path, monkeypatch):
    """A fake work copy /apps/_wt/<app>/<name>-like dir under tmp_path, with a .wt.scope
    allowing src/** and package.json literally."""
    wt_root = tmp_path / "_wt"
    root = wt_root / "demoapp" / "job1"
    (root / "src").mkdir(parents=True)
    (root / "node_modules").mkdir()
    (root / "src" / "allowed.ts").write_text("ok")
    (root / "src" / "nested").mkdir()
    (root / "src" / "nested" / "deep.ts").write_text("ok")
    (root / "outside.ts").write_text("not in scope")
    (root / ".wt.scope").write_text("src/**\npackage.json\n")
    (root / "package.json").write_text("{}")
    other = wt_root / "demoapp" / "job2"
    other.mkdir(parents=True)
    (other / "secret.txt").write_text("other copy's job")

    other_app = tmp_path / "apps" / "otherapp"
    other_app.mkdir(parents=True)

    projects_dir = tmp_path / "claude_projects"
    projects_dir.mkdir()
    (projects_dir / "memory.md").write_text("private")

    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    (token_dir / "naver.json").write_text("secret-token")

    monkeypatch.setattr(sf, "WT_ROOT", str(wt_root))
    monkeypatch.setattr(sf, "TOKEN_DIR", str(token_dir))
    monkeypatch.setattr(sf, "CLAUDE_PROJECTS_DIR", str(projects_dir))
    monkeypatch.setattr(sf, "OFF", str(tmp_path / "off-switch-never-exists"))
    # Copy-session detection is WT_COPY-only (team-lead 2026-11-02 fix) -- set it by default so
    # the many tests below that only pass `cwd` keep exercising the fenced path; the few tests
    # that specifically need a NON-copy session delenv it themselves.
    monkeypatch.setenv("WT_COPY", str(root))
    return {
        "root": str(root),
        "other_copy": str(other),
        "other_app": str(other_app),
        "projects_dir": str(projects_dir),
        "token_dir": str(token_dir),
    }


def _call(data):
    return sf.decide(data)


# ---------------------------------------------------------------------------
# Not a work-copy session at all -> Part 2 must not touch anything
# ---------------------------------------------------------------------------

def test_non_copy_session_edit_passes(copy, tmp_path, monkeypatch):
    monkeypatch.delenv("WT_COPY", raising=False)
    out = _call({"tool_name": "Edit", "cwd": str(tmp_path / "apps" / "someapp"),
                 "tool_input": {"file_path": str(tmp_path / "apps" / "someapp" / "x.ts")}})
    assert out is None


def test_non_copy_session_bash_write_passes(copy, tmp_path, monkeypatch):
    monkeypatch.delenv("WT_COPY", raising=False)
    out = _call({"tool_name": "Bash", "cwd": str(tmp_path / "apps" / "someapp"),
                 "tool_input": {"command": "echo hi > out.txt"}})
    assert out is None


def test_cwd_inside_copy_without_wt_copy_env_not_fenced(copy, monkeypatch):
    """Regression for the live false positive (team-lead 2026-11-02): a session whose `cwd`
    happens to sit inside a work copy, but WT_COPY is unset (it only `cd`d there to look, or is
    the lead's own session), must NOT be fenced -- a write outside the copy's .wt.scope passes."""
    monkeypatch.delenv("WT_COPY", raising=False)
    out = _call({"tool_name": "Write", "cwd": copy["root"],
                 "tool_input": {"file_path": os.path.join(copy["root"], "outside.ts")}})
    assert out is None


def test_cwd_inside_copy_without_wt_copy_env_bash_not_fenced(copy, monkeypatch):
    """Same regression, the exact shape reported live: `bash deploy.sh app > $L` from a cwd
    inside a copy, no WT_COPY -- must pass, not be denied as "범위 밖 쓰기"."""
    monkeypatch.delenv("WT_COPY", raising=False)
    out = _call({"tool_name": "Bash", "cwd": copy["root"],
                 "tool_input": {"command": "bash deploy.sh app > $L"}})
    assert out is None


# ---------------------------------------------------------------------------
# WRITES inside a work-copy session (Edit/Write/MultiEdit)
# ---------------------------------------------------------------------------

def test_edit_in_scope_glob_allowed(copy):
    out = _call({"tool_name": "Edit", "cwd": copy["root"],
                 "tool_input": {"file_path": os.path.join(copy["root"], "src", "allowed.ts")}})
    assert out is None


def test_edit_in_scope_nested_glob_allowed(copy):
    out = _call({"tool_name": "Edit", "cwd": copy["root"],
                 "tool_input": {"file_path": os.path.join(copy["root"], "src", "nested", "deep.ts")}})
    assert out is None


def test_edit_outside_scope_glob_denied(copy):
    out = _call({"tool_name": "Write", "cwd": copy["root"],
                 "tool_input": {"file_path": os.path.join(copy["root"], "outside.ts")}})
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert ".wt.scope" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_edit_literal_package_json_allowed(copy):
    out = _call({"tool_name": "Edit", "cwd": copy["root"],
                 "tool_input": {"file_path": os.path.join(copy["root"], "package.json")}})
    assert out is None


def test_edit_copy_artifact_node_modules_allowed(copy):
    out = _call({"tool_name": "Write", "cwd": copy["root"],
                 "tool_input": {"file_path": os.path.join(copy["root"], "node_modules", "pkg", "index.js")}})
    assert out is None


def test_edit_outside_copy_root_denied(copy):
    out = _call({"tool_name": "Edit", "cwd": copy["root"],
                 "tool_input": {"file_path": os.path.join(copy["other_app"], "file.ts")}})
    assert out is not None
    assert "사본 밖" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_edit_another_copy_denied(copy):
    out = _call({"tool_name": "Edit", "cwd": copy["root"],
                 "tool_input": {"file_path": os.path.join(copy["other_copy"], "secret.txt")}})
    assert out is not None


def test_edit_scratchpad_allowed(copy, monkeypatch):
    monkeypatch.setattr(sf, "SCRATCH_PREFIX", "/tmp/claude")
    out = _call({"tool_name": "Write", "cwd": copy["root"],
                 "tool_input": {"file_path": "/tmp/claude-1000/scratch/tmpfile.txt"}})
    assert out is None


def test_edit_env_file_denied_even_if_in_scope_dir(copy):
    envfile = os.path.join(copy["root"], "src", ".env")
    out = _call({"tool_name": "Edit", "cwd": copy["root"], "tool_input": {"file_path": envfile}})
    assert out is not None
    assert "보호 경로" in out["hookSpecificOutput"]["permissionDecisionReason"]


# ---------------------------------------------------------------------------
# READS inside a work-copy session
# ---------------------------------------------------------------------------

def test_read_normal_file_allowed(copy):
    out = _call({"tool_name": "Read", "cwd": copy["root"],
                 "tool_input": {"file_path": os.path.join(copy["root"], "src", "allowed.ts")}})
    assert out is None


def test_read_env_file_denied(copy):
    out = _call({"tool_name": "Read", "cwd": copy["root"],
                 "tool_input": {"file_path": os.path.join(copy["root"], ".env")}})
    assert out is not None
    assert "읽기 금지" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_read_token_dir_denied(copy):
    out = _call({"tool_name": "Read", "cwd": copy["root"],
                 "tool_input": {"file_path": os.path.join(copy["token_dir"], "naver.json")}})
    assert out is not None


def test_read_other_copy_denied(copy):
    out = _call({"tool_name": "Read", "cwd": copy["root"],
                 "tool_input": {"file_path": os.path.join(copy["other_copy"], "secret.txt")}})
    assert out is not None


def test_read_claude_projects_denied(copy):
    out = _call({"tool_name": "Read", "cwd": copy["root"],
                 "tool_input": {"file_path": os.path.join(copy["projects_dir"], "memory.md")}})
    assert out is not None


def test_read_outside_copy_but_not_forbidden_allowed(copy):
    """Reading another app's ordinary code is NOT fenced -- only the four protected categories
    are (team-lead spec: "읽기는 .env*·토큰파일·다른 사본·~/.claude/projects/만 거부")."""
    out = _call({"tool_name": "Read", "cwd": copy["root"],
                 "tool_input": {"file_path": os.path.join(copy["other_app"], "readme.md")}})
    assert out is None


# ---------------------------------------------------------------------------
# Bash: redirection / tee / sed -i / cp / mv / mkdir
# ---------------------------------------------------------------------------

def test_bash_redirect_into_scope_allowed(copy):
    out = _call({"tool_name": "Bash", "cwd": copy["root"],
                 "tool_input": {"command": "echo hi > src/allowed.ts"}})
    assert out is None


def test_bash_redirect_outside_scope_denied(copy):
    out = _call({"tool_name": "Bash", "cwd": copy["root"],
                 "tool_input": {"command": "echo hi > outside.ts"}})
    assert out is not None
    assert ".wt.scope" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_bash_redirect_outside_copy_root_denied(copy):
    out = _call({"tool_name": "Bash", "cwd": copy["root"],
                 "tool_input": {"command": f"echo hi > {copy['other_app']}/x.txt"}})
    assert out is not None
    assert "사본 밖" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_bash_sed_i_outside_scope_denied(copy):
    out = _call({"tool_name": "Bash", "cwd": copy["root"],
                 "tool_input": {"command": "sed -i 's/a/b/' outside.ts"}})
    assert out is not None


def test_bash_sed_i_in_scope_allowed(copy):
    out = _call({"tool_name": "Bash", "cwd": copy["root"],
                 "tool_input": {"command": "sed -i 's/a/b/' src/allowed.ts"}})
    assert out is None


def test_bash_cp_dest_outside_scope_denied(copy):
    out = _call({"tool_name": "Bash", "cwd": copy["root"],
                 "tool_input": {"command": "cp src/allowed.ts outside.ts"}})
    assert out is not None


def test_bash_cp_dest_in_scope_allowed(copy):
    out = _call({"tool_name": "Bash", "cwd": copy["root"],
                 "tool_input": {"command": "cp src/allowed.ts src/nested/deep.ts"}})
    assert out is None


def test_bash_mkdir_outside_scope_denied(copy):
    out = _call({"tool_name": "Bash", "cwd": copy["root"],
                 "tool_input": {"command": "mkdir newtopdir"}})
    assert out is not None


def test_bash_mkdir_in_scope_allowed(copy):
    out = _call({"tool_name": "Bash", "cwd": copy["root"],
                 "tool_input": {"command": "mkdir -p src/newsub"}})
    assert out is None


def test_bash_tee_outside_scope_denied(copy):
    out = _call({"tool_name": "Bash", "cwd": copy["root"],
                 "tool_input": {"command": "echo hi | tee outside.ts"}})
    assert out is not None


def test_bash_env_mention_denied_even_without_write_target_match(copy):
    out = _call({"tool_name": "Bash", "cwd": copy["root"],
                 "tool_input": {"command": "cp ../../../apps/otherapp/.env src/allowed.ts"}})
    assert out is not None
    assert "읽기 금지" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_bash_token_dir_mention_denied(copy):
    cmd = f"cat {copy['token_dir']}/naver.json > src/allowed.ts"
    out = _call({"tool_name": "Bash", "cwd": copy["root"], "tool_input": {"command": cmd}})
    assert out is not None
    assert "읽기 금지" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_bash_read_only_command_passes(copy):
    out = _call({"tool_name": "Bash", "cwd": copy["root"], "tool_input": {"command": "cat src/allowed.ts"}})
    assert out is None


# ---------------------------------------------------------------------------
# Bypass attempts
# ---------------------------------------------------------------------------

def test_bash_variable_assigned_in_same_command_is_tracked(copy):
    """Codex 2026-11-02 P1: a variable this SAME command assigns a literal value to must be
    substituted before the "cannot judge" rule applies -- `L=outside.ts; echo hi > "$L"` was
    passing through as an unresolved $VAR when it is in fact a plain, resolvable bypass."""
    out = _call({"tool_name": "Bash", "cwd": copy["root"],
                 "tool_input": {"command": 'L=outside.ts; echo hi > "$L"'}})
    assert out is not None
    assert ".wt.scope" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_bash_no_space_redirect_denied(copy):
    """Codex P1: shlex only splits on whitespace, so `echo hi>outside.ts` (operator glued to the
    word, no space) used to hide the redirection as part of one opaque token."""
    out = _call({"tool_name": "Bash", "cwd": copy["root"],
                 "tool_input": {"command": "echo hi>outside.ts"}})
    assert out is not None


def test_bash_command_wrapper_rm_denied(copy):
    """Codex P1: `command`/absolute-path/`env`/a leading `VAR=val` in front of `rm` all defeated
    the plain `tokens[0] == "rm"` check."""
    for cmd in ("command rm outside.ts", "/usr/bin/rm outside.ts", "env rm outside.ts",
                "LC_ALL=C rm outside.ts"):
        out = _call({"tool_name": "Bash", "cwd": copy["root"], "tool_input": {"command": cmd}})
        assert out is not None, f"should deny: {cmd}"


def test_bash_mv_source_outside_scope_denied(copy):
    """Codex P1: `mv` DELETES its source -- only the destination was ever checked, so moving an
    out-of-scope file INTO scope (hiding/destroying it) passed."""
    out = _call({"tool_name": "Bash", "cwd": copy["root"],
                 "tool_input": {"command": "mv outside.ts src/allowed.ts"}})
    assert out is not None


def test_bash_cp_target_directory_flag_denied(copy):
    """Codex P1: `cp -t DIR src...` (GNU target-directory form) was reading the wrong token as
    the destination and missing that DIR itself was ever written to."""
    out = _call({"tool_name": "Bash", "cwd": copy["root"],
                 "tool_input": {"command": f"cp -t {copy['other_app']} src/allowed.ts"}})
    assert out is not None
    assert "사본 밖" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_bash_cd_then_write_in_other_real_dir_denied(copy):
    """Codex P1: after `_split_segments` breaks `cd DIR && cmd` into two segments, the second
    segment's relative path used to resolve against the ORIGINAL cwd (the copy), not DIR -- so
    writing into a real sibling app via `cd` was invisible to the fence."""
    out = _call({"tool_name": "Bash", "cwd": copy["root"],
                 "tool_input": {"command": f"cd {copy['other_app']} && echo hi > src/main.ts"}})
    assert out is not None
    assert "사본 밖" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_bash_subshell_cd_bypass_denied(copy):
    """Codex 2026-11-02 second-pass review + team-lead P1 verdict: subshell/grouping syntax
    (`(`, `)`, command substitution's `$(`) is not something _split_segments understands, so a
    `cd` inside one lost cwd tracking and could write outside the copy unnoticed. A write-signal
    command containing any paren is now "판정 불가" -> denied outright in a copy session."""
    out = _call({"tool_name": "Bash", "cwd": copy["root"],
                 "tool_input": {"command": "cd src && (cd ..; echo x > outside.ts)"}})
    assert out is not None
    assert "괄호" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_bash_paren_in_non_copy_session_passes(copy, monkeypatch):
    """The paren-denial is a Part 2 (copy-session) rule only -- a lead session (WT_COPY unset)
    running the exact same subshell command is entirely unaffected."""
    monkeypatch.delenv("WT_COPY", raising=False)
    out = _call({"tool_name": "Bash", "cwd": copy["root"],
                 "tool_input": {"command": "cd src && (cd ..; echo x > outside.ts)"}})
    assert out is None


def test_bash_variable_write_target_cannot_judge_passes(copy):
    """A write target this hook cannot resolve ($VAR -- no access to the live shell env) is
    "판정 불가" and left unchecked, NOT denied and NOT treated as a parse failure (team-lead
    2026-11-02 correction) -- even inside an actually-fenced copy session (WT_COPY set)."""
    out = _call({"tool_name": "Bash", "cwd": copy["root"],
                 "tool_input": {"command": "bash deploy.sh app > $L"}})
    assert out is None


def test_bash_tilde_target_is_expanded_and_judged(copy, monkeypatch):
    """`~` IS resolved (deterministic) before the scope check, so a `~`-prefixed path outside
    the copy is still caught -- it is only an un-expandable $VAR that is let through."""
    fake_home = copy["other_app"]
    monkeypatch.setenv("HOME", fake_home)
    out = _call({"tool_name": "Bash", "cwd": copy["root"],
                 "tool_input": {"command": "echo hi > ~/leak.txt"}})
    assert out is not None
    assert "사본 밖" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_bash_unparsable_command_fails_closed(copy):
    out = _call({"tool_name": "Bash", "cwd": copy["root"],
                 "tool_input": {"command": "echo 'unbalanced > outside.ts"}})
    assert out is not None
    assert "해석할 수 없는" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_bash_cd_then_relative_write_outside_scope_denied(copy):
    out = _call({"tool_name": "Bash", "cwd": copy["root"],
                 "tool_input": {"command": "cd .. && echo hi > job1-leak.txt"}})
    assert out is not None


def test_edit_symlink_escape_denied(copy):
    link = os.path.join(copy["root"], "escape")
    os.symlink(copy["other_app"], link)
    out = _call({"tool_name": "Edit", "cwd": copy["root"],
                 "tool_input": {"file_path": os.path.join(link, "x.ts")}})
    assert out is not None
    assert "사본 밖" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_wt_copy_env_set_but_missing_dir_fails_closed(copy, tmp_path, monkeypatch):
    """Live incident fix (team-lead 2026-11-02 01:08): WT_COPY set but pointing at a directory
    that no longer exists (stale env var after wt-gc.sh reaped the copy, a typo, a race) must
    NOT silently fall through to "not a copy session" (that was the fail-open bug that let a
    write to /apps/exampleapp/x.ts through). It must deny the call outright instead."""
    monkeypatch.setenv("WT_COPY", str(tmp_path / "_wt" / "someapp" / "already-gone"))
    out = _call({"tool_name": "Edit", "cwd": "/apps/exampleapp",
                 "tool_input": {"file_path": "/apps/exampleapp/x.ts"}})
    assert out is not None
    assert "WT_COPY" in out["hookSpecificOutput"]["permissionDecisionReason"]

    out2 = _call({"tool_name": "Bash", "cwd": "/apps/exampleapp",
                  "tool_input": {"command": "echo hi > /apps/exampleapp/x.ts"}})
    assert out2 is not None


def test_wt_copy_env_overrides_cwd_detection(copy, monkeypatch):
    monkeypatch.setenv("WT_COPY", copy["root"])
    out = _call({"tool_name": "Write", "cwd": "/some/unrelated/dir",
                 "tool_input": {"file_path": os.path.join(copy["root"], "outside.ts")}})
    assert out is not None
    assert ".wt.scope" in out["hookSpecificOutput"]["permissionDecisionReason"]


# ---------------------------------------------------------------------------
# Off switch disables Part 2 too (owner-only, shared with Part 1)
# ---------------------------------------------------------------------------

def test_off_switch_disables_copy_fence(copy, monkeypatch):
    monkeypatch.setattr(sf, "OFF", copy["root"])  # any existing path works as the "off" marker
    out = _call({"tool_name": "Write", "cwd": copy["root"],
                 "tool_input": {"file_path": os.path.join(copy["root"], "outside.ts")}})
    assert out is None


# ---------------------------------------------------------------------------
# Part 1 regression: CURRENT_SCOPE.json app allow-list must still work unchanged
# ---------------------------------------------------------------------------

def test_part1_app_scope_still_enforced(tmp_path, monkeypatch):
    apps_root = tmp_path / "apps"
    (apps_root / "allowedapp").mkdir(parents=True)
    (apps_root / "blockedapp").mkdir(parents=True)
    scope_file = tmp_path / "CURRENT_SCOPE.json"
    scope_file.write_text('{"allow": ["allowedapp"]}')
    monkeypatch.setattr(sf, "APPS_ROOT", str(apps_root))
    monkeypatch.setattr(sf, "SCOPE_FILE", str(scope_file))
    monkeypatch.setattr(sf, "OFF", str(tmp_path / "off-never-exists"))
    monkeypatch.delenv("WT_COPY", raising=False)
    out = _call({"tool_name": "Edit", "cwd": str(apps_root / "blockedapp"),
                 "tool_input": {"file_path": str(apps_root / "blockedapp" / "x.ts")}})
    assert out is not None
    assert "범위 밖" in out["hookSpecificOutput"]["permissionDecisionReason"]

    out2 = _call({"tool_name": "Edit", "cwd": str(apps_root / "allowedapp"),
                  "tool_input": {"file_path": str(apps_root / "allowedapp" / "x.ts")}})
    assert out2 is None


def test_current_scope_file_itself_always_protected(copy, tmp_path, monkeypatch):
    scope_file = tmp_path / "CURRENT_SCOPE.json"
    scope_file.write_text('{"allow": []}')
    monkeypatch.setattr(sf, "SCOPE_FILE", str(scope_file))
    out = _call({"tool_name": "Edit", "cwd": copy["root"], "tool_input": {"file_path": str(scope_file)}})
    assert out is not None
    assert "CURRENT_SCOPE.json" in out["hookSpecificOutput"]["permissionDecisionReason"]
