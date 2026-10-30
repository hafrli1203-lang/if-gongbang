"""Every JSON key EngineConfig/ProjectConfig/ContextConfig understands,
declared exactly once as one row in ENGINE_FIELDS/PROJECT_FIELDS/
CONTEXT_FIELDS below. Each row carries its own parse, default and
serialize together (see FieldSpec/ProjectFieldSpec/ContextFieldSpec) --
the "known keys" set used to reject typos, the JSON loader
(engine.config.io) and to_json_dict's writer all walk the SAME table,
so a field that is only ever parsed (and never serialized), or vice
versa, cannot exist: there is no second place to add one half and
forget the other. (This replaced a version where EngineConfig.context's
required_skills/require_library were added to the dataclass and to
from_json's parsing but not to to_json_dict, so no JSON config could
actually set them and write_json silently dropped them -- see
tests/unit/test_config.py.)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from engine.config.model import (
    EngineConfig, McpServerConfig, ProjectConfig, ReleaseConfig, WorkerConfig,
    is_known_runner as _is_known_runner, KNOWN_PUBLISHER_KINDS as _KNOWN_PUBLISHER_KINDS,
    KNOWN_SANDBOX_MODES as _KNOWN_SANDBOX_MODES,
)
from engine.config.context_settings import ContextConfig, LibraryMcpConfig

OMIT = object()  # serialize() sentinel: to_json_dict leaves this key out entirely


def _str_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("expected a list of strings")
    return list(value)


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("expected a string or null")
    return value


# ---------------------------------------------------------------------------
# EngineConfig top-level fields: one row per JSON key. `parse(value, base)`
# returns the {dataclass_field_name: value, ...} overrides that key
# contributes (usually one, "publisher" contributes five); `default(base)`
# supplies the same shape when the key is absent (None means "required",
# and the loader calls parse(None, base) so the field's own type check
# produces the error, instead of duplicating a second "missing" message
# here); `serialize(config)` reads whatever fields it needs off an
# EngineConfig and returns this key's JSON value, or OMIT to leave the
# key out of the written file entirely (used by skill_index_path when
# unset, and always by "security" -- see its row below).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FieldSpec:
    key: str
    parse: Callable[[object, Path], dict[str, object]]
    default: Callable[[Path], dict[str, object]] | None
    serialize: Callable[["EngineConfig"], object]


def _validated_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    return float(value)


def _validated_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _validated_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _validated_choice(value: object, choices: frozenset[str], label: str) -> str:
    if value not in choices:
        raise ValueError(f"unknown {label}: {value!r}")
    return value


def _parse_projects_value(value: object, base: Path) -> dict[str, "ProjectConfig"]:
    if not isinstance(value, dict):
        raise ValueError("projects must be an object")
    projects: dict[str, ProjectConfig] = {}
    for name, item in value.items():
        if not isinstance(item, dict) or not isinstance(item.get("repo"), str):
            raise ValueError(f"project {name!r} needs a repo path")
        fields = _load_project_fields(item, base, name)
        projects[name] = ProjectConfig(name=name, **fields)
    return projects


def _parse_station_owners(value: object) -> dict[str, dict[str, tuple[str, ...]]]:
    if not isinstance(value, dict):
        raise ValueError("station_owners must be an object")
    owners: dict[str, dict[str, tuple[str, ...]]] = {}
    for station, roles in value.items():
        if not isinstance(roles, dict):
            raise ValueError(f"station {station!r} roles must be an object")
        owners[station] = {r: tuple(_str_list(p)) for r, p in roles.items()}
    return owners


def _parse_workers(value: object) -> dict[str, WorkerConfig]:
    if not isinstance(value, dict):
        raise ValueError("workers must be an object")
    workers: dict[str, WorkerConfig] = {}
    for role, item in value.items():
        if not isinstance(item, dict):
            raise ValueError(f"worker {role!r} must be an object")
        runner = item.get("runner")
        if not _is_known_runner(runner):
            raise ValueError(f"worker {role!r}: unknown runner {runner!r}")
        workers[role] = WorkerConfig(
            runner=str(runner),
            model=_opt_str(item.get("model")),
            max_turns=(None if item.get("max_turns") is None else int(item["max_turns"])),
            budget_usd=(None if item.get("budget_usd") is None else float(item["budget_usd"])),
            timeout_sec=float(item.get("timeout_sec", 900.0)),
            reasoning=_opt_str(item.get("reasoning")),
        )
    return workers


def _worker_to_json(w: WorkerConfig) -> dict[str, object]:
    return {"runner": w.runner, "model": w.model, "max_turns": w.max_turns,
            "budget_usd": w.budget_usd, "timeout_sec": w.timeout_sec,
            "reasoning": w.reasoning}


def _parse_station_terminal_allow(value: object) -> dict[str, dict[str, tuple[str, ...]]]:
    if not isinstance(value, dict):
        raise ValueError("station_terminal_allow must be an object")
    table: dict[str, dict[str, tuple[str, ...]]] = {}
    for station, roles in value.items():
        if not isinstance(roles, dict):
            raise ValueError(f"station_terminal_allow[{station!r}] must be an object")
        table[station] = {r: tuple(_str_list(p)) for r, p in roles.items()}
    return table


_KNOWN_MCP_SERVER_KEYS = frozenset({"command", "args", "env_passthrough"})


def _parse_mcp_servers(value: object) -> dict[str, McpServerConfig]:
    if not isinstance(value, dict):
        raise ValueError("mcp_servers must be an object")
    servers: dict[str, McpServerConfig] = {}
    for name, item in value.items():
        if not isinstance(item, dict):
            raise ValueError(f"mcp_servers[{name!r}] must be an object")
        unknown = set(item) - _KNOWN_MCP_SERVER_KEYS
        if unknown:
            raise ValueError(f"mcp_servers[{name!r}]: unknown key(s): {sorted(unknown)}")
        command = tuple(_str_list(item.get("command")))
        if not command:
            raise ValueError(f"mcp_servers[{name!r}]: command must be a non-empty list")
        servers[str(name)] = McpServerConfig(
            command=command,
            args=tuple(_str_list(item.get("args"))),
            env_passthrough=tuple(_str_list(item.get("env_passthrough"))),
        )
    return servers


def _mcp_server_to_json(s: McpServerConfig) -> dict[str, object]:
    return {"command": list(s.command), "args": list(s.args),
            "env_passthrough": list(s.env_passthrough)}


def _parse_station_mcp_allow(value: object) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise ValueError("station_mcp_allow must be an object")
    return {str(station): tuple(_str_list(names)) for station, names in value.items()}


def _parse_mcp_tool_allow(value: object) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise ValueError("mcp_tool_allow must be an object")
    return {str(server): tuple(_str_list(names)) for server, names in value.items()}


def _parse_bench_readonly_paths(value: object) -> tuple[Path, ...]:
    entries = _str_list(value)
    paths: list[Path] = []
    for entry in entries:
        if not (entry.startswith("~") or entry.startswith("/")):
            raise ValueError(
                f"bench_readonly_paths entry must be absolute or ~/...: {entry!r}"
            )
        # expanduser reads THIS (engine) process's own HOME -- never
        # a worker's or bench case's env, which is the whole point
        # of this setting.
        paths.append(Path(os.path.expanduser(entry)).resolve())
    return tuple(paths)


_KNOWN_PUBLISHER_KEYS = frozenset({
    "kind", "argv", "dry_run_argv", "timeout_sec", "dry_run",
})


def _parse_publisher(value: object, base: Path) -> dict[str, object]:
    del base  # publisher argv/paths are not resolved against the config file
    if not isinstance(value, dict):
        raise ValueError("publisher must be an object")
    unknown = set(value) - _KNOWN_PUBLISHER_KEYS
    if unknown:
        raise ValueError(f"publisher: unknown key(s): {sorted(unknown)}")
    fields: dict[str, object] = {}
    if "kind" in value:
        fields["publisher_kind"] = _validated_choice(
            value["kind"], _KNOWN_PUBLISHER_KINDS, "publisher kind")
    if "argv" in value:
        fields["publisher_argv"] = tuple(_str_list(value.get("argv")))
    if "dry_run_argv" in value:
        fields["publisher_dry_run_argv"] = tuple(_str_list(value.get("dry_run_argv")))
    if "timeout_sec" in value:
        fields["publisher_timeout_sec"] = _validated_number(
            value["timeout_sec"], "publisher.timeout_sec")
    if "dry_run" in value:
        fields["publisher_dry_run"] = _validated_bool(value["dry_run"], "publisher.dry_run")
    return fields


def _publisher_to_json(cfg: "EngineConfig") -> dict[str, object]:
    return {
        "kind": cfg.publisher_kind, "argv": list(cfg.publisher_argv),
        "dry_run_argv": list(cfg.publisher_dry_run_argv),
        "timeout_sec": cfg.publisher_timeout_sec,
        "dry_run": cfg.publisher_dry_run,
    }


def _parse_skill_index_path(value: object, base: Path) -> dict[str, object]:
    del base
    if not isinstance(value, str) or not value:
        raise ValueError("skill_index_path must be a non-empty string")
    # expanduser reads THIS (engine) process's own HOME -- never a
    # worker's or planner's env, same rule as bench_readonly_paths.
    return {"skill_index_path": Path(os.path.expanduser(value)).resolve()}


ENGINE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("ledger_path",
              lambda v, base: {"ledger_path": (base / str(v)).resolve()},
              lambda base: {"ledger_path": (base / "ledger.sqlite").resolve()},
              lambda cfg: str(cfg.ledger_path)),
    FieldSpec("home",
              lambda v, base: {"home": (base / str(v)).resolve()},
              lambda base: {"home": (base / "home").resolve()},
              lambda cfg: str(cfg.home)),
    FieldSpec("secrets_dir",
              lambda v, base: {"secrets_dir": (base / str(v)).resolve()},
              lambda base: {"secrets_dir": (base / "secrets").resolve()},
              lambda cfg: str(cfg.secrets_dir)),
    FieldSpec("projects",
              lambda v, base: {"projects": _parse_projects_value(v, base)},
              None,  # required: parse(None, base) raises "projects must be an object"
              lambda cfg: {name: _project_to_json(p) for name, p in cfg.projects.items()}),
    FieldSpec("station_owners",
              lambda v, base: {"station_owners": _parse_station_owners(v)},
              lambda base: {"station_owners": {}},
              lambda cfg: {s: {r: list(p) for r, p in roles.items()}
                           for s, roles in cfg.station_owners.items()}),
    FieldSpec("protected_paths",
              lambda v, base: {"protected_paths": tuple(_str_list(v))},
              lambda base: {"protected_paths": ()},
              lambda cfg: list(cfg.protected_paths)),
    FieldSpec("hooks",
              lambda v, base: {"hooks": tuple((base / str(p)).resolve() for p in _str_list(v))},
              lambda base: {},
              lambda cfg: [str(h) for h in cfg.hooks]),
    FieldSpec("hook_timeout_sec",
              lambda v, base: {"hook_timeout_sec": _validated_number(v, "hook_timeout_sec")},
              lambda base: {},
              lambda cfg: cfg.hook_timeout_sec),
    FieldSpec("approval_ttl_sec",
              lambda v, base: {"approval_ttl_sec": _validated_int(v, "approval_ttl_sec")},
              lambda base: {},
              lambda cfg: cfg.approval_ttl_sec),
    FieldSpec("watch_max_resumes_per_hour",
              lambda v, base: {"watch_max_resumes_per_hour": _validated_int(
                  v, "watch_max_resumes_per_hour")},
              lambda base: {},
              lambda cfg: cfg.watch_max_resumes_per_hour),
    FieldSpec("stations",
              lambda v, base: {"stations": tuple(_str_list(v))},
              lambda base: {},
              lambda cfg: list(cfg.stations)),
    FieldSpec("optional_stations",
              lambda v, base: {"optional_stations": tuple(_str_list(v))},
              lambda base: {},
              lambda cfg: list(cfg.optional_stations)),
    FieldSpec("workers",
              lambda v, base: {"workers": _parse_workers(v)},
              lambda base: {},
              lambda cfg: {role: _worker_to_json(w) for role, w in cfg.workers.items()}),
    FieldSpec("station_terminal_allow",
              lambda v, base: {"station_terminal_allow": _parse_station_terminal_allow(v)},
              lambda base: {},
              lambda cfg: {s: {r: list(p) for r, p in roles.items()}
                           for s, roles in cfg.station_terminal_allow.items()}),
    FieldSpec("worker_env_passthrough",
              lambda v, base: {"worker_env_passthrough": tuple(_str_list(v))},
              lambda base: {},
              lambda cfg: list(cfg.worker_env_passthrough)),
    FieldSpec("sandbox",
              lambda v, base: {"sandbox": _validated_choice(
                  v, _KNOWN_SANDBOX_MODES, "sandbox mode")},
              lambda base: {},
              lambda cfg: cfg.sandbox),
    FieldSpec("bench_home_writable",
              lambda v, base: {"bench_home_writable": _validated_bool(
                  v, "bench_home_writable")},
              lambda base: {},
              lambda cfg: cfg.bench_home_writable),
    FieldSpec("bench_readonly_paths",
              lambda v, base: {"bench_readonly_paths": _parse_bench_readonly_paths(v)},
              lambda base: {},
              lambda cfg: [str(p) for p in cfg.bench_readonly_paths]),
    FieldSpec("publisher",
              _parse_publisher,
              lambda base: {},
              _publisher_to_json),
    FieldSpec("parallel_review_bench",
              lambda v, base: {"parallel_review_bench": _validated_bool(
                  v, "parallel_review_bench")},
              lambda base: {},
              lambda cfg: cfg.parallel_review_bench),
    FieldSpec("mcp_servers",
              lambda v, base: {"mcp_servers": _parse_mcp_servers(v)},
              lambda base: {},
              lambda cfg: {name: _mcp_server_to_json(s) for name, s in cfg.mcp_servers.items()}),
    FieldSpec("station_mcp_allow",
              lambda v, base: {"station_mcp_allow": _parse_station_mcp_allow(v)},
              lambda base: {},
              lambda cfg: {s: list(v) for s, v in cfg.station_mcp_allow.items()}),
    FieldSpec("mcp_tool_allow",
              lambda v, base: {"mcp_tool_allow": _parse_mcp_tool_allow(v)},
              lambda base: {},
              lambda cfg: {s: list(v) for s, v in cfg.mcp_tool_allow.items()}),
    FieldSpec("skill_index_path",
              _parse_skill_index_path,
              lambda base: {},
              lambda cfg: (str(cfg.skill_index_path)
                           if cfg.skill_index_path is not None else OMIT)),
    FieldSpec("context",
              lambda v, base: ({} if v is None else {"context": _parse_context(v, base)}),
              lambda base: {},
              lambda cfg: _context_to_json(cfg.context)),
    # engine.application.app_security.check() reads "security" straight back out of the
    # raw JSON file at config.config_path itself (see that module's own
    # docstring for why -- it is read fresh per check() call, never cached
    # on EngineConfig). It is accepted here (so a config that sets it still
    # loads) but never parsed into a field and never written back by
    # to_json_dict (OMIT) -- round-tripping a config through write_json
    # does not preserve a "security" block; only the original file has it.
    FieldSpec("security", lambda v, base: {}, lambda base: {}, lambda cfg: OMIT),
)

KNOWN_TOP_LEVEL_KEYS = frozenset(spec.key for spec in ENGINE_FIELDS)


# ---------------------------------------------------------------------------
# ProjectConfig fields: same contract as FieldSpec above, but parse/default
# also take the owning project's `name` (for error messages) and serialize
# takes the field's own value directly (the JSON key equals the dataclass
# field name for every row here, "repo" included).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProjectFieldSpec:
    key: str
    parse: Callable[[object, Path, str], object]
    default: Callable[[Path, str], object] | None
    serialize: Callable[[object], object]


def _project_opt_str(value: object, name: str, field_name: str, *, null_ok_msg: bool = False) -> str | None:
    if value is not None and not isinstance(value, str):
        suffix = "must be a string or null" if null_ok_msg else "must be a string"
        raise ValueError(f"project {name!r}: {field_name} {suffix}")
    return value


def _project_str(value: object, name: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"project {name!r}: {field_name} must be a string")
    return value


def _project_number(value: object, name: str, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"project {name!r}: {field_name} must be a number")
    return float(value)


def _project_int(value: object, name: str, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"project {name!r}: {field_name} must be an integer")
    return value


def _project_writable_subpaths(value: object, name: str) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise ValueError(f"project {name!r}: copy_link_writable_subpaths must be an object")
    return {str(entry): tuple(_str_list(subpaths)) for entry, subpaths in value.items()}


def _project_hidden_paths(value: object, name: str) -> tuple[Path, ...]:
    entries = _str_list(value)
    for entry in entries:
        if not entry.startswith("/"):
            raise ValueError(f"project {name!r}: hidden_paths entry must be absolute: {entry!r}")
    return tuple(Path(p) for p in entries)


def _project_file_dep_targets(value: object, name: str) -> tuple[tuple[str, str], ...]:
    """FORM only, same as _project_reference_paths: no filesystem check
    at config-parse time. Each entry a 2-item [relspec, target] list;
    `target` must look absolute (a cheap string check, enforced again,
    identically, by ProjectConfig.__post_init__). The real resolve/
    validate pass (engine.application.steps.file_deps, against the
    REAL source checkout) runs once at `gb2 init` time, before this
    value is ever written to config.json -- see ProjectConfig.
    file_dep_targets' own docstring."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"project {name!r}: file_dep_targets must be a list")
    out: list[tuple[str, str]] = []
    for entry in value:
        if (not isinstance(entry, list) or len(entry) != 2
                or not all(isinstance(x, str) for x in entry)):
            raise ValueError(
                f"project {name!r}: file_dep_targets entry must be [relspec, target]: {entry!r}"
            )
        relspec, target = entry
        if not target.startswith("/"):
            raise ValueError(
                f"project {name!r}: file_dep_targets target must be absolute: {target!r}"
            )
        out.append((relspec, target))
    return tuple(out)


