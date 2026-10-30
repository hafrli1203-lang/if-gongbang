"""workspace.sync_work_branch_if_behind: rebases a clean, behind gb2/work
worktree onto its base; leaves a dirty one alone."""
import subprocess
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path
_TOOLS = str(_Path(__file__).resolve().parents[2] / "tools")
if _TOOLS not in _sys.path:
    _sys.path.insert(0, _TOOLS)

from gb2lib import shared, workspace


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@x", "PATH": "/usr/bin:/bin"}).stdout.strip()


def _setup(tmp_path: Path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "master")
    (src / "a.txt").write_text("1\n")
    _git(src, "add", "a.txt"); _git(src, "commit", "-q", "-m", "one")
    wt = tmp_path / "ws" / "repo"
    wt.parent.mkdir(parents=True)
    _git(src, "worktree", "add", "-q", "-b", "gb2/work", str(wt), "master")
    (src / "b.txt").write_text("2\n")
    _git(src, "add", "b.txt"); _git(src, "commit", "-q", "-m", "two on master")
    monkeypatch.setattr(shared, "app_workspace", lambda app: tmp_path / "ws")
    monkeypatch.setattr(workspace, "read_worktree_base", lambda app: "master")
    return src, wt


def test_behind_and_clean_is_rebased(tmp_path: Path, monkeypatch) -> None:
    src, wt = _setup(tmp_path, monkeypatch)
    out = workspace.sync_work_branch_if_behind("cafe")
    assert out == {"synced": True, "behind": 1, "reason": None}
    assert (wt / "b.txt").exists()
    assert _git(wt, "rev-parse", "HEAD") == _git(src, "rev-parse", "master")


def test_dirty_worktree_is_left_alone(tmp_path: Path, monkeypatch) -> None:
    _src, wt = _setup(tmp_path, monkeypatch)
    (wt / "dirty.txt").write_text("x\n")
    out = workspace.sync_work_branch_if_behind("cafe")
    assert out["synced"] is False and out["reason"] == "worktree_dirty"
    assert not (wt / "b.txt").exists()
