#!/usr/bin/env python3
# Generated from the private ops workspace at b94af62b97af64e2e58e8c102fa2538f066ae366; do not edit by hand (see harness/export_ops_public.py).
"""Safety hook: blocks outright-destructive Bash commands, regardless of any approval gate.

v2 gap bundle 1 (team-lead 2026-11-02, plan `_ops/plans/2026-11-02-v2-gap-verdicts.md`
6절, items 17/18(partial)/20/21/22/280(partial), 공백④): the previous installed version of this
hook was a plain regex list over the literal command string, so a reordered/split option
(`git -C . push --force`, `rm -f -r /`, `rm -fr /`) or a command-runner prefix chain
(`sudo -u root rm -rf /`, `env -u FOO rm -rf /`, `nice -n 5 ...`, chained) never matched, and the
hook was not even registered in settings.json's PreToolUse Bash hooks at all - so "승인 없이는
위험 명령 거부" (MASTER_RULES.md, PSTACK_KO.md) was a documented promise with nothing behind it.

PreToolUse hook - exit 0: allow, exit 2: block. This is the LAST line of defense for commands
that should never run from an agent session at all, independent of scope_guard.py's approval
gate (which this bundle explicitly leaves untouched - "승인 관문 자체는 바꾸지 말 것"): a reversible,
in-scope action goes through that gate (overridable with "승인 <TASK>"); the catastrophic actions
here are never approvable by anyone but the owner typing the command themselves outside Claude.

structural_block() walks every segment `_shell.all_segments()` finds (after fully unwrapping
separators - including a literal newline, which plain whitespace-splitting would otherwise
swallow -, command substitution, nested shells, and `env -S`) for git/rm's actual option grammar
and any command-runner prefix chain, so option order, option splitting, prefix wrapping, and the
obfuscation tricks above cannot change the answer. A Codex read-only adversarial review
(2026-11-02) of the first version of this file - BEFORE it shared `_shell.py` with
protect_sensitive_files.py - found and this revision fixes: a literal-newline-separated command,
`bash -lc '...'`, `env -S '...'`, a doubly-nested `$(...)`, and a `git push ... +HEAD:main` force
refspec (a leading "+" on a refspec forces that one ref, the same effect as --force, without the
literal word ever appearing).

The plain regex list (BLOCKED_PATTERNS) stays as a second, independent layer for everything else
(DB commands, mkfs, dd, chmod, curl|sh, secret echo) where option-reordering is not a realistic
concern, and as the fallback when a command cannot be tokenized at all.

pkill/killall are hard-blocked the SAME structural way (see _pkill_killall_hit below) - these are
never approvable, even with "승인 <TASK>" (team-lead instruction: "pkill·killall 하드 차단").
A bare mention of the word "pkill"/"killall" inside another command's own argument (a quoted echo
string, a grep pattern) is never flagged, because only a segment's ACTUAL first command (after
any prefix chain) is checked.

A malformed/unparseable stdin payload, or an internal error while judging the command, fails
CLOSED (blocks) - never silently allows a command through unexamined (item 22).

team-lead instruction 2026-11-02 (사고②, 반장 우회 길 차단): `git rebase`/`reset`/`checkout`/
`stash` targeting a work copy under /apps/_wt/ (via cwd or `-C <path>`) is hard-blocked for
every session, including the copy's own owner - see _wt_copy_guard_hit() below.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _shell  # noqa: E402

BLOCKED_PATTERNS = [
    # Destructive file operations (plain-regex layer; structural_block() also catches the
    # option-reordered/prefixed forms of the rm -rf cases below).
    (r"\brm\s+-rf\s+(?:/|/\*|/home/*|/etc/*|/usr/*|/var/*|/boot/*|/bin/*|/lib/*|/sbin/*|/opt/*|/root/*)(?=\s|$)",
     "rm -rf on a bare root-level system/home directory: catastrophic, never allowed"),
    (r"\brm\s+-rf\s+~", "rm -rf ~: home directory deletion"),
    (r"\brm\s+-rf\s+\.\s", "rm -rf .: deletes the whole current directory"),
    (r"\brm\s+-rf\s+\*", "rm -rf *: wildcard deletion"),
    (r"\brm\s+-rf\s+\$", "rm -rf with a variable: deletion target is not visible"),

    # Destructive git commands (plain-regex layer; structural_block() also catches the
    # option-reordered/prefixed forms of these)
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

    # Process kills (plain-regex layer; structural_block()'s _pkill_killall_hit is the primary,
    # prefix/order-proof layer for pkill/killall specifically)
    (r"kill\s+-9\s+-1", "kill -9 -1: kills every process"),
    (r"killall\s+-9", "killall -9: force-kills processes by name"),
]

WARN_PATTERNS = [
    (r"\brm\s+", "rm used - confirm this deletion is intended"),
    (r"git\s+rebase", "git rebase - rewrites history, double check"),
    (r"npm\s+publish", "npm publish - publishes a package publicly"),
]

# Targets that make `rm -r[f]` catastrophic regardless of option order/splitting, trailing
# slashes, or a `.`-padded path.
_CATASTROPHIC_RM_TARGETS = {
    "/", "~", "*", "/*",
    "/home", "/etc", "/usr", "/var", "/boot",
    "/bin", "/lib", "/sbin", "/opt", "/root",
}

# Owner decision 2026-11-02 00:35/00:50 ("승인 절차를 없애라" + 하드 차단 이분법): a recursive
# force-delete OUTSIDE of a disposable work-copy or scratchpad is now hard-blocked outright, not
# merely a catastrophic-literal-target check - there is no approval branch to fall back to
# anymore, so "ordinary rm -r of a real project folder" must itself default-deny rather than
# default-allow. A work copy (`/apps/_wt/<app>/<name>`, made by `_shared/wt-new.sh`) and a
# scratchpad path (`/tmp/...`, including this harness's own `/tmp/claude-*` scratch dirs) are the
# two places a recursive delete is expected and safe - "백업 없는 rm -rf(사본·scratchpad 밖)".
_SAFE_RM_ROOTS = ("/apps/_wt/", "/tmp/")


def _in_safe_rm_zone(target: str, cwd: str) -> bool:
    """A relative target is resolved against `cwd` (no filesystem access - purely textual, since
    this hook must never touch disk); an unresolvable/empty cwd for a relative target fails
    CLOSED (not in a safe zone), per the same "안전 쪽으로" bias used throughout this file."""
    if target.startswith("$"):
        return False
    abs_target = target if target.startswith("/") else (os.path.join(cwd, target) if cwd else None)
    if not abs_target:
        return False
    normalized = os.path.normpath(abs_target)
    return any(normalized == root.rstrip("/") or normalized.startswith(root) for root in _SAFE_RM_ROOTS)


def _normalize_rm_target(target: str) -> str:
    """Collapses repeated slashes, drops trailing slashes, and resolves trailing "/." segments,
    so "/etc/", "/etc//", and "/etc/." are all recognized as the same bare "/etc" target. A
    "$..." variable target is returned unchanged (never expanded - judged separately)."""
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


def _pkill_killall_hit(segment: list[str]) -> str | None:
    """pkill/killall, run directly or behind any command-runner prefix chain, are hard-blocked
    outright - never approval-gated. A bare mention inside a quoted string is never flagged,
    because only the segment's actual first command (after assignments/prefixes) is checked."""
    name, _ = _shell._first_command(segment)
    if name in ("pkill", "killall"):
        return f"{name}: 프로세스 강제 종료는 항상 금지(승인으로도 허용하지 않음)"
    return None


