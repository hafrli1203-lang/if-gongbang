"""The two boundary lists tests/acceptance/test_t6_crash_matrix.py drives:
every processor step (engine.processor.Faults.crash_before/crash_after,
engine.steps.KINDS) and every named flow-layer boundary
(engine.faults.flow_boundary).

PROCESSOR_STEP_BOUNDARIES is mechanical: one (kind, step) pair per
StepDef in each engine/steps/*.py module, mirroring engine.steps.KINDS.
Re-extract it by hand if a steps() function adds/renames/reorders a
StepDef -- there is no live introspection here because building a
StepContext to call steps(ctx) has side effects (lazy imports, config
reads) this module must not have just to list names.
"""
from __future__ import annotations

from engine.position import FLOW_BOUNDARIES  # noqa: F401 -- re-exported, see below

# kind -> step names, in the order each steps() function returns them.
# Source: engine/steps/close.py, commit.py, install.py, merge.py,
# publish.py, report.py, review.py, bench_run.py, station.py
# (read at commit c74fe93; matches engine.steps.KINDS' keys).
KIND_STEPS: dict[str, tuple[str, ...]] = {
    "close": (
        "set_closing", "archive_tmp", "archive_commit", "reset_to_head",
        "apply_deferred_approvals", "verify_tree_eq_head", "set_closed",
        "open_queue",
    ),
    "abandon": (
        "set_closing", "archive_tmp", "archive_commit", "reset_to_head",
        "apply_deferred_approvals", "verify_tree_eq_head", "set_closed",
        "open_queue",
    ),
    "commit": (
        "compute_fingerprint", "verify_three", "git_commit", "record_commit",
        "report",
    ),
    "publish": (
        "check_approval", "check_artifact",
        # "advance_master" is inserted here only when the project has a
        # `release` config (engine/steps/publish.py:571) -- listed
        # separately below (PUBLISH_STEPS_WITH_ADVANCE_MASTER) since it
        # is not part of every publish request's step list.
        "record_source", "call_publisher", "record_publish", "report",
    ),
    "install": (
        "acquire_project_lock", "snapshot_manifest", "invalidate_evidence",
        "run_installer", "verify_manifest", "record",
    ),
    "scripts_change": (
        "acquire_project_lock", "snapshot_manifest", "invalidate_evidence",
        "run_installer", "verify_manifest", "record",
    ),
    "test_config_change": (
        "acquire_project_lock", "snapshot_manifest", "invalidate_evidence",
        "run_installer", "verify_manifest", "record",
    ),
    "merge": (
        "collect_copies", "check_conflicts", "apply_allowed_paths", "record",
    ),
    "report": (
        "write_report",
    ),
    "bench": (
        "compute_fingerprint", "run_tests", "trust_checks", "record_bench",
    ),
    "run_station": (
        "spawn_worker", "await_worker", "record_station",
    ),
    "review": (
        "compute_fingerprint", "spawn_reviewer", "await_reviewer", "record_review",
    ),
}

# publish's step list with a `release`-configured project's extra step
# (engine/steps/publish.py:571), inserted right after check_artifact.
PUBLISH_STEPS_WITH_ADVANCE_MASTER: tuple[str, ...] = (
    "check_approval", "check_artifact", "advance_master",
    "record_source", "call_publisher", "record_publish", "report",
)

CRASH_EDGES: tuple[str, ...] = ("crash_before", "crash_after")

# kinds whose steps() function is the IDENTICAL object as another kind's
# already in this list (engine/steps/__init__.py KINDS: "abandon" ->
# close.steps, "scripts_change"/"test_config_change" -> install.steps) --
# skipped so each real step list is only listed once, under its first
# kind name. This is NOT a step-NAME dedup: "merge" and "install" each
# have their own unrelated step literally named "record" (two different
# functions), and both must be listed, one under each kind -- arming
# engine.faults.Faults.crash_before/after by step name alone would hit
# BOTH of them at once if a test armed "record", which is exactly why
# test_t6_crash_matrix.py's own driver only ever arms one at a time
# against a job script that only ever reaches one of the two kinds that
# share that literal name in any single run.
_ALIAS_KINDS: frozenset[str] = frozenset({"abandon", "scripts_change", "test_config_change"})


def processor_step_boundaries() -> tuple[tuple[str, str, str], ...]:
    """(kind, step, edge) for every StepDef in KIND_STEPS, both edges,
    one entry per (kind, step) pair -- see _ALIAS_KINDS for the only
    kinds skipped (their own step list is a different kind's, verbatim,
    not merely same-named steps)."""
    out: list[tuple[str, str, str]] = []
    for kind, steps in KIND_STEPS.items():
        if kind in _ALIAS_KINDS:
            continue
        for step in steps:
            for edge in CRASH_EDGES:
                out.append((kind, step, edge))
    for edge in CRASH_EDGES:
        out.append(("publish", "advance_master", edge))
    return tuple(out)


PROCESSOR_STEP_BOUNDARIES: tuple[tuple[str, str, str], ...] = processor_step_boundaries()


# ---- flow-layer boundaries ----------------------------------------------
#
# engine.position.FLOW_BOUNDARIES has landed and is re-exported at this
# module's top (see above) -- every test in test_t6_crash_matrix.py/
# test_t7_config_matrix.py reads boundaries.FLOW_BOUNDARIES, one name,
# same as it reads boundaries.PROCESSOR_STEP_BOUNDARIES for the
# processor-step list; engine.position.FLOW_BOUNDARIES lists every
# engine.faults.flow_boundary name the flow layer passes right before a
# ledger write, so the harness drives exactly that list.
