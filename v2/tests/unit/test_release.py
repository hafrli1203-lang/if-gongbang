"""Unit tests for engine.release and engine.steps.publish's advance_master
step: fast-forwarding a project's configured master repo and driving its
deploy through the publish request/approval machinery. A local fixture
builds its own "gamma" project (never alpha/beta from tests/conftest.py,
to avoid any collision with other test files) plus a `master_repo` that
is a `git clone --shared` of it -- --shared means master_repo's object
database has an alternates file pointing at gamma's own, so a new commit
gamma makes becomes visible to master_repo (git cat-file -e, diff, merge
--ff-only) without any fetch/push step, matching advance_master's own
`git -C master_repo merge --ff-only <sha>` (no fetch call of its own)."""
from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from textwrap import dedent

import pytest

from engine import evidence, release
from engine.approvals import grant
from engine.config import EngineConfig, ProjectConfig, ReleaseConfig
from engine.evidence import invalidate_after, record
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.faults import CrashInjected, Faults
from engine.ledger import Event, Ledger
from engine.processor import Fakes, Processor
from engine.publishers import ShellPublisher
from engine.steps.open_job import open_job
from tests.conftest import GIT_ENV, git, make_repo


class ReleaseWorkspace:
    def __init__(self, root: Path, config: EngineConfig, ledger: Ledger,
                 repo: Path, master_repo: Path) -> None:
        self.root = root
        self.config = config
        self.ledger = ledger
        self.repo = repo
        self.master_repo = master_repo


def _clone_shared(src: Path, dest: Path) -> Path:
    subprocess.run(["git", "clone", "--shared", "-q", str(src), str(dest)],
                   check=True, capture_output=True, env=GIT_ENV)
    return dest


@pytest.fixture
def rws(tmp_path: Path) -> ReleaseWorkspace:
    repo = make_repo(tmp_path / "repos" / "gamma",
                     {"app.py": "v1\n", "other.py": "o\n"})
    master_repo = _clone_shared(repo, tmp_path / "master" / "gamma")
    home = tmp_path / "home"
    secrets = tmp_path / "secrets"
    home.mkdir()
    secrets.mkdir()
    config = EngineConfig(
        ledger_path=tmp_path / "ledger.sqlite",
        home=home,
        secrets_dir=secrets,
        projects={
            "gamma": ProjectConfig(
                "gamma", repo,
                release=ReleaseConfig(master_repo=master_repo, target="gamma-app"),
            ),
        },
        station_owners={},
        approval_ttl_sec=3600,
    )
    ledger = Ledger.create(config.ledger_path)
    ledger.register_project("gamma")
    return ReleaseWorkspace(tmp_path, config, ledger, repo, master_repo)


def _open(rws: ReleaseWorkspace, job_name: str) -> str:
    return open_job(rws.config, rws.ledger, "gamma", job_name, "full", roles=())


def _commit(repo: Path, rel: str, content: str, message: str) -> str:
    (repo / rel).write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                   capture_output=True, env=GIT_ENV)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message],
                   check=True, capture_output=True, env=GIT_ENV)
    return git(repo, "rev-parse", "HEAD")


def _seed_full_bench(rws: ReleaseWorkspace, attempt_id: str) -> None:
    """Record a passing full-scope bench for rws.repo's CURRENT
    fingerprint -- advance_master's own defense-in-depth check
    (_full_bench_passed_for_release) refuses ("full_bench_missing")
    without one. A test driving advance_master directly via grant() +
    run_once (bypassing release.approve_and_run's own ensure_full_bench
    gate entirely) must seed this itself to reach an actual merge,
    exactly as a real ship would only get here after that gate already
    passed."""
    fp = evidence.fingerprint(rws.repo, rws.config.projects["gamma"].excludes)
    record(rws.ledger, "gamma", attempt_id, "bench", fp, payload={"passed": True, "scope": "full"})


def _current_fp(rws: ReleaseWorkspace) -> str:
    return evidence.fingerprint(rws.repo, rws.config.projects["gamma"].excludes)


def _job_name_for_test(rws: ReleaseWorkspace, attempt_id: str) -> str:
    for _seq, event in rws.ledger.events("gamma", "job.opened"):
        if event.payload.get("attempt_id") == attempt_id:
            return str(event.payload.get("job_name"))
    raise AssertionError(f"no job.opened event for {attempt_id!r}")


def _seed_plan_reviewed(rws: ReleaseWorkspace, attempt_id: str) -> None:
    """A 'plan.reviewed' event for `attempt_id`'s own job, zero
    requirement/task ids (every test in this file drives advance_master
    directly, never through a real plan review -- see _seed_acceptance's
    own docstring) -- engine.evidence_graph.build() refuses the whole
    job certificate ("plan: no reviewed manifest") without SOME such
    event, regardless of how many
    requirements the fallback zero-requirement Plan actually has."""
    rws.ledger.append(Event(
        project="gamma", kind="plan.reviewed", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"job": _job_name_for_test(rws, attempt_id), "digest": "stub-digest",
                 "scene_digests": {}, "requirement_ids": [], "task_ids": []},
    ))


def _seed_acceptance(rws: ReleaseWorkspace, attempt_id: str, fp: str | None = None) -> None:
    """Record a passing acceptance.recorded event for `attempt_id`,
    bound to `fp` (rws.repo's CURRENT fingerprint by default) --
    advance_master's job-level certificate check (engine.release.
    job_certificate_status) requires this, matched against the exact
    fingerprint of the commit being released. A test driving
    advance_master directly via grant() + run_once/approve_and_run
    (never through engine.full_orbit, which would have run engine.
    acceptance.run_acceptance itself) must seed this the same way it
    already has to seed a passing full bench. By itself this is only
    HALF of the job certificate -- see _seed_app_security and
    _seed_job_certificate below. Also seeds _seed_plan_reviewed:
    every caller of this function drives job_certificate_status
    somewhere down the line, and none of them ever go through a real
    plan review."""
    _seed_plan_reviewed(rws, attempt_id)
    rws.ledger.append(Event(
        project="gamma", kind="acceptance.recorded", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"job": "test-job", "passed": True, "scenes": [],
                 "full_suite_passed": True, "tree_changed": False, "reason": None,
                 "fingerprint": fp if fp is not None else _current_fp(rws)},
    ))


def _seed_app_security(rws: ReleaseWorkspace, attempt_id: str, fp: str | None = None, *,
                       policy_hash: str | None = None) -> None:
    """Record a passing app_security.recorded event for `attempt_id`,
    bound to `fp` (rws.repo's CURRENT fingerprint by default) -- the
    OTHER half of the job certificate engine.release.
    job_certificate_status requires, matching the real
    engine.app_security.record() payload shape
    ({"passed","findings","ran","fingerprint","policy_hash"}).
    `policy_hash` defaults to rws.config's OWN current
    security_policy_hash -- engine.steps.publish now always passes its
    real `config` into job_certificate_status (ctx.config, never
    None), so every test that goes through the real publish/Processor
    pipeline (approve_and_run, run_request, ...) needs a certificate
    that actually matches rws.config's policy by default; a test
    exercising a MISMATCH deliberately passes a different one."""
    from engine import app_security

    rws.ledger.append(Event(
        project="gamma", kind="app_security.recorded", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"passed": True, "findings": [], "ran": ["secret_scan"],
                 "fingerprint": fp if fp is not None else _current_fp(rws),
                 "policy_hash": (policy_hash if policy_hash is not None
                                 else app_security.security_policy_hash(rws.config))},
    ))


