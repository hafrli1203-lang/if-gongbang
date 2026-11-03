#!/usr/bin/env bash
# Generated from private source at a8eb56b1bac538511499bb39b350117e5eead6e1; do not edit by hand (see harness/export_v3_public.py).
# wt-gc.sh [--dry] [--only <copy>]
# Removes only work copies whose job is finished for good — docs/v3-wt-interface.md §6. Run at session start and on a nightly schedule.
# A copy is removed when
#   (a) <copy>/.wt.done exists AND it checks out (branch tip is an ancestor of the app's current base branch, no
#       uncommitted change) — see "(a) verify" below, or
#   (c) no .wt.done, but the recorded branch is ALREADY an ancestor of the base branch (really merged — e.g. hand
#       ff-merged without going through wt-land.sh) and the copy has no uncommitted change, no live unit, and its
#       last touch (newest file mtime or .wt.json's own mtime, whichever is later) was WT_GC_FRESH_SEC (30 min, not
#       the 48h below) or more ago — a short floor so a crew that just resumed this exact copy right after the merge
#       does not lose it out from under them — or
#   (b) no live unit, folder untouched for 48h, and its WT_TASK_STATUS_CMD task ended as failed/cancelled,
#       and the copy has no uncommitted change, or
#   (b2) no live unit, no task_id at all (a hand-made test/실측 copy — wt-new.sh without an AX OS task; team-lead
#       2026-11-02, 실측: one app's copy): clean (ignoring AUTO_RE) + untouched WT_GC_TASKLESS_SEC (2h) or more
#       -> git worktree remove (branch wt/<name> KEPT — unlanded commits are never silently discarded); dirty +
#       untouched 48h or more -> moved whole to $WT_ARCHIVE_ROOT/<날짜>/wt-stale/<app>-<name>, NEVER deleted.
# Everything else is kept: copies being resumed, done-but-not-landed tasks, copies without .wt.json (made by hand),
# and — before any of (a)/(c)/(b) above is even checked — every copy of an app that wt-land.sh is landing right now
# (that app's land.lock held, team-lead 2026-11-02: gc removed a work copy's directory out from under
# a running wt-land between its merge+deploy+push succeeding and it finishing .wt.json/.wt.done).
# Removal: stop its units -> git worktree remove --force -> release the test DB slot -> delete branch wt/<name> only for (a).
# --dry prints the decision for every copy and changes nothing.
# (a) verify (team-lead instruction 2026-11-02, 사고①): a hand-written .wt.done once made this script trust a forged
#   marker and delete an unlanded, uncommitted copy. .wt.done alone is no longer enough — the copy's branch tip must
#   also be an ancestor of the app's current base branch (i.e. actually landed, not merely marked) AND the copy must
#   have zero uncommitted change (same filter as (b)'s dirty check). Either check failing: the copy is PRESERVED, not
#   deleted, and the reason is appended to $WT_DATA/<app>/gc-skipped.log for a person to look at.
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

dry=0; only=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry) dry=1; shift ;;
    --only) only=$(readlink -f "${2:?--only <사본>}"); shift 2 ;;   # judge (and remove) just this one copy
    *) echo "모르는 인자: $1" >&2; exit 2 ;;
  esac
