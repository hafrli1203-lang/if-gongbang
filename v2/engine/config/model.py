"""Engine configuration dataclasses: EngineConfig/ProjectConfig/
ContextConfig and their sibling value types, plus the validation each
one's own __post_init__ enforces. The JSON field tables that parse/
serialize these (one row per key, see engine.config.fields) and the
from_json/to_json_dict/write_json load/save functions (engine.
config_io) live in their own modules; engine.config re-exports
everything from all three so `from engine.config import X` keeps
working unchanged for every existing caller.

Nothing project-specific is hardcoded here. All project names, roles,
stations, allowed paths and protected paths come from an EngineConfig
built by the caller (tests build one from fixtures).
"""
from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass, field
from pathlib import Path

from engine.config.context_settings import ContextConfig

_RUNNER_KINDS = ("claude", "codex", "scripted")

_KNOWN_RUNNERS = frozenset({"claude", "codex", "scripted"})
_KNOWN_BENCH_SCOPES = frozenset({"related", "full"})
KNOWN_SANDBOX_MODES = frozenset({"none", "bwrap"})
KNOWN_PUBLISHER_KINDS = frozenset({"fake", "shell"})


def is_known_runner(name: object) -> bool:
    """claude, codex, scripted, or a named scripted variant ("scripted_<role>") so a
    test can give each role its own script; real configs use the first three."""
    return isinstance(name, str) and (
        name in _KNOWN_RUNNERS or (name.startswith("scripted_") and len(name) > len("scripted_")))


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
        if not is_known_runner(self.runner):
            raise ValueError(f"unknown worker runner: {self.runner!r}")


