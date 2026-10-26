"""Compatibility alias: `engine.plan` now lives at `engine.domain.plan`
(docs/standards/engine-layout.md)."""
import sys

from engine.domain import plan as _module

sys.modules[__name__] = _module
