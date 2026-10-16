#!/usr/bin/env bash
# Packages chosen workspace skills into a Claude Code plugin folder (owner 2026-10-17: "자주 쓰는 것·필요한 것을 스킬화하거나
# 플러그인으로"). Nothing is installed or published: the output is a folder you can point `claude plugin` at, or commit
# into this (private) repository under plugin/. Layout follows the official plugins: .claude-plugin/plugin.json + skills/<name>/SKILL.md.
# usage: make-plugin.sh <plugin-name> <out-dir> <skill-name>... [--from DIR]   (default DIR: ~/.claude/skills)
#   make-plugin.sh if-gongbang-ops ./plugin 월요일-브리핑 광고-운영-점검 스킬-만들기
set -euo pipefail
name="${1:?plugin name}"; out="${2:?output dir}"; shift 2
from="$HOME/.claude/skills"; skills=()
while [ $# -gt 0 ]; do case "$1" in --from) from="$2"; shift 2 ;; *) skills+=("$1"); shift ;; esac; done
[ ${#skills[@]} -gt 0 ] || { echo "skill names required" >&2; exit 2; }
[[ "$name" =~ ^[a-z0-9-]+$ ]] || { echo "plugin name: lowercase, digits, dashes" >&2; exit 2; }
mkdir -p "$out/.claude-plugin" "$out/skills"
copied=()
for s in "${skills[@]}"; do
  src="$from/$s"
  [ -f "$src/SKILL.md" ] || { echo "skill missing: $src/SKILL.md" >&2; exit 1; }
  # Refuse secrets or personal data by file name (MASTER_RULES §3); the skill text itself is the author's responsibility.
  if find "$src" -type f \( -iname '*.env*' -o -iname '*token*' -o -iname '*secret*' -o -iname '*cookie*' \) | grep -q .; then echo "refusing $s: secret-looking file inside" >&2; exit 1; fi
  rm -rf "${out:?}/skills/$s"; mkdir -p "$out/skills/$s"; cp -r "$src/." "$out/skills/$s/"
  copied+=("$s")
done
desc="IF 공방 스킬 묶음: ${copied[*]}"
python3 - "$out/.claude-plugin/plugin.json" "$name" "$desc" <<'EOF'
import json, sys
path, name, desc = sys.argv[1:4]
json.dump({"name": name, "description": desc, "version": "0.1.0", "author": {"name": "IF 공방"}}, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
EOF
cat > "$out/README.md" <<EOF
# $name
IF 공방 스킬 묶음 (자동 생성, $(date +%F)). 들어 있는 스킬: ${copied[*]}
설치(로컬 폴더 마켓플레이스): \`claude plugin marketplace add <이 폴더의 상위 마켓플레이스>\` 또는 폴더째 복사. 자세한 방법은 Claude Code 문서의 plugin 절.
EOF
echo "plugin '$name' -> $out (skills: ${copied[*]})"
