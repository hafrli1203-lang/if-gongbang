"""Unit tests for engine.orbit: stage sequencing, stopping on a failed
request, stopping with needs_owner when a station asks for something,
and resuming via stages_after_install.

engine.steps.station / engine.steps.review (Task 3) are not yet merged
into this branch's engine.steps.KINDS, so "run_station" and "review" are
stubbed here via Processor.register() -- closely enough to the real
contract (a "station.finished" event carrying "requests"; a "review"
evidence row carrying {"passed": bool}) to exercise orbit's control flow
without depending on Task 3's module. merge/bench/commit/report/close are
the real engine.steps.* implementations already in this branch."""
from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

from engine import evidence, orbit
from engine.approvals import grant
from engine.config import ReleaseConfig
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.ledger import Event
from engine.processor import Fakes, Processor, StepContext, StepDef, submit
from engine.steps.open_job import copy_root, open_job
from tests.conftest import GIT_ENV, Workspace
from tests.fixtures.sample_app import make_sample


def _fakes() -> Fakes:
    return Fakes(publisher=FakePublisher(), installer=FakeInstaller())


def _station_finished_steps(requests: list[dict[str, object]]):
    """Stub run_station step list: appends the same station.finished
    event shape engine.steps.station.record_station will append, with the
    given `requests` (empty for a plain "the station is done" outcome)."""
    def _run(ctx: StepContext) -> dict[str, object]:
        ctx.ledger.append(Event(
            project=ctx.project, kind="station.finished", actor_kind="processor",
            actor_id="processor", attempt_id=ctx.attempt_id,
            payload={
                "request_id": ctx.request.request_id,
                "station": ctx.request.payload.get("station"),
                "role": ctx.request.payload.get("role"),
                "ok": True, "summary": "stub", "cost_usd": 0.01, "turns": 1,
                "changed_paths": [], "requests": list(requests),
            },
        ))
        return {}

    def _reconcile(ctx: StepContext) -> bool:
        for _seq, event in ctx.ledger.events(ctx.project, "station.finished"):
            if event.payload.get("request_id") == ctx.request.request_id:
                return True
        return False

    return lambda ctx: [StepDef("record_station", _run, _reconcile)]


def _review_steps(passed: bool):
    """Stub review step list: records review evidence at the repo's
    current fingerprint, same as engine.steps.review.record_review."""
    def _run(ctx: StepContext) -> dict[str, object]:
        project_config = ctx.config.projects[ctx.project]
        fp = evidence.fingerprint(project_config.repo, project_config.excludes)
        evidence.record(
            ctx.ledger, ctx.project, ctx.attempt_id, "review", fp,
            payload={"passed": passed, "request_id": ctx.request.request_id, "findings": []},
        )
        return {}

    def _reconcile(ctx: StepContext) -> bool:
        for row in ctx.ledger.evidence(ctx.attempt_id, "review"):
            if row.payload.get("request_id") == ctx.request.request_id:
                return True
        return False

    return lambda ctx: [StepDef("record_review", _run, _reconcile)]


def _processor_factory(run_station_requests: list[dict[str, object]] | None = None,
                       review_passed: bool = True):
    def factory(config, ledger, fakes) -> Processor:
        proc = Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", _station_finished_steps(run_station_requests or []))
        proc.register("review", _review_steps(review_passed))
        return proc
    return factory


def _sample_workspace(workspace: Workspace, variant: int) -> Workspace:
    """Repoint 'alpha' at a fresh sample app repo (plan Task 5 §8),
    mirroring tests/unit/test_steps_bench.py's _bench_workspace."""
    sample = make_sample(workspace.root / "sample", variant, Path(sys.executable))
    project_config = replace(
        workspace.config.projects["alpha"],
        repo=sample.repo, test_cmd=sample.test_cmd,
        install_cmd=sample.install_cmd, manifest=sample.manifest,
        bench_cases=sample.bench_cases,
    )
    new_config = replace(
        workspace.config,
        projects={**workspace.config.projects, "alpha": project_config},
    )
    return Workspace(workspace.root, new_config, workspace.ledger,
                     sample.repo, workspace.beta)


