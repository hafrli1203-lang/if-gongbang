"""A reusable scripted full-job driver for the crash matrix (tests/
acceptance/test_t6_crash_matrix.py) and the config matrix (test_t7_
config_matrix.py).

Modeled directly on tests/unit/test_full_orbit.py's own two-task-plan
test (_RunStationStub, _review_always_passes_steps,
_install_fake_acceptance_and_handoff, _full_orbit_workspace) -- this
module is that same scripted job turned into something other test files
can call repeatedly with different Faults and different config knobs,
instead of each test file re-writing its own copy.

No live AI: run_station/review are stubbed (tests/conftest.py's own
_no_live_ai_cli fixture refuses to spawn claude/codex from any test
regardless). merge/bench/commit/report/close run for REAL against a
real git sample app repo (tests/fixtures/sample_app.make_sample) via
Processor.register_defaults() -- so crashing one of their steps exercises
the real engine.steps.* code, not a stub.

Single resume entry point: `resume()`. Every crash-then-continue test in
this harness calls `driver.resume(...)`, never `full_orbit.
continue_full_job` directly, so a future change to how resume itself
works is a one-function edit for this whole harness.
"""
from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from engine import evidence, full_orbit, orbit
from engine.app_security import security_policy_hash
from engine.config import ReleaseConfig
from engine.faults import Faults
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.ledger import Event
from engine.processor import Fakes, Processor, StepContext, StepDef
from engine.steps.open_job import copy_root, dirty_paths
from tests.conftest import Workspace
from tests.fixtures.sample_app import make_sample

FULL_STATION_OWNERS = {
    "facts": {"facts": (".gongbang/",)},
    "probe": {"prober": (".gongbang/",)},
    "plan": {"planner": (".gongbang/", "tests/scenes/")},
    "design": {"designer": (".gongbang/",)},
    "build": {"builder": ("src/", "tests/")},
    "review": {"reviewer": ("review.md",)},
}

_SCENE_SCRIPT_PATH = "tests/scenes/req_check.sh"
_SCENE_SCRIPT_CONTENT = "exit 0\n"


def full_orbit_workspace(workspace: Workspace, *, sample_name: str = "sample") -> Workspace:
    """Swaps workspace.alpha for a real git sample app (variant 1: real
    pytest test_cmd + real bench cases, so bench/commit/merge/close all
    have real work to do, not a stub). `sample_name` must be distinct
    across two calls sharing the same `workspace` (e.g. a baseline run
    and a crash run in the same test) -- make_sample() re-inits/re-
    commits in place, and committing identical content twice is a no-op
    `git commit` failure, not a silent success."""
    sample = make_sample(workspace.root / sample_name, 1, Path(sys.executable))
    project_config = replace(
        workspace.config.projects["alpha"], repo=sample.repo,
        test_cmd=sample.test_cmd, install_cmd=sample.install_cmd,
        manifest=sample.manifest, bench_cases=sample.bench_cases,
    )
    new_config = replace(
        workspace.config,
        projects={**workspace.config.projects, "alpha": project_config},
        station_owners=FULL_STATION_OWNERS,
    )
    return Workspace(workspace.root, new_config, workspace.ledger, sample.repo, workspace.beta)


def fakes() -> Fakes:
    return Fakes(publisher=FakePublisher(), installer=FakeInstaller())


def _with_test_owns(tasks: list[dict[str, object]]) -> list[dict[str, object]]:
    """`tasks`, each with a synthetic tests/ owns path appended iff it
    owns something under src/ with no tests/ entry of its own already --
    validate_plan's own 결함 42 check (engine.domain.plan_model,
    plans/-gongbang-entry-unify.md §C) refuses a build task
    that owns src/ with no tests/ path to put its own test file under.
    Same helper as tests/unit/test_full_orbit.py's own, duplicated here
    since this harness module has no dependency on that test module."""
    out: list[dict[str, object]] = []
    for task in tasks:
        owns = list(task.get("owns") or [])
        has_src = any(str(p).startswith("src/") for p in owns)
        has_tests = any(str(p).startswith("tests/") for p in owns)
        if has_src and not has_tests:
            owns = [*owns, f"tests/{task['id']}_test_owns.py"]
        out.append({**task, "owns": owns})
    return out


