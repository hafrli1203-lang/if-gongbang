"""Engine configuration. Nothing project-specific is hardcoded here.

All project names, roles, stations, allowed paths and protected paths come
from an EngineConfig built by the caller (tests build one from fixtures).
"""
from __future__ import annotations

import json
import os
import posixpath
from dataclasses import dataclass, field, replace
from pathlib import Path

_RUNNER_KINDS = ("claude", "codex", "scripted")


@dataclass(frozen=True)
class WorkerConfig:
    """One worker role's launch settings (AI CLI or scripted runner)."""

    runner: str  # "claude" | "codex" | "scripted"
    model: str | None = None
    max_turns: int = 40
    budget_usd: float = 3.0
    timeout_sec: float = 900.0
    reasoning: str | None = None  # codex: model_reasoning_effort

    def __post_init__(self) -> None:
        if self.runner not in _RUNNER_KINDS:
            raise ValueError(f"unknown worker runner: {self.runner!r}")


_KNOWN_RUNNERS = frozenset({"claude", "codex", "scripted"})
_KNOWN_BENCH_SCOPES = frozenset({"related", "full"})


@dataclass(frozen=True)
class ReleaseConfig:
    """4단계 release track for one project (ENGINE.md "배포를 궤도 안에"):
    the original app repo this project's own repo fast-forwards into
    once an owner approves a publish request, and the deploy target
    name passed to the top-level `publisher` argv's "{target}"."""

    master_repo: Path
    target: str

    def __post_init__(self) -> None:
        if not self.target:
            raise ValueError("release.target must be a non-empty string")


@dataclass(frozen=True)
class ProjectConfig:
    """One managed project (a git repository on disk)."""

    name: str
    repo: Path
    # relative paths inside repo that are never fingerprinted/archived
    excludes: tuple[str, ...] = ()
    # relative paths holding build artifacts (compared by artifact hash)
    artifact_paths: tuple[str, ...] = ()
    # run in repo; exit 0 means the project's tests passed
    test_cmd: tuple[str, ...] = ()
    # run in repo after a manifest change (install/update dependencies)
    install_cmd: tuple[str, ...] = ()
    # relative path the install-like request kinds may edit
    manifest: str | None = None
    # relative path of the bench cases JSON (engine.steps.bench_run)
    bench_cases: str | None = None
    # per-run timeout for test_cmd (full run and each trust-check case)
    test_timeout_sec: float = 300.0
    # bench's run_tests scope (engine.steps.bench_run): "related" (default)
    # runs related_cmd against only this attempt's changed source files
    # when related_cmd is configured and there IS at least one changed
    # file, falling back to the full test_cmd otherwise; "full" always
    # runs test_cmd. A project with no related_cmd set behaves exactly
    # like "full" regardless of this value (nothing to run instead).
    bench_scope: str = "related"
    # run in repo for a "related"-scope bench, with one list entry equal
    # to the literal string "{files}" replaced in place by the attempt's
    # changed source file paths (one argv token per file, never joined
    # into a single string) -- e.g. ["npx", "vitest", "related", "--run",
    # "{files}"]. Empty (default): bench_scope "related" always falls
    # back to the full test_cmd for this project.
    related_cmd: tuple[str, ...] = ()
    # relative paths that _make_copy symlinks into a fresh worker copy
    # from the repo itself (e.g. "node_modules" -- a gitignored directory
    # `git archive` never puts in the copy, needed only so a Bash-run
    # test_cmd/install_cmd *inside the copy* can resolve it; it is NOT a
    # policy carve-out -- engine.policy's worker-read-bounded-to-copy_root
    # rule (rule 1) resolves symlinks via os.path.realpath, so a worker's
    # own Read/Glob/Grep of copy/node_modules/... still resolves to the
    # real repo path and stays denied there; only a Bash command the
    # engine itself runs *in* the copy (test_cmd, install_cmd) benefits).
    copy_links: tuple[str, ...] = ()
    # copy_links entry -> relative subpaths under it that get a fresh,
    # empty, WRITABLE tmpfs scratch mount in the worker sandbox (deeper
    # than the entry's own read-only re-exposure, so it wins) -- e.g.
    # {"node_modules": (".vite-temp", ".vite")}: Vite/Vitest write
    # their bundled config and cache there at startup, and the entry's
    # read-only mount alone would make that EROFS. Per-worker scratch
    # that is never backed by anything on the host and never reaches
    # the real repo's node_modules, regardless of writes.
    copy_link_writable_subpaths: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # 4단계 release track (ENGINE.md), or None -- a project with no
    # `release` configured keeps today's publish behavior unchanged
    # (engine.steps.publish.steps() only inserts advance_master when
    # this is set).
    release: "ReleaseConfig | None" = None
    # Absolute host paths to hide (tmpfs) in EVERY builder/reviewer/
    # bench sandbox for this project, on top of the fixed set each
    # already hides (engine home, secrets_dir, other projects' repos,
    # the ledger). For a setup where `repo` is itself a disposable
    # worktree/clone and the TRUE original source checkout (with its
    # own real .env/credentials) lives at a separate, fixed path, that
    # original path would otherwise stay visible read-only through
    # the sandbox's own "--ro-bind / /" base mirror -- sandboxed code
    # (e.g. a builder-run test file, which runs outside engine.policy)
    # could read it directly by that absolute path even though `repo`
    # itself is safely isolated. Each entry must be an absolute path;
    # a nonexistent one is silently skipped at mount time, same as any
    # other `hidden` entry (engine.sandbox.wrap).
    hidden_paths: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        if self.bench_scope not in _KNOWN_BENCH_SCOPES:
            raise ValueError(f"unknown bench_scope: {self.bench_scope!r}")
        for entry in self.copy_links:
            _validate_copy_links_entry(entry)
        for entry, subpaths in self.copy_link_writable_subpaths.items():
            if entry not in self.copy_links:
                raise ValueError(
                    f"copy_link_writable_subpaths entry not in copy_links: {entry!r}"
                )
            for subpath in subpaths:
                _validate_copy_link_writable_subpath(subpath)
        for entry in self.hidden_paths:
            if not entry.is_absolute():
                raise ValueError(f"hidden_paths entry must be an absolute path: {entry!r}")


