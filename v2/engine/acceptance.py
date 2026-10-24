"""acceptance -- the (scripted, non-step) check that a job's own pass
scenes and the project's real test suite both come out green, run after
the last build task's commit (ENGINE.md full-orbit flow: build loop ->
acceptance -> handoff). Not an engine.processor request kind: called
directly by engine.full_orbit as a plain function (the full-orbit loop
owns retry/fix-round bookkeeping, not this module), so there is no
StepContext/reconcile here -- re-running it just re-executes every
scene and the full suite again, which is always safe (both are read-only
checks of the repo's current tree).

Reuses engine.steps.bench_run's sandbox-argv builder and env rules
exactly, the same way tests/unit/test_sandbox.py already does -- a
SimpleNamespace standing in for StepContext, since
_bench_sandbox_argv/_bench_env only ever read ctx.config/ctx.project/
ctx.attempt_id, never ctx.request or ctx.fakes. This is the one bench
sandbox policy this codebase has (writable=repo, readonly=repo/.git,
hidden=engine home/secrets/other projects/real HOME, net=False) --
acceptance scenes and the full suite get exactly the same isolation a
bench request gets, not a second, divergent one.

Acceptance integrity: the repo's own fingerprint (engine.evidence.
fingerprint) is taken before any scene runs and again after the full
suite finishes; any difference means something inside a scene or the
suite itself altered the tree being accepted -- AcceptanceResult.
tree_changed is then True and passed is forced False, regardless of
what the scenes/suite themselves reported, and the written JSON/
ledger event carry the same reason ("tree_changed") so this is never
silently indistinguishable from an ordinary scene/suite failure.

run_probe_scripts runs each probe target's own script (facts.md's
"## 외부 대상" names, normalized to a safe `[a-z0-9-]{1,40}` file id)
from .gongbang/<job>/probe/<safe-id>.(py|sh|js) inside the SAME bench
sandbox scenes/the full suite run in -- the engine executes the
script itself; a worker's "PROBE OK" text in some log is never trusted
as the evidence (see engine.gates' check_probe docstring for the
compatibility path this replaces). Each run appends one
probe.executed ledger event ({name, script, returncode, stdout_tail})
regardless of outcome.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

from engine import evidence, sandbox
from engine.config import EngineConfig
from engine.ledger import Event, Ledger
from engine.steps.bench_run import _TAIL_CHARS, _bench_env, _bench_sandbox_argv
from engine.steps.open_job import _job_name_for

if TYPE_CHECKING:
    from engine.plan import Plan, Scene


@dataclass(frozen=True)
class SceneResult:
    id: str
    passed: bool
    returncode: int
    tail: str


@dataclass(frozen=True)
class AcceptanceResult:
    passed: bool
    scenes: tuple[SceneResult, ...]
    full_suite_passed: bool
    full_suite_returncode: int = 0
    full_suite_tail: str = ""
    # True iff the repo's own fingerprint differed between the start and
    # the end of this run_acceptance call -- something inside a scene or
    # the full suite altered the tree being accepted. passed is always
    # False when this is True, regardless of scenes/full_suite_passed.
    tree_changed: bool = False


@dataclass(frozen=True)
class ProbeEvidence:
    name: str
    script: str
    returncode: int
    stdout_tail: str
    ok: bool


def acceptance_path(config: EngineConfig, project: str, job_name: str) -> Path:
    return config.projects[project].repo / ".gongbang" / job_name / "acceptance.json"


def _bench_ctx(config: EngineConfig, project: str, attempt_id: str) -> SimpleNamespace:
    """Stand-in for StepContext (see module docstring) -- the only
    attributes _bench_sandbox_argv/_bench_env ever read."""
    return SimpleNamespace(config=config, project=project, attempt_id=attempt_id)


def _run_in_bench_sandbox(
    config: EngineConfig, project: str, attempt_id: str, repo: Path,
    argv: list[str], env: dict[str, str], timeout_sec: float, *, net: bool = False,
) -> tuple[int, str]:
    """(returncode, tail) for one command run exactly the way
    engine.steps.bench_run.run_tests runs the real suite. A command
    that fails to even start (bad argv[0], sandbox unavailable) or
    times out both count as failed here -- returncode -1, the
    exception text as the tail -- rather than raising: a single scene
    or the full suite failing to start must not abort every other
    scene still to be checked."""
    try:
        wrapped_argv, child_env = _bench_sandbox_argv(
            _bench_ctx(config, project, attempt_id), repo, argv, env, net=net)
    except sandbox.SandboxUnavailable as exc:
        return -1, f"sandbox_unavailable: {exc}"
    popen_env = sandbox.exec_env(config.sandbox, child_env)
    try:
        result = subprocess.run(
            wrapped_argv, cwd=str(repo), capture_output=True, text=True,
            timeout=timeout_sec, env=popen_env,
        )
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except OSError as exc:
        return -1, f"failed to start: {exc}"
    combined = (result.stdout or "") + (result.stderr or "")
    return result.returncode, combined[-_TAIL_CHARS:]


def _run_scene(config: EngineConfig, project: str, attempt_id: str, repo: Path,
              env: dict[str, str], timeout_sec: float, scene: "Scene") -> SceneResult:
    returncode, tail = _run_in_bench_sandbox(
        config, project, attempt_id, repo, list(scene.cmd), env, timeout_sec)
    return SceneResult(id=scene.id, passed=returncode == 0,
                       returncode=returncode, tail=tail)


def _run_full_suite(config: EngineConfig, project: str, attempt_id: str,
                    repo: Path, env: dict[str, str],
                    timeout_sec: float) -> tuple[bool, int, str]:
    """(passed, returncode, tail) for the project's own test_cmd. A
    project with no test_cmd configured can never be accepted -- there
    is nothing to run as "the project's full test_cmd" -- fails closed
    (returncode -1, "no_test_cmd") rather than silently passing."""
    test_cmd = config.projects[project].test_cmd
    if not test_cmd:
        return False, -1, "no_test_cmd"
    returncode, tail = _run_in_bench_sandbox(
        config, project, attempt_id, repo, list(test_cmd), env, timeout_sec)
    return returncode == 0, returncode, tail


def _job_name(ledger: Ledger, project: str, attempt_id: str) -> str:
    job_id = ledger.attempt(attempt_id).job_id
    return _job_name_for(ledger, project, job_id)


def _write_acceptance_json(
    config: EngineConfig, project: str, job_name: str, passed: bool,
    full_suite_passed: bool, full_suite_returncode: int, full_suite_tail: str,
    scenes: tuple[SceneResult, ...], tree_changed: bool = False,
) -> Path:
    path = acceptance_path(config, project, job_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "job": job_name,
        "passed": passed,
        "tree_changed": tree_changed,
        "full_suite": {
            "passed": full_suite_passed,
            "returncode": full_suite_returncode,
            "tail": full_suite_tail,
        },
        "scenes": [
            {"id": s.id, "passed": s.passed, "returncode": s.returncode, "tail": s.tail}
            for s in scenes
        ],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _fingerprint_or_none(repo: Path, excludes: tuple[str, ...]) -> str | None:
    """None on a FingerprintError -- treated the same as "the tree
    changed" by the caller (see run_acceptance): a fingerprint that
    cannot be computed is never trusted as proof the tree stayed the
    same."""
    try:
        return evidence.fingerprint(repo, excludes)
    except evidence.FingerprintError:
        return None


def run_acceptance(config: EngineConfig, ledger: Ledger, project: str,
                   attempt_id: str, plan: "Plan") -> AcceptanceResult:
    """Run every plan.scenes[*].cmd, then the project's full test_cmd,
    all inside the bench sandbox (see module docstring); write
    acceptance.json into the job's .gongbang/<job>/ directory inside the
    repo, and append one acceptance.recorded ledger event. A plan with
    zero scenes is never accepted (there is nothing demonstrating the
    job actually does what was asked) -- scenes=() and passed=False,
    without running the full suite at all (reason "no_scenes" in the
    written file/ledger event only -- AcceptanceResult itself carries no
    "reason" field, per the fixed interface contract)."""
    project_cfg = config.projects[project]
    repo = project_cfg.repo
    env = _bench_env(_bench_ctx(config, project, attempt_id))
    timeout_sec = project_cfg.test_timeout_sec
    job_name = _job_name(ledger, project, attempt_id)

    if not plan.scenes:
        acc = AcceptanceResult(passed=False, scenes=(), full_suite_passed=False)
        _write_acceptance_json(config, project, job_name, False, False, -1,
                               "no_scenes", ())
        _record(ledger, project, attempt_id, job_name, acc, reason="no_scenes")
        return acc

    before_fp = _fingerprint_or_none(repo, project_cfg.excludes)

    scenes = tuple(
        _run_scene(config, project, attempt_id, repo, env, timeout_sec, scene)
        for scene in plan.scenes
    )
    full_suite_passed, full_suite_returncode, full_suite_tail = _run_full_suite(
        config, project, attempt_id, repo, env, timeout_sec)

    after_fp = _fingerprint_or_none(repo, project_cfg.excludes)
    tree_changed = before_fp is None or after_fp is None or before_fp != after_fp

    passed = full_suite_passed and all(s.passed for s in scenes) and not tree_changed
    acc = AcceptanceResult(
        passed=passed, scenes=scenes, full_suite_passed=full_suite_passed,
        full_suite_returncode=full_suite_returncode, full_suite_tail=full_suite_tail,
        tree_changed=tree_changed,
    )
    _write_acceptance_json(config, project, job_name, passed, full_suite_passed,
                           full_suite_returncode, full_suite_tail, scenes, tree_changed)
    if passed:
        reason = None
    elif tree_changed:
        reason = "tree_changed"
    else:
        reason = "scenes_or_suite_failed"
    _record(ledger, project, attempt_id, job_name, acc, reason=reason)
    return acc


def _record(ledger: Ledger, project: str, attempt_id: str, job_name: str,
           acc: AcceptanceResult, reason: str | None) -> None:
    ledger.append(Event(
        project=project, kind="acceptance.recorded", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={
            "job": job_name,
            "passed": acc.passed,
            "scenes": [{"id": s.id, "passed": s.passed} for s in acc.scenes],
            "full_suite_passed": acc.full_suite_passed,
            "tree_changed": acc.tree_changed,
            "reason": reason,
        },
    ))


_PROBE_SAFE_ID_RE = re.compile(r"[^a-z0-9-]+")
_PROBE_EXTENSIONS = (".py", ".sh", ".js")
_PROBE_INTERPRETERS: dict[str, tuple[str, ...]] = {
    ".py": (sys.executable,),
    ".sh": ("sh",),
    ".js": ("node",),
}


def _safe_probe_id(name: str) -> str:
    """Normalizes `name` to the `[a-z0-9-]{1,40}` file id probe scripts
    are named with -- never the raw target name (which can carry
    spaces, Korean text, or any other character a filename should not
    carry)."""
    lowered = _PROBE_SAFE_ID_RE.sub("-", name.strip().lower()).strip("-")
    return (lowered or "target")[:40]


def _probe_script_path(repo: Path, job: str, safe_id: str) -> Path | None:
    probe_dir = repo / ".gongbang" / job / "probe"
    for ext in _PROBE_EXTENSIONS:
        candidate = probe_dir / f"{safe_id}{ext}"
        if candidate.exists():
            return candidate
    return None


def _probe_argv(script: Path) -> list[str]:
    return [*_PROBE_INTERPRETERS[script.suffix], str(script)]


def _record_probe_executed(ledger: Ledger, project: str, attempt_id: str,
                           name: str, script: str, returncode: int,
                           stdout_tail: str) -> None:
    ledger.append(Event(
        project=project, kind="probe.executed", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"name": name, "script": script, "returncode": returncode,
                 "stdout_tail": stdout_tail},
    ))


def run_probe_scripts(
    config: EngineConfig, ledger: Ledger, project: str, attempt_id: str,
    job_name: str, targets: list[tuple[str, str]],
) -> tuple["ProbeEvidence", ...]:
    """Runs each target's own probe script (see module docstring) inside
    the bench sandbox and appends one probe.executed ledger event per
    target, in order. A target with no script at any of the known
    extensions gets returncode=-1, stdout_tail="script_missing",
    ok=False -- never raises, so one missing script does not stop the
    rest of the targets from being probed and recorded."""
    project_cfg = config.projects[project]
    repo = project_cfg.repo
    env = _bench_env(_bench_ctx(config, project, attempt_id))
    timeout_sec = project_cfg.test_timeout_sec
    results: list[ProbeEvidence] = []
    for name, _url in targets:
        safe_id = _safe_probe_id(name)
        script = _probe_script_path(repo, job_name, safe_id)
        if script is None:
            _record_probe_executed(ledger, project, attempt_id, name, "", -1, "script_missing")
            results.append(ProbeEvidence(
                name=name, script="", returncode=-1, stdout_tail="script_missing", ok=False))
            continue
        returncode, tail = _run_in_bench_sandbox(
            config, project, attempt_id, repo, _probe_argv(script), env, timeout_sec, net=True)
        ok = returncode == 0 and script.exists()
        _record_probe_executed(ledger, project, attempt_id, name, str(script), returncode, tail)
        results.append(ProbeEvidence(
            name=name, script=str(script), returncode=returncode, stdout_tail=tail, ok=ok))
    return tuple(results)
