"""Codex CLI reviewer runner. Read-only sandbox (`-s read-only`): the
reviewer only ever looks at the repo, never writes to it, so it needs no
PreToolUse gate of its own."""
from __future__ import annotations

import json
from pathlib import Path

from engine.config import WorkerConfig
from engine.infrastructure.workers.base import WorkerResult, WorkerSpec, worker_env
from engine.infrastructure.workers.prompts import write_verdict_schema

_DEFAULT_REASONING = "medium"


def _schema_path(result_path: Path) -> Path:
    return result_path.with_suffix(".schema.json")


class CodexRunner:
    needs_gate = False  # codex's own -s read-only sandbox, no hook gate
    read_only = True
    # The real `codex` CLI writes its own session/credential state under
    # this -- the only HOME subpath this runner needs writable.
    home_writable_subpaths = (".codex",)
    # Same reasoning as ClaudeRunner.prompt_via_stdin: the prompt (here,
    # a review prompt that embeds a whole diff, chunked up to a 240k-char
    # budget -- see review.py's own module docstring) can exceed Linux's
    # MAX_ARG_STRLEN as a single argv element. `codex exec ... -` (a bare
    # "-" in the prompt's own argv position) reads the prompt from stdin
    # instead.
    prompt_via_stdin = True

    def argv(self, spec: WorkerSpec, worker: WorkerConfig) -> list[str]:
        schema_path = _schema_path(spec.result_path)
        # spec.result_path's parent (e.g. a request's stations/ dir) may
        # not exist yet on a fresh attempt -- write_verdict_schema would
        # raise FileNotFoundError writing next to it otherwise.
        schema_path.parent.mkdir(parents=True, exist_ok=True)
        write_verdict_schema(schema_path)
        reasoning = worker.reasoning or _DEFAULT_REASONING
        return [
            "codex", "exec", "-s", "read-only", "--skip-git-repo-check",
            "-C", str(spec.workdir),
            "-c", f'model_reasoning_effort="{reasoning}"',
            "--output-schema", str(schema_path),
            "-o", str(spec.result_path),
            "-",
        ]

    def env(self, spec: WorkerSpec, worker: WorkerConfig) -> dict[str, str]:
        return worker_env(spec)

    def parse_result(self, stdout_path: Path, spec: WorkerSpec) -> WorkerResult:
        # The reviewer's verdict is the file codex wrote via -o, not its
        # stdout (stdout_path is still where the runner's combined
        # stdout+stderr went, kept only for debugging/logs).
        try:
            raw = json.loads(spec.result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return WorkerResult(False, "unparseable_result", None, None, (), None)
        if not isinstance(raw, dict):
            return WorkerResult(False, "unparseable_result", None, None, (), None)
        return WorkerResult(
            ok=True, summary="codex_review", cost_usd=None, turns=None,
            requests=(), verdict=raw,
        )
