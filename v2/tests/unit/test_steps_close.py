"""Unit tests for engine.steps.close: full close/abandon flow via the
Processor, plus direct checks of the archive_commit reconcile predicate."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import engine.steps.close as close_module
from engine import ids, procs, state
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.faults import CrashInjected, Faults
from engine.processor import Fakes, Processor, StepContext, submit
from engine.steps.close import _archive_root, _final_dir, _reconcile_archive_commit, _tmp_dir
from engine.steps.open_job import open_job, recover_job
from tests.conftest import Workspace, git


def _proc(workspace: Workspace) -> Processor:
    proc = Processor(workspace.config, workspace.ledger,
                      Fakes(FakePublisher(), FakeInstaller()))
    proc.register_defaults()
    return proc


def test_close_archives_change_and_resets_tree(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job1",
                          "full", roles=("builder",))
    (workspace.alpha / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")

    rid = submit(workspace.ledger, "alpha", attempt_id, "close",
                 {"mode": "close"}, "owner")
    proc = _proc(workspace)
    n = proc.run_once("alpha")

    assert n == 1
    assert workspace.ledger.request(rid).status == "done"
    assert workspace.ledger.project("alpha").state == "CLOSED"
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('alpha')\n"

    archive_root = workspace.config.home / "archive" / "alpha" / ids.attempt_dirname(attempt_id)
    final_dirs = [p for p in archive_root.iterdir() if not p.name.startswith(".tmp-")]
    assert len(final_dirs) == 1
    assert (final_dirs[0] / "src" / "app.py").read_text(encoding="utf-8") == "print('changed')\n"


def test_abandon_marks_attempt_abandoned(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job2",
                          "full", roles=("builder",))
    (workspace.alpha / "src" / "app.py").write_text("print('scrap')\n", encoding="utf-8")

    submit(workspace.ledger, "alpha", attempt_id, "abandon",
           {"mode": "abandon"}, "owner")
    proc = _proc(workspace)
    n = proc.run_once("alpha")

    assert n == 1
    assert workspace.ledger.project("alpha").state == "CLOSED"
    finished = workspace.ledger.events("alpha", "attempt.finished")
    assert finished[-1][1].payload == {"attempt_id": attempt_id, "status": "abandoned"}


def test_archive_commit_reconcile_false_when_only_tmp_exists(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job3",
                          "full", roles=("builder",))
    rid = submit(workspace.ledger, "alpha", attempt_id, "close",
                 {"mode": "close"}, "owner")
    request = workspace.ledger.request(rid)
    ctx = StepContext(config=workspace.config, ledger=workspace.ledger,
                      fakes=Fakes(FakePublisher(), FakeInstaller()),
                      request=request, project="alpha", attempt_id=attempt_id)
    tmp = _tmp_dir(ctx)
    tmp.mkdir(parents=True)
    assert _reconcile_archive_commit(ctx) is False


def test_archive_commit_reconcile_true_when_dir_and_event_exist(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job4",
                          "full", roles=("builder",))
    rid = submit(workspace.ledger, "alpha", attempt_id, "close",
                 {"mode": "close"}, "owner")
    request = workspace.ledger.request(rid)
    ctx = StepContext(config=workspace.config, ledger=workspace.ledger,
                      fakes=Fakes(FakePublisher(), FakeInstaller()),
                      request=request, project="alpha", attempt_id=attempt_id)
    final = _final_dir(ctx)
    final.mkdir(parents=True)
    from engine.ledger import Event
    workspace.ledger.append(Event(
        project="alpha", kind="archive.done", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"attempt_id": attempt_id, "path": str(final)},
    ))
    assert _reconcile_archive_commit(ctx) is True


def test_set_closed_resume_after_crash_between_its_two_effects_does_not_duplicate(
    workspace: Workspace,
) -> None:
    """_run_set_closed's own run() does two durable things in order:
    append 'attempt.finished', then state.transition CLOSING -> CLOSED.
    A crash between them (simulated here: append attempt.finished
    directly, leaving the project in CLOSING, exactly where the real
    step would be caught mid-way) used to make a resumed call to
    run() (the processor's own resume rule re-runs 'started' steps
    whose reconcile is False -- _reconcile_set_closed is "project.state
    == CLOSED", still False here) re-append a SECOND attempt.finished
    event for an attempt that was already finished, rather than
    noticing it and going straight to the state transition it never
    got to the first time."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job-crash",
                          "full", roles=("builder",))
    rid = submit(workspace.ledger, "alpha", attempt_id, "close", {"mode": "close"}, "owner")
    request = workspace.ledger.request(rid)
    ctx = StepContext(config=workspace.config, ledger=workspace.ledger,
                      fakes=Fakes(FakePublisher(), FakeInstaller()),
                      request=request, project="alpha", attempt_id=attempt_id)
    # Move the project to CLOSING the same way set_closing's own run()
    # does, then simulate the crash: only the FIRST of set_closed's two
    # effects landed.
    state.transition(workspace.ledger, "alpha", "close_job", "owner")
    from engine.ledger import Event

    workspace.ledger.append(Event(
        project="alpha", kind="attempt.finished", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"attempt_id": attempt_id, "status": "closed"},
    ))
    assert workspace.ledger.project("alpha").state == "CLOSING"
    assert workspace.ledger.attempt(attempt_id).status == "closed"

    close_module._run_set_closed(ctx)

    assert workspace.ledger.project("alpha").state == "CLOSED"
    assert len(workspace.ledger.events("alpha", "attempt.finished")) == 1


