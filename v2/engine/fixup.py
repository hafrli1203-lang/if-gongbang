"""Compatibility alias: `engine.fixup` now lives at `engine.application.repair`
(docs/standards/engine-layout.md)."""
import sys

from engine.application import repair as _module

sys.modules[__name__] = _module