def _normalize_rel(path: str) -> str:
    """posixpath.normpath, with the no-op "." result (an empty or
    "./"-only path) mapped back to "" -- a leading "./" or a trailing
    "/" must compare equal to the plain form ("./src/vendor" ==
    "src/vendor"), not slip past the owned-prefix overlap check as a
    lexically different string for the same real location."""
    normalized = posixpath.normpath(path)
    return "" if normalized == "." else normalized


def _under_any_prefix(entry: str, prefixes: tuple[str, ...]) -> bool:
    trimmed_entry = _normalize_rel(entry)
    for prefix in prefixes:
        trimmed_prefix = _normalize_rel(prefix)
        if trimmed_entry == trimmed_prefix or trimmed_entry.startswith(trimmed_prefix + "/"):
            return True
    return False


def _is_ancestor_or_equal(entry: Path, root: Path) -> bool:
    """True iff `entry` IS `root`, or is an ancestor of it (entry at or
    above root in the tree) -- the forbidden shape for
    bench_readonly_paths: an entry strictly BELOW root (e.g. a cache
    dir under the real HOME) is fine and is exactly what the setting
    is for."""
    if entry == root:
        return True
    try:
        root.relative_to(entry)
        return True
    except ValueError:
        return False


def _validate_copy_links_entry(entry: str) -> None:
    if not entry or entry.startswith("/"):
        raise ValueError(f"copy_links entry must be a relative path: {entry!r}")
    parts = Path(entry).parts
    if ".." in parts:
        raise ValueError(f"copy_links entry must not contain '..': {entry!r}")
    if parts[0] == ".git":
        raise ValueError(f"copy_links entry must not be inside .git: {entry!r}")


def _validate_copy_link_writable_subpath(subpath: str) -> None:
    if not subpath or subpath.startswith("/"):
        raise ValueError(
            f"copy_link_writable_subpaths subpath must be a relative path: {subpath!r}"
        )
    if ".." in Path(subpath).parts:
        raise ValueError(
            f"copy_link_writable_subpaths subpath must not contain '..': {subpath!r}"
        )


