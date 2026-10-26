"""Compatibility alias: `engine.processor` now lives at `engine.application.processor`
(docs/standards/engine-layout.md)."""
import sys

from engine.application import processor as _module

sys.modules[__name__] = _module
