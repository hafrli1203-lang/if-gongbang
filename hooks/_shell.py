#!/usr/bin/env python3
# Generated from the private ops workspace at b94af62b97af64e2e58e8c102fa2538f066ae366; do not edit by hand (see harness/export_ops_public.py).
"""Shared, hardened shell-command analysis helpers for block_dangerous.py and
protect_sensitive_files.py. Not a hook itself - never registered in settings.json.

Added after a Codex read-only adversarial review of the first version of this bundle (v2 gap
bundle 1, 2026-11-02) found real bypasses in the simpler, non-shared tokenizer each hook had
started with:
  - a literal newline between two commands (`printf ok\\npkill node`) was treated as plain
    whitespace, not a command separator, so only the FIRST line's command was ever checked;
  - `bash -lc '...'` (a combined short-option cluster ending in "c") was not recognized as the
    shell's "run this string" flag - only a bare "-c" token was;
  - `env -S '...'`/`env --split-string '...'` opaquely consumed its own argument as "an option
    value" without ever analyzing the command string packed inside it;
  - `$(...)`/backtick command substitution was only unwrapped ONE level deep, so
    `$(echo $(pkill node))` was never re-examined for its own, deeper substitution.

`all_segments()` below is the single place that now does all of this - both hooks build their
decision ONLY from its output, so a future fix to any one of these evasions never needs to be
made twice.
"""
from __future__ import annotations

import re
import shlex

SEPARATORS = {";", "&&", "||", "|", "&"}
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
NESTED_SHELLS = {"bash", "sh", "zsh", "dash", "ash"}
# A combined short-option cluster that includes "c" (bash's "run the next arg as a command
# string" flag) - "-c", "-lc", "-ic", "-xc", ... A long option ("--foo") never matches.
_SHELL_DASH_C_RE = re.compile(r"^-[a-zA-Z]*c[a-zA-Z]*$")

GIT_GLOBAL_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}

_PREFIX_VALUE_OPTS = {
    "sudo": {"-u", "-g", "-U", "-C", "-D", "--user", "--group", "--close-from", "--chdir"},
    "env": {"-u", "-C", "-P", "--unset", "--chdir"},  # "-S"/"--split-string" handled separately below
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "-P", "-u", "--class", "--classdata"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
}
_PREFIX_POSITIONAL = {"timeout": 1, "chrt": 1, "taskset": 1}
_PLAIN_PREFIXES = {"sudo", "exec", "xargs", "nohup", "time", "command"}
_CHAIN_PREFIXES = _PLAIN_PREFIXES | set(_PREFIX_VALUE_OPTS) | set(_PREFIX_POSITIONAL)

MAX_SUBSTITUTION_DEPTH = 4


def _basename(tok: str) -> str:
    return tok.rsplit("/", 1)[-1]


def _tokenize(command: str) -> list[str] | None:
    # A literal newline is an ordinary command separator in a real shell, not whitespace a
    # command can hide behind - normalize it to "; " BEFORE shlex ever sees it, so the segment
    # split below actually happens. Over-triggering on a newline that happens to sit inside a
    # quoted multi-line string is an acceptable false-positive direction for a safety hook
    # ("안전 쪽으로") - it can only make this hook MORE cautious, never less.
    command = command.replace("\n", " ; ").replace("\r", " ; ")
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except Exception:
        return None


def _split_segments(tokens: list[str]) -> list[list[str]]:
    segs: list[list[str]] = []
    cur: list[str] = []
    for t in tokens:
        if t in SEPARATORS:
            segs.append(cur)
            cur = []
        else:
            cur.append(t)
    segs.append(cur)
    return [s for s in segs if s]


def _skip_one_prefix(tokens: list[str], i: int) -> int:
    name = _basename(tokens[i])
    value_opts = _PREFIX_VALUE_OPTS.get(name, set())
    j = i + 1
    while j < len(tokens):
        t = tokens[j]
        if ASSIGN_RE.match(t):
            j += 1
            continue
        if t == "--":
            j += 1
            break
        if not t.startswith("-") or t == "-":
            break
        j += 1
        if t in value_opts or any(t.startswith(o + "=") for o in value_opts if o.startswith("--")):
            if t in value_opts:
                j += 1
    for _ in range(_PREFIX_POSITIONAL.get(name, 0)):
        if j < len(tokens) and not tokens[j].startswith("-"):
            j += 1
    return j


