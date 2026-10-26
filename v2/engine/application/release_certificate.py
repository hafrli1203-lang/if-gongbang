"""job_certificate_status: whether the job a release commit belongs to
carries the acceptance + app_security certificate a release requires,
tolerant of the one-commit gap engine.application.pipeline's own handoff commit
always introduces between when those checks ran and the commit
actually released (see that function's own docstring for the full
reasoning)."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from engine.infrastructure.gitutil import git_argv

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger
    from engine.config import EngineConfig
    from engine.domain.plan import Plan


def latest_full_bench_passed(ledger: Ledger, attempt_id: str, fp: str) -> bool:
    """True iff the newest non-stale bench evidence recorded against
    exactly `fp` passed AND ran the full suite (scope "full" or the
    "full(fallback)" a related-only run falls back to when it selected
    zero real tests) -- a "related"-only passing bench is not enough
    to ship."""
    rows = [row for row in ledger.evidence(attempt_id, "bench")
            if not row.stale and row.fingerprint == fp]
    if not rows:
        return False
    latest = rows[-1]
    if latest.payload.get("passed") is not True:
        return False
    return str(latest.payload.get("scope", "")).startswith("full")


_JOB_CERTIFICATE_MISSING_REASON = "release_requires_job_certificate"
_JOB_CERTIFICATE_STALE_POLICY_REASON = "release_requires_job_certificate:security_policy_changed"


def _job_name_for_attempt(ledger: Ledger, project: str, attempt_id: str) -> str | None:
    """The job_name of the 'job.opened' event that created `attempt_id`,
    or None if no such event exists at all (should not happen for any
    attempt that ever ran a request, but this function never raises --
    the caller treats None the same as "no certificate"). Implemented
    locally (never engine.application.steps.open_job's own identically-shaped, but
    private, `_job_opened_payload_for_attempt`) since that module is
    outside this one's owned scope for this task."""
    for _seq, event in ledger.events(project, "job.opened"):
        if event.payload.get("attempt_id") == attempt_id:
            job_name = event.payload.get("job_name")
            return str(job_name) if job_name is not None else None
    return None


def _plan_or_empty(repo: Path, job_name: str) -> "Plan":
    """engine.domain.plan.load_plan(repo, job_name), or a Plan with zero
    requirements when plan.json does not exist/does not parse --
    job_certificate_status predates plan.json existing at all (several
    of this module's own tests drive it straight off hand-written
    ledger events, with no plan.json ever written to `repo`), so a
    missing/broken plan must never itself make a certificate check
    raise. A Plan with zero requirements makes
    engine.domain.evidence_graph.build() return a graph with an empty
    `requirements` tuple -- missing() then falls back to its own
    job-wide fingerprint/certificate checks only, exactly what this
    function checked on its own before engine.domain.evidence_graph existed."""
    from engine.domain.plan import Plan, PlanError, load_plan

    try:
        return load_plan(repo, job_name)
    except PlanError:
        return Plan(goal="", ui=False, scenes=(), tasks=(), acceptance=())


def _tree_exists(repo: Path, tree_sha: str) -> bool:
    """True iff `tree_sha` resolves as an actual tree object in `repo`'s
    own object database (`git cat-file -t` == "tree") -- a recorded
    fingerprint is never trusted as a real, diffable tree just because
    some ledger event says so (it could be garbage-collected, or from a
    repo that no longer exists in this shape)."""
    try:
        result = subprocess.run(
            git_argv(repo, "cat-file", "-t", tree_sha),
            capture_output=True, text=True,
        )
    except OSError:
        return False
    return result.returncode == 0 and result.stdout.strip() == "tree"


_GONGBANG_PREFIX = ".gongbang/"


def _generated_report_paths(job_name: str) -> frozenset[str]:
    """Exactly the two paths engine.application.pipeline's own handoff commit
    (write_handoff + _commit_job_trail) adds/changes AFTER engine.
    acceptance.run_acceptance already fingerprinted the tree -- see
    _job_certificate's docstring for why that one-commit difference
    must never by itself make a job's certificate look stale. Scoped
    to THIS job_name only, and to exactly these two generated outputs
    -- never the whole ".gongbang/" tree, which also holds plan.json,
    facts.md, probe scripts/logs and tests/scenes (this job's own, and
    every other job's under the same repo): a change to any of those
    after acceptance/security already ran must still refuse release,
    the same as a real source change outside .gongbang/ would."""
    return frozenset({
        f"{_GONGBANG_PREFIX}{job_name}/report.md",
        f"{_GONGBANG_PREFIX}{job_name}/acceptance.json",
    })


