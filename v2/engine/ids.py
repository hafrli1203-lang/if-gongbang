"""Compatibility alias: `engine.ids` now lives at `engine.domain.ids`
(docs/standards/engine-layout.md)."""
import sys

from engine.domain import ids as _module

sys.modules[__name__] = _module
