#!/usr/bin/env python3
"""route.py — pick the k best-matching skills for a task sentence.

Usage:
    python3 route.py "<일 문장>" [--k 3] [--max-chars 6000] [--json]

Scoring = BM25 over precomputed per-doc token counts (built by
build_index.py, so this stays well under 1s), with Korean<->English
alias expansion, a 공방 칸(station) default-skill boost, and a small
usage bonus from injections.jsonl.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tokenize_lib import tokenize, TOKEN_RE  # noqa: E402

INDEX_PATH = Path(os.environ.get("SKILL_ROUTER_INDEX") or os.path.expanduser("~/.cache/skill-router/index.json"))
ALIASES_PATH = Path(__file__).parent / "aliases.json"
# SKILL_ROUTER_INJECTIONS_LOG 오버라이드(시험·작업 사본 실행용) —
# 같은 앱의 시험이 운영 계기판 파일을 더럽히지 않게 한다.
INJECTIONS_LOG = Path(os.environ.get("SKILL_ROUTER_INJECTIONS_LOG") or os.path.expanduser("~/.cache/skill-router/injections.jsonl"))

FRONTMATTER_RE = __import__("re").compile(r"^---\s*\n.*?\n---\s*\n", __import__("re").DOTALL)

# 공방 칸(station) defaults — MASTER_RULES / WORKFLOW_RULES 레일 3단계 참고.
STATION_DEFAULTS = {
    "dev": {
        "keywords": ["앱", "만들어", "개발", "고쳐", "기능", "버그", "코드", "구현", "배포"],
        "skills": ["일", "크래시-아웃", "writing-plans"],
    },
    "screen": {
        "keywords": ["화면", "디자인", "UI", "UX", "랜딩", "포스터", "카드뉴스", "레이아웃"],
        "skills": ["impeccable", "ai-tell-free-web", "아우라-파밍"],
    },
    "public_writing": {
        "keywords": ["블로그", "글", "콘텐츠", "공개", "발행", "포스트", "캡션", "seo", "검색노출"],
        "skills": ["seo-최적화", "copywriting-ko", "humanize-korean"],
    },
    "research": {
        "keywords": ["리서치", "조사", "알아봐", "찾아줘", "검색해"],
        "skills": ["agent-reach", "research"],
    },
    "planning_doc": {
        "keywords": ["설계부터", "스펙", "문서로 적어", "구현 전", "코드 짜기 전"],
        "skills": ["writing-plans"],
    },
    "status_report": {
        "keywords": ["진행 상황", "보고해", "어디까지 했", "뭐 하고 있"],
        "skills": ["진행-보고"],
    },
}

_INDEX_CACHE: dict | None = None


def _load_index_data() -> dict:
    global _INDEX_CACHE
    if _INDEX_CACHE is None:
        if not INDEX_PATH.is_file():
            _INDEX_CACHE = {"items": [], "doc_freq": {}, "avg_len": 1.0, "n_docs": 0}
        else:
            _INDEX_CACHE = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    return _INDEX_CACHE


def _load_aliases() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    if not ALIASES_PATH.is_file():
        return out
    data = json.loads(ALIASES_PATH.read_text(encoding="utf-8"))
    for group in data.get("groups", []):
        all_terms = set()
        for side in ("ko", "en"):
            for term in group.get(side, []):
                all_terms.update(TOKEN_RE.findall(term.lower()))
        for term in all_terms:
            out.setdefault(term, set()).update(all_terms)
    return out


def _expand(tokens: list[str], aliases: dict[str, set[str]]) -> Counter:
    expanded = Counter(tokens)
    for tok in tokens:
        for eq in aliases.get(tok, ()):
            expanded[eq] += 1
    return expanded


def _bm25_score(query_tokens: Counter, doc_tokens: dict, doc_len: int, avg_len: float,
                 doc_freq: dict, n_docs: int, k1: float = 1.5, b: float = 0.75) -> float:
    score = 0.0
    for term, qf in query_tokens.items():
        f = doc_tokens.get(term, 0)
        if f == 0:
            continue
        df = doc_freq.get(term, 1)
        idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
        denom = f + k1 * (1 - b + b * doc_len / max(avg_len, 1.0))
        score += idf * (f * (k1 + 1)) / max(denom, 1e-9)
    return score


def _usage_bonus(name: str, usage_counts: dict[str, int]) -> float:
    n = usage_counts.get(name, 0)
    return min(n, 10) * 0.05


def _load_recent_usage(window_sec: float = 7 * 24 * 3600) -> dict[str, int]:
    counts: dict[str, int] = Counter()
    if not INJECTIONS_LOG.is_file():
        return counts
    now = time.time()
    try:
        for line in INJECTIONS_LOG.read_text(encoding="utf-8", errors="ignore").splitlines()[-2000:]:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if now - float(rec.get("ts", 0)) > window_sec:
                continue
            for name in rec.get("skills", []):
                counts[name] += 1
    except Exception:
        pass
    return counts


def _station_boost(query: str, skill_name: str) -> float:
    # Station keyword hits are a high-confidence signal (a curated company
    # default for a known task shape), so they must outweigh coincidental
    # BM25 matches from the 3,600-skill warehouse (those routinely score
    # 15-20+ on generic terms like "문서" — see eval debug 2026-11-05).
    boost = 0.0
    for station in STATION_DEFAULTS.values():
        if any(kw in query for kw in station["keywords"]):
            if skill_name in station["skills"]:
                boost += 8.0
    return boost


def _source_boost(item: dict) -> float:
    source = item.get("source", "")
    if source == "core":
        return 5.0
    if source.startswith("plugin:"):
        return 3.0 if item.get("enabled") else 0.5
    return 0.0  # warehouse: wins only on a strong text match


def route(query: str, k: int = 3) -> list[dict]:
    data = _load_index_data()
    items = data.get("items", [])
    if not items:
        return []
    doc_freq = data.get("doc_freq", {})
    avg_len = data.get("avg_len", 1.0)
    n_docs = data.get("n_docs", len(items))

    aliases = _load_aliases()
    q_expanded = _expand(tokenize(query), aliases)
    usage = _load_recent_usage()

    scored = []
    for it in items:
        doc_tokens = it.get("_tokens", {})
        doc_len = it.get("_len", sum(doc_tokens.values()) or 1)
        s = _bm25_score(q_expanded, doc_tokens, doc_len, avg_len, doc_freq, n_docs)
        s += _station_boost(query, it.get("name", ""))
        s += _usage_bonus(it.get("name", ""), usage)
        s += _source_boost(it)
        scored.append((s, it))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [it for s, it in scored if s > 0][:k]


def _skill_body(path: str, max_chars: int) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    body = FRONTMATTER_RE.sub("", text, count=1).strip()
    if len(body) <= max_chars:
        return body
    cut = body[:max_chars]
    last_para = cut.rfind("\n\n")
    if last_para > max_chars * 0.5:
        cut = cut[:last_para]
    return cut + "\n\n...(생략)"


def _log_injection(query: str, chosen: list[dict]) -> None:
    INJECTIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "query": query[:300], "skills": [c.get("name") for c in chosen]}
    with open(INJECTIONS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--max-chars", type=int, default=6000)
    ap.add_argument("--json", action="store_true")
    # 기록은 opt-in이다: 훅·앱이 실제로 세션에 넣을 때만 --log를 주고
    # 기록한다. 맨손 CLI 호출(--k 시험 등)은 기본으로 기록하지 않아 계기판
    # 숫자가 시험 호출로 부풀지 않는다.
    # --no-log는 과거 기본값(기록함)과의 호환을 위해 그대로 받아 두지만
    # 이제는 아무 효과가 없다(기본이 이미 기록 안 함).
    ap.add_argument("--log", action="store_true")
    ap.add_argument("--no-log", action="store_true")
    args = ap.parse_args()

    chosen = route(args.query, k=args.k)
    if args.log and chosen:
        _log_injection(args.query, chosen)

    if args.json:
        print(json.dumps([{"name": c["name"], "path": c["path"]} for c in chosen], ensure_ascii=False))
        return

    if not chosen:
        return

    per_skill_budget = max(args.max_chars // max(len(chosen), 1), 500)
    parts = []
    for c in chosen:
        body = _skill_body(c["path"], per_skill_budget)
        if body:
            parts.append(f"### {c['name']}\n{body}")
    print("\n\n".join(parts))


if __name__ == "__main__":
    main()
