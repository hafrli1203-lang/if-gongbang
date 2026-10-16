import json
from datetime import datetime, timezone

import hook_hits as hh


def write_session(tmp_path, project, records):
    d = tmp_path / project
    d.mkdir(parents=True, exist_ok=True)
    path = d / "session.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return path


def test_scope_guard_denial_is_counted(tmp_path):
    rec = {
        "type": "user", "timestamp": "2026-10-10T00:00:00Z",
        "message": {"content": [{
            "type": "tool_result", "is_error": True,
            "content": "PreToolUse:Bash hook error: 대표 승인 필요: git push",
        }]},
    }
    write_session(tmp_path, "demo", [rec])
    result = hh.scan(str(tmp_path), datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert result["blocks_by_hook"].get("scope_guard") == 1


def test_stop_block_is_counted(tmp_path):
    rec = {
        "type": "attachment", "timestamp": "2026-10-10T00:00:00Z",
        "attachment": {"type": "hook_blocking_error", "blockingError": "완료·통과·성공 보고에 이 턴의 실행 근거 없음"},
    }
    write_session(tmp_path, "demo", [rec])
    result = hh.scan(str(tmp_path), datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert result["blocks_by_hook"].get("verify_claims") == 1


def test_out_of_window_is_excluded(tmp_path):
    rec = {
        "type": "attachment", "timestamp": "2026-09-01T00:00:00Z",
        "attachment": {"type": "hook_blocking_error", "blockingError": "완료·통과·성공 보고에 이 턴의 실행 근거 없음"},
    }
    write_session(tmp_path, "demo", [rec])
    result = hh.scan(str(tmp_path), datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert result["blocks_by_hook"] == {}
