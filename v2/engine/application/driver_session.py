"""A live-driver marker for `gb2 watch`, 반장 관찰 "watch 오판정").

Background: engine.application.watch.driver_alive used to know a driver
(`gb2 run/change/continue/fix`, or watch's own --resume) only through the
project's PROCESSOR lock -- which Processor.run_once holds per request and
releases between them. Every gap in which the driver was alive but not
mid-request (composing the next station's prompt, between stations,
right after job.open_intent and before the first request) read as
"nothing alive" -> "stalled"/"failed" (live jobs, 10+ misjudgments
a day), and `gb2 watch --loop --resume` could step into that gap.

`driver_session` records the driver's own pid for the whole command
under a SEPARATE lock key (`driver:{project}` -- the same locks table
and the same pid-liveness reclaim Ledger.acquire_lock already does, so a
crashed driver's row is reclaimed by the next one and never pins the
project). watch.driver_alive reads it alongside the processor lock.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger

DRIVER_HOLDER = "driver"


def driver_lock_key(project: str) -> str:
    return f"driver:{project}"


@contextmanager
def driver_session(ledger: "Ledger", project: str) -> Iterator[bool]:
    """Marks this process as `project`'s live driver for the block's
    duration. Yields True when the marker was taken, False when another
    LIVE driver already holds it (the block still runs -- this is a
    marker for watch, never a mutual-exclusion gate; the processor lock
    is that)."""
    key = driver_lock_key(project)
    acquired = ledger.acquire_lock(key, DRIVER_HOLDER)
    try:
        yield acquired
    finally:
        if acquired:
            ledger.release_lock(key, DRIVER_HOLDER)


__all__ = ["DRIVER_HOLDER", "driver_lock_key", "driver_session"]
