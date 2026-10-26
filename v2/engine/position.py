"""Compatibility alias: `engine.position` now lives at `engine.application.job_state`
(docs/standards/engine-layout.md)."""
import sys

from engine.application import job_state as _module

sys.modules[__name__] = _module
