"""Compatibility alias: `engine.station_prompts` now lives at `engine.application.stage_prompts`
(docs/standards/engine-layout.md)."""
import sys

from engine.application import stage_prompts as _module

sys.modules[__name__] = _module
