#!/usr/bin/env bash
# Generated from private source at a8eb56b1bac538511499bb39b350117e5eead6e1; do not edit by hand (see harness/export_v3_public.py).
# Common deploy for one Next.js app.
# Order is forced: busy check -> memory check -> quality gate -> build in a separate folder -> swap only on success
# -> restart -> deploy-check -> automatic rollback if the check fails.
# usage: deploy.sh <app> [--check | --rollback | --dry-run]
#   --dry-run: the read-only checks (busy, memory, lock) plus every command it would run, printed; nothing built or swapped.
#   Every real deploy writes a receipt ${WT_DEPLOY_STATE}/<app>.receipts/<id>.json (atomic): status started -> passed |
#   failed (exit code, step timings, the tree checked and the tree built). One still "started" after its process is gone is
#   marked "unknown" by the next deploy; wt-land treats unknown as "do not touch, a person checks" (gaps 183/190/156).
#   The tree is fingerprinted before the checks and again after the build (git tree of the working folder + the content of
#   every file: a shared internal package); a difference, or one from DEPLOY_EXPECT_TREE (wt-land: the merged
#   tree), stops with exit 9 before the running build is touched (gaps 40/161).
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
# a mistyped mode (--chek) must never fall through to a real deploy (Codex 11-02)
case "$mode" in ""|--check|--rollback|--dry-run) ;; *) echo "모르는 모드: $mode (--check | --rollback | --dry-run)"; exit 2 ;; esac
# Test seams (scratch tree + fake deployer); every default is the live value.
APPS_ROOT="${DEPLOY_APPS_ROOT:-${WT_APPS_ROOT}}"; STATE_ROOT="${DEPLOY_STATE_ROOT:-${WT_DEPLOY_STATE}}"
SYSTEMCTL="${DEPLOY_SYSTEMCTL:-systemctl}"
dgit() { git -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.quotePath=false "$@"; }   # repo config cannot run code here (gap 69)
dir="$APPS_ROOT/$app"; envf="$dir/deploy.env"
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
  case "$mode" in --rollback) sub=rollback; cmd="${DEPLOY_CMD_ROLLBACK:-}" ;; --check) sub=status; cmd="${DEPLOY_CMD_CHECK:-}" ;; ""|--dry-run) sub=deploy; cmd="${DEPLOY_CMD_DEPLOY:-}" ;; *) echo "모르는 모드: $mode"; exit 2 ;; esac
  [ -n "$cmd" ] || cmd="${DEPLOY_CMD:-} $sub"
  # the command itself is never printed: deploy.env may put a token or DB credential inline (Codex 11-02)
  if [ "$mode" = --dry-run ]; then echo "(dry-run) 앱 자체 배포 명령으로 넘김($APPS_ROOT/$app 에서, 내용 숨김 ${#cmd}자)"; exit 0; fi
  # The app's own command is relative to the app folder (scripts/deploy.sh) — run it from there, not from the caller's folder
  # (2026-11-01: called from another folder it failed with "scripts/deploy.sh: No such file").
  echo "앱 자체 배포로 넘김(내용 숨김 ${#cmd}자)"; cd "$APPS_ROOT/$app" && exec bash -c "$cmd"
fi
UNIT="${UNIT:-${WT_UNIT_PREFIX:-ifapp}-$app}"; BUILD_MEM="${BUILD_MEM:-6G}"; NODE_HEAP="${NODE_HEAP:-4096}"; QUALITY_GATE="${QUALITY_GATE:-1}"; BUSY_PATTERN="${BUSY_PATTERN:-codex|gti|claude|python}"
# Memory-capped build wrapper (adapter, optional): WT_HEAVY_CMD "--mem <cap> --name <n> -- <cmd...>".
# Unset: the build command below runs directly, without a memory cap (warning printed).
HEAVY="${WT_HEAVY_CMD:-}"
SHARED="$(cd "$(dirname "$0")" && pwd)"
CHECK="${DEPLOY_CHECK:-$SHARED/deploy-check.sh}"
PREV="$STATE_ROOT/$app"   # previous build kept outside the project (10-14: build tools scan the whole tree)
BUILD="$dir/.next-build"
cd "$dir"
mkdir -p "$(dirname "$PREV")"

