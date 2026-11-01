#!/usr/bin/env bash
# Generated from private source at 5b12eebee24334c8a78ac755dc179c5b1c3f0e47; do not edit by hand (see harness/export_v3_public.py).
# Common deploy for one Next.js app.
# Order is forced: busy check -> memory check -> quality gate -> build in a separate folder -> swap only on success
# -> restart -> deploy-check -> automatic rollback if the check fails.
# usage: deploy.sh <app> [--check | --rollback]
#   PRE_SWAP_CMD (env, optional): run after a good build and before the swap/restart; non-zero stops with exit 8 (wt-land migrations).
#   --rollback also restarts every enabled ${WT_UNIT_PREFIX:-ifapp}-<app>-*.service worker unit.
# Reads ${WT_APPS_ROOT}/<app>/deploy.env:
#   UNIT=${WT_UNIT_PREFIX:-ifapp}-<app>  PORT=3270  HEALTH_PATHS="/ /lookup"  WORKER_UNIT=${WT_UNIT_PREFIX:-ifapp}-<app>-worker (optional)
#   HEALTH_HOST="<tailnet-host>:3225" (Host header for apps that trust only their tailscale host)  HEALTH_CODES="200"
#   APP_DIR=<path> (Next project in a subfolder)  ON_DEMAND=1 (do not start an app that was off)  DEPLOY_CMD="bash scripts/deploy.sh" (forward to the app's own deploy)  BUILD_MEM=6G (heavy.sh cap)  NODE_HEAP=4096 (MB)  QUALITY_GATE=1 (run typecheck/test before build)  REVIEW_GATE=1 (live run + Codex review, review-gate.sh)  BUSY_PATTERN="codex|gti|claude|python"
# The app's next.config must honor IF_BUILD_DIR as distDir (one line: distDir: process.env.IF_BUILD_DIR ?? ".next").
set -euo pipefail
app="${1:?app}"; mode="${2:-}"
[[ "$app" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "앱 이름이 이상합니다: $app"; exit 2; }
dir="$WT_APPS_ROOT/$app"; envf="$dir/deploy.env"
[ -f "$envf" ] || { echo "배포 중단 — $envf 없음 (UNIT·PORT·HEALTH_PATHS 필요)"; exit 2; }
# shellcheck disable=SC1090
. "$envf"
dir="${APP_DIR:-$dir}"   # the Next project may live in a subfolder
# Apps with their own release-based deploy (apt: isolated release dirs + health gate + rollback) set DEPLOY_CMD in deploy.env;
# this script then only forwards to it so every app still has one entry point and one settings file.
if [ -n "${DEPLOY_CMD:-}${DEPLOY_CMD_DEPLOY:-}" ]; then
  # PRE_SWAP_CMD (migrations from wt-land) needs the build→swap split of this script; an app's own deploy has no such point.
  if [ -n "${PRE_SWAP_CMD:-}" ] && [ "$mode" = "" ]; then echo "배포 중단 — 이 앱은 자체 배포(DEPLOY_CMD)라 PRE_SWAP_CMD(마이그레이션)를 끼울 자리가 없습니다."; exit 8; fi
  # Either DEPLOY_CMD + subcommand (apt: deploy|rollback|status) or per-mode commands DEPLOY_CMD_DEPLOY/_CHECK/_ROLLBACK.
  case "$mode" in --rollback) sub=rollback; cmd="${DEPLOY_CMD_ROLLBACK:-}" ;; --check) sub=status; cmd="${DEPLOY_CMD_CHECK:-}" ;; "") sub=deploy; cmd="${DEPLOY_CMD_DEPLOY:-}" ;; *) echo "모르는 모드: $mode"; exit 2 ;; esac
  [ -n "$cmd" ] || cmd="${DEPLOY_CMD:-} $sub"
  # The app's own command is relative to the app folder (scripts/deploy.sh) — run it from there, not from the caller's folder
  # (2026-11-01: called from another folder it failed with "scripts/deploy.sh: No such file").
  echo "앱 자체 배포로 넘김: $cmd"; cd "${WT_APPS_ROOT}/$app" && exec bash -c "$cmd"
