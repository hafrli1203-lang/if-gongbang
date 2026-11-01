#!/usr/bin/env bash
# Generated from private source at 5b12eebee24334c8a78ac755dc179c5b1c3f0e47; do not edit by hand (see harness/export_v3_public.py).
# wt-land.sh <app> <name> [--retry <land-id>] [--dry]
# "Open a PR" for a work copy = land it on the app's default branch and deploy — docs/v3-wt-interface.md §5.
#  1) land.lock (${WT_DATA}/<app>/land.lock, separate from deploy.sh's own lock; waits WT_LAND_WAIT s, default 600)
#  2) the app folder must be clean (git status --porcelain); an uncommitted tsconfig.json alone is restored (next rewrites it).
#     Anything else: exit 4 with the list.
#  3) in the copy: restore next's tsconfig.json/next-env.d.ts, refuse other uncommitted tracked changes (exit 4),
#     git rebase <base> (conflict: abort, exit 6), scope-check.sh (violation: exit 2)
#  4) empty commit "land: <name>" + trailer "Landed-By: wt-land" on the copy's branch, then git merge --ff-only in the app folder
#  5) migrations/ or drizzle/ in the landed range -> PRE_SWAP_CMD="bash _shared/wt-migrate.sh <app> <land-id>" (backup, then migrate)
#     deploy: REVIEW_GATE=1 apps -> ship.sh <app>, others -> deploy.sh <app> (busy/memory/lock codes 2/3/6 wait and retry)
#  6) success: push, units stopped, post-deploy check in the copy (6b), land_id + post_verify in .wt.json, .wt.done and
#     slot released unless the check failed (then the copy is kept). exit 0 (7 = deployed but push failed)
#     failure: ${WT_DATA}/<app>/land-fail-<id>.json (landed range) -> source `git revert` of that range first ->
#     the running build: deploy.sh codes 1/2/3/4/6/8 never swapped (nothing to undo); 5/7 already swapped back by deploy.sh;
#     ship.sh path: deploy.sh guarded every attempt; anything else -> deploy.sh --rollback.
#  Apps with their own deploy (DEPLOY_CMD) are refused up front (exit 2): their rollback cannot be relied on. Then workers (${WT_UNIT_PREFIX:-ifapp}-<app>-*.service) restart on the reverted source,
#     deploy-check.sh, exit 5.
#  --retry <land-id>: reverts the revert recorded in land-fail-<id>.json and deploys again (same success/failure path).
#  --dry: runs every check for real and prints each step it would take; the repositories, the deploy and the copy are
#         not changed (the land log and land.lock are still written); exits with the code the real run would stop at.
#  Each land records the deployed tree hash (land_tree) in the log and .wt.json.
# Every step is logged to ${WT_DATA}/<app>/land-<id>.log.
# Run from a private copy (11-01 23:14: this file was edited while a live wt-land ran; bash reads a script as it goes and
# died on the half-new text after the deploy, before push). The copy under $WT_DATA/.run is what executes.
if [ -z "${WT_SELF:-}" ]; then
  _run="${WT_DATA:?WT_DATA 환경변수가 필요합니다}/.run"; mkdir -p "$_run"
  find "$_run" -name '*.sh' -mmin +1440 -delete 2>/dev/null || true
  _copy="$_run/$(basename "$0" .sh)-$$.sh"; cp "$(readlink -f "$0")" "$_copy"
  WT_SELF="$(readlink -f "$0")" exec bash "$_copy" "$@"
fi
_wt_self="$WT_SELF"; unset WT_SELF   # children (scope-check, wt-stop, ...) make their own copies
set -euo pipefail
# shellcheck source=wt-lib.sh
. "$(dirname "$_wt_self")/wt-lib.sh"

app="${1:-}"; name="${2:-}"; shift 2 2>/dev/null || true
retry=""; dry=0
while [ $# -gt 0 ]; do
  case "$1" in
    --retry) retry="${2:?--retry <land-id>}"; shift 2 ;;
    --dry) dry=1; shift ;;
    *) WT_EXIT=2 wt_die "모르는 인자: $1" ;;
  esac
