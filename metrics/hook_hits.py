#!/usr/bin/env python3
"""Count hook blocks/denials in Claude Code transcripts (stdlib only).

usage: python3 hook_hits.py --since YYYY-MM-DD --until YYYY-MM-DD [--out file.json]
       [--root <transcripts dir, default from gongbang.config.json>]

Window is UTC, `since` inclusive, `until` exclusive (record "timestamp" field).
Only counts and labels are written - never message bodies.

Where each hook's denial shows up in a transcript:
  PreToolUse deny / exit 2  -> "user" record, message.content[] tool_result (is_error) whose
                               text starts "PreToolUse:<Tool> hook error: ..."
        scope_guard.py deny  -> "...hook error: 대표 승인 필요: ..." (approval gate closed)
                                "...hook error: 차단: ..."          (always-block, no gate possible)
        block_dangerous.py   -> "...hook error: [python3 .../block_dangerous.py]: BLOCKED: ..."
  Stop block                -> "attachment" record, attachment.type == "hook_blocking_error"
        verify_claims.py     -> blockingError contains "완료·통과·성공 보고에 이 턴의 실행 근거"
  Non-blocking hook failure -> attachment.type == "hook_non_blocking_error" (counted separately)
approve_gate.py itself never blocks (UserPromptSubmit, prints only); its gate is enforced by
scope_guard.py.
"""
import argparse
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/hooks")
import gongbang_config  # noqa: E402

PRE_RE = re.compile(r"^(PreToolUse|PostToolUse|UserPromptSubmit):(\S*) hook error: (.*)", re.S)
SCRIPT_RE = re.compile(r"^\[([^\]]*)\]: ")
VERIFY_CLAIMS = "완료·통과·성공 보고에 이 턴의 실행 근거"
SCOPE_GATE = "대표 승인 필요:"
SCOPE_ALWAYS = "차단:"


def parse_day(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def parse_ts(s: object) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def script_label(cmd: str) -> str:
    m = re.search(r"([\w.-]+\.(?:py|sh|js|mjs|ts))", cmd)
    return m.group(1).rsplit(".", 1)[0] if m else "unknown-command"


def classify_pre(body: str) -> str:
    m = SCRIPT_RE.match(body)
    if m:
        return script_label(m.group(1))
    if body.startswith(SCOPE_GATE) or body.startswith(SCOPE_ALWAYS):
        return "scope_guard"
    return "unknown"


def scan(root: str, since: datetime, until: datetime) -> dict:
    counts = Counter()
    non_blocking = Counter()
    per_day = defaultdict(Counter)
    for path in glob.glob(os.path.join(root, "*", "*.jsonl")):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    ts = parse_ts(rec.get("timestamp"))
                    if not ts or not (since <= ts < until):
                        continue
                    day = ts.date().isoformat()
                    if rec.get("type") == "user":
                        content = (rec.get("message") or {}).get("content")
                        if isinstance(content, list):
                            for c in content:
                                if not (isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error")):
                                    continue
                                text = c.get("content")
                                if isinstance(text, list):
                                    text = "\n".join(str(t.get("text", "")) for t in text if isinstance(t, dict))
                                m = PRE_RE.match(str(text or ""))
                                if m:
                                    label = classify_pre(m.group(3))
                                    counts[label] += 1
                                    per_day[day][label] += 1
                    elif rec.get("type") == "attachment":
                        att = rec.get("attachment") or {}
                        if att.get("type") == "hook_blocking_error":
                            counts["verify_claims" if VERIFY_CLAIMS in str(att.get("blockingError", "")) else "stop_block"] += 1
                            per_day[day]["stop_block"] += 1
                        elif att.get("type") == "hook_non_blocking_error":
                            non_blocking["non_blocking"] += 1
        except Exception:
            continue
    return {"blocks_by_hook": dict(counts), "non_blocking": dict(non_blocking),
            "per_day": {d: dict(c) for d, c in per_day.items()}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--out")
    ap.add_argument("--root")
    args = ap.parse_args()
    cfg = gongbang_config.load()
    root = args.root or cfg["transcripts_root"]
    result = scan(root, parse_day(args.since), parse_day(args.until))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
