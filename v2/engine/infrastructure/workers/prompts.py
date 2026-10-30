"""Prompt text for a builder worker and a reviewer, plus the reviewer's
verdict JSON schema (handed to CodexRunner's --output-schema)."""
from __future__ import annotations

import json
from pathlib import Path

from engine.domain.install_request_shape import (
    NON_INSTALL_PENDING_KINDS,
    install_request_shape_text,
)
from engine.domain.plan_model import SCENES_PREFIX
from engine.infrastructure.workers.base import WorkerSpec

# Same computation engine.application.steps.worker_step.ENGINE_ROOT uses -- never
# imported from that module directly: engine.application.steps.worker_step already
# imports engine.infrastructure.workers.prompts, so importing back from here would be
# circular. A worker's sandbox mounts ENGINE_ROOT read-only but hides
# the rest of the apps root this engine's own checkout lives under; a
# path outside ENGINE_ROOT in a prompt would point at a file no worker
# (including the reviewer, which reads this prompt too) can open.
_ENGINE_ROOT = Path(__file__).resolve().parents[3]
_ARCHITECTURE_STANDARD_PATH = _ENGINE_ROOT / "docs" / "standards" / "architecture.md"

_ARCHITECTURE_RULE_NOTE = (
    "For new code, also flag any violation of "
    f"{_ARCHITECTURE_STANDARD_PATH} (layer import "
    "direction, folder/file naming, file size) as severity P1.\n"
)

_FINAL_JSON_INSTRUCTION = (
    "When you are done, reply with exactly ONE final JSON object, with "
    "no other text after it:\n"
    '{"status": "done" | "blocked" | "failed", "summary": "<short summary>", '
    '"skills_applied": [{"name": "<exact name from a \\"## 이 칸의 스킬\\" '
    'heading above>", "where": "<what you used it for>"}], '
    '"requests": [...]}\n'
    'List every skill under "## 이 칸의 스킬" you actually used in '
    '"skills_applied", with where/how; leave out one you did not use. '
    'An empty array means none of them were used.\n'
    'Use "blocked" only when you cannot finish until the owner fulfils an '
    'entry in "requests" (e.g. a dependency install); "failed" means the '
    'task cannot be done at all.\n'
)

