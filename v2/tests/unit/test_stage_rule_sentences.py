"""The actual incident this guards against (docs/ai task s28): the prober
worker's own instructions (engine.application.stage_prompts.prober_prompt)
once said only "로그에 PROBE OK" while the judgment code
(engine.application.acceptance_probe._probe_observed) required "PROBE OK
<문서 URL>" on the script's own stdout -- a worker that followed the
prompt to the letter still failed the probe, because the rule sentence
was hand-typed twice (once in the prompt, once -- differently -- in the
check) instead of written once and reused.

Every check this applies to now exposes its own rule sentence(s) as a
constant/function living right next to the check itself
(engine.domain.stage_checks, engine.domain.plan_model), and
engine.application.stage_prompts imports and splices those exact strings
into its own prompt text rather than retyping them. This module has two
kinds of test:

  1. literal-substring tests -- every rule sentence a check exposes is a
     literal substring of the station prompt that is supposed to tell the
     worker about it. Add a new rule sentence without wiring it into the
     matching prompt, and one of these fails -- that is the whole point.
  2. follow-the-instructions tests -- an artifact built exactly the way a
     prompt instructs (a facts.md table, a probe script's stdout tail, a
     plan.json) actually passes the check that same prompt exists to
     satisfy.
"""
from __future__ import annotations

from pathlib import Path

from engine.application.acceptance_probe import _probe_observed
from engine.application.stage_prompts import (
    designer_prompt, facts_prompt, planner_prompt, prober_prompt, small_prompt,
)
from engine.domain.plan_model import (
    load_plan, plan_owns_rule_sentence, plan_path, plan_rule_sentences,
    plan_rule_sentences_small, validate_plan,
)
from engine.domain.stage_checks import (
    check_facts, check_probe_targets, facts_rule_sentences, prober_rule_sentences,
    station_record_rule_sentence,
)

_JOB = "job1"


# --------------------------------------------------------------------- 1 ---

def test_facts_rule_sentences_appear_verbatim_in_facts_prompt() -> None:
    text = facts_prompt("요청 내용", _JOB)
    for sentence in facts_rule_sentences():
        assert sentence in text, f"missing from facts_prompt: {sentence!r}"


def test_facts_rule_sentences_appear_verbatim_in_small_prompt() -> None:
    text = small_prompt("요청 내용", _JOB)
    for sentence in facts_rule_sentences():
        assert sentence in text, f"missing from small_prompt: {sentence!r}"


def test_prober_rule_sentences_appear_verbatim_in_prober_prompt() -> None:
    text = prober_prompt("요청 내용", _JOB, (("대상1", "https://example.com/docs"),))
    for sentence in prober_rule_sentences(_JOB):
        assert sentence in text, f"missing from prober_prompt: {sentence!r}"


def test_plan_rule_sentences_appear_verbatim_in_planner_prompt() -> None:
    text = planner_prompt("요청 내용", _JOB, "facts.md 내용")
    for sentence in plan_rule_sentences():
        assert sentence in text, f"missing from planner_prompt: {sentence!r}"


def test_plan_rule_sentences_small_appear_verbatim_in_small_prompt() -> None:
    text = small_prompt("요청 내용", _JOB)
    for sentence in plan_rule_sentences_small():
        assert sentence in text, f"missing from small_prompt: {sentence!r}"


# 결함 12: facts/probe/plan/design each get a station_record_
# rule_sentence telling the worker which path is ITS OWN record, so the
# rule a worker is told and merge.py's own
# engine.domain.stage_checks.other_station_record_violation check can
# never drift apart -- same pattern as every sentence above.

def test_facts_station_record_rule_appears_verbatim_in_facts_prompt() -> None:
    text = facts_prompt("요청 내용", _JOB)
    assert station_record_rule_sentence("facts", _JOB) in text


def test_probe_station_record_rule_appears_verbatim_in_prober_prompt() -> None:
    text = prober_prompt("요청 내용", _JOB, (("대상1", "https://example.com/docs"),))
    assert station_record_rule_sentence("probe", _JOB) in text