# next rewrites these two during a build; put the committed versions back one by one, and only a tracked one: a single
# `git checkout -- next-env.d.ts tsconfig.json` fails as a whole when next-env.d.ts is untracked (one app ignores
# it), which left tsconfig.json modified after every deploy (11-02).
restore_next_files() { local f; for f in tsconfig.json next-env.d.ts; do dgit ls-files --error-unmatch -- "$f" >/dev/null 2>&1 && dgit checkout -- "$f" 2>/dev/null; done; return 0; }

# Fingerprint of what is deployed: the working folder's git tree (index copy + every non-ignored change; tsconfig.json /
# next-env.d.ts are rewritten by next itself and taken from the index) plus the content of each file: dependency.
fingerprint() {
  local idx tree deps
  idx=$(mktemp); cp "$(dgit rev-parse --path-format=absolute --git-path index)" "$idx" 2>/dev/null || : > "$idx"
  # add everything, then put next's own two files back to their HEAD version (or out, when HEAD has none): naming them
  # in an exclude pathspec fails outright when an app's .gitignore lists them (observed once)
  local pre f; pre=$(dgit rev-parse --show-prefix)
  if GIT_INDEX_FILE="$idx" dgit add -A -- . >/dev/null 2>&1; then
    for f in tsconfig.json next-env.d.ts; do
      if dgit cat-file -e "HEAD:$pre$f" 2>/dev/null; then GIT_INDEX_FILE="$idx" dgit reset -q HEAD -- "$f" >/dev/null 2>&1
      else GIT_INDEX_FILE="$idx" dgit rm -q --cached --ignore-unmatch -- "$f" >/dev/null 2>&1; fi
    done
    tree=$(GIT_INDEX_FILE="$idx" dgit write-tree) || tree="?"
  else tree="?"; fi
  rm -f "$idx"
  deps=$(node -e '
    const p = require("./package.json"); const all = { ...(p.dependencies || {}), ...(p.devDependencies || {}) };
    for (const v of Object.values(all)) if (typeof v === "string" && v.startsWith("file:")) console.log(v.slice(5));
  ' 2>/dev/null | sort | while read -r d; do
      [ -d "$d" ] && find "$d" -type f -not -path '*/node_modules/*' -not -path '*/.git/*' -print0 | sort -z | xargs -0 -r sha256sum | sed "s#  $d/#  #"
    done | sha256sum | cut -c1-16)
  printf '%s+%s' "$tree" "$deps"
}

if [ "$mode" = --dry-run ]; then
  echo "(dry-run) $app — 아무것도 빌드·교체·재시작하지 않습니다"
  if flock -n "$PREV.lock" true 2>/dev/null; then echo "(dry-run) 배포 잠금 비어 있음($PREV.lock)"; else echo "(dry-run) 배포 잠금 사용 중 — 실제 실행이면 exit 6"; fi
  echo "(dry-run) 여유 메모리 $(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)MB (4096MB 미만이면 exit 3)"
  echo "(dry-run) 지문 $(fingerprint)${DEPLOY_EXPECT_TREE:+ / 기대 트리 $DEPLOY_EXPECT_TREE}"
  [ "$QUALITY_GATE" = 1 ] && for s in ${GATE_SCRIPTS:-typecheck test}; do node -e "process.exit(require('./package.json').scripts?.['$s'] ? 0 : 1)" && echo "(dry-run) 검사: npm run -s $s"; done
  [ "${REVIEW_GATE:-0}" = 1 ] && echo "(dry-run) 점검: bash $SHARED/review-gate.sh $app --check"
  echo "(dry-run) 빌드: IF_BUILD_DIR=.next-build NODE_OPTIONS=--max-old-space-size=$NODE_HEAP bash $HEAVY --mem $BUILD_MEM --name $app -- npm run -s build"
  [ -n "${PRE_SWAP_CMD:-}" ] && echo "(dry-run) 교체 전 명령 있음(내용은 비밀값이 있을 수 있어 숨김, ${#PRE_SWAP_CMD}자)"
  echo "(dry-run) 교체: .next → $PREV/.next, .next-build → .next"
  echo "(dry-run) 재시작: $SYSTEMCTL --user restart $UNIT${WORKER_UNIT:+ $WORKER_UNIT}"
  echo "(dry-run) 확인: bash $CHECK $app (실패 시 이전 빌드로 되돌림, exit 5)"
  exit 0
