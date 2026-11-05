#!/usr/bin/env bash
# Generated from private source at 494f80a1335ecba422757332f15d6b4100b0afaf; do not edit by hand (see harness/export_v3_public.py).
# wt-adopt.sh <copy path>
# Registers a work copy made by hand (git worktree under ${WT_ROOT}/<app>/<name> without .wt.json) so the limits,
# wt-stop, wt-land and wt-gc see it. Writes .wt.json {app, name, branch, base, path, db: null, port: null, units: [],
# adopted_at}. No test DB slot and no port are given: wt-run then runs without DATABASE_URL slot/PORT (--dev refuses),
# and wt-land/wt-gc skip the slot release. An adopted copy counts toward the limits like any other until .wt.done exists.
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
[ -n "$wt" ] && [ -d "$wt" ] || WT_EXIT=2 wt_die "사용: wt-adopt.sh <사본 경로>"
case "$wt" in "$WT_ROOT"/*/*) ;; *) WT_EXIT=2 wt_die "사본은 $WT_ROOT/<앱>/<이름> 아래여야 합니다: $wt" ;; esac
rel="${wt#"$WT_ROOT"/}"; app="${rel%%/*}"; name="${rel#*/}"
[[ "$name" != */* ]] || WT_EXIT=2 wt_die "사본 경로가 깊습니다: $wt"
WT_EXIT=2 wt_check_name app "$app"; WT_EXIT=2 wt_check_name existing "$name"
[ -f "$wt/.wt.json" ] && { echo "이미 등록된 사본입니다: $wt"; exit 0; }
# must be a worktree of the app's own repository
common=$(wt_git -C "$wt" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
[ "$common" = "$(wt_git -C "$WT_APPS/$app" rev-parse --path-format=absolute --git-common-dir)" ] \
  || WT_EXIT=2 wt_die "$app 저장소의 작업 사본이 아닙니다: $wt"
branch=$(wt_git -C "$wt" rev-parse --abbrev-ref HEAD)
[ "$branch" != HEAD ] || WT_EXIT=2 wt_die "가지가 없는(detached) 사본은 등록하지 않습니다: $wt"
# wt-land and wt-gc work on branch wt/<name>: a copy on any other branch could merge or delete the wrong branch (Codex 11-01)
[ "$branch" = "wt/$name" ] || WT_EXIT=2 wt_die "사본 가지가 wt/$name 이 아니라 $branch 입니다 — 등록하지 않습니다: $wt"
base=$(wt_base_branch "$app")
[ "$branch" != "$base" ] || WT_EXIT=2 wt_die "사본이 기본 가지($base) 자체에 있습니다: $wt"

mkdir -p "$WT_DATA"; exec 6>"$WT_DATA/global.mutex"
flock 6
wt_json_set "$wt/.wt.json" app="$app" name="$name" branch="$branch" base="$base" path="$wt" db=null port=null units="[]" \
  adopted_at="$(date -Iseconds)"
flock -u 6; exec 6>&-
python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])), ensure_ascii=False))' "$wt/.wt.json"
