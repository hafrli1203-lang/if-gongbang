"""E1 office approval: the HMAC-signed, nonce-bound token path that
lets the AI office web app approve a design/install/plan_edit request
without posing as a TTY. engine.domain.office_approval covers the pure
signing/verification math; this file exercises it together with
engine.application.office_approval's I/O (the real key file, the
ledger-side nonce record, each kind's target_version, and the pending-
approval listing the office server itself would read).

tools/gb2lib's own TTY approval paths (approve-design/approve-install/
approve-plan-edit) are unit-tested separately in tests/unit/
test_gb2_new.py and must keep passing unmodified -- see
tests/unit/test_gb2_office_approval.py for this feature's CLI-level
--office-token tests."""
from __future__ import annotations

import os
import stat
import time
from dataclasses import replace
from pathlib import Path

import pytest

from engine.application import office_approval as app_office
from engine.application.job_state import JobPosition, record_position
from engine.domain import office_approval as domain_office
from engine.steps.open_job import open_job
from tests.conftest import Workspace, seed_reviewed_manifest

_INSTALL_REQUEST = {
    "kind": "install", "manifest": "package.json",
    "change": {"dependencies": {"zod": "3.0.0"}},
}


def _with_manifest(workspace: Workspace, manifest: str) -> Workspace:
    project = replace(workspace.config.projects["alpha"], manifest=manifest)
    config = replace(workspace.config,
                      projects={**workspace.config.projects, "alpha": project})
    return replace(workspace, config=config)


def _open_attempt(workspace: Workspace, job_name: str) -> str:
    return open_job(workspace.config, workspace.ledger, "alpha", job_name,
                    "full", ("builder",))


def _stop_with_pending_install(workspace: Workspace, job_name: str,
                               attempt_id: str, request: dict) -> None:
    record_position(workspace.ledger, "alpha", JobPosition(
        job=job_name, stage="awaiting_task_owner", attempt_id=attempt_id,
        pending_requests=(request,), stop_reason="owner_request",
    ))


def _stop_with_design_approval_pending(workspace: Workspace, job_name: str,
                                       attempt_id: str) -> None:
    """Makes _design_actually_pending True: a reviewed
    plan, design not yet approved, AND the job's own recorded position
    genuinely at STAGE_DESIGN_APPROVAL -- not just "approved event
    absent", which is also true before a design station ever ran."""
    from engine.application.job_state import STAGE_DESIGN_APPROVAL

    seed_reviewed_manifest(workspace.ledger, "alpha", attempt_id)
    record_position(workspace.ledger, "alpha", JobPosition(
        job=job_name, stage=STAGE_DESIGN_APPROVAL, attempt_id=attempt_id,
        stop_reason="design_approval",
    ))


def _key_file(workspace: Workspace, *, secret: bytes = b"super-secret-office-key",
              mode: int = 0o600) -> Path:
    """Writes the office-approval key at its one, fixed, non-
    configurable location
    (engine.application.office_approval.resolve_key_path): `secrets_dir
    / "office-approval.key"`. workspace.config.secrets_dir already
    exists as a real directory (tests/conftest.py's own `workspace`
    fixture creates it)."""
    path = workspace.config.secrets_dir / app_office.KEY_FILENAME
    path.write_bytes(secret)
    path.chmod(mode)
    return path


def _ws_with_key(workspace: Workspace, *, mode: int = 0o600) -> Workspace:
    _key_file(workspace, mode=mode)
    return workspace


# ---------------------------------------------------------------------------
# engine.domain.office_approval -- pure signing/verification
# ---------------------------------------------------------------------------

def test_domain_build_and_verify_token_round_trips() -> None:
    secret = b"k1"
    token = domain_office.build_token(
        secret, app="demo", job_name="j1", attempt_id="A#1", kind="install",
        target_version="v1", issued_at=1000, nonce="n1",
    )
    ok, reason = domain_office.verify_token(
        secret, token, expected_app="demo", expected_job_name="j1",
        expected_attempt_id="A#1", expected_kind="install",
        expected_target_version="v1", now=1001,
    )
    assert (ok, reason) == (True, "ok")