def test_plan_station_record_rule_appears_verbatim_in_planner_prompt() -> None:
    text = planner_prompt("요청 내용", _JOB, "facts.md 내용")
    assert station_record_rule_sentence("plan", _JOB) in text


def test_design_station_record_rule_appears_verbatim_in_designer_prompt() -> None:
    text = designer_prompt("요청 내용", _JOB, "목표")
    assert station_record_rule_sentence("design", _JOB) in text


# --------------------------------------------------------------------- 2 ---

def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_facts_md_written_the_way_facts_prompt_instructs_passes_check_facts(
    tmp_path: Path,
) -> None:
    # Exactly the shape facts_prompt tells the worker to write: a pass
    # row with a source, a 「코드에 넣는 값」 section with no 확인 필요
    # left in it, and an "## 외부 대상" section ending in "(없음)".
    facts_md = (
        "| 주장 | 판정 | 출처 |\n"
        "|---|---|---|\n"
        "| 요청 요약 | 통과 | request |\n\n"
        "## 코드에 넣는 값\n"
        "- 제한값: 10 (출처: docs/spec.md:3)\n\n"
        "## 외부 대상\n"
        "- (없음)\n"
    )
    _write(tmp_path / ".gongbang" / _JOB / "facts.md", facts_md)
    assert check_facts(tmp_path, _JOB) == []
    assert check_probe_targets(tmp_path, _JOB) == []


def test_facts_md_with_a_real_probe_target_written_as_instructed_parses(
    tmp_path: Path,
) -> None:
    facts_md = (
        "| 주장 | 판정 | 출처 |\n"
        "|---|---|---|\n"
        "| 요청 요약 | 통과 | request |\n\n"
        "## 외부 대상\n"
        "- 네이버 지도 API: https://developers.naver.com/docs/map\n"
    )
    _write(tmp_path / ".gongbang" / _JOB / "facts.md", facts_md)
    assert check_facts(tmp_path, _JOB) == []
    assert check_probe_targets(tmp_path, _JOB) == [
        ("네이버 지도 API", "https://developers.naver.com/docs/map"),
    ]


def test_probe_stdout_written_the_way_prober_prompt_instructs_passes_probe_observed() -> None:
    # prober_prompt: "성공했을 때 표준 출력 마지막에 정확히 `PROBE OK
    # <문서>` 한 줄을 찍는다 -- <문서>는 ... "문서:" 값을 글자 그대로
    # 쓴다". The <문서> value handed to the worker (prober_prompt's own
    # targets_text) is literally the target's url, so the stdout tail a
    # worker who follows that instruction produces is "PROBE OK <url>".
    url = "https://example.com/docs"
    returncode = 0
    tail = f"(요청 중...)\nPROBE OK {url}\n"
    assert _probe_observed(returncode, tail, url) is True


def test_probe_stdout_that_only_half_follows_the_instruction_fails_probe_observed() -> None:
    # A script that exits 0 but never actually names the target's own
    # url must still fail -- exit 0 alone proves nothing (see
    # _probe_observed's own docstring).
    url = "https://example.com/docs"
    assert _probe_observed(0, "PROBE OK\n", url) is False
    assert _probe_observed(1, f"PROBE OK {url}\n", url) is False


def _write_plan_for(repo: Path, job: str, scene_cmd: tuple[str, ...]) -> None:
    import json

    plan = {
        "goal": "목표",
        "ui": False,
        "requirements": [{"id": "r1", "text": "요구 사항", "kind": "function"}],
        "scenes": [{
            "id": "s1", "actor_does": "누른다", "app_shows": "보인다", "where": "/",
            "cmd": list(scene_cmd), "covers": ["r1"],
        }],
        "tasks": [{
            "id": "t1", "title": "작업", "detail": "", "owns": ["src/"],
            "depends_on": [], "covers": ["r1"],
        }],
        "acceptance": ["끝"],
    }
    _write(plan_path(repo, job), json.dumps(plan, ensure_ascii=False))