def test_default_stages_order(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "wording",
                          "full", ("builder", "reviewer"))
    stages = orbit.default_stages(workspace.config, attempt_id, "builder", "reviewer",
                                  "fix the wording", "wording")
    assert [s.kind for s in stages] == [
        "run_station", "merge", "review", "bench", "commit", "report", "close",
    ]
    assert stages[0].payload == {
        "station": "build", "role": "builder", "task": "fix the wording",
    }
    assert stages[1].payload == {"copies": [
        {"role": "builder", "station": "build",
         "path": str(copy_root(workspace.config, attempt_id, "builder"))},
    ]}
    assert stages[2].payload == {"role": "reviewer", "task": "fix the wording"}
    assert stages[3].payload == {}
    assert stages[4].payload == {"message": "gongbang: wording"}
    assert stages[6].payload == {}


def test_stops_on_failed_request(workspace: Workspace) -> None:
    # bench_cases is left unset (ProjectConfig default None): trust_checks
    # raises StepFailed("no_bench_cases") before touching anything else --
    # a clean, deterministic failure to prove the orbit stops right there
    # with no later stage ever submitted.
    project_config = replace(workspace.config.projects["alpha"], test_cmd=("true",))
    config = replace(workspace.config,
                     projects={**workspace.config.projects, "alpha": project_config})
    attempt_id = open_job(config, workspace.ledger, "alpha", "will-fail",
                          "full", ("builder",))

    outcome = orbit.continue_job(
        config, workspace.ledger, _fakes(), "alpha", attempt_id,
        [orbit.Stage("bench", {}), orbit.Stage("close", {})],
    )

    assert outcome.status == "failed"
    assert outcome.reason == "no_bench_cases"
    assert outcome.failed_request is not None
    assert outcome.completed == ()
    # close (the second stage) must never have been reached.
    assert workspace.ledger.project("alpha").state == "OPEN"


def test_needs_owner_on_worker_requests(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "ask-owner",
                          "full", ("builder",))
    pending = [{"kind": "install", "manifest": "requirements.txt",
               "change": {"add_lines": ["vendored_pkg==0.1.0"]}}]

    outcome = orbit.continue_job(
        workspace.config, workspace.ledger, _fakes(), "alpha", attempt_id,
        [orbit.Stage("run_station", {"station": "build", "role": "builder", "task": "t"}),
         orbit.Stage("merge", {"copies": []})],
        processor_factory=_processor_factory(run_station_requests=pending),
    )

    assert outcome.status == "needs_owner"
    assert outcome.pending_requests == tuple(pending)
    # merge (the stage right after run_station) still runs -- the
    # station's own file changes land in the repo before the owner is
    # asked for anything further -- so it shows up as completed too.
    assert outcome.completed == ("run_station", "merge")


def test_continue_reruns_review_and_bench_after_install(workspace: Workspace) -> None:
    """stages_after_install deliberately starts at "review", not "bench"
    or later: evidence recorded before an install must not be trusted --
    continue_job running this stage list must produce fresh review and
    bench evidence rather than relying on whatever was already there
    (engine.steps.install.invalidate_evidence marks exactly this evidence
    stale in the real pipeline; this test isolates orbit's own sequencing
    from Task 4's install step by pre-seeding evidence under a request id
    that could never be this run's own)."""
    ws = _sample_workspace(workspace, variant=1)
    attempt_id = open_job(ws.config, ws.ledger, "alpha", "after-install",
                          "full", ("reviewer",))
    pre_install_fp = "0" * 40
    evidence.record(ws.ledger, "alpha", attempt_id, "review", pre_install_fp,
                    payload={"passed": True, "request_id": "pre-install", "findings": []})
    evidence.record(ws.ledger, "alpha", attempt_id, "bench", pre_install_fp,
                    payload={"passed": True, "reason": "ok", "request_id": "pre-install"})

    stages = orbit.stages_after_install(ws.config, attempt_id, "builder", "reviewer",
                                        "after-install")
    outcome = orbit.continue_job(
        ws.config, ws.ledger, _fakes(), "alpha", attempt_id, stages,
        processor_factory=_processor_factory(review_passed=True),
    )

    assert outcome.status == "done"
    assert outcome.completed == ("review", "bench", "commit", "report", "close")
    review_rows = ws.ledger.evidence(attempt_id, "review")
    bench_rows = ws.ledger.evidence(attempt_id, "bench")
    assert len(review_rows) == 2
    assert len(bench_rows) == 2
    assert review_rows[-1].payload["request_id"] != "pre-install"
    assert bench_rows[-1].payload["request_id"] != "pre-install"
    assert ws.ledger.project("alpha").state == "CLOSED"


