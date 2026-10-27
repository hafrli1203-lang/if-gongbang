"""Literal path prefixes shared by plan validation, the planner rule sentences and the
steps that treat planner-owned files specially. Split out of engine.domain.plan_model so the
rule sentences can live in their own module without an import cycle."""
from __future__ import annotations

# The one shared literal every module that cares about this boundary
# (this one, engine.application.steps.merge's _PLANNER_ONLY_PREFIX,
# engine.application.steps.suite_select's own full-scope exclusion) must use --
# the planner-owned scene-script directory inside a project's own repo.
# Exported so a later caller imports the VALUE rather than re-typing the
# literal a third time.
SCENES_PREFIX = "tests/scenes/"

# validate_plan's own 결함 42 check (a build task touching src/ must
# also own a tests/ path of its own) -- the two literal prefixes it
# compares against, named here once rather than re-typed at the check
# site and in plan_rule_sentences' own PLAN_TASK_TEST_OWNS_RULE text.
SRC_OWNS_PREFIX = "src/"
TEST_OWNS_PREFIX = "tests/"

__all__ = ["SCENES_PREFIX", "SRC_OWNS_PREFIX", "TEST_OWNS_PREFIX"]
