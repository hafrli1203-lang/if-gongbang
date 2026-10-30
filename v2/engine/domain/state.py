"""Project state machine: CLOSED / OPEN / CLOSING / RECOVERING.

Only the owner and the processor may drive transitions. Safety policy
(engine.domain.policy) is evaluated regardless of state; only request processing
sleeps while CLOSED.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from engine.domain.records import Event, LedgerError

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger

_PROCESSOR_INPUTS = frozenset(
    {"finalized", "archive_failed_open", "archive_failed_recovering"}
)


class TransitionError(Exception):
    """Input not allowed in the current state."""


class ProjectOpenError(TransitionError):
    """Raised by a pre-check BEFORE any ledger write when a caller tries
    to open a fresh job (`open_with_intent`) while the project is not
    CLOSED -- a different job is still active. Deliberately a distinct
    subclass of TransitionError (never raised by `transition`/`open_job`
    themselves, which keep their own generic TransitionError for the
    guards they already had) so a caller that wants to turn ONLY this
    specific, pre-write rejection into a friendly message can catch it
    without also swallowing `open_job`'s own "project_busy"/queued-
    requests refusals, which still mean something went wrong mid-write
    and should surface as-is."""


def _has_unknown_request(ledger: Ledger, project: str) -> bool:
    """True iff some request submitted for this project currently has
    status 'unknown'. Walks request.submitted events rather than a direct
    query: the public Ledger API has no "all requests by status" read."""
    for _seq, event in ledger.events(project, "request.submitted"):
        rid = event.payload.get("request_id")
        if rid is None:
            continue
        try:
            if ledger.request(str(rid)).status == "unknown":
                return True
        except LedgerError:
            continue
    return False


def _prior_closing_state(ledger: Ledger, project: str) -> str | None:
    """The 'prev' recorded on the most recent state.changed event that
    entered CLOSING, or None if there is none."""
    for _seq, event in reversed(ledger.events(project, "state.changed")):
        if event.payload.get("state") == "CLOSING":
            return event.payload.get("prev")
    return None


# (current_state, input) -> next_state
TRANSITIONS: dict[tuple[str, str], str] = {
    ("CLOSED", "open_job"): "OPEN",
    ("OPEN", "close_job"): "CLOSING",
    ("OPEN", "abandon_job"): "CLOSING",
    ("RECOVERING", "close_job"): "CLOSING",
    ("RECOVERING", "abandon_job"): "CLOSING",
    ("CLOSING", "finalized"): "CLOSED",
    ("CLOSING", "archive_failed_open"): "OPEN",
    ("CLOSING", "archive_failed_recovering"): "RECOVERING",
    ("OPEN", "recover_job"): "RECOVERING",
}


def transition(ledger: Ledger, project: str, input_name: str, actor_id: str) -> str:
    """Apply one input. Appends state.changed and returns the new state.

    Extra guards beyond the table:
    - open_job: refused when queued requests exist for the project.
    - recover_job: refused unless the current attempt status is 'active'.
      This bare transition() call only moves OPEN -> RECOVERING for
      whichever attempt is already current; it does not by itself make
      recovery a new attempt. The owner-facing recovery operation
      (engine.application.steps.open_job.recover_job) marks the old attempt
      'interrupted' and opens a new one BEFORE calling this function, so
      by the time this guard runs, current_attempt_id already points at
      the new (active) attempt.
    - approval inputs are not transitions; approvals.grant() checks that
      state != CLOSING itself.
    Raises TransitionError otherwise.
    """
    current = ledger.project(project).state
    key = (current, input_name)
    if key not in TRANSITIONS:
        raise TransitionError(
            f"input {input_name!r} not allowed in state {current!r}"
        )
    if input_name == "open_job" and ledger.queued_requests(project):
        raise TransitionError("open_job refused: queued requests pending")
    if input_name == "open_job" and _has_unknown_request(ledger, project):
        raise TransitionError("open_job refused: unknown_requests_pending")
    if input_name == "close_job" and current == "RECOVERING":
        attempt_id = ledger.project(project).current_attempt_id
        has_report = attempt_id is not None and any(
            event.attempt_id == attempt_id
            for _seq, event in ledger.events(project, "report.written")
        )
        if not has_report:
            raise TransitionError(
                "close_job refused: no report for current attempt"
            )
    if input_name == "recover_job":
        attempt = ledger.project(project).current_attempt_id
        if attempt is None or ledger.attempt(attempt).status != "active":
            raise TransitionError(
                "recover_job refused: current attempt is not active"
            )
    next_state = TRANSITIONS[key]
    actor_kind = "processor" if input_name in _PROCESSOR_INPUTS else "owner"
    ledger.append(Event(
        project=project, kind="state.changed", actor_kind=actor_kind,
        actor_id=actor_id,
        payload={"state": next_state, "input": input_name, "prev": current},
    ))
    return next_state


def require_state(ledger: Ledger, project: str, *allowed: str) -> None:
    """Raise TransitionError unless the project state is in `allowed`."""
    current = ledger.project(project).state
    if current not in allowed:
        raise TransitionError(
            f"required state in {allowed}, got {current!r}"
        )
