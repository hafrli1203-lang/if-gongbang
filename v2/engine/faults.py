"""Compatibility alias: `engine.faults` now lives at `engine.domain.faults`
(docs/standards/engine-layout.md)."""
import sys

from engine.domain import faults as _module

sys.modules[__name__] = _module
