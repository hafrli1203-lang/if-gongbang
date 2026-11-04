#!/usr/bin/env bash
# Generated from private source at 495481ceddef11d8ccd4438d8948d5cc71c484d2; do not edit by hand (see harness/export_v3_public.py).
# wt-land.sh <app> <name> [--retry <land-id>] [--dry] [--ack-unknown | --resolve kept|reverted]
# "Open a PR" for a work copy = land it on the app's default branch and deploy — docs/v3-wt-interface.md §5.
#  1) land.lock (${WT_DATA}/<app>/land.lock, separate from deploy.sh's own lock; waits WT_LAND_WAIT s, default 600)
#  2) the app folder must be clean (git status --porcelain); an uncommitted tsconfig.json alone is restored (next rewrites it).
#     Anything else: exit 4 with the list.
#  3) in the copy: restore next's tsconfig.json/next-env.d.ts, refuse other uncommitted tracked changes (exit 4),
#     git rebase <base> (conflict: abort, exit 6), scope-check.sh (violation: exit 2)
#  4) land commit "land: <name>" + trailer "Landed-By: wt-land" built from the checked commit tree (commit-tree, not the
#     index) on the copy branch, then git merge --ff-only of exactly that commit in the app folder
#  5) migrations/ or drizzle/ in the landed range -> PRE_SWAP_CMD="bash _shared/wt-migrate.sh <app> <land-id>" (backup, then migrate)
#     deploy: REVIEW_GATE=1 apps -> ship.sh <app>, others -> deploy.sh <app> (busy/memory/lock codes 2/3/6 wait and retry)
#  6) success: push, units stopped, post-deploy check in the copy (6b), land_id + post_verify in .wt.json, .wt.done and
#     slot released unless the check failed (then the copy is kept). exit 0 (7 = deployed but push failed)
#     failure: ${WT_DATA}/<app>/land-fail-<id>.json (landed range) -> source `git revert` of that range first ->
#     the running build: deploy.sh codes 1/2/3/4/6/8 never swapped (nothing to undo); 5/7 already swapped back by deploy.sh;
#     ship.sh path: deploy.sh guarded every attempt; anything else -> deploy.sh --rollback.
#  Apps with their own deploy (DEPLOY_CMD) go through the same land, with one extra independent confirmation step (5a) and their own recovery path on failure (self_deploy branch below). Workers (${WT_UNIT_PREFIX:-ifapp}-<app>-*.service) restart on the reverted source,
#     deploy-check.sh, exit 5.
#  Also (v2 gaps, 2026-11-02): a land whose deploy outcome is unknown blocks later lands (exit 10, land-unknown-<id>.json,
#  never reverted automatically — gap 183); a security check runs before the merge (sec-gate.sh: secrets in added lines,
#  new dependency advisories; exit 11 — gap 34); the shared .git/config must be unchanged since wt-new (exit 4 — gap
#  155/271); the deploy gets DEPLOY_EXPECT_TREE = the merged tree and its receipt is read back (gaps 40/190).
#  --retry <land-id>: reverts the revert recorded in land-fail-<id>.json and deploys again (same success/failure path).
#  --dry: runs every check for real and prints each step it would take; the repositories, the deploy and the copy are
#         not changed (the land log and land.lock are still written); exits with the code the real run would stop at.
#  Each land records the deployed tree hash (land_tree) in the log and .wt.json.
# Every step is logged to ${WT_DATA}/<app>/land-<id>.log.
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
retry=""; dry=0; ack=0; resolution=""
while [ $# -gt 0 ]; do
  case "$1" in
    --retry) retry="${2:?--retry <land-id>}"; shift 2 ;;
    --dry) dry=1; shift ;;
    --ack-unknown) ack=1; shift ;;   # a person checked production: clear the unknown marks, then stop
    --resolve)   # gap 132: a person settles an unknown outcome — kept (the new build runs) or reverted (the old one runs)
      ack=1; resolution="${2:-}"; shift 2 || true
      case "$resolution" in kept|reverted) ;; *) WT_EXIT=2 wt_die "--resolve kept | reverted" ;; esac ;;
    *) WT_EXIT=2 wt_die "모르는 인자: $1" ;;
  esac
