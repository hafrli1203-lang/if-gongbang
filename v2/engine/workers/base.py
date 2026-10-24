"""WorkerSpec/WorkerResult/WorkerRunner contracts shared by every runner
(claude_cli, codex_cli, scripted), plus the small pieces every runner's
argv()/env() needs: the GB_* identity env vars a spawned worker's
engine.gate hook reads, an env-passthrough allowlist (so a worker never
inherits the operator's own shell environment) that comes from
EngineConfig.worker_env_passthrough via WorkerSpec.env_passthrough (never
widened per-project or per-station), and a helper to wait for a spawned
pid to exit without assuming this process is always still its parent (a
crash-and-resume can land in a different process).

HOME is in the default passthrough because the `claude`/`codex` CLI
needs it to find its own credentials -- it is not optional for either
real runner. What keeps a worker from reading the operator's actual
home directory through that HOME value is NOT this allowlist: it is
engine.policy's worker-read-bounded-to-copy_root rule (rule 1) and the
terminal "~"/shell-metachar denials (rule 5b) -- a worker process may
have HOME set to the operator's real home, but every tool call it makes
still goes through engine.gate -> Policy, which refuses to read or
execute anything outside the worker's own copy or allowed terminal
patterns regardless of what HOME points at.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from engine.config import McpServerConfig, WorkerConfig
from engine.procs import exit_file_for, is_alive

# Matches EngineConfig.worker_env_passthrough's default exactly. Runners
# use this fixed list rather than threading EngineConfig through the
# WorkerRunner.env(spec, worker) protocol (which only gets WorkerSpec and
# WorkerConfig) -- a worker's env is deliberately the same tiny allowlist
# for every project, not something a project or station can widen.
DEFAULT_ENV_PASSTHROUGH: tuple[str, ...] = ("PATH", "HOME", "LANG", "TERM")

# The engine package is not pip-installed; `python -m engine.X` only
# resolves when the project root is on sys.path. A worker runs with cwd
# set to its own copy (or the repo, for a read-only reviewer), never
# this project's root, so PYTHONPATH must be set explicitly -- computed
# here (parents[2]: workers/ -> engine/ -> project root), never taken
# from this process's own PYTHONPATH (which could point anywhere).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class WorkerSpec:
    project: str
    attempt_id: str
    generation: int
    station: str
    role: str
    copy_root: Path  # worker's own working copy (= repo for a read-only reviewer)
    workdir: Path    # where the worker process actually runs
    task: str        # fully built prompt (engine.workers.prompts.build_prompt/review_prompt)
    result_path: Path  # file the runner is expected to leave its result in
    config_path: Path | None  # for GB_CONFIG (None: runner needs no gate)
    env_passthrough: tuple[str, ...] = DEFAULT_ENV_PASSTHROUGH
    # ProjectFacts snapshot (station._write_gate_facts) and the plain
    # decision-log file (station._init_gate_log) the gate hook reads/
    # appends to instead of ever opening the ledger itself -- None for
    # a runner that needs no gate (e.g. the reviewer's codex, read_only).
    gate_facts_path: Path | None = None
    gate_log_path: Path | None = None
    # A gate-ro copy of config_path (station._gate_config_copy_path),
    # read by GB_CONFIG instead of config_path itself when set -- the
    # gate hook then always reads its config from the one directory the
    # sandbox mounts read-only and remounts immutable (see
    # engine.steps.station._gate_ro_dir), regardless of where the
    # original config file sits on the host. None for a runner that
    # needs no gate.
    gate_config_path: Path | None = None
    # name -> launch info for the mcp servers THIS request is allowed
    # (engine.plan.tools_for_station's "mcp" list for this station,
    # intersected with EngineConfig.station_mcp_allow by whoever builds
    # this spec -- engine.steps.station). Empty (default): no MCP
    # servers for this worker -- ClaudeRunner then passes no
    # --mcp-config at all, byte-identical to before this field existed.
    # The gate hook enforces the SAME allowed names independently via
    # gate_facts.json's own "mcp_allow" (see engine.gate/engine.policy)
    # -- this field only controls what the CLI is launched *able* to
    # call; the gate is what actually blocks an unlisted one.
    mcp_servers: dict[str, McpServerConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkerResult:
    ok: bool
    summary: str
    cost_usd: float | None
    turns: int | None
    requests: tuple[dict[str, object], ...]
    verdict: dict[str, object] | None


class WorkerRunner(Protocol):
    needs_gate: bool  # True: tool calls go through engine.gate (needs config_path)
    read_only: bool   # True: cannot modify its workdir (required for `review`)
    # Relative paths under $HOME this runner's own CLI needs to write
    # (e.g. ClaudeRunner's ".claude" dir and ".claude.json" file,
    # CodexRunner's ".codex" dir) -- engine.sandbox's station/review call
    # sites bind $HOME read-only and re-expose only these subpaths
    # writable, never the whole HOME. Empty for a runner whose CLI needs
    # no writable HOME state (ScriptedRunner).
    home_writable_subpaths: tuple[str, ...]

    def argv(self, spec: WorkerSpec, worker: WorkerConfig) -> list[str]: ...

    def env(self, spec: WorkerSpec, worker: WorkerConfig) -> dict[str, str]: ...

    def parse_result(self, stdout_path: Path, spec: WorkerSpec) -> WorkerResult: ...


def gate_env(spec: WorkerSpec) -> dict[str, str]:
    """The GB_* identity vars engine.gate (python -m engine.gate, run as a
    Claude PreToolUse hook) reads from its environment to reconstruct the
    calling Actor and look up the EngineConfig -- GB_GATE_FACTS/
    GB_GATE_LOG name the ProjectFacts snapshot and decision-log files
    the gate reads/appends to instead of ever opening the ledger
    itself (ENGINE.md 「한계」: the worker sandbox no longer has the
    ledger directory writable)."""
    config_for_gate = spec.gate_config_path if spec.gate_config_path is not None else spec.config_path
    return {
        "GB_CONFIG": str(config_for_gate) if config_for_gate is not None else "",
        "GB_PROJECT": spec.project,
        "GB_ATTEMPT": spec.attempt_id,
        "GB_GENERATION": str(spec.generation),
        "GB_STATION": spec.station,
        "GB_ROLE": spec.role,
        "GB_COPY_ROOT": str(spec.copy_root),
        "GB_GATE_FACTS": str(spec.gate_facts_path) if spec.gate_facts_path is not None else "",
        "GB_GATE_LOG": str(spec.gate_log_path) if spec.gate_log_path is not None else "",
    }


def passthrough_env(names: tuple[str, ...]) -> dict[str, str]:
    """Only the named vars, taken from this process's own environment --
    anything else (an operator's secrets, ANTHROPIC_* overrides, ...)
    never reaches a spawned worker."""
    return {name: os.environ[name] for name in names if name in os.environ}


def worker_env(spec: WorkerSpec) -> dict[str, str]:
    """The env every runner uses: spec.env_passthrough (set by the step
    that built this WorkerSpec from config.worker_env_passthrough) plus
    this worker's GB_* identity vars plus PYTHONPATH (see _PROJECT_ROOT
    above) -- the `claude`/`codex`/scripted-worker child process needs
    the latter to invoke `python -m engine.gate` or
    `python -m engine.fakes.scripted_worker` from a cwd outside this
    project."""
    return {
        **passthrough_env(spec.env_passthrough),
        **gate_env(spec),
        "PYTHONPATH": str(_PROJECT_ROOT),
    }


def last_json_block(text: str) -> dict[str, object] | None:
    """The rightmost top-level {...} object in `text`.

    Brace-counting over the raw characters breaks the moment a brace
    appears inside a JSON string (e.g. a summary like "fixed the } in
    the parser" or a "requests" entry that happens to be a nested
    object), since a "}"/"{" inside quotes -- or one that belongs to a
    nested object -- is indistinguishable from a top-level one by
    character alone. Instead, every "{" position in the text is handed
    to json.JSONDecoder.raw_decode, which understands string literals
    (never stops at a brace inside quotes) and reports exactly where
    the value it parsed ends; of every candidate that parses as a dict,
    the one whose parsed span extends furthest into the text (the
    largest `end`) is the top-level, rightmost one -- any object nested
    inside it (e.g. one entry of its own "requests" array) necessarily
    ends earlier, before that enclosing object's own closing brace, so
    it never wins the comparison. Used to pull the worker's final
    status JSON out of a CLI's last free-text message (Claude's
    `result`, or a scripted worker's own stdout "result" field, which
    happens to already be exactly that JSON encoded as a string)."""
    decoder = json.JSONDecoder()
    best: tuple[int, dict[str, object]] | None = None
    pos = -1
    while True:
        pos = text.find("{", pos + 1)
        if pos == -1:
            return best[1] if best is not None else None
        try:
            parsed, end = decoder.raw_decode(text, pos)
        except ValueError:  # JSONDecodeError or e.g. an oversized int literal
            continue
        if isinstance(parsed, dict) and (best is None or end > best[0]):
            best = (end, parsed)


def parse_cli_shaped_result(stdout_path: Path) -> WorkerResult:
    """Shared parse_result for ClaudeRunner and ScriptedRunner: both leave
    a JSON object on stdout with "result" (free text whose tail is the
    worker's own final JSON), "total_cost_usd", "num_turns" and
    "is_error"."""
    try:
        raw = json.loads(stdout_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return WorkerResult(False, "unparseable_result", None, None, (), None)
    if not isinstance(raw, dict):
        return WorkerResult(False, "unparseable_result", None, None, (), None)
    cost = raw.get("total_cost_usd")
    turns = raw.get("num_turns")
    if not (cost is None or isinstance(cost, (int, float))):
        cost = None
    if not (turns is None or isinstance(turns, int)):
        turns = None
    block = last_json_block(str(raw.get("result", "")))
    if block is None:
        return WorkerResult(False, "unparseable_result", cost, turns, (), None)
    summary = str(block.get("summary", ""))
    requests_raw = block.get("requests", [])
    requests = tuple(r for r in requests_raw if isinstance(r, dict)) \
        if isinstance(requests_raw, list) else ()
    status = block.get("status")
    # "blocked" is only acceptable together with at least one request the
    # owner can act on; a bare "blocked" is a failure like any other.
    finished = status == "done" or (status == "blocked" and bool(requests))
    ok = finished and not raw.get("is_error", False)
    return WorkerResult(
        ok=ok, summary=summary, cost_usd=cost, turns=turns,
        requests=requests, verdict=None,
    )


def parse_cli_shaped_verdict(stdout_path: Path) -> WorkerResult:
    """Like parse_cli_shaped_result, but for a REVIEWER: the worker's
    final JSON block IS the verdict ({"passed", "findings"}) rather than
    a builder's {"status", "summary", "requests"}. Used by ScriptedRunner
    for a "review" station (see station discrimination in
    ScriptedRunner.parse_result) -- CodexRunner gets its verdict from the
    --output-schema result file instead, never from stdout."""
    try:
        raw = json.loads(stdout_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return WorkerResult(False, "unparseable_result", None, None, (), None)
    if not isinstance(raw, dict):
        return WorkerResult(False, "unparseable_result", None, None, (), None)
    cost = raw.get("total_cost_usd")
    turns = raw.get("num_turns")
    block = last_json_block(str(raw.get("result", "")))
    if block is None:
        return WorkerResult(False, "unparseable_result", cost, turns, (), None)
    return WorkerResult(
        ok=True, summary="scripted_review", cost_usd=cost, turns=turns,
        requests=(), verdict=block,
    )


def _progress_mark(paths: "tuple[Path, ...]") -> tuple[tuple[int, int], ...]:
    marks = []
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            marks.append((-1, -1))
            continue
        marks.append((st.st_size, st.st_mtime_ns))
    return tuple(marks)


def wait_while_progressing(pid: int, stall_sec: float,
                           progress_paths: "tuple[Path, ...]",
                           exit_file: Path | None = None) -> int | None:
    """Wait for `pid` to exit for as long as it keeps making progress.
    Progress = any of `progress_paths` (the worker's stdout, its gate log
    of tool calls, ...) changed size or mtime. There is no total time
    limit: a worker that keeps working is never cut off. Returns the exit
    code, or None only when nothing changed for `stall_sec` in a row
    (the worker is stuck, e.g. waiting on input it will never get).

    `exit_file` defaults to engine.procs.exit_file_for(progress_paths[0])
    when not given explicitly (None if progress_paths is empty) --
    every call site lists the worker's stdout path first in
    progress_paths (the same path it gave engine.procs.spawn as
    stdout_path), so this recovers the identical, now engine-only exit
    record exit_file_for's own docstring describes with no change
    needed at any existing call site. A caller may still pass its own
    exit_file explicitly (e.g. one derived some other way, or paired
    with a spawn() call that was given an explicit exit_file instead
    of relying on stdout_path at all)."""
    if exit_file is None and progress_paths:
        exit_file = exit_file_for(progress_paths[0])
    last_mark = _progress_mark(progress_paths)
    last_change = time.time()
    while True:
        code = wait_for_exit(pid, 1.0, exit_file)
        if code is not None:
            return code
        mark = _progress_mark(progress_paths)
        if mark != last_mark:
            last_mark = mark
            last_change = time.time()
        elif time.time() - last_change >= stall_sec:
            return None


_UNKNOWN_EXIT = -1  # distinct from every real exit code (0-255); callers
                    # already treat -1 (WIFEXITED false, a signalled
                    # process) as abnormal/failed, so this reuses that
                    # existing meaning rather than inventing a new one.


def _read_exit_file(exit_file: Path | None) -> int | None:
    if exit_file is None:
        return None
    try:
        text = exit_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def wait_for_exit(pid: int, timeout_sec: float,
                  exit_file: Path | None = None) -> int | None:
    """Poll until `pid` exits or `timeout_sec` elapses. Returns the exit
    code, or None on timeout. Reaps via waitpid(WNOHANG) when this
    process is still pid's parent (the normal case: it spawned the
    worker itself, or it is resuming into an already-reaped same-process
    attempt -- see below). If waitpid raises ChildProcessError -- no
    live child entry for `pid` at all, either because this process was
    never its parent (resumed in a different OS process after a crash)
    or because an earlier call already reaped it -- and the process is
    confirmed gone (not is_alive), the real exit code is read from
    `exit_file` (engine.procs.spawn's own wrapper writes it there,
    refusing to follow a symlink or clobber an existing file -- see
    engine.exit_writer). Trusting a bare "process is gone" as
    success, the way this used to work, silently turned a crashed or
    killed worker into a reported success; `exit_file` is None's/
    missing/unreadable fallback is `_UNKNOWN_EXIT` (-1), which every
    existing caller already treats as a failure, never 0."""
    deadline = time.time() + timeout_sec
    while True:
        try:
            reaped_pid, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            if is_alive(pid):
                reaped_pid, status = 0, 0
            else:
                code = _read_exit_file(exit_file)
                return code if code is not None else _UNKNOWN_EXIT
        if reaped_pid == pid:
            return os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
        if time.time() >= deadline:
            return None
        time.sleep(0.05)
