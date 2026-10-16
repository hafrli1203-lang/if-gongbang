import json
import os
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "hooks", "next_task.sh")


def run_script(cfg: dict, cwd: str) -> str:
    env = dict(os.environ)
    cfg_path = os.path.join(cwd, "gongbang.config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    env["GONGBANG_CONFIG"] = cfg_path
    result = subprocess.run(["bash", SCRIPT], cwd=cwd, env=env, capture_output=True, text=True, timeout=10)
    return result.stdout


def test_alerts_are_shown_even_without_a_work_queue_file(tmp_path):
    alerts = tmp_path / "ALERTS.md"
    alerts.write_text("- [ ] 10-16 11:00 {\"kind\": \"service_failed\"}\n", encoding="utf-8")
    cfg = {
        "work_queue_path": str(tmp_path / "does-not-exist-WORK_QUEUE.md"),
        "alerts_path": str(alerts),
        "apps_root": str(tmp_path / "apps"),
    }
    out = run_script(cfg, str(tmp_path))
    assert "작업판 없음" in out
    assert "운영 경보" in out
    assert "service_failed" in out


def test_work_queue_is_shown_when_present(tmp_path):
    queue = tmp_path / "WORK_QUEUE.md"
    queue.write_text("## 전역 순서\n### [ ] G1 do the thing\n", encoding="utf-8")
    cfg = {
        "work_queue_path": str(queue),
        "alerts_path": str(tmp_path / "no-alerts.md"),
        "apps_root": str(tmp_path / "apps"),
    }
    out = run_script(cfg, str(tmp_path))
    assert "작업판 (자동" in out
    assert "G1" in out