def test_bench_stage_payload_carries_fingerprint(workspace) -> None:
    from engine import evidence, orbit
    stage = orbit.Stage("bench", {})
    payload = orbit._stage_payload(workspace.config, "alpha", stage)
    project = workspace.config.projects["alpha"]
    assert payload["fp"] == evidence.fingerprint(project.repo, project.excludes)
    review_payload = orbit._stage_payload(workspace.config, "alpha", orbit.Stage("review", {"role": "r"}))
    assert review_payload == {"role": "r", "fp": payload["fp"]}
    assert orbit._stage_payload(workspace.config, "alpha", orbit.Stage("close", {})) == {}


def _review_steps_parallel(events: list[tuple[str, float]]):
    """Stub review step list matching the real contract's step NAMES
    (spawn_reviewer / await_reviewer / record_review -- orbit's
    parallel_review_bench path only knows to stop_after="spawn_reviewer"
    by name), close enough to prove overlap: spawn_reviewer forks a real
    `sleep 0.3` child process and returns immediately; await_reviewer
    blocks on it. `events` records monotonic timestamps so the test can
    assert bench's own run_tests started before this await returned."""
    procs: dict[str, subprocess.Popen] = {}

    def _spawn(ctx: StepContext) -> dict[str, object]:
        events.append(("review_spawn_start", time.monotonic()))
        proc = subprocess.Popen(["sleep", "0.3"])
        procs[ctx.request.request_id] = proc
        return {"pid": proc.pid}

    def _reconcile_spawn(ctx: StepContext) -> bool:
        return ctx.request.request_id in procs

    def _await(ctx: StepContext) -> dict[str, object]:
        procs[ctx.request.request_id].wait()
        events.append(("review_await_end", time.monotonic()))
        return {}

    def _reconcile_await(ctx: StepContext) -> bool:
        return False

    def _record(ctx: StepContext) -> dict[str, object]:
        project_config = ctx.config.projects[ctx.project]
        fp = evidence.fingerprint(project_config.repo, project_config.excludes)
        evidence.record(
            ctx.ledger, ctx.project, ctx.attempt_id, "review", fp,
            payload={"passed": True, "request_id": ctx.request.request_id, "findings": []},
        )
        return {}

    def _reconcile_record(ctx: StepContext) -> bool:
        for row in ctx.ledger.evidence(ctx.attempt_id, "review"):
            if row.payload.get("request_id") == ctx.request.request_id:
                return True
        return False

    return lambda ctx: [
        StepDef("spawn_reviewer", _spawn, _reconcile_spawn),
        StepDef("await_reviewer", _await, _reconcile_await),
        StepDef("record_review", _record, _reconcile_record),
    ]