done
[ -n "$app" ] && [ -n "$name" ] || WT_EXIT=2 wt_die "사용: wt-land.sh <앱> <이름> [--retry <land-id>] [--dry]"
WT_EXIT=2 wt_check_name app "$app"; WT_EXIT=2 wt_check_name existing "$name"
src="$WT_APPS/$app"; wt="$WT_ROOT/$app/$name"; branch="wt/$name"
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
# Apps with their own deploy (DEPLOY_CMD in deploy.env) have their own exit
# codes and rollback meanings. The common deploy.sh already forwards to DEPLOY_CMD for these (it reads deploy.env the
# same way wt-land does), so a normal land goes through exactly like any other app (step 5 below). The only difference
# is on deploy failure (team-lead 11-02, L1-0232): their own rollback cannot be relied on in general, so wt-land's own
# recovery there is "revert the source, run DEPLOY_CMD once more" (the same thing each of their scripts already does
# internally — some apps keep a pre-build copy and swap only after a good build; one app's own comment
# says the same by hand: "이전 커밋으로 checkout 뒤 다시 deploy"). If that second run also fails, nothing more is
# guessed at: the land stops, a person is paged (notify-owner), and the copy is kept — see the self_deploy branch below.
self_deploy=$(bash -c '. "$1" >/dev/null 2>&1; printf "%s" "${DEPLOY_CMD:-}${DEPLOY_CMD_DEPLOY:-}"' _ "$src/deploy.env" 2>/dev/null || true)
# The Next project may live in a subfolder (deploy.env APP_DIR, someapp: web/): next's own rewrites of tsconfig.json /
# next-env.d.ts land there, not at the repo root, and a land stopped on them twice (someapp session, 11-04).
app_sub=$(bash -c '. "$1" >/dev/null 2>&1; printf "%s" "${APP_DIR:-}"' _ "$src/deploy.env" 2>/dev/null || true); app_sub="${app_sub%/}"
case "$app_sub" in "$src"/*) app_sub="${app_sub#"$src"/}" ;; /*) app_sub="" ;; esac   # absolute (someapp) -> relative; outside the app -> ignored
next_files() { local f; for f in tsconfig.json next-env.d.ts; do printf '%s\n' "$f"; [ -n "$app_sub" ] && [ "$app_sub" != . ] && printf '%s\n' "$app_sub/$f"; done; return 0; }
say "합치기 시작 — $app/$name (기본 가지 $base, land-id $id${retry:+, 재시도}${dry:+$( [ "$dry" = 1 ] && echo ', dry')}${self_deploy:+, 자체 배포 앱})"

# Evidence files a range brings (gaps 115/117, harness probe_trust): a facts.md still saying 확인 필요, or a probe/*.log
# without PROBE OK + response evidence + a date within WT_PROBE_MAX_AGE_H (24) hours, stops the land with exit 12.
# Read from the commit itself. Used by a normal land (copy fork..checked) and by --retry (the recorded old..new).
evidence_check() {  # $1 repo, $2 from, $3 to
  local ev_bad=0 ev_n=0 evtmp f kind ev_rc out
  evtmp=$(mktemp)
  while IFS= read -r -d '' f; do
    case "$f" in
      facts.md|*/facts.md) kind=facts ;;
      probe/*.log|*/probe/*.log) kind=probe ;;
      *) continue ;;
    esac
    ev_n=$((ev_n + 1))
    wt_git -C "$1" show "$3:$f" > "$evtmp" 2>/dev/null || continue
    ev_rc=0
    if [ "$kind" = facts ]; then out=$(python3 "$WT_SHARED/suite-check.py" facts "$evtmp") || ev_rc=$?
    else out=$(python3 "$WT_SHARED/suite-check.py" probe "$evtmp" "${WT_PROBE_MAX_AGE_H:-24}") || ev_rc=$?; fi
    case "$ev_rc" in
      0) say "3c) $f 통과" ;;
      3) say "3c) $f — 검사 도구(harness) 없음, 건너뜀" ;;
      *) ev_bad=1; say "3c) $f 불통과: $out" ;;
    esac
  done < <(wt_git -C "$1" diff --name-only -z --no-renames --diff-filter=AMT "$2" "$3")
  rm -f "$evtmp"
  [ "$ev_bad" = 0 ] || stop 12 "사실 확인(facts.md) 또는 실측 기록(probe/*.log)이 끝나지 않았습니다(위 목록)."
  [ "$ev_n" = 0 ] || say "3c) 증거 파일 ${ev_n}개 확인"
  return 0
}

restart_workers() {  # every enabled ${WT_UNIT_PREFIX:-ifapp}-<app>-*.service except the web unit, by unit name
  local web u bad=0 units
  web=$(bash -c '. "$1" >/dev/null 2>&1; printf "%s" "${UNIT:-}"' _ "$src/deploy.env" 2>/dev/null || true); web="${web:-${WT_UNIT_PREFIX:-ifapp}-$app}"
  # a failed listing is a failure, never "no workers" (Codex 11-02)
  units=$(systemctl --user list-units --type=service --all --no-legend --plain "${WT_UNIT_PREFIX:-ifapp}-$app-*.service" 2>/dev/null) || return 1
  for u in $(awk '{print $1}' <<< "$units"); do
    [ "$u" = "$web.service" ] && continue
    [ "$(systemctl --user is-enabled "$u" 2>/dev/null || true)" = disabled ] && continue
    if systemctl --user restart "$u"; then say "워커 재시작 — $u"; else say "워커 재시작 실패 — $u"; bad=1; fi
  done
  return "$bad"
}

# 1) land lock
exec 5>"$WT_DATA/$app/land.lock"
flock -w "${WT_LAND_WAIT:-600}" 5 || { say "다른 합치기가 진행 중입니다($WT_DATA/$app/land.lock) — 나중에 다시."; exit 6; }
say "1) 합치기 잠금 잡음 — $WT_DATA/$app/land.lock"
# 1a) a land whose deploy ended in an unknown state blocks the next one until a person has looked (gap 183): its revert
# or rollback would act on a state nobody knows. Clearing it = deleting the land-unknown-*.json after checking.
# Also any deploy receipt of this app still "started" by a process that is gone, or "unknown" and not acknowledged: the
# land that started it may have died with it (reboot, killed process group) and never written land-unknown (Codex 11-02).
receipts_dir="${DEPLOY_STATE_ROOT:-${WT_DEPLOY_STATE}}/$app.receipts"
unknown_receipts() { [ -d "$receipts_dir" ] && python3 "$WT_SHARED/receipts.py" blocking "$receipts_dir" || true; }
if [ "$ack" = 1 ]; then
  [ "$dry" = 1 ] && WT_EXIT=2 wt_die "--ack-unknown/--resolve 와 --dry 는 함께 쓰지 않습니다"
  if [ "$resolution" = reverted ]; then
    # the old build runs: bring the source back to it too. Only from a recorded range (land-unknown-<id>.json), on the
    # default branch, in a clean folder — anything else is a person's job with git, then --resolve kept (Codex 11-02).
    [ "$(wt_git -C "$src" rev-parse --abbrev-ref HEAD)" = "$base" ] || WT_EXIT=4 wt_die "앱 폴더가 기본 가지($base)가 아닙니다 — 되돌리지 않습니다."
    # anything staged is a person's: never restored or committed here (Codex 11-02)
    wt_git -C "$src" diff --cached --quiet || WT_EXIT=4 wt_die "앱 폴더에 스테이징된 변경이 있습니다 — 되돌리지 않습니다."
    # a deploy that died mid-build leaves next's own rewrites behind: put those two back first (tracked ones only)
    for f in $(next_files); do
      if wt_git -C "$src" ls-files --error-unmatch -- "$f" >/dev/null 2>&1 && ! wt_git -C "$src" diff --quiet -- "$f"; then
        wt_git -C "$src" checkout HEAD -- "$f"; say "next 자동 변경 복원: $f"
      fi
    done
    [ -z "$(wt_git -C "$src" status --porcelain)" ] || WT_EXIT=4 wt_die "앱 폴더에 커밋 안 된 변경이 있습니다 — 되돌리지 않습니다(그 변경까지 커밋될 수 있음)."
    ls "$WT_DATA/$app"/land-unknown-*.json >/dev/null 2>&1 \
      || WT_EXIT=4 wt_die "되돌릴 범위 기록(land-unknown-*.json)이 없습니다(wt-land도 함께 멈춘 경우) — 손으로 되돌린 뒤 --resolve kept."
    rg_app=$(bash -c '. "$1" >/dev/null 2>&1; printf "%s" "${REVIEW_GATE:-0}"' _ "$src/deploy.env" 2>/dev/null || echo 0)
    for u in "$WT_DATA/$app"/land-unknown-*.json; do
      [ -f "$u" ] || continue
      u_old=$(wt_json_get "$u" old); u_new=$(wt_json_get "$u" new); u_id=$(wt_json_get "$u" id)
      # already reverted by an earlier --resolve run (its worker restart failed): never revert twice
      if [ -n "$(wt_json_get "$u" source_reverted)" ]; then say "소스는 이미 되돌림 — $u_id ($(wt_json_get "$u" source_reverted | cut -c1-9))"; continue; fi
      # ship.sh's own fix(auto) commits after the land belong to it too (newest first)
      u_ours=$( { [ "$rg_app" = 1 ] && wt_git -C "$src" rev-list --grep='^fix(auto):' "$u_new..HEAD"; wt_git -C "$src" rev-list "$u_old..$u_new"; } )
      # shellcheck disable=SC2086
      if [ -n "$u_ours" ] && wt_git -C "$src" revert --no-commit $u_ours && wt_git -C "$src" commit -q --allow-empty \
           -m "revert: land $u_id 결과 모름 → 사람이 '이전 빌드'로 확정 (wt-land --resolve reverted)" -m "Landed-By: wt-land"; then
        wt_json_set "$u" source_reverted="$(wt_git -C "$src" rev-parse HEAD)"
        say "소스 되돌림 $(wt_git -C "$src" rev-parse --short HEAD) — $u_id"
      else
        wt_git -C "$src" revert --abort >/dev/null 2>&1 || true
        WT_EXIT=4 wt_die "되돌림 충돌 — $u_id($u_old..$u_new)를 손으로 되돌린 뒤 --resolve kept 로 표시하세요."
      fi
    done
  fi
  if [ "$resolution" = reverted ]; then
    # the source is back on the old code: every worker must run it before the block is cleared (Codex 11-02)
    restart_workers || WT_EXIT=5 wt_die "워커 재시작 실패 — 결과 모름 표시는 그대로 둡니다. 워커를 확인한 뒤 다시 --resolve reverted."
  fi
  for r in $(unknown_receipts); do
    python3 "$WT_SHARED/receipts.py" ack "$r"
    [ -n "$resolution" ] && python3 "$WT_SHARED/receipts.py" set "$r" resolution="$resolution"
    say "확인됨으로 표시: $r${resolution:+ ($resolution)}"
  done
  for u in "$WT_DATA/$app"/land-unknown-*.json; do [ -f "$u" ] && mv "$u" "$u.acked" && say "확인됨으로 표시: $u"; done
  say "결과 모름 표시를 지웠습니다 — 합치기를 다시 실행하세요."; exit 0
fi
unk=$( { ls "$WT_DATA/$app"/land-unknown-*.json 2>/dev/null || true; unknown_receipts; } )
unk=$(sed -n 1,3p <<< "$unk")   # cut after collecting: head on the pipe could kill the producer (SIGPIPE)
[ -z "$unk" ] || stop 10 "이전 배포 결과를 모릅니다: $(printf '%s' "$unk" | tr '\n' ' ')— 운영을 확인한 뒤 wt-land.sh $app $name --ack-unknown"

# 1b) REVIEW_GATE apps deploy through ship.sh; another ship run would mix its fix(auto) commits into this land
review_gate=$(bash -c '. "$1" >/dev/null 2>&1; printf "%s" "${REVIEW_GATE:-0}"' _ "$src/deploy.env" 2>/dev/null || echo 0)
if [ "$review_gate" = 1 ]; then
  ship_lock="$WT_DEPLOY_STATE/$app.review/ship.lock"
  if [ -e "$ship_lock" ] && ! flock -n "$ship_lock" true; then stop 6 "이 앱의 ship.sh가 이미 돌고 있습니다 — 끝난 뒤 다시 합칩니다."; fi
fi

# 2) app folder clean
cur=$(wt_git -C "$src" rev-parse --abbrev-ref HEAD)
[ "$cur" = "$base" ] || stop 4 "앱 폴더가 기본 가지($base)가 아니라 $cur 에 있습니다."
# next build/dev rewrites files in the app folder (first observed: " M next-env.d.ts" stopped a land unexpectedly). Unstaged
# changes to tsconfig.json / next-env.d.ts are restored from HEAD. CLAUDE.md / AGENTS.md are restored only when the
# change is nothing but Next's <!-- BEGIN:nextjs-agent-rules --> ... <!-- END:nextjs-agent-rules --> block; any other
# edit there may be a person's and stops the land, as does anything else or a staged change.
next_block_only() {  # $1 repo, $2 file -> 0 when HEAD and the working file differ only inside Next's agent-rules block
  python3 - "$1" "$2" <<'PY'
import re, subprocess, sys
repo, f = sys.argv[1], sys.argv[2]
head = subprocess.run(["git", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", "-C", repo, "show", f"HEAD:{f}"], capture_output=True, text=True).stdout
work = open(f"{repo}/{f}", encoding="utf-8", errors="replace").read()
# only the block and the blank lines around it are dropped; everything else, whitespace included, must match exactly
block = re.compile(r"\n*<!-- BEGIN:nextjs-agent-rules -->.*?<!-- END:nextjs-agent-rules -->\n*", re.S)
norm = lambda t: block.sub("\n", t).rstrip("\n")
sys.exit(0 if norm(head) == norm(work) else 1)
PY
}
# root or any subfolder (someapp web/): these two names are only ever next's own rewrites, restored from HEAD, never committed
AUTO_RE='^ M ((.*/)?tsconfig\.json|(.*/)?next-env\.d\.ts)$'
dirty=$(wt_git -C "$src" status --porcelain)
auto=$(printf '%s\n' "$dirty" | grep -E "$AUTO_RE" | cut -c4- || true)
rest=$(printf '%s\n' "$dirty" | grep -vE "$AUTO_RE" | sed '/^$/d' || true)
for f in CLAUDE.md AGENTS.md; do
  if grep -qx " M $f" <<< "$rest" && next_block_only "$src" "$f"; then
    auto=$(printf '%s\n%s' "$auto" "$f" | sed '/^$/d')
    rest=$(printf '%s\n' "$rest" | grep -vx " M $f" || true)
  fi
