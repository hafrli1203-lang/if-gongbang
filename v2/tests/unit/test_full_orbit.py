"""Scripted end-to-end tests for engine.full_orbit: the four flows the
full-orbit contract calls out --
  1. a two-task plan: both tasks build in order, acceptance passes,
     handoff is called, status "done".
  2. a ui plan: stops needs_owner "design_approval"; after a
     design.approved ledger event, continue_full_job finishes.
  3. acceptance fails once, one fix round, then passes -> "done".
  4. acceptance keeps failing past max_fix_rounds -> "failed".

No live AI: run_station/review are stubbed at the Processor level
(like tests/unit/test_orbit.py already does for engine.orbit itself),
writing real files into each role's own worker copy so merge/gates/
plan parsing all run for real against real files in a real sample app
repo (tests/fixtures/sample_app.make_sample). engine.acceptance/
engine.handoff (worker C's modules) do not exist in this worktree yet,
so they are stubbed via sys.modules -- engine.full_orbit imports them
lazily by name, exactly as it will once those modules land for real."""
from __future__ import annotations

import json
import subprocess
import sys
import types
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from engine import full_orbit, orbit
from engine.config import ReleaseConfig
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.ledger import Event
from engine.processor import Fakes, Processor, StepContext, StepDef, StepFailed
from engine.steps.open_job import copy_root, dirty_paths
from tests.conftest import GIT_ENV, Workspace
from tests.fixtures.sample_app import make_sample

FULL_STATION_OWNERS = {
    "facts": {"facts": (".gongbang/",)},
    "probe": {"prober": (".gongbang/",)},
    "plan": {"planner": (".gongbang/", "tests/scenes/")},
    "design": {"designer": (".gongbang/",)},
    "build": {"builder": ("src/",)},
    "review": {"reviewer": ("review.md",)},
}


def _full_orbit_workspace(workspace: Workspace) -> Workspace:
    sample = make_sample(workspace.root / "sample", 1, Path(sys.executable))
    project_config = replace(
        workspace.config.projects["alpha"], repo=sample.repo,
        test_cmd=sample.test_cmd, install_cmd=sample.install_cmd,
        manifest=sample.manifest, bench_cases=sample.bench_cases,
    )
    new_config = replace(
        workspace.config,
        projects={**workspace.config.projects, "alpha": project_config},
        station_owners=FULL_STATION_OWNERS,
    )
    return Workspace(workspace.root, new_config, workspace.ledger, sample.repo, workspace.beta)


def _fakes() -> Fakes:
    return Fakes(publisher=FakePublisher(), installer=FakeInstaller())


def _plan_json(*, ui: bool = False, tasks: list[dict[str, object]]) -> str:
    return json.dumps({
        "goal": "작은 기능 추가", "ui": ui, "scenes": [],
        "tasks": tasks, "acceptance": ["요구사항대로 동작한다"],
    })


class _RunStationStub:
    """Pops one {relpath: content} dict off `queues[station]` per
    run_station call for that station, writes those files into the
    requesting role's own worker copy, then appends the real
    station.finished event shape (ok=True, changed_paths from the
    copy's own git status, requests=[])."""

    def __init__(self, queues: dict[str, list[dict[str, str]]]) -> None:
        self.queues = queues
        self.calls: list[tuple[str, str]] = []

    def steps(self, ctx: StepContext) -> list[StepDef]:
        def _run(ctx: StepContext) -> dict[str, object]:
            station = str(ctx.request.payload.get("station"))
            role = str(ctx.request.payload.get("role"))
            self.calls.append((station, role))
            copy = copy_root(ctx.config, ctx.attempt_id, role)
            files = self.queues[station].pop(0)
            for rel, content in files.items():
                target = copy / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            changed = dirty_paths(copy, ())
            ctx.ledger.append(Event(
                project=ctx.project, kind="station.finished", actor_kind="processor",
                actor_id="processor", attempt_id=ctx.attempt_id,
                payload={
                    "request_id": ctx.request.request_id, "station": station,
                    "role": role, "ok": True, "summary": "stub", "cost_usd": 0.0,
                    "turns": 1, "changed_paths": list(changed), "requests": [],
                },
            ))
            return {}

        def _reconcile(ctx: StepContext) -> bool:
            for _seq, event in ctx.ledger.events(ctx.project, "station.finished"):
                if event.payload.get("request_id") == ctx.request.request_id:
                    return True
            return False

        return [StepDef("record_station", _run, _reconcile)]


