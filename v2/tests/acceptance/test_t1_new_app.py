"""T1 end-to-end goal test (plan
(the full-orbit contract): starting from NO
app folder, `gb2 new <app> "요구사항"` must drive the whole orbit --
facts -> plan -> (no design, plan.ui=False) -> two build tasks (each
leaving the full suite green) -> acceptance (engine.acceptance,
Worker C's own module) -> handoff report (engine.handoff) -> done.

Everything AI-shaped is a deterministic ScriptedRunner (engine.fakes.
scripted_worker) -- no live Claude/Codex call anywhere in this file.

SKIPPED until worker A's engine.full_orbit/engine.plan/engine.gates and
worker B's `gb2 new` are merged into this branch (see the lead's
handoff message) -- see _full_orbit_ready()/_gb2_has() below. Until
then this module intentionally does not hard-import engine.full_orbit
at module scope (that would turn the skip into a collection ERROR).

Scope note (documented, not a guess the lead needs to double-check):
this test exercises the CONTRACTED engine.full_orbit.run_full_job
entry point directly, rather than going through tools/gb2's CLI
argument parsing -- `gb2 new`'s own exact flag names for injecting
scripted (non-live) workers do not exist in this branch yet to write
against. If the lead wants this test to instead drive
`gb2.cmd_new`/`gb2.main(["new", ...])` once that CLI is final, the
fixture setup below (config/fakes/scripted scripts) is the part to
keep -- only the three lines that build the "new app" skeleton and
call run_full_job need to change to go through gb2 instead."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from engine.config import EngineConfig, ProjectConfig, WorkerConfig
from engine.context import ContextConfig
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.ledger import Ledger
from engine.processor import Fakes
from engine.workers.scripted import ScriptedRunner

try:
    from engine.full_orbit import run_full_job
except ImportError:
    run_full_job = None  # noqa: N816 -- sentinel checked by pytestmark below

_GB2_PATH = Path(__file__).resolve().parents[2] / "tools" / "gb2"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
    "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": "/dev/null",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True, env=_GIT_ENV)


def _gb2_has_new_subcommand() -> bool:
    """True iff tools/gb2 defines a "new" handler (worker B's
    cmd_new) -- loaded the same way tests/unit/test_gb2.py loads the
    extensionless script, inside a try/except since this file must
    stay importable even before that script exists at all changes
    worker B might still be making."""
    import importlib.util as _ilu
    from importlib.machinery import SourceFileLoader

    try:
        loader = SourceFileLoader("gb2_t1_probe", str(_GB2_PATH))
        spec = _ilu.spec_from_loader("gb2_t1_probe", loader)
        assert spec is not None
        module = _ilu.module_from_spec(spec)
        loader.exec_module(module)
    except Exception:
        return False
    return hasattr(module, "cmd_new")


pytestmark = pytest.mark.skipif(
    run_full_job is None or not _gb2_has_new_subcommand(),
    reason="needs worker A/B merge: engine.full_orbit/engine.plan/engine.gates "
          "(worker A) and tools/gb2's `new` subcommand (worker B)",
)


_GREET_PY = '''\
def greet(name: str) -> str:
    return f"Hello, {name}"
'''

_FAREWELL_PY = '''\
def farewell(name: str) -> str:
    return f"Goodbye, {name}"
'''

_TEST_GREET_PY = '''\
from greeter import greet


def test_greet():
    assert greet("World") == "Hello, World"
'''

_TEST_FAREWELL_PY = '''\
from greeter.farewell import farewell


def test_farewell():
    assert farewell("World") == "Goodbye, World"
'''

_SCENE_GREET_PY = '''\
import sys
sys.path.insert(0, "src")
from greeter import greet
assert greet("World") == "Hello, World"
'''

_SCENE_FAREWELL_PY = '''\
import sys
sys.path.insert(0, "src")
from greeter.farewell import farewell
assert farewell("World") == "Goodbye, World"
'''

_FACTS_MD = '''\
# 사실 확인

| 주장 | 판정 | 출처 |
|---|---|---|
| greet()은 "Hello, {name}"을 돌려준다 | 통과 | 요구사항 원문 |
| farewell()은 "Goodbye, {name}"을 돌려준다 | 통과 | 요구사항 원문 |

## 코드에 넣는 값
(없음 -- 하드코딩할 값 없음)

## 외부 대상
(없음)
'''


def _plan_json(job: str) -> str:
    return json.dumps({
        "goal": "인사말 앱: 만나면 Hello, 헤어지면 Goodbye를 보여준다",
        "ui": False,
        "requirements": [
            {"id": "R1", "text": "greet()이 Hello, X 형태를 돌려준다", "kind": "function"},
            {"id": "R2", "text": "farewell()이 Goodbye, X 형태를 돌려준다", "kind": "function"},
        ],
        "scenes": [
            {"id": "s1", "actor_does": "이름을 주고 인사를 받는다",
             "app_shows": "Hello, World", "where": "cli",
             "cmd": [sys.executable, "tests/scenes/s1_greet.py"], "covers": ["R1"]},
            {"id": "s2", "actor_does": "이름을 주고 작별 인사를 받는다",
             "app_shows": "Goodbye, World", "where": "cli",
             "cmd": [sys.executable, "tests/scenes/s2_farewell.py"], "covers": ["R2"]},
        ],
        "tasks": [
            {"id": "t1", "title": "greet 구현", "detail": "greet()과 그 시험을 만든다",
             "owns": ["src/greeter/__init__.py", "tests/test_greeter.py"],
             "depends_on": []},
            {"id": "t2", "title": "farewell 구현",
             "detail": "farewell()과 그 시험을 만든다",
             "owns": ["src/greeter/farewell.py", "tests/test_farewell.py"],
             "depends_on": ["t1"]},
        ],
        "tools": [
            {"kind": "skill", "name": "greet-helper", "stations": ["build"],
             "why": "인사말 형식 규칙을 지킨다"},
            {"kind": "skill", "name": "review-helper", "stations": ["review"],
             "why": "검토 기준"},
            {"kind": "skill", "name": "없는-스킬", "stations": ["build"],
             "why": "창고에 없는 것은 설치 필요로 보고된다"},
        ],
        "acceptance": [
            "greet()이 Hello, X 형태를 돌려준다",
            "farewell()이 Goodbye, X 형태를 돌려준다",
        ],
    }, ensure_ascii=False)


def _install_fake_app_security(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stands in for another change' own engine.app_security module (not yet
    merged into this worktree -- engine.full_orbit imports it lazily by
    name, right after handoff, same as engine.acceptance/engine.handoff
    above). Always reports passed=True here: T1/T2 are about the
    facts->plan->build->accept->handoff flow, not security findings."""
    security_mod = types.ModuleType("engine.app_security")
    security_mod.record = lambda ledger, project, attempt_id, result: None  # type: ignore[attr-defined]

    def check(config, project, attempt_id, plan):
        return types.SimpleNamespace(passed=True, findings=(), ran=())

    security_mod.check = check  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "engine.app_security", security_mod)
    import engine as _engine_pkg
    monkeypatch.setattr(_engine_pkg, "app_security", security_mod, raising=False)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_script(path: Path, actions: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(actions), encoding="utf-8")