def test_parallel_review_bench_overlap_and_failed_bench_still_records_review(
    workspace: Workspace,
) -> None:
    """config.parallel_review_bench (default True): review's dispatch
    step (spawn_reviewer, here a real `sleep 0.3` child process) must
    not block bench from starting -- bench's own run_tests must begin
    before review's await_reviewer returns. bench_cases is left unset
    on 'alpha' (ProjectConfig default None), so bench deterministically
    fails with "no_bench_cases" right after run_tests -- proving a
    failed bench still stops the job (status "failed", failed_request
    is the bench request) while the review's own evidence is still
    recorded (its spawned process ran and record_review wrote evidence)
    even though the job never reaches "done"."""
    events: list[tuple[str, float]] = []

    import engine.steps.bench_run as bench_run_module
    real_run_run_tests = bench_run_module._run_run_tests

    def _timed_run_tests(ctx):
        events.append(("bench_run_tests_start", time.monotonic()))
        return real_run_run_tests(ctx)

    import pytest
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(bench_run_module, "_run_run_tests", _timed_run_tests)
    try:
        project_config = replace(workspace.config.projects["alpha"], test_cmd=("true",))
        config = replace(workspace.config,
                         projects={**workspace.config.projects, "alpha": project_config})
        attempt_id = open_job(config, workspace.ledger, "alpha", "parallel-job",
                              "full", ("reviewer",))

        def factory(cfg, ledger, fakes) -> Processor:
            proc = Processor(cfg, ledger, fakes)
            proc.register_defaults()
            proc.register("review", _review_steps_parallel(events))
            return proc

        outcome = orbit.continue_job(
            config, workspace.ledger, _fakes(), "alpha", attempt_id,
            [orbit.Stage("review", {"role": "reviewer"}), orbit.Stage("bench", {}),
             orbit.Stage("close", {})],
            processor_factory=factory,
        )
    finally:
        monkeypatch.undo()

    assert outcome.status == "failed"
    assert outcome.reason == "no_bench_cases"
    assert outcome.completed == ("review",)  # bench itself never reached "done"

    event_times = dict(events)
    assert event_times["bench_run_tests_start"] < event_times["review_await_end"], (
        "bench's run_tests must start before review's spawned process is awaited, "
        f"not after: {events}"
    )

    review_evidence = workspace.ledger.evidence(attempt_id, "review")
    assert len(review_evidence) == 1
    assert review_evidence[0].payload["passed"] is True
    assert workspace.ledger.project("alpha").state == "OPEN"


def test_blocked_by_unrelated_unknown_request_is_pending(workspace: Workspace) -> None:
    """orbit must not treat a stage as completed when run_once refuses to
    process anything at all because some OTHER request for this project
    is 'unknown' (Processor._block_on_unknown) -- e.g. a prior publish
    whose response never arrived. continue_job must report "pending", not
    silently mark the never-run stage completed."""
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job-pub",
                          "full", ())
    artifact_hash = evidence.sha256_file(workspace.beta / "dist" / "bundle.js")
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                {"target": "store", "artifact_path": "dist/bundle.js",
                 "artifact_hash": artifact_hash}, "owner")
    grant(workspace.ledger, "beta", rid, ttl_sec=3600)
    fakes = Fakes(publisher=FakePublisher(fail_after_effect=True, lookup_fails=True),
                 installer=FakeInstaller())
    proc = Processor(workspace.config, workspace.ledger, fakes)
    proc.register_defaults()
    proc.run_once("beta")
    assert workspace.ledger.request(rid).status == "unknown"

    outcome = orbit.continue_job(
        workspace.config, workspace.ledger, fakes, "beta", attempt_id,
        [orbit.Stage("report", {"summary": "x"})],
    )

    assert outcome.status == "pending"
    assert outcome.reason in ("request_queued", "request_running")
    assert outcome.completed == ()


# ---------------------------------------------------------------------------
# release track: a job on a project with `release`
# configured stops at "needs_owner" / "release_approval" right after its
# commit stage, instead of running straight through to report/close --
# tools/gb2's ship/skip-ship pick it up from there via
# orbit.release_followup_stages.
# ---------------------------------------------------------------------------

