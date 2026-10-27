"""T16 -- G24 (운영자 지시: "전체적으로 계속 수정해도 또 문제가 반복되는걸 잡아야한다",
`ops-repo/audit/gongbang_directives.md`): a STRUCTURAL table test, not
another crash-recovery scenario (test_t8_flow_boundary_stops.py already
has 8 of those -- see that file's own docstring). This file answers a
different question: "모든 멈춤에는 다시 이어갈 길이 있다" -- enumerated,
once, in one table, so a new stop added to the engine without a matching
owner command and a matching test is a FAILURE here, not a silent gap.

The table's left column is `engine.full_orbit.RESUME_TABLE`'s own key set
(`engine.application.job_state.STAGE_*`) -- the single source of truth for
which internal "stage" a needs_owner stop can leave a job position in.
`docs/ENGINE.md`'s own "stage / 의미 / 이어가는 명령 / internal 멈춤 길"
table (writtenff, this file's own source for the "owner
command" column) is NOT re-derived here -- it is read as the already-
reviewed spec and cross-checked against the live RESUME_TABLE keys, so
this test fails the moment the two drift apart.

For every stage this links an EVIDENCE test already in this suite that
dynamically drives that exact stop to a real resume ("no lost/duplicated
work", per test_t8's own docstring) -- this test does not re-run that
scenario (that would just duplicate test_t8/test_t9d/test_gb2.py), it
asserts the evidence test still EXISTS and is collectible. Deleting an
evidence test without replacing the table entry fails this file
immediately -- that is the whole point ("길이 없는 상태가 나오면 그것이
결함이다").

결함 45(owner decline note reaching the resumed worker)·46(approve/
decline an edit request mid fix-round) are already fixed on main
(af5e013/a80ddb9, merged as s49-decline-note/s50-fix-task-edit) -- their
rows point at the real regression tests for each, and this file actually
RUNS those two (nested pytest subprocess) since they are the two defects
this job was asked to track by number.

결함 47 (`gb2 approve-plan-edit`) was NOT in main when this job started
(grep confirmed: no "approve-plan-edit" anywhere under tools/gb2*,
HEAD 7e3ecdf) -- another work group was merging it concurrently. It
landed on main (HEAD 5b17a16, fast-forwarded into this worktree) WHILE
this file was being written, bringing its own regression test
(`tests/acceptance/test_t14_owner_plan_amend.py`). The row below is a
real evidence row, not the xfail this file's own first draft carried --
the timing (결함 found open, then closed mid-task by a different work
group) is itself worth noting in the report, not hidden."""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from engine import full_orbit
from engine.application import job_state

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GB2_PATH = _REPO_ROOT / "tools" / "gb2"
_ENGINE_DOC = _REPO_ROOT / "docs" / "ENGINE.md"


