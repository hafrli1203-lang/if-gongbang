#!/usr/bin/env python3
# Generated from the private ops workspace at 24c1bb68758c8a552514d06d8280463876278f76; do not edit by hand (see harness/export_ops_public.py).
"""IF 공방 작업 사본(wt) 환경 사전 점검 -- 묶음 8 #29.

Checks every external dependency the wt-*.sh tool chain (plans/2026-11-01-wt-interface.md)
and pstack/PSTACK_KO.md path need, and prints OK/FAIL per item. Read-only: never installs,
writes, or prints a secret value -- the postgres check runs through wt-env.py exactly the
way wt-lib.sh's own wt_pg_admin does, so a DB URL or password never reaches this script's
own code, only the child psql process's environment.

Run: python3 tools/wt_doctor.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
SHARED = Path("/apps/_shared")
WT_ENVPY = SHARED / "wt-env.py"
WT_ADMIN_ENV = Path("/apps/search/.env")  # same admin role wt-lib.sh's wt_pg_admin uses
APPS_ROOT = Path("/apps")
FACTS_DIR = APPS_ROOT / "_ops" / "app-facts"
SKILLS_LIBRARY = HOME / ".claude" / "skills-library"
PSTACK_DIR = SKILLS_LIBRARY / "03-에이전트·하네스·오케스트레이션" / "pstack" / "skills"
PSTACK_KO = Path("/apps/_ops/PSTACK_KO.md")
HEAVY_SH = Path("/apps/exampleapp/scripts/heavy.sh")
SETTINGS_JSON = HOME / ".claude" / "settings.json"
RAIL_AGENTS = ("사실확정자", "실측자", "계획자", "시안자", "구현자", "검토자")
PSTACK_LINKED_SKILLS = ("poteto-mode", "create-verification-skill", "ketchup", "correct")
SLOT_DB_RE = re.compile(r"^\s*([A-Za-z0-9._-]+)_wt([1-3])_test\s*\|")
# postgresql://user:password@host or user:password@host -- Codex 검토(2026-11-02)로 발견: psql/wt-env.py
# 자식 프로세스의 stderr를 그대로 보여주면 접속 URL의 비밀번호가 새어나갈 수 있다. 보고 전에 가린다.
_CREDENTIAL_RE = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)?[^\s:@/]+:[^\s:@/]+@")

Check = tuple[str, bool, str]


def _redact(text: str) -> str:
    """Masks any `user:password@`-shaped substring (with or without a scheme) so a
    postgres/host error message never carries a credential into a check's detail."""
    return _CREDENTIAL_RE.sub(lambda m: f"{m.group('scheme') or ''}***:***@", text)


def _cmd_present(name: str) -> Check:
    found = shutil.which(name)
    return (name, found is not None, found or "PATH에 없음")


def _bwrap_runs() -> Check:
    """shutil.which만으로는 부족하다 -- 커널 지원이 없거나 setuid가 빠진 bwrap은
    PATH엔 있어도 실제 작업 격리 때 실패하므로 같은 자기-마운트로 실측한다."""
    if shutil.which("bwrap") is None:
        return ("bwrap", False, "PATH에 없음")
    try:
        result = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "true"],
            capture_output=True, timeout=10,
        )
    except OSError as exc:
        return ("bwrap", False, f"실행 실패: {exc}")
    except subprocess.TimeoutExpired:
        return ("bwrap", False, "시간초과")
    if result.returncode != 0:
        tail = (result.stderr or b"").decode("utf-8", "replace").strip()[-300:]
        return ("bwrap", False, f"exit {result.returncode}: {tail}" if tail else f"exit {result.returncode}")
    return ("bwrap", True, "ok")


def _systemd_user_runs() -> Check:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-system-running"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ("systemd --user", False, f"실행 실패: {exc}")
    state = (result.stdout or "").strip() or (result.stderr or "").strip()
    ok = state in ("running", "degraded")  # degraded = 일부 유닛 실패, systemd 자체는 동작
    return ("systemd --user", ok, state or "응답 없음")


