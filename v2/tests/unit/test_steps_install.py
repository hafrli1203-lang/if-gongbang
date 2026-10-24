"""Unit tests for engine.steps.install: manifest merge, project lock,
installer failure modes, and evidence invalidation."""
from __future__ import annotations

import json

import pytest

from engine import ids
from engine.faults import CrashInjected, Faults
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.installers import InstallFailed, Installer
from engine.processor import Fakes, Processor, submit
from engine.steps import install
from engine.steps.open_job import open_job
from tests.conftest import Workspace


def _processor(workspace: Workspace, holder: str = "p1",
                installer: Installer | None = None,
                faults: Faults | None = None) -> Processor:
    fakes = Fakes(publisher=FakePublisher(), installer=installer or FakeInstaller())
    proc = Processor(config=workspace.config, ledger=workspace.ledger, fakes=fakes,
                      holder=holder, faults=faults)
    proc.register("install", install.steps)
    proc.register("scripts_change", install.steps)
    proc.register("test_config_change", install.steps)
    return proc


def test_install_merges_manifest_and_releases_lock(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "deps",
                           "full", ("builder",))
    rid = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "package.json", "change": {"dependencies": {"zod": "3.0.0"}},
    }, "owner")
    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 1
    assert workspace.ledger.request(rid).status == "done"
    data = json.loads((workspace.alpha / "package.json").read_text(encoding="utf-8"))
    assert data["dependencies"] == {"zod": "3.0.0"}
    assert data["scripts"] == {"test": "pytest"}
    # the install-local lock was released: a different holder can take it
    assert workspace.ledger.acquire_lock("install:alpha", "someone-else")
    workspace.ledger.release_lock("install:alpha", "someone-else")


def test_install_locked_fails_when_held_by_other(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "deps2",
                           "full", ("builder",))
    rid = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "package.json", "change": {"a": 1},
    }, "owner")
    assert workspace.ledger.acquire_lock("install:alpha", "other-holder")
    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "install_locked"}
    workspace.ledger.release_lock("install:alpha", "other-holder")


def test_run_installer_response_lost_marks_unknown(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "deps3",
                           "full", ("builder",))
    rid = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "package.json", "change": {"a": 1},
    }, "owner")
    installer = FakeInstaller(fail_after_effect=True)
    proc = _processor(workspace, installer=installer)
    n = proc.run_once("alpha")
    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "unknown"
    assert req.result == {"reason": "installer_response_lost"}
    # the installer's effect already happened even though the caller never
    # got a response for it
    assert installer.applied(rid)


def test_verify_manifest_mismatch_fails(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "deps4",
                           "full", ("builder",))
    rid = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "package.json", "change": {"a": 1},
    }, "owner")

    class BadInstaller(FakeInstaller):
        def apply(self, key: str, manifest, change: dict[str, object], original: bytes | None = None) -> None:  # type: ignore[override]
            self.applied_keys[key] = change  # recorded as applied, but writes nothing

    proc = _processor(workspace, installer=BadInstaller())
    n = proc.run_once("alpha")
    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "manifest_mismatch"}


def test_invalidate_evidence_marks_review_and_bench_stale(workspace: Workspace) -> None:
    from engine import evidence

    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "deps5",
                           "full", ("builder",))
    evidence.record(workspace.ledger, "alpha", attempt_id, "review", "fp1")
    evidence.record(workspace.ledger, "alpha", attempt_id, "bench", "fp1")
    rid = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "package.json", "change": {"a": 1},
    }, "owner")
    proc = _processor(workspace)
    proc.run_once("alpha")
    assert workspace.ledger.request(rid).status == "done"
    assert evidence.latest(workspace.ledger, attempt_id, "review") is None
    assert evidence.latest(workspace.ledger, attempt_id, "bench") is None


