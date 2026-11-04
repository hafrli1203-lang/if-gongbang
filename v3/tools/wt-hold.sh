#!/usr/bin/env bash
# Generated from private source at 495481ceddef11d8ccd4438d8948d5cc71c484d2; do not edit by hand (see harness/export_v3_public.py).
# wt-hold.sh <app> <name> "<reason>"   |   wt-hold.sh <app> <name> --release
# Marks a work copy as WAITING ON A PERSON (owner answer, QR scan, decision) by writing <copy>/.wt.hold with the reason.
# stall_watch.py then shows it as "대기" and never pages about it; wt-gc keeps it (uncommitted/ahead work is untouched).
# --release removes the mark. Owner 2026-11-05: the stall watcher paged 20+ times about copies that were only waiting.
set -euo pipefail
# shellcheck source=wt-lib.sh
. "$(cd "$(dirname "$0")" && pwd)/wt-lib.sh"
app="${1:?app}"; name="${2:?name}"; reason="${3:?reason or --release}"
wt="$WT_ROOT/$app/$name"
[ -f "$wt/.wt.json" ] || wt_die "사본이 아닙니다: $wt"
if [ "$reason" = --release ]; then
  rm -f -- "$wt/.wt.hold"; echo "대기 해제: $app/$name"
else
  printf '%s\n%s\n' "$reason" "since=$(date -Is)" > "$wt/.wt.hold"; echo "대기 표시: $app/$name — $reason"
fi
