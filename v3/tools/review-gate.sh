#!/usr/bin/env bash
# Generated from private source at a8eb56b1bac538511499bb39b350117e5eead6e1; do not edit by hand (see harness/export_v3_public.py).
# Review gate (2026-11-01, owner "왜 이렇게 빠트리는일이 많지? 이런일 없게 쫌 할수 없나?" → "추천대로"): the two pre-deploy
# checks that used to depend on memory — a live run of the changed code on real data, and the Codex read-only review — are
# enforced here. Opt-in per app with REVIEW_GATE=1 in deploy.env (enable per app, incrementally).
#
# usage:
#   review-gate.sh <app> --live "<command>"   run a command against real data (DB session read-only) for the current change; its output
#                                             is saved for this exact diff (a later edit needs a new live run)
#   review-gate.sh <app> --check              called by deploy.sh: needs a live run for this diff, then runs Codex (cached per
#                                             diff); P0/P1 stops the deploy
#   review-gate.sh <app> --qa                 called by deploy.sh after the restart: screen QA with Playwright + Chromium
#                                             (qa-pages.mjs) on QA_PATHS (default HEALTH_PATHS) and the pages they link to,
#                                             the changed routes first; exit 7 on a failure (deploy.sh rolls back)
#   review-gate.sh <app> --deployed           called by deploy.sh after a good deploy: the next diff starts from this commit
# Every --check that clears (pass, skip, no-code-change, Codex-off) writes the exact HEAD sha it looked at to
# <state>/reviewed.json (gap 2026-11-03, dori-superagent pattern): wt-land compares this sha to the commit it is about to
# land and, when they differ (a commit was added to the copy after the check passed), re-runs this gate on the new HEAD
# automatically before merging — no commit lands unreviewed, whatever the diff-hash cache would otherwise have reused.
# Fixing is not left to a person: ship.sh runs deploy.sh and, when a check fails, has Codex fix it and goes again.
# The diff is everything tracked that changed since the last deployed commit (committed or not). Changes that touch only
# Markdown files need neither check. REVIEW_SKIP="<reason>" lets one deploy through; the reason is logged.
set -euo pipefail
app="${1:?app}"; mode="${2:?mode}"
[[ "$app" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "앱 이름이 이상합니다: $app"; exit 2; }
dir="${DEPLOY_APPS_ROOT:-${WT_APPS_ROOT}}/$app"; envf="$dir/deploy.env"   # DEPLOY_APPS_ROOT / DEPLOY_STATE_ROOT: test seams
# shellcheck disable=SC1090
[ -f "$envf" ] && . "$envf"
dir="${APP_DIR:-$dir}"
state="${DEPLOY_STATE_ROOT:-${WT_DEPLOY_STATE}}/$app.review"; mkdir -p "$state"
cd "$dir"

base=$(cat "$state/deployed" 2>/dev/null || true)
git cat-file -e "${base:-none}^{commit}" 2>/dev/null || base=$(git rev-parse HEAD~1)   # first use: the last commit only
diff_file="$state/current.diff"
git diff "$base" > "$diff_file"
hash=$(sha256sum < "$diff_file" | cut -c1-16)
code_files=$(git diff --name-only "$base" | grep -v -E '\.md$' || true)

# gap (2026-11-03, dori-superagent pattern): --check clearing a deploy was tied only to the diff's content hash, never to the
# exact commit SHA it looked at. A receipt naming the diff is not enough for a caller (wt-land) that needs to ask "is THIS
# exact commit the one that passed?" — so every --check that clears (passes, is skipped, or needs no review) writes the
# current HEAD sha into a receipt (receipts.py, same tool deploy.sh/wt-land already use for other receipts). wt-land reads
# this file before merging a work copy and re-runs this gate automatically when the copy moved past the reviewed sha.
record_reviewed() {  # $1 = reason (no-code-changes | skipped:<why> | codex-skipped | codex-pass)
  python3 "$(dirname "$0")/receipts.py" set "$state/reviewed.json" \
    sha="$(git rev-parse HEAD)" diff="$hash" base="$base" reason="$1" at="$(date -Iseconds)"
}

case "$mode" in
  --deployed)
    git rev-parse HEAD > "$state/deployed"; exit 0 ;;
  --live)
    cmd="${3:?명령을 주세요: review-gate.sh $app --live \"<command>\"}"
    out="$state/live-$hash.txt"
    { echo "# $(date '+%F %T') base ${base:0:7} diff $hash"; echo "# \$ $cmd"; } > "$out"
    printf '%s' "$cmd" > "$state/live-cmd"   # ship.sh runs the same check again after an automatic fix
    # The database refuses writes during a live run (PostgreSQL read-only session; node-pg and psql read PGOPTIONS).
    if PGOPTIONS="${PGOPTIONS:-} -c default_transaction_read_only=on" bash -c "$cmd" >> "$out" 2>&1; then tail -20 "$out"; echo "실데이터 실행 기록 — $out"
    else tail -20 "$out"; rm -f "$out"; echo "실데이터 실행이 실패했습니다(기록하지 않음). 고친 뒤 다시 실행하세요."; exit 1; fi
    exit 0 ;;
  --qa)
    # "|| true": no changed screen is normal (grep finds nothing → pipefail would end the script silently)
    routes=$( { git diff --name-only "$base" | grep -E '^(src/)?app/' || true; } | sed -E 's#^(src/)?app##; s#/[^/]*$##; s#/\([^/)]*\)##g; s#^$#/#' | sort -u | tr '\n' ' ')
    qa_out="$state/qa-$(date +%Y%m%d-%H%M%S)"
    echo "화면 검수(Playwright·Chromium) — 바뀐 화면: ${routes:-없음}"
    node "$(dirname "$0")/qa-pages.mjs" "$dir" "http://127.0.0.1:${PORT:?PORT}" "$qa_out" "${QA_PATHS:-${HEALTH_PATHS:-/}}" "$routes" "${QA_MAX:-30}"
    exit $? ;;
  --check) ;;
  *) echo "모르는 모드: $mode"; exit 2 ;;
