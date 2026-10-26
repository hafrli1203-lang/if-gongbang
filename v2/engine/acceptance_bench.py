"""Compatibility alias: `engine.acceptance_bench` now lives at `engine.application.acceptance_bench`
(docs/standards/engine-layout.md)."""
import sys

from engine.application import acceptance_bench as _module

sys.modules[__name__] = _module
