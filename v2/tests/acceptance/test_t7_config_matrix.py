"""T7 -- the config-combination matrix: docs/flow-map.md ⑥'s own table
(release on/off x full/small/single-task mode x ui on/off x external-
target on/off x entry (new/change/fix/continue)), one test per named
row. The table's own "시험" column already says which rows have a test
today and which do not -- this file mirrors that column exactly rather
than re-deriving a full cross product (most of the 2x3x2x2x4 combinations
describe no real engine path at all, e.g. "ui" only matters for the
"new"/"change" full-mode entry; a cross product would mostly test
nothing).

Rows already covered by an existing test elsewhere are `pytest.mark.
skip`-ped with a pointer to that test (this file documents the matrix,
it does not duplicate engine tests this worktree does not own). Rows
the flow-map marks "없음"/"확인 못 함" get a real test here, or an
honest skip naming exactly what is missing to build one, if this pass's
time did not reach it."""
from __future__ import annotations

import pytest

from engine import full_orbit
from engine.faults import CrashInjected
from engine.ledger import Event
from tests.conftest import Workspace
from tests.harness import driver

# The table itself (flow-map.md ⑥), for the record -- row name -> whether
# this harness has a real test for it, and why not when it does not.
CONFIG_MATRIX_ROWS: dict[str, str] = {
    "new_or_change_default": "covered by tests/unit/test_full_orbit.py::"
        "test_two_task_plan_builds_both_tasks_in_order_and_finishes_done",
    "ui_screen_task": "covered by tests/unit/test_full_orbit.py::"
        "test_ui_plan_stops_for_design_approval_then_continue_finishes",
    "external_target_present": "covered by tests/unit/test_full_orbit.py::"
        "test_probe_runs_and_logs_reach_the_planner_as_실측_결과",
    "release_configured": "covered by tests/unit/test_full_orbit.py::"
        "test_release_configured_project_makes_one_publish_request_for_the_whole_job",
    "gb2_fix_full_job": "covered by tests/unit/test_gb2.py:912",
    "cursor_resume_mid_fix": "no existing test (flow-map ⑥ 없음) -- see "
        "test_t6_crash_matrix.py::"
        "test_release_configured_fix_recovery_makes_exactly_one_publish_request "
        "(same gap, release x fix-recovery) and feedback-1 "
        "(same gap, no release)",
    "gb2_run_single_task_plus_release": "not built in this pass -- needs a "
        "single-task orbit.run_job/continue_job driver (tools/gb2 `run` "
        "entry), documented in full_orbit.py:78-85 as a known limitation; "
        "this harness only drives engine.full_orbit's full-job entry "
        "points (run_full_job/continue_full_job)",
    "gb2_fix_single_task_plus_release": "not built in this pass -- same "
        "missing single-task driver as gb2_run_single_task_plus_release",
    "fast_mode_three_fallbacks": "covered by tests/unit/test_small_orbit.py "
        "(maybe_full_fallback)",
    "fast_mode_plus_release": "covered by tests/unit/test_small_orbit.py",
    "missing_required_facts": "covered by tests/unit/test_full_orbit.py "
        "(check_facts failure path)",
    "design_wait_and_crash_simultaneously": "built in this file: "
        "test_design_approval_wait_survives_a_crash_right_after_approval",
}

_SKIPPED_ROWS = {
    name: reason for name, reason in CONFIG_MATRIX_ROWS.items()
    if name != "design_wait_and_crash_simultaneously"
}


@pytest.mark.parametrize("row", sorted(_SKIPPED_ROWS))
def test_config_matrix_row_documented(row: str) -> None:
    pytest.skip(_SKIPPED_ROWS[row])


def test_design_approval_wait_survives_a_crash_right_after_approval(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """flow-map ⑥ '시안 대기 + 중단 동시': a ui-plan job stopped
    needs_owner/design_approval, the owner approves, and the very next
    continue_full_job call (which must run the build task that was
    always waiting behind the design approval) crashes partway through.
    A second continue_full_job must finish the job without re-asking for
    design approval and without running the build station twice."""
    job_name = "design-wait-crash-job"
    ws = driver.full_orbit_workspace(workspace)
    plan_json = driver.plan_json(ui=True, tasks=[
        {"id": "t1", "title": "화면 연결", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    stub = driver.RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md":
                   "- 요청 확인: 통과 (출처: request)\n\n## 외부 대상\n(없음)\n"}],
        "plan": [driver.plan_queue_entry(job_name, plan_json)],
        "design": [{f".gongbang/{job_name}/design/design.md": "# 시안\n"}],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    state = driver.install_fake_acceptance_and_handoff(
        monkeypatch, [driver.FakeAcceptanceResult(passed=True)],
    )
    flt = __import__("engine.faults", fromlist=["Faults"]).Faults()
    factory = driver.make_processor_factory(stub, flt)

    first = full_orbit.run_full_job(
        ws.config, ws.ledger, driver.fakes(), "alpha", job_name, "화면 하나 만들어줘",
        processor_factory=factory,
    )
    assert first.status == "needs_owner"
    assert first.reason == "design_approval"

    ws.ledger.append(Event(
        project="alpha", kind="design.approved", actor_kind="owner", actor_id="owner",
        attempt_id=None, payload={"job": job_name},
    ))

    # The build task's own run_station is the very next effect due to
    # run after approval -- crash right before it journals 'started'
    # (crash_before "record_station", the stub's one step) to land the
    # fault squarely inside the window this row is about.
    flt.crash_before.add("record_station")
    with pytest.raises(CrashInjected):
        full_orbit.continue_full_job(
            ws.config, ws.ledger, driver.fakes(), "alpha", job_name, processor_factory=factory,
        )
    assert "record_station" not in flt.crash_before

    done = full_orbit.continue_full_job(
        ws.config, ws.ledger, driver.fakes(), "alpha", job_name, processor_factory=factory,
    )

    assert done.status == "done"
    assert (ws.alpha / "src/greeter/extra1.py").exists()
    # the build station ran exactly once, not re-asked for design
    # approval again and not re-run twice by the crash+resume.
    assert [c for c in stub.calls if c[0] == "build"] == [("build", "builder")]
    assert state.acceptance_calls == 1
    assert len(state.handoff_calls) == 1
