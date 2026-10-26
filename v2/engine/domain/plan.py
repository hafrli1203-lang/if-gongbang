"""plan.json — the plan a planner station writes for one job: a goal,
pass/fail scenes (acceptance scripts a human's own action maps to), the
task breakdown (each task's own write prefixes and dependency order),
and plain-text acceptance criteria.

plan.json lives at repo/.gongbang/<job>/plan.json -- part of the app
repo's own committed job trail, like facts.md/report.md (see the
full-orbit contract). See engine.domain.plan_model's own module docstring for
the scene-cmd-runner rules (why a `covers`-claiming scene must actually
RUN its tests/scenes/ script, not merely mention its path).

This module is the stable import path for three pieces that now live
in their own files:
  - engine.domain.plan_model: Scene/Task/Requirement/Tool/Plan, PlanError,
    load_plan, ordered_tasks, requirement_coverers, validate_plan (the
    parse/validate half).
  - engine.domain.plan_review: scene_file_digests, plan_digest,
    record_plan_reviewed, is_plan_reviewed, verify_reviewed_scenes,
    check_resumed_plan (the digest/review-record half).
  - engine.domain.plan_tools: tools_for_station, skill_catalog_text,
    classify_skill_tools, validate_tools (the skill/mcp catalog half).
Everything below is re-exported here unchanged so no existing caller
of `engine.domain.plan` needs to change -- including engine.application.acceptance's own
`from engine.domain.plan import _scene_script_arg` (a private name import of
a function engine.domain.plan_model still owns)."""
from __future__ import annotations

from engine.domain.plan_model import (
    Plan, PlanError, Requirement, Scene, Task, Tool, load_plan, ordered_tasks,
    plan_path, requirement_coverers, validate_plan, scene_script_arg as _scene_script_arg,
)
from engine.domain.plan_review import (
    check_resumed_plan, is_plan_reviewed, plan_digest, record_plan_reviewed,
    scene_file_digests, verify_reviewed_scenes,
)
from engine.domain.plan_tools import (
    classify_skill_tools, skill_catalog_text, tools_for_station, validate_tools,
)

__all__ = [
    "Scene", "Task", "Tool", "Requirement", "Plan", "PlanError", "plan_path", "load_plan",
    "validate_plan", "validate_tools", "tools_for_station", "classify_skill_tools",
    "ordered_tasks", "requirement_coverers", "plan_digest", "scene_file_digests",
    "record_plan_reviewed", "check_resumed_plan", "verify_reviewed_scenes",
    "is_plan_reviewed",
]
