"""Whole-tree immutability for a test/scene run: what keeps the code a
run actually executes equal to the code its fingerprint certifies.

Three pieces, used together by engine.application.acceptance (each
scene's and the full suite's own disposable snapshot) and, for the
read-only root mount only, by engine.application.steps.suite_run (the
bench's run in the real repo):

- `scratch_writable_pairs`: with the run's root bound READ-ONLY
  (suite_run._bench_sandbox_argv's `root_readonly`), the only places
  inside the root a run may still write are the project's declared
  `copy_link_writable_subpaths` (measured: vitest with a
  vitest.config.* writes its bundled config to
  node_modules/.vite-temp/ and fails with EROFS without it; with
  `--no-cache` and no config file it writes nothing under the root).
  Each such path gets a fresh, empty scratch directory OUTSIDE the root
  bound over it, so nothing the run writes there ever lands in the
  root's own host tree. Everything else a run needs to write goes to
  the sandbox's own `--tmpfs /tmp` or its isolated $HOME.
- `lock_tree_dirs` / `unlock_tree_dirs`: strip (restore) the write bits
  of every directory of a disposable snapshot, symlinks never followed.
  Under sandbox="bwrap" the read-only bind already refuses every write;
  this is what makes "create a new file anywhere in the snapshot" fail
  under sandbox="none" too (same-UID: a process that chmods a
  directory back can still write -- the manifest comparison below is
  what catches that).
- `tree_manifest` / `manifest_changes` / `manifest_digest`: a full
  listing of every entry under a root -- tracked, untracked AND
  ignored, `.git` included, symlinks recorded by their own target and
  never followed -- taken right before a run and again right after it.
  Any added, removed or changed entry (a new root-level
  vitest.config.*, a new node_modules symlink, a patched module, a
  chmod) is a mutation; unlike `git status`, nothing untracked or
  ignored is exempt.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

from engine.application.steps.open_job import is_within as _is_within

_WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
_CHUNK = 1 << 20

Manifest = dict[str, tuple[object, ...]]


def new_scratch_root(base: Path) -> Path:
    """A fresh, empty directory under `base` (created if missing) --
    one per sandboxed run, so no run ever sees another's scratch."""
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="scratch-", dir=base))


def scratch_writable_pairs(project_cfg, root: Path,
                           scratch: Path) -> tuple[tuple[Path, Path], ...]:
    """(scratch_source, dest) pairs for sandbox.wrap's `writable_remap`:
    one per configured `copy_link_writable_subpaths` path that is a
    real (non-symlink) directory inside `root`. A destination whose
    parent chain resolves outside `root` (e.g. the entry itself is a
    symlink to an external tree) is skipped -- binding over it would
    open a write into that external tree. A missing destination under a
    real, contained parent is created empty first (bwrap cannot create
    a mountpoint under a read-only bind)."""
    root_real = Path(os.path.realpath(root))
    subpaths_by_entry = getattr(project_cfg, "copy_link_writable_subpaths", {}) or {}
    pairs: list[tuple[Path, Path]] = []
    for entry, subpaths in subpaths_by_entry.items():
        for sub in subpaths:
            dest = root / entry / sub
            if dest.is_symlink():
                continue
            # A nested allowance (e.g. node_modules/.cache/vitest) on a fresh
            # install: create the missing parents too, as long as the
            # nearest existing ancestor is a real directory inside `root`.
            ancestor = dest.parent
            while not ancestor.exists() and ancestor != root:
                ancestor = ancestor.parent
            if ancestor.is_symlink() or not ancestor.is_dir():
                continue
            if not _is_within(Path(os.path.realpath(ancestor)), root_real):
                continue
            if not dest.exists():
                try:
                    dest.mkdir(parents=True)
                except OSError:
                    continue
            parent_real = Path(os.path.realpath(dest.parent))
            if not _is_within(parent_real, root_real):
                continue
            if not dest.is_dir():
                continue
            source = scratch / entry / sub
            source.mkdir(parents=True, exist_ok=True)
            pairs.append((source, dest))
    return tuple(pairs)


def _walk_dirs(root: Path):
    """Every real directory under `root` (root included), top-down,
    never descending through a symlink."""
    stack = [root]
    while stack:
        current = stack.pop()
        yield current
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
        except OSError:
            continue


