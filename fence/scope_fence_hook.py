#!/usr/bin/env python3
"""PreToolUse hook (Edit|Write|MultiEdit|NotebookEdit|Bash|Agent): block work aimed at an app
outside the owner's current scope (CURRENT_SCOPE.json).

Owner directives G19 ("개발 툴부터 정립하는게 우선이다 자꾸 개발부터 급하게 할려고 하지마라") /
G25 ("다른 앱은 이프 공방 개발 끝나고 보기로 했잖아") were, per the [date] 전수조사, "글로만
있음(막는 장치 없음)" -- this hook is that device. It is additive to, not a replacement for,
scope-guard.py (infra paths: settings.json/systemd/.env/...) and preflight-gate.py (pre-change
report) -- all three run independently.

Scope is a plain allow-list the owner sets with `set_scope.py`, run by the owner's own typed `!`
command (never a model tool call -- this hook denies every model-side write to CURRENT_SCOPE.json
itself, unconditionally, the same "no gate, ever" posture scope-guard.py gives its own hook files).
No CURRENT_SCOPE.json yet, or it is unreadable/empty -> no restriction (nothing has been scoped,
so nothing is blocked by this hook).

Blocked once a scope exists:
  - Edit/Write/MultiEdit/NotebookEdit whose file_path sits under <apps-root>/<app>/ for an <app> not
    in the allow-list. Reading is never touched here.
  - a Bash `gb2 run|fix|continue|change|new <app>` call for an out-of-scope <app> (gb2 watch is
    exempt -- it legitimately walks every app itself).
  - a Bash command that WRITES (same write-signal idea as preflight-gate.py, after its B4-1 fix)
    into an out-of-scope <apps-root>/<app>/ path.
  - an Agent tool_use whose prompt/description/name names another real <apps-root>/<app> directory
    (a whole word, case-sensitive) that is not in scope -- catches the lead dispatching a worker
    at the wrong app. Known limit: a short app name that is also an ordinary word (listed itself as
    a real app dir, e.g. one two characters long) could false-positive; names under 3 characters are
    skipped for this reason, and the off switch exists for the rest.
Off switch (owner only): ~/.claude/gate/scope-fence-off (does NOT exempt the CURRENT_SCOPE.json
self-protection above). Fails open on any internal error other than that self-protection check.
"""
from __future__ import annotations

import json
import os
import re
import sys

SCOPE_FILE = os.environ.get("SCOPE_FENCE_FILE") or "<apps-root>/<ops>/CURRENT_SCOPE.json"
APPS_ROOT = os.environ.get("SCOPE_FENCE_APPS_ROOT") or "<apps-root>"
OFF = os.path.expanduser("~/.claude/gate/scope-fence-off")
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
WRITE_SIGNAL = re.compile(
    r"(?<![<>&0-9])>>?(?!&)|\btee\b|\bsed\s+(-[a-zA-Z]*i|--in-place)|\bperl\s+-[a-zA-Z]*i|\b(mv|cp|rm|touch|truncate|patch)\s"
    r"|\bgit\s+(apply|checkout|restore|am)\b|write_text\(|open\([^)]*['\"][wa]['\"]"
)
GB2_RUN = re.compile(r"\bgb2(?:\.py)?\b[^;&|\n]*?\b(run|fix|continue|change|new)\b\s+([A-Za-z0-9_.-]+)")


def _apps_prefix() -> str:
    return APPS_ROOT.rstrip("/") + "/"


def app_of(path: str) -> str | None:
    p = os.path.normpath(path)
    prefix = _apps_prefix()
    if not p.startswith(prefix):
        return None
    rest = p[len(prefix):]
    return rest.split("/", 1)[0] if rest else None


