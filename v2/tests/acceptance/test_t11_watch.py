"""Acceptance T11 -- `gb2 watch`'s own judgment (engine.application.watch) against
a real ledger, plus `gb2 watch --loop --resume`'s rate limit (tools/gb2lib/
watch_ops.py).

Background: an operator wrapped a `gb2` call in a shell timeout. The
wrapper killed the engine process mid-dispatch, but the request it was
running stayed "running" in the ledger forever -- nothing else was
watching, so the job sat stuck for three hours before anyone noticed.
`judge_app` is the one place that must tell a genuinely live driver (or
worker subprocess) apart from a project whose ledger still records
"running"/"queued" work with nothing left alive to finish it. This file
exercises all five verdicts, the dead-pid-in-the-lock-row case
specifically (结함 this whole feature exists for), and the resume-rate
limit `gb2 watch --loop --resume` must never exceed.
"""
from __future__ import annotations

from pathlib import Path

import os
import subprocess
import sys

from engine.application import watch
from engine.application.job_state import JobPosition, record_position
from engine.application.steps.open_job import open_job
from engine.ledger import Event
from tests.conftest import Workspace

# A pid that is certain not to exist on this host right now: spawn a real
# child, wait for it to exit, then reuse its now-dead pid -- the same
# "genuinely was a real pid, is not any more" shape engine.infrastructure.procs
# itself relies on elsewhere (e.g. its own _RECOVERED_PID sentinel docstring).


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _open(ws: Workspace, job_name: str = "J-watch") -> str:
    return open_job(ws.config, ws.ledger, "alpha", job_name, "full", ("builder",))


def _submit_request(ws: Workspace, attempt_id: str, request_id: str, *,
                    status: str = "queued", kind: str = "run_station") -> None:
    ws.ledger.append(Event(
        project="alpha", kind="request.submitted", actor_kind="owner", actor_id="owner",
        attempt_id=attempt_id,
        payload={"request_id": request_id, "kind": kind, "content_hash": "h",
                 "payload": {}},
    ))
    if status != "queued":
        ws.ledger.append(Event(
            project="alpha", kind="request.status", actor_kind="processor",
            actor_id="processor", attempt_id=attempt_id,
            payload={"request_id": request_id, "status": status, "result": {}},
        ))


def test_idle_project_never_opened(workspace: Workspace) -> None:
    v = watch.judge_app(workspace.ledger, "alpha")
    assert v.verdict == "idle"
    assert v.stage is None
    assert v.open_requests == 0


def test_running_when_lock_holder_pid_is_alive(workspace: Workspace) -> None:
    attempt_id = _open(workspace)
    _submit_request(workspace, attempt_id, "R-1", status="running")
    # This test process's own pid is, definitionally, alive right now.
    assert workspace.ledger.acquire_lock("alpha", "driver-x", pid=os.getpid())

    v = watch.judge_app(workspace.ledger, "alpha")
    assert v.verdict == "running"


def test_stalled_when_lock_holder_pid_is_dead(workspace: Workspace) -> None:
    """The exact incident this feature exists for: a crashed driver left
    a lock row behind with a pid that no longer exists, and a request it
    was in the middle of dispatching is still "running"."""
    attempt_id = _open(workspace)
    _submit_request(workspace, attempt_id, "R-1", status="running")
    dead = _dead_pid()
    # Bypass acquire_lock's own dead-pid reclaim (it would just hand the
    # lock to us) -- write the stale row directly, as if some earlier,
    # now-dead driver still held it.
    workspace.ledger._conn.execute(
        "INSERT INTO locks(project, holder, acquired_ts, pid) VALUES (?,?,?,?)",
        ("alpha", "crashed-driver", 0.0, dead),
    )
    workspace.ledger._conn.commit()

    v = watch.judge_app(workspace.ledger, "alpha")
    assert v.verdict == "stalled"
    assert v.open_requests == 1


def test_waiting_owner_stage(workspace: Workspace) -> None:
    attempt_id = _open(workspace)
    record_position(workspace.ledger, "alpha", JobPosition(
        job="J-watch", stage="awaiting_task_owner", attempt_id=attempt_id,
        task_id="t1",
    ))

    v = watch.judge_app(workspace.ledger, "alpha")
    assert v.verdict == "waiting_owner"
    assert v.stage == "awaiting_task_owner"
    assert v.task_id == "t1"


