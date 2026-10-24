"""Engine-side context gathering for station prompts (docs/v2-staff.md
「스킬이 "알아서" 붙는 방식」): the company library (via a stdio MCP
client this module alone spawns -- the WORKER process never gets
library MCP access), lessons, a project's CURRENT_DECISIONS.md, and
station skills (engine.skills). Pure read side: never writes anything
a worker or the orchestrator's own commit would pick up.

Trust levels on a ContextItem (borrowed idea, SoDam O-Brain "imported =
low trust, not auto-injected"):
  "approved"   -- a project's own CURRENT_DECISIONS.md section
  "experience" -- a lesson (labelled "경험·아직 기준 아님" when rendered)
  "reference"  -- a plain library_search result
A library/lesson result whose own `origin` field is "run_output" (AI-
generated output, not a human-approved fact) is never turned into a
ContextItem at all -- it is filtered out in gather(), not merely
down-ranked.

A library client failure (spawn, protocol, timeout) never raises out
of gather() -- it is recorded as `failed=True` on the returned Context,
and build() turns that into both a "곳간 조회 실패" line appended to
the station prompt and a `context.failed` ledger event the caller
appends (gather/build themselves never touch the ledger -- the caller
already holds it, same as every other engine.full_orbit call)."""
from __future__ import annotations

import json
import queue
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from engine.skills import load_skill

if TYPE_CHECKING:
    from engine.config import EngineConfig

ITEM_KINDS = ("library", "lesson", "decision", "skill")
TRUST_KINDS = ("approved", "experience", "reference")

# Section size caps (characters) for the "주입된 자료" block a station
# prompt gets appended -- a single huge library result (or many small
# ones) must never blow up a worker's own prompt budget.
PER_ITEM_CAP = 2000
TOTAL_SECTION_CAP = 12000

