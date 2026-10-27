"""결함 42: a project's `file:../_shared/kit`-style package.json
dependency, mapped via ProjectConfig.file_dep_targets, must actually resolve
INSIDE a real bwrap sandbox -- not just as a (source, dest) pair a unit test
inspects on its own, which test_35 (tests/acceptance/test_t9d_also_own.py)
already covered and which never caught this.

Reproduces the real gb2 layout (tools/gb2lib/workspace.py's own
_setup_app_workspace): an apps ROOT holds one directory per app, each with
its own `repo/` (the gb2-managed worktree) and its own `_shared/` sibling
(the real external package `file:../_shared/kit` escapes into) --
`hidden_paths=(apps_root,)` is exactly what that function sets on every
project's own config, to keep one app's sandbox from seeing another app's
source tree. That hide is deliberately BROADER than `repo` itself, so the
escaping dependency's real target also lands inside it and needs its own,
deeper readonly_remap to be re-exposed -- which is the actual bug: with only
`remap_pairs_for_root`/`remap_pairs_for_root`s' path arithmetic and no
pre-created mountpoint, the deeper bind either never gets emitted for the
right root (scenes/probe, whose own `repo` parameter is a disposable
snapshot, never the real repo) or can't be created at all once its parent is
already hidden read-only.

Three real callers build a sandboxed argv from a project's own copy_links (a
`node_modules` symlink whose relative target escapes the repo, e.g.
`@scope/kit -> ../../../_shared/kit`) plus its file_dep_targets, and this
test runs each one for real under bwrap: the worker's own copy
(engine.application.steps.worker_sandbox.sandboxed_argv), the real repo via
bench's own sandbox (engine.application.steps.suite_run.bench_sandbox_argv),
and a disposable acceptance snapshot
(engine.application.acceptance_bench.run_in_bench_sandbox). Skipped outright
when bwrap itself is unavailable on this host (sandbox.available() is
False) -- there is nothing this test could prove without it, and the
engine's own sandbox="bwrap" callers already fail closed in that case
(sandbox.SandboxUnavailable)."""
from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.config import EngineConfig, ProjectConfig
from engine.infrastructure import sandbox
from engine.infrastructure import evidence
from engine.application.steps import file_deps, suite_snapshot, worker_sandbox
from engine.application.steps.open_job import copy_root, open_job
from engine.application.steps.suite_run import bench_sandbox_argv
from engine.application.acceptance_bench import run_in_bench_sandbox
from engine.infrastructure.workers.base import WorkerSpec
from tests.conftest import Workspace, git

def _node_runs_in_bwrap() -> bool:
    """True iff a plain bwrap sandbox (root bound read-only, fresh /tmp) can exec `node` on this
    host. Some CI runners keep node where such a sandbox cannot reach it; there the real-sandbox
    node checks below cannot say anything about file deps, so they skip with that reason."""
    if not sandbox.available():
        return False
    try:
        proc = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
             "--", "node", "-e", "0"], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


needs_bwrap = pytest.mark.skipif(
    not _node_runs_in_bwrap() or os.environ.get("GITHUB_ACTIONS") == "true",
    reason="bwrap unavailable, node not runnable inside bwrap, or a GitHub Actions runner "
           "(node lives where the engine's own sandbox hides it; these checks target the real host)",
)


class _NoHomeWritableRunner:
    home_writable_subpaths: tuple[str, ...] = ()


def _probe_cmd(dest: Path) -> list[str]:
    """`require()` the escaping package if node is on PATH (the real shape
    a project's own test tooling needs), else just read the file straight
    off disk -- either way, success means the escaping symlink chain
    resolved all the way to the real target INSIDE the sandbox."""
    if _node_on_path():
        return ["node", "-e", f"require('{dest.as_posix()}/package.json'); "
                               "console.log('ok')"]
    return ["cat", str(dest / "index.js")]


def _node_on_path() -> bool:
    import shutil
    return shutil.which("node") is not None


