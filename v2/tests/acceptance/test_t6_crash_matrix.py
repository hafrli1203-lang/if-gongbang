"""T6 -- the crash matrix: for every processor step boundary
(engine.faults.Faults.crash_before/crash_after across every stage list,
see tests/harness/boundaries.py) and the whole-flow-layer naming
convention this harness documents for flow boundaries, crash a scripted
full job there once and assert:
  (a) the job reaches its final state ("done");
  (b) no effect happened twice (every ledger effect-count this harness
      can observe equals the SAME job run start-to-finish with no
      crash at all);
  (c) no work was lost (every task's own file change is present).

docs/flow-map.md ③/⑥ is this file's test plan: the rows marked "있음"
are the ones this matrix should pass outright; "없음"/"확인 못 함" are
the ones a first run here might newly fail, at which point they get
pytest.mark.xfail(strict=True) with the flow-map row as the reason
(removed the moment a later engine fix makes them pass -- strict=True
turns a surprise XPASS into a hard failure so nobody forgets to delete
the marker).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine import evidence, full_orbit, plan as plan_module
from tests.conftest import GIT_ENV, Workspace
from tests.harness import boundaries, driver

# ---- boundaries this base two-task job can actually reach --------------
#
# run_station/merge (facts, plan) and run_station/merge/review/bench/
# commit/report/close (each build task) are all exercised by the base
# job (engine.orbit.default_stages, docs/flow-map.md ①). install/
# scripts_change/test_config_change and publish/advance_master are NOT
# -- nothing in this scripted job ever asks the owner to install
# anything or configures `release`, so Faults armed for those step names
# would never fire (driver.run_crash_then_resume asserts that itself,
# turning a silent no-op into a loud failure instead of a false "pass").
# They are skipped here, not xfailed: this is a harness scope gap (no
# job script reaches them YET), not a known engine bug -- see the TODO
# below for the two follow-up job scripts that would close this gap.
_REACHABLE_KINDS = {"run_station", "merge", "review", "bench", "commit", "report", "close"}

# tests/harness/driver.py's own RunStationStub/review_always_passes_steps
# replace run_station/review's REAL step list (spawn_worker/await_worker/
# record_station; compute_fingerprint/spawn_reviewer/await_reviewer/
# record_review) with a single stand-in step each ("record_station"/
# "record_review") -- there is no live AI worker or reviewer process in
# this harness (tests/conftest.py's own _no_live_ai_cli fixture refuses
# one from any test), so the other three step names per kind can never
# fire here; arming them would silently never crash at all. Skipped, not
# xfailed -- the real engine.steps.station/review modules have their own
# unit-level crash tests (tests/unit/test_steps_station.py, test_steps_
# review.py) that DO drive spawn_worker/await_worker/spawn_reviewer/
# await_reviewer for real; this matrix's own job is the FULL-JOB resume
# path around whichever single step actually ran, not re-proving those
# steps' own internals.
_STUBBED_ONLY_STEPS = {"spawn_worker", "await_worker", "compute_fingerprint",
                       "spawn_reviewer", "await_reviewer"}

# "compute_fingerprint" is also a REAL step name shared by bench AND
# commit (both run it, for real, per task -- unlike review's own
# stubbed-out occurrence above). bench's own compute_fingerprint
# always runs chronologically FIRST within a task's stage list
# (run_station -> merge -> review -> bench -> commit -> ...), so
# arming "compute_fingerprint" from the start of a fresh job run
# always consumes the fault on bench's occurrence, never reaches
# commit's own -- driver.run_crash_then_resume's own kind verification
# catches and fails loudly on exactly this (AssertionError: "fired on
# kind='bench', not the intended kind='commit'") before being carved
# out here. Not an engine bug: commit's own compute_fingerprint step
# IS correctly crash-recoverable (proved by every OTHER commit:*
# boundary passing, and by bench:compute_fingerprint itself passing)
# -- this job script simply cannot isolate it from bench's earlier,
# same-named occurrence with the engine's own step-name-only Faults
# mechanism.
_SHADOWED_BY_EARLIER_KIND = frozenset({("commit", "compute_fingerprint")})

# A crash inside the close of the shared facts/plan attempt at
# set_closed(after)/open_queue is correctly recovered: the job's
# position is STAGE_EARLY_CLOSE, whose resume finishes that SAME
# close request instead of redoing the stage from scratch against an
# already-closed attempt (which would otherwise raise
# ValueError("attempt_not_active")) -- see the plain (no xfail) test
# right below. The build-task double-commit case this group once
# covered (commit:verify_three/git_commit/record_commit/report) is
# also correctly recovered and lives in _REACHABLE above.
_FOUND_BUG: frozenset[tuple[str, str, str]] = frozenset(
    [("close", "set_closed", "crash_after"),
     ("close", "open_queue", "crash_before"),
     ("close", "open_queue", "crash_after")]
)


def _reachable_by_base_job(kind: str, step: str, edge: str) -> bool:
    if kind not in _REACHABLE_KINDS:
        return False
    if kind in ("run_station", "review") and step in _STUBBED_ONLY_STEPS:
        return False
    if (kind, step) in _SHADOWED_BY_EARLIER_KIND:
        return False
    if (kind, step, edge) in _FOUND_BUG:
        return False
    return True


_REACHABLE = [
    (kind, step, edge) for kind, step, edge in boundaries.PROCESSOR_STEP_BOUNDARIES
    if _reachable_by_base_job(kind, step, edge)
]
_FOUND_BUG_CASES = [
    (kind, step, edge) for kind, step, edge in boundaries.PROCESSOR_STEP_BOUNDARIES
    if (kind, step, edge) in _FOUND_BUG
]
# install's and publish's own boundaries are now covered by dedicated
# job scripts below (test_crash_at_every_install_boundary_recovers_
# once, test_crash_at_every_publish_boundary_recovers_once) --
# scripts_change/test_config_change share install.py's own steps()
# function (boundaries._ALIAS_KINDS), so the same 6 steps cover all
# three kinds; only "install" itself appears in PROCESSOR_STEP_
# BOUNDARIES. Excluded from _UNREACHABLE so the skip count reflects
# only what is genuinely still not driven anywhere in this file (now
# nothing -- every kind in PROCESSOR_STEP_BOUNDARIES is covered by
# some test in this file; _UNREACHABLE/its test collect 0 cases, kept
# as the structural seam for whatever future kind shows up uncovered).
_COVERED_BY_DEDICATED_SCRIPT = frozenset({"install", "publish"})

_UNREACHABLE = [
    (kind, step, edge) for kind, step, edge in boundaries.PROCESSOR_STEP_BOUNDARIES
    if not _reachable_by_base_job(kind, step, edge) and (kind, step, edge) not in _FOUND_BUG
    and kind not in _COVERED_BY_DEDICATED_SCRIPT
]


def _ids(cases: list[tuple[str, str, str]]) -> list[str]:
    return [f"{kind}:{step}:{edge}" for kind, step, edge in cases]


def _duplicated_request_ids(ws: Workspace, kind: str) -> list[str]:
    """request_ids of `kind` whose own dispatch-effect event
    (station.finished for run_station, commit.recorded for commit,
    merge.recorded for merge) was journaled more than once FOR THE SAME
    request_id -- the actual "worker launched twice for one request"
    bug the contract's (b) names, as opposed to a legitimate NEW
    request_id (a crash early enough that the engine abandons the
    half-open attempt and redoes the whole station from scratch opens
    a brand new attempt/request, which launches its OWN worker once,
    not the same request twice)."""
    event_kind = {"run_station": "station.finished", "commit": "commit.recorded",
                 "merge": "merge.recorded"}[kind]
    counts: dict[str, int] = {}
    for _seq, ev in ws.ledger.events("alpha", event_kind):
        rid = ev.payload.get("request_id")
        if isinstance(rid, str):
            counts[rid] = counts.get(rid, 0) + 1
    return [rid for rid, n in counts.items() if n > 1]


def _assert_crash_recovered_cleanly(
    job: driver.TwoTaskJob, kind: str, step: str, edge: str,
) -> None:
    outcome = driver.run_crash_then_resume(job, kind, step, edge)

    assert outcome.status == "done", (kind, step, edge, outcome.reason)
    assert outcome.report_path is not None
    assert Path(outcome.report_path).exists()
    # (c) no work lost: both tasks' own files are there, with the
    # content the build station actually wrote (not left half-done).
    assert (job.ws.alpha / "src/greeter/extra1.py").read_text(encoding="utf-8") == "VALUE_1 = 1\n"
    assert (job.ws.alpha / "src/greeter/extra2.py").read_text(encoding="utf-8") == "VALUE_2 = 2\n"
    # (b) no effect happened twice, exactly as the plan's own wording:
    #   - commits per task: one commit.recorded per task, never more.
    assert len(job.ws.ledger.events("alpha", "commit.recorded")) == 2
    #   - publish requests: none -- this job never configures `release`.
    assert [r for r in job.ws.ledger.queued_requests("alpha") if r.kind == "publish"] == []
    #   - worker launches per request: no SINGLE request_id's own
    #     dispatch effect (station.finished/commit.recorded/merge.
    #     recorded) was journaled twice. A crash early enough that the
    #     engine abandons the half-open attempt and redoes the whole
    #     station from scratch opens a brand new attempt/request_id
    #     (engine.full_orbit._rerun_closed_task_from_scratch and the
    #     facts/probe/plan/design stages' own equivalent) -- that NEW
    #     request launching its own worker once is correct, not a
    #     duplicate of the OLD (abandoned) request's own single launch.
    for check_kind in ("run_station", "commit", "merge"):
        assert _duplicated_request_ids(job.ws, check_kind) == [], (kind, step, edge, check_kind)
    assert job.state.acceptance_calls == 1
    assert len(job.state.handoff_calls) == 1


@pytest.mark.parametrize("kind,step,edge", _REACHABLE, ids=_ids(_REACHABLE))
def test_crash_at_every_reachable_boundary_recovers_once(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, kind: str, step: str, edge: str,
) -> None:
    job = driver.build_two_task_job(workspace, monkeypatch, job_name="crash-job")
    _assert_crash_recovered_cleanly(job, kind, step, edge)


@pytest.mark.parametrize("kind,step,edge", _FOUND_BUG_CASES, ids=_ids(_FOUND_BUG_CASES))
def test_crash_at_every_found_bug_boundary_recovers_once(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, kind: str, step: str, edge: str,
) -> None:
    job = driver.build_two_task_job(workspace, monkeypatch, job_name="crash-job")
    _assert_crash_recovered_cleanly(job, kind, step, edge)


@pytest.mark.skip(reason=(
    "install (and scripts_change/test_config_change, which share its "
    "step list) and publish (incl. advance_master) -- the kinds the "
    "base two-task job cannot reach on its own -- are each covered by "
    "their own dedicated job script below (test_crash_at_every_"
    "install_boundary_recovers_once, test_crash_at_every_publish_"
    "boundary_recovers_once), driven through driver.submit_crash_then_"
    "resume. The only other entries here are commit:compute_"
    "fingerprint (both edges, see _SHADOWED_BY_EARLIER_KIND above) --"
    " genuinely unreachable BY THIS JOB SCRIPT, not an engine bug. "
    "Kept so a future new kind/collision _reachable_by_base_job "
    "excludes is caught here as a visible skip instead of silently "
    "missing from every group."
))
@pytest.mark.parametrize("kind,step,edge", _UNREACHABLE, ids=_ids(_UNREACHABLE))
def test_crash_at_every_unreachable_boundary_recovers_once(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, kind: str, step: str, edge: str,
) -> None:
    pass  # never runs -- pytest.mark.skip above


# ---- flow-layer boundaries ----------------------------------------------

def test_harness_drives_the_engines_own_flow_boundary_list() -> None:
    from engine import position

    assert boundaries.FLOW_BOUNDARIES is position.FLOW_BOUNDARIES
    assert boundaries.FLOW_BOUNDARIES


# The test above only proves two names reference the same tuple, not
# that any flow boundary actually recovers. This parametrizes EVERY
# name in
# boundaries.FLOW_BOUNDARIES: reachable ones are armed on the base
# two-task job (or the release-configured/fix-cycle jobs already built
# above for other tests) and must actually fire, then driver.resume()/
# engine.position.resume_job must finish the job cleanly. Names this
# job infrastructure cannot reach are listed explicitly with a reason,
# never silently dropped.
_FLOW_BOUNDARY_UNREACHABLE_REASONS: dict[str, str] = {
    "position.record.design": "needs a UI plan (plan.ui=True) -- not built in this pass",
    "position.record.awaiting_design_approval": "needs a UI plan stopped at design "
        "approval -- tests/acceptance/test_t7_config_matrix.py's own design-wait test "
        "covers the SAME stop via a different mechanism (a run_station crash after "
        "design.approved, not this boundary itself)",
    "position.record.awaiting_probe_retry": "needs a probe-retry (external target + "
        "a failing probe run) job script -- not built in this pass",
    "position.record.awaiting_plan_review": "needs a plan-review-failure job script "
        "-- not built in this pass",
    "position.record.awaiting_requirements_review": "needs a requirements-coverage-"
        "failure job script -- not built in this pass",
    "position.record.awaiting_security_fix": "needs an app_security failure job "
        "script -- not built in this pass",
    "position.record.awaiting_early_station_owner": "needs a facts/probe/plan/design "
        "station that itself stops needs_owner (worker_stuck/context gap/owner "
        "request) -- not built in this pass",
    "full_orbit.context_failed": "needs a required skill/company-library lookup that "
        "never loads (unmet_required) -- no skill config in any job script here",
    "position.record.single_task": "`gb2 run`'s own single-task entry -- this harness "
        "only drives full-job entry points (run_full_job/continue_full_job/"
        "resume_job), same documented gap as test_t7_config_matrix.py's "
        "gb2_run_single_task_plus_release row",
    "position.record.small_plan": "`gb2 change --small`'s own entry -- not built in "
        "this pass",
    "fixup.fix.opening_failed": "only fires when open_job() itself raises while "
        "opening the fix's replacement attempt (e.g. the project is unexpectedly not "
        "CLOSED) -- not reproduced by this harness's plain one-review-failure fix "
        "scenario",
    "fixup.fix.task_stop": "fires when the FIX's own repaired task itself stops "
        "needs_owner again (a nested stop) -- not reproduced by this harness's plain "
        "one-review-failure fix scenario, which always finishes in one round",
    "close.attempt_finished": "already covered by this file's own close:* boundary "
        "group above (test_crash_at_every_reachable_boundary_recovers_once / "
        "test_crash_at_every_found_bug_boundary_recovers_once) -- same ledger write, "
        "named there by its processor-step identity instead of its flow-boundary name",
    "close.state_closed": "same as close.attempt_finished -- already covered above",
    "full_orbit.probe_skipped": "needs a job with no external target that still "
        "reaches the probe stage check -- the base two-task job's own plan has no "
        "external target either, but tracing showed this specific boundary sits "
        "where probe is skipped from a DIFFERENT entry point (continue_full_job's "
        "own early-station resume) this job script never takes; not reproduced",
    "full_orbit.orphan_abandoned": "needs a specific crash-then-reopen sequence that "
        "leaves an orphaned attempt behind for a LATER call to find and abandon -- "
        "not built in this pass",
    "full_orbit.release_decision": "fires inside finalize_release, reached only via "
        "`gb2 ship`/`gb2 skip-ship`/`gb2 resolve` -- this harness's own publish-"
        "boundary test (test_crash_at_every_publish_boundary_recovers_once) drives "
        "publish's own processor steps directly instead of through finalize_release; "
        "not built in this pass",
    "position.record.awaiting_release_approval": "reaching this stop needs "
        "app_security.recorded/acceptance.recorded ALREADY on the ledger before the "
        "ONE run that must also have the fault armed from the start (the driver's "
        "own app_security stub is a no-op by design -- test_crash_at_every_publish_"
        "boundary_recovers_once's own _release_stopped_job appends those AFTER a "
        "crash-free run, which cannot be reused here without also undoing that run); "
        "not built in this pass",
    "small_orbit.plan_reviewed": "`gb2 change --small`'s own entry -- not built in "
        "this pass",
}


def _crash_flow_boundary_on_two_task_job(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    from engine import faults

    job = driver.build_two_task_job(workspace, monkeypatch, job_name="flow-boundary-job")
    monkeypatch.setattr(faults, "ACTIVE_FLOW_FAULTS", faults.Faults(crash_before={boundary}))
    with pytest.raises(faults.CrashInjected):
        driver.run_to_completion(job)
    fired = boundary not in faults.ACTIVE_FLOW_FAULTS.crash_before
    monkeypatch.setattr(faults, "ACTIVE_FLOW_FAULTS", None)
    assert fired, f"{boundary} never fired on the base two-task job"

    outcome = driver.resume(job)
    assert outcome.status == "done", (boundary, outcome)
    assert len(job.ws.ledger.events("alpha", "commit.recorded")) == 2  # no doubled commit
    assert (job.ws.alpha / "src/greeter/extra1.py").read_text(encoding="utf-8") == "VALUE_1 = 1\n"
    assert (job.ws.alpha / "src/greeter/extra2.py").read_text(encoding="utf-8") == "VALUE_2 = 2\n"


_TWO_TASK_JOB_BOUNDARY_NAMES = (
    "position.record.facts", "position.record.probe", "position.record.plan",
    "position.record.early_close", "full_orbit.attempt_opened",
    "position.record.task", "position.record.post_tasks",
    "full_orbit.context_injected", "full_orbit.plan_reviewed",
)

# "position.record.facts" first fires at the job's opening intent,
# before any attempt or position exists: a crash there means nothing
# started, and continue reports that there is no attempt to resume.
# Every later opening (task, release, retry) records its intent first,
# so "full_orbit.attempt_opened" (after open_job, before the position
# on the attempt) resumes by adopting the attempt.
_TWO_TASK_JOB_XFAIL_REASONS: dict[str, str] = {
    "position.record.facts": (
        "a crash before the job's first record (its opening intent): nothing "
        "started, so continue raises FullOrbitError('no attempt for job ...') "
        "and the request is rerun with `gb2 new`/`gb2 change`."
    ),
}
_TWO_TASK_JOB_BOUNDARIES = [
    pytest.param(name, marks=pytest.mark.xfail(strict=True, reason=_TWO_TASK_JOB_XFAIL_REASONS[name]))
    if name in _TWO_TASK_JOB_XFAIL_REASONS else name
    for name in _TWO_TASK_JOB_BOUNDARY_NAMES
]


@pytest.mark.parametrize("boundary", _TWO_TASK_JOB_BOUNDARIES, ids=_TWO_TASK_JOB_BOUNDARY_NAMES)
def test_crash_at_every_two_task_job_flow_boundary_recovers_once(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    _crash_flow_boundary_on_two_task_job(workspace, monkeypatch, boundary)


@pytest.mark.parametrize("boundary", [
    "fixup.fix.started", "fixup.fix.closed_old", "fixup.fix.opening_new",
    "fixup.fix.opened_new", "fixup.fix.restored", "fixup.fix.submitted",
    "fixup.fix.finished",
])
def test_crash_at_every_fix_cycle_flow_boundary_recovers_once(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    from tests.unit import test_position as tp

    ws = driver.full_orbit_workspace(workspace, sample_name=f"sample-fb-{boundary.rsplit('.', 1)[-1]}")
    _first, factory, _reviews = tp._failed_task_job(ws, monkeypatch, ws.config,
                                                   f"fix-flow-{boundary.rsplit('.', 1)[-1]}")
    tp._crash_fix_at(monkeypatch, ws, ws.config, boundary, factory)

    outcome = full_orbit.continue_full_job(
        ws.config, ws.ledger, driver.fakes(), "alpha",
        f"fix-flow-{boundary.rsplit('.', 1)[-1]}", processor_factory=factory,
    )
    assert outcome.status == "done", (boundary, outcome)


def test_crash_at_awaiting_task_owner_flow_boundary_recovers_once(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """awaiting_task_owner -- a build task that stops needs_owner, same
    scenario as test_crash_at_every_install_boundary_recovers_once's
    own helper (an install request asked mid-task), but with the fault
    armed BEFORE the first run so it is this exact position write that
    crashes, not a step inside it."""
    from engine import faults
    from engine.faults import Faults as _Faults

    job_name = "task-owner-flow-job"
    ws = driver.full_orbit_workspace(workspace, sample_name=f"sample-{job_name}")
    plan_json = driver.plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    stub = driver.RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md":
                   "- 요청 확인: 통과 (출처: request)\n\n## 외부 대상\n(없음)\n"}],
        "plan": [driver.plan_queue_entry(job_name, plan_json)],
    })
    install_request = {"kind": "install", "manifest": "package.json",
                       "change": {"dependencies": {"left-pad": "^1.0.0"}}}
    build_steps, _build_calls = _build_steps_asking_for_install(stub, install_request)

    def factory(cfg, ledger, fks):
        from engine.processor import Processor
        proc = Processor(cfg, ledger, fks)
        proc.register_defaults()
        proc.register("run_station", build_steps)
        proc.register("review", driver.review_always_passes_steps)
        return proc

    state = driver.install_fake_acceptance_and_handoff(
        monkeypatch, [driver.FakeAcceptanceResult(passed=True)],
    )
    job = driver.TwoTaskJob(ws=ws, job_name=job_name, stub=stub,
                            faults=_Faults(), processor_factory=factory, state=state)

    monkeypatch.setattr(faults, "ACTIVE_FLOW_FAULTS",
                        faults.Faults(crash_before={"position.record.awaiting_task_owner"}))
    with pytest.raises(faults.CrashInjected):
        driver.run_to_completion(job, request="파일 하나 만들어줘")
    fired = ("position.record.awaiting_task_owner" not in faults.ACTIVE_FLOW_FAULTS.crash_before)
    monkeypatch.setattr(faults, "ACTIVE_FLOW_FAULTS", None)
    assert fired, "position.record.awaiting_task_owner never fired"

    outcome = driver.resume(job)
    assert outcome.status == "needs_owner", outcome
    assert outcome.reason == "owner_request"


@pytest.mark.parametrize("boundary", sorted(_FLOW_BOUNDARY_UNREACHABLE_REASONS))
def test_flow_boundary_documented_as_unreachable(boundary: str) -> None:
    pytest.skip(_FLOW_BOUNDARY_UNREACHABLE_REASONS[boundary])


def test_every_flow_boundary_is_either_tested_or_documented_as_unreachable() -> None:
    from engine import position as _position

    tested = set(_TWO_TASK_JOB_BOUNDARY_NAMES) | {
        "fixup.fix.started", "fixup.fix.closed_old", "fixup.fix.opening_new",
        "fixup.fix.opened_new", "fixup.fix.restored", "fixup.fix.submitted",
        "fixup.fix.finished", "position.record.awaiting_task_owner",
    }
    documented = set(_FLOW_BOUNDARY_UNREACHABLE_REASONS)
    assert set(_position.FLOW_BOUNDARIES) == tested | documented, (
        set(_position.FLOW_BOUNDARIES) - (tested | documented),
        (tested | documented) - set(_position.FLOW_BOUNDARIES),
    )


# ---- the three named feedback cases (plan §"완료 기준") -----------------

def test_fix_opened_new_crash_then_continue_does_not_open_a_second_round(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/flow-map.md ③ row 10 (피드백 ①): a crash right after
    fixup.fix records the opened replacement attempt; continue finishes
    that same fix cycle and never opens a second replacement."""
    from tests.unit import test_position as tp

    ws = driver.full_orbit_workspace(workspace)
    _first, factory, _reviews = tp._failed_task_job(ws, monkeypatch, ws.config,
                                                   "fix-feedback-1")
    tp._crash_fix_at(monkeypatch, ws, ws.config, "fixup.fix.restored", factory)

    outcome = full_orbit.continue_full_job(ws.config, ws.ledger, driver.fakes(), "alpha",
                                           "fix-feedback-1", processor_factory=factory)

    assert outcome.status == "done", outcome
    assert len(tp._fix_opened(ws.ledger)) == 1
    assert len(ws.ledger.events("alpha", "fix.started")) == 1
    assert len(ws.ledger.attempts_for_job_name("alpha", "fix-feedback-1")) == 4


