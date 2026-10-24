"""Unit tests for engine.installers: RealInstaller runs a real install_cmd
and rolls the manifest back byte-for-byte on failure, plus the offline
uv install path the dependency-variant sample app relies on."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from engine.installers import InstallFailed, RealInstaller, _inject_ignore_scripts
from engine.sandbox import available as bwrap_available
from tests.fixtures.sample_app import make_wheel


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    return repo


def test_real_installer_runs_cmd_and_marks(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    marker_dir = tmp_path / "markers"
    manifest = repo / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    ran_marker = tmp_path / "ran.txt"
    install_cmd = (sys.executable, "-c",
                   f"open({str(ran_marker)!r}, 'w').write('ran')")
    installer = RealInstaller(repo=repo, install_cmd=install_cmd, marker_dir=marker_dir)

    installer.apply("req-1", manifest, {"dependencies": {"zod": "3.0.0"}})

    assert ran_marker.exists()
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data == {"dependencies": {"zod": "3.0.0"}}
    assert installer.applied("req-1") is True
    # idempotent: a second apply() with the same key is a no-op (install_cmd
    # does not run again, so the marker file is not re-written)
    ran_marker.unlink()
    installer.apply("req-1", manifest, {"dependencies": {"zod": "3.0.0"}})
    assert not ran_marker.exists()


def test_applied_false_when_manifest_changed_after_marker(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    marker_dir = tmp_path / "markers"
    manifest = repo / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    install_cmd = (sys.executable, "-c", "pass")
    installer = RealInstaller(repo=repo, install_cmd=install_cmd, marker_dir=marker_dir)

    installer.apply("req-1", manifest, {"a": 1})
    assert installer.applied("req-1") is True

    manifest.write_text('{"a": 1, "tampered": true}', encoding="utf-8")
    assert installer.applied("req-1") is False


def test_failed_cmd_restores_manifest_bytes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    marker_dir = tmp_path / "markers"
    manifest = repo / "manifest.json"
    before = '{"existing": "value"}'
    manifest.write_text(before, encoding="utf-8")
    install_cmd = (sys.executable, "-c", "import sys; sys.exit(3)")
    installer = RealInstaller(repo=repo, install_cmd=install_cmd, marker_dir=marker_dir)

    with pytest.raises(InstallFailed):
        installer.apply("req-1", manifest, {"new": "thing"})

    assert manifest.read_text(encoding="utf-8") == before
    assert installer.applied("req-1") is False


def test_requirements_add_lines_idempotent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    marker_dir = tmp_path / "markers"
    manifest = repo / "requirements.txt"
    manifest.write_text("already==1.0\n", encoding="utf-8")
    install_cmd = (sys.executable, "-c", "pass")
    installer = RealInstaller(repo=repo, install_cmd=install_cmd, marker_dir=marker_dir)

    installer.apply("req-1", manifest, {"add_lines": ["vendored_pkg==0.1.0"]})
    content = manifest.read_text(encoding="utf-8")
    assert content.count("vendored_pkg==0.1.0") == 1
    assert "already==1.0" in content

    # a second request applying the same add_lines must not duplicate the
    # line (idempotent merge, not blind append)
    manifest_before = manifest.read_text(encoding="utf-8")
    installer2 = RealInstaller(repo=repo, install_cmd=install_cmd, marker_dir=marker_dir)
    installer2.apply("req-2", manifest, {"add_lines": ["vendored_pkg==0.1.0"]})
    assert manifest.read_text(encoding="utf-8").count("vendored_pkg==0.1.0") == 1
    assert manifest.read_text(encoding="utf-8") == manifest_before


def test_apply_after_crash_rolls_back_to_original(tmp_path: Path) -> None:
    """A crash between a first apply() writing the modified manifest and
    install_cmd finishing leaves half-applied bytes on disk. A resumed
    apply() given the caller's snapshot as `original` must roll back to
    THAT (the true pre-install bytes), never to the half-applied bytes
    that happen to be on disk when this call starts."""
    repo = _repo(tmp_path)
    marker_dir = tmp_path / "markers"
    manifest = repo / "manifest.json"
    original = b'{"a": 1}'
    # Half-applied bytes left behind by a (simulated) earlier crashed
    # apply() -- NOT what either `original` or a correct re-derivation
    # from it would produce.
    manifest.write_bytes(b'{"a": 1, "half": "applied"}')
    install_cmd = (sys.executable, "-c", "import sys; sys.exit(3)")
    installer = RealInstaller(repo=repo, install_cmd=install_cmd, marker_dir=marker_dir)

    with pytest.raises(InstallFailed):
        installer.apply("req-1", manifest, {"b": 2}, original=original)

    assert manifest.read_bytes() == original
    assert installer.applied("req-1") is False


def test_resumed_apply_is_idempotent_from_original(tmp_path: Path) -> None:
    """Same half-applied-on-disk setup, but install_cmd now succeeds: the
    final manifest must be exactly merge_one_level(original, change) --
    derived from `original`, not from the half-applied disk bytes (which
    carry a "stray" key that must NOT survive)."""
    repo = _repo(tmp_path)
    marker_dir = tmp_path / "markers"
    manifest = repo / "manifest.json"
    original = b'{"a": 1}'
    manifest.write_bytes(b'{"a": 1, "stray": true}')
    install_cmd = (sys.executable, "-c", "pass")
    installer = RealInstaller(repo=repo, install_cmd=install_cmd, marker_dir=marker_dir)

    installer.apply("req-1", manifest, {"b": 2}, original=original)

    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data == {"a": 1, "b": 2}
    assert installer.applied("req-1") is True


def test_offline_uv_install_into_target(tmp_path: Path) -> None:
    if shutil.which("uv") is None:
        pytest.fail("uv is required for this VM's test suite; it is not on PATH")
    repo = _repo(tmp_path)
    vendor = repo / "vendor"
    make_wheel(vendor)
    manifest = repo / "requirements.txt"
    manifest.write_text("", encoding="utf-8")
    target = repo / ".deps"
    marker_dir = tmp_path / "markers"
    install_cmd = ("uv", "pip", "install", "--python", sys.executable,
                   "--target", str(target), "--no-index", "--find-links",
                   str(vendor), "-r", str(manifest))
    installer = RealInstaller(repo=repo, install_cmd=install_cmd, marker_dir=marker_dir)

    installer.apply("req-1", manifest, {"add_lines": ["vendored_pkg==0.1.0"]})

    assert (target / "vendored_pkg" / "__init__.py").exists()
    assert installer.applied("req-1") is True


def test_spawn_failure_restores_manifest_bytes(tmp_path: Path) -> None:
    """P2: a nonexistent install_cmd executable raises OSError from
    subprocess.run (not SubprocessError's TimeoutExpired path) -- the
    manifest write that happened just before must still be rolled back,
    and InstallFailed must be raised rather than letting OSError escape."""
    repo = _repo(tmp_path)
    marker_dir = tmp_path / "markers"
    manifest = repo / "manifest.json"
    before = '{"existing": "value"}'
    manifest.write_text(before, encoding="utf-8")
    install_cmd = (str(tmp_path / "does-not-exist-binary"),)
    installer = RealInstaller(repo=repo, install_cmd=install_cmd, marker_dir=marker_dir)

    with pytest.raises(InstallFailed, match="install_spawn_failed"):
        installer.apply("req-1", manifest, {"new": "thing"})

    assert manifest.read_text(encoding="utf-8") == before
    assert installer.applied("req-1") is False


def test_add_lines_rejects_urls_paths_and_options(tmp_path: Path) -> None:
    from engine.installers import InstallFailed, validate_add_lines
    assert validate_add_lines({"add_lines": ["vendored_pkg==0.1.0", " zod>=3,<4 ", "a-b[extra]"]}) == [
        "vendored_pkg==0.1.0", "zod>=3,<4", "a-b[extra]"]
    for bad in ["vendored_pkg @ file:///./vendor/x.whl", "pkg --find-links vendor",
                "git+https://x/y", "../local", "pkg; python_version<'3'", "-e .", "pkg ==1 extra"]:
        try:
            validate_add_lines({"add_lines": [bad]})
        except InstallFailed as exc:
            assert str(exc).startswith("manifest_line_rejected")
        else:
            raise AssertionError(f"accepted: {bad!r}")


def test_apply_rejects_bad_line_before_touching_manifest(tmp_path: Path) -> None:
    from engine.installers import InstallFailed, RealInstaller
    repo = tmp_path / "repo"; repo.mkdir()
    manifest = repo / "requirements.txt"; manifest.write_bytes(b"a==1\n")
    inst = RealInstaller(repo, ("true",), tmp_path / "markers", timeout_sec=5)
    try:
        inst.apply("k", manifest, {"add_lines": ["x @ file:///tmp/x.whl"]})
    except InstallFailed:
        pass
    else:
        raise AssertionError("expected InstallFailed")
    assert manifest.read_bytes() == b"a==1\n"
    assert not inst.applied("k")


# ---- P1 fix: sanitized env / --ignore-scripts / sandboxed install_cmd ----

def test_inject_ignore_scripts_for_npm_pnpm_yarn() -> None:
    assert _inject_ignore_scripts(("npm", "install")) == (
        "npm", "install", "--ignore-scripts")
    assert _inject_ignore_scripts(("pnpm", "install")) == (
        "pnpm", "install", "--ignore-scripts")
    assert _inject_ignore_scripts(("yarn", "install")) == (
        "yarn", "install", "--ignore-scripts")
    # already present -> not duplicated
    assert _inject_ignore_scripts(("npm", "install", "--ignore-scripts")) == (
        "npm", "install", "--ignore-scripts")
    # any other tool's install_cmd is left alone
    assert _inject_ignore_scripts(("uv", "pip", "install")) == ("uv", "pip", "install")


def test_npm_install_cmd_actually_run_contains_ignore_scripts(tmp_path: Path) -> None:
    """The real argv exec'd must carry --ignore-scripts for a npm-shaped
    install_cmd -- not just the pure helper function above. A fake `npm`
    on PATH records the argv it was called with so this is checked
    against what RealInstaller actually executes, not what it intended
    to."""
    repo = _repo(tmp_path)
    marker_dir = tmp_path / "markers"
    manifest = repo / "package.json"
    manifest.write_text("{}", encoding="utf-8")

    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    argv_log = tmp_path / "argv.log"
    fake_npm = fake_bin / "npm"
    fake_npm.write_text(
        f"#!/bin/sh\necho \"$@\" > {argv_log}\n", encoding="utf-8")
    fake_npm.chmod(0o755)

    import os
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{fake_bin}:{old_path}"
    try:
        installer = RealInstaller(
            repo=repo, install_cmd=("npm", "install"), marker_dir=marker_dir)
        installer.apply("req-1", manifest, {"dependencies": {"zod": "3.0.0"}})
    finally:
        os.environ["PATH"] = old_path

    assert "--ignore-scripts" in argv_log.read_text(encoding="utf-8")


def test_allow_lifecycle_scripts_opt_out(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    marker_dir = tmp_path / "markers"
    manifest = repo / "package.json"
    manifest.write_text("{}", encoding="utf-8")

    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    argv_log = tmp_path / "argv.log"
    fake_npm = fake_bin / "npm"
    fake_npm.write_text(
        f"#!/bin/sh\necho \"$@\" > {argv_log}\n", encoding="utf-8")
    fake_npm.chmod(0o755)

    import os
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{fake_bin}:{old_path}"
    try:
        installer = RealInstaller(
            repo=repo, install_cmd=("npm", "install"), marker_dir=marker_dir,
            allow_lifecycle_scripts=True)
        installer.apply("req-1", manifest, {"dependencies": {"zod": "3.0.0"}})
    finally:
        os.environ["PATH"] = old_path

    assert "--ignore-scripts" not in argv_log.read_text(encoding="utf-8")


def test_install_cmd_does_not_see_real_home_or_secret_env(tmp_path: Path) -> None:
    """A sentinel env var (standing in for a real secret the engine
    process happens to carry, e.g. an API token) and a sentinel file
    under the engine's own real HOME must both be invisible to
    install_cmd: the env var because RealInstaller builds an explicit
    minimal env (PATH/LANG/TERM only, never the ambient os.environ), the
    file because install_cmd's $HOME is pointed at a fresh empty temp
    directory, never the real one."""
    repo = _repo(tmp_path)
    marker_dir = tmp_path / "markers"
    manifest = repo / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    real_home = tmp_path / "real-home"
    real_home.mkdir()
    sentinel_file = real_home / "sentinel.secret"
    sentinel_file.write_text("do-not-leak", encoding="utf-8")

    # Written INSIDE repo (a real writable mount under sandbox_mode=
    # "bwrap"; irrelevant here since this test uses the default
    # sandbox_mode="none", but kept the same way so the two tests stay
    # directly comparable) -- a path outside repo/tmp_home would not even
    # exist to write to once sandboxing is turned on.
    result_path = repo / "result.json"
    install_cmd = (
        sys.executable, "-c",
        "import json, os, pathlib, sys\n"
        "out = {\n"
        "    'home': os.environ.get('HOME'),\n"
        "    'has_secret_env': 'GB2_TEST_SECRET' in os.environ,\n"
        "}\n"
        f"pathlib.Path({str(result_path)!r}).write_text(json.dumps(out))\n",
    )

    import os
    os.environ["GB2_TEST_SECRET"] = "leaked-token"
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(real_home)
    try:
        installer = RealInstaller(repo=repo, install_cmd=install_cmd, marker_dir=marker_dir)
        installer.apply("req-1", manifest, {"a": 1})
    finally:
        del os.environ["GB2_TEST_SECRET"]
        if old_home is not None:
            os.environ["HOME"] = old_home

    out = json.loads(result_path.read_text(encoding="utf-8"))
    # No inherited secret env var, and $HOME is not the engine process's
    # real HOME -- under sandbox_mode="none" (this test) that is an env
    # value only, not an OS-level guarantee; the separate bwrap test below
    # checks that a sentinel FILE under the real HOME is unreadable even
    # if install_cmd ignored $HOME entirely.
    assert out["has_secret_env"] is False
    assert out["home"] != str(real_home)


@pytest.mark.skipif(not bwrap_available(), reason="bwrap not available on this host")
def test_install_cmd_under_bwrap_cannot_read_real_home(tmp_path: Path) -> None:
    """Same sentinel-file check as above, but with sandbox_mode="bwrap":
    the OS-level guarantee on top of the plain env value -- install_cmd
    cannot read the real HOME even if it ignored $HOME entirely (e.g. by
    hardcoding a path), because the real HOME is never mounted into the
    sandbox at all."""
    repo = _repo(tmp_path)
    marker_dir = tmp_path / "markers"
    manifest = repo / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    real_home = Path.home()
    sentinel_file = real_home / "gb2_bwrap_sentinel_test.secret"
    created = not sentinel_file.exists()
    if created:
        sentinel_file.write_text("do-not-leak", encoding="utf-8")
    try:
        # Written INSIDE repo: the one writable mount this sandboxed
        # install_cmd actually has (see installers.apply's `writable=
        # (self.repo, tmp_home)`) -- a path outside it would not exist
        # inside the sandbox at all (sandbox.wrap hides /tmp wholesale).
        result_path = repo / "result.txt"
        install_cmd = (
            sys.executable, "-c",
            f"import pathlib, sys\n"
            f"p = pathlib.Path({str(sentinel_file)!r})\n"
            f"pathlib.Path({str(result_path)!r}).write_text(str(p.exists()))\n",
        )
        installer = RealInstaller(
            repo=repo, install_cmd=install_cmd, marker_dir=marker_dir,
            sandbox_mode="bwrap", net=False)
        installer.apply("req-1", manifest, {"a": 1})
        assert result_path.read_text(encoding="utf-8") == "False"
    finally:
        if created:
            sentinel_file.unlink(missing_ok=True)