fi

exec 9>"$PREV.lock"; flock -n 9 || { echo "배포 중단 — 같은 앱의 배포가 이미 진행 중입니다($PREV.lock)."; exit 6; }   # Codex P1: no concurrent deploys

# Receipt (gaps 190/156/183): written before anything runs, rewritten atomically after each step, final status by the EXIT trap.
RECEIPTS="$PREV.receipts"; mkdir -p "$RECEIPTS"
receipt_id="${DEPLOY_RECEIPT_ID:-manual}-$(date +%Y%m%d-%H%M%S)-$$"
RECEIPT="$RECEIPTS/$receipt_id.json"
receipt() {  # key=value ... ; "step:<name>=<seconds>" goes into steps{} (receipts.py, atomic)
  python3 "$SHARED/receipts.py" set "$RECEIPT" "$@"
}
# an earlier receipt left "started" by a process that is gone (same boot and start time, not just the pid): unknown
python3 "$SHARED/receipts.py" mark-stale "$RECEIPTS" || true
# an earlier deploy with an unknown outcome blocks a new one before anything is touched (Codex 11-02): the person who
# checked production clears it (wt-land.sh <app> <copy> --ack-unknown, or receipts.py ack <file>). --check/--rollback pass.
if [ -z "$mode" ]; then
  blockers=$(python3 "$SHARED/receipts.py" blocking "$RECEIPTS" || true)
  if [ -n "$blockers" ]; then echo "배포 중단 — 이전 배포 결과를 모릅니다(확인 전): $(sed -n 1,3p <<< "$blockers" | tr '\n' ' ')"; exit 10; fi
fi
step_t=$(date +%s.%N)
step_done() { local now; now=$(date +%s.%N); receipt "step:$1=$(awk -v a="$step_t" -v b="$now" 'BEGIN{printf "%.1f", b-a}')"; step_t=$now; }
# keep the newest 50 receipts per app; an unacknowledged unknown is never pruned (it is what blocks the next land)
python3 "$SHARED/receipts.py" prune "$RECEIPTS" 50 || true
read -r boot_id pid_start < <(python3 "$SHARED/receipts.py" self "$$")
receipt id="$receipt_id" app="$app" mode="${mode:-deploy}" pid="$$" pid_start="$pid_start" boot_id="$boot_id" \
  host="$(hostname)" started_at="$(date -Iseconds)" status=started \
  head="$(dgit rev-parse HEAD 2>/dev/null || echo ?)" ${DEPLOY_EXPECT_TREE:+expect_tree="$DEPLOY_EXPECT_TREE"}