@dataclass(frozen=True)
class ReleaseConfig:
    """4단계 release track for one project (ENGINE.md "배포를 궤도 안에"):
    the original app repo this project's own repo fast-forwards into
    once an owner approves a publish request, and the deploy target
    name passed to the top-level `publisher` argv's "{target}"."""

    master_repo: Path
    target: str
    # Optional command (argv) run AFTER a real deploy succeeds, expected
    # to print (to stdout) the digest of what is actually live on the
    # deploy target now. When set, engine.application.steps.publish's own
    # call_publisher passes it through to the publisher, which compares
    # that printed digest against the artifact digest measured BEFORE
    # dispatch and fails the whole publish ("deploy_digest_mismatch")
    # on any difference -- see engine.infrastructure.publishers.ShellPublisher.publish.
    # Empty (default): nothing runs after deploy; the receipt/report
    # mark that publish "verified": False rather than refusing it --
    # a real deploy with no verify_cmd configured is not itself an
    # error, only one this engine cannot confirm on its own.
    verify_cmd: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.target:
            raise ValueError("release.target must be a non-empty string")


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
    # relative path of the bench cases JSON (engine.application.steps.suite_run)
    bench_cases: str | None = None
    # per-run timeout for test_cmd (full run and each trust-check case)
    test_timeout_sec: float = 300.0
    # bench's run_tests scope (engine.application.steps.suite_run): "related" (default)
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
    # One hint line engine.application.steps.worker_step appends to a builder's task
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
    # policy carve-out -- engine.domain.policy's worker-read-bounded-to-copy_root
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
    # (engine.application.steps.publish.steps() only inserts advance_master when
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
    # (e.g. a builder-run test file, which runs outside engine.domain.policy)
    # could read it directly by that absolute path even though `repo`
    # itself is safely isolated. Each entry must be an absolute path;
    # a nonexistent one is silently skipped at mount time, same as any
    # other `hidden` entry (engine.infrastructure.sandbox.wrap).
    hidden_paths: tuple[Path, ...] = ()
    # Absolute host paths (or "~/..." expanded with the ENGINE PROCESS's
    # own os.environ["HOME"]) naming folders of reference material (past
    # deliverables, style guides, benchmarking briefs, ...) an owner wants
    # a worker/reviewer for THIS project to be able to open and read
    # directly instead of paraphrasing from a prompt alone (team lead
    #: a worker sandbox hid these folders entirely).
    #
    # NEITHER directory-ness/`.env*` content NOR overlap with any
    # protected area is checked here, or anywhere in engine.config --
    # see engine.config.fields._project_reference_paths's own docstring
    # for why (a P1: a host-filesystem check at config-parse
    # time made a worker's gate hook, which re-parses the whole engine
    # config on every tool call, die with exit code 2 -- denying every
    # tool call for every OTHER project too -- whenever any one
    # project's reference folder was momentarily unreachable).
    #
    # The real checks run exactly once, in
    # engine.infrastructure.reference_snapshot.snapshot_references, right
    # before a worker/reviewer first needs this project's snapshot for an
    # attempt (not at config load, and not repeated per tool call): each
    # entry is realpath()'d and rejected if it overlaps (ancestor, equal,
    # OR descendant) config.home/secrets_dir/the ledger directory/ANY
    # project's repo/this engine's own source tree, or if it is the real
    # HOME itself or one of this project's own hidden_paths (descendants
    # of those two ARE allowed -- a reference folder commonly lives
    # somewhere under the operator's HOME), UNLESS some component of its
    # path is itself a dot-prefixed (hidden) folder, which is rejected
    # unconditionally. Only files that pass that are ever copied into a
    # read-only, engine-owned snapshot directory that the worker/reviewer
    # sandbox actually mounts -- the configured path here is NEVER itself
    # mounted or read by a worker/reviewer.
    #
    # A ProjectConfig built directly in Python (bypassing engine.config.
    # fields entirely) goes through that SAME snapshot_references call
    # and gets the SAME checks -- there is exactly one place these rules
    # are enforced, not a second, weaker one here.
    reference_paths: tuple[Path, ...] = ()
    # Bare CLI names (e.g. "claude") a probe script for THIS project may
    # itself spawn as a real CLI, during run_probe_scripts' own actual
    # re-execution of that script (engine.application.acceptance_probe --
    # never the worker/builder sandbox, which already mounts every
    # runner's own CLI unconditionally). The probe's bench sandbox
    # otherwise hides the real operator HOME wholesale and mounts no
    # CLI binary at all (unlike a worker), so a probe script that spawns
    # one (e.g. `claude -p ...`) fails there with a bare "command not
    # found" that proves nothing about whatever the CLI itself would
    # have done. Each name here gets the SAME narrow exposure a worker
    # gets (engine.application.steps.worker_sandbox.cli_runtime_mounts --
    # its own installed binary plus its minimal login files, never the
    # rest of the real HOME). Default empty: no project opts into this,
    # byte-identical to before this field existed.
    probe_runtime_clis: tuple[str, ...] = ()
    # (relspec, absolute_target) pairs for every package.json `file:`
    # dependency that points outside this project's repo (결함 17,
    #: a worktree-managed project nests one directory level
    # deeper than its real source checkout, so the SAME relative "../"
    # escape a copy/worktree/snapshot preserves byte-for-byte lands one
    # level short of the real target). `relspec` is the dependency's
    # own path exactly as written after `file:` (e.g. "../_shared/kit");
    # `target` is that path already resolved against the REAL source
    # checkout and validated (engine.application.steps.file_deps,
    # called once by `gb2 init` -- tools/gb2lib/workspace.py -- which is
    # the only place that still knows the real, un-mirrored location).
    # Deliberately NOT re-validated here (no filesystem I/O in
    # __post_init__, same reasoning as reference_paths' own deferred
    # checks): engine.application.steps.file_deps.remap_pairs_for_root
    # is the one place a later sandbox build actually uses this value,
    # and it already drops a pair whose target no longer exists.
    file_dep_targets: tuple[tuple[str, str], ...] = ()
    # argv for a structure-check command this project supplies on its
    # own (COMMON_ARCHITECTURE_STANDARD.md's `arch_check`, e.g.
    # ["python3", "tools/arch_check.py"]) -- the engine never ships or
    # hardcodes a path to one itself (public code: no particular
    # internal checkout belongs in here). Empty (default): bench
    # (engine.application.steps.suite_run) skips the structure check
    # entirely, unchanged from before this field existed. When set,
    # bench appends this attempt's repo path and "--json" itself and
    # runs it inside the same bench sandbox run_tests uses -- never a
    # second, differently-isolated subprocess call.
    arch_check_cmd: tuple[str, ...] = ()

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
        for pair in self.file_dep_targets:
            if len(pair) != 2:
                raise ValueError(f"file_dep_targets entry must be (relspec, target): {pair!r}")
            relspec, target = pair
            if not target.startswith("/"):
                raise ValueError(f"file_dep_targets target must be absolute: {target!r}")