done
if [ -n "$rest" ]; then
  say "2) 앱 폴더가 깨끗하지 않습니다:"; printf '%s\n' "$rest" | sed 's/^/     /' | tee -a "$log"
  stop 4 "앱 폴더에 커밋 안 된 변경이 있어 합치지 않습니다(위 목록)."
elif [ -n "$auto" ]; then
  # shellcheck disable=SC2086
  run wt_git -C "$src" checkout HEAD -- $auto
  say "2) 앱 폴더 — next 자동 변경만 있어 복원: $(printf '%s' "$auto" | tr '\n' ' ')"
else
  say "2) 앱 폴더 깨끗함"
fi

# 2b) the repository's shared .git/config must be what it was when the copy was made (gap 155/271): a copy's code can
# write it (fsmonitor, hooksPath, filters, aliases, remote URLs) and the app folder's next git command would honour it.
cfg_rec=$(wt_json_get "$wt/.wt.json" git_config_sig)
if [ -z "$cfg_rec" ]; then
  say "2b) git 설정 기준 없음(wt-new 이전 사본) — 비교 건너뜀"
elif [ "$cfg_rec" != "$(wt_git_config_sig "$src")" ]; then
  say "2b) 저장소 .git/config가 사본을 만든 뒤 바뀌었습니다. 바뀐 키(값은 숨김):"
  keydiff=$({ diff <(cat "$WT_DATA/$app/$name.gitconfig.keys" 2>/dev/null) <(wt_git_config_keys "$src") || true; } | grep -E '^[<>]' || true)
  if [ -n "$keydiff" ]; then sed 's/^/     /' <<< "$keydiff" | tee -a "$log"; else say "     (키는 같고 값만 바뀜)"; fi
  stop 4 "git 설정 변경 — 확인 전에는 합치지 않습니다(git config --file $(wt_git -C "$src" rev-parse --path-format=absolute --git-common-dir)/config --list)."
