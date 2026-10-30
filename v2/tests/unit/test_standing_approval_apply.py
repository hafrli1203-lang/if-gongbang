"""Unit tests for engine.application.standing_approval -- the I/O-
reading half of owner standing approval: loading the
policy file, reading a real manifest off disk, matching a real pending
request against it, and appending the design.approved event. tools/
gb2lib/watch_ops.py's test file covers the "waiting_owner -> auto-
resume" wiring itself; this file covers what it calls."""
from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

from engine.application.job_state import STAGE_DESIGN_APPROVAL, JobPosition, record_position
from engine.application.standing_approval import (
    DEFAULT_POLICY_PATH,
    all_requests_match_standing,
    design_standing_approved,
    load_policy,
    policy_digest,
    request_standing_kind,
)
from engine.domain.standing_approval_policy import parse_policy
from engine.steps.open_job import open_job
from tests.conftest import Workspace, seed_reviewed_manifest


def _with_manifest(workspace: Workspace, manifest: str) -> Workspace:
    project = replace(workspace.config.projects["alpha"], manifest=manifest)
    config = replace(workspace.config,
                      projects={**workspace.config.projects, "alpha": project})
    return replace(workspace, config=config)


def _open_attempt(workspace: Workspace, job_name: str) -> str:
    return open_job(workspace.config, workspace.ledger, "alpha", job_name,
                    "full", ("builder",))


def _stop_with_design_approval_pending(workspace: Workspace, job_name: str,
                                       attempt_id: str) -> None:
    """Copy of tests/unit/test_office_approval.py's own private helper
    (see that file's docstring for why each test module keeps its own
    copy of this small setup rather than importing another module's
    leading-underscore name)."""
    seed_reviewed_manifest(workspace.ledger, "alpha", attempt_id)
    record_position(workspace.ledger, "alpha", JobPosition(
        job=job_name, stage=STAGE_DESIGN_APPROVAL, attempt_id=attempt_id,
        stop_reason="design_approval",
    ))


def _write_manifest(repo: Path, dependencies: dict[str, str] | None = None,
                    dev_dependencies: dict[str, str] | None = None) -> None:
    data: dict[str, object] = {"name": "alpha"}
    if dependencies is not None:
        data["dependencies"] = dependencies
    if dev_dependencies is not None:
        data["devDependencies"] = dev_dependencies
    (repo / "package.json").write_text(json.dumps(data), encoding="utf-8")


_ALLOW_INSTALL = parse_policy({"apps": {"alpha": ["dependency_patch_bump"]}})
_ALLOW_DESIGN = parse_policy({"apps": {"alpha": ["design_approval"]}})
_ALLOW_NONE = parse_policy({"apps": {}})


# ---- load_policy ------------------------------------------------------------

def test_load_policy_missing_file_is_empty(tmp_path: Path) -> None:
    policy = load_policy(tmp_path / "does-not-exist.json")
    assert policy.allowed_kinds("alpha") == frozenset()


def test_load_policy_malformed_json_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "STANDING_APPROVALS.json"
    path.write_text("{not valid json", encoding="utf-8")
    policy = load_policy(path)
    assert policy.allowed_kinds("alpha") == frozenset()


def test_load_policy_well_formed_parses(tmp_path: Path) -> None:
    path = tmp_path / "STANDING_APPROVALS.json"
    path.write_text(json.dumps({"apps": {"alpha": ["dependency_patch_bump"]}}), encoding="utf-8")
    policy = load_policy(path)
    assert policy.allowed_kinds("alpha") == {"dependency_patch_bump"}


def test_default_policy_path_is_the_claude_gate_file() -> None:
    """moved off this repo entirely, into a
    directory an existing hook already refuses any model tool call
    against unconditionally -- see module docstring."""
    assert str(DEFAULT_POLICY_PATH) == os.path.expanduser(
        "~/.claude/gate/standing-approvals.json")


def test_env_override_is_ignored_outside_pytest(tmp_path: Path, monkeypatch) -> None:
    """GB2_STANDING_APPROVALS_PATH only redirects
    load_policy while PYTEST_CURRENT_TEST is set (which pytest itself
    always sets for the duration of a test) -- simulating "not running
    under pytest" by deleting that var must fall back to the real
    default path, never the env-named one."""
    import engine.application.standing_approval as sa

    decoy = tmp_path / "decoy.json"
    decoy.write_text(json.dumps({"apps": {"alpha": ["dependency_patch_bump"]}}),
                     encoding="utf-8")
    monkeypatch.setenv("GB2_STANDING_APPROVALS_PATH", str(decoy))
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "forged")  # even then: ignored

    assert sa._policy_path(None) == sa.DEFAULT_POLICY_PATH


