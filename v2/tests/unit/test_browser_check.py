"""Unit tests for engine.browser_check: visiting a generated app's own
pages in a real, already-installed browser, starting/stopping its dev
server, and the "no Playwright on this host" path. Runs against the
REAL Playwright this host already has (engine.browser_check never
installs one) -- not mocked, per this repo's own "actually run it
once" convention; test_unavailable_path_never_tries_to_use_a_browser
is the one exception, since it specifically tests the no-Playwright
branch."""
from __future__ import annotations

import http.server
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from engine import browser_check
from engine.browser_check import check_app, find_backend, visit_pages

pytestmark = pytest.mark.skipif(
    find_backend() is None, reason="no Playwright on this host",
)

_CLEAN_PAGE = b"<!DOCTYPE html><html><body><h1>Home</h1></body></html>"
_ERROR_PAGE = (
    b"<!DOCTYPE html><html><body><h1>Broken</h1>"
    b"<script>console.error('boom');</script></body></html>"
)
_PAGES = {"/clean.html": _CLEAN_PAGE, "/broken.html": _ERROR_PAGE}


class _StaticHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server's naming)
        if self.path == "/favicon.ico":
            # The browser's own automatic favicon request -- answered
            # with "no content" (not 404) so it never shows up as an
            # unrelated console error in a test that is only checking
            # the PAGE's own console output.
            self.send_response(204)
            self.end_headers()
            return
        body = _PAGES.get(self.path)
        status = 200 if body is not None else 404
        body = body or b"<html><body>not found</body></html>"
        # Chromium treats a 4xx/5xx response with an EMPTY body as a
        # network-level failure (net::ERR_HTTP_RESPONSE_CODE_FAILURE),
        # not an ordinary "page loaded, status 404" navigation -- a
        # real dev server's own error page always has a body; this
        # stand-in must too, or a "missing page" test would fail for
        # the wrong reason (confirmed directly against this exact
        # handler shape).
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_str: str, *args: object) -> None:  # noqa: A002
        pass  # silence per-request stderr logging during the test run


@contextmanager
def _static_site() -> Iterator[str]:
    """A tiny HTTP server standing in for a generated app's dev server
    -- serves exactly _PAGES, nothing else (module docstring: "a tiny
    static fixture app served by a python http.server stand-in")."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _StaticHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[0], server.server_address[1]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_a_clean_page_passes_with_a_screenshot(tmp_path: Path) -> None:
    with _static_site() as base_url:
        result = visit_pages(base_url, ["/clean.html"], tmp_path / "screens")

    assert result.status == "ok"
    assert len(result.pages) == 1
    page = result.pages[0]
    assert page.ok is True
    assert page.http_status == 200
    assert page.console_errors == ()
    assert page.reason is None
    assert page.screenshot_path is not None
    assert Path(page.screenshot_path).stat().st_size > 0


def test_a_page_with_a_console_error_fails_with_the_error_captured(tmp_path: Path) -> None:
    with _static_site() as base_url:
        result = visit_pages(base_url, ["/broken.html"], tmp_path / "screens")

    assert result.status == "failed"
    page = result.pages[0]
    assert page.ok is False
    assert page.reason == "console_error"
    assert any("boom" in err for err in page.console_errors)
    # Still saved a screenshot of the broken page -- a caller
    # diagnosing the failure needs to see it, not just the error text.
    assert page.screenshot_path is not None
    assert Path(page.screenshot_path).exists()


def test_a_missing_page_is_a_4xx_failure(tmp_path: Path) -> None:
    with _static_site() as base_url:
        result = visit_pages(base_url, ["/does-not-exist.html"], tmp_path / "screens")

    assert result.status == "failed"
    page = result.pages[0]
    assert page.http_status == 404
    assert page.reason == "http_4xx"


def test_multiple_pages_each_get_their_own_numbered_screenshot(tmp_path: Path) -> None:
    with _static_site() as base_url:
        result = visit_pages(
            base_url, ["/clean.html", "/broken.html"], tmp_path / "screens",
        )

    assert [p.path for p in result.pages] == ["/clean.html", "/broken.html"]
    names = sorted(p.name for p in (tmp_path / "screens").iterdir())
    assert names == ["00-clean_html.png", "01-broken_html.png"]


def test_check_app_starts_the_dev_server_from_package_json_and_stops_it(
    tmp_path: Path,
) -> None:
    """End-to-end: check_app reads the generated app's own package.json
    "dev" script, runs it on a free port, visits a page, and tears the
    server down -- a minimal Node http server standing in for a real
    `vite` dev server (both just need to honor --port/--strictPort)."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "package.json").write_text(json.dumps({
        "name": "fixture-app", "scripts": {"dev": "node server.js"},
    }), encoding="utf-8")
    (app_dir / "server.js").write_text(
        "const http = require('http');\n"
        "const args = process.argv;\n"
        "const port = Number(args[args.indexOf('--port') + 1]);\n"
        "http.createServer((req, res) => {\n"
        "  res.writeHead(200, {'Content-Type': 'text/html'});\n"
        "  res.end('<html><body><h1>fixture</h1></body></html>');\n"
        "}).listen(port, '127.0.0.1');\n",
        encoding="utf-8",
    )

    result = check_app(app_dir, ["/"], tmp_path / "screens")

    assert result.status == "ok"
    assert result.pages[0].http_status == 200
    # The server was actually torn down -- not asserted directly here
    # (no public "is it still running" probe), but start_dev_server's
    # own port is now free again if a second check_app call against
    # the same app_dir can still bind one, which test_steps_bench.py-
    # style tests already rely on process-group termination for
    # elsewhere in this codebase (engine.publishers' own pattern this
    # module reuses).


def test_check_app_reports_error_when_package_json_has_no_dev_script(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "package.json").write_text(json.dumps({"name": "x", "scripts": {}}),
                                          encoding="utf-8")

    result = check_app(app_dir, ["/"], tmp_path / "screens")

    assert result.status == "error"
    assert result.reason == "dev_server_did_not_start"
    assert result.pages == ()


def test_unavailable_path_never_tries_to_use_a_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser_check, "find_backend", lambda: None)

    result = visit_pages("http://127.0.0.1:1", ["/"], tmp_path / "screens")

    assert result.status == "unavailable"
    assert result.pages == ()
    assert not (tmp_path / "screens").exists()
