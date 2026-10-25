"""Unit tests for engine.steps.publish: full publish flow via the
Processor, plus direct checks of the call_publisher reconcile predicate."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

import pytest

from engine.approvals import grant
from engine.evidence import fingerprint as _repo_fingerprint
from engine.evidence import record, sha256_file
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.faults import CrashInjected, Faults
from engine.ledger import Event
from engine.processor import Fakes, Processor, StepContext, StepUnknown, submit
from engine.steps.open_job import open_job
from engine.steps.publish import _reconcile_call_publisher, _run_record_publish
from tests.conftest import Workspace


def _proc(workspace: Workspace, publisher: FakePublisher | None = None) -> Processor:
    proc = Processor(workspace.config, workspace.ledger,
                      Fakes(publisher or FakePublisher(), FakeInstaller()))
    proc.register_defaults()
    return proc


def _certify_full(workspace: Workspace, project: str, attempt_id: str) -> str:
    """Record a passing full-suite bench AND a passing acceptance +
    app_security certificate, all bound to the project repo's CURRENT
    fingerprint -- the evidence engine.steps.publish's call_publisher
    now requires UNCONDITIONALLY before any real dispatch (see that
    module's own docstring), not only when `release` happens to be
    configured. None of these tests go through engine.full_orbit/
    engine.acceptance/engine.app_security themselves, so this appends
    the same shaped events those modules' own record functions do,
    directly, the same way this file already seeds 'source'/'publish'
    evidence by hand elsewhere. Returns the fingerprint it certified."""
    project_cfg = workspace.config.projects[project]
    fp = _repo_fingerprint(project_cfg.repo, project_cfg.excludes)
    record(workspace.ledger, project, attempt_id, "bench", fp,
           payload={"passed": True, "scope": "full"})
    for kind in ("acceptance.recorded", "app_security.recorded"):
        workspace.ledger.append(Event(
            project=project, kind=kind, actor_kind="processor",
            actor_id="processor", attempt_id=attempt_id,
            payload={"passed": True, "fingerprint": fp},
        ))
    return fp


def test_publish_succeeds_with_matching_approval_and_artifact(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job1",
                          "full", roles=("builder",))
    artifact_hash = sha256_file(workspace.beta / "dist" / "bundle.js")
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "dist/bundle.js",
                  "artifact_hash": artifact_hash}, "owner")
    grant(workspace.ledger, "beta", rid, ttl_sec=3600)
    _certify_full(workspace, "beta", attempt_id)

    publisher = FakePublisher()
    proc = _proc(workspace, publisher)
    n = proc.run_once("beta")

    assert n == 1
    assert workspace.ledger.request(rid).status == "done"
    assert len(publisher.calls) == 1
    assert workspace.ledger.events("beta", "publish.recorded")


def test_publish_fails_without_approval(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job2",
                          "full", roles=("builder",))
    artifact_hash = sha256_file(workspace.beta / "dist" / "bundle.js")
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "dist/bundle.js",
                  "artifact_hash": artifact_hash}, "owner")

    publisher = FakePublisher()
    proc = _proc(workspace, publisher)
    n = proc.run_once("beta")

    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "no_approval"}
    assert publisher.calls == []


def test_publish_without_release_still_requires_full_bench_before_dispatch(
    workspace: Workspace,
) -> None:
    """Regression for codex-goal-review-4.md item 13 / codex-diff-
    review-s17.md "Whole-job verification is not a universal
    deployment invariant" (engine/steps/publish.py:407,545): this
    project has NO `release` configured at all -- before this fix, an
    approved+artifact-matching publish reached the real publisher call
    on nothing more than check_approval/check_artifact, with no full-
    suite bench and no job certificate required, simply because
    release_config was None. A passing approval and artifact hash
    alone must no longer be enough."""
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job-no-bench",
                          "full", roles=("builder",))
    artifact_hash = sha256_file(workspace.beta / "dist" / "bundle.js")
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "dist/bundle.js",
                  "artifact_hash": artifact_hash}, "owner")
    grant(workspace.ledger, "beta", rid, ttl_sec=3600)
    # Deliberately no _certify_full(...) call here.

    publisher = FakePublisher()
    n = _proc(workspace, publisher).run_once("beta")

    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "full_bench_missing"}
    assert publisher.calls == []


def test_publish_without_release_still_requires_job_certificate_before_dispatch(
    workspace: Workspace,
) -> None:
    """Same finding as the bench test above, for the OTHER half of the
    universal gate: a passing full-suite bench alone (no release
    configured, and no acceptance/app_security certificate for this
    job) must still refuse to dispatch."""
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job-no-cert",
                          "full", roles=("builder",))
    artifact_hash = sha256_file(workspace.beta / "dist" / "bundle.js")
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "dist/bundle.js",
                  "artifact_hash": artifact_hash}, "owner")
    grant(workspace.ledger, "beta", rid, ttl_sec=3600)

    project_cfg = workspace.config.projects["beta"]
    fp = _repo_fingerprint(project_cfg.repo, project_cfg.excludes)
    record(workspace.ledger, "beta", attempt_id, "bench", fp,
           payload={"passed": True, "scope": "full"})
    # Deliberately no acceptance.recorded/app_security.recorded events.

    publisher = FakePublisher()
    n = _proc(workspace, publisher).run_once("beta")

    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "release_requires_job_certificate"}
    assert publisher.calls == []


def test_publish_fails_on_artifact_mismatch(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job3",
                          "full", roles=("builder",))
    artifact_hash = sha256_file(workspace.beta / "dist" / "bundle.js")
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "dist/bundle.js",
                  "artifact_hash": artifact_hash}, "owner")
    grant(workspace.ledger, "beta", rid, ttl_sec=3600)
    (workspace.beta / "dist" / "bundle.js").write_text("tampered\n", encoding="utf-8")

    publisher = FakePublisher()
    proc = _proc(workspace, publisher)
    n = proc.run_once("beta")

    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "artifact_mismatch"}
    assert publisher.calls == []


@dataclass
class _StubPublisher:
    """A minimal Publisher whose lookup() returns whatever fixed dict
    (or None) the test wants, to exercise _reconcile_call_publisher's
    receipt-field validation against shapes FakePublisher itself
    cannot produce (it never sets "status" or a mismatched identity)."""
    receipt: dict[str, object] | None

    def publish(self, key: str, target: str, artifact_hash: str) -> dict[str, object]:
        raise NotImplementedError("not used by these reconcile-only tests")

    def lookup(self, key: str) -> dict[str, object] | None:
        return self.receipt


def _ctx_for(workspace: Workspace, rid: str, attempt_id: str, publisher: object) -> StepContext:
    request = workspace.ledger.request(rid)
    return StepContext(config=workspace.config, ledger=workspace.ledger,
                       fakes=Fakes(publisher, FakeInstaller()),
                       request=request, project="beta", attempt_id=attempt_id)


def test_call_publisher_reconcile_false_before_any_call_and_true_after_a_validated_done(
    workspace: Workspace,
) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job4",
                          "full", roles=("builder",))
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "dist/bundle.js",
                  "artifact_hash": "x"}, "owner")
    ctx = _ctx_for(workspace, rid, attempt_id, _StubPublisher(receipt=None))
    assert _reconcile_call_publisher(ctx) is False

    ctx2 = _ctx_for(workspace, rid, attempt_id, _StubPublisher(
        receipt={"key": rid, "target": "store", "artifact_hash": "x", "status": "done"}))
    assert _reconcile_call_publisher(ctx2) is True


def test_call_publisher_reconcile_raises_step_unknown_on_lookup_failure(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job5",
                          "full", roles=("builder",))
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "dist/bundle.js",
                  "artifact_hash": "x"}, "owner")
    request = workspace.ledger.request(rid)
    publisher = FakePublisher(lookup_fails=True)
    ctx = StepContext(config=workspace.config, ledger=workspace.ledger,
                      fakes=Fakes(publisher, FakeInstaller()),
                      request=request, project="beta", attempt_id=attempt_id)
    with pytest.raises(StepUnknown):
        _reconcile_call_publisher(ctx)


def test_call_publisher_reconcile_treats_an_empty_receipt_as_unknown(
    workspace: Workspace,
) -> None:
    """A receipt with no "status" field at all (missing/unrecognizable)
    must never be treated as success -- only an explicit, validated
    "done" is."""
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job-empty-receipt",
                          "full", roles=("builder",))
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "dist/bundle.js",
                  "artifact_hash": "x"}, "owner")
    ctx = _ctx_for(workspace, rid, attempt_id, _StubPublisher(receipt={}))
    with pytest.raises(StepUnknown):
        _reconcile_call_publisher(ctx)


def test_call_publisher_reconcile_rejects_a_done_receipt_with_mismatched_artifact_hash(
    workspace: Workspace,
) -> None:
    """A "done" receipt sitting at the right lookup key but naming a
    DIFFERENT artifact_hash than this request's own payload must not be
    trusted as this request's success."""
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job-mismatch",
                          "full", roles=("builder",))
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "dist/bundle.js",
                  "artifact_hash": "x"}, "owner")
    ctx = _ctx_for(workspace, rid, attempt_id, _StubPublisher(
        receipt={"key": rid, "target": "store", "artifact_hash": "DIFFERENT", "status": "done"}))
    with pytest.raises(StepUnknown):
        _reconcile_call_publisher(ctx)


