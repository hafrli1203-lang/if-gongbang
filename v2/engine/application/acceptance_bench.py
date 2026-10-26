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

import subprocess
from pathlib import Path
from types import SimpleNamespace

from engine.infrastructure import sandbox
from engine.config import EngineConfig
from engine.application.steps.suite_run import TAIL_CHARS, bench_sandbox_argv


def bench_ctx(config: EngineConfig, project: str, attempt_id: str) -> SimpleNamespace:
    """Stand-in for StepContext (see module docstring) -- the only
    attributes bench_sandbox_argv/bench_env ever read."""
    return SimpleNamespace(config=config, project=project, attempt_id=attempt_id)


def run_in_bench_sandbox(
    config: EngineConfig, project: str, attempt_id: str, repo: Path,
    argv: list[str], env: dict[str, str], timeout_sec: float, *, net: bool = False,
    extra_readonly: tuple[Path, ...] = (),
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
    actually points to."""
    try:
        wrapped_argv, child_env = bench_sandbox_argv(
            bench_ctx(config, project, attempt_id), repo, argv, env, net=net,
            extra_readonly=extra_readonly)
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
