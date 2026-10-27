#!/usr/bin/env python3
"""G30 멈춤 감시 상시화 (운영자가 직접 실행 — 유닛 설치·서비스 활성은 승인 관문 대상).

    python3 <apps-root>/<ops>/harness/apply_gongbang_watch.py          # 설치 + enable --now
    python3 <apps-root>/<ops>/harness/apply_gongbang_watch.py --check  # 바꾸지 않고 확인만
    python3 <apps-root>/<ops>/harness/apply_gongbang_watch.py --undo   # 중지·해제 + 유닛 제거

harness/ifapp-gongbang-watch.service를 ~/.config/systemd/user/에 복사하고
`systemctl --user daemon-reload && systemctl --user enable --now`를 돈다.
이 스크립트를 직접 실행하는 것은 운영자만 한다(AI는 유닛 편집·서비스 활성 금지 — MASTER_RULES §2).
AI 쪽 자체 시험은 --dry-run으로, 유닛 파일 내용만 가짜 HOME에 써서 확인하고 systemctl은 부르지 않는다.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "ifapp-gongbang-watch.service"
UNIT_NAME = "ifapp-gongbang-watch.service"


def _unit_dir() -> Path:
    import os
    home = os.environ.get("GONGBANG_WATCH_HOME")
    base = Path(home) if home else Path.home()
    return base / ".config" / "systemd" / "user"


def _target() -> Path:
    return _unit_dir() / UNIT_NAME


def check() -> int:
    t = _target()
    print(f"현재: 유닛 파일 {'있음' if t.exists() else '없음'} ({t})")
    if shutil.which("systemctl") and t == Path.home() / ".config" / "systemd" / "user" / UNIT_NAME:
        out = subprocess.run(["systemctl", "--user", "is-active", UNIT_NAME],
                             capture_output=True, text=True, timeout=10)
        print(f"현재: 서비스 상태 {out.stdout.strip() or out.stderr.strip()}")
    return 0


def undo(dry_run: bool) -> int:
    t = _target()
    if not dry_run:
        subprocess.run(["systemctl", "--user", "disable", "--now", UNIT_NAME], check=False)
    t.unlink(missing_ok=True)
    print(f"유닛 제거: {t}")
    if not dry_run:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    return 0


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    if "--undo" in sys.argv:
        return undo(dry_run)
    if "--check" in sys.argv:
        return check()
    unit_dir = _unit_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    target = _target()
    shutil.copy2(SOURCE, target)
    print(f"유닛 복사: {SOURCE} -> {target}")
    if dry_run:
        print("--dry-run: systemctl 호출 생략, 파일만 확인")
        return 0
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", UNIT_NAME], check=True)
    out = subprocess.run(["systemctl", "--user", "is-active", UNIT_NAME],
                         capture_output=True, text=True, timeout=10)
    print(f"설치 완료, 상태: {out.stdout.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
