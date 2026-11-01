#!/usr/bin/env python3
"""Public-readiness scan: finds generic, structurally-identifiable strings (home-directory
paths, /srv-style server paths, Windows user paths, non-loopback IPv4 addresses, email
addresses) that would mean server or account info leaked into a public repo.

Used two ways:
  1. CLI:  public_ready_scan.py <repo-root> [extra-list]   -> prints hits, exit 1 if any
  2. Library: `from harness.public_ready_scan import scan` for tests/test_public_ready.py.

Specific sensitive identifiers (a company/brand name, a person's handle, a real server
account, a project codename) are deliberately NOT listed here, even as things to search FOR -
listing them here would publish them, in the very file meant to keep them out. That real list
lives in a local, gitignored file instead: tests/public_ready.local.txt (path overridable via
GONGBANG_PUBLIC_READY_EXTRA), one regex per line, '#'-prefixed lines ignored. See
tests/public_ready.local.example.txt for the format (placeholders only, safe to commit).
"""
import os
import re
import subprocess

# Only generic, structural patterns - no company name, brand name, person's handle, account
# name or project codename belongs in this list.
GENERIC = [
    r"/home/[A-Za-z0-9_.-]+",
    r"/srv/[A-Za-z0-9_.-]+",
    r"C:\\Users\\[A-Za-z0-9_.-]+",
    # IPv4 address, excluding loopback/any (127.0.0.1, 0.0.0.0) which are not server info
    r"\b(?!127\.0\.0\.1\b)(?!0\.0\.0\.0\b)(?:\d{1,3}\.){3}\d{1,3}\b",
    # email address
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
]
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv"}
SKIP_FILES = {"test_public_ready.py", "public_ready.local.txt", "public_ready.local.example.txt"}


def load_list(root: str) -> list[str]:
    cands = [
        os.environ.get("GONGBANG_PUBLIC_READY_EXTRA") or "",
        os.path.join(root, "tests", "public_ready.local.txt"),
        os.path.expanduser("~/.gongbang/public_ready.local.txt"),
    ]
    out: list[str] = []
    for p in cands:
        if p and os.path.isfile(p):
            for line in open(p, encoding="utf-8"):
                s = line.strip()
                if s and not s.startswith("#"):
                    out.append(s)
    return out


def tracked_files(root: str) -> list[str] | None:
    """Only what git would push. Untracked/ignored files never leave the machine, and walking a
    big working tree made a push hook time out in the private workspace this tool was built in."""
    try:
        out = subprocess.check_output(["git", "-C", root, "ls-files", "-z"], stderr=subprocess.DEVNULL)
    except Exception:
        return None
    return [os.path.join(root, f) for f in out.decode("utf-8", "ignore").split("\0") if f]


def all_files(root: str) -> list[str]:
    files: list[str] = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        files.extend(os.path.join(dp, fn) for fn in fns)
    return files


def scan(root: str, extra_skip: set[str] = frozenset()) -> list[str]:
    rx = [re.compile(p, re.IGNORECASE) for p in GENERIC + load_list(root)]
    hits: list[str] = []
    for path in (tracked_files(root) or all_files(root)):
        abspath = os.path.abspath(path)
        if os.path.basename(path) in SKIP_FILES or abspath in extra_skip or not os.path.isfile(path):
            continue
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for r in rx:
                m = r.search(line)
                if m:
                    hits.append(f"{os.path.relpath(path, root)}:{i}: {r.pattern[:30]} -> {m.group(0)[:40]}")
                    break
    return hits


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("usage: public_ready_scan.py <repo-root> [extra-list-file]", file=sys.stderr)
        sys.exit(2)
    root = os.path.abspath(sys.argv[1])
    if len(sys.argv) > 2:
        os.environ["GONGBANG_PUBLIC_READY_EXTRA"] = sys.argv[2]
    h = scan(root)
    print("\n".join(h) if h else "clean")
    sys.exit(1 if h else 0)
