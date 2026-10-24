"""Prompt text for a builder worker and a reviewer, plus the reviewer's
verdict JSON schema (handed to CodexRunner's --output-schema)."""
from __future__ import annotations

import json
from pathlib import Path

from engine.workers.base import WorkerSpec

_INSTALL_REQUEST_SHAPE = (
    '{"kind": "install", "manifest": "<rel path>", '
    '"change": {"add_lines": ["<name>==<version>", ...]}} -- each line is a plain '
    'requirement specifier (package name, optional ==version); URLs, file paths, '
    'options and markers are rejected'
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
    "required": ["passed", "findings"],
    "additionalProperties": False,
}


def write_verdict_schema(path: Path) -> None:
    path.write_text(json.dumps(VERDICT_SCHEMA), encoding="utf-8")


def build_prompt(spec: WorkerSpec, allowed_prefixes: tuple[str, ...],
                  terminal_patterns: tuple[str, ...],
                  test_cmd: tuple[str, ...] = ()) -> str:
    """A builder's full prompt: the task body plus the hard limits a
    PreToolUse gate (engine.gate) will actually enforce -- the allowed
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


def review_prompt(spec: WorkerSpec, diff_text: str) -> str:
    """A reviewer's full prompt: the original task plus the diff to
    review, and the verdict JSON shape to reply with."""
    return (
        f"Review the following change against this task:\n{spec.task}\n\n"
        f"Diff:\n{diff_text}\n\n"
        "Reply with exactly ONE final JSON object, with no other text "
        "after it:\n"
        '{"passed": true or false, "findings": [{"severity": '
        '"P0"|"P1"|"P2"|"P3", "file": "<path>", "line": <int>, '
        '"note": "<text>"}]}\n'
    )
