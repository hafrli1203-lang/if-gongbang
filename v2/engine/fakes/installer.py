"""Compatibility alias: `engine.fakes.installer` now lives at `tests.fakes.installer`
(docs/standards/engine-layout.md)."""
import sys

from tests.fakes import installer as _module

sys.modules[__name__] = _module
