# Generated from the private ops workspace at 24c1bb68758c8a552514d06d8280463876278f76; do not edit by hand (see harness/export_ops_public.py).
"""Tests for tools/wt_doctor.py -- the environment pre-flight checker (묶음 8 #29)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent / "tools"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


wd = _load("wt_doctor", TOOLS / "wt_doctor.py")


def test_cmd_present_true_for_known_binary() -> None:
    name, ok, detail = wd._cmd_present("ls")
    assert name == "ls"
    assert ok is True
    assert detail.endswith("/ls")


def test_cmd_present_false_for_unknown_binary() -> None:
    name, ok, detail = wd._cmd_present("this-binary-does-not-exist-xyz")
    assert ok is False
    assert "PATH" in detail


def test_file_exists_true(tmp_path) -> None:
    p = tmp_path / "x.md"
    p.write_text("x", encoding="utf-8")
    name, ok, detail = wd._file_exists("label", p)
    assert ok is True
    assert detail == "ok"


def test_file_exists_false_reports_path(tmp_path) -> None:
    p = tmp_path / "missing.md"
    name, ok, detail = wd._file_exists("label", p)
    assert ok is False
    assert str(p) in detail


def test_pstack_links_ok_when_all_present(tmp_path, monkeypatch) -> None:
    skills = tmp_path / ".claude" / "skills"
    skills.mkdir(parents=True)
    for s in wd.PSTACK_LINKED_SKILLS:
        (skills / s).mkdir()
    monkeypatch.setattr(wd, "HOME", tmp_path)
    _name, ok, _detail = wd._pstack_links()
    assert ok is True


def test_pstack_links_fail_lists_missing(tmp_path, monkeypatch) -> None:
    skills = tmp_path / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / wd.PSTACK_LINKED_SKILLS[0]).mkdir()
    monkeypatch.setattr(wd, "HOME", tmp_path)
    _name, ok, detail = wd._pstack_links()
    assert ok is False
    assert wd.PSTACK_LINKED_SKILLS[1] in detail


def test_rail_agents_ok_when_all_present(tmp_path, monkeypatch) -> None:
    agents = tmp_path / ".claude" / "agents"
    agents.mkdir(parents=True)
    for a in wd.RAIL_AGENTS:
        (agents / f"{a}.md").write_text("", encoding="utf-8")
    monkeypatch.setattr(wd, "HOME", tmp_path)
    _name, ok, _detail = wd._rail_agents()
    assert ok is True


def test_rail_agents_fail_lists_missing(tmp_path, monkeypatch) -> None:
    agents = tmp_path / ".claude" / "agents"
    agents.mkdir(parents=True)
    monkeypatch.setattr(wd, "HOME", tmp_path)
    _name, ok, detail = wd._rail_agents()
    assert ok is False
    assert wd.RAIL_AGENTS[0] in detail


def test_hooks_wired_true_when_hooks_present(tmp_path, monkeypatch) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [], "Stop": []}}), encoding="utf-8")
    monkeypatch.setattr(wd, "SETTINGS_JSON", settings)
    _name, ok, detail = wd._hooks_wired()
    assert ok is True
    assert "PreToolUse" in detail


def test_hooks_wired_false_when_empty(tmp_path, monkeypatch) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    monkeypatch.setattr(wd, "SETTINGS_JSON", settings)
    _name, ok, _detail = wd._hooks_wired()
    assert ok is False


def test_hooks_wired_false_when_missing_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(wd, "SETTINGS_JSON", tmp_path / "missing.json")
    _name, ok, detail = wd._hooks_wired()
    assert ok is False
    assert "없음" in detail


def test_apps_with_facts_counts_cards(tmp_path, monkeypatch) -> None:
    facts = tmp_path / "app-facts"
    facts.mkdir()
    (facts / "a.md").write_text("", encoding="utf-8")
    (facts / "b.md").write_text("", encoding="utf-8")
    monkeypatch.setattr(wd, "FACTS_DIR", facts)
    _name, ok, detail = wd._apps_with_facts()
    assert ok is True
    assert "2개" in detail


def test_checks_returns_nonempty_list_of_triples() -> None:
    results = wd.checks()
    assert len(results) > 0
    for name, ok, detail in results:
        assert isinstance(name, str)
        assert isinstance(ok, bool)
        assert isinstance(detail, str)


def test_main_exit_code_matches_fail_presence(monkeypatch, capsys) -> None:
    monkeypatch.setattr(wd, "checks", lambda: [("a", True, "ok"), ("b", False, "no")])
    rc = wd.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out
    assert "FAIL 1개" in out


def test_main_exit_zero_when_all_ok(monkeypatch) -> None:
    monkeypatch.setattr(wd, "checks", lambda: [("a", True, "ok")])
    assert wd.main() == 0