def test_failed_when_last_request_failed_and_nothing_pending(workspace: Workspace) -> None:
    attempt_id = _open(workspace)
    record_position(workspace.ledger, "alpha", JobPosition(
        job="J-watch", stage="task", attempt_id=attempt_id, task_id="t1",
    ))
    _submit_request(workspace, attempt_id, "R-1", status="failed")

    v = watch.judge_app(workspace.ledger, "alpha")
    assert v.verdict == "failed"


def test_failed_when_parallel_review_failed_but_later_submitted_bench_passed(
        workspace: Workspace) -> None:
    """Live shape: review and bench run in parallel, review is submitted
    first and fails (review_not_passed), bench is submitted after it and
    passes. The attempt failed even though its last-submitted request
    is "done"."""
    attempt_id = _open(workspace)
    record_position(workspace.ledger, "alpha", JobPosition(
        job="J-watch", stage="task", attempt_id=attempt_id, task_id="t1",
    ))
    _submit_request(workspace, attempt_id, "R-review", status="queued", kind="review")
    _submit_request(workspace, attempt_id, "R-bench", status="done", kind="bench")
    workspace.ledger.append(Event(
        project="alpha", kind="request.status", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"request_id": "R-review", "status": "failed",
                 "result": {"reason": "review_not_passed"}},
    ))

    v = watch.judge_app(workspace.ledger, "alpha")
    assert v.verdict == "failed"


def test_waiting_owner_on_release_approval_despite_a_queued_request(
        workspace: Workspace) -> None:
    """ a review finding: a queued `ship` release-approval
    request is a NORMAL, expected stop for the owner, not a crash --
    before the classify() reorder this used to come back "stalled"
    purely because Ledger.queued_requests still saw the release request
    queued, which would make `gb2 watch --loop --resume` burn its hourly
    auto-resume budget and eventually report the app "failed" over
    something that was never actually stuck."""
    attempt_id = _open(workspace)
    record_position(workspace.ledger, "alpha", JobPosition(
        job="J-watch", stage="awaiting_release_approval", attempt_id=attempt_id,
        task_id="t1",
    ))
    _submit_request(workspace, attempt_id, "R-ship", status="queued", kind="release")

    v = watch.judge_app(workspace.ledger, "alpha")
    assert v.verdict == "waiting_owner"


def test_stalled_when_position_recorded_before_any_request_submitted(
        workspace: Workspace) -> None:
    """ a review finding: a driver that crashes right after
    job_state.record_position but before it ever submits the request
    that stage was about to make leaves open_requests==0 AND no
    "failed" request behind -- before this fix that fell all the way
    through to "idle", silently abandoning a job with real, in-progress
    work recorded for it."""
    attempt_id = _open(workspace)
    record_position(workspace.ledger, "alpha", JobPosition(
        job="J-watch", stage="task", attempt_id=attempt_id, task_id="t1",
    ))
    # Deliberately no _submit_request call -- the crash this test
    # reproduces happens strictly before one.

    v = watch.judge_app(workspace.ledger, "alpha")
    assert v.verdict == "stalled"


def test_gave_up_pending_clears_after_a_new_job_opens(workspace: Workspace) -> None:
    """ a review finding: the owner manually recovering by
    starting a fresh job (never going through `gb2 watch --resume`, so
    no "watch.resumed" event exists) must still clear the old
    "watch.gave_up" -- otherwise watch keeps reporting this app
    "failed" (gave up) forever over work the owner already moved past."""
    attempt_id = _open(workspace)
    _submit_request(workspace, attempt_id, "R-1", status="running")
    workspace.ledger.append(Event(
        project="alpha", kind="watch.gave_up", actor_kind="processor",
        actor_id="watch", payload={"reason": "watch_max_resumes_per_hour"},
    ))
    assert watch.gave_up_pending(workspace.ledger, "alpha") is True

    # The owner starts a brand-new job on the same project -- a fresh
    # job.opened event with no watch.resumed in between. gave_up_pending
    # only reads this event's kind/timestamp (Ledger.event_timestamps),
    # never its payload, so a minimal, directly-appended event is the
    # same fact a real open_job() would record.
    new_attempt_id = f"{attempt_id}-manual-recovery"
    workspace.ledger.append(Event(
        project="alpha", kind="job.opened", actor_kind="owner", actor_id="owner",
        attempt_id=new_attempt_id,
        payload={"job_id": "J2", "job_name": "J-watch-2", "attempt_id": new_attempt_id,
                  "n": 1, "kind": "full", "generation": 1,
                  "dirty_paths": [], "base_head": "", "copy_links_skipped": [],
                  "copy_git_config_sha256": {}},
    ))

    assert watch.gave_up_pending(workspace.ledger, "alpha") is False


