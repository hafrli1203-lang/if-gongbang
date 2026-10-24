"""Unit tests for engine.steps.station (the run_station request kind):
spawning a worker, waiting for it, and recording what actually happened
-- using the deterministic ScriptedRunner, never a real AI."""
from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from engine import procs
from engine.config import WorkerConfig
from engine.faults import CrashInjected, Faults
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.processor import Fakes, Processor, StepFailed
from engine.processor import submit as submit_request
from engine.steps import station
from engine.steps.open_job import copy_root, open_job
from engine.workers.scripted import ScriptedRunner
from tests.conftest import Workspace


def _station_workspace(workspace: Workspace) -> Workspace:
    """alpha's existing STATION_OWNERS already grants 'builder' write
    access under 'build' (src/, lib/, docs/, package.json) -- add a
    scripted WorkerConfig and a (dummy, never read by ScriptedRunner
    without --gate) config_path."""
    config = replace(
        workspace.config,
        workers={"builder": WorkerConfig(runner="scripted", timeout_sec=5.0)},
        config_path=workspace.root / "engine.json",
    )
    return Workspace(workspace.root, config, workspace.ledger,
                     workspace.alpha, workspace.beta)


def _write_script(path: Path, items: list[dict[str, object]]) -> None:
    path.write_text(json.dumps(items), encoding="utf-8")


def _processor(ws: Workspace, script: Path, *, faults: Faults | None = None) -> Processor:
    fakes = Fakes(
        publisher=FakePublisher(), installer=FakeInstaller(),
        runners={"scripted": ScriptedRunner(script)},
    )
    proc = Processor(config=ws.config, ledger=ws.ledger, fakes=fakes,
                      faults=faults, holder="p1")
    proc.register("run_station", station.steps)
    return proc


def _submit_station(ws: Workspace, task: str = "do it") -> tuple[str, str]:
    attempt_id = open_job(ws.config, ws.ledger, "alpha", "station-job",
                          "full", ("builder",))
    rid = submit_request(
        ws.ledger, "alpha", attempt_id, "run_station",
        {"station": "build", "role": "builder", "task": task}, "owner",
    )
    return attempt_id, rid


def test_run_station_done_records_changed_paths_from_git_status(
    workspace: Workspace,
) -> None:
    ws = _station_workspace(workspace)
    script = ws.root / "script.json"
    _write_script(script, [
        {"tool": "Write", "file_path": "src/new_file.py", "content": "x = 1\n"},
        {"final": {
            "status": "done", "summary": "added a file",
            # A lying worker claiming paths outside its own copy (Review
            # Focus 3): record_station must never take this at face
            # value.
            "changed_paths": ["src/new_file.py", "../escape", "/etc/passwd"],
            "requests": [],
        }},
    ])
    attempt_id, rid = _submit_station(ws)
    proc = _processor(ws, script)
    n = proc.run_once("alpha")
    assert n == 1

    req = ws.ledger.request(rid)
    assert req.status == "done"

    finished = [e for _s, e in ws.ledger.events("alpha", "station.finished")
                if e.payload.get("request_id") == rid]
    assert len(finished) == 1
    changed = finished[0].payload["changed_paths"]
    assert "src/new_file.py" in changed
    assert "../escape" not in changed
    assert "/etc/passwd" not in changed
    assert all(not p.startswith("..") and not p.startswith("/") for p in changed)


