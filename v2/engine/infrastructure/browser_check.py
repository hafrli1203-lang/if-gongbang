"""browser_check -- visit a generated app's own pages in a real browser
and report what actually happened: a page/console error, or a 4xx/5xx
navigation response. New, standalone module (plan: C2) -- not wired
into engine.application.acceptance yet; see the module docstring's own "wiring"
note at the bottom for the call site this is meant for.

Two independent halves, each testable on its own:
  start_dev_server / stop_dev_server   run the generated app's own
      package.json "dev" script on a free port, wait for it to answer
      HTTP, and kill the whole process group afterward.
  visit_pages                          drive a real, already-installed
      Playwright (Python bindings if importable, else a Node driver
      script run against a `playwright` package already present on
      this host -- see find_backend) to open each page, capture
      console errors/page errors/the navigation response status, and
      save one screenshot per page.
check_app composes both for the common case (start the app's own dev
server, visit pages, stop it) -- a caller that already has a server
running elsewhere (e.g. this module's own tests, against a plain
`python -m http.server` stand-in) can call visit_pages directly.

Never installs anything (no `npm install`, no `playwright install`):
availability is a yes/no fact about what the host already has, exactly
per the plan's own instruction. A host with neither a Python
`playwright` package nor a Node one on npx's own package cache gets
status "unavailable", not a crash and not a silent skip that LOOKS
like a pass.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

__all__ = [
    "PageResult", "BrowserCheckResult", "find_backend", "visit_pages",
    "start_dev_server", "stop_dev_server", "check_app",
]

_DEFAULT_NAV_TIMEOUT_SEC = 15.0
_DEFAULT_STARTUP_TIMEOUT_SEC = 20.0
_TERM_GRACE_SEC = 2.0
_KILL_GRACE_SEC = 2.0


@dataclass(frozen=True)
class PageResult:
    """One visited page's own outcome. `reason` is None iff `ok` --
    "console_error" | "page_error" | "http_4xx" | "http_5xx" |
    "navigation_failed" otherwise. `console_errors`/`page_errors` are
    always populated regardless of `ok` (a page can have a console
    warning-shaped error that this module still counts as fine --
    kept for a caller/report to see, never silently dropped)."""
    path: str
    ok: bool
    http_status: int | None
    console_errors: tuple[str, ...]
    page_errors: tuple[str, ...]
    screenshot_path: str | None
    reason: str | None = None


@dataclass(frozen=True)
class BrowserCheckResult:
    """status: "ok" (every page ok) | "failed" (at least one page
    not ok) | "unavailable" (no usable Playwright on this host) |
    "error" (the dev server never came up, or the browser itself
    failed to launch -- `pages` is always () for these two)."""
    status: str
    pages: tuple[PageResult, ...]
    reason: str | None = None


# ---- availability -------------------------------------------------

@dataclass(frozen=True)
class _Backend:
    kind: str  # "python" | "node"
    node_modules: Path | None = None  # only for kind == "node"


def _python_playwright_available() -> bool:
    return importlib.util.find_spec("playwright") is not None


_NPX_CACHE_GLOBS = (
    "_npx/*/node_modules/playwright",
)


def _find_node_playwright() -> Path | None:
    """The `node_modules` directory of an already-fetched `playwright`
    npm package -- checked via npx's own on-disk cache (~/.npm/_npx/*)
    rather than re-running `npx playwright --version` (which can, on a
    host with no cached copy and no network, hang or fail outright --
    a plain directory glob never has that risk and never touches the
    network). Returns the FIRST candidate whose package.json actually
    parses, so a half-written/corrupted cache entry is skipped rather
    than trusted. None if nothing is found -- never raises."""
    home = Path.home()
    npm_cache = Path(os.environ.get("npm_config_cache", str(home / ".npm")))
    for pattern in _NPX_CACHE_GLOBS:
        for candidate in sorted(npm_cache.glob(pattern)):
            package_json = candidate / "package.json"
            try:
                json.loads(package_json.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            return candidate.parent
    return None


def find_backend() -> _Backend | None:
    """Which already-installed Playwright this host can actually
    drive a browser with -- Python bindings preferred (no subprocess/
    JSON marshaling needed) over the Node driver. None means
    "unavailable": neither is present, and this module never installs
    either."""
    if _python_playwright_available():
        return _Backend(kind="python")
    node_modules = _find_node_playwright()
    if node_modules is not None and shutil.which("node") is not None:
        return _Backend(kind="node", node_modules=node_modules)
    return None


# ---- dev server -----------------------------------------------------

@dataclass(frozen=True)
class DevServer:
    process: "subprocess.Popen[str]"
    port: int
    base_url: str


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_http(url: str, timeout_sec: float) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1.0)  # noqa: S310 - localhost only
            return True
        except urllib.error.HTTPError:
            return True  # server answered -- any status code counts as "up"
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    return False


def start_dev_server(
    app_dir: Path, *, script: str = "dev",
    startup_timeout_sec: float = _DEFAULT_STARTUP_TIMEOUT_SEC,
) -> DevServer | None:
    """Runs `npm run <script> -- --port <free port> --strictPort` in
    `app_dir` (the generated app's own package.json; tools/templates'
    own "dev" script is `vite`, which understands both flags) in its
    own process group, and waits for it to answer HTTP before
    returning. None (the process, if started, is already killed) if
    package.json has no such script, npm itself cannot be found, the
    process exits early, or it never answers within
    startup_timeout_sec -- never raises."""
    package_json = app_dir / "package.json"
    try:
        manifest = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict) or script not in (manifest.get("scripts") or {}):
        return None
    if shutil.which("npm") is None:
        return None
    port = _free_port()
    try:
        process = subprocess.Popen(
            ["npm", "run", script, "--", "--port", str(port), "--strictPort"],
            cwd=str(app_dir), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return None
    base_url = f"http://127.0.0.1:{port}"
    if process.poll() is not None or not _wait_for_http(base_url, startup_timeout_sec):
        _terminate_process_group(process)
        return None
    return DevServer(process=process, port=port, base_url=base_url)


def _pgid_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True  # fail safe: not confirmed gone


def _terminate_process_group(process: "subprocess.Popen[str]") -> bool:
    """Same TERM-then-KILL-with-reap pattern engine.infrastructure.publishers uses
    for its own deploy subprocess -- start_new_session=True puts the
    dev server in its own group, so killing it never reaches this
    module's own process."""
    pgid = process.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_TERM_GRACE_SEC)
    if not _pgid_alive(pgid):
        return True
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_KILL_GRACE_SEC)
    return not _pgid_alive(pgid)