def _release_project(workspace: Workspace, tmp_path: Path):
    """Give "alpha" a `release` config whose master_repo is a `git clone
    --shared` of alpha made BEFORE the attempt's own commit -- shared
    objects (via git's alternates) mean a commit alpha makes becomes
    visible to master_repo's own object database without any fetch,
    the same assumption tests/unit/test_release.py documents and
    verifies for engine.release's own git primitives directly."""
    master_repo = tmp_path / "release-master"
    subprocess.run(["git", "clone", "--shared", "-q", str(workspace.alpha), str(master_repo)],
                  check=True, capture_output=True, env=GIT_ENV)
    project_config = replace(
        workspace.config.projects["alpha"],
        release=ReleaseConfig(master_repo=master_repo, target="alpha-app"),
    )
    config = replace(workspace.config,
                     projects={**workspace.config.projects, "alpha": project_config})
    return config, master_repo


def test_commit_stops_at_needs_owner_for_release_configured_project(
    workspace: Workspace, tmp_path: Path,
) -> None:
    config, master_repo = _release_project(workspace, tmp_path)
    attempt_id = open_job(config, workspace.ledger, "alpha", "ship-me", "full", ())
    (workspace.alpha / "src" / "app.py").write_text("print('v2')\n", encoding="utf-8")
    fp = evidence.fingerprint(workspace.alpha, config.projects["alpha"].excludes)
    evidence.record(workspace.ledger, "alpha", attempt_id, "review", fp,
                    payload={"passed": True})
    evidence.record(workspace.ledger, "alpha", attempt_id, "bench", fp,
                    payload={"passed": True})

    outcome = orbit.continue_job(
        config, workspace.ledger, _fakes(), "alpha", attempt_id,
        [orbit.Stage("commit", {"message": "gongbang: ship-me"}),
         orbit.Stage("report", {"summary": "gongbang: ship-me"}),
         orbit.Stage("close", {})],
    )

    assert outcome.status == "needs_owner"
    assert outcome.reason == "release_approval"
    assert outcome.completed == ("commit",)  # report/close never ran
    assert workspace.ledger.project("alpha").state == "OPEN"

    pending = outcome.pending_requests
    assert len(pending) == 1
    assert pending[0]["kind"] == "publish"
    assert pending[0]["target"] == "alpha-app"
    sha = subprocess.run(["git", "-C", str(workspace.alpha), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True, env=GIT_ENV).stdout.strip()
    assert pending[0]["artifact_hash"] == sha
    assert pending[0]["owner_text"] == f"승인 {pending[0]['request_id']}"

    queued = workspace.ledger.queued_requests("alpha")
    assert [r.kind for r in queued] == ["publish"]
    assert queued[0].request_id == pending[0]["request_id"]
    # master untouched -- advance_master only runs once the owner grants
    # the approval (gb2 ship), never as a side effect of submitting it.
    master_head = subprocess.run(["git", "-C", str(master_repo), "rev-parse", "HEAD"],
                                 check=True, capture_output=True, text=True,
                                 env=GIT_ENV).stdout.strip()
    assert master_head != sha


def test_release_followup_stages_shape() -> None:
    assert [s.kind for s in orbit.release_followup_stages("job1")] == ["report", "close"]
    assert orbit.release_followup_stages("job1")[0].payload == {"summary": "gongbang: job1"}


def test_release_not_configured_project_reaches_done_without_needs_owner(
    workspace: Workspace,
) -> None:
    """Sanity check the interception is conditional: a project with no
    `release` set (the "alpha" fixture itself) must commit straight
    through to report/close, never stopping at "needs_owner"."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "no-release",
                          "full", ())
    (workspace.alpha / "src" / "app.py").write_text("print('v2')\n", encoding="utf-8")
    fp = evidence.fingerprint(workspace.alpha, workspace.config.projects["alpha"].excludes)
    evidence.record(workspace.ledger, "alpha", attempt_id, "review", fp,
                    payload={"passed": True})
    evidence.record(workspace.ledger, "alpha", attempt_id, "bench", fp,
                    payload={"passed": True})

    outcome = orbit.continue_job(
        workspace.config, workspace.ledger, _fakes(), "alpha", attempt_id,
        [orbit.Stage("commit", {"message": "gongbang: no-release"}),
         orbit.Stage("report", {"summary": "gongbang: no-release"}),
         orbit.Stage("close", {})],
    )

    assert outcome.status == "done"
    assert outcome.completed == ("commit", "report", "close")
    assert workspace.ledger.project("alpha").state == "CLOSED"


def test_on_progress_called_once_per_finished_stage_in_order(workspace: Workspace) -> None:
    """P2-2 support: continue_job's on_progress callback fires once per
    finished stage, in order -- the mechanism gb2 run uses for
    incremental progress reporting instead of driving the stage list
    one growing prefix at a time (which would defeat
    parallel_review_bench's overlap, since each such call starts a
    brand-new Processor from scratch, so a review stage that already
    finished to "done" in an earlier call can never be adjacent to
    bench in a LATER call's stage list for _should_run_parallel to
    interleave them)."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "progress",
                          "full", ())
    (workspace.alpha / "src" / "app.py").write_text("print('v2')\n", encoding="utf-8")
    fp = evidence.fingerprint(workspace.alpha, workspace.config.projects["alpha"].excludes)
    evidence.record(workspace.ledger, "alpha", attempt_id, "review", fp,
                    payload={"passed": True})
    evidence.record(workspace.ledger, "alpha", attempt_id, "bench", fp,
                    payload={"passed": True})
    seen: list[str] = []

    outcome = orbit.continue_job(
        workspace.config, workspace.ledger, _fakes(), "alpha", attempt_id,
        [orbit.Stage("commit", {"message": "gongbang: progress"}),
         orbit.Stage("report", {"summary": "gongbang: progress"}),
         orbit.Stage("close", {})],
        on_progress=seen.append,
    )

    assert outcome.status == "done"
    assert seen == ["commit", "report", "close"]


