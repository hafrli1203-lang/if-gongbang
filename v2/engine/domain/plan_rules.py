"""The literal Korean rule sentences the planner prompts must carry verbatim (split out of
engine.domain.plan_model to keep that module under the 800-line budget). The names are still
re-exported by engine.domain.plan_model."""
from __future__ import annotations

from engine.domain.plan_paths import SRC_OWNS_PREFIX, TEST_OWNS_PREFIX

# ---------------------------------------------------------------------------
# Rule sentences -- same purpose as engine.domain.stage_checks' own rule
# sentences (see that module's docstring for the incident this pattern
# guards against): the literal Korean text a planner-station prompt
# (engine.application.stage_prompts.planner_prompt/small_prompt) must carry
# VERBATIM for a rule this module actually enforces (_parse_plan's schema,
# validate_plan's requirement-coverage gate, Scene's own exit-0 contract).
# tests/unit/test_stage_rule_sentences.py checks this.

# _parse_plan's own required top-level shape -- goal/ui/scenes/tasks/
# acceptance, plus the optional requirements/tools lists -- exactly as the
# full-orbit planner must write it (depends_on free-form per task).
PLAN_SCHEMA_RULE = (
    'plan.json 스키마: '
    '{"goal": str, "ui": bool, '
    '"requirements": [{"id","text","kind": "function"|"security"}], '
    '"scenes": [{"id","actor_does","app_shows","where","cmd": [argv...], '
    '"covers": [requirement id...]}], '
    '"tasks": [{"id","title","detail","owns": [관련 경로...], '
    '"depends_on": [task id...], "covers": [requirement id...]}], '
    '"acceptance": [str]}.'
)

# Same schema, for the `--small` worker turn (quick_pipeline.small_prompt)
# whose plan always has exactly one task -- depends_on is fixed to [] there
# rather than free-form, so this is its own constant, never PLAN_SCHEMA_RULE
# reused with a find/replace.
PLAN_SCHEMA_RULE_SMALL = (
    'plan.json 스키마: '
    '{"goal": str, "ui": bool, '
    '"requirements": [{"id","text","kind": "function"|"security"}], '
    '"scenes": [{"id","actor_does","app_shows","where","cmd": [argv...], '
    '"covers": [requirement id...]}], '
    '"tasks": [{"id","title","detail","owns": [관련 경로...], '
    '"depends_on": [], "covers": [requirement id...]}], '
    '"acceptance": [str]}.'
)

# validate_plan's own requirement_coverers gate: a task-only `covers` is
# tracking only, never sufficient -- only a PASSING scene's own `covers`
# satisfies a requirement.
PLAN_REQUIREMENT_SCENE_COVERAGE_RULE = (
    "모든 requirement는 그것을 실제로 확인하는 장면 하나 이상의 covers에 "
    "그 id로 나타나야 한다(작업의 covers는 추적용일 뿐 충족 근거가 아니다)."
)

# Scene's own dataclass docstring/contract ("runs in repo root; exit 0 =
# pass") -- stated to the planner so a scene it writes actually behaves
# that way.
PLAN_SCENE_EXIT0_RULE = (
    "장면(scene)마다 cmd가 실행할 스크립트를 tests/scenes/ 아래에 실제로 "
    "만든다 — 통과 장면은 사람의 행동 → 결과를 그대로 자동으로 확인하는 "
    "스크립트이고, exit 0이면 통과다."
)

# scene_script_arg's own _SAFE_SCENE_CMD_FLAGS allowlist -- stated to the
# planner so a scene cmd copying a project's own test_cmd convention
# (e.g. "npx vitest run --no-color --no-cache") is not silently rejected
# by validate_plan's scene_cmd_not_a_scene_file check, and so a planner
# never reaches for a value-taking flag ("--config x", "--root=..")
# scene_script_arg will never accept there (see that allowlist's own
# comment for why: an exact-match allowlist, never a prefix/pattern one,
# so a flag that merely LOOKS safe is never treated as safe).
PLAN_SCENE_CMD_FLAGS_RULE = (
    '장면의 cmd가 "npx", "vitest", "run" 다음에 플래그를 더 쓸 때는 값이 '
    '없는 "--no-color", "--no-cache", "--silent" 중에서만 고르고, 장면 '
    '스크립트 경로는 그 뒤에 그대로 와야 한다 -- "--config x"나 '
    '"--root=.."처럼 값을 받는 플래그는 쓰지 않는다.'
)