def test_domain_verify_rejects_forged_signature() -> None:
    secret = b"k1"
    token = domain_office.build_token(
        secret, app="demo", job_name="j1", attempt_id="A#1", kind="install",
        target_version="v1", issued_at=1000, nonce="n1",
    )
    forged = {**token, "sig": "0" * len(token["sig"])}
    ok, reason = domain_office.verify_token(
        secret, forged, expected_app="demo", expected_job_name="j1",
        expected_attempt_id="A#1", expected_kind="install",
        expected_target_version="v1", now=1001,
    )
    assert (ok, reason) == (False, "signature_invalid")


def test_domain_verify_rejects_wrong_key() -> None:
    token = domain_office.build_token(
        b"k1", app="demo", job_name="j1", attempt_id="A#1", kind="install",
        target_version="v1", issued_at=1000, nonce="n1",
    )
    ok, reason = domain_office.verify_token(
        b"k2", token, expected_app="demo", expected_job_name="j1",
        expected_attempt_id="A#1", expected_kind="install",
        expected_target_version="v1", now=1001,
    )
    assert (ok, reason) == (False, "signature_invalid")


def test_domain_verify_rejects_expired_token() -> None:
    secret = b"k1"
    token = domain_office.build_token(
        secret, app="demo", job_name="j1", attempt_id="A#1", kind="install",
        target_version="v1", issued_at=1000, nonce="n1", ttl_seconds=600,
    )
    ok, reason = domain_office.verify_token(
        secret, token, expected_app="demo", expected_job_name="j1",
        expected_attempt_id="A#1", expected_kind="install",
        expected_target_version="v1", now=1601,
    )
    assert (ok, reason) == (False, "expired")


def test_domain_verify_rejects_target_version_mismatch() -> None:
    secret = b"k1"
    token = domain_office.build_token(
        secret, app="demo", job_name="j1", attempt_id="A#1", kind="install",
        target_version="old-version", issued_at=1000, nonce="n1",
    )
    ok, reason = domain_office.verify_token(
        secret, token, expected_app="demo", expected_job_name="j1",
        expected_attempt_id="A#1", expected_kind="install",
        expected_target_version="new-version", now=1001,
    )
    assert (ok, reason) == (False, "target_version_mismatch")


def test_domain_build_token_refuses_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown approval kind"):
        domain_office.build_token(
            b"k1", app="demo", job_name="j1", attempt_id="A#1", kind="publish",
            target_version="v1", issued_at=1000, nonce="n1",
        )


# ---------------------------------------------------------------------------
# engine.application.office_approval -- key file I/O
# ---------------------------------------------------------------------------

def test_load_office_secret_missing_file_is_refused(workspace: Workspace) -> None:
    # No key file written at all -- secrets_dir exists but is empty.
    secret, reason = app_office.load_office_secret(workspace.config)
    assert (secret, reason) == (None, "office_key_missing")


def test_load_office_secret_refuses_permissions_wider_than_600(
    workspace: Workspace,
) -> None:
    key_path = _key_file(workspace, mode=0o644)
    secret, reason = app_office.load_office_secret(workspace.config)
    assert (secret, reason) == (None, "office_key_permissions_too_open")
    del key_path


def test_load_office_secret_accepts_exactly_600(workspace: Workspace) -> None:
    key_path = _key_file(workspace, mode=0o600)
    secret, reason = app_office.load_office_secret(workspace.config)
    assert reason == "ok"
    assert secret == key_path.read_bytes()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# install kind: issue -> verify, end to end, through the real ledger
# ---------------------------------------------------------------------------

def test_issue_and_verify_office_token_for_install_succeeds(
    workspace: Workspace,
) -> None:
    ws = _ws_with_key(_with_manifest(workspace, "package.json"))
    job_name = "office-install-1"
    attempt_id = _open_attempt(ws, job_name)
    _stop_with_pending_install(ws, job_name, attempt_id, _INSTALL_REQUEST)

    token = app_office.issue_office_approval_token(
        ws.config, ws.ledger, "alpha", job_name, attempt_id, "install",
    )
    ok, reason = app_office.verify_office_token(
        ws.config, ws.ledger, "alpha", job_name, attempt_id, "install", token,
    )
    assert (ok, reason) == (True, "ok")


def test_verify_office_token_rejects_missing_key_file(
    workspace: Workspace,
) -> None:
    ws = _with_manifest(workspace, "package.json")
    # No key file written -- secrets_dir exists but is empty.
    job_name = "office-install-nokey"
    attempt_id = _open_attempt(ws, job_name)
    _stop_with_pending_install(ws, job_name, attempt_id, _INSTALL_REQUEST)

    fake_token = {
        "app": "alpha", "job_name": job_name, "attempt_id": attempt_id,
        "kind": "install", "target_version": "whatever", "issued_at": int(time.time()),
        "nonce": "n", "ttl_seconds": 600, "sig": "0" * 64,
    }
    ok, reason = app_office.verify_office_token(
        ws.config, ws.ledger, "alpha", job_name, attempt_id, "install", fake_token,
    )
    assert (ok, reason) == (False, "office_key_missing")


