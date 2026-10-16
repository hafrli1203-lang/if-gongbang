#!/usr/bin/env python3
"""Shared config loader for every IF-Gongbang hook, metric and ops script.

Every environment-specific value (apps root, work-queue file, alerts file, lessons storage,
library/knowledge-base root, transcripts root, log dir, the approval time window, the extra
hook names a local install wants protected) is resolved HERE and only here. Nothing else in
this repo hardcodes a path.

Resolution order (first one found wins, then defaults fill in anything missing):
  1. The file named by the GONGBANG_CONFIG environment variable.
  2. gongbang.config.json found by walking up from this file (repo root, typically).
  3. Built-in defaults below, all under the current user's home directory, so a fresh
     checkout works without any setup.

See gongbang.config.example.json at the repo root for the shape of the file.

Validation: a malformed config must never silently produce a value a security-critical hook
(scope_guard.py) can crash on. Every path-like key is guaranteed to come back as a non-empty
absolute string: a type-mismatched or empty override is ignored (the built-in default is used
for that key instead), and every path value is both expanduser'd and made absolute, so a
relative `apps_root` in the config file can never silently make every absolute-path prefix
comparison in preflight_gate.py fail to match (which would be a fail-OPEN gap for a gate that
is supposed to fail closed).

The approval gate is not file-based (see fence_parse.py): approval_window_seconds and
extra_protected_hook_names below are the environment-tunable parts of it.
installed_hooks_dir names the REAL directory scope_guard.py protects (see below), not merely
"where hooks live".
"""
import json
import os

DEFAULTS = {
    # Root directory under which every app/project repo lives.
    "apps_root": "~/apps",
    # App folder names under apps_root that are exempt from the pre-change report gate
    # (e.g. the gongbang control repo itself, or a docs-only scratch folder).
    "exempt_apps": [],
    # Single shared work-queue file (one line of truth for "what to do next"). Only its
    # basename is compared against an edited file's basename (see scope_guard.py), so renaming
    # this still works without touching code.
    "work_queue_path": "~/.gongbang/WORK_QUEUE.md",
    # Open operational alerts the session should surface before anything else.
    "alerts_path": "~/.gongbang/ALERTS.md",
    # Local, file-based storage for the lesson log (L1). One JSONL file per app.
    "lessons_dir": "~/.gongbang/lessons",
    # Root folder of searchable reference material ("the archive"), used only to derive
    # topic keywords at runtime - never a fixed industry/keyword list.
    "library_root": "~/.gongbang/library",
    # Where Claude Code session transcripts live - the ONLY place scope_guard.py/
    # approve_gate.py look for approval evidence (see fence_parse.py).
    "transcripts_root": "~/.claude/projects",
    # Where hooks may append their own diagnostic logs.
    "log_dir": "~/.gongbang/logs",
    # Preflight gate mode: "warn" (never blocks, just logs+notices) or "block" (denies).
    "preflight_mode": "warn",
    # Name of the knowledge-base search tool a UserPromptSubmit nudge should mention.
    "library_tool_name": "library_search",
    # How far back (seconds) scope_guard.py/approve_gate.py replay the owner's chat transcript
    # for "승인 <TASK>" / "승인 취소". Default 4 hours, matching docs/concept.md.
    "approval_window_seconds": 4 * 3600,
    # Extra hook/script basenames (no extension needed) a local install wants protected on top
    # of this repo's own built-in list (fence_parse.PROTECTED_HOOK_NAMES) - e.g. a site-local
    # hook that isn't part of this repo but still must never be touched by a tool call.
    "extra_protected_hook_names": [],
    # The REAL directory the agent harness actually loads hooks from. scope_guard.py's
    # always-block protection applies ONLY to files inside this directory (resolved via
    # realpath) - not to a same-named file anywhere else in the filesystem (e.g. this very
    # repo's own hooks/ folder before it is installed).
    "installed_hooks_dir": "~/.claude/hooks",
    # The (retired, file-based) approval-gate directory, kept here only so scope_guard.py can
    # still refuse to let a tool call resurrect it (defense in depth for a mid-upgrade
    # install). Set to "" to disable this check entirely.
    "legacy_gate_dir": "~/.claude/gate",
    # Regex patterns (case-insensitive) matching a Bash command that actually RUNS a test
    # suite, judged against the command's own ACTUAL first command (see
    # verify_claims.KNOWN_TEST_BUILD_PROGRAMS) - not just these words appearing anywhere in the
    # command text. verify_claims.py requires at least one of these to have been run in the
    # current turn, with a result that actually came back and was not an error or a
    # background-launch acknowledgement, before it lets a "테스트 통과" claim stand - a plain
    # "완료/성공" claim still only needs some tool call (see verify_claims.py).
    "test_command_patterns": [
        r"\bpytest\b", r"\bpython3?\s+-m\s+pytest\b", r"\bpython3?\s+-m\s+unittest\b",
        r"\bunittest\b", r"\bvitest\b", r"\bjest\b",
        r"\bnpm\s+(test|run\s+test)\b", r"\b(pnpm|yarn)\s+test\b",
        r"\bcargo\s+test\b", r"\bgo\s+test\b",
        r"\bmvn\s+test\b", r"\bgradle(w)?\s+test\b",
    ],
    # Same idea as test_command_patterns, but for a "빌드 성공" claim - a passing test run does
    # not satisfy a build claim, and vice versa (see verify_claims.py).
    "build_command_patterns": [
        r"\bnpm\s+run\s+build\b", r"\b(pnpm|yarn)\s+build\b",
        r"\btsc\b", r"\bcargo\s+build\b", r"\bgo\s+build\b", r"\bgradle(w)?\s+build\b",
    ],
}