esac

if [ -z "$code_files" ]; then echo "점검 — 코드 변경 없음(문서만) — 통과"; record_reviewed "no-code-changes"; exit 0; fi
if [ -n "${REVIEW_SKIP:-}" ]; then echo "$(date '+%F %T') diff $hash skipped: $REVIEW_SKIP" >> "$state/skips.log"; echo "점검 건너뜀 — 이유 기록: $REVIEW_SKIP"; record_reviewed "skipped:$REVIEW_SKIP"; exit 0; fi

# 1) live run on real data for this exact diff
if [ ! -s "$state/live-$hash.txt" ]; then
  echo "배포 중단 — 이번 변경을 실제 데이터로 돌린 기록이 없습니다(diff $hash, ${base:0:7} 이후 $(echo "$code_files" | wc -l)개 파일)."
  echo "  바뀐 코드 경로를 읽기 전용으로 돌린 뒤: bash ${WT_SHARED}/review-gate.sh $app --live \"<명령>\""
  exit 4
fi
echo "실데이터 실행 기록 있음 — $state/live-$hash.txt"
# a live run that is a test run must have run tests: 0 executed / all skipped is a fake pass (gap 247, harness
# suite_trust; INTEGRATION.md §1). The runner is read from the app, not assumed.
st_rc=0; python3 "$(dirname "$0")/suite-check.py" zero "$(python3 "$(dirname "$0")/suite-check.py" runner "$dir")" "$state/live-$hash.txt" || st_rc=$?
if [ "$st_rc" = 1 ]; then echo "배포 중단 — 이번 실데이터 실행이 실제로는 시험 0개를 돌렸습니다(가짜 통과)."; exit 4; fi
[ "$st_rc" = 3 ] && echo "경고 — 시험 신뢰 검사 도구(harness) 없음: 0개 실행 검사를 건너뜁니다"
# REVIEW_GATE_CODEX=0 (deploy.env or env): the Codex review step is skipped; the live run above and screen QA (--qa) stay
# (plan 2026-11-01 §5: "Codex 점검만 제거" — one Codex check happens in the worker's own verification instead).
if [ "${REVIEW_GATE_CODEX:-1}" = 0 ]; then echo "Codex 점검 건너뜀(REVIEW_GATE_CODEX=0) — 실데이터 실행·화면 검수는 유지"; record_reviewed "codex-skipped"; exit 0; fi

