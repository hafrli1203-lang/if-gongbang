"""Engine configuration. Nothing project-specific is hardcoded here.

All project names, roles, stations, allowed paths and protected paths come
from an EngineConfig built by the caller (tests build one from fixtures).

Every JSON key EngineConfig/ProjectConfig/ContextConfig understands is
declared exactly once, as one row in ENGINE_FIELDS/PROJECT_FIELDS/
CONTEXT_FIELDS below. Each row carries its own parse, default and
serialize together (see FieldSpec/ProjectFieldSpec/ContextFieldSpec) --
the "known keys" set used to reject typos, the JSON loader and
to_json_dict's writer all walk the SAME table, so a field that is only
ever parsed (and never serialized), or vice versa, cannot exist: there is
no second place to add one half and forget the other. (This replaced a
version where EngineConfig.context's required_skills/require_library
were added to the dataclass and to from_json's parsing but not to
to_json_dict, so no JSON config could actually set them and write_json
silently dropped them -- see tests/unit/test_config.py.)
"""
from __future__ import annotations

import json
import os
import posixpath
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from engine.context import ContextConfig, LibraryMcpConfig

_RUNNER_KINDS = ("claude", "codex", "scripted")


@dataclass(frozen=True)
class WorkerConfig:
    """One worker role's launch settings (AI CLI or scripted runner)."""

    runner: str  # "claude" | "codex" | "scripted"
    model: str | None = None
    max_turns: int | None = None  # None: no turn cap; timeout_sec still stops a hung worker
    budget_usd: float | None = None  # None: no per-run spend cap (subscription use)
    timeout_sec: float = 900.0
    reasoning: str | None = None  # codex: model_reasoning_effort

    def __post_init__(self) -> None:
        if not _is_known_runner(self.runner):
            raise ValueError(f"unknown worker runner: {self.runner!r}")


_KNOWN_RUNNERS = frozenset({"claude", "codex", "scripted"})


def _is_known_runner(name: object) -> bool:
    """claude, codex, scripted, or a named scripted variant ("scripted_<role>") so a
    test can give each role its own script; real configs use the first three."""
    return isinstance(name, str) and (
        name in _KNOWN_RUNNERS or (name.startswith("scripted_") and len(name) > len("scripted_")))
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
    # One hint line engine.steps.station appends to a builder's task
    # text verbatim (never parsed/templated -- plain config-authored
    # guidance), e.g. telling the builder to run related_cmd-style
    # tests while editing and the full test_cmd suite at most once at
    # the end. None (default): no hint line added, task text
    # unchanged from before this field existed.
    builder_test_hint: str | None = None
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