def test_verify_office_token_rejects_nonce_reuse(
    workspace: Workspace,
) -> None:
    ws = _ws_with_key(_with_manifest(workspace, "package.json"))
    job_name = "office-install-2"
    attempt_id = _open_attempt(ws, job_name)
    _stop_with_pending_install(ws, job_name, attempt_id, _INSTALL_REQUEST)

    token = app_office.issue_office_approval_token(
        ws.config, ws.ledger, "alpha", job_name, attempt_id, "install",
    )
    first = app_office.verify_office_token(
        ws.config, ws.ledger, "alpha", job_name, attempt_id, "install", token,
    )
    assert first == (True, "ok")

    second = app_office.verify_office_token(
        ws.config, ws.ledger, "alpha", job_name, attempt_id, "install", token,
    )
    assert second == (False, "nonce_reused")


def test_verify_office_token_rejects_stale_target_version(
    workspace: Workspace,
) -> None:
    """A token issued while ONE install request was pending must be
    refused once the pending batch has changed (here: a second request
    added) -- compute_target_version hashes the whole pending batch,
    so adding/removing/changing any one of them moves the hash."""
    ws = _ws_with_key(_with_manifest(workspace, "package.json"))
    job_name = "office-install-3"
    attempt_id = _open_attempt(ws, job_name)
    _stop_with_pending_install(ws, job_name, attempt_id, _INSTALL_REQUEST)

    token = app_office.issue_office_approval_token(
        ws.config, ws.ledger, "alpha", job_name, attempt_id, "install",
    )

    other_request = {
        "kind": "install", "manifest": "package.json",
        "change": {"dependencies": {"left-pad": "1.0.0"}},
    }
    record_position(ws.ledger, "alpha", JobPosition(
        job=job_name, stage="awaiting_task_owner", attempt_id=attempt_id,
        pending_requests=(_INSTALL_REQUEST, other_request), stop_reason="owner_request",
    ))

    ok, reason = app_office.verify_office_token(
        ws.config, ws.ledger, "alpha", job_name, attempt_id, "install", token,
    )
    assert (ok, reason) == (False, "target_version_mismatch")


def test_issue_office_approval_token_refuses_without_key_file(
    workspace: Workspace,
) -> None:
    ws = _with_manifest(workspace, "package.json")
    # No key file written -- secrets_dir exists but is empty.
    job_name = "office-install-5"
    attempt_id = _open_attempt(ws, job_name)
    _stop_with_pending_install(ws, job_name, attempt_id, _INSTALL_REQUEST)

    with pytest.raises(ValueError, match="office_key_missing"):
        app_office.issue_office_approval_token(
            ws.config, ws.ledger, "alpha", job_name, attempt_id, "install",
        )


# ---------------------------------------------------------------------------
# design / plan_edit target_version: plan_digest-based
# ---------------------------------------------------------------------------

def test_compute_target_version_design_tracks_plan_digest(
    workspace: Workspace,
) -> None:
    job_name = "office-design-1"
    _open_attempt(workspace, job_name)
    plan_file = workspace.alpha / ".gongbang" / job_name / "plan.json"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text('{"goal": "v1"}\n', encoding="utf-8")

    v1 = app_office.compute_target_version(
        workspace.config, workspace.ledger, "alpha", job_name, "design",
    )
    assert v1 == app_office.design_target_version(workspace.alpha, job_name)

    plan_file.write_text('{"goal": "v2"}\n', encoding="utf-8")
    v2 = app_office.compute_target_version(
        workspace.config, workspace.ledger, "alpha", job_name, "design",
    )
    assert v2 != v1
    assert v2 == app_office.design_target_version(workspace.alpha, job_name)