def _project_reference_paths(value: object, name: str) -> tuple[Path, ...]:
    """FORM only -- absolute or "~/..." -- never touches the host
    filesystem (no is_dir()/.env check here any more). Config loading
    must never fail because some OTHER project's reference_paths entry
    happens to be momentarily missing/unreadable on whatever host is
    re-parsing this file for e.g. a worker's gate hook P1:
    a hidden is_dir() check here made gate_hook re-deserializing the
    WHOLE config die, with exit code 2, denying every tool call for
    every OTHER project too, whenever any one project's reference
    folder was not reachable). The real checks -- directory-ness, no
    `.env*` anywhere in the tree, no overlap with secrets_dir/home/
    the ledger dir/any project's repo/hidden_paths/the real HOME,
    no hidden path component -- all live in engine.infrastructure.
    reference_snapshot.snapshot_references, run once per attempt
    (worker_sandbox/review, right before a worker/reviewer actually
    needs the snapshot), not at config-parse time. A ProjectConfig
    built directly in Python (bypassing this parser entirely) goes
    through that same snapshot function and gets the same checks --
    there is exactly one place these rules are enforced."""
    entries = _str_list(value)
    paths: list[Path] = []
    for entry in entries:
        if not (entry.startswith("~") or entry.startswith("/")):
            raise ValueError(
                f"project {name!r}: reference_paths entry must be absolute or ~/...: {entry!r}"
            )
        # expanduser reads THIS (engine) process's own HOME -- never a
        # worker's or bench case's env, same rule as bench_readonly_paths.
        paths.append(Path(os.path.expanduser(entry)))
    return tuple(paths)


