"""Compatibility alias: `engine.skills` now lives at `engine.infrastructure.skills`
(docs/standards/engine-layout.md)."""
import sys

from engine.infrastructure import skills as _module

sys.modules[__name__] = _module
