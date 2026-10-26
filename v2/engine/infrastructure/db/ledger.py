"""SQLite ledger: append-only events + derived tables. Schema version 2.

Rules
- Every write goes through Ledger.append(); derived tables are updated in
  the same transaction by the engine.infrastructure.db.ledger_handlers table.
- Ledger.open() never creates a file: missing file or wrong user_version
  raises LedgerError (fail closed). Ledger.create() is the only creator.
- WAL mode. One processor per project holds a row in `locks`.

The schema (DDL + row dataclasses, engine.infrastructure.db.ledger_schema), the derived-
table effects for each event kind (engine.infrastructure.db.ledger_handlers) and online
backups of the sqlite file (engine.infrastructure.db.ledger_backup) each live in their
own module; this module re-exports all of them so `from engine.infrastructure.db.ledger
import X` keeps working unchanged.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from engine.infrastructure.db.ledger_backup import backup
from engine.infrastructure.db.ledger_handlers import HANDLERS
from engine.infrastructure.db.ledger_schema import (
    EVIDENCE_KINDS, GATE_LOG_IMPORT_KIND, REQUEST_STATUSES, SCHEMA_SQL, SCHEMA_VERSION,
    STATES, STEP_STATUSES, ApprovalRow, AttemptRow, Event, EvidenceRow, LedgerError,
    ProjectRow, RequestRow, StepRow,
)


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
            handler = HANDLERS.get(event.kind)
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


__all__ = [
    "SCHEMA_VERSION", "GATE_LOG_IMPORT_KIND", "STATES", "REQUEST_STATUSES",
    "STEP_STATUSES", "EVIDENCE_KINDS", "LedgerError", "Event", "ProjectRow",
    "AttemptRow", "RequestRow", "StepRow", "ApprovalRow", "EvidenceRow",
    "Ledger", "backup",
]
