"""Unit tests for engine.acceptance: scene cmds + the project's full
test_cmd, run inside the bench sandbox, written to acceptance.json and
recorded on the ledger. Uses the sample greeter app (variant 1) so the
full suite is a real pytest run, not a stub."""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from engine import sandbox
from engine.acceptance import acceptance_path, run_acceptance, run_probe_scripts
from engine.steps.open_job import open_job
from tests.conftest import Workspace
from tests.fixtures.sample_app import make_sample
from tests.plan_stub import Plan, Scene

_needs_bwrap = pytest.mark.skipif(
    not sandbox.available(), reason="bwrap not available on this host",
)


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


def test_scene_patching_a_tracked_file_fails_the_scene_outright(
    workspace: Workspace,
) -> None:
    """A scene is free to create a new, untracked scratch file (previous
    test), but a scene that tries to open-for-write a file the
    snapshot's own HEAD already tracks must never get far enough to
    actually change it: _lock_tracked_files (engine.acceptance) strips
    every tracked file's write bit before any scene runs, so the
    `open(..., 'w')` itself raises PermissionError and the scene fails
    right there -- not "fails later once _tracked_mutated_paths
    notices a diff" (the old, now-closed shape of this bug: a scene
    could patch, observe its own result, then restore the bytes before
    anything re-checked `git status`, leaving no diff at all to catch).
    A plain failed scene does not by itself stop the next scene or the
    full suite from running (same as any other scene assertion
    failure) -- only an actual tracked mutation
    (_tracked_mutated_paths) does that; s2 here is an unrelated no-op
    and both it and the full suite still run and pass, but the job as
    a whole is still not accepted because s1 failed."""
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    plan = Plan(
        goal="x", ui=False,
        scenes=(
            Scene(id="s1", actor_does="x", app_shows="x", where="cli",
                 cmd=(sys.executable, "-c",
                      "open('src/greeter/__init__.py', 'w').write("
                      "'def greet(name): return \"WRONG\"')")),
            Scene(id="s2", actor_does="x", app_shows="x", where="cli",
                 cmd=(sys.executable, "-c", "pass")),
        ),
        tasks=(), acceptance=("x",),
    )

    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, plan)

    assert acc.scenes[0].passed is False
    assert "PermissionError" in acc.scenes[0].tail or "Permission denied" in acc.scenes[0].tail
    assert acc.snapshot_mutated is False
    assert acc.passed is False
    assert len(acc.scenes) == 2  # s2 still ran (s1 failing is not a mutation)
    assert acc.scenes[1].passed is True
    assert acc.full_suite_passed is True  # the full suite ran too, against clean code
    # The real repo's own tracked file was never touched.
    assert "Hello" in (ws.alpha / "src" / "greeter" / "__init__.py").read_text(encoding="utf-8")

    data = json.loads(acceptance_path(ws.config, "alpha", "job1").read_text(encoding="utf-8"))
    assert data["snapshot_mutated"] is False
    assert data["passed"] is False
    assert len(data["scenes"]) == 2

    events = [e for _seq, e in ws.ledger.events("alpha", "acceptance.recorded")]
    assert events[0].payload["snapshot_mutated"] is False
    assert events[0].payload["reason"] == "scenes_or_suite_failed"


def test_scene_that_tries_to_patch_and_restore_src_now_fails_outright(
    workspace: Workspace,
) -> None:
    """Regression for the "acceptance certifies different executed
    code" finding (engine/acceptance.py module docstring, codex-
    goal-review-4.md item 12): a scene that patches a tracked file,
    runs (anything) against the patched version, then restores the
    original bytes before anything re-checks `git status` used to
    leave no HEAD-tracked diff, so _tracked_mutated_paths alone could
    never catch it -- the scene's own command had, in fact, run
    against code different from what later gets committed and
    certified. _lock_tracked_files now makes every tracked file
    read-only in each stage's own fresh snapshot before the scene
    runs at all, so the very first `write_text` call here raises
    PermissionError and the scene fails outright (never gets to
    "restore" anything, let alone leave a marker behind for s2)."""
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    plan = Plan(
        goal="x", ui=False,
        scenes=(
            Scene(id="s1", actor_does="x", app_shows="x", where="cli",
                 cmd=(sys.executable, "-c",
                      "import pathlib; "
                      "p = pathlib.Path('src/greeter/__init__.py'); "
                      "orig = p.read_text(); "
                      "p.write_text('def greet(name): return \"POISON\"'); "
                      "pathlib.Path('left_behind_marker.txt').write_text('x'); "
                      "p.write_text(orig)")),
            Scene(id="s2", actor_does="x", app_shows="x", where="cli",
                 cmd=(sys.executable, "-c",
                      "import pathlib, sys; "
                      "assert not pathlib.Path('left_behind_marker.txt').exists(); "
                      "sys.path.insert(0, 'src'); from greeter import greet; "
                      "assert greet('World') == 'Hello, World'")),
        ),
        tasks=(), acceptance=("x",),
    )

    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, plan)

    assert acc.scenes[0].passed is False
    assert "PermissionError" in acc.scenes[0].tail or "Permission denied" in acc.scenes[0].tail
    assert acc.snapshot_mutated is False
    assert acc.passed is False
    # s1's own script raised on the very first write_text call, before
    # the marker write or the restore it never reached -- so s2 (which
    # still runs; a failed-but-not-mutated scene does not stop the
    # loop) finds no marker and the original, untouched implementation.
    assert len(acc.scenes) == 2
    assert acc.scenes[1].passed is True