def test_on_progress_fires_for_review_in_the_parallel_review_bench_path(
    workspace: Workspace,
) -> None:
    """on_progress must also fire from inside _run_review_then_bench
    (config.parallel_review_bench's speed path), not just the plain
    per-stage loop -- "review" only here, since this reuses the
    existing overlap test's setup where bench deliberately fails
    (no_bench_cases) and so never reaches "done" itself."""
    events: list[tuple[str, float]] = []
    seen: list[str] = []
    project_config = replace(workspace.config.projects["alpha"], test_cmd=("true",))
    config = replace(workspace.config,
                     projects={**workspace.config.projects, "alpha": project_config})
    attempt_id = open_job(config, workspace.ledger, "alpha", "progress-parallel",
                          "full", ("reviewer",))

    def factory(cfg, ledger, fakes) -> Processor:
        proc = Processor(cfg, ledger, fakes)
        proc.register_defaults()
        proc.register("review", _review_steps_parallel(events))
        return proc

    outcome = orbit.continue_job(
        config, workspace.ledger, _fakes(), "alpha", attempt_id,
        [orbit.Stage("review", {"role": "reviewer"}), orbit.Stage("bench", {}),
         orbit.Stage("close", {})],
        processor_factory=factory, on_progress=seen.append,
    )

    assert outcome.status == "failed"
    assert outcome.reason == "no_bench_cases"
    assert seen == ["review"]


def test_run_job_does_not_create_a_reviewer_work_copy(workspace: Workspace) -> None:
    """run_job's open_job call only requests a copy for the builder
    role -- the reviewer runs against its own snapshot
    (engine.steps.review), never a pre-made copy_root, so one must
    never be created for it."""
    from engine.steps.open_job import copy_root as _copy_root

    attempt_id = orbit.run_job(
        workspace.config, workspace.ledger, _fakes(), "alpha", "job-no-reviewer-copy",
        "fix the wording", "builder", "reviewer",
    ).attempt_id

    assert _copy_root(workspace.config, attempt_id, "builder").exists()
    assert not _copy_root(workspace.config, attempt_id, "reviewer").exists()


