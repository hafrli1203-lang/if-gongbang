"""Acceptance 02 — boundary enforcement while the project is OPEN.

Covers protected paths across every write-like tool, role authorization,
sensitive reads, and process-group cleanup for a closed station.
"""
from __future__ import annotations

import shutil

import pytest

from engine import procs
from engine.ledger import Event
from engine.policy import Action, Actor, Policy
from engine.state import transition
from engine.steps.open_job import copy_root as production_copy_root
from tests.conftest import Workspace


def _open_alpha(workspace: Workspace) -> str:
    attempt_id = "J-alpha-B#1"
    workspace.ledger.append(Event(
        project="alpha", kind="job.opened", actor_kind="owner",
        actor_id="owner", attempt_id=attempt_id,
        payload={
            "job_id": "J-alpha-B", "job_name": "boundary",
            "attempt_id": attempt_id, "n": 1, "kind": "full",
            "generation": 1, "dirty_paths": [],
        },
    ))
    transition(workspace.ledger, "alpha", "open_job", "owner")
    return attempt_id


def _builder(workspace: Workspace, attempt_id: str, suffix: str) -> tuple[Actor, "Path"]:
    from pathlib import Path
    # Real worker copies live at config.home/"copies"/{attempt_id}/{role}
    # (engine.steps.open_job.copy_root) — use that layout here too, so
    # these tests exercise the protected-path rule a real worker copy
    # actually hits (home/"copies" is protected by default; only the
    # actor's own copy_root is carved out of that).
    copy_root: Path = production_copy_root(workspace.config, f"{attempt_id}-{suffix}", "builder")
    shutil.copytree(workspace.alpha, copy_root)
    actor = Actor(kind="worker", role="builder", attempt_id=attempt_id,
                  generation=1, station="build", copy_root=copy_root)
    return actor, copy_root


def test_mcp_is_decided_by_granted_server_not_by_path(workspace: Workspace) -> None:
    # An MCP call carries a server name, not a path: it is allowed only
    # for a server granted to this request and station, and a call with
    # no server, or for a server nobody granted, is always denied.
    attempt_id = _open_alpha(workspace)
    policy = Policy(workspace.config, workspace.ledger)
    actor, copy_root = _builder(workspace, attempt_id, "mcp")
    no_server = policy.check("alpha", actor, Action(tool="mcp", path=copy_root / "src" / "new.py"))
    assert no_server.allowed is False
    ungranted = policy.check("alpha", actor, Action(tool="mcp", target="playwright"))
    assert ungranted.allowed is False


@pytest.mark.parametrize("tool", ["write", "browser"])
def test_protected_paths_denied_for_every_write_like_tool(
    workspace: Workspace, tool: str,
) -> None:
    # "terminal" is deliberately excluded here: a worker terminal action
    # carries a command, not a path, so it is not decided by the
    # path-prefix check this test exercises — see
    # tests/unit/test_policy.py::test_terminal_* for its own rules
    # (command required, shell-metachar rejection, allow-pattern table).
    attempt_id = _open_alpha(workspace)
    policy = Policy(workspace.config, workspace.ledger)
    actor, copy_root = _builder(workspace, attempt_id, f"prot-{tool}")

    allowed_decision = policy.check("alpha", actor, Action(
        tool=tool, path=copy_root / "src" / "new.py"))
    assert allowed_decision.allowed is True

    denied_paths = [
        workspace.config.secrets_dir / "prod.key",
        workspace.config.ledger_path,
        copy_root / ".git" / "config",
        copy_root / ".gitignore",
        copy_root / ".claude" / "settings.json",
    ]
    for path in denied_paths:
        decision = policy.check("alpha", actor, Action(tool=tool, path=path))
        assert decision.allowed is False, f"{tool} should deny {path}"


def test_reviewer_cannot_write_src(workspace: Workspace) -> None:
    attempt_id = _open_alpha(workspace)
    policy = Policy(workspace.config, workspace.ledger)
    copy_root = production_copy_root(workspace.config, f"{attempt_id}-reviewer-src", "reviewer")
    shutil.copytree(workspace.alpha, copy_root)
    reviewer = Actor(kind="worker", role="reviewer", attempt_id=attempt_id,
                     generation=1, station="build", copy_root=copy_root)
    decision = policy.check("alpha", reviewer, Action(
        tool="write", path=copy_root / "src" / "new.py"))
    assert decision.allowed is False


def test_worker_cannot_write_into_another_roles_copy(workspace: Workspace) -> None:
    """P1-1: home/"copies" stays protected for a copy that is not this
    actor's own copy_root, even though both copies live under the same
    config.home/"copies" tree (one attempt, two roles)."""
    attempt_id = _open_alpha(workspace)
    policy = Policy(workspace.config, workspace.ledger)
    builder_actor, _builder_copy = _builder(workspace, attempt_id, "crossrole-builder")
    other_copy = production_copy_root(workspace.config, f"{attempt_id}-crossrole-builder", "reviewer")
    shutil.copytree(workspace.alpha, other_copy)
    decision = policy.check("alpha", builder_actor, Action(
        tool="write", path=other_copy / "src" / "app.py"))
    assert decision.allowed is False
    assert decision.reason == "protected_path"


def test_worker_read_of_secret_denied(workspace: Workspace) -> None:
    attempt_id = _open_alpha(workspace)
    policy = Policy(workspace.config, workspace.ledger)
    actor, _copy_root = _builder(workspace, attempt_id, "read-secret")
    decision = policy.check("alpha", actor, Action(
        tool="read", path=workspace.config.secrets_dir / "prod.key"))
    assert decision.allowed is False


def test_spawned_child_dead_after_close_station(workspace: Workspace) -> None:
    attempt_id = _open_alpha(workspace)
    proc = procs.spawn(
        workspace.ledger, "alpha", attempt_id, "build",
        ["sleep", "30"], cwd=workspace.alpha,
    )
    assert procs.is_alive(proc.pid) is True
    killed = procs.close_station(workspace.ledger, "alpha", attempt_id, "build")
    assert proc.pid in killed
    proc.wait(timeout=5)
    assert procs.is_alive(proc.pid) is False
