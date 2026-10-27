"""Plain-text task prompts engine.application.pipeline hands to run_station for
each non-build station, plus the per-task builder text for the build
loop. These are instructions TO a worker (claude/codex/scripted), never
business content of their own -- no industry, brand or keyword specifics
belong here. The only business content is `request` (the owner's own
request text) and the plan's own goal/task text, both supplied by the
caller."""
from __future__ import annotations

from pathlib import Path

from engine.domain.plan import Task
from engine.domain.plan_model import (
    PLAN_JSON_VALID_RULE, PLAN_REQUIREMENT_SCENE_COVERAGE_RULE, PLAN_SCENE_CMD_FLAGS_RULE,
    PLAN_SCENE_EXIT0_RULE, PLAN_SCHEMA_RULE, PLAN_SCHEMA_RULE_SMALL, PLAN_TASK_TEST_OWNS_RULE,
    plan_owns_rule_sentence,
)
from engine.domain.policy import OWNS_SCOPE_REQUEST_RULE
from engine.domain.stage_checks import (
    FACTS_CODE_VALUES_NO_NEEDS_CHECK_RULE, FACTS_PASS_NEEDS_SOURCE_RULE,
    PROBE_EXIT_AND_OK_LINE_RULE, PROBE_TARGETS_EMPTY_MARKER_RULE,
    PROBE_TARGETS_EXCLUDE_EXISTING_TEST_TOOLING_RULE,
    PROBE_TARGETS_INTERNAL_DOC_RULE, PROBE_TARGETS_SECTION_FORMAT_RULE,
    probe_log_format_rule, station_record_rule_sentence,
)

# Same root engine.application.steps.worker_sandbox.ENGINE_ROOT computes
# (this file is one level shallower: engine/application/stage_prompts.py
# vs. engine/application/steps/worker_sandbox.py, hence parents[2] here
# rather than parents[3] there) --
# never imported from that module directly: engine.application.steps.worker_step
# already imports engine.infrastructure.workers.prompts, so importing back from here
# would be circular. A worker's sandbox mounts ENGINE_ROOT read-only
# (engine.application.steps.worker_step._sandboxed_argv) but hides the rest of the
# apps root this engine's own checkout lives under (config.home,
# hidden_paths) -- a path OUTSIDE ENGINE_ROOT in a prompt would point
# at a file no worker can actually open.
_ENGINE_ROOT = Path(__file__).resolve().parents[2]
_ARCHITECTURE_STANDARD_PATH = _ENGINE_ROOT / "docs" / "standards" / "architecture.md"

# Shared across planner_prompt and small_prompt -- referencing the
# standard's own file path rather than pasting its rules here (keep
# the prompt short; the planner can open that file if it needs detail).
_ARCHITECTURE_LAYER_NOTE = (
    "각 작업(task)의 detail에 그 작업이 owns에 적은 파일들이 "
    f"{_ARCHITECTURE_STANDARD_PATH}의 네 층 "
    "(interface/application/domain/infrastructure) 중 어디에 속하는지 "
    "한 줄로 적는다."
)


def facts_prompt(request: str, job: str) -> str:
    return (
        f"요청: {request}\n\n"
        f".gongbang/{job}/facts.md 를 쓴다. 이 요청을 실행하는 데 필요한 "
        "사실(기준·숫자·외부 API 형식 등)을 표로 적는다: "
        "| 주장 | 판정(통과/확인 필요/탈락) | 출처 |. "
        f"{FACTS_PASS_NEEDS_SOURCE_RULE} {FACTS_CODE_VALUES_NO_NEEDS_CHECK_RULE} "
        "다음 단계로 넘어가지 못한다. 모르는 값은 지어내지 말고 확인 필요로 남긴다.\n\n"
        f"마지막으로 {PROBE_TARGETS_SECTION_FORMAT_RULE}"
        "(probe 단계는 이 절만 보고 무엇을 실제로 호출해 볼지 정한다). "
        f"{PROBE_TARGETS_EMPTY_MARKER_RULE} {PROBE_TARGETS_INTERNAL_DOC_RULE} "
        f"{PROBE_TARGETS_EXCLUDE_EXISTING_TEST_TOOLING_RULE}\n\n"
        f"{station_record_rule_sentence('facts', job)}"
    )


