#!/usr/bin/env bash
# Generated from private source at d72495d955c050e98c98a283b5521f31f3eff43a; do not edit by hand (see harness/export_v3_public.py).
# wt-new.sh <app> <name> [--task <id>] [--scope-file <file>]
# Creates a work copy (git worktree) for one job — contract: docs/v3-wt-interface.md §1.
#  - limits: active copies per app <= WT_APP_MAX (3), all apps <= WT_GLOBAL_MAX (6); a scope that allows migrations/
#    needs no other migrations-scoped copy of the same app. Over a limit: prints "대기 ..." and exits 3.
#  - worktree ${WT_ROOT}/<app>/<name> on branch wt/<name> from the app's default branch (its checked-out HEAD branch)
#  - node_modules is a hard-link copy of the app's (cp -al; installing inside a copy is refused by wt-bin/npm)
#  - apps with a postgres DATABASE_URL get a test DB slot <app>_wt<N>_test: every user schema dropped, public recreated
#    for the app role, the production DB's extensions recreated (read from its catalog), then the app's migrate command
#    run inside the copy with the slot DB in the environment (never the app's env file)
#  - a free port 4000-4999, .wt.json written, one JSON line on stdout
# Migrate command: deploy.env WT_MIGRATE_CMD, else package.json db:migrate / migrate with --env-file removed,
# else `npx drizzle-kit migrate` when a drizzle config exists, else none.
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

app="${1:-}"; name="${2:-}"; shift 2 2>/dev/null || true
task=""; scope_file=""
while [ $# -gt 0 ]; do
  case "$1" in
    --task) task="${2:?--task 값}"; shift 2 ;;
    --scope-file) scope_file="${2:?--scope-file 값}"; shift 2 ;;
    *) WT_EXIT=2 wt_die "모르는 인자: $1" ;;
  esac
done
[ -n "$app" ] && [ -n "$name" ] || WT_EXIT=2 wt_die "사용: wt-new.sh <앱> <이름> [--task <id>] [--scope-file <파일>]"
WT_EXIT=2 wt_check_name app "$app"; WT_EXIT=2 wt_check_name name "$name"
[ -z "$scope_file" ] || [ -f "$scope_file" ] || WT_EXIT=2 wt_die "범위 파일이 없습니다: $scope_file"
src="$WT_APPS_ROOT/$app"; wt="$WT_ROOT/$app/$name"; branch="wt/$name"
[ -e "$wt" ] && WT_EXIT=2 wt_die "이미 있는 사본입니다: $wt"
git -C "$src" show-ref --verify --quiet "refs/heads/$branch" && WT_EXIT=2 wt_die "가지가 이미 있습니다: $branch"
mkdir -p "$WT_DATA" "$WT_ROOT/$app"

# 1) limits + port under one global mutex (two wt-new calls must not both see room for one more)
exec 6>"$WT_DATA/global.mutex"; flock 6
app_n=$(wt_active_dirs "$app" | wc -l); all_n=$(wt_active_dirs "" | wc -l)
[ "$app_n" -lt "$WT_APP_MAX" ] || { echo "대기 — $app 사본 ${app_n}개(상한 $WT_APP_MAX)"; exit 3; }
[ "$all_n" -lt "$WT_GLOBAL_MAX" ] || { echo "대기 — 전체 사본 ${all_n}개(상한 $WT_GLOBAL_MAX)"; exit 3; }
if [ -n "$scope_file" ] && wt_scope_has_migrations "$scope_file"; then
  for d in $(wt_active_dirs "$app"); do
    wt_scope_has_migrations "$d/.wt.scope" && { echo "대기 — $app 에 DB 변경 사본이 이미 있습니다($d)"; exit 3; }
  done
fi
used_ports=" "
for d in $(wt_active_dirs ""); do p=$(wt_json_get "$d/.wt.json" port); [ -n "$p" ] && used_ports+="$p "; done
listening=" $(ss -ltnH 2>/dev/null | awk '{n=split($4,a,":"); print a[n]}' | sort -u | tr '\n' ' ') "
port=""
for p in $(seq 4000 4999); do
  case "$used_ports$listening" in *" $p "*) continue ;; esac
  port="$p"; break
done
[ -n "$port" ] || { echo "대기 — 빈 포트(4000~4999)가 없습니다"; exit 3; }

base=$(wt_base_branch "$app")
created=0; slot=""
cleanup() {  # undo a half-made copy so a retry starts clean
  [ "$created" = 1 ] || return 0
  [ -n "$slot" ] && wt_slot_release "$app" "$wt"
  git -C "$src" worktree remove --force "$wt" >/dev/null 2>&1 || true
  git -C "$src" branch -D "$branch" >/dev/null 2>&1 || true
}
trap 'rc=$?; [ $rc -ne 0 ] && cleanup; exit $rc' EXIT

