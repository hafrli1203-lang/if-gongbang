"""The two things that make the gate hook work for a station worker:
writing the gate_facts.json/engine_config.json snapshot it reads
BEFORE the worker runs, and importing the gate.log it (and the gate
hook) WRITE while it runs back into the real ledger AFTER. Split out
of engine.application.steps.worker_step, which still owns spawning/awaiting/recording
the worker itself.
"""
from __future__ import annotations

import fcntl
import json
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from engine.domain import ids
from engine.config import McpServerConfig, to_json_dict
from engine.domain.records import Event, LedgerError
from engine.domain.policy import ProjectFacts
from engine.application.processor import StepContext, StepFailed

if TYPE_CHECKING:
    from engine.infrastructure.workers.base import WorkerSpec


def _mkdir_chain_no_symlink(base: Path, *parts: str) -> Path:
    """mkdir every missing component under `base` (assumed already a
    safe, pre-existing directory -- created once at config/job-open
    time, never per-request), refusing if any existing component along
    the way is a symlink or not a plain directory. `gate-ro`'s own leaf
    directories are created per request, and request ids are
    deterministic, so an EARLIER request's worker could in principle
    predict a FUTURE one's pathname -- this stays defensive about that
    even though, by construction, the worker never has write access to
    anything under `base` in the first place once the sandbox mounts
    are in place."""
    current = base
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise StepFailed("gate_ro_path_unsafe")
        try:
            current.mkdir()
        except FileExistsError:
            if current.is_symlink() or not current.is_dir():
                raise StepFailed("gate_ro_path_unsafe") from None
        except OSError as exc:
            raise StepFailed("gate_ro_path_unsafe") from exc
    return current


def _write_new_file_no_symlink(path: Path, data: bytes, *, allow_replace_plain: bool) -> None:
    """Write `path` without ever following a symlink at its final
    component (O_NOFOLLOW: a symlink planted there makes the open fail
    rather than writing through it). An existing PLAIN regular file may
    be replaced when `allow_replace_plain` -- a resumed spawn_worker
    re-writing its own facts/config before any worker has ever run for
    this request, the only case this function is called more than once
    for the same path -- but a symlink there is always refused outright
    (opened with O_CREAT|O_EXCL|O_NOFOLLOW)."""
    if path.is_symlink():
        raise StepFailed("gate_ro_path_unsafe")
    if path.exists():
        if not (allow_replace_plain and path.is_file()):
            raise StepFailed("gate_ro_path_unsafe")
        path.unlink()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o644)
    except OSError as exc:
        raise StepFailed("gate_ro_path_unsafe") from exc
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _touch_no_symlink(path: Path) -> None:
    """Create `path` if missing without ever following a symlink --
    same O_NOFOLLOW reasoning as _write_new_file_no_symlink, but for
    the log file, which stays worker-writable for its whole life (so
    idempotent re-touch, never O_EXCL, is correct here)."""
    if path.is_symlink():
        raise StepFailed("gate_ro_path_unsafe")
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o644)
    except OSError as exc:
        raise StepFailed("gate_ro_path_unsafe") from exc
    os.close(fd)


def task_owns(ctx: StepContext) -> tuple[str, ...] | None:
    """The optional "owns" entry of this run_station request's payload
    (engine.application.pipeline's build loop: a per-task narrowing of the
    build station's own configured write prefixes -- see
    engine.domain.policy.ProjectFacts' own docstring). None if the payload
    carries no such key (every run_station caller that predates this
    field, and every non-build station) -- unchanged, unnarrowed
    behavior."""
    raw = ctx.request.payload.get("owns")
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
        raise StepFailed("invalid_owns")
    return tuple(raw)


def _payload_mcp_names(payload: dict[str, object]) -> tuple[str, ...]:
    """The "mcp" list of this request's optional "tools" payload entry
    (engine.domain.plan.tools_for_station's shape: {"skill": [...], "mcp":
    [...]}) -- () for a payload that carries no "tools" key at all
    (every caller that predates this field)."""
    tools = payload.get("tools")
    if not isinstance(tools, dict):
        return ()
    raw = tools.get("mcp", [])
    if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
        raise StepFailed("invalid_tools_mcp")
    return tuple(raw)