# team-lead instruction 2026-11-02 (사고②): another session's work copy (/apps/_wt/<app>/
# <name>, made by _shared/wt-new.sh) is never rebased/reset/checked-out/stashed by a direct Bash
# call from ANY session, including the copy's own owner - landing (rebase onto base, scope-check,
# the ff-only merge) is wt-land.sh's job alone. 2026-11-02 사고②: a session's `git rebase` on a
# copy another session still had uncommitted work in wiped that work. Hard block, no approval
# branch, no owner exception.
_WT_ROOT = os.path.normpath(os.path.join(os.environ.get("WT_APPS", "/apps"), "_wt"))


def _under_wt_root(path: str) -> bool:
    if not path:
        return False
    normalized = os.path.normpath(os.path.expanduser(path))
    return normalized == _WT_ROOT or normalized.startswith(_WT_ROOT + os.sep)


def _resolve_rel(base: str, val: str) -> str:
    val = val.strip("'\"")
    return val if os.path.isabs(val) else os.path.join(base or ".", val)


def _git_repo_dir(segment: list[str], cwd: str) -> str:
    """Resolves the directory a git invocation in `segment` actually targets, in the same
    priority git itself uses: an explicit worktree (`--work-tree=`/`-C` in that literal order, or
    the `GIT_WORK_TREE` env assignment preceding the command) wins outright; `--git-dir=`/
    `GIT_DIR` alone resolves to its parent directory when its basename is `.git` (git's own
    default worktree-from-gitdir rule), else the value itself; with neither, `-C` (repeated,
    each relative to the previous) resolved from `cwd`. Purely textual, never touches disk.

    Fixed 2026-11-02 (Codex P1): `--work-tree=`/`--git-dir=`/`GIT_WORK_TREE`/`GIT_DIR` used to be
    silently skipped as "just another global option with a value" without ever being read, so
    `GIT_DIR=W/.git GIT_WORK_TREE=W git checkout -f HEAD` (run from OUTSIDE the copy) resolved to
    `cwd`, not `W`, and the work-copy guard never fired."""
    name, i = _shell._first_command(segment)
    if name != "git":
        return cwd
    work_tree = None
    git_dir = None
    for tok in segment[:i]:
        if tok.startswith("GIT_WORK_TREE="):
            work_tree = tok.split("=", 1)[1]
        elif tok.startswith("GIT_DIR="):
            git_dir = tok.split("=", 1)[1]
    j = i + 1
    c_repo = cwd
    while j < len(segment):
        t = segment[j]
        if t == "-C" and j + 1 < len(segment):
            c_repo = _resolve_rel(c_repo, segment[j + 1]); j += 2; continue
        if t.startswith("-C="):
            c_repo = _resolve_rel(c_repo, t.split("=", 1)[1]); j += 1; continue
        if t == "--work-tree" and j + 1 < len(segment):
            work_tree = segment[j + 1]; j += 2; continue
        if t.startswith("--work-tree="):
            work_tree = t.split("=", 1)[1]; j += 1; continue
        if t == "--git-dir" and j + 1 < len(segment):
            git_dir = segment[j + 1]; j += 2; continue
        if t.startswith("--git-dir="):
            git_dir = t.split("=", 1)[1]; j += 1; continue
        if t in _shell.GIT_GLOBAL_OPTS_WITH_VALUE:
            j += 2; continue
        if any(t.startswith(opt + "=") for opt in _shell.GIT_GLOBAL_OPTS_WITH_VALUE):
            j += 1; continue
        if t.startswith("-"):
            j += 1; continue
        break
    if work_tree:
        return _resolve_rel(cwd, work_tree)
    if git_dir:
        resolved = _resolve_rel(cwd, git_dir)
        return os.path.dirname(resolved) if os.path.basename(resolved) == ".git" else resolved
    return c_repo


