"""T9e -- 결함 27, marketing-chat job): planning round 2
failed with "task T11: owns 'package.json' is outside build prefixes
['src/', 'tests/', 'docs/ai/', 'public/', 'index.html', 'vite.config.js',
...]" (engine.domain.plan_model.validate_plan). The check itself was
correct -- a manifest change goes through a separate install/
scripts_change request with owner approval, never a build task's own
`owns` -- but neither planner_prompt nor small_prompt
(engine.application.stage_prompts) ever told the planner what `owns`
may contain or where a manifest change belongs instead, so the SAME
rejection kept recurring on every planning round.

Fixed by plan_owns_rule_sentence (engine.domain.plan_model), spliced
into both prompts via `build_prefixes`/`manifest` params their real
callers now pass through: engine.application.pipeline_planning.
run_plan_and_review (full orbit) and engine.application.quick_pipeline.
_run_from_attempt (`--small`).

This module exercises the REAL wiring end to end against the shared
`workspace` fixture (tests/conftest.py) -- the actual
engine.application.pipeline_stages.build_prefixes(config) and the
project's own ProjectConfig.manifest, not synthetic tuples -- rather
than re-testing plan_owns_rule_sentence/validate_plan in isolation
(already covered by tests/unit/test_stage_rule_sentences.py's own
결함 27 section). No live AI: this only calls the prompt-building and
plan-validating functions directly, same as that unit module.

The fixture's own STATION_OWNERS["build"]["builder"] includes
"package.json" (a permissive default for OTHER acceptance tests that
build+merge real files) -- narrowed here to ("src/", "tests/") so the
scenario actually reproduces the real app's mismatch this defect was
filed against."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from engine.application.pipeline_stages import build_prefixes
from engine.application.stage_prompts import planner_prompt, small_prompt
from engine.domain.plan_model import load_plan, plan_owns_rule_sentence, plan_path, validate_plan
from tests.conftest import Workspace

_JOB = "job1"


def _narrowed(workspace: Workspace) -> Workspace:
    """alpha's own build prefixes without "package.json" -- the real
    app shape the defect was found against -- and an explicit manifest
    path, mirroring a project that actually configured one."""
    narrow_owners = {
        **workspace.config.station_owners,
        "build": {"builder": ("src/", "tests/", "docs/")},
    }
    project = replace(workspace.config.projects["alpha"], manifest="package.json")
    config = replace(
        workspace.config, station_owners=narrow_owners,
        projects={**workspace.config.projects, "alpha": project},
    )
    return replace(workspace, config=config)


def _write_plan(repo: Path, job: str, owns: list[str]) -> None:
    scene_script = repo / "tests" / "scenes" / "s1.py"
    scene_script.parent.mkdir(parents=True, exist_ok=True)
    scene_script.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    plan = {
        "goal": "목표", "ui": False,
        "requirements": [{"id": "r1", "text": "요구 사항", "kind": "function"}],
        "scenes": [{
            "id": "s1", "actor_does": "누른다", "app_shows": "보인다", "where": "/",
            "cmd": ["python3", "tests/scenes/s1.py"], "covers": ["r1"],
        }],
        "tasks": [{
            "id": "t1", "title": "작업", "detail": "", "owns": owns,
            "depends_on": [], "covers": ["r1"],
        }],
        "acceptance": ["끝"],
    }
    path = plan_path(repo, job)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")


def test_27_planner_prompt_names_this_projects_real_prefixes_and_manifest(
    workspace: Workspace,
) -> None:
    ws = _narrowed(workspace)
    prefixes = build_prefixes(ws.config)
    manifest = ws.config.projects["alpha"].manifest
    assert prefixes == ("src/", "tests/", "docs/")
    assert manifest == "package.json"

    text = planner_prompt("요청 내용", _JOB, "facts.md 내용", build_prefixes=prefixes,
                          manifest=manifest)
    sentence = plan_owns_rule_sentence(prefixes, manifest)
    assert sentence is not None and sentence in text
    for prefix in prefixes:
        assert prefix in text


def test_27_small_prompt_names_this_projects_real_prefixes_and_manifest(
    workspace: Workspace,
) -> None:
    ws = _narrowed(workspace)
    prefixes = build_prefixes(ws.config)
    manifest = ws.config.projects["alpha"].manifest

    text = small_prompt("요청 내용", _JOB, build_prefixes=prefixes, manifest=manifest)
    sentence = plan_owns_rule_sentence(prefixes, manifest)
    assert sentence is not None and sentence in text


def test_27_plan_owning_the_manifest_directly_fails_the_same_way_the_incident_did(
    workspace: Workspace,
) -> None:
    ws = _narrowed(workspace)
    prefixes = build_prefixes(ws.config)
    _write_plan(ws.alpha, _JOB, owns=["package.json"])

    plan = load_plan(ws.alpha, _JOB)
    problems = validate_plan(plan, ws.alpha, prefixes)
    assert any(
        "owns 'package.json' is outside build prefixes" in p for p in problems
    ), problems


def test_27_plan_whose_owns_follows_the_sentence_passes_for_the_same_project(
    workspace: Workspace,
) -> None:
    ws = _narrowed(workspace)
    prefixes = build_prefixes(ws.config)
    _write_plan(ws.alpha, _JOB, owns=["src/app.py", "tests/app_test.py"])

    plan = load_plan(ws.alpha, _JOB)
    assert validate_plan(plan, ws.alpha, prefixes) == []
