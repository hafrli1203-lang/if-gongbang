"""Compatibility alias: `engine.app_security` now lives at `engine.application.app_security`
(docs/standards/engine-layout.md)."""
import sys

from engine.application import app_security as _module

sys.modules[__name__] = _module