_KNOWN_RELEASE_KEYS = frozenset({"master_repo", "target", "verify_cmd"})


def _parse_release(raw: object, base: Path, project_name: str) -> "ReleaseConfig":
    if not isinstance(raw, dict):
        raise ValueError(f"project {project_name!r}: release must be an object")
    unknown = set(raw) - _KNOWN_RELEASE_KEYS
    if unknown:
        raise ValueError(
            f"project {project_name!r}: release: unknown key(s): {sorted(unknown)}"
        )
    master_repo = raw.get("master_repo")
    target = raw.get("target")
    if not isinstance(master_repo, str) or not master_repo:
        raise ValueError(
            f"project {project_name!r}: release.master_repo must be a non-empty string"
        )
    if not isinstance(target, str) or not target:
        raise ValueError(
            f"project {project_name!r}: release.target must be a non-empty string"
        )
    verify_cmd = tuple(_str_list(raw.get("verify_cmd")))
    return ReleaseConfig(master_repo=(base / master_repo).resolve(), target=target,
                          verify_cmd=verify_cmd)


PROJECT_FIELDS: tuple[ProjectFieldSpec, ...] = (
    ProjectFieldSpec("repo",
                      lambda v, base, name: (base / str(v)).resolve(),
                      None,  # presence+type already checked by the caller
                      lambda v: str(v)),
    ProjectFieldSpec("excludes",
                      lambda v, base, name: tuple(_str_list(v)),
                      lambda base, name: (), lambda v: list(v)),
    ProjectFieldSpec("artifact_paths",
                      lambda v, base, name: tuple(_str_list(v)),
                      lambda base, name: (), lambda v: list(v)),
    ProjectFieldSpec("test_cmd",
                      lambda v, base, name: tuple(_str_list(v)),
                      lambda base, name: (), lambda v: list(v)),
    ProjectFieldSpec("install_cmd",
                      lambda v, base, name: tuple(_str_list(v)),
                      lambda base, name: (), lambda v: list(v)),
    ProjectFieldSpec("manifest",
                      lambda v, base, name: _project_opt_str(v, name, "manifest"),
                      lambda base, name: None, lambda v: v),
    ProjectFieldSpec("bench_cases",
                      lambda v, base, name: _project_opt_str(v, name, "bench_cases"),
                      lambda base, name: None, lambda v: v),
    ProjectFieldSpec("test_timeout_sec",
                      lambda v, base, name: _project_number(v, name, "test_timeout_sec"),
                      lambda base, name: 300.0, lambda v: v),
    ProjectFieldSpec("bench_scope",
                      lambda v, base, name: _project_str(v, name, "bench_scope"),
                      lambda base, name: "related", lambda v: v),
    ProjectFieldSpec("related_cmd",
                      lambda v, base, name: tuple(_str_list(v)),
                      lambda base, name: (), lambda v: list(v)),
    ProjectFieldSpec("arch_check_cmd",
                      lambda v, base, name: tuple(_str_list(v)),
                      lambda base, name: (), lambda v: list(v)),
    # Default 2, not 5: ENGINE.md's
    # 완성 원칙 is "fix then re-review, at most 2 rounds, then the foreman"
    # and JobPosition's own default is 2 -- the CLI passed this 5 through.
    ProjectFieldSpec("max_fix_rounds",
                      lambda v, base, name: _project_int(v, name, "max_fix_rounds"),
                      lambda base, name: 2, lambda v: v),
    ProjectFieldSpec("builder_test_hint",
                      lambda v, base, name: _project_opt_str(
                          v, name, "builder_test_hint", null_ok_msg=True),
                      lambda base, name: None, lambda v: v),
    ProjectFieldSpec("copy_links",
                      lambda v, base, name: tuple(_str_list(v)),
                      lambda base, name: (), lambda v: list(v)),
    ProjectFieldSpec("copy_link_writable_subpaths",
                      lambda v, base, name: _project_writable_subpaths(v, name),
                      lambda base, name: {},
                      lambda v: {e: list(s) for e, s in v.items()}),
    ProjectFieldSpec("release",
                      lambda v, base, name: (
                          None if v is None else _parse_release(v, base, name)),
                      lambda base, name: None,
                      lambda v: ({"master_repo": str(v.master_repo), "target": v.target,
                                  "verify_cmd": list(v.verify_cmd)}
                                 if v is not None else None)),
    ProjectFieldSpec("hidden_paths",
                      lambda v, base, name: _project_hidden_paths(v, name),
                      lambda base, name: (), lambda v: [str(p) for p in v]),
    ProjectFieldSpec("reference_paths",
                      lambda v, base, name: _project_reference_paths(v, name),
                      lambda base, name: (), lambda v: [str(p) for p in v]),
    ProjectFieldSpec("probe_runtime_clis",
                      lambda v, base, name: tuple(_str_list(v)),
                      lambda base, name: (), lambda v: list(v)),
    ProjectFieldSpec("file_dep_targets",
                      lambda v, base, name: _project_file_dep_targets(v, name),
                      lambda base, name: (),
                      lambda v: [list(pair) for pair in v]),
)