done
[ -n "$app" ] && [ -n "$name" ] || WT_EXIT=2 wt_die "사용: wt-land.sh <앱> <이름> [--retry <land-id>] [--dry]"
WT_EXIT=2 wt_check_name app "$app"; WT_EXIT=2 wt_check_name existing "$name"
src="$WT_APPS_ROOT/$app"; wt="$WT_ROOT/$app/$name"; branch="wt/$name"
[ -f "$wt/.wt.json" ] || WT_EXIT=2 wt_die "사본이 아닙니다(.wt.json 없음): $wt"
base=$(wt_json_get "$wt/.wt.json" base); [ -n "$base" ] || base=$(wt_base_branch "$app")
id="${retry:-$name-$(date +%Y%m%d%H%M%S)}"
[[ "$id" =~ ^[A-Za-z0-9._-]+$ ]] || WT_EXIT=2 wt_die "land-id가 이상합니다: $id"
mkdir -p "$WT_DATA/$app"
log="$WT_DATA/$app/land-$id.log"; [ "$dry" = 1 ] && log="$WT_DATA/$app/land-$id.dry.log"
failrec="$WT_DATA/$app/land-fail-$id.json"
say() { echo "[$(date +%T)] $*" | tee -a "$log"; }
would=0   # --dry: first exit code the real run would have stopped with
stop() {  # $1 code, $2 message
  if [ "$dry" = 1 ]; then say "(dry) 실제 실행이면 여기서 멈춤 exit $1 — $2"; [ "$would" = 0 ] && would="$1"; return 0; fi
  say "$2"; exit "$1"
}
run() {  # mutating step: printed under --dry, executed otherwise
  if [ "$dry" = 1 ]; then say "(dry) 실행할 것: $*"; return 0; fi
  say "\$ $*"; "$@"
}
# Apps with their own deploy (DEPLOY_CMD in deploy.env: 해당 앱 deploy.env에 DEPLOY_CMD가 있을 때) have their own exit codes and rollback
# meanings, so a failed land could not be undone reliably (Codex review 11-01, twice). They are refused before anything moves.
self_deploy=$(bash -c '. "$1" >/dev/null 2>&1; printf "%s" "${DEPLOY_CMD:-}${DEPLOY_CMD_DEPLOY:-}"' _ "$src/deploy.env" 2>/dev/null || true)
[ -z "$self_deploy" ] || WT_EXIT=2 wt_die "자체 배포 앱(DEPLOY_CMD)은 wt-land가 합치지 않습니다 — 반장이 앱 자체 배포로 합치고 배포합니다: $app"
say "합치기 시작 — $app/$name (기본 가지 $base, land-id $id${retry:+, 재시도}${dry:+$( [ "$dry" = 1 ] && echo ', dry')})"

# 1) land lock
exec 5>"$WT_DATA/$app/land.lock"
flock -w "${WT_LAND_WAIT:-600}" 5 || { say "다른 합치기가 진행 중입니다($WT_DATA/$app/land.lock) — 나중에 다시."; exit 6; }
say "1) 합치기 잠금 잡음 — $WT_DATA/$app/land.lock"

# 1b) REVIEW_GATE apps deploy through ship.sh; another ship run would mix its fix(auto) commits into this land
review_gate=$(bash -c '. "$1" >/dev/null 2>&1; printf "%s" "${REVIEW_GATE:-0}"' _ "$src/deploy.env" 2>/dev/null || echo 0)
if [ "$review_gate" = 1 ]; then
  ship_lock="$WT_DEPLOY_STATE/$app.review/ship.lock"
  if [ -e "$ship_lock" ] && ! flock -n "$ship_lock" true; then stop 6 "이 앱의 ship.sh가 이미 돌고 있습니다 — 끝난 뒤 다시 합칩니다."; fi
fi

