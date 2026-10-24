"""Unit tests for engine.processor: submit dedupe, step journal resume,
ordering, locking, and failure/unknown handling. Uses hand-made StepDef
lists registered directly (register_defaults is exercised elsewhere)."""
from __future__ import annotations

from pathlib import Path

import pytest

from engine import ids
from engine.faults import CrashInjected, Faults
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.ledger import Event, Ledger
from engine.processor import (
    Fakes, Processor, ProcessorBusy, StepDef, StepFailed, StepUnknown, submit,
)
from engine.state import transition
from engine.steps.open_job import open_job, recover_job
from tests.conftest import Workspace


def _ledger(tmp_path: Path) -> Ledger:
    ledger = Ledger.create(tmp_path / "ledger.sqlite")
    ledger.register_project("alpha")
    return ledger


def _open_attempt(ledger: Ledger, generation: int = 1, n: int = 1,
                   job_id: str = "J-alpha-X") -> str:
    """Open an attempt and, the first time, move the project to OPEN (a
    second attempt in the same test reuses the already-open project)."""
    attempt_id = f"{job_id}#{n}"
    ledger.append(Event(
        project="alpha", kind="job.opened", actor_kind="owner", actor_id="owner",
        attempt_id=attempt_id,
        payload={"job_id": job_id, "job_name": "job", "attempt_id": attempt_id,
                  "n": n, "kind": "full", "generation": generation, "dirty_paths": []},
    ))
    if ledger.project("alpha").state == "CLOSED":
        transition(ledger, "alpha", "open_job", "owner")
    return attempt_id


def _processor(ledger: Ledger, holder: str = "p1", faults: Faults | None = None,
                config: object | None = None) -> Processor:
    fakes = Fakes(publisher=FakePublisher(), installer=FakeInstaller())
    return Processor(config=config, ledger=ledger, fakes=fakes, faults=faults, holder=holder)


