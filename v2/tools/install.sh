#!/usr/bin/env bash
# IF 공방 v2 설치: 엔진 가상환경, gb2 실행 명령, AI 직원 파일, /일 명령.
#
#   bash tools/install.sh --apps-root <앱 저장소 폴더> [--workspace <작업 폴더>]
#                         [--library-mcp "<회사 도서관 MCP 실행 명령>"]
#                         [--direction-file <방향 원문표 경로>]
#
# 이미 있는 직원 파일·스킬은 덮어쓰지 않는다. 다시 실행해도 안전하다.
set -euo pipefail

ENGINE="$(cd "$(dirname "$0")/.." && pwd)"
APPS_ROOT=""
WORKSPACE="$HOME/.gongbang/workspace"
LIBRARY_MCP=""
DIRECTION_FILE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --apps-root) APPS_ROOT="$2"; shift 2 ;;
    --workspace) WORKSPACE="$2"; shift 2 ;;
    --library-mcp) LIBRARY_MCP="$2"; shift 2 ;;
    --direction-file) DIRECTION_FILE="$2"; shift 2 ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 2 ;;
  esac
done
[ -n "$APPS_ROOT" ] || { echo "--apps-root 가 필요하다" >&2; exit 2; }

need() { command -v "$1" >/dev/null 2>&1 || { echo "없음: $1 ($2)" >&2; MISSING=1; }; }
MISSING=0
need python3.12 "엔진 실행"
need git "작업 사본"
need bwrap "작업자 격리(bubblewrap)"
need claude "구현 작업자(Claude Code CLI)"
need codex "검토 작업자(Codex CLI)"
[ "$MISSING" = 0 ] || { echo "위 도구를 먼저 설치한 뒤 다시 실행한다." >&2; exit 1; }

if [ ! -x "$ENGINE/.venv/bin/python" ]; then
  python3.12 -m venv "$ENGINE/.venv"
  "$ENGINE/.venv/bin/python" -m pip install -q pytest
fi

mkdir -p "$HOME/.local/bin" "$WORKSPACE"
cat > "$HOME/.local/bin/gb2" <<EOF
#!/usr/bin/env bash
export GB2_WORKSPACE="\${GB2_WORKSPACE:-$WORKSPACE}"
export GB2_APPS_ROOT="\${GB2_APPS_ROOT:-$APPS_ROOT}"
export GB2_LIBRARY_MCP="\${GB2_LIBRARY_MCP:-$LIBRARY_MCP}"
export GB2_DIRECTION_FILE="\${GB2_DIRECTION_FILE:-$DIRECTION_FILE}"
exec "$ENGINE/.venv/bin/python" -P "$ENGINE/tools/gb2" "\$@"
EOF
chmod +x "$HOME/.local/bin/gb2"

mkdir -p "$HOME/.claude/agents" "$HOME/.claude/skills"
for f in "$ENGINE"/tools/agents/*.md; do
  [ -e "$HOME/.claude/agents/$(basename "$f")" ] || cp "$f" "$HOME/.claude/agents/"
done
for d in "$ENGINE"/tools/skills/*/; do
  name="$(basename "$d")"
  [ -e "$HOME/.claude/skills/$name" ] || cp -r "$d" "$HOME/.claude/skills/$name"
done

echo "설치됨:"
echo "  gb2 명령      $HOME/.local/bin/gb2"
echo "  AI 직원       $HOME/.claude/agents/ ($(ls "$ENGINE"/tools/agents | wc -l)명)"
echo "  /일 명령      $HOME/.claude/skills/일"
echo "확인: gb2 --help"
