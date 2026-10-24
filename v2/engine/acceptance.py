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

Acceptance integrity: scenes and the full suite never run against the
real repo at all. The repo's own fingerprint (engine.evidence.
fingerprint) is taken ONCE, before anything runs, and a disposable
snapshot holding exactly that content is built (git archive <that
tree hash> -> fresh git init/commit, same pattern
engine.steps.open_job._make_copy uses for a worker copy) under
config.home/"acceptance"/<attempt>; every scene and the full suite run
against THIS snapshot, never `repo`. This is deliberate: a before/after
fingerprint comparison taken around a scene that runs IN the real repo
cannot catch a scene that mutates a file, runs the test, then restores
it before the comparison's "after" read -- the restore makes the
diff look clean. Running against a snapshot whose fingerprint was
fixed BEFORE any scene started closes that: there is nothing for a
scene to usefully "restore" to fool, since the thing being certified
(AcceptanceResult.fingerprint) was never re-derived from anything a
scene could have touched. The real repo's OWN fingerprint is still
re-checked after the full suite finishes, as a second, independent
guard -- it should never differ (nothing ran against `repo` itself),
and a difference (tree_changed=True, passed forced False, same as
before) means something unexpected happened to touch the real repo
during this call at all.

The snapshot itself still stays writable to every scene (a scene
creating a new, untracked scratch/output file is legitimate -- see
test_scene_mutation_runs_against_a_snapshot_not_the_real_repo), so
fixing the snapshot's fingerprint once up front is not by itself
enough: nothing stopped a scene from PATCHING a tracked implementation
file inside the snapshot and having that edit stick for every later
scene and the full suite, which would then certify code the fixed
fingerprint never covered. run_acceptance checks the snapshot's own
`git status` for any HEAD-tracked (not bare-untracked) change after
EVERY scene and after the full suite (_tracked_mutated_paths); the
first stage that trips it stops the run right there (snapshot_mutated=
True, passed=False, reason "snapshot_mutated") -- no scene or the full
suite ever runs again against a snapshot a prior stage is known to
have altered.

run_probe_scripts runs each probe target's own script from
.gongbang/<job>/probe/<id>.(py|sh|js) -- the engine executes the
script itself; a worker's "PROBE OK" text in some log is never
trusted as the evidence (see engine.gates' check_probe docstring for
the compatibility path this replaces). `<id>` is engine.gates.
probe_target_id(i), the target's 1-based POSITION in the targets list
(see that function's own docstring) -- never derived from the
target's own name: a name-derived id (the old scheme here, since
superseded) collapses every Korean-only name to the same fallback id,
and "Naver API"/"Naver_API" to the same id too, letting one passing
script silently stand in for several distinct targets. Each run
appends one probe.executed ledger event ({id, name, script,
returncode, stdout_tail}) regardless of outcome.

Like run_acceptance's own scenes/full suite, every probe script runs
against a disposable snapshot of the repo's fingerprinted tree (see
_make_snapshot/_link_snapshot_deps), never the real project repo --
the bench sandbox this reuses always mounts its `repo` argument
writable (test runners write __pycache__/coverage there), so running
a probe script directly against the real repo (the previous shape of
this function) gave an external-API probe script -- not reviewed the
way a build worker's own commit is -- direct write access to the
project's real working tree. The real repo is still mounted read-only
alongside the snapshot (extra_readonly), so a copy_links symlink the
snapshot only points back into it (e.g. node_modules) still resolves.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

