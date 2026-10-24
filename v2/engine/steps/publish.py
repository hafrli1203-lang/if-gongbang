"""publish — independent publisher with bound approval and artifact hash.

check_approval   approvals.check(ledger, request) else failed
check_artifact   sha256_file(repo/artifact_path) == payload.artifact_hash
                 AND == approval.artifact_hash else failed. Special form
                 artifact_path="@HEAD": artifact_hash must be a full
                 40-hex commit sha; checks `git rev-parse HEAD` ==
                 artifact_hash (StepFailed("artifact_mismatch")) AND the
                 working tree is clean, excludes applied
                 (StepFailed("tree_dirty")) -- see _check_head_artifact.
                 approvals.check/grant bind to this sha exactly the way
                 they bind to a file hash (they only ever compare the
                 string value, never interpret it).
advance_master   ONLY when the project has `release` configured
                 (engine.config.ReleaseConfig) -- steps() inserts this
                 right after check_artifact in that case, otherwise the
                 step list is exactly what it was before this module
                 gained release support. Requires artifact_path="@HEAD"
                 (StepFailed("release_requires_head_artifact") otherwise
                 -- a release project's publish payload is always built
                 by engine.release.prepare, which always uses @HEAD).
                 Re-checks the consumed approval's binding and expiry
                 (StepFailed("approval_invalid_before_advance_master")
                 -- check_approval ran earlier and is never re-run once
                 done, so a crash-and-resume gap between it and this
                 step must not let an expired/rebound approval through
                 unnoticed). Refuses ("master_not_clean") unless
                 master_repo has no tracked modification and no
                 untracked-and-not-ignored file -- the deploy command
                 builds directly from that shared working tree.
                 Fast-forwards release.master_repo to the approved sha:
                 refuses ("not_fast_forward") unless master_repo's own
                 HEAD is an ancestor of the sha, refuses
                 ("incoming_path_occupied") if any path the incoming
                 commits ADD is currently occupied by an untracked or
                 gitignored file in master_repo (git's own merge
                 defaults to silently overwriting an ignored file at
                 such a path), else runs `git -C master_repo merge
                 --ff-only --no-overwrite-ignore <sha>` (engine.release,
                 via engine.gitutil).
record_source    evidence.record(kind='source', ...) before any publisher
                 effect, so a git failure here fails closed with nothing
                 published (fingerprinting after publication would mean a
                 git failure is reported as failed despite the external
                 effect having already happened)
call_publisher   re-verifies the artifact hash and the used approval's
                 fields/expiry immediately before dispatch (check_approval
                 and check_artifact are journaled 'done' well before this
                 point and are never re-run once done, so a crash-and-
                 resume gap between them and this step must not let an
                 artifact edit or an approval's expiry slip through
                 unnoticed) -> StepFailed, no publisher call made. When
                 release is configured, ALSO re-verifies master_repo's
                 HEAD still equals the sha advance_master already
                 fast-forwarded it to (StepFailed("master_not_advanced")
                 if not) AND that master_repo is still clean
                 (StepFailed("master_not_clean") if not -- the same
                 precondition advance_master already checked, re-checked
                 here since the deploy command is about to build
                 directly from that working tree) -- same "re-check
                 right before dispatch" pattern as the artifact/approval
                 re-checks, for the same crash-and-resume-gap reason.
                 fakes.publisher.publish(key=request_id, target, hash)
                 on PublisherCrashed: request status -> unknown, stop
                 reconcile: the publisher's own receipt decides -- the
                 ONLY success is a receipt whose status is "done" AND
                 whose own key/target/artifact_hash all match this
                 request's (-> done); a "failed" status is a known,
                 final outcome (StepFailed("deploy_failed"), never
                 redispatched, never silently turned into success on a
                 later retry_unknown); any other receipt (no receipt
                 yet, "started", an unresolved "unknown", a "done"
                 receipt whose fields don't match, missing/unknown
                 status, or a lookup error) stays unresolved
                 (StepUnknown) rather than being redispatched or
                 treated as done.
record_publish   evidence.record(kind='publish', ...) + publish.recorded
report           report.write_report / report.exists
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from engine import release as release_mod
from engine.approvals import check as approvals_check
from engine.evidence import FingerprintError, fingerprint, record, sha256_file
from engine.fakes.publisher import PublisherCrashed
from engine.gitutil import git_argv
from engine.ledger import Event
from engine.processor import StepContext, StepDef, StepFailed, StepUnknown
from engine.steps.open_job import dirty_paths
from engine.steps.report import exists as report_exists
from engine.steps.report import write_report

_HEAD_ARTIFACT_PATH = "@HEAD"
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _project_config(ctx: StepContext):
    return ctx.config.projects[ctx.project]


def _repo(ctx: StepContext) -> Path:
    return _project_config(ctx).repo


def _excludes(ctx: StepContext) -> tuple[str, ...]:
    return _project_config(ctx).excludes


def _run_check_approval(ctx: StepContext) -> dict[str, object]:
    decision = approvals_check(ctx.ledger, ctx.request)
    if not decision.allowed:
        raise StepFailed(decision.reason)
    return {"approval": "ok"}


def _reconcile_check_approval(ctx: StepContext) -> bool:
    return any(
        row.used_seq is not None
        for row in ctx.ledger.approvals_for(ctx.request.request_id)
    )


def _is_within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _resolved_artifact_path(ctx: StepContext, artifact_path: str) -> Path:
    """Validate artifact_path (no absolute/.. escape, must resolve inside
    the repo, and inside the project's configured artifact_paths when
    any are set) and return the resolved on-disk path. Shared by
    check_artifact and call_publisher's re-verification."""
    repo = _repo(ctx)
    rel = Path(artifact_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise StepFailed("path_rejected")
    repo_real = repo.resolve()
    try:
        full_real = (repo / rel).resolve()
    except OSError as exc:
        raise StepFailed("path_rejected") from exc
    if not _is_within(full_real, repo_real):
        raise StepFailed("path_rejected")
    artifact_paths = _project_config(ctx).artifact_paths
    if artifact_paths:
        allowed = tuple(p.rstrip("/") for p in artifact_paths)
        rel_str = rel.as_posix()
        if not any(rel_str == p or rel_str.startswith(p + "/") for p in allowed):
            raise StepFailed("path_rejected")
    return full_real


def _rev_parse_head(repo: Path) -> str:
    try:
        result = subprocess.run(
            git_argv(repo, "rev-parse", "HEAD"),
            check=True, capture_output=True, text=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise StepFailed("no_fingerprint") from exc
    return result.stdout.strip()


def _check_head_artifact(ctx: StepContext, expected: str, *,
                         mismatch_reason: str, dirty_reason: str) -> str:
    """artifact_path="@HEAD": `expected` must be a full 40-hex commit
    sha equal to `git rev-parse HEAD`, AND the working tree must be
    clean (dirty_paths empty, excludes applied) -- a HEAD sha alone
    says nothing about uncommitted changes sitting on top of it, which
    would then be what actually gets deployed. `mismatch_reason`/
    `dirty_reason` let check_artifact and call_publisher's later
    re-verification use their own established reason codes (the
    latter folds both into one "artifact_changed_before_publish",
    matching the file-artifact re-check's existing single-reason
    pattern) rather than this function hard-coding either."""
    if not _FULL_SHA_RE.match(expected):
        raise StepFailed(mismatch_reason)
    repo = _repo(ctx)
    actual = _rev_parse_head(repo)
    if actual != expected:
        raise StepFailed(mismatch_reason)
    if dirty_paths(repo, _excludes(ctx)):
        raise StepFailed(dirty_reason)
    return actual


def _run_check_artifact(ctx: StepContext) -> dict[str, object]:
    artifact_path = str(ctx.request.payload.get("artifact_path", ""))
    expected = str(ctx.request.payload.get("artifact_hash", ""))
    if artifact_path == _HEAD_ARTIFACT_PATH:
        actual = _check_head_artifact(
            ctx, expected, mismatch_reason="artifact_mismatch", dirty_reason="tree_dirty")
        return {"artifact_hash": actual}
    full_real = _resolved_artifact_path(ctx, artifact_path)
    actual = sha256_file(full_real)
    if actual != expected:
        raise StepFailed("artifact_mismatch")
    return {"artifact_hash": actual}


def _full_bench_passed_for_release(ctx: StepContext) -> bool:
    """Defense in depth alongside engine.release.approve_and_run's own
    ensure_full_bench gate (which normally refuses to grant an
    approval at all until this already holds): true iff the latest
    non-stale bench evidence for this attempt, bound to the project
    repo's CURRENT fingerprint, passed with scope "full"/
    "full(fallback)" (release._latest_full_bench_passed -- same check,
    same fingerprint computation engine.steps.publish's own
    record_source step uses). False (never raises) if the fingerprint
    itself cannot be computed."""
    try:
        fp = fingerprint(_repo(ctx), _excludes(ctx))
    except FingerprintError:
        return False
    return release_mod._latest_full_bench_passed(ctx.ledger, ctx.attempt_id, fp)


def _run_advance_master(ctx: StepContext) -> dict[str, object]:
    """release.master_repo fast-forward to the approved @HEAD sha -- see
    the module docstring's "advance_master" entry. Runs only when
    steps() inserted this step (release configured); check_approval/
    check_artifact already ran, so the approval is bound to this exact
    sha and the working copy's own HEAD already equals it -- but both
    are journaled 'done' well before this point and never re-run, so
    the approval's binding/expiry is re-checked here too, immediately
    before this step's own mutation, not only later before dispatch.
    Also refuses ("full_bench_missing") unless a passing, non-stale
    FULL-suite bench already exists for this exact fingerprint --
    defense in depth should an approval ever exist despite
    ensure_full_bench never having passed (approve_and_run's own gate
    is the primary guard; this must not be the only thing standing
    between a failed suite and a real deploy)."""
    artifact_path = str(ctx.request.payload.get("artifact_path", ""))
    if artifact_path != _HEAD_ARTIFACT_PATH:
        raise StepFailed("release_requires_head_artifact")
    sha = str(ctx.request.payload.get("artifact_hash", ""))
    target = str(ctx.request.payload.get("target", ""))
    if not _approval_still_valid_for_publish(ctx, target, sha, time.time()):
        raise StepFailed("approval_invalid_before_advance_master")
    release_config = _project_config(ctx).release
    if release_config is None:
        raise StepFailed("release_not_configured")
    master_repo = release_config.master_repo
    # master_is_clean (no tracked dirt, no untracked-and-not-ignored
    # dirt at all) is strictly stronger than "no OVERLAP between
    # master's own dirty tracked paths and the incoming commits'" --
    # the deploy command builds directly from this shared working
    # tree, so even an unrelated tracked modification is refused here,
    # not just one the merge would actually touch.
    if not release_mod.master_is_clean(master_repo):
        raise StepFailed("master_not_clean")
    if not release_mod.is_fast_forwardable(master_repo, sha):
        raise StepFailed("not_fast_forward")
    occupied = release_mod.overlapping_paths(
        release_mod.incoming_added_paths(master_repo, sha),
        release_mod.master_untracked_or_ignored_paths(master_repo),
    )
    if occupied:
        raise StepFailed("incoming_path_occupied")
    # Last guard before the mutation itself: every OTHER refusal reason
    # above is independent of bench status and must still surface as
    # its own specific reason, not be masked by this one.
    if not _full_bench_passed_for_release(ctx):
        raise StepFailed("full_bench_missing")
    release_mod.ff_merge(master_repo, sha)
    return {"master_head": sha}


def _reconcile_advance_master(ctx: StepContext) -> bool:
    """True iff master_repo's HEAD already equals this request's sha --
    a crash strictly after ff_merge but before this step's journal
    'done' must not re-run the merge (git merge --ff-only on an
    already-advanced HEAD is a harmless no-op, but this reconcile keeps
    the same "observe a fact, don't re-dispatch" discipline every other
    step here follows)."""
    release_config = _project_config(ctx).release
    if release_config is None:
        return False
    sha = str(ctx.request.payload.get("artifact_hash", ""))
    try:
        return release_mod.head_sha(release_config.master_repo) == sha
    except subprocess.CalledProcessError:
        return False


def _run_record_source(ctx: StepContext) -> dict[str, object]:
    """Fingerprint the repo as 'source' evidence BEFORE any publisher
    effect (a git failure here fails closed with nothing published,
    unlike fingerprinting after publication). Always recomputes and
    records a NEW row bound to this request_id -- never reuses whatever
    'source' evidence the attempt already has: a second publish in the
    same attempt, after the source changed, must record the new fp
    against itself, not silently inherit an earlier publish's fp."""
    try:
        fp = fingerprint(_repo(ctx), _excludes(ctx))
    except FingerprintError as exc:
        raise StepFailed("no_fingerprint") from exc
    record(ctx.ledger, ctx.project, ctx.attempt_id, "source", fp,
           payload={"request_id": ctx.request.request_id})
    return {"fp": fp}


def _reconcile_record_source(ctx: StepContext) -> bool:
    """True iff a 'source' evidence row bound to THIS request already
    exists -- not just "some source evidence exists for the attempt",
    which could belong to an earlier or later request."""
    return _source_evidence_for_request(ctx) is not None


def _source_evidence_for_request(ctx: StepContext):
    rows = [
        row for row in ctx.ledger.evidence(ctx.attempt_id, "source")
        if not row.stale and row.payload.get("request_id") == ctx.request.request_id
    ]
    return rows[-1] if rows else None


def _approval_still_valid_for_publish(ctx: StepContext, target: str,
                                       artifact_hash: str, now: float) -> bool:
    """check_approval already consumed (used_seq) the approval row that
    authorized this request; re-check its fields and expiry right before
    dispatch rather than trusting the long-done check_approval step."""
    for row in ctx.ledger.approvals_for(ctx.request.request_id):
        if (row.used_seq is not None and row.target == target
                and row.artifact_hash == artifact_hash and row.expires_ts > now):
            return True
    return False


def _run_call_publisher(ctx: StepContext) -> dict[str, object]:
    target = str(ctx.request.payload.get("target", ""))
    artifact_hash = str(ctx.request.payload.get("artifact_hash", ""))
    artifact_path = str(ctx.request.payload.get("artifact_path", ""))

    # Re-verify right before dispatch: check_approval/check_artifact were
    # journaled 'done' earlier and are never re-run once done, so a crash
    # between them and this step (then an artifact edit, or the approval
    # simply expiring) must not go unnoticed. No publisher call has been
    # made yet, so a mismatch here is a clean StepFailed, never 'unknown'.
    if artifact_path == _HEAD_ARTIFACT_PATH:
        _check_head_artifact(
            ctx, artifact_hash,
            mismatch_reason="artifact_changed_before_publish",
            dirty_reason="artifact_changed_before_publish",
        )
    else:
        full_real = _resolved_artifact_path(ctx, artifact_path)
        if sha256_file(full_real) != artifact_hash:
            raise StepFailed("artifact_changed_before_publish")
    if not _approval_still_valid_for_publish(ctx, target, artifact_hash, time.time()):
        raise StepFailed("approval_invalid_before_publish")

    release_config = _project_config(ctx).release
    if release_config is not None:
        try:
            master_now = release_mod.head_sha(release_config.master_repo)
        except subprocess.CalledProcessError as exc:
            raise StepFailed("master_not_advanced") from exc
        if master_now != artifact_hash:
            raise StepFailed("master_not_advanced")
        if not release_mod.master_is_clean(release_config.master_repo):
            raise StepFailed("master_not_clean")
        # Same re-check-right-before-dispatch reasoning as the two
        # guards just above: advance_master's own full_bench_missing
        # check is journaled 'done' well before this point and never
        # re-run, so evidence invalidated (e.g. an install) in the gap
        # between advance_master and call_publisher must not go
        # unnoticed -- never dispatch on stale say-so.
        if not _full_bench_passed_for_release(ctx):
            raise StepFailed("full_bench_missing")

    key = ctx.request.request_id
    try:
        ctx.fakes.publisher.publish(key=key, target=target, artifact_hash=artifact_hash)
    except PublisherCrashed as exc:
        raise StepUnknown("publish_response_lost") from exc
    return {"target": target, "artifact_hash": artifact_hash}


def _reconcile_call_publisher(ctx: StepContext) -> bool:
    """Explicit receipt semantics (never "any receipt at all means
    success", and never "status == done means success" alone either):
    the ONLY success is a receipt whose status is "done" AND whose own
    recorded identity (key, target, artifact_hash) matches this
    request's -- a done receipt that happens to sit at the right
    lookup key but names a different target/sha (data corruption, a
    receipt file reused across keys, ...) must not be trusted. A
    "failed" status is a known, final outcome -- StepFailed, never
    silently turned into "done" by a later retry_unknown. Everything
    else (no receipt yet, "started", an unresolved "unknown", a
    receipt missing/mismatching its own status or identity fields, or
    a lookup error) stays unresolved rather than being redispatched by
    run() or quietly accepted as done."""
    try:
        receipt = ctx.fakes.publisher.lookup(ctx.request.request_id)
    except RuntimeError as exc:
        raise StepUnknown("publish_status_unavailable") from exc
    if receipt is None:
        return False
    if receipt.get("status") == "failed":
        raise StepFailed("deploy_failed")
    target = str(ctx.request.payload.get("target", ""))
    artifact_hash = str(ctx.request.payload.get("artifact_hash", ""))
    # "key" is ShellPublisher's receipt field name; FakePublisher's
    # lookup() (test-only, never a real deploy) calls the same thing
    # "id" instead -- accept either without requiring every Publisher
    # implementation to agree on one field name.
    receipt_key = receipt.get("key", receipt.get("id"))
    if (receipt.get("status") == "done" and receipt_key == ctx.request.request_id
            and receipt.get("target") == target
            and receipt.get("artifact_hash") == artifact_hash):
        return True
    raise StepUnknown("publish_response_lost")


def _run_record_publish(ctx: StepContext) -> dict[str, object]:
    # record_source always runs (and is journaled 'done') before
    # call_publisher, so a source row bound to this request is guaranteed
    # to exist here — no fallback fingerprinting after the publisher
    # effect.
    source = _source_evidence_for_request(ctx)
    if source is None:
        raise StepFailed("no_fingerprint")
    fp = source.fingerprint
    artifact_hash = str(ctx.request.payload.get("artifact_hash", ""))
    target = str(ctx.request.payload.get("target", ""))

    # Read the publisher's own receipt directly (an observable fact),
    # never call_publisher's journaled payload -- that step's reconcile
    # can return True (skip re-running) without ever re-populating its
    # 'done' payload on a resumed run, so a later step must read an
    # observable fact rather than trust an earlier step's own
    # self-reported journal payload.
    try:
        receipt = ctx.fakes.publisher.lookup(ctx.request.request_id)
    except RuntimeError:
        receipt = None
    dry_run = bool(receipt.get("dry_run", False)) if receipt else False

    # The evidence insert and the publish.recorded event are two separate
    # ledger appends that can't be made atomic through this API. Each is
    # independently idempotent on request_id instead, so a crash between
    # them (this step re-run on resume, since the outer reconcile below
    # only looks for the event) performs whichever one is still missing —
    # never duplicates the evidence row, never skips the event.
    already_recorded = any(
        row.payload.get("request_id") == ctx.request.request_id
        for row in ctx.ledger.evidence(ctx.attempt_id, "publish")
    )
    if not already_recorded:
        record(ctx.ledger, ctx.project, ctx.attempt_id, "publish", fp,
               artifact_hash=artifact_hash,
               payload={"request_id": ctx.request.request_id, "dry_run": dry_run})

    already_evented = any(
        event.payload.get("request_id") == ctx.request.request_id
        for _seq, event in ctx.ledger.events(ctx.project, "publish.recorded")
    )
    if not already_evented:
        ctx.ledger.append(Event(
            project=ctx.project, kind="publish.recorded", actor_kind="processor",
            actor_id="processor", attempt_id=ctx.attempt_id,
            payload={"request_id": ctx.request.request_id, "target": target,
                      "artifact_hash": artifact_hash, "dry_run": dry_run},
        ))
    return {"fp": fp, "artifact_hash": artifact_hash, "dry_run": dry_run}


def _reconcile_record_publish(ctx: StepContext) -> bool:
    """True iff a publish.recorded event for this request already exists —
    a crash between record_publish's run() and its journal 'done' must not
    duplicate the publish.recorded event (or the evidence row behind it)."""
    return any(
        event.payload.get("request_id") == ctx.request.request_id
        for _seq, event in ctx.ledger.events(ctx.project, "publish.recorded")
    )


def steps(ctx: StepContext) -> list[StepDef]:
    step_list = [
        StepDef("check_approval", _run_check_approval, _reconcile_check_approval),
        StepDef("check_artifact", _run_check_artifact, lambda ctx: False),
    ]
    if _project_config(ctx).release is not None:
        step_list.append(
            StepDef("advance_master", _run_advance_master, _reconcile_advance_master)
        )
    step_list += [
        StepDef("record_source", _run_record_source, _reconcile_record_source),
        StepDef("call_publisher", _run_call_publisher, _reconcile_call_publisher),
        StepDef("record_publish", _run_record_publish, _reconcile_record_publish),
        StepDef("report", write_report, report_exists),
    ]
    return step_list
