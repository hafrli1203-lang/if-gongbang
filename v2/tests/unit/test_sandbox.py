"""Unit tests for engine.sandbox: mode="none" argv passthrough, bwrap
argv construction, and (when bwrap is actually available on this host)
real sandboxed execution -- writable/hidden paths, net isolation, and
full run_station/bench integrations through the real engine.gate."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine import ids, sandbox
from engine.config import WorkerConfig
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.processor import Fakes, Processor
from engine.processor import submit as submit_request
from engine.steps import bench_run, review, station
from engine.steps.open_job import copy_root, open_job
from engine.workers.base import WorkerSpec
from engine.workers.claude_cli import ClaudeRunner
from engine.workers.codex_cli import CodexRunner
from engine.workers.scripted import ScriptedRunner
from tests.conftest import Workspace
from tests.fixtures.sample_app import make_sample

_needs_bwrap = pytest.mark.skipif(
    not sandbox.available(), reason="bwrap not available on this host",
)


# ---------------------------------------------------------------------------
# mode="none": pure passthrough, no bwrap involvement at all.
# ---------------------------------------------------------------------------

def test_mode_none_returns_argv_unchanged() -> None:
    argv = ["echo", "hi"]
    result = sandbox.wrap(argv, cwd=Path("/tmp"), mode="none")
    assert result == argv
    assert result is not argv  # wrap() must not hand back the caller's own list


def test_mode_none_ignores_writable_and_hidden() -> None:
    result = sandbox.wrap(
        ["true"], cwd=Path("/tmp"), writable=(Path("/x"),),
        hidden=(Path("/y"),), net=True, mode="none",
    )
    assert result == ["true"]


def test_unknown_mode_rejected() -> None:
    with pytest.raises(ValueError):
        sandbox.wrap(["true"], cwd=Path("/tmp"), mode="weird")


# ---------------------------------------------------------------------------
# bwrap argv construction: deterministic, tmpfs/hide before binds.
# ---------------------------------------------------------------------------

@_needs_bwrap
def test_bwrap_argv_is_deterministic(tmp_path: Path) -> None:
    writable = (tmp_path / "w",)
    hidden = (tmp_path / "h",)
    writable[0].mkdir()
    hidden[0].mkdir()
    a = sandbox.wrap(["echo", "x"], cwd=tmp_path, writable=writable,
                     hidden=hidden, net=False, mode="bwrap")
    b = sandbox.wrap(["echo", "x"], cwd=tmp_path, writable=writable,
                     hidden=hidden, net=False, mode="bwrap")
    assert a == b


@_needs_bwrap
def test_bwrap_ancestor_writable_mounted_before_descendant_hidden(tmp_path: Path) -> None:
    """The P0 regression this module exists to prevent: a writable path
    that is an ANCESTOR of a hidden one (e.g. ledger_path.parent == the
    engine's base dir, home to a hidden `home`/`secrets_dir`) must be
    mounted BEFORE the hidden descendant, not after -- grouping by kind
    (hidden-first, writable-second, as an earlier version of wrap() did)
    put the ancestor's --bind last, which re-exposed everything the
    --tmpfs on its child had just hidden."""
    base = tmp_path
    home = base / "home"
    home.mkdir()
    argv = sandbox.wrap(
        ["true"], cwd=base, writable=(base,), hidden=(home,), mode="bwrap",
    )
    base_idx = argv.index(str(base))
    home_idx = argv.index(str(home))
    assert argv[base_idx - 1] == "--bind"
    assert argv[home_idx - 1] == "--tmpfs"
    assert base_idx < home_idx


@_needs_bwrap
def test_bwrap_argv_puts_hidden_before_writable(tmp_path: Path) -> None:
    home = tmp_path / "home"
    copy = home / "copies" / "a1" / "builder"
    copy.mkdir(parents=True)
    argv = sandbox.wrap(
        ["echo"], cwd=copy, writable=(copy,), hidden=(home,),
        net=False, mode="bwrap",
    )
    # The --tmpfs for `home` and the --bind for `copy` (nested under it)
    # must appear in that order, or `copy` would come back hidden too.
    home_idx = argv.index(str(home))
    bind_idx = argv.index(str(copy))
    assert argv[home_idx - 1] == "--tmpfs"
    assert argv[bind_idx - 1] == "--bind"
    assert home_idx < bind_idx


@_needs_bwrap
def test_bwrap_argv_has_die_with_parent_and_chdir(tmp_path: Path) -> None:
    argv = sandbox.wrap(["true"], cwd=tmp_path, mode="bwrap")
    assert "--die-with-parent" in argv
    assert argv[argv.index("--chdir") + 1] == str(tmp_path)
    assert argv[-1] == "true"


@_needs_bwrap
def test_bwrap_net_flag(tmp_path: Path) -> None:
    with_net = sandbox.wrap(["true"], cwd=tmp_path, net=True, mode="bwrap")
    without_net = sandbox.wrap(["true"], cwd=tmp_path, net=False, mode="bwrap")
    assert "--share-net" in with_net
    assert "--share-net" not in without_net


@_needs_bwrap
def test_same_path_hidden_and_writable_is_a_conflict(tmp_path: Path) -> None:
    """A path requested as both hidden and writable/readonly is a direct
    contradiction (equal depth, so a stable sort's tie-break order is an
    implementation detail -- never something to rely on to decide
    whether it ends up hidden or exposed)."""
    shared = tmp_path / "shared"
    shared.mkdir()
    with pytest.raises(ValueError, match="mount conflict"):
        sandbox.wrap(["true"], cwd=tmp_path, hidden=(shared,),
                     writable=(shared,), mode="bwrap")
    with pytest.raises(ValueError, match="mount conflict"):
        sandbox.wrap(["true"], cwd=tmp_path, hidden=(shared,),
                     readonly=(shared,), mode="bwrap")


def test_bwrap_mode_without_bwrap_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "_available_cache", False)
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.wrap(["true"], cwd=tmp_path, mode="bwrap")


def test_available_caches_result(monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox._reset_available_cache()
    calls = {"n": 0}
    real_which = sandbox.shutil.which

    def counting_which(name: str) -> str | None:
        calls["n"] += 1
        return real_which(name)

    monkeypatch.setattr(sandbox.shutil, "which", counting_which)
    sandbox.available()
    sandbox.available()
    assert calls["n"] == 1
    sandbox._reset_available_cache()


# ---------------------------------------------------------------------------
# Real sandboxed execution: writable vs. hidden vs. outside, net isolation.
# ---------------------------------------------------------------------------

@_needs_bwrap
def test_write_inside_writable_path_succeeds(tmp_path: Path) -> None:
    writable_dir = tmp_path / "writable"
    writable_dir.mkdir()
    target = writable_dir / "out.txt"
    argv = sandbox.wrap(
        ["sh", "-c", f"echo hi > {target}"], cwd=writable_dir,
        writable=(writable_dir,), mode="bwrap",
    )
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert target.read_text() == "hi\n"


@_needs_bwrap
def test_write_outside_writable_path_fails(tmp_path: Path) -> None:
    writable_dir = tmp_path / "writable"
    writable_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    target = outside_dir / "out.txt"
    argv = sandbox.wrap(
        ["sh", "-c", f"echo hi > {target}"], cwd=writable_dir,
        writable=(writable_dir,), mode="bwrap",
    )
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert not target.exists()


@_needs_bwrap
def test_hidden_dir_appears_empty(tmp_path: Path) -> None:
    # `work` is a SIBLING of `hidden_dir`, not its ancestor: a writable
    # rebind of an ANCESTOR of a hidden path would re-expose it (the
    # ancestor's real content, including the hidden child, comes back),
    # so the --chdir target used here must never be `hidden_dir`'s parent.
    work = tmp_path / "work"
    work.mkdir()
    hidden_dir = tmp_path / "hidden"
    hidden_dir.mkdir()
    (hidden_dir / "secret.txt").write_text("s", encoding="utf-8")
    argv = sandbox.wrap(
        ["ls", "-A", str(hidden_dir)], cwd=work,
        writable=(work,), hidden=(hidden_dir,), mode="bwrap",
    )
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert result.stdout.strip() == ""
    # The file is still on the real host, untouched.
    assert (hidden_dir / "secret.txt").read_text() == "s"


@_needs_bwrap
def test_hidden_dir_is_remounted_read_only(tmp_path: Path) -> None:
    """A file that needs to stay unreplaceable can't just be mounted
    read-only by itself inside a directory the worker can write to --
    a worker could `mv` its own request's directory out of the way
    and recreate an empty one at the same pathname with a fake file in
    it, since the attack renames the file's own parent, not the file.
    wrap() remounts every hidden (tmpfs)
    directory read-only right after its nested mounts are in place
    (see the module docstring), which this verifies directly: mkdir and
    mv both fail anywhere inside the hidden tree, several plain-
    directory levels deep, with no explicit mount of their own."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "jobs").mkdir()
    (home / "jobs" / "attempt1").mkdir()
    argv = sandbox.wrap(
        ["sh", "-c",
         f"mkdir {home}/jobs/attempt1/evil 2>/dev/null; echo mkdir=$?; "
         f"mv {home}/jobs/attempt1 {home}/jobs/attempt1-moved 2>/dev/null; echo mv=$?"],
        cwd=tmp_path, hidden=(home,), mode="bwrap",
    )
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert "mkdir=1" in result.stdout
    assert "mv=1" in result.stdout


@_needs_bwrap
def test_gate_ro_dir_cannot_be_renamed_or_written_but_reads_fine(tmp_path: Path) -> None:
    """The actual gate-ro shape: a read-only directory NESTED inside a
    hidden (and now remounted-ro) $HOME, sibling of a writable stations
    dir -- the worker must still be able to read the facts file and
    write into its own stations dir, while gate-ro itself cannot be
    renamed, deleted, or written through, even though both mounts sit
    under the SAME hidden ancestor."""
    home = tmp_path / "home"
    attempt_dir = home / "jobs" / "attempt1"
    stations_dir = attempt_dir / "stations"
    stations_dir.mkdir(parents=True)
    gate_ro_dir = attempt_dir / "gate-ro"
    gate_ro_dir.mkdir()
    (gate_ro_dir / "gate_facts.json").write_text('{"ok": true}', encoding="utf-8")
    argv = sandbox.wrap(
        ["sh", "-c",
         f"cat {gate_ro_dir}/gate_facts.json; "
         f"(echo evil > {gate_ro_dir}/gate_facts.json) 2>/dev/null; "
         f"[ $? -ne 0 ] && echo write=denied || echo write=allowed; "
         f"mv {gate_ro_dir} {gate_ro_dir}-moved 2>/dev/null; echo mv=$?; "
         f"touch {stations_dir}/ok 2>/dev/null; echo stations_write=$?"],
        cwd=tmp_path,
        writable=(stations_dir,),
        readonly=(gate_ro_dir,),
        hidden=(home,), mode="bwrap",
    )
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert '{"ok": true}' in result.stdout
    assert "write=denied" in result.stdout
    assert "mv=1" in result.stdout
    assert "stations_write=0" in result.stdout


@_needs_bwrap
def test_hidden_file_unreadable(tmp_path: Path) -> None:
    # `work` is a SIBLING of `secret`, not its ancestor: rebinding `work`
    # writable (so its own --chdir target exists under the base --tmpfs
    # /tmp) must never re-expose `secret`, which this test is checking
    # stays hidden.
    work = tmp_path / "work"
    work.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("s", encoding="utf-8")
    argv = sandbox.wrap(
        ["cat", str(secret)], cwd=work, writable=(work,), hidden=(secret,),
        mode="bwrap",
    )
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert secret.read_text() == "s"


@_needs_bwrap
def test_readonly_remap_argv_binds_source_to_different_dest(tmp_path: Path) -> None:
    cache = tmp_path / "real_home" / ".cache" / "ms-playwright"
    cache.mkdir(parents=True)
    dest = tmp_path / "sandbox_home" / ".cache" / "ms-playwright"
    argv = sandbox.wrap(
        ["true"], cwd=tmp_path, readonly_remap=((cache, dest),), mode="bwrap",
    )
    bind_idx = argv.index(str(cache))
    assert argv[bind_idx - 1] == "--ro-bind"
    assert argv[bind_idx + 1] == str(dest)


@_needs_bwrap
def test_readonly_remap_skips_nonexistent_source(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    dest = tmp_path / "home" / "x"
    argv = sandbox.wrap(
        ["true"], cwd=tmp_path, readonly_remap=((missing, dest),), mode="bwrap",
    )
    assert str(missing) not in argv


@_needs_bwrap
def test_readonly_remap_exposes_real_path_inside_sandboxed_home(tmp_path: Path) -> None:
    """The actual use case: a real, read-only toolchain cache dir
    re-exposed at the SAME relative place under a fresh, otherwise
    hidden/empty sandboxed $HOME, readable from `$HOME/<rel>` and not
    writable there."""
    real_home = tmp_path / "real_home"
    cache = real_home / ".cache" / "ms-playwright"
    cache.mkdir(parents=True)
    (cache / "browser.bin").write_text("binary\n", encoding="utf-8")

    sandboxed_home = tmp_path / "sandboxed_home"
    sandboxed_home.mkdir()
    work = tmp_path / "work"
    work.mkdir()

    argv = sandbox.wrap(
        ["sh", "-c", 'cat "$HOME/.cache/ms-playwright/browser.bin" '
                     '&& (echo no > "$HOME/.cache/ms-playwright/browser.bin" '
                     '&& echo WROTE || echo WRITE_DENIED)'],
        cwd=work, writable=(work, sandboxed_home), hidden=(real_home,),
        readonly_remap=((cache, sandboxed_home / ".cache" / "ms-playwright"),),
        mode="bwrap", child_env={"HOME": str(sandboxed_home)},
    )
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert "binary" in result.stdout
    assert "WRITE_DENIED" in result.stdout
    assert (cache / "browser.bin").read_text(encoding="utf-8") == "binary\n"


@_needs_bwrap
def test_net_false_blocks_outbound_connection(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    code = ("import socket; socket.create_connection(('.'.join(['1'] * 4), 53), "
            "timeout=2)")
    argv = sandbox.wrap(
        [sys.executable, "-c", code], cwd=work, writable=(work,),
        net=False, mode="bwrap",
    )
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0


@_needs_bwrap
def test_net_true_allows_outbound_connection(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    code = ("import socket; socket.create_connection(('.'.join(['1'] * 4), 53), "
            "timeout=2)")
    argv = sandbox.wrap(
        [sys.executable, "-c", code], cwd=work, writable=(work,),
        net=True, mode="bwrap",
    )
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# resolve_runtime: a CLI resolved by bare name (argv[0]) through PATH must
# still be reachable inside a sandbox that hides the real HOME wholesale --
# the real HOME is where it is usually installed.
# ---------------------------------------------------------------------------

@_needs_bwrap
def test_hiding_apps_and_workspace_roots_still_lets_a_cargo_style_runtime_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tools/gb2's own hidden-roots policy (the whole apps root and the
    whole gb2 workspace root hidden, with only this job's own copy re-
    exposed) exercised directly against engine.sandbox: a neighbor
    app's secret and the workspace root's own secret must both stay
    unreadable, while THIS job's own re-exposed copy is readable and a
    runtime installed in a `~/.cargo/bin`-style flat directory (one
    secret FILE placed directly alongside the real binary) still
    starts, without that sibling secret becoming readable either --
    see resolve_runtime's own narrow-mount docstring for why a flat
    bin directory holding several unrelated tools is exactly the
    layout its old "mount the whole parent directory" rule used to
    over-expose."""
    apps_root = tmp_path / "apps_root"
    workspace_root = tmp_path / "workspace_root"
    this_job_copy = apps_root / "app_a"
    neighbor_app = apps_root / "app_b"
    this_job_copy.mkdir(parents=True)
    neighbor_app.mkdir(parents=True)
    (this_job_copy / "own_file.txt").write_text("app_a content", encoding="utf-8")
    (neighbor_app / "secret.txt").write_text("app_b secret", encoding="utf-8")
    workspace_root.mkdir(parents=True)
    (workspace_root / "secret.txt").write_text("workspace secret", encoding="utf-8")

    cargo_bin = tmp_path / "cargo_bin"
    cargo_bin.mkdir(parents=True)
    real_tool = cargo_bin / "mytool"
    real_tool.write_text(f"#!{sys.executable}\nprint('mytool-1.0')\n", encoding="utf-8")
    real_tool.chmod(0o755)
    (cargo_bin / "othersecrettool").write_text("neighbor tool secret", encoding="utf-8")

    monkeypatch.setenv("PATH", f"{cargo_bin}:{os.environ.get('PATH', '')}")
    sandbox._reset_available_cache()
    try:
        runtime = sandbox.resolve_runtime("mytool")
        argv = sandbox.wrap(
            ["mytool"], cwd=tmp_path,
            hidden=(apps_root, workspace_root),
            readonly=(this_job_copy,) + runtime.readonly,
            symlinks=runtime.symlinks,
            net=False, mode="bwrap", child_env={"PATH": os.environ["PATH"]},
        )
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0
        assert "mytool-1.0" in result.stdout

        def _cannot_read(target: Path) -> None:
            cat_argv = sandbox.wrap(
                ["cat", str(target)], cwd=tmp_path,
                hidden=(apps_root, workspace_root),
                readonly=(this_job_copy,) + runtime.readonly,
                symlinks=runtime.symlinks,
                net=False, mode="bwrap", child_env={"PATH": os.environ["PATH"]},
            )
            cat_result = subprocess.run(cat_argv, capture_output=True, text=True, timeout=10)
            assert cat_result.returncode != 0

        _cannot_read(neighbor_app / "secret.txt")
        _cannot_read(workspace_root / "secret.txt")
        _cannot_read(cargo_bin / "othersecrettool")

        can_read_argv = sandbox.wrap(
            ["cat", str(this_job_copy / "own_file.txt")], cwd=tmp_path,
            hidden=(apps_root, workspace_root),
            readonly=(this_job_copy,) + runtime.readonly,
            symlinks=runtime.symlinks,
            net=False, mode="bwrap", child_env={"PATH": os.environ["PATH"]},
        )
        can_read_result = subprocess.run(can_read_argv, capture_output=True, text=True, timeout=10)
        assert can_read_result.returncode == 0
        assert "app_a content" in can_read_result.stdout
    finally:
        sandbox._reset_available_cache()


def test_resolve_runtime_returns_empty_for_unknown_command() -> None:
    mounts = sandbox.resolve_runtime("no_such_command_at_all_xyz")
    assert mounts.readonly == ()
    assert mounts.symlinks == ()


@_needs_bwrap
def test_resolve_runtime_lets_a_home_installed_launcher_start_under_a_hidden_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Team lead's own reproduction shape: a fake HOME with an
    executable script at HOME/.local/bin/tool, symlinked to a
    versioned install under HOME/.local/share/tool/v1/tool (the shape
    described for this host's own claude/codex, even though this
    host's REAL claude/codex turn out to be laid out as
    <pkg_root>/bin/<launcher> instead -- see the two "on this host"
    tests below for that exact, already-installed shape). A sandboxed
    spawn of "tool" must start and print its version even with the
    whole fake HOME hidden; a sentinel file placed elsewhere under
    the same HOME must stay unreadable."""
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    install_dir = home / ".local" / "share" / "tool" / "v1"
    bin_dir.mkdir(parents=True)
    install_dir.mkdir(parents=True)
    real_tool = install_dir / "tool"
    real_tool.write_text(
        f"#!{sys.executable}\nprint('tool-version-1.0')\n", encoding="utf-8",
    )
    real_tool.chmod(0o755)
    (bin_dir / "tool").symlink_to(real_tool)
    sentinel = home / "sentinel.txt"
    sentinel.write_text("do not read", encoding="utf-8")

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    sandbox._reset_available_cache()
    try:
        runtime = sandbox.resolve_runtime("tool")
        argv = sandbox.wrap(
            ["tool"], cwd=tmp_path, hidden=(home,), readonly=runtime.readonly,
            symlinks=runtime.symlinks,
            net=False, mode="bwrap", child_env={"PATH": os.environ["PATH"]},
        )
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0
        assert "tool-version-1.0" in result.stdout

        cat_argv = sandbox.wrap(
            ["cat", str(sentinel)], cwd=tmp_path, hidden=(home,),
            readonly=runtime.readonly, symlinks=runtime.symlinks,
            net=False, mode="bwrap",
            child_env={"PATH": os.environ["PATH"]},
        )
        cat_result = subprocess.run(cat_argv, capture_output=True, text=True, timeout=10)
        assert cat_result.returncode != 0
    finally:
        sandbox._reset_available_cache()


@_needs_bwrap
def test_resolve_runtime_claude_starts_under_a_hidden_real_home() -> None:
    """On THIS host, `claude` is installed under the real operator
    HOME (an npm-global-style <pkg_root>/bin/claude.exe launcher) --
    the actual shape engine.steps.station/review's own worker sandbox
    needs this for, not just the synthetic fixture above. Skipped
    (via the assert below, not a pytest.skip, so a regression that
    silently stops finding `claude` is never mistaken for "nothing to
    test here") only if this host has no `claude` on PATH at all."""
    if shutil.which("claude") is None:
        pytest.skip("claude not installed on this host")
    host = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=20)
    if host.returncode != 0 or not host.stdout.strip():
        pytest.skip("claude on PATH is a placeholder (prints no version) on this host")
    home = Path(os.environ["HOME"])
    runtime = sandbox.resolve_runtime("claude")
    assert runtime.readonly, "claude resolved via PATH but resolve_runtime found no mounts"
    argv = sandbox.wrap(
        ["claude", "--version"], cwd=Path("/tmp"), hidden=(home,),
        readonly=runtime.readonly, symlinks=runtime.symlinks, net=False, mode="bwrap",
        child_env={"PATH": os.environ.get("PATH", "")},
    )
    result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0
    assert result.stdout.strip() == host.stdout.strip()


@_needs_bwrap
def test_resolve_runtime_codex_starts_under_a_hidden_real_home() -> None:
    """Same as the claude test above, for `codex` -- a `#!/usr/bin/env
    node` script (see _shebang_interpreter), so this also exercises
    resolve_runtime's interpreter-chasing branch for real."""
    if shutil.which("codex") is None:
        pytest.skip("codex not installed on this host")
    host = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=20)
    if host.returncode != 0 or not host.stdout.strip():
        pytest.skip("codex on PATH is a placeholder (prints no version) on this host")
    home = Path(os.environ["HOME"])
    runtime = sandbox.resolve_runtime("codex")
    assert runtime.readonly, "codex resolved via PATH but resolve_runtime found no mounts"
    argv = sandbox.wrap(
        ["codex", "--version"], cwd=Path("/tmp"), hidden=(home,),
        readonly=runtime.readonly, symlinks=runtime.symlinks, net=False, mode="bwrap",
        child_env={"PATH": os.environ.get("PATH", "")},
    )
    result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0
    assert result.stdout.strip() == host.stdout.strip()


# ---------------------------------------------------------------------------
# Integration: run_station and bench under sandbox="bwrap", through the
# real engine.gate (scripted worker's --gate consults `python -m
# engine.gate`, exactly as ClaudeRunner does).
# ---------------------------------------------------------------------------

def _gated_station_workspace(workspace: Workspace, config_path: Path) -> Workspace:
    config = replace(
        workspace.config,
        workers={"builder": WorkerConfig(runner="scripted", timeout_sec=10.0)},
        sandbox="bwrap",
    )
    from engine.config import write_json
    config = write_json(config, config_path)
    return Workspace(workspace.root, config, workspace.ledger,
                     workspace.alpha, workspace.beta)


def _station_processor(ws: Workspace, gate_argv: tuple[str, ...], attempt_id: str) -> Processor:
    # Must live somewhere still visible inside the worker sandbox: the
    # ledger directory is no longer writable at all (so it can no
    # longer incidentally re-expose the whole pytest tmp_path, which
    # lives under the sandbox's always-hidden /tmp -- see sandbox.py's
    # fixed "--tmpfs /tmp" prelude), so this now goes inside the
    # worker's own copy, which IS mounted writable.
    script = copy_root(ws.config, attempt_id, "builder") / "builder_script.json"
    script.write_text(json.dumps([
        {"tool": "Write", "file_path": "src/ok.py", "content": "print('ok')\n"},
        {"final": {"status": "done", "summary": "ok", "requests": []}},
    ]), encoding="utf-8")
    fakes = Fakes(
        publisher=FakePublisher(), installer=FakeInstaller(),
        runners={"scripted": ScriptedRunner(script, gate_argv=gate_argv)},
    )
    proc = Processor(config=ws.config, ledger=ws.ledger, fakes=fakes, holder="p1")
    proc.register("run_station", station.steps)
    return proc


@_needs_bwrap
def test_run_station_under_bwrap_completes_with_gate_events(
    workspace: Workspace, tmp_path: Path,
) -> None:
    config_path = tmp_path / "engine.json"
    ws = _gated_station_workspace(workspace, config_path)
    attempt_id = open_job(ws.config, ws.ledger, "alpha", "job1", "full", ("builder",))
    rid = submit_request(
        ws.ledger, "alpha", attempt_id, "run_station",
        {"station": "build", "role": "builder", "task": "write ok.py"}, "owner",
    )
    gate_argv = (sys.executable, "-P", "-m", "engine.gate")
    proc = _station_processor(ws, gate_argv, attempt_id)
    n = proc.run_once("alpha")
    assert n >= 1

    requests = {r.request_id: r for r in ws.ledger.queued_requests("alpha")}
    assert rid not in requests  # request finished (not left queued/running)

    kinds = [e.kind for _seq, e in ws.ledger.events("alpha")]
    assert "proc.started" in kinds
    assert "station.finished" in kinds
    # The gate's own decisions are imported from gate.log by
    # record_station, but NEVER under their original 'policy.decided'/
    # 'gate.denied' kind -- only under the single inert
    # 'gate_log.decision' kind (actor_kind "gate_log"), which has no
    # ledger handler and is never matched by any reconcile() query --
    # forwarding the log line's own kind verbatim would instead let a
    # forged line reach a real handler. The gate itself never
    # opens the ledger (see engine.gate's module docstring).
    assert "policy.decided" not in kinds
    assert "gate_log.decision" in kinds
    assert "gate_log.imported" in kinds
    decided = [e for _seq, e in ws.ledger.events("alpha", "gate_log.decision")]
    assert decided and all(e.actor_kind == "gate_log" for e in decided)
    assert any(e.payload.get("orig_kind") == "policy.decided" for e in decided)

    finished = [e for _seq, e in ws.ledger.events("alpha") if e.kind == "station.finished"]
    assert any(e.payload.get("ok") for e in finished)


@_needs_bwrap
def test_bench_run_tests_under_bwrap_passes(workspace: Workspace, tmp_path: Path) -> None:
    sample = make_sample(tmp_path / "sample", variant=1, python=Path(sys.executable))
    project_config = replace(
        workspace.config.projects["alpha"],
        repo=sample.repo, test_cmd=sample.test_cmd,
        install_cmd=sample.install_cmd, manifest=sample.manifest,
        bench_cases=sample.bench_cases,
    )
    config = replace(
        workspace.config,
        projects={**workspace.config.projects, "alpha": project_config},
        sandbox="bwrap",
    )
    ws = Workspace(workspace.root, config, workspace.ledger, sample.repo, workspace.beta)

    attempt_id = open_job(ws.config, ws.ledger, "alpha", "bench-job", "full", ("builder",))
    rid = submit_request(ws.ledger, "alpha", attempt_id, "bench", {}, "owner")

    fakes = Fakes(publisher=FakePublisher(), installer=FakeInstaller())
    proc = Processor(config=ws.config, ledger=ws.ledger, fakes=fakes, holder="p1")
    proc.register("bench", bench_run.steps)
    n = proc.run_once("alpha")
    assert n >= 1

    bench_evidence = [row for row in ws.ledger.evidence(attempt_id, "bench")
                      if row.payload.get("request_id") == rid]
    assert bench_evidence, "no bench evidence recorded"
    assert bench_evidence[-1].payload.get("passed") is True


# ---------------------------------------------------------------------------
# P0/P1 regression: station.py's real _sandboxed_argv, exercised directly
# against the realistic layout ENGINE.md describes (ledger_path.parent is
# an ANCESTOR of the hidden home/secrets_dir -- the exact shape that used
# to leak), plus the HOME read-only/writable-subpaths split.
# ---------------------------------------------------------------------------

class _NoHomeWritableRunner:
    home_writable_subpaths: tuple[str, ...] = ()


@_needs_bwrap
def test_station_sandbox_hides_sibling_copy_secrets_and_ledger(
    workspace: Workspace,
) -> None:
    """Real execution of engine.steps.station._sandboxed_argv for the
    realistic layout (ledger_path.parent == base, home == base/home,
    secrets_dir == base/secrets, this project's repo == workspace.alpha,
    the sibling project's repo == workspace.beta). A secret file and a
    sibling role's copy under `home` must stay unreadable; the
    worker's own copy must stay writable; the ledger file itself must
    be neither readable nor writable at all (ENGINE.md 「한계」: the
    worker sandbox no longer has the ledger directory writable -- the
    gate hook reads a ProjectFacts snapshot and appends to a plain log
    file instead, see engine.gate)."""
    attempt_id = "J-alpha-TEST01#1"
    own_copy = workspace.config.home / "copies" / attempt_id / "builder"
    own_copy.mkdir(parents=True)
    sibling_copy = workspace.config.home / "copies" / attempt_id / "other"
    sibling_copy.mkdir(parents=True)
    (sibling_copy / "sibling.txt").write_text("sibling\n", encoding="utf-8")
    stations_dir = workspace.config.home / "jobs" / attempt_id / "stations"
    stations_dir.mkdir(parents=True)

    config = replace(workspace.config, sandbox="bwrap")
    spec = WorkerSpec(
        project="alpha", attempt_id=attempt_id, generation=1, station="build",
        role="builder", copy_root=own_copy, workdir=own_copy, task="",
        result_path=stations_dir / "r.json", config_path=None,
    )
    ctx = SimpleNamespace(config=config, project="alpha", attempt_id=attempt_id)

    def wrapped(argv: list[str]) -> list[str]:
        return station._sandboxed_argv(ctx, spec, argv, {}, _NoHomeWritableRunner())

    secret = workspace.config.secrets_dir / "prod.key"
    cat = subprocess.run(wrapped(["sh", "-c", f"cat {secret}"]),
                         capture_output=True, text=True, timeout=10)
    assert cat.returncode != 0
    assert "SECRET" not in cat.stdout

    ls = subprocess.run(wrapped(["sh", "-c", f"ls {sibling_copy} 2>&1"]),
                        capture_output=True, text=True, timeout=10)
    assert ls.returncode != 0 or "sibling.txt" not in ls.stdout

    write_copy = subprocess.run(
        wrapped(["sh", "-c", f"echo hi > {own_copy}/out.txt"]),
        capture_output=True, text=True, timeout=10,
    )
    assert write_copy.returncode == 0
    assert (own_copy / "out.txt").read_text(encoding="utf-8") == "hi\n"

    workspace.config.ledger_path.write_text("LEDGER-SECRET-BYTES\n", encoding="utf-8")

    cat_ledger = subprocess.run(
        wrapped(["sh", "-c", f"cat {workspace.config.ledger_path}"]),
        capture_output=True, text=True, timeout=10,
    )
    assert cat_ledger.returncode != 0
    assert "LEDGER-SECRET-BYTES" not in cat_ledger.stdout

    write_ledger = subprocess.run(
        wrapped(["sh", "-c", f"echo overwritten > {workspace.config.ledger_path}"]),
        capture_output=True, text=True, timeout=10,
    )
    assert write_ledger.returncode != 0
    assert workspace.config.ledger_path.read_text(encoding="utf-8") == "LEDGER-SECRET-BYTES\n"

    sqlite_probe = subprocess.run(
        wrapped([sys.executable, "-c",
                f"import sqlite3; sqlite3.connect('{workspace.config.ledger_path}')"
                ".execute('select 1')"]),
        capture_output=True, text=True, timeout=10,
    )
    assert sqlite_probe.returncode != 0


@_needs_bwrap
def test_station_sandbox_argv_has_no_ledger_dir_bind(workspace: Workspace) -> None:
    """argv-level check: no --bind targets the ledger's directory at
    all (it is no longer writable, period) -- the WHOLE directory is
    hidden (tmpfs, then remounted read-only), not just the ledger file
    by name, so any other file in it (a future SQLite companion, an
    alias) is unmasked too, not just the four known names."""
    attempt_id = "J-alpha-ARGVCHECK#1"
    own_copy = workspace.config.home / "copies" / attempt_id / "builder"
    own_copy.mkdir(parents=True)
    workspace.config.ledger_path.write_text("x\n", encoding="utf-8")
    config = replace(workspace.config, sandbox="bwrap")
    spec = WorkerSpec(
        project="alpha", attempt_id=attempt_id, generation=1, station="build",
        role="builder", copy_root=own_copy, workdir=own_copy, task="",
        result_path=own_copy / "r.json", config_path=None,
    )
    ctx = SimpleNamespace(config=config, project="alpha", attempt_id=attempt_id)
    argv = station._sandboxed_argv(ctx, spec, ["true"], {}, _NoHomeWritableRunner())

    ledger_dir = str(workspace.config.ledger_path.parent)
    for i, token in enumerate(argv):
        if token == ledger_dir:
            assert argv[i - 1] != "--bind", "ledger directory must never be bound writable"
    tmpfs_indices = [i for i, token in enumerate(argv) if token == ledger_dir
                     and argv[i - 1] == "--tmpfs"]
    remount_indices = [i for i, token in enumerate(argv) if token == ledger_dir
                       and argv[i - 1] == "--remount-ro"]
    assert tmpfs_indices, "ledger directory must be hidden (--tmpfs)"
    assert remount_indices, "ledger directory's tmpfs must be remounted read-only"


@_needs_bwrap
def test_station_sandbox_home_writable_only_under_runner_subpaths(
    workspace: Workspace, tmp_path: Path,
) -> None:
    """ P1 fallback: $HOME inside the sandbox is no longer the
    real operator HOME at all (not even read-only) -- it is a fresh,
    per-attempt directory (engine.sandbox.prepare_worker_home) seeded
    with ONLY the real CLI's own login file. A runner's own declared
    home_writable_subpaths (e.g. ClaudeRunner's ".claude") are rebound
    writable on top of that fresh directory, never on top of the real
    one. The real HOME's OWN path is hidden outright: a sentinel file
    there (standing in for an ssh key, another app's config, ...) is
    unreadable from a worker-run command, by its real absolute path OR
    via $HOME -- $HOME simply does not resolve there any more."""
    fake_home = tmp_path / "fakehome"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".claude" / ".credentials.json").write_text(
        "REAL-CLAUDE-CREDENTIALS\n", encoding="utf-8")
    (fake_home / ".claude.json").write_text("REAL-CLAUDE-JSON\n", encoding="utf-8")
    sentinel = fake_home / ".ssh"
    sentinel.mkdir()
    (sentinel / "id_rsa").write_text("SENTINEL-SSH-KEY\n", encoding="utf-8")

    attempt_id = "J-alpha-TEST02#1"
    own_copy = workspace.config.home / "copies" / attempt_id / "builder"
    own_copy.mkdir(parents=True)
    stations_dir = workspace.config.home / "jobs" / attempt_id / "stations"
    stations_dir.mkdir(parents=True)

    config = replace(workspace.config, sandbox="bwrap")
    spec = WorkerSpec(
        project="alpha", attempt_id=attempt_id, generation=1, station="build",
        role="builder", copy_root=own_copy, workdir=own_copy, task="",
        result_path=stations_dir / "r.json", config_path=None,
    )
    ctx = SimpleNamespace(config=config, project="alpha", attempt_id=attempt_id)

    def wrapped(argv: list[str]) -> list[str]:
        # A FRESH env dict per call, matching real usage
        # (engine.steps.station._run_spawn_worker calls runner.env(...)
        # -- always a new dict -- exactly once per _sandboxed_argv call)
        # -- _sandboxed_argv overwrites env["HOME"] in place to this
        # worker's own fresh home dir, so reusing one dict across
        # several calls would feed call N+1 call N's OWN worker home as
        # if it were the real HOME to hide, which is not a real
        # production shape and would self-conflict (home_dir requested
        # as both hidden and readonly).
        return station._sandboxed_argv(
            ctx, spec, argv, {"HOME": str(fake_home)}, ClaudeRunner())

    # The real HOME's own path is invisible outright, not merely
    # read-only -- writing OR reading it by its real absolute path fails.
    outside_write = subprocess.run(
        wrapped(["sh", "-c", f"echo hi > {fake_home}/some_new_file"]),
        capture_output=True, text=True, timeout=10,
    )
    assert outside_write.returncode != 0
    assert not (fake_home / "some_new_file").exists()

    sentinel_read = subprocess.run(
        wrapped(["sh", "-c", f"cat {sentinel}/id_rsa"]),
        capture_output=True, text=True, timeout=10,
    )
    assert sentinel_read.returncode != 0
    assert "SENTINEL-SSH-KEY" not in sentinel_read.stdout

    # $HOME itself now resolves to a FRESH directory -- never fake_home.
    home_check = subprocess.run(
        wrapped(["sh", "-c", f'test "$HOME" != {fake_home} && echo DIFFERENT_HOME']),
        capture_output=True, text=True, timeout=10,
    )
    assert home_check.returncode == 0
    assert "DIFFERENT_HOME" in home_check.stdout

    # The real CLI's own login files WERE copied into that fresh $HOME
    # (ClaudeRunner's .claude/.credentials.json and .claude.json).
    login_read = subprocess.run(
        wrapped(["sh", "-c",
                 'cat "$HOME/.claude/.credentials.json" && cat "$HOME/.claude.json"']),
        capture_output=True, text=True, timeout=10,
    )
    assert login_read.returncode == 0
    assert "REAL-CLAUDE-CREDENTIALS" in login_read.stdout
    assert "REAL-CLAUDE-JSON" in login_read.stdout

    # Everything else under the real HOME (not a login file) was NOT
    # copied in -- unreachable via $HOME either.
    sentinel_via_home = subprocess.run(
        wrapped(["sh", "-c", 'cat "$HOME/.ssh/id_rsa" 2>&1']),
        capture_output=True, text=True, timeout=10,
    )
    assert sentinel_via_home.returncode != 0
    assert "SENTINEL-SSH-KEY" not in sentinel_via_home.stdout

    # home_writable_subpaths (".claude") is writable under the fresh HOME.
    inside = subprocess.run(
        wrapped(["sh", "-c", 'echo hi > "$HOME/.claude/session.json"']),
        capture_output=True, text=True, timeout=10,
    )
    assert inside.returncode == 0
    worker_home = (workspace.config.home / "jobs" / ids.attempt_dirname(attempt_id)
                   / "worker-home" / "build" / "builder")
    assert (worker_home / ".claude" / "session.json").read_text(
        encoding="utf-8") == "hi\n"


