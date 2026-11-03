# Generated from the private ops workspace at b94af62b97af64e2e58e8c102fa2538f066ae366; do not edit by hand (see harness/export_ops_public.py).
"""daily_review.py 시험 — 가짜 office.sqlite·notify-log.jsonl·ALERTS.md로 집계·정렬·중복 제외를 확인한다."""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import daily_review  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _make_db(tmp_path: Path, rows: list[dict]) -> Path:
    db = tmp_path / "office.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE tasks (id TEXT, status TEXT, employee_name TEXT, created_at TEXT, "
        "started_at TEXT, ended_at TEXT, resume_count INTEGER, owner_followup INTEGER, "
        "evidence_ok INTEGER, report_format_ok INTEGER, error TEXT, source TEXT)"
    )
    for r in rows:
        con.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r.get("id", "t"), r.get("status", "done"), r.get("employee_name", "현상"),
                r.get("created_at"), r.get("started_at"), r.get("ended_at"),
                r.get("resume_count", 0), r.get("owner_followup", 0),
                r.get("evidence_ok"), r.get("report_format_ok"), r.get("error"), r.get("source"),
            ),
        )
    con.commit()
    con.close()
    return db


def test_group_repeated_alerts_counts_same_text_normalized():
    rows = [
        {"text": "#경보 sdstuck/w1 자체 배포 복구 실패 — 기록 /tmp/tmp.3iAbC"},
        {"text": "#경보 sdstuck/w1 자체 배포 복구 실패 — 기록 /tmp/tmp.XyZ99"},
        {"text": "#경보 다른 문제"},
    ]
    grouped = daily_review.group_repeated_alerts(rows)
    assert len(grouped) == 1
    key, count = grouped[0]
    assert count == 2
    assert "sdstuck/w1" in key


def test_open_alerts_reads_only_unchecked_lines(tmp_path):
    alerts = tmp_path / "ALERTS.md"
    alerts.write_text(
        '- [x] 11-02 00:00 {"kind": "done_one"}\n'
        '- [ ] 11-03 04:39 {"kind": "service_failed", "unit": "ifapp-foo.service"}\n'
        '- [ ] 11-03 07:35 {"kind": "service_failed", "unit": "ifapp-foo.service"}\n',
        encoding="utf-8",
    )
    open_items = daily_review.read_open_alerts(alerts)
    assert len(open_items) == 2
    assert all("ifapp-foo.service" in a for a in open_items)


def test_build_findings_zero_evidence_not_included(tmp_path, monkeypatch):
    db = _make_db(tmp_path, [])
    monkeypatch.setattr(daily_review, "OFFICE_DB", db)
    monkeypatch.setattr(daily_review, "NOTIFY_LOG", tmp_path / "no-such.jsonl")
    monkeypatch.setattr(daily_review, "ALERTS_MD", tmp_path / "no-such.md")
    monkeypatch.setattr(daily_review, "NO_FAILED_UNITS", True)
    result = daily_review.build_findings(24)
    assert result["findings"] == []
    assert result["top10"] == []


def test_build_findings_ranks_by_score(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    rows = [
        {"id": "a", "created_at": _iso(now - timedelta(hours=1)), "owner_followup": 1},
        {"id": "b", "created_at": _iso(now - timedelta(hours=1)), "owner_followup": 1},
        {"id": "c", "created_at": _iso(now - timedelta(hours=1)), "resume_count": 1},
    ]
    db = _make_db(tmp_path, rows)
    monkeypatch.setattr(daily_review, "OFFICE_DB", db)
    monkeypatch.setattr(daily_review, "NOTIFY_LOG", tmp_path / "no-such.jsonl")
    monkeypatch.setattr(daily_review, "ALERTS_MD", tmp_path / "no-such.md")
    monkeypatch.setattr(daily_review, "NO_FAILED_UNITS", True)
    result = daily_review.build_findings(24)
    assert result["tasks_seen"] == 3
    # 불편(owner_followup, count=2, weight=2 -> score 4) 이 비효율(resume, count=1, weight=1 -> score 1)보다 위
    assert result["top10"][0]["bucket"] == "불편"
    assert result["top10"][0]["score"] == 4


def test_old_tasks_outside_window_excluded(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    rows = [{"id": "old", "created_at": _iso(now - timedelta(hours=48)), "owner_followup": 1}]
    db = _make_db(tmp_path, rows)
    monkeypatch.setattr(daily_review, "OFFICE_DB", db)
    monkeypatch.setattr(daily_review, "NOTIFY_LOG", tmp_path / "no-such.jsonl")
    monkeypatch.setattr(daily_review, "ALERTS_MD", tmp_path / "no-such.md")
    monkeypatch.setattr(daily_review, "NO_FAILED_UNITS", True)
    result = daily_review.build_findings(24)
    assert result["tasks_seen"] == 0
    assert result["findings"] == []


def test_append_work_queue_items_skips_already_tracked(tmp_path):
    queue = tmp_path / "WORK_QUEUE.md"
    queue.write_text(
        "# WORK_QUEUE\n\n## 전역 순서 (위에서부터)\n\n"
        "- [ ] (자율) 이미 있음: ifapp-foo.service 어쩌구\n",
        encoding="utf-8",
    )
    result = {"top10": [
        {"bucket": "고장", "title": "서비스 failed: ifapp-foo.service", "count": 1, "evidence": "e", "score": 3},
        {"bucket": "고장", "title": "서비스 failed: ifapp-bar.service", "count": 1, "evidence": "e", "score": 3},
    ]}
    added = daily_review.append_work_queue_items(queue, result, "2026-11-03")
    assert added == ["서비스 failed: ifapp-bar.service"]
    text = queue.read_text(encoding="utf-8")
    assert text.count("ifapp-bar.service") == 1
    assert "밤 점검 2026-11-03" in text
    # 기존 줄은 그대로(이미 있던 foo 줄 안 중복)
    assert text.count("ifapp-foo.service") == 1


def test_render_report_md_marks_empty_bucket(tmp_path):
    result = {
        "since_hours": 24, "tasks_seen": 0, "notify_rows_seen": 0, "open_alerts_seen": 0,
        "failed_units_seen": 0, "findings": [], "top10": [],
    }
    md = daily_review.render_report_md(result, "2026-11-03")
    assert "건수 0" in md
    assert "근거 있는 항목 없음" in md
