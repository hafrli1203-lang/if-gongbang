"""T9d -- 결함 26, sample-app build stage): task T2 removed a
field it owned (limits.ts/content.ts/facts.ts/readout.ts/facts/page.tsx),
but the only consumer of that field (src/lib/production/copy.ts) was in
NO task's `owns` -- the code review (P1) found `copy.ts` still read the
removed field and broke with `undefined.split`, but a build task's own
write outside its `owns` is silently DROPPED by merge
(engine.application.steps.merge._owner_prefixes' own per-task
narrowing), never written and never a loud failure, so the exact same
P1 came back on the second `gb2 fix` too. `gb2 continue --from plan`
cannot be used once the build stage has started.

`gb2 fix <app> --also-own <path>` (engine.application.repair.
add_also_own) is the fix: the owner widens the task currently being
fixed with the extra path, recorded as its own ledger event (plan.json
itself stays frozen evidence -- never edited), and the next `gb2 fix`
folds it into the replacement attempt's own run_station/merge "owns"
(engine.application.repair._fix_spec).

  test_26_*  (a) a review that fails on a file outside the task's owns
             -> after add_also_own + fix(), the builder's change to
             that path passes merge and the job finishes "done".
  test_26_refuse_*  (b) the four refusal cases: a path another task
             already owns, a syntactically unsafe path (absolute/
             parent-reference/hidden component), an engine record path
             (.gongbang/, tests/scenes/), and no task currently being
             fixed at all.
  test_26_compose_fix_task_*  (c) the fix instructions and the general
             builder prompt both carry the new sentences verbatim.

No live AI: run_station/review are stubbed at the Processor level,
exactly like tests/unit/test_full_orbit.py (whose own helpers this file
reuses via `import tests.unit.test_full_orbit as _tfo`, the same
whole-module-import convention tests/acceptance/test_t9c_force_reopen.py
already uses so tools/arch_check.py's own check_private_imports never
flags it) -- merge/bench/commit/report/close are the real
engine.application.steps.* implementations, against a real sample app
repo, so merge genuinely drops (or, once widened, keeps) the
out-of-owns write for real."""
from __future__ import annotations

import pytest

import tests.unit.test_full_orbit as _tfo
from engine import fixup, full_orbit, position
from engine.application.job_state import STAGE_TASK_OWNER, load_position, outstanding_requests
from engine.application.processor import StepContext, StepDef, StepFailed
from engine.application.repair_target import (
    EDIT_REQUEST_KIND, HANDOFF_KIND, HANDOFF_NOTE_PREFIX, FixTarget,
)
from engine.application.stage_prompts import builder_task_text
from engine.domain.plan import Plan, Task
from engine.domain.policy import OWNS_SCOPE_REQUEST_RULE
from engine.steps.open_job import copy_root, dirty_paths
from tests.conftest import Workspace

_FACTS_NO_TARGET = "- 요청 확인: 통과 (출처: request)\n\n## 외부 대상\n(없음)\n"

_TASK_OWNED_PATH = "src/greeter/task_owns_extra.py"
_OUTSIDE_OWNED_PATH = "src/other/outside_owns.py"


def _review_queue_steps(outcomes: list[tuple[bool, list]]):
    """Pops one (passed, findings) pair per review request -- like
    _tfo._review_always_passes_steps, but controllable, so this file
    can reproduce 결함 26's own review-fails-then-passes sequence
    without any real reviewer CLI."""
    from engine import evidence

    calls: list[bool] = []

    def steps(_ctx: StepContext) -> list[StepDef]:
        def _run(ctx: StepContext) -> dict[str, object]:
            passed, findings = outcomes[len(calls)]
            calls.append(passed)
            project_config = ctx.config.projects[ctx.project]
            fp = evidence.fingerprint(project_config.repo, project_config.excludes)
            evidence.record(
                ctx.ledger, ctx.project, ctx.attempt_id, "review", fp,
                payload={"passed": passed, "request_id": ctx.request.request_id,
                         "findings": findings, "reviewer_passed": passed, "coverage": "full"},
            )
            if not passed:
                raise StepFailed("review_not_passed")
            return {}

        def _reconcile(ctx: StepContext) -> bool:
            for row in ctx.ledger.evidence(ctx.attempt_id, "review"):
                if row.payload.get("request_id") == ctx.request.request_id:
                    return True
            return False

        return [StepDef("record_review", _run, _reconcile)]

    return steps, calls


def _processor_factory_with_review(stub: "_tfo._RunStationStub", review_steps):
    from engine.application.processor import Processor

    def factory(config, ledger, fakes) -> Processor:
        proc = Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", stub.steps)
        proc.register("review", review_steps)
        return proc

    return factory


# ---- (a) review fails outside owns -> add_also_own -> fix() lands it --

def test_26_also_own_lets_a_fix_actually_land_a_write_outside_the_original_owns(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _tfo._full_orbit_workspace(workspace)
    job_name = "also-own-job"
    plan_json = _tfo._plan_json(tasks=[
        {"id": "t1", "title": "필드 정리", "detail": "",
         "owns": [_TASK_OWNED_PATH], "depends_on": []},
    ])
    stub = _tfo._RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": _FACTS_NO_TARGET}],
        "plan": [_tfo._plan_queue_entry(job_name, plan_json)],
        "build": [
            # Attempt 1: the owned file is updated; the builder ALSO
            # tries to leave a trail in the file the review will flag
            # -- outside this task's owns, so merge must silently drop
            # it (결함 26's own mechanism).
            {_TASK_OWNED_PATH: "VALUE_A = 1\n",
             _OUTSIDE_OWNED_PATH: "VALUE_COPY = 1  # stale\n"},
            # Attempt 2 (the fix): the SAME two files, this time with
            # the consumer actually updated -- only landable once
            # --also-own has widened this task's owns.
            {_TASK_OWNED_PATH: "VALUE_A = 1\n",
             _OUTSIDE_OWNED_PATH: "VALUE_COPY = 2  # fixed\n"},
        ],
    })
    review_steps, review_calls = _review_queue_steps([
        # engine.application.pipeline_planning's own STAGE_PLAN_REVIEW
        # also goes through a "review" request, before the build task's
        # own -- this queue covers that one first.
        (True, []),
        (False, [{"severity": "P1", "reason": f"{_OUTSIDE_OWNED_PATH} 도 함께 고쳐야 한다"}]),
        (True, []),
    ])
    factory = _processor_factory_with_review(stub, review_steps)
    _tfo._install_fake_acceptance_and_handoff(monkeypatch, [_tfo._FakeAcceptanceResult(passed=True)])

    first = full_orbit.run_full_job(
        ws.config, ws.ledger, _tfo._fakes(), "alpha", job_name, "owns 밖 결과 반영 확인",
        processor_factory=factory,
    )

    assert first.status == "failed", first
    assert review_calls == [True, False]
    # merge dropped the out-of-owns write for real -- this IS 결함 26's
    # own failure mode, reproduced against the real merge step.
    assert not (ws.alpha / _OUTSIDE_OWNED_PATH).exists()
    assert (ws.alpha / _TASK_OWNED_PATH).read_text(encoding="utf-8") == "VALUE_A = 1\n"

    granted = fixup.add_also_own(ws.config, ws.ledger, "alpha", (_OUTSIDE_OWNED_PATH,))
    assert granted == (_OUTSIDE_OWNED_PATH,)

    def fakes_for(_attempt_id: str):
        return _tfo._fakes()

    result = fixup.fix(ws.config, ws.ledger, "alpha", "", fakes_for,
                       processor_factory=factory)
    full_outcome = position.resume_after_fix(
        ws.config, ws.ledger, fakes_for, "alpha", result, processor_factory=factory,
    )

    assert review_calls == [True, False, True]
    assert result.outcome.status == "done", result.outcome
    assert full_outcome is not None and full_outcome.status == "done", full_outcome
    # The path --also-own granted now passes merge for real.
    assert (ws.alpha / _OUTSIDE_OWNED_PATH).read_text(encoding="utf-8") == (
        "VALUE_COPY = 2  # fixed\n"
    )


# ---- (b) the four refusal cases ---------------------------------------

def _two_task_plan() -> Plan:
    return Plan(
        goal="g", ui=False, scenes=(),
        tasks=(
            Task(id="t1", title="x", detail="", owns=("src/a.py",), depends_on=()),
            Task(id="t2", title="y", detail="", owns=("src/other/t2_owned.py",), depends_on=()),
        ),
        acceptance=(), requirements=(),
    )


def test_26_refuse_a_path_another_task_already_owns(workspace: Workspace) -> None:
    from engine.application.repair_target import FixupError, validate_also_own

    with pytest.raises(FixupError, match="also_own_owned_by_other_task"):
        validate_also_own(workspace.ledger, "alpha", "jobx", _two_task_plan(), "t1",
                          ("src/other/t2_owned.py",))


def test_26_refuse_an_absolute_path(workspace: Workspace) -> None:
    from engine.application.repair_target import FixupError, validate_also_own

    with pytest.raises(FixupError, match="also_own_absolute"):
        validate_also_own(workspace.ledger, "alpha", "jobx", _two_task_plan(), "t1",
                          ("/etc/passwd",))


def test_26_refuse_a_parent_reference_path(workspace: Workspace) -> None:
    from engine.application.repair_target import FixupError, validate_also_own

    with pytest.raises(FixupError, match="also_own_parent_reference"):
        validate_also_own(workspace.ledger, "alpha", "jobx", _two_task_plan(), "t1",
                          ("../../etc/passwd",))


def test_26_refuse_a_hidden_component_path(workspace: Workspace) -> None:
    from engine.application.repair_target import FixupError, validate_also_own

    with pytest.raises(FixupError, match="also_own_hidden_component"):
        validate_also_own(workspace.ledger, "alpha", "jobx", _two_task_plan(), "t1",
                          (".secret/file.txt",))