@_needs_bwrap
def test_scene_cannot_delete_and_recreate_a_tracked_file_under_bwrap(
    workspace: Workspace,
) -> None:
    """The one gap plain chmod (_lock_tracked_files) cannot close on
    its own: unlink is governed by the PARENT directory's write
    permission, not the file's own mode, so a scene could delete a
    tracked file and write a new one back at the same path -- an
    "untracked substitute" standing in for the certified implementation
    without ever tripping _tracked_mutated_paths's bare-untracked
    exemption differently than a legitimate new scratch file. Under
    sandbox="bwrap", the tracked file is ALSO bind-mounted read-only
    directly (run_acceptance's extra_readonly), and a bind-mounted path
    cannot be unlinked either (EBUSY) -- this closes that remaining
    gap; test_scene_that_tries_to_patch_and_restore_src_now_fails_outright
    already covers the plain in-place write under every sandbox mode,
    including "none"."""
    ws = _sample_workspace(workspace)
    ws = Workspace(ws.root, replace(ws.config, sandbox="bwrap"), ws.ledger,
                  ws.alpha, ws.beta)
    attempt_id = _open(ws)
    plan = Plan(
        goal="x", ui=False,
        scenes=(
            Scene(id="s1", actor_does="x", app_shows="x", where="cli",
                 cmd=(sys.executable, "-c",
                      "import pathlib; "
                      "p = pathlib.Path('src/greeter/__init__.py'); "
                      "p.unlink(); "
                      "p.write_text('def greet(name): return \"POISON\"')")),
        ),
        tasks=(), acceptance=("x",),
    )

    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, plan)

    assert acc.scenes[0].passed is False
    assert acc.passed is False


def test_scene_that_drops_an_untracked_config_file_does_not_leak_into_the_next_scene(
    workspace: Workspace,
) -> None:
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    plan = Plan(
        goal="x", ui=False,
        scenes=(
            Scene(id="s1", actor_does="x", app_shows="x", where="cli",
                 cmd=(sys.executable, "-c",
                      "open('config.override.json', 'w').write('{}')")),
            Scene(id="s2", actor_does="x", app_shows="x", where="cli",
                 cmd=(sys.executable, "-c",
                      "import pathlib; "
                      "assert not pathlib.Path('config.override.json').exists()")),
        ),
        tasks=(), acceptance=("x",),
    )

    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, plan)

    assert acc.scenes[0].passed is True
    assert acc.scenes[1].passed is True
    assert acc.passed is True
    # Never reached the real repo either.
    assert not (ws.alpha / "config.override.json").exists()


