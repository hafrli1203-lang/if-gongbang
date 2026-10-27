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
                     HEAD^{tree} == fp -> done. Checked UNCONDITIONALLY
                     at the top of run() too (_head_already_commits),
                     not only via reconcile: a genuinely different
                     request_id for the same (attempt, fp) -- never
                     'started' before, so the processor never consults
                     this step's reconcile for it at all -- must still
                     recognize the effect already happened and skip
                     committing again, rather than producing a second
                     real `git commit` for one task's one change (the
                     T6 crash-matrix's own found bug).
record_commit        append commit.recorded {sha, fp} -- skipped (returns
                     the existing {sha, fp} instead) if a commit.recorded
                     for this exact (sha, fp) already exists under ANY
                     request_id (_existing_commit_record), the same
                     any-request_id idempotency git_commit provides one
                     step earlier.
report               report.write_report / report.exists
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from engine.infrastructure.evidence import (
    FingerprintError,
    fingerprint,
    range_tree,
    three_match_reason,
    with_file_deps,
)
from engine.infrastructure.gitutil import git_argv
from engine.domain.records import Event
from engine.application.processor import StepContext, StepDef, StepFailed
from engine.application.steps.report import exists as report_exists
from engine.application.steps.report import write_report

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


def _file_dep_targets(ctx: StepContext) -> tuple[tuple[str, str], ...]:
    """Same role as review._file_dep_targets/suite_run._file_dep_targets:
    feeds evidence.with_file_deps so verify_three's three_match check
    compares against the SAME kind of evidence fingerprint review/bench
    now record (a review finding, -- `fp` itself (this step's own
    journaled plain git tree hash) is untouched and keeps being used,
    unchanged, for _build_range_tree's tree==fp check, the commit
    message, _head_already_commits and record_commit: all of those
    build or verify the REAL git tree/commit, which must never be
    mixed with file-dep content (module docstring; close.py's own
    fingerprint(repo)==HEAD^{tree} check elsewhere relies on the same
    plain-tree invariant and is untouched by this)."""
    return _project_config(ctx).file_dep_targets


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
    # a review finding: `fp` stays the plain git tree hash every
    # OTHER step in this file (git_commit/record_commit/the commit
    # message/_head_already_commits) keeps using unchanged; `evidence_fp`
    # is the separate, kit-content-aware value verify_three compares
    # against review/bench's own recorded evidence fingerprints -- see
    # _file_dep_targets' own docstring for why these must never be the
    # same value here.
    evidence_fp = with_file_deps(fp, _file_dep_targets(ctx))
    return {"fp": fp, "evidence_fp": evidence_fp, "head": head}


def _run_verify_three(ctx: StepContext) -> dict[str, object]:
    fp = str(_journal_payload(ctx, "compute_fingerprint")["fp"])
    evidence_fp = str(_journal_payload(ctx, "compute_fingerprint")["evidence_fp"])
    ok, reason = three_match_reason(ctx.ledger, ctx.attempt_id, evidence_fp)
    if not ok:
        raise StepFailed(reason)
    return {"fp": fp, "evidence_fp": evidence_fp}


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


def _head_already_commits(ctx: StepContext, fp: str) -> str | None:
    """HEAD's own sha iff HEAD's commit message already carries THIS
    (attempt_id, fp) pair and HEAD^{tree} already equals fp -- i.e. the
    commit effect this step is about to perform has ALREADY happened.
    None otherwise (including on any git failure -- an attempt to read
    "has this already happened" that cannot even read HEAD has not
    established that it has).

    This is deliberately NOT scoped to the current request_id: every
    crash point in this stage must be recognizable from observable
    state ("검사한 바이트를 그대로 실행한다" -- execute exactly the bytes
    that were checked), and engine.application.processor's own per-request
    reconcile dispatch (engine.application.processor.Processor._run_steps: "done
    건너뛰기 / started는 reconcile / 없으면 실행") only ever calls a
    step's reconcile for a request whose OWN journal already has a
    'started' row for that step -- never for a request that has not
    run this step before. A genuinely different request_id for the
    SAME task's SAME (attempt, content) -- e.g. engine.application.pipeline's
    resume path resubmitting 'commit' for a task it (wrongly) believes
    was never committed, this codebase's own T6 crash-matrix finding
    (docs/flow-map.md; tests/acceptance/test_t6_crash_matrix.py) -- has
    no 'started' row to reconcile against at all; its step function
    would simply RUN, unconditionally, producing a second real `git
    commit` for one task's one change. Checking this directly inside
    _run_git_commit's own body (not only via _reconcile_git_commit)
    closes that gap regardless of which request_id reaches it, or at
    which of this stage's crash points the previous attempt stopped."""
    repo = _repo(ctx)
    try:
        message = subprocess.run(
            git_argv(repo, "log", "-1", "--format=%B"),
            check=True, capture_output=True, text=True,
        ).stdout
        head_sha = subprocess.run(
            git_argv(repo, "rev-parse", "HEAD"),
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        head_tree = subprocess.run(
            git_argv(repo, "rev-parse", "HEAD^{tree}"),
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        return None
    if (f"attempt: {ctx.attempt_id}" in message
            and f"fingerprint: {fp}" in message
            and head_tree == fp):
        return head_sha
    return None


def _run_git_commit(ctx: StepContext) -> dict[str, object]:
    fp = str(_journal_payload(ctx, "compute_fingerprint")["fp"])
    recorded_head = str(_journal_payload(ctx, "compute_fingerprint")["head"])
    message = str(ctx.request.payload.get("message", ""))
    full_message = f"{message}\n\nattempt: {ctx.attempt_id}\nfingerprint: {fp}"
    repo = _repo(ctx)

    # This exact commit effect already happened -- by this very request
    # on an earlier crash-resume, or by an unrelated, now-redundant
    # request for the same task within the same attempt (see
    # _head_already_commits's own docstring). Nothing left to do: no
    # second `git commit`, and no head_moved/tree_changed check either
    # -- both compare against what THIS request's own compute_fingerprint
    # saw, which is moot once the effect it was building towards is
    # already sitting at HEAD.
    already = _head_already_commits(ctx, fp)
    if already is not None:
        return {"sha": already, "fp": fp}

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
    return _head_already_commits(ctx, fp) is not None


def _existing_commit_record(ctx: StepContext, sha: str, fp: str) -> dict[str, object] | None:
    """{"sha": sha, "fp": fp} iff a commit.recorded event already exists
    for this EXACT (sha, fp) pair, regardless of which request_id wrote
    it -- the record_commit half of the same observable-state check
    _head_already_commits does for git_commit (module docstring): a
    second, different request_id reaching record_commit for a commit
    git_commit itself recognized as already-done (same sha) must not
    still append a second commit.recorded event for it."""
    for _seq, event in ctx.ledger.events(ctx.project, "commit.recorded"):
        if event.payload.get("sha") == sha and event.payload.get("fp") == fp:
            return {"sha": sha, "fp": fp}
    return None


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
    existing = _existing_commit_record(ctx, sha, fp)
    if existing is not None:
        return existing
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