def test_26_refuse_an_engine_record_path_under_tests_scenes(workspace: Workspace) -> None:
    from engine.application.repair_target import FixupError, validate_also_own

    with pytest.raises(FixupError, match="also_own_engine_record"):
        validate_also_own(workspace.ledger, "alpha", "jobx", _two_task_plan(), "t1",
                          ("tests/scenes/evil.py",))


def test_26_refuse_an_engine_record_path_under_gongbang(workspace: Workspace) -> None:
    """.gongbang/ is ALSO a hidden-component path (it starts with "."),
    so this is refused by that earlier, more specific check -- still
    refused either way, which is all this category requires."""
    from engine.application.repair_target import FixupError, validate_also_own

    with pytest.raises(FixupError):
        validate_also_own(workspace.ledger, "alpha", "jobx", _two_task_plan(), "t1",
                          (".gongbang/jobx/facts.md",))


def test_26_refuse_no_task_currently_being_fixed(workspace: Workspace) -> None:
    """A project that was never even opened has no failed attempt at
    all -- find_fix_target's own refusal, surfaced through
    add_also_own exactly as it is through fix() itself."""
    with pytest.raises(fixup.FixupError):
        fixup.add_also_own(workspace.config, workspace.ledger, "alpha", ("src/x.py",))


_T2_OWNED_PATH = "src/other/t2_build_owned.py"


def test_26_a_finished_earlier_task_path_can_be_granted_to_the_task_being_fixed(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """a review finding: find_fix_target.add_also_own calls
    validate_also_own with no active_task_ids at all, so a FINISHED
    task's plan.json `owns` still blocks a later --also-own grant for
    the SAME path -- unlike approve_pending_edit (결함 28's own entry
    point), which already passes `pos.remaining_task_ids` and so
    already releases a finished task's owns (결함 34/38).

    Repro: t1 (owns _TASK_OWNED_PATH) finishes and commits normally;
    t2's build then writes outside its own owns, into
    _TASK_OWNED_PATH -- the exact path t1 already finished with. The
    owner must still be able to --also-own it onto t2."""
    ws = _tfo._full_orbit_workspace(workspace)
    job_name = "also-own-finished-task-job"
    plan_json = _tfo._plan_json(tasks=[
        {"id": "t1", "title": "첫 작업", "detail": "",
         "owns": [_TASK_OWNED_PATH], "depends_on": []},
        {"id": "t2", "title": "둘째 작업", "detail": "",
         "owns": [_T2_OWNED_PATH], "depends_on": []},
    ])
    stub = _tfo._RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": _FACTS_NO_TARGET}],
        "plan": [_tfo._plan_queue_entry(job_name, plan_json)],
        "build": [
            {_TASK_OWNED_PATH: "VALUE_A = 1\n"},  # t1: lands cleanly.
            # t2's attempt 1: reaches outside its own owns into t1's
            # already-finished path -- merge drops it, review flags it.
            {_T2_OWNED_PATH: "VALUE_B = 1\n", _TASK_OWNED_PATH: "VALUE_A = 2  # t2 touch\n"},
            # t2's fix attempt: same write, landable once widened.
            {_T2_OWNED_PATH: "VALUE_B = 1\n", _TASK_OWNED_PATH: "VALUE_A = 2  # t2 touch\n"},
        ],
    })
    review_steps, review_calls = _review_queue_steps([
        (True, []),  # plan review
        (True, []),  # t1's review
        (False, [{"severity": "P1", "reason": f"{_TASK_OWNED_PATH} 도 함께 고쳐야 한다"}]),
        (True, []),  # t2's fix review
    ])
    factory = _processor_factory_with_review(stub, review_steps)
    _tfo._install_fake_acceptance_and_handoff(monkeypatch, [_tfo._FakeAcceptanceResult(passed=True)])

    first = full_orbit.run_full_job(
        ws.config, ws.ledger, _tfo._fakes(), "alpha", job_name, "둘째 작업이 첫 파일도 고친다",
        processor_factory=factory,
    )

    assert first.status == "failed", first
    assert review_calls == [True, True, False]
    assert (ws.alpha / _TASK_OWNED_PATH).read_text(encoding="utf-8") == "VALUE_A = 1\n"

    # RED (before the fix): also_own_owned_by_other_task -- t1 is
    # finished, its plan.json owns must no longer block t2's grant.
    granted = fixup.add_also_own(ws.config, ws.ledger, "alpha", (_TASK_OWNED_PATH,))
    assert granted == (_TASK_OWNED_PATH,)

    def fakes_for(_attempt_id: str):
        return _tfo._fakes()

    result = fixup.fix(ws.config, ws.ledger, "alpha", "", fakes_for,
                       processor_factory=factory)
    full_outcome = position.resume_after_fix(
        ws.config, ws.ledger, fakes_for, "alpha", result, processor_factory=factory,
    )

    assert review_calls == [True, True, False, True]
    assert result.outcome.status == "done", result.outcome
    assert full_outcome is not None and full_outcome.status == "done", full_outcome
    assert (ws.alpha / _TASK_OWNED_PATH).read_text(encoding="utf-8") == "VALUE_A = 2  # t2 touch\n"


# ---- (c) the instructions/prompt carry the new sentences verbatim ----

def test_26_compose_fix_task_lists_the_owner_added_paths() -> None:
    from engine.application.repair_target import compose_fix_task

    target = FixTarget(
        attempt_id="a1", job_id="j1", job_name="jobx", builder_role="builder",
        reviewer_role="reviewer", original_task="원 요청", failing_kind="review",
        failing_reason=None, review_findings=[], bench_tail=None,
    )

    text = compose_fix_task(target, "", also_own=(_OUTSIDE_OWNED_PATH,))

    assert f"Paths the owner added to this task: {_OUTSIDE_OWNED_PATH}" in text


def test_26_compose_fix_task_omits_the_section_when_nothing_was_added() -> None:
    from engine.application.repair_target import compose_fix_task

    target = FixTarget(
        attempt_id="a1", job_id="j1", job_name="jobx", builder_role="builder",
        reviewer_role="reviewer", original_task="원 요청", failing_kind="review",
        failing_reason=None, review_findings=[], bench_tail=None,
    )

    text = compose_fix_task(target, "", also_own=())

    assert "Paths the owner added to this task" not in text


def test_26_builder_prompt_tells_the_builder_to_report_instead_of_reaching_outside_owns() -> None:
    task = Task(id="t1", title="제목", detail="내용", owns=("src/a.py",), depends_on=())

    text = builder_task_text(task, "목표")

    assert OWNS_SCOPE_REQUEST_RULE in text


# ---- 결함 28 (sample-app T4,: a pending kind="edit" request --
# find_fix_target can never see it (the attempt never failed at all, it
# is still `active`, simply stopped at STAGE_TASK_OWNER). `gb2 fix <app>
# --also-own <path>` must still be the one command that approves it
# (engine.application.repair.approve_pending_edit). ----------------------

_EDIT_PATH_A = "src/other/edit_a.py"
_EDIT_PATH_B = "src/other/edit_b.py"
_EDIT_PATH_NOT_REQUESTED = "src/other/not_requested.py"


def _station_stub_with_requests(queues: dict[str, list[dict[str, str]]], target_station: str,
                                reported: list[dict[str, object]], *, on_call: int = 1):
    """Like tests.unit.test_full_orbit._station_stub_with_one_owner_request,
    but reports every dict in `reported` (not just one) on the `on_call`-th
    call for `target_station` -- 결함 28's own scenario needs TWO
    pending edit requests in the same stop, not one. `on_call` (결함 46)
    lets a caller put that stop on a LATER call to the same station --
    e.g. the acceptance-fix round's own synthesized task, which is
    always the second (or later) "build" call, never the first."""
    stub = _tfo._RunStationStub(queues)
    base_steps = stub.steps
    calls = {"n": 0}

    def steps(ctx: StepContext) -> list[StepDef]:
        station = str(ctx.request.payload.get("station"))
        if station != target_station:
            return base_steps(ctx)
        calls["n"] += 1
        if calls["n"] != on_call:
            return base_steps(ctx)

        def _run(ctx: StepContext) -> dict[str, object]:
            role = str(ctx.request.payload.get("role"))
            stub.calls.append((station, role))
            ctx.ledger.append(_tfo.Event(
                project=ctx.project, kind="station.finished", actor_kind="processor",
                actor_id="processor", attempt_id=ctx.attempt_id,
                payload={
                    "request_id": ctx.request.request_id, "station": station, "role": role,
                    "ok": True, "summary": "stub", "cost_usd": 0.0, "turns": 1,
                    "changed_paths": [], "requests": list(reported),
                },
            ))
            return {}

        def _reconcile(ctx: StepContext) -> bool:
            for _seq, event in ctx.ledger.events(ctx.project, "station.finished"):
                if event.payload.get("request_id") == ctx.request.request_id:
                    return True
            return False

        return [StepDef("record_station", _run, _reconcile)]

    return stub, steps