# 2) Codex read-only review, cached per diff (it also judges whether the live run really exercised the change)
# keyed by the diff AND the live record: a re-made live check must be judged again, not answered from the cache
live_hash=$(sha256sum < "$state/live-$hash.txt" | cut -c1-8)
verdict="$state/codex-$hash-$live_hash.txt"
if [ ! -s "$verdict" ]; then
  echo "Codex 점검 중 …(diff $hash)"
  start=$(date +%s)
  # gap 264: a diff over the budget is not handed over whole — Codex gets the file list (largest first) and reviews file
  # by file, then says which files it covered; gap 274: the workspace architecture standard is part of the review.
  budget="${REVIEW_DIFF_BUDGET:-240000}"; dsize=$(stat -c %s "$diff_file"); extra=""
  if [ "$dsize" -gt "$budget" ]; then
    git diff --numstat "$base" | awk '{print ($1+$2) "\t" $3}' | sort -rn > "$state/current.files"
    echo "경고 — diff ${dsize}바이트가 예산 ${budget}을 넘습니다: 파일별로 나눠 점검하게 합니다($state/current.files)"
    extra=" The diff is $dsize bytes, over the $budget-byte budget: do not read $diff_file whole. Take the files listed (changed lines, largest first) in $state/current.files and review each with 'git -C $dir diff ${base:0:12} -- <file>'. Before the VERDICT line write one line 'COVERAGE: full' if you reviewed every listed file, otherwise 'COVERAGE: partial — <files not reviewed>'."
  fi
  arch="${REVIEW_ARCH_STANDARD:-${WT_APPS_ROOT}/_ops/COMMON_ARCHITECTURE_STANDARD.md}"
  [ -f "$arch" ] && extra="$extra Also hold the change to the workspace architecture standard at $arch (and $dir/docs/ai/CURRENT_DECISIONS.md if it exists): a violation this diff introduces is a P1; one that only sits in untouched code is not."
  prompt="Review the code change in $dir. The diff (from deployed commit ${base:0:7} to the working tree) is at $diff_file; read surrounding code as needed. The live run on real data recorded for this change is at $state/live-$hash.txt (its command and output): if that run does not actually exercise the changed code paths, report it as a P1 written exactly as '- P1 [LIVE] — <what the run should do instead>' (the [LIVE] tag only for a live run that is not enough; a defect in the code itself never gets it). A run that prints NO DATA because the database really has no such rows is acceptable when the changed code is pure or covered by tests in the diff — do not block on missing data alone. The live run is read-only by design (the database refuses writes): never ask it to perform saves, inserts, job queueing or outside calls. A write path is acceptable when its read side ran live and a test in the diff covers the write; if no test covers a changed write path, report that as a P1 on the code (a missing test), not on the live run. Also check the blast radius beyond the diff: callers of changed functions, DB columns or JSON shapes other code or other apps read (other apps call this app's /api routes), feature flags, jobs and timers — a break there counts as a defect even though it is outside the diff. Report only real defects as P0/P1/P2 with file:line, briefly, in Korean.$extra The LAST line must be exactly 'VERDICT: BLOCK' if there is any P0 or P1, otherwise exactly 'VERDICT: PASS'."
  if ! timeout 900 codex exec -s read-only -c model_reasoning_effort="medium" -o "$verdict.tmp" "$prompt" < /dev/null > "$state/codex-$hash-$live_hash.log" 2>&1; then
    tail -5 "$state/codex-$hash-$live_hash.log"; rm -f "$verdict.tmp"
    echo "배포 중단 — Codex 점검을 실행하지 못했습니다(로그 $state/codex-$hash-$live_hash.log). 다시 시도하거나, 정말 필요하면 REVIEW_SKIP=\"이유\"."; exit 4
  fi
  # gap 265: a verdict is only about the code it saw — if the change moved while Codex read it, throw the verdict away
  now_hash=$(git diff "$base" | sha256sum | cut -c1-16)
  if [ "$now_hash" != "$hash" ]; then
    rm -f "$verdict.tmp"; echo "배포 중단 — Codex가 읽는 사이 코드가 바뀌었습니다(diff $hash → $now_hash). 다시 실행하세요."; exit 4
  fi
  mv "$verdict.tmp" "$verdict"
  printf "\n# %s초\n" "$(( $(date +%s) - start ))" >> "$verdict"
fi
grep -m1 '^COVERAGE:' "$verdict" | grep -q partial && echo "경고 — Codex가 일부 파일만 점검했습니다: $(grep -m1 '^COVERAGE:' "$verdict")"
cat "$verdict"
if grep -q '^VERDICT: PASS' "$verdict"; then echo "Codex 점검 통과"; record_reviewed "codex-pass"; exit 0; fi
grep -q '^VERDICT: BLOCK' "$verdict" || { rm -f "$verdict"; echo "배포 중단 — Codex 답에 판정 줄이 없습니다. 다시 실행하세요."; exit 4; }
echo "배포 중단 — Codex가 P0/P1을 찾았습니다. 고친 뒤 다시 배포하세요(고치면 실데이터 실행도 다시)."; exit 4
