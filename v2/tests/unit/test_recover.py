"""Unit tests for engine.steps.open_job.recover_job: recovery is a NEW
attempt on the same job, not a re-use of the interrupted one.
Also covers the 'report' request kind that lets a recovering attempt
satisfy the (RECOVERING, close_job) report guard."""
from __future__ import annotations

import pytest

from engine.app_security import security_policy_hash
from engine import ids, procs, state
from engine.approvals import grant
from engine.evidence import fingerprint as _repo_fingerprint
from engine.evidence import record, sha256_file
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.faults import CrashInjected, Faults
from engine.ledger import Event
from engine.processor import Fakes, Processor, retry_unknown, submit
from engine.steps.open_job import open_job, recover_job
from tests.conftest import seed_reviewed_manifest, Workspace


def _certify_full(workspace: Workspace, project: str, attempt_id: str) -> None:
    """Record a passing full-suite bench AND a passing acceptance +
    app_security certificate, bound to the project repo's CURRENT
    fingerprint -- engine.steps.publish's call_publisher now requires
    both of these unconditionally before any real dispatch (not only
    when `release` is configured), see that module's own docstring."""
    project_cfg = workspace.config.projects[project]
    fp = _repo_fingerprint(project_cfg.repo, project_cfg.excludes)
    record(workspace.ledger, project, attempt_id, "bench", fp,
           payload={"passed": True, "scope": "full"})
    for kind in ("acceptance.recorded", "app_security.recorded"):
        workspace.ledger.append(Event(
            project=project, kind=kind, actor_kind="processor",
            actor_id="processor", attempt_id=attempt_id,
            payload={"passed": True, "fingerprint": fp,
                     **({"policy_hash": security_policy_hash(workspace.config)}
                        if kind == "app_security.recorded" else {})},
        ))
    seed_reviewed_manifest(workspace.ledger, project, attempt_id)


def _proc(workspace: Workspace, publisher: FakePublisher | None = None) -> Processor:
    proc = Processor(workspace.config, workspace.ledger,
                      Fakes(publisher or FakePublisher(), FakeInstaller()))
    proc.register_defaults()
    return proc


def test_recover_makes_new_attempt_same_job(workspace: Workspace) -> None:
    ledger = workspace.ledger
    attempt_id = open_job(workspace.config, ledger, "alpha", "job1", "full", roles=())
    assert attempt_id.endswith("#1")

    new_attempt_id = recover_job(workspace.config, ledger, "alpha", "owner")

    assert new_attempt_id.endswith("#2")
    assert new_attempt_id.split("#")[0] == attempt_id.split("#")[0]  # same job_id
    old_attempt = ledger.attempt(attempt_id)
    new_attempt = ledger.attempt(new_attempt_id)
    assert old_attempt.status == "interrupted"
    assert new_attempt.kind == "recovery"
    assert new_attempt.generation == old_attempt.generation + 1

    project_row = ledger.project("alpha")
    assert project_row.state == "RECOVERING"
    assert project_row.current_attempt_id == new_attempt_id
    assert project_row.generation == new_attempt.generation

    opened = [event for _seq, event in ledger.events("alpha", "job.opened")
              if event.payload.get("attempt_id") == new_attempt_id]
    assert len(opened) == 1
    assert opened[0].payload.get("recovered_from") == attempt_id


def test_recover_refused_when_closed(workspace: Workspace) -> None:
    with pytest.raises(state.TransitionError):
        recover_job(workspace.config, workspace.ledger, "alpha", "owner")


def test_recover_refused_while_processor_holds_lock(workspace: Workspace) -> None:
    """A Processor.run_once() holds the project lock for the whole
    request, not just a read of attempt/project state at process()
    entry -- recover_job must take the same lock, or an owner's recovery
    could mark an attempt 'interrupted' while a slow in-flight request
    (e.g. run_installer) on that same attempt is still running under it."""
    ledger = workspace.ledger
    attempt_id = open_job(workspace.config, ledger, "alpha", "job1", "full", roles=())

    assert ledger.acquire_lock("alpha", "p1")
    try:
        with pytest.raises(state.TransitionError):
            recover_job(workspace.config, ledger, "alpha", "owner")
    finally:
        ledger.release_lock("alpha", "p1")

    # No ledger write happened: no attempt.finished, no new job.opened.
    assert ledger.events("alpha", "attempt.finished") == []
    recovered = [e for _seq, e in ledger.events("alpha", "job.opened")
                 if e.payload.get("recovered_from")]
    assert recovered == []
    assert ledger.attempt(attempt_id).status == "active"
    assert ledger.project("alpha").state == "OPEN"


