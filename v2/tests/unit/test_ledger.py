"""Unit tests for engine.ledger: lifecycle, append dispatch, locks."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from engine.ledger import Event, Ledger, LedgerError


def test_open_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(LedgerError):
        Ledger.open(tmp_path / "missing.sqlite")


def test_open_wrong_schema_version_raises(tmp_path: Path) -> None:
    path = tmp_path / "wrong_version.sqlite"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version=999")
    conn.execute("CREATE TABLE placeholder(x INTEGER)")
    conn.commit()
    conn.close()
    with pytest.raises(LedgerError):
        Ledger.open(path)


def test_open_old_version_1_ledger_raises(tmp_path: Path) -> None:
    """N7: the locks table gained a pid column without a SCHEMA_VERSION
    bump, so a ledger created before that change (user_version=1) opens
    fine under the old check and then crashes the first time
    acquire_lock() reads the (now-missing) pid column. SCHEMA_VERSION is
    now 2 with no migration in stage 1: a version-1 ledger must be
    refused at open() instead of crashing later."""
    from engine.ledger import SCHEMA_VERSION

    assert SCHEMA_VERSION == 2
    path = tmp_path / "old_v1.sqlite"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version=1")
    conn.executescript(
        "CREATE TABLE locks(project TEXT PRIMARY KEY, holder TEXT NOT NULL,"
        " acquired_ts REAL NOT NULL);"
    )
    conn.commit()
    conn.close()
    with pytest.raises(LedgerError):
        Ledger.open(path)


def test_open_non_db_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "garbage.sqlite"
    path.write_text("this is not a sqlite database\n" * 10, encoding="utf-8")
    with pytest.raises(LedgerError):
        Ledger.open(path)


def test_create_existing_raises(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    Ledger.create(path).close()
    with pytest.raises(LedgerError):
        Ledger.create(path)


def test_append_updates_projects_row(tmp_path: Path) -> None:
    ledger = Ledger.create(tmp_path / "ledger.sqlite")
    ledger.register_project("alpha")
    assert ledger.project("alpha").state == "CLOSED"
    ledger.append(Event(
        project="alpha", kind="state.changed", actor_kind="owner",
        actor_id="owner1",
        payload={"state": "OPEN", "input": "open_job", "prev": "CLOSED"},
    ))
    assert ledger.project("alpha").state == "OPEN"
    ledger.close()


def test_append_refuses_gate_log_actor_with_other_kind(tmp_path: Path) -> None:
    """Defense in depth: engine.steps.station's gate-log importer is
    the only caller that ever appends with actor_kind "gate_log", and
    it must only ever use the one inert 'gate_log.decision' kind --
    append() itself refuses any OTHER kind under that actor_kind
    outright, even if a future bug in the importer tried to forward a
    log line's own forged kind."""
    ledger = Ledger.create(tmp_path / "ledger.sqlite")
    ledger.register_project("alpha")
    before = [seq for seq, _e in ledger.events("alpha")]
    with pytest.raises(LedgerError):
        ledger.append(Event(
            project="alpha", kind="approval.granted", actor_kind="gate_log",
            actor_id="builder",
            payload={"approval_id": "a1", "action": "publish", "target": "prod",
                      "artifact_hash": "x", "request_id": "r1",
                      "expires_ts": 9999999999.0},
        ))
    after = [seq for seq, _e in ledger.events("alpha")]
    assert after == before  # the refused event was never inserted
    # The one allowed kind for that actor_kind still works normally.
    ledger.append(Event(
        project="alpha", kind="gate_log.decision", actor_kind="gate_log",
        actor_id="builder", payload={"orig_kind": "policy.decided", "allowed": True},
    ))
    ledger.close()


def test_job_opened_inserts_job_and_attempt(tmp_path: Path) -> None:
    ledger = Ledger.create(tmp_path / "ledger.sqlite")
    ledger.register_project("alpha")
    attempt_id = "J-alpha-XXX#1"
    ledger.append(Event(
        project="alpha", kind="job.opened", actor_kind="processor",
        actor_id="proc1", attempt_id=attempt_id,
        payload={
            "job_id": "J-alpha-XXX", "job_name": "fix-thing",
            "attempt_id": attempt_id, "n": 1, "kind": "full",
            "generation": 1, "dirty_paths": ["src/scratch.py"],
        },
    ))
    attempt = ledger.attempt(attempt_id)
    assert attempt.job_id == "J-alpha-XXX"
    assert attempt.status == "active"
    assert attempt.generation == 1
    row = ledger.project("alpha")
    assert row.current_attempt_id == attempt_id
    assert row.generation == 1
    assert row.dirty_paths == ("src/scratch.py",)
    found = ledger.attempts_for_job_name("alpha", "fix-thing")
    assert [a.attempt_id for a in found] == [attempt_id]
    ledger.close()