done
STALE_SEC=$((48 * 3600))
# team-lead 2026-11-02: condition (c) below (merged + clean + no unit) must not delete a copy another crew just
# started reusing right after the merge (실측: one app's copy) — a short freshness floor, not the 48h one.
WT_GC_FRESH_SEC="${WT_GC_FRESH_SEC:-$((30 * 60))}"
# team-lead 2026-11-02 (실측: one app's copy): every "미커밋 0" check below used to count next's own
# auto-rewritten tsconfig.json/next-env.d.ts (a build in the copy touches them) as an uncommitted change, so a
# copy that was actually merged+clean (apart from those two files) was never cleaned up and kept a slot forever.
# Same list wt-land.sh already treats as "not really dirty" (AUTO_RE there).
WT_GC_AUTO_RE='^ M (tsconfig\.json|next-env\.d\.ts)$'
# team-lead 2026-11-02 (실측: one app's copy) — a folder under $WT_ROOT with neither
# .wt.json nor .git (died mid wt-new.sh, before either was written) is not a work copy at all, just a stray leftover
# that sat there holding a slot. STRAY_SEC is a separate, longer floor than the failed-task 48h (b) above: a
# literally-just-starting wt-new.sh also has neither file for a moment, so this must never be too short.
STRAY_SEC=$((24 * 3600))
WT_ARCHIVE_ROOT="${WT_ARCHIVE_ROOT:?WT_ARCHIVE_ROOT 환경변수가 필요합니다 (오래된 사본을 보관할 폴더)}"
# team-lead 2026-11-02 (실측: one app's copy) — a copy wt-new.sh made without a tracked task (a crew's own
# test/실측 copy, .wt.json task_id empty) can never match the task_status-based (b) rule below: task_status("")
# always returns '' ("모름"), which that rule reads as "not yet confirmed failed" and keeps forever — a slot
# held for good with no AX OS task that could ever resolve it. TASKLESS_SEC (2h, far shorter than the 48h (b)
# floor — nothing is waiting on an AX OS task to fail here) is the separate floor for this case only.
WT_GC_TASKLESS_SEC="${WT_GC_TASKLESS_SEC:-$((2 * 3600))}"
removed=0; kept=0

task_status() {  # $1 task id -> status word from the adapter command, '' when unknown or unset
  [ -n "$1" ] && [ -n "${WT_TASK_STATUS_CMD:-}" ] || return 0
  $WT_TASK_STATUS_CMD "$1" 2>/dev/null || true
}

newest_mtime() {  # newest mtime under the copy, ignoring node_modules (a symlink) and .git
  find "$1" -path "$1/node_modules" -prune -o -name .git -prune -o -printf '%T@\n' 2>/dev/null | sort -n | tail -1 | cut -d. -f1
}

skip_log() {  # $1 app, $2 message — gc-skipped.log, never touched by anything but this script
  mkdir -p "$WT_DATA/$1"
  printf '[%s] %s\n' "$(date -Iseconds)" "$2" >> "$WT_DATA/$1/gc-skipped.log"
}

# true when a wt-land.sh is right now landing something for $1 (its land.lock, $WT_DATA/<app>/land.lock, is held) —
# team-lead 2026-11-02, 실측: wt-land removed a work copy's directory mid-land (after the merge+
# deploy+push succeeded, before it finished writing .wt.json/.wt.done) because gc ran concurrently (AX OS start) and
# this exact copy already satisfied condition (c) at that instant. gc must not touch ANY copy of an app while that
# app is being landed — it cannot tell which copy is the one in flight. Non-blocking: a missing lock file means no
# land has ever run (not busy); any other failure to test the lock is treated as "busy" (안전 쪽으로 — fail-closed).
wt_app_landing() {  # $1 app -> 0 = busy (a land is in progress), 1 = not
  local lf="$WT_DATA/$1/land.lock" fd
  [ -f "$lf" ] || return 1
  exec {fd}<>"$lf" || return 0
  if flock -n "$fd"; then flock -u "$fd"; exec {fd}<&-; return 1; else exec {fd}<&-; return 0; fi
}