# 2) app folder clean
cur=$(git -C "$src" rev-parse --abbrev-ref HEAD)
[ "$cur" = "$base" ] || stop 4 "앱 폴더가 기본 가지($base)가 아니라 $cur 에 있습니다."
# next build/dev rewrites files in the app folder (first observed: " M next-env.d.ts" stopped a land unexpectedly). Unstaged
# changes to tsconfig.json / next-env.d.ts are restored from HEAD. CLAUDE.md / AGENTS.md are restored only when the
# change is nothing but Next's <!-- BEGIN:nextjs-agent-rules --> ... <!-- END:nextjs-agent-rules --> block; any other
# edit there may be a person's and stops the land, as does anything else or a staged change.
next_block_only() {  # $1 repo, $2 file -> 0 when HEAD and the working file differ only inside Next's agent-rules block
  python3 - "$1" "$2" <<'PY'
import re, subprocess, sys
repo, f = sys.argv[1], sys.argv[2]
head = subprocess.run(["git", "-C", repo, "show", f"HEAD:{f}"], capture_output=True, text=True).stdout
work = open(f"{repo}/{f}", encoding="utf-8", errors="replace").read()
# only the block and the blank lines around it are dropped; everything else, whitespace included, must match exactly
block = re.compile(r"\n*<!-- BEGIN:nextjs-agent-rules -->.*?<!-- END:nextjs-agent-rules -->\n*", re.S)
norm = lambda t: block.sub("\n", t).rstrip("\n")
sys.exit(0 if norm(head) == norm(work) else 1)
PY
}
AUTO_RE='^ M (tsconfig\.json|next-env\.d\.ts)$'
dirty=$(git -C "$src" status --porcelain)
auto=$(printf '%s\n' "$dirty" | grep -E "$AUTO_RE" | cut -c4- || true)
rest=$(printf '%s\n' "$dirty" | grep -vE "$AUTO_RE" | sed '/^$/d' || true)
for f in CLAUDE.md AGENTS.md; do
  if printf '%s\n' "$rest" | grep -qx " M $f" && next_block_only "$src" "$f"; then
    auto=$(printf '%s\n%s' "$auto" "$f" | sed '/^$/d')
    rest=$(printf '%s\n' "$rest" | grep -vx " M $f" || true)
  fi
done
if [ -n "$rest" ]; then
  say "2) 앱 폴더가 깨끗하지 않습니다:"; printf '%s\n' "$rest" | sed 's/^/     /' | tee -a "$log"
  stop 4 "앱 폴더에 커밋 안 된 변경이 있어 합치지 않습니다(위 목록)."
elif [ -n "$auto" ]; then
  # shellcheck disable=SC2086
  run git -C "$src" checkout HEAD -- $auto
  say "2) 앱 폴더 — next 자동 변경만 있어 복원: $(printf '%s' "$auto" | tr '\n' ' ')"
else
  say "2) 앱 폴더 깨끗함"
fi

