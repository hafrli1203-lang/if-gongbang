"""plan.json — the plan a planner station writes for one job: a goal,
pass/fail scenes (acceptance scripts a human's own action maps to), the
task breakdown (each task's own write prefixes and dependency order),
and plain-text acceptance criteria.

plan.json lives at repo/.gongbang/<job>/plan.json -- part of the app
repo's own committed job trail, like facts.md/report.md (see the
full-orbit contract). A scene's `cmd` runs in the repo root; exit 0
means the scene passed. Scene scripts referenced from `cmd` live under
tests/scenes/ in the same repo -- the planner may write them; a builder
station must never touch tests/scenes/ (see engine.full_orbit and
EngineConfig.station_owners for each role's own write prefixes there).

A scene that `covers` a requirement must actually RUN its scene
script, not merely mention its path somewhere in `cmd` -- `cmd` must
start with one of the supported scene runners below, and the token
right after that runner must be the tests/scenes/ script itself (see
validate_plan/_scene_script_arg). This is why `["true",
"tests/scenes/x.py"]` is rejected even though "tests/scenes/x.py"
appears in `cmd`: `true` ignores its arguments and exits 0 without
ever executing that script, which would let a scene claim a
requirement is demonstrated when nothing was ever run against it.
Supported runners: "python3", "python", "node", "bash", "sh", and
"npx vitest run" (the three-token prefix, for a Node test file used
as a scene). cmd[0] must be a BARE runner name -- never a path (no
"/" or "\\") -- so `["./src/node", "tests/scenes/x.js"]` is also
rejected: a path-qualified cmd[0] resolves relative to the process's
cwd regardless of PATH, which would let a job's own repo content
silently stand in for the trusted "node" engine.acceptance actually
resolves (via shutil.which against the ENGINE's own environment, not
anything the repo or the sandbox env could have set) when it
executes a covers-claiming scene. The scene path itself must stay
inside tests/scenes/ once normalized -- no ".." component, never
absolute -- so `["python3", "tests/scenes/../../src/check.py"]` is
rejected too, even though that string literally starts with
"tests/scenes/".
"""
from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import dataclass
import re
from pathlib import Path
from typing import TYPE_CHECKING

from engine.ledger import Event, Ledger
from engine.skills import skill_path_in_root

if TYPE_CHECKING:
    from engine.config import EngineConfig


class PlanError(ValueError):
    """plan.json is missing, not valid JSON, does not match the fixed
    schema below, or (from ordered_tasks) has an unknown depends_on or
    a dependency cycle. `reason` is a short machine-readable token;
    callers (engine.full_orbit) turn it into a FullOutcome("failed",
    reason=...) rather than letting this surface as an unhandled
    exception mid-orbit."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Scene:
    id: str
    actor_does: str
    app_shows: str
    where: str
    cmd: tuple[str, ...]  # runs in repo root; exit 0 = pass
    # requirement ids (Plan.requirements) this scene demonstrates -- the
    # handoff 대조표 and the post-acceptance requirement-coverage check
    # (engine.full_orbit) both key off this, never off position.
    covers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    detail: str
    owns: tuple[str, ...]
    depends_on: tuple[str, ...]
    # requirement ids this task implements -- tracking only (e.g. the
    # handoff report's own "도구표 대 실제 사용"-style section). This
    # task reaching "done" never counts as covering a requirement on
    # its own; completion requires at least one PASSING scene for it
    # (see validate_plan and engine.full_orbit._uncovered_requirement_
    # ids).
    covers: tuple[str, ...] = ()


_KNOWN_REQUIREMENT_KINDS = ("function", "security")


@dataclass(frozen=True)
class Requirement:
    id: str
    text: str
    kind: str  # "function" | "security"


_KNOWN_TOOL_KINDS = ("skill", "mcp")


@dataclass(frozen=True)
class Tool:
    """One entry of plan.json's optional "tools" list: the planner's own
    request for a skill (a station-side prompt injection, resolved by
    engine.skills.load_skill) or an mcp server (a station-side
    --mcp-config entry, gated by engine.policy against
    EngineConfig.station_mcp_allow) for one or more stations.
    `kind` is "skill" or "mcp"; `name` is the skill name (a skill_roots
    subdirectory) or the mcp_servers config key; `stations` lists which
    station(s) get it; `why` is the planner's own plain-text reason
    (never parsed, just carried through to the handoff report's
    "도구표 대 실제 사용" section)."""

    kind: str
    name: str
    stations: tuple[str, ...]
    why: str


@dataclass(frozen=True)
class Plan:
    goal: str
    ui: bool
    scenes: tuple[Scene, ...]
    tasks: tuple[Task, ...]
    acceptance: tuple[str, ...]
    tools: tuple[Tool, ...] = ()
    requirements: tuple[Requirement, ...] = ()


def plan_path(repo: Path, job: str) -> Path:
    return repo / ".gongbang" / job / "plan.json"


def _file_sha256(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()


def scene_file_digests(path: Path) -> dict[str, str]:
    """{scene file path (exactly as it appears in that scene's own
    `cmd`, relative to the repo) -> sha256 hex digest of its current
    bytes}, one entry per DISTINCT tests/scenes/ file any scene's cmd
    in the plan.json at `path` references -- "" for a referenced file
    that cannot be read (missing/unreadable), never raising, so a
    scene script that disappears after review still changes the
    digest plan_digest folds this into, rather than silently keeping
    the old one. {} if plan.json itself cannot be read/parsed (the
    same tolerant shape plan_digest needs: a resume must always get
    SOME digest to compare, never an exception, even for a job whose
    plan.json went missing between attempts)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    repo = path.parent.parent.parent  # repo/.gongbang/<job>/plan.json
    scene_paths: set[str] = set()
    for scene in raw.get("scenes", []) if isinstance(raw.get("scenes"), list) else []:
        if not isinstance(scene, dict):
            continue
        cmd = scene.get("cmd", [])
        if not isinstance(cmd, list):
            continue
        for token in cmd:
            if isinstance(token, str) and token.startswith("tests/scenes/"):
                scene_paths.add(token)
    return {p: _file_sha256(repo / p) for p in sorted(scene_paths)}


