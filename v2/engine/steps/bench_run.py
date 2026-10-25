"""bench — run the project's real test suite plus trust-checked cases.

compute_fingerprint  fp = evidence.fingerprint(repo); journal {fp}
                     (reconcile False: a crash mid-run re-computes rather
                     than trusting a half-written journal row).
run_tests            Speed default (project.bench_scope, "related" unless
                     set to "full"): with scope "related" and a
                     configured project.related_cmd, run related_cmd
                     against only this attempt's changed source files
                     (engine.steps.open_job.dirty_paths against HEAD,
                     the same range the fingerprint covers -- at this
                     point in the stage list HEAD still equals
                     base_head, since commit runs after bench) in place
                     of the literal "{files}" token, one argv entry per
                     file; falls back to the full project.test_cmd when
                     related_cmd is unset or there are no changed
                     source files. Runs IN repo (never a disposable
                     copy of it -- see _bench_immutable_source's own
                     docstring for why a copy built from compute_
                     fingerprint's fp would silently lose whatever an
                     install request just wrote into a gitignored
                     destination like .deps/ or node_modules/, breaking
                     the install-then-test flow), but every tracked
                     file AND tracked directory is bind-mounted read-
                     only alongside it (_bench_immutable_source, the
                     same tracked_file_paths/tracked_directories
                     engine.acceptance uses for ITS OWN disposable
                     snapshot, so the bench cannot write tracked source) --
                     an explicit environment built from config.
                     worker_env_passthrough only (never the processor's
                     own full os.environ, which may carry secrets a
                     project's test_cmd has no business seeing); journal
                     {returncode, tail, scope, argv, no_tests_executed}
                     (tail = last 2000 chars of combined stdout+stderr;
                     scope is "related"/"full"/"full(fallback)"; argv is
                     the exact command run; no_tests_executed -- see
                     looks_like_zero_tests_ran -- true iff returncode==0
                     but nothing was actually exercised). A timeout ->
                     StepFailed
                     ("tests_timeout"). Neither a non-zero exit NOR
                     no_tests_executed is itself a step failure here:
                     record_bench (not this step) folds both into the
                     overall passed/reason.
trust_checks         project.bench_cases (a JSON file in the repo) ->
                     bench.Case list; each case's run() executes
                     `test_cmd + [selector]` with os.environ overridden
                     by that case's own optional "env" object (e.g. a
                     negative-control case that only fires under a
                     sample-fixture-specific env var, so the real
                     run_tests suite stays green while the control case
                     still exercises its failure signature);
                     bench.run_bench() decides pass/fail/reason.
                     Missing/absent bench_cases -> StepFailed
                     ("no_bench_cases"). reconcile False.
record_bench         recompute the fingerprint; if it differs from
                     compute_fingerprint's, StepFailed
                     ("tree_changed_during_bench") -- no evidence is
                     recorded for a tree a worker kept editing under our
                     feet. Otherwise passed = (run_tests returncode==0)
                     and trust.passed (plan §5: the real suite AND the
                     trust-checked cases must both be clean); reason is
                     trust.reason when the cases failed, else
                     "tests_failed" when only the real suite is red,
                     else "ok". Record evidence (kind "bench", payload
                     {"passed", "reason", "request_id", "per_case",
                     "tests_returncode", "scope", "argv"} -- scope/argv
                     carried over from run_tests' own journal payload)
                     FIRST, then, if the bench did
                     not pass, raise StepFailed(reason) -- evidence is
                     recorded before raising so the failure (and its
                     signatures) stays visible to three_match/record
                     even though the request itself ends 'failed'.
                     reconcile: evidence for this request_id already
                     recorded AND passed=True -- a recorded passed=False
                     re-raises StepFailed(reason) rather than reading
                     its own existence as success.
"""
from __future__ import annotations

import json
import re
import os
import shutil
import stat
import subprocess
from pathlib import Path

from engine import bench, evidence, ids, sandbox
from engine.config import EngineConfig
from engine.gitutil import git_argv
from engine.processor import StepContext, StepDef, StepFailed
from engine.steps.open_job import _is_within, dirty_paths

