#!/usr/bin/env bash
# Generated from private source at a8eb56b1bac538511499bb39b350117e5eead6e1; do not edit by hand (see harness/export_v3_public.py).
# wt-migrate.sh <app> <land-id>
# PRE_SWAP_CMD of a land that brings migrations (wt-land.sh -> deploy.sh, after a good build, before the swap).
# 1) pg_dump -Fc of the app's production DB -> ${WT_BACKUPS}/<app>/pre-land-<land-id>.dump; any failure exits 1
#    (deploy.sh then stops with exit 8 and the running build stays).
# 2) the app's production migrate: package.json "db:migrate" or "migrate" (they load the app's env file themselves),
#    or deploy.env WT_MIGRATE_CMD with the env file loaded into the child only. Values are never printed.
# Rules for landed migrations (plan §5): one migration file per land, additive only, old code must keep writing.
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

app="${1:-}"; id="${2:-}"
wt_check_name app "$app"
[[ "$id" =~ ^[A-Za-z0-9._-]+$ ]] || wt_die "land-id가 이상합니다: $id"
src="$WT_APPS/$app"
envf=$(wt_app_env "$app")
[ -n "$envf" ] && python3 "$WT_ENVPY" has --env-file "$envf" || wt_die "운영 DB 주소(DATABASE_URL)를 찾지 못했습니다 — 백업 없이 마이그레이션하지 않습니다."

# never overwrite: a retry or ship.sh's second attempt must keep the dump taken before the first migrate (Codex 11-01)
out="$WT_BACKUPS/$app/pre-land-$id-$(date +%Y%m%d%H%M%S)-$$.dump"
[ -e "$out" ] && wt_die "백업 파일 이름이 겹칩니다: $out"
mkdir -p "$WT_BACKUPS/$app"
echo "백업 — pg_dump → $out"
python3 "$WT_ENVPY" pg --env-file "$envf" -- pg_dump -Fc -f "$out" || { rm -f "$out"; wt_die "백업 실패 — 마이그레이션하지 않습니다."; }
pg_restore -l "$out" >/dev/null || wt_die "백업 파일을 읽을 수 없습니다($out) — 마이그레이션하지 않습니다."
echo "백업 완료 — $(du -h "$out" | cut -f1)"

mig=""
[ -f "$src/deploy.env" ] && mig=$(bash -c '. "$1" >/dev/null 2>&1; printf "%s" "${WT_MIGRATE_CMD:-}"' _ "$src/deploy.env")
cd "$src"
# Both paths run with the env file loaded by wt-env.py real (inherited DB variables dropped first): node --env-file does NOT
# override a DATABASE_URL already in the environment, so the DB that was backed up is the DB that gets migrated.
if [ -n "$mig" ]; then
  echo "마이그레이션(운영) — WT_MIGRATE_CMD"
  PATH="$src/node_modules/.bin:$PATH" python3 "$WT_ENVPY" real --env-file "$envf" -- bash -c "$mig"
else
  script=$(node -e 'const s=(require("./package.json").scripts)||{}; process.stdout.write(s["db:migrate"]?"db:migrate":(s["migrate"]?"migrate":""))')
  [ -n "$script" ] || wt_die "마이그레이션 명령이 없습니다(package.json db:migrate/migrate, deploy.env WT_MIGRATE_CMD)."
  echo "마이그레이션(운영) — npm run $script"
  python3 "$WT_ENVPY" real --env-file "$envf" -- npm run -s "$script"
fi
echo "마이그레이션 완료 — 되돌릴 땐 pg_restore --clean -d <db> $out"
