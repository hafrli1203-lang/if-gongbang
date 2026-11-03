#!/usr/bin/env python3
# Generated from the private ops workspace at b94af62b97af64e2e58e8c102fa2538f066ae366; do not edit by hand (see harness/export_ops_public.py).
"""Merges this bundle's hook registrations into ~/.claude/settings.json's "hooks" section,
preserving every existing entry (never truncates or replaces the whole file) - the merge half of
`apply_hooks_v3.sh`.

v2 gap bundle 1/3 (team-lead 2026-11-02, plan `_ops/plans/2026-11-02-v2-gap-verdicts.md`
6절): block_dangerous.py, protect_sensitive_files.py, read_whole.py, directive_check.py,
orbit_nudge.py and bansang_habit.py all existed as files on disk but were NOT present anywhere in
settings.json's hooks section (verified by reading the live file - see this task's report) -
"승인 없이는 위험 명령 거부" (MASTER_RULES.md) was a documented promise with nothing behind it
(공백①). This script is what actually connects them.

Idempotent: run twice in a row and the second run makes no changes (detected by the exact
`command` string already being present in the matching event+matcher's hooks list). The approval
gate itself (approve_gate.py / scope_guard.py's general reversible-action gate) is never touched
or added here - "승인 관문 자체는 바꾸지 말 것" (team-lead instruction).

Usage:
  python3 merge_settings_hooks.py <settings.json path> <hooks dir path> [--dry]
`--dry` prints the unified diff of the hooks section that WOULD be written and makes no change
to the file on disk.
"""
from __future__ import annotations

import copy
import difflib
import json
import sys

# (event name, matcher string, hook script basename, extra kwargs for the hook entry dict)
REGISTRATIONS = [
    ("UserPromptSubmit", "", "lesson-capture.py", {}),
    ("PreToolUse", "Bash", "block_dangerous.py", {}),
    ("PreToolUse", "Read|Edit|Write|MultiEdit|NotebookEdit|Bash", "protect_sensitive_files.py", {}),
    # item 144: no-ops instantly for any non-mcp__ tool name, so the broad "*" matcher is cheap.
    ("PreToolUse", "*", "mcp_scope_limit.py", {}),
    # hooks-B's own file (v2 gap bundle 4, real-time scope fence) - registered here per
    # team-lead instruction so one apply script covers both bundles' hooks in one pass.
    ("PreToolUse", "Edit|Write|MultiEdit|NotebookEdit|Bash|Agent", "scope_fence.py", {}),
]

# The old, now-superseded registration this bundle replaces (widened matcher, new file).
OLD_PROTECT_SENSITIVE_FILES_BASENAME = "protect-sensitive-files.py"
# Hooks the owner decided not to run (2026-11-02): removed on every apply, never re-added.
# 2026-11-02 wiring audit (plans/2026-11-02-wiring-audit-workspace.md 2절, "미등록 훅 파일 14개"):
# these 11 were sitting in ~/.claude/hooks with no settings.json entry anywhere (dead files, not
# a registration this script needs to strip) - added here anyway so the RETIRED_BASENAMES list is
# the single source of truth for "which hook basenames are not supposed to run", and so
# apply_hooks_v3.sh's remove_retired_files() (physical cleanup, see that script) has one list to
# read instead of two.
RETIRED_BASENAMES = (
    "protect-sensitive-files.py", "search-year.py", "auto-format.py", "library-nudge.py",
    "approve-gate.py", "notify-complete.py",
    "bansang_habit.py", "block-dangerous.py", "directive_check.py", "finish-output.mjs",
    "gb_prefix_unwrap.py", "fence_parse.py", "orbit_nudge.py", "preflight-gate.py",
    "read_whole.py", "scope-guard.py", "worker_rules.py",
)


def _command_for(hooks_dir: str, basename: str) -> str:
    return f"python3 {hooks_dir}/{basename}"


def _entry_commands(entry: dict) -> list[str]:
    return [h.get("command", "") for h in entry.get("hooks", []) if isinstance(h, dict)]


def _has_command(entry: dict, command: str) -> bool:
    return command in _entry_commands(entry)