def allowed_mcp_servers(ctx: StepContext, station: str) -> dict[str, McpServerConfig]:
    """This request's payload "mcp" names, narrowed to BOTH
    config.station_mcp_allow[station] (the project-wide upper bound a
    plan's own tool table can never widen past) AND config.mcp_servers
    (a name the operator never configured launch info for is dropped,
    not trusted). The SAME narrowing engine.domain.policy._decide_mcp repeats
    independently from gate_facts.json's "mcp_allow" -- this result
    only controls what the worker CLI is launched *able* to call; the
    gate is what actually blocks an unlisted one at tool-call time."""
    requested = set(_payload_mcp_names(ctx.request.payload))
    station_allow = set(ctx.config.station_mcp_allow.get(station, ()))
    names = sorted(requested & station_allow & set(ctx.config.mcp_servers))
    return {name: ctx.config.mcp_servers[name] for name in names}


def _build_project_facts(ctx: StepContext, owns: tuple[str, ...] | None = None,
                         mcp_allow: tuple[str, ...] = ()) -> ProjectFacts:
    """A ProjectFacts snapshot taken HERE (the engine process, real
    ledger access) for the gate hook (no ledger access at all, see
    engine.interface.hook_gate's module docstring) to decide with instead."""
    row = ctx.ledger.project(ctx.project)
    attempt_active = False
    if row.current_attempt_id is not None:
        try:
            attempt_active = ctx.ledger.attempt(row.current_attempt_id).status == "active"
        except LedgerError:
            attempt_active = False
    return ProjectFacts(
        project=ctx.project, registered=True, state=row.state,
        generation=row.generation, attempt_id=row.current_attempt_id,
        attempt_active=attempt_active, owns=owns, mcp_allow=mcp_allow,
    )


def write_gate_facts(ctx: StepContext) -> None:
    """Writes gate_facts.json (and, if this project has a config file,
    a copy of it as engine_config.json) into the fresh, symlink-checked
    gate-ro dir -- see station_paths.gate_ro_dir's docstring for why
    this directory, not the stations dir, is where these live, and why
    the config is copied here too rather than mounted from its
    original location: GB_CONFIG then always points inside the one
    directory the sandbox mounts read-only and remounts immutable,
    regardless of where the original config file happens to sit on the
    host.

    The config copy is `to_json_dict(ctx.config)` -- the ALREADY
    in-memory-resolved config, every path absolute -- never a raw byte
    copy of the original file. The original file can itself contain
    relative paths (e.g. a `hooks` entry written as a path relative to
    the original config's own directory); copying its bytes verbatim
    into gate-ro and then loading it from THAT directory would resolve
    those same relative paths against the wrong base and silently
    relocate them (e.g. a hook script the gate is supposed to run).
    Serializing the resolved, absolute-path config instead means the
    copy means the same thing regardless of which directory it is
    loaded from."""
    station = str(ctx.request.payload["station"])
    mcp_allow = tuple(sorted(allowed_mcp_servers(ctx, station)))
    facts = _build_project_facts(ctx, owns=task_owns(ctx), mcp_allow=mcp_allow)
    ro_dir = _mkdir_chain_no_symlink(
        ctx.config.home, "jobs", ids.attempt_dirname(ctx.attempt_id),
        "gate-ro", ctx.request.request_id,
    )
    data = json.dumps({
        "project": facts.project, "registered": facts.registered,
        "state": facts.state, "generation": facts.generation,
        "attempt_id": facts.attempt_id, "attempt_active": facts.attempt_active,
        "owns": (list(facts.owns) if facts.owns is not None else None),
        "mcp_allow": list(facts.mcp_allow),
    }).encode("utf-8")
    _write_new_file_no_symlink(ro_dir / "gate_facts.json", data, allow_replace_plain=True)
    if ctx.config.config_path is not None:
        config_data = json.dumps(to_json_dict(ctx.config), ensure_ascii=False).encode("utf-8")
        _write_new_file_no_symlink(ro_dir / "engine_config.json", config_data,
                                    allow_replace_plain=True)


def init_gate_log(ctx: StepContext) -> None:
    log_dir = _mkdir_chain_no_symlink(
        ctx.config.home, "jobs", ids.attempt_dirname(ctx.attempt_id),
        "stations", ctx.request.request_id,
    )
    _touch_no_symlink(log_dir / "gate.log")


_GATE_LOG_MAX_BYTES = 8 * 1024 * 1024  # 8 MiB