def test_scripts_change_and_test_config_change_use_same_steps(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "deps6",
                           "full", ("builder",))
    rid1 = submit(workspace.ledger, "alpha", attempt_id, "scripts_change", {
        "manifest": "package.json", "change": {"scripts": {"lint": "ruff"}},
    }, "owner")
    proc = _processor(workspace)
    assert proc.run_once("alpha") == 1
    assert workspace.ledger.request(rid1).status == "done"

    (workspace.alpha / "bench.json").write_text("{}\n", encoding="utf-8")
    rid2 = submit(workspace.ledger, "alpha", attempt_id, "test_config_change", {
        "manifest": "bench.json", "change": {"timeout": 30},
    }, "owner")
    assert proc.run_once("alpha") == 1
    assert workspace.ledger.request(rid2).status == "done"
    bench = json.loads((workspace.alpha / "bench.json").read_text(encoding="utf-8"))
    assert bench == {"timeout": 30}


# ---- P1-6: invalidate_evidence must re-run on resume, not be skipped -----

def test_invalidate_evidence_crash_before_still_invalidates_on_resume(workspace: Workspace) -> None:
    from engine import evidence

    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "deps7",
                           "full", ("builder",))
    evidence.record(workspace.ledger, "alpha", attempt_id, "review", "fp1")
    rid = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "package.json", "change": {"a": 1},
    }, "owner")

    proc1 = _processor(workspace, holder="p1",
                        faults=Faults(crash_before={"invalidate_evidence"}))
    with pytest.raises(CrashInjected):
        proc1.run_once("alpha")
    assert workspace.ledger.request(rid).status == "running"
    # crash fired before invalidate_evidence ran: review is not yet stale
    assert evidence.latest(workspace.ledger, attempt_id, "review") is not None

    proc2 = _processor(workspace, holder="p2")
    n = proc2.run_once("alpha")
    assert n == 1
    assert workspace.ledger.request(rid).status == "done"
    assert evidence.latest(workspace.ledger, attempt_id, "review") is None


# ---- P1-7: a failed step must not leak the install-local lock forever ----

def test_verify_manifest_failure_releases_lock_for_next_install(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "deps8",
                           "full", ("builder",))
    rid1 = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "package.json", "change": {"a": 1},
    }, "owner")

    class BadInstaller(FakeInstaller):
        def apply(self, key: str, manifest, change: dict[str, object], original: bytes | None = None) -> None:  # type: ignore[override]
            self.applied_keys[key] = change  # recorded as applied, but writes nothing

    proc1 = _processor(workspace, installer=BadInstaller())
    n1 = proc1.run_once("alpha")
    assert n1 == 0
    req1 = workspace.ledger.request(rid1)
    assert req1.status == "failed"
    assert req1.result == {"reason": "manifest_mismatch"}

    # a second, independent install request must not be stuck behind the
    # first one's never-released lock
    rid2 = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "package.json", "change": {"b": 2},
    }, "owner")
    proc2 = _processor(workspace)
    n2 = proc2.run_once("alpha")
    assert n2 == 1
    assert workspace.ledger.request(rid2).status == "done"
    data = json.loads((workspace.alpha / "package.json").read_text(encoding="utf-8"))
    assert data["b"] == 2


def test_lock_reclaim_from_a_different_process_pid_succeeds(workspace: Workspace) -> None:
    """#21 remainder: a failed/done holder's lock may have been acquired
    by a different OS process (a different pid) than this one -- e.g. a
    processor that crashed and was restarted under a new pid. Reclaim
    must still work (via force_release_lock), not refuse because the pid
    doesn't match this process's."""
    from engine.ledger import Event

    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "deps11",
                           "full", ("builder",))
    rid1 = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "package.json", "change": {"a": 1},
    }, "owner")
    # simulate: rid1's lock was taken by a different process entirely
    other_pid = 999999
    assert workspace.ledger.acquire_lock("install:alpha", rid1, pid=other_pid)
    # ...and that process crashed after marking the request failed
    workspace.ledger.append(Event(
        project="alpha", kind="request.status", actor_kind="processor",
        actor_id="other-process", attempt_id=attempt_id,
        payload={"request_id": rid1, "status": "failed", "result": {"reason": "crashed"}},
    ))
    assert workspace.ledger.request(rid1).status == "failed"

    rid2 = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "package.json", "change": {"b": 2},
    }, "owner")
    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 1
    assert workspace.ledger.request(rid2).status == "done"
    data = json.loads((workspace.alpha / "package.json").read_text(encoding="utf-8"))
    assert data["b"] == 2


