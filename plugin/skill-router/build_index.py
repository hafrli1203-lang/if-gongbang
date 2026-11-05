#!/usr/bin/env python3
"""build_index.py — scan all SKILL.md sources and build the router index.

Sources scanned (always-on skills, installed plugins, the skill warehouse):
  1. ~/.claude/skills/*/SKILL.md            (core, always loaded)
  2. ~/.claude/plugins/cache/**/SKILL.md     (plugins, enabled or disabled)
  3. ~/.claude/skills-library/**/SKILL.md    (warehouse, categorized folders)

Excludes: _중복, _보관, node_modules, tests, test, examples, example, .git,
__pycache__, dist, build.

Output: $SKILL_ROUTER_INDEX, default ~/.cache/skill-router/index.json
  [{ name, description, path, source, category, enabled }]

Run nightly (see README.md) and on demand:
    python3 build_index.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

HOME = Path(os.environ.get("HOME") or os.path.expanduser("~"))
SKILLS_DIR = HOME / ".claude" / "skills"
PLUGINS_CACHE = HOME / ".claude" / "plugins" / "cache"
WAREHOUSE_DIR = HOME / ".claude" / "skills-library"
OUT_PATH = Path(os.environ.get("SKILL_ROUTER_INDEX") or os.path.expanduser("~/.cache/skill-router/index.json"))

EXCLUDE_DIR_NAMES = {
    "_중복", "_보관", "node_modules", "tests", "test", "examples", "example",
    ".git", "__pycache__", "dist", "build", "_drafts", "_index",
}

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _excluded(path: Path) -> bool:
    return any(part in EXCLUDE_DIR_NAMES for part in path.parts)


def _parse_frontmatter(text: str) -> dict:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    block = m.group(1)
    out: dict = {}
    # simple "key: value" line parser (frontmatter here is flat, no nested yaml)
    cur_key: Optional[str] = None
    for line in block.splitlines():
        if not line.strip():
            continue
        if line.startswith((" ", "\t")) and cur_key:
            out[cur_key] = (out.get(cur_key, "") + " " + line.strip()).strip()
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            out[key] = val
            cur_key = key
    return out


def _category_from_path(skill_md: Path, base: Path) -> str:
    try:
        rel = skill_md.relative_to(base)
    except ValueError:
        return ""
    parts = rel.parts
    return parts[0] if len(parts) > 1 else ""


def _enabled_plugins() -> set[str]:
    """Best-effort read of which plugin names are currently enabled.

    settings.json's enabledPlugins is {"<plugin>@<marketplace>": true|false}.
    """
    enabled: set[str] = set()
    settings_paths = [HOME / ".claude" / "settings.json", HOME / ".claude" / "settings.local.json"]
    for sp in settings_paths:
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            continue
        val = data.get("enabledPlugins")
        if isinstance(val, dict):
            for key, is_on in val.items():
                if is_on:
                    enabled.add(str(key).split("@", 1)[0])
    return enabled


def scan_core() -> list[dict]:
    out = []
    if not SKILLS_DIR.is_dir():
        return out
    for entry in sorted(SKILLS_DIR.iterdir()):
        if not entry.is_dir() or entry.name in EXCLUDE_DIR_NAMES:
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            continue
        fm = _parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="ignore"))
        out.append({
            "name": fm.get("name") or entry.name,
            "description": fm.get("description") or "",
            "path": str(skill_md),
            "source": "core",
            "category": "",
            "enabled": True,
        })
    return out


def scan_plugins() -> list[dict]:
    out = []
    if not PLUGINS_CACHE.is_dir():
        return out
    enabled_names = _enabled_plugins()
    for skill_md in PLUGINS_CACHE.rglob("SKILL.md"):
        if _excluded(skill_md):
            continue
        fm = _parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="ignore"))
        try:
            rel = skill_md.relative_to(PLUGINS_CACHE)
            # cache layout: <marketplace>/<plugin>/<version>/... — plugin name is parts[1].
            plugin_name = rel.parts[1] if len(rel.parts) > 1 else rel.parts[0]
        except (ValueError, IndexError):
            plugin_name = ""
        name = fm.get("name") or skill_md.parent.name
        out.append({
            "name": name,
            "description": fm.get("description") or "",
            "path": str(skill_md),
            "source": f"plugin:{plugin_name}",
            "category": "",
            "enabled": plugin_name in enabled_names if enabled_names else False,
        })
    return out


def scan_warehouse() -> list[dict]:
    out = []
    if not WAREHOUSE_DIR.is_dir():
        return out
    for skill_md in WAREHOUSE_DIR.rglob("SKILL.md"):
        if _excluded(skill_md):
            continue
        fm = _parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="ignore"))
        cat = _category_from_path(skill_md, WAREHOUSE_DIR)
        out.append({
            "name": fm.get("name") or skill_md.parent.name,
            "description": fm.get("description") or "",
            "path": str(skill_md),
            "source": "warehouse",
            "category": cat,
            "enabled": False,
        })
    return out


def _priority(it: dict) -> int:
    source = it.get("source", "")
    if source == "core":
        return 0
    if source.startswith("plugin:"):
        return 1 if it.get("enabled") else 2
    return 3  # warehouse


def build() -> list[dict]:
    """Scan all sources, then dedup by skill name.

    The same skill name commonly appears under several plugin cache paths
    or warehouse copies (2,382 of 5,663 unique names in the first scan,
    2026-11-05). Keeping every path would waste router top-k slots on the
    same skill and slow scoring, so we keep one representative per name:
    core > enabled plugin > disabled plugin > warehouse, shortest path
    as the tiebreak.
    """
    items = scan_core() + scan_plugins() + scan_warehouse()
    best: dict[str, dict] = {}
    for it in items:
        name = it["name"]
        cur = best.get(name)
        if cur is None:
            best[name] = it
            continue
        if (_priority(it), len(it["path"])) < (_priority(cur), len(cur["path"])):
            best[name] = it
    return sorted(best.values(), key=lambda x: x["name"])


def _add_precomputed_tokens(items: list[dict]) -> dict:
    """Tokenize every doc once at build time (not per query) so route.py's
    per-request latency stays well under the 1s budget. Also rolls up the
    global doc_freq table BM25 needs."""
    from collections import Counter

    sys.path.insert(0, str(Path(__file__).parent))
    from tokenize_lib import token_counts  # noqa: E402

    doc_freq: Counter = Counter()
    lens = []
    for it in items:
        tc = token_counts(it)
        it["_tokens"] = dict(tc)
        it["_len"] = sum(tc.values())
        lens.append(it["_len"])
        for term in tc:
            doc_freq[term] += 1
    avg_len = sum(lens) / max(len(lens), 1)
    return {"doc_freq": dict(doc_freq), "avg_len": avg_len, "n_docs": len(items)}


def main() -> None:
    items = build()
    stats = _add_precomputed_tokens(items)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(
            {"built_at": __import__("time").time(), "items": items, **stats},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    n_core = sum(1 for i in items if i["source"] == "core")
    n_plugin = sum(1 for i in items if i["source"].startswith("plugin:"))
    n_wh = sum(1 for i in items if i["source"] == "warehouse")
    print(f"index built: {len(items)} skills (core={n_core} plugin={n_plugin} warehouse={n_wh}) -> {OUT_PATH}")


if __name__ == "__main__":
    main()
