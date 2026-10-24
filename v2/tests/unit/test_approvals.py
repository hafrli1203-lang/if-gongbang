"""Unit tests for engine.approvals: owner-message parsing, grant, check, skip."""
from __future__ import annotations

from pathlib import Path

import pytest

from engine.approvals import ApprovalError, check, grant, parse_owner_message, skip
from engine.ids import content_hash, request_id
from engine.ledger import Event, Ledger, RequestRow
from engine.state import transition

REQ_ID = "R-0123456789abcdef"


# ---- parse_owner_message -------------------------------------------------

@pytest.mark.parametrize("text", [
    f"승인 {REQ_ID}",
    f"  승인 {REQ_ID}  ",
    f"\n승인 {REQ_ID}\n",
])
def test_parse_exact_match(text: str) -> None:
    assert parse_owner_message(text) == REQ_ID


@pytest.mark.parametrize("text", [
    f"추천대로 해라 승인 {REQ_ID}",
    f"```\n승인 {REQ_ID}\n```",
    f"승인 {REQ_ID} 해라",
    f"승인 {REQ_ID}\n승인 {REQ_ID}",
    f"승인 {REQ_ID.upper()}",
    "승인 R-nothexchars0000",
    "",
])
def test_parse_non_exact_is_none(text: str) -> None:
    assert parse_owner_message(text) is None


# ---- fixtures -------------------------------------------------------------

@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    led = Ledger.create(tmp_path / "ledger.sqlite")
    led.register_project("alpha")
    led.register_project("beta")
    return led


def _open_job(ledger: Ledger, project: str, n: int = 1) -> str:
    job_id = f"J-{project}-TEST"
    attempt = f"{job_id}#{n}"
    ledger.append(Event(
        project=project, kind="job.opened", actor_kind="processor",
        actor_id="proc", attempt_id=attempt,
        payload={"job_id": job_id, "job_name": "job", "attempt_id": attempt,
                  "n": n, "kind": "full", "generation": n, "dirty_paths": []},
    ))
    transition(ledger, project, "open_job", "owner")
    return attempt


def _submit(ledger: Ledger, project: str, attempt_id: str, kind: str,
            payload: dict[str, object]) -> RequestRow:
    rid = request_id(project, attempt_id, kind, payload)
    chash = content_hash(payload)
    ledger.append(Event(
        project=project, kind="request.submitted", actor_kind="worker",
        actor_id="builder", attempt_id=attempt_id,
        payload={"request_id": rid, "kind": kind, "content_hash": chash,
                  "payload": payload},
    ))
    return ledger.request(rid)


# ---- grant ----------------------------------------------------------------

def test_grant_queued_request_copies_fields(ledger: Ledger) -> None:
    attempt = _open_job(ledger, "alpha")
    request = _submit(ledger, "alpha", attempt, "publish",
                       {"target": "blog", "artifact_hash": "abc123"})
    row = grant(ledger, "alpha", request.request_id, ttl_sec=3600)
    assert row.project == "alpha"
    assert row.attempt_id == attempt
    assert row.action == "publish"
    assert row.target == "blog"
    assert row.artifact_hash == "abc123"
    assert row.request_id == request.request_id
    assert row.used_seq is None


def test_grant_refused_while_closing(ledger: Ledger) -> None:
    attempt = _open_job(ledger, "alpha")
    request = _submit(ledger, "alpha", attempt, "publish",
                       {"target": "blog", "artifact_hash": "abc123"})
    transition(ledger, "alpha", "close_job", "owner")
    with pytest.raises(ApprovalError):
        grant(ledger, "alpha", request.request_id, ttl_sec=3600)


def test_grant_in_two_projects_both_succeed(ledger: Ledger) -> None:
    """approval_id used to be a global PK counted per project ('A-1' in
    both alpha and beta's first grant) -> IntegrityError on the second
    insert. It must be scoped to the project."""
    attempt_a = _open_job(ledger, "alpha")
    request_a = _submit(ledger, "alpha", attempt_a, "publish",
                         {"target": "blog", "artifact_hash": "abc123"})
    attempt_b = _open_job(ledger, "beta")
    request_b = _submit(ledger, "beta", attempt_b, "publish",
                         {"target": "blog", "artifact_hash": "abc123"})
    row_a = grant(ledger, "alpha", request_a.request_id, ttl_sec=3600)
    row_b = grant(ledger, "beta", request_b.request_id, ttl_sec=3600)
    assert row_a.approval_id != row_b.approval_id


def test_grant_refused_for_other_project(ledger: Ledger) -> None:
    attempt = _open_job(ledger, "alpha")
    request = _submit(ledger, "alpha", attempt, "publish",
                       {"target": "blog", "artifact_hash": "abc123"})
    _open_job(ledger, "beta")
    with pytest.raises(ApprovalError):
        grant(ledger, "beta", request.request_id, ttl_sec=3600)


