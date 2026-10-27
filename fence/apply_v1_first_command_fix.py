#!/usr/bin/env python3
"""Fix for the LIVE scope guard's command finder — run by the owner (the model cannot touch the hooks).

Usage:  python3 apply_v1_first_command_fix.py           # back up, replace one function, self-test, auto-rollback
        python3 apply_v1_first_command_fix.py --check   # only show which probes the live guard misses now

Problem (read from the live file): _first_command skips ONE runner prefix (env/nice/...) and then only
tokens that start with "-", so an option VALUE becomes "the command":
    env -u FOO git push      -> command "FOO"   (push not gated)
    nice -n 5 git push       -> command "5"
    timeout 30 git push      -> "timeout" is not a known prefix at all
    env nice git push        -> only the first prefix is skipped
Fix: replace exactly that function with one that strips a whole prefix CHAIN using a per-command
option-arity table. "sudo" stays NOT skippable (sudo itself remains a gated command).
The guard denies by printing a permissionDecision "deny" JSON (exit 0); the probes check that.
"""
import json
import os
import shutil
import subprocess
import sys

HOOKS = os.path.expanduser("~/.claude/hooks")
TARGET = os.path.join(HOOKS, "scope-guard.py")
BACKUP = TARGET + ".bak-firstcmd"
CHECK = "--check" in sys.argv

OLD = '''def _first_command(tokens: list[str]) -> tuple[str | None, int]:
    """Skips leading VAR=value assignments and one command-runner prefix (sudo/env/xargs/...),
    returns (basename of the real command token, its index), or (None, -1) if none found."""
    i = 0
    while i < len(tokens) and ASSIGN_RE.match(tokens[i]):
        i += 1
    # "sudo" is deliberately NOT a skippable prefix: "sudo -u deploy git push" used to resolve to
    # the option VALUE "deploy" as the command and slip past every gate. sudo itself is gated.
    if i < len(tokens) and _basename(tokens[i]) in ALLOWED_PREFIXES:
        i += 1
        while i < len(tokens) and (ASSIGN_RE.match(tokens[i]) or tokens[i].startswith("-")):
            i += 1
    if i >= len(tokens):
        return None, -1
    return _basename(tokens[i]), i
'''

NEW = '''# Runner prefixes whose own options take a separate VALUE token; that value is consumed so it is
# never mistaken for the real command ("env -u FOO git push", "nice -n 5 git push").
_PREFIX_VALUE_OPTS = {
    "env": {"-u", "-C", "-S", "-P", "--unset", "--chdir", "--split-string"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "-P", "-u", "--class", "--classdata"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "xargs": {"-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s", "--arg-file", "--delimiter",
              "--max-args", "--max-procs", "--max-chars", "--replace"},
}
# A required positional argument before the real command ("timeout 30 cmd", "chrt 10 cmd").
_PREFIX_POSITIONAL = {"timeout": 1, "chrt": 1, "taskset": 1}
# sudo is deliberately absent: sudo itself is a gated command.
_CHAIN_PREFIXES = ALLOWED_PREFIXES | set(_PREFIX_VALUE_OPTS) | set(_PREFIX_POSITIONAL)


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
        if t in value_opts:
            j += 1
    for _ in range(_PREFIX_POSITIONAL.get(name, 0)):
        if j < len(tokens) and not tokens[j].startswith("-"):
            j += 1
    return j


def _first_command(tokens: list[str]) -> tuple[str | None, int]:
    """Skips leading VAR=value assignments and a whole CHAIN of command-runner prefixes
    (env/nice/ionice/timeout/stdbuf/xargs/nohup/exec/command/time/chrt/taskset, any mix, with their
    option values), returns (basename of the real command token, its index), or (None, -1).
    "sudo" is NOT skippable: "sudo ..." resolves to sudo, which is gated itself."""
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
'''

GATED_PROBES = [
    "git push origin main",
    "env -u FOO git push origin main",
    "nice -n 5 git push origin main",
    "timeout 30 git push origin main",
    "env nice -n 5 timeout 30 git push origin main",
    "FOO=1 env -u BAR stdbuf -oL git push origin main",
    "sudo -u root true",
]
FREE_PROBES = ["ls -la", "nice -n 5 ls", "env -u FOO echo hi", "git status", "timeout 30 git log -1"]


def denied(command: str) -> bool:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "<apps-root>",
               "hook_event_name": "PreToolUse"}  # no session_id: a gated command must be denied
    r = subprocess.run([sys.executable, TARGET], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=30)
    return r.returncode == 2 or '"permissionDecision": "deny"' in r.stdout


def probe() -> list[str]:
    bad = [f"must deny:  {c}" for c in GATED_PROBES if not denied(c)]
    bad += [f"must allow: {c}" for c in FREE_PROBES if denied(c)]
    return bad


def show(title: str, bad: list[str]) -> None:
    print(f"[{title}] {len(bad)} problem(s)")
    for b in bad:
        print("   - " + b)


def main() -> int:
    if not os.path.exists(TARGET):
        print(f"[stop] {TARGET} not found")
        return 1
    before = probe()
    show("before", before)
    if "must deny:  git push origin main" in before:
        print("[stop] the plain control is not denied -- probe setup is wrong, nothing changed")
        return 5
    if CHECK:
        return 0 if not before else 3
    src = open(TARGET, encoding="utf-8").read()
    if "_CHAIN_PREFIXES" in src:
        print("[ok] already patched")
        return 0 if not before else 3
    if OLD not in src:
        print("[stop] the expected _first_command text was not found (file differs) -- nothing changed")
        return 6
    shutil.copy2(TARGET, BACKUP)
    open(TARGET, "w", encoding="utf-8").write(src.replace(OLD, NEW, 1))
    after = probe()
    show("after", after)
    if after:
        shutil.copy2(BACKUP, TARGET)
        print("[rollback] restored the original from " + os.path.basename(BACKUP))
        return 4
    print("[ok] patched; backup kept at " + os.path.basename(BACKUP))
    return 0


if __name__ == "__main__":
    sys.exit(main())
