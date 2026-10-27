"""Engine-owned, read-only snapshot of a project's reference_paths
(ProjectConfig) -- the ONE place those paths are ever actually checked
against the host filesystem or read (docs/standards redesign,
replacing a config-parse-time check that: (a) only rejected an entry
equal to or an ancestor of a protected root, never a DESCENDANT of one
-- e.g. `secrets_dir/"production"` passed straight through; (b) only
looked at the entry's own immediate children for a leaked `.env*` file,
never anything nested deeper; (c) validated the path once, at config
load, then mounted whatever that same path resolved to at EVERY later
worker/reviewer spawn -- a symlink swapped in between had no re-check at
all; (d) ran on every project's reference_paths whenever ANY worker's
gate hook re-parsed the whole engine config, so one project's
momentarily-unreachable folder could make is_dir() raise and deny every
tool call for every OTHER project too.

This module fixes all four by: doing the overlap/hidden-component check
here (not in engine.config), copying ONLY a filtered, capped,
symlink-free set of regular files into a fresh engine-owned directory
under config.home (never the source itself), making that copy read-only
once it is in place, and treating that copy as already correct forever
after -- a later call for the same attempt reuses it instead of
re-reading the (by then possibly tampered-with) source at all. Only the
SNAPSHOT is ever mounted into a sandbox or read by a worker/reviewer;
the configured reference_paths entry itself never is.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.config import EngineConfig, ProjectConfig

ENGINE_ROOT = Path(__file__).resolve().parents[2]

# Per-file and total snapshot size caps (bytes). Hit on a single file:
# that one file is skipped (never partially copied). Hit on the running
# total: the whole copy stops right there -- no later file in the same
# source directory is copied either, so a snapshot is always either
# "everything under the caps" or "a documented, recorded partial
# result", never a silent truncation partway through one file.
FILE_CAP_BYTES = 20 * 1024 * 1024
TOTAL_CAP_BYTES = 500 * 1024 * 1024

WARNINGS_FILENAME = "_snapshot_warnings.json"


@dataclass(frozen=True)
class SnapshotEntry:
    """One reference_paths entry's result: `name` is the original
    folder's own basename (for the prompt -- "each original folder name
    alongside"), `source` is the validated, realpath()'d original (never
    itself mounted/read by a worker), `path` is the snapshot directory
    actually mounted read-only, `warnings` records any file this
    snapshot skipped or any point where copying stopped early (never
    silently -- see module docstring)."""

    name: str
    source: Path
    path: Path
    warnings: tuple[str, ...] = ()


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _overlaps(a: Path, b: Path) -> bool:
    """True if `a` and `b` are the same path, or either is an ancestor
    of the other (in either direction)."""
    return a == b or _is_under(a, b) or _is_under(b, a)


def snapshot_dir_name(index: int, original: Path) -> str:
    """Deterministic snapshot subdirectory name for the `index`-th
    reference_paths entry -- shared, pure (no I/O) path math used both
    here (the actual copy target) and by engine.infrastructure.context
    (which only ever needs to know where a snapshot WILL/DOES live, to
    render it into a prompt, never to copy anything itself)."""
    return f"{index}-{original.name or 'root'}"


def validate_reference_path(entry: Path, project_cfg: "ProjectConfig",
                             engine_cfg: "EngineConfig") -> Path:
    """realpath()s `entry` and raises ValueError if it may not be
    snapshotted for `project_cfg`. Two different shapes of forbidden
    root:
      - FULL bidirectional block (entry may not equal, be an ancestor
        of, OR be a descendant of): config.home, secrets_dir, the
        ledger directory, this engine's own source tree, and EVERY
        registered project's own repo (not just this project's) --
        these must never be exposed even partially.
      - RELAXED block (entry may not equal or be an ANCESTOR of, but a
        plain descendant is fine): the real operator HOME and this
        project's own hidden_paths -- legitimate reference material
        commonly lives somewhere under one of these, so only "the
        whole protected tree" is rejected, not every descendant.
    Regardless of either list: an entry with ANY hidden (dot-prefixed)
    path component anywhere in it (not just its own basename) is
    rejected outright -- this is what actually keeps a credentials
    folder under HOME (.ssh, .gnupg, .aws, .config, .claude, .codex,
    .gongbang, .npm, .cache, or any other dotfile) out, since HOME
    itself allows descendants."""
    if not Path(entry).is_absolute():
        raise ValueError(
            f"project {project_cfg.name!r}: reference_paths entry must be absolute: {entry!r}"
        )
    # Hidden components are checked on the path as written too, not only
    # after realpath: a "~/.ssh" that is itself a symlink to a plain-named
    # directory elsewhere must still be refused.
    if any(part.startswith(".") for part in Path(entry).parts):
        raise ValueError(
            f"project {project_cfg.name!r}: reference_path_hidden_component: {entry!r}"
        )
    real = Path(os.path.realpath(entry))

    full_block = [engine_cfg.home, engine_cfg.secrets_dir,
                  engine_cfg.ledger_path.parent, ENGINE_ROOT]
    full_block += [proj.repo for proj in engine_cfg.projects.values()]
    for root in full_block:
        root_real = Path(os.path.realpath(root))
        if _overlaps(real, root_real):
            raise ValueError(
                f"project {project_cfg.name!r}: reference_path_forbidden_overlap: {entry!r}"
            )

    relaxed: list[Path] = []
    real_home = os.environ.get("HOME")
    if real_home:
        relaxed.append(Path(real_home))
    relaxed.extend(project_cfg.hidden_paths)
    for root in relaxed:
        root_real = Path(os.path.realpath(root))
        # entry == root, or entry is an ANCESTOR of root (root under
        # entry) -- a plain descendant of root is fine and is exactly
        # what this relaxed list exists to allow.
        if real == root_real or _is_under(root_real, real):
            raise ValueError(
                f"project {project_cfg.name!r}: reference_path_forbidden_overlap: {entry!r}"
            )

    if any(part.startswith(".") for part in real.parts):
        raise ValueError(
            f"project {project_cfg.name!r}: reference_path_hidden_component: {entry!r}"
        )
    if not real.is_dir():
        raise ValueError(
            f"project {project_cfg.name!r}: reference_paths entry must be a directory: {entry!r}"
        )
    return real


def _copy_file(src: Path, dest: Path, total_so_far: int) -> tuple[bool, int, str | None]:
    """Copies one regular file, opened with O_NOFOLLOW right after an
    lstat check -- the size/type this function acts on is always the
    SAME file descriptor's own fstat, pinned at the moment of copy, so
    a swap between the earlier lstat and this open (or any change after
    this call returns) can never affect what ends up in the snapshot.
    Returns (copied, bytes_copied, warning_or_None); copied=False with
    bytes_copied==0 and a warning means "stop the whole walk here" (the
    total cap was hit) -- the caller must not continue to the next file."""
    try:
        lst = os.lstat(src)
    except OSError:
        return False, 0, None
    if stat.S_ISLNK(lst.st_mode) or not stat.S_ISREG(lst.st_mode):
        return False, 0, None
    if lst.st_size > FILE_CAP_BYTES:
        return False, 0, f"skipped_too_large:{src.name}"
    if total_so_far + lst.st_size > TOTAL_CAP_BYTES:
        return False, 0, f"snapshot_total_cap_reached:{src.name}"
    try:
        fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False, 0, f"skipped_unreadable:{src.name}"
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode):
            return False, 0, f"skipped_not_regular:{src.name}"
        if fst.st_size > FILE_CAP_BYTES:
            return False, 0, f"skipped_too_large:{src.name}"
        if total_so_far + fst.st_size > TOTAL_CAP_BYTES:
            return False, 0, f"snapshot_total_cap_reached:{src.name}"
        # Copy at most the smaller of the per-file cap and what is left of
        # the total cap, plus one byte to detect a file that grew after
        # fstat: the caps hold for the bytes actually written, not the
        # size seen at check time.
        limit = min(FILE_CAP_BYTES, TOTAL_CAP_BYTES - total_so_far)
        written = 0
        with os.fdopen(fd, "rb") as fsrc:
            fd = -1  # ownership transferred to fsrc; do not close twice
            with open(dest, "wb") as fdst:
                while written <= limit:
                    chunk = fsrc.read(min(1_048_576, limit + 1 - written))
                    if not chunk:
                        break
                    fdst.write(chunk)
                    written += len(chunk)
        if written > limit:
            os.unlink(dest)
            return False, 0, f"skipped_grew_past_cap:{src.name}"
        return True, written, None
    finally:
        if fd != -1:
            os.close(fd)


def _hidden(name: str) -> bool:
    return name.startswith(".")  # covers ".env*" too -- any dotfile/dir


def _copy_tree(source: Path, dest: Path) -> tuple[str, ...]:
    """os.walk(followlinks=False) copy of `source` into `dest`: hidden
    (dot-prefixed, including every `.env*`) files/dirs and symlinked
    dirs are pruned from the walk outright (never even descended into
    or recorded as warnings -- they are deliberately excluded, not a
    failure); a symlinked FILE or anything the per-file/total caps
    reject IS recorded (see _copy_file). Stops immediately (returns)
    once the total cap is hit -- no file after that point is copied,
    not even a partial one."""
    warnings: list[str] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        dirnames[:] = [d for d in dirnames
                       if not _hidden(d) and not os.path.islink(Path(dirpath) / d)]
        rel = Path(dirpath).relative_to(source)
        dest_dir = dest / rel
        dest_dir.mkdir(parents=True, exist_ok=True)
        for fname in filenames:
            if _hidden(fname):
                continue
            copied, size, warning = _copy_file(Path(dirpath) / fname, dest_dir / fname, total)
            if warning is not None:
                warnings.append(warning)
            if copied:
                total += size
            elif warning is not None and warning.startswith("snapshot_total_cap_reached"):
                return tuple(warnings)
    return tuple(warnings)


def _make_read_only(root: Path) -> None:
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
        for fname in filenames:
            try:
                os.chmod(Path(dirpath) / fname, 0o444)
            except OSError:
                pass
        os.chmod(dirpath, 0o555)


def snapshot_references(project_cfg: "ProjectConfig", engine_cfg: "EngineConfig",
                         dest: Path) -> tuple[SnapshotEntry, ...]:
    """Validates and (if not already done for this `dest`) copies every
    one of `project_cfg.reference_paths` into its own subdirectory under
    `dest` (see snapshot_dir_name), then makes that subdirectory
    read-only. A subdirectory that ALREADY exists under `dest` is never
    re-validated or re-copied -- the first snapshot for a given
    attempt/project pins the content for its entire lifetime; a source
    changed afterward (even swapped for a symlink to somewhere
    forbidden) has no effect on an existing snapshot."""
    dest.mkdir(parents=True, exist_ok=True)
    entries: list[SnapshotEntry] = []
    for index, original in enumerate(project_cfg.reference_paths):
        target = dest / snapshot_dir_name(index, original)
        if target.exists():
            entries.append(SnapshotEntry(name=original.name, source=original, path=target))
            continue
        validated = validate_reference_path(original, project_cfg, engine_cfg)
        tmp_target = dest / f".tmp-{snapshot_dir_name(index, original)}"
        if tmp_target.exists():
            shutil.rmtree(tmp_target)
        tmp_target.mkdir(parents=True)
        warnings = _copy_tree(validated, tmp_target)
        if warnings:
            (tmp_target / WARNINGS_FILENAME).write_text(
                json.dumps(warnings, ensure_ascii=False, indent=2), encoding="utf-8")
        _make_read_only(tmp_target)
        os.replace(tmp_target, target)
        entries.append(SnapshotEntry(name=original.name, source=validated, path=target,
                                     warnings=tuple(warnings)))
    return tuple(entries)


def ensure_snapshot_for_attempt(engine_cfg: "EngineConfig", project: str,
                                attempt_id: str) -> tuple[SnapshotEntry, ...]:
    """The call site every worker/reviewer sandbox builder uses
    (engine.application.steps.worker_sandbox/review): () immediately,
    with no directory even created, when this project has no
    reference_paths configured (byte-identical to before this feature
    existed) -- otherwise snapshot_references under this attempt's own
    engine.domain.ids.references_root."""
    from engine.domain.ids import references_root

    project_cfg = engine_cfg.projects[project]
    if not project_cfg.reference_paths:
        return ()
    dest = references_root(engine_cfg.home, attempt_id)
    return snapshot_references(project_cfg, engine_cfg, dest)


__all__ = [
    "SnapshotEntry", "snapshot_dir_name", "validate_reference_path",
    "snapshot_references", "ensure_snapshot_for_attempt",
    "FILE_CAP_BYTES", "TOTAL_CAP_BYTES", "WARNINGS_FILENAME", "ENGINE_ROOT",
]