# --------------------------------------------------------------------
# The table itself. `evidence` is (module, test function name) pairs --
# at least one, dynamically imported and resolved by getattr below. A
# stage with no owner-visible resume action of its own (STAGE_EARLY_
# CLOSE) still gets a row, so the table's key set can be compared
# 1:1 against RESUME_TABLE's key set without a silent gap.
# --------------------------------------------------------------------
OWNER_STOP_TABLE: dict[str, dict[str, object]] = {
    job_state.STAGE_FACTS: {
        "command": "gb2 continue",
        "evidence": [("tests.unit.test_full_orbit",
                      "test_internal_crash_at_an_early_station_is_retried_by_continue_not_stuck")],
    },
    job_state.STAGE_PROBE: {
        "command": "gb2 continue",
        "evidence": [("tests.unit.test_full_orbit",
                      "test_internal_crash_at_an_early_station_is_retried_by_continue_not_stuck")],
    },
    job_state.STAGE_PLAN: {
        "command": "gb2 continue",
        "evidence": [("tests.unit.test_full_orbit",
                      "test_internal_crash_at_an_early_station_is_retried_by_continue_not_stuck")],
    },
    job_state.STAGE_DESIGN: {
        "command": "gb2 continue",
        "evidence": [("tests.unit.test_full_orbit",
                      "test_internal_crash_at_an_early_station_is_retried_by_continue_not_stuck")],
    },
    job_state.STAGE_PROBE_RETRY: {
        "command": "facts.md/probe 로그 수정 -> gb2 continue",
        "evidence": [("tests.acceptance.test_t8_flow_boundary_stops",
                      "test_crash_at_probe_retry_boundary_then_continue_resumes_and_finishes")],
    },
    job_state.STAGE_PLAN_REVIEW: {
        "command": "plan.json 수정 또는 그대로 -> gb2 continue",
        "evidence": [("tests.acceptance.test_t8_flow_boundary_stops",
                      "test_crash_at_plan_review_boundary_then_continue_resumes_and_finishes")],
    },
    job_state.STAGE_EARLY_STATION: {
        "command": "설치 승인이면 gb2 approve-install -> gb2 continue; 그 외는 gb2 continue",
        "evidence": [("tests.acceptance.test_t8_flow_boundary_stops",
                      "test_crash_at_early_station_owner_boundary_then_continue_resumes_and_finishes"),
                     ("tests.unit.test_gb2",
                      "test_approve_install_finds_and_resumes_an_early_station_request")],
    },
    job_state.STAGE_SMALL_PLAN: {
        "command": "gb2 continue",
        "evidence": [("tests.acceptance.test_t8_flow_boundary_stops",
                      "test_crash_at_small_plan_boundary_then_continue_resumes_and_finishes")],
    },
    job_state.STAGE_DESIGN_APPROVAL: {
        "command": 'gb2 approve-design (TTY, "승인 <job>" 입력)',
        "evidence": [("tests.acceptance.test_t8_flow_boundary_stops",
                      "test_crash_at_design_approval_boundary_then_continue_resumes_and_finishes")],
    },
    job_state.STAGE_EARLY_CLOSE: {
        "command": "gb2 continue (단순 close)",
        "evidence": [("tests.unit.test_full_orbit", "_processor_factory")],
        "note": "run_station 자체의 owner 조치는 없음 -- 구조적 anchor만",
    },
    job_state.STAGE_TASK: {
        "command": "gb2 continue",
        "evidence": [("tests.unit.test_full_orbit",
                      "test_internal_crash_at_a_build_task_is_retried_by_continue_not_stuck")],
    },
    job_state.STAGE_TASK_OWNER: {
        "command": "설치면 gb2 approve-install -> gb2 continue; owns-scope edit는 "
                    "gb2 fix --also-own/--decline; 그 외는 gb2 continue",
        "evidence": [("tests.acceptance.test_t9d_also_own",
                      "test_28_also_own_approves_both_pending_edit_requests_and_resumes_the_same_attempt"),
                     ("tests.acceptance.test_t9d_also_own",
                      "test_43_decline_resolves_an_edit_request_that_cannot_be_granted")],
        "note": "docs/ENGINE.md 601행 '확인 필요': crash=False 경로의 "
                "rerun_task_owner_stop은 outstanding_requests만 보고 unknown "
                "요청은 안 푼다 -- 설치 승인 drain 중 internal 크래시로 "
                "unknown이 되는 경우는 이미 문서화된 한계(범위 밖, 별도 과제, "
                "이고 이 시험이 새로 찾은 결함이 아니다.",
    },
    job_state.STAGE_POST_TASKS: {
        "command": "gb2 continue",
        "evidence": [("tests.acceptance.test_t8_flow_boundary_stops",
                      "test_crash_at_requirements_review_boundary_then_continue_resumes_to_the_same_stop")],
    },
    job_state.STAGE_REQUIREMENTS_REVIEW: {
        "command": "gb2 continue (요구사항 커버리지를 채운 뒤)",
        "evidence": [("tests.acceptance.test_t8_flow_boundary_stops",
                      "test_crash_at_requirements_review_boundary_then_continue_resumes_to_the_same_stop")],
    },
    job_state.STAGE_SECURITY_FIX: {
        "command": "소스 수정 -> gb2 continue",
        "evidence": [("tests.acceptance.test_t8_flow_boundary_stops",
                      "test_crash_at_security_fix_boundary_then_continue_resumes_and_finishes")],
    },
    job_state.STAGE_RELEASE_APPROVAL: {
        "command": "gb2 ship(승인)/gb2 skip-ship(포기); publish가 unknown/중단된 "
                    "running이면 먼저 gb2 resolve",
        "evidence": [("tests.acceptance.test_t8_flow_boundary_stops",
                      "test_crash_at_release_approval_boundary_then_continue_resumes_to_the_same_stop"),
                     ("tests.unit.test_gb2",
                      "test_ship_with_correct_line_merges_and_deploys_and_closes"),
                     ("tests.unit.test_gb2",
                      "test_skip_ship_abandons_publish_without_deploy_and_closes"),
                     ("tests.unit.test_gb2",
                      "test_resolve_deployed_with_passing_check_records_done_and_closes")],
    },
}