def prober_prompt(request: str, job: str, targets: tuple[tuple[str, str], ...]) -> str:
    from engine.domain.stage_checks import probe_target_id

    targets_text = "\n".join(
        f"- {probe_target_id(i)} (이름: {name}, 문서: {url})" for i, (name, url) in enumerate(targets))
    return (
        f"요청: {request}\n\n"
        "이 일이 의존하는 외부 대상(API·CLI·파일 형식)마다 공식 문서를 "
        "열고 실제로 한 번 호출해 본다. 대상:\n"
        f"{targets_text}\n\n"
        f"대상마다 .gongbang/{job}/probe/<번호>.py (또는 .sh, .js)에 "
        "10~30줄짜리 실제 호출 스크립트를 쓰고 한 번 실행한다. <번호>는 위 목록 "
        "맨 앞의 t1, t2 같은 값이다. 파일 이름을 대상 이름으로 만들지 않는다. 엔진이 이 스크립트를 다시 "
        f"실행해 통과를 판정한다. {PROBE_EXIT_AND_OK_LINE_RULE} 그다음 "
        f"{probe_log_format_rule(job)} "
        "재사용할 어댑터는 만들지 않는다 -- 호출이 되는지 확인하는 "
        "스크립트와 로그만 남긴다. 비밀값은 로그에 남기지 않는다. "
        f"{station_record_rule_sentence('probe', job)}"
    )


def planner_prompt(request: str, job: str, facts_md: str, tool_catalog: str = "",
                   probe_text: str = "", build_prefixes: "tuple[str, ...] | None" = None,
                   manifest: str | None = None) -> str:
    """`build_prefixes`/`manifest` are the project's own
    engine.application.pipeline_stages.build_prefixes(config) and
    ProjectConfig.manifest -- passed through so plan_owns_rule_sentence
    can tell the planner the SAME owns boundary validate_plan will
    check the written plan.json against (결함 27). None (the default)
    drops the sentence entirely, same as plan_owns_rule_sentence itself
    -- a caller with no build_prefixes of its own (there is none today;
    kept for parity with validate_plan's own None-skip contract)."""
    probe_part = f"\n\n{probe_text}" if probe_text else ""
    owns_rule = plan_owns_rule_sentence(build_prefixes, manifest)
    owns_rule_part = f" {owns_rule}" if owns_rule else ""
    tools_part = ""
    if tool_catalog:
        tools_part = (
            "\n\n이 일에 쓸 도구표도 plan.json의 \"tools\"에 적는다: "
            '[{"kind": "skill"|"mcp", "name": str, "stations": [칸...], "why": str}]. '
            "칸은 facts·plan·design·build·review 중에서 고른다. 아래 목록 전체에서 "
            "이 일에 실제로 도움이 되는 스킬과 MCP를 칸마다 고르고, 왜 쓰는지 "
            "한 줄로 적는다. 고른 스킬은 그 칸 작업자의 지시문에 본문이 들어간다. "
            "목록에 없는데 꼭 필요한 것은 그대로 적으면 \"설치 필요(사람 승인)\"로 "
            "보고된다.\n\n## 고를 수 있는 도구\n" + tool_catalog
        )
    return (
        f"요청: {request}\n\n"
        f"사실확정 결과(.gongbang/{job}/facts.md):\n{facts_md}"
        f"{probe_part}\n\n"
        f".gongbang/{job}/plan.json 과, 필요하면 tests/scenes/ 아래 장면 "
        f"스크립트를 쓴다. {PLAN_SCHEMA_RULE} {PLAN_JSON_VALID_RULE} "
        f"{PLAN_SCENE_EXIT0_RULE} {PLAN_SCENE_CMD_FLAGS_RULE} {PLAN_TASK_TEST_OWNS_RULE} "
        "작업(task)마다 owns에 그 작업이 "
        "쓸 파일·폴더만 적는다(다른 작업의 소유 경로와 겹치지 않게 나눈다)."
        f"{owns_rule_part} "
        "depends_on으로 작업 순서를 정한다. 화면이 있는 요청이면 ui를 "
        "true로 둔다.\n\n"
        "requirements에는 이 요청이 실제로 요구하는 것을 하나씩 적는다 "
        "(기능은 kind \"function\", 권한·데이터 분리 같은 보안 요구는 kind "
        f"\"security\"). {PLAN_REQUIREMENT_SCENE_COVERAGE_RULE} 보안 요구는 막혀야 할 행동이 실제로 막히는지 "
        "확인하는 장면으로 쓴다 — 예: A 사용자로 B 사용자의 데이터를 보거나 "
        "고칠 수 없어야 한다. 장면이 없는 requirement가 있으면 이 계획은 "
        "통과하지 못한다.\n\n"
        f"{_ARCHITECTURE_LAYER_NOTE}\n\n"
        f"{station_record_rule_sentence('plan', job)}"
        + tools_part
    )