@dataclass(frozen=True)
class EngineConfig:
    """Workspace-wide engine settings."""

    ledger_path: Path
    home: Path  # engine home: jobs/, archive/, copies/ live under here
    secrets_dir: Path
    projects: dict[str, ProjectConfig]
    # station -> role -> allowed write prefixes (relative to the worker copy)
    station_owners: dict[str, dict[str, tuple[str, ...]]]
    # paths (absolute or relative-to-repo) workers may never write; .git,
    # ledger, secrets and home are always protected regardless of this list
    protected_paths: tuple[str, ...] = ()
    # policy hook scripts (engine.hooks.run) consulted after rules 1-6 allow
    # a non-read worker action; any deny/failed hook flips the decision
    hooks: tuple[Path, ...] = ()
    hook_timeout_sec: float = 2.0
    approval_ttl_sec: int = 4 * 3600
    stations: tuple[str, ...] = (
        "register", "facts", "probe", "plan", "design",
        "build", "review", "bench", "report",
    )
    optional_stations: tuple[str, ...] = ("design", "cross")
    # role -> worker launch settings (engine.workers)
    workers: dict[str, WorkerConfig] = field(default_factory=dict)
    # station -> role -> allowed terminal command regexes (re.fullmatch)
    station_terminal_allow: dict[str, dict[str, tuple[str, ...]]] = field(
        default_factory=dict)
    # environment variable names passed through to worker subprocesses
    worker_env_passthrough: tuple[str, ...] = ("PATH", "HOME", "LANG", "TERM")
    # OS sandbox for spawned child processes (engine.sandbox): "none"
    # (default, keeps every existing test behavior-identical) or "bwrap".
    sandbox: str = "none"
    # bench's run_tests/trust_checks subprocesses are, by default, NOT
    # given a writable HOME under sandbox="bwrap" -- only a project
    # whose test_cmd itself needs to write under $HOME (e.g. a tool
    # that caches under ~/.cache) should set this, and only after
    # checking it does not also widen what that tool can read.
    bench_home_writable: bool = False
    # Absolute host paths (or "~/..." expanded with the ENGINE PROCESS's
    # own os.environ["HOME"] -- never a worker's or bench case's env) a
    # bench's sandbox re-exposes read-only despite hiding the real HOME
    # (e.g. "~/.cache/ms-playwright": a project's test suite needs
    # browser binaries installed there, but must not get the rest of
    # the operator's real home). Default empty. Validated in
    # __post_init__ against ledger_path.parent, secrets_dir, home and the
    # real HOME itself.
    bench_readonly_paths: tuple[Path, ...] = ()
    # Which Publisher engine.cli._build_fakes wires in: "fake" (default,
    # FakePublisher, in-memory, used by every test) or "shell"
    # (engine.publishers.ShellPublisher, drives a real deploy script).
    # publisher_argv/publisher_dry_run_argv may contain a literal
    # "{target}" placeholder; publisher_dry_run_argv is what actually
    # runs while publisher_dry_run is True (the default) -- a real
    # deploy command only ever runs once an owner has explicitly set
    # this to False for that project's config.
    publisher_kind: str = "fake"
    publisher_argv: tuple[str, ...] = ()
    publisher_dry_run_argv: tuple[str, ...] = ()
    publisher_timeout_sec: float = 900.0
    publisher_dry_run: bool = True
    # engine.orbit speed default: when True (the default), a "review"
    # stage immediately followed by a "bench" stage runs its reviewer's
    # dispatch step (spawn) and then the whole bench request while the
    # spawned reviewer process is still running, finishing the review
    # request (await+record) only after bench completes -- the project
    # lock still serializes every ledger write; only the external
    # reviewer child process overlaps with bench's own subprocess. False
    # keeps the old fully-sequential review-then-bench behavior.
    parallel_review_bench: bool = True
    # path to the JSON file this EngineConfig was loaded from (GB_CONFIG
    # for a spawned worker's gate hook); None for a config built directly
    # in Python (e.g. most unit tests), which must then never spawn a
    # worker that relies on GB_CONFIG.
    config_path: Path | None = None

    def __post_init__(self) -> None:
        if self.sandbox not in _KNOWN_SANDBOX_MODES:
            raise ValueError(f"unknown sandbox mode: {self.sandbox!r}")
        if self.publisher_kind not in _KNOWN_PUBLISHER_KINDS:
            raise ValueError(f"unknown publisher kind: {self.publisher_kind!r}")
        # copy_links symlinks a gitignored dir (e.g. node_modules) straight
        # from the repo into a worker's copy (ENGINE.md 3단계 copy_links) so
        # a Bash-run test_cmd/install_cmd *inside the copy* can resolve it.
        # With sandbox="none" that symlink is also a live write path INTO
        # the repo for any code the worker runs via Bash (e.g.
        # copy/node_modules/../../repo/<file>), since nothing isolates the
        # worker's filesystem access at the OS level -- only sandbox="bwrap"
        # (station.py hides the repo itself and re-exposes copy_links
        # targets read-only) closes that. Fail closed at config load, not
        # at the moment some worker exploits it.
        if self.sandbox != "bwrap":
            for name, project in self.projects.items():
                if project.copy_links:
                    raise ValueError("copy_links_requires_bwrap")
        # station_owners' write prefixes are not project-scoped (the same
        # table applies to every project's worker copy, see conftest's
        # STATION_OWNERS fixture). A copy_links entry at or under one of
        # them would make merge's _candidate_files() encounter the
        # engine-created symlink, if the copy's own `git status` ever
        # reports that path (e.g. the entry is not itself gitignored),
        # while planning a builder's own, legitimate changes under that
        # prefix, and reject the whole merge with symlink_in_copy --
        # caught here, at config load, rather than at merge time against
        # a worker's otherwise-valid change.
        owned_prefixes = tuple(
            prefix for roles in self.station_owners.values()
            for prefixes in roles.values() for prefix in prefixes
        )
        for name, project in self.projects.items():
            for entry in project.copy_links:
                if _under_any_prefix(entry, owned_prefixes):
                    raise ValueError("copy_link_under_owned_prefix")
        # bench_readonly_paths exists to re-expose a TOOLCHAIN cache dir
        # (e.g. ~/.cache/ms-playwright) from under an otherwise-hidden
        # real HOME -- an entry equal to or ABOVE the real HOME itself,
        # the ledger dir, secrets_dir or config.home would instead
        # re-expose one of those wholesale (the same shape of mistake
        # this module's own copy_links/sandbox fixes closed elsewhere).
        forbidden_roots = [self.home, self.secrets_dir, self.ledger_path.parent]
        real_home = os.environ.get("HOME")
        if real_home:
            forbidden_roots.append(Path(real_home))
        for entry in self.bench_readonly_paths:
            for root in forbidden_roots:
                if _is_ancestor_or_equal(entry, root):
                    raise ValueError("bench_readonly_path_forbidden")

    @staticmethod
    def from_json(path: Path) -> "EngineConfig":
        """Load config from a JSON file. Raises ValueError on bad shape."""
        raw: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("config root must be an object")
        config = _from_dict(raw, path.parent)
        return replace(config, config_path=path.resolve())


