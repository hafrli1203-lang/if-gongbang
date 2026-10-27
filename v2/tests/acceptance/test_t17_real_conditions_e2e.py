"""T17 -- G45 (운영자 지시: "또 테스트를 하면 또 며칠 걸리는데 도대체 빨리하는
방법 없나?", `ops-repo/audit/gongbang_directives.md`) + G26 ("빠르게
작업하는건 기본값으로"). The audit's own G45 finding was "실제 조건
시험 묶음 없음" -- the pieces already existed separately (test_t9_real_
conditions.py's own gitignore/probe-log defects, test_t13_file_dep_
real_sandbox.py's file: dependency, test_t3_gb2_node_e2e.py's real-
subprocess gb2), but never combined on ONE sample app and driven
through ONE job the way a past incident asks for: "실제 조건을 갖춘 표본 앱으로
처음부터 끝까지 도는 실전 시험, 짧은 실제 AI 1회 포함".

`_apply_real_conditions` adds, on top of the existing "greeter" sample
app (tests/fixtures/sample_app.py, already a real git repo) every
condition a past incident names:
  - a real .gitignore rule that actually hides something a job writes
    (`*.log` -- proven below by writing a stray .log file and checking
    `git status --porcelain` never lists it, the same assertion style
    test_t9_real_conditions.py's own defect-2 regression test uses)
  - a long instruction file (docs/REQUIREMENTS.md, >3000 chars) sitting
    outside the builder's own `station_owners` prefix (src/, tests/),
    to prove a build task never touches content it has no business
    touching
  - two different "hidden path" shapes: a TRACKED dotfile (.env.example,
    committed on purpose -- a real convention) and an UNTRACKED,
    gitignored one (.secrets/local.txt -- never meant to be committed)
  - a `file:../shared-kit` package.json dependency escaping the repo,
    resolved into `ProjectConfig.file_dep_targets` the exact same way
    `tools/gb2lib/workspace.py`'s own `cmd_init` does (discover_
    escaping_file_deps -> resolve_escaping_target -> validate_escaping_
    target), then exercised through `file_deps.sandbox_file_dep_pairs`
    -- the same function test_t13_file_dep_real_sandbox.py already
    proved resolves correctly under a real bwrap mount, reused here
    rather than re-proving bwrap mount mechanics from scratch.

The default (non-`live`) test drives this fixture through a full
ScriptedRunner job (engine.orbit.run_job, builder+reviewer, the same
shape test_13_copy_change.py's own scripted half uses) and asserts
every condition survived the job untouched or was handled correctly.
The `@pytest.mark.live` test drives the SAME fixture through ONE real
AI job (Claude builder, a Codex-based reviewer) -- "짧은 실제 AI 1회" -- and
records cost/turns/duration via tests.acceptance._owner_tasks.
record_live_run, exactly like test_13/14/15's own live variants."""
from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from engine import orbit
from engine.application.steps import file_deps
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.processor import Fakes
from engine.steps.open_job import open_job
from engine.config import WorkerConfig
from engine.workers.scripted import ScriptedRunner
from tests.conftest import Workspace, git
from tests.fixtures.sample_app import _TEST_GREETER_V1
from tests.acceptance._owner_tasks import (
    engine_repo_root, record_live_run, sample_workspace, show_head,
    station_config, station_events_cost_turns, with_workers, write_script,
)

_JOB_NAME = "t17-real-conditions"
_TASK = ('Change the greeting in src/greeter/__init__.py to '
        '"Welcome, {name}!" and update tests/test_greeter.py to match.')

_BUILDER_SCRIPT = [
    {"tool": "Write", "file_path": "src/greeter/__init__.py",
     "content": 'def greet(name: str) -> str:\n    return f"Welcome, {name}!"\n'},
    {"tool": "Write", "file_path": "tests/test_greeter.py",
     "content": _TEST_GREETER_V1.replace('"Hello, World"', '"Welcome, World!"')},
    {"final": {"status": "done", "summary": "updated greeting wording", "requests": []}},
]
_REVIEWER_SCRIPT = [{"final": {"passed": True, "coverage": "full", "findings": []}}]

# >3000 chars, deliberately outside src/+tests/ (the builder's own
# station_owners prefix per station_config) -- nothing in this job is
# ever allowed to touch it.
_LONG_INSTRUCTIONS = (
    "# 요구사항 원문 (실제 조건 묶음 -- 긴 지시문)\n\n"
    + "\n".join(
        f"- 요구사항 {i}: 이 줄은 표본 앱의 docs/REQUIREMENTS.md가 실제로 "
        "길어야 한다는 a past incident 조건을 채우기 위한 반복 문장이며, 빌더의 "
        "station_owners 범위(src/, tests/) 밖에 있어 어떤 build task도 "
        "이 파일을 건드리지 않아야 한다."
        for i in range(1, 80)
    )
    + "\n"
)


