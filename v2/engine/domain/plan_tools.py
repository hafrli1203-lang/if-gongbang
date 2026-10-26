"""plan.tools' skill/mcp catalog and validation: tools_for_station,
skill_catalog_text (what the planner is shown to pick from),
classify_skill_tools (usable vs needs_install) and validate_tools (mcp
server/station checks). Built on top of engine.domain.plan_model.Plan."""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from engine.domain.plan_model import Plan
from engine.infrastructure.skills import skill_path_in_root

if TYPE_CHECKING:
    from engine.config import EngineConfig


def tools_for_station(plan: Plan, station: str, *,
                      usable_skills: "frozenset[str] | None" = None) -> dict[str, list[str]]:
    """plan.tools narrowed to one station, split by kind: {"skill": [...],
    "mcp": [...]}, each a sorted list of distinct names. Used by
    engine.application.pipeline to carry a station's own tool table into its
    run_station payload (build/design) -- never the whole plan.tools
    list, so a worker's own gate facts/argv only ever see the names
    that apply to the station it is actually running in.

    `usable_skills`, when given (engine.application.pipeline passes
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
    """Skill roots live in the optional context block (engine.infrastructure.context);
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
# (engine.infrastructure.workers.claude_cli._write_mcp_config_file/argv) -- codex's
# read-only reviewer CLI has no such flag (engine.application.steps.review's own
# "reviewer_mcp_unsupported" check) and a scripted runner has no concept
# of mcp at all (engine.infrastructure.workers.scripted.ScriptedRunner.argv never reads
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
    validation failure: engine.application.pipeline never grants a needs_install
    skill to any station (tools_for_station's own usable_skills
    filter), and engine.application.handoff reports it as "설치 필요(사람 승인)"
    rather than silently dropping it. `plan` is duck-typed (getattr),
    same as engine.application.handoff's own Plan-shaped-object convention, so a
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
    validation and then fail at runtime, mid-job, with engine.application.steps.
    review's own "reviewer_mcp_unsupported" (_review_payload's own
    comment already names this exact failure mode). Catching it here
    instead means a bad plan never even starts running stations for
    it. A "skill" entry is deliberately NOT checked here: per the
    owner's design, a skill the planner asked for that is not in
    config.skill_index_path is "needs_install" (see
    classify_skill_tools), not a reason to fail the whole job -- it is
    just never granted to any station, and is reported as such in the
    handoff. Problems are plain strings naming kind+name, same style
    as validate_plan's own problem list -- callers (engine.application.pipeline)
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
    "tools_for_station", "skill_catalog_text", "classify_skill_tools", "validate_tools",
]
