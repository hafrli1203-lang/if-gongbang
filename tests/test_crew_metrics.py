# Generated from the private ops workspace at b94af62b97af64e2e58e8c102fa2538f066ae366; do not edit by hand (see harness/export_ops_public.py).
"""crew_metrics.py 시험 (2026-11-02 team-lead 지시, 03:30 자가 점검 지적 보정):
① 분모가 "앱 파일을 실제로 고친 작업조"인지 ② DB 쓰기 정규식이 문자열 언급(cat으로 파일
읽기, created_at/updated_at 같은 컬럼명)을 "쓰기"로 잡지 않고 실행 명령만 잡는지.
엔진 venv로 돌림: /apps/if-gongbang-v2/.venv/bin/python -m pytest tools/tests/test_crew_metrics.py
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)

_saved_argv = sys.argv
sys.argv = ["crew_metrics.py"]  # module reads sys.argv at import time -- keep it plain
import crew_metrics as cm  # noqa: E402
sys.argv = _saved_argv


# ---- is_db_write_command: 실행인지 문자열 언급인지 ----

def test_db_write_real_migrate_execution_is_risk():
    assert cm.is_db_write_command("cd /apps/exampleapp && npm run migrate 2>&1 | tail -40") is True


def test_db_write_real_seed_execution_is_risk():
    assert cm.is_db_write_command('npm run seed -- user1 pass "x" "y"') is True


def test_db_write_psql_update_is_risk():
    assert cm.is_db_write_command('psql -tAc "UPDATE tasks SET status=\'done\' WHERE id=1"') is True


def test_db_write_psql_create_table_is_risk():
    assert cm.is_db_write_command('psql -c "CREATE TABLE x (id int)"') is True


def test_cat_migrate_file_is_not_risk():
    """2026-11-02 지적: 파일을 그냥 읽은 것(cat)인데 "scripts/migrate.ts"라는 이름만 보고 잡던 오탐."""
    assert cm.is_db_write_command("cat scripts/migrate.ts") is False


def test_select_with_created_at_column_is_not_risk():
    """2026-11-02 지적: created_at/updated_at 컬럼명의 "create"/"update" 부분 문자열을
    쓰기 명령으로 잘못 잡던 오탐 (\\b 경계 추가로 고침)."""
    cmd = 'echo "select count(*) from lessons where created_at > now() - interval \'24 hours\';" > /tmp/n.sql; bash metrics/psql_os.sh /tmp/n.sql'
    assert cm.is_db_write_command(cmd) is False


def test_select_with_updated_at_column_is_not_risk():
    assert cm.is_db_write_command('psql -c "select id, updated_at from tasks limit 1"') is False


def test_grep_for_drop_table_text_is_not_risk():
    assert cm.is_db_write_command("grep -rn 'DROP TABLE' migrations/") is False


def test_heredoc_body_regex_literal_is_not_risk():
    """2026-11-03 실측(adev-metrics-5008007de1559b63): heredoc 안 Python 소스의 regex
    문자열 `...|npm run seed|...`이 `|`로 쪼개져 "npm run seed" 조각이 실행 명령으로
    잘못 잡혔다. heredoc 본문은 명령으로 보지 않아야 한다."""
    cmd = (
        "python3 - \"$F\" <<'EOF'\n"
        "import re\n"
        "DB_WRITE_OLD = re.compile(r\"(npm run (-s )?(db:)?migrate|scripts/migrate\\.ts|db:migrate|npm run seed|psql[^\\n]*(insert))\")\n"
        "EOF\n"
    )
    assert cm.is_db_write_command(cmd) is False


def test_heredoc_body_commit_message_mentioning_migrate_is_not_risk():
    """같은 사고의 또 다른 모양: git commit -m 안 heredoc 커밋 메시지 글(설명문)에
    "npm run migrate/seed 실행"이라는 말이 들어 있어도 실행한 것이 아니다."""
    cmd = (
        "git commit -m \"$(cat <<'EOF'\n"
        "fix: ... stageA-1(/apps/exampleapp에서 직접 npm run migrate/seed 실행, 진짜 사고)\n"
        "EOF\n"
        ")\" -- tools/crew_metrics.py"
    )
    assert cm.is_db_write_command(cmd) is False


# ---- APP_PATH: "앱 파일" 범위 ----

def test_app_path_matches_app_folder():
    assert cm.APP_PATH.search("/apps/exampleapp/src/x.ts")


def test_app_path_matches_worktree_copy():
    assert cm.APP_PATH.search("/apps/_wt/exampleapp/fence-probe/src/x.ts")


def test_app_path_excludes_ops_dir():
    assert not cm.APP_PATH.search("/apps/_ops/metrics/dev_units.py")


def test_app_path_excludes_shared_tooling():
    assert not cm.APP_PATH.search("/apps/_shared/deploy.sh")


# ---- scan(): 분모가 될 app_file_edit 신호 ----

def write_transcript(path: str, tool_uses: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for tu in tool_uses:
            row = {"message": {"role": "assistant", "content": [{"type": "tool_use", "name": tu["name"], "input": tu["input"]}]}}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_scan_flags_app_file_edit_on_edit_tool(tmp_path):
    p = tmp_path / "agent-fix1-abc.jsonl"
    write_transcript(str(p), [{"name": "Edit", "input": {"file_path": "/apps/exampleapp/src/x.ts"}}])
    m = cm.scan(str(p))
    assert m["app_file_edit"] is True


def test_scan_does_not_flag_app_file_edit_for_research_only(tmp_path):
    """general-purpose/fork/Explore류: v2 대조·판정표만 읽고 쓰는 일 -- 앱 파일을 고치지 않음."""
    p = tmp_path / "agent-audit1-abc.jsonl"
    write_transcript(str(p), [
        {"name": "Read", "input": {"file_path": "/apps/_ops/plans/2026-11-02-v2-gap-verdicts.md"}},
        {"name": "Write", "input": {"file_path": "/apps/_ops/reports/audit-out.md"}},
    ])
    m = cm.scan(str(p))
    assert m["app_file_edit"] is False


def test_scan_flags_app_file_edit_on_git_commit_in_bash(tmp_path):
    p = tmp_path / "agent-fix2-abc.jsonl"
    write_transcript(str(p), [{"name": "Bash", "input": {"command": "git -C /apps/_wt/exampleapp/x commit -m msg -- /apps/_wt/exampleapp/x/src/a.ts"}}])
    m = cm.scan(str(p))
    assert m["app_file_edit"] is True


def test_scan_prod_db_risk_excludes_test_db_hint(tmp_path):
    p = tmp_path / "agent-fix3-abc.jsonl"
    write_transcript(str(p), [{"name": "Bash", "input": {"command": "DATABASE_URL=exampleapp_test npm run migrate"}}])
    m = cm.scan(str(p))
    assert m["prod_db_risk"] == 0


def test_scan_prod_db_risk_counts_real_prod_write(tmp_path):
    p = tmp_path / "agent-fix4-abc.jsonl"
    write_transcript(str(p), [{"name": "Bash", "input": {"command": "cd /apps/exampleapp && npm run migrate"}}])
    m = cm.scan(str(p))
    assert m["prod_db_risk"] == 1
