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

RULES_VERSION = "2"

# The private source's own root-path conventions (where its apps live, where it keeps work-copy
# data, backups, deploy state, etc. on the machine this bundle is extracted from) are NEVER
# hardcoded in this file - that would put the private host's literal paths into a public repo.
# They are read from a local,
# gitignored config instead. See export_v3_private.example.json (committed, placeholders only)
# for the shape; copy it to export_v3_private.json (gitignored) with the real values to run an
# extraction. required keys: src_apps_root, src_data_root, src_backups_root,
# src_deploy_state_root, src_admin_env, src_unit_prefix.
REQUIRED_CFG_KEYS = (
    "src_apps_root", "src_data_root", "src_backups_root", "src_deploy_state_root",
    "src_admin_env", "src_unit_prefix", "src_ops_dirname", "src_archive_root",
)


def load_src_cfg() -> dict:
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.environ.get("WT_EXPORT_PRIVATE_CFG") or os.path.join(here, "export_v3_private.json")
    if not os.path.isfile(path):
        example = os.path.join(here, "export_v3_private.example.json")
        raise SystemExit(
            f"비공개 설정이 없습니다: {path}\n"
            f"{example}를 복사해 실제 경로로 채우세요(그 파일은 .gitignore 처리돼 있어 커밋되지 않습니다)."
        )
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_CFG_KEYS if not cfg.get(k)]
    if missing:
        raise SystemExit(f"비공개 설정에 빠진 키: {', '.join(missing)} ({path})")
    return cfg

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
    ("deploy-check.sh", "deploy-check.sh"),
    ("wt-scope.sh", "wt-scope.sh"),
    ("wt-sandbox.py", "wt-sandbox.py"),
    ("receipts.py", "receipts.py"),
    ("sec-gate.sh", "sec-gate.sh"),
    ("suite-check.py", "suite-check.py"),
    ("new-app.sh", "new-app.sh"),
]
# Explicitly excluded (plan 2026-11-01 "공개판 전에 로컬에서 먼저 고칠 것" / item B "제외"):
#   app_facts.py (internal fact-card generator), work_claim.py (Markdown-line queue, the
#   private task tracker does not match it), crew_metrics.py (depends on internal paths),
#   ship.sh (REVIEW_GATE adapter target - documented in docs/v3-adapters.md as "adapter, stop
#   if absent", not
#   extracted because it calls back into review-gate.sh's internal Codex/QA wiring that is
#   itself out of scope here), kit/ (app-side library, not a worktree tool).

# --- substitution rules -------------------------------------------------------------------
# Order per file: per-file table (exact lines, including the ones GLOBAL's generic patterns
# would otherwise mangle) -> GLOBAL (every file, mechanical path/adapter patterns repeated
# verbatim across several files - the private source's own "run from a private copy" header is
# byte-identical in 8 of the 13 wt-*.sh files). A hit surviving both into the leftover check
# means a new row belongs in one of these tables, not that GLOBAL quietly did the right thing.
# Every regex that needs to match one of the private source's own root paths builds it from
# `cfg` with `re.escape(...)` - never as a literal in this file's own source (see
# REQUIRED_CFG_KEYS above).


