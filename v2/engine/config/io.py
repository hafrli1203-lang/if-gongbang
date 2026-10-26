"""Load/save EngineConfig as JSON: the glue between the field tables in
engine.config.fields and the dataclasses in engine.config.model. Walking
the SAME table (ENGINE_FIELDS) to both parse and serialize means a
field present in the written JSON is, structurally, always one
from_json() can also read back (and vice versa)."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from engine.config.fields import ENGINE_FIELDS, KNOWN_TOP_LEVEL_KEYS, OMIT
from engine.config.model import EngineConfig


def _load_engine_fields(raw: dict[str, object], base: Path) -> dict[str, object]:
    unknown = set(raw) - KNOWN_TOP_LEVEL_KEYS
    if unknown:
        raise ValueError(f"unknown config key(s): {sorted(unknown)}")
    fields: dict[str, object] = {}
    for spec in ENGINE_FIELDS:
        if spec.key in raw:
            fields.update(spec.parse(raw[spec.key], base))
        elif spec.default is not None:
            fields.update(spec.default(base))
        else:
            fields.update(spec.parse(None, base))
    return fields


def _from_dict(raw: dict[str, object], base: Path) -> EngineConfig:
    return EngineConfig(**_load_engine_fields(raw, base))


def load_engine_config(path: Path) -> EngineConfig:
    """Load config from a JSON file. Raises ValueError on bad shape.
    EngineConfig.from_json is a thin wrapper around this (see that
    method's own docstring for why the call is lazy/indirect)."""
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("config root must be an object")
    config = _from_dict(raw, path.parent)
    return replace(config, config_path=path.resolve())


def to_json_dict(config: EngineConfig) -> dict[str, object]:
    """The JSON shape from_json() reads, with absolute paths (joining an
    absolute path onto the file's directory yields that absolute path),
    so a config built in Python can be written to disk for a spawned
    worker's GB_CONFIG and loaded back identically (minus config_path).
    Walks ENGINE_FIELDS -- the same table _load_engine_fields() walks to
    parse -- so a field present here is, structurally, always one that
    from_json() can also read back (and vice versa)."""
    out: dict[str, object] = {}
    for spec in ENGINE_FIELDS:
        value = spec.serialize(config)
        if value is not OMIT:
            out[spec.key] = value
    return out


def write_json(config: EngineConfig, path: Path) -> EngineConfig:
    """Write to_json_dict(config) at `path` and load it back, so the
    returned config carries config_path (needed by gated workers)."""
    path.write_text(json.dumps(to_json_dict(config), ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return EngineConfig.from_json(path)


__all__ = ["load_engine_config", "to_json_dict", "write_json"]
