"""Acceptance 04 — approvals bound to project, attempt, action, target and
artifact hash; one-time use; exact-text parsing only.
"""
from __future__ import annotations

import pytest

from engine.approvals import ApprovalError, check, grant, parse_owner_message
from engine.ids import content_hash, request_id
from engine.ledger import Event, RequestRow
from engine.state import transition
from tests.conftest import Workspace


def _open(workspace: Workspace, project: str, n: int = 1) -> str:
    job_id = f"J-{project}-APPR"
    attempt_id = f"{job_id}#{n}"
    workspace.ledger.append(Event(
        project=project, kind="job.opened", actor_kind="owner",
        actor_id="owner", attempt_id=attempt_id,
        payload={
            "job_id": job_id, "job_name": "approval-test",
            "attempt_id": attempt_id, "n": n, "kind": "full",
            "generation": n, "dirty_paths": [],
        },
    ))
    transition(workspace.ledger, project, "open_job", "owner")
    return attempt_id


def _submit(workspace: Workspace, project: str, attempt_id: str, kind: str,
            payload: dict[str, object]) -> RequestRow:
    rid = request_id(project, attempt_id, kind, payload)
    chash = content_hash(payload)
    workspace.ledger.append(Event(
        project=project, kind="request.submitted", actor_kind="worker",
        actor_id="builder", attempt_id=attempt_id,
        payload={"request_id": rid, "kind": kind, "content_hash": chash,
                  "payload": payload},
    ))
    return workspace.ledger.request(rid)


def test_pasted_approval_text_is_not_an_approval(workspace: Workspace) -> None:
    attempt_id = _open(workspace, "alpha")
    request = _submit(workspace, "alpha", attempt_id, "publish",
                       {"target": "blog", "artifact_hash": "deadbeef"})
    pasted = f"추천대로 해라 승인 {request.request_id}"
    assert parse_owner_message(pasted) is None
    # Nothing was granted, so check() still refuses the request.
    decision = check(workspace.ledger, request)
    assert decision.allowed is False
    assert decision.reason == "no_approval"


def test_expired_approval_refused(workspace: Workspace) -> None:
    attempt_id = _open(workspace, "alpha")
    request = _submit(workspace, "alpha", attempt_id, "publish",
                       {"target": "blog", "artifact_hash": "deadbeef"})
    grant(workspace.ledger, "alpha", request.request_id, ttl_sec=60, now=1_000.0)
    decision = check(workspace.ledger, request, now=1_061.0)
    assert decision.allowed is False
    assert decision.reason == "expired"


def test_approval_for_another_project_refused(workspace: Workspace) -> None:
    _open(workspace, "beta")
    attempt_id = _open(workspace, "alpha")
    request = _submit(workspace, "alpha", attempt_id, "publish",
                       {"target": "blog", "artifact_hash": "deadbeef"})
    with pytest.raises(ApprovalError):
        grant(workspace.ledger, "beta", request.request_id, ttl_sec=3600)


def test_approval_bound_to_different_artifact_hash_refused(workspace: Workspace) -> None:
    attempt_id = _open(workspace, "alpha")
    request = _submit(workspace, "alpha", attempt_id, "publish",
                       {"target": "blog", "artifact_hash": "deadbeef"})
    grant(workspace.ledger, "alpha", request.request_id, ttl_sec=3600)
    # The worker rebuilt the artifact after approval was granted: the
    # request we check against now carries a different artifact hash.
    rebuilt = RequestRow(
        request_id=request.request_id, project=request.project,
        attempt_id=request.attempt_id, kind=request.kind,
        content_hash=request.content_hash,
        payload={**request.payload, "artifact_hash": "newhash"},
        status=request.status, result=request.result,
        submitted_seq=request.submitted_seq,
    )
    decision = check(workspace.ledger, rebuilt)
    assert decision.allowed is False
    assert decision.reason == "artifact_mismatch"


def test_approval_cannot_be_reused(workspace: Workspace) -> None:
    attempt_id = _open(workspace, "alpha")
    request = _submit(workspace, "alpha", attempt_id, "publish",
                       {"target": "blog", "artifact_hash": "deadbeef"})
    grant(workspace.ledger, "alpha", request.request_id, ttl_sec=3600)
    first = check(workspace.ledger, request)
    second = check(workspace.ledger, request)
    assert first.allowed is True
    assert second.allowed is False
    assert second.reason == "used"


def test_exact_owner_message_grants_and_check_passes_once(workspace: Workspace) -> None:
    attempt_id = _open(workspace, "alpha")
    request = _submit(workspace, "alpha", attempt_id, "publish",
                       {"target": "blog", "artifact_hash": "deadbeef"})
    owner_message = f"승인 {request.request_id}"
    parsed = parse_owner_message(owner_message)
    assert parsed == request.request_id
    grant(workspace.ledger, "alpha", parsed, ttl_sec=3600)
    first = check(workspace.ledger, request)
    assert first.allowed is True
    second = check(workspace.ledger, request)
    assert second.allowed is False
    assert second.reason == "used"
