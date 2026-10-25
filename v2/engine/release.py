"""Fast-forward a project's configured master repo to the released
commit, then drive its deploy through the existing publish request/
approval machinery.

This module gives:
  - the git primitives engine.steps.publish's advance_master step uses
    (is_fast_forwardable, master_dirty_tracked_paths, merge_range_paths,
    ff_merge), all routed through engine.gitutil so a tampered
    master/.git/config can never redirect an engine-issued git call;
  - prepare(): submits this project's publish request for its own
    repo's current HEAD and builds the owner-facing approval prompt;
  - approve_and_run(): parses the owner's typed approval text, grants
    it, and drives the Processor until that request leaves
    queued/running.

Every git call here reads/writes the REAL master_repo on disk (never a
disposable worker copy), so it always goes through engine.gitutil.git_argv
(core.fsmonitor/hooksPath disabled) -- same reasoning as
engine.steps.open_job and engine.steps.publish's own HEAD checks.
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

from engine import evidence
from engine.approvals import ApprovalError, grant, parse_owner_message
from engine.config import EngineConfig
from engine.gitutil import git_argv
from engine.ledger import Event, Ledger, LedgerError
from engine.processor import CancelError, Fakes, Processor, ProcessorBusy, cancel_request, submit


def head_sha(repo: Path) -> str:
    """git rev-parse HEAD of `repo` (engine.gitutil-guarded). Used both
    for the project's own repo (the artifact sha a publish request is
    submitted for) and for master_repo (its current fast-forward base)."""
    result = subprocess.run(
        git_argv(repo, "rev-parse", "HEAD"),
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _object_known(repo: Path, sha: str) -> bool:
    """True iff `repo`'s own object database already has `sha` -- a
    fast-forward (or even a `git diff`/`log` against it) is otherwise
    impossible, not merely "not yet an ancestor"."""
    result = subprocess.run(git_argv(repo, "cat-file", "-e", sha), capture_output=True)
    return result.returncode == 0


def is_fast_forwardable(master_repo: Path, sha: str) -> bool:
    """True iff master_repo's current HEAD is an ancestor of `sha` --
    `git merge --ff-only sha` can succeed there without rewriting
    master's own history. False (never raised) for every other outcome:
    unrelated history, or `sha` entirely unknown to master_repo's own
    object database."""
    if not _object_known(master_repo, sha):
        return False
    result = subprocess.run(
        git_argv(master_repo, "merge-base", "--is-ancestor", "HEAD", sha),
        capture_output=True,
    )
    return result.returncode == 0


def _status_paths(master_repo: Path, *, include_ignored: bool) -> dict[str, str]:
    """Internal: relative path -> its two-letter `git status --porcelain`
    code, for every entry `git status` reports (plus, when
    `include_ignored`, every gitignored path too -- `--ignored`).
    Shared by every master_repo status helper below so each only
    differs in which status codes it keeps."""
    args = ["status", "--porcelain", "-z", "--untracked-files=all"]
    if include_ignored:
        args.append("--ignored")
    out = subprocess.run(git_argv(master_repo, *args), check=True, capture_output=True).stdout
    tokens = out.decode("utf-8", "surrogateescape").split("\0")
    paths: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if not entry:
            continue
        status, rel = entry[:2], entry[3:]
        paths[rel] = status
        if status[0] in ("R", "C") and i < len(tokens) and tokens[i]:
            paths[tokens[i]] = status
            i += 1
    return paths


def master_dirty_tracked_paths(master_repo: Path) -> tuple[str, ...]:
    """Relative paths of master_repo's own tracked, uncommitted
    modifications (staged or unstaged). Untracked files ('??' entries)
    are deliberately excluded: an untracked file can never collide with
    a path the incoming commits touch -- a fast-forward just adds it as
    a newly tracked file, nothing for this overlap check to catch."""
    return tuple(sorted(
        rel for rel, status in _status_paths(master_repo, include_ignored=False).items()
        if status != "??"
    ))


def master_untracked_paths(master_repo: Path) -> tuple[str, ...]:
    """Relative paths of files in master_repo's working tree that are
    untracked and NOT gitignored -- exactly what a plain `git status`
    (no --ignored) already shows as '??'."""
    return tuple(sorted(
        rel for rel, status in _status_paths(master_repo, include_ignored=False).items()
        if status == "??"
    ))


def master_untracked_or_ignored_paths(master_repo: Path) -> tuple[str, ...]:
    """Relative paths of every file in master_repo's working tree that
    git does NOT track -- both plain untracked ('??') and gitignored
    ('!!', only visible with --ignored) paths. These are exactly the
    paths a fast-forward's own --no-overwrite-ignore protects from
    being silently replaced, so advance_master cross-checks them
    against what the incoming commits would add."""
    return tuple(sorted(
        rel for rel, status in _status_paths(master_repo, include_ignored=True).items()
        if status in ("??", "!!")
    ))


def master_is_clean(master_repo: Path) -> bool:
    """True iff master_repo has no tracked modification AND no
    untracked-but-not-ignored file -- the precondition the deploy
    command's own working tree must satisfy, since it builds and
    deploys directly from whatever is sitting in master_repo. A
    gitignored file (a build artifact, a local .env, ...) is NOT
    required to be absent -- only tracked dirt and untracked-and-not-
    ignored dirt make master_is_clean False."""
    return (not master_dirty_tracked_paths(master_repo)
            and not master_untracked_paths(master_repo))


def merge_range_paths(master_repo: Path, sha: str) -> tuple[str, ...]:
    """Relative paths touched between master_repo's current HEAD and
    `sha` -- the commits a fast-forward would bring in. Requires `sha`
    to already be a known object in master_repo (see _object_known);
    callers check is_fast_forwardable/_object_known first."""
    result = subprocess.run(
        git_argv(master_repo, "diff", "--name-only", "HEAD", sha),
        check=True, capture_output=True, text=True,
    )
    return tuple(sorted(p for p in result.stdout.splitlines() if p))


def incoming_added_paths(master_repo: Path, sha: str) -> tuple[str, ...]:
    """Relative paths the incoming commits (master_repo's current HEAD
    .. sha) ADD that master's own tree does not already have -- the
    ones a fast-forward's checkout would newly create on disk, as
    opposed to a path master already tracks that the merge only
    modifies. Requires `sha` to already be a known object in
    master_repo, same as merge_range_paths."""
    result = subprocess.run(
        git_argv(master_repo, "diff", "--name-only", "--diff-filter=A", "HEAD", sha),
        check=True, capture_output=True, text=True,
    )
    return tuple(sorted(p for p in result.stdout.splitlines() if p))


def overlapping_paths(a: tuple[str, ...], b: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(set(a) & set(b)))


def ff_merge(master_repo: Path, sha: str) -> None:
    """git merge --ff-only --no-overwrite-ignore sha in master_repo.
    --no-overwrite-ignore (git's own default is the opposite,
    --overwrite-ignore) stops the merge from silently replacing a
    gitignored local file that happens to occupy a path the incoming
    commits add -- advance_master additionally checks
    incoming_added_paths against master_untracked_or_ignored_paths
    itself and refuses ("incoming_path_occupied") before ever reaching
    this call, so this flag is defense in depth, not the only guard.
    Raises subprocess.CalledProcessError on any git-level refusal.
    Callers are expected to have already checked
    is_fast_forwardable/master_dirty_tracked_paths/merge_range_paths
    for an overlap and refused (StepFailed) before ever reaching this
    call -- this function does not re-check either."""
    subprocess.run(
        git_argv(master_repo, "merge", "--ff-only", "--no-overwrite-ignore", sha),
        check=True, capture_output=True, text=True,
    )


def _commit_log(repo: Path, base: str, sha: str) -> list[dict[str, str]]:
    """Commits in `repo` between `base` and `sha`. `base` is master_repo's
    HEAD at prepare() time -- if master has since diverged with its own
    commit, `repo` (the project's own history) may never have seen that
    commit at all, so this returns [] rather than raising; it is purely
    an informational list for the owner-facing prompt, never a
    correctness check (that is is_fast_forwardable's job)."""
    if base == sha or not _object_known(repo, base):
        return []
    result = subprocess.run(
        git_argv(repo, "log", "--format=%H %s", f"{base}..{sha}"),
        check=True, capture_output=True, text=True,
    )
    commits: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        head, _, subject = line.partition(" ")
        commits.append({"sha": head, "subject": subject})
    return commits


def build_summary(project_repo: Path, master_repo: Path, sha: str,
                   target: str) -> dict[str, object]:
    """The commit list / diff summary / target / hash a `gb2 ship`-style
    prompt shows the owner before they type the approval text.
    `diff_paths`/`fast_forward_ok` reflect master_repo's state
    AT THE TIME this is called -- they are advisory for the prompt, not
    a substitute for advance_master's own checks at approval time
    (master can move between prepare() and the owner actually typing
    the approval)."""
    base = head_sha(master_repo)
    commits = _commit_log(project_repo, base, sha)
    ff_ok = is_fast_forwardable(master_repo, sha)
    diff_paths = merge_range_paths(master_repo, sha) if _object_known(master_repo, sha) else ()
    return {
        "target": target, "artifact_hash": sha, "base_head": base,
        "commits": commits, "diff_paths": list(diff_paths),
        "fast_forward_ok": ff_ok,
    }


def prepare(config: EngineConfig, ledger: Ledger, project: str,
            attempt_id: str) -> tuple[str, dict[str, object]]:
    """Submit a publish request for this project's current repo HEAD
    (artifact_path="@HEAD", artifact_hash=that HEAD sha) and return
    (request_id, summary) -- summary carries the exact text the owner
    must type (parse_owner_message's required shape, "승인
    R-xxxxxxxxxxxxxxxx") under summary["owner_text"], plus the commit
    list/diff/target/hash for display. Raises ValueError if this
    project has no `release` configured -- release.prepare only applies
    to a project on that track, not every publish request."""
    project_config = config.projects[project]
    release_config = project_config.release
    if release_config is None:
        raise ValueError(f"project {project!r} has no release configured")
    sha = head_sha(project_config.repo)
    payload = {"target": release_config.target, "artifact_path": "@HEAD",
               "artifact_hash": sha}
    request_id = submit(ledger, project, attempt_id, "publish", payload, actor_id="owner")
    summary = build_summary(project_config.repo, release_config.master_repo, sha,
                            release_config.target)
    summary["request_id"] = request_id
    summary["owner_text"] = f"승인 {request_id}"
    return request_id, summary


def _latest_full_bench_passed(ledger: Ledger, attempt_id: str, fp: str) -> bool:
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
    locally (never engine.steps.open_job's own identically-shaped, but
    private, `_job_opened_payload_for_attempt`) since that module is
    outside this one's owned scope for this task."""
    for _seq, event in ledger.events(project, "job.opened"):
        if event.payload.get("attempt_id") == attempt_id:
            job_name = event.payload.get("job_name")
            return str(job_name) if job_name is not None else None
    return None


def _latest_passing_fingerprint(
    ledger: Ledger, project: str, kind: str, attempt_ids: frozenset[str],
) -> str | None:
    """The payload["fingerprint"] of the newest event of `kind` whose
    attempt_id is one of `attempt_ids` and whose payload["passed"] is
    True -- None if no such event exists, or if the newest one has no
    non-"" fingerprint recorded (a run that itself failed to fingerprint
    the repo at all must never be trusted as a match for anything)."""
    rows = [
        (seq, event) for seq, event in ledger.events(project, kind)
        if event.attempt_id in attempt_ids
    ]
    if not rows:
        return None
    _seq, latest = max(rows, key=lambda row: row[0])
    if latest.payload.get("passed") is not True:
        return None
    fp = latest.payload.get("fingerprint")
    return str(fp) if fp else None


def _latest_passing_app_security_policy_hash(
    ledger: Ledger, project: str, attempt_ids: frozenset[str],
) -> str | None:
    """The payload["policy_hash"] of the newest 'app_security.recorded'
    event among `attempt_ids` whose payload["passed"] is True -- same
    selection as _latest_passing_fingerprint's own "app_security.
    recorded" case (so this is always reading the SAME event
    job_certificate_status's fingerprint check already found), just
    pulling a different field. None if no such event exists, or if it
    carries no non-"" policy_hash at all (an app_security.recorded
    event from before engine.app_security.security_policy_hash existed
    -- that absence must refuse the same as a real mismatch would,
    never be treated as "nothing to compare, so it still matches")."""
    rows = [
        (seq, event) for seq, event in ledger.events(project, "app_security.recorded")
        if event.attempt_id in attempt_ids
    ]
    if not rows:
        return None
    _seq, latest = max(rows, key=lambda row: row[0])
    if latest.payload.get("passed") is not True:
        return None
    policy_hash = latest.payload.get("policy_hash")
    return str(policy_hash) if policy_hash else None


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
    """Exactly the two paths engine.full_orbit's own handoff commit
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
    check() -- which engine.full_orbit calls strictly AFTER the handoff
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
    (engine.ledger.Ledger.attempts_for_job_name, keyed by the job_name
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
    fingerprints the tree BEFORE engine.full_orbit's own write_handoff
    + _commit_job_trail commit .gongbang/<job>/report.md (and the
    already-written .gongbang/<job>/acceptance.json) into the repo --
    the FIRST commit after that handoff, strictly before engine.
    app_security.check() ever runs and strictly before engine.release.
    prepare() ever submits a publish request for @HEAD, is the exact
    commit actually released. So acceptance's own recorded fingerprint
    and the released commit's fingerprint can NEVER be bit-identical on
    the normal engine.full_orbit path -- they differ by EXACTLY the
    handoff bookkeeping under .gongbang/, which is why this function
    diffs the two trees and only objects to a difference OUTSIDE
    .gongbang/ (a real source change, e.g. the owner or something else
    committing directly into the app repo after acceptance already
    ran), rather than requiring byte-identical fingerprints.
    engine.app_security.check() itself runs AFTER the handoff commit
    (see engine.full_orbit._finish_after_handoff, called after
    _commit_job_trail), so its own recorded fingerprint is normally
    bit-identical to the released commit's -- the tree_a == tree_b
    short-circuit in _changed_paths_outside_gongbang covers that case
    without ever shelling out to git diff.

    Scoped by JOB, not by attempt_id equality, because engine.full_orbit.
    _maybe_release_or_done opens a BRAND NEW attempt (via open_job) for
    the release itself, strictly after acceptance/app_security already
    ran and were recorded against an EARLIER attempt of the same job --
    attempt_id equality would make this refuse every legitimate
    full_orbit release too. engine.orbit.continue_job's own commit-stage
    handler, which calls release.prepare() directly for the `gb2 run`
    -> `gb2 ship` path (engine.full_orbit/engine.acceptance/engine.
    app_security are never invoked there), still opens its OWN job (see
    tools/gb2's cmd_run -> engine.steps.open_job.open_job) -- so a
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

    Until engine.full_orbit (or engine.app_security itself) is changed
    to call engine.app_security.record() after .check() on the real
    release path, NO job will ever satisfy this function's app_security
    half at all -- not a fingerprint problem, there is simply no event
    yet."""
    if not fingerprint or not _tree_exists(repo, fingerprint):
        # The caller's own, just-computed release fingerprint must
        # itself be a real tree -- defensive only (engine.evidence.
        # fingerprint() writes the tree into this same repo synchronously
        # right before any caller ever gets a value back from it, so
        # this should never actually fire), but a malformed/garbage
        # value here must still refuse cleanly, never crash the diff
        # below with a git error.
        return False, _JOB_CERTIFICATE_MISSING_REASON
    job_name = _job_name_for_attempt(ledger, project, attempt_id)
    if job_name is None:
        return False, _JOB_CERTIFICATE_MISSING_REASON
    attempt_ids = frozenset(
        row.attempt_id for row in ledger.attempts_for_job_name(project, job_name)
    )
    if not attempt_ids:
        return False, _JOB_CERTIFICATE_MISSING_REASON

    changed: list[str] = []
    for kind in ("acceptance.recorded", "app_security.recorded"):
        recorded_fp = _latest_passing_fingerprint(ledger, project, kind, attempt_ids)
        if recorded_fp is None:
            return False, _JOB_CERTIFICATE_MISSING_REASON
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
    # certificate's own policy_hash (engine.app_security.
    # security_policy_hash, computed at check()-time) is compared
    # against the SAME hash recomputed from the CURRENT config; a
    # mismatch means the certificate speaks for a policy that no
    # longer applies, security has to rerun. `config=None` (a caller
    # that has not been updated to pass it yet) skips this check
    # entirely -- see this function's own call sites for which ones
    # still need updating.
    if config is not None:
        from engine import app_security  # lazy import, see full_orbit's own convention
        recorded_policy_hash = _latest_passing_app_security_policy_hash(
            ledger, project, attempt_ids,
        )
        if recorded_policy_hash is None:
            return False, _JOB_CERTIFICATE_MISSING_REASON
        if recorded_policy_hash != app_security.security_policy_hash(config):
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


def _ledger_generation(ledger: Ledger, project: str) -> int:
    """A number that strictly increases with every ledger write for
    this project (the seq of its latest event, 0 if none yet) -- used
    as a bench payload nonce so a resubmission forced by
    ensure_full_bench (because the evidence check says fresh evidence
    is required even though submit()'s normal, deterministic
    request_id already names an old, terminal request) always gets a
    request_id submit() has never seen before, rather than
    deterministically colliding with that same old request again."""
    events = ledger.events(project)
    return events[-1][0] if events else 0


def ensure_full_bench(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                      attempt_id: str, max_iterations: int = 50) -> dict[str, object]:
    """Before advance_master ever runs for a ship: the commit being
    released must have a FULL-suite passing bench recorded against its
    exact fingerprint, not merely whatever scope satisfied the earlier
    commit step's three_match (bench_scope "related" is enough for
    that). If the latest passing, non-stale bench evidence for the
    current fingerprint already has scope "full"/"full(fallback)",
    this is a no-op ("ran": False).

    Otherwise submits a bench request with payload {"fp": fp, "scope":
    "full"} -- engine.steps.bench_run._resolve_test_argv treats
    payload["scope"] == "full" as an override that skips the
    related-tests shortcut entirely, regardless of the project's own
    bench_scope/related_cmd config. submit() is deterministic
    (request_id = hash of the payload), so if an EARLIER bench request
    with this exact payload already exists and is already terminal
    (done/failed/unknown) -- e.g. it passed once, then its evidence
    was later invalidated (an install) or marked stale -- submit()
    would just hand back that same old, already-resolved request_id
    with no new bench run at all, and trusting its "done" status as-is
    would silently resurrect a since-invalidated pass. So: if the
    request submit() returns is already terminal, resubmit once with
    _ledger_generation() mixed into the payload, forcing a request_id
    that has never existed before and therefore WILL actually run.
    Either way, the final "passed" is read back from the ledger's own
    evidence for this exact fingerprint (_latest_full_bench_passed),
    never from the bench request's own status -- the request's status
    is used only to drive run_request's resume loop, never trusted as
    the verdict by itself.

    Returns {"ran", "passed", "fp", "request_id"?, "status"?}; never
    raises -- a request kind this project doesn't support, or any
    other failure, surfaces as "passed": False."""
    project_config = config.projects[project]
    fp = evidence.fingerprint(project_config.repo, project_config.excludes)
    if _latest_full_bench_passed(ledger, attempt_id, fp):
        return {"ran": False, "passed": True, "fp": fp}

    payload: dict[str, object] = {"fp": fp, "scope": "full"}
    request_id = submit(ledger, project, attempt_id, "bench", payload, "owner")
    if ledger.request(request_id).status not in ("queued", "running"):
        # submit() handed back an already-terminal request for this
        # exact payload -- see the docstring above for why that must
        # not be trusted as-is here.
        payload = {**payload, "_generation": _ledger_generation(ledger, project)}
        request_id = submit(ledger, project, attempt_id, "bench", payload, "owner")

    processor = Processor(config, ledger, fakes)
    processor.register_defaults()
    # run_request (not run_once): this project may ALREADY have the
    # publish request itself sitting queued/approved (prepare() submits
    # it, and approve_and_run grants it, before ever calling this) --
    # run_once would drain the WHOLE project queue in one call, which
    # could dispatch (or, if not yet approved, wrongly fail with
    # "no_approval") that unrelated publish request as a side effect of
    # just trying to run this bench. run_request touches ONLY this one
    # request_id.
    status = ledger.request(request_id).status
    for _ in range(max_iterations):
        if status not in ("queued", "running"):
            break
        status = processor.run_request(project, request_id)
    request = ledger.request(request_id)
    return {
        # The verdict is the ledger's own evidence for this exact fp,
        # never request.status alone (see docstring).
        "ran": True, "passed": _latest_full_bench_passed(ledger, attempt_id, fp), "fp": fp,
        "request_id": request_id, "status": request.status, "result": request.result,
    }


def approve_and_run(config: EngineConfig, ledger: Ledger, fakes: Fakes, project: str,
                    request_id: str, owner_text: str,
                    max_iterations: int = 50) -> dict[str, object]:
    """Parse `owner_text` (must be exactly "승인 R-..." per
    approvals.parse_owner_message), refuse if it names a different
    request than `request_id`, then -- BEFORE ever granting an
    approval, so a failed bench never leaves a valid, unused approval
    sitting next to a still-queued publish request for a LATER `gb2
    skip-ship`/`gb2 fix` queue-drain to pick up and deploy -- require
    ensure_full_bench to pass. A failed or still-running full bench
    refuses the whole ship ("reason": "full_bench_failed") with no
    approval ever granted; the request itself is left exactly as the
    caller finds it (callers that must guarantee it can never be
    dispatched later -- e.g. tools/gb2's `ship` -- call
    cancel_request/cancel_pending_publish themselves on this outcome).
    Only once the bench passes is the approval granted, then a fresh
    Processor's run_once driven in a bounded loop until the publish
    request leaves queued/running (done/failed/unknown). Never calls
    the real deploy script itself -- that only happens if
    `fakes.publisher` is wired to a real ShellPublisher by the
    caller."""
    parsed = parse_owner_message(owner_text)
    if parsed is None:
        return {"status": "rejected", "reason": "owner_text_malformed",
                "request_id": request_id}
    if parsed != request_id:
        return {"status": "rejected", "reason": "owner_text_wrong_request",
                "request_id": request_id}
    try:
        attempt_id = ledger.request(request_id).attempt_id
    except LedgerError as exc:
        return {"status": "rejected", "reason": f"unknown_request: {exc}",
                "request_id": request_id}

    bench_check = ensure_full_bench(config, ledger, fakes, project, attempt_id)
    if not bench_check["passed"]:
        return {"status": "rejected", "reason": "full_bench_failed",
                "request_id": request_id, "bench": bench_check}

    try:
        grant(ledger, project, request_id, ttl_sec=config.approval_ttl_sec)
    except ApprovalError as exc:
        return {"status": "rejected", "reason": str(exc), "request_id": request_id}

    processor = Processor(config, ledger, fakes)
    processor.register_defaults()
    # run_request, same reasoning as ensure_full_bench: touch only this
    # request_id, never the whole project queue.
    status = ledger.request(request_id).status
    for _ in range(max_iterations):
        if status not in ("queued", "running"):
            break
        status = processor.run_request(project, request_id)
    request = ledger.request(request_id)
    return {"status": request.status, "result": request.result,
            "request_id": request_id, "bench": bench_check}


def cancel_pending_publish(ledger: Ledger, project: str, attempt_id: str,
                           reason: str) -> list[str]:
    """Cancel every still-QUEUED 'publish' request of `attempt_id`
    (engine.processor.cancel_request -> status 'failed', result
    {"reason": reason}) and revoke every one of ITS approvals that has
    not yet been used -- an 'approval.used' event, the exact derived
    effect approvals.check's own successful path records, so a later
    approvals.check against it reports "used" (refused) rather than
    ever succeeding. Defense in depth for the window a full-bench
    failure used to leave open (approve_and_run now refuses to grant
    an approval at all until ensure_full_bench passes, so there is
    normally nothing here left to revoke -- this guards the case
    where an approval exists anyway, e.g. this function being called
    again after a partial failure, or a future caller granting one
    through some other path).

    A publish request that is already 'running'/'done'/'failed'/
    'unknown' is left completely untouched: it is either already
    resolved, or (running) has an external effect that must be
    reconciled through `gb2 resolve`/retry_unknown, never force-
    cancelled here. Returns the request_ids actually cancelled,
    oldest-submitted first; [] if this attempt has no queued publish
    request at all.

    Holds the project lock (the same one Processor.run_once/run_request
    takes) for the whole read-queued-requests-then-cancel sequence, not
    just the final write: without it, a caller's own "is this safe to
    cancel" check (e.g. tools/gb2's `skip-ship`, which checks
    _project_lock_is_free and the publisher's own receipt BEFORE calling
    this) and this function's cancellation could straddle a live
    processor's dispatch -- a request seen 'queued' here could flip to
    'running'/dispatched between that caller's own check and this
    function's own read, cancelling a request a processor had, by then,
    already started delivering. Raises ProcessorBusy (no ledger write at
    all) if the lock is already held by a live processor -- callers must
    retry rather than proceed as if nothing needed cancelling."""
    holder = f"cancel-pending-publish-{uuid.uuid4().hex[:8]}"
    if not ledger.acquire_lock(project, holder):
        raise ProcessorBusy(f"project {project!r} locked by another processor")
    try:
        cancelled: list[str] = []
        for request in ledger.queued_requests(project):
            if request.kind != "publish" or request.attempt_id != attempt_id:
                continue
            if request.status != "queued":
                continue  # 'running': has an external effect -- not ours to touch
            cancel_request(ledger, project, request.request_id, reason)
            _revoke_unused_approvals(ledger, project, attempt_id, request.request_id)
            cancelled.append(request.request_id)
        return cancelled
    finally:
        ledger.release_lock(project, holder)


def _revoke_unused_approvals(ledger: Ledger, project: str, attempt_id: str,
                             request_id: str) -> None:
    """'approval.used' for every one of `request_id`'s approvals that
    has not yet been used -- the exact derived effect approvals.check's
    own successful path records, so a later approvals.check against it
    reports "used" (refused) rather than ever succeeding. Shared by
    cancel_pending_publish and cancel_running_publish_before_dispatch."""
    for approval in ledger.approvals_for(request_id):
        if approval.used_seq is None:
            ledger.append(Event(
                project=project, kind="approval.used",
                actor_kind="owner", actor_id="owner", attempt_id=attempt_id,
                payload={"approval_id": approval.approval_id},
            ))


def cancel_running_publish_before_dispatch(ledger: Ledger, project: str,
                                           request_id: str, reason: str) -> bool:
    """For a publish request currently 'running': cancel it (status
    'failed', result {"reason": reason}) and revoke its unused
    approvals, exactly like cancel_pending_publish does for a queued
    one -- but ONLY IF this request's own step journal shows
    call_publisher has no entry AT ALL yet (not even 'started') --
    the dispatch step that actually calls fakes.publisher.publish().
    Returns True if cancelled, False (no ledger mutation at all) if
    call_publisher's journal entry already exists ('started' or
    'done') -- the caller must then refuse and point the owner at
    `gb2 resolve` instead, never force this.

    This is a JOURNAL check only. engine.processor._run_steps journals
    a step 'started' strictly BEFORE that step's own run() executes,
    so a crash in the narrow window between that journal write and
    call_publisher's run() actually calling publish() would also show
    'started' with no receipt on disk yet at all -- deliberately
    treated here as "might already be dispatching", never as safe to
    cancel. This function now holds the project lock itself (see
    below) for its own status/journal checks and cancellation write,
    so a caller no longer needs the lock held to make those checks
    safe -- but callers (tools/gb2's `skip-ship`) are still
    responsible for confirming, before ever calling this, that no
    receipt exists for this key at all: the publisher's own receipt
    is not read here.

    Raises CancelError if the request does not belong to `project` or
    is not 'running' (same guard cancel_request uses for 'queued').

    Holds the project lock (the same one Processor.run_once/run_request
    and cancel_pending_publish take) for the whole status-check,
    journal-check and cancellation-write sequence -- cancel_pending_
    publish's own docstring explains why this matters for a queued
    request, and the same race exists here for a running one: without
    the lock, this function's own "journal shows no call_publisher
    entry yet" read and its "failed" status write are not atomic with
    a live processor concurrently running THIS SAME request's steps
    under that lock. A processor that is mid-dispatch when this
    function reads an empty journal would go on to call
    Processor._set_status(..., "done", ...) once it finishes --
    unconditionally overwriting the "failed" cancellation this
    function just wrote, so the deploy would proceed (and be recorded
    as done) despite the cancellation. Holding the lock here makes
    that impossible: a processor cannot even begin draining this
    project's queue while this function holds it, so a confirmed-empty
    journal read stays true until this function's own write lands.
    Raises ProcessorBusy (no ledger write at all) if the lock is
    already held by a live processor -- callers must retry rather
    than proceed as if nothing needed cancelling."""
    holder = f"cancel-running-publish-{uuid.uuid4().hex[:8]}"
    if not ledger.acquire_lock(project, holder):
        raise ProcessorBusy(f"project {project!r} locked by another processor")
    try:
        request = ledger.request(request_id)
        if request.project != project:
            raise CancelError(
                f"request {request_id} belongs to project {request.project!r}, "
                f"not {project!r}"
            )
        if request.status != "running":
            raise CancelError(f"request {request_id} is {request.status!r}, not running")
        if ledger.steps(request_id).get("call_publisher") is not None:
            return False
        ledger.append(Event(
            project=project, kind="request.status", actor_kind="owner",
            actor_id="owner", attempt_id=request.attempt_id,
            payload={"request_id": request_id, "status": "failed",
                     "result": {"reason": reason}},
        ))
        _revoke_unused_approvals(ledger, project, request.attempt_id, request_id)
        return True
    finally:
        ledger.release_lock(project, holder)


__all__ = [
    "head_sha", "is_fast_forwardable", "master_dirty_tracked_paths",
    "master_untracked_paths", "master_untracked_or_ignored_paths",
    "master_is_clean", "merge_range_paths", "incoming_added_paths",
    "overlapping_paths", "ff_merge", "build_summary",
    "prepare", "approve_and_run", "ensure_full_bench", "cancel_pending_publish",
    "cancel_running_publish_before_dispatch", "job_certificate_status",
]
