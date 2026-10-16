"""Scans the whole repo (except .git and this file) for generic, publicly-safe-to-publish
patterns that would mean server/account info leaked into a public repo: home-directory
paths, /srv paths, Windows user paths, IPv4 addresses (excluding loopback/any) and email
addresses.

Specific sensitive identifiers (a company/brand name, a borrowed service/brand name, a
person's chat handle, a real server account, an internal project name, an industry example
that must stay generic) are deliberately NOT listed in this file, even as things to search
FOR - listing them here would publish them, in the very file meant to keep them out. That real
list lives in a local, gitignored file instead:
tests/public_ready.local.txt (path overridable via GONGBANG_PUBLIC_READY_EXTRA), one regex
per line, '#'-prefixed lines ignored. See tests/public_ready.local.example.txt for the format
(filled with placeholder words, safe to commit). On a fresh clone with no local list, the
local-list test is skipped rather than failing - the generic structural check below still
runs unconditionally and is the one this repo's own CI/public-readiness check relies on.
"""
import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THIS_FILE = os.path.abspath(__file__)
def _is_skip_dir(name: str) -> bool:
    return name in (".git", "__pycache__", ".pytest_cache", "node_modules") or name.startswith(".venv") or name == "venv"

# Only generic, structural patterns - no company name, brand name, person's handle, account
# name or project codename belongs in this list. See the module docstring for why.
GENERIC_PATTERNS = [
    r"/home/[A-Za-z0-9_.-]+",
    r"/srv/[A-Za-z0-9_.-]+",
    r"C:\\Users\\[A-Za-z0-9_.-]+",
    # IPv4 address, excluding loopback/any (127.0.0.1, 0.0.0.0) which are not server info
    r"\b(?!127\.0\.0\.1\b)(?!0\.0\.0\.0\b)(?:\d{1,3}\.){3}\d{1,3}\b",
    # email address
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
]


def _local_list_path() -> str:
    return os.environ.get("GONGBANG_PUBLIC_READY_EXTRA") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "public_ready.local.txt"
    )


def _load_local_patterns() -> list[str]:
    path = _local_list_path()
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line)
    return out


def iter_text_files(extra_skip: set[str] = frozenset()):
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if not _is_skip_dir(d)]
        for name in filenames:
            path = os.path.join(dirpath, name)
            abspath = os.path.abspath(path)
            if abspath == THIS_FILE or abspath in extra_skip:
                continue
            yield path


def _scan(patterns: list[str], extra_skip: set[str] = frozenset()) -> list[str]:
    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
    hits = []
    for path in iter_text_files(extra_skip):
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception:
            continue
        for rx in compiled:
            for m in rx.finditer(content):
                hits.append(f"{os.path.relpath(path, REPO_ROOT)}: {m.group(0)!r} ({rx.pattern})")
    return hits


def test_no_generic_server_identifiers_anywhere():
    hits = _scan(GENERIC_PATTERNS)
    assert not hits, "forbidden strings found:\n" + "\n".join(hits)


def test_no_local_extra_identifiers_anywhere():
    patterns = _load_local_patterns()
    if not patterns:
        pytest.skip(f"no local identifier list at {_local_list_path()} - nothing extra to check")
    # The local list file itself necessarily contains the real identifiers (that's its whole
    # job) - it must not be scanned against its own patterns, or every real list would fail
    # on itself. tests/public_ready.local.example.txt (committed) is placeholder-only, so it
    # is intentionally still scanned.
    hits = _scan(patterns, extra_skip={os.path.abspath(_local_list_path())})
    assert not hits, "forbidden strings found:\n" + "\n".join(hits)
