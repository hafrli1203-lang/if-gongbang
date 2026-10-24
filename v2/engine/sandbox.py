"""OS-level sandbox for child processes spawned by engine.procs.spawn.

Closes ENGINE.md's first "한계" bullet: code a worker writes (e.g. a
project's own tests/conftest.py) used to run with full engine-process
filesystem access during a bench/trust-check run or a worker's own CLI
process. mode="bwrap" runs that child inside a bubblewrap sandbox instead;
mode="none" (the default) is a no-op so all pre-existing behavior and
tests are unchanged.

bwrap primitives used here (verified empirically, not assumed):
 - "--ro-bind / /" mirrors the whole host tree read-only, so every
   existing path is visible unless later hidden or rebound.
 - "--tmpfs <dir>" replaces a directory's contents with an empty tmpfs
   (verified: a file that existed there is no longer visible inside).
 - "--ro-bind /dev/null <file>" hides a single file: inside the sandbox
   it is a protected zero-length node (verified: reading it after this
   bind returns "Permission denied", not the original content).
 - A later "--bind"/"--ro-bind" on a path ALWAYS shadows an earlier one
   on an ancestor of that path (verified: bwrap creates the missing
   mountpoint under the earlier mount and the later one takes over) --
   this is how a path stays usable (or stays hidden) despite an
   ancestor being mounted differently. Mount options are therefore
   never grouped by kind (hidden-then-writable, as an earlier version
   of this module did): they are merged into ONE list and sorted by
   path depth ascending (shallowest first) before being emitted, so a
   shallow mount of one kind is always followed -- and overridden --
   by a deeper mount of another kind, regardless of which kind is
   "hidden" and which is "writable"/"readonly". Getting this backwards
   was a real bug here: a writable bind of an ANCESTOR directory
   (e.g. the engine's base dir, home to both the ledger and a hidden
   secrets_dir) emitted AFTER a hidden tmpfs on one of its own children
   re-exposed that child's real host content, undoing the hide.
 - "--unshare-all" without "--share-net" blocks outbound sockets
   (verified: socket.create_connection raises immediately); adding
   "--share-net" restores network access (verified: the same call
   succeeds). Position of "--share-net" in the argv does not matter --
   bwrap applies the namespace flags as a set before any bind option.
 - "--die-with-parent" is always passed: if the engine process that
   spawned bwrap itself dies, the sandboxed child (and anything it
   forked) dies with it, same expectation as engine.procs' own
   start_new_session + process-group bookkeeping for the unsandboxed
   case.
 - "--chdir <cwd> --" sets the child's working directory and marks the
   end of bwrap's own options, so wrapped argv is never misparsed as
   bwrap flags.
 - "--clearenv" followed by one "--setenv NAME VALUE" per entry of
   `child_env` gives the SANDBOXED CHILD exactly that environment,
   independent of whatever environment the bwrap process itself (the
   launcher, a separate, briefly-privileged step before the namespace
   it builds takes effect) was executed with. This matters because the
   launcher's own argv[0] and environment are never derived from
   `child_env`: a worker/bench-case-controlled env dict (e.g. a bench
   case's own "env" override, read from a JSON file inside the
   project's repo) must never be able to change which `bwrap` binary
   gets exec'd (a malicious PATH) or inject into its own process
   before it drops privileges (LD_PRELOAD/LD_LIBRARY_PATH) -- see
   available()'s resolved absolute path and launcher_env().

A hidden/writable/readonly path that does not exist on the host is
silently skipped (nothing to mount there); only directories are
tmpfs'd, a file is hidden via the /dev/null bind, matching what the
self-test above confirmed for each shape. Every path is resolved with
os.path.realpath before mounting, so a path that is itself a symlink
(e.g. into a hidden directory) cannot be used to mount the wrong real
location.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

_KNOWN_MODES = frozenset({"none", "bwrap"})

# Process-wide cache: available() runs a real subprocess, so callers that
# check it on every spawn (every station/review/bench step) should not
# pay that cost each time. Tests reset it via _reset_available_cache().
_available_cache: bool | None = None
# The absolute path available() resolved bwrap to -- set together with
# _available_cache=True, never re-derived from a caller-supplied PATH
# (there is none: shutil.which here always consults THIS process's own
# os.environ, set at engine startup, never a worker's or bench case's
# env dict).
_bwrap_path_cache: str | None = None

_SELF_TEST_TIMEOUT_SEC = 1.0


class SandboxUnavailable(Exception):
    """mode="bwrap" was requested but bwrap is missing or failed its
    self-test -- the caller must fail closed (StepFailed), never fall
    back to running the child unsandboxed."""


def available() -> bool:
    """True iff `bwrap` is on PATH and a 1-second self-test
    (`bwrap --unshare-all --ro-bind / / true`) exits 0. Cached per
    process after the first call, together with the resolved absolute
    path (see _bwrap_path())."""
    global _available_cache, _bwrap_path_cache
    if _available_cache is not None:
        return _available_cache
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        _available_cache = False
        return False
    try:
        result = subprocess.run(
            [bwrap, "--unshare-all", "--ro-bind", "/", "/", "true"],
            capture_output=True, timeout=_SELF_TEST_TIMEOUT_SEC,
        )
        _available_cache = result.returncode == 0
        if _available_cache:
            _bwrap_path_cache = os.path.realpath(bwrap)
    except (OSError, subprocess.TimeoutExpired):
        _available_cache = False
    return _available_cache


def _bwrap_path() -> str:
    """The absolute, resolved bwrap executable path found by available()
    -- wrap() execs THIS, never the bare string "bwrap": returning
    bare "bwrap" as argv[0] would let a case-controlled PATH entry
    from a worker/bench-case env dict resolve it to an attacker binary
    before any sandboxing took effect."""
    if not available() or _bwrap_path_cache is None:
        raise SandboxUnavailable("bwrap not available")
    return _bwrap_path_cache


def launcher_env() -> dict[str, str]:
    """The environment the bwrap LAUNCHER process itself (not the
    sandboxed child) is executed with -- a fixed minimal environment
    taken only from the trusted engine process's own os.environ, never
    from a worker's or bench case's requested child env. Case-
    controlled PATH/LD_PRELOAD/LD_LIBRARY_PATH must never reach the
    process that execs bwrap, since that could substitute the bwrap
    binary (if ever invoked by bare name) or inject a shared object
    into it before it applies the namespace it is supposed to isolate
    with. The sandboxed child's own environment is set independently,
    inside the sandbox, via `--setenv` (see wrap()'s `child_env`)."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def exec_env(mode: str, child_env: dict[str, str] | None) -> dict[str, str] | None:
    """The env a caller should pass to subprocess.Popen/run for the argv
    wrap() just built: mode="none" execs `child_env` directly (there is
    no bwrap process to separate it from), so it is returned unchanged;
    mode="bwrap" execs bwrap itself, which must run under
    launcher_env() regardless of `child_env` -- the child's env was
    already baked into the argv via `--setenv`."""
    if mode == "bwrap":
        return launcher_env()
    return child_env


def _reset_available_cache() -> None:
    """Test-only: forces the next available() call to re-probe bwrap."""
    global _available_cache, _bwrap_path_cache
    _available_cache = None
    _bwrap_path_cache = None


def _depth(path: Path) -> int:
    return len(path.parts)


def _is_strict_ancestor(ancestor: Path, descendant: Path) -> bool:
    if ancestor == descendant:
        return False
    try:
        descendant.relative_to(ancestor)
        return True
    except ValueError:
        return False


def _real(path: Path) -> Path:
    return Path(os.path.realpath(path))


def wrap(argv: list[str], *, cwd: Path, writable: tuple[Path, ...] = (),
         hidden: tuple[Path, ...] = (), readonly: tuple[Path, ...] = (),
         readonly_remap: tuple[tuple[Path, Path], ...] = (),
         net: bool = False, mode: str = "none",
         child_env: dict[str, str] | None = None) -> list[str]:
    """Build the argv engine.procs.spawn should actually exec. Callers
    must pass the RETURNED argv to subprocess with env=exec_env(mode,
    child_env), not with `child_env` directly -- see exec_env()'s
    docstring for why.

    mode="none": returns argv unchanged (list(argv), so callers may
    mutate the result without touching the caller's own list).
    `child_env` is not used (there is no bwrap process to bake it into
    -- the caller execs argv directly with it).

    mode="bwrap": returns a bwrap invocation wrapping argv, execing the
    absolute path available() resolved (never the bare string "bwrap",
    which a caller-controlled PATH could redirect). `child_env`, if
    given, is baked into the argv itself via `--clearenv` + one
    `--setenv` per entry, so the sandboxed child's environment is
    exactly `child_env` regardless of what env the bwrap launcher
    process is executed with. Raises SandboxUnavailable if available()
    is False -- never silently degrades to unsandboxed execution.

    `hidden`, `readonly` and `writable` are merged into one list of
    mount options and sorted by resolved DESTINATION path depth
    ascending (shallowest first), regardless of the order the caller
    passed them in or which of the three tuples a path came from --
    see the module docstring for why this, not grouping by kind, is
    the rule that keeps a deeper mount able to override a shallower
    one of any other kind. After sorting, a ValueError is raised (fail
    closed) if a `writable` path would still end up mounted AFTER a
    `hidden` path it is a strict ancestor of -- that specific ordering
    is exactly the shape of the bug this function exists to prevent,
    so it is never allowed to reach the built argv even if some future
    change to the sort breaks the depth invariant that currently
    prevents it.

    `readonly_remap`: (host_source, sandbox_dest) pairs, each mounted
    `--ro-bind <host_source> <sandbox_dest>` -- unlike every other
    category here, source and destination may differ (e.g. re-
    exposing a real toolchain cache dir at the same relative place
    under a sandboxed, otherwise-empty $HOME). Only the SOURCE must
    exist on the host; a nonexistent destination is fine and expected
    (bwrap creates the missing mountpoint, same as any other nested
    bind -- see the module docstring). The destination's depth is what
    sorting and the ancestor-of-hidden invariant check both use, since
    that is the sandbox-side location the mount actually shadows.
    """
    if mode not in _KNOWN_MODES:
        raise ValueError(f"unknown sandbox mode: {mode!r}")
    if mode == "none":
        return list(argv)

    if not available():
        raise SandboxUnavailable("bwrap not available")

    # (depth, kind, source, dest) -- kind in {"hide", "ro", "rw"}.
    # source == dest for every category except readonly_remap.
    mounts: list[tuple[int, str, Path, Path]] = []
    for path in hidden:
        real = _real(path)
        if not real.exists():
            continue
        mounts.append((_depth(real), "hide", real, real))
    for path in readonly:
        real = _real(path)
        if not real.exists():
            continue
        mounts.append((_depth(real), "ro", real, real))
    for source, dest in readonly_remap:
        real_source = _real(source)
        if not real_source.exists():
            continue
        real_dest = _real(dest)
        mounts.append((_depth(real_dest), "ro", real_source, real_dest))
    for path in writable:
        real = _real(path)
        if not real.exists():
            continue
        mounts.append((_depth(real), "rw", real, real))

    # Same destination requested as BOTH hidden and writable/readonly is
    # a direct contradiction, not an ordering question depth-sorting can
    # resolve (equal depth, so which one a stable sort emits last is an
    # implementation detail, not a guarantee) -- fail closed rather than
    # let tie-break order silently decide whether the path ends up
    # hidden or exposed.
    hide_dests_eq = {dest for _d, k, _s, dest in mounts if k == "hide"}
    other_dests_eq = {dest for _d, k, _s, dest in mounts if k in ("rw", "ro")}
    conflicts = hide_dests_eq & other_dests_eq
    if conflicts:
        raise ValueError(
            "sandbox mount conflict: "
            f"{sorted(str(p) for p in conflicts)} requested as both hidden "
            "and writable/readonly"
        )

    mounts.sort(key=lambda m: m[0])

    hide_dests = [dest for _d, k, _s, dest in mounts if k == "hide"]
    rw_dests = [dest for _d, k, _s, dest in mounts if k == "rw"]
    for rw_dest in rw_dests:
        for hide_dest in hide_dests:
            if _is_strict_ancestor(rw_dest, hide_dest) and \
                    mounts.index((_depth(rw_dest), "rw", rw_dest, rw_dest)) > \
                    mounts.index((_depth(hide_dest), "hide", hide_dest, hide_dest)):
                raise ValueError(
                    f"sandbox mount order violation: writable {rw_dest} "
                    f"would be mounted after hidden descendant {hide_dest}"
                )

    args: list[str] = [_bwrap_path(), "--unshare-all"]
    if net:
        args.append("--share-net")
    args += [
        "--die-with-parent",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
    ]

    hide_dir_dests: list[Path] = []
    for _depth_value, kind, source, dest in mounts:
        if kind == "hide":
            if dest.is_dir():
                args += ["--tmpfs", str(dest)]
                hide_dir_dests.append(dest)
            else:
                args += ["--ro-bind", "/dev/null", str(dest)]
        elif kind == "ro":
            args += ["--ro-bind", str(source), str(dest)]
        else:  # "rw"
            args += ["--bind", str(source), str(dest)]

    # Remount every tmpfs "hide" read-only, but only AFTER every nested
    # mount under it was already emitted above: bwrap must mkdir a new
    # mountpoint's directory entry before binding onto it, and it cannot
    # do that once the parent is already read-only (verified empirically,
    # same class of bug as the bench_home remap fix -- "mkdir ... :
    # Read-only file system"). Doing the remount last, after the whole
    # depth-sorted pass, means every nested writable/readonly bind under
    # a hidden directory still works, while the hidden directory itself
    # becomes genuinely immutable: a worker can no longer create, rename
    # or delete entries directly inside it (verified: mkdir and mv both
    # fail there, a nested bind still reads/writes normally, and a
    # nested read-only bind's mountpoint can no longer be renamed out of
    # the way either -- closing off a "renameable writable ancestor"
    # for anything placed under a hidden root, e.g. a facts file in a
    # gate-ro directory under the hidden $HOME).
    for dest in hide_dir_dests:
        args += ["--remount-ro", str(dest)]

    args += ["--chdir", str(_real(cwd))]
    if child_env is not None:
        args.append("--clearenv")
        for name in sorted(child_env):
            args += ["--setenv", name, child_env[name]]
    args += ["--"]
    args += list(argv)
    return args


__all__ = ["SandboxUnavailable", "available", "exec_env", "launcher_env", "wrap"]
