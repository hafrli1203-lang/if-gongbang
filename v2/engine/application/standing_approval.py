"""Owner standing approval: reads the owner-maintained
policy file and decides, for ONE pending owner request or ONE pending
design stop, whether it is narrow enough to approve without stopping
for the owner -- the actual approve_pending call + processor drain +
resume is each caller's own job (tools/gb2lib/watch_ops.py's standing-
approval branch, the one place this module is wired into the automatic
`gb2 watch --loop --resume` path today), same layering split
owner_approval.py itself uses (a pure validate_pending check here, the
actual submit/append done by the caller that already holds a confirm).

Policy file location (kept outside this repo
entirely): `~/.claude/gate/standing-approvals.json`, a directory an
EXISTING, unrelated hook (the same "승인 관문" gate every other
irreversible-action approval in this workspace lives under) already
refuses to let any model tool call touch, full stop -- no
scope_fence_hook-style file-specific pattern-matching for this module
to maintain or get wrong. Only the owner's own `!`-run
harness/set_standing_approvals.py writes here. `GB2_STANDING_APPROVALS_
PATH` overrides the path for a TEST run ONLY (gated on
`PYTEST_CURRENT_TEST` being set, which pytest itself sets for the
duration of every test and nothing else ever does) -- a real caller,
even one with that env var set by accident, always reads the real gate
file. No file, or a file this module cannot parse -> EMPTY_POLICY
(identical to "no standing approval at all" -- see
standing_approval_policy.parse_policy's own docstring on why malformed
and missing are the same outcome).

Two allowed kinds, matched against a pending request's REAL current
state (never the request's own claim alone):
  dependency_patch_bump -- an "install" request; see
      standing_approval_policy.is_dependency_patch_bump. Needs the
      manifest's CURRENT dependency sections, read fresh from the repo
      (_current_json_sections) -- never trusted from the request
      itself, which could claim an old_version that is not what is
      actually on disk right now.
  design_approval        -- not a request at all; see
      design_standing_approved below.

결함: a third kind covering a
package.json "scripts" edit existed here briefly and was removed --
see standing_approval_policy's own module docstring for why (a fixed
runner allowlist could be defeated by wrapping the same payload inside
an already-installed tool, and its lifecycle-hook exclusion was a
prefix guess, not a closed list). request_standing_kind below now
never matches a "scripts_change" request at all; it always falls
through to None (ask the owner).

결함: a batch mixing a covered "install" request
with an "edit" (or any other, unknown) kind used to be invisible to
the all-or-nothing check here, because the caller (tools/gb2lib/
watch_ops.py) queried ONLY office_approval.pending_install_requests
(itself filtered to owner_approval.PENDING_KIND_WHITELIST) -- a
genuinely pending request of a kind outside that whitelist was simply
never looked at, so a batch the owner would see as "two different
things waiting" could auto-approve the one install request and leave
the job looking like it was handled. The fix lives in the CALLER now
(watch_ops.maybe_standing_approve queries the FULL, unfiltered
job_state_requests.pending_owner_requests and passes every one of them
through all_requests_match_standing): any kind this module's
request_standing_kind does not recognize (install outside the policy,
"edit", anything whitelist-filtering used to hide) already returns
None here, so passing the unfiltered list through unchanged is
sufficient -- no new matching logic was needed, only a wider INPUT."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from engine.domain import ids
from engine.domain.install_request_shape import JSON_INSTALL_SECTIONS
from engine.domain.records import Event
from engine.domain.standing_approval_policy import (
    EMPTY_POLICY,
    StandingApprovalPolicy,
    is_dependency_patch_bump,
    parse_policy,
    is_dependency_only_install,
)

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger

# The owner-only policy file's one real location -- a directory an
# EXISTING hook already refuses any model tool call against
# unconditionally (see module docstring). `_POLICY_PATH_ENV` is read
# ONLY while pytest is actually running (PYTEST_CURRENT_TEST, which
# pytest itself sets -- see _policy_path), so a real caller can never
# be redirected by that env var, by accident or otherwise.
DEFAULT_POLICY_PATH = Path(os.path.expanduser("~/.claude/gate/standing-approvals.json"))
# (no environment override -- see _policy_path)


def _policy_path(override: Path | None = None) -> Path:
    # No environment override at all: any process could set one.
    # Tests replace DEFAULT_POLICY_PATH on this module instead.
    if override is not None:
        return override
    return DEFAULT_POLICY_PATH


def load_policy(path: Path | None = None) -> StandingApprovalPolicy:
    """EMPTY_POLICY if the file does not exist or cannot be parsed as
    JSON -- see standing_approval_policy.parse_policy's own docstring
    for why a malformed file is never treated as an error (fail closed
    to "no standing approval", the owner is asked, same as before this
    feature existed)."""
    real_path = _policy_path(path)
    try:
        text = real_path.read_text(encoding="utf-8")
    except OSError:
        return EMPTY_POLICY
    try:
        data = json.loads(text)
    except ValueError:
        return EMPTY_POLICY
    return parse_policy(data)


def policy_digest(policy: StandingApprovalPolicy) -> str:
    """A stable identity for `policy.raw`, recorded on every event this
    module's caller appends -- never the policy's own content (already
    on disk, nothing secret), just enough for an audit trail to show
    EXACTLY which policy version produced a given auto-approval."""
    return ids.content_hash(policy.raw)


def _current_json_sections(repo: Path, manifest_rel: str) -> dict[str, dict[str, str]]:
    """The manifest's CURRENT dependencies/devDependencies/
    optionalDependencies sections, read fresh from `repo/manifest_rel`
    -- {} for anything this cannot read as a JSON object (missing
    file, not valid JSON, not a dict), which is also what
    is_dependency_patch_bump treats as "nothing already there", i.e.
    never a match -- a manifest standing approval cannot even read is
    never auto-approved."""
    path = repo / manifest_rel
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for section in JSON_INSTALL_SECTIONS:
        value = loaded.get(section)
        if isinstance(value, dict):
            out[section] = {
                str(name): version for name, version in value.items()
                if isinstance(name, str) and isinstance(version, str)
            }
    return out


def request_standing_kind(policy: StandingApprovalPolicy, app: str,
                           request: dict, repo: Path) -> str | None:
    """The ALLOWED_STANDING_KINDS value this ONE pending request
    matches under `policy` for `app`, or None if it is not covered --
    either the app's policy does not list "dependency_patch_bump", the
    request's `kind` is not "install" at all (every other kind,
    including "scripts_change" and anything this codebase does not
    even define, is NEVER covered -- see module docstring), or its
    actual shape (checked against the manifest's real current content)
    fails the narrow rule. Never raises -- an unreadable manifest, a
    request missing an expected field, all fall through to None (ask
    the owner, the pre-existing behavior)."""
    if not isinstance(request, dict):
        return None
    if request.get("kind") != "install":
        return None
    if not (policy.allowed_kinds(app) & {"dependency_patch_bump", "dependency_install"}):
        return None
    change = request.get("change")
    manifest = request.get("manifest")
    if not isinstance(change, dict) or not isinstance(manifest, str):
        return None
    if not manifest.endswith(".json"):
        return None
    allowed = policy.allowed_kinds(app)
    current = _current_json_sections(repo, manifest)
    if "dependency_patch_bump" in allowed and current and is_dependency_patch_bump(change, current):
        return "dependency_patch_bump"
    # dependency_install: the policy that allows external packages
    # -- any dependency-only change, policy-gated per app.
    if "dependency_install" in allowed and is_dependency_only_install(change):
        return "dependency_install"
    return None


def all_requests_match_standing(policy: StandingApprovalPolicy, app: str,
                                 requests: list[dict], repo: Path) -> list[str] | None:
    """[matched_kind, ...] (same order as `requests`) iff EVERY request
    in a non-empty `requests` matches standing policy -- None (never a
    partial list) the moment any ONE request does not match, or when
    `requests` is empty. All-or-nothing by design (지시문): a batch
    with even one request outside the narrow rule must stop for the
    owner exactly as before, never auto-approve the others and leave
    one behind."""
    if not requests:
        return None
    matched: list[str] = []
    for request in requests:
        kind = request_standing_kind(policy, app, request, repo)
        if kind is None:
            return None
        matched.append(kind)
    return matched


def design_standing_approved(ledger: "Ledger", project: str, job_name: str,
                              attempt_id: str, repo: Path, *,
                              policy: StandingApprovalPolicy | None = None) -> bool:
    """True iff `project`'s policy allows "design_approval" AND the job
    is genuinely stopped awaiting one right now (office_approval.
    design_actually_pending) -- and, on True, a 'design.approved' event
    was just appended (confirm_source="standing", target_version pinned
    via office_approval.design_target_version exactly like
    `gb2 approve-design` does, plus this policy's own digest for the
    audit trail) so the EXISTING resume gate (office_approval.
    design_approval_still_matches / stage_checks.check_design_approved)
    sees it exactly as any other design approval and lets the build
    stage proceed. False, nothing appended, if the policy does not
    cover it OR there is no genuine design stop to approve (never
    appends a design.approved event for a job that is not actually
    waiting on one)."""
    policy = policy if policy is not None else load_policy()
    if "design_approval" not in policy.allowed_kinds(project):
        return False
    from engine.application.office_approval import DesignArtifactUnreadable, design_actually_pending, design_target_version

    if not design_actually_pending(ledger, project, job_name):
        return False
    try:
        target_version = design_target_version(repo, job_name)
    except DesignArtifactUnreadable:
        return False
    ledger.append(Event(
        project=project, kind="design.approved", actor_kind="processor",
        actor_id="watch", attempt_id=attempt_id,
        payload={"job": job_name, "confirm_source": "standing",
                 "target_version": target_version,
                 "standing_policy_digest": policy_digest(policy)},
    ))
    return True


__all__ = [
    "DEFAULT_POLICY_PATH", "load_policy", "policy_digest",
    "request_standing_kind", "all_requests_match_standing",
    "design_standing_approved",
]