fi
UNIT="${UNIT:-${WT_UNIT_PREFIX:-ifapp}-$app}"; BUILD_MEM="${BUILD_MEM:-6G}"; NODE_HEAP="${NODE_HEAP:-4096}"; QUALITY_GATE="${QUALITY_GATE:-1}"; BUSY_PATTERN="${BUSY_PATTERN:-codex|gti|claude|python}"
# Memory-capped build wrapper (adapter, optional): WT_HEAVY_CMD "--mem <cap> --name <n> -- <cmd...>".
# Unset: the build command below runs directly, without a memory cap (warning printed).
HEAVY="${WT_HEAVY_CMD:-}"
SHARED="$(cd "$(dirname "$0")" && pwd)"
PREV="$WT_DEPLOY_STATE/$app"   # previous build kept outside the project (10-14: build tools scan the whole tree)
BUILD="$dir/.next-build"
cd "$dir"
mkdir -p "$(dirname "$PREV")"
exec 9>"$PREV.lock"; flock -n 9 || { echo "배포 중단 — 같은 앱의 배포가 이미 진행 중입니다($PREV.lock)."; exit 6; }   # Codex P1: no concurrent deploys

swap_back() {  # rollback: put the previous build back and restart
  if [ -d "$PREV/.next" ]; then
    [ -d .next ] && { [ -d "$PREV/.next-failed" ] && find "$PREV/.next-failed" -depth -delete; mv .next "$PREV/.next-failed"; }
    mv "$PREV/.next" .next
    local ok=0
    systemctl --user restart "$UNIT" || ok=1
    [ -n "${WORKER_UNIT:-}" ] && { systemctl --user restart "$WORKER_UNIT" || ok=1; }   # worker follows the web (same checkout)
    sleep 5
    [ "$(systemctl --user is-active "$UNIT" 2>/dev/null)" = active ] || ok=1
    if [ "$ok" = 0 ]; then echo "되돌림 — 이전 빌드로 재시작했습니다($PREV/.next). 소스 되돌림은 git에서."
    else echo "되돌림 실패 — 이전 빌드로도 $UNIT 이(가) 뜨지 않습니다. journalctl --user -u $UNIT 확인." >&2; return 1; fi
  else
    echo "되돌릴 이전 빌드가 없습니다($PREV/.next)." >&2; return 1
  fi
}