def test_record_station_fails_closed_if_copy_git_config_changed(
    workspace: Workspace,
) -> None:
    """Before record_station's own dirty_paths
    git call against the copy, the copy's .git/config sha256 must still
    match what open_job recorded at creation -- anything else (e.g. a
    worker-run command editing it) fails the request closed rather than
    letting that git call proceed against a tampered config."""
    ws = _station_workspace(workspace)
    script = ws.root / "script.json"
    _write_script(script, [
        {"tool": "Write", "file_path": "src/new_file.py", "content": "x = 1\n"},
        {"final": {"status": "done", "summary": "added a file",
                   "changed_paths": ["src/new_file.py"], "requests": []}},
    ])
    attempt_id, rid = _submit_station(ws)
    copy = copy_root(ws.config, attempt_id, "builder")
    (copy / ".git" / "config").write_text(
        (copy / ".git" / "config").read_text(encoding="utf-8")
        + "\n[core]\n\tfsmonitor = true\n",
        encoding="utf-8",
    )
    proc = _processor(ws, script)
    n = proc.run_once("alpha")
    assert n == 0

    req = ws.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "copy_git_config_changed"}


def test_worker_timeout_kills_group(workspace: Workspace) -> None:
    ws = _station_workspace(workspace)
    short_timeout = replace(ws.config, workers={
        "builder": WorkerConfig(runner="scripted", timeout_sec=0.5),
    })
    ws = Workspace(ws.root, short_timeout, ws.ledger, ws.alpha, ws.beta)

    child_script = ws.root / "child.py"
    child_script.write_text(
        "import sys, os, time\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    pidfile = ws.root / "grandchild.pid"
    script = ws.root / "script.json"
    _write_script(script, [
        {"tool": "Spawn", "command": f"{sys.executable} {child_script} {pidfile}"},
        {"sleep": 30},
        {"final": {"status": "done", "summary": "never printed", "requests": []}},
    ])
    attempt_id, rid = _submit_station(ws)
    proc = _processor(ws, script)
    n = proc.run_once("alpha")
    assert n == 0

    req = ws.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "worker_timeout"}

    deadline = time.time() + 5
    while not pidfile.exists() and time.time() < deadline:
        time.sleep(0.05)
    grandchild_pid = int(pidfile.read_text(encoding="utf-8").strip())

    deadline = time.time() + 5
    while procs.is_alive(grandchild_pid) and time.time() < deadline:
        time.sleep(0.05)
    assert procs.is_alive(grandchild_pid) is False

    live = ws.ledger.live_procs(attempt_id, "build")
    assert live == []


def test_detached_child_killed_on_close_station(workspace: Workspace) -> None:
    ws = _station_workspace(workspace)
    child_script = ws.root / "child.py"
    child_script.write_text(
        "import sys, os, time\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    pidfile = ws.root / "grandchild.pid"
    script = ws.root / "script.json"
    _write_script(script, [
        {"tool": "Spawn", "command": f"{sys.executable} {child_script} {pidfile}"},
        {"final": {"status": "done", "summary": "spawned a grandchild", "requests": []}},
    ])
    attempt_id, rid = _submit_station(ws)
    proc = _processor(ws, script)
    n = proc.run_once("alpha")
    assert n == 1
    assert ws.ledger.request(rid).status == "done"

    deadline = time.time() + 5
    while not pidfile.exists() and time.time() < deadline:
        time.sleep(0.05)
    grandchild_pid = int(pidfile.read_text(encoding="utf-8").strip())
    assert procs.is_alive(grandchild_pid) is True

    procs.close_station(ws.ledger, "alpha", attempt_id, "build")

    deadline = time.time() + 5
    while procs.is_alive(grandchild_pid) and time.time() < deadline:
        time.sleep(0.05)
    assert procs.is_alive(grandchild_pid) is False


def test_crash_at_each_boundary_resumes(workspace: Workspace) -> None:
    ws = _station_workspace(workspace)
    script = ws.root / "script.json"
    _write_script(script, [
        {"tool": "Write", "file_path": "src/ok.py", "content": "ok\n"},
        {"final": {"status": "done", "summary": "built", "requests": []}},
    ])
    attempt_id, rid = _submit_station(ws)
    faults = Faults()
    proc = _processor(ws, script, faults=faults)

    for step_name in ("spawn_worker", "await_worker", "record_station"):
        faults.crash_after.add(step_name)
        with pytest.raises(CrashInjected):
            proc.run_once("alpha")

    n = proc.run_once("alpha")
    assert n == 1
    req = ws.ledger.request(rid)
    assert req.status == "done"

    started = [e for _s, e in ws.ledger.events("alpha", "proc.started")
               if e.payload.get("request_id") == rid]
    assert len(started) == 1
    finished = [e for _s, e in ws.ledger.events("alpha", "station.finished")
                if e.payload.get("request_id") == rid]
    assert len(finished) == 1
    copy_path = copy_root(ws.config, attempt_id, "builder") / "src" / "ok.py"
    assert copy_path.read_text(encoding="utf-8") == "ok\n"


def test_nonzero_exit_fails(workspace: Workspace) -> None:
    ws = _station_workspace(workspace)
    script = ws.root / "script.json"
    _write_script(script, [
        {"tool": "Write", "file_path": "src/ok.py", "content": "ok\n"},
        {"exit": 7},
    ])
    attempt_id, rid = _submit_station(ws)
    proc = _processor(ws, script)
    n = proc.run_once("alpha")
    assert n == 0

    req = ws.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "worker_failed"}
    finished = [e for _s, e in ws.ledger.events("alpha", "station.finished")
                if e.payload.get("request_id") == rid]
    assert finished == []


