"""Acceptance 05: same payload submitted twice is one request and one
effect; the processor never re-reads the live artifact once a request has
been accepted (it uses the ledger's recorded payload, not whatever bytes
happen to be on disk at process time)."""
from __future__ import annotations

from engine.app_security import security_policy_hash
from engine.approvals import grant
from engine.evidence import fingerprint as _repo_fingerprint
from engine.evidence import record, sha256_file
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.ledger import Event
from engine.processor import Fakes, Processor, submit
from engine.steps.open_job import open_job
from tests.conftest import Workspace


def _proc(workspace: Workspace, publisher: FakePublisher) -> Processor:
    proc = Processor(workspace.config, workspace.ledger, Fakes(publisher, FakeInstaller()))
    proc.register_defaults()
    return proc


def _certify_full(workspace: Workspace, project: str, attempt_id: str) -> None:
    """Record a passing full-suite bench AND a passing acceptance +
    app_security certificate, bound to the project repo's CURRENT
    fingerprint -- engine.steps.publish's call_publisher now requires
    both of these unconditionally before any real dispatch (not only
    when `release` is configured), see that module's own docstring."""
    project_cfg = workspace.config.projects[project]
    fp = _repo_fingerprint(project_cfg.repo, project_cfg.excludes)
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


def test_same_publish_payload_twice_dedupes_and_calls_once(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job1",
                          "full", roles=("builder",))
    original_hash = sha256_file(workspace.beta / "dist" / "bundle.js")
    payload = {"target": "store", "artifact_path": "dist/bundle.js",
               "artifact_hash": original_hash}

    rid1 = submit(workspace.ledger, "beta", attempt_id, "publish", payload, "owner")
    rid2 = submit(workspace.ledger, "beta", attempt_id, "publish", payload, "owner")
    assert rid1 == rid2
    assert len(workspace.ledger.events("beta", "request.submitted")) == 1

    grant(workspace.ledger, "beta", rid1, ttl_sec=3600)

    # Worker changes the artifact bytes after submission; the ledger's
    # recorded payload hash must still be the one checked, so this must
    # fail the artifact check rather than silently publish the new bytes.
    (workspace.beta / "dist" / "bundle.js").write_text("new build\n", encoding="utf-8")

    publisher = FakePublisher()
    proc = _proc(workspace, publisher)
    n = proc.run_once("beta")

    assert n == 0
    req = workspace.ledger.request(rid1)
    assert req.status == "failed"
    assert req.result == {"reason": "artifact_mismatch"}
    assert publisher.calls == []

    # Re-running after a terminal 'failed' status makes no second call either
    # (queued_requests no longer returns it).
    n2 = proc.run_once("beta")
    assert n2 == 0
    assert publisher.calls == []


def test_successful_publish_run_again_makes_no_second_call(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job2",
                          "full", roles=("builder",))
    artifact_hash = sha256_file(workspace.beta / "dist" / "bundle.js")
    payload = {"target": "store", "artifact_path": "dist/bundle.js",
               "artifact_hash": artifact_hash}
    rid = submit(workspace.ledger, "beta", attempt_id, "publish", payload, "owner")
    grant(workspace.ledger, "beta", rid, ttl_sec=3600)
    _certify_full(workspace, "beta", attempt_id)

    publisher = FakePublisher()
    proc = _proc(workspace, publisher)
    n1 = proc.run_once("beta")
    assert n1 == 1
    assert len(publisher.calls) == 1

    n2 = proc.run_once("beta")
    assert n2 == 0
    assert len(publisher.calls) == 1
