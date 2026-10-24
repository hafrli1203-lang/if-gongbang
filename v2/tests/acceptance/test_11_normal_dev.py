"""Acceptance test 11 — normal day-to-day dev requests.

scripts_change, a dependency install, and a test_config_change all run to
completion; afterward, previously recorded review/bench evidence is stale;
the owner's pre-existing dirty edit survives the whole flow untouched.
Also: install crashes at every step boundary recover cleanly with the
installer effect applied exactly once, and an installer response lost
after the effect leaves the request 'unknown' until a lookup-based resume
reconciles it to 'done' without re-applying.
"""
from __future__ import annotations

import json

import pytest

from engine import evidence
from engine.faults import CrashInjected, Faults
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.processor import Fakes, Processor, retry_unknown, submit
from engine.steps import install
from engine.steps.open_job import open_job
from tests.conftest import Workspace


def _processor(workspace: Workspace, holder: str = "p1", faults: Faults | None = None,
                installer: FakeInstaller | None = None) -> Processor:
    fakes = Fakes(publisher=FakePublisher(), installer=installer or FakeInstaller())
    proc = Processor(config=workspace.config, ledger=workspace.ledger, fakes=fakes,
                      faults=faults, holder=holder)
    proc.register("install", install.steps)
    proc.register("scripts_change", install.steps)
    proc.register("test_config_change", install.steps)
    return proc


def test_scripts_dependency_and_test_config_requests_complete(workspace: Workspace) -> None:
    (workspace.alpha / "docs" / "readme.md").write_text("owner edit\n", encoding="utf-8")
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "normal-dev",
                           "full", ("builder",))
    assert workspace.ledger.project("alpha").dirty_paths == ("docs/readme.md",)

    evidence.record(workspace.ledger, "alpha", attempt_id, "review", "fp-before")
    evidence.record(workspace.ledger, "alpha", attempt_id, "bench", "fp-before")

    bench_config = workspace.alpha / "bench.json"
    bench_config.write_text("{}\n", encoding="utf-8")

    proc = _processor(workspace)

    rid_scripts = submit(workspace.ledger, "alpha", attempt_id, "scripts_change", {
        "manifest": "package.json", "change": {"scripts": {"lint": "ruff"}},
    }, "owner")
    assert proc.run_once("alpha") == 1
    assert workspace.ledger.request(rid_scripts).status == "done"

    rid_deps = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "package.json", "change": {"dependencies": {"zod": "3.0.0"}},
    }, "owner")
    assert proc.run_once("alpha") == 1
    assert workspace.ledger.request(rid_deps).status == "done"

    rid_test_config = submit(workspace.ledger, "alpha", attempt_id, "test_config_change", {
        "manifest": "bench.json", "change": {"timeout": 30},
    }, "owner")
    assert proc.run_once("alpha") == 1
    assert workspace.ledger.request(rid_test_config).status == "done"

    manifest = json.loads((workspace.alpha / "package.json").read_text(encoding="utf-8"))
    assert manifest["scripts"] == {"test": "pytest", "lint": "ruff"}
    assert manifest["dependencies"] == {"zod": "3.0.0"}
    assert json.loads(bench_config.read_text(encoding="utf-8")) == {"timeout": 30}

    # a scripts/dependency/test-config change invalidates review and bench
    assert evidence.latest(workspace.ledger, attempt_id, "review") is None
    assert evidence.latest(workspace.ledger, attempt_id, "bench") is None

    # the owner's pre-existing dirty edit was never touched by any install step
    assert (workspace.alpha / "docs" / "readme.md").read_text(encoding="utf-8") == "owner edit\n"


INSTALL_STEP_NAMES = (
    "acquire_project_lock", "snapshot_manifest", "run_installer",
    "verify_manifest", "invalidate_evidence", "record",
)


@pytest.mark.parametrize("crash_step", INSTALL_STEP_NAMES)
@pytest.mark.parametrize("boundary", ["crash_before", "crash_after"])
def test_install_crash_at_each_boundary_recovers(
    workspace: Workspace, crash_step: str, boundary: str,
) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha",
                           f"crash-{boundary}-{crash_step}", "full", ("builder",))
    rid = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "package.json", "change": {"dependencies": {"zod": "1.0.0"}},
    }, "owner")

    installer = FakeInstaller()
    proc1 = _processor(workspace, holder="p1", faults=Faults(**{boundary: {crash_step}}),
                        installer=installer)
    with pytest.raises(CrashInjected):
        proc1.run_once("alpha")
    assert workspace.ledger.request(rid).status == "running"

    proc2 = _processor(workspace, holder="p2", faults=Faults(), installer=installer)
    n = proc2.run_once("alpha")
    assert n == 1
    assert workspace.ledger.request(rid).status == "done"

    # the installer effect happened exactly once, not duplicated by resume
    assert len(installer.applied_keys) == 1
    assert installer.applied_keys[rid] == {"dependencies": {"zod": "1.0.0"}}
    manifest = json.loads((workspace.alpha / "package.json").read_text(encoding="utf-8"))
    assert manifest["dependencies"] == {"zod": "1.0.0"}

    # no duplicated lock rows / dangling holder: the install-local lock is free
    assert workspace.ledger.acquire_lock("install:alpha", "checker")
    workspace.ledger.release_lock("install:alpha", "checker")


def test_installer_fail_after_effect_then_resumes_via_lookup(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "unknown-install",
                           "full", ("builder",))
    rid = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "package.json", "change": {"dependencies": {"left-pad": "1.0.0"}},
    }, "owner")

    installer = FakeInstaller(fail_after_effect=True)
    proc1 = _processor(workspace, holder="p1", installer=installer)
    n1 = proc1.run_once("alpha")
    assert n1 == 0
    assert workspace.ledger.request(rid).status == "unknown"
    assert workspace.ledger.request(rid).result == {"reason": "installer_response_lost"}

    installer.fail_after_effect = False
    retry_unknown(workspace.ledger, rid, "owner")
    proc2 = _processor(workspace, holder="p2", installer=installer)
    n2 = proc2.run_once("alpha")
    assert n2 == 1
    assert workspace.ledger.request(rid).status == "done"
    assert len(installer.applied_keys) == 1  # not re-applied
    manifest = json.loads((workspace.alpha / "package.json").read_text(encoding="utf-8"))
    assert manifest["dependencies"] == {"left-pad": "1.0.0"}
