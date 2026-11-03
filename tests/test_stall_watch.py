# Generated from the private ops workspace at b94af62b97af64e2e58e8c102fa2538f066ae366; do not edit by hand (see harness/export_ops_public.py).
"""stall_watch.py 판정 시험 — 가짜 사본 3개(활발·30분 멈춤·완료)로 돌린다. 실제 git·systemd·office.sqlite는 쓰지 않는다."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stall_watch  # noqa: E402


def _make_copy(tmp_path: Path, app: str, name: str, file_age_min: float, created_age_min: float, done: bool = False) -> Path:
    wt_dir = tmp_path / app / name
    wt_dir.mkdir(parents=True)
    f = wt_dir / "note.txt"
    f.write_text("x", encoding="utf-8")
    now = time.time()
    file_mtime = now - file_age_min * 60
    os.utime(f, (file_mtime, file_mtime))
    created_at_epoch = now - created_age_min * 60
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(created_at_epoch)) + "+09:00"
    info = {"app": app, "name": name, "path": str(wt_dir), "units": [], "created_at": created_at}
    wt_json = wt_dir / ".wt.json"
    wt_json.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    os.utime(wt_json, (file_mtime, file_mtime))  # .wt.json 자체 mtime도 "최근에 손댔다"는 가짜 신호가 안 되게
    if done:
        done_marker = wt_dir / ".wt.done"
        done_marker.write_text(now_iso(), encoding="utf-8")
        os.utime(done_marker, (file_mtime, file_mtime))
    return wt_dir


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def test_active_copy_not_stalled(tmp_path):
    _make_copy(tmp_path, "demoapp", "active", file_age_min=2, created_age_min=10)
    jp = tmp_path / "demoapp" / "active" / ".wt.json"
    row = stall_watch.scan_one(jp, tmp_path / "no-such.sqlite", time.time())
    assert row is not None
    assert row["finished"] is False
    assert stall_watch.is_stalled(row, threshold_min=30) is False
    assert row["age_min"] < 30


def test_stalled_copy_detected(tmp_path):
    _make_copy(tmp_path, "demoapp", "stuck", file_age_min=45, created_age_min=90)
    jp = tmp_path / "demoapp" / "stuck" / ".wt.json"
    row = stall_watch.scan_one(jp, tmp_path / "no-such.sqlite", time.time())
    assert row is not None
    assert row["finished"] is False
    assert stall_watch.is_stalled(row, threshold_min=30) is True
    assert row["age_min"] > 30


def test_finished_copy_never_stalled(tmp_path):
    _make_copy(tmp_path, "demoapp", "done", file_age_min=500, created_age_min=600, done=True)
    jp = tmp_path / "demoapp" / "done" / ".wt.json"
    row = stall_watch.scan_one(jp, tmp_path / "no-such.sqlite", time.time())
    assert row is not None
    assert row["finished"] is True
    # 끝난 사본은 아무리 오래됐어도 "멈춤"으로 잡히지 않는다
    assert stall_watch.is_stalled(row, threshold_min=30) is False


def test_find_wt_jsons_lists_all_three(tmp_path):
    _make_copy(tmp_path, "demoapp", "active", file_age_min=2, created_age_min=10)
    _make_copy(tmp_path, "demoapp", "stuck", file_age_min=45, created_age_min=90)
    _make_copy(tmp_path, "demoapp", "done", file_age_min=500, created_age_min=600, done=True)
    found = stall_watch.find_wt_jsons(tmp_path)
    names = sorted(p.parent.name for p in found)
    assert names == ["active", "done", "stuck"]


def test_main_end_to_end_no_notify(tmp_path, monkeypatch, capsys):
    _make_copy(tmp_path, "demoapp", "active", file_age_min=2, created_age_min=10)
    _make_copy(tmp_path, "demoapp", "stuck", file_age_min=45, created_age_min=90)
    _make_copy(tmp_path, "demoapp", "done", file_age_min=500, created_age_min=600, done=True)
    monkeypatch.setenv("STALL_WATCH_WT_ROOT", str(tmp_path))
    monkeypatch.setenv("STALL_WATCH_DB", str(tmp_path / "no-such.sqlite"))
    monkeypatch.setenv("STALL_WATCH_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("STALL_WATCH_NO_NOTIFY", "1")
    stall_watch.WT_ROOT = tmp_path
    stall_watch.OFFICE_DB = tmp_path / "no-such.sqlite"
    stall_watch.STATE_FILE = tmp_path / "state.json"
    stall_watch.NO_NOTIFY = True
    rc = stall_watch.main(argv=[])
    out = capsys.readouterr().out
    assert rc == 0
    assert "멈춤 사본 1개" in out
    assert "stuck" in out and "active" in out and "done" in out