def _first_command(tokens: list[str]) -> tuple[str | None, int]:
    """Skips leading VAR=value assignments and a whole CHAIN of command-runner prefixes (sudo/
    env/nice/ionice/timeout/stdbuf/xargs/nohup/exec/command/time/chrt/taskset), returns
    (basename of the real command token, its index), or (None, -1)."""
    i = 0
    while i < len(tokens) and ASSIGN_RE.match(tokens[i]):
        i += 1
    guard = 0
    while i < len(tokens) and _basename(tokens[i]) in _CHAIN_PREFIXES and guard < 16:
        i = _skip_one_prefix(tokens, i)
        while i < len(tokens) and ASSIGN_RE.match(tokens[i]):
            i += 1
        guard += 1
    if i >= len(tokens):
        return None, -1
    return _basename(tokens[i]), i


def _flag_set(tokens: list[str]) -> set[str]:
    flags: set[str] = set()
    for t in tokens:
        if t == "--":
            break
        if t.startswith("--"):
            flags.add(t[2:].split("=", 1)[0])
        elif t.startswith("-") and len(t) > 1:
            flags.update(t[1:])
    return flags


def _positional_args(tokens: list[str]) -> list[str]:
    out = []
    stop = False
    for t in tokens:
        if t == "--":
            stop = True
            continue
        if not stop and t.startswith("-") and len(t) > 1:
            continue
        out.append(t)
    return out


def git_subcommand_and_rest(segment: list[str]) -> tuple[str, list[str]] | None:
    name, i = _first_command(segment)
    if name != "git":
        return None
    j = i + 1
    while j < len(segment):
        t = segment[j]
        if t in GIT_GLOBAL_OPTS_WITH_VALUE:
            j += 2
            continue
        if any(t.startswith(opt + "=") for opt in GIT_GLOBAL_OPTS_WITH_VALUE):
            j += 1
            continue
        if t.startswith("-"):
            j += 1
            continue
        break
    if j >= len(segment):
        return None
    return segment[j], segment[j + 1:]


def _nested_inner_commands(segment: list[str]) -> list[str]:
    """Inner command STRINGS hidden behind this segment, to be re-tokenized and re-analyzed as
    if they were their own top-level command: `bash -c '...'` (any combined short-option
    cluster containing "c" - "-c", "-lc", "-ic", ...), and `env -S '...'`/
    `env --split-string='...'` (env's own "run this packed string" feature).

    Unlike _first_command (which skips the WHOLE prefix chain to find the terminal command, for
    checks like rm/git detection that only care about what finally runs), this walks the chain
    ONE LINK AT A TIME so that `sudo env -S '...'` or `nice -n 5 bash -c '...'` still finds the
    shell/env special case even though it is not the first token - a prefix that is neither a
    shell nor env is simply skipped and the walk continues down the chain."""
    i = 0
    while i < len(segment) and ASSIGN_RE.match(segment[i]):
        i += 1
    guard = 0
    while i < len(segment) and guard < 16:
        name = _basename(segment[i])
        rest = segment[i + 1:]
        if name in NESTED_SHELLS:
            for idx, t in enumerate(rest):
                if _SHELL_DASH_C_RE.match(t):
                    return [rest[idx + 1]] if idx + 1 < len(rest) else []
            return []
        if name == "env":
            for idx, t in enumerate(rest):
                if t in ("-S", "--split-string"):
                    return [rest[idx + 1]] if idx + 1 < len(rest) else []
                if t.startswith("--split-string="):
                    return [t.split("=", 1)[1]]
            # env had no -S/--split-string: fall through, skip it as an ordinary prefix below.
        if name not in _CHAIN_PREFIXES:
            return []
        new_i = _skip_one_prefix(segment, i)
        if new_i <= i:
            return []
        i = new_i
        while i < len(segment) and ASSIGN_RE.match(segment[i]):
            i += 1
        guard += 1
    return []


