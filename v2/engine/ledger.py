"""SQLite ledger: append-only events + derived tables. Schema version 1.

Rules
- Every write goes through Ledger.append(); derived tables are updated in
  the same transaction by _apply().
- Ledger.open() never creates a file: missing file or wrong user_version
  raises LedgerError (fail closed). Ledger.create() is the only creator.
- WAL mode. One processor per project holds a row in `locks`.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

SCHEMA_VERSION = 2  # v2: locks.pid (process-liveness lock reclaim). No
# migration logic exists yet -- a v1 ledger (created before this column
# existed) must be refused by open(), not opened and later crash the
# first time acquire_lock() reads a pid column that isn't there.

# The ONLY kind engine.steps.station._import_gate_log is ever allowed to
# append under actor_kind "gate_log" -- see append()'s check below. That
# importer re-files a worker-writable log (engine.gate's gate.log) into
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


class Ledger:
    """Single-writer ledger. Not thread-safe; one instance per process."""

    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self._conn = conn
        self.path = path

    # ---- lifecycle -------------------------------------------------
    @staticmethod
    def create(path: Path) -> "Ledger":
        """Create a new ledger file. Raises LedgerError if it exists."""
        if path.exists():
            raise LedgerError(f"ledger already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA_SQL)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
        return Ledger(conn, path)

    @staticmethod
    def open(path: Path) -> "Ledger":
        """Open an existing ledger. Raises LedgerError if missing, not a
        database, or PRAGMA user_version != SCHEMA_VERSION."""
        if not path.exists():
            raise LedgerError(f"ledger missing: {path}")
        conn = sqlite3.connect(str(path))
        try:
            version_row = conn.execute("PRAGMA user_version").fetchone()
            conn.execute("SELECT count(*) FROM sqlite_master")
        except sqlite3.DatabaseError as exc:
            conn.close()
            raise LedgerError(f"not a sqlite database: {path}") from exc
        version = version_row[0] if version_row else 0
        if version != SCHEMA_VERSION:
            conn.close()
            raise LedgerError(
                f"wrong schema version: expected {SCHEMA_VERSION}, got {version}"
            )
        conn.execute("PRAGMA journal_mode=WAL")
        return Ledger(conn, path)

    def close(self) -> None:
        self._conn.close()

    # ---- writes ----------------------------------------------------
    def append(self, event: Event) -> int:
        """Insert the event and apply its derived-table effect in one
        transaction. Returns the new seq. Unknown event kinds are stored
        with no derived effect. If the derived-table handler raises, the
        whole transaction (including the events insert) is rolled back and
        a LedgerError is raised instead — a half-applied event never
        reaches the next commit. Refuses outright (before the INSERT
        even runs) any event with actor_kind "gate_log" whose kind is
        not GATE_LOG_IMPORT_KIND -- see that constant's own docstring."""
        if event.actor_kind == "gate_log" and event.kind != GATE_LOG_IMPORT_KIND:
            raise LedgerError(
                "actor_kind 'gate_log' may only append kind "
                f"{GATE_LOG_IMPORT_KIND!r}, got {event.kind!r}"
            )
        payload_json = json.dumps(event.payload, ensure_ascii=False)
        ts = time.time()
        try:
            cur = self._conn.execute(
                "INSERT INTO events(ts, project, kind, actor_kind, actor_id,"
                " attempt_id, payload_json) VALUES (?,?,?,?,?,?,?)",
                (ts, event.project, event.kind, event.actor_kind, event.actor_id,
                 event.attempt_id, payload_json),
            )
            seq = int(cur.lastrowid)
            handler = _HANDLERS.get(event.kind)
            if handler is not None:
                handler(self._conn, seq, ts, event)
        except Exception as exc:
            self._conn.rollback()
            raise LedgerError(f"append failed for {event.kind!r}: {exc}") from exc
        self._conn.commit()
        return seq

    def register_project(self, project: str) -> None:
        """Insert projects row in state CLOSED (idempotent)."""
        existing = self._conn.execute(
            "SELECT 1 FROM projects WHERE project=?", (project,)
        ).fetchone()
        if existing is not None:
            return
        self.append(
            Event(project=project, kind="project.registered",
                  actor_kind="owner", actor_id="system")
        )

    def acquire_lock(self, project: str, holder: str, pid: int | None = None) -> bool:
        """Insert into locks; False if another live holder already has it.

        The lock's identity is the pair (holder, pid), not holder alone:
        two different holder strings that happen to share a pid (or vice
        versa) must not be treated as the same owner, so a re-acquire
        (returning True without reclaiming) only happens when both match
        the row exactly.

        Uses INSERT OR IGNORE then reads back the row, instead of
        SELECT-then-INSERT, so two processes racing on an unheld lock never
        both see 'no row' and both INSERT (IntegrityError). If the row
        already belongs to a different holder whose process is no longer
        alive (os.kill(pid, 0) -> ProcessLookupError), the lock is
        reclaimed for the new holder.
        """
        if pid is None:
            pid = os.getpid()
        now = time.time()
        self._conn.execute(
            "INSERT OR IGNORE INTO locks(project, holder, acquired_ts, pid)"
            " VALUES (?,?,?,?)",
            (project, holder, now, pid),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT holder, pid FROM locks WHERE project=?", (project,)
        ).fetchone()
        if row is None:
            return False
        current_holder, current_pid = row
        if current_holder == holder and current_pid == pid:
            return True
        if current_pid is not None and not _pid_alive(int(current_pid)):
            # Conditional reclaim: the UPDATE's WHERE repeats the exact
            # (holder, pid) we just observed. If a second caller raced us
            # and already reclaimed the row (or the original holder came
            # back and re-acquired), that WHERE no longer matches and
            # rowcount is 0 -- we must not blindly overwrite whatever is
            # there now, and we lose the race honestly.
            cur = self._conn.execute(
                "UPDATE locks SET holder=?, acquired_ts=?, pid=?"
                " WHERE project=? AND holder=? AND pid=?",
                (holder, now, pid, project, current_holder, current_pid),
            )
            self._conn.commit()
            return cur.rowcount == 1
        return False

    def release_lock(self, project: str, holder: str, pid: int | None = None) -> None:
        """Delete the lock row only if both holder and pid match -- the
        same identity check as acquire_lock, so one holder can never
        release a different holder's (or a different process's) lock."""
        if pid is None:
            pid = os.getpid()
        self._conn.execute(
            "DELETE FROM locks WHERE project=? AND holder=? AND pid=?",
            (project, holder, pid),
        )
        self._conn.commit()

    def force_release_lock(self, project: str, holder: str) -> bool:
        """Delete the lock row for `holder` regardless of pid. For callers
        that already know by other means (e.g. the holder's request is
        terminal: failed or done) that the lock is safe to drop even
        though it may have been acquired by a different process's pid,
        where release_lock's pid check would otherwise refuse. Returns
        True iff a row was actually deleted."""
        cur = self._conn.execute(
            "DELETE FROM locks WHERE project=? AND holder=?", (project, holder)
        )
        self._conn.commit()
        return cur.rowcount > 0

    # ---- reads -----------------------------------------------------
    def lock_holder(self, project: str) -> str | None:
        """Current holder of the lock row keyed by `project` (same key
        space as acquire_lock/release_lock — callers may use composite
        keys like 'install:{project}'), or None if unlocked."""
        row = self._conn.execute(
            "SELECT holder FROM locks WHERE project=?", (project,)
        ).fetchone()
        return row[0] if row is not None else None

    def project(self, project: str) -> ProjectRow:
        """Raises LedgerError if the project is not registered."""
        row = self._conn.execute(
            "SELECT project, state, current_attempt_id, generation,"
            " last_request_no, dirty_paths_json FROM projects WHERE project=?",
            (project,),
        ).fetchone()
        if row is None:
            raise LedgerError(f"project not registered: {project}")
        return ProjectRow(
            project=row[0], state=row[1], current_attempt_id=row[2],
            generation=row[3], last_request_no=row[4],
            dirty_paths=tuple(json.loads(row[5])),
        )

    def attempt(self, attempt_id: str) -> AttemptRow:
        row = self._conn.execute(
            "SELECT attempt_id, job_id, n, kind, generation, status"
            " FROM attempts WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise LedgerError(f"unknown attempt: {attempt_id}")
        return AttemptRow(*row)

    def attempt_project(self, attempt_id: str) -> str:
        """The project that owns this attempt, via its job. LedgerError if
        the attempt is unknown."""
        row = self._conn.execute(
            "SELECT j.project FROM attempts a JOIN jobs j ON a.job_id = j.job_id"
            " WHERE a.attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise LedgerError(f"unknown attempt: {attempt_id}")
        return str(row[0])

    def attempts_for_job_name(self, project: str, job_name: str) -> list[AttemptRow]:
        rows = self._conn.execute(
            "SELECT a.attempt_id, a.job_id, a.n, a.kind, a.generation, a.status"
            " FROM attempts a JOIN jobs j ON a.job_id = j.job_id"
            " WHERE j.project=? AND j.job_name=? ORDER BY a.n",
            (project, job_name),
        ).fetchall()
        return [AttemptRow(*row) for row in rows]

    def request(self, request_id: str) -> RequestRow:
        row = self._conn.execute(
            "SELECT request_id, project, attempt_id, kind, content_hash,"
            " payload_json, status, result_json, submitted_seq"
            " FROM requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise LedgerError(f"unknown request: {request_id}")
        return _row_to_request(row)

    def queued_requests(self, project: str) -> list[RequestRow]:
        """Requests with status queued or running, oldest submitted first."""
        rows = self._conn.execute(
            "SELECT request_id, project, attempt_id, kind, content_hash,"
            " payload_json, status, result_json, submitted_seq FROM requests"
            " WHERE project=? AND status IN ('queued','running')"
            " ORDER BY submitted_seq",
            (project,),
        ).fetchall()
        return [_row_to_request(row) for row in rows]

    def unknown_requests(self, project: str) -> list[RequestRow]:
        """Requests with status 'unknown' for this project, oldest
        submitted first."""
        rows = self._conn.execute(
            "SELECT request_id, project, attempt_id, kind, content_hash,"
            " payload_json, status, result_json, submitted_seq FROM requests"
            " WHERE project=? AND status='unknown' ORDER BY submitted_seq",
            (project,),
        ).fetchall()
        return [_row_to_request(row) for row in rows]

    def steps(self, request_id: str) -> dict[str, StepRow]:
        rows = self._conn.execute(
            "SELECT request_id, step_name, status, payload_json"
            " FROM steps WHERE request_id=?",
            (request_id,),
        ).fetchall()
        return {
            row[1]: StepRow(row[0], row[1], row[2], json.loads(row[3]))
            for row in rows
        }

    def approvals_for(self, request_id: str) -> list[ApprovalRow]:
        rows = self._conn.execute(
            "SELECT approval_id, project, attempt_id, action, target,"
            " artifact_hash, request_id, expires_ts, used_seq"
            " FROM approvals WHERE request_id=?",
            (request_id,),
        ).fetchall()
        return [ApprovalRow(*row) for row in rows]

    def evidence(self, attempt_id: str, kind: str) -> list[EvidenceRow]:
        """All evidence rows of that kind for the attempt, oldest first."""
        rows = self._conn.execute(
            "SELECT evidence_id, project, attempt_id, kind, fingerprint,"
            " artifact_hash, stale, payload_json, seq FROM evidence"
            " WHERE attempt_id=? AND kind=? ORDER BY seq",
            (attempt_id, kind),
        ).fetchall()
        return [
            EvidenceRow(row[0], row[1], row[2], row[3], row[4], row[5],
                        bool(row[6]), json.loads(row[7]), row[8])
            for row in rows
        ]

    def events(self, project: str, kind: str | None = None) -> list[tuple[int, Event]]:
        if kind is None:
            rows = self._conn.execute(
                "SELECT seq, project, kind, actor_kind, actor_id, attempt_id,"
                " payload_json FROM events WHERE project=? ORDER BY seq",
                (project,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT seq, project, kind, actor_kind, actor_id, attempt_id,"
                " payload_json FROM events WHERE project=? AND kind=?"
                " ORDER BY seq",
                (project, kind),
            ).fetchall()
        return [
            (row[0], Event(project=row[1], kind=row[2], actor_kind=row[3],
                            actor_id=row[4], attempt_id=row[5],
                            payload=json.loads(row[6])))
            for row in rows
        ]

    def live_procs(self, attempt_id: str, station: str | None = None) -> list[tuple[int, int]]:
        """(pid, pgid) rows with ended_ts NULL."""
        if station is None:
            rows = self._conn.execute(
                "SELECT pid, pgid FROM procs WHERE attempt_id=? AND ended_ts IS NULL",
                (attempt_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT pid, pgid FROM procs WHERE attempt_id=? AND station=?"
                " AND ended_ts IS NULL",
                (attempt_id, station),
            ).fetchall()
        return [(row[0], row[1]) for row in rows]


# Event kinds with derived effects (documented for implementers):
#   project.registered   -> projects row CLOSED
#   job.opened           payload {job_id, job_name, attempt_id, n, kind,
#                                 generation, dirty_paths} -> jobs+attempts,
#                                 projects.current_attempt_id/generation
#                                 (recover_job also sets an optional
#                                 recovered_from: old_attempt_id -- an
#                                 unknown payload key, ignored by this
#                                 handler, same as base_head)
#   attempt.finished     payload {attempt_id, status} -> attempts.status
#                        (status: active | closed | abandoned | interrupted)
#   state.changed        payload {state} -> projects.state
#   request.submitted    payload {request_id, kind, content_hash, payload}
#                        -> requests row (status queued)
#   request.status       payload {request_id, status, result} -> requests
#   request.step         payload {request_id, step_name, status, payload}
#                        -> steps upsert
#   approval.granted     payload {approval_id, action, target, artifact_hash,
#                                 request_id, expires_ts} -> approvals
#   approval.used        payload {approval_id} -> approvals.used_seq = seq
#   evidence.recorded    payload {kind, fingerprint, artifact_hash, payload}
#                        -> evidence row
#   evidence.stale       payload {attempt_id, kinds} -> evidence.stale = 1
#   proc.started/ended   payload {pid, pgid, station} -> procs
#   policy.decided       (no derived effect)
#   archive.done, commit.recorded, publish.called, publish.recorded,
#   report.written       (no derived effect; read via events())


def _pid_alive(pid: int) -> bool:
    """True if a process with this pid currently exists (or we lack
    permission to tell, which we treat as alive to be conservative)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _row_to_request(row: tuple[object, ...]) -> RequestRow:
    return RequestRow(
        request_id=row[0], project=row[1], attempt_id=row[2], kind=row[3],
        content_hash=row[4], payload=json.loads(row[5]), status=row[6],
        result=json.loads(row[7]), submitted_seq=row[8],
    )


def _h_project_registered(conn: sqlite3.Connection, seq: int, ts: float, event: Event) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO projects(project, state) VALUES (?, 'CLOSED')",
        (event.project,),
    )


def _h_job_opened(conn: sqlite3.Connection, seq: int, ts: float, event: Event) -> None:
    p = event.payload
    job_id = str(p["job_id"])
    conn.execute(
        "INSERT OR IGNORE INTO jobs(job_id, project, job_name, created_seq)"
        " VALUES (?,?,?,?)",
        (job_id, event.project, p["job_name"], seq),
    )
    conn.execute(
        "INSERT INTO attempts(attempt_id, job_id, n, kind, generation, status)"
        " VALUES (?,?,?,?,?,'active')",
        (p["attempt_id"], job_id, p["n"], p["kind"], p["generation"]),
    )
    conn.execute(
        "UPDATE projects SET current_attempt_id=?, generation=?,"
        " dirty_paths_json=? WHERE project=?",
        (p["attempt_id"], p["generation"],
         json.dumps(p.get("dirty_paths", []), ensure_ascii=False), event.project),
    )


def _h_attempt_finished(conn: sqlite3.Connection, seq: int, ts: float, event: Event) -> None:
    p = event.payload
    conn.execute(
        "UPDATE attempts SET status=? WHERE attempt_id=?",
        (p["status"], p["attempt_id"]),
    )


def _h_state_changed(conn: sqlite3.Connection, seq: int, ts: float, event: Event) -> None:
    conn.execute(
        "UPDATE projects SET state=? WHERE project=?",
        (event.payload["state"], event.project),
    )


def _h_request_submitted(conn: sqlite3.Connection, seq: int, ts: float, event: Event) -> None:
    p = event.payload
    conn.execute(
        "INSERT INTO requests(request_id, project, attempt_id, kind,"
        " content_hash, payload_json, status, submitted_seq)"
        " VALUES (?,?,?,?,?,?,'queued',?)",
        (p["request_id"], event.project, event.attempt_id, p["kind"],
         p["content_hash"], json.dumps(p.get("payload", {}), ensure_ascii=False),
         seq),
    )


def _h_request_status(conn: sqlite3.Connection, seq: int, ts: float, event: Event) -> None:
    p = event.payload
    conn.execute(
        "UPDATE requests SET status=?, result_json=? WHERE request_id=?",
        (p["status"], json.dumps(p.get("result", {}), ensure_ascii=False),
         p["request_id"]),
    )


def _h_request_step(conn: sqlite3.Connection, seq: int, ts: float, event: Event) -> None:
    p = event.payload
    conn.execute(
        "INSERT INTO steps(request_id, step_name, status, payload_json)"
        " VALUES (?,?,?,?)"
        " ON CONFLICT(request_id, step_name) DO UPDATE SET"
        " status=excluded.status, payload_json=excluded.payload_json",
        (p["request_id"], p["step_name"], p["status"],
         json.dumps(p.get("payload", {}), ensure_ascii=False)),
    )


def _h_approval_granted(conn: sqlite3.Connection, seq: int, ts: float, event: Event) -> None:
    p = event.payload
    conn.execute(
        "INSERT INTO approvals(approval_id, project, attempt_id, action,"
        " target, artifact_hash, request_id, expires_ts)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (p["approval_id"], event.project, event.attempt_id, p["action"],
         p["target"], p["artifact_hash"], p["request_id"], p["expires_ts"]),
    )


def _h_approval_used(conn: sqlite3.Connection, seq: int, ts: float, event: Event) -> None:
    conn.execute(
        "UPDATE approvals SET used_seq=? WHERE approval_id=?",
        (seq, event.payload["approval_id"]),
    )


def _h_evidence_recorded(conn: sqlite3.Connection, seq: int, ts: float, event: Event) -> None:
    p = event.payload
    conn.execute(
        "INSERT INTO evidence(project, attempt_id, kind, fingerprint,"
        " artifact_hash, payload_json, seq) VALUES (?,?,?,?,?,?,?)",
        (event.project, event.attempt_id, p["kind"], p["fingerprint"],
         p.get("artifact_hash"), json.dumps(p.get("payload", {}), ensure_ascii=False),
         seq),
    )


def _h_evidence_stale(conn: sqlite3.Connection, seq: int, ts: float, event: Event) -> None:
    p = event.payload
    attempt = p["attempt_id"]
    kinds = p["kinds"]
    conn.executemany(
        "UPDATE evidence SET stale=1 WHERE attempt_id=? AND kind=?",
        [(attempt, kind) for kind in kinds],
    )


def _h_proc_started(conn: sqlite3.Connection, seq: int, ts: float, event: Event) -> None:
    p = event.payload
    conn.execute(
        "INSERT INTO procs(pid, pgid, attempt_id, station, started_ts)"
        " VALUES (?,?,?,?,?)",
        (p["pid"], p["pgid"], event.attempt_id, p["station"], ts),
    )


def _h_proc_ended(conn: sqlite3.Connection, seq: int, ts: float, event: Event) -> None:
    conn.execute(
        "UPDATE procs SET ended_ts=? WHERE pid=?",
        (ts, event.payload["pid"]),
    )


_HANDLERS: dict[str, Callable[[sqlite3.Connection, int, float, Event], None]] = {
    "project.registered": _h_project_registered,
    "job.opened": _h_job_opened,
    "attempt.finished": _h_attempt_finished,
    "state.changed": _h_state_changed,
    "request.submitted": _h_request_submitted,
    "request.status": _h_request_status,
    "request.step": _h_request_step,
    "approval.granted": _h_approval_granted,
    "approval.used": _h_approval_used,
    "evidence.recorded": _h_evidence_recorded,
    "evidence.stale": _h_evidence_stale,
    "proc.started": _h_proc_started,
    "proc.ended": _h_proc_ended,
}