def _changed_paths_outside_gongbang(repo: Path, tree_a: str, tree_b: str,
                                    job_name: str) -> tuple[str, ...]:
    """Paths that differ between two tree objects (git diff --name-only,
    both sides already confirmed to be real tree objects by the caller),
    excluding only _generated_report_paths(job_name) -- see that
    function's docstring for why the exemption is this narrow, not the
    whole ".gongbang/" prefix. tree_a == tree_b short-circuits to ()
    without ever shelling out (the common case once app_security's own
    check() -- which engine.application.pipeline calls strictly AFTER the handoff
    commit -- is fingerprinted at the exact released tree)."""
    if tree_a == tree_b:
        return ()
    result = subprocess.run(
        git_argv(repo, "diff", "--name-only", tree_a, tree_b),
        check=True, capture_output=True, text=True,
    )
    exempt = _generated_report_paths(job_name)
    return tuple(p for p in result.stdout.splitlines() if p and p not in exempt)


_MAX_REASON_PATHS = 3


def _certificate_reason(changed: tuple[str, ...]) -> str:
    if not changed:
        return _JOB_CERTIFICATE_MISSING_REASON
    shown = ",".join(changed[:_MAX_REASON_PATHS])
    more = "" if len(changed) <= _MAX_REASON_PATHS else f",+{len(changed) - _MAX_REASON_PATHS} more"
    return f"{_JOB_CERTIFICATE_MISSING_REASON}:{shown}{more}"


