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
from tests.conftest import Workspace, git
from tests.fixtures.sample_app import make_sample
from tests.plan_stub import Plan, Scene
from tests.probe_target import contacting_py_script, serve_target
from tests.security_gate import bwrap_skipif

_needs_bwrap = bwrap_skipif(
    sandbox.available(), reason="bwrap not available on this host",
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


def test_scene_writing_a_new_file_at_the_snapshot_root_is_refused(
    workspace: Workspace,
) -> None:
    """Scenes/the full suite run against a disposable snapshot of the
    tree (engine.acceptance's module docstring), never the real repo,
    and that snapshot is immutable while the scene runs -- untracked
    paths at its ROOT included: a new root-level file could be an
    execution substitute (a config file, a module a loose import
    resolves first). Under sandbox="none" the barrier is the snapshot's
    own directory chmod (suite_tree_guard.lock_tree_dirs), so the
    write itself raises PermissionError and the scene fails; nothing
    was mutated and the real repo is untouched."""
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

    assert acc.scenes[0].passed is False
    assert "Permission denied" in acc.scenes[0].tail
    assert acc.snapshot_mutated is False
    assert acc.full_suite_passed is True
    assert acc.tree_changed is False
    assert acc.passed is False
    assert not (ws.alpha / "untracked_by_scene.txt").exists()

    data = json.loads(acceptance_path(ws.config, "alpha", "job1").read_text(encoding="utf-8"))
    assert data["tree_changed"] is False
    assert data["passed"] is False
    assert data["fingerprint"] == acc.fingerprint
    assert len(data["fingerprint"]) == 40

    events = [e for _seq, e in ws.ledger.events("alpha", "acceptance.recorded")]
    assert events[0].payload["tree_changed"] is False
    assert events[0].payload["fingerprint"] == acc.fingerprint
    assert events[0].payload["reason"] == "scenes_or_suite_failed"


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
    """Acceptance must not certify different executed code than what
    actually gets committed (see engine/acceptance.py's own module
    docstring): a scene that patches a tracked file,
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

    # s1's write is refused outright (the snapshot is immutable while a
    # stage runs); s2 runs in its own fresh snapshot and sees nothing.
    assert acc.scenes[0].passed is False
    assert acc.scenes[1].passed is True
    assert acc.snapshot_mutated is False
    assert acc.passed is False
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

    assert acc.scenes[0].passed is False  # mkdir refused
    assert acc.snapshot_mutated is False
    assert acc.full_suite_passed is True
    assert acc.passed is False
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
    # probe_target_id), never derived from the target's own name. The
    # script must really reach the target (a local fake server) through
    # the engine's recording proxy, and print the legacy line too.
    with serve_target() as url:
        (probe_dir / "t1.py").write_text(contacting_py_script(url), encoding="utf-8")
        results = run_probe_scripts(
            ws.config, ws.ledger, "alpha", attempt_id, "job1", [("stripe", url)],
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
    # t2 has no script at all -- must be reported missing, not silently
    # covered by t1's passing script.
    with serve_target("/a") as url_a:
        (probe_dir / "t1.py").write_text(contacting_py_script(url_a), encoding="utf-8")
        results = run_probe_scripts(
            ws.config, ws.ledger, "alpha", attempt_id, "job1",
            [("Naver API", url_a), ("Naver_API", "https://b")],
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


def test_scene_edited_in_the_race_window_before_the_old_check_point_is_still_caught(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_acceptance must never check engine.plan.verify_reviewed_scenes
    against the live `repo` separately from the fingerprint/snapshot it
    actually executes: if it did, a scene script edited in the window
    between that check and the fingerprint call (a worker process
    racing the acceptance run) could pass the digest check against the
    OLD content, then get fingerprinted/snapshotted/executed WITH the
    new, never-reviewed content, and still certify a passing scene.
    This test reproduces that exact window by monkeypatching
    engine.acceptance._fingerprint_or_none -- the point right after
    where such a repo-level check would run -- to tamper with the
    scene script as a side effect the first time it runs, before
    delegating to the real implementation. run_acceptance must never
    let this scene run at all: the digest check happens against each
    stage's OWN already-built snapshot, which necessarily already
    contains whatever the file held at fingerprint time, so there is
    no window left for a check to see one version and an execution to
    see another."""
    import engine.acceptance as acceptance_module

    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    scenes_dir = ws.alpha / "tests" / "scenes"
    scenes_dir.mkdir(parents=True)
    scene_path = scenes_dir / "s1.py"
    scene_path.write_text(
        "import sys; sys.path.insert(0, 'src')\n"
        "from greeter import greet\n"
        "assert greet('World') == 'Hello, World'\n",
        encoding="utf-8",
    )
    _write_and_review_real_plan(ws, "job1", ("python3", "tests/scenes/s1.py"))

    real_fingerprint_or_none = acceptance_module._fingerprint_or_none
    tampered = {"done": False}

    def _tampering_fingerprint_or_none(repo: Path, excludes: tuple[str, ...]):
        if not tampered["done"]:
            tampered["done"] = True
            # The race: something changes the scene script in the
            # window right after the (now-removed) repo-level check
            # the old code ran, and right before this fingerprint call
            # -- never seen by any reviewer.
            scene_path.write_text("import sys; sys.exit(1)  # tampered mid-race\n",
                                  encoding="utf-8")
        return real_fingerprint_or_none(repo, excludes)

    monkeypatch.setattr(acceptance_module, "_fingerprint_or_none",
                        _tampering_fingerprint_or_none)

    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, _real_plan(ws, "job1"))

    # Never ran unreviewed: no scene (and no full suite) executed at all.
    assert acc.scenes == ()
    assert acc.full_suite_passed is False
    # Never yields passing evidence.
    assert acc.passed is False

    events = [e for _seq, e in ws.ledger.events("alpha", "acceptance.recorded")]
    assert events[-1].payload["reason"] == "plan_changed_since_review"
    data = json.loads(acceptance_path(ws.config, "alpha", "job1").read_text(encoding="utf-8"))
    assert data["passed"] is False
    assert data["scenes"] == []


