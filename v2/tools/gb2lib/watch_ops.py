"""`gb2 watch`: one verdict per app (engine.application.watch.judge_app) and,
under `--loop --resume`, resuming a "stalled" app through the SAME
"이어 하기" path `gb2 continue` already uses -- engine.application.job_state.
resume_job, under the name engine.full_orbit.continue_full_job.

Background (the real incident this exists for -- see engine.application.watch's
own module docstring): an operator wrapped a `gb2` call in a shell
timeout. The wrapper killed the engine process mid-dispatch, but the
request it was running stayed "running" in the ledger forever -- nothing
else was watching, so the job sat stuck for three hours before anyone
noticed. `gb2 watch --loop --resume` is the thing that now notices and
resumes it on its own, in-process -- never a new `gb2` subprocess, and
never anything other than `judge_app`'s own "stalled" verdict (a
"waiting_owner"/"failed" app is never auto-resumed; only a human
clearing that stop may continue it).

`maybe_resume` is the one entry point `run_watch_pass` calls for every
app its own pass judged; it is also tested directly (no loop, no
sleep) against a fake-but-real ledger.

Operator alerts (G16/G30,: `run_watch_pass` must not just
record a stuck app in the ledger and move on -- the workspace-wide
operator alert file (ALERTS.md-shaped: one `- [ ] MM-DD HH:MM {json}`
line per incident, read by the session-start hook) needs to hear about
it too, so the owner is told BEFORE being asked. `post_ops_alert` posts
one line for a (app, kind, attempt_id) triple at most once -- a
"watch.alert_posted" ledger event guards the dedup, so a later pass
re-judging the SAME still-stuck attempt never spams the same line
again, while a genuinely new attempt (a fresh job, or a resumed one
that fails again) is its own, legitimately new incident."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from engine.application import watch
from engine.application.processor import ProcessorBusy
from engine.config import EngineConfig
from engine.ledger import Event, Ledger

from . import ops, shared

__all__ = ["cmd_watch", "maybe_resume", "maybe_standing_approve", "run_watch_pass",
           "judge_one", "post_ops_alert"]

_RESUME_WINDOW_SEC = 3600.0
_ALERTS_PATH_ENV = "GB2_OPS_ALERTS_PATH"
_DEFAULT_ALERTS_PATH = Path("<apps-root>/<ops>/ops/ALERTS.md")


def _job_name_for_attempt(ledger: Ledger, project: str, attempt_id: str) -> str | None:
    """This module's own small copy of the same job.opened lookup every
    other module here keeps privately (see tools/gb2lib/ops.py's own copy's
    docstring for why this codebase duplicates it per module)."""
    for _seq, event in ledger.events(project, "job.opened"):
        if event.payload.get("attempt_id") == attempt_id:
            return str(event.payload.get("job_name"))
    return None


def _continue_full_job(config: EngineConfig, ledger: Ledger, fakes_for: Callable,
                       project: str, job_name: str) -> object:
    """A thin, directly-monkeypatchable seam over engine.full_orbit.
    continue_full_job (== engine.application.job_state.resume_job -- the exact
    "이어 하기" path `gb2 continue` drives) -- kept as a bare module
    function (not inlined into maybe_resume) so a test can replace it
    with a stub and never start a real worker/reviewer CLI."""
    from engine import full_orbit

    return full_orbit.continue_full_job(config, ledger, fakes_for, project, job_name)


def _resume_count_last_hour(ledger: Ledger, project: str, now: float) -> int:
    cutoff = now - _RESUME_WINDOW_SEC
    return sum(1 for ts in ledger.event_timestamps(project, "watch.resumed") if ts >= cutoff)


def maybe_resume(config: EngineConfig, ledger: Ledger, project: str,
                 fakes_for: Callable, record: dict[str, object], *,
                 now: float | None = None) -> str:
    """Resumes `project` iff `record["verdict"] == "stalled"` -- never
    for "waiting_owner"/"failed"/"running"/"idle", a human decision in
    every one of those cases. Returns one of:
      "not_stalled"      -- record's verdict was not "stalled"; nothing done
      "gave_up"          -- watch_max_resumes_per_hour already hit within
                             the trailing hour; a "watch.gave_up" event
                             was appended (once -- see
                             engine.application.watch.gave_up_pending) and
                             nothing was resumed
      "skipped_no_attempt"/"skipped_no_job" -- the project has no
                             current attempt (or no job.opened for it)
                             to resume at all
      "skipped_locked"   -- another live driver already holds this
                             project's lock (engine.application.watch.
                             driver_alive) -- never resumed out from
                             under it
      "resumed"          -- a "watch.resumed" event was appended and
                             engine.full_orbit.continue_full_job ran (to
                             whatever outcome -- done/needs_owner/failed
                             are all "resumed" here: the point is that
                             watch tried, not that it necessarily
                             finished the job)
      "resume_raced"     -- continue_full_job itself hit ProcessorBusy
                             (a live driver grabbed the lock between the
                             pre-check above and the call) -- the
                             "watch.resumed" event already recorded this
                             attempt either way, same as a real one.
      "resume_failed"    -- continue_full_job itself hit
                             engine.full_orbit.FullOrbitError,
                             a review -- e.g. a single-task `gb2 run`
                             job has no full-orbit position to resume
                             through this path at all: job_state.
                             resume_job raises for stage=="single_task").
                             A "watch.resume_failed" event was appended
                             (same attempt_id, carrying the error text) so
                             this ONE app's own ledger records why it
                             could not be auto-resumed, and `run_watch_pass`
                             moves on to the next app instead of this
                             exception killing the whole watch pass for
                             every other app too -- the original incident
                             this whole module exists to catch was exactly
                             one stuck app silently freezing everything
                             watching it."""
    now = time.time() if now is None else now
    if record.get("verdict") != "stalled":
        return "not_stalled"

    project_row = ledger.project(project)
    max_per_hour = config.watch_max_resumes_per_hour
    count = _resume_count_last_hour(ledger, project, now)
    if count >= max_per_hour:
        if not watch.gave_up_pending(ledger, project, project_state=project_row.state):
            ledger.append(Event(
                project=project, kind="watch.gave_up", actor_kind="processor",
                actor_id="watch",
                payload={"reason": "watch_max_resumes_per_hour", "count": count,
                         "max_per_hour": max_per_hour, "verdict": record.get("verdict"),
                         "stage": record.get("stage")},
            ))
        return "gave_up"

    attempt_id = project_row.current_attempt_id
    if attempt_id is None:
        return "skipped_no_attempt"
    job_name = _job_name_for_attempt(ledger, project, attempt_id)
    if job_name is None:
        return "skipped_no_job"
    if watch.driver_alive(ledger, project):
        return "skipped_locked"

    ledger.append(Event(
        project=project, kind="watch.resumed", actor_kind="processor",
        actor_id="watch", attempt_id=attempt_id,
        payload={"reason": record.get("verdict"), "stage": record.get("stage"),
                 "open_requests": record.get("open_requests")},
    ))
    from engine.full_orbit import FullOrbitError

    from engine.application.driver_session import driver_session

    try:
        with driver_session(ledger, project):
            _continue_full_job(config, ledger, fakes_for, project, job_name)
    except ProcessorBusy:
        return "resume_raced"
    except FullOrbitError as exc:
        ledger.append(Event(
            project=project, kind="watch.resume_failed", actor_kind="processor",
            actor_id="watch", attempt_id=attempt_id,
            payload={"reason": str(exc), "stage": record.get("stage")},
        ))
        return "resume_failed"
    return "resumed"


def maybe_standing_approve(config: EngineConfig, ledger: Ledger, project: str,
                           fakes_for: Callable, record: dict[str, object], *,
                           policy_path: Path | None = None) -> str:
    """Owner standing approval: the ONLY
    place `record["verdict"] == "waiting_owner"` is ever acted on
    automatically -- maybe_resume above never touches it, by design
    (a human decision). Owner-set policy (engine.application.
    standing_approval.load_policy) may pre-clear a NARROW set of
    request shapes (a dependency patch bump, or a design approval) for
    this app; this function checks EVERY genuinely pending stop
    against that policy and ONLY IF EVERYTHING pending matches does it
    actually approve+drain the install(s) (via the EXISTING
    owner_approval.approve_pending gate, confirm_source="standing")
    and/or append a design.approved event (standing_approval.
    design_standing_approved), then resumes through the SAME
    `continue_full_job` path maybe_resume's "resumed" branch uses. One
    request outside the policy -- or no policy at all -- leaves
    EVERYTHING untouched and returns "not_all_matched": never a
    partial approval, the job keeps waiting for the owner exactly as
    before this feature existed.

    결함: this used to query ONLY
    office_approval.pending_install_requests, itself filtered to
    owner_approval.PENDING_KIND_WHITELIST -- a pending request of ANY
    other kind (an "edit" request, or any kind this codebase does not
    even define yet) was invisible to the all-or-nothing check, so a
    batch mixing a covered install with one of those could auto-
    approve the install alone and leave the OTHER stop unaddressed
    while looking "handled". Now queries job_state_requests.
    pending_owner_requests directly -- the SAME unfiltered list
    job_state's own resume gates read, no kind filter at all -- so
    standing_approval.request_standing_kind (which already returns
    None for anything that is not a covered "install") sees every
    genuinely pending stop, not just the three historically-
    whitelisted kinds.

    Returns one of: "not_waiting_owner" (record's verdict was not
    "waiting_owner"), "skipped_no_attempt"/"skipped_no_job" (nothing to
    resume), "skipped_locked" (another live driver holds the lock --
    same check maybe_resume makes), "not_all_matched" (no pending stop
    was covered by the policy, or there was nothing pending at all --
    nothing changed), "standing_install_failed:<status>" (an approved
    install itself failed to drain to "done" -- nothing further is
    attempted), "resume_raced"/"resume_failed" (same meaning as
    maybe_resume's own), or "standing_approved_resumed" (every pending
    stop was approved and `continue_full_job` ran)."""
    # A small-path job stopped on an owner request is judged "idle", not
    # "waiting_owner"; the pending-request list below still decides.
    if record.get("verdict") not in ("waiting_owner", "idle"):
        return "not_waiting_owner"

    project_row = ledger.project(project)
    attempt_id = project_row.current_attempt_id
    if attempt_id is None:
        return "skipped_no_attempt"
    job_name = _job_name_for_attempt(ledger, project, attempt_id)
    if job_name is None:
        return "skipped_no_job"
    if watch.driver_alive(ledger, project):
        return "skipped_locked"

    from engine import owner as owner_module
    from engine.application import office_approval
    from engine.application.job_state_requests import pending_owner_requests
    from engine.application.standing_approval import (
        all_requests_match_standing, design_standing_approved, load_policy, policy_digest,
    )
    from engine.cli import _drain

    repo = config.projects[project].repo
    policy = load_policy(policy_path)
    approved_something = False

    # the FULL, unfiltered pending-owner-request list --
    # never office_approval.pending_install_requests' own
    # PENDING_KIND_WHITELIST-filtered view, which would silently hide
    # a non-whitelisted (e.g. "edit") request sitting alongside a
    # covered install and let the install alone through. See this
    # function's own docstring.
    pending = pending_owner_requests(ledger, project, job_name, repo=repo)
    if pending:
        matched_kinds = all_requests_match_standing(policy, project, pending, repo)
        if matched_kinds is None:
            return "not_all_matched"
        batch = office_approval.pinned_install_batch(ledger, project, job_name, pending)
        digest = policy_digest(policy)
        for entry, matched_kind in zip(batch, matched_kinds):
            request_id = owner_module.approve_pending(
                config, ledger, project, attempt_id, entry["request"],
                confirm=True, confirm_source="standing", proposal=entry["proposal"],
            )
            ledger.append(Event(
                project=project, kind="standing.approved", actor_kind="processor",
                actor_id="watch", attempt_id=attempt_id,
                payload={"request_id": request_id, "kind": matched_kind,
                         "policy_digest": digest},
            ))
            status = _drain(config, ledger, project, attempt_id, request_id)
            if status != "done":
                return f"standing_install_failed:{status}"
        approved_something = True

    if design_standing_approved(ledger, project, job_name, attempt_id, repo, policy=policy):
        approved_something = True

    if not approved_something:
        return "not_all_matched"

    ledger.append(Event(
        project=project, kind="watch.resumed", actor_kind="processor",
        actor_id="watch", attempt_id=attempt_id,
        payload={"reason": "standing_approved", "stage": record.get("stage")},
    ))
    from engine.full_orbit import FullOrbitError

    from engine.application.driver_session import driver_session

    try:
        with driver_session(ledger, project):
            _continue_full_job(config, ledger, fakes_for, project, job_name)
    except ProcessorBusy:
        return "resume_raced"
    except FullOrbitError as exc:
        ledger.append(Event(
            project=project, kind="watch.resume_failed", actor_kind="processor",
            actor_id="watch", attempt_id=attempt_id,
            payload={"reason": str(exc), "stage": record.get("stage")},
        ))
        return "resume_failed"
    return "standing_approved_resumed"


def _alerts_path(override: Path | None = None) -> Path:
    """The ALERTS.md-shaped file `post_ops_alert` appends to: `override`
    when given (a real caller never passes one; tests always do, so a
    test run never touches the real workspace-wide file), else
    GB2_OPS_ALERTS_PATH, else the real file the session-start hook
    reads (<home>/.claude/hooks/next-task.sh's own "vm_watch
    ALERTS" block)."""
    if override is not None:
        return override
    env_value = os.environ.get(_ALERTS_PATH_ENV)
    return Path(env_value) if env_value else _DEFAULT_ALERTS_PATH


def post_ops_alert(ledger: Ledger, project: str, kind: str, attempt_id: str | None,
                   reason: str, *, path: Path | None = None,
                   now: float | None = None) -> bool:
    """Appends one `- [ ] MM-DD HH:MM {"kind", "app", "attempt", "reason"}`
    line to the ops alerts file for (project, kind, attempt_id) -- but
    only the FIRST time for that exact triple. A "watch.alert_posted"
    ledger event (appended right after the file write, same as every
    other watch.* bookkeeping event in this module) is what a later
    call checks to skip a duplicate: the same still-stuck attempt judged
    "failed"/"waiting_owner" on every subsequent watch pass must not
    write a new ALERTS.md line each time, while a genuinely different
    attempt_id (a fresh job, or the same job resumed into a new attempt
    that fails again) is its own new incident and DOES get its own
    line. The dedupe key also holds the alerts file the line went to: a
    line written to some other file (a test run pointed at a scratch
    path) must never suppress the real alert. Returns True iff a line
    was actually written."""
    alerts_path = _alerts_path(path)
    for _seq, event in ledger.events(project, "watch.alert_posted"):
        if (event.payload.get("kind") == kind
                and event.payload.get("attempt_id") == attempt_id
                and event.payload.get("path") == str(alerts_path)):
            return False
    now = time.time() if now is None else now
    alerts_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%m-%d %H:%M", time.localtime(now))
    body = json.dumps(
        {"kind": kind, "app": project, "attempt": attempt_id, "reason": reason},
        ensure_ascii=False,
    )
    with alerts_path.open("a", encoding="utf-8") as f:
        f.write(f"- [ ] {stamp} {body}\n")
    ledger.append(Event(
        project=project, kind="watch.alert_posted", actor_kind="processor",
        actor_id="watch", attempt_id=attempt_id,
        payload={"kind": kind, "attempt_id": attempt_id, "reason": reason,
                 "path": str(alerts_path)},
    ))
    return True


def judge_one(app: str) -> dict[str, object]:
    """One verdict record for `app`, as the plain JSON dict `gb2 watch`
    prints one line of -- {"error": ...} instead if `app` has no
    config.json yet (never raises; a bad/uninitialized app among
    several named on one `gb2 watch` call must not abort the rest)."""
    config_path = shared.app_workspace(app) / "config.json"
    if not config_path.exists():
        return {"app": app, "verdict": "idle", "stage": None, "task_id": None,
                "open_requests": 0, "since_last_event_sec": 0.0,
                "error": "not_initialized"}
    config = EngineConfig.from_json(config_path)
    ledger = Ledger.open(config.ledger_path)
    try:
        verdict = watch.judge_app(ledger, app)
    finally:
        ledger.close()
    return asdict(verdict)


def run_watch_pass(apps: list[str], *, resume: bool = False,
                   alerts_path: Path | None = None) -> list[dict[str, object]]:
    """One pass over `apps`: judge each, post an operator alert for any
    newly-or-still "failed"/"waiting_owner" verdict (G16/G30 --
    `post_ops_alert` itself skips a duplicate for the same attempt), and
    -- only when `resume` is True -- call `maybe_resume` for every one
    judged "stalled" (posting a "gongbang_resume_failed" alert too if
    that resume attempt itself comes back "resume_failed"). Each app's
    own config/ledger is opened and closed within this one call; nothing
    is held across passes. `alerts_path` is for tests ONLY (a real
    caller never passes one -- see `_alerts_path`'s own docstring).

    Also when `resume` is True (owner standing approval): every
    "waiting_owner" verdict is first offered to `maybe_standing_approve`
    -- if the owner's policy covers everything that app is stopped on,
    it is auto-approved and resumed right here, BEFORE the alert above
    is even considered, and the record is re-judged so the alert (and
    the ordinary stalled-resume branch right below) see where the job
    actually stands now. A verdict the policy does not fully cover is
    untouched and alerts exactly as before. The record carries the
    added "standing_approve" key whenever this was attempted.

    Returns the list of verdict records (each carrying an added "resume"
    key when `resume` was True)."""
    records: list[dict[str, object]] = []
    for app in apps:
        record = judge_one(app)
        if resume and "error" not in record and record.get("verdict") in ("waiting_owner", "idle"):
            config_path = shared.app_workspace(app) / "config.json"
            config = EngineConfig.from_json(config_path)
            try:
                config, fakes_for = shared.fakes_builder(config, app)
            except ValueError:
                fakes_for = None
            if fakes_for is not None:
                ledger = Ledger.open(config.ledger_path)
                try:
                    standing_result = maybe_standing_approve(config, ledger, app, fakes_for, record)
                finally:
                    ledger.close()
                record["standing_approve"] = standing_result
                if standing_result == "standing_approved_resumed":
                    # Re-judge: the job may have moved on (done, a
                    # different owner stop, or even "stalled" if the
                    # resumed run itself crashed) -- every check below
                    # (the alert, the ordinary stalled-resume branch)
                    # must see where the job stands NOW, never the
                    # stale "waiting_owner" verdict standing approval
                    # just cleared.
                    refreshed = judge_one(app)
                    refreshed["standing_approve"] = standing_result
                    record = refreshed
        if "error" not in record and record.get("verdict") in ("failed", "waiting_owner"):
            config_path = shared.app_workspace(app) / "config.json"
            config = EngineConfig.from_json(config_path)
            ledger = Ledger.open(config.ledger_path)
            try:
                attempt_id = ledger.project(app).current_attempt_id
                kind = ("gongbang_waiting_owner" if record["verdict"] == "waiting_owner"
                        else "gongbang_job_failed")
                post_ops_alert(
                    ledger, app, kind, attempt_id,
                    f"stage={record.get('stage')} open_requests={record.get('open_requests')}",
                    path=alerts_path,
                )
            finally:
                ledger.close()
        if resume and record.get("verdict") == "stalled" and "error" not in record:
            config_path = shared.app_workspace(app) / "config.json"
            config = EngineConfig.from_json(config_path)
            try:
                config, fakes_for = shared.fakes_builder(config, app)
            except ValueError as exc:
                record["resume"] = f"skipped_config_error:{exc}"
                records.append(record)
                continue
            ledger = Ledger.open(config.ledger_path)
            try:
                record["resume"] = maybe_resume(config, ledger, app, fakes_for, record)
                if record["resume"] == "resume_failed":
                    attempt_id = ledger.project(app).current_attempt_id
                    post_ops_alert(
                        ledger, app, "gongbang_resume_failed", attempt_id,
                        "watch --resume could not auto-resume this app",
                        path=alerts_path,
                    )
            finally:
                ledger.close()
        records.append(record)
    return records


def cmd_watch(args: argparse.Namespace) -> int:
    apps = list(args.apps) if args.apps else ops._discover_apps()
    if not apps:
        print(json.dumps({"error": "no apps found"}), file=sys.stderr)
        return 1

    if args.loop:
        try:
            while True:
                for record in run_watch_pass(apps, resume=args.resume):
                    print(json.dumps(record, ensure_ascii=False))
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0

    for record in run_watch_pass(apps, resume=args.resume):
        print(json.dumps(record, ensure_ascii=False))
    return 0
