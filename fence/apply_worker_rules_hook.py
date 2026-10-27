#!/usr/bin/env python3
"""작업조 규칙 집행 훅 설치 (운영자가 직접 실행).

    python3 <apps-root>/<ops>/harness/apply_worker_rules_hook.py          # 설치
    python3 <apps-root>/<ops>/harness/apply_worker_rules_hook.py --check  # 바꾸지 않고 확인만
    python3 <apps-root>/<ops>/harness/apply_worker_rules_hook.py --undo   # 되돌리기

하는 일: settings.json 백업 → 훅 파일 복사 → PreToolUse(Bash)에 한 줄 추가
→ 설치된 훅을 가짜 호출로 자체 시험 → 실패하면 자동으로 되돌림.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
SETTINGS = HOME / ".claude" / "settings.json"
HOOK_DIR = HOME / ".claude" / "hooks"
TARGET = HOOK_DIR / "worker_rules.py"
SOURCE = Path(__file__).resolve().parent / "worker_rules_hook.py"
BACKUP = SETTINGS.with_name("settings.json.bak-worker-rules")
COMMAND = f"python3 {TARGET}"
MATCHER = "Bash|Monitor|ScheduleWakeup"


def _entry_present(data: dict) -> bool:
    for group in data.get("hooks", {}).get("PreToolUse", []):
        for hook in group.get("hooks", []):
            if hook.get("command") == COMMAND:
                return True
    return False


def _set_matcher(data: dict) -> None:
    """An earlier install registered matcher "Bash"; widen it in place."""
    for group in data.get("hooks", {}).get("PreToolUse", []):
        if any(h.get("command") == COMMAND for h in group.get("hooks", [])):
            group["matcher"] = MATCHER


def _denied(command: str, *, worker: bool = True, cwd: str = "<apps-root>/s21-orbit",
            background: bool = False, tool: str = "Bash") -> bool:
    event = {"tool_name": tool, "cwd": cwd,
             "tool_input": {"command": command, "run_in_background": background}}
    if worker:
        event["agent_id"] = "worker-1"
    out = subprocess.run([sys.executable, str(TARGET)], input=json.dumps(event),
                         capture_output=True, text=True, timeout=20)
    return '"deny"' in out.stdout


def _self_test() -> bool:
    main_repo = "<apps-root>/if-gongbang-v2"
    checks = [
        ("작업조 git stash → 막힘", _denied("git stash push -m x"), True),
        ("작업조 git -C <트리> reset → 막힘", _denied("git -C <apps-root>/s21-orbit reset --hard"), True),
        ("작업조 git checkout -- 파일 → 막힘", _denied("git checkout -- engine/plan.py"), True),
        ("작업조 git add -A → 막힘", _denied("git add -A"), True),
        ("작업조 main 폴더에서 commit → 막힘", _denied("git commit -m x -- a.py", cwd=main_repo), True),
        ("작업조 cd main && commit → 막힘", _denied(f"cd {main_repo} && git commit -m x -- a.py"), True),
        ("작업조 git -C main commit → 막힘", _denied(f"git -C {main_repo} commit -m x"), True),
        ("작업조 pytest 백그라운드 → 막힘", _denied(".venv/bin/python -m pytest -q", background=True), True),
        ("작업조 다른 명령 백그라운드 → 막힘", _denied("bash run_tests.sh > log 2>&1", background=True), True),
        ("작업조 Monitor 도구 → 막힘", _denied("", tool="Monitor"), True),
        ("작업조 ScheduleWakeup 도구 → 막힘", _denied("", tool="ScheduleWakeup"), True),
        ("작업조 until … sleep 대기 → 막힘", _denied("until [ -f done ]; do sleep 30; done"), True),
        ("작업조 while … sleep 대기 → 막힘", _denied("while kill -0 123; do sleep 5; done; cat log"), True),
        ("작업조 git restore → 막힘", _denied("git restore engine/plan.py"), True),
        ("작업조 git add . → 막힘", _denied("git add ."), True),
        ("작업조 git checkout -b 가지 → 통과", _denied("git checkout -b feature"), False),
        ("작업조 커밋 메시지에 stash 글자 → 통과", _denied('git commit -m "no stash used" -- a.py'), False),
        ("반장 Monitor → 통과", _denied("", tool="Monitor", worker=False), False),
        ("반장 sleep 대기 → 통과", _denied("until [ -f x ]; do sleep 30; done", worker=False), False),
        ("작업조 자기 트리 commit → 통과", _denied("git commit -m x -- a.py"), False),
        ("작업조 git add 경로 → 통과", _denied("git add engine/plan.py tests/unit/test_plan.py"), False),
        ("작업조 git merge main → 통과", _denied("git merge --no-edit main"), False),
        ("작업조 pytest 전경 → 통과", _denied(".venv/bin/python -m pytest -q tests/unit/test_plan.py"), False),
        ("반장 main commit → 통과", _denied("git commit -m x -- a.py", worker=False, cwd=main_repo), False),
        ("반장 pytest 백그라운드 → 통과", _denied("pytest -q", worker=False, background=True), False),
    ]
    ok = True
    for label, got, want in checks:
        print(f"  {'OK ' if got == want else 'FAIL'} {label}")
        ok &= got == want
    return ok


def undo() -> int:
    if BACKUP.exists():
        shutil.copy2(BACKUP, SETTINGS)
        print(f"settings.json 되돌림 ← {BACKUP}")
    TARGET.unlink(missing_ok=True)
    print("훅 파일 제거")
    return 0


def main() -> int:
    if "--undo" in sys.argv:
        return undo()
    data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    present = _entry_present(data)
    print(f"현재: 훅 연결 {'있음' if present else '없음'}, 훅 파일 {'있음' if TARGET.exists() else '없음'}")
    if "--check" in sys.argv:
        return 0
    shutil.copy2(SETTINGS, BACKUP)
    HOOK_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE, TARGET)
    if not present:
        groups = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
        groups.append({"matcher": MATCHER, "hooks": [{"type": "command", "command": COMMAND, "timeout": 10}]})
    _set_matcher(data)
    SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("자체 시험:")
    if not _self_test():
        print("자체 시험 실패 → 되돌립니다")
        undo()
        return 1
    print("설치 완료. 지금 돌고 있는 작업조에도 다음 명령부터 적용됩니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