# 2026-11-02 (Codex P1): `git -c alias.wipe='reset --hard' wipe` dodged every check above because
# `git_subcommand_and_rest` only ever reads the LITERAL subcommand token ("wipe"), never resolving
# a `-c alias.<name>=<value>` config override defined on the same invocation.
_ALIAS_CONFIG_RE = re.compile(r"^alias\.([A-Za-z0-9_-]+)=(.*)$", re.S)


def _git_aliases(segment: list[str]) -> dict[str, str]:
    name, i = _shell._first_command(segment)
    if name != "git":
        return {}
    aliases: dict[str, str] = {}
    j = i + 1
    while j < len(segment):
        t = segment[j]
        if t in ("-c", "--config") and j + 1 < len(segment):
            m = _ALIAS_CONFIG_RE.match(segment[j + 1].strip("'\""))
            if m:
                aliases[m.group(1)] = m.group(2)
            j += 2
            continue
        if t.startswith("-c=") or t.startswith("--config="):
            m = _ALIAS_CONFIG_RE.match(t.split("=", 1)[1].strip("'\""))
            if m:
                aliases[m.group(1)] = m.group(2)
            j += 1
            continue
        if t in _shell.GIT_GLOBAL_OPTS_WITH_VALUE:
            j += 2
            continue
        if any(t.startswith(opt + "=") for opt in _shell.GIT_GLOBAL_OPTS_WITH_VALUE):
            j += 1
            continue
        if t.startswith("-"):
            j += 1
            continue
        break
    return aliases