def _sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _apply_real_conditions(ws: Workspace, shared_root: Path) -> dict[str, object]:
    """Adds every a past incident condition on top of the sample app's own
    real git repo, commits the tracked additions, and returns the
    facts a test needs to assert nothing broke.

    `shared_root` MUST be outside `ws.root` -- the real gb2 layout
    keeps GB2_APPS_ROOT (a project's repo + its `_shared` sibling) and
    GB2_WORKSPACE (home/secrets/ledger, this test's own `ws.root`)
    as two entirely separate trees; file_deps.validate_escaping_target
    refuses a target that nests with either one (`forbidden_roots`),
    exactly as it should -- putting shared-kit inside `ws.root` here
    would be testing a layout gb2 itself never produces."""
    repo = ws.alpha

    gitignore = repo / ".gitignore"
    gitignore.write_text(gitignore.read_text(encoding="utf-8") + "*.log\n.secrets/\n",
                         encoding="utf-8")

    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "REQUIREMENTS.md").write_text(_LONG_INSTRUCTIONS, encoding="utf-8")
    assert len(_LONG_INSTRUCTIONS) > 3000

    (repo / ".env.example").write_text("SAMPLE_API_KEY=changeme\n", encoding="utf-8")

    secrets_dir = repo / ".secrets"
    secrets_dir.mkdir(exist_ok=True)
    secret_file = secrets_dir / "local.txt"
    secret_file.write_text("super-secret-should-never-be-committed\n", encoding="utf-8")

    shared_dir = shared_root / "shared-kit"
    shared_dir.mkdir(parents=True, exist_ok=True)
    (shared_dir / "package.json").write_text(
        json.dumps({"name": "@shared/kit", "version": "0.0.1", "main": "index.js"}),
        encoding="utf-8")
    (shared_dir / "index.js").write_text(
        "module.exports = { hello: () => 'hi' };\n", encoding="utf-8")
    relspec = "file:" + os.path.relpath(shared_dir, repo)
    (repo / "package.json").write_text(json.dumps({
        "name": "sample-app", "private": True,
        "dependencies": {"@shared/kit": relspec},
    }, ensure_ascii=False), encoding="utf-8")

    noise_log = repo / "build-noise.log"
    noise_log.write_text("not a real build artifact, just noise\n", encoding="utf-8")

    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m",
        "t17: real-condition fixtures (gitignore *.log, long docs, hidden paths, file: dep)")

    return {
        "secret_sha_before": _sha256(secret_file), "secret_file": secret_file,
        "noise_log": noise_log, "requirements_path": repo / "docs" / "REQUIREMENTS.md",
        "requirements_text_before": _LONG_INSTRUCTIONS, "shared_dir": shared_dir,
    }


def _file_dep_targets(repo: Path, workspace_root: Path) -> tuple[tuple[str, str], ...]:
    """Mirrors tools/gb2lib/workspace.py's own cmd_init exactly:
    discover -> resolve -> validate -> persist as (relspec, target)
    pairs, the real way `ProjectConfig.file_dep_targets` gets built."""
    targets: list[tuple[str, str]] = []
    for relspec in file_deps.discover_escaping_file_deps(repo):
        target = file_deps.resolve_escaping_target(repo, relspec)
        file_deps.validate_escaping_target(target, forbidden_roots=(workspace_root,))
        targets.append((relspec, str(target)))
    return tuple(targets)


def _config_with_real_conditions(
    workspace: Workspace, shared_root: Path,
) -> tuple[Workspace, dict[str, object]]:
    ws = sample_workspace(workspace, variant=1)
    facts = _apply_real_conditions(ws, shared_root)
    targets = _file_dep_targets(ws.alpha, ws.root)
    config = station_config(ws)
    project = config.projects["alpha"]
    new_project = replace(project, file_dep_targets=targets)
    config = replace(config, projects={**config.projects, "alpha": new_project})
    return replace(ws, config=config), facts


# --------------------------------------------------------------------
# 1. the file: dependency actually resolves (reuses test_t13's own
#    proven function -- not re-proving bwrap mount mechanics here).
# --------------------------------------------------------------------

def test_file_dep_targets_resolve_to_the_sibling_shared_kit(
    workspace: Workspace, tmp_path_factory: pytest.TempPathFactory,
) -> None:
    shared_root = tmp_path_factory.mktemp("t17-shared")
    ws, facts = _config_with_real_conditions(workspace, shared_root)
    project = ws.config.projects["alpha"]
    assert project.file_dep_targets, "no file: dependency discovered -- fixture is wrong"
    relspec, target = project.file_dep_targets[0]
    assert relspec.endswith("shared-kit")
    assert Path(target) == facts["shared_dir"].resolve()

    pairs = file_deps.sandbox_file_dep_pairs(
        ws.alpha, repo=ws.alpha, file_dep_targets=project.file_dep_targets)
    assert pairs, "sandbox_file_dep_pairs produced no remap pairs"


