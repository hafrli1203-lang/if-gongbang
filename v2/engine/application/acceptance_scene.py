"""Running one scene's cmd (or the project's own full test_cmd) inside
the bench sandbox against a disposable snapshot, and detecting a
scene/full-suite stage that left the snapshot's own HEAD-tracked
content mutated. See engine.application.acceptance's module docstring for why
every stage gets its own fresh, disposable snapshot rather than
running against the real repo.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from engine.config import EngineConfig
from engine.infrastructure.gitutil import git_argv
from engine.application.steps.suite_zero_tests import (
    looks_like_zero_tests_ran as _looks_like_zero_tests_ran,
)
from engine.application.steps.open_job import is_within as _is_within

from engine.application.acceptance_bench import run_in_bench_sandbox

if TYPE_CHECKING:
    from engine.domain.plan import Scene


@dataclass(frozen=True)
class SceneResult:
    id: str
    passed: bool
    returncode: int
    tail: str


def _trusted_scene_argv(scene: "Scene", scene_snapshot: Path) -> list[str] | None:
    """The argv engine.application.acceptance actually executes for a covers-
    claiming scene -- never `scene.cmd` as given. engine.domain.plan's own
    plan-review-time check (_scene_script_arg) only looks at STRINGS;
    it cannot stop a plan.json that was never actually re-reviewed
    (see verify_reviewed_scenes, which guards against that separately)
    from still trying to run something untrustworthy here, and it has
    no access to a real filesystem to catch a tracked SYMLINK escaping
    tests/scenes/ at the moment of execution. So this function re-
    derives the scene path the same way (_scene_script_arg), then:
      - resolves cmd[0] (already verified bare, no path separator) to
        an ABSOLUTE executable via shutil.which against the ENGINE's
        OWN os.environ PATH -- never anything the sandboxed child env,
        the project config, or the repo itself could have set -- so
        the actual binary that runs can never be a same-named
        executable the job's own repo happens to ship
      - verifies the scene path's REAL location (symlinks followed),
        resolved against THIS snapshot's REAL, non-symlinked
        tests/scenes/ directory

    None on any failure (no supported runner, no scene path, the
    runner cannot be found via shutil.which, "tests" or
    "tests/scenes" itself is a symlink -- see
    engine.domain.plan.validate_plan's own matching check for why resolving
    THROUGH either one before it becomes the containment boundary is
    exactly the bug this guards against -- or the scene path's real
    location escapes tests/scenes/) -- the caller must record this
    scene as failed, never fall back to running `scene.cmd` as
    given."""
    from engine.domain.plan import _scene_script_arg

    cmd = tuple(scene.cmd)
    scene_path = _scene_script_arg(cmd)
    if scene_path is None:
        return None
    runner_path = shutil.which(cmd[0])
    if runner_path is None:
        return None
    tests_dir = scene_snapshot / "tests"
    scenes_dir = tests_dir / "scenes"
    if tests_dir.is_symlink() or scenes_dir.is_symlink():
        # A symlinked "tests" or "tests/scenes" would make .resolve()
        # follow it BEFORE it becomes the boundary below -- e.g.
        # "tests/scenes" -> "../src" resolves scenes_root to this
        # snapshot's own src/, so src/check.py would then look
        # "contained". The REAL tests/scenes/ is only ever trusted
        # when neither component is itself a symlink.
        return None
    scenes_root = scenes_dir.resolve()
    try:
        resolved = (scene_snapshot / scene_path).resolve()
    except OSError:
        return None
    if not _is_within(resolved, scenes_root):
        return None
    return [runner_path, *cmd[1:]]


def run_scene(config: EngineConfig, project: str, attempt_id: str, repo: Path,
              env: dict[str, str], timeout_sec: float, scene: "Scene",
              extra_readonly: tuple[Path, ...] = ()) -> SceneResult:
    """Runs one scene's cmd inside the bench sandbox against `repo`
    (the scene's own disposable snapshot -- see engine.application.acceptance's
    module docstring). A scene that `covers` a requirement (getattr,
    duck-typed -- the test-only Plan/Scene stub some unit tests still
    use has no `covers` field at all, and is never meant to exercise
    this path) is never run as `scene.cmd` verbatim: _trusted_scene_argv
    re-resolves it to a trusted, absolute argv first. A scene whose cmd
    fails that resolution is recorded failed (returncode=-1, never
    actually executed) -- not silently run as given, and not silently
    treated as passing either. A scene with no `covers` keeps running
    exactly as given, same as before this check existed -- it carries
    no requirement-evidence claim for this resolution to protect."""
    if getattr(scene, "covers", ()):
        argv = _trusted_scene_argv(scene, repo)
        if argv is None:
            return SceneResult(id=scene.id, passed=False, returncode=-1,
                               tail="scene_cmd_untrusted")
    else:
        argv = list(scene.cmd)
    returncode, tail = run_in_bench_sandbox(
        config, project, attempt_id, repo, argv, env, timeout_sec,
        extra_readonly=extra_readonly)
    if returncode == 0 and _looks_like_zero_tests_ran(tail):
        # An exit-0 scene
        # that executed zero actual tests/assertions must not pass --
        # same reuse of bench's own existing no-tests signal as
        # run_full_suite below, never a separately invented one.
        return SceneResult(id=scene.id, passed=False, returncode=-1,
                           tail="no_tests_executed")
    return SceneResult(id=scene.id, passed=returncode == 0,
                       returncode=returncode, tail=tail)


def run_full_suite(config: EngineConfig, project: str, attempt_id: str,
                    repo: Path, env: dict[str, str],
                    timeout_sec: float,
                    extra_readonly: tuple[Path, ...] = ()) -> tuple[bool, int, str]:
    """(passed, returncode, tail) for the project's own test_cmd. A
    project with no test_cmd configured can never be accepted -- there
    is nothing to run as "the project's full test_cmd" -- fails closed
    (returncode -1, "no_test_cmd") rather than silently passing."""
    test_cmd = config.projects[project].test_cmd
    if not test_cmd:
        return False, -1, "no_test_cmd"
    returncode, tail = run_in_bench_sandbox(
        config, project, attempt_id, repo, list(test_cmd), env, timeout_sec,
        extra_readonly=extra_readonly)
    if returncode == 0 and _looks_like_zero_tests_ran(tail):
        return False, -1, "no_tests_executed"
    return returncode == 0, returncode, tail


def tracked_mutated_paths(snapshot: Path) -> tuple[str, ...]:
    """Relative paths in `snapshot`'s own `git status --porcelain -z
    --untracked-files=all` that are NOT a bare untracked addition
    (status "??") -- a scene creating a new scratch/output file is not
    a mutation of the certified tree; a scene that edits, stages,
    deletes or renames a path HEAD already tracks (bench_snapshot.
    make_snapshot's own commit) is. Same call/rename-pair handling as
    engine.application.steps.open_job.dirty_paths, filtered down to tracked
    changes only."""
    out = subprocess.run(
        git_argv(snapshot, "status", "--porcelain", "-z", "--untracked-files=all"),
        check=True, capture_output=True,
    ).stdout
    tokens = out.decode("utf-8", "surrogateescape").split("\0")
    paths: list[str] = []
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if not entry:
            continue
        status, rel = entry[:2], entry[3:]
        tracked = status != "??"
        if tracked:
            paths.append(rel)
        if status[0] in ("R", "C") and i < len(tokens) and tokens[i]:
            if tracked:
                paths.append(tokens[i])
            i += 1
    return tuple(paths)
