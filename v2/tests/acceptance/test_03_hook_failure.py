"""Acceptance 03 — hook failures (timeout/crash/garbage/bad decision) must
never result in an allow. A policy step consulting such a hook stays denied.
"""
from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

from engine import hooks
from engine.ledger import Event
from engine.policy import Action, Actor, Policy
from engine.state import transition
from engine.steps.open_job import copy_root as production_copy_root
from tests.conftest import Workspace

HOOKS_DIR = Path(__file__).resolve().parent.parent.parent / "engine" / "fakes" / "hooks"


def test_timeout_never_allows() -> None:
    decision = hooks.run(HOOKS_DIR / "slow.py", {}, timeout_sec=0.2)
    assert decision.allowed is False


def test_crash_never_allows() -> None:
    decision = hooks.run(HOOKS_DIR / "crash.py", {}, timeout_sec=1.0)
    assert decision.allowed is False


def test_garbage_stdout_never_allows() -> None:
    decision = hooks.run(HOOKS_DIR / "garbage.py", {}, timeout_sec=1.0)
    assert decision.allowed is False


def test_bad_decision_value_never_allows(tmp_path: Path) -> None:
    script = tmp_path / "bad_decision.py"
    script.write_text(
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'decision': 'maybe'}))\n",
        encoding="utf-8",
    )
    decision = hooks.run(script, {}, timeout_sec=1.0)
    assert decision.allowed is False


def test_consulting_failing_hooks_never_ends_up_allowed() -> None:
    results = [
        hooks.run(HOOKS_DIR / "slow.py", {}, timeout_sec=0.2),
        hooks.run(HOOKS_DIR / "crash.py", {}, timeout_sec=1.0),
        hooks.run(HOOKS_DIR / "garbage.py", {}, timeout_sec=1.0),
    ]
    assert all(not decision.allowed for decision in results)


def test_ok_hook_is_the_only_one_that_allows() -> None:
    decision = hooks.run(HOOKS_DIR / "ok.py", {}, timeout_sec=1.0)
    assert decision.allowed is True


def _open_alpha(workspace: Workspace) -> str:
    attempt_id = "J-alpha-HOOK#1"
    workspace.ledger.append(Event(
        project="alpha", kind="job.opened", actor_kind="owner",
        actor_id="owner", attempt_id=attempt_id,
        payload={
            "job_id": "J-alpha-HOOK", "job_name": "hookjob",
            "attempt_id": attempt_id, "n": 1, "kind": "full",
            "generation": 1, "dirty_paths": [],
        },
    ))
    transition(workspace.ledger, "alpha", "open_job", "owner")
    return attempt_id


def test_policy_denies_otherwise_allowed_write_when_hook_fails(
    workspace: Workspace,
) -> None:
    """P2-3: Policy was never wired to consult config.hooks — an
    otherwise-allowed worker write stayed allowed no matter what a
    configured hook said. A failing/denying hook must flip the decision."""
    attempt_id = _open_alpha(workspace)
    copy_root = production_copy_root(workspace.config, f"{attempt_id}-coupling", "builder")
    shutil.copytree(workspace.alpha, copy_root)
    actor = Actor(kind="worker", role="builder", attempt_id=attempt_id,
                  generation=1, station="build", copy_root=copy_root)
    action = Action(tool="write", path=copy_root / "src" / "new.py")

    for name in ("slow.py", "crash.py", "garbage.py"):
        cfg = replace(workspace.config, hooks=(HOOKS_DIR / name,))
        policy = Policy(cfg, workspace.ledger)
        decision = policy.check("alpha", actor, action)
        assert decision.allowed is False, f"{name} should have denied"
        assert decision.reason.startswith("hook:"), decision.reason


def test_policy_allows_through_an_ok_hook(workspace: Workspace) -> None:
    """P2-3: with an allowing hook configured, the otherwise-allowed write
    stays allowed."""
    attempt_id = _open_alpha(workspace)
    copy_root = production_copy_root(workspace.config, f"{attempt_id}-coupling-ok", "builder")
    shutil.copytree(workspace.alpha, copy_root)
    actor = Actor(kind="worker", role="builder", attempt_id=attempt_id,
                  generation=1, station="build", copy_root=copy_root)
    action = Action(tool="write", path=copy_root / "src" / "new.py")

    cfg = replace(workspace.config, hooks=(HOOKS_DIR / "ok.py",))
    policy = Policy(cfg, workspace.ledger)
    decision = policy.check("alpha", actor, action)
    assert decision.allowed is True
