#!/usr/bin/env python3
"""Shared approval-parsing + transcript-reading logic for approve_gate.py and scope_guard.py.

The approval gate is not a JSON file that a Bash/Edit/Write call could forge. A path-string
blocklist on a writable file cannot close that hole, so instead scope_guard.py re-derives "is
the gate open for THIS session" straight from the owner's own chat transcript for THIS
session_id (cfg["transcripts_root"]/*/<session_id>.jsonl - a file only the agent harness
itself appends to) and replays every `승인 <TASK>` / `승인 취소` the owner actually typed in
the last cfg["approval_window_seconds"] (default 4h), in the order it was typed.
approve_gate.py does not decide or persist anything; it only echoes back what it recognized in
the prompt just submitted, as feedback for the owner.

This removes the whole class of "forge the gate file" attacks. It does not make the fence
perfect - see README.md "알려진 한계" for what is still open. The approval is scoped to THIS
SESSION ONLY - a TASK name is a human-readable label, not a scope: approving under any one
name opens every gated action for the rest of the session, not just ones related to that name.
See README.md.

Every environment-specific value this module needs (transcripts_root, approval_window_seconds,
extra_protected_hook_names) comes from the caller's `cfg` (gongbang_config.load()) - nothing
here is hardcoded to a real person's home directory or a specific server path.
"""
from __future__ import annotations

import glob
import json
import os
import re
import time
from datetime import datetime

# Read backward in 1MB chunks until a complete line's timestamp is older than the window, up
# to a 64MB hard cap - a flat "last N bytes" read could otherwise drop a still-valid approval
# behind a wall of big tool output. If the cap is hit first, judgment is made on whatever was
# read, which can mean missing an older-but-still-valid approval and denying something that
# should have been allowed (see README "알려진 한계").
TAIL_CHUNK_BYTES = 1 * 1024 * 1024
MAX_TAIL_BYTES = 64 * 1024 * 1024

NAME_RE = r"[A-Za-z0-9가-힣_.-]{1,40}"
# A "승인 <name>" only counts at the very start of the message, or right after a comma/newline.
OCCURRENCE_RE = re.compile(r"(?:^|[,\n])\s*승인\s+(" + NAME_RE + r")")
CLOSE_WORDS = {"취소", "해제"}

# Negation words that can follow "승인" directly (with or without a space) and mean the owner
# is NOT approving anything. Exact multi-word phrases WITH an internal space go here (the
# stem-regex list below handles a negation stem plus any Korean inflectional ending, e.g.
# "안합니다", "말고요", "하지마세요").
NEGATIONS = [
    "하지말고", "하지마", "하지않음", "하지 않음", "안함", "안 함", "하지말", "하지",
    "말고", "말자", "말", "금지", "않음", "않", "안",
    "거부", "불가", "보류", "취소해",
]

# A negation STEM followed by ANY inflectional ending still means "don't approve" - "안합니다",
# "안 할게", "하지 마세요", "말아주세요". This deliberately trades precision for safety: a word
# that happens to START with a negation syllable (e.g. "안내문", "안전") now also gets flagged
# as a negated approval-adjacent word when it immediately follows a "승인 <name>". That is the
# SAFE direction for approval-recognition specifically (worst case: a real approval attempt is
# not recognized and the gate stays closed, which just means re-typing "승인 X" - unlike
# over-blocking a Bash command, which stops real work).
NEGATION_STEM_RES = [re.compile(p) for p in (
    r"^안\w*$", r"^하지\s*마\w*$", r"^하지\w*$", r"^말\w*$", r"^말고\w*$",
    r"^금지\w*$", r"^않\w*$", r"^거부\w*$", r"^불가\w*$", r"^보류\w*$", r"^취소\w*$",
)]


def _word_is_negation(word: str) -> bool:
    w = word.rstrip(",.!?~")
    if not w:
        return False
    if w in NEGATIONS:
        return True
    return any(r.match(w) for r in NEGATION_STEM_RES)


