"""Acceptance 13 — owner task #1 (wording fix), end to end: open a job on
the sample "greeter" app (plan Task 5 §8, variant 1), run the builder
station, merge its copy, review, bench, commit and close.

engine.config.WorkerConfig, engine.processor.Fakes.runners and
engine.workers.scripted.ScriptedRunner are Task 2/3's additions and are
not yet merged into this branch -- every reference to them is inside a
test function body (never at module scope), so collecting this module
never fails before that merge. The scripted test is marked
xfail(strict=False) for the same reason: once Task 2/3 land, the team
lead removes the mark and this test runs for real. The @pytest.mark.live
variant calls real AI CLIs (Claude as builder, Codex as reviewer) and is
never run automatically -- only explicitly, with `-m live`, after the
team lead's approval."""
from __future__ import annotations

import time

import pytest

from engine import ids, orbit
from engine.fakes.publisher import FakePublisher
from engine.steps.open_job import open_job
from tests.fixtures.sample_app import _TEST_GREETER_V1
from tests.acceptance._owner_tasks import (
    engine_repo_root, record_live_run, sample_workspace, show_head,
    station_config, station_events_cost_turns, with_workers, write_script,
)
from tests.conftest import Workspace

_JOB_NAME = "wording-fix"
_TASK = ('Change the greeting in src/greeter/__init__.py to '
        '"Welcome, {name}!" and update tests/test_greeter.py to match.')

_BUILDER_SCRIPT = [
    {"tool": "Write", "file_path": "src/greeter/__init__.py",
     "content": 'def greet(name: str) -> str:\n    return f"Welcome, {name}!"\n'},
    {"tool": "Write", "file_path": "tests/test_greeter.py",
     # the sample's own test file with only the expected wording changed --
     # the env-gated negative control stays exactly as the fixture wrote it
     "content": _TEST_GREETER_V1.replace('"Hello, World"', '"Welcome, World!"')},
    {"final": {"status": "done", "summary": "updated greeting wording", "requests": []}},
]
_REVIEWER_SCRIPT = [{"final": {"passed": True, "coverage": "full", "findings": []}}]


def test_copy_change_end_to_end_scripted(workspace: Workspace, tmp_path) -> None:
    from engine.config import WorkerConfig
    from engine.fakes.installer import FakeInstaller
    from engine.processor import Fakes
    from engine.workers.scripted import ScriptedRunner

    ws = sample_workspace(workspace, variant=1)
    config = station_config(ws)
    attempt_id = open_job(config, ws.ledger, "alpha", _JOB_NAME, "full",
                          ("builder", "reviewer"))
    build_stage, merge_stage = orbit.default_stages(
        config, attempt_id, "builder", "reviewer", _TASK, _JOB_NAME,
    )[:2]

    # Both builder and reviewer run through the same "scripted" runner
    # type (WorkerConfig.runner is restricted to claude|codex|scripted),
    # so each phase gets its own Fakes with a differently-scripted
    # ScriptedRunner under that one key -- a real ScriptedRunner instance
    # is bound to a single, fixed script.
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
    assert outcome1.status == "done"
    assert outcome1.completed == ("run_station", "merge")

    reviewer_fakes = Fakes(
        publisher=FakePublisher(), installer=FakeInstaller(),
        runners={"scripted": ScriptedRunner(
            write_script(tmp_path, "reviewer.json", _REVIEWER_SCRIPT), read_only=True)},
    )
    stages2 = orbit.stages_after_install(config, attempt_id, "builder", "reviewer", _JOB_NAME)
    outcome2 = orbit.continue_job(config, ws.ledger, reviewer_fakes, "alpha", attempt_id, stages2)

    assert outcome2.status == "done"
    assert outcome2.completed == ("review", "bench", "commit", "report", "close")

    assert "Welcome, " in show_head(ws.alpha, "src/greeter/__init__.py")
    assert "Welcome, World!" in show_head(ws.alpha, "tests/test_greeter.py")
    bench_rows = ws.ledger.evidence(attempt_id, "bench")
    assert bench_rows[-1].payload["passed"] is True
    assert ws.ledger.project("alpha").state == "CLOSED"
    archive_root = config.home / "archive" / "alpha" / ids.attempt_dirname(attempt_id)
    assert archive_root.exists() and any(archive_root.iterdir())


@pytest.mark.live
def test_copy_change_live(workspace: Workspace) -> None:
    from engine.config import WorkerConfig
    from engine.fakes.installer import FakeInstaller
    from engine.processor import Fakes
    from engine.workers.claude_cli import ClaudeRunner
    from engine.workers.codex_cli import CodexRunner

    ws = sample_workspace(workspace, variant=1)
    config = with_workers(station_config(ws), {
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
    record_live_run(engine_repo_root(), "test_13", outcome, cost_usd, turns, duration_sec)

    assert outcome.status == "done"