# A signal (TERM/INT/HUP) can land at any step, the swap included: the outcome is unknown, never "passed" (bash runs the
# EXIT trap with $? = 0 after a trapped TERM — Codex 11-02). Exit 128+n so callers see a signal, not a step's code.
on_signal() { sig_seen=1; receipt status=unknown note="signal $1" rc="$((128 + $2))" finished_at="$(date -Iseconds)" 2>/dev/null || true; exit $((128 + $2)); }
trap 'on_signal TERM 15' TERM; trap 'on_signal INT 2' INT; trap 'on_signal HUP 1' HUP
# Once the swap has started, only success (0) or a completed swap-back (5, 7) is a known outcome; any other exit — a
# failed swap-back, a systemctl error under set -e — leaves new build and source in an unknown mix (Codex 11-02).
final_status() {
  if [ "$1" = 0 ]; then echo passed
  elif [ -n "${swapped:-}" ] && { [ -n "${swapback_failed:-}" ] || { [ "$1" != 5 ] && [ "$1" != 7 ]; }; }; then echo unknown
  else echo failed; fi
}
trap 'rc=$?; [ -n "${sig_seen:-}" ] || receipt status="$(final_status "$rc")" rc="$rc" finished_at="$(date -Iseconds)" 2>/dev/null || true
      if [ "$rc" = 0 ] && [ -n "${tests_pending:-}" ]; then python3 "$SHARED/suite-check.py" record "$STATE_ROOT/$app.test-counts.json" "npm run test" "$tests_pending" 2>/dev/null || true; fi' EXIT

swap_back() {  # rollback: put the previous build back and restart
  if [ -d "$PREV/.next" ]; then
    swapped=1; receipt swapped=1 2>/dev/null || true   # files move from here: a half-done rollback is an unknown state
    # every file step is checked here: callers use `swap_back || …`, which switches set -e off inside (Codex 11-02)
    if [ -d .next ]; then
      if [ -d "$PREV/.next-failed" ]; then find "$PREV/.next-failed" -depth -delete || { echo "되돌림 실패 — $PREV/.next-failed 정리 실패" >&2; return 1; }; fi
      mv .next "$PREV/.next-failed" || { echo "되돌림 실패 — 새 빌드를 치우지 못함" >&2; return 1; }
    fi
    mv "$PREV/.next" .next || { echo "되돌림 실패 — 이전 빌드를 되돌려 놓지 못함" >&2; return 1; }
    local ok=0
    $SYSTEMCTL --user restart "$UNIT" || ok=1
    [ -n "${WORKER_UNIT:-}" ] && { $SYSTEMCTL --user restart "$WORKER_UNIT" || ok=1; }   # worker follows the web (same checkout)
    sleep 5
    [ "$($SYSTEMCTL --user is-active "$UNIT" 2>/dev/null)" = active ] || ok=1
    if [ "$ok" = 0 ]; then echo "되돌림 — 이전 빌드로 재시작했습니다($PREV/.next). 소스 되돌림은 git에서."
    else echo "되돌림 실패 — 이전 빌드로도 $UNIT 이(가) 뜨지 않습니다. journalctl --user -u $UNIT 확인." >&2; return 1; fi
  else
    echo "되돌릴 이전 빌드가 없습니다($PREV/.next)." >&2; return 1
  fi
}

# 1) busy: never restart while the web or worker unit has generation children (09-24 incident; cgroup-wide, pgrep -P fallback)
busy_procs() {  # prints matching processes of one unit, empty when idle
  local unit="$1" cgroup main_pid out="" pid comm args
  cgroup=$($SYSTEMCTL --user show "$unit" -p ControlGroup --value 2>/dev/null || true)
  main_pid=$($SYSTEMCTL --user show "$unit" -p MainPID --value 2>/dev/null || echo 0)
  if [ -n "$cgroup" ] && [ -r "/sys/fs/cgroup$cgroup/cgroup.procs" ]; then
    while read -r pid; do
      { [ -z "$pid" ] || [ "$pid" = "$main_pid" ]; } && continue
      comm=$(cat "/proc/$pid/comm" 2>/dev/null || true); args=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | cut -c1-120 || true)
      # matched on the arguments, printed without them: a job's argv can carry a key (--api-key=…) (Codex 11-02)
      if grep -qE "$BUSY_PATTERN" <<< "$comm $args"; then out="${out}${unit} ${pid} ${comm}"$'\n'; fi
    done < "/sys/fs/cgroup$cgroup/cgroup.procs"
  elif [ "${main_pid:-0}" != 0 ]; then
    out=$(pgrep -P "$main_pid" -a 2>/dev/null | grep -E "$BUSY_PATTERN" | awk '{print $1, $2}' || true)   # pid + program, no arguments
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
  for u in $($SYSTEMCTL --user list-units --type=service --all --no-legend --plain "${WT_UNIT_PREFIX:-ifapp}-$app-*.service" 2>/dev/null | awk '{print $1}'); do
    { [ "$u" = "$UNIT.service" ] || [ "$u" = "${WORKER_UNIT:-none}.service" ] || [ "$u" = "${WORKER_UNIT:-none}" ]; } && continue
    [ "$($SYSTEMCTL --user is-enabled "$u" 2>/dev/null || true)" = disabled ] && continue
    $SYSTEMCTL --user restart "$u" && echo "워커 재시작 — $u" || { echo "워커 재시작 실패 — $u" >&2; rb=1; }
  done
  exit "$rb"
