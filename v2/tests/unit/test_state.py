"""Unit tests for engine.state: transition table, guards, require_state."""
from __future__ import annotations

from pathlib import Path

import pytest

from engine.ledger import Event, Ledger
from engine.state import TRANSITIONS, TransitionError, require_state, transition


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    led = Ledger.create(tmp_path / "ledger.sqlite")
    led.register_project("alpha")
    return led


def _open_attempt(ledger: Ledger, n: int = 1) -> str:
    """Give the project an active attempt so recover_job can be exercised."""
    attempt_id = f"J-alpha-XXX#{n}"
    ledger.append(Event(
        project="alpha", kind="job.opened", actor_kind="processor",
        actor_id="proc1", attempt_id=attempt_id,
        payload={"job_id": "J-alpha-XXX", "job_name": "job", "attempt_id": attempt_id,
                  "n": n, "kind": "full", "generation": n, "dirty_paths": []},
    ))
    return attempt_id


def _force_state(ledger: Ledger, state: str) -> None:
    ledger.append(Event(
        project="alpha", kind="state.changed", actor_kind="owner",
        actor_id="owner1", payload={"state": state, "input": "test", "prev": "?"},
    ))


@pytest.mark.parametrize("current,input_name,expected", [
    (k[0], k[1], v) for k, v in TRANSITIONS.items()
])
def test_each_transition_table_row(ledger: Ledger, current: str, input_name: str,
                                    expected: str) -> None:
    if input_name == "recover_job" or current == "RECOVERING":
        attempt_id = _open_attempt(ledger)
        if current == "RECOVERING" and input_name == "close_job":
            # close_job from RECOVERING is only allowed with a passing
            # report for the current attempt (plan §3, row ⑧).
            ledger.append(Event(
                project="alpha", kind="report.written", actor_kind="processor",
                actor_id="proc1", attempt_id=attempt_id, payload={},
            ))
    if current != "CLOSED":
        _force_state(ledger, current)
    result = transition(ledger, "alpha", input_name, "owner1")
    assert result == expected
    assert ledger.project("alpha").state == expected


def test_open_job_refused_when_queue_nonempty(ledger: Ledger) -> None:
    ledger.append(Event(
        project="alpha", kind="request.submitted", actor_kind="owner",
        actor_id="owner1", attempt_id="none",
        payload={"request_id": "R-1", "kind": "commit",
                  "content_hash": "h", "payload": {}},
    ))
    with pytest.raises(TransitionError):
        transition(ledger, "alpha", "open_job", "owner1")


def test_recover_refused_when_attempt_not_active(ledger: Ledger) -> None:
    attempt_id = _open_attempt(ledger)
    _force_state(ledger, "OPEN")
    ledger.append(Event(
        project="alpha", kind="attempt.finished", actor_kind="processor",
        actor_id="proc1", attempt_id=attempt_id,
        payload={"attempt_id": attempt_id, "status": "closed"},
    ))
    with pytest.raises(TransitionError):
        transition(ledger, "alpha", "recover_job", "owner1")


def test_invalid_transition_raises(ledger: Ledger) -> None:
    with pytest.raises(TransitionError):
        transition(ledger, "alpha", "close_job", "owner1")


def test_require_state(ledger: Ledger) -> None:
    require_state(ledger, "alpha", "CLOSED")
    with pytest.raises(TransitionError):
        require_state(ledger, "alpha", "OPEN")
    require_state(ledger, "alpha", "OPEN", "CLOSED")


def test_close_job_from_recovering_refused_without_report(ledger: Ledger) -> None:
    """P2-7: close_job out of RECOVERING must require a report.written
    event for the current attempt (abandon_job stays unguarded)."""
    attempt_id = _open_attempt(ledger)
    _force_state(ledger, "OPEN")
    transition(ledger, "alpha", "recover_job", "owner1")
    assert ledger.project("alpha").state == "RECOVERING"

    with pytest.raises(TransitionError):
        transition(ledger, "alpha", "close_job", "owner1")

    ledger.append(Event(
        project="alpha", kind="report.written", actor_kind="processor",
        actor_id="proc1", attempt_id=attempt_id, payload={},
    ))
    result = transition(ledger, "alpha", "close_job", "owner1")
    assert result == "CLOSING"


def test_abandon_job_from_recovering_does_not_require_report(ledger: Ledger) -> None:
    _open_attempt(ledger)
    _force_state(ledger, "OPEN")
    transition(ledger, "alpha", "recover_job", "owner1")
    result = transition(ledger, "alpha", "abandon_job", "owner1")
    assert result == "CLOSING"


def test_open_job_refused_when_unknown_request_pending(ledger: Ledger) -> None:
    """P3: open_job must refuse while any request for the project is
    'unknown' — otherwise retry_unknown() later fails as stale_generation
    after a new attempt/generation has already been opened on top of it."""
    ledger.append(Event(
        project="alpha", kind="request.submitted", actor_kind="owner",
        actor_id="owner1", attempt_id="J-alpha-XXX#1",
        payload={"request_id": "R-u1", "kind": "publish",
                  "content_hash": "h", "payload": {}},
    ))
    ledger.append(Event(
        project="alpha", kind="request.status", actor_kind="processor",
        actor_id="processor", attempt_id="J-alpha-XXX#1",
        payload={"request_id": "R-u1", "status": "unknown", "result": {}},
    ))
    with pytest.raises(TransitionError):
        transition(ledger, "alpha", "open_job", "owner1")
