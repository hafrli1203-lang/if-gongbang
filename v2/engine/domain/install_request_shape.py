"""Single source of truth for the shape an "install" pending request's
`change` must have, keyed by the project's manifest file type (a JSON
manifest's dependency sections vs. a line-list manifest's `add_lines`).

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

This module is pure (no I/O), so both application (owner_approval) and
infrastructure (workers.prompts) may import it without crossing the
layer-direction rule in docs/standards/architecture.md -- domain sits
below both."""
from __future__ import annotations

import re

# engine.application.owner_approval._validate_json_install_change reuses
# this exact set -- an "install" kind's JSON `change` may only touch
# these top-level sections (never e.g. "scripts": see that function's
# own docstring for the exploit this closes).
JSON_INSTALL_SECTIONS: frozenset[str] = frozenset(
    {"dependencies", "devDependencies", "optionalDependencies"}
)

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


def install_request_shape_text(manifest: str | None) -> str:
    """The exact `requests[]` entry shape a worker must use to ask the
    owner for a dependency change on `manifest`. `manifest=None` (no
    manifest configured for this project/station) falls back to the
    line-list shape, same as before this module existed -- a worker
    with no configured manifest to compare against will be rejected by
    validate_pending regardless of which shape it follows
    ("manifest_not_configured"), so the fallback only affects prompt
    wording, never approval."""
    if manifest is not None and is_json_manifest(manifest):
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
        '{"kind": "install", "manifest": "<rel path>", '
        '"change": {"add_lines": ["<name>==<version>", ...]}} -- each line is a '
        "plain requirement specifier (package name, optional ==version); URLs, "
        "file paths, options and markers are rejected"
    )


__all__ = [
    "JSON_INSTALL_SECTIONS",
    "PACKAGE_NAME_RE",
    "VERSION_SPECIFIER_RE",
    "is_json_manifest",
    "install_request_shape_text",
]
