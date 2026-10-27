"""Deciding WHICH test command a bench's run_tests step actually runs
-- the full project test_cmd, or a narrower related_cmd scoped to this
attempt's changed files, when the project opts into that and nothing
makes the narrowing untrustworthy.

결함 16, evidence bench seq 1703): a project's full-scope
test_cmd (and the "full(fallback)" scope run_tests falls back to --
engine.application.steps.suite_run._run_run_tests) must never include
plan_model.SCENES_PREFIX (tests/scenes/) scene scripts. Those files are
written by the PLAN station for requirements this attempt's own
build/design work has not finished yet -- a scene testing
not-yet-implemented behavior failing is the expected, correct state
right up until a later station's acceptance pass actually runs it
(engine.application.acceptance, never bench); bench folding that same,
expected-at-this-point failure into "the project's real suite is red"
makes a request's FIRST ever attempt at a multi-station job
unconditionally fail bench, regardless of what that attempt itself
changed. full_scope_argv below appends a scene-excluding argument
chosen PER RUNNER (see its own docstring) -- never a flag invented
without first confirming, against the actual installed runner, that it
exists and does what this needs (crash-out: `npx vitest run --help`
confirmed `--exclude <glob>` in this repo,.
"""
from __future__ import annotations

from engine.application.processor import StepContext
from engine.application.steps.open_job import dirty_paths
from engine.domain.plan_model import SCENES_PREFIX

# Runner-name token -> exclusion argv appended to that runner's own
# full-suite test_cmd, so a bench run never executes a scene script
# (see module docstring). Vitest's own `--exclude <glob>` is the only
# entry confirmed against an installed runner (`npx vitest run --help`,
# -- a project configured with a different runner gets NO
# exclusion appended (test_cmd runs exactly as configured, unchanged
# from before this feature), rather than guessing an unverified flag
# for a runner this engine has never actually run with scenes excluded.
_SCENE_EXCLUSION_BY_RUNNER: dict[str, tuple[str, ...]] = {
    "vitest": ("--exclude", f"{SCENES_PREFIX}**"),
}


def full_scope_argv(test_cmd: tuple[str, ...]) -> list[str]:
    """`test_cmd` with its runner's own scene-exclusion argv appended
    (see _SCENE_EXCLUSION_BY_RUNNER) -- `test_cmd` unchanged if no
    token in it names a known runner. Detected by exact token match
    (e.g. "vitest" as one whole argv entry, as in ["npx", "vitest",
    "run", ...]) rather than a substring search, so a project whose
    own test_cmd happens to mention "vitest" inside some OTHER
    argument (a file path, a reporter name) is never matched by
    accident."""
    return with_scene_exclusion(list(test_cmd))


def with_scene_exclusion(argv: list[str]) -> list[str]:
    """`argv` with its runner's scene-exclusion argv appended. Shared by
    the full AND the related scope: a related run once pulled in later
    tasks' scene tests (they import the changed files) and failed a
    correct task (sample-app T2, defect 25)."""
    for runner, extra in _SCENE_EXCLUSION_BY_RUNNER.items():
        if runner in argv:
            return argv + list(extra)
    return argv


def _changed_source_paths(ctx: StepContext) -> tuple[list[str], bool]:
    """(existing_paths, has_removal): this attempt's changed files
    right now (dirty_paths against HEAD -- the same union the
    fingerprint covers), split into the ones that still exist on disk
    (a deleted file is not a useful argument to a "related tests"
    command) and whether ANY changed path no longer exists -- a
    deletion, or the OLD side of a rename (dirty_paths records both
    paths of a rename/copy; its old path never exists on disk after
    the rename, so it is caught by this same check). A caller must
    treat has_removal as "related-scope selection cannot be trusted
    for this change": a related-tests command driven only by the
    paths that still exist would never account for code that depended
    on a path which no longer exists at all (e.g. a deleted module
    other, untouched files still import), silently narrowing the test
    selection below what actually needs checking. Read-only: no
    write, no git state change."""
    project = ctx.config.projects[ctx.project]
    repo = project.repo
    paths = dirty_paths(repo, project.excludes)
    existing = [p for p in paths if (repo / p).exists()]
    return existing, len(existing) != len(paths)


def build_related_argv(related_cmd: tuple[str, ...], files: list[str]) -> list[str]:
    """related_cmd with the single literal token "{files}" expanded in
    place into one argv entry per changed file (never joined into one
    string -- each file must be its own argv token)."""
    argv: list[str] = []
    for part in related_cmd:
        if part == "{files}":
            argv.extend(files)
        else:
            argv.append(part)
    return argv


def resolve_test_argv(ctx: StepContext) -> tuple[str, list[str]]:
    """Returns (scope, argv) for the real-suite run_tests step. The
    request's own payload can force the full suite outright
    (payload["scope"] == "full", e.g. engine.application.release.ensure_full_bench
    submitting a release's required full-suite bench) -- checked
    first, ahead of the project's own related-tests shortcut, so a
    caller that specifically needs "full" never gets "related" back
    regardless of config. Otherwise "related" only when the project
    opts in (bench_scope == "related", the default) AND configures
    related_cmd AND there is at least one changed source file right
    now AND nothing was deleted or renamed (see
    _changed_source_paths's has_removal: a related-tests command
    driven only by paths that still exist can never account for code
    that depended on a path which no longer exists) -- otherwise
    "full" with the project's own test_cmd, PLUS that runner's own
    scene-exclusion argv (결함 16 -- see full_scope_argv/
    module docstring): a plan-station scene testing not-yet-built
    behavior is expected to fail until engine.application.acceptance actually
    runs it, never bench."""
    project = ctx.config.projects[ctx.project]
    if ctx.request.payload.get("scope") == "full":
        return "full", full_scope_argv(tuple(project.test_cmd))
    if project.bench_scope == "related" and project.related_cmd:
        changed, has_removal = _changed_source_paths(ctx)
        if changed and not has_removal:
            return "related", with_scene_exclusion(build_related_argv(project.related_cmd, changed))
    return "full", full_scope_argv(tuple(project.test_cmd))
