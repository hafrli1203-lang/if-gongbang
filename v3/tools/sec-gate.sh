#!/usr/bin/env bash
# Generated from private source at a8eb56b1bac538511499bb39b350117e5eead6e1; do not edit by hand (see harness/export_v3_public.py).
# sec-gate.sh <repo> <from> <to> [--baseline <dir>]
# Security check of one land, before it is merged (gap 34; v2 engine app_security.py rewritten without the engine).
#  1) secrets in what the land ADDS (git diff <from> <to>, added lines only — every app already carries old literals in
#     fixtures and backup scripts, so a whole-repo scan would stop every land): private key blocks, well-known token
#     prefixes (sk-, ghp_, AKIA, xoxb-), password literals (`password = "..."`, `"password": "..."`), and added secret
#     files (.env*, except examples, *.pem, *.key, id_rsa*). Reported as secret:<kind>:<path>:<line>; the value never.
#  2) dependency audit, only when the land changes package.json / a lockfile (every app already has known advisories; a
#     land that does not touch dependencies cannot add one): `npm audit --json` (read-only) in <repo> at <to> against the
#     same at <from>, both read from the commits themselves (git show, never a working folder). New advisories block;
#     ones already there do not. (--baseline is accepted and no longer used.) A lockfile
#     missing, or the audit unable to run (registry/network), blocks too — unless SEC_ALLOW_AUDIT_UNAVAILABLE=1.
#     Python manifests: pip-audit the same way when it is installed; without it a warning (not installed on this server).
# exit 0 = pass, 1 = findings (listed), 2 = usage.
set -euo pipefail
repo="${1:-}"; from="${2:-}"; to="${3:-}"; baseline=""
[ "${4:-}" = --baseline ] && baseline="${5:-}"
[ -n "$repo" ] && [ -n "$from" ] && [ -n "$to" ] || { echo "사용: sec-gate.sh <저장소> <from> <to> [--baseline <폴더>]" >&2; exit 2; }
g() { git -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.quotePath=false -C "$repo" "$@"; }
g rev-parse --verify -q "$from^{commit}" >/dev/null && g rev-parse --verify -q "$to^{commit}" >/dev/null \
  || { echo "보안 검사 불가 — 커밋을 찾지 못했습니다($from / $to)" >&2; exit 2; }

findings=$(mktemp); trap 'rm -f "$findings"' EXIT

