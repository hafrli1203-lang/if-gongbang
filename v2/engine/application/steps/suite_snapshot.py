"""Disposable, byte-for-byte snapshots of a certified tree fingerprint,
and the immutability guards (chmod for a throwaway snapshot, read-only
bind-mount lists for the real, ongoing repo) that keep a scene/test
from quietly rewriting what it is supposed to only be exercising.
Shared by engine.application.steps.suite_run (the real test suite + trust-checked
cases) and engine.application.acceptance (scene/full-suite disposable snapshots).
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

from engine.infrastructure import evidence
from engine.domain import ids
from engine.config import EngineConfig
from engine.infrastructure.gitutil import git_argv
from engine.application.steps.open_job import is_within as _is_within
from engine.application.steps.open_job import link_entry_children
from engine.application.steps.suite_tree_guard import unlock_tree_dirs

_SNAPSHOT_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "engine", "GIT_AUTHOR_EMAIL": "engine@local",
    "GIT_COMMITTER_NAME": "engine", "GIT_COMMITTER_EMAIL": "engine@local",
}


class SnapshotIntegrityError(Exception):
    """Raised by make_snapshot if the snapshot it just built is not
    byte-for-byte what `fp` certifies: a fresh `git
    archive <fp>` extraction followed by `init`/`add -A`/`commit`
    should ALWAYS leave `git status --porcelain` empty (there is
    nothing else in `dest` yet for `add -A` to have missed) -- a non-
    empty status right after construction, before anything has even
    run against it, means something OUTSIDE the certified fingerprint
    landed in the snapshot (a bug in this very function, a stale
    leftover link_snapshot_deps wrote into `dest` BEFORE this ran
    instead of after, ...). Caught nowhere in this module, deliberately:
    a failure here is a construction bug, not a scene/test result, and
    must surface as a hard, uncaught failure the same way a `git`
    subprocess failure already would (this function's other calls are
    all check=True)."""


def snapshot_dir(config: EngineConfig, attempt_id: str, label: str) -> Path:
    """Scratch location for ONE disposable snapshot -- under
    config.home, never under any project's repo, so it is never itself
    part of anything evidence.fingerprint covers. `label` picks out
    which of the several independent snapshots a single caller builds
    this is ("scene-<i>", "full_suite", "plan" -- engine.application.acceptance;
    "bench" -- engine.application.steps.suite_run's own run_tests/trust_checks).
    Each label gets its OWN directory so one stage's run can never see
    another's leftover untracked state."""
    return config.home / "acceptance" / ids.attempt_dirname(attempt_id) / label


def make_snapshot(repo: Path, dest: Path, fp: str) -> str:
    """Materialize exactly the content `fp` (evidence.fingerprint's tree
    hash for `repo`, taken by the caller BEFORE anything runs) into a
    fresh, self-contained copy at `dest` -- evidence.export_tree (ls-
    tree + cat-file blob, NEVER `git archive`: see that function's own
    docstring for why `git archive`'s export-ignore/export-subst
    .gitattributes handling can silently omit or rewrite a certified
    file), then `git init`/
    `add -A`/`commit` with a fixed identity (same pattern as engine.
    steps.open_job._make_copy's worker copy), so `dest` is a normal git
    worktree a test runner can `git status` against. Removes any stale
    `dest` from a previous run of the same attempt first.

    Before returning, verifies TWO things, either raising
    SnapshotIntegrityError (see that class's own docstring):
    (a) `dest`'s own `git status --porcelain` is empty -- "before
        anything runs, fail if the snapshot contains files outside the
        certified fingerprint";
    (b) `dest`'s own committed tree hash (HEAD^{tree}) equals `fp`
        exactly -- export_tree already writes every blob byte-for-byte,
        but THIS commit's own `git add -A` still runs against the
        fresh `dest` repo's OWN .gitattributes (if the exported tree
        happens to include one, e.g. a text=auto/eol filter), which
        could re-normalize content differently than however `fp`'s own
        tree was originally built -- (a) alone cannot catch this (a
        re-normalized file is still fully committed, nothing left
        dirty), only comparing the resulting tree hash to the
        certified `fp` can. Both checks run here, at the one place
        every snapshot in this codebase is built, rather than
        separately by each caller.

    Returns that verified HEAD^{tree} (always equal to `fp`), so a
    caller can record in evidence exactly which tree the run it is
    about to start executes. A stale `dest` whose directories a
    previous run locked (suite_tree_guard.lock_tree_dirs) is unlocked
    first, so its removal cannot fail on a read-only directory."""
    if dest.exists():
        unlock_tree_dirs(dest)
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    evidence.export_tree(repo, fp, dest)
    subprocess.run(git_argv(dest, "init", "-q"), check=True, capture_output=True)
    env = {**os.environ, **_SNAPSHOT_GIT_IDENTITY, "GIT_CONFIG_GLOBAL": "/dev/null"}
    # -f: `dest` holds exactly the certified tree export_tree just wrote,
    # nothing else, so the app's own .gitignore (e.g. "*.log" matching a
    # tracked .gongbang/<job>/probe/t1.log) must not drop any of it.
    subprocess.run(git_argv(dest, "add", "-A", "-f"), check=True,
                    capture_output=True, env=env)
    subprocess.run(git_argv(dest, "commit", "-q", "-m", "engine-snapshot"),
                    check=True, capture_output=True, env=env)
    status = subprocess.run(
        git_argv(dest, "status", "--porcelain", "-z", "--untracked-files=all"),
        check=True, capture_output=True,
    ).stdout
    if status:
        raise SnapshotIntegrityError(
            f"{dest} has content outside fingerprint {fp} right after construction"
        )
    head_tree = subprocess.run(
        git_argv(dest, "rev-parse", "HEAD^{tree}"), check=True, capture_output=True, text=True,
    ).stdout.strip()
    if head_tree != fp:
        raise SnapshotIntegrityError(
            f"{dest}'s own committed tree {head_tree} does not match certified fingerprint {fp}"
        )
    return head_tree


def tracked_file_paths(snapshot: Path) -> tuple[Path, ...]:
    """Absolute paths, under `snapshot`, of every file HEAD tracks in
    THIS disposable snapshot's own commit (make_snapshot) -- the exact
    set a scene/test must never be able to modify in place (see
    lock_tracked_files and callers' extra_readonly use of this same
    list). Never includes anything link_snapshot_deps adds afterwards
    (copy_links symlinks, e.g. node_modules): those are untracked by
    definition and legitimately need their destination to stay
    writable (new child entries) for the test tooling that walks
    them."""
    out = subprocess.run(
        git_argv(snapshot, "ls-tree", "-r", "-z", "--name-only", "HEAD"),
        check=True, capture_output=True,
    ).stdout
    names = out.decode("utf-8", "surrogateescape").split("\0")
    return tuple(snapshot / name for name in names if name)


def tracked_directories(snapshot: Path) -> tuple[Path, ...]:
    """Absolute paths, under `snapshot`, of every directory that
    contains at least one HEAD-tracked file -- EXCLUDING `snapshot`
    itself, which a caller that needs it immutable binds read-only as
    a whole (suite_run._bench_sandbox_argv's `root_readonly`; the
    acceptance snapshots and the bench both do). Each of these blocks
    a scene/test planting a brand-new file INSIDE an existing tracked
    package directory, e.g. a sibling module next to a real one that a
    loose `import` could resolve first. lock_tracked_files' per-file chmod
    cannot stop this at all (it only strips write bits from files that
    already exist); binding each of THESE directories read-only in the
    bwrap sandbox (callers' extra_readonly) does, since a read-only-
    bound directory refuses creating OR deleting any entry in it, not
    merely editing an existing one's content. sandbox="none" has no
    bind mounts at all, so this protection does not apply there -- a
    documented, not-chased limitation, same as the rest of this
    codebase's sandbox="none" caveats."""
    dirs = {p.parent for p in tracked_file_paths(snapshot)}
    dirs.discard(snapshot)
    return tuple(sorted(dirs))


def lock_tracked_files(tracked: tuple[Path, ...]) -> None:
    """Strip every write bit from each tracked file's CURRENT mode --
    the plain-permissions half of closing "a scene/test patches/
    restores implementation, or supplies an untracked substitute": a
    write on a certified file now raises PermissionError regardless of
    sandbox mode, including mode="none" (engine.infrastructure.sandbox.wrap is a no-op
    there, so the per-file read-only BIND a caller also passes never
    runs at all -- this chmod is the only guard that mode has).
    Directories are left untouched, and so is anything that is not,
    right now, a plain tracked file (a symlink git tracked, or a path a
    prior stage already turned into something else) -- only pre-
    existing tracked FILES lose their write bit. Creating NEW entries is
    blocked separately: by the read-only root bind under bwrap, and by
    suite_tree_guard.lock_tree_dirs' directory chmod under "none"; any
    entry that still appears is caught by suite_tree_guard's whole-tree
    manifest comparison.

    This alone does not stop deleting a tracked file and writing a new
    one back at the same path -- unlink is governed by the PARENT
    directory's write permission, which must stay writable for the
    reason above, not by the file's own mode bits. sandbox="bwrap"
    closes that remaining gap (callers pass this same tracked list as
    extra read-only binds): a bind-mounted read-only file cannot be
    unlinked either (confirmed empirically -- EBUSY, "device or
    resource busy", not EACCES). Under sandbox="none" that one gap
    remains, the same same-UID class of limitation documented elsewhere
    in this codebase for that mode."""
    for path in tracked:
        if path.is_symlink() or not path.is_file():
            continue
        current = stat.S_IMODE(path.stat().st_mode)
        path.chmod(current & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def link_snapshot_deps(project_cfg, repo: Path, snapshot: Path) -> None:
    """Give the snapshot the same untracked dependency links a worker copy
    gets (project copy_links, e.g. node_modules) -- read through to the
    repo, so a scene/test can resolve its test tools.

    Deliberately does NOT call engine.application.steps.open_job._link_copy_link_entry
    (used for WORKER role copies): that helper fails closed
    ("copy_link_source_escapes_repo") when `repo/entry` resolves outside
    `repo`, to guard against a worker's own git-tracked commit planting an
    intermediate symlink that makes a role copy -- built by git-archiving
    that very commit -- escape `repo`. That risk does not apply here:
    every current copy_links entry (node_modules) is also one of the
    project's own `excludes`, i.e. untracked, written straight onto
    `repo`'s filesystem by engine/tooling infra (tools/gb2's worktree
    setup, npm install) and never by anything in a worker's commit; the
    snapshot's own git-archive step (make_snapshot, run before this)
    already strips every untracked path out, so there is no tracked-tree
    content here that could smuggle an escaping symlink. `repo/entry`
    resolving outside `repo` is therefore not failed closed on below --
    linking the snapshot to it exposes nothing beyond what every
    ordinary bench run already reads straight out of `repo` via the
    sandbox's default `--ro-bind / /` (engine.infrastructure.sandbox.wrap module
    docstring), and this project's own node_modules IS exactly this
    shape (tools/gb2's worktree setup can leave it a symlink to an
    external node_modules tree -- confirmed empirically: with the
    escape check, the snapshot ended up with no node_modules at all,
    `npx vitest run` fell back to the registry, and the run failed
    closed with npm EAI_AGAIN under net=False). `copy_link_writable_
    subpaths` are unaffected either way: they always become real,
    empty, LOCAL directories inside the snapshot, never symlinked to
    the repo or its target (see the per-child loop below) -- nothing
    written there can reach the real repo or the external dependency
    tree. The DESTINATION-side checks (a pre-existing symlink in
    `dest`'s own parent chain, which COULD have been planted by the
    snapshot's own git-archived tracked tree) are still enforced below,
    since that risk is unrelated to where the source lives."""
    dest_real = Path(os.path.realpath(snapshot))
    writable_subpaths_by_entry = getattr(project_cfg, "copy_link_writable_subpaths", {}) or {}
    for entry in getattr(project_cfg, "copy_links", ()) or ():
        _link_snapshot_dep_entry(
            dest_real, snapshot, repo, entry,
            tuple(writable_subpaths_by_entry.get(entry, ())),
        )


def _link_snapshot_dep_entry(dest_real: Path, dest: Path, repo: Path, entry: str,
                             writable_subpaths: tuple[str, ...] = ()) -> None:
    """One copy_links entry re-exposed inside a disposable snapshot --
    see link_snapshot_deps for why this does not reuse (and does not
    need) engine.application.steps.open_job._link_copy_link_entry's source-
    containment check. Silently does nothing for a missing/unsafe case
    (same fail-closed-but-non-raising shape as that function), since a
    missing test dependency surfaces on its own as a test failure
    rather than needing a second signal here.

    The actual merge-into-an-existing-real-directory behavior (defect
    22 -- a copy_links entry like "knowledge" that is only PARTLY
    gitignored, so `evidence.export_tree`'s own certified-fingerprint
    extraction already materialized a real directory with that one
    tracked file in it, before this function ever runs) is shared with
    open_job._link_copy_link_entry via `link_entry_children`, passing
    `repo_real=None` so that shared helper skips only the source-
    containment check this function's own docstring says must not
    apply here, while still merging children the same way."""
    target = repo / entry
    if not target.exists():
        return
    current = dest
    for part in Path(entry).parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            return  # destination parent already a symlink -- unsafe, skip
    current.mkdir(parents=True, exist_ok=True)
    parent_real = Path(os.path.realpath(current))
    if parent_real != dest_real and not _is_within(parent_real, dest_real):
        return  # destination parent escapes the snapshot -- unsafe, skip

    entry_dest = dest / entry
    if not writable_subpaths:
        if not entry_dest.exists():
            entry_dest.symlink_to(target)
            return
        if entry_dest.is_symlink():
            return  # already linked (e.g. a re-run); nothing further to do
        if not target.is_dir() or not entry_dest.is_dir():
            return  # tracked content already there wins; nothing to link
        link_entry_children(target, entry_dest, dest_real, repo_real=None)
        return

    if not target.is_dir():
        return  # writable_subpaths only make sense for a directory entry
    entry_dest.mkdir(parents=True, exist_ok=True)
    link_entry_children(target, entry_dest, dest_real, repo_real=None,
                         skip_names=frozenset(writable_subpaths))
    for subpath in writable_subpaths:
        (entry_dest / subpath).mkdir(parents=True, exist_ok=True)