# The ONLY two kinds ever accepted from the worker-writable gate.log,
# and the exact field set/types allowed in each one's payload -- every
# OTHER field is rejected (the line is malformed), never passed
# through, since a worker controls every byte of this file.
# "allowed"/"reason"/"tool"/"path"/"target"/"command"
# mirror engine.domain.policy.Policy.check's own real 'policy.decided' payload
# exactly; "tool_name"/"reason" mirror engine.interface.hook_gate's own real
# 'gate.denied' payload for an unrecognized tool.
_POLICY_DECIDED_FIELDS: dict[str, int] = {
    "reason": 80, "tool": 20, "path": 400, "target": 400, "command": 400,
}
_GATE_DENIED_FIELDS: dict[str, int] = {"reason": 80, "tool_name": 200}


def _validate_gate_log_payload(kind: str, payload: dict[str, object]) -> dict[str, object] | None:
    """A cleaned, type/length-checked payload for the one allowed field
    set of `kind`, or None if `payload` does not match at all (the
    line is then malformed) -- see import_gate_log."""
    if kind == "policy.decided":
        str_fields = _POLICY_DECIDED_FIELDS
        if "allowed" not in payload or not isinstance(payload["allowed"], bool):
            return None
        cleaned: dict[str, object] = {"allowed": payload["allowed"]}
        required = ("reason",)
    elif kind == "gate.denied":
        str_fields = _GATE_DENIED_FIELDS
        cleaned = {}
        required = ("reason",)
    else:
        return None
    allowed_keys = set(str_fields) | ({"allowed"} if kind == "policy.decided" else set())
    if set(payload) - allowed_keys:
        return None
    for key in required:
        if key not in payload:
            return None
    for key, max_len in str_fields.items():
        if key not in payload:
            continue
        value = payload[key]
        if value is None:
            cleaned[key] = None
            continue
        if not isinstance(value, str) or len(value) > max_len:
            return None
        cleaned[key] = value
    return cleaned


class _GateLogOversize(Exception):
    def __init__(self, size: int) -> None:
        self.size = size
        super().__init__(f"gate log {size} bytes exceeds cap")