@_needs_bwrap
def test_station_sandbox_codex_auth_not_visible_to_claude_worker(
    workspace: Workspace, tmp_path: Path,
) -> None:
    """Two different roles' worker HOMEs, built from the SAME real HOME
    (containing both a claude and a codex login file), must each get
    ONLY their own CLI's login file -- a ClaudeRunner worker must never
    see codex's auth.json, and (by the same construction) a
    CodexRunner worker must never see claude's credentials."""
    fake_home = tmp_path / "fakehome"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".claude" / ".credentials.json").write_text(
        "REAL-CLAUDE-CREDENTIALS\n", encoding="utf-8")
    (fake_home / ".codex").mkdir(parents=True)
    (fake_home / ".codex" / "auth.json").write_text(
        "REAL-CODEX-AUTH\n", encoding="utf-8")

    attempt_id = "J-alpha-TEST03#1"
    config = replace(workspace.config, sandbox="bwrap")
    env = {"HOME": str(fake_home)}

    def home_for(role: str, runner: object) -> Path:
        own_copy = workspace.config.home / "copies" / attempt_id / role
        own_copy.mkdir(parents=True, exist_ok=True)
        stations_dir = workspace.config.home / "jobs" / attempt_id / "stations"
        stations_dir.mkdir(parents=True, exist_ok=True)
        spec = WorkerSpec(
            project="alpha", attempt_id=attempt_id, generation=1, station="build",
            role=role, copy_root=own_copy, workdir=own_copy, task="",
            result_path=stations_dir / f"{role}.json", config_path=None,
        )
        ctx = SimpleNamespace(config=config, project="alpha", attempt_id=attempt_id)
        argv = station._sandboxed_argv(ctx, spec, ["true"], dict(env), runner)
        result = subprocess.run(argv, capture_output=True, timeout=10)
        assert result.returncode == 0
        return (workspace.config.home / "jobs" / ids.attempt_dirname(attempt_id)
                / "worker-home" / "build" / role)

    claude_home = home_for("builder-claude", ClaudeRunner())
    codex_home = home_for("builder-codex", CodexRunner())

    assert (claude_home / ".claude" / ".credentials.json").read_text(
        encoding="utf-8") == "REAL-CLAUDE-CREDENTIALS\n"
    assert not (claude_home / ".codex" / "auth.json").exists()

    assert (codex_home / ".codex" / "auth.json").read_text(
        encoding="utf-8") == "REAL-CODEX-AUTH\n"
    assert not (codex_home / ".claude").exists()