def _seed_job_certificate(rws: ReleaseWorkspace, attempt_id: str) -> None:
    """Both halves of the job certificate (acceptance AND app_security),
    bound to rws.repo's CURRENT fingerprint -- what a real engine.
    full_orbit release job would have recorded (once app_security's
    own .record() call is wired into full_orbit -- see engine.release.
    job_certificate_status's own docstring for that gap) before ever
    reaching engine.release.prepare. Every test driving advance_master
    to an actual, successful merge must seed this the same way it
    already has to seed a passing full bench."""
    fp = _current_fp(rws)
    _seed_acceptance(rws, attempt_id, fp)
    _seed_app_security(rws, attempt_id, fp)


def _proc(rws: ReleaseWorkspace, publisher: FakePublisher | None = None) -> Processor:
    proc = Processor(rws.config, rws.ledger, Fakes(publisher or FakePublisher(), FakeInstaller()))
    proc.register_defaults()
    return proc


# ---------------------------------------------------------------------------
# 1. no approval -> no publish, master untouched
# ---------------------------------------------------------------------------

def test_release_publish_fails_without_approval(rws: ReleaseWorkspace) -> None:
    attempt_id = _open(rws, "job1")
    master_before = release.head_sha(rws.master_repo)
    sha = _commit(rws.repo, "app.py", "v2\n", "advance")
    request_id, summary = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)

    assert summary["owner_text"] == f"승인 {request_id}"
    assert summary["target"] == "gamma-app"
    assert summary["artifact_hash"] == sha

    publisher = FakePublisher()
    n = _proc(rws, publisher).run_once("gamma")

    assert n == 0
    req = rws.ledger.request(request_id)
    assert req.status == "failed"
    assert req.result == {"reason": "no_approval"}
    assert publisher.calls == []
    assert release.head_sha(rws.master_repo) == master_before


# ---------------------------------------------------------------------------
# 2. approval granted for a DIFFERENT request/sha is rejected
# ---------------------------------------------------------------------------

def test_release_approval_for_different_hash_is_rejected(rws: ReleaseWorkspace) -> None:
    attempt_id = _open(rws, "job2")
    request_id_a, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)
    grant(rws.ledger, "gamma", request_id_a, ttl_sec=3600)
    _seed_full_bench(rws, attempt_id)
    _seed_job_certificate(rws, attempt_id)

    publisher = FakePublisher()
    proc = _proc(rws, publisher)
    n1 = proc.run_once("gamma")
    assert n1 == 1
    assert rws.ledger.request(request_id_a).status == "done"

    # repo advances again -> a second, different sha/request with no
    # approval of its own; request_id_a's approval must not carry over.
    _commit(rws.repo, "app.py", "v3\n", "advance again")
    request_id_b, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)
    assert request_id_b != request_id_a

    n2 = proc.run_once("gamma")
    assert n2 == 0
    req_b = rws.ledger.request(request_id_b)
    assert req_b.status == "failed"
    assert req_b.result == {"reason": "no_approval"}


# ---------------------------------------------------------------------------
# 3. not fast-forward
# ---------------------------------------------------------------------------

def test_release_advance_master_rejects_non_fast_forward(rws: ReleaseWorkspace) -> None:
    attempt_id = _open(rws, "job3")
    sha = _commit(rws.repo, "app.py", "v2\n", "advance")
    # master diverges with its own unrelated commit instead of staying
    # an ancestor of sha.
    _commit(rws.master_repo, "master_only.py", "m\n", "master diverges")

    request_id, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)
    grant(rws.ledger, "gamma", request_id, ttl_sec=3600)

    publisher = FakePublisher()
    n = _proc(rws, publisher).run_once("gamma")

    assert n == 0
    req = rws.ledger.request(request_id)
    assert req.status == "failed"
    assert req.result == {"reason": "not_fast_forward"}
    assert publisher.calls == []
    assert release.head_sha(rws.master_repo) != sha


# ---------------------------------------------------------------------------
# 4. master must be fully clean (ANY tracked modification, or any
#    untracked-and-not-ignored file, refuses -- not just paths that
#    overlap what the incoming commits touch, since the deploy command
#    builds directly from this shared working tree).
# ---------------------------------------------------------------------------

def test_release_advance_master_rejects_dirty_tracked_overlap(rws: ReleaseWorkspace) -> None:
    attempt_id = _open(rws, "job4")
    master_before = release.head_sha(rws.master_repo)
    sha = _commit(rws.repo, "app.py", "v2\n", "advance app.py")
    # master's own uncommitted tracked change touches the SAME path.
    (rws.master_repo / "app.py").write_text("local-edit\n", encoding="utf-8")

    request_id, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)
    grant(rws.ledger, "gamma", request_id, ttl_sec=3600)

    publisher = FakePublisher()
    n = _proc(rws, publisher).run_once("gamma")

    assert n == 0
    req = rws.ledger.request(request_id)
    assert req.status == "failed"
    assert req.result == {"reason": "master_not_clean"}
    assert publisher.calls == []
    assert release.head_sha(rws.master_repo) == master_before
    assert (rws.master_repo / "app.py").read_text(encoding="utf-8") == "local-edit\n"


def test_release_advance_master_rejects_unrelated_dirty_tracked_file(
    rws: ReleaseWorkspace,
) -> None:
    """A dirty tracked file master_repo has that the incoming commits do
    NOT touch must still block the fast-forward -- master_is_clean
    requires zero tracked dirt, not just zero overlapping tracked dirt
    (the deploy command builds the whole working tree, not just the
    paths the merge happens to touch)."""
    attempt_id = _open(rws, "job4b")
    master_before = release.head_sha(rws.master_repo)
    sha = _commit(rws.repo, "app.py", "v2\n", "advance app.py only")
    (rws.master_repo / "other.py").write_text("local-edit\n", encoding="utf-8")

    request_id, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)
    grant(rws.ledger, "gamma", request_id, ttl_sec=3600)

    publisher = FakePublisher()
    n = _proc(rws, publisher).run_once("gamma")

    assert n == 0
    req = rws.ledger.request(request_id)
    assert req.status == "failed"
    assert req.result == {"reason": "master_not_clean"}
    assert release.head_sha(rws.master_repo) == master_before


def test_release_advance_master_rejects_untracked_file_in_master(
    rws: ReleaseWorkspace,
) -> None:
    """An untracked-and-not-gitignored file in master_repo also makes
    master_is_clean False -- not just tracked modifications."""
    attempt_id = _open(rws, "job4c")
    master_before = release.head_sha(rws.master_repo)
    sha = _commit(rws.repo, "app.py", "v2\n", "advance app.py")
    (rws.master_repo / "stray.txt").write_text("oops\n", encoding="utf-8")

    request_id, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)
    grant(rws.ledger, "gamma", request_id, ttl_sec=3600)

    publisher = FakePublisher()
    n = _proc(rws, publisher).run_once("gamma")

    assert n == 0
    req = rws.ledger.request(request_id)
    assert req.status == "failed"
    assert req.result == {"reason": "master_not_clean"}
    assert release.head_sha(rws.master_repo) == master_before