def _wire_project(workspace: Workspace, label: str) -> tuple[EngineConfig, Path, Path, Path]:
    """Builds a fresh project under its own `apps_root/app/{repo,_shared}`
    tree (the real gb2 shape -- see this module's own docstring), registers
    it on `workspace.config`/`workspace.ledger`, and returns
    (config, repo, real_shared_kit, apps_root)."""
    apps_root = workspace.root / f"apps-{label}"
    app_dir = apps_root / "app"
    repo = app_dir / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    (repo / "package.json").write_text(
        '{"name": "app", "dependencies": {"@scope/kit": "file:../_shared/kit"}}\n',
        encoding="utf-8",
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")

    real_shared = app_dir / "_shared" / "kit"
    real_shared.mkdir(parents=True)
    (real_shared / "index.js").write_text("module.exports = {ok: true};\n")
    (real_shared / "package.json").write_text('{"name": "kit", "main": "index.js"}\n')

    scoped = repo / "node_modules" / "@scope"
    scoped.mkdir(parents=True)
    (scoped / "kit").symlink_to("../../../_shared/kit")

    targets = (("../_shared/kit", str(real_shared)),)
    project_config = ProjectConfig(
        "app", repo, excludes=("node_modules/",), copy_links=("node_modules",),
        file_dep_targets=targets, hidden_paths=(apps_root,),
    )
    config = replace(
        workspace.config, projects={**workspace.config.projects, "app": project_config},
        sandbox="bwrap",
    )
    workspace.ledger.register_project("app")
    return config, repo, real_shared, apps_root


@needs_bwrap
def test_42_worker_copy_sandbox_resolves_escaping_file_dep(workspace: Workspace) -> None:
    """The builder's own copy -- the role copy every worker actually runs
    in (engine.application.steps.worker_sandbox.sandboxed_argv)."""
    config, _repo, real_shared, _apps_root = _wire_project(workspace, "worker")
    attempt_id = open_job(config, workspace.ledger, "app", "filedep-worker",
                          "full", ("builder",))
    own_copy = copy_root(config, attempt_id, "builder")
    ctx = SimpleNamespace(config=config, project="app", attempt_id=attempt_id,
                          ledger=workspace.ledger,
                          request=SimpleNamespace(request_id="rid-worker"))
    spec = WorkerSpec(
        project="app", attempt_id=attempt_id, generation=1, station="build",
        role="builder", copy_root=own_copy, workdir=own_copy, task="",
        result_path=own_copy / "r.json", config_path=None,
    )
    argv = worker_sandbox.sandboxed_argv(ctx, spec, _probe_cmd(real_shared), {},
                                         _NoHomeWritableRunner())
    pass_fds = sandbox.pass_fds_for(argv)
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10,
                                 pass_fds=pass_fds)
    finally:
        sandbox.close_fds(pass_fds)
    assert result.returncode == 0, (
        f"worker copy sandbox could not resolve the escaping file dep: "
        f"rc={result.returncode} argv={argv} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


@needs_bwrap
def test_42_bench_real_repo_sandbox_resolves_escaping_file_dep(workspace: Workspace) -> None:
    """The real repo itself, run through bench's own sandbox builder
    (engine.application.steps.suite_run.bench_sandbox_argv) -- the same
    builder run_tests/trust_checks use against the real, ongoing repo."""
    config, repo, real_shared, _apps_root = _wire_project(workspace, "bench")
    ctx = SimpleNamespace(config=config, project="app", attempt_id="A-1")
    argv, child_env = bench_sandbox_argv(ctx, repo, _probe_cmd(real_shared), {})
    popen_env = sandbox.exec_env(config.sandbox, child_env)
    pass_fds = sandbox.pass_fds_for(argv)
    try:
        result = subprocess.run(argv, cwd=str(repo), capture_output=True,
                                 text=True, timeout=10, env=popen_env, pass_fds=pass_fds)
    finally:
        sandbox.close_fds(pass_fds)
    assert result.returncode == 0, (
        f"bench real-repo sandbox could not resolve the escaping file dep: "
        f"rc={result.returncode} argv={argv} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


@needs_bwrap
def test_42_acceptance_snapshot_sandbox_resolves_escaping_file_dep(workspace: Workspace) -> None:
    """A disposable acceptance snapshot (engine.application.steps.
    suite_snapshot.make_snapshot + link_snapshot_deps), run through the
    SAME bench sandbox builder via engine.application.acceptance_bench.
    run_in_bench_sandbox -- the primitive every acceptance scene/full-suite
    stage and probe script actually use."""
    config, repo, real_shared, _apps_root = _wire_project(workspace, "accept")
    project_config = config.projects["app"]
    fp = evidence.fingerprint(repo, project_config.excludes)
    snapshot = suite_snapshot.snapshot_dir(config, "A-1", "scene-0")
    suite_snapshot.make_snapshot(repo, snapshot, fp)
    suite_snapshot.link_snapshot_deps(project_config, repo, snapshot)

    returncode, tail = run_in_bench_sandbox(
        config, "app", "A-1", snapshot, _probe_cmd(real_shared), {}, 10.0,
        extra_readonly=(repo,),
    )
    assert returncode == 0, (
        f"acceptance snapshot sandbox could not resolve the escaping file dep: "
        f"rc={returncode} tail={tail!r}"
    )


def test_42_sandbox_file_dep_pairs_is_the_one_shared_root() -> None:
    """Structural guard: every sandbox builder must compute its (source,
    dest) pairs through file_deps.sandbox_file_dep_pairs, the one function
    that knows how to make a readonly_remap destination actually mountable
    under bwrap -- never remap_pairs_for_root/remap_pairs_for_roots
    directly, which only compute the path arithmetic and say nothing about
    whether bwrap can mount there."""
    import inspect
    from engine.application.steps import suite_run, worker_sandbox as ws

    for module in (suite_run, ws):
        source = inspect.getsource(module)
        assert "file_deps.sandbox_file_dep_pairs(" in source
        assert "file_deps.remap_pairs_for_root(" not in source
        assert "file_deps.remap_pairs_for_roots(" not in source


def test_42_sandbox_file_dep_pairs_precreates_unmountable_dest(tmp_path: Path) -> None:
    """RED-equivalent unit check for the deeper bug, without bwrap: a dest
    that does not exist on the host, under a parent this call never
    separately mounts writable, must still come back ready to `--ro-bind`
    onto -- sandbox_file_dep_pairs pre-creates it on the host (the same
    technique suite_run._bench_sandbox_argv already uses for
    bench_readonly_paths), so the caller never has to special-case this
    itself."""
    source_pkg = tmp_path / "real" / "kit"
    source_pkg.mkdir(parents=True)
    (source_pkg / "index.js").write_text("ok")

    root = tmp_path / "apps" / "sample" / "jobs" / "J-1" / "builder"
    root.mkdir(parents=True)
    targets = (("../_shared/kit", str(source_pkg)),)

    pairs = file_deps.sandbox_file_dep_pairs(root, repo=root, file_dep_targets=targets)
    assert pairs == ((source_pkg, root.parent / "_shared" / "kit"),)
    _source, dest = pairs[0]
    assert dest.exists()  # pre-created on the host, ready to --ro-bind onto
    assert dest.is_dir()
    assert list(dest.iterdir()) == []  # an empty mountpoint, not a copy of the source
