"""Fingerprints and the three kinds of evidence (source / build / publish).

fingerprint() == range_tree(): the git tree hash of the commit-tree range —
tracked-in-HEAD paths (`git ls-tree -r HEAD`, so a force-added tracked file
that matches .gitignore is still included) UNION staged/index paths
(`git ls-files --cached`, so a file that is `git add`ed but not yet
committed — including the new name of a staged rename — is not dropped)
UNION untracked, non-ignored paths (`git ls-files --others
--exclude-per-directory=.gitignore`, which honors ONLY in-tree .gitignore
files, never global excludesFile or .git/info/exclude). An excluded path
that is tracked in HEAD keeps its HEAD content (mode + blob, unchanged)
rather than being dropped — excludes must not delete a tracked file from
a commit built off this tree; an excluded path that is NOT tracked in
HEAD is simply omitted, as before. A tracked, non-excluded path that no
longer exists on disk is omitted (a deleted tracked file). Each surviving
path's content is hashed into a temporary index and written as a tree.
Any git failure, a symlink pointing outside the work tree, or a nested
repository raises FingerprintError. Callers treat "no fingerprint" as
"refuse" (never as pass).
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

from engine.infrastructure.gitutil import git_argv
from engine.infrastructure.db.ledger import Event, EvidenceRow, Ledger


class FingerprintError(Exception):
    """Fingerprint could not be computed; treat as refusal."""


def fingerprint(repo: Path, excludes: tuple[str, ...] = ()) -> str:
    """Return a 40-char git tree hash for the commit-tree range of `repo`."""
    return range_tree(Path(repo), excludes)


def range_tree(repo: Path, excludes: tuple[str, ...] = ()) -> str:
    """Build the commit-tree range deterministically and return its tree hash.

    See the module docstring for the exact path union and the excluded-
    tracked-path preservation rule. Each surviving path's content is hashed
    into a fresh temporary index via `update-index --add --cacheinfo`, then
    written as a tree.
    """
    repo = Path(repo)
    _check_tree(repo, excludes)
    env_base = dict(os.environ)
    env_base["GIT_CONFIG_GLOBAL"] = "/dev/null"
    head_entries = _head_tracked_entries(repo, env_base)
    paths = _range_paths(repo, env_base, head_entries)
    with tempfile.TemporaryDirectory() as tmp:
        env = _index_env(env_base, Path(tmp))
        try:
            _build_index(repo, paths, excludes, head_entries, env)
            tree_hash = _write_tree(repo, env)
        except (subprocess.CalledProcessError, OSError) as exc:
            raise FingerprintError(f"git failed computing tree for {repo}: {exc}") from exc
    return tree_hash


def _head_tracked_entries(repo: Path, env_base: dict[str, str]) -> dict[str, tuple[str, str]]:
    """path -> (mode, blob_sha) for every path tracked in HEAD. Empty dict
    if there is no HEAD commit yet."""
    result = subprocess.run(
        git_argv(repo, "ls-tree", "-r", "-z", "HEAD"),
        capture_output=True, env=env_base,
    )
    if result.returncode != 0:
        return {}
    entries: dict[str, tuple[str, str]] = {}
    for line in _split_nul(result.stdout):
        if not line:
            continue
        meta, _, rel = line.partition("\t")
        mode, _type, sha = meta.split(" ")
        entries[rel] = (mode, sha)
    return entries


def _range_paths(repo: Path, env_base: dict[str, str],
                 head_entries: dict[str, tuple[str, str]]) -> list[str]:
    """Union of HEAD-tracked, index/cached (staged) and untracked-non-ignored
    paths, as a sorted, deduplicated list of repo-relative posix strings."""
    try:
        cached_out = subprocess.run(
            git_argv(repo, "ls-files", "-z", "--cached"),
            check=True, capture_output=True, env=env_base,
        ).stdout
        untracked_out = subprocess.run(
            git_argv(repo, "ls-files", "-z", "--others",
             "--exclude-per-directory=.gitignore"),
            check=True, capture_output=True, env=env_base,
        ).stdout
    except (subprocess.CalledProcessError, OSError) as exc:
        raise FingerprintError(f"git failed listing paths for {repo}: {exc}") from exc
    cached = _split_nul(cached_out)
    untracked = _split_nul(untracked_out)
    return sorted({p for p in (*head_entries.keys(), *cached, *untracked) if p})


def _index_env(env_base: dict[str, str], tmp: Path) -> dict[str, str]:
    """Env for a fresh, disposable temporary index rooted under `tmp`."""
    index_path = tmp / "index"
    home_dir = tmp / "home"
    home_dir.mkdir()
    env = dict(env_base)
    env.update({"GIT_INDEX_FILE": str(index_path), "HOME": str(home_dir)})
    return env


def _needs_slow_path(rel: str) -> bool:
    """True for a path byte sequence that would corrupt the batched,
    newline/tab-delimited `hash-object --stdin-paths` and `update-index
    --index-info` formats -- git has no NUL-safe variant of either, so
    such a (pathological, essentially never seen in practice) path
    falls back to the old one-subprocess-per-path handling instead of
    being silently mishandled. Newline/tab break the line-oriented
    format outright; a backslash is git's own quoted-path escape
    character, and a path that STARTS WITH a double quote makes git
    try to parse the whole line as a C-quoted string (verified
    directly: `printf '"q.txt\\n' | git hash-object --stdin-paths`
    fails the entire batch with "line is badly quoted" rather than
    just mishandling that one path) -- excluded here rather than
    risked, in either direction (misinterpretation or an outright
    failure that would otherwise fail every OTHER path in the same
    batch too)."""
    return "\n" in rel or "\t" in rel or "\\" in rel or rel.startswith('"')


def _hash_entries_batch(repo: Path, paths: list[str],
                        env: dict[str, str]) -> dict[str, tuple[str, str]]:
    """path -> (mode, blob_sha) for every REGULAR-file path in `paths`,
    computed with a single `git hash-object --stdin-paths -w` call
    instead of one `git hash-object -w <path>` subprocess per path --
    verified to return the identical sha, in the same order as the
    newline-separated input, as the old per-path call (manual probe:
    both produced the same object for the same file). Never called
    with a symlink path: `git hash-object`, with or without
    --stdin-paths, always DEREFERENCES a symlink and hashes its
    TARGET file's content (verified empirically -- there is no
    --no-follow in this git version) -- the opposite of what a
    symlink's own tree entry must store, so _hash_entry's separate
    readlink-based path remains the only correct way to hash one."""
    if not paths:
        return {}
    stdin_data = "\n".join(paths) + "\n"
    result = subprocess.run(
        git_argv(repo, "hash-object", "--stdin-paths", "-w"),
        input=stdin_data, check=True, capture_output=True, env=env, text=True,
    )
    shas = result.stdout.splitlines()
    if len(shas) != len(paths):
        raise FingerprintError(
            f"hash-object --stdin-paths returned {len(shas)} lines for "
            f"{len(paths)} paths"
        )
    entries: dict[str, tuple[str, str]] = {}
    for rel, sha in zip(paths, shas):
        full = repo / rel
        mode = "100755" if os.access(full, os.X_OK) else "100644"
        entries[rel] = (mode, sha)
    return entries


def _build_index(repo: Path, paths: list[str], excludes: tuple[str, ...],
                 head_entries: dict[str, tuple[str, str]],
                 env: dict[str, str]) -> None:
    """Stage every surviving path into the temporary index named by
    env["GIT_INDEX_FILE"]: current on-disk content normally, or the
    unchanged HEAD blob for an excluded path that is tracked in HEAD (so
    excludes never delete a tracked file from a commit built off this
    tree). Batches the common case (regular files, paths with no
    newline/tab) into one hash-object call and one update-index call
    total, rather than two subprocess calls PER path -- symlinks and
    pathological paths still go through the original one-call-per-path
    sequence, each correctly, just not batched."""
    batch_entries: dict[str, tuple[str, str]] = {}
    slow_rels: list[str] = []
    to_hash: list[str] = []
    for rel in paths:
        if _is_excluded_rel(rel, excludes):
            head_entry = head_entries.get(rel)
            if head_entry is None:
                continue  # excluded and never tracked: omit from the tree
            if _needs_slow_path(rel):
                slow_rels.append(rel)
            else:
                batch_entries[rel] = head_entry
            continue
        full = repo / rel
        if not os.path.lexists(full):
            continue  # deleted tracked file: omit from the tree
        if _needs_slow_path(rel) or full.is_symlink():
            slow_rels.append(rel)
        else:
            to_hash.append(rel)
    if to_hash:
        batch_entries.update(_hash_entries_batch(repo, to_hash, env))
    if batch_entries:
        index_info = "".join(
            f"{mode} {sha} 0\t{rel}\n" for rel, (mode, sha) in batch_entries.items()
        )
        subprocess.run(
            git_argv(repo, "update-index", "--index-info"),
            input=index_info, check=True, capture_output=True, env=env, text=True,
        )
    for rel in slow_rels:
        if _is_excluded_rel(rel, excludes):
            mode, blob_sha = head_entries[rel]
        else:
            mode, blob_sha = _hash_entry(repo, repo / rel, env)
        subprocess.run(
            git_argv(repo, "update-index", "--add",
             "--cacheinfo", f"{mode},{blob_sha},{rel}"),
            check=True, capture_output=True, env=env,
        )


def _write_tree(repo: Path, env: dict[str, str]) -> str:
    """write-tree the temporary index and return the validated 40-char hash."""
    result = subprocess.run(
        git_argv(repo, "write-tree"),
        check=True, capture_output=True, env=env, text=True,
    )
    tree_hash = result.stdout.strip()
    if len(tree_hash) != 40:
        raise FingerprintError(f"unexpected tree hash for {repo}: {tree_hash!r}")
    return tree_hash


def _split_nul(raw: bytes) -> list[str]:
    text = raw.decode("utf-8", "surrogateescape")
    return text.split("\0")


def _hash_entry(repo: Path, full: Path, env: dict[str, str]) -> tuple[str, str]:
    """Return (git mode, blob sha) for a path, writing the blob into the
    repo's object store. Symlinks are hashed as their target string."""
    if full.is_symlink():
        target = os.readlink(full)
        result = subprocess.run(
            git_argv(repo, "hash-object", "-w", "-t", "blob", "--stdin"),
            input=target.encode("utf-8", "surrogateescape"),
            check=True, capture_output=True, env=env,
        )
        return "120000", result.stdout.decode().strip()
    mode = "100755" if os.access(full, os.X_OK) else "100644"
    result = subprocess.run(
        git_argv(repo, "hash-object", "-w", "--", str(full)),
        check=True, capture_output=True, env=env,
    )
    return mode, result.stdout.decode().strip()


