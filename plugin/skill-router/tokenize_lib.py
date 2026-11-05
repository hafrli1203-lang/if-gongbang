"""tokenize_lib.py — shared tokenizer for build_index.py and route.py.

Word tokens (English, lightly stemmed) + Korean character bigrams. The
bigrams are a cheap stand-in for a real morphological analyzer: Korean
glues particles/endings onto words (조사/어미), so an exact word match
misses '자료로' against a doc that only has '자료'. mecab/konlpy would
need a package install we don't have here; bigrams recovered 4 of 11
agglutination misses in the 2026-11-05 eval at acceptable noise cost.
"""
from __future__ import annotations

import re
from collections import Counter

TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[가-힣]+")
HANGUL_RE = re.compile(r"[가-힣]+")


def stem(token: str) -> str:
    if not re.match(r"^[a-z]+$", token):
        return token
    for suf, keep_min in (("ing", 3), ("edly", 3), ("ed", 3), ("es", 3), ("s", 3)):
        if token.endswith(suf) and len(token) - len(suf) >= keep_min:
            return token[: -len(suf)]
    return token


def korean_bigrams(text: str) -> list[str]:
    out = []
    for run in HANGUL_RE.findall(text or ""):
        if len(run) < 2:
            out.append(run)
            continue
        out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return out


def tokenize(text: str) -> list[str]:
    words = [stem(t.lower()) for t in TOKEN_RE.findall(text or "")]
    return words + korean_bigrams(text)


def doc_text(item: dict) -> str:
    return f"{item.get('name', '')} {item.get('description', '')} {item.get('category', '')}"


def token_counts(item: dict) -> Counter:
    return Counter(tokenize(doc_text(item)))
