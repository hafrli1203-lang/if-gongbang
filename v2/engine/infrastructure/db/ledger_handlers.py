"""Derived-table effects for each ledger event kind. Ledger.append()
(engine.infrastructure.db.ledger) looks a kind up in HANDLERS and, if found, calls it
inside the same transaction as the events insert -- an unknown kind is
stored with no derived effect, and a handler that raises rolls back
the whole transaction (including the events insert).

Event kinds with derived effects (documented for implementers):
  project.registered   -> projects row CLOSED
  job.opened           payload {job_id, job_name, attempt_id, n, kind,
                                generation, dirty_paths} -> jobs+attempts,
                                projects.current_attempt_id/generation
                                (recover_job also sets an optional
                                recovered_from: old_attempt_id -- an
                                unknown payload key, ignored by this
                                handler, same as base_head)
  attempt.finished     payload {attempt_id, status} -> attempts.status
                       (status: active | closed | abandoned | interrupted)
  state.changed        payload {state} -> projects.state
  request.submitted    payload {request_id, kind, content_hash, payload}
                       -> requests row (status queued)
  request.status       payload {request_id, status, result} -> requests
  request.step         payload {request_id, step_name, status, payload}
                       -> steps upsert
  approval.granted     payload {approval_id, action, target, artifact_hash,
                                request_id, expires_ts} -> approvals
  approval.used        payload {approval_id} -> approvals.used_seq = seq
  evidence.recorded    payload {kind, fingerprint, artifact_hash, payload}
                       -> evidence row
  evidence.stale       payload {attempt_id, kinds} -> evidence.stale = 1
  proc.started/ended   payload {pid, pgid, station} -> procs
  policy.decided       (no derived effect)
  archive.done, commit.recorded, publish.called, publish.recorded,
  report.written       (no derived effect; read via events())
"""
from __future__ import annotations

import json
import sqlite3
from typing import Callable

from engine.infrastructure.db.ledger_schema import Event


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


HANDLERS: dict[str, Callable[[sqlite3.Connection, int, float, Event], None]] = {
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

__all__ = ["HANDLERS"]
