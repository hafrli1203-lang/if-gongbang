"""Child processes per station, each in its own session/process group,
recorded in the ledger so a closed station can kill everything it spawned.
"""
from __future__ import annotations

import ctypes
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from engine.infrastructure import sandbox
from engine.infrastructure.db.ledger import Event, Ledger

# The standalone writer script (no `engine` package imports of its own
# -- see its module docstring) that spawn()'s unsandboxed `sh` wrapper
# invokes by absolute path to actually record the exit code. A sibling
# of this file, so it is always resolvable regardless of PYTHONPATH.
_EXIT_WRITER = Path(__file__).resolve().with_name("exit_writer.py")

# Resolved once at import time, NOT inside _die_with_engine() itself --
# dlopen can take a lock, and a preexec_fn runs in a freshly forked
# child where only the calling thread was duplicated (see
# _die_with_engine's own docstring); doing the dlopen here, long before
# any fork, means the preexec_fn itself only ever makes one plain libc
# call through an already-resolved function pointer. None on any
# platform/host where this fails (non-Linux, or no libc.so.6 by that
# exact name) -- _die_with_engine then does nothing, same as today.
try:
    _LIBC: ctypes.CDLL | None = ctypes.CDLL("libc.so.6", use_errno=True)
except OSError:
    _LIBC = None

_PR_SET_PDEATHSIG = 1  # Linux prctl(2) option; see _die_with_engine.


def _die_with_engine(expected_parent_pid: int) -> None:
    """preexec_fn for spawn()'s own Popen call below (bound to this
    call's own `expected_parent_pid` via a lambda at the call site,
    since Popen's preexec_fn itself takes no arguments) -- runs in the
    freshly forked child, after fork but before exec, asking the
    kernel (Linux prctl, PR_SET_PDEATHSIG) to SIGKILL THIS process the
    moment its real parent -- THIS engine process, as it existed at
    the instant spawn() forked -- exits for ANY reason: a clean exit,
    a crash, an operator SIGKILL of the engine itself.

    start_new_session=True (passed to the same Popen call, as it
    already was before this existed) only moves the child into its
    OWN, new process group/session, so a SIGINT/SIGHUP delivered to
    the engine's controlling terminal does not also reach it -- that
    is a completely separate concern from the engine process's own
    death, which it does nothing about: an ordinary Unix child whose
    parent exits is simply reparented to init (pid 1) and keeps
    running, orphaned, for however long it likes. PR_SET_PDEATHSIG is
    the one piece that closes that specific gap, and it tracks the
    real parent/child relationship, not the session -- setsid() does
    not reset or retarget it.

    This closes the gap for exactly the immediate child spawn() is
    about to exec (the unsandboxed `sh` exit-code wrapper, or the raw
    argv when no exit_file is used at all) -- see spawn()'s own
    docstring for how that single, closed link is enough to bring
    down a WHOLE mode="bwrap" sandboxed tree with it (bwrap's own
    --die-with-parent, built by engine.infrastructure.sandbox.wrap, already ties the
    sandboxed process to the `sh` wrapper that execs it; this makes
    the `sh` wrapper itself die the moment the ENGINE does, so the two
    mechanisms chain together). Under mode="none" there is no bwrap
    anywhere in the chain, and `sh -c '"$@"; ...'` must fork (not
    exec) to run "$@" so it can still write the exit code afterward --
    that forked grandchild has no pdeathsig of its own and is NOT
    reached by this fix; see spawn()'s own docstring's "Known limit"
    paragraph for that one, narrower, still-open gap.

    Remaining race (external review, P2) and its close: fork() and
    this prctl call are two separate steps -- if the engine dies in
    the (normally tiny) window between them, this child is reparented
    to init BEFORE PR_SET_PDEATHSIG is ever armed, so the SIGKILL this
    is meant to deliver never gets scheduled at all, and the child
    keeps running orphaned, exactly the outcome this whole mechanism
    exists to prevent. `expected_parent_pid` is os.getpid() read by
    the PARENT, in the parent's own process, immediately before
    Popen() forks -- the real parent pid this child is SUPPOSED to
    have right after that fork, before any reparenting could possibly
    happen. Immediately after the prctl call (armed or not), this
    compares os.getppid() (read fresh, in the child, right now)
    against that value: if they differ, the parent is already gone --
    reparented to init -- so this exits immediately via os._exit (not
    sys.exit/return: this is still before exec, and must never run
    any more Python, let alone the real child argv, having already
    lost its supervising engine)."""
    if _LIBC is not None:
        try:
            _LIBC.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
        except OSError:
            pass
    if os.getppid() != expected_parent_pid:
        os._exit(1)


