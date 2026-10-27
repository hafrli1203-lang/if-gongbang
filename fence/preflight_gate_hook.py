#!/usr/bin/env python3
"""PreToolUse hook (Edit | Write | MultiEdit | NotebookEdit | Bash): no app-code change before a pre-change report.

WORKFLOW_RULES §2: before changing code, one assistant message in the current turn (after the last human prompt)
must carry all four headings: 읽은 자료 / 예상 위험 / 바꿀 파일 / 되돌리는 법.
Applies only to files inside a git repo under <apps-root>/<app>/ -- the app folder itself, or a git worktree
below it such as <apps-root>/<app>-wt/<name>/ (not docs, *.md, <ops>, scratch).
MODE "block" (owner [date] "1,2,3 다 승인한다", gate 공방울타리): denies. The first week ran as "warn".
Off switch (owner only): ~/.claude/gate/preflight-off. Fails open on any error.
Proposal: <apps-root>/<ops>/proposed-hooks/preflight-gate.md
"""
import json
import os
import re
import sys
import time
from datetime import datetime

MODE = "block"
APPS = "<apps-root>/"
OFF = os.path.expanduser("~/.claude/gate/preflight-off")
LOG = os.path.expanduser("~/.claude/logs/preflight-gate.jsonl")
HEADINGS = ("읽은 자료", "예상 위험", "바꿀 파일", "되돌리는 법")
TAIL_BYTES = 8 * 1024 * 1024
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
EXEMPT_APPS = ("<ops>",)
OTHER_WRITE_SIGNAL = re.compile(
    r"\btee\b|\bsed\s+(-[a-zA-Z]*i|--in-place)|\bperl\s+-[a-zA-Z]*i|\b(mv|cp|rm|touch|truncate|patch)\s"
    r"|\bgit\s+(apply|checkout|restore|am)\b|write_text\(|open\([^)]*['\"][wa]['\"]"
)
# A bare `>`/`>>` only counts as writing APP CODE when its target is a LITERAL <apps-root>/ path.
# A variable target ("> $S/log", "> \"$VAR/x\"") is not app code and must not fall back to judging
# by cwd -- B4-1 ([date] harness audit): this used to flag any redirect with no literal target
# by assuming the cwd, which wrongly caught scratch-log redirects written through a shell variable.
REDIRECT_TO_APP = re.compile(r"(?<![<>&0-9])>>?(?!&)\s*\"?'?<apps-root>/")
ABS_PATH = re.compile(r"<apps-root>/[^\s'\"`;|&)]+")
# A redirection whose target is an absolute path outside <apps-root> (scratch logs, /dev/null) writes no app code.
OUTSIDE_REDIRECT = re.compile(r"(?<![<>&])(?:[0-9]|&)?>>?\s*(?!<apps-root>/)/[^\s;|&)]*")
REASON = ("착수 전 보고 필요: 앱 코드를 고치기 전에 네 가지를 먼저 쓴다 — 읽은 자료(파일:줄) / 예상 위험(병목 포함) / "
          "바꿀 파일 / 되돌리는 법 (WORKFLOW_RULES §2). 운영자에게 보이는 글로 쓰고, 같은 내용을 "
          "{path} 에도 Write로 저장한 뒤 다시 시도한다.")
# Text written in the same assistant message as a tool call is not in the transcript yet when this hook runs
# (seen live [date]), so the report may also be written to this per-session file during the turn.
REPORT_DIR = os.path.expanduser("~/.claude/preflight")
UNSAFE_ID = re.compile(r"[/\\]|\.\.")
# The foreman and its sub-agents (workers) share one session_id, so they would overwrite one
# another's report file (observed live [date]: a worker's report replaced the foreman's,
# blocking the foreman again). A sub-agent call carries agent_id; the foreman's PreToolUse events
# do not. Workers get their own <session>-<agent_id>.md; the foreman keeps <session>.md.


def report_path(session: str, agent_id: str) -> str | None:
    """The report file this caller (foreman, or the named worker) must write. None if unsafe."""
    if not session or UNSAFE_ID.search(session):
        return None
    if agent_id and UNSAFE_ID.search(agent_id):
        return None
    name = f"{session}-{agent_id}.md" if agent_id else f"{session}.md"
    return os.path.join(REPORT_DIR, name)