def _is_excluded_rel(rel: str, excludes: tuple[str, ...]) -> bool:
    for excl in excludes:
        trimmed = excl.rstrip("/")
        if rel == trimmed or rel.startswith(trimmed + "/"):
            return True
    return False


def _check_tree(repo: Path, excludes: tuple[str, ...]) -> None:
    """Raise FingerprintError on an out-of-tree symlink or a nested .git."""
    if not (repo / ".git").exists():
        raise FingerprintError(f"not a git repository: {repo}")
    repo_real = repo.resolve()
    for dirpath, dirnames, filenames in os.walk(repo, followlinks=False):
        current = Path(dirpath)
        rel = current.relative_to(repo)
        if rel == Path("."):
            dirnames[:] = [d for d in dirnames if d != ".git"]
        elif ".git" in dirnames or ".git" in filenames:
            raise FingerprintError(f"nested git repository under {current}")
        dirnames[:] = [d for d in dirnames if not _is_excluded(rel, d, excludes)]
        for name in list(dirnames) + filenames:
            if _is_excluded(rel, name, excludes):
                continue
            full = current / name
            if full.is_symlink():
                target_real = full.resolve()
                if not _is_within(target_real, repo_real):
                    raise FingerprintError(f"symlink escapes repo: {full}")


def _is_excluded(rel: Path, name: str, excludes: tuple[str, ...]) -> bool:
    candidate = (rel / name) if rel != Path(".") else Path(name)
    candidate_str = candidate.as_posix()
    for excl in excludes:
        trimmed = excl.rstrip("/")
        if candidate_str == trimmed or candidate_str.startswith(trimmed + "/"):
            return True
    return False


