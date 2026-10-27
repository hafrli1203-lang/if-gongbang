"""T9f -- 결함 30, 마케팅 채팅 job, read-only from that app's
own ledger.sqlite): the plan station stopped twice IN A ROW with
"plan:plan_unreadable" (attempts #12 and #13) -- the planner-written
`.gongbang/<job>/plan.json` had an unescaped double quote inside its own
`goal` string (a shell flag quoted as `--tools ""`), breaking plan.json's
own JSON syntax (json.JSONDecodeError "Expecting ',' delimiter: line 2
column 654"). `gb2 continue --from plan` (and plain `gb2 continue`, since
the stop's own job.position already sits at STAGE_PLAN/STAGE_PLAN_REVIEW)
reran the SAME planner with a prompt that never said a single word about
WHY the last plan.json broke, so the SAME mistake just repeated.

engine.application.pipeline_planning.record_plan_check_failure/
plan_check_failure_note is the fix -- same placement/approach as
3e95f0d's plan_review_findings_note (a different, EARLIER failure: this
one is load_plan/validate_plan rejecting plan.json itself, before any
reviewer ever runs): a reopened plan station's prompt now gets a
"[지난 계획 파일 문제 -- 고칠 것]" section prepended, carrying the JSON
error's own line/column plus ~80 characters of context on each side
(for a plan_unreadable stop), or the validate_plan problem list (for a
plan_invalid stop) -- never silently handing the same broken file back
with no clue why it failed.

No live AI: run_station/review are stubbed at the Processor level, same
pattern test_t9c_force_reopen.py/test_t9d_also_own.py already use."""
from __future__ import annotations

import json

import pytest

import tests.unit.test_full_orbit as _tfo
from engine import full_orbit
from engine.application.processor import Processor, StepContext
from tests.conftest import Workspace


def _capturing_plan_processor_factory(
    stub: "_tfo._RunStationStub", plan_task_texts: list[str],
) -> "full_orbit.orbit.ProcessorFactory":
    def steps_capturing_plan_task(ctx: StepContext):
        if str(ctx.request.payload.get("station")) == "plan":
            plan_task_texts.append(str(ctx.request.payload.get("task", "")))
        return stub.steps(ctx)

    def factory(config, ledger, fakes) -> Processor:
        proc = Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", steps_capturing_plan_task)
        proc.register("review", _tfo._review_always_passes_steps)
        return proc

    return factory


# ---- 30a. a json.JSONDecodeError (plan_unreadable) carries its own ----
# ---- line/column + ~80-char context into the reopened plan prompt -----

def test_30_plan_unreadable_resume_carries_the_json_error_position_and_context(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _tfo._full_orbit_workspace(workspace)
    job_name = "plan-unreadable-job"
    # The real shape of the defect: an unescaped `""` inside the
    # planner-written goal string breaks plan.json's own JSON syntax.
    broken_goal = '마케팅 챗봇 응답: --tools ""로 호출'
    broken_plan_text = (
        '{"goal": "' + broken_goal + '", "ui": false, "scenes": [], '
        '"tasks": [{"id":"t1","title":"x","detail":"",'
        '"owns":["src/greeter/extra1.py"],"depends_on":[]}], '
        '"acceptance": ["ok"], "requirements": []}'
    )
    valid_plan_json = _tfo._plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    stub = _tfo._RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md":
                   "- 요청 확인: 통과 (출처: request)\n\n## 외부 대상\n(없음)\n"}],
        "plan": [
            {f".gongbang/{job_name}/plan.json": broken_plan_text},
            _tfo._plan_queue_entry(job_name, valid_plan_json),
        ],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    plan_task_texts: list[str] = []
    factory = _capturing_plan_processor_factory(stub, plan_task_texts)
    _tfo._install_fake_acceptance_and_handoff(monkeypatch, [_tfo._FakeAcceptanceResult(passed=True)])

    first = full_orbit.run_full_job(
        ws.config, ws.ledger, _tfo._fakes(), "alpha", job_name, "마케팅 챗봇 만들어줘",
        processor_factory=factory,
    )
    assert first.status == "failed", first
    assert first.reason.startswith("plan:plan_unreadable"), first.reason
    assert len(plan_task_texts) == 1
    assert "[지난 계획 파일 문제" not in plan_task_texts[0]

    done = full_orbit.continue_full_job(
        ws.config, ws.ledger, _tfo._fakes(), "alpha", job_name, processor_factory=factory,
    )

    assert done.status == "done", done
    assert len(plan_task_texts) == 2
    second = plan_task_texts[1]
    assert second.index("[지난 계획 파일 문제 -- 고칠 것]") == 0
    assert "plan_unreadable" in second
    # The JSON parser's own line/column, and ~80 chars of context on
    # each side of the error point -- the error must point AT the real
    # broken quote, not just name "plan_unreadable" with no detail.
    assert "line 1 column" in second
    assert "--tools" in second
    assert (ws.alpha / "src/greeter/extra1.py").exists()


