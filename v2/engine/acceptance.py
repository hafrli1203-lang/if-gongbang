"""Compatibility alias: `engine.acceptance` now lives at `engine.application.acceptance`
(docs/standards/engine-layout.md)."""
import sys

from engine.application import acceptance as _module

sys.modules[__name__] = _module
