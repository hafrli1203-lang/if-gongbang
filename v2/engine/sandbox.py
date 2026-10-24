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

Known limit, verified (NOT a gap this module can close):
nesting a second wrap() call's bwrap invocation INSIDE a process that
is already running under a first bwrap sandbox does not work on a host
with AppArmor's unprivileged-userns restriction enforced (the common
case on current Ubuntu: `sysctl kernel.apparmor_restrict_unprivileged_
userns` = 1). Verified directly on this host: a bare `bwrap ... --
bwrap ...` (bwrap wrapping a second bwrap) fails with "No permissions
to create new namespace"; `strace -f` on the same command shows the
inner process's own `clone(...CLONE_NEWUSER...)` call itself returning
EPERM, not an application-level refusal inside bwrap. This matches
`/etc/apparmor.d/bwrap`'s own profile comment on this host ("bwrap is
allowed access to user namespaces and capabilities within the user
namespace, but its children do not have capabilities, blocking bwrap
from being able to be used to arbitrarily by-pass the user namespace
restrictions"): the `unpriv_bwrap` profile a nested bwrap exec
transitions into has `allow userns,` but `audit deny capability,`, and
(separately) plain `unshare --user` by an ordinary unconfined process
on this host also fails with the same EPERM -- so this is host policy
applying to ANY attempt at a second layer of unprivileged user-
namespace creation from inside the first sandbox, not something wrap()
could work around by building its argv differently (e.g. via a Claude
Code PreToolUse hook's `updatedInput`, which DOES let a caller rewrite
a worker's Bash command before it runs -- the CLI feature itself is
real and present in this host's installed `claude` binary, but the
nested-bwrap command it would need to wrap that rewritten command in
cannot actually execute here). A caller wanting to isolate ONE
subprocess more tightly than the sandbox its own process already runs
under must therefore do so at the OUTER layer (the first, non-nested
wrap() call that launches the whole worker/bench process tree), not by
having code running inside that sandbox invoke wrap() a second time.

 follow-up (the actual mitigation for the OUTER layer, since
nesting is closed off): engine.steps.station/review now build each
worker/reviewer's $HOME as a fresh, per-attempt directory (see
prepare_worker_home below) seeded with ONLY the real CLI's own login/
credentials file, instead of mounting the real operator HOME wholesale
read-only. The real HOME is hidden outright. Residual, accepted limit:
ANY code the worker/reviewer's allowed Bash command runs (same
unresolved nested-sandbox gap above) can still read that one login
file inside ITS OWN worker HOME -- e.g. a worker-run test file could
open $HOME/.claude/.credentials.json -- since it is a real file needed
for the CLI itself to authenticate and there is no narrower OS-level
primitive available on this host to hand the CLI process its own
credential while denying it to a child the CLI spawns. This is a much
smaller blast radius than before (one CLI's own login file, inside a
disposable per-attempt directory, instead of the operator's entire
real HOME -- other apps' configs, other CLIs' credentials, ssh keys),
not a complete closure.

Same shape, separate instance: a worker/reviewer whose runner carries
mcp_servers gets its MCP launch config (server names, argv, and any
embedded env_passthrough secrets such as API keys -- see
engine.workers.claude_cli._write_mcp_config_file) written to a file
under the engine's own gate-ro directory and passed to the CLI as
`--mcp-config <path>`. That directory is mounted read-only into the
WHOLE worker/reviewer sandbox (so the gate hook, and the CLI itself,
can read it), not narrowed to the CLI process alone -- there is no OS-
level primitive on this host (same gap as the credentials-file one
above) to hand the CLI its own `--mcp-config` file while denying read
access to a child the CLI's allowed Bash command spawns. Current
limit, not fixed by anything in this module: ANY code that command
runs can read that file and recover the MCP servers' own secrets,
exactly as it could read the CLI's own login file. Closing this would
need a separate MCP relay process (something standing between the
sandboxed worker and the real MCP server, holding the secret itself
rather than handing it to the sandbox at all) -- out of scope here.
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


