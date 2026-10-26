"""Compatibility alias: `engine.exit_writer` now lives at `engine.infrastructure.exit_writer`
(docs/standards/engine-layout.md)."""
import sys

from engine.infrastructure import exit_writer as _module

if __name__ == "__main__":
    sys.exit(_module.main(sys.argv))
else:
    sys.modules[__name__] = _module