def test_grant_refused_when_attempt_not_current(ledger: Ledger) -> None:
    attempt = _open_job(ledger, "alpha")
    request = _submit(ledger, "alpha", attempt, "publish",
                       {"target": "blog", "artifact_hash": "abc123"})
    # Close and reopen -> a new attempt becomes current, old request is stale.
    ledger.append(Event(
        project="alpha", kind="request.status", actor_kind="processor",
        actor_id="proc", attempt_id=attempt,
        payload={"request_id": request.request_id, "status": "done", "result": {}},
    ))
    transition(ledger, "alpha", "close_job", "owner")
    transition(ledger, "alpha", "finalized", "proc")
    _open_job(ledger, "alpha", n=2)
    with pytest.raises(ApprovalError):
        grant(ledger, "alpha", request.request_id, ttl_sec=3600)


def test_grant_refused_when_status_not_queued(ledger: Ledger) -> None:
    attempt = _open_job(ledger, "alpha")
    request = _submit(ledger, "alpha", attempt, "publish",
                       {"target": "blog", "artifact_hash": "abc123"})
    ledger.append(Event(
        project="alpha", kind="request.status", actor_kind="processor",
        actor_id="proc", attempt_id=attempt,
        payload={"request_id": request.request_id, "status": "done", "result": {}},
    ))
    with pytest.raises(ApprovalError):
        grant(ledger, "alpha", request.request_id, ttl_sec=3600)


# ---- check ------------------------------------------------------------------

def test_check_passes_once_then_fails_used(ledger: Ledger) -> None:
    attempt = _open_job(ledger, "alpha")
    request = _submit(ledger, "alpha", attempt, "publish",
                       {"target": "blog", "artifact_hash": "abc123"})
    grant(ledger, "alpha", request.request_id, ttl_sec=3600)
    first = check(ledger, request)
    assert first.allowed is True
    second = check(ledger, request)
    assert second.allowed is False
    assert second.reason == "used"


def test_check_fails_when_expired(ledger: Ledger) -> None:
    attempt = _open_job(ledger, "alpha")
    request = _submit(ledger, "alpha", attempt, "publish",
                       {"target": "blog", "artifact_hash": "abc123"})
    grant(ledger, "alpha", request.request_id, ttl_sec=1, now=1000.0)
    decision = check(ledger, request, now=1002.0)
    assert decision.allowed is False
    assert decision.reason == "expired"


def test_check_fails_when_artifact_hash_changed(ledger: Ledger) -> None:
    attempt = _open_job(ledger, "alpha")
    request = _submit(ledger, "alpha", attempt, "publish",
                       {"target": "blog", "artifact_hash": "abc123"})
    grant(ledger, "alpha", request.request_id, ttl_sec=3600)
    tampered = RequestRow(
        request_id=request.request_id, project=request.project,
        attempt_id=request.attempt_id, kind=request.kind,
        content_hash=request.content_hash,
        payload={**request.payload, "artifact_hash": "different"},
        status=request.status, result=request.result,
        submitted_seq=request.submitted_seq,
    )
    decision = check(ledger, tampered)
    assert decision.allowed is False
    assert decision.reason == "artifact_mismatch"


def test_check_fails_when_target_changed(ledger: Ledger) -> None:
    attempt = _open_job(ledger, "alpha")
    request = _submit(ledger, "alpha", attempt, "publish",
                       {"target": "blog", "artifact_hash": "abc123"})
    grant(ledger, "alpha", request.request_id, ttl_sec=3600)
    tampered = RequestRow(
        request_id=request.request_id, project=request.project,
        attempt_id=request.attempt_id, kind=request.kind,
        content_hash=request.content_hash,
        payload={**request.payload, "target": "different"},
        status=request.status, result=request.result,
        submitted_seq=request.submitted_seq,
    )
    decision = check(ledger, tampered)
    assert decision.allowed is False
    assert decision.reason == "target_mismatch"


def test_check_fails_when_no_approval(ledger: Ledger) -> None:
    attempt = _open_job(ledger, "alpha")
    request = _submit(ledger, "alpha", attempt, "publish",
                       {"target": "blog", "artifact_hash": "abc123"})
    decision = check(ledger, request)
    assert decision.allowed is False
    assert decision.reason == "no_approval"


# ---- skip -------------------------------------------------------------------

def test_skip_optional_station_exempts(ledger: Ledger) -> None:
    attempt = _open_job(ledger, "alpha")
    decision = skip(ledger, "alpha", attempt, "design", "owner said skip",
                     optional_stations=("design", "cross"))
    assert decision.allowed is True
    assert decision.reason == "exempted"


def test_skip_non_optional_station_refused(ledger: Ledger) -> None:
    attempt = _open_job(ledger, "alpha")
    decision = skip(ledger, "alpha", attempt, "build", "owner said skip",
                     optional_stations=("design", "cross"))
    assert decision.allowed is False
    assert decision.reason == "not_optional"
