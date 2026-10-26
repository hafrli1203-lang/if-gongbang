"""trusted -- capture a file's bytes once, at the moment a check reads
it, so a later use of "the same file" can run against those exact
bytes instead of reading the path a second time and trusting nothing
changed in between.

The problem this closes (flow-map §⑤, the one asymmetric row among the
engine's trust boundaries): a "check this file" step and a "now use
its content" step that both independently `path.read_text()`/
`read_bytes()` the same path are only guaranteed to see the same bytes
by TIMING -- there is a real file on disk between the two reads, and
anything with write access to it (a worker process, a crash-then-
resume racing a late writer, a symlinked parent directory swapped out
from under a fixed path string) can change what the second read sees
without the first read ever noticing. The two engine boundaries this
codebase already calls "model" (engine.application.steps.merge's collect_copies/
apply, engine.application.steps.review's compute vs. record) do not have this gap
because they pass a FROZEN PLAN -- a fingerprint recorded once -- to
the later step, which then re-checks the live file against that frozen
value rather than quietly re-trusting a second independent read. This
module is that same pattern, factored out as two small primitives
(capture at check time, verify at use time) instead of being
reimplemented per call site: `t = trusted.capture(path)` right after a
check has decided this content is fine, then `data = trusted.verify(t,
path)` immediately before the content is actually used (written into a
prompt, executed, merged) -- `data` is the one and only value either
caller should ever act on; a bare second `path.read_text()` call
between those two points is exactly the bug this exists to make
unnecessary.

Reading never follows a symlink anywhere along `path` -- not just the
final component. `os.open(path, O_NOFOLLOW)` by itself only refuses a
symlink at the LAST path element; a parent directory swapped for a
symlink (e.g. a worker's `mv real_dir real_dir.old; ln -s /somewhere
real_dir`) would otherwise redirect a fixed path string to an
attacker-chosen file with no code here ever seeing it happen. Each
raw path component (except the leaf, which additionally refuses a
FIFO/device -- see _read_no_symlink) is opened individually via
`dir_fd` instead, so a symlink anywhere in the chain simply fails the
read rather than being silently followed. (Same technique engine.
steps.station._read_gate_log_no_symlink already uses for gate.log,
generalized here rather than reused directly: that function is
one-shot-import-specific -- it also classifies "missing" vs. "oversize"
into ledger events of its own, which is not this module's job.)

Capped at TRUSTED_MAX_BYTES (same 8 MiB figure engine.application.steps.worker_step
already uses for the same reason -- a bounded read of a file no
caller of this module ever expects to be large: plan.json, one scene
script, facts.md, one probe script/log) so a file this large can never
be read unbounded into memory; over the cap is TrustCaptureError, the
same "refuse, do not silently truncate" shape as a missing file or a
symlinked path component.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

TRUSTED_MAX_BYTES = 8 * 1024 * 1024  # 8 MiB


class TrustCaptureError(OSError):
    """Raised by capture()/verify() instead of reading: `path` does not
    exist, some component along it is a symlink, the leaf is not a
    plain regular file, or its size exceeds TRUSTED_MAX_BYTES. Never
    raised for "content changed since capture" -- that is
    TrustViolation, a different condition with a different meaning
    (verify() only raises this one for a path it could not even read
    safely, the same way capture() would have failed on it)."""


class TrustViolation(Exception):
    """Raised by verify() when `path` can be read safely but its
    current bytes no longer match the digest captured earlier. Always
    carries the path (as given to verify(), not necessarily the same
    string object capture() saw) as both `.path` and the exception
    arg, so a caller that catches this broadly still knows which file
    failed without having to thread it through separately."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        super().__init__(self.path)


@dataclass(frozen=True)
class Trusted:
    """One file's bytes, fixed at the moment capture() read them.
    `path` is str(path) as given to capture() -- kept only so a caller
    holding a Trusted can report which file it is without carrying the
    Path separately; verify() always takes its own `path` argument
    rather than trusting this field, since the whole point is that the
    caller, not this dataclass, controls what gets re-checked. `digest`
    is sha256(data).hexdigest() -- comparing it is equivalent to
    comparing `data` byte-for-byte, cheaper when a caller only needs
    the yes/no and not the bytes themselves."""

    path: str
    data: bytes
    digest: str