if [ -z "$retry" ]; then
  # 3) copy: clean, rebased, inside its scope
  # staged changes are never next's own rewrites: refuse them before restoring anything (Codex 11-01)
  staged=$(git -C "$wt" diff --cached --name-only)
  [ -z "$staged" ] || { say "사본에 스테이징된 변경:"; printf '%s\n' "$staged" | tee -a "$log"; stop 4 "사본에 스테이징된 변경이 있습니다 — 커밋하거나 되돌린 뒤 다시."; }
  for f in tsconfig.json next-env.d.ts CLAUDE.md AGENTS.md; do   # rewritten by next dev (Next 16: CLAUDE.md)
    git -C "$wt" diff --quiet -- "$f" 2>/dev/null || run git -C "$wt" checkout HEAD -- "$f"
  done
  wdirty=$(git -C "$wt" diff --name-only | grep -vxE 'tsconfig\.json|next-env\.d\.ts|CLAUDE\.md|AGENTS\.md' || true)
  [ -z "$wdirty" ] || { say "사본에 커밋 안 된 변경:"; printf '%s\n' "$wdirty" | tee -a "$log"; stop 4 "사본에 커밋 안 된 추적 파일이 있습니다 — 커밋하거나 되돌린 뒤 다시."; }
  ahead=$(git -C "$wt" rev-list --count "$base..HEAD")
  [ "$ahead" -gt 0 ] || stop 3 "합칠 커밋이 없습니다($branch 가 $base 보다 앞선 커밋 0)."
  if git -C "$wt" merge-base --is-ancestor "$base" HEAD; then
    say "3) 사본 — $base 위에 이미 있음, 커밋 ${ahead}개"
  elif [ "$dry" = 1 ]; then
    say "3) 사본 — $base 가 앞서 나감: (dry) 실행할 것: git -C $wt rebase $base"
  else
    say "3) 사본 — git rebase $base"
    if ! git -C "$wt" rebase "$base" >>"$log" 2>&1; then
      git -C "$wt" rebase --abort >/dev/null 2>&1 || true
      stop 6 "rebase 충돌 — 사본에서 충돌을 풀고 다시 합치세요."
    fi
  fi
  sc_rc=0; bash "$WT_SHARED/scope-check.sh" "$wt" 2>&1 | tee -a "$log" || sc_rc=${PIPESTATUS[0]}
  [ "$sc_rc" = 0 ] && say "3) 범위 검사 통과" || stop 2 "범위 검사 실패 — 허용 경로 밖 변경이 있습니다."

  # 4) land commit + fast-forward
  old=$(git -C "$src" rev-parse HEAD)
  run git -C "$wt" commit -q --allow-empty -m "land: $name" -m "Landed-By: wt-land"
  run git -C "$src" merge -q --ff-only "$branch" || stop 6 "빨리감기 합치기 실패 — 앱 폴더의 $base 가 그 사이 움직였습니다. 다시 합치면 rebase부터 합니다."
  new=$(git -C "$src" rev-parse HEAD)
  range_files=$(git -C "$wt" diff --name-only "$old...HEAD")
else
  [ -f "$failrec" ] || WT_EXIT=2 wt_die "재시도 기록이 없습니다: $failrec"
  revert_c=$(wt_json_get "$failrec" revert)
  [ -n "$revert_c" ] || WT_EXIT=2 wt_die "기록에 되돌림 커밋이 없습니다: $failrec"
  # what the retry re-applies is everything the recorded revert undid (the landed range plus ship.sh fix(auto) commits)
  sc_rc=0; bash "$WT_SHARED/scope-check.sh" "$wt" --range "$(git -C "$src" rev-parse "$revert_c^")..$revert_c" 2>&1 | tee -a "$log" || sc_rc=${PIPESTATUS[0]}
  [ "$sc_rc" = 0 ] && say "3) 재시도 범위 검사 통과" || stop 2 "재시도 범위 검사 실패 — 허용 경로 밖 변경이 있습니다."
  old=$(git -C "$src" rev-parse HEAD)
  run git -C "$src" revert --no-edit "$revert_c"
  new=$(git -C "$src" rev-parse HEAD)
  range_files=$(git -C "$src" diff --name-only "$(wt_json_get "$failrec" old)" "$(wt_json_get "$failrec" new)")
  say "3~4) 재시도 — 되돌림 $revert_c 을 다시 되돌림"
fi
say "4) 합친 범위 ${old:0:9}..${new:0:9} — 파일 $(printf '%s\n' "$range_files" | grep -c . || true)개"
# the exact tree that is deployed (after rebase / retry): recorded so the land can be matched to a build later
if [ "$dry" = 1 ]; then land_tree=$(git -C "$wt" rev-parse 'HEAD^{tree}'); else land_tree=$(git -C "$src" rev-parse 'HEAD^{tree}'); fi
say "4) 합칠 트리 $land_tree"
[ "$dry" = 1 ] || wt_json_set "$wt/.wt.json" land_tree="$land_tree" land_commit="$new"