def test_verify_tree_eq_head_failure_leaves_project_stuck_in_closing(
    workspace: Workspace, monkeypatch
) -> None:
    """P1-8 (context): unlike an archive_tmp/archive_commit failure (which
    calls _on_archive_failure to transition back to OPEN/RECOVERING),
    verify_tree_eq_head failing leaves the project in CLOSING with no
    transition out. A later set_closing for a new close request must not
    assume a clean OPEN/RECOVERING start."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job-stuck",
                          "full", roles=("builder",))
    rid1 = submit(workspace.ledger, "alpha", attempt_id, "close", {"mode": "close"}, "owner")

    monkeypatch.setattr(close_module, "fingerprint", lambda repo, excludes: "x" * 40)
    n1 = _proc(workspace).run_once("alpha")

    assert n1 == 0
    req1 = workspace.ledger.request(rid1)
    assert req1.status == "failed"
    assert req1.result == {"reason": "tree_ne_head"}
    assert workspace.ledger.project("alpha").state == "CLOSING"


def test_run_set_closing_idempotent_when_state_already_closing(workspace: Workspace) -> None:
    """P1-8: set_closing's own run() must not call state.transition again
    when the project is already CLOSING (e.g. left stuck by an earlier
    close request's verify_tree_eq_head failure) — (CLOSING, close_job)
    and (CLOSING, abandon_job) are not in TRANSITIONS, so doing so would
    raise TransitionError instead of letting a new close request proceed.
    It must still report the state that was CLOSING's prior state and
    still close any live station processes."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job-stuck2",
                          "full", roles=("builder",))
    rid = submit(workspace.ledger, "alpha", attempt_id, "close", {"mode": "close"}, "owner")
    request = workspace.ledger.request(rid)
    ctx = StepContext(config=workspace.config, ledger=workspace.ledger,
                      fakes=Fakes(FakePublisher(), FakeInstaller()),
                      request=request, project="alpha", attempt_id=attempt_id)

    # Simulate a prior close request's set_closing having already moved
    # the project to CLOSING (and then failing later, e.g. on
    # verify_tree_eq_head, with no transition back out).
    state.transition(workspace.ledger, "alpha", "close_job", "processor")
    assert workspace.ledger.project("alpha").state == "CLOSING"

    out = close_module._run_set_closing(ctx)  # must not raise TransitionError

    assert out["prior"] == "OPEN"
    assert out["mode"] == "close"
    assert workspace.ledger.project("alpha").state == "CLOSING"


