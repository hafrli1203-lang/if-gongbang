#!/usr/bin/env bash
# Generated from the private ops workspace at b94af62b97af64e2e58e8c102fa2538f066ae366; do not edit by hand (see harness/export_ops_public.py).
# Installs this bundle's hooks into ~/.claude/hooks and registers them in
# ~/.claude/settings.json's hooks section: gongbang_config.py (config loader) + _shell.py (shared
# tokenizer) + block_dangerous.py + protect_sensitive_files.py (v2 gap bundles 1/2) +
# mcp_scope_limit.py (item 144) + scope_fence.py (v2 gap bundle 4, hooks-B's file - registered
# here per team-lead instruction so one apply script covers both bundles' hooks in one pass; its
# CONTENT is hooks-B's, this script only deploys/registers it) + lessons-start.py (SessionStart
# hook, already registered in settings.json by install-l1.sh - this bundle only re-deploys its
# FILE CONTENT so the 2026-11-02 exclude-list change below actually reaches the running copy).
#
# lessons-start.py / "no-cap" decision superseded (owner decision 2026-11-02 01:10, team-lead
# recommendation, no objection): the 2026-10-15 "no cap, just measure it" decision produced a
# 74-entry/~70KB workspace lessons injection, most of it either already enforced by a real device
# (graduated) or a near-duplicate of another entry. Replaced with an EXCLUDE-LIST approach instead
# of a hard cap: `_ops/lessons_exclude.json` names the specific L1 numbers to skip (with a
# reason - graduated/device, or merged-into another number) and lessons-start.py's
# filter_excluded() drops exactly those, only for app=="workspace", only ever skipping injection -
# never deleting the lesson from the DB (MASTER_RULES §1 "지우지 않는다"). Full reasoning per
# number: `_ops/plans/2026-11-02-lessons-triage.md`.
#
# Owner decision 2026-11-02 00:35/00:50: the approval gate (approve_gate.py, and scope_guard.py's
# general reversible-action "승인 <TASK>" branch) has been REMOVED entirely - every hook in this
# bundle is a plain binary hard-block/pass, never an "approve to proceed" branch. Behavioral
# hooks that only nag or force a report format (read_whole/directive_check/orbit_nudge/
# bansang_habit) are built but NOT installed - archived under `hooks-src/_보관/` instead (see
# that folder's README for why).
#
# Safety:
#   - Backs up the CURRENT ~/.claude/hooks directory and ~/.claude/settings.json to
#     /backups/hooks-<timestamp>/ before touching anything (MASTER_RULES §1: "삭제·이동은
#     백업 뒤 진행하고 보고한다").
#   - Validates every deployed .py file with `python3 -m py_compile` and the settings.json merge
#     result with `python3 -m json.tool` before leaving them in place; on any validation failure
#     it restores the backup and exits non-zero (never leaves a broken install).
#   - Idempotent: running it twice makes no further changes the second time (merge_settings_hooks.py
#     checks for the exact command string already being registered).
#   - `--dry`: prints the unified diff of the settings.json "hooks" section that WOULD be written,
#     and copies NOTHING - no backup, no file writes, no settings.json change at all.
#
# Usage: bash apply_hooks_v3.sh [--dry]
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOKS_DIR="${CLAUDE_HOOKS_DIR:-$HOME/.claude/hooks}"
SETTINGS_PATH="${CLAUDE_SETTINGS_PATH:-$HOME/.claude/settings.json}"
DRY=0
if [[ "${1:-}" == "--dry" ]]; then
  DRY=1
fi

DEPLOY_FILES=(
  gongbang_config.py
  _shell.py
  block_dangerous.py
  protect_sensitive_files.py
  lesson-capture.py
  next-task.sh
  mcp_scope_limit.py
  scope_fence.py
  lessons-start.py
)

echo "== IF Gongbang v3 hook bundle 1/2/4+항목144 설치 $( [[ $DRY -eq 1 ]] && echo '(--dry, 쓰기 없음)' ) =="

if [[ $DRY -eq 1 ]]; then
  echo "-- 등록될 settings.json hooks 절 diff (실제 적용 안 함) --"
  python3 "$SRC_DIR/merge_settings_hooks.py" "$SETTINGS_PATH" "$HOOKS_DIR" --dry
  exit 0
fi

if [[ ! -f "$SETTINGS_PATH" ]]; then
  echo "BLOCKED: $SETTINGS_PATH 가 없다. 설치 중단." >&2
  exit 1
fi

TS="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="/backups/hooks-${TS}"
mkdir -p "$BACKUP_DIR"
cp -a "$HOOKS_DIR" "$BACKUP_DIR/hooks"
cp -a "$SETTINGS_PATH" "$BACKUP_DIR/settings.json"
echo "백업: $BACKUP_DIR"

restore_backup() {
  echo "검증 실패 — 백업에서 되돌린다: $BACKUP_DIR" >&2
  rm -rf "$HOOKS_DIR"
  cp -a "$BACKUP_DIR/hooks" "$HOOKS_DIR"
  cp -a "$BACKUP_DIR/settings.json" "$SETTINGS_PATH"
}
trap 'restore_backup' ERR

mkdir -p "$HOOKS_DIR"
for f in "${DEPLOY_FILES[@]}"; do
  cp "$SRC_DIR/$f" "$HOOKS_DIR/$f"
done

echo "-- 미등록·은퇴 훅 파일 정리 (2026-11-02 배선 조사, RETIRED_BASENAMES 전부) --"
RETIRED_COUNT=0
python3 - "$HOOKS_DIR" "$SRC_DIR" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[2])
from merge_settings_hooks import RETIRED_BASENAMES
hooks_dir, names = sys.argv[1], RETIRED_BASENAMES
import os
removed = []
for name in names:
    p = os.path.join(hooks_dir, name)
    if os.path.isfile(p):
        os.remove(p)
        removed.append(name)
print(f"{len(removed)}개 제거: {', '.join(removed) if removed else '(이미 없음)'}")
PYEOF

echo "-- 문법 검증 (bash -n / py_compile) --"
bash -n "$SRC_DIR/apply_hooks_v3.sh"
for f in "${DEPLOY_FILES[@]}"; do
  case "$f" in *.py) python3 -m py_compile "$HOOKS_DIR/$f" ;; *.sh) bash -n "$HOOKS_DIR/$f" ;; esac
done
echo "OK: ${#DEPLOY_FILES[@]}개 파일 문법 검증 통과"

echo "-- settings.json hooks 절 병합 --"
python3 "$SRC_DIR/merge_settings_hooks.py" "$SETTINGS_PATH" "$HOOKS_DIR"
python3 -m json.tool "$SETTINGS_PATH" > /dev/null
echo "OK: settings.json 유효한 JSON"

trap - ERR
echo "== 설치 완료. 되돌리려면: cp -a $BACKUP_DIR/hooks $HOOKS_DIR && cp -a $BACKUP_DIR/settings.json $SETTINGS_PATH =="
