"""Engine records under .gongbang/ travel even when the app's .gitignore
matches them: a real job stopped at probe_failed because the app ignored
"*.log" and every probe/t<N>.log was dropped at merge."""
from __future__ import annotations

import subprocess
from pathlib import Path

from engine.application.pipeline_stages import commit_paths
from engine.application.steps.open_job import dirty_paths
from tests.conftest import make_repo


def _repo_ignoring_logs(tmp_path: Path) -> Path:
    repo = make_repo(tmp_path / "app", {"a.txt": "a\n"})
    # make_repo writes its own .gitignore; add the app's "*.log" rule after.
    with (repo / ".gitignore").open("a", encoding="utf-8") as f:
        f.write("*.log\n")
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "ignore logs"], check=True)
    probe = repo / ".gongbang" / "job" / "probe"
    probe.mkdir(parents=True)
    (probe / "t1.log").write_text("PROBE OK\n", encoding="utf-8")
    (repo / "build.log").write_text("noise\n", encoding="utf-8")
    return repo


def test_dirty_paths_includes_ignored_engine_records_only(tmp_path: Path) -> None:
    repo = _repo_ignoring_logs(tmp_path)
    paths = dirty_paths(repo, ())
    assert ".gongbang/job/probe/t1.log" in paths
    assert "build.log" not in paths


def test_commit_paths_commits_ignored_engine_record(tmp_path: Path) -> None:
    repo = _repo_ignoring_logs(tmp_path)
    commit_paths(repo, [".gongbang/job/probe/t1.log"], "records")
    tracked = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True,
                             text=True, check=True).stdout.split()
    assert ".gongbang/job/probe/t1.log" in tracked
    assert "build.log" not in tracked


def test_snapshot_keeps_tracked_record_that_app_ignores(tmp_path: Path) -> None:
    from engine.application.steps.suite_snapshot import make_snapshot
    repo = _repo_ignoring_logs(tmp_path)
    commit_paths(repo, [".gongbang/job/probe/t1.log"], "records")
    fp = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
                        capture_output=True, text=True, check=True).stdout.strip()
    dest = tmp_path / "snap"
    make_snapshot(repo, dest, fp)  # raised SnapshotIntegrityError before the fix
    assert (dest / ".gongbang" / "job" / "probe" / "t1.log").read_text(encoding="utf-8") == "PROBE OK\n"


def test_worker_copy_base_includes_tracked_ignored_record(tmp_path: Path) -> None:
    from engine.application.steps.open_job import _make_copy
    repo = _repo_ignoring_logs(tmp_path)
    commit_paths(repo, [".gongbang/job/probe/t1.log"], "records")
    dest = tmp_path / "copy"
    _make_copy(repo, dest)
    assert ".gongbang/job/probe/t1.log" not in dirty_paths(dest, ())