@_needs_bwrap
def test_review_sandbox_mounts_snapshot_readonly_hides_repo_and_ledger(
    workspace: Workspace,
) -> None:
    """review._sandboxed_argv mounts the reviewer's own per-request
    SNAPSHOT (spec.workdir -- the extracted, already-fingerprinted
    tree, never the live repo itself -- see review._export_review_
    snapshot) explicitly read-only, hides the live project repo
    outright (a concurrently running bench writing to it must never
    be observable through its real absolute path either), and -- the
    reviewer runs no gate hook, so it never had any business touching
    the ledger at all -- hides the ledger file itself outright too,
    the same way station.py's worker sandbox now does
    (ENGINE.md 「한계」)."""
    config = replace(workspace.config, sandbox="bwrap")
    ctx = SimpleNamespace(config=config, project="alpha", attempt_id="J-alpha-REV01#1")
    snapshot = workspace.root / "review-snapshot"
    snapshot.mkdir()
    (snapshot / "frozen.txt").write_text("SNAPSHOT-CONTENT\n", encoding="utf-8")
    spec = WorkerSpec(
        project="alpha", attempt_id=ctx.attempt_id, generation=1, station="review",
        role="reviewer", copy_root=snapshot, workdir=snapshot, task="",
        result_path=workspace.root / "r.json", config_path=None,
    )
    workspace.config.ledger_path.write_text("LEDGER-SECRET-BYTES\n", encoding="utf-8")
    argv = review._sandboxed_argv(ctx, spec, ["true"], {}, _NoHomeWritableRunner())

    snapshot_str = str(snapshot)
    ro_idx = argv.index(snapshot_str)
    assert argv[ro_idx - 1] == "--ro-bind"

    write_result = subprocess.run(
        argv[:-1] + ["sh", "-c", f"echo hi > {snapshot_str}/probe.txt"],
        capture_output=True, text=True, timeout=10,
    )
    assert write_result.returncode != 0
    assert not (snapshot / "probe.txt").exists()

    read_snapshot = subprocess.run(
        argv[:-1] + ["sh", "-c", f"cat {snapshot_str}/frozen.txt"],
        capture_output=True, text=True, timeout=10,
    )
    assert read_snapshot.returncode == 0
    assert "SNAPSHOT-CONTENT" in read_snapshot.stdout

    cat_repo = subprocess.run(
        argv[:-1] + ["sh", "-c", f"cat {workspace.alpha}/package.json"],
        capture_output=True, text=True, timeout=10,
    )
    assert cat_repo.returncode != 0

    cat_ledger = subprocess.run(
        argv[:-1] + ["sh", "-c", f"cat {workspace.config.ledger_path}"],
        capture_output=True, text=True, timeout=10,
    )
    assert cat_ledger.returncode != 0
    assert "LEDGER-SECRET-BYTES" not in cat_ledger.stdout