def test_policy_digest_is_stable_for_same_content() -> None:
    a = parse_policy({"apps": {"alpha": ["dependency_patch_bump"]}})
    b = parse_policy({"apps": {"alpha": ["dependency_patch_bump"]}})
    assert policy_digest(a) == policy_digest(b)


# ---- request_standing_kind: install (dependency_patch_bump) --------------

def test_install_patch_bump_matches_when_policy_allows(workspace: Workspace) -> None:
    _write_manifest(workspace.alpha, dependencies={"next": "16.3.6", "zod": "4.6.6"})
    request = {"kind": "install", "manifest": "package.json",
               "change": {"dependencies": {"next": "16.4.3", "zod": "4.6.7"}}}
    assert request_standing_kind(_ALLOW_INSTALL, "alpha", request, workspace.alpha) \
        == "dependency_patch_bump"


def test_install_major_bump_never_matches(workspace: Workspace) -> None:
    _write_manifest(workspace.alpha, dependencies={"next": "16.3.6"})
    request = {"kind": "install", "manifest": "package.json",
               "change": {"dependencies": {"next": "17.0.0"}}}
    assert request_standing_kind(_ALLOW_INSTALL, "alpha", request, workspace.alpha) is None


def test_install_new_package_never_matches(workspace: Workspace) -> None:
    _write_manifest(workspace.alpha, dependencies={"next": "16.3.6"})
    request = {"kind": "install", "manifest": "package.json",
               "change": {"dependencies": {"left-pad": "1.0.0"}}}
    assert request_standing_kind(_ALLOW_INSTALL, "alpha", request, workspace.alpha) is None


def test_install_matches_nothing_when_policy_does_not_allow_it(workspace: Workspace) -> None:
    _write_manifest(workspace.alpha, dependencies={"next": "16.3.6"})
    request = {"kind": "install", "manifest": "package.json",
               "change": {"dependencies": {"next": "16.4.3"}}}
    assert request_standing_kind(_ALLOW_NONE, "alpha", request, workspace.alpha) is None


# ---- request_standing_kind: every non-"install" kind is never covered ---

def test_scripts_change_kind_never_matches_anything(workspace: Workspace) -> None:
    """scripts_change is no longer a
    standing-approval kind at all -- even a plain, harmless-looking
    `node server.js` never auto-approves; it always asks the owner."""
    request = {"kind": "scripts_change", "manifest": "package.json",
               "change": {"scripts": {"start": "node server.js"}}}
    full_policy = parse_policy({"apps": {"alpha": ["dependency_patch_bump", "design_approval"]}})
    assert request_standing_kind(full_policy, "alpha", request, workspace.alpha) is None


def test_test_config_change_kind_never_matches_anything(workspace: Workspace) -> None:
    """test_config_change is NOT one of the standing-approval kinds
    (ALLOWED_STANDING_KINDS has no entry for it) -- always asks the
    owner."""
    request = {"kind": "test_config_change", "manifest": "package.json",
               "change": {"jest": {"testTimeout": 5000}}}
    full_policy = parse_policy({"apps": {"alpha": ["dependency_patch_bump", "design_approval"]}})
    assert request_standing_kind(full_policy, "alpha", request, workspace.alpha) is None


def test_edit_kind_never_matches_anything(workspace: Workspace) -> None:
    """A completely non-whitelisted kind (never even defined as an
    owner-approval kind) must never match either -- request_standing_
    kind only ever recognizes "install"."""
    request = {"kind": "edit", "path": "src/x.py", "diff": "..."}
    full_policy = parse_policy({"apps": {"alpha": ["dependency_patch_bump", "design_approval"]}})
    assert request_standing_kind(full_policy, "alpha", request, workspace.alpha) is None


# ---- all_requests_match_standing: all-or-nothing --------------------------

def test_all_requests_match_returns_none_for_empty_list(workspace: Workspace) -> None:
    assert all_requests_match_standing(_ALLOW_INSTALL, "alpha", [], workspace.alpha) is None


def test_all_requests_match_is_none_if_even_one_request_does_not_match(workspace: Workspace) -> None:
    _write_manifest(workspace.alpha, dependencies={"next": "16.3.6"})
    covered = {"kind": "install", "manifest": "package.json",
               "change": {"dependencies": {"next": "16.4.3"}}}
    not_covered = {"kind": "install", "manifest": "package.json",
                  "change": {"dependencies": {"left-pad": "1.0.0"}}}
    assert all_requests_match_standing(
        _ALLOW_INSTALL, "alpha", [covered, not_covered], workspace.alpha) is None


