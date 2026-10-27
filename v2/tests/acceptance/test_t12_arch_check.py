"""T12 -- COMMON_ARCHITECTURE_STANDARD.md's arch_check wired into bench
(ProjectConfig.arch_check_cmd, engine.application.steps.suite_run's new
run_arch_check step). The engine never ships or hardcodes a path to an
arch_check tool itself (public code) -- every test here configures its
own small fake tool (a committed script reading a committed JSON
fixture) and asserts only on the step's OWN behavior:

  not configured            -> skipped, bench unaffected       test_a
  violation on a CHANGED    -> bench fails ("arch_violations"),
    file                       evidence names the new violation  test_b
  violation on an UNCHANGED -> bench still passes, evidence
    (pre-existing) file        counts it as existing, not new    test_c
  the tool itself fails to  -> bench fails ("arch_check_failed"),
    run (missing script,       before any violation is counted  test_d
    output isn't the shape)
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

from engine.application.processor import Fakes, Processor
from engine.application.processor import submit as submit_request
from engine.application.steps import suite_run
from engine.application.steps.open_job import open_job
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from tests.conftest import Workspace, git
from tests.fixtures.sample_app import make_sample

# A tiny, project-supplied stand-in for a real arch_check tool: reads a
# sibling JSON fixture (the test edits its content per case) and prints
# the exact {"violations": [...]} shape the real
# ops-repo/harness/arch_check.py --json prints (confirmed against a
# real run of that tool, -- never invented. Exits 1 iff the
# fixture names at least one violation, matching the real tool's own
# "exit 1 means violations found" contract.
_FAKE_ARCH_CHECK = '''\
import json
import sys
from pathlib import Path

fixture = Path(__file__).resolve().parent / "arch_fixture.json"
violations = json.loads(fixture.read_text(encoding="utf-8")) if fixture.exists() else []
print(json.dumps({"app_dir": sys.argv[1], "ok": not violations, "violations": violations}))
sys.exit(1 if violations else 0)
'''


def _processor(workspace: Workspace) -> Processor:
    fakes = Fakes(publisher=FakePublisher(), installer=FakeInstaller())
    proc = Processor(config=workspace.config, ledger=workspace.ledger, fakes=fakes,
                      holder="p1")
    proc.register("bench", suite_run.steps)
    return proc


def _sample_with_fake_arch_check(workspace: Workspace, name: str,
                                 violations: list[dict[str, str]]) -> Workspace:
    """A clean, passing variant-1 sample app (greet + negative control,
    tests/fixtures/sample_app.py) with a committed fake arch_check tool
    and `arch_fixture.json` seeded to `violations`, wired into
    ProjectConfig.arch_check_cmd."""
    sample = make_sample(workspace.root / name, 1, Path(sys.executable))
    (sample.repo / "fake_arch.py").write_text(_FAKE_ARCH_CHECK, encoding="utf-8")
    (sample.repo / "arch_fixture.json").write_text(
        json.dumps(violations), encoding="utf-8")
    git(sample.repo, "add", "-A")
    git(sample.repo, "commit", "-q", "-m", "add fake arch_check")
    project = replace(
        workspace.config.projects["alpha"],
        repo=sample.repo, test_cmd=sample.test_cmd, install_cmd=sample.install_cmd,
        manifest=sample.manifest, bench_cases=sample.bench_cases,
        arch_check_cmd=(sys.executable, "fake_arch.py"),
    )
    config = replace(workspace.config, projects={**workspace.config.projects, "alpha": project})
    return Workspace(workspace.root, config, workspace.ledger, sample.repo, workspace.beta)


def _run_bench(ws: Workspace) -> tuple[str, str]:
    attempt_id = open_job(ws.config, ws.ledger, "alpha", f"{ws.alpha.name}-job", "full", ("builder",))
    rid = submit_request(ws.ledger, "alpha", attempt_id, "bench", {}, "owner")
    proc = _processor(ws)
    proc.run_once("alpha")
    return attempt_id, rid


def test_a_not_configured_is_skipped_and_never_blocks_bench(workspace: Workspace) -> None:
    sample = make_sample(workspace.root / "t12a", 1, Path(sys.executable))
    project = replace(
        workspace.config.projects["alpha"], repo=sample.repo, test_cmd=sample.test_cmd,
        install_cmd=sample.install_cmd, manifest=sample.manifest, bench_cases=sample.bench_cases,
    )  # arch_check_cmd left at its default: ()
    config = replace(workspace.config, projects={**workspace.config.projects, "alpha": project})
    ws = Workspace(workspace.root, config, workspace.ledger, sample.repo, workspace.beta)

    attempt_id, rid = _run_bench(ws)
    req = ws.ledger.request(rid)
    assert req.status == "done"
    payload = ws.ledger.evidence(attempt_id, "bench")[0].payload
    assert payload["passed"] is True
    assert payload["arch"] == {"configured": False, "new_violations": [], "existing_violation_count": 0}


def test_b_violation_on_a_changed_file_fails_bench(workspace: Workspace) -> None:
    ws = _sample_with_fake_arch_check(workspace, "t12b", [
        {"check": "file_size", "file": "src/greeter/__init__.py", "detail": "too long"},
    ])
    # This attempt's own doing: an uncommitted edit to the exact file
    # the fixture names as violating -- dirty_paths (HEAD vs working
    # tree, unchanged since commit runs after bench) must pick it up.
    init_py = ws.alpha / "src" / "greeter" / "__init__.py"
    init_py.write_text(init_py.read_text(encoding="utf-8") + "\n# padding\n", encoding="utf-8")

    attempt_id, rid = _run_bench(ws)
    req = ws.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "arch_violations"}
    payload = ws.ledger.evidence(attempt_id, "bench")[0].payload
    assert payload["passed"] is False
    assert payload["reason"] == "arch_violations"
    assert payload["arch"]["configured"] is True
    assert payload["arch"]["new_violations"] == [
        {"check": "file_size", "file": "src/greeter/__init__.py", "detail": "too long"},
    ]
    assert payload["arch"]["existing_violation_count"] == 0


def test_c_violation_on_an_untouched_file_never_blocks_bench(workspace: Workspace) -> None:
    ws = _sample_with_fake_arch_check(workspace, "t12c", [
        {"check": "banned_name", "file": "requirements.txt", "detail": "pre-existing, untouched"},
    ])
    # Nothing in the repo is changed this attempt -- the named file is
    # the SAME pre-existing violation on every run, never this
    # attempt's doing.

    attempt_id, rid = _run_bench(ws)
    req = ws.ledger.request(rid)
    assert req.status == "done"
    payload = ws.ledger.evidence(attempt_id, "bench")[0].payload
    assert payload["passed"] is True
    assert payload["reason"] == "ok"
    assert payload["arch"]["new_violations"] == []
    assert payload["arch"]["existing_violation_count"] == 1


def test_d_tool_itself_failing_to_run_fails_bench(workspace: Workspace) -> None:
    sample = make_sample(workspace.root / "t12d", 1, Path(sys.executable))
    project = replace(
        workspace.config.projects["alpha"], repo=sample.repo, test_cmd=sample.test_cmd,
        install_cmd=sample.install_cmd, manifest=sample.manifest, bench_cases=sample.bench_cases,
        # No such script in the repo -- the tool itself cannot run,
        # never a "0 violations" clean pass.
        arch_check_cmd=(sys.executable, "no_such_arch_check.py"),
    )
    config = replace(workspace.config, projects={**workspace.config.projects, "alpha": project})
    ws = Workspace(workspace.root, config, workspace.ledger, sample.repo, workspace.beta)

    attempt_id, rid = _run_bench(ws)
    req = ws.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "arch_check_failed"}
    # Never got far enough to record ANY bench evidence -- run_arch_check
    # itself raised, before record_bench's evidence.record ever runs.
    assert ws.ledger.evidence(attempt_id, "bench") == []


def test_e_arch_check_cmd_outside_repo_survives_the_projects_own_hidden_paths(
    workspace: Workspace,
) -> None:
    """결함 60: arch_check_cmd may point at a tool that lives OUTSIDE
    this project's repo (e.g. a shared tool checked out once, reused
    by every project configured for bench -- an app config pointing at the
    engine's own tools/arch_check.py does exactly this). This
    project's OWN hidden_paths (meant to hide sibling apps'
    repos/secrets from ITS bench sandbox, for example a list covering
    the shared apps root) must never also
    hide the arch_check tool itself just because the tool happens to
    live under one of those hidden roots -- worker_sandbox.py already
    protects the exact same engine-own-code case for the
    builder/reviewer sandbox (`readonly += (ENGINE_ROOT,)`); the bench
    sandbox (_bench_sandbox_argv) must do the same for arch_check_cmd's
    own script path, or the tool "fails to run" (StepFailed
    arch_check_failed) on every project that hides a root containing
    it, regardless of whether the repo itself has any real violation."""
    sample = make_sample(workspace.root / "t12e", 1, Path(sys.executable))
    # The tool lives under a separate tree this project's OWN
    # hidden_paths hides wholesale -- never under `sample.repo`, so
    # this reproduces hidden_paths shadowing a path OTHER than the
    # repo itself (the repo stays visible regardless, via its own
    # deeper `writable` mount).
    apps_root = workspace.root / "other_apps_root"
    tool_dir = apps_root / "shared-engine" / "tools"
    tool_dir.mkdir(parents=True)
    (tool_dir / "arch_check_tool.py").write_text(
        "import json\nprint(json.dumps({'violations': []}))\n", encoding="utf-8")
    project = replace(
        workspace.config.projects["alpha"],
        repo=sample.repo, test_cmd=sample.test_cmd, install_cmd=sample.install_cmd,
        manifest=sample.manifest, bench_cases=sample.bench_cases,
        arch_check_cmd=(sys.executable, str(tool_dir / "arch_check_tool.py")),
        hidden_paths=(apps_root,),
    )
    config = replace(workspace.config, sandbox="bwrap",
                     projects={**workspace.config.projects, "alpha": project})
    ws = Workspace(workspace.root, config, workspace.ledger, sample.repo, workspace.beta)

    attempt_id, rid = _run_bench(ws)
    req = ws.ledger.request(rid)
    assert req.status == "done"
    payload = ws.ledger.evidence(attempt_id, "bench")[0].payload
    assert payload["arch"]["configured"] is True
    assert payload["arch"]["new_violations"] == []