def small_prompt(request: str, job: str, tool_catalog: str = "",
                 build_prefixes: "tuple[str, ...] | None" = None,
                 manifest: str | None = None) -> str:
    """engine.application.quick_pipeline's own combined facts+plan call -- one worker
    turn writes BOTH .gongbang/<job>/facts.md and .gongbang/<job>/
    plan.json (plus tests/scenes/ if needed), instead of the full
    orbit's separate facts/probe/plan stations. Only ever used for a
    request small enough to skip the probe station entirely -- the
    worker still writes facts.md's own "## 외부 대상" section, and
    engine.application.quick_pipeline refuses small mode outright (falls back to the
    full job) if that section names any real target, same as the full
    orbit's own probe-skip rule (engine.domain.stage_checks.check_probe_targets)."""
    tools_part = ""
    if tool_catalog:
        tools_part = (
            "\n\n이 일에 쓸 도구표도 plan.json의 \"tools\"에 적는다: "
            '[{"kind": "skill"|"mcp", "name": str, "stations": [칸...], "why": str}]. '
            "칸은 facts·plan·design·build·review 중에서 고른다. 아래 목록 전체에서 "
            "이 일에 실제로 도움이 되는 스킬과 MCP를 칸마다 고르고, 왜 쓰는지 "
            "한 줄로 적는다. 목록에 없는데 꼭 필요한 것은 그대로 적으면 "
            "\"설치 필요(사람 승인)\"로 보고된다.\n\n## 고를 수 있는 도구\n" + tool_catalog
        )
    owns_rule = plan_owns_rule_sentence(build_prefixes, manifest)
    owns_rule_part = f" {owns_rule}" if owns_rule else ""
    return (
        f"요청: {request}\n\n"
        "이 일은 작게 끝낼 수 있는 변경이다. 사실확정과 계획을 한 번에 끝낸다.\n\n"
        f"1) 먼저 .gongbang/{job}/facts.md 를 쓴다. 이 요청을 실행하는 데 필요한 "
        "사실(기준·숫자·외부 API 형식 등)을 표로 적는다: "
        "| 주장 | 판정(통과/확인 필요/탈락) | 출처 |. "
        f"{FACTS_PASS_NEEDS_SOURCE_RULE} {FACTS_CODE_VALUES_NO_NEEDS_CHECK_RULE} "
        "끝나지 못한다. 모르는 값은 지어내지 말고 확인 필요로 남긴다. "
        f"마지막으로 {PROBE_TARGETS_SECTION_FORMAT_RULE}. 의존하는 외부 대상이 "
        f"있으면 이 요청은 작게 끝낼 수 없다. {PROBE_TARGETS_EMPTY_MARKER_RULE} "
        f"{PROBE_TARGETS_INTERNAL_DOC_RULE} "
        f"{PROBE_TARGETS_EXCLUDE_EXISTING_TEST_TOOLING_RULE}\n\n"
        f"2) 이어서 .gongbang/{job}/plan.json 과, 필요하면 tests/scenes/ 아래 "
        "장면 스크립트를 쓴다. 이 계획은 작업(tasks)을 정확히 1개만 담는다 "
        f"— 작게 끝낼 수 있는 변경이므로 여러 작업으로 나누지 않는다. "
        f"{PLAN_SCHEMA_RULE_SMALL} {PLAN_JSON_VALID_RULE} "
        "requirements에는 이 요청이 실제로 요구하는 것을 최소 1개 적는다 "
        "(기능은 kind \"function\", 권한·데이터 분리 같은 보안 요구는 kind "
        f"\"security\"). {PLAN_REQUIREMENT_SCENE_COVERAGE_RULE} 장면이 없는 requirement가 "
        f"있으면 이 계획은 통과하지 못한다. {PLAN_SCENE_EXIT0_RULE} "
        f"{PLAN_SCENE_CMD_FLAGS_RULE} {PLAN_TASK_TEST_OWNS_RULE} "
        f"tasks[0].owns에는 이 작업이 쓸 파일·폴더만 적는다.{owns_rule_part}\n\n"
        f"{_ARCHITECTURE_LAYER_NOTE}"
        + tools_part
    )


