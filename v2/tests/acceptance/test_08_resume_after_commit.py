"""Acceptance 08: resume after record_commit (before report) completes the
report without a second commit; resume after record_publish completes the
report without a second publisher call; a publisher crash after its side
effect leaves the request 'unknown' until a status lookup confirms it.

A request that reached 'unknown' is resumed only through the owner input
processor.retry_unknown(); run_once() then re-enters its step journal.
"""
from __future__ import annotations

import subprocess

from engine import ids
from engine.approvals import grant
from engine.app_security import security_policy_hash
from engine.evidence import fingerprint, record, sha256_file
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.faults import CrashInjected, Faults
from engine.ledger import Event
from engine.processor import Fakes, Processor, retry_unknown, submit
from engine.steps.open_job import open_job
from tests.conftest import seed_reviewed_manifest, Workspace


def _certify_full(workspace: Workspace, project: str, attempt_id: str) -> None:
    """Record a passing full-suite bench AND a passing acceptance +
    app_security certificate, bound to the project repo's CURRENT
    fingerprint -- engine.steps.publish's call_publisher now requires
    both of these unconditionally before any real dispatch (not only
    when `release` is configured), see that module's own docstring."""
    project_cfg = workspace.config.projects[project]
    fp = fingerprint(project_cfg.repo, project_cfg.excludes)
    record(workspace.ledger, project, attempt_id, "bench", fp,
           payload={"passed": True, "scope": "full"})
    for kind in ("acceptance.recorded", "app_security.recorded"):
        workspace.ledger.append(Event(
            project=project, kind=kind, actor_kind="processor",
            actor_id="processor", attempt_id=attempt_id,
            payload={"passed": True, "fingerprint": fp,
                     **({"policy_hash": security_policy_hash(workspace.config)}
                        if kind == "app_security.recorded" else {})},
        ))
    seed_reviewed_manifest(workspace.ledger, project, attempt_id)


def _rev_count(repo) -> int:
    return int(subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip())


def _report_exists(workspace: Workspace, attempt_id: str) -> bool:
    path = workspace.config.home / "jobs" / ids.attempt_dirname(attempt_id) / "report.md"
    return path.exists() and attempt_id in path.read_text(encoding="utf-8")


def test_resume_after_record_commit_before_report(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job1",
                          "full", roles=("builder",))
    (workspace.alpha / "src" / "app.py").write_text("print('v2')\n", encoding="utf-8")
    fp = fingerprint(workspace.alpha, workspace.config.projects["alpha"].excludes)
    record(workspace.ledger, "alpha", attempt_id, "review", fp, payload={"passed": True})
    record(workspace.ledger, "alpha", attempt_id, "bench", fp, payload={"passed": True})
    before = _rev_count(workspace.alpha)

    rid = submit(workspace.ledger, "alpha", attempt_id, "commit", {"message": "m"}, "owner")
    proc1 = Processor(workspace.config, workspace.ledger, Fakes(FakePublisher(), FakeInstaller()),
                      faults=Faults(crash_after={"record_commit"}))
    proc1.register_defaults()
    try:
        proc1.run_once("alpha")
    except CrashInjected:
        pass

    proc2 = Processor(workspace.config, workspace.ledger, Fakes(FakePublisher(), FakeInstaller()))
    proc2.register_defaults()
    n = proc2.run_once("alpha")

    assert n == 1
    assert _rev_count(workspace.alpha) == before + 1
    assert workspace.ledger.request(rid).status == "done"
    assert _report_exists(workspace, attempt_id)


def test_resume_after_record_publish_before_report(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job2",
                          "full", roles=("builder",))
    artifact_hash = sha256_file(workspace.beta / "dist" / "bundle.js")
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "dist/bundle.js",
                  "artifact_hash": artifact_hash}, "owner")
    grant(workspace.ledger, "beta", rid, ttl_sec=3600)
    _certify_full(workspace, "beta", attempt_id)

    publisher = FakePublisher()
    proc1 = Processor(workspace.config, workspace.ledger, Fakes(publisher, FakeInstaller()),
                      faults=Faults(crash_after={"record_publish"}))
    proc1.register_defaults()
    try:
        proc1.run_once("beta")
    except CrashInjected:
        pass

    proc2 = Processor(workspace.config, workspace.ledger, Fakes(publisher, FakeInstaller()))
    proc2.register_defaults()
    n = proc2.run_once("beta")

    assert n == 1
    assert len(publisher.calls) == 1
    assert workspace.ledger.request(rid).status == "done"
    assert _report_exists(workspace, attempt_id)


def test_publisher_crash_after_effect_then_lookup_confirms_done(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job3",
                          "full", roles=("builder",))
    artifact_hash = sha256_file(workspace.beta / "dist" / "bundle.js")
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "dist/bundle.js",
                  "artifact_hash": artifact_hash}, "owner")
    grant(workspace.ledger, "beta", rid, ttl_sec=3600)
    _certify_full(workspace, "beta", attempt_id)

    publisher = FakePublisher(fail_after_effect=True)
    proc = Processor(workspace.config, workspace.ledger, Fakes(publisher, FakeInstaller()))
    proc.register_defaults()
    n = proc.run_once("beta")

    assert n == 0
    assert workspace.ledger.request(rid).status == "unknown"
    assert len(publisher.calls) == 1

    # The response arrives on a later status lookup: flip off the crash flag
    # and resume the single request directly (see module docstring).
    publisher.fail_after_effect = False
    retry_unknown(workspace.ledger, rid, "owner")
    assert proc.run_once("beta") == 1

    assert workspace.ledger.request(rid).status == "done"
    assert len(publisher.calls) == 1
    assert _report_exists(workspace, attempt_id)


def test_publisher_lookup_failure_keeps_request_unknown(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job4",
                          "full", roles=("builder",))
    artifact_hash = sha256_file(workspace.beta / "dist" / "bundle.js")
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "dist/bundle.js",
                  "artifact_hash": artifact_hash}, "owner")
    grant(workspace.ledger, "beta", rid, ttl_sec=3600)
    _certify_full(workspace, "beta", attempt_id)

    publisher = FakePublisher(fail_after_effect=True, lookup_fails=True)
    proc = Processor(workspace.config, workspace.ledger, Fakes(publisher, FakeInstaller()))
    proc.register_defaults()
    n = proc.run_once("beta")
    assert n == 0
    assert workspace.ledger.request(rid).status == "unknown"

    retry_unknown(workspace.ledger, rid, "owner")
    assert proc.run_once("beta") == 0
    assert workspace.ledger.request(rid).status == "unknown"
    assert len(publisher.calls) == 1