# Every key from_json/​_from_dict understands. An unrecognized top-level
# key is a typo (e.g. "hook" instead of "hooks") that would otherwise
# silently load with the field's default — fail closed instead.
_KNOWN_TOP_LEVEL_KEYS = frozenset({
    "ledger_path", "home", "secrets_dir", "projects", "station_owners",
    "protected_paths", "hooks", "hook_timeout_sec", "approval_ttl_sec",
    "stations", "optional_stations", "workers", "station_terminal_allow",
    "worker_env_passthrough", "sandbox", "bench_home_writable",
    "bench_readonly_paths", "publisher", "parallel_review_bench",
})

_KNOWN_SANDBOX_MODES = frozenset({"none", "bwrap"})
_KNOWN_PUBLISHER_KINDS = frozenset({"fake", "shell"})

_KNOWN_PROJECT_KEYS = frozenset({
    "repo", "excludes", "artifact_paths", "test_cmd", "install_cmd",
    "manifest", "bench_cases", "test_timeout_sec", "copy_links",
    "copy_link_writable_subpaths", "bench_scope", "related_cmd", "release",
    "hidden_paths",
})

_KNOWN_RELEASE_KEYS = frozenset({"master_repo", "target"})


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
    return ReleaseConfig(master_repo=(base / master_repo).resolve(), target=target)


