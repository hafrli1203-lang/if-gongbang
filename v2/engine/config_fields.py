"""Compatibility alias: `engine.config_fields` now lives at `engine.config.fields`
(docs/standards/engine-layout.md)."""
import sys

from engine.config import fields as _module

sys.modules[__name__] = _module
