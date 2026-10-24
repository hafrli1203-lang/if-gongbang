"""Unit tests for engine.steps.bench_run: the sample greeter app's
variant 2 (reproducible bug) drives the real-test-run + trust-check
path end to end."""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.processor import Fakes, Processor
from engine.processor import submit as submit_request
from engine.steps import bench_run
from engine.steps.open_job import open_job
from tests.conftest import GIT_ENV, Workspace, git
from tests.fixtures.sample_app import make_sample


def _processor(workspace: Workspace) -> Processor:
    fakes = Fakes(publisher=FakePublisher(), installer=FakeInstaller())
    proc = Processor(config=workspace.config, ledger=workspace.ledger, fakes=fakes,
                      holder="p1")
    proc.register("bench", bench_run.steps)
    return proc


def _bench_workspace(workspace: Workspace, variant: int) -> Workspace:
    """Repoint the 'alpha' project at a fresh sample app repo (variant),
    re-register it, and keep the rest of workspace's fixture (ledger,
    home) intact."""
    sample = make_sample(workspace.root / "sample", variant, Path(sys.executable))
    project_config = replace(
        workspace.config.projects["alpha"],
        repo=sample.repo, test_cmd=sample.test_cmd,
        install_cmd=sample.install_cmd, manifest=sample.manifest,
        bench_cases=sample.bench_cases,
    )
    new_config = replace(
        workspace.config,
        projects={**workspace.config.projects, "alpha": project_config},
    )
    return Workspace(workspace.root, new_config, workspace.ledger,
                     sample.repo, workspace.beta)


def _submit_bench(workspace: Workspace) -> tuple[str, str]:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "bench-job",
                           "full", ("builder",))
    rid = submit_request(workspace.ledger, "alpha", attempt_id, "bench", {}, "owner")
    return attempt_id, rid


def test_bench_fails_before_fix_with_assertion_signature(workspace: Workspace) -> None:
    ws = _bench_workspace(workspace, variant=2)
    attempt_id, rid = _submit_bench(ws)
    proc = _processor(ws)
    n = proc.run_once("alpha")
    assert n == 0
    req = ws.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "case_failed"}
    bench_evidence = ws.ledger.evidence(attempt_id, "bench")
    assert len(bench_evidence) == 1
    assert bench_evidence[0].payload["passed"] is False
    assert bench_evidence[0].payload["reason"] == "case_failed"
    assert bench_evidence[0].payload["per_case"]["initials_bug"] is False


def test_bench_passes_after_fix(workspace: Workspace) -> None:
    ws = _bench_workspace(workspace, variant=2)
    # Fix the off-by-one bug directly in the repo (simulating a worker's
    # edit), matching every word's initial.
    init_py = ws.alpha / "src" / "greeter" / "__init__.py"
    fixed = init_py.read_text(encoding="utf-8").replace(
        "words[:1]", "words"
    )
    init_py.write_text(fixed, encoding="utf-8")

    attempt_id, rid = _submit_bench(ws)
    proc = _processor(ws)
    n = proc.run_once("alpha")
    assert n == 1
    req = ws.ledger.request(rid)
    assert req.status == "done"
    bench_evidence = ws.ledger.evidence(attempt_id, "bench")
    assert len(bench_evidence) == 1
    assert bench_evidence[0].payload["passed"] is True


def test_bench_requires_negative_control(workspace: Workspace) -> None:
    ws = _bench_workspace(workspace, variant=2)
    cases_path = ws.alpha / "bench" / "cases.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    only_real = [c for c in cases if not c["expect_fail"]]
    cases_path.write_text(json.dumps(only_real), encoding="utf-8")

    attempt_id, rid = _submit_bench(ws)
    proc = _processor(ws)
    proc.run_once("alpha")
    req = ws.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "no_negative_control"}