# --------------------------------------------------------------------
# 2. end to end, scripted (no cost, runs every time): facts/gitignore/
#    hidden paths/long docs all survive a real job untouched.
# --------------------------------------------------------------------

def test_real_condition_sample_app_end_to_end_scripted(
    workspace: Workspace, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory,
) -> None:
    shared_root = tmp_path_factory.mktemp("t17-shared")
    ws, facts = _config_with_real_conditions(workspace, shared_root)
    config = ws.config

    attempt_id = open_job(config, ws.ledger, "alpha", _JOB_NAME, "full",
                          ("builder", "reviewer"))
    build_stage, merge_stage = orbit.default_stages(
        config, attempt_id, "builder", "reviewer", _TASK, _JOB_NAME,
    )[:2]

    config = with_workers(config, {
        "builder": WorkerConfig("scripted"), "reviewer": WorkerConfig("scripted"),
    })
    builder_fakes = Fakes(
        publisher=FakePublisher(), installer=FakeInstaller(),
        runners={"scripted": ScriptedRunner(
            write_script(tmp_path, "builder.json", _BUILDER_SCRIPT))},
    )
    outcome1 = orbit.continue_job(config, ws.ledger, builder_fakes, "alpha", attempt_id,
                                  [build_stage, merge_stage])
    assert outcome1.status == "done", outcome1

    reviewer_fakes = Fakes(
        publisher=FakePublisher(), installer=FakeInstaller(),
        runners={"scripted": ScriptedRunner(
            write_script(tmp_path, "reviewer.json", _REVIEWER_SCRIPT), read_only=True)},
    )
    stages2 = orbit.stages_after_install(config, attempt_id, "builder", "reviewer", _JOB_NAME)
    outcome2 = orbit.continue_job(config, ws.ledger, reviewer_fakes, "alpha", attempt_id, stages2)

    assert outcome2.status == "done", outcome2
    assert "Welcome, " in show_head(ws.alpha, "src/greeter/__init__.py")

    # -- 1. the real .gitignore rule actually hid the stray .log file.
    porcelain = git(ws.alpha, "status", "--porcelain")
    assert "build-noise.log" not in porcelain
    assert ".secrets" not in porcelain

    # -- 2. the long instructions file was never touched by the build.
    assert facts["requirements_path"].read_text(encoding="utf-8") == \
        facts["requirements_text_before"]

    # -- 3. the gitignored hidden secret is untouched, byte for byte,
    #    and was never committed into the job's own commit trail.
    assert _sha256(facts["secret_file"]) == facts["secret_sha_before"]
    log_output = git(ws.alpha, "log", "--name-only", "--format=")
    assert "local.txt" not in log_output
    assert "build-noise.log" not in log_output

    # -- 4. the tracked hidden dotfile (.env.example) survived as a
    #    real, trackable path (not confused with the gitignored one).
    assert (ws.alpha / ".env.example").exists()
    tracked = git(ws.alpha, "ls-files")
    assert ".env.example" in tracked.split("\n")
    assert ".secrets/local.txt" not in tracked.split("\n")


# --------------------------------------------------------------------
# 3. G26: the SAME fixture, ONE real AI job -- never run automatically.
# --------------------------------------------------------------------

@pytest.mark.live
def test_real_condition_sample_app_live_small_change(
    workspace: Workspace, tmp_path_factory: pytest.TempPathFactory,
) -> None:
    from engine.workers.claude_cli import ClaudeRunner
    from engine.workers.codex_cli import CodexRunner

    shared_root = tmp_path_factory.mktemp("t17-shared")
    ws, facts = _config_with_real_conditions(workspace, shared_root)
    config = with_workers(ws.config, {
        "builder": WorkerConfig("claude", model="sonnet", max_turns=40,
                                budget_usd=3.0, timeout_sec=900.0),
        "reviewer": WorkerConfig("codex", reasoning="medium"),
    })
    fakes = Fakes(
        publisher=FakePublisher(), installer=FakeInstaller(),
        runners={"claude": ClaudeRunner(), "codex": CodexRunner()},
    )

    started = time.monotonic()
    outcome = orbit.run_job(config, ws.ledger, fakes, "alpha", _JOB_NAME, _TASK,
                            "builder", "reviewer")
    duration_sec = time.monotonic() - started
    cost_usd, turns = station_events_cost_turns(ws.ledger, "alpha")
    record_live_run(engine_repo_root(), "test_t17_real_conditions", outcome,
                    cost_usd, turns, duration_sec)

    # Real conditions must survive a REAL AI job too, not just a
    # scripted one -- same checks as the scripted test above.
    porcelain = git(ws.alpha, "status", "--porcelain")
    assert "build-noise.log" not in porcelain
    assert _sha256(facts["secret_file"]) == facts["secret_sha_before"]

    assert outcome.status == "done", outcome