def test_gave_up_pending_clears_when_project_closed(workspace: Workspace) -> None:
    """ a review finding: the owner finishing the job by hand
    (project state -> CLOSED) is the other manual-recovery path that
    must clear an old give-up with no "watch.resumed" event at all."""
    attempt_id = _open(workspace)
    _submit_request(workspace, attempt_id, "R-1", status="running")
    workspace.ledger.append(Event(
        project="alpha", kind="watch.gave_up", actor_kind="processor",
        actor_id="watch", payload={"reason": "watch_max_resumes_per_hour"},
    ))
    assert watch.gave_up_pending(workspace.ledger, "alpha", project_state="CLOSED") is False
    assert watch.gave_up_pending(workspace.ledger, "alpha", project_state="OPEN") is True


def test_classify_is_a_pure_decision_table() -> None:
    """No ledger, no pid access -- every branch via plain booleans."""
    assert watch.classify(driver_is_alive=True, worker_is_alive=False,
                          open_request_count=0, stage=None, pending_owner=False,
                          last_failed=False) == "running"
    assert watch.classify(driver_is_alive=False, worker_is_alive=True,
                          open_request_count=0, stage=None, pending_owner=False,
                          last_failed=False) == "running"
    assert watch.classify(driver_is_alive=False, worker_is_alive=False,
                          open_request_count=2, stage=None, pending_owner=False,
                          last_failed=False) == "stalled"
    assert watch.classify(driver_is_alive=False, worker_is_alive=False,
                          open_request_count=0, stage="awaiting_task_owner",
                          pending_owner=False, last_failed=False) == "waiting_owner"
    assert watch.classify(driver_is_alive=False, worker_is_alive=False,
                          open_request_count=0, stage="task", pending_owner=True,
                          last_failed=False) == "waiting_owner"
    assert watch.classify(driver_is_alive=False, worker_is_alive=False,
                          open_request_count=0, stage="task", pending_owner=False,
                          last_failed=True) == "failed"
    assert watch.classify(driver_is_alive=False, worker_is_alive=False,
                          open_request_count=0, stage=None, pending_owner=False,
                          last_failed=False) == "idle"
    # a review finding: waiting_owner outranks stalled even
    # with a queued request open (a release approval is not a crash).
    assert watch.classify(driver_is_alive=False, worker_is_alive=False,
                          open_request_count=1, stage="awaiting_release_approval",
                          pending_owner=False, last_failed=False) == "waiting_owner"
    # a review finding: a stage with no request yet
    # submitted under its own attempt is stalled, not idle.
    assert watch.classify(driver_is_alive=False, worker_is_alive=False,
                          open_request_count=0, stage="task", pending_owner=False,
                          last_failed=False, attempt_has_submitted_request=False) == "stalled"
    # ...but the same stage WITH a submitted (and failed) request keeps
    # classifying as "failed", never "stalled" -- attempt_has_submitted_
    # request defaults to True for every existing caller.
    assert watch.classify(driver_is_alive=False, worker_is_alive=False,
                          open_request_count=0, stage="task", pending_owner=False,
                          last_failed=True) == "failed"


# ---------------------------------------------------------------------
# gb2 watch --loop --resume: rate limit (tools/gb2lib/watch_ops.py)
# ---------------------------------------------------------------------

