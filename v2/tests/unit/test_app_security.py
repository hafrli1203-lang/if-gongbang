"""Unit tests for engine.app_security.check: secret scan, dependency
audit, and plan security-requirement coverage (s10 contract item 1)."""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

from engine import app_security
from tests.conftest import Workspace, git


def test_clean_repo_passes_with_only_secret_scan_run(workspace: Workspace) -> None:
    result = app_security.check(workspace.config, "alpha", "J-alpha-TEST#1", plan=None)
    assert result.passed is True
    assert result.findings == ()
    assert result.ran == ("secret_scan",)


def test_private_key_block_is_found_and_never_printed(workspace: Workspace) -> None:
    key_path = workspace.alpha / "src" / "key.pem"
    key_path.write_text(
        "-----BEGIN RSA PRIVATE KEY-----\nMIIsupersecret==\n-----END RSA PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    git(workspace.alpha, "add", "-A")
    git(workspace.alpha, "commit", "-q", "-m", "add key")

    result = app_security.check(workspace.config, "alpha", "J-alpha-TEST#2", plan=None)
    assert result.passed is False
    assert any(f.startswith("secret:private_key:src/key.pem:") for f in result.findings)
    assert not any("MIIsupersecret" in f for f in result.findings)


def test_token_prefix_and_password_literal_are_found(workspace: Workspace) -> None:
    leaky = workspace.alpha / "src" / "leaky.py"
    leaky.write_text(
        'TOKEN = "sk-abcdefghijklmnop"\n'
        'password = "hunter22"\n',
        encoding="utf-8",
    )
    git(workspace.alpha, "add", "-A")
    git(workspace.alpha, "commit", "-q", "-m", "add leak")

    result = app_security.check(workspace.config, "alpha", "J-alpha-TEST#3", plan=None)
    assert result.passed is False
    assert any(f.startswith("secret:token_prefix:src/leaky.py:1") for f in result.findings)
    assert any(f.startswith("secret:password_literal:src/leaky.py:2") for f in result.findings)
    assert not any("hunter22" in f or "sk-abcdefghijklmnop" in f for f in result.findings)


def test_untracked_file_is_never_scanned(workspace: Workspace) -> None:
    (workspace.alpha / "scratch.py").write_text('password = "untracked1"\n', encoding="utf-8")
    result = app_security.check(workspace.config, "alpha", "J-alpha-TEST#4", plan=None)
    assert result.passed is True
    assert result.findings == ()


def test_no_lockfile_skips_dependency_audit(workspace: Workspace) -> None:
    result = app_security.check(workspace.config, "alpha", "J-alpha-TEST#5", plan=None)
    assert "dependency_audit" not in result.ran


def _write_lockfile(repo: Path) -> None:
    (repo / "package-lock.json").write_text("{}\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add lockfile")


def _config_with_security(workspace: Workspace, security: dict,
                          tmp_path: Path) -> object:
    config_path = tmp_path / "engine.json"
    config_path.write_text(json.dumps({"security": security}), encoding="utf-8")
    return replace(workspace.config, config_path=config_path)


def test_audit_passes_when_command_exits_zero(workspace: Workspace, tmp_path: Path) -> None:
    _write_lockfile(workspace.alpha)
    config = _config_with_security(
        workspace, {"audit_cmd": [sys.executable, "-c", "pass"]}, tmp_path,
    )
    result = app_security.check(config, "alpha", "J-alpha-TEST#6", plan=None)
    assert result.passed is True
    assert result.ran == ("secret_scan", "dependency_audit")
    assert result.findings == ()


def test_audit_failure_from_real_vulnerabilities_always_fails(
    workspace: Workspace, tmp_path: Path,
) -> None:
    """A nonzero exit from a command that actually ran (found real
    vulnerabilities) must fail the check even if allow_audit_unavailable
    is set -- that flag only covers the command failing to run at all."""
    _write_lockfile(workspace.alpha)
    config = _config_with_security(
        workspace,
        {"audit_cmd": [sys.executable, "-c", "import sys; sys.exit(1)"],
         "allow_audit_unavailable": True},
        tmp_path,
    )
    result = app_security.check(config, "alpha", "J-alpha-TEST#7", plan=None)
    assert result.passed is False
    assert any(f.startswith("dependency_audit_failed:") for f in result.findings)


def test_audit_command_not_found_fails_closed_by_default(
    workspace: Workspace, tmp_path: Path,
) -> None:
    _write_lockfile(workspace.alpha)
    config = _config_with_security(
        workspace, {"audit_cmd": ["this-binary-does-not-exist-xyz"]}, tmp_path,
    )
    result = app_security.check(config, "alpha", "J-alpha-TEST#8", plan=None)
    assert result.passed is False
    assert any(f.startswith("audit_unavailable:") for f in result.findings)


def test_audit_command_not_found_allowed_when_configured(
    workspace: Workspace, tmp_path: Path,
) -> None:
    _write_lockfile(workspace.alpha)
    config = _config_with_security(
        workspace,
        {"audit_cmd": ["this-binary-does-not-exist-xyz"], "allow_audit_unavailable": True},
        tmp_path,
    )
    result = app_security.check(config, "alpha", "J-alpha-TEST#9", plan=None)
    assert result.passed is True
    assert any(f.startswith("audit_unavailable:") for f in result.findings)


def test_lockfile_but_no_audit_cmd_configured_fails_closed(
    workspace: Workspace,
) -> None:
    _write_lockfile(workspace.alpha)
    result = app_security.check(workspace.config, "alpha", "J-alpha-TEST#10", plan=None)
    assert result.passed is False
    assert "audit_unavailable:no_audit_cmd_configured" in result.findings


# ---- plan security-requirement coverage --------------------------------

def test_plan_none_or_without_requirements_adds_no_coverage_check(
    workspace: Workspace,
) -> None:
    result = app_security.check(workspace.config, "alpha", "J-alpha-TEST#11", plan=None)
    assert "plan_security_coverage" not in result.ran

    class _PlanNoRequirements:
        scenes = ()

    result2 = app_security.check(
        workspace.config, "alpha", "J-alpha-TEST#12", plan=_PlanNoRequirements(),
    )
    assert "plan_security_coverage" not in result2.ran


def test_security_requirement_covered_by_a_scene_passes(workspace: Workspace) -> None:
    plan = {
        "requirements": [{"id": "R1", "text": "no secrets", "kind": "security"},
                        {"id": "R2", "text": "a feature", "kind": "function"}],
        "scenes": [{"id": "S1", "covers": ["R1"]}],
    }
    result = app_security.check(workspace.config, "alpha", "J-alpha-TEST#13", plan=plan)
    assert result.passed is True
    assert "plan_security_coverage" in result.ran
    assert not any("security_requirement_uncovered" in f for f in result.findings)


def test_security_requirement_not_covered_by_any_scene_fails(workspace: Workspace) -> None:
    plan = {
        "requirements": [{"id": "R1", "text": "no secrets", "kind": "security"}],
        "scenes": [{"id": "S1", "covers": ["R2"]}],
    }
    result = app_security.check(workspace.config, "alpha", "J-alpha-TEST#14", plan=plan)
    assert result.passed is False
    assert "security_requirement_uncovered:R1" in result.findings
