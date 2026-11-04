#!/usr/bin/env python3
# Generated from the private ops workspace at 24c1bb68758c8a552514d06d8280463876278f76; do not edit by hand (see harness/export_ops_public.py).
"""Safety hook: caps which MCP servers a work-copy (사본, `_wt/<app>/<name>`) session may call.

v2 gap item 144 (team-lead 2026-11-02, plan `_ops/plans/2026-11-02-v2-gap-verdicts.md`
행 229): "MCP 서버/도구 역할별 이중 상한 ... 세션 단위로 MCP 도구를 제한하는 장치가 없음" ->
"PreToolUse 훅으로 사본 세션 MCP 도구 명시 제한".

Scoped narrower than the item's own "역할별"(per-role) wording: the PreToolUse payload has no
CONFIRMED field carrying a subagent's role/agent-definition name as a string (grepped every
installed hook - `agent_id`/`agent_type` are only ever used as an EXISTENCE check, e.g.
bansang_habit.py:92, worker_rules.py:42, never read as a value) - inventing a field name would
violate MASTER_RULES §3's "크래시-아웃" (don't code against an unconfirmed external format).
What IS confirmed and already used elsewhere in this bundle (protect_sensitive_files.py,
block_dangerous.py) is the tool call's own `cwd`, so this hook caps by WORK LOCATION instead: a
call whose cwd sits inside a disposable work copy is restricted to the dev-tool MCP servers that
copy's own work plausibly needs (the knowledge-base search and browser automation used across
this bundle's own development); every owner-account-linked integration (Gmail, Calendar, Drive,
Notion, Claude Docs, the 당근 ads server) is denied from a work-copy cwd - those need the OWNER's
own authorization context, not a disposable sub-session's.

사실 확정 필요: true per-ROLE enforcement (a 구현자 session calling a tool outside its own
`~/.claude/agents/구현자.md` `tools:` list) needs that confirmed field first; this hook does not
attempt it.

PreToolUse hook, matcher should include every MCP-capable tool name (`mcp__*`) - in practice
registered with matcher "*" since Claude Code hook matchers don't support a `mcp__` prefix
wildcard reliably across versions; this hook itself no-ops instantly for any non-"mcp__" tool
name, so a broad matcher costs nothing. Fails OPEN on any internal error (a dev-tool access
hiccup is not worth risking blocking ordinary work over) - this is a narrower, additive safety
net, not this bundle's primary defense layer (that is block_dangerous.py/protect_sensitive_files.py,
which both fail CLOSED).
"""
import json
import os
import sys

WORK_COPY_ROOT = "/apps/_wt/"

# MCP server ids (the segment between "mcp__" and the tool's own name - see
# `_server_id` below) a work-copy session may still call. Anything else - every
# owner-account-linked integration (Gmail/Calendar/Drive/Notion/Claude Docs, 당근 ads) - is denied
# from a work-copy cwd.
SAFE_SERVERS_IN_WORK_COPY = {
    "company-library",
    "plugin_playwright_playwright",
    "playwright",
}


def _server_id(tool_name: str) -> str | None:
    if not tool_name.startswith("mcp__"):
        return None
    parts = tool_name.split("__", 2)
    if len(parts) < 2:
        return None
    return parts[1]


def _in_work_copy(cwd: str) -> bool:
    if not cwd:
        return False
    try:
        normalized = os.path.normpath(os.path.expanduser(cwd))
    except Exception:
        return False
    return normalized == WORK_COPY_ROOT.rstrip("/") or normalized.startswith(WORK_COPY_ROOT)


def decide(data: dict) -> str | None:
    tool_name = str(data.get("tool_name", ""))
    server = _server_id(tool_name)
    if not server:
        return None
    cwd = str(data.get("cwd", "") or "")
    if not _in_work_copy(cwd):
        return None
    if server in SAFE_SERVERS_IN_WORK_COPY:
        return None
    return (f"사본 세션({cwd})에서 MCP 서버 '{server}'는 허용 목록 밖이다 — 운영자 계정 연동 도구는 "
            "원 세션에서만 쓴다(v2 공백 항목 144).")


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # fail OPEN - see module docstring
    try:
        reason = decide(data)
    except Exception:
        return 0
    if reason:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