def _resolve_git_alias(subcmd: str, rest: list[str], segment: list[str]) -> tuple[str, list[str]] | str:
    """Returns (effective_subcmd, effective_rest) with a `-c alias.<subcmd>=<value>` override
    applied, or a block-reason STRING outright for a `!shell` alias (an alias value starting with
    `!` runs as a raw shell command - never safely re-analyzable here as a git subcommand, so it
    is hard-blocked unconditionally rather than silently passed through unexamined)."""
    aliases = _git_aliases(segment)
    value = aliases.get(subcmd)
    if value is None:
        return subcmd, rest
    if value.strip().startswith("!"):
        return f"git alias로 셸 명령 실행(alias.{subcmd}='!...') — 항상 금지(검사를 피해 갈 수 있음, Codex P1)"
    alias_tokens = _shell._tokenize(value) or value.split()
    if not alias_tokens:
        return subcmd, rest
    return alias_tokens[0], alias_tokens[1:] + rest


def _wt_copy_guard_hit(subcmd: str, segment: list[str], cwd: str) -> str | None:
    if subcmd == "worktree":
        return None  # handled separately by _worktree_remove_hit (positional path, not -C/cwd)
    if subcmd not in ("rebase", "reset", "checkout", "stash"):
        return None
    repo = _git_repo_dir(segment, cwd)
    if not _under_wt_root(repo):
        return None
    return (f"git {subcmd}: 작업 사본(/apps/_wt/..)은 다른 세션이 쓰고 있을 수 있어 직접 돌리지 "
            "않는다 — 합치기는 wt-land.sh만 한다")


def _worktree_remove_hit(subcmd: str, rest: list[str], cwd: str) -> str | None:
    """`git worktree remove [--force] <path>` deletes a work copy outright with none of
    wt-gc.sh's merge/dirty verification - 2026-11-02 Codex P1. Blocked whenever the targeted path
    is under /apps/_wt/, regardless of where the git command itself runs (-C/cwd do not
    matter here - the path being removed is what matters)."""
    if subcmd != "worktree":
        return None
    positional = _shell._positional_args(rest)
    if not positional or positional[0] != "remove":
        return None
    for p in positional[1:]:
        if _under_wt_root(_resolve_rel(cwd, p)):
            return "git worktree remove: 작업 사본 삭제는 wt-gc.sh만 한다(합침·미커밋 검증 없이 직접 지우지 않음)"
    return None


def _git_force_hit(subcmd: str, rest: list[str]) -> str | None:
    flags = _shell._flag_set(rest)
    if subcmd == "push":
        if {"force", "f"} & flags:
            return "git push --force: overwrites remote history"
        # A refspec argument starting with "+" forces that one ref, the exact effect of
        # --force, without the word ever appearing (`git push origin +HEAD:main`).
        for arg in _shell._positional_args(rest):
            if arg.startswith("+") and arg != "+":
                return "git push에 '+' 강제 refspec: --force와 같은 효과"
        return None
    if subcmd == "reset" and "hard" in flags:
        return "git reset --hard: permanently discards uncommitted changes"
    if subcmd == "clean" and ("f" in flags) and ("d" in flags):
        return "git clean -fd: permanently deletes untracked files"
    if subcmd == "checkout":
        args = _shell._positional_args(rest)
        if args and args[-1] == "." and "--" in rest:
            return "git checkout -- .: discards all local changes"
    if subcmd == "stash":
        return "git stash: 작업 중 변경을 숨김 — 하드 차단"
    if subcmd == "add":
        if ({"A", "all"} & flags) or "." in _shell._positional_args(rest):
            return "git add -A/--all/.: 범위 밖 파일이 섞여 커밋될 위험 — 하드 차단"
    return None


