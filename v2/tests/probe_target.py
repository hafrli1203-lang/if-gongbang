"""A local stand-in for a probe target's real address.

Probe scripts in tests must actually reach their target through the
engine's recording proxy to pass, but tests never call real external
services: `serve_target()` starts a tiny HTTP server on 127.0.0.1 and
yields its URL, which a test uses as the target's facts.md address.
"""
from __future__ import annotations

import http.server
import threading
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager

import pytest


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib hook name
        body = b'{"status": "ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


@contextmanager
def serve_target(path: str = "/docs/api") -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}{path}"
    finally:
        server.shutdown()
        server.server_close()


def contacting_py_script(url: str, *, print_ok: bool = True) -> str:
    """A probe script body that fetches `url` through whatever proxy the
    environment names (urllib honors HTTP_PROXY) and then prints the
    legacy `PROBE OK <url>` line."""
    lines = [
        "import urllib.request",
        f"urllib.request.urlopen({url!r}, timeout=10).read()",
    ]
    if print_ok:
        lines.append(f"print('PROBE OK {url}')")
    return "\n".join(lines) + "\n"


@pytest.fixture
def probe_target() -> Iterator[Callable[[str], str]]:
    """Factory fixture: probe_target("/path") starts one local target
    server and returns its URL; every server stops at teardown."""
    with ExitStack() as stack:
        yield lambda path="/docs/api": stack.enter_context(serve_target(path))