def test_compute_target_version_design_also_tracks_design_artifact_files(
    workspace: Workspace,
) -> None:
    """swapping a design.md/comp/screenshot under an
    UNCHANGED plan.json must also move design_target_version -- the
    previous implementation hashed plan.json alone and missed this."""
    job_name = "office-design-artifacts"
    _open_attempt(workspace, job_name)
    plan_file = workspace.alpha / ".gongbang" / job_name / "plan.json"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text('{"goal": "v1"}\n', encoding="utf-8")
    design_dir = workspace.alpha / ".gongbang" / job_name / "design"
    design_dir.mkdir(parents=True)
    design_md = design_dir / "design.md"
    design_md.write_text("# first comp\n", encoding="utf-8")

    v1 = app_office.compute_target_version(
        workspace.config, workspace.ledger, "alpha", job_name, "design",
    )
    # plan.json itself never changes from here on
    design_md.write_text("# swapped comp\n", encoding="utf-8")
    v2 = app_office.compute_target_version(
        workspace.config, workspace.ledger, "alpha", job_name, "design",
    )
    assert v1 != v2


def test_issue_and_verify_office_token_for_design_succeeds(
    workspace: Workspace,
) -> None:
    ws = _ws_with_key(workspace)
    job_name = "office-design-2"
    attempt_id = _open_attempt(ws, job_name)
    plan_file = ws.alpha / ".gongbang" / job_name / "plan.json"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text('{"goal": "v1"}\n', encoding="utf-8")
    _stop_with_design_approval_pending(ws, job_name, attempt_id)

    token = app_office.issue_office_approval_token(
        ws.config, ws.ledger, "alpha", job_name, attempt_id, "design",
    )
    ok, reason = app_office.verify_office_token(
        ws.config, ws.ledger, "alpha", job_name, attempt_id, "design", token,
    )
    assert (ok, reason) == (True, "ok")

    # a fresh token for a SECOND verify, against a plan that changed
    # since issuance -- the SAME content check, exercised through the
    # real verify_office_token entry point this time.
    plan_file.write_text('{"goal": "v2"}\n', encoding="utf-8")
    token2 = app_office.issue_office_approval_token(
        ws.config, ws.ledger, "alpha", job_name, attempt_id, "design",
    )
    stale_ok, stale_reason = app_office.verify_office_token(
        ws.config, ws.ledger, "alpha", job_name, attempt_id, "design", token,
    )
    assert (stale_ok, stale_reason) == (False, "target_version_mismatch")
    fresh_ok, fresh_reason = app_office.verify_office_token(
        ws.config, ws.ledger, "alpha", job_name, attempt_id, "design", token2,
    )
    assert (fresh_ok, fresh_reason) == (True, "ok")


def test_verify_office_token_refuses_design_when_not_actually_pending(
    workspace: Workspace,
) -> None:
    """a valid, correctly-signed design token must still
    be refused if the job is not genuinely stopped at the design-
    approval gate right now (e.g. the plan was never reviewed at all)
    -- "design.approved absent" alone is not "a design approval is
    pending"."""
    ws = _ws_with_key(workspace)
    job_name = "office-design-3"
    attempt_id = _open_attempt(ws, job_name)
    # plan never reviewed, no design-approval position recorded

    token = app_office.issue_office_approval_token(
        ws.config, ws.ledger, "alpha", job_name, attempt_id, "design",
    )
    ok, reason = app_office.verify_office_token(
        ws.config, ws.ledger, "alpha", job_name, attempt_id, "design", token,
    )
    assert (ok, reason) == (False, "design_not_pending")


# ---------------------------------------------------------------------------
# list_pending_office_approvals -- the office server's own listing read
# ---------------------------------------------------------------------------

def test_list_pending_office_approvals_empty_with_no_open_job(
    workspace: Workspace,
) -> None:
    assert app_office.list_pending_office_approvals(
        workspace.config, workspace.ledger, "alpha") == []


def test_list_pending_office_approvals_reports_install_preview_without_values(
    workspace: Workspace,
) -> None:
    ws = _with_manifest(workspace, "package.json")
    job_name = "office-list-1"
    attempt_id = _open_attempt(ws, job_name)
    _stop_with_pending_install(ws, job_name, attempt_id, _INSTALL_REQUEST)

    pending = app_office.list_pending_office_approvals(ws.config, ws.ledger, "alpha")
    kinds = {entry["kind"] for entry in pending}
    assert "install" in kinds
    install_entry = next(e for e in pending if e["kind"] == "install")
    assert install_entry["job_name"] == job_name
    assert install_entry["attempt_id"] == attempt_id
    preview = install_entry["request_preview"]
    assert preview == [{"manifest": "package.json", "change": {"dependencies": ["zod"]}}]
    # section/key NAMES only -- never a value (e.g. the version string)
    assert "3.0.0" not in str(preview)