def test_resume_only_targets_stalled_never_waiting_owner(workspace: Workspace,
                                                         monkeypatch) -> None:
    from tools.gb2lib import watch_ops

    attempt_id = _open(workspace)
    record_position(workspace.ledger, "alpha", JobPosition(
        job="J-watch", stage="awaiting_task_owner", attempt_id=attempt_id,
        task_id="t1",
    ))

    calls: list[str] = []
    monkeypatch.setattr(watch_ops, "_continue_full_job", lambda *a, **k: calls.append("called"))

    record = watch.judge_app(workspace.ledger, "alpha").__dict__
    outcome = watch_ops.maybe_resume(
        workspace.config, workspace.ledger, "alpha",
        lambda attempt: None, record, now=1_000_000.0,
    )
    assert outcome == "not_stalled"
    assert calls == []


def test_resume_gives_up_past_the_hourly_limit(workspace: Workspace, monkeypatch) -> None:
    from tools.gb2lib import watch_ops

    attempt_id = _open(workspace)
    _submit_request(workspace, attempt_id, "R-1", status="running")

    calls: list[str] = []
    monkeypatch.setattr(watch_ops, "_continue_full_job",
                        lambda config, ledger, fakes_for, project, job_name, **k: calls.append(job_name))

    now = 2_000_000.0
    record = watch.judge_app(workspace.ledger, "alpha", now=now).__dict__
    assert record["verdict"] == "stalled"

    results = [
        watch_ops.maybe_resume(workspace.config, workspace.ledger, "alpha",
                               lambda attempt: None, record, now=now + i)
        for i in range(4)
    ]
    assert len(calls) == 3  # watch_max_resumes_per_hour default
    assert results[:3] == ["resumed"] * 3
    assert results[3] == "gave_up"

    gave_up_events = workspace.ledger.events("alpha", "watch.gave_up")
    assert len(gave_up_events) == 1

    v = watch.judge_app(workspace.ledger, "alpha", now=now + 4)
    assert v.verdict == "failed"


def test_resume_failed_does_not_raise_and_records_an_event(workspace: Workspace,
                                                            monkeypatch) -> None:
    """ a review finding: a stuck single-task `gb2 run` job
    has no full-orbit position to resume through this path at all --
    job_state.resume_job (== engine.full_orbit.continue_full_job) raises
    FullOrbitError for it. Before this fix that exception propagated out
    of maybe_resume uncaught, which would have killed run_watch_pass's
    loop over every OTHER app it was still judging in the same pass --
    exactly the kind of single-app failure this whole watch feature
    exists to notice without taking the rest of the fleet down with it."""
    from engine.full_orbit import FullOrbitError
    from tools.gb2lib import watch_ops

    attempt_id = _open(workspace)
    _submit_request(workspace, attempt_id, "R-1", status="running")

    def _raise(*a: object, **k: object) -> None:
        raise FullOrbitError("no pending stop recorded for job 'J-watch'")

    monkeypatch.setattr(watch_ops, "_continue_full_job", _raise)

    now = 3_000_000.0
    record = watch.judge_app(workspace.ledger, "alpha", now=now).__dict__
    assert record["verdict"] == "stalled"

    outcome = watch_ops.maybe_resume(
        workspace.config, workspace.ledger, "alpha",
        lambda attempt: None, record, now=now,
    )
    assert outcome == "resume_failed"

    failed_events = workspace.ledger.events("alpha", "watch.resume_failed")
    assert len(failed_events) == 1
    assert "no pending stop" in failed_events[0][1].payload["reason"]


# ---------------------------------------------------------------------
# Owner standing approval:
# tools/gb2lib/watch_ops.maybe_standing_approve
# ---------------------------------------------------------------------

def _with_manifest(workspace: Workspace, manifest: str) -> Workspace:
    from dataclasses import replace
    project = replace(workspace.config.projects["alpha"], manifest=manifest)
    config = replace(workspace.config,
                     projects={**workspace.config.projects, "alpha": project})
    return replace(workspace, config=config)


def _stop_with_pending_install(workspace: Workspace, job_name: str,
                               attempt_id: str, request: dict) -> None:
    record_position(workspace.ledger, "alpha", JobPosition(
        job=job_name, stage="awaiting_task_owner", attempt_id=attempt_id,
        pending_requests=(request,), stop_reason="owner_request",
    ))


