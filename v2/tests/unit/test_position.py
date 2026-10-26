"""engine.position: one recorded position per transition, one resume path.

Regression tests:
- a crash right after a fix attempt opens resumes that fix without
  opening another attempt;
- a release-configured job whose failed task is repaired through a
  crash-and-continue makes exactly one publish request, at the end;
- on a ledger written before `job.position` existed, a needs_owner
  progress stop followed by a later cursor resumes from the cursor."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from engine import faults, fixup, full_orbit, full_orbit_runner, position
from engine.ledger import Event
from engine.processor import Processor, StepContext, StepDef, StepFailed
from tests.conftest import Workspace
from tests.unit.test_full_orbit import (
    _FakeAcceptanceResult,
    _fakes,
    _full_orbit_workspace,
    _install_fake_acceptance_and_handoff,
    _plan_json,
    _plan_queue_entry,
    _release_project,
    _RunStationStub,
)

_ENGINE = Path(__file__).resolve().parents[2] / "engine" / "application"
_FACTS_MD = "- 요청 확인: 통과 (출처: request)\n\n## 외부 대상\n(없음)\n"


def _fix_opened(ledger, project: str = "alpha") -> list[str]:
    opened: list[str] = []
    for _seq, event in ledger.events(project, "job.position"):
        fix = event.payload.get("fix")
        if isinstance(fix, dict) and isinstance(fix.get("new_attempt_id"), str):
            opened.append(fix["new_attempt_id"])
    return opened


def _publish_request_ids(ledger, project: str = "alpha") -> list[str]:
    return [str(event.payload["request_id"])
            for _seq, event in ledger.events(project, "request.submitted")
            if event.payload.get("kind") == "publish"]


def _first_review_of_t1_fails(review_calls: list[str], failures: int = 1):
    """A review stub: the first `failures` reviews naming task t1 fail,
    every other review passes."""
    from engine import evidence

    def _review_steps(ctx: StepContext) -> list[StepDef]:
        task_text = str(ctx.request.payload.get("task", ""))

        def _run(ctx: StepContext) -> dict[str, object]:
            review_calls.append(task_text)
            if "첫 파일" in task_text and sum("첫 파일" in t for t in review_calls) <= failures:
                raise StepFailed("review_rejected")
            project_config = ctx.config.projects[ctx.project]
            fp = evidence.fingerprint(project_config.repo, project_config.excludes)
            evidence.record(ctx.ledger, ctx.project, ctx.attempt_id, "review", fp,
                            payload={"passed": True, "request_id": ctx.request.request_id,
                                     "findings": []})
            return {}

        def _reconcile(ctx: StepContext) -> bool:
            return any(row.payload.get("request_id") == ctx.request.request_id
                       for row in ctx.ledger.evidence(ctx.attempt_id, "review"))

        return [StepDef("record_review", _run, _reconcile)]

    return _review_steps


def _failed_task_job(ws: Workspace, monkeypatch: pytest.MonkeyPatch, config, job_name: str,
                     *, t1_failures: int = 1):
    """A two-task full job whose task t1 failed review (an ordinary
    "failed" outcome; its first `t1_failures` reviews fail): returns
    (first outcome, processor factory, review call log)."""
    plan_json = _plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
        {"id": "t2", "title": "둘째 파일", "detail": "", "owns": ["src/greeter/extra2.py"],
         "depends_on": ["t1"]},
    ])
    stub = _RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": _FACTS_MD}],
        "plan": [_plan_queue_entry(job_name, plan_json)],
        "build": [
            *({"src/greeter/extra1.py": "VALUE_1 = 1\n"} for _ in range(t1_failures + 1)),
            {"src/greeter/extra2.py": "VALUE_2 = 2\n"},
        ],
    })
    review_calls: list[str] = []
    review_steps = _first_review_of_t1_fails(review_calls, t1_failures)

    def factory(cfg, ledger, fakes) -> Processor:
        proc = Processor(cfg, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", stub.steps)
        proc.register("review", review_steps)
        return proc

    _install_fake_acceptance_and_handoff(monkeypatch, [_FakeAcceptanceResult(passed=True)])
    first = full_orbit.run_full_job(config, ws.ledger, _fakes(), "alpha", job_name,
                                    "두 파일을 만들어줘", processor_factory=factory)
    assert first.status == "failed", first
    return first, factory, review_calls


def _crash_fix_at(monkeypatch: pytest.MonkeyPatch, ws: Workspace, config, boundary: str,
                  factory) -> None:
    monkeypatch.setattr(faults, "ACTIVE_FLOW_FAULTS", faults.Faults(crash_before={boundary}))
    with pytest.raises(faults.CrashInjected):
        fixup.fix(config, ws.ledger, "alpha", "", lambda _a: _fakes(), processor_factory=factory)
    monkeypatch.setattr(faults, "ACTIVE_FLOW_FAULTS", None)


# ---------------------------------------------------------------------------
# (1) crash right after the fix attempt opens
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("boundary", ["fixup.fix.restored", "fixup.fix.opened_new"])
def test_crash_around_the_fix_attempt_opening_resumes_without_another_attempt(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    """`fixup.fix.restored`: the fix attempt is open and recorded, the
    archive not yet restored. `fixup.fix.opened_new`: open_job already
    ran, the opening not yet recorded. Either way `continue` finishes
    the same fix on the same replacement attempt and the job completes."""
    ws = _full_orbit_workspace(workspace)
    job_name = f"fix-crash-{boundary.rsplit('.', 1)[-1]}"
    first, factory, review_calls = _failed_task_job(ws, monkeypatch, ws.config, job_name)
    old_attempt_id = first.attempt_id

    _crash_fix_at(monkeypatch, ws, ws.config, boundary, factory)
    replacement = ws.ledger.attempts_for_job_name("alpha", job_name)[-1].attempt_id
    assert replacement != old_attempt_id
    assert ws.ledger.attempt(replacement).status == "active"

    pos = position.load_position(ws.ledger, "alpha", job_name)
    assert pos is not None and pos.fix is not None
    assert pos.fix.old_attempt_id == old_attempt_id and pos.fix.finished is False

    resumed = full_orbit.continue_full_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                                           processor_factory=factory)

    assert resumed.status == "done", resumed
    attempts = [a.attempt_id for a in ws.ledger.attempts_for_job_name("alpha", job_name)]
    # early stations, t1 (failed), the ONE fix replacement, t2 -- nothing else
    assert len(attempts) == 4, attempts
    assert attempts[2] == replacement
    assert _fix_opened(ws.ledger) == [replacement]
    assert len(ws.ledger.events("alpha", "fix.started")) == 1
    assert len(ws.ledger.events("alpha", "fix.finished")) == 1
    # the fix's composed task (not the plain t1 text) is what passed review
    assert any("Original request" in t for t in review_calls)
    assert (ws.alpha / "src/greeter/extra2.py").read_text(encoding="utf-8") == "VALUE_2 = 2\n"


# ---------------------------------------------------------------------------
# (2) release configured + a recovered fix: one publish request, at the end
# ---------------------------------------------------------------------------

def test_release_job_with_a_recovered_fix_makes_one_publish_request_at_the_end(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    ws = _full_orbit_workspace(workspace)
    config, _master = _release_project(ws, tmp_path)
    job_name = "release-fix-recovered"
    first, factory, _reviews = _failed_task_job(ws, monkeypatch, config, job_name)

    # The fix is interrupted, then finished by `continue` -- with the
    # project's release config passed in unchanged.
    _crash_fix_at(monkeypatch, ws, config, "fixup.fix.restored", factory)
    assert _publish_request_ids(ws.ledger) == []

    outcome = full_orbit.continue_full_job(config, ws.ledger, _fakes(), "alpha", job_name,
                                           processor_factory=factory)

    assert outcome.status == "needs_owner", outcome
    assert outcome.reason == "release_approval"
    publish_ids = _publish_request_ids(ws.ledger)
    assert len(publish_ids) == 1
    # bound to the job's final release attempt, after every task
    assert ws.ledger.request(publish_ids[0]).attempt_id == outcome.attempt_id
    assert (ws.alpha / "src/greeter/extra2.py").exists()
    pos = position.load_position(ws.ledger, "alpha", job_name)
    assert pos is not None and pos.stage == position.STAGE_RELEASE_APPROVAL


def test_fixup_strips_release_itself_for_a_full_job_fix(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Without any crash: the repaired task commits without stopping for a
    per-task release, and continuing the job makes the one request."""
    ws = _full_orbit_workspace(workspace)
    config, _master = _release_project(ws, tmp_path)
    job_name = "release-fix-direct"
    _first, factory, _reviews = _failed_task_job(ws, monkeypatch, config, job_name)

    result = fixup.fix(config, ws.ledger, "alpha", "", lambda _a: _fakes(),
                       processor_factory=factory)
    assert result.full_job is True
    assert result.outcome.status == "done", result.outcome
    assert _publish_request_ids(ws.ledger) == []

    outcome = position.resume_after_fix(config, ws.ledger, _fakes(), "alpha", result,
                                        processor_factory=factory)
    assert outcome is not None and outcome.reason == "release_approval"
    assert len(_publish_request_ids(ws.ledger)) == 1


