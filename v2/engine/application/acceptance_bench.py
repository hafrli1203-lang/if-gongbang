"""The one sandboxed-subprocess primitive every acceptance stage (a
scene, the full suite, a probe script) runs through -- reusing
engine.application.steps.suite_run's own sandbox-argv builder and env rules
exactly, via a SimpleNamespace standing in for StepContext, since
bench_sandbox_argv/_bench_env only ever read ctx.config/ctx.project/
ctx.attempt_id, never ctx.request or ctx.fakes. This is the one bench
sandbox policy this codebase has (writable=repo, readonly=repo/.git,
hidden=engine home/secrets/other projects/real HOME, net=False) --
acceptance stages get exactly the same isolation a bench request gets,
not a second, divergent one.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from engine.infrastructure import sandbox
from engine.config import EngineConfig
from engine.application.steps.suite_run import TAIL_CHARS, bench_home_dir, bench_sandbox_argv
from engine.application.steps.worker_sandbox import cli_runtime_mounts


def bench_ctx(config: EngineConfig, project: str, attempt_id: str) -> SimpleNamespace:
    """Stand-in for StepContext (see module docstring) -- the only
    attributes bench_sandbox_argv/bench_env ever read."""
    return SimpleNamespace(config=config, project=project, attempt_id=attempt_id)


def run_in_bench_sandbox(
    config: EngineConfig, project: str, attempt_id: str, repo: Path,
    argv: list[str], env: dict[str, str], timeout_sec: float, *, net: bool = False,
    extra_readonly: tuple[Path, ...] = (),
    runtime_clis: tuple[str, ...] = (),
    root_readonly: bool = False,
) -> tuple[int, str]:
    """(returncode, tail) for one command run exactly the way
    engine.application.steps.suite_run.run_tests runs the real suite. A command
    that fails to even start (bad argv[0], sandbox unavailable) or
    times out both count as failed here -- returncode -1, the
    exception text as the tail -- rather than raising: a single scene
    or the full suite failing to start must not abort every other
    scene still to be checked.

    extra_readonly is forwarded to bench_sandbox_argv as-is -- here,
    always the real project repo (see engine.application.acceptance.run_acceptance),
    since `repo` in THIS function is the disposable snapshot (see
    engine.application.acceptance's module docstring), not the real repo any
    copy_links symlink inside it (bench_snapshot.link_snapshot_deps)
    actually points to.

    runtime_clis: bare CLI names (e.g. "claude") that `argv` itself may
    spawn as a CHILD of the command run here -- e.g. a probe script
    that shells out to a real CLI (engine.application.acceptance_probe's own
    project.probe_runtime_clis). This bench sandbox otherwise hides
    the real HOME wholesale and never mounts any CLI's own installed
    binary or login files (unlike engine.application.steps.worker_sandbox.
    sandboxed_argv, which a real worker/reviewer process goes through
    instead) -- without this, such a CLI fails inside the sandbox with
    a bare "command not found" (ENOENT), which proves nothing about
    whatever the CLI itself would have done. For each name here,
    engine.application.steps.worker_sandbox.cli_runtime_mounts (the SAME
    function a real worker uses) seeds this run's own, already-
    isolated bench $HOME (suite_run.bench_home_dir -- never the real
    operator HOME) with that CLI's minimal login files and returns the
    binary/symlink mounts to add; empty (default) changes nothing
    about the mounts bench_sandbox_argv already builds.

    root_readonly: forwarded to bench_sandbox_argv -- `repo` (the
    disposable snapshot) is bound read-only as a whole; acceptance's
    scenes and full suite always pass True."""
    extra_symlinks: tuple[tuple[Path, Path], ...] = ()
    extra_writable: tuple[Path, ...] = ()
    if runtime_clis:
        home_dir = bench_home_dir(bench_ctx(config, project, attempt_id))
        real_home_str = os.environ.get("HOME")
        real_home = Path(real_home_str) if real_home_str else None
        for cli_name in runtime_clis:
            readonly, symlinks, writable = cli_runtime_mounts(cli_name, home_dir, real_home)
            extra_readonly += readonly
            extra_symlinks += symlinks
            extra_writable += writable
    try:
        wrapped_argv, child_env = bench_sandbox_argv(
            bench_ctx(config, project, attempt_id), repo, argv, env, net=net,
            extra_readonly=extra_readonly, extra_symlinks=extra_symlinks,
            extra_writable=extra_writable, root_readonly=root_readonly)
    except sandbox.SandboxUnavailable as exc:
        return -1, f"sandbox_unavailable: {exc}"
    popen_env = sandbox.exec_env(config.sandbox, child_env)
    pass_fds = sandbox.pass_fds_for(wrapped_argv)
    try:
        result = subprocess.run(
            wrapped_argv, cwd=str(repo), capture_output=True, text=True,
            timeout=timeout_sec, env=popen_env, pass_fds=pass_fds,
        )
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except OSError as exc:
        return -1, f"failed to start: {exc}"
    finally:
        sandbox.close_fds(pass_fds)
    combined = (result.stdout or "") + (result.stderr or "")
    return result.returncode, combined[-TAIL_CHARS:]