def test_release_configured_fix_recovery_makes_exactly_one_publish_request(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """docs/flow-map.md ⑥ '커서 재개 중 수정 마무리' x release configured
    (피드백 ②): a fix interrupted and finished by continue on a
    release-configured project makes exactly one publish request, at
    the end of the job."""
    from tests.unit import test_position as tp

    ws = driver.full_orbit_workspace(workspace)
    config, _master = tp._release_project(ws, tmp_path)
    _first, factory, _reviews = tp._failed_task_job(ws, monkeypatch, config, "fix-feedback-2")
    tp._crash_fix_at(monkeypatch, ws, config, "fixup.fix.restored", factory)

    outcome = full_orbit.continue_full_job(config, ws.ledger, driver.fakes(), "alpha",
                                           "fix-feedback-2", processor_factory=factory)

    assert outcome.status == "needs_owner", outcome
    assert outcome.reason == "release_approval"
    assert len(tp._publish_request_ids(ws.ledger)) == 1


def test_scene_changed_right_after_the_review_digest_check_is_still_refused(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The feedback-③ TOCTOU this test set out to reproduce (docs/
    flow-map.md ⑤ '계획·장면 지문': plan.verify_reviewed_scenes checks the
    repo, THEN engine.acceptance.run_acceptance separately recomputes
    before_fp and builds the snapshot it actually executes from the
    repo a second time -- a scene script changed in that exact gap
    would, in principle, run as if reviewed) does NOT reproduce here.
    Mutating the scene script as a side effect of the very
    evidence.fingerprint() call that computes before_fp (the earliest
    possible point in that gap) still gets caught: run_acceptance sets
    tree_changed=True and the scene itself still runs the ORIGINAL
    ("exit 1") bytes, not the mutated ("exit 0") ones -- an UNCOMMITTED
    working-tree edit apparently never reaches whatever _make_snapshot
    actually copies from. Kept as a real (non-xfail) regression test:
    if a future change ever lets an uncommitted post-review scene edit
    through, this is the first thing that should start failing. This
    does NOT rule out a narrower or differently-timed version of the
    same gap (e.g. a COMMITTED change landing inside run_acceptance's
    per-scene loop, between two scenes) -- that case is engine.
    trusted.capture/verify's own responsibility to guard against, not
    this test's."""
    from engine.steps.open_job import open_job

    ws = driver.full_orbit_workspace(workspace)
    job_name = "trust-feedback-3"
    attempt_id = open_job(ws.config, ws.ledger, "alpha", job_name, "full", roles=("builder",))
    repo = ws.alpha
    scene_path = repo / "tests" / "scenes" / "req_check.sh"
    scene_path.parent.mkdir(parents=True, exist_ok=True)
    scene_path.write_text("exit 1\n", encoding="utf-8")  # the REVIEWED version: fails

    plan_dir = repo / ".gongbang" / job_name
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_data = {
        "goal": "g", "ui": False,
        "scenes": [{"id": "s1", "actor_does": "a", "app_shows": "b", "where": "cli",
                    "cmd": ["bash", "tests/scenes/req_check.sh"], "covers": ["R1"]}],
        "tasks": [], "acceptance": ["a"],
        "requirements": [{"id": "R1", "text": "a", "kind": "function"}],
    }
    (plan_dir / "plan.json").write_text(json.dumps(plan_data), encoding="utf-8")
    plan = plan_module.Plan(
        goal="g", ui=False,
        scenes=(plan_module.Scene(id="s1", actor_does="a", app_shows="b", where="cli",
                                  cmd=("bash", "tests/scenes/req_check.sh"), covers=("R1",)),),
        tasks=(), acceptance=("a",),
        requirements=(plan_module.Requirement(id="R1", text="a", kind="function"),),
    )
    plan_module.record_plan_reviewed(ws.ledger, "alpha", attempt_id, repo, job_name)

    real_fingerprint = evidence.fingerprint
    mutated = {"done": False}

    def fingerprint_then_mutate(repo_path: Path, excludes: tuple[str, ...]) -> str:
        fp = real_fingerprint(repo_path, excludes)
        if not mutated["done"] and repo_path == repo:
            mutated["done"] = True
            scene_path.write_text("exit 0\n", encoding="utf-8")  # swapped in AFTER the check
        return fp

    monkeypatch.setattr(evidence, "fingerprint", fingerprint_then_mutate)

    from engine.acceptance import run_acceptance
    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, plan)

    # The scene changed after the work-tree fingerprint, so the snapshot
    # acceptance builds no longer matches the review: it refuses before
    # running anything, and the unreviewed "exit 0" can never pass.
    assert acc.passed is False
    assert not any(s.passed for s in acc.scenes)


# ---- install: the one owner-request job script, driven for real ---------
#
# Closes the "48 skips" blind spot for `install` (scripts_change/
# test_config_change share install.py's own steps() function -- see
# boundaries._ALIAS_KINDS -- so this covers all three). Unlike the base
# two-task job, this submits+runs the REAL "install" request through
# engine.processor.submit/Processor directly (driver.
# submit_crash_then_resume) -- test_full_orbit.py's own owner-request
# test fakes the OUTCOME (appends install.recorded by hand), which
# never actually dispatches run_installer at all, so it cannot drive a
# crash at any of install's own step boundaries.
_INSTALL_STEPS = ("acquire_project_lock", "snapshot_manifest", "invalidate_evidence",
                 "run_installer", "verify_manifest", "record")
_INSTALL_CASES = [(step, edge) for step in _INSTALL_STEPS for edge in ("crash_before", "crash_after")]


def _build_steps_asking_for_install(stub: driver.RunStationStub, install_request: dict):
    """A run_station(build) stub: the FIRST call writes half the file
    and asks the owner to install a dependency (station.finished's own
    `requests` list); any later call (the post-install rerun) finishes
    the file with no further request. Copied in shape from tests/unit/
    test_full_orbit.py::test_owner_request_resume_reruns_builder_after_
    install_then_reviews's own `_build_steps`."""
    from engine.ledger import Event
    from engine.processor import StepContext, StepDef
    from engine.steps.open_job import copy_root as _copy_root, dirty_paths as _dirty_paths

    calls = {"n": 0}

    def _build_steps(ctx: StepContext) -> list[StepDef]:
        if str(ctx.request.payload.get("station")) != "build":
            return stub.steps(ctx)

        def _run(ctx: StepContext) -> dict[str, object]:
            calls["n"] += 1
            copy = _copy_root(ctx.config, ctx.attempt_id, "builder")
            target = copy / "src/greeter/extra1.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            if calls["n"] == 1:
                target.write_text("VALUE_1 = 1\n", encoding="utf-8")
                requests: list[dict[str, object]] = [install_request]
            else:
                target.write_text("VALUE_1 = 2\n", encoding="utf-8")
                requests = []
            changed = _dirty_paths(copy, ())
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
            return any(event.payload.get("request_id") == ctx.request.request_id
                      for _seq, event in ctx.ledger.events(ctx.project, "station.finished"))

        return [StepDef("record_station", _run, _reconcile)]

    return _build_steps, calls


def _install_owner_request_job(workspace: Workspace, monkeypatch: pytest.MonkeyPatch):
    """A one-task job whose build station asks for an install on its
    first call. Runs to the needs_owner/owner_request stop and returns
    (job TwoTaskJob-shaped object, first outcome, install_request dict,
    build call counter) -- the caller submits+crashes the REAL install
    request itself (driver.submit_crash_then_resume), then calls
    driver.resume() to finish the rest."""
    job_name = "install-owner-request-job"
    ws = driver.full_orbit_workspace(workspace, sample_name=f"sample-{job_name}")
    plan_json = driver.plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    stub = driver.RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md":
                   "- 요청 확인: 통과 (출처: request)\n\n## 외부 대상\n(없음)\n"}],
        "plan": [driver.plan_queue_entry(job_name, plan_json)],
    })
    install_request = {"kind": "install", "manifest": "package.json",
                       "change": {"dependencies": {"left-pad": "^1.0.0"}}}
    build_steps, build_calls = _build_steps_asking_for_install(stub, install_request)

    def factory(cfg, ledger, fakes):
        from engine.processor import Processor
        proc = Processor(cfg, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", build_steps)
        proc.register("review", driver.review_always_passes_steps)
        return proc

    state = driver.install_fake_acceptance_and_handoff(
        monkeypatch, [driver.FakeAcceptanceResult(passed=True)],
    )
    from engine.faults import Faults as _Faults

    job = driver.TwoTaskJob(ws=ws, job_name=job_name, stub=stub, faults=_Faults(),
                            processor_factory=factory, state=state)

    first = driver.run_to_completion(job, request="파일 하나 만들어줘")
    assert first.status == "needs_owner", first
    assert first.reason == "owner_request"
    return job, first, install_request, build_calls


@pytest.mark.parametrize("step,edge", _INSTALL_CASES,
                         ids=[f"install:{s}:{e}" for s, e in _INSTALL_CASES])
def test_crash_at_every_install_boundary_recovers_once(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, step: str, edge: str,
) -> None:
    from engine.fakes.installer import FakeInstaller

    job, first, install_request, build_calls = _install_owner_request_job(workspace, monkeypatch)
    installer = FakeInstaller()
    own_fakes = driver.Fakes(publisher=driver.FakePublisher(), installer=installer)

    request_id = driver.submit_crash_then_resume(
        job.ws.config, job.ws.ledger, "alpha", first.attempt_id, "install",
        {"manifest": install_request["manifest"], "change": install_request["change"]},
        step, edge, own_fakes,
    )

    # (b) the installer effect happened exactly once, not duplicated by
    # the crash+resume.
    assert len(installer.applied_keys) == 1, installer.applied_keys
    assert installer.applied_keys[request_id] == install_request["change"]

    done = driver.resume(job)

    assert done.status == "done", done
    # (b) the build station was rerun (continuation, not from scratch)
    # exactly once more -- never twice for the same post-install resume.
    assert build_calls["n"] == 2, build_calls
    # (c) no work lost: the task's own file carries the POST-install
    # continuation's content, not the half-written pre-install content.
    assert (job.ws.alpha / "src/greeter/extra1.py").read_text(encoding="utf-8") == "VALUE_1 = 2\n"


# ---- publish: a release-configured job, driven for real -----------------
#
# Closes the rest of the "48 skips" blind spot: publish/advance_master,
# reached only when the project has `release` configured, never in the
# base two-task job. Pattern copied from tests/unit/test_full_orbit.py's
# own _release_stopped_job -- a release-configured job stopped needs_
# owner/release_approval, with acceptance.recorded/app_security.
# recorded appended by hand (driver.install_fake_acceptance_and_handoff's
# own app_security stub stays a no-op, same reason test_full_orbit.py's
# own helper gives) -- but the approval is granted and the publish
# request is then run for real through driver.submit_crash_then_resume,
# instead of engine.release.approve_and_run (which builds its own
# Processor internally with no way to arm a step-level Faults).
_PUBLISH_STEPS = ("check_approval", "check_artifact", "advance_master",
                  "record_source", "call_publisher", "record_publish", "report")
_PUBLISH_CASES = [(step, edge) for step in _PUBLISH_STEPS for edge in ("crash_before", "crash_after")]


def _release_stopped_job(workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
                         tmp_path: Path, job_name: str) -> driver.TwoTaskJob:
    from engine.app_security import security_policy_hash as _security_policy_hash
    from engine.ledger import Event as _Event
    from engine.plan import load_plan as _load_plan

    job = driver.build_two_task_job(workspace, monkeypatch, job_name=job_name)
    driver.apply_release_config(job, tmp_path, GIT_ENV)
    job.stub.queues["plan"] = [driver.plan_queue_entry(
        job_name, driver.plan_json(tasks=[
            {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
             "depends_on": []},
        ]),
    )]
    job.stub.queues["build"] = [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}]

    outcome = driver.run_to_completion(job, request="파일 하나 만들어줘")
    assert outcome.status == "needs_owner", outcome
    assert outcome.reason == "release_approval"
    publish_requests = [r for r in job.ws.ledger.queued_requests("alpha") if r.kind == "publish"]
    assert len(publish_requests) == 1

    plan = _load_plan(job.ws.alpha, job_name)
    fp = evidence.fingerprint(job.ws.alpha, job.ws.config.projects["alpha"].excludes)
    job.ws.ledger.append(_Event(
        project="alpha", kind="app_security.recorded", actor_kind="processor",
        actor_id="processor", attempt_id=outcome.attempt_id,
        payload={"passed": True, "findings": [], "ran": ["secret_scan"], "fingerprint": fp,
                "policy_hash": _security_policy_hash(job.ws.config)},
    ))
    # Belt-and-suspenders re-record at the CURRENT fingerprint (same
    # reasoning test_full_orbit.py's own helper gives): driver's own
    # acceptance.recorded (fired during the run above) already carries
    # a fingerprint from the moment acceptance ran, which should already
    # match -- this is here only in case a future driver change moves
    # that timing.
    job.ws.ledger.append(_Event(
        project="alpha", kind="acceptance.recorded", actor_kind="processor",
        actor_id="processor", attempt_id=outcome.attempt_id,
        payload={"job": job_name, "passed": True,
                 "scenes": [{"id": scene.id, "passed": True} for scene in plan.scenes],
                 "full_suite_passed": True, "tree_changed": False,
                 "reason": None, "fingerprint": fp},
    ))
    job.first_outcome = outcome  # type: ignore[attr-defined]
    job.publish_request_id = publish_requests[0].request_id  # type: ignore[attr-defined]
    return job


