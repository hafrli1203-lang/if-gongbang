#!/usr/bin/env python3
# Generated from the private ops workspace at 24c1bb68758c8a552514d06d8280463876278f76; do not edit by hand (see harness/export_ops_public.py).
"""멈춘 작업 사본 감시 (2026-11-03, team-lead 지시 — 운영자 "왜 작업을 안했는지 확인해.
지금 이것도 심각한 오류다"(L1-0261): 반장이 작업조 보고만 기다려 00:30~07:46 멈춘 사고 재발 방지).

`/apps/_wt/<앱>/<이름>` 마다 다음을 모아 "진행 중인데 N분(기본 30) 동안 아무 활동이 없는 사본"을 찾는다:
  ① 마지막 커밋 시각(git log -1)
  ② 그 사본 안 파일 mtime 최대(.git·node_modules·빌드 산출물 제외)
  ③ `.wt.json`의 `task_id`가 있으면 the office app 작업 상태(office.sqlite, 읽기 전용)
  ④ `.wt.json`의 `units`에 적힌 systemd --user 유닛이 지금 살아 있는지

읽기만 한다 — 아무것도 고치거나 끄지 않는다. `.wt.done`이 있으면 끝난 사본으로 보고 감시 대상에서 뺀다
(wt-lib.sh `wt_active_dirs`와 같은 기준).

멈춤이 발견되면 표로 출력하고, `node /apps/_shared/notify-owner.mjs`로 반장·운영자에게 1줄을 보낸다
(같은 사본은 상태 파일로 30분에 1번만 — 중복 알림 방지).

the office app `GET /api/task-metrics`는 기간 집계만 주고 작업 하나의 상태를 주지 않아(routes/task-metrics.js 실측,
2026-11-03) 이 감시에는 쓸 수 없다 — 대신 office.sqlite `tasks` 테이블을 읽기 전용으로 직접 읽는다.

Run: python3 /apps/_ops/tools/stall_watch.py [분=30]
  환경변수(시험용): STALL_WATCH_WT_ROOT, STALL_WATCH_DB, STALL_WATCH_STATE, STALL_WATCH_NO_NOTIFY=1
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

WT_ROOT = Path(os.environ.get("STALL_WATCH_WT_ROOT", "/apps/_wt"))
OFFICE_DB = Path(os.environ.get("STALL_WATCH_DB", "/data/exampleapp/office.sqlite"))
STATE_FILE = Path(os.environ.get("STALL_WATCH_STATE", "/apps/_ops/metrics/stall-watch-state.json"))
NO_NOTIFY = os.environ.get("STALL_WATCH_NO_NOTIFY") == "1"
NOTIFY_MJS = "/apps/_shared/notify-owner.mjs"
RENOTIFY_SEC = 6 * 60 * 60   # the same copy paged repeatedly at a short interval once; first page, then once per renotify window

# 빌드 산출물·캐시 폴더 — MASTER_RULES §7 "제외 폴더"와 같다. 여기 mtime은 "작업했다"는 신호가 아니다.
EXCLUDE_DIRS = {
    ".git", "node_modules", "dist", "build", "out", ".next", "coverage", ".venv",
    "__pycache__", ".cache", "logs", "tmp", "vendor", "target",
}


def _parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        v = value.replace("Z", "+00:00")
        return datetime.fromisoformat(v).timestamp()
    except ValueError:
        return None


def load_wt_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def last_commit_epoch(copy_path: Path) -> float | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(copy_path), "log", "-1", "--format=%ct"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    text = out.stdout.strip()
    return float(text) if text.isdigit() else None


def max_file_mtime(copy_path: Path) -> float | None:
    best: float | None = None
    for root, dirs, files in os.walk(copy_path):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".next-")]
        for name in files:
            try:
                mtime = (Path(root) / name).stat().st_mtime
            except OSError:
                continue
            if best is None or mtime > best:
                best = mtime
    return best


def task_status_from_db(db_path: Path, task_id: str) -> dict | None:
    """office.sqlite를 읽기 전용(mode=ro)으로만 연다 — 쓰지 않는다."""
    if not db_path.exists() or not task_id:
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError:
        return None
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT status, created_at, started_at, ended_at, resume_count, owner_followup, error "
            "FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = cur.fetchone()
    finally:
        con.close()
    if row is None:
        return None
    status, created_at, started_at, ended_at, resume_count, owner_followup, error = row
    epochs = [e for e in (_parse_iso(created_at), _parse_iso(started_at), _parse_iso(ended_at)) if e is not None]
    return {
        "status": status,
        "activity_epoch": max(epochs) if epochs else None,
        "resume_count": resume_count,
        "owner_followup": owner_followup,
        "has_error": bool(error),
    }


def units_alive(units: list[str]) -> list[tuple[str, str]]:
    result = []
    for unit in units or []:
        try:
            out = subprocess.run(
                ["systemctl", "--user", "is-active", unit],
                capture_output=True, text=True, timeout=5,
            )
            state = out.stdout.strip() or out.stderr.strip() or "알수없음"
        except (OSError, subprocess.TimeoutExpired):
            state = "확인못함"
        result.append((unit, state))
    return result


def work_pending(copy_path: Path, base: str) -> bool:
    """True only when the copy still holds work that could stall: commits not yet in its base branch, or
    uncommitted changes (wt's own .wt.* files ignored). A merged-and-clean copy or an empty copy (no commit
    yet) is nothing to page about — gc handles those (2026-11-03: #시스템 got "멈춤" for probe4/syschan/bktest
    which were all merged or empty)."""
    try:
        if base:
            ahead = subprocess.run(["git", "-C", str(copy_path), "rev-list", "--count", f"{base}..HEAD"],
                                   capture_output=True, text=True, timeout=20)
            if ahead.returncode == 0 and int(ahead.stdout.strip() or "0") > 0:
                return True
        st = subprocess.run(["git", "-C", str(copy_path), "status", "--porcelain"], capture_output=True, text=True, timeout=20)
        if st.returncode != 0:
            return True  # cannot tell -> be safe, keep watching
        return any(line and ".wt." not in line for line in st.stdout.splitlines())
    except (subprocess.SubprocessError, ValueError):
        return True


def scan_one(json_path: Path, db_path: Path, now: float) -> dict | None:
    info = load_wt_json(json_path)
    if info is None:
        return None
    copy_path = Path(info.get("path") or json_path.parent)
    if not copy_path.is_dir():
        return None
    hold = read_hold(copy_path)
    finished = (copy_path / ".wt.done").exists() or not work_pending(copy_path, info.get("base") or "")
    commit_epoch = last_commit_epoch(copy_path) if not finished else None
    mtime_epoch = max_file_mtime(copy_path) if not finished else None
    created_epoch = _parse_iso(info.get("created_at"))
    task_id = info.get("task_id")
    task = task_status_from_db(db_path, task_id) if task_id else None
    task_epoch = task.get("activity_epoch") if task else None
    units = units_alive(info.get("units") or [])

    candidates = [e for e in (commit_epoch, mtime_epoch, task_epoch, created_epoch) if e is not None]
    activity_epoch = max(candidates) if candidates else now
    age_min = (now - activity_epoch) / 60.0

    return {
        "app": info.get("app"),
        "name": info.get("name"),
        "path": str(copy_path),
        "finished": finished,
        "hold": hold,
        "task_id": task_id,
        "task_status": task.get("status") if task else None,
        "commit_epoch": commit_epoch,
        "mtime_epoch": mtime_epoch,
        "task_epoch": task_epoch,
        "created_epoch": created_epoch,
        "activity_epoch": activity_epoch,
        "age_min": age_min,
        "units": units,
    }


def read_hold(copy_path: Path) -> str | None:
    """`.wt.hold` (written by _shared/wt-hold.sh) = this copy is waiting on a person (owner answer, QR scan, decision):
    not stalled, not paged. The file holds the reason."""
    f = copy_path / ".wt.hold"
    try:
        return f.read_text(encoding="utf-8").strip() or "대기" if f.exists() else None
    except OSError:
        return None


def is_stalled(row: dict, threshold_min: float) -> bool:
    if row["finished"] or row.get("hold"):
        return False
    return row["age_min"] > threshold_min


def find_wt_jsons(wt_root: Path) -> list[Path]:
    if not wt_root.is_dir():
        return []
    return sorted(wt_root.glob("*/*/.wt.json"))


def _fmt_age(minutes: float) -> str:
    if minutes < 60:
        return f"{minutes:.0f}분"
    return f"{minutes / 60:.1f}시간"


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def notify(row: dict, threshold_min: float) -> None:
    # owner should do. One line because notify-owner sends one line.
    text = (
        f"작업이 멈춘 것 같습니다: {row['app']} 앱의 '{row['name']}' 작업 사본이 {_fmt_age(row['age_min'])} 동안 아무 변화가 없습니다. "
        f"작업조가 끝내지 못하고 멈췄거나 잊힌 사본일 수 있습니다. "
        f"운영자가 할 일: 없음 — 반장이 확인해서 이어서 하거나 정리합니다. (같은 알림은 6시간에 한 번만 옵니다)"
    )
    try:
        subprocess.run(
            ["node", NOTIFY_MJS, text, "/", "시스템"],  # machine alert channel 
            timeout=20, capture_output=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    threshold_min = float(argv[0]) if argv else 30.0
    now = time.time()
    rows = []
    for jp in find_wt_jsons(WT_ROOT):
        row = scan_one(jp, OFFICE_DB, now)
        if row is not None:
            rows.append(row)

    print(f"멈춘 작업 감시 — 사본 {len(rows)}개, 기준 {threshold_min:.0f}분")
    print(f"{'사본':40} {'상태':8} {'나이':10} {'작업상태':10} {'살아있는 유닛'}")
    stalled_rows = []
    for row in rows:
        state = "완료" if row["finished"] else ("대기" if row.get("hold") else ("멈춤" if is_stalled(row, threshold_min) else "진행중"))
        alive = ", ".join(f"{u}:{s}" for u, s in row["units"]) or "-"
        label = f"{row['app']}/{row['name']}"
        print(f"{label:40} {state:8} {_fmt_age(row['age_min']):10} {row['task_status'] or '-':10} {alive}")
        if state == "멈춤":
            stalled_rows.append(row)

    if stalled_rows and not NO_NOTIFY:
        prev_state = _load_state()
        for row in stalled_rows:
            key = row["path"]
            last_notified = prev_state.get(key)
            if last_notified is not None and (now - last_notified) < RENOTIFY_SEC:
                continue
            notify(row, threshold_min)
            prev_state[key] = now
        _save_state(prev_state)

    print(f"멈춤 사본 {len(stalled_rows)}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
