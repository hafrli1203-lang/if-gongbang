"""Fast-forward a project's configured master repo to the released
commit, then drive its deploy through the existing publish request/
approval machinery.

This module gives:
  - the git primitives engine.steps.publish's advance_master step uses
    (is_fast_forwardable, master_dirty_tracked_paths, merge_range_paths,
    ff_merge), all routed through engine.gitutil so a tampered
    master/.git/config can never redirect an engine-issued git call;
  - prepare(): submits this project's publish request for its own
    repo's current HEAD and builds the owner-facing approval prompt;
  - approve_and_run(): parses the owner's typed approval text, grants
    it, and drives the Processor until that request leaves
    queued/running.

Every git call here reads/writes the REAL master_repo on disk (never a
disposable worker copy), so it always goes through engine.gitutil.git_argv
(core.fsmonitor/hooksPath disabled) -- same reasoning as
engine.steps.open_job and engine.steps.publish's own HEAD checks.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from engine.approvals import ApprovalError, grant, parse_owner_message
from engine.config import EngineConfig
from engine.gitutil import git_argv
from engine.ledger import Ledger
from engine.processor import Fakes, Processor, submit


def head_sha(repo: Path) -> str:
    """git rev-parse HEAD of `repo` (engine.gitutil-guarded). Used both
    for the project's own repo (the artifact sha a publish request is
    submitted for) and for master_repo (its current fast-forward base)."""
    result = subprocess.run(
        git_argv(repo, "rev-parse", "HEAD"),
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


def prepare(config: EngineConfig, ledger: Ledger, project: str,
            attempt_id: str) -> tuple[str, dict[str, object]]:
    """Submit a publish request for this project's current repo HEAD
    (artifact_path="@HEAD", artifact_hash=that HEAD sha) and return
    (request_id, summary) -- summary carries the exact text the owner
    must type (parse_owner_message's required shape, "승인
    R-xxxxxxxxxxxxxxxx") under summary["owner_text"], plus the commit
    list/diff/target/hash for display. Raises ValueError if this
    project has no `release` configured -- release.prepare only applies
    to a project on that track, not every publish request."""
    project_config = config.projects[project]
    release_config = project_config.release
    if release_config is None:
        raise ValueError(f"project {project!r} has no release configured")
    sha = head_sha(project_config.repo)
    payload = {"target": release_config.target, "artifact_path": "@HEAD",
               "artifact_hash": sha}
    request_id = submit(ledger, project, attempt_id, "publish", payload, actor_id="owner")
    summary = build_summary(project_config.repo, release_config.master_repo, sha,
                            release_config.target)
    summary["request_id"] = request_id
    summary["owner_text"] = f"승인 {request_id}"
    return request_id, summary


def approve_and_run(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                    request_id: str, owner_text: str,
                    max_iterations: int = 50) -> dict[str, object]:
    """Parse `owner_text` (must be exactly "승인 R-..." per
    approvals.parse_owner_message), refuse if it names a different
    request than `request_id`, grant the approval, then drive a fresh
    Processor's run_once in a bounded loop until this request leaves
    queued/running (done/failed/unknown). Never calls the real deploy
    script itself -- that only happens if `fakes.publisher` is wired to
    a real ShellPublisher by the caller."""
    parsed = parse_owner_message(owner_text)
    if parsed is None:
        return {"status": "rejected", "reason": "owner_text_malformed",
                "request_id": request_id}
    if parsed != request_id:
        return {"status": "rejected", "reason": "owner_text_wrong_request",
                "request_id": request_id}
    try:
        grant(ledger, project, request_id, ttl_sec=config.approval_ttl_sec)
    except ApprovalError as exc:
        return {"status": "rejected", "reason": str(exc), "request_id": request_id}

    processor = Processor(config, ledger, fakes)
    processor.register_defaults()
    request = ledger.request(request_id)
    for _ in range(max_iterations):
        if request.status not in ("queued", "running"):
            break
        processor.run_once(project)
        request = ledger.request(request_id)
    return {"status": request.status, "result": request.result,
            "request_id": request_id}


__all__ = [
    "head_sha", "is_fast_forwardable", "master_dirty_tracked_paths",
    "master_untracked_paths", "master_untracked_or_ignored_paths",
    "master_is_clean", "merge_range_paths", "incoming_added_paths",
    "overlapping_paths", "ff_merge", "build_summary",
    "prepare", "approve_and_run",
]
