#!/usr/bin/env python3
"""UserPromptSubmit hook: nudges the session to search the reference library before
answering a question about company/project material, instead of answering from memory.

Keywords are derived AT RUNTIME from the owner's own folder names under cfg["library_root"]
(never a fixed industry/keyword list - this tool must stay industry-agnostic). A handful of
generic words are always treated as a signal because they are the vocabulary this workflow
itself uses for "go check the library" questions (자료/기준/사례/강의).

Shares lesson_capture.py's MAX_PROMPT_CHARS cap (same clean_prompt(), so unbounded-input risk
is bounded the same way here) and the same SIGALRM-based deadline via
gongbang_config.run_with_deadline(). MAX_LIBRARY_ENTRIES additionally bounds the cost of
listing library_root itself, independent of the prompt, in case that directory is very large
or sits on slow/network storage.

Fails open on any error -> silent exit 0. Prints at most one line, only when the prompt
looks like a question about reference material; otherwise prints nothing.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gongbang_config  # noqa: E402
from lesson_capture import MAX_PROMPT_CHARS, clean_prompt  # noqa: E402

GENERIC_DROP = {"자료", "메인", "규칙"}
FIXED_GENERIC = {"자료", "기준", "사례", "강의"}
SPLIT_RX = re.compile(r"[\s/·,\-_()\[\]]+")
HOOK_DEADLINE_S = 0.25
# Bounds the cost of deriving keywords from a very large or slow library_root directory,
# independent of (and in addition to) the prompt-length cap shared with lesson_capture.py.
MAX_LIBRARY_ENTRIES = 2000


def derive_keywords(library_root: str) -> set[str]:
    keywords = set(FIXED_GENERIC)
    try:
        for i, name in enumerate(os.listdir(library_root)):
            if i >= MAX_LIBRARY_ENTRIES:
                break
            if name.startswith("."):
                continue
            for token in SPLIT_RX.split(name):
                token = token.strip()
                if len(token) >= 2 and token not in GENERIC_DROP:
                    keywords.add(token)
    except Exception:
        pass
    return keywords


def looks_like_library_question(prompt: str, keywords: set[str]) -> bool:
    return any(kw in prompt for kw in keywords)


def build_message(tool_name: str) -> str:
    return (
        f"회사/프로젝트 자료를 묻는 질문일 수 있다. 답하기 전에 {tool_name}로 먼저 검색하고, "
        "검색 없이 기억으로 답하지 않는다."
    )


def detect(data: dict, cfg: dict) -> str | None:
    raw = str(data.get("prompt", "")) if isinstance(data, dict) else ""
    if len(raw) > MAX_PROMPT_CHARS:
        raw = raw[:MAX_PROMPT_CHARS]
    cleaned = clean_prompt(raw)
    if not cleaned:
        return None
    keywords = derive_keywords(cfg["library_root"])
    if not looks_like_library_question(cleaned, keywords):
        return None
    tool_name = cfg.get("library_tool_name", "library_search")
    return build_message(tool_name)


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
