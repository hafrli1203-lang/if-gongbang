#!/usr/bin/env python3
"""공방울타리2 설치 (운영자가 직접 실행) — G25+G19 범위 고정, G05 전환 알림, G13 병렬 알림.

    python3 <apps-root>/<ops>/harness/apply_gongbang_fence2.py          # 설치
    python3 <apps-root>/<ops>/harness/apply_gongbang_fence2.py --check  # 바꾸지 않고 확인만
    python3 <apps-root>/<ops>/harness/apply_gongbang_fence2.py --undo   # 되돌리기

하는 일:
  1. scope_fence_hook.py 설치 (PreToolUse, Edit|Write|MultiEdit|NotebookEdit|Bash|Agent):
     CURRENT_SCOPE.json의 허용 목록 밖 앱 쓰기·gb2 실행·작업조 지시를 막는다.
  2. orbit_nudge_hook.py 설치 (Stop): 같은 시험 3회 연속 실패(G05), 결과 기다리며 턴 종료(G13)를
     막지 않고 알린다.
  3. preflight_gate_hook.py를 다시 복사해 B4-1 수정(변수 리디렉션 오탐)을 반영한다(이미 설치돼
     있던 apply_gongbang_fence.py의 몫을 같은 소스로 갱신 — 중복 로직 없이 최신 소스로 맞춘다).
settings.json과 관련 훅 파일을 백업 → 복사·등록 → 자체 시험 → 실패하면 이번 실행만 되돌림.
CURRENT_SCOPE.json 자체는 이 스크립트가 만들지 않는다 — 운영자가 set_scope.py로 직접 정한다.
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
BACKUP = SETTINGS.with_name("settings.json.bak-gongbang-fence2")
RUN_BACKUP = SETTINGS.with_name("settings.json.bak-gongbang-fence2-run")
PREFLIGHT = HOOK_DIR / "preflight-gate.py"

NEW_HOOKS = [  # (source, target, event, matcher)
    (HERE / "scope_fence_hook.py", HOOK_DIR / "scope_fence.py", "PreToolUse",
     "Edit|Write|MultiEdit|NotebookEdit|Bash|Agent"),
    (HERE / "orbit_nudge_hook.py", HOOK_DIR / "orbit_nudge.py", "Stop", ""),
]
REFRESH_ONLY = [(HERE / "preflight_gate_hook.py", PREFLIGHT)]  # B4-1 fix, no new settings entry


def _command(target: Path) -> str:
    return f"python3 {target}"


def _present(data: dict, event: str, command: str) -> bool:
    return any(h.get("command") == command
               for g in data.get("hooks", {}).get(event, []) for h in g.get("hooks", []))


def _run_hook(target: Path, event: dict, env: dict) -> str:
    out = subprocess.run([sys.executable, str(target)], input=json.dumps(event, ensure_ascii=False),
                         capture_output=True, text=True, timeout=20, env={**os.environ, **env})
    return out.stdout


def _transcript(records: list[dict]) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return f.name


def _write_settings(data: dict) -> None:
    tmp = SETTINGS.with_name("settings.json.tmp-gongbang-fence2")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    json.loads(tmp.read_text(encoding="utf-8"))
    os.replace(tmp, SETTINGS)


def _restore(backup: Path, target: Path) -> None:
    tmp = target.with_name(target.name + ".tmp-restore2")
    shutil.copy2(backup, tmp)
    os.replace(tmp, target)


def _run_copy(target: Path) -> Path:
    return target.with_name(target.name + ".bak-run2")


def _rollback_run() -> None:
    if RUN_BACKUP.exists():
        _restore(RUN_BACKUP, SETTINGS)
    for _s, target in REFRESH_ONLY:
        if _run_copy(target).exists():
            _restore(_run_copy(target), target)
    for _s, target, _e, _m in NEW_HOOKS:
        if _run_copy(target).exists():
            _restore(_run_copy(target), target)
        else:
            target.unlink(missing_ok=True)
    print("이번 설치만 되돌림(그 전 설정 그대로)")


def _self_test() -> bool:
    scope_fence, orbit_nudge = (t for _s, t, _e, _m in NEW_HOOKS)
    tmp = Path(tempfile.mkdtemp(prefix="gongbang-fence2-selftest-"))
    apps = tmp / "apps"
    (apps / "demo-app" / "src").mkdir(parents=True)
    (apps / "other-app" / "src").mkdir(parents=True)
    scope_file = tmp / "CURRENT_SCOPE.json"
    scope_file.write_text(json.dumps({"allow": ["demo-app"]}), encoding="utf-8")
    env = {"SCOPE_FENCE_FILE": str(scope_file), "SCOPE_FENCE_APPS_ROOT": str(apps)}

    def edit(app: str) -> dict:
        return {"tool_name": "Edit", "tool_input": {"file_path": str(apps / app / "src" / "x.py")}}

    off = {name: (Path(os.path.expanduser(f"~/.claude/gate/{name}-off"))).exists()
           for name in ("scope-fence", "orbit-nudge")}

    def bash(cmd: str, cwd: str = "") -> dict:
        return {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": cwd or str(apps / "demo-app")}

    def stop(records: list[dict]) -> str:
        t = _transcript(records)
        out = _run_hook(orbit_nudge, {"transcript_path": t}, {})
        Path(t).unlink(missing_ok=True)
        return out

    def _say(text: str) -> dict:
        return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}

    def _tool(name: str, inp: dict, tid: str) -> dict:
        return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}

    def _result(tid: str, text: str, is_error: bool = False) -> dict:
        return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid,
                                                          "content": text, "is_error": is_error}]}}

    fail3 = []
    for i in range(3):
        fail3.append(_tool("Bash", {"command": "pytest -q tests/x.py"}, f"t{i}"))
        fail3.append(_result(f"t{i}", "1 FAILED"))
    # Live shape: a background launch answers at once with its id; pending until a completion notice.
    pending = [_tool("Bash", {"command": "sleep 1", "run_in_background": True}, "bg1"),
               _result("bg1", "Command running in background with ID: bx1abc. Output is being written to: x")]

    checks = [
        ("범위 밖 Edit → 막힘", '"deny"' in _run_hook(scope_fence, edit("other-app"), env), True),
        ("범위 안 Edit → 통과", '"deny"' in _run_hook(scope_fence, edit("demo-app"), env), False),
        ("범위 밖 gb2 run → 막힘", '"deny"' in _run_hook(scope_fence, bash("gb2 run other-app"), env), True),
        ("범위 안 gb2 run → 통과", '"deny"' in _run_hook(scope_fence, bash("gb2 run demo-app"), env), False),
        ("gb2 watch(전체) → 통과", '"deny"' in _run_hook(scope_fence, bash("gb2 watch --loop"), env), False),
        ("Agent 지시에 다른 앱 이름 → 막힘",
         '"deny"' in _run_hook(scope_fence, {"tool_name": "Agent", "tool_input": {"prompt": "other-app 고쳐라"}}, env), True),
        ("Agent 지시에 범위 앱 이름만 → 통과",
         '"deny"' in _run_hook(scope_fence, {"tool_name": "Agent", "tool_input": {"prompt": "demo-app 고쳐라"}}, env), False),
        ("CURRENT_SCOPE.json을 모델이 Edit → 항상 막힘",
         '"deny"' in _run_hook(scope_fence, {"tool_name": "Edit", "tool_input": {"file_path": str(scope_file)}}, env), True),
        ("같은 시험 3연속 실패 → 알림", "지시 대조" not in (out := stop(fail3)) and bool(out), True),
        ("대기 중인 백그라운드 뒤 턴 종료 → 알림", bool(stop(pending)), True),
        ("시험 성공 뒤 종료 → 조용함", bool(stop([_tool('Bash', {'command': 'pytest'}, 'ok1'), _result('ok1', 'passed')])), False),
    ]
    shutil.rmtree(tmp, ignore_errors=True)
    skip = {0: off["scope-fence"], 1: off["scope-fence"], 2: off["scope-fence"], 3: off["scope-fence"],
            4: off["scope-fence"], 5: off["scope-fence"], 6: off["scope-fence"], 7: False,
            8: off["orbit-nudge"], 9: off["orbit-nudge"], 10: off["orbit-nudge"]}
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
    for _s, target, _e, _m in NEW_HOOKS:
        target.unlink(missing_ok=True)
    print("새 훅 파일 제거(preflight-gate.py의 B4-1 수정은 남는다 — apply_gongbang_fence.py --undo로 더 되돌림)")
    return 0


def main() -> int:
    if "--undo" in sys.argv:
        return undo()
    data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    for _s, target, event, _m in NEW_HOOKS:
        on = _present(data, event, _command(target))
        print(f"현재: {target.name} 연결 {'있음' if on else '없음'}, 파일 {'있음' if target.exists() else '없음'}")
    if "--check" in sys.argv:
        return 0
    if not BACKUP.exists():
        shutil.copy2(SETTINGS, BACKUP)
    shutil.copy2(SETTINGS, RUN_BACKUP)
    for _s, target in REFRESH_ONLY:
        _run_copy(target).unlink(missing_ok=True)
        if target.exists():
            shutil.copy2(target, _run_copy(target))
    for _s, target, _e, _m in NEW_HOOKS:
        _run_copy(target).unlink(missing_ok=True)
        if target.exists():
            shutil.copy2(target, _run_copy(target))
    try:
        for source, target in REFRESH_ONLY:
            _restore(source, target)
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
    print("설치 완료. 범위를 정하려면: python3 <apps-root>/<ops>/harness/set_scope.py <앱...> --reason \"...\"")
    print("새 대화창부터 확실히 적용됩니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
