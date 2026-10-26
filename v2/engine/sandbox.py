"""Compatibility alias: `engine.sandbox` now lives at `engine.infrastructure.sandbox`
(docs/standards/engine-layout.md)."""
import sys

from engine.infrastructure import sandbox as _module

sys.modules[__name__] = _module
