"""Identifiers: job_id, attempt_id, request_id, canonical JSON."""
from __future__ import annotations

import hashlib
import json
import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid_like() -> str:
    """26-char time-sortable id (10 chars time, 16 chars randomness).

    Standard ULID encoding: 48-bit millisecond timestamp in the high bits,
    80 random bits in the low bits, the combined 128-bit value encoded as
    26 Crockford base32 characters (most significant char first), so two
    ids generated at different milliseconds sort lexically by time.
    """
    ts_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_bits = int.from_bytes(os.urandom(10), "big")  # 80 bits
    value = (ts_ms << 80) | rand_bits
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_job_id(project: str) -> str:
    """Return 'J-{project}-{ulid_like()}'."""
    return f"J-{project}-{ulid_like()}"


def attempt_id(job_id: str, n: int) -> str:
    """Return '{job_id}#{n}'. n starts at 1."""
    return f"{job_id}#{n}"


def attempt_dirname(attempt_id: str) -> str:
    """Filesystem-safe form of an attempt_id for use as (part of) a
    directory name: every '#' is replaced with '-'. A literal '#' in a
    path breaks tools that resolve config via file URLs -- e.g. Vite/
    Vitest refuse to start with "working directory path contains a
    literal '#'". A real attempt_id (ids.attempt_id)
    contains exactly one '#' (job_id's own fixed "J-{project}-{ulid}"
    shape, and a project name, never contain one), so this
    substitution is unambiguous for any id this module itself
    produces; a caller-built id (test fixtures sometimes pass an
    arbitrary string here, never one with a second '#') is passed
    through the same substitution rather than rejected. Ledger ids
    (the attempt_id string itself, as stored/looked up) are never
    changed -- only paths built FROM one for the filesystem use this."""
    return attempt_id.replace("#", "-")


def canonical_json(payload: object) -> str:
    """Deterministic JSON: sorted keys, no whitespace, ensure_ascii=False."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False)


def request_id(project: str, attempt: str, kind: str, payload: object) -> str:
    """'R-' + sha256(project|attempt|kind|canonical_json(payload))[:16]."""
    raw = f"{project}|{attempt}|{kind}|{canonical_json(payload)}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"R-{digest[:16]}"


def content_hash(payload: object) -> str:
    """sha256 hex of canonical_json(payload)."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


__all__ = [
    "ulid_like", "new_job_id", "attempt_id", "attempt_dirname", "canonical_json",
    "request_id", "content_hash",
]
