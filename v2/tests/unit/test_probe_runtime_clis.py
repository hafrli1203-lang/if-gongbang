"""A project's probe_runtime_clis (engine.config.ProjectConfig) names bare
CLIs (e.g. "claude") a probe script may itself spawn during run_probe_
scripts' own real re-execution. The probe's bench sandbox otherwise
hides the real operator HOME wholesale and mounts no CLI binary at all
(unlike a worker, see engine.application.steps.worker_sandbox.sandboxed_argv) --
a live run against the 퍼포먼스광고 project confirmed `spawnSync claude
ENOENT` for a .js probe target that itself shells out to `claude -p`.

engine.application.acceptance_bench.run_in_bench_sandbox's own `runtime_clis`
kwarg closes that gap by reusing engine.application.steps.worker_sandbox.
cli_runtime_mounts -- the SAME function a real worker sandbox uses --
rather than a second, divergent mount policy."""
from __future__ import annotations

from pathlib import Path

import pytest

from engine.application import acceptance_bench
from engine.application.steps import worker_sandbox
from engine.infrastructure import sandbox as sandbox_mod
from tests.conftest import Workspace

_needs_bwrap = pytest.mark.skipif(
    not sandbox_mod.available(), reason="bwrap not available on this host",
)


def _patch_bench_sandbox_argv(monkeypatch, captured: dict[str, object]) -> None:
    def fake_bench_sandbox_argv(ctx, repo, argv, env, *, net=False,
                               extra_readonly=(), extra_symlinks=(),
                               extra_writable=()):
        captured["extra_readonly"] = extra_readonly
        captured["extra_symlinks"] = extra_symlinks
        captured["extra_writable"] = extra_writable
        return list(argv), dict(env)

    monkeypatch.setattr(acceptance_bench, "bench_sandbox_argv", fake_bench_sandbox_argv)


def test_empty_runtime_clis_leaves_mounts_unchanged(
    workspace: Workspace, monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    _patch_bench_sandbox_argv(monkeypatch, captured)

    acceptance_bench.run_in_bench_sandbox(
        workspace.config, "alpha", "attempt1", workspace.alpha,
        ["true"], {}, 5.0,
    )

    assert captured["extra_readonly"] == ()
    assert captured["extra_symlinks"] == ()
    assert captured["extra_writable"] == ()


def test_runtime_clis_claude_adds_the_same_mounts_a_worker_gets(
    workspace: Workspace, monkeypatch, tmp_path: Path,
) -> None:
    """With probe_runtime_clis=("claude",), run_in_bench_sandbox must
    add exactly the (readonly, symlinks) cli_runtime_mounts("claude",
    ...) returns -- a fake resolve_runtime stands in for the real CLI
    lookup so this test does not depend on claude actually being
    installed on the host running it."""
    fake_bin = tmp_path / "claude-real-bin"
    fake_bin.write_text("#!/bin/sh\necho fake claude\n", encoding="utf-8")
    fake_symlink_dest = tmp_path / "home" / ".local" / "bin" / "claude"
    fake_mounts = sandbox_mod.RuntimeMounts(
        readonly=(fake_bin,), symlinks=((fake_bin, fake_symlink_dest),),
    )

    seen_names: list[str] = []

    def fake_resolve_runtime(cli_name: str) -> sandbox_mod.RuntimeMounts:
        seen_names.append(cli_name)
        return fake_mounts if cli_name == "claude" else sandbox_mod.RuntimeMounts()

    monkeypatch.setattr(worker_sandbox.sandbox, "resolve_runtime", fake_resolve_runtime)
    # prepare_worker_home copies login files from the real HOME -- point
    # it at an empty tmp dir so this test never touches the operator's
    # actual ~/.claude.
    monkeypatch.setenv("HOME", str(tmp_path / "fake-real-home"))

    captured: dict[str, object] = {}
    _patch_bench_sandbox_argv(monkeypatch, captured)

    acceptance_bench.run_in_bench_sandbox(
        workspace.config, "alpha", "attempt1", workspace.alpha,
        ["true"], {}, 5.0, runtime_clis=("claude",),
    )

    assert seen_names == ["claude"]
    home_dir = acceptance_bench.bench_home_dir(
        acceptance_bench.bench_ctx(workspace.config, "alpha", "attempt1"))
    assert fake_bin in captured["extra_readonly"]
    assert home_dir in captured["extra_readonly"]
    assert (fake_bin, fake_symlink_dest) in captured["extra_symlinks"]
    # 결함 20: claude's own home_writable_subpaths (".claude",
    # ".claude.json") must be writable under THIS disposable bench
    # home too, the same as a real worker gets under its own home.
    assert (home_dir / ".claude") in captured["extra_writable"]
    assert (home_dir / ".claude.json") in captured["extra_writable"]
    # home_dir must actually exist on disk (sandbox.prepare_worker_home's
    # own side effect) -- a mount of a nonexistent path is silently
    # skipped by sandbox.wrap, which would make this mount a no-op.
    assert home_dir.is_dir()


@_needs_bwrap
def test_probe_js_target_spawning_a_real_cli_sees_it_under_bwrap(
    workspace: Workspace, monkeypatch,
) -> None:
    """End-to-end, under sandbox="bwrap": a probe target's own .js
    script spawns a real CLI resolvable on THIS test host's PATH --
    `sh`, standing in for "claude"/"codex" since this test must not
    require either to actually be installed -- and must find it
    (resolve_runtime mounts it in) rather than failing with the bare
    "command not found" a hidden-HOME bench sandbox gives by default."""
    from dataclasses import replace

    from engine.application.acceptance_probe import run_probe_scripts
    from engine.steps.open_job import open_job

    alpha_project = replace(workspace.config.projects["alpha"], probe_runtime_clis=("sh",))
    config = replace(
        workspace.config, sandbox="bwrap",
        projects={**workspace.config.projects, "alpha": alpha_project},
    )
    attempt_id = open_job(config, workspace.ledger, "alpha", "job1", "full",
                          roles=("builder",))
    probe_dir = workspace.alpha / ".gongbang" / "job1" / "probe"
    probe_dir.mkdir(parents=True)
    (probe_dir / "t1.js").write_text(
        "const { spawnSync } = require('child_process');\n"
        "const r = spawnSync('sh', ['-c', 'echo sh-found']);\n"
        "if (r.error) {\n"
        "  console.log('SPAWN_ERROR:' + r.error.code);\n"
        "} else {\n"
        "  console.log(r.stdout.toString().trim());\n"
        "  console.log('PROBE OK https://docs.stripe.com/api');\n"
        "}\n",
        encoding="utf-8",
    )

    results = run_probe_scripts(
        config, workspace.ledger, "alpha", attempt_id, "job1",
        [("stripe", "https://docs.stripe.com/api")],
    )

    assert "SPAWN_ERROR" not in results[0].stdout_tail
    assert "sh-found" in results[0].stdout_tail
    assert results[0].ok is True
