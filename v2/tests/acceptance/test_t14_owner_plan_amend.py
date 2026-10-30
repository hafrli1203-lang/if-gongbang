"""T14 -- 결함 47, appX job): a job's plan was already
reviewed once, then the OWNER edited a tests/scenes/ file after that
(e.g. appX r21's own fake-DB mock, widened to offer `withTx` so the
builder could keep a single transaction instead of branching on
whether the fake DB has one). check_resumed_plan's own digest check
(engine.domain.plan_review.plan_digest -- plan.json's bytes PLUS every
tests/scenes/ file any scene's cmd references) then catches the edit
exactly as designed and reports "plan_changed_since_review" on every
resume point (engine.application.pipeline_resume.resumed_plan_problems),
stopping the job cold -- correctly, since nothing yet tells the engine
this particular edit was OWNER-approved rather than tampering.

`gb2 approve-plan-edit <app> --reason "..."` (tools/gb2lib/runner.
cmd_approve_plan_edit) is the owner-only unstick: it calls
engine.domain.plan_review.record_owner_plan_amend, which appends a
`plan.owner_amended` event (job/reason/changed -- the exact scene file
paths whose digest moved, each old+new sha256) immediately followed by
a fresh `plan.reviewed` event (actor_kind="owner") at the plan's
CURRENT digest -- so check_resumed_plan reads [] again right after.
plan.json itself is never edited by this path; only plan.reviewed's
own ledger record moves forward to match a scene file the owner
already changed on disk.

This module exercises engine.domain.plan_review directly (Ledger +
plan.json/tests/scenes/ fixtures), reusing tests.unit.test_plan's own
`_write_plan`/`_valid_plan_dict`/`_write_scene_script` helpers via a
whole-module import (same convention test_t9d_also_own.py/
test_t9f_plan_check_resume.py already use, so tools/arch_check.py's
own check_private_imports never flags a `from ... import _private`).
No live AI, no gb2 CLI process: the actual `gb2 approve-plan-edit`
wiring (tools/gb2lib/runner.cmd_approve_plan_edit) is exercised
separately, for real, against the live appX job (see the job's own
completion report, not this test suite)."""
from __future__ import annotations

from pathlib import Path

import tests.unit.test_plan as _tp
from engine.domain.plan import check_resumed_plan, record_owner_plan_amend, record_plan_reviewed
from engine.ledger import Ledger


def _reviewed_ledger_with_scene(tmp_path: Path, job: str = "job") -> Ledger:
    """A job whose plan.json + tests/scenes/base.py were reviewed once
    (record_plan_reviewed), ready for an owner edit to the scene file
    on top of."""
    _tp._write_plan(tmp_path, job, _tp._valid_plan_dict())
    ledger = Ledger.create(tmp_path / "ledger.sqlite3")
    record_plan_reviewed(ledger, "alpha", "attempt-1", tmp_path, job)
    return ledger


def _edit_scene_file(tmp_path: Path, new_text: str) -> None:
    (tmp_path / "tests" / "scenes" / "base.py").write_text(new_text, encoding="utf-8")


# ---- (a) baseline: an owner-edited scene file, with no amend yet, ----
# ---- still trips check_resumed_plan's existing gate -------------------

def test_scene_edit_after_review_still_trips_plan_changed_since_review(
    tmp_path: Path,
) -> None:
    ledger = _reviewed_ledger_with_scene(tmp_path)
    _edit_scene_file(tmp_path, "pass  # owner widened the fake DB mock\n")

    problems = check_resumed_plan(ledger, "alpha", "job", tmp_path)
    assert "plan_changed_since_review" in problems


# ---- (b)+(c) approve-plan-edit clears the gate and records a full ----
# ---- audit trail (job, reason, changed scene paths' old/new digest) --

def test_record_owner_plan_amend_clears_the_gate_and_records_the_audit_trail(
    tmp_path: Path,
) -> None:
    from engine.domain.plan_review import scene_file_digests
    from engine.domain.plan_model import plan_path

    ledger = _reviewed_ledger_with_scene(tmp_path)
    scene_path = tmp_path / "tests" / "scenes" / "base.py"
    before_digest = scene_file_digests(plan_path(tmp_path, "job"))["tests/scenes/base.py"]
    _edit_scene_file(tmp_path, "pass  # owner widened the fake DB mock\n")
    after_digest = scene_file_digests(plan_path(tmp_path, "job"))["tests/scenes/base.py"]
    assert before_digest != after_digest

    problems = record_owner_plan_amend(
        ledger, "alpha", "attempt-1", tmp_path, "job",
        "운영자 결정: r21 장면 가짜 DB에 withTx 통로 추가",
    )
    assert problems == []

    # (c): the gate clears immediately.
    assert check_resumed_plan(ledger, "alpha", "job", tmp_path) == []

    # (c): the audit event carries job/reason/changed.
    amend_events = [e for _seq, e in ledger.events("alpha", "plan.owner_amended")]
    assert len(amend_events) == 1
    amend = amend_events[0]
    assert amend.actor_kind == "owner"
    assert amend.payload["job"] == "job"
    assert amend.payload["reason"] == "운영자 결정: r21 장면 가짜 DB에 withTx 통로 추가"
    assert amend.payload["changed"] == {
        "tests/scenes/base.py": {"old": before_digest, "new": after_digest},
    }

    # A fresh plan.reviewed event (actor_kind="owner") was appended too.
    reviewed_events = [e for _seq, e in ledger.events("alpha", "plan.reviewed")]
    assert len(reviewed_events) == 2
    assert reviewed_events[-1].actor_kind == "owner"

    assert scene_path.read_text(encoding="utf-8") == "pass  # owner widened the fake DB mock\n"


