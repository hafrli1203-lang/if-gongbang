"""`gb2 watch`'s engine-side judgment: one verdict per app, computed from
the ledger alone (never from a gb2-owned file like runs.json -- this
module is application layer and must not depend on tools/gb2's own
state).

Background (the real incident this exists for): an operator wrapped a
`gb2 run`/`gb2 continue` call in a shell timeout. The wrapper killed the
engine process mid-dispatch, but the request it was running stayed
"running" in the ledger forever -- nothing else was watching, so the
job sat stuck for three hours before anyone noticed. `judge_app` below
is the one place that tells a genuinely live driver (or a live worker
subprocess it spawned) apart from a project whose ledger still records
"running"/"queued" work with nothing left alive to finish it.

Five verdicts, in the priority order `classify` checks them,
a review -- see `classify`'s own docstring for the two bugs this
reorder/widening fixes):
  running       -- a live driver (the ledger's own project-lock holder,
                    a real pid still alive on this host -- see
                    engine.infrastructure.db.ledger.Ledger.lock_pid) OR a live
                    worker subprocess (engine.infrastructure.db.ledger.Ledger.
                    live_procs for the project's current attempt, any
                    pid still alive).
  failed (gave up) -- `gb2 watch --loop --resume` already hit
                    watch_max_resumes_per_hour for this app and recorded
                    a "watch.gave_up" event with no "watch.resumed"
                    since (see `gave_up_pending`): watch must report
                    this app as failed, needing the foreman, not
                    "stalled" (which would imply it is still safe to
                    keep auto-resuming) -- even while its stuck request
                    is technically still "queued"/"running".
  waiting_owner -- the newest job.position stage is one of the
                    "awaiting_*" stages (engine.application.job_state.STAGES), or
                    some pending owner request is still outstanding
                    (job_state.pending_owner_requests) -- a human
                    decision, never something `gb2 watch --resume`
                    may act on. Checked BEFORE "stalled" (reordered --
                    a queued release-approval request, e.g. a `ship`
                    waiting on the owner, used to be misjudged
                    "stalled" purely because Ledger.queued_requests saw
                    it still queued, triggering pointless auto-resumes
                    before `gb2 watch --loop --resume` gave up on an app
                    that was never actually stuck).
  stalled       -- queued/running requests exist (Ledger.queued_requests),
                    OR the newest job.position recorded a stage with NO
                    request ever submitted under its own attempt (a
                    crash between recording the position and submitting
                    the request that stage was about to make -- see
                    `attempt_has_submitted_request`) -- either way,
                    nothing above is alive: the exact crash this module
                    exists to catch.
  failed        -- none of the above, and the project's most recently
                    submitted request ended "failed" -- a genuine
                    engine failure (not a stop for the owner) that
                    needs the foreman's judgment, not a retry loop.
  idle          -- none of the above (including a project that was
                    never OPEN, or finished "done" with nothing left to
                    do).

`classify` itself takes only plain booleans/strings -- no ledger or pid
access of its own -- so every branch is exercised in a unit test with
no fake ledger at all; `judge_app` is the thin, ledger-reading wrapper
tools/gb2lib/watch_ops.py actually calls."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from engine.domain.records import LedgerError
from engine.application.job_state import STAGES, load_position, pending_owner_requests
from engine.infrastructure import procs as proc_lib

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger

# Every stage a job stops at FOR THE OWNER (engine.application.job_state's own
# comment: "Stages a job stops at for the owner") -- derived from the
# naming convention every one of those stage values already follows
# ("awaiting_...") rather than a second hand-maintained list that could
# drift from STAGES itself.
AWAITING_STAGES: tuple[str, ...] = tuple(s for s in STAGES if s.startswith("awaiting_"))

VERDICTS: tuple[str, ...] = ("running", "stalled", "waiting_owner", "failed", "idle")


@dataclass(frozen=True)
class WatchVerdict:
    app: str
    verdict: str
    stage: str | None
    task_id: str | None
    open_requests: int
    since_last_event_sec: float


def _job_name_for_attempt(ledger: "Ledger", project: str, attempt_id: str) -> str | None:
    """job_name of the job.opened event that created `attempt_id`, or
    None if there is none. This module's own small copy of the same
    lookup tools/gb2lib/ops.py, engine.application.repair_target and others each
    already keep privately -- see repair_target._job_name_for_attempt's
    own docstring for why this codebase duplicates it per module rather
    than importing another module's private name across a boundary."""
    for _seq, event in ledger.events(project, "job.opened"):
        if event.payload.get("attempt_id") == attempt_id:
            return str(event.payload.get("job_name"))
    return None