git -C "$src" worktree add -q -b "$branch" "$wt" "$base" >&2
created=1
# placeholder so the next wt-new (after the mutex is released) counts this copy and its port
wt_json_set "$wt/.wt.json" app="$app" name="$name" branch="$branch" base="$base" path="$wt" port="$port" units="[]" db=null
[ -n "$scope_file" ] && cp "$scope_file" "$wt/.wt.scope"
# Copy artifacts are ignored by git structurally (Tailwind's source scan and scope-check both follow git ignores).
# A worktree's info/exclude is the repository's common one, so this also covers the app folder; lines are only appended.
excl=$(git -C "$wt" rev-parse --path-format=absolute --git-path info/exclude)
mkdir -p "$(dirname "$excl")"; touch "$excl"
for p in '.next-wt*/' '.verify/' '.wt.json' '.wt.json.tmp' '.wt.scope' '.wt.done' '.pids'; do
  grep -qxF -- "$p" "$excl" || printf '%s\n' "$p" >> "$excl"
done
flock -u 6; exec 6>&-
# node_modules: hard-link copy, not a symlink — Next 16/Turbopack refuses a node_modules symlink pointing out of the project
# root (observed on two apps). Same filesystem: almost no disk, seconds. Other filesystem: rsync --link-dest.
if [ -d "$src/node_modules" ]; then
  cp -al "$src/node_modules" "$wt/node_modules" 2>/dev/null \
    || { rm -rf -- "$wt/node_modules"; rsync -a --link-dest="$src/node_modules/" "$src/node_modules/" "$wt/node_modules/"; } \
    || wt_die "node_modules 복사 실패"
  # relative symlinks that leave node_modules (file:../_shared/kit -> ../../../_shared/kit) break one level deeper in
  # the copy: re-point each broken one at what the app folder's same link resolves to, as an absolute path. ln -sfn
  # replaces only the copy's directory entry; the app folder's link (same inode under cp -al) is left as it was.
  relinked=0
  while IFS= read -r -d '' l; do
    t=$(readlink -f "$src/${l#"$wt/"}" 2>/dev/null || true)
    [ -n "$t" ] && [ -e "$t" ] || { echo "끊긴 링크(앱 폴더에서도 대상 없음): ${l#"$wt/"}" >&2; continue; }
    ln -sfn "$t" "$l"; relinked=$((relinked + 1))
  done < <(find "$wt/node_modules" -maxdepth 3 -xtype l -print0)
  [ "$relinked" = 0 ] || echo "node_modules 링크 재연결 ${relinked}개" >&2
fi