def _edit_requests_stop(workspace: Workspace, job_name: str):
    """Runs a single-task plan to its own needs_owner stop on TWO
    pending kind="edit" requests (`_EDIT_PATH_A`/`_EDIT_PATH_B`) -- the
    build task's own owns-scope report (OWNS_SCOPE_REQUEST_RULE). The
    SAME processor_factory, if driven again after an approval, writes
    the owned file plus both edit paths and reports no further
    requests. Returns (workspace, first FullOutcome, processor_factory)."""
    ws = _tfo._full_orbit_workspace(workspace)
    plan_json = _tfo._plan_json(tasks=[
        {"id": "t1", "title": "필드 정리", "detail": "",
         "owns": [_TASK_OWNED_PATH], "depends_on": []},
    ])
    queues = {
        "facts": [{f".gongbang/{job_name}/facts.md": _FACTS_NO_TARGET}],
        "plan": [_tfo._plan_queue_entry(job_name, plan_json)],
        "build": [
            {_TASK_OWNED_PATH: "VALUE_A = 1\n", _EDIT_PATH_A: "EDIT_A = 1\n",
             _EDIT_PATH_B: "EDIT_B = 1\n"},
        ],
    }
    edit_requests = [
        {"kind": EDIT_REQUEST_KIND, "path": _EDIT_PATH_A, "reason": "copy.ts 도 고쳐야 한다"},
        {"kind": EDIT_REQUEST_KIND, "path": _EDIT_PATH_B, "reason": "readout.ts 도 고쳐야 한다"},
    ]
    stub, steps_fn = _station_stub_with_requests(queues, "build", edit_requests)
    review_steps, review_calls = _review_queue_steps([(True, []), (True, [])])

    def factory(config, ledger, fakes):
        proc = _tfo.Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", steps_fn)
        proc.register("review", review_steps)
        return proc

    first = full_orbit.run_full_job(
        ws.config, ws.ledger, _tfo._fakes(), "alpha", job_name, "owns 밖 결과 반영 확인",
        processor_factory=factory,
    )
    return ws, first, factory, review_calls


def test_28_edit_request_stop_records_two_pending_edit_requests(
    workspace: Workspace,
) -> None:
    job_name = "edit-stop-job"
    ws, first, _factory, _review_calls = _edit_requests_stop(workspace, job_name)

    assert first.status == "needs_owner", first
    pos = load_position(ws.ledger, "alpha", job_name)
    assert pos is not None and pos.stage == STAGE_TASK_OWNER
    kinds_paths = {(r.get("kind"), r.get("path")) for r in pos.pending_requests}
    assert kinds_paths == {
        (EDIT_REQUEST_KIND, _EDIT_PATH_A), (EDIT_REQUEST_KIND, _EDIT_PATH_B),
    }
    # find_fix_target can never see this stop (nothing failed) --
    # add_also_own/fix() must refuse it exactly the way the real
    # incident did.
    with pytest.raises(fixup.FixupError, match="no_failed_attempt"):
        fixup.add_also_own(ws.config, ws.ledger, "alpha", (_EDIT_PATH_A,))


def test_28_approve_pending_edit_refuses_a_path_that_was_not_requested(
    workspace: Workspace,
) -> None:
    job_name = "edit-stop-refuse-job"
    ws, first, _factory, _review_calls = _edit_requests_stop(workspace, job_name)
    assert first.status == "needs_owner", first

    def fakes_for(_attempt_id: str):
        return _tfo._fakes()

    with pytest.raises(fixup.FixupError, match=f"also_own_not_requested:{_EDIT_PATH_NOT_REQUESTED}"):
        fixup.approve_pending_edit(ws.config, ws.ledger, "alpha",
                                   (_EDIT_PATH_NOT_REQUESTED,), fakes_for)

    # Nothing was recorded -- the same two edit requests are still
    # outstanding, unchanged.
    pos = load_position(ws.ledger, "alpha", job_name)
    assert len(outstanding_requests(ws.ledger, "alpha", pos)) == 2


