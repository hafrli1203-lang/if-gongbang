"""Unit tests for engine.ledger.backup (P6, docs/flow-map.md ⑧B/E):
SQLite online backup while another connection is writing, restore, and
the keep-N pruning."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from engine.ledger import Event, Ledger, LedgerError, backup


def _make_ledger(path: Path) -> Ledger:
    ledger = Ledger.create(path)
    ledger.register_project("alpha")
    return ledger


def test_backup_creates_a_verified_copy(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    ledger = _make_ledger(db_path)
    ledger.append(Event(project="alpha", kind="test.ping", actor_kind="owner",
                        actor_id="owner"))
    ledger.close()

    dest_dir = tmp_path / "backups"
    out = backup(db_path, dest_dir, keep=10)

    assert out.parent == dest_dir
    assert out.exists()
    # restore: the copy opens as a normal ledger and has the event.
    restored = Ledger.open(out)
    try:
        events = list(restored.events("alpha", "test.ping"))
        assert len(events) == 1
    finally:
        restored.close()


def test_backup_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(LedgerError):
        backup(tmp_path / "missing.sqlite", tmp_path / "backups")


def test_backup_while_another_connection_is_writing(tmp_path: Path) -> None:
    """The online backup API (sqlite3.Connection.backup) must stay
    consistent even while a second connection keeps appending events
    through WAL -- not a plain file copy, which could grab a
    half-written page. The backup call itself must not raise, and the
    resulting copy must still pass PRAGMA integrity_check (verified
    inside backup() itself, so a non-raising call already proves it)."""
    db_path = tmp_path / "ledger.sqlite"
    _make_ledger(db_path).close()

    stop = threading.Event()
    writer_error: list[BaseException] = []

    def write_loop() -> None:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            n = 0
            while not stop.is_set() and n < 500:
                conn.execute(
                    "INSERT INTO events(ts, project, kind, actor_kind, actor_id,"
                    " attempt_id, payload_json) VALUES (?,?,?,?,?,?,?)",
                    (time.time(), "alpha", "test.ping", "owner", "owner", None, "{}"),
                )
                conn.commit()
                n += 1
        except BaseException as exc:  # noqa: BLE001 - surfaced via writer_error
            writer_error.append(exc)
        finally:
            conn.close()

    writer = threading.Thread(target=write_loop)
    writer.start()
    try:
        dest_dir = tmp_path / "backups"
        out = backup(db_path, dest_dir, keep=10)
    finally:
        stop.set()
        writer.join(timeout=10)

    assert not writer_error
    assert out.exists()
    restored = Ledger.open(out)
    restored.close()


def test_backup_keeps_only_the_newest_n(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    _make_ledger(db_path).close()
    dest_dir = tmp_path / "backups"

    made: list[Path] = []
    for _ in range(5):
        made.append(backup(db_path, dest_dir, keep=3))
        time.sleep(1.01)  # distinct second -> distinct filename, oldest-first order

    remaining = sorted(dest_dir.glob("ledger-*.sqlite"))
    assert len(remaining) == 3
    # the three kept are the three most RECENTLY made, in order
    assert remaining == sorted(made[-3:])
    for gone in made[:-3]:
        assert not gone.exists()


def test_backup_prunes_a_legacy_suffixless_name_by_its_real_age(tmp_path: Path) -> None:
    """A legacy backup file with no sequence suffix at all
    (ledger-<ts>.sqlite, from before this module always appended one)
    must still be pruned by its actual, older timestamp -- not sorted
    after every properly-suffixed name just because it lacks one,
    which would make keep=1 keep the stale legacy file and delete the
    backup that was just created."""
    db_path = tmp_path / "ledger.sqlite"
    _make_ledger(db_path).close()
    dest_dir = tmp_path / "backups"
    dest_dir.mkdir()
    legacy = dest_dir / "ledger-20200101T000000Z.sqlite"
    legacy.write_bytes(b"not a real sqlite file, only the name matters here")

    fresh = backup(db_path, dest_dir, keep=1)

    assert fresh.exists(), "the just-created backup must survive pruning"
    assert not legacy.exists(), "the OLDER legacy-named backup must be the one pruned"
    assert list(dest_dir.glob("ledger-*.sqlite")) == [fresh]


def test_backup_same_second_collision_keeps_the_newest_with_keep_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two backups whose filename timestamp collides (same wall-clock
    second) must still prune oldest-first by actual creation order --
    not by a plain string sort of the filename, which can disagree
    with creation order depending on how the collision suffix is
    formatted: a non-zero-padded suffix on only the later collision
    (e.g. a bare "-1") would sort BEFORE the padding-free first file's
    bare ".sqlite" name as a string, so keep=1 would delete the
    just-created newer file and keep the older one, leaving backup()'s
    own returned Path pointing at nothing."""
    db_path = tmp_path / "ledger.sqlite"
    _make_ledger(db_path).close()
    dest_dir = tmp_path / "backups"

    # Force both calls into the identical "second" without needing to
    # race the real wall clock.
    monkeypatch.setattr("engine.ledger.time.strftime",
                        lambda *args, **kwargs: "20260101T000000Z")

    first = backup(db_path, dest_dir, keep=1)
    second = backup(db_path, dest_dir, keep=1)

    assert first != second
    assert not first.exists(), "the OLDER same-second backup must be the one pruned"
    assert second.exists(), "the NEWER same-second backup must survive"
    assert list(dest_dir.glob("ledger-*.sqlite")) == [second]


def test_backup_keep_zero_prunes_everything(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    _make_ledger(db_path).close()
    dest_dir = tmp_path / "backups"
    out = backup(db_path, dest_dir, keep=0)
    assert not out.exists()
    assert list(dest_dir.glob("ledger-*.sqlite")) == []


def test_backup_rejects_negative_keep(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.sqlite"
    _make_ledger(db_path).close()
    with pytest.raises(ValueError):
        backup(db_path, tmp_path / "backups", keep=-1)
