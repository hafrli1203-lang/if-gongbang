#!/usr/bin/env python3
"""SessionStart hook: shows this project's L1 experiences (owner feedback kept verbatim,
not yet a standard) so the next session reads them before repeating what the owner already
pointed out once.

Reads directly from the local JSONL store (lessons_store.py) - no database and no child
process. Fails open: exit 0, no output, if cwd is not under apps_root or nothing is stored
yet for this app.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gongbang_config  # noqa: E402
import lessons_store  # noqa: E402

HEADER = "## 이 앱의 작업 일지 (L1 경험 — 아직 기준 아님 / 기준은 그 앱의 결정 문서에)"


def derive_app(cwd: str, cfg: dict) -> str | None:
    apps_root = os.path.normpath(cfg["apps_root"])
    cwd = os.path.normpath(cwd or "")
    if cwd == apps_root:
        return "workspace"
    prefix = apps_root + os.sep
    if not cwd.startswith(prefix):
        return None
    app = cwd[len(prefix):].split(os.sep, 1)[0]
    return app or "workspace"


def _label(status: str) -> str:
    return "기준" if status == "promoted" else "경험·아직 기준 아님"


def _scope_note(scope: str, exceptions: str) -> str:
    parts = []
    if scope:
        parts.append(f"범위: {scope}")
    if exceptions:
        parts.append(f"예외: {exceptions}")
    return f" ({' · '.join(parts)})" if parts else ""


#: statuses that must never be shown as live guidance. "retired" is a standard that was
#: promoted and later withdrawn; "rejected" is the owner explicitly saying the proposed rule
#: is wrong - its `rule` text must never be re-injected into a later session as if it were
#: still live advice.
_HIDDEN_STATUSES = ("retired", "rejected")


def build_output(rows: list[dict]) -> str:
    lines = [
        f"[{r['number']} · {_label(r['status'])}] {r['scene']} / {r['why']} / 다음부터: {r['rule']}"
        f"{_scope_note(r.get('scope', ''), r.get('exceptions', ''))}"
        for r in rows if r.get("status") not in _HIDDEN_STATUSES
    ]
    if not lines:
        return ""
    body = "\n".join([HEADER] + lines)
    byte_size = len(body.encode("utf-8"))
    return f"{body}\n총 {len(lines)}건 · {byte_size}바이트"


def main() -> int:
    try:
        data = json.load(sys.stdin)
        cfg = gongbang_config.load()
        cwd = str(data.get("cwd", "")) if isinstance(data, dict) else ""
        app = derive_app(cwd, cfg)
        if app is None:
            return 0
        rows = lessons_store.list_lessons(app, cfg)
        out = build_output(rows)
        if out:
            print(out)
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