# ---------------------------------------------------------------------------
# (3) legacy ledgers: the newest of cursor/progress wins
# ---------------------------------------------------------------------------

def _legacy(ledger, kind: str, attempt_id: str, payload: dict) -> None:
    ledger.append(Event(project="alpha", kind=kind, actor_kind="processor",
                        actor_id="processor", attempt_id=attempt_id, payload=payload))


def test_legacy_progress_stop_followed_by_a_later_cursor_resumes_from_the_cursor(
    workspace: Workspace,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "legacy-newest-wins"
    attempt_id = full_orbit.open_job(ws.config, ws.ledger, "alpha", job_name, "full", ())
    _legacy(ws.ledger, "full_orbit.progress", attempt_id, {
        "job": job_name, "attempt_id": attempt_id, "stage": position.STAGE_EARLY_STATION,
        "resume_at": position.STAGE_EARLY_STATION, "station": "facts",
        "stop_reason": "owner_request", "pending_requests": [], "max_fix_rounds": 2,
    })
    _legacy(ws.ledger, "full_orbit.cursor", attempt_id, {
        "job": job_name, "stage": "probe", "task_id": None, "next_action": "run_station",
        "attempt_id": attempt_id, "request": "요청", "max_fix_rounds": 3,
    })

    pos = position.load_position(ws.ledger, "alpha", job_name)

    assert pos is not None
    assert pos.stage == "probe"
    assert pos.attempt_id == attempt_id
    assert pos.stop_reason is None
    assert pos.get("request") == "요청"
    assert pos.max_fix_rounds == 3
    assert full_orbit.RESUME_TABLE[pos.stage].__name__ == "_resume_reopened_station"


def test_legacy_task_owner_progress_reads_as_a_task_stop(workspace: Workspace) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "legacy-task-owner"
    attempt_id = full_orbit.open_job(ws.config, ws.ledger, "alpha", job_name, "full", ())
    install = {"kind": "install", "manifest": "package.json",
               "change": {"dependencies": {"left-pad": "^1.0.0"}}}
    _legacy(ws.ledger, "full_orbit.cursor", attempt_id, {
        "job": job_name, "stage": "task", "task_id": "t1", "next_action": "run_station",
        "attempt_id": attempt_id, "task_text": "x", "remaining_task_ids": ["t1", "t2"],
    })
    _legacy(ws.ledger, "full_orbit.progress", attempt_id, {
        "job": job_name, "attempt_id": attempt_id, "stage": position.STAGE_TASK_OWNER,
        "resume_at": position.STAGE_TASK_OWNER, "current_task_id": "t1",
        "current_task_text": "with context", "current_base_task_text": "base",
        "remaining_task_ids": ["t1", "t2"], "stop_reason": None, "fixing": False,
        "pending_requests": [install], "max_fix_rounds": 2,
    })

    pos = position.load_position(ws.ledger, "alpha", job_name)

    assert pos is not None
    assert pos.stage == position.STAGE_TASK_OWNER
    assert pos.task_id == "t1"
    assert pos.remaining_task_ids == ("t1", "t2")
    assert pos.get("task_text") == "with context"
    assert pos.get("base_task_text") == "base"
    assert position.pending_owner_requests(ws.ledger, "alpha", job_name) == (install,)

    _legacy(ws.ledger, "install.recorded", attempt_id, {
        "request_id": "r1", "kind": "install", "manifest": "./package.json",
        "change": install["change"]})
    assert position.pending_owner_requests(ws.ledger, "alpha", job_name) == ()


def test_legacy_fix_opened_without_cursor_transfer_moves_to_the_replacement(
    workspace: Workspace,
) -> None:
    """An old ledger that crashed between `fix.opened_new` and the cursor
    transfer: the position is read as already on the replacement, with
    the fix still unfinished."""
    ws = _full_orbit_workspace(workspace)
    job_name = "legacy-fix-opened"
    old = full_orbit.open_job(ws.config, ws.ledger, "alpha", job_name, "full", ())
    _legacy(ws.ledger, "full_orbit.cursor", old, {
        "job": job_name, "stage": "task", "task_id": "t1", "next_action": "run_station",
        "attempt_id": old, "remaining_task_ids": ["t1"],
    })
    _legacy(ws.ledger, "fix.started", old, {"job_name": job_name, "old_attempt_id": old})
    _legacy(ws.ledger, "fix.opened_new", old, {"new_attempt_id": "NEW#2"})

    pos = position.load_position(ws.ledger, "alpha", job_name)

    assert pos is not None
    assert pos.stage == position.STAGE_TASK
    assert pos.attempt_id == "NEW#2"
    assert pos.fix == position.FixState(old, "NEW#2", finished=False)

    _legacy(ws.ledger, "fix.finished", old, {})
    finished = position.load_position(ws.ledger, "alpha", job_name)
    assert finished is not None and finished.fix is not None and finished.fix.finished


def test_new_position_round_trips(workspace: Workspace) -> None:
    ws = _full_orbit_workspace(workspace)
    pos = position.JobPosition(
        job="round-trip", stage=position.STAGE_TASK_OWNER, attempt_id="A#1", task_id="t1",
        remaining_task_ids=("t1", "t2"), fixing=True,
        fix=position.FixState("A#0", "A#1"), pending_requests=({"kind": "install"},),
        stop_reason="owner_request", extra={"task_text": "x", "max_fix_rounds": 2},
    )
    position.record_position(ws.ledger, "alpha", pos)

    assert position.load_position(ws.ledger, "alpha", "round-trip") == pos


def test_resume_job_refuses_a_single_task_position(workspace: Workspace) -> None:
    ws = _full_orbit_workspace(workspace)
    attempt_id = full_orbit.open_job(ws.config, ws.ledger, "alpha", "single", "full", ())
    position.record_position(ws.ledger, "alpha", position.JobPosition(
        job="single", stage=position.STAGE_SINGLE, attempt_id=attempt_id,
        fix=position.FixState("old", attempt_id)))
    _legacy(ws.ledger, "fix.started", "old", {"job_name": "single", "old_attempt_id": "old"})
    _legacy(ws.ledger, "fix.finished", "old", {})

    with pytest.raises(full_orbit.FullOrbitError, match="no pending stop"):
        position.resume_job(ws.config, ws.ledger, _fakes(), "alpha", "single")


# ---------------------------------------------------------------------------
# a crash between a task's opening record and its position on the attempt
# ---------------------------------------------------------------------------

def _two_task_job_crashing_at(ws: Workspace, monkeypatch: pytest.MonkeyPatch,
                              job_name: str, boundary: str, *, context_gap: bool = False):
    """A two-task job run with the named flow boundary armed: returns
    (processor factory, run_station stub) after the crash. `boundary`
    may be a name no code reaches, when the caller injects its own
    crash."""
    from dataclasses import replace as _replace

    from engine.context import ContextConfig

    plan_json = _plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
        {"id": "t2", "title": "둘째 파일", "detail": "", "owns": ["src/greeter/extra2.py"],
         "depends_on": ["t1"]},
    ])
    stub = _RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": _FACTS_MD}],
        "plan": [_plan_queue_entry(job_name, plan_json)],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"},
                  {"src/greeter/extra2.py": "VALUE_2 = 2\n"}],
    })
    from tests.unit.test_full_orbit import _review_always_passes_steps

    def factory(cfg, ledger, fakes) -> Processor:
        proc = Processor(cfg, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", stub.steps)
        proc.register("review", _review_always_passes_steps)
        return proc

    config = ws.config
    if context_gap:
        config = _replace(ws.config, context=ContextConfig(
            station_skills={"build": ("필수-없는-스킬",)},
            required_skills={"build": ("필수-없는-스킬",)}))
    _install_fake_acceptance_and_handoff(monkeypatch, [_FakeAcceptanceResult(passed=True)])
    monkeypatch.setattr(faults, "ACTIVE_FLOW_FAULTS", faults.Faults(crash_before={boundary}))
    with pytest.raises(faults.CrashInjected):
        full_orbit.run_full_job(config, ws.ledger, _fakes(), "alpha", job_name,
                                "두 파일을 만들어줘", processor_factory=factory)
    monkeypatch.setattr(faults, "ACTIVE_FLOW_FAULTS", None)
    return factory, stub


def _crash_after_builder_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first build task's open_job opens for real, then the process
    dies before the task position on that attempt is recorded."""
    real = full_orbit.open_job
    fired = {"done": False}

    def _open(config, ledger, project, job, kind, roles):
        attempt_id = real(config, ledger, project, job, kind, roles)
        if tuple(roles) == (full_orbit.BUILDER_ROLE,) and not fired["done"]:
            fired["done"] = True
            raise faults.CrashInjected("after open_job")
        return attempt_id

    # A build task's open_job call is made from engine.full_orbit_runner's
    # own _open_with_intent (the facade re-exports the same function
    # object, but it resolves `open_job` through that module's own
    # globals), so the patch has to target it there, not the facade.
    monkeypatch.setattr(full_orbit_runner, "open_job", _open)


def test_crash_after_open_job_before_the_task_position_adopts_the_open_attempt(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "task-opening-crash"
    _crash_after_builder_open(monkeypatch)
    factory, stub = _two_task_job_crashing_at(ws, monkeypatch, job_name, "never-armed")
    opened = ws.ledger.attempts_for_job_name("alpha", job_name)[-1].attempt_id
    pos = position.load_position(ws.ledger, "alpha", job_name)
    assert pos is not None and pos.stage == position.STAGE_TASK
    assert pos.task_id == "t1" and pos.attempt_id is None
    assert position.opening_roles(pos) == (full_orbit.BUILDER_ROLE,)
    assert ws.ledger.project("alpha").state == "OPEN"

    resumed = full_orbit.continue_full_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                                           processor_factory=factory)

    assert resumed.status == "done", resumed
    attempts = [a.attempt_id for a in ws.ledger.attempts_for_job_name("alpha", job_name)]
    assert len(attempts) == 3, attempts  # early stations, t1 (adopted), t2
    assert attempts[1] == opened
    assert stub.calls.count(("build", "builder")) == 2
    assert len(ws.ledger.events("alpha", "commit.recorded")) == 2


def test_crash_before_open_job_opens_the_task_attempt_on_resume(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opening position is recorded, open_job never ran (the crash
    hit right before the open): resume opens the attempt once."""
    ws = _full_orbit_workspace(workspace)
    job_name = "task-opening-before-open"
    real_open = full_orbit.open_job
    state = {"n": 0}

    def _open_crashing_on_t1(config, ledger, project, job, kind, roles):
        if roles == (full_orbit.BUILDER_ROLE,) and state["n"] == 0:
            state["n"] += 1
            raise faults.CrashInjected("before open_job")
        return real_open(config, ledger, project, job, kind, roles)

    # See _crash_after_builder_open's own comment: the build task's
    # open_job call is made from engine.full_orbit_runner's own
    # _open_with_intent, so that is what must be patched.
    monkeypatch.setattr(full_orbit_runner, "open_job", _open_crashing_on_t1)
    factory, stub = _two_task_job_crashing_at(ws, monkeypatch, job_name, "never-armed")
    monkeypatch.setattr(full_orbit_runner, "open_job", real_open)
    assert ws.ledger.project("alpha").state == "CLOSED"
    pos = position.load_position(ws.ledger, "alpha", job_name)
    assert pos is not None and pos.stage == position.STAGE_TASK and pos.attempt_id is None

    resumed = full_orbit.continue_full_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                                           processor_factory=factory)

    assert resumed.status == "done", resumed
    assert len(ws.ledger.attempts_for_job_name("alpha", job_name)) == 3
    assert stub.calls.count(("build", "builder")) == 2