def test_release_advance_master_ignores_gitignored_file_in_master(
    rws: ReleaseWorkspace,
) -> None:
    """A gitignored file sitting in master_repo must NOT block the
    fast-forward by itself -- only tracked dirt and untracked-but-not-
    ignored dirt do. First ships a .gitignore to master through the
    normal publish flow (so master's own HEAD -- not just repo's --
    actually has it, and "ignored.txt" is therefore really ignored
    there), then checks a SECOND, unrelated release still succeeds
    with that ignored file sitting untouched in master's tree."""
    attempt_id = _open(rws, "job4d")
    _commit(rws.repo, ".gitignore", "ignored.txt\n", "add gitignore")
    rid0, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)
    grant(rws.ledger, "gamma", rid0, ttl_sec=3600)
    _seed_full_bench(rws, attempt_id)
    _seed_job_certificate(rws, attempt_id)
    assert _proc(rws, FakePublisher()).run_once("gamma") == 1
    assert rws.ledger.request(rid0).status == "done"

    (rws.master_repo / "ignored.txt").write_text("build artifact\n", encoding="utf-8")
    sha = _commit(rws.repo, "app.py", "v2\n", "advance app.py only")
    request_id, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)
    grant(rws.ledger, "gamma", request_id, ttl_sec=3600)
    _seed_full_bench(rws, attempt_id)
    _seed_job_certificate(rws, attempt_id)

    publisher = FakePublisher()
    n = _proc(rws, publisher).run_once("gamma")

    assert n == 1
    assert rws.ledger.request(request_id).status == "done"
    assert release.head_sha(rws.master_repo) == sha
    assert (rws.master_repo / "ignored.txt").read_text(encoding="utf-8") == "build artifact\n"


# ---------------------------------------------------------------------------
# 5. success: master HEAD == sha + publish.recorded, via approve_and_run
# ---------------------------------------------------------------------------

def test_release_approve_and_run_succeeds(rws: ReleaseWorkspace) -> None:
    attempt_id = _open(rws, "job5")
    sha = _commit(rws.repo, "app.py", "v2\n", "advance")
    request_id, summary = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)
    # approve_and_run's own full-suite-bench gate (ensure_full_bench) is
    # exercised separately below -- pre-seed a passing full-scope bench
    # for this exact commit so this test stays focused on the approval/
    # dispatch flow it was written for.
    fp = evidence.fingerprint(rws.repo, rws.config.projects["gamma"].excludes)
    record(rws.ledger, "gamma", attempt_id, "bench", fp, payload={"passed": True, "scope": "full"})
    _seed_job_certificate(rws, attempt_id)

    publisher = FakePublisher()
    fakes = Fakes(publisher, FakeInstaller())
    outcome = release.approve_and_run(
        rws.config, rws.ledger, fakes, "gamma", request_id, summary["owner_text"])

    assert outcome["status"] == "done"
    assert rws.ledger.request(request_id).status == "done"
    assert release.head_sha(rws.master_repo) == sha
    assert len(publisher.calls) == 1
    assert publisher.calls[0].target == "gamma-app"
    assert publisher.calls[0].artifact_hash == sha
    assert rws.ledger.events("gamma", "publish.recorded")


def test_release_approve_and_run_rejects_malformed_owner_text(rws: ReleaseWorkspace) -> None:
    attempt_id = _open(rws, "job5b")
    _commit(rws.repo, "app.py", "v2\n", "advance")
    request_id, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)

    fakes = Fakes(FakePublisher(), FakeInstaller())
    outcome = release.approve_and_run(
        rws.config, rws.ledger, fakes, "gamma", request_id, "please approve this")

    assert outcome["status"] == "rejected"
    assert outcome["reason"] == "owner_text_malformed"
    assert rws.ledger.request(request_id).status == "queued"


def test_release_approve_and_run_rejects_text_for_another_request(
    rws: ReleaseWorkspace,
) -> None:
    attempt_id = _open(rws, "job5c")
    _commit(rws.repo, "app.py", "v2\n", "advance")
    request_id, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)

    fakes = Fakes(FakePublisher(), FakeInstaller())
    outcome = release.approve_and_run(
        rws.config, rws.ledger, fakes, "gamma", request_id, "승인 R-0000000000000000")

    assert outcome["status"] == "rejected"
    assert outcome["reason"] == "owner_text_wrong_request"


# ---------------------------------------------------------------------------
# 6. advance_master crash-resume does not merge twice
# ---------------------------------------------------------------------------

def test_release_advance_master_crash_resume_merges_once(rws: ReleaseWorkspace) -> None:
    attempt_id = _open(rws, "job6")
    sha = _commit(rws.repo, "app.py", "v2\n", "advance")
    request_id, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)
    grant(rws.ledger, "gamma", request_id, ttl_sec=3600)
    _seed_full_bench(rws, attempt_id)
    _seed_job_certificate(rws, attempt_id)

    publisher = FakePublisher()
    proc1 = Processor(rws.config, rws.ledger, Fakes(publisher, FakeInstaller()),
                      faults=Faults(crash_after={"advance_master"}))
    proc1.register_defaults()
    with pytest.raises(CrashInjected):
        proc1.run_once("gamma")
    assert release.head_sha(rws.master_repo) == sha  # run() already happened

    proc2 = _proc(rws, publisher)
    n = proc2.run_once("gamma")

    assert n == 1
    assert rws.ledger.request(request_id).status == "done"
    assert release.head_sha(rws.master_repo) == sha
    assert len(publisher.calls) == 1  # call_publisher ran exactly once total


# ---------------------------------------------------------------------------
# 7. real deploy failure: master stays at sha, never rolled back by the
#    engine itself (the deploy command is responsible for its own
#    rollback of the deployed target -- the engine never touches master
#    again after a failed/unknown publish).
#    NOTE: call_publisher turns ANY PublisherCrashed -- including a
#    clean non-zero deploy exit code -- into
#    StepUnknown("publish_response_lost"), i.e. request status
#    "unknown", never "failed". This keeps call_publisher's
#    crash-safety contract (a response that never arrived must not be
#    re-dispatched from a brand-new request) intact for every publish
#    request, release-tracked or not, so this test asserts that actual
#    behavior rather than a "failed" status.
# ---------------------------------------------------------------------------

def test_release_deploy_failure_leaves_master_at_sha(rws: ReleaseWorkspace, tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(dedent("""\
        import sys
        raise SystemExit(1)
    """), encoding="utf-8")
    shell_publisher = ShellPublisher(
        argv_template=(sys.executable, str(probe)),
        dry_run_argv_template=(sys.executable, str(probe)),
        receipts_dir=rws.root / "home" / "publish",
        dry_run=False,
    )

    attempt_id = _open(rws, "job7")
    sha = _commit(rws.repo, "app.py", "v2\n", "advance")
    request_id, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)
    grant(rws.ledger, "gamma", request_id, ttl_sec=3600)
    _seed_full_bench(rws, attempt_id)
    _seed_job_certificate(rws, attempt_id)

    proc = Processor(rws.config, rws.ledger, Fakes(shell_publisher, FakeInstaller()))
    proc.register_defaults()
    n = proc.run_once("gamma")

    assert n == 0
    req = rws.ledger.request(request_id)
    assert req.status == "unknown"
    assert req.result == {"reason": "publish_response_lost"}
    receipt = shell_publisher.lookup(request_id)
    assert receipt is not None
    assert receipt["status"] == "failed"
    assert receipt["returncode"] == 1
    # advance_master already ran and is never undone by a later step.
    assert release.head_sha(rws.master_repo) == sha


# ---------------------------------------------------------------------------
# release.build_summary / is_fast_forwardable direct checks
# ---------------------------------------------------------------------------

def test_build_summary_lists_commits_and_diff_paths(rws: ReleaseWorkspace) -> None:
    sha = _commit(rws.repo, "app.py", "v2\n", "second commit")
    summary = release.build_summary(rws.repo, rws.master_repo, sha, "gamma-app")

    assert summary["fast_forward_ok"] is True
    assert summary["diff_paths"] == ["app.py"]
    assert [c["sha"] for c in summary["commits"]] == [sha]
    assert summary["commits"][0]["subject"] == "second commit"