def test_plan_json_written_to_the_schema_planner_prompt_states_loads_and_validates(
    tmp_path: Path,
) -> None:
    # The scene must actually RUN a tests/scenes/ script via a supported
    # runner (PLAN_SCENE_EXIT0_RULE) and the requirement must be covered
    # by that scene (PLAN_REQUIREMENT_SCENE_COVERAGE_RULE) -- both stated
    # to the planner in planner_prompt, both checked here against a
    # plan.json that actually does what the prompt instructs.
    scene_script = tmp_path / "tests" / "scenes" / "s1.py"
    _write(scene_script, "import sys\nsys.exit(0)\n")
    _write_plan_for(tmp_path, _JOB, ("python3", "tests/scenes/s1.py"))

    plan = load_plan(tmp_path, _JOB)
    assert validate_plan(plan, tmp_path, build_prefixes=("src/",)) == []


def test_plan_json_whose_scene_does_not_actually_run_the_script_fails_validate_plan(
    tmp_path: Path,
) -> None:
    # The planner-prompt text says to make the scene's cmd RUN the
    # script -- a cmd that merely mentions the path without running it
    # (the exact "true tests/scenes/x.py" shape plan_model's own module
    # docstring calls out) must still be rejected.
    scene_script = tmp_path / "tests" / "scenes" / "s1.py"
    _write(scene_script, "import sys\nsys.exit(0)\n")
    _write_plan_for(tmp_path, _JOB, ("true", "tests/scenes/s1.py"))

    plan = load_plan(tmp_path, _JOB)
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/",))
    assert any("scene_cmd_not_a_scene_file" in p for p in problems)


def test_plan_json_scene_cmd_with_the_safe_flags_the_rule_sentence_names_passes(
    tmp_path: Path,
) -> None:
    # PLAN_SCENE_CMD_FLAGS_RULE's own example cmd -- a planner that
    # copies a project's own "npx vitest run --no-color --no-cache"
    # test_cmd convention into a scene must not be rejected for it.
    scene_script = tmp_path / "tests" / "scenes" / "r2-x.test.ts"
    _write(scene_script, "test('x', () => {})\n")
    _write_plan_for(
        tmp_path, _JOB,
        ("npx", "vitest", "run", "--no-color", "--no-cache", "tests/scenes/r2-x.test.ts"),
    )

    plan = load_plan(tmp_path, _JOB)
    assert validate_plan(plan, tmp_path, build_prefixes=("src/",)) == []


def test_plan_json_scene_cmd_with_a_value_taking_flag_still_fails_validate_plan(
    tmp_path: Path,
) -> None:
    # "--config x"/"--root=.." are exactly the value-taking flags
    # PLAN_SCENE_CMD_FLAGS_RULE tells the planner NOT to use -- confirm
    # scene_script_arg still rejects them (an exact-match allowlist,
    # never a prefix one).
    scene_script = tmp_path / "tests" / "scenes" / "r2-x.test.ts"
    _write(scene_script, "test('x', () => {})\n")
    _write_plan_for(
        tmp_path, _JOB,
        ("npx", "vitest", "run", "--root=..", "tests/scenes/r2-x.test.ts"),
    )

    plan = load_plan(tmp_path, _JOB)
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/",))
    assert any("scene_cmd_not_a_scene_file" in p for p in problems)


# --------------------------------------------------------------------- 결함 27 ---
#, marketing-chat job, planning round 2: plan check failed with
# "task T11: owns 'package.json' is outside build prefixes ['src/', ...]"
# -- validate_plan's owns-vs-build_prefixes check (plan_model.py) was
# right to reject it, but neither planner_prompt nor small_prompt ever
# told the planner what owns may contain or where a manifest change
# belongs instead, so the SAME rejection kept recurring. Fixed by
# plan_owns_rule_sentence, spliced into both prompts via the
# build_prefixes/manifest params their real callers
# (pipeline_planning.run_plan_and_review, quick_pipeline._run_from_attempt)
# now pass through.