@dataclass(frozen=True)
class McpServerConfig:
    """One stdio MCP server this engine may wire into a worker's own
    --mcp-config for a job whose plan.tools names it (engine.plan.Tool
    kind="mcp") -- never handed to a worker directly; ClaudeRunner
    builds a per-request JSON containing only the servers that
    request's station/role is actually allowed (station_mcp_allow,
    engine.policy's own mcp rule). `command` is the full argv (program
    first, e.g. ["npx", "-y", "@acme/mcp-server"]); `args` are extra
    arguments appended after it for this engine's own use (kept
    separate from `command` so a shared base command can be reused
    with different trailing args, same shape plan §"mcp" asks for);
    `env_passthrough` is a list of environment variable NAMES this
    engine process's own os.environ supplies values for when building
    the server's own "env" entry -- the worker's env is never widened
    by this (the mcp server runs as the CLI's own child, not inside
    the worker's shell)."""

    command: tuple[str, ...]
    args: tuple[str, ...] = ()
    env_passthrough: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("mcp server command must be a non-empty list")


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
    # worker that relies on GB_CONFIG. Deliberately NOT one of
    # ENGINE_FIELDS below: it names the file this config came FROM, so it
    # is never itself a key inside that file.
    config_path: Path | None = None
    # name -> stdio MCP server launch info this engine may grant a
    # worker for a job whose plan.tools names it (engine.plan.Tool
    # kind="mcp"). Empty (default): no project uses engine-granted MCP
    # access -- byte-identical to before this field existed.
    mcp_servers: dict[str, McpServerConfig] = field(default_factory=dict)
    # station -> the mcp_servers names that station may EVER be handed,
    # regardless of what a job's own plan.tools asks for -- the upper
    # bound engine.policy's mcp rule checks a request's gate facts
    # against (the facts carry the actual per-request allow list, which
    # engine.full_orbit computes as plan.tools(mcp, station) intersected
    # with this table -- see engine.plan.tools_for_station). A station
    # absent from this table may never use any MCP server.
    station_mcp_allow: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # mcp server name -> the specific tool names (the <tool> of
    # mcp__<server>__<tool>) that server may ever be called with,
    # regardless of station_mcp_allow/the request's own gate facts.
    # Default deny (engine.policy._decide_mcp): a server ABSENT from
    # this table denies every tool call for it -- granting a server to
    # a station (station_mcp_allow) is not itself permission to call
    # every tool that server exposes; every mcp_servers entry a
    # station may ever be granted needs its own mcp_tool_allow list
    # naming the specific tools it may call.
    mcp_tool_allow: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # The skill library's own index file (e.g. "~/.claude/skills-library/
    # INDEX.md") the planner prompt is given so it only ever asks for a
    # skill that is actually listed there (engine.plan.
    # classify_skill_tools re-checks the same index at job-validation
    # time: a plan.tools skill entry NOT in it becomes "needs_install",
    # never granted to a worker, and reported as such in the handoff).
    # Expanded ("~" and resolved) at config load time, here, not
    # wherever it is later read from -- this process's own HOME, never
    # a worker's. None (default): no skill index configured, so every
    # plan.tools skill entry is needs_install.
    skill_index_path: Path | None = None
    # Optional context-injection settings (engine.context/engine.skills):
    # skill roots, per-station skill names, the company-library stdio
    # MCP server, per-project CURRENT_DECISIONS.md paths and lessons
    # app names. None (default): engine.context.gather/build inject
    # nothing, same as before this field existed.
    context: "ContextConfig | None" = None

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
        # station_mcp_allow is an upper bound ON TOP OF mcp_servers, not
        # a second place to name a server -- a typo here would silently
        # grant a request nothing (engine.policy still fails closed)
        # but a config that NAMES an unregistered server is a config
        # error, caught at load time rather than discovered later as a
        # request that mysteriously never gets its mcp server.
        for station, names in self.station_mcp_allow.items():
            for name in names:
                if name not in self.mcp_servers:
                    raise ValueError(
                        f"station_mcp_allow[{station!r}] names unknown mcp server: {name!r}"
                    )
        # mcp_tool_allow is keyed by server name, same reasoning as
        # station_mcp_allow above: a typo here would silently restrict
        # nothing (the server-level checks still fail closed) but a
        # key naming an unregistered server is a config error, caught
        # at load time.
        for server in self.mcp_tool_allow:
            if server not in self.mcp_servers:
                raise ValueError(f"mcp_tool_allow names unknown mcp server: {server!r}")

    @staticmethod
    def from_json(path: Path) -> "EngineConfig":
        """Load config from a JSON file. Raises ValueError on bad shape."""
        raw: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("config root must be an object")
        config = _from_dict(raw, path.parent)
        return replace(config, config_path=path.resolve())


_OMIT = object()  # serialize() sentinel: to_json_dict leaves this key out entirely


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
# EngineConfig and returns this key's JSON value, or _OMIT to leave the
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
                           if cfg.skill_index_path is not None else _OMIT)),
    FieldSpec("context",
              lambda v, base: ({} if v is None else {"context": _parse_context(v, base)}),
              lambda base: {},
              lambda cfg: _context_to_json(cfg.context)),
    # engine.app_security.check() reads "security" straight back out of the
    # raw JSON file at config.config_path itself (see that module's own
    # docstring for why -- it is read fresh per check() call, never cached
    # on EngineConfig). It is accepted here (so a config that sets it still
    # loads) but never parsed into a field and never written back by
    # to_json_dict (_OMIT) -- round-tripping a config through write_json
    # does not preserve a "security" block; only the original file has it.
    FieldSpec("security", lambda v, base: {}, lambda base: {}, lambda cfg: _OMIT),
)

_KNOWN_TOP_LEVEL_KEYS = frozenset(spec.key for spec in ENGINE_FIELDS)
_KNOWN_SANDBOX_MODES = frozenset({"none", "bwrap"})
_KNOWN_PUBLISHER_KINDS = frozenset({"fake", "shell"})


def _load_engine_fields(raw: dict[str, object], base: Path) -> dict[str, object]:
    unknown = set(raw) - _KNOWN_TOP_LEVEL_KEYS
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
                      lambda v: ({"master_repo": str(v.master_repo), "target": v.target}
                                 if v is not None else None)),
    ProjectFieldSpec("hidden_paths",
                      lambda v, base, name: _project_hidden_paths(v, name),
                      lambda base, name: (), lambda v: [str(p) for p in v]),
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
        if value is not _OMIT:
            out[spec.key] = value
    return out


def write_json(config: EngineConfig, path: Path) -> EngineConfig:
    """Write to_json_dict(config) at `path` and load it back, so the
    returned config carries config_path (needed by gated workers)."""
    path.write_text(json.dumps(to_json_dict(config), ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return EngineConfig.from_json(path)


__all__ = ["EngineConfig", "ProjectConfig", "ReleaseConfig", "WorkerConfig",
           "McpServerConfig", "to_json_dict", "write_json"]
