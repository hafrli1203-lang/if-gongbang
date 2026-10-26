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


# Flow-layer boundaries (engine.application.pipeline, engine.application.repair, engine.application.quick_pipeline)
# are not processor steps, so they cannot use Faults.crash_before/after.
# Each named flow boundary calls flow_boundary(name) right before the
# ledger write that ends it; a test arms ACTIVE_FLOW_FAULTS to crash there.
ACTIVE_FLOW_FAULTS: Faults | None = None


def flow_boundary(name: str) -> None:
    """Raises CrashInjected once when a test armed `name` in
    ACTIVE_FLOW_FAULTS.crash_before; a no-op otherwise."""
    if ACTIVE_FLOW_FAULTS is not None:
        ACTIVE_FLOW_FAULTS.maybe_crash_before(name)