@dataclass(frozen=True)
class McpServerConfig:
    """One stdio MCP server this engine may wire into a worker's own
    --mcp-config for a job whose plan.tools names it (engine.domain.plan.Tool
    kind="mcp") -- never handed to a worker directly; ClaudeRunner
    builds a per-request JSON containing only the servers that
    request's station/role is actually allowed (station_mcp_allow,
    engine.domain.policy's own mcp rule). `command` is the full argv (program
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
    # policy hook scripts (engine.infrastructure.policy_hooks.run) consulted after rules 1-6 allow
    # a non-read worker action; any deny/failed hook flips the decision
    hooks: tuple[Path, ...] = ()
    hook_timeout_sec: float = 2.0
    approval_ttl_sec: int = 4 * 3600
    # `gb2 watch --loop --resume`'s own anti-thrash limit (engine.application.
    # watch): at most this many "watch.resumed" events within any trailing
    # one-hour window for one app. A "stalled" app past the limit is left
    # alone (a "watch.gave_up" event is appended instead, and the verdict
    # stays "failed") rather than resumed forever against a cause watch
    # itself cannot fix -- see watch.py's own module docstring.
    watch_max_resumes_per_hour: int = 3
    stations: tuple[str, ...] = (
        "register", "facts", "probe", "plan", "design",
        "build", "review", "bench", "report",
    )
    optional_stations: tuple[str, ...] = ("design", "cross")
    # role -> worker launch settings (engine.infrastructure.workers)
    workers: dict[str, WorkerConfig] = field(default_factory=dict)
    # station -> role -> allowed terminal command regexes (re.fullmatch)
    station_terminal_allow: dict[str, dict[str, tuple[str, ...]]] = field(
        default_factory=dict)
    # environment variable names passed through to worker subprocesses
    worker_env_passthrough: tuple[str, ...] = ("PATH", "HOME", "LANG", "TERM")
    # OS sandbox for spawned child processes (engine.infrastructure.sandbox): "none"
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
    # Which Publisher engine.interface.cli._build_fakes wires in: "fake" (default,
    # FakePublisher, in-memory, used by every test) or "shell"
    # (engine.infrastructure.publishers.ShellPublisher, drives a real deploy script).
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
    # engine.application.task_pipeline speed default: when True (the default), a "review"
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
    # worker for a job whose plan.tools names it (engine.domain.plan.Tool
    # kind="mcp"). Empty (default): no project uses engine-granted MCP
    # access -- byte-identical to before this field existed.
    mcp_servers: dict[str, McpServerConfig] = field(default_factory=dict)
    # station -> the mcp_servers names that station may EVER be handed,
    # regardless of what a job's own plan.tools asks for -- the upper
    # bound engine.domain.policy's mcp rule checks a request's gate facts
    # against (the facts carry the actual per-request allow list, which
    # engine.application.pipeline computes as plan.tools(mcp, station) intersected
    # with this table -- see engine.domain.plan.tools_for_station). A station
    # absent from this table may never use any MCP server.
    station_mcp_allow: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # mcp server name -> the specific tool names (the <tool> of
    # mcp__<server>__<tool>) that server may ever be called with,
    # regardless of station_mcp_allow/the request's own gate facts.
    # Default deny (engine.domain.policy._decide_mcp): a server ABSENT from
    # this table denies every tool call for it -- granting a server to
    # a station (station_mcp_allow) is not itself permission to call
    # every tool that server exposes; every mcp_servers entry a
    # station may ever be granted needs its own mcp_tool_allow list
    # naming the specific tools it may call.
    mcp_tool_allow: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # The skill library's own index file (e.g. "~/.claude/skills-library/
    # INDEX.md") the planner prompt is given so it only ever asks for a
    # skill that is actually listed there (engine.domain.plan.
    # classify_skill_tools re-checks the same index at job-validation
    # time: a plan.tools skill entry NOT in it becomes "needs_install",
    # never granted to a worker, and reported as such in the handoff).
    # Expanded ("~" and resolved) at config load time, here, not
    # wherever it is later read from -- this process's own HOME, never
    # a worker's. None (default): no skill index configured, so every
    # plan.tools skill entry is needs_install.
    skill_index_path: Path | None = None
    # Optional context-injection settings (engine.infrastructure.context/engine.skills):
    # skill roots, per-station skill names, the company-library stdio
    # MCP server, per-project CURRENT_DECISIONS.md paths and lessons
    # app names. None (default): engine.infrastructure.context.gather/build inject
    # nothing, same as before this field existed.
    context: "ContextConfig | None" = None

    def __post_init__(self) -> None:
        if self.sandbox not in KNOWN_SANDBOX_MODES:
            raise ValueError(f"unknown sandbox mode: {self.sandbox!r}")
        if self.publisher_kind not in KNOWN_PUBLISHER_KINDS:
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
        # reference_paths is deliberately NOT validated here -- see
        # ProjectConfig.reference_paths's own docstring: the real checks
        # (overlap with secrets_dir, home, ledger, repos, hidden_paths, real
        # HOME, hidden path components) run once, in
        # engine.infrastructure.reference_snapshot.snapshot_references,
        # never at config load (a P1 fix -- a host-filesystem
        # check here made a worker's gate hook die re-parsing the whole
        # config on every tool call whenever any project's reference
        # folder was momentarily unreachable).
        # station_mcp_allow is an upper bound ON TOP OF mcp_servers, not
        # a second place to name a server -- a typo here would silently
        # grant a request nothing (engine.domain.policy still fails closed)
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
        # Lazy import: engine.config.io's own _from_dict builds an
        # EngineConfig by walking engine.config.fields' ENGINE_FIELDS
        # table, both of which import THIS module at their own top level
        # (they construct/annotate EngineConfig) -- importing config_io
        # at this module's own top would be circular. By the time this
        # method actually runs, both modules are fully loaded.
        from engine.config.io import load_engine_config

        return load_engine_config(path)


__all__ = [
    "WorkerConfig", "ReleaseConfig", "ProjectConfig", "McpServerConfig", "EngineConfig",
]
