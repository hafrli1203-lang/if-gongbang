"""PreToolUse hook gate: turn a Claude Code hook call into a
engine.policy.Policy decision.

Run as `python -m engine.gate`. Reads one Claude Code PreToolUse hook JSON
object from stdin; environment variables GB_CONFIG, GB_PROJECT, GB_ATTEMPT,
GB_GENERATION, GB_STATION, GB_ROLE, GB_COPY_ROOT describe the worker
process asking.

This process NEVER opens the ledger (ENGINE.md 「한계」: the worker
sandbox no longer has the ledger directory writable at all, so a
`Ledger.open` here would simply fail -- and should, since letting
worker-run code reach the real ledger was the actual problem). Instead:
GB_GATE_FACTS names a JSON file (station.py writes it fresh before
spawning the worker, from a real ledger read) this process loads into
a `engine.policy.ProjectFacts`; GB_GATE_LOG names a plain file (also
pre-created by station.py, in the stations dir -- worker-writable) this
process appends its own decisions to, via `engine.policy.FileSink`,
instead of the ledger. `record_station` imports every line of that log
into the real ledger afterward (see engine/steps/station.py). Either
env var missing, or its file missing/unparseable, is a fail-closed
deny ("gate_facts_missing" / "gate_log_missing") -- never a silent
fall-back to some other source of truth.

Exit code matters here, not just stdout, because of how the calling
Claude CLI actually behaves on a PreToolUse hook (measured directly,
not assumed): hook exit 1 -> tool PROCEEDS (fails OPEN); hook exit 2 ->
tool is BLOCKED (stderr shown to the model); a hook that times out ->
tool PROCEEDS (fails OPEN) regardless of any `"onFailure"` field in the
hook definition, which this CLI version does not honour. So:

 - A normal decision (decide() returned, allow or deny) prints the
   {"hookSpecificOutput": {...}} JSON to stdout and exits 0 -- Claude
   Code reads permissionDecision from that JSON, which is correct for
   both "allow" and "deny" (a deny with exit 0 still blocks the tool;
   it just also gives the model a reason instead of a bare block).
 - Any exception while producing that decision (malformed stdin JSON,
   missing env var, missing/bad config, a LedgerError, a Policy
   exception) -- a case where we have no trustworthy JSON output to
   show -- logs the reason to stderr and exits 2 (hard block), rather
   than printing a deny JSON with exit 0. The caller's own command
   string additionally wraps the interpreter invocation in
   `sh -c '... || exit 2'` (see claude_cli._gate_settings) so that even
   a startup failure before this module's own code runs (missing
   interpreter, import error) still exits 2.
 - A SIGALRM watchdog (signal.alarm(10), see _install_watchdog) covers
   the remaining case: this process itself hanging (a stuck ledger
   lock, a hung Policy hook script) past its own exception handling.
   Its handler writes one line to stderr and calls os._exit(2)
   directly, since a signal handler cannot rely on the hung code ever
   returning control to it. The Claude hook's own `timeout` is set to
   30s specifically so our 10s alarm fires well before it -- the
   residual fail-open window is only "this process becomes truly
   unkillable to its own SIGALRM delivery", which the surrounding 30s
   CLI timeout still bounds (fails OPEN at that point, not blocked).

Tool-name to Action mapping:
  Read/Glob/Grep/LS/NotebookRead      -> read,  path = file_path or path
                                                        or hook cwd
  Edit/Write/MultiEdit/NotebookEdit   -> write, path = file_path
                                                        (missing -> deny)
  Bash                                -> terminal, command = command
  mcp__<server>__<tool>               -> mcp, target = <server> (the
                                          bit between the first and
                                          second "__"), mcp_tool =
                                          <tool> (everything after the
                                          second "__", passed through
                                          for engine.policy's optional
                                          per-server mcp_tool_allow
                                          check); malformed (no second
                                          "__", or nothing after it) ->
                                          deny "tool_not_allowed"
                                          without calling Policy, same
                                          as any other unrecognized
                                          tool.
  anything else (WebFetch, Task, ...)  -> deny "tool_not_allowed"
                                                  without calling Policy,
                                                  recorded as a
                                                  'gate.denied' event
                                                  (via the same sink
                                                  as Policy's own
                                                  'policy.decided')
                                                  instead of
                                                  'policy.decided'.
Relative paths are resolved against the hook's own "cwd" field.

Path-like fields (file_path, path, notebook_path) are stripped and
checked for a literal "~" path component or embedded NUL/newline before
being resolved -- Claude Code itself expands "~" and trims whitespace
in these fields before invoking a tool, so a hook that checked the raw
string could be shown `copy/~/...` and compare that literal (which
looks like a normal path inside the copy) while the tool actually acts
on the expanded home-directory path. We do not re-expand and allow;
any such field is denied outright ("ambiguous_path"). Glob's `pattern`
and Grep's `path`/`glob`/`include` fields get the same treatment plus
an absolute-path/".."-component check ("pattern_escapes"), since Glob
and Grep search FROM a root rather than taking a single target path.

Known limit: a worker copy's own `.git/` and `.gitignore` are protected
from Edit/Write (engine.policy rule 4), but a recursive Grep/Glob whose
*root* is allowed (e.g. the copy root itself) can still have those
files turn up among its read results -- the gate has no way to exclude
individual descendants from a single recursive read action, only to
allow or deny the root path being searched.
"""
from __future__ import annotations

