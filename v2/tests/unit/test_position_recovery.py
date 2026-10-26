"""Recovery regressions from goal review 5 (P1 record-intent-first, P5
owner fulfillment). Each test reproduces one interruption or owner-input
case and requires `continue` (engine.position.resume_job) alone to
recover it."""
from __future__ import annotations

from pathlib import Path

import pytest

from engine import faults, full_orbit, full_orbit_runner, position, small_orbit
from engine.processor import Processor
from tests.conftest import Workspace
from tests.unit.test_full_orbit import (
    _FakeAcceptanceResult,
    _fakes,
    _full_orbit_workspace,
    _install_fake_acceptance_and_handoff,
    _plan_json,
    _plan_queue_entry,
    _release_project,
    _review_always_passes_steps,
    _RunStationStub,
)
from tests.unit.test_small_orbit import _JOB as _SMALL_JOB
from tests.unit.test_small_orbit import (
    _FACTS_MD_NO_TARGETS,
    _planning_files,
    _small_plan_json,
)

_FACTS_MD = "- 요청 확인: 통과 (출처: request)\n\n## 외부 대상\n(없음)\n"


def _one_task_stub(job_name: str) -> _RunStationStub:
    plan_json = _plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    return _RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": _FACTS_MD}],
        "plan": [_plan_queue_entry(job_name, plan_json)],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })


def _factory(stub: _RunStationStub):
    def factory(cfg, ledger, fakes) -> Processor:
        proc = Processor(cfg, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", stub.steps)
        proc.register("review", _review_always_passes_steps)
        return proc
    return factory


def _crash_after_open(monkeypatch: pytest.MonkeyPatch, module, roles: tuple[str, ...]) -> None:
    """The first open_job call (from `module`) for `roles` opens the
    attempt for real, then the process dies before anything else."""
    real = module.open_job
    fired = {"done": False}

    def _open(config, ledger, project, job, kind, roles_):
        attempt_id = real(config, ledger, project, job, kind, roles_)
        if tuple(roles_) == roles and not fired["done"]:
            fired["done"] = True
            raise faults.CrashInjected("after open_job")
        return attempt_id

    monkeypatch.setattr(module, "open_job", _open)


_EARLY_ROLES = (full_orbit.FACTS_ROLE, full_orbit.PROBER_ROLE, full_orbit.PLANNER_ROLE,
                full_orbit.DESIGNER_ROLE)


# ---- (a) record the intent before every attempt opens --------------------

def test_a_crash_right_after_the_first_attempt_opens_resumes(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "intent-first-open"
    stub = _one_task_stub(job_name)
    _install_fake_acceptance_and_handoff(monkeypatch, [_FakeAcceptanceResult(passed=True)])
    _crash_after_open(monkeypatch, full_orbit_runner, _EARLY_ROLES)
    with pytest.raises(faults.CrashInjected):
        full_orbit.run_full_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                                "파일 하나 만들어줘", processor_factory=_factory(stub))

    resumed = full_orbit.continue_full_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                                           processor_factory=_factory(stub))

    assert resumed.status == "done", resumed
    # the adopted early attempt plus t1's attempt -- nothing opened twice
    assert len(ws.ledger.attempts_for_job_name("alpha", job_name)) == 2