def driver_alive(ledger: "Ledger", project: str) -> bool:
    """True iff the project's own processor lock (Ledger.acquire_lock/
    release_lock keyed by the plain project name -- never a composite
    key like "install:{project}", a different lock entirely) is
    currently held by a pid still alive on this host."""
    pid = ledger.lock_pid(project)
    if pid is None:
        return False
    return proc_lib.is_alive(pid)


def worker_alive(ledger: "Ledger", attempt_id: str | None) -> bool:
    """True iff any procs row left open (ended_ts IS NULL) for this
    attempt still has a live pid -- a spawned worker/reviewer/bench
    subprocess the driver itself may have already died without
    reaping."""
    if attempt_id is None:
        return False
    for pid, _pgid in ledger.live_procs(attempt_id):
        if proc_lib.is_alive(pid):
            return True
    return False


def _last_attempt_request_ids(ledger: "Ledger", project: str) -> list[str]:
    """Request ids of the attempt that submitted the most recent request
    -- review and bench run in parallel inside one attempt, so the last
    submitted request alone is not the attempt's outcome."""
    by_attempt: dict[str, list[str]] = {}
    last_attempt: str | None = None
    for _seq, event in ledger.events(project, "request.submitted"):
        request_id = event.payload.get("request_id")
        if not isinstance(request_id, str) or event.attempt_id is None:
            continue
        by_attempt.setdefault(event.attempt_id, []).append(request_id)
        last_attempt = event.attempt_id
    return by_attempt.get(last_attempt, []) if last_attempt is not None else []


def _attempt_has_submitted_request(ledger: "Ledger", project: str,
                                   attempt_id: str | None) -> bool:
    """True iff `attempt_id` itself (the CURRENT attempt, per
    ProjectRow.current_attempt_id) has at least one request.submitted
    event of its own. None for `attempt_id` (no current attempt at all)
    counts as True -- the default/common case classify's own
    `attempt_has_submitted_request` default already assumes, and there
    is no "position recorded with no request yet" crash window to catch
    without a current attempt in the first place."""
    if attempt_id is None:
        return True
    for _seq, event in ledger.events(project, "request.submitted"):
        if event.attempt_id == attempt_id:
            return True
    return False


def _last_request_failed(ledger: "Ledger", project: str) -> bool:
    """True iff any request of the most recent attempt ended in status
    "failed" -- the ledger-native signal of a genuine, unrecovered
    engine failure: a job that stopped for the owner (needs_owner)
    leaves its stopping request pending or done, never failed; only a
    request the engine itself gave up on (review/bench exceeding
    max_fix_rounds, an invalid plan, ...) does. Every fix round opens a
    new attempt, so an earlier round's failure never counts here."""
    for request_id in _last_attempt_request_ids(ledger, project):
        try:
            if ledger.request(request_id).status == "failed":
                return True
        except LedgerError:
            continue
    return False


def classify(*, driver_is_alive: bool, worker_is_alive: bool, open_request_count: int,
            stage: str | None, pending_owner: bool, last_failed: bool,
            gave_up_pending: bool = False,
            attempt_has_submitted_request: bool = True) -> str:
    """The pure decision table -- see this module's own docstring for
    the five verdicts and their priority order. Takes only
    already-gathered facts (no ledger/pid access of its own), so every
    branch is exercised with plain booleans in a unit test.

    `attempt_has_submitted_request`, a review): False
    only when `stage` was just recorded by job_state.record_position for
    the CURRENT attempt and that attempt has not yet submitted a single
    request.submitted event -- the crash window between "wrote where
    this job is" and "asked the processor to do that work". Defaults to
    True (the common case) so every existing caller that never passes
    it keeps the old stalled/failed split for a stage WITH requests
    (e.g. a stage="task" position whose own request ended "failed" must
    still classify as "failed", never "stalled" -- see
    test_failed_when_last_request_failed_and_nothing_pending)."""
    if driver_is_alive or worker_is_alive:
        return "running"
    if gave_up_pending:
        return "failed"
    if pending_owner or (stage is not None and stage in AWAITING_STAGES):
        return "waiting_owner"
    if open_request_count > 0 or (stage is not None and not attempt_has_submitted_request):
        return "stalled"
    if last_failed:
        return "failed"
    return "idle"


