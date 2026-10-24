"""Unit tests for engine.fixup: continuing a job's failed-at-review-or-
bench attempt instead of starting over.

Like tests/unit/test_orbit.py, run_station and review are stubbed via
Processor.register() rather than depending on the real worker/CLI
steps -- closely enough to the real contract (a station.finished event,
a review evidence row carrying {"passed": bool, "findings": [...]})
to exercise engine.fixup's own logic. merge/bench/commit/report/close
are the real engine.steps.* implementations, against a real sample app
repo (tests/fixtures/sample_app, variant 2 -- the reproducible
"initials" bug), so bench genuinely fails until the bug is actually
fixed."""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

from engine import evidence, fixup, orbit
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.ledger import Event
from engine.processor import Fakes, Processor, StepContext, StepDef, StepFailed
from engine.processor import submit as submit_request
from engine.steps.open_job import copy_root, open_job
from tests.conftest import Workspace
from tests.fixtures.sample_app import make_sample

_INIT_PY_FIXED = '''\
def greet(name: str) -> str:
    return f"Hello, {name}"


def initials(full_name: str) -> str:
    words = full_name.split()
    return "".join(word[0] for word in words).upper()
'''


def _fakes() -> Fakes:
    return Fakes(publisher=FakePublisher(), installer=FakeInstaller())


def _sample_workspace(workspace: Workspace, variant: int) -> Workspace:
    """Repoint 'alpha' at a fresh sample app repo, same helper as
    tests/unit/test_orbit.py's own _sample_workspace -- duplicated here
    deliberately (ENGINE.md's own convention: a few lines of lookup/
    setup duplicated rather than depending on another test module's
    private helper)."""
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


# ---------------------------------------------------------------------
# A stub run_station that behaves two different ways depending on the
# task text it is handed -- a plain task writes an unrelated "earlier
# change" (docs/notes.md, never fixing the bug); a composed fix task
# (compose_fix_task always starts with "Continue this task") writes the
# actual bug fix. This lets ONE processor_factory drive both the
# original failing attempt and the fix() attempt in the same test,
# proving the fix attempt's builder copy keeps the first attempt's own
# change (it was overlaid in by fixup, not re-created by this stub)
# while adding the new one.
# ---------------------------------------------------------------------

def _run_station_build(ctx: StepContext) -> dict[str, object]:
    role = str(ctx.request.payload.get("role"))
    copy_path = copy_root(ctx.config, ctx.attempt_id, role)
    task = str(ctx.request.payload.get("task", ""))
    if "Continue this task" in task:
        target = copy_path / "src" / "greeter" / "__init__.py"
    else:
        target = copy_path / "docs" / "notes.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        _INIT_PY_FIXED if "Continue this task" in task else "investigating the bug\n",
        encoding="utf-8",
    )
    ctx.ledger.append(Event(
        project=ctx.project, kind="station.finished", actor_kind="processor",
        actor_id="processor", attempt_id=ctx.attempt_id,
        payload={
            "request_id": ctx.request.request_id,
            "station": ctx.request.payload.get("station"), "role": role,
            "ok": True, "summary": "stub", "cost_usd": 0.0, "turns": 1,
            "changed_paths": [], "requests": [],
        },
    ))
    return {}


def _reconcile_station(ctx: StepContext) -> bool:
    for _seq, event in ctx.ledger.events(ctx.project, "station.finished"):
        if event.payload.get("request_id") == ctx.request.request_id:
            return True
    return False


def _run_station_steps(ctx: StepContext) -> list[StepDef]:
    return [StepDef("record_station", _run_station_build, _reconcile_station)]


def _review_steps(passed: bool, findings: list[object] | None = None):
    def _run(ctx: StepContext) -> dict[str, object]:
        project_config = ctx.config.projects[ctx.project]
        fp = evidence.fingerprint(project_config.repo, project_config.excludes)
        evidence.record(
            ctx.ledger, ctx.project, ctx.attempt_id, "review", fp,
            payload={"passed": passed, "request_id": ctx.request.request_id,
                     "findings": findings or []},
        )
        if not passed:
            # Mirrors engine.steps.review.record_review: a failed review
            # must stay visible as a failed REQUEST (status 'failed',
            # reason 'review_not_passed'), not silently let the job move
            # on to bench.
            raise StepFailed("review_not_passed")
        return {}

    def _reconcile(ctx: StepContext) -> bool:
        return any(
            row.payload.get("request_id") == ctx.request.request_id
            for row in ctx.ledger.evidence(ctx.attempt_id, "review")
        )

    return lambda ctx: [StepDef("record_review", _run, _reconcile)]