def _from_dict(raw: dict[str, object], base: Path) -> EngineConfig:
    unknown = set(raw) - _KNOWN_TOP_LEVEL_KEYS
    if unknown:
        raise ValueError(f"unknown config key(s): {sorted(unknown)}")
    projects = _parse_projects(raw, base)
    owners = _parse_station_owners(raw)
    return EngineConfig(
        ledger_path=(base / str(raw.get("ledger_path", "ledger.sqlite"))).resolve(),
        home=(base / str(raw.get("home", "home"))).resolve(),
        secrets_dir=(base / str(raw.get("secrets_dir", "secrets"))).resolve(),
        projects=projects,
        station_owners=owners,
        protected_paths=tuple(_str_list(raw.get("protected_paths"))),
        **_parse_optional_fields(raw, base),
    )


def _parse_projects(raw: dict[str, object], base: Path) -> dict[str, ProjectConfig]:
    projects_raw = raw.get("projects")
    if not isinstance(projects_raw, dict):
        raise ValueError("projects must be an object")
    projects: dict[str, ProjectConfig] = {}
    for name, item in projects_raw.items():
        if not isinstance(item, dict) or not isinstance(item.get("repo"), str):
            raise ValueError(f"project {name!r} needs a repo path")
        unknown = set(item) - _KNOWN_PROJECT_KEYS
        if unknown:
            raise ValueError(f"project {name!r}: unknown key(s): {sorted(unknown)}")
        manifest = item.get("manifest")
        bench_cases = item.get("bench_cases")
        if manifest is not None and not isinstance(manifest, str):
            raise ValueError(f"project {name!r}: manifest must be a string")
        if bench_cases is not None and not isinstance(bench_cases, str):
            raise ValueError(f"project {name!r}: bench_cases must be a string")
        timeout = item.get("test_timeout_sec", 300.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError(f"project {name!r}: test_timeout_sec must be a number")
        writable_subpaths_raw = item.get("copy_link_writable_subpaths", {})
        if not isinstance(writable_subpaths_raw, dict):
            raise ValueError(
                f"project {name!r}: copy_link_writable_subpaths must be an object"
            )
        writable_subpaths = {
            str(entry): tuple(_str_list(subpaths))
            for entry, subpaths in writable_subpaths_raw.items()
        }
        bench_scope = item.get("bench_scope", "related")
        if not isinstance(bench_scope, str):
            raise ValueError(f"project {name!r}: bench_scope must be a string")
        release_raw = item.get("release")
        release = (
            _parse_release(release_raw, base, name) if release_raw is not None else None
        )
        hidden_paths_raw = _str_list(item.get("hidden_paths"))
        for entry in hidden_paths_raw:
            if not entry.startswith("/"):
                raise ValueError(
                    f"project {name!r}: hidden_paths entry must be absolute: {entry!r}"
                )
        projects[name] = ProjectConfig(
            name=name,
            repo=(base / str(item["repo"])).resolve(),
            excludes=tuple(_str_list(item.get("excludes"))),
            artifact_paths=tuple(_str_list(item.get("artifact_paths"))),
            test_cmd=tuple(_str_list(item.get("test_cmd"))),
            install_cmd=tuple(_str_list(item.get("install_cmd"))),
            manifest=manifest,
            bench_cases=bench_cases,
            test_timeout_sec=float(timeout),
            copy_links=tuple(_str_list(item.get("copy_links"))),
            copy_link_writable_subpaths=writable_subpaths,
            bench_scope=bench_scope,
            related_cmd=tuple(_str_list(item.get("related_cmd"))),
            release=release,
            hidden_paths=tuple(Path(p) for p in hidden_paths_raw),
        )
    return projects


def _parse_workers(raw: dict[str, object]) -> dict[str, WorkerConfig]:
    workers_raw = raw.get("workers", {})
    if not isinstance(workers_raw, dict):
        raise ValueError("workers must be an object")
    workers: dict[str, WorkerConfig] = {}
    for role, item in workers_raw.items():
        if not isinstance(item, dict):
            raise ValueError(f"worker {role!r} must be an object")
        runner = item.get("runner")
        if runner not in _KNOWN_RUNNERS:
            raise ValueError(f"worker {role!r}: unknown runner {runner!r}")
        workers[role] = WorkerConfig(
            runner=str(runner),
            model=_opt_str(item.get("model")),
            max_turns=int(item.get("max_turns", 40)),
            budget_usd=float(item.get("budget_usd", 3.0)),
            timeout_sec=float(item.get("timeout_sec", 900.0)),
            reasoning=_opt_str(item.get("reasoning")),
        )
    return workers


def _parse_station_terminal_allow(
    raw: dict[str, object],
) -> dict[str, dict[str, tuple[str, ...]]]:
    table_raw = raw.get("station_terminal_allow", {})
    if not isinstance(table_raw, dict):
        raise ValueError("station_terminal_allow must be an object")
    table: dict[str, dict[str, tuple[str, ...]]] = {}
    for station, roles in table_raw.items():
        if not isinstance(roles, dict):
            raise ValueError(f"station_terminal_allow[{station!r}] must be an object")
        table[station] = {r: tuple(_str_list(p)) for r, p in roles.items()}
    return table


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("expected a string or null")
    return value


def _parse_station_owners(raw: dict[str, object]) -> dict[str, dict[str, tuple[str, ...]]]:
    owners_raw = raw.get("station_owners", {})
    if not isinstance(owners_raw, dict):
        raise ValueError("station_owners must be an object")
    owners: dict[str, dict[str, tuple[str, ...]]] = {}
    for station, roles in owners_raw.items():
        if not isinstance(roles, dict):
            raise ValueError(f"station {station!r} roles must be an object")
        owners[station] = {r: tuple(_str_list(p)) for r, p in roles.items()}
    return owners


def _parse_optional_fields(raw: dict[str, object], base: Path) -> dict[str, object]:
    """hooks/hook_timeout_sec/approval_ttl_sec/stations/optional_stations:
    only set a field if the key is present, so an absent key keeps the
    EngineConfig dataclass default rather than re-asserting it here."""
    fields: dict[str, object] = {}
    if "hooks" in raw:
        fields["hooks"] = tuple(
            (base / str(p)).resolve() for p in _str_list(raw.get("hooks"))
        )
    if "hook_timeout_sec" in raw:
        value = raw["hook_timeout_sec"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("hook_timeout_sec must be a number")
        fields["hook_timeout_sec"] = float(value)
    if "approval_ttl_sec" in raw:
        value = raw["approval_ttl_sec"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("approval_ttl_sec must be an integer")
        fields["approval_ttl_sec"] = value
    if "stations" in raw:
        fields["stations"] = tuple(_str_list(raw.get("stations")))
    if "optional_stations" in raw:
        fields["optional_stations"] = tuple(_str_list(raw.get("optional_stations")))
    if "workers" in raw:
        fields["workers"] = _parse_workers(raw)
    if "station_terminal_allow" in raw:
        fields["station_terminal_allow"] = _parse_station_terminal_allow(raw)
    if "worker_env_passthrough" in raw:
        fields["worker_env_passthrough"] = tuple(
            _str_list(raw.get("worker_env_passthrough")))
    if "sandbox" in raw:
        value = raw["sandbox"]
        if value not in _KNOWN_SANDBOX_MODES:
            raise ValueError(f"unknown sandbox mode: {value!r}")
        fields["sandbox"] = value
    if "bench_home_writable" in raw:
        value = raw["bench_home_writable"]
        if not isinstance(value, bool):
            raise ValueError("bench_home_writable must be a boolean")
        fields["bench_home_writable"] = value
    if "bench_readonly_paths" in raw:
        entries = _str_list(raw.get("bench_readonly_paths"))
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
        fields["bench_readonly_paths"] = tuple(paths)
    if "publisher" in raw:
        fields.update(_parse_publisher(raw["publisher"]))
    if "parallel_review_bench" in raw:
        value = raw["parallel_review_bench"]
        if not isinstance(value, bool):
            raise ValueError("parallel_review_bench must be a boolean")
        fields["parallel_review_bench"] = value
    return fields


_KNOWN_PUBLISHER_KEYS = frozenset({
    "kind", "argv", "dry_run_argv", "timeout_sec", "dry_run",
})


def _parse_publisher(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError("publisher must be an object")
    unknown = set(raw) - _KNOWN_PUBLISHER_KEYS
    if unknown:
        raise ValueError(f"publisher: unknown key(s): {sorted(unknown)}")
    fields: dict[str, object] = {}
    if "kind" in raw:
        kind = raw["kind"]
        if kind not in _KNOWN_PUBLISHER_KINDS:
            raise ValueError(f"unknown publisher kind: {kind!r}")
        fields["publisher_kind"] = kind
    if "argv" in raw:
        fields["publisher_argv"] = tuple(_str_list(raw.get("argv")))
    if "dry_run_argv" in raw:
        fields["publisher_dry_run_argv"] = tuple(_str_list(raw.get("dry_run_argv")))
    if "timeout_sec" in raw:
        value = raw["timeout_sec"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("publisher.timeout_sec must be a number")
        fields["publisher_timeout_sec"] = float(value)
    if "dry_run" in raw:
        value = raw["dry_run"]
        if not isinstance(value, bool):
            raise ValueError("publisher.dry_run must be a boolean")
        fields["publisher_dry_run"] = value
    return fields


def _str_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("expected a list of strings")
    return list(value)


def to_json_dict(config: EngineConfig) -> dict[str, object]:
    """The JSON shape from_json() reads, with absolute paths (joining an
    absolute path onto the file's directory yields that absolute path),
    so a config built in Python can be written to disk for a spawned
    worker's GB_CONFIG and loaded back identically (minus config_path)."""
    projects: dict[str, object] = {}
    for name, project in config.projects.items():
        projects[name] = {
            "repo": str(project.repo), "excludes": list(project.excludes),
            "artifact_paths": list(project.artifact_paths),
            "test_cmd": list(project.test_cmd), "install_cmd": list(project.install_cmd),
            "manifest": project.manifest, "bench_cases": project.bench_cases,
            "test_timeout_sec": project.test_timeout_sec,
            "copy_links": list(project.copy_links),
            "copy_link_writable_subpaths": {
                entry: list(subpaths)
                for entry, subpaths in project.copy_link_writable_subpaths.items()
            },
            "bench_scope": project.bench_scope,
            "related_cmd": list(project.related_cmd),
            "hidden_paths": [str(p) for p in project.hidden_paths],
        }
        if project.release is not None:
            projects[name]["release"] = {
                "master_repo": str(project.release.master_repo),
                "target": project.release.target,
            }
    workers = {
        role: {"runner": w.runner, "model": w.model, "max_turns": w.max_turns,
               "budget_usd": w.budget_usd, "timeout_sec": w.timeout_sec,
               "reasoning": w.reasoning}
        for role, w in config.workers.items()
    }
    return {
        "ledger_path": str(config.ledger_path), "home": str(config.home),
        "secrets_dir": str(config.secrets_dir), "projects": projects,
        "station_owners": {s: {r: list(p) for r, p in roles.items()}
                           for s, roles in config.station_owners.items()},
        "protected_paths": list(config.protected_paths),
        "hooks": [str(h) for h in config.hooks],
        "hook_timeout_sec": config.hook_timeout_sec,
        "approval_ttl_sec": config.approval_ttl_sec,
        "stations": list(config.stations),
        "optional_stations": list(config.optional_stations),
        "workers": workers,
        "station_terminal_allow": {s: {r: list(p) for r, p in roles.items()}
                                   for s, roles in config.station_terminal_allow.items()},
        "worker_env_passthrough": list(config.worker_env_passthrough),
        "sandbox": config.sandbox,
        "bench_home_writable": config.bench_home_writable,
        "bench_readonly_paths": [str(p) for p in config.bench_readonly_paths],
        "publisher": {
            "kind": config.publisher_kind, "argv": list(config.publisher_argv),
            "dry_run_argv": list(config.publisher_dry_run_argv),
            "timeout_sec": config.publisher_timeout_sec,
            "dry_run": config.publisher_dry_run,
        },
        "parallel_review_bench": config.parallel_review_bench,
    }


def write_json(config: EngineConfig, path: Path) -> EngineConfig:
    """Write to_json_dict(config) at `path` and load it back, so the
    returned config carries config_path (needed by gated workers)."""
    path.write_text(json.dumps(to_json_dict(config), ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return EngineConfig.from_json(path)


__all__ = ["EngineConfig", "ProjectConfig", "ReleaseConfig", "WorkerConfig",
           "to_json_dict", "write_json"]