def test_open_job_refused_while_processor_holds_lock(workspace: Workspace) -> None:
    ledger = workspace.ledger
    assert ledger.acquire_lock("alpha", "p1")
    try:
        with pytest.raises(state.TransitionError):
            open_job(workspace.config, ledger, "alpha", "job1", "full", roles=())
    finally:
        ledger.release_lock("alpha", "p1")

    assert ledger.events("alpha", "job.opened") == []
    assert ledger.project("alpha").state == "CLOSED"


def test_recover_releases_lock_on_success_and_on_refusal(workspace: Workspace) -> None:
    ledger = workspace.ledger
    open_job(workspace.config, ledger, "alpha", "job1", "full", roles=())
    assert ledger.lock_holder("alpha") is None  # open_job released its own lock

    recover_job(workspace.config, ledger, "alpha", "owner")
    assert ledger.lock_holder("alpha") is None  # recover_job released on success

    # Project is now RECOVERING, not OPEN: this call's own state guard
    # refuses it (not the lock) -- exercises the lock-then-release-on-
    # raise path through `finally`.
    with pytest.raises(state.TransitionError):
        recover_job(workspace.config, ledger, "alpha", "owner")
    assert ledger.lock_holder("alpha") is None


def test_recover_makes_no_copies(workspace: Workspace) -> None:
    ledger = workspace.ledger
    attempt_id = open_job(workspace.config, ledger, "alpha", "job1", "full",
                          roles=("builder",))
    old_copy_root = workspace.config.home / "copies" / ids.attempt_dirname(attempt_id)
    assert (old_copy_root / "builder").exists()

    new_attempt_id = recover_job(workspace.config, ledger, "alpha", "owner")

    new_copy_root = workspace.config.home / "copies" / ids.attempt_dirname(new_attempt_id)
    assert not new_copy_root.exists()
    assert (old_copy_root / "builder").exists()  # old copy left alone


def _job_opened_payload(ledger, project: str, attempt_id: str) -> dict:
    return next(
        event.payload for _seq, event in ledger.events(project, "job.opened")
        if event.payload.get("attempt_id") == attempt_id
    )


def test_recover_inherits_dirty_paths_and_base_head(workspace: Workspace) -> None:
    """recover_job must NOT recompute dirty_paths/base_head: both are
    inherited unchanged from the old attempt's own job.opened payload.
    An owner file left dirty BEFORE open_job stays the job's dirty_paths
    baseline through recovery; a worker's change made AFTER open_job
    (even mid-flight, never committed) is this job's own work, not
    pre-existing owner dirt -- recomputing fresh at recover_job time
    would misfile it as the latter and leave it un-archived forever."""
    ledger = workspace.ledger
    (workspace.alpha / "docs" / "readme.md").write_text("owner notes\n", encoding="utf-8")

    attempt_id = open_job(workspace.config, ledger, "alpha", "job1", "full", roles=())
    old_payload = _job_opened_payload(ledger, "alpha", attempt_id)
    assert old_payload["dirty_paths"] == ["docs/readme.md"]

    (workspace.alpha / "src" / "app.py").write_text("print('worker change')\n", encoding="utf-8")

    new_attempt_id = recover_job(workspace.config, ledger, "alpha", "owner")
    new_payload = _job_opened_payload(ledger, "alpha", new_attempt_id)

    assert new_payload["dirty_paths"] == old_payload["dirty_paths"]
    assert new_payload["base_head"] == old_payload["base_head"]
    assert tuple(ledger.project("alpha").dirty_paths) == ("docs/readme.md",)


def test_midflight_request_of_interrupted_attempt_reconciles(workspace: Workspace) -> None:
    """A publish request dispatched (effect recorded by the fake
    publisher) then crashed to 'unknown' before the lookup could confirm
    it must, after recover_job() + retry_unknown(), reconcile against the
    OLD (now interrupted) attempt without dispatching the publisher a
    second time. publish's dispatch step is call_publisher, and its run()
    did execute (journaled 'started') before raising -- that is exactly
    what _check_allowed's _dispatch_may_have_run() check requires to let
    an interrupted attempt's request through; contrast
    test_interrupted_request_before_dispatch_fails, where the crash lands
    one step before the dispatch step even starts."""
    ledger = workspace.ledger
    attempt_id = open_job(workspace.config, ledger, "beta", "job-pub", "full", roles=())
    artifact_hash = sha256_file(workspace.beta / "dist" / "bundle.js")
    rid = submit(ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "dist/bundle.js",
                  "artifact_hash": artifact_hash}, "owner")
    grant(ledger, "beta", rid, ttl_sec=3600)
    _certify_full(workspace, "beta", attempt_id)

    publisher = FakePublisher(fail_after_effect=True)
    n1 = _proc(workspace, publisher).run_once("beta")
    assert n1 == 0
    assert ledger.request(rid).status == "unknown"
    assert len(publisher.calls) == 1

    new_attempt_id = recover_job(workspace.config, ledger, "beta", "owner")
    assert ledger.attempt(attempt_id).status == "interrupted"
    assert new_attempt_id != attempt_id

    retry_unknown(ledger, rid, "owner")
    n2 = _proc(workspace, publisher).run_once("beta")

    assert n2 == 1
    assert ledger.request(rid).status == "done"
    assert len(publisher.calls) == 1  # effect not re-dispatched