def _review_always_passes_steps(ctx: StepContext) -> list[StepDef]:
    from engine import evidence

    def _run(ctx: StepContext) -> dict[str, object]:
        project_config = ctx.config.projects[ctx.project]
        fp = evidence.fingerprint(project_config.repo, project_config.excludes)
        evidence.record(
            ctx.ledger, ctx.project, ctx.attempt_id, "review", fp,
            payload={"passed": True, "request_id": ctx.request.request_id, "findings": []},
        )
        return {}

    def _reconcile(ctx: StepContext) -> bool:
        for row in ctx.ledger.evidence(ctx.attempt_id, "review"):
            if row.payload.get("request_id") == ctx.request.request_id:
                return True
        return False

    return [StepDef("record_review", _run, _reconcile)]


def _processor_factory(stub: _RunStationStub) -> orbit.ProcessorFactory:
    def factory(config, ledger, fakes) -> Processor:
        proc = Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", stub.steps)
        proc.register("review", _review_always_passes_steps)
        return proc
    return factory


# ---- fake engine.acceptance / engine.handoff (worker C's modules) -----

@dataclass(frozen=True)
class _FakeSceneResult:
    id: str
    passed: bool
    returncode: int = 0
    tail: str = ""


@dataclass(frozen=True)
class _FakeAcceptanceResult:
    passed: bool
    scenes: tuple[_FakeSceneResult, ...] = ()
    full_suite_passed: bool = True


@dataclass
class _AcceptanceHandoffFakes:
    results: list[_FakeAcceptanceResult]
    acceptance_calls: int = 0
    handoff_calls: list[_FakeAcceptanceResult] = field(default_factory=list)


@dataclass(frozen=True)
class _FakeSecurityResult:
    passed: bool
    findings: tuple[str, ...] = ()
    ran: tuple[str, ...] = ()


def _install_fake_app_security(monkeypatch: pytest.MonkeyPatch, *,
                               passed: bool = True) -> list[tuple[str, str]]:
    """Stands in for s10-ops' own engine.app_security module (not yet
    merged into this worktree -- engine.full_orbit imports it lazily by
    name, same pattern as engine.acceptance/engine.handoff above).
    Returns the list `check` calls append (project, attempt_id) to, so
    a test can assert it ran exactly once."""
    calls: list[tuple[str, str]] = []
    security_mod = types.ModuleType("engine.app_security")

    def check(config, project, attempt_id, plan):
        calls.append((project, attempt_id))
        return _FakeSecurityResult(passed=passed)

    security_mod.check = check  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "engine.app_security", security_mod)
    import engine as _engine_pkg
    monkeypatch.setattr(_engine_pkg, "app_security", security_mod, raising=False)
    return calls