_SUBSTITUTION_RE = re.compile(r"\$\(([^()]*(?:\([^()]*\)[^()]*)*)\)|`([^`]*)`")


def _extract_substitutions(text: str) -> list[str]:
    out = []
    for m in _SUBSTITUTION_RE.finditer(text):
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        if inner:
            out.append(inner)
    return out


# Redirection operators shlex's punctuation_chars splits out as their own tokens (`2>/dev/null`
# -> "2", ">", "/dev/null"). _strip_redirections() below removes these triples/pairs from a
# token list before it is read as positional arguments, so a stderr/stdout redirect after a
# command's real arguments is never mistaken for one more argument (Codex 11-02: `rm -rf a b
# 2>/dev/null` read "2" as a THIRD delete target and refused the command outright).
_REDIR_OPS = {"<", ">", ">>", "<<", "<<<", ">&", "&>", "<&", "<>", ">>&"}


def _strip_redirections(tokens: list[str]) -> list[str]:
    out: list[str] = []
    i, n = 0, len(tokens)
    while i < n:
        t = tokens[i]
        if t.isdigit() and i + 1 < n and tokens[i + 1] in _REDIR_OPS:
            i += 2
            if i < n:   # the redirect's target (a filename, or "&1"/"2" for a duplicated fd)
                i += 1
            continue
        if t in _REDIR_OPS:
            i += 1
            if i < n:
                i += 1
            continue
        out.append(t)
        i += 1
    return out


def all_segments(command: str, _depth: int = 0) -> list[list[str]]:
    """Every shell segment that could actually run as its own command, after fully unwrapping:
    newline/`;`/`&&`/`||`/`|`/`&` separators, `$(...)`/backtick command substitution (recursively
    - a substitution INSIDE a substitution is unwrapped too, up to MAX_SUBSTITUTION_DEPTH), and a
    nested-shell or `env -S` string (also recursively). Best-effort: a piece that fails to
    tokenize is simply skipped, never treated as an error for the rest of the command."""
    if _depth > MAX_SUBSTITUTION_DEPTH:
        return []
    tokens = _tokenize(command)
    if tokens is None:
        return []
    segments = _split_segments(tokens)
    out = list(segments)
    for inner_text in _extract_substitutions(command):
        out.extend(all_segments(inner_text, _depth + 1))
    for seg in segments:
        for inner_cmd in _nested_inner_commands(seg):
            out.extend(all_segments(inner_cmd, _depth + 1))
    return out


def nested_segments(command: str, _depth: int = 0) -> list[list[str]]:
    """Every segment HIDDEN behind substitution/nested-shell/`env -S` unwrapping - never the
    literal top-level segments `_split_segments` finds at depth 0 (a caller that already walked
    those in execution order with cd-tracking, as block_dangerous.structural_block's pass 1 does,
    must not have that tracking defeated by a second pass that re-checks the same top-level
    segments against the ORIGINAL cwd - Codex 11-02: `cd /apps/<app> && git checkout -- f`
    run from a work-copy cwd was wrongly blocked because the all_segments() second pass re-judged
    the `git checkout` segment against the un-cd'ed original cwd, undoing pass 1's tracking)."""
    if _depth > MAX_SUBSTITUTION_DEPTH:
        return []
    tokens = _tokenize(command)
    if tokens is None:
        return []
    segments = _split_segments(tokens)
    out: list[list[str]] = []
    for inner_text in _extract_substitutions(command):
        out.extend(all_segments(inner_text, _depth + 1))
    for seg in segments:
        for inner_cmd in _nested_inner_commands(seg):
            out.extend(all_segments(inner_cmd, _depth + 1))
    return out


__all__ = [
    "SEPARATORS", "GIT_GLOBAL_OPTS_WITH_VALUE", "_tokenize", "_split_segments", "_first_command",
    "_flag_set", "_positional_args", "_strip_redirections", "git_subcommand_and_rest",
    "all_segments", "nested_segments",
]