# 1) secrets in what the land adds — EVERY commit in <from>..<to> against its first parent, not only the end result:
# a secret committed and removed again later still reaches the remote with the push (Codex 11-02). One file at a time,
# and only lines after the file's first "@@" are content (a content line reading "++ /dev/null" is never a header).
# --text/--no-textconv: a .gitattributes "-diff" or textconv driver must not turn an added secret into "Binary files
# differ". Added secret files are judged by name (status A or T), independent of how their patch renders.
scan_rc=0
python3 - "$repo" "$from" "$to" >> "$findings" <<'PY' || scan_rc=$?
import re, subprocess, sys
repo, a, b = sys.argv[1:]
G = ["git", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", "-c", "core.quotePath=false", "-C", repo]
P = [
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("token_prefix", re.compile(r"\b(?:sk-[A-Za-z0-9_-]{10,}|ghp_[A-Za-z0-9]{10,}|AKIA[0-9A-Z]{12,}|xoxb-[A-Za-z0-9-]{10,})\b")),
    ("password_literal", re.compile(r"[\"']?password[\"']?\s*[:=]\s*[\"'][^\"']{6,}", re.I)),
]
SECRET_FILE = re.compile(r"(^|/)(\.env(\.[^/]*)?|[^/]*\.pem|[^/]*\.key|id_rsa[^/]*|id_ed25519[^/]*)$")
EXAMPLE = re.compile(r"\.(example|sample|template)$")


def run(*args):
    return subprocess.run(G + list(args), check=True, capture_output=True).stdout


def zlist(raw):
    return [x for x in raw.decode("utf-8", "surrogateescape").split("\0") if x]


seen = set()
commits = run("rev-list", "--reverse", f"{a}..{b}").decode().split()
for c in commits:
    parent = f"{c}^1"
    short = c[:8]
    for path in zlist(run("diff", "--name-only", "-z", "--no-renames", "--diff-filter=AT", parent, c)):
        if SECRET_FILE.search(path) and not EXAMPLE.search(path):
            key = ("secret_file", path)
            if key not in seen:
                seen.add(key)
                print(f"secret:secret_file:{path}:0@{short}")
    for path in zlist(run("diff", "--name-only", "-z", "--no-renames", "--diff-filter=AMT", parent, c)):
        out = run("diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv", "--text", "--no-renames",
                  parent, c, "--", path)
        in_body, line = False, 0
        for raw in out.decode("utf-8", "surrogateescape").splitlines():
            if raw.startswith("@@"):
                in_body = True
                m = re.search(r"\+(\d+)", raw)
                line = int(m.group(1)) if m else 0
            elif in_body and raw.startswith("+"):
                for name, pat in P:
                    if pat.search(raw[1:]):
                        print(f"secret:{name}:{path}:{line}@{short}")
                line += 1
PY
[ "$scan_rc" = 0 ] || { echo "보안 검사 불가 — git 조회 실패(코드 $scan_rc)" >&2; exit 2; }

# 2) dependency audit when the land touches dependencies
changed=$(g diff --name-only -z "$from" "$to" | tr '\0' '\n')   # quotePath off + -z: no quoted names
advisories() {  # $1 dir -> sorted advisory keys, or the word UNAVAILABLE
  local out rc=0
  out=$(cd "$1" && timeout 120 npm audit --json 2>/dev/null) || rc=$?
  printf '%s' "$out" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("UNAVAILABLE"); sys.exit()
if isinstance(d.get("error"), dict):
    print("UNAVAILABLE"); sys.exit()
keys = set()
for name, v in (d.get("vulnerabilities") or {}).items():
    for via in v.get("via", []):
        if isinstance(via, dict):
            keys.add(str(via.get("source") or via.get("url") or name + ":" + str(v.get("severity"))))
for k in sorted(keys):
    print(k)
'
}
PKG_RE='(^|/)(package\.json|package-lock\.json|npm-shrinkwrap\.json)$'
snaps=$(mktemp -d); trap 'rm -f "$findings"; rm -rf -- "${snaps:?}"' EXIT
snapshot() {  # $1 commit, $2 folder ("." = root) -> a temp folder with that folder's package files as committed
  local out f p; out="$snaps/$(printf '%s' "$1:$2" | sha256sum | cut -c1-12)"; mkdir -p "$out"
  for f in package.json package-lock.json npm-shrinkwrap.json; do
    if [ "$2" = . ]; then p="$f"; else p="$2/$f"; fi
    g show "$1:$p" > "$out/$f" 2>/dev/null || rm -f "$out/$f"
  done
  echo "$out"
}
if [ "${SEC_SKIP_AUDIT:-0}" != 1 ] && grep -qE "$PKG_RE" <<< "$changed"; then
  # each Node project the land touches is audited in its own folder (an app may keep it in a subfolder such as web/)
  while IFS= read -r d; do
    [ -n "$d" ] || continue
    # audited from the commits themselves, never a working folder: what is audited is what lands (Codex 11-02)
    snap_new=$(snapshot "$to" "$d"); snap_old=$(snapshot "$from" "$d")
    if [ ! -f "$snap_new/package-lock.json" ]; then
      echo "audit:no_lockfile:$d/package-lock.json" >> "$findings"; continue
    fi
    new=$(advisories "$snap_new")
    old=""; [ -f "$snap_old/package-lock.json" ] && old=$(advisories "$snap_old")
    if [ "$new" = UNAVAILABLE ] || [ "$old" = UNAVAILABLE ]; then
      if [ "${SEC_ALLOW_AUDIT_UNAVAILABLE:-0}" = 1 ]; then echo "경고 — npm audit를 돌리지 못했습니다($d, 허용됨)" >&2
      else echo "audit:unavailable:npm:$d" >> "$findings"; fi
    else
      comm -13 <(printf '%s\n' "$old" | sed '/^$/d' | sort -u) <(printf '%s\n' "$new" | sed '/^$/d' | sort -u) \
        | sed "s#^#audit:new_advisory:$d:#" >> "$findings"
      echo "npm audit($d) — 이 land 전 $(grep -c . <<< "$old" || true)건, 뒤 $(grep -c . <<< "$new" || true)건" >&2
    fi
  done < <(grep -E "$PKG_RE" <<< "$changed" | sed -E 's#(^|/)[^/]+$##; s#^$#.#' | sort -u)
fi
if [ "${SEC_SKIP_AUDIT:-0}" != 1 ] && grep -qE '(^|/)(requirements[^/]*\.txt|pyproject\.toml)$' <<< "$changed"; then
  if command -v pip-audit >/dev/null 2>&1; then
    pa() { (cd "$1" && timeout 180 pip-audit -f json 2>/dev/null) | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("UNAVAILABLE"); sys.exit()
deps = d.get("dependencies", d) if isinstance(d, dict) else d
for dep in deps or []:
    for v in dep.get("vulns", []):
        print(str(dep.get("name")) + ":" + str(v.get("id")))
' | sort -u; }
    new=$(pa "$repo"); old=""; [ -n "$baseline" ] && old=$(pa "$baseline")
    if [ "$new" = UNAVAILABLE ]; then echo "audit:unavailable:pip" >> "$findings"
    else comm -13 <(printf '%s\n' "$old" | sed '/^$/d') <(printf '%s\n' "$new" | sed '/^$/d') | sed 's/^/audit:new_advisory:/' >> "$findings"; fi
  else
    echo "경고 — Python 의존성이 바뀌었지만 pip-audit가 설치돼 있지 않아 감사하지 못했습니다" >&2
  fi
fi

n=$(grep -c . "$findings" || true)
if [ "$n" -gt 0 ]; then echo "보안 검사 실패 ${n}건:"; sed 's/^/  /' "$findings"; exit 1; fi
echo "보안 검사 통과 — 추가된 줄에 비밀값 없음$(grep -qE "$PKG_RE" <<< "$changed" && [ "${SEC_SKIP_AUDIT:-0}" != 1 ] && echo ', 새 취약점 없음')"