def plan_json(*, ui: bool = False, tasks: list[dict[str, object]]) -> str:
    return json.dumps({
        "goal": "작은 기능 추가", "ui": ui,
        "scenes": [
            {"id": "s1", "actor_does": "요구사항을 확인한다", "app_shows": "통과한다",
             "where": "cli", "cmd": ["bash", _SCENE_SCRIPT_PATH], "covers": ["R1"]},
        ],
        "tasks": _with_test_owns(tasks), "acceptance": ["요구사항대로 동작한다"],
        "requirements": [{"id": "R1", "text": "요구사항대로 동작한다", "kind": "function"}],
    })


def plan_queue_entry(job_name: str, plan_json_text: str) -> dict[str, str]:
    return {
        f".gongbang/{job_name}/plan.json": plan_json_text,
        _SCENE_SCRIPT_PATH: _SCENE_SCRIPT_CONTENT,
    }


def two_task_plan_json(job_name: str) -> str:
    return plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
        {"id": "t2", "title": "둘째 파일", "detail": "", "owns": ["src/greeter/extra2.py"],
         "depends_on": ["t1"]},
    ])


class RunStationStub:
    """Pops one {relpath: content} dict off `queues[station]` per
    run_station call for that station, writes those files into the
    requesting role's own worker copy, then appends the real
    station.finished event shape.

    A crash-then-resume case that makes the engine redo an entire
    station from scratch on a BRAND NEW attempt (engine.full_orbit.
    _rerun_closed_task_from_scratch does this for a build task; the
    facts/probe/plan/design stages' own close-crash resume does the
    equivalent thing one level up) is a genuinely different request_id
    (new attempt_id in the payload), so Processor.process() calls
    run() again for real instead of reconciling a 'done' journal
    entry -- this stub must give that second call SOMETHING, never an
    IndexError on an exhausted queue. Two cases, tried in this order:
      1. (station, role, task) exactly matches an earlier call -- a
         build task's own cursor replays its ORIGINAL task text
         verbatim on redo (engine.full_orbit._rerun_closed_task_from_
         scratch reads cursor["task_text"], never rebuilds it), so
         this is the reliable signal telling two DIFFERENT tasks
         (e.g. t1 vs t2) apart on the SAME station name: replay the
         exact same files this exact key was served before.
      2. no match, but the queue for this station is empty -- facts/
         probe/plan/design rebuild their own prompt text on a redo
         (not guaranteed byte-identical to the original call), so (1)
         never matches there; replay the LAST entry served for this
         STATION (not keyed by task) instead of popping past the end
         of a queue this harness only ever stocks with exactly as
         many entries as the crash-free path needs."""

    def __init__(self, queues: dict[str, list[dict[str, str]]]) -> None:
        self.queues = queues
        self.calls: list[tuple[str, str]] = []
        self._served: dict[tuple[str, str, str], dict[str, str]] = {}
        self._last_served: dict[str, dict[str, str]] = {}

    def steps(self, ctx: StepContext) -> list[StepDef]:
        def _run(ctx: StepContext) -> dict[str, object]:
            station = str(ctx.request.payload.get("station"))
            role = str(ctx.request.payload.get("role"))
            task = str(ctx.request.payload.get("task", ""))
            self.calls.append((station, role))
            copy = copy_root(ctx.config, ctx.attempt_id, role)
            key = (station, role, task)
            files = self._served.get(key)
            if files is None:
                queue = self.queues[station]
                files = queue.pop(0) if queue else self._last_served[station]
                self._served[key] = files
                self._last_served[station] = files
            for rel, content in files.items():
                target = copy / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            changed = dirty_paths(copy, ())
            ctx.ledger.append(Event(
                project=ctx.project, kind="station.finished", actor_kind="processor",
                actor_id="processor", attempt_id=ctx.attempt_id,
                payload={
                    "request_id": ctx.request.request_id, "station": station,
                    "role": role, "ok": True, "summary": "stub", "cost_usd": 0.0,
                    "turns": 1, "changed_paths": list(changed), "requests": [],
                },
            ))
            return {}

        def _reconcile(ctx: StepContext) -> bool:
            for _seq, event in ctx.ledger.events(ctx.project, "station.finished"):
                if event.payload.get("request_id") == ctx.request.request_id:
                    return True
            return False

        return [StepDef("record_station", _run, _reconcile)]


