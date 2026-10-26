"""`gb2 run`/`gb2 fix`/`gb2 continue`/`gb2 change`/`gb2 approve-design`/
`gb2 approve-install`: driving a job -- a single `run_station`+review+
bench task (`run`/`fix`), or the full orbit (`change`/`continue`/
`approve-design`/`approve-install`, same engine.full_orbit entry
points `workspace.cmd_new` uses) -- through the engine."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from engine import evidence, fixup, orbit
from engine.cli import _build_fakes, _drain, _job_name_for_attempt
from engine.config import EngineConfig
from engine.ledger import Event, Ledger
from engine.steps.open_job import open_job

from . import ops, shared, workspace

__all__ = [
    "cmd_run", "cmd_fix", "cmd_change", "cmd_approve_design",
    "cmd_continue", "cmd_approve_install",
]


# --------------------------------------------------------------------
# run
# --------------------------------------------------------------------

def _findings_by_severity(findings: list[object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        if isinstance(finding, dict):
            severity = str(finding.get("severity", "?"))
            counts[severity] = counts.get(severity, 0) + 1
    return counts


def _summarize(config: EngineConfig, ledger: Ledger, app: str, attempt_id: str,
               outcome: orbit.JobOutcome, wall_sec: float) -> dict[str, object]:
    project = config.projects[app]
    review = evidence.latest(ledger, attempt_id, "review")
    bench_row = evidence.latest(ledger, attempt_id, "bench")
    findings = []
    if review is not None:
        findings = review.payload.get("findings", [])
    commit_hash = None
    try:
        commit_hash = shared.git(["rev-parse", "HEAD"], cwd=project.repo)
    except subprocess.CalledProcessError:
        pass
    changed_files: list[str] = []
    try:
        base_head = None
        for _seq, event in ledger.events(app, "job.opened"):
            if event.payload.get("attempt_id") == attempt_id:
                base_head = event.payload.get("base_head")
                break
        if base_head and commit_hash and base_head != commit_hash:
            out = shared.git(["diff", "--name-only", f"{base_head}..{commit_hash}"],
                       cwd=project.repo)
            changed_files = [line for line in out.splitlines() if line]
    except subprocess.CalledProcessError:
        pass
    cost_usd = 0.0
    for _seq, event in ledger.events(app, "station.finished"):
        if event.attempt_id == attempt_id:
            cost = event.payload.get("cost_usd")
            if isinstance(cost, (int, float)):
                cost_usd += float(cost)
    metrics: dict[str, object] = {}
    for _seq, event in ledger.events(app, "job.metrics"):
        if event.attempt_id == attempt_id:
            metrics = event.payload  # last one wins (report never repeats mid-job)
    return {
        "app": app, "attempt_id": attempt_id, "status": outcome.status,
        "reason": outcome.reason, "commit": commit_hash,
        "changed_files": changed_files, "cost_usd": round(cost_usd, 4),
        "minutes": round(wall_sec / 60.0, 2),
        "findings_by_severity": _findings_by_severity(findings),
        "bench_scope": (bench_row.payload.get("scope") if bench_row else None),
        "bench_passed": (bench_row.payload.get("passed") if bench_row else None),
        "durations_ms": metrics.get("durations_ms", {}),
        "test_runs": metrics.get("test_runs", {}),
    }


def cmd_run(args: argparse.Namespace) -> int:
    config_path = shared.app_workspace(args.app) / "config.json"
    if not config_path.exists():
        print(json.dumps({"error": f"not initialized: run `gb2 init {args.app}` first"}),
             file=sys.stderr)
        return 1
    workspace.verify_app_repo_identity(args.app)
    if args.task_file:
        task = Path(args.task_file).read_text(encoding="utf-8")
    elif args.task:
        task = args.task
    else:
        print(json.dumps({"error": "--task or --task-file required"}), file=sys.stderr)
        return 1

    config = EngineConfig.from_json(config_path)
    job_name = args.job_name or f"gb2-{int(time.time())}"
    ledger = Ledger.open(config.ledger_path)
    start = time.monotonic()
    try:
        # Only "builder" gets a worker copy -- the reviewer runs
        # against its own fresh snapshot (engine.steps.review),
        # never a pre-made copy_root.
        attempt_id = open_job(config, ledger, args.app, job_name, "full",
                              ("builder",))
        fakes = _build_fakes(config, args.app, attempt_id, None)
        stages = orbit.default_stages(config, attempt_id, "builder", "reviewer",
                                      task, job_name)

        # The FULL stage list in one continue_job call -- driving it one
        # growing prefix at a time (one stage, then the first two, ...)
        # would start a brand-new Processor on every call, so a review
        # stage already finished to "done" in an earlier call could
        # never be adjacent to bench in a later call's stage list for
        # config.parallel_review_bench's overlap to actually happen.
        outcome = orbit.continue_job(config, ledger, fakes, args.app, attempt_id,
                                     stages, on_progress=shared.progress_printer)
        wall_sec = time.monotonic() - start
        summary = _summarize(config, ledger, args.app, attempt_id, outcome, wall_sec)
    finally:
        ledger.close()

    shared.append_run_log(args.app, {"job_name": job_name, **summary})
    print(json.dumps(summary, ensure_ascii=False))
    if summary["status"] == "done":
        return 0
    if summary["status"] == "needs_owner":
        return 2
    return 3


def cmd_fix(args: argparse.Namespace) -> int:
    """Continue the app's current job attempt after it failed at review
    or bench (engine.fixup.fix): closes that attempt (archiving, never
    discarding, its own merged-but-uncommitted work), opens a new
    attempt on the same job, restores the archived work into the new
    attempt's builder copy, and resubmits a run_station task composed
    from the original request plus any review findings / failing test
    output / `args.extra` the owner adds -- then runs the job's normal
    stage list through to done/needs_owner/failed, same as `run`.
    Refuses (exit 1, nothing changed) if the app's current attempt
    never reached a failed review/bench request with merge already
    done -- engine.fixup.FixupError's message says which.

    If the fixed attempt belongs to a full-orbit job (`gb2 new`/`gb2
    change`, as opposed to a plain `gb2 run` single-task job), the one
    task fixup.fix() repairs is never the whole job -- once it ends
    "done", engine.position.resume_after_fix continues the job through
    engine.position.resume_job: whatever plan.tasks are still queued,
    then acceptance, the requirement-coverage check, the handoff report,
    the app-wide security check, and (only once, for the WHOLE job) its
    single release request. For a plain single-task job it returns None
    and the fix outcome is the whole result."""
    config_path = shared.app_workspace(args.app) / "config.json"
    if not config_path.exists():
        print(json.dumps({"error": f"not initialized: run `gb2 init {args.app}` first"}),
             file=sys.stderr)
        return 1
    workspace.verify_app_repo_identity(args.app)
    config = EngineConfig.from_json(config_path)
    extra = args.extra or ""

    def fakes_for(attempt_id: str):
        return _build_fakes(config, args.app, attempt_id, None)

    ledger = Ledger.open(config.ledger_path)
    start = time.monotonic()
    try:
        from engine import position

        # fixup.fix decides on its own whether this task belongs to a
        # full-orbit job (and so must not release per task);
        # resume_after_fix then continues that job's remaining work, or
        # returns None for a plain single-task job.
        try:
            result = fixup.fix(config, ledger, args.app, extra, fakes_for,
                               on_progress=shared.progress_printer)
        except fixup.FixupError as exc:
            return shared.err_json(str(exc), 1)

        full_outcome = position.resume_after_fix(
            config, ledger, fakes_for, args.app, result, on_progress=shared.progress_printer,
        )
        wall_sec = time.monotonic() - start
        if full_outcome is not None:
            summary = shared.summarize_full_outcome(config, ledger, args.app, full_outcome, wall_sec)
        else:
            summary = _summarize(config, ledger, args.app, result.new_attempt_id,
                                 result.outcome, wall_sec)
        summary["old_attempt_id"] = result.old_attempt_id
        summary["task"] = result.task
    finally:
        ledger.close()

    shared.append_run_log(args.app, {"job_name": result.job_name, **summary})
    print(json.dumps(summary, ensure_ascii=False))
    if summary["status"] == "done":
        return 0
    if summary["status"] == "needs_owner":
        return 2
    return 3


# --------------------------------------------------------------------
# change / approve-design / continue / approve-install (full orbit)
# --------------------------------------------------------------------

def _run_small_job_and_report(args: argparse.Namespace, config: EngineConfig,
                              job_name: str, request: str) -> int:
    """`gb2 change <app> "..." --small`: engine.small_orbit.run_small_job
    against this app's workspace. If small mode refuses outright --
    engine.small_orbit.NEEDS_PROBE_REASON (facts.md names a real
    external dependency), UI_PLAN_REASON (plan.json sets "ui": true,
    which needs the full job's own designer/approval gate), or
    SINGLE_TASK_REASON (plan.json has more than one task) -- falls
    back to the ordinary full job (engine.full_orbit.run_full_job),
    via engine.small_orbit.maybe_full_fallback -- the SAME helper a
    later `gb2 continue` (engine.small_orbit.resume_small_plan) calls
    for this exact set of refusals (FULL_FALLBACK_REASONS), so a
    fallback discovered on a resume runs under the identical fallback
    job_name (engine.small_orbit.fallback_job_name) this initial call
    would have used. The owner still gets a real job out of a single
    `gb2 change --small` call, just not the fast path. Every refusal
    already closed+committed the small attempt itself
    (engine.small_orbit._close_preserving_evidence) before returning,
    so the project is CLOSED again here and this fallback's own
    open_job call succeeds. The printed summary carries "durations_ms"
    (engine.small_orbit's own per-station timing) either way; a
    fallback also carries "small_fallback_reason"."""
    from engine import small_orbit

    try:
        config, fakes_for = shared.fakes_builder(config, args.app)
    except ValueError as exc:
        return shared.err_json(str(exc), 1)

    ledger = Ledger.open(config.ledger_path)
    start = time.monotonic()
    try:
        outcome = small_orbit.run_small_job(
            config, ledger, fakes_for, args.app, job_name, request,
            on_progress=shared.progress_printer,
        )
        full_outcome = small_orbit.maybe_full_fallback(
            config, ledger, fakes_for, args.app, job_name, request, outcome,
            on_progress=shared.progress_printer,
        )
        if full_outcome is not None:
            job_name = small_orbit.fallback_job_name(job_name)
            wall_sec = time.monotonic() - start
            summary = shared.summarize_full_outcome(config, ledger, args.app, full_outcome, wall_sec)
            summary["durations_ms"] = {
                k: round(v, 1) for k, v in dict(outcome.durations_ms).items()
            }
            summary["small_fallback_reason"] = outcome.reason
        else:
            wall_sec = time.monotonic() - start
            summary = shared.summarize_small_outcome(config, ledger, args.app, outcome, wall_sec)
    finally:
        ledger.close()

    shared.append_run_log(args.app, {"job_name": job_name, **summary})
    print(json.dumps(summary, ensure_ascii=False))
    return shared.exit_code_for_full_outcome(str(summary["status"]))


def cmd_change(args: argparse.Namespace) -> int:
    """`gb2 change <app> "변경 요청"`: same full orbit as `gb2 new`,
    against an app `gb2 init`/`gb2 new` already set up -- never
    scaffolds or touches the repo's git history itself."""
    config_path = shared.app_workspace(args.app) / "config.json"
    if not config_path.exists():
        return shared.err_json(
            f"not initialized: run `gb2 new {args.app} \"...\"` or "
            f"`gb2 init {args.app}` first", 1,
        )
    request = shared.read_requirement(args)
    if not request:
        return shared.err_json("change request text (or --task-file) required", 1)
    doctor_error = ops.require_doctor_ok()
    if doctor_error:
        return shared.err_json(doctor_error, 1)
    workspace.verify_app_repo_identity(args.app)
    config = EngineConfig.from_json(config_path)

    job_name = args.job_name or f"gb2-change-{int(time.time())}"
    if args.small:
        return _run_small_job_and_report(args, config, job_name, request)
    return shared.run_full_job_and_report(args, config, job_name, request)


def cmd_approve_design(args: argparse.Namespace) -> int:
    """`gb2 approve-design <app>`: the owner's TTY-only approval gate
    for engine.gates.check_design_approved -- requires the owner to
    TYPE the exact "승인 <job>" line (never accepted non-interactively,
    refused with exit 4 before anything is read from stdin, same
    convention as `release_ops.cmd_ship`'s release approval), appends a
    `design.approved` ledger event carrying {"job": job_name}, then
    resumes the job with engine.full_orbit.continue_full_job -- which
    only makes progress past the design station once that event is
    there for this job (engine.gates.check_design_approved)."""
    config_path = shared.app_workspace(args.app) / "config.json"
    if not config_path.exists():
        return shared.err_json(f"not initialized: run `gb2 init {args.app}` first", 1)
    workspace.verify_app_repo_identity(args.app)
    if not sys.stdin.isatty():
        return shared.err_json("refused: non-interactive shell, approve-design requires a TTY", 4)
    config = EngineConfig.from_json(config_path)
    try:
        config, fakes_for = shared.fakes_builder(config, args.app)
    except ValueError as exc:
        return shared.err_json(str(exc), 1)

    from engine import full_orbit

    ledger = Ledger.open(config.ledger_path)
    try:
        project_row = ledger.project(args.app)
        attempt_id = project_row.current_attempt_id
        if attempt_id is None:
            return shared.err_json("no_active_job", 1)
        job_name = _job_name_for_attempt(ledger, args.app, attempt_id)

        owner_text = input(f'type the approval line exactly ("승인 {job_name}"): ')
        if owner_text != f"승인 {job_name}":
            return shared.err_json("refused: approval line did not match, nothing changed", 1)

        ledger.append(Event(
            project=args.app, kind="design.approved", actor_kind="owner",
            actor_id="owner", attempt_id=attempt_id, payload={"job": job_name},
        ))

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


def cmd_continue(args: argparse.Namespace) -> int:
    """`gb2 continue <app>`: resume the app's current job with
    engine.full_orbit.continue_full_job -- used after the owner has
    cleared whatever `needs_owner` reason stopped it last (an install
    approval, a release approval, anything other than the design-
    approval gate, which has its own `gb2 approve-design`)."""
    config_path = shared.app_workspace(args.app) / "config.json"
    if not config_path.exists():
        return shared.err_json(f"not initialized: run `gb2 init {args.app}` first", 1)
    workspace.verify_app_repo_identity(args.app)
    config = EngineConfig.from_json(config_path)
    try:
        config, fakes_for = shared.fakes_builder(config, args.app)
    except ValueError as exc:
        return shared.err_json(str(exc), 1)

    from engine import full_orbit

    ledger = Ledger.open(config.ledger_path)
    try:
        project_row = ledger.project(args.app)
        attempt_id = project_row.current_attempt_id
        if attempt_id is None:
            return shared.err_json("no_active_job", 1)
        job_name = _job_name_for_attempt(ledger, args.app, attempt_id)

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
    happens. Only once every pending request is confirmed "done" does
    this resume the job with engine.full_orbit.continue_full_job, which
    (since b0c8208) reruns the builder task that stopped from where it
    left off, rather than skipping straight to review."""
    config_path = shared.app_workspace(args.app) / "config.json"
    if not config_path.exists():
        return shared.err_json(f"not initialized: run `gb2 init {args.app}` first", 1)
    workspace.verify_app_repo_identity(args.app)
    if not sys.stdin.isatty():
        return shared.err_json("refused: non-interactive shell, approve-install requires a TTY", 4)
    config = EngineConfig.from_json(config_path)
    try:
        config, fakes_for = shared.fakes_builder(config, args.app)
    except ValueError as exc:
        return shared.err_json(str(exc), 1)

    from engine import full_orbit
    from engine import owner as owner_module

    ledger = Ledger.open(config.ledger_path)
    try:
        project_row = ledger.project(args.app)
        attempt_id = project_row.current_attempt_id
        if attempt_id is None:
            return shared.err_json("no_active_job", 1)
        job_name = _job_name_for_attempt(ledger, args.app, attempt_id)

        # Every owner action kind a station can propose (install,
        # scripts_change, test_config_change), still unfulfilled against
        # the current manifest.
        pending = [req for req in full_orbit.pending_owner_requests(
                       ledger, args.app, job_name, repo=config.projects[args.app].repo)
                   if req.get("kind") in owner_module.PENDING_KIND_WHITELIST]
        if not pending:
            return shared.err_json("no_pending_owner_request", 1)

        print(json.dumps({"job_name": job_name, "pending_owner_requests": pending},
                         ensure_ascii=False, indent=1, sort_keys=True), file=sys.stderr)

        owner_text = input(f'type the approval line exactly ("승인 {job_name}"): ')
        if owner_text != f"승인 {job_name}":
            return shared.err_json("refused: approval line did not match, nothing changed", 1)

        from engine import position as position_module

        stop = position_module.load_position(ledger, args.app, job_name)
        for request in pending:
            try:
                request_id = owner_module.approve_pending(
                    config, ledger, args.app, attempt_id, request,
                    confirm=True, confirm_source="tty",
                    proposal=(position_module.proposal_id(ledger, args.app, stop, request)
                              if stop is not None else None),
                )
            except ValueError as exc:
                return shared.err_json(f"approve_pending_refused: {exc}", 1)
            status = _drain(config, ledger, args.app, attempt_id, request_id)
            if status != "done":
                return shared.err_json(f"install_request_failed: {request_id} status={status}", 3)

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