def exit_file_for(stdout_path: Path) -> Path:
    """The file spawn() has the real command write its own exit code
    into, derived deterministically from `stdout_path` -- never a
    separate parameter a caller also has to pass, and never a path inside stdout_path's OWN (worker-writable)
    directory any more either: it is a SIBLING "exit" directory of
    that directory's own parent -- typically .../stations/<request>.
    stdout -> .../exit/<request>.stdout.exit for engine.application.steps.worker_step/
    review's own stdout_path shape (home/jobs/<attempt>/stations/
    <request>.stdout) -- never the same worker-writable `stations`
    directory stdout_path itself lives in.

    This needs no change to engine.application.steps.worker_step/review at all to
    become engine-only: "exit" is just another plain sibling of
    "stations"/"gate-ro"/"worker-home" under the SAME `home/jobs/
    <attempt>/` tree those modules' own _sandboxed_argv already hides
    wholesale from every sandboxed child and never re-exposes (neither
    one adds a "exit" entry to any writable/readonly mount list), so
    under sandbox mode="bwrap" this directory does not exist inside
    the sandbox at all -- exactly the same "hidden by not being
    mounted" effect engine.application.steps.worker_step's own gate-ro directory
    achieves, with no dedicated mount of its own either.

    Before this: the exit file sat right next to
    stdout_path in the SAME worker-writable directory. Any code the
    worker's sandbox runs (not just its own gated tool calls -- e.g.
    the project's own test suite, once an allowed Bash command invokes
    it) could reach that directory and swap the file for a symlink
    before the real command exited (making the unsandboxed `sh`
    wrapper that later writes the exit code write through it instead),
    or pre-plant a forged '0' so a crashed/malicious worker is wrongly
    read back as a success on resume. See exit_writer.py's own module
    docstring for the write-side defense (O_EXCL|O_NOFOLLOW) kept as a
    second layer on top of this directory-level isolation. Every call
    site that spawns a worker gives procs.spawn a stdout_path AND
    always lists that same path first in the progress_paths it later
    hands engine.infrastructure.workers.base.wait_while_progressing, so that function
    derives the identical (now-safe) path from progress_paths[0] with
    no new argument of its own either."""
    return stdout_path.parent.parent / "exit" / (stdout_path.name + ".exit")


def stdin_file_for(stdout_path: Path) -> Path:
    """The file a caller that wants the real command's instructions fed
    over stdin (rather than as an argv element) should write the prompt
    text into, derived deterministically from `stdout_path` -- the same
    shape as exit_file_for's own sibling-directory rule, and for the
    identical reason: a `claude`/`codex` CLI's full prompt can be
    hundreds of KB, far past Linux's MAX_ARG_STRLEN (131072 bytes) for
    a single argv element, which makes exec itself fail with E2BIG --
    see claude_cli.ClaudeRunner/codex_cli.CodexRunner's own module
    docstrings for the live incident this closes. A SIBLING "stdin"
    directory of stdout_path's own parent -- typically .../stations/
    <request>.stdout -> .../stdin/<request>.stdout.stdin -- never a
    path inside stdout_path's own (worker-writable) stations directory:
    the file only needs to be openable by THIS process, on the host,
    strictly before the real command is exec'd (spawn() opens it and
    hands the child an already-dup'd fd 0, exactly like stdout_path
    itself), so it needs no sandbox mount of its own at all -- fd 0 is
    inherited across fork/exec regardless of bwrap's own mount
    namespace. Keeping it out of the worker-writable tree anyway (same
    "exit" treatment) means nothing the worker's own sandbox runs can
    race writing over it before spawn() opens it."""
    return stdout_path.parent.parent / "stdin" / (stdout_path.name + ".stdin")


