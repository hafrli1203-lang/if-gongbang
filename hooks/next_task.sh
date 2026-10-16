#!/usr/bin/env bash
# SessionStart hook: show the shared work queue so the owner never has to say "what's next".
# Prints (1) the first open global item, (2) this project's section, (3) any open alerts.
# Paths come from gongbang.config.json (via the GONGBANG_CONFIG env var, or the repo-local
# default file) - never hardcoded here.
set -euo pipefail

CONFIG_PY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/gongbang_config.py"
read_cfg() { python3 "$CONFIG_PY" | python3 -c "import json,sys; print(json.load(sys.stdin).get('$1',''))"; }

Q="$(read_cfg work_queue_path)"
A="$(read_cfg alerts_path)"
APPS_ROOT="$(read_cfg apps_root)"

# The work-queue block and the alerts block below it are independent: each is skipped on its
# own if its file is missing, but neither one skips the other - an open operational alert
# (service down, etc.) must still show even if the work queue hasn't been set up yet.
if [ -r "$Q" ]; then
  cwd=$(pwd)
  app=""
  case "$cwd" in "$APPS_ROOT"/*) app=${cwd#"$APPS_ROOT"/}; app=${app%%/*};; esac

  echo "## 작업판 (자동, $Q)"
  first=$(awk '/^## 전역 순서/{f=1;next} /^## /{f=0} f && (/^### \[ \]/ || /^- \[ \]/){print; exit}' "$Q")
  [ -n "$first" ] && { echo "다음 전역 TASK:"; echo "$first" | cut -c1-400; }
  if [ -n "$app" ]; then
    sec=$(awk -v a="$app" '/^### /{f=index($0,a)>0} /^## /{f=0} f' "$Q" | head -12)
    if [ -n "$sec" ]; then echo "이 프로젝트($app) 절:"; echo "$sec"; else echo "이 프로젝트($app): 큐에 항목 없음."; fi
  fi
  echo "규칙: 대표 지시가 있으면 그것이 우선. 없으면 위 TASK를 계획 보고 -> 승인 -> 구현 -> 검수 -> 작업판 기록 순서로 진행한다."
else
  echo "## 작업판 없음 ($Q 없음 또는 읽기 불가 — 아래 운영 경보는 별도로 확인한다)"
fi

if [ -s "$A" ] && grep -q "^- \[ \]" "$A"; then
  echo "## 운영 경보 (먼저 보고, 확인 후 [x]): $(grep -c "^- \[ \]" "$A")건"
  grep "^- \[ \]" "$A" | tail -5
fi
exit 0
