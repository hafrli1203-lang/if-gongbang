#!/usr/bin/env python3
"""Safety hook: denies any tool call that reads, writes, or otherwise names a path matching a
configured sensitive-file glob pattern (secrets, private keys, env files) - a separate concern
from scope_guard.py's approval gate (reversible-with-approval) and block_dangerous.py's
catastrophic-command block (destructive actions). This hook is about WHICH FILES are off-limits
at all, regardless of what operation targets them.

PreToolUse hook (Read | Edit | Write | MultiEdit | NotebookEdit | Bash) - exit 0: allow,
prints a PreToolUse deny JSON and exits 0 to block (the harness's own deny-decision shape, same
convention scope_guard.py uses).

Patterns come from gongbang_config.py's "protected_paths" / "protected_exceptions" (see
gongbang.config.example.json for the shape) - never hardcoded in this file, so a local install
can widen or narrow the list (a different secret-file naming convention, an extra key
extension, ...) without a code change. Defaults match the example config:
  protected_paths:      [".env", ".env.*", "*.pem", "*.key", "id_rsa*"]
  protected_exceptions: [".env.example", "*.sample"]
An exception match always wins over a protected match for the SAME path, so ".env.example"
(which also matches the ".env.*" protected pattern) is still allowed through.

For Read/Edit/Write/MultiEdit/NotebookEdit, the tool's own file_path (or notebook_path) is
checked directly - no shell involved.

For Bash, every token of the command is checked, not only a write target - reading a secret
(`cat .env`, `sed -n 1p .env`, `scp id_rsa host:`) is exactly what this hook exists to stop, not
merely writing one. Tokenizing reuses the SAME shell tokenizer scope_guard.py and
block_dangerous.py already share (see scope_guard.py's module docstring) so quoting/punctuation
handling cannot drift between the three hooks. A flag token ("-la", "--foo") is never treated as
a path. If the command cannot be tokenized at all (malformed quoting), this hook fails CLOSED
with a coarse substring check instead of silently allowing a protected filename through.

Matching is by fnmatch against the raw token, its ~/$VAR-expanded form, and its basename, so
".env" matches a bare filename, "/project/.env" (absolute), "../.env" (relative) and
"$HOME/.env" (unexpanded-until-checked) alike, while "*.key" matches any filename ending in
.key regardless of directory. This is a filename/glob check only - it does not open or read
file contents, so a file that merely happens to be named safely but contains a leaked secret is
outside this hook's scope (see docs/hardening.md for that limit).
"""
import fnmatch
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gongbang_config  # noqa: E402
import scope_guard as _sg  # noqa: E402 - shares the one shell tokenizer, not a duplicate

DEFAULT_PROTECTED = [".env", ".env.*", "*.pem", "*.key", "id_rsa*"]
DEFAULT_EXCEPTIONS = [".env.example", "*.sample"]

PATH_TOOLS = {"Read": "file_path", "Edit": "file_path", "Write": "file_path",
              "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}


def _patterns(cfg: dict) -> tuple[list[str], list[str]]:
    protected = cfg.get("protected_paths")
    if not isinstance(protected, list) or not protected:
        protected = DEFAULT_PROTECTED
    exceptions = cfg.get("protected_exceptions")
    if not isinstance(exceptions, list):
        exceptions = DEFAULT_EXCEPTIONS
    return [str(p) for p in protected], [str(p) for p in exceptions]


def _path_candidates(raw: str) -> list[str]:
    stripped = raw.strip("'\"")
    expanded = os.path.expanduser(os.path.expandvars(stripped))
    basename = os.path.basename(expanded) or os.path.basename(stripped)
    out = []
    for c in (stripped, expanded, basename):
        if c and c not in out:
            out.append(c)
    return out


def _matches_any(candidates: list[str], patterns: list[str]) -> str | None:
    for pat in patterns:
        for c in candidates:
            if fnmatch.fnmatch(c, pat):
                return pat
    return None


def check_path(path: str, cfg: dict) -> str | None:
    """Returns the matched protected pattern, or None if `path` is empty, matches no protected
    pattern, or matches a protected pattern but ALSO matches an exception pattern (exception
    wins)."""
    if not path:
        return None
    protected, exceptions = _patterns(cfg)
    candidates = _path_candidates(path)
    if _matches_any(candidates, exceptions):
        return None
    return _matches_any(candidates, protected)


def check_command(command: str, cfg: dict) -> str | None:
    if not command:
        return None
    protected, exceptions = _patterns(cfg)
    tokens = _sg._tokenize(command)
    if tokens is None:
        # Malformed quoting: the real tokenizer gave up, so fail CLOSED with a coarse
        # substring/basename check over the raw text rather than silently allowing a protected
        # filename through unrecognized. This intentionally does not try to honor exceptions
        # perfectly (a quoted ".env.example" could theoretically still trip it) - the fallback
        # only runs on already-broken shell syntax, where "안전 쪽으로" wins over precision.
        for pat in protected:
            if re.search(re.escape(pat).replace(r"\*", ".*"), command):
                return pat
        return None
    for tok in tokens:
        if tok in _sg.SEPARATORS or tok.startswith("-"):
            continue
        candidates = _path_candidates(tok)
        if _matches_any(candidates, exceptions):
            continue
        hit = _matches_any(candidates, protected)
        if hit:
            return hit
    return None


def deny(reason: str) -> int:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


def _run(data: dict, cfg: dict) -> int:
    tool = data.get("tool_name", "")
    inp = data.get("tool_input", {}) or {}

    hit = None
    if tool in PATH_TOOLS:
        hit = check_path(str(inp.get(PATH_TOOLS[tool], "")), cfg)
    elif tool == "Bash":
        hit = check_command(str(inp.get("command", "")), cfg)

    if hit:
        return deny(f"보호 대상 파일(패턴 {hit}) — 비밀값·키 파일은 읽거나 쓰지 않는다.")
    return 0


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception as exc:
        return deny(f"입력 파싱 실패로 차단(안전 쪽으로): {type(exc).__name__}")
    try:
        cfg = gongbang_config.load()
        return _run(data, cfg)
    except Exception as exc:  # fail-closed: an internal error must never look like "allow"
        return deny(f"내부 오류로 차단(안전 쪽으로): {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