fi
if [ "$mode" = --check ]; then echo "생성 작업 없음 — 배포해도 됩니다."; exit 0; fi

# 1b) what is about to be checked and built
fp_before=$(fingerprint); receipt tree="$fp_before"
[ "${fp_before%%+*}" != "?" ] || { echo "배포 중단 — 앱 폴더 트리 지문을 만들지 못했습니다(git add/write-tree 실패). 도는 앱은 그대로입니다."; exit 9; }
if [ -n "${DEPLOY_EXPECT_TREE:-}" ] && [ "${fp_before%%+*}" != "$DEPLOY_EXPECT_TREE" ]; then
  echo "배포 중단 — 앱 폴더 트리(${fp_before%%+*})가 합친 트리($DEPLOY_EXPECT_TREE)와 다릅니다. 도는 앱은 그대로입니다."; exit 9
fi
step_done preflight

# 2) memory: a build that dies half-way must never touch the running .next, but still refuse to start starved (09-24 incident 2)
avail_mb=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
[ "${avail_mb:-0}" -ge 4096 ] || { echo "배포 중단 — 여유 메모리 ${avail_mb}MB (4096MB 미만)."; exit 3; }

# Test-run trust (gaps 42/46/247, harness suite_trust): a green run that ran 0 tests, or one whose test count fell 30%+
# from the last deploy, is not a pass. The runner is read from the app (package.json / pytest config), never assumed.
# SUITE_ALLOW_DROP=1 lets one deploy through after tests were removed on purpose (logged in the receipt).
suite_trust_check() {  # $1 = the test output file
  local runner rc n
  runner=$(python3 "$SHARED/suite-check.py" runner .)
  rc=0; python3 "$SHARED/suite-check.py" zero "$runner" "$1" 0 || rc=$?
  case "$rc" in
    0) ;;
    3) echo "경고 — 시험 신뢰 검사 도구(harness) 없음: 0개 실행 검사를 건너뜁니다"; receipt tests_trust=unavailable; return 0 ;;
    *) echo "배포 중단 — 시험이 통과로 끝났지만 실제로 0개가 돌았습니다($runner). 도는 앱은 그대로입니다."; exit 4 ;;
  esac
  rc=0; n=$(python3 "$SHARED/suite-check.py" count "$runner" "$1" "$STATE_ROOT/$app.test-counts.json" "npm run test") || rc=$?
  receipt tests_runner="$runner" tests_count="${n%% *}"
  # the baseline moves only after this deploy succeeds (EXIT trap): a deploy that fails later keeps the old count
  case "${n%% *}" in ''|*[!0-9]*) ;; *) tests_pending="${n%% *}" ;; esac
  if [ "$rc" = 1 ]; then
    if [ "${SUITE_ALLOW_DROP:-0}" = 1 ]; then echo "경고 — 시험 수가 30% 넘게 줄었지만 허용(SUITE_ALLOW_DROP=1): $n"; receipt tests_drop_allowed=1
    else echo "배포 중단 — 시험 수가 30% 넘게 줄었습니다: $n. 일부러 지운 것이면 SUITE_ALLOW_DROP=1"; exit 4; fi
  fi
  echo "시험 신뢰 검사 통과 — $runner, $n"
}