# 1) busy: never restart while the web or worker unit has generation children (09-24 incident; cgroup-wide, pgrep -P fallback)
busy_procs() {  # prints matching processes of one unit, empty when idle
  local unit="$1" cgroup main_pid out="" pid comm args
  cgroup=$(systemctl --user show "$unit" -p ControlGroup --value 2>/dev/null || true)
  main_pid=$(systemctl --user show "$unit" -p MainPID --value 2>/dev/null || echo 0)
  if [ -n "$cgroup" ] && [ -r "/sys/fs/cgroup$cgroup/cgroup.procs" ]; then
    while read -r pid; do
      { [ -z "$pid" ] || [ "$pid" = "$main_pid" ]; } && continue
      comm=$(cat "/proc/$pid/comm" 2>/dev/null || true); args=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | cut -c1-120 || true)
      if echo "$comm $args" | grep -qE "$BUSY_PATTERN"; then out="${out}${unit} ${pid} ${comm} ${args}"$'\n'; fi
    done < "/sys/fs/cgroup$cgroup/cgroup.procs"
  elif [ "${main_pid:-0}" != 0 ]; then
    out=$(pgrep -P "$main_pid" -a 2>/dev/null | grep -E "$BUSY_PATTERN" || true)
  fi
  printf '%s' "$out"
}
busy_all() { busy_procs "$UNIT"; [ -n "${WORKER_UNIT:-}" ] && busy_procs "$WORKER_UNIT"; return 0; }
busy=$(busy_all)
if [ -n "$busy" ]; then echo "배포 중단 — 서비스가 생성 작업 중입니다:"; printf '%s\n' "$busy" | cut -c1-120; exit 2; fi
if [ "$mode" = --rollback ]; then   # manual rollback only when idle (Codex P1)
  swap_back || exit 1
  # Every worker of this app runs the same checkout (wt-land failure path, 2026-11-01): restart all ${WT_UNIT_PREFIX:-ifapp}-<app>-*.service
  # units, not only WORKER_UNIT, so none keeps running code from the failed build. Listed by unit name, never by process.
  rb=0
  for u in $(systemctl --user list-units --type=service --all --no-legend --plain "${WT_UNIT_PREFIX:-ifapp}-$app-*.service" 2>/dev/null | awk '{print $1}'); do
    { [ "$u" = "$UNIT.service" ] || [ "$u" = "${WORKER_UNIT:-none}.service" ] || [ "$u" = "${WORKER_UNIT:-none}" ]; } && continue
    [ "$(systemctl --user is-enabled "$u" 2>/dev/null || true)" = disabled ] && continue
    systemctl --user restart "$u" && echo "워커 재시작 — $u" || { echo "워커 재시작 실패 — $u" >&2; rb=1; }
  done
  exit "$rb"
fi
if [ "$mode" = --check ]; then echo "생성 작업 없음 — 배포해도 됩니다."; exit 0; fi

# 2) memory: a build that dies half-way must never touch the running .next, but still refuse to start starved (09-24 incident 2)
avail_mb=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
[ "${avail_mb:-0}" -ge 4096 ] || { echo "배포 중단 — 여유 메모리 ${avail_mb}MB (4096MB 미만)."; exit 3; }

# 3) quality gate: typecheck / test when the app defines them (an app once shipped hundreds of new type errors unnoticed)
if [ "$QUALITY_GATE" = 1 ]; then
# The running build's generated route-type file lists routes that may no longer exist; the type check would fail on it
# after a route is deleted (observed once). Must run before the quality gate (typecheck).
rm -f "$dir/.next/types/validator.ts"
  for s in ${GATE_SCRIPTS:-typecheck test}; do   # GATE_SCRIPTS="typecheck" for apps without tests (vitest exits 1 when it finds none)
    if node -e "process.exit(require('./package.json').scripts?.['$s'] ? 0 : 1)"; then
      echo "검사 $s …"; npm run -s "$s" >/tmp/deploy-$app-$s.log 2>&1 || { tail -20 "/tmp/deploy-$app-$s.log"; echo "배포 중단 — $s 실패. 도는 앱은 그대로입니다."; exit 4; }
    fi
  done
fi

# 3b) review gate (opt-in REVIEW_GATE=1): a live run on real data for this change + Codex read-only review (2026-11-01)
if [ "${REVIEW_GATE:-0}" = 1 ]; then bash "$SHARED/review-gate.sh" "$app" --check || exit 4; fi

# 4) build in a separate folder under the heavy-job runner; cache carried over and returned on failure
[ -d "$BUILD" ] && find "$BUILD" -depth -delete
mkdir -p "$BUILD"
[ -d .next/cache ] && mv .next/cache "$BUILD/cache"
build_failed=0
if [ -n "$HEAVY" ]; then
  IF_BUILD_DIR=.next-build NODE_OPTIONS="--max-old-space-size=$NODE_HEAP" bash $HEAVY --mem "$BUILD_MEM" --name "$app" -- npm run -s build || build_failed=1
else
  echo "경고 — WT_HEAVY_CMD 없음: 메모리 상한 없이 바로 빌드합니다." >&2
  IF_BUILD_DIR=.next-build NODE_OPTIONS="--max-old-space-size=$NODE_HEAP" npm run -s build || build_failed=1
