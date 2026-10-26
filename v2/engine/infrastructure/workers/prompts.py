"""Prompt text for a builder worker and a reviewer, plus the reviewer's
verdict JSON schema (handed to CodexRunner's --output-schema)."""
from __future__ import annotations

import json
from pathlib import Path

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

_INSTALL_REQUEST_SHAPE = (
    '{"kind": "install", "manifest": "<rel path>", '
    '"change": {"add_lines": ["<name>==<version>", ...]}} -- each line is a plain '
    'requirement specifier (package name, optional ==version); URLs, file paths, '
    'options and markers are rejected'
)

_ARCHITECTURE_RULE_NOTE = (
    "For new code, also flag any violation of "
    f"{_ARCHITECTURE_STANDARD_PATH} (layer import "
    "direction, folder/file naming, file size) as severity P1.\n"
)

_FINAL_JSON_INSTRUCTION = (
    "When you are done, reply with exactly ONE final JSON object, with "
    "no other text after it:\n"
    '{"status": "done" | "blocked" | "failed", "summary": "<short summary>", '
    '"requests": [...]}\n'
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
                  test_cmd: tuple[str, ...] = ()) -> str:
    """A builder's full prompt: the task body plus the hard limits a
    PreToolUse gate (engine.interface.hook_gate) will actually enforce -- the allowed
    write path prefixes and the allowed terminal command patterns -- and
    the exact shape of the final JSON reply, including how to ask the
    owner for something outside those limits (e.g. a dependency change
    on a protected manifest) instead of trying to edit it directly."""
    prefixes_text = ", ".join(allowed_prefixes) if allowed_prefixes else "(none)"
    patterns_text = ", ".join(terminal_patterns) if terminal_patterns else "(none)"
    test_text = (
        f"Run the project's tests with exactly this command: "
        f"{' '.join(test_cmd)}\n" if test_cmd else ""
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
        f'"requests" in your final JSON shaped like: {_INSTALL_REQUEST_SHAPE}\n'
        f"{_FINAL_JSON_INSTRUCTION}"
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
    that looks indistinguishable from one that did."""
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
        f"{scope_text}\n\n"
        f"Diff:\n{diff_text}\n\n"
        f"{_ARCHITECTURE_RULE_NOTE}"
        "Reply with exactly ONE final JSON object, with no other text "
        "after it:\n"
        '{"passed": true or false, "coverage": "full"|"partial", '
        '"findings": [{"severity": "P0"|"P1"|"P2"|"P3", "file": "<path>", '
        '"line": <int>, "note": "<text>"}]}\n'
    )