def app_repo(path: str) -> str | None:
    """Return the git root (app folder or a worktree below it) if path sits in one under <apps-root>/<app>/."""
    p = os.path.normpath(path)
    if not p.startswith(APPS):
        return None
    app = p[len(APPS):].split("/", 1)[0]
    if not app or app in EXEMPT_APPS:
        return None
    top = APPS + app
    cur = p
    while cur.startswith(top):
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        if cur == top:
            break
        cur = os.path.dirname(cur)
    return None


def exempt(path: str) -> bool:
    p = os.path.normpath(path)
    return p.endswith(".md") or "/docs/" in p


def targets_for(tool: str, inp: dict, cwd: str) -> list[str]:
    if tool in EDIT_TOOLS:
        p = str(inp.get("file_path") or inp.get("notebook_path") or "")
        cands = [p] if p else []
    elif tool == "Bash":
        cmd = OUTSIDE_REDIRECT.sub(" ", str(inp.get("command", "")))
        has_app_redirect = bool(REDIRECT_TO_APP.search(cmd))
        has_other_signal = bool(OTHER_WRITE_SIGNAL.search(cmd))
        if not has_app_redirect and not has_other_signal:
            return []
        cands = ABS_PATH.findall(cmd)
        if not cands and has_other_signal and cwd:
            cands = [os.path.join(cwd, "_")]  # relative targets of mv/rm/sed -i/...: judge by the working directory
    else:
        return []
    return [c for c in cands if app_repo(c) and not exempt(c)]


def is_human(rec: dict) -> bool:
    if rec.get("type") != "user" or rec.get("isMeta"):
        return False
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    return bool(content) and isinstance(content, list) and all(isinstance(c, dict) and c.get("type") == "text" for c in content)


def tail_lines(path: str) -> list[str]:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        data = f.read().decode("utf-8", errors="replace")
    lines = data.splitlines()
    return lines[1:] if size > TAIL_BYTES else lines


def prompt_text(rec: dict) -> str:
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    return "\n".join(str(c.get("text", "")) for c in content or [] if isinstance(c, dict))


def _last_human_time(transcript: str) -> float | None:
    for line in reversed(tail_lines(transcript)):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if is_human(rec):
            stamp = str(rec.get("timestamp") or "")
            try:
                return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
            except Exception:
                return None
    return None


def report_file_ok(session: str, agent_id: str, transcript: str) -> bool:
    """This caller's own report file holds all four headings and was written after the last human prompt."""
    path = report_path(session, agent_id)
    if not path:
        return False
    try:
        mtime = os.path.getmtime(path)
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return False
    since = _last_human_time(transcript)
    return since is not None and mtime >= since and all(h in text for h in HEADINGS)


def reported(transcript: str) -> bool:
    """True if one assistant text block after the last human prompt contains all four headings, or the prompt
    itself does (a subagent's task prompt carrying the parent's approved pre-change report)."""
    for line in reversed(tail_lines(transcript)):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if is_human(rec):
            return all(h in prompt_text(rec) for h in HEADINGS)
        if rec.get("type") != "assistant":
            continue
        for c in (rec.get("message") or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text" and all(h in str(c.get("text", "")) for h in HEADINGS):
                return True
    return False


def log(entry: dict) -> None:
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def decide(data: dict, mode: str = MODE) -> dict | None:
    """Return the hook JSON to print, or None to stay silent."""
    tool = str(data.get("tool_name", ""))
    targets = targets_for(tool, data.get("tool_input") or {}, str(data.get("cwd", "")))
    if not targets or os.path.exists(OFF):
        return None
    transcript = str(data.get("transcript_path", ""))
    if not transcript or not os.path.exists(transcript) or reported(transcript):
        return None
    session = str(data.get("session_id") or "")
    agent_id = str(data.get("agent_id") or "")
    if report_file_ok(session, agent_id, transcript):
        return None
    log({"ts": int(time.time()), "mode": mode, "session": data.get("session_id"), "agent": data.get("agent_id"),
         "tool": tool, "targets": targets[:5], "cwd": data.get("cwd")})
    path = report_path(session, agent_id) or os.path.join(REPORT_DIR, f"{session}.md")
    if mode == "block":
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": REASON.format(path=path)}}
    return {"systemMessage": "[경고 모드] " + REASON.format(path=path)}


def main() -> int:
    try:
        data = json.load(sys.stdin)
        out = decide(data)
    except Exception:
        return 0
    if out:
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
