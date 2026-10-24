"""Per-task write narrowing (engine.full_orbit's build loop): a
ProjectFacts carrying `owns` narrows a worker's station/role prefixes
down to exactly that task's own paths -- a builder with the full
"src/, lib/, docs/, package.json" build prefixes must still be denied
writing under "lib/" if ITS task's owns is only ("src/",). Covers
engine.policy (the actual enforcement), engine.gate (loading `owns`
from the gate facts file) and engine.steps.station (fail-fast
validation before a worker is ever spawned, and writing `owns` into
gate_facts.json)."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from engine.config import WorkerConfig
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.gate import decide
from engine.ledger import Event, Ledger
from engine.policy import Action, Actor, Decision, Policy, ProjectFacts
from engine.processor import Fakes, Processor, StepFailed
from engine.processor import submit as submit_request
from engine.state import transition
from engine.steps import station
from engine.steps.open_job import copy_root as production_copy_root
from engine.steps.open_job import open_job
from engine.workers.scripted import ScriptedRunner
from tests.conftest import Workspace


def _fakes(**runners: object) -> Fakes:
    return Fakes(publisher=FakePublisher(), installer=FakeInstaller(), runners=runners)


def _open_project(ledger: Ledger, project: str, generation: int = 1) -> str:
    attempt_id = f"J-{project}-OWNS#1"
    ledger.append(Event(
        project=project, kind="job.opened", actor_kind="owner",
        actor_id="owner", attempt_id=attempt_id,
        payload={
            "job_id": f"J-{project}-OWNS", "job_name": "job",
            "attempt_id": attempt_id, "n": 1, "kind": "full",
            "generation": generation, "dirty_paths": [],
        },
    ))
    transition(ledger, project, "open_job", "owner")
    return attempt_id


# ---- engine.policy: Policy.check() with a narrowed ProjectFacts.owns ----

def _make_copy(workspace: Workspace, name: str) -> Path:
    import shutil
    dest = production_copy_root(workspace.config, f"J-alpha-OWNS#1-{name}", "builder")
    shutil.copytree(workspace.alpha, dest)
    return dest


class _NullSink:
    def record(self, event: Event) -> None:
        pass


def test_owns_narrows_allowed_write_to_task_prefix(workspace: Workspace) -> None:
    _open_project(workspace.ledger, "alpha")
    copy_root = _make_copy(workspace, "allow")
    facts = ProjectFacts(project="alpha", registered=True, state="OPEN", generation=1,
                         attempt_id=None, attempt_active=False, owns=("src/",))
    policy = Policy(workspace.config, facts=facts, sink=_NullSink())
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(tool="write", path=copy_root / "src/new.py"))
    assert decision == Decision(True, "within_allowed_path")


def test_owns_denies_write_outside_task_prefix_but_inside_build_prefixes(
    workspace: Workspace,
) -> None:
    """"lib/" IS one of the project's configured build prefixes -- a
    builder with no owns narrowing could write there. A task whose own
    owns is only ("src/",) must still be denied."""
    _open_project(workspace.ledger, "alpha")
    copy_root = _make_copy(workspace, "deny")
    facts = ProjectFacts(project="alpha", registered=True, state="OPEN", generation=1,
                         attempt_id=None, attempt_active=False, owns=("src/",))
    policy = Policy(workspace.config, facts=facts, sink=_NullSink())
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(tool="write", path=copy_root / "lib/new.py"))
    assert decision == Decision(False, "outside_allowed_path")

    # Sanity: the SAME write, with no owns narrowing at all, is allowed
    # (proves the denial above comes from owns, not some other rule).
    unnarrowed_facts = ProjectFacts(project="alpha", registered=True, state="OPEN",
                                    generation=1, attempt_id=None, attempt_active=False)
    unnarrowed = Policy(workspace.config, facts=unnarrowed_facts, sink=_NullSink())
    assert unnarrowed.check("alpha", actor, Action(
        tool="write", path=copy_root / "lib/new.py")).allowed is True


def test_owns_escaping_configured_prefixes_denied_outright(workspace: Workspace) -> None:
    """A gate facts file claiming an owns entry OUTSIDE the project's
    configured build prefixes (should never happen by construction --
    engine.plan.validate_plan and engine.steps.station._full_spec both
    refuse such a plan/task before a worker is ever spawned) must still
    be denied here, not silently trusted as a wider grant."""
    _open_project(workspace.ledger, "alpha")
    copy_root = _make_copy(workspace, "escape")
    facts = ProjectFacts(project="alpha", registered=True, state="OPEN", generation=1,
                         attempt_id=None, attempt_active=False, owns=("not-a-build-prefix/",))
    policy = Policy(workspace.config, facts=facts, sink=_NullSink())
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(tool="write", path=copy_root / "src/new.py"))
    assert decision == Decision(False, "owns_escapes_station_prefixes")


def test_owns_empty_tuple_denies_every_write(workspace: Workspace) -> None:
    _open_project(workspace.ledger, "alpha")
    copy_root = _make_copy(workspace, "empty")
    facts = ProjectFacts(project="alpha", registered=True, state="OPEN", generation=1,
                         attempt_id=None, attempt_active=False, owns=())
    policy = Policy(workspace.config, facts=facts, sink=_NullSink())
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(tool="write", path=copy_root / "src/new.py"))
    assert decision == Decision(False, "owns_escapes_station_prefixes")


# ---- engine.gate: decide() loads `owns` from the gate facts file -------

def test_gate_decide_loads_owns_and_narrows(workspace: Workspace, tmp_path: Path) -> None:
    config_path = tmp_path / "engine.json"
    config_path.write_text("{}", encoding="utf-8")  # decide() re-loads from GB_CONFIG
    import engine.config as config_mod
    monkey_config = replace(workspace.config, config_path=config_path)
    # engine.gate.decide() calls EngineConfig.from_json(env["GB_CONFIG"]) itself,
    # so the file at config_path must be the real, loadable config, not the
    # in-memory one -- write it out via the same shape engine.config writes.
    config_path.write_text(json.dumps(config_mod.to_json_dict(monkey_config)),
                           encoding="utf-8")

    copy_root = _make_copy(workspace, "gate")
    facts_path = tmp_path / "gate_facts.json"
    facts_path.write_text(json.dumps({
        "project": "alpha", "registered": True, "state": "OPEN", "generation": 1,
        "attempt_id": None, "attempt_active": False, "owns": ["src/"],
    }), encoding="utf-8")
    gate_log = tmp_path / "gate.log"
    gate_log.write_text("", encoding="utf-8")

    env = {
        "GB_CONFIG": str(config_path), "GB_PROJECT": "alpha", "GB_ATTEMPT": "",
        "GB_GENERATION": "1", "GB_STATION": "build", "GB_ROLE": "builder",
        "GB_COPY_ROOT": str(copy_root), "GB_GATE_FACTS": str(facts_path),
        "GB_GATE_LOG": str(gate_log),
    }
    _open_project(workspace.ledger, "alpha")

    allowed_hook = {
        "tool_name": "Write", "tool_input": {"file_path": str(copy_root / "src/new.py")},
        "cwd": str(copy_root),
    }
    out = decide(allowed_hook, env)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

    denied_hook = {
        "tool_name": "Write", "tool_input": {"file_path": str(copy_root / "lib/new.py")},
        "cwd": str(copy_root),
    }
    out = decide(denied_hook, env)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == "outside_allowed_path"


def test_gate_decide_malformed_owns_fails_closed(workspace: Workspace, tmp_path: Path) -> None:
    import engine.config as config_mod
    config_path = tmp_path / "engine.json"
    monkey_config = replace(workspace.config, config_path=config_path)
    config_path.write_text(json.dumps(config_mod.to_json_dict(monkey_config)),
                           encoding="utf-8")
    copy_root = _make_copy(workspace, "malformed")
    facts_path = tmp_path / "gate_facts.json"
    facts_path.write_text(json.dumps({
        "project": "alpha", "registered": True, "state": "OPEN", "generation": 1,
        "attempt_id": None, "attempt_active": False, "owns": "src/",  # not a list
    }), encoding="utf-8")
    gate_log = tmp_path / "gate.log"
    gate_log.write_text("", encoding="utf-8")
    env = {
        "GB_CONFIG": str(config_path), "GB_PROJECT": "alpha", "GB_ATTEMPT": "",
        "GB_GENERATION": "1", "GB_STATION": "build", "GB_ROLE": "builder",
        "GB_COPY_ROOT": str(copy_root), "GB_GATE_FACTS": str(facts_path),
        "GB_GATE_LOG": str(gate_log),
    }
    hook = {
        "tool_name": "Write", "tool_input": {"file_path": str(copy_root / "src/new.py")},
        "cwd": str(copy_root),
    }
    out = decide(hook, env)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == "gate_facts_missing"


# ---- engine.steps.station: fail-fast + gate_facts.json wiring ----------

def _station_workspace(workspace: Workspace) -> Workspace:
    config = replace(
        workspace.config,
        workers={"builder": WorkerConfig(runner="scripted", timeout_sec=5.0)},
        config_path=workspace.root / "engine.json",
    )
    return Workspace(workspace.root, config, workspace.ledger, workspace.alpha, workspace.beta)


def test_full_spec_refuses_owns_escaping_build_prefixes(workspace: Workspace) -> None:
    ws = _station_workspace(workspace)
    attempt_id = open_job(ws.config, ws.ledger, "alpha", "owns-job", "full", ("builder",))
    rid = submit_request(
        ws.ledger, "alpha", attempt_id, "run_station",
        {"station": "build", "role": "builder", "task": "do it", "owns": ["not-owned/"]},
        "owner",
    )
    req = ws.ledger.request(rid)
    dummy_script = ws.root / "dummy.json"
    dummy_script.write_text("[]", encoding="utf-8")
    fakes = _fakes(scripted=ScriptedRunner(dummy_script))
    ctx = station.StepContext(config=ws.config, ledger=ws.ledger, fakes=fakes,
                              request=req, project="alpha", attempt_id=attempt_id)
    with pytest.raises(StepFailed) as exc_info:
        station._full_spec(ctx)
    assert exc_info.value.args[0] == "owns_escapes_station_prefixes"


def test_write_gate_facts_includes_task_owns(workspace: Workspace) -> None:
    ws = _station_workspace(workspace)
    attempt_id = open_job(ws.config, ws.ledger, "alpha", "owns-job2", "full", ("builder",))
    rid = submit_request(
        ws.ledger, "alpha", attempt_id, "run_station",
        {"station": "build", "role": "builder", "task": "do it", "owns": ["src/"]},
        "owner",
    )
    req = ws.ledger.request(rid)
    ctx = station.StepContext(config=ws.config, ledger=ws.ledger, fakes=None,
                              request=req, project="alpha", attempt_id=attempt_id)
    station._write_gate_facts(ctx)
    data = json.loads(station._gate_facts_path(ctx).read_text(encoding="utf-8"))
    assert data["owns"] == ["src/"]


def test_owns_blocks_builder_write_outside_task_end_to_end(workspace: Workspace) -> None:
    """A real ScriptedRunner, spawned with --gate (engine.gate as a real
    subprocess), given owns=("src/",): a Write under "lib/" (inside the
    project's configured build prefixes) is refused by the gate, so it
    never lands in the copy -- while a Write under "src/" succeeds."""
    ws = _station_workspace(workspace)
    script = ws.root / "owns_script.json"
    script.write_text(json.dumps([
        {"tool": "Write", "file_path": "src/allowed.py", "content": "x = 1\n"},
        {"tool": "Write", "file_path": "lib/blocked.py", "content": "y = 2\n"},
        {"final": {"status": "done", "summary": "", "changed_paths": [], "requests": []}},
    ]), encoding="utf-8")
    import sys
    gate_argv = (sys.executable, "-P", "-m", "engine.gate")
    fakes = _fakes(scripted=ScriptedRunner(script, gate_argv=gate_argv))
    proc = Processor(config=ws.config, ledger=ws.ledger, fakes=fakes, holder="p1")
    proc.register("run_station", station.steps)

    attempt_id = open_job(ws.config, ws.ledger, "alpha", "owns-e2e", "full", ("builder",))
    rid = submit_request(
        ws.ledger, "alpha", attempt_id, "run_station",
        {"station": "build", "role": "builder", "task": "do it", "owns": ["src/"]},
        "owner",
    )
    n = proc.run_once("alpha")
    assert n == 1

    copy = production_copy_root(ws.config, attempt_id, "builder")
    assert (copy / "src/allowed.py").exists()
    assert not (copy / "lib/blocked.py").exists()
