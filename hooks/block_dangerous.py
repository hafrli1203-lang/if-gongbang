#!/usr/bin/env python3
"""Safety hook: blocks outright-destructive Bash commands, regardless of the approval gate.

PreToolUse hook - exit 0: allow, exit 2: block. This is the last line of defense for
commands that should never run from an agent session at all (as opposed to scope_guard.py,
which gates reversible-with-approval actions behind "승인 <TASK>").

A plain regex over `git push origin main --force` matches, but reordered or split options
like `git -C . push --force`, `git -c x=y push`, `rm -f -r /target` or `rm -fr /target` would
not (the literal substring never appears). structural_block() below reuses scope_guard.py's
own shell tokenizer/segment-splitter/first-command finder (one parser shared by both hooks -
see scope_guard.py's module docstring) to walk git/rm's actual option grammar, so option order
and splitting cannot change the answer. The plain regex list stays as a second, independent
layer for everything else (DB commands, mkfs, dd, chmod, curl|sh, secret echo, process kills),
where option-reordering is not a realistic concern.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scope_guard as _sg  # noqa: E402 - shares the one shell tokenizer/parser, not a duplicate

BLOCKED_PATTERNS = [
    # Destructive file operations (plain-regex layer; structural_block() also catches the
    # option-reordered forms of the rm -rf cases below).
    #
    # Only true catastrophic, top-level targets are hard-blocked here (a bare root or a bare
    # top-level system/home directory, not any ordinary path under them, like a project
    # subfolder) - an ordinary absolute path goes through scope_guard.py's approval gate
    # instead. Mirrors _CATASTROPHIC_RM_TARGETS in structural_block() below. A trailing slash
    # (or run of them) on the target is tolerated, so "rm -rf /etc/" is still caught even
    # though structural_block() (the primary layer) is this pattern's real normalization path -
    # this regex is only the fallback for malformed-quoting input structural_block() can't
    # tokenize at all.
    (r"\brm\s+-rf\s+(?:/|/\*|/home/*|/etc/*|/usr/*|/var/*|/boot/*|/bin/*|/lib/*|/sbin/*|/opt/*|/root/*)(?=\s|$)",
     "rm -rf on a bare root-level system/home directory: catastrophic, never allowed"),
    (r"\brm\s+-rf\s+~", "rm -rf ~: home directory deletion"),
    (r"\brm\s+-rf\s+\.\s", "rm -rf .: deletes the whole current directory"),
    (r"\brm\s+-rf\s+\*", "rm -rf *: wildcard deletion"),
    (r"\brm\s+-rf\s+\$", "rm -rf with a variable: deletion target is not visible"),

    # Destructive git commands (plain-regex layer; structural_block() also catches the
    # option-reordered forms of these)
    (r"git\s+reset\s+--hard", "git reset --hard: permanently discards uncommitted changes"),
    (r"git\s+push\s+.*--force\b", "git push --force: overwrites remote history"),
    (r"git\s+push\s+.*-f\b", "git push -f: force push overwrites remote history"),
    (r"git\s+clean\s+-fd", "git clean -fd: permanently deletes untracked files"),
    (r"git\s+checkout\s+--\s+\.", "git checkout -- .: discards all local changes"),

    # Destructive DB commands
    (r"DROP\s+(DATABASE|TABLE|SCHEMA)", "DROP: permanently deletes data"),
    (r"TRUNCATE\s+TABLE", "TRUNCATE: deletes every row in a table"),
    (r"DELETE\s+FROM\s+\w+\s*;?\s*$", "DELETE without WHERE: deletes every row"),

    # Dangerous system commands
    (r"\bmkfs\b", "mkfs: formatting a filesystem is destructive"),
    (r"\bdd\s+if=", "dd: direct disk writes risk data loss"),
    (r"chmod\s+-R\s+777", "chmod -R 777: opens all permissions, a security risk"),
    (r"curl\s+.*\|\s*(ba)?sh", "curl | sh: runs an unverified remote script"),
    (r"wget\s+.*\|\s*(ba)?sh", "wget | sh: runs an unverified remote script"),

    # Secret exposure
    (r"echo\s+\$\w*(KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)", "echoes a secret environment variable"),
    (r"printenv\s+.*(KEY|SECRET|TOKEN|PASSWORD)", "prints a secret environment variable"),

    # Process kills
    (r"kill\s+-9\s+-1", "kill -9 -1: kills every process"),
    (r"killall\s+-9", "killall -9: force-kills processes by name"),
]

WARN_PATTERNS = [
    (r"\brm\s+", "rm used - confirm this deletion is intended"),
    (r"git\s+rebase", "git rebase - rewrites history, double check"),
    (r"npm\s+publish", "npm publish - publishes a package publicly"),
]

# Targets that make `rm -r[f]` catastrophic regardless of option order/splitting, trailing
# slashes, or repeated/`.`-padded path segments. Mirrors the BLOCKED_PATTERNS rm -rf cases
# below (root, home, cwd, wildcard, unexpanded variable) plus the other bare top-level
# system directories - anything else recursive (a normal project folder, or a subfolder of one
# of these, like "/etc/myapp") is left to scope_guard.py's approval gate, which is the
# intended, reversible-with-approval path for an ordinary deletion.
_CATASTROPHIC_RM_TARGETS = {
    "/", "~", "*",
    "/home", "/etc", "/usr", "/var", "/boot",
    "/bin", "/lib", "/sbin", "/opt", "/root",
}


def _normalize_rm_target(target: str) -> str:
    """Normalizes a filesystem path target for catastrophic-target comparison: collapses
    repeated slashes, drops trailing slashes, and resolves trailing "/." segments - so
    "/etc/", "/etc//", and "/etc/." are all recognized as the same bare "/etc" target that
    `rm -rf /etc` would be. A "$..." variable target is returned unchanged (never expanded -
    it is judged separately, below)."""
    if target.startswith("$"):
        return target
    t = re.sub(r"/+", "/", target)
    while True:
        if t.endswith("/."):
            t = t[:-2] or "/"
            continue
        if len(t) > 1 and t.endswith("/"):
            t = t[:-1]
            continue
        break
    return t


def _is_catastrophic_rm_target(target: str) -> bool:
    normalized = _normalize_rm_target(target)
    if normalized.startswith("$"):
        return True
    return normalized in _CATASTROPHIC_RM_TARGETS


def _git_subcommand_and_rest(segment: list[str]) -> tuple[str, list[str]] | None:
    """Same global-option-skipping walk as scope_guard._git_hit, returning the raw
    (subcommand, rest) pair instead of a gating reason - block_dangerous applies its own,
    narrower (catastrophic-only) flag checks on top of it."""
    name, i = _sg._first_command(segment)
    if name != "git":
        return None
    j = i + 1
    while j < len(segment):
        t = segment[j]
        if t in _sg.GIT_GLOBAL_OPTS_WITH_VALUE:
            j += 2
            continue
        if any(t.startswith(opt + "=") for opt in _sg.GIT_GLOBAL_OPTS_WITH_VALUE):
            j += 1
            continue
        if t.startswith("-"):
            j += 1
            continue
        break
    if j >= len(segment):
        return None
    return segment[j], segment[j + 1:]


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


def structural_block(command: str) -> str | None:
    tokens = _sg._tokenize(command)
    if tokens is None:
        return None  # malformed quoting: BLOCKED_PATTERNS below is the fallback layer
    for segment in _sg._split_segments(tokens):
        git = _git_subcommand_and_rest(segment)
        if git:
            subcmd, rest = git
            flags = _flag_set(rest)
            if subcmd == "push" and ({"force", "f"} & flags):
                return "git push --force: overwrites remote history"
            if subcmd == "reset" and "hard" in flags:
                return "git reset --hard: permanently discards uncommitted changes"
            if subcmd == "clean" and ("f" in flags) and ("d" in flags):
                return "git clean -fd: permanently deletes untracked files"
            if subcmd == "checkout":
                args = _positional_args(rest)
                if args and args[-1] == "." and "--" in rest:
                    return "git checkout -- .: discards all local changes"
            continue
        name, i = _sg._first_command(segment)
        if name == "rm":
            flags = _flag_set(segment[i + 1:])
            if ({"r", "R", "recursive"} & flags) and ("f" in flags or "force" in flags):
                for target in _positional_args(segment[i + 1:]):
                    if _is_catastrophic_rm_target(target):
                        return (f"rm -rf {target}: destructive deletion of a catastrophic "
                                 "target (root, home dir, wildcard or unexpanded variable)")
    return None


def check(command: str) -> tuple[str | None, str | None]:
    """Returns (block_reason, warn_reason); either may be None."""
    structural = structural_block(command)
    if structural:
        return structural, None
    for pattern, reason in BLOCKED_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return reason, None
    for pattern, reason in WARN_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return None, reason
    return None, None


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        # Unreadable input means we cannot tell whether this call is dangerous: fail CLOSED.
        print(f"BLOCKED: 입력 파싱 실패로 안전하게 차단({type(e).__name__})", file=sys.stderr)
        return 2
    if not isinstance(data, dict) or data.get("tool_name") != "Bash":
        return 0
    tool_input = data.get("tool_input")
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if not command:
        return 0
    try:
        block, warn = check(command)
    except Exception as e:
        # This is the last line of defense, so an internal parsing error must fail CLOSED
        # (block), not silently allow the command through.
        print(f"BLOCKED: 내부 오류로 안전하게 차단({type(e).__name__})\n명령: {command}", file=sys.stderr)
        return 2
    if block:
        print(f"BLOCKED: {block}\n명령: {command}", file=sys.stderr)
        return 2
    if warn:
        print(f"WARNING: {warn}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
