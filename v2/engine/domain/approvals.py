"""Owner approvals bound to project, attempt, action, target, artifact hash,
request id and expiry. One-time use.

parse_owner_message(): the WHOLE message must match
    ^\\s*승인\\s+(R-[0-9a-f]{16})\\s*$
Anything else (code fences, surrounding words, mid-sentence) is None.
"""
from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from engine.domain.records import ApprovalRow, Event, LedgerError, RequestRow
from engine.domain.policy import Decision

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger

APPROVAL_RE = re.compile(r"^\s*승인\s+(R-[0-9a-f]{16})\s*$")


class ApprovalError(Exception):
    pass


def parse_owner_message(text: str) -> str | None:
    """Return the request id or None."""
    match = APPROVAL_RE.fullmatch(text)
    if match is None:
        return None
    return match.group(1)


def _grantable_request(ledger: Ledger, project: str, request_id: str) -> RequestRow:
    """Look up the request and apply grant()'s refusal checks (ApprovalError
    on: unknown request, project CLOSING, wrong project/attempt, or the
    request is not queued)."""
    try:
        request = ledger.request(request_id)
    except LedgerError as exc:
        raise ApprovalError(f"unknown request: {request_id}") from exc
    proj_row = ledger.project(project)
    if proj_row.state == "CLOSING":
        raise ApprovalError("project is CLOSING: approvals refused")
    if request.project != project:
        raise ApprovalError(
            f"request {request_id} belongs to project {request.project!r}, not {project!r}"
        )
    if request.attempt_id != proj_row.current_attempt_id:
        raise ApprovalError(
            f"request {request_id} is not for the current attempt"
        )
    if request.status != "queued":
        raise ApprovalError(
            f"request {request_id} has status {request.status!r}, not queued"
        )
    return request


def grant(ledger: Ledger, project: str, request_id: str, ttl_sec: int,
          now: float | None = None) -> ApprovalRow:
    """Grant approval for a queued request of the current attempt; copies
    action/target/artifact_hash from the request at grant time. Refused
    (ApprovalError) when CLOSING, unknown, not queued, other project/attempt."""
    request = _grantable_request(ledger, project, request_id)
    now_ts = now if now is not None else time.time()
    granted_so_far = len(ledger.events(project, "approval.granted"))
    # approvals.approval_id is a global primary key, but the counter above
    # is per project: two projects each granting their first approval
    # would both compute "A-1" and the second insert would raise
    # IntegrityError. Scope the id to the project.
    approval_id = f"A-{project}-{granted_so_far + 1}"
    target = str(request.payload.get("target", ""))
    artifact_hash = str(request.payload.get("artifact_hash", ""))
    expires_ts = now_ts + ttl_sec

    ledger.append(Event(
        project=project, kind="approval.granted", actor_kind="owner",
        actor_id="owner", attempt_id=request.attempt_id,
        payload={
            "approval_id": approval_id,
            "action": request.kind,
            "target": target,
            "artifact_hash": artifact_hash,
            "request_id": request_id,
            "expires_ts": expires_ts,
        },
    ))
    for row in ledger.approvals_for(request_id):
        if row.approval_id == approval_id:
            return row
    raise ApprovalError("approval was not recorded")


def check(ledger: Ledger, request: RequestRow, now: float | None = None) -> Decision:
    """Allowed iff an unused, unexpired approval exists whose project,
    attempt_id, action, target, artifact_hash and request_id all equal the
    request's current values. On success marks it used (approval.used)."""
    now_ts = now if now is not None else time.time()
    target = str(request.payload.get("target", ""))
    artifact_hash = str(request.payload.get("artifact_hash", ""))
    reason = "no_approval"
    for row in ledger.approvals_for(request.request_id):
        if row.used_seq is not None:
            reason = "used"
            continue
        if row.expires_ts <= now_ts:
            reason = "expired"
            continue
        if row.project != request.project:
            reason = "project_mismatch"
            continue
        if row.attempt_id != request.attempt_id:
            reason = "attempt_mismatch"
            continue
        if row.action != request.kind:
            reason = "action_mismatch"
            continue
        if row.target != target:
            reason = "target_mismatch"
            continue
        if row.artifact_hash != artifact_hash:
            reason = "artifact_mismatch"
            continue
        if row.request_id != request.request_id:
            reason = "request_mismatch"
            continue
        ledger.append(Event(
            project=request.project, kind="approval.used",
            actor_kind="processor", actor_id="processor",
            attempt_id=request.attempt_id,
            payload={"approval_id": row.approval_id},
        ))
        return Decision(True, "approved")
    return Decision(False, reason)


def skip(ledger: Ledger, project: str, attempt_id: str, station: str,
         reason: str, optional_stations: tuple[str, ...]) -> Decision:
    """Owner exemption for an optional station only. Records
    'station.exempted' (never 'passed')."""
    if station not in optional_stations:
        return Decision(False, "not_optional")
    ledger.append(Event(
        project=project, kind="station.exempted", actor_kind="owner",
        actor_id="owner", attempt_id=attempt_id,
        payload={"station": station, "reason": reason},
    ))
    return Decision(True, "exempted")
