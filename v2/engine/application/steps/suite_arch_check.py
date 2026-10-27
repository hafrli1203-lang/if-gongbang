"""bench's structure-check step (ProjectConfig.arch_check_cmd) -- split
out of engine.application.steps.suite_run to keep that file under the
800-line cap (COMMON_ARCHITECTURE_STANDARD.md §3). See suite_run's own
module docstring, "run_arch_check" entry, for the step's full contract
and reasoning; this module holds only the pure mechanics, called with
suite_run's own sandbox/env helpers passed in (never imported back from
suite_run -- that would be circular, since suite_run imports `run` from
here)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

from engine.application.processor import StepContext, StepFailed
from engine.application.steps.open_job import dirty_paths
from engine.config import ProjectConfig
from engine.infrastructure import sandbox

# Signature of suite_run._bench_sandbox_argv: (ctx, repo, argv, env, *,
# extra_readonly=()) -> (wrapped_argv, child_env).
SandboxArgvFn = Callable[..., tuple[list[str], dict[str, str]]]


def run(ctx: StepContext, *, project: ProjectConfig, repo: Path,
        extra_readonly: tuple[Path, ...], env: dict[str, str], timeout_sec: float,
        sandbox_mode: str, sandbox_argv_fn: SandboxArgvFn) -> dict[str, object]:
    """Not configured (`project.arch_check_cmd` empty): {"configured":
    False} -- record_bench then treats this exactly as if the step did
    not exist. Configured: runs it (argv + [str(repo), "--json"],
    inside the same bench sandbox run_tests uses) and requires its
    stdout to parse as the one JSON object {"violations": [{"check",
    "file", "detail"}, ...], ...} (arch_check.py --json's own shape) --
    anything else (crashed, not found, printed a traceback instead) is
    StepFailed("arch_check_failed") regardless of exit code: exit 1 on
    a clean run that FOUND violations is normal data, never a tool
    failure. Splits violations into "new_violations" (this attempt's
    own doing -- "file" is one of dirty_paths against HEAD right now,
    the same set merge just applied, since commit runs after bench)
    and "existing_violation_count" (every other, pre-existing
    violation, counted but never blocking -- MASTER_RULES §2, "기존
    기능을 없애지 않는다")."""
    if not project.arch_check_cmd:
        return {"configured": False}
    argv = [*project.arch_check_cmd, str(repo), "--json"]
    wrapped_argv, child_env = sandbox_argv_fn(ctx, repo, argv, env,
                                              extra_readonly=extra_readonly)
    popen_env = sandbox.exec_env(sandbox_mode, child_env)
    pass_fds = sandbox.pass_fds_for(wrapped_argv)
    try:
        result = subprocess.run(
            wrapped_argv, cwd=str(repo), capture_output=True,
            timeout=timeout_sec, text=True, env=popen_env, pass_fds=pass_fds,
        )
    except subprocess.TimeoutExpired as exc:
        raise StepFailed("arch_check_timeout") from exc
    finally:
        sandbox.close_fds(pass_fds)
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise StepFailed("arch_check_failed") from exc
    violations = data.get("violations") if isinstance(data, dict) else None
    if not isinstance(violations, list):
        raise StepFailed("arch_check_failed")
    changed = set(dirty_paths(repo, project.excludes))
    new_violations = [v for v in violations
                      if isinstance(v, dict) and v.get("file") in changed]
    return {
        "configured": True,
        "new_violations": new_violations,
        "existing_violation_count": len(violations) - len(new_violations),
    }
