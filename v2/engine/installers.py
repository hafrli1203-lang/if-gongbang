"""Installers: apply a manifest change, then run the project's install
command. `FakeInstaller` (engine.fakes.installer) is in-memory, used by
unit tests that do not need a real subprocess. `RealInstaller` here runs
the actual install_cmd in the repo and rolls the manifest back byte-for-
byte on failure, so a half-applied dependency change never lands.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Protocol


class Installer(Protocol):
    def apply(self, key: str, manifest: Path, change: dict[str, object],
              original: bytes | None = None) -> None: ...

    def applied(self, key: str) -> bool: ...


class InstallFailed(Exception):
    """The install command exited non-zero or timed out. The manifest has
    already been restored to its pre-call bytes by the time this is
    raised."""


def merge_one_level(data: dict[str, object], change: dict[str, object]) -> dict[str, object]:
    """Top-level keys overwrite; nested dicts are merged one level deep.
    Shared by FakeInstaller and RealInstaller's JSON manifest path."""
    result = dict(data)
    for key, value in change.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = {**current, **value}
        else:
            result[key] = value
    return result


def _apply_json_change(before: bytes, change: dict[str, object]) -> bytes:
    import json
    current: dict[str, object] = {}
    if before:
        loaded = json.loads(before.decode("utf-8"))
        if isinstance(loaded, dict):
            current = loaded
    merged = merge_one_level(current, change)
    return json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8")


# A requirement line a worker may ask for: a bare package name with an
# optional extras list and version specifier. No URLs, paths, options,
# environment markers or whitespace -- anything else is a way to make
# install_cmd fetch or execute something the owner did not choose.
_REQUIREMENT_LINE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?"
    r"((==|~=|>=|<=|!=|>|<)[A-Za-z0-9.*+!-]+(,(==|~=|>=|<=|!=|>|<)[A-Za-z0-9.*+!-]+)*)?$"
)


def validate_add_lines(change: dict[str, object]) -> list[str]:
    """Return change["add_lines"] as validated strings or raise
    InstallFailed("manifest_line_rejected:<line>")."""
    add_lines = change.get("add_lines", [])
    if not isinstance(add_lines, list):
        raise InstallFailed("manifest_line_rejected:not_a_list")
    cleaned: list[str] = []
    for raw in add_lines:
        line = str(raw).strip()
        if not _REQUIREMENT_LINE.fullmatch(line):
            raise InstallFailed(f"manifest_line_rejected:{line[:60]}")
        cleaned.append(line)
    return cleaned


def _apply_lines_change(before: bytes, change: dict[str, object]) -> bytes:
    """`change["add_lines"]`: append lines not already present, keeping a
    trailing newline. Idempotent -- re-applying the same change is a
    no-op for lines already there."""
    add_lines = validate_add_lines(change)
    text = before.decode("utf-8") if before else ""
    existing = set(line.strip() for line in text.splitlines())
    new_lines = [str(line) for line in add_lines if str(line).strip() not in existing]
    if not new_lines:
        return before
    if text and not text.endswith("\n"):
        text += "\n"
    text += "".join(f"{line}\n" for line in new_lines)
    return text.encode("utf-8")


class RealInstaller:
    """Applies a manifest change on disk, then runs `install_cmd` in
    `repo`. On any non-zero exit or timeout, the manifest is restored to
    its exact pre-call bytes and InstallFailed is raised -- the manifest
    change and the install command succeed or fail together.

    `original`, when given, is both the rollback target AND the base
    `change` is applied onto -- NOT whatever is currently on disk. This
    matters across a crash/resume: if a first apply() wrote the modified
    manifest and then the process died before install_cmd finished, a
    resumed apply() would otherwise read the already-half-applied bytes
    off disk as "before", so a second failure would roll back to that
    half-applied state rather than the true original (the caller's
    snapshot). Passing the caller's snapshot bytes as `original` makes a
    resumed apply() both correctly idempotent (re-deriving the same
    `after` from the same base) and safe to roll back."""

    def __init__(self, repo: Path, install_cmd: tuple[str, ...],
                 marker_dir: Path, timeout_sec: float = 600.0) -> None:
        self.repo = Path(repo)
        self.install_cmd = install_cmd
        self.marker_dir = Path(marker_dir)
        self.timeout_sec = timeout_sec

    def _marker_path(self, key: str) -> Path:
        return self.marker_dir / f"{key}.done"

    def apply(self, key: str, manifest: Path, change: dict[str, object],
              original: bytes | None = None) -> None:
        if self.applied(key):
            return  # idempotent: already applied, no-op
        manifest = Path(manifest)
        if original is not None:
            before = original
        else:
            before = manifest.read_bytes() if manifest.exists() else b""
        if manifest.suffix == ".json":
            after = _apply_json_change(before, change)
        else:
            after = _apply_lines_change(before, change)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_bytes(after)
        try:
            result = subprocess.run(
                list(self.install_cmd), cwd=str(self.repo),
                capture_output=True, timeout=self.timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            manifest.write_bytes(before)
            raise InstallFailed("install_timeout") from exc
        except OSError as exc:
            # install_cmd's executable missing, not executable, or some
            # other spawn-time OS failure -- the process never ran, but
            # the manifest write above already happened and must still
            # be rolled back so a half-applied dependency change never
            # lands just because the command itself couldn't start.
            manifest.write_bytes(before)
            raise InstallFailed("install_spawn_failed") from exc
        if result.returncode != 0:
            manifest.write_bytes(before)
            raise InstallFailed(f"install_failed:{result.returncode}")
        self.marker_dir.mkdir(parents=True, exist_ok=True)
        rel = self._manifest_relpath(manifest)
        digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        self._marker_path(key).write_text(f"{digest}\n{rel}\n", encoding="utf-8")

    def applied(self, key: str) -> bool:
        marker = self._marker_path(key)
        if not marker.exists():
            return False
        lines = marker.read_text(encoding="utf-8").splitlines()
        if len(lines) < 2:
            return False
        digest, rel = lines[0], lines[1]
        manifest = self.repo / rel
        if not manifest.exists():
            return False
        return hashlib.sha256(manifest.read_bytes()).hexdigest() == digest

    def _manifest_relpath(self, manifest: Path) -> str:
        try:
            return manifest.resolve().relative_to(self.repo.resolve()).as_posix()
        except ValueError:
            return manifest.as_posix()


__all__ = ["Installer", "InstallFailed", "RealInstaller", "merge_one_level"]