def prepare_worker_home(dest: Path, real_home: Path | None,
                         login_files: tuple[str, ...],
                         writable_subpaths: tuple[str, ...]) -> None:
    """Build (or complete) a fresh, minimal $HOME at `dest` for a
    sandboxed CLI worker/reviewer process -- containing ONLY the real
    CLI's own login/credentials files, never the rest of the real,
    operator HOME this engine process itself runs under P1
    fallback: the real HOME used to be mounted read-only wholesale --
    see engine.steps.station/review's own `_sandboxed_argv` docstrings
    -- which left every OTHER app's config, other CLIs' credentials
    and e.g. ssh keys readable to any code a worker's allowed Bash
    command runs inside the SAME sandbox namespace; a NESTED, stricter
    bwrap per Bash command was investigated and found impossible on a
    host with AppArmor's unprivileged-userns restriction enforced --
    see this module's own "Known limit" note above -- so the fix here
    is at the one layer that IS adjustable: shrink what the whole
    worker process tree can reach in the first place).

    `dest` is created if missing. For each `real_home`-relative path in
    `login_files` (e.g. ".claude/.credentials.json", ".codex/
    auth.json") that exists as a plain file under `real_home` AND does
    not already exist under `dest`, a byte-for-byte copy (shutil.
    copy2, never a symlink -- a symlink back into `real_home` would
    reopen exactly the path this function exists to close) is made at
    the same relative path under `dest`, creating parent directories
    as needed. Already-idempotent: a destination path that already
    exists (an earlier call's copy, or session state the CLI itself
    wrote into THIS worker's own, already-isolated home on a previous
    attempt/retry of the same request) is left untouched -- this runs
    on every spawn, including a reconciled retry, and must never
    clobber state the sandboxed CLI already wrote.

    `writable_subpaths` (a runner's own `home_writable_subpaths`, e.g.
    (".claude", ".claude.json")): any entry with NO filename suffix
    (".claude", ".codex" -- a directory, by every shape seen in this
    codebase so far) is created as a real, empty directory under
    `dest` if it does not already exist, so the caller's own writable
    mount for it (sandbox.wrap's `writable` -- a nonexistent source is
    silently skipped there) actually takes effect even when no
    `login_files` entry happened to create that directory as a side
    effect of its own parent-mkdir. An entry WITH a suffix (e.g.
    ".claude.json") is left alone -- a single top-level FILE the CLI
    itself creates on its own first write if `login_files` did not
    already seed it from `real_home`; this function never invents a
    config file's content."""
    dest.mkdir(parents=True, exist_ok=True)
    if real_home is not None:
        for rel in login_files:
            target = dest / rel
            if target.exists():
                continue
            source = real_home / rel
            if not source.is_file():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for rel in writable_subpaths:
        if Path(rel).suffix:
            continue
        (dest / rel).mkdir(parents=True, exist_ok=True)


def _shebang_interpreter(path: Path) -> str | None:
    """The interpreter named on `path`'s own `#!` line, or None if the
    file does not start with one (a native/compiled binary -- e.g.
    this host's own `claude.exe`, an ELF executable despite the `.exe`
    name -- or a file that cannot even be opened/read). `env <name>`
    is unwrapped to `<name>` (the common `#!/usr/bin/env node` shape);
    any other shape returns the first token on the line verbatim,
    which may already be an absolute path (shutil.which handles both:
    see resolve_runtime)."""
    try:
        with open(path, "rb") as f:
            head = f.read(256)
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return None
    try:
        line = head.split(b"\n", 1)[0].decode("utf-8")
    except UnicodeDecodeError:
        return None
    tokens = line[2:].strip().split()
    if not tokens:
        return None
    if Path(tokens[0]).name == "env" and len(tokens) > 1:
        return tokens[1]
    return tokens[0]