def _read_no_symlink(path: Path, max_bytes: int) -> bytes:
    """Bytes at `path`, refusing to follow a symlink anywhere along it
    (module docstring) and refusing anything that is not, in the end, a
    plain regular file under `max_bytes`. TrustCaptureError for every
    failure mode (not absolute, missing path, symlinked component,
    non-regular leaf, oversize) -- never a bare OSError a caller might
    mistake for something else, and never a partial/truncated read.

    `path` must be absolute: `path.parts[0]` is then always the root
    ("/"), which the walk below opens first and treats as trusted
    (there is no parent of "/" for a symlink to have been substituted
    for) -- a relative path has no such fixed, un-spoofable anchor to
    start the walk from, so one is rejected outright rather than
    silently walked from the current working directory."""
    if not path.is_absolute():
        raise TrustCaptureError(f"not an absolute path: {path}")
    parts = path.parts  # e.g. ("/", "a", "b", "c.txt")
    try:
        dir_fd = os.open(parts[0], os.O_DIRECTORY)
    except OSError as exc:
        raise TrustCaptureError(f"cannot open {path}: {exc}") from exc
    try:
        for part in parts[1:-1]:
            try:
                next_fd = os.open(part, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            except OSError as exc:
                raise TrustCaptureError(f"cannot open {path}: {exc}") from exc
            os.close(dir_fd)
            dir_fd = next_fd
        try:
            # O_NONBLOCK on open: a FIFO planted at the leaf (not a
            # symlink -- O_NOFOLLOW does not apply to a FIFO) would
            # otherwise block this open indefinitely waiting for a
            # writer. Cleared below, via fcntl, only once fstat has
            # confirmed a plain regular file.
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                        dir_fd=dir_fd)
        except OSError as exc:
            raise TrustCaptureError(f"cannot open {path}: {exc}") from exc
    finally:
        os.close(dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise TrustCaptureError(f"not a regular file: {path}")
        if st.st_size > max_bytes:
            raise TrustCaptureError(
                f"{path} is {st.st_size} bytes, over the {max_bytes} cap")
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
        return os.read(fd, st.st_size)
    finally:
        os.close(fd)


def capture(path: Path, *, max_bytes: int = TRUSTED_MAX_BYTES) -> Trusted:
    """Read `path` once (no symlink anywhere along it, capped at
    max_bytes -- see _read_no_symlink) and fix its bytes/digest into a
    Trusted the caller carries forward. TrustCaptureError on any read
    failure (missing, symlinked, non-regular, oversize) -- a check that
    cannot even read the file it is supposed to be checking has nothing
    to capture."""
    data = _read_no_symlink(path, max_bytes)
    return Trusted(path=str(path), data=data, digest=hashlib.sha256(data).hexdigest())


def verify(t: Trusted, path: Path, *, max_bytes: int = TRUSTED_MAX_BYTES) -> bytes:
    """`t.data` iff `path`'s CURRENT bytes still hash to `t.digest`;
    TrustViolation(path) otherwise. Always re-reads `path` (same
    no-symlink, capped read as capture()) rather than trusting that
    nothing could have changed -- that live re-check, not the digest
    comparison by itself, is the only thing that can ever actually
    detect a change; a caller that already holds a Trusted and skips
    calling this before using the content has gained nothing from
    capturing it in the first place. A path that fails to read at all
    (TrustCaptureError propagates unchanged, as a reader is entitled to
    treat "has it been read-protected/symlink-swapped/replaced with a
    FIFO since capture" the same OSError-shaped way whether capture()
    or verify() is the one hitting it) -- this is distinct from
    TrustViolation: a read failure is "cannot tell", a digest mismatch
    is "told you, and it changed"."""
    current = _read_no_symlink(path, max_bytes)
    if hashlib.sha256(current).hexdigest() != t.digest:
        raise TrustViolation(path)
    return t.data


__all__ = ["Trusted", "TrustCaptureError", "TrustViolation", "capture", "verify",
           "TRUSTED_MAX_BYTES"]