else
  say "2b) git 설정 그대로"
fi

if [ -z "$retry" ]; then
  # 3) copy: clean, rebased, inside its scope
  # staged changes are never next's own rewrites: refuse them before restoring anything (Codex 11-01)
  staged=$(wt_git -C "$wt" diff --cached --name-only)
  [ -z "$staged" ] || { say "사본에 스테이징된 변경:"; printf '%s\n' "$staged" | tee -a "$log"; stop 4 "사본에 스테이징된 변경이 있습니다 — 커밋하거나 되돌린 뒤 다시."; }
  # next's own rewrites: tsconfig.json / next-env.d.ts always; CLAUDE.md / AGENTS.md only when the change is nothing but
  # Next's agent-rules block — any other edit there is a person's and stops the land (Codex 11-02)
  copy_auto="$(next_files | tr '\n' ' ')"; copy_auto="${copy_auto% }"
  for f in CLAUDE.md AGENTS.md; do
    wt_git -C "$wt" diff --quiet -- "$f" 2>/dev/null || { next_block_only "$wt" "$f" && copy_auto="$copy_auto $f"; } || true
  done
  for f in $copy_auto; do
    wt_git -C "$wt" diff --quiet -- "$f" 2>/dev/null || run wt_git -C "$wt" checkout HEAD -- "$f"
  done
  wdirty=$(wt_git -C "$wt" diff --name-only | grep -vxF -f <(tr ' ' '\n' <<< "$copy_auto") || true)
  [ -z "$wdirty" ] || { say "사본에 커밋 안 된 변경:"; printf '%s\n' "$wdirty" | tee -a "$log"; stop 4 "사본에 커밋 안 된 추적 파일이 있습니다 — 커밋하거나 되돌린 뒤 다시."; }
  ahead=$(wt_git -C "$wt" rev-list --count "$base..HEAD")
  [ "$ahead" -gt 0 ] || stop 3 "합칠 커밋이 없습니다($branch 가 $base 보다 앞선 커밋 0)."
  if wt_git -C "$wt" merge-base --is-ancestor "$base" HEAD; then
    say "3) 사본 — $base 위에 이미 있음, 커밋 ${ahead}개"
  elif [ "$dry" = 1 ]; then
    say "3) 사본 — $base 가 앞서 나감: (dry) 실행할 것: wt_git -C $wt rebase $base"
  else
    say "3) 사본 — git rebase $base"
    if ! wt_git -C "$wt" rebase "$base" >>"$log" 2>&1; then
      wt_git -C "$wt" rebase --abort >/dev/null 2>&1 || true
      stop 6 "rebase 충돌 — 사본에서 충돌을 풀고 다시 합치세요."
    fi
  fi
  # the commit that the checks see is pinned: only this exact commit is landed (Codex 11-02 — a commit added to the copy
  # while the audit ran would otherwise ride along unchecked)
  checked=$(wt_git -C "$wt" rev-parse HEAD)
  sc_rc=0; bash "$WT_SHARED/scope-check.sh" "$wt" 2>&1 | tee -a "$log" || sc_rc=${PIPESTATUS[0]}
  [ "$sc_rc" = 0 ] && say "3) 범위 검사 통과" || stop 2 "범위 검사 실패 — 허용 경로 밖 변경이 있습니다."
  # 3b) security: secrets in the added lines, new dependency advisories (gap 34)
  sg_rc=0; bash "$WT_SHARED/sec-gate.sh" "$wt" "$(wt_git -C "$wt" merge-base "$base" "$checked")" "$checked" --baseline "$src" 2>&1 | tee -a "$log" || sg_rc=${PIPESTATUS[0]}
  [ "$sg_rc" = 0 ] && say "3b) 보안 검사 통과" || stop 11 "보안 검사 실패 — 비밀값 또는 새 취약점(위 목록, 값은 출력하지 않음)."
  # 3c) evidence files this land brings (gaps 115/117)
  evidence_check "$wt" "$(wt_git -C "$wt" merge-base "$base" "$checked")" "$checked"

  # 3d) review-gate's exact-SHA receipt (2026-11-03, dori-superagent pattern): a Codex pass recorded for an older commit
  # in this copy must never wave a commit added afterward through unreviewed. review-gate.sh writes the exact sha it
  # cleared to <state>/reviewed.json; compare it to $checked and, on a mismatch, re-run review-gate on this copy's exact
  # HEAD before merging — automatically, with the live command review-gate itself cached last time (MASTER_RULES §1: no
  # approval wait). A dry run only reports what it would do.
  if [ "$review_gate" = 1 ]; then
    rg_state="${DEPLOY_STATE_ROOT:-${WT_DEPLOY_STATE}}/$app.review"
    reviewed_sha=$(wt_json_get "$rg_state/reviewed.json" sha 2>/dev/null || true)
    if [ "$reviewed_sha" = "$checked" ]; then
      say "3d) 점검 영수증 SHA 일치(${checked:0:9}) — 재점검 건너뜀"
    elif [ "$dry" = 1 ]; then
      say "3d) (dry) 점검 영수증 SHA 불일치(영수증 ${reviewed_sha:-없음}, 땅 대상 ${checked:0:9}) — 실제 실행이면 review-gate 재점검"
    else
      say "3d) 점검 영수증 SHA 불일치(영수증 ${reviewed_sha:-없음}, 땅 대상 ${checked:0:9}) — review-gate 재점검"
      live_cmd=$(cat "$rg_state/live-cmd" 2>/dev/null || true)
      if [ -n "$live_cmd" ]; then
        rg_rc=0; APP_DIR="$wt" bash "$WT_SHARED/review-gate.sh" "$app" --live "$live_cmd" 2>&1 | tee -a "$log" || rg_rc=${PIPESTATUS[0]}
        [ "$rg_rc" = 0 ] || stop 4 "재점검용 실데이터 실행이 실패했습니다 — 고친 뒤 다시 합치세요."
      fi
      rg_rc=0; APP_DIR="$wt" bash "$WT_SHARED/review-gate.sh" "$app" --check 2>&1 | tee -a "$log" || rg_rc=${PIPESTATUS[0]}
      [ "$rg_rc" = 0 ] || stop 4 "재점검 불통과 — 고친 뒤 다시 합치세요(위 Codex 지적 참고)."
      reviewed_sha=$(wt_json_get "$rg_state/reviewed.json" sha 2>/dev/null || true)
      [ "$reviewed_sha" = "$checked" ] || stop 4 "재점검 뒤에도 영수증 SHA가 맞지 않습니다 — 다시 합치세요."
      say "3d) 재점검 통과 — 새 영수증 SHA ${reviewed_sha:0:9}"
    fi
  fi

  # 4) land commit + fast-forward
  old=$(wt_git -C "$src" rev-parse HEAD)
  [ "$(wt_git -C "$wt" rev-parse HEAD)" = "$checked" ] || stop 6 "검사한 뒤 사본에 커밋이 더 생겼습니다 — 다시 합치면 처음부터 검사합니다."
  # The land commit is made from the checked commit's own tree, never from the copy's index: anything staged meanwhile
  # cannot ride along (Codex 11-02). The branch moves only if it still points at the checked commit.
  land_c="$branch"
  if [ "$dry" = 0 ]; then
    land_c=$(wt_git -C "$wt" commit-tree "$checked^{tree}" -p "$checked" -m "land: $name" -m "Landed-By: wt-land")
    wt_git -C "$wt" update-ref "refs/heads/$branch" "$land_c" "$checked" \
      || stop 6 "검사한 뒤 사본 가지가 움직였습니다 — 다시 합치면 처음부터 검사합니다."
    say "4) land 커밋 ${land_c:0:9} = 검사한 트리 그대로"
  else
    say "(dry) 실행할 것: commit-tree <검사한 트리> -p ${checked:0:9} → update-ref refs/heads/$branch (검사한 커밋일 때만)"
  fi
  run wt_git -C "$src" merge -q --ff-only "$land_c" || stop 6 "빨리감기 합치기 실패 — 앱 폴더의 $base 가 그 사이 움직였습니다. 다시 합치면 rebase부터 합니다."
  new=$(wt_git -C "$src" rev-parse HEAD)
  range_files=$(wt_git -C "$wt" diff --name-only "$old...$checked")
