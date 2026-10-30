"""Pure matching rules for owner "standing approval":
an owner-set policy file names a NARROW set of request shapes the
engine may approve on its own, without stopping for a human, because
the owner decided ahead of time they are low-risk enough to pre-clear.

This module never touches disk, the ledger, or wall-clock time -- the
policy dict and whatever "current" state a caller already has in hand
(a manifest's current sections) are passed in as plain values, so
every rule here is covered by a unit test with no fakes at all.
engine.application.standing_approval is the thin, I/O-reading wrapper
(loads the policy file, reads the real manifest, calls approve_pending/
appends design.approved) that actually uses this module.

Two allowed kinds (ALLOWED_STANDING_KINDS), deliberately narrow and
closed -- the owner adds a new one here, nowhere else:

  dependency_patch_bump -- an "install" owner request whose every
      changed package already sits in the SAME manifest section
      (never a new package, never a moved section) and whose new
      version is the same semver MAJOR as what is there now (0.x
      requires the same MINOR too, semver's own convention that 0.x
      has no stability guarantee across minors). A new dependency, a
      section move, or any version string this module cannot parse as
      a plain X.Y.Z is never covered -- the policy covers a patch
      bump, nothing wider.

  design_approval -- NOT an owner-request kind at all (a design
      approval is not a `requests[]` entry; see job_state.
      STAGE_DESIGN_APPROVAL) -- this module exposes no matching
      function for it (there is nothing to match: once the policy
      allows it for an app, every pending design stop qualifies). It
      is listed in ALLOWED_STANDING_KINDS only so the policy file's
      shape-check accepts the string; engine.application.
      standing_approval decides when a design stop is genuinely
      pending.

결함: a THIRD kind,
"scripts_change_existing_runner" (a package.json "scripts" edit whose
value matched a fixed node/vite/vitest/next/tsc allowlist), existed
here briefly and was removed -- an external review found it could be
defeated by wrapping the exact same payload inside an already-
installed runner (e.g. a devDependency binary that itself execs
anything, or a `node` script whose "plain relative path" argument is
itself a file the attacker's own request just created) and that its
lifecycle-hook exclusion (prefix-based) could miss a hook name this
module's own author did not anticipate. Standing approval now never
auto-approves ANY script/shell-command content at all -- only a
version-number comparison on an already-trusted package, and an
already-separately-approved design artifact. A scripts_change request
of any shape always falls through to None in this module (never
matched) and keeps waiting for the owner.

A malformed/missing policy is never an error here -- see EMPTY_POLICY
and parse_policy's own docstring: "no policy" and "a config file this
module cannot make sense of" are deliberately the SAME outcome (fail
closed to the pre-existing behavior: the owner is asked)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping

ALLOWED_STANDING_KINDS: frozenset[str] = frozenset(
    {"dependency_patch_bump", "design_approval", "dependency_install"}
)

# "dependency_install" (policy: external packages are allowed): ANY
# install request that touches only dependency sections of a JSON manifest (new packages included, any version specifier that
# install_request_shape already admits). Still policy-gated per app, still
# never scripts/config/other kinds -- the owner lists it in the gate file.
_DEPENDENCY_SECTIONS = frozenset(
    {"dependencies", "devDependencies", "optionalDependencies", "peerDependencies"}
)


def is_dependency_only_install(change: object) -> bool:
    """True iff `change` is a non-empty mapping of dependency sections only,
    each a non-empty mapping of package name -> version specifier string
    (install_request_shape.VERSION_SPECIFIER_RE)."""
    from engine.domain.install_request_shape import VERSION_SPECIFIER_RE

    if not isinstance(change, Mapping) or not change:
        return False
    for section, packages in change.items():
        if section not in _DEPENDENCY_SECTIONS:
            return False
        if not isinstance(packages, Mapping) or not packages:
            return False
        for name, spec in packages.items():
            if not isinstance(name, str) or not name.strip():
                return False
            if not isinstance(spec, str) or not VERSION_SPECIFIER_RE.match(spec):
                return False
    return True

# A plain `X.Y.Z` prefix -- deliberately not a full semver-range
# grammar (install_request_shape.VERSION_SPECIFIER_RE already allows
# range operators like "^"/"~"/">="; this module only ever accepts a
# PLAIN version here, same convention validate_pending's own shape
# check uses elsewhere in this codebase for "the exact thing that will
# be installed", never a range that could resolve to something wider
# than what the owner actually saw).
_PLAIN_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def _parse_plain_version(spec: str) -> tuple[int, int, int] | None:
    if not isinstance(spec, str):
        return None
    text = spec.strip()
    if any(ch in text for ch in (" ", ",", "||")):
        # A range/compound specifier ("1.0.0 - 2.0.0", ">=1.0.0 <2.0.0",
        # "1.0.0 || 2.0.0") is never a single version this module can
        # safely compare -- never covered by a patch-bump rule.
        return None
    match = _PLAIN_VERSION.match(text)
    if match is None or match.end() != len(text):
        return None
    major, minor, patch = (int(g) for g in match.groups())
    return major, minor, patch


def is_patch_bump_version(old_spec: str, new_spec: str) -> bool:
    """True iff `new_spec` is the same semver MAJOR as `old_spec`
    (same MINOR too when that major is 0 -- semver's own "0.x has no
    stability guarantee across minors" convention). False for either
    an unparseable specifier (a range, a URL, a tag) or an actual major
    bump."""
    old = _parse_plain_version(old_spec)
    new = _parse_plain_version(new_spec)
    if old is None or new is None:
        return False
    if old[0] != new[0]:
        return False
    if old[0] == 0 and old[1] != new[1]:
        return False
    return True


def is_dependency_patch_bump(change: Mapping[str, object],
                              current_sections: Mapping[str, Mapping[str, str]]) -> bool:
    """True iff `change` (an "install" request's own `change`, already
    shape-validated by owner_approval.validate_pending before this is
    ever called) touches ONLY sections/packages ALREADY present in
    `current_sections` (the manifest's own dependencies/
    devDependencies/optionalDependencies right now), every new version
    a patch bump (is_patch_bump_version) over the one currently there.
    False (never covered) for a brand-new package, a package moved to
    a different section, or any section `current_sections` has no
    entry for at all."""
    if not change:
        return False
    for section, packages in change.items():
        if not isinstance(packages, Mapping) or not packages:
            return False
        current_packages = current_sections.get(section)
        if not isinstance(current_packages, Mapping):
            return False
        for name, new_version in packages.items():
            if not isinstance(name, str) or not isinstance(new_version, str):
                return False
            old_version = current_packages.get(name)
            if old_version is None:
                return False
            if not is_patch_bump_version(old_version, new_version):
                return False
    return True


@dataclass(frozen=True)
class StandingApprovalPolicy:
    """A parsed standing-approvals policy file -- `apps` maps an app
    name (or the literal "*" for every app) to the frozenset of
    standing-approval kinds the owner pre-cleared for it. `raw`/
    `set_at`/`reason` are kept verbatim for the audit trail (an
    'owner.approved' event records this policy's own digest, never its
    content, since the digest is engine.application.
    standing_approval's job to compute over `raw`)."""
    apps: Mapping[str, frozenset[str]] = field(default_factory=dict)
    raw: Mapping[str, object] = field(default_factory=dict)
    set_at: str = ""
    reason: str = ""

    def allowed_kinds(self, app: str) -> frozenset[str]:
        return self.apps.get("*", frozenset()) | self.apps.get(app, frozenset())


EMPTY_POLICY = StandingApprovalPolicy()


def parse_policy(data: object) -> StandingApprovalPolicy:
    """Parses an already-loaded JSON value (the standing-approvals
    policy file's content). Anything this function cannot make sense
    of -- not a dict, no "apps" dict, an app key mapped to something
    other than a list of strings, or any string outside
    ALLOWED_STANDING_KINDS -- returns EMPTY_POLICY (no standing
    approval at all) rather than raising: "malformed policy" and "no
    policy file" must behave identically (fail closed to the pre-
    existing behavior, the owner is asked), never partially-apply a
    policy this module could not fully validate."""
    if not isinstance(data, Mapping):
        return EMPTY_POLICY
    apps_raw = data.get("apps")
    if not isinstance(apps_raw, Mapping):
        return EMPTY_POLICY
    apps: dict[str, frozenset[str]] = {}
    for app, kinds in apps_raw.items():
        if not isinstance(app, str) or not isinstance(kinds, list):
            return EMPTY_POLICY
        kind_set: set[str] = set()
        for kind in kinds:
            if not isinstance(kind, str) or kind not in ALLOWED_STANDING_KINDS:
                return EMPTY_POLICY
            kind_set.add(kind)
        apps[app] = frozenset(kind_set)
    set_at = data.get("set_at")
    reason = data.get("reason")
    return StandingApprovalPolicy(
        apps=apps, raw=dict(data),
        set_at=set_at if isinstance(set_at, str) else "",
        reason=reason if isinstance(reason, str) else "",
    )


__all__ = [
    "ALLOWED_STANDING_KINDS", "StandingApprovalPolicy", "EMPTY_POLICY",
    "parse_policy", "is_patch_bump_version", "is_dependency_patch_bump",
]
