#!/usr/bin/env bash
# Generated from private source at 494f80a1335ecba422757332f15d6b4100b0afaf; do not edit by hand (see harness/export_v3_public.py).
# new-app.sh <app> [--template <dir>]
# Scaffolds a new app folder <apps root>/<app> as a git repository with one first commit (gap 28; v2 `gb2 new`
# rewritten without the engine). Called by `wt-new.sh <app> <name> --new` when the folder does not exist yet.
#  - a template: --template <dir>, else ${WT_APPS_ROOT}/_ops/templates/<WT_TEMPLATE or "next-app">/ when it exists —
#    copied as is;
#  - otherwise a minimal Next.js app in the COMMON_ARCHITECTURE_STANDARD.md layout (src/app, src/server/{application,
#    domain,infrastructure}, src/lib, src/config, tests/), next.config honoring IF_BUILD_DIR, docs/ai/CURRENT_DECISIONS.md.
#    Package versions are copied from an existing app (WT_SCAFFOLD_VERSIONS_FROM, required), never invented.
# Nothing is installed: node_modules, deploy.env (unit/port) and the env file are left to the person setting the app up.
# exit 0 = created, 2 = usage / already exists / no version source.
set -euo pipefail
app="${1:-}"; tpl=""
[ "${2:-}" = --template ] && tpl="${3:-}"
apps="${WT_APPS:-${WT_APPS_ROOT}}"
[[ "$app" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "사용: new-app.sh <앱> [--template <폴더>]" >&2; exit 2; }
dst="$apps/$app"
[ -e "$dst" ] && { echo "이미 있습니다: $dst" >&2; exit 2; }
[ -z "$tpl" ] && [ -d "${WT_APPS_ROOT}/_ops/templates/${WT_TEMPLATE:-next-app}" ] && tpl="${WT_APPS_ROOT}/_ops/templates/${WT_TEMPLATE:-next-app}"

mkdir -p "$dst"
if [ -n "$tpl" ]; then
  [ -d "$tpl" ] || { rmdir "$dst"; echo "템플릿 폴더가 없습니다: $tpl" >&2; exit 2; }
  cp -a "$tpl/." "$dst/"
  echo "템플릿에서 만듦: $tpl" >&2
else
  from="${WT_SCAFFOLD_VERSIONS_FROM:?WT_SCAFFOLD_VERSIONS_FROM 환경변수가 필요합니다 (버전을 베낄 기존 앱의 경로)}/package.json"
  [ -f "$from" ] || { rmdir "$dst"; echo "버전을 가져올 앱이 없습니다: $from" >&2; exit 2; }
  python3 - "$dst" "$app" "$from" <<'PY'
import json, os, re, sys
dst, app, src = sys.argv[1:]
ref = json.load(open(src))
deps = {**ref.get("dependencies", {}), **ref.get("devDependencies", {})}
def pick(names):
    return {n: deps[n] for n in names if n in deps}
pkg = {
    "name": re.sub(r"[^a-z0-9-]", "-", app.lower()),
    "version": "0.1.0",
    "private": True,
    "scripts": {"dev": "next dev -H 127.0.0.1", "build": "next build", "start": "next start -H 127.0.0.1",
                "typecheck": "tsc --noEmit"},
    "dependencies": pick(["next", "react", "react-dom", "zod"]),
    "devDependencies": pick(["typescript", "@types/node", "@types/react", "@types/react-dom"]),
}
missing = [n for n in ("next", "react", "react-dom", "typescript") if n not in deps]
if missing:
    sys.exit(f"기준 앱에 버전이 없습니다: {', '.join(missing)}")
files = {
    "package.json": json.dumps(pkg, indent=2, ensure_ascii=False) + "\n",
    "next.config.ts": 'import type { NextConfig } from "next";\n\nconst nextConfig: NextConfig = {\n'
                      '  // Common deploy builds into IF_BUILD_DIR and swaps it in only after a successful build.\n'
                      '  distDir: process.env.IF_BUILD_DIR ?? ".next",\n};\n\nexport default nextConfig;\n',
    "tsconfig.json": json.dumps({
        "compilerOptions": {"target": "ES2022", "lib": ["dom", "dom.iterable", "esnext"], "strict": True,
                            "noEmit": True, "module": "esnext", "moduleResolution": "bundler", "jsx": "preserve",
                            "esModuleInterop": True, "skipLibCheck": True, "resolveJsonModule": True,
                            "isolatedModules": True, "incremental": True, "plugins": [{"name": "next"}],
                            "paths": {"@/*": ["./src/*"]}},
        "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx"], "exclude": ["node_modules"]}, indent=2) + "\n",
    ".gitignore": "node_modules/\n.next/\n.next-*/\n.env\n.env.*\n!.env.example\n*.log\ntsconfig.tsbuildinfo\nnext-env.d.ts\n",
    "src/app/layout.tsx": 'export default function RootLayout({ children }: { children: React.ReactNode }) {\n'
                          '  return (\n    <html lang="ko">\n      <body>{children}</body>\n    </html>\n  );\n}\n',
    "src/app/page.tsx": f'export default function Home() {{\n  return <main>{app}</main>;\n}}\n',
    "src/server/application/.gitkeep": "", "src/server/domain/.gitkeep": "",
    "src/server/infrastructure/db/.gitkeep": "", "src/server/infrastructure/integrations/.gitkeep": "",
    "src/lib/.gitkeep": "", "src/config/.gitkeep": "", "tests/.gitkeep": "",
    "docs/ai/CURRENT_DECISIONS.md": f"# {app} — 결정 기록

결정은 원문 그대로, 날짜와 함께 적는다.
",
    "README.md": f"# {app}

구조 표준 문서가 있다면 그것을 참고하세요.
",
}
for rel, body in files.items():
    path = os.path.join(dst, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
PY
  echo "최소 Next 앱 틀로 만듦(버전 출처 $from)" >&2
fi
git -c core.hooksPath=/dev/null -C "$dst" init -q -b main
git -c core.hooksPath=/dev/null -C "$dst" add -A
git -c core.hooksPath=/dev/null -C "$dst" commit -q -m "chore: $app 앱 틀 (new-app.sh)"
echo "새 앱 $dst — 첫 커밋 $(git -C "$dst" rev-parse --short HEAD). 남은 일: npm install, deploy.env(UNIT·PORT·HEALTH_PATHS), 서비스 유닛" >&2
