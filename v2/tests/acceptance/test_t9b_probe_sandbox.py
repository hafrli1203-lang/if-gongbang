"""T9b -- two more "real-condition" regression locks against the probe
station's own sandbox/parsing policy, split out of test_t9_real_
conditions.py (that file's own 800-line budget -- docs/standards/
engine-layout.md) rather than appended there. Same style as that file:
real git repos/workspace fixture, a real bwrap sandbox when available,
no live AI anywhere.

Both defects come from the SAME real job (마케팅 채팅, 실측
재실행 -- a probe acceptance re-run against the real app's own ledger,
`probe.executed` events read-only):

  19. an "## 외부 대상" bullet's doc value was an http(s) URL with a
      human explanation appended on the SAME line -- the parsed "url"
      then never matched the probe script's own real "PROBE OK <url>"
      stdout line, failing a target whose probe had actually
      succeeded                                                test_19_*
  20. a probe target's own probe_runtime_clis CLI (standing in for
      `codex`) got its disposable probe $HOME mounted wholesale
      read-only, with none of that CLI's own home_writable_subpaths
      (e.g. ".codex") granted writable the way a real worker already
      gets under its own fresh home -- the CLI then failed "Read-only
      file system" writing its own session state, which proves
      nothing about whatever it would otherwise have done        test_20_*
"""
from __future__ import annotations

import pytest

from engine.infrastructure import sandbox as sandbox_mod

_needs_bwrap = pytest.mark.skipif(
    not sandbox_mod.available(), reason="bwrap not available on this host",
)


# ---- 19. an "## 외부 대상" doc URL with a trailing explanation on the ---
#          SAME line must still match the real probe script's own
#          "PROBE OK <url>" output

def test_19_doc_url_with_trailing_explanation_still_matches_the_real_probe(
    workspace,
) -> None:
    """The real incident (마케팅 채팅 job, 실측 재실행): a
    real facts.md bullet read "- Claude Code CLI(...): https://docs.
    claude.com/en/docs/claude-code/cli-reference -- probe t1 PROBE OK
    (단, ...)" -- an http(s) URL with a human explanation appended on
    the SAME line. The real probe script still only ever prints
    "PROBE OK <bare url>" (PROBE_EXIT_AND_OK_LINE_RULE), so the engine's
    own url -- parsed by check_probe_targets -- must be the bare url
    alone, not that whole trailing string, or every such target fails
    even though its probe actually succeeded."""
    from engine.application.acceptance_probe import run_probe_scripts
    from engine.domain.stage_checks import check_probe_targets
    from engine.steps.open_job import open_job

    doc_url = "https://docs.claude.com/en/docs/claude-code/cli-reference"
    facts_text = (
        "## 외부 대상\n"
        f"- Claude Code CLI(코드 실행): {doc_url} "
        "-- probe t1 PROBE OK(단, 실측 환경 한정)\n"
    )
    facts_dir = workspace.alpha / ".gongbang" / "job19"
    facts_dir.mkdir(parents=True)
    (facts_dir / "facts.md").write_text(facts_text, encoding="utf-8")

    targets = check_probe_targets(workspace.alpha, "job19")
    assert targets == [("Claude Code CLI(코드 실행)", doc_url)], (
        "check_probe_targets must drop the trailing explanation and "
        "keep only the bare url -- otherwise this repro proves nothing")

    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "job19",
                          "full", roles=("builder",))
    probe_dir = workspace.alpha / ".gongbang" / "job19" / "probe"
    probe_dir.mkdir(parents=True)
    # The real script a prober would actually write: it only ever knows
    # (and prints) the bare address, never the planner's own trailing
    # explanation.
    (probe_dir / "t1.py").write_text(
        f"print('PROBE OK {doc_url}')\n", encoding="utf-8")

    results = run_probe_scripts(
        workspace.config, workspace.ledger, "alpha", attempt_id, "job19",
        targets,
    )

    assert results[0].ok is True, results[0].stdout_tail
    assert results[0].returncode == 0


def test_19_red_the_unsplit_url_never_matches_the_scripts_real_output() -> None:
    """Demonstrates the pre-fix failure mode directly: the OLD url (the
    whole string after ":", explanation included -- what `body.
    partition(":")` alone, with no split, used to return) never matches
    `_PROBE_OK_MARK + " " + url` against the script's own real stdout,
    even though the script's bare address is correct and its probe
    genuinely succeeded."""
    from engine.application.acceptance_probe import _probe_observed

    doc_url = "https://docs.claude.com/en/docs/claude-code/cli-reference"
    old_unsplit_url = f"{doc_url} -- probe t1 PROBE OK(단, 실측 환경 한정)"
    real_script_stdout = f"PROBE OK {doc_url}\n"

    assert _probe_observed(0, real_script_stdout, old_unsplit_url) is False, (
        "the pre-fix (unsplit) url was expected to fail this match -- "
        "the repro no longer demonstrates the historical bug")
    assert _probe_observed(0, real_script_stdout, doc_url) is True


