import json
import os
from datetime import datetime, timezone

import corrections as corr


def write_session(tmp_path, project, records):
    d = tmp_path / project
    d.mkdir(parents=True, exist_ok=True)
    path = d / "session.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return path


def user_record(text, ts):
    return {"type": "user", "entrypoint": "cli", "timestamp": ts, "message": {"content": text}}


def test_owner_text_filters_tool_results():
    rec = {"type": "user", "entrypoint": "cli",
            "message": {"content": [{"type": "tool_result", "content": "x"}]}}
    assert corr.owner_text(rec) is None


def test_scan_counts_corrections(tmp_path):
    write_session(tmp_path, "demo", [
        user_record("아니 그거 하지 마", "2026-10-10T00:00:00Z"),
        user_record("다음 기능 추가해줘", "2026-10-10T00:01:00Z"),
    ])
    since = datetime(2026, 10, 1, tzinfo=timezone.utc)
    until = datetime(2026, 10, 20, tzinfo=timezone.utc)
    result = corr.scan(str(tmp_path), since, until)
    assert result["total_owner_messages"] == 2
    assert result["correction_hits"] == 1


def test_scan_respects_window(tmp_path):
    write_session(tmp_path, "demo", [user_record("아니 하지 마", "2026-09-01T00:00:00Z")])
    since = datetime(2026, 10, 1, tzinfo=timezone.utc)
    until = datetime(2026, 10, 20, tzinfo=timezone.utc)
    result = corr.scan(str(tmp_path), since, until)
    assert result["total_owner_messages"] == 0
