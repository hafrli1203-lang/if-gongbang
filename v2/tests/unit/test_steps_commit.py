"""Unit tests for engine.steps.commit: full commit flow via the Processor,
plus direct checks of the git_commit reconcile predicate."""
from __future__ import annotations

import os
import subprocess

from engine.evidence import fingerprint, record
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.faults import CrashInjected, Faults
from engine.ledger import Event
from engine.processor import Fakes, Processor, StepContext, submit
from engine.steps.commit import _reconcile_git_commit
from engine.steps.open_job import open_job
from tests.conftest import Workspace, git


def _proc(workspace: Workspace) -> Processor:
    proc = Processor(workspace.config, workspace.ledger,
                      Fakes(FakePublisher(), FakeInstaller()))
    proc.register_defaults()
    return proc


def _record_matching_evidence(workspace: Workspace, attempt_id: str) -> str:
    fp = fingerprint(workspace.alpha, workspace.config.projects["alpha"].excludes)
    record(workspace.ledger, "alpha", attempt_id, "review", fp, payload={"passed": True})
    record(workspace.ledger, "alpha", attempt_id, "bench", fp, payload={"passed": True})
    return fp


def test_commit_creates_commit_with_attempt_and_fingerprint(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job1",
                          "full", roles=("builder",))
    (workspace.alpha / "src" / "app.py").write_text("print('v2')\n", encoding="utf-8")
    _record_matching_evidence(workspace, attempt_id)

    before = subprocess.run(["git", "-C", str(workspace.alpha), "rev-list", "--count", "HEAD"],
                            check=True, capture_output=True, text=True).stdout.strip()
    rid = submit(workspace.ledger, "alpha", attempt_id, "commit",
                 {"message": "update app"}, "owner")
    proc = _proc(workspace)
    n = proc.run_once("alpha")

    assert n == 1
    assert workspace.ledger.request(rid).status == "done"
    after = subprocess.run(["git", "-C", str(workspace.alpha), "rev-list", "--count", "HEAD"],
                           check=True, capture_output=True, text=True).stdout.strip()
    assert int(after) == int(before) + 1
    message = subprocess.run(["git", "-C", str(workspace.alpha), "log", "-1", "--format=%B"],
                             check=True, capture_output=True, text=True).stdout
    assert f"attempt: {attempt_id}" in message
    assert "fingerprint:" in message
    assert workspace.ledger.events("alpha", "commit.recorded")


def test_commit_fails_when_fingerprints_differ(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job2",
                          "full", roles=("builder",))
    old_fp = fingerprint(workspace.alpha, workspace.config.projects["alpha"].excludes)
    record(workspace.ledger, "alpha", attempt_id, "review", old_fp, payload={"passed": True})
    record(workspace.ledger, "alpha", attempt_id, "bench", old_fp, payload={"passed": True})
    (workspace.alpha / "src" / "app.py").write_text("print('changed-after-review')\n",
                                                     encoding="utf-8")

    rid = submit(workspace.ledger, "alpha", attempt_id, "commit",
                 {"message": "late change"}, "owner")
    proc = _proc(workspace)
    n = proc.run_once("alpha")

    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "fingerprints_differ"}