# ---- (d) nothing changed since review -> refused, nothing recorded --

def test_record_owner_plan_amend_refuses_when_nothing_changed(tmp_path: Path) -> None:
    ledger = _reviewed_ledger_with_scene(tmp_path)

    problems = record_owner_plan_amend(
        ledger, "alpha", "attempt-1", tmp_path, "job", "아무것도 안 바꿨는데 재승인 시도",
    )
    assert problems == ["no_change_since_review"]
    assert list(ledger.events("alpha", "plan.owner_amended")) == []
    # The ORIGINAL review event is still the only plan.reviewed event.
    assert len([e for _seq, e in ledger.events("alpha", "plan.reviewed")]) == 1


# ---- (e) validate_plan failure -> refused, nothing recorded -----------

def test_record_owner_plan_amend_refuses_when_plan_is_now_invalid(tmp_path: Path) -> None:
    ledger = _reviewed_ledger_with_scene(tmp_path)
    _edit_scene_file(tmp_path, "pass  # owner widened the fake DB mock\n")
    # Break plan.json itself: a task that depends on itself is still
    # schema-valid JSON but validate_plan rejects it outright (a
    # dependency cycle), independent of the scene-file edit above.
    broken = _tp._valid_plan_dict(tasks=[
        {"id": "t1", "title": "고치기", "detail": "...",
         "owns": ["src/", "tests/"], "depends_on": ["t1"]},
    ])
    _tp._write_plan(tmp_path, "job", broken)

    problems = record_owner_plan_amend(
        ledger, "alpha", "attempt-1", tmp_path, "job", "무효한 계획에 재승인 시도",
    )
    assert problems != []
    assert list(ledger.events("alpha", "plan.owner_amended")) == []


# ---- (f) blank reason -> refused, nothing recorded ---------------------

def test_record_owner_plan_amend_refuses_blank_reason(tmp_path: Path) -> None:
    ledger = _reviewed_ledger_with_scene(tmp_path)
    _edit_scene_file(tmp_path, "pass  # owner widened the fake DB mock\n")

    problems = record_owner_plan_amend(ledger, "alpha", "attempt-1", tmp_path, "job", "   ")
    assert problems == ["reason_required"]
    assert list(ledger.events("alpha", "plan.owner_amended")) == []


# ---- (g) expected_current_digest pinning --------
# ---- -- the signed/pinned digest is what gets STORED, never a ---------
# ---- further, independent re-read of the file ------------------------

def test_record_owner_plan_amend_stores_the_pinned_digest_verbatim(tmp_path: Path) -> None:
    """tools/gb2lib/approve_ops.py's --office-token path pins a plan_
    edit_target_version ONCE and passes it both to verify_office_token
    (expected_target_version) and here (expected_current_digest). The
    resulting plan.reviewed event's own "digest" field must be that
    EXACT pinned value -- record_plan_reviewed must never recompute it
    a further time from disk."""
    from engine.domain.plan_review import plan_digest
    from engine.domain.plan_model import plan_path

    ledger = _reviewed_ledger_with_scene(tmp_path)
    _edit_scene_file(tmp_path, "pass  # owner widened the fake DB mock\n")
    current_digest = plan_digest(plan_path(tmp_path, "job"))

    problems = record_owner_plan_amend(
        ledger, "alpha", "attempt-1", tmp_path, "job", "office 승인 경로 테스트",
        expected_current_digest=current_digest,
    )
    assert problems == []

    reviewed_events = [e for _s, e in ledger.events("alpha", "plan.reviewed")
                       if e.payload.get("job") == "job"]
    assert reviewed_events[-1].payload["digest"] == current_digest


def test_record_owner_plan_amend_refuses_when_pinned_digest_is_stale(tmp_path: Path) -> None:
    """If the plan moved AFTER the caller pinned expected_current_digest
    (e.g. a further edit landed between office-token verification and
    this call), record_owner_plan_amend must refuse rather than record
    an approval for content the signed digest never actually covered."""
    ledger = _reviewed_ledger_with_scene(tmp_path)
    _edit_scene_file(tmp_path, "pass  # owner widened the fake DB mock\n")

    problems = record_owner_plan_amend(
        ledger, "alpha", "attempt-1", tmp_path, "job", "stale pin 테스트",
        expected_current_digest="not-the-real-digest",
    )
    assert problems == ["plan_changed_after_verification"]
    assert list(ledger.events("alpha", "plan.owner_amended")) == []
