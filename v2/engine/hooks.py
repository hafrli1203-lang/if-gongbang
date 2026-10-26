"""Compatibility alias: `engine.hooks` now lives at `engine.infrastructure.policy_hooks`
(docs/standards/engine-layout.md)."""
import sys

from engine.infrastructure import policy_hooks as _module

sys.modules[__name__] = _module
