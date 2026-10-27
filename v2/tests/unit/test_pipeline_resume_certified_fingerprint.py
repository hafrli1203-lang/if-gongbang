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


def _latest_certified_fingerprint(ledger: Ledger, project: str) -> str:
    rows = list(ledger.events(project, "job.position"))
    assert rows, "no job.position event recorded at all"
    _seq, latest = rows[-1]
    return str(latest.payload.get("extra", {}).get("certified_fingerprint", ""))


def _run_for(ws: Workspace, job_name: str) -> Run:
    return Run(config=ws.config, ledger=ws.ledger,
              fakes=Fakes(FakePublisher(), FakeInstaller()),
              project="alpha", job=job_name, on_progress=None, processor_factory=None)


def test_finish_after_handoff_records_plain_fingerprint_with_no_file_dep_targets(
    workspace: Workspace,
) -> None:
    """Regression lock: a project with no file_dep_targets must keep
    getting exactly the old plain tree fingerprint recorded -- no
    behavior change for the common case."""
    job_name = "certfp-plain"
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", job_name,
                          "full", roles=("builder",))
    _plant_secret(workspace.alpha)

    outcome = finish_after_handoff(workspace.config, workspace.ledger, "alpha", job_name,
                                   _EMPTY_PLAN, attempt_id, Path("report.md"), 2)

    assert outcome.status == "needs_owner"
    plain_fp = evidence.fingerprint(workspace.alpha, workspace.config.projects["alpha"].excludes)
    assert _latest_certified_fingerprint(workspace.ledger, "alpha") == plain_fp


def test_finish_after_handoff_records_file_dep_aware_fingerprint(
    workspace: Workspace, tmp_path: Path,
) -> None:
    """a review finding (4) reproduction, record side: a project WITH
    file_dep_targets must record the file-dep-aware evidence
    fingerprint (evidence.with_file_deps), not the plain tree
    fingerprint alone -- otherwise the lookup side (resume_
    security_fix) can never tell the kit's own content apart from
    "nothing changed"."""
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

    assert outcome.status == "needs_owner"
    plain_fp = evidence.fingerprint(ws.alpha, ws.config.projects["alpha"].excludes)
    expected = evidence.with_file_deps(plain_fp, targets)
    recorded = _latest_certified_fingerprint(ws.ledger, "alpha")
    assert recorded == expected
    assert recorded != plain_fp  # must actually fold the kit in, not a no-op


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
