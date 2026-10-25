"""engine.board -- deterministic, read-only HTML dashboard over one or
more gb2 engine ledgers.

Borrowed idea (Hermes Memory Wiki): no LLM, no network, stdlib only --
a plain data report, not a generated summary. Everything on the page is
either a literal field from a ledger row / lessons export / queue file,
or a value computed by a fixed, documented rule from those fields. There
is no invented number here: cost/duration are "-" or "확인 필요" when the
ledger simply does not carry the data, rather than guessed.

This module reads ledger sqlite files DIRECTLY with a read-only sqlite3
connection, instead of going through engine.ledger.Ledger. Two reasons:

1. The "daily log" / "recent activity" panels need each event's `ts`
   column. engine.ledger.Ledger.events() returns Event objects, and
   Event (ledger.py's own dataclass) does not carry ts at all -- so the
   public API cannot answer "when did this happen" even though the
   underlying table can.
2. Ledger.open() enforces an exact schema-version match and is meant for
   a single writer process; a read-only board that must "work offline
   from files" (plan requirement) should not take that lock-ish
   contract on, and should degrade (skip that app, keep going) if one
   app's ledger is mid-migration or simply missing -- not raise.

This module never calls Ledger.append and never opens a connection for
writing. sqlite3.connect() is always given `mode=ro` via a file: URI.
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------
# Reading ledgers (read-only, direct SQL -- see module docstring)
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class EventRow:
    seq: int
    ts: float
    kind: str
    actor_kind: str
    actor_id: str
    attempt_id: str | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class AttemptRow:
    attempt_id: str
    job_id: str
    n: int
    status: str


@dataclass(frozen=True)
class JobBoard:
    """One job_name's roll-up across all of its attempts, for one app."""

    app: str
    job_id: str
    job_name: str
    status: str  # derived display label -- see _derive_job_status
    stage: str  # derived display label -- see _derive_job_stage
    attempt_count: int
    cost_usd: float | None  # None: no station carried a cost_usd value
    duration_sec: float | None  # None: fewer than two timestamped events
    first_ts: float | None
    last_ts: float | None


@dataclass(frozen=True)
class AppBoard:
    app: str
    project: str
    jobs: tuple[JobBoard, ...]
    events: tuple[EventRow, ...]  # all events for this project, by seq
    error: str | None = None  # ledger missing/unreadable -- jobs/events empty