def test_stripping_covers_after_review_is_still_caught_even_with_a_stale_caller_plan(
    workspace: Workspace,
) -> None:
    """The previous gating condition for whether run_acceptance even runs
    engine.plan.verify_reviewed_scenes was "does the Plan object the
    caller passed in still claim any `covers`" -- but a plan.json
    edited AFTER review to simply delete every `covers` entry makes
    that condition False too, silently disabling the one check meant
    to catch exactly that edit. This test strips `covers` from
    plan.json on disk after review, then calls run_acceptance with a
    STALE Plan object a caller parsed BEFORE that edit (the realistic
    full_orbit shape: a design-station resume loads Plan once, then a
    later edit happens to the repo before run_acceptance actually
    runs) -- the stale object still has `covers`, so the OLD gating
    condition would have run the check using the caller's own (now
    wrong) belief about the content. The fix makes run_acceptance load
    its own Plan from this call's certified snapshot whenever
    is_plan_reviewed() is true, regardless of what the caller passed,
    so the covers-stripped plan.json's changed digest is still caught."""
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
    stale_plan = _real_plan(ws, "job1")  # parsed BEFORE the strip below
    assert any(getattr(s, "covers", ()) for s in stale_plan.scenes)

    # The attack: delete `covers` from plan.json on disk, after review.
    from engine.plan import plan_path

    path = plan_path(ws.alpha, "job1")
    data = json.loads(path.read_text(encoding="utf-8"))
    for scene in data["scenes"]:
        scene.pop("covers", None)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, stale_plan)

    assert acc.scenes == ()
    assert acc.full_suite_passed is False
    assert acc.passed is False
    events = [e for _seq, e in ws.ledger.events("alpha", "acceptance.recorded")]
    assert events[-1].payload["reason"] == "plan_changed_since_review"


def test_a_never_reviewed_stub_plan_runs_exactly_as_given(workspace: Workspace) -> None:
    """A job with no plan.reviewed record at all (the test-only
    Plan/Scene stub most of this file's other tests use) has no
    certified snapshot-plan to prefer over the caller's own -- the fix
    for the reviewed case must not force every caller to also write a
    real engine.plan plan.json + review record just to call
    run_acceptance at all."""
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
    assert acc.scenes[0].passed is True


