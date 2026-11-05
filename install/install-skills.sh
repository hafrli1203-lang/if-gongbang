#!/usr/bin/env bash
# install-skills.sh [manifest.json]
#
# Re-installs, on a NEW machine, the public-origin plugin marketplaces and plugins this
# install actually uses - from skills-manifest.json (default: the file next to this script).
# Registers each marketplace from ITS OWN public git/GitHub address (never from a copy of its
# code kept in this repository - there is none), installs each listed plugin, then enables
# only the ones the manifest marks enabled. Idempotent: `claude plugin marketplace add` and
# `claude plugin install` are safe to re-run against an already-registered/installed target.
#
# What this intentionally does NOT install: a marketplace registered on the machine that built
# this manifest from a local directory (no public git remote), or any skill built from this
# company's own private material or task playbooks - see skills-manifest.json's own "_comment"
# and docs/v3.md's "Skills install list" section for why those are left out rather than copied
# in as code.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="${1:-$HERE/skills-manifest.json}"
[ -f "$MANIFEST" ] || { echo "매니페스트가 없습니다: $MANIFEST" >&2; exit 2; }
command -v claude >/dev/null 2>&1 || { echo "claude CLI가 PATH에 없습니다." >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "python3이 필요합니다." >&2; exit 2; }

echo "마켓플레이스 등록..."
python3 - "$MANIFEST" <<'PY' | while IFS=$'\t' read -r name addr; do
import json, sys
m = json.load(open(sys.argv[1]))
for mk in m.get("marketplaces", []):
    addr = mk.get("repo") or mk.get("url")
    if addr:
        print(f"{mk['name']}\t{addr}")
PY
  echo "  - $name <- $addr"
  claude plugin marketplace add "$addr" >/dev/null 2>&1 || echo "    (등록 실패 또는 이미 있음: $name)"
done

echo "플러그인 설치..."
python3 - "$MANIFEST" <<'PY' | while IFS=$'\t' read -r id enabled; do
import json, sys
m = json.load(open(sys.argv[1]))
for p in m.get("plugins", []):
    print(f"{p['id']}\t{'1' if p.get('enabled') else '0'}")
PY
  echo "  - $id"
  claude plugin install "$id" >/dev/null 2>&1 || echo "    (설치 실패 또는 이미 있음: $id)"
  if [ "$enabled" = 1 ]; then
    claude plugin enable "$id" >/dev/null 2>&1 || echo "    (켜기 실패: $id)"
  fi
done

echo "완료 — skills-manifest.json에 적힌 마켓플레이스·플러그인을 설치/켜기 시도했습니다."
echo "자동 스킬 선택기는 아직 이 목록에 없습니다(skills-manifest.json의 skill_router 키 참고)."
