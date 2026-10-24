"""Acceptance 14 — owner task #2 (reproducible bug), end to end: a
standalone bench request on the sample "greeter" app (plan Task 5 §8,
variant 2) first reproduces the known off-by-one bug (failed,
case_failed, with the case's own assertion signature recorded as
evidence) on the SAME still-open attempt; the builder then fixes it and
the job runs through to close.

See tests/acceptance/test_13_copy_change.py's module docstring for why
every Task 2/3 reference below is inside a function body, and why the
scripted test is marked xfail(strict=False) until that merge lands."""
from __future__ import annotations

import time

import pytest

from engine import orbit
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.processor import Fakes
from engine.steps.open_job import open_job
from tests.acceptance._owner_tasks import (
    engine_repo_root, record_live_run, sample_workspace, show_head,
    station_config, station_events_cost_turns, with_workers, write_script,
)
from tests.conftest import Workspace

_JOB_NAME = "fix-initials"
_TASK = ('tests/test_greeter.py::test_initials expects initials() to '
        "return every word's initial, not just the first word's -- fix "
        "the off-by-one in src/greeter/__init__.py.")

_FIXED_INIT_PY = '''\
def greet(name: str) -> str:
    return f"Hello, {name}"


def initials(full_name: str) -> str:
    words = full_name.split()
    return "".join(word[0] for word in words).upper()
'''
_FIX_BUILDER_SCRIPT = [
    {"tool": "Write", "file_path": "src/greeter/__init__.py", "content": _FIXED_INIT_PY},
    {"final": {"status": "done", "summary": "fixed off-by-one initials bug", "requests": []}},
]
_REVIEWER_SCRIPT = [{"final": {"passed": True, "coverage": "full", "findings": []}}]


def test_repro_bug_end_to_end_scripted(workspace: Workspace, tmp_path) -> None:
    from engine.config import WorkerConfig
    from engine.workers.scripted import ScriptedRunner

    ws = sample_workspace(workspace, variant=2)
    base_config = station_config(ws)
    attempt_id = open_job(base_config, ws.ledger, "alpha", _JOB_NAME, "full",
                          ("builder", "reviewer"))

    # Reproduce: a standalone bench request on the untouched attempt must
    # fail with the case's own signature, not pass silently.
    outcome0 = orbit.continue_job(
        base_config, ws.ledger, Fakes(publisher=FakePublisher(), installer=FakeInstaller()),
        "alpha", attempt_id, [orbit.Stage("bench", {})],
    )
    assert outcome0.status == "failed"
    assert outcome0.reason == "case_failed"
    bench_rows = ws.ledger.evidence(attempt_id, "bench")
    assert bench_rows[-1].payload["passed"] is False
    assert bench_rows[-1].payload["per_case"]["initials_bug"] is False

    # Fix: the SAME attempt (a failed request never closes or advances
    # it) now runs the builder -> merge -> review -> bench -> commit ->
    # report -> close sequence to a real fix.
    config = with_workers(base_config, {
        "builder": WorkerConfig("scripted"), "reviewer": WorkerConfig("scripted"),
    })
    build_stage, merge_stage = orbit.default_stages(
        config, attempt_id, "builder", "reviewer", _TASK, _JOB_NAME,
    )[:2]
    builder_fakes = Fakes(
        publisher=FakePublisher(), installer=FakeInstaller(),
        runners={"scripted": ScriptedRunner(
            write_script(tmp_path, "builder.json", _FIX_BUILDER_SCRIPT))},
    )
    outcome1 = orbit.continue_job(config, ws.ledger, builder_fakes, "alpha", attempt_id,
                                  [build_stage, merge_stage])
    assert outcome1.status == "done"

    reviewer_fakes = Fakes(
        publisher=FakePublisher(), installer=FakeInstaller(),
        runners={"scripted": ScriptedRunner(
            write_script(tmp_path, "reviewer.json", _REVIEWER_SCRIPT), read_only=True)},
    )
    stages2 = orbit.stages_after_install(config, attempt_id, "builder", "reviewer", _JOB_NAME)
    outcome2 = orbit.continue_job(config, ws.ledger, reviewer_fakes, "alpha", attempt_id, stages2)
    assert outcome2.status == "done"

    bench_rows_after = ws.ledger.evidence(attempt_id, "bench")
    assert bench_rows_after[-1].payload["passed"] is True
    assert "words).upper" in show_head(ws.alpha, "src/greeter/__init__.py")
    assert ws.ledger.project("alpha").state == "CLOSED"


@pytest.mark.live
def test_repro_bug_live(workspace: Workspace) -> None:
    from engine.config import WorkerConfig
    from engine.workers.claude_cli import ClaudeRunner
    from engine.workers.codex_cli import CodexRunner

    ws = sample_workspace(workspace, variant=2)
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
    record_live_run(engine_repo_root(), "test_14", outcome, cost_usd, turns, duration_sec)

    assert outcome.status == "done"
