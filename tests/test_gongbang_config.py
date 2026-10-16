import json
import os

import gongbang_config as gc


def test_load_defaults_are_absolute_paths(tmp_path, monkeypatch):
    monkeypatch.delenv("GONGBANG_CONFIG", raising=False)
    cfg = gc.load(start_dir=str(tmp_path))  # no gongbang.config.json anywhere above tmp_path
    for key in gc.PATH_KEYS:
        assert os.path.isabs(cfg[key]), key


def test_relative_apps_root_override_is_made_absolute(tmp_path, monkeypatch):
    cfg_path = tmp_path / "gongbang.config.json"
    cfg_path.write_text(json.dumps({"apps_root": "relative/apps"}), encoding="utf-8")
    monkeypatch.setenv("GONGBANG_CONFIG", str(cfg_path))
    cfg = gc.load()
    assert os.path.isabs(cfg["apps_root"])


def test_null_installed_hooks_dir_override_falls_back_to_default(tmp_path, monkeypatch):
    cfg_path = tmp_path / "gongbang.config.json"
    cfg_path.write_text(json.dumps({"installed_hooks_dir": None}), encoding="utf-8")
    monkeypatch.setenv("GONGBANG_CONFIG", str(cfg_path))
    cfg = gc.load()
    assert cfg["installed_hooks_dir"]  # non-empty, did not crash
    assert os.path.isabs(cfg["installed_hooks_dir"])


def test_legacy_gate_dir_empty_string_disables_it(tmp_path, monkeypatch):
    cfg_path = tmp_path / "gongbang.config.json"
    cfg_path.write_text(json.dumps({"legacy_gate_dir": ""}), encoding="utf-8")
    monkeypatch.setenv("GONGBANG_CONFIG", str(cfg_path))
    cfg = gc.load()
    assert cfg["legacy_gate_dir"] == ""


def test_legacy_gate_dir_default_is_absolute(tmp_path, monkeypatch):
    monkeypatch.delenv("GONGBANG_CONFIG", raising=False)
    cfg = gc.load(start_dir=str(tmp_path))
    assert os.path.isabs(cfg["legacy_gate_dir"])


def test_empty_string_override_falls_back_to_default(tmp_path, monkeypatch):
    cfg_path = tmp_path / "gongbang.config.json"
    cfg_path.write_text(json.dumps({"apps_root": ""}), encoding="utf-8")
    monkeypatch.setenv("GONGBANG_CONFIG", str(cfg_path))
    cfg = gc.load()
    assert cfg["apps_root"]


def test_malformed_json_falls_back_to_defaults(tmp_path, monkeypatch):
    cfg_path = tmp_path / "gongbang.config.json"
    cfg_path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("GONGBANG_CONFIG", str(cfg_path))
    cfg = gc.load()
    assert cfg["preflight_mode"] == "warn"


def test_valid_override_is_applied(tmp_path, monkeypatch):
    cfg_path = tmp_path / "gongbang.config.json"
    cfg_path.write_text(json.dumps({"preflight_mode": "block"}), encoding="utf-8")
    monkeypatch.setenv("GONGBANG_CONFIG", str(cfg_path))
    cfg = gc.load()
    assert cfg["preflight_mode"] == "block"


def test_type_mismatched_override_is_ignored(tmp_path, monkeypatch):
    cfg_path = tmp_path / "gongbang.config.json"
    cfg_path.write_text(json.dumps({"preflight_mode": 123}), encoding="utf-8")
    monkeypatch.setenv("GONGBANG_CONFIG", str(cfg_path))
    cfg = gc.load()
    assert cfg["preflight_mode"] == "warn"


def test_extra_protected_hook_names_defaults_empty():
    cfg = gc.load(start_dir="/nonexistent-path-for-test")
    assert cfg["extra_protected_hook_names"] == []


def test_run_with_deadline_returns_value_on_success():
    assert gc.run_with_deadline(1.0, lambda: 42) == 42


def test_run_with_deadline_returns_default_on_exception():
    def boom():
        raise RuntimeError("x")
    assert gc.run_with_deadline(1.0, boom, default="fallback") == "fallback"


def test_run_with_deadline_times_out_long_running_call():
    import time
    result = gc.run_with_deadline(0.05, lambda: time.sleep(2), default="timed-out")
    assert result == "timed-out"
