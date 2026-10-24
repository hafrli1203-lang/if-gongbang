"""Unit tests for engine.hooks: fail-closed subprocess hook execution."""
from __future__ import annotations

from pathlib import Path

from engine import hooks

HOOKS_DIR = Path(__file__).resolve().parent.parent.parent / "engine" / "fakes" / "hooks"


def test_ok_hook_allows() -> None:
    decision = hooks.run(HOOKS_DIR / "ok.py", {"x": 1}, timeout_sec=1.0)
    assert decision.allowed is True


def test_slow_hook_times_out() -> None:
    decision = hooks.run(HOOKS_DIR / "slow.py", {"x": 1}, timeout_sec=0.2)
    assert decision.allowed is False


def test_crash_hook_denied() -> None:
    decision = hooks.run(HOOKS_DIR / "crash.py", {"x": 1}, timeout_sec=1.0)
    assert decision.allowed is False


def test_garbage_hook_denied() -> None:
    decision = hooks.run(HOOKS_DIR / "garbage.py", {"x": 1}, timeout_sec=1.0)
    assert decision.allowed is False


def test_invalid_decision_value_denied(tmp_path: Path) -> None:
    script = tmp_path / "maybe.py"
    script.write_text(
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'decision': 'maybe'}))\n",
        encoding="utf-8",
    )
    decision = hooks.run(script, {"x": 1}, timeout_sec=1.0)
    assert decision.allowed is False