def _install_dir(resolved: Path) -> Path:
    """The directory to mount read-only so `resolved` (and whatever it
    needs alongside it) can actually run: `resolved`'s own parent,
    UNLESS that parent is literally named "bin" -- the common npm-
    global-install shape (confirmed on this host for both `claude` and
    `codex`: each is a thin launcher at `<pkg_root>/bin/<name>` that
    needs sibling `node_modules`/`package.json` under `<pkg_root>`
    itself to run, not just the one launcher file) -- in which case
    the GRANDPARENT (the package root) is returned instead."""
    parent = resolved.parent
    if parent.name == "bin":
        return parent.parent
    return parent


def resolve_runtime(exe_name: str) -> tuple[Path, ...]:
    """Read-only mounts that must stay visible inside a sandbox whose
    `hidden` set includes the real operator HOME wholesale (see
    engine.steps.station/review's own worker/reviewer sandbox policy),
    so a CLI resolved by bare name through PATH (a runner's argv[0],
    e.g. "claude"/"codex") can still be found and started, without
    ever re-exposing the real HOME itself: () if `exe_name` is not
    found via shutil.which on THIS process's own PATH (same trusted-
    os.environ reasoning as available()/launcher_env() -- never a
    worker/case-controlled PATH).

    Otherwise: the PARENT DIRECTORY of the path shutil.which resolved
    (e.g. ~/.local/bin -- that path itself is usually a symlink, and
    on every installation shape seen so far, SOMEWHERE under the real
    HOME, hence invisible once HOME is hidden unless re-mounted
    explicitly). The symlink's own parent directory, never the
    symlink path by itself, is what gets mounted here: every other
    mount this module builds (wrap()'s `readonly`/`writable`/`hidden`,
    and even `readonly_remap`'s own destination) is keyed by
    os.path.realpath, which -- run on the HOST, where the symlink
    genuinely exists -- always follows it fully; asking wrap() to
    mount "the symlink path" by that path alone therefore mounts its
    REAL TARGET's location instead, never creating the symlink's own
    directory entry inside the sandbox at all (confirmed empirically:
    PATH lookup for the bare command then fails "No such file or
    directory", even though the install directory below, and the
    binary it actually points to, are both mounted and present).
    Mounting the parent directory side-steps this entirely -- the
    symlink (and anything else directly alongside it) is simply
    PART of that directory's own content, nothing extra to resolve.

    PLUS _install_dir(realpath(that path)) -- the resolved target's
    own directory (see _install_dir's docstring for the npm-global
    "bin" special case). If the resolved target is itself a script
    with a `#!` shebang (see _shebang_interpreter -- e.g. `codex`'s
    own `codex.js`, `#!/usr/bin/env node`), the named interpreter is
    resolved the SAME way (which + realpath) and ITS install
    directory is mounted too -- one level of shebang-chasing only; an
    interpreter that is itself another shebang script is not chased
    further (nothing installed on this host, or described by the team
    lead, is shaped that way).

    Every returned path is exactly what a caller should add to
    sandbox.wrap's own `readonly` tuple -- never `writable`: none of
    this is state the sandboxed CLI needs to modify, only its own
    installed code, same reasoning as engine.steps.station.
    ENGINE_ROOT's own readonly mount for this engine's own package.

    Verified directly on this host, under mode="bwrap", with the real
    operator HOME hidden wholesale: a sandboxed `claude --version` and
    `codex --version` both start and print their version (rc 0) with
    exactly these mounts added, while a sentinel file placed elsewhere
    under the same real HOME stays unreadable."""
    which_path = shutil.which(exe_name)
    if which_path is None:
        return ()
    symlink_path = Path(which_path)
    resolved = Path(os.path.realpath(symlink_path))
    mounts = [symlink_path.parent, _install_dir(resolved)]
    interp_name = _shebang_interpreter(resolved)
    if interp_name is not None:
        interp_which = shutil.which(interp_name)
        if interp_which is not None:
            interp_symlink = Path(interp_which)
            interp_resolved = Path(os.path.realpath(interp_symlink))
            mounts.append(interp_symlink.parent)
            mounts.append(_install_dir(interp_resolved))
    return tuple(mounts)


__all__ = [
    "SandboxUnavailable", "available", "exec_env", "launcher_env",
    "prepare_worker_home", "resolve_runtime", "wrap",
]
