#!/usr/bin/env python3
"""Local, file-based storage for the lesson log (L1 experiences -> promoted standards).

One append-only-in-spirit JSONL file per app/project under cfg["lessons_dir"] (one file per
app, name derived from the app string - see _safe_name()). Each line is one lesson record:
  {"number": "L1-0001", "app": "...", "scene": "...", "why": "...", "rule": "...",
   "scope": "...", "exceptions": "...", "owner_quote": "...", "status": "candidate",
   "sessions_seen": ["session-id", ...]}

This intentionally has no network/DB dependency - a Postgres-backed version is an optional
upgrade an installer can wire in later (see docs/concept.md "작업 일지"), described there as
optional rather than required.

Every mutation (add_lesson/mark_seen/set_status) runs its whole read-renumber-write cycle
inside an exclusive lock on a dedicated `<file>.lock` file (fcntl.flock - POSIX only; see
_exclusive() for the no-op fallback on platforms without fcntl), so concurrent writes cannot
lose a lesson. The write itself goes through a temp-file-plus-os.replace() so a crash or
exception mid-write can never leave a half-written file in place - the previous content
survives untouched, and the exception propagates to the caller (a hook should treat that as
"could not record the lesson this time", not silently lose data).
"""
import contextlib
import json
import os
import tempfile

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX-only; fails open (no locking) elsewhere
    fcntl = None

STATUSES = ("candidate", "promoted", "rejected", "retired")


def _safe_name(app: str) -> str:
    """Collision-free filename-safe encoding of an app name: alnum ASCII characters and "-"
    pass through unchanged, a literal "_" is doubled, and everything else becomes
    "_XXXX_" (its codepoint in hex) - so "demo app" ("_0020_" for the space) and "demo_app"
    ("__" for the underscore) can never collide on the same file."""
    out = []
    for ch in app:
        if ch.isascii() and (ch.isalnum() or ch == "-"):
            out.append(ch)
        elif ch == "_":
            out.append("__")
        else:
            out.append("_%04x_" % ord(ch))
    name = "".join(out) or "workspace"
    return name[:200]


def _path(app: str, cfg: dict) -> str:
    return os.path.join(cfg["lessons_dir"], f"{_safe_name(app)}.jsonl")


@contextlib.contextmanager
def _exclusive(path: str):
    """Holds an exclusive lock on `<path>.lock` for the duration of the `with` block, so two
    concurrent callers (same process or different) never interleave a read-modify-write cycle
    on the same app's lesson file. A separate lock file (never replaced) is used rather than
    locking the data file itself, so the lock survives the data file being atomically replaced
    underneath it (see _write_all). No-op (best effort, not a guarantee) on a platform without
    fcntl."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if fcntl is None:
        yield
        return
    lock_path = path + ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_all(app: str, cfg: dict) -> list[dict]:
    path = _path(app, cfg)
    if not os.path.isfile(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _write_all(app: str, cfg: dict, rows: list[dict]) -> None:
    """Writes the whole file atomically: a temp file in the same directory, fsync'd, then
    os.replace()'d onto the real path. A reader never observes a partially-written file, and
    if anything raises before the replace, the original file is untouched."""
    path = _path(app, cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-lessons-", suffix=".jsonl")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def next_number(app: str, cfg: dict) -> str:
    with _exclusive(_path(app, cfg)):
        rows = _read_all(app, cfg)
    return f"L1-{len(rows) + 1:04d}"


def add_lesson(app: str, cfg: dict, *, scene: str, why: str, rule: str, owner_quote: str,
               scope: str = "", exceptions: str = "", session: str = "") -> dict:
    path = _path(app, cfg)
    with _exclusive(path):
        rows = _read_all(app, cfg)
        record = {
            "number": f"L1-{len(rows) + 1:04d}",
            "app": app,
            "scene": scene,
            "why": why,
            "rule": rule,
            "scope": scope,
            "exceptions": exceptions,
            "owner_quote": owner_quote,
            "status": "candidate",
            "sessions_seen": [session] if session else [],
        }
        rows.append(record)
        _write_all(app, cfg, rows)
    return record


def mark_seen(app: str, cfg: dict, number: str, session: str) -> bool:
    path = _path(app, cfg)
    with _exclusive(path):
        rows = _read_all(app, cfg)
        changed = False
        for r in rows:
            if r.get("number") == number:
                seen = r.setdefault("sessions_seen", [])
                if session and session not in seen:
                    seen.append(session)
                    changed = True
        if changed:
            _write_all(app, cfg, rows)
    return changed


def set_status(app: str, cfg: dict, number: str, status: str) -> bool:
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status}")
    path = _path(app, cfg)
    with _exclusive(path):
        rows = _read_all(app, cfg)
        changed = False
        for r in rows:
            if r.get("number") == number:
                r["status"] = status
                changed = True
        if changed:
            _write_all(app, cfg, rows)
    return changed


def list_lessons(app: str, cfg: dict) -> list[dict]:
    return _read_all(app, cfg)


def promotion_candidates(app: str, cfg: dict, min_sessions: int = 3) -> list[dict]:
    """Lessons seen from min_sessions or more distinct sessions and not yet decided on."""
    out = []
    for r in _read_all(app, cfg):
        if r.get("status") in ("candidate",) and len(set(r.get("sessions_seen", []))) >= min_sessions:
            out.append(r)
    return out