# ---------------------------------------------------------------------------
# P1-3: the consumed approval's binding/expiry is re-checked immediately
# before advance_master's own mutation, not only later before dispatch.
# ---------------------------------------------------------------------------

def test_release_advance_master_rechecks_approval_expiry_before_merging(
    rws: ReleaseWorkspace,
) -> None:
    """A crash right after check_approval (journaled 'done'), resumed
    after the granted approval has since expired, must refuse BEFORE
    ever touching master -- not merge first and only reject later at
    call_publisher's own re-check."""
    attempt_id = _open(rws, "job-p13")
    master_before = release.head_sha(rws.master_repo)
    _commit(rws.repo, "app.py", "v2\n", "advance")
    request_id, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)
    grant(rws.ledger, "gamma", request_id, ttl_sec=1)

    publisher = FakePublisher()
    proc1 = Processor(rws.config, rws.ledger, Fakes(publisher, FakeInstaller()),
                      faults=Faults(crash_after={"check_approval"}))
    proc1.register_defaults()
    with pytest.raises(CrashInjected):
        proc1.run_once("gamma")

    time.sleep(1.2)
    proc2 = _proc(rws, publisher)
    n = proc2.run_once("gamma")

    assert n == 0
    req = rws.ledger.request(request_id)
    assert req.status == "failed"
    assert req.result == {"reason": "approval_invalid_before_advance_master"}
    assert publisher.calls == []
    assert release.head_sha(rws.master_repo) == master_before


# ---------------------------------------------------------------------------
# P1-4: a path the incoming commits ADD must not be allowed to silently
# overwrite an untracked or gitignored file already occupying it in
# master.
# ---------------------------------------------------------------------------

def test_release_advance_master_rejects_incoming_path_occupied_by_untracked_file(
    rws: ReleaseWorkspace,
) -> None:
    """The occupying file is ignored only through master's own LOCAL,
    never-shared `.git/info/exclude` (a real git mechanism for a
    private, per-checkout ignore rule the shared repo never sees) --
    so master_is_clean does not already refuse first (an ignored file
    is fine by itself), and the project's own repo can still commit
    that same path normally (its .gitignore, if any, never mentions
    it). This test isolates incoming_path_occupied specifically."""
    attempt_id = _open(rws, "job-p14")
    master_before = release.head_sha(rws.master_repo)
    exclude_path = rws.master_repo / ".git" / "info" / "exclude"
    exclude_path.write_text("newfile.txt\n", encoding="utf-8")

    sha = _commit(rws.repo, "newfile.txt", "incoming content\n", "add newfile")
    (rws.master_repo / "newfile.txt").write_text("local stray content\n", encoding="utf-8")

    request_id, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)
    grant(rws.ledger, "gamma", request_id, ttl_sec=3600)

    publisher = FakePublisher()
    n = _proc(rws, publisher).run_once("gamma")

    assert n == 0
    req = rws.ledger.request(request_id)
    assert req.status == "failed"
    assert req.result == {"reason": "incoming_path_occupied"}
    assert publisher.calls == []
    assert release.head_sha(rws.master_repo) == master_before
    assert (rws.master_repo / "newfile.txt").read_text(
        encoding="utf-8") == "local stray content\n"


def test_call_publisher_rechecks_master_clean_right_before_dispatch(
    rws: ReleaseWorkspace,
) -> None:
    """P1-2: even after advance_master already merged, call_publisher
    re-verifies master is still clean immediately before dispatch -- a
    crash right after advance_master (journaled 'done'), resumed after
    SOMETHING else dirtied master in between, must refuse rather than
    deploy from a now-dirty working tree."""
    attempt_id = _open(rws, "job-p12-dispatch")
    sha = _commit(rws.repo, "app.py", "v2\n", "advance")
    request_id, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)
    grant(rws.ledger, "gamma", request_id, ttl_sec=3600)
    _seed_full_bench(rws, attempt_id)
    _seed_job_certificate(rws, attempt_id)

    publisher = FakePublisher()
    proc1 = Processor(rws.config, rws.ledger, Fakes(publisher, FakeInstaller()),
                      faults=Faults(crash_after={"advance_master"}))
    proc1.register_defaults()
    with pytest.raises(CrashInjected):
        proc1.run_once("gamma")
    assert release.head_sha(rws.master_repo) == sha

    (rws.master_repo / "app.py").write_text("dirtied-after-merge\n", encoding="utf-8")

    proc2 = _proc(rws, publisher)
    n = proc2.run_once("gamma")

    assert n == 0
    req = rws.ledger.request(request_id)
    assert req.status == "failed"
    assert req.result == {"reason": "master_not_clean"}
    assert publisher.calls == []


def test_call_publisher_rechecks_full_bench_right_before_dispatch(
    rws: ReleaseWorkspace,
) -> None:
    """Same re-check-right-before-dispatch reasoning as the master-
    clean/master-advanced checks above: advance_master's own
    full_bench_missing guard is journaled 'done' well before this
    point and never re-run, so evidence invalidated (e.g. an install)
    in the gap between advance_master and call_publisher must not go
    unnoticed -- never dispatch on stale say-so."""
    attempt_id = _open(rws, "job-p15-dispatch")
    sha = _commit(rws.repo, "app.py", "v2\n", "advance")
    request_id, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)
    grant(rws.ledger, "gamma", request_id, ttl_sec=3600)
    _seed_full_bench(rws, attempt_id)
    _seed_job_certificate(rws, attempt_id)

    publisher = FakePublisher()
    proc1 = Processor(rws.config, rws.ledger, Fakes(publisher, FakeInstaller()),
                      faults=Faults(crash_after={"advance_master"}))
    proc1.register_defaults()
    with pytest.raises(CrashInjected):
        proc1.run_once("gamma")
    assert release.head_sha(rws.master_repo) == sha

    invalidate_after(rws.ledger, "gamma", attempt_id, ("bench",))

    proc2 = _proc(rws, publisher)
    n = proc2.run_once("gamma")

    assert n == 0
    req = rws.ledger.request(request_id)
    assert req.status == "failed"
    assert req.result == {"reason": "full_bench_missing"}
    assert publisher.calls == []


# ---------------------------------------------------------------------------
# ensure_full_bench / approve_and_run's full-suite-bench gate: a ship
# must have a FULL-scope passing bench for the exact released
# fingerprint, not merely whatever scope satisfied the earlier commit
# step. Uses the real pytest-based sample app (tests.fixtures.sample_app,
# variant 1) so run_tests actually executes real tests.
# ---------------------------------------------------------------------------

def _rws_with_real_tests(tmp_path: Path) -> ReleaseWorkspace:
    from tests.fixtures.sample_app import make_sample

    sample = make_sample(tmp_path / "repos" / "gamma", 1, Path(sys.executable))
    repo = sample.repo
    master_repo = _clone_shared(repo, tmp_path / "master" / "gamma")
    home = tmp_path / "home"
    secrets = tmp_path / "secrets"
    home.mkdir()
    secrets.mkdir()
    config = EngineConfig(
        ledger_path=tmp_path / "ledger.sqlite", home=home, secrets_dir=secrets,
        projects={"gamma": ProjectConfig(
            "gamma", repo, test_cmd=sample.test_cmd, bench_cases=sample.bench_cases,
            release=ReleaseConfig(master_repo=master_repo, target="gamma-app"),
        )},
        station_owners={}, approval_ttl_sec=3600,
    )
    ledger = Ledger.create(config.ledger_path)
    ledger.register_project("gamma")
    return ReleaseWorkspace(tmp_path, config, ledger, repo, master_repo)


