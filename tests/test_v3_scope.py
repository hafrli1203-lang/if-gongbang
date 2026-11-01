"""Negative test for v3/tools/scope-check.sh: a change outside .wt.scope must be refused
(exit 2), and a change inside it must pass (exit 0). Pure git + bash, no Postgres/systemd.
"""
import json
import os
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCOPE_CHECK = os.path.join(ROOT, "v3", "tools", "scope-check.sh")

pytestmark = pytest.mark.skipif(not os.path.isfile(SCOPE_CHECK), reason="v3/tools not extracted yet")


def git(repo: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    assert r.returncode == 0, f"git {args} failed: {r.stdout}{r.stderr}"
    return r.stdout.strip()


@pytest.fixture
def copy(tmp_path):
    wt = tmp_path / "copy"
    wt.mkdir()
    git(str(wt), "init", "-q", "-b", "main")
    git(str(wt), "config", "user.email", "ci-test")
    git(str(wt), "config", "user.name", "t")
    (wt / "README.md").write_text("base\n")
    git(str(wt), "add", "README.md")
    git(str(wt), "commit", "-q", "-m", "base")
    git(str(wt), "checkout", "-q", "-b", "wt/job1")
    (wt / ".wt.scope").write_text("allowed/**\n")
    (wt / ".wt.json").write_text(json.dumps({"app": "demoapp", "name": "job1", "base": "main"}))
    return wt


def env_for(tmp_path):
    env = dict(os.environ)
    env.update({
        "WT_APPS_ROOT": str(tmp_path / "apps-unused"),
        "WT_DATA": str(tmp_path / "data"),
        "WT_ADMIN_ENV": str(tmp_path / "admin.env-unused"),
    })
    return env


def run_scope_check(copy_path, tmp_path, extra_args=()):
    return subprocess.run(
        ["bash", SCOPE_CHECK, str(copy_path), *extra_args],
        env=env_for(tmp_path), capture_output=True, text=True, timeout=30,
    )


def test_in_scope_change_passes(copy, tmp_path):
    (copy / "allowed").mkdir()
    (copy / "allowed" / "a.txt").write_text("x\n")
    git(str(copy), "add", "allowed/a.txt")
    git(str(copy), "commit", "-q", "-m", "feat: allowed change")

    r = run_scope_check(copy, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "범위 안" in r.stdout


def test_out_of_scope_change_refused(copy, tmp_path):
    (copy / "allowed").mkdir()
    (copy / "allowed" / "a.txt").write_text("x\n")
    git(str(copy), "add", "allowed/a.txt")
    git(str(copy), "commit", "-q", "-m", "feat: allowed change")

    (copy / "forbidden").mkdir()
    (copy / "forbidden" / "b.txt").write_text("y\n")
    git(str(copy), "add", "forbidden/b.txt")
    git(str(copy), "commit", "-q", "-m", "feat: forbidden change")

    r = run_scope_check(copy, tmp_path)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "forbidden/b.txt" in r.stdout
    assert "허용 경로 밖" in r.stdout


def test_package_json_change_needs_explicit_scope_line(copy, tmp_path):
    (copy / "package.json").write_text("{}\n")
    git(str(copy), "add", "package.json")
    git(str(copy), "commit", "-q", "-m", "chore: touch package.json")

    r = run_scope_check(copy, tmp_path)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "package.json" in r.stdout