def _basename_from_command(command: str) -> str:
    return command.rsplit("/", 1)[-1].strip()


def remove_old_registration(settings: dict, hooks_dir: str) -> bool:
    """Removes any existing hooks-list item whose command runs OLD_PROTECT_SENSITIVE_FILES_BASENAME
    (any directory, not only `hooks_dir`, so an install that used a different path is still
    cleaned up), replaced by this bundle's protect_sensitive_files.py registration. Returns True
    if anything changed."""
    changed = False
    hooks = settings.setdefault("hooks", {})
    for event_name, entries in list(hooks.items()):
        if not isinstance(entries, list):
            continue
        remaining_entries = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                remaining_entries.append(entry)
                continue
            kept = [h for h in entry["hooks"]
                    if not (isinstance(h, dict)
                            and _basename_from_command(str(h.get("command", ""))) in RETIRED_BASENAMES)]
            if len(kept) != len(entry["hooks"]):
                changed = True
            if not kept:
                continue  # the whole entry is now empty (its only hook was the old one) - drop it
            entry["hooks"] = kept
            remaining_entries.append(entry)
        hooks[event_name] = remaining_entries
    return changed


def add_registrations(settings: dict, hooks_dir: str) -> bool:
    changed = False
    hooks = settings.setdefault("hooks", {})
    for event_name, matcher, basename, extra in REGISTRATIONS:
        command = _command_for(hooks_dir, basename)
        entries = hooks.setdefault(event_name, [])
        if not isinstance(entries, list):
            continue
        if any(_has_command(e, command) for e in entries if isinstance(e, dict)):
            continue  # already registered - idempotent no-op
        new_entry = {"hooks": [{"type": "command", "command": command, **extra}]}
        if matcher:
            new_entry = {"matcher": matcher, **new_entry}
        entries.append(new_entry)
        changed = True
    return changed


# Permission deny rules every session on this account must carry (structural, not advisory).
# 2026-11-02: four worker sessions in one day parked a test run in the background and ended their
# turn, so the lead never got the result (owner: "오류가 많은데?"). A project-level deny only covers
# cwd under /apps; worker worktrees live in the scratchpad, so the rule has to be account-wide.
# Long commands run in the foreground with timeout up to 600000 instead.
REQUIRED_DENY = ("Bash(run_in_background:true)",)


def add_required_deny(settings: dict) -> bool:
    perms = settings.setdefault("permissions", {})
    deny = perms.setdefault("deny", [])
    if not isinstance(deny, list):
        return False
    missing = [d for d in REQUIRED_DENY if d not in deny]
    if not missing:
        return False
    perms["deny"] = [*deny, *missing]
    return True


def merge(settings: dict, hooks_dir: str) -> tuple[dict, bool]:
    out = copy.deepcopy(settings)
    changed = remove_old_registration(out, hooks_dir)
    changed = add_registrations(out, hooks_dir) or changed
    changed = add_required_deny(out) or changed
    return out, changed


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--dry"]
    dry = "--dry" in sys.argv[1:]
    if len(args) != 2:
        print("usage: merge_settings_hooks.py <settings.json> <hooks_dir> [--dry]", file=sys.stderr)
        return 2
    settings_path, hooks_dir = args
    with open(settings_path, encoding="utf-8") as f:
        before = json.load(f)
    after, changed = merge(before, hooks_dir)
    before_text = json.dumps(before.get("hooks", {}), ensure_ascii=False, indent=1, sort_keys=True).splitlines()
    after_text = json.dumps(after.get("hooks", {}), ensure_ascii=False, indent=1, sort_keys=True).splitlines()
    diff = list(difflib.unified_diff(before_text, after_text, fromfile="hooks(before)", tofile="hooks(after)", lineterm=""))
    if dry:
        if diff:
            print("\n".join(diff))
        else:
            print("(변경 없음 — 이미 등록됨, idempotent)")
        return 0
    if not changed:
        print("변경 없음 — 이미 등록됨 (idempotent)")
        return 0
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(after, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print("\n".join(diff))
    return 0


if __name__ == "__main__":
    sys.exit(main())