else
  [ -f "$failrec" ] || WT_EXIT=2 wt_die "재시도 기록이 없습니다: $failrec"
  revert_c=$(wt_json_get "$failrec" revert)
  [ -n "$revert_c" ] || WT_EXIT=2 wt_die "기록에 되돌림 커밋이 없습니다: $failrec"
  # what the retry re-applies is everything the recorded revert undid (the landed range plus ship.sh fix(auto) commits)
  sc_rc=0; bash "$WT_SHARED/scope-check.sh" "$wt" --range "$(wt_git -C "$src" rev-parse "$revert_c^")..$revert_c" 2>&1 | tee -a "$log" || sc_rc=${PIPESTATUS[0]}
  [ "$sc_rc" = 0 ] && say "3) 재시도 범위 검사 통과" || stop 2 "재시도 범위 검사 실패 — 허용 경로 밖 변경이 있습니다."
  # what the retry re-applies is the recorded range old..new (every commit is scanned). A retry that brings dependency
  # changes back is refused:
  # the audit must compare against today's app folder, which only a normal land (a fresh copy) does (Codex 11-02).
  retry_names=$(wt_git -C "$src" diff --name-only "$revert_c" "$revert_c^")
  if grep -qE '(^|/)(package\.json|package-lock\.json|npm-shrinkwrap\.json|requirements[^/]*\.txt|pyproject\.toml)$' <<< "$retry_names"; then
    stop 11 "의존성이 바뀌는 land는 재시도하지 않습니다 — 새 사본에서 다시 합치세요(감사를 지금 기준으로)."
  fi
  sg_rc=0; SEC_SKIP_AUDIT=1 bash "$WT_SHARED/sec-gate.sh" "$src" "$(wt_json_get "$failrec" old)" "$(wt_json_get "$failrec" new)" 2>&1 | tee -a "$log" || sg_rc=${PIPESTATUS[0]}
  [ "$sg_rc" = 0 ] && say "3b) 재시도 보안 검사 통과" || stop 11 "재시도 보안 검사 실패(위 목록)."
  evidence_check "$src" "$(wt_json_get "$failrec" old)" "$(wt_json_get "$failrec" new)"
  old=$(wt_git -C "$src" rev-parse HEAD)
  run wt_git -C "$src" revert --no-edit "$revert_c"
  new=$(wt_git -C "$src" rev-parse HEAD)
  range_files=$(wt_git -C "$src" diff --name-only "$(wt_json_get "$failrec" old)" "$(wt_json_get "$failrec" new)")
  say "3~4) 재시도 — 되돌림 $revert_c 을 다시 되돌림"