def test_publish_rejects_absolute_artifact_path(workspace: Workspace) -> None:
    """artifact_path is joined to the repo without checking it stays
    inside the repo or inside the project's configured artifact_paths. An
    absolute path must be rejected before any hashing happens."""
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job-abs",
                          "full", roles=("builder",))
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "/etc/passwd",
                  "artifact_hash": "x"}, "owner")
    grant(workspace.ledger, "beta", rid, ttl_sec=3600)

    publisher = FakePublisher()
    proc = _proc(workspace, publisher)
    n = proc.run_once("beta")

    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "path_rejected"}
    assert publisher.calls == []


def test_publish_rejects_path_traversal_outside_repo(workspace: Workspace) -> None:
    """A '..' artifact_path that resolves outside the repo (e.g. to
    the engine's secrets dir) must be rejected, not used as a hash oracle."""
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job-traverse",
                          "full", roles=("builder",))
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "../../secrets/prod.key",
                  "artifact_hash": "x"}, "owner")
    grant(workspace.ledger, "beta", rid, ttl_sec=3600)

    publisher = FakePublisher()
    proc = _proc(workspace, publisher)
    n = proc.run_once("beta")

    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "path_rejected"}
    assert publisher.calls == []


def test_publish_rejects_path_outside_configured_artifact_paths(workspace: Workspace) -> None:
    """beta's ProjectConfig.artifact_paths is ("dist/",) — a path
    inside the repo but outside that prefix must still be rejected."""
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job-outside",
                          "full", roles=("builder",))
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "lib/main.py",
                  "artifact_hash": "x"}, "owner")
    grant(workspace.ledger, "beta", rid, ttl_sec=3600)

    publisher = FakePublisher()
    proc = _proc(workspace, publisher)
    n = proc.run_once("beta")

    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "path_rejected"}
    assert publisher.calls == []


