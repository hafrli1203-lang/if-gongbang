"""Compatibility alias: `engine.acceptance_scene` now lives at `engine.application.acceptance_scene`
(docs/standards/engine-layout.md)."""
import sys

from engine.application import acceptance_scene as _module

sys.modules[__name__] = _module
