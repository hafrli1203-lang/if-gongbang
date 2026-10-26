"""Fast-forward a project's configured master repo to the released
commit, then drive its deploy through the existing publish request/
approval machinery.

This module gives:
  - prepare(): submits this project's publish request for its own
    repo's current HEAD and builds the owner-facing approval prompt
    (engine.infrastructure.release_git.build_summary);
  - approve_and_run(): parses the owner's typed approval text, grants
    it, and drives the Processor until that request leaves
    queued/running;
  - ensure_full_bench()/cancel_pending_publish()/
    cancel_running_publish_before_dispatch(): the rest of the ship/
    skip-ship/fix machinery around a publish request.

The git primitives engine.application.steps.publish's advance_master step uses
(is_fast_forwardable, master_dirty_tracked_paths, merge_range_paths,
ff_merge, build_summary, ...) live in engine.infrastructure.release_git; the job-
certificate check a release requires (job_certificate_status) lives in
engine.application.release_certificate. Both are re-exported here so every existing
`engine.application.release.X` caller keeps working unchanged.
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from engine.infrastructure import evidence
from engine.domain.approvals import ApprovalError, grant, parse_owner_message
from engine.config import EngineConfig
from engine.domain.records import Event, LedgerError
from engine.application.processor import CancelError, Fakes, Processor, ProcessorBusy, cancel_request, submit
from engine.application.release_certificate import job_certificate_status, latest_full_bench_passed

# Back-compat spelling: this module used to define this check itself
# as a private `_latest_full_bench_passed`, before engine.
# release_certificate owned it as the public `latest_full_bench_passed`
# (used across that module boundary, so it needed a public name --
# ENGINE.md's own module-split rule). tests/unit/test_release.py still
# reads it off this module by that exact private name.
_latest_full_bench_passed = latest_full_bench_passed
from engine.infrastructure.release_git import (
    build_summary, commit_tree_hash, ff_merge, head_sha, incoming_added_paths,
    is_fast_forwardable, master_dirty_tracked_paths, master_is_clean,
    master_untracked_or_ignored_paths, master_untracked_paths, merge_range_paths,
    overlapping_paths,
)

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger


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


def _ledger_generation(ledger: Ledger, project: str) -> int:
    """A number that strictly increases with every ledger write for
    this project (the seq of its latest event, 0 if none yet) -- used
    as a bench payload nonce so a resubmission forced by
    ensure_full_bench (because the evidence check says fresh evidence
    is required even though submit()'s normal, deterministic
    request_id already names an old, terminal request) always gets a
    request_id submit() has never seen before, rather than
    deterministically colliding with that same old request again."""
    events = ledger.events(project)
    return events[-1][0] if events else 0


def ensure_full_bench(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                      attempt_id: str, max_iterations: int = 50) -> dict[str, object]:
    """Before advance_master ever runs for a ship: the commit being
    released must have a FULL-suite passing bench recorded against its
    exact fingerprint, not merely whatever scope satisfied the earlier
    commit step's three_match (bench_scope "related" is enough for
    that). If the latest passing, non-stale bench evidence for the
    current fingerprint already has scope "full"/"full(fallback)",
    this is a no-op ("ran": False).

    Otherwise submits a bench request with payload {"fp": fp, "scope":
    "full"} -- engine.application.steps.suite_run._resolve_test_argv treats
    payload["scope"] == "full" as an override that skips the
    related-tests shortcut entirely, regardless of the project's own
    bench_scope/related_cmd config. submit() is deterministic
    (request_id = hash of the payload), so if an EARLIER bench request
    with this exact payload already exists and is already terminal
    (done/failed/unknown) -- e.g. it passed once, then its evidence
    was later invalidated (an install) or marked stale -- submit()
    would just hand back that same old, already-resolved request_id
    with no new bench run at all, and trusting its "done" status as-is
    would silently resurrect a since-invalidated pass. So: if the
    request submit() returns is already terminal, resubmit once with
    _ledger_generation() mixed into the payload, forcing a request_id
    that has never existed before and therefore WILL actually run.
    Either way, the final "passed" is read back from the ledger's own
    evidence for this exact fingerprint (_latest_full_bench_passed),
    never from the bench request's own status -- the request's status
    is used only to drive run_request's resume loop, never trusted as
    the verdict by itself.

    Returns {"ran", "passed", "fp", "request_id"?, "status"?}; never
    raises -- a request kind this project doesn't support, or any
    other failure, surfaces as "passed": False."""
    project_config = config.projects[project]
    fp = evidence.fingerprint(project_config.repo, project_config.excludes)
    if latest_full_bench_passed(ledger, attempt_id, fp):
        return {"ran": False, "passed": True, "fp": fp}

    payload: dict[str, object] = {"fp": fp, "scope": "full"}
    request_id = submit(ledger, project, attempt_id, "bench", payload, "owner")
    if ledger.request(request_id).status not in ("queued", "running"):
        # submit() handed back an already-terminal request for this
        # exact payload -- see the docstring above for why that must
        # not be trusted as-is here.
        payload = {**payload, "_generation": _ledger_generation(ledger, project)}
        request_id = submit(ledger, project, attempt_id, "bench", payload, "owner")

    processor = Processor(config, ledger, fakes)
    processor.register_defaults()
    # run_request (not run_once): this project may ALREADY have the
    # publish request itself sitting queued/approved (prepare() submits
    # it, and approve_and_run grants it, before ever calling this) --
    # run_once would drain the WHOLE project queue in one call, which
    # could dispatch (or, if not yet approved, wrongly fail with
    # "no_approval") that unrelated publish request as a side effect of
    # just trying to run this bench. run_request touches ONLY this one
    # request_id.
    status = ledger.request(request_id).status
    for _ in range(max_iterations):
        if status not in ("queued", "running"):
            break
        status = processor.run_request(project, request_id)
    request = ledger.request(request_id)
    return {
        # The verdict is the ledger's own evidence for this exact fp,
        # never request.status alone (see docstring).
        "ran": True, "passed": latest_full_bench_passed(ledger, attempt_id, fp), "fp": fp,
        "request_id": request_id, "status": request.status, "result": request.result,
    }


def approve_and_run(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                    request_id: str, owner_text: str,
                    max_iterations: int = 50) -> dict[str, object]:
    """Parse `owner_text` (must be exactly "승인 R-..." per
    approvals.parse_owner_message), refuse if it names a different
    request than `request_id`, then -- BEFORE ever granting an
    approval, so a failed bench never leaves a valid, unused approval
    sitting next to a still-queued publish request for a LATER `gb2
    skip-ship`/`gb2 fix` queue-drain to pick up and deploy -- require
    ensure_full_bench to pass. A failed or still-running full bench
    refuses the whole ship ("reason": "full_bench_failed") with no
    approval ever granted; the request itself is left exactly as the
    caller finds it (callers that must guarantee it can never be
    dispatched later -- e.g. tools/gb2's `ship` -- call
    cancel_request/cancel_pending_publish themselves on this outcome).
    Only once the bench passes is the approval granted, then a fresh
    Processor's run_once driven in a bounded loop until the publish
    request leaves queued/running (done/failed/unknown). Never calls
    the real deploy script itself -- that only happens if
    `fakes.publisher` is wired to a real ShellPublisher by the
    caller."""
    parsed = parse_owner_message(owner_text)
    if parsed is None:
        return {"status": "rejected", "reason": "owner_text_malformed",
                "request_id": request_id}
    if parsed != request_id:
        return {"status": "rejected", "reason": "owner_text_wrong_request",
                "request_id": request_id}
    try:
        attempt_id = ledger.request(request_id).attempt_id
    except LedgerError as exc:
        return {"status": "rejected", "reason": f"unknown_request: {exc}",
                "request_id": request_id}

    bench_check = ensure_full_bench(config, ledger, fakes, project, attempt_id)
    if not bench_check["passed"]:
        return {"status": "rejected", "reason": "full_bench_failed",
                "request_id": request_id, "bench": bench_check}

    try:
        grant(ledger, project, request_id, ttl_sec=config.approval_ttl_sec)
    except ApprovalError as exc:
        return {"status": "rejected", "reason": str(exc), "request_id": request_id}

    processor = Processor(config, ledger, fakes)
    processor.register_defaults()
    # run_request, same reasoning as ensure_full_bench: touch only this
    # request_id, never the whole project queue.
    status = ledger.request(request_id).status
    for _ in range(max_iterations):
        if status not in ("queued", "running"):
            break
        status = processor.run_request(project, request_id)
    request = ledger.request(request_id)
    return {"status": request.status, "result": request.result,
            "request_id": request_id, "bench": bench_check}


def cancel_pending_publish(ledger: Ledger, project: str, attempt_id: str,
                           reason: str) -> list[str]:
    """Cancel every still-QUEUED 'publish' request of `attempt_id`
    (engine.application.processor.cancel_request -> status 'failed', result
    {"reason": reason}) and revoke every one of ITS approvals that has
    not yet been used -- an 'approval.used' event, the exact derived
    effect approvals.check's own successful path records, so a later
    approvals.check against it reports "used" (refused) rather than
    ever succeeding. Defense in depth for the window a full-bench
    failure used to leave open (approve_and_run now refuses to grant
    an approval at all until ensure_full_bench passes, so there is
    normally nothing here left to revoke -- this guards the case
    where an approval exists anyway, e.g. this function being called
    again after a partial failure, or a future caller granting one
    through some other path).

    A publish request that is already 'running'/'done'/'failed'/
    'unknown' is left completely untouched: it is either already
    resolved, or (running) has an external effect that must be
    reconciled through `gb2 resolve`/retry_unknown, never force-
    cancelled here. Returns the request_ids actually cancelled,
    oldest-submitted first; [] if this attempt has no queued publish
    request at all.

    Holds the project lock (the same one Processor.run_once/run_request
    takes) for the whole read-queued-requests-then-cancel sequence, not
    just the final write: without it, a caller's own "is this safe to
    cancel" check (e.g. tools/gb2's `skip-ship`, which checks
    _project_lock_is_free and the publisher's own receipt BEFORE calling
    this) and this function's cancellation could straddle a live
    processor's dispatch -- a request seen 'queued' here could flip to
    'running'/dispatched between that caller's own check and this
    function's own read, cancelling a request a processor had, by then,
    already started delivering. Raises ProcessorBusy (no ledger write at
    all) if the lock is already held by a live processor -- callers must
    retry rather than proceed as if nothing needed cancelling."""
    holder = f"cancel-pending-publish-{uuid.uuid4().hex[:8]}"
    if not ledger.acquire_lock(project, holder):
        raise ProcessorBusy(f"project {project!r} locked by another processor")
    try:
        cancelled: list[str] = []
        for request in ledger.queued_requests(project):
            if request.kind != "publish" or request.attempt_id != attempt_id:
                continue
            if request.status != "queued":
                continue  # 'running': has an external effect -- not ours to touch
            cancel_request(ledger, project, request.request_id, reason)
            _revoke_unused_approvals(ledger, project, attempt_id, request.request_id)
            cancelled.append(request.request_id)
        return cancelled
    finally:
        ledger.release_lock(project, holder)


def _revoke_unused_approvals(ledger: Ledger, project: str, attempt_id: str,
                             request_id: str) -> None:
    """'approval.used' for every one of `request_id`'s approvals that
    has not yet been used -- the exact derived effect approvals.check's
    own successful path records, so a later approvals.check against it
    reports "used" (refused) rather than ever succeeding. Shared by
    cancel_pending_publish and cancel_running_publish_before_dispatch."""
    for approval in ledger.approvals_for(request_id):
        if approval.used_seq is None:
            ledger.append(Event(
                project=project, kind="approval.used",
                actor_kind="owner", actor_id="owner", attempt_id=attempt_id,
                payload={"approval_id": approval.approval_id},
            ))


def cancel_running_publish_before_dispatch(ledger: Ledger, project: str,
                                           request_id: str, reason: str) -> bool:
    """For a publish request currently 'running': cancel it (status
    'failed', result {"reason": reason}) and revoke its unused
    approvals, exactly like cancel_pending_publish does for a queued
    one -- but ONLY IF this request's own step journal shows
    call_publisher has no entry AT ALL yet (not even 'started') --
    the dispatch step that actually calls fakes.publisher.publish().
    Returns True if cancelled, False (no ledger mutation at all) if
    call_publisher's journal entry already exists ('started' or
    'done') -- the caller must then refuse and point the owner at
    `gb2 resolve` instead, never force this.

    This is a JOURNAL check only. engine.application.processor._run_steps journals
    a step 'started' strictly BEFORE that step's own run() executes,
    so a crash in the narrow window between that journal write and
    call_publisher's run() actually calling publish() would also show
    'started' with no receipt on disk yet at all -- deliberately
    treated here as "might already be dispatching", never as safe to
    cancel. This function now holds the project lock itself (see
    below) for its own status/journal checks and cancellation write,
    so a caller no longer needs the lock held to make those checks
    safe -- but callers (tools/gb2's `skip-ship`) are still
    responsible for confirming, before ever calling this, that no
    receipt exists for this key at all: the publisher's own receipt
    is not read here.

    Raises CancelError if the request does not belong to `project` or
    is not 'running' (same guard cancel_request uses for 'queued').

    Holds the project lock (the same one Processor.run_once/run_request
    and cancel_pending_publish take) for the whole status-check,
    journal-check and cancellation-write sequence -- cancel_pending_
    publish's own docstring explains why this matters for a queued
    request, and the same race exists here for a running one: without
    the lock, this function's own "journal shows no call_publisher
    entry yet" read and its "failed" status write are not atomic with
    a live processor concurrently running THIS SAME request's steps
    under that lock. A processor that is mid-dispatch when this
    function reads an empty journal would go on to call
    Processor._set_status(..., "done", ...) once it finishes --
    unconditionally overwriting the "failed" cancellation this
    function just wrote, so the deploy would proceed (and be recorded
    as done) despite the cancellation. Holding the lock here makes
    that impossible: a processor cannot even begin draining this
    project's queue while this function holds it, so a confirmed-empty
    journal read stays true until this function's own write lands.
    Raises ProcessorBusy (no ledger write at all) if the lock is
    already held by a live processor -- callers must retry rather
    than proceed as if nothing needed cancelling."""
    holder = f"cancel-running-publish-{uuid.uuid4().hex[:8]}"
    if not ledger.acquire_lock(project, holder):
        raise ProcessorBusy(f"project {project!r} locked by another processor")
    try:
        request = ledger.request(request_id)
        if request.project != project:
            raise CancelError(
                f"request {request_id} belongs to project {request.project!r}, "
                f"not {project!r}"
            )
        if request.status != "running":
            raise CancelError(f"request {request_id} is {request.status!r}, not running")
        if ledger.steps(request_id).get("call_publisher") is not None:
            return False
        ledger.append(Event(
            project=project, kind="request.status", actor_kind="owner",
            actor_id="owner", attempt_id=request.attempt_id,
            payload={"request_id": request_id, "status": "failed",
                     "result": {"reason": reason}},
        ))
        _revoke_unused_approvals(ledger, project, request.attempt_id, request_id)
        return True
    finally:
        ledger.release_lock(project, holder)


__all__ = [
    "head_sha", "commit_tree_hash", "is_fast_forwardable", "master_dirty_tracked_paths",
    "master_untracked_paths", "master_untracked_or_ignored_paths",
    "master_is_clean", "merge_range_paths", "incoming_added_paths",
    "overlapping_paths", "ff_merge", "build_summary",
    "prepare", "approve_and_run", "ensure_full_bench", "cancel_pending_publish",
    "cancel_running_publish_before_dispatch", "job_certificate_status",
]