def test_bench_evidence_fp_rechecked(workspace: Workspace, monkeypatch) -> None:
    """A file changed after compute_fingerprint (simulating a worker
    editing the repo mid-bench) must stop record_bench from recording
    evidence or succeeding."""
    ws = _bench_workspace(workspace, variant=2)
    attempt_id, rid = _submit_bench(ws)
    proc = _processor(ws)

    import engine.steps.bench_run as bench_run_module
    real_run_trust_checks = bench_run_module._run_trust_checks

    def _mutate_then_run(ctx):
        (ws.alpha / "new_file.txt").write_text("surprise\n", encoding="utf-8")
        return real_run_trust_checks(ctx)

    monkeypatch.setattr(bench_run_module, "_run_trust_checks", _mutate_then_run)
    n = proc.run_once("alpha")
    assert n == 0
    req = ws.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "tree_changed_during_bench"}
    assert ws.ledger.evidence(attempt_id, "bench") == []


def test_bench_module_not_found_is_not_reproduction(workspace: Workspace) -> None:
    """variant 3 (dependency missing): a case EXPECTED to reproduce an
    "assertion" failure, that instead fails because the dependency is
    missing (ModuleNotFoundError), must not be accepted as a match --
    a missing dependency is a different failure than the check firing,
    so this is wrong_failure_signature, not a passing negative control."""
    ws = _bench_workspace(workspace, variant=3)
    cases_path = ws.alpha / "bench" / "cases.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    cases.append({
        "name": "remote_case",
        "selector": "tests/test_remote.py::test_fetch_status",
        "expect_fail": True,
        "expected_signature": "assertion",
    })
    cases_path.write_text(json.dumps(cases), encoding="utf-8")
    subprocess.run(["git", "-C", str(ws.alpha), "add", "-A"], check=True, env=GIT_ENV)
    subprocess.run(["git", "-C", str(ws.alpha), "commit", "-q", "-m", "cases"],
                    check=True, env=GIT_ENV)

    attempt_id, rid = _submit_bench(ws)
    proc = _processor(ws)
    proc.run_once("alpha")
    req = ws.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "wrong_failure_signature"}
    bench_evidence = ws.ledger.evidence(attempt_id, "bench")
    assert bench_evidence[0].payload["per_case"]["remote_case"] is False


def test_bench_fails_when_suite_fails_but_cases_pass(workspace: Workspace) -> None:
    """plan §5: passed = tests_ok and trust.passed. A test outside the
    trust-checked cases (so trust_checks itself is clean) that fails the
    full test_cmd run must still fail the overall bench, with its own
    reason ("tests_failed") distinct from a trust-check failure."""
    ws = _bench_workspace(workspace, variant=1)
    (ws.alpha / "tests" / "test_extra.py").write_text(
        "def test_unrelated_failure():\n    assert False\n", encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(ws.alpha), "add", "-A"], check=True, env=GIT_ENV)
    subprocess.run(["git", "-C", str(ws.alpha), "commit", "-q", "-m", "extra"],
                    check=True, env=GIT_ENV)

    attempt_id, rid = _submit_bench(ws)
    proc = _processor(ws)
    n = proc.run_once("alpha")
    assert n == 0
    req = ws.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "tests_failed"}
    bench_evidence = ws.ledger.evidence(attempt_id, "bench")
    assert len(bench_evidence) == 1
    assert bench_evidence[0].payload["passed"] is False
    assert bench_evidence[0].payload["reason"] == "tests_failed"
    assert bench_evidence[0].payload["tests_returncode"] != 0
    # the trust-checked cases themselves are unaffected by the unrelated
    # failing test (it is not referenced by any case's selector)
    assert bench_evidence[0].payload["per_case"] == {
        "greet": True, "negative_control": False,
    }


