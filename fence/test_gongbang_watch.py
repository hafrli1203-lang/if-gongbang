"""Tests for apply_gongbang_watch.py -- file-writing part only (--dry-run never calls systemctl,
since enabling a real service is forbidden for the AI; the owner runs the real install)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APPLY = HERE / "apply_gongbang_watch.py"
UNIT_SOURCE = HERE / "ifapp-gongbang-watch.service"


def _run(fake_home: Path, *args: str) -> subprocess.CompletedProcess:
    import os
    env = {**os.environ, "GONGBANG_WATCH_HOME": str(fake_home)}
    return subprocess.run([sys.executable, str(APPLY), *args], capture_output=True, text=True,
                          timeout=20, env=env)


def test_unit_source_mentions_gb2_watch_loop_resume() -> None:
    text = UNIT_SOURCE.read_text(encoding="utf-8")
    assert "tools/gb2 watch --loop --resume" in text
    assert "GB2_APPS_ROOT=<apps-root>" in text
    assert "GB2_WORKSPACE=<apps-root>/live-workspace" in text


def test_dry_run_install_writes_unit_without_systemctl(tmp_path: Path) -> None:
    res = _run(tmp_path, "--dry-run")
    assert res.returncode == 0, res.stderr
    target = tmp_path / ".config" / "systemd" / "user" / "ifapp-gongbang-watch.service"
    assert target.exists()
    assert "gb2 watch --loop --resume" in target.read_text(encoding="utf-8")
    assert "systemctl" not in res.stdout.lower() or "생략" in res.stdout


def test_dry_run_undo_removes_unit_file(tmp_path: Path) -> None:
    _run(tmp_path, "--dry-run")
    target = tmp_path / ".config" / "systemd" / "user" / "ifapp-gongbang-watch.service"
    assert target.exists()
    res = _run(tmp_path, "--undo", "--dry-run")
    assert res.returncode == 0, res.stderr
    assert not target.exists()


def test_check_on_empty_home_reports_missing(tmp_path: Path) -> None:
    res = _run(tmp_path, "--check")
    assert res.returncode == 0
    assert "없음" in res.stdout
