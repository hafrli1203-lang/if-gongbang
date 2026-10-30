"""P6 (docs/flow-map.md 사 "설정·문서는 같은 커밋에서 같이 바꾼다"):
engine.config.ENGINE_FIELDS/PROJECT_FIELDS/CONTEXT_FIELDS are the single
table that drives both from_json's parsing and to_json_dict's writing.
This test builds ONE config JSON that sets every field those three
tables know about to a non-default value, round-trips it twice
(load -> dump -> load), and asserts the two loaded EngineConfig
instances are equal -- a field present in only one half of the table
(parsed but never serialized, or vice versa) would make this fail
either by never reaching a non-default value, or by not surviving the
round trip. It also checks that an unknown top-level key is still
rejected (the known-keys set is derived from ENGINE_FIELDS, not a
second hand-written list that could drift from it).
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import engine.config as config_mod
from engine.config import EngineConfig, to_json_dict, write_json

_OMIT = config_mod._OMIT  # the module's own "leave this key out" sentinel


def _full_project_dict(tmp_path: Path) -> dict[str, object]:
    (tmp_path / "repo").mkdir(exist_ok=True)
    (tmp_path / "reference-material").mkdir(exist_ok=True)
    return {
        "repo": "repo",
        "excludes": ["node_modules"],
        "artifact_paths": ["dist"],
        "test_cmd": ["npm", "test"],
        "install_cmd": ["npm", "install"],
        "manifest": "package.json",
        "bench_cases": "bench_cases.json",
        "test_timeout_sec": 42,
        "bench_scope": "full",
        "related_cmd": ["npx", "vitest", "related", "--run", "{files}"],
        "arch_check_cmd": ["python3", "tools/arch_check.py"],
        "max_fix_rounds": 7,
        "builder_test_hint": "run related tests as you edit",
        "copy_links": ["node_modules"],
        "copy_link_writable_subpaths": {"node_modules": [".vite-temp", ".vite"]},
        "release": {"master_repo": "master-repo", "target": "myapp"},
        "hidden_paths": [str(tmp_path / "original-src")],
        "reference_paths": [str(tmp_path / "reference-material")],
        "probe_runtime_clis": ["claude"],
        "file_dep_targets": [["../_shared/kit", str(tmp_path / "shared" / "kit")]],
    }


def _full_config_dict(tmp_path: Path) -> dict[str, object]:
    return {
        "ledger_path": "custom/ledger.sqlite",
        "home": "custom/home",
        "secrets_dir": "custom/secrets",
        "projects": {"alpha": _full_project_dict(tmp_path)},
        "station_owners": {"build": {"builder": ["src/"]}},
        "protected_paths": ["secret/"],
        "hooks": ["hooks/ok.py"],
        "hook_timeout_sec": 5,
        "approval_ttl_sec": 60,
        "watch_max_resumes_per_hour": 5,
        "stations": ["build", "review"],
        "optional_stations": ["review"],
        "workers": {
            "builder": {"runner": "claude", "model": "m", "max_turns": 10,
                        "budget_usd": 1.5, "timeout_sec": 60.0},
            "reviewer": {"runner": "codex", "reasoning": "low"},
        },
        "station_terminal_allow": {"build": {"builder": ["ls"]}},
        "worker_env_passthrough": ["PATH", "HOME"],
        "sandbox": "bwrap",
        "bench_home_writable": True,
        "bench_readonly_paths": [str(tmp_path / "toolcache")],
        "publisher": {
            "kind": "shell",
            "argv": ["/opt/deploy.sh", "{target}"],
            "dry_run_argv": ["/opt/deploy.sh", "{target}", "--check"],
            "timeout_sec": 120, "dry_run": False,
        },
        "parallel_review_bench": False,
        "mcp_servers": {
            "srv1": {"command": ["npx", "-y", "@acme/mcp"], "args": ["--flag"],
                     "env_passthrough": ["PATH"]},
        },
        "station_mcp_allow": {"build": ["srv1"]},
        "mcp_tool_allow": {"srv1": ["tool1"]},
        "skill_index_path": str(tmp_path / "skills" / "INDEX.md"),
        "context": {
            "skill_roots": ["root1"],
            "station_skills": {"build": ["skill1"]},
            "library_mcp": {"command": ["node", "lib.js"], "timeout_sec": 5},
            "decisions_paths": {"alpha": "CURRENT_DECISIONS.md"},
            "lessons_app": {"alpha": "alpha-app"},
            "direction_path": "DIRECTION.md",
            "required_skills": {"build": ["skill1"]},
            "require_library": ["alpha"],
        },
        "security": {"audit_cmd": ["npm", "audit"]},
    }


def _write(tmp_path: Path, data: dict[str, object]) -> Path:
    path = tmp_path / "engine.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_full_config_round_trips_every_field(tmp_path: Path) -> None:
    config = EngineConfig.from_json(_write(tmp_path, _full_config_dict(tmp_path)))

    # Every ENGINE_FIELDS row except "security" (deliberately never
    # written back, see its own row's comment) must have produced a
    # value distinguishable from EngineConfig()'s bare dataclass
    # default -- otherwise this test would "pass" without the field
    # ever having been exercised at all.
    bare_defaults = to_json_dict(EngineConfig(
        ledger_path=Path("/x/ledger.sqlite"), home=Path("/x/home"),
        secrets_dir=Path("/x/secrets"), projects={}, station_owners={},
    ))
    populated = to_json_dict(config)
    for spec in config_mod.ENGINE_FIELDS:
        if spec.key == "security":
            continue
        assert populated.get(spec.key) != bare_defaults.get(spec.key), (
            f"ENGINE_FIELDS[{spec.key!r}] never reached a non-default value "
            "-- this test's fixture is missing it"
        )

    loaded = write_json(config, tmp_path / "out.json")
    assert dataclasses.replace(loaded, config_path=None) == \
        dataclasses.replace(config, config_path=None)

    # "security" is accepted but never round-tripped (engine.app_security
    # reads it fresh from config_path's own JSON file, never caches it on
    # EngineConfig) -- the written file must not carry a "security" key
    # at all, not even a null placeholder.
    written = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert "security" not in written


def test_project_fields_round_trip(tmp_path: Path) -> None:
    config = EngineConfig.from_json(_write(tmp_path, _full_config_dict(tmp_path)))
    bare_project_defaults = {
        spec.key: spec.serialize(getattr(
            config_mod.ProjectConfig(name="x", repo=Path("/x")), spec.key))
        for spec in config_mod.PROJECT_FIELDS
    }
    populated = to_json_dict(config)["projects"]["alpha"]
    for spec in config_mod.PROJECT_FIELDS:
        if spec.key == "repo":  # always project-specific, no bare default to compare
            continue
        assert populated[spec.key] != bare_project_defaults[spec.key], (
            f"PROJECT_FIELDS[{spec.key!r}] never reached a non-default value"
        )

    loaded = write_json(config, tmp_path / "out.json")
    assert loaded.projects["alpha"] == config.projects["alpha"]


def test_context_fields_round_trip(tmp_path: Path) -> None:
    config = EngineConfig.from_json(_write(tmp_path, _full_config_dict(tmp_path)))
    assert config.context is not None
    bare_context_defaults = {
        spec.key: spec.serialize(getattr(config_mod.ContextConfig(), spec.key))
        for spec in config_mod.CONTEXT_FIELDS
    }
    populated = to_json_dict(config)["context"]
    for spec in config_mod.CONTEXT_FIELDS:
        assert populated[spec.key] != bare_context_defaults[spec.key], (
            f"CONTEXT_FIELDS[{spec.key!r}] never reached a non-default value"
        )

    loaded = write_json(config, tmp_path / "out.json")
    assert loaded.context == config.context


def test_unknown_top_level_key_is_rejected(tmp_path: Path) -> None:
    data = _full_config_dict(tmp_path)
    data["bogus_top_level_key"] = True
    try:
        EngineConfig.from_json(_write(tmp_path, data))
        raise AssertionError("expected ValueError for unknown top-level key")
    except ValueError as exc:
        assert "bogus_top_level_key" in str(exc)


def test_unknown_project_key_is_rejected(tmp_path: Path) -> None:
    data = _full_config_dict(tmp_path)
    data["projects"]["alpha"]["bogus_project_key"] = True
    try:
        EngineConfig.from_json(_write(tmp_path, data))
        raise AssertionError("expected ValueError for unknown project key")
    except ValueError as exc:
        assert "bogus_project_key" in str(exc)


def test_unknown_context_key_is_rejected(tmp_path: Path) -> None:
    data = _full_config_dict(tmp_path)
    data["context"]["bogus_context_key"] = True
    try:
        EngineConfig.from_json(_write(tmp_path, data))
        raise AssertionError("expected ValueError for unknown context key")
    except ValueError as exc:
        assert "bogus_context_key" in str(exc)