# true when $wt's recorded branch (.wt.json's "branch", NOT necessarily HEAD) is actually merged into $app's
# current base branch AND the copy has zero uncommitted change (wt's own untracked artifacts — node_modules,
# .wt.* including .wt.test-output.log — ignored, same filter scope-check.sh uses). A failed lookup (base branch
# gone, repo trouble, git status itself failing) is NOT treated as "merged+clean" (안전 쪽으로). SILENT — callers
# decide what, if anything, to log on failure; a plain "not yet mergeable" is normal, not suspicious.
# team-lead 2026-11-02 Codex P1 review of this bundle found and this fixes two bypasses:
#  - `tip` used to be `rev-parse HEAD` — `git -C $wt switch --detach <base>` moves HEAD onto an already-merged
#    commit WITHOUT touching the recorded branch wt/<name> at all, so a copy with real unmerged commits on that
#    branch passed this check; `tip` is now read from the branch .wt.json actually RECORDS (and later removal still
#    deletes exactly that recorded branch, per the existing `br=$(wt_json_get "$wt/.wt.json" branch)` below) — moving
#    HEAD elsewhere no longer changes what gets verified.
#  - the dirty check used `... || true`, which (under `set -e`/`pipefail`) silently turned a FAILED `git status`
#    (e.g. a session setting `GIT_INDEX_FILE=/dev/null` before invoking this script, corrupting every git call made
#    from then on) into "no output" = "clean". `git status` itself must now succeed, or the copy is preserved.
# team-lead 2026-11-02 (같은 사고 클래스: 커밋 0개인 빈 사본은 (b2)에서도 "손수 사본 — 2시간"의 짧은 floor로
#읽으면 안 된다; 조건 (c)처럼 "일"이 전혀 없었던 것일 뿐이라 48시간(STALE_SEC)까지는 그냥 둔다). 0 = 비어 있음
# (가지가 기본 가지와의 분기점 그 자체), 1 = 커밋이 1개 이상 붙어 있음(또는 판정 불가 — 안전 쪽으로 "비지 않음").
wt_branch_empty() {  # $1 wt-path, $2 app (app unused, kept for call-site symmetry) -> 0 = empty, 1 = has a commit
                      # made since the copy itself was created, or unknown (안전 쪽으로 — "비지 않음")
  local br tip created tip_epoch created_epoch
  br=$(wt_json_get "$1/.wt.json" branch); [ -n "$br" ] || return 1
  tip=$(wt_git -C "$1" rev-parse "refs/heads/$br" 2>/dev/null) || return 1
  created=$(wt_json_get "$1/.wt.json" created_at); [ -n "$created" ] || return 1
  # comparing the branch tip's commit DATE against the copy's OWN created_at (never against the app's current base
  # branch state) — a merge-base/is-ancestor comparison against base looked identical for "never touched at all"
  # and "had exactly one commit that base has since fast-forwarded past" (Codex-class bug this replaced: GC4's own
  # test, a real landed commit, was wrongly read as "empty" once main caught up to its tip after the ff-merge).
  tip_epoch=$(wt_git -C "$1" log -1 --format=%ct "$tip" 2>/dev/null) || return 1
  created_epoch=$(date -d "$created" +%s 2>/dev/null) || return 1
  [[ "$tip_epoch" =~ ^[0-9]+$ ]] && [[ "$created_epoch" =~ ^[0-9]+$ ]] || return 1
  # strictly LESS THAN, not <=: a commit made in the same wall-clock SECOND as the copy itself (real traffic is
  # never this fast, but an automated test harness committing right after wt-new.sh easily is) must never be read
  # as "empty" — ambiguous here means "has a commit" (안전 쪽으로), never the other way around.
  [ "$tip_epoch" -lt "$created_epoch" ]
}

wt_merged_and_clean() {  # $1 wt-path, $2 app -> 0 = verified landed + clean, 1 = not (silent)
  local br tip base d
  br=$(wt_json_get "$1/.wt.json" branch); [ -n "$br" ] || return 1
  tip=$(wt_git -C "$1" rev-parse "refs/heads/$br" 2>/dev/null) || return 1
  base=$(wt_base_branch "$2" 2>/dev/null) || return 1
  wt_git -C "$WT_APPS/$2" merge-base --is-ancestor "$tip" "$base" 2>/dev/null || return 1
  # team-lead 2026-11-02 (실측: one app's copy — 막 만든, 커밋 0개인 빈 사본이 30분 뒤 이 (c) 규칙으로
  # 지워짐): a branch with ZERO commits of its own is trivially "an ancestor of base" (it IS a commit already
  # in base's history) but represents no landed work at all — "합쳐짐"으로 읽으면 안 된다.
  wt_branch_empty "$1" "$2" && return 1 || true   # (set -e 안전: 비어있지 않을 때 이 줄 자체가 비0 종료가 되면 안 됨)
  d=$(wt_git -C "$1" status --porcelain 2>/dev/null) || return 1
  d=$(printf '%s\n' "$d" | grep -vE '^\?\? (node_modules|\.wt\.)' | grep -vE "$WT_GC_AUTO_RE" || true)
  [ -z "$d" ]
}

