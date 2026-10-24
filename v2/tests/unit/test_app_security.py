"""Unit tests for engine.app_security.check: secret scan, dependency
audit, and plan security-requirement coverage."""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

from engine import app_security
from tests.conftest import Workspace, git


def test_clean_repo_with_no_lockfile_fails_closed_by_default(workspace: Workspace) -> None:
    """workspace.alpha has a package.json (it is a Node app) but no
    package-lock.json -- a Node app with no committed lockfile must
    never silently pass; this is its own recorded failure state, not
    folded into "nothing to audit"."""
    result = app_security.check(workspace.config, "alpha", "J-alpha-TEST#1", plan=None)
    assert result.passed is False
    assert "dependency_audit" in result.ran
    assert "audit_skipped:no_lockfile" in result.findings


def test_clean_repo_without_lockfile_passes_when_explicitly_allowed(
    workspace: Workspace, tmp_path: Path,
) -> None:
    config = _config_with_security(workspace, {"allow_no_lockfile": True}, tmp_path)
    result = app_security.check(config, "alpha", "J-alpha-TEST#1b", plan=None)
    assert result.passed is True
    assert result.ran == ("secret_scan", "dependency_audit")
    assert "audit_skipped:no_lockfile" in result.findings


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


def test_untracked_file_is_never_scanned(workspace: Workspace, tmp_path: Path) -> None:
    (workspace.alpha / "scratch.py").write_text('password = "untracked1"\n', encoding="utf-8")
    config = _config_with_security(workspace, {"allow_no_lockfile": True}, tmp_path)
    result = app_security.check(config, "alpha", "J-alpha-TEST#4", plan=None)
    assert result.passed is True
    assert result.findings == ("audit_skipped:no_lockfile",)


def test_dependency_audit_not_run_for_a_non_node_repo(workspace: Workspace) -> None:
    """No package.json at all -- never claimed to be a Node app, so
    "dependency_audit" is never added to `ran` (distinct from
    "no_lockfile", which is its own recorded failure for a repo that
    DOES have a package.json)."""
    import subprocess

    from engine.gitutil import git_argv

    (workspace.alpha / "package.json").unlink()
    subprocess.run(git_argv(workspace.alpha, "add", "-A"), check=True, capture_output=True)
    subprocess.run(git_argv(workspace.alpha, "commit", "-q", "-m", "remove package.json"),
                   check=True, capture_output=True)
    result = app_security.check(workspace.config, "alpha", "J-alpha-TEST#5", plan=None)
    assert "dependency_audit" not in result.ran
    assert not any("no_lockfile" in f for f in result.findings)


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


_REAL_VULN_JSON = json.dumps({
    "auditReportVersion": 2,
    "vulnerabilities": {
        "fake-vulnerable-pkg": {"name": "fake-vulnerable-pkg", "severity": "high"},
    },
    "metadata": {"vulnerabilities": {"info": 0, "low": 0, "moderate": 0, "high": 1,
                                     "critical": 0, "total": 1}},
})

_REGISTRY_ERROR_JSON = json.dumps({
    "error": {"code": "ENOTFOUND", "summary": "request to registry failed",
              "detail": "getaddrinfo ENOTFOUND registry.npmjs.org"},
})


def test_audit_failure_from_real_vulnerabilities_always_fails(
    workspace: Workspace, tmp_path: Path,
) -> None:
    """A nonzero exit from a command whose own JSON output actually
    reports a finding (`npm audit --json`'s own "vulnerabilities" shape)
    must fail the check even if allow_audit_unavailable is set -- that
    flag only covers the command failing to run at all, never a real
    finding."""
    _write_lockfile(workspace.alpha)
    config = _config_with_security(
        workspace,
        {"audit_cmd": [sys.executable, "-c",
                       f"import sys; print({_REAL_VULN_JSON!r}); sys.exit(1)"],
         "allow_audit_unavailable": True},
        tmp_path,
    )
    result = app_security.check(config, "alpha", "J-alpha-TEST#7", plan=None)
    assert result.passed is False
    assert any(f.startswith("dependency_audit_failed:") for f in result.findings)