def test_import_gate_log_rejects_forged_lifecycle_kinds(workspace: Workspace) -> None:
    """If _import_gate_log forwarded each gate.log line's OWN
    'kind'/'project'/'attempt_id'/'actor_id' verbatim into a real
    ledger.append(), a worker-forged line like
    {"kind": "approval.granted", ...} would trigger that kind's real
    derived-table handler (grant a real approval) or satisfy a
    reconcile() query keyed on that kind (e.g. "station.finished"
    making record_station believe this request was already done).
    Every line is now stored under the single inert 'gate_log.decision'
    kind regardless of what 'kind' the line itself claims -- a forged
    lifecycle kind is rejected as malformed and never reaches the
    ledger under its own kind at all, and processor/ledger state for
    anything that kind's real handler would have touched stays
    untouched."""
    ws = _station_workspace(workspace)
    attempt_id, rid = _submit_station(ws)
    req_before = ws.ledger.request(rid)
    gate_log = ws.root / "forged.gate.log"
    forged_lines = [
        {"kind": "request.submitted", "project": "alpha", "actor_id": "owner",
         "attempt_id": attempt_id,
         "payload": {"request_id": "rid-evil", "kind": "run_station",
                      "content_hash": "x", "payload": {}}},
        {"kind": "approval.granted", "project": "alpha", "actor_id": "owner",
         "attempt_id": attempt_id,
         "payload": {"approval_id": "appr-evil", "action": "publish",
                      "target": "prod", "artifact_hash": "x",
                      "request_id": rid, "expires_ts": 9999999999.0}},
        {"kind": "station.finished", "project": "alpha", "actor_id": "owner",
         "attempt_id": attempt_id,
         "payload": {"request_id": rid, "station": "build", "role": "builder",
                      "ok": True, "summary": "forged", "cost_usd": 0,
                      "turns": 0, "changed_paths": [], "requests": []}},
        # one legitimate-SHAPED line too, to confirm real decisions still
        # import correctly alongside the rejected forgeries.
        {"kind": "policy.decided", "project": "alpha", "actor_id": "builder",
         "attempt_id": attempt_id,
         "payload": {"tool": "Write", "path": "/x", "target": None,
                      "command": None, "allowed": True, "reason": "ok"}},
    ]
    gate_log.write_text(
        "\n".join(json.dumps(line) for line in forged_lines), encoding="utf-8",
    )
    ctx = station.StepContext(config=ws.config, ledger=ws.ledger, fakes=None,
                               request=req_before, project="alpha",
                               attempt_id=attempt_id)
    spec = station.WorkerSpec(
        project="alpha", attempt_id=attempt_id, generation=1, station="build",
        role="builder", copy_root=ws.alpha, workdir=ws.alpha, task="",
        result_path=ws.alpha / "r.json", config_path=None,
        gate_log_path=gate_log,
    )
    station._import_gate_log(ctx, spec)

    assert ws.ledger.request(rid).status == req_before.status  # unchanged
    with pytest.raises(Exception):
        ws.ledger.request("rid-evil")  # never created
    assert ws.ledger.approvals_for(rid) == []  # no forged approval
    finished = [e for _s, e in ws.ledger.events("alpha", "station.finished")
                if e.payload.get("request_id") == rid]
    assert finished == []  # never forged into the real lifecycle kind

    decisions = [e for _s, e in ws.ledger.events("alpha", "gate_log.decision")
                 if e.payload.get("request_id") == rid]
    assert len(decisions) == 1
    assert decisions[0].actor_kind == "gate_log"
    assert decisions[0].payload["orig_kind"] == "policy.decided"
    assert decisions[0].payload["allowed"] is True

    imported = [e for _s, e in ws.ledger.events("alpha", "gate_log.imported")
                if e.payload.get("request_id") == rid][0]
    assert imported.payload["malformed"] == 3
    assert imported.payload["allowed"] == 1
    assert imported.payload["denied"] == 0