def gave_up_pending(ledger: "Ledger", project: str, *,
                    project_state: str | None = None) -> bool:
    """True iff `gb2 watch --loop --resume` already gave up on this app
    (a "watch.gave_up" event) and nothing has recovered it since -- see
    this module's own docstring's "failed (gave up)" verdict. False once
    any of these follow the give-up:
      - a NEWER "watch.resumed" event (the owner widened the limit, or
        something else resubmitted the same stuck work);
      - a NEWER "job.opened" event, a review -- the
        owner manually started fresh work on this project without ever
        going through `gb2 watch --resume`, e.g. `gb2 run` again; the
        old give-up must not keep reporting "failed" forever over a
        project that has since moved on);
      - `project_state == "CLOSED"` (the owner finished the job by hand
        -- engine.domain.state's own ("OPEN","close_job")->"CLOSING"->
        "CLOSED" transition is the one ledger-visible fact that a human
        closed it out, so there is nothing left pending to give up on).
    `project_state` is the caller's already-fetched
    Ledger.project(project).state (judge_app already holds it) -- never
    re-fetched here, so a caller that omits it simply never clears via
    that third path."""
    gave_up_ts = ledger.event_timestamps(project, "watch.gave_up")
    if not gave_up_ts:
        return False
    cutoff = max(gave_up_ts)
    resumed_ts = ledger.event_timestamps(project, "watch.resumed")
    if resumed_ts and max(resumed_ts) > cutoff:
        return False
    opened_ts = ledger.event_timestamps(project, "job.opened")
    if opened_ts and max(opened_ts) > cutoff:
        return False
    if project_state == "CLOSED":
        return False
    return True


def judge_app(ledger: "Ledger", project: str, *, now: float | None = None) -> WatchVerdict:
    """One verdict for `project`, read entirely from its own ledger (and
    this host's live process table). `now`, when given, is the
    reference time `since_last_event_sec` is measured against (tests
    pass a fixed value; a real caller leaves it as time.time())."""
    now = time.time() if now is None else now
    try:
        project_row = ledger.project(project)
    except LedgerError:
        return WatchVerdict(project, "idle", None, None, 0, 0.0)

    attempt_id = project_row.current_attempt_id
    stage: str | None = None
    task_id: str | None = None
    pending_owner = False
    if attempt_id is not None:
        job_name = _job_name_for_attempt(ledger, project, attempt_id)
        if job_name is not None:
            pos = load_position(ledger, project, job_name)
            if pos is not None:
                stage = pos.stage
                task_id = pos.task_id
                pending_owner = bool(pending_owner_requests(ledger, project, job_name))

    open_requests = ledger.queued_requests(project)
    verdict = classify(
        driver_is_alive=driver_alive(ledger, project),
        worker_is_alive=worker_alive(ledger, attempt_id),
        open_request_count=len(open_requests),
        stage=stage, pending_owner=pending_owner,
        last_failed=_last_request_failed(ledger, project),
        gave_up_pending=gave_up_pending(ledger, project, project_state=project_row.state),
        attempt_has_submitted_request=_attempt_has_submitted_request(ledger, project, attempt_id),
    )
    last_ts = ledger.last_event_ts(project)
    since_last_event_sec = max(0.0, now - last_ts) if last_ts is not None else 0.0
    return WatchVerdict(project, verdict, stage, task_id, len(open_requests),
                        since_last_event_sec)


__all__ = [
    "WatchVerdict", "VERDICTS", "AWAITING_STAGES", "classify", "judge_app",
    "driver_alive", "worker_alive", "gave_up_pending",
]