_TRUST_LABEL = {"approved": "승인됨", "experience": "경험·아직 기준 아님", "reference": "참고"}


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
                         (engine.skills.load_skill), also one level of
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
    """

    skill_roots: tuple[str, ...] = ()
    station_skills: dict[str, tuple[str, ...]] = field(default_factory=dict)
    library_mcp: "LibraryMcpConfig | None" = None
    decisions_paths: dict[str, str] = field(default_factory=dict)
    # The owner's direction quotes file (verbatim, dated), injected whole
    # into the facts and plan stations of every job.
    direction_path: str | None = None
    lessons_app: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextItem:
    kind: str  # "library" | "lesson" | "decision" | "skill"
    title: str
    text: str
    source: str
    trust: str  # "approved" | "experience" | "reference"

    def __post_init__(self) -> None:
        if self.kind not in ITEM_KINDS:
            raise ValueError(f"unknown ContextItem kind: {self.kind!r}")
        if self.trust not in TRUST_KINDS:
            raise ValueError(f"unknown ContextItem trust: {self.trust!r}")


@dataclass(frozen=True)
class Context:
    items: tuple[ContextItem, ...] = ()
    failed: bool = False


@dataclass(frozen=True)
class StationContext:
    """What a station prompt actually gets: `text` is the ready-to-
    append block (empty string if there is nothing to add). `injected`/
    `skills`/`missing`/`failed` are exactly the fields
    engine.full_orbit's caller turns into the `context.injected`
    (and, if `failed`, `context.failed`) ledger event payload."""

    text: str
    injected: tuple[ContextItem, ...]
    skills: tuple[str, ...]
    missing: tuple[str, ...]
    failed: bool
    # Which requested skills came from the station's fixed base set and
    # which from the job's own plan.tools (the planner's pick).
    base: tuple[str, ...] = ()
    planned: tuple[str, ...] = ()


class McpClientError(Exception):
    """Any stdio MCP client failure (spawn, protocol, timeout). Always
    caught inside gather() -- never escapes it."""


class _StdioMcpClient:
    """Minimal synchronous MCP stdio client: newline-delimited JSON-RPC
    2.0 (the MCP stdio transport -- no Content-Length framing), one
    request in flight at a time. `initialize` + the
    `notifications/initialized` notification run at construction time;
    call_tool() does `tools/call`."""

    def __init__(self, command: tuple[str, ...], timeout_sec: float) -> None:
        self._timeout = timeout_sec
        try:
            self._proc = subprocess.Popen(
                list(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
        except OSError as exc:
            raise McpClientError(f"spawn failed: {exc}") from exc
        self._next_id = 1
        try:
            self._initialize()
        except McpClientError:
            self._kill()
            raise

    def _send(self, message: dict[str, object]) -> None:
        if self._proc.stdin is None:
            raise McpClientError("no stdin")
        try:
            self._proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise McpClientError(f"write failed: {exc}") from exc

    def _read_line_with_timeout(self) -> str:
        """Reads one line off stdout, bounded by `self._timeout` --
        `readline()` on a pipe blocks indefinitely on its own, so a
        stuck/slow server would otherwise hang gather()/build() forever.
        Read happens on a daemon thread; if it has not produced a line
        within the timeout, the server process is killed and a
        McpClientError raised. The reader thread itself is left to exit
        on its own once the killed process's stdout closes (its result,
        if any, is simply never collected)."""
        if self._proc.stdout is None:
            raise McpClientError("no stdout")
        result: "queue.Queue[str]" = queue.Queue(maxsize=1)

        def _reader() -> None:
            try:
                result.put(self._proc.stdout.readline())
            except (OSError, ValueError):
                result.put("")

        thread = threading.Thread(target=_reader, daemon=True)
        thread.start()
        thread.join(self._timeout)
        if thread.is_alive():
            self._kill()
            raise McpClientError(f"stdio MCP server timed out after {self._timeout}s")
        return result.get()

    def _kill(self) -> None:
        try:
            self._proc.kill()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=self._timeout)
        except Exception:
            pass

    def _read_message(self) -> dict[str, object]:
        line = self._read_line_with_timeout()
        if not line:
            raise McpClientError("stdio MCP server closed stdout")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise McpClientError(f"bad json from stdio MCP server: {exc}") from exc
        if not isinstance(message, dict):
            raise McpClientError("stdio MCP server response was not an object")
        return message

    def _request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        """Sends one JSON-RPC request and reads messages until one with
        a matching `id` arrives. A message without a matching id (e.g.
        a server notification sent ahead of the real reply) is not the
        reply and is skipped rather than mistaken for it."""
        request_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            message = self._read_message()
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise McpClientError(f"stdio MCP server error: {message['error']!r}")
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    def _initialize(self) -> None:
        self._request("initialize", {
            "protocolVersion": "", "capabilities": {},
            "clientInfo": {"name": "gongbang-engine", "version": "1"},
        })
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call_tool(self, name: str, arguments: dict[str, object]) -> list[dict[str, object]]:
        """Calls `tools/call`, expecting MCP's own result shape
        {"content": [{"type": "text", "text": <json array string>}]}
        where the text, parsed as JSON, is a list of plain result
        objects. Any content entry that isn't exactly that shape is
        skipped rather than raising."""
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        content = result.get("content")
        if not isinstance(content, list):
            return []
        items: list[dict[str, object]] = []
        for entry in content:
            if not isinstance(entry, dict) or entry.get("type") != "text":
                continue
            text = entry.get("text")
            if not isinstance(text, str):
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, list):
                items.extend(p for p in parsed if isinstance(p, dict))
        return items

    def close(self) -> None:
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=self._timeout)
        except Exception:
            try:
                self._proc.kill()
            except OSError:
                pass


def _library_items(client: _StdioMcpClient, request: str) -> list[ContextItem]:
    items: list[ContextItem] = []
    for entry in client.call_tool("library_search", {"query": request, "k": 5}):
        if entry.get("origin") == "run_output":
            continue
        items.append(ContextItem(
            kind="library", title=str(entry.get("title", "")),
            text=str(entry.get("text", "")),
            source=str(entry.get("source", "library")), trust="reference",
        ))
    return items


def _lesson_items(client: _StdioMcpClient, app: str) -> list[ContextItem]:
    items: list[ContextItem] = []
    for entry in client.call_tool("lessons_for", {"app": app}):
        if entry.get("origin") == "run_output":
            continue
        items.append(ContextItem(
            kind="lesson", title=str(entry.get("title", "")),
            text=str(entry.get("text", "")),
            source=str(entry.get("source", "lessons")), trust="experience",
        ))
    return items


def _decision_item(config: "EngineConfig", project: str) -> ContextItem | None:
    ctx_cfg = getattr(config, "context", None)
    if ctx_cfg is None:
        return None
    path = ctx_cfg.decisions_paths.get(project)
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    return ContextItem(kind="decision", title="CURRENT_DECISIONS", text=text,
                        source=path, trust="approved")


DIRECTION_STATIONS = ("facts", "plan")


def _direction_item(config: "EngineConfig", station: str) -> ContextItem | None:
    ctx_cfg = getattr(config, "context", None)
    path = getattr(ctx_cfg, "direction_path", None) if ctx_cfg is not None else None
    if not path or station not in DIRECTION_STATIONS:
        return None
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return None
    return ContextItem(kind="decision", title="방향 원문표", text=text,
                       source=str(path), trust="approved")


def gather(config: "EngineConfig", project: str, station: str, request: str) -> Context:
    """Collects every ContextItem this station is entitled to for this
    `request` (used as the library_search query): the project's own
    CURRENT_DECISIONS.md section (if configured) plus, if a
    `library_mcp` server is configured, library_search + lessons_for
    results (lessons only if `lessons_app` names this project). `station`
    is accepted for a uniform call shape across every caller even though
    this function itself does not yet vary behavior by station -- skill
    selection (which DOES vary by station) lives in build() below."""
    items: list[ContextItem] = []
    failed = False
    ctx_cfg = getattr(config, "context", None)

    direction = _direction_item(config, station)
    if direction is not None:
        items.append(direction)

    decision = _decision_item(config, project)
    if decision is not None:
        items.append(decision)

    if ctx_cfg is not None and ctx_cfg.library_mcp is not None:
        app = ctx_cfg.lessons_app.get(project)
        client: _StdioMcpClient | None = None
        try:
            client = _StdioMcpClient(ctx_cfg.library_mcp.command, ctx_cfg.library_mcp.timeout_sec)
            items.extend(_library_items(client, request))
            if app:
                items.extend(_lesson_items(client, app))
        except McpClientError:
            failed = True
        finally:
            if client is not None:
                client.close()

    return Context(items=tuple(items), failed=failed)


def render_items(items: tuple[ContextItem, ...]) -> str:
    """Renders `items` into a "## 주입된 자료" block, each item's text
    capped to PER_ITEM_CAP characters and the whole block capped to
    TOTAL_SECTION_CAP characters (items past the cap are dropped, not
    truncated mid-item). The owner's own approved text (decisions,
    direction quotes) is never cut and never counts against the cap:
    it is sent whole. Empty string for no items."""
    if not items:
        return ""
    header = "## 주입된 자료"
    blocks = [header]
    used = len(header)
    for item in items:
        approved = item.trust == "approved"
        text = item.text if approved else item.text[:PER_ITEM_CAP]
        label = _TRUST_LABEL.get(item.trust, item.trust)
        block = f"\n\n### [{label}] {item.title} ({item.source})\n{text}"
        if approved:
            blocks.append(block)
            continue
        if used + len(block) > TOTAL_SECTION_CAP:
            break
        blocks.append(block)
        used += len(block)
    return "".join(blocks)


def _skills_section(roots: tuple[str, ...], names: tuple[str, ...]) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    loaded: list[str] = []
    missing: list[str] = []
    bodies: list[str] = []
    for name in names:
        skill = load_skill(roots, name)
        if skill.body is None:
            missing.append(name)
        else:
            loaded.append(name)
            bodies.append(f"\n\n### {name}\n{skill.body}")
    text = ""
    if bodies:
        text += "## 이 칸의 스킬" + "".join(bodies)
    if missing:
        if text:
            text += "\n\n"
        text += "## 이 칸의 스킬 (없음)\n" + "\n".join(f"- {name}" for name in missing)
    return text, tuple(loaded), tuple(missing)


def build(config: "EngineConfig", project: str, station: str, request: str,
          planned_skills: tuple[str, ...] = ()) -> StationContext:
    """One call a station-prompt-building call site makes: gathers
    context items, loads this station's configured skills, and
    assembles the full appendix text (library/lesson/decision items,
    skill bodies, missing-skill notice, and -- on a library client
    failure -- the "곳간 조회 실패" line). The caller (engine.full_orbit)
    appends `.text` to its own prompt and records `.injected`/`.skills`/
    `.missing`/`.failed` as the `context.injected`/`context.failed`
    ledger events."""
    ctx_cfg = getattr(config, "context", None)
    ctx = gather(config, project, station, request)
    roots = ctx_cfg.skill_roots if ctx_cfg is not None else ()
    base = tuple(ctx_cfg.station_skills.get(station, ())) if ctx_cfg is not None else ()
    # The planner's own pick for this station comes on top of the small
    # fixed base set; the planned list is the main path, not an extra.
    planned = tuple(n for n in dict.fromkeys(planned_skills) if n not in base)
    names = base + planned
    skills_text, loaded, missing = _skills_section(roots, names)

    parts = []
    items_text = render_items(ctx.items)
    if items_text:
        parts.append(items_text)
    if skills_text:
        parts.append(skills_text)
    if ctx.failed:
        parts.append("곳간 조회 실패")
    text = "\n\n".join(parts)

    return StationContext(base=base, planned=planned, text=text, injected=ctx.items, skills=loaded,
                          missing=missing, failed=ctx.failed)


__all__ = [
    "ContextItem", "Context", "StationContext", "McpClientError",
    "LibraryMcpConfig", "ContextConfig",
    "gather", "render_items", "build", "PER_ITEM_CAP", "TOTAL_SECTION_CAP",
]