def write_stdin_file(path: Path, text: str) -> None:
    """Writes `text` (a runner's full prompt) to a fresh 0600 file at
    `path` -- same O_NOFOLLOW/fchmod-on-the-open-fd shape as
    claude_cli._write_mcp_config_file, for the same reason: an
    attacker-planted symlink at this path must be refused outright
    rather than written through, and the file must never be briefly
    world/group-readable between creation and chmod."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
    finally:
        os.close(fd)


def _matching(ledger: Ledger, project: str, kind: str, attempt_id: str,
              station: str, request_id: str | None) -> list[dict[str, object]]:
    return [event.payload for _seq, event in ledger.events(project, kind)
            if event.attempt_id == attempt_id
            and event.payload.get("station") == station
            and event.payload.get("request_id") == request_id]


def _has_unresolved_intent(ledger: Ledger, project: str, attempt_id: str,
                           station: str, request_id: str | None) -> bool:
    """True iff some EARLIER spawn() call for this exact (attempt_id,
    station, request_id) appended proc.launch_intent but crashed before
    its own proc.started could follow -- see spawn()'s own docstring.
    A request_id of None still matches correctly (both sides compare
    equal via plain `==`), the same identity shape _reconcile_spawn_
    worker-style callers already key proc.started lookups on
    elsewhere (engine.application.steps.worker_step/review)."""
    intents = _matching(ledger, project, "proc.launch_intent", attempt_id,
                        station, request_id)
    if not intents:
        return False
    started = _matching(ledger, project, "proc.started", attempt_id,
                        station, request_id)
    return not started


def _find_live_pid_by_stdout(stdout_path: Path) -> int | None:
    """The smallest live pid whose own fd 1 (stdout) resolves to
    `stdout_path` -- the identity spawn() can still check even though
    the crashed call never got to record a real pid: Popen always
    opens `stdout_path` itself and hands the child a dup'd fd 1 onto
    it (see spawn()'s own Popen call), and that fd is inherited
    unchanged across both fork (a plain Unix guarantee) and exec (no
    close-on-exec is ever set on it), so it still points there no
    matter how deep the process actually spawned is in the `sh`/bwrap
    wrapping chain spawn() builds. The SMALLEST matching pid is picked
    deliberately: when more than one process in the same chain shares
    this fd (e.g. the unsandboxed mode="none" fork case spawn()'s own
    docstring describes), the outermost one spawn() itself started
    (the `sh` wrapper, or the raw command with no wrapping at all) was
    always forked earliest and so always has the lowest pid among
    them -- the same one a non-crashed spawn() call would have
    recorded as `pid` in its own proc.started event. None if nothing
    on this host currently has that fd open on that path (the crash
    happened before Popen ever ran, or the process has since exited --
    either way there is nothing to adopt; the caller spawns fresh)."""
    target = os.path.realpath(stdout_path)
    try:
        candidates = [int(name) for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return None
    matches: list[int] = []
    for pid in candidates:
        try:
            fd1 = os.readlink(f"/proc/{pid}/fd/1")
        except OSError:
            continue
        if fd1 == target:
            matches.append(pid)
    return min(matches) if matches else None


# A pid value no spawn() call's own real Popen can ever produce (a
# kernel pid is always a small positive number, well under this on
# every Linux pid_max setting -- the default is 4194304, and even the
# documented maximum with a 64-bit pid_max is 2**22). Recorded as the
# pid/pgid of a RECOVERED, already-finished dispatch (see
# _recover_completed_dispatch) so every existing pid-keyed reader
# (engine.infrastructure.procs.is_alive, engine.infrastructure.workers.base.wait_for_exit) reliably
# finds "no such process" and falls back to the real evidence (the
# exit file this never-forked-by-us dispatch already wrote) instead of
# trying to supervise a process this process never actually started.
_RECOVERED_PID = 2**31 - 1


def _completed_receipt_code(exit_file: Path | None) -> int | None:
    """The real exit code an EARLIER spawn() call's own exit-writer
    wrapper already wrote to `exit_file`, if any -- None when the file
    does not exist or does not hold a bare integer. Same read rule as
    engine.infrastructure.workers.base._read_exit_file for the identical file,
    duplicated rather than imported: engine.infrastructure.procs must not depend on
    engine.infrastructure.workers (the reverse import direction already exists --
    engine.infrastructure.workers.base imports exit_file_for from this module)."""
    if exit_file is None:
        return None
    try:
        text = exit_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _recover_completed_dispatch(ledger: Ledger, project: str, attempt_id: str,
                                station: str, request_id: str | None,
                                code: int) -> SimpleNamespace:
    """Adopt a dispatch THIS process never actually forked, but whose
    own exit-writer wrapper already ran to completion: an earlier
    spawn() call's Popen succeeded and the real command finished and
    wrote `code` to its exit file, before that call's own engine
    process crashed strictly between Popen succeeding and its
    proc.started append (see spawn()'s own docstring). Recording an
    ordinary proc.started (pid/pgid=_RECOVERED_PID) -- immediately
    followed by a matching proc.ended, since this dispatch is already
    known, with certainty, to be over -- rather than deleting the real
    exit file and the real, already-written stdout and launching a
    SECOND, fully redundant process (the bug this closes). Pairing
    started+ended right here, rather than leaving the row "live" for
    some later close_station() to end, means engine.infrastructure.db.ledger.live_procs
    never reports this station as having anything to signal/wait on
    for a pid this process never actually owns.

    Neither the exit file nor stdout_path is touched -- both are left
    exactly as the original, completed run left them, since they ARE
    the evidence this adopts rather than discards."""
    ledger.append(Event(
        project=project, kind="proc.started", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"pid": _RECOVERED_PID, "pgid": _RECOVERED_PID,
                  "station": station, "request_id": request_id,
                  "recovered_completed": True, "recovered_exit_code": code},
    ))
    ledger.append(Event(
        project=project, kind="proc.ended", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"pid": _RECOVERED_PID},
    ))
    return SimpleNamespace(pid=_RECOVERED_PID)


def _adopt_if_live(ledger: Ledger, project: str, attempt_id: str, station: str,
                   request_id: str | None,
                   stdout_path: Path | None) -> SimpleNamespace | None:
    """None unless an earlier spawn() call for this exact identity left
    an unresolved proc.launch_intent AND the real process it started
    is still findable and alive (see _find_live_pid_by_stdout) -- in
    which case this call must ADOPT that process (append proc.started
    for it, with "adopted": True, so every existing proc.started-keyed
    reconcile/liveness/close_station check sees it exactly as if a
    non-crashed spawn() call had started it) rather than spawn a
    SECOND, duplicate one. `stdout_path` is required for adoption
    (None -- e.g. a caller with no stdout file at all -- always
    returns None here, falling through to a normal, fresh Popen): with
    no stdout file there is no identity left to search by at all.

    The returned SimpleNamespace carries only `.pid` -- callers that
    need more (`.wait()`, `.terminate()`) only ever do so on a FRESH
    spawn() call's own real Popen, never on a resumed/reconciled one:
    engine.application.steps.worker_step/review's own spawn_worker steps read `.pid`
    once and return, and every later interaction with the process
    (liveness, close_station) goes through the ledger's proc.started
    row and engine.infrastructure.procs' own pid/pgid-based helpers, not back through
    this object."""
    if stdout_path is None:
        return None
    if not _has_unresolved_intent(ledger, project, attempt_id, station, request_id):
        return None
    pid = _find_live_pid_by_stdout(stdout_path)
    if pid is None:
        return None
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return None
    ledger.append(Event(
        project=project, kind="proc.started", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"pid": pid, "pgid": pgid, "station": station,
                  "request_id": request_id, "adopted": True},
    ))
    return SimpleNamespace(pid=pid)


def spawn(ledger: Ledger, project: str, attempt_id: str, station: str,
          argv: list[str], cwd: Path, *, env: dict[str, str] | None = None,
          stdout_path: Path | None = None, exit_file: Path | None = None,
          stdin_devnull: bool = False, stdin_path: Path | None = None,
          pass_fds: tuple[int, ...] = (),
          request_id: str | None = None) -> "subprocess.Popen[bytes] | SimpleNamespace":
    """Popen with start_new_session=True; append proc.started {pid, pgid,
    station, request_id}.

    BEFORE any of that, this records a proc.launch_intent event for
    this exact (attempt_id, station, request_id) identity, and checks
    whether an EARLIER call already recorded one with no resolving
    proc.started -- see _adopt_if_live. This closes a crash window
    that otherwise left a real, running process with NO ledger record
    at all: Popen succeeding and the proc.started append below are two
    separate steps: an engine crash strictly between them used to mean
    nothing ever recorded that pid, so a later retry of the same
    station+request (engine.application.steps.worker_step/review's own reconcile,
    keyed on proc.started) found no matching event, saw nothing alive
    by that account, and spawned a SECOND, duplicate process while the
    first kept running, forever unsupervised (never reachable by
    close_station's own pgid-keyed SIGTERM/SIGKILL, since nothing
    recorded its pid/pgid at all). Recording intent first and checking
    for a live, adoptable match on every call closes it: a retried
    call either finds and adopts the still-running first process (no
    duplicate spawned), or finds nothing alive to adopt and the
    earlier command already finished and left a real exit-file
    receipt (adopted via _recover_completed_dispatch instead of
    discarded -- see below), or finds nothing alive AND no finished
    receipt either, with no earlier intent at all to resolve, and
    proceeds exactly as before (a fresh Popen).

    A retry that finds an earlier unresolved intent but no live
    process to adopt used to treat that identically to "no earlier
    intent at all": delete whatever exit file/stdout existed and
    spawn fresh -- which is correct when the earlier Popen's real
    command was itself killed/crashed before finishing (nothing
    finished to lose; see test_resume_after_a_killed_engine_sh_bwrap_
    chain_leaves_no_orphan_and_no_duplicate for that already-safe
    case, exercised against a real engine->sh->bwrap chain), but was
    wrong when the earlier command had in fact already run its
    exit-writer wrapper to completion: THAT case now checks the exit
    file first and adopts its real, already-written code via
    _recover_completed_dispatch instead of deleting the receipt and
    launching a second, fully redundant process for identical work.

    env=None inherits this process's own environment (Popen's default),
    exactly as before this parameter existed; a worker runner instead
    passes an explicit dict (its own small passthrough allowlist plus
    GB_* identity vars) so the child never inherits the operator's full
    environment. pass_fds: fds this process already has open that the
    CHILD must inherit (e.g. engine.infrastructure.sandbox.pass_fds_for(argv), for a
    `--args FD`-based env pipe engine.infrastructure.sandbox.wrap() built) -- empty
    by default, identical to never passing it. This process's own
    copy of every fd named here is closed (see engine.infrastructure.sandbox.
    close_fds) on every return path, whether or not a fresh Popen
    actually ran, since the caller has no other chance to close it
    otherwise.

    stdout_path, if given, is opened for combined
    stdout+stderr and closed again once Popen has started the child (the
    child already has its own duplicated fd by then); stdin_devnull
    redirects stdin from /dev/null instead of inheriting it (needed by
    e.g. `codex exec`, which otherwise blocks on stdin). stdin_path, if
    given, is opened for reading (same open-then-close-after-Popen-
    starts shape as stdout_path) and the child's stdin is that file
    instead -- used by a runner whose CLI reads its prompt from stdin
    rather than an argv element (see stdin_file_for's own docstring for
    why: a multi-hundred-KB prompt as a single argv element can exceed
    Linux's MAX_ARG_STRLEN and make exec itself fail with E2BIG).
    stdin_path and stdin_devnull are mutually exclusive -- both naming a
    stdin source for the same spawn is a caller bug, not a runtime
    condition to silently resolve one way, so this raises ValueError
    immediately rather than picking one.

    exit_file defaults to exit_file_for(stdout_path) when not given
    explicitly (None if stdout_path is also None -- no wrapping at
    all, matching every behavior before either parameter existed). A
    caller may still pass an explicit exit_file of its own (independent
    of stdout_path, or even without a stdout_path at all) -- but every
    existing call site (engine.application.steps.worker_step/review) needs no change
    at all to get the safe, engine-only location: it already always
    gives stdout_path, and exit_file_for's own docstring is what moved
    the derived path out of the worker-writable directory, not a new
    parameter threaded through those modules.

    exit_file, once resolved, must be a path under an engine-only
    directory a worker/reviewer sandbox never has write (or even read)
    access to -- see exit_file_for's docstring. This process unlinks
    any stale file at that path itself (never following a symlink
    there: Path.unlink removes the link, not its target) strictly
    before the real command starts. The real argv is then wrapped as
    `sh -c '"$@"; code=$?; <python> <exit_writer.py> "$0" "$code";
    exit "$code"' <exit_file> <argv...>` -- sh's own convention makes
    the first operand after the script text $0 (never part of "$@"),
    so the wrapped command's own argv[0]/quoting reaches the real
    process completely unchanged. This runs OUTSIDE whatever sandbox
    wrapping (e.g. a bwrap invocation) argv already carries -- it
    wraps the whole spawned command, sandboxed or not, rather than
    reaching inside it -- and the wrapper always `exit`s with the REAL
    command's own exit code regardless of whether the writer itself
    succeeded (never the writer's own exit status), so every existing
    caller of this exit code is unaffected.

    Engine death policy: the Popen call below also sets preexec_fn=
    (a lambda closing over this_pid, this process's own pid read right
    before the fork) calling _die_with_engine, so the immediate child
    (this `sh` wrapper, or the raw argv when no exit_file is used) is
    itself killed the moment THIS engine process exits, for any reason
    -- see _die_with_engine's own docstring for the mechanism
    (PR_SET_PDEATHSIG), the fork-to-prctl race its ppid check closes,
    and why start_new_session=True alone does not already provide
    this. Combined with bwrap's own --die-with-parent
    (engine.infrastructure.sandbox.wrap, tied to whatever process execs the bwrap
    invocation -- here, this same `sh` wrapper), a mode="bwrap"
    sandboxed tree is fully chained to the engine process's own
    lifetime: engine dies -> `sh` dies (this fix) -> the sandboxed
    process dies (bwrap's own flag) -> everything bwrap itself spawned
    inside the sandbox dies with it (bwrap's own guarantee). Verified
    directly (tests/unit/test_procs.py): spawning a REAL, separate
    Python process that itself calls this function under mode="bwrap"
    and then SIGKILLing that process leaves no descendant alive.

    Known limit, NOT closed by the above: under mode="none" (no bwrap
    anywhere in the chain), `sh -c '"$@"; code=$?; ...'` must FORK
    (never exec) to run "$@", since it still has to write the exit
    code afterward -- that forked grandchild is a plain Unix child of
    `sh` with no pdeathsig of its own, so it is NOT reached by
    _die_with_engine killing `sh`: if `sh` dies while the real,
    unsandboxed command is still running, that command is simply
    reparented to init and keeps running, orphaned. Closing this would mean
    replacing the `sh -c` wrapper itself with a small supervisor
    process that forks its own child with its OWN pdeathsig pointed at
    the supervisor (rather than relying on a shell's lack of any such
    primitive) -- a larger rewrite of this exact wrapping mechanism,
    out of scope here; mode="bwrap" (where this IS fully closed) is
    the mode every worker/reviewer/bench sandbox actually runs under
    in production (engine.config.EngineConfig.sandbox)."""
    if stdin_path is not None and stdin_devnull:
        raise ValueError("stdin_path and stdin_devnull are mutually exclusive")
    adopted = _adopt_if_live(ledger, project, attempt_id, station, request_id,
                             stdout_path)
    if adopted is not None:
        sandbox.close_fds(pass_fds)
        return adopted
    if (stdout_path is not None
            and _has_unresolved_intent(ledger, project, attempt_id, station, request_id)):
        # _adopt_if_live already ruled out a still-live process for this
        # exact identity -- an earlier call's launch_intent is still
        # unresolved (no proc.started ever followed it) with nothing
        # alive to adopt. Before falling through to the delete-exit-
        # file/truncate-stdout/relaunch path below, check whether that
        # earlier dispatch actually ran all the way to completion: its
        # own exit-writer wrapper already wrote a real code to the
        # exit file (see _recover_completed_dispatch for why that
        # case must adopt the existing receipt rather than discard
        # it). No valid receipt there means the earlier attempt did
        # NOT run its wrapper to completion -- whether because Popen
        # itself never ran, or because the real command was killed/
        # crashed before finishing (the already-covered, intentional
        # case this function's docstring and
        # test_resume_after_a_killed_engine_sh_bwrap_chain_leaves_no_
        # orphan_and_no_duplicate exercise against a REAL engine->sh->
        # bwrap chain) -- either way there is nothing finished to lose
        # by falling through to a fresh Popen below.
        pending_exit_file = exit_file if exit_file is not None else exit_file_for(stdout_path)
        code = _completed_receipt_code(pending_exit_file)
        if code is not None:
            sandbox.close_fds(pass_fds)
            return _recover_completed_dispatch(
                ledger, project, attempt_id, station, request_id, code)
    ledger.append(Event(
        project=project, kind="proc.launch_intent", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"station": station, "request_id": request_id,
                  "stdout_path": str(stdout_path) if stdout_path is not None else None},
    ))
    if exit_file is None and stdout_path is not None:
        exit_file = exit_file_for(stdout_path)
    stdout_handle = None
    stdin_handle = None
    real_argv = argv
    if exit_file is not None:
        exit_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(exit_file.parent, 0o700)
        except OSError:
            pass
        try:
            exit_file.unlink()
        except FileNotFoundError:
            pass
        writer_cmd = f'{shlex.quote(sys.executable)} {shlex.quote(str(_EXIT_WRITER))}'
        real_argv = [
            "sh", "-c",
            f'"$@"; code=$?; {writer_cmd} "$0" "$code"; exit "$code"',
            str(exit_file), *argv,
        ]
    # Read in THIS process, right before the fork below -- see
    # _die_with_engine's own "Remaining race" paragraph for why this
    # exact value, read at this exact point, is what closes the
    # fork-to-prctl race rather than merely narrowing it.
    this_pid = os.getpid()
    try:
        if stdout_path is not None:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stdout_handle = open(stdout_path, "wb")
        if stdin_path is not None:
            stdin_handle = open(stdin_path, "rb")
        if stdin_handle is not None:
            stdin_arg = stdin_handle
        elif stdin_devnull:
            stdin_arg = subprocess.DEVNULL
        else:
            stdin_arg = None
        proc = subprocess.Popen(
            real_argv, cwd=str(cwd), env=env,
            stdout=stdout_handle if stdout_handle is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if stdout_handle is not None else subprocess.DEVNULL,
            stdin=stdin_arg,
            start_new_session=True,
            preexec_fn=lambda: _die_with_engine(this_pid),
            pass_fds=pass_fds,
        )
    finally:
        if stdout_handle is not None:
            stdout_handle.close()
        if stdin_handle is not None:
            stdin_handle.close()
        # The child has its own copy via fork by the time Popen's
        # constructor returns (or never will, if it raised) -- this
        # process's own copy is never needed again either way.
        sandbox.close_fds(pass_fds)
    pgid = os.getpgid(proc.pid)
    ledger.append(Event(
        project=project, kind="proc.started", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"pid": proc.pid, "pgid": pgid, "station": station,
                  "request_id": request_id},
    ))
    return proc


def close_station(ledger: Ledger, project: str, attempt_id: str, station: str,
                  grace_sec: float = 0.5) -> list[int]:
    """SIGTERM every recorded pgid of that station, wait grace_sec, SIGKILL
    any process GROUP still alive, wait up to grace_sec again, append
    proc.ended for each recorded pid. Liveness is checked at the process
    group (not just the recorded leader pid): a descendant that ignores
    SIGTERM, left behind after its leader already exited, keeps the group
    alive and must still be reached by the group SIGKILL. proc.ended is
    recorded only once every group is confirmed gone; otherwise raises
    RuntimeError and records nothing, so a live survivor is never reported
    as closed."""
    live = ledger.live_procs(attempt_id, station)
    if not live:
        return []

    pgids = {pgid for _, pgid in live}
    pids = {pid for pid, _ in live}
    _signal_group(pgids, signal.SIGTERM)
    _wait_until_groups_gone(pgids, pids, grace_sec)

    if _any_group_alive(pgids):
        _signal_group(pgids, signal.SIGKILL)
        _wait_until_groups_gone(pgids, pids, grace_sec)

    if _any_group_alive(pgids):
        raise RuntimeError("station_procs_alive")

    killed: list[int] = []
    for pid, _ in live:
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        killed.append(pid)
        ledger.append(Event(
            project=project, kind="proc.ended", actor_kind="processor",
            actor_id="processor", attempt_id=attempt_id, payload={"pid": pid},
        ))
    return killed


def _signal_group(pgids: set[int], sig: int) -> None:
    for pgid in pgids:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _any_group_alive(pgids: set[int]) -> bool:
    return any(_group_alive(pgid) for pgid in pgids)


def _wait_until_groups_gone(pgids: set[int], recorded_pids: set[int],
                            grace_sec: float) -> None:
    """Poll until every group is gone or grace_sec elapses. Opportunistically
    reaps each recorded pid (WNOHANG) on every tick: a killed direct child
    becomes a zombie — which still answers os.killpg(pgid, 0) as "alive" —
    until its parent reaps it, so a liveness check taken without reaping
    first would spuriously see the group as still alive forever. A
    descendant that is not our child (left behind by a dead leader) is
    reaped by init on its own once killed; this loop just gives that time."""
    deadline = time.time() + grace_sec
    while True:
        for pid in recorded_pids:
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        if not _any_group_alive(pgids) or time.time() >= deadline:
            return
        time.sleep(0.05)


def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
