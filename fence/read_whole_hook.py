#!/usr/bin/env python3
"""PreToolUse hook (Read | Bash): a document is read to the end, not by its first lines.

Owner [date]: "긴문서를 첫줄만 읽고 그냥 넘기는 ai 습관을 고칠수 있나?" / "지시도 앞 문장꺼만 보고".
Denies a partial read of a document (.md / .txt / .csv / .html) that would fit in one whole Read:
  - Read with offset/limit that stops before the file's end,
  - Bash `head ...` or `sed -n ...p` on such a file.
Allowed: files longer than one Read can hold (WHOLE_READ_LINES), read in pieces; a file this session
already read whole with Read (going back to one section afterwards is fine); code and logs.
Fails open on any error. Off switch (owner only): ~/.claude/gate/read-whole-off.
"""
import json
import os
import re
import shlex
import sys

OFF = os.path.expanduser("~/.claude/gate/read-whole-off")
TAIL_BYTES = 8 * 1024 * 1024
WHOLE_READ_LINES = 2000  # the Read tool's own default line window
DOC_EXT = (".md", ".txt", ".csv", ".html")
REASON = ("문서는 끝까지 읽는다: {path} ({lines}줄)을 앞부분만 읽으려 했다. offset·limit 없이 Read로 전체를 "
          "읽은 뒤 판단한다(운영자 지시 [date] \"긴문서를 첫줄만 읽고 그냥 넘기는\" 습관 차단).")


def line_count(path: str) -> int | None:
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except Exception:
        return None


def is_doc(path: str) -> bool:
    return path.lower().endswith(DOC_EXT)


def read_whole_before(transcript: str, path: str) -> bool:
    try:
        with open(transcript, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - TAIL_BYTES))
            data = f.read().decode("utf-8", errors="replace")
    except Exception:
        return False
    for line in data.splitlines():
        if path not in line or '"tool_use"' not in line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        for c in (rec.get("message") or {}).get("content") or []:
            if not isinstance(c, dict) or c.get("type") != "tool_use" or c.get("name") != "Read":
                continue
            inp = c.get("input") or {}
            if inp.get("file_path") != path:
                continue
            start = int(inp.get("offset") or 1)
            limit = int(inp.get("limit") or WHOLE_READ_LINES)
            lines = line_count(path) or 0
            if start <= 1 and limit >= lines:
                return True
    return False


def bash_partial_targets(cmd: str, cwd: str) -> list[str]:
    """Doc files a `head`/`sed -n` segment reads partially."""
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except Exception:
        return []
    out: list[str] = []
    seg: list[str] = []
    for t in tokens + [";"]:
        if t in (";", "&&", "||", "|", "&"):
            if seg and (seg[0] == "head" or (seg[0] == "sed" and "-n" in seg)):
                for a in seg[1:]:
                    if a.startswith("-") or not is_doc(a):
                        continue
                    out.append(a if os.path.isabs(a) else os.path.join(cwd or "/", a))
            seg = []
        else:
            seg.append(t)
    return out


def head_count(cmd: str, path: str) -> int | None:
    """N of `head -N file` / `head -n N file` for this file, else None."""
    m = re.search(r"\bhead\s+(?:-n\s*)?-?(\d+)\s+[^;|&]*" + re.escape(os.path.basename(path)), cmd)
    return int(m.group(1)) if m else None


def decide(data: dict) -> dict | None:
    if os.path.exists(OFF):
        return None
    tool = str(data.get("tool_name", ""))
    inp = data.get("tool_input") or {}
    if tool == "Read":
        path = str(inp.get("file_path") or "")
        if not is_doc(path) or not (inp.get("offset") or inp.get("limit")):
            return None
        candidates = [path]
    elif tool == "Bash":
        candidates = bash_partial_targets(str(inp.get("command", "")), str(data.get("cwd", "")))
    else:
        return None
    transcript = str(data.get("transcript_path") or "")
    for path in candidates:
        lines = line_count(path)
        if lines is None or lines > WHOLE_READ_LINES:
            continue
        if tool == "Read":
            start = int(inp.get("offset") or 1)
            limit = int(inp.get("limit") or WHOLE_READ_LINES)
            if start <= 1 and start + limit - 1 >= lines:
                continue
        if tool == "Bash":
            n = head_count(str(inp.get("command", "")), path)
            if n is not None and n >= lines:
                continue
            if re.search(r"\bsed\s+-n\s+['\"]?1,\$p['\"]?\s", str(inp.get("command", "")) + " "):
                continue
        if transcript and read_whole_before(transcript, path):
            continue
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": REASON.format(path=path, lines=lines)}}
    return None


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