def test_context_gap_crash_after_open_job_still_rechecks_the_context(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "task-opening-gap"
    _crash_after_builder_open(monkeypatch)
    factory, stub = _two_task_job_crashing_at(ws, monkeypatch, job_name, "never-armed",
                                              context_gap=True)
    from dataclasses import replace as _replace

    from engine.context import ContextConfig

    gap_config = _replace(ws.config, context=ContextConfig(
        station_skills={"build": ("필수-없는-스킬",)},
        required_skills={"build": ("필수-없는-스킬",)}))
    still = full_orbit.continue_full_job(gap_config, ws.ledger, _fakes(), "alpha", job_name,
                                         processor_factory=factory)
    assert still.status == "needs_owner"
    assert still.reason is not None and "필수-없는-스킬" in still.reason
    assert ("build", "builder") not in stub.calls
    assert len(ws.ledger.attempts_for_job_name("alpha", job_name)) == 2

    done = full_orbit.continue_full_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                                        processor_factory=factory)
    assert done.status == "done", done
    assert len(ws.ledger.attempts_for_job_name("alpha", job_name)) == 3


# ---------------------------------------------------------------------------
# boundaries
# ---------------------------------------------------------------------------

def test_every_flow_boundary_in_the_code_is_listed() -> None:
    literal = re.compile(r'flow_boundary\(\s*"([^"]+)"\s*\)')
    used: set[str] = set()
    for path in [*_ENGINE.glob("*.py"), *(_ENGINE / "steps").glob("*.py")]:
        used.update(literal.findall(path.read_text(encoding="utf-8")))
    fix_kinds = re.findall(r'_append_fix_event\(\s*ledger, project, "(fix\.[a-z_]+)"',
                           (_ENGINE / "repair.py").read_text(encoding="utf-8"))
    used.update(f"fixup.{kind}" for kind in fix_kinds)
    assert used, "no boundaries found"
    assert used <= set(position.FLOW_BOUNDARIES), sorted(used - set(position.FLOW_BOUNDARIES))
    assert len(position.FLOW_BOUNDARIES) == len(set(position.FLOW_BOUNDARIES))
    assert {f"position.record.{s}" for s in position.STAGES} <= set(position.FLOW_BOUNDARIES)