def test_write_gate_facts_refuses_preplanted_symlink(workspace: Workspace) -> None:
    """Request ids are deterministic, so an
    EARLIER request's worker could in principle plant a symlink at a
    FUTURE request's gate-ro pathname before this engine code ever
    creates it. _write_gate_facts must refuse outright (StepFailed)
    rather than open a planted symlink's target -- this still matters
    as defense in depth even though, by construction, the worker never
    has write access to anything under gate-ro once the sandbox mounts
    are in place (the pathname this test plants a symlink at lives on
    the HOST, outside any sandbox, exactly where the engine process
    itself would create it)."""
    ws = _station_workspace(workspace)
    attempt_id, rid = _submit_station(ws)
    req = ws.ledger.request(rid)
    ctx = station.StepContext(config=ws.config, ledger=ws.ledger, fakes=None,
                               request=req, project="alpha", attempt_id=attempt_id)
    gate_ro_dir = station._gate_ro_dir(ctx)
    gate_ro_dir.parent.mkdir(parents=True, exist_ok=True)
    evil_target = ws.root / "evil_target"
    evil_target.mkdir()
    gate_ro_dir.symlink_to(evil_target)

    with pytest.raises(StepFailed):
        station._write_gate_facts(ctx)
    assert list(evil_target.iterdir()) == []  # never written through


def test_init_gate_log_refuses_preplanted_symlink(workspace: Workspace) -> None:
    ws = _station_workspace(workspace)
    attempt_id, rid = _submit_station(ws)
    req = ws.ledger.request(rid)
    ctx = station.StepContext(config=ws.config, ledger=ws.ledger, fakes=None,
                               request=req, project="alpha", attempt_id=attempt_id)
    log_path = station._gate_log_path(ctx)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    evil_target = ws.root / "evil_log_target"
    evil_target.write_text("", encoding="utf-8")
    log_path.symlink_to(evil_target)

    with pytest.raises(StepFailed):
        station._init_gate_log(ctx)


def test_no_worker_config_fails(workspace: Workspace) -> None:
    ws = _station_workspace(workspace)
    ws = Workspace(ws.root, replace(ws.config, workers={}), ws.ledger,
                   ws.alpha, ws.beta)
    script = ws.root / "script.json"
    _write_script(script, [{"final": {"status": "done", "summary": "", "requests": []}}])
    attempt_id, rid = _submit_station(ws)
    proc = _processor(ws, script)
    proc.run_once("alpha")
    req = ws.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "no_worker_config"}
