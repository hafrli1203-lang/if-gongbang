"""Shared argv builder for git invoked by the engine itself (never inside
a worker/bench sandbox) against a project's REAL repository -- as
opposed to a disposable copy the engine just created and fully
controls.

Why this exists: a project's `.git/config` can be modified by code that
ran with write access to the repo (today, a bench test_cmd before
engine.application.steps.suite_run's own repo/.git read-only sandbox mount; a
worker never gets this, since it only ever writes its own separate
copy). A planted `core.fsmonitor` command or hook script is then
executed -- with full engine privileges, completely outside any
sandbox -- the next time the engine runs a git command that consults
it (`status`, `diff`, `ls-files`, among others). Every engine-side git
call against a real repo therefore disables both unconditionally,
regardless of whatever the repo's own .git/config says; `-c` flags
given this way always win over the config file."""
from __future__ import annotations

from pathlib import Path

_GIT_SAFETY_FLAGS = ("-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null")


_CALLS = 0


def git_argv(repo: Path, *args: str) -> list[str]:
    """["git", "-C", str(repo), <safety flags>, *args]. Every call is
    counted (see call_count) so a job can report how many git processes
    the engine started for it."""
    global _CALLS
    _CALLS += 1
    return ["git", "-C", str(repo), *_GIT_SAFETY_FLAGS, *args]


def call_count() -> int:
    """git_argv calls made in this engine process so far. Per process,
    not per job: a job that spans several engine runs (e.g. a wait for
    an install approval) reports only the last run's share."""
    return _CALLS


__all__ = ["git_argv", "call_count"]
