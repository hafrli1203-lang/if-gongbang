"""Compatibility alias: `engine.browser_check` now lives at `engine.infrastructure.browser_check`
(docs/standards/engine-layout.md)."""
import sys

from engine.infrastructure import browser_check as _module

sys.modules[__name__] = _module
