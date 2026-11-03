#!/usr/bin/env python3
# Generated from the private ops workspace at b94af62b97af64e2e58e8c102fa2538f066ae366; do not edit by hand (see harness/export_ops_public.py).
"""Safety hook: denies any tool call that reads, writes, or otherwise names a path the owner has
fenced off - secrets/keys, the enforcement hooks themselves, the harness's own settings.json and
gongbang.config.json, this server's systemd units and Caddy config, and the work-queue's "[x]"
completion marks.

v2 gap bundle 1+2 (team-lead 2026-11-02, plan `_ops/plans/2026-11-02-v2-gap-verdicts.md`
6절, 공백③, items 1/2/7/11/18/19/280/281): the previously installed version of this hook only
matched a hardcoded list of secret-file patterns (.env/.pem/.key/...) against Edit/Write's own
file_path - no config loader, no hooks/gate/settings.json/systemd/Caddyfile protection, no
WORK_QUEUE.md "[x]" protection, and ran only on Edit/Write (never Read, never Bash).

A Codex read-only adversarial review (2026-11-02) of the FIRST version of this file found and
this revision fixes several real bypasses:
  - `truncate -s 0 <hook>` / `rm <hook>` / `mv <hook> /tmp/...` were none of them recognized as
    touching the hooks dir (only a redirect/tee/cp/mv-DESTINATION was checked) - truncating,
    deleting, or moving a hook file out disables it exactly as effectively as overwriting it, so
    `_touched_write_paths()` below now also treats rm's and truncate's every positional arg, and
    cp/mv's SOURCE argument (not just the destination), as a touched path;
  - `bash -c '...'`/`env -S '...'` wrapping a write to settings.json was never unwrapped, because
    this file had its own, simpler touched-path scan instead of sharing `_shell.all_segments()`
    with block_dangerous.py - now it does;
  - `~/.claude/./settings.json` (a harmless-looking "." path segment) defeated the hard-pattern
    regex, which never normalized the path first - `_path_candidates()` now also normpath's;
  - `~/.claude/gongbang.config.json` itself - the file this hook's OWN protected-path list is
    read from - was not protected at all: an agent could write a config override that changes
    `installed_hooks_dir`, so the next hook invocation would protect the WRONG directory instead
    of the real, live one. Fixed by making the two directories/files this hook's self-protection
    depends on (the real hooks dir, the real gate dir, settings.json, settings.local.json, and
    gongbang.config.json) a HARDCODED constant (REAL_CLAUDE_HOME below), never read from the
    (overridable) loaded config - only the non-security-critical patterns (ordinary secret-file
    globs) stay config-driven per items 1/11;
  - `echo '[x] TASK1' | tee -a WORK_QUEUE.md` (a pipe) and `sed -i 's/ ] TASK1/x] TASK1/' ...`
    (building "[x]" from two separate pieces) both defeated the old "literal '[x]' text in the
    SAME segment as the write target" check. The WORK_QUEUE check is simplified instead: ANY
    Bash write-target hit on WORK_QUEUE.md (redirect/tee/cp/mv/rm/truncate/sed -i/perl -i) is
    denied outright, regardless of what text the command contains - marking a task done is
    work_claim.py's job alone, so there is no legitimate reason for an agent's Bash call to write
    to that file by any other means at all.

This version also reads its ordinary secret-file glob patterns from gongbang_config.py
(protected_paths/protected_exceptions) instead of hardcoding them (item 1/11).

team-lead instruction 2026-11-02 (사고①, 반장 우회 길 차단): also protects work-copy bookkeeping
files - /apps/_wt/<app>/<name>/.wt.json(.tmp)/.wt.scope/.wt.done - from a direct Bash/Edit/
Write/MultiEdit by any session. See check_wt_state_protect() below for why no allowlist exception
is needed for wt-new.sh/wt-land.sh/wt-gc.sh themselves.

team-lead instruction 2026-11-03 08:40 (WORK_QUEUE 32번, 사본 상한 우회 사고): also protects every
app's ORIGINAL source tree - /apps/<app>/{src,tests,scripts,app,components,lib,prisma,
migrations}/** for an <app> not starting with "_" - from a direct Edit/Write/MultiEdit/
NotebookEdit or Bash write/delete/truncate, from ANY session (not only a WT_COPY session - unlike
scope_fence.py's Part 2, which only fences a session wt-run.sh itself marked as running inside a
copy). See check_app_source_protect() below; WT_LEAD_DIRECT=1 is the lead's own deliberate,
per-call exception.

PreToolUse hook (Read | Edit | Write | MultiEdit | NotebookEdit | Bash) - exit 0 always; a deny
is expressed as a PreToolUse deny JSON body on stdout (the harness's own deny-decision shape),
not via exit code.
"""
import fnmatch
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _shell  # noqa: E402
import gongbang_config  # noqa: E402