fi
say "4) 합친 범위 ${old:0:9}..${new:0:9} — 파일 $(printf '%s\n' "$range_files" | grep -c . || true)개"
# the exact tree that is deployed (after rebase / retry): recorded so the land can be matched to a build later
if [ "$dry" = 1 ]; then land_tree=$(wt_git -C "$wt" rev-parse 'HEAD^{tree}'); else land_tree=$(wt_git -C "$src" rev-parse 'HEAD^{tree}'); fi
say "4) 합칠 트리 $land_tree"
[ "$dry" = 1 ] || wt_json_set "$wt/.wt.json" land_tree="$land_tree" land_commit="$new"

# 5) deploy (migrations first, inside deploy.sh's pre-swap point)
pre=""
if grep -qE "$WT_MIG_RE" <<< "$range_files"; then   # herestring: a pipe into grep -q can die of SIGPIPE on a long list
  pre="bash $WT_SHARED/wt-migrate.sh $app $id"
  say "5) 마이그레이션 있음 — PRE_SWAP_CMD=\"$pre\" (백업 → migrate)"
fi
if [ "$review_gate" = 1 ]; then
  deploy=(bash "$WT_SHARED/ship.sh" "$app")
  # forward the live command 3d) already reviewed (cached by review-gate.sh): ship.sh then re-runs the exact same check
  # on the merged tree instead of asking Codex to write a new one from scratch (gap found 2026-11-03: review-gate.sh
  # cached this file since 11-01 but nothing ever read it back).
  rg_live=$(cat "${DEPLOY_STATE_ROOT:-${WT_DEPLOY_STATE}}/$app.review/live-cmd" 2>/dev/null || true)
  [ -n "$rg_live" ] && deploy+=("$rg_live")
else
  deploy=(bash "$WT_SHARED/deploy.sh" "$app")
fi
say "5) 배포 — ${deploy[*]}"
rc=0
if [ "$dry" = 1 ]; then
  say "(dry) 실행할 것: PRE_SWAP_CMD=${pre:-없음} ${deploy[*]}"
else
  for _ in $(seq 1 20); do
    expect="$land_tree"   # ship.sh moves it forward only after cherry-picking its own fix (Codex 11-02)
    rc=0; PRE_SWAP_CMD="$pre" DEPLOY_RECEIPT_ID="land-$id" DEPLOY_EXPECT_TREE="$expect" "${deploy[@]}" >>"$log" 2>&1 || rc=$?
    case "$rc" in 2|3|6) [ "$review_gate" = 1 ] && break; say "배포 대기(코드 $rc) — 1분 뒤 다시"; sleep 60 ;; *) break ;; esac
  done
  tail -5 "$log"
  # 5a) self-deploy apps forward straight into their own script (common deploy.sh's receipt/fingerprint checks never
  # run for them — gap: DEPLOY_CMD is an exec, not a call). Their own script already health-checks itself before
  # returning 0, but run the shared deploy-check.sh too (unit active + deploy.env HOST_CHECK/PORT, same as every other
  # app) as independent evidence; a check failure here means something is actually broken even though the app's own
  # script returned 0, so it is treated as a deploy failure (rc=5) and goes through the same revert-and-recover path.
  if [ -n "$self_deploy" ] && [ "$rc" = 0 ]; then
    if bash "$WT_SHARED/deploy-check.sh" "$app" >>"$log" 2>&1; then
      say "5a) 자체 배포 뒤 확인 통과 — 유닛 active, HOST_CHECK/포트 응답"
    else
      say "5a) 자체 배포 뒤 확인 실패 — 자체 배포 스크립트는 0을 반환했지만 응답이 없습니다. 배포 실패로 처리합니다."
      rc=5
    fi
  fi
fi

# 5b) the deploy's own receipt (gap 190): the newest one written for this land
receipt_file=""; receipt_status=""
if [ "$dry" = 0 ]; then
  receipt_file=$(ls -t "${DEPLOY_STATE_ROOT:-${WT_DEPLOY_STATE}}/$app.receipts"/land-"$id"-*.json 2>/dev/null | head -1 || true)
  [ -n "$receipt_file" ] && receipt_status=$(wt_json_get "$receipt_file" status)
  say "5b) 배포 영수증 ${receipt_file:-없음} — 상태 ${receipt_status:-없음}, 코드 $rc"
  # 5c) unknown (gap 183): killed by a signal, an exit code no deploy step uses, or a receipt still "started" — the
  # deploy may or may not have swapped. Nothing is reverted or rolled back automatically; the next land waits for a person.
  # any blocking receipt after the deploy counts, not only the newest one (ship.sh can run several attempts)
  blocking_now=$(unknown_receipts)
  if [ "$rc" -ge 128 ] || [ "$rc" -gt 11 ] || [ -n "$blocking_now" ] \
     || { [ "$rc" != 0 ] && { [ "$receipt_status" = started ] || [ "$receipt_status" = unknown ]; }; }; then
    unkf="$WT_DATA/$app/land-unknown-$id.json"
    python3 - "$unkf" "$id" "$app" "$name" "$old" "$new" "$rc" "${receipt_file:-}" <<'PY'