def _connect_ro(path: Path) -> sqlite3.Connection:
    # Path.as_uri() percent-encodes the path itself (spaces, '?', '#',
    # '%', ...) -- as_posix() does not, so a ledger path containing any
    # of those characters could otherwise inject extra sqlite URI query
    # parameters or simply fail to open.
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def load_app_board(app: str, ledger_path: Path, project: str,
                   repo: Path | None = None) -> AppBoard:
    """Read everything for `project` out of the sqlite file at
    `ledger_path`. Never raises -- a missing file, a file that is not a
    sqlite database, or a project with no rows yet all come back as an
    AppBoard with empty jobs/events (and `error` set for the first two),
    so one broken/absent app's ledger never stops the rest of the
    board from rendering.

    `repo` (the app's own project repo, where its plan.json files
    live) is optional and defaults to None, which keeps every existing
    caller's behavior byte-for-byte unchanged (every test in this
    module's own test suite omits it). When given, `_derive_job_status`
    additionally requires the P4 evidence graph (engine.evidence_graph)
    to be complete before it will ever show 완료/배포됨/건너뜀 for a job
    whose latest release_decision event says so -- see that function's
    own docstring for exactly what "complete" means and how a failure
    to even determine it (no plan.json, ledger busy, ...) degrades."""
    try:
        conn = _connect_ro(ledger_path)
    except sqlite3.OperationalError as exc:
        return AppBoard(app=app, project=project, jobs=(), events=(),
                        error=f"원장을 열 수 없습니다: {exc}")
    try:
        try:
            job_rows = conn.execute(
                "SELECT job_id, job_name FROM jobs WHERE project=?", (project,)
            ).fetchall()
            attempt_rows = conn.execute(
                "SELECT a.attempt_id, a.job_id, a.n, a.status"
                " FROM attempts a JOIN jobs j ON a.job_id = j.job_id"
                " WHERE j.project=?", (project,)
            ).fetchall()
            event_rows = conn.execute(
                "SELECT seq, ts, kind, actor_kind, actor_id, attempt_id,"
                " payload_json FROM events WHERE project=? ORDER BY seq",
                (project,),
            ).fetchall()
            # request_id -> status, for deriving a release request's own
            # CURRENT terminal outcome (see _derive_job_status's own
            # "awaiting_release_approval" handling) -- a publish that
            # ended "failed"/"unknown" without `ship` ever reaching its
            # own report/close followup (a failed
            # publish must never leave the board showing "사람 확인
            # 대기" forever) is only visible through this table; nothing
            # on the events table itself is updated when a request's own
            # status changes.
            request_status = dict(conn.execute(
                "SELECT request_id, status FROM requests WHERE project=?", (project,)
            ).fetchall())
        except sqlite3.DatabaseError as exc:
            return AppBoard(app=app, project=project, jobs=(), events=(),
                            error=f"원장을 읽지 못했습니다: {exc}")
    finally:
        conn.close()

    events = tuple(
        EventRow(seq=row[0], ts=row[1], kind=row[2], actor_kind=row[3],
                actor_id=row[4], attempt_id=row[5],
                payload=_safe_json(row[6]))
        for row in event_rows
    )
    attempts = tuple(
        AttemptRow(attempt_id=row[0], job_id=row[1], n=row[2], status=row[3])
        for row in attempt_rows
    )
    job_names = {row[0]: row[1] for row in job_rows}

    attempts_by_job: dict[str, list[AttemptRow]] = {}
    for attempt in attempts:
        attempts_by_job.setdefault(attempt.job_id, []).append(attempt)

    jobs = tuple(
        _derive_job(app, job_id, job_names[job_id],
                   sorted(attempts_by_job.get(job_id, ()), key=lambda a: a.n),
                   events, request_status, ledger_path=ledger_path, project=project, repo=repo)
        for job_id in job_names
    )
    return AppBoard(app=app, project=project, jobs=jobs, events=events)


def _safe_json(payload_json: str) -> dict[str, Any]:
    try:
        data = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------
# Deriving one job's display row
# ---------------------------------------------------------------------

_AWAITING_PREFIX = "awaiting_"

# P1 (ENGINE.md s20 flow-map (가)): the flow layer now journals ONE
# position event, 'job.position' (engine.position.record_position),
# in place of the former 'full_orbit.cursor'/'full_orbit.progress'
# pair -- same "stage"/"stop_reason" fields this module already read,
# but `request_id` moved under payload["extra"]["request_id"] (the
# JobPosition.extra catch-all) rather than sitting at the payload's own
# top level. A ledger written before P1 still has 'full_orbit.
# progress' events and no 'job.position' ones at all -- this module
# reads BOTH kinds in the SAME seq-ordered pass (see _derive_job_status/
# _derive_job_stage), so "whichever one has the newest seq wins" still
# holds across the cutover, old events on an old ledger, new events on
# a new one, never a mix that needs its own special case.
_POSITION_EVENT_KINDS = ("full_orbit.progress", "job.position")


def _position_request_id(event: "EventRow") -> object:
    if event.kind == "job.position":
        extra = event.payload.get("extra")
        return extra.get("request_id") if isinstance(extra, dict) else None
    return event.payload.get("request_id")


