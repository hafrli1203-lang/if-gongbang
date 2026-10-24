"""Acceptance 10: commit and publish refuse on evidence/artifact mismatch —
a stale fingerprint, a stale review, or a tampered artifact byte must fail
closed, never pass."""
from __future__ import annotations

import time

from engine.approvals import grant
from engine.evidence import fingerprint, invalidate_after, record, sha256_file
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.faults import CrashInjected, Faults
from engine.processor import Fakes, Processor, submit
from engine.steps.open_job import open_job
from tests.conftest import Workspace


def _commit_proc(workspace: Workspace) -> Processor:
    proc = Processor(workspace.config, workspace.ledger, Fakes(FakePublisher(), FakeInstaller()))
    proc.register_defaults()
    return proc


def test_commit_rejects_review_recorded_for_an_old_fingerprint(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job1",
                          "full", roles=("builder",))
    old_fp = fingerprint(workspace.alpha, workspace.config.projects["alpha"].excludes)
    record(workspace.ledger, "alpha", attempt_id, "review", old_fp, payload={"passed": True})
    record(workspace.ledger, "alpha", attempt_id, "bench", old_fp, payload={"passed": True})
    # An extra edit happens after the review/bench were recorded.
    (workspace.alpha / "src" / "app.py").write_text("print('post-review-edit')\n",
                                                     encoding="utf-8")

    rid = submit(workspace.ledger, "alpha", attempt_id, "commit", {"message": "m"}, "owner")
    n = _commit_proc(workspace).run_once("alpha")

    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "fingerprints_differ"}


def test_commit_rejects_stale_review_evidence(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job2",
                          "full", roles=("builder",))
    (workspace.alpha / "src" / "app.py").write_text("print('v2')\n", encoding="utf-8")
    fp = fingerprint(workspace.alpha, workspace.config.projects["alpha"].excludes)
    record(workspace.ledger, "alpha", attempt_id, "review", fp, payload={"passed": True})
    record(workspace.ledger, "alpha", attempt_id, "bench", fp, payload={"passed": True})
    # A dependency/config change invalidates the review after the fact.
    invalidate_after(workspace.ledger, "alpha", attempt_id, ("review",))

    rid = submit(workspace.ledger, "alpha", attempt_id, "commit", {"message": "m"}, "owner")
    n = _commit_proc(workspace).run_once("alpha")

    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    # three_match_reason distinguishes WHICH evidence is missing/invalid:
    # the review was invalidated, so there is no current (non-stale)
    # review to pass -- "review_not_passed", not the generic
    # "fingerprints_differ" this used to report for every mismatch shape.
    assert req.result == {"reason": "review_not_passed"}


def test_publish_rejects_tampered_artifact_byte_and_never_calls_publisher(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job3",
                          "full", roles=("builder",))
    approved_hash = sha256_file(workspace.beta / "dist" / "bundle.js")
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "dist/bundle.js",
                  "artifact_hash": approved_hash}, "owner")
    grant(workspace.ledger, "beta", rid, ttl_sec=3600)

    # Flip one byte in the artifact file after approval.
    original = (workspace.beta / "dist" / "bundle.js").read_bytes()
    tampered = bytearray(original)
    tampered[0] ^= 0xFF
    (workspace.beta / "dist" / "bundle.js").write_bytes(bytes(tampered))
    assert sha256_file(workspace.beta / "dist" / "bundle.js") != approved_hash

    publisher = FakePublisher()
    proc = Processor(workspace.config, workspace.ledger, Fakes(publisher, FakeInstaller()))
    proc.register_defaults()
    n = proc.run_once("beta")

    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "artifact_mismatch"}
    assert publisher.calls == []


def test_publish_resumed_check_artifact_catches_tamper_after_crash(workspace: Workspace) -> None:
    """C-8: check_approval/check_artifact are journaled 'done' well before
    the first publisher effect and are never re-run once done — a crash
    between them and call_publisher followed by an artifact edit must not
    let the resumed run publish the new bytes. publisher.calls must stay
    empty."""
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job-c8a",
                          "full", roles=("builder",))
    artifact_hash = sha256_file(workspace.beta / "dist" / "bundle.js")
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "dist/bundle.js",
                  "artifact_hash": artifact_hash}, "owner")
    grant(workspace.ledger, "beta", rid, ttl_sec=3600)

    publisher = FakePublisher()
    proc1 = Processor(workspace.config, workspace.ledger, Fakes(publisher, FakeInstaller()),
                      faults=Faults(crash_after={"check_artifact"}))
    proc1.register_defaults()
    try:
        proc1.run_once("beta")
    except CrashInjected:
        pass

    (workspace.beta / "dist" / "bundle.js").write_text("tampered-after-crash\n", encoding="utf-8")

    proc2 = Processor(workspace.config, workspace.ledger, Fakes(publisher, FakeInstaller()))
    proc2.register_defaults()
    n = proc2.run_once("beta")

    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert publisher.calls == []


def test_publish_rejects_artifact_changed_right_before_dispatch(workspace: Workspace) -> None:
    """C-8: directly exercises call_publisher's own re-verification. A
    crash_before boundary leaves check_approval/check_artifact/record_source
    already journaled 'done' and call_publisher not yet started (not even
    'started' has run its body) — the resumed call_publisher must catch an
    artifact edit made in that window itself, not just rely on an earlier
    step having already run."""
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job-c8b",
                          "full", roles=("builder",))
    artifact_hash = sha256_file(workspace.beta / "dist" / "bundle.js")
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "dist/bundle.js",
                  "artifact_hash": artifact_hash}, "owner")
    grant(workspace.ledger, "beta", rid, ttl_sec=3600)

    publisher = FakePublisher()
    proc1 = Processor(workspace.config, workspace.ledger, Fakes(publisher, FakeInstaller()),
                      faults=Faults(crash_before={"call_publisher"}))
    proc1.register_defaults()
    try:
        proc1.run_once("beta")
    except CrashInjected:
        pass

    (workspace.beta / "dist" / "bundle.js").write_text("tampered-before-dispatch\n", encoding="utf-8")

    proc2 = Processor(workspace.config, workspace.ledger, Fakes(publisher, FakeInstaller()))
    proc2.register_defaults()
    n = proc2.run_once("beta")

    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "artifact_changed_before_publish"}
    assert publisher.calls == []


def test_publish_rejects_approval_expired_before_dispatch(workspace: Workspace) -> None:
    """C-8: a crash right after check_approval (approval already marked
    used) followed by the approval expiring before resume must not let
    call_publisher dispatch on a now-expired approval."""
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job-c8c",
                          "full", roles=("builder",))
    artifact_hash = sha256_file(workspace.beta / "dist" / "bundle.js")
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "dist/bundle.js",
                  "artifact_hash": artifact_hash}, "owner")
    grant(workspace.ledger, "beta", rid, ttl_sec=1)

    publisher = FakePublisher()
    proc1 = Processor(workspace.config, workspace.ledger, Fakes(publisher, FakeInstaller()),
                      faults=Faults(crash_after={"check_approval"}))
    proc1.register_defaults()
    try:
        proc1.run_once("beta")
    except CrashInjected:
        pass

    time.sleep(1.1)  # the 1-second approval is now expired

    proc2 = Processor(workspace.config, workspace.ledger, Fakes(publisher, FakeInstaller()))
    proc2.register_defaults()
    n = proc2.run_once("beta")

    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "approval_invalid_before_publish"}
    assert publisher.calls == []
