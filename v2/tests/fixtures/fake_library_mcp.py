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
`failed=True` path."""
from __future__ import annotations

import json
import sys


def _write(message: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _read() -> dict[str, object] | None:
    line = sys.stdin.readline()
    if not line:
        return None
    return json.loads(line)


_LIBRARY_RESULTS = [
    {"title": "안경원 재주문 기준", "text": "3개월 넘으면 재주문 후보로 본다.",
     "source": "lib:opt-rule-12", "origin": "approved"},
    {"title": "어제 만든 초안", "text": "AI가 방금 만든 광고 카피 초안.",
     "source": "run:job-99", "origin": "run_output"},
]

_LESSON_RESULTS = [
    {"title": "경험 사례 1", "text": "카피 길이를 지어내지 말 것.",
     "source": "lesson:case-1", "origin": "experience"},
]


def main() -> None:
    crash_after_init = "--crash-after-init" in sys.argv[1:]
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
            params = request.get("params", {})
            name = params.get("name")
            if name == "library_search":
                payload = _LIBRARY_RESULTS
            elif name == "lessons_for":
                payload = _LESSON_RESULTS
            else:
                payload = []
            _write({
                "jsonrpc": "2.0", "id": request_id,
                "result": {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]},
            })
        else:
            _write({"jsonrpc": "2.0", "id": request_id, "result": {}})


if __name__ == "__main__":
    main()
