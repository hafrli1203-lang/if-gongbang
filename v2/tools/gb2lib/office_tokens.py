"""--office-token file handling shared by the three AI-office approval
CLI entry points (`gb2 approve-design`/`approve-install`/`approve-
plan-edit`) -- split out of runner.py to keep that file under the
800-line architecture cap (docs/standards/architecture.md §3), and so
the three call sites' read-token + verify + reason-format logic cannot
drift apart from one another."""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from engine.application import office_approval
from engine.config import EngineConfig

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger

__all__ = ["load_office_token", "verify_office_token_or_reason"]


def load_office_token(path: str) -> dict | None:
    """The office server's signed approval document, read from a FILE
    (never the command line itself -- a secret-bearing document has
    no business in argv/process listings/shell history). None if the
    file cannot be read, is not valid JSON, or does not parse to a
    JSON object."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def verify_office_token_or_reason(config: EngineConfig, ledger: "Ledger", app: str,
                                   job_name: str, attempt_id: str, kind: str,
                                   token_path: str, *,
                                   expected_target_version: str | None = None,
                                   ) -> tuple[str | None, str | None]:
    """("office", None) iff the document at `token_path` is a valid,
    unused office-approval token for exactly this (app, job_name,
    attempt_id, kind) -- the confirm_source a caller should record in
    place of "tty". Otherwise (None, <reason>): "office_token_
    unreadable" if the file cannot be read/parsed, or
    "office_token_refused: <reason>" wrapping whatever engine.
    application.office_approval.verify_office_token itself refused
    (bad/forged signature, expired, a reused nonce, or a stale
    target_version).

    `expected_target_version`: the caller's OWN pinned
    snapshot value (engine.application.office_approval.
    install_target_version/design_target_version/
    plan_edit_target_version, computed from the EXACT same `pending`
    list / file read the caller is about to act on) -- passed straight
    through to verify_office_token so the verification and the actual
    approval share one snapshot instead of two independent queries
    that could disagree if something changed in between. `None` (the
    default) lets verify_office_token compute it fresh on its own."""
    token = load_office_token(token_path)
    if token is None:
        return None, "office_token_unreadable"
    ok, reason = office_approval.verify_office_token(
        config, ledger, app, job_name, attempt_id, kind, token,
        expected_target_version=expected_target_version,
    )
    if not ok:
        return None, f"office_token_refused: {reason}"
    return "office", None
