"""Unit tests for engine.infrastructure.reference_snapshot: the ONE
place ProjectConfig.reference_paths is ever checked against the host
filesystem or actually read (see that module's own docstring for the
 redesign this replaces -- a config-parse-time check that let
a protected root's own DESCENDANT (e.g. secrets_dir/"production")
through, never looked past an entry's own immediate children for a
leaked .env*, and re-mounted whatever a path resolved to at every later
worker/reviewer spawn with no re-check at all)."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from engine.config import EngineConfig, ProjectConfig
from engine.infrastructure import reference_snapshot as rs
from tests.conftest import make_repo


def _engine_config(tmp_path: Path, *, alpha_reference: tuple[Path, ...] = (),
                   alpha_hidden: tuple[Path, ...] = ()) -> EngineConfig:
    alpha_repo = make_repo(tmp_path / "repos" / "alpha", {"a.txt": "a\n"})
    beta_repo = make_repo(tmp_path / "repos" / "beta", {"b.txt": "b\n"})
    return EngineConfig(
        ledger_path=tmp_path / "engine" / "ledger.sqlite",
        home=tmp_path / "engine" / "home",
        secrets_dir=tmp_path / "engine" / "secrets",
        projects={
            "alpha": ProjectConfig("alpha", alpha_repo, reference_paths=alpha_reference,
                                   hidden_paths=alpha_hidden),
            "beta": ProjectConfig("beta", beta_repo),
        },
        station_owners={"build": {"builder": ("src/",)}},
    )


def _entries_under(root: Path) -> set[str]:
    found = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            found.add(str((Path(dirpath) / fname).relative_to(root)))
    return found


# ---------------------------------------------------------------------------
# Repro 1: a protected root's own DESCENDANT (not just the root
# itself) must be rejected too.
# ---------------------------------------------------------------------------

def test_snapshot_rejects_entry_under_secrets_dir(tmp_path: Path) -> None:
    config = _engine_config(tmp_path)
    secret_subdir = config.secrets_dir / "production"
    secret_subdir.mkdir(parents=True)
    project = ProjectConfig("alpha", config.projects["alpha"].repo,
                            reference_paths=(secret_subdir,))
    with pytest.raises(ValueError, match="reference_path_forbidden_overlap"):
        rs.snapshot_references(project, config, tmp_path / "dest")


# ---------------------------------------------------------------------------
# Repro 2: another project's repo (not just THIS project's own)
# must be rejected too.
# ---------------------------------------------------------------------------

def test_snapshot_rejects_entry_under_another_projects_repo(tmp_path: Path) -> None:
    config = _engine_config(tmp_path)
    private = config.projects["beta"].repo / "private"
    private.mkdir(parents=True)
    project = ProjectConfig("alpha", config.projects["alpha"].repo,
                            reference_paths=(private,))
    with pytest.raises(ValueError, match="reference_path_forbidden_overlap"):
        rs.snapshot_references(project, config, tmp_path / "dest")


# ---------------------------------------------------------------------------
# Repro 3: a NESTED .env* (not just one directly inside the
# configured entry) must never be copied into the snapshot.
# ---------------------------------------------------------------------------

def test_snapshot_does_not_copy_nested_dotenv_file(tmp_path: Path) -> None:
    config = _engine_config(tmp_path)
    material = tmp_path / "material"
    (material / "app").mkdir(parents=True)
    (material / "app" / "brief.md").write_text("BRIEF\n", encoding="utf-8")
    (material / "app" / ".env.production").write_text("SECRET=1\n", encoding="utf-8")
    project = ProjectConfig("alpha", config.projects["alpha"].repo,
                            reference_paths=(material,))

    entries = rs.snapshot_references(project, config, tmp_path / "dest")
    [entry] = entries
    copied = _entries_under(entry.path)
    assert "app/brief.md" in copied
    assert not any(".env" in name for name in copied)


# ---------------------------------------------------------------------------
# Repro 4: content is pinned at first snapshot -- swapping the
# source for a symlink (even to somewhere forbidden) afterward must
# have NO effect on an already-made snapshot.
# ---------------------------------------------------------------------------

def test_snapshot_unchanged_when_source_swapped_for_symlink_after_first_snapshot(
    tmp_path: Path,
) -> None:
    config = _engine_config(tmp_path)
    material = tmp_path / "material"
    material.mkdir()
    (material / "brief.md").write_text("ORIGINAL-BRIEF\n", encoding="utf-8")
    project = ProjectConfig("alpha", config.projects["alpha"].repo,
                            reference_paths=(material,))
    dest = tmp_path / "dest"

    first = rs.snapshot_references(project, config, dest)
    [first_entry] = first
    assert (first_entry.path / "brief.md").read_text(encoding="utf-8") == "ORIGINAL-BRIEF\n"

    # Swap the ORIGINAL source directory for a symlink into secrets_dir.
    import shutil

    shutil.rmtree(material)
    os.symlink(config.secrets_dir, material)

    second = rs.snapshot_references(project, config, dest)
    [second_entry] = second
    assert second_entry.path == first_entry.path
    # Unchanged: still the original content, no re-validation/re-copy,
    # and no exception despite the source now pointing at secrets_dir.
    assert (second_entry.path / "brief.md").read_text(encoding="utf-8") == "ORIGINAL-BRIEF\n"


# ---------------------------------------------------------------------------
# Config-load side of P1 #5 lives in tests/unit/test_config.py
# (test_reference_paths_other_projects_missing_entry_does_not_break_config_load);
# this is the snapshot-function side: a project with NO reference_paths
# must never even create a references/ directory or touch the
# filesystem at all.
# ---------------------------------------------------------------------------

def test_ensure_snapshot_for_attempt_does_nothing_when_no_reference_paths(
    tmp_path: Path,
) -> None:
    config = _engine_config(tmp_path)
    entries = rs.ensure_snapshot_for_attempt(config, "alpha", "J-alpha-ABC#1")
    assert entries == ()
    assert not (config.home / "references").exists()


# ---------------------------------------------------------------------------
# HOME/hidden_paths relaxed rule: a plain descendant (no hidden/dot
# component) is ALLOWED; a hidden-component descendant is rejected.
# ---------------------------------------------------------------------------

def test_snapshot_allows_plain_descendant_of_real_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _engine_config(tmp_path)
    fake_home = tmp_path / "operator-home"
    material = fake_home / "Documents" / "material"
    material.mkdir(parents=True)
    (material / "brief.md").write_text("BRIEF\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))
    project = ProjectConfig("alpha", config.projects["alpha"].repo,
                            reference_paths=(material,))

    entries = rs.snapshot_references(project, config, tmp_path / "dest")
    [entry] = entries
    assert (entry.path / "brief.md").read_text(encoding="utf-8") == "BRIEF\n"


def test_snapshot_rejects_hidden_component_under_real_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _engine_config(tmp_path)
    fake_home = tmp_path / "operator-home"
    ssh_like = fake_home / ".ssh" / "material"
    ssh_like.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    project = ProjectConfig("alpha", config.projects["alpha"].repo,
                            reference_paths=(ssh_like,))

    with pytest.raises(ValueError, match="reference_path_hidden_component"):
        rs.snapshot_references(project, config, tmp_path / "dest")


def test_snapshot_rejects_entry_that_is_itself_the_real_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _engine_config(tmp_path)
    fake_home = tmp_path / "operator-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    project = ProjectConfig("alpha", config.projects["alpha"].repo,
                            reference_paths=(fake_home,))

    with pytest.raises(ValueError, match="reference_path_forbidden_overlap"):
        rs.snapshot_references(project, config, tmp_path / "dest")


# ---------------------------------------------------------------------------
# Size caps: per-file skip with a recorded warning, total cap stops the
# whole copy (never a silent partial file).
# ---------------------------------------------------------------------------

def test_snapshot_skips_file_over_per_file_cap_and_records_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rs, "FILE_CAP_BYTES", 10)
    config = _engine_config(tmp_path)
    material = tmp_path / "material"
    material.mkdir()
    (material / "small.txt").write_text("ok\n", encoding="utf-8")
    (material / "big.txt").write_text("x" * 100, encoding="utf-8")
    project = ProjectConfig("alpha", config.projects["alpha"].repo,
                            reference_paths=(material,))

    [entry] = rs.snapshot_references(project, config, tmp_path / "dest")
    copied = _entries_under(entry.path)
    assert "small.txt" in copied
    assert "big.txt" not in copied
    assert any("skipped_too_large" in w for w in entry.warnings)


def test_snapshot_total_cap_stops_copy_and_records_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rs, "TOTAL_CAP_BYTES", 10)
    config = _engine_config(tmp_path)
    material = tmp_path / "material"
    material.mkdir()
    (material / "a.txt").write_text("x" * 8, encoding="utf-8")
    (material / "b.txt").write_text("x" * 8, encoding="utf-8")
    project = ProjectConfig("alpha", config.projects["alpha"].repo,
                            reference_paths=(material,))

    [entry] = rs.snapshot_references(project, config, tmp_path / "dest")
    assert any("snapshot_total_cap_reached" in w for w in entry.warnings)


# ---------------------------------------------------------------------------
# Snapshot is made read-only after copying.
# ---------------------------------------------------------------------------

def test_snapshot_files_and_dirs_are_made_read_only(tmp_path: Path) -> None:
    config = _engine_config(tmp_path)
    material = tmp_path / "material"
    (material / "sub").mkdir(parents=True)
    (material / "sub" / "f.txt").write_text("x\n", encoding="utf-8")
    project = ProjectConfig("alpha", config.projects["alpha"].repo,
                            reference_paths=(material,))

    [entry] = rs.snapshot_references(project, config, tmp_path / "dest")
    file_mode = stat.S_IMODE(os.stat(entry.path / "sub" / "f.txt").st_mode)
    dir_mode = stat.S_IMODE(os.stat(entry.path / "sub").st_mode)
    assert file_mode == 0o444
    assert dir_mode == 0o555


# ---------------------------------------------------------------------------
# Second review: hidden name checked before realpath, relative paths
# refused, and a file that grows after fstat cannot pass the caps.
# ---------------------------------------------------------------------------

def test_snapshot_rejects_hidden_named_symlink_to_plain_directory(tmp_path: Path) -> None:
    config = _engine_config(tmp_path)
    plain = tmp_path / "volume" / "credentials"
    plain.mkdir(parents=True)
    (plain / "id.txt").write_text("secret\n", encoding="utf-8")
    link_home = tmp_path / "userhome"
    link_home.mkdir()
    (link_home / ".ssh").symlink_to(plain)
    project = ProjectConfig("alpha", config.projects["alpha"].repo,
                            reference_paths=(link_home / ".ssh",))
    with pytest.raises(ValueError, match="reference_path_hidden_component"):
        rs.snapshot_references(project, config, tmp_path / "dest")


def test_snapshot_rejects_relative_entry(tmp_path: Path) -> None:
    config = _engine_config(tmp_path)
    project = ProjectConfig("alpha", config.projects["alpha"].repo,
                            reference_paths=(Path("../../../tmp"),))
    with pytest.raises(ValueError, match="must be absolute"):
        rs.snapshot_references(project, config, tmp_path / "dest")


def test_copy_file_stops_a_file_that_grew_past_the_cap(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "grow.txt"
    src.write_bytes(b"x" * 5)
    real_fstat = os.fstat

    def small_fstat(fd: int) -> os.stat_result:
        result = real_fstat(fd)
        if stat.S_ISREG(result.st_mode):
            src.write_bytes(b"x" * 100)  # grows right after the size check
        return result

    monkeypatch.setattr(rs, "FILE_CAP_BYTES", 10)
    monkeypatch.setattr(rs.os, "fstat", small_fstat)
    copied, written, warning = rs._copy_file(src, tmp_path / "out.txt", 0)
    assert (copied, written) == (False, 0)
    assert warning is not None and warning.startswith("skipped_grew_past_cap")
    assert not (tmp_path / "out.txt").exists()
