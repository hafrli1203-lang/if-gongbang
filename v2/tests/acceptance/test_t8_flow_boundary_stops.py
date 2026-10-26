"""T8 -- the 8 owner-stop flow-boundary groups the base two-task job
(test_t6_crash_matrix.py) cannot reach on its own: design approval,
security fix, small_plan, probe retry, plan review, requirements
review, the early-station owner stop, and release approval.

Each test builds the SAME scripted scenario tests/unit/test_full_orbit.
py (or test_small_orbit.py) already proved reaches that stop, but arms
engine.faults.ACTIVE_FLOW_FAULTS for the stop's own position.record.*
boundary BEFORE the one run that would write it, expects exactly one
CrashInjected, then resumes through engine.position.resume_job.

The crash lands BEFORE the stop's own position write ever commits, so
resume_job's own load_position falls back to the last position
actually written and retries that stage fresh instead of reproducing
the stop as-is -- for most groups (probe retry, plan review, security
fix, design approval, the early-station owner request) that fresh
retry, using the SAME fakes/stub queues this test already prepared,
reaches "done" directly in one resume call: a different, equally
valid recovery path from a plain "stop, then a real owner action"
cycle, not a second owner action. requirements_review and release_
approval genuinely need a human decision no retry can supply, so
those two resume back to the SAME needs_owner stop instead. Every
test checks no lost/duplicated work for whichever path it takes
(file content, acceptance/handoff call counts, no second publish
request, no station call that should not have re-run).

Real failures found this way are xfail(strict=True) with the reason,
exactly like test_t6_crash_matrix.py's own _FOUND_BUG group."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from engine import faults, full_orbit, position, small_orbit
from engine.faults import CrashInjected
from engine.ledger import Event
from engine.processor import Processor, StepContext, StepDef
from tests.conftest import Workspace
from tests.unit.test_full_orbit import (
    _EARLY_INSTALL_REQUEST,
    _FakeAcceptanceResult,
    _FakeSceneResult,
    _FakeSecurityResult,
    _RunStationStub,
    _fakes,
    _full_orbit_workspace,
    _install_fake_acceptance_and_handoff,
    _plan_json,
    _plan_queue_entry,
    _processor_factory,
    _release_project,
    _review_always_passes_steps,
    _station_stub_with_one_owner_request,
)
from tests.unit.test_small_orbit import _FACTS_MD_NO_TARGETS, _planning_files, _small_plan_json


def _arm_and_crash(monkeypatch: pytest.MonkeyPatch, boundary: str, run_once) -> None:
    """Arms `boundary`, calls `run_once()` expecting exactly one
    CrashInjected, confirms it actually fired (not merely that nothing
    raised), then clears the fault."""
    monkeypatch.setattr(faults, "ACTIVE_FLOW_FAULTS", faults.Faults(crash_before={boundary}))
    with pytest.raises(CrashInjected):
        run_once()
    fired = boundary not in faults.ACTIVE_FLOW_FAULTS.crash_before
    monkeypatch.setattr(faults, "ACTIVE_FLOW_FAULTS", None)
    assert fired, f"{boundary} never fired"


# ---- 1. probe retry -------------------------------------------------------

def test_crash_at_probe_retry_boundary_then_continue_resumes_and_finishes(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "flow-probe-retry"
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
            {f".gongbang/{job_name}/probe/t1.log":
             "https://docs.stripe.com/api\nPROBE FAIL\nconnection refused\n"},
            {f".gongbang/{job_name}/probe/t1.log":
             "https://docs.stripe.com/api\nPROBE OK\n{\"status\": 200}\n"},
        ],
        "plan": [_plan_queue_entry(job_name, plan_json)],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    _install_fake_acceptance_and_handoff(monkeypatch, [_FakeAcceptanceResult(passed=True)])
    factory = _processor_factory(stub)

    _arm_and_crash(monkeypatch, "position.record.awaiting_probe_retry", lambda: full_orbit.run_full_job(
        ws.config, ws.ledger, _fakes(), "alpha", job_name, "stripe 결제 붙여줘",
        processor_factory=factory,
    ))

    # The crash lands BEFORE the stop's own position write ever commits
    # -- load_position falls back to the last position actually
    # written (before probe ran at all), so resume_job's own retry
    # redispatches probe fresh rather than reproducing the needs_owner
    # stop: the stub's own SECOND, "PROBE OK" queue entry is what that
    # fresh probe call consumes, so this one resume call goes straight
    # to "done" -- a DIFFERENT (and just as valid) recovery path from
    # the plain needs_owner-then-owner-retries one test_full_orbit.py's
    # own test exercises, not a second owner action.
    done = position.resume_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                               processor_factory=factory)
    assert done.status == "done", done
    assert [c for c in stub.calls if c[0] == "probe"] == [
        ("probe", "prober"), ("probe", "prober"),
    ]
    assert (ws.alpha / "src/greeter/extra1.py").exists()


# ---- 2. plan review --------------------------------------------------------

def test_crash_at_plan_review_boundary_then_continue_resumes_and_finishes(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "flow-plan-review"
    plan_json = _plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    stub = _RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": "- 요청 확인: 통과 (출처: request)\n\n## 외부 대상\n(없음)\n"}],
        "plan": [_plan_queue_entry(job_name, plan_json), _plan_queue_entry(job_name, plan_json)],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    review_calls = {"n": 0}

    def _review_fails_once_then_passes(ctx: StepContext) -> list[StepDef]:
        def _run(ctx: StepContext) -> dict[str, object]:
            review_calls["n"] += 1
            if review_calls["n"] == 1:
                from engine.processor import StepFailed
                raise StepFailed("review_not_passed")
            from engine import evidence
            project_config = ctx.config.projects[ctx.project]
            fp = evidence.fingerprint(project_config.repo, project_config.excludes)
            evidence.record(ctx.ledger, ctx.project, ctx.attempt_id, "review", fp,
                            payload={"passed": True, "request_id": ctx.request.request_id,
                                     "findings": []})
            return {}

        def _reconcile(ctx: StepContext) -> bool:
            if review_calls["n"] == 1:
                return False
            return any(row.payload.get("request_id") == ctx.request.request_id
                      for row in ctx.ledger.evidence(ctx.attempt_id, "review"))

        return [StepDef("record_review", _run, _reconcile)]

    def factory(config, ledger, fakes) -> Processor:
        proc = Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", stub.steps)
        proc.register("review", _review_fails_once_then_passes)
        return proc

    _install_fake_acceptance_and_handoff(monkeypatch, [_FakeAcceptanceResult(passed=True)])

    _arm_and_crash(monkeypatch, "position.record.awaiting_plan_review", lambda: full_orbit.run_full_job(
        ws.config, ws.ledger, _fakes(), "alpha", job_name, "파일 하나 만들어줘",
        processor_factory=factory,
    ))

    # Same reasoning as the probe-retry test above: the crash lands
    # BEFORE the stop's own position write commits, so resume falls
    # back to the last-written position (before this plan review ran
    # at all) and redispatches review fresh -- review_calls["n"] was
    # already 1 (the crashed run's own failing call), so THIS call is
    # review_calls["n"] == 2, the one the stub scripts to pass -- one
    # resume call goes straight to "done".
    done = position.resume_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                               processor_factory=factory)
    assert done.status == "done", done
    assert (ws.alpha / "src/greeter/extra1.py").exists()


# ---- 3. security fix -------------------------------------------------------

def test_crash_at_security_fix_boundary_then_continue_resumes_and_finishes(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "flow-security-fix"
    plan_json = _plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    stub = _RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": "- 요청 확인: 통과 (출처: request)\n\n## 외부 대상\n(없음)\n"}],
        "plan": [_plan_queue_entry(job_name, plan_json)],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    state = _install_fake_acceptance_and_handoff(monkeypatch, [
        _FakeAcceptanceResult(passed=True), _FakeAcceptanceResult(passed=True),
    ])
    security_results = [False, True]

    def check(config, project, attempt_id, plan):
        return _FakeSecurityResult(passed=security_results.pop(0))

    sys.modules["engine.app_security"].check = check  # type: ignore[attr-defined]
    factory = _processor_factory(stub)

    _arm_and_crash(monkeypatch, "position.record.awaiting_security_fix", lambda: full_orbit.run_full_job(
        ws.config, ws.ledger, _fakes(), "alpha", job_name, "파일 하나 만들어줘",
        processor_factory=factory,
    ))

    # Same "crash lands before the stop's own write" reasoning as
    # probe-retry/plan-review: acceptance+handoff+the first (failing)
    # security check all already ran before the crash; resume falls
    # back to the pre-acceptance position and reruns acceptance ->
    # handoff -> security fresh (STAGE_SECURITY_FIX's own resume always
    # does, per _resume_acceptance's docstring), consuming this test's
    # SECOND prepared acceptance result and security_results entry
    # (True) -- one resume call goes straight to "done".
    done = position.resume_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                               processor_factory=factory)
    assert done.status == "done", done
    assert state.acceptance_calls == 2
    assert security_results == []  # both prepared results consumed, none left over


# ---- 4. design approval ----------------------------------------------------

def test_crash_at_design_approval_boundary_then_continue_resumes_and_finishes(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "flow-design-approval"
    plan_json = _plan_json(ui=True, tasks=[
        {"id": "t1", "title": "화면 연결", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    stub = _RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": "- 요청 확인: 통과 (출처: request)\n\n## 외부 대상\n(없음)\n"}],
        "plan": [_plan_queue_entry(job_name, plan_json)],
        # Two identical entries: the crash lands before the stop's own
        # position write commits, so resume falls back to an earlier
        # position and redispatches the design station fresh (same
        # reasoning as probe-retry/plan-review/security-fix above).
        "design": [{f".gongbang/{job_name}/design/design.md": "# 시안\n"}] * 2,
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    _install_fake_acceptance_and_handoff(monkeypatch, [_FakeAcceptanceResult(passed=True)])
    factory = _processor_factory(stub)

    _arm_and_crash(monkeypatch, "position.record.awaiting_design_approval", lambda: full_orbit.run_full_job(
        ws.config, ws.ledger, _fakes(), "alpha", job_name, "화면 하나 만들어줘",
        processor_factory=factory,
    ))

    first = position.resume_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                                processor_factory=factory)
    assert first.status == "needs_owner", first
    assert first.reason == "design_approval"
    assert (ws.alpha / f".gongbang/{job_name}/design/design.md").exists()

    ws.ledger.append(Event(project="alpha", kind="design.approved", actor_kind="owner",
                           actor_id="owner", attempt_id=None, payload={"job": job_name}))

    done = position.resume_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                               processor_factory=factory)
    assert done.status == "done", done
    assert (ws.alpha / "src/greeter/extra1.py").exists()


# ---- 4b. entering the design station itself (not its approval stop) -----

def test_crash_at_design_stage_boundary_then_continue_resumes_and_finishes(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """position.record.design -- recorded right before the design
    station is dispatched (unlike position.record.awaiting_design_
    approval above, which is the STOP after design runs). By this
    point facts/plan have already recorded their own positions, so
    (unlike position.record.facts/small_plan) there is somewhere for
    resume_job to fall back to."""
    ws = _full_orbit_workspace(workspace)
    job_name = "flow-design-stage"
    plan_json = _plan_json(ui=True, tasks=[
        {"id": "t1", "title": "화면 연결", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    stub = _RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": "- 요청 확인: 통과 (출처: request)\n\n## 외부 대상\n(없음)\n"}],
        # Two identical entries each: the crash lands before "design"'s
        # own position write commits, so resume falls back to the
        # position recorded before design even ran (right after plan)
        # and redispatches BOTH plan and design fresh.
        "plan": [_plan_queue_entry(job_name, plan_json)] * 2,
        "design": [{f".gongbang/{job_name}/design/design.md": "# 시안\n"}] * 2,
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    _install_fake_acceptance_and_handoff(monkeypatch, [_FakeAcceptanceResult(passed=True)])
    factory = _processor_factory(stub)

    _arm_and_crash(monkeypatch, "position.record.design", lambda: full_orbit.run_full_job(
        ws.config, ws.ledger, _fakes(), "alpha", job_name, "화면 하나 만들어줘",
        processor_factory=factory,
    ))

    first = position.resume_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                                processor_factory=factory)
    assert first.status == "needs_owner", first
    assert first.reason == "design_approval"
    # position.record.design fires BEFORE design is ever dispatched, so
    # the crashed run never called it at all -- the resumed run's own
    # fresh retry is the ONLY design call.
    assert stub.calls.count(("design", "designer")) == 1

    ws.ledger.append(Event(project="alpha", kind="design.approved", actor_kind="owner",
                           actor_id="owner", attempt_id=None, payload={"job": job_name}))
    done = position.resume_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                               processor_factory=factory)
    assert done.status == "done", done
    assert (ws.alpha / "src/greeter/extra1.py").exists()


# ---- 5. the early-station owner stop (facts asks for an install) ---------

def test_crash_at_early_station_owner_boundary_then_continue_resumes_and_finishes(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "flow-early-station"
    plan_json = _plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    queues = {
        "facts": [{f".gongbang/{job_name}/facts.md": "- 요청 확인: 통과 (출처: request)\n\n## 외부 대상\n(없음)\n"}],
        "plan": [_plan_queue_entry(job_name, plan_json)],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    }
    stub, steps_fn = _station_stub_with_one_owner_request(queues, "facts", _EARLY_INSTALL_REQUEST)

    def factory(config, ledger, fakes) -> Processor:
        proc = Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", steps_fn)
        proc.register("review", _review_always_passes_steps)
        return proc

    _install_fake_acceptance_and_handoff(monkeypatch, [_FakeAcceptanceResult(passed=True)])

    _arm_and_crash(
        monkeypatch, "position.record.awaiting_early_station_owner",
        lambda: full_orbit.run_full_job(
            ws.config, ws.ledger, _fakes(), "alpha", job_name, "파일 하나 만들어줘",
            processor_factory=factory,
        ),
    )

    # The crash lands before the stop's own position write commits, so
    # resume falls back to the position before "facts" ran and
    # redispatches it fresh -- _station_stub_with_one_owner_request's
    # own `calls["n"]` counter (already 1 from the crashed run's own
    # station.finished, which DID carry the install request) is now
    # past 1, so this redo behaves like every later call: it just
    # writes facts.md, no owner request this time. The job never needs
    # to ask again and goes straight to "done" -- a different, equally
    # valid recovery path from test_full_orbit.py's own plain needs_
    # owner-then-approve one, not a second owner action.
    done = position.resume_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                               processor_factory=factory)
    assert done.status == "done", done
    assert (ws.alpha / "src/greeter/extra1.py").exists()


# ---- 6. requirements review (acceptance passes, a requirement is -------
# ---- left with no covering PASSING scene) ---------------------------------

def test_crash_at_requirements_review_boundary_then_continue_resumes_to_the_same_stop(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "flow-requirements-review"
    plan_json = _plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    stub = _RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": "- 요청 확인: 통과 (출처: request)\n\n## 외부 대상\n(없음)\n"}],
        "plan": [_plan_queue_entry(job_name, plan_json)],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    # passed=True but the ONE scene reported ("s2") never matches
    # plan.json's own scene id ("s1", the one R1's `covers` names) --
    # acceptance itself is fine, the coverage gate is what stops this.
    # Two identical results: the crash lands before the stop's own
    # position write commits, so resume falls back to an earlier
    # position and reruns acceptance fresh (same "crash before the
    # stop write" pattern as every other test in this file) -- the
    # SAME uncovered-requirement verdict is what the resumed run must
    # reach again, not something fixed out from under it.
    uncovered_result = _FakeAcceptanceResult(passed=True, scenes=(_FakeSceneResult("s2", True),))
    state = _install_fake_acceptance_and_handoff(
        monkeypatch, [uncovered_result, uncovered_result],
    )
    factory = _processor_factory(stub)

    _arm_and_crash(
        monkeypatch, "position.record.awaiting_requirements_review",
        lambda: full_orbit.run_full_job(
            ws.config, ws.ledger, _fakes(), "alpha", job_name, "파일 하나 만들어줘",
            processor_factory=factory,
        ),
    )

    resumed = position.resume_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                                  processor_factory=factory)
    assert resumed.status == "needs_owner", resumed
    assert str(resumed.reason).startswith("requirements_uncovered")
    assert state.acceptance_calls == 2  # the crashed run's own call, then resume's fresh rerun
    assert len(state.handoff_calls) == 0  # handoff never ran -- coverage gate stopped it first


# ---- 7. release approval ---------------------------------------------------

def test_crash_at_release_approval_boundary_then_continue_resumes_to_the_same_stop(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    ws = _full_orbit_workspace(workspace)
    config, _master = _release_project(ws, tmp_path)
    job_name = "flow-release-approval"
    plan_json = _plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    stub = _RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": "- 요청 확인: 통과 (출처: request)\n\n## 외부 대상\n(없음)\n"}],
        "plan": [_plan_queue_entry(job_name, plan_json)],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    # app_security's own fake `record` is a no-op by design (see
    # _install_fake_app_security's own docstring) -- replace it with a
    # real write so the ONE run (fault armed from the very start) can
    # reach job_certificate_status's own gate and stop at release_
    # approval without a second, post-hoc run to backfill the event.
    from engine.app_security import security_policy_hash
    from engine import evidence as evidence_mod

    def real_record(ledger, project, attempt_id, result) -> None:
        project_config = config.projects[project]
        fp = evidence_mod.fingerprint(project_config.repo, project_config.excludes)
        ledger.append(Event(
            project=project, kind="app_security.recorded", actor_kind="processor",
            actor_id="processor", attempt_id=attempt_id,
            payload={"passed": result.passed, "findings": list(result.findings),
                     "ran": list(result.ran), "fingerprint": fp,
                     "policy_hash": security_policy_hash(config)},
        ))

    # Two entries: the crash lands before the stop's own position
    # write commits, so resume falls back to the pre-acceptance
    # position and reruns acceptance -> handoff -> security fresh
    # (same pattern as every post_tasks-reached stop in this file).
    _install_fake_acceptance_and_handoff(
        monkeypatch, [_FakeAcceptanceResult(passed=True), _FakeAcceptanceResult(passed=True)],
    )
    sys.modules["engine.app_security"].record = real_record  # type: ignore[attr-defined]
    factory = _processor_factory(stub)

    _arm_and_crash(
        monkeypatch, "position.record.awaiting_release_approval",
        lambda: full_orbit.run_full_job(
            config, ws.ledger, _fakes(), "alpha", job_name, "파일 하나 만들어줘",
            processor_factory=factory,
        ),
    )

    resumed = position.resume_job(config, ws.ledger, _fakes(), "alpha", job_name,
                                  processor_factory=factory)
    assert resumed.status == "needs_owner", resumed
    assert resumed.reason == "release_approval"
    publish_requests = [r for r in ws.ledger.queued_requests("alpha") if r.kind == "publish"]
    assert len(publish_requests) == 1  # not doubled by the crash+resume


# ---- 8. small_plan ---------------------------------------------------------

@pytest.mark.xfail(strict=True, reason=(
    "FOUND by this harness: engine.small_orbit.run_small_job's own FIRST "
    "_record_small call (position.record.small_plan, right after open_job "
    "-- engine/small_orbit.py:479-482) is the VERY FIRST position write "
    "for this job, same class of bug as test_t6_crash_matrix.py's own "
    "xfail on position.record.facts (a brand-new full job's own first "
    "write): a crash there leaves nothing for resume_job to read -- "
    "engine.full_orbit_base.FullOrbitError('no attempt for job ...') -- "
    "because load_position finds no job.position event at all yet and "
    "ledger.attempts_for_job_name comes back empty too (open_job's own "
    "attempt-open intent never committed either). s20-position's own "
    "current batch (see position.record.facts)."
))
def test_crash_at_small_plan_boundary_then_continue_resumes_and_finishes(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _full_orbit_workspace(workspace)
    job_name = "flow-small-plan"
    stub = _RunStationStub({
        "plan": [_planning_files(_FACTS_MD_NO_TARGETS, _small_plan_json())],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    state = _install_fake_acceptance_and_handoff(
        monkeypatch, [_FakeAcceptanceResult(passed=True, scenes=(_FakeSceneResult("s1", True),))],
    )
    factory = _processor_factory(stub)

    _arm_and_crash(monkeypatch, "position.record.small_plan", lambda: small_orbit.run_small_job(
        ws.config, ws.ledger, _fakes(), "alpha", job_name, "값 하나만 바꿔줘",
        processor_factory=factory,
    ))

    done = position.resume_job(ws.config, ws.ledger, _fakes(), "alpha", job_name,
                               processor_factory=factory)
    assert done.status == "done", done
    assert stub.calls == [("plan", "planner"), ("build", "builder")]  # not re-run from scratch
    assert (ws.alpha / "src/greeter/extra1.py").exists()
    assert state.acceptance_calls == 1
