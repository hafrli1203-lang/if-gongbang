"""Installers: apply a manifest change, then run the project's install
command. `FakeInstaller` (engine.fakes.installer) is in-memory, used by
unit tests that do not need a real subprocess. `RealInstaller` here runs
the actual install_cmd in the repo and rolls the manifest back byte-for-
byte on failure, so a half-applied dependency change never lands.

Security note (P1 finding,: a worker's own `add_lines`/
`change` request can name an arbitrary package. Running install_cmd with
the engine process's own ambient environment (its real credentials,
tokens, PATH-resolved tool versions) and with the package manager's
default lifecycle-script behavior would let a MALICIOUS package name
run arbitrary code with the engine's own environment and secrets the
moment install_cmd executes it (npm/pnpm/yarn preinstall/postinstall
scripts; this is a well-known real-world supply-chain vector, not
specific to this engine). RealInstaller now always runs install_cmd
with a minimal, explicitly-built env (`_installer_env`: PATH/LANG/TERM
from the engine's own os.environ, HOME pointing at a fresh empty
per-call temp directory -- never the engine's real env dict, which
could carry secrets_dir-adjacent values or API tokens) and, by default,
appends `--ignore-scripts` for npm/pnpm/yarn (`allow_lifecycle_scripts=
True` opts a specific project back into lifecycle scripts, e.g. one
whose own install_cmd legitimately needs a native-module postinstall
step -- a per-project, explicit choice, not this module's default).
`sandbox_mode`/`hidden_paths`/`net` additionally let a caller route
install_cmd through engine.sandbox.wrap, same as engine.steps.bench_run
and engine.steps.station already do for other subprocesses; the
default (`sandbox_mode="none"`) keeps install_cmd's existing behavior
(same argv, same cwd) for every caller that does not opt in, so this is
additive, not a cutover.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

from engine import sandbox

# npm/pnpm/yarn all accept a trailing "--ignore-scripts" flag on an
# install-like invocation (npm/pnpm: documented flag; yarn classic: same
# flag name) -- this is the one lifecycle-script-suppression mechanism
# common to all three, checked against each tool's own install docs
# before being relied on here, not guessed.
_SCRIPT_CAPABLE_MANAGERS = frozenset({"npm", "pnpm", "yarn"})

# Environment variable NAMES passed through from the engine process's
# own os.environ into install_cmd's env -- same fixed set
# EngineConfig.worker_env_passthrough defaults to for worker subprocesses
# (PATH, HOME, LANG, TERM), minus HOME: HOME is never passed through here
# (see _installer_env) because the real HOME is exactly the thing a
# malicious lifecycle script or a leaky install_cmd must not reach
# (shell rc files, package-manager auth tokens in ~/.npmrc, ssh keys).
_ENV_PASSTHROUGH = ("PATH", "LANG", "TERM")


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


def _inject_ignore_scripts(install_cmd: tuple[str, ...]) -> tuple[str, ...]:
    """Append "--ignore-scripts" to a npm/pnpm/yarn install_cmd, unless
    it is already present. Any other tool's install_cmd (uv, pip, a
    scripted installer, ...) is returned unchanged -- this is a targeted
    suppression of ONE specific, well-documented lifecycle-script
    mechanism, not a generic rewrite of every possible install_cmd."""
    if not install_cmd:
        return install_cmd
    program = Path(install_cmd[0]).name
    if program not in _SCRIPT_CAPABLE_MANAGERS:
        return install_cmd
    if "--ignore-scripts" in install_cmd:
        return install_cmd
    return (*install_cmd, "--ignore-scripts")


def run_install_cmd(repo: Path, install_cmd: tuple[str, ...], *,
                     timeout_sec: float,
                     allow_lifecycle_scripts: bool = False,
                     sandbox_mode: str = "none",
                     hidden_paths: tuple[Path, ...] = (),
                     net: bool = True) -> subprocess.CompletedProcess[bytes]:
    """The one place `install_cmd` is actually spawned, shared by
    RealInstaller.apply (a manifest-change install) and
    engine.bootstrap.ensure_installed (an app's FIRST install, no
    manifest to change) -- both get the exact same hardening:
    --ignore-scripts injected for a recognized npm/pnpm/yarn
    `install_cmd` (unless `allow_lifecycle_scripts`), a minimal
    explicit env (PATH/LANG/TERM only -- see _installer_env), a fresh
    empty per-call $HOME (never the real one), and, when
    `sandbox_mode="bwrap"`, OS-level isolation via engine.sandbox.wrap
    (`hidden_paths`/`net` passed straight through; `repo` and the
    temporary $HOME are the only writable mounts). `sandbox_mode=
    "none"` (the default) keeps the pre-existing bare-argv behavior --
    only the env/lifecycle-script hardening above is unconditional.

    Raises subprocess.TimeoutExpired, OSError or
    engine.sandbox.SandboxUnavailable -- the caller decides how to
    report each (RealInstaller.apply rolls back the manifest first;
    ensure_installed has no manifest and reports a BootstrapResult)."""
    cmd = install_cmd
    if not allow_lifecycle_scripts:
        cmd = _inject_ignore_scripts(cmd)
    tmp_home = Path(tempfile.mkdtemp(prefix="gb2-install-home-"))
    try:
        child_env = _installer_env()
        child_env["HOME"] = str(tmp_home)
        # Under sandbox_mode="bwrap" the REAL host HOME (this engine
        # process's own $HOME, if any) is always added to `hidden` on
        # top of whatever the caller passed -- install_cmd must not be
        # able to read it at the OS level either, not just via the
        # $HOME env value above.
        real_home_str = os.environ.get("HOME")
        hidden = hidden_paths
        if real_home_str:
            hidden = (*hidden, Path(real_home_str))
        argv = sandbox.wrap(
            list(cmd), cwd=repo, writable=(repo, tmp_home),
            hidden=hidden, net=net, mode=sandbox_mode, child_env=child_env,
        )
        popen_env = sandbox.exec_env(sandbox_mode, child_env)
        return subprocess.run(
            argv, cwd=str(repo), capture_output=True, timeout=timeout_sec,
            env=popen_env,
        )
    finally:
        shutil.rmtree(tmp_home, ignore_errors=True)


def _installer_env() -> dict[str, str]:
    """A minimal env for install_cmd: PATH/LANG/TERM taken from the
    engine process's own os.environ (same names
    EngineConfig.worker_env_passthrough already treats as safe to hand a
    worker subprocess), never the full ambient env (which may carry
    secrets this process itself was started with). No HOME entry is
    added here -- RealInstaller.apply() sets HOME to a fresh, empty,
    per-call temp directory instead (see apply()'s `tmp_home`), so
    install_cmd can never read the real HOME's package-manager config/
    auth files (~/.npmrc, ~/.pip/pip.conf, ...)."""
    env: dict[str, str] = {}
    for name in _ENV_PASSTHROUGH:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


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
    `after` from the same base) and safe to roll back.

    `allow_lifecycle_scripts` (default False): False appends
    "--ignore-scripts" to a recognized npm/pnpm/yarn install_cmd (see
    _inject_ignore_scripts) -- the safe default, since a worker's own
    `change`/`add_lines` can name an arbitrary package and a lifecycle
    script in it would otherwise run with this process's install
    environment the moment install_cmd executes. A project whose own
    install_cmd genuinely needs lifecycle scripts (e.g. a native-module
    postinstall step) must opt back in explicitly per-RealInstaller, not
    by this module silently allowing it.

    `sandbox_mode`/`hidden_paths`/`net`: optional engine.sandbox.wrap
    routing for install_cmd, same shape engine.steps.bench_run and
    engine.steps.station already use for their own subprocesses.
    `sandbox_mode="none"` (the default) runs install_cmd exactly as
    before (bare argv, no namespace isolation) -- only the env/
    lifecycle-script changes above apply unconditionally, regardless of
    `sandbox_mode`. `hidden_paths` are extra absolute host paths to hide
    from install_cmd (e.g. the engine's own home/secrets_dir/ledger
    directory -- this module has no EngineConfig of its own, so the
    caller supplies whichever roots apply to its setup); `net` (default
    True) controls whether install_cmd keeps outbound network access
    (needed for a real registry fetch) under `sandbox_mode="bwrap"`."""

    def __init__(self, repo: Path, install_cmd: tuple[str, ...],
                 marker_dir: Path, timeout_sec: float = 600.0,
                 allow_lifecycle_scripts: bool = False,
                 sandbox_mode: str = "none",
                 hidden_paths: tuple[Path, ...] = (),
                 net: bool = True) -> None:
        self.repo = Path(repo)
        self.install_cmd = install_cmd
        self.marker_dir = Path(marker_dir)
        self.timeout_sec = timeout_sec
        self.allow_lifecycle_scripts = allow_lifecycle_scripts
        self.sandbox_mode = sandbox_mode
        self.hidden_paths = hidden_paths
        self.net = net

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
            result = run_install_cmd(
                self.repo, self.install_cmd, timeout_sec=self.timeout_sec,
                allow_lifecycle_scripts=self.allow_lifecycle_scripts,
                sandbox_mode=self.sandbox_mode, hidden_paths=self.hidden_paths,
                net=self.net,
            )
        except sandbox.SandboxUnavailable as exc:
            manifest.write_bytes(before)
            raise InstallFailed("install_sandbox_unavailable") from exc
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


__all__ = ["Installer", "InstallFailed", "RealInstaller", "merge_one_level",
           "run_install_cmd"]