# ---- 20. a probe's own probe_runtime_clis CLI needs the SAME home- ------
#          writable-subpaths a real worker already gets

@_needs_bwrap
def test_20_probe_runtime_cli_gets_its_own_home_writable_subpath(
    workspace, monkeypatch,
) -> None:
    """The real incident (마케팅 채팅 job, 실측 재실행): a
    real `codex exec` run as a probe's own configured probe_runtime_
    clis CLI failed "Read-only file system (os error 30) ... failed to
    initialize in-process app-server client" -- its disposable probe
    $HOME (never the real operator HOME) was mounted wholesale
    read-only, with none of CodexRunner.home_writable_subpaths
    (".codex") granted writable, unlike a real worker running the same
    CLI (engine.application.steps.worker_sandbox.sandboxed_argv already
    grants its OWN fresh worker-home that writable subpath).

    A real `codex`/`claude` CLI is never started here -- a tiny real
    shell script stands in for one, doing the ONE thing that matters:
    writing into its own $HOME/.codex, the shape a real CLI's own
    session/credential write takes. `cli_name="codex"` is kept literal
    (not a fake name) so this exercises the REAL CLI_HOME_WRITABLE_
    SUBPATHS["codex"] lookup, not a name this repro invented."""
    from dataclasses import replace

    from engine.application.acceptance_probe import run_probe_scripts
    from engine.application.steps import worker_sandbox
    from engine.infrastructure import sandbox as sandbox_mod2
    from engine.steps.open_job import open_job

    # Never touch the operator's own real ~/.codex -- cli_runtime_mounts
    # reads os.environ["HOME"] directly (trusted-engine-process reasoning,
    # see acceptance_bench.run_in_bench_sandbox's own docstring), so this
    # test must point it at an empty, throwaway directory instead.
    monkeypatch.setenv("HOME", str(workspace.root / "fake-real-home"))

    fake_bin = workspace.root / "fake-codex-bin"
    fake_bin.write_text(
        "#!/bin/sh\n"
        'mkdir -p "$HOME/.codex" && echo wrote > "$HOME/.codex/started" '
        "&& echo CODEX_WROTE_OK\n",
        encoding="utf-8",
    )
    fake_bin.chmod(0o755)

    def fake_resolve_runtime(name: str) -> sandbox_mod2.RuntimeMounts:
        return sandbox_mod2.RuntimeMounts(readonly=(fake_bin,)) if name == "codex" \
            else sandbox_mod2.RuntimeMounts()

    monkeypatch.setattr(worker_sandbox.sandbox, "resolve_runtime", fake_resolve_runtime)

    script = (
        "import subprocess\n"
        f"r = subprocess.run([{str(fake_bin)!r}], capture_output=True, text=True)\n"
        "print((r.stdout or '') + (r.stderr or ''))\n"
        "if 'CODEX_WROTE_OK' in (r.stdout or ''):\n"
        "    print('PROBE OK https://docs.example/codex')\n"
    )

    config = replace(
        workspace.config, sandbox="bwrap",
        projects={name: replace(p, probe_runtime_clis=("codex",))
                  for name, p in workspace.config.projects.items()},
    )

    def _run(project: str, job_name: str) -> tuple:
        # A separate PROJECT per call (never the same one twice): open_job
        # refuses a second `open_job` on a project that is still OPEN from
        # an earlier attempt in this same test, and this repro has no
        # reason to close one first.
        attempt_id = open_job(config, workspace.ledger, project, job_name, "full",
                              roles=("builder",))
        repo = workspace.alpha if project == "alpha" else workspace.beta
        probe_dir = repo / ".gongbang" / job_name / "probe"
        probe_dir.mkdir(parents=True)
        (probe_dir / "t1.py").write_text(script, encoding="utf-8")
        return run_probe_scripts(
            config, workspace.ledger, project, attempt_id, job_name,
            [("codex cli", "https://docs.example/codex")],
        )

    # RED: reproduces the pre-fix shape by forcing CLI_HOME_WRITABLE_
    # SUBPATHS to grant nothing (결함 20's own actual pre-fix table,
    # which always passed writable_subpaths=() to prepare_worker_home
    # and returned no writable mount at all) -- the fake codex's own
    # mkdir into $HOME/.codex must then fail closed.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(worker_sandbox, "CLI_HOME_WRITABLE_SUBPATHS", {})
        results_red = _run("alpha", "job20red")
    assert "CODEX_WROTE_OK" not in results_red[0].stdout_tail, results_red[0].stdout_tail
    assert results_red[0].ok is False

    # GREEN: the real, fixed table -- the fake codex's own $HOME/.codex
    # write must now succeed, the same way a real worker's own CLI
    # write already does under sandboxed_argv.
    results_green = _run("beta", "job20green")
    assert "CODEX_WROTE_OK" in results_green[0].stdout_tail, results_green[0].stdout_tail
    assert results_green[0].ok is True
