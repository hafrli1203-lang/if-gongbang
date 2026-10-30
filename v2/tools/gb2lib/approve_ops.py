"""`gb2 approve-design`/`gb2 approve-plan-edit`/`gb2 approve-install`:
the three owner-approval CLI entry points -- split out of runner.py
(which keeps `run`/`fix`/`change`/`continue`) to stay under the
800-line architecture cap (docs/standards/architecture.md §3). Each
command accepts either the existing TTY "승인 <job>" line or, as of
E1, a `--office-token <path>` document verified through
engine.application.office_approval (tools/gb2lib/office_tokens.py's
shared read+verify helper) -- see each function's own docstring."""
from __future__ import annotations

import argparse
import json
import sys
import time

from engine.cli import _drain, _job_name_for_attempt
from engine.config import EngineConfig
from engine.ledger import Event, Ledger

from . import office_tokens, shared, workspace

__all__ = ["cmd_approve_design", "cmd_approve_plan_edit", "cmd_approve_install"]


def cmd_approve_plan_edit(args: argparse.Namespace) -> int:
    """`gb2 approve-plan-edit <app> --reason "<사유>"`: the owner's
    re-approval for the CURRENT job's plan after the owner has edited
    a tests/scenes/ file post-review (plan.json itself is never
    touched -- see engine.domain.plan_review.record_owner_plan_amend's
    own docstring for the full contract). No TTY gate (unlike
    approve-design/approve-install's "승인 <job>" line): this command
    is owner-only by construction already -- a builder runs inside the
    engine's sandbox and has no path to invoke `gb2` at all, and
    engine.application.repair_target never hands tests/scenes/ to a
    worker in the first place, so there is no non-owner caller to gate
    against here.

    Refused (exit 1, nothing recorded) if there is no active job for
    this app, or if record_owner_plan_amend itself refuses (blank
    reason, unreadable/invalid plan, never reviewed, or nothing
    changed since the last review). On success, prints {"job_name":
    ..., "reason": ..., "changed": ...} and returns 0 -- this command
    only re-approves the plan; it does NOT resume the job itself
    (`gb2 fix`/`gb2 continue` does that separately, same as any other
    owner action).

    `--office-token <path>` (E1, AI office approval): a signed
    approval document (engine.application.office_approval, kind=
    "plan_edit") read from a FILE, verified against this job's CURRENT
    plan content before anything is recorded -- refused (exit 1,
    nothing recorded) if the file cannot be read/parsed
    ("office_token_unreadable") or engine.application.office_approval.
    verify_office_token itself refuses it (bad/forged signature,
    expired, a reused nonce, or the plan has already moved on from the
    version the token names). On success, confirm_source="office" is
    recorded on the plan.owner_amended event instead of the default
    "tty" -- this command never had a TTY gate of its own (see above),
    so --office-token changes only that recorded provenance, not
    whether the command runs non-interactively (it always did).

    `current_digest` is read
    ONCE, right here, regardless of path -- the SAME value is what the
    office token is verified against (`expected_target_version`, office
    path only) AND, unconditionally, what is passed to
    record_owner_plan_amend as `expected_current_digest`. Never two
    independent plan_digest reads that could disagree if a scene file
    changed in between (TTY included -- pinning once is free and
    removes the question entirely rather than branching on it).
    record_owner_plan_amend re-checks that pinned value against the
    CURRENT digest right before it would otherwise approve, and
    refuses ("plan_changed_after_verification") rather than silently
    re-blessing content that moved since. The STORED "plan.reviewed"
    digest is this SAME pinned value too (engine.domain.plan_review.
    record_plan_reviewed's `digest_override`), never a THIRD,
    independent re-read."""
    config_path = shared.app_workspace(args.app) / "config.json"
    if not config_path.exists():
        return shared.err_json(f"not initialized: run `gb2 init {args.app}` first", 1)
    workspace.verify_app_repo_identity(args.app)
    config = EngineConfig.from_json(config_path)

    from engine.application.office_approval import plan_edit_target_version
    from engine.domain.plan_review import record_owner_plan_amend

    ledger = Ledger.open(config.ledger_path)
    try:
        project_row = ledger.project(args.app)
        attempt_id = project_row.current_attempt_id
        if attempt_id is None:
            return shared.err_json("no_active_job", 1)
        job_name = _job_name_for_attempt(ledger, args.app, attempt_id)
        if job_name is None:
            return shared.err_json("no_active_job", 1)
        repo = config.projects[args.app].repo

        current_digest = plan_edit_target_version(repo, job_name)
        confirm_source = "tty"
        office_token_path = getattr(args, "office_token", None)
        if office_token_path is not None:
            confirm_source, error = office_tokens.verify_office_token_or_reason(
                config, ledger, args.app, job_name, attempt_id, "plan_edit", office_token_path,
                expected_target_version=current_digest,
            )
            if error is not None:
                return shared.err_json(error, 1)

        # A plan the reviewer never passed (stopped at awaiting_plan_review,
        # e.g. the round cap) may be approved here by the foreman after a
        # hand edit -- see record_owner_plan_amend's allow_first_review.
        from engine.application.job_state import STAGE_PLAN_REVIEW, load_position

        position = load_position(ledger, args.app, job_name)
        at_plan_review = position is not None and position.stage == STAGE_PLAN_REVIEW
        problems = record_owner_plan_amend(
            ledger, args.app, attempt_id, repo, job_name, args.reason or "",
            confirm_source=confirm_source, expected_current_digest=current_digest,
            allow_first_review=at_plan_review,
        )
        if problems:
            return shared.err_json(f"refused: {'; '.join(problems)}", 1)

        changed = None
        for _seq, event in ledger.events(args.app, "plan.owner_amended"):
            if event.attempt_id == attempt_id and event.payload.get("job") == job_name:
                changed = event.payload.get("changed")
        result = shared.approval_recorded_summary(args.app, job_name, "plan.owner_amended")
        result.update({"reason": args.reason, "changed": changed})
        print(json.dumps(result, ensure_ascii=False))
        return 0
    finally:
        ledger.close()


