# Generated from the private ops workspace at b94af62b97af64e2e58e8c102fa2538f066ae366; do not edit by hand (see harness/export_ops_public.py).
"""app_facts.py 시험 (자가 점검 2026-11-03):
① 지난 실수가 카드에 "번호: 다음부터 규칙" 한 줄씩 들어가고, 조회 실패면 직접 부르라고 적는지
② sqlite 앱·파일 저장 앱·손으로 DATABASE_URL을 넣는 시험이 있는 앱을 "시험 DB 단서 없음"과 구분하는지.
엔진 venv로 돌림: /apps/if-gongbang-v2/.venv/bin/python -m pytest tools/tests/test_app_facts.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import app_facts as af  # noqa: E402

SAMPLE = (
    "[L1-0242 · 경험·아직 기준 아님] 장면 문장. / 이유 문장. / 다음부터: 출처를 못 찾은 사실은 확인 필요로만 쓴다.\n"
    "\"운영자 원문\"\n"
    "[L1-0233 · 경험·아직 기준 아님] 장면. / 이유. / 다음부터: 다음부터: 화면에 영어 내부 이름을 쓰지 않는다.\n"
    "\"원문 둘\""
)


def test_summarize_lessons_keeps_id_and_rule_only():
    items = af.summarize_lessons(SAMPLE)
    assert items == [
        "- L1-0242: 출처를 못 찾은 사실은 확인 필요로만 쓴다.",
        "- L1-0233: 화면에 영어 내부 이름을 쓰지 않는다.",
    ]


def test_summarize_lessons_caps_long_rule():
    long = "[L1-0001 · 경험] a / b / 다음부터: " + "가" * 400
    (item,) = af.summarize_lessons(long)
    assert item.endswith("…") and len(item) < 250


def test_lessons_section_failure_and_empty():
    assert "자동 조회 실패" in af.lessons_section("x", None)[1]
    assert "자동 조회 실패" in af.lessons_section("x", {})[1]
    assert af.lessons_section("x", {"x": []})[1] == "- 기록된 지난 실수 없음."


def _app(tmp: Path, deps: dict, files: dict[str, str]) -> Path:
    (tmp / "package.json").write_text(json.dumps({"dependencies": deps}))
    for rel, txt in files.items():
        f = tmp / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(txt)
    return tmp


def test_sqlite_app_with_temp_tests(tmp_path):
    d = _app(tmp_path, {"better-sqlite3": "1"}, {"tests/unit/a.test.js": "fs.mkdtempSync(x)"})
    hint = af.test_db_hint(d)
    assert hint.startswith("sqlite 앱") and "시험 파일 1개" in hint


def test_pg_app_with_env_gated_tests_is_flagged(tmp_path):
    d = _app(tmp_path, {"pg": "1"}, {"src/lib/x.integration.test.ts":
                                     "const runIf = process.env.DATABASE_URL ? describe : describe.skip;"})
    assert "가드 없음" in af.test_db_hint(d)


def test_file_storage_app_with_temp_setup(tmp_path):
    d = _app(tmp_path, {}, {"vitest.global-setup.ts": "await fs.mkdtemp(p)"})
    assert af.test_db_hint(d).startswith("파일 저장 앱")


def test_named_test_db_still_wins(tmp_path):
    d = _app(tmp_path, {"pg": "1"}, {"vitest.config.ts": "TEST_DATABASE_URL", "tests/setup.ts": "checkTestDbUrl()"})
    hint = af.test_db_hint(d)
    assert hint.startswith("시험 DB 단서") and "가드 있음" in hint
