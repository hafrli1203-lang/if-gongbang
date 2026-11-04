#!/usr/bin/env bash
# Generated from private source at 495481ceddef11d8ccd4438d8948d5cc71c484d2; do not edit by hand (see harness/export_v3_public.py).
# wt-scope.sh <app> <name> --file <scope-file> | --add <glob>...
# Sets or extends a work copy's .wt.scope (the only sanctioned way besides wt-new.sh --scope-file:
# the protect_sensitive_files hook refuses hand edits of ${WT_ROOT}/**/.wt.*:
# a copy created without --scope-file had no way to get a scope afterwards, so wt-land could never merge it).
# Keeps existing lines, appends new ones, drops duplicates and blank/comment lines. Prints the resulting scope.
set -euo pipefail
app="${1:?앱}"; name="${2:?사본 이름}"; shift 2
wt="$WT_ROOT/$app/$name"
[ -d "$wt" ] && [ -f "$wt/.wt.json" ] || { echo "사본이 없습니다: $wt" >&2; exit 2; }
scope="$wt/.wt.scope"
new=()
while [ $# -gt 0 ]; do
  case "$1" in
    --file) f="${2:?--file 값}"; [ -f "$f" ] || { echo "범위 파일이 없습니다: $f" >&2; exit 2; }
            while IFS= read -r l; do new+=("$l"); done < "$f"; shift 2 ;;
    --add) shift; while [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; do new+=("$1"); shift; done ;;
    *) echo "사용: wt-scope.sh <앱> <이름> --file <파일> | --add <glob>..." >&2; exit 2 ;;
  esac
done
[ ${#new[@]} -gt 0 ] || { echo "추가할 범위가 없습니다" >&2; exit 2; }
tmp="$(mktemp)"
{ [ -f "$scope" ] && cat "$scope"; printf '%s\n' "${new[@]}"; } \
  | sed -e 's/[[:space:]]*$//' | grep -v -e '^$' -e '^#' | awk '!seen[$0]++' > "$tmp"
# never allow a bare "**"/"/" that would mean "everything" — scope must name paths
if grep -qx -e '\*\*' -e '/' -e '\.' "$tmp"; then echo "전체 허용(**·/·.)은 범위가 아닙니다" >&2; rm -f "$tmp"; exit 2; fi
mv "$tmp" "$scope"
echo "범위 $(wc -l < "$scope")줄 — $scope"; cat "$scope"
