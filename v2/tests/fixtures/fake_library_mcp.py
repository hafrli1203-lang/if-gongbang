"""A fake MCP stdio server for engine.context's library-lookup tests.
Speaks the same minimal protocol engine.context._StdioMcpClient does:
newline-delimited JSON-RPC 2.0, `initialize` -> {} result, then a
`notifications/initialized` notification (no response expected), then
any number of `tools/call` requests.

Supported tools:
  library_search {query, k} -> one real result and one `origin:
      "run_output"` result (engine.context must filter the latter out).
  lessons_for {app} -> one lesson result.

Run standalone (no args) for the success path. `--crash-after-init`
exits right after the initialize response, before ever answering a
tools/call, so a test can exercise engine.context's McpClientError /
`failed=True` path. `--hang-on-call` accepts a tools/call request but
never answers it (sleeps well past any sane test timeout), so a test
can exercise the client's own read-timeout path. `--notify-before-reply`
writes one id-less JSON-RPC notification before each tools/call's real
reply, so a test can exercise the client's id-matching (a notification
must never be mistaken for the reply it is waiting on). `--flood-
notifications` writes an id-less notification every 0.3s, forever,
instead of ever answering the tools/call -- so a test can check that
the client enforces ONE overall deadline for the whole request, not a
fresh per-message window that a steady drip of notifications (each
well inside any single read's own timeout) could otherwise stretch out
indefinitely."""
from __future__ import annotations

import json
import sys
import time


def _write(message: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _read() -> dict[str, object] | None:
    line = sys.stdin.readline()
    if not line:
        return None
    return json.loads(line)


# Shapes copied from the real company library MCP live call):
# library_search answers ONE object {"audience", "policy", "results": [...]}
# whose results carry title/text/citation/path/origin/authorityLabel, and
# `origin` is the library's own display label ("실행 결과" for AI run
# output) -- not a bare list, and not the internal key. lessons_for answers
# plain text, not JSON. The earlier list-shaped fake hid both mismatches.
_LIBRARY_PAYLOAD = {
    "audience": "internal",
    "policy": "internalCitation=true results are internal-only.",
    "results": [
        {"title": "sample-app/guide.pdf", "text": "3개월 넘으면 재주문 후보로 본다.",
         "citation": "guide.pdf · p.12", "path": "sample-app/guide.pdf",
         "origin": "회사", "authorityLabel": "운영자 지정 · 메인",
         "internalCitation": True},
        {"title": "draft.md", "text": "AI가 방금 만든 광고 카피 초안.",
         "citation": "draft.md", "path": "runs/draft.md", "origin": "실행 결과",
         "authorityLabel": "", "internalCitation": True},
    ],
}

_LESSONS_TEXT = (
    "[lesson-01 · 경험·아직 기준 아님] 카피 길이를 지어내지 말 것. / 근거 없는 숫자는 쓰지 않는다.\n"
    "[lesson-02 · 경험·아직 기준 아님] 화면 예시는 서로 다른 업종 두 개."
)


def _crash_once_marker_arg() -> str | None:
    """`--crash-once-marker=<path>`: the FIRST process launched with a
    given marker path acts exactly like --crash-after-init (so
    engine.context's one gather()-level retry, 결함 49, has a real
    one-off failure to retry past); every later process -- the marker
    file now exists -- behaves like the plain success path. Lets a test
    exercise "spawn 1 of this exact command fails, spawn 2 of the SAME
    command succeeds" without engine.context ever reusing a process
    across its own retry attempts (gather() always spawns fresh)."""
    for arg in sys.argv[1:]:
        if arg.startswith("--crash-once-marker="):
            return arg[len("--crash-once-marker="):]
    return None


def main() -> None:
    crash_after_init = "--crash-after-init" in sys.argv[1:]
    hang_on_call = "--hang-on-call" in sys.argv[1:]
    notify_before_reply = "--notify-before-reply" in sys.argv[1:]
    flood_notifications = "--flood-notifications" in sys.argv[1:]
    marker = _crash_once_marker_arg()
    if marker is not None:
        from pathlib import Path
        marker_path = Path(marker)
        if not marker_path.exists():
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.write_text("crashed-once\n", encoding="utf-8")
            crash_after_init = True
    while True:
        request = _read()
        if request is None:
            return
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            _write({"jsonrpc": "2.0", "id": request_id, "result": {}})
            if crash_after_init:
                return
        elif method == "notifications/initialized":
            continue
        elif method == "tools/call":
            if hang_on_call:
                time.sleep(3600)
                return
            if flood_notifications:
                while True:
                    time.sleep(0.3)
                    _write({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})
            if notify_before_reply:
                _write({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})
            params = request.get("params", {})
            name = params.get("name")
            if name == "library_search":
                text = json.dumps(_LIBRARY_PAYLOAD, ensure_ascii=False)
            elif name == "lessons_for":
                text = _LESSONS_TEXT
            else:
                text = json.dumps([], ensure_ascii=False)
            _write({
                "jsonrpc": "2.0", "id": request_id,
                "result": {"content": [{"type": "text", "text": text}]},
            })
        else:
            _write({"jsonrpc": "2.0", "id": request_id, "result": {}})


if __name__ == "__main__":
    main()