def test_lock_held_by_running_request_is_not_reclaimed(workspace: Workspace) -> None:
    """A lock held by a request that is genuinely still running (or by a
    holder string that is not a known request_id at all) must NOT be
    reclaimed -- only failed/done holders are reclaimable."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "deps9",
                           "full", ("builder",))
    rid = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "package.json", "change": {"a": 1},
    }, "owner")
    assert workspace.ledger.acquire_lock("install:alpha", "some-other-holder")

    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "install_locked"}
    # the other holder's lock is untouched
    assert workspace.ledger.lock_holder("install:alpha") == "some-other-holder"
    workspace.ledger.release_lock("install:alpha", "some-other-holder")


# ---- P2-1: `manifest` path is validated like merge's paths ---------------

@pytest.mark.parametrize("bad_manifest", ["../outside.json", "/etc/passwd", ".git/config"])
def test_manifest_path_outside_repo_is_rejected(workspace: Workspace, bad_manifest: str) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "deps10",
                           "full", ("builder",))
    rid = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": bad_manifest, "change": {"a": 1},
    }, "owner")
    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "path_rejected"}
    # the lock was not leaked by the rejected request either: a later,
    # valid install request can still reclaim and proceed
    rid2 = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "package.json", "change": {"c": 3},
    }, "owner")
    proc2 = _processor(workspace)
    assert proc2.run_once("alpha") == 1
    assert workspace.ledger.request(rid2).status == "done"


# ---- B-12: refuse to install over the owner's uncommitted manifest edit --

def test_install_on_dirty_manifest_is_refused_owner_value_intact(workspace: Workspace) -> None:
    (workspace.alpha / "package.json").write_text(
        '{"name": "alpha", "scripts": {"test": "pytest"}, "owner_edit": true}\n',
        encoding="utf-8",
    )
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "dirty-manifest",
                           "full", ("builder",))
    assert "package.json" in workspace.ledger.project("alpha").dirty_paths

    rid = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "package.json", "change": {"dependencies": {"zod": "1.0.0"}},
    }, "owner")
    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "manifest_dirty"}
    data = json.loads((workspace.alpha / "package.json").read_text(encoding="utf-8"))
    assert data == {"name": "alpha", "scripts": {"test": "pytest"}, "owner_edit": True}
    # the install-local lock was not leaked by this refusal either: a later
    # install on a different (clean) manifest can still reclaim and proceed
    (workspace.alpha / "bench.json").write_text("{}\n", encoding="utf-8")
    rid2 = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "bench.json", "change": {"timeout": 10},
    }, "owner")
    proc2 = _processor(workspace)
    assert proc2.run_once("alpha") == 1
    assert workspace.ledger.request(rid2).status == "done"


def test_install_snapshot_saves_manifest_bytes(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "snap1",
                           "full", ("builder",))
    rid = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "package.json", "change": {"dependencies": {"zod": "1.0.0"}},
    }, "owner")
    proc = _processor(workspace)
    assert proc.run_once("alpha") == 1
    assert workspace.ledger.request(rid).status == "done"

    snapshot_path = (workspace.config.home / "jobs" / ids.attempt_dirname(attempt_id)
                     / "snapshots" / f"{rid}-manifest")
    assert snapshot_path.exists()
    before = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert before == {"name": "alpha", "scripts": {"test": "pytest"}}


# ---- #12 remainder: normalize `manifest` before the dirty-path check ----

def test_install_on_dirty_manifest_with_dotslash_prefix_is_still_refused(
    workspace: Workspace,
) -> None:
    """dirty_paths stores "package.json" (no "./"); a request spelling the
    same file as "./package.json" must still be recognized as dirty, not
    slip past a raw string compare."""
    (workspace.alpha / "package.json").write_text(
        '{"name": "alpha", "scripts": {"test": "pytest"}, "owner_edit": true}\n',
        encoding="utf-8",
    )
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "dirty-dotslash",
                           "full", ("builder",))
    assert "package.json" in workspace.ledger.project("alpha").dirty_paths

    rid = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "./package.json", "change": {"dependencies": {"zod": "1.0.0"}},
    }, "owner")
    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "manifest_dirty"}
    data = json.loads((workspace.alpha / "package.json").read_text(encoding="utf-8"))
    assert data == {"name": "alpha", "scripts": {"test": "pytest"}, "owner_edit": True}


def test_install_manifest_dotslash_prefix_matches_plain_manifest(workspace: Workspace) -> None:
    """A normal (non-dirty) install spelled with a "./" prefix behaves
    exactly like the plain spelling: same manifest file, one effect."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "dotslash-ok",
                           "full", ("builder",))
    rid = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "./package.json", "change": {"dependencies": {"zod": "2.0.0"}},
    }, "owner")
    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 1
    assert workspace.ledger.request(rid).status == "done"
    data = json.loads((workspace.alpha / "package.json").read_text(encoding="utf-8"))
    assert data["dependencies"] == {"zod": "2.0.0"}


