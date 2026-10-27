"""plan.json's data shapes and the parse/validate rules a plan must
satisfy: Scene/Task/Requirement/Tool/Plan dataclasses, load_plan,
ordered_tasks, requirement_coverers, and validate_plan (including the
scene-cmd-runner rules that decide whether a `covers`-claiming scene
actually runs its own tests/scenes/ script). engine.domain.plan_review (the
digest/review-record functions) and engine.domain.plan_tools (the skill/mcp
catalog functions) build on top of this module; engine.domain.plan re-exports
all three so `from engine.domain.plan import X` keeps working unchanged.

plan.json lives at repo/.gongbang/<job>/plan.json -- part of the app
repo's own committed job trail, like facts.md/report.md (see the
full-orbit contract). A scene's `cmd` runs in the repo root; exit 0
means the scene passed. Scene scripts referenced from `cmd` live under
tests/scenes/ in the same repo -- the planner may write them; a builder
station must never touch tests/scenes/ (see engine.application.pipeline and
EngineConfig.station_owners for each role's own write prefixes there).

A scene that `covers` a requirement must actually RUN its scene
script, not merely mention its path somewhere in `cmd` -- `cmd` must
start with one of the supported scene runners below, and the token
right after that runner must be the tests/scenes/ script itself (see
validate_plan/scene_script_arg). This is why `["true",
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
silently stand in for the trusted "node" engine.application.acceptance actually
resolves (via shutil.which against the ENGINE's own environment, not
anything the repo or the sandbox env could have set) when it
executes a covers-claiming scene. The scene path itself must stay
inside tests/scenes/ once normalized -- no ".." component, never
absolute -- so `["python3", "tests/scenes/../../src/check.py"]` is
rejected too, even though that string literally starts with
"tests/scenes/".
"""
from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path


from engine.domain.plan_paths import SCENES_PREFIX, SRC_OWNS_PREFIX, TEST_OWNS_PREFIX  # noqa: E402,F401