def load_scope() -> list[str] | None:
    try:
        with open(SCOPE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        allow = data.get("allow")
        return [str(a) for a in allow] if isinstance(allow, list) and allow else None
    except Exception:
        return None


def _scope_file_hit(path: str, cmd: str) -> bool:
    """CURRENT_SCOPE.json may never be changed by a model tool call -- always blocked, no gate,
    regardless of the off switch (mirrors scope-guard.py's own protection of the hook files)."""
    scope_norm = os.path.normpath(SCOPE_FILE)
    if path and os.path.normpath(path) == scope_norm:
        return True
    if cmd and scope_norm in cmd:
        return bool(re.search(r"(>>?|\btee\b|\bsed\s+-[a-zA-Z]*i|\bcp\b|\bmv\b|\brm\b)[^\n]*" + re.escape(scope_norm), cmd))
    return False


def _bash_targets(cmd: str, cwd: str) -> list[str]:
    if not WRITE_SIGNAL.search(cmd):
        return []
    abs_path = re.compile(re.escape(_apps_prefix()) + r"[^\s'\"`;|&)]+")
    cands = abs_path.findall(cmd)
    if not cands and cwd:
        cands = [os.path.join(cwd, "_")]
    return cands


def _agent_app_hit(prompt: str, allow: list[str]) -> str | None:
    try:
        names = [n for n in os.listdir(APPS_ROOT) if os.path.isdir(os.path.join(APPS_ROOT, n))]
    except Exception:
        return None
    allow_set = set(allow)
    for name in names:
        # dot folders (.claude, .git, ...) are settings, not apps (seen live: ".claude" in a
        # worker prompt about login files was fenced as an out-of-scope app)
        if name in allow_set or len(name) < 3 or name.startswith("."):
            continue
        if re.search(r"(?<![\w/-])" + re.escape(name) + r"(?![\w-])", prompt):
            return name
    return None


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def _is_real_app(name: str) -> bool:
    return os.path.isdir(os.path.join(APPS_ROOT, name))


def decide(data: dict) -> dict | None:
    tool = str(data.get("tool_name", ""))
    inp = data.get("tool_input") or {}
    path = str(inp.get("file_path") or inp.get("notebook_path") or "") if tool in EDIT_TOOLS else ""
    cmd = str(inp.get("command", "")) if tool == "Bash" else ""

    if _scope_file_hit(path, cmd):
        return _deny("CURRENT_SCOPE.json은 운영자 전용 set_scope.py(!로 직접 실행)로만 바꾼다. 모델 도구로는 수정 금지.")

    if os.path.exists(OFF):
        return None
    allow = load_scope()
    if allow is None:
        return None  # no scope set yet -> no restriction

    if tool in EDIT_TOOLS:
        app = app_of(path) if path else None
        if app and app not in allow:
            return _deny(f"범위 밖: {app}은 지금 허용 범위({', '.join(allow)}) 밖이다. CURRENT_SCOPE.json 확인.")
        return None

    if tool == "Bash":
        # Only a REAL app folder under the apps root is fenced: a scratch sample app (gb2 with its
        # own GB2_APPS_ROOT elsewhere) or a word inside a sed pattern is not an app (seen live).
        m = GB2_RUN.search(cmd)
        if m and m.group(2) not in allow and _is_real_app(m.group(2)):
            return _deny(f"범위 밖: gb2 {m.group(1)} {m.group(2)}은 허용 범위({', '.join(allow)}) 밖이다.")
        for t in _bash_targets(cmd, str(data.get("cwd", ""))):
            app = app_of(t)
            if app and app not in allow and _is_real_app(app):
                return _deny(f"범위 밖: {app} 쓰기는 허용 범위({', '.join(allow)}) 밖이다.")
        return None

    if tool == "Agent":
        prompt = " ".join(str(inp.get(k, "")) for k in ("prompt", "description", "name"))
        hit = _agent_app_hit(prompt, allow)
        if hit:
            return _deny(f"범위 밖: 작업조 지시에 다른 앱({hit}) 이름이 있다. 지금 범위({', '.join(allow)}) 밖이면 시키지 않는다.")
        return None
    return None


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
