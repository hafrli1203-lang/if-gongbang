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


def test_scene_mutation_runs_against_a_snapshot_not_the_real_repo(
    workspace: Workspace,
) -> None:
    """Scenes/the full suite run against a disposable snapshot of the
    tree (engine.acceptance's module docstring), never the real repo --
    a scene writing a new file lands in the snapshot only. This is the
    fix for the old loophole: a scene used to run IN the real repo, so
    a before/after fingerprint comparison around it could be fooled by
    a scene that mutated a file, ran the test, then restored it before
    the "after" read -- the restore made that diff look clean. Fixing
    the tree BEFORE any scene runs and never letting a scene touch the
    real repo at all closes that: there is nothing left for a scene to
    usefully "restore" to fool."""
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
    assert acc.tree_changed is False
    assert acc.passed is True
    # The scene's write never reached the real repo.
    assert not (ws.alpha / "untracked_by_scene.txt").exists()

    data = json.loads(acceptance_path(ws.config, "alpha", "job1").read_text(encoding="utf-8"))
    assert data["tree_changed"] is False
    assert data["passed"] is True
    assert data["fingerprint"] == acc.fingerprint
    assert len(data["fingerprint"]) == 40

    events = [e for _seq, e in ws.ledger.events("alpha", "acceptance.recorded")]
    assert events[0].payload["tree_changed"] is False
    assert events[0].payload["fingerprint"] == acc.fingerprint
    assert events[0].payload["reason"] is None


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
    assert len(acc.fingerprint) == 40


def test_run_probe_scripts_runs_existing_script_and_records_evidence(
    workspace: Workspace,
) -> None:
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    probe_dir = ws.alpha / ".gongbang" / "job1" / "probe"
    probe_dir.mkdir(parents=True)
    # "t1" -- 1-based position in the targets list (engine.gates.
    # probe_target_id), never derived from the target's own name.
    (probe_dir / "t1.py").write_text(
        "print('PROBE OK')\n", encoding="utf-8",
    )

    results = run_probe_scripts(
        ws.config, ws.ledger, "alpha", attempt_id, "job1",
        [("stripe", "https://docs.stripe.com/api")],
    )

    assert len(results) == 1
    assert results[0].id == "t1"
    assert results[0].name == "stripe"
    assert results[0].ok is True
    assert results[0].returncode == 0
    assert "PROBE OK" in results[0].stdout_tail

    events = [e for _seq, e in ws.ledger.events("alpha", "probe.executed")]
    assert len(events) == 1
    assert events[0].payload["id"] == "t1"
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
    assert results[0].id == "t1"
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
    (probe_dir / "t1.sh").write_text("exit 1\n", encoding="utf-8")

    results = run_probe_scripts(
        ws.config, ws.ledger, "alpha", attempt_id, "job1",
        [("slack", "https://api.slack.com/web")],
    )

    assert results[0].ok is False
    assert results[0].returncode == 1


def test_run_probe_scripts_colliding_names_get_distinct_ids_and_scripts(
    workspace: Workspace,
) -> None:
    """Regression: "Naver API"/"Naver_API" (and any Korean-only name)
    used to collapse to the same safe-id under the old name-derived
    scheme, so one target's script could silently stand in for the
    other. Position-based ids (t1, t2) never collide."""
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    probe_dir = ws.alpha / ".gongbang" / "job1" / "probe"
    probe_dir.mkdir(parents=True)
    (probe_dir / "t1.py").write_text("print('PROBE OK')\n", encoding="utf-8")
    # t2 has no script at all -- must be reported missing, not silently
    # covered by t1's passing script.

    results = run_probe_scripts(
        ws.config, ws.ledger, "alpha", attempt_id, "job1",
        [("Naver API", "https://a"), ("Naver_API", "https://b")],
    )

    assert [r.id for r in results] == ["t1", "t2"]
    assert results[0].ok is True
    assert results[1].ok is False
    assert results[1].stdout_tail == "script_missing"


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
