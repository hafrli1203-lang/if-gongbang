"""merge — integrate worker copies into the project repo by allowed paths.

Request payload contract:
  {"copies": [{"role": str, "station": str, "path": str}, ...]}
Allowed write prefixes are never taken from the request. They are derived
from config.station_owners[station][role] (unknown station/role ->
StepFailed("unknown_owner")). Copies are keyed by their list index, not
by role, in every internal structure.

`path` must be EXACTLY realpath(copy_root(config, attempt_id, role)) --
the one copy open_job created for that role under this attempt, not
merely "somewhere under home/copies/{attempt_id}/" -- else StepFailed
("copy_path_mismatch"). The weaker "somewhere under home" check used to
let a worker nest an arbitrary subdirectory inside its OWN copy (e.g.
tester_copy/tests/bench/payload/src/app.py) and submit that nested dir
claiming a different role/station with broader write access (#2). Since
copy_root is a pure function of (attempt_id, role), this also means two
copies in one request can never legitimately claim the same role --
"two parallel builders" must be two distinct roles with their own
config-derived prefixes, not two directories both claiming "builder".

Every candidate relative path is rejected (StepFailed("path_rejected"),
before anything is written) if it is absolute, contains a ".."
component, resolves outside the repo, or is policy-protected
(Policy.is_protected). A symlink found under an allowed prefix fails the
whole request (StepFailed("symlink_in_copy")) rather than being
followed. File content is compared and copied as bytes, never text, so
binary files never raise UnicodeDecodeError.

collect_copies      validates copies/paths and computes, for each copy
                    index, the files under its config-derived allowed
                    prefixes whose content differs from the copy's BASE
                    (job.opened's "base_head" — the sha the copy was made
                    from, NOT the project's current HEAD, which may have
                    advanced past base_head via another request's merge+
                    commit since this copy was created — B-10). Journals
                    a per-copy, per-path content hash (sha256) as the
                    frozen plan: later steps verify against it rather
                    than re-trusting the mutable copy on disk (B-11).
check_conflicts     first re-verifies the live copies still match
                    collect_copies' frozen plan exactly (a copy flipped
                    between collect_copies and check_conflicts, e.g.
                    because check_conflicts itself crashed and is being
                    resumed, must not be judged on stale/mismatched data)
                    -> StepFailed("copy_changed_after_check") if not.
                    Then three kinds of conflict, all -> request failed,
                    nothing written:
                      same relative path changed by two copies with
                      different content -> StepFailed("merge_conflict")
                      a path the copy changes where the CURRENT
                      destination (the real file in the repo right now)
                      already differs from base_head's content for that
                      path -> StepFailed("merge_conflict_with_head")
                      (someone else's committed change landed there since
                      this copy's base; stage 1 has no 3-way merge) --
                      UNLESS this same attempt's own earlier merge
                      already wrote something different there, in which
                      case the destination must instead still match
                      exactly what THAT write put there, or
                      StepFailed("merge_conflict_with_owner_edit")
                      (an owner hand-edited an already-merged path
                      during an owner stop -- e.g. an install pause --
                      while this same attempt stayed active; see
                      _attempt_already_applied_hashes).
                    A path in the project's current dirty_paths (the
                    owner's own pre-existing uncommitted edit, present
                    before any copy was even made) is exempt from both
                    of the above -- it is handled by apply_allowed_paths
                    simply never touching it.
apply_allowed_paths re-verifies every copy's changes against
                    collect_copies' frozen plan (same path set, same
                    hashes) before writing anything -> StepFailed
                    ("copy_changed_after_check") if a copy was edited
                    after collect_copies ran (B-11); then re-checks every
                    touched destination against base ONE more time,
                    immediately before writing (StepFailed
                    ("merge_conflict_with_head") if it no longer matches
                    -- closes the gap between check_conflicts and this
                    step, e.g. a crash here followed by an external
                    commit landing on the destination, then resume);
                    then writes each copy's changes to the repo. Files in
                    dirty_paths are never touched. Its reconcile checks
                    the DESTINATION's bytes against the frozen plan's
                    hashes, not "do the copies still agree with the
                    destination" -- the latter would wrongly read as
                    "already applied" if every copy were reverted back to
                    base after the plan was frozen.
record              append merge.recorded {paths}, from the frozen plan.

collect_copies and check_conflicts are pure computations with no side
effect on the repo, so their `reconcile` always returns False: a resumed
run after a crash simply re-runs them rather than trusting a journaled
"started" payload (which, for a step crashed before run(), is empty —
that emptiness used to be misread as a completed result, a KeyError
waiting to happen downstream). check_conflicts, apply_allowed_paths and
record do not read each other's journal payload for anything but the
one frozen plan (collect_copies' "done" payload, read only once it is
genuinely done, per ENGINE.md's "자기신고를 믿지 않는다" principle) --
every other fact is recomputed from observable state (the repo, the
copies, the ledger) at the time it is needed.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from engine.ledger import Event
from engine.policy import Policy, _owns_within_prefixes
from engine.processor import StepContext, StepDef, StepFailed
from engine.steps.open_job import copy_root, dirty_paths

# Reserved for the planner role ONLY (engine.plan's own module docstring:
# "a builder station must never touch tests/scenes/") -- enforced here as
# a hard, config-independent floor, never trusted to station_owners alone:
# an operator config that (by mistake or overlap) gives some other role a
# prefix wide enough to also cover this path must still never let that
# role's merge actually land a write under it.
_PLANNER_ONLY_PREFIX = "tests/scenes/"
_PLANNER_ROLE = "planner"


def steps(ctx: StepContext) -> list[StepDef]:
    return [
        StepDef("collect_copies", _collect_copies, _reconcile_false),
        StepDef("check_conflicts", _check_conflicts, _reconcile_false),
        StepDef("apply_allowed_paths", _apply_allowed_paths, _reconcile_apply),
        StepDef("record", _record, _reconcile_record),
    ]


def _reconcile_false(ctx: StepContext) -> bool:
    return False


def _repo(ctx: StepContext) -> Path:
    return ctx.config.projects[ctx.project].repo


def _copies(ctx: StepContext) -> list[dict[str, object]]:
    return list(ctx.request.payload["copies"])


def _copy_path_map(ctx: StepContext) -> dict[str, Path]:
    return {str(i): Path(str(c["path"])) for i, c in enumerate(_copies(ctx))}


def _is_under(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _owner_prefixes(ctx: StepContext, copy: dict[str, object]) -> tuple[str, ...]:
    """The configured station_owners prefixes for this copy's role,
    narrowed to this copy's own `owns` entry when present (engine.
    full_orbit's per-task narrowing -- the same `owns` run_station's own
    gate already enforced while the worker was running, see engine.
    steps.station._task_owns; merge's own re-check of it must never be
    weaker than that, or a task's build could land a change under a
    sibling task's own file via the broader, unnarrowed station prefix).
    `owns`, when given, must itself be a non-empty subset of the
    configured prefixes -- never a way to WIDEN them."""
    station = str(copy.get("station", ""))
    role = str(copy.get("role", ""))
    prefixes = ctx.config.station_owners.get(station, {}).get(role)
    if not prefixes:
        raise StepFailed("unknown_owner")
    prefixes = tuple(prefixes)
    owns = copy.get("owns")
    if owns is None:
        return prefixes
    if not isinstance(owns, list) or not all(isinstance(v, str) for v in owns):
        raise StepFailed("invalid_owns")
    owns_t = tuple(owns)
    if not owns_t or not _owns_within_prefixes(owns_t, prefixes):
        raise StepFailed("owns_escapes_station_prefixes")
    return owns_t


def _validate_copy_path(ctx: StepContext, copy_path: Path, role: str) -> None:
    """The copy's path must be EXACTLY the copy open_job created for this
    role under this attempt -- realpath equality, not merely "somewhere
    under home/copies/{attempt_id}/" (that weaker check let a worker nest
    an arbitrary subdirectory inside ITS OWN copy -- e.g.
    tester_copy/tests/bench/payload/src/app.py -- and submit that nested
    dir claiming a different role/station with broader write access)."""
    expected = Path(os.path.realpath(copy_root(ctx.config, ctx.attempt_id, role)))
    resolved = Path(os.path.realpath(copy_path))
    if resolved != expected:
        raise StepFailed("copy_path_mismatch")


def _deny_if_planner_reserved(ctx: StepContext, role: str, rel: str) -> None:
    """StepFailed("tests_scenes_reserved_for_planner") for any role other
    than the planner touching tests/scenes/ -- journaled first (a denial
    worth seeing, not a silent path_rejected) so it is distinguishable
    from an ordinary out-of-repo/protected-path rejection. See
    _owner_prefixes' own docstring: this is the config-independent floor
    underneath whatever station_owners happens to say."""
    if role == _PLANNER_ROLE:
        return
    normalized = rel.rstrip("/")
    if normalized != _PLANNER_ONLY_PREFIX.rstrip("/") and not rel.startswith(_PLANNER_ONLY_PREFIX):
        return
    ctx.ledger.append(Event(
        project=ctx.project, kind="merge.denied", actor_kind="processor",
        actor_id="processor", attempt_id=ctx.attempt_id,
        payload={"request_id": ctx.request.request_id, "role": role, "path": rel,
                 "reason": "tests_scenes_reserved_for_planner"},
    ))
    raise StepFailed("tests_scenes_reserved_for_planner")


def _validate_rel(ctx: StepContext, repo: Path, policy: Policy, rel: str, role: str) -> None:
    _deny_if_planner_reserved(ctx, role, rel)
    rel_path = Path(rel)
    if rel_path.is_absolute() or ".." in rel_path.parts:
        raise StepFailed("path_rejected")
    target = repo / rel
    real_repo = Path(os.path.realpath(repo))
    real_target = Path(os.path.realpath(target))
    if real_target != real_repo and not _is_under(real_target, real_repo):
        raise StepFailed("path_rejected")
    if policy.is_protected(ctx.project, target):
        raise StepFailed("path_rejected")


# ---- base_head (B-10): copies diff against their own base, not live HEAD -

def _base_head(ctx: StepContext) -> str:
    for _seq, event in ctx.ledger.events(ctx.project, "job.opened"):
        if event.payload.get("attempt_id") == ctx.attempt_id:
            base_head = event.payload.get("base_head")
            if base_head:
                return str(base_head)
            break
    raise StepFailed("no_base_head")


def _ref_bytes(repo: Path, ref: str, rel: str) -> bytes | None:
    """Content of rel at git ref `ref`, or None if it does not exist there."""
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{rel}"],
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _ref_rels_under_prefix(repo: Path, ref: str, prefix: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", ref, "--", prefix],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def _dest_bytes(repo: Path, rel: str) -> bytes | None:
    """Content of rel as it actually stands in the repo's working tree
    right now (the real destination a write would land on), or None."""
    target = repo / rel
    if not target.exists():
        return None
    return target.read_bytes()


def _candidate_files(copy_path: Path, prefixes: tuple[str, ...],
                      excludes: tuple[str, ...]) -> set[str]:
    """Relative (posix) paths under any allowed prefix that the copy's
    OWN `git status` (open_job.dirty_paths, never a raw filesystem walk)
    reports as added/modified/deleted/untracked against its base --
    never the result of walking the copy's directory tree directly:
    a raw filesystem walk under an allowed prefix would also pick up a
    build artifact a worker's own test run leaves behind (e.g.
    `tests/__pycache__/*.pyc`) even where `.gitignore` and the
    project's own `excludes` both already mark it ignored, since git
    itself never considers that path part of any change at all.
    Comparing such an ignored file's bytes against the destination
    repo's own, unrelated copy of the same build artifact would then
    report a false conflict over a file nobody asked to change.
    dirty_paths() already honors the copy's `.gitignore` (git status
    never reports an ignored path, tracked or not) and applies
    `excludes` on top of that, so a path that should never be a merge
    candidate in the first place never becomes one.

    Still raises StepFailed("symlink_in_copy") for any candidate git
    status DOES report that turns out to be a symlink in the copy -- the
    rejection stays, just checked against the paths git itself names
    rather than against every path a raw walk would have found."""
    all_dirty = dirty_paths(copy_path, excludes)
    found: set[str] = set()
    for rel in all_dirty:
        rel_path = Path(rel)
        for prefix in prefixes:
            prefix_path = Path(prefix)
            if rel_path == prefix_path or _is_under(rel_path, prefix_path):
                target = copy_path / rel
                if target.is_symlink():
                    raise StepFailed("symlink_in_copy")
                found.add(rel)
                break
    return found


def _compute_changes(ctx: StepContext) -> dict[str, dict[str, str]]:
    """index -> {rel: "added"|"modified"|"deleted"}, each copy's delta
    against its own base_head (not the project's possibly-advanced live
    HEAD — B-10). Validates every copy and every candidate path before
    returning anything; raises StepFailed on the first violation, before
    any file is written (this function never writes)."""
    repo = _repo(ctx)
    policy = Policy(ctx.config, ctx.ledger)
    base_head = _base_head(ctx)
    excludes = ctx.config.projects[ctx.project].excludes
    changes: dict[str, dict[str, str]] = {}
    for index, copy in enumerate(_copies(ctx)):
        key = str(index)
        role = str(copy.get("role", ""))
        prefixes = _owner_prefixes(ctx, copy)
        copy_path = Path(str(copy["path"]))
        _validate_copy_path(ctx, copy_path, role)
        copy_files = _candidate_files(copy_path, prefixes, excludes)
        role_changes: dict[str, str] = {}
        for rel in copy_files:
            target = copy_path / rel
            if not target.exists():
                # git status reported it (e.g. a deletion) but it is
                # gone from the copy's working tree now -- handled by
                # the base_head walk below instead of read here.
                continue
            _validate_rel(ctx, repo, policy, rel, role)
            base_bytes = _ref_bytes(repo, base_head, rel)
            copy_bytes = target.read_bytes()
            if base_bytes is None:
                role_changes[rel] = "added"
            elif base_bytes != copy_bytes:
                role_changes[rel] = "modified"
        for prefix in prefixes:
            for rel in _ref_rels_under_prefix(repo, base_head, prefix):
                if not (copy_path / rel).exists():
                    _validate_rel(ctx, repo, policy, rel, role)
                    role_changes[rel] = "deleted"
        changes[key] = role_changes
    return changes


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _compute_plan(ctx: StepContext) -> dict[str, dict[str, str | None]]:
    """The frozen plan: for each copy index, for each changed path, the
    sha256 of the copy's current content (None for a deletion). Computing
    this twice from an unmutated copy always yields the same result; a
    mutation to the copy between two computations changes it (B-11's
    detection hook)."""
    changes = _compute_changes(ctx)
    copy_paths = _copy_path_map(ctx)
    plan: dict[str, dict[str, str | None]] = {}
    for key, role_changes in changes.items():
        copy_path = copy_paths[key]
        plan[key] = {
            rel: (None if status == "deleted" else _hash_bytes((copy_path / rel).read_bytes()))
            for rel, status in role_changes.items()
        }
    return plan


# ---- collect_copies -------------------------------------------------------

def _collect_copies(ctx: StepContext) -> dict[str, object]:
    plan = _compute_plan(ctx)
    return {"plan": plan}


def _frozen_plan(ctx: StepContext) -> dict[str, dict[str, str | None]]:
    row = ctx.ledger.steps(ctx.request.request_id).get("collect_copies")
    if row is None or "plan" not in row.payload:
        raise StepFailed("no_frozen_plan")
    return row.payload["plan"]


# ---- shared: destination must still match base for every touched path ---

def _attempt_already_applied_hashes(ctx: StepContext) -> dict[str, str | None]:
    """{rel: sha256 or None} for every relative path an EARLIER
    merge.recorded event on THIS SAME attempt_id already wrote to the
    repo, carrying the hash of the bytes THAT write actually put there
    (None for a deletion) -- never just the bare path, which used to
    be enough by itself to exempt the path from _verify_destinations_
    unchanged entirely. The exemption's old reasoning ("only one
    attempt is ever active, and nothing but merge itself writes under
    these prefixes while it is") does not hold across an owner stop:
    engine.full_orbit's own install/owner-approval pause leaves THIS
    SAME attempt active while the repo sits there waiting, and an
    owner is free to hand-edit a file merge already wrote during that
    window before approving continuation (codex-goal-review-4.md item
    6 / codex-diff-review-s17.md "Repeated merging can overwrite owner
    edits", engine/steps/merge.py:423) -- a later re-merge on the same
    attempt must still catch that, not silently overwrite it just
    because the path's NAME was already applied once before. Later
    events for the same rel overwrite earlier ones (the hash of the
    MOST RECENT write this attempt made, matching what
    _verify_destinations_unchanged actually needs to compare against
    right now)."""
    hashes: dict[str, str | None] = {}
    for _seq, event in ctx.ledger.events(ctx.project, "merge.recorded"):
        if event.attempt_id != ctx.attempt_id:
            continue
        hashes.update(event.payload.get("hashes", {}))
    return hashes


def _verify_destinations_unchanged(ctx: StepContext, rels: set[str]) -> None:
    """For every rel a copy touches (outside dirty_paths): if this same
    attempt's own earlier merge already wrote something to this path
    (_attempt_already_applied_hashes), the destination right NOW must
    equal exactly what THAT write put there -- checked FIRST, and
    checked regardless of what the destination's bytes happen to
    equal right now, including base_head's own content. Only a rel
    this attempt has NEVER applied before falls back to the plain
    "does the destination still equal base_head" comparison.

    The order matters (codex-diff-review-s18.md G6: "merge.py:453
    accepts destination equal to base BEFORE checking the last applied
    digest"): checking "current == base_content" first, and only
    consulting the last-applied hash on a MISMATCH against base, means
    an owner who reverts an already-merged path back to its base_head
    content (undoing this attempt's own earlier write, on purpose)
    makes current == base_content true again -- short-circuiting
    straight past the already-applied check and right into "nothing
    to see here", so apply_allowed_paths silently overwrites that
    deliberate revert with whatever the copy has now. Checking the
    last-applied hash FIRST, for every already-applied rel, catches
    this: current no longer equals the hash THIS attempt actually
    wrote there, so it still fails with merge_conflict_with_owner_edit
    even though current happens to equal base_head's bytes too.

    Either mismatch means something outside this merge touched the
    destination since the relevant reference point: someone else's
    committed change landed there since this copy's base
    (StepFailed("merge_conflict_with_head"), stage 1 has no 3-way
    merge), or the owner edited (or reverted) a path this attempt
    already merged, during an owner stop while this same attempt
    stayed active (StepFailed("merge_conflict_with_owner_edit") --
    never safe to silently overwrite, whether or not the edit happens
    to coincide with base_head's own bytes). A rel this attempt
    applied before but with no recorded hash for it (an older-format
    merge.recorded event, from before hashes were tracked) gets NO
    exemption at all and falls back to the plain base_head comparison
    -- unknown provenance must never be assumed safe. Called both at
    check time and again immediately before apply writes anything, so
    a commit (or an owner's edit/revert) landing on the destination
    during the gap between the two is still caught (#10/#11
    remainder)."""
    repo = _repo(ctx)
    base_head = _base_head(ctx)
    dirty = set(ctx.ledger.project(ctx.project).dirty_paths)
    already_applied = _attempt_already_applied_hashes(ctx)
    for rel in rels:
        if rel in dirty:
            continue
        current = _dest_bytes(repo, rel)
        if rel in already_applied:
            expected_hash = already_applied[rel]
            current_hash = None if current is None else _hash_bytes(current)
            if current_hash == expected_hash:
                continue
            raise StepFailed("merge_conflict_with_owner_edit")
        base_content = _ref_bytes(repo, base_head, rel)
        if current == base_content:
            continue
        raise StepFailed("merge_conflict_with_head")


def _rels_in(changes_or_plan: dict[str, dict[str, object]]) -> set[str]:
    return {rel for role_map in changes_or_plan.values() for rel in role_map}


# ---- check_conflicts -------------------------------------------------------

def _check_conflicts(ctx: StepContext) -> dict[str, object]:
    # #10/#11 remainder: check_conflicts must judge the FROZEN plan, not
    # whatever the mutable copies currently look like -- a copy flipped
    # between collect_copies and check_conflicts (e.g. because
    # check_conflicts itself crashed and is being resumed) must be caught
    # here rather than silently producing a conflict verdict on data that
    # no longer matches what was checked.
    frozen = _frozen_plan(ctx)
    current_plan = _compute_plan(ctx)
    if current_plan != frozen:
        raise StepFailed("copy_changed_after_check")

    changes = _compute_changes(ctx)
    copy_paths = _copy_path_map(ctx)
    seen: dict[str, bytes | None] = {}
    for key, role_changes in changes.items():
        copy_path = copy_paths[key]
        for rel, status in role_changes.items():
            content = None if status == "deleted" else (copy_path / rel).read_bytes()
            if rel in seen:
                if seen[rel] != content:
                    raise StepFailed("merge_conflict")
            else:
                seen[rel] = content
    _verify_destinations_unchanged(ctx, _rels_in(changes))
    return {}


# ---- apply_allowed_paths ---------------------------------------------------

def _applied_list_from_plan(ctx: StepContext, plan: dict[str, dict[str, str | None]]) -> list[str]:
    dirty = set(ctx.ledger.project(ctx.project).dirty_paths)
    applied: list[str] = []
    for role_plan in plan.values():
        for rel in role_plan:
            if rel not in dirty:
                applied.append(rel)
    return applied


def _verify_against_plan(ctx: StepContext, plan: dict[str, dict[str, str | None]]) -> None:
    """B-11: the copies must still match the plan collect_copies froze.
    Anything else (a path added/removed, a hash that no longer matches)
    means a copy was mutated after it was checked -> fail before any
    write, never apply the newer, unchecked bytes."""
    current = _compute_plan(ctx)
    if current != plan:
        raise StepFailed("copy_changed_after_check")


def _apply_allowed_paths(ctx: StepContext) -> dict[str, object]:
    plan = _frozen_plan(ctx)
    _verify_against_plan(ctx, plan)
    # #10/#11 remainder: re-check the destination against base immediately
    # before writing, not only back at check_conflicts time -- a crash
    # before this step, an external commit landing on the destination, and
    # then a resume must still be caught here, not silently overwritten.
    _verify_destinations_unchanged(ctx, _rels_in(plan))
    changes = _compute_changes(ctx)
    repo = _repo(ctx)
    copy_paths = _copy_path_map(ctx)
    dirty = set(ctx.ledger.project(ctx.project).dirty_paths)
    for key, role_changes in changes.items():
        copy_path = copy_paths[key]
        for rel, status in role_changes.items():
            if rel in dirty:
                continue
            target = repo / rel
            if status == "deleted":
                if target.exists():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((copy_path / rel).read_bytes())
    return {}


def _reconcile_apply(ctx: StepContext) -> bool:
    """True only if the DESTINATION already reflects the frozen plan for
    every path -- not "the copies still agree with the destination",
    which would wrongly read as "already applied" if every copy had been
    reverted back to base after the plan was frozen (#10/#11 remainder:
    reconcile must not treat an all-reverted set of copies as done)."""
    plan = _frozen_plan(ctx)
    repo = _repo(ctx)
    dirty = set(ctx.ledger.project(ctx.project).dirty_paths)
    for role_plan in plan.values():
        for rel, expected_hash in role_plan.items():
            if rel in dirty:
                continue
            target = repo / rel
            if expected_hash is None:
                if target.exists():
                    return False
            else:
                if not target.exists():
                    return False
                if _hash_bytes(target.read_bytes()) != expected_hash:
                    return False
    return True


# ---- record --------------------------------------------------------------

def _record(ctx: StepContext) -> dict[str, object]:
    plan = _frozen_plan(ctx)
    applied = _applied_list_from_plan(ctx, plan)
    # The hash of what apply_allowed_paths actually wrote for each
    # applied rel (None for a deletion) -- the frozen plan already
    # carries exactly that, per role; a rel applied by more than one
    # role here always agrees across roles (_check_conflicts' own
    # "seen" dict already enforced that before apply ever ran), so any
    # one role's hash for it is the right one. See
    # _attempt_already_applied_hashes for why later merges on this
    # same attempt need this, not just the bare path list.
    hashes: dict[str, str | None] = {}
    for role_plan in plan.values():
        for rel in applied:
            if rel in role_plan:
                hashes[rel] = role_plan[rel]
    ctx.ledger.append(Event(
        project=ctx.project, kind="merge.recorded", actor_kind="processor",
        actor_id="processor", attempt_id=ctx.attempt_id,
        payload={"request_id": ctx.request.request_id, "paths": applied,
                  "hashes": hashes},
    ))
    return {}


def _reconcile_record(ctx: StepContext) -> bool:
    for _seq, event in ctx.ledger.events(ctx.project, "merge.recorded"):
        if event.payload.get("request_id") == ctx.request.request_id:
            return True
    return False