def _derive_job(app: str, job_id: str, job_name: str,
                attempts: list[AttemptRow], all_events: tuple[EventRow, ...],
                request_status: dict[str, str], *, ledger_path: Path, project: str,
                repo: Path | None) -> JobBoard:
    attempt_ids = {a.attempt_id for a in attempts}
    job_events = tuple(e for e in all_events if e.attempt_id in attempt_ids)
    timestamps = [e.ts for e in job_events]
    first_ts = min(timestamps) if timestamps else None
    last_ts = max(timestamps) if timestamps else None
    duration_sec = (last_ts - first_ts) if (first_ts is not None and last_ts is not None
                                            and last_ts > first_ts) else None

    costs = [e.payload.get("cost_usd") for e in job_events if e.kind == "station.finished"]
    costs = [c for c in costs if isinstance(c, (int, float))]
    cost_usd = sum(costs) if costs else None

    latest_attempt = attempts[-1] if attempts else None
    status = _derive_job_status(job_events, latest_attempt, request_status,
                                ledger_path=ledger_path, project=project,
                                job_name=job_name, repo=repo)
    stage = _derive_job_stage(job_events)

    return JobBoard(
        app=app, job_id=job_id, job_name=job_name, status=status, stage=stage,
        attempt_count=len(attempts), cost_usd=cost_usd, duration_sec=duration_sec,
        first_ts=first_ts, last_ts=last_ts,
    )


# engine.full_orbit.record_release_decision's own "decision" values ->
# this board's display label. "not_configured" is never expected to win
# a seq race against anything (a project with no release track never
# records an "awaiting_release_approval" progress event at all -- see
# _maybe_release_or_done's own docstring), but it still needs a label
# in case a future caller records it some other way; "완료" matches
# job.handoff_written's own label since that is exactly what
# "not_configured" means (nothing left to release, job just finished).
# "failed" is for a future caller that might record this decision
# itself; the common case today -- `ship` driving a publish request all
# the way to "failed"/"unknown" without ever reaching report/close --
# is instead derived straight from the request's own CURRENT status
# (see _derive_job_status's own "awaiting_release_approval" handling),
# since `ship` returns without journaling anything at all on that path.
_RELEASE_DECISION_LABEL = {
    "shipped": "배포됨",
    "skipped": "건너뜀",
    "not_configured": "완료",
    "failed": "실패",
}


_RELEASE_SUCCESS_DECISIONS = ("shipped", "skipped", "not_configured")
_INCOMPLETE_GRAPH_LABEL = "사람 확인 대기: 증거 불완전"


