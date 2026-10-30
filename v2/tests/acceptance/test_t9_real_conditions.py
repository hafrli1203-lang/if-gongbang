"""T9 -- "real-condition" acceptance suite: a fixture app deliberately
shaped like the real performance-ads project that produced 8 defects on
 (commits 2be9e64..37d232e), run through the engine's own
production functions end to end -- real git repos, a real bwrap sandbox
when available, real subprocess spawn, and the full_orbit scripted-
worker flow (no live AI anywhere: ScriptedRunner / the station-stub
pattern tests/unit/test_full_orbit.py already uses stand in for a real
worker). Each test below is a regression lock for exactly one of the 8
fixed defects; at least one (t9b_*) is additionally shown RED against
the pre-fix code, loaded straight from git history via `git show
<commit>^:<path>` -- never git stash, never touching this worktree's
own checkout.

Defect -> commit -> test:
  1. facts.md header with extra words ("출처(파일:줄 또는 문서)")  2be9e64  test_1_*
  2. app .gitignore "*.log" drops .gongbang/<job>/probe/t<N>.log       446c805  test_2_*
  3. same, in the certified evidence snapshot                          f4fb0ec  test_3_*
  4. probe ".py" target spawned via the engine's own hidden venv      c9e5568  test_4_*
  5. probe sandbox hides a CLI a probe script itself shells out to    c9e5568  test_5_*
  6. prober prompt never states the exact "PROBE OK <doc>" line       4ac8fbb  test_6_*
  7. a >131072-byte CLI prompt on argv fails exec with E2BIG          ee45a57  test_7_*
  8. an internal station crash sticks `gb2 continue` in "unknown"     37d232e  test_8_*
  9. (결함 10, same sample-app job) a plan-review-rejection resume
     reopened the plan station with no memory of the rejecting
     review's own findings                                            (this worktree)  test_9_*
  12. (결함 12, 마케팅 채팅 job, a prober's own copy
      carried an edited facts.md (station "facts"'s own record) --
      station_owners granted "probe" the whole ".gongbang/" tree, so
      merge's own config-driven prefix never caught it; the next
      facts.md read then choked with
      probe_targets_invalid:malformed_probe_target_line, and `gb2
      continue` just reopened "probe" onto the same broken file
      forever                                                          (this worktree)  test_12_*
  16. (결함 16, sample-app bench seq 1703) bench's own full-scope test_cmd
      ran tests/scenes/ (plan-owned, not-yet-built-behavior scripts)
      unconditionally, so a job's first attempt always failed bench    (this worktree)  test_16_*
  17. (결함 17, same evidence) a package.json `file:../...` dependency
      resolved one nesting level short inside the engine's live workspace
      worktree (`Cannot find package '@sample-app/kit/db'`) even though
      the identical dependency resolves fine in the real source repo   (this worktree)  test_17_*
  18. (결함 18, same evidence) a failed bench's own evidence payload
      carried no test output and no failed-file names, so diagnosing
      it required re-running the suite by hand                        (this worktree)  test_18_*
  19. (결함 19, 마케팅 채팅 job, 실측 재실행) an "## 외부
      대상" bullet's doc value was an http(s) URL with a human
      explanation appended on the SAME line -- the parsed "url" then
      never matched the probe script's own real "PROBE OK <url>"
      stdout line, failing a target whose probe had actually
      succeeded                      (this worktree, test_t9b_probe_sandbox.py)  test_19_*
  20. (결함 20, 마케팅 채팅 job, 실측 재실행) a probe
      target's own probe_runtime_clis CLI (standing in for `codex`)
      got its disposable probe $HOME mounted wholesale read-only, with
      none of that CLI's own home_writable_subpaths (e.g. ".codex")
      granted writable the way a real worker already gets under its
      own fresh home -- the CLI then failed "Read-only file system"
      writing its own session state, which proves nothing about
      whatever it would otherwise have done  (this worktree, test_t9b_probe_sandbox.py)  test_20_*
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from engine.infrastructure import sandbox as sandbox_mod
from tests.conftest import GIT_ENV, git
from tests.probe_target import contacting_py_script, probe_target, serve_target  # noqa: F401
from tests.security_gate import bwrap_skipif

REPO_ROOT = Path(__file__).resolve().parents[2]

_needs_bwrap = bwrap_skipif(
    sandbox_mod.available(),
    reason="bwrap not available on this host -- defects 4/5 (hidden probe "
           "python / hidden probe-sandbox CLI) can only be reproduced under "
           "a real bwrap sandbox; skipped rather than faked.",
)


def _git_app(tmp_path: Path, name: str, gitignore: str = "*.log\n") -> Path:
    """A tiny real git repo standing in for a real app -- the ONE thing
    every one of the 8 defects needs: an app .gitignore that matches
    engine records under .gongbang/ (the real incident's own rule was
    literally "*.log")."""
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text(gitignore, encoding="utf-8")
    (repo / "package.json").write_text('{"name": "%s"}\n' % name, encoding="utf-8")
    (repo / "README.md").write_text("fixture app\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


# ---- 1. facts table header contains extra words -------------------------

def test_1_facts_header_with_extra_words_is_accepted_by_the_real_checker(
    tmp_path: Path,
) -> None:
    """A real facts.md -- committed into a real git repo, not a bare
    string -- whose header reads "출처(파일:줄 또는 문서)" (the engine's
    own prober/fact-checker prompt tells a worker to write exactly this)
    must be accepted by the real, current engine.gates.check_facts."""
    from engine.gates import check_facts

    repo = _git_app(tmp_path, "app1")
    facts_dir = repo / ".gongbang" / "jobA"
    facts_dir.mkdir(parents=True)
    (facts_dir / "facts.md").write_text(
        "| 주장 | 판정 | 출처(파일:줄 또는 문서) |\n"
        "|---|---|---|\n"
        "| a | 통과 | `src/x.ts:3` |\n",
        encoding="utf-8",
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "facts")

    assert check_facts(repo, "jobA") == []



def _source_at(rev: str, path: str) -> str:
    """`path` as of `rev` in this checkout's own git history; skips (with the
    reason) when that history is absent -- the public copy ships without the
    private commit history these RED-proof tests read from."""
    import pytest
    spec = f"{rev}:{path}"
    if subprocess.run(["git", "-C", str(REPO_ROOT), "cat-file", "-e", spec],
                      capture_output=True).returncode != 0:
        pytest.skip(f"git history for {spec} is not in this checkout")
    return subprocess.run(["git", "-C", str(REPO_ROOT), "cat-file", "blob", spec],
                          check=True, capture_output=True, text=True).stdout

def test_1_red_against_the_pre_fix_checker_loaded_from_git_history(
    tmp_path: Path,
) -> None:
    """Same facts.md, but run through the EXACT pre-fix function body
    (git show 2be9e64^:engine/domain/stage_checks.py -- the parent of the
    fixing commit, not this worktree's own file) loaded into a throwaway
    module: it must reject the very row the current checker accepts,
    proving this is a real regression lock, not a tautology."""
    import types

    old_src = _source_at("2be9e64^", "engine/domain/stage_checks.py")
    old_mod = types.ModuleType("_t9_old_stage_checks")
    old_mod.__dict__["__file__"] = "<git show 2be9e64^>"
    exec(compile(old_src, "<git show 2be9e64^:engine/domain/stage_checks.py>", "exec"),
         old_mod.__dict__)

    repo = _git_app(tmp_path, "app1red")
    facts_dir = repo / ".gongbang" / "jobA"
    facts_dir.mkdir(parents=True)
    (facts_dir / "facts.md").write_text(
        "| 주장 | 판정 | 출처(파일:줄 또는 문서) |\n"
        "|---|---|---|\n"
        "| a | 통과 | `src/x.ts:3` |\n",
        encoding="utf-8",
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "facts")

    old_errors = old_mod.check_facts(repo, "jobA")
    assert old_errors != [], (
        "pre-fix check_facts was expected to reject this row (it only "
        "recognized an EXACT '판정'/'출처' header), but it passed -- the "
        "repro no longer demonstrates the historical bug")

    from engine.gates import check_facts
    assert check_facts(repo, "jobA") == []


# ---- 2. app .gitignore drops a real probe log from dirty_paths ----------

def test_2_gitignored_probe_log_is_still_picked_up_by_dirty_paths(
    tmp_path: Path,
) -> None:
    from engine.steps.open_job import dirty_paths

    repo = _git_app(tmp_path, "app2", gitignore="*.log\n")
    probe_dir = repo / ".gongbang" / "jobB" / "probe"
    probe_dir.mkdir(parents=True)
    (probe_dir / "t1.log").write_text("probe: Some API https://docs.example/x\nPROBE OK\n",
                                      encoding="utf-8")
    # Confirm the app's own .gitignore really does hide it from plain git.
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True, env=GIT_ENV,
    ).stdout
    assert "t1.log" not in status, "fixture is not actually gitignored -- test is void"

    paths = dirty_paths(repo, excludes=())
    assert ".gongbang/jobB/probe/t1.log" in paths


def test_2_commit_paths_force_adds_the_gitignored_record(tmp_path: Path) -> None:
    from engine.application.pipeline_stages import commit_paths

    repo = _git_app(tmp_path, "app2b", gitignore="*.log\n")
    probe_dir = repo / ".gongbang" / "jobB" / "probe"
    probe_dir.mkdir(parents=True)
    (probe_dir / "t1.log").write_text("PROBE OK\n", encoding="utf-8")

    commit_paths(repo, [".gongbang/jobB/probe/t1.log"], "probe record")

    tracked = subprocess.run(
        ["git", "-C", str(repo), "ls-files", ".gongbang"],
        capture_output=True, text=True, env=GIT_ENV,
    ).stdout
    assert ".gongbang/jobB/probe/t1.log" in tracked


# ---- 3. the certified evidence snapshot drops the same record -----------

def test_3_snapshot_keeps_a_gitignored_probe_log_the_repo_already_tracks(
    tmp_path: Path,
) -> None:
    from engine.application.pipeline_stages import commit_paths
    from engine.application.steps.suite_snapshot import make_snapshot
    from engine.infrastructure import evidence

    repo = _git_app(tmp_path, "app3", gitignore="*.log\n")
    probe_dir = repo / ".gongbang" / "jobC" / "probe"
    probe_dir.mkdir(parents=True)
    (probe_dir / "t1.log").write_text("PROBE OK\n", encoding="utf-8")
    # Force-tracked exactly like the real pipeline does (defect 2's own fix).
    commit_paths(repo, [".gongbang/jobC/probe/t1.log"], "probe record")

    fp = evidence.fingerprint(repo, excludes=())
    dest = tmp_path / "snapshot3"
    make_snapshot(repo, dest, fp)

    tracked = subprocess.run(
        ["git", "-C", str(dest), "ls-files", ".gongbang"],
        capture_output=True, text=True, env=GIT_ENV,
    ).stdout
    assert ".gongbang/jobC/probe/t1.log" in tracked
    assert (dest / ".gongbang" / "jobC" / "probe" / "t1.log").exists()


def test_3_red_without_force_add_the_snapshot_drops_it(tmp_path: Path) -> None:
    """Reconstructs the pre-fix shape of make_snapshot's own last steps
    (plain `git add -A`, no -f) directly against a real export -- never
    a mocked substitute -- to show the historical bug is real: the
    snapshot dest is itself a fresh repo that re-applies the exported
    .gitignore, so a plain `add -A` silently drops the very record
    commit 446c805 force-tracked upstream."""
    from engine.application.pipeline_stages import commit_paths
    from engine.infrastructure import evidence
    from engine.infrastructure.gitutil import git_argv

    repo = _git_app(tmp_path, "app3red", gitignore="*.log\n")
    probe_dir = repo / ".gongbang" / "jobC" / "probe"
    probe_dir.mkdir(parents=True)
    (probe_dir / "t1.log").write_text("PROBE OK\n", encoding="utf-8")
    commit_paths(repo, [".gongbang/jobC/probe/t1.log"], "probe record")

    fp = evidence.fingerprint(repo, excludes=())
    dest = tmp_path / "snapshot3red"
    dest.mkdir()
    evidence.export_tree(repo, fp, dest)
    subprocess.run(git_argv(dest, "init", "-q"), check=True, capture_output=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
           "GIT_CONFIG_GLOBAL": "/dev/null"}
    subprocess.run(git_argv(dest, "add", "-A"), check=True, capture_output=True, env=env)
    subprocess.run(git_argv(dest, "commit", "-q", "-m", "engine-snapshot"),
                   check=True, capture_output=True, env=env)

    tracked = subprocess.run(
        ["git", "-C", str(dest), "ls-files", ".gongbang"],
        capture_output=True, text=True, env=GIT_ENV,
    ).stdout
    assert ".gongbang/jobC/probe/t1.log" not in tracked, (
        "pre-fix (plain 'git add -A') was expected to drop the gitignored "
        "record from the snapshot, but it is present -- the repro no "
        "longer demonstrates the historical bug")


# ---- 4. a ".py" probe target must not resolve to the engine's own ------
#         hidden venv interpreter

def test_4_resolved_probe_python_is_not_the_engines_own_hidden_venv(
    tmp_path: Path,
) -> None:
    from engine.application.acceptance_probe import (
        _probe_python_blocked, _resolve_system_python,
    )

    # A real, self-contained venv layout -- a COPIED interpreter binary
    # (never a symlink: this host's own venv happens to symlink straight
    # to the system python, which would make sys.executable's realpath
    # escape the venv root by itself and prove nothing) under a root the
    # probe sandbox would never mount, exactly like the checked-in venv
    # the real incident hit (`bwrap: execvp .../.venv/bin/python: No
    # such file or directory`).
    fake_venv_root = tmp_path / "engine-app" / ".venv"
    fake_venv_python = fake_venv_root / "bin" / "python"
    fake_venv_python.parent.mkdir(parents=True)
    real_python = shutil.which("python3") or sys.executable
    shutil.copy2(real_python, fake_venv_python)
    fake_venv_python.chmod(0o755)

    resolved = _resolve_system_python()

    # The historical bug: run_probe_scripts used sys.executable
    # unconditionally. That interpreter sits under the engine's own
    # checked-in venv, which the probe sandbox never mounts.
    assert _probe_python_blocked(str(fake_venv_python), (fake_venv_root,)) is True, (
        "a python3 under the engine's own venv root must be considered "
        "hidden -- otherwise this repro proves nothing")

    # The fix: a python3 resolved off PATH must not be blocked by that
    # same hidden root -- it is a real, different interpreter.
    assert _probe_python_blocked(resolved, (fake_venv_root,)) is False, (
        f"resolved system python {resolved!r} still falls under the "
        f"fake engine venv {fake_venv_root!r} -- the repro is void")


def test_4_red_sys_executable_unconditionally_was_the_pre_fix_argv(
    tmp_path: Path,
) -> None:
    """Confirms the pre-fix shape by reading it straight from git
    history (git show c9e5568^:engine/application/acceptance_probe.py --
    the parent of the fixing commit) rather than re-describing it: the
    old module's own `_PROBE_INTERPRETERS[".py"]` was `(sys.executable,)`
    with no resolution/hidden check at all -- so for the fake venv
    python built in test_4_resolved_probe_python_is_not_the_engines_own_
    hidden_venv above, that old, unconditional choice is exactly the
    hidden interpreter the fix no longer picks."""
    old_src = _source_at("c9e5568^", "engine/application/acceptance_probe.py")
    assert '".py": (sys.executable,)' in old_src, (
        "pre-fix acceptance_probe.py no longer has the unconditional "
        "sys.executable mapping this repro assumes -- check the commit range")
    assert "_resolve_system_python" not in old_src
    assert "_probe_python_blocked" not in old_src


@_needs_bwrap
def test_4_py_probe_target_runs_for_real_under_bwrap_without_the_hidden_venv(
    workspace, monkeypatch,
) -> None:
    """End-to-end under a real bwrap sandbox: a ".py" probe target must
    execute and print the exact mark the engine checks for, using an
    interpreter the sandbox actually mounts -- never
    "bwrap: execvp .../.venv/bin/python: No such file or directory"."""
    from dataclasses import replace

    from engine.application.acceptance_probe import run_probe_scripts
    from engine.steps.open_job import open_job

    config = replace(workspace.config, sandbox="bwrap")
    attempt_id = open_job(config, workspace.ledger, "alpha", "job4", "full",
                          roles=("builder",))
    probe_dir = workspace.alpha / ".gongbang" / "job4" / "probe"
    probe_dir.mkdir(parents=True)
    with serve_target() as url:
        (probe_dir / "t1.py").write_text(
            "import sys\n"
            "print('python says hi:', sys.version.split()[0])\n"
            + contacting_py_script(url),
            encoding="utf-8",
        )
        results = run_probe_scripts(
            config, workspace.ledger, "alpha", attempt_id, "job4", [("Some API", url)],
        )

    assert results[0].stdout_tail != "python_runtime_hidden", results[0].stdout_tail
    assert "python says hi" in results[0].stdout_tail
    assert results[0].ok is True


# ---- 5. a probe script that itself spawns a real CLI must find it ------
#         inside the probe sandbox

@_needs_bwrap
def test_5_probe_js_target_spawning_a_cli_finds_it_only_when_configured(
    workspace, monkeypatch, tmp_path: Path, probe_target,
) -> None:
    """Mirrors the live incident's second half exactly: a .js probe
    target shells out to a real CLI (standing in for `claude`/`codex`,
    neither of which this test may actually start) installed at a
    non-standard, per-user location like a real CLI install (never
    /usr/bin, which the base sandbox already exposes regardless of any
    policy -- that is why `sh` alone cannot reproduce this defect).
    Without probe_runtime_clis naming it, the probe sandbox hides it
    (bare ENOENT); with it named, the SAME mount policy a real worker
    gets (engine.application.steps.worker_sandbox.cli_runtime_mounts) applies and
    the script actually finds it. resolve_runtime's own PATH lookup is
    monkeypatched to point "fakecli" at a tiny real script -- exactly
    tests/unit/test_probe_runtime_clis.py's own established pattern --
    since this host has no actual `claude`/`codex` install to probe."""
    from dataclasses import replace

    from engine.application.acceptance_probe import run_probe_scripts
    from engine.application.steps import worker_sandbox
    from engine.infrastructure import sandbox as sandbox_mod2
    from engine.steps.open_job import open_job

    fake_bin = tmp_path / "fakecli-real-bin"
    fake_bin.write_text("#!/bin/sh\necho fakecli-found\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    fake_mounts = sandbox_mod2.RuntimeMounts(readonly=(fake_bin,))

    def fake_resolve_runtime(name: str) -> sandbox_mod2.RuntimeMounts:
        return fake_mounts if name == "fakecli" else sandbox_mod2.RuntimeMounts()

    url = probe_target("/cli")
    # After the CLI check, reach the target through the engine's proxy
    # (HTTP_PROXY) with node's own http client.
    script = (
        "const { spawnSync } = require('child_process');\n"
        "const http = require('http');\n"
        f"const r = spawnSync({json.dumps(str(fake_bin))}, []);\n"
        "if (r.error) { console.log('SPAWN_ERROR:' + r.error.code); }\n"
        "else { console.log(r.stdout.toString().trim());\n"
        "       const p = new URL(process.env.HTTP_PROXY);\n"
        f"       http.get({{host: p.hostname, port: p.port, path: {json.dumps(url)}}}, (res) => {{\n"
        "         res.resume();\n"
        f"         res.on('end', () => console.log({json.dumps('PROBE OK ' + url)})); }}); }}\n"
    )

    # (a) not configured -> the probe sandbox's default `readonly` set
    # never includes fake_bin's own directory (a per-user install path,
    # not a base-OS one), so the script cannot even exec it.
    attempt_a = open_job(replace(workspace.config, sandbox="bwrap"), workspace.ledger,
                         "alpha", "job5a", "full", roles=("builder",))
    probe_dir_a = workspace.alpha / ".gongbang" / "job5a" / "probe"
    probe_dir_a.mkdir(parents=True)
    (probe_dir_a / "t1.js").write_text(script, encoding="utf-8")
    results_a = run_probe_scripts(
        replace(workspace.config, sandbox="bwrap"), workspace.ledger, "alpha", attempt_a,
        "job5a", [("cli api", url)],
    )
    assert "SPAWN_ERROR" in results_a[0].stdout_tail, results_a[0].stdout_tail
    assert results_a[0].ok is False

    # (b) configured via probe_runtime_clis -> the real mount policy
    # applies. Run against "beta" (workspace's OTHER registered project,
    # not "alpha" again) -- "alpha" is still OPEN from job5a above, and
    # this repro does not need to close it to make its point.
    monkeypatch.setattr(worker_sandbox.sandbox, "resolve_runtime", fake_resolve_runtime)
    beta_with_cli = replace(workspace.config.projects["beta"], probe_runtime_clis=("fakecli",))
    config_b = replace(workspace.config, sandbox="bwrap",
                        projects={**workspace.config.projects, "beta": beta_with_cli})
    attempt_b = open_job(config_b, workspace.ledger, "beta", "job5b", "full",
                         roles=("builder",))
    probe_dir_b = workspace.beta / ".gongbang" / "job5b" / "probe"
    probe_dir_b.mkdir(parents=True)
    (probe_dir_b / "t1.js").write_text(script, encoding="utf-8")
    results_b = run_probe_scripts(
        config_b, workspace.ledger, "beta", attempt_b, "job5b",
        [("cli api", url)],
    )
    assert "SPAWN_ERROR" not in results_b[0].stdout_tail, results_b[0].stdout_tail
    assert "fakecli-found" in results_b[0].stdout_tail
    assert results_b[0].ok is True


# ---- 6. the prober prompt states the exact "PROBE OK <doc>" rule --------

def test_6_prober_prompt_names_the_exact_line_the_real_checker_requires() -> None:
    from engine.application.acceptance_probe import _PROBE_OK_MARK, _probe_observed
    from engine.application.stage_prompts import prober_prompt

    doc_url = "https://docs.example/some-api"
    text = prober_prompt("요청", "jobD", (("Some API", doc_url),))
    assert f"`{_PROBE_OK_MARK} <문서>`" in text
    assert doc_url in text

    # The real checker this prompt must satisfy: a script whose own
    # stdout ends with "PROBE OK <doc_url>" is what _probe_observed
    # actually requires (exit code 0 is checked separately by the
    # caller) -- exercised here for real, not re-derived.
    assert _probe_observed(0, f"ok\n{_PROBE_OK_MARK} {doc_url}\n", doc_url) is True
    assert _probe_observed(0, "ok\nPROBE OK\n", doc_url) is False


# ---- 7. a >131072-byte CLI prompt must never ride on argv ---------------

_MAX_ARG_STRLEN = 131072


def test_7_oversized_argv_element_fails_exec_with_e2big(tmp_path: Path) -> None:
    """Reproduces the historical mechanism directly against a real
    execve (no mock, no AI): a single argv element past Linux's
    MAX_ARG_STRLEN makes exec itself fail. This is what putting
    spec.task directly on argv (the pre-fix ClaudeRunner/CodexRunner
    shape) would still do today -- an OS limit, not something this
    worktree's own code could "revert" meaningfully."""
    oversized = "x" * (_MAX_ARG_STRLEN + 1000)
    with pytest.raises(OSError) as exc_info:
        subprocess.run(["/bin/true", oversized], capture_output=True)
    assert exc_info.value.errno == 7 or "Argument list too long" in str(exc_info.value)


def test_7_claude_and_codex_runners_never_put_the_task_on_argv() -> None:
    from engine.config import WorkerConfig
    from engine.infrastructure.workers.base import WorkerSpec
    from engine.infrastructure.workers.claude_cli import ClaudeRunner
    from engine.infrastructure.workers.codex_cli import CodexRunner

    big_task = "y" * (_MAX_ARG_STRLEN + 5000)
    spec = WorkerSpec(
        project="alpha", attempt_id="attempt1", generation=1, station="build",
        role="builder", copy_root=Path("/tmp/copy"), workdir=Path("/tmp/copy"),
        task=big_task, result_path=Path("/tmp/out.json"), config_path=None,
    )
    claude_worker = WorkerConfig(runner="claude", model=None)
    codex_worker = WorkerConfig(runner="codex", model=None)

    claude_argv = ClaudeRunner().argv(spec, claude_worker)
    assert big_task not in claude_argv
    assert ClaudeRunner.prompt_via_stdin is True

    codex_argv = CodexRunner().argv(spec, codex_worker)
    assert big_task not in codex_argv
    assert codex_argv[-1] == "-"
    assert CodexRunner.prompt_via_stdin is True


def test_7_a_150kb_prompt_really_round_trips_over_a_real_spawned_stdin(
    tmp_path: Path,
) -> None:
    """Real procs.spawn, a real subprocess, a prompt comfortably past
    MAX_ARG_STRLEN (150,000 bytes, matching the task's own requirement
    and close to the 169,674-byte real incident) -- fed over stdin_path
    exactly as worker_step/review now do for any runner with
    prompt_via_stdin=True, never as an argv element. The stand-in
    command is named "fake-cli", never "claude"/"codex" (tests/conftest.
    py's autouse _no_live_ai_cli guard refuses to spawn anything named
    either -- this test must not need GB2_LIVE=1)."""
    from engine.infrastructure import procs
    from engine.infrastructure.db.ledger import Ledger

    prompt = "P" * 150_000
    assert len(prompt.encode("utf-8")) > _MAX_ARG_STRLEN

    repo = tmp_path / "repo"
    repo.mkdir()
    ledger = Ledger.create(tmp_path / "ledger.sqlite")
    ledger.register_project("alpha")

    cat_bin = shutil.which("cat")
    assert cat_bin is not None, "no `cat` on PATH -- cannot do a real stdin spawn"

    stdout_path = tmp_path / "home" / "jobs" / "attempt1" / "stations" / "req1.stdout"
    stdin_path = procs.stdin_file_for(stdout_path)
    procs.write_stdin_file(stdin_path, prompt)

    proc = procs.spawn(
        ledger, "alpha", "attempt1", "build", [cat_bin], repo,
        stdout_path=stdout_path, stdin_path=stdin_path, request_id="req1",
    )
    proc.wait(timeout=10)

    echoed = stdout_path.read_text(encoding="utf-8")
    assert echoed == prompt
    assert len(echoed) == 150_000


# ---- 8. an internal station crash must not stick `gb2 continue` --------

def test_8_internal_crash_at_an_early_station_is_retried_by_continue(
    workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drives the real full_orbit/Processor machinery (ScriptedRunner-
    shaped station stubs, no live AI) through exactly the crash shape
    the real incident hit: a station's run_station raises a plain
    OSError from its second step after its dispatch step already
    journaled 'done' -- Processor._run_one's own catch-all then leaves
    the request 'unknown' rather than 'failed'. `gb2 continue` must
    retry that SAME station for real, not return the same stuck
    outcome forever."""
    import tests.unit.test_full_orbit as fo_tests
    from engine import full_orbit
    from engine.processor import Processor

    ws_full = fo_tests._full_orbit_workspace(workspace)
    job_name = "t9-early-plan-crash"
    plan_json = fo_tests._plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    queues = {
        "facts": [{f".gongbang/{job_name}/facts.md": "- 요청 확인: 통과 (출처: request)\n\n## 외부 대상\n(없음)\n"}],
        "plan": [fo_tests._plan_queue_entry(job_name, plan_json)],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    }
    stub, steps_fn = fo_tests._station_stub_with_one_internal_crash(queues, "plan")

    def factory(config, ledger, fakes) -> Processor:
        proc = Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", steps_fn)
        proc.register("review", fo_tests._review_always_passes_steps)
        return proc

    fo_tests._install_fake_acceptance_and_handoff(
        monkeypatch, [fo_tests._FakeAcceptanceResult(passed=True)])

    first = full_orbit.run_full_job(
        ws_full.config, ws_full.ledger, fo_tests._fakes(), "alpha", job_name, "파일 하나 만들어줘",
        processor_factory=factory,
    )
    assert first.status == "failed", first
    assert "internal:OSError" in first.reason, first

    second = full_orbit.continue_full_job(
        ws_full.config, ws_full.ledger, fo_tests._fakes(), "alpha", job_name,
        processor_factory=factory,
    )
    assert second.status == "done", second
    assert ("plan", "planner") in stub.calls
    assert (ws_full.alpha / "src/greeter/extra1.py").exists()


# ---- 9. (결함 10) plan-review rejection findings survive `gb2 continue` --

def test_9_plan_review_rejection_findings_survive_a_real_gb2_continue(
    workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real defect found testing a performance-ads job,
    separate from the 8 above): a plan review failed with a P1 finding
    (`evidence.record(kind="review", ...)`, ledger-recorded exactly as
    engine.application.steps.review.record_review does it for real) and
    `gb2 continue` reopened the SAME plan station on a fresh attempt --
    but the new planner's own prompt never mentioned the finding at
    all, so nothing stopped the same mistake recurring. Drives the real
    engine.application.pipeline_resume.continue_full_job (full_orbit.continue_full_job)
    against a real git app repo; only run_station/review are stubbed
    (same ScriptedRunner-shaped station-stub pattern every other test
    in this file and tests/unit/test_full_orbit.py use), never the
    resume/planning/review-evidence machinery itself."""
    import tests.unit.test_full_orbit as fo_tests
    from engine import full_orbit
    from engine.processor import Processor, StepContext, StepDef, StepFailed

    ws_full = fo_tests._full_orbit_workspace(workspace)
    job_name = "t9-plan-review-findings"
    plan_json = fo_tests._plan_json(tasks=[
        {"id": "t1", "title": "첫 파일", "detail": "", "owns": ["src/greeter/extra1.py"],
         "depends_on": []},
    ])
    stub = fo_tests._RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md":
                   "- 요청 확인: 통과 (출처: request)\n\n## 외부 대상\n(없음)\n"}],
        "plan": [fo_tests._plan_queue_entry(job_name, plan_json),
                 fo_tests._plan_queue_entry(job_name, plan_json)],
        "build": [{"src/greeter/extra1.py": "VALUE_1 = 1\n"}],
    })
    plan_task_texts: list[str] = []

    def steps_capturing_plan_task(ctx: StepContext) -> list[StepDef]:
        if str(ctx.request.payload.get("station")) == "plan":
            plan_task_texts.append(str(ctx.request.payload.get("task", "")))
        return stub.steps(ctx)

    review_calls = {"n": 0}
    finding = {"severity": "P1", "file": "src/greeter/extra1.py", "line": 7,
               "note": "import 순서가 잘못됐다"}

    def review_fails_once_with_finding_then_passes(ctx: StepContext) -> list[StepDef]:
        def _run(ctx: StepContext) -> dict[str, object]:
            review_calls["n"] += 1
            from engine import evidence

            project_config = ctx.config.projects[ctx.project]
            fp = evidence.fingerprint(project_config.repo, project_config.excludes)
            if review_calls["n"] == 1:
                evidence.record(
                    ctx.ledger, ctx.project, ctx.attempt_id, "review", fp,
                    payload={"passed": False, "request_id": ctx.request.request_id,
                             "findings": [finding]},
                )
                raise StepFailed("review_not_passed")
            evidence.record(
                ctx.ledger, ctx.project, ctx.attempt_id, "review", fp,
                payload={"passed": True, "request_id": ctx.request.request_id, "findings": []},
            )
            return {}

        def _reconcile(ctx: StepContext) -> bool:
            if review_calls["n"] == 1:
                return False
            for row in ctx.ledger.evidence(ctx.attempt_id, "review"):
                if row.payload.get("request_id") == ctx.request.request_id:
                    return True
            return False

        return [StepDef("record_review", _run, _reconcile)]

    def factory(config, ledger, fakes) -> Processor:
        proc = Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", steps_capturing_plan_task)
        proc.register("review", review_fails_once_with_finding_then_passes)
        return proc

    fo_tests._install_fake_acceptance_and_handoff(
        monkeypatch, [fo_tests._FakeAcceptanceResult(passed=True)])

    first = full_orbit.run_full_job(
        ws_full.config, ws_full.ledger, fo_tests._fakes(), "alpha", job_name, "파일 하나 만들어줘",
        processor_factory=factory,
    )
    assert first.status == "needs_owner"
    assert first.reason == "plan_review_failed"
    assert len(plan_task_texts) == 1
    assert "[지난 검토에서 고칠 것]" not in plan_task_texts[0]

    second = full_orbit.continue_full_job(
        ws_full.config, ws_full.ledger, fo_tests._fakes(), "alpha", job_name,
        processor_factory=factory,
    )
    assert second.status == "done", second
    assert len(plan_task_texts) == 2
    resumed_task = plan_task_texts[1]
    assert resumed_task.index("[지난 검토에서 고칠 것]") == 0
    assert "P1" in resumed_task
    assert "src/greeter/extra1.py:7" in resumed_task
    assert "import 순서가 잘못됐다" in resumed_task
    assert (ws_full.alpha / "src/greeter/extra1.py").exists()


