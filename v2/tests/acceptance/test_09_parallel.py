"""Acceptance test 09 — parallel workers.

Two worker copies editing disjoint allowed paths both land in the repo on
merge (A). The same file edited differently by two copies fails the whole
request as a conflict and writes nothing (B). The owner's pre-existing
dirty edit survives a merge that touches the same directory (C). Two
install requests queued together are processed one at a time, in order,
by the single processor (D). A crash at any boundary of any of the 4
merge steps recovers to the same result as an uncrashed run, whether the
crash lands before or after the step's run() (E); a conflicting merge
crashed before check_conflicts still fails with merge_conflict and writes
nothing once resumed (F).
"""
from __future__ import annotations

import dataclasses

import pytest

from engine.config import EngineConfig
from engine.faults import CrashInjected, Faults
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.ledger import Event
from engine.processor import Fakes, Processor, submit
from engine.steps import install, merge
from engine.steps.open_job import copy_root, open_job
from tests.conftest import Workspace


def _processor(workspace: Workspace, holder: str = "p1",
                faults: Faults | None = None,
                config: EngineConfig | None = None) -> Processor:
    fakes = Fakes(publisher=FakePublisher(), installer=FakeInstaller())
    proc = Processor(config=config or workspace.config, ledger=workspace.ledger,
                      fakes=fakes, holder=holder, faults=faults)
    proc.register("merge", merge.steps)
    proc.register("install", install.steps)
    return proc


def _overlap_config(config: EngineConfig) -> EngineConfig:
    """A config variant adding a second, synthetic role ("shadow"/
    "shadow_builder") whose allowed prefix overlaps "build"/"builder"'s
    "src/". copy_root is a pure function of (attempt_id, role), so two
    copies in one merge request can never legitimately claim the SAME
    role (that is exactly the attack #2 closes); a scenario that needs
    two independently-edited copies able to touch the same path uses two
    distinct, really-different roles instead. Built locally with
    dataclasses.replace -- never touches the shared conftest fixture."""
    return dataclasses.replace(config, station_owners={
        **config.station_owners,
        "shadow": {"shadow_builder": ("src/",)},
    })


def test_scenario_a_disjoint_edits_both_preserved(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "parallel-a",
                           "full", ("builder", "tester"))
    copy1 = copy_root(workspace.config, attempt_id, "builder")
    copy2 = copy_root(workspace.config, attempt_id, "tester")
    (copy1 / "src" / "app.py").write_text("print('one')\n", encoding="utf-8")
    (copy2 / "tests" / "bench" / "cases.json").write_text("[1]\n", encoding="utf-8")

    rid = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [
            {"role": "builder", "station": "build", "path": str(copy1)},
            {"role": "tester", "station": "register", "path": str(copy2)},
        ],
    }, "owner")
    proc = _processor(workspace)
    assert proc.run_once("alpha") == 1
    assert workspace.ledger.request(rid).status == "done"
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('one')\n"
    assert (workspace.alpha / "tests" / "bench" / "cases.json").read_text(encoding="utf-8") == "[1]\n"


def test_scenario_b_conflicting_edit_fails_nothing_written(workspace: Workspace) -> None:
    cfg = _overlap_config(workspace.config)
    attempt_id = open_job(cfg, workspace.ledger, "alpha", "parallel-b",
                           "full", ("builder", "shadow_builder"))
    copy1 = copy_root(cfg, attempt_id, "builder")
    copy2 = copy_root(cfg, attempt_id, "shadow_builder")
    (copy1 / "src" / "app.py").write_text("print('one')\n", encoding="utf-8")
    (copy2 / "src" / "app.py").write_text("print('two')\n", encoding="utf-8")

    rid = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [
            {"role": "builder", "station": "build", "path": str(copy1)},
            {"role": "shadow_builder", "station": "shadow", "path": str(copy2)},
        ],
    }, "owner")
    proc = _processor(workspace, config=cfg)
    assert proc.run_once("alpha") == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "merge_conflict"}
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('alpha')\n"


def test_scenario_c_owner_dirty_path_survives_merge(workspace: Workspace) -> None:
    (workspace.alpha / "docs" / "readme.md").write_text("owner edit\n", encoding="utf-8")
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "parallel-c",
                           "full", ("builder",))
    assert workspace.ledger.project("alpha").dirty_paths == ("docs/readme.md",)

    copy1 = copy_root(workspace.config, attempt_id, "builder")
    (copy1 / "docs" / "readme.md").write_text("worker edit\n", encoding="utf-8")

    rid = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [{"role": "builder", "station": "build", "path": str(copy1)}],
    }, "owner")
    proc = _processor(workspace)
    assert proc.run_once("alpha") == 1
    assert workspace.ledger.request(rid).status == "done"
    assert (workspace.alpha / "docs" / "readme.md").read_text(encoding="utf-8") == "owner edit\n"