def _processor_factory(review_passed: bool = True, review_findings=None):
    def factory(config, ledger, fakes) -> Processor:
        proc = Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", _run_station_steps)
        proc.register("review", _review_steps(review_passed, review_findings))
        return proc
    return factory


# ---------------------------------------------------------------------
# refusal: nothing to fix
# ---------------------------------------------------------------------

def test_find_fix_target_refuses_when_project_is_closed(workspace: Workspace) -> None:
    assert workspace.ledger.project("alpha").state == "CLOSED"
    with pytest.raises(fixup.FixupError, match="no_failed_attempt"):
        fixup.find_fix_target(workspace.ledger, "alpha")


def test_fix_refuses_when_there_is_no_failed_attempt(workspace: Workspace) -> None:
    def fakes_for(attempt_id: str) -> Fakes:
        return _fakes()

    with pytest.raises(fixup.FixupError):
        fixup.fix(workspace.config, workspace.ledger, "alpha", "", fakes_for)
    # Nothing changed: still CLOSED, no attempt was opened.
    assert workspace.ledger.project("alpha").state == "CLOSED"


def test_find_fix_target_refuses_when_merge_never_completed(workspace: Workspace) -> None:
    """bench fails deterministically (no bench_cases configured) before
    run_station/merge ever happen -- find_fix_target must refuse rather
    than treat this failed-but-never-merged attempt as fixable (there
    is nothing a fix attempt's builder copy could usefully be seeded
    with)."""
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

    with pytest.raises(fixup.FixupError, match="merge never completed"):
        fixup.find_fix_target(workspace.ledger, "alpha")


# ---------------------------------------------------------------------
# review findings flow into the composed task
# ---------------------------------------------------------------------

def test_find_fix_target_and_compose_task_carry_review_findings(
    workspace: Workspace,
) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "needs-review-fix",
                          "full", ("builder", "reviewer"))
    findings = [{"severity": "P1", "summary": "missing null check"}]
    outcome = orbit.continue_job(
        workspace.config, workspace.ledger, _fakes(), "alpha", attempt_id,
        [orbit.Stage("run_station",
                     {"station": "build", "role": "builder", "task": "add the feature"}),
         orbit.Stage("merge", {"copies": [
             {"role": "builder", "station": "build",
              "path": str(copy_root(workspace.config, attempt_id, "builder"))},
         ]}),
         orbit.Stage("review", {"role": "reviewer"})],
        processor_factory=_processor_factory(review_passed=False, review_findings=findings),
    )
    assert outcome.status == "failed"
    assert outcome.completed == ("run_station", "merge")

    target = fixup.find_fix_target(workspace.ledger, "alpha")
    assert target.failing_kind == "review"
    assert target.original_task == "add the feature"
    assert target.review_findings == findings
    assert target.bench_tail is None  # bench never even ran

    task = fixup.compose_fix_task(target, "please re-check auth too")
    assert "Continue this task. Original request: add the feature" in task
    assert "Review findings to address:" in task
    assert "missing null check" in task
    assert "Failing test output:" not in task
    assert "Extra: please re-check auth too" in task


