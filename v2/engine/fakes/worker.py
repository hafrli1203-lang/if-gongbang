"""Compatibility alias: `engine.fakes.worker` now lives at `tests.fakes.worker`
(docs/standards/engine-layout.md)."""
import sys

from tests.fakes import worker as _module

sys.modules[__name__] = _module
