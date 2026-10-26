"""Compatibility alias: `engine.bootstrap` now lives at `engine.infrastructure.bootstrap`
(docs/standards/engine-layout.md)."""
import sys

from engine.infrastructure import bootstrap as _module

sys.modules[__name__] = _module