def test_request_submitted_then_status(tmp_path: Path) -> None:
    ledger = Ledger.create(tmp_path / "ledger.sqlite")
    ledger.register_project("alpha")
    rid = "R-abc123"
    ledger.append(Event(
        project="alpha", kind="request.submitted", actor_kind="owner",
        actor_id="owner1", attempt_id="J-alpha-XXX#1",
        payload={"request_id": rid, "kind": "commit",
                  "content_hash": "deadbeef", "payload": {"msg": "hi"}},
    ))
    req = ledger.request(rid)
    assert req.status == "queued"
    assert req.payload == {"msg": "hi"}
    assert [r.request_id for r in ledger.queued_requests("alpha")] == [rid]

    ledger.append(Event(
        project="alpha", kind="request.status", actor_kind="processor",
        actor_id="proc1", attempt_id="J-alpha-XXX#1",
        payload={"request_id": rid, "status": "done", "result": {"ok": True}},
    ))
    req = ledger.request(rid)
    assert req.status == "done"
    assert req.result == {"ok": True}
    assert ledger.queued_requests("alpha") == []
    ledger.close()


def test_step_upsert(tmp_path: Path) -> None:
    ledger = Ledger.create(tmp_path / "ledger.sqlite")
    ledger.register_project("alpha")
    rid = "R-step1"
    ledger.append(Event(
        project="alpha", kind="request.submitted", actor_kind="owner",
        actor_id="owner1", attempt_id="J-alpha-XXX#1",
        payload={"request_id": rid, "kind": "commit",
                  "content_hash": "h", "payload": {}},
    ))
    ledger.append(Event(
        project="alpha", kind="request.step", actor_kind="processor",
        actor_id="proc1", attempt_id="J-alpha-XXX#1",
        payload={"request_id": rid, "step_name": "compute_fingerprint",
                  "status": "started", "payload": {}},
    ))
    steps = ledger.steps(rid)
    assert steps["compute_fingerprint"].status == "started"

    ledger.append(Event(
        project="alpha", kind="request.step", actor_kind="processor",
        actor_id="proc1", attempt_id="J-alpha-XXX#1",
        payload={"request_id": rid, "step_name": "compute_fingerprint",
                  "status": "done", "payload": {"fingerprint": "abc"}},
    ))
    steps = ledger.steps(rid)
    assert len(steps) == 1
    assert steps["compute_fingerprint"].status == "done"
    assert steps["compute_fingerprint"].payload == {"fingerprint": "abc"}
    ledger.close()


def test_approval_used_sets_seq(tmp_path: Path) -> None:
    ledger = Ledger.create(tmp_path / "ledger.sqlite")
    ledger.register_project("alpha")
    rid = "R-approve1"
    ledger.append(Event(
        project="alpha", kind="approval.granted", actor_kind="owner",
        actor_id="owner1", attempt_id="J-alpha-XXX#1",
        payload={"approval_id": "A-1", "action": "commit", "target": "repo",
                  "artifact_hash": "h1", "request_id": rid,
                  "expires_ts": 9999999999.0},
    ))
    approvals = ledger.approvals_for(rid)
    assert len(approvals) == 1
    assert approvals[0].used_seq is None

    seq = ledger.append(Event(
        project="alpha", kind="approval.used", actor_kind="processor",
        actor_id="proc1", attempt_id="J-alpha-XXX#1",
        payload={"approval_id": "A-1"},
    ))
    approvals = ledger.approvals_for(rid)
    assert approvals[0].used_seq == seq
    ledger.close()


def test_evidence_stale(tmp_path: Path) -> None:
    ledger = Ledger.create(tmp_path / "ledger.sqlite")
    ledger.register_project("alpha")
    attempt_id = "J-alpha-XXX#1"
    ledger.append(Event(
        project="alpha", kind="evidence.recorded", actor_kind="processor",
        actor_id="proc1", attempt_id=attempt_id,
        payload={"kind": "review", "fingerprint": "fp1", "payload": {}},
    ))
    ledger.append(Event(
        project="alpha", kind="evidence.recorded", actor_kind="processor",
        actor_id="proc1", attempt_id=attempt_id,
        payload={"kind": "bench", "fingerprint": "fp1", "payload": {}},
    ))
    assert ledger.evidence(attempt_id, "review")[0].stale is False

    ledger.append(Event(
        project="alpha", kind="evidence.stale", actor_kind="processor",
        actor_id="proc1", attempt_id=attempt_id,
        payload={"attempt_id": attempt_id, "kinds": ["review"]},
    ))
    assert ledger.evidence(attempt_id, "review")[0].stale is True
    assert ledger.evidence(attempt_id, "bench")[0].stale is False
    ledger.close()


