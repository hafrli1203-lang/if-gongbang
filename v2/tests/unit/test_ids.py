"""Unit tests for engine.ids: ulid_like, job/attempt ids, canonical_json,
request_id, content_hash."""
from __future__ import annotations

from engine.ids import (
    _CROCKFORD,
    attempt_dirname,
    attempt_id,
    canonical_json,
    content_hash,
    new_job_id,
    request_id,
    ulid_like,
)


def test_ulid_like_length_and_sortable() -> None:
    first = ulid_like()
    second = ulid_like()
    assert len(first) == 26
    assert len(second) == 26
    assert all(ch in _CROCKFORD for ch in first)
    assert all(ch in _CROCKFORD for ch in second)
    # Time component occupies the first 10 chars; generated moments apart,
    # the later id must not sort strictly before the earlier one.
    assert second[:10] >= first[:10]


def test_job_id_prefix() -> None:
    job_id = new_job_id("alpha")
    assert job_id.startswith("J-alpha-")
    assert len(job_id) == len("J-alpha-") + 26


def test_attempt_id_format() -> None:
    job_id = new_job_id("alpha")
    assert attempt_id(job_id, 1) == f"{job_id}#1"
    assert attempt_id(job_id, 2) == f"{job_id}#2"


def test_attempt_dirname_replaces_hash_with_dash() -> None:
    """A literal '#' breaks tools that resolve config via file URLs
    (Vite/Vitest). A real attempt_id has exactly one '#' (job_id#n);
    the substitution must be unambiguous for it."""
    job_id = new_job_id("search")
    aid = attempt_id(job_id, 1)
    assert aid.count("#") == 1
    dirname = attempt_dirname(aid)
    assert "#" not in dirname
    assert dirname == aid.replace("#", "-")
    assert dirname == f"{job_id}-1"


def test_attempt_dirname_is_injective_across_several_n_values() -> None:
    job_id = new_job_id("search")
    dirnames = {attempt_dirname(attempt_id(job_id, n)) for n in range(1, 6)}
    assert len(dirnames) == 5


def test_request_id_same_for_same_canonical_payload() -> None:
    payload_a = {"b": 1, "a": 2, "nested": {"y": 1, "x": 2}}
    payload_b = {"a": 2, "nested": {"x": 2, "y": 1}, "b": 1}
    assert canonical_json(payload_a) == canonical_json(payload_b)
    rid_a = request_id("alpha", "J-alpha-X#1", "commit", payload_a)
    rid_b = request_id("alpha", "J-alpha-X#1", "commit", payload_b)
    assert rid_a == rid_b
    assert rid_a.startswith("R-")
    assert content_hash(payload_a) == content_hash(payload_b)


def test_request_id_differs_for_different_payload() -> None:
    rid_a = request_id("alpha", "J-alpha-X#1", "commit", {"a": 1})
    rid_b = request_id("alpha", "J-alpha-X#1", "commit", {"a": 2})
    rid_c = request_id("beta", "J-alpha-X#1", "commit", {"a": 1})
    rid_d = request_id("alpha", "J-alpha-X#1", "install", {"a": 1})
    assert rid_a != rid_b
    assert rid_a != rid_c
    assert rid_a != rid_d
