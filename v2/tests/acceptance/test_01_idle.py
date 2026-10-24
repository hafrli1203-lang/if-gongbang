"""Acceptance 01 — idle (CLOSED) project: safety policy still evaluates.

Read is allowed for everyone; any worker write-like or privileged tool is
denied while the project has not been opened. Owner read is allowed too.
"""
from __future__ import annotations

import shutil

from engine.policy import Action, Actor, Policy
from engine.steps.open_job import copy_root as production_copy_root
from tests.conftest import Workspace


def _idle_copy(workspace: Workspace, name: str):
    # Real worker copies live at config.home/"copies"/{attempt_id}/{role}
    # (engine.steps.open_job.copy_root); use that layout here too, even
    # though no job is actually open yet, so these tests exercise the same
    # protected-path logic a real worker copy hits in production.
    dest = production_copy_root(workspace.config, f"J-alpha-IDLE-{name}", "builder")
    shutil.copytree(workspace.alpha, dest)
    return dest


def test_worker_read_allowed_while_closed(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    assert workspace.ledger.project("alpha").state == "CLOSED"
    copy_root = _idle_copy(workspace, "read")
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=0,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(
        tool="read", path=copy_root / "src/app.py"))
    assert decision.allowed is True


def test_worker_write_denied_while_closed(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    copy_root = _idle_copy(workspace, "write")
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=0,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(
        tool="write", path=copy_root / "src/new.py"))
    assert decision.allowed is False


def test_worker_terminal_denied_while_closed(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    copy_root = _idle_copy(workspace, "terminal")
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=0,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(
        tool="terminal", path=copy_root, target="pytest"))
    assert decision.allowed is False


def test_worker_publish_denied_while_closed(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    copy_root = _idle_copy(workspace, "publish")
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=0,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(tool="publish", target="prod"))
    assert decision.allowed is False


def test_worker_install_denied_while_closed(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    copy_root = _idle_copy(workspace, "install")
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=0,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(tool="install", target="deps"))
    assert decision.allowed is False


def test_owner_read_allowed_while_closed(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    owner = Actor(kind="owner", role="owner", attempt_id=None, generation=0)
    decision = policy.check("alpha", owner, Action(
        tool="read", path=workspace.alpha / "src/app.py"))
    assert decision.allowed is True
