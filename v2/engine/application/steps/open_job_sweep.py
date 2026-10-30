"""Stale `.tmp-` staging-directory sweep for one project, run by
engine.application.steps.open_job at the top of every open (split out so
open_job.py stays under the repo's 800-line budget)."""
from __future__ import annotations

import re
import shutil
from typing import TYPE_CHECKING

from engine.config import EngineConfig
from engine.domain.records import LedgerError

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger

def sweep_stale_tmp_dirs(ledger: "Ledger", project: str, config: EngineConfig) -> None:
    """Removes every `.tmp-` staging directory this project can have left
    behind that the intent-based reconcile above cannot name,
    Observed on a live job: a driver killed mid-copy twice left `copies/.tmp-J-
    <app>-…` with a half-extracted tar, and a close killed mid-
    archive left `archive/<app>/<attempt>/.tmp-<token>`; both sat
    there until a human moved them). Safe here because open_job holds
    the project lock: no other open_job for THIS project is mid-copy, so
    any `copies/.tmp-J-{project}-*` is garbage; and an archive `.tmp-`
    is garbage unless a close is in flight right now (project state
    CLOSING), in which case it is left alone. Other projects' dirs
    (different "J-{project}-" prefix / archive subfolder) are never
    touched."""
    copies_root = config.home / "copies"
    # Exact shape ".tmp-J-{project}-{ulid}-{n}": the remainder after the project
    # name is one dash-free id segment plus the attempt number, so project
    # "alpha" never matches "alpha-api"'s staging dirs.
    mine = re.compile(rf"^\.tmp-J-{re.escape(project)}-[0-9A-Za-z]+-\d+$")
    if copies_root.is_dir() and not copies_root.is_symlink():
        for entry in copies_root.iterdir():
            if entry.is_symlink() or not entry.is_dir():
                continue
            if mine.match(entry.name):
                shutil.rmtree(entry, ignore_errors=True)
    try:
        closing = ledger.project(project).state == "CLOSING"
    except LedgerError:
        closing = False
    archive_root = config.home / "archive" / project
    if closing or archive_root.is_symlink() or not archive_root.is_dir():
        return
    for attempt_dir in archive_root.iterdir():
        # Never follow a symlink (an attempt dir or a .tmp- entry pointing
        # outside the archive would otherwise have its target removed).
        if attempt_dir.is_symlink() or not attempt_dir.is_dir():
            continue
        for entry in attempt_dir.iterdir():
            if entry.name.startswith(".tmp-") and entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)


__all__ = ["sweep_stale_tmp_dirs"]
