"""Whole-tree immutability of an acceptance stage's snapshot: the root
is read-only while a scene / the full suite runs (bwrap bind, plus a
directory chmod under sandbox="none"), and a whole-tree manifest taken
before and after the run catches ANY added, removed or changed entry --
untracked, ignored and symlink entries included -- that slipped past
the write barrier. Also: the declared copy_link_writable_subpaths stay
writable through scratch directories outside the snapshot, and every
stage's evidence names the tree it ran in."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

from engine.application import acceptance
from engine.application.acceptance import acceptance_path, run_acceptance
from engine.application.steps import suite_tree_guard
from engine.application.steps.open_job import open_job
from engine.application.steps.suite_snapshot import snapshot_dir
from engine.infrastructure import sandbox
from tests.conftest import Workspace, git
from tests.fixtures.sample_app import make_sample
from tests.plan_stub import Plan, Scene
from tests.security_gate import bwrap_skipif

_needs_bwrap = bwrap_skipif(sandbox.available(), reason="bwrap not available on this host")


def _sample_workspace(workspace: Workspace, *, mode: str = "none") -> Workspace:
    sample = make_sample(workspace.root / "sample", 1, Path(sys.executable))
    project_config = replace(workspace.config.projects["alpha"],
                             repo=sample.repo, test_cmd=sample.test_cmd)
    config = replace(workspace.config, sandbox=mode,
                     projects={**workspace.config.projects, "alpha": project_config})
    return Workspace(workspace.root, config, workspace.ledger, sample.repo, workspace.beta)


def _open(ws: Workspace) -> str:
    return open_job(ws.config, ws.ledger, "alpha", "job1", "full", roles=("builder",))


def _plan(*scripts: str) -> Plan:
    scenes = tuple(
        Scene(id=f"s{i + 1}", actor_does="x", app_shows="x", where="cli",
              cmd=(sys.executable, "-c", script))
        for i, script in enumerate(scripts)
    )
    return Plan(goal="x", ui=False, scenes=scenes, tasks=(), acceptance=("x",))


_GREET_OK = ("import sys; sys.path.insert(0, 'src'); from greeter import greet; "
             "assert greet('World') == 'Hello, World'")


# ---- manifest primitives --------------------------------------------------

def test_manifest_sees_same_size_rewrite_and_does_not_follow_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "a.txt").write_text("aaaa", encoding="utf-8")
    (outside / "dep.js").write_text("one", encoding="utf-8")
    (root / "node_modules").symlink_to(outside)
    before = suite_tree_guard.tree_manifest(root)
    assert "node_modules/dep.js" not in before  # never followed

    (outside / "dep.js").write_text("two", encoding="utf-8")
    assert suite_tree_guard.manifest_changes(before, suite_tree_guard.tree_manifest(root)) == ()

    (root / "a.txt").write_text("bbbb", encoding="utf-8")
    (root / "node_modules").unlink()
    (root / "node_modules").symlink_to(tmp_path)
    (root / ".hidden_ignored").mkdir()
    after = suite_tree_guard.tree_manifest(root)
    assert suite_tree_guard.manifest_changes(before, after) == (
        ".hidden_ignored", "a.txt", "node_modules")
    assert suite_tree_guard.manifest_digest(before) != suite_tree_guard.manifest_digest(after)


def test_locked_tree_dirs_refuse_new_entries_and_unlock_restores(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    suite_tree_guard.lock_tree_dirs(root)
    try:
        for target in (root / "new.txt", root / "sub" / "new.txt"):
            try:
                target.write_text("x", encoding="utf-8")
            except PermissionError:
                continue
            raise AssertionError(f"{target} was writable")
    finally:
        suite_tree_guard.unlock_tree_dirs(root)
    (root / "sub" / "ok.txt").write_text("x", encoding="utf-8")


# ---- sandbox="none": chmod barrier + manifest catches a bypass ----------------

def test_none_mode_root_file_planted_after_chmod_bypass_is_caught(workspace: Workspace) -> None:
    """Same-UID code can chmod the locked root back and write -- the
    whole-tree manifest still catches the new, untracked root file:
    the stage is marked mutated and nothing after it runs."""
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    plant = ("import os; os.chmod('.', 0o755); "
             "open('vitest.config.js', 'w').write('export default {}')")
    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id,
                         _plan(plant, _GREET_OK))

    assert acc.snapshot_mutated is True
    assert acc.passed is False
    assert len(acc.scenes) == 1  # s2 never ran
    assert acc.full_suite_tail == "not_run"
    assert "vitest.config.js" in acc.stages[0].changed
    events = [e for _seq, e in ws.ledger.events("alpha", "acceptance.recorded")]
    assert events[0].payload["reason"] == "snapshot_mutated"
    assert "vitest.config.js" in events[0].payload["stages"][0]["changed"]
    assert not (ws.alpha / "vitest.config.js").exists()


def test_none_mode_root_symlink_and_ignored_path_are_caught(workspace: Workspace) -> None:
    """A new node_modules symlink at the root and a write under a
    gitignored directory (.deps/, in the sample app's own .gitignore)
    are both invisible to `git status`-style checks; the manifest is
    not."""
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    plant = ("import os; os.chmod('.', 0o755); os.symlink('/usr', 'node_modules'); "
             "os.mkdir('.deps'); open('.deps/shadow.py', 'w').write('X = 1')")
    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, _plan(plant))

    assert acc.snapshot_mutated is True
    assert acc.passed is False
    changed = acc.stages[0].changed
    assert "node_modules" in changed
    assert ".deps" in changed and ".deps/shadow.py" in changed


def test_full_suite_stage_is_guarded_too(workspace: Workspace) -> None:
    ws = _sample_workspace(workspace)
    script = ("import os; os.chmod('.', 0o755); open('planted.txt', 'w').write('x')")
    project_config = replace(ws.config.projects["alpha"],
                             test_cmd=(sys.executable, "-c", script))
    ws = Workspace(ws.root, replace(ws.config, projects={**ws.config.projects,
                                                          "alpha": project_config}),
                   ws.ledger, ws.alpha, ws.beta)
    attempt_id = _open(ws)
    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, _plan("pass"))

    assert acc.scenes[0].passed is True
    assert acc.snapshot_mutated is True
    assert acc.passed is False
    assert [stage.stage for stage in acc.stages] == ["scene-0", "full_suite"]
    assert acc.stages[1].changed == (".", "planted.txt")


# ---- evidence: the tree each stage ran in ---------------------------------

def test_passing_run_records_tree_and_identical_manifests_per_stage(
    workspace: Workspace,
) -> None:
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, _plan(_GREET_OK))

    assert acc.passed is True
    assert [stage.stage for stage in acc.stages] == ["scene-0", "full_suite"]
    for stage in acc.stages:
        assert stage.tree == acc.fingerprint
        assert stage.manifest_before == stage.manifest_after
        assert stage.changed == ()
    data = json.loads(acceptance_path(ws.config, "alpha", "job1").read_text(encoding="utf-8"))
    assert [s["tree"] for s in data["stages"]] == [acc.fingerprint, acc.fingerprint]
    assert all(s["changed_count"] == 0 for s in data["stages"])
    events = [e for _seq, e in ws.ledger.events("alpha", "acceptance.recorded")]
    assert events[0].payload["stages"][1]["tree"] == acc.fingerprint


def test_rerun_can_rebuild_a_snapshot_whose_dirs_were_left_locked(
    workspace: Workspace,
) -> None:
    """A crash between lock and unlock leaves read-only directories
    behind; the next make_snapshot of the same label must still
    succeed."""
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    assert run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, _plan("pass")).passed
    suite_tree_guard.lock_tree_dirs(snapshot_dir(ws.config, attempt_id, "scene-0"))
    assert run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, _plan("pass")).passed


# ---- sandbox="bwrap": read-only root bind -----------------------------------

_BWRAP_PLANT = (
    "import os, sys\n"
    "refused = 0\n"
    "try:\n"
    "    os.chmod('.', 0o755)\n"
    "except OSError:\n"
    "    pass\n"
    "for action in (lambda: open('vitest.config.js', 'w').write('x'),\n"
    "               lambda: os.symlink('/usr', 'node_modules_shadow'),\n"
    "               lambda: os.mkdir('.deps'),\n"
    "               lambda: os.unlink('README.md') if os.path.exists('README.md') else 1/0):\n"
    "    try:\n"
    "        action()\n"
    "    except (OSError, ZeroDivisionError):\n"
    "        refused += 1\n"
    "sys.exit(0 if refused == 4 else 1)\n"
)


@_needs_bwrap
def test_bwrap_root_writes_are_refused_and_nothing_is_mutated(workspace: Workspace) -> None:
    ws = _sample_workspace(workspace, mode="bwrap")
    attempt_id = _open(ws)
    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id,
                         _plan(_BWRAP_PLANT, _GREET_OK))

    assert acc.scenes[0].passed is True, acc.scenes[0].tail  # every write refused
    assert acc.scenes[1].passed is True, acc.scenes[1].tail
    assert acc.snapshot_mutated is False
    assert acc.full_suite_passed is True, acc.full_suite_tail
    assert acc.passed is True
    assert all(stage.changed == () for stage in acc.stages)


@_needs_bwrap
def test_bwrap_declared_cache_subpath_and_tmp_stay_writable(workspace: Workspace) -> None:
    """copy_link_writable_subpaths (node_modules/.vite-temp, the path
    vitest writes its bundled config to) is writable through a scratch
    directory OUTSIDE the snapshot; /tmp is writable; the rest of
    node_modules is not. The stage passes and the snapshot is
    unchanged -- the cache write never lands in its tree."""
    ws = _sample_workspace(workspace, mode="bwrap")
    (ws.alpha / ".gitignore").write_text(".deps/\n__pycache__/\nnode_modules/\n",
                                         encoding="utf-8")
    (ws.alpha / "node_modules" / "pkg").mkdir(parents=True)
    (ws.alpha / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1\n",
                                                                encoding="utf-8")
    git(ws.alpha, "add", "-A")
    git(ws.alpha, "commit", "-q", "-m", "ignore node_modules")
    project_config = replace(ws.config.projects["alpha"], copy_links=("node_modules",),
                             copy_link_writable_subpaths={"node_modules": (".vite-temp",)})
    ws = Workspace(ws.root, replace(ws.config, projects={**ws.config.projects,
                                                          "alpha": project_config}),
                   ws.ledger, ws.alpha, ws.beta)
    attempt_id = _open(ws)
    script = (
        "import os, sys\n"
        "open('node_modules/.vite-temp/vitest.config.js.timestamp.mjs', 'w').write('x')\n"
        "open('/tmp/scratch.txt', 'w').write('x')\n"
        "assert open('node_modules/pkg/index.js').read().startswith('module.exports')\n"
        "try:\n"
        "    open('node_modules/evil.js', 'w').write('x')\n"
        "except OSError:\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    acc = run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, _plan(script))

    assert acc.scenes[0].passed is True, acc.scenes[0].tail
    assert acc.snapshot_mutated is False
    assert acc.passed is True, acc.full_suite_tail
    host_cache = snapshot_dir(ws.config, attempt_id, "scene-0") / "node_modules" / ".vite-temp"
    assert host_cache.is_dir() and list(host_cache.iterdir()) == []
    assert not (ws.alpha / "node_modules" / "evil.js").exists()


def test_stage_snapshot_dirs_are_unlocked_after_each_run(workspace: Workspace) -> None:
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    run_acceptance(ws.config, ws.ledger, "alpha", attempt_id, _plan("pass"))
    root = snapshot_dir(ws.config, attempt_id, "scene-0")
    assert os.access(root, os.W_OK)
    assert acceptance.StageIntegrity is not None


def test_vite5_config_keeps_root_writable_and_nested_cache_parents_are_created(tmp_path) -> None:
    import json
    from types import SimpleNamespace
    from engine.application.steps.suite_tree_guard import scratch_writable_pairs, vite_writes_beside_config
    root = tmp_path / "repo"
    (root / "node_modules" / "vite").mkdir(parents=True)
    (root / "node_modules" / "vite" / "package.json").write_text(json.dumps({"version": "5.4.1"}))
    assert vite_writes_beside_config(root) is False  # no config file
    (root / "vite.config.js").write_text("export default {}\n")
    assert vite_writes_beside_config(root) is True
    (root / "node_modules" / "vite" / "package.json").write_text(json.dumps({"version": "6.0.0"}))
    assert vite_writes_beside_config(root) is False
    cfg = SimpleNamespace(copy_link_writable_subpaths={"node_modules": [".cache/vitest"]})
    pairs = scratch_writable_pairs(cfg, root, tmp_path / "scratch")
    assert [str(d.relative_to(root)) for _s, d in pairs] == ["node_modules/.cache/vitest"]
