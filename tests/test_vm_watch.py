import json

import vm_watch


def test_run_writes_log_and_state(tmp_path, monkeypatch):
    # Stub out every subprocess-backed probe so this test has no dependency on the real OS.
    monkeypatch.setattr(vm_watch, "freezes", lambda since, unit: [])
    monkeypatch.setattr(vm_watch, "unit_problems", lambda glob_: [])
    monkeypatch.setattr(vm_watch, "resources", lambda: [])
    monkeypatch.setattr(vm_watch, "app_problems", lambda glob_, probe, prev: ([], {}))

    cfg = {"service_unit_glob": "app-*.service", "health_probe_path": "/__ops_probe",
           "time_sync_unit": "systemd-timesyncd"}
    state_path = tmp_path / "state.json"
    log_path = tmp_path / "log.jsonl"
    alerts_path = tmp_path / "ALERTS.md"

    rec = vm_watch.run(cfg, str(state_path), str(log_path), str(alerts_path))
    assert rec["findings"] == []
    assert log_path.exists()
    state = json.loads(state_path.read_text())
    assert "last" in state


def test_run_writes_alert_for_new_finding(tmp_path, monkeypatch):
    monkeypatch.setattr(vm_watch, "freezes", lambda since, unit: [])
    monkeypatch.setattr(vm_watch, "unit_problems", lambda glob_: [{"kind": "service_failed", "unit": "app-x.service"}])
    monkeypatch.setattr(vm_watch, "resources", lambda: [])
    monkeypatch.setattr(vm_watch, "app_problems", lambda glob_, probe, prev: ([], {}))

    cfg = {"service_unit_glob": "app-*.service", "health_probe_path": "/__ops_probe",
           "time_sync_unit": "systemd-timesyncd"}
    state_path = tmp_path / "state.json"
    log_path = tmp_path / "log.jsonl"
    alerts_path = tmp_path / "ALERTS.md"

    vm_watch.run(cfg, str(state_path), str(log_path), str(alerts_path))
    assert alerts_path.exists()
    assert "service_failed" in alerts_path.read_text()