class _AlwaysFailsInstaller:
    """Installer stub that always raises InstallFailed, simulating a real
    install_cmd that exits non-zero."""

    def apply(self, key: str, manifest, change, original: bytes | None = None) -> None:
        raise InstallFailed("install_failed:3")

    def applied(self, key: str) -> bool:
        return False


def test_install_failed_marks_request_failed(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "deps-fail",
                           "full", ("builder",))
    rid = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "package.json", "change": {"a": 1},
    }, "owner")
    proc = _processor(workspace, installer=_AlwaysFailsInstaller())
    n = proc.run_once("alpha")
    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "install_failed"}


def test_configured_manifest_allowed_even_when_worker_protected(workspace: Workspace) -> None:
    """protected_paths keeps workers off the manifest; the processor's own
    install request for the CONFIGURED manifest must still be allowed,
    while any other protected file stays rejected."""
    from dataclasses import replace
    from engine.processor import StepFailed
    from engine.steps import install as install_mod
    project = replace(workspace.config.projects["alpha"], manifest="package.json")
    config = replace(workspace.config,
                     projects={**workspace.config.projects, "alpha": project},
                     protected_paths=(*workspace.config.protected_paths, "package.json"))

    class _Req:
        payload = {"manifest": "package.json", "change": {}}
    class _Ctx:
        pass
    ctx = _Ctx(); ctx.config = config; ctx.ledger = workspace.ledger
    ctx.project = "alpha"; ctx.request = _Req()
    assert install_mod._manifest_path(ctx) == workspace.alpha / "package.json"
    ctx.request.payload = {"manifest": "package-lock.json", "change": {}}
    try:
        install_mod._manifest_path(ctx)
    except StepFailed as exc:
        assert exc.reason == "path_rejected"
    else:
        raise AssertionError("expected path_rejected")


# ---- P1: invalidate_evidence runs before run_installer, not after -------

def test_invalidate_evidence_step_precedes_run_installer_in_step_list(
    workspace: Workspace,
) -> None:
    """A failed install_cmd may already have changed .deps/environment
    before failing even though the manifest itself rolls back -- stale
    review/bench evidence must be invalidated regardless of whether the
    install succeeds, so invalidate_evidence must run before (not after)
    run_installer in the fixed step list."""
    from engine.processor import StepContext as _StepContext
    names = [step.name for step in install.steps(_StepContext(
        config=workspace.config, ledger=workspace.ledger,
        fakes=Fakes(FakePublisher(), FakeInstaller()), request=None,  # type: ignore[arg-type]
        project="alpha", attempt_id="unused",
    ))]
    assert names.index("invalidate_evidence") < names.index("run_installer")


