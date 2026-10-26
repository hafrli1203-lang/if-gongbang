"""`gb2 ship`/`gb2 skip-ship`/`gb2 resolve`: the release/publish track
(engine.release + the orbit stop after commit, when a project has
`release` configured)."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid

from engine import release
from engine.cli import _build_fakes, _drain, _job_name_for_attempt
from engine.config import EngineConfig
from engine.ledger import Event, Ledger
from engine.processor import retry_unknown
from engine.publishers import ShellPublisher

from . import shared, workspace

__all__ = ["cmd_ship", "cmd_skip_ship", "cmd_resolve"]


def _find_pending_publish(ledger: Ledger, project: str) -> tuple[str, str, str, str] | None:
    """(request_id, attempt_id, artifact_hash, target) of the queued
    'publish' request a release-configured job's orbit run left behind
    (JobOutcome status "needs_owner", reason "release_approval" --
    engine.orbit._run_stages), or None if there is none right now."""
    for row in ledger.queued_requests(project):
        if row.kind == "publish":
            return (row.request_id, row.attempt_id,
                    str(row.payload.get("artifact_hash", "")),
                    str(row.payload.get("target", "")))
    return None


def _find_unknown_publish(ledger: Ledger, project: str) -> tuple[str, str, str, str] | None:
    """Same shape as _find_pending_publish, for a 'publish' request
    that is stuck "unknown" (its deploy's outcome could not be
    determined -- engine.publishers.ShellPublisher) instead of
    "queued"."""
    for row in ledger.unknown_requests(project):
        if row.kind == "publish":
            return (row.request_id, row.attempt_id,
                    str(row.payload.get("artifact_hash", "")),
                    str(row.payload.get("target", "")))
    return None


def _publish_request_ids_for_attempt(ledger: Ledger, project: str,
                                      attempt_id: str) -> list[str]:
    """Every 'publish' request ever SUBMITTED for `attempt_id`, oldest
    first, regardless of current status -- unlike _find_pending_publish/
    _find_unknown_publish (one particular status bucket each, and
    project-wide rather than scoped to one attempt), this finds EVERY
    one belonging to this attempt: a `gb2 ship` already cancelled
    (failed) after a full-bench failure, one still 'queued', one
    'running'/'unknown' left that way by an interrupt, all of it --
    `gb2 skip-ship` needs to see all of them to decide whether it is
    safe to continue at all. [] if this attempt never submitted a
    'publish' request."""
    ids: list[str] = []
    seen: set[str] = set()
    for _seq, event in ledger.events(project, "request.submitted"):
        if event.attempt_id != attempt_id or event.payload.get("kind") != "publish":
            continue
        rid = str(event.payload["request_id"])
        if rid not in seen:
            seen.add(rid)
            ids.append(rid)
    return ids


def cmd_ship(args: argparse.Namespace) -> int:
    """Show the pending publish request's summary (commits, diff paths,
    target, sha, approval TTL), then require the owner to TYPE the
    exact "승인 R-..." line on a TTY (never accepted non-interactively --
    refused with exit 4 before anything is read from stdin). On a
    correct line: engine.release.approve_and_run grants the approval
    and drives the publish (fast-forward master + the configured
    deploy) to a terminal status. If the approval step itself refused
    because the full test suite failed ("full_bench_failed"), the
    request is NOT left queued -- release.cancel_pending_publish fails
    it outright (and revokes any approval, though approve_and_run never
    grants one in that case) so a later `gb2 skip-ship`/`gb2 fix` queue
    drain can never pick this exact stale request back up and deploy
    it. Every outcome from here -- shipped, that cancellation, or any
    other terminal failure (e.g. "artifact_mismatch") -- then goes
    through engine.full_orbit.finalize_release, the one place report ->
    close and the release decision actually happen (see its own
    docstring); only a request STILL left queued (a wrong approval
    line, malformed owner text, an unused-approval error -- never
    touched at all) leaves the job's project OPEN for a later retry,
    since finalize_release itself no-ops for that case too."""
    config_path = shared.app_workspace(args.app) / "config.json"
    if not config_path.exists():
        return shared.err_json(f"not initialized: run `gb2 init {args.app}` first", 1)
    workspace.verify_app_repo_identity(args.app)
    config = EngineConfig.from_json(config_path)
    project_config = config.projects[args.app]
    if project_config.release is None:
        return shared.err_json("release_not_configured", 1)

    if not sys.stdin.isatty():
        return shared.err_json("refused: non-interactive shell, ship requires a TTY", 4)

    ledger = Ledger.open(config.ledger_path)
    try:
        pending = _find_pending_publish(ledger, args.app)
        if pending is None:
            return shared.err_json("no_pending_publish_request", 1)
        request_id, attempt_id, artifact_hash, target = pending

        summary = release.build_summary(
            project_config.repo, project_config.release.master_repo, artifact_hash, target,
        )
        print(json.dumps({
            "request_id": request_id, "summary": summary,
            "approval_ttl_sec": config.approval_ttl_sec,
        }, ensure_ascii=False, indent=1, sort_keys=True), file=sys.stderr)

        owner_text = input(f'type the approval line exactly ("승인 {request_id}"): ')
        fakes = _build_fakes(config, args.app, attempt_id, None)
        publish_outcome = release.approve_and_run(
            config, ledger, fakes, args.app, request_id, owner_text,
        )
        if publish_outcome["status"] == "rejected":
            # approve_and_run's own EARLY bail-outs -- a wrong approval
            # line, malformed owner text, an unused-approval error, or
            # the full-bench pre-check refusing before ever granting an
            # approval. The request was either never touched at all
            # (still "queued" -- a later retry with the right line must
            # still see it pending) or, for "full_bench_failed"
            # specifically, just cancelled -- but a cancelled-for-bench
            # request is deliberately left for the owner's OWN later
            # decision (`gb2 skip-ship`, or `gb2 fix` + a fresh ship),
            # never auto-finalized here: test_ship_full_bench_failure_
            # then_skip_ship_never_deploys_and_closes pins exactly this
            # -- skip-ship must still be able to act on this same
            # attempt afterward, which finalize_release's own report ->
            # close would have already closed out from under it.
            if publish_outcome.get("reason") == "full_bench_failed":
                release.cancel_pending_publish(ledger, args.app, attempt_id, "full_bench_failed")
            print(json.dumps(publish_outcome, ensure_ascii=False))
            return 3

        # "done", or a genuine terminal "failed"/"unknown" reached by
        # actually running the publish (e.g. "artifact_mismatch", or a
        # real deploy exit code) -- both go through the SAME finalizer:
        # report/close and the release decision happen either way (see
        # engine.full_orbit.finalize_release's own docstring), not only
        # on success.
        from engine import full_orbit
        from engine import handoff

        job_name = _job_name_for_attempt(ledger, args.app, attempt_id)
        report_path = str(handoff.report_path(config, args.app, job_name))
        full_outcome = full_orbit.finalize_release(
            config, ledger, fakes, args.app, attempt_id, job_name, request_id, report_path,
        )
    finally:
        ledger.close()

    if publish_outcome["status"] != "done":
        # A genuine terminal failure (not the early "rejected" case
        # above, already returned) -- report/close + the release
        # decision already ran; the printed shape is still exactly
        # `publish_outcome` itself (its own "status"/"result" carry
        # what actually happened), matching every caller that already
        # parses this exact shape for this case.
        print(json.dumps(publish_outcome, ensure_ascii=False))
        return 3

    receipt_path = config.home / "publish" / f"{request_id}.json"
    print(json.dumps({
        "publish": publish_outcome, "receipt_path": str(receipt_path),
        "attempt_id": full_outcome.attempt_id, "status": full_outcome.status,
        "reason": full_outcome.reason,
    }, ensure_ascii=False))
    return 0 if full_outcome.status == "done" else 3


def cmd_skip_ship(args: argparse.Namespace) -> int:
    """Abandon the pending publish request and finish the job with
    report -> close. Before continuing, refuses outright (exit 3, no
    mutation) if ANY publish request of this attempt is 'unknown' (the
    owner must run `gb2 resolve` first) or 'running' and NOT safely
    cancellable -- "safely cancellable" means: no live processor holds
    the project lock, AND no receipt exists on disk for its key, AND
    engine.release.cancel_running_publish_before_dispatch confirms
    call_publisher's own journal entry does not exist at all yet (so
    the real deploy was genuinely never dispatched -- e.g. a
    KeyboardInterrupt during an earlier step like record_source).
    Without this check, report -> close's own queue processing would
    RESUME that exact 'running' request from wherever its journal left
    it -- the opposite of "skip": skip-ship abandoning a publish must
    never itself be what ends up deploying it.

    Once every publish request of this attempt is confirmed either
    already terminal or just-cancelled, release.cancel_pending_publish
    fails any still-QUEUED one outright (and revokes its unused
    approvals) -- belt-and-suspenders, rather than relying solely on
    the ordinary queue-drain's check_approval step to fail it
    "no_approval" on its own."""
    config_path = shared.app_workspace(args.app) / "config.json"
    if not config_path.exists():
        return shared.err_json(f"not initialized: run `gb2 init {args.app}` first", 1)
    config = EngineConfig.from_json(config_path)

    ledger = Ledger.open(config.ledger_path)
    try:
        project_row = ledger.project(args.app)
        attempt_id = project_row.current_attempt_id
        request_ids = (
            _publish_request_ids_for_attempt(ledger, args.app, attempt_id)
            if attempt_id is not None else []
        )
        if attempt_id is None or not request_ids:
            return shared.err_json("no_pending_publish_request", 1)

        publisher = None
        for request_id in request_ids:
            request = ledger.request(request_id)
            if request.status == "unknown":
                return shared.err_json(
                    f"refused: publish request {request_id} is 'unknown' -- run "
                    "`gb2 resolve` first, then `gb2 skip-ship` again", 3,
                )
            if request.status != "running":
                continue
            if not _project_lock_is_free(ledger, args.app):
                return shared.err_json(
                    "refused: project is locked by a live processor -- wait for it "
                    "to finish, then try `gb2 skip-ship` again", 3,
                )
            if publisher is None:
                publisher = _build_fakes(config, args.app, attempt_id, None).publisher
            try:
                has_receipt = publisher.lookup(request_id) is not None
            except RuntimeError:
                has_receipt = True  # can't confirm -- treat as unsafe, never cancel
            cancelled_before_dispatch = (
                not has_receipt
                and release.cancel_running_publish_before_dispatch(
                    ledger, args.app, request_id, "cancelled_before_dispatch")
            )
            if not cancelled_before_dispatch:
                return shared.err_json(
                    f"refused: publish request {request_id} may already be mid-"
                    "dispatch -- run `gb2 resolve` first, then `gb2 skip-ship` "
                    "again", 3,
                )

        cancelled = release.cancel_pending_publish(ledger, args.app, attempt_id, "skip_ship")

        # Same finalizer `gb2 ship` goes through (engine.full_orbit.
        # finalize_release's own docstring) -- report/close + the
        # release decision, idempotent, no matter which caller reached
        # this terminal state. `request_ids[-1]` (the same one reported
        # below as "skipped_request_id") is representative: every
        # request this just cancelled carries the identical reason
        # ("skip_ship"), so any of them decides the same "skipped"
        # outcome.
        from engine import full_orbit
        from engine import handoff

        job_name = _job_name_for_attempt(ledger, args.app, attempt_id)
        fakes = _build_fakes(config, args.app, attempt_id, None)
        report_path = str(handoff.report_path(config, args.app, job_name))
        full_outcome = full_orbit.finalize_release(
            config, ledger, fakes, args.app, attempt_id, job_name, request_ids[-1], report_path,
        )
    finally:
        ledger.close()
    print(json.dumps({
        "skipped_request_id": request_ids[-1], "cancelled_request_ids": cancelled,
        "attempt_id": full_outcome.attempt_id, "status": full_outcome.status,
        "reason": full_outcome.reason,
    }, ensure_ascii=False))
    return 0 if full_outcome.status == "done" else 3


def _check_argv(config: EngineConfig, target: str) -> list[str]:
    return [part.format(target=target) for part in config.publisher_dry_run_argv]


def _project_lock_is_free(ledger: Ledger, project: str) -> bool:
    """True iff no LIVE processor currently holds this project's lock.
    Reuses acquire_lock's own pid-liveness reclaim (a dead holder's row
    is reclaimed, not just reported busy) instead of reimplementing it,
    then immediately releases whatever it just (re)acquired -- this
    check never actually keeps the lock for itself."""
    probe = f"gb2-resolve-probe-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    if not ledger.acquire_lock(project, probe):
        return False
    ledger.release_lock(project, probe)
    return True


def _find_running_publish(ledger: Ledger, project: str,
                          publisher: "ShellPublisher") -> tuple[str, str, str, str] | None:
    """Same shape as _find_pending_publish, for a 'publish' request
    left 'running' -- or still 'queued' with an existing receipt
    already on disk, which can only happen if call_publisher's run()
    actually fired before the request's own status flip was ever
    observed -- by a KeyboardInterrupt (or any other crash) that hit
    while ShellPublisher.publish was dispatching it. Pure read: never
    mutates the ledger; `cmd_resolve` only marks a request found this
    way 'unknown' once it has ALSO confirmed (via _project_lock_is_free
    and the receipt's own pgid) that nothing could still be running
    it."""
    for row in ledger.queued_requests(project):  # queued OR running
        if row.kind != "publish":
            continue
        if row.status == "running" or publisher.lookup(row.request_id) is not None:
            return (row.request_id, row.attempt_id,
                    str(row.payload.get("artifact_hash", "")),
                    str(row.payload.get("target", "")))
    return None


def cmd_resolve(args: argparse.Namespace) -> int:
    """For a publish request stuck "unknown" (engine.publishers.
    ShellPublisher could not determine whether the deploy actually
    happened) -- OR one left "running" (or still "queued" with a
    receipt already on disk) by a KeyboardInterrupt that hit `gb2
    ship` while it was waiting on the deploy, confirmed by
    _project_lock_is_free (no live processor still holds the project's
    lock) that nothing could still be mid-dispatch for it: show the
    receipt (a request with none yet -- the publisher never recorded
    starting -- is resolved the same way), refuse outright if the
    receipt's own recorded process
    group is still alive (never ask the owner to guess while it might
    still be running), run the publisher's own check command
    (dry_run_argv, e.g. "deploy.sh <app> --check"), and require the
    owner -- on a TTY only -- to say "deployed" or "not deployed".
    "deployed" is accepted only if the check command itself exited 0;
    "not deployed" is always accepted. Either answer REWRITES THE
    RECEIPT only (to "done" or "failed") and lets retry_unknown/the
    ordinary reconcile path take it from there -- this never re-runs
    the real deploy command itself, and never redeploys. Once the
    request leaves "unknown", the job continues with report -> close
    (orbit.release_followup_stages), same as ship/skip-ship."""
    config_path = shared.app_workspace(args.app) / "config.json"
    if not config_path.exists():
        return shared.err_json(f"not initialized: run `gb2 init {args.app}` first", 1)
    workspace.verify_app_repo_identity(args.app)
    config = EngineConfig.from_json(config_path)
    if config.publisher_kind != "shell":
        return shared.err_json("resolve only applies to a project with publisher.kind=shell", 1)
    if not sys.stdin.isatty():
        return shared.err_json("refused: non-interactive shell, resolve requires a TTY", 4)

    ledger = Ledger.open(config.ledger_path)
    try:
        publisher = ShellPublisher(
            argv_template=config.publisher_argv,
            dry_run_argv_template=config.publisher_dry_run_argv,
            receipts_dir=config.home / "publish",
            timeout_sec=config.publisher_timeout_sec, dry_run=config.publisher_dry_run,
        )

        pending = _find_unknown_publish(ledger, args.app)
        already_unknown = pending is not None
        if pending is None:
            if not _project_lock_is_free(ledger, args.app):
                return shared.err_json(
                    "refused: project is locked by a live processor -- wait for "
                    "it to finish, then run `gb2 resolve` again", 1,
                )
            pending = _find_running_publish(ledger, args.app, publisher)
        if pending is None:
            return shared.err_json("no_unknown_publish_request", 1)
        request_id, attempt_id, artifact_hash, target = pending

        receipt = publisher.lookup(request_id)
        if receipt is None:
            # The publisher never recorded starting (a crash right after
            # call_publisher was journaled started): the same unknown path,
            # the check command and the owner's answer decide.
            receipt = {"key": request_id, "target": target, "artifact_hash": artifact_hash,
                       "status": "unknown", "dry_run": config.publisher_dry_run,
                       "returncode": None}

        pgid = receipt.get("pgid")
        if isinstance(pgid, int):
            try:
                os.killpg(pgid, 0)
                return shared.err_json(
                    f"refused: process group {pgid} is still alive -- wait for it "
                    "to finish, then run `gb2 resolve` again", 1,
                )
            except ProcessLookupError:
                pass  # confirmed gone -- safe to ask the owner
            except OSError:
                return shared.err_json(
                    f"refused: could not confirm process group {pgid} is gone", 1,
                )

        if not already_unknown:
            # Confirmed above: no live processor holds the lock, and
            # the receipt's own process group is gone -- safe to move
            # this out of 'running' so the rest of this flow (identical
            # to the ordinary stuck-unknown path) can take it from here.
            ledger.append(Event(
                project=args.app, kind="request.status", actor_kind="owner",
                actor_id="owner", attempt_id=attempt_id,
                payload={"request_id": request_id, "status": "unknown",
                         "result": {"reason": "interrupted"}},
            ))

        check_argv = _check_argv(config, target)
        check_result = subprocess.run(check_argv, capture_output=True, text=True)
        check_tail = ((check_result.stdout or "") + (check_result.stderr or ""))[-4000:]
        print(json.dumps({
            "request_id": request_id, "receipt": receipt, "check_argv": check_argv,
            "check_returncode": check_result.returncode, "check_tail": check_tail,
        }, ensure_ascii=False, indent=1, sort_keys=True), file=sys.stderr)

        answer = input('Did it actually deploy? Type "deployed" or "not deployed": ').strip()
        if answer == "deployed":
            if check_result.returncode != 0:
                return shared.err_json(
                    "refused: check command did not exit 0 -- cannot confirm deployed", 1,
                )
            new_status = "done"
            new_returncode: object = 0
        elif answer == "not deployed":
            new_status = "failed"
            new_returncode = receipt.get("returncode")
        else:
            return shared.err_json(f"refused: unrecognized answer {answer!r}, nothing changed", 1)

        publisher._write_receipt(request_id, {
            **receipt, "status": new_status, "ended_ts": time.time(),
            "returncode": new_returncode, "tail": check_tail,
        })

        retry_unknown(ledger, request_id, "owner")
        resolved_status = _drain(config, ledger, args.app, attempt_id, request_id)

        # report -> close -> record_release_decision, through the SAME
        # finalizer `gb2 ship`/`gb2 skip-ship`/continue_full_job's own
        # STAGE_RELEASE_APPROVAL resume all use (engine.full_orbit.
        # finalize_release) -- never report/close run directly here --
        # so this job's `full_orbit.release_decision` is always
        # recorded (the board, which reads that event, otherwise never
        # learns this request was ever resolved) and a repeated `gb2
        # resolve` call changes nothing: by the time the request is no
        # longer unknown/running, `_find_unknown_publish`/
        # `_find_running_publish` both return None above, and this
        # whole block is never reached again.
        from engine import full_orbit

        job_name = _job_name_for_attempt(ledger, args.app, attempt_id)
        fakes = _build_fakes(config, args.app, attempt_id, None)
        release_progress = full_orbit.pending_release_progress(ledger, args.app, job_name)
        report_path = str(release_progress.get("report_path", "")) if release_progress else ""
        final_outcome = full_orbit.finalize_release(
            config, ledger, fakes, args.app, attempt_id, job_name, request_id, report_path,
        )
    finally:
        ledger.close()

    print(json.dumps({
        "resolved_request_id": request_id, "resolved_status": resolved_status,
        "app": args.app, "attempt_id": final_outcome.attempt_id, "status": final_outcome.status,
        "reason": final_outcome.reason, "report_path": final_outcome.report_path,
    }, ensure_ascii=False))
    return 0 if final_outcome.status == "done" else 3