def _derive_job_status(job_events: tuple[EventRow, ...],
                       latest_attempt: AttemptRow | None,
                       request_status: dict[str, str] | None = None, *,
                       ledger_path: Path | None = None, project: str | None = None,
                       job_name: str | None = None, repo: Path | None = None) -> str:
    """Best-effort display label from whatever this job's events show --
    not an authoritative engine state (the engine has no single "job
    status" field; full_orbit's own FullOutcome is a return value, not a
    ledger row).

    Every event below is seq-stamped, and whichever one has the HIGHEST
    seq (i.e. happened most recently) decides the label -- NOT a fixed
    priority order. That matters because `tools/gb2`'s `ship`/
    `skip-ship` (and a release-configured job's report/close followup
    simply failing on its own) all run strictly AFTER the
    "awaiting_release_approval" progress event that first parked the
    job waiting for the owner -- without seq-ordering, that one stale
    "사람 확인 대기" label would win forever even once the job has
    plainly moved past it (ship/skip-ship/failure must
    never leave a stale "release approval waiting" row behind).

    Candidates considered, each from its own seq:
    - the LAST full_orbit.progress/job.position event (see
      _POSITION_EVENT_KINDS) whose stage starts with "awaiting_" ->
      "사람 확인 대기" (plus ": <stop_reason>" when that event carries
      one) -- UNLESS it is this
      job's release-approval stop AND `request_status` (read straight
      from the ledger's own requests table, never updated by the
      progress/position event itself) shows that SAME request_id
      already ended
      "failed"/"unknown": `tools/gb2`'s `ship` drives a publish request
      to exactly that terminal status and then returns without ever
      journaling report/close or a release_decision on a real deploy
      failure -- the board must
      still show 실패, not keep repeating "사람 확인 대기" for a
      request that will never be approved again.
    - a job.handoff_written event (there is at most one per job --
      engine.handoff.write_handoff) is deliberately NOT a 완료 candidate
      on its own: handoff always runs BEFORE the app-wide security
      check and the job's release decision (engine.full_orbit.
      _finish_after_handoff), so a crash between "handoff written" and
      either of those two later steps would otherwise read as 완료 while
      the job is still, in fact, stuck mid-security or never released.
      완료 (and every other terminal label below) comes only from a
      LATER event that this module can trace back to the final
      certificate the job actually reached -- a release_decision, or a
      job.metrics failure, or a still-open "awaiting_*" stop. A job
      whose latest event is simply its own handoff_written (security
      crashed right after, before recording anything) therefore falls
      through to the "진행 중"/"중단" fallback below, same as a job
      that has not reached handoff at all -- which is honest: from the
      ledger's own evidence alone, "완료" is not yet known to be true.
    - the LAST "job.metrics" event whose payload["outcome"] == "failed"
      (engine.orbit's own per-attempt outcome record, written right
      before a stage-list call returns early -- including a release
      followup's own report/close simply failing) -> 실패.
    - the LAST full_orbit.release_decision event (engine.full_orbit.
      record_release_decision -- `gb2 ship`/`gb2 skip-ship`'s own
      durable terminal record for a release-configured job) ->
      _RELEASE_DECISION_LABEL[decision] -- UNLESS the decision is one
      of _RELEASE_SUCCESS_DECISIONS (shipped/skipped/not_configured,
      i.e. every label this table maps to something other than 실패)
      AND `repo` was given AND the P4 evidence graph (engine.
      evidence_graph, built fresh from `ledger_path`'s own CURRENT
      ledger + `job_name`'s own CURRENT plan.json -- never trusted
      from this release_decision event's own say-so) is NOT complete
      right now: a release decision recorded in the past is itself
      only as good as the evidence that justified it at the time, and
      this board renders the CURRENT ledger, not a snapshot from when
      that decision was made -- a requirement's only covering scene
      quietly losing its pass (plan.json edited since, or the ledger
      itself edited by hand) must stop a stale "완료" from being
      displayed as this job's status. `ledger_path`/`project`/
      `job_name`/`repo` are all optional (default None, this function's
      own pre-existing callers in this module's test suite never pass
      them) -- omitting any one of them skips this extra check
      entirely, the exact behavior before P4. A `repo` with no
      plan.json, or a ledger that cannot be opened right now
      (_release_graph_complete returns None either way), is NEVER
      treated as "incomplete" -- only a definite `missing(graph)` makes
      this downgrade _INCOMPLETE_GRAPH_LABEL, same "degrade, never
      raise or over-claim" rule this module's own docstring sets for
      every other field.

    If none of those exist at all, falls back to: the latest attempt's
    own status is abandoned/interrupted -> 중단; otherwise -> 진행 중.
    """
    request_status = request_status or {}
    candidates: list[tuple[int, str, str | None]] = []
    for event in job_events:
        if event.kind in _POSITION_EVENT_KINDS:
            stage = event.payload.get("stage")
            if isinstance(stage, str) and stage.startswith(_AWAITING_PREFIX):
                request_id = _position_request_id(event)
                if (isinstance(request_id, str)
                        and request_status.get(request_id) in ("failed", "unknown")):
                    candidates.append((event.seq, "실패", None))
                    continue
                reason = event.payload.get("stop_reason")
                label = (f"사람 확인 대기: {reason}" if isinstance(reason, str) and reason
                        else "사람 확인 대기")
                candidates.append((event.seq, label, None))
        elif event.kind == "job.metrics":
            outcome = event.payload.get("outcome")
            if outcome == "failed":
                candidates.append((event.seq, "실패", None))
        elif event.kind == "full_orbit.release_decision":
            decision = event.payload.get("decision")
            if isinstance(decision, str):
                candidates.append((
                    event.seq, _RELEASE_DECISION_LABEL.get(decision, "완료"),
                    decision if decision in _RELEASE_SUCCESS_DECISIONS else None,
                ))

    if candidates:
        candidates.sort(key=lambda row: row[0])
        _seq, label, success_decision = candidates[-1]
        if (success_decision is not None and repo is not None and ledger_path is not None
                and project is not None and job_name is not None):
            complete = _release_graph_complete(ledger_path, project, job_name, repo)
            if complete is False:
                return _INCOMPLETE_GRAPH_LABEL
        return label
    if latest_attempt is not None and latest_attempt.status in ("abandoned", "interrupted"):
        return "중단"
    return "진행 중"