def _is_within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


class TreeExportError(Exception):
    """export_tree refuses a tree entry shaped in a way git itself
    never actually writes (an absolute path, or one containing
    "..") -- never trusted regardless, since this is reading git's
    OWN object database, not external input, but a caller must still
    never silently skip or mis-place such an entry."""


def export_tree(repo: Path, fp: str, dest: Path) -> None:
    """Extracts tree object `fp` -- already written into `repo`'s own
    git object database by fingerprint()/range_tree(), never a fresh
    read of the live working tree -- into `dest` (which must already
    exist as an empty directory; the caller owns creating/clearing it),
    entry by entry via `git ls-tree` + `git cat-file blob` (both
    through engine.infrastructure.gitutil, never a raw git invocation), rather than
    `git archive`. `git archive` applies `export-ignore`/`export-subst`
    .gitattributes: an `export-ignore`'d path can be OMITTED from the
    archive entirely even though the fingerprinted tree itself contains
    it (so a caller that then certifies "this is what fp means" would
    never actually see a file the recorded evidence says was part of
    it), and an `export-subst`'d path has its content REWRITTEN (a
    `$Format:...$` placeholder expanded) rather than exported byte-for-
    byte. Neither behavior is appropriate for a snapshot that must
    match the fingerprinted tree exactly: `ls-tree -r` lists every blob
    (and gitlink) entry regardless of attributes, and `cat-file blob`
    returns that exact entry's bytes, unfiltered. A gitlink entry (mode
    160000, a submodule reference) is skipped outright -- there is no
    blob to read for it, and this engine does not manage submodules.

    Moved here (from engine.application.steps.review, the reviewer's own snapshot
    export, which still calls this) so engine.application.steps.suite_run can reuse
    the exact same exporter for engine.application.acceptance's and its own
    disposable snapshots, so none of them can miss an export-ignored
    file."""
    listing = subprocess.run(
        git_argv(repo, "ls-tree", "-r", "-z", fp),
        check=True, capture_output=True,
    ).stdout
    for raw_entry in listing.split(b"\0"):
        if not raw_entry:
            continue
        meta, _, rel_bytes = raw_entry.partition(b"\t")
        mode_bytes, obj_type_bytes, oid_bytes = meta.split(b" ")
        mode = mode_bytes.decode("ascii")
        obj_type = obj_type_bytes.decode("ascii")
        if obj_type != "blob":
            continue  # gitlink (submodule, type "commit"); never a blob to read
        oid = oid_bytes.decode("ascii")
        rel = rel_bytes.decode("utf-8", "surrogateescape")
        rel_path = Path(rel)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            # git itself never writes a tree entry shaped like this;
            # fail closed rather than trust it regardless.
            raise TreeExportError(f"unsafe tree entry path: {rel!r}")
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        blob = subprocess.run(
            git_argv(repo, "cat-file", "blob", oid),
            check=True, capture_output=True,
        ).stdout
        if mode == "120000":
            target.symlink_to(blob.decode("utf-8", "surrogateescape"))
        else:
            target.write_bytes(blob)
            if mode == "100755":
                target.chmod(0o755)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record(ledger: Ledger, project: str, attempt_id: str, kind: str,
           fp: str, artifact_hash: str | None = None,
           payload: dict[str, object] | None = None) -> int:
    """Append evidence.recorded; returns seq."""
    return ledger.append(Event(
        project=project, kind="evidence.recorded", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={
            "kind": kind, "fingerprint": fp, "artifact_hash": artifact_hash,
            "payload": payload or {},
        },
    ))