def review_always_passes_steps(ctx: StepContext) -> list[StepDef]:
    from engine import evidence

    def _run(ctx: StepContext) -> dict[str, object]:
        project_config = ctx.config.projects[ctx.project]
        fp = evidence.fingerprint(project_config.repo, project_config.excludes)
        evidence.record(
            ctx.ledger, ctx.project, ctx.attempt_id, "review", fp,
            payload={"passed": True, "request_id": ctx.request.request_id, "findings": []},
        )
        return {}

    def _reconcile(ctx: StepContext) -> bool:
        for row in ctx.ledger.evidence(ctx.attempt_id, "review"):
            if row.payload.get("request_id") == ctx.request.request_id:
                return True
        return False

    return [StepDef("record_review", _run, _reconcile)]


def make_processor_factory(stub: RunStationStub, faults: Faults) -> orbit.ProcessorFactory:
    """The Faults instance is shared across every Processor this factory
    builds (full_orbit calls its processor_factory fresh for each
    stage/request it dispatches) -- arming one step fires exactly once,
    the first time any processor built from this factory reaches it,
    then self-clears (engine.faults.Faults.maybe_crash* discards the
    name after firing)."""

    def factory(config, ledger, fks) -> Processor:
        proc = Processor(config, ledger, fks, faults=faults)
        proc.register_defaults()
        proc.register("run_station", stub.steps)
        proc.register("review", review_always_passes_steps)
        return proc
    return factory


@dataclass(frozen=True)
class FakeSceneResult:
    id: str
    passed: bool
    returncode: int = 0
    tail: str = ""


@dataclass(frozen=True)
class FakeAcceptanceResult:
    passed: bool
    scenes: tuple[FakeSceneResult, ...] = ()
    full_suite_passed: bool = True

    def __post_init__(self) -> None:
        if not self.scenes:
            object.__setattr__(self, "scenes", (FakeSceneResult("s1", self.passed),))


@dataclass
class AcceptanceHandoffState:
    results: list[FakeAcceptanceResult]
    acceptance_calls: int = 0
    handoff_calls: list[FakeAcceptanceResult] = field(default_factory=list)


def _job_of(ledger, project: str, attempt_id: str) -> str:
    for _seq, event in ledger.events(project, "job.opened"):
        if event.payload.get("attempt_id") == attempt_id:
            return str(event.payload.get("job_name"))
    return ""