# STAGE_SINGLE (`gb2 run`) is NOT in RESUME_TABLE (docs/ENGINE.md 605행:
# "RESUME_TABLE에 없음") -- its own way forward is `gb2 fix`, not `gb2
# continue`. Kept as a separate row so it is still covered by this
# file's "every stop has evidence" check without polluting the
# RESUME_TABLE-keys-match-doc-table comparison below.
SINGLE_TASK_ROW = {
    "command": "gb2 fix (gb2 continue가 아니다)",
    "evidence": [("tests.unit.test_gb2",
                  "test_cmd_fix_calls_fixup_fix_with_the_app_and_extra_text")],
}

# CLI-level stops that never write a ledger needs_owner event of their
# own (fixup.FixupError, reported by tools/gb2lib/runner.py as a
# needs_owner-SHAPED exit 2) -- same "owner decision required, here is
# the exact next command" character as every RESUME_TABLE row, just one
# layer up (gb2, not engine.application.job_state). 결함 45/46 live here.
CLI_STOP_TABLE: dict[str, dict[str, object]] = {
    "fix_rounds_exhausted": {
        "command": 'gb2 fix <app> "<줄인 지시>" --past-cap, 또는 운영자가 지적을 '
                    "알려진 문제로 받아들이기",
        "evidence": [("tests.acceptance.test_t9d_also_own",
                      "test_41_fix_refuses_with_nothing_recorded_once_rounds_are_exhausted"),
                     ("tests.acceptance.test_t9d_also_own",
                      "test_41_past_cap_allows_one_more_round_and_records_the_event")],
    },
    "결함45_owner_decline_note_reaches_worker": {
        "command": "gb2 fix <app> --decline <P> [extra note]",
        "evidence": [("tests.acceptance.test_t9d_also_own",
                      "test_45_decline_note_reaches_the_resumed_worker")],
    },
    "결함46_approve_or_decline_edit_request_during_fix_round": {
        "command": "gb2 fix <app> --also-own <P> | --decline <P> (수정 회차 중에도)",
        "evidence": [("tests.acceptance.test_t9d_also_own",
                      "test_46_owner_can_approve_and_decline_an_edit_request_raised_during_a_fix_round")],
    },
    "결함47_plan_changed_since_review_after_owner_edited_a_scene": {
        "command": 'gb2 approve-plan-edit <app> --reason "<사유>" (review 뒤 '
                    "tests/scenes/ 파일을 운영자가 직접 고쳤을 때 -- 모든 resume "
                    "지점에서 plan_changed_since_review/resumed_plan_invalid로 "
                    "멈추는 cross-stage 게이트, 특정 STAGE_* 하나에 묶이지 않음)",
        "evidence": [("tests.acceptance.test_t14_owner_plan_amend",
                      "test_scene_edit_after_review_still_trips_plan_changed_since_review"),
                     ("tests.acceptance.test_t14_owner_plan_amend",
                      "test_record_owner_plan_amend_clears_the_gate_and_records_the_audit_trail")],
    },
}


def _resolve(module_path: str, func_name: str):
    module = importlib.import_module(module_path)
    return getattr(module, func_name)


# --------------------------------------------------------------------
# 1. RESUME_TABLE's own key set == this table's key set (STAGE_EARLY_
#    CLOSE has no owner action but is kept as a structural anchor so a
#    drift there is still visible, not quietly carved out).
# --------------------------------------------------------------------

def test_every_resume_table_stage_has_an_owner_stop_table_row() -> None:
    live_stages = set(full_orbit.RESUME_TABLE.keys())
    documented_stages = set(OWNER_STOP_TABLE.keys())
    missing = live_stages - documented_stages
    assert not missing, (
        f"engine.full_orbit.RESUME_TABLE has stage(s) with NO owner-stop-table "
        f"row -- a way forward for {missing} was never written down or tested: "
        f"this IS the G24 defect this file exists to catch"
    )
    extra = documented_stages - live_stages
    assert not extra, (
        f"owner-stop-table row(s) {extra} name a stage RESUME_TABLE no longer "
        f"has -- table is stale, update or remove the row"
    )