def _apply_cd(segment: list[str], cwd: str) -> str:
    """Updates the running cwd when `segment` is a plain `cd <dir>` - 2026-11-02 Codex P1:
    `cd W && git checkout -f HEAD` used to be checked against the ORIGINAL tool-call cwd only,
    never the directory the first segment actually `cd`ed into, so the work-copy guard never saw
    that the second segment's git command was running inside `W`."""
    name, i = _shell._first_command(segment)
    if name != "cd":
        return cwd
    args = _shell._positional_args(segment[i + 1:])
    if not args or args[0].startswith("$"):
        return cwd  # bare `cd` (HOME) or an unresolvable variable target: leave unchanged
    return _resolve_rel(cwd, args[0])


def _check_segment(segment: list[str], cwd: str) -> str | None:
    pk = _pkill_killall_hit(segment)
    if pk:
        return pk
    git = _shell.git_subcommand_and_rest(segment)
    if git:
        subcmd, rest = git
        resolved = _resolve_git_alias(subcmd, rest, segment)
        if isinstance(resolved, str):
            return resolved
        subcmd, rest = resolved
        hit = _git_force_hit(subcmd, rest)
        if hit:
            return hit
        hit = _worktree_remove_hit(subcmd, rest, cwd)
        if hit:
            return hit
        hit = _wt_copy_guard_hit(subcmd, segment, cwd)
        if hit:
            return hit
        return None
    name, i = _shell._first_command(segment)
    if name == "rm":
        rest = _shell._strip_redirections(segment[i + 1:])
        flags = _shell._flag_set(rest)
        if ({"r", "R", "recursive"} & flags) and ("f" in flags or "force" in flags):
            for target in _shell._positional_args(rest):
                if _is_catastrophic_rm_target(target):
                    return (f"rm -rf {target}: destructive deletion of a catastrophic "
                             "target (root, home dir, wildcard or unexpanded variable)")
                if not _in_safe_rm_zone(target, cwd):
                    return (f"rm -rf {target}: 백업 없는 재귀 삭제, 사본(_wt)·scratchpad(/tmp) 밖 "
                             "— 하드 차단")
    return None


def structural_block(command: str, cwd: str = "") -> str | None:
    tokens = _shell._tokenize(command)
    if tokens is not None:
        # Pass 1: top-level segments (separated by `;`/`&&`/`||`/`|`/`&`/newline) in their actual
        # execution ORDER, tracking a `cd` across them - the authoritative pass for cwd-sensitive
        # checks (git -C resolution, rm's safe-zone check).
        running_cwd = cwd
        for seg in _shell._split_segments(tokens):
            hit = _check_segment(seg, running_cwd)
            if hit:
                return hit
            running_cwd = _apply_cd(seg, running_cwd)
    # Pass 2: only segments HIDDEN behind substitution/nested-shells/env -S (`_shell.
    # nested_segments` - deliberately NOT `all_segments`, which would also re-check the literal
    # top-level segments pass 1 already walked with cd-tracking, against the unmodified ORIGINAL
    # cwd, undoing that tracking - Codex 11-02 false block). Each hidden segment is checked
    # against the original cwd (a `cd` inside a substitution/nested shell does not escape to
    # affect its sibling, same as real shell scoping; cwd-tracking for genuinely nested command
    # strings is not attempted here - conservative default, same as before this fix).
    for seg in _shell.nested_segments(command):
        hit = _check_segment(seg, cwd)
        if hit:
            return hit
    return None


def check(command: str, cwd: str = "") -> tuple[str | None, str | None]:
    """Returns (block_reason, warn_reason); either may be None."""
    structural = structural_block(command, cwd)
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
    cwd = str(data.get("cwd", "") or "")
    try:
        block, warn = check(command, cwd)
    except Exception as e:
        # This is the last line of defense, so an internal error must fail CLOSED, not silently
        # allow the command through (item 22).
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