def test_fresh_request_of_interrupted_attempt_fails(workspace: Workspace) -> None:
    """A request of the interrupted attempt that never started (no
    journaled steps) has nothing to reconcile and must be refused."""
    ledger = workspace.ledger
    attempt_id = open_job(workspace.config, ledger, "alpha", "job1", "full", roles=())
    rid = submit(ledger, "alpha", attempt_id, "install", {}, "owner")

    recover_job(workspace.config, ledger, "alpha", "owner")
    assert ledger.attempt(attempt_id).status == "interrupted"

    n = _proc(workspace).run_once("alpha")

    assert n == 0
    req = ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "attempt_interrupted"}


def test_interrupted_request_before_dispatch_fails(workspace: Workspace) -> None:
    """A request of the interrupted attempt journaled only up to a step
    BEFORE its kind's dispatch step (install's dispatch step is
    run_installer; here the crash lands right after snapshot_manifest,
    one step earlier) has no external effect yet to reconcile and must
    be refused outright, same as a request with no journaled steps at
    all -- 'some step is journaled' is not enough on its own."""
    ledger = workspace.ledger
    attempt_id = open_job(workspace.config, ledger, "alpha", "job-install", "full", roles=())
    rid = submit(ledger, "alpha", attempt_id, "install", {
        "manifest": "package.json", "change": {"dependencies": {"zod": "3.0.0"}},
    }, "owner")

    proc1 = Processor(workspace.config, ledger, Fakes(FakePublisher(), FakeInstaller()),
                      faults=Faults(crash_after={"snapshot_manifest"}), holder="p1")
    proc1.register_defaults()
    with pytest.raises(CrashInjected):
        proc1.run_once("alpha")

    journal = ledger.steps(rid)
    assert journal["acquire_project_lock"].status == "done"
    assert journal["snapshot_manifest"].status == "started"
    assert "run_installer" not in journal  # the dispatch step never started
    assert ledger.request(rid).status == "running"

    recover_job(workspace.config, ledger, "alpha", "owner")
    assert ledger.attempt(attempt_id).status == "interrupted"

    proc2 = _proc(workspace)
    n = proc2.run_once("alpha")

    assert n == 0
    req = ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "attempt_interrupted"}


def test_report_request_then_close_from_recovering(workspace: Workspace) -> None:
    ledger = workspace.ledger
    open_job(workspace.config, ledger, "alpha", "job1", "full", roles=())
    new_attempt_id = recover_job(workspace.config, ledger, "alpha", "owner")

    proc = _proc(workspace)
    rid_report = submit(ledger, "alpha", new_attempt_id, "report",
                        {"summary": "recovered"}, "owner")
    n1 = proc.run_once("alpha")
    assert n1 == 1
    assert ledger.request(rid_report).status == "done"

    rid_close = submit(ledger, "alpha", new_attempt_id, "close", {"mode": "close"}, "owner")
    n2 = proc.run_once("alpha")
    assert n2 == 1
    assert ledger.request(rid_close).status == "done"
    assert ledger.project("alpha").state == "CLOSED"

    archive_root = workspace.config.home / "archive" / "alpha" / ids.attempt_dirname(new_attempt_id)
    final_dirs = [p for p in archive_root.iterdir() if not p.name.startswith(".tmp-")]
    assert len(final_dirs) == 1


def test_close_from_recovering_without_report_fails(workspace: Workspace) -> None:
    ledger = workspace.ledger
    open_job(workspace.config, ledger, "alpha", "job1", "full", roles=())
    new_attempt_id = recover_job(workspace.config, ledger, "alpha", "owner")

    proc = _proc(workspace)
    rid_close = submit(ledger, "alpha", new_attempt_id, "close", {"mode": "close"}, "owner")
    n = proc.run_once("alpha")

    assert n == 0
    req = ledger.request(rid_close)
    assert req.status == "failed"
    # The FIRST close attempt hits state.transition()'s own
    # (RECOVERING, close_job) report guard directly, which raises
    # TransitionError -- the processor's catch-all records that as
    # "internal:TransitionError" (close has no dispatch step, so this is
    # 'failed', never 'unknown'). set_closing's own explicit
    # StepFailed("report_required") only fires on a SECOND close/abandon
    # request once the project is already stuck in CLOSING (see
    # tests/unit/test_steps_close.py::
    # test_set_closing_requires_report_when_prior_recovering_without_report).
    assert req.result["reason"] == "internal:TransitionError"
    assert ledger.project("alpha").state == "RECOVERING"