def _install_fake_acceptance_and_handoff(
    monkeypatch: pytest.MonkeyPatch, results: list[_FakeAcceptanceResult], *,
    security_passed: bool = True,
) -> _AcceptanceHandoffFakes:
    _install_fake_app_security(monkeypatch, passed=security_passed)
    state = _AcceptanceHandoffFakes(results=list(results))

    acceptance_mod = types.ModuleType("engine.acceptance")

    def run_acceptance(config, ledger, project, attempt_id, plan):
        state.acceptance_calls += 1
        return state.results.pop(0)

    acceptance_mod.run_acceptance = run_acceptance  # type: ignore[attr-defined]

    def run_probe_scripts(config, ledger, project, attempt_id, job_name, targets):
        # Stands in for the engine's own run of each probe script: a
        # target passes when its scripted log says PROBE OK (the stub
        # scripts in these tests write a log, not a runnable script).
        from types import SimpleNamespace
        repo = config.projects[project].repo
        out = []
        for name, _url in targets:
            log = repo / ".gongbang" / job_name / "probe" / f"{name}.log"
            text = log.read_text(encoding="utf-8") if log.exists() else ""
            ok = "PROBE OK" in text and "PROBE FAIL" not in text
            out.append(SimpleNamespace(name=name, script=str(log), returncode=0 if ok else 1,
                                       stdout_tail=text[-200:], ok=ok))
        return tuple(out)

    acceptance_mod.run_probe_scripts = run_probe_scripts  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "engine.acceptance", acceptance_mod)
    import engine as _engine_pkg
    monkeypatch.setattr(_engine_pkg, "acceptance", acceptance_mod, raising=False)

    handoff_mod = types.ModuleType("engine.handoff")

    def write_handoff(config, ledger, project, attempt_id, job, plan, acc):
        state.handoff_calls.append(acc)
        report_path = config.projects[project].repo / ".gongbang" / job / "report.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("report\n", encoding="utf-8")
        return report_path

    handoff_mod.write_handoff = write_handoff  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "engine.handoff", handoff_mod)
    import engine as _engine_pkg
    monkeypatch.setattr(_engine_pkg, "handoff", handoff_mod, raising=False)
    return state


# ---- 1. two-task plan: both tasks build in order, done -----------------

def test_two_task_plan_builds_both_tasks_in_order_and_finishes_done(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "two-task-job"
    plan_json = _plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
        {"id": "t2", "title": "둘째 파일", "detail": "", "owns": ["src/greeter/extra2.py"],
         "depends_on": ["t1"]},
    ])
    stub = _RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": "- 요청 확인: 통과 (출처: request)\n"}],
        "plan": [{f".gongbang/{job_name}/plan.json": plan_json}],
        "build": [
            {"src/greeter/extra1.py": "VALUE_1 = 1\n"},
            {"src/greeter/extra2.py": "VALUE_2 = 2\n"},
        ],
    })
    state = _install_fake_acceptance_and_handoff(
        monkeypatch, [_FakeAcceptanceResult(passed=True)],
    )

    outcome = full_orbit.run_full_job(
        ws.config, ws.ledger, _fakes(), "alpha", job_name, "두 파일을 만들어줘",
        processor_factory=_processor_factory(stub),
    )

    assert outcome.status == "done"
    assert outcome.reason is None
    assert outcome.report_path is not None
    assert Path(outcome.report_path).exists()
    assert [c for c in stub.calls if c[0] == "build"] == [
        ("build", "builder"), ("build", "builder"),
    ]
    assert (ws.alpha / "src/greeter/extra1.py").exists()
    assert (ws.alpha / "src/greeter/extra2.py").exists()
    assert state.acceptance_calls == 1
    assert len(state.handoff_calls) == 1
    assert state.handoff_calls[0].passed is True


# ---- 2. ui plan: needs_owner design_approval, then continue -----------