def plan_digest(path: Path) -> str:
    """sha256 hex digest over plan.json's own raw bytes (at `path`)
    plus every distinct tests/scenes/ file any scene's cmd references
    (scene_file_digests, folded in sorted path order as
    "<path>:<sha256>") -- a later edit to plan.json OR to any scene
    script it names changes this digest. engine.full_orbit's own
    record_plan_reviewed stores this (plus the per-file breakdown)
    in a plan.reviewed ledger event right after plan review passes; a
    resume (engine.full_orbit.continue_full_job) recomputes it and
    compares against that recorded value to decide whether the plan
    needs reviewing again. Never raises: an unreadable plan.json folds
    in as b"", same tolerant shape scene_file_digests already uses, so
    a resume always has SOME digest to compare rather than crashing."""
    try:
        plan_bytes = path.read_bytes()
    except OSError:
        plan_bytes = b""
    hasher = hashlib.sha256(plan_bytes)
    for scene_path, file_digest in scene_file_digests(path).items():
        hasher.update(f"\n{scene_path}:{file_digest}".encode("utf-8"))
    return hasher.hexdigest()


def record_plan_reviewed(ledger: Ledger, project: str, attempt_id: str,
                         repo: Path, job: str) -> None:
    """Appends one plan.reviewed ledger event ({"job": job, "digest":
    plan_digest(...), "scene_digests": scene_file_digests(...),
    "requirement_ids": [...], "task_ids": [...]}) for this (project,
    job) attempt -- engine.full_orbit calls this once, right after the
    planner's own plan review Stage comes back "done" (ENGINE.md: plan
    -> plan review -> commit), so plan.reviewed always names the exact
    plan.json + scene files that review actually judged, never a later
    edit. A resume reads the latest such event for this job back and
    recomputes plan_digest against the CURRENT plan.json/scene files
    to decide whether the plan changed since it was last reviewed (see
    plan_digest's own docstring). requirement_ids/task_ids (plan order,
    read via load_plan -- [] if the plan cannot be loaded at all, which
    should never happen here since review already ran against this
    same plan.json) are the ids check_resumed_plan compares against a
    LATER plan.json to catch a requirement or task quietly removed
    (rather than changed) since this review -- a case plan_digest alone
    would also catch (any edit changes the digest), but naming exactly
    which id disappeared is more useful than just "the plan changed"."""
    path = plan_path(repo, job)
    try:
        plan = load_plan(repo, job)
        requirement_ids = [r.id for r in plan.requirements]
        task_ids = [t.id for t in plan.tasks]
    except PlanError:
        requirement_ids, task_ids = [], []
    ledger.append(Event(
        project=project, kind="plan.reviewed", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"job": job, "digest": plan_digest(path),
                 "scene_digests": scene_file_digests(path),
                 "requirement_ids": requirement_ids, "task_ids": task_ids},
    ))


