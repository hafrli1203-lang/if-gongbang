#!/usr/bin/env python3
"""Stop hook (notify-only, never blocks): two non-blocking nudges for the lead (반장).

Owner directives, per the [date] 전수조사 (both "문서만(막는 장치 없음)" until now):
  G13 "다른 일도 진행하고 있지?" (반복 3회) -- when the turn is about to end right after
      dispatching something async (a backgrounded Bash, or an Agent/SendMessage call) with no
      result back in the transcript yet, remind the lead to pick up a non-overlapping next item
      from WORK_QUEUE.md while waiting, instead of just ending the turn to wait on it.
  G05 "세부 계획은 네이티브로 하자 문제가 있을경우 전환을 해야하는것도 기억해라" + WORKFLOW_RULES
      §5 ("검증 실패·회귀·반복 지적이 생기면 subagent-driven으로 바꾼다") -- when the SAME Bash
      command has failed on its last RUNS_TO_FLAG (3) consecutive attempts, remind the lead that
      this already-written rule calls for switching to a subagent-driven approach. 3 is not a new
      number invented here -- it is "검증 실패 2회 더"/"3회째" read directly off WORKFLOW_RULES §5's
      own wording ("반복"); it is a nudge, not a hard gate, so getting it wrong costs nothing.
Both only ever return a systemMessage, never {"decision": "block"} -- unlike directive_check_hook.py,
this hook cannot stop the turn. Off switch (owner only): ~/.claude/gate/orbit-nudge-off.
Fails open on any error.
"""
from __future__ import annotations

import json
import os
import re
import sys

OFF = os.path.expanduser("~/.claude/gate/orbit-nudge-off")
TAIL_BYTES = 8 * 1024 * 1024
RUNS_TO_FLAG = 3
FAIL_MARKERS = ("FAILED", "Traceback", "AssertionError", "Error:", "exit code 1", "ERROR", "rc=1", "rc!=0")
G13_MSG = ("[알림] 뒤에서 도는 작업이 있고 결과를 기다리는 채로 턴이 끝나려 한다. 기다리는 동안 "
           "WORK_QUEUE.md·남은 일 목록에서 겹치지 않는 다음 일을 같이 보라(운영자 \"다른 일도 진행하고 있지?\").")
G05_MSG = ("[알림] 같은 시험 명령이 {n}회째 실패했다. WORKFLOW_RULES §5: 검증 실패·회귀·반복 지적이 "
           "생기면 subagent-driven으로 전환한다.")


def tail_records(path: str) -> list[dict]:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        data = f.read().decode("utf-8", errors="replace")
    lines = data.splitlines()[1:] if size > TAIL_BYTES else data.splitlines()
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _tool_uses(records: list[dict]) -> list[tuple[str, str, dict, int]]:
    """[(tool_use_id, name, input, record_index)] in order, main session only."""
    out = []
    for i, rec in enumerate(records):
        if rec.get("type") != "assistant" or rec.get("isSidechain"):
            continue
        for c in (rec.get("message") or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                out.append((str(c.get("id") or ""), str(c.get("name", "")), c.get("input") or {}, i))
    return out


def _result_for(records: list[dict], tool_use_id: str, after_index: int) -> tuple[bool, str] | None:
    """(is_failure, text) for the tool_result matching tool_use_id after after_index, or None if
    no result has come back yet (still pending -- the G13 signal)."""
    for rec in records[after_index + 1:]:
        if rec.get("type") != "user":
            continue
        for c in (rec.get("message") or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") == tool_use_id:
                content = c.get("content")
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                return bool(c.get("is_error")) or any(m in text for m in FAIL_MARKERS), text
    return None


_LAUNCH_ID = re.compile(r"(?:running in background with ID: |agentId: )([A-Za-z0-9]+)")
_DONE_ID = re.compile(r"<task-id>([A-Za-z0-9]+)</task-id>.*?<status>(?:completed|failed|killed)</status>", re.S)


def _record_text(rec: dict) -> str:
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if not isinstance(c, dict):
            continue
        body = c.get("content", c.get("text", ""))
        parts.append(body if isinstance(body, str) else json.dumps(body, ensure_ascii=False))
    return "\n".join(parts)


def pending_background(records: list[dict]) -> set[str]:
    """Ids of background work (a backgrounded Bash or an async Agent) that was launched and has
    no completion notice yet. A launch answers at once ("running in background with ID: X" /
    "agentId: X"), so "no tool_result yet" never happens in practice -- seen live [date]:
    the first version of this check could never fire."""
    launched: list[str] = []
    done: set[str] = set()
    for rec in records:
        if rec.get("isSidechain"):
            continue
        if rec.get("type") == "user":
            text = _record_text(rec)
            launched.extend(_LAUNCH_ID.findall(text))
            done.update(_DONE_ID.findall(text))
        elif isinstance(rec.get("content"), str):
            # completion notices arrive as "queue-operation" records with a plain content string
            done.update(_DONE_ID.findall(rec["content"]))
    return {i for i in launched if i not in done}


def g13_hit(records: list[dict]) -> bool:
    return bool(pending_background(records))


def g05_hit(records: list[dict]) -> int:
    """Length of the current losing streak of the SAME Bash command (most recent attempt first), 0 if none."""
    uses = [(tid, inp, idx) for tid, name, inp, idx in _tool_uses(records) if name == "Bash"]
    if not uses:
        return 0
    last_cmd = str(uses[-1][1].get("command", ""))
    if not last_cmd:
        return 0
    streak = 0
    for tid, inp, idx in reversed(uses):
        if str(inp.get("command", "")) != last_cmd:
            break
        res = _result_for(records, tid, idx)
        if res is None or not res[0]:
            break
        streak += 1
    return streak


def decide(data: dict) -> dict | None:
    if data.get("stop_hook_active") or os.path.exists(OFF):
        return None
    path = str(data.get("transcript_path") or "")
    if not path or not os.path.exists(path):
        return None
    records = tail_records(path)
    msgs = []
    streak = g05_hit(records)
    if streak >= RUNS_TO_FLAG:
        msgs.append(G05_MSG.format(n=streak))
    if g13_hit(records):
        msgs.append(G13_MSG)
    if not msgs:
        return None
    return {"systemMessage": " / ".join(msgs)}


def main() -> int:
    try:
        out = decide(json.load(sys.stdin))
    except Exception:
        return 0
    if out:
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
