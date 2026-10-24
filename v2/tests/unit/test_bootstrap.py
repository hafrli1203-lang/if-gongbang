"""Unit tests for engine.bootstrap.ensure_installed/record_bootstrap."""
from __future__ import annotations

from pathlib import Path

from engine.bootstrap import ensure_installed, record_bootstrap
from engine.ledger import Ledger


def _repo_with_package_json(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(
        '{"name": "x", "devDependencies": {"left-pad": "1.0.0"}}\n',
        encoding="utf-8",
    )
    return repo


def test_already_installed_does_not_run_anything(tmp_path: Path) -> None:
    repo = _repo_with_package_json(tmp_path)
    (repo / "node_modules").mkdir()

    result = ensure_installed(repo, ("false",))

    assert result.ran is False
    assert result.ok is True
    assert result.detail == "already_installed"


def test_no_install_cmd_configured_fails_closed(tmp_path: Path) -> None:
    repo = _repo_with_package_json(tmp_path)

    result = ensure_installed(repo, ())

    assert result.ran is False
    assert result.ok is False
    assert result.detail == "no_install_cmd"


def test_successful_install_creates_node_modules(tmp_path: Path) -> None:
    repo = _repo_with_package_json(tmp_path)

    result = ensure_installed(
        repo, ("sh", "-c", "mkdir -p node_modules && echo installed"),
    )

    assert result.ran is True
    assert result.ok is True
    assert result.detail == ""
    assert (repo / "node_modules").exists()


def test_failing_install_command_is_reported_not_raised(tmp_path: Path) -> None:
    repo = _repo_with_package_json(tmp_path)

    result = ensure_installed(repo, ("sh", "-c", "echo boom >&2; exit 1"))

    assert result.ran is True
    assert result.ok is False
    assert "install_failed:" in result.detail
    assert "boom" in result.detail


def test_missing_binary_is_reported_not_raised(tmp_path: Path) -> None:
    repo = _repo_with_package_json(tmp_path)

    result = ensure_installed(repo, ("/no/such/binary-anywhere",))

    assert result.ran is True
    assert result.ok is False
    assert result.detail.startswith("install_failed:")


def test_timeout_is_reported_not_raised(tmp_path: Path) -> None:
    repo = _repo_with_package_json(tmp_path)

    result = ensure_installed(repo, ("sleep", "5"), timeout_sec=0.1)

    assert result.ran is True
    assert result.ok is False
    assert "TimeoutExpired" in result.detail


# ---- P1 fix: bootstrap now routes through engine.installers' hardening ----

def test_bootstrap_npm_install_cmd_gets_ignore_scripts(tmp_path: Path) -> None:
    """P1 finding: a bare subprocess.run used to run install_cmd with no
    hardening at all. A fake `npm` on PATH records the argv it was
    called with -- must carry --ignore-scripts, same as
    engine.installers.RealInstaller already guarantees for every other
    install in the engine."""
    repo = _repo_with_package_json(tmp_path)
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    argv_log = tmp_path / "argv.log"
    fake_npm = fake_bin / "npm"
    fake_npm.write_text(f"#!/bin/sh\necho \"$@\" > {argv_log}\n", encoding="utf-8")
    fake_npm.chmod(0o755)

    import os
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{fake_bin}:{old_path}"
    try:
        result = ensure_installed(repo, ("npm", "install"))
    finally:
        os.environ["PATH"] = old_path

    assert result.ok is True
    assert "--ignore-scripts" in argv_log.read_text(encoding="utf-8")


def test_bootstrap_install_cmd_cannot_read_real_home_secret(tmp_path: Path) -> None:
    """P1 finding: install_cmd used to run with the engine process's own
    ambient environment and real $HOME. A sentinel secret file under a
    stand-in "real HOME" must be unreadable -- install_cmd's $HOME is
    pointed at a fresh empty temp directory instead, and a sentinel env
    var (standing in for a real secret token) is never passed through
    (only PATH/LANG/TERM are)."""
    repo = _repo_with_package_json(tmp_path)
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    sentinel = real_home / "sentinel.secret"
    sentinel.write_text("do-not-leak", encoding="utf-8")
    result_path = repo / "result.json"

    import json as _json
    import os
    import sys
    install_cmd = (
        sys.executable, "-c",
        "import json, os, pathlib, sys\n"
        "out = {\n"
        "    'home': os.environ.get('HOME'),\n"
        "    'has_secret_env': 'GB2_TEST_SECRET' in os.environ,\n"
        "}\n"
        f"pathlib.Path({str(result_path)!r}).write_text(json.dumps(out))\n",
    )
    old_home = os.environ.get("HOME")
    os.environ["GB2_TEST_SECRET"] = "leaked-token"
    os.environ["HOME"] = str(real_home)
    try:
        result = ensure_installed(repo, install_cmd)
    finally:
        del os.environ["GB2_TEST_SECRET"]
        if old_home is not None:
            os.environ["HOME"] = old_home

    assert result.ok is True
    out = _json.loads(result_path.read_text(encoding="utf-8"))
    assert out["has_secret_env"] is False
    assert out["home"] != str(real_home)


def test_record_bootstrap_appends_dependency_installed_event(tmp_path: Path) -> None:
    ledger = Ledger.create(tmp_path / "ledger.sqlite")
    ledger.register_project("alpha")
    result = ensure_installed(tmp_path / "does-not-exist", ())

    seq = record_bootstrap(ledger, "alpha", result)

    events = [e for _seq, e in ledger.events("alpha", "dependency.installed")]
    assert len(events) == 1
    assert events[0].payload == {"ran": False, "ok": False, "detail": "no_install_cmd"}
    assert seq > 0
    ledger.close()
