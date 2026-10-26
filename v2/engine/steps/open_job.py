"""Compatibility alias: `engine.steps.open_job` now lives at `engine.application.steps.open_job`
(docs/standards/engine-layout.md)."""
import sys

from engine.application.steps import open_job as _module

sys.modules[__name__] = _module
