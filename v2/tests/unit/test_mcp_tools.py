"""Unit tests for the skills/MCP tool-table contract (W2's slice):

  - engine.plan: plan.json's optional "tools" list (Tool dataclass,
    tools_for_station, validate_tools).
  - engine.gate / engine.policy: a worker calling an mcp__<server>__*
    tool is allowed only if `server` is in BOTH the request's own
    gate-facts allow list and the project's station_mcp_allow upper
    bound.
  - engine.workers.claude_cli: ClaudeRunner.argv emits --mcp-config /
    --strict-mcp-config and extends --tools with mcp__<server>__* only
    when spec.mcp_servers is non-empty.
  - engine.handoff: reconcile_tools counts actual mcp calls/skill
    injections from a synthetic ledger against plan.tools.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from engine.config import EngineConfig, McpServerConfig, WorkerConfig
from engine.handoff import reconcile_tools
from engine.ledger import Event, Ledger
from engine.plan import (
    Plan, PlanError, Tool, classify_skill_tools, load_plan, plan_path, tools_for_station,
    validate_tools,
)
from engine.policy import ProjectFacts
from engine.state import transition
from engine.workers.base import WorkerSpec
from engine.workers.claude_cli import ClaudeRunner
from tests.conftest import make_repo

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _write_plan(repo: Path, job: str, data: dict[str, object]) -> None:
    path = plan_path(repo, job)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _valid_plan_dict(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "goal": "작은 수정", "ui": False,
        "scenes": [{"id": "s1", "actor_does": "클릭", "app_shows": "결과",
                    "where": "home", "cmd": ["true"]}],
        "tasks": [{"id": "t1", "title": "고치기", "detail": "...",
                   "owns": ["src/"], "depends_on": []}],
        "acceptance": ["요구사항대로 동작한다"],
    }
    base.update(overrides)
    return base


# ---- engine.plan: tools parsing ---------------------------------------

def test_load_plan_tools_defaults_to_empty(tmp_path: Path) -> None:
    _write_plan(tmp_path, "job", _valid_plan_dict())
    plan = load_plan(tmp_path, "job")
    assert plan.tools == ()


def test_load_plan_parses_tools(tmp_path: Path) -> None:
    data = _valid_plan_dict(tools=[
        {"kind": "mcp", "name": "library", "stations": ["build"], "why": "검색"},
        {"kind": "skill", "name": "dataviz", "stations": ["design", "build"], "why": "차트"},
    ])
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    assert plan.tools == (
        Tool(kind="mcp", name="library", stations=("build",), why="검색"),
        Tool(kind="skill", name="dataviz", stations=("design", "build"), why="차트"),
    )


def test_load_plan_rejects_unknown_tool_kind(tmp_path: Path) -> None:
    data = _valid_plan_dict(tools=[
        {"kind": "browser", "name": "x", "stations": ["build"], "why": "why"},
    ])
    _write_plan(tmp_path, "job", data)
    with pytest.raises(PlanError) as exc:
        load_plan(tmp_path, "job")
    assert "kind_must_be_skill_or_mcp" in exc.value.reason


def test_load_plan_rejects_tool_with_no_stations(tmp_path: Path) -> None:
    data = _valid_plan_dict(tools=[
        {"kind": "mcp", "name": "x", "stations": [], "why": "why"},
    ])
    _write_plan(tmp_path, "job", data)
    with pytest.raises(PlanError):
        load_plan(tmp_path, "job")


def test_tools_for_station_splits_by_kind_and_station() -> None:
    plan = Plan(
        goal="g", ui=False, scenes=(), tasks=(), acceptance=("a",),
        tools=(
            Tool(kind="mcp", name="library", stations=("build",), why="w"),
            Tool(kind="mcp", name="other", stations=("design",), why="w"),
            Tool(kind="skill", name="dataviz", stations=("build", "design"), why="w"),
        ),
    )
    assert tools_for_station(plan, "build") == {"skill": ["dataviz"], "mcp": ["library"]}
    assert tools_for_station(plan, "design") == {"skill": ["dataviz"], "mcp": ["other"]}
    assert tools_for_station(plan, "review") == {"skill": [], "mcp": []}


# ---- engine.plan: validate_tools ---------------------------------------

def _config_with_mcp(tmp_path: Path, mcp_servers: dict[str, object]) -> EngineConfig:
    make_repo(tmp_path / "repo", {"a.txt": "x"})
    config_path = tmp_path / "engine.json"
    config_path.write_text(json.dumps({
        "projects": {"alpha": {"repo": "repo"}},
        "station_owners": {"build": {"builder": ["src/"]}},
        "mcp_servers": mcp_servers,
    }), encoding="utf-8")
    return EngineConfig.from_json(config_path)


def test_validate_tools_mcp_name_registered_is_fine(tmp_path: Path) -> None:
    config = _config_with_mcp(tmp_path, {"library": {"command": ["python3", "-m", "lib"]}})
    plan = Plan(goal="g", ui=False, scenes=(), tasks=(), acceptance=("a",),
               tools=(Tool(kind="mcp", name="library", stations=("build",), why="w"),))
    assert validate_tools(plan, config) == []


def test_validate_tools_mcp_name_unregistered_is_a_problem(tmp_path: Path) -> None:
    config = _config_with_mcp(tmp_path, {})
    plan = Plan(goal="g", ui=False, scenes=(), tasks=(), acceptance=("a",),
               tools=(Tool(kind="mcp", name="ghost", stations=("build",), why="w"),))
    problems = validate_tools(plan, config)
    assert len(problems) == 1
    assert "ghost" in problems[0]


def test_validate_tools_never_hard_fails_an_unlisted_skill(tmp_path: Path) -> None:
    """Per the owner's design: a skill not in the index is
    "needs_install" (see classify_skill_tools tests below), never a
    reason to fail the whole job -- validate_tools only ever reports
    problems for an mcp tool."""
    config = _config_with_mcp(tmp_path, {})
    plan = Plan(goal="g", ui=False, scenes=(), tasks=(), acceptance=("a",),
               tools=(Tool(kind="skill", name="ghost-skill", stations=("build",), why="w"),))
    assert validate_tools(plan, config) == []


# ---- engine.plan: classify_skill_tools (skill index / needs_install) --

def _config_with_skill_index(tmp_path: Path, index_text: str | None) -> EngineConfig:
    make_repo(tmp_path / "repo", {"a.txt": "x"})
    extra: dict[str, object] = {}
    if index_text is not None:
        index_path = tmp_path / "INDEX.md"
        index_path.write_text(index_text, encoding="utf-8")
        extra["skill_index_path"] = str(index_path)
    config_path = tmp_path / "engine.json"
    config_path.write_text(json.dumps({
        "projects": {"alpha": {"repo": "repo"}},
        **extra,
    }), encoding="utf-8")
    return EngineConfig.from_json(config_path)


def test_classify_skill_tools_listed_in_index_is_usable(tmp_path: Path) -> None:
    config = _config_with_skill_index(tmp_path, "- dataviz: charts and graphs\n- other-skill: x\n")
    plan = Plan(goal="g", ui=False, scenes=(), tasks=(), acceptance=("a",),
               tools=(Tool(kind="skill", name="dataviz", stations=("build",), why="w"),))
    usable, needs_install = classify_skill_tools(plan, config)
    assert usable == frozenset({"dataviz"})
    assert needs_install == frozenset()


def test_classify_skill_tools_not_in_index_is_needs_install(tmp_path: Path) -> None:
    config = _config_with_skill_index(tmp_path, "- dataviz: charts and graphs\n")
    plan = Plan(goal="g", ui=False, scenes=(), tasks=(), acceptance=("a",),
               tools=(Tool(kind="skill", name="ghost-skill", stations=("build",), why="w"),))
    usable, needs_install = classify_skill_tools(plan, config)
    assert usable == frozenset()
    assert needs_install == frozenset({"ghost-skill"})


def test_classify_skill_tools_no_index_configured_is_needs_install(tmp_path: Path) -> None:
    config = _config_with_skill_index(tmp_path, None)
    plan = Plan(goal="g", ui=False, scenes=(), tasks=(), acceptance=("a",),
               tools=(Tool(kind="skill", name="dataviz", stations=("build",), why="w"),))
    usable, needs_install = classify_skill_tools(plan, config)
    assert usable == frozenset()
    assert needs_install == frozenset({"dataviz"})


def test_tools_for_station_drops_needs_install_skills() -> None:
    plan = Plan(
        goal="g", ui=False, scenes=(), tasks=(), acceptance=("a",),
        tools=(
            Tool(kind="skill", name="dataviz", stations=("build",), why="w"),
            Tool(kind="skill", name="ghost-skill", stations=("build",), why="w"),
        ),
    )
    result = tools_for_station(plan, "build", usable_skills=frozenset({"dataviz"}))
    assert result == {"skill": ["dataviz"], "mcp": []}


# ---- engine.gate / engine.policy: mcp rule -----------------------------

def _write_config(tmp_path: Path, extra: dict[str, object]) -> Path:
    base: dict[str, object] = {
        "projects": {"alpha": {"repo": "repo"}},
        "station_owners": {"build": {"builder": ["src/"]}},
    }
    base.update(extra)
    config_path = tmp_path / "engine.json"
    config_path.write_text(json.dumps(base), encoding="utf-8")
    return config_path


def _open_attempt(config: EngineConfig, ledger: Ledger, role: str = "builder") -> tuple[str, Path]:
    attempt_id = "J-alpha-MCP#1"
    ledger.append(Event(
        project="alpha", kind="job.opened", actor_kind="owner", actor_id="owner",
        attempt_id=attempt_id,
        payload={
            "job_id": "J-alpha-MCP", "job_name": "mcpjob", "attempt_id": attempt_id,
            "n": 1, "kind": "full", "generation": 1, "dirty_paths": [],
        },
    ))
    transition(ledger, "alpha", "open_job", "owner")
    copy_root = config.home / "copies" / attempt_id / role
    shutil.copytree(config.projects["alpha"].repo, copy_root)
    return attempt_id, copy_root


def _setup(tmp_path: Path, extra: dict[str, object] | None = None) -> tuple[EngineConfig, Path, str, Path]:
    make_repo(tmp_path / "repo", {"src/app.py": "print('x')\n"})
    config_path = _write_config(tmp_path, extra or {})
    config = EngineConfig.from_json(config_path)
    ledger = Ledger.create(config.ledger_path)
    ledger.register_project("alpha")
    attempt_id, copy_root = _open_attempt(config, ledger)
    ledger.close()
    return config, config_path, attempt_id, copy_root


def _write_gate_facts_and_log(tmp_path: Path, *, mcp_allow: list[str], project: str = "alpha",
                              state: str = "OPEN", generation: int = 1,
                              attempt_id: str = "J-alpha-MCP#1") -> tuple[Path, Path]:
    facts_path = tmp_path / "gate_facts.json"
    facts_path.write_text(json.dumps({
        "project": project, "registered": True, "state": state,
        "generation": generation, "attempt_id": attempt_id, "attempt_active": True,
        "mcp_allow": mcp_allow,
    }), encoding="utf-8")
    log_path = tmp_path / "gate.log"
    log_path.write_text("", encoding="utf-8")
    return facts_path, log_path


def _base_env(config_path: Path, attempt_id: str, copy_root: Path,
              facts_path: Path, log_path: Path) -> dict[str, str]:
    return {
        "GB_CONFIG": str(config_path), "GB_PROJECT": "alpha", "GB_ATTEMPT": attempt_id,
        "GB_GENERATION": "1", "GB_STATION": "build", "GB_ROLE": "builder",
        "GB_COPY_ROOT": str(copy_root),
        "GB_GATE_FACTS": str(facts_path), "GB_GATE_LOG": str(log_path),
    }


def _run_gate(stdin: str, env: dict[str, str]) -> dict[str, object]:
    full_env = {**env, "PATH": os.environ.get("PATH", "")}
    proc = subprocess.run(
        [sys.executable, "-m", "engine.gate"],
        input=stdin, capture_output=True, text=True, cwd=str(REPO_ROOT), env=full_env,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_gate_allows_mcp_tool_in_both_request_and_station_allow(tmp_path: Path) -> None:
    config, config_path, attempt_id, copy_root = _setup(tmp_path, {
        "mcp_servers": {"library": {"command": ["python3", "-m", "lib"]}},
        "station_mcp_allow": {"build": ["library"]},
    })
    facts_path, log_path = _write_gate_facts_and_log(tmp_path, mcp_allow=["library"])
    stdin = json.dumps({"tool_name": "mcp__library__search", "cwd": str(copy_root),
                        "tool_input": {"query": "x"}})
    output = _run_gate(stdin, _base_env(config_path, attempt_id, copy_root, facts_path, log_path))
    assert output["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_gate_denies_mcp_server_not_in_station_allow(tmp_path: Path) -> None:
    config, config_path, attempt_id, copy_root = _setup(tmp_path, {
        "mcp_servers": {
            "library": {"command": ["python3", "-m", "lib"]},
            "other": {"command": ["python3", "-m", "other"]},
        },
        "station_mcp_allow": {"build": ["library"]},
    })
    # the request's own gate facts say "other" is allowed, but the
    # project's station_mcp_allow upper bound for "build" never named
    # it -- rule 5c requires both.
    facts_path, log_path = _write_gate_facts_and_log(tmp_path, mcp_allow=["other"])
    stdin = json.dumps({"tool_name": "mcp__other__search", "cwd": str(copy_root),
                        "tool_input": {"query": "x"}})
    output = _run_gate(stdin, _base_env(config_path, attempt_id, copy_root, facts_path, log_path))
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert output["hookSpecificOutput"]["permissionDecisionReason"] == "mcp_not_in_station_allow"


def test_gate_denies_mcp_server_not_in_request_allow(tmp_path: Path) -> None:
    config, config_path, attempt_id, copy_root = _setup(tmp_path, {
        "mcp_servers": {
            "library": {"command": ["python3", "-m", "lib"]},
            "other": {"command": ["python3", "-m", "other"]},
        },
        "station_mcp_allow": {"build": ["library", "other"]},
    })
    # this request's own plan.tools only asked for "library" -- "other"
    # is a config-level upper bound entry, not something this job planned.
    facts_path, log_path = _write_gate_facts_and_log(tmp_path, mcp_allow=["library"])
    stdin = json.dumps({"tool_name": "mcp__other__search", "cwd": str(copy_root),
                        "tool_input": {"query": "x"}})
    output = _run_gate(stdin, _base_env(config_path, attempt_id, copy_root, facts_path, log_path))
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert output["hookSpecificOutput"]["permissionDecisionReason"] == "mcp_not_in_request_allow"


def test_gate_denies_malformed_mcp_tool_name_without_calling_policy(tmp_path: Path) -> None:
    config, config_path, attempt_id, copy_root = _setup(tmp_path)
    facts_path, log_path = _write_gate_facts_and_log(tmp_path, mcp_allow=["library"])
    stdin = json.dumps({"tool_name": "mcp__noservertool", "cwd": str(copy_root), "tool_input": {}})
    output = _run_gate(stdin, _base_env(config_path, attempt_id, copy_root, facts_path, log_path))
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert output["hookSpecificOutput"]["permissionDecisionReason"] == "tool_not_allowed"


def test_policy_mcp_denied_without_facts_snapshot(tmp_path: Path) -> None:
    """Policy built straight from a ledger (no ProjectFacts) must fail
    closed for mcp -- there is no "unnarrowed" allowance, only an
    explicit per-request list."""
    config, config_path, attempt_id, copy_root = _setup(tmp_path, {
        "mcp_servers": {"library": {"command": ["python3"]}},
        "station_mcp_allow": {"build": ["library"]},
    })
    from engine.policy import Action, Actor, Policy

    ledger = Ledger.open(config.ledger_path)
    policy = Policy(config, ledger=ledger)
    actor = Actor(kind="worker", role="builder", attempt_id=attempt_id, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(tool="mcp", target="library"))
    ledger.close()
    assert decision.allowed is False
    assert decision.reason == "mcp_not_in_request_allow"


# ---- engine.workers.claude_cli: --mcp-config / --strict-mcp-config -----

def _spec(tmp_path: Path, mcp_servers: dict[str, McpServerConfig] | None = None) -> WorkerSpec:
    copy_root = tmp_path / "copy"
    copy_root.mkdir()
    return WorkerSpec(
        project="alpha", attempt_id="J-alpha-TEST#1", generation=1,
        station="build", role="builder", copy_root=copy_root, workdir=copy_root,
        task="do the thing", result_path=tmp_path / "result.json",
        config_path=tmp_path / "engine.json",
        mcp_servers=(mcp_servers or {}),
    )


def test_argv_without_mcp_servers_is_unchanged(tmp_path: Path) -> None:
    argv = ClaudeRunner().argv(_spec(tmp_path), WorkerConfig(runner="claude"))
    assert "--mcp-config" not in argv
    assert "--strict-mcp-config" not in argv
    assert argv[argv.index("--tools") + 1] == "Read,Edit,Write,Glob,Grep,Bash"


def test_argv_with_mcp_servers_adds_strict_mcp_config_and_tool_names(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LIBRARY_TOKEN", "should-not-leak-if-not-passthrough")
    spec = _spec(tmp_path, {
        "library": McpServerConfig(command=("python3", "-m", "lib"), args=("--quiet",),
                                   env_passthrough=("LIBRARY_TOKEN",)),
    })
    argv = ClaudeRunner().argv(spec, WorkerConfig(runner="claude"))
    assert "--strict-mcp-config" in argv
    assert "--mcp-config" in argv
    tools = argv[argv.index("--tools") + 1]
    assert "mcp__library__*" in tools.split(",")

    mcp_json = json.loads(argv[argv.index("--mcp-config") + 1])
    entry = mcp_json["mcpServers"]["library"]
    assert entry["command"] == "python3"
    assert entry["args"] == ["-m", "lib", "--quiet"]
    assert entry["env"] == {"LIBRARY_TOKEN": "should-not-leak-if-not-passthrough"}


def test_argv_mcp_env_only_carries_configured_names(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("UNRELATED_SECRET", raising=False)
    monkeypatch.setenv("UNRELATED_SECRET", "leak")
    spec = _spec(tmp_path, {
        "library": McpServerConfig(command=("python3",), env_passthrough=()),
    })
    argv = ClaudeRunner().argv(spec, WorkerConfig(runner="claude"))
    mcp_json = json.loads(argv[argv.index("--mcp-config") + 1])
    assert "env" not in mcp_json["mcpServers"]["library"]


# ---- engine.handoff: reconcile_tools ------------------------------------

def _mcp_decided_event(attempt_id: str, server: str) -> Event:
    return Event(
        project="alpha", kind="policy.decided", actor_kind="worker", actor_id="builder",
        attempt_id=attempt_id,
        payload={"tool": "mcp", "path": None, "target": server, "command": None,
                 "allowed": True, "reason": "mcp_allowed"},
    )


def _context_injected_event(attempt_id: str, station: str, skills: list[str]) -> Event:
    return Event(
        project="alpha", kind="context.injected", actor_kind="processor", actor_id="processor",
        attempt_id=attempt_id,
        payload={"station": station, "items": [], "skills": skills, "missing": []},
    )


def test_reconcile_tools_counts_calls_and_injections(tmp_path: Path) -> None:
    make_repo(tmp_path / "repo", {"a.txt": "x"})
    config_path = tmp_path / "engine.json"
    config_path.write_text(json.dumps({"projects": {"alpha": {"repo": "repo"}}}), encoding="utf-8")
    config = EngineConfig.from_json(config_path)
    ledger = Ledger.create(config.ledger_path)
    ledger.register_project("alpha")
    attempt_id, _copy = _open_attempt(config, ledger)

    ledger.append(_mcp_decided_event(attempt_id, "library"))
    ledger.append(_mcp_decided_event(attempt_id, "library"))
    ledger.append(_mcp_decided_event(attempt_id, "unplanned-server"))
    ledger.append(_context_injected_event(attempt_id, "build", ["dataviz"]))

    plan = Plan(
        goal="g", ui=False, scenes=(), tasks=(), acceptance=("a",),
        tools=(
            Tool(kind="mcp", name="library", stations=("build",), why="검색"),
            Tool(kind="mcp", name="never-called", stations=("build",), why="안 쓴 도구"),
            Tool(kind="skill", name="dataviz", stations=("build",), why="차트"),
        ),
    )
    reconciliation = reconcile_tools(ledger, "alpha", "mcpjob", plan)
    ledger.close()

    by_name = {row.name: row for row in reconciliation.rows}
    assert by_name["library"].call_count == 2
    assert by_name["never-called"].call_count == 0
    assert by_name["dataviz"].used_in_stations == ("build",)
    assert reconciliation.unplanned == ("mcp:unplanned-server",)


def test_reconcile_tools_ignores_events_from_other_attempts(tmp_path: Path) -> None:
    make_repo(tmp_path / "repo", {"a.txt": "x"})
    config_path = tmp_path / "engine.json"
    config_path.write_text(json.dumps({"projects": {"alpha": {"repo": "repo"}}}), encoding="utf-8")
    config = EngineConfig.from_json(config_path)
    ledger = Ledger.create(config.ledger_path)
    ledger.register_project("alpha")
    attempt_id, _copy = _open_attempt(config, ledger)

    # An event tagged with an attempt_id that is NOT part of this job's
    # own attempts (attempts_for_job_name) must never be counted.
    ledger.append(_mcp_decided_event("some-other-attempt#1", "library"))

    plan = Plan(goal="g", ui=False, scenes=(), tasks=(), acceptance=("a",),
               tools=(Tool(kind="mcp", name="library", stations=("build",), why="w"),))
    reconciliation = reconcile_tools(ledger, "alpha", "mcpjob", plan)
    ledger.close()

    assert reconciliation.rows[0].call_count == 0


def test_reconcile_tools_marks_needs_install_skill_and_write_handoff_shows_it(tmp_path: Path) -> None:
    from engine.handoff import write_handoff
    from engine.acceptance import AcceptanceResult, SceneResult

    config = _config_with_skill_index(tmp_path, "- other-skill: x\n")
    ledger = Ledger.create(config.ledger_path)
    ledger.register_project("alpha")
    attempt_id, _copy = _open_attempt(config, ledger)

    plan = Plan(
        goal="g", ui=False, scenes=(), tasks=(), acceptance=("요구사항",),
        tools=(Tool(kind="skill", name="ghost-skill", stations=("build",), why="차트"),),
    )
    _usable, needs_install = classify_skill_tools(plan, config)
    reconciliation = reconcile_tools(ledger, "alpha", "mcpjob", plan, needs_install=needs_install)
    assert reconciliation.rows[0].needs_install is True

    acc = AcceptanceResult(passed=True, scenes=(), full_suite_passed=True)
    report = write_handoff(config, ledger, "alpha", attempt_id, "mcpjob", plan, acc)
    ledger.close()

    text = report.read_text(encoding="utf-8")
    assert "설치 필요(사람 승인)" in text
    assert "ghost-skill" in text
