#!/usr/bin/env python3
"""V1 prefix-chain fix for the LIVE hooks — run by the owner (the model is blocked from ~/.claude/hooks).

Usage:  python3 apply_v1_prefix_fix.py           # back up, apply, self-test, auto-rollback on failure
        python3 apply_v1_prefix_fix.py --check   # only run the bypass probes against the live hooks

What it fixes (external review, reproduced in the public repo copy of the same design):
  - "env -u FOO rm -rf /", "nice -n 5 rm -rf /", "sudo -u root rm -rf /", "timeout 10 rm -rf /":
    option VALUES (FOO, 5, root, 10) were read as the command, so the real command behind a
    prefix chain escaped the structural checks.
How: installs ~/.claude/hooks/gb_prefix_unwrap.py and, in block-dangerous.py and scope-guard.py,
right after the line that reads the Bash command, appends the unwrapped inner command of every
segment as extra lines, so the existing checks also see "rm -rf /" / "git push" at a line start.
Nothing is removed from the original command text, so existing blocks keep working.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

HOOKS = os.path.expanduser("~/.claude/hooks")
TARGETS = ["block-dangerous.py", "scope-guard.py"]
MODULE = "gb_prefix_unwrap.py"
MARK = "# gb_prefix_unwrap: v1 prefix-chain fix"
CHECK = "--check" in sys.argv
# The scope guard gates commands per project folder, so probes run from inside an app repo.
PROBE_CWD = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--cwd=")),
                 "<apps-root>/if-gongbang")

MODULE_SRC = r'''"""Unwrap command-runner prefix chains (sudo/env/nice/ionice/timeout/stdbuf/nohup/...) so the
real command behind them is visible to the guard hooks. Option values are consumed, never read
as the command."""
import re
import shlex

ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
VALUE_OPTS = {
    "sudo": {"-u", "-g", "-p", "-r", "-t", "-C", "-h", "-D",
             "--user", "--group", "--prompt", "--role", "--type", "--close-from", "--host", "--chdir"},
    "env": {"-u", "-C", "-S", "-P", "--unset", "--chdir", "--split-string"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "-P", "-u", "--class", "--classdata"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
}
POSITIONAL = {"timeout": 1}
PREFIXES = set(VALUE_OPTS) | {"nohup", "command", "exec", "builtin", "time", "xargs", "chrt", "taskset"}
SEPARATORS = {";", "&&", "||", "|", "&", "\n"}


def _base(tok):
    return tok.rsplit("/", 1)[-1]


def _tokens(command):
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except Exception:
        return None


def _segments(tokens):
    segs, cur = [], []
    for t in tokens:
        if t in SEPARATORS:
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    return segs


def _skip_one(tokens, i):
    name = _base(tokens[i])
    opts = VALUE_OPTS.get(name, set())
    j = i + 1
    while j < len(tokens):
        t = tokens[j]
        if ASSIGN_RE.match(t):
            j += 1
            continue
        if t == "--":
            j += 1
            break
        if not t.startswith("-"):
            break
        j += 1
        if t in opts or (name == "stdbuf" and len(t) == 2):
            j += 1
    for _ in range(POSITIONAL.get(name, 0)):
        if j < len(tokens) and not tokens[j].startswith("-"):
            j += 1
    if name in ("chrt", "taskset") and j < len(tokens) and not tokens[j].startswith("-"):
        j += 1
    return j


def inner(seg):
    i = 0
    while i < len(seg) and ASSIGN_RE.match(seg[i]):
        i += 1
    start = i
    while i < len(seg) and _base(seg[i]) in PREFIXES:
        i = _skip_one(seg, i)
    if i == start or i >= len(seg):
        return None
    return " ".join(shlex.quote(t) for t in seg[i:])


def augment(command):
    if not isinstance(command, str) or not command:
        return command
    toks = _tokens(command)
    if toks is None:
        return command
    extra = [x for x in (inner(s) for s in _segments(toks)) if x]
    if not extra:
        return command
    return command + "\n" + "\n".join(extra)
'''

ANCHOR_RE = re.compile(r'^(?P<ind>[ \t]*)command\s*=\s*.*["\']command["\'].*$', re.M)
# Fallback anchor: the line that parses the hook's stdin JSON ("data = json.load(sys.stdin)" or
# "data = json.loads(raw)"); the command inside that dict is rewritten in place right after it.
JSON_RE = re.compile(r'^(?P<ind>[ \t]*)(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*json\.loads?\(.*\)\s*$', re.M)
INSERT_JSON = (
    "{ind}" + MARK + "\n"
    "{ind}try:\n"
    "{ind}    import gb_prefix_unwrap as _gbu\n"
    "{ind}    _gbu_ti = {var}.get(\"tool_input\") if isinstance({var}, dict) else None\n"
    "{ind}    if isinstance(_gbu_ti, dict) and isinstance(_gbu_ti.get(\"command\"), str):\n"
    "{ind}        _gbu_ti[\"command\"] = _gbu.augment(_gbu_ti[\"command\"])\n"
    "{ind}except Exception as _gbu_err:\n"
    "{ind}    print(f\"BLOCKED: prefix unwrap failed ({{type(_gbu_err).__name__}})\", file=sys.stderr)\n"
    "{ind}    sys.exit(2)\n"
)

INSERT = (
    "{ind}" + MARK + "\n"
    "{ind}try:\n"
    "{ind}    import gb_prefix_unwrap as _gbu\n"
    "{ind}    command = _gbu.augment(command)\n"
    "{ind}except Exception as _gbu_err:\n"
    "{ind}    print(f\"BLOCKED: prefix unwrap failed ({{type(_gbu_err).__name__}})\", file=sys.stderr)\n"
    "{ind}    sys.exit(2)\n"
)

PROBES_DANGEROUS = [
    "env -u FOO rm -rf /",
    "nice -n 5 rm -rf /",
    "sudo -u root rm -rf /",
    "timeout 10 rm -rf /",
    "ionice -c 3 nice -n 5 rm -rf /",
]
PROBES_GATED = [
    "env -u FOO git push origin main",
    "nice -n 5 git push origin main",
    "timeout 30 git push origin main",
]
PROBES_SAFE = ["ls -la", "nice -n 5 ls", "env -u FOO echo hi", "git status"]
# Commands the guards must already gate without approval; the first one that is actually gated
# becomes the control, and the prefix-chain probes wrap it. (Some strings are built by joining
# pieces so that this file itself does not trip the guards when it is edited.)
CONTROL_CANDIDATES = ["git push origin main", "git " + "reset --" + "hard HEAD",
                      "systemctl --user stop ifapp-probe", "crontab -r", "sudo true"]
CONTROL_GATED = CONTROL_CANDIDATES[0]


def run_hook(name, command, transcript):
    payload = {"tool_name": "Bash", "tool_input": {"command": command},
               "transcript_path": transcript, "cwd": PROBE_CWD,
               "hook_event_name": "PreToolUse", "session_id": "probe"}
    r = subprocess.run([sys.executable, os.path.join(HOOKS, name)], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=30)
    return r.returncode


def discover_gates(transcript):
    """Guard hooks that gate a control command (names only). Tries each candidate in turn."""
    global CONTROL_GATED, PROBES_GATED
    for cand in CONTROL_CANDIDATES:
        CONTROL_GATED = cand
        found = _gates_for(transcript)
        if found:
            PROBES_GATED = [f"env -u FOO {cand}", f"nice -n 5 {cand}", f"timeout 30 {cand}"]
            return found
    return []


def _gates_for(transcript):
    found = []
    for fn in sorted(os.listdir(HOOKS)):
        if not fn.endswith(".py") or fn == MODULE or ".bak" in fn:
            continue
        # Only guard-type hooks are probed; others (lesson/brief/session hooks) may have side effects.
        if not any(k in fn for k in ("guard", "gate", "approv", "scope", "fence", "preflight")):
            continue
        try:
            if run_hook(fn, CONTROL_GATED, transcript) == 2:
                found.append(fn)
        except Exception:
            pass
    return found


GATES = []


def probe(transcript):
    bad = []
    if not GATES:
        bad.append(("CONTROL: no hook gates a plain git push in this probe setup", CONTROL_GATED))
    for c in PROBES_DANGEROUS:
        if run_hook("block-dangerous.py", c, transcript) != 2:
            bad.append(("block-dangerous.py must block", c))
    for c in PROBES_GATED:
        if GATES and all(run_hook(g, c, transcript) != 2 for g in GATES):
            bad.append((f"{'/'.join(GATES)} must gate (no approval)", c))
    for c in PROBES_SAFE:
        for h in ["block-dangerous.py"] + GATES:
            if run_hook(h, c, transcript) == 2:
                bad.append((f"{h} must allow", c))
    return bad


def main():
    for t in ["block-dangerous.py"]:
        if not os.path.exists(os.path.join(HOOKS, t)):
            print(f"[stop] {t} not found in {HOOKS}")
            return 1
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        # one human message without any approval line
        epoch_stamp = datetime.fromtimestamp(0, timezone.utc).isoformat().replace("+00:00", "Z")
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "probe"},
                            "origin": {"kind": "human"}, "sessionId": "probe",
                            "timestamp": epoch_stamp}) + "\n")
        transcript = f.name
    hooks = sorted(fn for fn in os.listdir(HOOKS) if fn.endswith(".py") and ".bak" not in fn)
    print(f"[hooks] {', '.join(hooks)}")
    GATES[:] = discover_gates(transcript)
    print(f"[gates] control '{CONTROL_GATED}' is gated by: {', '.join(GATES) or '(none)'}")
    before = probe(transcript)
    print(f"[probe-before] {len(before)} problem(s)")
    for why, c in before:
        print(f"   - {why}: {c}")
    if CHECK:
        return 0 if not before else 3
    if any(why.startswith("CONTROL") for why, _ in before):
        print("[stop] probe setup cannot see the gate; nothing changed")
        return 5
    if not before:
        print("[ok] nothing to fix")
        return 0
    backups = {}
    try:
        with open(os.path.join(HOOKS, MODULE), "w", encoding="utf-8") as f:
            f.write(MODULE_SRC)
        for t in ["block-dangerous.py"] + GATES:
            path = os.path.join(HOOKS, t)
            src = open(path, encoding="utf-8").read()
            if MARK in src:
                print(f"[ok]   {t}: already patched")
                continue
            m = ANCHOR_RE.search(src)
            insert = INSERT.format(ind=m.group("ind")) if m else None
            if not m:
                m = JSON_RE.search(src)
                if m:
                    insert = INSERT_JSON.format(ind=m.group("ind"), var=m.group("var"))
            if not m:
                raise RuntimeError(f"{t}: no anchor (command line or stdin JSON parse) found — not patched")
            if "import sys" not in src:
                raise RuntimeError(f"{t}: no 'import sys' — not patched")
            shutil.copy2(path, path + ".bak-v1prefix")
            backups[t] = path + ".bak-v1prefix"
            end = m.end() + 1
            src = src[:end] + insert + src[end:]
            open(path, "w", encoding="utf-8").write(src)
            print(f"[done] {t}: patched (backup {t}.bak-v1prefix)")
        after = probe(transcript)
        print(f"[probe-after] {len(after)} problem(s)")
        for why, c in after:
            print(f"   - {why}: {c}")
        if after:
            raise RuntimeError("self-test failed")
        print("[ok] all probes pass")
        return 0
    except Exception as e:
        print(f"[rollback] {e}")
        for t, b in backups.items():
            shutil.copy2(b, os.path.join(HOOKS, t))
            print(f"   restored {t}")
        return 4


if __name__ == "__main__":
    sys.exit(main())