def test_case_env_applied_only_to_that_case(workspace: Workspace) -> None:
    """The negative control's cases.json "env" is applied only to that
    case's own subprocess (trust_checks); run_tests (the full suite,
    plain os.environ) must skip rather than fail it, so a healthy repo
    still gets a passing bench."""
    ws = _bench_workspace(workspace, variant=1)
    attempt_id, rid = _submit_bench(ws)
    proc = _processor(ws)
    n = proc.run_once("alpha")
    assert n == 1
    req = ws.ledger.request(rid)
    assert req.status == "done"
    bench_evidence = ws.ledger.evidence(attempt_id, "bench")
    assert bench_evidence[0].payload["passed"] is True
    assert bench_evidence[0].payload["tests_returncode"] == 0
    # the control case still ran (under its own env) and failed as designed
    assert bench_evidence[0].payload["per_case"]["negative_control"] is False


def test_run_tests_does_not_leak_processor_environment(workspace: Workspace, monkeypatch) -> None:
    """run_tests must build the test_cmd subprocess's environment from
    config.worker_env_passthrough only, never the processor's own full
    os.environ -- a secret set in the processor's environment (an API
    key, a token) must not reach a project's test command."""
    monkeypatch.setenv("SOME_SECRET", "leaked-value")
    ws = _bench_workspace(workspace, variant=1)
    probe_cmd = (sys.executable, "-c",
                 "import os, sys; sys.exit(1 if 'SOME_SECRET' in os.environ else 0)")
    project_config = replace(ws.config.projects["alpha"], test_cmd=probe_cmd)
    ws = Workspace(
        ws.root,
        replace(ws.config, projects={**ws.config.projects, "alpha": project_config}),
        ws.ledger, ws.alpha, ws.beta,
    )

    attempt_id, rid = _submit_bench(ws)
    proc = _processor(ws)
    proc.run_once("alpha")
    bench_evidence = ws.ledger.evidence(attempt_id, "bench")
    assert len(bench_evidence) == 1
    assert bench_evidence[0].payload["tests_returncode"] == 0


def test_run_tests_timeout_uses_configured_test_timeout_sec(workspace: Workspace) -> None:
    """test_timeout_sec comes from ProjectConfig, not a hardcoded module
    constant: a short configured timeout with a test_cmd that sleeps
    longer must fail run_tests with "tests_timeout"."""
    ws = _bench_workspace(workspace, variant=1)
    slow_cmd = (sys.executable, "-c", "import time; time.sleep(5)")
    project_config = replace(
        ws.config.projects["alpha"], test_cmd=slow_cmd, test_timeout_sec=0.2,
    )
    ws = Workspace(
        ws.root,
        replace(ws.config, projects={**ws.config.projects, "alpha": project_config}),
        ws.ledger, ws.alpha, ws.beta,
    )

    attempt_id, rid = _submit_bench(ws)
    proc = _processor(ws)
    n = proc.run_once("alpha")
    assert n == 0
    req = ws.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "tests_timeout"}
    assert ws.ledger.evidence(attempt_id, "bench") == []


def test_build_related_argv_expands_files_token() -> None:
    from engine.steps.bench_run import _build_related_argv
    argv = _build_related_argv(
        ("npx", "vitest", "related", "--run", "{files}"),
        ["src/a.ts", "src/b.ts"],
    )
    assert argv == ["npx", "vitest", "related", "--run", "src/a.ts", "src/b.ts"]
    # no {files} token at all: nothing to expand, argv passes through.
    assert _build_related_argv(("npx", "vitest", "run"), ["src/a.ts"]) == [
        "npx", "vitest", "run",
    ]