def test_list_pending_office_approvals_reports_design_when_plan_reviewed_not_approved(
    workspace: Workspace,
) -> None:
    job_name = "office-list-2"
    attempt_id = _open_attempt(workspace, job_name)
    _stop_with_design_approval_pending(workspace, job_name, attempt_id)

    pending = app_office.list_pending_office_approvals(workspace.config, workspace.ledger, "alpha")
    kinds = {entry["kind"] for entry in pending}
    assert "design" in kinds


def test_list_pending_office_approvals_omits_design_when_plan_never_reviewed(
    workspace: Workspace,
) -> None:
    """merely "not design.approved yet" (true for every
    job that never even reached a design station) must not be reported
    as a pending design approval."""
    job_name = "office-list-3"
    _open_attempt(workspace, job_name)

    pending = app_office.list_pending_office_approvals(workspace.config, workspace.ledger, "alpha")
    kinds = {entry["kind"] for entry in pending}
    assert "design" not in kinds


# ---------------------------------------------------------------------------
# load_office_secret TOCTOU hardening
# ---------------------------------------------------------------------------

def test_load_office_secret_refuses_a_symlink_at_the_key_path(
    workspace: Workspace, tmp_path: Path,
) -> None:
    real_key = tmp_path / "real.key"
    real_key.write_bytes(b"real-secret")
    real_key.chmod(0o600)
    key_path = workspace.config.secrets_dir / app_office.KEY_FILENAME
    key_path.symlink_to(real_key)

    secret, reason = app_office.load_office_secret(workspace.config)
    assert (secret, reason) == (None, "office_key_missing")


def test_load_office_secret_refuses_a_fifo(workspace: Workspace) -> None:
    import os as _os

    fifo_path = workspace.config.secrets_dir / app_office.KEY_FILENAME
    _os.mkfifo(fifo_path, 0o600)

    secret, reason = app_office.load_office_secret(workspace.config)
    assert (secret, reason) == (None, "office_key_not_regular_file")


def test_load_office_secret_checks_permissions_and_reads_the_same_file_descriptor(
    workspace: Workspace,
) -> None:
    """Regression for the reproduced TOCTOU: `stat()` then a
    SEPARATE `read_bytes()` re-resolves the path twice, so swapping the
    file between the two calls bypasses the permission check done
    against the first file. load_office_secret must check and read via
    one open file descriptor instead -- this test only proves the
    happy path still reads the real, checked file's bytes (the race
    itself is not practically triggerable from a single-threaded unit
    test without a TOCTOU window to land in; the fix's own docstring
    explains why a single fd closes it structurally)."""
    _key_file(workspace, secret=b"single-fd-secret", mode=0o600)

    secret, reason = app_office.load_office_secret(workspace.config)
    assert (secret, reason) == (b"single-fd-secret", "ok")


# ---------------------------------------------------------------------------
# install_target_version includes proposal_id
# ---------------------------------------------------------------------------

def test_install_target_version_changes_when_the_identical_request_is_reproposed(
    workspace: Workspace,
) -> None:
    """An office token signed for proposal A's batch must not verify
    against an identical-content LATER proposal B for the same
    request -- install_target_version folds in each request's
    proposal_id (engine.application.job_state_requests.proposal_id)
    precisely so the hash differs once a new station.finished event
    re-proposes the same content."""
    from engine.ledger import Event as _Event

    ws = _with_manifest(workspace, "package.json")
    job_name = "office-reproposal"
    attempt_id = _open_attempt(ws, job_name)
    request = dict(_INSTALL_REQUEST)

    ws.ledger.append(_Event(
        project="alpha", kind="station.finished", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"request_id": "R-station-1", "station": "build", "role": "builder",
                 "ok": True, "requests": [request]},
    ))
    record_position(ws.ledger, "alpha", JobPosition(
        job=job_name, stage="awaiting_task_owner", attempt_id=attempt_id,
        pending_requests=(request,), stop_reason="owner_request",
    ))
    version_a = app_office.install_target_version(ws.ledger, "alpha", job_name, [request])

    # A second, LATER proposal with byte-for-byte identical content.
    ws.ledger.append(_Event(
        project="alpha", kind="station.finished", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"request_id": "R-station-2", "station": "build", "role": "builder",
                 "ok": True, "requests": [request]},
    ))
    version_b = app_office.install_target_version(ws.ledger, "alpha", job_name, [request])

    assert version_a != version_b


