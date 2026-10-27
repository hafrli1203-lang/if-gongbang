#!/usr/bin/env python3
"""Stop hook: a completion report must be checked against the owner's own words.

Owner [date]: "지금 니가 내 지시를 제대로 안듣는건 어떻게 검증을 못하나?"
When the final assistant text of a turn reports work as done (same claim words as verify-claims.py), it must
carry a "지시 대조" section that quotes at least one fragment of the owner's own directive verbatim (in double
quotes or 「」), so the report is laid next to what was actually asked. A quote that is not found word for word
in an owner prompt of this session does not count -- paraphrase is what this hook exists to stop.
Main session only (subagents end with SubagentStop). Guarded against loops via stop_hook_active.
Fails open on any error. Off switch (owner only): ~/.claude/gate/directive-check-off.
"""
import json
import os
import re
import sys

OFF = os.path.expanduser("~/.claude/gate/directive-check-off")
TAIL_BYTES = 8 * 1024 * 1024
HEADING = "지시 대조"
CLAIM = re.compile(r"(완료했|완료됐|완료되었|작업 완료|통과했|통과됐|테스트 통과(?!하지|\s*못|\s*실패)|빌드 성공|배포 성공|정상 동작|정상 작동|성공했|성공적으로|다 고쳤|모두 고쳤|끝냈)")
QUOTE = re.compile(r"\"([^\"\n]{2,})\"|“([^”\n]{2,})”|「([^」\n]{2,})」")
REASON = ("완료 보고에 '지시 대조'가 없다. 운영자가 이번 일에 쓴 지시 원문을 큰따옴표로 글자 그대로 옮기고, 그 지시마다 "
          "무엇을 했는지·못 했는지를 나란히 적은 '지시 대조' 절을 넣어 다시 답한다(원문은 대화에 있는 그대로, 요약 금지).")


def tail_records(path: str) -> list[dict]:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        data = f.read().decode("utf-8", errors="replace")
    lines = data.splitlines()
    if size > TAIL_BYTES:
        lines = lines[1:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def human_text(rec: dict) -> str | None:
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isCompactSummary"):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or not content or not all(isinstance(c, dict) for c in content):
        return None
    if any(c.get("type") == "tool_result" for c in content):
        return None
    texts = [str(c.get("text", "")) for c in content if c.get("type") == "text"]
    return "\n".join(texts) if texts else None


def split_turn(records: list[dict]) -> tuple[str, list[str]]:
    """(the turn's final answer -- assistant text after its last tool call --, every owner prompt in the tail)."""
    owner: list[str] = []
    turn_text: list[str] = []
    in_final = True
    for rec in reversed(records):
        text = human_text(rec)
        if text is not None:
            owner.append(text)
            in_final = False
            continue
        if not in_final or rec.get("type") not in ("assistant", "user"):
            continue
        blocks = (rec.get("message") or {}).get("content") or []
        if not isinstance(blocks, list):
            continue
        if any(isinstance(c, dict) and c.get("type") in ("tool_use", "tool_result") for c in blocks):
            in_final = False
            continue
        for c in reversed(blocks):
            if isinstance(c, dict) and c.get("type") == "text":
                turn_text.append(str(c.get("text", "")))
    return "\n".join(reversed(turn_text)), owner


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def quotes_owner(final: str, owner: list[str]) -> bool:
    pool = [_norm(o) for o in owner]
    for m in QUOTE.finditer(final):
        frag = _norm(next(g for g in m.groups() if g))
        if any(frag in o for o in pool):
            return True
    return False


def decide(data: dict) -> dict | None:
    if data.get("stop_hook_active") or os.path.exists(OFF):
        return None
    path = str(data.get("transcript_path") or "")
    if not path or not os.path.exists(path):
        return None
    final, owner = split_turn(tail_records(path))
    if not CLAIM.search(final):
        return None
    if HEADING in final and quotes_owner(final, owner):
        return None
    return {"decision": "block", "reason": REASON}


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