def test_ensure_full_bench_runs_full_suite_after_a_related_only_pass(
    tmp_path: Path,
) -> None:
    rws = _rws_with_real_tests(tmp_path)
    attempt_id = _open(rws, "job-fullbench")
    _commit(rws.repo, "note.txt", "note\n", "advance")
    fp = evidence.fingerprint(rws.repo, rws.config.projects["gamma"].excludes)
    # Simulate the commit step's own three_match having been satisfied
    # by a "related"-only bench (allowed there) -- never "full".
    record(rws.ledger, "gamma", attempt_id, "bench", fp,
          payload={"passed": True, "scope": "related"})

    fakes = Fakes(FakePublisher(), FakeInstaller())
    result = release.ensure_full_bench(rws.config, rws.ledger, fakes, "gamma", attempt_id)

    assert result["ran"] is True
    assert result["passed"] is True
    full_rows = [
        row for row in rws.ledger.evidence(attempt_id, "bench")
        if not row.stale and row.fingerprint == fp and row.payload.get("scope") == "full"
    ]
    assert len(full_rows) == 1
    assert full_rows[0].payload["passed"] is True


def test_ensure_full_bench_is_a_noop_when_latest_bench_is_already_full(
    tmp_path: Path,
) -> None:
    rws = _rws_with_real_tests(tmp_path)
    attempt_id = _open(rws, "job-fullbench-noop")
    _commit(rws.repo, "note.txt", "note\n", "advance")
    fp = evidence.fingerprint(rws.repo, rws.config.projects["gamma"].excludes)
    record(rws.ledger, "gamma", attempt_id, "bench", fp,
          payload={"passed": True, "scope": "full"})

    fakes = Fakes(FakePublisher(), FakeInstaller())
    result = release.ensure_full_bench(rws.config, rws.ledger, fakes, "gamma", attempt_id)

    assert result == {"ran": False, "passed": True, "fp": fp}


def test_full_bench_failure_blocks_approve_and_run_before_any_merge(
    tmp_path: Path,
) -> None:
    rws = _rws_with_real_tests(tmp_path)
    attempt_id = _open(rws, "job-fullbench-fail")
    _commit(rws.repo, "note.txt", "note\n", "advance")
    master_before = release.head_sha(rws.master_repo)
    fp = evidence.fingerprint(rws.repo, rws.config.projects["gamma"].excludes)
    record(rws.ledger, "gamma", attempt_id, "bench", fp,
          payload={"passed": True, "scope": "related"})

    # Force the full suite to fail regardless of the sample app's own
    # (passing) tests.
    failing_project = replace(
        rws.config.projects["gamma"],
        test_cmd=(sys.executable, "-c", "import sys; sys.exit(1)"),
    )
    failing_config = replace(
        rws.config, projects={**rws.config.projects, "gamma": failing_project},
    )

    request_id, summary = release.prepare(failing_config, rws.ledger, "gamma", attempt_id)
    fakes = Fakes(FakePublisher(), FakeInstaller())
    outcome = release.approve_and_run(
        failing_config, rws.ledger, fakes, "gamma", request_id, summary["owner_text"])

    assert outcome["status"] == "rejected"
    assert outcome["reason"] == "full_bench_failed"
    assert outcome["bench"]["passed"] is False
    assert release.head_sha(rws.master_repo) == master_before  # never merged
    assert fakes.publisher.calls == []  # never dispatched


# ---------------------------------------------------------------------------
# A full-bench failure must never leave a window a later queue drain
# (`gb2 skip-ship`'s continue_job, or `gb2 fix`'s run_once) can walk
# into and deploy through -- approve_and_run must grant NOTHING when
# the bench fails, and advance_master must refuse on its own even if
# something else ever granted one anyway.
# ---------------------------------------------------------------------------

def test_full_bench_failure_grants_no_approval_so_a_later_drain_cannot_deploy(
    tmp_path: Path,
) -> None:
    """approve_and_run must call ensure_full_bench BEFORE granting the
    approval: granting first would leave a valid, unused approval
    sitting next to the still-queued publish request after a failed
    full suite, and a LATER queue drain (this test simulates `gb2
    fix`'s own run_once call) would then dispatch it anyway. With
    ensure_full_bench before grant(), no approval is ever created, so
    that later drain's own check_approval step fails it "no_approval"
    exactly like it would with no approval attempt at all -- the
    publisher is never touched and master never advances."""
    rws = _rws_with_real_tests(tmp_path)
    attempt_id = _open(rws, "job-fullbench-no-grant")
    _commit(rws.repo, "note.txt", "note\n", "advance")
    master_before = release.head_sha(rws.master_repo)
    fp = evidence.fingerprint(rws.repo, rws.config.projects["gamma"].excludes)
    record(rws.ledger, "gamma", attempt_id, "bench", fp,
          payload={"passed": True, "scope": "related"})

    failing_project = replace(
        rws.config.projects["gamma"],
        test_cmd=(sys.executable, "-c", "import sys; sys.exit(1)"),
    )
    failing_config = replace(
        rws.config, projects={**rws.config.projects, "gamma": failing_project},
    )

    request_id, summary = release.prepare(failing_config, rws.ledger, "gamma", attempt_id)
    outcome = release.approve_and_run(
        failing_config, rws.ledger, Fakes(FakePublisher(), FakeInstaller()),
        "gamma", request_id, summary["owner_text"])
    assert outcome["status"] == "rejected"
    assert outcome["reason"] == "full_bench_failed"

    # No approval was ever granted for this request -- the bug's exact
    # unused-approval window.
    assert rws.ledger.approvals_for(request_id) == []
    assert rws.ledger.request(request_id).status == "queued"

    # Simulate `gb2 fix`'s own drain: a fresh Processor's run_once over
    # the whole project queue, same call fixup.fix ends up making.
    publisher = FakePublisher()
    proc = Processor(rws.config, rws.ledger, Fakes(publisher, FakeInstaller()))
    proc.register_defaults()
    n = proc.run_once("gamma")

    assert n == 0
    req = rws.ledger.request(request_id)
    assert req.status == "failed"
    assert req.result == {"reason": "no_approval"}
    assert publisher.calls == []
    assert release.head_sha(rws.master_repo) == master_before


def test_advance_master_refuses_full_bench_missing_even_with_a_valid_approval(
    tmp_path: Path,
) -> None:
    """Defense in depth, independent of approve_and_run's own ordering:
    even if an approval exists for a request whose fingerprint has no
    passing full-scope bench at all (the exact state the ordering bug
    used to produce), advance_master itself refuses
    ("full_bench_missing") rather than relying solely on
    ensure_full_bench ever having been called."""
    rws = _rws_with_real_tests(tmp_path)
    attempt_id = _open(rws, "job-fullbench-defense")
    sha = _commit(rws.repo, "note.txt", "note\n", "advance")
    master_before = release.head_sha(rws.master_repo)
    # No bench evidence recorded for this fingerprint at all -- grant()
    # directly, bypassing approve_and_run's ensure_full_bench gate
    # entirely (simulating that gate having been skipped or reverted).
    request_id, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)
    grant(rws.ledger, "gamma", request_id, ttl_sec=3600)

    publisher = FakePublisher()
    n = _proc(rws, publisher).run_once("gamma")

    assert n == 0
    req = rws.ledger.request(request_id)
    assert req.status == "failed"
    assert req.result == {"reason": "full_bench_missing"}
    assert publisher.calls == []
    assert release.head_sha(rws.master_repo) == master_before