def stop_dev_server(server: DevServer) -> bool:
    return _terminate_process_group(server.process)


# ---- visiting pages --------------------------------------------------

def _page_result(path: str, http_status: int | None,
                 console_errors: tuple[str, ...], page_errors: tuple[str, ...],
                 screenshot_path: str | None, *, navigation_failed: bool) -> PageResult:
    """Classifies one page's outcome -- the SAME rule for both
    backends, so a caller never sees a different pass/fail definition
    depending on which Playwright drove the browser."""
    if navigation_failed:
        reason: str | None = "navigation_failed"
    elif http_status is not None and 500 <= http_status:
        reason = "http_5xx"
    elif http_status is not None and 400 <= http_status < 500:
        reason = "http_4xx"
    elif page_errors:
        reason = "page_error"
    elif console_errors:
        reason = "console_error"
    else:
        reason = None
    return PageResult(
        path=path, ok=reason is None, http_status=http_status,
        console_errors=console_errors, page_errors=page_errors,
        screenshot_path=screenshot_path, reason=reason,
    )


def _visit_pages_python(base_url: str, pages: Sequence[str], screens_dir: Path,
                        nav_timeout_sec: float) -> BrowserCheckResult:
    from playwright.sync_api import Error as PlaywrightError  # type: ignore[import-not-found]
    from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]

    screens_dir.mkdir(parents=True, exist_ok=True)
    results: list[PageResult] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                for i, path in enumerate(pages):
                    results.append(_visit_one_page_python(
                        browser, base_url, path, i, screens_dir, nav_timeout_sec))
            finally:
                browser.close()
    except PlaywrightError as exc:
        return BrowserCheckResult(status="error", pages=(), reason=str(exc)[:500])
    status = "ok" if all(p.ok for p in results) else "failed"
    return BrowserCheckResult(status=status, pages=tuple(results))