def cmd_approve_design(args: argparse.Namespace) -> int:
    """`gb2 approve-design <app>`: the owner's TTY-only approval gate
    for engine.gates.check_design_approved -- requires the owner to
    TYPE the exact "승인 <job>" line (never accepted non-interactively,
    refused with exit 4 before anything is read from stdin, same
    convention as `release_ops.cmd_ship`'s release approval), then
    appends a `design.approved` ledger event carrying {"job": job_name}.

    결함 64: this command used to resume the job itself with engine.
    full_orbit.continue_full_job right after recording that approval,
    in the SAME process -- so a build loop that runs for minutes kept
    the owner's own TTY (the one they just typed the approval line
    into) busy, with no way to get it back short of closing the
    terminal (which killed the job build mid-flight too, since nothing
    else was driving it). `gb2 watch --loop --resume` already resumes
    every stopped job on its own; this command's job is ONLY to record
    the approval and hand back (shared.approval_recorded_summary) that
    the resume is `gb2 continue`'s job (or watch's) from here. Pass
    `--continue` to keep the OLD one-shot behavior -- record the
    approval, then resume in this same call -- for a caller that wants
    it (still covered by its own tests, unchanged).

    `--office-token <path>` (E1, AI office approval): takes the place
    of the TTY "승인 <job>" line -- the TTY requirement above is
    skipped ONLY when this is given, never weakened for a plain call.
    The file must hold a signed approval document (engine.
    application.office_approval, kind="design") naming exactly this
    (app, job_name, attempt_id) and the job's CURRENT plan+design
    content; refused (exit 1, nothing recorded) if the file cannot be
    read/parsed ("office_token_unreadable"), verify_office_token itself
    refuses it (bad/forged signature, expired, a reused nonce, the
    design content moved on since the token was issued, or a design/
    screens file could not even be read -- "design_artifact_unreadable"),
    or a design/screens file cannot be read for
    the TTY path's own pin below. On success, confirm_source="office"
    is recorded on the design.approved event in place of "tty".

    `target_version`: pinned ONCE,
    right here (engine.application.office_approval.
    design_target_version), and stored VERBATIM on the design.approved
    event -- never re-derived at approval time. engine.application.
    pipeline_resume's own resume point (design_approval_still_matches)
    compares the CURRENT content against this exact stored value
    before letting the job's build stage actually start, so a design
    deliverable swapped AFTER approval but before the next resume is
    caught there, fail closed, rather than trusted on the strength of
    "design.approved exists" alone."""
    config_path = shared.app_workspace(args.app) / "config.json"
    if not config_path.exists():
        return shared.err_json(f"not initialized: run `gb2 init {args.app}` first", 1)
    workspace.verify_app_repo_identity(args.app)
    office_token_path = getattr(args, "office_token", None)
    if office_token_path is None and not sys.stdin.isatty():
        return shared.err_json("refused: non-interactive shell, approve-design requires a TTY", 4)
    config = EngineConfig.from_json(config_path)
    try:
        config, fakes_for = shared.fakes_builder(config, args.app)
    except ValueError as exc:
        return shared.err_json(str(exc), 1)

    from engine.application.office_approval import DesignArtifactUnreadable, design_target_version

    ledger = Ledger.open(config.ledger_path)
    try:
        project_row = ledger.project(args.app)
        attempt_id = project_row.current_attempt_id
        if attempt_id is None:
            return shared.err_json("no_active_job", 1)
        job_name = _job_name_for_attempt(ledger, args.app, attempt_id)
        repo = config.projects[args.app].repo

        try:
            target_version = design_target_version(repo, job_name)
        except DesignArtifactUnreadable as exc:
            return shared.err_json(str(exc), 1)

        confirm_source = "tty"
        if office_token_path is not None:
            confirm_source, error = office_tokens.verify_office_token_or_reason(
                config, ledger, args.app, job_name, attempt_id, "design", office_token_path,
                expected_target_version=target_version,
            )
            if error is not None:
                return shared.err_json(error, 1)
        else:
            owner_text = input(f'type the approval line exactly ("승인 {job_name}"): ')
            if owner_text != f"승인 {job_name}":
                return shared.err_json("refused: approval line did not match, nothing changed", 1)

        ledger.append(Event(
            project=args.app, kind="design.approved", actor_kind="owner",
            actor_id="owner", attempt_id=attempt_id,
            payload={"job": job_name, "confirm_source": confirm_source,
                     "target_version": target_version},
        ))

        if not getattr(args, "continue_job", False):
            print(json.dumps(
                shared.approval_recorded_summary(args.app, job_name, "design.approved"),
                ensure_ascii=False,
            ))
            return 0

        from engine import full_orbit

        start = time.monotonic()
        outcome = full_orbit.continue_full_job(
            config, ledger, fakes_for, args.app, job_name, on_progress=shared.progress_printer,
        )
        wall_sec = time.monotonic() - start
        summary = shared.summarize_full_outcome(config, ledger, args.app, outcome, wall_sec)
    finally:
        ledger.close()

    shared.append_run_log(args.app, {"job_name": job_name, **summary})
    print(json.dumps(summary, ensure_ascii=False))
    return shared.exit_code_for_full_outcome(str(summary["status"]))