# ---------------------------------------------------------------------------
# plan_edit_target_version (companion to design_target_version)
# ---------------------------------------------------------------------------

def test_plan_edit_target_version_tracks_plan_digest(workspace: Workspace) -> None:
    from engine.domain.plan_review import plan_digest as _plan_digest

    job_name = "office-plan-edit-digest"
    _open_attempt(workspace, job_name)
    plan_file = workspace.alpha / ".gongbang" / job_name / "plan.json"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text('{"goal": "v1"}\n', encoding="utf-8")

    assert (app_office.plan_edit_target_version(workspace.alpha, job_name)
            == _plan_digest(plan_file))

    plan_file.write_text('{"goal": "v2"}\n', encoding="utf-8")
    assert (app_office.plan_edit_target_version(workspace.alpha, job_name)
            == _plan_digest(plan_file))


# ---------------------------------------------------------------------------
# P2-3 crash-recovery note: nonce consumption is NOT in the same SQL
# transaction as the real approval (owner_approval.approve_pending/
# record_owner_plan_amend each commit their own ledger events through
# separate Ledger.append calls in a DIFFERENT module, and Processor.
# submit is itself multi-step) -- making every step of that span into
# one literal transaction would require reworking Processor/
# owner_approval/plan_review far beyond this feature's scope. Instead:
# a crash between "nonce consumed" and "approval durably recorded"
# burns that ONE token (it can never be replayed, which is the actual
# security property nonces exist for) but never loses the approval
# itself -- the office server mints a FRESH token (new nonce, same
# target_version, since nothing was approved yet so the pending batch
# is unchanged) and retries. This test proves that recovery path
# actually works, not merely that the old token is burned.
# ---------------------------------------------------------------------------

def test_a_burned_nonce_is_recoverable_with_a_freshly_issued_token(
    workspace: Workspace,
) -> None:
    ws = _ws_with_key(_with_manifest(workspace, "package.json"))
    job_name = "office-install-crash-recovery"
    attempt_id = _open_attempt(ws, job_name)
    _stop_with_pending_install(ws, job_name, attempt_id, _INSTALL_REQUEST)

    token1 = app_office.issue_office_approval_token(
        ws.config, ws.ledger, "alpha", job_name, attempt_id, "install",
    )
    # First verification succeeds and consumes token1's nonce -- then,
    # simulating a crash, the caller never goes on to actually call
    # owner_approval.approve_pending with it.
    ok1, reason1 = app_office.verify_office_token(
        ws.config, ws.ledger, "alpha", job_name, attempt_id, "install", token1,
    )
    assert (ok1, reason1) == (True, "ok")

    # token1 itself can never be replayed (it is genuinely spent).
    replay_ok, replay_reason = app_office.verify_office_token(
        ws.config, ws.ledger, "alpha", job_name, attempt_id, "install", token1,
    )
    assert (replay_ok, replay_reason) == (False, "nonce_reused")

    # Recovery: the pending batch never actually changed (nothing was
    # approved), so a FRESH token for the SAME target_version verifies
    # fine, with its own, different nonce.
    token2 = app_office.issue_office_approval_token(
        ws.config, ws.ledger, "alpha", job_name, attempt_id, "install",
    )
    assert token2["nonce"] != token1["nonce"]
    ok2, reason2 = app_office.verify_office_token(
        ws.config, ws.ledger, "alpha", job_name, attempt_id, "install", token2,
    )
    assert (ok2, reason2) == (True, "ok")


# ---------------------------------------------------------------------------
# design artifact read/enumeration failure
# must refuse, never hash as empty.
# ---------------------------------------------------------------------------

