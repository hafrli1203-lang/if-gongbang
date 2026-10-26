"""Standalone script invoked by engine.infrastructure.procs.spawn's unsandboxed `sh`
wrapper to record a worker's real exit code into an engine-only file,
refusing to follow a symlink or clobber an existing file at that path.

Deliberately has NO `from engine import ...` of its own: this script
runs via its absolute path (never `python -m engine...`), under
whatever minimal environment the `sh` wrapper itself was launched with
(no PYTHONPATH guaranteed -- see engine.infrastructure.sandbox.launcher_env for the
bwrap case), so it must be runnable with nothing beyond the standard
library.

O_EXCL|O_NOFOLLOW together mean: a symlink already sitting at this path
(planted by anything with write access to its parent directory before
the real command exited) is refused outright rather than written
through, and a regular file already sitting there (e.g. a forged exit
code planted ahead of time, trying to make a later crash read back as
a false success) is refused rather than silently accepted or
overwritten. engine.infrastructure.procs.spawn already unlinks any stale file at this
path itself, from the trusted engine process, strictly before the real
command is even started -- so the only way this open can still find
something there is exactly the attack this script exists to refuse."""
from __future__ import annotations

import os
import sys


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        return 2
    path, code = argv[1], argv[2]
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError:
        return 1
    try:
        os.write(fd, code.encode("ascii", errors="replace"))
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
