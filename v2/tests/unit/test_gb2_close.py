"""`gb2 close` (foreman close,: refusal paths need no live
engine -- not initialized, no attempt, already CLOSED."""
import argparse
import json
from pathlib import Path

import pytest

import sys as _sys
from pathlib import Path as _Path
_TOOLS = str(_Path(__file__).resolve().parents[2] / "tools")
if _TOOLS not in _sys.path:
    _sys.path.insert(0, _TOOLS)

from gb2lib import runner, shared


def _ns(app: str, reason=None) -> argparse.Namespace:
    return argparse.Namespace(app=app, reason=reason)


def test_close_refuses_uninitialized_app(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(shared, "app_workspace", lambda app: tmp_path / "apps" / app)
    rc = runner.cmd_close(_ns("cafe-site"))
    assert rc == 1
    out = capsys.readouterr()
    assert "not initialized" in (out.err + out.out)


def test_close_refuses_when_already_closed(monkeypatch, tmp_path: Path, capsys) -> None:
    class Row:
        current_attempt_id = "J-academy-01#3"
        state = "CLOSED"

    class FakeLedger:
        @staticmethod
        def open(path):
            return FakeLedger()

        def project(self, app):
            return Row()

    class FakeConfig:
        ledger_path = tmp_path / "ledger.sqlite"

        @staticmethod
        def from_json(path):
            return FakeConfig()

    ws = tmp_path / "apps" / "academy"
    ws.mkdir(parents=True)
    (ws / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(shared, "app_workspace", lambda app: ws)
    monkeypatch.setattr(runner, "EngineConfig", FakeConfig)
    monkeypatch.setattr(runner, "Ledger", FakeLedger)
    rc = runner.cmd_close(_ns("academy"))
    assert rc == 1
    captured = capsys.readouterr()
    assert "already CLOSED" in (captured.err + captured.out)