def test_scene_that_exits_0_with_zero_tests_executed_does_not_pass(
    workspace: Workspace,
) -> None:
    """Authorization evidence must never be a skipped test: a scene
    that exits 0 but never actually executed anything (e.g. its own
    assertions all skipped)
    must not be certified as having proven the requirement it claims
    to cover. Reuses bench's own existing vitest no-tests-ran banner
    (engine.steps.bench_run.looks_like_zero_tests_ran) -- never a
    separately invented heuristic."""
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    plan = Plan(
        goal="x", ui=False,
        scenes=(Scene(id="s1", actor_does="x", app_shows="x", where="cli",
                     cmd=(sys.executable, "-c",
                          "print('Tests  0 passed (0)'); raise SystemExit(0)")),),
        tasks=(), acceptance=("x",),
    )

    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, plan)

    assert acc.scenes[0].passed is False
    assert acc.scenes[0].tail == "no_tests_executed"
    assert acc.passed is False


def test_full_suite_that_exits_0_with_all_tests_skipped_does_not_pass(
    workspace: Workspace,
) -> None:
    ws = _sample_workspace(workspace)
    project_config = replace(ws.config.projects["alpha"],
                             test_cmd=(sys.executable, "-c",
                                      "print('Tests  3 skipped (3)'); raise SystemExit(0)"))
    ws = Workspace(ws.root, replace(ws.config, projects={**ws.config.projects, "alpha": project_config}),
                   ws.ledger, ws.alpha, ws.beta)
    attempt_id = _open(ws)
    plan = Plan(
        goal="x", ui=False,
        scenes=(Scene(id="s1", actor_does="x", app_shows="x", where="cli",
                     cmd=(sys.executable, "-c", "raise SystemExit(0)")),),
        tasks=(), acceptance=("x",),
    )

    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, plan)

    assert acc.scenes[0].passed is True
    assert acc.full_suite_passed is False
    assert acc.full_suite_tail == "no_tests_executed"
    assert acc.passed is False


@_needs_bwrap
def test_scene_cannot_shadow_a_tracked_package_with_a_new_untracked_file(
    workspace: Workspace,
) -> None:
    """A scene creating a brand-new, untracked file INSIDE an existing
    tracked package directory (src/greeter/, here) -- an untracked
    package shadowing a tracked module -- must fail to write it under
    sandbox="bwrap" (engine.acceptance.
    _tracked_directories' own read-only bind); the snapshot root is
    covered separately (test_acceptance_tree_guard.py)."""
    ws = _sample_workspace(workspace)
    ws = Workspace(ws.root, replace(ws.config, sandbox="bwrap"), ws.ledger,
                  ws.alpha, ws.beta)
    attempt_id = _open(ws)
    shadow_script = (
        "import pathlib, sys\n"
        "try:\n"
        "    pathlib.Path('src/greeter/evil.py').write_text('EVIL = 1\\n')\n"
        "except OSError:\n"
        "    sys.exit(0)  # write correctly refused\n"
        "sys.exit(1)  # write SUCCEEDED -- containment failed\n"
    )
    plan = Plan(
        goal="x", ui=False,
        scenes=(Scene(id="s1", actor_does="x", app_shows="x", where="cli",
                     cmd=(sys.executable, "-c", shadow_script)),),
        tasks=(), acceptance=("x",),
    )

    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, plan)

    assert acc.scenes[0].passed is True
    assert not (ws.alpha / "src" / "greeter" / "evil.py").exists()


def test_snapshot_includes_an_export_ignored_file(workspace: Workspace) -> None:
    """A `git archive <fp>` snapshot would honor `.gitattributes`
    export-ignore -- a certified, fingerprinted file could be silently
    MISSING from the snapshot a scene/test actually runs against.
    engine.evidence.export_tree (ls-tree + cat-file blob) ignores
    export-ignore entirely; the snapshot must still contain the file,
    or acceptance refuses outright (it would never even reach the
    scene if the snapshot's own tree hash stopped matching fp)."""
    ws = _sample_workspace(workspace)
    (ws.alpha / ".gitattributes").write_text(
        "src/greeter/__init__.py export-ignore\n", encoding="utf-8",
    )
    git(ws.alpha, "add", "-A")
    git(ws.alpha, "commit", "-q", "-m", "export-ignore the runtime module")
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

    assert acc.scenes[0].passed is True
    assert acc.passed is True
