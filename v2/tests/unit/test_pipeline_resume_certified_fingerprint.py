"""Unit tests for the certified_fingerprint pair, the remaining half of
a review finding (4) s65-evfp could not reach (pipeline_resume.py was owned by
a parallel split job at the time): engine.application.pipeline_tasks.
finish_after_handoff RECORDS it (the repo's own fingerprint at the
moment a security-fix stop happens), and engine.application.
pipeline_resume.resume_security_fix LOOKS it up on resume, to tell a
plain security retry apart from one where the source changed since
(which must rerun acceptance/handoff fresh first).

Both sides now go through the SAME file-dep-aware evidence fingerprint
(engine.infrastructure.evidence.with_file_deps), so:
  - a project with no file_dep_targets gets exactly the old plain tree
    fingerprint on both sides -- no behavior change at all.
  - a project WITH file_dep_targets must detect a shared package's own
    content changing since the certificate was recorded as "the source
    changed", even though the app's own repo tree never moved -- the
    exact gap commit/review/bench evidence already closed."""
from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from engine.application.job_state import JobPosition
from engine.application.pipeline_base import Run
from engine.application.pipeline_tasks import finish_after_handoff
from engine.domain.plan_model import Plan
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.infrastructure import evidence
from engine.ledger import Ledger
from engine.processor import Fakes
from engine.steps.open_job import open_job
from tests.conftest import GIT_ENV, Workspace


def _with_file_dep_targets(ws: Workspace, targets: tuple[tuple[str, str], ...]) -> Workspace:
    project = replace(ws.config.projects["alpha"], file_dep_targets=targets)
    config = replace(ws.config, projects={**ws.config.projects, "alpha": project})
    return Workspace(ws.root, config, ws.ledger, ws.alpha, ws.beta)


def _plant_secret(repo: Path) -> None:
    """A tracked (staged is enough -- app_security._tracked_files uses
    `git ls-files`, never requiring a commit) file app_security.check's
    own _scan_secrets deterministically flags, so check().passed is
    reliably False without needing npm/pip network access."""
    (repo / "secret.txt").write_text('password = "abcdef123456789"\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                   capture_output=True, env=GIT_ENV)


_EMPTY_PLAN = Plan(goal="", ui=False, scenes=(), tasks=(), acceptance=())


def _run_for(ws: Workspace, job_name: str) -> Run:
    return Run(config=ws.config, ledger=ws.ledger,
              fakes=Fakes(FakePublisher(), FakeInstaller()),
              project="alpha", job=job_name, on_progress=None, processor_factory=None)


def test_finish_after_handoff_proceeds_past_a_failed_security_check_with_no_file_dep_targets(
    workspace: Workspace,
) -> None:
    """Policy (no approval stops; a failed security check is
    recorded and the job keeps going): finish_after_handoff no
    longer stops needs_owner on a failed security check, so it no
    longer records a certified_fingerprint either (that value only
    ever existed to let a LATER resume of that stop detect a moved
    HEAD -- resume_security_fix itself, and the certified_fingerprint
    comparison it does, are both unchanged and still covered directly
    by the two tests below this one; there is simply no forward-path
    stop left to record one from any more). It proceeds straight to
    "done" (no `release` configured here) and records
    "security.auto_passed" instead."""
    job_name = "certfp-plain"
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", job_name,
                          "full", roles=("builder",))
    _plant_secret(workspace.alpha)

    outcome = finish_after_handoff(workspace.config, workspace.ledger, "alpha", job_name,
                                   _EMPTY_PLAN, attempt_id, Path("report.md"), 2)

    assert outcome.status == "done", outcome
    auto_passed = [e for _seq, e in workspace.ledger.events("alpha", "security.auto_passed")]
    assert len(auto_passed) == 1
    assert auto_passed[0].payload["confirm_source"] == "auto"


