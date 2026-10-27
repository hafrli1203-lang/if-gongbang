#!/usr/bin/env python3
"""공방 울타리 전부 한 번에 설치 (운영자가 직접 실행).

    python3 <apps-root>/<ops>/harness/apply_gongbang_all.py          # 전부 설치·갱신
    python3 <apps-root>/<ops>/harness/apply_gongbang_all.py --check  # 바꾸지 않고 확인만

각 설치 스크립트를 순서대로 돌린다. 각 스크립트는 자체 시험이 실패하면 자기 설치분만 되돌린다.
하나라도 실패하면 거기서 멈추고 실패한 이름을 알려 준다(앞서 성공한 설치는 그대로 둔다).
멈춤 감시 서비스(apply_gongbang_watch.py)는 서비스 재시작이 따르므로 포함하지 않는다.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ORDER = (
    "apply_gongbang_fence.py",   # pre-change report block, directive check, read to end
    "apply_gongbang_fence2.py",  # scope fence, switch/parallel nudges
    "apply_bansang_hook.py",     # foreman habit block
    "apply_worker_rules_hook.py",  # worker rules
)


def main() -> int:
    extra = [a for a in sys.argv[1:] if a == "--check"]
    for name in ORDER:
        print(f"== {name}")
        rc = subprocess.run([sys.executable, str(HERE / name), *extra]).returncode
        if rc != 0:
            print(f"실패: {name} (rc={rc}) — 여기서 멈춤. 앞서 성공한 설치는 그대로 둠.")
            return rc
    print("전부 완료." if not extra else "확인 끝.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
