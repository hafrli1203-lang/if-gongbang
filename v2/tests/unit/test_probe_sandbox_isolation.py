"""run_probe_scripts must never give a probe script write access to the
real project repo -- a probe script is an external-API check the
prober worker wrote, never reviewed the way a build worker's own
commit is, so it gets the same disposable-snapshot isolation
run_acceptance's own scenes/full suite already get, not the bench
sandbox's ordinary writable-repo policy. See engine/acceptance.py's
module docstring and run_probe_scripts' own docstring."""
from __future__ import annotations

from dataclasses import replace

import pytest

from engine import sandbox
from engine.acceptance import run_probe_scripts
from engine.steps.open_job import open_job
from tests.conftest import Workspace

_needs_bwrap = pytest.mark.skipif(
    not sandbox.available(), reason="bwrap not available on this host",
)


def _open(workspace: Workspace, job_name: str = "job1") -> str:
    return open_job(workspace.config, workspace.ledger, "alpha", job_name,
                    "full", roles=("builder",))


def test_probe_writing_a_relative_path_lands_in_the_snapshot_not_the_repo(
    workspace: Workspace,
) -> None:
    """Even under sandbox="none" (no bwrap mount involved at all), the
    probe script's cwd must be the disposable snapshot, never the real
    repo -- a relative-path write from the script must never reach the
    real repo's working tree."""
    attempt_id = _open(workspace)
    probe_dir = workspace.alpha / ".gongbang" / "job1" / "probe"
    probe_dir.mkdir(parents=True)
    (probe_dir / "t1.py").write_text(
        "from pathlib import Path\n"
        "Path('pwned.txt').write_text('pwned')\n"
        "print('PROBE OK')\n",
        encoding="utf-8",
    )

    results = run_probe_scripts(
        workspace.config, workspace.ledger, "alpha", attempt_id, "job1",
        [("stripe", "https://docs.stripe.com/api")],
    )

    assert results[0].ok is True
    assert not (workspace.alpha / "pwned.txt").exists()


@_needs_bwrap
def test_probe_cannot_write_into_the_real_repo_by_absolute_path_under_bwrap(
    workspace: Workspace,
) -> None:
    """Under sandbox="bwrap", the real repo is mounted into the probe's
    sandbox only via run_probe_scripts' own extra_readonly=(repo,) --
    read-only, never writable (unlike the ordinary bench sandbox policy,
    which always mounts its own `repo` argument writable). A probe
    script that targets the real repo's ABSOLUTE path directly (not
    just a relative path resolved against a different cwd) must still
    fail to write there."""
    config = replace(workspace.config, sandbox="bwrap")
    attempt_id = open_job(config, workspace.ledger, "alpha", "job1", "full",
                          roles=("builder",))
    probe_dir = workspace.alpha / ".gongbang" / "job1" / "probe"
    probe_dir.mkdir(parents=True)
    target = workspace.alpha / "pwned.txt"
    (probe_dir / "t1.py").write_text(
        "from pathlib import Path\n"
        "try:\n"
        f"    Path({str(target)!r}).write_text('pwned')\n"
        "except OSError as exc:\n"
        "    print(f'WRITE_BLOCKED:{exc}')\n"
        "else:\n"
        "    print('WRITE_SUCCEEDED')\n",
        encoding="utf-8",
    )

    results = run_probe_scripts(
        config, workspace.ledger, "alpha", attempt_id, "job1",
        [("stripe", "https://docs.stripe.com/api")],
    )

    assert "WRITE_BLOCKED" in results[0].stdout_tail
    assert "WRITE_SUCCEEDED" not in results[0].stdout_tail
    assert not target.exists()
