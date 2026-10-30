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

# arch_check.py --json's own violation shape (this module's docstring,
# "run_arch_check" entry): {"check", "file", "detail"}, all strings. A
# tool whose stdout parses as JSON but whose violations don't match
# this -- {}, a bare string/number, a dict missing one of the three
# fields, or a non-string value for one of them -- must never fall
# through to "existing_violation_count" as if it were real pre-existing
# data: that would silently hide a broken arch_check tool AND any real
# violations it was supposed to report (F3,. Caught as the
# same StepFailed("arch_check_failed") a crashed tool or non-JSON
# stdout already gets, in `run` below.
_REQUIRED_VIOLATION_FIELDS = ("check", "file", "detail")


def _is_well_formed_violation(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    return all(isinstance(item.get(field), str) for field in _REQUIRED_VIOLATION_FIELDS)


def tool_readonly_paths(project: ProjectConfig) -> tuple[Path, ...]:
    """Every absolute-path token inside project.arch_check_cmd that
    exists on the host -- e.g. a shared arch_check tool checked out
    once, outside this project's own repo (for example an app config whose arch_check_cmd points at
    the engine's own tools/arch_check.py) -- for a caller to
    expose read-only inside the bench sandbox, on top of whatever this
    project's OWN hidden_paths hides (hidden_paths
    exists to hide SIBLING projects' repos/secrets from this project's
    bench sandbox, never the tool this project itself configured bench
    to run -- an app whose hidden_paths cover the shared apps root tmpfs's the exact directory the arch_check
    tool lives under, so `python3 .../arch_check.py` failed with "No
    such file or directory", a non-JSON stdout StepFailed
    ("arch_check_failed") read as the tool itself being broken
    regardless of exit code -- when the real repo had zero new
    violations the whole time. Reproduced directly with bwrap,
   , byte-identical error text. worker_sandbox.py already
    makes the same exception for the engine's own source tree via
    `readonly += (ENGINE_ROOT,)` -- this generalizes that one
    hardcoded case to whatever directory arch_check_cmd's own
    absolute-path token actually resolves to, so it works for ANY
    project's externally-located tool, not only one living under this
    engine's own checkout. A relative token (a script committed
    inside the repo, e.g. the T12 tests' own "fake_arch.py") is
    already covered by `repo` itself and is left untouched here --
    exposing its parent would just re-expose `repo` a second time."""
    paths: tuple[Path, ...] = ()
    for token in project.arch_check_cmd:
        candidate = Path(token)
        if candidate.is_absolute() and candidate.exists():
            paths += (candidate.parent,)
    return paths


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
    if not all(_is_well_formed_violation(v) for v in violations):
        raise StepFailed("arch_check_failed")
    changed = set(dirty_paths(repo, project.excludes))
    new_violations = [v for v in violations
                      if isinstance(v, dict) and v.get("file") in changed]
    return {
        "configured": True,
        "new_violations": new_violations,
        "existing_violation_count": len(violations) - len(new_violations),
    }