def _postgres_reachable() -> Check:
    """wt-lib.sh의 wt_pg_admin과 같은 경로로 `select 1`을 실제로 실행한다 --
    URL·비밀값은 이 스크립트에 들어오지 않고 psql 자식 프로세스 환경에만 있다."""
    if not WT_ENVPY.exists():
        return ("postgres 접속", False, f"{WT_ENVPY} 없음")
    if not WT_ADMIN_ENV.exists():
        return ("postgres 접속", False, f"{WT_ADMIN_ENV} 없음")
    if shutil.which("psql") is None:
        return ("postgres 접속", False, "psql PATH에 없음")
    try:
        result = subprocess.run(
            ["python3", str(WT_ENVPY), "pg", "--env-file", str(WT_ADMIN_ENV), "--",
             # -X: .psqlrc를 읽지 않는다 -- 관리자 접속으로 시작 설정의 임의 SQL/셸이
             # 실행되는 것을 막는다(Codex 검토 2026-11-02 P1).
             "psql", "-X", "-tAc", "select 1"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ("postgres 접속", False, f"실행 실패: {type(exc).__name__}")
    if result.returncode != 0 or result.stdout.strip() != "1":
        # 원문 stderr는 보여주지 않는다 -- wt-env.py 예외 메시지에 접속 URL의
        # 비밀번호가 실려 나올 수 있다(Codex 검토 2026-11-02 P1). exit 코드만.
        return ("postgres 접속", False, f"접속 실패 (exit {result.returncode}) -- 직접 `wt-env.py pg`로 확인")
    return ("postgres 접속", True, "ok")


def _slot_dbs() -> Check:
    """`psql -lqt`로 받은 DB 목록에서 `<앱>_wt<1|2|3>_test` 패턴을 모아, 패턴이
    하나라도 보인 앱마다 1/2/3이 전부 있는지 센다 -- 기대 숫자(30)를 지어내지
    않고 지금 서버에 실제로 있는 걸 그대로 센다."""
    if not WT_ENVPY.exists() or not WT_ADMIN_ENV.exists() or shutil.which("psql") is None:
        return ("슬롯 DB", False, "psql 또는 wt-env.py 또는 admin env 없음 -- postgres 접속 확인 먼저")
    try:
        result = subprocess.run(
            ["python3", str(WT_ENVPY), "pg", "--env-file", str(WT_ADMIN_ENV), "--", "psql", "-X", "-lqt"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ("슬롯 DB", False, f"실행 실패: {type(exc).__name__}")
    if result.returncode != 0:
        # 원문 stderr 대신 고정 메시지(비밀값 노출 방지, Codex 검토 2026-11-02 P1).
        return ("슬롯 DB", False, f"DB 목록 조회 실패 (exit {result.returncode})")
    found: dict[str, set[str]] = {}
    for line in result.stdout.splitlines():
        m = SLOT_DB_RE.match(line)
        if m:
            found.setdefault(m.group(1), set()).add(m.group(2))
    total = sum(len(v) for v in found.values())
    incomplete = sorted(app for app, slots in found.items() if slots != {"1", "2", "3"})
    if incomplete:
        return ("슬롯 DB", False, f"{total}개 있음, 1/2/3 중 빠진 앱: {', '.join(incomplete)}")
    return ("슬롯 DB", True, f"{total}개 ({len(found)}개 앱 x 3)")


def _file_exists(label: str, path: Path, detail_ok: str = "ok") -> Check:
    return (label, path.exists(), detail_ok if path.exists() else f"{path} 없음")


def _pstack_links() -> Check:
    missing = [s for s in PSTACK_LINKED_SKILLS if not (HOME / ".claude" / "skills" / s).exists()]
    if missing:
        return ("pstack 링크 4개", False, f"없음: {', '.join(missing)}")
    return ("pstack 링크 4개", True, ", ".join(PSTACK_LINKED_SKILLS))


def _rail_agents() -> Check:
    missing = [a for a in RAIL_AGENTS if not (HOME / ".claude" / "agents" / f"{a}.md").exists()]
    if missing:
        return ("작업조 정의(레일 6)", False, f"없음: {', '.join(missing)}")
    return ("작업조 정의(레일 6)", True, ", ".join(RAIL_AGENTS))


def _hooks_wired() -> Check:
    """settings.json의 hooks 절을 읽기만 한다 -- 훅 내용을 바꾸지 않는다."""
    if not SETTINGS_JSON.exists():
        return ("훅 연결", False, f"{SETTINGS_JSON} 없음")
    try:
        data = json.loads(SETTINGS_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return ("훅 연결", False, f"읽기 실패: {exc}")
    hooks = data.get("hooks")
    if not isinstance(hooks, dict) or not hooks:
        return ("훅 연결", False, "hooks 절 없음/비어 있음")
    events = sorted(hooks.keys())
    return ("훅 연결", True, f"{len(events)}개 이벤트: {', '.join(events)}")


def _apps_with_facts() -> Check:
    if not FACTS_DIR.exists():
        return ("앱 사실 카드", False, f"{FACTS_DIR} 없음")
    apps = sorted(p.stem for p in FACTS_DIR.glob("*.md"))
    return ("앱 사실 카드", len(apps) > 0, f"{len(apps)}개: {', '.join(apps)}")


def checks() -> list[Check]:
    out: list[Check] = []
    for name in ("git", "node", "npm", "python3.12", "claude", "codex", "flock", "psql"):
        out.append(_cmd_present(name))
    out.append(_bwrap_runs())
    out.append(_systemd_user_runs())
    out.append(_postgres_reachable())
    out.append(_slot_dbs())
    out.append(_file_exists("heavy.sh", HEAVY_SH))
    out.append(_file_exists("pstack(poteto-mode SKILL.md)", PSTACK_DIR / "poteto-mode" / "SKILL.md"))
    out.append(_file_exists("PSTACK_KO.md", PSTACK_KO))
    out.append(_file_exists("skill-library INDEX.md", SKILLS_LIBRARY / "INDEX.md"))
    out.append(_pstack_links())
    out.append(_rail_agents())
    out.append(_hooks_wired())
    out.append(_apps_with_facts())
    return out


def main() -> int:
    results = checks()
    width = max(len(name) for name, _ok, _detail in results)
    fail_count = 0
    for name, ok, detail in results:
        mark = "OK" if ok else "FAIL"
        if not ok:
            fail_count += 1
        print(f"{mark:5} {name.ljust(width)}  {detail}")
    print(f"\n{len(results)}개 점검, FAIL {fail_count}개")
    # doctor는 보고만 한다 -- FAIL이 있어도 여기서 고치지 않는다(team-lead 지시).
    # exit code만 FAIL 유무를 알린다(ops.py cmd_doctor와 같은 관례), 호출한 쪽이 판단.
    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