def _read_gate_log_no_symlink(path: Path) -> bytes | None:
    """The log's raw bytes, or None if it does not exist. Every
    directory component of `path` -- not just the final one -- is
    opened individually with O_NOFOLLOW via `dir_fd`: `os.open(path,
    O_NOFOLLOW)` by itself only refuses a symlink at the LAST path
    element, so a worker that replaces the writable per-request
    directory this log normally lives in (`path.parent`) with a
    symlink could otherwise redirect this read to an attacker-chosen
    file through an unprotected intermediate component. The leaf file
    is opened O_RDONLY|O_NONBLOCK: a FIFO planted there (not a
    symlink -- O_NOFOLLOW does not apply to a FIFO) would otherwise
    block this open indefinitely waiting for a writer. Once `fstat`
    confirms a plain regular file, O_NONBLOCK is cleared via `fcntl`
    before anything is read (regular-file reads are unaffected by it,
    but clearing it keeps the fd in its ordinary blocking state for
    any future use). Capped at _GATE_LOG_MAX_BYTES (raises
    _GateLogOversize rather than reading an unbounded amount of
    worker-controlled data into memory)."""
    # NEVER pre-resolve `path` (e.g. via realpath) before walking it --
    # that would follow exactly the symlink this function exists to
    # refuse. Each raw component is opened one at a time instead; any
    # one of them being a symlink (other than the anchor itself) makes
    # the corresponding O_NOFOLLOW open fail.
    components = path.parts
    try:
        dir_fd = os.open(components[0], os.O_DIRECTORY)
    except FileNotFoundError:
        return None
    try:
        for part in components[1:-1]:
            try:
                next_fd = os.open(part, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise StepFailed("gate_log_path_unsafe") from exc
            os.close(dir_fd)
            dir_fd = next_fd
        try:
            fd = os.open(components[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                        dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StepFailed("gate_log_path_unsafe") from exc
    finally:
        os.close(dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise StepFailed("gate_log_path_unsafe")
        if st.st_size > _GATE_LOG_MAX_BYTES:
            raise _GateLogOversize(st.st_size)
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
        return os.read(fd, st.st_size)
    finally:
        os.close(fd)


def _existing_gate_log_lines(
    ctx: StepContext, request_id: str,
) -> tuple[dict[int, dict[str, object]], set[int]]:
    """(decision_payloads, malformed_line_numbers) for THIS request_id
    only -- loaded from the ledger ONCE per import call, not once per
    log line. Re-querying ctx.ledger.events() (an O(total events in
    the project) scan) inside the per-line loop made a full import
    quadratic in the number of lines, letting a worker with a large
    (still under the 8 MiB cap) gate.log monopolize the processor
    while holding its lock."""
    decisions: dict[int, dict[str, object]] = {}
    for _seq, event in ctx.ledger.events(ctx.project, "gate_log.decision"):
        if event.payload.get("request_id") == request_id:
            line_no = event.payload.get("log_line")
            if isinstance(line_no, int):
                decisions[line_no] = event.payload
    malformed: set[int] = set()
    for _seq, event in ctx.ledger.events(ctx.project, "gate_log.malformed"):
        if event.payload.get("request_id") == request_id:
            line_no = event.payload.get("log_line")
            if isinstance(line_no, int):
                malformed.add(line_no)
    return decisions, malformed


def _gate_log_event_already_recorded(ctx: StepContext, kind: str, request_id: str) -> bool:
    """Idempotency for the one-shot oversize/missing audit events below
    -- a crash between appending one of these and the final
    'gate_log.imported' marker must not duplicate it on resume."""
    for _seq, event in ctx.ledger.events(ctx.project, kind):
        if event.payload.get("request_id") == request_id:
            return True
    return False


def _journal_gate_log_malformed(ctx: StepContext, request_id: str, line_no: int,
                                 raw_line: str) -> None:
    ctx.ledger.append(Event(
        project=ctx.project, kind="gate_log.malformed", actor_kind="processor",
        actor_id="processor", attempt_id=ctx.attempt_id,
        payload={"request_id": request_id, "log_line": line_no, "raw": raw_line[:200]},
    ))


def gate_log_already_imported(ctx: StepContext) -> bool:
    for _seq, event in ctx.ledger.events(ctx.project, "gate_log.imported"):
        if event.payload.get("request_id") == ctx.request.request_id:
            return True
    return False


def import_gate_log(ctx: StepContext, spec: WorkerSpec) -> None:
    """Imports this request's gate.log (the gate hook's own, worker-
    writable decision log -- see engine.interface.hook_gate's module docstring) into
    the real ledger -- but NEVER by trusting a single byte of the log
    line's own 'kind'/'project'/'attempt_id'/'actor_id' fields. Those
    fields, if forwarded verbatim into a real ledger.append() (which
    dispatches purely on `kind`, with no awareness of who is asking),
    would let a worker-forged line like {"kind": "approval.granted",
    ...} or {"kind": "station.finished", ...} trigger that kind's real
    derived-table handler (or satisfy a reconcile() query keyed on that
    kind) -- a worker could grant itself an approval, or make
    record_station's own reconcile believe this request was already
    finished, by writing one line to a file it already had write
    access to for an entirely different reason (the audit log).

    Every imported line is instead stored under ONE single, inert kind
    -- 'gate_log.decision' -- which has no _HANDLERS entry (see
    engine.infrastructure.db.ledger) and is never matched by any reconcile() query in
    this codebase (none of which ever look for that kind): forging
    ANY content into it can therefore never trigger a derived-table
    effect or make a later step believe something happened that did
    not. engine.infrastructure.db.ledger.Ledger.append() also independently refuses
    (actor_kind="gate_log") any kind other than 'gate_log.decision' as
    defense in depth, so even a future bug here that reintroduced
    verbatim forwarding would still not reach a real handler.

    project/attempt_id/actor_id/request_id on the appended event all
    come from `ctx`/`spec` -- the trusted engine context -- NEVER from
    the log line, which only survives (if it parses) as 'orig_kind'
    plus a strictly validated payload subset (see
    _validate_gate_log_payload): a worker cannot change what project,
    attempt or request its own forged lines get attributed to either.

    A line that is not valid JSON, not an object, has a 'kind' other
    than 'policy.decided'/'gate.denied', or has a payload that does not
    match that kind's exact allowed-field schema is never appended as
    a decision at all -- it is counted malformed and recorded as its
    own 'gate_log.malformed' event instead (same inert treatment: no
    handler, no reconcile match). The log itself is opened with
    O_NOFOLLOW, must be a plain regular file, and is capped at 8 MiB
    (over that: a single 'gate_log.oversize' audit event, nothing
    imported); a missing log yields one 'gate_log.missing' event.

    The whole import is crash-idempotent: each line is skipped if an
    event already exists for its exact (request_id, log_line) --
    whether from an earlier, interrupted run of this exact function --
    and the final 'gate_log.imported' marker (whose allowed/denied/
    malformed totals are always a fresh tally over the ledger's own
    gate_log.decision/malformed events for this request_id, never
    local counters, so a resumed partial import still produces one
    accurate summary) is always written LAST.

    PLAIN FACT, not a weakened guarantee, even after all of the above:
    this log lives in a worker-writable directory (the stations dir),
    so its very existence and every byte in it is still audit evidence
    a worker could have forged or suppressed -- that no longer lets a
    forged line change what the ledger believes happened, but it does
    not affect ENFORCEMENT either way, which already happened (the
    gate hook's own exit code, decided before the line was ever
    written, is what actually blocked or allowed the tool)."""
    request_id = ctx.request.request_id
    status = "ok"
    lines: list[str] = []
    # Loaded ONCE, not re-queried per line (see _existing_gate_log_lines).
    decisions, malformed_lines = _existing_gate_log_lines(ctx, request_id)
    if spec.gate_log_path is not None:
        try:
            raw = _read_gate_log_no_symlink(spec.gate_log_path)
        except _GateLogOversize as exc:
            status = "oversize"
            if not _gate_log_event_already_recorded(ctx, "gate_log.oversize", request_id):
                ctx.ledger.append(Event(
                    project=ctx.project, kind="gate_log.oversize", actor_kind="processor",
                    actor_id="processor", attempt_id=ctx.attempt_id,
                    payload={"request_id": request_id, "size": exc.size},
                ))
        else:
            if raw is None:
                status = "missing"
                if not _gate_log_event_already_recorded(ctx, "gate_log.missing", request_id):
                    ctx.ledger.append(Event(
                        project=ctx.project, kind="gate_log.missing", actor_kind="processor",
                        actor_id="processor", attempt_id=ctx.attempt_id,
                        payload={"request_id": request_id},
                    ))
            else:
                try:
                    lines = raw.decode("utf-8").splitlines()
                except UnicodeDecodeError:
                    status = "undecodable"
                    if 0 not in malformed_lines:
                        _journal_gate_log_malformed(ctx, request_id, 0, "<undecodable>")
                        malformed_lines.add(0)

    for line_no, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        if line_no in decisions or line_no in malformed_lines:
            continue
        try:
            entry = json.loads(raw_line)
            if not isinstance(entry, dict):
                raise ValueError("gate log line is not a JSON object")
            kind = entry.get("kind")
            payload = entry.get("payload")
            if not isinstance(kind, str) or not isinstance(payload, dict):
                raise ValueError("missing or malformed kind/payload")
            cleaned = _validate_gate_log_payload(kind, payload)
            if cleaned is None:
                raise ValueError("unrecognized kind or payload shape")
        except (json.JSONDecodeError, ValueError, TypeError):
            _journal_gate_log_malformed(ctx, request_id, line_no, raw_line)
            malformed_lines.add(line_no)
            continue
        decided_payload = {"orig_kind": kind, **cleaned,
                           "request_id": request_id, "log_line": line_no}
        ctx.ledger.append(Event(
            project=ctx.project, kind="gate_log.decision",
            actor_kind="gate_log", actor_id=spec.role, attempt_id=ctx.attempt_id,
            payload=decided_payload,
        ))
        decisions[line_no] = decided_payload

    allowed = sum(1 for p in decisions.values()
                 if p.get("orig_kind") == "policy.decided" and p.get("allowed") is True)
    denied = len(decisions) - allowed
    malformed = len(malformed_lines)

    ctx.ledger.append(Event(
        project=ctx.project, kind="gate_log.imported", actor_kind="processor",
        actor_id="processor", attempt_id=ctx.attempt_id,
        payload={"request_id": request_id, "status": status, "lines": len(lines),
                 "allowed": allowed, "denied": denied, "malformed": malformed},
    ))


__all__ = [
    "task_owns", "allowed_mcp_servers", "write_gate_facts", "init_gate_log",
    "gate_log_already_imported", "import_gate_log",
]