def install_fake_acceptance_and_handoff(
    monkeypatch: pytest.MonkeyPatch, results: list[FakeAcceptanceResult], *,
    security_passed: bool = True,
) -> AcceptanceHandoffState:
    """Stands in for engine.acceptance/engine.handoff/engine.app_security
    the same way tests/unit/test_full_orbit.py's own helper does --
    this harness tests the FLOW layer's crash/resume behavior, not
    acceptance/handoff/security's own internals (those have their own
    unit tests; engine.acceptance's trust-boundary behavior is
    covered by the dedicated test in test_t6_crash_matrix.py, which
    calls run_acceptance directly instead of going through this
    stub)."""
    calls: list[tuple[str, str]] = []
    security_mod = types.ModuleType("engine.app_security")
    security_mod.record = lambda ledger, project, attempt_id, result: None  # type: ignore[attr-defined]

    def check(config, project, attempt_id, plan):
        calls.append((project, attempt_id))

        @dataclass(frozen=True)
        class _SecurityResult:
            passed: bool
            findings: tuple[str, ...] = ()
            ran: tuple[str, ...] = ()

        return _SecurityResult(passed=security_passed)

    security_mod.check = check  # type: ignore[attr-defined]
    security_mod.security_policy_hash = security_policy_hash  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "engine.app_security", security_mod)
    import engine as _engine_pkg
    monkeypatch.setattr(_engine_pkg, "app_security", security_mod, raising=False)

    state = AcceptanceHandoffState(results=list(results))

    from engine import handoff as real_handoff

    acceptance_mod = types.ModuleType("engine.acceptance")

    def run_acceptance(config, ledger, project, attempt_id, plan):
        # Records acceptance.recorded the way engine.acceptance does (see
        # tests/unit/test_full_orbit.py's own _install_fake_acceptance_
        # and_handoff, the model this is copied from, updated once
        # engine.evidence_graph started reading this event's own scene
        # results + fingerprint instead of trusting the in-memory
        # FakeAcceptanceResult the processor never actually journals) --
        # the requirement-coverage gate (full_orbit's own runtime check)
        # reads THIS ledger event, not state.results, so a job that never
        # writes it stops needs_owner "requirements_uncovered:..." no
        # matter what passed=True this fake returns in memory.
        state.acceptance_calls += 1
        result = state.results.pop(0)
        project_config = config.projects[project]
        ledger.append(Event(
            project=project, kind="acceptance.recorded", actor_kind="processor",
            actor_id="processor", attempt_id=attempt_id,
            payload={"job": _job_of(ledger, project, attempt_id), "passed": result.passed,
                     "scenes": [{"id": sc.id, "passed": sc.passed} for sc in result.scenes],
                     "full_suite_passed": result.full_suite_passed, "tree_changed": False,
                     "fingerprint": evidence.fingerprint(project_config.repo,
                                                         project_config.excludes),
                     "reason": None},
        ))
        return result

    acceptance_mod.run_acceptance = run_acceptance  # type: ignore[attr-defined]

    def run_probe_scripts(config, ledger, project, attempt_id, job_name, targets):
        # Engine-shaped records (recorded on the ledger like the real
        # run), with pass/fail taken from the scripted log.
        from engine.application.acceptance_probe import ProbeEvidence, _record_probe_executed
        from engine.domain.probe_observation import target_host
        repo = config.projects[project].repo
        out = []
        from engine.gates import probe_target_id
        for i, (name, url) in enumerate(targets):
            log = repo / ".gongbang" / job_name / "probe" / f"{probe_target_id(i)}.log"
            text = log.read_text(encoding="utf-8") if log.exists() else ""
            ok = "PROBE OK" in text and "PROBE FAIL" not in text
            out.append(ProbeEvidence(
                id=probe_target_id(i), name=name, script=str(log),
                returncode=0 if ok else 1,
                stdout_tail=f"PROBE OK {url}" if ok else "exit 1", ok=ok, url=url,
                reached=ok,
                contacts=({"host": target_host(url) or "", "port": 443, "method": "CONNECT",
                           "connected": True, "bytes_up": 1, "bytes_down": 1,
                           "response_sha256": "a" * 64},) if ok else ()))
            _record_probe_executed(ledger, project, attempt_id, out[-1])
        return tuple(out)

    acceptance_mod.run_probe_scripts = run_probe_scripts  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "engine.acceptance", acceptance_mod)
    monkeypatch.setattr(_engine_pkg, "acceptance", acceptance_mod, raising=False)

    handoff_mod = types.ModuleType("engine.handoff")

    def write_handoff(config, ledger, project, attempt_id, job, plan, acc):
        state.handoff_calls.append(acc)
        report_path = config.projects[project].repo / ".gongbang" / job / "report.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("report\n", encoding="utf-8")
        return report_path

    handoff_mod.write_handoff = write_handoff  # type: ignore[attr-defined]
    # The final section is the real one (imported above, before the
    # stubs): it only reads the ledger and the plan, and appends to the
    # report this stub wrote.
    handoff_mod.write_final_section = real_handoff.write_final_section  # type: ignore[attr-defined]
    handoff_mod.report_path = real_handoff.report_path  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "engine.handoff", handoff_mod)
    monkeypatch.setattr(_engine_pkg, "handoff", handoff_mod, raising=False)
    return state


def release_project(ws: Workspace, tmp_path: Path, git_env: dict[str, str]):
    """Same technique as tests/unit/test_full_orbit.py's own
    _release_project: a `git clone --shared` master repo made BEFORE any
    of this job's commits, so release.job_certificate_status has
    somewhere to publish to."""
    import subprocess

    master_repo = tmp_path / "release-master"
    subprocess.run(["git", "clone", "--shared", "-q", str(ws.alpha), str(master_repo)],
                   check=True, capture_output=True, env=git_env)
    project_config = replace(
        ws.config.projects["alpha"],
        release=ReleaseConfig(master_repo=master_repo, target="alpha-app"),
    )
    config = replace(ws.config, projects={**ws.config.projects, "alpha": project_config})
    return config, master_repo


