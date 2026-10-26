"""Compatibility alias: `engine.config_io` now lives at `engine.config.io`
(docs/standards/engine-layout.md)."""
import sys

from engine.config import io as _module

sys.modules[__name__] = _module
