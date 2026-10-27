"""acceptance -- the (scripted, non-step) check that a job's own pass
scenes and the project's real test suite both come out green, run after
the last build task's commit (ENGINE.md full-orbit flow: build loop ->
acceptance -> handoff). Not an engine.application.processor request kind: called
directly by engine.application.pipeline as a plain function (the full-orbit loop
owns retry/fix-round bookkeeping, not this module), so there is no
StepContext/reconcile here -- re-running it just re-executes every
scene and the full suite again, which is always safe (both are read-only
checks of the repo's current tree).

Scene/full-suite execution (engine.application.acceptance_scene) and running each
"PROBE OK" target script (engine.application.acceptance_probe) both run through the
one sandboxed-subprocess primitive in engine.application.acceptance_bench, which
reuses engine.application.steps.suite_run's own sandbox-argv builder and env rules
exactly -- acceptance stages get exactly the same isolation a bench
request gets, not a second, divergent one.

Acceptance integrity: scenes and the full suite never run against the
real repo at all. The repo's own fingerprint (engine.infrastructure.evidence.
fingerprint) is taken ONCE, before anything runs, and EVERY scene and
the full suite gets its OWN disposable snapshot holding exactly that
content (git archive <that tree hash> -> fresh git init/commit, same
pattern engine.application.steps.open_job._make_copy uses for a worker copy),
rebuilt fresh immediately before that stage runs, under
config.home/"acceptance"/<attempt>/<label> (bench_snapshot.snapshot_dir's
"scene-<i>"/"full_suite" labels). This is deliberate, and stronger than
one shared snapshot every stage ran against in turn (the earlier shape
of this function): a before/after fingerprint comparison taken around a
scene that runs IN the real repo cannot catch a scene that mutates a
file, runs the test, then restores it before the comparison's "after"
read -- the restore makes the diff look clean; and even checking the
snapshot's own tracked `git status` after each stage (still done, see
below) cannot catch a scene that leaves behind UNTRACKED residue --
a dropped config file, a poisoned cache/output directory, a stray
compiled artifact -- since none of that is HEAD-tracked content a
`git status` diff would ever flag. Giving every stage a brand-new
snapshot, built the same way bench_snapshot.make_snapshot/
link_snapshot_deps built the first one, means no stage can inherit
anything -- tracked OR untracked -- that a prior stage's own process
left on disk; the only thing every snapshot has in common is the one
fingerprint fixed before any stage ran. The real repo's OWN fingerprint
is still re-checked after the full suite finishes, as a second,
independent guard -- it should never differ (nothing ran against
`repo` itself), and a difference (tree_changed=True, passed forced
False, same as before) means something unexpected happened to touch
the real repo during this call at all.

Each stage's own fresh snapshot still stays writable to that stage for
a scene creating a new, untracked scratch/output file (legitimate --
see test_scene_mutation_runs_against_a_snapshot_not_the_real_repo), so
fixing the fingerprint once up front is not by itself enough: a scene
that could freely rewrite a tracked implementation file inside its own
snapshot, run its scene command against the patched version, then
restore the original bytes before anything re-checks `git status`
would certify a scene that in fact ran against different code than
what gets committed, leaving no tracked diff behind to catch
afterwards. Before anything in a stage runs, run_acceptance locks every
HEAD-tracked file in that stage's own fresh snapshot read-only
(bench_snapshot.tracked_file_paths + lock_tracked_files): the scene's
own open(path, "w")/write_text on a certified file now fails outright
(PermissionError), regardless of sandbox mode, rather than being let
through and only possibly caught after the fact. The same tracked list
is also passed to the bench sandbox as extra read-only binds
(sandbox="bwrap" only) so a scene cannot work around the plain chmod by
deleting and recreating a tracked path either (confirmed empirically:
EBUSY on unlink, not EACCES) -- under sandbox="none" that one
delete-and-recreate path remains open (same same-UID limitation this
codebase documents elsewhere for that mode). run_acceptance still
checks each snapshot's own `git status` for any HEAD-tracked (not
bare-untracked) change right after that stage finishes
(acceptance_scene._tracked_mutated_paths) as a second, independent
guard -- the first stage that trips it stops the run right there
(snapshot_mutated=True, passed=False, reason "snapshot_mutated") -- no
later scene or the full suite ever runs at all once this trips (their
own snapshots are simply never built).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from engine.infrastructure import evidence
from engine.config import EngineConfig
from engine.domain.records import Event
from engine.application.steps.suite_run import bench_env
from engine.application.steps.suite_snapshot import lock_tracked_files as _lock_tracked_files
from engine.application.steps.suite_snapshot import link_snapshot_deps as _link_snapshot_deps
from engine.application.steps.suite_snapshot import make_snapshot as _make_snapshot
from engine.application.steps.suite_snapshot import snapshot_dir as _snapshot_dir
from engine.application.steps.suite_snapshot import tracked_directories as _tracked_directories
from engine.application.steps.suite_snapshot import tracked_file_paths as _tracked_file_paths
from engine.application.steps.open_job import job_name_for as _job_name_for

from engine.application.acceptance_bench import bench_ctx
from engine.application.acceptance_probe import ProbeEvidence, run_probe_scripts
from engine.application.acceptance_scene import (
    SceneResult,
    run_full_suite as _run_full_suite,
    run_scene as _run_scene,
    tracked_mutated_paths as _tracked_mutated_paths,
)

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger
    from engine.domain.plan import Plan


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
    # The tree hash (engine.infrastructure.evidence.fingerprint) certified by this run --
    # taken once, before the snapshot was built, from the real repo.
    # "" iff nothing was ever accepted (no_scenes, or the fingerprint
    # itself could not be computed) -- never trust an empty string as
    # "the empty tree"; callers (release/handoff) must treat it the same
    # as passed=False.
    fingerprint: str = ""
    # a review finding (ckon: `fingerprint` above stays the PLAIN
    # git tree hash -- engine.application.release_certificate.job_certificate_status
    # diffs it against real git trees (`git diff`), which only ever
    # works on an actual tree hash, never a value folded with
    # file_dep_targets' own content. This field is the SEPARATE,
    # file-dep-aware value (evidence.with_file_deps(fingerprint,
    # project_cfg.file_dep_targets)) that same caller additionally
    # compares, so a project whose shared package content changed
    # since this acceptance run -- even though the app's own repo
    # tree never moved -- is still caught as stale, the same gap
    # already closed for commit/bench/review evidence. "" whenever
    # `fingerprint` itself is "" (nothing to fold).
    evidence_fingerprint: str = ""
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


def acceptance_path(config: EngineConfig, project: str, job_name: str) -> Path:
    return config.projects[project].repo / ".gongbang" / job_name / "acceptance.json"


def _job_name(ledger: Ledger, project: str, attempt_id: str) -> str:
    job_id = ledger.attempt(attempt_id).job_id
    return _job_name_for(ledger, project, job_id)


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
    env = bench_env(bench_ctx(config, project, attempt_id))
    timeout_sec = project_cfg.test_timeout_sec
    job_name = _job_name(ledger, project, attempt_id)

    before_fp = _fingerprint_or_none(repo, project_cfg.excludes)
    if before_fp is None:
        # Nothing to certify: a fingerprint that cannot be computed is
        # never trusted as proof of what tree scenes would even be
        # accepting (see _fingerprint_or_none) -- fail the same way a
        # tree that changed mid-run does, without running anything.
        # (Checked before even loading a Plan: there is no certified
        # snapshot to load one FROM without a fingerprint.)
        acc = AcceptanceResult(passed=False, scenes=(), full_suite_passed=False,
                               tree_changed=True)
        _write_acceptance_json(config, project, job_name, False, False, -1,
                               "no_fingerprint", (), tree_changed=True)
        _record(ledger, project, attempt_id, job_name, acc, reason="tree_changed")
        return acc

    # Whether this job's plan was EVER reviewed (engine.domain.plan.
    # is_plan_reviewed) -- not whether the plan object a caller passed
    # in still claims any `covers` (the previous gating condition, and
    # the exact loophole a plan.json edited after review to simply
    # delete every `covers` entry could walk through: stripping
    # `covers` would otherwise have silently disabled the very check
    # meant to catch stripping `covers`, see is_plan_reviewed's own
    # docstring). When reviewed, `plan` is REPLACED here with the Plan
    # parsed from THIS call's own certified snapshot (never the live
    # repo, and never the caller's already-parsed object, which could
    # have been parsed before a later edit) -- a job with no review on
    # record at all (the test-only Plan/Scene stub some unit tests
    # still use, which never calls engine.domain.plan.record_plan_reviewed)
    # has no certified snapshot-plan to prefer over the caller's own,
    # so it keeps using exactly what the caller passed, unchanged from
    # before this check existed.
    from engine.domain.plan import PlanError, is_plan_reviewed, load_plan, verify_reviewed_scenes

    reviewed = is_plan_reviewed(ledger, project, job_name)
    if reviewed:
        plan_snapshot = _snapshot_dir(config, attempt_id, "plan")
        _make_snapshot(repo, plan_snapshot, before_fp)
        try:
            plan = load_plan(plan_snapshot, job_name)
        except PlanError as exc:
            reason = f"plan:{exc.reason}"
            acc = AcceptanceResult(passed=False, scenes=(), full_suite_passed=False,
                                   fingerprint=before_fp)
            _write_acceptance_json(config, project, job_name, False, False, -1,
                                   reason, (), fingerprint=before_fp)
            _record(ledger, project, attempt_id, job_name, acc, reason=reason)
            return acc
    elif any(getattr(scene, "covers", ()) for scene in plan.scenes):
        # Never reviewed at all, yet this plan claims to cover at least
        # one requirement -- a real (non-stub) plan reaching acceptance
        # in this shape is a production bug (review was skipped or
        # never ran), not something to quietly certify against whatever
        # the caller happened to parse. Only a plan with NO covers claim
        # anywhere (the test-only Plan/Scene stub some unit tests still
        # use, which has no `covers` field at all -- see getattr) keeps
        # the old, review-free path below unchanged.
        reason = "plan_never_reviewed"
        acc = AcceptanceResult(passed=False, scenes=(), full_suite_passed=False,
                               fingerprint=before_fp)
        _write_acceptance_json(config, project, job_name, False, False, -1,
                               reason, (), fingerprint=before_fp)
        _record(ledger, project, attempt_id, job_name, acc, reason=reason)
        return acc

    if not plan.scenes:
        acc = AcceptanceResult(passed=False, scenes=(), full_suite_passed=False,
                               fingerprint=before_fp)
        _write_acceptance_json(config, project, job_name, False, False, -1,
                               "no_scenes", (), fingerprint=before_fp)
        _record(ledger, project, attempt_id, job_name, acc, reason="no_scenes")
        return acc

    # Every scene and the full suite gets its OWN fresh snapshot, built
    # right before that stage runs (see module docstring) -- never one
    # snapshot shared/reused across stages. `repo` is still passed as
    # extra_readonly to every stage: it is the real project repo's own
    # parent directory that config.ledger_path.parent
    # (bench_run._bench_sandbox_argv's `hidden`) hides, so without this,
    # every link_snapshot_deps symlink pointing back into `repo` (e.g.
    # node_modules) would resolve to nothing inside this sandbox --
    # read-only, so a stage still cannot write back into the real repo
    # through it.
    #
    # The reviewed-plan digest check (engine.domain.plan.verify_reviewed_scenes)
    # runs AGAINST EACH STAGE'S OWN SNAPSHOT, right after that snapshot
    # is built and before anything in it executes -- never against
    # `repo` itself. Checking the work tree and then building a
    # snapshot from a fingerprint taken afterwards (the previous shape
    # of this function) left a window between "what was checked" and
    # "what gets executed": the engine never re-reads plan.json/scene
    # files after this check, but repo is a live working tree another
    # process could still be writing to, so the checked bytes and the
    # executed bytes were only guaranteed equal by timing, not by
    # construction. Checking the snapshot itself closes that gap -- the
    # digest comparison and the scene run now read the exact same,
    # already-materialized, nobody-else-can-touch-it files. Skipped
    # entirely only for a job with NO plan.reviewed record at all
    # (`reviewed` above, computed once from the ledger before anything
    # here ran) -- never based on whether the CURRENT plan still
    # claims any `covers` (see is_plan_reviewed's own docstring for
    # why that used to be the wrong, strippable gate).
    scenes: list[SceneResult] = []
    snapshot_mutated = False
    plan_problems: list[str] = []
    full_suite_passed, full_suite_returncode, full_suite_tail = False, -1, "not_run"
    for i, scene in enumerate(plan.scenes):
        scene_snapshot = _snapshot_dir(config, attempt_id, f"scene-{i}")
        _make_snapshot(repo, scene_snapshot, before_fp)
        if reviewed:
            plan_problems = verify_reviewed_scenes(
                ledger, project, job_name, scene_snapshot)
            if plan_problems:
                break
        scene_tracked = _tracked_file_paths(scene_snapshot)
        _lock_tracked_files(scene_tracked)
        scene_tracked_dirs = _tracked_directories(scene_snapshot)
        _link_snapshot_deps(config.projects[project], repo, scene_snapshot)
        scenes.append(_run_scene(config, project, attempt_id, scene_snapshot, env,
                                 timeout_sec, scene,
                                 extra_readonly=(repo, *scene_tracked, *scene_tracked_dirs)))
        # Fail fast the instant a scene leaves ITS OWN snapshot's
        # HEAD-tracked content mutated: a scene that patches an
        # implementation file must never let a LATER scene or the full
        # suite run at all -- their own snapshots are simply never
        # built once this trips.
        if _tracked_mutated_paths(scene_snapshot):
            snapshot_mutated = True
            break
    if plan_problems:
        reason = ";".join(plan_problems)
        acc = AcceptanceResult(passed=False, scenes=tuple(scenes), full_suite_passed=False)
        _write_acceptance_json(config, project, job_name, False, False, -1,
                               reason, tuple(scenes))
        _record(ledger, project, attempt_id, job_name, acc, reason=reason)
        return acc
    if not snapshot_mutated:
        suite_snapshot = _snapshot_dir(config, attempt_id, "full_suite")
        _make_snapshot(repo, suite_snapshot, before_fp)
        if reviewed:
            plan_problems = verify_reviewed_scenes(
                ledger, project, job_name, suite_snapshot)
            if plan_problems:
                reason = ";".join(plan_problems)
                acc = AcceptanceResult(passed=False, scenes=tuple(scenes),
                                       full_suite_passed=False)
                _write_acceptance_json(config, project, job_name, False, False, -1,
                                       reason, tuple(scenes))
                _record(ledger, project, attempt_id, job_name, acc, reason=reason)
                return acc
        suite_tracked = _tracked_file_paths(suite_snapshot)
        _lock_tracked_files(suite_tracked)
        suite_tracked_dirs = _tracked_directories(suite_snapshot)
        _link_snapshot_deps(config.projects[project], repo, suite_snapshot)
        full_suite_passed, full_suite_returncode, full_suite_tail = _run_full_suite(
            config, project, attempt_id, suite_snapshot, env, timeout_sec,
            extra_readonly=(repo, *suite_tracked, *suite_tracked_dirs))
        if _tracked_mutated_paths(suite_snapshot):
            snapshot_mutated = True

    # Second, independent guard: the REAL repo's own fingerprint, which
    # nothing above ever ran against, should be unchanged. A difference
    # here means something touched `repo` itself during this call.
    after_fp = _fingerprint_or_none(repo, project_cfg.excludes)
    tree_changed = after_fp is None or before_fp != after_fp

    passed = (not snapshot_mutated and full_suite_passed
              and all(s.passed for s in scenes) and not tree_changed)
    scenes_t = tuple(scenes)
    evidence_fp = evidence.with_file_deps(before_fp, project_cfg.file_dep_targets)
    acc = AcceptanceResult(
        passed=passed, scenes=scenes_t, full_suite_passed=full_suite_passed,
        full_suite_returncode=full_suite_returncode, full_suite_tail=full_suite_tail,
        tree_changed=tree_changed, fingerprint=before_fp, evidence_fingerprint=evidence_fp,
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
            "evidence_fingerprint": acc.evidence_fingerprint,
            "reason": reason,
        },
    ))