# Hook/script basenames (this repo's own hooks, no extension needed) that must never be
# touched/forged by the model INSIDE the real installed hooks/gate directories (see
# scope_guard.always_block_hit - this list alone does not decide anything; scope_guard also
# requires the path/command to actually point at the configured installed location before
# treating a bare mention of one of these names as protected). Matched as a bare word
# (hyphen or underscore variants). cfg["extra_protected_hook_names"] (gongbang_config.py) can
# add site-local names on top of this built-in list.
PROTECTED_HOOK_NAMES = [
    "approve-gate", "approve_gate",
    "scope-guard", "scope_guard",
    "fence-parse", "fence_parse",
    "verify-claims", "verify_claims",
    "preflight-gate", "preflight_gate",
    "block-dangerous", "block_dangerous",
    "lesson-capture", "lesson_capture",
    "lesson-start", "lesson_start",
    "lessons-store", "lessons_store",
    "library-nudge", "library_nudge",
    "gongbang-config", "gongbang_config",
    "next-task", "next_task",
]


def hook_name_pattern(extra_names: list[str] | None = None) -> re.Pattern:
    names = list(PROTECTED_HOOK_NAMES) + [str(n) for n in (extra_names or []) if n]
    return re.compile(r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\b", re.I)


# Built-in-list-only pattern, for callers (or tests) that don't need the configurable
# extension. scope_guard.py always calls hook_name_pattern(cfg["extra_protected_hook_names"])
# instead so a site-local addition is honored.
HOOK_NAME_RE = hook_name_pattern()

# The (retired, file-based) gate directory convention, wherever it still appears - defense in
# depth for an install that is mid-upgrade from an older, file-based version of this fence.
# This is a NAMING convention check only; scope_guard.always_block_hit additionally requires
# the command/path to resolve under the configured legacy_gate_dir before treating this as the
# real thing, not just a coincidentally-named folder elsewhere.
GATE_DIR_RE = re.compile(r"gate[/\\]approved\b", re.I)


# ---------------------------------------------------------------------------
# Approval-syntax parsing
# ---------------------------------------------------------------------------

_TRIPLE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_TILDE_FENCE_RE = re.compile(r"~~~.*?~~~", re.DOTALL)
# Markdown indented code (4 spaces or a tab at line start) is code as well.
_INDENTED_CODE_RE = re.compile(r"(?m)^(?: {4}|\t).*$")
_BACKTICK_RE = re.compile(r"`[^`\n]{0,200}`")
_DQUOTE_RE = re.compile(r'"[^"\n]{0,200}"')
_SQUOTE_RE = re.compile(r"'[^'\n]{0,200}'")
_CURLY_DQUOTE_RE = re.compile(r"“[^”\n]{0,200}”")
_CURLY_SQUOTE_RE = re.compile(r"‘[^’\n]{0,200}’")


def strip_quotes_and_code(text: str) -> str:
    """Remove code fences and quoted spans so an approval mentioned only inside a quote or a
    code block (e.g. "'승인 X'를 입력하면...") can never be recognized as a real approval."""
    out = _TRIPLE_FENCE_RE.sub(" ", text)
    out = _TILDE_FENCE_RE.sub(" ", out)
    out = _INDENTED_CODE_RE.sub(" ", out)
    out = _BACKTICK_RE.sub(" ", out)
    out = _DQUOTE_RE.sub(" ", out)
    out = _SQUOTE_RE.sub(" ", out)
    out = _CURLY_DQUOTE_RE.sub(" ", out)
    out = _CURLY_SQUOTE_RE.sub(" ", out)
    return out


def _is_negated(cleaned: str, match: "re.Match[str]") -> bool:
    """True if the captured name is itself a negation word/stem ("안합니다"), or the word(s)
    right after the name form one (<TASK> + "하지 마" -> negated; <TASK> + "안 할게" -> negated
    via the first word "안" alone, which is already a full stem match)."""
    name = match.group(1)
    if _word_is_negation(name):
        return True
    tail = cleaned[match.end():match.end() + 20].lstrip()
    words = tail.split()
    if not words:
        return False
    w1 = words[0].rstrip(",.!?~")
    if _word_is_negation(w1):
        return True
    if len(words) > 1:
        w2 = words[1].rstrip(",.!?~")
        if f"{w1} {w2}" in NEGATIONS or any(r.match(f"{w1} {w2}") for r in NEGATION_STEM_RES):
            return True
        if _word_is_negation(w1 + w2):
            return True
    return False


# OCCURRENCE_RE's "(?:^|[,\n])\s*" would otherwise try matching at EVERY position inside a long
# run of separator/whitespace characters, with \s* re-scanning the same run from each of those
# positions - quadratic on a message with a long run of newlines. Collapsing any run of 2+
# whitespace characters that contains a newline down to a single newline before matching
# removes the long run entirely (bounded \s* afterward), without changing which "승인 X"
# occurrences are recognized (a run of blank lines before "승인" is still "right after a
# newline" either way).
_BLANK_RUN_RE = re.compile(r"[ \t\r]*\n[ \t\r\n]*")


def _collapse_separator_runs(text: str) -> str:
    return _BLANK_RUN_RE.sub("\n", text)


def parse_events(text: str) -> tuple[list[tuple[str, str | None]], list[str]]:
    """Returns (events, rejected): events is [("open", name) | ("close", None), ...] in the
    order they appear in the (quote/code-stripped) text; rejected is the list of names that
    looked like "승인 X" but were negated (for user-facing feedback only, not for gating)."""
    cleaned = strip_quotes_and_code(text)
    cleaned = _collapse_separator_runs(cleaned)
    events: list[tuple[str, str | None]] = []
    rejected: list[str] = []
    for m in OCCURRENCE_RE.finditer(cleaned):
        name = m.group(1).rstrip(",.")
        if not name:
            continue
        if name in CLOSE_WORDS:
            events.append(("close", None))
            continue
        if _is_negated(cleaned, m):
            rejected.append(name)
            continue
        events.append(("open", name))
    return events, rejected


def replay(messages: list[tuple[float, str]]) -> set[str]:
    """messages: [(unix_ts, text), ...], any order. Replays every approval event across all
    messages in chronological (timestamp, then insertion) order, so a single message like
    "승인 전체, 승인 취소" ends up closed (the cancel comes after, in text order). Returns the
    set of task names open at the end ("전체" is just a name like any other - see README
    "알려진 한계": a TASK name is a label, not a scope)."""
    open_tasks: set[str] = set()
    for _, text in sorted(messages, key=lambda m: m[0]):
        events, _rejected = parse_events(text)
        for kind, name in events:
            if kind == "close":
                open_tasks.clear()
            else:
                open_tasks.add(name)
    return open_tasks


# ---------------------------------------------------------------------------
# Transcript reading
# ---------------------------------------------------------------------------

_SYSTEM_WRAPPER_PREFIXES = ("<", "[SYSTEM NOTIFICATION")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


def _parse_ts(ts: str) -> float | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def is_human_turn_message(rec: dict) -> bool:
    """True if `rec` is a transcript line the owner themself typed (origin.kind == "human"),
    as opposed to a peer/subagent relay message or a background notification that is also
    stored as a type=="user" line. Shared by verify_claims.py and preflight_gate.py to decide
    where one turn ends and the next begins - a peer reply or a queued notification arriving
    mid-turn must never be mistaken for a new human turn and reset the evidence collected so
    far in the current one."""
    if rec.get("type") != "user":
        return False
    if rec.get("isMeta") or rec.get("isSidechain") or rec.get("isCompactSummary"):
        return False
    origin = rec.get("origin")
    return isinstance(origin, dict) and origin.get("kind") == "human"


def _extract_user_text(obj: dict) -> str | None:
    """Returns the human-authored text of a {"type":"user", ...} transcript line, or None if
    this line is not a genuine human chat message (tool_result wrapper, meta/sidechain/compact
    summary line, or a system-wrapper block like <system-reminder>)."""
    if obj.get("isMeta") or obj.get("isSidechain") or obj.get("isCompactSummary"):
        return None
    # Only lines the harness marks as typed by a person count. Messages relayed from subagents or
    # other sessions are also stored as "user" lines but carry origin.kind == "peer". A line without
    # an origin is not trusted.
    origin = obj.get("origin")
    if not isinstance(origin, dict) or origin.get("kind") != "human":
        return None
    message = obj.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        texts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                return None
            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif block_type == "image":
                continue  # an image attached alongside text is still a plain human message
            else:
                return None  # tool_result or anything else -> not a plain human message
        text = "\n".join(texts)
    else:
        return None
    if text.lstrip().startswith(_SYSTEM_WRAPPER_PREFIXES):
        return None
    return text


def _extract_attachment_text(obj: dict) -> str | None:
    """Returns the prompt text of a human-originated queued_command attachment, or None."""
    att = obj.get("attachment")
    if not isinstance(att, dict):
        return None
    if att.get("type") != "queued_command" or att.get("commandMode") != "prompt":
        return None
    origin = att.get("origin")
    if not isinstance(origin, dict) or origin.get("kind") != "human":
        return None
    prompt = att.get("prompt")
    return prompt if isinstance(prompt, str) else None


def find_transcript_path(session_id: str, transcripts_root: str) -> str | None:
    """Finds <transcripts_root>/*/<session_id>.jsonl. Returns None unless the session_id is a
    safe token AND exactly one file matches (missing or ambiguous -> caller fails closed)."""
    if not session_id or not _SESSION_ID_RE.match(session_id):
        return None
    pattern = os.path.join(transcripts_root, "*", session_id + ".jsonl")
    matches = glob.glob(pattern)
    if len(matches) != 1:
        return None
    return matches[0]


def _read_tail_window(path: str, cutoff: float) -> bytes:
    """Reads backward from the end of `path` in TAIL_CHUNK_BYTES chunks until a complete
    line's timestamp is older than `cutoff`, or MAX_TAIL_BYTES has been read, or the start of
    the file is reached. Drops the leading partial line (left over from seeking mid-file).
    Tolerant of the file growing/shrinking while we read it (each read is bounded by an
    explicit size, never a size-less f.read())."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        buffer = b""
        total_read = 0
        while pos > 0 and total_read < MAX_TAIL_BYTES:
            chunk_size = min(TAIL_CHUNK_BYTES, pos)
            pos -= chunk_size
            f.seek(pos)
            chunk = f.read(chunk_size)
            total_read += len(chunk)
            buffer = chunk + buffer
            nl = buffer.find(b"\n")
            if pos > 0 and nl == -1:
                continue  # no complete line boundary yet; keep reading further back
            candidate = buffer[nl + 1:] if (pos > 0 and nl != -1) else buffer
            nl2 = candidate.find(b"\n")
            candidate_line = candidate[:nl2] if nl2 != -1 else candidate
            try:
                obj = json.loads(candidate_line.decode("utf-8", errors="strict"))
                ts = _parse_ts(str(obj.get("timestamp", "")))
            except Exception:
                ts = None
            if ts is not None and ts < cutoff:
                break
        if pos > 0:
            nl = buffer.find(b"\n")
            buffer = buffer[nl + 1:] if nl != -1 else b""
    return buffer


def read_recent_human_messages(session_id: str, cfg: dict, now: float | None = None) -> list[tuple[float, str]] | None:
    """Returns [(unix_ts, text), ...] for human chat messages / queued human prompts in the
    last cfg["approval_window_seconds"]. Reads backward in TAIL_CHUNK_BYTES chunks (up to
    MAX_TAIL_BYTES) so a big block of recent tool output doesn't push an older-but-still-valid
    approval out of view. Returns None (the fail-closed signal) if the transcript cannot be
    found or read.

    `now` defaults to the FENCE_NOW environment variable if set (test-only time injection, not
    a path - see README), else real wall-clock time."""
    path = find_transcript_path(session_id, cfg["transcripts_root"])
    if path is None:
        return None
    if now is None:
        env_now = os.environ.get("FENCE_NOW")
        now = float(env_now) if env_now else time.time()
    window = float(cfg.get("approval_window_seconds") or 4 * 3600)
    cutoff = now - window
    try:
        raw = _read_tail_window(path, cutoff)
    except Exception:
        return None

    messages: list[tuple[float, str]] = []
    for raw_line in raw.split(b"\n"):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            obj = json.loads(raw_line.decode("utf-8", errors="strict"))
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        ts = _parse_ts(str(obj.get("timestamp", "")))
        if ts is None or ts < cutoff:
            continue
        otype = obj.get("type")
        text: str | None = None
        if otype == "user":
            text = _extract_user_text(obj)
        elif otype == "attachment":
            text = _extract_attachment_text(obj)
        if text:
            messages.append((ts, text))
    return messages


def compute_open_tasks(session_id: str, cfg: dict, now: float | None = None) -> tuple[bool, set[str]]:
    """Returns (ok, open_tasks). ok=False means the transcript could not be found or read, and
    the caller MUST treat the gate as closed (fail-closed), not open."""
    messages = read_recent_human_messages(session_id, cfg, now=now)
    if messages is None:
        return False, set()
    return True, replay(messages)
