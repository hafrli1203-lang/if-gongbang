"""Always-on safety policy. Default deny. Evaluated in every project state.

Rules (first match wins) — see plan §4:
 0. project not in config.projects: denied for every tool/actor
    ("unregistered_project"), before any ledger lookup (fail-closed, no
    LedgerError escapes).
 1. read: allowed for owner/processor everywhere, except the same
    sensitive areas rule 4 denies workers (secrets_dir, the ledger file
    and its SQLite WAL/SHM/rollback-journal companion files, engine
    home's jobs/archive/copies areas other than the worker's own copy,
    .gitignore anywhere, protected_paths, .git/). For a WORKER: path=None
    is always denied ("read_requires_path") -- a worker is a real
    process that inherits the operator's HOME, so an unbounded read
    (e.g. Read with no file_path falling back to some other cwd) must
    never be allowed through by default; the sensitive-area denial above
    applies first (it also protects those areas from inside the worker's
    own copy); copy_root=None is denied ("no_copy_root"); otherwise the
    read is allowed only if realpath(path) is under realpath(copy_root)
    ("read_within_copy"), else denied ("read_outside_copy") -- a worker
    cannot read anything outside its own sandboxed copy, including the
    operator's ~/.claude, other projects, or the real project repo.
 2. worker write/terminal/mcp/browser/publish/install/commit: denied unless
    project state is OPEN.
 3. worker with generation < project generation: denied (stale worker).
 4. protected paths (ledger, secrets_dir, home/jobs, home/archive,
    home/copies (any copy other than the worker's own), .git/ (repo or
    copy), config files, hook dirs, .gitignore at any depth,
    config.protected_paths): denied for workers. Paths are resolved with
    os.path.realpath so symlinks cannot escape; a worker's write-target
    must also resolve to somewhere under its own copy_root, never outside
    it (a swapped-in symlink for a station prefix cannot escape the copy).
 5. write-like tools (write/browser with a path): allowed only if the
    resolved path is under one of config.station_owners[station][role]
    prefixes joined to the worker copy.
 5c. mcp (a tool named mcp__<server>__<tool>, engine.gate parses `server`
    into action.target, no path): no target -> denied
    ("mcp_requires_target"); `server` not in facts.mcp_allow (the
    per-request allow list, set by whoever writes gate_facts.json from
    engine.plan.tools_for_station) -> denied ("mcp_not_in_request_allow");
    `server` not in config.station_mcp_allow[station] (the project-wide
    upper bound) -> denied ("mcp_not_in_station_allow"); else the
    configured hooks are consulted same as rule 5, then allowed
    ("mcp_allowed"). A plan's own tool table can only narrow a
    station's mcp access, never widen it past station_mcp_allow.
 5b. worker terminal actions (a path-less tool with a command): command
    None/empty -> denied ("terminal_requires_command"); command containing
    any shell metacharacter (; & | ` $ newline > <, note: any "$", not
    just "$(" -- a bare variable expansion like $HOME is an escape too)
    -> denied ("shell_metachar"); command containing "~" -> denied
    ("home_reference"); command containing ".." -> denied
    ("path_traversal"); no station_terminal_allow[station][role] table, or
    an empty one -> denied ("terminal_not_authorized"); else allowed only
    if the command re.fullmatch's one of those patterns
    ("terminal_pattern"), else denied ("terminal_no_match"). This replaces
    the path-prefix check of rule 5 for the terminal tool only (terminal
    actions have no path).
    After rule 5/5b allow a non-read worker action, each configured
    config.hooks script is consulted (engine.hooks.run); any deny/failed
    hook flips the decision to denied.
 6. publish/install/commit: denied for workers always (processor only).
Every decision is appended as a 'policy.decided' event.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from engine.config import EngineConfig
from engine.ledger import Event, Ledger

TOOLS = ("read", "write", "terminal", "mcp", "browser", "publish", "install", "commit")
WRITE_LIKE = ("write", "terminal", "mcp", "browser")
WORKER_ONLY_FORBIDDEN = ("publish", "install", "commit")


@dataclass(frozen=True)
class Actor:
    kind: str  # owner | worker | processor
    role: str  # e.g. builder, reviewer, tester, planner, processor, owner
    attempt_id: str | None
    generation: int
    station: str | None = None
    copy_root: Path | None = None  # worker's own working copy


@dataclass(frozen=True)
class Action:
    tool: str
    path: Path | None = None
    target: str | None = None
    command: str | None = None


_SHELL_METACHARS = (";", "&", "|", "`", "$", "\n", ">", "<")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class ProjectFacts:
    """A snapshot of the ledger facts Policy needs about one project,
    taken by the engine process (which has real ledger access) and
    handed to a Policy instance that must decide WITHOUT any ledger
    access of its own -- the gate hook, run inside the worker sandbox,
    where the ledger directory is no longer writable (ENGINE.md
    「한계」). Written to a file (station.py's gate_facts.json) and read
    back by engine.gate; `registered`/`attempt_id`/`attempt_active`
    are carried for completeness/audit even where Policy's current
    rules do not consult them directly (only `state` and `generation`
    are read today -- rules 2 and 3).

    `owns`, when not None, is a per-task narrowing of this worker's
    configured station/role write prefixes (engine.full_orbit's build
    loop: a task's own `owns`, already checked a subset of the
    project's build prefixes by engine.plan.validate_plan before the
    job ever spawns a worker for it) -- see rule 5's own
    _decide_station_prefix for how it narrows the allowed prefix list,
    and engine.steps.station for how it reaches gate_facts.json."""
    project: str
    registered: bool
    state: str
    generation: int
    attempt_id: str | None
    attempt_active: bool
    owns: tuple[str, ...] | None = None
    # The mcp server NAMES this one request's run_station payload was
    # actually given (engine.plan.tools_for_station's "mcp" list for
    # this station, as resolved by whoever wrote gate_facts.json) --
    # NOT the config-wide mcp_servers table. _decide_mcp requires a
    # server be in BOTH this list and config.station_mcp_allow[station]
    # (the upper bound) before allowing an mcp__<server>__* tool call;
    # absent (default empty tuple) denies every mcp call for this
    # request, same fail-closed default as `owns=None` leaving write
    # prefixes unnarrowed is NOT this field's behavior -- there is no
    # "unnarrowed" mcp allowance, only an explicit list or none.
    mcp_allow: tuple[str, ...] = ()


class DecisionSink(Protocol):
    """Where Policy.check() records a 'policy.decided' event (or
    engine.gate records a 'gate.denied' one for an unrecognized tool).
    LedgerSink is the normal path (append to the real ledger);
    FileSink is for a process -- again, the gate hook -- that has no
    ledger access and instead appends one JSON line per decision to a
    plain file the engine imports afterward (station.record_station)."""

    def record(self, event: Event) -> None: ...


class LedgerSink:
    """The default sink: appends straight to the real ledger, exactly
    as Policy did before DecisionSink existed -- byte-identical
    behavior for every existing (ledger, no facts) caller."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def record(self, event: Event) -> None:
        self.ledger.append(event)


class FileSink:
    """Appends one JSON line per decision to `path` -- opened in append
    mode on every call (no held file handle, no fsync: this is an
    audit trail a later step imports into the real ledger, not the
    ledger itself, so losing the very last line to a crash is no
    worse than losing any other not-yet-imported worker output).
    Lives in a worker-writable directory (station.py's stations dir),
    so it is evidence worker-run code could in principle forge --
    documented plainly in ENGINE.md: this does not weaken enforcement
    (the hook's exit code, decided by this same process before the
    line is ever written, is what actually blocks a tool), only the
    AUDIT TRAIL's trustworthiness for an already-blocked or already-
    allowed action."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def record(self, event: Event) -> None:
        line = json.dumps({
            "ts": time.time(), "kind": event.kind, "project": event.project,
            "actor_kind": event.actor_kind, "actor_id": event.actor_id,
            "attempt_id": event.attempt_id, "payload": event.payload,
        }, ensure_ascii=False)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _is_under(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _matches_protected_relative(path: Path, base: Path, entries: tuple[str, ...]) -> bool:
    try:
        rel = path.relative_to(base)
    except ValueError:
        return False
    rel_str = rel.as_posix()
    for entry in entries:
        if entry.endswith("/"):
            stripped = entry.rstrip("/")
            if rel_str == stripped or rel_str.startswith(entry):
                return True
        else:
            if rel_str == entry or rel.name == entry:
                return True
    return False


def _is_ledger_companion(real: Path, ledger_path: Path) -> bool:
    """SQLite leaves companion files next to the main db file in WAL mode
    (ledger.sqlite-wal, ledger.sqlite-shm) or rollback-journal mode
    (ledger.sqlite-journal). Approval/evidence pages can live in those
    files too, so they must be as protected as the ledger file itself."""
    ledger_real = Path(os.path.realpath(ledger_path))
    return real.parent == ledger_real.parent and real.name.startswith(ledger_real.name)


def _normalize_rel(path: str) -> str:
    """Trim a trailing-slash prefix / leading-"./" the same way a
    directory prefix is compared elsewhere -- used only by
    _owns_within_prefixes' own string-level containment check, never
    for the resolved-path check _within_allowed_prefix already does."""
    stripped = path.rstrip("/")
    return stripped[2:] if stripped.startswith("./") else stripped


def _prefix_covers(prefix: str, entry: str) -> bool:
    e = _normalize_rel(entry)
    if prefix.endswith("/"):
        base = _normalize_rel(prefix)
        return e == base or e.startswith(base + "/")
    return e == _normalize_rel(prefix)


def _owns_within_prefixes(owns: tuple[str, ...], prefixes: tuple[str, ...]) -> bool:
    """True iff every entry of `owns` is at or under one of `prefixes`
    (same trailing-slash directory-vs-exact-file convention as
    EngineConfig.station_owners) -- a defense-in-depth re-check of
    what engine.plan.validate_plan already enforced at plan-approval
    time, done again here against a tampered or malformed gate facts
    file, never trusted a second time without re-verifying."""
    return all(any(_prefix_covers(p, entry) for p in prefixes) for entry in owns)


def _within_allowed_prefix(copy_root: Path, prefixes: tuple[str, ...], path: Path) -> bool:
    # copy_root itself is resolved (it is the trusted boundary), but the
    # prefix suffix is deliberately NOT resolved before joining: resolving
    # copy_root / prefix would follow a symlink planted at that prefix
    # (e.g. a worker replacing its copy's src/ with a symlink to the real
    # project repo) and compare against the escaped target instead of
    # catching the escape. The real defense is requiring resolved to still
    # be under the resolved copy_root below.
    copy_root_real = Path(os.path.realpath(copy_root))
    resolved = Path(os.path.realpath(path))
    if not (resolved == copy_root_real or _is_under(resolved, copy_root_real)):
        return False
    for prefix in prefixes:
        prefix_real = copy_root_real / prefix
        if prefix.endswith("/"):
            if resolved == prefix_real or _is_under(resolved, prefix_real):
                return True
        else:
            if resolved == prefix_real:
                return True
    return False


class Policy:
    def __init__(self, config: EngineConfig, ledger: Ledger | None = None, *,
                 facts: ProjectFacts | None = None,
                 sink: DecisionSink | None = None) -> None:
        """`ledger=<Ledger>, facts=None` (the normal engine-process path):
        every ledger read Policy makes (project state/generation) comes
        from `ledger` itself, and `sink` defaults to LedgerSink(ledger)
        -- byte-identical to this class before ProjectFacts/DecisionSink
        existed. `facts=<ProjectFacts>` (the gate-hook path, no ledger
        access at all): every such read comes from `facts` instead, and
        `sink` must be given explicitly (there is no ledger to default
        a LedgerSink from) -- raises ValueError if it is not."""
        self.config = config
        self.ledger = ledger
        self.facts = facts
        if sink is not None:
            self.sink = sink
        elif ledger is not None:
            self.sink = LedgerSink(ledger)
        else:
            raise ValueError("Policy needs a ledger or an explicit sink")

    def _project_state_and_generation(self, project: str) -> tuple[str, int]:
        if self.facts is not None:
            return self.facts.state, self.facts.generation
        assert self.ledger is not None  # guaranteed by __init__
        row = self.ledger.project(project)
        return row.state, row.generation

    def check(self, project: str, actor: Actor, action: Action) -> Decision:
        """Evaluate rules 1-6 and record the decision via self.sink."""
        decision = self._decide(project, actor, action)
        self.sink.record(Event(
            project=project, kind="policy.decided", actor_kind=actor.kind,
            actor_id=actor.role, attempt_id=actor.attempt_id,
            payload={
                "tool": action.tool,
                "path": str(action.path) if action.path is not None else None,
                "target": action.target,
                "command": action.command,
                "allowed": decision.allowed,
                "reason": decision.reason,
            },
        ))
        return decision

    def is_protected(self, project: str, path: Path) -> bool:
        """True if realpath(path) is inside any protected location.

        Actor-agnostic: this is the safe default used when a path cannot be
        attributed to the asking actor's own copy (see _protected_for_actor).
        home/"copies" is protected here as a whole — recognizing a worker's
        own copy as not-protected is _protected_for_actor's job, not this
        method's, since this method has no actor to compare against.
        .gitignore is always protected, at any depth, regardless of
        config.protected_paths. SQLite's WAL/SHM/rollback-journal
        companion files next to the ledger (ledger.sqlite-wal etc.) are
        always protected too — they carry approval/evidence pages exactly
        like the main ledger file.
        """
        real = Path(os.path.realpath(path))
        if real.name == ".gitignore":
            return True
        if _is_ledger_companion(real, self.config.ledger_path):
            return True
        protected_abs = [
            Path(os.path.realpath(self.config.ledger_path)),
            Path(os.path.realpath(self.config.secrets_dir)),
            Path(os.path.realpath(self.config.home / "jobs")),
            Path(os.path.realpath(self.config.home / "archive")),
            Path(os.path.realpath(self.config.home / "copies")),
        ]
        proj = self.config.projects.get(project)
        if proj is not None:
            protected_abs.append(Path(os.path.realpath(proj.repo / ".git")))
        for base in protected_abs:
            if real == base or _is_under(real, base):
                return True

        relative_bases = []
        if proj is not None:
            relative_bases.append(Path(os.path.realpath(proj.repo)))
        relative_bases.append(Path(os.path.realpath(self.config.home)))
        for base in relative_bases:
            if _matches_protected_relative(real, base, self.config.protected_paths):
                return True
        return False

    def _protected_for_actor(self, project: str, actor: Actor, path: Path) -> bool:
        """is_protected(), except a worker's own copy_root is carved out of
        the blanket home/"copies" protection (still protected within it:
        its own .git/ and any config.protected_paths entry, and .gitignore
        anywhere). A path under home/"copies" that is NOT this actor's own
        copy_root (another attempt's, or another role's) stays denied via
        is_protected()'s blanket rule."""
        real = Path(os.path.realpath(path))
        if real.name == ".gitignore":
            return True
        if _is_ledger_companion(real, self.config.ledger_path):
            return True
        if actor.copy_root is not None:
            copy_root_real = Path(os.path.realpath(actor.copy_root))
            if real == copy_root_real or _is_under(real, copy_root_real):
                copy_git = copy_root_real / ".git"
                if real == copy_git or _is_under(real, copy_git):
                    return True
                if _matches_protected_relative(real, copy_root_real, self.config.protected_paths):
                    return True
                return False
        return self.is_protected(project, path)

    def _run_hooks(self, project: str, actor: Actor, action: Action) -> Decision | None:
        """Consult config.hooks in order. Returns a denying Decision if any
        hook denies/fails, else None (no objection). Local import to avoid
        a circular import with engine.hooks (which imports Decision)."""
        if not self.config.hooks:
            return None
        from engine.hooks import run as run_hook

        payload = {
            "project": project,
            "tool": action.tool,
            "path": str(action.path) if action.path is not None else None,
            "target": action.target,
            "role": actor.role,
            "attempt_id": actor.attempt_id,
        }
        for script in self.config.hooks:
            decision = run_hook(script, payload, self.config.hook_timeout_sec)
            if not decision.allowed:
                return Decision(False, f"hook:{decision.reason}")
        return None

    def _decide(self, project: str, actor: Actor, action: Action) -> Decision:
        # Rule 0: project must be registered in config. Checked first, for
        # every tool and actor, so a bad project name never reaches
        # self.ledger.project() (which would raise LedgerError).
        if project not in self.config.projects:
            return Decision(False, "unregistered_project")

        # Rule 1: read.
        if action.tool == "read":
            return self._decide_read(project, actor, action)

        # Owner and processor: allowed for everything at stage 1, still logged.
        if actor.kind in ("owner", "processor"):
            return Decision(True, f"{actor.kind}_allowed")

        # From here: actor.kind == "worker", non-read tool.
        return self._decide_worker_write(project, actor, action)

    def _decide_read(self, project: str, actor: Actor, action: Action) -> Decision:
        if actor.kind != "worker":
            return Decision(True, "read_allowed")

        # Worker read is bounded to its own copy (see module docstring
        # rule 1): a worker is a real process that inherits the
        # operator's HOME, so a path-less read or a read of anything
        # outside its sandboxed copy must never be allowed by default.
        if action.path is None:
            return Decision(False, "read_requires_path")
        if self._protected_for_actor(project, actor, action.path):
            return Decision(False, "worker_read_denied_sensitive_area")
        if actor.copy_root is None:
            return Decision(False, "no_copy_root")
        real = Path(os.path.realpath(action.path))
        copy_root_real = Path(os.path.realpath(actor.copy_root))
        if real == copy_root_real or _is_under(real, copy_root_real):
            return Decision(True, "read_within_copy")
        return Decision(False, "read_outside_copy")

    def _decide_worker_write(self, project: str, actor: Actor, action: Action) -> Decision:
        """Rules 2, 3, 6, 4: everything that can deny a worker's non-read
        tool before we even get to checking the station/role prefix."""
        # Rule 2: project must be OPEN.
        project_state, project_generation = self._project_state_and_generation(project)
        if project_state != "OPEN":
            return Decision(False, f"project_not_open:{project_state}")

        # Rule 3: stale worker generation.
        if actor.generation < project_generation:
            return Decision(False, "stale_generation")

        # Rule 6: publish/install/commit are always denied for workers.
        if action.tool in WORKER_ONLY_FORBIDDEN:
            return Decision(False, "worker_forbidden_tool")
        if action.tool not in WRITE_LIKE:
            return Decision(False, "unknown_tool")

        # Rule 5b: terminal has no path, so it skips rule 4's path check
        # entirely and is decided by command/pattern instead.
        if action.tool == "terminal":
            return self._decide_terminal(project, actor, action)

        # mcp has no path either (action.target carries the server
        # name) -- decided by the request's own gate-facts allow list
        # intersected with the station's configured upper bound,
        # instead of rule 4/5's path-prefix check.
        if action.tool == "mcp":
            return self._decide_mcp(project, actor, action)

        # Rule 4: protected paths.
        if action.path is None:
            return Decision(False, "write_like_requires_path")
        if self._protected_for_actor(project, actor, action.path):
            return Decision(False, "protected_path")

        return self._decide_station_prefix(project, actor, action)

    def _decide_terminal(self, project: str, actor: Actor, action: Action) -> Decision:
        """Rule 5b: worker terminal commands. See module docstring."""
        command = action.command
        if not command:
            return Decision(False, "terminal_requires_command")
        if any(ch in command for ch in _SHELL_METACHARS):
            return Decision(False, "shell_metachar")
        if "~" in command:
            return Decision(False, "home_reference")
        if ".." in command:
            return Decision(False, "path_traversal")
        patterns = self.config.station_terminal_allow.get(
            actor.station or "", {}).get(actor.role)
        if not patterns:
            return Decision(False, "terminal_not_authorized")
        matched = any(re.fullmatch(pattern, command) for pattern in patterns)
        if not matched:
            return Decision(False, "terminal_no_match")

        hook_denial = self._run_hooks(project, actor, action)
        if hook_denial is not None:
            return hook_denial
        return Decision(True, "terminal_pattern")

    def _decide_mcp(self, project: str, actor: Actor, action: Action) -> Decision:
        """A tool named mcp__<server>__<tool> (engine.gate parses the
        server name into action.target). Allowed only if `server` is
        in BOTH the per-request allow list (self.facts.mcp_allow --
        None when this Policy is built straight from a ledger, no
        facts snapshot at all, which fails closed the same as an empty
        tuple) AND config.station_mcp_allow[actor.station] (the
        project-wide upper bound on what that station may ever be
        handed) -- a plan's own tool table can only narrow a station's
        access, never widen it past what the operator configured."""
        server = action.target
        if not server:
            return Decision(False, "mcp_requires_target")
        request_allow = self.facts.mcp_allow if self.facts is not None else ()
        if server not in (request_allow or ()):
            return Decision(False, "mcp_not_in_request_allow")
        station_allow = self.config.station_mcp_allow.get(actor.station or "", ())
        if server not in station_allow:
            return Decision(False, "mcp_not_in_station_allow")

        hook_denial = self._run_hooks(project, actor, action)
        if hook_denial is not None:
            return hook_denial
        return Decision(True, "mcp_allowed")

    def _decide_station_prefix(self, project: str, actor: Actor, action: Action) -> Decision:
        """Rule 5: within the actor's station/role allowed prefixes, plus
        the hook consultation that follows an otherwise-allowed write.

        If this decision is being made from a ProjectFacts snapshot
        carrying a per-task `owns` (engine.full_orbit's build loop --
        see ProjectFacts' own docstring), the configured prefixes are
        narrowed to `owns` for this check: a builder may then only
        write under its OWN task's owns, never the full set of paths
        every task in the plan is collectively allowed to touch.
        `owns` escaping the configured prefixes (it should never, by
        construction -- engine.plan.validate_plan already refused
        such a plan, and engine.steps.station re-checks before ever
        spawning the worker) is denied outright rather than silently
        falling back to the wider, un-narrowed prefix list."""
        station_table = self.config.station_owners.get(actor.station or "", {})
        prefixes = station_table.get(actor.role)
        if not prefixes:
            return Decision(False, "station_role_not_authorized")
        if actor.copy_root is None:
            return Decision(False, "no_copy_root")
        if self.facts is not None and self.facts.owns is not None:
            owns = tuple(self.facts.owns)
            if not owns or not _owns_within_prefixes(owns, prefixes):
                return Decision(False, "owns_escapes_station_prefixes")
            prefixes = owns
        if not _within_allowed_prefix(actor.copy_root, prefixes, action.path):
            return Decision(False, "outside_allowed_path")

        hook_denial = self._run_hooks(project, actor, action)
        if hook_denial is not None:
            return hook_denial
        return Decision(True, "within_allowed_path")