def test_all_requests_match_lists_every_kind_when_all_covered(workspace: Workspace) -> None:
    _write_manifest(workspace.alpha, dependencies={"next": "16.3.6", "zod": "4.6.6"})
    install_req1 = {"kind": "install", "manifest": "package.json",
                    "change": {"dependencies": {"next": "16.4.3"}}}
    install_req2 = {"kind": "install", "manifest": "package.json",
                    "change": {"dependencies": {"zod": "4.6.7"}}}
    result = all_requests_match_standing(_ALLOW_INSTALL, "alpha", [install_req1, install_req2],
                                         workspace.alpha)
    assert result == ["dependency_patch_bump", "dependency_patch_bump"]


def test_all_requests_match_is_none_when_an_edit_request_is_mixed_in(workspace: Workspace) -> None:
    """the whole batch must be rejected the
    moment ANY pending request -- even one of a kind this module has
    never heard of -- is not covered, never just the ones it
    recognizes."""
    _write_manifest(workspace.alpha, dependencies={"next": "16.3.6"})
    covered_install = {"kind": "install", "manifest": "package.json",
                       "change": {"dependencies": {"next": "16.4.3"}}}
    edit_request = {"kind": "edit", "path": "src/x.py", "diff": "..."}
    result = all_requests_match_standing(
        _ALLOW_INSTALL, "alpha", [covered_install, edit_request], workspace.alpha)
    assert result is None


# ---- design_standing_approved -----------------------------------------------

def test_design_standing_approved_appends_event_when_allowed_and_pending(
        workspace: Workspace) -> None:
    ws = _with_manifest(workspace, "package.json")
    attempt_id = _open_attempt(ws, "dj1")
    _stop_with_design_approval_pending(ws, "dj1", attempt_id)

    approved = design_standing_approved(ws.ledger, "alpha", "dj1", attempt_id, ws.alpha,
                                        policy=_ALLOW_DESIGN)

    assert approved is True
    events = ws.ledger.events("alpha", "design.approved")
    assert len(events) == 1
    payload = events[0][1].payload
    assert payload["confirm_source"] == "standing"
    assert payload["job"] == "dj1"
    assert "standing_policy_digest" in payload


def test_design_standing_approved_false_when_policy_does_not_allow_it(
        workspace: Workspace) -> None:
    ws = _with_manifest(workspace, "package.json")
    attempt_id = _open_attempt(ws, "dj2")
    _stop_with_design_approval_pending(ws, "dj2", attempt_id)

    approved = design_standing_approved(ws.ledger, "alpha", "dj2", attempt_id, ws.alpha,
                                        policy=_ALLOW_NONE)

    assert approved is False
    assert ws.ledger.events("alpha", "design.approved") == []


def test_design_standing_approved_false_when_not_actually_pending(workspace: Workspace) -> None:
    ws = _with_manifest(workspace, "package.json")
    attempt_id = _open_attempt(ws, "dj3")
    # No _stop_with_design_approval_pending call -- the job never
    # reached STAGE_DESIGN_APPROVAL, so there is nothing to approve.

    approved = design_standing_approved(ws.ledger, "alpha", "dj3", attempt_id, ws.alpha,
                                        policy=_ALLOW_DESIGN)

    assert approved is False
    assert ws.ledger.events("alpha", "design.approved") == []


# ---- request_standing_kind: install (dependency_install, -----

def test_install_new_package_matches_when_dependency_install_allowed(workspace: Workspace) -> None:
    """Policy (external packages allowed): a new devDependency is covered
    once the app's policy lists dependency_install (patch-bump rule alone
    still refuses it -- see test_install_new_package_never_matches)."""
    _write_manifest(workspace.alpha, dependencies={"next": "16.3.6"})
    policy = parse_policy({"apps": {"alpha": ["dependency_install"]}})
    request = {"kind": "install", "manifest": "package.json",
               "change": {"devDependencies": {"jsdom": "^25.0.1"}}}
    assert request_standing_kind(policy, "alpha", request, workspace.alpha) == "dependency_install"
    scripts = {"kind": "install", "manifest": "package.json",
               "change": {"scripts": {"lint": "eslint src"}}}
    assert request_standing_kind(policy, "alpha", scripts, workspace.alpha) is None
