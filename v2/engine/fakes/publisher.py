"""Compatibility alias: `engine.fakes.publisher` now lives at `engine.infrastructure.fake_publisher`
(docs/standards/engine-layout.md)."""
import sys

from engine.infrastructure import fake_publisher as _module

sys.modules[__name__] = _module
