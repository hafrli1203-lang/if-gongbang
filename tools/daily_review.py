#!/usr/bin/env python3
# Generated from the private ops workspace at b94af62b97af64e2e58e8c102fa2538f066ae366; do not edit by hand (see harness/export_ops_public.py).
"""밤 점검 24시간 분석 (2026-11-03, team-lead 지시 — 운영자 "매일 발전하는 프롬프트 두 줄": 둘째 줄은
"지난 24시간 로그를 분석해 고장·불편·비효율을 효과 순으로 제안, 매일"). `tools/self_improve.sh` 뒤에 붙여 돈다.

이 스크립트는 AI를 부르지 않는다(읽기만) — self_improve.sh의 claude -p 호출과는 다른, 결정적 집계다.
지난 24시간의 실제 신호만 센다:
  - the office app 작업(office.sqlite 읽기 전용): 상태·소요·resume_count(재시도)·owner_followup(운영자 재지시)·
    evidence_ok·report_format_ok
  - `notify-log.jsonl`에서 같은 경보 문구가 반복된 횟수(반복 = 고장이 안 고쳐지고 있다는 뜻)
  - `ops/ALERTS.md`의 열린(`- [ ]`) 항목
  - 지금 failed 상태인 systemd --user 유닛
  - `metrics/dev-units-<오늘>.json` vs 전날 — 있으면만

항목을 고장(가중치 3)·불편(가중치 2)·비효율(가중치 1) 세 묶음으로 모으고, 예상 효과(영향 건수 × 가중치)로
정렬해 상위 10개를 ① `reports/daily-review-<날짜>.md` ② WORK_QUEUE.md 「전역 순서」 맨 위(자율) 줄
(이미 같은 유닛·문구로 올라와 있으면 건너뜀) ③ 디스코드 #밤점검 표로 낸다.

근거 없는 항목은 올리지 않는다(건수 0이면 안 올림, team-lead 지시).

Run: python3 /apps/_ops/tools/daily_review.py [시간=24]
  환경변수(시험용): DAILY_REVIEW_DB, DAILY_REVIEW_NOTIFY_LOG, DAILY_REVIEW_ALERTS, DAILY_REVIEW_WORK_QUEUE,
                   DAILY_REVIEW_REPORTS, DAILY_REVIEW_NO_DISCORD=1, DAILY_REVIEW_NO_FAILED_UNITS=1
"""
from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path("/apps/_ops")
OFFICE_DB = Path(os.environ.get("DAILY_REVIEW_DB", "/data/exampleapp/office.sqlite"))
NOTIFY_LOG = Path(os.environ.get("DAILY_REVIEW_NOTIFY_LOG", "/data/exampleapp/notify-log.jsonl"))
ALERTS_MD = Path(os.environ.get("DAILY_REVIEW_ALERTS", str(ROOT / "ops" / "ALERTS.md")))
WORK_QUEUE = Path(os.environ.get("DAILY_REVIEW_WORK_QUEUE", str(ROOT / "WORK_QUEUE.md")))
REPORTS = Path(os.environ.get("DAILY_REVIEW_REPORTS", str(ROOT / "reports")))
NO_DISCORD = os.environ.get("DAILY_REVIEW_NO_DISCORD") == "1"
NO_FAILED_UNITS = os.environ.get("DAILY_REVIEW_NO_FAILED_UNITS") == "1"
NOTIFY_TABLE_MJS = str(ROOT / "tools" / "notify_discord_table.mjs")

WEIGHT = {"고장": 3, "불편": 2, "비효율": 1}  # 자가 점검 가중치(상수) — 영향 건수 × 가중치로만 정렬

_TMP_TAIL_RE = re.compile(r"(/tmp/tmp\.\w+|/tmp/[\w.-]{6,})")  # 임의 파일명 꼬리를 지워 같은 경보를 하나로 묶는다


def _parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _normalize_alert_text(text: str) -> str:
    """notify-log.jsonl 문구에서 임시 경로·시각 꼬리를 지워 "같은 경보 반복"을 셀 수 있게 한다."""
    t = _TMP_TAIL_RE.sub("<tmp>", text)
    t = re.sub(r"\d{1,2}:\d{2}(:\d{2})?", "<시각>", t)
    return t.strip()


def read_ax_os_tasks(db_path: Path, since_epoch: float) -> list[dict]:
    if not db_path.exists():
        return []
    try:
        con = __import__("sqlite3").connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except Exception:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT id, status, employee_name, created_at, started_at, ended_at, "
            "resume_count, owner_followup, evidence_ok, report_format_ok, error, source "
            "FROM tasks"
        )
        rows = cur.fetchall()
    finally:
        con.close()
    out = []
    for row in rows:
        created_epoch = _parse_iso(row[3])
        if created_epoch is None or created_epoch < since_epoch:
            continue
        out.append({
            "id": row[0], "status": row[1], "employee_name": row[2], "created_at": row[3],
            "started_at": row[4], "ended_at": row[5], "resume_count": row[6] or 0,
            "owner_followup": row[7] or 0, "evidence_ok": row[8], "report_format_ok": row[9],
            "error": row[10], "source": row[11],
        })
    return out


