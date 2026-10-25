"""run_probe_scripts must never give a probe script write access to the
real project repo -- a probe script is an external-API check the
prober worker wrote, never reviewed the way a build worker's own
commit is, so it gets the same disposable-snapshot isolation
run_acceptance's own scenes/full suite already get, not the bench
sandbox's ordinary writable-repo policy. See engine/acceptance.py's
module docstring and run_probe_scripts' own docstring."""
from __future__ import annotations

import hashlib
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
        "print('PROBE OK https://docs.stripe.com/api')\n",
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


def test_probe_evidence_records_url_sha256_and_snapshot_fingerprint(
    workspace: Workspace,
) -> None:
    """Each ProbeEvidence (and its matching probe.executed ledger
    event) must name the target's own url, the sha256 of the exact
    script bytes that ran, and the snapshot fingerprint it ran
    against -- never just a pass/fail signal with nothing to check it
    against later."""
    attempt_id = _open(workspace)
    probe_dir = workspace.alpha / ".gongbang" / "job1" / "probe"
    probe_dir.mkdir(parents=True)
    script_text = "print('PROBE OK')\n"
    (probe_dir / "t1.py").write_text(script_text, encoding="utf-8")

    results = run_probe_scripts(
        workspace.config, workspace.ledger, "alpha", attempt_id, "job1",
        [("stripe", "https://docs.stripe.com/api")],
    )

    assert results[0].url == "https://docs.stripe.com/api"
    assert results[0].sha256 == hashlib.sha256(script_text.encode("utf-8")).hexdigest()
    assert len(results[0].fingerprint) == 40

    events = [e for _seq, e in workspace.ledger.events("alpha", "probe.executed")]
    assert events[0].payload["url"] == "https://docs.stripe.com/api"
    assert events[0].payload["sha256"] == results[0].sha256
    assert events[0].payload["fingerprint"] == results[0].fingerprint


def test_probe_exit_zero_without_the_probe_ok_url_line_is_not_ok(
    workspace: Workspace,
) -> None:
    """A script that exits 0 but never actually says `PROBE OK
    <url>` must not count as having observed its target -- exit 0
    alone (e.g. a no-op `exit 0`, or a script that only prints "PROBE
    OK" for some OTHER target) proves nothing about whether this
    target was ever contacted."""
    attempt_id = _open(workspace)
    probe_dir = workspace.alpha / ".gongbang" / "job1" / "probe"
    probe_dir.mkdir(parents=True)
    (probe_dir / "t1.py").write_text("print('ok, but not the marker')\n", encoding="utf-8")

    results = run_probe_scripts(
        workspace.config, workspace.ledger, "alpha", attempt_id, "job1",
        [("stripe", "https://docs.stripe.com/api")],
    )

    assert results[0].returncode == 0
    assert results[0].ok is False


def test_probe_ok_line_for_a_different_targets_url_is_not_ok(
    workspace: Workspace,
) -> None:
    """A script that prints `PROBE OK` followed by some OTHER target's
    url (not this one's) must not count as having observed THIS
    target."""
    attempt_id = _open(workspace)
    probe_dir = workspace.alpha / ".gongbang" / "job1" / "probe"
    probe_dir.mkdir(parents=True)
    (probe_dir / "t1.py").write_text(
        "print('PROBE OK https://api.slack.com/web')\n", encoding="utf-8",
    )

    results = run_probe_scripts(
        workspace.config, workspace.ledger, "alpha", attempt_id, "job1",
        [("stripe", "https://docs.stripe.com/api")],
    )

    assert results[0].returncode == 0
    assert results[0].ok is False


def test_two_targets_each_get_their_own_snapshot_one_cannot_rewrite_the_others(
    workspace: Workspace,
) -> None:
    """Target 1's script writing into its OWN snapshot (even a file
    that happens to share target 2's probe script's name) must never
    be visible to target 2's run -- each target gets a fresh snapshot
    rebuilt from the real repo's tree, never one shared snapshot every
    target's script runs in, in turn."""
    attempt_id = _open(workspace)
    probe_dir = workspace.alpha / ".gongbang" / "job1" / "probe"
    probe_dir.mkdir(parents=True)
    (probe_dir / "t1.py").write_text(
        "from pathlib import Path\n"
        "Path('t2.py').write_text(\"print('PROBE OK https://api.slack.com/web')\\n\")\n"
        "print('PROBE OK https://docs.stripe.com/api')\n",
        encoding="utf-8",
    )
    (probe_dir / "t2.py").write_text(
        "print('legit t2 script, never patched by t1')\n", encoding="utf-8",
    )

    results = run_probe_scripts(
        workspace.config, workspace.ledger, "alpha", attempt_id, "job1",
        [("stripe", "https://docs.stripe.com/api"), ("slack", "https://api.slack.com/web")],
    )

    assert results[0].ok is True
    # If t1 and t2 shared one snapshot, t1's write would have replaced
    # t2.py with a script that prints "PROBE OK" for t2's own url,
    # making t2 "pass" despite its REAL script never running at all.
    assert results[1].ok is False
    assert "legit t2 script" in results[1].stdout_tail


def test_probe_evidence_missing_script_has_no_sha256_but_has_fingerprint(
    workspace: Workspace,
) -> None:
    attempt_id = _open(workspace)

    results = run_probe_scripts(
        workspace.config, workspace.ledger, "alpha", attempt_id, "job1",
        [("slack", "https://api.slack.com/web")],
    )

    assert results[0].sha256 == ""
    assert results[0].url == "https://api.slack.com/web"
    assert len(results[0].fingerprint) == 40
