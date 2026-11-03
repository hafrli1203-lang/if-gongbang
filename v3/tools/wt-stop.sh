#!/usr/bin/env bash
# Generated from private source at a8eb56b1bac538511499bb39b350117e5eead6e1; do not edit by hand (see harness/export_v3_public.py).
# wt-stop.sh <copy>
# Stops only the scope units recorded in <copy>/.wt.json "units" (systemctl --user stop), then empties the list.
# Never kills by process name or pattern — contract: docs/v3-wt-interface.md §3.
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

wt=$(readlink -f "${1:-}" 2>/dev/null || true)
[ -n "$wt" ] && [ -f "$wt/.wt.json" ] || WT_EXIT=2 wt_die "사용: wt-stop.sh <사본> (.wt.json 없음)"
units=$(wt_json_get "$wt/.wt.json" units)
stopped=0; failed=0
for u in $units; do
  [[ "$u" =~ ^wt-[A-Za-z0-9._-]+\.scope$ ]] || { echo "이상한 유닛 이름 건너뜀: $u" >&2; continue; }
  if wt_unit_alive "$u" && ! systemctl --user stop "$u"; then failed=$((failed + 1)); echo "정지 실패: $u" >&2; continue; fi
  wt_unit_alive "$u" || stopped=$((stopped + 1))
  # remove just this unit: a wt-run --bg that registered meanwhile keeps its entry (Codex 11-01)
  exec 6>"$WT_DATA/global.mutex"; flock 6
  wt_json_set "$wt/.wt.json" units="-$u"
  flock -u 6; exec 6>&-
done
echo "정지 ${stopped}개, 실패 ${failed}개 — $wt"
[ "$failed" = 0 ]
