"""Single source of truth for the shape a pending owner-approval
request's `change` must have, keyed by the project's manifest file
type (a JSON manifest's dependency sections vs. a line-list manifest's
`add_lines`).

결함 58: before this module existed, the instruction a builder worker
was given (engine.infrastructure.workers.prompts._INSTALL_REQUEST_SHAPE,
hard-coded to the add_lines shape for every manifest) and the check an
owner-approval request was actually held to
(engine.application.owner_approval._validate_json_install_change, which
only ever accepts a JSON manifest's dependencies/devDependencies/
optionalDependencies section) disagreed for any .json manifest: a
request built exactly as instructed was always rejected with
"manifest_section_not_allowed". Both engine.infrastructure.workers.prompts
(the instruction) and engine.application.owner_approval (the check) now
import JSON_INSTALL_SECTIONS/PACKAGE_NAME_RE/VERSION_SPECIFIER_RE and
install_request_shape_text from here, so the two can never drift apart
again -- change the rule once, both sides follow.

결함 65 (same root as 결함 58): build_prompt's instruction only ever
described the "install" shape, never "scripts_change"/
"test_config_change" -- a real worker that needed to add a package.json
"scripts" entry (e.g. "start") had no instructed shape for that and
filed it as an "install" request instead, which validate_pending
correctly rejects (an "install" kind's `change` may only touch a
dependency section, see JSON_INSTALL_SECTIONS below) with
"manifest_section_not_allowed" -- a reason that does not say what to do
instead. install_request_shape_text now takes a `kind` argument so the
instruction text names the right kind for a non-dependency manifest
edit, and USE_SCRIPTS_CHANGE_KIND_REASON gives owner_approval a reason
code that names the fix (never the rejected section/name/value itself)
instead of the shape-agnostic "manifest_section_not_allowed".

This module is pure (no I/O), so both application (owner_approval) and
infrastructure (workers.prompts) may import it without crossing the
layer-direction rule in docs/standards/architecture.md -- domain sits
below both."""
from __future__ import annotations

import re

# engine.application.owner_approval._validate_json_install_change reuses
# this exact set -- an "install" kind's JSON `change` may only touch
# these top-level sections (never e.g. "scripts": see that function's
# own docstring for the exploit this closes). scripts_change/
# test_config_change are NOT restricted to this set -- they legitimately
# write other sections of the same manifest (e.g. "scripts") -- see
# that function's own docstring; this module does not change that rule.
JSON_INSTALL_SECTIONS: frozenset[str] = frozenset(
    {"dependencies", "devDependencies", "optionalDependencies"}
)

# The two owner-approval kinds that exist for a manifest edit which is
# NOT a dependency bump (engine.application.owner_approval.
# PENDING_KIND_WHITELIST minus "install"). Kept here, not re-derived at
# each call site, so a worker's instruction and owner_approval's reason
# code always name the same kind.
NON_INSTALL_PENDING_KINDS: tuple[str, ...] = ("scripts_change", "test_config_change")

# engine.application.owner_approval.validate_pending returns this (never
# "manifest_section_not_allowed") when an "install" kind's JSON `change`
# touches a section outside JSON_INSTALL_SECTIONS -- the fix is always
# the same regardless of which section was attempted (a dependency bump
# never needs one), so a constant, actionable reason is more useful
# than the generic shape-mismatch one. Still a rejection -- an "install"
# request smuggling a "scripts" backdoor (e.g. postinstall) is rejected
# exactly as before, see owner_approval's own test coverage; this only
# renames WHY, never weakens WHETHER.
USE_SCRIPTS_CHANGE_KIND_REASON = "use_scripts_change_kind"

# npm package name: optional "@scope/" prefix, then lowercase
# alphanumerics/._- (conservative; real npm allows a few more corner
# cases, but this is a validator, not a registry mirror).
PACKAGE_NAME_RE = re.compile(r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")

# A version SPECIFIER's allowed character set, not a full semver-range
# grammar: deliberately excludes ":" and "/", so none of "file:",
# "git+ssh://", "http://", "link:", "workspace:" can ever match -- all
# of them need at least one of those two characters.
VERSION_SPECIFIER_RE = re.compile(r"^[A-Za-z0-9.^~<>=*x+,|\- ]+$")


def is_json_manifest(manifest: str) -> bool:
    """True for a package.json-style manifest (dependency sections);
    False for a line-list manifest (requirements.txt-style add_lines)."""
    return manifest.endswith(".json")


def install_request_shape_text(manifest: str | None, kind: str = "install") -> str:
    """The exact `requests[]` entry shape a worker must use to ask the
    owner for a `kind` change on `manifest` -- "install" for a
    dependency bump, "scripts_change"/"test_config_change" for any
    other manifest edit (e.g. a package.json "scripts" entry or
    test-runner configuration). `manifest=None` (no manifest configured
    for this project/station) falls back to the line-list shape, same
    as before this module existed -- a worker with no configured
    manifest to compare against will be rejected by validate_pending
    regardless of which shape it follows ("manifest_not_configured"),
    so the fallback only affects prompt wording, never approval.

    For a line-list manifest (e.g. requirements.txt), validate_pending
    routes every whitelisted kind through the SAME add_lines check
    (engine.application.owner_approval.validate_pending's own
    manifest.endswith(".json") branch) -- `kind` only changes which
    literal appears in the example text there, never the shape."""
    if manifest is not None and is_json_manifest(manifest):
        if kind == "install":
            return (
                '{"kind": "install", "manifest": "<rel path>", '
                '"change": {"<section>": {"<package name>": "<version>", ...}}} -- '
                "<section> is one of "
                + ", ".join(sorted(JSON_INSTALL_SECTIONS))
                + '; <package name> is a plain npm package name (optional "@scope/" '
                'prefix); <version> is a plain version specifier (no "file:", '
                '"git+ssh://", "http://", "link:", "workspace:")'
            )
        return (
            f'{{"kind": "{kind}", "manifest": "<rel path>", '
            '"change": {"<top-level manifest key>": {"<key>": "<value>", ...}, '
            '...}} -- <top-level manifest key> may be any key of the manifest '
            'that is NOT a dependency section (e.g. "scripts"); each nested '
            "key/value is shallow-merged into that section (existing keys "
            "there are kept unless this overwrites them); every string value "
            "must be plain (no shell metacharacters such as ; & | ` $, no "
            'newline, no "..", no "~", printable characters only)'
        )
    return (
        f'{{"kind": "{kind}", "manifest": "<rel path>", '
        '"change": {"add_lines": ["<name>==<version>", ...]}} -- each line is a '
        "plain requirement specifier (package name, optional ==version); URLs, "
        "file paths, options and markers are rejected"
    )


__all__ = [
    "JSON_INSTALL_SECTIONS",
    "NON_INSTALL_PENDING_KINDS",
    "USE_SCRIPTS_CHANGE_KIND_REASON",
    "PACKAGE_NAME_RE",
    "VERSION_SPECIFIER_RE",
    "is_json_manifest",
    "install_request_shape_text",
]