# ---------------------------------------------------------------------------
# another change: common release gate -- a job-level certificate
# (engine.release.job_certificate_status) is required before
# advance_master ever merges, independent of a passing full bench.
# ---------------------------------------------------------------------------

def test_job_certificate_status_false_without_any_event(
    rws: ReleaseWorkspace,
) -> None:
    attempt_id = _open(rws, "job-cert-none")
    ok, reason = release.job_certificate_status(
        rws.ledger, "gamma", attempt_id, rws.repo, _current_fp(rws))
    assert ok is False
    assert reason == "release_requires_job_certificate"


def test_job_certificate_status_false_when_latest_acceptance_failed(
    rws: ReleaseWorkspace,
) -> None:
    attempt_id = _open(rws, "job-cert-failed")
    fp = _current_fp(rws)
    _seed_app_security(rws, attempt_id, fp)
    rws.ledger.append(Event(
        project="gamma", kind="acceptance.recorded", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"job": "j", "passed": False, "scenes": [],
                 "full_suite_passed": False, "tree_changed": False,
                 "reason": "scenes_or_suite_failed", "fingerprint": fp},
    ))
    ok, reason = release.job_certificate_status(rws.ledger, "gamma", attempt_id, rws.repo, fp)
    assert ok is False
    assert reason == "release_requires_job_certificate"


def test_job_certificate_status_false_when_app_security_missing(
    rws: ReleaseWorkspace,
) -> None:
    """Acceptance alone, passing, is NOT enough -- app_security must
    also have a passing record (with a fingerprint that, once diffed
    against the release fingerprint, touches nothing outside
    .gongbang/)."""
    attempt_id = _open(rws, "job-cert-nosecurity")
    fp = _current_fp(rws)
    _seed_acceptance(rws, attempt_id, fp)
    ok, reason = release.job_certificate_status(rws.ledger, "gamma", attempt_id, rws.repo, fp)
    assert ok is False
    assert reason == "release_requires_job_certificate"


def test_job_certificate_status_passes_when_only_gongbang_differs(
    rws: ReleaseWorkspace,
) -> None:
    """The handoff-only case this check exists to tolerate: acceptance/
    app_security were recorded against the tree BEFORE engine.
    full_orbit's own handoff commit (write_handoff + _commit_job_trail)
    added .gongbang/<job>/report.md -- the released commit's own
    fingerprint differs from the recorded one by EXACTLY that
    bookkeeping path, nothing else, so this must still pass."""
    attempt_id = _open(rws, "job-cert-gongbang-only")
    _seed_job_certificate(rws, attempt_id)  # bound to the pre-handoff fingerprint
    (rws.repo / ".gongbang" / "job-cert-gongbang-only").mkdir(parents=True)
    _commit(rws.repo, ".gongbang/job-cert-gongbang-only/report.md",
           "# report\n", "gongbang: handoff")
    released_fp = _current_fp(rws)

    ok, reason = release.job_certificate_status(
        rws.ledger, "gamma", attempt_id, rws.repo, released_fp)
    assert ok is True
    assert reason == ""


def test_job_certificate_status_refuses_when_a_non_report_gongbang_file_changes(
    rws: ReleaseWorkspace,
) -> None:
    """The exemption is exactly the two generated report paths
    (report.md, acceptance.json), never the whole .gongbang/ tree: a
    change to this job's own plan.json after acceptance/app_security
    already ran must still refuse, the same as a real source change
    would -- otherwise a later edit to the approved plan, requirements
    or executable probe scripts could ride through on a stale
    certificate."""
    attempt_id = _open(rws, "job-cert-plan-drift")
    _seed_job_certificate(rws, attempt_id)
    (rws.repo / ".gongbang" / "job-cert-plan-drift").mkdir(parents=True)
    _commit(rws.repo, ".gongbang/job-cert-plan-drift/plan.json",
           '{"scenes": []}\n', "plan.json changed after acceptance")
    released_fp = _current_fp(rws)

    ok, reason = release.job_certificate_status(
        rws.ledger, "gamma", attempt_id, rws.repo, released_fp)
    assert ok is False
    assert reason.startswith("release_requires_job_certificate:")
    assert "plan.json" in reason


def test_job_certificate_status_refuses_when_source_changes_outside_gongbang(
    rws: ReleaseWorkspace,
) -> None:
    """A real source change after acceptance/app_security already ran
    (e.g. something committing directly into the app repo) must still
    refuse, with the changed path named in the reason."""
    attempt_id = _open(rws, "job-cert-src-drift")
    _seed_job_certificate(rws, attempt_id)
    _commit(rws.repo, "app.py", "v3-drifted\n", "drift after acceptance")
    released_fp = _current_fp(rws)

    ok, reason = release.job_certificate_status(
        rws.ledger, "gamma", attempt_id, rws.repo, released_fp)
    assert ok is False
    assert reason.startswith("release_requires_job_certificate:")
    assert "app.py" in reason


def test_job_certificate_status_refuses_when_recorded_tree_missing(
    rws: ReleaseWorkspace,
) -> None:
    """A recorded fingerprint that does not resolve as a real tree
    object in the repo (garbage-collected, or simply never a real
    fingerprint) must refuse outright -- never diffed against
    anything, never trusted as a match."""
    attempt_id = _open(rws, "job-cert-missing-tree")
    fake_tree = "0" * 40
    _seed_acceptance(rws, attempt_id, fake_tree)
    _seed_app_security(rws, attempt_id, fake_tree)

    ok, reason = release.job_certificate_status(
        rws.ledger, "gamma", attempt_id, rws.repo, _current_fp(rws))
    assert ok is False
    assert reason == "release_requires_job_certificate"


def test_job_certificate_status_true_after_seeding_both_halves(
    rws: ReleaseWorkspace,
) -> None:
    attempt_id = _open(rws, "job-cert-ok")
    _seed_job_certificate(rws, attempt_id)
    ok, reason = release.job_certificate_status(
        rws.ledger, "gamma", attempt_id, rws.repo, _current_fp(rws))
    assert ok is True
    assert reason == ""


def test_job_certificate_status_finds_evidence_on_an_earlier_attempt_of_the_same_job(
    rws: ReleaseWorkspace,
) -> None:
    """engine.full_orbit._maybe_release_or_done opens a BRAND NEW
    attempt for the release itself, strictly after acceptance/
    app_security already ran and were recorded against an EARLIER
    attempt of the SAME job (same job_name) -- this must still find
    them (job-scoped, not attempt_id-equality-scoped). Builds the two
    job.opened events directly (bypassing engine.steps.open_job.
    open_job's own project-state machine entirely, which requires
    closing the project between opens and is irrelevant to what this
    function reads) -- same job_id/job_name, n=1 and n=2."""
    import engine.ids as ids_mod

    job_id = ids_mod.new_job_id("gamma")
    earlier_attempt_id = ids_mod.attempt_id(job_id, 1)
    release_attempt_id = ids_mod.attempt_id(job_id, 2)
    for n, aid in ((1, earlier_attempt_id), (2, release_attempt_id)):
        rws.ledger.append(Event(
            project="gamma", kind="job.opened", actor_kind="owner", actor_id="owner",
            attempt_id=aid,
            payload={"job_id": job_id, "job_name": "job-cert-multiattempt",
                     "attempt_id": aid, "n": n, "kind": "full", "generation": n,
                     "dirty_paths": [], "base_head": "0" * 40,
                     "copy_links_skipped": [], "copy_git_config_sha256": {}},
        ))
    _seed_job_certificate(rws, earlier_attempt_id)
    fp = _current_fp(rws)

    ok, reason = release.job_certificate_status(
        rws.ledger, "gamma", release_attempt_id, rws.repo, fp)
    assert ok is True
    assert reason == ""