# load_plan's own json.loads gate (결함 30): a goal/detail string that
# contains a literal double quote (e.g. a shell flag quoted as `--tools
# ""`) breaks plan.json's own JSON syntax if written unescaped -- the
# real, repeating failure (plan_unreadable) this rule sentence exists to
# head off before it happens, rather than only explaining it after a
# reopened station already repeated it (see plan_check_failure_note in
# engine.application.pipeline_planning for the after-the-fact half).
PLAN_SCENE_FILE_DISCOVERABLE_RULE = (
    '장면의 cmd가 "npx vitest run"일 때는 장면 스크립트 파일 이름이 '
    "vitest 기본 시험 탐색 규칙(파일명이 \".test.ts\"/\".spec.ts\" 등 "
    '".test"나 ".spec"으로 끝나야 함)에 맞아야 한다 -- 그 밖의 이름'
    "(예: \".scene.ts\")은 파일이 있어도 vitest가 찾지 못해 "
    '"No test files found"로 매번 실패한다.'
)

PLAN_JSON_VALID_RULE = (
    "plan.json은 유효한 JSON이어야 한다 -- 문자열 안의 쌍따옴표는 \\\"로 "
    "이스케이프한다. 쓴 뒤 직접 파싱해 보고 확인한다."
)


# validate_plan's own 결함 42 check (src/ owns needs a tests/ owns of
# its own, new plans only -- see that check's own comment for why it is
# gated on build_prefixes rather than a separate "new vs resumed" flag).
PLAN_TASK_TEST_OWNS_RULE = (
    f'owns에 "{SRC_OWNS_PREFIX}" 아래 경로가 있는 작업(task)은 "{TEST_OWNS_PREFIX}" 아래 '
    "자기 시험 파일 경로를 owns에 최소 하나 넣는다 -- 그 작업의 build 단계가 쓸 시험 "
    "파일 자리를 미리 정해 두는 것이다."
)


def plan_rule_sentences() -> tuple[str, ...]:
    """Every rule sentence the full-orbit planner prompt (planner_prompt)
    must carry verbatim."""
    return (
        PLAN_SCHEMA_RULE, PLAN_JSON_VALID_RULE, PLAN_REQUIREMENT_SCENE_COVERAGE_RULE,
        PLAN_SCENE_EXIT0_RULE, PLAN_SCENE_CMD_FLAGS_RULE, PLAN_SCENE_FILE_DISCOVERABLE_RULE,
        PLAN_TASK_TEST_OWNS_RULE,
    )


def plan_rule_sentences_small() -> tuple[str, ...]:
    """Same, for the `--small` combined facts+plan prompt (small_prompt),
    whose plan.json schema sentence is PLAN_SCHEMA_RULE_SMALL instead."""
    return (
        PLAN_SCHEMA_RULE_SMALL, PLAN_JSON_VALID_RULE, PLAN_REQUIREMENT_SCENE_COVERAGE_RULE,
        PLAN_SCENE_EXIT0_RULE, PLAN_SCENE_CMD_FLAGS_RULE, PLAN_SCENE_FILE_DISCOVERABLE_RULE,
        PLAN_TASK_TEST_OWNS_RULE,
    )


__all__ = [
    "PLAN_SCHEMA_RULE", "PLAN_SCHEMA_RULE_SMALL", "PLAN_REQUIREMENT_SCENE_COVERAGE_RULE",
    "PLAN_SCENE_EXIT0_RULE", "PLAN_SCENE_CMD_FLAGS_RULE", "PLAN_SCENE_FILE_DISCOVERABLE_RULE",
    "PLAN_JSON_VALID_RULE", "PLAN_TASK_TEST_OWNS_RULE",
    "plan_rule_sentences", "plan_rule_sentences_small",
]
