"""Acceptance 06: crash at every step boundary of close (8) and commit (4)
is recoverable by a fresh Processor, with no duplicate archive directory,
no duplicate commit, and the tree back at HEAD."""
from __future__ import annotations

import subprocess

import pytest

from engine import ids
from engine.evidence import fingerprint, record
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.faults import CrashInjected, Faults
from engine.processor import Fakes, Processor, submit
from engine.steps.open_job import open_job
from tests.conftest import Workspace

CLOSE_STEPS = (
    "set_closing", "archive_tmp", "archive_commit", "reset_to_head",
    "apply_deferred_approvals", "verify_tree_eq_head", "set_closed", "open_queue",
)
COMMIT_STEPS = ("compute_fingerprint", "verify_three", "git_commit", "record_commit")


def _rev_count(repo) -> int:
    return int(subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip())


def _started_step_counts(workspace: Workspace, request_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _seq, event in workspace.ledger.events("alpha", "request.step"):
        if event.payload["request_id"] != request_id:
            continue
        if event.payload["status"] != "started":
            continue
        name = event.payload["step_name"]
        counts[name] = counts.get(name, 0) + 1
    return counts


@pytest.mark.parametrize("crash_step", CLOSE_STEPS)
def test_close_crash_at_every_boundary_resumes_cleanly(workspace: Workspace, crash_step: str) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", f"job-{crash_step}",
                          "full", roles=("builder",))
    (workspace.alpha / "src" / "app.py").write_text("print('close-change')\n", encoding="utf-8")
    rid = submit(workspace.ledger, "alpha", attempt_id, "close", {"mode": "close"}, "owner")

    proc1 = Processor(workspace.config, workspace.ledger, Fakes(FakePublisher(), FakeInstaller()),
                      faults=Faults(crash_after={crash_step}))
    proc1.register_defaults()
    with pytest.raises(CrashInjected):
        proc1.run_once("alpha")

    proc2 = Processor(workspace.config, workspace.ledger, Fakes(FakePublisher(), FakeInstaller()),
                      faults=Faults())
    proc2.register_defaults()
    n = proc2.run_once("alpha")
    assert n == 1

    assert workspace.ledger.request(rid).status == "done"
    assert workspace.ledger.project("alpha").state == "CLOSED"

    archive_root = workspace.config.home / "archive" / "alpha" / ids.attempt_dirname(attempt_id)
    final_dirs = [p for p in archive_root.iterdir() if not p.name.startswith(".tmp-")]
    assert len(final_dirs) == 1
    assert (final_dirs[0] / "src" / "app.py").read_text(encoding="utf-8") == "print('close-change')\n"

    head_tree = subprocess.run(
        ["git", "-C", str(workspace.alpha), "rev-parse", "HEAD^{tree}"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    now_tree = fingerprint(workspace.alpha, workspace.config.projects["alpha"].excludes)
    assert now_tree == head_tree

    counts = _started_step_counts(workspace, rid)
    for step_name, count in counts.items():
        assert count <= 2, f"{step_name} started {count} times"


@pytest.mark.parametrize("crash_step", COMMIT_STEPS)
def test_commit_crash_at_every_boundary_resumes_cleanly(workspace: Workspace, crash_step: str) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", f"cjob-{crash_step}",
                          "full", roles=("builder",))
    (workspace.alpha / "src" / "app.py").write_text("print('commit-change')\n", encoding="utf-8")
    fp = fingerprint(workspace.alpha, workspace.config.projects["alpha"].excludes)
    record(workspace.ledger, "alpha", attempt_id, "review", fp, payload={"passed": True})
    record(workspace.ledger, "alpha", attempt_id, "bench", fp, payload={"passed": True})

    before = _rev_count(workspace.alpha)
    rid = submit(workspace.ledger, "alpha", attempt_id, "commit",
                 {"message": "crash-test"}, "owner")

    proc1 = Processor(workspace.config, workspace.ledger, Fakes(FakePublisher(), FakeInstaller()),
                      faults=Faults(crash_after={crash_step}))
    proc1.register_defaults()
    with pytest.raises(CrashInjected):
        proc1.run_once("alpha")

    proc2 = Processor(workspace.config, workspace.ledger, Fakes(FakePublisher(), FakeInstaller()),
                      faults=Faults())
    proc2.register_defaults()
    n = proc2.run_once("alpha")
    assert n == 1

    assert workspace.ledger.request(rid).status == "done"
    after = _rev_count(workspace.alpha)
    assert after == before + 1

    counts = _started_step_counts(workspace, rid)
    for step_name, count in counts.items():
        assert count <= 2, f"{step_name} started {count} times"
