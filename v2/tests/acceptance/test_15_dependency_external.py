"""Acceptance 15 — owner task #3 (dependency + external call), end to
end: the builder writes src/greeter/remote.py (needs the not-yet-
installed vendored_pkg and a status URL from the environment, plan Task
5 §8, variant 3), asks the owner to install it (requirements.txt is
protected -- the builder cannot edit it itself), the orbit stops
needs_owner once that request shows up and the builder's file change has
landed via merge, the owner submits the install, and continue_job on
stages_after_install finishes review/bench/commit/report/close.

See tests/acceptance/test_13_copy_change.py's module docstring for why
every Task 2/3 reference below is inside a function body, and why the
scripted test is marked xfail(strict=False) until that merge lands."""
from __future__ import annotations

import time

import pytest

from engine import orbit
from engine.fakes.publisher import FakePublisher
from engine.processor import Fakes, submit
from engine.steps.open_job import open_job
from tests.acceptance._owner_tasks import (
    engine_repo_root, record_live_run, sample_workspace, station_config,
    station_events_cost_turns, status_stub, status_url, with_status_url,
    with_workers, write_script,
)
from tests.conftest import Workspace

_JOB_NAME = "fetch-status"
_TASK = ("Implement fetch_status(url) in src/greeter/remote.py using "
        "urllib and vendored_pkg.parse, per tests/test_remote.py.")

_REMOTE_PY = '''\
import urllib.request

from vendored_pkg import parse


def fetch_status(url: str) -> dict:
    with urllib.request.urlopen(url) as response:
        return parse(response.read().decode("utf-8"))
'''
_INSTALL_REQUEST = {
    "kind": "install", "manifest": "requirements.txt",
    "change": {"add_lines": ["vendored_pkg==0.1.0"]},
}
_DEP_BUILDER_SCRIPT = [
    {"tool": "Write", "file_path": "src/greeter/remote.py", "content": _REMOTE_PY},
    {"final": {"status": "blocked", "summary": "needs vendored_pkg",
              "requests": [_INSTALL_REQUEST]}},
]
_REVIEWER_SCRIPT = [{"final": {"passed": True, "coverage": "full", "findings": []}}]


def test_dependency_external_end_to_end_scripted(workspace: Workspace, tmp_path) -> None:
    from engine.config import WorkerConfig
    from engine.installers import RealInstaller
    from engine.workers.scripted import ScriptedRunner

    with status_stub() as server:
        ws = sample_workspace(workspace, variant=3)
        base_config = with_status_url(station_config(ws), status_url(server))
        config = with_workers(base_config, {
            "builder": WorkerConfig("scripted"), "reviewer": WorkerConfig("scripted"),
        })
        attempt_id = open_job(config, ws.ledger, "alpha", _JOB_NAME, "full",
                              ("builder", "reviewer"))
        build_stage, merge_stage = orbit.default_stages(
            config, attempt_id, "builder", "reviewer", _TASK, _JOB_NAME,
        )[:2]
        builder_fakes = Fakes(
            publisher=FakePublisher(),
            installer=RealInstaller(
                repo=config.projects["alpha"].repo,
                install_cmd=config.projects["alpha"].install_cmd,
                marker_dir=config.home / "jobs" / attempt_id / "installs",
                timeout_sec=120.0,
            ),
            runners={"scripted": ScriptedRunner(
                write_script(tmp_path, "builder.json", _DEP_BUILDER_SCRIPT))},
        )

        outcome1 = orbit.continue_job(config, ws.ledger, builder_fakes, "alpha", attempt_id,
                                      [build_stage, merge_stage])
        assert outcome1.status == "needs_owner"
        assert outcome1.pending_requests == (_INSTALL_REQUEST,)
        assert outcome1.completed == ("run_station", "merge")
        # remote.py has landed via merge even though install is pending --
        # it is on disk (working tree), not yet committed (that is the
        # next phase's job, once bench can actually import vendored_pkg).
        assert "vendored_pkg" in (ws.alpha / "src" / "greeter" / "remote.py").read_text()

        rid = submit(ws.ledger, "alpha", attempt_id, "install",
                     dict(_INSTALL_REQUEST, manifest="requirements.txt"), "owner")
        from engine.processor import Processor
        proc = Processor(config, ws.ledger, builder_fakes)
        proc.register_defaults()
        proc.run_once("alpha")
        assert ws.ledger.request(rid).status == "done"
        assert (ws.alpha / ".deps" / "vendored_pkg" / "__init__.py").exists()

        reviewer_fakes = Fakes(
            publisher=FakePublisher(), installer=builder_fakes.installer,
            runners={"scripted": ScriptedRunner(
                write_script(tmp_path, "reviewer.json", _REVIEWER_SCRIPT), read_only=True)},
        )
        stages2 = orbit.stages_after_install(config, attempt_id, "builder", "reviewer", _JOB_NAME)
        outcome2 = orbit.continue_job(config, ws.ledger, reviewer_fakes, "alpha", attempt_id, stages2)

        assert outcome2.status == "done"
        assert outcome2.completed == ("review", "bench", "commit", "report", "close")
        assert ws.ledger.project("alpha").state == "CLOSED"
        assert server.hits >= 1


@pytest.mark.live
def test_dependency_external_live(workspace: Workspace) -> None:
    from engine.config import WorkerConfig
    from engine.installers import RealInstaller
    from engine.processor import Processor
    from engine.workers.claude_cli import ClaudeRunner
    from engine.workers.codex_cli import CodexRunner

    with status_stub() as server:
        ws = sample_workspace(workspace, variant=3)
        base_config = with_status_url(station_config(ws), status_url(server))
        config = with_workers(base_config, {
            "builder": WorkerConfig("claude", model="sonnet", max_turns=40,
                                    budget_usd=3.0, timeout_sec=900.0),
            "reviewer": WorkerConfig("codex", reasoning="medium"),
        })

        started = time.monotonic()
        attempt_id = open_job(config, ws.ledger, "alpha", _JOB_NAME, "full",
                              ("builder", "reviewer"))
        fakes = Fakes(
            publisher=FakePublisher(),
            installer=RealInstaller(
                repo=config.projects["alpha"].repo,
                install_cmd=config.projects["alpha"].install_cmd,
                marker_dir=config.home / "jobs" / attempt_id / "installs",
                timeout_sec=300.0,
            ),
            runners={"claude": ClaudeRunner(), "codex": CodexRunner()},
        )
        build_stage, merge_stage = orbit.default_stages(
            config, attempt_id, "builder", "reviewer", _TASK, _JOB_NAME,
        )[:2]
        outcome = orbit.continue_job(config, ws.ledger, fakes, "alpha", attempt_id,
                                     [build_stage, merge_stage])
        if outcome.status == "needs_owner":
            proc = Processor(config, ws.ledger, fakes)
            proc.register_defaults()
            for req in outcome.pending_requests:
                submit(ws.ledger, "alpha", attempt_id, str(req["kind"]),
                       {k: v for k, v in req.items() if k != "kind"}, "owner")
            proc.run_once("alpha")
            stages2 = orbit.stages_after_install(config, attempt_id, "builder", "reviewer",
                                                  _JOB_NAME)
            outcome = orbit.continue_job(config, ws.ledger, fakes, "alpha", attempt_id, stages2)
        duration_sec = time.monotonic() - started
        cost_usd, turns = station_events_cost_turns(ws.ledger, "alpha")
        record_live_run(engine_repo_root(), "test_15", outcome, cost_usd, turns, duration_sec)

        assert outcome.status == "done"