# 5) deploy (migrations first, inside deploy.sh's pre-swap point)
pre=""
if printf '%s\n' "$range_files" | grep -qE "$WT_MIG_RE"; then
  pre="bash $WT_SHARED/wt-migrate.sh $app $id"
  say "5) 마이그레이션 있음 — PRE_SWAP_CMD=\"$pre\" (백업 → migrate)"
fi
if [ "$review_gate" = 1 ]; then
  [ -x "$WT_SHARED/ship.sh" ] || stop 2 "REVIEW_GATE=1 이지만 ship.sh 어댑터가 없습니다 — docs/v3-adapters.md 참고."
  deploy=(bash "$WT_SHARED/ship.sh" "$app")
else deploy=(bash "$WT_SHARED/deploy.sh" "$app"); fi
say "5) 배포 — ${deploy[*]}"
rc=0
if [ "$dry" = 1 ]; then
  say "(dry) 실행할 것: PRE_SWAP_CMD=${pre:-없음} ${deploy[*]}"
else
  for _ in $(seq 1 20); do
    rc=0; PRE_SWAP_CMD="$pre" "${deploy[@]}" >>"$log" 2>&1 || rc=$?
    case "$rc" in 2|3|6) [ "$review_gate" = 1 ] && break; say "배포 대기(코드 $rc) — 1분 뒤 다시"; sleep 60 ;; *) break ;; esac
  done
  tail -5 "$log"
fi

restart_workers() {  # every enabled ${WT_UNIT_PREFIX:-ifapp}-<app>-*.service except the web unit, by unit name
  local web u
  web=$(bash -c '. "$1" >/dev/null 2>&1; printf "%s" "${UNIT:-}"' _ "$src/deploy.env" 2>/dev/null || true); web="${web:-${WT_UNIT_PREFIX:-ifapp}-$app}"
  for u in $(systemctl --user list-units --type=service --all --no-legend --plain "${WT_UNIT_PREFIX:-ifapp}-$app-*.service" 2>/dev/null | awk '{print $1}'); do
    [ "$u" = "$web.service" ] && continue
    [ "$(systemctl --user is-enabled "$u" 2>/dev/null || true)" = disabled ] && continue
    systemctl --user restart "$u" && say "워커 재시작 — $u" || say "워커 재시작 실패 — $u"
  done
}