# (a) .wt.done 경로: 위와 같은 검증이되, 실패하면 "위조 의심"으로 gc-skipped.log에 남긴다(.wt.done이 있는데도
# 검증에 실패하는 것은 정상적인 "아직 안 합쳐짐"이 아니라 의심스러운 상태이기 때문 — 2026-11-02 사고①).
wt_done_verified() {  # $1 wt-path, $2 app -> 0 = verified landed + clean, 1 = not (skip_log를 남김)
  local br tip base d
  br=$(wt_json_get "$1/.wt.json" branch)
  [ -n "$br" ] || { skip_log "$2" "$1 — .wt.done 있지만 .wt.json에 가지 이름이 없음(검증 불가) — 보존"; return 1; }
  tip=$(wt_git -C "$1" rev-parse "refs/heads/$br" 2>/dev/null) \
    || { skip_log "$2" "$1 — .wt.done 있지만 기록된 가지($br)를 읽지 못함(검증 불가) — 보존"; return 1; }
  base=$(wt_base_branch "$2" 2>/dev/null) || { skip_log "$2" "$1 — .wt.done 있지만 기본 가지를 읽지 못함(검증 불가) — 보존"; return 1; }
  if ! wt_git -C "$WT_APPS/$2" merge-base --is-ancestor "$tip" "$base" 2>/dev/null; then
    skip_log "$2" "$1 — .wt.done 있지만 기록된 가지($br)가 기본 가지($base)에 합쳐지지 않음(미검증/위조 의심 종료 표식) — 보존"
    return 1
  fi
  if ! d=$(wt_git -C "$1" status --porcelain 2>/dev/null); then
    skip_log "$2" "$1 — .wt.done 있지만 상태 확인(git status) 자체가 실패함(검증 불가) — 보존"
    return 1
  fi
  d=$(printf '%s\n' "$d" | grep -vE '^\?\? (node_modules|\.wt\.)' | grep -vE "$WT_GC_AUTO_RE" || true)
  if [ -n "$d" ]; then
    skip_log "$2" "$1 — .wt.done 있지만 미커밋 변경이 있음 — 보존"
    return 1
  fi
  return 0
}

