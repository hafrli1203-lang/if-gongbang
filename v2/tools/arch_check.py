#!/usr/bin/env python3
"""arch_check -- checks a single app's source tree against the
architecture standard (docs/standards/architecture.md): file size, layer-import direction, cross-module private
names, import cycles, banned/vague names, and (only if a word list is
given) industry-hardcoding suspect words.

    arch_check.py <app_dir> [--layers <rules.json>] [--words <file>]
                  [--json]

<app_dir> is scanned for both stacks at once (Node/TS and Python) --
whichever file types it actually has. The layer map defaults to this
standard's own folders for each stack (see DEFAULT_LAYER_RULES below);
--layers <rules.json> replaces BOTH defaults with a custom list of
{"match": "prefix"|"component", "pattern": "...", "layer": "..."}
rules, applied regardless of file extension.

--words <file> is a newline-separated list of suspect words (e.g.
industry/brand names someone should not have hardcoded into this
app). Deliberately NOT bundled with a built-in list -- MASTER_RULES'
"업종 하드코딩 금지": this tool must never ship an opinion about which
industry words are suspect; the caller decides, per app, per run.

Exit code: 1 if any violation was found, else 0. --json switches the
report to one JSON object on stdout; without it, a plain table.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

EXCLUDE_DIR_NAMES = {
    "node_modules", ".git", "dist", "build", "out", ".next", ".nuxt",
    "coverage", ".venv", "venv", "env", "__pycache__", ".cache", "logs",
    "tmp", "temp", "vendor", "target",
}
TS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
PY_EXTENSIONS = (".py",)
SOURCE_EXTENSIONS = TS_EXTENSIONS + PY_EXTENSIONS
MAX_LINES = 800

# Layer direction: for layer K, the set of layers K is allowed to
# import. "neutral" (lib/config -- layer-independent pure tools or
# env reading) is importable by everyone and imports only itself.
# interface is the composition root (may wire a concrete
# infrastructure adapter into an application call), so it may import
# every other layer; application orchestrates infrastructure directly
# (a port goes in front of an external connection only where it has
# to be swappable), so it may import infrastructure and domain;
# infrastructure may reach down into domain but never up into
# application or interface; nothing but interface imports interface;
# domain is the pure bottom layer.
LAYER_ALLOWED: dict[str, set[str]] = {
    "interface": {"interface", "application", "domain", "infrastructure", "neutral"},
    "application": {"application", "domain", "infrastructure", "neutral"},
    "infrastructure": {"infrastructure", "domain", "neutral"},
    "domain": {"domain", "neutral"},
    "neutral": {"neutral"},
}

# COMMON_ARCHITECTURE_STANDARD.md section 2's own folder names, as
# (match_type, pattern, layer) rules. "prefix" matches a path prefix
# (relative to app_dir, forward slashes, trailing "/"); "component"
# matches any single path segment, anywhere in the relative path --
# the Python layout nests interface/application/domain/infrastructure
# directly under an arbitrary package directory, so a path-prefix
# rule can't name that package dir in advance.
DEFAULT_LAYER_RULES: list[tuple[str, str, str]] = [
    ("prefix", "src/app/", "interface"),
    ("prefix", "src/components/", "interface"),
    ("prefix", "src/server/actions/", "interface"),
    ("prefix", "src/server/application/", "application"),
    ("prefix", "src/server/domain/", "domain"),
    ("prefix", "src/server/infrastructure/", "infrastructure"),
    ("prefix", "src/lib/", "neutral"),
    ("prefix", "src/config/", "neutral"),
    ("component", "interface", "interface"),
    ("component", "application", "application"),
    ("component", "domain", "domain"),
    ("component", "infrastructure", "infrastructure"),
    ("component", "config", "neutral"),
]

# tests/ (and the engine's own tests/scenes/, tests/fakes/) legitimately
# import across every layer to exercise it -- never checked for layer
# direction or import cycles, only for size/banned-name/words.
TEST_DIR_PREFIXES = ("tests/", "test/", "__tests__/")

VAGUE_BASENAME_RE = re.compile(r"^(utils?|helpers?|misc|common|temp|tmp)\d*$", re.I)
NEW_PREFIX_RE = re.compile(r"^new[_-]", re.I)
V2_SUFFIX_RE = re.compile(r"[_-]v\d+$", re.I)
_DIGIT_RUN_RE = re.compile(r"\d+")


@dataclass
class Violation:
    check: str
    file: str
    detail: str

    def row(self) -> tuple[str, str, str]:
        return (self.check, self.file, self.detail)


@dataclass
class SourceFile:
    path: Path
    rel: str  # posix, relative to app_dir
    stack: str  # "ts" | "py"
    layer: str | None
    text: str


# --------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------

def discover_files(app_dir: Path) -> list[Path]:
    found: list[Path] = []
    for path in app_dir.rglob("*"):
        if not path.is_file() or path.suffix not in SOURCE_EXTENSIONS:
            continue
        if any(part in EXCLUDE_DIR_NAMES for part in path.relative_to(app_dir).parts[:-1]):
            continue
        found.append(path)
    return sorted(found)


def is_test_path(rel: str) -> bool:
    return rel.startswith(TEST_DIR_PREFIXES)


def resolve_layer(rel: str, rules: list[tuple[str, str, str]]) -> str | None:
    """Longest-prefix "prefix" rule wins over a "component" rule; among
    rules of the same kind, the longest match wins. A test path is
    never assigned a layer (see TEST_DIR_PREFIXES)."""
    if is_test_path(rel):
        return None
    best: tuple[int, str] | None = None
    parts = rel.split("/")
    for kind, pattern, layer in rules:
        if kind == "prefix" and rel.startswith(pattern):
            score = len(pattern) + 1000  # prefix rules outrank component rules
        elif kind == "component" and pattern in parts[:-1]:
            score = len(pattern)
        else:
            continue
        if best is None or score > best[0]:
            best = (score, layer)
    return best[1] if best else None


def load_sources(app_dir: Path, rules: list[tuple[str, str, str]]) -> list[SourceFile]:
    sources = []
    for path in discover_files(app_dir):
        rel = path.relative_to(app_dir).as_posix()
        stack = "py" if path.suffix in PY_EXTENSIONS else "ts"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sources.append(SourceFile(path, rel, stack, resolve_layer(rel, rules), text))
    return sources


# --------------------------------------------------------------------
# Check: file size
# --------------------------------------------------------------------

def check_file_size(sources: list[SourceFile]) -> list[Violation]:
    out = []
    for src in sources:
        n = src.text.count("\n") + 1
        if n > MAX_LINES:
            out.append(Violation("file_size", src.rel, f"{n} lines (> {MAX_LINES})"))
    return out


# --------------------------------------------------------------------
# Import extraction (TS/JS)
# --------------------------------------------------------------------

_TS_IMPORT_FROM = re.compile(
    r'^\s*(?:export\s+)?import\s+(?P<clause>[^;\n]*?)\s+from\s+["\'](?P<spec>[^"\']+)["\']',
    re.M,
)
_TS_IMPORT_BARE = re.compile(r'^\s*import\s+["\'](?P<spec>[^"\']+)["\']', re.M)
_TS_EXPORT_FROM = re.compile(
    r'^\s*export\s+(?:\{[^}]*\}\s+|\*\s+)?from\s+["\'](?P<spec>[^"\']+)["\']', re.M,
)
_TS_REQUIRE = re.compile(r'require\(\s*["\'](?P<spec>[^"\']+)["\']\s*\)')
_TS_DYNAMIC = re.compile(r'import\(\s*["\'](?P<spec>[^"\']+)["\']\s*\)')


@dataclass
class ImportRef:
    spec: str
    names: list[str] = field(default_factory=list)  # named imports (exported names)


def extract_ts_imports(text: str) -> list[ImportRef]:
    refs: list[ImportRef] = []
    for m in _TS_IMPORT_FROM.finditer(text):
        clause, spec = m.group("clause"), m.group("spec")
        names: list[str] = []
        brace = re.search(r"\{([^}]*)\}", clause)
        if brace:
            for token in brace.group(1).split(","):
                token = token.strip()
                if not token:
                    continue
                names.append(token.split(" as ")[0].strip().lstrip("type").strip())
        refs.append(ImportRef(spec, names))
    for pattern in (_TS_IMPORT_BARE, _TS_EXPORT_FROM, _TS_REQUIRE, _TS_DYNAMIC):
        for m in pattern.finditer(text):
            refs.append(ImportRef(m.group("spec"), []))
    return refs


def resolve_ts_spec(importer: Path, spec: str, app_dir: Path) -> Path | None:
    if spec.startswith("."):
        base = (importer.parent / spec).resolve()
    elif spec.startswith("@/"):
        src_root = app_dir / "src" if (app_dir / "src").is_dir() else app_dir
        base = (src_root / spec[2:]).resolve()
    else:
        return None  # external package
    candidates = [base.with_name(base.name + ext) if ext else base for ext in ("",) + TS_EXTENSIONS]
    candidates += [base / f"index{ext}" for ext in TS_EXTENSIONS]
    for cand in candidates:
        if cand.is_file():
            try:
                cand.relative_to(app_dir)
            except ValueError:
                return None  # resolves outside app_dir -- treat as external
            return cand
    return None


# --------------------------------------------------------------------
# Import extraction (Python -- absolute package imports, per the
# standard's Python layout; "from ." relative imports are also read,
# since the engine's own code uses them for sibling modules)
# --------------------------------------------------------------------

def extract_py_imports(text: str) -> list[tuple[str, int, list[str]]]:
    """[(module, leading_dot_count, names)]. `module` is "" for a bare
    "from . import x" (dots alone name the package); `leading_dot_count`
    is 0 for an absolute import.

    Walks only `tree.body` (module-scope statements), via `ast`, not
    every `Import`/`ImportFrom` node in the file -- an import inside a
    function/method body is a deliberately deferred import (this
    codebase's own convention for breaking a real cycle, e.g.
    `_run_full_job_and_report`'s own `from engine import full_orbit`),
    never a static module-to-module dependency, so it must not count
    toward the cycle/layer checks below. Falls back to an empty list
    on a syntax error (never crashes the whole run over one bad file)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out: list[tuple[str, int, list[str]]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((alias.name, 0, []))
        elif isinstance(node, ast.ImportFrom):
            names = [alias.name for alias in node.names if alias.name != "*"]
            out.append((node.module or "", node.level, names))
    return out


def resolve_py_module(importer: Path, module: str, dots: int, app_dir: Path) -> Path | None:
    if dots > 0:
        start = importer.parent
        for _ in range(dots - 1):
            start = start.parent
        target = start / Path(module.replace(".", "/")) if module else start
    else:
        target = app_dir / Path(module.replace(".", "/"))
    for cand in (target.with_name(target.name + ".py"), target / "__init__.py"):
        if cand.is_file():
            try:
                cand.relative_to(app_dir)
            except ValueError:
                return None
            return cand
    return None


# --------------------------------------------------------------------
# Build the whole-project internal import graph
# --------------------------------------------------------------------

@dataclass
class Edge:
    src: SourceFile
    dst: Path
    names: list[str]


def build_edges(sources: list[SourceFile], app_dir: Path) -> list[Edge]:
    by_path = {s.path: s for s in sources}
    edges: list[Edge] = []
    for src in sources:
        if src.stack == "ts":
            for ref in extract_ts_imports(src.text):
                dst = resolve_ts_spec(src.path, ref.spec, app_dir)
                if dst is not None and dst in by_path and dst != src.path:
                    edges.append(Edge(src, dst, ref.names))
        else:
            for module, dots, names in extract_py_imports(src.text):
                dst = resolve_py_module(src.path, module, dots, app_dir)
                if dst is not None and dst in by_path and dst != src.path:
                    edges.append(Edge(src, dst, names))
    return edges


# --------------------------------------------------------------------
# Check: layer direction
# --------------------------------------------------------------------

def check_layer_direction(edges: list[Edge], by_path: dict[Path, SourceFile]) -> list[Violation]:
    out = []
    for edge in edges:
        dst_src = by_path.get(edge.dst)
        if dst_src is None or edge.src.layer is None or dst_src.layer is None:
            continue
        allowed = LAYER_ALLOWED.get(edge.src.layer, set())
        if dst_src.layer not in allowed:
            out.append(Violation(
                "layer_direction", edge.src.rel,
                f"imports {dst_src.rel} ({edge.src.layer} -> {dst_src.layer} not allowed)",
            ))
    return out


# --------------------------------------------------------------------
# Check: cross-module private-name imports
# --------------------------------------------------------------------

def check_private_imports(edges: list[Edge]) -> list[Violation]:
    out = []
    for edge in edges:
        for name in edge.names:
            if name.startswith("_") and not name.startswith("__"):
                out.append(Violation(
                    "private_import", edge.src.rel,
                    f"imports private name {name!r} from {edge.dst.name}",
                ))
    return out


# --------------------------------------------------------------------
# Check: import cycles
# --------------------------------------------------------------------

def check_cycles(edges: list[Edge], by_path: dict[Path, SourceFile]) -> list[Violation]:
    graph: dict[Path, set[Path]] = {}
    for edge in edges:
        graph.setdefault(edge.src.path, set()).add(edge.dst)

    state: dict[Path, int] = {}  # 0 unvisited, 1 in-progress, 2 done
    reported: set[frozenset[Path]] = set()
    out: list[Violation] = []

    def dfs(node: Path, stack: list[Path]) -> None:
        state[node] = 1
        stack.append(node)
        for nxt in sorted(graph.get(node, ()), key=str):
            if state.get(nxt, 0) == 0:
                dfs(nxt, stack)
            elif state.get(nxt) == 1:
                cycle_start = stack.index(nxt)
                cycle = stack[cycle_start:] + [nxt]
                key = frozenset(cycle)
                if key not in reported:
                    reported.add(key)
                    names = " -> ".join(by_path[p].rel for p in cycle)
                    out.append(Violation("import_cycle", by_path[node].rel, names))
        stack.pop()
        state[node] = 2

    for node in sorted(graph, key=str):
        if state.get(node, 0) == 0:
            dfs(node, [])
    return out


# --------------------------------------------------------------------
# Check: banned / vague / near-duplicate names
# --------------------------------------------------------------------

def _levenshtein1(a: str, b: str) -> bool:
    """True iff a and b are at most one edit apart (cheap check: only
    needs to tell "0 or 1" from "more than 1" apart, never the exact
    distance)."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(1 for x, y in zip(a, b) if x != y) <= 1
    longer, shorter = (a, b) if len(a) > len(b) else (b, a)
    for i in range(len(longer)):
        if longer[:i] + longer[i + 1:] == shorter:
            return True
    return False


def check_banned_names(sources: list[SourceFile]) -> list[Violation]:
    out = []
    for src in sources:
        stem = Path(src.rel).stem
        if VAGUE_BASENAME_RE.match(stem) or NEW_PREFIX_RE.match(stem) or V2_SUFFIX_RE.search(stem):
            out.append(Violation("banned_name", src.rel, f"vague/placeholder name {stem!r}"))

    by_dir: dict[str, list[SourceFile]] = {}
    for src in sources:
        by_dir.setdefault(str(Path(src.rel).parent), []).append(src)
    seen_pairs: set[frozenset[str]] = set()
    for _dirname, group in by_dir.items():
        for i, a in enumerate(group):
            stem_a = Path(a.rel).stem.lower()
            for b in group[i + 1:]:
                stem_b = Path(b.rel).stem.lower()
                if stem_a == stem_b:
                    continue
                if _DIGIT_RUN_RE.sub("#", stem_a) == _DIGIT_RUN_RE.sub("#", stem_b):
                    continue  # e.g. "stage5-actions" vs "stage7-actions" -- a
                    # deliberate numbered series, not a near-duplicate name
                is_plural = stem_a + "s" == stem_b or stem_b + "s" == stem_a
                if is_plural or (len(stem_a) >= 4 and len(stem_b) >= 4 and _levenshtein1(stem_a, stem_b)):
                    pair_key = frozenset((a.rel, b.rel))
                    if pair_key in seen_pairs:
                        continue
                    seen_pairs.add(pair_key)
                    out.append(Violation(
                        "near_duplicate_name", a.rel, f"near-duplicate of {b.rel}",
                    ))
    return out


# --------------------------------------------------------------------
# Check: industry-hardcoding suspect words (opt-in only -- see module
# docstring; `words` is [] unless the caller passes --words)
# --------------------------------------------------------------------

def check_hardcoded_words(sources: list[SourceFile], words: list[str]) -> list[Violation]:
    if not words:
        return []
    out = []
    patterns = [(w, re.compile(r"\b" + re.escape(w) + r"\b", re.I)) for w in words]
    for src in sources:
        for word, pattern in patterns:
            if pattern.search(src.text):
                out.append(Violation("hardcoded_word", src.rel, f"contains suspect word {word!r}"))
    return out


# --------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------

def run(app_dir: Path, rules: list[tuple[str, str, str]], words: list[str]) -> list[Violation]:
    sources = load_sources(app_dir, rules)
    by_path = {s.path: s for s in sources}
    edges = build_edges(sources, app_dir)

    violations: list[Violation] = []
    violations += check_file_size(sources)
    violations += check_layer_direction(edges, by_path)
    violations += check_private_imports(edges)
    violations += check_cycles(edges, by_path)
    violations += check_banned_names(sources)
    violations += check_hardcoded_words(sources, words)
    return violations


def _print_table(violations: list[Violation]) -> None:
    if not violations:
        print("arch_check: no violations")
        return
    rows = [v.row() for v in violations]
    widths = [max(len(r[i]) for r in rows + [("CHECK", "FILE", "DETAIL")]) for i in range(3)]
    header = ("CHECK", "FILE", "DETAIL")
    for row in [header, *rows]:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    print(f"\n{len(violations)} violation(s)")


def load_layer_rules(path: str | None) -> list[tuple[str, str, str]]:
    if not path:
        return DEFAULT_LAYER_RULES
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [(r["match"], r["pattern"], r["layer"]) for r in data]


def load_words(path: str | None) -> list[str]:
    if not path:
        return []
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arch_check")
    parser.add_argument("app_dir")
    parser.add_argument("--layers", default=None,
                        help="JSON file: list of {match, pattern, layer} rules, "
                             "replacing both stacks' default layer map")
    parser.add_argument("--words", default=None,
                        help="newline-separated suspect-word list file (default: none)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    app_dir = Path(args.app_dir).resolve()
    if not app_dir.is_dir():
        print(f"arch_check: not a directory: {app_dir}", file=sys.stderr)
        return 2

    rules = load_layer_rules(args.layers)
    words = load_words(args.words)
    violations = run(app_dir, rules, words)

    if args.json:
        print(json.dumps({
            "app_dir": str(app_dir), "ok": not violations,
            "violations": [vars(v) for v in violations],
        }, ensure_ascii=False, indent=1))
    else:
        _print_table(violations)

    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