def test_ui_plan_stops_for_design_approval_then_continue_finishes(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "ui-job"
    plan_json = _plan_json(ui=True, tasks=[
        {"id": "t1", "title": "화면 연결", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    stub = _RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": "- 요청 확인: 통과 (출처: request)\n"}],
        "plan": [{f".gongbang/{job_name}/plan.json": plan_json}],
        "design": [{f".gongbang/{job_name}/design/design.md": "# 시안\n"}],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    state = _install_fake_acceptance_and_handoff(
        monkeypatch, [_FakeAcceptanceResult(passed=True)],
    )
    factory = _processor_factory(stub)

    first = full_orbit.run_full_job(
        ws.config, ws.ledger, _fakes(), "alpha", job_name, "화면 하나 만들어줘",
        processor_factory=factory,
    )
    assert first.status == "needs_owner"
    assert first.reason == "design_approval"
    # The designer station's own output already landed (merge already ran
    # before the stop) -- the owner is looking at a real file, not a promise.
    assert (ws.alpha / f".gongbang/{job_name}/design/design.md").exists()

    # continue_full_job before approval: still waiting, no progress lost.
    still_waiting = full_orbit.continue_full_job(
        ws.config, ws.ledger, _fakes(), "alpha", job_name, processor_factory=factory,
    )
    assert still_waiting.status == "needs_owner"
    assert still_waiting.reason == "design_approval"

    ws.ledger.append(Event(
        project="alpha", kind="design.approved", actor_kind="owner", actor_id="owner",
        attempt_id=None, payload={"job": job_name},
    ))

    done = full_orbit.continue_full_job(
        ws.config, ws.ledger, _fakes(), "alpha", job_name, processor_factory=factory,
    )
    assert done.status == "done"
    assert done.report_path is not None
    assert (ws.alpha / "src/greeter/extra1.py").exists()
    assert state.acceptance_calls == 1
    assert len(state.handoff_calls) == 1


# ---- 3. acceptance fails once, one fix round, then passes -------------

def test_acceptance_fails_once_fix_round_then_passes(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "fix-once-job"
    plan_json = _plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    stub = _RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": "- 요청 확인: 통과 (출처: request)\n"}],
        "plan": [{f".gongbang/{job_name}/plan.json": plan_json}],
        "build": [
            {"src/greeter/extra1.py": "VALUE_1 = 1\n"},
            {"src/greeter/extra1.py": "VALUE_1 = 2  # fixed\n"},
        ],
    })
    state = _install_fake_acceptance_and_handoff(monkeypatch, [
        _FakeAcceptanceResult(passed=False, scenes=(_FakeSceneResult("s1", False),)),
        _FakeAcceptanceResult(passed=True),
    ])

    outcome = full_orbit.run_full_job(
        ws.config, ws.ledger, _fakes(), "alpha", job_name, "파일 하나 만들어줘",
        processor_factory=_processor_factory(stub),
    )

    assert outcome.status == "done"
    assert state.acceptance_calls == 2
    assert len(state.handoff_calls) == 1
    assert state.handoff_calls[0].passed is True
    assert (ws.alpha / "src/greeter/extra1.py").read_text(encoding="utf-8") == (
        "VALUE_1 = 2  # fixed\n"
    )
    # Exactly one fix-round build call (two total: the original task + one fix).
    assert [c for c in stub.calls if c[0] == "build"] == [
        ("build", "builder"), ("build", "builder"),
    ]


# ---- 4. acceptance keeps failing past max_fix_rounds -------------------

def test_acceptance_fails_past_max_fix_rounds_reports_failed(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "fix-exceeded-job"
    plan_json = _plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    stub = _RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": "- 요청 확인: 통과 (출처: request)\n"}],
        "plan": [{f".gongbang/{job_name}/plan.json": plan_json}],
        "build": [
            {"src/greeter/extra1.py": "VALUE_1 = 1\n"},
            {"src/greeter/extra1.py": "VALUE_1 = 2\n"},  # the one allowed fix round
        ],
    })
    state = _install_fake_acceptance_and_handoff(monkeypatch, [
        _FakeAcceptanceResult(passed=False, scenes=(_FakeSceneResult("s1", False),)),
        _FakeAcceptanceResult(passed=False, scenes=(_FakeSceneResult("s1", False),)),
    ])

    outcome = full_orbit.run_full_job(
        ws.config, ws.ledger, _fakes(), "alpha", job_name, "파일 하나 만들어줘",
        max_fix_rounds=1, processor_factory=_processor_factory(stub),
    )

    assert outcome.status == "failed"
    assert outcome.reason == "max_fix_rounds_exceeded"
    assert state.acceptance_calls == 2
    assert state.handoff_calls == []


# ---- 5. probe station: skipped / succeeds / needs_owner on failure -----

def _plan_task_text(ws: Workspace) -> str:
    """The task text of the LAST "plan" run_station request this job's
    own ledger holds -- mirrors tests/acceptance/test_t1_new_app.py's
    own request.submitted lookup."""
    texts = [
        str((event.payload.get("payload") or {}).get("task", ""))
        for _seq, event in ws.ledger.events("alpha", "request.submitted")
        if event.payload.get("kind") == "run_station"
        and (event.payload.get("payload") or {}).get("station") == "plan"
    ]
    assert texts, "no plan run_station request found"
    return texts[-1]


def test_probe_skipped_when_facts_has_no_외부_대상(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "no-probe-job"
    plan_json = _plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    stub = _RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": "- 요청 확인: 통과 (출처: request)\n"}],
        "plan": [{f".gongbang/{job_name}/plan.json": plan_json}],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    _install_fake_acceptance_and_handoff(monkeypatch, [_FakeAcceptanceResult(passed=True)])

    outcome = full_orbit.run_full_job(
        ws.config, ws.ledger, _fakes(), "alpha", job_name, "파일 하나 만들어줘",
        processor_factory=_processor_factory(stub),
    )

    assert outcome.status == "done"
    assert ("probe", "prober") not in stub.calls
    skipped = [event for _seq, event in ws.ledger.events("alpha", "probe.skipped")
              if event.payload.get("job") == job_name]
    assert len(skipped) == 1


def test_probe_runs_and_logs_reach_the_planner_as_실측_결과(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "probe-ok-job"
    facts_md = (
        "# 사실확정\n"
        "- 요청 확인: 통과 (출처: request)\n\n"
        "## 외부 대상\n"
        "- stripe: https://docs.stripe.com/api\n"
    )
    plan_json = _plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    stub = _RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": facts_md}],
        "probe": [{f".gongbang/{job_name}/probe/stripe.log":
                  "https://docs.stripe.com/api\nPROBE OK\n{\"status\": 200}\n"}],
        "plan": [{f".gongbang/{job_name}/plan.json": plan_json}],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    _install_fake_acceptance_and_handoff(monkeypatch, [_FakeAcceptanceResult(passed=True)])

    outcome = full_orbit.run_full_job(
        ws.config, ws.ledger, _fakes(), "alpha", job_name, "stripe 결제 붙여줘",
        processor_factory=_processor_factory(stub),
    )

    assert outcome.status == "done"
    assert ("probe", "prober") in stub.calls
    assert (ws.alpha / f".gongbang/{job_name}/probe/stripe.log").exists()
    plan_text = _plan_task_text(ws)
    assert "실측 결과" in plan_text
    assert "PROBE OK" in plan_text


def test_probe_failure_stops_needs_owner_without_crashing(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "probe-fail-job"
    facts_md = (
        "# 사실확정\n"
        "- 요청 확인: 통과 (출처: request)\n\n"
        "## 외부 대상\n"
        "- stripe: https://docs.stripe.com/api\n"
    )
    stub = _RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": facts_md}],
        # The prober's own station.finished reports ok=True (it ran),
        # but the log it wrote never says PROBE OK -- check_probe, not
        # the worker's own self-report, is what the job trusts.
        "probe": [{f".gongbang/{job_name}/probe/stripe.log":
                  "https://docs.stripe.com/api\nPROBE FAIL\nconnection refused\n"}],
    })
    _install_fake_acceptance_and_handoff(monkeypatch, [])

    outcome = full_orbit.run_full_job(
        ws.config, ws.ledger, _fakes(), "alpha", job_name, "stripe 결제 붙여줘",
        processor_factory=_processor_factory(stub),
    )

    assert outcome.status == "needs_owner"
    assert outcome.reason == "probe_failed:stripe"
    # The prober's own log trail already landed/committed -- the owner
    # looks at a real log, not a promise.
    assert (ws.alpha / f".gongbang/{job_name}/probe/stripe.log").exists()
    # The job never reached "plan" -- no run_station call for it.
    assert ("plan", "planner") not in stub.calls


# ---- 8. plan review gate: a P0/P1 finding on the planner's own change --

def _review_always_fails_steps(ctx: StepContext) -> list[StepDef]:
    def _run(ctx: StepContext) -> dict[str, object]:
        raise StepFailed("review_not_passed")

    def _reconcile(ctx: StepContext) -> bool:
        return False

    return [StepDef("record_review", _run, _reconcile)]


def test_plan_review_failure_stops_before_committing_the_plan_trail(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "plan-review-fail-job"
    plan_json = _plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    stub = _RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": "- 요청 확인: 통과 (출처: request)\n"}],
        "plan": [{f".gongbang/{job_name}/plan.json": plan_json}],
    })

    def factory(config, ledger, fakes) -> Processor:
        proc = Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", stub.steps)
        proc.register("review", _review_always_fails_steps)
        return proc

    _install_fake_acceptance_and_handoff(monkeypatch, [])

    outcome = full_orbit.run_full_job(
        ws.config, ws.ledger, _fakes(), "alpha", job_name, "파일 하나 만들어줘",
        processor_factory=factory,
    )

    assert outcome.status == "needs_owner"
    assert outcome.reason == "plan_review_failed"
    # The gate stopped the job before any build task ever ran.
    assert ("build", "builder") not in stub.calls
    # plan.json landed in the working tree (the plan merge already ran --
    # the review judged real files, not a promise) but the plan trail's
    # own commit -- which would make it every later reviewer's unreviewed
    # base_head -- never happened, because the gate ran first.
    assert (ws.alpha / f".gongbang/{job_name}/plan.json").exists()
    log = subprocess.run(["git", "-C", str(ws.alpha), "log", "--oneline"],
                         capture_output=True, text=True, check=True).stdout
    assert f"{job_name} - plan" not in log


# ---- 9. worker_stuck resume: rerun the build task, then merge, review --

def test_worker_stuck_resume_reruns_build_task_before_review(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "stuck-job"
    plan_json = _plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    stub = _RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": "- 요청 확인: 통과 (출처: request)\n"}],
        "plan": [{f".gongbang/{job_name}/plan.json": plan_json}],
    })
    build_calls = {"n": 0}

    def _build_steps(ctx: StepContext) -> list[StepDef]:
        station = str(ctx.request.payload.get("station"))
        if station != "build":
            return stub.steps(ctx)

        def _run(ctx: StepContext) -> dict[str, object]:
            build_calls["n"] += 1
            if build_calls["n"] <= 4:
                # Simulate a worker that stalls every time -- exhausts
                # engine.orbit's own MAX_STATION_CONTINUES continuations
                # within run_full_job's single call, stopping it
                # needs_owner "worker_stuck" with the merge stage never
                # having run for this task.
                raise StepFailed("worker_stalled")
            copy = copy_root(ctx.config, ctx.attempt_id, "builder")
            target = copy / "src/greeter/extra1.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("VALUE_1 = 1\n", encoding="utf-8")
            changed = dirty_paths(copy, ())
            ctx.ledger.append(Event(
                project=ctx.project, kind="station.finished", actor_kind="processor",
                actor_id="processor", attempt_id=ctx.attempt_id,
                payload={
                    "request_id": ctx.request.request_id, "station": "build",
                    "role": "builder", "ok": True, "summary": "stub", "cost_usd": 0.0,
                    "turns": 1, "changed_paths": list(changed), "requests": [],
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
        proc.register("run_station", _build_steps)
        proc.register("review", _review_always_passes_steps)
        return proc

    state = _install_fake_acceptance_and_handoff(
        monkeypatch, [_FakeAcceptanceResult(passed=True)],
    )

    first = full_orbit.run_full_job(
        ws.config, ws.ledger, _fakes(), "alpha", job_name, "파일 하나 만들어줘",
        processor_factory=factory,
    )

    assert first.status == "needs_owner"
    # Every needs_owner stop surfaces the same generic FullOutcome.reason
    # ("owner_request" -- see _station_outcome_to_full); the real orbit-
    # level reason ("worker_stuck") is journaled separately as this job's
    # progress stop_reason, which continue_full_job reads to pick the
    # right resume path.
    assert first.reason == "owner_request"
    progress = full_orbit._load_progress(ws.ledger, "alpha", first.attempt_id, job_name)
    assert progress is not None
    assert progress.get("stop_reason") == "worker_stuck"
    # The merge stage never ran for this stalled attempt -- nothing landed.
    assert not (ws.alpha / "src/greeter/extra1.py").exists()
    assert build_calls["n"] == 4

    done = full_orbit.continue_full_job(
        ws.config, ws.ledger, _fakes(), "alpha", job_name, processor_factory=factory,
    )

    assert done.status == "done", done
    assert build_calls["n"] == 5
    assert (ws.alpha / "src/greeter/extra1.py").read_text(encoding="utf-8") == "VALUE_1 = 1\n"
    assert state.acceptance_calls == 1
    assert len(state.handoff_calls) == 1


# ---- 10. release: ONE publish request for the whole job, not one per task --

def _release_project(ws: Workspace, tmp_path: Path):
    """Give "alpha" a `release` config whose master_repo is a `git clone
    --shared` of alpha made BEFORE any of this job's own commits --
    same technique/rationale as tests/unit/test_orbit.py's own
    _release_project."""
    master_repo = tmp_path / "release-master"
    subprocess.run(["git", "clone", "--shared", "-q", str(ws.alpha), str(master_repo)],
                  check=True, capture_output=True, env=GIT_ENV)
    from dataclasses import replace as _replace

    project_config = _replace(
        ws.config.projects["alpha"],
        release=ReleaseConfig(master_repo=master_repo, target="alpha-app"),
    )
    config = _replace(ws.config, projects={**ws.config.projects, "alpha": project_config})
    return config, master_repo


def test_release_configured_project_makes_one_publish_request_for_the_whole_job(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    ws = _full_orbit_workspace(workspace)
    config, _master_repo = _release_project(ws, tmp_path)
    job_name = "release-job"
    plan_json = _plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
        {"id": "t2", "title": "둘째 파일", "detail": "", "owns": ["src/greeter/extra2.py"],
         "depends_on": ["t1"]},
    ])
    stub = _RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": "- 요청 확인: 통과 (출처: request)\n"}],
        "plan": [{f".gongbang/{job_name}/plan.json": plan_json}],
        "build": [
            {"src/greeter/extra1.py": "VALUE_1 = 1\n"},
            {"src/greeter/extra2.py": "VALUE_2 = 2\n"},
        ],
    })
    _install_fake_acceptance_and_handoff(monkeypatch, [_FakeAcceptanceResult(passed=True)])

    outcome = full_orbit.run_full_job(
        config, ws.ledger, _fakes(), "alpha", job_name, "두 파일을 만들어줘",
        processor_factory=_processor_factory(stub),
    )

    # Both tasks built in full BEFORE the job ever stops for release --
    # a per-task release stop (the bug this fixes) would have returned
    # needs_owner right after task t1's own commit, with t2 never built.
    assert (ws.alpha / "src/greeter/extra1.py").exists()
    assert (ws.alpha / "src/greeter/extra2.py").exists()
    assert outcome.status == "needs_owner"
    assert outcome.reason == "release_approval"

    publish_requests = [r for r in ws.ledger.queued_requests("alpha") if r.kind == "publish"]
    assert len(publish_requests) == 1


# ---- 11. resume table: probe_failed/plan_review_failed rerun their station --

def test_continue_after_probe_failed_reruns_probe_then_finishes(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "probe-retry-job"
    facts_md = (
        "# 사실확정\n- 요청 확인: 통과 (출처: request)\n\n"
        "## 외부 대상\n- stripe: https://docs.stripe.com/api\n"
    )
    plan_json = _plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    stub = _RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": facts_md}],
        "probe": [
            {f".gongbang/{job_name}/probe/stripe.log":
             "https://docs.stripe.com/api\nPROBE FAIL\nconnection refused\n"},
            {f".gongbang/{job_name}/probe/stripe.log":
             "https://docs.stripe.com/api\nPROBE OK\n{\"status\": 200}\n"},
        ],
        "plan": [{f".gongbang/{job_name}/plan.json": plan_json}],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    _install_fake_acceptance_and_handoff(monkeypatch, [_FakeAcceptanceResult(passed=True)])
    factory = _processor_factory(stub)

    first = full_orbit.run_full_job(
        ws.config, ws.ledger, _fakes(), "alpha", job_name, "stripe 결제 붙여줘",
        processor_factory=factory,
    )
    assert first.status == "needs_owner"
    assert first.reason == "probe_failed:stripe"
    progress = full_orbit._load_progress(ws.ledger, "alpha", first.attempt_id, job_name)
    assert progress is not None
    assert progress.get("stage") == full_orbit.STAGE_PROBE_RETRY
    assert progress.get("resume_at") == full_orbit.STAGE_PROBE_RETRY
    assert ("plan", "planner") not in stub.calls

    done = full_orbit.continue_full_job(
        ws.config, ws.ledger, _fakes(), "alpha", job_name, processor_factory=factory,
    )

    assert done.status == "done", done
    assert [c for c in stub.calls if c[0] == "probe"] == [
        ("probe", "prober"), ("probe", "prober"),
    ]
    assert (ws.alpha / "src/greeter/extra1.py").exists()


def test_continue_after_plan_review_failed_reruns_plan_and_review(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "plan-retry-job"
    plan_json = _plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    stub = _RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": "- 요청 확인: 통과 (출처: request)\n"}],
        "plan": [
            {f".gongbang/{job_name}/plan.json": plan_json},
            {f".gongbang/{job_name}/plan.json": plan_json},
        ],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    review_calls = {"n": 0}

    def _review_fails_once_then_passes(ctx: StepContext) -> list[StepDef]:
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

    def factory(config, ledger, fakes) -> Processor:
        proc = Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", stub.steps)
        proc.register("review", _review_fails_once_then_passes)
        return proc

    _install_fake_acceptance_and_handoff(monkeypatch, [_FakeAcceptanceResult(passed=True)])

    first = full_orbit.run_full_job(
        ws.config, ws.ledger, _fakes(), "alpha", job_name, "파일 하나 만들어줘",
        processor_factory=factory,
    )
    assert first.status == "needs_owner"
    assert first.reason == "plan_review_failed"
    progress = full_orbit._load_progress(ws.ledger, "alpha", first.attempt_id, job_name)
    assert progress is not None
    assert progress.get("stage") == full_orbit.STAGE_PLAN_REVIEW
    assert ("build", "builder") not in stub.calls

    done = full_orbit.continue_full_job(
        ws.config, ws.ledger, _fakes(), "alpha", job_name, processor_factory=factory,
    )

    assert done.status == "done", done
    assert [c for c in stub.calls if c[0] == "plan"] == [
        ("plan", "planner"), ("plan", "planner"),
    ]
    # probe was never run again -- facts.md names no external target, so
    # the resume starts straight at "plan", not "probe".
    assert ("probe", "prober") not in stub.calls
    assert (ws.alpha / "src/greeter/extra1.py").exists()