def latest(ledger: Ledger, attempt_id: str, kind: str) -> EvidenceRow | None:
    """Newest non-stale evidence of that kind, or None."""
    rows = [row for row in ledger.evidence(attempt_id, kind) if not row.stale]
    return rows[-1] if rows else None


def invalidate_after(ledger: Ledger, project: str, attempt_id: str,
                     kinds: tuple[str, ...]) -> None:
    """Mark all evidence of those kinds stale (e.g. after a dependency,
    scripts or test-config change)."""
    ledger.append(Event(
        project=project, kind="evidence.stale", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"attempt_id": attempt_id, "kinds": list(kinds)},
    ))


def three_match_reason(ledger: Ledger, attempt_id: str,
                       now_fp: str) -> tuple[bool, str]:
    """Like three_match(), but distinguishes WHICH of the three
    conditions failed, so a caller (engine.application.steps.commit) can report a
    specific reason instead of one generic "fingerprints_differ" for
    every case -- a review that never passed used to surface
    identically to a review/bench/commit tree that genuinely moved."""
    review = latest(ledger, attempt_id, "review")
    if review is None or review.payload.get("passed") is not True:
        return False, "review_not_passed"
    bench = latest(ledger, attempt_id, "bench")
    if bench is None or bench.payload.get("passed") is not True:
        return False, "bench_not_passed"
    if not (review.fingerprint == bench.fingerprint == now_fp):
        return False, "fingerprints_differ"
    return True, "ok"


def three_match(ledger: Ledger, attempt_id: str, now_fp: str) -> bool:
    """True iff latest review, latest bench and now_fp are all equal, neither
    review nor bench is missing/stale, AND both carry payload {"passed":
    True} — a review or bench row whose payload omits "passed" or sets it
    to anything other than True counts as not passed (fail closed)."""
    ok, _reason = three_match_reason(ledger, attempt_id, now_fp)
    return ok