def test_design_target_version_raises_when_an_artifact_is_unreadable(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_name = "office-design-unreadable"
    _open_attempt(workspace, job_name)
    plan_file = workspace.alpha / ".gongbang" / job_name / "plan.json"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text('{"goal": "v1"}\n', encoding="utf-8")
    design_dir = workspace.alpha / ".gongbang" / job_name / "design"
    design_dir.mkdir(parents=True)
    design_md = design_dir / "design.md"
    design_md.write_text("# approved comp\n", encoding="utf-8")

    real_read_bytes = Path.read_bytes

    def _boom(self: Path, *a, **kw):
        if self == design_md:
            raise PermissionError("synthetic read failure")
        return real_read_bytes(self, *a, **kw)

    monkeypatch.setattr(Path, "read_bytes", _boom)

    with pytest.raises(app_office.DesignArtifactUnreadable):
        app_office.design_target_version(workspace.alpha, job_name)


def test_verify_office_token_refuses_when_design_artifact_becomes_unreadable(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """the previous implementation treated
    an unreadable file as empty, so an owner-approved comp replaced by
    something now unreadable kept verifying against the STALE (empty)
    hash as if nothing had changed. Must refuse instead."""
    ws = _ws_with_key(workspace)
    job_name = "office-design-unreadable-2"
    attempt_id = _open_attempt(ws, job_name)
    plan_file = ws.alpha / ".gongbang" / job_name / "plan.json"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text('{"goal": "v1"}\n', encoding="utf-8")
    _stop_with_design_approval_pending(ws, job_name, attempt_id)

    token = app_office.issue_office_approval_token(
        ws.config, ws.ledger, "alpha", job_name, attempt_id, "design",
    )

    design_dir = ws.alpha / ".gongbang" / job_name / "design"
    design_dir.mkdir(parents=True)
    optional = design_dir / "optional.png"
    optional.write_bytes(b"")

    real_read_bytes = Path.read_bytes

    def _boom(self: Path, *a, **kw):
        if self == optional:
            raise PermissionError("synthetic read failure")
        return real_read_bytes(self, *a, **kw)

    monkeypatch.setattr(Path, "read_bytes", _boom)

    ok, reason = app_office.verify_office_token(
        ws.config, ws.ledger, "alpha", job_name, attempt_id, "design", token,
    )
    assert (ok, reason) == (False, "design_artifact_unreadable")


# ---------------------------------------------------------------------------
# design_approval_still_matches -- the resume-time staleness check
# ---------------------------------------------------------------------------

def test_design_approval_still_matches_true_with_no_approval_yet(
    workspace: Workspace,
) -> None:
    job_name = "office-design-resume-1"
    _open_attempt(workspace, job_name)
    assert app_office.design_approval_still_matches(
        workspace.ledger, "alpha", job_name, workspace.alpha) is True


def test_design_approval_still_matches_detects_a_swapped_artifact_after_approval(
    workspace: Workspace,
) -> None:
    from engine.ledger import Event as _Event

    job_name = "office-design-resume-2"
    attempt_id = _open_attempt(workspace, job_name)
    plan_file = workspace.alpha / ".gongbang" / job_name / "plan.json"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text('{"goal": "v1"}\n', encoding="utf-8")
    design_dir = workspace.alpha / ".gongbang" / job_name / "design"
    design_dir.mkdir(parents=True)
    design_md = design_dir / "design.md"
    design_md.write_text("# approved comp\n", encoding="utf-8")

    approved_target_version = app_office.design_target_version(workspace.alpha, job_name)
    workspace.ledger.append(_Event(
        project="alpha", kind="design.approved", actor_kind="owner",
        actor_id="owner", attempt_id=attempt_id,
        payload={"job": job_name, "confirm_source": "tty",
                 "target_version": approved_target_version},
    ))
    assert app_office.design_approval_still_matches(
        workspace.ledger, "alpha", job_name, workspace.alpha) is True

    # The design deliverable is swapped AFTER approval.
    design_md.write_text("# swapped comp, never approved\n", encoding="utf-8")
    assert app_office.design_approval_still_matches(
        workspace.ledger, "alpha", job_name, workspace.alpha) is False


def test_design_approval_still_matches_true_for_legacy_event_without_target_version(
    workspace: Workspace,
) -> None:
    """A design.approved event recorded before this field existed must
    never be retroactively invalidated."""
    from engine.ledger import Event as _Event

    job_name = "office-design-resume-3"
    attempt_id = _open_attempt(workspace, job_name)
    workspace.ledger.append(_Event(
        project="alpha", kind="design.approved", actor_kind="owner",
        actor_id="owner", attempt_id=attempt_id,
        payload={"job": job_name, "confirm_source": "tty"},
    ))
    assert app_office.design_approval_still_matches(
        workspace.ledger, "alpha", job_name, workspace.alpha) is True


# ---------------------------------------------------------------------------
# pinned_install_batch -- one lookup shared by both the hash and the
# actual submission
# ---------------------------------------------------------------------------

def test_pinned_install_batch_proposal_matches_the_hash_it_was_computed_from(
    workspace: Workspace,
) -> None:
    ws = _with_manifest(workspace, "package.json")
    job_name = "office-pinned-batch"
    attempt_id = _open_attempt(ws, job_name)
    request = dict(_INSTALL_REQUEST)
    from engine.ledger import Event as _Event

    ws.ledger.append(_Event(
        project="alpha", kind="station.finished", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"request_id": "R-station-1", "station": "build", "role": "builder",
                 "ok": True, "requests": [request]},
    ))
    record_position(ws.ledger, "alpha", JobPosition(
        job=job_name, stage="awaiting_task_owner", attempt_id=attempt_id,
        pending_requests=(request,), stop_reason="owner_request",
    ))

    batch = app_office.pinned_install_batch(ws.ledger, "alpha", job_name, [request])
    assert len(batch) == 1
    pinned_proposal = batch[0]["proposal"]
    assert pinned_proposal is not None

    # A LATER proposal for the identical content lands after the batch
    # was already pinned -- the pinned batch's own proposal must stay
    # the FIRST one, exactly what install_target_version_from_batch
    # hashed, never silently re-resolved to the newer one.
    ws.ledger.append(_Event(
        project="alpha", kind="station.finished", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"request_id": "R-station-2", "station": "build", "role": "builder",
                 "ok": True, "requests": [request]},
    ))
    assert batch[0]["proposal"] == pinned_proposal
    hash_from_batch = app_office.install_target_version_from_batch(batch)
    assert hash_from_batch == app_office.ids.content_hash(batch)


