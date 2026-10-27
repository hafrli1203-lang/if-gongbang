"""Single processor: takes queued requests one at a time and runs their
fixed step list with a step journal, so a crash at any boundary resumes
without repeating completed effects.

Resume rule for a step found in the journal:
  done    -> skip
  started -> call reconcile(ctx); True => mark done and skip,
             False => run again
  absent  -> run
After run(): faults.maybe_crash(step) then journal 'done'.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from engine.domain import ids
from engine.config import EngineConfig
from engine.domain.faults import CrashInjected, Faults
from engine.domain.records import Event, LedgerError, RequestRow

if TYPE_CHECKING:
    from engine.infrastructure.installers import Installer
    from engine.infrastructure.fake_publisher import FakePublisher
    from engine.infrastructure.workers.base import WorkerRunner
    from engine.infrastructure.db.ledger import Ledger


_CLOSE_LIKE_KINDS = ("close", "abandon")

# The step whose run() actually dispatches an external, possibly
# non-idempotent effect for each kind -- used by _run_one's catch-all to
# tell "this blew up before touching the outside world" (safe to fail
# outright) apart from "this blew up after the effect may have already
# happened" (must become 'unknown' so retry_unknown can reconcile, not a
# silent 'failed' that could retry the external effect from scratch via
# a brand-new request).
_DISPATCH_STEP_BY_KIND = {
    "publish": "call_publisher",
    "install": "run_installer",
    "scripts_change": "run_installer",
    "test_config_change": "run_installer",
    "commit": "git_commit",
    "run_station": "spawn_worker",
    "review": "spawn_reviewer",
}

# The fixed set of request kinds the engine knows how to run (mirrors
# engine.application.steps.KINDS' keys, kept as a literal tuple here instead of
# importing engine.application.steps at module scope to avoid a circular import --
# register_defaults() already imports it lazily for the same reason).
# A kind outside this set must never become a queued request: unknown
# step lists turn into StepUnknown -> 'unknown' status, which blocks the
# whole project (_block_on_unknown) until an owner notices and retries
# something that can never succeed.
KNOWN_KINDS = (
    "close", "abandon", "commit", "publish", "install",
    "scripts_change", "test_config_change", "merge", "report", "bench",
    "run_station", "review",
)


class ProcessorBusy(Exception):
    """Another processor holds the project lock."""


class RetryError(Exception):
    """retry_unknown() called on a request that is not 'unknown'."""


class CancelError(Exception):
    """cancel_request() called on a request that is not 'queued'."""


class StepFailed(Exception):
    """A step determined the request cannot succeed. str(exc) is the reason
    recorded on the request (status 'failed')."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class StepUnknown(Exception):
    """A step could not determine the outcome (e.g. an external call's
    response never arrived and a status lookup also failed). str(exc) is
    the reason recorded on the request (status 'unknown'); processing of
    further queued requests stops for this run_once call."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Fakes:
    publisher: FakePublisher
    installer: Installer
    # runner name ("claude" | "codex" | "scripted", WorkerConfig.runner) ->
    # the WorkerRunner that knows how to spawn/parse that kind of worker.
    runners: dict[str, WorkerRunner] = field(default_factory=dict)


@dataclass
class StepContext:
    config: EngineConfig
    ledger: Ledger
    fakes: Fakes
    request: RequestRow
    project: str
    attempt_id: str

    def journal(self, step: str, status: str, payload: dict[str, object] | None = None) -> None:
        """Append request.step for this request."""
        self.ledger.append(Event(
            project=self.project, kind="request.step", actor_kind="processor",
            actor_id="processor", attempt_id=self.attempt_id,
            payload={"request_id": self.request.request_id, "step_name": step,
                      "status": status, "payload": payload or {}},
        ))


@dataclass(frozen=True)
class StepDef:
    name: str
    run: Callable[[StepContext], dict[str, object]]
    reconcile: Callable[[StepContext], bool]


StepList = Callable[[StepContext], list[StepDef]]


class Processor:
    def __init__(self, config: EngineConfig, ledger: Ledger, fakes: Fakes,
                 faults: Faults | None = None, holder: str | None = None) -> None:
        self.config = config
        self.ledger = ledger
        self.fakes = fakes
        self.faults = faults or Faults()
        # Each Processor instance needs its own lock identity: a static
        # default like "processor" would make two Processor() instances
        # in the same process indistinguishable to acquire_lock/
        # release_lock (same-holder "re-acquire" would return True for
        # the second one, and either could release the other's lock).
        self.holder = holder if holder is not None else f"processor-{uuid.uuid4().hex[:8]}"
        self._steps: dict[str, StepList] = {}

    def register(self, kind: str, steps: StepList) -> None:
        self._steps[kind] = steps

    def register_defaults(self) -> None:
        """Register engine.application.steps.* step lists for every request kind."""
        from engine.application.steps import KINDS
        for kind, steps_fn in KINDS.items():
            self.register(kind, steps_fn)

    def run_request(self, project: str, request_id: str,
                    stop_after: str | None = None) -> str:
        """Process exactly one request by id, not the whole queue --
        used by engine.application.task_pipeline's parallel_review_bench speed default to
        run a review's dispatch step (spawn_reviewer -- forking codex/
        claude as a child process) and a bench request concurrently in
        wall-clock time, while still giving every ledger write the same
        single-processor guarantee run_once gives: this method acquires
        and releases the project lock around the whole call exactly
        like run_once does, so two run_request calls (even from two
        different Processor instances) can never mutate the ledger at
        the same instant -- what overlaps is only the reviewer's
        unsupervised external child process running while THIS call
        works on a different request (e.g. bench).

        `stop_after`, if given, must name one of this request's own
        step names. Once that step's run() has journaled 'done', this
        method returns immediately: the request is left 'running' (no
        terminal status set), exactly as if a crash had landed right
        after that step -- a later run_request/run_once call resumes it
        from the journal via the ordinary resume rule (module
        docstring). Returns the request's resulting status: a terminal
        one ("done"/"failed"/"unknown") if the whole step list ran (or
        if the request was already terminal before this call -- a
        no-op), "running" if stop_after cut it short, or "queued" if
        _block_on_unknown refused to touch this project's requests at
        all (some OTHER request for the project is 'unknown').

        `request_id` must actually belong to `project` -- checked by a
        plain, lock-free read BEFORE acquiring any lock or touching the
        ledger, so a mismatched pair is rejected with no mutation at
        all; the lock this method then acquires is the REQUEST's own
        project (by this point proven equal to `project`), never the
        caller-supplied value taken on faith, so passing project "A"
        for a request that actually belongs to project "B" can never
        run that request under project "A"'s lock while a genuine "B"
        processor also believes it holds exclusive access to it."""
        request = self.ledger.request(request_id)
        if request.project != project:
            raise ValueError(
                f"request {request_id!r} belongs to project {request.project!r}, "
                f"not {project!r}"
            )
        if not self.ledger.acquire_lock(request.project, self.holder):
            raise ProcessorBusy(f"project {request.project!r} locked by another processor")
        try:
            if self._block_on_unknown(request.project):
                return self.ledger.request(request_id).status
            request = self.ledger.request(request_id)
            if request.status == "queued":
                self._set_status(request, "running", {})
                request = self.ledger.request(request_id)
            if request.status != "running":
                return request.status  # already terminal: no-op
            outcome = self._run_one(request, stop_after=stop_after)
            if outcome == "stopped":
                return "running"
            return self.ledger.request(request_id).status
        finally:
            self.ledger.release_lock(request.project, self.holder)

    def run_once(self, project: str) -> int:
        """Acquire the lock (ProcessorBusy if held), process queued/running
        requests in order, release the lock. Returns number processed.
        A CrashInjected propagates after releasing the lock; the request
        stays 'running' for the next run to resume.

        If any request for this project is 'unknown', nothing is
        processed at all (see _block_on_unknown)."""
        if not self.ledger.acquire_lock(project, self.holder):
            raise ProcessorBusy(f"project {project!r} locked by another processor")
        done_count = 0
        try:
            if self._block_on_unknown(project):
                return done_count
            for request in self.ledger.queued_requests(project):
                if request.status == "queued":
                    self._set_status(request, "running", {})
                outcome = self._run_one(request)
                if outcome is None:
                    break  # request went 'unknown': stop this run_once
                if outcome:
                    done_count += 1
        finally:
            self.ledger.release_lock(project, self.holder)
        return done_count

    def _block_on_unknown(self, project: str) -> bool:
        """If any request for `project` is 'unknown', append
        'processor.blocked' and return True: an unknown outcome must be
        resolved by the owner (retry_unknown) before the queue moves on,
        otherwise a later request could e.g. close the attempt and turn
        the stalled retry into 'stale_generation' instead of actually
        resolving it."""
        blocking = self.ledger.unknown_requests(project)
        if not blocking:
            return False
        first = blocking[0]
        self.ledger.append(Event(
            project=project, kind="processor.blocked",
            actor_kind="processor", actor_id=self.holder,
            attempt_id=first.attempt_id,
            payload={"reason": "unknown_request", "request_id": first.request_id},
        ))
        return True

    def _run_one(self, request: RequestRow,
                stop_after: str | None = None) -> bool | None | str:
        """Run one request via process() and record its outcome. Returns
        True (done), False (failed, keep draining the queue), None
        (became 'unknown' -- run_once must stop for this call), or the
        string "stopped" (only possible when stop_after is given --
        run_once itself never passes one -- process() returned False,
        meaning it stopped right after journaling stop_after's step as
        'done' rather than running the rest of the step list; no
        terminal status is set, the request stays 'running')."""
        try:
            completed = self.process(request, stop_after)
        except StepFailed as exc:
            self._set_status(request, "failed", {"reason": exc.reason})
            return False
        except StepUnknown as exc:
            self._set_status(request, "unknown", {"reason": exc.reason})
            return None
        except CrashInjected:
            raise
        except Exception as exc:  # noqa: BLE001 - any other bug in a step
            # (KeyError, LedgerError, TransitionError, a subprocess or
            # filesystem error, ...) must not leave this request 'running'
            # forever and block the queue.
            if self._dispatch_may_have_run(request):
                # A later step blew up AFTER the external effect (publish/
                # install/commit) was already dispatched: 'failed' would
                # let the owner resubmit and dispatch it again. 'unknown'
                # routes this through retry_unknown, which reconciles
                # instead of re-dispatching.
                self._set_status(request, "unknown", {
                    "reason": f"internal:{type(exc).__name__}",
                    "detail": str(exc)[:300],
                })
                return None
            self._set_status(request, "failed", {
                "reason": f"internal:{type(exc).__name__}",
                "detail": str(exc)[:300],
            })
            return False
        if not completed:
            # stop_after's step just journaled 'done' and process()
            # returned early without raising: leave the request
            # 'running', set no terminal status -- run_request resumes
            # it later via the ordinary journal resume rule.
            return "stopped"
        result = {
            name: row.payload
            for name, row in self.ledger.steps(request.request_id).items()
        }
        self._set_status(request, "done", result)
        return True

    def _dispatch_may_have_run(self, request: RequestRow) -> bool:
        """True if this kind has a dispatch step and the journal shows it
        'started' or 'done' for this request (its run() executed, so the
        external effect may already have happened)."""
        dispatch_step = _DISPATCH_STEP_BY_KIND.get(request.kind)
        if dispatch_step is None:
            return False
        row = self.ledger.steps(request.request_id).get(dispatch_step)
        return row is not None and row.status in ("started", "done")

    def process(self, request: RequestRow, stop_after: str | None = None) -> bool:
        """Run one request's step list with the journal/resume rule, after
        _check_allowed()'s guards. Returns True if every step ran (the
        normal case) or False if `stop_after` named a step that was
        just journaled 'done' and this returned before running any
        step after it (see Processor.run_request)."""
        attempt = self.ledger.attempt(request.attempt_id)
        project_row = self.ledger.project(request.project)
        self._check_allowed(request, attempt, project_row)
        steps_fn = self._steps.get(request.kind)
        if steps_fn is None:
            # Not StepUnknown: an unsupported/unregistered kind can never
            # resolve itself (there is no external effect to look up), so
            # leaving it 'unknown' would block the whole project forever
            # (_block_on_unknown) waiting for a retry that can't help.
            raise StepFailed("unsupported_kind")
        ctx = StepContext(
            config=self.config, ledger=self.ledger, fakes=self.fakes,
            request=request, project=request.project, attempt_id=request.attempt_id,
        )
        return self._run_steps(ctx, steps_fn(ctx), stop_after)

    def _check_allowed(self, request: RequestRow, attempt, project_row) -> None:
        """Guards evaluated before any step runs: the attempt must be
        'active' (a closed/abandoned attempt's requests never execute) --
        except for the close/abandon request's OWN later steps, since
        set_closed already marks the attempt closed/abandoned before the
        remaining steps (open_queue) run, and a crash-resume after
        set_closed must still be able to finish them; the attempt's
        generation must not be stale; and the project state must allow
        this request kind (close/abandon run while OPEN or RECOVERING so
        a recovering attempt can still be closed; every other kind
        requires OPEN, e.g. a request queued before close_job must not
        run once the project has moved to CLOSED).

        An 'interrupted' attempt (recover_job opened a new attempt and
        left this one behind, engine.application.steps.open_job.recover_job) is
        checked BEFORE all of the above and on its own terms: only a
        request whose kind's DISPATCH step may already have run
        (_dispatch_may_have_run -- the same check _run_one uses to tell
        'failed' apart from 'unknown') may still run, only while the
        project is RECOVERING, and never a close/abandon (those belong
        to the new attempt). "Some step is journaled" is not enough on
        its own: a kind with no dispatch step, or a request journaled
        only up to a step BEFORE its dispatch step (e.g. install's
        acquire_project_lock/snapshot_manifest, before run_installer),
        has no external effect yet to reconcile -- continuing it under
        the interrupted attempt would just be doing fresh work on an
        attempt the new one is supposed to supersede. Such a request
        skips the generation check below entirely -- the generation
        fence is for the *new* attempt's own requests; this is the
        processor catching up on an external effect the interrupted
        attempt already dispatched. Anything else (no dispatch step at
        all, or dispatch not yet started) is refused outright."""
        if attempt.status == "interrupted":
            if (request.kind not in _CLOSE_LIKE_KINDS
                    and project_row.state == "RECOVERING"
                    and self._dispatch_may_have_run(request)):
                return
            raise StepFailed("attempt_interrupted")
        if request.kind not in _CLOSE_LIKE_KINDS and attempt.status != "active":
            raise StepFailed("attempt_not_active")
        if attempt.generation < project_row.generation:
            raise StepFailed("stale_generation")
        # close/abandon's own steps move the project OPEN/RECOVERING ->
        # CLOSING (set_closing) -> CLOSED (set_closed); a crash-resume
        # re-enters process() for the steps after whichever one the
        # project is now in must still be allowed, since that is the
        # close/abandon request itself making progress, not a stale
        # request left over from before it. 'report' also runs while
        # RECOVERING (not just OPEN): it is the recovery attempt's own
        # way of satisfying the (RECOVERING, close_job) report guard --
        # engine.application.steps.report, ENGINE.md close-step table -- so a fresh
        # report request must be allowed before any close request exists
        # to make it "mid-flight".
        allowed_states = (
            ("OPEN", "RECOVERING", "CLOSING", "CLOSED") if request.kind in _CLOSE_LIKE_KINDS
            else ("OPEN", "RECOVERING") if request.kind == "report"
            else ("OPEN",)
        )
        if project_row.state in allowed_states:
            return
        # A non-close request that already has journaled steps (it is
        # mid-flight, not fresh) may still reconcile while the project is
        # RECOVERING -- e.g. retry_unknown() after a recover_job() left
        # the project RECOVERING. A fresh (never-started) request of the
        # same kind still needs OPEN: it has no partial external effect
        # to reconcile, so there's nothing for recovery to finish.
        if project_row.state == "RECOVERING" and self.ledger.steps(request.request_id):
            return
        raise StepFailed(f"state_{project_row.state}")

    def _run_steps(self, ctx: StepContext, steps: list[StepDef],
                  stop_after: str | None = None) -> bool:
        """Returns True once every step has journaled 'done'. Returns
        False as soon as `stop_after` names a step that was JUST
        journaled 'done' in this call -- the caller (process(), via
        Processor.run_request) leaves the request 'running' rather than
        recording a terminal status, so a later call resumes from
        whatever the journal shows (identical resume rule either way:
        'done' skips, 'started' reconciles-or-reruns, absent runs)."""
        journal = self.ledger.steps(ctx.request.request_id)
        for step in steps:
            row = journal.get(step.name)
            if row is not None and row.status == "done":
                if stop_after == step.name:
                    return False
                continue
            if row is None:
                ctx.journal(step.name, "started")
            elif row.status == "started":
                if step.reconcile(ctx):
                    ctx.journal(step.name, "done", row.payload)
                    if stop_after == step.name:
                        return False
                    continue
            self.faults.maybe_crash_before(step.name)
            out = step.run(ctx)
            self.faults.maybe_crash(step.name)
            ctx.journal(step.name, "done", out)
            if stop_after == step.name:
                return False
        return True

    def _set_status(self, request: RequestRow, status: str, result: dict[str, object]) -> None:
        self.ledger.append(Event(
            project=request.project, kind="request.status", actor_kind="processor",
            actor_id=self.holder, attempt_id=request.attempt_id,
            payload={"request_id": request.request_id, "status": status, "result": result},
        ))


def submit(ledger: Ledger, project: str, attempt_id: str, kind: str,
           payload: dict[str, object], actor_id: str) -> str:
    """Append request.submitted unless a request with the same request_id
    exists AND is not 'failed' (then return that existing id, no new
    event -- ordinary idempotency for an in-flight/done request, e.g. a
    caller that calls submit() again after a crash before the original
    call ever returned). Returns request_id.

    If the request with this exact payload's id DOES exist but is
    'failed', this is instead a RETRY: a brand-new request is created
    whose payload is `payload` plus "retry_of"/"retry_n" (engine.domain.ids.
    with_retry) -- a different payload, so it hashes to a genuinely
    different, deterministic request_id, never the same stale failed
    one. This applies to every request kind alike (there is nothing
    kind-specific about "the previous attempt at this exact action
    failed, and the caller wants to try it again now that the cause is
    fixed"); engine.interface.cli's `close` command is a direct beneficiary --
    calling it again after a failed close resubmits the close request
    as a retry rather than forever returning the same failed one. If
    THAT retry also already exists and is itself 'failed' (e.g. this
    function is called twice before the retry is ever actually run),
    the chain is walked one level at a time until either a non-failed
    existing request is found (returned as-is) or a request_id that
    does not exist yet is reached (created). 'unknown' is deliberately
    NOT auto-retried this way -- only engine.application.processor.retry_unknown's
    explicit owner action resolves that status.

    Validates before touching the ledger (ValueError, no event appended):
    the project must be registered, the kind must be one of
    KNOWN_KINDS, the attempt must exist and belong to that project, and
    payload must be a dict. The attempt must additionally be 'active'
    ONLY when this call is actually about to append a brand-new
    request.submitted event -- never when it is merely reusing an
    existing (non-'failed') request_id, checked further below. A
    close/abandon request's OWN last step (engine.application.steps.close's
    set_closed) flips the attempt to 'closed'/'abandoned' as part of
    completing ITS OWN request, before that request's remaining steps
    (open_queue) or its own terminal 'done' status are ever recorded --
    a crash in exactly that window used to make every later call that
    re-submits this SAME already-in-flight request (e.g.
    engine.application.pipeline.finalize_release/_close_attempt resuming after
    such a crash) raise 'attempt_not_active' before it could even look
    up the request it only meant to reuse, even though
    engine.application.processor._check_allowed already has a dedicated carve-out
    letting exactly this request's own later steps keep running on a
    no-longer-active attempt."""
    try:
        ledger.project(project)
    except LedgerError as exc:
        raise ValueError(f"unregistered_project: {project!r}") from exc
    if kind not in KNOWN_KINDS:
        raise ValueError(f"unsupported_kind: {kind!r}")
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    try:
        attempt = ledger.attempt(attempt_id)
    except LedgerError as exc:
        raise ValueError(f"unknown_attempt: {attempt_id!r}") from exc
    if ledger.attempt_project(attempt_id) != project:
        raise ValueError(
            f"attempt {attempt_id!r} does not belong to project {project!r}"
        )

    current_payload = payload
    current_rid = ids.request_id(project, attempt_id, kind, current_payload)
    retry_n = 0
    while True:
        try:
            existing = ledger.request(current_rid)
        except LedgerError:
            break  # current_rid has never been submitted -- create it below
        if existing.status != "failed":
            return current_rid
        retry_n += 1
        current_payload = ids.with_retry(payload, current_rid, retry_n)
        current_rid = ids.request_id(project, attempt_id, kind, current_payload)
    # Reaching here means current_rid has never been submitted -- a
    # genuinely NEW request.submitted event is about to be appended, so
    # (unlike the lookups above) the attempt must still be active: there
    # is no external effect yet for a closed/abandoned/interrupted
    # attempt to dispatch.
    if attempt.status != "active":
        raise ValueError(f"attempt_not_active: {attempt_id!r} is {attempt.status!r}")
    ledger.append(Event(
        project=project, kind="request.submitted", actor_kind="owner",
        actor_id=actor_id, attempt_id=attempt_id,
        payload={"request_id": current_rid, "kind": kind,
                  "content_hash": ids.content_hash(current_payload),
                  "payload": current_payload},
    ))
    return current_rid


def cancel_request(ledger: Ledger, project: str, request_id: str, reason: str) -> None:
    """Move a request straight to 'failed' (result {"reason": reason}),
    WITHOUT ever letting it dispatch -- the owner-facing counterpart of
    a step raising StepFailed, used when a request must be taken off
    the table entirely (e.g. its approval is about to be revoked, or
    the commit it targets is known bad) rather than left for the
    ordinary queue drain to resolve on its own.

    Refuses (CancelError, no event appended) unless the request's
    status is exactly 'queued': a 'running' request may already be
    mid-dispatch (there is no external-effect dispatch step a plain
    status flip could safely interrupt -- see
    engine.infrastructure.publishers.ShellPublisher's own "never redispatch an
    unresolved outcome" contract), and a request already
    'done'/'failed'/'unknown' has nothing left to cancel. Also refuses
    if `request_id` does not actually belong to `project` -- same
    mismatched-pair guard Processor.run_request applies."""
    request = ledger.request(request_id)
    if request.project != project:
        raise CancelError(
            f"request {request_id} belongs to project {request.project!r}, "
            f"not {project!r}"
        )
    if request.status != "queued":
        raise CancelError(f"request {request_id} is {request.status!r}, not queued")
    ledger.append(Event(
        project=project, kind="request.status", actor_kind="owner",
        actor_id="owner", attempt_id=request.attempt_id,
        payload={"request_id": request_id, "status": "failed",
                 "result": {"reason": reason}},
    ))


def retry_unknown(ledger: Ledger, request_id: str, actor_id: str) -> None:
    """Owner input: put an 'unknown' request back to 'running' so the next
    run_once() resumes it through the step journal (reconcile decides
    whether the external effect already happened). Any other status raises
    RetryError — a failed or done request is never retried this way."""
    request = ledger.request(request_id)
    if request.status != "unknown":
        raise RetryError(f"request {request_id} is {request.status!r}, not unknown")
    ledger.append(Event(
        project=request.project, kind="request.status", actor_kind="owner",
        actor_id=actor_id, attempt_id=request.attempt_id,
        payload={"request_id": request_id, "status": "running",
                 "result": {"retried_by": actor_id}},
    ))
