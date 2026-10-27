"""Fresh minimal $HOME for a sandboxed CLI worker (login files always mirrored
from the real HOME). Split out of engine.infrastructure.sandbox to keep
that module under the 800-line budget; sandbox re-exports prepare_worker_home."""
from __future__ import annotations

import filecmp
import os
import shutil
from pathlib import Path


def prepare_worker_home(dest: Path, real_home: Path | None,
                         login_files: tuple[str, ...],
                         writable_subpaths: tuple[str, ...]) -> None:
    """Build (or complete) a fresh, minimal $HOME at `dest` for a
    sandboxed CLI worker/reviewer process -- containing ONLY the real
    CLI's own login/credentials files, never the rest of the real,
    operator HOME this engine process itself runs under P1
    fallback: the real HOME used to be mounted read-only wholesale --
    see engine.application.steps.worker_step/review's own `_sandboxed_argv` docstrings
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
    as needed. Already-idempotent: a destination path that already exists AND is
    non-empty (an earlier call's completed copy, or session state the
    CLI itself wrote into THIS worker's own, already-isolated home on a
    previous attempt/retry of the same request) is left untouched --
    this runs on every spawn, including a reconciled retry, and must
    never clobber state the sandboxed CLI already wrote.

    An existing but EMPTY (0-byte) destination file is re-copied rather
    than left alone (결함 55,: a worker killed (SIGTERM, by
    `gb2 watch --resume` or otherwise) while THIS function's own copy
    was in flight can leave exactly that shape -- a plain shutil.copy2
    straight to the destination path truncates it open before writing
    any bytes, so a kill in that window leaves a 0-byte file that
    `target.exists()` alone cannot tell apart from a real, completed
    copy. A real login file (`.credentials.json`/`.claude.json`) is
    never legitimately 0 bytes, so treating "exists and empty" as
    "never actually finished" and healing it cannot clobber real
    CLI-written session state -- only a copy that never landed. The
    copy itself is also made atomic (write to a sibling temp file,
    then os.replace into place) so a kill DURING a retry's own healing
    copy leaves either the still-empty old file or the complete new
    one -- never a second truncated file a third attempt would have to
    reason about the same way.

    An existing, NON-empty destination login file is ALSO re-copied,
    unconditionally, whenever its bytes differ at all from the host's OWN
    copy (`real_home / rel`) -- never decided by mtime (결함 59b,
   /28, superseding 결함 59's own mtime rule below: build
    attempt #36 STILL failed "OAuth session expired and could not be
    refreshed" after 결함 59's fix, because that fix only re-copied when
    the host's copy was STRICTLY NEWER; live stat showed the opposite --
    the WORKER's own login copy was the newer one. Reproduced on a real
    host: a sandboxed `claude` CLI that fails to silently refresh its own
    token in place still RE-WRITES the credentials file before reporting
    the auth failure, bumping its mtime past the host's -- so a login
    file that is ever corrupted this way can never recover under an
    mtime rule, by construction: a failed in-sandbox write is always
    newer than the host copy that was there when that spawn started, and
    every later spawn of the SAME worker HOME -- station._run_spawn_worker's
    resume path keeps one -- then finds the worker "newer" forever and
    never heals it. 결함 59's own reasoning for trusting mtime --
    protecting a worker's own SUCCESSFUL in-place refresh from being
    clobbered back to a stale host copy -- turned out to rest on an
    assumption mtime alone cannot tell apart: a successful refresh and a
    failed one both bump mtime the same way, only a successful one
    produces bytes a worker could usefully keep. Lacking any way to
    validate a token's content here, the simpler and now-decided rule is
    comparing CONTENT instead of time: this function's whole job is
    keeping every worker's login current with the host's, so the host's
    copy always wins once it differs at all, regardless of which side
    changed more recently). This is still safe against clobbering real
    CLI-written session state kept in a DIFFERENT, non-login file under
    the same `writable_subpaths` directory (e.g.
    `.claude/session-state.json`) -- this loop only ever touches paths
    literally named in `login_files`, never the rest of a worker's own
    HOME. A target whose bytes already MATCH the host's copy exactly
    (`filecmp.cmp(..., shallow=False)`, a real content comparison, never
    the size+mtime-only "shallow" shortcut, which could not tell a
    content change made within the same mtime-resolution window from no
    change at all) is left untouched -- a no-op optimization, not a
    staleness judgment, so a call that finds nothing actually changed
    does not pay an unnecessary copy on every single spawn. The copy
    itself is also made atomic (write to a sibling temp file, then
    os.replace into place) so a kill DURING a refresh copy leaves either
    the still-old target untouched or the complete new one -- never a
    half-written target.

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
            source = real_home / rel
            if not source.is_file():
                continue
            if target.exists():
                target_size = target.stat().st_size
                # target.exists() alone (the previous check here) cannot
                # distinguish a completed copy from a 0-byte file an
                # earlier, killed attempt's own copy left mid-write
                # (결함 55) -- a real login file is never legitimately
                # empty, so only a non-empty existing file counts as
                # "already seeded" at all.
                if target_size > 0:
                    # Still seeded -- but NEVER trusted by mtime alone
                    # (결함 59b): only an exact content match with the
                    # host's own copy counts as "nothing to do here".
                    if filecmp.cmp(source, target, shallow=False):
                        continue
            target.parent.mkdir(parents=True, exist_ok=True)
            # Copy to a sibling temp file first, then rename into place
            # (os.replace is atomic on the same filesystem): a kill
            # mid-copy now leaves either the old target untouched or
            # the temp file orphaned, never a half-written target --
            # the exact shape this function's own healing check above
            # has to tell apart from a real completed copy.
            tmp_target = target.with_name(target.name + f".tmp-{os.getpid()}")
            try:
                shutil.copy2(source, tmp_target)
                os.replace(tmp_target, target)
            finally:
                tmp_target.unlink(missing_ok=True)
    for rel in writable_subpaths:
        if Path(rel).suffix:
            continue
        (dest / rel).mkdir(parents=True, exist_ok=True)


__all__ = ["prepare_worker_home"]