def _visit_one_page_python(browser, base_url: str, path: str, index: int,
                           screens_dir: Path, nav_timeout_sec: float) -> PageResult:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page = browser.new_page()
    page.on("console", lambda msg: msg.type == "error" and console_errors.append(msg.text))
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    screenshot_path = screens_dir / f"{index:02d}-{_safe_name(path)}.png"
    http_status: int | None = None
    navigation_failed = False
    try:
        response = page.goto(base_url + path, timeout=nav_timeout_sec * 1000)
        http_status = response.status if response is not None else None
        page.screenshot(path=str(screenshot_path))
    except Exception:  # noqa: BLE001 - any navigation failure, classified uniformly
        navigation_failed = True
    finally:
        page.close()
    return _page_result(
        path, http_status, tuple(console_errors), tuple(page_errors),
        str(screenshot_path) if screenshot_path.exists() else None,
        navigation_failed=navigation_failed,
    )


def _safe_name(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", path).strip("_") or "root"


def visit_pages(
    base_url: str, pages: Sequence[str], screens_dir: Path, *,
    nav_timeout_sec: float = _DEFAULT_NAV_TIMEOUT_SEC,
) -> BrowserCheckResult:
    """Visits each of `pages` (each a path like "/" or "/about",
    joined onto `base_url`) in a real, headless, already-installed
    Chromium, one screenshot per page saved under `screens_dir`
    (<two-digit index>-<sanitized path>.png). status "unavailable" iff
    find_backend() finds neither a Python nor a Node Playwright on
    this host -- never installs one."""
    backend = find_backend()
    if backend is None:
        return BrowserCheckResult(status="unavailable", pages=(),
                                  reason="no playwright found on this host")
    if backend.kind == "python":
        return _visit_pages_python(base_url, pages, screens_dir, nav_timeout_sec)
    assert backend.node_modules is not None
    return _visit_pages_node(base_url, pages, screens_dir, backend.node_modules,
                             nav_timeout_sec)


def check_app(
    app_dir: Path, pages: Sequence[str], screens_dir: Path, *,
    script: str = "dev", startup_timeout_sec: float = _DEFAULT_STARTUP_TIMEOUT_SEC,
    nav_timeout_sec: float = _DEFAULT_NAV_TIMEOUT_SEC,
) -> BrowserCheckResult:
    """start_dev_server(app_dir) -> visit_pages(...) -> stop_dev_server,
    always (even if visit_pages raises or the dev server never came
    up -- "error"/"dev_server_did_not_start" either way, never a
    leaked server process)."""
    backend_check = find_backend()
    if backend_check is None:
        return BrowserCheckResult(status="unavailable", pages=(),
                                  reason="no playwright found on this host")
    server = start_dev_server(app_dir, script=script, startup_timeout_sec=startup_timeout_sec)
    if server is None:
        return BrowserCheckResult(status="error", pages=(),
                                  reason="dev_server_did_not_start")
    try:
        return visit_pages(server.base_url, pages, screens_dir,
                           nav_timeout_sec=nav_timeout_sec)
    finally:
        stop_dev_server(server)


# ---- Node backend ----------------------------------------------------

_NODE_DRIVER_SCRIPT = r"""
const fs = require('fs');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright');

function findChromiumExecutable() {
  const cacheDir = process.env.PLAYWRIGHT_BROWSERS_PATH
    || path.join(os.homedir(), '.cache', 'ms-playwright');
  let entries = [];
  try { entries = fs.readdirSync(cacheDir); } catch (e) { return []; }
  const candidates = [];
  for (const name of entries) {
    const m = name.match(/^chromium-(\d+)$/);
    if (!m) continue;
    for (const rel of ['chrome-linux64/chrome', 'chrome-linux/chrome']) {
      const full = path.join(cacheDir, name, rel);
      if (fs.existsSync(full)) candidates.push({ rev: parseInt(m[1], 10), path: full });
    }
  }
  candidates.sort((a, b) => b.rev - a.rev);
  return candidates.map((c) => c.path);
}

async function launchChromium() {
  try {
    return await chromium.launch({ headless: true });
  } catch (e) {
    for (const executablePath of findChromiumExecutable()) {
      try {
        return await chromium.launch({ headless: true, executablePath });
      } catch (e2) { /* try the next candidate */ }
    }
    throw e;
  }
}

async function main() {
  const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const browser = await launchChromium();
  const results = [];
  try {
    for (const item of config.pages) {
      const consoleErrors = [];
      const pageErrors = [];
      const page = await browser.newPage();
      page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });
      page.on('pageerror', (err) => pageErrors.push(String(err)));
      let httpStatus = null;
      let navigationFailed = false;
      try {
        const response = await page.goto(config.baseUrl + item.path,
          { timeout: config.navTimeoutMs });
        httpStatus = response ? response.status() : null;
        await page.screenshot({ path: item.screenshot });
      } catch (e) {
        navigationFailed = true;
      }
      await page.close();
      results.push({
        path: item.path, httpStatus, consoleErrors, pageErrors,
        screenshot: fs.existsSync(item.screenshot) ? item.screenshot : null,
        navigationFailed,
      });
    }
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify({ pages: results }));
}

main().catch((e) => {
  process.stdout.write(JSON.stringify({ error: String(e && e.message || e) }));
  process.exit(1);
});
"""


def _visit_pages_node(base_url: str, pages: Sequence[str], screens_dir: Path,
                      node_modules: Path, nav_timeout_sec: float) -> BrowserCheckResult:
    screens_dir.mkdir(parents=True, exist_ok=True)
    page_configs = [
        {"path": p, "screenshot": str(screens_dir / f"{i:02d}-{_safe_name(p)}.png")}
        for i, p in enumerate(pages)
    ]
    config = {"baseUrl": base_url, "pages": page_configs,
             "navTimeoutMs": nav_timeout_sec * 1000}
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        script_path = tmp_path / "driver.js"
        script_path.write_text(_NODE_DRIVER_SCRIPT, encoding="utf-8")
        env = {**os.environ, "NODE_PATH": str(node_modules)}
        try:
            result = subprocess.run(
                ["node", str(script_path), str(config_path)],
                capture_output=True, text=True, timeout=nav_timeout_sec * len(pages) + 30,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return BrowserCheckResult(status="error", pages=(), reason="node_driver_timeout")
        try:
            parsed = json.loads(result.stdout or "{}")
        except ValueError:
            return BrowserCheckResult(
                status="error", pages=(),
                reason=f"node_driver_bad_output: {(result.stderr or '')[:300]}",
            )
    if "error" in parsed:
        return BrowserCheckResult(status="error", pages=(), reason=str(parsed["error"])[:500])
    results = tuple(
        _page_result(
            row["path"], row.get("httpStatus"),
            tuple(row.get("consoleErrors", ())), tuple(row.get("pageErrors", ())),
            row.get("screenshot"), navigation_failed=bool(row.get("navigationFailed")),
        )
        for row in parsed.get("pages", ())
    )
    status = "ok" if all(p.ok for p in results) else "failed"
    return BrowserCheckResult(status=status, pages=results)


# ---- wiring note (not implemented here; plan: wiring comes later) ---
#
# Proposed call site: engine.application.acceptance.run_acceptance, right after the
# full suite's own snapshot is built and locked down (same per-stage
# disposable-snapshot pattern every other stage in that function
# already uses) -- call engine.infrastructure.browser_check.check_app(suite_snapshot,
# pages, screens_dir) once Scene gets its own optional `url` field
# (engine.domain.plan_model.Scene) and at least one scene in the plan sets it;
# screens_dir would be a new acceptance.json-adjacent directory, e.g.
# repo/.gongbang/<job>/screens/. A non-"ok" BrowserCheckResult should
# fail acceptance the same way a failing scene already does (new
# AcceptanceResult reason, e.g. "browser_check_failed"); "unavailable"
# should NOT fail acceptance (this host simply cannot run it) but
# should still be recorded on the acceptance.recorded event so a job
# that genuinely needed a screen check is never silently un-checked
# without a visible trace.
