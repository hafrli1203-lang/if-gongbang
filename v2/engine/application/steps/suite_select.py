"""Deciding WHICH test command a bench's run_tests step actually runs
-- the full project test_cmd, or a narrower related_cmd scoped to this
attempt's changed files, when the project opts into that and nothing
makes the narrowing untrustworthy.
"""
from __future__ import annotations

from engine.application.processor import StepContext
from engine.application.steps.open_job import dirty_paths


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
    "full" with the project's own test_cmd, unchanged from before this
    feature."""
    project = ctx.config.projects[ctx.project]
    if ctx.request.payload.get("scope") == "full":
        return "full", list(project.test_cmd)
    if project.bench_scope == "related" and project.related_cmd:
        changed, has_removal = _changed_source_paths(ctx)
        if changed and not has_removal:
            return "related", build_related_argv(project.related_cmd, changed)
    return "full", list(project.test_cmd)
