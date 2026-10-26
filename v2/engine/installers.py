"""Compatibility alias: `engine.installers` now lives at `engine.infrastructure.installers`
(docs/standards/engine-layout.md)."""
import sys

from engine.infrastructure import installers as _module

sys.modules[__name__] = _module