import json, sys, datetime
p, i, a, n, o, nw, rc, rcpt = sys.argv[1:]
json.dump({"id": i, "app": a, "name": n, "old": o, "new": nw, "deploy_rc": int(rc), "receipt": rcpt,
           "at": datetime.datetime.now().astimezone().isoformat()}, open(p, "w"), ensure_ascii=False)
PY
    say "배포 결과 모름(코드 $rc, 영수증 ${receipt_status:-없음}) — 되돌리지도 다시 하지도 않습니다. 운영을 확인한 뒤 $unkf 를 지우세요."
    exit 10
  fi
fi


# 6b) After a good deploy: when the app has .claude/skills/verify-<app>/ (launch.sh, doctor.sh, drive.mjs), run them in the
# landed work copy (its own port and slot test DB, never production): launch -> doctor -> one journey. The journey is
# deploy.env VERIFY_JOURNEY, else the first journey without arguments on the SKILL.md "Journeys:" line.
# A failure never rolls back (the deploy already passed deploy-check); it is recorded for the KakaoTalk report.
# Sets post_verify = passed | doctor_only | failed | skipped and post_evidence = evidence dir (or log).
post_verify_run() {
  local skill="$wt/.claude/skills/verify-$app" h journey ev out
  post_verify=skipped; post_evidence=""
  h="$skill"; [ -f "$h/launch.sh" ] || h="$skill/scripts"   # helpers at the skill root or under scripts/ (app-dependent)
  if [ ! -d "$WT_APPS/$app/.claude/skills/verify-$app" ] || [ ! -f "$h/launch.sh" ] || [ ! -f "$h/doctor.sh" ] || [ ! -f "$h/drive.mjs" ]; then
    say "6b) 배포 뒤 확인 — verify-$app 스킬 없음: deploy.sh 응답 확인만"; return 0
  fi
  post_evidence="$log"
  # check what was deployed: ship.sh may have added fix(auto) commits on the base branch after this copy's tip
  if [ "$(wt_git -C "$wt" rev-parse HEAD)" != "$(wt_git -C "$src" rev-parse HEAD)" ]; then
    wt_git -C "$wt" merge -q --ff-only "$(wt_git -C "$src" rev-parse HEAD)" >>"$log" 2>&1 \
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
  if [ "$post_verify" != failed ] && grep -q '"ok":true' <<< "$out"; then post_verify=passed; else post_verify=failed; fi
  say "6b) 배포 뒤 확인 $( [ "$post_verify" = passed ] && echo 통과 || echo 실패 ) — $journey, 증거 $post_evidence"
}

if [ "$rc" = 0 ]; then
  # 6) success
  if wt_git -C "$src" remote get-url origin >/dev/null 2>&1; then
    run wt_git -C "$src" push -q origin "$base" || { say "배포는 됐지만 push 실패 — 나중에 wt_git -C $src push origin $base"; rc=7; }
  else
    say "6) 원격 없음 — push 건너뜀"
  fi
  # Merge+deploy+push above already succeeded. From here on everything touches $wt itself (post-deploy check,
  # .wt.json bookkeeping, .wt.done) — but wt-gc.sh can run concurrently (AX OS start) and, the instant this copy's
  # branch looks merged+clean, remove the worktree directory out from under this very script (team-lead 2026-11-02,
  # 실측: an app's wiring copy — wt_json_set died on ".wt.json.tmp" FileNotFoundError between step 6's tests and
  # step 7's write). wt-gc.sh now skips an app while its land.lock is held, which should prevent this going forward;
  # this is the fallback for the remaining race window. There is nothing left to roll back — the land already
  # happened — so a missing copy here is "already cleaned up", not a failure: log it and finish with exit 0.
  if [ "$dry" = 0 ] && [ ! -d "$wt" ]; then
    say "6) 사본 폴더가 이미 없습니다(gc가 합쳐짐을 확인하고 정리한 것으로 보임) — 배포·push는 끝났으니 영수증만 남기고 끝냅니다."
    say "합치기 끝 — $app/$name ${new:0:9}${retry:+ (재시도)} (사본은 이미 정리됨)"
    exit "$rc"
  fi
  if [ "$dry" = 0 ]; then
    bash "$WT_SHARED/wt-stop.sh" "$wt" >>"$log" 2>&1 || true
    post_verify_run
    bash "$WT_SHARED/wt-stop.sh" "$wt" >>"$log" 2>&1 || true
    # tests the deploy ran on this tree (deploy.sh quality gate + suite_trust), next to the tree hash (gaps 42/247)
    if [ -n "$receipt_file" ]; then
      say "6) 시험 $(wt_json_get "$receipt_file" tests_count)개($(wt_json_get "$receipt_file" tests_runner)) — 배포 트리 $(wt_json_get "$receipt_file" tree | cut -d+ -f1)"
    fi
    if [ ! -d "$wt" ]; then   # same race, now hit mid-step (post_verify_run can take a while) — same resolution
      say "6) 사본 폴더가 중간에 없어졌습니다(gc 동시 실행으로 보임) — 배포·push는 끝났으니 영수증만 남기고 끝냅니다."
      say "합치기 끝 — $app/$name ${new:0:9}${retry:+ (재시도)} (사본은 이미 정리됨)"
      exit "$rc"
    fi
    # what was really deployed: ship.sh may have added fix(auto) commits after the land commit
    wt_json_set "$wt/.wt.json" land_id="$id" landed_at="$(date -Iseconds)" post_verify="$post_verify" post_evidence="${post_evidence:-null}" \
      land_commit="$(wt_git -C "$src" rev-parse HEAD)" land_tree="$(wt_git -C "$src" rev-parse 'HEAD^{tree}')" \
      deploy_receipt="${receipt_file:-null}"
    say "6) 배포된 커밋 $(wt_git -C "$src" rev-parse --short HEAD), 트리 $(wt_git -C "$src" rev-parse 'HEAD^{tree}')"
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
ours=$( { [ "$review_gate" = 1 ] && [ "$rc" != 2 ] && wt_git -C "$src" rev-list --grep='^fix(auto):' "$new..HEAD"; wt_git -C "$src" rev-list "$old..$new"; } )
head_now=$(wt_git -C "$src" rev-parse HEAD)
say "배포 실패(코드 $rc) — 되돌립니다. 기록 $failrec"
python3 - "$failrec" "$id" "$app" "$name" "$old" "$new" "$head_now" "$rc" <<'PY'
import json, sys, datetime
p, i, a, n, o, nw, hd, rc = sys.argv[1:]
json.dump({"id": i, "app": a, "name": n, "old": o, "new": nw, "head_after_deploy": hd, "deploy_rc": int(rc),
           "at": datetime.datetime.now().astimezone().isoformat()}, open(p, "w"), ensure_ascii=False)