fi
if [ "$build_failed" = 1 ]; then
  [ -d "$BUILD/cache" ] && [ ! -d .next/cache ] && mv "$BUILD/cache" .next/cache
  echo "배포 중단 — 빌드 실패. 도는 앱(.next)은 그대로입니다."; exit 1
fi

# 5) swap: re-check busy (work may have started during the build), keep the previous build outside the project, restart once
busy=$(busy_all)
if [ -n "$busy" ]; then echo "배포 중단 — 빌드는 끝났지만 그 사이 생성 작업이 시작됐습니다. 끝난 뒤 다시 실행하세요(빌드는 다시 합니다):"; printf '%s\n' "$busy" | cut -c1-120; exit 2; fi
# 4b) pre-swap hook (wt-land: pg_dump backup then migrate) — after a good build, before the running build is touched.
# A failure stops here with the old build still serving (exit 8). Written as an if: `[ -n ] && cmd || exit 8` would exit 8 on every
# deploy without a hook.
if [ -n "${PRE_SWAP_CMD:-}" ]; then
  echo "배포 전 단계(PRE_SWAP_CMD) 실행 …"
  if ! bash -c "$PRE_SWAP_CMD"; then
    [ -d "$BUILD/cache" ] && [ -d .next ] && [ ! -d .next/cache ] && mv "$BUILD/cache" .next/cache
    echo "배포 중단 — PRE_SWAP_CMD 실패. 도는 앱(.next)은 그대로입니다."; exit 8
  fi
fi
mkdir -p "$PREV"
[ -d "$PREV/.next" ] && find "$PREV/.next" -depth -delete
[ -d .next ] && mv .next "$PREV/.next"
mv "$BUILD" .next
git checkout -- next-env.d.ts tsconfig.json 2>/dev/null || true   # next writes the build dir into both; restore the repo versions (10-15, 10-16)
was_active=$(systemctl --user is-active "$UNIT" 2>/dev/null || true)
if [ "${ON_DEMAND:-0}" = 1 ] && [ "$was_active" != active ]; then
  if [ "${REVIEW_GATE:-0}" = 1 ]; then bash "$SHARED/review-gate.sh" "$app" --deployed; fi
  echo "배포 완료 — $app (쓸 때만 켜는 앱이라 켜지 않음; 다음 시작 때 새 빌드 사용)"; exit 0
fi
if ! systemctl --user restart "$UNIT"; then echo "재시작 실패 — 되돌립니다."; swap_back; exit 5; fi   # Codex P1: set -e must not skip the rollback
if [ -n "${WORKER_UNIT:-}" ]; then systemctl --user restart "$WORKER_UNIT" || echo "경고 — 워커 재시작 실패($WORKER_UNIT); 웹은 새 빌드로 올라감"; fi
for _ in $(seq 1 30); do
  pid=$(systemctl --user show "$UNIT" -p MainPID --value)
  started=$( [ "$pid" != 0 ] && stat -c %Y "/proc/$pid" 2>/dev/null || echo 0 )   # pid may vanish in a crash loop — never let the arithmetic fail
  if [ "$started" != 0 ] && [ $(( $(date +%s) - started )) -ge 45 ]; then break; fi
  sleep 3
done

# 6) check, roll back on failure
if bash "$SHARED/deploy-check.sh" "$app"; then
  if [ "${REVIEW_GATE:-0}" = 1 ]; then
    # 7) screen QA (Playwright + Chromium); a failing screen goes back to the previous build (ship.sh then fixes and redeploys)
    if ! bash "$SHARED/review-gate.sh" "$app" --qa; then echo "화면 검수 실패 — 되돌립니다."; swap_back; exit 7; fi
    bash "$SHARED/review-gate.sh" "$app" --deployed
  fi
  echo "배포 완료 — $app"
else echo "배포 확인 실패 — 되돌립니다."; swap_back; exit 5; fi