def test_a_crash_right_after_the_release_attempt_opens_resumes(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    ws = _full_orbit_workspace(workspace)
    config, _master = _release_project(ws, tmp_path)
    job_name = "intent-release-open"
    stub = _one_task_stub(job_name)
    _install_fake_acceptance_and_handoff(monkeypatch, [_FakeAcceptanceResult(passed=True),
                                                       _FakeAcceptanceResult(passed=True)])
    _crash_after_open(monkeypatch, full_orbit_runner, ())
    with pytest.raises(faults.CrashInjected):
        full_orbit.run_full_job(config, ws.ledger, _fakes(), "alpha", job_name,
                                "파일 하나 만들어줘", processor_factory=_factory(stub))
    release_attempt = ws.ledger.attempts_for_job_name("alpha", job_name)[-1].attempt_id

    resumed = full_orbit.continue_full_job(config, ws.ledger, _fakes(), "alpha", job_name,
                                           processor_factory=_factory(stub))

    assert resumed.status == "needs_owner", resumed
    assert resumed.reason == "release_approval"
    assert resumed.attempt_id == release_attempt
    publish = [e for _s, e in ws.ledger.events("alpha", "request.submitted")
               if e.payload.get("kind") == "publish"]
    assert len(publish) == 1


def test_a_crash_right_after_the_small_attempt_opens_resumes(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = _SMALL_JOB  # the job name _planning_files writes under
    stub = _RunStationStub({
        "plan": [_planning_files(_FACTS_MD_NO_TARGETS, _small_plan_json())],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    _install_fake_acceptance_and_handoff(monkeypatch, [_FakeAcceptanceResult(passed=True)])
    _crash_after_open(monkeypatch, full_orbit_runner, (small_orbit.PLANNING_ROLE,))
    with pytest.raises(faults.CrashInjected):
        small_orbit.run_small_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                                  "값 하나만 바꿔줘", processor_factory=_factory(stub))

    resumed = full_orbit.continue_full_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                                           processor_factory=_factory(stub))

    assert resumed.status == "done", resumed
    assert len(ws.ledger.attempts_for_job_name("alpha", job_name)) == 2


def test_a_open_intent_is_a_position_without_an_attempt(workspace: Workspace) -> None:
    ws = _full_orbit_workspace(workspace)
    position.record_position(ws.ledger, "alpha", position.JobPosition(
        job="intent-only", stage=position.STAGE_FACTS, attempt_id=None,
        extra={"roles": list(_EARLY_ROLES), "request": "r", "max_fix_rounds": 2}))
    pos = position.load_position(ws.ledger, "alpha", "intent-only")
    assert pos is not None and pos.attempt_id is None
    assert position.opening_roles(pos) == _EARLY_ROLES


# ---- (b) a task close interrupted mid-way is finished before advancing --

class _CrashOnNth:
    """An ACTIVE_FLOW_FAULTS stand-in: crashes at the `n`th time `name` is
    passed, once."""

    def __init__(self, name: str, n: int) -> None:
        self.name, self.n, self.seen = name, n, 0

    def maybe_crash_before(self, name: str) -> None:
        if name != self.name:
            return
        self.seen += 1
        if self.seen == self.n:
            raise faults.CrashInjected(name)


def _two_task_stub(job_name: str) -> _RunStationStub:
    plan_json = _plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
        {"id": "t2", "title": "둘째 파일", "detail": "", "owns": ["src/greeter/extra2.py"],
         "depends_on": ["t1"]},
    ])
    return _RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": _FACTS_MD}],
        "plan": [_plan_queue_entry(job_name, plan_json)],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"},
                  {"src/greeter/extra2.py": "VALUE_2 = 2\n"}],
    })


def test_b_task_close_crash_between_attempt_finished_and_closed_is_finished(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """t1's close records attempt.finished, then dies before the project
    leaves CLOSING. continue must finish that close, then build t2."""
    ws = _full_orbit_workspace(workspace)
    job_name = "task-close-crash"
    stub = _two_task_stub(job_name)
    _install_fake_acceptance_and_handoff(monkeypatch, [_FakeAcceptanceResult(passed=True)])
    # close #1 is the shared early attempt's, close #2 is t1's
    monkeypatch.setattr(faults, "ACTIVE_FLOW_FAULTS", _CrashOnNth("close.state_closed", 2))
    with pytest.raises(faults.CrashInjected):
        full_orbit.run_full_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                                "두 파일을 만들어줘", processor_factory=_factory(stub))
    monkeypatch.setattr(faults, "ACTIVE_FLOW_FAULTS", None)
    assert ws.ledger.project("alpha").state == "CLOSING"

    resumed = full_orbit.continue_full_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                                           processor_factory=_factory(stub))

    assert resumed.status == "done", resumed
    assert ws.ledger.project("alpha").state == "CLOSED"
    assert (ws.alpha / "src/greeter/extra2.py").read_text(encoding="utf-8") == "VALUE_2 = 2\n"
    assert len(ws.ledger.events("alpha", "commit.recorded")) == 2


# ---- (c) one canonical task-stage spec, replayed by fix and recovery ----

