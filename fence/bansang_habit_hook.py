#!/usr/bin/env python3
"""반장 습관 차단 훅 (PreToolUse, matcher "Bash|Edit|Write").

IF 공방 v2 설계 7절 4번·13절: 반장이 스킬·곳간 대신 터미널로 때우거나,
궤도·작업조에 넘기지 않고 혼자 계속 손으로 하는 것을 막는다.

규칙
A. Bash 비율: 이 세션 본 대화의 최근 WINDOW개 도구 호출 중 Bash가 RATIO를
   넘고, 그 안에 스킬 호출(Skill)·스킬 창고 읽기·곳간 검색이 하나도 없으면
   Bash를 막는다. 스킬이나 곳간을 한 번 쓰면 풀린다.
B. 반장 호출 상한: 운영자의 마지막 메시지 뒤로 본 대화의 Bash/Edit/Write 호출이
   CAP을 넘으면 막는다. 일을 궤도(gb2)·작업조(Agent)에 넘기라는 뜻이다.
   Agent·SendMessage·Skill·Read·Grep·Glob·MCP 조회는 막지 않는다.

막을 때는 deny JSON을 내고 exit 0 (다른 집행 훅과 같은 방식).
작업조(서브 에이전트)의 호출은 세지 않는다(isSidechain).

B4-2 검토([date] 공방울타리 작업): "읽기 전용 Bash(조회)는 상한 50회에서 빼는지" 검토 결과,
빼지 않기로 함. 운영자 지시 G31("설계와 반장 습관 차단은 왜 안되어 있어")은 반장이 손으로 때우는
것을 더 세게 막으라는 지시이고, 조회성 Bash(ls·cat·grep 등)도 "스킬·곳간 대신 터미널로 때우는"
습관의 일부다. 완화하면 그 습관 차단이 약해지므로 COUNTED는 그대로 둔다.
"""
from __future__ import annotations

import json
import sys

WINDOW = 40
MIN_CALLS = 20
RATIO = 0.40
CAP = 50
COUNTED = {"Bash", "Edit", "Write", "MultiEdit"}


def _skillish(name: str, tool_input: dict) -> bool:
    if name == "Skill":
        return True
    if name.startswith("mcp__company-library__"):
        return True
    if name == "Read" and "skills-library" in str(tool_input.get("file_path", "")):
        return True
    return False


def _is_owner_message(entry: dict) -> bool:
    if entry.get("type") != "user" or entry.get("isSidechain"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return not content.lstrip().startswith("<")
    if isinstance(content, list):
        has_text = any(isinstance(b, dict) and b.get("type") == "text"
                       and not str(b.get("text", "")).lstrip().startswith("<")
                       for b in content)
        has_result = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
        return has_text and not has_result
    return False


def _calls(transcript_path: str) -> tuple[list[tuple[str, dict]], int]:
    """(main-session tool calls in order, index of the first call after the
    last owner message)."""
    calls: list[tuple[str, dict]] = []
    since = 0
    try:
        with open(transcript_path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if _is_owner_message(entry):
                    since = len(calls)
                    continue
                if entry.get("type") != "assistant" or entry.get("isSidechain"):
                    continue
                for block in (entry.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        calls.append((str(block.get("name", "")), block.get("input") or {}))
    except OSError:
        return [], 0
    return calls, since


def decide(event: dict) -> str | None:
    """Deny reason, or None to allow."""
    tool = str(event.get("tool_name", ""))
    if tool not in COUNTED:
        return None
    # A worker (subagent) call carries its own agent identity; this hook
    # only governs the lead's own hands, never a worker's.
    if event.get("agent_id") or event.get("agent_type"):
        return None
    calls, since = _calls(str(event.get("transcript_path", "")))
    recent = calls[-WINDOW:]
    if tool == "Bash" and len(recent) >= MIN_CALLS:
        bash = sum(1 for name, _ in recent if name == "Bash")
        if bash / len(recent) > RATIO and not any(_skillish(n, i) for n, i in recent):
            return (f"반장 습관 차단: 최근 {len(recent)}회 중 Bash {bash}회"
                    f"({bash * 100 // len(recent)}%), 스킬·곳간 사용 0회. "
                    "skill-library 스킬로 이 일에 맞는 스킬을 찾아 쓰거나 곳간을 검색한 뒤 다시 하라.")
    hands = sum(1 for name, _ in calls[since:] if name in COUNTED)
    if hands >= CAP:
        return (f"반장 습관 차단: 운영자의 마지막 지시 뒤로 반장이 직접 Bash/Edit/Write를 {hands}회 했다"
                f"(상한 {CAP}). 남은 일은 궤도(gb2)나 작업조(Agent)에 넘기고, 결과를 받아 보고하라.")
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
