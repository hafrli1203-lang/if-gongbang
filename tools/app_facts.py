#!/usr/bin/env python3
# Generated from the private ops workspace at 24c1bb68758c8a552514d06d8280463876278f76; do not edit by hand (see harness/export_ops_public.py).
"""App fact cards (2026-11-01, owner: "개발 구조를 모든 앱이 알아서 자동으로 보고 거기에 맞게 하게 해라").

Writes /apps/_ops/app-facts/<app>.md for every app with a deploy.env: the facts a fresh agent must know before
touching the app (which DB is production, where tests run, how to deploy, what not to touch) plus the common working method.
Each app's CLAUDE.md imports its card (@-import), and the crew agent definitions read it at start, so nobody has to be told.
Facts come from files only (deploy.env, package.json, git, test config). .env files are never opened — only their names.
Run: python3 /apps/_ops/tools/app_facts.py   (also nightly, see app-facts.timer)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

APPS = Path("/apps")
OUT = APPS / "_ops" / "app-facts"
SHIP = "/apps/_shared/ship.sh"
DEPLOY = "/apps/_shared/deploy.sh"

METHOD = """## 일하는 법 (모든 앱 공통 — 이것만 지키면 된다)
0. 먼저 `/apps/_ops/PSTACK_KO.md`와 poteto-mode SKILL.md(실제 경로: `~/.claude/skills-library/03-에이전트·하네스·오케스트레이션/pstack/skills/poteto-mode/SKILL.md`)를 읽는다(2026-11-01 pstack 도입).
1. 시작: 이 카드(위 「지난 실수」 포함 — 첫 보고 첫 줄을 `지난 실수: L1-xxxx` 또는 `지난 실수: 걸리는 것 없음`으로 쓴다) → 그 앱 `docs/ai/CURRENT_DECISIONS.md`의 해당 절을 읽는다. 더 오래된 실수는 `lessons_for(app)`. 운영자 말은 원문 그대로 옮긴다.
2. 통과 장면부터: "사람이 하는 일 → 앱이 보여 주는 결과 → 화면 위치"를 먼저 적고 만든다. 입력 칸만 만든 것은 구현이 아니다.
3. 운영 DB에 쓰지 않는다: 시험·dev 서버·seed는 아래 「시험 DB」만. 운영 마이그레이션은 백업 뒤 배포 때 한 번. dev 서버·임시 빌드 폴더(distDir)는 앱 폴더가 아니라 자기 scratchpad 아래에 둔다(앱 폴더에 남은 .next-* 가 린트·Tailwind 검사를 3만 건 넘게 오염시킴, 2026-11-01).
4. 완료는 증거로: 화면이면 실제로 눌러 본 캡처(1440·390), 데이터면 조회 결과, 코드면 이 앱 CI 범위 전체 실패 0. 증거가 없으면 "미검증: 무엇"이라고 쓴다 — 그건 완료가 아니다. 설치·연결·입구 일(스킬·자료·직원 매뉴얼·지시 판별)은 운영자가 실제로 쓰는 자리(카톡 지시 한 줄·the office app 화면)에 1건 넣어 그 자산이 읽히고 결과가 돌아온 출력까지가 완료다 — 없으면 "만듦(통과 장면 미검증)"(L1-0253·0254·0258·0259).
5. 떨어지면 고친다: 같은 점검을 다시 돌리지 말고 지적 자리를 고친다. 같은 지적이 두 번이면 방법을 바꾼다(더 단순하게). 점검이 할 수 없는 것을 요구하면 기준을 바로잡는다.
6. 문제는 스스로 찾는다: 작업하다 본 다른 결함(오류·누락·느림·이상한 화면)은 범위 안이면 고치고, 밖이면 보고 끝에 「발견한 문제」로 적는다.
7. 보고: 한국어. 한 것(커밋·화면·숫자) / 미검증 / 발견한 문제. 하지 않은 일을 했다고 쓰지 않는다. 출처를 못 찾은 사실·읽지 않은 파일은 "출처 못 찾음 — 확인 필요"·"목록만 봄"으로 쓰고 참·거짓을 단정하지 않는다(L1-0242·0244). 운영자에게 가는 글은 쉬운 말 3~5줄(한 줄에 한 사실: 됐다/안 됐다/운영자가 할 일)을 먼저, 해시·경로·영어 약어는 그 아래 「상세」로(L1-0260).
8. 운영자에게 묻기 전에 확인한다: "있나/했나/어디에 있나"는 상태 파일·DB·설정·작업판·앱이 가진 자료에서 먼저 찾는다. 운영자에게는 서버에서 알 수 없는 것만 묻는다(L1-0229·0211).
9. 승인을 청하지 않는다: 맡은 범위 안의 배포·패키지·push·DB 변경(백업 뒤)은 바로 하고 보고한다. 삭제·이동은 백업 뒤 진행·보고. 훅이 막으면 한 줄 알리고 다른 길로 완성한다(L1-0250·0232). 단, 운영자가 "봐라·조사해라·보고해라"만 한 일은 고치지 않고 보고만 한다(L1-0234).
10. 옮기기·대체·전환 일은 고치기 전에 「지금 있는 것 → 새 자리」 대조표부터: 켜진 것(서비스·타이머·훅 연결·cron)·옛 경로 기능 전수·운영자가 등록한 자산(스킬 분류·서재·도구)을 서버에서 실측해 항목마다 있음/옮김/뺌(이유)으로 채우고, 빈 칸 0일 때만 "대체"라고 쓴다(L1-0243·0247·0248·0249).
11. 기준은 운영자 원문만: 성공 기준·숫자·분류 기준·규칙 줄은 운영자가 말한 것만 쓴다. 내 제안은 「제안(운영자 확인 전)」으로 따로 적고 결정 근거로 쓰지 않으며, 운영자가 정한 기준을 다른 규칙을 끌어와 좁히지 않는다(우려는 한 줄로 보고). 분모·기한·하향 조건을 운영자에게 묻지 않는다(L1-0251·0252·0255·0257).
"""


def sh(*args: str, cwd: Path | None = None) -> str:
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def read_env_kv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"')
    return out


TEST_DB_SCAN_FILES = [
    "vitest.config.ts", "vitest.config.mts", "vitest.config.js", "scripts/setup-test-db.sh",
    "scripts/create-db.sh", "tests/setup.ts", "test/setup.ts", "web/vitest.config.ts",
    "tests/setup/db-guard.ts", "tests/helpers/setup.ts", "src/lib/db-guard.ts",
]
# 11-01 exampleapp 사고(team-lead 지적): 시험 db 이름이 "보이기만" 해선 안전하지 않다 — 그 이름으로
# 실제로 "강제 전환"하고 "운영이면 멈추는" 코드가 있어야 가드다. 이 4개 앱(exampleapp·exampleapp·
# exampleapp·search)에 똑같은 모양(checkTestDbUrl/db-guard.ts, 운영 db 이름 그대로면 throw)으로
# 넣었다 — 그 서명을 찾으면 "가드 있음", 시험 db 단서는 있는데 그 서명이 없으면 "가드 없음"(경고).
GUARD_SIGNATURES = ("checkTestDbUrl", "db-guard", "[db-guard]")


SQLITE_DEPS = ("better-sqlite3", "sqlite3", "@libsql/client")
TEMP_DB_MARKERS = ("mkdtemp", ":memory:", "tmpdir()")
# 11-03 self-check: exampleapp integration tests run only when DATABASE_URL is passed by hand
# (`runIf = process.env.DATABASE_URL ? describe : describe.skip`) and nothing checks that it is not production.
ENV_GATED_DB = re.compile(r"process\.env\.DATABASE_URL\s*\?\s*describe\s*:\s*describe\.skip")


def pkg_deps(app_dir: Path) -> dict[str, str]:
    try:
        pkg = json.loads((app_dir / "package.json").read_text())
    except Exception:
        return {}
    return {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}


def test_files(app_dir: Path):
    for base in ("tests", "test", "src"):
        for suffix in (".test.ts", ".test.js", ".test.mts", ".test.tsx"):
            yield from walk_files(app_dir / base, suffix)


def special_db_hint(app_dir: Path) -> str:
    """Hints test_db_hint() cannot give: sqlite apps (tests on temp files, not a named test DB) and pg apps whose
    DB tests are gated on a hand-passed DATABASE_URL with no guard (11-03: exampleapp was flagged "no test DB" though
    every DB test uses mkdtemp; exampleapp had no flag at all for the real risk)."""
    deps = pkg_deps(app_dir)
    temp_hits = gated = 0
    for f in test_files(app_dir):
        try:
            txt = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        temp_hits += any(m in txt for m in TEMP_DB_MARKERS)
        gated += bool(ENV_GATED_DB.search(txt))
    if any(d in deps for d in SQLITE_DEPS):
        now = (f"시험은 임시 파일(mkdtemp·:memory:)로 돈다(시험 파일 {temp_hits}개)" if temp_hits
               else "임시 파일을 쓰는 DB 시험이 아직 없다")
        return (f"sqlite 앱: {now}. 운영 파일은 코드 기본값 경로 — "
                "사본 실행은 `_shared/wt-env.py`가 사본 데이터 폴더로 바꾼다. 새 시험은 임시 파일만 쓴다.")
    if gated:
        return (f"**DATABASE_URL을 손으로 넣어야 도는 DB 시험 {gated}개, 운영인지 막는 가드 없음** — 운영 URL을 넣고 돌리지 않는다. "
                "시험 DB를 만들고 가드를 넣기 전에는 그 시험을 건너뛴 채(기본) 둔다.")
    if not any(k in deps for k in ("pg", "postgres", "drizzle-orm", "@prisma/client", "mysql2")):
        for rel in ("vitest.global-setup.ts", "vitest.setup.ts", "tests/setup.ts"):
            f = app_dir / rel
            if f.exists() and "mkdtemp" in f.read_text(encoding="utf-8", errors="replace"):
                return (f"파일 저장 앱(DB 의존 없음): 시험은 임시 폴더 복사본에서 돈다(`{rel}`). "
                        "운영 자료 폴더에 직접 쓰는 시험·스크립트를 만들지 않는다.")
        return "package.json에 DB 의존 없음 — DB를 쓰는 일이면 어디에 저장하는지 먼저 확인(확인 필요)."
    return ""


def test_db_hint(app_dir: Path) -> str:
    hits: list[str] = []
    guard_found = False
    for rel in TEST_DB_SCAN_FILES:
        p = app_dir / rel
        if not p.exists():
            continue
        txt = p.read_text(encoding="utf-8", errors="replace")
        for name in re.findall(r"[A-Za-z0-9_]*_test\b|TEST_DATABASE_URL|DATABASE_URL_TEST", txt):
            hits.append(f"`{name}` ({rel})")
        if any(sig in txt for sig in GUARD_SIGNATURES):
            guard_found = True
    hits = list(dict.fromkeys(hits))
    if not hits:
        special = special_db_hint(app_dir)
        if special:
            return special
        return "시험 DB 단서를 파일에서 찾지 못함 — 시험이 운영 DB를 쓰는지 먼저 확인하고, 운영이면 시험 DB를 만든 뒤 시작(확인 필요)."
    base = "시험 DB 단서: " + ", ".join(hits[:4]) + " — 시험·dev 서버는 이쪽으로만."
    if guard_found:
        return base + " 시험 가드 있음(운영 db 이름이면 시험 전체가 멈춘다, db-guard.ts)."
    return base + " **시험 가드 없음 — 이 앱 시험은 운영 DB로 돈다, 시험 전에 가드부터 넣어라**(이름만 비슷하고 강제·중단이 없으면 여전히 위험하다)."


# 2026-11-01 "사본 실행" 절(plans/2026-11-01-wt-interface.md) — wt-new.sh/wt-run.sh가 사본에서
# dev·migrate를 돌릴 때 쓸 명령·스키마·호스트 검사 필요 여부를 package.json·마이그레이션 SQL·
# deploy.env(WT_DEV_CMD/WT_MIGRATE_CMD)에서만 뽑는다. 지어낸 값은 "확인 필요"로 남긴다.
EXCLUDE_DIRS = {
    "node_modules", ".git", "dist", "build", "out", ".next", "coverage",
    ".venv", "__pycache__", ".cache", "logs", "tmp", "vendor", "target",
}


def walk_files(base: Path, suffix: str):
    if not base.exists():
        return
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".next")]
        for f in files:
            if f.endswith(suffix):
                yield Path(root) / f


def dev_cmd(app_dir: Path, env: dict[str, str]) -> str:
    override = env.get("WT_DEV_CMD")
    if override:
        return override + "(deploy.env WT_DEV_CMD)"
    scripts = {}
    try:
        scripts = json.loads((app_dir / "package.json").read_text()).get("scripts", {})
    except Exception:
        pass
    dev = scripts.get("dev", "")
    if "next" in dev:
        cmd = re.sub(r"-p\s+\d+", "-p {PORT}", dev)
        if "{PORT}" not in cmd:
            cmd = cmd.rstrip() + " -p {PORT}"
        if "-H" not in cmd:
            cmd += " -H 127.0.0.1"
        return cmd
    return "확인 필요(next dev 아님 — deploy.env에 WT_DEV_CMD 넣을 것)"


def migrate_cmd(app_dir: Path, env: dict[str, str]) -> str:
    override = env.get("WT_MIGRATE_CMD")
    if override:
        return override + "(deploy.env WT_MIGRATE_CMD)"
    try:
        pkg = json.loads((app_dir / "package.json").read_text())
    except Exception:
        pkg = {}
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    if any("drizzle" in k for k in deps):
        return "npx drizzle-kit migrate"
    return "확인 필요(drizzle 없음 — deploy.env에 WT_MIGRATE_CMD 넣을 것)"


SCHEMA_RE = re.compile(r"CREATE SCHEMA(?:\s+IF NOT EXISTS)?\s+([A-Za-z0-9_]+)", re.IGNORECASE)


def schemas(app_dir: Path) -> str:
    found: list[str] = []
    for p in walk_files(app_dir, ".sql"):
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in SCHEMA_RE.findall(txt):
            if m.lower() != "public" and m not in found:
                found.append(m)
    return ", ".join(["public", *found]) if found else "public"


def host_check(app_dir: Path) -> str:
    for suffix in (".ts", ".tsx"):
        for p in walk_files(app_dir / "src", suffix):
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if "allowedHosts" in txt or "APP_HOSTS" in txt:
                return "VERIFY_ALLOWED_HOST 필요"
    return "불필요"


# 2026-11-01 team-lead 지시: 오늘 search 빌드가 tmp/ 519MB(.gitignore 안 됨)를 Turbopack이
# 스캔하다 6G에서 OOM(exampleapp·exampleapp과 같은 원인). git이 이미 추적도 무시도 안 하는
# 디렉터리(기본 `git status --porcelain`이 untracked dir를 묶어서 보여주는 것)만 보고 크기를 잰다
# — 지어낸 기준 없음, 50MB는 team-lead 지시값.
BUILD_SCAN_RISK_MB = 50


def build_scan_risk(d: Path) -> list[str]:
    status = sh("git", "status", "--porcelain", cwd=d)
    out: list[str] = []
    for line in status.splitlines():
        if not line.startswith("??"):
            continue
        rel = line[3:].strip()
        if not rel.endswith("/"):
            continue
        full = d / rel
        if not full.is_dir():
            continue
        try:
            du = subprocess.run(["du", "-sm", str(full)], capture_output=True, text=True, timeout=20).stdout
            mb = int(du.split()[0]) if du.strip() else 0
        except Exception:
            continue
        if mb >= BUILD_SCAN_RISK_MB:
            out.append(f"- **빌드 스캔 위험**: `{rel}` {mb}MB, `.gitignore`에 없음 — 넣을 것(11-01 search tmp/ 519MB 6G OOM과 같은 원인).")
    return out


ALERTS = APPS / "_ops" / "ops" / "ALERTS.md"


def deploy_state(app: str, d: Path) -> list[str]:
    """Where production is vs HEAD, and open alerts — a fresh agent must not redo or skip a stuck deploy (11-01 exampleapp)."""
    out: list[str] = []
    deployed_f = Path(f"/data/_deploy-prev/{app}.review/deployed")
    if deployed_f.exists():
        dep = deployed_f.read_text().strip()
        ahead = sh("git", "rev-list", "--count", f"{dep}..HEAD", cwd=d)
        state = "운영과 같음" if ahead == "0" else (f"운영에 안 나간 커밋 {ahead}개 — 이어서 일하기 전에 `git log {dep[:7]}..HEAD`로 무엇인지 본다" if ahead else "확인 필요")
        out.append(f"- 배포 상태(카드 생성 때): 운영 커밋 `{dep[:7]}` · {state}.")
    try:
        open_alerts = [l for l in ALERTS.read_text(encoding="utf-8").splitlines() if l.startswith("- [ ]") and f'"app": "{app}"' in l]
    except OSError:
        open_alerts = []
    if open_alerts:
        out.append(f"- 열린 운영 경보 {len(open_alerts)}건(`_ops/ops/ALERTS.md`) — 최근: " + open_alerts[-1][6:110].replace("`", "'") + " …")
    return out


def decisions_doc_ref(d: Path) -> str:
    """2026-11-02 wiring audit: exampleapp·exampleapp have no docs/ai/CURRENT_DECISIONS.md,
    so the card was pointing at a file that doesn't exist. Check the real path and fall back
    truthfully instead of hardcoding the common one."""
    if (d / "docs" / "ai" / "CURRENT_DECISIONS.md").exists():
        return "`docs/ai/CURRENT_DECISIONS.md`"
    if (d / "docs" / "DECISIONS.md").exists():
        return "없음(docs/DECISIONS.md 사용)"
    return "없음(결정 문서 자체가 없음)"


LIBRARY_MCP = "/apps/exampleapp/bin/mcp.sh"
LESSONS_PER_APP = 5
LESSON_HEAD = re.compile(r"^\[(L1-\d+)[^\]]*\]\s*", re.M)


def fetch_lessons(apps: list[str]) -> dict[str, list[str]]:
    """11-03 self-check: crews read the card (18/25) far more than they call lessons_for (11/25) — the card read
    already happens, so the app's recent lessons ride along in it. Talks to the library MCP server over stdio (the
    same server the crews use), so no credentials are touched here. Returns {} on any failure; the card then says so."""
    out: dict[str, list[str]] = {}
    try:
        proc = subprocess.Popen([LIBRARY_MCP], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return out
    try:
        def call(obj: dict) -> dict:
            assert proc.stdin and proc.stdout
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()
            return json.loads(proc.stdout.readline() or "{}") if "id" in obj else {}
        call({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "app_facts", "version": "1"}}})
        call({"jsonrpc": "2.0", "method": "notifications/initialized"})
        for i, app in enumerate(apps, start=1):
            res = call({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                        "params": {"name": "lessons_for", "arguments": {"app": app, "limit": LESSONS_PER_APP}}})
            text = ((res.get("result") or {}).get("content") or [{}])[0].get("text", "")
            out[app] = summarize_lessons(text)
    except Exception:
        pass
    finally:
        proc.terminate()
    return out


def summarize_lessons(text: str) -> list[str]:
    """One line per lesson: id + its "다음부터" rule (the part a fresh crew must apply), capped for card length."""
    items: list[str] = []
    heads = list(LESSON_HEAD.finditer(text))
    for n, m in enumerate(heads):
        body = text[m.end(): heads[n + 1].start() if n + 1 < len(heads) else len(text)]
        rule = body.split("다음부터:", 1)[-1].split("\n", 1)[0].strip()
        rule = re.sub(r"^다음부터:\s*", "", rule)
        items.append(f"- {m.group(1)}: " + (rule[:220] + "…" if len(rule) > 220 else rule))
    return items


def lessons_section(app: str, lessons: dict[str, list[str]] | None) -> list[str]:
    # Placed right under the app facts (11-04: crews that read only the first 15 lines missed it at the bottom; 3/17 app-edit
    # crews wrote the number). The fixed first-line form is what crew_metrics can count.
    head = (f"## 지난 실수 (자동, `lessons_for({app})` 최근 {LESSONS_PER_APP}건) — 첫 보고 첫 줄: "
            "`지난 실수: L1-xxxx`(걸리는 번호) 또는 `지난 실수: 걸리는 것 없음`")
    if lessons is None or app not in lessons:
        return [head, "- 자동 조회 실패 — 시작할 때 `lessons_for(app)`를 직접 부른다."]
    return [head, *(lessons[app] or ["- 기록된 지난 실수 없음."])]


def card(app: str, lessons: dict[str, list[str]] | None = None) -> str:
    d = APPS / app
    env = read_env_kv(d / "deploy.env")
    app_dir = Path(env["APP_DIR"]) if env.get("APP_DIR", "").startswith("/") else (d / env["APP_DIR"] if env.get("APP_DIR") else d)
    pkg = {}
    try:
        pkg = json.loads((app_dir / "package.json").read_text())
    except Exception:
        pass
    scripts = pkg.get("scripts", {})
    ci = [s for s in ("typecheck", "type-check", "lint", "test", "build") if s in scripts]
    env_files = sorted(p.name for p in app_dir.glob(".env*") if p.name != ".env.example")
    remote = re.sub(r"https://|\.git$", "", sh("git", "remote", "get-url", "origin", cwd=d))
    review = env.get("REVIEW_GATE") == "1"
    unit = env.get("UNIT", f"ifapp-{app}")
    worker = env.get("WORKER_UNIT", "")
    lines = [
        f"# 사실 카드 — {app} (자동 생성, 손으로 고치지 말 것: `_ops/tools/app_facts.py`)",
        "",
        "## 이 앱의 사실",
        f"- 폴더 `{app_dir}` · 서비스 `{unit}`" + (f" + 작업기 `{worker}`" if worker else "") + f" · 포트 {env.get('PORT', '?')}",
        f"- **운영 DB = 이 앱의 환경 파일({', '.join(env_files) or '없음'})이 가리키는 DB.** 운영 서비스가 그 파일을 읽는다. 개발 DB는 따로 없다고 보고 일한다. 환경 파일은 열지도 출력하지도 않는다.",
        f"- {test_db_hint(app_dir)}",
        f"- CI와 같은 검사 범위: " + (", ".join(f"`npm run {s}`" for s in ci) or "package.json에 검사 스크립트 없음(확인 필요)") + " — push·배포·「완료」 전에 실패 0.",
        f"- 배포: `bash {SHIP if review else DEPLOY} {app}`" + (" (자동 수정·화면 검수 포함)" if review else "") + " — 빌드는 한 번에 하나, timeout으로 감싸지 않는다.",
        f"- 원격: {remote or '없음'} · 커밋은 `git commit -- <내 파일>`만(stash·reset·checkout·add -A 금지, 다른 세션 변경은 건드리지 않음).",
        "- 손대지 않는 것: `.env*`, `deploy.env`, 다른 앱 폴더, 서버 설정. 운영자 결정 원문: " + decisions_doc_ref(d) + ".",
        *deploy_state(app, d),
        *build_scan_risk(d),
        "",
        *lessons_section(app, lessons),
        "",
        "## 사본 실행 (`_shared/wt-new.sh`·`wt-run.sh`가 쓰는 값, 2026-11-01)",
        f"- DEV_CMD: `{dev_cmd(app_dir, env)}`",
        f"- MIGRATE_CMD: `{migrate_cmd(app_dir, env)}`",
        f"- SCHEMAS: {schemas(app_dir)}",
        f"- HOST_CHECK: {host_check(app_dir)}",
        *([
            "- 실데이터 확인(ship.sh 두 번째 인자)은 `scripts/zz-live-<이름>.ts`에 둔다. `tests/`에 두면 배포 시험이 DB 환경 없이 그것까지 돌려 깨지고 자동 수정도 그 파일을 못 본다(11-01 14:57). ship.sh는 커밋 안 된 시험 파일이 있으면 시작하지 않는다.",
            "- Codex가 `P1 [LIVE]`(실데이터 확인 부족)만 내면 ship.sh가 확인 스크립트를 지적대로 다시 만든다 — 코드를 고치지 말고 확인을 고친다.",
        ] if review else []),
        "",
        METHOD,
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    apps = sorted(p.parent.name for p in APPS.glob("*/deploy.env"))
    lessons = fetch_lessons(apps)
    for app in apps:
        (OUT / f"{app}.md").write_text(card(app, lessons), encoding="utf-8")
    print(f"사실 카드 {len(apps)}개: {', '.join(apps)} · 지난 실수 조회 {len(lessons)}/{len(apps)}개 앱")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