def test_related_scope_runs_related_cmd_against_changed_files(workspace: Workspace) -> None:
    """bench_scope="related" with related_cmd configured and a changed
    source file present: run_tests runs related_cmd (not test_cmd), with
    {files} expanded to that changed file, and journals scope="related"
    plus the exact argv -- carried into the bench evidence payload."""
    ws = _bench_workspace(workspace, variant=1)
    # A probe script that writes its own argv to a marker file, so the
    # test can see exactly what command ran without a real vitest.
    marker = ws.root / "argv_seen.json"
    probe = (
        sys.executable, "-c",
        "import json,sys; open(sys.argv[1], 'w').write(json.dumps(sys.argv[2:]))",
        str(marker), "{files}",
    )
    changed_file = ws.alpha / "src" / "greeter" / "__init__.py"
    changed_file.write_text(changed_file.read_text(encoding="utf-8") + "\n# touched\n",
                            encoding="utf-8")
    project_config = replace(
        ws.config.projects["alpha"], bench_scope="related", related_cmd=probe,
    )
    ws = Workspace(
        ws.root, replace(ws.config, projects={**ws.config.projects, "alpha": project_config}),
        ws.ledger, ws.alpha, ws.beta,
    )

    attempt_id, rid = _submit_bench(ws)
    proc = _processor(ws)
    proc.run_once("alpha")

    seen_argv = json.loads(marker.read_text(encoding="utf-8"))
    assert seen_argv == ["src/greeter/__init__.py"]
    bench_evidence = ws.ledger.evidence(attempt_id, "bench")
    assert bench_evidence[0].payload["scope"] == "related"
    assert bench_evidence[0].payload["argv"][-1] == "src/greeter/__init__.py"


def test_related_scope_falls_back_to_full_when_a_file_was_deleted(workspace: Workspace) -> None:
    """A deletion mixed in with an ordinary edit (e.g. a runtime-loaded
    module removed, plus an unrelated existing file touched) must
    force "full" scope, not "related" -- a related-tests command
    driven only by paths that STILL exist on disk would never pick up
    the deleted path at all, so code elsewhere that depended on it
    (not the edited file) could go unexercised while scope stays
    "related" and passes anyway."""
    ws = _bench_workspace(workspace, variant=1)
    marker = ws.root / "argv_seen.json"
    probe = (
        sys.executable, "-c",
        "import json,sys; open(sys.argv[1], 'w').write(json.dumps(sys.argv[2:]))",
        str(marker), "{files}",
    )
    changed_file = ws.alpha / "src" / "greeter" / "__init__.py"
    changed_file.write_text(changed_file.read_text(encoding="utf-8") + "\n# touched\n",
                            encoding="utf-8")
    deleted_file = ws.alpha / "src" / "greeter" / "farewell.py"
    deleted_file.write_text("def bye():\n    return 'bye'\n", encoding="utf-8")
    git(ws.alpha, "add", "-A")
    git(ws.alpha, "commit", "-q", "-m", "add farewell.py")
    deleted_file.unlink()
    project_config = replace(
        ws.config.projects["alpha"], bench_scope="related", related_cmd=probe,
    )
    ws = Workspace(
        ws.root, replace(ws.config, projects={**ws.config.projects, "alpha": project_config}),
        ws.ledger, ws.alpha, ws.beta,
    )

    attempt_id, rid = _submit_bench(ws)
    proc = _processor(ws)
    n = proc.run_once("alpha")
    assert n == 1
    bench_evidence = ws.ledger.evidence(attempt_id, "bench")
    assert bench_evidence[0].payload["scope"] == "full"
    assert bench_evidence[0].payload["argv"] == list(ws.config.projects["alpha"].test_cmd)
    assert not marker.exists()  # related_cmd (the probe) never ran


def test_related_scope_falls_back_to_full_when_no_changed_files(workspace: Workspace) -> None:
    """related_cmd is configured, but nothing has changed since HEAD (a
    freshly opened job with a clean tree): no changed source files ->
    falls back to the full test_cmd, scope recorded as "full"."""
    ws = _bench_workspace(workspace, variant=1)
    project_config = replace(
        ws.config.projects["alpha"], bench_scope="related",
        related_cmd=(sys.executable, "-c", "import sys; sys.exit(1)", "{files}"),
    )
    ws = Workspace(
        ws.root, replace(ws.config, projects={**ws.config.projects, "alpha": project_config}),
        ws.ledger, ws.alpha, ws.beta,
    )

    attempt_id, rid = _submit_bench(ws)
    proc = _processor(ws)
    n = proc.run_once("alpha")
    assert n == 1  # the full test_cmd (sample app's own, passing) ran -- not the exit(1) stub
    bench_evidence = ws.ledger.evidence(attempt_id, "bench")
    assert bench_evidence[0].payload["scope"] == "full"
    assert bench_evidence[0].payload["argv"] == list(ws.config.projects["alpha"].test_cmd)


