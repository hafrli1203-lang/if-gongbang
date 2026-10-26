"""Compatibility alias: `engine.release_certificate` now lives at `engine.application.release_certificate`
(docs/standards/engine-layout.md)."""
import sys

from engine.application import release_certificate as _module

sys.modules[__name__] = _module