def test_close_kills_live_station_processes(workspace: Workspace) -> None:
    """P2-4: close never called procs.close_station, so child processes
    spawned by a station outlived the close. set_closing must kill them."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job-procs",
                          "full", roles=("builder",))
    proc_handle = procs.spawn(workspace.ledger, "alpha", attempt_id, "build",
                              ["sleep", "30"], cwd=workspace.alpha)
    assert _is_alive(proc_handle.pid)

    submit(workspace.ledger, "alpha", attempt_id, "close", {"mode": "close"}, "owner")
    n = _proc(workspace).run_once("alpha")

    assert n == 1
    assert workspace.ledger.project("alpha").state == "CLOSED"
    deadline = time.time() + 2.0
    while _is_alive(proc_handle.pid) and time.time() < deadline:
        time.sleep(0.05)
    assert not _is_alive(proc_handle.pid)


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_reset_to_head_removes_staged_but_uncommitted_new_file(workspace: Workspace) -> None:
    """P2-8: an index-only added file (git add, never committed) makes
    `git checkout HEAD -- path` fail because the path is not in HEAD.
    reset_to_head must unstage and delete it instead."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job-added",
                          "full", roles=("builder",))
    new_file = workspace.alpha / "src" / "new_thing.py"
    new_file.write_text("print('new')\n", encoding="utf-8")
    git(workspace.alpha, "add", "src/new_thing.py")

    submit(workspace.ledger, "alpha", attempt_id, "close", {"mode": "close"}, "owner")
    n = _proc(workspace).run_once("alpha")

    assert n == 1
    assert workspace.ledger.project("alpha").state == "CLOSED"
    assert not new_file.exists()
    status = subprocess.run(
        ["git", "-C", str(workspace.alpha), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert status == ""


def test_reset_to_head_refuses_change_made_after_archive_commit(workspace: Workspace) -> None:
    """C-9: archive_tmp/archive_commit are 'done' by the time reset_to_head
    runs. A crash after archive_commit followed by a further edit to the
    same file must not let reset_to_head silently reset/discard that new,
    unarchived content — it must fail closed and leave the file (and the
    project state) untouched."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job-c9",
                          "full", roles=("builder",))
    (workspace.alpha / "src" / "app.py").write_text("print('archived-version')\n",
                                                     encoding="utf-8")
    rid = submit(workspace.ledger, "alpha", attempt_id, "close", {"mode": "close"}, "owner")

    proc1 = Processor(workspace.config, workspace.ledger, Fakes(FakePublisher(), FakeInstaller()),
                      faults=Faults(crash_after={"archive_commit"}))
    proc1.register_defaults()
    try:
        proc1.run_once("alpha")
    except CrashInjected:
        pass

    # A worker edits the same file further, after it was already archived.
    (workspace.alpha / "src" / "app.py").write_text("print('unarchived-edit')\n",
                                                     encoding="utf-8")

    proc2 = _proc(workspace)
    n = proc2.run_once("alpha")

    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "changed_after_archive"}
    assert workspace.ledger.project("alpha").state == "CLOSING"
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('unarchived-edit')\n"


def test_reset_to_head_allows_change_matching_the_archive(workspace: Workspace) -> None:
    """C-9 (control): a crash after archive_commit with NO further edits —
    the live change still matches the archived bytes exactly — must still
    let reset_to_head and the rest of close proceed normally."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job-c9-ok",
                          "full", roles=("builder",))
    (workspace.alpha / "src" / "app.py").write_text("print('archived-version')\n",
                                                     encoding="utf-8")
    rid = submit(workspace.ledger, "alpha", attempt_id, "close", {"mode": "close"}, "owner")

    proc1 = Processor(workspace.config, workspace.ledger, Fakes(FakePublisher(), FakeInstaller()),
                      faults=Faults(crash_after={"archive_commit"}))
    proc1.register_defaults()
    try:
        proc1.run_once("alpha")
    except CrashInjected:
        pass

    proc2 = _proc(workspace)
    n = proc2.run_once("alpha")

    assert n == 1
    assert workspace.ledger.request(rid).status == "done"
    assert workspace.ledger.project("alpha").state == "CLOSED"
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('alpha')\n"


def test_reset_to_head_refuses_chmod_after_archive_commit(workspace: Workspace) -> None:
    """#9: the archive guard must compare mode (executable bit), not just
    bytes. A crash after archive_commit followed by only a chmod +x (same
    content) on the archived file must still fail closed."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job-chmod",
                          "full", roles=("builder",))
    target = workspace.alpha / "src" / "app.py"
    target.write_text("print('archived-version')\n", encoding="utf-8")
    rid = submit(workspace.ledger, "alpha", attempt_id, "close", {"mode": "close"}, "owner")

    proc1 = Processor(workspace.config, workspace.ledger, Fakes(FakePublisher(), FakeInstaller()),
                      faults=Faults(crash_after={"archive_commit"}))
    proc1.register_defaults()
    try:
        proc1.run_once("alpha")
    except CrashInjected:
        pass

    target.chmod(target.stat().st_mode | 0o111)  # +x, bytes unchanged

    proc2 = _proc(workspace)
    n = proc2.run_once("alpha")

    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "changed_after_archive"}
    assert workspace.ledger.project("alpha").state == "CLOSING"
    assert os.access(target, os.X_OK)  # untouched


def test_set_closing_requires_report_when_prior_recovering_without_report(
    workspace: Workspace, monkeypatch
) -> None:
    """#16: set_closing's idempotent skip (project already CLOSING) must
    not bypass the (RECOVERING, close_job) report guard that a real
    state.transition() call would have enforced."""
    open_job(workspace.config, workspace.ledger, "alpha", "job-recovering",
             "full", roles=("builder",))
    (workspace.alpha / "src" / "app.py").write_text("print('recovering')\n", encoding="utf-8")
    attempt_id = recover_job(workspace.config, workspace.ledger, "alpha", "owner")
    assert workspace.ledger.project("alpha").state == "RECOVERING"

    # Drive to CLOSING via abandon (no report needed for abandon_job),
    # then make verify_tree_eq_head fail so the project is left stuck in
    # CLOSING with prior == RECOVERING and no report.
    rid_abandon = submit(workspace.ledger, "alpha", attempt_id, "abandon", {}, "owner")
    monkeypatch.setattr(close_module, "fingerprint", lambda repo, excludes: "x" * 40)
    n1 = _proc(workspace).run_once("alpha")
    assert n1 == 0
    assert workspace.ledger.request(rid_abandon).status == "failed"
    assert workspace.ledger.project("alpha").state == "CLOSING"

    monkeypatch.undo()
    rid_close = submit(workspace.ledger, "alpha", attempt_id, "close", {"mode": "close"}, "owner")
    n2 = _proc(workspace).run_once("alpha")

    assert n2 == 0
    req_close = workspace.ledger.request(rid_close)
    assert req_close.status == "failed"
    assert req_close.result == {"reason": "report_required"}
    assert workspace.ledger.project("alpha").state == "CLOSING"

    from engine.ledger import Event
    workspace.ledger.append(Event(
        project="alpha", kind="report.written", actor_kind="processor",
        actor_id="proc1", attempt_id=attempt_id, payload={},
    ))
    rid_close2 = submit(workspace.ledger, "alpha", attempt_id, "close", {"mode": "close2"}, "owner")
    n3 = _proc(workspace).run_once("alpha")

    assert n3 == 1
    assert workspace.ledger.request(rid_close2).status == "done"
    assert workspace.ledger.project("alpha").state == "CLOSED"


def test_reset_to_head_restores_original_path_after_rename(workspace: Workspace) -> None:
    """#24: a `git status -z` rename entry carries both the new and the
    original path. reset_to_head must drop the new path (HEAD has no
    entry under that name) and restore the original path from HEAD,
    rather than trying (and failing) to check out HEAD at the new name."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job-rename",
                          "full", roles=("builder",))
    git(workspace.alpha, "mv", "src/app.py", "src/renamed.py")

    rid = submit(workspace.ledger, "alpha", attempt_id, "close", {"mode": "close"}, "owner")
    n = _proc(workspace).run_once("alpha")

    assert n == 1
    assert workspace.ledger.request(rid).status == "done"
    assert workspace.ledger.project("alpha").state == "CLOSED"
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('alpha')\n"
    assert not (workspace.alpha / "src" / "renamed.py").exists()
    status = subprocess.run(
        ["git", "-C", str(workspace.alpha), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert status == ""


def test_set_closing_resumes_and_kills_procs_left_alive_after_transition(
    workspace: Workspace,
) -> None:
    """N4 (set_closing reconcile): state already CLOSING is not proof the
    station-kill loop ran to completion. With a live proc still recorded
    for this attempt, reconcile must say "not done yet" so the step's
    run() is called again (idempotent: transition skipped, close_station
    re-run) and actually kills it, rather than treating "state is
    CLOSING" alone as "this step finished"."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job-live-proc",
                          "full", roles=("builder",))
    proc_handle = procs.spawn(workspace.ledger, "alpha", attempt_id, "build",
                              ["sleep", "30"], cwd=workspace.alpha)
    assert _is_alive(proc_handle.pid)

    rid = submit(workspace.ledger, "alpha", attempt_id, "close", {"mode": "close"}, "owner")
    # Simulate a crash mid set_closing: the state transition already
    # landed, but close_station was never reached.
    state.transition(workspace.ledger, "alpha", "close_job", "processor")
    from engine.ledger import Event
    workspace.ledger.append(Event(
        project="alpha", kind="request.step", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"request_id": rid, "step_name": "set_closing", "status": "started",
                  "payload": {}},
    ))

    n = _proc(workspace).run_once("alpha")

    assert n == 1
    assert workspace.ledger.request(rid).status == "done"
    assert workspace.ledger.project("alpha").state == "CLOSED"
    deadline = time.time() + 2.0
    while _is_alive(proc_handle.pid) and time.time() < deadline:
        time.sleep(0.05)
    assert not _is_alive(proc_handle.pid)


def _close_crashing_at(workspace: Workspace, boundary: str, monkeypatch) -> str:
    """Opens an attempt, edits the tree, and runs its close request with
    a crash armed at the named flow boundary inside set_closed."""
    from engine import faults

    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", f"job-{boundary}",
                          "full", roles=("builder",))
    (workspace.alpha / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    submit(workspace.ledger, "alpha", attempt_id, "close", {"mode": "close"}, "owner")
    monkeypatch.setattr(faults, "ACTIVE_FLOW_FAULTS", Faults(crash_before={boundary}))
    try:
        _proc(workspace).run_once("alpha")
        raise AssertionError("expected CrashInjected")
    except CrashInjected:
        pass
    monkeypatch.setattr(faults, "ACTIVE_FLOW_FAULTS", None)
    return attempt_id


def _attempt_finished_count(workspace: Workspace, attempt_id: str) -> int:
    return sum(1 for _seq, e in workspace.ledger.events("alpha", "attempt.finished")
               if e.attempt_id == attempt_id)


def test_crash_before_attempt_finished_resumes_to_closed_once(
    workspace: Workspace, monkeypatch,
) -> None:
    attempt_id = _close_crashing_at(workspace, "close.attempt_finished", monkeypatch)
    assert workspace.ledger.attempt(attempt_id).status == "active"
    assert workspace.ledger.project("alpha").state == "CLOSING"

    _proc(workspace).run_once("alpha")

    assert workspace.ledger.project("alpha").state == "CLOSED"
    assert workspace.ledger.attempt(attempt_id).status == "closed"
    assert _attempt_finished_count(workspace, attempt_id) == 1


def test_crash_between_attempt_finished_and_state_closed_resumes_once(
    workspace: Workspace, monkeypatch,
) -> None:
    """Flow-map boundary 12: the attempt is finished, the project still
    CLOSING. Resuming only finishes the state transition."""
    attempt_id = _close_crashing_at(workspace, "close.state_closed", monkeypatch)
    assert workspace.ledger.attempt(attempt_id).status == "closed"
    assert workspace.ledger.project("alpha").state == "CLOSING"

    _proc(workspace).run_once("alpha")

    assert workspace.ledger.project("alpha").state == "CLOSED"
    assert _attempt_finished_count(workspace, attempt_id) == 1
