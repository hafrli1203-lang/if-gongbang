"""A probe passes only when the engine itself saw it reach its target.

run_probe_scripts runs each probe script behind the engine's own
recording proxy; the script's own "PROBE OK <url>" output is no longer
enough. Targets here are local fake servers (tests/probe_target.py),
never real external services."""
from __future__ import annotations

import socket
import sys
import urllib.request
from dataclasses import replace
from pathlib import Path

from engine import sandbox
from engine.acceptance import run_probe_scripts
from engine.domain.evidence_graph import _probe_ok
from engine.domain.probe_observation import target_host, target_reached
from engine.infrastructure.probe_proxy import RecordingProxy
from engine.steps.open_job import open_job
from tests.conftest import Workspace
from tests.fixtures.sample_app import make_sample
from tests.probe_target import contacting_py_script, serve_target
from tests.security_gate import bwrap_skipif

_needs_bwrap = bwrap_skipif(sandbox.available(), reason="bwrap not available on this host")


def _sample_workspace(workspace: Workspace) -> Workspace:
    sample = make_sample(workspace.root / "sample", 1, Path(sys.executable))
    project_config = replace(workspace.config.projects["alpha"],
                             repo=sample.repo, test_cmd=sample.test_cmd)
    new_config = replace(workspace.config,
                         projects={**workspace.config.projects, "alpha": project_config})
    return Workspace(workspace.root, new_config, workspace.ledger, sample.repo, workspace.beta)


def _open(ws: Workspace) -> str:
    return open_job(ws.config, ws.ledger, "alpha", "job1", "full", roles=("builder",))


def _probe_dir(ws: Workspace) -> Path:
    path = ws.alpha / ".gongbang" / "job1" / "probe"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _events(ws: Workspace) -> list[dict[str, object]]:
    return [e.payload for _seq, e in ws.ledger.events("alpha", "probe.executed")]


def test_probe_that_really_reaches_its_target_passes(workspace: Workspace) -> None:
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    with serve_target() as url:
        (_probe_dir(ws) / "t1.py").write_text(contacting_py_script(url), encoding="utf-8")
        results = run_probe_scripts(ws.config, ws.ledger, "alpha", attempt_id, "job1",
                                    [("fake api", url)])

    assert results[0].ok is True, results[0].stdout_tail
    assert results[0].reached is True
    assert results[0].started_at and results[0].finished_at
    payload = _events(ws)[0]
    assert payload["reached"] is True
    assert payload["expected_host"] == "127.0.0.1"
    assert payload["started_at"] and payload["finished_at"]
    contacts = payload["contacts"]
    assert isinstance(contacts, list) and contacts
    assert contacts[0]["host"] == "127.0.0.1"
    assert contacts[0]["bytes_down"] > 0
    assert len(str(contacts[0]["response_sha256"])) == 64
    assert len(str(payload["sha256"])) == 64


def test_probe_that_only_prints_probe_ok_fails(workspace: Workspace) -> None:
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    with serve_target() as url:
        (_probe_dir(ws) / "t1.py").write_text(f"print('PROBE OK {url}')\n", encoding="utf-8")
        results = run_probe_scripts(ws.config, ws.ledger, "alpha", attempt_id, "job1",
                                    [("fake api", url)])

    assert results[0].returncode == 0
    assert f"PROBE OK {url}" in results[0].stdout_tail
    assert results[0].ok is False
    assert results[0].reached is False
    assert _events(ws)[0]["contacts"] == []


def test_probe_reaching_a_different_host_fails(workspace: Workspace) -> None:
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    with serve_target() as other_url:
        target = "http://target.invalid/docs"
        script = (contacting_py_script(other_url, print_ok=False)
                  + f"print('PROBE OK {target}')\n")
        (_probe_dir(ws) / "t1.py").write_text(script, encoding="utf-8")
        results = run_probe_scripts(ws.config, ws.ledger, "alpha", attempt_id, "job1",
                                    [("target", target)])

    assert results[0].returncode == 0
    assert results[0].ok is False


def test_only_one_of_two_targets_reached_fails_the_other(workspace: Workspace) -> None:
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    with serve_target("/a") as url_a, serve_target("/b") as url_b:
        probe_dir = _probe_dir(ws)
        (probe_dir / "t1.py").write_text(contacting_py_script(url_a), encoding="utf-8")
        # t2 contacts nothing in its own run and only prints the line;
        # t1's contact belongs to t1's run and never counts for t2.
        (probe_dir / "t2.py").write_text(f"print('PROBE OK {url_b}')\n", encoding="utf-8")
        results = run_probe_scripts(ws.config, ws.ledger, "alpha", attempt_id, "job1",
                                    [("a", url_a), ("b", url_b)])

    assert [r.ok for r in results] == [True, False]
    failed = [r.name for r in results if not r.ok]
    assert failed == ["b"]