def test_submit_dedupes_same_payload(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    attempt_id = _open_attempt(ledger)
    rid1 = submit(ledger, "alpha", attempt_id, "commit", {"message": "hi"}, "owner")
    rid2 = submit(ledger, "alpha", attempt_id, "commit", {"message": "hi"}, "owner")
    assert rid1 == rid2
    assert len(ledger.events("alpha", "request.submitted")) == 1


def test_submit_different_payload_new_id(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    attempt_id = _open_attempt(ledger)
    rid1 = submit(ledger, "alpha", attempt_id, "commit", {"message": "hi"}, "owner")
    rid2 = submit(ledger, "alpha", attempt_id, "commit", {"message": "bye"}, "owner")
    assert rid1 != rid2
    assert len(ledger.events("alpha", "request.submitted")) == 2


def _mark_status(ledger: Ledger, attempt_id: str, request_id: str, status: str) -> None:
    ledger.append(Event(
        project="alpha", kind="request.status", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"request_id": request_id, "status": status, "result": {}},
    ))


def test_submit_retries_failed_request_with_new_id(tmp_path: Path) -> None:
    """A failed request with this exact payload must NOT keep coming
    back as-is forever -- submit() creates a genuinely new request
    whose payload carries retry_of/retry_n, applying to every request
    kind alike (engine.cli's `close` is one direct beneficiary)."""
    ledger = _ledger(tmp_path)
    attempt_id = _open_attempt(ledger)
    rid1 = submit(ledger, "alpha", attempt_id, "commit", {"message": "hi"}, "owner")
    _mark_status(ledger, attempt_id, rid1, "failed")

    rid2 = submit(ledger, "alpha", attempt_id, "commit", {"message": "hi"}, "owner")
    assert rid2 != rid1
    req2 = ledger.request(rid2)
    assert req2.status == "queued"
    assert req2.payload["message"] == "hi"
    assert req2.payload["retry_of"] == rid1
    assert req2.payload["retry_n"] == 1
    assert len(ledger.events("alpha", "request.submitted")) == 2


def test_submit_retry_chain_increments_through_repeated_failures(tmp_path: Path) -> None:
    """A SECOND failure (of the retry itself) advances the chain again
    -- retry_n keeps counting, retry_of always points at the specific
    request just retried (the immediately preceding one, not
    necessarily the very first)."""
    ledger = _ledger(tmp_path)
    attempt_id = _open_attempt(ledger)
    rid1 = submit(ledger, "alpha", attempt_id, "commit", {"message": "hi"}, "owner")
    _mark_status(ledger, attempt_id, rid1, "failed")
    rid2 = submit(ledger, "alpha", attempt_id, "commit", {"message": "hi"}, "owner")
    _mark_status(ledger, attempt_id, rid2, "failed")
    rid3 = submit(ledger, "alpha", attempt_id, "commit", {"message": "hi"}, "owner")

    assert len({rid1, rid2, rid3}) == 3
    req3 = ledger.request(rid3)
    assert req3.payload["retry_of"] == rid2
    assert req3.payload["retry_n"] == 2
    assert len(ledger.events("alpha", "request.submitted")) == 3


def test_submit_retry_itself_idempotent_when_still_pending(tmp_path: Path) -> None:
    """Calling submit() again for the SAME failed original, before the
    retry it already created has itself finished, must return that
    SAME retry -- not spawn yet another one just because submit() was
    called twice."""
    ledger = _ledger(tmp_path)
    attempt_id = _open_attempt(ledger)
    rid1 = submit(ledger, "alpha", attempt_id, "commit", {"message": "hi"}, "owner")
    _mark_status(ledger, attempt_id, rid1, "failed")
    rid2 = submit(ledger, "alpha", attempt_id, "commit", {"message": "hi"}, "owner")
    rid2_again = submit(ledger, "alpha", attempt_id, "commit", {"message": "hi"}, "owner")
    assert rid2_again == rid2
    assert len(ledger.events("alpha", "request.submitted")) == 2


def test_submit_does_not_auto_retry_unknown_status(tmp_path: Path) -> None:
    """'unknown' is deliberately NOT retried by submit() -- only
    engine.processor.retry_unknown's explicit owner action resolves
    that status; a plain re-submit must return the same unknown id."""
    ledger = _ledger(tmp_path)
    attempt_id = _open_attempt(ledger)
    rid1 = submit(ledger, "alpha", attempt_id, "commit", {"message": "hi"}, "owner")
    _mark_status(ledger, attempt_id, rid1, "unknown")
    rid2 = submit(ledger, "alpha", attempt_id, "commit", {"message": "hi"}, "owner")
    assert rid2 == rid1
    assert len(ledger.events("alpha", "request.submitted")) == 1


def test_done_step_skipped(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    attempt_id = _open_attempt(ledger)
    rid = submit(ledger, "alpha", attempt_id, "commit", {}, "owner")
    called = []
    steps = [StepDef("a", lambda ctx: called.append(1) or {}, lambda ctx: False)]
    ledger.append(Event(
        project="alpha", kind="request.step", actor_kind="processor",
        actor_id="p1", attempt_id=attempt_id,
        payload={"request_id": rid, "step_name": "a", "status": "done", "payload": {}},
    ))
    proc = _processor(ledger)
    proc.register("commit", lambda ctx: steps)
    n = proc.run_once("alpha")
    assert n == 1
    assert called == []
    assert ledger.request(rid).status == "done"


def test_started_step_reconciled_true_skips(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    attempt_id = _open_attempt(ledger)
    rid = submit(ledger, "alpha", attempt_id, "commit", {}, "owner")
    called = []
    steps = [StepDef("a", lambda ctx: called.append(1) or {"x": 1}, lambda ctx: True)]
    ledger.append(Event(
        project="alpha", kind="request.step", actor_kind="processor",
        actor_id="p1", attempt_id=attempt_id,
        payload={"request_id": rid, "step_name": "a", "status": "started", "payload": {}},
    ))
    proc = _processor(ledger)
    proc.register("commit", lambda ctx: steps)
    n = proc.run_once("alpha")
    assert n == 1
    assert called == []
    assert ledger.steps(rid)["a"].status == "done"


def test_started_step_reconciled_false_reruns(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    attempt_id = _open_attempt(ledger)
    rid = submit(ledger, "alpha", attempt_id, "commit", {}, "owner")
    called = []
    steps = [StepDef("a", lambda ctx: called.append(1) or {"x": 1}, lambda ctx: False)]
    ledger.append(Event(
        project="alpha", kind="request.step", actor_kind="processor",
        actor_id="p1", attempt_id=attempt_id,
        payload={"request_id": rid, "step_name": "a", "status": "started", "payload": {}},
    ))
    proc = _processor(ledger)
    proc.register("commit", lambda ctx: steps)
    n = proc.run_once("alpha")
    assert n == 1
    assert called == [1]
    assert ledger.steps(rid)["a"].status == "done"


def test_crash_after_step_leaves_running_then_resume_completes(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    attempt_id = _open_attempt(ledger)
    rid = submit(ledger, "alpha", attempt_id, "merge", {}, "owner")

    effects: dict[str, list[int]] = {"a": [], "b": []}

    def make_run(name: str):
        def run(ctx: object) -> dict[str, object]:
            effects[name].append(1)
            return {}
        return run

    def make_reconcile(name: str):
        def reconcile(ctx: object) -> bool:
            return bool(effects[name])
        return reconcile

    steps = [
        StepDef("a", make_run("a"), make_reconcile("a")),
        StepDef("b", make_run("b"), make_reconcile("b")),
    ]

    proc1 = _processor(ledger, holder="p1", faults=Faults(crash_after={"b"}))
    proc1.register("merge", lambda ctx: steps)
    with pytest.raises(CrashInjected):
        proc1.run_once("alpha")

    assert effects == {"a": [1], "b": [1]}
    assert ledger.request(rid).status == "running"
    # the lock was released despite the crash
    assert ledger.acquire_lock("alpha", "checker")
    ledger.release_lock("alpha", "checker")

    proc2 = _processor(ledger, holder="p2", faults=Faults())
    proc2.register("merge", lambda ctx: steps)
    n = proc2.run_once("alpha")

    assert n == 1
    assert effects == {"a": [1], "b": [1]}  # each step's run() fired exactly once
    assert ledger.request(rid).status == "done"


def test_one_request_at_a_time_in_order(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    attempt_id = _open_attempt(ledger)
    order: list[str] = []

    def make_steps(tag: str) -> list[StepDef]:
        return [StepDef("only", lambda ctx: order.append(tag) or {}, lambda ctx: False)]

    submit(ledger, "alpha", attempt_id, "commit", {"i": 1}, "owner")
    submit(ledger, "alpha", attempt_id, "publish", {"i": 2}, "owner")
    proc = _processor(ledger)
    proc.register("commit", lambda ctx: make_steps("r1"))
    proc.register("publish", lambda ctx: make_steps("r2"))
    n = proc.run_once("alpha")
    assert n == 2
    assert order == ["r1", "r2"]


def test_closed_attempt_install_request_fails_state_closed(tmp_path: Path) -> None:
    from engine.state import transition

    ledger = _ledger(tmp_path)
    transition(ledger, "alpha", "open_job", "owner")
    attempt_id = _open_attempt(ledger)
    transition(ledger, "alpha", "close_job", "owner")
    transition(ledger, "alpha", "finalized", "processor")
    assert ledger.attempt(attempt_id).status == "active"
    assert ledger.project("alpha").state == "CLOSED"

    rid = ids.request_id("alpha", attempt_id, "install", {})
    ledger.append(Event(
        project="alpha", kind="request.submitted", actor_kind="owner",
        actor_id="owner", attempt_id=attempt_id,
        payload={"request_id": rid, "kind": "install",
                  "content_hash": ids.content_hash({}), "payload": {}},
    ))
    called = []

    def run_would_crash(ctx: object) -> dict[str, object]:
        called.append(1)
        raise AssertionError("must not run against a closed attempt")

    proc = _processor(ledger)
    proc.register("install", lambda ctx: [StepDef("only", run_would_crash, lambda c: False)])
    n = proc.run_once("alpha")
    assert n == 0
    assert called == []
    req = ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "state_CLOSED"}


def test_second_processor_busy(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    assert ledger.acquire_lock("alpha", "p1")
    proc2 = _processor(ledger, holder="p2")
    with pytest.raises(ProcessorBusy):
        proc2.run_once("alpha")
    ledger.release_lock("alpha", "p1")


def test_two_default_holder_processors_do_not_collide(tmp_path: Path) -> None:
    """D-5: Processor() with no holder argument used to default to the
    static string "processor" for every instance, so a second Processor
    in the same process could "re-acquire" the first's lock (and release
    it). Each instance must now get its own identity."""
    import os

    ledger = _ledger(tmp_path)
    fakes = Fakes(publisher=FakePublisher(), installer=FakeInstaller())
    proc1 = Processor(config=None, ledger=ledger, fakes=fakes)
    proc2 = Processor(config=None, ledger=ledger, fakes=fakes)
    assert proc1.holder != proc2.holder

    assert ledger.acquire_lock("alpha", proc1.holder, pid=os.getpid())
    with pytest.raises(ProcessorBusy):
        proc2.run_once("alpha")
    ledger.release_lock("alpha", proc1.holder, pid=os.getpid())


def test_stale_generation_request_failed(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    attempt_id = _open_attempt(ledger, generation=1, n=1)
    _open_attempt(ledger, generation=2, n=2)  # bumps project.generation to 2
    rid = submit(ledger, "alpha", attempt_id, "commit", {}, "owner")
    called = []
    proc = _processor(ledger)
    proc.register("commit", lambda ctx: [StepDef("only", lambda c: called.append(1) or {},
                                             lambda c: False)])
    n = proc.run_once("alpha")
    assert n == 0
    assert called == []
    req = ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "stale_generation"}


def test_step_failed_marks_failed(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    attempt_id = _open_attempt(ledger)
    rid = submit(ledger, "alpha", attempt_id, "commit", {}, "owner")

    def run_fail(ctx: object) -> dict[str, object]:
        raise StepFailed("bad_thing")

    proc = _processor(ledger)
    proc.register("commit", lambda ctx: [StepDef("only", run_fail, lambda c: False)])
    n = proc.run_once("alpha")
    assert n == 0
    req = ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "bad_thing"}


def test_step_unknown_stops_processing(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    attempt_id = _open_attempt(ledger)
    rid1 = submit(ledger, "alpha", attempt_id, "commit", {"i": 1}, "owner")
    rid2 = submit(ledger, "alpha", attempt_id, "publish", {"i": 2}, "owner")

    def run_unknown(ctx: object) -> dict[str, object]:
        raise StepUnknown("bench_unreachable")

    called2 = []
    proc = _processor(ledger)
    proc.register("commit", lambda ctx: [StepDef("only", run_unknown, lambda c: False)])
    proc.register("publish", lambda ctx: [StepDef("only", lambda c: called2.append(1) or {},
                                              lambda c: False)])
    n = proc.run_once("alpha")
    assert n == 0
    assert called2 == []  # the second queued request was never reached
    req1 = ledger.request(rid1)
    assert req1.status == "unknown"
    assert req1.result == {"reason": "bench_unreachable"}
    req2 = ledger.request(rid2)
    assert req2.status == "queued"


def test_run_once_blocks_entirely_while_a_request_is_unknown(tmp_path: Path) -> None:
    """D-22: a request left 'unknown' by one run_once() call used to only
    stop that call; the *next* run_once() would skip over the unknown
    request (queued_requests() only returns queued/running) and execute
    later requests anyway -- which could e.g. close the attempt, so that
    retrying the unknown request later fails as 'stale_generation'
    instead of actually being resolved. Now ANY unknown request for the
    project blocks run_once() from processing anything at all, and a
    processor.blocked event records why."""
    ledger = _ledger(tmp_path)
    attempt_id = _open_attempt(ledger)
    rid_a = submit(ledger, "alpha", attempt_id, "commit", {"i": 1}, "owner")
    rid_b = submit(ledger, "alpha", attempt_id, "publish", {"i": 2}, "owner")

    def run_unknown(ctx: object) -> dict[str, object]:
        raise StepUnknown("bench_unreachable")

    called_b = []
    proc = _processor(ledger)
    proc.register("commit", lambda ctx: [StepDef("only", run_unknown, lambda c: False)])
    proc.register("publish", lambda ctx: [StepDef("only", lambda c: called_b.append(1) or {},
                                              lambda c: False)])
    n1 = proc.run_once("alpha")
    assert n1 == 0
    assert ledger.request(rid_a).status == "unknown"

    # Second run_once: B must NOT run while A is still unknown.
    n2 = proc.run_once("alpha")
    assert n2 == 0
    assert called_b == []
    assert ledger.request(rid_b).status == "queued"
    blocked_events = ledger.events("alpha", "processor.blocked")
    assert len(blocked_events) == 1
    assert blocked_events[0][1].payload == {
        "reason": "unknown_request", "request_id": rid_a,
    }

    # Resolve A (retry_unknown + a fixed fake), then both A and B run.
    from engine.processor import retry_unknown
    proc.register("commit", lambda ctx: [StepDef("only", lambda c: {}, lambda c: False)])
    retry_unknown(ledger, rid_a, "owner")
    n3 = proc.run_once("alpha")
    assert n3 == 2
    assert called_b == [1]
    assert ledger.request(rid_a).status == "done"
    assert ledger.request(rid_b).status == "done"


def test_submit_unknown_attempt_raises_and_appends_nothing(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    before = len(ledger.events("alpha"))
    with pytest.raises(ValueError):
        submit(ledger, "alpha", "J-alpha-NOPE#1", "commit", {"message": "hi"}, "owner")
    assert len(ledger.events("alpha")) == before
    assert ledger.queued_requests("alpha") == []


def test_submit_unregistered_project_raises(tmp_path: Path) -> None:
    ledger = Ledger.create(tmp_path / "ledger.sqlite")
    with pytest.raises(ValueError):
        submit(ledger, "ghost", "J-ghost-X#1", "commit", {"message": "hi"}, "owner")


def test_submit_unsupported_kind_raises_and_appends_nothing(tmp_path: Path) -> None:
    """N8: an unsupported kind must be rejected at submit() time -- if it
    were accepted it would become 'unknown' at process() time (no steps
    exist for it) and block the whole project forever."""
    ledger = _ledger(tmp_path)
    attempt_id = _open_attempt(ledger)
    before = len(ledger.events("alpha"))
    with pytest.raises(ValueError):
        submit(ledger, "alpha", attempt_id, "frobnicate", {}, "owner")
    assert len(ledger.events("alpha")) == before
    assert ledger.queued_requests("alpha") == []


def test_process_unregistered_kind_fails_not_unknown(tmp_path: Path) -> None:
    """N8: a KNOWN_KINDS kind this particular Processor never registered
    steps for must fail outright (StepFailed 'unsupported_kind'), not
    become 'unknown' -- there is no external effect to look up, so
    leaving it unknown would just block the project forever."""
    ledger = _ledger(tmp_path)
    attempt_id = _open_attempt(ledger)
    rid = submit(ledger, "alpha", attempt_id, "merge", {}, "owner")
    proc = _processor(ledger)  # register_defaults() not called
    n = proc.run_once("alpha")
    assert n == 0
    req = ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "unsupported_kind"}


def test_submit_non_dict_payload_raises(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    attempt_id = _open_attempt(ledger)
    with pytest.raises(ValueError):
        submit(ledger, "alpha", attempt_id, "commit", "not-a-dict", "owner")  # type: ignore[arg-type]


def test_submit_inactive_attempt_raises(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    attempt_id = _open_attempt(ledger)
    ledger.append(Event(
        project="alpha", kind="attempt.finished", actor_kind="owner",
        actor_id="owner", attempt_id=attempt_id,
        payload={"attempt_id": attempt_id, "status": "closed"},
    ))
    with pytest.raises(ValueError):
        submit(ledger, "alpha", attempt_id, "commit", {"message": "hi"}, "owner")


def test_run_once_unexpected_exception_fails_request_and_continues(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    attempt_id = _open_attempt(ledger)
    rid1 = submit(ledger, "alpha", attempt_id, "commit", {"i": 1}, "owner")
    rid2 = submit(ledger, "alpha", attempt_id, "publish", {"i": 2}, "owner")

    def run_boom(ctx: object) -> dict[str, object]:
        raise KeyError("missing_field")

    called2 = []
    proc = _processor(ledger)
    proc.register("commit", lambda ctx: [StepDef("only", run_boom, lambda c: False)])
    proc.register("publish", lambda ctx: [StepDef("only", lambda c: called2.append(1) or {},
                                              lambda c: False)])
    n = proc.run_once("alpha")
    assert n == 1
    assert called2 == [1]
    req1 = ledger.request(rid1)
    assert req1.status == "failed"
    assert req1.result["reason"].startswith("internal:")
    req2 = ledger.request(rid2)
    assert req2.status == "done"


def test_exception_after_dispatch_step_becomes_unknown_not_failed(tmp_path: Path) -> None:
    """N6: a later step blowing up AFTER the kind's dispatch step
    (call_publisher for publish) already ran must become 'unknown', not
    'failed' -- 'failed' would let the owner resubmit, re-dispatching the
    same external effect (here: publishing) a second time. 'unknown'
    routes it through retry_unknown() instead, which reconciles."""
    ledger = _ledger(tmp_path)
    attempt_id = _open_attempt(ledger)
    rid = submit(ledger, "alpha", attempt_id, "publish", {}, "owner")

    def boom(ctx: object) -> dict[str, object]:
        raise PermissionError("artifact dir not writable")

    steps = [
        StepDef("call_publisher", lambda ctx: {}, lambda ctx: False),
        StepDef("after", boom, lambda ctx: False),
    ]
    proc = _processor(ledger)
    proc.register("publish", lambda ctx: steps)
    n = proc.run_once("alpha")
    assert n == 0
    req = ledger.request(rid)
    assert req.status == "unknown"
    assert req.result["reason"] == "internal:PermissionError"
    assert ledger.steps(rid)["call_publisher"].status == "done"


def test_exception_before_dispatch_step_still_fails(tmp_path: Path) -> None:
    """Contrast case for N6: a step that blows up BEFORE the dispatch step
    ever ran (call_publisher is absent from the journal) has no external
    effect to reconcile, so the old 'failed' behavior is still correct."""
    ledger = _ledger(tmp_path)
    attempt_id = _open_attempt(ledger)
    rid = submit(ledger, "alpha", attempt_id, "publish", {}, "owner")

    def boom(ctx: object) -> dict[str, object]:
        raise PermissionError("artifact dir not writable")

    steps = [
        StepDef("before", boom, lambda ctx: False),
        StepDef("call_publisher", lambda ctx: {}, lambda ctx: False),
    ]
    proc = _processor(ledger)
    proc.register("publish", lambda ctx: steps)
    n = proc.run_once("alpha")
    assert n == 0
    req = ledger.request(rid)
    assert req.status == "failed"
    assert req.result["reason"] == "internal:PermissionError"
    assert "call_publisher" not in ledger.steps(rid)


def test_resume_in_recovering_state_allowed_for_mid_flight_request(workspace: Workspace) -> None:
    """N3: a non-close request that already has journaled steps (it's
    mid-flight, not fresh) must still be allowed to reconcile on its own
    now-'interrupted' attempt after recover_job() opens a new one.
    process() used to require OPEN unconditionally for non-close kinds,
    which would fail a retry_unknown()'d request as 'state_RECOVERING'
    before it even got a chance to look anything up.

    The fake step is named "run_installer" (not an arbitrary name): since
    _check_allowed's interrupted-attempt branch now requires the kind's
    DISPATCH step specifically to have run (_dispatch_may_have_run,
    _DISPATCH_STEP_BY_KIND["install"] == "run_installer"), not merely
    "some step is journaled", this test's single fake step has to be the
    real dispatch step name to exercise that path at all."""
    ledger = workspace.ledger
    attempt_id = open_job(workspace.config, ledger, "alpha", "job", "full", roles=())
    rid = submit(ledger, "alpha", attempt_id, "install", {}, "owner")

    effects: list[int] = []
    steps = [StepDef("run_installer", lambda ctx: effects.append(1) or {},
                      lambda ctx: bool(effects))]
    proc1 = _processor(ledger, config=workspace.config, faults=Faults(crash_after={"run_installer"}))
    proc1.register("install", lambda ctx: steps)
    with pytest.raises(CrashInjected):
        proc1.run_once("alpha")
    assert effects == [1]
    assert ledger.request(rid).status == "running"

    new_attempt_id = recover_job(workspace.config, ledger, "alpha", "owner")
    assert ledger.project("alpha").state == "RECOVERING"
    assert new_attempt_id != attempt_id
    assert ledger.attempt(attempt_id).status == "interrupted"

    proc2 = _processor(ledger, holder="p2", config=workspace.config, faults=Faults())
    proc2.register("install", lambda ctx: steps)
    n = proc2.run_once("alpha")
    assert n == 1
    assert ledger.request(rid).status == "done"
    assert effects == [1]  # reconcile(True) skipped re-running run()


def test_fresh_request_in_recovering_still_requires_open(workspace: Workspace) -> None:
    """N3 contrast: a FRESH (never-started) request has no journaled
    steps and no partial effect to reconcile, so it must still be
    refused while the project is RECOVERING -- here against the NEW
    (active) recovery attempt, since submit() refuses an interrupted
    attempt outright."""
    ledger = workspace.ledger
    open_job(workspace.config, ledger, "alpha", "job", "full", roles=())
    new_attempt_id = recover_job(workspace.config, ledger, "alpha", "owner")
    rid = submit(ledger, "alpha", new_attempt_id, "install", {}, "owner")
    called: list[int] = []
    proc = _processor(ledger, config=workspace.config)
    proc.register("install", lambda ctx: [StepDef("only", lambda c: called.append(1) or {},
                                                    lambda c: False)])
    n = proc.run_once("alpha")
    assert n == 0
    assert called == []
    req = ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "state_RECOVERING"}


def test_retry_unknown_only_for_unknown(workspace) -> None:
    """retry_unknown flips 'unknown' back to 'running'; any other status is refused."""
    from engine.ledger import Event
    from engine.processor import RetryError, retry_unknown, submit

    ledger = workspace.ledger
    ledger.append(Event("alpha", "job.opened", "owner", "owner", "J-alpha-X#1", {
        "job_id": "J-alpha-X", "job_name": "x", "attempt_id": "J-alpha-X#1", "n": 1,
        "kind": "full", "generation": 1, "dirty_paths": []}))
    rid = submit(ledger, "alpha", "J-alpha-X#1", "publish", {"target": "t"}, "owner")
    with pytest.raises(RetryError):
        retry_unknown(ledger, rid, "owner")
    ledger.append(Event("alpha", "request.status", "processor", "p", "J-alpha-X#1",
                        {"request_id": rid, "status": "unknown", "result": {}}))
    retry_unknown(ledger, rid, "owner")
    assert ledger.request(rid).status == "running"


def test_run_request_rejects_mismatched_project_before_any_mutation(workspace) -> None:
    """run_request(project, request_id) must refuse outright, with no
    ledger mutation and no lock acquired on either project, when
    request_id actually belongs to a DIFFERENT project than the one
    given."""
    ledger = workspace.ledger
    attempt_id = open_job(workspace.config, ledger, "beta", "job", "full", roles=())
    rid = submit(ledger, "beta", attempt_id, "report", {"summary": "x"}, "owner")

    proc = _processor(ledger, config=workspace.config)
    with pytest.raises(ValueError):
        proc.run_request("alpha", rid)

    assert ledger.request(rid).status == "queued"  # never touched
    # Neither project's lock was left held by the refused call.
    assert ledger.acquire_lock("alpha", "someone-else")
    ledger.release_lock("alpha", "someone-else")
    assert ledger.acquire_lock("beta", "someone-else")
    ledger.release_lock("beta", "someone-else")