class PlanError(ValueError):
    """plan.json is missing, not valid JSON, does not match the fixed
    schema below, or (from ordered_tasks) has an unknown depends_on or
    a dependency cycle. `reason` is a short machine-readable token;
    callers (engine.application.pipeline) turn it into a FullOutcome("failed",
    reason=...) rather than letting this surface as an unhandled
    exception mid-orbit.

    `detail` ("" unless `reason == "plan_unreadable"` from a real JSON
    parse failure -- see json_decode_error_detail) is the human-
    readable error position and surrounding text a caller (engine.
    application.pipeline_planning.run_plan_and_review, 결함 30) records
    to the ledger so a reopened plan station's prompt can tell the
    planner exactly what was wrong with the LAST plan.json it wrote,
    instead of silently handing the same broken file back with no clue
    why it failed."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
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
    # (engine.application.pipeline) both key off this, never off position.
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
    # (see validate_plan and engine.application.pipeline._uncovered_requirement_
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
    engine.infrastructure.skills.load_skill) or an mcp server (a station-side
    --mcp-config entry, gated by engine.domain.policy against
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


# How much context json_decode_error_detail keeps on each side of the
# error position -- the owner's own figure (결함 30): enough that the
# planner can actually SEE the broken quote/comma, never the whole file.
_JSON_ERROR_CONTEXT_CHARS = 80


def json_decode_error_detail(raw_text: str, exc: BaseException) -> str:
    """"line L column C: <80 chars before>⟪ERROR⟫<80 chars after>" for a
    real json.JSONDecodeError, else "" (e.g. exc is a plain
    UnicodeDecodeError, which has no line/col/pos to show). Lives next
    to load_plan (the one place that actually catches this) -- same
    bd38249 reasoning as every other rule/detail helper in this module:
    the code that reads the failure shape stays next to the code that
    turns it into text."""
    lineno = getattr(exc, "lineno", None)
    colno = getattr(exc, "colno", None)
    pos = getattr(exc, "pos", None)
    msg = getattr(exc, "msg", None) or str(exc)
    if not isinstance(pos, int):
        return msg
    start = max(0, pos - _JSON_ERROR_CONTEXT_CHARS)
    end = min(len(raw_text), pos + _JSON_ERROR_CONTEXT_CHARS)
    before = raw_text[start:pos]
    after = raw_text[pos:end]
    where = f"line {lineno} column {colno}" if lineno is not None else f"문자 위치 {pos}"
    return f"{msg} ({where}): ...{before}⟪여기⟫{after}..."


def load_plan(repo: Path, job: str) -> Plan:
    """Raises PlanError("plan_missing"/"plan_unreadable"/<schema token>) --
    a "plan_unreadable" from a real JSON parse failure carries
    json_decode_error_detail's own text as PlanError.detail."""
    path = plan_path(repo, job)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PlanError("plan_missing") from None
    except OSError:
        raise PlanError("plan_unreadable") from None
    try:
        raw = json.loads(raw_text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise PlanError("plan_unreadable", json_decode_error_detail(raw_text, exc)) from None
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
    place both validate_plan and engine.application.pipeline's post-acceptance
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
# scene_script_arg), and every later prefix token ("vitest", "run")
# is also compared literally.
_SUPPORTED_SCENE_RUNNERS: tuple[tuple[str, ...], ...] = (
    ("python3",), ("python",), ("node",), ("bash",), ("sh",),
    ("npx", "vitest", "run"),
)

# Value-less flags a `covers`-claiming scene's cmd may place between its
# runner prefix and the scene script itself -- e.g. ["npx", "vitest",
# "run", "--no-color", "--no-cache", "tests/scenes/x.test.ts"], the same
# shape a project's own test_cmd (EngineConfig.test_cmd, e.g. "npx vitest
# run --no-color --no-cache") already uses, so a planner copying that
# project convention into a scene's cmd is not rejected outright. A flag
# that takes a value -- "--config x" (separate token) or "--root=.."
# (packed into one token with "=") -- is deliberately NOT on this list and
# never will be matched by prefix/pattern: scene_script_arg only ever
# skips a token that equals one of these EXACTLY, so a value-taking flag
# simply fails to match, is never treated as "the scene path", and the
# scene has no scene_script_arg at all (same as any other unsupported
# token here) -- never silently treated as safe because it merely LOOKS
# like one of these.
_SAFE_SCENE_CMD_FLAGS: tuple[str, ...] = ("--no-color", "--no-cache", "--silent")

# vitest's own default test-discovery pattern (its `test.include` option,
# left unset by every tools/templates/new-app vite.config.js --
# defineConfig({}) -- so this is the pattern vitest itself falls back
# to, not a value this engine invented): `**/*.{test,spec}.?(c|m)[jt]s?(x)`.
# 결함 54, node-small gb2 change): a scene cmd
# ["npx", "vitest", "run", "tests/scenes/welcome-greeting.scene.ts"]
# passes scene_script_arg (a safe tests/scenes/ path) and the token
# existence check right above (the file is really there), yet vitest
# itself never discovers a ".scene.ts" file -- "No test files found",
# exit 1, every attempt, forever (max_fix_rounds_exceeded). Only the
# ("npx", "vitest", "run") runner needs this: python3/python/node/bash/
# sh (the other _SUPPORTED_SCENE_RUNNERS entries) run the named file
# directly, with no discovery step of their own to miss it.
_VITEST_DISCOVERABLE_RE = re.compile(r"\.(?:test|spec)\.(?:[cm])?[jt]sx?$")

# Same tuple as _SUPPORTED_SCENE_RUNNERS' own vitest entry -- referenced,
# never retyped, so the two can never drift on which prefix counts as
# "the vitest runner".
_VITEST_RUNNER_PREFIX: tuple[str, ...] = _SUPPORTED_SCENE_RUNNERS[-1]


def _vitest_discoverable(path: str) -> bool:
    return _VITEST_DISCOVERABLE_RE.search(path) is not None


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


def scene_script_arg(cmd: tuple[str, ...]) -> str | None:
    """The tests/scenes/ path `cmd` actually runs -- the token right
    after a matched _SUPPORTED_SCENE_RUNNERS prefix and any run of
    _SAFE_SCENE_CMD_FLAGS immediately following it, iff that token is a
    safe scene path (_is_safe_scene_path). None if `cmd` does not start
    with any supported runner, cmd[0] is not a BARE runner name (contains
    "/" or "\\" -- a path-qualified cmd[0] is never trusted here at all,
    see module docstring), or the token right after the runner (and any
    leading safe flags) is something else (an unsupported/value-taking
    flag, a different path, a path that escapes tests/scenes/, or the
    scene path buried behind other tokens) -- `cmd` mentioning
    "tests/scenes/..." ANYWHERE else does not count (see module
    docstring: `["true", "tests/scenes/x.py"]` must return None here,
    since `true` never runs that script).

    Imported directly by engine.application.acceptance (`from engine.domain.plan import
    scene_script_arg`), so this exact name must keep resolving on
    this module -- never renamed to a public one on its own."""
    if not cmd or "/" in cmd[0] or "\\" in cmd[0]:
        return None
    for prefix in _SUPPORTED_SCENE_RUNNERS:
        n = len(prefix)
        if len(cmd) <= n or cmd[0] != prefix[0] or tuple(cmd[1:n]) != prefix[1:]:
            continue
        idx = n
        while idx < len(cmd) - 1 and cmd[idx] in _SAFE_SCENE_CMD_FLAGS:
            idx += 1
        if idx >= len(cmd):
            continue
        arg = cmd[idx]
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
        that script (scene_script_arg), not merely mention its path
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
        pass engine.domain.evidence_graph's own runtime completion gate
        (missing()), which only ever counts a PASSING scene -- so this
        is checked here too, not only there.
    Schema-level problems (missing/mistyped fields) are not re-checked
    here -- load_plan already raised PlanError for those before a Plan
    object could exist."""
    problems: list[str] = []
    if not plan.requirements:
        # A plan with no requirements at all would otherwise sail
        # through every other check here with zero problems -- yet
        # name nothing for the handoff's own requirement<->scene table
        # (engine.application.handoff._scene_table) or engine.domain.evidence_graph's runtime
        # completion gate (missing()) to ever check against. Required
        # for every plan, full-job and `--small`
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
        # 결함 42, plans/-gongbang-entry-unify.md
        # §C): a build task whose owns touches src/ with no owns path of
        # its own under tests/ reaches the build station with no test
        # file to write at all -- the repeated real incident (an
        # operator demanding tests, with none in the task's own `owns`
        # to put them under, so the builder has to stop and ask).
        # SKIPPED when `build_prefixes` is None, same reason the
        # owns-subset check right above is: check_resumed_plan re-checks
        # an ALREADY-REVIEWED plan on every resume, never a freshly
        # written one -- this rule only ever needs to catch a NEW plan,
        # at the one point (pipeline_planning.run_plan_and_review,
        # quick_pipeline's own plan check) that calls validate_plan with
        # a real build_prefixes.
        for task in plan.tasks:
            if (any(_prefix_covers(SRC_OWNS_PREFIX, entry) for entry in task.owns)
                    and not any(_prefix_covers(TEST_OWNS_PREFIX, entry) for entry in task.owns)):
                problems.append(
                    f"task {task.id}: owns {SRC_OWNS_PREFIX} 경로가 있지만 "
                    f"{TEST_OWNS_PREFIX} 아래 자기 시험 파일이 owns에 없다"
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
        scene_arg = scene_script_arg(scene.cmd)
        if (scene_arg is not None
                and tuple(scene.cmd[:len(_VITEST_RUNNER_PREFIX)]) == _VITEST_RUNNER_PREFIX
                and not _vitest_discoverable(scene_arg)):
            # scene_script_arg already proved the vitest runner's next
            # real token IS this tests/scenes/ file and it exists on
            # disk -- but vitest's own default discovery pattern
            # (_VITEST_DISCOVERABLE_RE) still never matches it, so
            # every acceptance run of this scene fails with "No test
            # files found" regardless (결함 54).
            problems.append(f"scene {scene.id}: scene_file_not_discoverable: {scene_arg}")
        if scene.covers and scene_arg is None:
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


def plan_owns_rule_sentence(build_prefixes: "tuple[str, ...] | None",
                             manifest: str | None) -> str | None:
    """The literal Korean sentence a planner-station prompt
    (engine.application.stage_prompts.planner_prompt/small_prompt) must carry
    VERBATIM, right next to the owns-vs-build_prefixes check above this
    function, so the planner is told the same boundary that check
    enforces BEFORE it writes a plan that fails it -- 결함 27
   , marketing-chat job): plan check round 2 failed with
    "task T11: owns 'package.json' is outside build prefixes [...]"
    because nothing in either prompt ever said what `owns` may contain
    or where a manifest change belongs instead (same bd38249 "rules and
    instructions in one place" family as every other *_RULE constant in
    this module).

    Returns None when `build_prefixes` is None -- there is no prefix
    list to name, same skip this function's only caller (stage_prompts)
    must match. `manifest` is the project's ProjectConfig.manifest (the
    one path install/scripts_change may edit, e.g. "package.json");
    when it is also None, the sentence names "package.json" only as a
    generic example, never as this project's own real path."""
    if build_prefixes is None:
        return None
    manifest_example = manifest or "package.json"
    return (
        "작업(task)의 owns는 다음 경로 아래에서만 고를 수 있다: "
        f"{', '.join(build_prefixes)}. {manifest_example} 같은 매니페스트 파일은 "
        "owns에 넣지 않는다 -- 의존성이나 스크립트를 바꿔야 하면 그 작업의 detail에 "
        "\"설치/스크립트 변경 요청: <무엇, 왜>\"라고 적는다(운영자 승인 뒤 따로 적용된다)."
    )