def test_c_crash_after_fix_finished_replays_the_fix_requests_exactly(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fixup.fix submits the repaired task's run_station, records
    fix.finished, then dies before running the stages. continue must
    replay exactly the fix's own stage spec: the same run_station
    request (one worker dispatch) with the task's own owns."""
    from engine import fixup, orbit
    from tests.unit import test_position as tp

    ws = _full_orbit_workspace(workspace)
    job_name = "fix-spec-replay"
    _first, factory, _reviews = tp._failed_task_job(ws, monkeypatch, ws.config, job_name)
    real_continue = orbit.continue_job
    fired = {"done": False}

    def _continue_crashing_once(*args, **kwargs):
        if not fired["done"]:
            fired["done"] = True
            raise faults.CrashInjected("after fix.finished")
        return real_continue(*args, **kwargs)

    monkeypatch.setattr(fixup.orbit, "continue_job", _continue_crashing_once)
    with pytest.raises(faults.CrashInjected):
        fixup.fix(ws.config, ws.ledger, "alpha", "", lambda _a: _fakes(),
                  processor_factory=factory)
    monkeypatch.setattr(fixup.orbit, "continue_job", real_continue)
    fix_attempt = ws.ledger.attempts_for_job_name("alpha", job_name)[-1].attempt_id

    resumed = full_orbit.continue_full_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                                           processor_factory=factory)

    assert resumed.status == "done", resumed
    run_stations = [e.payload for _s, e in ws.ledger.events("alpha", "request.submitted")
                    if e.attempt_id == fix_attempt and e.payload.get("kind") == "run_station"]
    assert len(run_stations) == 1, run_stations
    assert run_stations[0]["payload"]["owns"] == ["src/greeter/extra1.py"]
    assert "Original request" in run_stations[0]["payload"]["task"]


# ---- (d) every resume validates the plan before dispatch ----------------

def test_d_design_station_resume_refuses_a_plan_edited_after_review(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.test_full_orbit import (
        _EARLY_INSTALL_REQUEST,
        _approve_early_install,
        _station_stub_with_one_owner_request,
    )

    ws = _full_orbit_workspace(workspace)
    job_name = "design-resume-plan-edit"
    plan_json = _plan_json(ui=True, tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    queues = {
        "facts": [{f".gongbang/{job_name}/facts.md": _FACTS_MD}],
        "plan": [_plan_queue_entry(job_name, plan_json)],
        "design": [{f".gongbang/{job_name}/design/design.md": "디자인\n"}],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    }
    _stub, steps_fn = _station_stub_with_one_owner_request(queues, "design",
                                                           _EARLY_INSTALL_REQUEST)

    def factory(cfg, ledger, fakes) -> Processor:
        proc = Processor(cfg, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", steps_fn)
        proc.register("review", _review_always_passes_steps)
        return proc

    _install_fake_acceptance_and_handoff(monkeypatch, [_FakeAcceptanceResult(passed=True)])
    first = full_orbit.run_full_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                                    "화면 하나 만들어줘", processor_factory=factory)
    assert first.reason == "owner_request"

    # While the design station waits on the owner, plan.json is changed
    # after its review (ui off, the requirement dropped).
    edited = _plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ]).replace('"R1"', '"R9"')
    (ws.alpha / f".gongbang/{job_name}/plan.json").write_text(edited, encoding="utf-8")
    _approve_early_install(ws, first.attempt_id)

    resumed = full_orbit.continue_full_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                                           processor_factory=factory)

    assert resumed.status == "needs_owner", resumed
    assert resumed.reason is not None and resumed.reason.startswith("resumed_plan_invalid")


# ---- (e) an owner's repair of facts.md is the input the retry checks ----

def test_e_owner_repaired_facts_reach_the_probe_retry_gate(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """facts.md names an external target malformed, so the probe station
    stops for the owner. The owner fixes facts.md in the repository;
    continue must check that repaired file, not the committed old one."""
    ws = _full_orbit_workspace(workspace)
    job_name = "facts-repair-retry"
    plan_json = _plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    malformed = "- 요청 확인: 통과 (출처: request)\n\n## 외부 대상\n- stripe 문서 없음\n"
    stub = _RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": malformed}],
        "plan": [_plan_queue_entry(job_name, plan_json)],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    _install_fake_acceptance_and_handoff(monkeypatch, [_FakeAcceptanceResult(passed=True)])
    first = full_orbit.run_full_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                                    "파일 하나 만들어줘", processor_factory=_factory(stub))
    assert first.status == "needs_owner", first
    assert first.reason is not None and first.reason.startswith("probe_targets_invalid")

    (ws.alpha / f".gongbang/{job_name}/facts.md").write_text(_FACTS_MD, encoding="utf-8")
    resumed = full_orbit.continue_full_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                                           processor_factory=_factory(stub))

    assert resumed.status == "done", resumed
    assert (ws.alpha / f".gongbang/{job_name}/facts.md").read_text(encoding="utf-8") == _FACTS_MD


