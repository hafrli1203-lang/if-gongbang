"""T2 end-to-end goal test (plan
(the full-orbit plan): `gb2 change <app>
"변경 요청"` on an EXISTING small app (greet() + its own pre-existing
test, already committed before this job starts) must drive the whole
orbit through two new build tasks and leave the pre-existing test
green throughout, in addition to the new scenes passing.

See tests/acceptance/test_t1_new_app.py's module docstring for the
shared rationale: deterministic ScriptedRunner workers only (no live
AI), and this test drives engine.full_orbit.run_full_job directly
rather than tools/gb2's CLI parsing, since `gb2 change`'s own flag
names for injecting non-live workers do not exist in this branch yet.
SKIPPED until worker A's engine.full_orbit and worker B's `gb2 change`
are merged (see _gb2_has_change_subcommand() below)."""
from __future__ import annotations

import json
import subprocess
import sys
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

from engine.config import EngineConfig, ProjectConfig, WorkerConfig
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.ledger import Ledger
from engine.processor import Fakes
from engine.workers.scripted import ScriptedRunner
from tests.fixtures.sample_app import make_sample

try:
    from engine.full_orbit import run_full_job
except ImportError:
    run_full_job = None  # noqa: N816 -- sentinel checked by pytestmark below

_GB2_PATH = Path(__file__).resolve().parents[2] / "tools" / "gb2"


def _gb2_has_change_subcommand() -> bool:
    import importlib.util as _ilu

    try:
        loader = SourceFileLoader("gb2_t2_probe", str(_GB2_PATH))
        spec = _ilu.spec_from_loader("gb2_t2_probe", loader)
        assert spec is not None
        module = _ilu.module_from_spec(spec)
        loader.exec_module(module)
    except Exception:
        return False
    return hasattr(module, "cmd_change")


pytestmark = pytest.mark.skipif(
    run_full_job is None or not _gb2_has_change_subcommand(),
    reason="needs worker A/B merge: engine.full_orbit (worker A) and "
          "tools/gb2's `change` subcommand (worker B)",
)


_INITIALS_PY = '''\
def initials(full_name: str) -> str:
    return "".join(word[0] for word in full_name.split()).upper()
'''

_FAREWELL_PY = '''\
def farewell(name: str) -> str:
    return f"Goodbye, {name}"
'''

_TEST_INITIALS_PY = '''\
from greeter.initials import initials


def test_initials():
    assert initials("Ada Lovelace") == "AL"
'''

_TEST_FAREWELL_PY = '''\
from greeter.farewell import farewell


def test_farewell():
    assert farewell("World") == "Goodbye, World"
'''

_SCENE_GREET_REGRESSION_PY = '''\
import sys
sys.path.insert(0, "src")
from greeter import greet
assert greet("World") == "Hello, World"
'''

_SCENE_INITIALS_PY = '''\
import sys
sys.path.insert(0, "src")
from greeter.initials import initials
assert initials("Ada Lovelace") == "AL"
'''

_FACTS_MD = '''\
# 사실 확인

| 주장 | 판정 | 출처 |
|---|---|---|
| initials()는 각 단어 첫 글자를 대문자로 이어붙인다 | 통과 | 요구사항 원문 |
| farewell()은 "Goodbye, {name}"을 돌려준다 | 통과 | 요구사항 원문 |

## 코드에 넣는 값
(없음 -- 하드코딩할 값 없음)
'''