for wt in "$WT_ROOT"/*/*; do
  [ -d "$wt" ] || continue
  [ -z "$only" ] || [ "$(readlink -f "$wt")" = "$only" ] || continue
  if [ ! -f "$wt/.wt.json" ]; then
    # a folder with neither .wt.json nor .git is not a copy at all (a hand-made copy at least has .git) — only a
    # stray leftover from a wt-new.sh that died before writing either. Old enough (STRAY_SEC, separate from a
    # just-starting wt-new.sh's own brief window with neither file) -> archived, never silently deleted.
    if [ ! -e "$wt/.git" ]; then
      m=$(newest_mtime "$wt"); fm=$(stat -c %Y "$wt" 2>/dev/null || echo 0)
      last=$(( ${m:-0} > fm ? ${m:-0} : fm )); age=$(( $(date +%s) - last ))
      if [ "$age" -ge "$STRAY_SEC" ]; then
        app_s=$(basename "$(dirname "$wt")"); name_s=$(basename "$wt")
        dest_dir="$WT_ARCHIVE_ROOT/$(date +%Y-%m-%d)/wt-stray"; dest="$dest_dir/$app_s-$name_s"
        if [ "$dry" = 1 ]; then
          echo "떠돌이 폴더 이동 예정  $wt — git 저장소 아님(.git 없음) + .wt.json 없음 + $((age / 3600))시간 넘음 -> $dest"
          removed=$((removed + 1)); continue
        fi
        mkdir -p "$dest_dir"
        if mv "$wt" "$dest" 2>/dev/null; then
          echo "떠돌이 폴더 이동  $wt -> $dest — git 저장소 아님(.git 없음) + .wt.json 없음 + $((age / 3600))시간 넘음"
          removed=$((removed + 1)); continue
        else
          echo "보존  $wt — 떠돌이 폴더 이동 실패"; kept=$((kept + 1)); continue
        fi
      fi
      echo "보존  $wt — git 저장소도 .wt.json도 없지만 아직 $((STRAY_SEC / 3600))시간 안 지남"; kept=$((kept + 1)); continue
    fi
    echo "보존  $wt — .wt.json 없음(손으로 만든 사본)"; kept=$((kept + 1)); continue
  fi
  app=$(wt_json_get "$wt/.wt.json" app); name=$(wt_json_get "$wt/.wt.json" name); task=$(wt_json_get "$wt/.wt.json" task_id)
  if wt_app_landing "$app"; then
    echo "보존  $wt — $app 합치기(wt-land) 진행 중(land.lock) — 건드리지 않음"; kept=$((kept + 1)); continue
  fi
  reason=""; done_mark=0
  if [ -e "$wt/.wt.done" ]; then
    if wt_done_verified "$wt" "$app"; then
      reason="종료 표식(.wt.done, 기본 가지에 합쳐짐 확인됨 + 미커밋 변경 없음)"; done_mark=1
    else
      echo "보존  $wt — .wt.done 있지만 검증 실패(gc-skipped.log 참고)"; kept=$((kept + 1)); continue
    fi
  else
    live=0
    for u in $(wt_json_get "$wt/.wt.json" units); do wt_unit_alive "$u" && live=1; done
    if [ "$live" = 1 ]; then echo "보존  $wt — 도는 유닛 있음"; kept=$((kept + 1)); continue; fi
    # (c) team-lead instruction 2026-11-02 (실측: 한 앱의 여러 사본이 가지는 master에
    # 손으로 ff 합쳐졌지만 wt-land를 안 거쳐 .wt.done이 없어 gc가 영원히 안 지우고 슬롯을 막음) — .wt.done이
    # 없어도 가지가 기본 가지의 조상이고(실제로 합쳐짐) + 미커밋 변경이 없고(wt 자체 산출물은 무시) + 살아
    # 있는 unit이 없으면 지운다. mtime/48h, AX OS task 상태는 이 경로에서는 보지 않는다 — git 사실만으로 충분.
    if wt_merged_and_clean "$wt" "$app"; then
      # last touched = the later of the copy's newest file mtime and .wt.json's own mtime (wt-land/wt-run update it
      # without necessarily touching a tracked file) — a crew that just resumed this exact copy bumps one of the two.
      jm=$(stat -c %Y "$wt/.wt.json" 2>/dev/null || echo 0)
      fm=$(newest_mtime "$wt"); fm="${fm:-0}"
      last=$(( jm > fm ? jm : fm )); fresh_age=$(( $(date +%s) - last ))
      if [ "$fresh_age" -lt "$WT_GC_FRESH_SEC" ]; then
        echo "보존  $wt — 합쳐졌지만 최근 $((fresh_age / 60))분 안에 바뀜(다른 작업조가 다시 쓰기 시작했을 수 있음)"; kept=$((kept + 1)); continue
      fi
      reason="가지가 기본 가지에 이미 합쳐짐 + 미커밋 변경 없음 + 마지막 변경 $((WT_GC_FRESH_SEC / 60))분 넘게 지남(종료 표식 없이도 정리, owner 2026-11-02)"; done_mark=1
    elif [ -z "$task" ]; then
      # (b2) team-lead 2026-11-02 (실측: one app's copy) — task_status("") is always "모름", so the (b)
      # branch below would keep a task-less hand-made copy forever; this is its own, shorter-floor rule.
      jm=$(stat -c %Y "$wt/.wt.json" 2>/dev/null || echo 0)
      fm=$(newest_mtime "$wt"); fm="${fm:-0}"
      last=$(( jm > fm ? jm : fm )); taskless_age=$(( $(date +%s) - last ))
      dirty=$(wt_git -C "$wt" status --porcelain 2>/dev/null | grep -vE '^\?\? (node_modules|\.wt\.)' | grep -vE "$WT_GC_AUTO_RE" || true)
      if [ -n "$dirty" ]; then
        if [ "$taskless_age" -lt "$STALE_SEC" ]; then
          echo "보존  $wt — task_id 없는 손수 사본, 미커밋 있음, 아직 $((STALE_SEC / 3600))시간 안 지남"; kept=$((kept + 1)); continue
        fi
        app_s=$(basename "$(dirname "$wt")"); name_s=$(basename "$wt")
        dest_dir="$WT_ARCHIVE_ROOT/$(date +%Y-%m-%d)/wt-stale"; dest="$dest_dir/$app_s-$name_s"
        if [ "$dry" = 1 ]; then
          echo "오래된 손수 사본(미커밋 있음) 보관 예정  $wt -> $dest — 지우지 않음"; removed=$((removed + 1)); continue
        fi
        mkdir -p "$dest_dir"
        if mv "$wt" "$dest" 2>/dev/null; then
          echo "오래된 손수 사본(미커밋 있음) 보관  $wt -> $dest — 지우지 않고 옮김"; removed=$((removed + 1)); continue
        else
          echo "보존  $wt — 보관 이동 실패"; kept=$((kept + 1)); continue
        fi
      fi
      # team-lead 2026-11-02 (실측: one app's copy) — a branch with zero commits of its own represents no
      # work at all, not "task-less work that is simply done" — give it the SAME 48h floor as an empty (c) copy,
      # not the short 2h floor meant for a real (if task-less) finished job.
      if wt_branch_empty "$wt" "$app"; then taskless_floor="$STALE_SEC"; empty_note=" — 빈 사본(커밋 0개)"
      else taskless_floor="$WT_GC_TASKLESS_SEC"; empty_note=""; fi
      if [ "$taskless_age" -lt "$taskless_floor" ]; then
        echo "보존  $wt — task_id 없는 손수 사본, 아직 $((taskless_floor / 60))분 안 지남$empty_note"; kept=$((kept + 1)); continue
      fi
      reason="task_id 없는 손수 사본 + 미커밋 변경 없음 + 마지막 변경 $((taskless_floor / 3600))시간 넘게 지남(가지는 보존, owner 2026-11-02)$empty_note"
      # done_mark stays 0 — the shared removal code below only deletes the branch when done_mark=1
    else
      m=$(newest_mtime "$wt"); age=$(( $(date +%s) - ${m:-0} ))
      st=$(task_status "$task")
      dirty=$(wt_git -C "$wt" status --porcelain 2>/dev/null | grep -vE '^\?\? (node_modules|\.wt\.)' | grep -vE "$WT_GC_AUTO_RE" || true)
      if [ "$age" -lt "$STALE_SEC" ]; then echo "보존  $wt — 최근 $((age / 3600))시간 안에 바뀜"; kept=$((kept + 1)); continue; fi
      case "$st" in failed|cancelled|canceled|discarded) ;; *) echo "보존  $wt — 작업 상태 '${st:-모름}'(실패 확정 아님)"; kept=$((kept + 1)); continue ;; esac
      if [ -n "$dirty" ]; then echo "보존  $wt — 미커밋 변경 있음"; kept=$((kept + 1)); continue; fi
      reason="48시간 지난 실패 작업($st)"
    fi
  fi
  if [ "$dry" = 1 ]; then echo "삭제 예정  $wt — $reason"; removed=$((removed + 1)); continue; fi
  bash "$WT_SHARED/wt-stop.sh" "$wt" >/dev/null || { echo "보존  $wt — 유닛 정지 실패"; kept=$((kept + 1)); continue; }
  # delete only the branch this copy recorded, and only when it is wt/<name> — read before the copy is removed
  br=$(wt_json_get "$wt/.wt.json" branch)
  wt_git -C "$WT_APPS/$app" worktree remove --force "$wt"
  wt_slot_release "$app" "$wt"
  [ "$done_mark" = 1 ] && [ "$br" = "wt/$name" ] && wt_git -C "$WT_APPS/$app" branch -D "$br" >/dev/null 2>&1 || true
  echo "삭제  $wt — $reason"; removed=$((removed + 1))
done
[ "$dry" = 1 ] && echo "(--dry) 삭제 예정 ${removed}개, 보존 ${kept}개" || echo "삭제 ${removed}개, 보존 ${kept}개"
