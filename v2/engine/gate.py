"""Compatibility alias: `engine.gate` now lives at `engine.interface.hook_gate`
(docs/standards/engine-layout.md)."""
import sys

from engine.interface import hook_gate as _module

if __name__ == "__main__":
    sys.exit(_module.main())
else:
    sys.modules[__name__] = _module