def test_find_fix_target_uses_the_selected_failing_bench_not_the_oldest_one(
    workspace: Workspace,
) -> None:
    """Two bench requests exist for the same attempt -- an OLDER one
    that already failed for a different reason, and the request that
    actually stopped the job this time. bench_tail must come from the
    SELECTED (currently-failing) request's own run_tests journal,
    never just "the first bench request found for this attempt"
    (which could be a stale, already-superseded failure)."""
    ledger = workspace.ledger
    attempt_id = open_job(workspace.config, ledger, "alpha", "retry-bench",
                          "full", ("builder", "reviewer"))
    submit_request(ledger, "alpha", attempt_id, "run_station",
                   {"station": "build", "role": "builder", "task": "fix the bug"}, "owner")
    merge_rid = submit_request(ledger, "alpha", attempt_id, "merge", {"copies": []}, "owner")
    ledger.append(Event(
        project="alpha", kind="request.status", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"request_id": merge_rid, "status": "done", "result": {}},
    ))

    old_bench_rid = submit_request(ledger, "alpha", attempt_id, "bench", {"round": 1}, "owner")
    ledger.append(Event(
        project="alpha", kind="request.step", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"request_id": old_bench_rid, "step_name": "run_tests", "status": "done",
                 "payload": {"tail": "OLD STALE FAILURE\n"}},
    ))
    ledger.append(Event(
        project="alpha", kind="request.status", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"request_id": old_bench_rid, "status": "failed",
                 "result": {"reason": "tests_failed"}},
    ))

    new_bench_rid = submit_request(ledger, "alpha", attempt_id, "bench", {"round": 2}, "owner")
    ledger.append(Event(
        project="alpha", kind="request.step", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"request_id": new_bench_rid, "step_name": "run_tests", "status": "done",
                 "payload": {"tail": "CURRENT REAL FAILURE\n"}},
    ))
    ledger.append(Event(
        project="alpha", kind="request.status", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"request_id": new_bench_rid, "status": "failed",
                 "result": {"reason": "tests_failed"}},
    ))

    target = fixup.find_fix_target(ledger, "alpha")
    assert target.failing_kind == "bench"
    assert target.bench_tail == "CURRENT REAL FAILURE\n"


def test_find_fix_target_bench_tail_is_none_when_failing_is_review(
    workspace: Workspace,
) -> None:
    """A bench request exists (and even failed earlier), but the
    CURRENTLY failing request is review -- bench_tail must stay None,
    never fall back to that unrelated bench request's own tail."""
    ledger = workspace.ledger
    attempt_id = open_job(workspace.config, ledger, "alpha", "review-after-bench",
                          "full", ("builder", "reviewer"))
    submit_request(ledger, "alpha", attempt_id, "run_station",
                   {"station": "build", "role": "builder", "task": "fix the bug"}, "owner")
    merge_rid = submit_request(ledger, "alpha", attempt_id, "merge", {"copies": []}, "owner")
    ledger.append(Event(
        project="alpha", kind="request.status", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"request_id": merge_rid, "status": "done", "result": {}},
    ))
    bench_rid = submit_request(ledger, "alpha", attempt_id, "bench", {"round": 1}, "owner")
    ledger.append(Event(
        project="alpha", kind="request.step", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"request_id": bench_rid, "step_name": "run_tests", "status": "done",
                 "payload": {"tail": "UNRELATED OLD BENCH FAILURE\n"}},
    ))
    ledger.append(Event(
        project="alpha", kind="request.status", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"request_id": bench_rid, "status": "failed",
                 "result": {"reason": "tests_failed"}},
    ))
    review_rid = submit_request(ledger, "alpha", attempt_id, "review", {"role": "reviewer"},
                                "owner")
    ledger.append(Event(
        project="alpha", kind="request.status", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"request_id": review_rid, "status": "failed",
                 "result": {"reason": "review_not_passed"}},
    ))

    target = fixup.find_fix_target(ledger, "alpha")
    assert target.failing_kind == "review"
    assert target.bench_tail is None


