#!/usr/bin/env bash
# Generated from private source at d72495d955c050e98c98a283b5521f31f3eff43a; do not edit by hand (see harness/export_v3_public.py).
# wt-run.sh <copy> [--bg] [--mem 4G] [--dev | -- <command...>]
# --dev: the app's dev server — deploy.env WT_DEV_CMD ({PORT} replaced), else package.json "dev" (next dev) on the copy's port
# Runs one command for a work copy inside a memory-capped systemd scope — contract: docs/v3-wt-interface.md §2.
#  - cwd = the copy; environment built by wt-env.py from the app's env-file KEY NAMES only:
#    DATABASE_URL / TEST_DATABASE_URL / other *DATABASE*URL* keys -> the copy's slot test DB (never the production DB),
#    other keys -> <copy>/.claude/verify-env.json (else ${WT_APPS_ROOT}/<app>/.claude/verify-env.json) values — every key in that
#    file is set, even ones the env file lacks — else fake "x-<KEY>"; internal-app address keys -> http://127.0.0.1:1 and
#    *_TOKEN_FILE/*_TOKEN_PATH -> a missing path (wt-env.py), so a copy never reports to a live internal service.
#    Plus PORT, VERIFY_ALLOWED_HOST=127.0.0.1:<port>, IF_BUILD_DIR=.next-wt, WT_SANDBOX=1(code that must not use
#    a production default outside this runner checks this — e.g. kit/ledger.ts, 2026-11-01); NODE_ENV is left unset.
#  - PATH starts with _shared/wt-bin (npm/pnpm/yarn install-type commands refused) and the copy's node_modules/.bin
#  - scope unit wt-<app>-<name>-<6 random>; --bg detaches (output -> ${WT_DATA}/<app>/<name>/<unit>.log) and records
#    the unit in .wt.json units; foreground returns the command's exit code
# A literal "$PORT" in the command is expanded by the shell before it gets here, so pass it quoted or use
# `bash -c 'npx next dev -p $PORT -H 127.0.0.1'`; a bare {PORT} token is replaced with the copy's port.
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

wt="${1:-}"; shift || true
bg=0; mem="4G"; dev=0
while [ $# -gt 0 ]; do
  case "$1" in
    --bg) bg=1; shift ;;
    --dev) dev=1; shift ;;
    --mem) mem="${2:?--mem 값}"; shift 2 ;;
    --) shift; break ;;
    *) WT_EXIT=2 wt_die "모르는 인자: $1 (명령 앞에 -- )" ;;
  esac
done
[ $# -gt 0 ] || [ "$dev" = 1 ] || WT_EXIT=2 wt_die "사용: wt-run.sh <사본> [--bg] [--mem 4G] [--dev | -- <명령...>]"
wt=$(readlink -f "$wt" 2>/dev/null || true)
[ -n "$wt" ] && [ -f "$wt/.wt.json" ] || WT_EXIT=2 wt_die "사본이 아닙니다(.wt.json 없음): ${1:-}"
[ -e "$wt/.wt.done" ] && WT_EXIT=2 wt_die "끝난 사본입니다(.wt.done): $wt"
app=$(wt_json_get "$wt/.wt.json" app); name=$(wt_json_get "$wt/.wt.json" name)
port=$(wt_json_get "$wt/.wt.json" port); db=$(wt_json_get "$wt/.wt.json" db)
src="$WT_APPS_ROOT/$app"

if [ "$dev" = 1 ]; then
  # dev server command: deploy.env WT_DEV_CMD, else package.json "dev" when it is next dev (its fixed -p/-H dropped)
  devcmd=""
  [ -f "$src/deploy.env" ] && devcmd=$(bash -c '. "$1" >/dev/null 2>&1; printf "%s" "${WT_DEV_CMD:-}"' _ "$src/deploy.env")
  if [ -z "$devcmd" ]; then
    devcmd=$(node -e '
      const d = ((require(process.argv[1]).scripts) || {}).dev || "";
      if (/^next dev\b/.test(d)) process.stdout.write(`npx ${d.replace(/\s(-p|--port|-H|--hostname)\s+\S+/g, "")} -p {PORT} -H 127.0.0.1`);
    ' "$wt/package.json" 2>/dev/null || true)
  fi
  [ -n "$devcmd" ] || WT_EXIT=2 wt_die "dev 명령을 정할 수 없습니다 — deploy.env에 WT_DEV_CMD를 적으세요."
  [ -n "$port" ] || WT_EXIT=2 wt_die "이 사본에는 포트가 없습니다(wt-adopt로 등록한 사본) — dev 서버는 wt-new로 만든 사본에서."
  set -- bash -c "${devcmd//\{PORT\}/$port}"
fi
args=()
for a in "$@"; do args+=("${a//\{PORT\}/$port}"); done
unit="wt-$app-$name-$(tr -dc 'a-z0-9' < /dev/urandom | head -c 6 || true)"
uscope="$unit.scope"   # systemctl needs the suffix: a bare name means .service

envargs=(app --app-dir "$src" --verify-env "$(wt_verify_env "$wt" "$app")" --set "IF_BUILD_DIR=.next-wt"
  --set "PATH=$WT_SHARED/wt-bin:$wt/node_modules/.bin:$PATH" --set "WT_COPY=$wt" --set "WT_UNIT=$unit"
  --set "WT_SANDBOX=1")
[ -n "$db" ] && envargs+=(--db "$db")
[ -n "$port" ] && envargs+=(--set "PORT=$port" --set "VERIFY_ALLOWED_HOST=127.0.0.1:$port")
run=(systemd-run --user --scope -q -p MemoryMax="$mem" -p MemorySwapMax=0 --unit "$unit" --
  python3 "$WT_ENVPY" "${envargs[@]}" -- "${args[@]}")

cd "$wt"
if [ "$bg" = 0 ]; then
  set +e; "${run[@]}"; rc=$?; set -e
  exit "$rc"
fi
logdir="$WT_DATA/$app/$name"; mkdir -p "$logdir"
log="$logdir/$unit.log"
setsid nohup "${run[@]}" > "$log" 2>&1 < /dev/null &
for _ in $(seq 1 20); do wt_unit_alive "$uscope" && break; sleep 0.25; done
wt_unit_alive "$uscope" || { tail -20 "$log" >&2; wt_die "백그라운드 실행이 시작되지 않았습니다($unit)"; }
exec 6>"$WT_DATA/global.mutex"; flock 6
wt_json_set "$wt/.wt.json" units="+$uscope"
flock -u 6; exec 6>&-
printf '{"unit":"%s","port":%s,"log":"%s"}\n' "$uscope" "${port:-null}" "$log"