def _require_str(obj: dict[str, object], key: str, where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise PlanError(f"{where}.{key}_must_be_nonempty_string")
    return value


def _require_str_allow_empty(obj: dict[str, object], key: str, where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise PlanError(f"{where}.{key}_must_be_string")
    return value


def _require_str_tuple(obj: dict[str, object], key: str, where: str, *,
                       allow_empty: bool = True) -> tuple[str, ...]:
    value = obj.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PlanError(f"{where}.{key}_must_be_list_of_strings")
    if not allow_empty and not value:
        raise PlanError(f"{where}.{key}_must_not_be_empty")
    return tuple(value)


def _parse_scene(raw: object, index: int) -> Scene:
    if not isinstance(raw, dict):
        raise PlanError(f"scenes[{index}]_must_be_object")
    sid = _require_str(raw, "id", f"scenes[{index}]")
    where = f"scene[{sid}]"
    return Scene(
        id=sid,
        actor_does=_require_str(raw, "actor_does", where),
        app_shows=_require_str(raw, "app_shows", where),
        where=_require_str(raw, "where", where),
        cmd=_require_str_tuple(raw, "cmd", where, allow_empty=False),
        covers=_require_str_tuple(raw, "covers", where),
    )


def _parse_requirement(raw: object, index: int) -> Requirement:
    if not isinstance(raw, dict):
        raise PlanError(f"requirements[{index}]_must_be_object")
    rid = _require_str(raw, "id", f"requirements[{index}]")
    where = f"requirement[{rid}]"
    kind = _require_str(raw, "kind", where)
    if kind not in _KNOWN_REQUIREMENT_KINDS:
        raise PlanError(f"{where}.kind_must_be_function_or_security")
    return Requirement(id=rid, text=_require_str(raw, "text", where), kind=kind)


def _parse_tool(raw: object, index: int) -> Tool:
    if not isinstance(raw, dict):
        raise PlanError(f"tools[{index}]_must_be_object")
    where = f"tools[{index}]"
    kind = _require_str(raw, "kind", where)
    if kind not in _KNOWN_TOOL_KINDS:
        raise PlanError(f"{where}.kind_must_be_skill_or_mcp")
    name = _require_str(raw, "name", where)
    stations = _require_str_tuple(raw, "stations", where, allow_empty=False)
    why = _require_str(raw, "why", where)
    return Tool(kind=kind, name=name, stations=stations, why=why)


def _parse_task(raw: object, index: int) -> Task:
    if not isinstance(raw, dict):
        raise PlanError(f"tasks[{index}]_must_be_object")
    tid = _require_str(raw, "id", f"tasks[{index}]")
    where = f"task[{tid}]"
    return Task(
        id=tid,
        title=_require_str(raw, "title", where),
        detail=_require_str_allow_empty(raw, "detail", where),
        owns=_require_str_tuple(raw, "owns", where, allow_empty=False),
        depends_on=_require_str_tuple(raw, "depends_on", where),
        covers=_require_str_tuple(raw, "covers", where),
    )


def _parse_plan(raw: object) -> Plan:
    if not isinstance(raw, dict):
        raise PlanError("plan_root_must_be_object")
    goal = _require_str(raw, "goal", "plan")
    ui = raw.get("ui")
    if not isinstance(ui, bool):
        raise PlanError("plan.ui_must_be_bool")
    scenes_raw = raw.get("scenes")
    if not isinstance(scenes_raw, list):
        raise PlanError("plan.scenes_must_be_list")
    scenes = tuple(_parse_scene(s, i) for i, s in enumerate(scenes_raw))
    tasks_raw = raw.get("tasks")
    if not isinstance(tasks_raw, list) or not tasks_raw:
        raise PlanError("plan.tasks_must_be_nonempty_list")
    tasks = tuple(_parse_task(t, i) for i, t in enumerate(tasks_raw))
    acceptance = _require_str_tuple(raw, "acceptance", "plan", allow_empty=False)
    tools_raw = raw.get("tools", [])
    if not isinstance(tools_raw, list):
        raise PlanError("plan.tools_must_be_list")
    tools = tuple(_parse_tool(t, i) for i, t in enumerate(tools_raw))
    requirements_raw = raw.get("requirements", [])
    if not isinstance(requirements_raw, list):
        raise PlanError("plan.requirements_must_be_list")
    requirements = tuple(_parse_requirement(r, i) for i, r in enumerate(requirements_raw))

    task_ids = [t.id for t in tasks]
    if len(set(task_ids)) != len(task_ids):
        raise PlanError("plan.tasks_ids_not_unique")
    scene_ids = [s.id for s in scenes]
    if len(set(scene_ids)) != len(scene_ids):
        raise PlanError("plan.scenes_ids_not_unique")
    requirement_ids = [r.id for r in requirements]
    if len(set(requirement_ids)) != len(requirement_ids):
        raise PlanError("plan.requirements_ids_not_unique")
    return Plan(goal=goal, ui=ui, scenes=scenes, tasks=tasks, acceptance=acceptance,
                tools=tools, requirements=requirements)


def load_plan(repo: Path, job: str) -> Plan:
    """Raises PlanError("plan_missing"/"plan_unreadable"/<schema token>)."""
    path = plan_path(repo, job)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PlanError("plan_missing") from None
    except OSError:
        raise PlanError("plan_unreadable") from None
    try:
        raw = json.loads(raw_text)
    except (ValueError, UnicodeDecodeError):
        raise PlanError("plan_unreadable") from None
    return _parse_plan(raw)


def _normalize_rel(path: str) -> str:
    normalized = posixpath.normpath(path)
    return "" if normalized == "." else normalized


def _prefix_covers(prefix: str, entry: str) -> bool:
    """True iff relative path `entry` is at or under `prefix` -- same
    trailing-slash convention EngineConfig.station_owners already uses
    (a prefix ending in "/" is a directory; one that does not is an
    exact file)."""
    e = _normalize_rel(entry)
    if prefix.endswith("/"):
        base = _normalize_rel(prefix)
        return e == base or e.startswith(base + "/")
    return e == _normalize_rel(prefix)


def ordered_tasks(plan: Plan) -> list[Task]:
    """plan.tasks in dependency order (a task after everything it
    depends_on), ties broken by each task's own position in
    plan.tasks. Raises PlanError("unknown_dep:<task>:<dep>") for a
    depends_on naming a task id not in this plan, or
    PlanError("dependency_cycle:<task>") if the graph has a cycle."""
    by_id = {t.id: t for t in plan.tasks}
    for task in plan.tasks:
        for dep in task.depends_on:
            if dep not in by_id:
                raise PlanError(f"unknown_dep:{task.id}:{dep}")

    ordered: list[Task] = []
    seen: set[str] = set()
    visiting: set[str] = set()

    def visit(task: Task) -> None:
        if task.id in seen:
            return
        if task.id in visiting:
            raise PlanError(f"dependency_cycle:{task.id}")
        visiting.add(task.id)
        for dep in task.depends_on:
            visit(by_id[dep])
        visiting.discard(task.id)
        seen.add(task.id)
        ordered.append(task)

    for task in plan.tasks:
        visit(task)
    return ordered


def requirement_coverers(plan: Plan) -> dict[str, tuple[tuple[str, str], ...]]:
    """requirement id -> tuple of (kind, id) pairs ("scene"|"task", that
    scene's/task's own id) that cover it, in plan order -- the single
    place both validate_plan and engine.full_orbit's post-acceptance
    check look up who covers a given requirement, so the two never
    drift apart on what "covers" means."""
    coverers: dict[str, list[tuple[str, str]]] = {r.id: [] for r in plan.requirements}
    for scene in plan.scenes:
        for rid in scene.covers:
            coverers.setdefault(rid, []).append(("scene", scene.id))
    for task in plan.tasks:
        for rid in task.covers:
            coverers.setdefault(rid, []).append(("task", task.id))
    return {rid: tuple(pairs) for rid, pairs in coverers.items()}


# Scene cmd runners a `covers`-claiming scene is allowed to use (see
# the module docstring for why this exists). Each entry is the fixed
# prefix of tokens `cmd` must start with -- cmd[0] must match the
# FIRST token LITERALLY (never by basename: a full/relative path like
# sys.executable or "./src/node" is a different, untrusted thing, see
# _scene_script_arg), and every later prefix token ("vitest", "run")
# is also compared literally.
_SUPPORTED_SCENE_RUNNERS: tuple[tuple[str, ...], ...] = (
    ("python3",), ("python",), ("node",), ("bash",), ("sh",),
    ("npx", "vitest", "run"),
)


def _is_safe_scene_path(token: str) -> bool:
    """True iff `token` is a tests/scenes/ file path with no escape --
    never absolute, never containing a ".." component (so
    "tests/scenes/../../src/check.py" is rejected even though that
    literal string starts with "tests/scenes/" -- normalizing it
    collapses to "../src/check.py", no longer under tests/scenes/ at
    all), and its own posixpath.normpath leaves it unchanged (no
    redundant "./", doubled slashes, or trailing slash hiding a second
    reading of the same path)."""
    if not token or token.startswith("/") or token.startswith("\\"):
        return False
    if any(part in ("..", "") for part in token.split("/")[1:]):
        # [1:] so "tests/scenes/x.py".split("/") -- whose first part
        # is "tests", never "" or ".." -- is never flagged by its own
        # first component; every part after the first still must be a
        # real, non-empty, non-".." name.
        return False
    if posixpath.normpath(token) != token:
        return False
    return token.startswith("tests/scenes/") and token != "tests/scenes/"


def _scene_script_arg(cmd: tuple[str, ...]) -> str | None:
    """The tests/scenes/ path `cmd` actually runs -- the token right
    after a matched _SUPPORTED_SCENE_RUNNERS prefix, iff that token is
    a safe scene path (_is_safe_scene_path). None if `cmd` does not
    start with any supported runner, cmd[0] is not a BARE runner name
    (contains "/" or "\\" -- a path-qualified cmd[0] is never trusted
    here at all, see module docstring), or the runner's first real
    argument is something else (a flag, a different path, a path that
    escapes tests/scenes/, or the scene path buried behind other
    tokens) -- `cmd` mentioning "tests/scenes/..." ANYWHERE else does
    not count (see module docstring: `["true", "tests/scenes/x.py"]`
    must return None here, since `true` never runs that script)."""
    if not cmd or "/" in cmd[0] or "\\" in cmd[0]:
        return None
    for prefix in _SUPPORTED_SCENE_RUNNERS:
        n = len(prefix)
        if len(cmd) <= n or cmd[0] != prefix[0] or tuple(cmd[1:n]) != prefix[1:]:
            continue
        arg = cmd[n]
        if _is_safe_scene_path(arg):
            return arg
    return None


def validate_plan(plan: Plan, repo: Path, build_prefixes: "tuple[str, ...] | None") -> list[str]:
    """[] iff `plan` is internally consistent and safe to run:
      - every task's `owns` is non-empty and a subset of
        `build_prefixes` (the project's configured build-station write
        prefixes, EngineConfig.station_owners["build"][<builder role>])
        -- SKIPPED when `build_prefixes` is None, for a caller (engine.
        plan.check_resumed_plan) that re-validates a plan with no
        config/build_prefixes of its own to check this against; every
        other check below still runs even then, since none of them
        depend on build_prefixes
      - every task's `depends_on` names a real task id, with no cycle
        (ordered_tasks does the actual topological check)
      - every scene's `cmd` that names a tests/scenes/ script
        references a file that actually exists in `repo` (the planner
        owns writing that file)
      - every scene that `covers` a requirement actually RUNS its own
        tests/scenes/ script -- `cmd` must start with a supported
        runner (_SUPPORTED_SCENE_RUNNERS) whose first real argument IS
        that script (_scene_script_arg), not merely mention its path
        somewhere in `cmd` (see module docstring: `["true",
        "tests/scenes/x.py"]` is rejected even though that path
        appears in `cmd`, because `true` never runs it)
      - every scene/task `covers` entry names a real requirement id,
        and every requirement is covered by at least one SCENE
        (requirement_coverers) -- a task-only `covers` is kept for
        tracking (the handoff report's own "도구표 대 실제 사용"-style
        section) but is never sufficient on its own: a task finishing
        "done" only means its own review/bench passed, never that any
        scene actually exercised the requirement's real behavior. A requirement with ONLY a task coverer
        would otherwise pass this static check yet never be able to
        pass engine.full_orbit's own runtime completion gate
        (_uncovered_requirement_ids), which only ever counts a PASSING
        scene -- so this is checked here too, not only there.
    Schema-level problems (missing/mistyped fields) are not re-checked
    here -- load_plan already raised PlanError for those before a Plan
    object could exist."""
    problems: list[str] = []
    if not plan.requirements:
        # A plan with no requirements at all would otherwise sail
        # through every other check here with zero problems -- yet
        # name nothing for the handoff's own requirement<->scene table
        # (engine.handoff._scene_table) or engine.full_orbit's runtime
        # completion gate (_uncovered_requirement_ids) to ever check
        # against. Required for every plan, full-job and `--small`
        # alike (per the owner's design, same rule as the probe
        # station's own facts.md evidence requirement).
        problems.append("plan_requirements_missing")
    if build_prefixes is not None:
        for task in plan.tasks:
            for entry in task.owns:
                if not any(_prefix_covers(prefix, entry) for prefix in build_prefixes):
                    problems.append(
                        f"task {task.id}: owns {entry!r} is outside build prefixes {list(build_prefixes)!r}"
                    )
    tests_dir = repo / "tests"
    scenes_dir = tests_dir / "scenes"
    # A symlinked "tests" or "tests/scenes" directory would make
    # .resolve() follow it BEFORE it becomes the containment boundary
    # below -- e.g. "tests/scenes" -> "../src" resolves scenes_root to
    # `repo`'s own src/ itself, so a token "tests/scenes/check.py"
    # also resolving under repo/src/ would wrongly look "contained"
    # (it literally IS src/check.py by then). The REAL, non-symlinked
    # tests/scenes/ is the only boundary ever trusted: if either
    # component is itself a symlink, every tests/scenes/ reference in
    # this plan is rejected outright, never silently resolved through
    # it.
    scenes_boundary_is_symlink = tests_dir.is_symlink() or scenes_dir.is_symlink()
    scenes_root = None if scenes_boundary_is_symlink else scenes_dir.resolve()
    for scene in plan.scenes:
        for token in scene.cmd:
            if not token.startswith("tests/scenes/"):
                continue
            if scenes_boundary_is_symlink:
                problems.append(
                    f"scene {scene.id}: tests/scenes/ itself is a symlink: {token}"
                )
                continue
            candidate = repo / token
            if not candidate.exists():
                problems.append(f"scene {scene.id}: script not found: {token}")
                continue
            # The string already starts with "tests/scenes/" -- but a
            # TRACKED SYMLINK at that literal path can still point
            # anywhere once resolved (git faithfully recreates a
            # committed symlink in any checkout/snapshot). A scene
            # script must stay inside tests/scenes/ once symlinks are
            # followed too, not just by its own unresolved spelling.
            try:
                resolved = candidate.resolve()
            except OSError:
                problems.append(f"scene {scene.id}: script not found: {token}")
                continue
            if resolved != scenes_root and scenes_root not in resolved.parents:
                problems.append(f"scene {scene.id}: script escapes tests/scenes/: {token}")
        if scene.covers and _scene_script_arg(scene.cmd) is None:
            # A scene claiming it demonstrates a requirement must
            # actually RUN a planner-owned script under tests/scenes/
            # via a supported runner -- ["true", "tests/scenes/x.py"]
            # (or any other cmd whose runner is not in
            # _SUPPORTED_SCENE_RUNNERS, or whose first real argument is
            # not that script) passing trivially proves nothing about
            # the requirement's real behavior, so it can never be
            # allowed to claim `covers` on its own.
            problems.append(f"scene {scene.id}: scene_cmd_not_a_scene_file")
    try:
        ordered_tasks(plan)
    except PlanError as exc:
        problems.append(str(exc.reason))

    requirement_ids = {r.id for r in plan.requirements}
    for scene in plan.scenes:
        for rid in scene.covers:
            if rid not in requirement_ids:
                problems.append(f"scene {scene.id}: covers unknown requirement {rid!r}")
    for task in plan.tasks:
        for rid in task.covers:
            if rid not in requirement_ids:
                problems.append(f"task {task.id}: covers unknown requirement {rid!r}")
    coverers = requirement_coverers(plan)
    for requirement in plan.requirements:
        pairs = coverers.get(requirement.id, ())
        if not any(kind == "scene" for kind, _cid in pairs):
            problems.append(
                f"requirement {requirement.id}: not covered by any scene "
                "(a task-only `covers` is kept for tracking but is never enough)"
            )
    return problems


def _latest_plan_reviewed(ledger: Ledger, project: str,
                          job_name: str) -> dict[str, object] | None:
    """The payload of the latest "plan.reviewed" ledger event recorded
    for this (project, job_name), or None if there is none -- the one
    place both check_resumed_plan and verify_reviewed_scenes read this
    back from, so the two can never disagree about which event is
    "the latest" one."""
    payload: dict[str, object] | None = None
    for _seq, event in ledger.events(project, "plan.reviewed"):
        if event.payload.get("job") == job_name:
            payload = event.payload
    return payload


def verify_reviewed_scenes(ledger: Ledger, project: str, job_name: str,
                           repo: Path) -> list[str]:
    """[] iff plan.json's CURRENT plan_digest (plan.json's own bytes
    plus every tests/scenes/ file any scene's cmd references) still
    matches the latest plan.reviewed event's digest for this (project,
    job_name) -- the narrow, cheap check engine.acceptance.
    run_acceptance runs immediately before executing any covers-
    claiming scene, so a scene script (or plan.json itself) edited
    after review -- even within the SAME attempt, with no owner-stop/
    resume involved at all -- can never be certified as if a reviewer
    had actually seen it. This is the single-purpose digest half of
    check_resumed_plan, factored out on its own: run_acceptance has no
    reason to also re-run the full validate_plan sweep (build
    prefixes, requirement coverage, dependency cycles -- all already
    enforced once at plan review and again at every resume point) on
    every single acceptance call.

    "plan_never_reviewed" if no plan.reviewed event exists at all;
    "plan_changed_since_review" if the digest no longer matches. Never
    raises -- plan_digest's own tolerant shape always yields SOME
    digest to compare, even for a plan.json that cannot be read."""
    reviewed = _latest_plan_reviewed(ledger, project, job_name)
    if reviewed is None:
        return ["plan_never_reviewed"]
    if reviewed.get("digest") != plan_digest(plan_path(repo, job_name)):
        return ["plan_changed_since_review"]
    return []


def check_resumed_plan(ledger: Ledger, project: str, job_name: str, repo: Path) -> list[str]:
    """[] iff the CURRENT plan.json (+ scene files) for `job_name` at
    `repo` is still, in every sense that matters, the plan a reviewer
    last approved -- the check every resume point in engine.full_orbit/
    engine.small_orbit must run before trusting a job's plan to keep
    going with build/merge/commit work under it -- previously only the
    STAGE_DESIGN_APPROVAL resume ever re-checked this; a task/
    security/cursor resume accepted a plan.json edited since review
    with no check at all.

    Problems, in order:
      - plan.json fails to load/parse: [f"plan:{PlanError.reason}"],
        nothing else is checked (there is no Plan to check further)
      - validate_plan(plan, repo, build_prefixes=None) -- every
        prefix-INDEPENDENT structural rule (scene cmd shape, scene
        file existence, requirement/scene/task coverage, dependency
        cycles); build_prefixes=None skips the owns-subset check,
        since this function has no config of its own to get a real
        build_prefixes from (a caller that also needs the owns check
        still has validate_plan itself, called with the real one)
      - "plan_never_reviewed" if no "plan.reviewed" ledger event exists
        yet for this (project, job_name) -- a resume reaching a point
        that needs a reviewed plan with none on record is itself a
        problem, not something to silently pass
      - "plan_changed_since_review" if the plan's current plan_digest
        no longer matches the latest plan.reviewed event's digest --
        plan.json or some scene file it references was edited since
        that review
      - "requirement_removed_since_review: <id>" / "task_removed_
        since_review: <id>" for any requirement/task id the latest
        plan.reviewed event recorded (record_plan_reviewed's own
        requirement_ids/task_ids) that is no longer in the CURRENT
        plan -- reported even when, for some reason, the digest check
        above did not already catch the same edit, so a shrunk plan is
        never accepted on an id-by-id basis either.

    Never raises. [] means the caller may trust this job's plan.json
    as-is and keep going; any other return is a reason to stop and
    treat the job as needs_owner, the same way a first-pass plan
    review failure already does."""
    try:
        plan = load_plan(repo, job_name)
    except PlanError as exc:
        return [f"plan:{exc.reason}"]

    problems = validate_plan(plan, repo, None)

    reviewed = _latest_plan_reviewed(ledger, project, job_name)
    if reviewed is None:
        problems.append("plan_never_reviewed")
        return problems

    if reviewed.get("digest") != plan_digest(plan_path(repo, job_name)):
        problems.append("plan_changed_since_review")

    reviewed_requirement_ids = tuple(reviewed.get("requirement_ids", ()))
    reviewed_task_ids = tuple(reviewed.get("task_ids", ()))
    current_requirement_ids = {r.id for r in plan.requirements}
    current_task_ids = {t.id for t in plan.tasks}
    for rid in reviewed_requirement_ids:
        if rid not in current_requirement_ids:
            problems.append(f"requirement_removed_since_review: {rid}")
    for tid in reviewed_task_ids:
        if tid not in current_task_ids:
            problems.append(f"task_removed_since_review: {tid}")
    return problems


def tools_for_station(plan: Plan, station: str, *,
                      usable_skills: "frozenset[str] | None" = None) -> dict[str, list[str]]:
    """plan.tools narrowed to one station, split by kind: {"skill": [...],
    "mcp": [...]}, each a sorted list of distinct names. Used by
    engine.full_orbit to carry a station's own tool table into its
    run_station payload (build/design) -- never the whole plan.tools
    list, so a worker's own gate facts/argv only ever see the names
    that apply to the station it is actually running in.

    `usable_skills`, when given (engine.full_orbit passes
    classify_skill_tools(plan, config)'s first element), drops any
    skill name NOT in it -- a "needs_install" skill (not in the
    configured skill index) is never granted to any station, no
    matter which stations its plan.tools entry lists. None (default):
    no filtering, every matching skill name is included -- callers
    that never heard of classify_skill_tools keep today's behavior."""
    skills = sorted({t.name for t in plan.tools if t.kind == "skill" and station in t.stations
                     and (usable_skills is None or t.name in usable_skills)})
    mcp = sorted({t.name for t in plan.tools if t.kind == "mcp" and station in t.stations})
    return {"skill": skills, "mcp": mcp}


_INDEX_BOLD = re.compile(r"^- \*\*([^*]+)\*\*")


def _parse_index_names(text: str) -> set[str]:
    """Skill names listed in a skill-library index text. Accepts the
    library's own line shape "- **<name>** — <description>" and the
    plain "- <name>: <description>" shape. Any other line is ignored."""
    names: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        bold = _INDEX_BOLD.match(stripped)
        name = bold.group(1) if bold else stripped[2:].split(":", 1)[0]
        name = name.strip()
        if name:
            names.add(name)
    return names


def _skill_roots(config: "EngineConfig") -> tuple[Path, ...]:
    """Skill roots live in the optional context block (engine.context);
    a plain object with a top-level skill_roots also works (tests)."""
    ctx = getattr(config, "context", None)
    roots = getattr(ctx, "skill_roots", None) if ctx is not None else None
    if roots is None:
        roots = getattr(config, "skill_roots", ()) or ()
    return tuple(Path(str(r)) for r in roots)


def _available_skill_names(config: "EngineConfig") -> set[str]:
    """Every skill name the engine can actually load: names in the index
    file and in the per-pack listings beside it (`_index/*.md`), plus
    every folder holding a SKILL.md directly under, or one category
    level below, each configured skill root (core skills are not in the
    index)."""
    names: set[str] = set()
    index_path = getattr(config, "skill_index_path", None)
    if index_path is not None:
        index = Path(index_path).expanduser()
        for path in [index, *sorted((index.parent / "_index").glob("*.md"))]:
            try:
                names |= _parse_index_names(path.read_text(encoding="utf-8"))
            except OSError:
                continue
    for root in _skill_roots(config):
        base = Path(root).expanduser()
        for pattern in ("*/SKILL.md", "*/*/SKILL.md"):
            names |= {
                p.parent.name for p in base.glob(pattern)
                if skill_path_in_root(base, p) is not None
            }
    return names


def _frontmatter_description(skill_md: Path) -> str:
    try:
        lines = skill_md.read_text(encoding="utf-8").splitlines()[:30]
    except OSError:
        return ""
    for i, line in enumerate(lines):
        if line.startswith("description:"):
            text = line[len("description:"):].strip().strip('"')
            if text in ("|", ">", ""):
                text = " ".join(x.strip() for x in lines[i + 1:i + 4])
            return text[:200]
    return ""


# The only worker runner whose CLI actually accepts an --mcp-config
# (engine.workers.claude_cli._write_mcp_config_file/argv) -- codex's
# read-only reviewer CLI has no such flag (engine.steps.review's own
# "reviewer_mcp_unsupported" check) and a scripted runner has no concept
# of mcp at all (engine.workers.scripted.ScriptedRunner.argv never reads
# it). A station whose configured role's runner is not in here can never
# actually use an mcp tool no matter what plan.tools or
# config.station_mcp_allow says -- see _station_runner/_mcp_capable.
_MCP_CAPABLE_RUNNERS = frozenset({"claude"})


def _station_runner(config: "EngineConfig", station: str) -> str | None:
    """The WorkerConfig.runner of whichever role config.station_owners
    assigns to write at `station` (one role per station, the only shape
    every config in this codebase uses) -- None if the station has no
    owner configured, or that role has no entry in config.workers (duck-
    typed via getattr so a pre-tools config stub without station_owners
    still works, same convention skill_catalog_text already uses for
    skill_index_path)."""
    roles = getattr(config, "station_owners", {}).get(station, {})
    workers = getattr(config, "workers", {}) or {}
    for role in roles:
        worker = workers.get(role)
        if worker is not None:
            return worker.runner
    return None


def _mcp_capable(config: "EngineConfig", station: str) -> bool:
    """True iff `station`'s own runner can actually take an mcp server
    (_MCP_CAPABLE_RUNNERS) -- an unconfigured station (_station_runner
    returns None, nothing to check against) is treated as capable, same
    as before this check existed, so a config this module cannot see a
    runner for is never silently narrowed."""
    runner = _station_runner(config, station)
    return runner is None or runner in _MCP_CAPABLE_RUNNERS


def skill_catalog_text(config: "EngineConfig") -> str:
    """What the planner is shown to pick this job's tools from: the
    skills directly under each skill root (name + description), the
    library index file itself, every per-pack skill name (names only --
    the full pack listings are too large to send), and the MCP servers
    the engine can grant with the stations each may go to. Empty string
    when nothing is configured."""
    parts: list[str] = []
    core: list[str] = []
    for root in _skill_roots(config):
        base = Path(root).expanduser()
        for md in sorted(base.glob("*/SKILL.md")):
            if skill_path_in_root(base, md) is None:
                continue
            core.append(f"- {md.parent.name}: {_frontmatter_description(md)}")
    if core:
        parts.append("### 바로 쓰는 스킬\n" + "\n".join(core))
    index_path = getattr(config, "skill_index_path", None)
    if index_path is not None:
        index = Path(index_path).expanduser()
        try:
            parts.append("### 스킬 창고 색인\n" + index.read_text(encoding="utf-8"))
        except OSError:
            pass
        packs: list[str] = []
        for listing in sorted((index.parent / "_index").glob("*.md")):
            try:
                names = sorted(_parse_index_names(listing.read_text(encoding="utf-8")))
            except OSError:
                continue
            if names:
                packs.append(f"- {listing.stem}: " + ", ".join(names))
        if packs:
            parts.append("### 스킬 묶음 안의 스킬 이름\n" + "\n".join(packs))
    servers = getattr(config, "mcp_servers", {}) or {}
    if servers:
        allow = getattr(config, "station_mcp_allow", {}) or {}
        rows = []
        for name in sorted(servers):
            stations = sorted(st for st, names in allow.items()
                              if name in names and _mcp_capable(config, st))
            rows.append(f"- {name}: 쓸 수 있는 칸 {', '.join(stations) or '(없음)'}")
        parts.append("### MCP 서버\n" + "\n".join(rows))
    return "\n\n".join(parts)


def classify_skill_tools(plan: Plan, config: "EngineConfig") -> tuple[frozenset[str], frozenset[str]]:
    """(usable, needs_install) skill names among plan.tools: a skill
    tool is "usable" only if its name appears in config.skill_index_path
    (the same catalog the planner prompt itself was given, per the
    owner's design -- a planner may only ask for what it was shown).
    No skill_index_path configured, the file cannot be read, or the
    name is simply absent from it -> "needs_install" instead of a hard
    validation failure: engine.full_orbit never grants a needs_install
    skill to any station (tools_for_station's own usable_skills
    filter), and engine.handoff reports it as "설치 필요(사람 승인)"
    rather than silently dropping it. `plan` is duck-typed (getattr),
    same as engine.handoff's own Plan-shaped-object convention, so a
    pre-tools Plan stub still works (every one of its skill names,
    i.e. none, is trivially needs_install)."""
    skill_names = {t.name for t in getattr(plan, "tools", ()) if t.kind == "skill"}
    if not skill_names:
        return frozenset(), frozenset()
    available = _available_skill_names(config)
    usable = frozenset(n for n in skill_names if n in available)
    needs_install = frozenset(n for n in skill_names if n not in available)
    return usable, needs_install


def validate_tools(plan: Plan, config: "EngineConfig") -> list[str]:
    """[] iff every plan.tools "mcp" entry names a key of
    config.mcp_servers -- an mcp server is infrastructure the operator
    configures, never something a planner can simply ask for and get
    installed, so an unregistered name is a hard job failure -- AND
    every station that entry lists has a runner that can actually take
    mcp (_mcp_capable). Without this second check, a plan.tools entry
    that named "review" for a codex-reviewed project would pass
    validation and then fail at runtime, mid-job, with engine.steps.
    review's own "reviewer_mcp_unsupported" (_review_payload's own
    comment already names this exact failure mode). Catching it here
    instead means a bad plan never even starts running stations for
    it. A "skill" entry is deliberately NOT checked here: per the
    owner's design, a skill the planner asked for that is not in
    config.skill_index_path is "needs_install" (see
    classify_skill_tools), not a reason to fail the whole job -- it is
    just never granted to any station, and is reported as such in the
    handoff. Problems are plain strings naming kind+name, same style
    as validate_plan's own problem list -- callers (engine.full_orbit)
    concatenate both lists the same way."""
    problems: list[str] = []
    for tool in plan.tools:
        if tool.kind != "mcp":
            continue
        if tool.name not in config.mcp_servers:
            problems.append(f"tool mcp {tool.name!r}: not in config.mcp_servers")
            continue
        for station in tool.stations:
            if not _mcp_capable(config, station):
                problems.append(
                    f"tool mcp {tool.name!r}: station {station!r}'s runner "
                    f"{_station_runner(config, station)!r} cannot take mcp"
                )
    return problems


__all__ = [
    "Scene", "Task", "Tool", "Requirement", "Plan", "PlanError", "plan_path", "load_plan",
    "validate_plan", "validate_tools", "tools_for_station", "classify_skill_tools",
    "ordered_tasks", "requirement_coverers", "plan_digest", "scene_file_digests",
    "record_plan_reviewed", "check_resumed_plan", "verify_reviewed_scenes",
]
