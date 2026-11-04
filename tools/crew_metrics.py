#!/usr/bin/env python3
# Generated from the private ops workspace at 24c1bb68758c8a552514d06d8280463876278f76; do not edit by hand (see harness/export_ops_public.py).
"""Crew behaviour metrics (2026-11-01, Agent Experience idea: measure what agents actually do before adding more rules —
Microsoft "Best practices are hypotheses until you measure them"; owner: "똑같은 실수를 반복하고 자꾸 누락하고").
2026-11-02 보정 (03:30 자가 점검 지적, reports/self-improve-2026-11-02.md): 어제치 84개 중 55개(general-purpose 42·
fork 9·Explore 3·Plan 1)는 v2 대조·판정표·링크 조사만 하고 앱 파일을 하나도 고치지 않았음 — 사실 카드를 읽을 대상이
아닌데 분모에 들어가 비율이 실제보다 낮게 보였음. 또 DB 쓰기 위험 정규식이 `created_at`/`updated_at` 같은 흔한 컬럼
이름(치환 전 "create"/"update"에 \b 경계가 없어 부분 문자열로도 매칭) 과 `cat scripts/migrate.ts`처럼 그냥 파일을
읽기만 한 명령까지 "DB 쓰기"로 잡았음.

Reads every crew (subagent) transcript changed in the last N hours under ~/.claude/projects/*/*/subagents/*.jsonl and counts,
per crew, the behaviours the fact cards ask for — but only over crews that actually touched an app file (Edit/Write on a
path under /apps/<app>/ or /apps/_wt/<app>/<name>/, or a `git commit` naming such a path in Bash). Research-only
crews (judged/compared/read links, no app file touched) are counted separately, never in the fact-card/lessons denominator:
  - app_file_edit    : touched at least one app file (see above) -- the new denominator
  - read_fact_card   : read /apps/_ops/app-facts/<app>.md (Read tool or cat/sed in Bash)
  - lessons_for      : called the lessons_for tool
  - prod_db_risk     : ran an actual DB-writing command (migrate/seed execution, or psql -c with a real insert/update/
                        delete/create/drop/alter statement) without pointing it at a test DB -- mere mentions (cat'ing a
                        migration file, a SELECT whose columns happen to contain "create"/"update") don't count
  - done_claims      : final-ish messages that say 완료/통과/성공
  - done_with_evidence: of those, how many also carry evidence (numbers like "12/12", "passed", "커밋 abc1234", exit codes)
Writes a JSON line per run to _ops/metrics/crew-metrics.jsonl and prints a short Korean table. Reads transcripts only.
Run: python3 /apps/_ops/tools/crew_metrics.py [hours=24] [at="YYYY-MM-DD HH:MM"]
  at: pretend "now" is this local time instead of the real clock -- lets a re-run reproduce an earlier window exactly
      (same file set as a past logged run) for a before/after comparison.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import time
from pathlib import Path

HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 24.0
_AT = sys.argv[2] if len(sys.argv) > 2 else None
NOW = time.mktime(time.strptime(_AT, "%Y-%m-%d %H:%M")) if _AT else time.time()
ROOT = Path.home() / ".claude" / "projects"
OUT = Path("/apps/_ops/metrics/crew-metrics.jsonl")

FACT_CARD = re.compile(r"_ops/app-facts/[\w.-]+\.md")
# App file paths this crew actually touched: /apps/<app>/... or the worktree copy /apps/_wt/<app>/<name>/...
# -- never _ops or _shared, which are workspace tooling, not an "app" with its own fact card.
APP_PATH = re.compile(r"/apps/(?:_wt/[\w.-]+/[\w.-]+|(?!_ops\b|_shared\b|_wt\b)[\w.-]+)/")
READ_ONLY_PROG = {"cat", "head", "tail", "less", "more", "sed", "grep", "awk", "vim", "nano", "ls", "wc", "diff", "echo", "cp", "mv", "find"}
EXEC_PROG_FOR_MIGRATE = {"node", "npx", "ts-node", "bash", "sh", "npm"}
TEST_DB_HINT = re.compile(r"(_test\b|mops_test|TEST_DATABASE_URL|DATABASE_URL=\S*_test|setup-test-db|db:setup-test)", re.I)
DONE_WORD = re.compile(r"(완료|통과|성공|끝냈|끝났)")
EVIDENCE = re.compile(r"(\d+\s*/\s*\d+|\d+\s*passed|passed\s*\(\d+\)|tests?\s+\d+|커밋\s*`?[0-9a-f]{7}|exit\s*=?\s*0|tsc[^\n]{0,20}0|eslint[^\n]{0,20}0|\b[0-9a-f]{7,8}\b)", re.I)


def _first_prog(sub: str) -> str:
    """The program name a shell sub-command actually runs (after optional `timeout N`)."""
    m = re.match(r"^\s*(?:timeout\s+\d+\s+)?(\S+)", sub)
    return os.path.basename(m.group(1)) if m else ""


# A heredoc opener (`<<'EOF'`, `<<"EOF"`, `<<EOF`, `<<-EOF`) -- its body (up to the line
# that is exactly the token) is never a shell command, but Python/regex source written
# there (adev-metrics-5008007de1559b63, 2026-11-03: a diagnostic script's own regex
# literal `...|npm run seed|...` was split on its `|` alternation and the fragment
# "npm run seed" read as an executed command) can contain program names, `|`, `;`, `&&`
# as plain text. Strip heredoc bodies before splitting into sub-commands.
HEREDOC_OPEN_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")


def _strip_heredocs(cmd: str) -> str:
    lines = cmd.split("\n")
    out = []
    i = 0
    while i < len(lines):
        out.append(lines[i])
        m = HEREDOC_OPEN_RE.search(lines[i])
        if m:
            token = m.group(2)
            i += 1
            while i < len(lines) and lines[i].strip() != token:
                i += 1
            i += 1  # skip the closing token line too (not a command)
            continue
        i += 1
    return "\n".join(out)


def is_db_write_command(cmd: str) -> bool:
    """True only for a sub-command that actually EXECUTES a DB write -- not a bare mention
    (cat'ing scripts/migrate.ts) and not a SELECT whose column names happen to contain
    "create"/"update"/"delete" as a substring (created_at, updated_at, deleted_at), and
    not text sitting inside a heredoc body (source code, commit messages, ...)."""
    cmd = _strip_heredocs(cmd)
    for sub in re.split(r"&&|\|\||;|\|", cmd):
        sub = sub.strip()
        if not sub:
            continue
        prog = _first_prog(sub)
        if prog in READ_ONLY_PROG:
            continue
        if prog == "psql":
            if re.search(r"\b(insert|update|delete|create|drop|alter)\b", sub, re.I):
                return True
        elif prog == "npm":
            if re.search(r"\brun\s+(-s\s+)?(db:)?migrate\b|\brun\s+seed\b", sub, re.I):
                return True
        elif prog in EXEC_PROG_FOR_MIGRATE:
            if re.search(r"\bdb:migrate\b|scripts/migrate\.ts|\bseed\b", sub, re.I):
                return True
    return False


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    parts.append(c.get("text", ""))
                elif c.get("type") == "tool_use":
                    parts.append(json.dumps(c.get("input", {}), ensure_ascii=False))
        return "\n".join(parts)
    return ""


def scan(path: str) -> dict:
    m = {"crew": Path(path).stem.replace("agent-", ""), "read_fact_card": False, "lessons_for": False,
         "app_file_edit": False, "prod_db_risk": 0, "done_claims": 0, "done_with_evidence": 0, "tool_calls": 0}
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        msg = d.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        for c in msg.get("content") or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use":
                m["tool_calls"] += 1
                name = c.get("name", "")
                inp_dict = c.get("input", {}) or {}
                inp = json.dumps(inp_dict, ensure_ascii=False)
                if FACT_CARD.search(inp) and (name in ("Read",) or re.search(r"\b(cat|sed|head|less)\b", inp)):
                    m["read_fact_card"] = True
                if "lessons_for" in name:
                    m["lessons_for"] = True
                if name in ("Edit", "Write") and APP_PATH.search(str(inp_dict.get("file_path", ""))):
                    m["app_file_edit"] = True
                if name == "Bash":
                    cmd = inp_dict.get("command", "")
                    # "git commit" or "git -C <wt> commit" (the wt-land/구현자 convention puts -C before commit)
                    if re.search(r"\bgit\b[^\n]*\bcommit\b", cmd) and APP_PATH.search(cmd):
                        m["app_file_edit"] = True
                    if is_db_write_command(cmd) and not TEST_DB_HINT.search(cmd):
                        m["prod_db_risk"] += 1
            elif c.get("type") == "text":
                t = c.get("text", "")
                if len(t) > 200 and DONE_WORD.search(t):
                    m["done_claims"] += 1
                    if EVIDENCE.search(t):
                        m["done_with_evidence"] += 1
    return m


def main() -> int:
    cutoff = NOW - HOURS * 3600
    # upper-bound by NOW too -- without it, a historical --at re-run (for a before/after comparison) would also
    # pick up files touched *after* that moment (the session kept writing to them later), widening the window.
    files = [f for f in glob.glob(str(ROOT / "*" / "*" / "subagents" / "*.jsonl")) if cutoff <= os.path.getmtime(f) <= NOW]
    rows = [scan(f) for f in files]
    rows = [r for r in rows if r["tool_calls"] > 0]
    total = len(rows)
    app_rows = [r for r in rows if r["app_file_edit"]]
    research_rows = [r for r in rows if not r["app_file_edit"]]
    n = len(app_rows)
    summary = {
        "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(NOW)), "hours": HOURS,
        "crews_total": total, "crews_app_edit": n, "crews_research_only": len(research_rows),
        "read_fact_card": sum(r["read_fact_card"] for r in app_rows),
        "lessons_for": sum(r["lessons_for"] for r in app_rows),
        "prod_db_risk_crews": sum(1 for r in rows if r["prod_db_risk"]),
        "done_claims": sum(r["done_claims"] for r in rows),
        "done_with_evidence": sum(r["done_with_evidence"] for r in rows),
        "risky": [r["crew"] for r in rows if r["prod_db_risk"]][:20],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")
    pct = lambda a, b: f"{a}/{b}" + (f" ({round(100 * a / b)}%)" if b else "")
    print(f"작업조 행동 측정 — 최근 {HOURS:g}시간, 작업조 {total}개(앱 수정 {n}개, 조사만 {len(research_rows)}개)")
    print(f"- 사실 카드 읽음(분모: 앱 수정 작업조): {pct(summary['read_fact_card'], n)}")
    print(f"- 지난 실수(lessons_for) 조회(분모: 앱 수정 작업조): {pct(summary['lessons_for'], n)}")
    print(f"- 시험 DB 표시 없이 DB 쓰기 명령 실행: 작업조 {summary['prod_db_risk_crews']}개 {summary['risky']}")
    print(f"- 「완료·통과」 보고 중 증거(숫자·커밋) 있음: {pct(summary['done_with_evidence'], summary['done_claims'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