# 6b) After a good deploy: when the app has .claude/skills/verify-<app>/ (launch.sh, doctor.sh, drive.mjs), run them in the
# landed work copy (its own port and slot test DB, never production): launch -> doctor -> one journey. The journey is
# deploy.env VERIFY_JOURNEY, else the first journey without arguments on the SKILL.md "Journeys:" line.
# A failure never rolls back (the deploy already passed deploy-check); it is recorded for the KakaoTalk report.
# Sets post_verify = passed | doctor_only | failed | skipped and post_evidence = evidence dir (or log).
post_verify_run() {
  local skill="$wt/.claude/skills/verify-$app" h journey ev out
  post_verify=skipped; post_evidence=""
  h="$skill"; [ -f "$h/launch.sh" ] || h="$skill/scripts"   # helpers at the skill root or under scripts/ (e.g. helpers kept under scripts/)
  if [ ! -d "${WT_APPS_ROOT}/$app/.claude/skills/verify-$app" ] || [ ! -f "$h/launch.sh" ] || [ ! -f "$h/doctor.sh" ] || [ ! -f "$h/drive.mjs" ]; then
    say "6b) 배포 뒤 확인 — verify-$app 스킬 없음: deploy.sh 응답 확인만"; return 0
  fi
  post_evidence="$log"
  # check what was deployed: ship.sh may have added fix(auto) commits on the base branch after this copy's tip
  if [ "$(git -C "$wt" rev-parse HEAD)" != "$(git -C "$src" rev-parse HEAD)" ]; then
    git -C "$wt" merge -q --ff-only "$(git -C "$src" rev-parse HEAD)" >>"$log" 2>&1 \
      || { post_verify=failed; say "배포 뒤 확인 실패 — 사본을 배포된 커밋으로 맞추지 못함"; return 0; }
  fi
  journey="${VERIFY_JOURNEY:-}"
  [ -n "$journey" ] || journey=$(bash -c '. "$1" >/dev/null 2>&1; printf "%s" "${VERIFY_JOURNEY:-}"' _ "$src/deploy.env" 2>/dev/null || true)
  if [ -z "$journey" ]; then
    journey=$(grep -m1 -oE 'Journeys:.*' "$skill/SKILL.md" | grep -oE '`[a-z0-9-]+`' | tr -d '`' | head -1 || true)
  fi
  if [ -z "$journey" ]; then   # no journey known: launch + doctor only (deploy.env VERIFY_JOURNEY adds one)
    say "6b) 배포 뒤 확인(사본, 운영 아님) — 여정 지정 없음(deploy.env VERIFY_JOURNEY): launch → doctor만"
    if timeout 600 bash "$h/launch.sh" "$wt" >>"$log" 2>&1 && timeout 180 bash "$h/doctor.sh" "$wt" >>"$log" 2>&1; then
      post_verify=doctor_only; say "6b) 배포 뒤 확인 — doctor 통과(여정 없음)"
    else post_verify=failed; say "배포 뒤 확인 실패 — launch 또는 doctor"; fi
    return 0
  fi
  say "6b) 배포 뒤 확인(사본 $wt, 운영 아님) — launch → doctor → $journey"
  if ! timeout 600 bash "$h/launch.sh" "$wt" >>"$log" 2>&1; then post_verify=failed; say "배포 뒤 확인 실패 — launch"; return 0; fi
  if ! timeout 180 bash "$h/doctor.sh" "$wt" >>"$log" 2>&1; then post_verify=failed; say "배포 뒤 확인 실패 — doctor"; return 0; fi
  out=$(timeout 600 node "$h/drive.mjs" "$wt" "$journey" 2>&1) || post_verify=failed
  printf '%s\n' "$out" >>"$log"
  # evidence dir: "out" in the result JSON on success, "(proof in <dir>)" on failure
  ev=$(printf '%s' "$out" | grep -oE "$wt/\.verify/[^\") ]+" | tail -1 || true)
  post_evidence="${ev:-$log}"
  if [ "$post_verify" != failed ] && printf '%s' "$out" | grep -q '"ok":true'; then post_verify=passed; else post_verify=failed; fi
  say "6b) 배포 뒤 확인 $( [ "$post_verify" = passed ] && echo 통과 || echo 실패 ) — $journey, 증거 $post_evidence"
}

if [ "$rc" = 0 ]; then
  # 6) success
  if git -C "$src" remote get-url origin >/dev/null 2>&1; then
    run git -C "$src" push -q origin "$base" || { say "배포는 됐지만 push 실패 — 나중에 git -C $src push origin $base"; rc=7; }
  else
    say "6) 원격 없음 — push 건너뜀"
  fi
  if [ "$dry" = 0 ]; then
    bash "$WT_SHARED/wt-stop.sh" "$wt" >>"$log" 2>&1 || true
    post_verify_run
    bash "$WT_SHARED/wt-stop.sh" "$wt" >>"$log" 2>&1 || true
    # what was really deployed: ship.sh may have added fix(auto) commits after the land commit
    wt_json_set "$wt/.wt.json" land_id="$id" landed_at="$(date -Iseconds)" post_verify="$post_verify" post_evidence="${post_evidence:-null}" \
      land_commit="$(git -C "$src" rev-parse HEAD)" land_tree="$(git -C "$src" rev-parse 'HEAD^{tree}')"
    say "6) 배포된 커밋 $(git -C "$src" rev-parse --short HEAD), 트리 $(git -C "$src" rev-parse 'HEAD^{tree}')"
    rm -f "$failrec"
    if [ "$post_verify" = failed ]; then
      # deployed and pushed; no rollback. The copy, its slot DB and the evidence stay for whoever looks next.
      say "배포 뒤 확인 실패 — 되돌리지 않음. 사본·시험 DB·증거 보존($wt)"
    else
      date -Iseconds > "$wt/.wt.done"
      wt_slot_release "$app" "$wt"
    fi
  else
    say "(dry) 실행할 것: wt-stop.sh $wt; 배포 뒤 확인(verify-$app 스킬이 있으면 사본에서 launch→doctor→여정 1개); .wt.json land_id=$id post_verify; 통과·건너뜀이면 .wt.done 생성·시험 DB 슬롯 반납"
  fi
  say "합치기 끝 — $app/$name ${new:0:9}${retry:+ (재시도)}"
  [ "$dry" = 1 ] && exit "$would"
  exit "$rc"
