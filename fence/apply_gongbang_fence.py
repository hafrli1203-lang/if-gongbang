#!/usr/bin/env python3
"""공방 울타리 3종 설치 (운영자가 직접 실행, [date] "1,2,3 다 승인한다").

    python3 <apps-root>/<ops>/harness/apply_gongbang_fence.py          # 설치
    python3 <apps-root>/<ops>/harness/apply_gongbang_fence.py --check  # 바꾸지 않고 확인만
    python3 <apps-root>/<ops>/harness/apply_gongbang_fence.py --undo   # 되돌리기

하는 일:
  1. 착수 전 보고 훅을 경고 → 막기로 바꾼다(작업 가지 폴더도 앱 코드로 본다).
  2. 지시 대조 훅(Stop)을 붙인다: 완료 보고에 운영자 원문 인용 + '지시 대조' 절이 없으면 다시 쓰게 한다.
  3. 끝까지 읽기 훅(Read|Bash)을 붙인다: 2000줄 이하 문서를 앞부분만 읽으면 막는다.
settings.json과 기존 착수 전 보고 훅을 백업 → 복사·등록 → 설치된 훅을 가짜 기록으로 자체 시험 → 실패하면 자동으로 되돌린다.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = Path.home()
SETTINGS = HOME / ".claude" / "settings.json"
HOOK_DIR = HOME / ".claude" / "hooks"
HERE = Path(__file__).resolve().parent
BACKUP = SETTINGS.with_name("settings.json.bak-gongbang-fence")
PREFLIGHT = HOOK_DIR / "preflight-gate.py"
PREFLIGHT_BACKUP = HOOK_DIR / "preflight-gate.py.bak-warn"
RUN_BACKUP = SETTINGS.with_name("settings.json.bak-gongbang-fence-run")
RUN_PREFLIGHT = HOOK_DIR / "preflight-gate.py.bak-run"
OFF_DIR = HOME / ".claude" / "gate"
NEW_HOOKS = [  # (source, target, event, matcher)
    (HERE / "directive_check_hook.py", HOOK_DIR / "directive_check.py", "Stop", ""),
    (HERE / "read_whole_hook.py", HOOK_DIR / "read_whole.py", "PreToolUse", "Read|Bash"),
]


def _command(target: Path) -> str:
    return f"python3 {target}"


def _present(data: dict, event: str, command: str) -> bool:
    return any(h.get("command") == command
               for g in data.get("hooks", {}).get(event, []) for h in g.get("hooks", []))


def _run_hook(target: Path, event: dict) -> str:
    out = subprocess.run([sys.executable, str(target)], input=json.dumps(event, ensure_ascii=False),
                         capture_output=True, text=True, timeout=20)
    return out.stdout


def _transcript(records: list[dict]) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return f.name


def _write_settings(data: dict) -> None:
    tmp = SETTINGS.with_name("settings.json.tmp-gongbang-fence")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    json.loads(tmp.read_text(encoding="utf-8"))
    os.replace(tmp, SETTINGS)


def _restore(backup: Path, target: Path) -> None:
    """Copy next to the target, then rename over it -- a failed copy never leaves a half-written target."""
    tmp = target.with_name(target.name + ".tmp-restore")
    shutil.copy2(backup, tmp)
    os.replace(tmp, target)


def _run_copy(target: Path) -> Path:
    return target.with_name(target.name + ".bak-run")


def _rollback_run() -> None:
    """Undo only this run: restore the settings and every hook file as they were when it started."""
    if RUN_BACKUP.exists():
        _restore(RUN_BACKUP, SETTINGS)
    if RUN_PREFLIGHT.exists():
        _restore(RUN_PREFLIGHT, PREFLIGHT)
    for _s, target, _e, _m in NEW_HOOKS:
        if _run_copy(target).exists():
            _restore(_run_copy(target), target)
        else:
            target.unlink(missing_ok=True)
    print("이번 설치만 되돌림(그 전 설정 그대로)")


def _self_test() -> bool:
    human = {"type": "user", "message": {"content": "1,2,3 다 승인한다 쭉 해라"}}

    def say(text: str) -> dict:
        return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}

    bare = _transcript([human])
    reported = _transcript([human, say("읽은 자료 a\n예상 위험 b\n바꿀 파일 c\n되돌리는 법 d")])
    claim = _transcript([human, say("테스트 통과했습니다.")])
    checked = _transcript([human, say('## 지시 대조\n- "1,2,3 다 승인한다" → 했음\n테스트 통과했습니다.')])
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write("줄\n" * 300)
        doc = f.name
    edit = {"tool_name": "Edit", "tool_input": {"file_path": "<apps-root>/if-gongbang-v2/engine/x.py"}}
    directive, read_whole = (t for _s, t, _e, _m in NEW_HOOKS)
    off = {name: (OFF_DIR / name).exists() for name in ("preflight-off", "directive-check-off", "read-whole-off")}
    checks = [
        ("보고 없이 앱 코드 수정 → 막힘", '"deny"' in _run_hook(PREFLIGHT, {**edit, "transcript_path": bare}), True),
        ("네 가지 보고 뒤 수정 → 통과", '"deny"' in _run_hook(PREFLIGHT, {**edit, "transcript_path": reported}), False),
        ("지시 대조 없는 완료 보고 → 다시 쓰게 함", '"block"' in _run_hook(directive, {"transcript_path": claim}), True),
        ("원문 인용한 지시 대조 → 통과", '"block"' in _run_hook(directive, {"transcript_path": checked}), False),
        ("300줄 문서 앞 20줄만 → 막힘", '"deny"' in _run_hook(read_whole, {
            "tool_name": "Read", "tool_input": {"file_path": doc, "limit": 20}, "transcript_path": bare}), True),
        ("300줄 문서 전체 → 통과", '"deny"' in _run_hook(read_whole, {
            "tool_name": "Read", "tool_input": {"file_path": doc}, "transcript_path": bare}), False),
    ]
    for p in (bare, reported, claim, checked, doc):
        Path(p).unlink(missing_ok=True)
    skip = {0: off["preflight-off"], 1: off["preflight-off"], 2: off["directive-check-off"],
            3: off["directive-check-off"], 4: off["read-whole-off"], 5: off["read-whole-off"]}
    ok = True
    for i, (label, got, want) in enumerate(checks):
        if skip[i]:
            print(f"  건너뜀 {label} (운영자가 끈 스위치 있음)")
            continue
        print(f"  {'OK ' if got == want else 'FAIL'} {label}")
        ok &= got == want
    return ok


def undo() -> int:
    if BACKUP.exists():
        shutil.copy2(BACKUP, SETTINGS)
        print(f"settings.json 되돌림 ← {BACKUP}")
    if PREFLIGHT_BACKUP.exists():
        shutil.copy2(PREFLIGHT_BACKUP, PREFLIGHT)
        print("착수 전 보고 훅 되돌림(경고 모드)")
    for _s, target, _e, _m in NEW_HOOKS:
        target.unlink(missing_ok=True)
    print("새 훅 파일 제거")
    return 0


def main() -> int:
    if "--undo" in sys.argv:
        return undo()
    data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    mode = "막기" if 'MODE = "block"' in PREFLIGHT.read_text(encoding="utf-8") else "경고"
    print(f"현재: 착수 전 보고 {mode}")
    for _s, target, event, _m in NEW_HOOKS:
        on = _present(data, event, _command(target))
        print(f"현재: {target.name} 연결 {'있음' if on else '없음'}, 파일 {'있음' if target.exists() else '없음'}")
    if "--check" in sys.argv:
        return 0
    if not BACKUP.exists():
        shutil.copy2(SETTINGS, BACKUP)
    if not PREFLIGHT_BACKUP.exists():
        shutil.copy2(PREFLIGHT, PREFLIGHT_BACKUP)
    shutil.copy2(SETTINGS, RUN_BACKUP)
    shutil.copy2(PREFLIGHT, RUN_PREFLIGHT)
    for _s, target, _e, _m in NEW_HOOKS:
        _run_copy(target).unlink(missing_ok=True)
        if target.exists():
            shutil.copy2(target, _run_copy(target))
    try:
        _restore(HERE / "preflight_gate_hook.py", PREFLIGHT)
        for source, target, event, matcher in NEW_HOOKS:
            _restore(source, target)
            if not _present(data, event, _command(target)):
                groups = data.setdefault("hooks", {}).setdefault(event, [])
                groups.append({"matcher": matcher, "hooks": [{"type": "command", "command": _command(target), "timeout": 10}]})
        _write_settings(data)
        print("자체 시험:")
        passed = _self_test()
    except Exception as exc:
        print(f"설치 중 오류: {exc}")
        passed = False
    if not passed:
        print("자체 시험 실패 → 되돌립니다")
        _rollback_run()
        return 1
    print("설치 완료. 새 대화창부터 확실히 적용됩니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
