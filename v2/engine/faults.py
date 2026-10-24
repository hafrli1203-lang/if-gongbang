"""Crash injection for acceptance tests.

Two boundaries per step:
  crash_before: after the step is journaled 'started', before run()
  crash_after:  after run(), before the step is journaled 'done'
"""
from __future__ import annotations

from dataclasses import dataclass, field


class CrashInjected(Exception):
    """Raised by the processor at an injected boundary."""


@dataclass
class Faults:
    crash_after: set[str] = field(default_factory=set)  # step names
    crash_before: set[str] = field(default_factory=set)  # step names
    fired: list[str] = field(default_factory=list)

    def maybe_crash(self, step_name: str) -> None:
        """Boundary: after run(), before 'done'."""
        if step_name in self.crash_after:
            self.crash_after.discard(step_name)
            self.fired.append(f"after:{step_name}")
            raise CrashInjected(step_name)

    def maybe_crash_before(self, step_name: str) -> None:
        """Boundary: after 'started', before run()."""
        if step_name in self.crash_before:
            self.crash_before.discard(step_name)
            self.fired.append(f"before:{step_name}")
            raise CrashInjected(step_name)