def test_28_also_own_approves_both_pending_edit_requests_and_resumes_the_same_attempt(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_name = "edit-stop-approve-job"
    ws, first, factory, review_calls = _edit_requests_stop(workspace, job_name)
    assert first.status == "needs_owner", first
    stopped_attempt_id = first.attempt_id

    _tfo._install_fake_acceptance_and_handoff(
        monkeypatch, [_tfo._FakeAcceptanceResult(passed=True)],
    )

    def fakes_for(_attempt_id: str):
        return _tfo._fakes()

    outcome = fixup.approve_pending_edit(
        ws.config, ws.ledger, "alpha", (_EDIT_PATH_A, _EDIT_PATH_B), fakes_for,
        processor_factory=factory,
    )

    assert outcome.status == "done", outcome
    # The SAME attempt was reused -- approve_pending_edit never opens a
    # new one (nothing here ever failed).
    assert outcome.attempt_id == stopped_attempt_id
    assert review_calls == [True, True]
    assert (ws.alpha / _TASK_OWNED_PATH).read_text(encoding="utf-8") == "VALUE_A = 1\n"
    assert (ws.alpha / _EDIT_PATH_A).read_text(encoding="utf-8") == "EDIT_A = 1\n"
    assert (ws.alpha / _EDIT_PATH_B).read_text(encoding="utf-8") == "EDIT_B = 1\n"

    # P5: the approved requests never stay pending -- re-reading the
    # (now-finished) job's own position computes no outstanding request
    # for either approved path.
    pos = load_position(ws.ledger, "alpha", job_name)
    if pos is not None and pos.stage == STAGE_TASK_OWNER:
        assert outstanding_requests(ws.ledger, "alpha", pos) == ()


def test_43_decline_resolves_an_edit_request_that_cannot_be_granted(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect 43 (sample-app T8): a builder asked to delete an engine-record
    file (never grantable with --also-own), so the request stayed outstanding
    forever and every resume stopped again in 0.02 min. The owner can now
    decline a pending edit request: recorded, no longer blocks, path not
    granted."""
    job_name = "edit-stop-decline-job"
    ws, first, factory, _review_calls = _edit_requests_stop(workspace, job_name)
    assert first.status == "needs_owner", first

    def fakes_for(_attempt_id: str):
        return _tfo._fakes()

    with pytest.raises(fixup.FixupError, match=f"decline_not_requested:{_EDIT_PATH_NOT_REQUESTED}"):
        fixup.approve_pending_edit(ws.config, ws.ledger, "alpha", (), fakes_for,
                                   declined=(_EDIT_PATH_NOT_REQUESTED,))
    pos = load_position(ws.ledger, "alpha", job_name)
    assert len(outstanding_requests(ws.ledger, "alpha", pos)) == 2

    _tfo._install_fake_acceptance_and_handoff(
        monkeypatch, [_tfo._FakeAcceptanceResult(passed=True)],
    )
    outcome = fixup.approve_pending_edit(
        ws.config, ws.ledger, "alpha", (_EDIT_PATH_A,), fakes_for,
        declined=(_EDIT_PATH_B,), processor_factory=factory,
    )
    assert outcome.status == "done", outcome
    assert (ws.alpha / _EDIT_PATH_A).read_text(encoding="utf-8") == "EDIT_A = 1\n"
    assert not (ws.alpha / _EDIT_PATH_B).exists()  # declined: never granted
    declined = [e.payload for _s, e in ws.ledger.events("alpha", "task.request_declined")]
    assert [d.get("path") for d in declined] == [_EDIT_PATH_B]


# ---- 결함 57 (a live app run,: a build task's own
# pending "install" request (owner_approval.PENDING_KIND_WHITELIST, not
# an owns-scope "edit" report) stops the job at STAGE_TASK_OWNER exactly
# like a 결함 28 edit request does -- but when its `manifest` points
# outside the project's own repo (a shared-kit manifest), `gb2
# approve-install` can never approve it (owner_approval.validate_pending's
# own `_unsafe_string_reason` correctly refuses "path_traversal" -- that
# check is NOT weakened by this fix). Before this fix, `gb2 fix <app>
# --decline <manifest>` fell straight through to NOT_TASK_OWNER_EDIT_STOP
# (approve_pending_edit's own `pending_edit_paths` only ever looked at
# is_owns_scope_request items, never a whitelisted kind's own `manifest`)
# and the classic add_also_own+fix() fallback then raised
# "no_failed_attempt" (the attempt never failed -- it is still active,
# simply stopped) -- leaving the owner with neither an approve path nor a
# decline path: a permanent stop. ----------------------------------------

_SHARED_INSTALL_MANIFEST = "../_shared/kit/package.json"
_SHARED_INSTALL_REQUEST = {
    "kind": "install", "manifest": _SHARED_INSTALL_MANIFEST,
    "change": {"dependencies": {"pg": "8.23.0"}},
}


def _install_request_stop(workspace: Workspace, job_name: str):
    """Runs a single-task plan to its own needs_owner stop on ONE pending
    kind="install" request whose manifest lives outside the project's
    repo -- the same shape `_edit_requests_stop` builds for a kind="edit"
    report, just a whitelisted kind instead."""
    ws = _tfo._full_orbit_workspace(workspace)
    plan_json = _tfo._plan_json(tasks=[
        {"id": "t1", "title": "필드 정리", "detail": "",
         "owns": [_TASK_OWNED_PATH], "depends_on": []},
    ])
    queues = {
        "facts": [{f".gongbang/{job_name}/facts.md": _FACTS_NO_TARGET}],
        "plan": [_tfo._plan_queue_entry(job_name, plan_json)],
        "build": [{_TASK_OWNED_PATH: "VALUE_A = 1\n"}],
    }
    stub, steps_fn = _station_stub_with_requests(
        queues, "build", [_SHARED_INSTALL_REQUEST],
    )
    review_steps, review_calls = _review_queue_steps([(True, []), (True, [])])

    def factory(config, ledger, fakes):
        proc = _tfo.Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", steps_fn)
        proc.register("review", review_steps)
        return proc

    first = full_orbit.run_full_job(
        ws.config, ws.ledger, _tfo._fakes(), "alpha", job_name, "설치가 필요한 결과 반영 확인",
        processor_factory=factory,
    )
    return ws, first, factory, review_calls


def test_57_approve_install_still_refuses_a_manifest_outside_the_repo(
    workspace: Workspace,
) -> None:
    """The security floor this fix must never weaken: validate_pending
    rejects the shared-kit manifest as path_traversal regardless of the
    decline path added below."""
    from engine import owner as owner_module

    job_name = "install-stop-security-job"
    ws, first, _factory, _review_calls = _install_request_stop(workspace, job_name)
    assert first.status == "needs_owner", first

    ok, reason = owner_module.validate_pending(ws.config, "alpha", _SHARED_INSTALL_REQUEST)
    assert (ok, reason) == (False, "path_traversal")


def test_57_decline_resolves_an_install_request_that_can_never_be_approved(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_name = "install-stop-decline-job"
    ws, first, factory, _review_calls = _install_request_stop(workspace, job_name)
    assert first.status == "needs_owner", first
    stopped_attempt_id = first.attempt_id

    pos = load_position(ws.ledger, "alpha", job_name)
    assert pos is not None and pos.stage == STAGE_TASK_OWNER
    assert [r.get("kind") for r in outstanding_requests(ws.ledger, "alpha", pos)] == ["install"]

    def fakes_for(_attempt_id: str):
        return _tfo._fakes()

    # Before this fix: raised NOT_TASK_OWNER_EDIT_STOP (install-kind
    # requests were invisible to approve_pending_edit's own pending set).
    with pytest.raises(fixup.FixupError, match="decline_not_requested:not-the-manifest"):
        fixup.approve_pending_edit(ws.config, ws.ledger, "alpha", (), fakes_for,
                                   declined=("not-the-manifest",))
    pos = load_position(ws.ledger, "alpha", job_name)
    assert len(outstanding_requests(ws.ledger, "alpha", pos)) == 1

    _tfo._install_fake_acceptance_and_handoff(
        monkeypatch, [_tfo._FakeAcceptanceResult(passed=True)],
    )
    outcome = fixup.approve_pending_edit(
        ws.config, ws.ledger, "alpha", (), fakes_for,
        declined=(_SHARED_INSTALL_MANIFEST,), decline_note="곳간 패키지는 별도로 설치한다",
        processor_factory=factory,
    )
    assert outcome.status == "done", outcome
    # The SAME attempt was reused -- nothing here ever failed.
    assert outcome.attempt_id == stopped_attempt_id

    declined = [e.payload for _s, e in ws.ledger.events("alpha", "task.request_declined")]
    assert [d.get("path") for d in declined] == [_SHARED_INSTALL_MANIFEST]
    assert declined[0].get("note") == "곳간 패키지는 별도로 설치한다"

    # P5: the declined install request never stays pending once resolved.
    pos = load_position(ws.ledger, "alpha", job_name)
    if pos is not None and pos.stage == STAGE_TASK_OWNER:
        assert outstanding_requests(ws.ledger, "alpha", pos) == ()


def test_45_decline_note_reaches_the_resumed_worker(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live appX #49: the owner declined a test-file edit and gave a
    direction ("keep the single transaction"), but the resumed builder was
    told only that its scope had been widened -- nothing about the refusal
    or the owner's words, so it would ask for the same edit again."""
    job_name = "edit-stop-decline-note-job"
    ws, first, factory, _review_calls = _edit_requests_stop(workspace, job_name)
    assert first.status == "needs_owner", first

    def fakes_for(_attempt_id: str):
        return _tfo._fakes()

    _tfo._install_fake_acceptance_and_handoff(
        monkeypatch, [_tfo._FakeAcceptanceResult(passed=True)],
    )
    note = "묶음 처리 유지: 기존 시험을 고치지 말고 범위 안에서 방법을 찾는다"
    fixup.approve_pending_edit(
        ws.config, ws.ledger, "alpha", (_EDIT_PATH_A,), fakes_for,
        declined=(_EDIT_PATH_B,), decline_note=note, processor_factory=factory,
    )
    declined = [e.payload for _s, e in ws.ledger.events("alpha", "task.request_declined")]
    assert declined[-1].get("note") == note
    resumed = [str(e.payload) for _s, e in ws.ledger.events("alpha", "request.submitted")
               if e.payload.get("kind") == "run_station"][-1]
    assert _EDIT_PATH_B in resumed and "운영자 거절" in resumed
    assert note in resumed


def test_codex_p2_3_a_later_grant_for_the_same_path_drops_the_earlier_decline() -> None:
    """a review finding: declined_for_attempt returns EVERY decline
    ever recorded for this attempt, with no regard for whether the
    owner later re-requested and GRANTED that same path (--also-own /
    approve_pending_edit, task.owns_extended) after declining it.
    Repro: same attempt declines path P, then later grants P -- the
    resumed worker's own task text must carry ONLY the grant ("직접
    고친다"), never also the stale decline ("고치지 않는다") for the
    SAME path, which rerun_task_owner_stop's own
    declined_followup_note(declined) + edit_approved_followup_task_text
    would otherwise fold in side by side."""
    from types import SimpleNamespace
    from engine.application.repair_target import (
        DECLINED_KIND, OWNS_EXTENDED_KIND, declined_for_attempt,
    )

    events: list[tuple[str, dict[str, object]]] = [
        (DECLINED_KIND, {"job_name": "j", "task_id": "t1", "path": "a.ts", "note": "n1"}),
        (DECLINED_KIND, {"job_name": "j", "task_id": "t1", "path": "b.ts", "note": "n2"}),
        # The owner changes their mind about a.ts and grants it after all.
        (OWNS_EXTENDED_KIND, {"job_name": "j", "task_id": "t1", "paths": ["a.ts"]}),
    ]

    class _Ledger:
        def events(self, project, kind):
            return [(i, SimpleNamespace(payload=p, attempt_id="att-1"))
                    for i, (k, p) in enumerate(events) if k == kind]

    out = declined_for_attempt(_Ledger(), "p", "j", "t1", "att-1")
    # a.ts was later granted -- no longer reported as declined.
    assert ("a.ts", "n1") not in out
    # b.ts was never granted -- still reported.
    assert ("b.ts", "n2") in out


# ---- 결함 46 (appX live #4,: approve_pending_edit's own
# "does this task still exist in plan.json" check compared `t.id ==
# pos.task_id` against plan.tasks directly -- for an edit request
# raised during an ACCEPTANCE-FIX round (pos.fixing=True), pos.task_id
# is the fix round's own synthesized id ("fix-1", pipeline_tasks.py's
# own fix_task), which is NEVER one of plan.tasks's real ids (exactly
# like pipeline_resume_tasks.task_for's own fixing branch never looks
# it up there either) -- so every such approval/decline raised
# "no_task_being_fixed" forever: the owner's `--decline` for appX's
# own tests/reference-kind.test.ts could not even be submitted. ------

def _fix_round_edit_requests_stop(workspace: Workspace, job_name: str,
                                  monkeypatch: pytest.MonkeyPatch):
    """Like `_edit_requests_stop`, but the stop happens on the
    ACCEPTANCE-FIX round's own synthesized task (`fixing=True`, task id
    `"fix-1"`), not the plan's original task: the first attempt
    finishes and FAILS acceptance, and the fix round itself is the one
    that reports the two pending edit requests."""
    ws = _tfo._full_orbit_workspace(workspace)
    plan_json = _tfo._plan_json(tasks=[
        {"id": "t1", "title": "필드 정리", "detail": "",
         "owns": [_TASK_OWNED_PATH], "depends_on": []},
    ])
    queues = {
        "facts": [{f".gongbang/{job_name}/facts.md": _FACTS_NO_TARGET}],
        "plan": [_tfo._plan_queue_entry(job_name, plan_json)],
        "build": [
            # Attempt 1: the original task t1, finishes cleanly.
            {_TASK_OWNED_PATH: "VALUE_A = 1\n"},
            # Attempt 2: the fix round's own synthesized task -- the
            # owned file again, plus the two out-of-owns edit paths it
            # reports instead of writing outright.
            {_TASK_OWNED_PATH: "VALUE_A = 1\n", _EDIT_PATH_A: "EDIT_A = 1\n",
             _EDIT_PATH_B: "EDIT_B = 1\n"},
        ],
    }
    edit_requests = [
        {"kind": EDIT_REQUEST_KIND, "path": _EDIT_PATH_A, "reason": "copy.ts 도 고쳐야 한다"},
        {"kind": EDIT_REQUEST_KIND, "path": _EDIT_PATH_B, "reason": "readout.ts 도 고쳐야 한다"},
    ]
    stub, steps_fn = _station_stub_with_requests(queues, "build", edit_requests, on_call=2)
    # Three "review" requests: plan review, task t1's own build review
    # (the first attempt, before acceptance ever runs), and the fix
    # round's own build review (after the owner's approval resumes it).
    review_steps, review_calls = _review_queue_steps([(True, []), (True, []), (True, [])])

    def factory(config, ledger, fakes):
        proc = _tfo.Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", steps_fn)
        proc.register("review", review_steps)
        return proc

    _tfo._install_fake_acceptance_and_handoff(monkeypatch, [
        _tfo._FakeAcceptanceResult(passed=False, scenes=(_tfo._FakeSceneResult("s1", False),)),
        _tfo._FakeAcceptanceResult(passed=True),
    ])

    first = full_orbit.run_full_job(
        ws.config, ws.ledger, _tfo._fakes(), "alpha", job_name, "owns 밖 결과 반영 확인",
        processor_factory=factory,
    )
    return ws, first, factory, review_calls


def test_46_edit_request_stop_during_a_fix_round_records_two_pending_requests(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_name = "fix-round-edit-stop-job"
    ws, first, _factory, _review_calls = _fix_round_edit_requests_stop(
        workspace, job_name, monkeypatch)

    assert first.status == "needs_owner", first
    pos = load_position(ws.ledger, "alpha", job_name)
    assert pos is not None and pos.stage == STAGE_TASK_OWNER and pos.fixing is True
    assert pos.task_id == "fix-1"
    kinds_paths = {(r.get("kind"), r.get("path")) for r in pos.pending_requests}
    assert kinds_paths == {
        (EDIT_REQUEST_KIND, _EDIT_PATH_A), (EDIT_REQUEST_KIND, _EDIT_PATH_B),
    }
    # Before the fix: this raised FixupError("no_task_being_fixed")
    # instead -- the fix round's own task id is never in plan.tasks.
    def fakes_for(_attempt_id: str):
        return _tfo._fakes()

    with pytest.raises(fixup.FixupError, match="also_own_not_requested"):
        fixup.approve_pending_edit(ws.config, ws.ledger, "alpha",
                                   (_EDIT_PATH_NOT_REQUESTED,), fakes_for)


def test_46_owner_can_approve_and_decline_an_edit_request_raised_during_a_fix_round(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual appX incident: the owner approved nothing and
    declined one path with a direction note -- reproduced here with one
    approval and one decline (test_43/45's own shape), so both the
    grant path and the decline path are both proven live on a FIX round
    task, not just the original task."""
    job_name = "fix-round-edit-stop-approve-job"
    ws, first, factory, _review_calls = _fix_round_edit_requests_stop(
        workspace, job_name, monkeypatch)
    assert first.status == "needs_owner", first
    stopped_attempt_id = first.attempt_id

    def fakes_for(_attempt_id: str):
        return _tfo._fakes()

    note = "묶음 처리 유지: 범위 안에서 방법을 찾는다"
    outcome = fixup.approve_pending_edit(
        ws.config, ws.ledger, "alpha", (_EDIT_PATH_A,), fakes_for,
        declined=(_EDIT_PATH_B,), decline_note=note, processor_factory=factory,
    )

    assert outcome.status == "done", outcome
    assert outcome.attempt_id == stopped_attempt_id
    assert (ws.alpha / _EDIT_PATH_A).read_text(encoding="utf-8") == "EDIT_A = 1\n"
    assert not (ws.alpha / _EDIT_PATH_B).exists()  # declined: never granted
    declined = [e.payload for _s, e in ws.ledger.events("alpha", "task.request_declined")]
    assert [d.get("path") for d in declined] == [_EDIT_PATH_B]
    assert declined[-1].get("note") == note


# ---- a review finding, a past incident's own additional scope):
# run_acceptance_and_handoff's own in-memory `rounds` started at 0 on
# EVERY call -- including advance_past_task's own resume after a fix
# round's task attempt itself stopped needs_owner mid-round (exactly
# test_46's own scenario above) and was resumed. A job that had
# already spent max_fix_rounds got max_fix_rounds MORE every time it
# was resumed, with no --past-cap ever asked for. ----------------------

def test_codex_p1_acceptance_fix_round_cap_persists_across_a_resume(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """max_fix_rounds=1: round 1 ("fix-1") stops on two pending edit
    requests, is approved and finishes -- 1 round now spent. Acceptance
    still fails; the bug re-entered the loop with `rounds` reset to 0
    (0 < max_fix_rounds(1) again) and opened ANOTHER round (even
    colliding on the same "fix-1" id); fixed, no second round opens at
    all (1 round already spent >= cap 1) and the job ends "failed"/
    "max_fix_rounds_exceeded" instead of spending a round past the cap
    with no owner override."""
    job_name = "p1-acceptance-cap-resume-job"
    ws = _tfo._full_orbit_workspace(workspace)
    plan_json = _tfo._plan_json(tasks=[
        {"id": "t1", "title": "필드 정리", "detail": "",
         "owns": [_TASK_OWNED_PATH], "depends_on": []},
    ])
    queues = {
        "facts": [{f".gongbang/{job_name}/facts.md": _FACTS_NO_TARGET}],
        "plan": [_tfo._plan_queue_entry(job_name, plan_json)],
        "build": [
            {_TASK_OWNED_PATH: "VALUE_A = 1\n"},  # attempt 1: original task t1
            {_TASK_OWNED_PATH: "VALUE_A = 1\n", _EDIT_PATH_A: "EDIT_A = 1\n",
             _EDIT_PATH_B: "EDIT_B = 1\n"},  # attempt 2: fix round 1 ("fix-1")
            # If the bug survives, a THIRD build call happens here (a
            # second round opened past the cap) -- never consumed once
            # the cap actually holds.
            {_TASK_OWNED_PATH: "VALUE_A = 1\n"},
        ],
    }
    edit_requests = [
        {"kind": EDIT_REQUEST_KIND, "path": _EDIT_PATH_A, "reason": "copy.ts 도 고쳐야 한다"},
        {"kind": EDIT_REQUEST_KIND, "path": _EDIT_PATH_B, "reason": "readout.ts 도 고쳐야 한다"},
    ]
    stub, steps_fn = _station_stub_with_requests(queues, "build", edit_requests, on_call=2)
    review_steps, review_calls = _review_queue_steps([(True, []), (True, []), (True, [])])

    def factory(config, ledger, fakes):
        proc = _tfo.Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", steps_fn)
        proc.register("review", review_steps)
        return proc

    _tfo._install_fake_acceptance_and_handoff(monkeypatch, [
        _tfo._FakeAcceptanceResult(passed=False, scenes=(_tfo._FakeSceneResult("s1", False),)),
        _tfo._FakeAcceptanceResult(passed=False, scenes=(_tfo._FakeSceneResult("s1", False),)),
    ])

    first = full_orbit.run_full_job(
        ws.config, ws.ledger, _tfo._fakes(), "alpha", job_name, "owns 밖 결과 반영 확인",
        max_fix_rounds=1, processor_factory=factory,
    )
    assert first.status == "needs_owner", first
    pos = load_position(ws.ledger, "alpha", job_name)
    assert pos is not None and pos.stage == STAGE_TASK_OWNER and pos.fixing is True
    assert pos.task_id == "fix-1"

    def fakes_for(_attempt_id: str):
        return _tfo._fakes()

    build_calls_before = len([c for c in stub.calls if c[0] == "build"])
    outcome = fixup.approve_pending_edit(
        ws.config, ws.ledger, "alpha", (_EDIT_PATH_A, _EDIT_PATH_B), fakes_for,
        processor_factory=factory,
    )

    assert outcome.status == "failed", outcome
    assert outcome.reason == "max_fix_rounds_exceeded", outcome
    # Exactly one MORE build call happened to finish fix round 1 --
    # never a third one (a second round opened past the cap).
    build_calls_after = len([c for c in stub.calls if c[0] == "build"])
    assert build_calls_after - build_calls_before == 1


# ---- 결함 31 (sample-app T4,: after 결함 28's --also-own
# approved T4's two pending edit requests and resumed it, T4 stopped
# AGAIN on owner_request -- its three new pending_requests were all a
# kind this engine has never heard of, "out_of_scope_followup", each
# still shaped {"path": ..., "reason": ...} (OWNS_SCOPE_REQUEST_RULE
# never pinned the exact shape down). One of the three
# (src/lib/production/flow.ts) was a file T5 -- a LATER task in the
# same plan -- already owned; approve_pending_edit could not approve
# ANY of them (it only ever matched kind="edit"). This file's own two
# cases:
#   test_31_unknown_kind_handed_to_later_task_*  (a) the reported path
#      is already in a LATER, not-yet-run task's own `owns` -- the job
#      never stops at all; the later task's own builder instructions
#      carry a HANDOFF_NOTE_PREFIX line, and the handoff itself is a
#      permanent ledger record (HANDOFF_KIND).
#   test_31_unknown_kind_owned_by_no_task_*  (b) the reported path is
#      owned by NO task -- is_owns_scope_request now matches it the
#      same way it always matched kind="edit", so `gb2 fix <app>
#      --also-own <path>` (fixup.approve_pending_edit) approves it. ----

_LATER_OWNED_PATH = "src/other/t2_owned.py"


def _handoff_build_stub(queues: dict[str, list[dict[str, str]]],
                        first_call_requests: list[dict[str, object]]):
    """Like _station_stub_with_requests, but captures every "build"
    call's own task text (in call order) -- this file's 결함 31 cases
    need to read back what a LATER task's own builder instructions
    actually said."""
    stub = _tfo._RunStationStub(queues)
    base_steps = stub.steps
    calls = {"n": 0}
    captured_tasks: list[str] = []

    def steps(ctx: StepContext) -> list[StepDef]:
        station = str(ctx.request.payload.get("station"))
        if station != "build":
            return base_steps(ctx)
        captured_tasks.append(str(ctx.request.payload.get("task")))
        calls["n"] += 1
        if calls["n"] > 1:
            return base_steps(ctx)

        def _run(ctx: StepContext) -> dict[str, object]:
            role = str(ctx.request.payload.get("role"))
            stub.calls.append((station, role))
            copy = copy_root(ctx.config, ctx.attempt_id, role)
            files = queues[station].pop(0)
            for rel, content in files.items():
                target = copy / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            changed = dirty_paths(copy, ())
            ctx.ledger.append(_tfo.Event(
                project=ctx.project, kind="station.finished", actor_kind="processor",
                actor_id="processor", attempt_id=ctx.attempt_id,
                payload={
                    "request_id": ctx.request.request_id, "station": station, "role": role,
                    "ok": True, "summary": "stub", "cost_usd": 0.0, "turns": 1,
                    "changed_paths": list(changed), "requests": list(first_call_requests),
                },
            ))
            return {}

        def _reconcile(ctx: StepContext) -> bool:
            for _seq, event in ctx.ledger.events(ctx.project, "station.finished"):
                if event.payload.get("request_id") == ctx.request.request_id:
                    return True
            return False

        return [StepDef("record_station", _run, _reconcile)]

    return stub, steps, captured_tasks


def test_31_unknown_kind_handed_to_later_task_does_not_stop_the_job(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_name = "handoff-job"
    ws = _tfo._full_orbit_workspace(workspace)
    plan_json = _tfo._plan_json(tasks=[
        {"id": "t1", "title": "필드 정리", "detail": "",
         "owns": [_TASK_OWNED_PATH], "depends_on": []},
        {"id": "t2", "title": "나중 작업", "detail": "",
         "owns": [_LATER_OWNED_PATH], "depends_on": []},
    ])
    queues = {
        "facts": [{f".gongbang/{job_name}/facts.md": _FACTS_NO_TARGET}],
        "plan": [_tfo._plan_queue_entry(job_name, plan_json)],
        "build": [
            {_TASK_OWNED_PATH: "VALUE_A = 1\n"},
            {_LATER_OWNED_PATH: "VALUE_B = 1\n"},
        ],
    }
    requests = [
        {"kind": "out_of_scope_followup", "path": _LATER_OWNED_PATH,
         "reason": "t2가 처리해야 한다"},
    ]
    stub, steps_fn, captured_tasks = _handoff_build_stub(queues, requests)
    review_steps, review_calls = _review_queue_steps([(True, []), (True, []), (True, [])])

    def factory(config, ledger, fakes):
        proc = _tfo.Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", steps_fn)
        proc.register("review", review_steps)
        return proc

    _tfo._install_fake_acceptance_and_handoff(
        monkeypatch, [_tfo._FakeAcceptanceResult(passed=True)],
    )

    outcome = full_orbit.run_full_job(
        ws.config, ws.ledger, _tfo._fakes(), "alpha", job_name, "owns 밖 결과 반영 확인",
        processor_factory=factory,
    )

    # The job never stopped for the owner at all -- t1's own report was
    # handed to t2 instead of reaching STAGE_TASK_OWNER.
    assert outcome.status == "done", outcome
    assert review_calls == [True, True, True]
    assert (ws.alpha / _TASK_OWNED_PATH).read_text(encoding="utf-8") == "VALUE_A = 1\n"
    assert (ws.alpha / _LATER_OWNED_PATH).read_text(encoding="utf-8") == "VALUE_B = 1\n"
    # t2's own builder instructions carry the handed-off request's note.
    assert len(captured_tasks) == 2
    assert f"{HANDOFF_NOTE_PREFIX} {_LATER_OWNED_PATH}: t2가 처리해야 한다" in captured_tasks[1]
    # the handoff is a permanent ledger record, not just the note text.
    handoffs = [event.payload for _seq, event in ws.ledger.events("alpha", HANDOFF_KIND)]
    assert any(
        h.get("job_name") == job_name and h.get("from_task_id") == "t1"
        and h.get("to_task_id") == "t2" and h.get("path") == _LATER_OWNED_PATH
        for h in handoffs
    )


def test_31_unknown_kind_owned_by_no_task_is_approvable_with_also_own(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_name = "unknown-kind-stop-job"
    unowned_path = "src/other/unowned_followup.py"
    ws = _tfo._full_orbit_workspace(workspace)
    plan_json = _tfo._plan_json(tasks=[
        {"id": "t1", "title": "필드 정리", "detail": "",
         "owns": [_TASK_OWNED_PATH], "depends_on": []},
    ])
    queues = {
        "facts": [{f".gongbang/{job_name}/facts.md": _FACTS_NO_TARGET}],
        "plan": [_tfo._plan_queue_entry(job_name, plan_json)],
        "build": [{_TASK_OWNED_PATH: "VALUE_A = 1\n", unowned_path: "FOLLOWUP = 1\n"}],
    }
    requests = [
        {"kind": "out_of_scope_followup", "path": unowned_path, "reason": "copy.ts 도 고쳐야 한다"},
    ]
    stub, steps_fn = _station_stub_with_requests(queues, "build", requests)
    review_steps, review_calls = _review_queue_steps([(True, []), (True, [])])

    def factory(config, ledger, fakes):
        proc = _tfo.Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", steps_fn)
        proc.register("review", review_steps)
        return proc

    first = full_orbit.run_full_job(
        ws.config, ws.ledger, _tfo._fakes(), "alpha", job_name, "owns 밖 결과 반영 확인",
        processor_factory=factory,
    )
    assert first.status == "needs_owner", first
    pos = load_position(ws.ledger, "alpha", job_name)
    assert pos is not None and pos.stage == STAGE_TASK_OWNER
    kinds_paths = {(r.get("kind"), r.get("path")) for r in pos.pending_requests}
    assert kinds_paths == {("out_of_scope_followup", unowned_path)}

    _tfo._install_fake_acceptance_and_handoff(
        monkeypatch, [_tfo._FakeAcceptanceResult(passed=True)],
    )

    def fakes_for(_attempt_id: str):
        return _tfo._fakes()

    outcome = fixup.approve_pending_edit(
        ws.config, ws.ledger, "alpha", (unowned_path,), fakes_for, processor_factory=factory,
    )

    assert outcome.status == "done", outcome
    assert review_calls == [True, True]
    assert (ws.alpha / unowned_path).read_text(encoding="utf-8") == "FOLLOWUP = 1\n"


def test_32_resumed_review_stage_names_owner_added_paths() -> None:
    """Defect 32 (sample-app T4): after an owner --also-own grant, a resumed
    task's reviewer still flagged the granted test files as out of scope."""
    from engine.application import task_pipeline as orbit
    from engine.application.job_state import widen_stage_owns
    stages = [orbit.Stage("review", {"role": "reviewer", "task": "T4 work"}),
              orbit.Stage("run_station", {"owns": ["src/a.ts"]})]
    out = widen_stage_owns(stages, ("tests/a.test.ts",))
    assert "tests/a.test.ts" in out[0].payload["task"]
    assert out[1].payload["owns"] == ["src/a.ts", "tests/a.test.ts"]


def test_33_note_request_never_blocks() -> None:
    """Defect 33 (sample-app T4): a builder's informational {"kind":"note"}
    request stopped the job for the owner."""
    from engine.domain.policy import NOTE_REQUEST_KIND, OWNS_SCOPE_REQUEST_RULE
    from engine.application.job_state import _request_fulfilled
    assert '"kind": "note"' in OWNS_SCOPE_REQUEST_RULE
    assert _request_fulfilled(None, "p", {"kind": NOTE_REQUEST_KIND, "detail": "x"},
                              after_seq=0, repo=None, proposal="") is True


def test_34_finished_task_path_can_be_granted_but_pending_task_path_cannot() -> None:
    """Defect 34 (sample-app T4): one export line was needed in a file owned by
    T11, which had already finished; only not-yet-run tasks keep their paths."""
    import pytest
    from types import SimpleNamespace
    from engine.application.repair_target import FixupError, validate_also_own

    class _Ledger:
        def events(self, project, kind):
            return []

    plan = SimpleNamespace(tasks=[SimpleNamespace(id="T4", owns=["src/a.ts"]),
                                  SimpleNamespace(id="T11", owns=["src/ai-input.ts"]),
                                  SimpleNamespace(id="T5", owns=["src/flow.ts"])])
    assert validate_also_own(_Ledger(), "p", "j", plan, "T4", ("src/ai-input.ts",),
                             ("T4", "T5")) == ("src/ai-input.ts",)
    with pytest.raises(FixupError):
        validate_also_own(_Ledger(), "p", "j", plan, "T4", ("src/flow.ts",), ("T4", "T5"))


def test_33b_note_never_blocks_without_a_request_filter() -> None:
    """Defect 33b (sample-app T5): the `gb2 fix` path passes no request_filter,
    so a lone note still stopped the job. Notes are dropped for every caller."""
    from engine.application.task_pipeline import blocking_station_requests
    note = {"kind": "note", "detail": "env info"}
    edit = {"kind": "edit", "path": "a.ts", "reason": "r"}
    assert blocking_station_requests([note], None) == []
    assert blocking_station_requests([note, edit], None) == [edit]
    seen: list[list[dict[str, object]]] = []

    def _filter(reqs: list[dict[str, object]]) -> list[dict[str, object]]:
        seen.append(reqs)
        return reqs
    assert blocking_station_requests([note, edit], _filter) == [edit]
    assert seen == [[edit]]


def test_35_worker_copy_link_resolves_file_dep_through_repo(tmp_path) -> None:
    """Defect 35 (sample-app T5): the builder's node_modules is a symlink into
    the REAL repo, so node_modules/@sample-app/kit -> ../../../_shared/kit
    resolves against the repo's parent, not the copy's. Mapping the file dep
    only under the copy left the builder with an empty package."""
    import os
    from engine.application.steps import file_deps
    shared = tmp_path / "real-shared" / "kit"
    shared.mkdir(parents=True)
    (shared / "index.js").write_text("ok")
    app = tmp_path / "apps" / "p"
    scoped = app / "repo" / "node_modules" / "@sample-app"
    scoped.mkdir(parents=True)
    (scoped / "kit").symlink_to("../../../_shared/kit")
    copy = app / "home" / "jobs" / "J-1" / "builder"
    copy.mkdir(parents=True)
    (copy / "node_modules").symlink_to(app / "repo" / "node_modules")
    targets = (("../_shared/kit", str(shared)),)
    probe = copy / "node_modules" / "@sample-app" / "kit" / "index.js"

    # RED: only the copy-rooted pair (the old worker behavior) misses it.
    old = file_deps.remap_pairs_for_root(copy, targets)
    assert all(not str(dest).startswith(str(app / "_shared")) for _src, dest in old)
    assert not probe.exists()

    pairs = file_deps.remap_pairs_for_roots((copy, app / "repo"), targets)
    assert (shared, app / "_shared" / "kit") in pairs
    assert len(pairs) == len(set(pairs))
    for src, dest in pairs:  # stand-in for sandbox.wrap's --ro-bind
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.symlink_to(src)
    assert os.path.exists(probe)


def test_36_bracket_path_missing_at_ref_reads_as_missing(tmp_path) -> None:
    """Defect 36 (sample-app T7): `git show REF:src/app/[adv]/route.ts` exits 0
    with empty output for a path that does not exist at REF (brackets read
    as a pattern), so a brand-new Next.js route file failed every merge
    with merge_conflict_with_head."""
    import subprocess
    from engine.application.steps import merge as merge_step
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "c"], check=True)
    assert merge_step._ref_bytes(repo, "HEAD", "src/app/[adv]/route.ts") is None
    assert merge_step._ref_bytes(repo, "HEAD", "a.txt") == b"x"


def test_38_finished_task_extra_grant_can_be_granted_again() -> None:
    """Defect 38 (sample-app T7): T5 (finished) had been granted
    tests/reference-kind.test.ts with --also-own; granting it to T7 failed
    also_own_owned_by_other_task. A finished task's EXTRA grants are released
    exactly like its plan owns (defect 34); a pending task's still block."""
    import pytest
    from types import SimpleNamespace
    from engine.application.repair_target import (
        OWNS_EXTENDED_KIND, FixupError, validate_also_own,
    )

    grants = [("T5", ["tests/kind.test.ts"]), ("T8", ["tests/t8.test.ts"])]

    class _Ledger:
        def events(self, project, kind):
            if kind != OWNS_EXTENDED_KIND:
                return []
            return [(i, SimpleNamespace(payload={"job_name": "j", "task_id": t, "paths": p}))
                    for i, (t, p) in enumerate(grants)]

    plan = SimpleNamespace(tasks=[SimpleNamespace(id="T5", owns=["src/flow.ts"]),
                                  SimpleNamespace(id="T7", owns=["src/x.ts"]),
                                  SimpleNamespace(id="T8", owns=["src/y.ts"])])
    active = ("T7", "T8")
    assert validate_also_own(_Ledger(), "p", "j", plan, "T7", ("tests/kind.test.ts",),
                             active) == ("tests/kind.test.ts",)
    with pytest.raises(FixupError):
        validate_also_own(_Ledger(), "p", "j", plan, "T7", ("tests/t8.test.ts",), active)


def test_39_also_own_outside_station_prefixes_refused_up_front() -> None:
    """Defect 39 (sample-app T7): --also-own scripts/worker.ts was recorded,
    then the run died in 0.02 min on owns_escapes_station_prefixes. The
    grant must be refused before anything is recorded, naming the path."""
    import pytest
    from types import SimpleNamespace
    from engine.application.repair_target import FixupError, validate_also_own

    class _Ledger:
        def events(self, project, kind):
            return []

    plan = SimpleNamespace(tasks=[SimpleNamespace(id="T7", owns=["src/x.ts"])])
    prefixes = ("src/", "tests/")
    with pytest.raises(FixupError, match="also_own_outside_station_prefixes:scripts/w.ts"):
        validate_also_own(_Ledger(), "p", "j", plan, "T7", ("scripts/w.ts",), ("T7",),
                          station_prefixes=prefixes)
    assert validate_also_own(_Ledger(), "p", "j", plan, "T7", ("tests/a.test.ts",), ("T7",),
                             station_prefixes=prefixes) == ("tests/a.test.ts",)


def test_39_both_grant_entry_points_pass_station_prefixes() -> None:
    import inspect
    from engine.application import repair, repair_approval
    for module in (repair, repair_approval):
        assert "station_prefixes=" in inspect.getsource(module)


def test_40_fix_path_hands_later_task_paths_off() -> None:
    """Defect 40 (sample-app T7): on the `gb2 fix` path an edit request for a
    path a LATER task owns stopped the job for the owner (and --also-own then
    refused it as another task's path) -- the handoff filter (defect 31) was
    only wired into the first run and plain resume, never the fix path.

    The fix now runs through the one shared task_stage_runner.run_task_stages
    (defect 33b/40's shared root cause -- see flow-map.md "같은 일을 하는
    입구가 여럿인 곳"), which builds the handoff filter itself from the
    HandoffContext repair.fix passes it: still every build-task attempt
    (first run, resume, and fix alike) goes through the SAME filter-building
    code, just no longer duplicated at each call site."""
    import inspect
    from engine.application import repair
    source = inspect.getsource(repair.fix)
    assert "handoff=_fix_handoff(" in source
    assert "run_task_stages(" in source


def test_37_carried_archive_never_crosses_a_committed_attempt(tmp_path) -> None:
    """Defect 37 (sample-app T7): fixing T7's first attempt walked back past
    T5's COMMITTED attempt and overlaid T5's stale, pre-commit flow.ts
    archive onto T7's builder copy. Walking back must stop at a commit."""
    from types import SimpleNamespace
    from engine.application import repair_target as rt
    stale = tmp_path / "a32"
    stale.mkdir()
    (stale / "flow.ts").write_text("old")
    archives = {"#32": stale, "#33": None, "#34": None}
    committed = {"#33"}

    class _Ledger:
        def attempts_for_job_name(self, project, job_name):
            return [SimpleNamespace(attempt_id=a) for a in ("#32", "#33", "#34")]

    orig_dir, orig_req = rt._archive_dir_for_attempt, rt.requests_for_attempt
    rt._archive_dir_for_attempt = lambda ledger, project, a: archives[a]
    rt.requests_for_attempt = lambda ledger, project, a: (
        [SimpleNamespace(kind="commit", status="done")] if a in committed else [])
    try:
        assert rt._latest_merged_archive(_Ledger(), "p", "j", "#34") is None
        committed.clear()  # same-task crash chain: still carried forward
        assert rt._latest_merged_archive(_Ledger(), "p", "j", "#34") == stale
    finally:
        rt._archive_dir_for_attempt, rt.requests_for_attempt = orig_dir, orig_req


def test_35_worker_sandbox_maps_file_deps_for_copy_and_repo() -> None:
    """The worker sandbox must call the two-root mapping (defect 35),
    through the one shared, mount-safe helper (defect 42, --
    see tests/acceptance/test_t13_file_dep_real_sandbox.py for the real
    bwrap reproduction this helper's own mkdir-precreate fixes)."""
    import inspect
    from engine.application.steps import worker_sandbox
    source = inspect.getsource(worker_sandbox)
    assert "file_deps.sandbox_file_dep_pairs(\n        spec.copy_root, repo" in source


# ---- 결함 41, plans/-gongbang-entry-unify.md §B):
# `gb2 fix` had no round cap -- a job could cycle failed-review ->
# fix() -> failed-review forever (appX T7 재시도 5바퀴 이상, real
# incident). engine.application.repair_target.fix_rounds_used counts
# how many of this job's earlier attempts, since its last commit,
# closed on a failed review/bench -- engine.application.repair.fix
# refuses (FixupError, NOTHING recorded) once that count reaches
# JobPosition.max_fix_rounds (default 2), unless the owner passes
# `past_cap=True` (tools/gb2lib.runner.cmd_fix's own `--past-cap`),
# which allows exactly one more round and records "fix.past_cap". ----

_ROUNDS_OWNED_PATH = "src/greeter/rounds_owns.py"


def test_41_fix_rounds_used_counts_only_failed_review_bench_attempts_since_commit() -> None:
    from types import SimpleNamespace
    from engine.application import repair_target as rt

    # Three earlier attempts of the same job, newest first (as
    # attempts_for_job_name itself returns, per that method's own
    # contract -- _earlier_attempts_since_last_commit reverses it):
    # #1 failed at review (counts), #2's own commit request is "done"
    # (the walk stops there, same 결함 37 boundary fix_rounds_used
    # reuses), #3 (older than the commit) must never be reached.
    attempts = [
        SimpleNamespace(attempt_id="#1"),
        SimpleNamespace(attempt_id="#2"),
        SimpleNamespace(attempt_id="#3"),
    ]
    requests_by_attempt = {
        "#1": [SimpleNamespace(kind="review", status="failed")],
        "#2": [SimpleNamespace(kind="commit", status="done")],
        "#3": [SimpleNamespace(kind="review", status="failed")],
    }

    class _Ledger:
        def attempts_for_job_name(self, project, job_name):
            return list(reversed(attempts))

    orig = rt.requests_for_attempt
    rt.requests_for_attempt = lambda ledger, project, a: requests_by_attempt[a]
    try:
        assert rt.fix_rounds_used(_Ledger(), "p", "j", "#current") == 1
    finally:
        rt.requests_for_attempt = orig


def test_41_fix_refuses_with_nothing_recorded_once_rounds_are_exhausted(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _tfo._full_orbit_workspace(workspace)
    job_name = "rounds-cap-job"
    plan_json = _tfo._plan_json(tasks=[
        {"id": "t1", "title": "값 바꾸기", "detail": "",
         "owns": [_ROUNDS_OWNED_PATH], "depends_on": []},
    ])
    stub = _tfo._RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": _FACTS_NO_TARGET}],
        "plan": [_tfo._plan_queue_entry(job_name, plan_json)],
        "build": [
            {_ROUNDS_OWNED_PATH: "VALUE = 1\n"},  # attempt 1 (first run)
            {_ROUNDS_OWNED_PATH: "VALUE = 2\n"},  # attempt 2 (fix round 1)
            {_ROUNDS_OWNED_PATH: "VALUE = 3\n"},  # attempt 3 (fix round 2)
        ],
    })
    # Every review fails -- this job never passes on its own; only the
    # round-cap refusal (never a real fix) ends this test.
    # Three DIFFERENT findings (never the SAME file+severity+note twice
    # in a row) -- this test is about the round-COUNT cap, not a past incident's
    # own no_progress (repeated-finding) check, which has its own tests
    # in tests/unit/test_full_orbit.py.
    review_steps, review_calls = _review_queue_steps([
        (True, []),   # plan review
        (False, [{"severity": "P1", "file": _ROUNDS_OWNED_PATH, "note": "r1"}]),   # attempt 1
        (False, [{"severity": "P1", "file": _ROUNDS_OWNED_PATH, "note": "r2"}]),   # fix round 1
        (False, [{"severity": "P1", "file": _ROUNDS_OWNED_PATH, "note": "r3"}]),   # fix round 2
    ])
    factory = _processor_factory_with_review(stub, review_steps)

    first = full_orbit.run_full_job(
        ws.config, ws.ledger, _tfo._fakes(), "alpha", job_name, "값 바꾸기",
        processor_factory=factory,
    )
    assert first.status == "failed", first

    def fakes_for(_attempt_id: str):
        return _tfo._fakes()

    # Round 1: 0 earlier failed-review attempts < max_fix_rounds(2) -- allowed.
    round1 = fixup.fix(ws.config, ws.ledger, "alpha", "", fakes_for,
                       processor_factory=factory)
    assert round1.outcome.status == "failed", round1.outcome

    # Round 2: 1 earlier failed-review attempt < 2 -- still allowed.
    round2 = fixup.fix(ws.config, ws.ledger, "alpha", "", fakes_for,
                       processor_factory=factory)
    assert round2.outcome.status == "failed", round2.outcome

    started_before = ws.ledger.events("alpha", "fix.started")
    attempt_before = ws.ledger.project("alpha").current_attempt_id

    # Round 3: 2 earlier failed-review attempts >= max_fix_rounds(2) --
    # refused, with NOTHING recorded (no new "fix.started" event, no
    # change to the project's current attempt).
    with pytest.raises(fixup.FixupError, match="fix_rounds_exhausted:2"):
        fixup.fix(ws.config, ws.ledger, "alpha", "", fakes_for,
                 processor_factory=factory)

    assert ws.ledger.events("alpha", "fix.started") == started_before
    assert ws.ledger.project("alpha").current_attempt_id == attempt_before
    assert review_calls == [True, False, False, False]


def test_41_past_cap_allows_one_more_round_and_records_the_event(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _tfo._full_orbit_workspace(workspace)
    job_name = "rounds-past-cap-job"
    plan_json = _tfo._plan_json(tasks=[
        {"id": "t1", "title": "값 바꾸기", "detail": "",
         "owns": [_ROUNDS_OWNED_PATH], "depends_on": []},
    ])
    stub = _tfo._RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": _FACTS_NO_TARGET}],
        "plan": [_tfo._plan_queue_entry(job_name, plan_json)],
        "build": [
            {_ROUNDS_OWNED_PATH: "VALUE = 1\n"},  # attempt 1 (first run)
            {_ROUNDS_OWNED_PATH: "VALUE = 2\n"},  # attempt 2 (fix round 1)
            {_ROUNDS_OWNED_PATH: "VALUE = 3\n"},  # attempt 3 (fix round 2)
            {_ROUNDS_OWNED_PATH: "VALUE = 4\n"},  # attempt 4 (--past-cap round)
        ],
    })
    # Three DIFFERENT findings, same reasoning as the test above.
    review_steps, review_calls = _review_queue_steps([
        (True, []),
        (False, [{"severity": "P1", "file": _ROUNDS_OWNED_PATH, "note": "r1"}]),
        (False, [{"severity": "P1", "file": _ROUNDS_OWNED_PATH, "note": "r2"}]),
        (False, [{"severity": "P1", "file": _ROUNDS_OWNED_PATH, "note": "r3"}]),
        (True, []),  # the --past-cap round finally passes
    ])
    factory = _processor_factory_with_review(stub, review_steps)
    _tfo._install_fake_acceptance_and_handoff(monkeypatch, [_tfo._FakeAcceptanceResult(passed=True)])

    first = full_orbit.run_full_job(
        ws.config, ws.ledger, _tfo._fakes(), "alpha", job_name, "값 바꾸기",
        processor_factory=factory,
    )
    assert first.status == "failed", first

    def fakes_for(_attempt_id: str):
        return _tfo._fakes()

    fixup.fix(ws.config, ws.ledger, "alpha", "", fakes_for, processor_factory=factory)
    fixup.fix(ws.config, ws.ledger, "alpha", "", fakes_for, processor_factory=factory)

    past_cap_events_before = ws.ledger.events("alpha", "fix.past_cap")

    round3 = fixup.fix(ws.config, ws.ledger, "alpha", "", fakes_for,
                       processor_factory=factory, past_cap=True)

    assert round3.outcome.status == "done", round3.outcome
    assert len(ws.ledger.events("alpha", "fix.past_cap")) == len(past_cap_events_before) + 1
    assert review_calls == [True, False, False, False, True]


# ---- 결함 42, plans/-gongbang-entry-unify.md §C):
# a NEW plan whose build task owns src/ with no tests/ owns path of its
# own reaches the build station with nowhere to put a test file -- the
# repeated real incident (운영자가 테스트를 요구하면 작업 owns에 시험
# 파일이 없어 작업자가 멈추고 권한 요청). engine.domain.plan_model.
# validate_plan now refuses this for any caller with a real
# build_prefixes (a NEW plan check); engine.domain.plan_review.
# check_resumed_plan (an ALREADY-REVIEWED plan's resume check) calls
# validate_plan with build_prefixes=None, so this rule never re-fires
# on resume -- same None-skip gate the owns-subset check already uses.

def test_42_src_owns_without_tests_owns_is_a_problem() -> None:
    from engine.domain.plan_model import Plan, Task, validate_plan

    plan = Plan(
        goal="g", ui=False, scenes=(),
        tasks=(Task(id="t1", title="x", detail="", owns=("src/a.py",), depends_on=()),),
        acceptance=(), requirements=(),
    )
    problems = validate_plan(plan, workspace_repo_stub(), build_prefixes=("src/", "tests/"))
    assert any("tests/" in p and "t1" in p for p in problems), problems


def test_42_src_owns_with_a_tests_owns_path_passes(tmp_path) -> None:
    from engine.domain.plan_model import Plan, Task, validate_plan

    plan = Plan(
        goal="g", ui=False, scenes=(),
        tasks=(Task(id="t1", title="x", detail="",
                    owns=("src/a.py", "tests/a_test.py"), depends_on=()),),
        acceptance=(), requirements=(),
    )
    # (plan has no requirements, which validate_plan already flags
    # separately via plan_requirements_missing -- irrelevant to this
    # rule, so only this rule's own message is checked for absence.)
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/", "tests/"))
    assert not any("자기 시험 파일" in p for p in problems), problems


def test_42_rule_is_skipped_when_build_prefixes_is_none_resumed_plan_path() -> None:
    from engine.domain.plan_model import Plan, Task, validate_plan

    plan = Plan(
        goal="g", ui=False, scenes=(),
        tasks=(Task(id="t1", title="x", detail="", owns=("src/a.py",), depends_on=()),),
        acceptance=(), requirements=(),
    )
    # check_resumed_plan's own call shape (engine.domain.plan_review) --
    # build_prefixes=None must never raise this rule's own problem,
    # even though the SAME plan fails it above with real prefixes.
    problems = validate_plan(plan, workspace_repo_stub(), build_prefixes=None)
    assert not any("자기 시험 파일" in p for p in problems), problems


def test_42_check_resumed_plan_never_raises_the_new_rule(tmp_path) -> None:
    """check_resumed_plan (the actual resume-path caller) must stay
    exactly as permissive about owns as it already is -- this rule is
    wired into validate_plan's own build_prefixes-gated block, the same
    one check_resumed_plan already calls with build_prefixes=None."""
    import json
    from engine.domain.plan_model import plan_path
    from engine.domain.plan_review import check_resumed_plan

    job = "job-42"
    plan_json = {
        "goal": "목표", "ui": False,
        "requirements": [{"id": "r1", "text": "요구", "kind": "function"}],
        "scenes": [{"id": "s1", "actor_does": "a", "app_shows": "b", "where": "/",
                    "cmd": ["python3", "tests/scenes/s1.py"], "covers": ["r1"]}],
        "tasks": [{"id": "t1", "title": "작업", "detail": "", "owns": ["src/only.py"],
                   "depends_on": [], "covers": ["r1"]}],
        "acceptance": ["끝"],
    }
    path = plan_path(tmp_path, job)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan_json, ensure_ascii=False), encoding="utf-8")
    script = tmp_path / "tests" / "scenes" / "s1.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")

    class _Ledger:
        def events(self, project, kind):
            return []

    problems = check_resumed_plan(_Ledger(), "p", job, tmp_path)
    assert not any("자기 시험 파일" in p for p in problems), problems
    # "plan_never_reviewed" is expected (no plan.reviewed event) -- the
    # point here is only that the new owns rule never adds to it.
    assert problems == ["plan_never_reviewed"], problems


def test_42_rule_sentence_appears_in_both_planner_and_small_prompts() -> None:
    from engine.application.stage_prompts import planner_prompt, small_prompt
    from engine.domain.plan_model import PLAN_TASK_TEST_OWNS_RULE

    assert PLAN_TASK_TEST_OWNS_RULE in planner_prompt("요청", "job1", "facts")
    assert PLAN_TASK_TEST_OWNS_RULE in small_prompt("요청", "job1")


def workspace_repo_stub():
    """A throwaway Path validate_plan's scene checks can `.exists()`/
    `.is_symlink()` against safely -- this file's 결함 42 tests never
    give `plan` any scenes, so nothing under it is ever actually read."""
    import tempfile
    from pathlib import Path
    return Path(tempfile.mkdtemp())
