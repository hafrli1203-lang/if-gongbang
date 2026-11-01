# Generated from private source at 5b12eebee24334c8a78ac755dc179c5b1c3f0e47; do not edit by hand (see harness/export_v3_public.py).
# shellcheck shell=bash
# Shared helpers for the worktree tools (wt-new/run/stop/land/gc/migrate, scope-check). Sourced, never run.
# Contract: docs/v3-wt-interface.md. Database URLs and env values are never printed:
# they pass through wt-env.py into a child's environment only.

WT_SHARED="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WT_APPS_ROOT="${WT_APPS_ROOT:?WT_APPS_ROOT 환경변수가 필요합니다 (앱들이 있는 폴더)}"
WT_ROOT="${WT_ROOT:-$WT_APPS_ROOT/_wt}"
WT_DATA="${WT_DATA:?WT_DATA 환경변수가 필요합니다 (작업 DB 슬롯·로그 루트)}"
WT_ADMIN_ENV="${WT_ADMIN_ENV:?WT_ADMIN_ENV 필요 — 시험 DB 생성 권한이 있는 postgres 역할의 env 파일 경로}"   # postgres role that creates/resets test DBs
WT_APP_MAX="${WT_APP_MAX:-3}"; WT_GLOBAL_MAX="${WT_GLOBAL_MAX:-8}"; WT_SLOTS="${WT_SLOTS:-3}"   # copies without .wt.done; test-DB slots per app
WT_ENVPY="$WT_SHARED/wt-env.py"

wt_die() { echo "$*" >&2; exit "${WT_EXIT:-1}"; }

wt_check_name() {  # $1 kind, $2 value
  case "$1" in
    app) [[ "$2" =~ ^[A-Za-z0-9._-]+$ ]] && [ -d "$WT_APPS_ROOT/$2/.git" ] || wt_die "앱 이름이 이상하거나 git 저장소가 아닙니다: $2" ;;
    name) [[ "$2" =~ ^[a-z0-9-]+$ ]] || wt_die "사본 이름은 [a-z0-9-]+ 만: $2" ;;
    existing) [[ "$2" =~ ^[A-Za-z0-9._-]+$ ]] || wt_die "사본 이름이 이상합니다: $2" ;;   # adopted hand-made copies
  esac
}

wt_app_env() {  # prints the app's env file path (.env.local, else .env), empty when none
  local d="$WT_APPS_ROOT/$1"
  if [ -f "$d/.env.local" ]; then echo "$d/.env.local"; elif [ -f "$d/.env" ]; then echo "$d/.env"; fi
}

wt_base_branch() { git -C "$WT_APPS_ROOT/$1" rev-parse --abbrev-ref HEAD; }

# .wt.json read/write (python, no jq dependency)
wt_json_get() {  # $1 file, $2 key -> value ('' when missing; lists joined by space)
  python3 - "$1" "$2" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
v = d.get(sys.argv[2])
if v is None:
    sys.exit(0)
print(" ".join(map(str, v)) if isinstance(v, list) else v)
PY
}

wt_json_set() {  # $1 file, then key=value pairs; value "[]" empties a list, "+x" appends to a list, "-x" removes from it
  python3 - "$@" <<'PY'
import json, os, sys
path = sys.argv[1]
d = json.load(open(path)) if os.path.exists(path) else {}
for kv in sys.argv[2:]:
    k, _, v = kv.partition("=")
    if v == "[]":
        d[k] = []
    elif v.startswith("+"):
        d[k] = [*d.get(k, []), v[1:]]
    elif v.startswith("-"):
        d[k] = [x for x in d.get(k, []) if x != v[1:]]
    elif v.isdigit():
        d[k] = int(v)
    elif v == "null":
        d[k] = None
    else:
        d[k] = v
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(d, f, ensure_ascii=False)
os.replace(tmp, path)
PY
}

wt_active_dirs() {  # $1 app or '' for all -> worktree dirs that are not finished (.wt.done absent)
  local pat="$WT_ROOT/${1:-*}/*" d
  for d in $pat; do
    [ -d "$d" ] || continue
    [ -e "$d/.wt.done" ] && continue
    echo "$d"
  done
}

# DB change files: migrations/ (most apps) and drizzle/ (drizzle-kit apps)
WT_MIG_RE='(^|/)(migrations|drizzle)/'
wt_scope_has_migrations() {  # $1 scope file
  [ -f "$1" ] && grep -qE '(^|/)(migrations|drizzle)(/|$)' "$1"
}

# Test DB slots: ${WT_DATA}/<app>/slot.<N>.lock holds the owning worktree path. Claims and releases run under
# a flock on the app's slots.mutex so two wt-new calls never take the same slot.
wt_slot_claim() {  # $1 app, $2 worktree path -> prints N
  local app="$1" wt="$2" n f owner
  mkdir -p "$WT_DATA/$app"
  exec 7>"$WT_DATA/$app/slots.mutex"; flock 7
  for n in $(seq 1 "$WT_SLOTS"); do
    f="$WT_DATA/$app/slot.$n.lock"
    owner=$(cat "$f" 2>/dev/null || true)
    if [ -z "$owner" ] || [ ! -d "$owner" ] || [ -e "$owner/.wt.done" ]; then
      printf '%s\n' "$wt" > "$f"; flock -u 7; exec 7>&-; echo "$n"; return 0
    fi
  done
  flock -u 7; exec 7>&-; return 1
}

wt_slot_release() {  # $1 app, $2 worktree path
  local app="$1" wt="$2" f
  [ -d "$WT_DATA/$app" ] || return 0
  exec 7>"$WT_DATA/$app/slots.mutex"; flock 7
  for f in "$WT_DATA/$app"/slot.*.lock; do
    [ -f "$f" ] && [ "$(cat "$f" 2>/dev/null)" = "$wt" ] && rm -f "$f"
  done
  flock -u 7; exec 7>&-
}

wt_verify_env() {  # $1 copy, $2 app -> the copy's .claude/verify-env.json, else the app folder's (path may not exist)
  if [ -f "$1/.claude/verify-env.json" ]; then echo "$1/.claude/verify-env.json"; else echo "$WT_APPS_ROOT/$2/.claude/verify-env.json"; fi
}

wt_pg_admin() {  # run a command with PG* set to the admin role on db $1
  local db="$1"; shift
  python3 "$WT_ENVPY" pg --env-file "$WT_ADMIN_ENV" --db "$db" -- "$@"
}

wt_unit_alive() {  # $1 unit -> 0 when systemd still knows it as active/activating
  local s; s=$(systemctl --user is-active "$1" 2>/dev/null || true)
  [ "$s" = active ] || [ "$s" = activating ] || [ "$s" = reloading ]
}