def _plan_json(job: str) -> str:
    return json.dumps({
        "goal": "기존 인사말 앱에 initials/farewell 기능을 더한다",
        "ui": False,
        "requirements": [
            {"id": "R1", "text": "initials()가 각 단어 첫 글자를 대문자로 돌려준다", "kind": "function"},
            {"id": "R2", "text": "farewell()이 Goodbye, X 형태를 돌려준다", "kind": "function"},
        ],
        "scenes": [
            {"id": "s1", "actor_does": "기존 인사를 그대로 받는다 (회귀 확인)",
             "app_shows": "Hello, World", "where": "cli",
             "cmd": [sys.executable, "tests/scenes/s1_greet_regression.py"]},
            {"id": "s2", "actor_does": "이름의 이니셜을 구한다",
             "app_shows": "AL", "where": "cli",
             "cmd": [sys.executable, "tests/scenes/s2_initials.py"], "covers": ["R1"]},
        ],
        "tasks": [
            {"id": "t1", "title": "initials 추가",
             "detail": "initials()와 그 시험을 만든다",
             "owns": ["src/greeter/initials.py", "tests/test_initials.py"],
             "depends_on": []},
            # R2 (farewell) has no automated pass/fail scene in this plan
            # -- covered by this task instead (engine.plan.Task.covers).
            {"id": "t2", "title": "farewell 추가",
             "detail": "farewell()과 그 시험을 만든다",
             "owns": ["src/greeter/farewell.py", "tests/test_farewell.py"],
             "depends_on": ["t1"], "covers": ["R2"]},
        ],
        "acceptance": [
            "initials()가 각 단어 첫 글자를 대문자로 돌려준다",
            "farewell()이 Goodbye, X 형태를 돌려준다",
        ],
    }, ensure_ascii=False)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _install_fake_app_security(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stands in for s10-ops' own engine.app_security module (not yet
    merged into this worktree) -- see test_t1_new_app.py's own copy of
    this helper for the full rationale."""
    security_mod = types.ModuleType("engine.app_security")

    def check(config, project, attempt_id, plan):
        return types.SimpleNamespace(passed=True, findings=(), ran=())

    security_mod.check = check  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "engine.app_security", security_mod)
    import engine as _engine_pkg
    monkeypatch.setattr(_engine_pkg, "app_security", security_mod, raising=False)


def test_change_keeps_pre_existing_test_green_and_adds_two_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_app_security(monkeypatch)
    sample = make_sample(tmp_path / "repo", 1, Path(sys.executable))
    repo = sample.repo
    (repo / "tests" / "scenes").mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                   capture_output=True)
    # tests/scenes/ did not exist in variant 1's commit -- harmless if
    # empty (git ignores empty dirs; the planner script below creates
    # the actual scene files that make it non-empty).

    home = tmp_path / "home"
    secrets = tmp_path / "secrets"
    home.mkdir()
    secrets.mkdir()
    scripts = tmp_path / "scripts"

    _write(scripts / "facts.json", json.dumps([
        {"tool": "Write", "file_path": ".gongbang/greeter/facts.md", "content": _FACTS_MD},
        {"final": {"status": "done", "summary": "facts", "requests": []}},
    ]))
    _write(scripts / "planner.json", json.dumps([
        {"tool": "Write", "file_path": ".gongbang/greeter/plan.json",
         "content": _plan_json("greeter")},
        {"tool": "Write", "file_path": "tests/scenes/s1_greet_regression.py",
         "content": _SCENE_GREET_REGRESSION_PY},
        {"tool": "Write", "file_path": "tests/scenes/s2_initials.py",
         "content": _SCENE_INITIALS_PY},
        {"final": {"status": "done", "summary": "plan", "requests": []}},
    ]))
    # See test_t1_new_app.py's module docstring: one static script for
    # every build call; per-task `owns` gating (full_orbit/gate, not
    # this test) is what actually makes only the in-scope task's Write
    # land on each individual run_station(build) call.
    _write(scripts / "builder.json", json.dumps([
        {"tool": "Write", "file_path": "src/greeter/initials.py", "content": _INITIALS_PY},
        {"tool": "Write", "file_path": "tests/test_initials.py", "content": _TEST_INITIALS_PY},
        {"tool": "Write", "file_path": "src/greeter/farewell.py", "content": _FAREWELL_PY},
        {"tool": "Write", "file_path": "tests/test_farewell.py", "content": _TEST_FAREWELL_PY},
        {"final": {"status": "done", "summary": "build", "requests": []}},
    ]))
    _write(scripts / "reviewer.json", json.dumps([
        {"final": {"passed": True, "findings": [], "coverage": "full"}},
    ]))

    config = EngineConfig(
        ledger_path=tmp_path / "ledger.sqlite",
        home=home, secrets_dir=secrets,
        projects={"greeter": ProjectConfig(
            "greeter", repo, test_cmd=sample.test_cmd,
            bench_cases=sample.bench_cases,
        )},
        station_owners={
            "facts": {"facts": (".gongbang/",)},
            "plan": {"planner": (".gongbang/", "tests/scenes/")},
            "build": {"builder": ("src/", "tests/")},
        },
        workers={
            "facts": WorkerConfig(runner="scripted_facts"),
            "planner": WorkerConfig(runner="scripted_planner"),
            "builder": WorkerConfig(runner="scripted_builder"),
            "reviewer": WorkerConfig(runner="scripted_reviewer"),
        },
    )
    ledger = Ledger.create(config.ledger_path)
    ledger.register_project("greeter")
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
        config, ledger, fakes, "greeter", "greeter",
        "이름의 이니셜을 구하는 기능과 작별 인사 기능을 더해줘",
    )

    assert outcome.status == "done", outcome

    # The pre-existing greet()/test_greet from BEFORE this job (not
    # written by any task here) must still be present and still pass
    # -- the whole point of T2.
    assert (repo / "src" / "greeter" / "__init__.py").read_text(encoding="utf-8") \
        .__contains__("def greet(")
    result = subprocess.run(
        ["env", "PYTHONPATH=src:.deps", sys.executable, "-m", "pytest", "-q",
         "tests/test_greeter.py"],
        cwd=str(repo), capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    acceptance_path = repo / ".gongbang" / "greeter" / "acceptance.json"
    assert acceptance_path.exists()
    acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
    assert acceptance["passed"] is True
    assert {s["id"] for s in acceptance["scenes"]} == {"s1", "s2"}

    report_text = (repo / ".gongbang" / "greeter" / "report.md").read_text(encoding="utf-8")
    # s10 contract item 4: R1 is covered by scene s2 (initials), R2 has
    # no scene of its own and is covered by task t2 (farewell) instead.
    assert "R1" in report_text and "scene:s2" in report_text
    assert "R2" in report_text and "task:t2" in report_text