def test_recover_closes_live_station_processes(workspace: Workspace) -> None:
    """P1: the old attempt's live processes (e.g. a builder station still
    running) must be killed and proc.ended recorded before the attempt is
    marked interrupted -- otherwise they are never reaped by anything."""
    ledger = workspace.ledger
    attempt_id = open_job(workspace.config, ledger, "alpha", "job1", "full", roles=())
    proc = procs.spawn(ledger, "alpha", attempt_id, "build", ["sleep", "30"],
                       cwd=workspace.alpha)
    assert procs.is_alive(proc.pid)

    recover_job(workspace.config, ledger, "alpha", "owner")

    assert not procs.is_alive(proc.pid)
    ended = [event for _seq, event in ledger.events("alpha", "proc.ended")
             if event.payload.get("pid") == proc.pid]
    assert len(ended) == 1
    assert ledger.live_procs(attempt_id) == []


def test_recover_resumes_after_crash_before_job_opened(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crash-safety (P1): a crash right after attempt.finished{interrupted}
    but before job.opened for the recovery attempt leaves the project OPEN
    with an interrupted current attempt. Calling recover_job again must
    finish the SAME recovery (one attempt.finished, one recovery
    job.opened), not refuse or double up."""
    ledger = workspace.ledger
    attempt_id = open_job(workspace.config, ledger, "alpha", "job1", "full", roles=())

    orig_append = ledger.append
    calls = {"n": 0}

    def _crashing_append(event):
        calls["n"] += 1
        if calls["n"] <= 1:
            return orig_append(event)
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(ledger, "append", _crashing_append)
    with pytest.raises(RuntimeError):
        recover_job(workspace.config, ledger, "alpha", "owner")
    monkeypatch.setattr(ledger, "append", orig_append)

    assert ledger.attempt(attempt_id).status == "interrupted"
    assert ledger.project("alpha").state == "OPEN"
    recovered = [event for _seq, event in ledger.events("alpha", "job.opened")
                 if event.payload.get("recovered_from")]
    assert recovered == []

    new_attempt_id = recover_job(workspace.config, ledger, "alpha", "owner")

    assert len(ledger.events("alpha", "attempt.finished")) == 1
    recovered = [event for _seq, event in ledger.events("alpha", "job.opened")
                 if event.payload.get("recovered_from")]
    assert len(recovered) == 1
    assert recovered[0].payload["attempt_id"] == new_attempt_id
    project_row = ledger.project("alpha")
    assert project_row.state == "RECOVERING"
    assert project_row.current_attempt_id == new_attempt_id


def test_recover_resumes_after_crash_before_state_changed(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crash-safety (P1): a crash after job.opened for the recovery
    attempt but before state.changed OPEN -> RECOVERING leaves state OPEN
    with an already-active 'recovery' current attempt. Calling recover_job
    again must only finish the transition, writing no extra events."""
    ledger = workspace.ledger
    attempt_id = open_job(workspace.config, ledger, "alpha", "job1", "full", roles=())

    orig_append = ledger.append
    calls = {"n": 0}

    def _crashing_append(event):
        calls["n"] += 1
        if calls["n"] <= 2:
            return orig_append(event)
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(ledger, "append", _crashing_append)
    with pytest.raises(RuntimeError):
        recover_job(workspace.config, ledger, "alpha", "owner")
    monkeypatch.setattr(ledger, "append", orig_append)

    assert ledger.project("alpha").state == "OPEN"
    recovered = [event for _seq, event in ledger.events("alpha", "job.opened")
                 if event.payload.get("recovered_from")]
    assert len(recovered) == 1
    new_attempt_id = recovered[0].payload["attempt_id"]
    assert ledger.attempt(new_attempt_id).status == "active"
    assert ledger.attempt(new_attempt_id).kind == "recovery"
    assert ledger.project("alpha").current_attempt_id == new_attempt_id

    returned_attempt_id = recover_job(workspace.config, ledger, "alpha", "owner")

    assert returned_attempt_id == new_attempt_id
    assert len(ledger.events("alpha", "attempt.finished")) == 1
    recovered_after = [event for _seq, event in ledger.events("alpha", "job.opened")
                       if event.payload.get("recovered_from")]
    assert len(recovered_after) == 1  # no second job.opened written
    project_row = ledger.project("alpha")
    assert project_row.state == "RECOVERING"
    assert project_row.current_attempt_id == new_attempt_id
    assert ledger.attempt(attempt_id).status == "interrupted"
