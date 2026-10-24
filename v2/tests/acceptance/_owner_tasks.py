"""Shared helpers for acceptance tests 13-15 (plan Task 5 §8): owner task
#1 wording fix, #2 reproducible bug, #3 dependency + external call, each
run end to end through engine.orbit against the sample "greeter" app.

Not a test module itself (no test_* functions; pytest does not collect
it, but guard against accidental collection anyway by the leading
underscore in the filename, matching this repo's own convention for
non-test support files under tests/).
"""
from __future__ import annotations

import http.server
import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from tests.conftest import Workspace
from tests.fixtures.sample_app import make_sample


def sample_workspace(workspace: Workspace, variant: int) -> Workspace:
    """Repoint 'alpha' at a fresh sample app repo (plan Task 5 §8),
    mirroring tests/unit/test_steps_bench.py's _bench_workspace."""
    sample = make_sample(workspace.root / "sample", variant, Path(sys.executable))
    project_config = replace(
        workspace.config.projects["alpha"],
        repo=sample.repo, test_cmd=sample.test_cmd,
        install_cmd=sample.install_cmd, manifest=sample.manifest,
        bench_cases=sample.bench_cases,
    )
    new_config = replace(
        workspace.config,
        projects={**workspace.config.projects, "alpha": project_config},
    )
    return Workspace(workspace.root, new_config, workspace.ledger,
                     sample.repo, workspace.beta)


def with_status_url(config, url: str):
    """Return a config whose 'alpha' project's test_cmd is prefixed with
    SAMPLE_STATUS_URL=<url> (make_sample's test_cmd already starts with
    "env", so the new assignment is simply inserted right after it --
    `env` accepts any number of VAR=value pairs before the command)."""
    project = config.projects["alpha"]
    assert project.test_cmd and project.test_cmd[0] == "env", project.test_cmd
    new_cmd = (project.test_cmd[0], f"SAMPLE_STATUS_URL={url}", *project.test_cmd[1:])
    new_project = replace(project, test_cmd=new_cmd)
    return replace(config, projects={**config.projects, "alpha": new_project})


def with_workers(config, workers: dict[str, object]):
    """dataclasses.replace(config, workers=workers) -- a tiny named
    wrapper so every call site reads the same way; raises TypeError
    before Task 2 merges EngineConfig.workers (there is nothing to add
    the field to yet), which is exactly the failure the scripted/live
    tests calling this are expected to hit until then."""
    from engine.config import write_json
    # Gated workers (claude) need GB_CONFIG -> the config must exist as a
    # JSON file; materialize it next to the ledger and load it back so
    # config_path is set. Scripted runs take the same path on purpose.
    return write_json(replace(config, workers=workers),
                      config.ledger_path.parent / "engine.json")


def station_config(ws: Workspace):
    """Base config for all three owner tasks: builder may write src/ and
    tests/; requirements.txt is protected (an install request is the only
    way to change it); the builder's allowed terminal commands cover
    running the sample app's own test suite."""
    return replace(
        ws.config,
        station_owners={**ws.config.station_owners,
                        "build": {"builder": ("src/", "tests/")}},
        protected_paths=(*ws.config.protected_paths, "requirements.txt"),
        station_terminal_allow={"build": {"builder": (
            r"\S*python3? -m pytest( -q)?( tests(/\S+)?)?",
            # the sample app's own test_cmd is env-prefixed
            # (`env PYTHONPATH=src:.deps <python> -m pytest -q`,
            # tests/fixtures/sample_app.py's make_sample) -- a builder
            # that re-runs the suite the same way it will actually be
            # run must match this form too, not just the plain one.
            r"env PYTHONPATH=\S+ \S*python3? -m pytest( -q)?( tests(/\S+)?)?",
            r"ls( -la?)?( \S+)?", r"cat \S+",
        )}},
    )


def write_script(tmp_path: Path, name: str, actions: list[dict[str, object]]) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(actions), encoding="utf-8")
    return path


def show_head(repo: Path, rel: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"HEAD:{rel}"],
        check=True, capture_output=True, text=True,
    ).stdout


class _StatusHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server's naming)
        if self.path == "/status":
            self.server.hits += 1  # type: ignore[attr-defined]
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format_str: str, *args: object) -> None:  # noqa: A002
        pass  # silence per-request stderr logging during the test run


@contextmanager
def status_stub() -> Iterator[http.server.HTTPServer]:
    """A tiny HTTP server on an OS-assigned port answering GET /status
    with {"ok": true}, standing in for the "external API" owner task #3
    calls. server.hits counts how many requests it served."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _StatusHandler)
    server.hits = 0  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def status_url(server: http.server.HTTPServer) -> str:
    host, port = server.server_address[0], server.server_address[1]
    return f"http://{host}:{port}/status"


def engine_repo_root() -> Path:
    """This engine repo's own root (not the sample app repo) --
    tests/acceptance/_owner_tasks.py -> tests -> repo root."""
    return Path(__file__).resolve().parents[2]


def record_live_run(repo_root: Path, test_name: str, outcome, cost_usd: float,
                    turns: int, duration_sec: float) -> None:
    out_dir = repo_root / "docs" / "stage2-runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{test_name}.json").write_text(json.dumps({
        "test": test_name, "outcome": outcome.status, "cost_usd": cost_usd,
        "turns": turns, "duration_sec": duration_sec,
        "attempt_id": outcome.attempt_id,
    }, indent=2), encoding="utf-8")


def station_events_cost_turns(ledger, project: str) -> tuple[float, int]:
    """Total cost_usd/turns across every station.finished event for this
    project -- used by the live tests to report what a run actually
    cost, summed rather than taken from a single event since a job may
    run more than one station over its lifetime."""
    cost = 0.0
    turns = 0
    for _seq, event in ledger.events(project, "station.finished"):
        cost += float(event.payload.get("cost_usd") or 0.0)
        turns += int(event.payload.get("turns") or 0)
    return cost, turns
