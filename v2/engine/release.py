"""Compatibility alias: `engine.release` now lives at `engine.application.release`
(docs/standards/engine-layout.md)."""
import sys

from engine.application import release as _module

sys.modules[__name__] = _module