PY
# shellcheck disable=SC2086
if [ -n "$ours" ] && wt_git -C "$src" revert --no-commit $ours && wt_git -C "$src" commit -q --allow-empty -m "revert: land $id 배포 실패(코드 $rc) — wt-land 자동 되돌림" -m "Landed-By: wt-land"; then
  wt_json_set "$failrec" revert="$(wt_git -C "$src" rev-parse HEAD)"
  say "소스 되돌림 커밋 $(wt_git -C "$src" rev-parse --short HEAD) — 커밋 $(printf '%s\n' "$ours" | grep -c .)개"
else
  wt_git -C "$src" revert --abort >/dev/null 2>&1 || true
  say "소스 되돌림 실패 — 반장이 직접 되돌려야 합니다: $(printf '%s\n' "$ours" | cut -c1-9 | tr '\n' ' ')"
fi
if [ -n "$self_deploy" ]; then
  # Self-deploy apps: the source above is now reverted to the old code (or revert failed and the source is still
  # whatever it was — in that case the redeploy below rebuilds the *new* code again and must fail the same way, which
  # is still safer than guessing). Recovery = run the app's own DEPLOY_CMD once more, exactly what each one's own
  # script already does by hand on a bad build (some apps: pre-build kept, swap only on success, so a
  # rebuild of the reverted source is just another normal deploy of theirs; one app's own comment says the same:
  # "이전 커밋으로 checkout 뒤 다시 deploy"). If this second run also fails, nothing more is tried automatically —
  # wt-land cannot tell apart from here whether the running process is the old code, the bad new code, or mid-restart,
  # so a person has to look (notify-owner) before anything else lands on this app.
  say "자체 배포 앱 — 되돌린 소스로 다시 배포: ${deploy[*]}"
  rc2=0; "${deploy[@]}" >>"$log" 2>&1 || rc2=$?
  if [ "$rc2" = 0 ] && bash "$WT_SHARED/deploy-check.sh" "$app" >>"$log" 2>&1; then
    say "복구 배포 통과(코드 $rc2) — 유닛 active, 응답 확인됨. 이전 코드로 돌아왔습니다."
    restart_workers || true
  else
    unkf="$WT_DATA/$app/land-unknown-$id.json"
    python3 - "$unkf" "$id" "$app" "$name" "$old" "$new" "$rc" "$rc2" <<'PY'
import json, sys, datetime
p, i, a, n, o, nw, rc, rc2 = sys.argv[1:]
json.dump({"id": i, "app": a, "name": n, "old": o, "new": nw, "deploy_rc": int(rc), "self_deploy_recover_rc": int(rc2),
           "at": datetime.datetime.now().astimezone().isoformat()}, open(p, "w"), ensure_ascii=False)
PY
    say "복구 배포도 실패/응답 없음(코드 $rc2) — 더 되돌리지 않습니다. 사람이 운영을 확인해야 합니다: $unkf"
    # WT_NOTIFY=0: test seam (same as ship.sh's SHIP_NOTIFY) — the self-deploy test (sdstuck/w1) paged the owner
    # 22 times on 2026-11-02 through this line.
    if [ "${WT_NOTIFY:-1}" = 0 ]; then say "(WT_NOTIFY=0) 알림 생략"; else
    timeout 20 node "$WT_SHARED/notify-owner.mjs" "$app 앱 배포가 실패했고 되돌리기도 실패했습니다(작업 '$name'). 지금 운영 사이트가 옛 버전인지 새 버전인지 알 수 없는 상태입니다. 대표가 할 일: 사이트가 정상으로 보이는지 한 번 열어 보시고 이상하면 반장에게 알려 주세요. 반장이 운영을 확인한 뒤 정리합니다." "/" "시스템" >>"$log" 2>&1 \
      || say "notify-owner 알림 실패 — 그래도 $log 를 사람이 봐야 합니다"
    fi
    say "운영을 확인한 뒤: wt-land.sh $app $name --ack-unknown"
    exit 10
  fi
else
  if [ "$review_gate" = 1 ]; then rc_kind=ship; else rc_kind="$rc"; fi
  case "$rc_kind" in
    ship) say "ship.sh 경로 — deploy.sh가 실패 때마다 이전 빌드를 지킵니다. 워커만 되돌린 소스로 다시 띄웁니다."; restart_workers || true ;;
    1|2|3|4|6|8|9|10) say "도는 빌드는 바뀌지 않았습니다(배포가 교체 전에 멈춤, 코드 $rc)." ;;
    5|7) say "deploy.sh가 이미 이전 빌드로 되돌렸습니다 — 워커만 되돌린 소스로 다시 띄웁니다."; restart_workers || true ;;
    *) say "배포가 어디서 멈췄는지 모름(코드 $rc) — deploy.sh --rollback"; bash "$WT_SHARED/deploy.sh" "$app" --rollback >>"$log" 2>&1 || say "되돌림(--rollback) 실패 — 반장 확인" ;;
  esac
  if bash "$WT_SHARED/deploy-check.sh" "$app" >>"$log" 2>&1; then say "응답 확인 통과 — 이전 상태로 돌아왔습니다."; else say "응답 확인 실패 — 반장 확인 필요(기록 $log)"; fi
fi
say "재시도: wt-land.sh $app $name --retry $id"
exit 5