def test_record_publish_crash_after_leaves_exactly_one_event(workspace: Workspace) -> None:
    """A crash right after record_publish's run() (publish.recorded
    already appended) must not duplicate that event on resume."""
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job-dedupe",
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
    assert workspace.ledger.request(rid).status == "done"
    events = [e for _seq, e in workspace.ledger.events("beta", "publish.recorded")
              if e.payload.get("request_id") == rid]
    assert len(events) == 1


def test_record_source_recomputes_fp_for_a_second_publish_after_source_change(
    workspace: Workspace,
) -> None:
    """record_source must not reuse whatever 'source' evidence the
    attempt already has. Two publishes in the same attempt, with the
    repo changed in between (unrelated to the artifact itself), must each
    record their OWN fingerprint, not the first one's."""
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job-8",
                          "full", roles=("builder",))
    artifact_hash = sha256_file(workspace.beta / "dist" / "bundle.js")

    rid1 = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store1", "artifact_path": "dist/bundle.js",
                  "artifact_hash": artifact_hash}, "owner")
    grant(workspace.ledger, "beta", rid1, ttl_sec=3600)
    _certify_full(workspace, "beta", attempt_id)
    n1 = _proc(workspace).run_once("beta")
    assert n1 == 1

    source_rows_1 = [r for r in workspace.ledger.evidence(attempt_id, "source")
                     if r.payload.get("request_id") == rid1]
    assert len(source_rows_1) == 1
    fp1 = source_rows_1[0].fingerprint

    # The repo changes -- unrelated to the artifact bytes themselves.
    (workspace.beta / "lib" / "main.py").write_text("print('changed')\n", encoding="utf-8")

    rid2 = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store2", "artifact_path": "dist/bundle.js",
                  "artifact_hash": artifact_hash}, "owner")
    assert rid2 != rid1
    grant(workspace.ledger, "beta", rid2, ttl_sec=3600)
    _certify_full(workspace, "beta", attempt_id)
    n2 = _proc(workspace).run_once("beta")
    assert n2 == 1

    source_rows_2 = [r for r in workspace.ledger.evidence(attempt_id, "source")
                     if r.payload.get("request_id") == rid2]
    assert len(source_rows_2) == 1
    fp2 = source_rows_2[0].fingerprint

    assert fp1 != fp2
    publish_rows_2 = [r for r in workspace.ledger.evidence(attempt_id, "publish")
                      if r.payload.get("request_id") == rid2]
    assert len(publish_rows_2) == 1
    assert publish_rows_2[0].fingerprint == fp2


