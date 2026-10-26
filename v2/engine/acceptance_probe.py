"""Compatibility alias: `engine.acceptance_probe` now lives at `engine.application.acceptance_probe`
(docs/standards/engine-layout.md)."""
import sys

from engine.application import acceptance_probe as _module

sys.modules[__name__] = _module
