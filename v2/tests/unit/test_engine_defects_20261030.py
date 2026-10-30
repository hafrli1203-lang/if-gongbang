"""Four engine defects observed on a live job,
WORK_QUEUE 「공방 v2 엔진 결함」), each pinned by one test:
  1. watch misjudged a live driver between requests  -> driver_session marker
  2. plan review cap never ended (past-cap per attempt) -> foreman stop
  3. a never-passed plan could not be approved by hand -> allow_first_review
  4. .tmp- copies/archive dirs survived a killed driver -> sweep at open_job
"""
from __future__ import annotations

import os
from pathlib import Path

import tests.unit.test_plan as _tp
from engine.application import auto_pilot, watch
from engine.application.driver_session import driver_lock_key, driver_session
from engine.application.job_state import STAGE_PLAN_REVIEW, JobPosition
from engine.application.steps.open_job import open_job
from engine.domain import ids
from engine.domain.plan import record_owner_plan_amend
from engine.domain.records import Event
from engine.ledger import Ledger
from tests.conftest import Workspace


# ---- 1. watch sees a live driver through the driver marker ----------

def test_driver_session_makes_watch_say_running_without_a_processor_lock(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "d1", "full", ("builder",))
    assert attempt_id
    # Between requests nothing holds the processor lock: before the fix
    # this read as "stalled" (position recorded, no request yet).
    assert watch.judge_app(workspace.ledger, "alpha").verdict != "running"
    with driver_session(workspace.ledger, "alpha") as taken:
        assert taken is True
        assert workspace.ledger.lock_pid(driver_lock_key("alpha")) == os.getpid()
        assert watch.judge_app(workspace.ledger, "alpha").verdict == "running"
    # Released on exit: the marker never outlives the command.
    assert workspace.ledger.lock_pid(driver_lock_key("alpha")) is None
    assert watch.judge_app(workspace.ledger, "alpha").verdict != "running"


# ---- 4. stale .tmp- staging dirs are swept at the next open ----------

def test_open_job_sweeps_tmp_copies_and_archive_dirs_of_this_project_only(workspace: Workspace) -> None:
    copies = workspace.config.home / "copies"
    mine = copies / ".tmp-J-alpha-01KILLEDMIDCOPY00000000-1"
    other = copies / ".tmp-J-beta-01SOMEONEELSE000000000-1"
    sibling = copies / ".tmp-J-alpha-api-01SIBLINGPROJECT000000-1"   # project "alpha-api", not ours
    for d in (mine, other, sibling):
        d.mkdir(parents=True)
        (d / "half.txt").write_text("half-extracted tar", encoding="utf-8")
    archive_tmp = workspace.config.home / "archive" / "alpha" / "J-alpha-01OLD-1" / ".tmp-abc123"
    archive_tmp.mkdir(parents=True)
    archive_done = workspace.config.home / "archive" / "alpha" / "J-alpha-01OLD-1" / "abc123"
    archive_done.mkdir(parents=True)
    # A symlinked attempt dir pointing outside the archive must not have its target swept.
    outside = workspace.root / "outside"
    (outside / ".tmp-victim").mkdir(parents=True)
    (workspace.config.home / "archive" / "alpha" / "J-alpha-01LINK-1").symlink_to(outside)

    open_job(workspace.config, workspace.ledger, "alpha", "sweep", "full", ("builder",))

    assert not mine.exists()
    assert other.exists()            # another project's staging dir is not ours to touch
    assert sibling.exists()          # a project whose name merely starts with ours is not ours either
    assert not archive_tmp.exists()
    assert archive_done.exists()     # a committed archive is kept
    assert (outside / ".tmp-victim").exists()   # symlink not followed


# ---- 3. the foreman can approve a plan the reviewer never passed -----

def test_owner_amend_of_a_never_reviewed_plan_is_refused_unless_first_review_allowed(tmp_path: Path) -> None:
    _tp._write_plan(tmp_path, "job", _tp._valid_plan_dict())
    ledger = Ledger.create(tmp_path / "ledger.sqlite3")
    assert record_owner_plan_amend(ledger, "alpha", "a1", tmp_path, "job", "반장 수정") == ["plan_never_reviewed"]
    assert record_owner_plan_amend(ledger, "alpha", "a1", tmp_path, "job", "반장 수정",
                                   allow_first_review=True) == []
    amend = [e for _seq, e in ledger.events("alpha", "plan.owner_amended")]
    reviewed = [e for _seq, e in ledger.events("alpha", "plan.reviewed")]
    assert len(amend) == 1 and amend[0].payload["first_review"] is True
    assert len(reviewed) == 1 and reviewed[0].actor_kind == "owner"


# ---- 2. the plan review cap stops for the foreman, no past-cap loop ---

def test_plan_review_cap_stops_for_the_foreman_with_a_note_and_event(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "capped", "full", ("builder",))
    finding = {"severity": "P1", "file": ".gongbang/capped/plan.json", "line": 3, "note": "묶음별 배포 없음"}
    workspace.ledger.append(Event(
        project="alpha", kind="evidence.recorded", actor_kind="processor", actor_id="reviewer",
        attempt_id=attempt_id, payload={"kind": "review", "fingerprint": "fp-1", "artifact_hash": None, "payload": {"passed": False, "findings": [finding]}},
    ))
    pos = JobPosition(job="capped", stage=STAGE_PLAN_REVIEW, attempt_id=attempt_id,
                      stop_reason=f"{auto_pilot.FIX_ROUNDS_EXHAUSTED_PREFIX}2")
    outcome = auto_pilot._advance(workspace.config, workspace.ledger, None, "alpha", "capped", pos,
                                  pos.stop_reason or "", set(), None, None)
    assert outcome is not None and outcome.status == "failed"
    assert outcome.reason.startswith("plan_review_capped:")
    capped = [e for _seq, e in workspace.ledger.events("alpha", "plan.review_capped")]
    assert len(capped) == 1 and capped[0].payload["next"] == "foreman"
    note = workspace.alpha / ".gongbang" / "capped" / "foreman-handoff.md"
    assert note.is_file()
    assert "묶음별 배포 없음" in note.read_text(encoding="utf-8")
    assert "approve-plan-edit" in note.read_text(encoding="utf-8")