def apply_release_config(job: "TwoTaskJob", tmp_path: Path, git_env: dict[str, str]) -> Path:
    """Clones job.ws.alpha (the SAME repo this job's own stations/merge/
    commit steps already run against) as the release master, then swaps
    job.ws.config for one with `release` pointed at it -- in place, so
    every reference this job's own TwoTaskJob already holds (ws, stub,
    processor_factory) keeps working unchanged. Must be called before
    this job's own repo gets its first real commit (a `git clone` after
    that would just clone a repo that is already ahead of "master"."""
    config, master_repo = release_project(job.ws, tmp_path, git_env)
    job.ws = Workspace(job.ws.root, config, job.ws.ledger, job.ws.alpha, job.ws.beta)
    return master_repo


@dataclass
class TwoTaskJob:
    ws: Workspace
    job_name: str
    stub: RunStationStub
    faults: Faults
    processor_factory: orbit.ProcessorFactory
    state: AcceptanceHandoffState


def build_two_task_job(workspace: Workspace, monkeypatch: pytest.MonkeyPatch, *,
                       job_name: str = "crash-matrix-job",
                       config: object | None = None,
                       sample_name: str | None = None) -> TwoTaskJob:
    """The base scripted job every T6 crash-boundary case runs: facts ->
    plan -> two build tasks (t1 depends_on [], t2 depends_on [t1]), each
    through run_station(build) -> merge -> review -> bench -> commit ->
    report -> close, acceptance passes once, handoff runs once.
    Naturally exercises every processor step boundary for run_station,
    merge, review, bench, commit, report and close (facts/plan only
    exercise run_station+merge, per engine.full_orbit._run_facts/_run_
    plan_and_review -- see docs/flow-map.md ①)."""
    ws = full_orbit_workspace(workspace, sample_name=sample_name or f"sample-{job_name}")
    if config is not None:
        ws = Workspace(ws.root, config, ws.ledger, ws.alpha, ws.beta)
    stub = RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md":
                   "- 요청 확인: 통과 (출처: request)\n\n## 외부 대상\n(없음)\n"}],
        "plan": [plan_queue_entry(job_name, two_task_plan_json(job_name))],
        "build": [
            {"src/greeter/extra1.py": "VALUE_1 = 1\n"},
            {"src/greeter/extra2.py": "VALUE_2 = 2\n"},
        ],
    })
    state = install_fake_acceptance_and_handoff(
        monkeypatch, [FakeAcceptanceResult(passed=True)],
    )
    flt = Faults()
    factory = make_processor_factory(stub, flt)
    return TwoTaskJob(ws=ws, job_name=job_name, stub=stub, faults=flt,
                      processor_factory=factory, state=state)


def arm(job: TwoTaskJob, step: str, edge: str) -> None:
    """Arms `step` to crash once on `edge` ('crash_before'/'crash_after')
    for every Processor this job's factory builds from now on."""
    getattr(job.faults, edge).add(step)


def resume(job: TwoTaskJob) -> full_orbit.FullOutcome:
    """The one resume entry point this whole harness calls --
    engine.position.resume_job. engine.full_orbit.continue_full_job
    is kept as a thin wrapper
    around the same function (its own docstring says so), but every
    test in test_t6_crash_matrix.py/test_t7_config_matrix.py calls
    driver.resume() rather than either name directly, so a future
    change to the resume entry point is still a one-line edit here."""
    from engine import position

    return position.resume_job(
        job.ws.config, job.ws.ledger, fakes(), "alpha", job.job_name,
        processor_factory=job.processor_factory,
    )


def run_to_completion(job: TwoTaskJob, request: str = "두 파일을 만들어줘") -> full_orbit.FullOutcome:
    return full_orbit.run_full_job(
        job.ws.config, job.ws.ledger, fakes(), "alpha", job.job_name, request,
        processor_factory=job.processor_factory,
    )