def cmd_approve_install(args: argparse.Namespace) -> int:
    """`gb2 approve-install <app>`: TTY-only approval for every pending
    owner request a station asked for (any kind engine.owner.
    PENDING_KIND_WHITELIST allows: install, scripts_change,
    test_config_change) -- a build task (engine.
    full_orbit.STAGE_TASK_OWNER) or an earlier facts/probe/plan/design
    station (STAGE_EARLY_STATION) -- found via engine.full_orbit.
    pending_owner_requests, the station.finished "requests" entry a
    full-orbit job is currently stopped on. Shows what manifest/change
    each one is asking for, requires the owner to TYPE the exact "승인 <job>" line (same
    TTY convention as `cmd_approve_design`/`release_ops.cmd_ship`,
    refused outright on a non-interactive shell), then submits/approves
    each one through the EXISTING engine.owner.validate_pending/
    approve_pending gate -- never re-implementing that validation here
    -- and drains the processor queue (engine.cli._drain, imported here
    as `_drain`) so the install actually runs before anything else
    happens.

    결함 64: once every pending request is confirmed "done", this used
    to resume the job right away with engine.full_orbit.
    continue_full_job -- since b0c8208, rerunning the builder task that
    stopped from where it left off -- in the SAME process, holding the
    owner's own TTY for however long that build loop ran. Now the
    install(s) are approved+drained and nothing more: `gb2 continue`
    (or `gb2 watch --loop --resume`, already running) picks the job
    back up from there (shared.approval_recorded_summary names it in
    the result). `--continue` keeps the old one-shot behavior for a
    caller that wants it.

    `--office-token <path>` (E1, AI office approval): takes the place
    of the TTY "승인 <job>" line -- the TTY requirement above is
    skipped ONLY when this is given. The file must hold a signed
    approval document (engine.application.office_approval, kind=
    "install") naming exactly this (app, job_name, attempt_id) and a
    target_version matching the FULL current pending batch (every
    request `pending` below holds, hashed together WITH each one's own
    proposal_id) -- refused (exit 1, nothing
    approved/drained) if the file cannot be read/parsed
    ("office_token_unreadable") or verify_office_token itself refuses
    it (bad/forged signature, expired, a reused nonce, or the pending
    batch has changed since the token was issued). On success, every
    request below is approved with confirm_source="office" instead of
    "tty" -- engine.application.owner_approval.approve_pending's own
    kind_requires_interactive_confirm check still applies exactly as
    before, so a scripts_change/test_config_change request is refused
    here too (only "install" accepts a non-"tty" confirm_source) --
    this command never weakens that; it only adds a second verified
    way to supply "tty"-equivalent confirmation for "install" itself.

    `pending` is queried exactly ONCE below, via
    engine.application.office_approval.pending_install_requests, and
    that SAME list is what install_target_version hashes for
    verification AND what the approval loop below iterates -- never a
    second, independent query that could observe a different batch if
    a station finished in between. the raw
    `pending_owner_requests`/`pending` printed to stderr below for a
    human to read before typing "승인 <job>" is skipped entirely on
    the --office-token path -- a request can carry a secret (e.g. a
    credential embedded in a dependency URL that validate_pending
    would still accept), and a subprocess's stderr is not guaranteed
    private the way an interactive owner's own terminal is (e.g. a web
    server collecting it into its own logs)."""
    config_path = shared.app_workspace(args.app) / "config.json"
    if not config_path.exists():
        return shared.err_json(f"not initialized: run `gb2 init {args.app}` first", 1)
    workspace.verify_app_repo_identity(args.app)
    office_token_path = getattr(args, "office_token", None)
    if office_token_path is None and not sys.stdin.isatty():
        return shared.err_json("refused: non-interactive shell, approve-install requires a TTY", 4)
    config = EngineConfig.from_json(config_path)
    try:
        config, fakes_for = shared.fakes_builder(config, args.app)
    except ValueError as exc:
        return shared.err_json(str(exc), 1)

    from engine import full_orbit
    from engine import owner as owner_module
    from engine.application.office_approval import (
        install_target_version_from_batch, pinned_install_batch,
    )

    ledger = Ledger.open(config.ledger_path)
    try:
        project_row = ledger.project(args.app)
        attempt_id = project_row.current_attempt_id
        if attempt_id is None:
            return shared.err_json("no_active_job", 1)
        job_name = _job_name_for_attempt(ledger, args.app, attempt_id)

        # Every owner action kind a station can propose (install,
        # scripts_change, test_config_change), still unfulfilled against
        # the current manifest -- queried exactly once (see docstring),
        # via the SAME engine.full_orbit.
        # pending_owner_requests seam job_state.py's own resume gates
        # use (and tests mock against), never engine.application.
        # office_approval's own internal copy of this query.
        pending = [req for req in full_orbit.pending_owner_requests(
                       ledger, args.app, job_name, repo=config.projects[args.app].repo)
                   if req.get("kind") in owner_module.PENDING_KIND_WHITELIST]
        if not pending:
            return shared.err_json("no_pending_owner_request", 1)

        # (request, proposal_id) pairs, computed ONCE (one load_position
        # call) -- the SAME pairs both the hash below and the actual
        # approval loop further down use, never a second, independent
        # proposal_id lookup at submission time (the signed proposal and the submitted one used to
        # come from
        # two separate lookups and could disagree).
        batch = pinned_install_batch(ledger, args.app, job_name, pending)

        confirm_source = "tty"
        if office_token_path is not None:
            target_version = install_target_version_from_batch(batch)
            confirm_source, error = office_tokens.verify_office_token_or_reason(
                config, ledger, args.app, job_name, attempt_id, "install", office_token_path,
                expected_target_version=target_version,
            )
            if error is not None:
                return shared.err_json(error, 1)
        else:
            # Shown only on the TTY path -- the human about to type the
            # approval line reads this in their OWN terminal (see docstring).
            print(json.dumps({"job_name": job_name, "pending_owner_requests": pending},
                             ensure_ascii=False, indent=1, sort_keys=True), file=sys.stderr)
            owner_text = input(f'type the approval line exactly ("승인 {job_name}"): ')
            if owner_text != f"승인 {job_name}":
                return shared.err_json("refused: approval line did not match, nothing changed", 1)

        for entry in batch:
            request = entry["request"]
            try:
                request_id = owner_module.approve_pending(
                    config, ledger, args.app, attempt_id, request,
                    confirm=True, confirm_source=confirm_source, proposal=entry["proposal"],
                )
            except ValueError as exc:
                return shared.err_json(f"approve_pending_refused: {exc}", 1)
            status = _drain(config, ledger, args.app, attempt_id, request_id)
            if status != "done":
                return shared.err_json(f"install_request_failed: {request_id} status={status}", 3)

        if not getattr(args, "continue_job", False):
            print(json.dumps(
                shared.approval_recorded_summary(args.app, job_name, "owner.approved"),
                ensure_ascii=False,
            ))
            return 0

        start = time.monotonic()
        outcome = full_orbit.continue_full_job(
            config, ledger, fakes_for, args.app, job_name, on_progress=shared.progress_printer,
        )
        wall_sec = time.monotonic() - start
        summary = shared.summarize_full_outcome(config, ledger, args.app, outcome, wall_sec)
    finally:
        ledger.close()

    shared.append_run_log(args.app, {"job_name": job_name, **summary})
    print(json.dumps(summary, ensure_ascii=False))
    return shared.exit_code_for_full_outcome(str(summary["status"]))
