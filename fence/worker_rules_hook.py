#!/usr/bin/env python3
"""작업조 규칙 집행 훅 (PreToolUse, matcher "Bash|Monitor|ScheduleWakeup").

작업조(서브 에이전트)가 지시서의 경고를 어기는 명령을 실행하지 못하게 막는다.
운영자·반장 본 세션의 호출은 건드리지 않는다(agent_id/agent_type이 없는 호출).

막는 것
1. git stash / git reset / git restore / 파일 되돌리기용 git checkout / git add -A·--all·.
   (작업 트리가 여러 개라 stash는 모두가 함께 쓰고, reset·checkout은 남의 변경을 지운다)
2. 엔진 기본 폴더(MAIN_REPOS)에서의 git commit — 작업조는 자기 작업 트리에서만 커밋한다.
3. 결과를 기다리며 멈추는 모든 방법 — 작업조는 전경에서 timeout을 두고 실행하고 바로 이어서 일한다.
   - Bash run_in_background (명령 종류와 상관없이)
   - Monitor 도구, ScheduleWakeup 도구
   - sleep으로 도는 대기 반복(until/while … sleep …)

막을 때는 deny JSON을 내고 exit 0 (다른 집행 훅과 같은 방식).
"""
from __future__ import annotations

import json
import re
import sys

MAIN_REPOS = ("<apps-root>/if-gongbang-v2",)

_GIT_FORBIDDEN = (
    (re.compile(r"\bgit\b(?:\s+-C\s+\S+)?\s+stash\b"), "git stash"),
    (re.compile(r"\bgit\b(?:\s+-C\s+\S+)?\s+reset\b"), "git reset"),
    (re.compile(r"\bgit\b(?:\s+-C\s+\S+)?\s+checkout\b[^;&|\n]*\s--(\s|$)"), "git checkout -- <파일>"),
    (re.compile(r"\bgit\b(?:\s+-C\s+\S+)?\s+restore\b"), "git restore"),
    (re.compile(r"\bgit\b(?:\s+-C\s+\S+)?\s+add\s+(-A|--all|\.)(\s|$|;|&)"), "git add -A / git add ."),
)
_GIT_COMMIT = re.compile(r"\bgit\b((?:\s+-[cC]\s+\S+)*)\s+commit\b")
_CD = re.compile(r"(?:^|[;&|]\s*)cd\s+(\S+)")
_SLEEP_LOOP = re.compile(r"\b(until|while)\b[^\n]*\bsleep\b|\bsleep\b[^\n]*\b(done)\b", re.DOTALL)
_WAIT_TOOLS = ("Monitor", "ScheduleWakeup")
_WAIT_REASON = ("작업조 규칙: 결과를 기다리며 멈추지 않는다(백그라운드 실행·Monitor·sleep 대기 모두 금지). "
                "전경에서 Bash timeout 600000으로 실행하고, 10분을 넘는 시험은 -k로 묶음을 나눈다.")


def _is_worker(event: dict) -> bool:
    return bool(event.get("agent_id") or event.get("agent_type"))


def _in_main_repo(path: str) -> bool:
    path = path.rstrip("/")
    return any(path == repo or path.startswith(repo + "/") for repo in MAIN_REPOS)


def _commit_targets_main(command: str, cwd: str) -> bool:
    for match in _GIT_COMMIT.finditer(command):
        dash_c = re.findall(r"-C\s+(\S+)", match.group(1) or "")
        if dash_c:
            if _in_main_repo(dash_c[-1]):
                return True
            continue
        before = command[:match.start()]
        cds = _CD.findall(before)
        where = cds[-1] if cds else cwd
        if _in_main_repo(where):
            return True
    return False


def decide(event: dict) -> str | None:
    """Deny reason, or None to allow."""
    if not _is_worker(event):
        return None
    if event.get("tool_name") in _WAIT_TOOLS:
        return _WAIT_REASON
    if event.get("tool_name") != "Bash":
        return None
    tool_input = event.get("tool_input") or {}
    command = str(tool_input.get("command", ""))
    for pattern, label in _GIT_FORBIDDEN:
        if pattern.search(command):
            return (f"작업조 규칙: {label} 금지. 작업 트리가 여럿이라 남의 변경을 지우거나 섞는다. "
                    "되돌릴 것은 `git show HEAD:<파일>`로 보고 직접 고치고, 커밋은 "
                    "`git add <경로>` + `git commit -m ... -- <경로>`로만 한다.")
    if _commit_targets_main(command, str(event.get("cwd", ""))):
        return ("작업조 규칙: 엔진 기본 폴더(main)에 직접 커밋 금지. "
                "자기 작업 트리(<apps-root>/<작업조 이름>)에서 커밋하고 반장에게 보고한다.")
    if tool_input.get("run_in_background") or _SLEEP_LOOP.search(command):
        return _WAIT_REASON
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    reason = decide(event)
    if reason:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
