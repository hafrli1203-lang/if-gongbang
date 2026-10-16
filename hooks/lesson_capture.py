#!/usr/bin/env python3
"""UserPromptSubmit hook: if the owner's prompt carries a correction marker, remind the
session to log it as an L1 experience (see lessons_store.add_lesson / mark_seen) instead of
letting a one-time correction disappear once the session ends.

Markers and the STRIP/RELAY/SKIP_PREFIX wrapper-stripping rules mirror metrics/corrections.py
MARK - kept in exactly one other place so the two stay comparable. Because this heuristic is
only ~60-65% precise (measured against hand-read samples), this hook only SUGGESTS logging -
it never forces a tool call and never blocks the prompt. Fails open: any error, or no marker
found, means silent exit 0.

A UserPromptSubmit hook must never add real latency to the user's prompt, no matter how long
or adversarial the input is. Two independent, redundant defenses bound this: (1) only the
first MAX_PROMPT_CHARS characters of the prompt are ever looked at - correction markers are a
few words, never buried past that point in a realistic message - and (2) the whole detect()
call runs under gongbang_config.run_with_deadline(), a SIGALRM-based wall-clock cutoff, so even
an unexpectedly slow regex on a short string cannot stall the prompt. library_nudge.py
imports MAX_PROMPT_CHARS from here and applies the same cap, since it shares clean_prompt().
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gongbang_config  # noqa: E402

# A correction marker is a few words; nothing in MARK below can need more context than this to
# fire. Capping the input here - before any regex at all runs - keeps this hook's worst case
# bounded independent of how long the rest of a pasted message is. See the module docstring.
MAX_PROMPT_CHARS = 4000
HOOK_DEADLINE_S = 0.25

MARK = [r'아니[야고라지잖]?(?![^\s]*(?:면|었|에요))', r'다시', r'그게 아니', r'말했잖', r'했잖아', r'(?<![가-힣])왜(?![냐]?하면)',
        r'하지\s?마', r'이해\s?(?:를\s?)?못', r'틀렸', r'틀리', r'거짓', r'확인\s?안', r'(?<![가-힣])또(?!한|는)', r'몇\s?번', r'엉뚱',
        r'원복', r'되돌려', r'제대로', r'멋대로', r'마음대로', r'시키지도', r'누가 .{0,10}하래', r'장난치', r'그러지 말',
        r'헤깔리지|헷갈리지']
RX = re.compile('|'.join('(?:%s)' % p for p in MARK))

STRIP = [
    (r'<system-reminder>.*?</system-reminder>', re.S),
    (r'<(command-[a-z]+|local-command-[a-z]+|bash-[a-z]+|task-notification|user-prompt-submit-hook)>.*?</\1>', re.S),
    (r'<pasted_content[^>]*>.*?</pasted_content[^>]*>', re.S),
]
RELAY = re.compile(r'^\s*(?:<(?:teammate-message|agent-message|cross-session-message)\b|Another .* session sent a message)')
SKIP_PREFIX = ('[Request interrupted', 'Caveat:', 'This session is being continued')


def derive_app(cwd: str, cfg: dict) -> str | None:
    """Project folder name for the lesson's `app` field: the first path segment under
    apps_root, "workspace" for apps_root itself, or None if cwd is not under apps_root."""
    apps_root = os.path.normpath(cfg["apps_root"])
    cwd = os.path.normpath(cwd or "")
    if cwd == apps_root:
        return "workspace"
    prefix = apps_root + os.sep
    if not cwd.startswith(prefix):
        return None
    app = cwd[len(prefix):].split(os.sep, 1)[0]
    return app or "workspace"


def clean_prompt(prompt: str) -> str | None:
    """Strips pasted/system/relayed wrapper content from the raw prompt the owner typed.
    Callers should cap `prompt` to MAX_PROMPT_CHARS before calling this (detect() below does)
    so an unterminated wrapper tag can't make the STRIP loop scan an unbounded string."""
    if not isinstance(prompt, str):
        return None
    if RELAY.match(prompt):
        return None
    c = prompt
    for pat, fl in STRIP:
        c = re.sub(pat, '', c, flags=fl)
    c = c.strip()
    if not c:
        return None
    if c.startswith(SKIP_PREFIX):
        return None
    return c


def build_message(app: str) -> str:
    return (
        "대표 지적일 수 있다. 지적이 맞으면 답하기 전에 lessons_store.add_lesson("
        f"app={app}, scene, why, rule, owner_quote=대표 문장 그대로)로 3문장(실제 장면 / "
        "그대로 쓸 수 없는 이유 / 다음부터 적용할 기준)을 남긴다. 이미 있는 경험이면 "
        "mark_seen. 지적이 아니면 무시."
    )


def detect(data: dict, cfg: dict) -> str | None:
    raw = str(data.get("prompt", "")) if isinstance(data, dict) else ""
    if len(raw) > MAX_PROMPT_CHARS:
        raw = raw[:MAX_PROMPT_CHARS]
    cleaned = clean_prompt(raw)
    if cleaned is None or not RX.search(cleaned):
        return None
    cwd = str(data.get("cwd", "")) if isinstance(data, dict) else ""
    app = derive_app(cwd, cfg) or "workspace"
    return build_message(app)


def main() -> int:
    try:
        data = json.load(sys.stdin)
        cfg = gongbang_config.load()
    except Exception:
        return 0
    msg = gongbang_config.run_with_deadline(HOOK_DEADLINE_S, lambda: detect(data, cfg))
    if msg:
        print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
