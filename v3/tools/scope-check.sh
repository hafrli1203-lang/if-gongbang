#!/usr/bin/env bash
# Generated from private source at 494f80a1335ecba422757332f15d6b4100b0afaf; do not edit by hand (see harness/export_v3_public.py).
# scope-check.sh <copy> [--range <a>..<b>]
# --range: check the files changed between two commits of the app repository instead of the copy (wt-land --retry).
# Compares what the copy changed with its allowed paths (<copy>/.wt.scope, one glob per line, # comments) — docs/v3-wt-interface.md §4.
# Changed = commits on the copy's branch since it left the base branch + uncommitted tracked changes + untracked files.
# Globs: * = one path segment, ** = any depth, a line ending in / = everything under that folder.
# package.json, lock files and any migrations/ or drizzle/ path pass only when an allowed line names them literally
# ("package.json", "package-lock.json", "lock", "migrations", "drizzle") — a broad "**" does not cover them.
# Ignored: untracked node_modules, .wt.* files, .next-wt build dirs, .verify/ evidence, .pids, and uncommitted tsconfig.json / next-env.d.ts / CLAUDE.md /
# AGENTS.md (next dev rewrites them; Next 16 regenerates CLAUDE.md). A committed change to them is still checked.
# exit 0 = all inside the scope, 2 = violations (listed), 1 = cannot check.
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

wt=$(readlink -f "${1:-}" 2>/dev/null || true)
[ -n "$wt" ] && [ -f "$wt/.wt.json" ] || wt_die "사용: scope-check.sh <사본> (.wt.json 없음)"
scope="$wt/.wt.scope"
[ -f "$scope" ] || { echo "범위 파일이 없습니다: $scope — 허용 경로를 한 줄씩 적어야 합니다."; exit 2; }
base=$(wt_json_get "$wt/.wt.json" base)
[ -n "$base" ] || base=$(wt_base_branch "$(wt_json_get "$wt/.wt.json" app)")
range=""
[ "${2:-}" = --range ] && range="${3:?--range <a>..<b>}"

# Work-copy artifacts are ignored only while UNTRACKED: verify evidence (.verify/), dev/build output (.next-wt*), the .wt.*
# state files, .pids and the node_modules copy. Committed or staged, they count like any other path.
ARTIFACTS='^(node_modules(/.*)?|\.wt\.(json|scope|done)(\.tmp)?|\.next-wt[^/]*(/.*)?|\.verify(/.*)?|\.pids(/.*)?)$'
# every git query must succeed: a failed one would otherwise read as "no changes" and pass (Codex 11-01)
g() { wt_git -C "$wt" "$@" || { echo "범위 검사 불가 — git ${*:1:2} 실패" >&2; exit 1; }; }
filt() { grep -vE "$1" || [ $? = 1 ]; }   # grep exit 1 = nothing left after filtering, not an error
if [ -n "$range" ]; then
  [[ "$range" =~ ^[0-9a-f]{7,40}\.\.[0-9a-f]{7,40}$ ]] || wt_die "범위 형식: <커밋>..<커밋>"
  app_dir="$WT_APPS/$(wt_json_get "$wt/.wt.json" app)"
  changed=$(wt_git -C "$app_dir" diff --name-only "${range%%..*}" "${range##*..}") || wt_die "범위 검사 불가 — git diff $range 실패"
else
fork=$(wt_git -C "$wt" merge-base "$base" HEAD) || wt_die "기본 가지($base)와의 갈림점을 찾지 못했습니다"
committed=$(g diff --name-only "$fork" HEAD)
staged=$(g diff --cached --name-only HEAD)   # staged changes are never "next-generated": always checked
unstaged=$(g diff --name-only)
untracked=$(g ls-files --others --exclude-standard)
changed=$(printf '%s\n%s\n%s\n%s\n' "$committed" "$staged" \
  "$(printf '%s\n' "$unstaged" | filt '^((.*/)?(tsconfig\.json|next-env\.d\.ts)|CLAUDE\.md|AGENTS\.md)$')" \
  "$(printf '%s\n' "$untracked" | filt "$ARTIFACTS")" | sed '/^$/d' | sort -u)
fi

CHANGED="$changed" python3 - "$scope" <<'PY'
import os, re, sys

def to_re(glob: str) -> re.Pattern:
    g = glob.strip()
    if g.startswith("./"):
        g = g[2:]
    if g.endswith("/"):
        g += "**"
    out, i = "", 0
    while i < len(g):
        if g.startswith("**/", i):
            out += "(?:.*/)?"; i += 3
        elif g.startswith("**", i):
            out += ".*"; i += 2
        elif g[i] == "*":
            out += "[^/]*"; i += 1
        elif g[i] == "?":
            out += "[^/]"; i += 1
        else:
            out += re.escape(g[i]); i += 1
    return re.compile(out + r"\Z")

SPECIAL = [
    (re.compile(r"(^|/)package\.json$"), "package.json"),
    (re.compile(r"(^|/)package-lock\.json$"), "package-lock.json"),
    (re.compile(r"(^|/)(pnpm-lock\.yaml|yarn\.lock|npm-shrinkwrap\.json)$"), "lock"),
    (re.compile(r"(^|/)migrations/"), "migrations"),
    (re.compile(r"(^|/)drizzle/"), "drizzle"),
]
lines = [l.strip() for l in open(sys.argv[1], encoding="utf-8") if l.strip() and not l.strip().startswith("#")]
rules = [(l, to_re(l)) for l in lines]
changed = [c for c in os.environ.get("CHANGED", "").splitlines() if c]
bad = []
for path in changed:
    hits = [l for l, r in rules if r.match(path)]
    need = next((word for rx, word in SPECIAL if rx.search(path)), None)
    if need == "lock":
        need_ok = any(("lock" in l) for l in hits)
    elif need:
        need_ok = any(need in l for l in hits)
    else:
        need_ok = True
    if not hits:
        bad.append(f"{path}  (허용 경로 밖)")
    elif not need_ok:
        bad.append(f"{path}  ({need} 는 범위에 이름이 적혀 있어야 함)")
if bad:
    print(f"범위 위반 {len(bad)}개 / 바뀐 파일 {len(changed)}개:")
    print("\n".join("  " + b for b in bad))
    sys.exit(2)
print(f"범위 안 — 바뀐 파일 {len(changed)}개 모두 허용 경로")
PY