def test_standing_approve_ignores_a_non_waiting_owner_verdict(workspace: Workspace) -> None:
    from tools.gb2lib import watch_ops

    outcome = watch_ops.maybe_standing_approve(
        workspace.config, workspace.ledger, "alpha", lambda attempt: None,
        {"verdict": "stalled"},
    )
    assert outcome == "not_waiting_owner"


def test_standing_approve_leaves_job_waiting_when_no_policy_set(
        workspace: Workspace, tmp_path, monkeypatch) -> None:
    """No STANDING_APPROVALS.json at all -> load_policy's EMPTY_POLICY ->
    nothing matches -> the job keeps waiting for the owner exactly as
    before this feature existed."""
    import json

    from tools.gb2lib import watch_ops

    monkeypatch.setattr("engine.application.standing_approval.DEFAULT_POLICY_PATH", Path(str(tmp_path / "missing.json")))
    ws = _with_manifest(workspace, "package.json")
    (ws.alpha / "package.json").write_text(
        json.dumps({"dependencies": {"next": "16.3.6"}}), encoding="utf-8")
    attempt_id = _open(ws, "J-std1")
    _stop_with_pending_install(ws, "J-std1", attempt_id, {
        "kind": "install", "manifest": "package.json",
        "change": {"dependencies": {"next": "16.4.3"}},
    })

    calls: list[str] = []
    monkeypatch.setattr(watch_ops, "_continue_full_job", lambda *a, **k: calls.append("called"))

    record = watch.judge_app(ws.ledger, "alpha").__dict__
    assert record["verdict"] == "waiting_owner"
    outcome = watch_ops.maybe_standing_approve(ws.config, ws.ledger, "alpha",
                                               lambda attempt: None, record)
    assert outcome == "not_all_matched"
    assert calls == []
    assert ws.ledger.events("alpha", "owner.approved") == []


def test_standing_approve_auto_approves_a_patch_bump_and_resumes(
        workspace: Workspace, tmp_path, monkeypatch) -> None:
    import json

    import engine.interface.cli as cli_mod
    from tools.gb2lib import watch_ops

    policy_path = tmp_path / "STANDING_APPROVALS.json"
    policy_path.write_text(json.dumps({"apps": {"alpha": ["dependency_patch_bump"]}}),
                           encoding="utf-8")
    monkeypatch.setattr("engine.application.standing_approval.DEFAULT_POLICY_PATH", Path(str(policy_path)))

    ws = _with_manifest(workspace, "package.json")
    (ws.alpha / "package.json").write_text(
        json.dumps({"dependencies": {"next": "16.3.6", "zod": "4.6.6"}}), encoding="utf-8")
    attempt_id = _open(ws, "J-std2")
    _stop_with_pending_install(ws, "J-std2", attempt_id, {
        "kind": "install", "manifest": "package.json",
        "change": {"dependencies": {"next": "16.4.3", "zod": "4.6.7"}},
    })

    monkeypatch.setattr(cli_mod, "_drain", lambda *a, **k: "done")
    calls: list[str] = []
    monkeypatch.setattr(watch_ops, "_continue_full_job", lambda *a, **k: calls.append("called"))

    record = watch.judge_app(ws.ledger, "alpha").__dict__
    assert record["verdict"] == "waiting_owner"
    outcome = watch_ops.maybe_standing_approve(ws.config, ws.ledger, "alpha",
                                               lambda attempt: None, record)

    assert outcome == "standing_approved_resumed"
    assert calls == ["called"]
    approved = ws.ledger.events("alpha", "owner.approved")
    assert len(approved) == 1
    assert approved[0][1].payload["confirm_source"] == "standing"
    standing_events = ws.ledger.events("alpha", "standing.approved")
    assert len(standing_events) == 1
    assert standing_events[0][1].payload["kind"] == "dependency_patch_bump"