import json
import os
import re
import signal
import sys
import unicodedata
from pathlib import Path
from types import FrameType
from typing import Mapping

from engine.config import EngineConfig
from engine.ledger import Event
from engine.policy import Action, Actor, FileSink, Policy, ProjectFacts

_READ_TOOLS = ("Read", "Glob", "Grep", "LS", "NotebookRead")
_WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
_MCP_PREFIX = "mcp__"


class _GateInputMissing(Exception):
    """GB_GATE_FACTS/GB_GATE_LOG is unset, or its file is missing or
    unparseable -- a DENY decision (not main()'s generic exit-2 hard
    block: decide() still produces a trustworthy, printable JSON
    output here, it is just always "deny"), per module docstring."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _AmbiguousPath(Exception):
    """Raised by _clean_path_field/_check_pattern_field when a path-like
    hook field cannot be trusted at face value (see module docstring:
    ~-expansion/whitespace-trimming happens in Claude before file_path
    reaches us, so the gate must re-normalize the same way before
    comparing against the literal string, or an escape disguised as
    `copy/~/...` would be checked as if it were a real path inside the
    copy)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _output(allowed: bool, reason: str) -> dict[str, object]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if allowed else "deny",
            "permissionDecisionReason": reason,
        }
    }


def _has_nul_or_newline(raw: str) -> bool:
    return "\x00" in raw or "\n" in raw or "\r" in raw


def _has_tilde_component(stripped: str) -> bool:
    if stripped.startswith("~"):
        return True
    for part in re.split(r"[\\/]", stripped):
        if part.startswith("~"):
            return True
    return False


def _has_hidden_chars(raw: str) -> bool:
    """True if `raw` contains any Unicode control/format char (U+FEFF BOM,
    zero-width joiners, ...) or any whitespace other than a plain space.
    Claude's own trim() strips such characters before use, so a path
    like "\ufeff~/.ssh/x" would reach the tool as "~/.ssh/x" while the
    literal string looked harmless here -- deny the spelling outright."""
    for ch in raw:
        cat = unicodedata.category(ch)
        if cat.startswith("C") or (cat.startswith("Z") and ch != " "):
            return True
    return False


def _clean_path_field(raw: str) -> str:
    """Normalize a hook path-like field the same way Claude normalizes
    `file_path`/`path` before using it (strip whitespace, expand `~`)
    -- except here that normalization is only used to DETECT an escape,
    never to resolve one: a stripped/tilde-bearing value is denied
    outright rather than being resolved and allowed. See module
    docstring and _AmbiguousPath."""
    if _has_nul_or_newline(raw) or _has_hidden_chars(raw):
        raise _AmbiguousPath("ambiguous_path")
    stripped = raw.strip()
    if not stripped:
        raise _AmbiguousPath("ambiguous_path")
    if _has_tilde_component(stripped):
        raise _AmbiguousPath("ambiguous_path")
    return stripped


