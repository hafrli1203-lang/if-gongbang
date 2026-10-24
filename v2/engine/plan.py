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
"""
from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass
import re
from pathlib import Path
from typing import TYPE_CHECKING

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
    # requirement ids this task implements but that have no scene of
    # their own (e.g. a "security" requirement with no automated
    # pass/fail scene) -- counts as covered once this task's own
    # attempt reaches "done" (see engine.full_orbit._uncovered_requirement_ids).
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


def validate_plan(plan: Plan, repo: Path, build_prefixes: tuple[str, ...]) -> list[str]:
    """[] iff `plan` is internally consistent and safe to run:
      - every task's `owns` is non-empty and a subset of
        `build_prefixes` (the project's configured build-station write
        prefixes, EngineConfig.station_owners["build"][<builder role>])
      - every task's `depends_on` names a real task id, with no cycle
        (ordered_tasks does the actual topological check)
      - every scene's `cmd` that names a tests/scenes/ script
        references a file that actually exists in `repo` (the planner
        owns writing that file)
      - every scene/task `covers` entry names a real requirement id,
        and every requirement is covered by at least one scene or task
        (requirement_coverers)
    Schema-level problems (missing/mistyped fields) are not re-checked
    here -- load_plan already raised PlanError for those before a Plan
    object could exist."""
    problems: list[str] = []
    for task in plan.tasks:
        for entry in task.owns:
            if not any(_prefix_covers(prefix, entry) for prefix in build_prefixes):
                problems.append(
                    f"task {task.id}: owns {entry!r} is outside build prefixes {list(build_prefixes)!r}"
                )
    for scene in plan.scenes:
        for token in scene.cmd:
            if token.startswith("tests/scenes/") and not (repo / token).exists():
                problems.append(f"scene {scene.id}: script not found: {token}")
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
        if not coverers.get(requirement.id):
            problems.append(f"requirement {requirement.id}: not covered by any scene or task")
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
            stations = sorted(st for st, names in allow.items() if name in names)
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
    installed, so an unregistered name is a hard job failure. A
    "skill" entry is deliberately NOT checked here: per the owner's
    design, a skill the planner asked for that is not in
    config.skill_index_path is "needs_install" (see
    classify_skill_tools), not a reason to fail the whole job -- it is
    just never granted to any station, and is reported as such in the
    handoff. Problems are plain strings naming kind+name, same style
    as validate_plan's own problem list -- callers (engine.full_orbit)
    concatenate both lists the same way."""
    problems: list[str] = []
    for tool in plan.tools:
        if tool.kind == "mcp" and tool.name not in config.mcp_servers:
            problems.append(f"tool mcp {tool.name!r}: not in config.mcp_servers")
    return problems


__all__ = [
    "Scene", "Task", "Tool", "Requirement", "Plan", "PlanError", "plan_path", "load_plan",
    "validate_plan", "validate_tools", "tools_for_station", "classify_skill_tools",
    "ordered_tasks", "requirement_coverers",
]