_KNOWN_PROJECT_KEYS = frozenset(spec.key for spec in PROJECT_FIELDS)


def _load_project_fields(item: dict[str, object], base: Path, name: str) -> dict[str, object]:
    unknown = set(item) - _KNOWN_PROJECT_KEYS
    if unknown:
        raise ValueError(f"project {name!r}: unknown key(s): {sorted(unknown)}")
    fields: dict[str, object] = {}
    for spec in PROJECT_FIELDS:
        if spec.key in item:
            fields[spec.key] = spec.parse(item[spec.key], base, name)
        elif spec.default is not None:
            fields[spec.key] = spec.default(base, name)
        else:
            fields[spec.key] = spec.parse(None, base, name)
    return fields


def _project_to_json(project: ProjectConfig) -> dict[str, object]:
    return {spec.key: spec.serialize(getattr(project, spec.key)) for spec in PROJECT_FIELDS}


# ---------------------------------------------------------------------------
# ContextConfig fields: same contract again, one row per key, no `base`/
# `name` needed (context values are taken as-given, never resolved against
# the config file's own directory).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ContextFieldSpec:
    key: str
    parse: Callable[[object], object]
    default: Callable[[], object]
    serialize: Callable[[object], object]


def _parse_station_skills(value: object, label: str) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise ValueError(f"context.{label} must be an object")
    return {str(station): tuple(_str_list(names)) for station, names in value.items()}


