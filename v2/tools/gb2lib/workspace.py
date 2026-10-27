"""`gb2 init`/`gb2 new`: building an app's engine workspace (config.json
rendered from the node-vitest template, bench_cases.json, the gb2/work
worktree, project registration, dependency bootstrap) and, for `new`,
scaffolding a brand-new app repo first."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import time
from pathlib import Path

from engine.application.steps import file_deps
from engine.bootstrap import ensure_installed, record_bootstrap
from engine.config import EngineConfig
from engine.ledger import Ledger

from . import ops, shared

__all__ = [
    "load_template", "render_config", "substitute_deploy_script", "fill_context",
    "verify_app_repo_identity", "read_worktree_base", "cmd_init", "cmd_new",
]

NEGATIVE_CONTROL_TEST = '''\
// gb2 negative control -- fails only when GB2_NEGATIVE=1 is set, so the
// normal suite stays green while the bench can still prove its grader
// is not "always passes".
import { describe, expect, it } from "vitest";

describe("gb2 negative control", () => {
  it.skipIf(!process.env.GB2_NEGATIVE)("fails on purpose when GB2_NEGATIVE is set", () => {
    expect(1).toBe(2);
  });
});
'''

NEGATIVE_CONTROL_CASES = [
    {"name": "gb2_negative_control", "selector": "tests/gb2-negative-control.test.ts",
     "expect_fail": True, "expected_signature": "assertion", "env": {"GB2_NEGATIVE": "1"}},
]


def _bench_cases_need_upgrade(bench_cases_path: Path) -> bool:
    """True iff `bench_cases_path` already exists but its own
    "gb2_negative_control" entry's expected_signature is not the
    current one (engine.steps.bench_run._case_signature now classifies
    a real vitest assertion failure as "assertion", never "exit:1" --
    a workspace generated before that change would otherwise fail
    every bench run with wrong_failure_signature forever, since
    nothing else ever rewrites bench_cases.json once it exists)."""
    try:
        data = json.loads(bench_cases_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, list):
        return False
    for case in data:
        if (isinstance(case, dict) and case.get("name") == "gb2_negative_control"
                and case.get("expected_signature") != "assertion"):
            return True
    return False


# --------------------------------------------------------------------
# Pure helpers (unit-tested without touching git or the real filesystem
# beyond a tmp_path).
# --------------------------------------------------------------------

def load_template(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def render_config(template: dict[str, object], app_name: str) -> dict[str, object]:
    """Return `template` with its single "__APP__" project key renamed
    to `app_name` -- the only per-app substitution the template needs;
    every path inside a project entry ("repo", "bench_cases", ...) is
    already relative to the app's own config.json and needs no further
    rewriting. Raises ValueError if the template's shape does not match
    (exactly one project, keyed "__APP__")."""
    projects = template.get("projects")
    if not isinstance(projects, dict) or list(projects.keys()) != ["__APP__"]:
        raise ValueError("template must have exactly one project key '__APP__'")
    rendered = dict(template)
    rendered["projects"] = {app_name: projects["__APP__"]}
    return rendered


def substitute_deploy_script(config_dict: dict[str, object],
                             deploy_script: Path) -> dict[str, object]:
    """Replace the literal "__DEPLOY_SCRIPT__" placeholder the template
    puts in the top-level publisher's argv/dry_run_argv entries with
    the real deploy script path -- the one place a path outside this
    app's own workspace needs filling in, so it is done here rather
    than baked into the template as a host-specific string."""
    publisher = config_dict.get("publisher")
    if not isinstance(publisher, dict):
        return config_dict
    rendered = dict(config_dict)
    rendered["publisher"] = {
        **publisher,
        **{
            key: [str(part).replace("__DEPLOY_SCRIPT__", str(deploy_script))
                  for part in publisher[key]]
            for key in ("argv", "dry_run_argv") if key in publisher
        },
    }
    return rendered


def fill_context(config_dict: dict[str, object], app: str,
                 source_repo: Path | None) -> dict[str, object]:
    """Fill the template's "context" block for this app: the company
    library MCP server from env GB2_LIBRARY_MCP (a command line; absent
    -> no library lookups), this app's lessons, and its own
    docs/ai/CURRENT_DECISIONS.md when the source repo has one. Host
    paths come from the environment and the repo, never the template."""
    context = config_dict.get("context")
    if not isinstance(context, dict):
        return config_dict
    filled = dict(context)
    library = os.environ.get("GB2_LIBRARY_MCP", "").strip()
    if library:
        filled["library_mcp"] = {"command": shlex.split(library), "timeout_sec": 20}
    filled["lessons_app"] = {app: app}
    direction = os.environ.get("GB2_DIRECTION_FILE", "").strip()
    if direction:
        filled["direction_path"] = direction
    if source_repo is not None:
        decisions = source_repo / "docs" / "ai" / "CURRENT_DECISIONS.md"
        if decisions.is_file():
            filled["decisions_paths"] = {app: str(decisions)}
    return {**config_dict, "context": filled}


# --------------------------------------------------------------------
# init
# --------------------------------------------------------------------

def _detect_default_branch(source_repo: Path) -> str:
    """The source repo's own default branch, in order of preference:
    (1) `git symbolic-ref refs/remotes/origin/HEAD` (what the remote
    itself calls its default, if this repo has an "origin" at all);
    (2) this repo's own current HEAD branch (a local-only repo, or one
    whose remote tracking ref was never fetched); (3) whichever of
    "master"/"main" exists as a local branch. Never assumes "master"
    (older repos) or "main" (newer ones) outright -- shared.WORKTREE_BASE
    stays only as the brand-new-repo default `cmd_new`'s own
    `_scaffold_new_app_repo` creates from scratch, where there is
    nothing yet to detect. Raises ValueError if none of these resolve
    (e.g. a repo with no commits at all)."""
    try:
        ref = shared.git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=source_repo)
    except subprocess.CalledProcessError:
        ref = ""
    if ref:
        return ref.split("/", 1)[1] if "/" in ref else ref
    try:
        head = shared.git(["symbolic-ref", "--short", "HEAD"], cwd=source_repo)
    except subprocess.CalledProcessError:
        head = ""
    if head:
        return head
    for candidate in ("master", "main"):
        if shared.git(["branch", "--list", candidate], cwd=source_repo):
            return candidate
    raise ValueError(f"cannot determine default branch for {source_repo}")


def _worktree_base_path(app: str) -> Path:
    """Where this app's own detected default-branch name is kept --
    gb2's own bookkeeping file (like runs.json), never a key inside
    config.json: engine/config.py's own strict key validation is outside
    this script's owned scope, so nothing here can add a new field to
    the engine config it writes."""
    return shared.app_workspace(app) / "worktree_base.txt"


def read_worktree_base(app: str) -> str:
    """The base branch `gb2 init`/`gb2 new` detected and recorded for
    this app, or shared.WORKTREE_BASE ("master") if this app's
    workspace predates _worktree_base_path existing at all -- back-
    compat only, never written fresh."""
    path = _worktree_base_path(app)
    if not path.exists():
        return shared.WORKTREE_BASE
    text = path.read_text(encoding="utf-8").strip()
    return text or shared.WORKTREE_BASE


def _write_worktree_base(app: str, base: str) -> None:
    _worktree_base_path(app).write_text(base, encoding="utf-8")


def _verify_worktree_identity(source_repo: Path, repo_dir: Path) -> None:
    """Raise ValueError unless `repo_dir` is ALREADY a worktree git
    itself registers for `source_repo`, currently on shared.WORKTREE_BRANCH
    -- an existing directory at that path must never be silently adopted
    and used as if it were (it could be anything: a stale unrelated
    checkout, a directory an app's own build left behind, ...)."""
    repo_dir_real = str(repo_dir.resolve())
    out = shared.git(["worktree", "list", "--porcelain"], cwd=source_repo)
    for entry in out.split("\n\n"):
        fields: dict[str, str] = {}
        for line in entry.splitlines():
            key, _, value = line.partition(" ")
            fields[key] = value
        wt_path = fields.get("worktree")
        if not wt_path or str(Path(wt_path).resolve()) != repo_dir_real:
            continue
        branch_ref = fields.get("branch", "")
        if branch_ref != f"refs/heads/{shared.WORKTREE_BRANCH}":
            raise ValueError(
                f"{repo_dir} is a worktree of {source_repo} but on "
                f"{branch_ref or '(detached)'!r}, not refs/heads/{shared.WORKTREE_BRANCH}"
            )
        return
    raise ValueError(f"{repo_dir} exists but is not a registered worktree of {source_repo}")


def verify_app_repo_identity(app: str) -> None:
    """Re-verify that this app's repo_dir is STILL a registered
    worktree of its source repo, on shared.WORKTREE_BRANCH, before a
    subcommand (run/ship/sync/diff) does anything else with it -- not
    just a check `init` makes once. Guards against the directory
    having been replaced, detached, or otherwise altered since init
    (a stale symlink swap, a careless `rm -rf` + recreate by hand,
    ...)."""
    repo_dir = shared.app_workspace(app) / "repo"
    if not repo_dir.exists():
        raise ValueError(f"not initialized: run `gb2 init {app}` first")
    source_repo = (shared.apps_root() / app).resolve()
    _verify_worktree_identity(source_repo, repo_dir)


def _copy_node_modules(source_repo: Path, repo_dir: Path) -> None:
    """Physically copy source_repo/node_modules into repo_dir, never
    link -- a worker's Bash runs inside this worktree and must never be
    able to write back into the source repo's own node_modules through
    a shared inode/symlink target. node_modules is always gitignored
    (see the node-vitest template's own .gitignore), so `git worktree
    add` never brings it along regardless of what is committed in
    source_repo -- this on-disk copy is the only way the worktree gets
    it. A no-op if source_repo has no node_modules yet."""
    source_node_modules = source_repo / "node_modules"
    if not source_node_modules.exists():
        return
    subprocess.run(["cp", "-r", str(source_node_modules),
                    str(repo_dir / "node_modules")], check=True)


def _ensure_worktree(source_repo: Path, repo_dir: Path, base: str) -> None:
    existing_branches = shared.git(["branch", "--list", shared.WORKTREE_BRANCH], cwd=source_repo)
    if not existing_branches:
        shared.git(["branch", shared.WORKTREE_BRANCH, base], cwd=source_repo)
    if repo_dir.exists():
        _verify_worktree_identity(source_repo, repo_dir)
        return
    shared.git(["worktree", "add", str(repo_dir), shared.WORKTREE_BRANCH], cwd=source_repo)
    _copy_node_modules(source_repo, repo_dir)
    neg_test = repo_dir / "tests" / "gb2-negative-control.test.ts"
    if not neg_test.exists():
        neg_test.parent.mkdir(parents=True, exist_ok=True)
        neg_test.write_text(NEGATIVE_CONTROL_TEST, encoding="utf-8")
        shared.git(["add", "tests/gb2-negative-control.test.ts"], cwd=repo_dir)
        shared.git(["-c", "user.name=gb2", "-c", "user.email=gb2@local",
             "commit", "-q", "-m", "test: gb2 negative control"], cwd=repo_dir)


def _lockfile_snapshot(source_repo: Path) -> bytes | None:
    """package-lock.json's current bytes, or None if it does not exist
    yet -- taken right BEFORE engine.bootstrap.ensure_installed runs, so
    `_commit_generated_lockfile` can tell whether THIS call's own
    bootstrap actually touched it (see that function's own docstring)."""
    lockfile = source_repo / "package-lock.json"
    return lockfile.read_bytes() if lockfile.exists() else None


def _commit_generated_lockfile(source_repo: Path, before: bytes | None) -> None:
    """After engine.bootstrap.ensure_installed's FIRST install into
    source_repo, npm has written (or updated) package-lock.json there.
    That file must be committed in source_repo -- not just left as an
    uncommitted working-tree change -- because `_ensure_worktree`'s
    `git worktree add` right after this only brings along what is in a
    commit; an uncommitted lockfile in source_repo would never reach
    the worktree.

    `before` is `_lockfile_snapshot(source_repo)` taken right before
    THIS call's own ensure_installed ran -- a no-op unless the file's
    bytes actually changed from that snapshot (ensure_installed found
    node_modules already installed, so nothing ran this time; or the
    file is dirty for some other, unrelated reason -- e.g. the owner
    edited it by hand -- that has nothing to do with this bootstrap and
    must never be swept up as if it did).

    Only this one explicit path is ever staged (never `git add -A`) and
    only this one path is ever committed: `git commit -- <path>` commits
    exactly that path's staged+worktree change regardless of whatever
    else the owner may have already staged in their own index before
    calling this -- that other staged work is left exactly as the owner
    left it, still staged, never swept into this commit."""
    lockfile = source_repo / "package-lock.json"
    if not lockfile.exists():
        return
    if lockfile.read_bytes() == before:
        return
    shared.git(["add", "--", "package-lock.json"], cwd=source_repo)
    shared.git(["-c", "user.name=gb2", "-c", "user.email=gb2@local",
         "commit", "-q", "-m", "chore: bootstrap dependency lockfile",
         "--", "package-lock.json"],
         cwd=source_repo)


class BootstrapFailedError(RuntimeError):
    """Raised by `_setup_app_workspace` when engine.bootstrap
    .ensure_installed fails for this app's source repo -- kept
    separate from ValueError (a config mistake) so `cmd_init`/`cmd_new`
    report it as exit code 2, not the generic exit code 1, and so
    nothing calls on into the full orbit (a job never even opens) on
    top of a dependency install that never actually succeeded."""


def _setup_app_workspace(app: str, with_release: bool) -> EngineConfig:
    """Shared by `cmd_init` and `cmd_new`: write this app's engine
    workspace (config.json rendered from the node-vitest template,
    bench_cases.json, the gb2/work worktree, project registration) for
    a source repo that must ALREADY exist at GB2_APPS_ROOT/<app> with a
    package.json -- `cmd_init` expects an app the owner built by hand;
    `cmd_new` calls `_scaffold_new_app_repo` first so this is always
    true by the time this runs. Returns the loaded EngineConfig. Raises
    ValueError (nothing written yet) for the workspace/source-repo
    coincide-or-nest misconfiguration or a missing package.json -- same
    checks `cmd_init` always made, now shared instead of duplicated.
    Also bootstraps this app's own dependencies (engine.bootstrap
    .ensure_installed against the SOURCE repo, never the worktree --
    see that module's docstring) once the ledger is open and the
    project is registered; raises BootstrapFailedError if that install
    fails, after still recording the attempt via record_bootstrap."""
    source_repo = (shared.apps_root() / app).resolve()
    workspace = shared.app_workspace(app)
    if source_repo == workspace or shared.is_within(workspace, source_repo) \
            or shared.is_within(source_repo, workspace):
        raise ValueError(
            f"workspace {workspace} and source repo {source_repo} must not "
            "coincide or nest -- check GB2_WORKSPACE/GB2_APPS_ROOT"
        )
    if not (source_repo / "package.json").exists():
        raise ValueError(f"no package.json under {source_repo}")
    repo_dir = workspace / "repo"
    if repo_dir.exists():
        # Verify BEFORE writing anything: an existing repo/ that turns
        # out not to actually be this source's gb2/work worktree must
        # never leave a config.json/bench_cases.json written for it
        # first and only fail afterward.
        _verify_worktree_identity(source_repo, repo_dir)

    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "home").mkdir(exist_ok=True)
    (workspace / "secrets").mkdir(exist_ok=True)

    template = load_template(shared.TEMPLATES_DIR / "node-vitest.json")
    config_dict = render_config(template, app)
    config_dict = substitute_deploy_script(config_dict, shared.deploy_script_path(shared.apps_root()))
    config_dict = fill_context(config_dict, app, source_repo)
    if with_release:
        # The real original repo (never the gb2/work worktree) is the
        # fast-forward target -- `engine.release`'s advance_master step
        # merges this project's own repo's HEAD into it. `target` is
        # the deploy name the top-level `publisher` argv's "{target}"
        # substitutes in.
        config_dict["projects"][app]["release"] = {
            "master_repo": str(source_repo), "target": app,
        }
    # Hide the WHOLE apps root and the WHOLE gb2 workspace root -- not
    # just this app's own source repo (the previous, narrower shape of
    # this list). This app's own source repo can hold real environment
    # files, which is reason enough to hide it, but hiding only that
    # one path left every OTHER app's own source repo under the same
    # apps root, and every OTHER app's own engine workspace (its home/
    # secrets/ledger under APPS_DIR.parent) fully readable to any
    # worker/reviewer/bench sandbox run for THIS app -- both roots are
    # mirrored read-only by every sandbox's own "--ro-bind / /" base
    # the same way any other absolute host path is, since neither was
    # ever named in `hidden` before. engine.sandbox.wrap's own
    # depth-sort then re-exposes exactly what THIS job still needs,
    # the same way it already re-exposes `repo` (this project's own
    # worktree, nested under the workspace root) today: a writable or
    # readonly mount of a DESCENDANT path is always applied after an
    # ancestor's hidden mount, never the other way round (see
    # engine.sandbox.wrap's own module docstring) -- station.py/
    # review.py/bench_run.py/acceptance.py/installers.py each already
    # mount their own job-specific descendant paths (the worker's own
    # copy, the acceptance snapshot, `repo` itself for an install) this
    # way, so nothing else needs to change for a job to keep seeing
    # its own copy/snapshot once this list widens.
    config_dict["projects"][app]["hidden_paths"] = [
        str(shared.apps_root().resolve()), str(shared.APPS_DIR.parent.resolve()),
    ]
    # 결함 17: resolve every package.json `file:../...`
    # dependency that escapes source_repo -- while source_repo is still
    # the REAL, un-mirrored checkout, the one place this can be done
    # correctly at all (engine.application.steps.file_deps' own module
    # docstring) -- and persist (relspec, already-validated absolute
    # target) pairs so every later sandbox build (bench/acceptance/
    # worker, against the gb2/work worktree, a worker copy, or a
    # disposable snapshot) can remap the SAME relspec onto whichever
    # root it is actually running against, instead of re-walking it and
    # landing one nesting level short. `forbidden_roots=(workspace,)`:
    # this app's own engine home/secrets/ledger all nest under
    # `workspace`, and a target that happens to land there (e.g. a
    # package.json hand-edited to point `file:` at this app's own
    # engine state) must never be exposed read-only in a sandbox.
    file_dep_targets: list[list[str]] = []
    for relspec in file_deps.discover_escaping_file_deps(source_repo):
        target = file_deps.resolve_escaping_target(source_repo, relspec)
        file_deps.validate_escaping_target(target, forbidden_roots=(workspace,))
        file_dep_targets.append([relspec, str(target)])
    config_dict["projects"][app]["file_dep_targets"] = file_dep_targets
    config_path = workspace / "config.json"
    config_path.write_text(json.dumps(config_dict, ensure_ascii=False, indent=1),
                           encoding="utf-8")

    bench_cases_path = workspace / "bench_cases.json"
    if not bench_cases_path.exists() or _bench_cases_need_upgrade(bench_cases_path):
        bench_cases_path.write_text(
            json.dumps(NEGATIVE_CONTROL_CASES, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

    # Bootstrap BEFORE the worktree is created (P1 fix): `_ensure_worktree`
    # below only copies node_modules into the fresh worktree if source_repo
    # already has one (_copy_node_modules) -- for a brand-new app it never
    # does until this install runs. Bootstrapping first means the worktree
    # created right after this sees a real node_modules to copy, instead of
    # a worker/bench run against it failing before it can run a single
    # test. `config` is loaded here (not after _ensure_worktree, as
    # before) so the sandbox hardening below has it.
    config = EngineConfig.from_json(config_path)
    ledger = Ledger.open(config.ledger_path) if config.ledger_path.exists() \
        else Ledger.create(config.ledger_path)
    try:
        for name in config.projects:
            ledger.register_project(name)
        install_cmd = config.projects[app].install_cmd
        # Deliberately NOT config.projects[app].hidden_paths: that list
        # is exactly [str(source_repo.resolve())] (set above), to hide
        # the real source from the WORKTREE's own sandboxes -- but this
        # install runs directly against source_repo itself, so passing
        # it here would hide the very directory being installed into
        # (engine.sandbox.wrap rejects a path that is both writable and
        # hidden).
        lockfile_before = _lockfile_snapshot(source_repo)
        bootstrap_result = ensure_installed(
            source_repo, tuple(install_cmd),
            sandbox_mode=config.sandbox,
            hidden_paths=(config.home, config.secrets_dir,
                         config.ledger_path.parent),
        )
        record_bootstrap(ledger, app, bootstrap_result)
        if not bootstrap_result.ok:
            raise BootstrapFailedError(
                f"dependency install failed for {source_repo}: "
                f"{bootstrap_result.detail}"
            )
    finally:
        ledger.close()

    _commit_generated_lockfile(source_repo, lockfile_before)

    base_branch = _detect_default_branch(source_repo)
    _write_worktree_base(app, base_branch)
    _ensure_worktree(source_repo, workspace / "repo", base_branch)

    return config


def cmd_init(args: argparse.Namespace) -> int:
    shared.validate_app_name(args.app)  # app_workspace() below re-checks too
    try:
        _setup_app_workspace(args.app, args.with_release)
    except BootstrapFailedError as exc:
        return shared.err_json(str(exc), 2)
    except ValueError as exc:
        return shared.err_json(str(exc), 1)
    print(json.dumps({"ok": True, "app": args.app, "workspace": str(shared.app_workspace(args.app))},
                     ensure_ascii=False))
    return 0


# --------------------------------------------------------------------
# new (full orbit: engine.full_orbit.run_full_job -- facts -> plan ->
# [design] -> build loop -> acceptance -> report, see shared
# .run_full_job_and_report)
# --------------------------------------------------------------------

# Skeleton files `gb2 new` seeds a brand-new app repo with (relative to
# tools/templates/new-app/). Listed explicitly, never discovered via
# glob, so a dotfile like .gitignore is never silently skipped by a
# glob pattern that happens not to match leading dots on some Python
# version, and so adding a new skeleton file is a deliberate one-line
# change here, not an accident of what happens to be on disk.
_NEW_APP_SKELETON_FILES = (
    "README.md", ".gitignore", "package.json", "tests/scenes/.gitkeep",
    "index.html", "vite.config.js", "src/main.js",
    # COMMON_ARCHITECTURE_STANDARD.md's layered folders, each with one
    # minimal real file (interface -> application -> domain, with
    # infrastructure behind domain's own port) -- see this skeleton's
    # own README.md "Structure" section.
    "src/app/home-page.js",
    "src/components/greeting-banner.js",
    "src/server/actions/get-greeting.js",
    "src/server/application/greeting/get-greeting.js",
    "src/server/domain/greeting/rules.js",
    "src/server/domain/greeting/ports.js",
    "src/server/infrastructure/db/greeting-log-repository.js",
    "src/server/infrastructure/integrations/system-clock.js",
    "src/config/env.js",
    "tests/unit/greeting-rules.test.js",
    "tests/e2e/README.md",
    "tests/fakes/clock.fake.js",
)


def _render_skeleton_text(text: str, app_name: str) -> str:
    """The skeleton's only per-app substitution: a literal "__APP__"
    token (package.json's "name" field), same placeholder convention
    as the config template's render_config."""
    return text.replace("__APP__", app_name)


_NEW_APP_MARKER_NAME = ".gongbang-new"


def _new_app_marker_path(source_repo: Path) -> Path:
    """gb2's own sentinel (never committed -- _scaffold_new_app_repo adds
    only the named skeleton files, never `-A`, so this is never staged)
    marking a `gb2 new` init still in progress for `source_repo`. Its
    PRESENCE is what lets `cmd_new` tell "a previous `gb2 new` crashed
    partway through, safe to resume" apart from "a real, already-
    complete app (or one gb2 never touched at all) -- never adopt it"."""
    return source_repo / _NEW_APP_MARKER_NAME


def _validate_workspace_source_repo_disjoint(app: str, source_repo: Path) -> None:
    """The same coincide-or-nest check `_setup_app_workspace` makes on
    its own -- duplicated here so `cmd_new` can run it BEFORE ever
    creating `source_repo` on disk (see cmd_new's own docstring: this is
    a config mistake, not something that should ever leave a half-made
    app repo around for a later `gb2 new` to have to resume from)."""
    workspace = shared.app_workspace(app)
    if source_repo == workspace or shared.is_within(workspace, source_repo) \
            or shared.is_within(source_repo, workspace):
        raise ValueError(
            f"workspace {workspace} and source repo {source_repo} must not "
            "coincide or nest -- check GB2_WORKSPACE/GB2_APPS_ROOT"
        )


def _scaffold_new_app_repo(source_repo: Path, app: str) -> None:
    """Create `source_repo` as a brand-new git repository seeded from
    tools/templates/new-app/ (README, .gitignore, a minimal Node+Vitest
    package.json with no dependencies installed yet, an empty
    tests/scenes/ -- the planner role is the only one later allowed to
    write under tests/scenes/), with one initial commit.

    Idempotent/resumable: if `source_repo` already exists but still
    carries `_new_app_marker_path`'s marker (a previous `gb2 new` call
    crashed somewhere inside this function, or inside the
    `_setup_app_workspace` call right after it, before the marker could
    be removed), this finishes whatever is still missing -- an existing
    skeleton file is left untouched (never re-rendered over real,
    already-produced content), `git init` is skipped if `.git` already
    exists, and the initial commit is skipped if HEAD already resolves
    -- instead of raising. An existing `source_repo` WITHOUT that
    marker (a real, already-complete app, or one gb2 never touched at
    all) is still refused outright, nothing created."""
    marker = _new_app_marker_path(source_repo)
    if source_repo.exists() and not marker.exists():
        raise ValueError(f"app_exists: {source_repo}")
    skeleton_dir = shared.TEMPLATES_DIR / "new-app"
    if not skeleton_dir.is_dir():
        raise ValueError(f"missing skeleton template dir: {skeleton_dir}")
    source_repo.mkdir(parents=True, exist_ok=True)
    marker.write_text(app, encoding="utf-8")
    for rel in _NEW_APP_SKELETON_FILES:
        src = skeleton_dir / rel
        dest = source_repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            dest.write_text(_render_skeleton_text(src.read_text(encoding="utf-8"), app),
                            encoding="utf-8")
    if not (source_repo / ".git").is_dir():
        shared.git(["init", "-q", "-b", shared.WORKTREE_BASE], cwd=source_repo)
    try:
        shared.git(["rev-parse", "HEAD"], cwd=source_repo)
        has_commit = True
    except subprocess.CalledProcessError:
        has_commit = False
    if not has_commit:
        # Named paths, never `-A`: the marker file sits in this same
        # directory and must never be staged/committed.
        shared.git(["add", *_NEW_APP_SKELETON_FILES], cwd=source_repo)
        shared.git(["-c", "user.name=gb2", "-c", "user.email=gb2@local",
             "commit", "-q", "-m", f"chore: scaffold {app} via gb2 new"], cwd=source_repo)


def cmd_new(args: argparse.Namespace) -> int:
    """`gb2 new <app> "요구사항"`: refuse if the app folder already
    exists as a real, unrelated/complete app, else scaffold it from
    tools/templates/new-app/ (one initial commit), set up its engine
    workspace exactly like `gb2 init`, then run the full orbit
    (engine.full_orbit.run_full_job) end to end: facts -> plan ->
    [design, if the plan is a UI job] -> the task loop -> acceptance ->
    report.

    Everything is validated BEFORE the app folder is ever created
    (app name, requirement text, the environment itself via
    `ops.require_doctor_ok` -- a missing CLI must never surface partway
    through scaffolding or mid-job, the workspace/source-repo coincide-
    or-nest check _setup_app_workspace would otherwise only catch after
    `source_repo` already exists) -- a config mistake must never leave
    a half-made app repo behind. If a PREVIOUS `gb2 new <app>` call left
    `source_repo` with _new_app_marker_path's marker still in it (it
    crashed somewhere inside scaffolding, workspace setup, or before
    the orbit run's first stage actually started), this resumes that
    same init instead of refusing "app_exists" -- see
    `_scaffold_new_app_repo`'s own docstring and shared
    .run_full_job_and_report's own docstring for when the marker is
    finally removed."""
    try:
        shared.validate_app_name(args.app)
    except ValueError as exc:
        return shared.err_json(str(exc), 1)
    request = shared.read_requirement(args)
    if not request:
        return shared.err_json("requirement text (or --task-file) required", 1)
    doctor_error = ops.require_doctor_ok()
    if doctor_error:
        return shared.err_json(doctor_error, 1)
    source_repo = (shared.apps_root() / args.app).resolve()
    marker = _new_app_marker_path(source_repo)
    if source_repo.exists() and not marker.exists():
        return shared.err_json(f"app_exists: {source_repo}", 1)

    try:
        _validate_workspace_source_repo_disjoint(args.app, source_repo)
        _scaffold_new_app_repo(source_repo, args.app)
        config = _setup_app_workspace(args.app, args.with_release)
    except BootstrapFailedError as exc:
        return shared.err_json(str(exc), 2)
    except ValueError as exc:
        return shared.err_json(str(exc), 1)
    # marker stays until run_full_job_and_report confirms the orbit's
    # first stage actually started -- see that function's own docstring.

    job_name = args.job_name or f"gb2-new-{int(time.time())}"
    return shared.run_full_job_and_report(args, config, job_name, request, new_app_marker=marker)