@_needs_bwrap
def test_station_sandbox_never_mounts_copy_link_that_escapes_to_secrets(
    workspace: Workspace,
) -> None:
    """copy_links=["deps"] with repo/deps a symlink to secrets_dir.
    open_job._make_copy correctly refuses to create that symlink in
    the copy (copy_link_source_escapes_repo); station._sandboxed_argv
    must not mount the CONFIGURED `repo/deps` read-only regardless of
    whether the copy's own symlink exists, or it would re-expose
    secrets_dir itself. Nothing is mounted for "deps", and the real
    sandboxed run cannot read the secret."""
    from dataclasses import replace

    (workspace.alpha / "deps").symlink_to(workspace.config.secrets_dir)

    project_config = replace(workspace.config.projects["alpha"], copy_links=("deps",))
    config = replace(
        workspace.config,
        projects={**workspace.config.projects, "alpha": project_config},
        sandbox="bwrap",
    )
    attempt_id = open_job(config, workspace.ledger, "alpha", "copy-links-secrets-escape",
                          "full", ("builder",))

    opened = [e for _seq, e in workspace.ledger.events("alpha", "job.opened")
             if e.payload.get("attempt_id") == attempt_id][0]
    assert opened.payload["copy_links_skipped"] == [
        {"role": "builder", "entry": "deps", "reason": "copy_link_source_escapes_repo"},
    ]

    own_copy = copy_root(config, attempt_id, "builder")
    ctx = SimpleNamespace(config=config, project="alpha", attempt_id=attempt_id,
                          ledger=workspace.ledger,
                          request=SimpleNamespace(request_id="rid-escape"))
    spec = WorkerSpec(
        project="alpha", attempt_id=attempt_id, generation=1, station="build",
        role="builder", copy_root=own_copy, workdir=own_copy, task="",
        result_path=own_copy / "r.json", config_path=None,
    )
    argv = station._sandboxed_argv(ctx, spec, ["true"], {}, _NoHomeWritableRunner())

    secrets = str(workspace.config.secrets_dir)
    # secrets_dir is always HIDDEN (--tmpfs, then --remount-ro) regardless
    # of copy_links -- the regression was an EXTRA --ro-bind of it from
    # the "deps" entry. It must appear exactly twice in argv: once as
    # that hide's --tmpfs and once as its trailing --remount-ro, never
    # re-bound by any --ro-bind/--bind.
    secret_indices = [i for i, token in enumerate(argv) if token == secrets]
    assert len(secret_indices) == 2
    flags = {argv[i - 1] for i in secret_indices}
    assert flags == {"--tmpfs", "--remount-ro"}

    secret_file = workspace.config.secrets_dir / "prod.key"
    result = subprocess.run(
        argv[:-1] + ["sh", "-c", f"cat {secret_file}"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "SECRET" not in result.stdout


@_needs_bwrap
def test_station_sandbox_reexposes_ordinary_copy_link_readonly(
    workspace: Workspace,
) -> None:
    """The ordinary case -- a gitignored repo/node_modules linked into
    the copy -- must be re-exposed, not skipped just because the repo
    itself (and the ledger dir, an ancestor of the repo in the usual
    layout) sits in the forbidden-roots list: that would make every
    repo/<entry> target register as 'inside a hidden root', leaving
    the copy's symlink pointed at an empty directory inside the
    sandbox. The target must be mounted read-only at its repo path and readable
    through the copy's symlink; writing through it must fail."""
    from dataclasses import replace

    mods = workspace.alpha / "node_modules"
    mods.mkdir()
    (mods / "f.txt").write_text("MODS\n", encoding="utf-8")
    (workspace.alpha / ".gitignore").write_text("node_modules/\n", encoding="utf-8")

    project_config = replace(workspace.config.projects["alpha"], copy_links=("node_modules",))
    config = replace(
        workspace.config,
        projects={**workspace.config.projects, "alpha": project_config},
        sandbox="bwrap",
    )
    attempt_id = open_job(config, workspace.ledger, "alpha", "copy-links-ordinary",
                          "full", ("builder",))
    opened = [e for _seq, e in workspace.ledger.events("alpha", "job.opened")
             if e.payload.get("attempt_id") == attempt_id][0]
    assert opened.payload["copy_links_skipped"] == []

    own_copy = copy_root(config, attempt_id, "builder")
    assert (own_copy / "node_modules").is_symlink()
    ctx = SimpleNamespace(config=config, project="alpha", attempt_id=attempt_id,
                          ledger=workspace.ledger,
                          request=SimpleNamespace(request_id="rid-ordinary"))
    spec = WorkerSpec(
        project="alpha", attempt_id=attempt_id, generation=1, station="build",
        role="builder", copy_root=own_copy, workdir=own_copy, task="",
        result_path=own_copy / "r.json", config_path=None,
    )
    argv = station._sandboxed_argv(ctx, spec, ["true"], {}, _NoHomeWritableRunner())

    target = str(mods)
    ro = [i for i, token in enumerate(argv) if token == target and argv[i - 1] == "--ro-bind"]
    assert ro, argv
    skipped = [e for _seq, e in workspace.ledger.events("alpha", "copy_link.mount_skipped")]
    assert skipped == []

    probe = f"cat {own_copy}/node_modules/f.txt && ! touch {own_copy}/node_modules/w"
    result = subprocess.run(
        argv[:-1] + ["sh", "-c", probe],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "MODS" in result.stdout


@_needs_bwrap
def test_bench_readonly_paths_remap_works_when_bench_home_is_readonly(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With bench_home_writable left at its default False, bench_home
    itself is mounted --ro-bind. bwrap can't auto-create a missing
    mountpoint under a read-only ancestor, so a readonly_remap
    destination under bench_home must not fail with
    "Can't mkdir ...: Read-only file system" -- exercised here with
    TWO bench_readonly_paths entries (ms-playwright cache, then .npm)
    to check ordering/stability."""
    real_home = tmp_path / "real_home"
    playwright_cache = real_home / ".cache" / "ms-playwright"
    playwright_cache.mkdir(parents=True)
    (playwright_cache / "browser.bin").write_text("binary\n", encoding="utf-8")
    npm_cache = real_home / ".npm"
    npm_cache.mkdir(parents=True)
    (npm_cache / "f").write_text("npm-cache-content\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(real_home))

    config = replace(
        workspace.config, sandbox="bwrap",
        bench_readonly_paths=(
            real_home / ".cache" / "ms-playwright",
            real_home / ".npm",
        ),
    )
    ctx = SimpleNamespace(config=config, project="alpha", attempt_id="J-alpha-BENCH01#1")
    argv, child_env = bench_run._bench_sandbox_argv(
        ctx, workspace.alpha, ["sh", "-c", 'cat "$HOME/.cache/ms-playwright/browser.bin" '
                                            '&& cat "$HOME/.npm/f" && echo ALL_OK'],
        {},
    )
    popen_env = sandbox.exec_env(config.sandbox, child_env)
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10, env=popen_env)
    assert result.returncode == 0, result.stderr
    assert "binary" in result.stdout
    assert "npm-cache-content" in result.stdout
    assert "ALL_OK" in result.stdout


@_needs_bwrap
def test_bench_readonly_paths_remap_and_writable_bench_home_both_work(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bench_home_writable=True: bench_home itself is writable (a
    project's test_cmd can write under $HOME), AND the remapped cache
    under it is still usable -- both the top-level write and the
    remapped read must succeed."""
    real_home = tmp_path / "real_home"
    npm_cache = real_home / ".npm"
    npm_cache.mkdir(parents=True)
    (npm_cache / "f").write_text("npm-cache-content\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(real_home))

    config = replace(
        workspace.config, sandbox="bwrap", bench_home_writable=True,
        bench_readonly_paths=(real_home / ".npm",),
    )
    ctx = SimpleNamespace(config=config, project="alpha", attempt_id="J-alpha-BENCH02#1")
    argv, child_env = bench_run._bench_sandbox_argv(
        ctx, workspace.alpha,
        ["sh", "-c", 'touch "$HOME/x" && cat "$HOME/.npm/f" && echo ALL_OK'],
        {},
    )
    popen_env = sandbox.exec_env(config.sandbox, child_env)
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10, env=popen_env)
    assert result.returncode == 0, result.stderr
    assert "npm-cache-content" in result.stdout
    assert "ALL_OK" in result.stdout


@_needs_bwrap
def test_copy_link_writable_subpaths_gives_scratch_write_under_readonly_entry(
    workspace: Workspace,
) -> None:
    """node_modules is re-exposed read-only in the worker sandbox so a
    Bash-run test_cmd can resolve it, but Vite/Vitest write
    node_modules/.vite-temp (bundled config) and node_modules/.vite
    (cache) at startup, which would fail with EROFS if nothing gave
    those subpaths write access.
    copy_link_writable_subpaths={"node_modules": [".vite-temp"]} must
    give exactly that one subpath a writable scratch mount, while an
    ordinary file directly under node_modules still can't be written,
    and the real repo's node_modules is never touched either way."""
    from dataclasses import replace

    (workspace.alpha / "node_modules" / "pkg").mkdir(parents=True)
    (workspace.alpha / "node_modules" / "pkg" / "index.js").write_text(
        "module.exports = 1;\n", encoding="utf-8")

    project_config = replace(
        workspace.config.projects["alpha"],
        copy_links=("node_modules",),
        copy_link_writable_subpaths={"node_modules": (".vite-temp",)},
    )
    config = replace(
        workspace.config,
        projects={**workspace.config.projects, "alpha": project_config},
        sandbox="bwrap",
    )
    attempt_id = open_job(config, workspace.ledger, "alpha", "copy-link-scratch",
                          "full", ("builder",))
    opened = [e for _seq, e in workspace.ledger.events("alpha", "job.opened")
             if e.payload.get("attempt_id") == attempt_id][0]
    assert opened.payload["copy_links_skipped"] == []

    own_copy = copy_root(config, attempt_id, "builder")
    ctx = SimpleNamespace(config=config, project="alpha", attempt_id=attempt_id,
                          ledger=workspace.ledger,
                          request=SimpleNamespace(request_id="rid-scratch"))
    spec = WorkerSpec(
        project="alpha", attempt_id=attempt_id, generation=1, station="build",
        role="builder", copy_root=own_copy, workdir=own_copy, task="",
        result_path=own_copy / "r.json", config_path=None,
    )
    node_modules = own_copy / "node_modules"

    def wrapped(argv: list[str]) -> list[str]:
        return station._sandboxed_argv(ctx, spec, argv, {}, _NoHomeWritableRunner())

    scratch_write = subprocess.run(
        wrapped(["sh", "-c", f"mkdir -p {node_modules}/.vite-temp "
                              f"&& touch {node_modules}/.vite-temp/x"]),
        capture_output=True, text=True, timeout=10,
    )
    assert scratch_write.returncode == 0, scratch_write.stderr

    ordinary_write = subprocess.run(
        wrapped(["sh", "-c", f"touch {node_modules}/y"]),
        capture_output=True, text=True, timeout=10,
    )
    assert ordinary_write.returncode != 0

    # The real repo's node_modules is untouched either way (the
    # scratch write lands only in the sandboxed tmpfs, never the host).
    real_node_modules = workspace.alpha / "node_modules"
    assert not (real_node_modules / ".vite-temp").exists()
    assert not (real_node_modules / "y").exists()
    assert (real_node_modules / "pkg" / "index.js").read_text(encoding="utf-8") \
        == "module.exports = 1;\n"