from engine import evidence, gates, ids, sandbox
from engine.config import EngineConfig
from engine.gitutil import git_argv
from engine.ledger import Event, Ledger
from engine.steps.bench_run import _TAIL_CHARS, _bench_env, _bench_sandbox_argv
from engine.steps.open_job import _is_within, _job_name_for

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
    # True iff the real repo's own fingerprint differed between the start
    # and the end of this run_acceptance call -- scenes/the full suite
    # run against a disposable snapshot (see module docstring), never
    # `repo` itself, so this should never be True; a True value means
    # something unexpected touched the real repo during this call. passed
    # is always False when this is True, regardless of scenes/
    # full_suite_passed.
    tree_changed: bool = False
    # The tree hash (engine.evidence.fingerprint) certified by this run --
    # taken once, before the snapshot was built, from the real repo.
    # "" iff nothing was ever accepted (no_scenes, or the fingerprint
    # itself could not be computed) -- never trust an empty string as
    # "the empty tree"; callers (release/handoff) must treat it the same
    # as passed=False.
    fingerprint: str = ""
    # True iff a scene (or the full suite) left the snapshot's own
    # HEAD-tracked content different from what _make_snapshot committed
    # -- a scene is still free to write a new, untracked scratch/output
    # file (test_scene_mutation_runs_against_a_snapshot_not_the_real_repo),
    # but patching an existing tracked file inside the snapshot must not
    # let a LATER scene or the full suite run against that silently
    # altered tree and still certify it. Always False when passed is
    # True. run_acceptance stops at the first scene/full-suite stage
    # that trips this -- every scenes entry after it never ran.
    snapshot_mutated: bool = False


@dataclass(frozen=True)
class ProbeEvidence:
    id: str
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
    extra_readonly: tuple[Path, ...] = (),
) -> tuple[int, str]:
    """(returncode, tail) for one command run exactly the way
    engine.steps.bench_run.run_tests runs the real suite. A command
    that fails to even start (bad argv[0], sandbox unavailable) or
    times out both count as failed here -- returncode -1, the
    exception text as the tail -- rather than raising: a single scene
    or the full suite failing to start must not abort every other
    scene still to be checked.

    extra_readonly is forwarded to _bench_sandbox_argv as-is -- here,
    always the real project repo (see run_acceptance), since `repo`
    in THIS function is the disposable snapshot (see module
    docstring), not the real repo any copy_links symlink inside it
    (_link_snapshot_deps) actually points to."""
    try:
        wrapped_argv, child_env = _bench_sandbox_argv(
            _bench_ctx(config, project, attempt_id), repo, argv, env, net=net,
            extra_readonly=extra_readonly)
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
              env: dict[str, str], timeout_sec: float, scene: "Scene",
              extra_readonly: tuple[Path, ...] = ()) -> SceneResult:
    returncode, tail = _run_in_bench_sandbox(
        config, project, attempt_id, repo, list(scene.cmd), env, timeout_sec,
        extra_readonly=extra_readonly)
    return SceneResult(id=scene.id, passed=returncode == 0,
                       returncode=returncode, tail=tail)


def _run_full_suite(config: EngineConfig, project: str, attempt_id: str,
                    repo: Path, env: dict[str, str],
                    timeout_sec: float,
                    extra_readonly: tuple[Path, ...] = ()) -> tuple[bool, int, str]:
    """(passed, returncode, tail) for the project's own test_cmd. A
    project with no test_cmd configured can never be accepted -- there
    is nothing to run as "the project's full test_cmd" -- fails closed
    (returncode -1, "no_test_cmd") rather than silently passing."""
    test_cmd = config.projects[project].test_cmd
    if not test_cmd:
        return False, -1, "no_test_cmd"
    returncode, tail = _run_in_bench_sandbox(
        config, project, attempt_id, repo, list(test_cmd), env, timeout_sec,
        extra_readonly=extra_readonly)
    return returncode == 0, returncode, tail


def _job_name(ledger: Ledger, project: str, attempt_id: str) -> str:
    job_id = ledger.attempt(attempt_id).job_id
    return _job_name_for(ledger, project, job_id)


def _snapshot_dir(config: EngineConfig, attempt_id: str) -> Path:
    """Scratch location for the disposable acceptance snapshot (module
    docstring) -- under config.home, never under any project's repo, so
    it is never itself part of anything evidence.fingerprint covers."""
    return config.home / "acceptance" / ids.attempt_dirname(attempt_id)


