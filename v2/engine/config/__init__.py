"""Engine configuration. Nothing project-specific is hardcoded here.

All project names, roles, stations, allowed paths and protected paths
come from an EngineConfig built by the caller (tests build one from
fixtures).

This module is the stable import path (`from engine.config import
...`) for three pieces that now live in their own files:
  - engine.config.model: the dataclasses (EngineConfig, ProjectConfig,
    ReleaseConfig, WorkerConfig, McpServerConfig) and their own
    __post_init__ validation.
  - engine.config.fields: the JSON field tables (ENGINE_FIELDS,
    PROJECT_FIELDS, CONTEXT_FIELDS) -- one row per key, parse/default/
    serialize together, so a field that is only ever parsed (and never
    serialized), or vice versa, cannot exist.
  - engine.config.io: from_json/to_json_dict/write_json, the glue
    between the two above.
Everything below is re-exported here unchanged so no existing caller
of `engine.config` needs to change."""
from __future__ import annotations

from engine.config.fields import (
    CONTEXT_FIELDS, ContextFieldSpec, ENGINE_FIELDS, FieldSpec, OMIT,
    PROJECT_FIELDS, ProjectFieldSpec,
)
from engine.config.io import to_json_dict, write_json
from engine.config.model import (
    EngineConfig, McpServerConfig, ProjectConfig, ReleaseConfig, WorkerConfig,
    realpath_overlaps,
)
from engine.config.context_settings import ContextConfig, LibraryMcpConfig

# Back-compat spelling: this module used to define the "leave this key
# out" sentinel itself as `_OMIT`, before engine.config.fields owned it
# as the public `OMIT`. tests/unit/test_config_roundtrip.py still reads
# it off this module by that exact name.
_OMIT = OMIT

__all__ = ["EngineConfig", "ProjectConfig", "ReleaseConfig", "WorkerConfig",
           "McpServerConfig", "to_json_dict", "write_json", "realpath_overlaps"]