def test_related_scope_falls_back_to_full_when_related_cmd_ran_zero_tests(
    workspace: Workspace,
) -> None:
    """A related_cmd that LOOKS like it ran successfully (exit 0) but
    actually executed zero tests (vitest's own "No test files found"
    output, e.g. because the configured selector no longer matches
    anything real) must never be trusted as "nothing needed checking"
    -- the engine falls back to the full test_cmd and records the
    distinct scope "full(fallback)", never a plain "related" pass."""
    ws = _bench_workspace(workspace, variant=1)
    changed_file = ws.alpha / "src" / "greeter" / "__init__.py"
    changed_file.write_text(changed_file.read_text(encoding="utf-8") + "\n# touched\n",
                            encoding="utf-8")
    probe = (
        sys.executable, "-c",
        "import sys; print('No test files found, exiting with code 1'); sys.exit(0)",
        "{files}",
    )
    project_config = replace(
        ws.config.projects["alpha"], bench_scope="related", related_cmd=probe,
    )
    ws = Workspace(
        ws.root, replace(ws.config, projects={**ws.config.projects, "alpha": project_config}),
        ws.ledger, ws.alpha, ws.beta,
    )

    attempt_id, rid = _submit_bench(ws)
    proc = _processor(ws)
    n = proc.run_once("alpha")
    assert n == 1  # the full test_cmd (sample app's own, passing) ran
    bench_evidence = ws.ledger.evidence(attempt_id, "bench")
    assert bench_evidence[0].payload["scope"] == "full(fallback)"
    assert bench_evidence[0].payload["argv"] == list(ws.config.projects["alpha"].test_cmd)


def test_related_scope_falls_back_to_full_when_every_selected_test_was_skipped(
    workspace: Workspace,
) -> None:
    """A related_cmd whose selector DID match files, but vitest then
    skipped every one of them (no passed/failed outcome at all, just
    "Tests  1 skipped (1)") must fall back the same way a zero-match
    selector does -- a selector matching something is not the same as
    anything having actually been exercised."""
    ws = _bench_workspace(workspace, variant=1)
    changed_file = ws.alpha / "src" / "greeter" / "__init__.py"
    changed_file.write_text(changed_file.read_text(encoding="utf-8") + "\n# touched\n",
                            encoding="utf-8")
    probe = (
        sys.executable, "-c",
        "import sys; print(' Test Files  1 skipped (1)\\n'"
        " '      Tests  1 skipped (1)'); sys.exit(0)",
        "{files}",
    )
    project_config = replace(
        ws.config.projects["alpha"], bench_scope="related", related_cmd=probe,
    )
    ws = Workspace(
        ws.root, replace(ws.config, projects={**ws.config.projects, "alpha": project_config}),
        ws.ledger, ws.alpha, ws.beta,
    )

    attempt_id, rid = _submit_bench(ws)
    proc = _processor(ws)
    n = proc.run_once("alpha")
    assert n == 1  # the full test_cmd (sample app's own, passing) ran
    bench_evidence = ws.ledger.evidence(attempt_id, "bench")
    assert bench_evidence[0].payload["scope"] == "full(fallback)"
    assert bench_evidence[0].payload["argv"] == list(ws.config.projects["alpha"].test_cmd)


