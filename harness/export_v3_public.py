#!/usr/bin/env python3
"""export_v3_public.py <private _shared dir> <commit hash> <output v3/ dir>

Builds the public v3/ tool bundle from a FIXED COMMIT of the private workspace's
`_shared/` directory. This is the only way v3/ is produced - never hand-edit files under
v3/tools/; fix the private source, re-run this script against a new commit.

Why a commit, not the working tree: the private `_shared/` is a live, frequently-changed
source of truth shared with other work. Extracting from `git show <commit>:<path>` instead of
the filesystem means the public bundle is reproducible from a named point in history,
independent of whatever the working tree looks like when this script happens to run.

What it does:
  1. Reads each allow-listed file's content as of <commit> (git show, not the filesystem).
  2. Applies a per-file table of substitutions (below) that replace internal absolute paths,
     internal adapter targets and internal app-name mentions with public, configurable
     equivalents - documented in docs/v3-adapters.md.
  3. Writes the transformed files under <output>/tools/ (wt-bin/ subfolder preserved) and
     generates wt-bin/pnpm, wt-bin/yarn as symlinks to wt-bin/npm (same install-refusing wrapper,
     dispatched by basename).
  4. Scans the transformed output for anything that looks like it leaked anyway (generic
     path patterns, and any name that is an app folder in the source tree's apps root) and
     refuses (exit 1, with the list) if it finds any - transformation is not "trust me", it is
     checked.
  5. Writes <output>/MANIFEST.json: source commit, per-file sha256 of the ORIGINAL (pre-
     transform) content, and the version of the substitution-rule table that produced it.

Exit codes: 0 ok, 1 leftover identifiers after transform (listed on stdout), 2 bad arguments /
source problems.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys

RULES_VERSION = "1"

# Files pulled from the private _shared/ at the fixed commit. Each entry is
# (source-relative-path, dest-relative-path-under-v3/tools/). wt-bin/pnpm and wt-bin/yarn are
# not listed: they are generated (symlinks to wt-bin/npm), not extracted.
ALLOWED_FILES = [
    ("wt-lib.sh", "wt-lib.sh"),
    ("wt-new.sh", "wt-new.sh"),
    ("wt-run.sh", "wt-run.sh"),
    ("wt-stop.sh", "wt-stop.sh"),
    ("wt-land.sh", "wt-land.sh"),
    ("wt-migrate.sh", "wt-migrate.sh"),
    ("wt-gc.sh", "wt-gc.sh"),
    ("wt-adopt.sh", "wt-adopt.sh"),
    ("scope-check.sh", "scope-check.sh"),
    ("wt-env.py", "wt-env.py"),
    ("wt-bin/npm", "wt-bin/npm"),
    ("deploy.sh", "deploy.sh"),
    ("review-gate.sh", "review-gate.sh"),
]
# Explicitly excluded (plan 2026-11-01 "공개판 전에 로컬에서 먼저 고칠 것" / item B "제외"):
#   app_facts.py (internal fact-card generator), work_claim.py (Markdown-line queue, the
#   private task tracker does not match it), crew_metrics.py (depends on internal paths),
#   ship.sh (REVIEW_GATE adapter target - documented in docs/v3-adapters.md as "adapter, stop
#   if absent", not
#   extracted because it calls back into review-gate.sh's internal Codex/QA wiring that is
#   itself out of scope here), kit/ (app-side library, not a worktree tool).

# --- substitution rules -------------------------------------------------------------------
# Order per file: GLOBAL (every file, mechanical path/adapter patterns repeated verbatim across
# several files - the private source's own "run from a private copy" header is byte-identical
# in 8 of the 13 files) -> per-file table (semantic rewrites specific to one file) -> BACKSTOP
# (generic catch-all, see below). A hit surviving all three into the leftover check means a new
# row belongs in one of these tables, not that the backstop quietly did the right thing.
GLOBAL: list[tuple[re.Pattern, str]] = [
    # the self-copy-and-exec header (private source commit 5b12eeb, 2026-11-01) carries its own
    # inline default before wt-lib.sh is sourced - identical text in 8 of the 13 files.
    (re.compile(r'_run="\$\{WT_DATA:-/srv/data/_wt\}/\.run"'), '_run="${WT_DATA:?WT_DATA 환경변수가 필요합니다}/.run"'),
    (re.compile(r'/srv/data/_deploy-prev\b'), "${WT_DEPLOY_STATE}"),
    (re.compile(r'/srv/data/_wt\b'), "${WT_DATA}"),
    (re.compile(r'/srv/apps/_shared\b'), "${WT_SHARED}"),
    (re.compile(r'/srv/apps/_wt\b'), "${WT_ROOT}"),
    (re.compile(r'/srv/apps/'), "${WT_APPS_ROOT}/"),
    (re.compile(r'/srv/backups\b'), "${WT_BACKUPS}"),
    (re.compile(r'_AGENCY_OS/plans/2026-11-01-wt-interface\.md'), "docs/v3-wt-interface.md"),
    # the private workspace's systemd unit-name prefix convention -> configurable, default kept
    # (a generic word, not an identifier) so unit names stay readable without WT_UNIT_PREFIX set.
    (re.compile(r'\bifapp-'), "${WT_UNIT_PREFIX:-ifapp}-"),
]

_RULES: dict[str, list[tuple[re.Pattern, str]]] = {
    "wt-lib.sh": [
        (re.compile(r'WT_ROOT="\$\{WT_ROOT:-/srv/apps/_wt\}"'),
         'WT_APPS_ROOT="${WT_APPS_ROOT:?WT_APPS_ROOT 환경변수가 필요합니다 (앱들이 있는 폴더)}"\n'
         'WT_ROOT="${WT_ROOT:-$WT_APPS_ROOT/_wt}"'),
        (re.compile(r'WT_DATA="\$\{WT_DATA:-/srv/data/_wt\}"'),
         'WT_DATA="${WT_DATA:?WT_DATA 환경변수가 필요합니다 (작업 DB 슬롯·로그 루트)}"'),
        (re.compile(r'WT_ADMIN_ENV="\$\{WT_ADMIN_ENV:-/srv/apps/search/\.env\}"'),
         'WT_ADMIN_ENV="${WT_ADMIN_ENV:?WT_ADMIN_ENV 필요 — 시험 DB 생성 권한이 있는 postgres 역할의 env 파일 경로}"'),
        (re.compile(r'local d="/srv/apps/\$1"'), 'local d="$WT_APPS_ROOT/$1"'),
        (re.compile(r'git -C "/srv/apps/\$1" rev-parse'), 'git -C "$WT_APPS_ROOT/$1" rev-parse'),
        (re.compile(r'\[ -d "/srv/apps/\$2/\.git" \]'), '[ -d "$WT_APPS_ROOT/$2/.git" ]'),
        (re.compile(r'echo "/srv/apps/\$2/\.claude/verify-env\.json"'),
         'echo "$WT_APPS_ROOT/$2/.claude/verify-env.json"'),
        (re.compile(r'# Contract: _AGENCY_OS/plans/2026-11-01-wt-interface\.md\.'),
         "# Contract: docs/v3-wt-interface.md."),
        (re.compile(r' \(team-lead 11-01: 6 -> 8\)'), ""),
        (re.compile(r'adopted hand-made copies \([^)]+\)'), "adopted hand-made copies"),
        (re.compile(r'\(drizzle-kit apps: [^)]+\)'), "(drizzle-kit apps)"),
    ],
    "wt-new.sh": [
        (re.compile(r'src="/srv/apps/\$app"'), 'src="$WT_APPS_ROOT/$app"'),
        (re.compile(r'# Contract: _AGENCY_OS/plans/2026-11-01-wt-interface\.md[^\n]*'),
         "# Contract: docs/v3-wt-interface.md §1."),
        (re.compile(r'\(DATABASE_OWNER_URL, [^)]+\)'), "(DATABASE_OWNER_URL)"),
        (re.compile(r'# root \([^)]+\)\. Same filesystem:'), "# root (observed on two apps). Same filesystem:"),
        (re.compile(r'\(env file TEST_DATABASE_URL, e\.g\. [^:]+: backup/forms\n  # roles\)'),
         "(env file TEST_DATABASE_URL: roles granted there)"),
    ],
    "wt-run.sh": [
        (re.compile(r'src="/srv/apps/\$app"'), 'src="$WT_APPS_ROOT/$app"'),
        (re.compile(r'— contract: wt-interface\.md §2\.'), "— contract: docs/v3-wt-interface.md §2."),
        (re.compile(r'so a copy never reports to [^.]*ledger or library\.'),
         "so a copy never reports to a live internal service."),
    ],
    "wt-stop.sh": [
        (re.compile(r'— contract: wt-interface\.md §3\.'), "— contract: docs/v3-wt-interface.md §3."),
    ],
    "wt-land.sh": [
        (re.compile(r'src="/srv/apps/\$app"'), 'src="$WT_APPS_ROOT/$app"'),
        (re.compile(r'ship_lock="/srv/data/_deploy-prev/\$app\.review/ship\.lock"'),
         'ship_lock="$WT_DEPLOY_STATE/$app.review/ship.lock"'),
        (re.compile(r"DEPLOY_CMD in deploy\.env: [^)]+\.\.\."), "DEPLOY_CMD in deploy.env"),
        (re.compile(r'first unit 11-01: " M next-env\.d\.ts" stopped [^)\s]+'),
         'first observed: " M next-env.d.ts" stopped a land unexpectedly'),
        (re.compile(r"# helpers at the skill root or under scripts/ \([^)]+\)"),
         "# helpers at the skill root or under scripts/ (app-dependent)"),
        (re.compile(r"— wt-interface\.md §5\."), "— docs/v3-wt-interface.md §5."),
        (re.compile(r'if \[ -z "\$self_deploy" \]; then'), 'if [ -z "$self_deploy" ]; then'),
        # ship.sh is an explicit adapter (REVIEW_GATE=1 apps): fail clearly if missing, not with
        # a raw "No such file" from bash.
        (re.compile(r'if \[ "\$review_gate" = 1 \]; then deploy=\(bash "\$WT_SHARED/ship\.sh" "\$app"\); else deploy=\(bash "\$WT_SHARED/deploy\.sh" "\$app"\); fi'),
         'if [ "$review_gate" = 1 ]; then\n'
         '  [ -x "$WT_SHARED/ship.sh" ] || stop 2 "REVIEW_GATE=1 이지만 ship.sh 어댑터가 없습니다 — docs/v3-adapters.md 참고."\n'
         '  deploy=(bash "$WT_SHARED/ship.sh" "$app")\n'
         'else deploy=(bash "$WT_SHARED/deploy.sh" "$app"); fi'),
    ],
    "wt-migrate.sh": [
        (re.compile(r'src="/srv/apps/\$app"'), 'src="$WT_APPS_ROOT/$app"'),
        (re.compile(r'out="/srv/backups/\$app/'), 'out="$WT_BACKUPS/$app/'),
        (re.compile(r'mkdir -p "/srv/backups/\$app"'), 'mkdir -p "$WT_BACKUPS/$app"'),
    ],
    "wt-gc.sh": [
        # The private source's internal task-tracker sqlite lookup is an internal adapter.
        # Public version: a configurable command (WT_TASK_STATUS_CMD <task-id> -> prints a
        # status word on stdout), empty/unset means "unknown" - which this script already
        # treats as "keep" (see case "$st" below).
        (re.compile(
            r'OFFICE_DB="\$\{WT_OFFICE_DB:-[^}]+\}"\nSTALE_SEC',
            re.S,
        ), 'STALE_SEC'),
        (re.compile(
            r'task_status\(\) \{.*?\n\}\n',
            re.S,
        ),
         'task_status() {  # $1 task id -> status word from the adapter command, \'\' when unknown or unset\n'
         '  [ -n "$1" ] && [ -n "${WT_TASK_STATUS_CMD:-}" ] || return 0\n'
         '  $WT_TASK_STATUS_CMD "$1" 2>/dev/null || true\n'
         '}\n'),
        (re.compile(r"— wt-interface\.md §6\."), "— docs/v3-wt-interface.md §6."),
        (re.compile(r"Run at \S+ \S+ start and nightly 03:30\."),
         "Run at session start and on a nightly schedule."),
        (re.compile(r"its \S+ \S+ task \(task_id in \.wt\.json\) ended"), "its WT_TASK_STATUS_CMD task ended"),
        (re.compile(r"\(landed, or discarded by \S+ \S+\)"), "(landed, or discarded by the caller)"),
    ],
    "wt-adopt.sh": [
        (re.compile(r"\(no \.wt\.json\) so the limits, *\n"), "(no .wt.json) so the limits,\n"),
        (re.compile(r"— contract: wt-interface\.md §\?"), "— contract: docs/v3-wt-interface.md"),
        (re.compile(r'adopted hand-made copies \([^)]+\)'), "adopted hand-made copies"),
    ],
    "scope-check.sh": [
        (re.compile(r"— wt-interface\.md §4\."), "— docs/v3-wt-interface.md §4."),
    ],
    "wt-env.py": [
        (re.compile(
            r'INTERNAL_URL_KEYS = \("OS_URL", "LEDGER_URL", "LIBRARY_URL", "SEARCH_URL", "SEARCH_PUBLIC_URL"\)',
        ),
         'INTERNAL_URL_KEYS = tuple(k for k in os.environ.get("WT_INTERNAL_URL_KEYS", "").split(",") if k)'),
        (re.compile(r"# Internal-app address keys: always pointed at a dead port in a copy \(code defaults like OS_URL=http://127\.0\.0\.1:\d+ would\n# otherwise reach the live \S+ \S+ / ledger / library\)\. Set even when the env file lacks them; nothing overrides them\."),
         "# Internal-app address keys (configured via WT_INTERNAL_URL_KEYS, comma-separated): always pointed at a dead\n"
         "# port in a copy, so a copy's own code defaults never reach a live internal service by accident.\n"
         "# Set even when the env file lacks them; nothing overrides them."),
        # LIVE_DATA is a generic "anything under the live data root" prefix check, not one fixed
        # subtree (unlike WT_DATA/WT_DEPLOY_STATE above) - an adapter var of its own.
        (re.compile(r'LIVE_DATA = "/srv/data/"   # the apps\' live data root: a copy gets its own folder under <copy>/\.verify/data/<KEY>'),
         'LIVE_DATA = os.environ.get("WT_LIVE_DATA_ROOT", "")   # the apps\' live data root (adapter, optional): a copy\n'
         "# gets its own folder under <copy>/.verify/data/<KEY> for any value under it; unset disables this relocation."),
        (re.compile(r"an \S+ \S+ or foreman session"), "a calling session"),
        (re.compile(r"\(\S+ \S+, a shell\)"), "(a calling session, a shell)"),
        (re.compile(r" \(11-01: /srv/data/<app> defaults\)"), ""),
    ],
    "deploy.sh": [
        (re.compile(r'dir="/srv/apps/\$app"'), 'dir="$WT_APPS_ROOT/$app"'),
        (re.compile(r'HEAVY=/srv/apps/[^/\s]+/scripts/heavy\.sh.*\n'),
         '# Memory-capped build wrapper (adapter, optional): WT_HEAVY_CMD "--mem <cap> --name <n> -- <cmd...>".\n'
         '# Unset: the build command below runs directly, without a memory cap (warning printed).\n'
         'HEAVY="${WT_HEAVY_CMD:-}"\n'),
        (re.compile(r'PREV="/srv/data/_deploy-prev/\$app"'), 'PREV="$WT_DEPLOY_STATE/$app"'),
        (re.compile(
            r'if ! IF_BUILD_DIR=\.next-build NODE_OPTIONS="--max-old-space-size=\$NODE_HEAP" bash "\$HEAVY" --mem "\$BUILD_MEM" --name "\$app" -- npm run -s build; then'
        ),
         'build_failed=0\n'
         'if [ -n "$HEAVY" ]; then\n'
         '  IF_BUILD_DIR=.next-build NODE_OPTIONS="--max-old-space-size=$NODE_HEAP" bash $HEAVY --mem "$BUILD_MEM" --name "$app" -- npm run -s build || build_failed=1\n'
         'else\n'
         '  echo "경고 — WT_HEAVY_CMD 없음: 메모리 상한 없이 바로 빌드합니다." >&2\n'
         '  IF_BUILD_DIR=.next-build NODE_OPTIONS="--max-old-space-size=$NODE_HEAP" npm run -s build || build_failed=1\n'
         'fi\n'
         'if [ "$build_failed" = 1 ]; then'),
        (re.compile(r'# Common deploy for one Next\.js app \([^)]*\), based on [^(]*\([^)]*\)\.'),
         "# Common deploy for one Next.js app."),
        (re.compile(r' \(G2 A4, 2026-10-16\)'), ""),
        (re.compile(r'HEALTH_HOST="\d{1,3}(?:\.\d{1,3}){3}:3225"'), 'HEALTH_HOST="<tailnet-host>:3225"'),
        (re.compile(r'APP_DIR=/srv/apps/<app>/web \(Next project in a subfolder\)'),
         "APP_DIR=<path> (Next project in a subfolder)"),
        (re.compile(r' \([a-z0-9_-]+: web/\)'), ""),
        (re.compile(r'\([A-Za-z0-9_-]+ 0→152 errors shipped unnoticed\)'),
         "(an app once shipped hundreds of new type errors unnoticed)"),
        (re.compile(r'\([a-z0-9_-]+ 2026-10-17, owner "넣어라"\)'), "(observed once)"),
        (re.compile(r'DEPLOY_CMD_DEPLOY/_CHECK/_ROLLBACK \([a-z0-9_-]+\)\.'), "DEPLOY_CMD_DEPLOY/_CHECK/_ROLLBACK."),
    ],
    "review-gate.sh": [
        (re.compile(r'dir="/srv/apps/\$app"'), 'dir="$WT_APPS_ROOT/$app"'),
        (re.compile(r'state="/srv/data/_deploy-prev/\$app\.review"'), 'state="$WT_DEPLOY_STATE/$app.review"'),
        (re.compile(r"\([a-z0-9_-]+ first; other apps later\)"), "(enable per app, incrementally)"),
    ],
    "wt-bin/npm": [],
}

HEADER = {
    "sh": "# Generated from private source at {commit}; do not edit by hand (see harness/export_v3_public.py).\n",
    "py": "# Generated from private source at {commit}; do not edit by hand (see harness/export_v3_public.py).\n",
}


def file_kind(relpath: str) -> str:
    return "py" if relpath.endswith(".py") else "sh"


def insert_header(text: str, commit: str, kind: str) -> str:
    header = HEADER[kind].format(commit=commit)
    lines = text.splitlines(keepends=True)
    if lines and lines[0].startswith("#!"):
        return lines[0] + header + "".join(lines[1:])
    return header + text


def git_show(repo: str, commit: str, relpath: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", repo, "show", f"{commit}:{relpath}"], stderr=subprocess.PIPE
        ).decode("utf-8")
    except subprocess.CalledProcessError as e:
        raise SystemExit(
            f"원본에 없는 파일이거나 커밋이 잘못됐습니다: {relpath} @ {commit}\n{e.stderr.decode('utf-8', 'ignore')}"
        )


def transform(relpath: str, text: str) -> str:
    # per-file rules first (exact lines, including the ones GLOBAL's generic patterns would
    # otherwise mangle - e.g. wt-lib.sh's own `WT_ROOT="${WT_ROOT:-/srv/apps/_wt}"` default
    # definition), then GLOBAL as a catch-all for the rest (comments, repeated boilerplate).
    for rx, repl in _RULES.get(relpath, []):
        text = rx.sub(repl, text)
    for rx, repl in GLOBAL:
        text = rx.sub(repl, text)
    return text


def app_names(apps_root: str) -> list[str]:
    """Internal app folder names at the apps root, excluding workspace/tooling folders and
    names too short or too common to check safely (a generic English word used coincidentally
    in a comment should not be flagged as a leaked app name)."""
    skip_dirs = {"_shared", "_wt", "_AGENCY_OS", "_archive"}
    common = {"search", "office", "web", "app", "www", "docs", "data", "test", "tests"}
    out = []
    try:
        for name in os.listdir(apps_root):
            if name.startswith((".", "_")) or name in skip_dirs or name.lower() in common:
                continue
            if len(name) < 4:
                continue
            if os.path.isdir(os.path.join(apps_root, name)):
                out.append(name)
    except OSError:
        pass
    return out


def leftover_check(out_dir: str, apps_root: str) -> list[str]:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "public_ready_scan", os.path.join(os.path.dirname(os.path.abspath(__file__)), "public_ready_scan.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    hits = mod.scan(out_dir)
    names = app_names(apps_root)
    if names:
        name_rx = re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b")
        for dp, _, fns in os.walk(out_dir):
            for fn in fns:
                p = os.path.join(dp, fn)
                try:
                    text = open(p, encoding="utf-8", errors="ignore").read()
                except Exception:
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    m = name_rx.search(line)
                    if m:
                        hits.append(f"{os.path.relpath(p, out_dir)}:{i}: 앱 이름 -> {m.group(0)}")
    return hits


def main() -> None:
    if len(sys.argv) != 4:
        print(__doc__)
        raise SystemExit(2)
    src_shared, commit, out_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    src_shared = os.path.abspath(src_shared)
    out_dir = os.path.abspath(out_dir)
    if not os.path.isdir(os.path.join(src_shared, ".git")) and not os.path.isdir(
        os.path.join(os.path.dirname(src_shared), ".git")
    ):
        raise SystemExit(f"git 저장소가 아닙니다: {src_shared}")
    apps_root = os.path.dirname(src_shared)

    tools_dir = os.path.join(out_dir, "tools")
    os.makedirs(tools_dir, exist_ok=True)
    manifest = {"source_commit": commit, "rules_version": RULES_VERSION, "files": {}}

    for rel_src, rel_dst in ALLOWED_FILES:
        original = git_show(src_shared, commit, rel_src)
        manifest["files"][rel_dst] = hashlib.sha256(original.encode("utf-8")).hexdigest()
        transformed = transform(rel_src, original)
        transformed = insert_header(transformed, commit, file_kind(rel_src))
        dest = os.path.join(tools_dir, rel_dst)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(transformed)
        if rel_src.startswith("wt-bin/") or rel_src.endswith(".sh") or rel_src.endswith(".py"):
            os.chmod(dest, 0o755)

    # pnpm/yarn: same install-refusing wrapper, dispatched by basename (see wt-bin/npm's own
    # `tool=$(basename "$0")` logic) - symlinks, not copies, so there is exactly one script to fix.
    bin_dir = os.path.join(tools_dir, "wt-bin")
    for name in ("pnpm", "yarn"):
        link = os.path.join(bin_dir, name)
        if os.path.islink(link) or os.path.exists(link):
            os.remove(link)
        os.symlink("npm", link)

    with open(os.path.join(out_dir, "MANIFEST.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")

    hits = leftover_check(tools_dir, apps_root)
    if hits:
        print(f"치환 뒤 남은 식별자 {len(hits)}건 — v3/ 를 쓰지 마세요:")
        print("\n".join(hits))
        raise SystemExit(1)
    print(f"추출 완료 — {len(ALLOWED_FILES)}개 파일 + pnpm/yarn 래퍼 2개, 커밋 {commit}, 잔존 식별자 0건")


if __name__ == "__main__":
    main()
