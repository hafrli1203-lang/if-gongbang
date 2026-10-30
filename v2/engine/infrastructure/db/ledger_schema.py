"""Ledger schema (SQLite DDL) and its version. The row dataclasses and
fixed constants it stores live in engine.domain.records and are
re-exported here, so `from engine.infrastructure.db.ledger import X` keeps working.
engine.infrastructure.db.ledger's Ledger class reads/writes against
this schema; ledger_handlers applies each event kind's derived-table
effect; ledger_backup copies/prunes backup files of the same sqlite
file.
"""
from __future__ import annotations

from engine.domain.records import (
    EVIDENCE_KINDS, GATE_LOG_IMPORT_KIND, REQUEST_STATUSES, STATES, STEP_STATUSES, ApprovalRow,
    AttemptRow, Event, EvidenceRow, LedgerError, ProjectRow, RequestRow, StepRow,
)

SCHEMA_VERSION = 2  # v2: locks.pid (process-liveness lock reclaim). No
# migration logic exists yet -- a v1 ledger (created before this column
# existed) must be refused by open(), not opened and later crash the
# first time acquire_lock() reads a pid column that isn't there.
#
# office_approval_nonces is
# deliberately NOT a SCHEMA_VERSION bump: Ledger.create()/open() both
# run `CREATE TABLE IF NOT EXISTS` for it (see Ledger's own docstring
# for why) instead of adding it to SCHEMA_SQL below -- a purely
# additive retrofit onto any EXISTING v2 ledger file, so updating this
# engine never breaks an installation that predates the office-
# approval feature. A real schema_version bump (with a real migration
# path) is still the right tool for a column/shape CHANGE to an
# existing table; this is additive-only and does not need one.

SCHEMA_SQL = """
CREATE TABLE events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  project TEXT NOT NULL,
  kind TEXT NOT NULL,
  actor_kind TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  attempt_id TEXT,
  payload_json TEXT NOT NULL
);
CREATE TABLE projects(
  project TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  current_attempt_id TEXT,
  generation INTEGER NOT NULL DEFAULT 0,
  last_request_no INTEGER NOT NULL DEFAULT 0,
  dirty_paths_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE jobs(
  job_id TEXT PRIMARY KEY,
  project TEXT NOT NULL,
  job_name TEXT NOT NULL,
  created_seq INTEGER NOT NULL
);
CREATE TABLE attempts(
  attempt_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  n INTEGER NOT NULL,
  kind TEXT NOT NULL,
  generation INTEGER NOT NULL,
  status TEXT NOT NULL
);
CREATE TABLE requests(
  request_id TEXT PRIMARY KEY,
  project TEXT NOT NULL,
  attempt_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL,
  result_json TEXT NOT NULL DEFAULT '{}',
  submitted_seq INTEGER NOT NULL
);
CREATE TABLE steps(
  request_id TEXT NOT NULL,
  step_name TEXT NOT NULL,
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(request_id, step_name)
);
CREATE TABLE approvals(
  approval_id TEXT PRIMARY KEY,
  project TEXT NOT NULL,
  attempt_id TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT NOT NULL,
  artifact_hash TEXT NOT NULL,
  request_id TEXT NOT NULL,
  expires_ts REAL NOT NULL,
  used_seq INTEGER
);
CREATE TABLE evidence(
  evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL,
  attempt_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  artifact_hash TEXT,
  stale INTEGER NOT NULL DEFAULT 0,
  payload_json TEXT NOT NULL DEFAULT '{}',
  seq INTEGER NOT NULL
);
CREATE TABLE procs(
  pid INTEGER PRIMARY KEY,
  pgid INTEGER NOT NULL,
  attempt_id TEXT NOT NULL,
  station TEXT NOT NULL,
  started_ts REAL NOT NULL,
  ended_ts REAL
);
CREATE TABLE locks(
  project TEXT PRIMARY KEY,
  holder TEXT NOT NULL,
  acquired_ts REAL NOT NULL,
  pid INTEGER
);
"""

# office_approval_nonces: added via CREATE TABLE IF NOT EXISTS (Ledger.
# create()/open(), never SCHEMA_SQL above) -- see SCHEMA_VERSION's own
# comment for why this table is deliberately additive-only.
OFFICE_APPROVAL_NONCES_DDL = """
CREATE TABLE IF NOT EXISTS office_approval_nonces(
  project TEXT NOT NULL,
  nonce TEXT NOT NULL,
  consumed_ts REAL NOT NULL,
  PRIMARY KEY(project, nonce)
);
"""

__all__ = [
    "SCHEMA_VERSION", "GATE_LOG_IMPORT_KIND", "STATES", "REQUEST_STATUSES",
    "STEP_STATUSES", "EVIDENCE_KINDS", "LedgerError", "Event", "ProjectRow",
    "AttemptRow", "RequestRow", "StepRow", "ApprovalRow", "EvidenceRow", "SCHEMA_SQL",
    "OFFICE_APPROVAL_NONCES_DDL",
]