def _release_graph_complete(ledger_path: Path, project: str, job_name: str,
                            repo: Path) -> bool | None:
    """True/False once this job's P4 evidence graph (engine.
    evidence_graph) can actually be computed right now -- None when it
    cannot be (no plan.json for `job_name` under `repo`, or the ledger
    at `ledger_path` cannot be opened through engine.ledger.Ledger at
    this moment). This is the ONE place this module reaches for
    engine.ledger.Ledger at all (see this module's own docstring for
    why it otherwise reads the sqlite file directly) -- strictly
    read-only (never .append()s), and any failure degrades to None
    rather than ever raising into a board render, same contract
    load_app_board's own docstring already promises for a broken
    ledger file."""
    try:
        from engine import evidence_graph
        from engine.ledger import Ledger
        from engine.plan import load_plan

        plan = load_plan(repo, job_name)
        ledger = Ledger.open(ledger_path)
        try:
            graph = evidence_graph.build(ledger, project, job_name, plan)
        finally:
            ledger.close()
        return not evidence_graph.missing(graph)
    except Exception:
        return None


def _derive_job_stage(job_events: tuple[EventRow, ...]) -> str:
    """Last full_orbit.progress/job.position .stage if any (whichever
    kind's event has the newest seq -- see _POSITION_EVENT_KINDS' own
    docstring), else the last station.finished station name, else
    "-"."""
    last_stage: str | None = None
    last_station: str | None = None
    for event in job_events:
        if event.kind in _POSITION_EVENT_KINDS:
            stage = event.payload.get("stage")
            if isinstance(stage, str):
                last_stage = stage
        elif event.kind == "station.finished":
            station = event.payload.get("station")
            if isinstance(station, str):
                last_station = station
    if last_stage is not None:
        return last_stage
    if last_station is not None:
        return last_station
    return "-"


# ---------------------------------------------------------------------
# Lessons grouping -- deterministic keyword grouping from titles
# ---------------------------------------------------------------------

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _topic_for_title(title: str) -> str:
    """The longest token (regex \\w+, length >= 2) in `title`, ties
    broken by earliest position -- a fixed, reproducible rule, not a
    model judgment. A title with no token that long groups under
    "(기타)"."""
    best: str | None = None
    for token in _WORD_RE.findall(title):
        if len(token) < 2:
            continue
        if best is None or len(token) > len(best):
            best = token
    return best if best is not None else "(기타)"