def build_global(cfg: dict) -> list[tuple[re.Pattern, str]]:
    apps_root = re.escape(cfg["src_apps_root"])
    data_root = re.escape(cfg["src_data_root"])
    backups_root = re.escape(cfg["src_backups_root"])
    deploy_state_root = re.escape(cfg["src_deploy_state_root"])
    unit_prefix = re.escape(cfg["src_unit_prefix"])
    ops_dirname = re.escape(cfg["src_ops_dirname"])
    return [
        # the self-copy-and-exec header (private source, 2026-11-01) carries its own inline
        # default before wt-lib.sh is sourced - identical text in 8 of the 13 wt-*.sh files.
        (re.compile(r'_run="\$\{WT_DATA:-' + data_root + r'\}/\.run"'),
         '_run="${WT_DATA:?WT_DATA 환경변수가 필요합니다}/.run"'),
        (re.compile(deploy_state_root + r'\b'), "${WT_DEPLOY_STATE}"),
        (re.compile(data_root + r'\b'), "${WT_DATA}"),
        (re.compile(apps_root + r'/_shared\b'), "${WT_SHARED}"),
        (re.compile(apps_root + r'/_wt\b'), "${WT_ROOT}"),
        # the private workspace's own ops/harness folder name (plans, a daily review tool, test-
        # trust checks) is an internal workspace name, not a generic convention - rename it like
        # any other internal identifier before the generic apps_root/ rule below touches the rest
        # of the path around it.
        (re.compile(r'\b' + ops_dirname + r'\b'), "_ops"),
        (re.compile(r'packages such as @[A-Za-z][A-Za-z0-9_-]*/kit\)'), "a shared internal package)"),
        (re.compile(apps_root + r'/'), "${WT_APPS_ROOT}/"),
        # anything left bare (not followed by a path separator - end of string, a shell `}`/`)`/
        # quote, etc.) after the rules above have consumed every apps_root+'/...' occurrence.
        (re.compile(apps_root + r'\b'), "${WT_APPS_ROOT}"),
        (re.compile(backups_root + r'\b'), "${WT_BACKUPS}"),
        # any remaining reference to the private plan doc this contract was generated from,
        # by whatever relative path it was written with - the suffix (e.g. " §1.") is left
        # alone since only "...wt-interface.md" itself is matched.
        (re.compile(r'[\w./-]*wt-interface\.md'), "docs/v3-wt-interface.md"),
        # the private workspace's systemd unit-name prefix convention -> configurable; the
        # fallback default equals the private value only because that value is itself a
        # generic word, not a secret - still read from cfg, never written here as a literal.
        (re.compile(r'\b' + unit_prefix + r'-'), '${WT_UNIT_PREFIX:-' + cfg["src_unit_prefix"] + '}-'),
    ]