def test_job_metrics_recorded_at_report_with_durations_and_bench_counts(
    workspace: Workspace,
) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "metrics-job",
                          "full", ())
    (workspace.alpha / "src" / "app.py").write_text("print('v2')\n", encoding="utf-8")
    fp = evidence.fingerprint(workspace.alpha, workspace.config.projects["alpha"].excludes)
    evidence.record(workspace.ledger, "alpha", attempt_id, "review", fp,
                    payload={"passed": True})
    evidence.record(workspace.ledger, "alpha", attempt_id, "bench", fp,
                    payload={"passed": True, "scope": "related"})

    outcome = orbit.continue_job(
        workspace.config, workspace.ledger, _fakes(), "alpha", attempt_id,
        [orbit.Stage("commit", {"message": "gongbang: metrics"}),
         orbit.Stage("report", {"summary": "gongbang: metrics"}),
         orbit.Stage("close", {})],
    )

    assert outcome.status == "done"
    events = [e for _seq, e in workspace.ledger.events("alpha", "job.metrics")
              if e.attempt_id == attempt_id]
    assert len(events) == 1
    payload = events[0].payload
    assert "commit" in payload["durations_ms"]
    assert "report" in payload["durations_ms"]
    assert all(isinstance(v, (int, float)) and v >= 0 for v in payload["durations_ms"].values())
    assert payload["test_runs"] == {"related": 1}
    # merge, fingerprint, commit and close all run git through gitutil
    assert isinstance(payload["git_calls"], int) and payload["git_calls"] > 0


def test_release_job_keeps_main_stage_durations_across_the_ship_stop(
    workspace: Workspace, tmp_path: Path,
) -> None:
    """A release-configured job's commit stage
    stops at needs_owner BEFORE report ever runs, so the fresh
    continue_job(release_followup_stages(...)) call gb2 ship/skip-ship
    makes afterward must not start its own durations_ms from empty --
    the final job.metrics event must still carry the earlier stages'
    (here, "commit") timing, not just report/close's."""
    config, _master_repo = _release_project(workspace, tmp_path)
    attempt_id = open_job(config, workspace.ledger, "alpha", "ship-metrics", "full", ())
    (workspace.alpha / "src" / "app.py").write_text("print('v2')\n", encoding="utf-8")
    fp = evidence.fingerprint(workspace.alpha, config.projects["alpha"].excludes)
    evidence.record(workspace.ledger, "alpha", attempt_id, "review", fp,
                    payload={"passed": True})
    evidence.record(workspace.ledger, "alpha", attempt_id, "bench", fp,
                    payload={"passed": True})

    stop_outcome = orbit.continue_job(
        config, workspace.ledger, _fakes(), "alpha", attempt_id,
        [orbit.Stage("commit", {"message": "gongbang: ship-metrics"})],
    )
    assert stop_outcome.status == "needs_owner"

    partial_events = [e for _seq, e in workspace.ledger.events("alpha", "job.metrics.partial")
                      if e.attempt_id == attempt_id]
    assert len(partial_events) == 1
    assert "commit" in partial_events[0].payload["durations_ms"]

    followup = orbit.release_followup_stages("ship-metrics")
    final_outcome = orbit.continue_job(
        config, workspace.ledger, _fakes(), "alpha", attempt_id, followup,
    )
    assert final_outcome.status == "done"

    metrics_events = [e for _seq, e in workspace.ledger.events("alpha", "job.metrics")
                      if e.attempt_id == attempt_id]
    assert len(metrics_events) == 1
    durations = metrics_events[0].payload["durations_ms"]
    assert "commit" in durations  # carried over from the stop, not lost
    assert "report" in durations  # this call's own stage (metrics are
    # recorded exactly when "report" completes, before "close" runs --
    # same as the non-release case, unaffected by this fix)