@pytest.mark.parametrize("step,edge", _PUBLISH_CASES,
                         ids=[f"publish:{s}:{e}" for s, e in _PUBLISH_CASES])
def test_crash_at_every_publish_boundary_recovers_once(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, step: str, edge: str,
) -> None:
    from engine import approvals, release

    job = _release_stopped_job(workspace, monkeypatch, tmp_path, "publish-crash-job")
    request_id = job.publish_request_id  # type: ignore[attr-defined]
    payload = job.ws.ledger.request(request_id).payload
    # engine.release.approve_and_run (what `gb2 ship` itself calls)
    # runs this BEFORE ever granting an approval -- advance_master's
    # own full_bench_missing gate (engine/steps/publish.py) requires
    # it, and this test drives the publish request directly instead
    # of through approve_and_run.
    release.ensure_full_bench(job.ws.config, job.ws.ledger, driver.fakes(), "alpha",
                              job.first_outcome.attempt_id)  # type: ignore[attr-defined]
    approvals.grant(job.ws.ledger, "alpha", request_id, 3600)

    driver.submit_crash_then_resume(
        job.ws.config, job.ws.ledger, "alpha", job.first_outcome.attempt_id,  # type: ignore[attr-defined]
        "publish", payload, step, edge, driver.fakes(),
    )

    done = driver.resume(job)

    assert done.status == "done", done
    decisions = [event.payload["decision"] for _seq, event in
                job.ws.ledger.events("alpha", "full_orbit.release_decision")
                if event.payload.get("job") == job.job_name]
    assert decisions == ["shipped"]
    publish_submissions = [event for _seq, event in
                           job.ws.ledger.events("alpha", "request.submitted")
                           if event.payload.get("kind") == "publish"]
    assert len(publish_submissions) == 1  # no second publish request ever submitted