# 2) test DB slot
envf=$(wt_app_env "$app")
db=""
if [ -n "$envf" ] && python3 "$WT_ENVPY" has --env-file "$envf"; then
  slot=$(wt_slot_claim "$app" "$wt") || { echo "대기 — $app 시험 DB 슬롯 $WT_SLOTS개가 모두 사용 중" ; exit 3; }
  db="${app}_wt${slot}_test"
  exists=$(wt_pg_admin postgres psql -XtAqc "select 1 from pg_database where datname = '$db'")
  [ "$exists" = 1 ] || wt_die "시험 DB가 없습니다: $db (풀 생성 필요)"
  # apps with a separate migration role (DATABASE_OWNER_URL): public belongs to that role, and its
  # migrate command connects with it (wt-env.py maps DATABASE_OWNER_URL to the slot DB with the owner's credentials)
  if python3 "$WT_ENVPY" has --env-file "$envf" --key DATABASE_OWNER_URL; then
    owner=$(python3 "$WT_ENVPY" user --env-file "$envf" --key DATABASE_OWNER_URL)
  else
    owner=$(python3 "$WT_ENVPY" user --env-file "$envf")
  fi
  prod_db=$(python3 "$WT_ENVPY" dbname --env-file "$envf")
  [ "$prod_db" != "$db" ] || wt_die "시험 DB 이름이 운영 DB와 같습니다"
  exts=$(wt_pg_admin "$prod_db" psql -XtAqc "select quote_ident(extname) from pg_extension e join pg_namespace n on n.oid = e.extnamespace where extname <> 'plpgsql' and nspname = 'public'")
  # drop every user schema (public, rs, drizzle, ...) then recreate public for the app role
  # shellcheck disable=SC2016
  sql=$(cat <<SQL
DO \$\$ DECLARE s text; BEGIN
  FOR s IN SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\_%' AND nspname <> 'information_schema' LOOP
    EXECUTE format('DROP SCHEMA %I CASCADE', s);
  END LOOP;
END \$\$;
CREATE SCHEMA public AUTHORIZATION $(printf '"%s"' "${owner//\"/\"\"}");
SQL
)
  for e in $exts; do sql+=$'\n'"CREATE EXTENSION IF NOT EXISTS $e SCHEMA public;"; done
  wt_pg_admin "$db" psql -XqAt -v ON_ERROR_STOP=1 -c "$sql" >&2
  wt_json_set "$wt/.wt.json" db="$db" slot="$slot"

  # migrate command
  mig=""
  if [ -f "$src/deploy.env" ]; then mig=$(bash -c '. "$1" >/dev/null 2>&1; printf "%s" "${WT_MIGRATE_CMD:-}"' _ "$src/deploy.env"); fi
  if [ -z "$mig" ]; then
    mig=$(node -e '
      const s = (require(process.argv[1]).scripts) || {};
      const c = s["db:migrate"] || s["migrate"] || "";
      process.stdout.write(c.replace(/\s--env-file(=|\s+)\S+/g, ""));
    ' "$wt/package.json" 2>/dev/null || true)
  fi
  if [ -z "$mig" ] && ls "$wt"/drizzle.config.* >/dev/null 2>&1; then mig="npx drizzle-kit migrate"; fi
  if [ -n "$mig" ]; then
    echo "마이그레이션(사본, $db): $mig" >&2
    ( cd "$wt" && PATH="$WT_SHARED/wt-bin:$wt/node_modules/.bin:$PATH" \
        python3 "$WT_ENVPY" app --app-dir "$src" --db "$db" --verify-env "$(wt_verify_env "$wt" "$app")" -- bash -c "$mig" ) >&2 \
      || wt_die "시험 DB 마이그레이션 실패($db) — 사본을 지웁니다"
  else
    echo "마이그레이션 명령 없음 — 빈 스키마로 둡니다($db)" >&2
  fi

  # Grants other roles hold in the app's own test DB (env file TEST_DATABASE_URL: roles granted there) are not made by migrations. Copy them for objects that exist in the slot too: schema ACLs, table/sequence
  # ACLs, default privileges. Read from the reference catalog as admin, applied as admin; statements are not printed.
  ref_db=""
  python3 "$WT_ENVPY" has --env-file "$envf" --key TEST_DATABASE_URL \
    && ref_db=$(python3 "$WT_ENVPY" dbname --env-file "$envf" --key TEST_DATABASE_URL)
  if [ -n "$ref_db" ] && [ "$ref_db" != "$prod_db" ] && [ "$ref_db" != "$db" ] \
    && [ "$(wt_pg_admin postgres psql -XtAqc "select 1 from pg_database where datname = '$ref_db'")" = 1 ]; then
    # shellcheck disable=SC2016
    grants=$(wt_pg_admin "$ref_db" psql -XtAq -v ON_ERROR_STOP=1 <<'SQL'
WITH acl AS (
  SELECT 'SCHEMA' AS kind, n.nspname AS sch, NULL::text AS rel, (aclexplode(n.nspacl)).*
    FROM pg_namespace n WHERE n.nspacl IS NOT NULL AND n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
  UNION ALL
  SELECT CASE WHEN c.relkind = 'S' THEN 'SEQUENCE' ELSE 'TABLE' END, n.nspname, c.relname, (aclexplode(c.relacl)).*
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE c.relacl IS NOT NULL AND c.relkind IN ('r','p','v','m','S') AND n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema'
)
SELECT format($f$DO $d$ BEGIN IF %s THEN EXECUTE %L; END IF; END $d$;$f$,
         CASE WHEN rel IS NULL THEN format('EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %L)', sch)
              ELSE format('to_regclass(%L) IS NOT NULL', format('%I.%I', sch, rel)) END,
         format('GRANT %s ON %s %s TO %s', privilege_type, kind,
                CASE WHEN rel IS NULL THEN quote_ident(sch) ELSE format('%I.%I', sch, rel) END,
                CASE WHEN grantee = 0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(grantee)) END))
  FROM acl WHERE grantee = 0 OR pg_get_userbyid(grantee) <> pg_get_userbyid(grantor)
UNION ALL
SELECT format($f$DO $d$ BEGIN IF %s THEN EXECUTE %L; END IF; END $d$;$f$,
         CASE WHEN d.defaclnamespace = 0 THEN 'true'
              ELSE format('EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %L)', d.defaclnamespace::regnamespace::text) END,
         format('ALTER DEFAULT PRIVILEGES FOR ROLE %I%s GRANT %s ON %s TO %s', pg_get_userbyid(d.defaclrole),
                CASE WHEN d.defaclnamespace = 0 THEN '' ELSE ' IN SCHEMA ' || d.defaclnamespace::regnamespace::text END,
                a.privilege_type,
                CASE d.defaclobjtype WHEN 'r' THEN 'TABLES' WHEN 'S' THEN 'SEQUENCES' WHEN 'f' THEN 'FUNCTIONS' WHEN 'T' THEN 'TYPES' ELSE 'SCHEMAS' END,
                CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END))
  FROM pg_default_acl d, aclexplode(d.defaclacl) a
 WHERE a.grantee = 0 OR a.grantee <> d.defaclrole;
SQL
)
    if [ -n "$grants" ]; then
      printf '%s\n' "$grants" | wt_pg_admin "$db" psql -XqAt -v ON_ERROR_STOP=1 >/dev/null \
        || wt_die "기준 시험 DB($ref_db)의 권한 복제 실패($db)"
      echo "권한 복제 — 기준 $ref_db 에서 $(printf '%s\n' "$grants" | grep -c .)줄" >&2
    fi
  fi
fi

now=$(date -Iseconds)
wt_json_set "$wt/.wt.json" created_at="$now" ${task:+task_id="$task"}
trap - EXIT
python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])), ensure_ascii=False))' "$wt/.wt.json"
