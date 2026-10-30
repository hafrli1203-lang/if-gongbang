"""Unit tests for engine.domain.standing_approval_policy -- the pure
matching rules behind owner standing approval. No
ledger, no disk, no fakes: every case here is plain values in, a
plain bool/tuple out.

a "scripts_change_existing_runner" kind
used to exist here (a package.json "scripts" edit matched against a
fixed node/vite/vitest/next/tsc allowlist); an external review found
it could be defeated (wrapping the same payload inside an already-
installed tool) and its lifecycle-hook exclusion was a prefix guess,
not a closed list. It was removed entirely -- see module docstring --
so this file now only covers dependency_patch_bump and policy
parsing."""
from __future__ import annotations

from engine.domain.standing_approval_policy import (
    ALLOWED_STANDING_KINDS,
    EMPTY_POLICY,
    is_dependency_patch_bump,
    is_patch_bump_version,
    parse_policy,
)


# ---- semver patch-bump comparison -----------------------------------------

def test_patch_bump_same_major_is_covered() -> None:
    assert is_patch_bump_version("16.3.6", "16.4.3") is True


def test_patch_bump_same_major_zod_style_is_covered() -> None:
    assert is_patch_bump_version("4.6.6", "4.6.7") is True


def test_major_bump_is_not_covered() -> None:
    assert is_patch_bump_version("16.3.6", "17.0.0") is False


def test_zero_major_requires_same_minor_too() -> None:
    assert is_patch_bump_version("0.3.1", "0.3.9") is True
    assert is_patch_bump_version("0.3.1", "0.4.0") is False


def test_range_specifier_is_never_covered() -> None:
    assert is_patch_bump_version("^16.3.6", "16.4.3") is False
    assert is_patch_bump_version("16.3.6", ">=16.4.3 <17.0.0") is False


# ---- dependency_patch_bump (install change vs. current manifest) ---------

_CURRENT = {"dependencies": {"next": "16.3.6", "zod": "4.6.6"},
            "devDependencies": {"vitest": "1.0.0"}}


def test_patch_bump_of_two_existing_packages_is_covered() -> None:
    change = {"dependencies": {"next": "16.4.3", "zod": "4.6.7"}}
    assert is_dependency_patch_bump(change, _CURRENT) is True


def test_new_package_is_not_covered() -> None:
    change = {"dependencies": {"left-pad": "1.0.0"}}
    assert is_dependency_patch_bump(change, _CURRENT) is False


def test_major_bump_of_existing_package_is_not_covered() -> None:
    change = {"dependencies": {"next": "17.0.0"}}
    assert is_dependency_patch_bump(change, _CURRENT) is False


def test_section_move_is_not_covered() -> None:
    # "next" exists under dependencies, not devDependencies -- a change
    # claiming it under devDependencies is a section move, not a bump.
    change = {"devDependencies": {"next": "16.4.3"}}
    assert is_dependency_patch_bump(change, _CURRENT) is False


def test_empty_change_is_not_covered() -> None:
    assert is_dependency_patch_bump({}, _CURRENT) is False


# ---- allowed kinds are closed: dependency shapes + design only -------------

def test_allowed_standing_kinds_is_closed() -> None:
    """a scripts/command edit of any shape is never a
    standing-approval kind. dependency_install (external packages
    allowed) is still a dependency-section-only shape."""
    assert ALLOWED_STANDING_KINDS == {"dependency_patch_bump", "design_approval", "dependency_install"}


# ---- policy parsing --------------------------------------------------------

def test_no_apps_key_is_empty_policy() -> None:
    assert parse_policy({"reason": "x"}) is EMPTY_POLICY


def test_unknown_kind_in_policy_is_empty_policy() -> None:
    assert parse_policy({"apps": {"sample-app": ["something_else"]}}) is EMPTY_POLICY


def test_scripts_change_kind_is_no_longer_a_known_policy_kind() -> None:
    assert parse_policy({"apps": {"sample-app": ["scripts_change_existing_runner"]}}) is EMPTY_POLICY


def test_well_formed_policy_parses() -> None:
    policy = parse_policy({
        "apps": {"*": ["dependency_patch_bump"], "sample-app": ["design_approval"]},
        "reason": "r", "set_at": "t",
    })
    assert policy.allowed_kinds("sample-app") == {"dependency_patch_bump", "design_approval"}
    assert policy.allowed_kinds("other-app") == {"dependency_patch_bump"}


# ---- dependency_install (external packages allowed) -----------

from engine.domain.standing_approval_policy import ALLOWED_STANDING_KINDS, is_dependency_only_install  # noqa: E402


def test_dependency_install_is_an_allowed_kind() -> None:
    assert "dependency_install" in ALLOWED_STANDING_KINDS
    policy = parse_policy({"apps": {"cafe-site": ["dependency_install"]}})
    assert "dependency_install" in policy.allowed_kinds("cafe-site")


def test_dependency_only_install_accepts_new_packages_in_dependency_sections() -> None:
    assert is_dependency_only_install({"devDependencies": {"jsdom": "^25.0.1"}}) is True
    assert is_dependency_only_install({"dependencies": {"zod": "4.6.7"}, "peerDependencies": {"react": ">=18"}}) is True


def test_dependency_only_install_rejects_scripts_empty_or_bad_specs() -> None:
    assert is_dependency_only_install({"scripts": {"start": "node x.js"}}) is False
    assert is_dependency_only_install({}) is False
    assert is_dependency_only_install({"dependencies": {}}) is False
    assert is_dependency_only_install({"dependencies": {"left-pad": "1.0.0; rm -rf /"}}) is False
    assert is_dependency_only_install("devDependencies") is False
