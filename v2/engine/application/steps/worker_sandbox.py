"""The sandbox mount policy for a station worker: which paths are
writable, read-only, hidden or symlinked inside the bwrap sandbox a
worker CLI (claude/codex/scripted) actually runs in. Split out of
engine.application.steps.worker_step, which still owns spawning/awaiting/recording the
worker itself and calls `sandboxed_argv` below to wrap its argv.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from engine.domain import ids
from engine.infrastructure import sandbox
from engine.infrastructure.reference_snapshot import SnapshotEntry, ensure_snapshot_for_attempt
from engine.infrastructure.workers.claude_cli import ClaudeRunner
from engine.infrastructure.workers.codex_cli import CodexRunner
from engine.domain.records import Event
from engine.application.processor import StepContext, StepFailed
from engine.application.steps import file_deps
from engine.application.steps import worker_shell_jail
from engine.application.steps.worker_paths import stations_dir

if TYPE_CHECKING:
    from engine.infrastructure.workers.base import WorkerSpec


ENGINE_ROOT = Path(__file__).resolve().parents[3]
# Where the claude CLI keeps the shell snapshot each command sources
# (observed on this host: `source ~/.claude/shell-snapshots/<file>`).
CLI_SHELL_SNAPSHOT_SUBDIR = Path(".claude") / "shell-snapshots"


def _other_project_repos(ctx: StepContext) -> tuple[Path, ...]:
    return tuple(p.repo for name, p in ctx.config.projects.items()
                 if name != ctx.project)


def _repo(ctx: StepContext) -> Path:
    return ctx.config.projects[ctx.project].repo


def _is_within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _hidden_roots(ctx: StepContext, repo: Path) -> tuple[Path, ...]:
    # hidden_paths (ProjectConfig): operator-named paths outside `repo`
    # itself that must stay invisible too -- e.g. the true original
    # source checkout a generated `repo` worktree was cloned from,
    # which the sandbox's own whole-tree "--ro-bind / /" base would
    # otherwise still expose read-only by its real absolute path.
    # secrets_dir here already covers the office-approval HMAC key
    # (engine.application.office_approval.resolve_key_path -- a FIXED
    # filename inside secrets_dir, never an operator-configurable path
    # elsewhere; see that module's own docstring) -- no separate entry
    # needed for it.
    return ((ctx.config.home, ctx.config.secrets_dir, repo)
            + _other_project_repos(ctx)
            + ctx.config.projects[ctx.project].hidden_paths)


def _linked_entries_for_role(ctx: StepContext, role: str) -> set[str]:
    """copy_links entries THIS role's copy actually got a symlink for,
    per job.opened.copy_links_skipped -- never the configured list
    itself, which can include an entry open_job._make_copy refused to
    link (missing source, or an unsafe shape: a source escaping the
    repo, or a destination parent already a symlink -- see
    open_job._link_copy_link_entry). A skipped entry is excluded here
    even though this function cannot see WHY it was skipped; the
    separate re-validation below (_safe_copy_link_mounts) is what
    actually checks the shape at mount time."""
    configured = set(ctx.config.projects[ctx.project].copy_links)
    if not configured:
        return set()
    skipped: set[str] = set()
    for _seq, event in ctx.ledger.events(ctx.project, "job.opened"):
        if event.payload.get("attempt_id") != ctx.attempt_id:
            continue
        for row in event.payload.get("copy_links_skipped", []):
            if isinstance(row, dict) and row.get("role") == role:
                skipped.add(str(row.get("entry")))
        break  # exactly one job.opened event per attempt_id
    return configured - skipped


def _journal_copy_link_mount_skipped(ctx: StepContext, entry: str, reason: str) -> None:
    ctx.ledger.append(Event(
        project=ctx.project, kind="copy_link.mount_skipped",
        actor_kind="processor", actor_id="processor", attempt_id=ctx.attempt_id,
        payload={"request_id": ctx.request.request_id, "entry": entry, "reason": reason},
    ))


def _safe_copy_link_mounts(ctx: StepContext, spec: WorkerSpec,
                           repo: Path) -> tuple[tuple[str, Path], ...]:
    """(entry, repo/<entry>) pairs safe to re-expose read-only in this
    role's sandbox -- only for entries _make_copy actually linked (see
    _linked_entries_for_role), AND only if repo/<entry>'s realpath,
    RE-CHECKED here (never trusted from config-load or copy-creation
    time alone), stays inside the repo and outside every hidden root
    (secrets_dir, config.home, the ledger directory, every other
    project's repo). This re-check matters on its own: with
    copy_links=["deps"] and repo/deps a symlink to secrets_dir,
    _make_copy correctly refuses to create that symlink in the copy,
    but mounting the configured `repo/deps` read-only regardless would
    still re-expose secrets_dir itself, entirely independent of
    whether the copy's own symlink existed. An entry failing either check is
    skipped (never mounted) and journaled as copy_link_mount_skipped,
    not silently trusted a second time."""
    linked = _linked_entries_for_role(ctx, spec.role)
    if not linked:
        return ()
    repo_real = Path(os.path.realpath(repo))
    # The repo itself is hidden in the worker sandbox, but repo/<entry> is
    # exactly what this function re-exposes read-only -- so the repo must
    # not be in the "forbidden" roots here (the within-repo check above
    # already requires the target to be inside it). The ledger directory
    # is a writable root, not a hidden one, and in ordinary layouts it is
    # an ancestor of the repo; it must not be in this list either, or a
    # legitimate copy_links entry (e.g. node_modules) would be skipped
    # for that reason instead.
    forbidden = (ctx.config.home, ctx.config.secrets_dir) + _other_project_repos(ctx)
    hidden_roots = tuple(Path(os.path.realpath(root)) for root in forbidden)
    mounts: list[tuple[str, Path]] = []
    for entry in sorted(linked):
        target = repo / entry
        if not target.exists():
            continue
        target_real = Path(os.path.realpath(target))
        if target_real != repo_real and not _is_within(target_real, repo_real):
            _journal_copy_link_mount_skipped(ctx, entry, "copy_link_mount_skipped")
            continue
        if any(target_real == root or _is_within(target_real, root)
               for root in hidden_roots):
            _journal_copy_link_mount_skipped(ctx, entry, "copy_link_mount_skipped")
            continue
        mounts.append((entry, target))
    return tuple(mounts)


def _ledger_hidden_root(ctx: StepContext) -> tuple[Path, ...]:
    """The WHOLE directory holding the ledger file -- not just the
    ledger file itself plus its known SQLite WAL/SHM/rollback-journal
    companions by name. Masking those four names individually leaves a
    gap for anything else that lives alongside them: a future SQLite
    companion file this list does not know about, a hard link or bind
    alias to the same inode under a different name, or simply another
    file in that directory a worker was never meant to see either.
    Hiding the directory itself (a tmpfs, then remounted read-only --
    see engine.infrastructure.sandbox.wrap) closes all of those at once, the same way
    engine.application.steps.suite_run already hides its own ledger directory
    wholesale. Any deeper path that still needs to be visible or
    writable under it (there is none today for the worker/reviewer
    sandboxes; `config.home`/`stations_dir`/`gate-ro` all live under a
    SEPARATE root) would simply be re-exposed by its own deeper mount,
    same as every other hidden root here -- never by excepting
    specific filenames out of this one."""
    return (ctx.config.ledger_path.parent,)


CLI_LOGIN_FILES: dict[str, tuple[str, ...]] = {
    # The SMALLEST set of real-HOME-relative files each real CLI needs
    # copied into its own fresh, per-attempt worker HOME (see
    # _worker_home_dir/sandbox.prepare_worker_home below) to actually
    # authenticate -- named explicitly by the team lead's own
    # decision, not guessed: everything ELSE under the real HOME (other
    # apps, other CLIs' own credentials, ssh keys, ...) must stay
    # invisible to a worker and to any code it runs, which a wholesale
    # readonly mount of the real HOME (this module's previous approach)
    # did not achieve. Keyed by the CLI's own bare executable name
    # (argv[0]/PATH lookup name, e.g. "claude"/"codex") -- the single
    # source of truth RUNNER_LOGIN_FILES below and cli_runtime_mounts
    # (engine.application.acceptance_probe's own, separately configured
    # probe_runtime_clis) both reuse, rather than each keeping its own
    # copy of the same literal file lists. A CLI absent from this table
    # (e.g. a scripted test double) needs none.
    "claude": (".claude/.credentials.json", ".claude.json"),
    "codex": (".codex/auth.json",),
}

RUNNER_LOGIN_FILES: dict[str, tuple[str, ...]] = {
    # Runner CLASS NAME -> the same CLI_LOGIN_FILES entry for that
    # runner's own CLI -- kept as its own table since _login_files_for
    # is looked up from a runner INSTANCE (type(runner).__name__), not
    # a bare CLI name. A runner class absent from this table (e.g.
    # ScriptedRunner, a test double) needs none.
    "ClaudeRunner": CLI_LOGIN_FILES["claude"],
    "CodexRunner": CLI_LOGIN_FILES["codex"],
}


def _login_files_for(runner: object) -> tuple[str, ...]:
    return RUNNER_LOGIN_FILES.get(type(runner).__name__, ())


# Bare CLI name (same keys as CLI_LOGIN_FILES) -> that CLI's own
# runner's home_writable_subpaths (ClaudeRunner.home_writable_subpaths,
# CodexRunner.home_writable_subpaths) -- read straight off the runner
# classes rather than re-typed here, so this table can never drift from
# what sandboxed_argv already grants a REAL worker running that same
# CLI (결함 20,: cli_runtime_mounts used to pass `()` for
# writable_subpaths unconditionally, so a probe script's own `codex
# exec` -- cli_runtime_mounts is run_in_bench_sandbox's only mount
# policy for a probe_runtime_clis entry -- got its disposable probe
# $HOME mounted wholesale read-only with no ".codex" writable inside
# it, and failed "Read-only file system (os error 30)" trying to
# initialize its own app-server client there; a real worker never hits
# this because sandboxed_argv's own writable tuple already includes
# `home_dir / sub for sub in subpaths`).
CLI_HOME_WRITABLE_SUBPATHS: dict[str, tuple[str, ...]] = {
    "claude": ClaudeRunner.home_writable_subpaths,
    "codex": CodexRunner.home_writable_subpaths,
}


def cli_runtime_mounts(
    cli_name: str, home_dir: Path, real_home: Path | None,
) -> tuple[tuple[Path, ...], tuple[tuple[Path, Path], ...], tuple[Path, ...]]:
    """Public helper: the SAME CLI-visibility policy `sandboxed_argv`
    below applies for a real worker runner -- the CLI's own installed
    binary mounted via sandbox.resolve_runtime, plus its minimal
    login-file set (CLI_LOGIN_FILES) copied into `home_dir` via
    sandbox.prepare_worker_home -- generalized here to a bare CLI name
    (e.g. "claude") instead of a runner instance, so a caller that has
    no WorkerSpec/runner at all (engine.application.acceptance_probe, for a
    probe script that itself spawns a real CLI, per that project's own
    configured probe_runtime_clis) gets the IDENTICAL narrow exposure
    a worker gets -- never a second, divergent reimplementation of it.

    Returns (readonly, symlinks, writable) -- the three tuples a
    caller should add to its own sandbox.wrap()-bound readonly/
    symlinks/writable tuples (`+=`; see sandbox.resolve_runtime's own
    docstring for what each readonly/symlinks mount is for). `home_dir`
    is appended to the returned `readonly` tuple so a caller does not
    also have to remember to re-add it (home_dir itself is created, if
    missing, and seeded with login files as a side effect of this
    call, via sandbox.prepare_worker_home). The returned `writable`
    tuple is `home_dir / sub for sub in CLI_HOME_WRITABLE_SUBPATHS[
    cli_name]` -- the SAME per-CLI subpaths (sandbox.wrap's own "a
    deeper mount always wins" rule, identical to sandboxed_argv's own
    `writable += tuple(home_dir / sub for sub in subpaths)`) a real
    worker running this CLI already gets writable under ITS OWN fresh
    home; a caller that skips mounting this writable (결함 20) leaves
    `home_dir` read-only everywhere a CLI like `codex` needs to write
    its own session/credential state (e.g. ".codex/..."), which fails
    closed as a sandbox error, not as anything the CLI itself did
    wrong. An unrecognized `cli_name` (not in CLI_LOGIN_FILES/
    CLI_HOME_WRITABLE_SUBPATHS) still gets its binary mounted (if found
    on PATH) but no login files copied and no writable subpath --
    same "needs none" shape as a runner class absent from
    RUNNER_LOGIN_FILES."""
    login_files = CLI_LOGIN_FILES.get(cli_name, ())
    subpaths = CLI_HOME_WRITABLE_SUBPATHS.get(cli_name, ())
    sandbox.prepare_worker_home(home_dir, real_home, login_files, subpaths)
    runtime = sandbox.resolve_runtime(cli_name)
    writable = tuple(home_dir / sub for sub in subpaths)
    return runtime.readonly + (home_dir,), runtime.symlinks, writable


def _worker_home_dir(ctx: StepContext, spec: WorkerSpec) -> Path:
    """A fresh, per-attempt-and-station $HOME for this worker's real
    CLI process -- never the real operator HOME (see sandbox.
    prepare_worker_home's own docstring for why). Lives under the
    engine's own home, alongside spec.copy_root/stations_dir, so it is
    a descendant of the `hidden` config.home entry and gets re-exposed
    the same way those already are (a deeper mount always wins -- see
    engine.infrastructure.sandbox.wrap's module docstring). Keyed by station AND role
    (not just attempt_id): engine.config.parallel_review_bench can run
    a builder and a reviewer for the SAME attempt_id concurrently, each
    needing its own isolated CLI identity directory."""
    return (ctx.config.home / "jobs" / ids.attempt_dirname(ctx.attempt_id)
            / "worker-home" / spec.station / spec.role)


def sandboxed_argv(ctx: StepContext, spec: WorkerSpec, argv: list[str],
                    env: dict[str, str], runner: object) -> list[str]:
    """Builder sandbox policy: writable = the worker's own copy, the
    stations dir (where this request's stdout/result files land, and
    where the gate hook's own decision LOG lives -- worker-writable,
    hence worker-forgeable; see engine.interface.hook_gate/station.record_station),
    and -- only if the real HOME exists -- the
    runner's own `home_writable_subpaths` under THIS WORKER'S OWN
    fresh home directory (_worker_home_dir), never under the real HOME
    (e.g. ClaudeRunner's ".claude"/".claude.json", CodexRunner's
    ".codex": the only state a real CLI needs to write for its own
    credentials/session). readonly = _worker_home_dir(ctx, spec) itself
    (mounted as $HOME; sandbox.prepare_worker_home seeds it, before
    this mount, with ONLY the real CLI's own login files named in
    RUNNER_LOGIN_FILES -- see that function's docstring for why this
    replaces the previous wholesale readonly mount of the real HOME);
    repo/<entry> for every copy_links
    entry _safe_copy_link_mounts validates as actually linked AND
    safe to re-expose (the symlink target a Bash-run test_cmd/
    install_cmd *inside the copy* needs to resolve -- see
    ProjectConfig.copy_links; a worker's own Read/Glob/Grep is still
    bounded to copy_root by engine.domain.policy's realpath rule regardless of
    this read-only re-exposure); PLUS, for a copy_links entry with
    configured copy_link_writable_subpaths (e.g. node_modules needing
    .vite-temp/.vite writable despite being otherwise read-only),
    copy_root/<entry> itself -- open_job._link_copy_link_entry built
    THAT entry as a real directory inside the copy (per-child
    symlinks to the repo, plus a real subdirectory per configured
    writable subpath), which `spec.copy_root` being writable would
    otherwise leave fully writable (ordinary new files included, not
    just the configured subpaths); this deeper read-only mount locks
    that back down. writable ALSO then re-adds copy_root/<entry>/
    <subpath> for each configured subpath -- deeper still, so it wins
    back over that read-only lock; both subpaths already exist on
    disk (open_job created them), so bwrap never needs to create a
    mountpoint under an already read-only bind (verified impossible --
    the real repo's node_modules could not be used this way, which is
    exactly why open_job builds a real directory in the COPY instead).
    readonly also includes the gate-ro DIRECTORY (spec.gate_facts_path.
    parent, if this runner needs_gate -- the ProjectFacts snapshot plus
    a copy of the engine config, see station_gate.gate_ro_dir/
    write_gate_facts) -- a directory ro-bind, a SIBLING of the stations
    dir (never nested inside it), under the same hidden root, so the
    worker can read it (the gate hook does) but never alter, rename or
    replace it -- see station_paths.gate_ro_dir's own docstring for why
    this has to be a separate directory rather than a file inside the
    writable stations dir. hidden = engine home (minus the worker's own
    copy/stations/worker-home dirs, re-added by the writable/readonly
    binds above/below), secrets_dir, THIS project's own repo (so e.g.
    a gitignored .env there is invisible to code the worker runs --
    the worker only ever touches its own copy, never the repo itself),
    every OTHER project's repo, PLUS the WHOLE directory holding the
    ledger file (see _ledger_hidden_root -- not merely the ledger file
    and its -wal/-shm/-journal companions by name, which would leave
    any other file in that directory, present or future, unmasked),
    PLUS the real operator HOME outright (read from THIS process's own
    os.environ, via `env.get("HOME")` -- `env` here is the runner's
    OWN env dict, built by engine.infrastructure.workers.base.worker_env from
    config.worker_env_passthrough, which only ever widens that set
    from THIS trusted engine process's os.environ, never from
    anything worker/case-controlled). net = True (the claude/codex CLI
    needs it). `env["HOME"]` itself is overwritten in place, here, to
    _worker_home_dir(ctx, spec) -- the SAME dict object the caller
    (station._run_spawn_worker) goes on to pass to sandbox.exec_env for
    the actual Popen env, so the real HOME value never reaches the
    spawned process even under sandbox_mode="none" (no bwrap mount
    involved at all there, so hiding it in the mount list would do
    nothing by itself)."""
    repo = _repo(ctx)
    home_dir = _worker_home_dir(ctx, spec)
    home_env = env.get("HOME")
    real_home = Path(home_env) if home_env else None
    login_files = _login_files_for(runner)
    subpaths = getattr(runner, "home_writable_subpaths", ())
    sandbox.prepare_worker_home(home_dir, real_home, login_files, subpaths)
    env["HOME"] = str(home_dir)
    writable = (spec.copy_root, stations_dir(ctx))
    safe_copy_link_mounts = _safe_copy_link_mounts(ctx, spec, repo)
    # reference_paths (ProjectConfig): NEVER mounted/read directly -- only
    # the engine-owned, read-only SNAPSHOT (engine.infrastructure.
    # reference_snapshot) a worker may open (facts/plan/build all hint at
    # this material by path -- see engine.infrastructure.context's own
    # "참고 자료 폴더" prompt item). Re-exposed the same way every other
    # deeper-than-hidden readonly mount here is: sandbox.wrap sorts all
    # mounts by path depth, so this wins over the real-HOME/home/
    # secrets_dir hides below (the snapshot itself lives under
    # config.home, also hidden by default). () immediately, no directory
    # even created, when this project has no reference_paths configured.
    snapshot_entries = ensure_snapshot_for_attempt(ctx.config, ctx.project, ctx.attempt_id)
    readonly = (tuple(target for _entry, target in safe_copy_link_mounts)
                + (home_dir,) + tuple(entry.path for entry in snapshot_entries))
    if spec.gate_facts_path is not None:
        # The whole gate-ro directory (gate_facts.json + a copy of the
        # engine config the gate needs for GB_CONFIG -- see
        # station_paths.gate_ro_dir/station_gate.write_gate_facts), never
        # spec.gate_facts_path alone: mounting just the one file would
        # still leave its PARENT directory entries (if ever exposed
        # some other way) open to the renameable-ancestor issue this
        # directory exists to avoid. This directory sits outside every
        # writable mount (sibling of the stations dir under the same
        # hidden, then --remount-ro'd, $HOME -- see engine.infrastructure.sandbox.wrap),
        # so the worker can read it but never rename, replace or write
        # it.
        readonly += (spec.gate_facts_path.parent,)
    elif ctx.config.config_path is not None:
        # No gate for this role: nothing reads GB_CONFIG from it, but
        # keep the pre-existing direct mount in case anything else
        # needs config_path readable.
        readonly += (ctx.config.config_path,)
    writable_subpaths_config = ctx.config.projects[ctx.project].copy_link_writable_subpaths
    for entry, _target in safe_copy_link_mounts:
        link_subpaths = writable_subpaths_config.get(entry, ())
        if not link_subpaths:
            continue
        copy_entry = spec.copy_root / entry
        readonly += (copy_entry,)
        writable += tuple(copy_entry / subpath for subpath in link_subpaths)
    # Every worker process may import this engine package inside the
    # sandbox (the gate hook, the scripted worker): when the engine lives
    # under a hidden tree -- a checkout under the operator's HOME or /tmp --
    # it is re-exposed read-only. The engine's own code is not secret.
    readonly += (ENGINE_ROOT,)
    # The runner's own CLI, resolved from argv[0] (e.g. "claude"/"codex")
    # by PATH -- without this, hiding the real HOME wholesale (below) also
    # hides the CLI's own installed binary whenever it lives under HOME
    # (confirmed on this host for both: `which claude`/`which codex`
    # resolve under ~/.local/..., and a sandboxed spawn with HOME hidden
    # and no mount for this fails "command not found" even though the
    # unsandboxed PATH lookup finds it fine). See sandbox.resolve_runtime's
    # own docstring for exactly what gets mounted and why; () for a
    # command not found via PATH at all (nothing to add).
    symlinks: tuple[tuple[Path, Path], ...] = ()
    if argv:
        runtime = sandbox.resolve_runtime(argv[0])
        readonly += runtime.readonly
        symlinks += runtime.symlinks
    writable += tuple(home_dir / sub for sub in subpaths)
    hidden = _hidden_roots(ctx, repo) + _ledger_hidden_root(ctx)
    if real_home is not None:
        hidden += (real_home,)
    # 결함 17: same project.file_dep_targets remap
    # suite_run._bench_sandbox_argv applies for bench/acceptance, here
    # re-mapped onto THIS worker's own copy_root instead of the real
    # repo -- see engine.application.steps.file_deps' module docstring.
    # 결함 35 (sample-app T5,: a copy_links entry (node_modules)
    # is a symlink into the REAL repo, so a relative link inside it
    # (node_modules/@sample-app/kit -> ../../../_shared/kit) resolves
    # against the repo's own parent, never the copy's -- the pair is
    # mapped for BOTH roots or the builder sees an empty package.
    # 결함 42: mapping the pair is not enough on its own --
    # file_deps.sandbox_file_dep_pairs (not remap_pairs_for_roots
    # directly) also pre-creates a dest that does not exist on the host,
    # since the real target usually sits inside hidden_paths' broader
    # apps-root hide (one level beyond `repo` itself) and bwrap cannot
    # mkdir a mountpoint there once that hide is remounted read-only.
    readonly_remap = file_deps.sandbox_file_dep_pairs(
        spec.copy_root, repo, ctx.config.projects[ctx.project].file_dep_targets)
    def wrap_cli(extra_writable: tuple[Path, ...] = (),
                 extra_readonly: tuple[Path, ...] = ()) -> list[str]:
        return sandbox.wrap(
            argv, cwd=spec.workdir, writable=writable + extra_writable, hidden=hidden,
            readonly=readonly + extra_readonly, readonly_remap=readonly_remap,
            symlinks=symlinks, net=True, mode=ctx.config.sandbox, child_env=env,
            protected_roots=(ctx.config.secrets_dir,),
        )

    try:
        return worker_shell_jail.wrap_for_runner(
            runner, mode=ctx.config.sandbox, env=env, wrap_cli=wrap_cli,
            root=worker_shell_jail.jail_root(
                ctx.config.home, ids.attempt_dirname(ctx.attempt_id), spec.station, spec.role),
            policy=lambda: _command_policy(ctx, spec, home_dir, safe_copy_link_mounts,
                                           snapshot_entries, readonly_remap, hidden),
            protected_roots=(ctx.config.secrets_dir,))
    except sandbox.SandboxUnavailable as exc:
        raise StepFailed("sandbox_unavailable") from exc


def _command_policy(ctx: StepContext, spec: WorkerSpec, home_dir: Path,
                    safe_copy_link_mounts: tuple[tuple[str, Path], ...],
                    snapshot_entries: tuple[SnapshotEntry, ...],
                    readonly_remap: tuple[tuple[Path, Path], ...],
                    hidden: tuple[Path, ...]) -> worker_shell_jail.CommandPolicy:
    """Layer B (the worker's shell commands) for a builder: the worker's
    own copy and its configured writable copy_links subpaths writable,
    and read-only the same project material layer A gets -- never the
    CLI's HOME (login files), the gate-ro directory (MCP launch config,
    gate facts) or the stations dir: the policy-gate hook runs in layer A
    (ClaudeRunner.local_shell_commands). The CLI's own shell-snapshot
    directory is re-exposed read-only: the CLI sources a snapshot file
    from there at the start of each command."""
    writable: tuple[Path, ...] = (spec.copy_root,)
    readonly: tuple[Path, ...] = tuple(target for _e, target in safe_copy_link_mounts)
    readonly += tuple(entry.path for entry in snapshot_entries)
    subpaths_config = ctx.config.projects[ctx.project].copy_link_writable_subpaths
    for entry, _target in safe_copy_link_mounts:
        link_subpaths = subpaths_config.get(entry, ())
        if link_subpaths:
            readonly += (spec.copy_root / entry,)
            writable += tuple(spec.copy_root / entry / sub for sub in link_subpaths)
    snapshots = home_dir / CLI_SHELL_SNAPSHOT_SUBDIR
    snapshots.mkdir(parents=True, exist_ok=True)
    readonly += (ENGINE_ROOT, snapshots)
    from engine.application.steps.worker_gate_log import allowed_mcp_servers
    try:
        granted = allowed_mcp_servers(ctx, spec.station)
    except AttributeError:  # a context with no request (direct/unit callers)
        granted = {}
    browser = any("playwright" in name.lower() or "browser" in name.lower() for name in granted)
    return worker_shell_jail.CommandPolicy(
        cwd=spec.workdir, writable=writable, readonly=readonly,
        readonly_remap=readonly_remap,
        hidden=hidden + sandbox.HOST_SOCKET_DIRS, net=browser)