def test_finish_after_handoff_proceeds_past_a_failed_security_check_with_file_dep_targets(
    workspace: Workspace, tmp_path: Path,
) -> None:
    """Same point as the sibling test above, for a project WITH
    file_dep_targets configured -- the file-dep-aware fingerprint logic
    this once exercised is still real code (evidence.with_file_deps),
    but nothing on the forward path calls it any more now that a
    failed security check never stops the job; only the SAME two
    legacy-resume tests below still exercise it directly."""
    kit = tmp_path / "_shared" / "kit"
    kit.mkdir(parents=True)
    (kit / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
    targets = (("../_shared/kit", str(kit)),)
    ws = _with_file_dep_targets(workspace, targets)

    job_name = "certfp-filedep"
    attempt_id = open_job(ws.config, ws.ledger, "alpha", job_name, "full", roles=("builder",))
    _plant_secret(ws.alpha)

    outcome = finish_after_handoff(ws.config, ws.ledger, "alpha", job_name,
                                   _EMPTY_PLAN, attempt_id, Path("report.md"), 2)

    assert outcome.status == "done", outcome
    auto_passed = [e for _seq, e in ws.ledger.events("alpha", "security.auto_passed")]
    assert len(auto_passed) == 1
    assert auto_passed[0].payload["confirm_source"] == "auto"


def test_resume_security_fix_reruns_acceptance_when_file_dep_target_content_changed(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """a review finding (4) reproduction, the lookup side: a certified_
    fingerprint recorded while the kit held content v1; the kit's
    content then changes to v2 even though alpha's own repo tree never
    moves at all. resume_security_fix must now detect that as "the
    source changed" (routing to resume_acceptance) -- before the fix,
    it compared only the plain tree fingerprint (unchanged here), so
    it would have silently rerun security alone against now-stale
    acceptance/handoff evidence."""
    kit = tmp_path / "_shared" / "kit2"
    kit.mkdir(parents=True)
    (kit / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
    targets = (("../_shared/kit2", str(kit)),)
    ws = _with_file_dep_targets(workspace, targets)

    job_name = "certfp-resume-stale"
    attempt_id = open_job(ws.config, ws.ledger, "alpha", job_name, "full", roles=("builder",))
    # plan.json written BEFORE fingerprinting: it must be part of the
    # repo's own tree both times the fingerprint is taken (once here,
    # once again inside resume_security_fix) -- writing it in between
    # would move the tree fingerprint for a reason that has nothing to
    # do with the kit at all, and falsely look like "the source
    # changed" regardless of file_dep_targets.
    plan_dir = ws.alpha / ".gongbang" / job_name
    plan_dir.mkdir(parents=True)
    (plan_dir / "plan.json").write_text(
        '{"goal":"g","ui":false,"scenes":[],"tasks":[{"id":"t1","title":"t","detail":"","owns":["x.py"],"depends_on":[],"covers":[]}],"acceptance":["a"]}\n', encoding="utf-8")

    plain_fp = evidence.fingerprint(ws.alpha, ws.config.projects["alpha"].excludes)
    certified = evidence.with_file_deps(plain_fp, targets)

    # The dependency's own content changes; alpha's repo itself does not.
    (kit / "index.js").write_text("module.exports = 2;\n", encoding="utf-8")

    import engine.application.pipeline_resume as pipeline_resume

    calls: list[str] = []
    monkeypatch.setattr(pipeline_resume, "resume_acceptance",
                        lambda run, pos: calls.append("resume_acceptance") or
                        pipeline_resume.FullOutcome("needs_owner", "stub", pos.attempt_id, None))
    monkeypatch.setattr(pipeline_resume, "finish_after_handoff",
                        lambda *a, **kw: calls.append("finish_after_handoff") or
                        pipeline_resume.FullOutcome("needs_owner", "stub", attempt_id, None))

    pos = JobPosition(job=job_name, stage="security_fix", attempt_id=attempt_id,
                      extra={"certified_fingerprint": certified, "report_path": "report.md",
                             "max_fix_rounds": 2})
    pipeline_resume.resume_security_fix(_run_for(ws, job_name), pos)

    assert calls == ["resume_acceptance"]


def test_resume_security_fix_reruns_security_only_when_file_dep_targets_unchanged(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control for the test above: nothing changed (neither the repo
    nor the kit) -- resume_security_fix must still take the cheap
    security-only path, never falsely rerun acceptance just because
    file_dep_targets is configured at all."""
    kit = tmp_path / "_shared" / "kit3"
    kit.mkdir(parents=True)
    (kit / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
    targets = (("../_shared/kit3", str(kit)),)
    ws = _with_file_dep_targets(workspace, targets)

    job_name = "certfp-resume-ok"
    attempt_id = open_job(ws.config, ws.ledger, "alpha", job_name, "full", roles=("builder",))
    # plan.json written BEFORE fingerprinting -- see the sibling
    # reproduction test's own comment for why the order matters here.
    plan_dir = ws.alpha / ".gongbang" / job_name
    plan_dir.mkdir(parents=True)
    (plan_dir / "plan.json").write_text(
        '{"goal":"g","ui":false,"scenes":[],"tasks":[{"id":"t1","title":"t","detail":"","owns":["x.py"],"depends_on":[],"covers":[]}],"acceptance":["a"]}\n', encoding="utf-8")

    plain_fp = evidence.fingerprint(ws.alpha, ws.config.projects["alpha"].excludes)
    certified = evidence.with_file_deps(plain_fp, targets)

    import engine.application.pipeline_resume as pipeline_resume

    calls: list[str] = []
    monkeypatch.setattr(pipeline_resume, "resume_acceptance",
                        lambda run, pos: calls.append("resume_acceptance") or
                        pipeline_resume.FullOutcome("needs_owner", "stub", pos.attempt_id, None))
    monkeypatch.setattr(pipeline_resume, "finish_after_handoff",
                        lambda *a, **kw: calls.append("finish_after_handoff") or
                        pipeline_resume.FullOutcome("needs_owner", "stub", attempt_id, None))

    pos = JobPosition(job=job_name, stage="security_fix", attempt_id=attempt_id,
                      extra={"certified_fingerprint": certified, "report_path": "report.md",
                             "max_fix_rounds": 2})
    pipeline_resume.resume_security_fix(_run_for(ws, job_name), pos)

    assert calls == ["finish_after_handoff"]
