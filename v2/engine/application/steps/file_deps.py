"""`file:../...` package.json dependencies that point OUTSIDE a
project's own repo (결함 17, evidence bench seq 1703: `npx
vitest run` failing with `Cannot find package '@sample-app/kit/db'`
inside the engine's live-workspace-managed worktree, even though the exact
same `file:../_shared/kit` dependency resolves fine in the project's
real, un-mirrored source checkout).

Root cause: `tools/gb2`'s managed worktree nests one directory level
deeper than a project's real source checkout (`GB2_APPS_ROOT/<app>/`
vs. `GB2_WORKSPACE/apps/<app>/repo/`), so a dependency's own relative
"../" escape -- preserved byte-for-byte by every copy this engine makes
(`cp -r` into a fresh worktree, `_make_copy`'s worker copy,
`suite_snapshot.make_snapshot`'s disposable snapshot) -- lands one
level short of the real target every time it is walked inside any of
those copies.

This module does NOT re-walk the relative path against the host
filesystem at use time (that would just reproduce the same bug one
level later). Instead:
  - `discover_escaping_file_deps`/`resolve_escaping_target` run ONCE,
    against the project's REAL source checkout, at `gb2 init` time
    (tools/gb2lib/workspace.py) -- the one place that still knows the
    real, un-mirrored location -- and their result (relspec, the
    already-correct absolute target) is persisted into
    ProjectConfig.file_dep_targets.
  - `remap_pairs_for_root` is the one function every later sandbox
    builder (engine.application.steps.suite_run._bench_sandbox_argv --
    shared by bench's own run_tests/trust_checks, every
    engine.application.acceptance scene/full-suite disposable snapshot, and
    engine.application.acceptance_probe, via that same shared function --
    plus engine.application.steps.worker_sandbox.sandboxed_argv for a
    builder's own copy) calls to turn that persisted, already-resolved
    target back into a `sandbox.wrap(readonly_remap=...)` pair for
    WHATEVER root it happens to be running against (the real repo, a
    worker copy, a disposable snapshot) -- re-deriving only the
    DESTINATION side (which varies per root) from the SAME relspec,
    never re-deriving the source side at all.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_DEP_KEYS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
_FILE_PREFIX = "file:"


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _file_dep_relspecs(package_json: Path) -> tuple[str, ...]:
    """Every distinct `file:...` dependency value in `package_json`
    (dependencies/devDependencies/optionalDependencies/peerDependencies),
    stripped of its `file:` prefix, in first-seen order. () if the file
    is missing or not valid JSON -- a project with no package.json (or
    a momentarily unreadable one) must never raise here; the caller's
    own test_cmd will fail on its own in that case, same as today."""
    try:
        raw = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    if not isinstance(raw, dict):
        return ()
    seen: dict[str, None] = {}
    for key in _DEP_KEYS:
        deps = raw.get(key)
        if not isinstance(deps, dict):
            continue
        for value in deps.values():
            if isinstance(value, str) and value.startswith(_FILE_PREFIX):
                seen[value[len(_FILE_PREFIX):]] = None
    return tuple(seen)


def discover_escaping_file_deps(repo: Path) -> tuple[str, ...]:
    """Of `repo`'s own package.json `file:` deps, only the ones whose
    path -- joined onto `repo` itself, the same way npm/node resolve a
    `file:` specifier relative to the package.json that names it --
    normalizes to somewhere OUTSIDE `repo`. A `file:./vendor/x` sibling
    dependency already inside the repo is left alone entirely: nothing
    about it is nesting-depth-sensitive, so it needs no remap."""
    package_json = repo / "package.json"
    out: list[str] = []
    for relspec in _file_dep_relspecs(package_json):
        joined = Path(os.path.normpath(str(repo / relspec)))
        if not _is_within(joined, repo):
            out.append(relspec)
    return tuple(out)


def resolve_escaping_target(source_repo: Path, relspec: str) -> Path:
    """`relspec` resolved against the project's REAL source checkout
    (never a gb2-managed copy) -- realpath()'d so a further symlink in
    the real tree is also flattened once, here, rather than re-walked
    by every later sandbox build."""
    return Path(os.path.realpath(source_repo / relspec))


class FileDepTargetError(ValueError):
    """Raised by validate_escaping_target -- see that function."""


def validate_escaping_target(target: Path, *, forbidden_roots: tuple[Path, ...]) -> None:
    """Raise FileDepTargetError unless `target` is safe to expose
    read-only, unconditionally, in every later sandbox this dependency's
    remap reaches: must be an absolute, existing directory; no
    dot-prefixed path component anywhere in it (keeps a credentials
    folder like `.aws`/`.ssh` out even if some ancestor is otherwise
    allowed); none of its own immediate children may be dot-prefixed
    either (catches a leaked `.env*`/`.git` sitting directly inside the
    target itself -- the one check reference_snapshot.validate_
    reference_path does not make, since that function copies files
    instead of exposing a whole directory and already prunes hidden
    entries file-by-file during that copy); and it must not equal or
    nest with any of `forbidden_roots` (the engine's own home/secrets/
    ledger dirs, this engine's own source tree, any registered
    project's repo -- the same shape of check reference_snapshot.
    validate_reference_path makes for its own "full_block" roots,
    applied here too since this directory is exposed just as directly)."""
    if not target.is_absolute():
        raise FileDepTargetError(f"file dep target must be absolute: {target}")
    if any(part.startswith(".") for part in target.parts):
        raise FileDepTargetError(f"file_dep_hidden_component: {target}")
    if not target.is_dir():
        raise FileDepTargetError(f"file dep target must be a directory: {target}")
    try:
        children = os.listdir(target)
    except OSError as exc:
        raise FileDepTargetError(f"file dep target unreadable: {target}") from exc
    hidden = [name for name in children if name.startswith(".")]
    if hidden:
        raise FileDepTargetError(f"file_dep_target_has_hidden_entries: {target}: {hidden}")
    for root in forbidden_roots:
        root_real = Path(os.path.realpath(root))
        if target == root_real or _is_within(target, root_real) or _is_within(root_real, target):
            raise FileDepTargetError(f"file_dep_target_forbidden_overlap: {target}")


def remap_pairs_for_root(
    root: Path, file_dep_targets: tuple[tuple[str, str], ...],
) -> tuple[tuple[Path, Path], ...]:
    """(source, dest) pairs for `sandbox.wrap`'s own `readonly_remap`:
    for each (relspec, target) ProjectConfig.file_dep_targets already
    carries (resolved and validated once, at `gb2 init` time, against
    the real source checkout -- see this module's own docstring),
    `dest` is `relspec` re-joined onto THIS CALL's `root` -- the real
    repo for a bench run, a worker's own copy, or a disposable
    acceptance/probe snapshot, whichever `root` the caller is actually
    about to run a sandboxed command against -- so the same escaping
    relative path resolves, inside that sandbox, to the one real
    target directory regardless of which root it was walked from.
    `source` is always `target` itself, never anything derived from
    `root` -- the already-resolved, already-validated absolute path
    carries over unchanged. A pair whose `source` no longer exists is
    dropped (sandbox.wrap's own readonly_remap already drops a missing
    source too; dropping it here as well keeps this function's return
    value accurate on its own, for a caller or test that inspects it
    directly without going through wrap())."""
    pairs: list[tuple[Path, Path]] = []
    for relspec, target in file_dep_targets:
        target_path = Path(target)
        if not target_path.exists():
            continue
        dest = Path(os.path.normpath(str(root / relspec)))
        pairs.append((target_path, dest))
    return tuple(pairs)


def remap_pairs_for_roots(
    roots: tuple[Path, ...], file_dep_targets: tuple[tuple[str, str], ...],
) -> tuple[tuple[Path, Path], ...]:
    """remap_pairs_for_root for every root in `roots`, duplicates dropped
    (결함 35: a worker needs its own copy AND the real repo mapped, since
    a copy_links symlink resolves relative links against the repo)."""
    pairs: list[tuple[Path, Path]] = []
    for root in roots:
        for pair in remap_pairs_for_root(root, file_dep_targets):
            if pair not in pairs:
                pairs.append(pair)
    return tuple(pairs)


def sandbox_file_dep_pairs(
    root: Path, repo: Path, file_dep_targets: tuple[tuple[str, str], ...],
) -> tuple[tuple[Path, Path], ...]:
    """The ONE function every sandbox builder (engine.application.steps.
    worker_sandbox.sandboxed_argv, engine.application.steps.suite_run.
    _bench_sandbox_argv) calls to get (source, dest) pairs for
    sandbox.wrap's own `readonly_remap` -- never remap_pairs_for_root/
    remap_pairs_for_roots directly, which only compute the path
    arithmetic and know nothing about whether bwrap can actually mount
    there (결함 42,.

    `root` is THIS call's own sandboxed root -- a worker's copy_root, or
    the repo itself for an ordinary bench run (`run_tests`/`trust_checks`
    pass the real repo for both `root` and `repo`, below, since they are
    the same path there); `repo` is ALWAYS the project's real,
    un-mirrored repo (ctx.config.projects[project].repo), even when
    `root` is something else entirely -- a disposable acceptance/probe
    snapshot (engine.application.acceptance_bench.run_in_bench_sandbox),
    which never equals the real repo. Pairs are computed for BOTH
    (remap_pairs_for_roots, duplicates dropped): a copy_links entry (e.g.
    `node_modules`) is always a symlink back to the REAL repo (or, for a
    disposable snapshot, a symlink into the real repo's own copy_links
    target -- see suite_snapshot.link_snapshot_deps), so a further
    escaping relative symlink inside it (`@scope/kit ->
    ../../../_shared/kit`) resolves against the REAL repo's own parent
    at the OS level, never against `root` -- the `root`-rooted pair is
    therefore usually a no-op, but kept anyway (cheap, and correct for
    the rarer copy_link_writable_subpaths shape, where a child symlink
    is recreated with an ABSOLUTE target computed from `root`'s own
    tree, not re-walked against the repo).

    `root.config.projects[project].hidden_paths` (tools/gb2lib/
    workspace.py's own `_setup_app_workspace`) hides the WHOLE apps root,
    a project's `repo` included -- broader than `repo` alone, on
    purpose, to keep one app's sandbox from seeing another app's source
    tree. A `file_dep_targets` entry's own real target is typically a
    SIBLING of `repo` (e.g. `repo/../_shared/kit`), so it sits inside
    that same hidden ancestor and is never re-exposed by `repo` alone
    (confirmed empirically: a readonly_remap keyed to a disposable
    snapshot's own root, as every pre-결함-42 caller computed, never
    named this sibling path at all, so the real target stayed hidden and
    `require()` failed with MODULE_NOT_FOUND even though `repo` itself
    was separately re-exposed). Each returned `dest` that does not yet
    exist on the host is pre-created here, as an empty directory --
    exactly the technique suite_run._bench_sandbox_argv's own
    bench_readonly_paths loop already uses, and for the same reason:
    bwrap can only auto-create a missing readonly_remap mountpoint under
    an ancestor that is ITSELF currently writable, which a `dest` nested
    under a `hidden` (tmpfs, later remounted read-only) ancestor is only
    true of up until that remount -- a `dest` one level further out,
    under the plain host mirror with no mount of its own at all, is
    never writable, and bwrap fails closed there with "Can't mkdir
    parents ...: Read-only file system" instead of creating it. Pre-
    creating `dest` on the host, here, before `sandbox.wrap` ever runs,
    means the later `--ro-bind` only ever mounts OVER an existing path,
    the same shape as every other nested bind this codebase already
    relies on."""
    pairs = remap_pairs_for_roots((root, repo), file_dep_targets)
    for _source, dest in pairs:
        if not dest.exists():
            dest.mkdir(parents=True, exist_ok=True)
    return pairs


__all__ = [
    "remap_pairs_for_roots",
    "sandbox_file_dep_pairs",
    "FileDepTargetError",
    "discover_escaping_file_deps",
    "resolve_escaping_target",
    "validate_escaping_target",
    "remap_pairs_for_root",
]