def designer_prompt(request: str, job: str, plan_goal: str) -> str:
    return (
        f"요청: {request}\n\n목표: {plan_goal}\n\n"
        f".gongbang/{job}/design/design.md 와 필요한 시안 파일을 쓴다. "
        "화면 구조·레이아웃·상태(빈 화면·로딩·오류 포함)를 글과 시안으로 "
        "보여준다. 이 시안은 사람이 보고 승인해야 다음 단계(구현)로 "
        f"넘어간다. 만든 화면의 스크린샷은 .gongbang/{job}/screens/ 아래에 "
        f"저장한다. {station_record_rule_sentence('design', job)}"
    )


def builder_task_text(task: Task, plan_goal: str, job: str | None = None,
                      ui: bool = False) -> str:
    owns_list = ", ".join(task.owns)
    text = (
        f"목표: {plan_goal}\n\n작업: {task.title}\n\n{task.detail}\n\n"
        f"이 작업에서 고칠 수 있는 경로는 다음뿐이다: {owns_list}. 이 범위 "
        "밖은 고치지 않는다. tests/scenes/ 는 쓰지 않는다(그 경로는 계획 "
        f"단계가 이미 끝냈다). {OWNS_SCOPE_REQUEST_RULE}"
    )
    if ui and job:
        text += (
            f"\n\n화면을 만들었으면 그 화면의 스크린샷을 .gongbang/{job}/"
            "screens/ 아래에 저장한다."
        )
    return text


def fix_task_text(task: Task, plan_goal: str, failing_scenes: tuple[str, ...],
                  job: str | None = None, ui: bool = False, *,
                  full_suite_returncode: int | None = None,
                  full_suite_tail: str = "") -> str:
    """`full_suite_returncode`/`full_suite_tail` (engine.application.acceptance.
    AcceptanceResult's own fields, read with getattr default by the
    caller so this works whether or not that field exists yet) are
    given only when NO scene failed but the project's own full test
    suite did -- the one case where `failing_scenes` alone leaves the
    builder with nothing to go on."""
    base = builder_task_text(task, plan_goal, job, ui)
    if failing_scenes:
        scenes_list = ", ".join(failing_scenes)
        return (
            f"{base}\n\n수락 시험에서 다음 통과 장면이 실패했다: {scenes_list}. "
            "원인을 고쳐 다시 통과하게 만든다."
        )
    tail = full_suite_tail[-2000:] if full_suite_tail else "(출력 없음)"
    return (
        f"{base}\n\n통과 장면은 모두 통과했지만, 프로젝트의 전체 시험(test_cmd)이 "
        f"실패했다 (종료 코드 {full_suite_returncode}). 마지막 출력:\n{tail}\n\n"
        "원인을 고쳐 전체 시험이 다시 통과하게 만든다."
    )


__all__ = [
    "facts_prompt", "prober_prompt", "planner_prompt", "small_prompt", "designer_prompt",
    "builder_task_text", "fix_task_text",
]