# Unstable-test detection (P1-2, 반장 2026-11-02): a first test-step failure is not necessarily a real
# regression — a test that only sometimes fails under load (server-dev-path.test.js 20:18 사고) must not
# be read as "the code is broken" OR silently waved through. On failure this reruns ONLY the file(s) the
# run's own report names as failing, twice more (3 attempts total). All 3 agree (all fail) -> an ordinary
# failure, same message/exit as before. The retries disagree -> "불안정 시험": the deploy still blocks
# (a flaky result is still not a pass), but the receipt and #경보 (ALERTS.md) say so by name, so the next
# person's first question is "why does this one file flip", not "what did I break". A runner this cannot
# re-run meaningfully (jest; failed-files finds nothing) falls straight back to the ordinary failure path.
ALERTS_FILE="${DEPLOY_ALERTS_FILE:-$APPS_ROOT/_ops/ops/ALERTS.md}"
handle_test_failure() {  # $1 = runner, $2 = first attempt's log file
  local runner="$1" log="$2" files rerun_rc2 rerun_rc3 retrylog2 retrylog3
  files=$(python3 "$SHARED/suite-check.py" failed-files "$runner" "$log" 2>/dev/null) || files=""
  if [ -z "$files" ]; then
    tail -20 "$log"; echo "배포 중단 — test 실패. 도는 앱은 그대로입니다."; exit 4
  fi
  echo "시험 실패 — 실패한 파일만 2회 더 돌려 불안정 여부 확인: $(tr '\n' ' ' <<< "$files")"
  retrylog2="/tmp/deploy-$app-test-retry2.log"; retrylog3="/tmp/deploy-$app-test-retry3.log"
  case "$runner" in
    vitest) mapfile -t rerun_cmd < <(printf 'npx\nvitest\nrun\n'; printf '%s\n' "$files") ;;
    jest)   mapfile -t rerun_cmd < <(printf 'npx\njest\n'; printf '%s\n' "$files") ;;
    pytest) mapfile -t rerun_cmd < <(printf 'python3\n-m\npytest\n'; printf '%s\n' "$files") ;;
    *) tail -20 "$log"; echo "배포 중단 — test 실패(재시도 불가능한 러너: $runner). 도는 앱은 그대로입니다."; exit 4 ;;
  esac
  # the whole script runs under `set -e` — a bare "cmd; rc=$?" would abort right here on the first
  # failing retry, before rc is ever read; the && / || form is what keeps the exit code without
  # tripping set -e (Codex-class bug, caught by tests/v2-gaps.sh's own "all 3 fail"/"mixed" cases).
  "${rerun_cmd[@]}" >"$retrylog2" 2>&1 && rerun_rc2=0 || rerun_rc2=$?
  "${rerun_cmd[@]}" >"$retrylog3" 2>&1 && rerun_rc3=0 || rerun_rc3=$?
  if [ "$rerun_rc2" = 0 ] && [ "$rerun_rc3" = 0 ]; then
    # 1회 실패, 2·3회 성공: 결과가 일치하지 않음 — 불안정
    :
  elif [ "$rerun_rc2" != 0 ] && [ "$rerun_rc3" != 0 ]; then
    tail -20 "$log"; echo "배포 중단 — 시험 3회 모두 실패(불안정 아님, 진짜 실패): $(tr '\n' ' ' <<< "$files")"; exit 4
  fi
  # here: 1회 실패 + (2·3회 중 적어도 하나는 성공, 나머지는 실패일 수도 있음) = 세 결과가 일치하지 않음 -> 불안정
  echo "불안정 시험 — $(tr '\n' ' ' <<< "$files") (1회차 실패, 2회차 rc=$rerun_rc2, 3회차 rc=$rerun_rc3)"
  receipt tests_flaky=1 "tests_flaky_files=$(tr '\n' ',' <<< "$files")"
  python3 -c "
