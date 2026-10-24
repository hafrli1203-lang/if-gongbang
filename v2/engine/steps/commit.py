"""commit — processor-only commit after review/bench/now fingerprints agree.

compute_fingerprint  fp = evidence.fingerprint(repo); journal {fp}
verify_three         evidence.three_match_reason(ledger, attempt, fp); failed
                     with the specific reason ("review_not_passed",
                     "bench_not_passed" or "fingerprints_differ")
git_commit           tree = evidence.range_tree(repo, excludes) -- the same
                     plumbing fingerprint()/range_tree() use, not an
                     independently-built temp index -- so the committed
                     tree is exactly the fingerprinted range, never a
                     near-miss built by different git flags; refuses
                     (tree_changed) if tree != fp; commit with message
                     "{message}\n\nattempt: {attempt_id}\nfingerprint: {fp}"
                     reconcile: HEAD message contains both lines AND
                     HEAD^{tree} == fp -> done
record_commit        append commit.recorded {sha, fp}
report               report.write_report / report.exists
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from engine.evidence import FingerprintError, fingerprint, range_tree, three_match_reason
from engine.gitutil import git_argv
from engine.ledger import Event
from engine.processor import StepContext, StepDef, StepFailed
from engine.steps.report import exists as report_exists
from engine.steps.report import write_report

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "processor", "GIT_AUTHOR_EMAIL": "processor@local",
    "GIT_COMMITTER_NAME": "processor", "GIT_COMMITTER_EMAIL": "processor@local",
}


def _project_config(ctx: StepContext):
    return ctx.config.projects[ctx.project]


def _repo(ctx: StepContext) -> Path:
    return _project_config(ctx).repo


def _excludes(ctx: StepContext) -> tuple[str, ...]:
    return _project_config(ctx).excludes


def _journal_payload(ctx: StepContext, step_name: str) -> dict[str, object]:
    row = ctx.ledger.steps(ctx.request.request_id).get(step_name)
    if row is None:
        raise StepFailed(f"missing_step:{step_name}")
    return row.payload


def _rev_parse_head(repo: Path) -> str:
    try:
        return subprocess.run(
            git_argv(repo, "rev-parse", "HEAD"),
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError) as exc:
        raise StepFailed("no_fingerprint") from exc


def _run_compute_fingerprint(ctx: StepContext) -> dict[str, object]:
    try:
        fp = fingerprint(_repo(ctx), _excludes(ctx))
    except FingerprintError as exc:
        raise StepFailed("no_fingerprint") from exc
    head = _rev_parse_head(_repo(ctx))
    return {"fp": fp, "head": head}


def _run_verify_three(ctx: StepContext) -> dict[str, object]:
    fp = str(_journal_payload(ctx, "compute_fingerprint")["fp"])
    ok, reason = three_match_reason(ctx.ledger, ctx.attempt_id, fp)
    if not ok:
        raise StepFailed(reason)
    return {"fp": fp}


def _build_range_tree(ctx: StepContext, repo: Path, fp: str) -> str:
    # Build the tree with the exact same plumbing fingerprint()/range_tree()
    # use (ls-tree HEAD + in-tree-.gitignore-only untracked listing), rather
    # than this step's own `add -A` + excludes temp index: two different
    # ways of deciding "what's in the range" can disagree on edge cases
    # (force-added-but-ignored files, nested .gitignore rules, ...), which
    # would make the tree != fp check below fire on a repo nobody touched.
    try:
        tree = range_tree(repo, _excludes(ctx))
    except FingerprintError as exc:
        raise StepFailed("git_commit_failed") from exc
    if tree != fp:
        # The tree changed between verify_three (which checked fp against
        # review/bench) and this step's own range_tree computation: a
        # worker edited the repo in between. Commit nothing rather than
        # commit an unreviewed tree under the old fingerprint.
        raise StepFailed("tree_changed")
    return tree


def _commit_tree_onto(repo: Path, tree: str, parent: str, message: str) -> str:
    """commit-tree `tree` as a child of `parent`, then move HEAD onto it
    with a compare-and-swap (`update-ref HEAD <new> <old>`): `parent` was
    just re-read fresh, right before this call, so the CAS can only fail
    if HEAD moved again in the brief window between that read and here —
    a race this step cannot close any further, only shrink."""
    env = dict(os.environ)
    env.update(_GIT_IDENTITY)
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    try:
        commit_sha = subprocess.run(
            git_argv(repo, "commit-tree", tree, "-p", parent, "-m", message),
            check=True, capture_output=True, env=env, text=True,
        ).stdout.strip()
        subprocess.run(
            git_argv(repo, "update-ref", "HEAD", commit_sha, parent),
            check=True, capture_output=True, env=env,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise StepFailed("git_commit_failed") from exc
    return commit_sha


def _run_git_commit(ctx: StepContext) -> dict[str, object]:
    fp = str(_journal_payload(ctx, "compute_fingerprint")["fp"])
    recorded_head = str(_journal_payload(ctx, "compute_fingerprint")["head"])
    message = str(ctx.request.payload.get("message", ""))
    full_message = f"{message}\n\nattempt: {ctx.attempt_id}\nfingerprint: {fp}"
    repo = _repo(ctx)

    # Re-read HEAD fresh, right before building anything: if it no longer
    # matches what compute_fingerprint saw, something else moved HEAD in
    # between (e.g. an empty commit, which the tree_changed check below
    # cannot catch on its own since it doesn't change the tree at all).
    current_head = _rev_parse_head(repo)
    if current_head != recorded_head:
        raise StepFailed("head_moved")

    tree = _build_range_tree(ctx, repo, fp)
    commit_sha = _commit_tree_onto(repo, tree, current_head, full_message)
    # Sync the real working index to the new HEAD (range_tree/commit-tree
    # never touched the repo's own index, so it still reflects the
    # pre-commit tree and `git status` would show spurious changes).
    subprocess.run(git_argv(repo, "reset", "-q"), check=True, capture_output=True)
    return {"sha": commit_sha, "fp": fp}


def _reconcile_git_commit(ctx: StepContext) -> bool:
    fp = str(_journal_payload(ctx, "compute_fingerprint")["fp"])
    repo = _repo(ctx)
    try:
        message = subprocess.run(
            git_argv(repo, "log", "-1", "--format=%B"),
            check=True, capture_output=True, text=True,
        ).stdout
        head_tree = subprocess.run(
            git_argv(repo, "rev-parse", "HEAD^{tree}"),
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        return False
    return (
        f"attempt: {ctx.attempt_id}" in message
        and f"fingerprint: {fp}" in message
        and head_tree == fp
    )


def _run_record_commit(ctx: StepContext) -> dict[str, object]:
    # Read HEAD directly rather than git_commit's journaled payload: if the
    # processor crashed between git_commit's run() and its journal 'done',
    # the resumed run marks it done via reconcile without ever re-running
    # it, so the journaled payload for that step can be empty.
    sha = subprocess.run(
        git_argv(_repo(ctx), "rev-parse", "HEAD"),
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    fp = str(_journal_payload(ctx, "compute_fingerprint")["fp"])
    ctx.ledger.append(Event(
        project=ctx.project, kind="commit.recorded", actor_kind="processor",
        actor_id="processor", attempt_id=ctx.attempt_id,
        payload={"request_id": ctx.request.request_id, "sha": sha, "fp": fp},
    ))
    return {"sha": sha, "fp": fp}


def _reconcile_record_commit(ctx: StepContext) -> bool:
    """True iff a commit.recorded event for this request already exists —
    a crash between record_commit's run() and its journal 'done' must not
    cause a second commit.recorded event on resume."""
    return any(
        event.payload.get("request_id") == ctx.request.request_id
        for _seq, event in ctx.ledger.events(ctx.project, "commit.recorded")
    )


def steps(ctx: StepContext) -> list[StepDef]:
    return [
        StepDef("compute_fingerprint", _run_compute_fingerprint, lambda ctx: False),
        StepDef("verify_three", _run_verify_three, lambda ctx: False),
        StepDef("git_commit", _run_git_commit, _reconcile_git_commit),
        StepDef("record_commit", _run_record_commit, _reconcile_record_commit),
        StepDef("report", write_report, report_exists),
    ]