def build_rules(cfg: dict) -> dict[str, list[tuple[re.Pattern, str]]]:
    apps_root = re.escape(cfg["src_apps_root"])
    data_root = re.escape(cfg["src_data_root"])
    backups_root = re.escape(cfg["src_backups_root"])
    deploy_state_root = re.escape(cfg["src_deploy_state_root"])
    admin_env = re.escape(cfg["src_admin_env"])
    archive_root = re.escape(cfg["src_archive_root"])
    ops_dirname = re.escape(cfg["src_ops_dirname"])
    # LIVE_DATA in wt-env.py is a prefix check against the data root's own PARENT (anything
    # under it, not only the _wt subtree) - derived from src_data_root (e.g. ".../_wt" ->
    # ".../"), not a separate required key, since every observed layout nests _wt under it.
    live_data_root = re.escape(cfg["src_data_root"].rstrip("/").rsplit("/_wt", 1)[0] + "/")
    return {
        "wt-lib.sh": [
            (re.compile(r'WT_ROOT="\$\{WT_ROOT:-' + apps_root + r'/_wt\}"'),
             'WT_APPS_ROOT="${WT_APPS_ROOT:?WT_APPS_ROOT 환경변수가 필요합니다 (앱들이 있는 폴더)}"\n'
             'WT_ROOT="${WT_ROOT:-$WT_APPS_ROOT/_wt}"'),
            (re.compile(r'WT_DATA="\$\{WT_DATA:-' + data_root + r'\}"'),
             'WT_DATA="${WT_DATA:?WT_DATA 환경변수가 필요합니다 (작업 DB 슬롯·로그 루트)}"'),
            (re.compile(r'WT_ADMIN_ENV="\$\{WT_ADMIN_ENV:-' + admin_env + r'\}"'),
             'WT_ADMIN_ENV="${WT_ADMIN_ENV:?WT_ADMIN_ENV 필요 — 시험 DB 생성 권한이 있는 postgres 역할의 env 파일 경로}"'),
            (re.compile(r'local d="' + apps_root + r'/\$1"'), 'local d="$WT_APPS_ROOT/$1"'),
            (re.compile(r'git -C "' + apps_root + r'/\$1" rev-parse'), 'git -C "$WT_APPS_ROOT/$1" rev-parse'),
            (re.compile(r'\[ -d "' + apps_root + r'/\$2/\.git" \]'), '[ -d "$WT_APPS_ROOT/$2/.git" ]'),
            (re.compile(r'echo "' + apps_root + r'/\$2/\.claude/verify-env\.json"'),
             'echo "$WT_APPS_ROOT/$2/.claude/verify-env.json"'),
            (re.compile(r' \(team-lead 11-01: 6 -> 8\)'), ""),
            (re.compile(r'adopted hand-made copies \([^)]+\)'), "adopted hand-made copies"),
            (re.compile(r'\(drizzle-kit apps: [^)]+\)'), "(drizzle-kit apps)"),
            (re.compile(r'[A-Za-z][A-Za-z0-9_.-]*/c-[A-Za-z][A-Za-z0-9_.-]*'), "an app's work copy"),
        ],
        "wt-new.sh": [
            (re.compile(r'src="' + apps_root + r'/\$app"'), 'src="$WT_APPS_ROOT/$app"'),
            (re.compile(r'\(DATABASE_OWNER_URL, [^)]+\)'), "(DATABASE_OWNER_URL)"),
            (re.compile(r'# root \([^)]+\)\. Same filesystem:'), "# root (observed on two apps). Same filesystem:"),
            (re.compile(r'\(env file TEST_DATABASE_URL, e\.g\. [^:]+: backup/forms\n  # roles\)'),
             "(env file TEST_DATABASE_URL: roles granted there)"),
        ],
        "wt-run.sh": [
            (re.compile(r'src="' + apps_root + r'/\$app"'), 'src="$WT_APPS_ROOT/$app"'),
            (re.compile(r'so a copy never reports to [^.]*ledger or library\.'),
             "so a copy never reports to a live internal service."),
        ],
        "wt-stop.sh": [],
        "wt-land.sh": [
            (re.compile(r'#  Apps with their own deploy \(DEPLOY_CMD\) are refused up front \(exit 2\): their rollback cannot be relied on\. Then workers'),
             "#  Apps with their own deploy (DEPLOY_CMD) go through the same land, with one extra independent confirmation step (5a) and their own recovery path on failure (self_deploy branch below). Workers"),
            (re.compile(r'src="' + apps_root + r'/\$app"'), 'src="$WT_APPS_ROOT/$app"'),
            (re.compile(r'ship_lock="' + deploy_state_root + r'/\$app\.review/ship\.lock"'),
             'ship_lock="$WT_DEPLOY_STATE/$app.review/ship.lock"'),
            (re.compile(r"DEPLOY_CMD in deploy\.env: [^)]+\.\.\."), "DEPLOY_CMD in deploy.env"),
            (re.compile(r'[a-z][a-z0-9_-]*, [a-z][a-z0-9_-]*, [a-z][a-z0-9_-]*, \.\.\.\)'), "example apps)"),
            (re.compile(r'[a-z][a-z0-9_-]*/[a-z][a-z0-9_-]* keep a pre-build copy and swap only after a good build; [a-z][a-z0-9_-]*\'s own comment'),
             "some apps keep a pre-build copy and swap only after a good build; one app's own comment"),
            (re.compile(r'실측: [A-Za-z][A-Za-z0-9_-]* wiring copy'), "실측: an app's wiring copy"),
            (re.compile(r'\([a-z][a-z0-9_-]*/[a-z][a-z0-9_-]*: pre-build kept, swap only on success, so a'),
             "(some apps: pre-build kept, swap only on success, so a"),
            (re.compile(r"of theirs; [a-z][a-z0-9_-]*'s own comment says the same:"),
             "of theirs; one app's own comment says the same:"),
            (re.compile(r'first unit 11-01: " M next-env\.d\.ts" stopped [^)\s]+'),
             'first observed: " M next-env.d.ts" stopped a land unexpectedly'),
            (re.compile(r"# helpers at the skill root or under scripts/ \([^)]+\)"),
             "# helpers at the skill root or under scripts/ (app-dependent)"),
            (re.compile(r'if \[ -z "\$self_deploy" \]; then'), 'if [ -z "$self_deploy" ]; then'),
            # ship.sh is an explicit adapter (REVIEW_GATE=1 apps): fail clearly if missing, not
            # with a raw "No such file" from bash.
            (re.compile(r'if \[ "\$review_gate" = 1 \]; then deploy=\(bash "\$WT_SHARED/ship\.sh" "\$app"\); else deploy=\(bash "\$WT_SHARED/deploy\.sh" "\$app"\); fi'),
             'if [ "$review_gate" = 1 ]; then\n'
             '  [ -x "$WT_SHARED/ship.sh" ] || stop 2 "REVIEW_GATE=1 이지만 ship.sh 어댑터가 없습니다 — docs/v3-adapters.md 참고."\n'
             '  deploy=(bash "$WT_SHARED/ship.sh" "$app")\n'
             'else deploy=(bash "$WT_SHARED/deploy.sh" "$app"); fi'),
        ],
        "wt-migrate.sh": [
            (re.compile(r'src="' + apps_root + r'/\$app"'), 'src="$WT_APPS_ROOT/$app"'),
            (re.compile(r'out="' + backups_root + r'/\$app/'), 'out="$WT_BACKUPS/$app/'),
            (re.compile(r'mkdir -p "' + backups_root + r'/\$app"'), 'mkdir -p "$WT_BACKUPS/$app"'),
        ],
        "wt-gc.sh": [
            # The private source's internal task-tracker sqlite lookup is an internal adapter.
            # Public version: a configurable command (WT_TASK_STATUS_CMD <task-id> -> prints a
            # status word on stdout), empty/unset means "unknown" - which this script already
            # treats as "keep" (see case "$st" below).
            (re.compile(r'OFFICE_DB="\$\{WT_OFFICE_DB:-[^}]+\}"\nSTALE_SEC', re.S), 'STALE_SEC'),
            (re.compile(r'task_status\(\) \{.*?\n\}\n', re.S),
             'task_status() {  # $1 task id -> status word from the adapter command, \'\' when unknown or unset\n'
             '  [ -n "$1" ] && [ -n "${WT_TASK_STATUS_CMD:-}" ] || return 0\n'
             '  $WT_TASK_STATUS_CMD "$1" 2>/dev/null || true\n'
             '}\n'),
            (re.compile(r"Run at \S+ \S+ start and nightly 03:30\."),
             "Run at session start and on a nightly schedule."),
            (re.compile(r"its \S+ \S+ task \(task_id in \.wt\.json\) ended"), "its WT_TASK_STATUS_CMD task ended"),
            (re.compile(r"\(landed, or discarded by \S+ \S+\)"), "(landed, or discarded by the caller)"),
            (re.compile(r'moved whole to ' + archive_root + r'/<날짜>/wt-stale/<app>-<name>'),
             "moved whole to $WT_ARCHIVE_ROOT/<날짜>/wt-stale/<app>-<name>"),
            (re.compile(r'gc removed an [A-Za-z][A-Za-z0-9_-]* wiring copy'), "gc removed a work copy"),
            (re.compile(r'실측: [A-Za-z][A-Za-z0-9_-]*/[A-Za-z][A-Za-z0-9_-]*, 11-01 23:32'), "실측: one app's copy"),
            (re.compile(r'실측: [A-Za-z][A-Za-z0-9_-]*/[A-Za-z][A-Za-z0-9_-]*'), "실측: one app's copy"),
            (re.compile(r'WITHOUT an [A-Z][A-Za-z ]* task'), "without a tracked task"),
            (re.compile(r'실측: [A-Za-z][A-Za-z0-9_-]* [a-z0-9]+/[a-z0-9]+/[a-z0-9]+가 가지는 master에'),
             "실측: 한 앱의 여러 사본이 가지는 master에"),
            (re.compile(r'task_status\(""\) is always "모름", so the \(b\)'), "task_status(\"\") is always \"모름\", so the (b)"),
            (re.compile(r'WT_ARCHIVE_ROOT="\$\{WT_ARCHIVE_ROOT:-' + archive_root + r'\}"   # test seam — tests must never write the real ' + archive_root),
             'WT_ARCHIVE_ROOT="${WT_ARCHIVE_ROOT:?WT_ARCHIVE_ROOT 환경변수가 필요합니다 (오래된 사본을 보관할 폴더)}"'),
            (re.compile(r'wt-land removed an [A-Za-z][A-Za-z0-9_-]* wiring copy'), "wt-land removed a work copy"),
            (re.compile(r'실측: [A-Za-z][A-Za-z0-9_-]*/[A-Za-z][A-Za-z0-9_-]*'), "실측: one app's copy"),
        ],
        "wt-adopt.sh": [
            (re.compile(r"\(no \.wt\.json\) so the limits, *\n"), "(no .wt.json) so the limits,\n"),
            (re.compile(r'adopted hand-made copies \([^)]+\)'), "adopted hand-made copies"),
        ],
        "scope-check.sh": [],
        "wt-env.py": [
            (re.compile(r'# code-default sqlite paths \([A-Za-z][A-Za-z0-9_-]* class\): the app has no \.env key at all for [A-Z_]+ — only a zod\n'
                        r'# default in source \(`export const DEFAULT_DB_PATH = "' + live_data_root + r'[A-Za-z][A-Za-z0-9_-]*/office\.sqlite"`, then\n'
                        r'# `[A-Z_]+: z\.string\(\)\.min\(1\)\.default\(DEFAULT_DB_PATH\)`\)\. wt-sandbox hiding ' + live_data_root + r'<app> was the only\n'),
             '# code-default sqlite paths (observed on one app class): the app has no .env key at all for its sqlite\n'
             '# path - only a code-level default (e.g. `const DEFAULT_DB_PATH = "<data-root>/<app>/app.sqlite"`, then\n'
             '# a zod schema defaulting to it). wt-sandbox hiding <data-root>/<app> was the only\n'),
            (re.compile(r'\([A-Za-z][A-Za-z0-9_-]* class: no \.env, only'), "(observed on one app class: no .env, only"),
            (re.compile(r'even a relative path or one outside ' + live_data_root.rstrip('/') + r'\), is moved into the copy \(11-01: ' + live_data_root + r'<app> defaults;'),
             "even a relative path or one outside the live data root), is moved into the copy (11-01: <data-root>/<app> defaults;"),
            (re.compile(r'\*_DB_PATH/SQLITE keys protected even when the value is not already under ' + live_data_root + r'\)'),
             "*_DB_PATH/SQLITE keys protected even when the value is not already under the live data root)"),
            (re.compile(r'INTERNAL_URL_KEYS = \("OS_URL", "LEDGER_URL", "LIBRARY_URL", "SEARCH_URL", "SEARCH_PUBLIC_URL"\)'),
             'INTERNAL_URL_KEYS = tuple(k for k in os.environ.get("WT_INTERNAL_URL_KEYS", "").split(",") if k)'),
            (re.compile(r"# Internal-app address keys: always pointed at a dead port in a copy \(code defaults like OS_URL=http://127\.0\.0\.1:\d+ would\n# otherwise reach the live \S+ \S+ / ledger / library\)\. Set even when the env file lacks them; nothing overrides them\."),
             "# Internal-app address keys (configured via WT_INTERNAL_URL_KEYS, comma-separated): always pointed at a dead\n"
             "# port in a copy, so a copy's own code defaults never reach a live internal service by accident.\n"
             "# Set even when the env file lacks them; nothing overrides them."),
            # LIVE_DATA is a generic "anything under the live data root" prefix check, not one
            # fixed subtree (unlike WT_DATA/WT_DEPLOY_STATE above) - an adapter var of its own.
            (re.compile(r'LIVE_DATA = "' + live_data_root + r'"   # the apps\' live data root: a copy gets its own folder under <copy>/\.verify/data/<KEY>'),
             'LIVE_DATA = os.environ.get("WT_LIVE_DATA_ROOT", "")   # the apps\' live data root (adapter, optional): a copy\n'
             "# gets its own folder under <copy>/.verify/data/<KEY> for any value under it; unset disables this relocation."),
            (re.compile(r"an \S+ \S+ or foreman session"), "a calling session"),
            (re.compile(r"\(\S+ \S+, a shell\)"), "(a calling session, a shell)"),
            (re.compile(r" \(11-01: " + live_data_root + r'<app> defaults\)'), ""),
        ],
        "deploy.sh": [
            (re.compile(r'dependency such as @[A-Za-z][A-Za-z0-9_-]*/kit\)'), "a shared internal package)"),
            (re.compile(r'dir="' + apps_root + r'/\$app"'), 'dir="$WT_APPS_ROOT/$app"'),
            (re.compile(r'HEAVY="\$\{DEPLOY_HEAVY:-' + apps_root + r'/[^/\s]+/scripts/heavy\.sh\}".*\n'),
             '# Memory-capped build wrapper (adapter, optional): WT_HEAVY_CMD "--mem <cap> --name <n> -- <cmd...>".\n'
             '# Unset: the build command below runs directly, without a memory cap (warning printed).\n'
             'HEAVY="${WT_HEAVY_CMD:-}"\n'),
            (re.compile(r'untracked \([A-Za-z][A-Za-z0-9_-]* ignores'), "untracked (one app ignores"),
            (re.compile(r"lists them \([A-Za-z][A-Za-z0-9_-]*: next-env\.d\.ts\)"), "lists them (observed once)"),
            (re.compile(r'\([A-Za-z0-9_-]+ 0→152 errors shipped unnoticed\)'),
             "(an app once shipped hundreds of new type errors unnoticed)"),
            (re.compile(r'PREV="' + deploy_state_root + r'/\$app"'), 'PREV="$WT_DEPLOY_STATE/$app"'),
            (re.compile(r'if ! IF_BUILD_DIR=\.next-build NODE_OPTIONS="--max-old-space-size=\$NODE_HEAP" bash "\$HEAVY" --mem "\$BUILD_MEM" --name "\$app" -- npm run -s build; then'),
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
            (re.compile(r' \(G2 A4, \d{4}-\d{2}-\d{2}\)'), ""),
            (re.compile(r'HEALTH_HOST="\d{1,3}(?:\.\d{1,3}){3}:3225"'), 'HEALTH_HOST="<tailnet-host>:3225"'),
            (re.compile(r'APP_DIR=' + apps_root + r'/<app>/web \(Next project in a subfolder\)'),
             "APP_DIR=<path> (Next project in a subfolder)"),
            (re.compile(r' \([a-z0-9_-]+: web/\)'), ""),
            (re.compile(r'\([A-Za-z0-9_-]+ 0→152 errors shipped unnoticed\)'),
             "(an app once shipped hundreds of new type errors unnoticed)"),
            (re.compile(r'\([a-z0-9_-]+ \d{4}-\d{2}-\d{2}, owner "넣어라"\)'), "(observed once)"),
            (re.compile(r'DEPLOY_CMD_DEPLOY/_CHECK/_ROLLBACK \([a-z0-9_-]+\)\.'), "DEPLOY_CMD_DEPLOY/_CHECK/_ROLLBACK."),
        ],
        "review-gate.sh": [
            (re.compile(r'dir="' + apps_root + r'/\$app"'), 'dir="$WT_APPS_ROOT/$app"'),
            (re.compile(r'state="' + deploy_state_root + r'/\$app\.review"'), 'state="$WT_DEPLOY_STATE/$app.review"'),
            (re.compile(r"\([a-z0-9_-]+ first; other apps later\)"), "(enable per app, incrementally)"),
        ],
        "deploy-check.sh": [
            (re.compile(r'envf="' + apps_root + r'/\$app/deploy\.env"'), 'envf="$WT_APPS_ROOT/$app/deploy.env"'),
            (re.compile(r'# Reads ' + apps_root + r'/<app>/deploy\.env:'), "# Reads $WT_APPS_ROOT/<app>/deploy.env:"),
            (re.compile(r'; e\.g\. "200 403" for apps that refuse loopback by design \([a-z0-9_-]+\)'),
             '; e.g. "200 403" for an app that refuses a loopback request by design'),
            (re.compile(r' \([a-z0-9_-]+: \d{1,3}(?:\.\d{1,3}){3}:PORT\)'), " (e.g. a specific tailnet address:PORT)"),
        ],
        "wt-bin/npm": [],
        "wt-scope.sh": [
            (re.compile(r'wt="' + apps_root + r'/_wt/\$app/\$name"'), 'wt="$WT_ROOT/$app/$name"'),
            (re.compile(r' — 2026-11-02, L1-0\d+ wiring audit:\n'), ":\n"),
        ],
        "wt-sandbox.py": [
            (re.compile(r'breaking every git command run inside the sandbox \(gap: [a-z][a-z0-9_-]*\n'
                        r'             scripts/backup\.sh\'s `git bundle`/`git log` died "not a git repository"\)'),
             'breaking every git command run inside the sandbox (observed once: an app\'s own\n'
             '             backup script\'s `git bundle`/`git log` died "not a git repository")'),
            (re.compile(r'e\.g\. [a-z][a-z0-9_-]* web/data/admin-credential\.json; the copy holds all tracked code\)'),
             "e.g. a credential file an app keeps untracked in its own data folder; the copy holds all tracked code)"),
            (re.compile(r'# team-lead 2026-11-02 \(실측: [a-z][a-z0-9_-]* scripts/backup\.sh, git bundle/log inside a copy died "not a git\n'
                        r'    # repository"\) — a `git worktree add` copy'),
             "# team-lead 2026-11-02 (실측: an app's own backup script, git bundle/log inside a copy died \"not a git\n"
             "    # repository\") — a `git worktree add` copy"),
            (re.compile(r'harness_dir = os\.path\.join\(APPS, "[A-Za-z_]+", "harness"\)'),
             'harness_dir = os.environ.get("WT_OPS_HARNESS_DIR", "")'),
            (re.compile(r'    # [A-Za-z_]+/harness \(read-only\): an app\'s own test suite may call its suite_trust\.py \(gap: [a-z][a-z0-9_-]*\n'
                        r'    # server-dev-path\'s "시험 0개" sandbox check got ModuleNotFoundError — [A-Za-z_]+ was hidden with the rest of /srv\n'
                        r'    # and never brought back\)\. Only this one subfolder, never the rest of [A-Za-z_]+ \(plans, app-facts, WORK_QUEUE\.md\)\.\n'),
             '    # The ops/harness folder (read-only, adapter path WT_OPS_HARNESS_DIR): an app\'s own test suite may\n'
             '    # import a test-trust check module from there. Only that one subfolder is ever exposed, never the\n'
             '    # rest of the ops workspace (plans, task queue, etc.) - unset means nothing is exposed.\n'),
        ],
        "receipts.py": [],
        "sec-gate.sh": [
            (re.compile(r'\([a-z][a-z0-9_-]* keeps it in web/\)'), "(an app may keep it in a subfolder such as web/)"),
        ],
        "suite-check.py": [
            (re.compile(r'harness modules in ' + apps_root + r'/[A-Za-z_]+/harness \(HARNESS_DIR overrides\)\.'),
             "harness modules at a configurable path (HARNESS_DIR)."),
            (re.compile(r'HARNESS = os\.environ\.get\("HARNESS_DIR", "' + apps_root + r'/[A-Za-z_]+/harness"\)'),
             'HARNESS = os.environ.get("HARNESS_DIR", "")'),
        ],
        "new-app.sh": [
            (re.compile(r'\(WT_SCAFFOLD_VERSIONS_FROM, default [a-z][a-z0-9_-]*\)'), "(WT_SCAFFOLD_VERSIONS_FROM, required)"),
            (re.compile(r'from="\$\{WT_SCAFFOLD_VERSIONS_FROM:-\$apps/[a-z][a-z0-9_-]*\}/package\.json"'),
             'from="${WT_SCAFFOLD_VERSIONS_FROM:?WT_SCAFFOLD_VERSIONS_FROM 환경변수가 필요합니다 (버전을 베낄 기존 앱의 경로)}/package.json"'),
            (re.compile(r'    "docs/ai/CURRENT_DECISIONS\.md": f"# \{app\} — 결정 기록\\n\\n대표 결정은 원문 그대로, 날짜와 함께 적는다\.\\n",\n'),
             '    "docs/ai/CURRENT_DECISIONS.md": f"# {app} — 결정 기록\\n\\n결정은 원문 그대로, 날짜와 함께 적는다.\\n",\n'),
            (re.compile(r'    "README\.md": f"# \{app\}\\n\\n구조: ' + apps_root + r'/' + ops_dirname + r'/COMMON_ARCHITECTURE_STANDARD\.md\\n",\n'),
             '    "README.md": f"# {app}\\n\\n구조 표준 문서가 있다면 그것을 참고하세요.\\n",\n'),
        ],
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


def transform(relpath: str, text: str, rules: dict, global_rules: list) -> str:
    # per-file rules first (exact lines, including the ones global_rules's generic patterns
    # would otherwise mangle - e.g. wt-lib.sh's own WT_ROOT default definition line), then
    # global_rules as a catch-all for the rest (comments, repeated boilerplate).
    for rx, repl in rules.get(relpath, []):
        text = rx.sub(repl, text)
    for rx, repl in global_rules:
        text = rx.sub(repl, text)
    return text


def app_names(apps_root: str) -> list[str]:
    """Internal app folder names at the apps root, excluding dot/underscore-prefixed
    workspace/tooling folders and names too short or too common to check safely (a generic
    English word used coincidentally in a comment should not be flagged as a leaked app
    name). No specific folder name is hardcoded here - whatever is actually in apps_root at
    run time is what gets checked for."""
    common = {"search", "office", "web", "app", "www", "docs", "data", "test", "tests"}
    out = []
    try:
        for name in os.listdir(apps_root):
            if name.startswith((".", "_")) or name.lower() in common:
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
    cfg = load_src_cfg()
    global_rules = build_global(cfg)
    rules = build_rules(cfg)

    tools_dir = os.path.join(out_dir, "tools")
    os.makedirs(tools_dir, exist_ok=True)
    manifest = {"source_commit": commit, "rules_version": RULES_VERSION, "files": {}}

    for rel_src, rel_dst in ALLOWED_FILES:
        original = git_show(src_shared, commit, rel_src)
        manifest["files"][rel_dst] = hashlib.sha256(original.encode("utf-8")).hexdigest()
        transformed = transform(rel_src, original, rules, global_rules)
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