# Keys whose value must be a non-empty absolute path. A malformed or type-mismatched override
# for one of these is ignored (falls back to DEFAULTS[key]) rather than propagated, and every
# value that does survive is both expanduser'd and os.path.abspath'd before being returned.
PATH_KEYS = {
    "apps_root", "work_queue_path", "alerts_path",
    "lessons_dir", "library_root", "transcripts_root", "log_dir", "installed_hooks_dir",
}

# Like PATH_KEYS, but an explicit empty string IS a valid override (it means "disable this
# check") rather than falling back to the default - only legacy_gate_dir uses this today.
OPTIONAL_PATH_KEYS = {"legacy_gate_dir"}


def _find_repo_config(start_dir: str) -> str | None:
    here = os.path.abspath(start_dir)
    for _ in range(6):
        candidate = os.path.join(here, "gongbang.config.json")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return None


def _valid_override(key: str, value: object) -> bool:
    default = DEFAULTS.get(key)
    if key not in DEFAULTS:
        return True  # an unknown key is harmless extra data, let it through unexamined
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, str):
        if key in OPTIONAL_PATH_KEYS:
            return isinstance(value, str)  # "" is a valid, meaningful override here
        return isinstance(value, str) and value.strip() != ""
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, list):
        return isinstance(value, list)
    return True


def load(start_dir: str | None = None) -> dict:
    """Returns the merged config. Every PATH_KEYS value comes back as a non-empty absolute
    path (expanduser + abspath already applied) - callers never need to validate or expand
    paths themselves."""
    cfg = dict(DEFAULTS)
    path = os.environ.get("GONGBANG_CONFIG")
    if not path:
        path = _find_repo_config(start_dir or os.path.dirname(os.path.abspath(__file__)))
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                overrides = json.load(f)
            if isinstance(overrides, dict):
                for k, v in overrides.items():
                    if _valid_override(k, v):
                        cfg[k] = v
                    # else: silently keep the safe default for this one key - a hook that
                    # crashes on a bad config value is worse than one that ignores it.
        except Exception:
            pass
    out: dict = {}
    for k, v in cfg.items():
        if k in PATH_KEYS:
            s = v if isinstance(v, str) and v.strip() else DEFAULTS.get(k, v)
            out[k] = os.path.abspath(os.path.expanduser(str(s)))
        elif k in OPTIONAL_PATH_KEYS:
            out[k] = "" if (isinstance(v, str) and v.strip() == "") else os.path.abspath(os.path.expanduser(str(v)))
        else:
            out[k] = v
    return out


def run_with_deadline(seconds: float, fn, default=None):
    """Runs fn() under a wall-clock deadline (SIGALRM-based on POSIX platforms that support
    it; a plain call with no deadline everywhere else). Returns fn()'s result, or `default` if
    it raised anything at all - including the deadline itself. Used by UserPromptSubmit hooks
    (lesson_capture.py, library_nudge.py) that must never add real latency to the user's
    prompt, no matter how adversarial or how long the input is."""
    try:
        import signal
    except ImportError:
        signal = None
    use_alarm = signal is not None and hasattr(signal, "SIGALRM") and hasattr(signal, "setitimer")
    previous = None
    try:
        if use_alarm:
            def _on_timeout(signum, frame):
                raise TimeoutError("gongbang hook deadline exceeded")
            previous = signal.signal(signal.SIGALRM, _on_timeout)
            signal.setitimer(signal.ITIMER_REAL, seconds)
        return fn()
    except Exception:
        return default
    finally:
        if use_alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)
            if previous is not None:
                signal.signal(signal.SIGALRM, previous)


if __name__ == "__main__":
    print(json.dumps(load(), ensure_ascii=False, indent=2))