def test_scenario_d_install_requests_processed_in_order(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "parallel-d",
                           "full", ("builder",))
    rid1 = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "package.json", "change": {"dependencies": {"zod": "3.0.0"}},
    }, "owner")
    rid2 = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "package.json", "change": {"scripts": {"lint": "ruff"}},
    }, "owner")
    proc = _processor(workspace)
    assert proc.run_once("alpha") == 2
    assert workspace.ledger.request(rid1).status == "done"
    assert workspace.ledger.request(rid2).status == "done"

    # the single processor handles one request at a time: the second
    # request's acquire_project_lock only starts after the first's
    # install.recorded event, by event sequence.
    first_record_seq = _seq_for_request(workspace, "install.recorded", rid1)
    second_acquire_started_seq = _step_started_seq(workspace, rid2, "acquire_project_lock")
    assert first_record_seq < second_acquire_started_seq


def _seq_for_request(workspace: Workspace, kind: str, request_id: str) -> int:
    for seq, event in workspace.ledger.events("alpha", kind):
        if event.payload.get("request_id") == request_id:
            return seq
    raise AssertionError(f"no {kind} event for {request_id}")


def _step_started_seq(workspace: Workspace, request_id: str, step_name: str) -> int:
    for seq, event in workspace.ledger.events("alpha", "request.step"):
        payload = event.payload
        if (payload.get("request_id") == request_id
                and payload.get("step_name") == step_name
                and payload.get("status") == "started"):
            return seq
    raise AssertionError(f"no started event for {step_name} of {request_id}")


MERGE_STEP_NAMES = ("collect_copies", "check_conflicts", "apply_allowed_paths", "record")


@pytest.mark.parametrize("crash_step", MERGE_STEP_NAMES)
@pytest.mark.parametrize("boundary", ["crash_before", "crash_after"])
def test_scenario_e_merge_crash_at_each_boundary_recovers_to_same_result(
    workspace: Workspace, crash_step: str, boundary: str,
) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha",
                           f"merge-crash-{boundary}-{crash_step}", "full",
                           ("builder", "tester"))
    copy1 = copy_root(workspace.config, attempt_id, "builder")
    copy2 = copy_root(workspace.config, attempt_id, "tester")
    (copy1 / "src" / "app.py").write_text("print('one')\n", encoding="utf-8")
    (copy2 / "tests" / "bench" / "cases.json").write_text("[1]\n", encoding="utf-8")

    payload = {
        "copies": [
            {"role": "builder", "station": "build", "path": str(copy1)},
            {"role": "tester", "station": "register", "path": str(copy2)},
        ],
    }
    rid = submit(workspace.ledger, "alpha", attempt_id, "merge", payload, "owner")

    faults = Faults(**{boundary: {crash_step}})
    proc1 = _processor(workspace, holder="p1", faults=faults)
    with pytest.raises(CrashInjected):
        proc1.run_once("alpha")
    assert workspace.ledger.request(rid).status == "running"

    proc2 = _processor(workspace, holder="p2")
    n = proc2.run_once("alpha")
    assert n == 1
    assert workspace.ledger.request(rid).status == "done"
    # same result as an uncrashed run would have produced
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('one')\n"
    assert (workspace.alpha / "tests" / "bench" / "cases.json").read_text(encoding="utf-8") == "[1]\n"
    # merge.recorded was written exactly once, not duplicated by resume
    recorded = [e for _seq, e in workspace.ledger.events("alpha", "merge.recorded")
                if e.payload.get("request_id") == rid]
    assert len(recorded) == 1


def test_scenario_f_conflict_crashed_before_check_conflicts_still_fails_nothing_written(
    workspace: Workspace,
) -> None:
    cfg = _overlap_config(workspace.config)
    attempt_id = open_job(cfg, workspace.ledger, "alpha",
                           "merge-crash-conflict", "full", ("builder", "shadow_builder"))
    copy1 = copy_root(cfg, attempt_id, "builder")
    copy2 = copy_root(cfg, attempt_id, "shadow_builder")
    (copy1 / "src" / "app.py").write_text("print('one')\n", encoding="utf-8")
    (copy2 / "src" / "app.py").write_text("print('two')\n", encoding="utf-8")

    rid = submit(workspace.ledger, "alpha", attempt_id, "merge", {
        "copies": [
            {"role": "builder", "station": "build", "path": str(copy1)},
            {"role": "shadow_builder", "station": "shadow", "path": str(copy2)},
        ],
    }, "owner")

    proc1 = _processor(workspace, holder="p1", config=cfg,
                        faults=Faults(crash_before={"check_conflicts"}))
    with pytest.raises(CrashInjected):
        proc1.run_once("alpha")
    assert workspace.ledger.request(rid).status == "running"
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('alpha')\n"

    proc2 = _processor(workspace, holder="p2", config=cfg)
    n = proc2.run_once("alpha")
    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "merge_conflict"}
    assert (workspace.alpha / "src" / "app.py").read_text(encoding="utf-8") == "print('alpha')\n"