def _check_pattern_field(raw: object) -> None:
    """Glob's `pattern` and Grep's `path`/`glob`/`include` fields are not
    run through _resolve (Glob/Grep search FROM a root, the pattern/glob
    is relative to it) but can still escape the copy: an absolute
    pattern, one containing a `..` component, or one containing `~`
    reaches outside the root the gate actually checked. Raises
    _AmbiguousPath("pattern_escapes") rather than returning a bool so
    callers cannot forget to act on it."""
    if raw is None:
        return
    text = str(raw)
    if _has_nul_or_newline(text) or _has_hidden_chars(text):
        raise _AmbiguousPath("pattern_escapes")
    stripped = text.strip()
    if not stripped:
        return
    if Path(stripped).is_absolute() or _has_tilde_component(stripped):
        raise _AmbiguousPath("pattern_escapes")
    for part in re.split(r"[\\/]", stripped):
        if part == "..":
            raise _AmbiguousPath("pattern_escapes")


def _mcp_server_and_tool(tool_name: str) -> tuple[str, str] | None:
    """"mcp__<server>__<tool>" -> (<server>, <tool>); None if `tool_name`
    does not have the "mcp__" prefix followed by a second "__" and a
    non-empty tool part (so neither half is ever empty or ambiguous --
    a malformed name is treated as an unrecognized tool, not as some
    server/tool with an empty name)."""
    if not tool_name.startswith(_MCP_PREFIX):
        return None
    rest = tool_name[len(_MCP_PREFIX):]
    server, sep, tool = rest.partition("__")
    if not sep or not server or not tool:
        return None
    return server, tool