def test_plan_owns_rule_sentence_appears_verbatim_in_planner_prompt_with_real_prefixes() -> None:
    prefixes = ("src/", "tests/", "docs/ai/")
    text = planner_prompt(
        "요청 내용", _JOB, "facts.md 내용", build_prefixes=prefixes, manifest="package.json",
    )
    sentence = plan_owns_rule_sentence(prefixes, "package.json")
    assert sentence is not None
    assert sentence in text
    # The sentence itself must actually name the real prefixes, not a
    # placeholder -- otherwise the planner still can't tell what it may
    # own.
    for prefix in prefixes:
        assert prefix in sentence
    assert "package.json" in sentence


def test_plan_owns_rule_sentence_appears_verbatim_in_small_prompt_with_real_prefixes() -> None:
    prefixes = ("src/",)
    text = small_prompt(
        "요청 내용", _JOB, build_prefixes=prefixes, manifest="requirements.txt",
    )
    sentence = plan_owns_rule_sentence(prefixes, "requirements.txt")
    assert sentence is not None
    assert sentence in text


def test_plan_owns_rule_sentence_is_none_and_absent_when_build_prefixes_unknown() -> None:
    assert plan_owns_rule_sentence(None, "package.json") is None
    # planner_prompt/small_prompt must not fabricate a prefix list or a
    # sentence about one when the caller has none to give (same
    # None-skip convention as validate_plan's own build_prefixes=None).
    planner_text = planner_prompt("요청 내용", _JOB, "facts.md 내용")
    small_text = small_prompt("요청 내용", _JOB)
    assert "owns는 다음 경로 아래에서만" not in planner_text
    assert "owns는 다음 경로 아래에서만" not in small_text


def test_plan_whose_owns_stay_inside_the_rule_sentences_prefixes_passes_validate_plan(
    tmp_path: Path,
) -> None:
    # A plan that follows plan_owns_rule_sentence's own instruction --
    # owns only under the named prefixes, no manifest path in owns --
    # must pass the exact check that sentence exists to explain.
    prefixes = ("src/", "tests/")
    scene_script = tmp_path / "tests" / "scenes" / "s1.py"
    _write(scene_script, "import sys\nsys.exit(0)\n")
    _write_plan_for(tmp_path, _JOB, ("python3", "tests/scenes/s1.py"))

    plan = load_plan(tmp_path, _JOB)
    assert validate_plan(plan, tmp_path, build_prefixes=prefixes) == []


def test_plan_owning_the_manifest_path_directly_fails_validate_plan_as_the_sentence_warns(
    tmp_path: Path,
) -> None:
    # The exact 결함 27 shape: a task owns "package.json" directly,
    # which is outside build_prefixes -- this is precisely what
    # plan_owns_rule_sentence tells the planner not to do.
    import json

    prefixes = ("src/", "tests/")
    scene_script = tmp_path / "tests" / "scenes" / "s1.py"
    _write(scene_script, "import sys\nsys.exit(0)\n")
    plan_json = {
        "goal": "목표",
        "ui": False,
        "requirements": [{"id": "r1", "text": "요구 사항", "kind": "function"}],
        "scenes": [{
            "id": "s1", "actor_does": "누른다", "app_shows": "보인다", "where": "/",
            "cmd": ["python3", "tests/scenes/s1.py"], "covers": ["r1"],
        }],
        "tasks": [{
            "id": "t1", "title": "작업", "detail": "", "owns": ["package.json"],
            "depends_on": [], "covers": ["r1"],
        }],
        "acceptance": ["끝"],
    }
    _write(plan_path(tmp_path, _JOB), json.dumps(plan_json, ensure_ascii=False))

    plan = load_plan(tmp_path, _JOB)
    problems = validate_plan(plan, tmp_path, build_prefixes=prefixes)
    assert any(
        "owns 'package.json' is outside build prefixes" in p for p in problems
    ), problems