def group_lessons(lessons: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group lesson records by `_topic_for_title` of each record's
    "title" field (missing/non-string title -> "(제목 없음)", its own
    separate group). Deterministic, no AI. Group keys are sorted by the
    caller when rendering (dict insertion order here is first-seen)."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for lesson in lessons:
        if not isinstance(lesson, dict):
            continue
        title = lesson.get("title")
        topic = _topic_for_title(title) if isinstance(title, str) and title else "(제목 없음)"
        groups.setdefault(topic, []).append(lesson)
    return groups


# ---------------------------------------------------------------------
# Tool table vs actual use -- optional panel, reused from W2 if present
# ---------------------------------------------------------------------


def _tool_reconciliation_available() -> bool:
    """True iff engine.handoff exposes a reconciliation function this
    board could call. As of this file's own commit, W2's reconciliation
    work (plan section W2: "handoff.py gets a section 도구표 대 실제
    사용") has not landed in this worktree yet, so this always returns
    False today and the panel is omitted -- exactly the plan's own
    fallback ("skip that panel"). Kept as a single, named probe point so
    wiring the real panel in later is a one-function change here rather
    than a search for where to hook it."""
    try:
        import engine.handoff as handoff_mod
    except ImportError:
        return False
    return hasattr(handoff_mod, "reconcile_tool_table")


# ---------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------

_CSS = """
:root {
  --page: #ffffff; --card: #ffffff; --soft: #f2f3f6;
  --line: #eaebee; --line-strong: #dcdee3;
  --ink: #212124; --ink-2: #4d5159; --muted: #6e737b; --faint: #868b94;
  --accent: #2f6fed; --accent-soft: #eaf1ff;
  --pos: #1a8a4b; --pos-soft: #e8f7ee;
  --warn: #b5790a; --warn-soft: #fff3df;
  --neg: #c13f3f; --neg-soft: #fcebeb;
}
@media (prefers-color-scheme: dark) {
  :root {
    --page: #16171a; --card: #1d1f23; --soft: #22242a;
    --line: #2b2d33; --line-strong: #3a3d45;
    --ink: #eceef1; --ink-2: #c7cad1; --muted: #9499a3; --faint: #6e737b;
    --accent: #5b94ff; --accent-soft: #1d2740;
    --pos: #4fd17f; --pos-soft: #16271c;
    --warn: #e2b14d; --warn-soft: #2e2512;
    --neg: #f08080; --neg-soft: #2e1a1a;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--page); color: var(--ink);
  font-family: "Pretendard Variable", Pretendard, -apple-system, BlinkMacSystemFont,
    "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans", sans-serif;
  font-size: 15px; line-height: 1.7; -webkit-font-smoothing: antialiased;
  word-break: keep-all; overflow-wrap: anywhere;
}
main { max-width: 1024px; margin: 0 auto; padding: 24px 20px 64px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 32px 0 12px; color: var(--ink); }
.meta { font-size: 13px; color: var(--muted); margin-bottom: 24px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 500; font-size: 13px; }
tr:last-child td { border-bottom: 1px solid var(--line-strong); }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px 20px; margin-bottom: 16px; }
.badge { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; }
.badge-pos { background: var(--pos-soft); color: var(--pos); }
.badge-warn { background: var(--warn-soft); color: var(--warn); }
.badge-neg { background: var(--neg-soft); color: var(--neg); }
.badge-soft { background: var(--soft); color: var(--ink-2); }
.empty { color: var(--faint); font-size: 14px; }
.day { font-size: 13px; color: var(--muted); margin: 16px 0 4px; }
.err { color: var(--neg); font-size: 13px; }
"""

_STATUS_BADGE = {
    "완료": "badge-pos",
    "배포됨": "badge-pos",
    "건너뜀": "badge-soft",
    "사람 확인 대기": "badge-warn",
    "실패": "badge-neg",
    "중단": "badge-neg",
    "진행 중": "badge-soft",
}


def _status_badge_class(status: str) -> str:
    """_STATUS_BADGE, matched by prefix for "사람 확인 대기: <reason>"
    ."""
    if status in _STATUS_BADGE:
        return _STATUS_BADGE[status]
    if status.startswith("사람 확인 대기"):
        return _STATUS_BADGE["사람 확인 대기"]
    return "badge-soft"


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}시간 {minutes}분"
    if minutes:
        return f"{minutes}분 {secs}초"
    return f"{secs}초"


def _fmt_cost(cost_usd: float | None) -> str:
    if cost_usd is None:
        return "확인 필요"
    return f"${cost_usd:.2f}"


def _fmt_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")


