"""Structural test for "작업 단계 실행 입구 하나" (plans/-gongbang-
entry-unify.md §A): a build task's own stage list (run_station -> merge
-> review -> bench -> commit -> report -> close) must be run through
exactly one door, engine.application.task_stage_runner.run_task_stages,
which is where the handoff filter and the owner-stop recording now both
live. Three different callers used to each build their own copy of that
logic around their own direct `orbit.continue_job` call -- the same root
cause reappeared three times (결함 31, 33b, 40) because a fix only ever
landed at some of the doors.

This test walks engine/application's own source with `ast` and fails if
any NEW `orbit.continue_job(...)` / `continue_job(...)` call shows up
outside task_stage_runner.py's own definition of run_task_stages, unless
that file is explicitly allow-listed below for calling continue_job for
something that is NOT a build task's stage list (a plan review, or a
single-task `gb2 run`/`gb2 open`+`run` pair) -- each with a one-line
reason it is not a build-task entry."""
from __future__ import annotations

import ast
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[2] / "engine"

# The one function allowed to call orbit.continue_job for a build task's
# own stage list.
RUNNER_MODULE = "task_stage_runner.py"

# Every OTHER file under engine/ that may still call continue_job, and why
# that call is not a build-task entry door (so it is outside this plan's
# scope and does not need to go through run_task_stages):
ALLOWED_CALLERS = {
    # Full-job plan review: reviews the merged-but-uncommitted plan.json/
    # facts.md/scenes BEFORE it is committed. Runs once per job, before any
    # plan.tasks entry exists yet -- not a build task's attempt.
    "application/pipeline_planning.py",
    # Quick mode (`gb2 change --small`)'s own plan review -- the same
    # review-before-commit reasoning as pipeline_planning.py, just for the
    # small-job path (quick_pipeline.falls back to the full orbit, which
    # DOES go through run_task_stages, for anything past this point).
    "application/quick_pipeline.py",
    # Early-station (facts/plan/design) run_station+merge and its own
    # close -- engine.application.pipeline_stages.run_and_merge/
    # close_attempt. These run BEFORE any plan.tasks entry exists (no
    # task_id, no `owns` to hand off against) and have no owner-stop
    # recording of their own (record_early_station_stop is a separate,
    # already-single function for that door) -- confirmed by reading the
    # two call sites: neither builds a request_filter or records a task
    # stop, so there is nothing of run_task_stages' own two jobs to share.
    "application/pipeline_stages.py",
    # `gb2 run`: a single bare task with NO plan.tasks/plan.json at all
    # (cmd_run's own docstring: "Neither path calls orbit.run_job"). There
    # is no later task to hand a request off to and no task position to
    # move to STAGE_TASK_OWNER (a single-task job has no such stage) --
    # the plan's own allow list names this door explicitly.
    "interface/cli.py",
}


def _relative_engine_path(path: Path) -> str:
    return str(path.relative_to(ENGINE_ROOT)).replace("\\", "/")


def _is_continue_job_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == "continue_job"
    if isinstance(func, ast.Name):
        return func.id == "continue_job"
    return False


def test_build_task_stages_run_through_one_door() -> None:
    offenders: list[str] = []
    for path in sorted(ENGINE_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = _relative_engine_path(path)
        if path.name == RUNNER_MODULE:
            continue  # the one door itself -- its own call is the point
        if rel in ALLOWED_CALLERS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if _is_continue_job_call(node):
                offenders.append(f"{rel}:{getattr(node, 'lineno', '?')}")
    assert offenders == [], (
        "orbit.continue_job called outside task_stage_runner.run_task_stages "
        f"and the allow list: {offenders}"
    )


def test_allowed_callers_still_exist_and_are_not_the_runner() -> None:
    """A stale allow-list entry (a file renamed/removed/merged away) would
    silently widen the test's blind spot -- fail loudly instead."""
    for rel in ALLOWED_CALLERS:
        path = ENGINE_ROOT / rel
        assert path.is_file(), f"allow-listed caller no longer exists: {rel}"
        assert path.name != RUNNER_MODULE


def test_run_task_stages_is_the_single_definition() -> None:
    """run_task_stages itself is defined exactly once, in task_stage_runner.py
    -- a second definition elsewhere would defeat this whole test by giving
    callers a second "door" with the same name."""
    hits: list[str] = []
    for path in sorted(ENGINE_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "run_task_stages":
                hits.append(_relative_engine_path(path))
    assert hits == ["application/" + RUNNER_MODULE]
