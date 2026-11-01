"""wt-land.sh success / failure-revert / --retry, against a fake deploy adapter (success.sh or
failure.sh dropped in as tools/deploy.sh) - no real systemd or Postgres needed for this path:
no migrations/ files are touched, REVIEW_GATE stays unset (deploy.sh path, not ship.sh), and no
app ever registers a verify-<app> skill or a remote, so wt-land.sh's own code takes the
"nothing to do" branch at each of those optional steps rather than skipping the test.

Known gap (see docs/v3-adapters.md): the failure path also calls tools/deploy-check.sh, which
is not part of this bundle (not in the private source's allow-listed file set either). Its
absence here surfaces exactly as it would for anyone using this bundle without that script: a
caught "bash: tools/deploy-check.sh: No such file or directory", the revert step still
succeeds, and wt-land.sh still exits 5 - asserted below, not hidden.
"""
import glob
import json
import os
import shutil
import stat
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "v3", "tools")

pytestmark = pytest.mark.skipif(not os.path.isdir(TOOLS), reason="v3/tools not extracted yet")


def git(repo: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    assert r.returncode == 0, f"git {args} failed: {r.stdout}{r.stderr}"
    return r.stdout.strip()


def fake_deploy(tools_dir: str, rc: int) -> None:
    """Replaces the extracted deploy.sh with a script that only reports success/failure - the
    build/swap/restart machinery itself is deploy.sh's own contract (docs/v3-adapters.md), not
    something this test re-implements."""
    p = os.path.join(tools_dir, "deploy.sh")
    with open(p, "w", encoding="utf-8") as f:
        f.write(f"#!/usr/bin/env bash\nset -euo pipefail\necho \"fake deploy rc={rc}\"\nexit {rc}\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)


@pytest.fixture
def harness(tmp_path):
    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    app = "demoapp"
    src = apps_root / app
    src.mkdir()
    git(str(src), "init", "-q", "-b", "main")
    git(str(src), "config", "user.email", "ci-test")
    git(str(src), "config", "user.name", "t")
    (src / "README.md").write_text("hello\n")
    git(str(src), "add", "README.md")
    git(str(src), "commit", "-q", "-m", "init")

    root = apps_root / "_wt"
    (root / app).mkdir(parents=True)

    tools = tmp_path / "tools"
    shutil.copytree(TOOLS, tools)

    data = tmp_path / "data"
    data.mkdir()
    env = dict(os.environ)
    env.update({
        "WT_APPS_ROOT": str(apps_root),
        "WT_ROOT": str(root),
        "WT_DATA": str(data),
        "WT_BACKUPS": str(tmp_path / "backups"),
        "WT_DEPLOY_STATE": str(tmp_path / "deploy-state"),
        "WT_ADMIN_ENV": str(tmp_path / "admin.env-unused"),
    })
    return {"apps_root": apps_root, "src": src, "root": root, "tools": tools, "env": env,
            "data": data, "app": app}


def make_copy(h, name: str, filename: str = "a.txt"):
    """A work copy exactly like wt-new.sh would leave behind (a git worktree + .wt.json +
    .wt.scope), with one committed, in-scope change - built directly with git rather than
    through wt-new.sh, since wt-new.sh's own job (port/test-DB-slot assignment) is unrelated to
    what wt-land.sh is being tested for here."""
    wt = h["root"] / h["app"] / name
    git(str(h["src"]), "worktree", "add", "-q", "-b", f"wt/{name}", str(wt), "main")
    (wt / "allowed").mkdir(exist_ok=True)
    (wt / "allowed" / filename).write_text("v1\n")
    git(str(wt), "add", f"allowed/{filename}")
    git(str(wt), "commit", "-q", "-m", "feat: add file")
    (wt / ".wt.scope").write_text("allowed/**\n")
    (wt / ".wt.json").write_text(json.dumps({
        "app": h["app"], "name": name, "branch": f"wt/{name}", "base": "main",
        "path": str(wt), "port": None, "db": None, "units": [],
    }))
    return wt


def run_land(h, *args, timeout=60):
    return subprocess.run(
        ["bash", os.path.join(h["tools"], "wt-land.sh"), h["app"], *args],
        env=h["env"], capture_output=True, text=True, timeout=timeout,
    )


def test_land_success(harness):
    wt = make_copy(harness, "ok1")
    fake_deploy(harness["tools"], 0)

    r = run_land(harness, "ok1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (wt / ".wt.done").exists()
    assert git(str(harness["src"]), "log", "-1", "--format=%s") == "land: ok1"
    assert "fake deploy rc=0" in r.stdout


def test_land_failure_reverts_source(harness):
    wt = make_copy(harness, "bad1")
    fake_deploy(harness["tools"], 1)

    r = run_land(harness, "bad1")
    assert r.returncode == 5, r.stdout + r.stderr
    assert not (wt / ".wt.done").exists()
    assert git(str(harness["src"]), "log", "-1", "--format=%s").startswith("revert: land ")
    fail_recs = glob.glob(str(harness["data"] / harness["app"] / "land-fail-*.json"))
    assert len(fail_recs) == 1, fail_recs
    rec = json.load(open(fail_recs[0]))
    assert rec["deploy_rc"] == 1
    # the file the land added is gone again after the revert - the source is back to "init"
    assert not (harness["src"] / "allowed" / "a.txt").exists()
    # known gap (see module docstring / docs/v3-adapters.md): deploy-check.sh is not bundled
    assert "deploy-check.sh" in r.stdout or "응답 확인 실패" in r.stdout


def test_land_retry_after_failure_then_success(harness):
    wt = make_copy(harness, "retry1")
    fake_deploy(harness["tools"], 1)
    r1 = run_land(harness, "retry1")
    assert r1.returncode == 5, r1.stdout + r1.stderr

    fail_recs = glob.glob(str(harness["data"] / harness["app"] / "land-fail-*.json"))
    assert len(fail_recs) == 1, fail_recs
    land_id = os.path.basename(fail_recs[0])[len("land-fail-"):-len(".json")]

    fake_deploy(harness["tools"], 0)
    r2 = run_land(harness, "retry1", "--retry", land_id)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert (wt / ".wt.done").exists()
    # the revert's revert brought the file back
    assert (harness["src"] / "allowed" / "a.txt").exists()
    assert not glob.glob(str(harness["data"] / harness["app"] / "land-fail-*.json"))


def test_land_refuses_self_deploying_app(harness):
    make_copy(harness, "selfdeploy1")
    (harness["src"] / "deploy.env").write_text('DEPLOY_CMD="bash scripts/deploy.sh"\n')

    r = run_land(harness, "selfdeploy1")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "자체 배포" in (r.stdout + r.stderr)
