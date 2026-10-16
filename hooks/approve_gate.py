#!/usr/bin/env python3
"""UserPromptSubmit hook: prints what the owner's approval syntax was recognized as.

This hook does not DECIDE anything and does not write any gate file - a written gate file can
always be forged by some Bash/Edit/Write incantation, so a file can never be the thing
scope_guard.py trusts. The actual authority lives in scope_guard.py, which re-reads the
owner's own chat transcript for THIS session_id directly (see fence_parse.py) every time a
gated tool call happens. This hook is purely an acknowledgement for the owner: it tells them
whether what they just typed will register as an approval once scope_guard.py replays the
transcript.

Recognized forms: `승인 <TASK>` (opens the gate for this session, labeled with that name),
`승인 전체` (opens it for anything in this session), `승인 취소` / `승인 해제` (closes it).
Several forms in one message are all recognized, in the order they appear. A negated form
("승인 안 함", or a quoted/instructional mention of the syntax) is printed as not recognized.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fence_parse  # noqa: E402
import gongbang_config  # noqa: E402


def handle(prompt: str, session_id: str, cfg: dict) -> list[str]:
    """Returns the lines to print (possibly empty - stay silent if nothing approval-shaped is
    in this message)."""
    events, rejected = fence_parse.parse_events(prompt)
    if not events and not rejected:
        return []

    opened_here = [name for kind, name in events if kind == "open"]
    closed_here = any(kind == "close" for kind, _ in events)
    hours = float(cfg.get("approval_window_seconds") or 4 * 3600) / 3600

    # Best-effort informational "final state" line: replay the session's recent transcript
    # (if it can be found) plus this message, so the owner sees the cumulative result. This
    # is purely informational - scope_guard.py is the one that actually enforces it, and it
    # always re-derives this fresh rather than trusting this hook's output.
    final_open: set[str] | None = None
    if session_id:
        ok, _ = fence_parse.compute_open_tasks(session_id, cfg)
        if ok:
            prior = fence_parse.read_recent_human_messages(session_id, cfg) or []
            final_open = fence_parse.replay(prior + [(float("inf"), prompt)])

    lines: list[str] = []
    if opened_here:
        suffix = f" (최종 열림: {', '.join(sorted(final_open))})" if final_open else ""
        lines.append(
            f"승인 관문 열림: {', '.join(opened_here)} (이 세션, {hours:g}시간). "
            f"되돌리기 어려운 명령은 scope_guard.py가 세션 대화 기록으로 다시 확인한다.{suffix}"
        )
    if closed_here:
        lines.append("승인 관문 닫힘(이 메시지 기준, 이 세션 한정).")
    if rejected and not opened_here and not closed_here:
        lines.append(f"승인으로 보지 않음: {', '.join(rejected)} (부정어 포함).")
    return lines


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    prompt = str(data.get("prompt", ""))
    # Relayed subagent/session messages and notifications reach this hook too; they are never approvals.
    head = prompt.lstrip()[:200]
    if head.startswith(("Another Claude session sent a message", "<")) or "<agent-message" in head:
        return 0
    session_id = str(data.get("session_id", "") or "")
    cfg = gongbang_config.load()

    for line in handle(prompt, session_id, cfg):
        print(line)
    if not session_id:
        events, rejected = fence_parse.parse_events(prompt)
        if events or rejected:
            print("참고: session_id 없음 - scope_guard.py가 이 세션을 식별하지 못해 관문을 열 수 없다.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