def test_standing_approve_rejects_a_new_package_and_leaves_job_waiting(
        workspace: Workspace, tmp_path, monkeypatch) -> None:
    """All-or-nothing: a batch mixing a covered patch bump with an
    uncovered new package must approve NEITHER."""
    import json

    from tools.gb2lib import watch_ops

    policy_path = tmp_path / "STANDING_APPROVALS.json"
    policy_path.write_text(json.dumps({"apps": {"alpha": ["dependency_patch_bump"]}}),
                           encoding="utf-8")
    monkeypatch.setattr("engine.application.standing_approval.DEFAULT_POLICY_PATH", Path(str(policy_path)))

    ws = _with_manifest(workspace, "package.json")
    (ws.alpha / "package.json").write_text(
        json.dumps({"dependencies": {"next": "16.3.6"}}), encoding="utf-8")
    attempt_id = _open(ws, "J-std3")
    _stop_with_pending_install(ws, "J-std3", attempt_id, {
        "kind": "install", "manifest": "package.json",
        "change": {"dependencies": {"left-pad": "1.0.0"}},
    })

    calls: list[str] = []
    monkeypatch.setattr(watch_ops, "_continue_full_job", lambda *a, **k: calls.append("called"))

    record = watch.judge_app(ws.ledger, "alpha").__dict__
    outcome = watch_ops.maybe_standing_approve(ws.config, ws.ledger, "alpha",
                                               lambda attempt: None, record)
    assert outcome == "not_all_matched"
    assert calls == []
    assert ws.ledger.events("alpha", "owner.approved") == []


def test_standing_approve_rejects_batch_mixed_with_a_non_whitelisted_edit_request(
        workspace: Workspace, tmp_path, monkeypatch) -> None:
    """a pending "edit" request (never part of
    owner_approval.PENDING_KIND_WHITELIST at all) sitting ALONGSIDE a
    covered install must block the WHOLE batch -- the install must
    not be approved alone while the edit request is left unaddressed
    but invisible to this check."""
    import json

    from tools.gb2lib import watch_ops

    policy_path = tmp_path / "STANDING_APPROVALS.json"
    policy_path.write_text(json.dumps({"apps": {"alpha": ["dependency_patch_bump"]}}),
                           encoding="utf-8")
    monkeypatch.setattr("engine.application.standing_approval.DEFAULT_POLICY_PATH", Path(str(policy_path)))

    ws = _with_manifest(workspace, "package.json")
    (ws.alpha / "package.json").write_text(
        json.dumps({"dependencies": {"next": "16.3.6"}}), encoding="utf-8")
    attempt_id = _open(ws, "J-std5")
    record_position(ws.ledger, "alpha", JobPosition(
        job="J-std5", stage="awaiting_task_owner", attempt_id=attempt_id,
        pending_requests=(
            {"kind": "install", "manifest": "package.json",
             "change": {"dependencies": {"next": "16.4.3"}}},
            {"kind": "edit", "path": "src/x.py", "diff": "..."},
        ),
        stop_reason="owner_request",
    ))

    calls: list[str] = []
    monkeypatch.setattr(watch_ops, "_continue_full_job", lambda *a, **k: calls.append("called"))

    record = watch.judge_app(ws.ledger, "alpha").__dict__
    assert record["verdict"] == "waiting_owner"
    outcome = watch_ops.maybe_standing_approve(ws.config, ws.ledger, "alpha",
                                               lambda attempt: None, record)
    assert outcome == "not_all_matched"
    assert calls == []
    assert ws.ledger.events("alpha", "owner.approved") == []


def test_standing_approve_skipped_when_a_live_driver_holds_the_lock(
        workspace: Workspace, tmp_path, monkeypatch) -> None:
    import json

    from tools.gb2lib import watch_ops

    policy_path = tmp_path / "STANDING_APPROVALS.json"
    policy_path.write_text(json.dumps({"apps": {"alpha": ["dependency_patch_bump"]}}),
                           encoding="utf-8")
    monkeypatch.setattr("engine.application.standing_approval.DEFAULT_POLICY_PATH", Path(str(policy_path)))

    ws = _with_manifest(workspace, "package.json")
    attempt_id = _open(ws, "J-std4")
    _stop_with_pending_install(ws, "J-std4", attempt_id, {
        "kind": "install", "manifest": "package.json",
        "change": {"dependencies": {"next": "16.4.3"}},
    })
    # Judge FIRST (no lock held yet -> "waiting_owner"), then a live
    # driver grabs the lock before this function actually acts on that
    # verdict -- the same race maybe_resume's own "skipped_locked"
    # check exists for.
    record = watch.judge_app(ws.ledger, "alpha").__dict__
    assert record["verdict"] == "waiting_owner"
    assert ws.ledger.acquire_lock("alpha", "driver-x", pid=os.getpid())

    outcome = watch_ops.maybe_standing_approve(ws.config, ws.ledger, "alpha",
                                               lambda attempt: None, record)
    assert outcome == "skipped_locked"


