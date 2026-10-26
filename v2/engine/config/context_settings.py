"""The `context` and `library_mcp` blocks of engine.config.EngineConfig:
which skills, library server, decisions and lessons a station prompt
gets (engine.infrastructure.context gathers them)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LibraryMcpConfig:
    """One stdio MCP server launch (engine.config's `library_mcp` key)."""

    command: tuple[str, ...]
    timeout_sec: float = 10.0

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("library_mcp.command must be a non-empty list")


@dataclass(frozen=True)
class ContextConfig:
    """engine.config.EngineConfig's optional `context` block.

    skill_roots      -- paths searched (in order) for `<name>/SKILL.md`
                         (engine.infrastructure.skills.load_skill), also one level of
                         category folders deep.
    station_skills   -- station name -> skill names to inject there.
    library_mcp      -- the company-library stdio MCP server, or None
                         (no library/lesson lookup at all).
    decisions_paths  -- project name -> that project's own
                         CURRENT_DECISIONS.md path.
    lessons_app      -- project name -> the app name passed to the
                         library's `lessons_for` tool (no entry: no
                         lessons lookup for that project, even with
                         library_mcp configured).
    required_skills  -- station name -> names (a subset of that
                         station's own station_skills/plan.tools names)
                         that MUST be loaded for this job to proceed.
                         Default empty: nothing is required anywhere --
                         a missing skill is, by default, still only the
                         existing "## 이 칸의 스킬 (없음)" annotation in
                         the prompt text, work continues. Only a name
                         explicitly listed here turns "missing" into
                         StationContext.required_missing, which a
                         caller (engine.application.pipeline) can check to stop
                         the job needs_owner instead of silently
                         proceeding without it -- without an explicit
                         name here, a missing required skill or
                         company-material lookup failure stays a mere
                         annotation and never stops the job (see
                         ENGINE.md).
    require_library  -- project names for which a library_mcp lookup
                         failure (Context.failed) must likewise count
                         as a required-material gap
                         (StationContext.required_library_failed), not
                         just the existing "곳간 조회 실패" annotation.
                         Default empty tuple: no project requires it.
    """

    skill_roots: tuple[str, ...] = ()
    station_skills: dict[str, tuple[str, ...]] = field(default_factory=dict)
    library_mcp: "LibraryMcpConfig | None" = None
    decisions_paths: dict[str, str] = field(default_factory=dict)
    # The owner's direction quotes file (verbatim, dated), injected whole
    # into the facts and plan stations of every job.
    direction_path: str | None = None
    lessons_app: dict[str, str] = field(default_factory=dict)
    required_skills: dict[str, tuple[str, ...]] = field(default_factory=dict)
    require_library: tuple[str, ...] = ()