def test_second_lock_holder_rejected(tmp_path: Path) -> None:
    ledger = Ledger.create(tmp_path / "ledger.sqlite")
    ledger.register_project("alpha")
    assert ledger.acquire_lock("alpha", "proc-1") is True
    assert ledger.acquire_lock("alpha", "proc-2") is False
    ledger.close()


def test_same_holder_reacquire_ok(tmp_path: Path) -> None:
    ledger = Ledger.create(tmp_path / "ledger.sqlite")
    ledger.register_project("alpha")
    assert ledger.acquire_lock("alpha", "proc-1") is True
    assert ledger.acquire_lock("alpha", "proc-1") is True
    ledger.release_lock("alpha", "proc-1")
    assert ledger.acquire_lock("alpha", "proc-2") is True
    ledger.close()


def test_acquire_lock_identity_is_holder_and_pid_pair(tmp_path: Path) -> None:
    """D-5: two different holders sharing the same (live) pid must not be
    treated as a re-acquire of each other's lock, and one must not be
    able to release the other's row."""
    ledger = Ledger.create(tmp_path / "ledger.sqlite")
    ledger.register_project("alpha")
    my_pid = os.getpid()
    assert ledger.acquire_lock("alpha", "proc-1", pid=my_pid) is True
    # Same pid, different holder -> not a re-acquire; pid is alive -> refused.
    assert ledger.acquire_lock("alpha", "proc-2", pid=my_pid) is False
    # proc-2 must not be able to release proc-1's lock.
    ledger.release_lock("alpha", "proc-2", pid=my_pid)
    assert ledger.lock_holder("alpha") == "proc-1"
    # proc-1 releasing with the matching (holder, pid) pair does work.
    ledger.release_lock("alpha", "proc-1", pid=my_pid)
    assert ledger.lock_holder("alpha") is None
    ledger.close()


def test_append_rolls_back_events_row_when_handler_fails(tmp_path: Path) -> None:
    """A handler failure (here: a duplicate approval_id, which the
    approvals table's PRIMARY KEY rejects) must not leave a half-applied
    event: no events row either, and append() raises LedgerError instead
    of letting the IntegrityError escape."""
    ledger = Ledger.create(tmp_path / "ledger.sqlite")
    ledger.register_project("alpha")
    ledger.append(Event(
        project="alpha", kind="approval.granted", actor_kind="owner",
        actor_id="owner1", attempt_id="J-alpha-XXX#1",
        payload={"approval_id": "A-alpha-1", "action": "commit", "target": "repo",
                  "artifact_hash": "h1", "request_id": "R-dup",
                  "expires_ts": 9999999999.0},
    ))
    events_before = len(ledger.events("alpha"))
    with pytest.raises(LedgerError):
        ledger.append(Event(
            project="alpha", kind="approval.granted", actor_kind="owner",
            actor_id="owner1", attempt_id="J-alpha-XXX#1",
            payload={"approval_id": "A-alpha-1", "action": "commit",
                      "target": "repo", "artifact_hash": "h1",
                      "request_id": "R-dup2", "expires_ts": 9999999999.0},
        ))
    assert len(ledger.events("alpha")) == events_before
    assert len(ledger.approvals_for("R-dup2")) == 0
    ledger.close()


def test_acquire_lock_reclaims_dead_holder(tmp_path: Path) -> None:
    import os

    ledger = Ledger.create(tmp_path / "ledger.sqlite")
    ledger.register_project("alpha")
    assert ledger.acquire_lock("alpha", "proc-1", pid=os.getpid()) is True
    # Another processor with the same (live) pid must be refused: this
    # process is still alive, so the lock is not reclaimable.
    assert ledger.acquire_lock("alpha", "proc-2", pid=99999) is False
    ledger.release_lock("alpha", "proc-1")

    # Simulate a holder whose process was SIGKILLed: dead pid, no release.
    ledger._conn.execute(
        "INSERT INTO locks(project, holder, acquired_ts, pid) VALUES (?,?,?,?)",
        ("alpha", "dead", 0.0, 999999),
    )
    ledger._conn.commit()
    assert ledger.acquire_lock("alpha", "proc-3", pid=os.getpid()) is True
    assert ledger.lock_holder("alpha") == "proc-3"
    ledger.close()


