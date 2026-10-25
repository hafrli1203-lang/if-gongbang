"""Unit tests for engine.trusted: capture/verify a file's bytes once
so a later use can run against exactly what a check already read."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from engine.trusted import (
    TRUSTED_MAX_BYTES, Trusted, TrustCaptureError, TrustViolation, capture, verify,
)


def test_capture_reads_current_bytes_and_digest(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_bytes(b"hello")

    t = capture(path)

    assert t.path == str(path)
    assert t.data == b"hello"
    import hashlib
    assert t.digest == hashlib.sha256(b"hello").hexdigest()


def test_verify_returns_the_captured_bytes_when_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_bytes(b"hello")
    t = capture(path)

    assert verify(t, path) == b"hello"


def test_verify_raises_trust_violation_when_content_changed(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_bytes(b"hello")
    t = capture(path)

    path.write_bytes(b"tampered")

    with pytest.raises(TrustViolation) as exc_info:
        verify(t, path)
    assert exc_info.value.path == str(path)


def test_verify_raises_trust_violation_even_if_bytes_are_the_same_length(
    tmp_path: Path,
) -> None:
    path = tmp_path / "a.txt"
    path.write_bytes(b"AAAAA")
    t = capture(path)

    path.write_bytes(b"BBBBB")

    with pytest.raises(TrustViolation):
        verify(t, path)


def test_capture_missing_file_raises_trust_capture_error(tmp_path: Path) -> None:
    with pytest.raises(TrustCaptureError):
        capture(tmp_path / "does_not_exist.txt")


def test_capture_relative_path_is_refused(tmp_path: Path) -> None:
    rel = Path(os.path.relpath(tmp_path / "a.txt"))
    with pytest.raises(TrustCaptureError):
        capture(rel)


def test_capture_refuses_a_symlinked_leaf(tmp_path: Path) -> None:
    real = tmp_path / "real.txt"
    real.write_bytes(b"secret")
    link = tmp_path / "link.txt"
    link.symlink_to(real)

    with pytest.raises(TrustCaptureError):
        capture(link)


def test_capture_refuses_a_symlinked_parent_directory(tmp_path: Path) -> None:
    """Not just the leaf -- a symlinked directory ANYWHERE along the
    path must also be refused (the exact gap a bare
    os.open(path, O_NOFOLLOW) on only the last component would miss)."""
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    (real_dir / "a.txt").write_bytes(b"secret")
    link_dir = tmp_path / "link_dir"
    link_dir.symlink_to(real_dir)

    with pytest.raises(TrustCaptureError):
        capture(link_dir / "a.txt")


def test_capture_refuses_a_directory(tmp_path: Path) -> None:
    with pytest.raises(TrustCaptureError):
        capture(tmp_path)


def test_capture_refuses_a_fifo(tmp_path: Path) -> None:
    fifo = tmp_path / "f.fifo"
    os.mkfifo(fifo)
    try:
        with pytest.raises(TrustCaptureError):
            capture(fifo)
    finally:
        fifo.unlink()


def test_capture_refuses_over_the_size_cap(tmp_path: Path) -> None:
    path = tmp_path / "big.txt"
    path.write_bytes(b"x" * 10)

    with pytest.raises(TrustCaptureError):
        capture(path, max_bytes=5)


def test_default_max_bytes_matches_the_documented_cap() -> None:
    assert TRUSTED_MAX_BYTES == 8 * 1024 * 1024


def test_verify_itself_refuses_a_path_that_became_unsafe_since_capture(
    tmp_path: Path,
) -> None:
    """verify() re-reads `path` the same guarded way capture() did --
    a path that was a plain file at capture time but has since been
    replaced with a symlink must raise TrustCaptureError (an unreadable
    path, "cannot tell"), never TrustViolation (a readable path whose
    content differs, "told you, and it changed") and never silently
    fall back to the originally captured bytes."""
    path = tmp_path / "a.txt"
    path.write_bytes(b"hello")
    t = capture(path)

    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_bytes(b"hello")
    path.unlink()
    path.symlink_to(elsewhere)

    with pytest.raises(TrustCaptureError):
        verify(t, path)


def test_trusted_is_frozen(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_bytes(b"hello")
    t = capture(path)

    with pytest.raises(Exception):
        t.data = b"other"  # type: ignore[misc]


def test_trusted_dataclass_fields_directly() -> None:
    t = Trusted(path="/x", data=b"abc", digest="deadbeef")
    assert t.path == "/x"
    assert t.data == b"abc"
    assert t.digest == "deadbeef"
