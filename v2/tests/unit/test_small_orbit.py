"""Scripted tests for engine.small_orbit: the fast path for a request
small enough to skip the probe station -- one combined facts+plan
worker call (station_prompts.small_prompt), then the SAME task-
attempt/acceptance/security/release machinery engine.full_orbit's own
tests already exercise.

No live AI: run_station/review are stubbed at the Processor level, the
same way tests/unit/test_full_orbit.py already does for engine.
full_orbit itself -- this module reuses that file's own fixtures/
fakes (workspace setup, _RunStationStub, the review-always-passes
stub, the fake engine.acceptance/engine.handoff/engine.app_security
modules) rather than duplicating them."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from engine import full_orbit, position, small_orbit
from engine.ledger import Event
from engine.processor import Processor, StepContext, StepDef, StepFailed
from engine.steps.open_job import copy_root, dirty_paths
from engine.steps.open_job import open_job as full_orbit_open_job
from tests.conftest import Workspace
from tests.unit.test_full_orbit import (
    _FakeAcceptanceResult,
    _FakeSceneResult,
    _RunStationStub,
    _fakes,
    _full_orbit_workspace,
    _install_fake_acceptance_and_handoff,
    _processor_factory,
    _record_install,
    _review_always_passes_steps,
)

_JOB = "small-job"

_FACTS_MD_NO_TARGETS = (
    "# 사실 확인\n\n| 주장 | 판정 | 출처 |\n|---|---|---|\n"
    "| 요청을 그대로 반영한다 | 통과 | request |\n\n"
    "## 코드에 넣는 값\n(없음)\n\n## 외부 대상\n(없음)\n"
)

_FACTS_MD_WITH_TARGET = (
    "# 사실 확인\n\n| 주장 | 판정 | 출처 |\n|---|---|---|\n"
    "| 요청을 그대로 반영한다 | 통과 | request |\n\n"
    "## 코드에 넣는 값\n(없음)\n\n## 외부 대상\n- 날씨 API: https://example.com/weather-api\n"
)


def _small_plan_json(*, requirements: bool = True) -> str:
    requirements_list = (
        [{"id": "R1", "text": "요청이 반영된다", "kind": "function"}] if requirements else []
    )
    scenes_list = (
        [{"id": "s1", "actor_does": "값을 확인한다", "app_shows": "바뀐 값이 보인다",
          "where": "cli", "cmd": ["python3", "tests/scenes/s1.py"], "covers": ["R1"]}]
        if requirements else []
    )
    return json.dumps({
        "goal": "작은 변경", "ui": False, "requirements": requirements_list,
        "scenes": scenes_list,
        "tasks": [{"id": "t1", "title": "값 바꾸기", "detail": "",
                  "owns": ["src/greeter/extra1.py", "tests/t1_test_owns.py"],
                  "depends_on": []}],
        "acceptance": ["요청이 반영된다"],
    })


def _planning_files(facts_md: str, plan_json: str) -> dict[str, str]:
    return {
        f".gongbang/{_JOB}/facts.md": facts_md,
        f".gongbang/{_JOB}/plan.json": plan_json,
        "tests/scenes/s1.py": "# scene stub -- acceptance is faked in this test\n",
    }


def test_small_job_runs_one_planning_call_and_finishes_done(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    stub = _RunStationStub({
        "plan": [_planning_files(_FACTS_MD_NO_TARGETS, _small_plan_json())],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    state = _install_fake_acceptance_and_handoff(
        monkeypatch, [_FakeAcceptanceResult(passed=True, scenes=(_FakeSceneResult("s1", True),))],
    )

    outcome = small_orbit.run_small_job(
        ws.config, ws.ledger, _fakes(), "alpha", _JOB, "값 하나만 바꿔줘",
        processor_factory=_processor_factory(stub),
    )

    assert outcome.status == "done"
    assert outcome.reason is None
    assert outcome.report_path is not None
    # Exactly one planning call (plan) + one build call -- a full job
    # would need a separate facts call too (see
    # test_full_orbit.test_two_task_plan_builds_both_tasks_in_order_and_
    # finishes_done, which needs "facts" AND "plan" AND "build").
    assert stub.calls == [("plan", "planner"), ("build", "builder")]
    assert (ws.alpha / "src/greeter/extra1.py").exists()
    assert state.acceptance_calls == 1
    assert len(state.handoff_calls) == 1

    # Per-station timing: planning (the combined call) and the
    # build/review/bench this one task's own attempt recorded (read
    # back from engine.orbit's own job.metrics events, never re-timed
    # -- a stubbed step can legitimately round to 0ms, so only the two
    # stages real enough to always take measurable time are checked).
    assert outcome.durations_ms["planning"] > 0
    assert outcome.durations_ms.get("build", 0) > 0


def test_small_job_refuses_when_facts_names_an_external_target(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    stub = _RunStationStub({
        "plan": [_planning_files(_FACTS_MD_WITH_TARGET, _small_plan_json())],
    })
    # Acceptance/handoff/security are never reached on this path -- an
    # empty results list means a call to run_acceptance would raise
    # IndexError (state.results.pop(0)), catching a regression that
    # runs the task loop anyway.
    _install_fake_acceptance_and_handoff(monkeypatch, [])

    outcome = small_orbit.run_small_job(
        ws.config, ws.ledger, _fakes(), "alpha", _JOB, "날씨를 보여줘",
        processor_factory=_processor_factory(stub),
    )

    assert outcome.status == "failed"
    assert outcome.reason == small_orbit.NEEDS_PROBE_REASON
    assert stub.calls == [("plan", "planner")]
    # Committed (never left uncommitted) AND closed -- the project
    # returns to CLOSED so tools/gb2's own fallback (engine.full_orbit.
    # run_full_job, under a fresh job_name) can open a new attempt right
    # after this call returns (a still-OPEN project would make
    # that fallback's own open_job raise "required state ('CLOSED',),
    # got 'OPEN'").
    assert (ws.alpha / f".gongbang/{_JOB}/facts.md").exists()
    assert ws.ledger.project("alpha").state == "CLOSED"
    log = subprocess.run(
        ["git", "-C", str(ws.alpha), "log", "--oneline", "--", f".gongbang/{_JOB}"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert log.strip()

    # The project being CLOSED again is not just a status flag -- a
    # brand-new job_name's own open_job can actually open right after
    # (the exact call tools/gb2's own fallback, engine.full_orbit.
    # run_full_job, makes first) rather than raising "required state
    # ('CLOSED',), got 'OPEN'" the way it did before this attempt was
    # ever closed.
    fallback_attempt_id = full_orbit_open_job(
        ws.config, ws.ledger, "alpha", f"{_JOB}-full", "full", (full_orbit.FACTS_ROLE,),
    )
    assert fallback_attempt_id
    assert ws.ledger.project("alpha").state == "OPEN"


def test_small_job_refuses_a_ui_plan(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid, single-task, requirement-covering plan that sets
    "ui": true must not proceed straight to the build task -- small
    mode never runs a designer or a design-approval gate. Same
    commit+close/project-returns-to-CLOSED story as the external-target
    refusal above."""
    ws = _full_orbit_workspace(workspace)
    ui_plan = json.dumps({
        "goal": "작은 변경", "ui": True,
        "requirements": [{"id": "R1", "text": "요청이 반영된다", "kind": "function"}],
        "scenes": [{"id": "s1", "actor_does": "값을 확인한다", "app_shows": "바뀐 값이 보인다",
                   "where": "cli", "cmd": ["python3", "tests/scenes/s1.py"], "covers": ["R1"]}],
        "tasks": [{"id": "t1", "title": "값 바꾸기", "detail": "", "owns": ["src/greeter/extra1.py"],
                  "depends_on": []}],
        "acceptance": ["요청이 반영된다"],
    })
    stub = _RunStationStub({"plan": [_planning_files(_FACTS_MD_NO_TARGETS, ui_plan)]})
    _install_fake_acceptance_and_handoff(monkeypatch, [])

    outcome = small_orbit.run_small_job(
        ws.config, ws.ledger, _fakes(), "alpha", _JOB, "화면 하나 바꿔줘",
        processor_factory=_processor_factory(stub),
    )

    assert outcome.status == "failed"
    assert outcome.reason == small_orbit.UI_PLAN_REASON
    assert stub.calls == [("plan", "planner")]
    assert ws.ledger.project("alpha").state == "CLOSED"