# ---- (f) owner fulfillment is bound to the proposal ----------------------

_INSTALL = {"kind": "install", "manifest": "package.json",
            "change": {"dependencies": {"left-pad": "^1.0.0"}}}


def _proposal_stop(ws: Workspace, job_name: str) -> str:
    """A task attempt whose station proposed _INSTALL, stopped for it."""
    from engine.ledger import Event

    attempt_id = full_orbit.open_job(ws.config, ws.ledger, "alpha", job_name, "full", ())
    ws.ledger.append(Event(
        project="alpha", kind="station.finished", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"request_id": "R-station", "station": "build", "role": "builder",
                 "ok": True, "requests": [_INSTALL]},
    ))
    position.record_position(ws.ledger, "alpha", position.JobPosition(
        job=job_name, stage=position.STAGE_TASK_OWNER, attempt_id=attempt_id, task_id="t1",
        pending_requests=(_INSTALL,), stop_reason="owner_request"))
    return attempt_id


def _install_recorded(ws: Workspace, attempt_id: str, kind: str = "install") -> None:
    from engine.ledger import Event

    ws.ledger.append(Event(
        project="alpha", kind="install.recorded", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"request_id": f"R-{kind}", "kind": kind, "manifest": "package.json",
                 "change": _INSTALL["change"]},
    ))


def _write_manifest(ws: Workspace, deps: dict) -> None:
    import json

    (ws.alpha / "package.json").write_text(json.dumps({"dependencies": deps}), encoding="utf-8")


def test_f_an_install_recorded_before_the_proposal_does_not_fulfil_it(
    workspace: Workspace,
) -> None:
    ws = _full_orbit_workspace(workspace)
    earlier = full_orbit.open_job(ws.config, ws.ledger, "alpha", "older-job", "full", ())
    _install_recorded(ws, earlier)
    from engine import state

    state.transition(ws.ledger, "alpha", "close_job", "owner")
    state.transition(ws.ledger, "alpha", "finalized", "owner")
    _write_manifest(ws, {"left-pad": "^1.0.0"})
    _proposal_stop(ws, "historical")

    assert position.pending_owner_requests(ws.ledger, "alpha", "historical",
                                           repo=ws.alpha) == (_INSTALL,)


def test_f_an_install_of_another_kind_does_not_fulfil_it(workspace: Workspace) -> None:
    ws = _full_orbit_workspace(workspace)
    attempt_id = _proposal_stop(ws, "other-kind")
    _write_manifest(ws, {"left-pad": "^1.0.0"})
    _install_recorded(ws, attempt_id, kind="scripts_change")

    assert position.pending_owner_requests(ws.ledger, "alpha", "other-kind",
                                           repo=ws.alpha) == (_INSTALL,)


def test_f_fulfilment_needs_the_current_manifest_to_hold_the_change(
    workspace: Workspace,
) -> None:
    ws = _full_orbit_workspace(workspace)
    attempt_id = _proposal_stop(ws, "current-manifest")
    _install_recorded(ws, attempt_id)
    _write_manifest(ws, {})  # reverted since the install
    assert position.pending_owner_requests(ws.ledger, "alpha", "current-manifest",
                                           repo=ws.alpha) == (_INSTALL,)

    _write_manifest(ws, {"left-pad": "^1.0.0"})
    assert position.pending_owner_requests(ws.ledger, "alpha", "current-manifest",
                                           repo=ws.alpha) == ()


# ---- (g) report.md ends with the final section --------------------------

def _report_tail(ws: Workspace, job_name: str) -> str:
    text = (ws.alpha / f".gongbang/{job_name}/report.md").read_text(encoding="utf-8")
    return text[text.rfind("## 최종 결과"):] if "## 최종 결과" in text else ""