import json, datetime, sys
stamp = datetime.datetime.now().strftime('%m-%d %H:%M')
files = sys.argv[2].strip('\n').split('\n') if sys.argv[2].strip() else []
rec = {'kind': 'unstable_test', 'app': sys.argv[1], 'files': files, 'retry2_rc': int(sys.argv[3]), 'retry3_rc': int(sys.argv[4])}
with open(sys.argv[5], 'a', encoding='utf-8') as f:
    f.write(f'- [ ] {stamp} ' + json.dumps(rec, ensure_ascii=False) + chr(10))
" "$app" "$files" "$rerun_rc2" "$rerun_rc3" "$ALERTS_FILE" 2>/dev/null || true
  echo "배포 중단 — 불안정 시험(결과가 매번 다름): $(tr '\n' ' ' <<< "$files") — 사람 확인 필요(#경보에 기록: $ALERTS_FILE). 도는 앱은 그대로입니다."
  exit 4
}

# 3) quality gate: typecheck / test when the app defines them (an app once shipped hundreds of new type errors unnoticed)
if [ "$QUALITY_GATE" = 1 ]; then
# The running build's generated route-type file lists routes that may no longer exist; the type check would fail on it
# after a route is deleted (observed once). Must run before the quality gate (typecheck).
rm -f "$dir/.next/types/validator.ts"
  for s in ${GATE_SCRIPTS:-typecheck test}; do   # GATE_SCRIPTS="typecheck" for apps without tests (vitest exits 1 when it finds none)
    if node -e "process.exit(require('./package.json').scripts?.['$s'] ? 0 : 1)"; then
      echo "검사 $s …"
      if NODE_OPTIONS="--max-old-space-size=$NODE_HEAP" npm run -s "$s" >/tmp/deploy-$app-$s.log 2>&1; then
        [ "$s" = test ] && suite_trust_check "/tmp/deploy-$app-$s.log"
      elif [ "$s" = test ]; then
        handle_test_failure "$(python3 "$SHARED/suite-check.py" runner .)" "/tmp/deploy-$app-$s.log"
      else
        tail -20 "/tmp/deploy-$app-$s.log"; echo "배포 중단 — $s 실패. 도는 앱은 그대로입니다."; exit 4
      fi
    fi
  done
fi