def _event_label(app: str, event: EventRow) -> str:
    p = event.payload
    if event.kind == "job.opened":
        return f"{app}: 작업 시작 - {p.get('job_name', '?')}"
    if event.kind == "attempt.finished":
        return f"{app}: 시도 종료 ({p.get('status', '?')})"
    if event.kind == "station.finished":
        return (f"{app}: {p.get('station', '?')}/{p.get('role', '?')} 완료 "
                f"(ok={p.get('ok')})")
    if event.kind in _POSITION_EVENT_KINDS:
        return f"{app}: 진행 - {p.get('stage', '?')}"
    if event.kind == "report.written":
        return f"{app}: 보고서 작성"
    if event.kind == "request.submitted":
        return f"{app}: 요청 제출 ({p.get('kind', '?')})"
    if event.kind == "request.status":
        return f"{app}: 요청 상태 ({p.get('status', '?')})"
    if event.kind == "approval.granted":
        return f"{app}: 승인 ({p.get('action', '?')} {p.get('target', '?')})"
    if event.kind == "gate_log.imported":
        return (f"{app}: 게이트 로그 가져옴 "
                f"({p.get('allowed', 0)} 허용 / {p.get('denied', 0)} 거부)")
    return f"{app}: {event.kind}"


def render_board_html(apps: list[AppBoard], *, lessons: list[dict[str, Any]] | None = None,
                      queue_text: str | None = None, generated_ts: float | None = None,
                      recent_limit: int = 30) -> str:
    """Build the full static page. Pure given its arguments (no file
    I/O, no clock reads unless `generated_ts` is None) -- callers that
    want a reproducible test render pass a fixed `generated_ts`."""
    if generated_ts is None:
        generated_ts = datetime.now(tz=timezone.utc).timestamp()

    all_events: list[tuple[str, EventRow]] = []
    for app_board in apps:
        for event in app_board.events:
            all_events.append((app_board.app, event))
    all_events.sort(key=lambda pair: pair[1].seq)

    jobs_html = _render_jobs_table(apps)
    day_log_html = _render_day_log(all_events)
    recent_html = _render_recent(all_events, recent_limit)
    lessons_html = _render_lessons(lessons or [])
    queue_html = _render_queue(queue_text)
    errors_html = _render_errors(apps)

    generated_label = _fmt_day(generated_ts) + " " + _fmt_time(generated_ts) + " UTC"

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>IF 공방 작업판</title>
<style>{_CSS}</style>
</head>
<body>
<main>
<h1>IF 공방 작업판</h1>
<div class="meta">생성 시각: {_esc(generated_label)} (고정 파일 기준, 자동 새로고침 없음)</div>
{errors_html}
<h2>작업 (앱별)</h2>
{jobs_html}
<h2>최근 활동</h2>
{recent_html}
<h2>날짜별 기록</h2>
{day_log_html}
<h2>작업판(큐)</h2>
{queue_html}
<h2>경험 묶음 (제목 키워드별)</h2>
{lessons_html}
</main>
</body>
</html>
"""


def _render_errors(apps: list[AppBoard]) -> str:
    rows = [app for app in apps if app.error]
    if not rows:
        return ""
    lines = "".join(f'<div class="err">{_esc(app.app)}: {_esc(app.error)}</div>' for app in rows)
    return f'<div class="card">{lines}</div>'


def _render_jobs_table(apps: list[AppBoard]) -> str:
    jobs = [job for app in apps for job in app.jobs]
    if not jobs:
        return '<p class="empty">작업이 없습니다.</p>'
    jobs = sorted(jobs, key=lambda j: (j.last_ts or 0), reverse=True)
    rows = []
    for job in jobs:
        badge_cls = _status_badge_class(job.status)
        rows.append(
            "<tr>"
            f"<td>{_esc(job.app)}</td>"
            f"<td>{_esc(job.job_name)}</td>"
            f'<td><span class="badge {badge_cls}">{_esc(job.status)}</span></td>'
            f"<td>{_esc(job.stage)}</td>"
            f"<td>{job.attempt_count}</td>"
            f"<td>{_esc(_fmt_duration(job.duration_sec))}</td>"
            f"<td>{_esc(_fmt_cost(job.cost_usd))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>앱</th><th>작업</th><th>상태</th><th>단계</th>"
        "<th>시도</th><th>걸린 시간</th><th>비용</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_recent(all_events: list[tuple[str, EventRow]], limit: int) -> str:
    if not all_events:
        return '<p class="empty">활동이 없습니다.</p>'
    recent = sorted(all_events, key=lambda pair: pair[1].seq, reverse=True)[:limit]
    rows = [
        f"<tr><td>{_esc(_fmt_day(event.ts))} {_esc(_fmt_time(event.ts))}</td>"
        f"<td>{_esc(_event_label(app, event))}</td></tr>"
        for app, event in recent
    ]
    return (
        "<table><thead><tr><th>시각(UTC)</th><th>내용</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_day_log(all_events: list[tuple[str, EventRow]]) -> str:
    if not all_events:
        return '<p class="empty">기록이 없습니다.</p>'
    by_day: dict[str, list[tuple[str, EventRow]]] = {}
    for app, event in all_events:
        by_day.setdefault(_fmt_day(event.ts), []).append((app, event))
    parts = []
    for day in sorted(by_day):
        rows = "".join(
            f"<li>{_esc(_fmt_time(event.ts))} - {_esc(_event_label(app, event))}</li>"
            for app, event in by_day[day]
        )
        parts.append(f'<div class="day">{_esc(day)}</div><ul>{rows}</ul>')
    return "".join(parts)


def _render_queue(queue_text: str | None) -> str:
    if not queue_text:
        return '<p class="empty">작업판 파일이 없습니다.</p>'
    return f'<div class="card"><pre>{_esc(queue_text)}</pre></div>'


def _render_lessons(lessons: list[dict[str, Any]]) -> str:
    if not lessons:
        return '<p class="empty">경험 기록이 없습니다.</p>'
    groups = group_lessons(lessons)
    parts = []
    for topic in sorted(groups):
        items = "".join(
            f"<li>{_esc(item.get('title', '(제목 없음)'))}</li>" for item in groups[topic]
        )
        parts.append(f'<div class="day">{_esc(topic)} ({len(groups[topic])})</div><ul>{items}</ul>')
    return "".join(parts)


# ---------------------------------------------------------------------
# File-facing orchestration (what tools/gb2's board subcommand calls)
# ---------------------------------------------------------------------


def build_board(
    app_ledgers: "dict[str, tuple[Path, str] | tuple[Path, str, Path | None]]", *,
    lessons_path: Path | None = None, queue_path: Path | None = None,
    generated_ts: float | None = None,
) -> str:
    """app_ledgers: {app_name: (ledger_path, project_name)} or
    {app_name: (ledger_path, project_name, repo)} -- the third, optional
    element (an app's own project repo, where its plan.json files live)
    enables load_app_board's own P4 evidence-graph completeness check
    for that app's jobs; a 2-tuple (every existing caller today) keeps
    that check off, exactly as before it existed. Reads the optional
    lessons export JSON (a list of lesson dicts; any other shape is
    treated as "no lessons", not an error) and the optional queue
    markdown file verbatim. Never raises for a missing or unreadable
    optional file -- only ledger read failures surface (per app, in the
    error panel)."""
    apps = [
        load_app_board(app, value[0], value[1], repo=value[2] if len(value) > 2 else None)
        for app, value in app_ledgers.items()
    ]
    lessons = _load_lessons(lessons_path) if lessons_path is not None else None
    queue_text = _load_text(queue_path) if queue_path is not None else None
    return render_board_html(apps, lessons=lessons, queue_text=queue_text,
                             generated_ts=generated_ts)


def _load_lessons(path: Path) -> list[dict[str, Any]] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, list) else None


def _load_text(path: Path) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