_TAIL_CHARS = 2000

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
    this is ("scene-<i>", "full_suite", "plan" -- engine.acceptance;
    "bench" -- this module's own run_tests/trust_checks). Each label
    gets its OWN directory so one stage's run can never see another's
    leftover untracked state."""
    return config.home / "acceptance" / ids.attempt_dirname(attempt_id) / label


def make_snapshot(repo: Path, dest: Path, fp: str) -> None:
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
        separately by each caller."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    evidence.export_tree(repo, fp, dest)
    subprocess.run(git_argv(dest, "init", "-q"), check=True, capture_output=True)
    env = {**os.environ, **_SNAPSHOT_GIT_IDENTITY, "GIT_CONFIG_GLOBAL": "/dev/null"}
    subprocess.run(git_argv(dest, "add", "-A"), check=True,
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
    itself (a scene/test dropping a brand-new, untracked scratch/output file at
    the snapshot's own ROOT is existing, accepted, test-covered
    behavior (engine.acceptance's own "must keep working" sibling-leak
    tests); what must NOT keep working is a scene/test planting a
    brand-new file INSIDE an existing tracked package directory, e.g. a
    sibling module next to a real one that a loose `import` could
    resolve first -- "a scene can create an untracked package
    shadowing a tracked module". lock_tracked_files' per-file chmod
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
    sandbox mode, including mode="none" (engine.sandbox.wrap is a no-op
    there, so the per-file read-only BIND a caller also passes never
    runs at all -- this chmod is the only guard that mode has).
    Directories are left untouched, and so is anything that is not,
    right now, a plain tracked file (a symlink git tracked, or a path a
    prior stage already turned into something else) -- only pre-
    existing tracked FILES lose their write bit, so a scene/test can
    still create a brand-new, untracked file anywhere, including next
    to tracked content (must keep working -- see tracked_directories
    for the one place that must NOT keep working).

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

    Deliberately does NOT call engine.steps.open_job._link_copy_link_entry
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
    sandbox's default `--ro-bind / /` (engine.sandbox.wrap module
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
    need) engine.steps.open_job._link_copy_link_entry's source-
    containment check. Silently does nothing for a missing/unsafe case
    (same fail-closed-but-non-raising shape as that function), since a
    missing test dependency surfaces on its own as a test failure
    rather than needing a second signal here."""
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

    if not writable_subpaths:
        (dest / entry).symlink_to(target)
        return

    if not target.is_dir():
        return  # writable_subpaths only make sense for a directory entry
    entry_dest = dest / entry
    entry_dest.mkdir(parents=True, exist_ok=True)
    writable_names = set(writable_subpaths)
    for child_name in os.listdir(target):
        if child_name in writable_names:
            continue  # the writable subpath wins; never symlinked to the repo
        (entry_dest / child_name).symlink_to(target / child_name)
    for subpath in writable_subpaths:
        (entry_dest / subpath).mkdir(parents=True, exist_ok=True)


def _project_config(ctx: StepContext):
    return ctx.config.projects[ctx.project]


def _repo(ctx: StepContext) -> Path:
    return _project_config(ctx).repo


def _excludes(ctx: StepContext) -> tuple[str, ...]:
    return _project_config(ctx).excludes


def _timeout_sec(ctx: StepContext) -> float:
    return _project_config(ctx).test_timeout_sec


def _journal_payload(ctx: StepContext, step_name: str) -> dict[str, object]:
    row = ctx.ledger.steps(ctx.request.request_id).get(step_name)
    if row is None:
        raise StepFailed(f"missing_step:{step_name}")
    return row.payload


def _run_compute_fingerprint(ctx: StepContext) -> dict[str, object]:
    try:
        fp = evidence.fingerprint(_repo(ctx), _excludes(ctx))
    except evidence.FingerprintError as exc:
        raise StepFailed("no_fingerprint") from exc
    submitted = ctx.request.payload.get("fp")
    if submitted is not None and submitted != fp:
        raise StepFailed("tree_changed_since_submit")
    return {"fp": fp}


def _other_project_repos(ctx: StepContext) -> tuple[Path, ...]:
    return tuple(p.repo for name, p in ctx.config.projects.items()
                 if name != ctx.project)


def _bench_home_dir(ctx: StepContext) -> Path:
    """A fresh, empty, per-attempt directory used as the sandboxed
    bench's $HOME -- NEVER the real operator home, regardless of
    bench_home_writable (that flag only decides whether this directory
    itself is mounted writable or read-only, see _bench_sandbox_argv).
    Lives under the engine's own home, so it is a descendant of the
    `hidden` config.home entry and gets re-exposed the same way
    station.py's worker copy does (deeper mount wins)."""
    path = ctx.config.home / "jobs" / ids.attempt_dirname(ctx.attempt_id) / "bench_home"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _bench_sandbox_argv(ctx: StepContext, repo: Path, argv: list[str],
                        env: dict[str, str], *, net: bool = False,
                        extra_readonly: tuple[Path, ...] = ()) -> tuple[list[str], dict[str, str]]:
    """Bench sandbox policy: writable = the repo itself (test runners
    write __pycache__/coverage/etc there). readonly = repo/.git,
    mounted AFTER the repo's own writable bind (deeper path wins) so
    test code cannot plant a `core.fsmonitor`/hook command there for a
    later engine-side git call (evidence.fingerprint et al.) to execute
    outside any sandbox -- plus, unless bench_home_writable is True,
    _bench_home_dir()'s empty per-attempt directory (if it IS True,
    that same directory moves to `writable` instead: either way it is
    never the real HOME). hidden = engine home, secrets_dir, the
    ledger's directory (no gate hook runs here, so unlike
    station.py/review.py the ledger dir is NOT re-added writable),
    every OTHER project's repo, and the real operator HOME outright
    (read from THIS process's own os.environ, never from `env` --
    `env` can carry a bench case's own "env" override, read from a
    JSON file inside the project's repo, and a case-controlled value
    must never decide what gets hidden or mounted writable). The
    returned child_env always pins HOME to _bench_home_dir(), overriding
    whatever HOME `env` itself carried, for the same reason. net = False
    -- a project's test suite has no business making outbound network
    calls as part of this engine's trust checks.

    extra_readonly: additional read-only mounts beyond repo/.git -- every
    existing caller passes `repo` itself as THIS function's `repo` (so it
    is always re-exposed, since it is also `writable`'s own entry); a
    caller that runs `argv` somewhere else entirely (engine.acceptance's
    disposable snapshot, built under config.home and therefore itself
    already hidden-then-re-exposed the same way _bench_home_dir() is)
    still needs the REAL project repo visible, read-only, for any
    on-disk content (e.g. a copy_links entry like node_modules) the
    snapshot only symlinks BACK into -- config.ledger_path.parent (see
    `hidden` below) is that real repo's own parent directory, so without
    this it stays hidden under that tmpfs with nothing to re-expose it
    (confirmed empirically: a snapshot-side symlink into an unlisted real
    repo resolved to nothing inside the sandbox, hanging `npx vitest run`
    trying to fetch the package it could not resolve locally, over a
    net=False sandbox, until it timed out). Defaults to `()`, so every
    pre-existing caller's mount list is unchanged.

    config.bench_readonly_paths (e.g. "~/.cache/ms-playwright", expanded
    at config load against the ENGINE process's own HOME) re-exposes
    each existing entry read-only at its real host path -- and, if it
    is under the real HOME, ALSO at the same relative place under
    _bench_home_dir(), via readonly_remap, so `$HOME/<rel>` resolves
    inside the sandbox despite the real HOME being hidden (a project's
    test suite needing a toolchain cache under the real home, e.g.
    installed browser binaries, without getting the rest of it)."""
    bench_home = _bench_home_dir(ctx)
    child_env = {**env, "HOME": str(bench_home)}
    writable = (repo,)
    readonly: tuple[Path, ...] = (repo / ".git",) + extra_readonly
    readonly_remap: tuple[tuple[Path, Path], ...] = ()
    if ctx.config.bench_home_writable:
        writable += (bench_home,)
    else:
        readonly += (bench_home,)
    hidden = ((ctx.config.home, ctx.config.secrets_dir,
               ctx.config.ledger_path.parent) + _other_project_repos(ctx)
              + ctx.config.projects[ctx.project].hidden_paths)
    real_home = os.environ.get("HOME")
    real_home_path = Path(real_home) if real_home else None
    if real_home_path is not None:
        hidden += (real_home_path,)
    for entry in ctx.config.bench_readonly_paths:
        if not entry.exists():
            continue
        readonly += (entry,)
        if real_home_path is not None:
            try:
                rel = entry.relative_to(real_home_path)
            except ValueError:
                continue
            dest = bench_home / rel
            # bwrap can only auto-create a MISSING mountpoint under an
            # ancestor mount that is itself writable (--bind) -- when
            # bench_home_writable is False (the default), bench_home is
            # mounted --ro-bind, and bwrap then has no write access to
            # create `dest`'s directory/file inside that read-only
            # bind, failing with "Can't mkdir ...: Read-only file
            # system". Pre-creating `dest` on the HOST here (plain
            # filesystem access, outside any
            # sandbox -- bench_home is the engine's own scratch dir)
            # means it already exists as part of whatever bench_home
            # content gets ro-bound, so the later --ro-bind onto it is
            # just a mount over an EXISTING path, never a mkdir, and
            # works regardless of bench_home's own writable/read-only
            # mount kind. Matches the source's own type (dir vs file)
            # so the mountpoint shape is right either way.
            if entry.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.touch(exist_ok=True)
            readonly_remap += ((entry, dest),)
    try:
        wrapped = sandbox.wrap(
            argv, cwd=repo, writable=writable, hidden=hidden, readonly=readonly,
            readonly_remap=readonly_remap, net=net, mode=ctx.config.sandbox,
            child_env=child_env,
        )
    except sandbox.SandboxUnavailable as exc:
        raise StepFailed("sandbox_unavailable") from exc
    return wrapped, child_env


def _bench_env(ctx: StepContext) -> dict[str, str]:
    """An explicit environment for the real suite, built only from
    config.worker_env_passthrough -- never the engine processor's own
    full os.environ, which may carry secrets (API keys, tokens) that a
    project's test_cmd has no business seeing. Passed to every bench
    subprocess (run_tests and each trust-checked case); a case's own
    "env" (see _case_env) is merged on top for that one case only."""
    names = ctx.config.worker_env_passthrough
    return {name: os.environ[name] for name in names if name in os.environ}


def _changed_source_paths(ctx: StepContext) -> tuple[list[str], bool]:
    """(existing_paths, has_removal): this attempt's changed files
    right now (dirty_paths against HEAD -- the same union the
    fingerprint covers), split into the ones that still exist on disk
    (a deleted file is not a useful argument to a "related tests"
    command) and whether ANY changed path no longer exists -- a
    deletion, or the OLD side of a rename (dirty_paths records both
    paths of a rename/copy; its old path never exists on disk after
    the rename, so it is caught by this same check). A caller must
    treat has_removal as "related-scope selection cannot be trusted
    for this change": a related-tests command driven only by the
    paths that still exist would never account for code that depended
    on a path which no longer exists at all (e.g. a deleted module
    other, untouched files still import), silently narrowing the test
    selection below what actually needs checking. Read-only: no
    write, no git state change."""
    project = _project_config(ctx)
    repo = _repo(ctx)
    paths = dirty_paths(repo, project.excludes)
    existing = [p for p in paths if (repo / p).exists()]
    return existing, len(existing) != len(paths)


def _build_related_argv(related_cmd: tuple[str, ...], files: list[str]) -> list[str]:
    """related_cmd with the single literal token "{files}" expanded in
    place into one argv entry per changed file (never joined into one
    string -- each file must be its own argv token)."""
    argv: list[str] = []
    for part in related_cmd:
        if part == "{files}":
            argv.extend(files)
        else:
            argv.append(part)
    return argv


def _resolve_test_argv(ctx: StepContext) -> tuple[str, list[str]]:
    """Returns (scope, argv) for the real-suite run_tests step. The
    request's own payload can force the full suite outright
    (payload["scope"] == "full", e.g. engine.release.ensure_full_bench
    submitting a release's required full-suite bench) -- checked
    first, ahead of the project's own related-tests shortcut, so a
    caller that specifically needs "full" never gets "related" back
    regardless of config. Otherwise "related" only when the project
    opts in (bench_scope == "related", the default) AND configures
    related_cmd AND there is at least one changed source file right
    now AND nothing was deleted or renamed (see
    _changed_source_paths's has_removal: a related-tests command
    driven only by paths that still exist can never account for code
    that depended on a path which no longer exists) -- otherwise
    "full" with the project's own test_cmd, unchanged from before this
    feature."""
    project = _project_config(ctx)
    if ctx.request.payload.get("scope") == "full":
        return "full", list(project.test_cmd)
    if project.bench_scope == "related" and project.related_cmd:
        changed, has_removal = _changed_source_paths(ctx)
        if changed and not has_removal:
            return "related", _build_related_argv(project.related_cmd, changed)
    return "full", list(project.test_cmd)


_VITEST_NO_TESTS_RE = re.compile(r"No test files found", re.I)
_VITEST_ZERO_TESTS_SUMMARY_RE = re.compile(r"^\s*Tests\s+0\s", re.I | re.M)
# A "Tests" summary line that never mentions "passed" or "failed"
# anywhere on it, but does mention "skipped" -- every selected test
# was skipped, not merely run and found correct, so nothing was
# actually exercised even though the file-selection step itself
# matched something (e.g. "Tests  1 skipped (1)"). The negative
# lookahead scans the rest of the line before "skipped" is reached, so
# a genuinely mixed line like "3 passed | 2 skipped (5)" does not
# match this (at least one real pass/fail outcome did occur there).
_VITEST_ALL_SKIPPED_RE = re.compile(
    r"^\s*Tests\s+(?!.*\b(?:passed|failed)\b).*\bskipped\b", re.I | re.M)


def looks_like_zero_tests_ran(combined_output: str) -> bool:
    """True iff `combined_output` carries one of vitest's own "nothing
    was actually exercised" shapes (module docstring on the regexes
    just above this function): "No test files found" (an installed
    vitest's own `passWithNoTests=true` default makes that exit 0, not
    1, looking exactly like an ordinary clean pass), a "Tests  0 ..."
    summary, or a "Tests ... skipped" summary with no passed/failed
    count on that same line -- a selector/suite matching something is
    not the same as anything actually having been exercised. This is
    the one no-tests-ran signal this codebase has; engine.acceptance
    reuses it VERBATIM (never a separately invented heuristic) for
    scenes and the full suite, so a skipped test can never stand in
    as evidence -- an exit-0 run
    that executed nothing must never count as having proven anything.
    Vitest-specific wording only: a pytest-based project's own
    "collected 0 items"/all-deselected shape is not recognized here --
    extending this to other runners is new-heuristic territory this
    function deliberately stays out of."""
    return bool(_VITEST_NO_TESTS_RE.search(combined_output)
                or _VITEST_ZERO_TESTS_SUMMARY_RE.search(combined_output)
                or _VITEST_ALL_SKIPPED_RE.search(combined_output))


def _ran_zero_tests(scope: str, combined_output: str) -> bool:
    """True iff a "related"-scope run executed ZERO actual tests (see
    looks_like_zero_tests_ran for the actual signal). Only ever checked
    for scope "related" here -- a "full" run legitimately exits 0 on an
    empty/trivial project without THIS caller's own related->full
    fallback applying; _run_run_tests separately applies
    looks_like_zero_tests_ran to the run's own FINAL output regardless
    of scope (see its own comment), which is the P2 guard."""
    if scope != "related":
        return False
    return looks_like_zero_tests_ran(combined_output)


def _run_test_cmd(ctx: StepContext, repo: Path, test_cmd: list[str],
                  env: dict[str, str], *,
                  extra_readonly: tuple[Path, ...] = ()) -> tuple[int, str]:
    argv, child_env = _bench_sandbox_argv(ctx, repo, list(test_cmd), env,
                                          extra_readonly=extra_readonly)
    # mode="bwrap": child_env is already baked into argv via --setenv;
    # the Popen env here must be the sandbox launcher's sanitized env,
    # never child_env itself (see sandbox.exec_env's docstring).
    # mode="none": exec_env returns child_env unchanged -- there is no
    # bwrap process to separate it from.
    popen_env = sandbox.exec_env(ctx.config.sandbox, child_env)
    try:
        result = subprocess.run(
            argv, cwd=str(repo), capture_output=True,
            timeout=_timeout_sec(ctx), text=True, env=popen_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise StepFailed("tests_timeout") from exc
    combined = (result.stdout or "") + (result.stderr or "")
    return result.returncode, combined


def _bench_immutable_source(ctx: StepContext) -> tuple[Path, ...]:
    """Every tracked file AND tracked directory of `_repo(ctx)` itself
    (tracked_file_paths/tracked_directories -- the SAME functions
    engine.acceptance uses for a scene/the full suite's OWN disposable
    snapshot) -- for a caller to bind read-only alongside the real repo
    in a bwrap sandbox call, WITHOUT ever copying the repo into a
    disposable snapshot and WITHOUT ever chmod'ing anything on it (both
    would be wrong here: a disposable copy of the real repo at THIS
    moment would not contain whatever a just-finished install request
    wrote into a gitignored destination like .deps/ or node_modules/ --
    those are never part of evidence.fingerprint's own tracked-plus-
    gitignore-aware range, so a snapshot built from fp alone would
    silently lose them, breaking the install-then-test flow the exact
    same way engine.acceptance's own copy_links exists to avoid for
    ITS disposable snapshots, but copy_links itself is gated to require
    sandbox="bwrap" at config load time (ProjectConfig.copy_links_
    requires_bwrap) -- not every project configured for bench has
    opted into that; and chmod'ing the real repo's own tracked files
    read-only would persist past this one bench request, breaking a
    LATER task's own merge into those same files on the SAME shared
    repo, which a disposable snapshot's chmod never risked since that
    copy was thrown away afterwards).

    A bwrap bind mount is per-process and never touches the host
    filesystem's own permissions, so binding these same paths read-only
    for just the duration of this one sandboxed test run is safe on the
    real, ongoing, shared repo in a way chmod never would be -- this is
    source and dependencies mounted immutably, without isolating
    scratch output from import paths (sandbox="none" has no bind
    mounts at all, so this protection does not apply there -- the same
    documented, not-chased limitation this codebase already accepts
    elsewhere for that mode)."""
    repo = _repo(ctx)
    return tracked_file_paths(repo) + tracked_directories(repo)


def _run_run_tests(ctx: StepContext) -> dict[str, object]:
    scope, test_cmd = _resolve_test_argv(ctx)
    repo = _repo(ctx)
    extra_readonly = _bench_immutable_source(ctx)
    env = _bench_env(ctx)
    returncode, combined = _run_test_cmd(ctx, repo, list(test_cmd), env,
                                         extra_readonly=extra_readonly)
    if _ran_zero_tests(scope, combined):
        # The related selector matched nothing real: a related-scope
        # run that executed zero tests must never be trusted as
        # "nothing needed checking" -- fall back to the full suite
        # instead of recording a pass over code nothing actually
        # exercised. "full(fallback)" is a
        # distinct scope value from an ordinary "full" choice, so a
        # reader of bench evidence can tell the two apart.
        scope = "full(fallback)"
        test_cmd = tuple(_project_config(ctx).test_cmd)
        returncode, combined = _run_test_cmd(ctx, repo, list(test_cmd), env,
                                             extra_readonly=extra_readonly)
    # An exit-0 run that executed
    # zero tests must never pass, regardless of scope -- the related-
    # scope fallback above already covers "related selected nothing",
    # but a "full"/"full(fallback)" run can land here too (e.g. every
    # test in the real suite itself skipped). Checked on the run's
    # own FINAL output only (never a fallback's now-discarded first
    # attempt). record_bench (not this step) folds no_tests_executed
    # into the overall passed/reason, same as it already does for a
    # plain non-zero returncode.
    no_tests_executed = returncode == 0 and looks_like_zero_tests_ran(combined)
    return {"returncode": returncode, "tail": combined[-_TAIL_CHARS:],
            "scope": scope, "argv": list(test_cmd),
            "no_tests_executed": no_tests_executed}


_ERROR_LINE = re.compile(r"^E\s+(?:([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))\b|(assert)\b)", re.M)

# vitest's own shape (no pytest "E   ..." lines to match at all): a run
# that collected and executed zero tests -- e.g. the control selector no
# longer matches any file -- must be its OWN distinct signature,
# "no_tests", never silently folded into a generic "exit:N" that could
# accidentally equal a negative control's configured expected_signature
# without the control's own assertion ever actually having run (a
# vitest install that defaults `passWithNoTests` to true exits 0 for
# exactly this case, looking like an ordinary clean pass). "assertion"
# is only reported when the output BOTH names AssertionError and
# vitest's own "N failed" summary line -- a failed test for some other
# reason (a thrown non-assertion error, a timeout vitest itself reports
# as a failure) must not be silently counted as the specific assertion
# failure a negative control's expected_signature "assertion" is
# supposed to prove actually fired.
_VITEST_FAILED_COUNT_RE = re.compile(r"\b\d+\s+failed\b", re.I)
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _case_signature(output: dict[str, object]) -> str | None:
    """Classify a failed case. pytest-shaped output: from its `E   ...`
    lines only (not from arbitrary text such as `from x import
    AssertionError` in a traceback source line) -- import/collection
    failures win over assertions. vitest-shaped output (no pytest
    `E   ` line matched at all): see the vitest regexes above."""
    # Test runners may colour their output; terminal escape codes would
    # otherwise break word boundaries in the summary-line patterns.
    text = _ANSI_ESCAPE.sub("", str(output.get("text", "")))
    names = [m.group(1) or "assert" for m in _ERROR_LINE.finditer(text)]
    for name in names:
        short = name.rsplit(".", 1)[-1]
        if short in ("ModuleNotFoundError", "ImportError"):
            return short
    if any(name == "assert" or name.rsplit(".", 1)[-1] == "AssertionError" for name in names):
        return "assertion"
    if _VITEST_NO_TESTS_RE.search(text):
        return "no_tests"
    if "AssertionError" in text and _VITEST_FAILED_COUNT_RE.search(text):
        return "assertion"
    return f"exit:{output.get('returncode')}"


# A bench case's "env" comes from bench_cases.json INSIDE the project's
# repo -- content a worker (or anyone with repo write access) can
# influence. These names are therefore never honored from a case,
# regardless of value: they could redirect which `test_cmd` binary
# resolves (PATH), inject into the test process's own dynamic linker
# (LD_PRELOAD/LD_LIBRARY_PATH), or override the bench HOME
# _bench_sandbox_argv deliberately pins (HOME).
_FORBIDDEN_CASE_ENV_KEYS = frozenset({"PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "HOME"})


def _case_env(base_env: dict[str, str], spec: dict[str, object]) -> dict[str, str]:
    """Per-case env override (sample/fixture detail, not an engine rule):
    merged over the bench's own passthrough-only base_env (see
    _bench_env) for that one case's subprocess only, so a case can need
    an env var to reproduce its failure without making run_tests'
    full-suite run red, and without leaking the processor's full
    os.environ into either -- every case runs with base_env at minimum,
    never a bare None that would fall back to subprocess.run inheriting
    the processor's whole environment. _FORBIDDEN_CASE_ENV_KEYS entries
    in the case's own "env" are silently dropped, never applied."""
    raw = spec.get("env")
    if not isinstance(raw, dict):
        return dict(base_env)
    overrides = {str(k): str(v) for k, v in raw.items()
                 if str(k) not in _FORBIDDEN_CASE_ENV_KEYS}
    return {**base_env, **overrides}


def _make_case(ctx: StepContext, test_cmd: tuple[str, ...], repo: Path,
                timeout_sec: float, base_env: dict[str, str],
                spec: dict[str, object], *,
                extra_readonly: tuple[Path, ...] = ()) -> bench.Case:
    selector = str(spec["selector"])
    name = str(spec["name"])
    env = _case_env(base_env, spec)
    argv, child_env = _bench_sandbox_argv(ctx, repo, [*test_cmd, selector], env,
                                          extra_readonly=extra_readonly)
    popen_env = sandbox.exec_env(ctx.config.sandbox, child_env)
    captured: dict[str, object] = {}

    def run() -> bool:
        try:
            result = subprocess.run(
                argv, cwd=str(repo), capture_output=True,
                timeout=timeout_sec, text=True, env=popen_env,
            )
        except subprocess.TimeoutExpired:
            captured["text"] = ""
            captured["returncode"] = "timeout"
            return False
        captured["text"] = (result.stdout or "") + (result.stderr or "")
        captured["returncode"] = result.returncode
        return result.returncode == 0

    def signature_of() -> str | None:
        return _case_signature(captured)

    return bench.Case(
        name=name, run=run,
        expect_fail=bool(spec.get("expect_fail", False)),
        expected_signature=(
            str(spec["expected_signature"]) if spec.get("expected_signature") is not None
            else None
        ),
        signature_of=signature_of,
    )


def _load_cases(ctx: StepContext) -> list[bench.Case]:
    project = _project_config(ctx)
    bench_cases_rel = project.bench_cases
    if not bench_cases_rel:
        raise StepFailed("no_bench_cases")
    cases_path = _repo(ctx) / bench_cases_rel
    if not cases_path.exists():
        raise StepFailed("no_bench_cases")
    try:
        raw = json.loads(cases_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise StepFailed("no_bench_cases") from exc
    if not isinstance(raw, list) or not raw:
        raise StepFailed("no_bench_cases")
    timeout_sec = _timeout_sec(ctx)
    base_env = _bench_env(ctx)
    # Each case's tracked source is immutable the same way run_tests'
    # own run is (_bench_immutable_source) -- see that function's own
    # docstring for why this binds the real repo read-only rather than
    # running against a disposable copy of it.
    extra_readonly = _bench_immutable_source(ctx)
    return [_make_case(ctx, project.test_cmd, _repo(ctx), timeout_sec, base_env, spec,
                       extra_readonly=extra_readonly)
            for spec in raw]


def _run_trust_checks(ctx: StepContext) -> dict[str, object]:
    fp = str(_journal_payload(ctx, "compute_fingerprint")["fp"])
    cases = _load_cases(ctx)
    result = bench.run_bench(cases, fp)
    return {"passed": result.passed, "reason": result.reason, "per_case": result.per_case}


def _run_record_bench(ctx: StepContext) -> dict[str, object]:
    fp = str(_journal_payload(ctx, "compute_fingerprint")["fp"])
    try:
        now_fp = evidence.fingerprint(_repo(ctx), _excludes(ctx))
    except evidence.FingerprintError as exc:
        raise StepFailed("no_fingerprint") from exc
    if now_fp != fp:
        raise StepFailed("tree_changed_during_bench")
    run_tests_result = _journal_payload(ctx, "run_tests")
    tests_returncode = run_tests_result["returncode"]
    # An exit-0 run that executed zero tests is not "ok" either --
    # run_tests_result.get (not []) since a step journaled by a
    # pre-P2 engine version (resumed from an old attempt) never wrote
    # this key at all, and a missing flag must never read as True.
    no_tests_executed = bool(run_tests_result.get("no_tests_executed", False))
    tests_ok = tests_returncode == 0 and not no_tests_executed
    trust = _journal_payload(ctx, "trust_checks")
    trust_passed = bool(trust["passed"])
    passed = tests_ok and trust_passed
    if not trust_passed:
        reason = str(trust["reason"])
    elif no_tests_executed:
        reason = "no_tests_executed"
    elif not tests_ok:
        reason = "tests_failed"
    else:
        reason = "ok"
    evidence.record(
        ctx.ledger, ctx.project, ctx.attempt_id, "bench", fp,
        payload={
            "passed": passed, "reason": reason,
            "request_id": ctx.request.request_id,
            "per_case": trust.get("per_case", {}),
            "tests_returncode": tests_returncode,
            "scope": run_tests_result.get("scope", "full"),
            "argv": run_tests_result.get("argv", []),
        },
    )
    if not passed:
        raise StepFailed(reason)
    return {"fp": fp, "passed": True}


def _reconcile_false(ctx: StepContext) -> bool:
    return False


def _reconcile_record_bench(ctx: StepContext) -> bool:
    """True iff bench evidence for this request_id was already recorded
    AND it reported passed=True. A crash after _run_record_bench's
    evidence.record but before its own `raise StepFailed(reason)` is
    the case this guards: that recorded failure's mere existence must
    not read back as success on resume -- it re-raises the same
    reason, so the request still terminates 'failed'. Duplicate
    evidence rows are still avoided either way: the passed=True branch
    returns True without a second evidence.record call, same as
    before."""
    for row in ctx.ledger.evidence(ctx.attempt_id, "bench"):
        if row.payload.get("request_id") == ctx.request.request_id:
            if row.payload.get("passed"):
                return True
            raise StepFailed(str(row.payload.get("reason", "tests_failed")))
    return False


def steps(ctx: StepContext) -> list[StepDef]:
    return [
        StepDef("compute_fingerprint", _run_compute_fingerprint, _reconcile_false),
        StepDef("run_tests", _run_run_tests, _reconcile_false),
        StepDef("trust_checks", _run_trust_checks, _reconcile_false),
        StepDef("record_bench", _run_record_bench, _reconcile_record_bench),
    ]
