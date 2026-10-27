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
                                reported: list[dict[str, object]]):
    """Like tests.unit.test_full_orbit._station_stub_with_one_owner_request,
    but reports every dict in `reported` (not just one) on the FIRST
    call for `target_station` -- 결함 28's own scenario needs TWO
    pending edit requests in the same stop, not one."""
    stub = _tfo._RunStationStub(queues)
    base_steps = stub.steps
    calls = {"n": 0}

    def steps(ctx: StepContext) -> list[StepDef]:
        station = str(ctx.request.payload.get("station"))
        if station != target_station:
            return base_steps(ctx)
        calls["n"] += 1
        if calls["n"] > 1:
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
    only wired into the first run and plain resume, never the fix path."""
    import inspect
    from engine.application import repair
    source = inspect.getsource(repair.fix)
    assert "request_filter=_fix_request_filter(" in source


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
    """The worker sandbox must call the two-root mapping (defect 35)."""
    import inspect
    from engine.application.steps import worker_sandbox
    source = inspect.getsource(worker_sandbox)
    assert "remap_pairs_for_roots(\n        (spec.copy_root, repo)" in source