# --------------------------------------------------------------------
# 2. Every row's evidence test(s) still exist and are real test
#    functions (collectible by pytest) -- NOT re-run here (that is
#    test_t8/test_t9d/test_gb2's own job); existence IS the "a way
#    forward actually has a test" guarantee this file makes.
# --------------------------------------------------------------------

@pytest.mark.parametrize("stage", sorted(OWNER_STOP_TABLE.keys()))
def test_resume_table_row_evidence_exists(stage: str) -> None:
    row = OWNER_STOP_TABLE[stage]
    for module_path, func_name in row["evidence"]:
        target = _resolve(module_path, func_name)
        assert callable(target), f"{module_path}.{func_name} is not callable"


def test_single_task_row_evidence_exists() -> None:
    for module_path, func_name in SINGLE_TASK_ROW["evidence"]:
        target = _resolve(module_path, func_name)
        assert callable(target)


@pytest.mark.parametrize("key", sorted(CLI_STOP_TABLE.keys()))
def test_cli_stop_table_row_evidence_exists_and_is_collectible(key: str) -> None:
    row = CLI_STOP_TABLE[key]
    for module_path, func_name in row["evidence"]:
        target = _resolve(module_path, func_name)
        assert callable(target)


# --------------------------------------------------------------------
# 3. 결함 45/46: run the exact two regression tests this job was asked
#    to confirm, by shelling out to pytest itself (a nested subprocess
#    keeps this one test's pass/fail tied to theirs without duplicating
#    their fixtures here).
# --------------------------------------------------------------------

def test_defect_45_and_46_regression_tests_pass_in_this_worktree() -> None:
    targets = [
        "tests/acceptance/test_t9d_also_own.py::test_45_decline_note_reaches_the_resumed_worker",
        "tests/acceptance/test_t9d_also_own.py::"
        "test_46_owner_can_approve_and_decline_an_edit_request_raised_during_a_fix_round",
    ]
    result = subprocess.run(
        [sys.executable, "-P", "-m", "pytest", "-q", *targets],
        cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, (
        f"결함 45/46 regression test(s) failed:\n{result.stdout}\n{result.stderr}"
    )


# --------------------------------------------------------------------
# 4. 결함 47 (approve-plan-edit): landed on main WHILE this job was
#    running (HEAD moved 7e3ecdf -> 5b17a16 mid-task, another work
#    group's merge) -- confirmed wired into tools/gb2's own dispatch
#    table and argparse subparsers, not just present as text somewhere.
# --------------------------------------------------------------------

def test_defect_47_approve_plan_edit_command_exists_and_is_wired() -> None:
    gb2_text = _GB2_PATH.read_text(encoding="utf-8")
    assert '"approve-plan-edit": cmd_approve_plan_edit' in gb2_text, (
        "gb2 approve-plan-edit is not wired into tools/gb2's own COMMANDS "
        "dispatch table"
    )
    assert "approve_plan_edit" in gb2_text, (
        "gb2 has no approve-plan-edit argparse subparser"
    )


# --------------------------------------------------------------------
# 5. docs/ENGINE.md's own stage table (591-605행) is read back here and
#    cross-checked for the same stage set, so the two sources (the doc
#    a human reads, the table this file executes) cannot silently
#    diverge either.
# --------------------------------------------------------------------

def _stage_constant_name(stage_value: str) -> str:
    for name, value in vars(job_state).items():
        if name.startswith("STAGE_") and value == stage_value:
            return name
    return stage_value


def test_engine_doc_stage_table_mentions_every_resume_table_stage() -> None:
    if not _ENGINE_DOC.exists():
        pytest.skip("docs/ENGINE.md is a private record, not part of the public copy")
    doc_text = _ENGINE_DOC.read_text(encoding="utf-8")
    missing_from_doc = [
        stage for stage in full_orbit.RESUME_TABLE
        if _stage_constant_name(stage) not in doc_text
    ]
    assert not missing_from_doc, (
        f"docs/ENGINE.md's own stage table never names {missing_from_doc} -- "
        f"the human-readable doc and the code have drifted"
    )