def _config_with_security(rws: ReleaseWorkspace, security: dict, config_path: Path) -> EngineConfig:
    import json
    config_path.write_text(json.dumps({"security": security}), encoding="utf-8")
    return replace(rws.config, config_path=config_path)


def test_job_certificate_status_ignores_policy_when_config_omitted(
    rws: ReleaseWorkspace,
) -> None:
    """Backward compatibility for the one caller (engine.steps.publish)
    not yet updated to pass `config` -- omitting it (the default) must
    keep behaving exactly as before this check existed, even though the
    seeded app_security.recorded event carries no real policy_hash."""
    attempt_id = _open(rws, "job-cert-no-config")
    _seed_job_certificate(rws, attempt_id)
    ok, reason = release.job_certificate_status(
        rws.ledger, "gamma", attempt_id, rws.repo, _current_fp(rws))
    assert ok is True
    assert reason == ""


def test_job_certificate_status_refuses_a_stale_certificate_after_tightening_the_security_policy(
    rws: ReleaseWorkspace, tmp_path: Path,
) -> None:
    """The regression this fix closes: app_security.check() passed
    under allow_no_lockfile=true and recorded its own policy_hash for
    that policy. The repo tree never changes at all, but the owner
    then tightens the config to allow_no_lockfile=false -- a release
    check bound only to the repo fingerprint would still see a
    "passing" certificate and ship. Passing the CURRENT config must
    instead recompute the policy hash fresh and refuse, since the
    certificate speaks for a policy that no longer applies."""
    from engine import app_security

    attempt_id = _open(rws, "job-cert-policy")
    loose_config = _config_with_security(rws, {"allow_no_lockfile": True},
                                         tmp_path / "loose.json")
    fp = _current_fp(rws)
    _seed_acceptance(rws, attempt_id, fp)
    _seed_app_security(rws, attempt_id, fp,
                       policy_hash=app_security.security_policy_hash(loose_config))

    ok, reason = release.job_certificate_status(
        rws.ledger, "gamma", attempt_id, rws.repo, fp, config=loose_config)
    assert ok is True
    assert reason == ""

    strict_config = _config_with_security(rws, {"allow_no_lockfile": False},
                                          tmp_path / "strict.json")
    ok, reason = release.job_certificate_status(
        rws.ledger, "gamma", attempt_id, rws.repo, fp, config=strict_config)
    assert ok is False
    assert reason == "release_requires_job_certificate:security_policy_changed"


def test_advance_master_refuses_without_job_certificate_even_with_full_bench(
    rws: ReleaseWorkspace,
) -> None:
    """The `gb2 run` -> `gb2 ship` path: engine.orbit.continue_job's own
    commit-stage handler calls engine.release.prepare() directly once
    review+bench pass, never running engine.acceptance (that only
    happens inside engine.full_orbit). A passing full bench alone must
    no longer be enough to actually merge into master -- this is the
    scenario the job-level certificate check exists to close."""
    attempt_id = _open(rws, "job-cert-missing")
    sha = _commit(rws.repo, "app.py", "v2\n", "advance")
    master_before = release.head_sha(rws.master_repo)
    request_id, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)
    grant(rws.ledger, "gamma", request_id, ttl_sec=3600)
    _seed_full_bench(rws, attempt_id)
    # Deliberately NOT seeding acceptance -- this is the exact gap.

    publisher = FakePublisher()
    n = _proc(rws, publisher).run_once("gamma")

    assert n == 0
    req = rws.ledger.request(request_id)
    assert req.status == "failed"
    assert req.result == {"reason": "release_requires_job_certificate"}
    assert publisher.calls == []
    assert release.head_sha(rws.master_repo) == master_before
    assert sha != master_before  # the commit really was there to ship


# ---------------------------------------------------------------------------
# engine.release.cancel_pending_publish
# ---------------------------------------------------------------------------

def test_cancel_pending_publish_fails_the_request_and_revokes_its_unused_approval(
    rws: ReleaseWorkspace,
) -> None:
    attempt_id = _open(rws, "job-cancel")
    _commit(rws.repo, "app.py", "v2\n", "advance")
    request_id, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)
    grant(rws.ledger, "gamma", request_id, ttl_sec=3600)

    cancelled = release.cancel_pending_publish(rws.ledger, "gamma", attempt_id, "full_bench_failed")

    assert cancelled == [request_id]
    req = rws.ledger.request(request_id)
    assert req.status == "failed"
    assert req.result == {"reason": "full_bench_failed"}
    approvals = rws.ledger.approvals_for(request_id)
    assert len(approvals) == 1
    assert approvals[0].used_seq is not None  # revoked: a later check() reports "used"

    from engine.approvals import check as approvals_check
    decision = approvals_check(rws.ledger, rws.ledger.request(request_id))
    assert decision.allowed is False
    assert decision.reason == "used"


def test_cancel_pending_publish_is_a_noop_with_nothing_queued(rws: ReleaseWorkspace) -> None:
    attempt_id = _open(rws, "job-cancel-noop")
    assert release.cancel_pending_publish(rws.ledger, "gamma", attempt_id, "x") == []


def test_cancel_pending_publish_leaves_a_running_request_untouched(
    rws: ReleaseWorkspace,
) -> None:
    """A publish request already 'running' (mid-dispatch) must never be
    force-cancelled -- it may already have an external effect that
    only `gb2 resolve`/retry_unknown can reconcile."""
    attempt_id = _open(rws, "job-cancel-running")
    _commit(rws.repo, "app.py", "v2\n", "advance")
    request_id, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)
    rws.ledger.append(Event(
        project="gamma", kind="request.status", actor_kind="processor",
        actor_id="p", attempt_id=attempt_id,
        payload={"request_id": request_id, "status": "running", "result": {}},
    ))

    cancelled = release.cancel_pending_publish(rws.ledger, "gamma", attempt_id, "x")

    assert cancelled == []
    assert rws.ledger.request(request_id).status == "running"


def test_cancel_pending_publish_refuses_while_a_processor_holds_the_lock(
    rws: ReleaseWorkspace,
) -> None:
    """cancel_pending_publish must hold the project lock for its whole
    check-then-cancel sequence, not just the final write -- otherwise a
    caller's own "safe to cancel" check and this function's own read of
    queued_requests could straddle a live processor's dispatch. Proven
    here by holding the lock externally first (as Processor.run_once/
    run_request would) and asserting cancel_pending_publish refuses
    outright (ProcessorBusy, no ledger write) instead of proceeding."""
    from engine.processor import ProcessorBusy

    attempt_id = _open(rws, "job-cancel-busy")
    _commit(rws.repo, "app.py", "v2\n", "advance")
    request_id, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)
    grant(rws.ledger, "gamma", request_id, ttl_sec=3600)

    assert rws.ledger.acquire_lock("gamma", "other-processor") is True
    try:
        with pytest.raises(ProcessorBusy):
            release.cancel_pending_publish(rws.ledger, "gamma", attempt_id, "x")
    finally:
        rws.ledger.release_lock("gamma", "other-processor")

    # refused outright -- the request is untouched, no approval revoked.
    req = rws.ledger.request(request_id)
    assert req.status == "queued"
    approvals = rws.ledger.approvals_for(request_id)
    assert approvals[0].used_seq is None