# ---- 12. (결함 12) a prober may never land a write into facts.md --------

def test_12_prober_copy_touching_facts_md_fails_instead_of_corrupting_it(
    workspace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real incident (마케팅 채팅 job,: a prober's own
    worker copy carried an edited facts.md alongside its own probe log
    -- station_owners grants "probe" the whole ".gongbang/" tree
    (FULL_STATION_OWNERS below, the real app's own shape), so merge's
    config-driven prefix alone never caught it, and the corrupted
    facts.md later choked check_probe_targets with
    probe_targets_invalid:malformed_probe_target_line, wedging the job.

    Drives the real engine.application.pipeline_planning/pipeline_stages
    machinery (full_orbit.run_full_job) through a real git app repo;
    only run_station/review are stubbed (same ScriptedRunner-shaped
    station-stub pattern every other test in this file uses). The fix
    (engine.domain.stage_checks.station_record_prefixes +
    engine.application.steps.merge._deny_if_other_station_record) must
    refuse the prober's merge outright, before facts.md is ever
    touched -- the job fails loudly at the probe station instead of
    silently corrupting its own trail and wedging later."""
    import tests.unit.test_full_orbit as fo_tests
    from engine import full_orbit

    ws_full = fo_tests._full_orbit_workspace(workspace)
    job_name = "t12-prober-touches-facts"
    good_facts = (
        "- 요청 확인: 통과 (출처: request)\n\n"
        "## 외부 대상\n- 어떤 API: https://example.com/docs\n"
    )
    stub = fo_tests._RunStationStub({
        "facts": [{f".gongbang/{job_name}/facts.md": good_facts}],
        "probe": [{
            f".gongbang/{job_name}/probe/t1.log": "PROBE OK https://example.com/docs\n",
            # The actual incident: the prober's own copy also carries an
            # edited facts.md -- station "facts"'s own record, not
            # "probe"'s.
            f".gongbang/{job_name}/facts.md": good_facts.replace(
                "어떤 API: https://example.com/docs",
                "어떤 API: https://example.com/docs (probe = 성공, 실제 호출 결과)",
            ),
        }],
    })

    def factory(config, ledger, fakes):
        from engine.processor import Processor
        proc = Processor(config, ledger, fakes)
        proc.register_defaults()
        proc.register("run_station", stub.steps)
        proc.register("review", fo_tests._review_always_passes_steps)
        return proc

    fo_tests._install_fake_acceptance_and_handoff(
        monkeypatch, [fo_tests._FakeAcceptanceResult(passed=True)])

    outcome = full_orbit.run_full_job(
        ws_full.config, ws_full.ledger, fo_tests._fakes(), "alpha", job_name, "파일 하나 만들어줘",
        processor_factory=factory,
    )

    assert outcome.status == "failed", outcome
    assert outcome.reason is not None
    assert outcome.reason.startswith("probe:wrote_other_station_record:")
    assert f".gongbang/{job_name}/facts.md" in outcome.reason
    # facts.md as committed by the earlier facts station must be
    # untouched -- the prober's corrupting edit never landed.
    assert (ws_full.alpha / f".gongbang/{job_name}/facts.md").read_text(
        encoding="utf-8") == good_facts
    denied = [e for _seq, e in ws_full.ledger.events("alpha", "merge.denied")
              if e.payload.get("reason") == "wrote_other_station_record"]
    assert denied and denied[0].payload["station"] == "probe"

