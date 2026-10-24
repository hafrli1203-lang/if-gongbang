"""report — last step of commit and publish, and its own request kind.

As the 'report' request kind (payload {"summary": str}), it is the single
step a worker/owner uses to satisfy the (RECOVERING, close_job) report
guard (engine.state.transition, ENGINE.md close-step table) before closing
a recovery attempt: 'summary' is accepted but not otherwise used by write_
report, which always writes the same attempt/request/kind fields -- stage
2 is where a real summary would be persisted into report.md."""
from __future__ import annotations

from pathlib import Path

from engine import ids
from engine.ledger import Event
from engine.processor import StepContext, StepDef


def report_path(ctx: StepContext) -> Path:
    return ctx.config.home / "jobs" / ids.attempt_dirname(ctx.attempt_id) / "report.md"


def write_report(ctx: StepContext) -> dict[str, object]:
    """Append report.written FIRST, then write report.md containing the
    attempt_id, request_id and the request kind.

    Order matters: a crash between the two used to go file-then-event, so
    a crash right after the write left report.md present with no event —
    and the old exists() checked only the file, so it saw that as "done"
    with no event ever recorded (silently breaking the RECOVERING close
    guard, which looks for report.written). Event-first plus exists()
    requiring both means a crash in between now reads as "not done" and
    the step safely re-runs."""
    ctx.ledger.append(Event(
        project=ctx.project, kind="report.written", actor_kind="processor",
        actor_id="processor", attempt_id=ctx.attempt_id,
        payload={"request_id": ctx.request.request_id},
    ))
    path = report_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# Report\n\nattempt: {ctx.attempt_id}\n"
        f"request: {ctx.request.request_id}\nkind: {ctx.request.kind}\n",
        encoding="utf-8",
    )
    return {"path": str(path), "request_id": ctx.request.request_id}


def exists(ctx: StepContext) -> bool:
    """True iff report.md exists AND contains both "attempt: {attempt_id}"
    and "request: {request_id}" AND a report.written event for this
    request_id is in the ledger. A report left behind by a different
    request on the same attempt (e.g. an earlier commit request) must not
    satisfy a later request's reconcile check — report.md is overwritten by
    each write, so only the most recent request's lines are ever in it. A
    file present without its event (or vice versa) is "not done"."""
    path = report_path(ctx)
    if not path.exists():
        return False
    content = path.read_text(encoding="utf-8")
    if (f"attempt: {ctx.attempt_id}" not in content
            or f"request: {ctx.request.request_id}" not in content):
        return False
    return any(
        event.payload.get("request_id") == ctx.request.request_id
        for _seq, event in ctx.ledger.events(ctx.project, "report.written")
    )


def steps(ctx: StepContext) -> list[StepDef]:
    """Step list for the 'report' request kind itself: a single step,
    reusing the same write_report/exists write-then-file-then-reconcile
    pair that commit/publish append as their own last step."""
    return [StepDef("write_report", write_report, exists)]