def _make_new_app_skeleton(repo: Path) -> None:
    """Stands in for `gb2 new`'s own tools/templates/new-app/ skeleton
    (README, .gitignore, empty tests/scenes/) -- deliberately NO src/
    and NO tests/test_*.py: the whole point of T1 is that the app's
    actual code does not exist until the build tasks create it."""
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _write(repo / "README.md", "# new app\n")
    _write(repo / ".gitignore", "__pycache__/\n.deps/\n")
    (repo / "tests" / "scenes").mkdir(parents=True)
    (repo / "tests" / "scenes" / ".gitkeep").write_text("", encoding="utf-8")
    # The negative control exists from the start (gb2 adds one to every app
    # workspace too), so the bench can prove its grader on the first task.
    _write(repo / "tests" / "test_control.py",
           "import os\n\n\ndef test_control():\n"
           "    if os.environ.get('T1_CONTROL'):\n"
           "        import module_that_does_not_exist  # noqa: F401\n")
    _write(repo / "bench/cases.json", json.dumps([
        {"name": "greet", "selector": "tests/test_greeter.py::test_greet",
         "expect_fail": False, "expected_signature": "assertion"},
        {"name": "negative_control",
         "selector": "tests/test_control.py",
         "expect_fail": True, "expected_signature": "ModuleNotFoundError",
         "env": {"T1_CONTROL": "1"}},
    ]))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init: empty skeleton")


