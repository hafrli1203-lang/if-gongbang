"""Git primitives for fast-forwarding a project's configured master repo
to the released commit (engine.application.steps.publish's own advance_master step),
plus the owner-facing summary engine.application.release.prepare() builds from them.

Every git call here reads/writes the REAL master_repo on disk (never a
disposable worker copy), so it always goes through engine.infrastructure.gitutil.git_argv
(core.fsmonitor/hooksPath disabled) -- same reasoning as
engine.application.steps.open_job and engine.application.steps.publish's own HEAD checks.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from engine.infrastructure.gitutil import git_argv


def head_sha(repo: Path) -> str:
    """git rev-parse HEAD of `repo` (engine.infrastructure.gitutil-guarded). Used both
    for the project's own repo (the artifact sha a publish request is
    submitted for) and for master_repo (its current fast-forward base)."""
    result = subprocess.run(
        git_argv(repo, "rev-parse", "HEAD"),
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def commit_tree_hash(repo: Path, sha: str) -> str:
    """git's own content-addressed tree hash for commit `sha` (`git
    rev-parse <sha>^{tree}`) -- a real, git-computed digest of exactly
    what that commit's tree contains, used (engine.application.steps.publish) as
    the pre-dispatch measurement of a release's artifact for a project
    with no configured build output of its own (ProjectConfig.
    artifact_paths empty -- a source-only deploy). Deliberately NOT
    `git archive`, whose export-ignore/export-subst .gitattributes can
    omit or rewrite a file entirely -- same reasoning engine.infrastructure.evidence.
    export_tree's own docstring already established for the identical
    choice elsewhere in this codebase. Raises subprocess.
    CalledProcessError if `sha` is not a commit known to `repo`."""
    result = subprocess.run(
        git_argv(repo, "rev-parse", f"{sha}^{{tree}}"),
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _object_known(repo: Path, sha: str) -> bool:
    """True iff `repo`'s own object database already has `sha` -- a
    fast-forward (or even a `git diff`/`log` against it) is otherwise
    impossible, not merely "not yet an ancestor"."""
    result = subprocess.run(git_argv(repo, "cat-file", "-e", sha), capture_output=True)
    return result.returncode == 0


def is_fast_forwardable(master_repo: Path, sha: str) -> bool:
    """True iff master_repo's current HEAD is an ancestor of `sha` --
    `git merge --ff-only sha` can succeed there without rewriting
    master's own history. False (never raised) for every other outcome:
    unrelated history, or `sha` entirely unknown to master_repo's own
    object database."""
    if not _object_known(master_repo, sha):
        return False
    result = subprocess.run(
        git_argv(master_repo, "merge-base", "--is-ancestor", "HEAD", sha),
        capture_output=True,
    )
    return result.returncode == 0


def _status_paths(master_repo: Path, *, include_ignored: bool) -> dict[str, str]:
    """Internal: relative path -> its two-letter `git status --porcelain`
    code, for every entry `git status` reports (plus, when
    `include_ignored`, every gitignored path too -- `--ignored`).
    Shared by every master_repo status helper below so each only
    differs in which status codes it keeps."""
    args = ["status", "--porcelain", "-z", "--untracked-files=all"]
    if include_ignored:
        args.append("--ignored")
    out = subprocess.run(git_argv(master_repo, *args), check=True, capture_output=True).stdout
    tokens = out.decode("utf-8", "surrogateescape").split("\0")
    paths: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if not entry:
            continue
        status, rel = entry[:2], entry[3:]
        paths[rel] = status
        if status[0] in ("R", "C") and i < len(tokens) and tokens[i]:
            paths[tokens[i]] = status
            i += 1
    return paths


def master_dirty_tracked_paths(master_repo: Path) -> tuple[str, ...]:
    """Relative paths of master_repo's own tracked, uncommitted
    modifications (staged or unstaged). Untracked files ('??' entries)
    are deliberately excluded: an untracked file can never collide with
    a path the incoming commits touch -- a fast-forward just adds it as
    a newly tracked file, nothing for this overlap check to catch."""
    return tuple(sorted(
        rel for rel, status in _status_paths(master_repo, include_ignored=False).items()
        if status != "??"
    ))


def master_untracked_paths(master_repo: Path) -> tuple[str, ...]:
    """Relative paths of files in master_repo's working tree that are
    untracked and NOT gitignored -- exactly what a plain `git status`
    (no --ignored) already shows as '??'."""
    return tuple(sorted(
        rel for rel, status in _status_paths(master_repo, include_ignored=False).items()
        if status == "??"
    ))


def master_untracked_or_ignored_paths(master_repo: Path) -> tuple[str, ...]:
    """Relative paths of every file in master_repo's working tree that
    git does NOT track -- both plain untracked ('??') and gitignored
    ('!!', only visible with --ignored) paths. These are exactly the
    paths a fast-forward's own --no-overwrite-ignore protects from
    being silently replaced, so advance_master cross-checks them
    against what the incoming commits would add."""
    return tuple(sorted(
        rel for rel, status in _status_paths(master_repo, include_ignored=True).items()
        if status in ("??", "!!")
    ))


def master_is_clean(master_repo: Path) -> bool:
    """True iff master_repo has no tracked modification AND no
    untracked-but-not-ignored file -- the precondition the deploy
    command's own working tree must satisfy, since it builds and
    deploys directly from whatever is sitting in master_repo. A
    gitignored file (a build artifact, a local .env, ...) is NOT
    required to be absent -- only tracked dirt and untracked-and-not-
    ignored dirt make master_is_clean False."""
    return (not master_dirty_tracked_paths(master_repo)
            and not master_untracked_paths(master_repo))


def merge_range_paths(master_repo: Path, sha: str) -> tuple[str, ...]:
    """Relative paths touched between master_repo's current HEAD and
    `sha` -- the commits a fast-forward would bring in. Requires `sha`
    to already be a known object in master_repo (see _object_known);
    callers check is_fast_forwardable/_object_known first."""
    result = subprocess.run(
        git_argv(master_repo, "diff", "--name-only", "HEAD", sha),
        check=True, capture_output=True, text=True,
    )
    return tuple(sorted(p for p in result.stdout.splitlines() if p))


def incoming_added_paths(master_repo: Path, sha: str) -> tuple[str, ...]:
    """Relative paths the incoming commits (master_repo's current HEAD
    .. sha) ADD that master's own tree does not already have -- the
    ones a fast-forward's checkout would newly create on disk, as
    opposed to a path master already tracks that the merge only
    modifies. Requires `sha` to already be a known object in
    master_repo, same as merge_range_paths."""
    result = subprocess.run(
        git_argv(master_repo, "diff", "--name-only", "--diff-filter=A", "HEAD", sha),
        check=True, capture_output=True, text=True,
    )
    return tuple(sorted(p for p in result.stdout.splitlines() if p))


def overlapping_paths(a: tuple[str, ...], b: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(set(a) & set(b)))


def ff_merge(master_repo: Path, sha: str) -> None:
    """git merge --ff-only --no-overwrite-ignore sha in master_repo.
    --no-overwrite-ignore (git's own default is the opposite,
    --overwrite-ignore) stops the merge from silently replacing a
    gitignored local file that happens to occupy a path the incoming
    commits add -- advance_master additionally checks
    incoming_added_paths against master_untracked_or_ignored_paths
    itself and refuses ("incoming_path_occupied") before ever reaching
    this call, so this flag is defense in depth, not the only guard.
    Raises subprocess.CalledProcessError on any git-level refusal.
    Callers are expected to have already checked
    is_fast_forwardable/master_dirty_tracked_paths/merge_range_paths
    for an overlap and refused (StepFailed) before ever reaching this
    call -- this function does not re-check either."""
    subprocess.run(
        git_argv(master_repo, "merge", "--ff-only", "--no-overwrite-ignore", sha),
        check=True, capture_output=True, text=True,
    )


def _commit_log(repo: Path, base: str, sha: str) -> list[dict[str, str]]:
    """Commits in `repo` between `base` and `sha`. `base` is master_repo's
    HEAD at prepare() time -- if master has since diverged with its own
    commit, `repo` (the project's own history) may never have seen that
    commit at all, so this returns [] rather than raising; it is purely
    an informational list for the owner-facing prompt, never a
    correctness check (that is is_fast_forwardable's job)."""
    if base == sha or not _object_known(repo, base):
        return []
    result = subprocess.run(
        git_argv(repo, "log", "--format=%H %s", f"{base}..{sha}"),
        check=True, capture_output=True, text=True,
    )
    commits: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        head, _, subject = line.partition(" ")
        commits.append({"sha": head, "subject": subject})
    return commits


def build_summary(project_repo: Path, master_repo: Path, sha: str,
                   target: str) -> dict[str, object]:
    """The commit list / diff summary / target / hash a `gb2 ship`-style
    prompt shows the owner before they type the approval text.
    `diff_paths`/`fast_forward_ok` reflect master_repo's state
    AT THE TIME this is called -- they are advisory for the prompt, not
    a substitute for advance_master's own checks at approval time
    (master can move between prepare() and the owner actually typing
    the approval)."""
    base = head_sha(master_repo)
    commits = _commit_log(project_repo, base, sha)
    ff_ok = is_fast_forwardable(master_repo, sha)
    diff_paths = merge_range_paths(master_repo, sha) if _object_known(master_repo, sha) else ()
    return {
        "target": target, "artifact_hash": sha, "base_head": base,
        "commits": commits, "diff_paths": list(diff_paths),
        "fast_forward_ok": ff_ok,
    }


__all__ = [
    "head_sha", "commit_tree_hash", "is_fast_forwardable", "master_dirty_tracked_paths",
    "master_untracked_paths", "master_untracked_or_ignored_paths",
    "master_is_clean", "merge_range_paths", "incoming_added_paths",
    "overlapping_paths", "ff_merge", "build_summary",
]
