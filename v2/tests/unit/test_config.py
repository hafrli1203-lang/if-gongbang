"""Unit tests for engine.config.EngineConfig.from_json.

from_json must load the "hooks" field (and hook_timeout_sec/
approval_ttl_sec/stations/optional_stations) -- otherwise a
JSON-configured hook would silently fail open, with the resulting
EngineConfig always carrying hooks=() regardless of what the JSON file
said, and Policy never consulting it.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from engine.config import EngineConfig
from engine.ledger import Event, Ledger
from engine.policy import Action, Actor, Policy
from engine.state import transition
from tests.conftest import make_repo

HOOKS_DIR = Path(__file__).resolve().parent.parent.parent / "engine" / "fakes" / "hooks"


def _write_config(tmp_path: Path, extra: dict[str, object]) -> Path:
    base = {
        "projects": {"alpha": {"repo": "repo"}},
        "station_owners": {"build": {"builder": ["src/"]}},
    }
    base.update(extra)
    config_path = tmp_path / "engine.json"
    config_path.write_text(json.dumps(base), encoding="utf-8")
    return config_path


def test_hooks_are_loaded_from_json_and_resolved(tmp_path: Path) -> None:
    make_repo(tmp_path / "repo", {"src/app.py": "print('x')\n"})
    config_path = _write_config(tmp_path, {
        "hooks": [str(HOOKS_DIR / "ok.py"), "relative_hook.py"],
    })
    config = EngineConfig.from_json(config_path)
    assert config.hooks == (
        HOOKS_DIR / "ok.py",
        (tmp_path / "relative_hook.py").resolve(),
    )


def test_optional_scalar_fields_are_loaded_from_json(tmp_path: Path) -> None:
    make_repo(tmp_path / "repo", {"src/app.py": "print('x')\n"})
    config_path = _write_config(tmp_path, {
        "hook_timeout_sec": 5,
        "approval_ttl_sec": 60,
        "watch_max_resumes_per_hour": 5,
        "stations": ["build", "review"],
        "optional_stations": ["review"],
    })
    config = EngineConfig.from_json(config_path)
    assert config.hook_timeout_sec == 5.0
    assert config.approval_ttl_sec == 60
    assert config.watch_max_resumes_per_hour == 5
    assert config.stations == ("build", "review")
    assert config.optional_stations == ("review",)


def test_absent_optional_fields_keep_dataclass_defaults(tmp_path: Path) -> None:
    make_repo(tmp_path / "repo", {"src/app.py": "print('x')\n"})
    config_path = _write_config(tmp_path, {})
    config = EngineConfig.from_json(config_path)
    assert config.hooks == ()
    assert config.hook_timeout_sec == 2.0
    assert config.approval_ttl_sec == 4 * 3600
    assert config.watch_max_resumes_per_hour == 3


def test_unknown_top_level_key_rejected(tmp_path: Path) -> None:
    make_repo(tmp_path / "repo", {"src/app.py": "print('x')\n"})
    # "hook" (singular) is the exact typo this rule exists to catch: without
    # the unknown-key check it would silently parse and leave hooks=().
    config_path = _write_config(tmp_path, {"hook": [str(HOOKS_DIR / "ok.py")]})
    try:
        EngineConfig.from_json(config_path)
        assert False, "expected ValueError for unknown key 'hook'"
    except ValueError as exc:
        assert "hook" in str(exc)


def test_security_key_is_accepted_but_not_parsed(tmp_path: Path) -> None:
    """engine.app_security.check() reads "security" straight back out of
    the raw JSON file (see that module's docstring) -- it must not be
    rejected as an unknown top-level key (that would make any app with
    an audit_cmd configured fail to load at all), and loading it does
    not add a dataclass field or attribute for it."""
    make_repo(tmp_path / "repo", {"src/app.py": "print('x')\n"})
    config_path = _write_config(tmp_path, {
        "security": {"audit_cmd": ["npm", "audit", "--omit=dev", "--json"]},
    })
    config = EngineConfig.from_json(config_path)
    assert not hasattr(config, "security")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert raw["security"] == {"audit_cmd": ["npm", "audit", "--omit=dev", "--json"]}


def test_workers_and_terminal_allow_loaded(tmp_path: Path) -> None:
    make_repo(tmp_path / "repo", {"src/app.py": "print('x')\n"})
    config_path = _write_config(tmp_path, {
        "workers": {
            "builder": {"runner": "claude", "model": "sonnet", "max_turns": 10,
                        "budget_usd": 1.5, "timeout_sec": 60.0},
            "reviewer": {"runner": "codex", "reasoning": "low"},
        },
        "station_terminal_allow": {
            "build": {"builder": [r"pytest( -q)?"]},
        },
        "worker_env_passthrough": ["PATH", "HOME"],
    })
    config = EngineConfig.from_json(config_path)
    assert config.workers["builder"].runner == "claude"
    assert config.workers["builder"].model == "sonnet"
    assert config.workers["builder"].max_turns == 10
    assert config.workers["builder"].budget_usd == 1.5
    assert config.workers["builder"].timeout_sec == 60.0
    assert config.workers["reviewer"].runner == "codex"
    assert config.workers["reviewer"].reasoning == "low"
    assert config.workers["reviewer"].model is None
    assert config.station_terminal_allow == {"build": {"builder": (r"pytest( -q)?",)}}
    assert config.worker_env_passthrough == ("PATH", "HOME")


def test_unknown_project_key_rejected(tmp_path: Path) -> None:
    make_repo(tmp_path / "repo", {"src/app.py": "print('x')\n"})
    config_path = tmp_path / "engine.json"
    config_path.write_text(json.dumps({
        "projects": {"alpha": {"repo": "repo", "bogus_key": "x"}},
        "station_owners": {"build": {"builder": ["src/"]}},
    }), encoding="utf-8")
    try:
        EngineConfig.from_json(config_path)
        assert False, "expected ValueError for unknown project key"
    except ValueError as exc:
        assert "bogus_key" in str(exc)


def test_bad_runner_rejected(tmp_path: Path) -> None:
    make_repo(tmp_path / "repo", {"src/app.py": "print('x')\n"})
    config_path = _write_config(tmp_path, {
        "workers": {"builder": {"runner": "gpt-nope"}},
    })
    try:
        EngineConfig.from_json(config_path)
        assert False, "expected ValueError for bad runner"
    except ValueError as exc:
        assert "gpt-nope" in str(exc)


def test_policy_denies_write_via_json_loaded_denying_hook(tmp_path: Path) -> None:
    """End-to-end: a hook named in the JSON config must actually reach
    Policy and be able to deny an otherwise-allowed write."""
    repo = make_repo(tmp_path / "repo", {"src/app.py": "print('x')\n"})
    config_path = _write_config(tmp_path, {
        "hooks": [str(HOOKS_DIR / "crash.py")],
        "hook_timeout_sec": 1.0,
    })
    config = EngineConfig.from_json(config_path)

    ledger = Ledger.create(config.ledger_path)
    ledger.register_project("alpha")
    attempt_id = "J-alpha-CFGHOOK#1"
    ledger.append(Event(
        project="alpha", kind="job.opened", actor_kind="owner", actor_id="owner",
        attempt_id=attempt_id,
        payload={
            "job_id": "J-alpha-CFGHOOK", "job_name": "cfghook",
            "attempt_id": attempt_id, "n": 1, "kind": "full",
            "generation": 1, "dirty_paths": [],
        },
    ))
    transition(ledger, "alpha", "open_job", "owner")

    copy_root = config.home / "copies" / attempt_id / "builder"
    shutil.copytree(repo, copy_root)
    actor = Actor(kind="worker", role="builder", attempt_id=attempt_id,
                  generation=1, station="build", copy_root=copy_root)
    policy = Policy(config, ledger)
    decision = policy.check("alpha", actor, Action(
        tool="write", path=copy_root / "src" / "new.py"))
    assert decision.allowed is False
    assert decision.reason.startswith("hook:")


def test_test_timeout_sec_loaded_and_validated(tmp_path: Path) -> None:
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    config = EngineConfig.from_json(_write_config(tmp_path, {
        "projects": {"alpha": {"repo": "repo", "test_timeout_sec": 12}},
    }))
    assert config.projects["alpha"].test_timeout_sec == 12.0
    bad = _write_config(tmp_path, {
        "projects": {"alpha": {"repo": "repo", "test_timeout_sec": "slow"}},
    })
    try:
        EngineConfig.from_json(bad)
    except ValueError as exc:
        assert "test_timeout_sec" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_copy_links_loaded_from_json(tmp_path: Path) -> None:
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    config = EngineConfig.from_json(_write_config(tmp_path, {
        "projects": {"alpha": {"repo": "repo", "copy_links": ["node_modules"]}},
        "sandbox": "bwrap",
    }))
    assert config.projects["alpha"].copy_links == ("node_modules",)


def test_copy_link_writable_subpaths_loaded_from_json(tmp_path: Path) -> None:
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    config = EngineConfig.from_json(_write_config(tmp_path, {
        "projects": {"alpha": {
            "repo": "repo", "copy_links": ["node_modules"],
            "copy_link_writable_subpaths": {"node_modules": [".vite-temp", ".vite"]},
        }},
        "sandbox": "bwrap",
    }))
    assert config.projects["alpha"].copy_link_writable_subpaths == {
        "node_modules": (".vite-temp", ".vite"),
    }


def test_copy_link_writable_subpaths_entry_must_be_in_copy_links() -> None:
    from engine.config import ProjectConfig
    try:
        ProjectConfig(
            "alpha", Path("/repo"), copy_links=("node_modules",),
            copy_link_writable_subpaths={"other": (".vite",)},
        )
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "other" in str(exc)


@pytest.mark.parametrize("subpath", ["/etc/passwd", "../escape"])
def test_copy_link_writable_subpaths_rejects_unsafe_subpath(subpath: str) -> None:
    from engine.config import ProjectConfig
    try:
        ProjectConfig(
            "alpha", Path("/repo"), copy_links=("node_modules",),
            copy_link_writable_subpaths={"node_modules": (subpath,)},
        )
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_copy_links_rejects_absolute_path(tmp_path: Path) -> None:
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    bad = _write_config(tmp_path, {
        "projects": {"alpha": {"repo": "repo", "copy_links": ["/etc/passwd"]}},
    })
    try:
        EngineConfig.from_json(bad)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "/etc/passwd" in str(exc)


def test_copy_links_rejects_dotdot(tmp_path: Path) -> None:
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    bad = _write_config(tmp_path, {
        "projects": {"alpha": {"repo": "repo", "copy_links": ["../escape"]}},
    })
    try:
        EngineConfig.from_json(bad)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "escape" in str(exc)


def test_copy_links_rejects_inside_git(tmp_path: Path) -> None:
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    bad = _write_config(tmp_path, {
        "projects": {"alpha": {"repo": "repo", "copy_links": [".git/hooks"]}},
    })
    try:
        EngineConfig.from_json(bad)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert ".git" in str(exc)


def test_copy_links_direct_construction_is_also_validated() -> None:
    """ProjectConfig built directly in Python (not via from_json, e.g. a
    unit test fixture) must be validated too -- __post_init__, not just
    the JSON loader."""
    from engine.config import ProjectConfig
    try:
        ProjectConfig("alpha", Path("/repo"), copy_links=("/abs",))
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_copy_links_without_bwrap_sandbox_is_rejected(tmp_path: Path) -> None:
    """copy_links is a live write path into the repo under sandbox="none"
    (a worker's Bash can write through copy/node_modules -> repo/
    node_modules): EngineConfig refuses to load such a config at all."""
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    bad = _write_config(tmp_path, {
        "projects": {"alpha": {"repo": "repo", "copy_links": ["node_modules"]}},
    })
    try:
        EngineConfig.from_json(bad)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "copy_links_requires_bwrap" in str(exc)


def test_copy_links_under_an_owned_prefix_is_rejected(tmp_path: Path) -> None:
    """station_owners' write prefixes are not project-scoped (the same
    table applies to every project), so a copy_links entry at or under
    one of them -- e.g. the builder's own "src/" -- would make merge's
    own symlink rejection fire against a worker's otherwise-valid
    change. Caught at config load instead."""
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    bad = _write_config(tmp_path, {
        "projects": {"alpha": {"repo": "repo", "copy_links": ["src/vendor"]}},
        "station_owners": {"build": {"builder": ["src/"]}},
        "sandbox": "bwrap",
    })
    try:
        EngineConfig.from_json(bad)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "copy_link_under_owned_prefix" in str(exc)


def test_copy_links_with_dot_slash_prefix_under_owned_prefix_is_also_rejected(
    tmp_path: Path,
) -> None:
    """"./src/vendor" and "src/vendor" name the same real location --
    the overlap check must normalize both the entry and the owned
    prefix before comparing, not treat a leading "./" (or a trailing
    "/" on the prefix) as a different string that slips past it."""
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    bad = _write_config(tmp_path, {
        "projects": {"alpha": {"repo": "repo", "copy_links": ["./src/vendor"]}},
        "station_owners": {"build": {"builder": ["src/"]}},
        "sandbox": "bwrap",
    })
    try:
        EngineConfig.from_json(bad)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "copy_link_under_owned_prefix" in str(exc)


def test_bench_readonly_paths_loaded_from_json(tmp_path: Path) -> None:
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    cache_dir = tmp_path / "somewhere" / "cache"
    config = EngineConfig.from_json(_write_config(tmp_path, {
        "projects": {"alpha": {"repo": "repo"}},
        "bench_readonly_paths": [str(cache_dir)],
    }))
    assert config.bench_readonly_paths == (cache_dir.resolve(),)


def test_bench_readonly_paths_rejects_entry_at_or_above_ledger_dir(tmp_path: Path) -> None:
    """ledger_path.parent defaults to the config's own base dir
    (tmp_path here) -- an entry equal to or above it would re-expose
    the ledger (and everything else under that dir) read-only from
    inside the bench sandbox, not just a toolchain cache."""
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    bad = _write_config(tmp_path, {
        "projects": {"alpha": {"repo": "repo"}},
        "bench_readonly_paths": [str(tmp_path)],
    })
    try:
        EngineConfig.from_json(bad)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "bench_readonly_path_forbidden" in str(exc)


def test_bench_readonly_paths_rejects_non_absolute_entry(tmp_path: Path) -> None:
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    bad = _write_config(tmp_path, {
        "projects": {"alpha": {"repo": "repo"}},
        "bench_readonly_paths": ["relative/cache"],
    })
    try:
        EngineConfig.from_json(bad)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "absolute" in str(exc)


def test_publisher_defaults_to_fake(tmp_path: Path) -> None:
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    config = EngineConfig.from_json(_write_config(tmp_path, {
        "projects": {"alpha": {"repo": "repo"}},
    }))
    assert config.publisher_kind == "fake"
    assert config.publisher_dry_run is True
    assert config.publisher_argv == ()


def test_publisher_shell_loaded_from_json(tmp_path: Path) -> None:
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    config = EngineConfig.from_json(_write_config(tmp_path, {
        "projects": {"alpha": {"repo": "repo"}},
        "publisher": {
            "kind": "shell",
            "argv": ["/opt/deploy/deploy.sh", "{target}"],
            "dry_run_argv": ["/opt/deploy/deploy.sh", "{target}", "--check"],
            "timeout_sec": 120, "dry_run": False,
        },
    }))
    assert config.publisher_kind == "shell"
    assert config.publisher_argv == ("/opt/deploy/deploy.sh", "{target}")
    assert config.publisher_dry_run_argv == (
        "/opt/deploy/deploy.sh", "{target}", "--check")
    assert config.publisher_timeout_sec == 120
    assert config.publisher_dry_run is False


def test_publisher_rejects_unknown_kind(tmp_path: Path) -> None:
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    bad = _write_config(tmp_path, {
        "projects": {"alpha": {"repo": "repo"}},
        "publisher": {"kind": "ftp"},
    })
    try:
        EngineConfig.from_json(bad)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "ftp" in str(exc)


def test_publisher_rejects_unknown_key(tmp_path: Path) -> None:
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    bad = _write_config(tmp_path, {
        "projects": {"alpha": {"repo": "repo"}},
        "publisher": {"kind": "shell", "argvv": []},
    })
    try:
        EngineConfig.from_json(bad)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "argvv" in str(exc)


def test_write_json_roundtrip_sets_config_path(tmp_path: Path) -> None:
    from dataclasses import replace
    from engine.config import WorkerConfig, write_json
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    base = EngineConfig.from_json(_write_config(tmp_path, {
        "projects": {"alpha": {"repo": "repo", "test_cmd": ["echo", "x"]}},
    }))
    config = replace(base, workers={"builder": WorkerConfig("claude", model="m")},
                     station_terminal_allow={"build": {"builder": ("ls",)}})
    loaded = write_json(config, tmp_path / "out.json")
    assert loaded.config_path == (tmp_path / "out.json").resolve()
    assert replace(loaded, config_path=None) == replace(config, config_path=None)


def test_hidden_paths_must_be_absolute(tmp_path: Path) -> None:
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    bad = _write_config(tmp_path, {
        "projects": {"alpha": {"repo": "repo", "hidden_paths": ["relative/path"]}},
    })
    try:
        EngineConfig.from_json(bad)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "hidden_paths" in str(exc)


def test_hidden_paths_loaded_and_roundtrips_through_to_json_dict(tmp_path: Path) -> None:
    from engine.config import to_json_dict, write_json
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    original_src = tmp_path / "original-src"
    original_src.mkdir()
    config = EngineConfig.from_json(_write_config(tmp_path, {
        "projects": {"alpha": {"repo": "repo", "hidden_paths": [str(original_src)]}},
    }))
    assert config.projects["alpha"].hidden_paths == (original_src,)
    loaded = write_json(config, tmp_path / "out.json")
    assert loaded.projects["alpha"].hidden_paths == (original_src,)
    assert to_json_dict(config)["projects"]["alpha"]["hidden_paths"] == [str(original_src)]



# ---------------------------------------------------------------------------
# reference_paths (ProjectConfig): FORM-only at config parse time (absolute
# or ~/...). All host-filesystem checks (directory-ness, .env* content,
# overlap with protected roots) moved to engine.infrastructure.
# reference_snapshot.snapshot_references, run once per attempt -- NOT
# here, so config loading can never die on another project's unreachable
# reference folder P1 fix). See tests/unit/test_reference_snapshot.py
# for the actual security checks.
# ---------------------------------------------------------------------------

def test_reference_paths_loaded_and_roundtrips_through_to_json_dict(tmp_path: Path) -> None:
    from engine.config import to_json_dict, write_json
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    reference = tmp_path / "reference-material"
    config = EngineConfig.from_json(_write_config(tmp_path, {
        "projects": {"alpha": {"repo": "repo", "reference_paths": [str(reference)]}},
    }))
    assert config.projects["alpha"].reference_paths == (reference,)
    loaded = write_json(config, tmp_path / "out.json")
    assert loaded.projects["alpha"].reference_paths == (reference,)
    assert (to_json_dict(config)["projects"]["alpha"]["reference_paths"]
            == [str(reference)])


def test_reference_paths_does_not_require_the_entry_to_exist(tmp_path: Path) -> None:
    """The P1 fix itself: a reference_paths entry that does not exist on
    disk at all must still load fine -- no is_dir()/existence check at
    config-parse time any more (that check now lives in
    engine.infrastructure.reference_snapshot, run once per attempt, not
    on every config re-parse)."""
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    missing = tmp_path / "does-not-exist-at-all"
    assert not missing.exists()
    config = EngineConfig.from_json(_write_config(tmp_path, {
        "projects": {"alpha": {"repo": "repo", "reference_paths": [str(missing)]}},
    }))
    assert config.projects["alpha"].reference_paths == (missing,)


def test_reference_paths_rejects_non_absolute_entry(tmp_path: Path) -> None:
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    bad = _write_config(tmp_path, {
        "projects": {"alpha": {"repo": "repo", "reference_paths": ["relative/material"]}},
    })
    try:
        EngineConfig.from_json(bad)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "absolute" in str(exc)


def test_reference_paths_accepts_tilde_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    config = EngineConfig.from_json(_write_config(tmp_path, {
        "projects": {"alpha": {"repo": "repo", "reference_paths": ["~/Documents/material"]}},
    }))
    assert config.projects["alpha"].reference_paths == (fake_home / "Documents" / "material",)


def test_reference_paths_other_projects_missing_entry_does_not_break_config_load(
    tmp_path: Path,
) -> None:
    """P1 #5's exact repro, at the config layer: project beta names a
    reference folder that does not exist (standing in for "momentarily
    unreachable from whatever host is re-parsing this config, e.g. a
    worker's gate hook") while alpha has none at all -- loading the
    WHOLE config (as a gate hook does on every tool call) must not fail
    just because of beta's entry."""
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    make_repo(tmp_path / "repo-beta", {"a.txt": "a\n"})
    config = EngineConfig.from_json(_write_config(tmp_path, {
        "projects": {
            "alpha": {"repo": "repo"},
            "beta": {"repo": "repo-beta",
                     "reference_paths": [str(tmp_path / "unreachable-for-beta")]},
        },
    }))
    assert config.projects["alpha"].reference_paths == ()
    assert config.projects["beta"].reference_paths == (tmp_path / "unreachable-for-beta",)


def test_file_dep_targets_rejects_a_target_inside_secrets_dir(tmp_path: Path) -> None:
    """a project's file_dep_targets remaps an
    absolute host path READ-ONLY into a worker/bench sandbox at a
    DIFFERENT, aliased location (engine.application.steps.file_deps.
    sandbox_file_dep_pairs) -- unlike bench_readonly_paths, that remap
    had no overlap check of its own at all, so a target pointing
    inside secrets_dir (the one place the office-approval HMAC key now
    always lives) would re-expose it through the alias even though the
    real secrets_dir path stays hidden everywhere else."""
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    secrets_dir = (tmp_path / "secrets").resolve()
    bad = _write_config(tmp_path, {
        "projects": {"alpha": {
            "repo": "repo",
            "file_dep_targets": [["../_shared/kit", str(secrets_dir / "office-approval.key")]],
        }},
    })
    try:
        EngineConfig.from_json(bad)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "file_dep_target_forbidden_overlap" in str(exc)


def test_file_dep_targets_rejects_a_target_above_secrets_dir(tmp_path: Path) -> None:
    """Same rule, other direction: a target at or ABOVE secrets_dir
    (e.g. its parent) would remap everything under secrets_dir too,
    not only a target strictly inside it."""
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    bad = _write_config(tmp_path, {
        "projects": {"alpha": {
            "repo": "repo",
            "file_dep_targets": [["../_shared/kit", str(tmp_path)]],
        }},
    })
    try:
        EngineConfig.from_json(bad)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "file_dep_target_forbidden_overlap" in str(exc)


def test_file_dep_targets_allows_a_target_under_the_same_root_as_the_ledger(
    tmp_path: Path,
) -> None:
    """A target BELOW ledger_path.parent (not AT/ABOVE it, and not
    inside secrets_dir) is ordinary and expected -- e.g. a real
    deployment's apps all living under the same root the ledger does
    (tests/acceptance/test_t13_file_dep_real_sandbox.py's own layout).
    Only home/ledger_path.parent/real_home's one-directional rule
    (bench_readonly_paths') and secrets_dir's stricter bidirectional
    one apply here -- not "everywhere under the config's own base
    directory"."""
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    shared = tmp_path / "shared" / "kit"
    config = EngineConfig.from_json(_write_config(tmp_path, {
        "projects": {"alpha": {
            "repo": "repo",
            "file_dep_targets": [["../_shared/kit", str(shared)]],
        }},
    }))
    assert config.projects["alpha"].file_dep_targets == (("../_shared/kit", str(shared)),)


def test_file_dep_targets_rejects_a_dotdot_path_that_resolves_into_secrets_dir(
    tmp_path: Path,
) -> None:
    """a target spelled with a "../" component
    that LOOKS like it stays outside secrets_dir, but actually resolves
    (os.path.realpath) straight into it, must be refused exactly like
    the direct spelling already is -- a plain, un-normalized Path
    comparison missed this."""
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    secrets_dir = (tmp_path / "secrets").resolve()
    secrets_dir.mkdir()
    sneaky_target = tmp_path / "shared" / ".." / "secrets" / "office-approval.key"
    bad = _write_config(tmp_path, {
        "projects": {"alpha": {
            "repo": "repo",
            "file_dep_targets": [["../_shared/kit", str(sneaky_target)]],
        }},
    })
    try:
        EngineConfig.from_json(bad)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "file_dep_target_forbidden_overlap" in str(exc)


def test_bench_readonly_paths_rejects_an_entry_inside_secrets_dir(
    tmp_path: Path,
) -> None:
    """the previous check only forbade an entry
    AT OR ABOVE secrets_dir -- an entry strictly INSIDE it (e.g. the
    office-approval key file itself) was allowed through, and
    suite_run.py's own bench sandbox would then re-expose it read-only
    despite secrets_dir being hidden."""
    make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    secrets_dir = (tmp_path / "secrets").resolve()
    secrets_dir.mkdir()
    bad = _write_config(tmp_path, {
        "projects": {"alpha": {"repo": "repo"}},
        "bench_readonly_paths": [str(secrets_dir / "office-approval.key")],
    })
    try:
        EngineConfig.from_json(bad)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "bench_readonly_path_forbidden" in str(exc)