def test_new_app_facts_plan_two_tasks_and_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_app_security(monkeypatch)
    repo = tmp_path / "repo"
    _make_new_app_skeleton(repo)

    home = tmp_path / "home"
    secrets = tmp_path / "secrets"
    home.mkdir()
    secrets.mkdir()
    scripts = tmp_path / "scripts"

    _write(scripts / "facts.json", json.dumps([
        {"tool": "Write", "file_path": ".gongbang/newapp/facts.md", "content": _FACTS_MD},
        {"final": {"status": "done", "summary": "facts", "requests": []}},
    ]))
    _write(scripts / "planner.json", json.dumps([
        {"tool": "Write", "file_path": ".gongbang/newapp/plan.json",
         "content": _plan_json("newapp")},
        {"tool": "Write", "file_path": "tests/scenes/s1_greet.py",
         "content": _SCENE_GREET_PY},
        {"tool": "Write", "file_path": "tests/scenes/s2_farewell.py",
         "content": _SCENE_FAREWELL_PY},
        {"final": {"status": "done", "summary": "plan", "requests": []}},
    ]))
    # One static script for every build-station call, whatever task is
    # "in scope" -- see module docstring: engine.full_orbit/engine.gate
    # narrows the worker's actual write permission to that task's own
    # `owns` prefixes, so the "other" task's Write below is expected to
    # be silently denied (not applied) on each call, and only the
    # in-scope one actually lands -- exactly the per-task isolation the
    # contract describes.
    _write(scripts / "builder.json", json.dumps([
        {"tool": "Write", "file_path": "src/greeter/__init__.py", "content": _GREET_PY},
        {"tool": "Write", "file_path": "tests/test_greeter.py", "content": _TEST_GREET_PY},
        {"tool": "Write", "file_path": "src/greeter/farewell.py", "content": _FAREWELL_PY},
        {"tool": "Write", "file_path": "tests/test_farewell.py", "content": _TEST_FAREWELL_PY},
        {"final": {"status": "done", "summary": "build", "requests": []}},
    ]))
    _write(scripts / "reviewer.json", json.dumps([
        {"final": {"passed": True, "findings": [], "coverage": "full"}},
    ]))

    skills_dir = tmp_path / "skills"
    _write(skills_dir / "greet-helper" / "SKILL.md",
           "---\nname: greet-helper\ndescription: greeting format rules\n---\nGREET-HELPER-BODY\n")
    _write(skills_dir / "review-helper" / "SKILL.md",
           "---\nname: review-helper\ndescription: review rules\n---\nREVIEW-HELPER-BODY\n")
    _write(skills_dir / "facts-base" / "SKILL.md",
           "---\nname: facts-base\ndescription: base facts skill\n---\nFACTS-BASE-BODY\n")

    config = EngineConfig(
        ledger_path=tmp_path / "ledger.sqlite",
        home=home, secrets_dir=secrets,
        projects={"newapp": ProjectConfig(
            "newapp", repo,
            test_cmd=("env", f"PYTHONPATH=src", sys.executable, "-m", "pytest", "-q"),
            bench_cases="bench/cases.json",
        )},
        station_owners={
            "facts": {"facts": (".gongbang/",)},
            "plan": {"planner": (".gongbang/", "tests/scenes/")},
            "build": {"builder": ("src/", "tests/")},
        },
        context=ContextConfig(skill_roots=(str(skills_dir),),
                              station_skills={"facts": ("facts-base",)}),
        workers={
            "facts": WorkerConfig(runner="scripted_facts"),
            "planner": WorkerConfig(runner="scripted_planner"),
            "builder": WorkerConfig(runner="scripted_builder"),
            "reviewer": WorkerConfig(runner="scripted_reviewer"),
        },
    )
    ledger = Ledger.create(config.ledger_path)
    ledger.register_project("newapp")
    fakes = Fakes(
        publisher=FakePublisher(), installer=FakeInstaller(),
        runners={
            "scripted_facts": ScriptedRunner(scripts / "facts.json"),
            "scripted_planner": ScriptedRunner(scripts / "planner.json"),
            "scripted_builder": ScriptedRunner(scripts / "builder.json"),
            "scripted_reviewer": ScriptedRunner(scripts / "reviewer.json", read_only=True),
        },
    )

    outcome = run_full_job(
        config, ledger, fakes, "newapp", "newapp",
        "인사말 앱을 처음부터 만들어줘: 만나면 Hello, 헤어지면 Goodbye",
    )

    assert outcome.status == "done", outcome

    assert (repo / "src" / "greeter" / "__init__.py").exists()
    assert (repo / "src" / "greeter" / "farewell.py").exists()
    acceptance_path = repo / ".gongbang" / "newapp" / "acceptance.json"
    assert acceptance_path.exists()
    acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
    assert acceptance["passed"] is True
    assert {s["id"] for s in acceptance["scenes"]} == {"s1", "s2"}
    assert all(s["passed"] for s in acceptance["scenes"])

    report_path = repo / ".gongbang" / "newapp" / "report.md"
    assert report_path.exists()

    # The planner was shown the tool catalog; the skill it picked for the
    # build station reached every build worker's instructions, the base
    # facts skill reached only the facts worker, and the skill missing
    # from every root is reported as needing installation.
    tasks_by_station: dict[str, list[str]] = {}
    for _seq, event in ledger.events("newapp", "request.submitted"):
        payload = event.payload.get("payload") or {}
        if event.payload.get("kind") == "run_station":
            tasks_by_station.setdefault(str(payload["station"]), []).append(str(payload["task"]))
    assert all("## 고를 수 있는 도구" in t and "greet-helper" in t
               for t in tasks_by_station["plan"])
    assert tasks_by_station["build"]
    assert all("GREET-HELPER-BODY" in t for t in tasks_by_station["build"])
    assert all("FACTS-BASE-BODY" in t for t in tasks_by_station["facts"])
    assert not any("GREET-HELPER-BODY" in t for t in tasks_by_station["facts"])
    injected = [e.payload for _s, e in ledger.events("newapp", "context.injected")
                if e.payload.get("station") == "build"]
    assert injected and all(p["planned"] == ["greet-helper"] for p in injected)
    assert "설치 필요" in report_path.read_text(encoding="utf-8")
    reviews = [e.payload.get("payload") or {} for _s, e in ledger.events("newapp", "request.submitted")
               if e.payload.get("kind") == "review"]
    assert reviews
    assert all("REVIEW-HELPER-BODY" in str(r.get("context", "")) for r in reviews)
    assert not any("GREET-HELPER-BODY" in str(r.get("task", "")) for r in reviews)
    assert not any("REVIEW-HELPER-BODY" in t for t in tasks_by_station["build"])
    report_text = report_path.read_text(encoding="utf-8")
    assert "s1" in report_text and "s2" in report_text
    assert "통과" in report_text
    # the 대조표 pairs requirement id <-> scene id,
    # not list position -- both requirements' own rows carry the scene
    # that actually covers them.
    assert "R1" in report_text and "scene:s1" in report_text
    assert "R2" in report_text and "scene:s2" in report_text