def test_record_publish_does_not_duplicate_evidence_when_only_event_missing(
    workspace: Workspace,
) -> None:
    """The evidence insert and the publish.recorded event in
    record_publish are two separate ledger appends. A crash landing
    between them (no step-level fault boundary exists mid-function, so
    this reproduces it directly: the evidence row from a 'crashed' first
    attempt is seeded by hand, the event is not) must not duplicate the
    evidence row when the step's run() is called again on resume."""
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job-25a",
                          "full", roles=("builder",))
    artifact_hash = sha256_file(workspace.beta / "dist" / "bundle.js")
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "dist/bundle.js",
                  "artifact_hash": artifact_hash}, "owner")
    request = workspace.ledger.request(rid)
    ctx = StepContext(config=workspace.config, ledger=workspace.ledger,
                      fakes=Fakes(FakePublisher(), FakeInstaller()),
                      request=request, project="beta", attempt_id=attempt_id)

    record(workspace.ledger, "beta", attempt_id, "source", "f" * 40,
           payload={"request_id": rid})
    # Stand in for the crash: the evidence insert half of a prior run()
    # already happened; the publish.recorded event never got appended.
    record(workspace.ledger, "beta", attempt_id, "publish", "f" * 40,
           artifact_hash=artifact_hash, payload={"request_id": rid})

    _run_record_publish(ctx)

    evidence_rows = [r for r in workspace.ledger.evidence(attempt_id, "publish")
                     if r.payload.get("request_id") == rid]
    events = [e for _seq, e in workspace.ledger.events("beta", "publish.recorded")
              if e.payload.get("request_id") == rid]
    assert len(evidence_rows) == 1
    assert len(events) == 1


def _head_sha(repo) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_publish_head_artifact_accepted_when_clean(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job-head-ok",
                          "full", roles=("builder",))
    head = _head_sha(workspace.beta)
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "@HEAD", "artifact_hash": head}, "owner")
    grant(workspace.ledger, "beta", rid, ttl_sec=3600)
    _certify_full(workspace, "beta", attempt_id)

    publisher = FakePublisher()
    n = _proc(workspace, publisher).run_once("beta")

    assert n == 1
    assert workspace.ledger.request(rid).status == "done"
    assert publisher.calls[0].artifact_hash == head
    assert workspace.ledger.events("beta", "publish.recorded")


def test_publish_head_artifact_rejected_on_sha_mismatch(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job-head-mismatch",
                          "full", roles=("builder",))
    wrong_sha = "a" * 40
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "@HEAD", "artifact_hash": wrong_sha},
                 "owner")
    grant(workspace.ledger, "beta", rid, ttl_sec=3600)

    publisher = FakePublisher()
    n = _proc(workspace, publisher).run_once("beta")

    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "artifact_mismatch"}
    assert publisher.calls == []


def test_publish_head_artifact_rejected_on_malformed_sha(workspace: Workspace) -> None:
    """artifact_hash must be a full 40-hex commit sha for @HEAD -- a
    short or non-hex value is rejected the same way (artifact_mismatch),
    never treated as "no sha at all means skip the check"."""
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job-head-badsha",
                          "full", roles=("builder",))
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "@HEAD", "artifact_hash": "not-a-sha"},
                 "owner")
    grant(workspace.ledger, "beta", rid, ttl_sec=3600)

    publisher = FakePublisher()
    n = _proc(workspace, publisher).run_once("beta")

    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "artifact_mismatch"}