def read_notify_log(path: Path, since_epoch: float) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        epoch = _parse_iso(row.get("at"))
        if epoch is None or epoch < since_epoch:
            continue
        out.append(row)
    return out


def group_repeated_alerts(notify_rows: list[dict]) -> list[tuple[str, int]]:
    """같은 경보 문구가 2번 이상 반복됐으면 "안 고쳐지고 있는 고장"으로 묶는다."""
    counts: dict[str, int] = {}
    for row in notify_rows:
        text = row.get("text") or ""
        if not text:
            continue
        key = _normalize_alert_text(text)
        counts[key] = counts.get(key, 0) + 1
    return [(k, v) for k, v in counts.items() if v >= 2]


def read_open_alerts(path: Path) -> list[str]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("- [ ]"):
            out.append(line[len("- [ ] "):].strip())
    return out


def read_failed_units() -> list[str]:
    if NO_FAILED_UNITS:
        return []
    try:
        out = subprocess.run(
            ["systemctl", "--user", "list-units", "--state=failed", "--no-legend", "--plain"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    units = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if parts:
            units.append(parts[0])
    return units


def build_findings(hours: float) -> dict:
    now = time.time()
    since = now - hours * 3600

    tasks = read_ax_os_tasks(OFFICE_DB, since)
    notify_rows = read_notify_log(NOTIFY_LOG, since)
    repeated_alerts = group_repeated_alerts(notify_rows)
    open_alerts = read_open_alerts(ALERTS_MD)
    failed_units = read_failed_units()

    findings: list[dict] = []

    # 고장: 반복된 디스코드/카톡 경보 (실제로 반복 = 아직 안 고쳐짐)
    for key, count in sorted(repeated_alerts, key=lambda kv: -kv[1]):
        findings.append({
            "bucket": "고장", "title": key[:140], "count": count,
            "evidence": f"notify-log.jsonl 지난 {hours:g}시간 같은 문구 {count}회",
        })

    # 고장: ops/ALERTS.md 열린 항목(각 1건, 중복 유닛은 합산)
    alert_units: dict[str, int] = {}
    for a in open_alerts:
        m = re.search(r'"unit":\s*"([^"]+)"', a)
        key = m.group(1) if m else a[:80]
        alert_units[key] = alert_units.get(key, 0) + 1
    for key, count in alert_units.items():
        findings.append({
            "bucket": "고장", "title": f"ALERTS.md 미확인: {key}", "count": count,
            "evidence": f"ops/ALERTS.md `- [ ]` {count}건",
        })

    # 고장: 지금 failed 유닛
    for unit in failed_units:
        findings.append({
            "bucket": "고장", "title": f"서비스 failed: {unit}", "count": 1,
            "evidence": "systemctl --user list-units --state=failed",
        })

    # the office app 작업 기반 불편·비효율
    owner_followup_n = sum(1 for t in tasks if t["owner_followup"])
    if owner_followup_n:
        findings.append({
            "bucket": "불편", "title": "the office app 작업 중 운영자 재지시(owner_followup)", "count": owner_followup_n,
            "evidence": f"office.sqlite tasks owner_followup=1 지난 {hours:g}시간 {owner_followup_n}건",
        })
    format_bad_n = sum(1 for t in tasks if t["report_format_ok"] == 0)
    if format_bad_n:
        findings.append({
            "bucket": "불편", "title": "the office app 보고 형식 미비(report_format_ok=0)", "count": format_bad_n,
            "evidence": f"office.sqlite tasks report_format_ok=0 지난 {hours:g}시간 {format_bad_n}건",
        })
    evidence_bad_n = sum(1 for t in tasks if t["evidence_ok"] == 0)
    if evidence_bad_n:
        findings.append({
            "bucket": "불편", "title": "the office app 완료 증거 없음(evidence_ok=0)", "count": evidence_bad_n,
            "evidence": f"office.sqlite tasks evidence_ok=0 지난 {hours:g}시간 {evidence_bad_n}건",
        })
    resume_n = sum(1 for t in tasks if t["resume_count"] > 0)
    if resume_n:
        findings.append({
            "bucket": "비효율", "title": "the office app 작업 재시도(resume_count>0)", "count": resume_n,
            "evidence": f"office.sqlite tasks resume_count>0 지난 {hours:g}시간 {resume_n}건",
        })

    durations = []
    for t in tasks:
        s, e = _parse_iso(t["started_at"]), _parse_iso(t["ended_at"])
        if s is not None and e is not None and e > s:
            durations.append((t["id"], e - s))
    if len(durations) >= 3:
        median = statistics.median(d for _, d in durations)
        over = [tid for tid, d in durations if d > median * 2]
        if over:
            findings.append({
                "bucket": "비효율", "title": "the office app 작업 소요 시간이 중앙값의 2배 넘음", "count": len(over),
                "evidence": f"중앙값 {median:.0f}초, 초과 {len(over)}/{len(durations)}건",
            })

    for f in findings:
        f["score"] = f["count"] * WEIGHT[f["bucket"]]

    top10 = sorted(findings, key=lambda f: -f["score"])[:10]
    return {
        "since_hours": hours, "tasks_seen": len(tasks), "notify_rows_seen": len(notify_rows),
        "open_alerts_seen": len(open_alerts), "failed_units_seen": len(failed_units),
        "findings": findings, "top10": top10,
    }


def render_report_md(result: dict, day: str) -> str:
    lines = [f"# 밤 점검 24시간 분석 — {day}", ""]
    lines.append(
        f"입력: the office app 작업 {result['tasks_seen']}건 · 알림 로그 {result['notify_rows_seen']}건 · "
        f"ALERTS.md 열린 항목 {result['open_alerts_seen']}건 · failed 유닛 {result['failed_units_seen']}건"
    )
    lines.append("")
    for bucket in ("고장", "불편", "비효율"):
        items = [f for f in result["findings"] if f["bucket"] == bucket]
        lines.append(f"## {bucket} (가중치 {WEIGHT[bucket]})")
        if not items:
            lines.append("- 없음(건수 0)")
        for it in sorted(items, key=lambda f: -f["score"]):
            lines.append(f"- {it['title']} — 영향 {it['count']}건, 효과 {it['score']} ({it['evidence']})")
        lines.append("")
    lines.append("## 효과 순 상위 10")
    if not result["top10"]:
        lines.append("- 근거 있는 항목 없음")
    for i, it in enumerate(result["top10"], 1):
        lines.append(f"{i}. [{it['bucket']}·{it['score']}] {it['title']} ({it['evidence']})")
    return "\n".join(lines) + "\n"


def render_discord_table(result: dict, day: str) -> str:
    lines = [f"밤 점검 {day} — 효과 순 상위 10"]
    if not result["top10"]:
        lines.append("근거 있는 항목 없음")
    for i, it in enumerate(result["top10"], 1):
        lines.append(f"{i:2}. [{it['bucket']}·{it['score']:2}] {it['title'][:60]}")
    return "\n".join(lines)


_QUEUE_LINE_RE = re.compile(r"^- \[([ x~])\]\s*(.*)$")


def existing_work_queue_text(queue_path: Path) -> str:
    if not queue_path.exists():
        return ""
    return queue_path.read_text(encoding="utf-8", errors="replace")


def already_tracked(text: str, title: str) -> bool:
    """제목 안의 눈에 띄는 토큰(유닛 이름 등)이 이미 작업판 본문에 있으면 중복으로 보고 건너뛴다."""
    m = re.search(r"[\w.-]{6,}\.(service|timer)", title)
    token = m.group(0) if m else title[:30]
    return token in text


def append_work_queue_items(queue_path: Path, result: dict, day: str) -> list[str]:
    """`## 전역 순서 (위에서부터)` 줄 바로 다음에 새 (자율) 줄을 끼워 넣는다 — work_claim.py의 claim 표시
    문법(`- [ ] ... (자율) ...`)과 같은 모양만 쓰고, 다른 줄은 한 글자도 바꾸지 않는다."""
    if not queue_path.exists():
        return []
    text = queue_path.read_text(encoding="utf-8")
    heading = "## 전역 순서 (위에서부터)"
    idx = text.find(heading)
    if idx < 0:
        return []
    insert_at = idx + len(heading)
    added = []
    new_lines = []
    for it in result["top10"]:
        if already_tracked(text, it["title"]):
            continue
        line = f"\n- [ ] (자율) (밤 점검 {day}) {it['bucket']}: {it['title']} — {it['evidence']}"
        new_lines.append(line)
        added.append(it["title"])
    if not new_lines:
        return []
    new_text = text[:insert_at] + "".join(new_lines) + text[insert_at:]
    queue_path.write_text(new_text, encoding="utf-8")
    return added


def send_discord(table_text: str) -> str:
    if NO_DISCORD:
        return "건너뜀(DAILY_REVIEW_NO_DISCORD=1)"
    try:
        out = subprocess.run(
            ["node", NOTIFY_TABLE_MJS, "밤점검"],
            input=table_text, capture_output=True, text=True, timeout=20,
        )
        return (out.stdout or out.stderr or "").strip() or "응답 없음"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"실패: {exc}"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    hours = float(argv[0]) if argv else 24.0
    day = datetime.now().strftime("%Y-%m-%d")

    result = build_findings(hours)
    report_md = render_report_md(result, day)
    REPORTS.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS / f"daily-review-{day}.md"
    report_path.write_text(report_md, encoding="utf-8")

    added = append_work_queue_items(WORK_QUEUE, result, day)
    table_text = render_discord_table(result, day)
    discord_result = send_discord(table_text)

    print(report_md)
    print(f"작업판에 새로 올린 항목: {len(added)}개")
    print(f"디스코드 전송 결과: {discord_result}")
    print(f"기록: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
