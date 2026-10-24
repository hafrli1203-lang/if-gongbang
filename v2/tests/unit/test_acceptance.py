"""Unit tests for engine.acceptance: scene cmds + the project's full
test_cmd, run inside the bench sandbox, written to acceptance.json and
recorded on the ledger. Uses the sample greeter app (variant 1) so the
full suite is a real pytest run, not a stub."""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

from engine.acceptance import acceptance_path, run_acceptance, run_probe_scripts
from engine.steps.open_job import open_job
from tests.conftest import Workspace
from tests.fixtures.sample_app import make_sample
from tests.plan_stub import Plan, Scene


def _sample_workspace(workspace: Workspace) -> Workspace:
    sample = make_sample(workspace.root / "sample", 1, Path(sys.executable))
    project_config = replace(
        workspace.config.projects["alpha"],
        repo=sample.repo, test_cmd=sample.test_cmd,
    )
    new_config = replace(
        workspace.config,
        projects={**workspace.config.projects, "alpha": project_config},
    )
    return Workspace(workspace.root, new_config, workspace.ledger,
                     sample.repo, workspace.beta)


def _open(workspace: Workspace, job_name: str = "job1") -> str:
    return open_job(workspace.config, workspace.ledger, "alpha", job_name,
                    "full", roles=("builder",))


def test_all_scenes_and_full_suite_pass(workspace: Workspace) -> None:
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    plan = Plan(
        goal="greet works", ui=False,
        scenes=(
            Scene(id="s1", actor_does="greets", app_shows="Hello, World",
                 where="cli", cmd=(sys.executable, "-c",
                                    "import sys; sys.path.insert(0, 'src'); "
                                    "from greeter import greet; "
                                    "assert greet('World') == 'Hello, World'")),
        ),
        tasks=(), acceptance=("greet returns Hello, X",),
    )

    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, plan)

    assert acc.passed is True
    assert acc.full_suite_passed is True
    assert len(acc.scenes) == 1
    assert acc.scenes[0].passed is True

    path = acceptance_path(ws.config, "alpha", "job1")
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["job"] == "job1"
    assert data["passed"] is True
    assert data["scenes"][0]["id"] == "s1"

    events = [e for _seq, e in ws.ledger.events("alpha", "acceptance.recorded")]
    assert len(events) == 1
    assert events[0].payload["job"] == "job1"
    assert events[0].payload["passed"] is True
    assert events[0].payload["scenes"] == [{"id": "s1", "passed": True}]


def test_a_failing_scene_fails_acceptance_even_though_full_suite_passes(
    workspace: Workspace,
) -> None:
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    plan = Plan(
        goal="greet works", ui=False,
        scenes=(
            Scene(id="s1", actor_does="greets", app_shows="Hello, World",
                 where="cli", cmd=(sys.executable, "-c", "raise SystemExit(1)")),
        ),
        tasks=(), acceptance=("greet returns Hello, X",),
    )

    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, plan)

    assert acc.passed is False
    assert acc.full_suite_passed is True
    assert acc.scenes[0].passed is False
    assert acc.scenes[0].returncode == 1

    data = json.loads(acceptance_path(ws.config, "alpha", "job1").read_text(encoding="utf-8"))
    assert data["passed"] is False


def test_scene_cmd_that_fails_to_start_counts_as_failed(workspace: Workspace) -> None:
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    plan = Plan(
        goal="x", ui=False,
        scenes=(Scene(id="s1", actor_does="x", app_shows="x", where="cli",
                     cmd=("/no/such/binary-anywhere",)),),
        tasks=(), acceptance=("x",),
    )

    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, plan)

    assert acc.scenes[0].passed is False
    assert acc.scenes[0].returncode == -1
    assert acc.passed is False


def test_zero_scenes_fails_without_running_the_full_suite(workspace: Workspace) -> None:
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    plan = Plan(goal="x", ui=False, scenes=(), tasks=(), acceptance=())

    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, plan)

    assert acc.passed is False
    assert acc.scenes == ()
    assert acc.full_suite_passed is False

    events = [e for _seq, e in ws.ledger.events("alpha", "acceptance.recorded")]
    assert events[0].payload["reason"] == "no_scenes"