def _load_facts(path_str: str | None, *, expected_project: str) -> ProjectFacts:
    """Loads and validates the ProjectFacts snapshot, refusing (same
    fail-closed deny as a missing file) anything that does not match
    the EXACT expected shape -- `generation` must be a real int, not
    merely int()-convertible: `int("1.9")` already raises, but
    `int(1.9)` silently truncates to 1, which would let a corrupted or
    tampered facts file with a float generation be accepted as a
    DIFFERENT (smaller) generation than it actually claims. `project`
    must equal the trusted GB_PROJECT this process was started with --
    a facts file naming some other project is never a legitimate
    engine-written snapshot for THIS worker."""
    if not path_str:
        raise _GateInputMissing("gate_facts_missing")
    try:
        raw = json.loads(Path(path_str).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise _GateInputMissing("gate_facts_missing") from None
    if not isinstance(raw, dict):
        raise _GateInputMissing("gate_facts_missing")
    try:
        generation_raw = raw["generation"]
        if isinstance(generation_raw, bool) or not isinstance(generation_raw, int):
            raise ValueError("generation is not an int")
        owns_raw = raw.get("owns")
        if owns_raw is not None:
            if not isinstance(owns_raw, list) or not all(isinstance(v, str) for v in owns_raw):
                raise ValueError("owns is not a list of strings")
            owns: tuple[str, ...] | None = tuple(owns_raw)
        else:
            owns = None
        mcp_allow_raw = raw.get("mcp_allow", [])
        if not isinstance(mcp_allow_raw, list) or not all(isinstance(v, str) for v in mcp_allow_raw):
            raise ValueError("mcp_allow is not a list of strings")
        facts = ProjectFacts(
            project=str(raw["project"]),
            registered=bool(raw["registered"]),
            state=str(raw["state"]),
            generation=generation_raw,
            attempt_id=(str(raw["attempt_id"]) if raw.get("attempt_id") is not None else None),
            attempt_active=bool(raw["attempt_active"]),
            owns=owns,
            mcp_allow=tuple(mcp_allow_raw),
        )
    except (KeyError, TypeError, ValueError):
        raise _GateInputMissing("gate_facts_missing") from None
    if facts.project != expected_project:
        raise _GateInputMissing("gate_facts_missing")
    return facts


def _open_log_sink(path_str: str | None) -> FileSink:
    if not path_str or not Path(path_str).exists():
        raise _GateInputMissing("gate_log_missing")
    return FileSink(Path(path_str))


def _resolve(raw: str, cwd: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = Path(cwd) / path
    return path


def decide(hook_input: dict[str, object], env: Mapping[str, str]) -> dict[str, object]:
    """Resolve the hook call to an Action, consult Policy, and return the
    hook output dict. Raises on any problem main() should hard-block on
    (missing env, bad config, ...) — decide() itself stays a
    straightforward translation so tests can see real exceptions when
    something is actually wrong. GB_GATE_FACTS/GB_GATE_LOG missing or
    unusable is the one exception to that: it is instead a plain DENY
    output (see module docstring and _GateInputMissing)."""
    tool_name = hook_input.get("tool_name")
    raw_tool_input = hook_input.get("tool_input")
    tool_input = raw_tool_input if isinstance(raw_tool_input, dict) else {}
    cwd = str(hook_input.get("cwd") or ".")

    config = EngineConfig.from_json(Path(env["GB_CONFIG"]))
    project = env["GB_PROJECT"]

    try:
        facts = _load_facts(env.get("GB_GATE_FACTS"), expected_project=project)
        sink = _open_log_sink(env.get("GB_GATE_LOG"))
    except _GateInputMissing as exc:
        return _output(False, exc.reason)

    actor = Actor(
        kind="worker",
        role=env["GB_ROLE"],
        attempt_id=(env.get("GB_ATTEMPT") or None),
        generation=int(env["GB_GENERATION"]),
        station=(env.get("GB_STATION") or None),
        copy_root=Path(env["GB_COPY_ROOT"]),
    )

    action: Action | None = None
    try:
        if tool_name in _READ_TOOLS:
            raw_path = (tool_input.get("file_path") or tool_input.get("path")
                        or tool_input.get("notebook_path") or cwd)
            clean = _clean_path_field(str(raw_path))
            action = Action(tool="read", path=_resolve(clean, cwd))
            if tool_name == "Glob":
                _check_pattern_field(tool_input.get("pattern"))
            if tool_name == "Grep":
                # Grep's `pattern` is a CONTENT regex ("/api/users" is
                # ordinary text), only its file filters select paths.
                _check_pattern_field(tool_input.get("glob"))
                _check_pattern_field(tool_input.get("include"))
        elif tool_name in _WRITE_TOOLS:
            raw_path = tool_input.get("file_path") or tool_input.get("notebook_path")
            if raw_path is None:
                return _output(False, "missing_path")
            clean = _clean_path_field(str(raw_path))
            action = Action(tool="write", path=_resolve(clean, cwd))
        elif tool_name == "Bash":
            command = tool_input.get("command")
            action = Action(tool="terminal",
                             command=(str(command) if command is not None else None))
        elif isinstance(tool_name, str) and (server_and_tool := _mcp_server_and_tool(tool_name)):
            server, mcp_tool = server_and_tool
            action = Action(tool="mcp", target=server, mcp_tool=mcp_tool)
        else:
            sink.record(Event(
                project=project, kind="gate.denied", actor_kind="worker",
                actor_id=actor.role, attempt_id=actor.attempt_id,
                payload={"tool_name": tool_name, "reason": "tool_not_allowed"},
            ))
            return _output(False, "tool_not_allowed")
    except _AmbiguousPath as exc:
        return _output(False, exc.reason)

    policy = Policy(config, facts=facts, sink=sink)
    decision = policy.check(project, actor, action)
    return _output(decision.allowed, decision.reason)


WATCHDOG_TIMEOUT_SEC = 10


def _handle_watchdog_alarm(signum: int, frame: FrameType | None) -> None:
    """SIGALRM handler: this process has been running longer than
    WATCHDOG_TIMEOUT_SEC, which means something inside decide() (a
    stuck ledger lock, a hung Policy hook script) is not returning. A
    handler cannot assume the interrupted code will ever hand control
    back normally, so it exits immediately rather than raising/
    returning -- os._exit (not sys.exit) skips cleanup that could
    itself block, matching the "exit 2 = blocked" contract main()
    otherwise returns from decide() path explicitly."""
    print("gate_timeout", file=sys.stderr)
    os._exit(2)


def _install_watchdog() -> None:
    signal.signal(signal.SIGALRM, _handle_watchdog_alarm)
    signal.alarm(WATCHDOG_TIMEOUT_SEC)


def main() -> int:
    _install_watchdog()
    raw_stdin = sys.stdin.read()
    try:
        hook_input = json.loads(raw_stdin)
        if not isinstance(hook_input, dict):
            raise ValueError("hook input must be a JSON object")
        output = decide(hook_input, os.environ)
    except Exception as exc:
        # No trustworthy JSON decision to show: log and hard-block
        # (exit 2) rather than printing a deny JSON with exit 0 -- see
        # module docstring for why exit code, not stdout, is what
        # actually blocks the tool on this CLI.
        print(f"gate_error: {exc!r}", file=sys.stderr)
        signal.alarm(0)
        return 2
    signal.alarm(0)
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