def test_publish_head_artifact_rejected_on_dirty_tree(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job-head-dirty",
                          "full", roles=("builder",))
    head = _head_sha(workspace.beta)
    (workspace.beta / "lib" / "main.py").write_text("print('dirty')\n", encoding="utf-8")
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "@HEAD", "artifact_hash": head}, "owner")
    grant(workspace.ledger, "beta", rid, ttl_sec=3600)

    publisher = FakePublisher()
    n = _proc(workspace, publisher).run_once("beta")

    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "tree_dirty"}
    assert publisher.calls == []


def test_publish_head_approval_bound_to_a_different_request_is_rejected(
    workspace: Workspace,
) -> None:
    """Two separate publish requests, two different HEAD shas (the repo
    advances in between) -- granting approval for the first must never
    satisfy the second; approvals.check binds to the exact request_id
    (and therefore the exact sha captured in ITS OWN payload at grant
    time), the same way it already does for a file artifact_hash."""
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job-head-twosha",
                          "full", roles=("builder",))
    head_a = _head_sha(workspace.beta)
    rid_a = submit(workspace.ledger, "beta", attempt_id, "publish",
                   {"target": "store", "artifact_path": "@HEAD", "artifact_hash": head_a},
                   "owner")
    grant(workspace.ledger, "beta", rid_a, ttl_sec=3600)
    _certify_full(workspace, "beta", attempt_id)

    publisher = FakePublisher()
    proc = _proc(workspace, publisher)
    # rid_a's own approval, bound to head_a, lets it through while HEAD
    # is still head_a -- processed BEFORE the repo advances, since
    # @HEAD always checks the CURRENT HEAD at processing time.
    n1 = proc.run_once("beta")
    assert n1 == 1
    assert workspace.ledger.request(rid_a).status == "done"

    GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
              "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
    subprocess.run(["git", "-C", str(workspace.beta), "commit", "--allow-empty",
                    "-q", "-m", "advance"], check=True, capture_output=True, env=GIT_ENV)
    head_b = _head_sha(workspace.beta)
    assert head_b != head_a
    rid_b = submit(workspace.ledger, "beta", attempt_id, "publish",
                   {"target": "store", "artifact_path": "@HEAD", "artifact_hash": head_b},
                   "owner")
    # rid_b has no approval of its own -- rid_a's approval, bound to a
    # different request_id (and a different sha), does not carry over.
    n2 = proc.run_once("beta")
    assert n2 == 0
    req_b = workspace.ledger.request(rid_b)
    assert req_b.status == "failed"
    assert req_b.result == {"reason": "no_approval"}


def test_publish_records_dry_run_flag_from_publisher_receipt(workspace: Workspace) -> None:
    """record_publish reads dry_run from the publisher's own receipt
    (an observable fact), not from call_publisher's journaled payload
    -- FakePublisher carries no "dry_run" concept at all, so this must
    default to False rather than raising or omitting the key."""
    attempt_id = open_job(workspace.config, workspace.ledger, "beta", "job-dryrun-flag",
                          "full", roles=("builder",))
    artifact_hash = sha256_file(workspace.beta / "dist" / "bundle.js")
    rid = submit(workspace.ledger, "beta", attempt_id, "publish",
                 {"target": "store", "artifact_path": "dist/bundle.js",
                  "artifact_hash": artifact_hash}, "owner")
    grant(workspace.ledger, "beta", rid, ttl_sec=3600)
    _certify_full(workspace, "beta", attempt_id)

    n = _proc(workspace).run_once("beta")
    assert n == 1

    publish_rows = [r for r in workspace.ledger.evidence(attempt_id, "publish")
                    if r.payload.get("request_id") == rid]
    assert len(publish_rows) == 1
    assert publish_rows[0].payload["dry_run"] is False

    recorded = [e for _seq, e in workspace.ledger.events("beta", "publish.recorded")
                if e.payload.get("request_id") == rid]
    assert len(recorded) == 1
    assert recorded[0].payload["dry_run"] is False