# ---- 30b. a validate_plan failure (plan_invalid) carries its own ------
# ---- problem list into the reopened plan prompt ------------------------

def test_30_validate_plan_failure_resume_carries_the_problem_list(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _tfo._full_orbit_workspace(workspace)
    job_name = "plan-invalid-job"
    # Valid JSON, but validate_plan rejects it outright: a plan with no
    # requirements at all names nothing for the handoff table or
    # engine.domain.evidence_graph's runtime completion gate to ever check
    # against (plan_model.validate_plan's own first check).
    no_requirements_plan = json.dumps({
        "goal": "작은 기능 추가", "ui": False, "scenes": [],
        "tasks": [{"id": "t1", "title": "첫 파일", "detail": "",
                   "owns": ["src/greeter/extra1.py"], "depends_on": []}],
        "acceptance": ["요구사항대로 동작한다"], "requirements": [],
    })
    valid_plan_json = _tfo._plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    stub = _tfo._RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md":
                   "- 요청 확인: 통과 (출처: request)\n\n## 외부 대상\n(없음)\n"}],
        "plan": [
            {f".gongbang/{job_name}/plan.json": no_requirements_plan},
            _tfo._plan_queue_entry(job_name, valid_plan_json),
        ],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    plan_task_texts: list[str] = []
    factory = _capturing_plan_processor_factory(stub, plan_task_texts)
    _tfo._install_fake_acceptance_and_handoff(monkeypatch, [_tfo._FakeAcceptanceResult(passed=True)])

    first = full_orbit.run_full_job(
        ws.config, ws.ledger, _tfo._fakes(), "alpha", job_name, "파일 하나 만들어줘",
        processor_factory=factory,
    )
    assert first.status == "failed", first
    assert first.reason.startswith("plan:plan_requirements_missing"), first.reason
    assert len(plan_task_texts) == 1
    assert "[지난 계획 파일 문제" not in plan_task_texts[0]

    done = full_orbit.continue_full_job(
        ws.config, ws.ledger, _tfo._fakes(), "alpha", job_name, processor_factory=factory,
    )

    assert done.status == "done", done
    assert len(plan_task_texts) == 2
    second = plan_task_texts[1]
    assert second.index("[지난 계획 파일 문제 -- 고칠 것]") == 0
    assert "plan_invalid" in second
    assert "plan_requirements_missing" in second
    assert (ws.alpha / "src/greeter/extra1.py").exists()


# ---- 30c. the planner prompt rule sentence that heads this off -------
# ---- BEFORE it happens is wired in (test_stage_rule_sentences.py's -----
# ---- own literal-substring check covers the verbatim match; this -----
# ---- just asserts it is present at all on a first attempt's prompt) --

def test_30_plan_json_valid_json_rule_sentence_is_in_the_very_first_plan_prompt() -> None:
    from engine.application.stage_prompts import planner_prompt, small_prompt
    from engine.domain.plan_model import PLAN_JSON_VALID_RULE

    assert PLAN_JSON_VALID_RULE in planner_prompt("요청", "job1", "facts.md 내용")
    assert PLAN_JSON_VALID_RULE in small_prompt("요청", "job1")