def test_target_without_an_address_cannot_pass(workspace: Workspace) -> None:
    ws = _sample_workspace(workspace)
    attempt_id = _open(ws)
    (_probe_dir(ws) / "t1.sh").write_text("echo 'PROBE OK pdftotext'\n", encoding="utf-8")
    results = run_probe_scripts(ws.config, ws.ledger, "alpha", attempt_id, "job1",
                                [("pdf", "pdftotext")])
    assert results[0].returncode == 0
    assert results[0].ok is False


def test_target_host_parsing() -> None:
    assert target_host("https://Docs.Example.com/api") == "docs.example.com"
    assert target_host("http://127.0.0.1:3216/x") == "127.0.0.1"
    assert target_host("코드 `src/a.ts`, 기본 주소 `http://127.0.0.1:3216`") == "127.0.0.1"
    assert target_host("(사내)") is None
    assert target_host("pdftotext") is None


def test_target_reached_needs_connection_and_response_bytes() -> None:
    url = "https://api.example.com/v1"
    good = {"host": "api.example.com", "port": 443, "method": "CONNECT",
            "connected": True, "bytes_up": 10, "bytes_down": 20}
    assert target_reached(url, [good]) is True
    assert target_reached(url, [{**good, "connected": False}]) is False
    assert target_reached(url, [{**good, "bytes_down": 0}]) is False
    assert target_reached(url, [{**good, "host": "other.example.com"}]) is False
    assert target_reached(url, []) is False


def test_evidence_graph_probe_ok_requires_recorded_contact() -> None:
    url = "https://api.example.com/v1"
    contact = {"host": "api.example.com", "port": 443, "connected": True, "bytes_down": 5}
    tail = f"PROBE OK {url}\n"
    assert _probe_ok(0, tail, url, [contact]) is True
    assert _probe_ok(0, tail, url, []) is False
    assert _probe_ok(0, tail, url, None) is False
    assert _probe_ok(1, tail, url, [contact]) is False


def test_recording_proxy_relays_http_and_records_connect() -> None:
    with serve_target() as url, RecordingProxy() as proxy:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy.url}))
        body = opener.open(url, timeout=10).read()
        port = int(url.split(":")[2].split("/")[0])
        with socket.create_connection(("127.0.0.1", int(proxy.url.rsplit(":", 1)[1]))) as raw:
            raw.sendall(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode())
            assert raw.recv(64).startswith(b"HTTP/1.1 200")
            raw.sendall(b"GET /x HTTP/1.0\r\n\r\n")
            assert raw.recv(64).startswith(b"HTTP/1.0 200")
    assert body == b'{"status": "ok"}'
    methods = sorted(str(c["method"]) for c in proxy.contacts())
    assert methods == ["CONNECT", "GET"]
    assert all(c["connected"] is True and int(str(c["bytes_down"])) > 0
               for c in proxy.contacts())


@_needs_bwrap
def test_probe_reaches_target_through_proxy_inside_bwrap(workspace: Workspace) -> None:
    ws = _sample_workspace(workspace)
    ws = Workspace(ws.root, replace(ws.config, sandbox="bwrap"), ws.ledger, ws.alpha, ws.beta)
    attempt_id = _open(ws)
    with serve_target() as url:
        (_probe_dir(ws) / "t1.py").write_text(contacting_py_script(url), encoding="utf-8")
        (_probe_dir(ws) / "t2.py").write_text(f"print('PROBE OK {url}')\n", encoding="utf-8")
        results = run_probe_scripts(ws.config, ws.ledger, "alpha", attempt_id, "job1",
                                    [("fake api", url), ("same api", url)])
    assert [r.ok for r in results] == [True, False], [r.stdout_tail for r in results]


def test_contact_on_another_port_of_the_same_host_does_not_count() -> None:
    from engine.domain.probe_observation import target_reached
    other_port = [{"host": "127.0.0.1", "port": 49152, "connected": True, "bytes_down": 10}]
    right_port = [{"host": "127.0.0.1", "port": 8443, "connected": True, "bytes_down": 10}]
    assert target_reached("https://127.0.0.1:8443/api", other_port) is False
    assert target_reached("https://127.0.0.1:8443/api", right_port) is True
    assert target_reached("https://api.example.com/v1", [{"host": "api.example.com", "port": 443, "connected": True, "bytes_down": 1}]) is True
