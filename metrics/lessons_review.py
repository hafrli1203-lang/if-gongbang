#!/usr/bin/env python3
"""Weekly L1 (lesson log) report for the owner - read-only.

Counts, per app, lessons by status and promotion candidates (same rule the SessionStart hook
uses: distinct sessions >= --min within the lesson's own sessions_seen list). Promotion itself
is the owner's word only ("승격 L1-번호"); this script never changes anything.

usage: python3 lessons_review.py [--min 3]
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/hooks")
import gongbang_config  # noqa: E402


def load_all(lessons_dir: str) -> list[dict]:
    rows = []
    for path in glob.glob(os.path.join(lessons_dir, "*.jsonl")):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", dest="min_sessions", type=int, default=3)
    args = ap.parse_args()
    cfg = gongbang_config.load()
    rows = load_all(cfg["lessons_dir"])

    by_app: dict = {}
    for r in rows:
        app = r.get("app", "workspace")
        s = by_app.setdefault(app, {"total": 0, "promoted": 0, "rejected": 0, "retired": 0, "candidates": []})
        s["total"] += 1
        status = r.get("status", "candidate")
        if status in ("promoted", "rejected", "retired"):
            s[status] += 1
        sessions = len(set(r.get("sessions_seen", [])))
        if status == "candidate" and sessions >= args.min_sessions:
            s["candidates"].append(f'{r.get("number")}({sessions}세션): {str(r.get("rule", ""))[:60]}')

    print(f"작업 일지 주간 보고 — 기준: 서로 다른 세션 {args.min_sessions}번 이상")
    print(f"전체 {len(rows)}건")
    for app, s in sorted(by_app.items()):
        print(f"- {app}: {s['total']}건 (승격 {s['promoted']} · 기각 {s['rejected']} · 내림 {s['retired']})")
        for c in s["candidates"]:
            print(f"    승격 후보 {c}")
    if not rows:
        print("(아직 기록된 경험이 없다)")
    print("승격은 대표가 '승격 L1-번호', 기각은 '기각 L1-번호'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
