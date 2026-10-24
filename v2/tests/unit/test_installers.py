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

from engine.installers import InstallFailed, RealInstaller
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
