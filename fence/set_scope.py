#!/usr/bin/env python3
"""운영자 전용 범위 설정 (운영자가 `!`로 직접 실행 — 모델 도구 호출이 아니므로 scope_fence_hook.py에
걸리지 않는다). <apps-root>/<ops>/CURRENT_SCOPE.json 하나를 원자적으로 갱신한다.

    python3 <apps-root>/<ops>/harness/set_scope.py if-gongbang-v2 if-gongbang-v2-wt live-workspace <ops> other-app --reason "공방 개발 먼저"
    python3 <apps-root>/<ops>/harness/set_scope.py --show     # 지금 범위만 보기
    python3 <apps-root>/<ops>/harness/set_scope.py --clear    # 범위 해제(= 무제한, 파일 삭제)

각 이름은 <apps-root>/<이름>이 실제 폴더인지 확인한다(오타 방지). 이전 값은 .bak으로 남긴다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

SCOPE_FILE = Path("<apps-root>/<ops>/CURRENT_SCOPE.json")
APPS_ROOT = Path("<apps-root>")


def show() -> int:
    if not SCOPE_FILE.exists():
        print("지금 범위: 없음 (제한 없음)")
        return 0
    data = json.loads(SCOPE_FILE.read_text(encoding="utf-8"))
    print(f"지금 범위: {', '.join(data.get('allow', []))}")
    print(f"사유: {data.get('reason', '')}")
    print(f"설정 시각: {data.get('set_at', '')}")
    return 0


def clear() -> int:
    if SCOPE_FILE.exists():
        SCOPE_FILE.rename(SCOPE_FILE.with_suffix(".json.bak"))
    print("범위 해제함 (지금부터 제한 없음)")
    return 0


def write(apps: list[str], reason: str) -> int:
    missing = [a for a in apps if not (APPS_ROOT / a).is_dir()]
    if missing:
        print(f"오류: <apps-root> 아래에 없는 이름 {missing} — 오타 확인", file=sys.stderr)
        return 1
    data = {"allow": sorted(set(apps)), "reason": reason, "set_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if SCOPE_FILE.exists():
        SCOPE_FILE.rename(SCOPE_FILE.with_suffix(".json.bak"))
    tmp = SCOPE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, SCOPE_FILE)
    print(f"범위 설정: {', '.join(data['allow'])}")
    print(f"사유: {reason}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("apps", nargs="*")
    ap.add_argument("--reason", default="")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--clear", action="store_true")
    ns = ap.parse_args()
    if ns.show:
        return show()
    if ns.clear:
        return clear()
    if not ns.apps:
        ap.print_help()
        return 1
    return write(ns.apps, ns.reason)


if __name__ == "__main__":
    sys.exit(main())