def lock_tree_dirs(root: Path) -> None:
    """Strip every write bit from every directory under `root`
    (deepest first is unnecessary: chmod on a directory never needs
    write access to its parent)."""
    for directory in _walk_dirs(root):
        mode = stat.S_IMODE(directory.lstat().st_mode)
        directory.chmod(mode & ~_WRITE_BITS)


def unlock_tree_dirs(root: Path) -> None:
    """Give the owner back write access to every directory under
    `root`, so a later shutil.rmtree (suite_snapshot.make_snapshot's own
    stale-dest removal) can delete the tree. A no-op for a missing
    root."""
    if not root.is_dir() or root.is_symlink():
        return
    root.chmod(stat.S_IMODE(root.lstat().st_mode) | stat.S_IWUSR | stat.S_IXUSR)
    for directory in _walk_dirs(root):
        try:
            mode = stat.S_IMODE(directory.lstat().st_mode)
            directory.chmod(mode | stat.S_IWUSR | stat.S_IXUSR)
        except OSError:
            continue


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK), b""):
                digest.update(chunk)
    except OSError as exc:
        return f"unreadable:{exc.errno}"
    return digest.hexdigest()


def _entry_record(path: Path, st: os.stat_result) -> tuple[object, ...]:
    mode = stat.S_IMODE(st.st_mode)
    if stat.S_ISLNK(st.st_mode):
        return ("link", os.readlink(path))
    if stat.S_ISDIR(st.st_mode):
        return ("dir", mode)
    if stat.S_ISREG(st.st_mode):
        return ("file", mode, st.st_size, _file_digest(path))
    return ("other", stat.S_IFMT(st.st_mode), mode)


def tree_manifest(root: Path) -> Manifest:
    """rel posix path -> record for EVERY entry under `root` (root
    itself recorded as "."), symlinks recorded by their literal target
    and never followed. Regular files are content-hashed, so an
    in-place rewrite that keeps size and mode is still a change."""
    manifest: Manifest = {".": _entry_record(root, root.lstat())}
    for directory in _walk_dirs(root):
        try:
            with os.scandir(directory) as entries:
                children = list(entries)
        except OSError as exc:
            rel = directory.relative_to(root).as_posix()
            manifest[f"{rel}/<unlistable>"] = ("error", exc.errno)
            continue
        for entry in children:
            path = Path(entry.path)
            rel = path.relative_to(root).as_posix()
            manifest[rel] = _entry_record(path, entry.stat(follow_symlinks=False))
    return manifest


def manifest_changes(before: Manifest, after: Manifest) -> tuple[str, ...]:
    """Sorted rel paths added, removed or changed between two
    tree_manifest() results."""
    keys = set(before) | set(after)
    return tuple(sorted(k for k in keys if before.get(k) != after.get(k)))


def manifest_digest(manifest: Manifest) -> str:
    """One sha256 over a whole manifest -- what evidence records, so a
    reader can tell two runs saw byte-identical trees without storing
    every entry."""
    encoded = json.dumps(sorted(manifest.items()), ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "Manifest", "lock_tree_dirs", "manifest_changes", "manifest_digest",
    "new_scratch_root", "scratch_writable_pairs", "tree_manifest", "unlock_tree_dirs",
]


_VITE_CONFIG_NAMES = tuple(f"{stem}.config.{ext}" for stem in ("vite", "vitest")
                           for ext in ("js", "mjs", "cjs", "ts", "mts", "cts"))


def vite_writes_beside_config(root: Path) -> bool:
    """True when a root-level vite/vitest config is loaded by Vite < 6,
    which writes its bundled config as `<config>.timestamp-*.mjs` right
    next to the original (Vite 6 moved it to node_modules/.vite-temp).
    Such a run needs a writable root; the whole-tree guard still catches
    anything that is left behind (Vite deletes that file after loading)."""
    if not any((root / name).is_file() for name in _VITE_CONFIG_NAMES):
        return False
    try:
        import json
        version = json.loads((root / "node_modules" / "vite" / "package.json")
                             .read_text(encoding="utf-8")).get("version", "")
        return int(str(version).split(".")[0]) < 6
    except (OSError, ValueError, TypeError):
        return True  # cannot tell: keep the run working