def test_bench_scope_full_ignores_related_cmd(workspace: Workspace) -> None:
    """bench_scope="full" always runs test_cmd, even with related_cmd
    configured and a changed file present."""
    ws = _bench_workspace(workspace, variant=1)
    changed_file = ws.alpha / "src" / "greeter" / "__init__.py"
    changed_file.write_text(changed_file.read_text(encoding="utf-8") + "\n# touched\n",
                            encoding="utf-8")
    project_config = replace(
        ws.config.projects["alpha"], bench_scope="full",
        related_cmd=(sys.executable, "-c", "import sys; sys.exit(1)", "{files}"),
    )
    ws = Workspace(
        ws.root, replace(ws.config, projects={**ws.config.projects, "alpha": project_config}),
        ws.ledger, ws.alpha, ws.beta,
    )

    attempt_id, rid = _submit_bench(ws)
    proc = _processor(ws)
    n = proc.run_once("alpha")
    assert n == 1
    bench_evidence = ws.ledger.evidence(attempt_id, "bench")
    assert bench_evidence[0].payload["scope"] == "full"


def test_case_signature_uses_error_lines_not_source_text() -> None:
    from engine.steps.bench_run import _case_signature
    importerr = ("tests/t.py:1: in <module>\n    from missing_pkg import AssertionError\n"
                 "E   ModuleNotFoundError: No module named 'missing_pkg'\n")
    assert _case_signature({"text": importerr, "returncode": 2}) == "ModuleNotFoundError"
    assert _case_signature({"text": ">       assert False\nE       assert False\n", "returncode": 1}) == "assertion"
    assert _case_signature({"text": "E   AssertionError: boom\n", "returncode": 1}) == "assertion"
    assert _case_signature({"text": "something else\n", "returncode": 3}) == "exit:3"


def test_case_signature_classifies_vitest_output() -> None:
    """vitest never prints a pytest-shaped `E   ...` line, so these all
    fall through to the vitest-specific checks. A run that collected
    zero tests must be "no_tests", NEVER a generic "exit:N" that could
    accidentally equal a negative control's own configured
    expected_signature without its assertion ever having run."""
    from engine.steps.bench_run import _case_signature
    no_tests = " No test files found, exiting with code 1\n"
    assert _case_signature({"text": no_tests, "returncode": 1}) == "no_tests"
    real_failure = (
        "FAIL  src/greeter.test.ts > greets\n"
        "AssertionError: expected 'hi' to be 'bye'\n"
        " Test Files  1 failed (1)\n"
        "      Tests  1 failed (1)\n"
    )
    assert _case_signature({"text": real_failure, "returncode": 1}) == "assertion"
    # Some OTHER thrown error (not an assertion at all) must not be
    # misclassified as "assertion" just because the run also failed.
    other_error = (
        "FAIL  src/greeter.test.ts > greets\n"
        "TypeError: cannot read property of undefined\n"
        "      Tests  1 failed (1)\n"
    )
    assert _case_signature({"text": other_error, "returncode": 1}) == "exit:1"


def test_ran_zero_tests_detects_no_files_and_all_skipped() -> None:
    from engine.steps.bench_run import _ran_zero_tests
    assert _ran_zero_tests("related", "No test files found, exiting with code 1\n")
    assert _ran_zero_tests("related", " Test Files  0 \n      Tests  0 \n")
    assert _ran_zero_tests(
        "related", " Test Files  1 skipped (1)\n      Tests  1 skipped (1)\n")
    # A mixed line (some real pass/fail outcome alongside skips) must
    # NOT be treated as zero coverage.
    assert not _ran_zero_tests(
        "related",
        " Test Files  1 passed (1)\n      Tests  3 passed | 2 skipped (5)\n",
    )
    assert not _ran_zero_tests(
        "related", " Test Files  1 failed (1)\n      Tests  1 failed (1)\n")
    # Never applies to a "full" run.
    assert not _ran_zero_tests("full", "No test files found, exiting with code 1\n")