def job_certificate_status(ledger: Ledger, project: str, attempt_id: str,
                           repo: Path, fingerprint: str,
                           config: "EngineConfig | None" = None) -> tuple[bool, str]:
    """(ok, reason) for whether the JOB `attempt_id` belongs to carries
    the certificate a release of the commit fingerprinted as
    `fingerprint` is required to have: the newest passing
    'acceptance.recorded' event AND the newest passing
    'app_security.recorded' event, among EVERY attempt of that same job
    (engine.infrastructure.db.ledger.Ledger.attempts_for_job_name, keyed by the job_name
    on `attempt_id`'s own 'job.opened' event -- never attempt_id
    equality alone), must each have a recorded fingerprint that is
    EITHER exactly `fingerprint`, OR a real tree object in `repo` whose
    git-diff against `fingerprint` touches nothing beyond this job's
    own two generated report paths (_generated_report_paths(job_name) --
    report.md and acceptance.json, never the whole ".gongbang/" tree:
    plan.json, facts.md, probe scripts/logs and tests/scenes changing
    after acceptance/security ran still refuses, same as any other
    source change). reason is "" when ok, else
    "release_requires_job_certificate" (optionally suffixed with
    ":<first few changed paths>" when the refusal is specifically
    because something outside those two generated paths changed, or
    with ":security_policy_changed" -- see `config` below).

    `config`, when given, additionally requires the newest passing
    'app_security.recorded' event's own policy_hash (engine.
    app_security.security_policy_hash at the moment that check() ran)
    to equal security_policy_hash(config) recomputed from THIS call's
    `config` -- a repo tree can match exactly while the "security"
    config itself was tightened afterward (e.g.
    allow_no_lockfile true -> false), which must make an
    already-recorded "passed" certificate stale even though nothing
    about the tree changed. `config=None` (the default) skips this
    check entirely, for callers not yet updated to pass it.

    Why an exact fingerprint match is too strict for acceptance's own
    recorded fingerprint (root cause investigated and reported to the
    team before this function was changed to tolerate it): engine.
    acceptance.run_acceptance
    fingerprints the tree BEFORE engine.application.pipeline's own write_handoff
    + _commit_job_trail commit .gongbang/<job>/report.md (and the
    already-written .gongbang/<job>/acceptance.json) into the repo --
    the FIRST commit after that handoff, strictly before engine.
    app_security.check() ever runs and strictly before engine.application.release.
    prepare() ever submits a publish request for @HEAD, is the exact
    commit actually released. So acceptance's own recorded fingerprint
    and the released commit's fingerprint can NEVER be bit-identical on
    the normal engine.application.pipeline path -- they differ by EXACTLY the
    handoff bookkeeping under .gongbang/, which is why this function
    diffs the two trees and only objects to a difference OUTSIDE
    .gongbang/ (a real source change, e.g. the owner or something else
    committing directly into the app repo after acceptance already
    ran), rather than requiring byte-identical fingerprints.
    engine.application.app_security.check() itself runs AFTER the handoff commit
    (see engine.application.pipeline._finish_after_handoff, called after
    _commit_job_trail), so its own recorded fingerprint is normally
    bit-identical to the released commit's -- the tree_a == tree_b
    short-circuit in _changed_paths_outside_gongbang covers that case
    without ever shelling out to git diff.

    Scoped by JOB, not by attempt_id equality, because engine.application.pipeline.
    _maybe_release_or_done opens a BRAND NEW attempt (via open_job) for
    the release itself, strictly after acceptance/app_security already
    ran and were recorded against an EARLIER attempt of the same job --
    attempt_id equality would make this refuse every legitimate
    full_orbit release too. engine.application.task_pipeline.continue_job's own commit-stage
    handler, which calls release.prepare() directly for the `gb2 run`
    -> `gb2 ship` path (engine.application.pipeline/engine.acceptance/engine.
    app_security are never invoked there), still opens its OWN job (see
    tools/gb2's cmd_run -> engine.application.steps.open_job.open_job) -- so a
    job_name is always found for that path too, but attempts_for_job_
    name finds no acceptance.recorded/app_security.recorded event for
    ANY of that job's attempts at all, so this still refuses it.

    `repo`/`fingerprint` are the caller's responsibility (engine.
    evidence.fingerprint(repo, excludes), the SAME function engine.
    steps.publish's own full-bench check already calls, against the
    project's own repo) -- right when checking, against the repo state
    check_artifact already confirmed IS the exact commit being
    released, never trusted from any earlier step's own say-so. A
    recorded fingerprint that does not resolve as a real tree object in
    `repo` (_tree_exists) refuses outright, same as a missing event --
    it is never diffed against anything.

    Until engine.application.pipeline (or engine.application.app_security itself) is changed
    to call engine.application.app_security.record() after .check() on the real
    release path, NO job will ever satisfy this function's app_security
    half at all -- not a fingerprint problem, there is simply no event
    yet.

    P4 (ENGINE.md s20 flow-map (라)): the existence half of both checks
    above -- "is there a passing acceptance.recorded event at all" and
    "is there a passing app_security.recorded event at all" -- is no
    longer this function's own job. It now builds engine.evidence_
    graph.build(ledger, project, job_name, plan) (plan via
    _plan_or_empty: this function predates plan.json existing at all,
    so a missing/unparseable one degrades to zero requirements rather
    than raising) and refuses outright whenever engine.domain.evidence_graph.
    missing(graph) is non-empty -- which now ALSO catches a requirement
    whose only covering scene stopped actually passing (plan.json
    edited, or the scene removed, since the job's last real acceptance
    run), something neither of this function's own former per-kind
    checks ever looked at. What this function still does on its own,
    because engine.domain.evidence_graph.build() takes no `repo`/`config` at
    all and so cannot do either: the tree-diff-tolerant match between
    EACH recorded fingerprint (graph.fingerprint, graph.
    certificate_fingerprint) and the actually-released `fingerprint`
    (the docstring section above, on why exact equality is too strict),
    and the policy_hash-vs-current-config staleness check below."""
    if not fingerprint or not _tree_exists(repo, fingerprint):
        # The caller's own, just-computed release fingerprint must
        # itself be a real tree -- defensive only (engine.infrastructure.evidence.
        # fingerprint() writes the tree into this same repo synchronously
        # right before any caller ever gets a value back from it, so
        # this should never actually fire), but a malformed/garbage
        # value here must still refuse cleanly, never crash the diff
        # below with a git error.
        return False, _JOB_CERTIFICATE_MISSING_REASON
    job_name = _job_name_for_attempt(ledger, project, attempt_id)
    if job_name is None:
        return False, _JOB_CERTIFICATE_MISSING_REASON

    from engine.domain import evidence_graph

    graph = evidence_graph.build(ledger, project, job_name, _plan_or_empty(repo, job_name))
    if evidence_graph.missing(graph):
        return False, _JOB_CERTIFICATE_MISSING_REASON

    changed: list[str] = []
    for recorded_fp in (graph.fingerprint, graph.certificate_fingerprint):
        if recorded_fp == fingerprint:
            continue
        if not _tree_exists(repo, recorded_fp):
            return False, _JOB_CERTIFICATE_MISSING_REASON
        changed.extend(_changed_paths_outside_gongbang(repo, recorded_fp, fingerprint,
                                                       job_name))

    # A matching tree is not enough for the app_security half: the
    # "security" config itself (audit_cmd/allow_no_lockfile/
    # allow_audit_unavailable/...) can be tightened AFTER the
    # certificate was recorded, with the repo tree never changing at
    # all -- e.g. allow_no_lockfile true -> false. The recorded
    # certificate's own policy_hash (engine.application.app_security.
    # security_policy_hash, computed at check()-time) is compared
    # against the SAME hash recomputed from the CURRENT config; a
    # mismatch means the certificate speaks for a policy that no
    # longer applies, security has to rerun. `config=None` (a caller
    # that has not been updated to pass it yet) skips this check
    # entirely -- see this function's own call sites for which ones
    # still need updating.
    if config is not None:
        from engine import app_security  # lazy import, see full_orbit's own convention
        if graph.certificate_policy_hash != app_security.security_policy_hash(config):
            return False, _JOB_CERTIFICATE_STALE_POLICY_REASON

    if changed:
        # Dedupe while keeping first-seen order (acceptance's own diff
        # before app_security's), so the reason's truncated path list
        # never repeats the same path twice.
        seen: dict[str, None] = {}
        for p in changed:
            seen.setdefault(p, None)
        return False, _certificate_reason(tuple(seen))
    return True, ""


__all__ = ["job_certificate_status", "latest_full_bench_passed"]