# 3a2) arch_check (구조 표준, 2026-11-02 배선 전수 조사 수정): 문서는 "CI에 넣어 어기면 실패"라 약속했지만
# 아무 데서도 안 불렀다. 지금 어기는 앱이 많아(27개 중 21개) 전부 하드로 걸면 배포가 멈추므로, 기준선
# (harness/arch_baseline.json, 이 날짜 실측)에서 위반 0이던 앱만 하드(새로 어기면 배포 중단) — 나머지는
# 경고로 영수증에 적고 배포는 계속한다.
if [ "$QUALITY_GATE" = 1 ] && command -v python3 >/dev/null 2>&1 && [ -f "$APPS_ROOT/_ops/harness/arch_check.py" ]; then
  arch_log="/tmp/deploy-$app-arch.log"; arch_out="/tmp/deploy-$app-arch.json"
  python3 "$APPS_ROOT/_ops/harness/arch_check.py" "$dir" --json >"$arch_out" 2>"$arch_log" || true
  arch_n=$(python3 -c "
import json
try:
    d = json.load(open('$arch_out'))
    print(len(d.get('violations', [])))
except Exception:
    print(-1)
" 2>/dev/null)
  if [ "${arch_n:-}" = "-1" ] || [ -z "${arch_n:-}" ]; then
    echo "경고 — arch_check 결과를 읽지 못함: $arch_log"; receipt arch_check=unavailable
  else
    baseline_zero=$(python3 -c "
import json
try:
    b = json.load(open('$APPS_ROOT/_ops/harness/arch_baseline.json'))
    print('1' if b.get('$app') == 0 else '0')
except Exception:
    print('0')
" 2>/dev/null)
    if [ "$arch_n" -gt 0 ] && [ "$baseline_zero" = "1" ]; then
      echo "배포 중단 — 구조 표준(arch_check) 위반 $arch_n 건. 이 앱은 기준선에서 위반 0이었습니다: $arch_log"
      receipt arch_check_violations="$arch_n" arch_check_hard=1
      exit 4
    fi
    [ "$arch_n" -gt 0 ] && echo "arch_check — 위반 $arch_n 건(경고만, 기존 구조 빚): $arch_log" || echo "arch_check — 위반 0건"
    receipt arch_check_violations="$arch_n"
  fi
fi

step_done gate

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
step_done build
# 4a) the tree that was checked must be the tree that was built (another session may have edited the folder meanwhile)
restore_next_files
fp_after=$(fingerprint); receipt tree_built="$fp_after"
if [ "$fp_after" != "$fp_before" ]; then
  [ -d "$BUILD/cache" ] && [ -d .next ] && [ ! -d .next/cache ] && mv "$BUILD/cache" .next/cache
  echo "배포 중단 — 검사한 뒤 빌드하는 사이 앱 폴더나 공유 패키지가 바뀌었습니다(검사 $fp_before / 빌드 $fp_after). 도는 앱은 그대로입니다."; exit 9
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
swapped=1; receipt swapped=1   # from here on an unexpected exit is an unknown outcome
mkdir -p "$PREV"
[ -d "$PREV/.next" ] && find "$PREV/.next" -depth -delete
[ -d .next ] && mv .next "$PREV/.next"
mv "$BUILD" .next
restore_next_files   # next writes the build dir into both; restore the repo versions (10-15, 10-16)
was_active=$($SYSTEMCTL --user is-active "$UNIT" 2>/dev/null || true)
if [ "${ON_DEMAND:-0}" = 1 ] && [ "$was_active" != active ]; then
  if [ "${REVIEW_GATE:-0}" = 1 ]; then bash "$SHARED/review-gate.sh" "$app" --deployed; fi
  echo "배포 완료 — $app (쓸 때만 켜는 앱이라 켜지 않음; 다음 시작 때 새 빌드 사용)"; exit 0
fi
if ! $SYSTEMCTL --user restart "$UNIT"; then echo "재시작 실패 — 되돌립니다."; swap_back || swapback_failed=1; exit 5; fi   # Codex P1: set -e must not skip the rollback
if [ -n "${WORKER_UNIT:-}" ]; then $SYSTEMCTL --user restart "$WORKER_UNIT" || echo "경고 — 워커 재시작 실패($WORKER_UNIT); 웹은 새 빌드로 올라감"; fi
step_done swap
for _ in $(seq 1 "${DEPLOY_WAIT_TRIES:-30}"); do
  pid=$($SYSTEMCTL --user show "$UNIT" -p MainPID --value)
  started=$( [ "$pid" != 0 ] && stat -c %Y "/proc/$pid" 2>/dev/null || echo 0 )   # pid may vanish in a crash loop — never let the arithmetic fail
  if [ "$started" != 0 ] && [ $(( $(date +%s) - started )) -ge 45 ]; then break; fi
  sleep 3
done

# 6) check, roll back on failure
if bash "$CHECK" "$app"; then
  if [ "${REVIEW_GATE:-0}" = 1 ]; then
    # 7) screen QA (Playwright + Chromium); a failing screen goes back to the previous build (ship.sh then fixes and redeploys)
    if ! bash "$SHARED/review-gate.sh" "$app" --qa; then echo "화면 검수 실패 — 되돌립니다."; swap_back || swapback_failed=1; exit 7; fi
    bash "$SHARED/review-gate.sh" "$app" --deployed
  fi
  echo "배포 완료 — $app"
else echo "배포 확인 실패 — 되돌립니다."; swap_back || swapback_failed=1; exit 5; fi
