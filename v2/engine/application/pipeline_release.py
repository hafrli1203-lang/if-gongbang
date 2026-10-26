"""The job's one release track: recording why it ended the way it did
(shipped/skipped/not_configured/failed), submitting the job's single
publish request once every task is done, and turning that request's
own terminal status into the job's final FullOutcome.

Depends on engine.application.pipeline_base (FullOutcome/ProgressCallback) and
engine.application.pipeline_stages (close_attempt/open_with_intent/commit_job_
trail/record_event) -- never the other way; engine.application.pipeline_tasks
and engine.application.pipeline_resume call into this module for
maybe_release_or_done/prepare_release/finalize_release."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from engine.domain import faults
from engine.application import task_pipeline as orbit
from engine.config import EngineConfig
from engine.application.pipeline_base import FullOutcome, ProgressCallback
from engine.application.pipeline_stages import (
    close_attempt, commit_job_trail, open_with_intent, record_event,
)
from engine.domain.records import Event
from engine.application.job_state import (
    STAGE_POST_TASKS, STAGE_RELEASE_APPROVAL, JobPosition, load_position, record_position,
)
from engine.application.processor import Fakes

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger


def record_release_decision(ledger: Ledger, project: str, attempt_id: str, job_name: str,
                            decision: str) -> None:
    """Journals a `full_orbit.release_decision` event -- the one durable
    record of how this job's release track ended: "not_configured" (no
    `release` in this project's config -- maybe_release_or_done's own
    straight-to-done path), "shipped" (the pending publish request
    reached "done"), "skipped" (the owner abandoned it -- `skip-ship`,
    a full-bench refusal, a cancel-before-dispatch, or a security fix
    superseding it -- see finalize_release's own _RELEASE_ABANDON_
    REASONS), or "failed" (the publish request reached a terminal
    "failed"/"unknown" status for any OTHER reason -- a real deploy/
    fast-forward failure). `finalize_release` is the one place that
    decides which of these four applies and is the only caller that
    should ever invoke this directly; `ship`/`skip-ship`/continue_full_
    job's own STAGE_RELEASE_APPROVAL resume all go through it rather
    than calling this themselves, so the decision logic lives in
    exactly one place. Idempotent in effect (not in storage): callers
    check `release_decision_already_recorded` first, since this
    itself always appends a fresh event. Without this, a release-
    configured job's own completion was only ever visible as engine.
    orbit's own JobOutcome from that one `continue_job` call -- nothing
    on the ledger ever said the WHOLE full-orbit job (not just this one
    attempt) had reached a terminal state, and a project with no
    release configured reached "done" with no record at all of why
    there was nothing left to approve."""
    faults.flow_boundary("full_orbit.release_decision")
    ledger.append(Event(
        project=project, kind="full_orbit.release_decision", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"job": job_name, "decision": decision},
    ))



# Every reason a publish request's own "result" dict carries when it was
# driven to a terminal non-"done" status by a deliberate owner/engine
# ABANDON -- `cancel_pending_publish`/`cancel_running_publish_before_
# dispatch` (engine.application.release), called with each of these exact strings --
# never a reason recorded by an actual dispatch failure (e.g.
# advance_master/call_publisher raising StepFailed with some OTHER
# reason, like "artifact_mismatch" or a real deploy exit code). Read by
# finalize_release to tell "the owner chose not to ship this" (decision
# "skipped") apart from "the release genuinely failed" (decision
# "failed") -- both reach the SAME terminal request status
# ("failed"/"unknown"), so the status alone can never tell them apart.
RELEASE_ABANDON_REASONS = frozenset({
    "skip_ship", "full_bench_failed", "cancelled_before_dispatch", "superseded_by_fix",
})



def write_final_report(config: EngineConfig, ledger: Ledger, project: str, attempt_id: str,
                        job_name: str, decision: str) -> None:
    """report.md's final section (security result, release decision,
    evidence completeness -- engine.application.handoff.write_final_section, which
    rewrites the same section when called again), committed to the job
    trail. A job with no handoff report (a plain `gb2 run` released by
    `gb2 ship`) has no report to finish."""
    from engine import handoff as handoff_mod

    if not handoff_mod.report_path(config, project, job_name).exists():
        return
    handoff_mod.write_final_section(config, ledger, project, job_name, decision)
    commit_job_trail(config, ledger, project, attempt_id, job_name,
                      f"gongbang: {job_name} - final report")



def release_decision_for_request(request_row: "object") -> str:
    """"shipped" (status "done"), "skipped" (a terminal non-"done"
    status whose own result.reason is one of RELEASE_ABANDON_REASONS),
    or "failed" (terminal non-"done" for any other reason -- a real
    deploy/fast-forward failure, e.g. "artifact_mismatch" or a
    nonzero-exit deploy script)."""
    if request_row.status == "done":  # type: ignore[attr-defined]
        return "shipped"
    result = request_row.result  # type: ignore[attr-defined]
    reason = result.get("reason") if isinstance(result, dict) else None
    if reason in RELEASE_ABANDON_REASONS:
        return "skipped"
    return "failed"



def release_decision_already_recorded(ledger: Ledger, project: str, job_name: str,
                                       attempt_id: str) -> bool:
    for _seq, event in ledger.events(project, "full_orbit.release_decision"):
        if event.attempt_id == attempt_id and event.payload.get("job") == job_name:
            return True
    return False



def finalize_release(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                     attempt_id: str, job_name: str, request_id: str, report_path: str,
                     on_progress: ProgressCallback | None = None,
                     processor_factory: "orbit.ProcessorFactory | None" = None) -> FullOutcome:
    """The ONE place a release-configured job's publish request --
    shipped (`gb2 ship` drove it to "done"), abandoned (`gb2 skip-ship`,
    a full-bench refusal, a cancel-before-dispatch, or a security fix
    superseding it), or genuinely failed (any other terminal non-"done"
    status) -- is turned into this job's final FullOutcome: report ->
    close (close_attempt, idempotent -- engine.application.processor.submit/
    run_once's own content-hash idempotency makes a second call on an
    already-"done" report/close request a no-op) THEN record_release_
    decision (idempotent in effect: never a second event for the same
    (project, job_name, attempt_id), see _release_decision_already_
    recorded), in that fixed order, no matter which of the three ways
    the request actually ended. `gb2 ship`/`gb2 skip-ship` (tools/gb2)
    and the STAGE_RELEASE_APPROVAL resume (a crash between the publish
    request reaching a terminal status and this function ever running)
    all call this, so the decide-close-record sequence exists once and
    a crash in that window still ends with the project closed and the
    decision recorded.

    request_id's own status not yet terminal (still "queued"/
    "running") is a plain no-op: returns FullOutcome("needs_owner",
    "release_approval", attempt_id, None) without touching the ledger
    at all -- the owner has not finished approving/rejecting yet (a
    `gb2 ship` call that typed the wrong approval line, for instance,
    leaves the request exactly like this; calling this function again
    after the owner fixes that is always safe)."""
    request_row = ledger.request(request_id)
    if request_row.status not in ("done", "failed", "unknown"):
        return FullOutcome("needs_owner", "release_approval", attempt_id, None)
    decision = release_decision_for_request(request_row)
    # close_attempt's own report/close stages are each individually
    # idempotent (engine.application.processor.submit/run_once's content-hash
    # idempotency, which now also tolerates reusing an already-
    # submitted request on a no-longer-active attempt -- see
    # engine.application.processor.submit's own docstring), so calling it again on a
    # job this function (or a same-process caller's own earlier
    # close_attempt) already closed is a safe no-op. What actually
    # needs skipping is the whole project already reaching CLOSED --
    # checking the ATTEMPT's own status instead (as this used to) is
    # wrong: engine.application.steps.close's set_closed flips the attempt to
    # "closed"/"abandoned" as its own FIRST effect, before the project's
    # own CLOSING -> CLOSED transition (its second effect) necessarily
    # ran -- a crash in exactly that window left the attempt non-active
    # but the project stuck CLOSING forever, and the old check here
    # silently treated that as "already closed", never finishing it.
    if ledger.project(project).state != "CLOSED":
        close_outcome = close_attempt(config, ledger, fakes, project, attempt_id, job_name,
                                       on_progress, processor_factory)
        if close_outcome.status != "done":
            return FullOutcome("failed", f"close:{close_outcome.reason}", attempt_id, None)
    if not release_decision_already_recorded(ledger, project, job_name, attempt_id):
        record_release_decision(ledger, project, attempt_id, job_name, decision)
    write_final_report(config, ledger, project, attempt_id, job_name, decision)
    if decision == "failed":
        return FullOutcome("failed", "release_rejected", attempt_id, None)
    return FullOutcome("done", None, attempt_id, report_path)



def maybe_release_or_done(config: EngineConfig, ledger: Ledger, project: str, job_name: str,
                           attempt_id: str, report_path: Path, max_fix_rounds: int) -> FullOutcome:
    """The LAST step of a whole job (ENGINE.md release track, an earlier round
    contract item 6): a project with no `release` configured reaches
    "done" straight away, exactly as before. A project WITH `release`
    configured instead submits ONE publish request for the whole job
    (engine.application.release.prepare, bound to `attempt_id`'s own current repo
    HEAD -- by now that HEAD already carries every task's commits plus
    the handoff commit) and stops needs_owner "release_approval";
    gb2's ship/skip-ship flow (outside this module) drives that request
    to done/failed, after which the STAGE_RELEASE_APPROVAL resume turns
    it into this job's final FullOutcome."""
    record_event(ledger, project, job_name, STAGE_POST_TASKS, attempt_id, next_action="release",
            max_fix_rounds=max_fix_rounds)
    release_config = config.projects[project].release
    if release_config is None:
        record_release_decision(ledger, project, attempt_id, job_name, "not_configured")
        write_final_report(config, ledger, project, attempt_id, job_name, "not_configured")
        return FullOutcome("done", None, attempt_id, str(report_path))
    # engine.application.release.prepare's own submit() requires an ACTIVE attempt --
    # `attempt_id` (the last task/fix attempt) already closed by the time
    # this runs (its own "close" stage already ran). Open a fresh, roleless
    # attempt purely to carry this one publish request -- "a final attempt
    # bound to the final commit": its base_head is
    # this exact repo HEAD (every task's commit plus the handoff commit),
    # and it is left OPEN (never closed here) while the job waits for the
    # owner's approval -- the release-approval position names it.
    intent = JobPosition(job=job_name, stage=STAGE_RELEASE_APPROVAL, attempt_id=None,
                         stop_reason="release_approval",
                         extra={"report_path": str(report_path),
                                "max_fix_rounds": max_fix_rounds})
    release_attempt_id = open_with_intent(config, ledger, project, intent, ())
    return prepare_release(ledger, config, project, replace(intent, attempt_id=release_attempt_id))



def prepare_release(ledger: Ledger, config: EngineConfig, project: str,
                     pos: JobPosition) -> FullOutcome:
    """Submits the job's one publish request on the release attempt
    (engine.application.release.prepare: the same HEAD always yields the same
    request) and records the release-approval stop naming it."""
    from engine.application import release as release_mod

    request_id, _summary = release_mod.prepare(config, ledger, project, pos.attempt_id or "")
    record_position(ledger, project, replace(pos, extra={**pos.extra, "request_id": request_id}))
    return FullOutcome("needs_owner", "release_approval", pos.attempt_id, None)



def pending_release_progress(ledger: Ledger, project: str,
                             job_name: str) -> dict[str, object] | None:
    """The release-approval stop this job is waiting on, if that is where
    it is (read by `gb2 resolve` for the report_path)."""
    pos = load_position(ledger, project, job_name)
    if pos is None or pos.stage != STAGE_RELEASE_APPROVAL:
        return None
    return {"stage": pos.stage, "attempt_id": pos.attempt_id, **pos.extra}


__all__ = [
    "record_release_decision", "finalize_release", "maybe_release_or_done", "prepare_release",
    "pending_release_progress",
]