def run_crash_then_resume(job: TwoTaskJob, kind: str, step: str, edge: str, *,
                          request: str = "두 파일을 만들어줘") -> full_orbit.FullOutcome:
    """Arms (step, edge), runs the job from scratch expecting exactly one
    CrashInjected, then calls driver.resume() to finish it. Returns the
    FINAL outcome (post-resume) for the caller to assert against.

    `step` is armed by NAME ONLY (engine.faults.Faults has no concept of
    "kind") -- when two DIFFERENT kinds in the same task's stage list
    share a step name (bench and commit both have "compute_fingerprint"),
    the one that runs FIRST consumes the fault, and a caller asking
    for "commit"'s own
    compute_fingerprint would silently get bench's crash instead -- the
    job still recovers fine (it IS a real crash, just not the one asked
    for), so a check that only asks "did step ever fire" passes without
    proving anything about `kind`. This verifies the crash landed on
    the INTENDED kind's OWN dispatch of `step`, not merely that `step`
    fired on SOME request: after the crash, exactly one request must be
    'running', its kind must equal `kind`, and `step`'s own journal
    entry for that exact request_id must be 'started' (not 'done' --
    confirming THIS call is what crashed, not an earlier, already-
    finished occurrence some other step's reconcile happens to read)."""
    from engine.faults import CrashInjected

    arm(job, step, edge)
    with pytest.raises(CrashInjected):
        run_to_completion(job, request)
    assert step not in getattr(job.faults, edge), (
        f"{edge}:{step} never fired -- this boundary is unreachable by "
        "the base two-task job (install/publish/abandon need a "
        "different job script; see test_t6_crash_matrix.py's own skips)"
    )
    running = [r for r in job.ws.ledger.queued_requests("alpha") if r.status == "running"]
    assert len(running) == 1, (kind, step, edge, running)
    stuck = running[0]
    assert stuck.kind == kind, (
        f"{edge}:{step} fired on kind={stuck.kind!r}, not the intended "
        f"kind={kind!r} -- a different kind's own same-named step ran "
        "first and consumed the fault; this exact (kind, step, edge) "
        "combo is not actually reachable by this job script"
    )
    step_row = job.ws.ledger.steps(stuck.request_id).get(step)
    assert step_row is not None and step_row.status == "started", (kind, step, edge, step_row)
    return resume(job)


def submit_crash_then_resume(config, ledger, project: str, attempt_id: str, kind: str,
                             payload: dict[str, object], step: str, edge: str,
                             own_fakes: Fakes) -> str:
    """The processor-level counterpart of run_crash_then_resume, for a
    request kind (install/publish) the base two-task job never submits
    on its own -- submits `kind`/`payload` directly (engine.processor.
    submit, same call a real owner-request/release flow makes), arms
    (step, edge) on a FRESH Processor built the same way
    tests/unit/test_full_orbit.py::test_install_crash_at_each_boundary_
    recovers does (register_defaults(), a dedicated holder per attempt
    so the crashed processor's lock release never races the resuming
    one), expects exactly one CrashInjected, then drains the SAME
    request to 'done' with a second, fault-free Processor. Returns the
    request_id for the caller to assert against (e.g. the installer's
    own applied_keys, or a publish receipt)."""
    from engine.faults import CrashInjected, Faults
    from engine.processor import Processor, submit

    request_id = submit(ledger, project, attempt_id, kind, payload, "owner")
    flt = Faults(**{edge: {step}})
    proc1 = Processor(config, ledger, own_fakes, faults=flt, holder=f"p1-{step}-{edge}")
    proc1.register_defaults()
    with pytest.raises(CrashInjected):
        proc1.run_once(project)
    assert step not in getattr(flt, edge), f"{edge}:{step} never fired for kind {kind!r}"
    assert ledger.request(request_id).status == "running"

    proc2 = Processor(config, ledger, own_fakes, holder=f"p2-{step}-{edge}")
    proc2.register_defaults()
    n = proc2.run_once(project)
    assert n == 1, (kind, step, edge, n)
    assert ledger.request(request_id).status == "done", (kind, step, edge,
                                                          ledger.request(request_id).result)
    return request_id