def test_scene_that_poisons_a_cache_dir_does_not_leak_into_the_full_suite(
    workspace: Workspace,
) -> None:
    """Same guarantee as the two tests above, exercised against the
    full suite's own fresh snapshot instead of a second scene."""
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    plan = Plan(
        goal="x", ui=False,
        scenes=(
            Scene(id="s1", actor_does="x", app_shows="x", where="cli",
                 cmd=(sys.executable, "-c",
                      "import pathlib; "
                      "d = pathlib.Path('.pytest_cache_poison'); d.mkdir(); "
                      "(d / 'poison.txt').write_text('bad')")),
        ),
        tasks=(), acceptance=("x",),
    )

    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, plan)

    assert acc.scenes[0].passed is True
    assert acc.full_suite_passed is True
    assert acc.passed is True
    assert not (ws.alpha / ".pytest_cache_poison").exists()


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
    # _probe_observed requires the marker AND this exact target's own
    # url on the same stdout line, not just the bare marker.
    (probe_dir / "t1.py").write_text(
        "print('PROBE OK https://docs.stripe.com/api')\n", encoding="utf-8",
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
    # _probe_observed requires the marker AND this target's own url.
    (probe_dir / "t1.py").write_text("print('PROBE OK https://a')\n", encoding="utf-8")
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


# ---- a covers-claiming scene's cmd is resolved/verified, never run as
# given -- see engine.plan._scene_script_arg and
# engine.acceptance._trusted_scene_argv/verify_reviewed_scenes ---------

def _write_and_review_real_plan(ws: Workspace, job_name: str, scene_cmd: tuple[str, ...]) -> None:
    """Write a real plan.json (engine.plan's own schema, not the
    tests/plan_stub one) with one scene covering requirement R1 via
    `scene_cmd`, and record it as reviewed -- the on-disk state
    engine.plan.verify_reviewed_scenes/check_resumed_plan actually
    read, independent from whatever Plan object a caller then hands
    to run_acceptance."""
    from engine.ledger import Ledger
    from engine.plan import plan_path, record_plan_reviewed

    data = {
        "goal": "greet works", "ui": False,
        "scenes": [{"id": "s1", "actor_does": "greets", "app_shows": "Hello, World",
                    "where": "cli", "cmd": list(scene_cmd), "covers": ["R1"]}],
        "tasks": [{"id": "t1", "title": "x", "detail": "", "owns": ["src/"],
                   "depends_on": []}],
        "acceptance": ["greet returns Hello, X"],
        "requirements": [{"id": "R1", "text": "greet returns Hello, X", "kind": "function"}],
    }
    path = plan_path(ws.alpha, job_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    record_plan_reviewed(ws.ledger, "alpha", "attempt-ignored", ws.alpha, job_name)


def _real_plan(ws: Workspace, job_name: str):
    from engine.plan import load_plan

    return load_plan(ws.alpha, job_name)


def test_scene_covering_a_requirement_via_bare_python3_passes(workspace: Workspace) -> None:
    """End-to-end happy path: a reviewed, unedited covers-claiming
    scene using a bare "python3" runner actually runs and passes."""
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    scenes_dir = ws.alpha / "tests" / "scenes"
    scenes_dir.mkdir(parents=True)
    (scenes_dir / "s1.py").write_text(
        "import sys; sys.path.insert(0, 'src')\n"
        "from greeter import greet\n"
        "assert greet('World') == 'Hello, World'\n",
        encoding="utf-8",
    )
    _write_and_review_real_plan(ws, "job1", ("python3", "tests/scenes/s1.py"))

    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, _real_plan(ws, "job1"))

    assert acc.scenes[0].passed is True
    assert acc.passed is True


def test_scene_with_path_qualified_runner_is_never_executed_even_if_it_would_exit_0(
    workspace: Workspace,
) -> None:
    """A covers-claiming scene whose cmd[0] is path-qualified (e.g.
    "./fake_node", the external review's exact shape) must never
    actually run -- even a fake runner that would always exit 0 must
    not make the scene, or overall acceptance, pass."""
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    scenes_dir = ws.alpha / "tests" / "scenes"
    scenes_dir.mkdir(parents=True)
    (scenes_dir / "s1.js").write_text("// never actually run\n", encoding="utf-8")
    fake_runner = ws.alpha / "fake_node"
    fake_runner.write_text("#!/bin/sh\ntouch RAN_FAKE_RUNNER\nexit 0\n", encoding="utf-8")
    fake_runner.chmod(0o755)
    _write_and_review_real_plan(ws, "job1", ("./fake_node", "tests/scenes/s1.js"))

    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, _real_plan(ws, "job1"))

    assert acc.scenes[0].passed is False
    assert acc.scenes[0].tail == "scene_cmd_untrusted"
    assert acc.passed is False
    assert not (ws.alpha / "RAN_FAKE_RUNNER").exists()


def test_scene_with_symlinked_tests_scenes_directory_is_never_executed(
    workspace: Workspace,
) -> None:
    """The boundary-escape shape at runtime: "tests/scenes" itself
    is a symlink (here, to "src", which make_sample already commits)
    -- resolving it BEFORE it becomes the containment boundary would
    make src/check.py look "contained" in tests/scenes/. Must never
    actually run, regardless of what check.py would have done."""
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    (ws.alpha / "src" / "check.py").write_text(
        "open('RAN_CHECK', 'w').close()\n", encoding="utf-8",
    )
    (ws.alpha / "tests" / "scenes").symlink_to(ws.alpha / "src")
    _write_and_review_real_plan(ws, "job1", ("python3", "tests/scenes/check.py"))

    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, _real_plan(ws, "job1"))

    assert acc.scenes[0].passed is False
    assert acc.scenes[0].tail == "scene_cmd_untrusted"
    assert acc.passed is False
    assert not (ws.alpha / "RAN_CHECK").exists()


def test_scene_edited_after_review_fails_acceptance_without_running_any_scene(
    workspace: Workspace,
) -> None:
    """A scene script edited after its plan was reviewed must fail
    acceptance outright -- no scene (and no full suite) ever runs,
    since nothing was certified against THIS edited version."""
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    scenes_dir = ws.alpha / "tests" / "scenes"
    scenes_dir.mkdir(parents=True)
    (scenes_dir / "s1.py").write_text(
        "import sys; sys.path.insert(0, 'src')\n"
        "from greeter import greet\n"
        "assert greet('World') == 'Hello, World'\n",
        encoding="utf-8",
    )
    _write_and_review_real_plan(ws, "job1", ("python3", "tests/scenes/s1.py"))
    # Edited AFTER review -- never seen by any reviewer.
    (scenes_dir / "s1.py").write_text("import sys; sys.exit(1)  # tampered\n",
                                      encoding="utf-8")

    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, _real_plan(ws, "job1"))

    assert acc.scenes == ()
    assert acc.full_suite_passed is False
    assert acc.passed is False
