"""The records the engine's ledger stores: one Event shape, the derived
row shapes read back from it, and the fixed status/kind constants.
Plain data, no storage -- engine.infrastructure.db.ledger is the SQLite
store that writes and reads them."""
from __future__ import annotations

from dataclasses import dataclass, field

# The ONLY kind engine.application.steps.worker_step._import_gate_log is ever allowed to
# append under actor_kind "gate_log" -- see Ledger.append()'s check. That
# importer re-files a worker-writable log (engine.interface.hook_gate's gate.log) into
# the ledger; everything it appends must land under this one inert kind
# (no _HANDLERS entry, never matched by any reconcile() query in this
# codebase) so a worker-forged log line can never trigger a real
# derived-table effect (grant an approval, mark a station finished, ...)
# by smuggling some other kind through. This is deliberately enforced
# HERE too, not only by the importer's own discipline, as defense in
# depth: a bug that reintroduced forwarding
# the log line's own kind would still be refused at this one choke
# point before it ever reaches a handler.
GATE_LOG_IMPORT_KIND = "gate_log.decision"

STATES = ("CLOSED", "OPEN", "CLOSING", "RECOVERING")
REQUEST_STATUSES = ("queued", "running", "done", "failed", "unknown")
STEP_STATUSES = ("started", "done")
EVIDENCE_KINDS = ("source", "review", "bench", "cross", "build", "publish")


class LedgerError(Exception):
    """Ledger missing, corrupt, wrong version, or invariant violated."""


@dataclass(frozen=True)
class Event:
    project: str
    kind: str
    actor_kind: str  # owner | worker | processor
    actor_id: str
    attempt_id: str | None = None
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectRow:
    project: str
    state: str
    current_attempt_id: str | None
    generation: int
    last_request_no: int
    dirty_paths: tuple[str, ...]


@dataclass(frozen=True)
class AttemptRow:
    attempt_id: str
    job_id: str
    n: int
    kind: str  # full | small
    generation: int
    status: str  # active | closed | abandoned | interrupted


@dataclass(frozen=True)
class RequestRow:
    request_id: str
    project: str
    attempt_id: str
    kind: str
    content_hash: str
    payload: dict[str, object]
    status: str
    result: dict[str, object]
    submitted_seq: int


@dataclass(frozen=True)
class StepRow:
    request_id: str
    step_name: str
    status: str
    payload: dict[str, object]


@dataclass(frozen=True)
class ApprovalRow:
    approval_id: str
    project: str
    attempt_id: str
    action: str
    target: str
    artifact_hash: str
    request_id: str
    expires_ts: float
    used_seq: int | None


@dataclass(frozen=True)
class EvidenceRow:
    evidence_id: int
    project: str
    attempt_id: str
    kind: str
    fingerprint: str
    artifact_hash: str | None
    stale: bool
    payload: dict[str, object]
    seq: int


__all__ = [
    "GATE_LOG_IMPORT_KIND", "STATES", "REQUEST_STATUSES", "STEP_STATUSES", "EVIDENCE_KINDS",
    "LedgerError", "Event", "ProjectRow", "AttemptRow", "RequestRow", "StepRow", "ApprovalRow",
    "EvidenceRow",
]