def test_audit_registry_error_is_unavailable_not_vulnerabilities(
    workspace: Workspace, tmp_path: Path,
) -> None:
    """A nonzero exit whose own JSON body is npm audit's OWN shape for a
    registry/network/DNS execution failure ({"error": {...}}, no real
    "vulnerabilities" entry at all) must be classified "unavailable",
    never "vulnerabilities" -- an infrastructure error must never be
    able to fail a release the exact same way a real finding does, nor
    be silently waved through by allow_audit_unavailable's own intended
    scope being defeated."""
    _write_lockfile(workspace.alpha)
    config = _config_with_security(
        workspace,
        {"audit_cmd": [sys.executable, "-c",
                       f"import sys; print({_REGISTRY_ERROR_JSON!r}); sys.exit(1)"]},
        tmp_path,
    )
    result = app_security.check(config, "alpha", "J-alpha-TEST#7b", plan=None)
    assert result.passed is False
    assert any(f.startswith("audit_unavailable:execution_error:") for f in result.findings)
    assert not any(f.startswith("dependency_audit_failed:") for f in result.findings)

    allowed_config = _config_with_security(
        workspace,
        {"audit_cmd": [sys.executable, "-c",
                       f"import sys; print({_REGISTRY_ERROR_JSON!r}); sys.exit(1)"],
         "allow_audit_unavailable": True},
        tmp_path,
    )
    allowed = app_security.check(allowed_config, "alpha", "J-alpha-TEST#7c", plan=None)
    assert allowed.passed is True


def test_audit_unparsable_output_is_unavailable_not_vulnerabilities(
    workspace: Workspace, tmp_path: Path,
) -> None:
    """A nonzero exit whose stdout is not even parsable JSON at all
    (garbled output, a crash before the tool ever produced its own
    report) must also be "unavailable", never "vulnerabilities" -- there
    is no actual finding in unparsable text to act on."""
    _write_lockfile(workspace.alpha)
    config = _config_with_security(
        workspace,
        {"audit_cmd": [sys.executable, "-c",
                       "import sys; print('not json at all'); sys.exit(1)"]},
        tmp_path,
    )
    result = app_security.check(config, "alpha", "J-alpha-TEST#7d", plan=None)
    assert result.passed is False
    assert any(f.startswith("audit_unavailable:unparsable_output:") for f in result.findings)
    assert not any(f.startswith("dependency_audit_failed:") for f in result.findings)


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


def test_security_requirement_covered_by_a_scene_passes(
    workspace: Workspace, tmp_path: Path,
) -> None:
    plan = {
        "requirements": [{"id": "R1", "text": "no secrets", "kind": "security"},
                        {"id": "R2", "text": "a feature", "kind": "function"}],
        "scenes": [{"id": "S1", "covers": ["R1"]}],
    }
    config = _config_with_security(workspace, {"allow_no_lockfile": True}, tmp_path)
    result = app_security.check(config, "alpha", "J-alpha-TEST#13", plan=plan)
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


# ---- fingerprint + record() ---------------------------------------------

def test_check_carries_the_repo_fingerprint(workspace: Workspace, tmp_path: Path) -> None:
    config = _config_with_security(workspace, {"allow_no_lockfile": True}, tmp_path)
    result = app_security.check(config, "alpha", "J-alpha-TEST#15", plan=None)
    assert len(result.fingerprint) == 40


def test_record_appends_app_security_recorded_event(
    workspace: Workspace, tmp_path: Path,
) -> None:
    config = _config_with_security(workspace, {"allow_no_lockfile": True}, tmp_path)
    attempt_id = "J-alpha-TEST#16"
    result = app_security.check(config, "alpha", attempt_id, plan=None)

    seq = app_security.record(workspace.ledger, "alpha", attempt_id, result)

    events = [e for _seq, e in workspace.ledger.events("alpha", "app_security.recorded")]
    assert len(events) == 1
    assert events[0].payload == {
        "passed": result.passed, "findings": list(result.findings),
        "ran": list(result.ran), "fingerprint": result.fingerprint,
    }
    assert seq > 0


def test_record_never_leaks_a_secret_value_into_the_ledger(
    workspace: Workspace, tmp_path: Path,
) -> None:
    leaky = workspace.alpha / "src" / "leaky.py"
    leaky.write_text('password = "hunter22hunter22"\n', encoding="utf-8")
    git(workspace.alpha, "add", "-A")
    git(workspace.alpha, "commit", "-q", "-m", "add leak")
    config = _config_with_security(workspace, {"allow_no_lockfile": True}, tmp_path)
    attempt_id = "J-alpha-TEST#17"
    result = app_security.check(config, "alpha", attempt_id, plan=None)

    app_security.record(workspace.ledger, "alpha", attempt_id, result)

    events = [e for _seq, e in workspace.ledger.events("alpha", "app_security.recorded")]
    assert "hunter22hunter22" not in str(events[0].payload)
