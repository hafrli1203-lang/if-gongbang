"""T9b -- continuation of tests/acceptance/test_t9_real_conditions.py
(split into its own file to keep that file under the 800-line cap;
same fixture-app evidence base: the sample-app performance-ads
bench run, evidence bench seq 1703). Each test below is a regression
lock for exactly one of three further defects that same bench run
exposed, beyond the 8 (plus 10/12) test_t9_real_conditions.py already
covers:

Defect -> test:
  16. bench's own full-scope test_cmd ran tests/scenes/ (plan-owned,
      not-yet-built-behavior scripts) unconditionally, so a job's
      FIRST attempt always failed bench regardless of what it
      changed.                                                    test_16_*
  17. a package.json `file:../...` dependency resolved one nesting
      level short inside the engine's live-workspace-managed worktree
      (`Cannot find package '@sample-app/kit/db'`) even though the
      identical dependency resolves fine in the real source repo.  test_17_*
  18. a failed bench's own evidence payload carried no test output
      and no failed-file names, so diagnosing it required re-running
      the suite by hand.                                           test_18_*
  22. a `gb2 fix` worker-copy creation crashed with an unhandled
      FileExistsError when a copy_links entry (e.g. "knowledge") was
      only PARTLY gitignored ("knowledge/*" + "!knowledge/SOURCES.md"),
      so the copy already had a real, partly-tracked directory there
      before the flat symlink_to ever ran., sample-app
      `gb2 fix`.                                                    test_22_*
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# ---- 16. (결함 16) bench's full-scope test_cmd must exclude tests/scenes/ --

def test_16_full_scope_bench_excludes_planner_scenes(workspace) -> None:
    """결함 16, evidence bench seq 1703): tests/scenes/
    scripts test requirements a later station has not built yet --
    expected to fail right up until engine.application.acceptance actually
    runs them, never bench (engine.application.steps.suite_run/
    suite_select). Before the fix, bench's own full-scope test_cmd ran
    every tests/scenes/ file unconditionally, so a job's FIRST attempt
    always failed bench regardless of what it changed.

    Drives the real engine.application.steps.suite_select.resolve_test_argv
    against a real StepContext (real EngineConfig/Ledger/RequestRow from
    the `workspace` fixture, via open_job/submit -- the exact
    construction Processor.run_once itself uses), not a hand-built dict."""
    from dataclasses import replace

    from engine.application.processor import Fakes, StepContext
    from engine.application.processor import submit as submit_request
    from engine.application.steps.open_job import open_job
    from engine.application.steps.suite_select import full_scope_argv, resolve_test_argv
    from engine.domain.plan_model import SCENES_PREFIX
    from engine.fakes.installer import FakeInstaller
    from engine.fakes.publisher import FakePublisher

    assert SCENES_PREFIX == "tests/scenes/"

    # Pure function, no I/O: the actual argv-construction logic.
    argv = full_scope_argv(("npx", "vitest", "run", "--no-color", "--no-cache"))
    assert argv[-2:] == ["--exclude", f"{SCENES_PREFIX}**"]
    # A runner this engine has never confirmed the right exclusion flag
    # for keeps test_cmd byte-for-byte unchanged -- no invented flag for
    # an unverified runner (crash-out: only vitest's `--exclude <glob>`
    # was confirmed, via `npx vitest run --help`,.
    assert full_scope_argv(("pytest", "-q")) == ["pytest", "-q"]

    project = replace(
        workspace.config.projects["alpha"],
        test_cmd=("npx", "vitest", "run", "--no-color", "--no-cache"),
        bench_scope="full",
    )
    config = replace(workspace.config, projects={**workspace.config.projects, "alpha": project})
    attempt_id = open_job(config, workspace.ledger, "alpha", "t16-job", "full", ("builder",))
    rid = submit_request(workspace.ledger, "alpha", attempt_id, "bench", {}, "owner")
    request = workspace.ledger.request(rid)
    ctx = StepContext(config=config, ledger=workspace.ledger, fakes=Fakes(publisher=FakePublisher(), installer=FakeInstaller()),
                       request=request, project="alpha", attempt_id=attempt_id)

    scope, full_argv = resolve_test_argv(ctx)
    assert scope == "full"
    assert full_argv == [
        "npx", "vitest", "run", "--no-color", "--no-cache", "--exclude", f"{SCENES_PREFIX}**",
    ]

    # The request payload forcing scope="full" outright (e.g. a release's
    # ensure_full_bench) takes the SAME exclusion-applying branch.
    rid2 = submit_request(workspace.ledger, "alpha", attempt_id, "bench",
                           {"scope": "full"}, "owner")
    request2 = workspace.ledger.request(rid2)
    ctx2 = StepContext(config=config, ledger=workspace.ledger, fakes=Fakes(publisher=FakePublisher(), installer=FakeInstaller()),
                        request=request2, project="alpha", attempt_id=attempt_id)
    scope2, forced_argv = resolve_test_argv(ctx2)
    assert scope2 == "full"
    assert forced_argv[-2:] == ["--exclude", f"{SCENES_PREFIX}**"]


# ---- 17. (결함 17) a package.json file:../... dep must resolve the same --
# ---- relative path to the real target from ANY mirrored worktree depth --

_needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not installed on this host",
)


@_needs_node
def test_17_file_dep_remap_resolves_through_real_node_require(tmp_path: Path) -> None:
    """결함 17, evidence bench seq 1703): `npx vitest run`
    failed with `Cannot find package '@sample-app/kit/db'` inside the
    engine's live-workspace-managed worktree, even though the IDENTICAL
    `file:../_shared/kit` dependency resolves fine in the real source
    checkout -- root cause: the managed worktree nests one directory
    level deeper (GB2_WORKSPACE/apps/<app>/repo/) than the real source
    checkout (GB2_APPS_ROOT/<app>/), so the SAME relative "../" escape,
    preserved byte-for-byte by `cp -r` (tools/gb2lib/workspace.py's own
    _copy_node_modules) into the worktree, lands one level short.

    Reproduces that exact shape with node/shared-pkg standing in for
    @sample-app/kit, then proves BOTH the RED (no remap: real node
    `require()` fails exactly like the evidence) and the GREEN (after
    engine.application.steps.file_deps resolves+remaps it) using node's own,
    real module resolution -- never a mocked one."""
    from engine.application.steps import file_deps

    # The REAL source checkout: GB2_APPS_ROOT/<app> stand-in.
    source_app = tmp_path / "source" / "app"
    source_app.mkdir(parents=True)
    (source_app / "package.json").write_text(
        json.dumps({"name": "app", "dependencies": {"shared-pkg": "file:../shared-pkg"}}),
        encoding="utf-8",
    )
    shared_pkg = tmp_path / "source" / "shared-pkg"
    shared_pkg.mkdir(parents=True)
    (shared_pkg / "package.json").write_text(
        json.dumps({"name": "shared-pkg", "main": "index.js"}), encoding="utf-8")
    (shared_pkg / "index.js").write_text("module.exports = 'ok-from-shared-pkg';\n",
                                         encoding="utf-8")
    (source_app / "node_modules").mkdir()
    # The exact relative text a real `npm install` of a `file:` dep
    # writes (confirmed against a real Node app repo's
    # own real node_modules/@sample-app/kit symlink,: 2 levels
    # up from node_modules/<pkg> reaches the dep's own parent).
    os.symlink("../../shared-pkg", source_app / "node_modules" / "shared-pkg")

    # The MANAGED WORKTREE stand-in: one directory level deeper than
    # source_app (GB2_WORKSPACE/apps/<app>/repo/ vs GB2_APPS_ROOT/<app>/).
    mirror_repo = tmp_path / "mirror" / "app" / "repo"
    mirror_repo.mkdir(parents=True)
    shutil.copy(source_app / "package.json", mirror_repo / "package.json")
    (mirror_repo / "node_modules").mkdir()
    # _copy_node_modules (tools/gb2lib/workspace.py) uses `cp -r`, which
    # preserves a symlink's own text unchanged -- `os.symlink` here
    # mirrors that exactly (not `shutil.copytree`, which can dereference).
    os.symlink("../../shared-pkg", mirror_repo / "node_modules" / "shared-pkg")

    require_js = (
        "try { console.log(require('./node_modules/shared-pkg')); }"
        " catch (e) { console.log('ERR:' + e.code); }"
    )

    def run_require() -> str:
        result = subprocess.run(
            ["node", "-e", require_js], cwd=mirror_repo,
            capture_output=True, text=True, timeout=30,
        )
        return (result.stdout or "").strip()

    # RED: before any remap, the worktree's own symlink chain lands one
    # level short (mirror/app/shared-pkg, which does not exist) -- the
    # exact MODULE_NOT_FOUND shape the real evidence's
    # "Cannot find package '@sample-app/kit/db'" is an instance of.
    assert run_require() == "ERR:MODULE_NOT_FOUND"

    # THE FIX: discover+resolve against the REAL source checkout...
    escaping = file_deps.discover_escaping_file_deps(source_app)
    assert escaping == ("../shared-pkg",)
    target = file_deps.resolve_escaping_target(source_app, "../shared-pkg")
    assert target == shared_pkg.resolve()
    file_deps.validate_escaping_target(target, forbidden_roots=())
    file_dep_targets = ((escaping[0], str(target)),)

    # ...then remap_pairs_for_root computes, for THIS mirrored worktree,
    # exactly the (source, dest) a sandbox.wrap(readonly_remap=...) call
    # would bind -- dest is precisely where the broken symlink chain
    # landed short above.
    pairs = file_deps.remap_pairs_for_root(mirror_repo, file_dep_targets)
    assert pairs == ((target, tmp_path / "mirror" / "app" / "shared-pkg"),)
    remap_source, remap_dest = pairs[0]
    assert not remap_dest.exists()  # confirms this IS the short landing spot

    # Applying the remap OURSELVES on the real filesystem (standing in
    # for what sandbox.wrap's own --ro-bind would do inside a bwrap
    # mount namespace -- see suite_run._bench_sandbox_argv's own
    # readonly_remap wiring) and re-running the IDENTICAL node command:
    remap_dest.symlink_to(remap_source)
    assert run_require() == "ok-from-shared-pkg"


# ---- 18. (결함 18) a failed bench's evidence must name what failed -------

def test_18_failed_bench_evidence_carries_tail_and_failed_test_files(
    workspace,
) -> None:
    """결함 18, evidence bench seq 1703): a failed bench's
    own evidence payload carried no test output and no failed-file
    names at all (just passed/reason/scope/argv) -- diagnosing WHY
    required re-running the suite by hand. Reuses the existing,
    already-real variant-2 sample app (tests/fixtures/sample_app.py,
    tests/unit/test_steps_bench.py's own pattern) -- a real git repo, a
    real `pytest -q` subprocess, a real assertion failure -- rather
    than inventing a fake test-runner output shape."""
    import sys
    from dataclasses import replace

    from engine.application.steps import suite_run
    from engine.application.processor import Fakes, Processor
    from engine.application.processor import submit as submit_request
    from engine.application.steps.open_job import open_job
    from engine.fakes.installer import FakeInstaller
    from engine.fakes.publisher import FakePublisher
    from tests.fixtures.sample_app import make_sample

    sample = make_sample(workspace.root / "sample18", 2, Path(sys.executable))
    project = replace(
        workspace.config.projects["alpha"], repo=sample.repo, test_cmd=sample.test_cmd,
        install_cmd=sample.install_cmd, manifest=sample.manifest,
        bench_cases=sample.bench_cases,
    )
    config = replace(workspace.config, projects={**workspace.config.projects, "alpha": project})

    attempt_id = open_job(config, workspace.ledger, "alpha", "t18-job", "full", ("builder",))
    rid = submit_request(workspace.ledger, "alpha", attempt_id, "bench", {}, "owner")

    fakes = Fakes(publisher=FakePublisher(), installer=FakeInstaller())
    proc = Processor(config=config, ledger=workspace.ledger, fakes=fakes, holder="p1")
    proc.register("bench", suite_run.steps)
    proc.run_once("alpha")

    req = workspace.ledger.request(rid)
    assert req.status == "failed"
    bench_evidence = workspace.ledger.evidence(attempt_id, "bench")
    assert len(bench_evidence) == 1
    payload = bench_evidence[0].payload
    assert payload["passed"] is False
    # The real pytest -q failure this sample always produces
    # (test_steps_bench.py's own test_bench_fails_before_fix_* locks the
    # SAME sample's per_case/reason shape; this test only adds the two
    # new keys).
    assert "FAILED" in payload["tail"]
    assert "test_initials" in payload["tail"]
    assert payload["failed_test_files"] == ["tests/test_greeter.py::test_initials"]
    # A credential-shaped URL in the tail is scrubbed, not printed
    # verbatim, in evidence a less-trusted reader (e.g. an external review
    # pass) may also see.
    assert suite_run._scrub_secrets("postgres://alice:s3cr3t@db/app") == \
        "postgres://***@db/app"


# ---- 22. (결함 22) a partly-tracked copy_links entry must merge, -------
# ---- not crash, in both worker copies and disposable snapshots --------

def _repo_with_partly_tracked_knowledge(tmp_path: Path) -> Path:
    """A repo whose .gitignore ignores everything under "knowledge/"
    EXCEPT one tracked file (the real sample-app shape: "knowledge/*" +
    "!knowledge/SOURCES.md"), plus one more file that sits on disk,
    gitignored, the way a real app's knowledge base holds data files
    the tests need but git never tracks."""
    from tests.conftest import make_repo

    repo = make_repo(tmp_path / "app22", {
        "a.txt": "a\n",
        "knowledge/SOURCES.md": "tracked\n",
    })
    # make_repo writes its own .gitignore; add the real partly-ignoring
    # rule after, same pattern as test_engine_records_ignored.py.
    with (repo / ".gitignore").open("a", encoding="utf-8") as f:
        f.write("knowledge/*\n!knowledge/SOURCES.md\n")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "partly ignore knowledge"],
                   check=True)
    (repo / "knowledge" / "extra.txt").write_text("untracked\n", encoding="utf-8")
    return repo


def test_22_worker_copy_merges_partly_tracked_copy_links_entry(tmp_path: Path) -> None:
    """결함 22: `_make_copy` must not raise FileExistsError when
    copy_links names an entry the copy's own `git archive HEAD`
    extraction already materialized as a real, partly-tracked
    directory -- it must merge in the untracked sibling instead,
    leaving the tracked file exactly as committed, and `git status`
    in the copy must stay clean."""
    from engine.application.steps.open_job import _make_copy

    repo = _repo_with_partly_tracked_knowledge(tmp_path)
    dest = tmp_path / "copy22"
    skipped, _sha256 = _make_copy(repo, dest, copy_links=("knowledge",))
    assert skipped == []

    assert (dest / "knowledge" / "SOURCES.md").read_text(encoding="utf-8") == "tracked\n"
    assert (dest / "knowledge" / "extra.txt").is_symlink()
    assert (dest / "knowledge" / "extra.txt").read_text(encoding="utf-8") == "untracked\n"

    status = subprocess.run(["git", "-C", str(dest), "status", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout
    assert status == ""


def test_22_snapshot_merges_partly_tracked_copy_links_entry(tmp_path: Path) -> None:
    """결함 22, snapshot side: engine.application.steps.suite_snapshot's
    make_snapshot + link_snapshot_deps must not raise either, and must
    reach the same merged shape as the worker copy."""
    from engine.config import ProjectConfig
    from engine.application.steps.suite_snapshot import link_snapshot_deps, make_snapshot

    repo = _repo_with_partly_tracked_knowledge(tmp_path)
    fp = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
                        capture_output=True, text=True, check=True).stdout.strip()
    dest = tmp_path / "snap22"
    make_snapshot(repo, dest, fp)  # would raise FileExistsError before the fix

    project_cfg = ProjectConfig("app22", repo, copy_links=("knowledge",))
    link_snapshot_deps(project_cfg, repo, dest)

    assert (dest / "knowledge" / "SOURCES.md").read_text(encoding="utf-8") == "tracked\n"
    assert (dest / "knowledge" / "extra.txt").is_symlink()
    assert (dest / "knowledge" / "extra.txt").read_text(encoding="utf-8") == "untracked\n"


def test_25_related_scope_also_excludes_scene_tests() -> None:
    """Defect 25 (sample-app T2): the related scope pulled in later tasks'
    scene tests that import the changed files and failed a correct task."""
    from engine.application.steps.suite_select import build_related_argv, with_scene_exclusion
    argv = with_scene_exclusion(build_related_argv(
        ("npx", "vitest", "related", "--run", "{files}"), ["src/a.ts"]))
    assert argv[-2:] == ["--exclude", "tests/scenes/**"]
    assert "src/a.ts" in argv