def test_failing_full_suite_fails_acceptance_even_with_a_passing_scene(
    workspace: Workspace,
) -> None:
    ws = _sample_workspace(workspace)
    # Break the suite: replace greet() so the real test_greet fails.
    (ws.alpha / "src" / "greeter" / "__init__.py").write_text(
        'def greet(name: str) -> str:\n    return "WRONG"\n', encoding="utf-8")
    attempt_id = _open(ws)
    plan = Plan(
        goal="x", ui=False,
        scenes=(Scene(id="s1", actor_does="x", app_shows="x", where="cli",
                     cmd=(sys.executable, "-c", "pass")),),
        tasks=(), acceptance=("x",),
    )

    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, plan)

    assert acc.scenes[0].passed is True
    assert acc.full_suite_passed is False
    assert acc.passed is False


def test_scene_that_changes_the_tree_fails_acceptance(workspace: Workspace) -> None:
    """A scene that writes a new file into the repo while it runs must
    be caught by the before/after fingerprint comparison -- it is never
    enough for the scene itself to exit 0 and the full suite to pass."""
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    plan = Plan(
        goal="x", ui=False,
        scenes=(Scene(id="s1", actor_does="x", app_shows="x", where="cli",
                     cmd=(sys.executable, "-c",
                          "open('untracked_by_scene.txt', 'w').write('x')")),),
        tasks=(), acceptance=("x",),
    )

    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, plan)

    assert acc.scenes[0].passed is True
    assert acc.full_suite_passed is True
    assert acc.tree_changed is True
    assert acc.passed is False

    data = json.loads(acceptance_path(ws.config, "alpha", "job1").read_text(encoding="utf-8"))
    assert data["tree_changed"] is True
    assert data["passed"] is False

    events = [e for _seq, e in ws.ledger.events("alpha", "acceptance.recorded")]
    assert events[0].payload["tree_changed"] is True
    assert events[0].payload["reason"] == "tree_changed"


def test_unchanged_tree_keeps_tree_changed_false(workspace: Workspace) -> None:
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    plan = Plan(
        goal="x", ui=False,
        scenes=(Scene(id="s1", actor_does="x", app_shows="x", where="cli",
                     cmd=(sys.executable, "-c", "pass")),),
        tasks=(), acceptance=("x",),
    )

    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, plan)

    assert acc.tree_changed is False
    assert acc.passed is True


def test_run_probe_scripts_runs_existing_script_and_records_evidence(
    workspace: Workspace,
) -> None:
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    probe_dir = ws.alpha / ".gongbang" / "job1" / "probe"
    probe_dir.mkdir(parents=True)
    (probe_dir / "stripe.py").write_text(
        "print('PROBE OK')\n", encoding="utf-8",
    )

    results = run_probe_scripts(
        ws.config, ws.ledger, "alpha", attempt_id, "job1",
        [("stripe", "https://docs.stripe.com/api")],
    )

    assert len(results) == 1
    assert results[0].name == "stripe"
    assert results[0].ok is True
    assert results[0].returncode == 0
    assert "PROBE OK" in results[0].stdout_tail

    events = [e for _seq, e in ws.ledger.events("alpha", "probe.executed")]
    assert len(events) == 1
    assert events[0].payload["name"] == "stripe"
    assert events[0].payload["returncode"] == 0


def test_run_probe_scripts_missing_script_is_not_ok(workspace: Workspace) -> None:
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)

    results = run_probe_scripts(
        ws.config, ws.ledger, "alpha", attempt_id, "job1",
        [("slack", "https://api.slack.com/web")],
    )

    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].returncode == -1
    assert results[0].stdout_tail == "script_missing"

    events = [e for _seq, e in ws.ledger.events("alpha", "probe.executed")]
    assert events[0].payload["script"] == ""


def test_run_probe_scripts_failing_script_is_not_ok(workspace: Workspace) -> None:
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    probe_dir = ws.alpha / ".gongbang" / "job1" / "probe"
    probe_dir.mkdir(parents=True)
    (probe_dir / "slack.sh").write_text("exit 1\n", encoding="utf-8")

    results = run_probe_scripts(
        ws.config, ws.ledger, "alpha", attempt_id, "job1",
        [("slack", "https://api.slack.com/web")],
    )

    assert results[0].ok is False
    assert results[0].returncode == 1


def test_no_test_cmd_configured_fails_closed(workspace: Workspace) -> None:
    attempt_id = _open(workspace)
    plan = Plan(
        goal="x", ui=False,
        scenes=(Scene(id="s1", actor_does="x", app_shows="x", where="cli",
                     cmd=(sys.executable, "-c", "pass")),),
        tasks=(), acceptance=("x",),
    )

    acc = run_acceptance(workspace.config, workspace.ledger, "alpha", attempt_id, plan)

    assert acc.full_suite_passed is False
    assert acc.passed is False