# ---------------------------------------------------------------------
# G16/G30: operator alerts (tools/gb2lib/watch_ops.post_ops_alert)
# ---------------------------------------------------------------------

def test_post_ops_alert_writes_one_line_and_ledger_event(workspace: Workspace,
                                                          tmp_path) -> None:
    from tools.gb2lib import watch_ops

    alerts_path = tmp_path / "ALERTS.md"
    posted = watch_ops.post_ops_alert(
        workspace.ledger, "alpha", "gongbang_job_failed", "A-1",
        "stage=task open_requests=0", path=alerts_path, now=1_700_000_000.0,
    )
    assert posted is True
    text = alerts_path.read_text(encoding="utf-8")
    assert text.startswith("- [ ] ")
    assert '"kind": "gongbang_job_failed"' in text
    assert '"app": "alpha"' in text
    assert '"attempt": "A-1"' in text

    alert_events = workspace.ledger.events("alpha", "watch.alert_posted")
    assert len(alert_events) == 1


def test_post_ops_alert_never_duplicates_the_same_app_kind_attempt(
        workspace: Workspace, tmp_path) -> None:
    """Re-judging the SAME still-stuck attempt on every later watch pass
    must not keep appending the same ALERTS.md line forever."""
    from tools.gb2lib import watch_ops

    alerts_path = tmp_path / "ALERTS.md"
    first = watch_ops.post_ops_alert(
        workspace.ledger, "alpha", "gongbang_job_failed", "A-1", "r1",
        path=alerts_path,
    )
    second = watch_ops.post_ops_alert(
        workspace.ledger, "alpha", "gongbang_job_failed", "A-1", "r2 (same attempt)",
        path=alerts_path,
    )
    assert first is True
    assert second is False
    assert alerts_path.read_text(encoding="utf-8").count("gongbang_job_failed") == 1


def test_post_ops_alert_posts_again_for_a_different_attempt(workspace: Workspace,
                                                             tmp_path) -> None:
    """A fresh attempt_id (a new job, or a resumed attempt that fails
    again) is its own, legitimately new incident -- it gets its own
    line even though the app and kind are the same as a prior alert."""
    from tools.gb2lib import watch_ops

    alerts_path = tmp_path / "ALERTS.md"
    watch_ops.post_ops_alert(workspace.ledger, "alpha", "gongbang_job_failed",
                             "A-1", "first", path=alerts_path)
    second = watch_ops.post_ops_alert(workspace.ledger, "alpha", "gongbang_job_failed",
                                      "A-2", "second", path=alerts_path)
    assert second is True
    assert alerts_path.read_text(encoding="utf-8").count("- [ ] ") == 2


def test_post_ops_alert_to_a_scratch_file_never_suppresses_the_real_one(
        workspace: Workspace, tmp_path) -> None:
    """Live: a check run pointed at a scratch alerts file
    recorded watch.alert_posted in the real ledger, so the real
    ALERTS.md never got those alerts. The dedupe key holds the file."""
    from tools.gb2lib import watch_ops

    scratch = tmp_path / "scratch" / "ALERTS.md"
    real = tmp_path / "real" / "ALERTS.md"
    assert watch_ops.post_ops_alert(workspace.ledger, "alpha", "gongbang_job_failed",
                                    "A-1", "check run", path=scratch) is True
    assert watch_ops.post_ops_alert(workspace.ledger, "alpha", "gongbang_job_failed",
                                    "A-1", "service pass", path=real) is True
    assert real.read_text(encoding="utf-8").count("gongbang_job_failed") == 1