VERDICT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "coverage": {"type": "string", "enum": ["full", "partial"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string"},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "note": {"type": "string"},
                },
                "required": ["severity", "file", "line", "note"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["passed", "coverage", "findings"],
    "additionalProperties": False,
}


def write_verdict_schema(path: Path) -> None:
    path.write_text(json.dumps(VERDICT_SCHEMA), encoding="utf-8")


def build_prompt(spec: WorkerSpec, allowed_prefixes: tuple[str, ...],
                  terminal_patterns: tuple[str, ...],
                  test_cmd: tuple[str, ...] = (),
                  manifest: str | None = None) -> str:
    """A builder's full prompt: the task body plus the hard limits a
    PreToolUse gate (engine.interface.hook_gate) will actually enforce -- the allowed
    write path prefixes and the allowed terminal command patterns -- and
    the exact shape of the final JSON reply, including how to ask the
    owner for something outside those limits (e.g. a dependency change
    on a protected manifest) instead of trying to edit it directly.

    `manifest` (the project's configured manifest path, e.g.
    ctx.config.projects[project].manifest) decides WHICH install-request
    shape the instruction below describes -- a JSON manifest's
    dependency-section shape or a line-list manifest's add_lines shape
    (engine.domain.install_request_shape.install_request_shape_text,
    shared with the matching owner_approval.validate_pending check: a
    request built exactly as instructed here is guaranteed to pass that
    check, by construction -- 결함 58, where this always described
    add_lines even for a .json manifest, whose gate only ever accepts
    a dependency section and rejected every such request).

    결함 65 (same root as 결함 58): this used to describe ONLY the
    "install" kind, even though engine.application.owner_approval.
    PENDING_KIND_WHITELIST also accepts "scripts_change"/
    "test_config_change" for a manifest edit that is not a dependency
    bump (e.g. a package.json "scripts" entry) -- a worker with no
    instructed shape for that filed it as "install" instead, which
    validate_pending rejects by construction (an "install" kind's
    `change` may only touch a dependency section). The instruction now
    also names "scripts_change"/"test_config_change" and their shape
    (install_request_shape_text(manifest, kind=...) for each of
    NON_INSTALL_PENDING_KINDS), still from the SAME shared module as
    the "install" shape, so a worker's choice of kind stays correct by
    construction too."""
    prefixes_text = ", ".join(allowed_prefixes) if allowed_prefixes else "(none)"
    patterns_text = ", ".join(terminal_patterns) if terminal_patterns else "(none)"
    test_text = (
        f"Run the project's tests with exactly this command: "
        f"{' '.join(test_cmd)}\n" if test_cmd else ""
    )
    install_request_shape = install_request_shape_text(manifest)
    non_install_shapes = "; or ".join(
        f'kind="{kind}" (for a manifest edit that is NOT a package '
        f"version bump, e.g. a build/start/test script): "
        f"{install_request_shape_text(manifest, kind=kind)}"
        for kind in NON_INSTALL_PENDING_KINDS
    )
    return (
        f"{spec.task}\n\n"
        f"{test_text}"
        f"You may only write files under these paths (relative to your "
        f"working copy): {prefixes_text}.\n"
        f"You may only run terminal commands matching these patterns: "
        f"{patterns_text}.\n"
        "If the task needs something outside those limits (for example, "
        "a new dependency added to a manifest file you cannot write), do "
        "not try to edit that file yourself. Instead add an entry to "
        f'"requests" in your final JSON shaped like: {install_request_shape}\n'
        f"For any OTHER manifest edit you cannot write yourself, use "
        f"{non_install_shapes}\n"
        f"{_FINAL_JSON_INSTRUCTION}"
    )


def _is_plan_review_diff(changed_files: list[str]) -> bool:
    """True iff every changed file lives under either a job's own
    `.gongbang/<job>/` trail (plan.json, facts.md) or `SCENES_PREFIX`
    (tests/scenes/*, the plan station's OWN write prefix alongside
    .gongbang/ -- see EngineConfig.station_owners' "plan" entry and
    engine.domain.plan_model's own module docstring: "the planner may
    write them"). That is the exact shape of the plan-review stage's
    diff (engine.application.quick_pipeline's own comment at "Review
    the merged-but-uncommitted plan.json+facts.md+scenes change"):
    this review runs BEFORE that trail is even committed, let alone
    before any build task has written a single line of src/.
    Narrowing the first cut of this check to ONLY ".gongbang/" missed
    exactly this -- a real plan-review diff whose one changed file was
    a tests/scenes/ script (sample-small4's own re-run after the first
    fix, R-cb8b57faa538f671.result.json: still a false P1 because this
    function said "no" to a plan review that in fact had no .gongbang/
    file in it at all, only a scene script). An empty changed_files
    list is never a plan review -- there is nothing a reviewer here,
    plan or build, could judge (the caller's own coverage/empty-diff
    handling, not this function's job).

    Each entry here is one RAW `git diff --name-status` line (see
    engine.application.steps.review._changed_files), e.g.
    "A\\t.gongbang/<job>/plan.json" or, for a rename,
    "R100\\told/path\\tnew/path" -- never a bare path. A third cut of
    this check (sample-small4's SECOND re-run via `gb2 continue`, same
    symptom again: R-e4b17f2ee77e134e.result.json) compared THAT raw
    line's prefix against ".gongbang/"/SCENES_PREFIX and always
    failed, since every real line starts with a status code and a
    tab, not the path. The actual path is the text after the LAST tab
    (a plain "A\\t<path>" line has exactly one; a rename's
    "R100\\told\\tnew" has two -- `git diff --name-status` always
    tab-separates this way, never a path containing a literal tab
    itself)."""
    if not changed_files:
        return False
    paths = (line.rsplit("\t", 1)[-1] for line in changed_files)
    return all(p.startswith(".gongbang/") or p.startswith(SCENES_PREFIX) for p in paths)


_PLAN_REVIEW_NOTE = (
    "This is a PLAN review, not a code review: every changed file "
    "listed above lives under a job's own .gongbang/<job>/ trail "
    "(plan.json, facts.md, scenes/*). No build task has run yet, so "
    "the original request's implementation has NOT been written and "
    "is correctly absent from this diff -- do NOT report a finding "
    "that the diff is missing the requested implementation. Judge "
    "instead whether plan.json's tasks and scenes, if carried out as "
    "written, would satisfy the original request (and whether facts.md "
    "supports the values the plan uses).\n\n"
)


def review_prompt(spec: WorkerSpec, original_task: str, changed_files: list[str],
                   diff_text: str, coverage_sent: str) -> str:
    """A reviewer's full prompt: the ORIGINAL request text the builder
    was given (so the reviewer judges the change against what was
    actually asked for, not only against the diff on its own), the
    list of changed files, the diff itself (or as much of it as fits
    the sent budget), and the verdict JSON shape to reply with.

    `coverage_sent` ("full" or "partial") describes how much of the
    diff `diff_text` actually contains -- "partial" means the diff was
    too large to send in full and only the changed-file list plus the
    first part of it is included here. The reviewer is told this
    explicitly and REQUIRED to echo it back as "coverage" in its own
    verdict JSON (never silently assumed by the caller), so a reviewer
    that did not actually see the whole change cannot report a verdict
    that looks indistinguishable from one that did.

    When `changed_files` is entirely a job's .gongbang/<job>/ trail
    (_is_plan_review_diff), this is the plan-review stage
    (engine.application.quick_pipeline.run_small_job /
    engine.application.pipeline_planning's own plan-review Stage), called
    BEFORE any build task runs -- 결함 53: without `_PLAN_REVIEW_NOTE`,
    a reviewer sees the original request's real feature text next to a
    diff that (correctly, at this stage) contains no src/ change at
    all, and reports a P1 "requested implementation missing from diff"
    (see tests/unit/test_worker_prompts.py's own regression test,
    reproducing R-0109d7e9e000cf30.result.json from the sample-small3
    probe run). A code-review diff (anything outside .gongbang/) never
    gets this note -- it must keep judging the actual implementation,
    same as always."""
    plan_review_note = _PLAN_REVIEW_NOTE if _is_plan_review_diff(changed_files) else ""
    files_text = "\n".join(f"- {f}" for f in changed_files) or "(no changed files)"
    if coverage_sent == "full":
        scope_text = (
            f"Review scope: FULL diff, all {len(changed_files)} changed "
            f"file(s) shown below. Reply with \"coverage\": \"full\"."
        )
    else:
        scope_text = (
            "Review scope: PARTIAL -- the full diff was too large to send "
            "in one prompt. Only the changed-file list above and the "
            "first part of the diff are shown below; you have NOT seen "
            "the whole change. Reply with \"coverage\": \"partial\" -- "
            "do not report \"full\"."
        )
    return (
        f"Original request:\n{original_task}\n\n"
        f"Changed files:\n{files_text}\n\n"
        f"{plan_review_note}"
        f"{scope_text}\n\n"
        f"Diff:\n{diff_text}\n\n"
        f"{_ARCHITECTURE_RULE_NOTE}"
        "Reply with exactly ONE final JSON object, with no other text "
        "after it:\n"
        '{"passed": true or false, "coverage": "full"|"partial", '
        '"findings": [{"severity": "P0"|"P1"|"P2"|"P3", "file": "<path>", '
        '"line": <int>, "note": "<text>"}]}\n'
    )