def _assert_final_section(ws: Workspace, job_name: str) -> str:
    tail = _report_tail(ws, job_name)
    assert tail.startswith("## 최종 결과"), tail
    assert "보안 점검:" in tail and "배포 결정:" in tail and "증거 완전성:" in tail
    text = (ws.alpha / f".gongbang/{job_name}/report.md").read_text(encoding="utf-8")
    assert text.count("## 최종 결과") == 1
    return tail


def test_g_report_ends_with_the_final_section_after_ship(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """`gb2 ship` = engine.release.approve_and_run then finalize_release."""
    from engine import release as release_mod
    from tests.unit.test_full_orbit import _release_stopped_job

    job_name = "final-after-ship"
    config, ws, outcome, request_id = _release_stopped_job(workspace, monkeypatch, tmp_path,
                                                           job_name)
    assert release_mod.approve_and_run(config, ws.ledger, _fakes(), "alpha", request_id,
                                       f"승인 {request_id}")["status"] == "done"
    done = full_orbit.finalize_release(config, ws.ledger, _fakes(), "alpha", outcome.attempt_id,
                                       job_name, request_id, "")
    assert done.status == "done", done
    tail = _assert_final_section(ws, job_name)
    assert "배포 결정: 배포됨" in tail or "shipped" in tail
    # resuming again rewrites the same one section
    full_orbit.finalize_release(config, ws.ledger, _fakes(), "alpha", outcome.attempt_id,
                                job_name, request_id, "")
    _assert_final_section(ws, job_name)


def test_g_report_ends_with_the_final_section_after_skip_ship(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """`gb2 skip-ship` = cancel_pending_publish then finalize_release."""
    from engine import release as release_mod
    from tests.unit.test_full_orbit import _release_stopped_job

    job_name = "final-after-skip"
    config, ws, outcome, request_id = _release_stopped_job(workspace, monkeypatch, tmp_path,
                                                           job_name)
    release_mod.cancel_pending_publish(ws.ledger, "alpha", outcome.attempt_id, "skip_ship")
    done = full_orbit.finalize_release(config, ws.ledger, _fakes(), "alpha", outcome.attempt_id,
                                       job_name, request_id, "")
    assert done.status == "done", done
    _assert_final_section(ws, job_name)


def test_g_report_ends_with_the_final_section_without_a_release(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "final-not-configured"
    stub = _one_task_stub(job_name)
    _install_fake_acceptance_and_handoff(monkeypatch, [_FakeAcceptanceResult(passed=True)])
    done = full_orbit.run_full_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                                   "파일 하나 만들어줘", processor_factory=_factory(stub))
    assert done.status == "done", done
    _assert_final_section(ws, job_name)
    assert ws.ledger.project("alpha").state == "CLOSED"
    from engine.steps.open_job import dirty_paths

    assert f".gongbang/{job_name}/report.md" not in dirty_paths(ws.alpha, ())


# ---- final round (1) a second repair's unfinished journal wins ----------

def test_final_1_second_repair_crashing_before_its_transfer_is_resumed(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repair A->B finishes (and fails review again); repair B->C opens C
    and dies before recording the transfer. continue must finish B->C on
    C, not follow A's finished repair."""
    from engine import fixup
    from tests.unit import test_position as tp

    ws = _full_orbit_workspace(workspace)
    job_name = "second-repair"
    _first, factory, _reviews = tp._failed_task_job(ws, monkeypatch, ws.config, job_name,
                                                   t1_failures=2)
    first_fix = fixup.fix(ws.config, ws.ledger, "alpha", "", lambda _a: _fakes(),
                          processor_factory=factory)
    assert first_fix.outcome.status == "failed", first_fix.outcome
    tp._crash_fix_at(monkeypatch, ws, ws.config, "fixup.fix.opened_new", factory)
    attempt_c = ws.ledger.attempts_for_job_name("alpha", job_name)[-1].attempt_id

    pos = position.load_position(ws.ledger, "alpha", job_name)
    assert pos is not None and pos.fix is not None
    assert pos.fix.old_attempt_id == first_fix.new_attempt_id and not pos.fix.finished

    resumed = full_orbit.continue_full_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                                           processor_factory=factory)

    assert resumed.status == "done", resumed
    attempts = [a.attempt_id for a in ws.ledger.attempts_for_job_name("alpha", job_name)]
    assert attempt_c in attempts and len(attempts) == 5, attempts  # early, t1, B, C, t2
    assert len(ws.ledger.events("alpha", "fix.started")) == 2


# ---- final round (2) a UI task's repair runs under the task config ------

def test_final_2_ui_task_repair_keeps_the_screens_allowance(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from engine import fixup
    from engine.ledger import Event
    from tests.unit import test_position as tp

    ws = _full_orbit_workspace(workspace)
    job_name = "ui-repair"
    plan_json = _plan_json(ui=True, tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    stub = _RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": _FACTS_MD}],
        "plan": [_plan_queue_entry(job_name, plan_json)],
        "design": [{f".gongbang/{job_name}/design/design.md": "디자인\n"}],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"},
                  {"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    review_steps = tp._first_review_of_t1_fails([])

    def factory(cfg, ledger, fakes) -> Processor:
        proc = Processor(cfg, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", stub.steps)
        proc.register("review", review_steps)
        return proc

    _install_fake_acceptance_and_handoff(monkeypatch, [_FakeAcceptanceResult(passed=True)])
    first = full_orbit.run_full_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                                    "화면 하나 만들어줘", processor_factory=factory)
    assert first.reason == "design_approval", first
    ws.ledger.append(Event(project="alpha", kind="design.approved", actor_kind="owner",
                           actor_id="owner", attempt_id=None, payload={"job": job_name}))
    failed = full_orbit.continue_full_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                                          processor_factory=factory)
    assert failed.status == "failed", failed

    result = fixup.fix(ws.config, ws.ledger, "alpha", "", lambda _a: _fakes(),
                       processor_factory=factory)

    assert result.outcome.status == "done", result.outcome


# ---- final round (3) a publish started without a receipt is resumed -----

def test_final_3_continue_resumes_a_publish_journaled_started_without_a_receipt(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """`gb2 ship` approved and ran the publish request, which died right
    after call_publisher was journaled started -- before the publisher
    ran, so no receipt exists. continue must resume that same request
    (with its approval) to the end: one publish, shipped, closed."""
    from engine import release as release_mod
    from engine.faults import Faults
    from engine.processor import Processor as RealProcessor
    from tests.unit.test_full_orbit import _release_stopped_job

    job_name = "publish-started-no-receipt"
    config, ws, outcome, request_id = _release_stopped_job(workspace, monkeypatch, tmp_path,
                                                           job_name)

    class _CrashingProcessor(RealProcessor):
        def __init__(self, config_, ledger_, fakes_, faults_=None) -> None:
            super().__init__(config_, ledger_, fakes_, Faults(crash_before={"call_publisher"}))

    monkeypatch.setattr(release_mod, "Processor", _CrashingProcessor)
    fakes = _fakes()
    with pytest.raises(faults.CrashInjected):
        release_mod.approve_and_run(config, ws.ledger, fakes, "alpha", request_id,
                                    f"승인 {request_id}")
    monkeypatch.setattr(release_mod, "Processor", RealProcessor)
    assert ws.ledger.request(request_id).status == "running"
    assert fakes.publisher.lookup(request_id) is None

    resumed = full_orbit.continue_full_job(config, ws.ledger, fakes, "alpha", job_name)

    assert resumed.status == "done", resumed
    assert ws.ledger.request(request_id).status == "done"
    assert ws.ledger.project("alpha").state == "CLOSED"
    decisions = [e.payload["decision"] for _s, e in
                 ws.ledger.events("alpha", "full_orbit.release_decision")
                 if e.payload.get("job") == job_name]
    assert decisions == ["shipped"]
    publish = [e for _s, e in ws.ledger.events("alpha", "request.submitted")
               if e.payload.get("kind") == "publish"]
    assert len(publish) == 1


# ---- final round (4) a repeated identical proposal gets its own approval -

def test_final_4_a_repeated_identical_proposal_can_be_fulfilled_again(
    workspace: Workspace,
) -> None:
    from dataclasses import replace

    from engine import owner
    from engine.fakes.installer import FakeInstaller
    from engine.fakes.publisher import FakePublisher
    from engine.ledger import Event
    from engine.processor import Fakes

    request = {"kind": "install", "manifest": "package.json",
               "change": {"dependencies": {"zod": "3.0.0"}}}
    project = replace(workspace.config.projects["alpha"], manifest="package.json")
    config = replace(workspace.config, projects={**workspace.config.projects, "alpha": project})
    attempt_id = full_orbit.open_job(config, workspace.ledger, "alpha", "repeat", "full",
                                     ("builder",))
    proc = Processor(config, workspace.ledger, Fakes(FakePublisher(), FakeInstaller()))
    proc.register_defaults()
    request_ids = []
    for n in (1, 2):
        workspace.ledger.append(Event(
            project="alpha", kind="station.finished", actor_kind="processor",
            actor_id="processor", attempt_id=attempt_id,
            payload={"request_id": f"R-station-{n}", "station": "build", "role": "builder",
                     "ok": True, "requests": [request]}))
        stop = position.JobPosition(job="repeat", stage=position.STAGE_TASK_OWNER,
                                    attempt_id=attempt_id, task_id="t1",
                                    pending_requests=(request,), stop_reason="owner_request")
        position.record_position(workspace.ledger, "alpha", stop)
        assert position.pending_owner_requests(workspace.ledger, "alpha", "repeat",
                                               repo=workspace.alpha) == (request,)
        request_ids.append(owner.approve_pending(
            config, workspace.ledger, "alpha", attempt_id, request, confirm=True,
            confirm_source="tty",
            proposal=position.proposal_id(workspace.ledger, "alpha", stop, request)))
        proc.run_once("alpha")
        assert workspace.ledger.request(request_ids[-1]).status == "done"
        assert position.pending_owner_requests(workspace.ledger, "alpha", "repeat",
                                               repo=workspace.alpha) == (), n
    assert request_ids[0] != request_ids[1]


# ---- every station, plan review and task review included, is gated -----

def _review_required(config):
    from dataclasses import replace as _replace

    from engine.context import ContextConfig

    return _replace(config, context=ContextConfig(
        station_skills={"review": ("필수-검토-스킬",)},
        required_skills={"review": ("필수-검토-스킬",)}))


def test_gate_a_missing_review_skill_stops_the_full_job_before_its_plan_review(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "review-gate-full"
    stub = _one_task_stub(job_name)
    reviews: list[str] = []

    def factory(cfg, ledger, fakes) -> Processor:
        proc = Processor(cfg, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", stub.steps)

        def _review(ctx):
            reviews.append("called")
            return _review_always_passes_steps(ctx)
        proc.register("review", _review)
        return proc

    _install_fake_acceptance_and_handoff(monkeypatch, [_FakeAcceptanceResult(passed=True)])
    gated = _review_required(ws.config)
    outcome = full_orbit.run_full_job(gated, ws.ledger, _fakes(), "alpha", job_name,
                                      "파일 하나 만들어줘", processor_factory=factory)

    assert outcome.status == "needs_owner", outcome
    assert outcome.reason is not None and "필수-검토-스킬" in outcome.reason
    assert ("plan", "planner") not in stub.calls
    assert reviews == []

    done = full_orbit.continue_full_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                                        processor_factory=factory)
    assert done.status == "done", done


def test_gate_a_missing_review_skill_stops_the_small_job_before_any_worker(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    stub = _RunStationStub({
        "plan": [_planning_files(_FACTS_MD_NO_TARGETS, _small_plan_json())],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    _install_fake_acceptance_and_handoff(monkeypatch, [_FakeAcceptanceResult(passed=True)])

    outcome = small_orbit.run_small_job(_review_required(ws.config), ws.ledger, _fakes(),
                                        "alpha", _SMALL_JOB, "값 하나만 바꿔줘",
                                        processor_factory=_factory(stub))

    assert outcome.status == "needs_owner", outcome
    assert outcome.reason is not None and "필수-검토-스킬" in outcome.reason
    assert stub.calls == []


def test_gate_a_task_gate_includes_the_task_review(workspace: Workspace) -> None:
    from engine.full_orbit_tasks import build_task_gate
    from engine.plan import Plan

    ws = _full_orbit_workspace(workspace)
    plan = Plan(goal="g", ui=False, scenes=(), tasks=(), acceptance=("a",))
    gate = build_task_gate(_review_required(ws.config), ws.ledger, "alpha", plan, "작업")
    assert gate.unmet is not None and "필수-검토-스킬" in gate.unmet
    assert build_task_gate(ws.config, ws.ledger, "alpha", plan, "작업").unmet is None