def test_acquire_lock_reclaim_loses_race_on_stale_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5: the old code reclaimed a dead holder's row with an
    unconditional UPDATE ... WHERE project=?, so two processors racing to
    reclaim the same dead lock could both succeed. The UPDATE must be
    conditional on the exact (holder, pid) just observed, and fail
    (rowcount 0) if a racer already changed the row first."""
    import engine.ledger as ledger_mod

    ledger = Ledger.create(tmp_path / "ledger.sqlite")
    ledger.register_project("alpha")
    ledger._conn.execute(
        "INSERT INTO locks(project, holder, acquired_ts, pid) VALUES (?,?,?,?)",
        ("alpha", "dead", 0.0, 999999),
    )
    ledger._conn.commit()
    monkeypatch.setattr(ledger_mod, "_pid_alive", lambda pid: pid != 999999)

    # sqlite3.Connection is a C type: its "execute" slot can't be patched
    # (instance-read-only, class-immutable), so the race is simulated by
    # swapping Ledger._conn for a thin proxy that intercepts just the one
    # UPDATE statement and forwards everything else untouched.
    real_conn = ledger._conn

    class _RacingConn:
        def execute(self, sql, params=()):
            if sql.lstrip().startswith(
                "UPDATE locks SET holder=?, acquired_ts=?, pid=?"
                " WHERE project=? AND holder=? AND pid=?"
            ):
                # A second processor wins the race and reclaims the row
                # first, right before our own conditional UPDATE runs.
                real_conn.execute(
                    "UPDATE locks SET holder=?, acquired_ts=?, pid=? WHERE project=?",
                    ("racer", 123.0, 42, "alpha"),
                )
                real_conn.commit()
            return real_conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(real_conn, name)

    ledger._conn = _RacingConn()

    assert ledger.acquire_lock("alpha", "proc-new", pid=os.getpid()) is False
    assert ledger.lock_holder("alpha") == "racer"
    ledger.close()


def test_force_release_lock_ignores_pid(tmp_path: Path) -> None:
    """#21: a lock acquired by one process's pid can be force-released by
    a caller running under a different pid, once it already knows by
    other means (the holding request is terminal) that it's safe --
    release_lock's pid check would otherwise refuse it."""
    ledger = Ledger.create(tmp_path / "ledger.sqlite")
    ledger.register_project("alpha")
    assert ledger.acquire_lock("install:alpha", "req-1", pid=4242) is True
    # release_lock (pid-checked) from a different pid must refuse.
    ledger.release_lock("install:alpha", "req-1", pid=os.getpid())
    assert ledger.lock_holder("install:alpha") == "req-1"
    # force_release_lock does not check pid at all.
    assert ledger.force_release_lock("install:alpha", "req-1") is True
    assert ledger.lock_holder("install:alpha") is None
    # Nothing to delete the second time.
    assert ledger.force_release_lock("install:alpha", "req-1") is False
    ledger.close()


def test_lock_holder_reports_current_holder_or_none(tmp_path: Path) -> None:
    ledger = Ledger.create(tmp_path / "ledger.sqlite")
    ledger.register_project("alpha")
    assert ledger.lock_holder("install:alpha") is None
    ledger.acquire_lock("install:alpha", "proc-1")
    assert ledger.lock_holder("install:alpha") == "proc-1"
    ledger.release_lock("install:alpha", "proc-1")
    assert ledger.lock_holder("install:alpha") is None
    ledger.close()


def test_attempt_project_resolves_owning_project(tmp_path: Path) -> None:
    ledger = Ledger.create(tmp_path / "ledger.sqlite")
    ledger.register_project("alpha")
    attempt_id = "J-alpha-XXX#1"
    ledger.append(Event(
        project="alpha", kind="job.opened", actor_kind="processor",
        actor_id="proc1", attempt_id=attempt_id,
        payload={"job_id": "J-alpha-XXX", "job_name": "job",
                  "attempt_id": attempt_id, "n": 1, "kind": "full",
                  "generation": 1, "dirty_paths": []},
    ))
    assert ledger.attempt_project(attempt_id) == "alpha"
    with pytest.raises(LedgerError):
        ledger.attempt_project("J-unknown#1")
    ledger.close()