def test_evidence_invalidated_even_when_install_cmd_fails(workspace: Workspace) -> None:
    from engine import evidence

    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "deps12",
                           "full", ("builder",))
    evidence.record(workspace.ledger, "alpha", attempt_id, "review", "fp1")
    evidence.record(workspace.ledger, "alpha", attempt_id, "bench", "fp1")
    rid = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "package.json", "change": {"a": 1},
    }, "owner")

    class _AlwaysFails(FakeInstaller):
        def apply(self, key: str, manifest, change, original: bytes | None = None) -> None:  # type: ignore[override]
            raise InstallFailed("install_failed:3")

    proc = _processor(workspace, installer=_AlwaysFails())
    n = proc.run_once("alpha")
    assert n == 0
    assert workspace.ledger.request(rid).status == "failed"
    # The evidence from BEFORE this install is stale regardless of the
    # install_cmd's own failure -- the dependency environment may have
    # partially changed before it failed.
    assert evidence.latest(workspace.ledger, attempt_id, "review") is None
    assert evidence.latest(workspace.ledger, attempt_id, "bench") is None


# ---- P1: manifest exemption only drops entries naming the manifest ------

def test_manifest_exemption_keeps_other_protected_paths_protected(
    workspace: Workspace,
) -> None:
    from dataclasses import replace as _replace
    from engine.processor import StepFailed
    from engine.steps import install as install_mod

    project = _replace(workspace.config.projects["alpha"], manifest="package.json")
    config = _replace(
        workspace.config,
        projects={**workspace.config.projects, "alpha": project},
        protected_paths=(*workspace.config.protected_paths, "package.json", "secret.txt"),
    )

    class _Req:
        payload = {"manifest": "secret.txt", "change": {}}

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.config = config
    ctx.ledger = workspace.ledger
    ctx.project = "alpha"
    ctx.request = _Req()
    # secret.txt is protected and NOT the configured manifest -- the
    # exemption for "package.json" must not leak to it.
    try:
        install_mod._manifest_path(ctx)
    except StepFailed as exc:
        assert exc.reason == "path_rejected"
    else:
        raise AssertionError("expected path_rejected")


def test_manifest_symlink_is_rejected(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "deps13",
                           "full", ("builder",))
    real_target = workspace.alpha / "real_manifest.json"
    real_target.write_text("{}\n", encoding="utf-8")
    link_path = workspace.alpha / "linked.json"
    link_path.symlink_to(real_target)

    rid = submit(workspace.ledger, "alpha", attempt_id, "install", {
        "manifest": "linked.json", "change": {"a": 1},
    }, "owner")
    proc = _processor(workspace)
    n = proc.run_once("alpha")
    assert n == 0
    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "path_rejected"}


def test_configured_manifest_inside_protected_dir_is_installable(workspace: Workspace) -> None:
    from dataclasses import replace
    from engine.steps import install as install_mod
    (workspace.alpha / "deps").mkdir()
    (workspace.alpha / "deps" / "requirements.txt").write_text("", encoding="utf-8")
    project = replace(workspace.config.projects["alpha"], manifest="deps/requirements.txt")
    config = replace(workspace.config,
                     projects={**workspace.config.projects, "alpha": project},
                     protected_paths=(*workspace.config.protected_paths, "deps/"))

    class _Req:
        payload = {"manifest": "deps/requirements.txt", "change": {"add_lines": ["x==1"]}}
    class _Ctx:
        pass
    ctx = _Ctx(); ctx.config = config; ctx.ledger = workspace.ledger
    ctx.project = "alpha"; ctx.request = _Req()
    assert install_mod._manifest_path(ctx) == workspace.alpha / "deps" / "requirements.txt"


def test_lines_manifest_matches_ignores_surrounding_whitespace(tmp_path: Path) -> None:
    from engine.steps.install import _lines_manifest_matches
    manifest = tmp_path / "requirements.txt"
    manifest.write_text("zod>=3,<4\n", encoding="utf-8")
    assert _lines_manifest_matches(manifest, {"add_lines": [" zod>=3,<4 "]}) is True