def test_fix_opens_a_copy_only_for_the_builder_role(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reviewer runs against its own exported snapshot, never a
    worker copy -- gb2 fix must not pay to check one out for it."""
    ws = _sample_workspace(workspace, variant=2)
    attempt1 = open_job(ws.config, ws.ledger, "alpha", "fix-initials",
                        "full", ("builder", "reviewer"))
    stages1 = orbit.default_stages(
        ws.config, attempt1, "builder", "reviewer",
        "investigate the initials bug", "fix-initials",
    )
    orbit.continue_job(
        ws.config, ws.ledger, _fakes(), "alpha", attempt1, stages1,
        processor_factory=_processor_factory(review_passed=True),
    )

    seen_roles: list[tuple[str, ...]] = []
    from engine.steps import open_job as open_job_module
    real_open_job = open_job_module.open_job

    def _spy(cfg, ledger_, project, job_name, kind, roles, *args, **kwargs):
        seen_roles.append(roles)
        return real_open_job(cfg, ledger_, project, job_name, kind, roles, *args, **kwargs)

    monkeypatch.setattr(open_job_module, "open_job", _spy)
    import engine.fixup as fixup_module
    monkeypatch.setattr(fixup_module, "open_job", _spy)

    def fakes_for(attempt_id: str) -> Fakes:
        return _fakes()

    fixup.fix(ws.config, ws.ledger, "alpha", "", fakes_for,
             processor_factory=_processor_factory(review_passed=True))

    assert len(seen_roles) == 1
    assert seen_roles[0] == ("builder",)


# ---------------------------------------------------------------------
# the main scenario: failed bench -> fix -> done, earlier change kept
# ---------------------------------------------------------------------

def test_fix_continues_a_failed_bench_keeping_the_earlier_change_and_adding_the_new_one(
    workspace: Workspace,
) -> None:
    ws = _sample_workspace(workspace, variant=2)

    attempt1 = open_job(ws.config, ws.ledger, "alpha", "fix-initials",
                        "full", ("builder", "reviewer"))
    stages1 = orbit.default_stages(
        ws.config, attempt1, "builder", "reviewer",
        "investigate the initials bug", "fix-initials",
    )
    outcome1 = orbit.continue_job(
        ws.config, ws.ledger, _fakes(), "alpha", attempt1, stages1,
        processor_factory=_processor_factory(review_passed=True),
    )

    assert outcome1.status == "failed"
    assert outcome1.completed == ("run_station", "merge", "review")
    assert ws.ledger.project("alpha").state == "OPEN"
    notes = ws.alpha / "docs" / "notes.md"
    assert notes.exists()
    assert notes.read_text(encoding="utf-8") == "investigating the bug\n"
    # the bug itself is still unfixed in the repo at this point.
    assert "words[:1]" in (ws.alpha / "src" / "greeter" / "__init__.py").read_text(
        encoding="utf-8")

    target = fixup.find_fix_target(ws.ledger, "alpha")
    assert target.attempt_id == attempt1
    assert target.failing_kind == "bench"
    assert target.original_task == "investigate the initials bug"
    assert target.bench_tail is not None
    assert "test_initials" in target.bench_tail

    def fakes_for(attempt_id: str) -> Fakes:
        return _fakes()

    seen_stages: list[str] = []
    result = fixup.fix(
        ws.config, ws.ledger, "alpha", "please finish the fix", fakes_for,
        on_progress=seen_stages.append,
        processor_factory=_processor_factory(review_passed=True),
    )

    assert result.old_attempt_id == attempt1
    assert result.new_attempt_id != attempt1
    assert "Continue this task. Original request: investigate the initials bug" in result.task
    assert "Failing test output:" in result.task
    assert "test_initials" in result.task
    assert "Extra: please finish the fix" in result.task

    assert result.outcome.status == "done"
    assert seen_stages == ["run_station", "merge", "review", "bench", "commit", "report", "close"]
    assert ws.ledger.project("alpha").state == "CLOSED"

    # the earlier attempt's own change is still there...
    assert notes.read_text(encoding="utf-8") == "investigating the bug\n"
    # ...and the new change (the actual fix) landed alongside it.
    fixed_src = (ws.alpha / "src" / "greeter" / "__init__.py").read_text(encoding="utf-8")
    assert "words[:1]" not in fixed_src
    assert fixed_src == _INIT_PY_FIXED

    bench_rows = ws.ledger.evidence(result.new_attempt_id, "bench")
    assert bench_rows[-1].payload["passed"] is True


# ---------------------------------------------------------------------
# fix after a failed fix: the new attempt itself crashes before its own
# merge ever completes, but an earlier attempt's archive still holds
# real merged work.
# ---------------------------------------------------------------------

def _crashing_run_station_processor_factory():
    """A processor_factory whose run_station always fails outright
    (StepFailed), simulating a builder crash -- never reaching merge."""
    def _run(ctx: StepContext) -> dict[str, object]:
        raise StepFailed("builder_crashed")

    def _reconcile(ctx: StepContext) -> bool:
        return False

    def factory(config, ledger, fakes) -> Processor:
        proc = Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", lambda ctx: [StepDef("record_station", _run, _reconcile)])
        return proc
    return factory


def test_fix_after_a_failed_fix_continues_from_the_latest_archive(
    workspace: Workspace,
) -> None:
    """attempt1 fails at review (merge already landed docs/notes.md).
    fix() #1 opens attempt2, seeded from attempt1's archive -- but
    attempt2's OWN builder crashes before merge ever runs again, so
    attempt2's own close archives nothing. fix() #2 must still find
    attempt1's archive (carried forward through attempt2, which never
    re-merged anything) and continue from it, never losing the first
    change, keeping the owner's own pre-existing dirty file untouched,
    and adding the real fix on top."""
    ws = _sample_workspace(workspace, variant=2)
    owner_dirty = ws.alpha / "OWNER_DIRTY.txt"
    owner_dirty.write_text("owner's own in-progress edit\n", encoding="utf-8")

    attempt1 = open_job(ws.config, ws.ledger, "alpha", "fix-initials",
                        "full", ("builder", "reviewer"))
    stages1 = orbit.default_stages(
        ws.config, attempt1, "builder", "reviewer",
        "investigate the initials bug", "fix-initials",
    )
    findings = [{"severity": "P1", "summary": "missing null check"}]
    outcome1 = orbit.continue_job(
        ws.config, ws.ledger, _fakes(), "alpha", attempt1, stages1,
        processor_factory=_processor_factory(review_passed=False, review_findings=findings),
    )
    assert outcome1.status == "failed"
    assert outcome1.completed == ("run_station", "merge")
    notes = ws.alpha / "docs" / "notes.md"
    assert notes.read_text(encoding="utf-8") == "investigating the bug\n"

    def fakes_for(attempt_id: str) -> Fakes:
        return _fakes()

    # fix #1: the new attempt's own builder crashes before merge.
    result1 = fixup.fix(
        ws.config, ws.ledger, "alpha", "", fakes_for,
        processor_factory=_crashing_run_station_processor_factory(),
    )
    assert result1.old_attempt_id == attempt1
    attempt2 = result1.new_attempt_id
    assert result1.outcome.status == "failed"
    assert result1.outcome.completed == ()
    assert ws.ledger.project("alpha").state == "OPEN"
    assert ws.ledger.attempt(attempt2).status == "active"
    # attempt1's close already archived+reset the real repo (notes.md is
    # not expected to be there again until a later merge lands it) --
    # but its own change did survive, overlaid into attempt2's builder
    # copy, never discarded.
    assert (copy_root(ws.config, attempt2, "builder") / "docs" / "notes.md").read_text(
        encoding="utf-8") == "investigating the bug\n"
    assert owner_dirty.read_text(encoding="utf-8") == "owner's own in-progress edit\n"

    # find_fix_target must now accept attempt2 (failed at run_station,
    # its own merge never completed) by walking back to attempt1's
    # still-real archive, and must carry attempt1's review findings
    # forward since attempt2 never got far enough to produce its own.
    target2 = fixup.find_fix_target(ws.ledger, "alpha")
    assert target2.attempt_id == attempt2
    assert target2.failing_kind == "run_station"
    assert target2.carried_archive is not None
    assert target2.review_findings == findings
    assert "investigate the initials bug" in target2.original_task

    # fix #2: a normal, successful fix.
    result2 = fixup.fix(
        ws.config, ws.ledger, "alpha", "second extra note", fakes_for,
        processor_factory=_processor_factory(review_passed=True),
    )
    assert result2.old_attempt_id == attempt2
    assert result2.outcome.status == "done"
    assert "second extra note" in result2.task
    assert "missing null check" in result2.task
    assert ws.ledger.project("alpha").state == "CLOSED"

    # the first change (attempt1's) is still there...
    assert notes.read_text(encoding="utf-8") == "investigating the bug\n"
    # ...the real fix landed on top of it...
    fixed_src = (ws.alpha / "src" / "greeter" / "__init__.py").read_text(encoding="utf-8")
    assert fixed_src == _INIT_PY_FIXED
    # ...and the owner's own pre-existing dirty file was never touched.
    assert owner_dirty.read_text(encoding="utf-8") == "owner's own in-progress edit\n"


def test_fix_cancels_any_pending_publish_for_the_attempt_it_replaces(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """engine.release.cancel_pending_publish is expected to land in a
    sibling change with this exact signature -- fixup.fix() must call it
    (guarded via getattr, since this checkout may not have it yet) for
    the OLD attempt BEFORE close_failed_attempt's own processor.run_once
    could otherwise dispatch a stale queued publish request as a side
    effect of just closing that attempt."""
    import engine.release as release_module

    ws = _sample_workspace(workspace, variant=2)
    attempt1 = open_job(ws.config, ws.ledger, "alpha", "fix-initials",
                        "full", ("builder", "reviewer"))
    stages1 = orbit.default_stages(
        ws.config, attempt1, "builder", "reviewer",
        "investigate the initials bug", "fix-initials",
    )
    orbit.continue_job(
        ws.config, ws.ledger, _fakes(), "alpha", attempt1, stages1,
        processor_factory=_processor_factory(review_passed=True),
    )

    calls: list[tuple[object, str, str, str]] = []

    def _spy(ledger_, project, attempt_id, reason):
        calls.append((ledger_, project, attempt_id, reason))
        return []

    monkeypatch.setattr(release_module, "cancel_pending_publish", _spy, raising=False)

    def fakes_for(attempt_id: str) -> Fakes:
        return _fakes()

    fixup.fix(ws.config, ws.ledger, "alpha", "", fakes_for,
             processor_factory=_processor_factory(review_passed=True))

    assert calls == [(ws.ledger, "alpha", attempt1, "superseded_by_fix")]


# ---------------------------------------------------------------------
# resuming an interrupted fix() transition itself (journal)
# ---------------------------------------------------------------------

def test_fix_resumes_after_a_crash_right_after_closing_the_old_attempt(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash inside open_job (simulated here) happens AFTER
    close_failed_attempt has already closed+archived the old attempt --
    the project is left CLOSED with nothing find_fix_target could ever
    accept (it requires state OPEN). fix() called again must resume
    from the journal instead of raising "not OPEN", open exactly ONE new
    attempt (never a second, orphaned one), and finish normally --
    never losing the first change or the owner's own pre-existing dirty
    file."""
    ws = _sample_workspace(workspace, variant=2)
    owner_dirty = ws.alpha / "OWNER_DIRTY.txt"
    owner_dirty.write_text("owner's own in-progress edit\n", encoding="utf-8")

    attempt1 = open_job(ws.config, ws.ledger, "alpha", "fix-initials",
                        "full", ("builder", "reviewer"))
    stages1 = orbit.default_stages(
        ws.config, attempt1, "builder", "reviewer",
        "investigate the initials bug", "fix-initials",
    )
    outcome1 = orbit.continue_job(
        ws.config, ws.ledger, _fakes(), "alpha", attempt1, stages1,
        processor_factory=_processor_factory(review_passed=True),
    )
    assert outcome1.status == "failed"
    assert outcome1.completed == ("run_station", "merge", "review")

    def fakes_for(attempt_id: str) -> Fakes:
        return _fakes()

    calls = {"n": 0}
    real_open_job = fixup.open_job

    def _crashing_open_job(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash in open_job")
        return real_open_job(*args, **kwargs)

    monkeypatch.setattr(fixup, "open_job", _crashing_open_job)

    with pytest.raises(RuntimeError, match="simulated crash"):
        fixup.fix(ws.config, ws.ledger, "alpha", "please finish the fix", fakes_for,
                 processor_factory=_processor_factory(review_passed=True))

    # exactly the broken state the external review described: CLOSED,
    # old attempt already closed+archived, no new attempt yet.
    assert ws.ledger.project("alpha").state == "CLOSED"
    assert len(ws.ledger.events("alpha", "fix.started")) == 1
    assert len(ws.ledger.events("alpha", "fix.closed_old")) == 1
    assert not ws.ledger.events("alpha", "fix.opened_new")
    assert not ws.ledger.events("alpha", "fix.finished")
    with pytest.raises(fixup.FixupError, match="not OPEN"):
        fixup.find_fix_target(ws.ledger, "alpha")

    result = fixup.fix(ws.config, ws.ledger, "alpha", "please finish the fix", fakes_for,
                       processor_factory=_processor_factory(review_passed=True))

    assert calls["n"] == 2  # open_job itself was retried, never silently skipped
    assert result.old_attempt_id == attempt1
    assert result.outcome.status == "done"
    assert ws.ledger.project("alpha").state == "CLOSED"
    # exactly ONE new attempt was ever opened for this fix -- the crash
    # must never have forked a second, orphaned one.
    assert len(ws.ledger.events("alpha", "fix.opened_new")) == 1
    assert len(ws.ledger.events("alpha", "fix.closed_old")) == 1

    notes = ws.alpha / "docs" / "notes.md"
    assert notes.read_text(encoding="utf-8") == "investigating the bug\n"
    fixed_src = (ws.alpha / "src" / "greeter" / "__init__.py").read_text(encoding="utf-8")
    assert fixed_src == _INIT_PY_FIXED
    assert owner_dirty.read_text(encoding="utf-8") == "owner's own in-progress edit\n"


def test_fix_resumes_after_a_crash_right_after_opening_the_new_attempt(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash in the archive overlay (simulated here) happens AFTER
    open_job has already created the new attempt -- the project is left
    OPEN with a brand-new attempt that has no failed request at all
    (find_fix_target would refuse it outright, with nothing to find).
    fix() called again must resume from the journal, reuse that SAME
    new attempt rather than opening a second one, and finish normally."""
    ws = _sample_workspace(workspace, variant=2)
    owner_dirty = ws.alpha / "OWNER_DIRTY.txt"
    owner_dirty.write_text("owner's own in-progress edit\n", encoding="utf-8")

    attempt1 = open_job(ws.config, ws.ledger, "alpha", "fix-initials",
                        "full", ("builder", "reviewer"))
    stages1 = orbit.default_stages(
        ws.config, attempt1, "builder", "reviewer",
        "investigate the initials bug", "fix-initials",
    )
    outcome1 = orbit.continue_job(
        ws.config, ws.ledger, _fakes(), "alpha", attempt1, stages1,
        processor_factory=_processor_factory(review_passed=True),
    )
    assert outcome1.status == "failed"

    def fakes_for(attempt_id: str) -> Fakes:
        return _fakes()

    calls = {"n": 0}
    real_overlay = fixup._overlay_archive_into_builder_copy

    def _crashing_overlay(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash in overlay")
        return real_overlay(*args, **kwargs)

    monkeypatch.setattr(fixup, "_overlay_archive_into_builder_copy", _crashing_overlay)

    with pytest.raises(RuntimeError, match="simulated crash"):
        fixup.fix(ws.config, ws.ledger, "alpha", "please finish the fix", fakes_for,
                 processor_factory=_processor_factory(review_passed=True))

    assert ws.ledger.project("alpha").state == "OPEN"
    opened_events = ws.ledger.events("alpha", "fix.opened_new")
    assert len(opened_events) == 1
    assert not ws.ledger.events("alpha", "fix.restored")
    assert not ws.ledger.events("alpha", "fix.finished")
    attempt2 = str(opened_events[0][1].payload["new_attempt_id"])
    assert ws.ledger.attempt(attempt2).status == "active"
    # the project's own current attempt has no failed (or any) request
    # at all yet -- find_fix_target has nothing to accept here.
    with pytest.raises(fixup.FixupError):
        fixup.find_fix_target(ws.ledger, "alpha")

    result = fixup.fix(ws.config, ws.ledger, "alpha", "please finish the fix", fakes_for,
                       processor_factory=_processor_factory(review_passed=True))

    assert calls["n"] == 2
    assert result.new_attempt_id == attempt2  # no duplicate attempt opened
    assert len(ws.ledger.events("alpha", "fix.opened_new")) == 1
    assert result.outcome.status == "done"
    assert ws.ledger.project("alpha").state == "CLOSED"

    notes = ws.alpha / "docs" / "notes.md"
    assert notes.read_text(encoding="utf-8") == "investigating the bug\n"
    fixed_src = (ws.alpha / "src" / "greeter" / "__init__.py").read_text(encoding="utf-8")
    assert fixed_src == _INIT_PY_FIXED
    assert owner_dirty.read_text(encoding="utf-8") == "owner's own in-progress edit\n"


def test_original_task_returns_the_run_station_task_text() -> None:
    """The after-install resume path rebuilds its stage list without the
    caller holding the task; original_task must recover it so the
    reviewer is not handed an empty request."""
    from types import SimpleNamespace

    from engine import fixup

    rows = [SimpleNamespace(kind="merge", payload={}),
            SimpleNamespace(kind="run_station", payload={"task": "fix the header"})]
    original = fixup._requests_for_attempt
    fixup._requests_for_attempt = lambda ledger, project, attempt_id: rows
    try:
        assert fixup.original_task(None, "p", "a#1") == "fix the header"
        rows.pop()
        assert fixup.original_task(None, "p", "a#1") == ""
    finally:
        fixup._requests_for_attempt = original
