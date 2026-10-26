"""Compatibility alias: `engine.config_model` now lives at `engine.config.model`
(docs/standards/engine-layout.md)."""
import sys

from engine.config import model as _module

sys.modules[__name__] = _module
