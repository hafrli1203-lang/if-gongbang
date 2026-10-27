#!/usr/bin/env python3
"""반장 습관 차단 훅 설치 (운영자가 직접 실행).

    python3 <apps-root>/<ops>/harness/apply_bansang_hook.py          # 설치
    python3 <apps-root>/<ops>/harness/apply_bansang_hook.py --check  # 바꾸지 않고 확인만
    python3 <apps-root>/<ops>/harness/apply_bansang_hook.py --undo   # 되돌리기

하는 일: settings.json 백업 → 훅 파일 복사 → PreToolUse(Bash|Edit|Write)에 한 줄 추가
→ 설치된 훅을 가짜 기록으로 자체 시험 → 실패하면 자동으로 되돌림.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = Path.home()
SETTINGS = HOME / ".claude" / "settings.json"
HOOK_DIR = HOME / ".claude" / "hooks"
TARGET = HOOK_DIR / "bansang_habit.py"
SOURCE = Path(__file__).resolve().parent / "bansang_habit_hook.py"
BACKUP = SETTINGS.with_name("settings.json.bak-bansang")
COMMAND = f"python3 {TARGET}"
MATCHER = "Bash|Edit|Write"


def _entry_present(data: dict) -> bool:
    for group in data.get("hooks", {}).get("PreToolUse", []):
        for hook in group.get("hooks", []):
            if hook.get("command") == COMMAND:
                return True
    return False


def _self_test() -> bool:
    def run(calls: list[str], tool: str, agent_id: str = "") -> bool:
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "test"}}) + "\n")
            for name in calls:
                f.write(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": name, "input": {}}]}}) + "\n")
            path = f.name
        event = {"tool_name": tool, "transcript_path": path}
        if agent_id:
            event["agent_id"] = agent_id
        out = subprocess.run([sys.executable, str(TARGET)], input=json.dumps(event),
                             capture_output=True, text=True, timeout=20)
        Path(path).unlink(missing_ok=True)
        return '"deny"' in out.stdout
    checks = [
        ("Bash 25회·스킬 0회 → 막힘", run(["Bash"] * 25, "Bash"), True),
        ("스킬 1회 뒤 Bash → 통과", run(["Skill"] + ["Bash"] * 24, "Bash"), False),
        ("직접 수정 50회 → 막힘", run(["Skill"] + ["Edit"] * 50, "Edit"), True),
        ("작업조 호출은 → 통과", run(["Edit"] * 60, "Agent"), False),
        ("작업조 자신의 Bash → 통과", run(["Bash"] * 60, "Bash", agent_id="worker-1"), False),
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
        SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("자체 시험:")
    if not _self_test():
        print("자체 시험 실패 → 되돌립니다")
        undo()
        return 1
    print("설치 완료. 새 대화창부터 적용됩니다(지금 창은 다음 도구 호출부터).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
