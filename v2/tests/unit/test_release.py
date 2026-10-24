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
from engine.ledger import Ledger
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
    assert _proc(rws, FakePublisher()).run_once("gamma") == 1
    assert rws.ledger.request(rid0).status == "done"

    (rws.master_repo / "ignored.txt").write_text("build artifact\n", encoding="utf-8")
    sha = _commit(rws.repo, "app.py", "v2\n", "advance app.py only")
    request_id, _ = release.prepare(rws.config, rws.ledger, "gamma", attempt_id)
    grant(rws.ledger, "gamma", request_id, ttl_sec=3600)

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