fi

# failure: record, revert the source first, then bring the running build and workers back to it
# Our commits = the landed range plus ship.sh's own fix(auto) commits made during this deploy (Codex review 11-01: a range
# fixed before ship.sh ran would leave its automatic fixes in place). Commits other sessions made meanwhile are not ours.
# ship.sh exit 2 = it did not run (its lock or a dirty folder): fix(auto) commits after $new are then someone else's
ours=$( { [ "$review_gate" = 1 ] && [ "$rc" != 2 ] && git -C "$src" rev-list --grep='^fix(auto):' "$new..HEAD"; git -C "$src" rev-list "$old..$new"; } )
head_now=$(git -C "$src" rev-parse HEAD)
say "배포 실패(코드 $rc) — 되돌립니다. 기록 $failrec"
python3 - "$failrec" "$id" "$app" "$name" "$old" "$new" "$head_now" "$rc" <<'PY'
import json, sys, datetime
p, i, a, n, o, nw, hd, rc = sys.argv[1:]
json.dump({"id": i, "app": a, "name": n, "old": o, "new": nw, "head_after_deploy": hd, "deploy_rc": int(rc),
           "at": datetime.datetime.now().astimezone().isoformat()}, open(p, "w"), ensure_ascii=False)
PY
# shellcheck disable=SC2086
if [ -n "$ours" ] && git -C "$src" revert --no-commit $ours && git -C "$src" commit -q --allow-empty -m "revert: land $id 배포 실패(코드 $rc) — wt-land 자동 되돌림" -m "Landed-By: wt-land"; then
  wt_json_set "$failrec" revert="$(git -C "$src" rev-parse HEAD)"
  say "소스 되돌림 커밋 $(git -C "$src" rev-parse --short HEAD) — 커밋 $(printf '%s\n' "$ours" | grep -c .)개"
else
  git -C "$src" revert --abort >/dev/null 2>&1 || true
  say "소스 되돌림 실패 — 반장이 직접 되돌려야 합니다: $(printf '%s\n' "$ours" | cut -c1-9 | tr '\n' ' ')"
fi
if [ "$review_gate" = 1 ]; then rc_kind=ship; else rc_kind="$rc"; fi
case "$rc_kind" in
  ship) say "ship.sh 경로 — deploy.sh가 실패 때마다 이전 빌드를 지킵니다. 워커만 되돌린 소스로 다시 띄웁니다."; restart_workers ;;
  1|2|3|4|6|8) say "도는 빌드는 바뀌지 않았습니다(배포가 교체 전에 멈춤, 코드 $rc)." ;;
  5|7) say "deploy.sh가 이미 이전 빌드로 되돌렸습니다 — 워커만 되돌린 소스로 다시 띄웁니다."; restart_workers ;;
  *) say "배포가 어디서 멈췄는지 모름(코드 $rc) — deploy.sh --rollback"; bash "$WT_SHARED/deploy.sh" "$app" --rollback >>"$log" 2>&1 || say "되돌림(--rollback) 실패 — 반장 확인" ;;
esac
if bash "$WT_SHARED/deploy-check.sh" "$app" >>"$log" 2>&1; then say "응답 확인 통과 — 이전 상태로 돌아왔습니다."; else say "응답 확인 실패 — 반장 확인 필요(기록 $log)"; fi
say "재시도: wt-land.sh $app $name --retry $id"
exit 5
