#!/usr/bin/env bash
# Generated from private source at d72495d955c050e98c98a283b5521f31f3eff43a; do not edit by hand (see harness/export_v3_public.py).
# wt-gc.sh [--dry] [--only <copy>]
# Removes only work copies whose job is finished for good — docs/v3-wt-interface.md §6. Run at session start and on a nightly schedule.
# A copy is removed when
#   (a) <copy>/.wt.done exists (landed, or discarded by the caller), or
#   (b) no live unit, folder untouched for 48h, and its WT_TASK_STATUS_CMD task ended as failed/cancelled,
#       and the copy has no uncommitted change.
# Everything else is kept: copies being resumed, done-but-not-landed tasks, copies without .wt.json (made by hand).
# Removal: stop its units -> git worktree remove --force -> release the test DB slot -> delete branch wt/<name> only for (a).
# --dry prints the decision for every copy and changes nothing.
# Run from a private copy (11-01 23:14: this file was edited while a live wt-land ran; bash reads a script as it goes and
# died on the half-new text after the deploy, before push). The copy under $WT_DATA/.run is what executes.
if [ -z "${WT_SELF:-}" ]; then
  _run="${WT_DATA:?WT_DATA 환경변수가 필요합니다}/.run"; _orig="$(readlink -f "$0")"
  mkdir -p "$_run" || { echo "실행 복사본 폴더를 만들 수 없습니다: $_run" >&2; exit 1; }
  find "$_run" -name '*.sh' -mmin +1440 -delete 2>/dev/null || true
  _copy="$_run/$(basename "$0" .sh)-$$.sh"
  # a failed or short copy would run an empty script and exit 0 (Codex 11-01): stop instead
  { cp "$_orig" "$_copy" && cmp -s "$_orig" "$_copy"; } || { echo "실행 복사본을 만들 수 없습니다: $_copy" >&2; exit 1; }
  WT_SELF="$_orig" exec bash "$_copy" "$@"
fi
_wt_self="$WT_SELF"; unset WT_SELF   # children (scope-check, wt-stop, ...) make their own copies
set -euo pipefail
# shellcheck source=wt-lib.sh
. "$(dirname "$_wt_self")/wt-lib.sh"

dry=0; only=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry) dry=1; shift ;;
    --only) only=$(readlink -f "${2:?--only <사본>}"); shift 2 ;;   # judge (and remove) just this one copy
    *) echo "모르는 인자: $1" >&2; exit 2 ;;
  esac
done
STALE_SEC=$((48 * 3600))
removed=0; kept=0

task_status() {  # $1 task id -> status word from the adapter command, '' when unknown or unset
  [ -n "$1" ] && [ -n "${WT_TASK_STATUS_CMD:-}" ] || return 0
  $WT_TASK_STATUS_CMD "$1" 2>/dev/null || true
}

newest_mtime() {  # newest mtime under the copy, ignoring node_modules (a symlink) and .git
  find "$1" -path "$1/node_modules" -prune -o -name .git -prune -o -printf '%T@\n' 2>/dev/null | sort -n | tail -1 | cut -d. -f1
}

for wt in "$WT_ROOT"/*/*; do
  [ -d "$wt" ] || continue
  [ -z "$only" ] || [ "$(readlink -f "$wt")" = "$only" ] || continue
  if [ ! -f "$wt/.wt.json" ]; then echo "보존  $wt — .wt.json 없음(손으로 만든 사본)"; kept=$((kept + 1)); continue; fi
  app=$(wt_json_get "$wt/.wt.json" app); name=$(wt_json_get "$wt/.wt.json" name); task=$(wt_json_get "$wt/.wt.json" task_id)
  reason=""; done_mark=0
  if [ -e "$wt/.wt.done" ]; then
    reason="종료 표식(.wt.done)"; done_mark=1
  else
    live=0
    for u in $(wt_json_get "$wt/.wt.json" units); do wt_unit_alive "$u" && live=1; done
    m=$(newest_mtime "$wt"); age=$(( $(date +%s) - ${m:-0} ))
    st=$(task_status "$task")
    dirty=$(git -C "$wt" status --porcelain 2>/dev/null | grep -vE '^\?\? (node_modules|\.wt\.)' || true)
    if [ "$live" = 1 ]; then echo "보존  $wt — 도는 유닛 있음"; kept=$((kept + 1)); continue; fi
    if [ "$age" -lt "$STALE_SEC" ]; then echo "보존  $wt — 최근 $((age / 3600))시간 안에 바뀜"; kept=$((kept + 1)); continue; fi
    case "$st" in failed|cancelled|canceled|discarded) ;; *) echo "보존  $wt — 작업 상태 '${st:-모름}'(실패 확정 아님)"; kept=$((kept + 1)); continue ;; esac
    if [ -n "$dirty" ]; then echo "보존  $wt — 미커밋 변경 있음"; kept=$((kept + 1)); continue; fi
    reason="48시간 지난 실패 작업($st)"
  fi
  if [ "$dry" = 1 ]; then echo "삭제 예정  $wt — $reason"; removed=$((removed + 1)); continue; fi
  bash "$WT_SHARED/wt-stop.sh" "$wt" >/dev/null || { echo "보존  $wt — 유닛 정지 실패"; kept=$((kept + 1)); continue; }
  # delete only the branch this copy recorded, and only when it is wt/<name> — read before the copy is removed
  br=$(wt_json_get "$wt/.wt.json" branch)
  git -C "${WT_APPS_ROOT}/$app" worktree remove --force "$wt"
  wt_slot_release "$app" "$wt"
  [ "$done_mark" = 1 ] && [ "$br" = "wt/$name" ] && git -C "${WT_APPS_ROOT}/$app" branch -D "$br" >/dev/null 2>&1 || true
  echo "삭제  $wt — $reason"; removed=$((removed + 1))
done
[ "$dry" = 1 ] && echo "(--dry) 삭제 예정 ${removed}개, 보존 ${kept}개" || echo "삭제 ${removed}개, 보존 ${kept}개"