from engine.domain.plan_rules import (  # noqa: E402,F401  -- re-exported, see that module
    PLAN_JSON_VALID_RULE, PLAN_REQUIREMENT_SCENE_COVERAGE_RULE, PLAN_SCENE_CMD_FLAGS_RULE,
    PLAN_SCENE_EXIT0_RULE, PLAN_SCENE_FILE_DISCOVERABLE_RULE, PLAN_SCHEMA_RULE,
    PLAN_SCHEMA_RULE_SMALL, PLAN_TASK_TEST_OWNS_RULE, plan_rule_sentences, plan_rule_sentences_small,
)



__all__ = [
    "SCENES_PREFIX", "SRC_OWNS_PREFIX", "TEST_OWNS_PREFIX",
    "Scene", "Task", "Tool", "Requirement", "Plan", "PlanError", "plan_path", "load_plan",
    "validate_plan", "ordered_tasks", "requirement_coverers", "plan_owns_rule_sentence",
    "PLAN_SCHEMA_RULE", "PLAN_SCHEMA_RULE_SMALL", "PLAN_REQUIREMENT_SCENE_COVERAGE_RULE",
    "PLAN_SCENE_EXIT0_RULE", "PLAN_SCENE_CMD_FLAGS_RULE", "PLAN_SCENE_FILE_DISCOVERABLE_RULE",
    "PLAN_TASK_TEST_OWNS_RULE",
    "plan_rule_sentences", "plan_rule_sentences_small",
]