# ---------------------------------------------------------------------------
# engine.release.cancel_running_publish_before_dispatch
# ---------------------------------------------------------------------------

def test_cancel_running_publish_before_dispatch_cancels_when_never_started(
    rws: ReleaseWorkspace,
) -> None:
    """A request left 'running' strictly before call_publisher's own
    journal entry exists at all (e.g. a KeyboardInterrupt during an
    earlier step like record_source) is safe to cancel outright."""
    attempt_id = _open(rws, "job-cancel-predispatch")
    _commit(rws.repo, "app.py", "v2\n", "advance")
    request_id, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)
    grant(rws.ledger, "gamma", request_id, ttl_sec=3600)
    _seed_full_bench(rws, attempt_id)
    _seed_job_certificate(rws, attempt_id)

    publisher = FakePublisher()
    proc = Processor(rws.config, rws.ledger, Fakes(publisher, FakeInstaller()))
    proc.register_defaults()
    status = proc.run_request("gamma", request_id, stop_after="record_source")
    assert status == "running"
    assert rws.ledger.steps(request_id).get("call_publisher") is None

    cancelled = release.cancel_running_publish_before_dispatch(
        rws.ledger, "gamma", request_id, "cancelled_before_dispatch")

    assert cancelled is True
    req = rws.ledger.request(request_id)
    assert req.status == "failed"
    assert req.result == {"reason": "cancelled_before_dispatch"}
    assert publisher.calls == []
    approvals = rws.ledger.approvals_for(request_id)
    assert len(approvals) == 1
    assert approvals[0].used_seq is not None  # revoked


def test_cancel_running_publish_before_dispatch_refuses_once_call_publisher_started(
    rws: ReleaseWorkspace,
) -> None:
    """Once call_publisher's own journal entry exists at all -- even
    just 'started', the narrow window strictly before its run()
    actually calls publish() -- this must never touch the request;
    the caller has to refuse and point the owner at `gb2 resolve`."""
    attempt_id = _open(rws, "job-cancel-started")
    _commit(rws.repo, "app.py", "v2\n", "advance")
    request_id, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)
    grant(rws.ledger, "gamma", request_id, ttl_sec=3600)
    _seed_full_bench(rws, attempt_id)
    _seed_job_certificate(rws, attempt_id)

    publisher = FakePublisher()
    proc = Processor(rws.config, rws.ledger, Fakes(publisher, FakeInstaller()),
                      faults=Faults(crash_before={"call_publisher"}))
    proc.register_defaults()
    with pytest.raises(CrashInjected):
        proc.run_once("gamma")
    assert rws.ledger.steps(request_id)["call_publisher"].status == "started"
    assert rws.ledger.request(request_id).status == "running"

    cancelled = release.cancel_running_publish_before_dispatch(
        rws.ledger, "gamma", request_id, "cancelled_before_dispatch")

    assert cancelled is False
    assert rws.ledger.request(request_id).status == "running"  # untouched
    assert publisher.calls == []


def test_cancel_running_publish_before_dispatch_refuses_while_a_processor_holds_the_lock(
    rws: ReleaseWorkspace,
) -> None:
    """Same race cancel_pending_publish's own lock-holding test proves,
    for a request already 'running' instead of 'queued': without
    holding the project lock for its whole status/journal-check-then-
    cancel sequence, a live processor could dispatch call_publisher
    (and overwrite this function's own "failed" write with "done")
    between this function's journal read and its cancellation write.
    Proven here the same way: hold the lock externally first, assert
    ProcessorBusy and no ledger write."""
    from engine.processor import ProcessorBusy

    attempt_id = _open(rws, "job-cancel-running-busy")
    _commit(rws.repo, "app.py", "v2\n", "advance")
    request_id, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)
    grant(rws.ledger, "gamma", request_id, ttl_sec=3600)
    _seed_full_bench(rws, attempt_id)
    _seed_job_certificate(rws, attempt_id)

    publisher = FakePublisher()
    proc = Processor(rws.config, rws.ledger, Fakes(publisher, FakeInstaller()))
    proc.register_defaults()
    status = proc.run_request("gamma", request_id, stop_after="record_source")
    assert status == "running"
    assert rws.ledger.steps(request_id).get("call_publisher") is None
    # The approval was already consumed by the earlier, real run_request
    # dispatch above (not by anything this test calls) -- captured here
    # so the busy refusal below can be shown to leave it exactly as is,
    # rather than asserting the wrong baseline (unused).
    used_seq_before = rws.ledger.approvals_for(request_id)[0].used_seq
    assert used_seq_before is not None

    assert rws.ledger.acquire_lock("gamma", "other-processor") is True
    try:
        with pytest.raises(ProcessorBusy):
            release.cancel_running_publish_before_dispatch(
                rws.ledger, "gamma", request_id, "cancelled_before_dispatch")
    finally:
        rws.ledger.release_lock("gamma", "other-processor")

    # refused outright -- the request is untouched, approvals unchanged.
    req = rws.ledger.request(request_id)
    assert req.status == "running"
    approvals = rws.ledger.approvals_for(request_id)
    assert approvals[0].used_seq == used_seq_before


def test_cancel_running_publish_before_dispatch_refuses_a_non_running_request(
    rws: ReleaseWorkspace,
) -> None:
    from engine.processor import CancelError

    attempt_id = _open(rws, "job-cancel-notrunning")
    _commit(rws.repo, "app.py", "v2\n", "advance")
    request_id, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)

    with pytest.raises(CancelError):
        release.cancel_running_publish_before_dispatch(
            rws.ledger, "gamma", request_id, "x")  # still 'queued', not 'running'


def test_ensure_full_bench_reruns_after_evidence_invalidated_instead_of_trusting_old_done_request(
    tmp_path: Path,
) -> None:
    """submit() is deterministic -- a bench request
    for an unchanged {fp, scope: "full"} payload that already ran once
    and passed, then had its evidence invalidated (e.g. a later
    install), must NOT be trusted as still passing just because that
    OLD request's own status is still "done". ensure_full_bench must
    detect this, resubmit with a fresh identity, and actually run the
    full suite again."""
    rws = _rws_with_real_tests(tmp_path)
    attempt_id = _open(rws, "job-regen")
    _commit(rws.repo, "note.txt", "note\n", "advance")
    fakes = Fakes(FakePublisher(), FakeInstaller())

    first = release.ensure_full_bench(rws.config, rws.ledger, fakes, "gamma", attempt_id)
    assert first["ran"] is True
    assert first["passed"] is True
    first_request_id = first["request_id"]
    fp = first["fp"]

    # Simulate the evidence being invalidated afterward (e.g. an
    # install) -- the fingerprint itself is unchanged (no new commit).
    invalidate_after(rws.ledger, "gamma", attempt_id, ("bench",))
    assert release._latest_full_bench_passed(rws.ledger, attempt_id, fp) is False

    second = release.ensure_full_bench(rws.config, rws.ledger, fakes, "gamma", attempt_id)

    assert second["ran"] is True
    assert second["passed"] is True
    assert second["request_id"] != first_request_id  # a genuinely new request
    fresh_rows = [
        row for row in rws.ledger.evidence(attempt_id, "bench")
        if not row.stale and row.fingerprint == fp and row.payload.get("scope") == "full"
    ]
    assert len(fresh_rows) == 1  # the invalidated row stays stale; one fresh row exists
