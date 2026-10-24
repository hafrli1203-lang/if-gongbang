"""Deterministic scripted worker runner: no real AI, used to exercise the
run_station/review machinery (process spawn/timeout/gate wiring) without
network calls or non-determinism."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from engine.config import WorkerConfig
from engine.workers.base import (
    WorkerResult, WorkerSpec, parse_cli_shaped_result, parse_cli_shaped_verdict,
    worker_env,
)


class ScriptedRunner:
    # A scripted worker is not a real CLI with its own credential state --
    # it needs no writable HOME subpath at all.
    home_writable_subpaths: tuple[str, ...] = ()

    def __init__(self, script_path: Path,
                 gate_argv: tuple[str, ...] | None = None,
                 read_only: bool = False) -> None:
        self.script_path = script_path
        self.gate_argv = gate_argv
        self.read_only = read_only

    @property
    def needs_gate(self) -> bool:
        return self.gate_argv is not None

    def argv(self, spec: WorkerSpec, worker: WorkerConfig) -> list[str]:
        # -P: same reasoning as ClaudeRunner's gate hook command -- this
        # process's cwd is the worker copy, which must never be able to
        # shadow `engine.fakes.scripted_worker` (or, via gate_argv below,
        # `engine.gate`) with a copy-planted module of its own. Any
        # gate_argv a caller wires in for real policy enforcement should
        # itself start with the same `sys.executable, "-P"` pair.
        argv = [sys.executable, "-P", "-m", "engine.fakes.scripted_worker",
                str(self.script_path)]
        if self.gate_argv is not None:
            argv += ["--gate", json.dumps(list(self.gate_argv))]
        return argv

    def env(self, spec: WorkerSpec, worker: WorkerConfig) -> dict[str, str]:
        return worker_env(spec)

    def parse_result(self, stdout_path: Path, spec: WorkerSpec) -> WorkerResult:
        # A reviewer's final JSON IS the verdict ({"passed", "findings"});
        # a builder's final JSON is {"status", "summary", "requests"}.
        # station discriminates which shape this run produced.
        if spec.station == "review":
            return parse_cli_shaped_verdict(stdout_path)
        return parse_cli_shaped_result(stdout_path)