def _parse_str_dict(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
        raise ValueError(f"context.{label} must be an object of strings")
    return {str(k): str(v) for k, v in value.items()}


_KNOWN_LIBRARY_MCP_KEYS = frozenset({"command", "timeout_sec"})


def _parse_library_mcp(raw: object) -> "LibraryMcpConfig":
    if not isinstance(raw, dict):
        raise ValueError("context.library_mcp must be an object")
    unknown = set(raw) - _KNOWN_LIBRARY_MCP_KEYS
    if unknown:
        raise ValueError(f"context.library_mcp: unknown key(s): {sorted(unknown)}")
    command = tuple(_str_list(raw.get("command")))
    if not command:
        raise ValueError("context.library_mcp.command must be a non-empty list")
    timeout = raw.get("timeout_sec", 10.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("context.library_mcp.timeout_sec must be a number")
    return LibraryMcpConfig(command=command, timeout_sec=float(timeout))


def _library_mcp_to_json(lib: "LibraryMcpConfig") -> dict[str, object]:
    return {"command": list(lib.command), "timeout_sec": lib.timeout_sec}


CONTEXT_FIELDS: tuple[ContextFieldSpec, ...] = (
    ContextFieldSpec("skill_roots",
                      lambda v: tuple(_str_list(v)), lambda: (), lambda v: list(v)),
    ContextFieldSpec("station_skills",
                      lambda v: _parse_station_skills(v, "station_skills"), lambda: {},
                      lambda v: {s: list(n) for s, n in v.items()}),
    ContextFieldSpec("library_mcp",
                      lambda v: (_parse_library_mcp(v) if v is not None else None),
                      lambda: None,
                      lambda v: (_library_mcp_to_json(v) if v is not None else None)),
    ContextFieldSpec("decisions_paths",
                      lambda v: _parse_str_dict(v, "decisions_paths"), lambda: {},
                      lambda v: dict(v)),
    ContextFieldSpec("lessons_app",
                      lambda v: _parse_str_dict(v, "lessons_app"), lambda: {},
                      lambda v: dict(v)),
    ContextFieldSpec("direction_path",
                      lambda v: (str(v) if v else None), lambda: None, lambda v: v),
    ContextFieldSpec("required_skills",
                      lambda v: _parse_station_skills(v, "required_skills"), lambda: {},
                      lambda v: {s: list(n) for s, n in v.items()}),
    ContextFieldSpec("require_library",
                      lambda v: tuple(_str_list(v)), lambda: (), lambda v: list(v)),
)

_KNOWN_CONTEXT_KEYS = frozenset(spec.key for spec in CONTEXT_FIELDS)


def _load_context_fields(raw: dict[str, object]) -> dict[str, object]:
    unknown = set(raw) - _KNOWN_CONTEXT_KEYS
    if unknown:
        raise ValueError(f"context: unknown key(s): {sorted(unknown)}")
    fields: dict[str, object] = {}
    for spec in CONTEXT_FIELDS:
        fields[spec.key] = spec.parse(raw[spec.key]) if spec.key in raw else spec.default()
    return fields


def _parse_context(raw: object, base: Path) -> "ContextConfig":
    del base  # context fields are taken as-given, not resolved against base
    if not isinstance(raw, dict):
        raise ValueError("context must be an object")
    return ContextConfig(**_load_context_fields(raw))


def _context_to_json(context: "ContextConfig | None") -> dict[str, object] | None:
    if context is None:
        return None
    return {spec.key: spec.serialize(getattr(context, spec.key)) for spec in CONTEXT_FIELDS}


__all__ = [
    "FieldSpec", "ProjectFieldSpec", "ContextFieldSpec",
    "ENGINE_FIELDS", "PROJECT_FIELDS", "CONTEXT_FIELDS",
    "KNOWN_TOP_LEVEL_KEYS", "OMIT",
]