def _two_task_plan_json(*, ui: bool) -> str:
    return json.dumps({
        "goal": "작은 변경", "ui": ui,
        "requirements": [{"id": "R1", "text": "요청이 반영된다", "kind": "function"}],
        "scenes": [{"id": "s1", "actor_does": "값을 확인한다", "app_shows": "바뀐 값이 보인다",
                   "where": "cli", "cmd": ["python3", "tests/scenes/s1.py"], "covers": ["R1"]}],
        "tasks": [
            {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
             "depends_on": []},
            {"id": "t2", "title": "둘째 파일", "detail": "", "owns": ["src/greeter/extra2.py"],
             "depends_on": []},
        ],
        "acceptance": ["요청이 반영된다"],
    })


def test_small_job_refuses_a_plan_with_more_than_one_task(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    stub = _RunStationStub({
        "plan": [_planning_files(_FACTS_MD_NO_TARGETS, _two_task_plan_json(ui=False))],
    })
    _install_fake_acceptance_and_handoff(monkeypatch, [])

    outcome = small_orbit.run_small_job(
        ws.config, ws.ledger, _fakes(), "alpha", _JOB, "값 두 개를 바꿔줘",
        processor_factory=_processor_factory(stub),
    )

    assert outcome.status == "failed"
    assert outcome.reason == small_orbit.SINGLE_TASK_REASON
    # Committed+closed, same as the external-target/UI refusals -- the
    # project returns to CLOSED so a full-orbit fallback can open.
    assert ws.ledger.project("alpha").state == "CLOSED"


def test_small_job_ui_plan_with_more_than_one_task_is_reported_as_ui_not_single_task(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """External-review finding: the ui check must run BEFORE the task-
    count check, so a ui:true plan with more than one task is reported
    (and falls back) as UI_PLAN_REASON, never SINGLE_TASK_REASON just
    because it also happens to have more than one task."""
    ws = _full_orbit_workspace(workspace)
    stub = _RunStationStub({
        "plan": [_planning_files(_FACTS_MD_NO_TARGETS, _two_task_plan_json(ui=True))],
    })
    _install_fake_acceptance_and_handoff(monkeypatch, [])

    outcome = small_orbit.run_small_job(
        ws.config, ws.ledger, _fakes(), "alpha", _JOB, "화면 두 개 바꿔줘",
        processor_factory=_processor_factory(stub),
    )

    assert outcome.status == "failed"
    assert outcome.reason == small_orbit.UI_PLAN_REASON
    assert ws.ledger.project("alpha").state == "CLOSED"


def test_small_job_ui_two_task_plan_falls_back_to_full_job(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a ui:true plan with more than one task goes to the
    full fallback (maybe_full_fallback), same as any other UI plan."""
    ws = _full_orbit_workspace(workspace)
    fallback_job = small_orbit.fallback_job_name(_JOB)
    stub = _RunStationStub({
        "plan": [_planning_files(_FACTS_MD_NO_TARGETS, _two_task_plan_json(ui=True))],
        "facts": [{f".gongbang/{fallback_job}/facts.md": _FACTS_MD_NO_TARGETS}],
    })
    _install_fake_acceptance_and_handoff(monkeypatch, [])

    outcome = small_orbit.run_small_job(
        ws.config, ws.ledger, _fakes(), "alpha", _JOB, "화면 두 개 바꿔줘",
        processor_factory=_processor_factory(stub),
    )
    assert outcome.reason == small_orbit.UI_PLAN_REASON

    fallback = small_orbit.maybe_full_fallback(
        ws.config, ws.ledger, _fakes(), "alpha", _JOB, "화면 두 개 바꿔줘", outcome,
        processor_factory=_processor_factory(stub),
    )
    assert fallback is not None
    assert ws.ledger.attempts_for_job_name("alpha", fallback_job)
    assert ("facts", "facts") in stub.calls


def test_small_job_non_ui_two_task_plan_falls_back_to_full_job(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a plain (non-UI) plan with more than one task also
    falls back (maybe_full_fallback) -- a multi-task plan is itself a
    "this request was not small enough" signal, same as an external
    target or a UI plan, so the request continues on the full path
    instead of just failing outright."""
    ws = _full_orbit_workspace(workspace)
    fallback_job = small_orbit.fallback_job_name(_JOB)
    stub = _RunStationStub({
        "plan": [_planning_files(_FACTS_MD_NO_TARGETS, _two_task_plan_json(ui=False))],
        "facts": [{f".gongbang/{fallback_job}/facts.md": _FACTS_MD_NO_TARGETS}],
    })
    _install_fake_acceptance_and_handoff(monkeypatch, [])

    outcome = small_orbit.run_small_job(
        ws.config, ws.ledger, _fakes(), "alpha", _JOB, "값 두 개를 바꿔줘",
        processor_factory=_processor_factory(stub),
    )
    assert outcome.reason == small_orbit.SINGLE_TASK_REASON

    fallback = small_orbit.maybe_full_fallback(
        ws.config, ws.ledger, _fakes(), "alpha", _JOB, "값 두 개를 바꿔줘", outcome,
        processor_factory=_processor_factory(stub),
    )
    assert fallback is not None
    assert ws.ledger.attempts_for_job_name("alpha", fallback_job)
    assert ("facts", "facts") in stub.calls


def test_small_job_refuses_a_plan_with_no_requirements(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    stub = _RunStationStub({
        "plan": [_planning_files(_FACTS_MD_NO_TARGETS, _small_plan_json(requirements=False))],
    })
    _install_fake_acceptance_and_handoff(monkeypatch, [])

    outcome = small_orbit.run_small_job(
        ws.config, ws.ledger, _fakes(), "alpha", _JOB, "값 하나만 바꿔줘",
        processor_factory=_processor_factory(stub),
    )

    assert outcome.status == "failed"
    assert outcome.reason == small_orbit.REQUIREMENT_REASON


def test_small_job_runs_a_release_request_the_same_way_a_full_job_does(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """engine.small_orbit never writes its own release logic -- it
    hands off to engine.full_orbit._finish_from_tasks, the literal same
    function a full job's own task loop/acceptance/handoff ends in, so
    a release-configured project stops needs_owner/"release_approval"
    here exactly the way test_full_orbit.py's own release tests prove
    it does for a full job (this test only has to prove small_orbit
    actually reaches that same function; the release mechanics
    themselves are full_orbit's own, already tested there)."""
    import subprocess
    from dataclasses import replace as _replace

    from engine.config import ReleaseConfig

    ws = _full_orbit_workspace(workspace)
    master_repo = tmp_path / "release-master"
    subprocess.run(["git", "clone", "--shared", "-q", str(ws.alpha), str(master_repo)],
                   check=True, capture_output=True)
    project_config = _replace(
        ws.config.projects["alpha"],
        release=ReleaseConfig(master_repo=master_repo, target="alpha-app"),
    )
    config = _replace(ws.config, projects={**ws.config.projects, "alpha": project_config})

    stub = _RunStationStub({
        "plan": [_planning_files(_FACTS_MD_NO_TARGETS, _small_plan_json())],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    _install_fake_acceptance_and_handoff(
        monkeypatch, [_FakeAcceptanceResult(passed=True, scenes=(_FakeSceneResult("s1", True),))])

    outcome = small_orbit.run_small_job(
        config, ws.ledger, _fakes(), "alpha", _JOB, "값 하나만 바꿔줘",
        processor_factory=_processor_factory(stub),
    )

    assert outcome.status == "needs_owner"
    assert outcome.reason == "release_approval"


def test_gb2_continue_resumes_a_small_job_after_a_build_install_request(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A small job's own single task asks the owner for an install mid-
    build -- engine.small_orbit never records this itself (it hands
    the whole task loop to engine.full_orbit._finish_from_tasks, the
    literal same function a full job uses), so the resulting
    STAGE_TASK_OWNER progress is already in the shape `gb2 continue`
    (engine.full_orbit.continue_full_job) expects, with no extra work
    needed here -- this test proves exactly that, rather than
    reimplementing full_orbit's own install-resume test."""
    ws = _full_orbit_workspace(workspace)
    install_request = {"kind": "install", "manifest": "package.json",
                       "change": {"dependencies": {"left-pad": "^1.0.0"}}}
    build_calls = {"n": 0}

    def _build_steps(ctx: StepContext) -> list[StepDef]:
        def _run(ctx: StepContext) -> dict[str, object]:
            build_calls["n"] += 1
            copy = copy_root(ctx.config, ctx.attempt_id, "builder")
            target = copy / "src/greeter/extra1.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            if build_calls["n"] == 1:
                target.write_text("VALUE_1 = 1\n", encoding="utf-8")
                requests: list[dict[str, object]] = [install_request]
            else:
                target.write_text("VALUE_1 = 1\nVALUE_2 = 2\n", encoding="utf-8")
                requests = []
            changed = dirty_paths(copy, ())
            ctx.ledger.append(Event(
                project=ctx.project, kind="station.finished", actor_kind="processor",
                actor_id="processor", attempt_id=ctx.attempt_id,
                payload={
                    "request_id": ctx.request.request_id, "station": "build",
                    "role": "builder", "ok": True, "summary": "stub", "cost_usd": 0.0,
                    "turns": 1, "changed_paths": list(changed), "requests": requests,
                },
            ))
            return {}

        def _reconcile(ctx: StepContext) -> bool:
            for _seq, event in ctx.ledger.events(ctx.project, "station.finished"):
                if event.payload.get("request_id") == ctx.request.request_id:
                    return True
            return False

        return [StepDef("record_station", _run, _reconcile)]

    def factory(config, ledger, fakes) -> Processor:
        stub = _RunStationStub({
            "plan": [_planning_files(_FACTS_MD_NO_TARGETS, _small_plan_json())],
        })

        def dispatch(ctx: StepContext) -> list[StepDef]:
            if str(ctx.request.payload.get("station")) == "build":
                return _build_steps(ctx)
            return stub.steps(ctx)

        proc = Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", dispatch)
        proc.register("review", _review_always_passes_steps)
        return proc

    state = _install_fake_acceptance_and_handoff(
        monkeypatch, [_FakeAcceptanceResult(passed=True, scenes=(_FakeSceneResult("s1", True),))],
    )

    first = small_orbit.run_small_job(
        ws.config, ws.ledger, _fakes(), "alpha", _JOB, "값 하나만 바꿔줘",
        processor_factory=factory,
    )
    assert first.status == "needs_owner"
    assert first.reason == "owner_request"
    assert build_calls["n"] == 1
    assert state.acceptance_calls == 0

    _record_install(ws, first.attempt_id, install_request, "r-install-1")

    # `gb2 continue` is engine.full_orbit.continue_full_job -- small_orbit
    # itself has no resume entry point of its own (see its module
    # docstring); the whole point of this test is that it does not need
    # one here.
    resumed = full_orbit.continue_full_job(
        ws.config, ws.ledger, _fakes(), "alpha", _JOB, processor_factory=factory,
    )
    assert resumed.status == "done", resumed
    assert build_calls["n"] == 2
    assert state.acceptance_calls == 1
    assert (ws.alpha / "src/greeter/extra1.py").read_text(encoding="utf-8") == (
        "VALUE_1 = 1\nVALUE_2 = 2\n"
    )


def test_gb2_continue_resumes_a_small_job_after_security_failed(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same point as the install-request test above, for the OTHER
    needs_owner stop engine.small_orbit hands to full_orbit's own
    machinery for free: engine.app_security failing once, then passing
    on a plain `gb2 continue` retry (test_full_orbit.py's own
    test_security_fix_resume_without_a_moved_head_just_reruns_security,
    driven through small_orbit instead of full_orbit)."""
    ws = _full_orbit_workspace(workspace)
    stub = _RunStationStub({
        "plan": [_planning_files(_FACTS_MD_NO_TARGETS, _small_plan_json())],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    state = _install_fake_acceptance_and_handoff(
        monkeypatch, [_FakeAcceptanceResult(passed=True, scenes=(_FakeSceneResult("s1", True),))],
    )
    security_calls: list[str] = []
    security_results = [False, True]

    def check(config, project, attempt_id, plan):
        security_calls.append(attempt_id)
        return type("FakeSecurity", (), {"passed": security_results.pop(0),
                                          "findings": (), "ran": ()})()

    sys.modules["engine.app_security"].check = check  # type: ignore[attr-defined]

    outcome = small_orbit.run_small_job(
        ws.config, ws.ledger, _fakes(), "alpha", _JOB, "값 하나만 바꿔줘",
        processor_factory=_processor_factory(stub),
    )
    assert outcome.status == "needs_owner"
    assert str(outcome.reason).startswith("security_failed")

    resumed = full_orbit.continue_full_job(
        ws.config, ws.ledger, _fakes(), "alpha", _JOB,
        processor_factory=_processor_factory(stub),
    )
    assert resumed.status == "done", resumed
    assert len(security_calls) == 2
    assert state.acceptance_calls == 1


def test_small_job_crash_leaves_a_small_plan_cursor_resumable_directly(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash right after run_small_job's own FIRST cursor must not
    leave `resume_small_plan` with nothing to resume from, and must not
    resume with an EMPTY request (the cursor's own "request" field, not
    the combined small-mode prompt that was never finished). Regression
    this cursor stage exists to fix: this attempt's own copy was made
    for ONLY the planner role (run_small_job's one combined call) --
    resuming a bare "facts" stage directly on that SAME attempt_id
    would reach for a FACTS_ROLE copy that was never made at all.
    resume_small_plan
    instead opens a brand-new attempt (never the crashed one) with only
    the PLANNER_ROLE copy, and reruns the WHOLE combined call -- no
    separate "facts" station call is ever made for a small job."""
    from engine.faults import CrashInjected, Faults

    ws = _full_orbit_workspace(workspace)
    request_text = "값 하나만 바꿔줘, 반드시 이 글자가 남아야 한다"
    crash_stub = _RunStationStub({"plan": [_planning_files(_FACTS_MD_NO_TARGETS, _small_plan_json())]})

    def _crash_factory(config, ledger, fakes) -> Processor:
        proc = Processor(config, ledger, fakes, Faults(crash_before={"record_station"}))
        proc.register_defaults()
        proc.register("run_station", crash_stub.steps)
        return proc

    with pytest.raises(CrashInjected):
        small_orbit.run_small_job(
            ws.config, ws.ledger, _fakes(), "alpha", _JOB, request_text,
            processor_factory=_crash_factory,
        )

    # Nothing ran yet -- the crash fired before run_station's own one
    # step ("record_station") ever executed.
    cursor = position.load_position(ws.ledger, "alpha", _JOB)
    assert cursor is not None
    assert cursor.stage == small_orbit.CURSOR_STAGE
    assert cursor.get("request") == request_text
    crashed_attempt_id = str(cursor.attempt_id)
    assert cursor.stop_reason is None

    plan_task_texts: list[str] = []

    def _capturing_plan_steps(ctx: StepContext) -> list[StepDef]:
        if str(ctx.request.payload.get("station")) == "plan":
            plan_task_texts.append(str(ctx.request.payload.get("task", "")))
        return resume_stub.steps(ctx)

    resume_stub = _RunStationStub({
        "plan": [_planning_files(_FACTS_MD_NO_TARGETS, _small_plan_json())],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })

    def _resume_factory(config, ledger, fakes) -> Processor:
        proc = Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", _capturing_plan_steps)
        proc.register("review", _review_always_passes_steps)
        return proc

    _install_fake_acceptance_and_handoff(
        monkeypatch, [_FakeAcceptanceResult(passed=True, scenes=(_FakeSceneResult("s1", True),))],
    )

    resumed = small_orbit.resume_small_plan(
        ws.config, ws.ledger, _fakes(), "alpha", _JOB, cursor, processor_factory=_resume_factory,
    )

    assert resumed.status == "done", resumed
    assert resumed.attempt_id != crashed_attempt_id
    assert len(plan_task_texts) == 1
    assert request_text in plan_task_texts[0]
    # Only the combined "plan" station ran on resume -- a separate
    # "facts" station call (what a full job's own early-station resume
    # would make) never happened.
    assert resume_stub.calls == [("plan", "planner"), ("build", "builder")]


def test_gb2_continue_resumes_a_crashed_small_job_via_small_plan_cursor(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from engine.faults import CrashInjected, Faults

    ws = _full_orbit_workspace(workspace)
    request_text = "값 하나만 바꿔줘"
    crash_stub = _RunStationStub({"plan": [_planning_files(_FACTS_MD_NO_TARGETS, _small_plan_json())]})

    def _crash_factory(config, ledger, fakes) -> Processor:
        proc = Processor(config, ledger, fakes, Faults(crash_before={"record_station"}))
        proc.register_defaults()
        proc.register("run_station", crash_stub.steps)
        return proc

    with pytest.raises(CrashInjected):
        small_orbit.run_small_job(
            ws.config, ws.ledger, _fakes(), "alpha", _JOB, request_text,
            processor_factory=_crash_factory,
        )

    resume_stub = _RunStationStub({
        "plan": [_planning_files(_FACTS_MD_NO_TARGETS, _small_plan_json())],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })

    def _resume_factory(config, ledger, fakes) -> Processor:
        proc = Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", resume_stub.steps)
        proc.register("review", _review_always_passes_steps)
        return proc

    _install_fake_acceptance_and_handoff(
        monkeypatch, [_FakeAcceptanceResult(passed=True, scenes=(_FakeSceneResult("s1", True),))],
    )

    resumed = full_orbit.continue_full_job(
        ws.config, ws.ledger, _fakes(), "alpha", _JOB, processor_factory=_resume_factory,
    )
    assert resumed.status == "done", resumed


def test_resume_small_plan_waits_when_owner_request_is_not_yet_fulfilled(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A small job's OWN combined planning call (never a build task --
    see the other owner-request tests above for that already-covered
    case) can itself ask the owner for something (e.g. an install the
    planner needs to read a package's types). resume_small_plan must
    gate on that exactly like continue_full_job's own STAGE_EARLY_
    STATION dispatch does: never silently proceed, and never reopen a
    fresh attempt, while the request is still unfulfilled."""
    ws = _full_orbit_workspace(workspace)
    install_request = {"kind": "install", "manifest": "package.json",
                       "change": {"dependencies": {"left-pad": "^1.0.0"}}}

    def _plan_steps_with_request(ctx: StepContext) -> list[StepDef]:
        def _run(ctx: StepContext) -> dict[str, object]:
            copy = copy_root(ctx.config, ctx.attempt_id, "planner")
            (copy / f".gongbang/{_JOB}").mkdir(parents=True, exist_ok=True)
            (copy / f".gongbang/{_JOB}/facts.md").write_text(_FACTS_MD_NO_TARGETS, encoding="utf-8")
            changed = dirty_paths(copy, ())
            ctx.ledger.append(Event(
                project=ctx.project, kind="station.finished", actor_kind="processor",
                actor_id="processor", attempt_id=ctx.attempt_id,
                payload={
                    "request_id": ctx.request.request_id, "station": "plan",
                    "role": "planner", "ok": True, "summary": "stub", "cost_usd": 0.0,
                    "turns": 1, "changed_paths": list(changed), "requests": [install_request],
                },
            ))
            return {}

        def _reconcile(ctx: StepContext) -> bool:
            for _seq, event in ctx.ledger.events(ctx.project, "station.finished"):
                if event.payload.get("request_id") == ctx.request.request_id:
                    return True
            return False

        return [StepDef("record_station", _run, _reconcile)]

    def factory(config, ledger, fakes) -> Processor:
        proc = Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", _plan_steps_with_request)
        return proc

    _install_fake_acceptance_and_handoff(monkeypatch, [])

    first = small_orbit.run_small_job(
        ws.config, ws.ledger, _fakes(), "alpha", _JOB, "값 하나만 바꿔줘", processor_factory=factory,
    )
    assert first.status == "needs_owner"
    assert first.reason == "owner_request"

    cursor = position.load_position(ws.ledger, "alpha", _JOB)
    assert cursor is not None
    assert cursor.stage == small_orbit.CURSOR_STAGE
    assert list(cursor.pending_requests) == [install_request]

    # Still unfulfilled -- resume_small_plan must not reopen anything.
    still_waiting = small_orbit.resume_small_plan(
        ws.config, ws.ledger, _fakes(), "alpha", _JOB, cursor, processor_factory=factory,
    )
    assert still_waiting.status == "needs_owner"
    assert still_waiting.reason == "owner_request"
    assert still_waiting.attempt_id == first.attempt_id
    assert ws.ledger.project("alpha").state == "OPEN"


def test_pending_owner_requests_will_see_a_small_plan_cursors_own_install_request(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    install_request = {"kind": "install", "manifest": "package.json",
                       "change": {"dependencies": {"left-pad": "^1.0.0"}}}

    def _plan_steps_with_request(ctx: StepContext) -> list[StepDef]:
        def _run(ctx: StepContext) -> dict[str, object]:
            copy = copy_root(ctx.config, ctx.attempt_id, "planner")
            (copy / f".gongbang/{_JOB}").mkdir(parents=True, exist_ok=True)
            (copy / f".gongbang/{_JOB}/facts.md").write_text(_FACTS_MD_NO_TARGETS, encoding="utf-8")
            changed = dirty_paths(copy, ())
            ctx.ledger.append(Event(
                project=ctx.project, kind="station.finished", actor_kind="processor",
                actor_id="processor", attempt_id=ctx.attempt_id,
                payload={
                    "request_id": ctx.request.request_id, "station": "plan",
                    "role": "planner", "ok": True, "summary": "stub", "cost_usd": 0.0,
                    "turns": 1, "changed_paths": list(changed), "requests": [install_request],
                },
            ))
            return {}

        def _reconcile(ctx: StepContext) -> bool:
            for _seq, event in ctx.ledger.events(ctx.project, "station.finished"):
                if event.payload.get("request_id") == ctx.request.request_id:
                    return True
            return False

        return [StepDef("record_station", _run, _reconcile)]

    def factory(config, ledger, fakes) -> Processor:
        proc = Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", _plan_steps_with_request)
        return proc

    _install_fake_acceptance_and_handoff(monkeypatch, [])

    outcome = small_orbit.run_small_job(
        ws.config, ws.ledger, _fakes(), "alpha", _JOB, "값 하나만 바꿔줘", processor_factory=factory,
    )
    assert outcome.status == "needs_owner"
    assert outcome.reason == "owner_request"

    pending = full_orbit.pending_owner_requests(ws.ledger, "alpha", _JOB)
    assert pending == (install_request,)


def test_resume_small_plan_reopens_a_fresh_attempt_once_owner_request_is_fulfilled(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    install_request = {"kind": "install", "manifest": "package.json",
                       "change": {"dependencies": {"left-pad": "^1.0.0"}}}
    plan_calls = {"n": 0}

    def _plan_steps_with_request_then_success(ctx: StepContext) -> list[StepDef]:
        call_n = plan_calls["n"]
        plan_calls["n"] += 1

        def _run(ctx: StepContext) -> dict[str, object]:
            copy = copy_root(ctx.config, ctx.attempt_id, "planner")
            files = _planning_files(_FACTS_MD_NO_TARGETS, _small_plan_json())
            for rel, content in files.items():
                target = copy / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            changed = dirty_paths(copy, ())
            ctx.ledger.append(Event(
                project=ctx.project, kind="station.finished", actor_kind="processor",
                actor_id="processor", attempt_id=ctx.attempt_id,
                payload={
                    "request_id": ctx.request.request_id, "station": "plan",
                    "role": "planner", "ok": True, "summary": "stub", "cost_usd": 0.0,
                    "turns": 1, "changed_paths": list(changed), "requests": [],
                },
            ))
            return {}

        def _run_with_request(ctx: StepContext) -> dict[str, object]:
            copy = copy_root(ctx.config, ctx.attempt_id, "planner")
            (copy / f".gongbang/{_JOB}").mkdir(parents=True, exist_ok=True)
            (copy / f".gongbang/{_JOB}/facts.md").write_text(_FACTS_MD_NO_TARGETS, encoding="utf-8")
            changed = dirty_paths(copy, ())
            ctx.ledger.append(Event(
                project=ctx.project, kind="station.finished", actor_kind="processor",
                actor_id="processor", attempt_id=ctx.attempt_id,
                payload={
                    "request_id": ctx.request.request_id, "station": "plan",
                    "role": "planner", "ok": True, "summary": "stub", "cost_usd": 0.0,
                    "turns": 1, "changed_paths": list(changed), "requests": [install_request],
                },
            ))
            return {}

        def _reconcile(ctx: StepContext) -> bool:
            for _seq, event in ctx.ledger.events(ctx.project, "station.finished"):
                if event.payload.get("request_id") == ctx.request.request_id:
                    return True
            return False

        chosen = _run_with_request if call_n == 0 else _run
        return [StepDef("record_station", chosen, _reconcile)]

    build_stub = _RunStationStub({"build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}]})

    def factory(config, ledger, fakes) -> Processor:
        def dispatch(ctx: StepContext) -> list[StepDef]:
            if str(ctx.request.payload.get("station")) == "build":
                return build_stub.steps(ctx)
            return _plan_steps_with_request_then_success(ctx)

        proc = Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", dispatch)
        proc.register("review", _review_always_passes_steps)
        return proc

    state = _install_fake_acceptance_and_handoff(
        monkeypatch, [_FakeAcceptanceResult(passed=True, scenes=(_FakeSceneResult("s1", True),))],
    )

    first = small_orbit.run_small_job(
        ws.config, ws.ledger, _fakes(), "alpha", _JOB, "값 하나만 바꿔줘", processor_factory=factory,
    )
    assert first.status == "needs_owner"
    assert first.reason == "owner_request"
    first_attempt_id = first.attempt_id

    _record_install(ws, first_attempt_id, install_request, "r-install-1")

    cursor = position.load_position(ws.ledger, "alpha", _JOB)
    assert cursor is not None
    assert cursor.attempt_id == first_attempt_id

    resumed = small_orbit.resume_small_plan(
        ws.config, ws.ledger, _fakes(), "alpha", _JOB, cursor, processor_factory=factory,
    )
    assert resumed.status == "done", resumed
    assert resumed.attempt_id != first_attempt_id
    assert plan_calls["n"] == 2  # the resume's own fresh attempt made the SECOND (successful) call
    assert state.acceptance_calls == 1
    # The stale attempt was closed (report -> close) rather than left
    # dangling OPEN -- resume_small_plan's own fresh open_job call could
    # not otherwise have succeeded at all (open_job requires CLOSED).
    closed_states = [
        e.payload.get("state") for _seq, e in ws.ledger.events("alpha", "state.changed")
    ]
    assert "CLOSED" in closed_states


def test_resume_small_plan_after_plan_review_failure_reopens_a_fresh_attempt(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plan-review-failed stop also resumes through a fresh attempt
    (cursor stop_reason="plan_review_failed", pending_requests=[]) --
    never full_orbit's own STAGE_PLAN_REVIEW progress/_reopen_for_retry,
    which would open the full facts/prober/planner/designer role set
    this attempt's copies directory never had."""
    ws = _full_orbit_workspace(workspace)
    review_calls = {"n": 0}

    def _review_steps(ctx: StepContext) -> list[StepDef]:
        def _run(ctx: StepContext) -> dict[str, object]:
            review_calls["n"] += 1
            if review_calls["n"] == 1:
                raise StepFailed("review_not_passed")
            from engine import evidence

            project_config = ctx.config.projects[ctx.project]
            fp = evidence.fingerprint(project_config.repo, project_config.excludes)
            evidence.record(
                ctx.ledger, ctx.project, ctx.attempt_id, "review", fp,
                payload={"passed": True, "request_id": ctx.request.request_id, "findings": []},
            )
            return {}

        def _reconcile(ctx: StepContext) -> bool:
            if review_calls["n"] == 1:
                return False
            for row in ctx.ledger.evidence(ctx.attempt_id, "review"):
                if row.payload.get("request_id") == ctx.request.request_id:
                    return True
            return False

        return [StepDef("record_review", _run, _reconcile)]

    stub = _RunStationStub({
        "plan": [
            _planning_files(_FACTS_MD_NO_TARGETS, _small_plan_json()),
            _planning_files(_FACTS_MD_NO_TARGETS, _small_plan_json()),
        ],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })

    def factory(config, ledger, fakes) -> Processor:
        proc = Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", stub.steps)
        proc.register("review", _review_steps)
        return proc

    _install_fake_acceptance_and_handoff(
        monkeypatch, [_FakeAcceptanceResult(passed=True, scenes=(_FakeSceneResult("s1", True),))],
    )

    first = small_orbit.run_small_job(
        ws.config, ws.ledger, _fakes(), "alpha", _JOB, "값 하나만 바꿔줘", processor_factory=factory,
    )
    assert first.status == "needs_owner"
    assert first.reason == "plan_review_failed"
    first_attempt_id = first.attempt_id

    cursor = position.load_position(ws.ledger, "alpha", _JOB)
    assert cursor is not None
    assert cursor.stage == small_orbit.CURSOR_STAGE
    assert cursor.stop_reason == "plan_review_failed"
    assert cursor.pending_requests == ()

    # A second planning worker call is still queued on the stub (reused
    # across both attempts, same as test_full_orbit.py's own plan-
    # review-retry tests).
    resumed = small_orbit.resume_small_plan(
        ws.config, ws.ledger, _fakes(), "alpha", _JOB, cursor, processor_factory=factory,
    )
    assert resumed.status == "done", resumed
    assert resumed.attempt_id != first_attempt_id
    # review_calls counts every "review" request this job ever ran:
    # the first (failing) plan review, the resumed (passing) plan
    # review, and the one task's own build-review -- three total.
    assert review_calls["n"] == 3
    assert stub.calls == [("plan", "planner"), ("plan", "planner"), ("build", "builder")]


def test_resume_small_plan_falls_back_to_full_job_when_retry_still_names_an_external_target(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resume_small_plan's own
    fresh combined call can rediscover the exact same refusal
    (NEEDS_PROBE_REASON/UI_PLAN_REASON) the FIRST call hit -- before
    maybe_full_fallback existed, resume_small_plan just returned that
    refusal as a bare FullOutcome; `gb2 continue` reported it but
    never actually ran the full-orbit fallback the initial `gb2 change
    --small` call would have. This drives resume_small_plan directly
    with a synthetic "crash before anything ran" cursor (attempt_id
    None -- the same shape a job's very first cursor has) whose own
    combined retry still names a real target, and checks the fallback
    full job (engine.full_orbit.run_full_job, under
    small_orbit.fallback_job_name(_JOB)) actually started."""
    ws = _full_orbit_workspace(workspace)
    fallback_job = small_orbit.fallback_job_name(_JOB)
    stub = _RunStationStub({
        "plan": [_planning_files(_FACTS_MD_WITH_TARGET, _small_plan_json())],
        "facts": [{f".gongbang/{fallback_job}/facts.md": _FACTS_MD_NO_TARGETS}],
    })
    _install_fake_acceptance_and_handoff(monkeypatch, [])

    cursor = position.JobPosition(job=_JOB, stage=small_orbit.CURSOR_STAGE, attempt_id=None,
                                  extra={"request": "날씨를 보여줘", "max_fix_rounds": 2})
    resumed = small_orbit.resume_small_plan(
        ws.config, ws.ledger, _fakes(), "alpha", _JOB, cursor,
        processor_factory=_processor_factory(stub),
    )

    # The retry's own combined call ran first (station "plan"), hit the
    # same refusal, closed+committed, and the fallback full job's own
    # facts station then actually ran under fallback_job_name(_JOB) --
    # proving the fallback was dispatched, not merely reported.
    assert stub.calls[0] == ("plan", "planner")
    assert ("facts", "facts") in stub.calls
    assert ws.ledger.attempts_for_job_name("alpha", fallback_job)
    assert resumed.attempt_id != cursor.attempt_id


def test_resume_small_plan_rejects_a_cursor_from_another_stage() -> None:
    with pytest.raises(small_orbit.SmallOrbitError):
        small_orbit.resume_small_plan(
            None, None, None, "alpha", _JOB,  # type: ignore[arg-type]
            position.JobPosition(job=_JOB, stage="facts", attempt_id="x",
                                 extra={"request": "r"}),
        )


def test_small_planning_stops_needs_owner_when_a_required_skill_is_missing(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The combined planning station gates on its required context the
    same way the full job's stations do: no worker runs, the job stops
    needs_owner, and continue rechecks before dispatching."""
    from dataclasses import replace

    from engine.context import ContextConfig

    ws = _full_orbit_workspace(workspace)
    gap_config = replace(ws.config, context=ContextConfig(
        station_skills={"plan": ("필수-없는-스킬",)},
        required_skills={"plan": ("필수-없는-스킬",)}))
    stub = _RunStationStub({
        "plan": [_planning_files(_FACTS_MD_NO_TARGETS, _small_plan_json())],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    _install_fake_acceptance_and_handoff(monkeypatch, [_FakeAcceptanceResult(passed=True)])

    first = small_orbit.run_small_job(gap_config, ws.ledger, _fakes(), "alpha", _JOB,
                                      "값 하나만 바꿔줘", processor_factory=_processor_factory(stub))

    assert first.status == "needs_owner", first
    assert first.reason is not None and "필수-없는-스킬" in first.reason
    assert stub.calls == []

    still = full_orbit.continue_full_job(gap_config, ws.ledger, _fakes(), "alpha", _JOB,
                                         processor_factory=_processor_factory(stub))
    assert still.status == "needs_owner" and stub.calls == []

    done = full_orbit.continue_full_job(ws.config, ws.ledger, _fakes(), "alpha", _JOB,
                                        processor_factory=_processor_factory(stub))
    assert done.status == "done", done