# ---------------------------------------------------------------------------
# directory ENUMERATION failure (not just a single
# file's read) must also refuse, never silently return {} as if the
# directory were still empty -- Path.rglob swallows this on this Python
# version; os.walk(onerror=...) must not.
# ---------------------------------------------------------------------------

def test_design_target_version_raises_when_directory_enumeration_fails(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_name = "office-design-enum-failure"
    _open_attempt(workspace, job_name)
    plan_file = workspace.alpha / ".gongbang" / job_name / "plan.json"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text('{"goal": "v1"}\n', encoding="utf-8")
    design_dir = workspace.alpha / ".gongbang" / job_name / "design"
    design_dir.mkdir(parents=True)

    # Sign/compute against the EMPTY design dir first -- this is the
    # exact sequence a review reproduced: issue while
    # empty, THEN make a later enumeration of that same directory fail.
    empty_version = app_office.design_target_version(workspace.alpha, job_name)

    (design_dir / "design.md").write_text("# late-arriving comp\n", encoding="utf-8")
    real_scandir = os.scandir

    def _boom(path=None):
        if Path(path) == design_dir:
            raise PermissionError("synthetic enumeration failure")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", _boom)

    with pytest.raises(app_office.DesignArtifactUnreadable):
        app_office.design_target_version(workspace.alpha, job_name)

    # Never silently treat the failed listing as "still empty" -- the
    # version must NOT quietly stay equal to the pre-failure one.
    monkeypatch.undo()
    assert empty_version != app_office.design_target_version(workspace.alpha, job_name)


def test_verify_office_token_refuses_when_directory_enumeration_fails(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end repro of the exact review scenario: issue a token
    while design/ is empty, then a file arrives and the directory's
    OWN enumeration (not a single file's read) starts failing --
    verify_office_token must refuse, never silently accept the stale,
    pre-failure (empty) hash as still current."""
    ws = _ws_with_key(workspace)
    job_name = "office-design-enum-failure-2"
    attempt_id = _open_attempt(ws, job_name)
    plan_file = ws.alpha / ".gongbang" / job_name / "plan.json"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text('{"goal": "v1"}\n', encoding="utf-8")
    design_dir = ws.alpha / ".gongbang" / job_name / "design"
    design_dir.mkdir(parents=True)
    _stop_with_design_approval_pending(ws, job_name, attempt_id)

    token = app_office.issue_office_approval_token(
        ws.config, ws.ledger, "alpha", job_name, attempt_id, "design",
    )

    (design_dir / "design.md").write_text("# late-arriving comp\n", encoding="utf-8")
    real_scandir = os.scandir

    def _boom(path=None):
        if Path(path) == design_dir:
            raise PermissionError("synthetic enumeration failure")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", _boom)

    ok, reason = app_office.verify_office_token(
        ws.config, ws.ledger, "alpha", job_name, attempt_id, "design", token,
    )
    assert (ok, reason) == (False, "design_artifact_unreadable")
