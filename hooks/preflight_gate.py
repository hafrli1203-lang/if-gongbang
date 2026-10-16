#!/usr/bin/env python3
"""PreToolUse hook (Edit | Write | MultiEdit | NotebookEdit | Bash): no app-code change
before a pre-change report.

Before changing code, one assistant message in the current turn (after the last human
prompt) must carry all four headings: 읽은 자료 / 예상 위험 / 바꿀 파일 / 되돌리는 법
(see docs/rules/WORKFLOW.md §2). Applies only to files inside a project git repo under
apps_root/<project>/ (not docs, *.md, or an exempt_apps entry).

preflight_mode "warn": never blocks, logs and shows a notice (good for a first rollout).
preflight_mode "block": denies. Fails open on any error.
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fence_parse  # noqa: E402 - shares the one human-turn-boundary definition with verify_claims.py
import gongbang_config  # noqa: E402
import scope_guard as _sg  # noqa: E402 - shares the one shell tokenizer/write-target extractor

HEADINGS = ("읽은 자료", "예상 위험", "바꿀 파일", "되돌리는 법")
TAIL_BYTES = 8 * 1024 * 1024
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
WRITE_SIGNAL = re.compile(
    r"(?<![<>&0-9])>>?(?!&)|\btee\b|\bsed\s+(-[a-zA-Z]*i|--in-place)|\bperl\s+-[a-zA-Z]*i|\b(mv|cp|rm|touch|truncate|patch)\s"
    r"|\bgit\s+(apply|checkout|restore|am)\b|write_text\(|open\([^)]*['\"][wa]['\"]"
)
REASON = ("착수 전 보고 필요: 코드를 고치기 전에 한 글 안에 네 가지를 먼저 쓴다 — 읽은 자료(파일:줄) / "
          "예상 위험(병목 포함) / 바꿀 파일 / 되돌리는 법. 쓴 뒤 다시 시도한다.")


def app_repo(path: str, cfg: dict) -> str | None:
    """Return the project root if path is inside apps_root/<project>/ that is a git repo."""
    apps_root = cfg["apps_root"].rstrip("/") + "/"
    p = os.path.normpath(path)
    if not p.startswith(apps_root):
        return None
    app = p[len(apps_root):].split("/", 1)[0]
    if not app or app in cfg.get("exempt_apps", []):
        return None
    root = apps_root + app
    return root if os.path.exists(os.path.join(root, ".git")) else None


def exempt(path: str) -> bool:
    p = os.path.normpath(path)
    return p.endswith(".md") or "/docs/" in p


def bash_write_targets(cmd: str, cwd: str) -> list[str]:
    """Returns only the paths `cmd` actually WRITES to (resolved to absolute, using `cwd` for
    any relative one) - reusing scope_guard.py's own tokenizer/segment-splitter/write-target
    extraction, the same one block_dangerous.py already shares, so there is exactly one
    implementation of "what does this Bash command write to" in the whole repo. Only genuine
    write targets are collected - a read-only path (a doc merely cat'd alongside a real code
    write in the same command) is never mistaken for a write target, and each collected target
    is judged on its own below, so one exempt path in a command cannot mask another target
    that is not exempt."""
    tokens = _sg._tokenize(cmd)
    if tokens is None:
        return []
    segments = _sg._split_segments(tokens)
    for seg in list(segments):
        segments.extend(_sg._expand_nested_shell_segment(seg))
    resolved: list[str] = []
    for seg in segments:
        for t in _sg._write_target_tokens(seg):
            stripped = t.strip("'\"")
            expanded = os.path.expanduser(os.path.expandvars(stripped))
            if not os.path.isabs(expanded) and cwd:
                expanded = os.path.join(cwd, expanded)
            resolved.append(os.path.normpath(expanded))
    return resolved


def targets_for(tool: str, inp: dict, cwd: str, cfg: dict) -> list[str]:
    if tool in EDIT_TOOLS:
        p = str(inp.get("file_path") or inp.get("notebook_path") or "")
        cands = [p] if p else []
    elif tool == "Bash":
        cmd = str(inp.get("command", ""))
        if not WRITE_SIGNAL.search(cmd):
            return []
        cands = bash_write_targets(cmd, cwd)
        if not cands and cwd:
            cands = [os.path.join(cwd, "_")]
    else:
        return []
    return [c for c in cands if app_repo(c, cfg) and not exempt(c)]


def is_human(rec: dict) -> bool:
    """A real human turn boundary - delegates to fence_parse.is_human_turn_message (shared with
    verify_claims.py), which requires origin.kind == "human". A tool_result or a peer/subagent
    relay message is also stored as type "user" but must never be mistaken for a new human
    turn."""
    return fence_parse.is_human_turn_message(rec)


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


def reported(transcript: str) -> bool:
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


def log(entry: dict, cfg: dict) -> None:
    try:
        path = os.path.join(cfg["log_dir"], "preflight-gate.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def decide(data: dict, cfg: dict) -> dict | None:
    tool = str(data.get("tool_name", ""))
    targets = targets_for(tool, data.get("tool_input") or {}, str(data.get("cwd", "")), cfg)
    if not targets:
        return None
    transcript = str(data.get("transcript_path", ""))
    if not transcript or not os.path.exists(transcript) or reported(transcript):
        return None
    log({"ts": int(time.time()), "mode": cfg["preflight_mode"], "session": data.get("session_id"),
         "agent": data.get("agent_id"), "tool": tool, "targets": targets[:5], "cwd": data.get("cwd")}, cfg)
    if cfg["preflight_mode"] == "block":
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": REASON}}
    return {"systemMessage": "[경고 모드] " + REASON}


def main() -> int:
    try:
        data = json.load(sys.stdin)
        cfg = gongbang_config.load()
        out = decide(data, cfg)
    except Exception:
        return 0
    if out:
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