_SNAPSHOT_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "engine", "GIT_AUTHOR_EMAIL": "engine@local",
    "GIT_COMMITTER_NAME": "engine", "GIT_COMMITTER_EMAIL": "engine@local",
}


def _make_snapshot(repo: Path, dest: Path, fp: str) -> None:
    """Materialize exactly the content `fp` (evidence.fingerprint's tree
    hash for `repo`, taken by the caller BEFORE any scene runs) into a
    fresh, self-contained copy at `dest` -- `git archive <tree hash>`
    extracted, then `git init`/`add -A`/`commit` with a fixed identity
    (same pattern as engine.steps.open_job._make_copy's worker copy),
    so `dest` is a normal git worktree a test runner can `git status`
    against rather than a bare tar extraction. Removes any stale `dest`
    from a previous run of the same attempt first."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    archive = subprocess.run(
        git_argv(repo, "archive", fp), check=True, capture_output=True,
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)
    subprocess.run(git_argv(dest, "init", "-q"), check=True, capture_output=True)
    env = {**os.environ, **_SNAPSHOT_GIT_IDENTITY, "GIT_CONFIG_GLOBAL": "/dev/null"}
    subprocess.run(git_argv(dest, "add", "-A"), check=True,
                    capture_output=True, env=env)
    subprocess.run(git_argv(dest, "commit", "-q", "-m", "acceptance-snapshot"),
                    check=True, capture_output=True, env=env)


def _link_snapshot_deps(project_cfg, repo: Path, snapshot: Path) -> None:
    """Give the snapshot the same untracked dependency links a worker copy
    gets (project copy_links, e.g. node_modules) -- read through to the
    repo, so scenes and the full suite can resolve their test tools.

    Deliberately does NOT call engine.steps.open_job._link_copy_link_entry
    (used for WORKER role copies): that helper fails closed
    ("copy_link_source_escapes_repo") when `repo/entry` resolves outside
    `repo`, to guard against a worker's own git-tracked commit planting an
    intermediate symlink that makes a role copy -- built by git-archiving
    that very commit -- escape `repo`. That risk does not apply here:
    every current copy_links entry (node_modules) is also one of the
    project's own `excludes`, i.e. untracked, written straight onto
    `repo`'s filesystem by engine/tooling infra (tools/gb2's worktree
    setup, npm install) and never by anything in a worker's commit; the
    snapshot's own git-archive step (_make_snapshot, run before this)
    already strips every untracked path out, so there is no tracked-tree
    content here that could smuggle an escaping symlink. `repo/entry`
    resolving outside `repo` is therefore not failed closed on below --
    linking the snapshot to it exposes nothing beyond what every
    ordinary, non-acceptance bench run already reads straight out of
    `repo` via the sandbox's default `--ro-bind / /` (engine.sandbox.wrap
    module docstring), and this project's own node_modules IS exactly
    this shape (tools/gb2's worktree setup can leave it a symlink to an
    external node_modules tree, e.g. GB2_TEST_NODE_MODULES in
    tests/acceptance/test_t3_gb2_node_e2e.py -- confirmed empirically:
    with the escape check, the snapshot ended up with no node_modules at
    all, `npx vitest run` fell back to the registry, and acceptance
    failed closed with "tests_failed"/npm EAI_AGAIN under net=False).
    `copy_link_writable_subpaths` are unaffected either way: they always
    become real, empty, LOCAL directories inside the snapshot, never
    symlinked to the repo or its target (see the per-child loop below) --
    nothing a scene writes there can reach the real repo or the external
    dependency tree. The DESTINATION-side checks (a pre-existing symlink
    in `dest`'s own parent chain, which COULD have been planted by the
    snapshot's own git-archived tracked tree) are still enforced below,
    since that risk is unrelated to where the source lives."""
    dest_real = Path(os.path.realpath(snapshot))
    writable_subpaths_by_entry = getattr(project_cfg, "copy_link_writable_subpaths", {}) or {}
    for entry in getattr(project_cfg, "copy_links", ()) or ():
        _link_snapshot_dep_entry(
            dest_real, snapshot, repo, entry,
            tuple(writable_subpaths_by_entry.get(entry, ())),
        )


def _link_snapshot_dep_entry(dest_real: Path, dest: Path, repo: Path, entry: str,
                             writable_subpaths: tuple[str, ...] = ()) -> None:
    """One copy_links entry re-exposed inside the acceptance snapshot --
    see _link_snapshot_deps for why this does not reuse (and does not
    need) engine.steps.open_job._link_copy_link_entry's source-
    containment check. Silently does nothing for a missing/unsafe case
    (same fail-closed-but-non-raising shape as that function), since a
    missing test dependency surfaces on its own as a scene/full-suite
    failure rather than needing a second signal here."""
    target = repo / entry
    if not target.exists():
        return
    current = dest
    for part in Path(entry).parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            return  # destination parent already a symlink -- unsafe, skip
    current.mkdir(parents=True, exist_ok=True)
    parent_real = Path(os.path.realpath(current))
    if parent_real != dest_real and not _is_within(parent_real, dest_real):
        return  # destination parent escapes the snapshot -- unsafe, skip

    if not writable_subpaths:
        (dest / entry).symlink_to(target)
        return

    if not target.is_dir():
        return  # writable_subpaths only make sense for a directory entry
    entry_dest = dest / entry
    entry_dest.mkdir(parents=True, exist_ok=True)
    writable_names = set(writable_subpaths)
    for child_name in os.listdir(target):
        if child_name in writable_names:
            continue  # the writable subpath wins; never symlinked to the repo
        (entry_dest / child_name).symlink_to(target / child_name)
    for subpath in writable_subpaths:
        (entry_dest / subpath).mkdir(parents=True, exist_ok=True)


def _write_acceptance_json(
    config: EngineConfig, project: str, job_name: str, passed: bool,
    full_suite_passed: bool, full_suite_returncode: int, full_suite_tail: str,
    scenes: tuple[SceneResult, ...], tree_changed: bool = False,
    fingerprint: str = "", snapshot_mutated: bool = False,
) -> Path:
    path = acceptance_path(config, project, job_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "job": job_name,
        "passed": passed,
        "tree_changed": tree_changed,
        "snapshot_mutated": snapshot_mutated,
        "fingerprint": fingerprint,
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


def _tracked_mutated_paths(snapshot: Path) -> tuple[str, ...]:
    """Relative paths in `snapshot`'s own `git status --porcelain -z
    --untracked-files=all` that are NOT a bare untracked addition
    (status "??") -- a scene creating a new scratch/output file is not
    a mutation of the certified tree; a scene that edits, stages,
    deletes or renames a path HEAD already tracks (_make_snapshot's
    own commit) is. Same call/rename-pair handling as
    engine.steps.open_job.dirty_paths, filtered down to tracked
    changes only."""
    out = subprocess.run(
        git_argv(snapshot, "status", "--porcelain", "-z", "--untracked-files=all"),
        check=True, capture_output=True,
    ).stdout
    tokens = out.decode("utf-8", "surrogateescape").split("\0")
    paths: list[str] = []
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if not entry:
            continue
        status, rel = entry[:2], entry[3:]
        tracked = status != "??"
        if tracked:
            paths.append(rel)
        if status[0] in ("R", "C") and i < len(tokens) and tokens[i]:
            if tracked:
                paths.append(tokens[i])
            i += 1
    return tuple(paths)


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
    if before_fp is None:
        # Nothing to certify: a fingerprint that cannot be computed is
        # never trusted as proof of what tree scenes would even be
        # accepting (see _fingerprint_or_none) -- fail the same way a
        # tree that changed mid-run does, without running anything.
        acc = AcceptanceResult(passed=False, scenes=(), full_suite_passed=False,
                               tree_changed=True)
        _write_acceptance_json(config, project, job_name, False, False, -1,
                               "no_fingerprint", (), tree_changed=True)
        _record(ledger, project, attempt_id, job_name, acc, reason="tree_changed")
        return acc

    snapshot = _snapshot_dir(config, attempt_id)
    _make_snapshot(repo, snapshot, before_fp)
    _link_snapshot_deps(config.projects[project], repo, snapshot)

    # Scenes and the full suite run against `snapshot`, never `repo`
    # itself -- see module docstring for why. `repo` is still passed as
    # extra_readonly: it is the real project repo's own parent directory
    # that config.ledger_path.parent (_bench_sandbox_argv's `hidden`)
    # hides, so without this, every _link_snapshot_deps symlink pointing
    # back into `repo` (e.g. node_modules) would resolve to nothing
    # inside this sandbox -- read-only, so scenes still cannot write
    # back into the real repo through it.
    scenes: list[SceneResult] = []
    snapshot_mutated = False
    full_suite_passed, full_suite_returncode, full_suite_tail = False, -1, "not_run"
    for scene in plan.scenes:
        scenes.append(_run_scene(config, project, attempt_id, snapshot, env,
                                 timeout_sec, scene, extra_readonly=(repo,)))
        # Fail fast the instant a scene leaves the snapshot's own
        # HEAD-tracked content mutated: a scene that patches an
        # implementation file must never let a LATER scene or the full
        # suite run (and pass) against that altered tree instead of the
        # one before_fp certified. Remaining scenes/the full suite never
        # run once this trips.
        if _tracked_mutated_paths(snapshot):
            snapshot_mutated = True
            break
    if not snapshot_mutated:
        full_suite_passed, full_suite_returncode, full_suite_tail = _run_full_suite(
            config, project, attempt_id, snapshot, env, timeout_sec, extra_readonly=(repo,))
        if _tracked_mutated_paths(snapshot):
            snapshot_mutated = True

    # Second, independent guard: the REAL repo's own fingerprint, which
    # nothing above ever ran against, should be unchanged. A difference
    # here means something touched `repo` itself during this call.
    after_fp = _fingerprint_or_none(repo, project_cfg.excludes)
    tree_changed = after_fp is None or before_fp != after_fp

    passed = (not snapshot_mutated and full_suite_passed
              and all(s.passed for s in scenes) and not tree_changed)
    scenes_t = tuple(scenes)
    acc = AcceptanceResult(
        passed=passed, scenes=scenes_t, full_suite_passed=full_suite_passed,
        full_suite_returncode=full_suite_returncode, full_suite_tail=full_suite_tail,
        tree_changed=tree_changed, fingerprint=before_fp,
        snapshot_mutated=snapshot_mutated,
    )
    _write_acceptance_json(config, project, job_name, passed, full_suite_passed,
                           full_suite_returncode, full_suite_tail, scenes_t, tree_changed,
                           fingerprint=before_fp, snapshot_mutated=snapshot_mutated)
    if passed:
        reason = None
    elif snapshot_mutated:
        reason = "snapshot_mutated"
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
            "snapshot_mutated": acc.snapshot_mutated,
            "fingerprint": acc.fingerprint,
            "reason": reason,
        },
    ))


_PROBE_EXTENSIONS = (".py", ".sh", ".js")
_PROBE_INTERPRETERS: dict[str, tuple[str, ...]] = {
    ".py": (sys.executable,),
    ".sh": ("sh",),
    ".js": ("node",),
}


def _probe_script_path(repo: Path, job: str, probe_id: str) -> Path | None:
    probe_dir = repo / ".gongbang" / job / "probe"
    for ext in _PROBE_EXTENSIONS:
        candidate = probe_dir / f"{probe_id}{ext}"
        if candidate.exists():
            return candidate
    return None


def _probe_argv(script: Path) -> list[str]:
    return [*_PROBE_INTERPRETERS[script.suffix], str(script)]


def _probe_snapshot_dir(config: EngineConfig, attempt_id: str) -> Path:
    """Scratch location for the disposable probe-run snapshot -- a
    sibling of _snapshot_dir's own acceptance snapshot (same `config.
    home`-rooted, never-under-any-repo placement), kept at its own
    "probe" leaf so a job that runs both run_probe_scripts and
    run_acceptance for the same attempt_id never has one call's
    shutil.rmtree(dest) (see _make_snapshot) race or clobber the
    other's snapshot directory."""
    return config.home / "acceptance" / ids.attempt_dirname(attempt_id) / "probe"


def _record_probe_executed(ledger: Ledger, project: str, attempt_id: str,
                           probe_id: str, name: str, script: str, returncode: int,
                           stdout_tail: str) -> None:
    ledger.append(Event(
        project=project, kind="probe.executed", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"id": probe_id, "name": name, "script": script,
                 "returncode": returncode, "stdout_tail": stdout_tail},
    ))


def run_probe_scripts(
    config: EngineConfig, ledger: Ledger, project: str, attempt_id: str,
    job_name: str, targets: list[tuple[str, str]],
) -> tuple["ProbeEvidence", ...]:
    """Runs each target's own probe script (see module docstring) inside
    a disposable snapshot of the repo's current fingerprinted tree, and
    appends one probe.executed ledger event per target, in order. Each
    target's script/log id is engine.gates.probe_target_id(i) -- its
    1-based POSITION in `targets`, never derived from its name -- so
    two targets can never collide onto the same id/script/log
    regardless of how similar or non-ASCII their names are (see module
    docstring). A target with no script at any of the known extensions
    gets returncode=-1, stdout_tail="script_missing", ok=False -- never
    raises, so one missing script does not stop the rest of the
    targets from being probed and recorded.

    The snapshot is rebuilt fresh from whatever the repo's own
    fingerprinted tree is RIGHT NOW (never a tree recorded earlier by
    some other step) -- the caller (engine.full_orbit) always commits
    the prober's own changes to the repo before calling this, so the
    snapshot always contains the just-committed probe scripts. A
    fingerprint that cannot be computed at all (_fingerprint_or_none)
    fails every target closed, the same "script_missing"-shaped,
    never-raising outcome, under its own stdout_tail reason
    "no_fingerprint", rather than running anything against an
    unreadable tree."""
    project_cfg = config.projects[project]
    repo = project_cfg.repo
    env = _bench_env(_bench_ctx(config, project, attempt_id))
    timeout_sec = project_cfg.test_timeout_sec
    results: list[ProbeEvidence] = []
    fp = _fingerprint_or_none(repo, project_cfg.excludes)
    if fp is None:
        for i, (name, _url) in enumerate(targets):
            probe_id = gates.probe_target_id(i)
            _record_probe_executed(ledger, project, attempt_id, probe_id, name,
                                   "", -1, "no_fingerprint")
            results.append(ProbeEvidence(
                id=probe_id, name=name, script="", returncode=-1,
                stdout_tail="no_fingerprint", ok=False))
        return tuple(results)

    snapshot = _probe_snapshot_dir(config, attempt_id)
    _make_snapshot(repo, snapshot, fp)
    _link_snapshot_deps(project_cfg, repo, snapshot)

    for i, (name, _url) in enumerate(targets):
        probe_id = gates.probe_target_id(i)
        script = _probe_script_path(snapshot, job_name, probe_id)
        if script is None:
            _record_probe_executed(ledger, project, attempt_id, probe_id, name,
                                   "", -1, "script_missing")
            results.append(ProbeEvidence(
                id=probe_id, name=name, script="", returncode=-1,
                stdout_tail="script_missing", ok=False))
            continue
        returncode, tail = _run_in_bench_sandbox(
            config, project, attempt_id, snapshot, _probe_argv(script), env, timeout_sec,
            net=True, extra_readonly=(repo,))
        ok = returncode == 0 and script.exists()
        _record_probe_executed(ledger, project, attempt_id, probe_id, name,
                               str(script), returncode, tail)
        results.append(ProbeEvidence(
            id=probe_id, name=name, script=str(script), returncode=returncode,
            stdout_tail=tail, ok=ok))
    return tuple(results)