def test_commit_includes_modified_force_added_ignored_tracked_file(workspace: Workspace) -> None:
    """git_commit's tree must be evidence.range_tree()'s tree, not a
    separately-built `add -A` index: a tracked file that matches
    .gitignore (force-added despite it) is still a tracked path
    (`ls-tree HEAD`), so a later edit to it must still make it into the
    commit."""
    (workspace.alpha / "dist").mkdir()
    (workspace.alpha / "dist" / "ignored_tracked.txt").write_text("v1\n", encoding="utf-8")
    git(workspace.alpha, "add", "-f", "dist/ignored_tracked.txt")
    git(workspace.alpha, "commit", "-q", "-m", "force-add ignored tracked file")

    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job-force-added",
                          "full", roles=("builder",))
    (workspace.alpha / "dist" / "ignored_tracked.txt").write_text("v2\n", encoding="utf-8")
    _record_matching_evidence(workspace, attempt_id)

    rid = submit(workspace.ledger, "alpha", attempt_id, "commit",
                 {"message": "update ignored-but-tracked file"}, "owner")
    n = _proc(workspace).run_once("alpha")

    assert n == 1
    assert workspace.ledger.request(rid).status == "done"
    committed = subprocess.run(
        ["git", "-C", str(workspace.alpha), "show", "HEAD:dist/ignored_tracked.txt"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert committed == "v2\n"


def test_commit_includes_file_matching_git_info_exclude(workspace: Workspace) -> None:
    """range_tree only honors in-tree .gitignore files for untracked paths
    (--exclude-per-directory=.gitignore), never .git/info/exclude or a
    global core.excludesFile — a file that is only hidden via
    .git/info/exclude must still be picked up as untracked and committed."""
    (workspace.alpha / "secret_local.txt").write_text("not actually secret\n", encoding="utf-8")
    exclude_file = workspace.alpha / ".git" / "info" / "exclude"
    exclude_file.write_text(exclude_file.read_text(encoding="utf-8") + "secret_local.txt\n",
                            encoding="utf-8")

    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job-info-exclude",
                          "full", roles=("builder",))
    _record_matching_evidence(workspace, attempt_id)

    rid = submit(workspace.ledger, "alpha", attempt_id, "commit",
                 {"message": "add file hidden only via .git/info/exclude"}, "owner")
    n = _proc(workspace).run_once("alpha")

    assert n == 1
    assert workspace.ledger.request(rid).status == "done"
    committed = subprocess.run(
        ["git", "-C", str(workspace.alpha), "show", "HEAD:secret_local.txt"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert committed == "not actually secret\n"


def test_commit_rejects_head_moved_between_verify_and_commit(workspace: Workspace) -> None:
    """#6: a tree_changed check alone misses an external commit that moves
    HEAD without changing the tree (e.g. an empty commit) -- compute_fingerprint
    records HEAD, and git_commit must re-check it fresh right before
    building anything, refusing (head_moved) rather than committing on top
    of an unexpected parent."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job-head-moved",
                          "full", roles=("builder",))
    _record_matching_evidence(workspace, attempt_id)
    before = subprocess.run(["git", "-C", str(workspace.alpha), "rev-list", "--count", "HEAD"],
                            check=True, capture_output=True, text=True).stdout.strip()
    rid = submit(workspace.ledger, "alpha", attempt_id, "commit", {"message": "msg"}, "owner")

    proc1 = Processor(workspace.config, workspace.ledger, Fakes(FakePublisher(), FakeInstaller()),
                      faults=Faults(crash_after={"verify_three"}))
    proc1.register_defaults()
    try:
        proc1.run_once("alpha")
    except CrashInjected:
        pass

    # HEAD advances externally without changing the tree at all.
    git(workspace.alpha, "commit", "--allow-empty", "-q", "-m", "external commit")

    proc2 = Processor(workspace.config, workspace.ledger, Fakes(FakePublisher(), FakeInstaller()))
    proc2.register_defaults()
    n = proc2.run_once("alpha")

    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "head_moved"}
    after = subprocess.run(["git", "-C", str(workspace.alpha), "rev-list", "--count", "HEAD"],
                           check=True, capture_output=True, text=True).stdout.strip()
    # Only the external empty commit landed; nothing from this step did.
    assert int(after) == int(before) + 1


def test_commit_rejects_tree_changed_between_verify_and_commit(workspace: Workspace) -> None:
    """P0-2: a crash right after verify_three (fp already checked against
    review/bench) followed by a worker edit must not let the resumed
    git_commit silently commit the new, unreviewed tree under the old
    fingerprint. Nothing should be committed."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job-tc",
                          "full", roles=("builder",))
    _record_matching_evidence(workspace, attempt_id)
    before = subprocess.run(["git", "-C", str(workspace.alpha), "rev-list", "--count", "HEAD"],
                            check=True, capture_output=True, text=True).stdout.strip()
    rid = submit(workspace.ledger, "alpha", attempt_id, "commit",
                 {"message": "late edit"}, "owner")

    proc1 = Processor(workspace.config, workspace.ledger, Fakes(FakePublisher(), FakeInstaller()),
                      faults=Faults(crash_after={"verify_three"}))
    proc1.register_defaults()
    try:
        proc1.run_once("alpha")
    except CrashInjected:
        pass

    # A worker edits the repo between the crash and the resume.
    (workspace.alpha / "src" / "app.py").write_text("print('edited-after-crash')\n",
                                                     encoding="utf-8")

    proc2 = Processor(workspace.config, workspace.ledger, Fakes(FakePublisher(), FakeInstaller()))
    proc2.register_defaults()
    n = proc2.run_once("alpha")

    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "tree_changed"}
    after = subprocess.run(["git", "-C", str(workspace.alpha), "rev-list", "--count", "HEAD"],
                           check=True, capture_output=True, text=True).stdout.strip()
    assert int(after) == int(before)


def test_commit_working_index_matches_head_after_commit(workspace: Workspace) -> None:
    """P3: git_commit builds the commit in a temporary index; the repo's
    own index must be synced to the new HEAD afterwards, or `git status`
    shows spurious staged changes."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job-reset",
                          "full", roles=("builder",))
    (workspace.alpha / "src" / "app.py").write_text("print('v2')\n", encoding="utf-8")
    _record_matching_evidence(workspace, attempt_id)
    submit(workspace.ledger, "alpha", attempt_id, "commit", {"message": "m"}, "owner")
    proc = _proc(workspace)
    assert proc.run_once("alpha") == 1

    status = subprocess.run(
        ["git", "-C", str(workspace.alpha), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert status == ""


def test_record_commit_crash_after_leaves_exactly_one_event(workspace: Workspace) -> None:
    """P2-2: a crash right after record_commit's run() (event already
    appended) must not duplicate commit.recorded on resume."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job-dedupe",
                          "full", roles=("builder",))
    (workspace.alpha / "src" / "app.py").write_text("print('dedupe')\n", encoding="utf-8")
    _record_matching_evidence(workspace, attempt_id)
    rid = submit(workspace.ledger, "alpha", attempt_id, "commit",
                 {"message": "m"}, "owner")

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
    assert workspace.ledger.request(rid).status == "done"
    events = [e for _seq, e in workspace.ledger.events("alpha", "commit.recorded")
              if e.payload.get("request_id") == rid]
    assert len(events) == 1


def test_git_commit_reconcile_true_once_head_has_markers(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job3",
                          "full", roles=("builder",))
    fp = _record_matching_evidence(workspace, attempt_id)
    rid = submit(workspace.ledger, "alpha", attempt_id, "commit",
                 {"message": "msg"}, "owner")
    request = workspace.ledger.request(rid)
    ctx = StepContext(config=workspace.config, ledger=workspace.ledger,
                      fakes=Fakes(FakePublisher(), FakeInstaller()),
                      request=request, project="alpha", attempt_id=attempt_id)
    workspace.ledger.append(Event(
        project="alpha", kind="request.step", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"request_id": rid, "step_name": "compute_fingerprint",
                  "status": "done", "payload": {"fp": fp}},
    ))
    assert _reconcile_git_commit(ctx) is False  # HEAD not committed yet

    subprocess.run(
        ["git", "-C", str(workspace.alpha), "commit", "--allow-empty", "-q",
         "-m", f"msg\n\nattempt: {attempt_id}\nfingerprint: {fp}"],
        check=True, capture_output=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
             "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": "/dev/null",
             "PATH": os.environ["PATH"]},
    )
    assert _reconcile_git_commit(ctx) is True
