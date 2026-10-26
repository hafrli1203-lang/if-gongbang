"""Online, verified backups of a ledger sqlite file (engine.infrastructure.db.ledger.backup)."""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

from engine.infrastructure.db.ledger_schema import LedgerError

_BACKUP_GLOB = "ledger-*.sqlite"


def backup(db_path: Path, dest_dir: Path, keep: int = 10) -> Path:
    """Copy the ledger at db_path into dest_dir using sqlite3's online
    backup API (Connection.backup) -- unlike a plain file copy, this
    stays consistent even while another connection is appending events
    through WAL (it copies committed pages page-by-page under the
    source's own lock, never a half-written page). The destination
    filename is a UTC timestamp (with a numeric suffix if two backups
    land in the same second, so neither call ever silently overwrites
    the other's file); callers that want a single well-known name
    (e.g. "latest") should read the returned Path, not reconstruct it.

    The copy is verified before this function returns: it is opened
    (never the source -- a bad PRAGMA against the live ledger would be
    its own hazard) and PRAGMA integrity_check must report exactly
    "ok". A copy that fails this is deleted and LedgerError is raised
    -- a broken file is never left behind pretending to be a usable
    backup.

    After a successful copy, only the newest `keep` files already
    matching the "ledger-*.sqlite" pattern in dest_dir are kept (oldest
    deleted by filename sort, which sorts chronologically because the
    timestamp is zero-padded and first in the name); keep=0 keeps none
    (every backup immediately pruned except nothing to restore from --
    callers that want unlimited history should pass a large keep)."""
    if not db_path.exists():
        raise LedgerError(f"ledger missing: {db_path}")
    if keep < 0:
        raise ValueError(f"keep must be >= 0, got {keep}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = _next_backup_path(dest_dir)
    source = sqlite3.connect(str(db_path))
    try:
        dest = sqlite3.connect(str(dest_path))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    _verify_backup(dest_path)
    _prune_backups(dest_dir, keep)
    return dest_path


_BACKUP_NAME_RE = re.compile(r"^ledger-(\d{8}T\d{6}Z)(?:-(\d+))?\.sqlite$")


def _next_backup_path(dest_dir: Path) -> Path:
    """A zero-padded sequence suffix is always present (even for the
    first backup of a given second, "-000") so filenames sort the same
    way chronologically whether compared as strings or parsed -- a
    bare, non-padded suffix on only the second-and-later collisions in
    the same second (e.g. "-1") would sort BEFORE the padding-free
    first file as a string ('-' < '.' in ASCII), which would make
    pruning delete the newest file instead of the oldest."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    seq = 0
    while True:
        candidate = dest_dir / f"ledger-{stamp}-{seq:03d}.sqlite"
        if not candidate.exists():
            return candidate
        seq += 1


def _verify_backup(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    if row is None or row[0] != "ok":
        path.unlink(missing_ok=True)
        raise LedgerError(f"backup failed integrity_check: {path} ({row})")


def _backup_sort_key(path: Path) -> tuple[str, int]:
    """(timestamp, sequence) parsed from the filename, not the raw
    filename string -- sorting by string alone is exactly the bug
    _next_backup_path's docstring describes; parsing and comparing the
    two parts as (str, int) is correct regardless of future padding
    choices. A legacy name with no sequence suffix at all (written by
    a version of this module before the suffix was always added)
    parses as sequence 0, sorted by its real timestamp like any other
    backup -- it must NOT sort after every timestamped name just
    because it lacks a suffix, or pruning would keep an old,
    suffix-less file over a genuinely newer one. A name this module
    did not itself create at all (glob matched something unexpected)
    sorts last, never pruned ahead of a real one."""
    match = _BACKUP_NAME_RE.match(path.name)
    if match is None:
        return ("￿", 0)
    seq = match.group(2)
    return (match.group(1), int(seq) if seq is not None else 0)


def _prune_backups(dest_dir: Path, keep: int) -> None:
    backups = sorted(dest_dir.glob(_BACKUP_GLOB), key=_backup_sort_key)
    for stale in backups[: max(0, len(backups) - keep)]:
        stale.unlink()


__all__ = ["backup"]