PATH_TOOLS = {"Read": "file_path", "Edit": "file_path", "Write": "file_path",
              "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# Owner decision 2026-11-02 00:35 ("훅 폴더·settings.json·... 쓰기(단 apply_hooks_v3.sh 같은
# 명시 스크립트 경로는 허용 목록)"): the reviewed, version-controlled installer is explicitly
# exempt from this hook's self-protect/hard-pattern checks - its whole JOB is to write into the
# hooks dir and merge settings.json. Matched on the BASENAME of the script a Bash segment's own
# first command actually runs (via `_shell._first_command`, so a prefix chain or an absolute/
# relative path to the same script still matches) - never a free-text substring match, so an
# unrelated command that merely mentions the installer's name in a comment or argument is not
# exempted.
INSTALLER_ALLOWLIST = {"apply_hooks_v3.sh", "merge_settings_hooks.py"}
# Interpreters that can legitimately run the installer as `<interpreter> <script> ...` - only
# these, never an arbitrary command that happens to take the installer's name as an argument
# (Codex P1 2026-11-02: `true apply_hooks_v3.sh; printf '{}' > ~/.claude/settings.json` was
# exempting the WHOLE command, including the forging segment, because the old check matched the
# allowlist name against EITHER the segment's own command OR its first argument with no
# requirement that the command be an interpreter at all).
_INSTALLER_INTERPRETERS = {"bash", "sh", "python3", "python"}


def _is_installer_segment(seg: list[str]) -> bool:
    """True only when THIS segment, on its own, is a direct or interpreter-run invocation of the
    installer - `apply_hooks_v3.sh ...` / `./apply_hooks_v3.sh ...` (direct exec) or
    `bash|sh|python3|python apply_hooks_v3.sh ...` (interpreter + the installer as its own first
    positional argument, never buried deeper). `true apply_hooks_v3.sh` does NOT match - "true"
    is not an interpreter, so its argument is just an ordinary (ignored) argument, not a script
    path."""
    name, i = _shell._first_command(seg)
    if i < 0:
        return False
    base = os.path.basename(name.strip("'\""))
    if base in INSTALLER_ALLOWLIST:
        return True
    if base in _INSTALLER_INTERPRETERS:
        rest = _shell._positional_args(seg[i + 1:])
        if rest and os.path.basename(rest[0].strip("'\"")) in INSTALLER_ALLOWLIST:
            return True
    return False


def _runs_allowlisted_installer(command: str) -> bool:
    """True only when EVERY segment of `command` is, on its own, an installer invocation (see
    _is_installer_segment) - kept for the simple single-segment legitimate calls this hook's own
    `_run()` early-returns 0 for. Fixed 2026-11-02 (Codex P1): no longer exempts a MULTI-segment
    command just because one segment mentions the installer - `_touched_write_paths()` below now
    skips only the matching segment's OWN targets, so a smuggled sibling segment (`; printf ... >
    ...`) is still scanned and denied normally even when this returns False for the whole thing."""
    segs = _shell.all_segments(command)
    return bool(segs) and all(_is_installer_segment(s) for s in segs)

DEFAULT_PROTECTED = gongbang_config.DEFAULTS["protected_paths"]
DEFAULT_EXCEPTIONS = gongbang_config.DEFAULTS["protected_exceptions"]

# The REAL control-plane paths this install's harness actually reads, hardcoded rather than
# config-driven (see module docstring: a config loader that could change what protects the
# config loader is a hole in itself). `gongbang.config.json` is included so the file that
# WOULD override installed_hooks_dir/legacy_gate_dir elsewhere in this codebase cannot itself be
# planted or edited by a tool call.
REAL_CLAUDE_HOME = os.path.expanduser("~/.claude")
REAL_HOOKS_DIR = os.path.join(REAL_CLAUDE_HOME, "hooks")
REAL_GATE_DIR = os.path.join(REAL_CLAUDE_HOME, "gate")
REAL_PROTECTED_FILES = {
    os.path.join(REAL_CLAUDE_HOME, "settings.json"),
    os.path.join(REAL_CLAUDE_HOME, "settings.local.json"),
    os.path.join(REAL_CLAUDE_HOME, "gongbang.config.json"),
}

# Hard write-protected PATTERNS for things outside ~/.claude entirely (systemd units, Caddyfile)
# - config-driven (item 1/11), unlike REAL_CLAUDE_HOME/REAL_PROTECTED_FILES above which are not
# security-overridable for the reason explained in the module docstring.
DEFAULT_HARD_PATTERNS = [p for p in gongbang_config.DEFAULTS["hard_protected_patterns"]
                         if "settings" not in p]

# Bash programs whose arguments are ALL touched paths (a delete/truncate touches its target just
# as much as a write does - for self-protect/hard-pattern/WORK_QUEUE purposes, "can this command
# make the protected file disappear or lose its content" matters exactly as much as "can it
# change the content").
# "touch" added 2026-11-02 (Codex P1): `touch W/.wt.done` creates an empty marker just as
# effectively as a redirect - wt-gc.sh's own check is `[ -e .wt.done ]`, content never matters.
_ALL_ARGS_TOUCH = {"rm", "truncate", "shred", "touch"}
# Programs whose arguments are all touched EXCEPT flags, and where BOTH the source(s) and the
# destination matter (moving a hook file's source OUT of the hooks dir disables it exactly as
# effectively as overwriting it in place).
_CP_MV_LIKE = {"cp", "mv", "install", "ln"}
_TEE_LIKE = {"tee"}
_INPLACE_EDIT_PROGRAMS = {"sed", "perl"}


def _patterns(cfg: dict) -> tuple[list[str], list[str]]:
    protected = cfg.get("protected_paths")
    if not isinstance(protected, list) or not protected:
        protected = DEFAULT_PROTECTED
    exceptions = cfg.get("protected_exceptions")
    if not isinstance(exceptions, list):
        exceptions = DEFAULT_EXCEPTIONS
    return [str(p) for p in protected], [str(p) for p in exceptions]


def _hard_patterns(cfg: dict) -> list[str]:
    patterns = cfg.get("hard_protected_patterns")
    if not isinstance(patterns, list) or not patterns:
        patterns = DEFAULT_HARD_PATTERNS
    else:
        patterns = [p for p in patterns if "settings" not in p]
    return [str(p) for p in patterns] or DEFAULT_HARD_PATTERNS


def _path_candidates(raw: str) -> list[str]:
    stripped = raw.strip("'\"")
    expanded = os.path.expanduser(os.path.expandvars(stripped))
    # Normalize a harmless-looking "." path segment ("~/.claude/./settings.json") BEFORE any
    # pattern match runs - otherwise a regex anchored on the real path never fires.
    normalized = os.path.normpath(expanded) if expanded else expanded
    basename = os.path.basename(normalized) or os.path.basename(stripped)
    out = []
    for c in (stripped, expanded, normalized, basename):
        if c and c not in out:
            out.append(c)
    return out


def _matches_any(candidates: list[str], patterns: list[str]) -> str | None:
    for pat in patterns:
        for c in candidates:
            if fnmatch.fnmatch(c, pat):
                return pat
    return None


def check_path(path: str, cfg: dict) -> str | None:
    """Returns the matched protected pattern, or None if `path` is empty, matches no protected
    pattern, or matches one but ALSO matches an exception pattern (exception wins)."""
    if not path:
        return None
    protected, exceptions = _patterns(cfg)
    candidates = _path_candidates(path)
    if _matches_any(candidates, exceptions):
        return None
    return _matches_any(candidates, protected)


def check_command(command: str, cfg: dict) -> str | None:
    if not command:
        return None
    protected, exceptions = _patterns(cfg)
    tokens = _shell._tokenize(command)
    if tokens is None:
        # Malformed quoting: fail CLOSED with a coarse substring/basename check over the raw
        # text rather than silently letting a protected filename through unrecognized.
        for pat in protected:
            if re.search(re.escape(pat).replace(r"\*", ".*"), command):
                return pat
        return None
    for tok in tokens:
        if tok in _shell.SEPARATORS or tok.startswith("-"):
            continue
        candidates = _path_candidates(tok)
        if _matches_any(candidates, exceptions):
            continue
        hit = _matches_any(candidates, protected)
        if hit:
            return hit
    return None


# ---------------------------------------------------------------------------
# Self-protect: the live hooks/gate dir, settings.json, gongbang.config.json, systemd units,
# Caddyfile - never approval-overridable (공백③, items 2/7/18/280).
# ---------------------------------------------------------------------------

def _under(path: str, parent: str) -> bool:
    try:
        real = os.path.realpath(os.path.expanduser(path))
        parent_real = os.path.realpath(os.path.expanduser(parent))
    except Exception:
        return False
    return real == parent_real or real.startswith(parent_real + os.sep)


def check_self_protect(path: str) -> str | None:
    """Protects only the LIVE, REAL (hardcoded - see module docstring) hooks dir, gate dir, and
    control files - a file elsewhere that merely shares a hook's name (a repo copy under
    hooks-src/, a staging folder) is ordinary work, not blocked."""
    if not path:
        return None
    if _under(path, REAL_HOOKS_DIR):
        return "집행 훅 자체는 승인으로도 고치지 않는다"
    if _under(path, REAL_GATE_DIR):
        return "승인 관문 디렉터리는 모델이 손댈 수 없다"
    try:
        real = os.path.realpath(os.path.expanduser(path))
    except Exception:
        real = path
    if real in REAL_PROTECTED_FILES:
        return "harness 설정 파일은 모델이 손댈 수 없다"
    return None


# ---------------------------------------------------------------------------
# Work-copy state files (.wt.json/.wt.json.tmp/.wt.scope/.wt.done) inside /apps/_wt/<app>/
# <name>/ - only wt-new.sh/wt-land.sh/wt-gc.sh (and their shared wt-lib.sh helpers) ever write
# these, never a session's own Bash/Edit/Write/MultiEdit (team-lead instruction 2026-11-02, 사고
# ①: a session hand-wrote a copy's .wt.done, so wt-gc.sh trusted the forged marker and deleted an
# unlanded, uncommitted copy). Never approval-overridable, no owner-script allowlist needed: the
# real wt-*.sh scripts write these files from INSIDE their own subprocess (e.g. `date -Iseconds
# > "$wt/.wt.done"` runs as a line of wt-land.sh, not as a session's own top-level Bash tool call)
# - a hook that only ever sees the top-level Bash/Edit/Write tool call never observes that write,
# so it needs no exception for it. What this DOES catch is a session's own Bash tool call whose
# literal command text directly targets one of these files (`echo ... > .../.wt.done`, `sed -i`,
# `cp`/`mv`/`rm`/`truncate` naming one) - exactly the forging path the incident used.
# ---------------------------------------------------------------------------
WT_ROOT = os.path.normpath(os.path.join(os.environ.get("WT_APPS", "/apps"), "_wt"))
WT_STATE_BASENAMES = {".wt.json", ".wt.json.tmp", ".wt.scope", ".wt.done"}


def check_wt_state_protect(path: str) -> str | None:
    if not path:
        return None
    try:
        real = os.path.realpath(os.path.expanduser(path))
    except Exception:
        real = os.path.normpath(os.path.expanduser(path))
    if not (real == WT_ROOT or real.startswith(WT_ROOT + os.sep)):
        return None
    if os.path.basename(real) in WT_STATE_BASENAMES:
        return "작업 사본 상태 파일(.wt.*)은 wt-new.sh/wt-land.sh/wt-gc.sh만 쓴다 — 손으로 고치지 않는다"
    return None


# ---------------------------------------------------------------------------
# App-source fence (반장 2026-11-03 08:40, WORK_QUEUE 32번): a session (worker or lead) must
# never Edit/Write/MultiEdit/NotebookEdit or Bash-write directly into an app's ORIGINAL source
# tree - only into a work copy (/apps/_wt/<app>/<name>, made by wt-new.sh). Incident this
# closes: 2026-11-03, axos-reliability worker hit the exampleapp copy cap (WT_APP_MAX=3) and,
# instead of waiting or telling the lead, Edited the app folder's own src directly - 11 uncommitted
# files, blocking another branch's merge. scope_fence.py's Part 2 fence only ever triggers inside a
# WT_COPY session (wt-run.sh sets it); a lead-spawned worker session run OUTSIDE a copy (the exact
# shape of this incident) was never covered by anything. This check is NOT conditioned on WT_COPY -
# it fires for every session, always, which is what makes it close the actual gap.
#
# Scope: only an app's deliverable-code subdirectories are fenced (APP_SOURCE_DIRS below) - an
# app's docs/, .gitignore, deploy.env, or any other top-level file/folder is untouched by this
# check (consistent with the team-lead instruction's explicit allow list; those were never the
# incident's problem - hand-editing CURRENT_DECISIONS.md or deploy.env directly in the app folder
# is ordinary, allowed work). "App" means a direct child of /apps/ whose name does not start
# with "_" (so /apps/_wt/**, /apps/_ops/**, /apps/_shared/**, and any other
# underscore-prefixed workspace folder are never in scope here at all - they are not "an app's
# original source").
#
# Off switch: WT_LEAD_DIRECT=1 in the environment - the lead's own deliberate, named exception
# (never a default, never set by a worker) for the rare case a direct edit to an app folder really
# is the right call. No file-based off-switch is given here (unlike scope-fence-off): this is a
# per-invocation env var the lead sets consciously for the one call that needs it, not a standing
# toggle a session could leave on.
# ---------------------------------------------------------------------------
APP_SOURCE_DIRS = {"src", "tests", "scripts", "app", "components", "lib", "prisma", "migrations"}
APPS_ROOT = "/apps"


def _app_source_hit(real_path: str) -> str | None:
    """Returns the app name when `real_path` (already realpath'd/normalized) sits under
    /apps/<app>/<one of APP_SOURCE_DIRS>/ for an <app> not starting with "_" - else None."""
    prefix = APPS_ROOT.rstrip("/") + "/"
    if not real_path.startswith(prefix):
        return None
    rest = real_path[len(prefix):]
    parts = rest.split("/")
    app = parts[0] if parts else ""
    if not app or app.startswith("_"):
        return None
    if len(parts) < 2 or parts[1] not in APP_SOURCE_DIRS:
        return None
    return app


def check_app_source_protect(path: str) -> str | None:
    if not path or os.environ.get("WT_LEAD_DIRECT") == "1":
        return None
    try:
        real = os.path.realpath(os.path.normpath(os.path.expanduser(os.path.expandvars(path))))
    except Exception:
        real = os.path.normpath(os.path.expanduser(path))
    app = _app_source_hit(real)
    if not app:
        return None
    return (f"앱 원본은 고치지 않는다({app}) — 사본 `wt-new.sh {app} <이름>`에서 "
            "(상한이면 반장에게 알리고 기다린다)")


def check_hard_patterns(path: str, cfg: dict) -> str | None:
    """systemd unit files / Caddyfile - matched against every normalized path candidate so a
    "." path segment or quoting cannot defeat the anchor (see module docstring)."""
    if not path:
        return None
    for candidate in _path_candidates(path):
        for pat in _hard_patterns(cfg):
            if re.search(pat, candidate):
                return pat
    return None


def _touched_write_paths(command: str) -> list[str]:
    """Every path-like argument in `command` that a WRITE, DELETE, or TRUNCATE could reach, after
    fully unwrapping separators/substitution/nested-shells via `_shell.all_segments()` - so a
    protected target hidden behind `bash -c`/`env -S`/`$(...)` is still caught (fixes the Codex-
    found `bash -c 'echo {} > ~/.claude/settings.json'` bypass). Used only for the self-protect /
    hard-pattern / WT-state / WORK_QUEUE checks below (always-on, no override) - the secret-file
    glob scan in check_command() above already scans every token of the OUTER command regardless.

    A segment that is, on its own, an installer invocation (`_is_installer_segment`) contributes
    NO targets - but this is scoped to that ONE segment, never the whole command, so a sibling
    segment smuggled in alongside it (`bash apply_hooks_v3.sh; touch W/.wt.done`) is still scanned
    (Codex P1 2026-11-02: the old whole-command `_runs_allowlisted_installer` short-circuit in
    `_run()` used to exempt that sibling too)."""
    out: list[str] = []
    for seg in _shell.all_segments(command):
        if _is_installer_segment(seg):
            continue
        for i, t in enumerate(seg):
            # `>|` (noclobber-override redirect) added 2026-11-02 (Codex P1): tokenizes as its
            # own single punctuation token, same as `>`/`>>` - previously unrecognized entirely.
            if t in (">", ">>", ">|") and i + 1 < len(seg):
                out.append(seg[i + 1])
        name, idx = _shell._first_command(seg)
        if idx < 0:
            continue
        rest = seg[idx + 1:]
        positional = _shell._positional_args(rest)
        if name in _ALL_ARGS_TOUCH:
            out.extend(positional)
        elif name in _CP_MV_LIKE or name in _TEE_LIKE:
            out.extend(positional)  # source(s) AND destination all matter - see module docstring
        elif name in _INPLACE_EDIT_PROGRAMS and any(a == "-i" or a.startswith("-i") for a in rest):
            out.extend(positional)
    return out


# ---------------------------------------------------------------------------
# WORK_QUEUE.md protection (items 19/281): ANY Bash write/delete/truncate touching it is denied
# outright - see module docstring for why the old "[x]"-text-matching approach was dropped.
# ---------------------------------------------------------------------------

def _count_x_marks(text: str) -> int:
    return text.count("[x]")


def _read_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _apply_edit(content: str, old: str, new: str, replace_all: bool) -> str:
    if not old:
        return content
    count = -1 if replace_all else 1
    return content.replace(old, new, count)


def _is_work_queue_path(path: str, cfg: dict) -> bool:
    wq = cfg.get("work_queue_path") or ""
    basename = os.path.basename(wq) if wq else "WORK_QUEUE.md"
    try:
        candidate_basename = os.path.basename(os.path.normpath(os.path.expanduser(os.path.expandvars(path))))
    except Exception:
        candidate_basename = os.path.basename(path)
    return candidate_basename == basename


def check_work_queue_edit(tool: str, inp: dict, cfg: dict) -> bool:
    """Returns True if applying this Edit/Write/MultiEdit would INCREASE the number of "[x]"
    marks in the target file compared to what's on disk now (catches a split old/new_string
    boundary, e.g. old=" ] TASK1", new="x] TASK1"). Edit/Write/MultiEdit go through a different,
    content-aware check than Bash (check_work_queue_bash below) because the harness's own tool
    call already gives us the exact before/after content to compare - a Bash command's effect on
    disk is not similarly knowable in advance, so that path is denied outright instead (see
    check_work_queue_bash)."""
    path = str(inp.get("file_path", ""))
    if not _is_work_queue_path(path, cfg):
        return False
    original = _read_file(path)
    if tool == "Write":
        new_content = str(inp.get("content", ""))
    elif tool == "Edit":
        new_content = _apply_edit(
            original, str(inp.get("old_string", "")), str(inp.get("new_string", "")),
            bool(inp.get("replace_all", False)),
        )
    elif tool == "MultiEdit":
        new_content = original
        for e in inp.get("edits") or []:
            if not isinstance(e, dict):
                continue
            new_content = _apply_edit(
                new_content, str(e.get("old_string", "")), str(e.get("new_string", "")),
                bool(e.get("replace_all", False)),
            )
    else:
        return False
    return _count_x_marks(new_content) > _count_x_marks(original)


def check_work_queue_bash(command: str, cfg: dict) -> bool:
    """True if `command` writes/deletes/truncates WORK_QUEUE.md through ANY Bash means, after
    unwrapping separators/pipes/substitution/nested-shells - deliberately NOT conditioned on the
    command containing the literal text "[x]" (see module docstring: that check was bypassed by
    both a pipe into `tee` and a `sed -i` that builds the mark from two separate pieces). Marking
    a task done is work_claim.py's job alone - there is no legitimate reason for an agent's Bash
    call to touch this file by any other means at all."""
    for target in _touched_write_paths(command):
        stripped = target.strip("'\"")
        expanded = os.path.expanduser(os.path.expandvars(stripped))
        if _is_work_queue_path(expanded, cfg):
            return True
    return False


def deny(reason: str) -> int:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


def _run(data: dict, cfg: dict) -> int:
    tool = data.get("tool_name", "")
    inp = data.get("tool_input", {}) or {}

    path = str(inp.get(PATH_TOOLS[tool], "")) if tool in PATH_TOOLS else ""
    command = str(inp.get("command", "")) if tool == "Bash" else ""

    if path:
        # Self-protect (hooks/gate dir, control files) and the hard write-protected patterns
        # (systemd/Caddyfile) are a WRITE-time check only - a plain Read of a hook's own source
        # (routine review work) is not blocked, same convention scope_guard.py's GATED_PATH
        # already uses ("읽기는 허용됨").
        if tool in WRITE_TOOLS:
            always = (check_self_protect(path) or check_hard_patterns(path, cfg)
                      or check_wt_state_protect(path) or check_app_source_protect(path))
            if always:
                return deny(f"보호 대상 경로 — {always}.")
        if tool in ("Edit", "Write", "MultiEdit") and check_work_queue_edit(tool, inp, cfg):
            return deny("작업판(WORK_QUEUE.md) 끝남 표시([x])는 손으로 쓰지 않는다 — `python3 _ops/tools/work_claim.py done --line N --by <이름> --evidence \"<증거>\"`로만(항목 추가·문구 수정은 Edit로 가능).")
        hit = check_path(path, cfg)
        if hit:
            return deny(f"보호 대상 파일(패턴 {hit}) — 비밀값·키 파일은 읽거나 쓰지 않는다.")
        return 0

    if command:
        if _runs_allowlisted_installer(command):
            return 0
        cwd = str(data.get("cwd", "") or "")
        for target in _touched_write_paths(command):
            stripped = target.strip("'\"")
            expanded = os.path.expanduser(os.path.expandvars(stripped))
            # A relative write target (`cd ~/.claude/hooks && echo x > evil.py`) must be
            # resolved against the TOOL CALL's own cwd, not this hook subprocess's unrelated cwd.
            resolved = expanded if os.path.isabs(expanded) else os.path.join(cwd or ".", expanded)
            always = (check_self_protect(resolved) or check_hard_patterns(resolved, cfg)
                      or check_wt_state_protect(resolved) or check_app_source_protect(resolved))
            if always:
                return deny(f"보호 대상 경로 — {always}.")
        if check_work_queue_bash(command, cfg):
            return deny("작업판(WORK_QUEUE.md)은 Bash(sed·echo·tee 등)로 쓰지 않는다 — 항목 추가·수정은 Edit 도구로, 완료 표시([x])는 work_claim.py로만.")
        hit = check_command(command, cfg)
        if hit:
            return deny(f"보호 대상 파일(패턴 {hit}) — 비밀값·키 파일은 읽거나 쓰지 않는다.")
        return 0

    return 0


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception as exc:
        return deny(f"입력 파싱 실패로 차단(안전 쪽으로): {type(exc).__name__}")
    try:
        cfg = gongbang_config.load()
        return _run(data, cfg)
    except Exception as exc:  # fail-closed: an internal error must never look like "allow"
        return deny(f"내부 오류로 차단(안전 쪽으로): {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
