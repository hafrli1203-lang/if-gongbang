#!/usr/bin/env python3
# Generated from the private ops workspace at b94af62b97af64e2e58e8c102fa2538f066ae366; do not edit by hand (see harness/export_ops_public.py).
"""PreToolUse hook (Edit|Write|MultiEdit|NotebookEdit|Bash|Agent): block work aimed at an app
outside the owner's current scope (CURRENT_SCOPE.json) -- AND, new in this revision (team-lead
2026-11-02, v2 gap bundle 4, items 105/110/111/158/159/167/214), fence a work-copy (wt-new.sh)
session into its own `.wt.scope` allow-list in real time, instead of only catching violations
after the fact via scope-check.sh.

Owner directives G19 ("개발 툴부터 정립하는게 우선이다 자꾸 개발부터 급하게 할려고 하지마라") /
G25 ("다른 앱은 이프 공방 개발 끝나고 보기로 했잖아") were, per the 2026-10-27 전수조사, "글로만
있음(막는 장치 없음)" -- this hook is that device. It is additive to, not a replacement for,
scope-guard.py (infra paths: settings.json/systemd/.env/...) and preflight-gate.py (pre-change
report) -- all three run independently. scope-check.sh (run at wt-land) remains the authoritative
post-hoc check; this hook is the realtime half, so a violation is refused before it ever lands in
a commit, not just caught afterwards.

=== Part 1: CURRENT_SCOPE.json app allow-list (unchanged from the 2026-10-27 revision) ===
Scope is a plain allow-list the owner sets with `set_scope.py`, run by the owner's own typed `!`
command (never a model tool call -- this hook denies every model-side write to CURRENT_SCOPE.json
itself, unconditionally, the same "no gate, ever" posture scope-guard.py gives its own hook files).
No CURRENT_SCOPE.json yet, or it is unreadable/empty -> no restriction (nothing has been scoped,
so nothing is blocked by this part of the hook).

Blocked once a scope exists:
  - Edit/Write/MultiEdit/NotebookEdit whose file_path sits under /apps/<app>/ for an <app> not
    in the allow-list (symlinks are resolved first).
  - a Bash `gb2 run|fix|continue|change|new <app>` call for an out-of-scope <app>.
  - a Bash command that WRITES into an out-of-scope /apps/<app>/ path.
  - an Agent tool_use whose prompt/description/name names another real /apps/<app> directory
    that is not in scope.
See the 2026-10-27 revision's comments (kept below, unit tests pin them) for exact edge cases.

=== Part 2: work-copy session fence (new) ===
A "work-copy session" is a Claude Code session operating inside one wt-new.sh copy
(/apps/_wt/<app>/<name>) -- detected ONLY from the env var WT_COPY (set by wt-run.sh for
every command it runs inside a copy). A `cwd` sitting under WT_ROOT is deliberately NOT treated
as a work-copy session on its own: a first cut of this hook did, and it produced a real false
positive the same day it shipped (team-lead 2026-11-02 live report) -- the lead's own session had
simply `cd`d into a copy to look at it (WT_COPY unset) and an ordinary `bash deploy.sh app > $L`
there was denied as "범위 밖 쓰기". `cwd` reflects where a command happened to run, not whose job
it is; only wt-run.sh setting WT_COPY means "this command IS the job, inside its own sandboxed
copy". A session that is NOT a work-copy session by that test (the lead's own session merely
looking around, an ordinary app repo) is entirely unaffected by this part -- it returns None
immediately.

Inside a work-copy session, this part enforces in real time what scope-check.sh otherwise only
catches after a commit:
  - WRITES (Edit/Write/MultiEdit/NotebookEdit file_path, and Bash commands that write via
    redirection/tee/sed -i/cp/mv/rm/touch/truncate/patch/mkdir/python open(...,'w'/'a')) are
    allowed only inside the copy's own directory, and only onto a path that is either (a) a
    copy-local artifact that is not part of the job's own deliverable (node_modules/, .verify/,
    .next-wt*/, .wt.json, .wt.scope, .wt.done, .pids/) or (b) matched by a glob line in the
    copy's own `<copy>/.wt.scope` file (the exact glob language scope-check.sh uses, ported here
    so both tools agree on what "in scope" means). A write under Claude's own scratchpad
    (/tmp/claude-...) is always allowed -- the harness itself tells every agent to use it for
    temp files, and it is outside any copy's deliverable.
  - READS are only narrowly restricted (this hook is not a general confidentiality boundary --
    the worker is expected to read sibling app code for context): denied only for `.env`* files,
    the shared external-token store (/data/exampleapp/integration-tokens/), another work
    copy's directory under WT_ROOT, and the Claude Code project-memory store (~/.claude/projects/
    -- another session's transcripts/memory, not this job's business).
  - A Bash command this hook cannot parse (shlex failure on any `;`/`&&`/`||`/`|`-separated
    segment) fails CLOSED while inside a work-copy session (and only there -- Part 1's existing
    behavviour for a non-copy session is untouched): better to block an odd one-off command than
    let an unparseable write slip through the fence. A write target that shlex DID tokenize but
    that is a shell variable this hook cannot resolve (`$VAR`/`${VAR}`, no access to the live
    shell environment) is the opposite case -- "판정 불가" (team-lead 2026-11-02 correction) --
    and is simply left unchecked rather than denied or guessed at; `~` IS resolved (deterministic,
    no session state needed) before that check runs.
Known limit (same "don't chase a same-privilege AI with text checks forever" call as Part 1):
redirection/tee/sed/cp/mv target extraction is a practical token scan, not a full shell grammar --
a sufficiently exotic one-liner (command substitution building a path at runtime, eval, etc.) can
still evade it. This is defense-in-depth layered on top of scope-check.sh's post-hoc commit check,
not a claim of a perfect sandbox.
Off switch (owner only, shared with Part 1): ~/.claude/gate/scope-fence-off. Fails open on any
internal error OTHER than a work-copy Bash command that failed to parse (fail-closed, above) and
the CURRENT_SCOPE.json self-protection (also fail-closed, unconditional, off-switch or not).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:  # optional: a sibling work-copy may add shared constants here; never required
    import gongbang_config as _gcfg  # type: ignore
except Exception:
    _gcfg = None

SCOPE_FILE = os.environ.get("SCOPE_FENCE_FILE") or "/apps/_ops/CURRENT_SCOPE.json"
APPS_ROOT = os.environ.get("SCOPE_FENCE_APPS_ROOT") or "/apps"
WT_ROOT = (
    os.environ.get("SCOPE_FENCE_WT_ROOT")
    or os.environ.get("WT_ROOT")
    or getattr(_gcfg, "WT_ROOT", None)
    or "/apps/_wt"
)
TOKEN_DIR = (
    os.environ.get("SCOPE_FENCE_TOKEN_DIR")
    or getattr(_gcfg, "TOKEN_DIR", None)
    or "/data/exampleapp/integration-tokens"
)
CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
SCRATCH_PREFIX = "/tmp/claude"
OFF = os.path.expanduser("~/.claude/gate/scope-fence-off")
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
WRITE_SIGNAL = re.compile(
    r"(?<![<>&0-9])>>?(?!&)|\btee\b|\bsed\s+(-[a-zA-Z]*i|--in-place)|\bperl\s+-[a-zA-Z]*i|\b(mv|cp|rm|touch|truncate|patch|mkdir)\s"
    r"|\bgit\s+(apply|checkout|restore|am)\b|write_text\(|open\([^)]*['\"][wa]['\"]"
)
GB2_RUN = re.compile(r"\bgb2(?:\.py)?\b[^;&|\n]*?\b(run|fix|continue|change|new)\b\s+([A-Za-z0-9_.-]+)")
GB2_ROOT_OVERRIDE = re.compile(r"\bGB2_APPS_ROOT\s*=")
QUOTED_SPAN = re.compile(r"'[^']*'|\"[^\"]*\"")
_PAREN_RE = re.compile(r"[()]")
CD_PREFIX = re.compile(r"^\s*cd\s+(\S+)\s*(?:&&|;)")
ENV_FILE_RE = re.compile(r"(^|/)\.env(\.[\w.-]+)?$")
ENV_FILE_IN_CMD_RE = re.compile(r"(?:^|[\s'\"/])\.env(\.[\w.-]+)?(?:[\s'\"]|$)")
COPY_ARTIFACT_RE = re.compile(
    r"^(node_modules(/.*)?|\.wt\.(json|scope|done)(\.tmp)?|\.next-wt[^/]*(/.*)?|\.verify(/.*)?|\.pids(/.*)?)$"
)


# ---------------------------------------------------------------------------
# Part 1 helpers (CURRENT_SCOPE.json app allow-list) -- unchanged logic
# ---------------------------------------------------------------------------

def _apps_prefix() -> str:
    return APPS_ROOT.rstrip("/") + "/"


def app_of(path: str) -> str | None:
    p = os.path.realpath(os.path.normpath(path))
    prefix = _apps_prefix()
    try:
        prefix = os.path.realpath(prefix.rstrip("/")) + "/"
    except OSError:
        pass
    if not p.startswith(prefix):
        return None
    rest = p[len(prefix):]
    return rest.split("/", 1)[0] if rest else None


def load_scope() -> list[str] | None:
    try:
        with open(SCOPE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        allow = data.get("allow")
        return [str(a) for a in allow] if isinstance(allow, list) and allow else None
    except Exception:
        return None


def _scope_file_hit(path: str, cmd: str) -> bool:
    scope_norm = os.path.normpath(SCOPE_FILE)
    if path and os.path.normpath(path) == scope_norm:
        return True
    if not cmd or scope_norm not in cmd:
        return False
    esc = re.escape(scope_norm)
    patterns = (
        r"(>>?|\btee\b|\bsed\s+-[a-zA-Z]*i|\bcp\b|\bmv\b|\brm\b)[^\n]*" + esc,
        r"\bopen\(\s*['\"]" + esc + r"['\"]\s*,\s*['\"][wa]",
        r"\bdd\b[^\n]*\bof=" + esc,
    )
    return any(re.search(p, cmd) for p in patterns)


def _effective_cwd(cmd: str, cwd: str) -> str:
    m = CD_PREFIX.match(cmd)
    if not m or not cwd:
        return cwd
    target = m.group(1).strip("'\"")
    return os.path.normpath(os.path.join(cwd, target))


def _bash_targets(cmd: str, cwd: str) -> list[str]:
    if not WRITE_SIGNAL.search(cmd):
        return []
    unquoted = QUOTED_SPAN.sub(lambda m: " " * len(m.group(0)), cmd)
    abs_path = re.compile(re.escape(_apps_prefix()) + r"[^\s'\"`;|&)]+")
    cands = abs_path.findall(unquoted)
    if not cands:
        eff_cwd = _effective_cwd(cmd, cwd)
        if eff_cwd:
            cands = [os.path.join(eff_cwd, "_")]
    return cands


def _agent_app_hit(prompt: str, allow: list[str]) -> str | None:
    try:
        names = [n for n in os.listdir(APPS_ROOT) if os.path.isdir(os.path.join(APPS_ROOT, n))]
    except Exception:
        return None
    allow_set = set(allow)
    abs_path = re.compile(re.escape(_apps_prefix()) + r"([^\s'\"`;|&)/]+)")
    for m in abs_path.finditer(prompt):
        name = m.group(1)
        if name and name not in allow_set and len(name) >= 3 and not name.startswith(".") and _is_real_app(name):
            return name
    for name in names:
        if name in allow_set or len(name) < 3 or name.startswith("."):
            continue
        if re.search(r"(?<![\w/-])" + re.escape(name) + r"(?![\w-])", prompt):
            return name
    return None


def _is_real_app(name: str) -> bool:
    return os.path.isdir(os.path.join(APPS_ROOT, name))


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


# ---------------------------------------------------------------------------
# Part 2 helpers (work-copy session fence) -- new
# ---------------------------------------------------------------------------

def _is_scratch(path: str) -> bool:
    return path.startswith(SCRATCH_PREFIX)


def _wt_root_rp() -> str:
    try:
        return os.path.realpath(WT_ROOT.rstrip("/"))
    except Exception:
        return WT_ROOT.rstrip("/")


def copy_root_of(cwd: str) -> str | None:
    """Work-copy root this session belongs to, or None when this is not a work-copy session.

    WT_COPY (set by wt-run.sh on every command it runs inside a copy) is the ONLY signal used.
    An earlier revision also treated a `cwd` sitting under WT_ROOT as a work-copy session; that
    produced a real false positive (team-lead 2026-11-02 live report): the LEAD's own session had
    merely `cd`d into a copy to look at it (WT_COPY unset) and a plain `bash deploy.sh app > $L`
    there was denied as "범위 밖 쓰기". A tool's cwd reflects where a command happened to run, not
    who owns the job -- only wt-run.sh setting WT_COPY means "this command IS the job inside its
    sandboxed copy". `cwd` is accepted as a parameter for call-site/test stability but is no
    longer consulted."""
    env_copy = os.environ.get("WT_COPY")
    if not env_copy:
        return None
    try:
        rp = os.path.realpath(env_copy)
        if os.path.isdir(rp):
            return rp
    except Exception:
        pass
    return None


def _forbidden_read_path(path: str, copy_root: str) -> bool:
    """.env*, the shared token store, ~/.claude/projects, or another work copy's directory."""
    if not path:
        return False
    try:
        real = os.path.realpath(os.path.normpath(path))
    except Exception:
        real = os.path.normpath(path)
    if ENV_FILE_RE.search(real):
        return True
    token_rp = os.path.realpath(TOKEN_DIR) if os.path.exists(TOKEN_DIR) else TOKEN_DIR
    if real == token_rp or real.startswith(token_rp + "/"):
        return True
    proj_rp = os.path.realpath(CLAUDE_PROJECTS_DIR) if os.path.exists(CLAUDE_PROJECTS_DIR) else CLAUDE_PROJECTS_DIR
    if real == proj_rp or real.startswith(proj_rp + "/"):
        return True
    root_rp = _wt_root_rp()
    if real.startswith(root_rp + "/") and not (real == copy_root or real.startswith(copy_root + "/")):
        return True
    return False


def _forbidden_read_in_cmd(cmd: str, copy_root: str) -> str | None:
    """Same four categories as _forbidden_read_path, scanned as plain text in a Bash command
    (a `cat`/`grep`/`head` of a secret does not need a write signal to be a problem)."""
    if not cmd:
        return None
    if ENV_FILE_IN_CMD_RE.search(cmd):
        return ".env"
    if TOKEN_DIR in cmd:
        return TOKEN_DIR
    if CLAUDE_PROJECTS_DIR in cmd:
        return CLAUDE_PROJECTS_DIR
    root = WT_ROOT.rstrip("/")
    for m in re.finditer(re.escape(root) + r"/([^/\s'\"`;|&)]+)/([^/\s'\"`;|&)]+)", cmd):
        other = os.path.join(root, m.group(1), m.group(2))
        try:
            other_rp = os.path.realpath(other)
        except Exception:
            other_rp = other
        if other_rp != copy_root:
            return other
    return None


def _scope_glob_lines(copy_root: str) -> list[str]:
    try:
        with open(os.path.join(copy_root, ".wt.scope"), encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
    except Exception:
        return []


def _glob_to_re(glob: str) -> re.Pattern:
    """Ported from scope-check.sh's inline Python `to_re` so both tools agree on what a
    `.wt.scope` glob line matches: * = one path segment, ** = any depth, trailing / = everything
    under that folder."""
    g = glob.strip()
    if g.startswith("./"):
        g = g[2:]
    if g.endswith("/"):
        g += "**"
    out, i = "", 0
    while i < len(g):
        if g.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif g.startswith("**", i):
            out += ".*"
            i += 2
        elif g[i] == "*":
            out += "[^/]*"
            i += 1
        elif g[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(g[i])
            i += 1
    return re.compile(out + r"\Z")


def _copy_write_allowed(rel: str, copy_root: str) -> bool:
    if COPY_ARTIFACT_RE.match(rel):
        return True
    for g in _scope_glob_lines(copy_root):
        if _glob_to_re(g).match(rel):
            return True
    return False


def _split_segments(cmd: str) -> list[str]:
    """Split on top-level `;`, `&&`, `||`, `|`, `\\n` -- the shell operators between separately
    shlex-tokenizable commands -- without touching anything inside a quoted span."""
    segs: list[str] = []
    buf: list[str] = []
    quote = None
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if quote:
            buf.append(c)
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            buf.append(c)
            i += 1
            continue
        if c in ";|&\n":
            if c in "|&" and i + 1 < n and cmd[i + 1] == c:
                segs.append("".join(buf))
                buf = []
                i += 2
                continue
            segs.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    segs.append("".join(buf))
    return [s.strip() for s in segs if s.strip()]


_REDIR_TOKEN_RE = re.compile(r"^\d*(>>?|&>>?)&?\d*(.*)$")
_REDIR_OP_RE = re.compile(r"\d*(?:>>|&>>|&>|>)")
_WRITE_CMD_WORDS = {"mv", "cp", "rm", "touch", "truncate", "patch", "mkdir"}
_UNEXPANDABLE_VAR_RE = re.compile(r"\$\{?[A-Za-z_]")
_VAR_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_CMD_WRAPPERS = {"command", "env", "exec", "nice", "nohup", "sudo", "time", "stdbuf", "ionice"}
_CD_STANDALONE_RE = re.compile(r"^\s*cd\s+(\S+)\s*$")


class _BashParseError(Exception):
    pass


def _normalize_redirs(seg: str) -> str:
    """shlex.split only splits on whitespace/quotes, so `echo hi>file` or `cmd 2>file` (operator
    glued to the next word, no space) stays ONE token and the redirection is invisible to the
    per-token scan below (Codex 2026-11-02 review, P1: `echo hi>outside.ts` passed unexamined).
    Inserts spaces around a `>`/`>>`/`&>`/`&>>` (with its optional leading fd digits) that sits
    OUTSIDE any quoted span, found via the same length-preserving quote-masking trick
    QUOTED_SPAN.sub uses elsewhere, so offsets into the ORIGINAL string stay valid and nothing
    inside a quote (where `>` is literal text, not an operator) is touched."""
    masked = QUOTED_SPAN.sub(lambda m: " " * len(m.group(0)), seg)
    out, last = [], 0
    for m in _REDIR_OP_RE.finditer(masked):
        out.append(seg[last:m.start()])
        out.append(" " + seg[m.start():m.end()] + " ")
        last = m.end()
    out.append(seg[last:])
    return "".join(out)


def _expand_vars(tok: str, env_vars: dict[str, str]) -> str:
    """Substitute a `$VAR`/`${VAR}` this segment (or an earlier one in the same command) itself
    assigned a literal value to -- the bypass Codex found live: `L=outside.ts; echo x > "$L"`
    passed because the target was seen as an un-resolvable variable and waved through. Only
    SIMPLE `NAME=literal` assignments collected by `_collect_assignments` are known; anything
    else is left as-is for `_resolve_target`'s normal "cannot judge" handling."""
    prev = tok
    for _ in range(4):  # cap chained substitution (A=$B; B=$C; ...) against runaway loops
        new = _VAR_REF_RE.sub(lambda m: env_vars.get(m.group(1) or m.group(2), m.group(0)), prev)
        if new == prev:
            return new
        prev = new
    return prev


def _collect_assignments(tokens: list[str], env_vars: dict[str, str]) -> int:
    """Registers every leading `NAME=value` token (`A=1 B=2 cmd ...`) into `env_vars` and skips
    past any leading command-wrapper word (`command`/`env`/`sudo`/...) so the *effective* command
    word is found underneath it -- Codex P1: `command rm x` / `/usr/bin/rm x` / `env rm x` /
    `LC_ALL=C rm x` all slipped past the old plain `tokens[0] == "rm"` check. Returns the index of
    the effective command word (len(tokens) if none remain)."""
    i = 0
    while i < len(tokens):
        m = _ASSIGN_RE.match(tokens[i])
        if m:
            env_vars[m.group(1)] = _expand_vars(m.group(2), env_vars)
            i += 1
            continue
        if tokens[i] in _CMD_WRAPPERS:
            i += 1
            continue
        break
    return i


def _resolve_target(tok: str, eff_cwd: str, env_vars: dict[str, str]) -> str | None:
    """Resolve one write-target token to an absolute path, or None when it cannot be judged.

    Order: expand any `$VAR` this command itself assigned a literal value to, THEN `~` (always
    deterministic), THEN check for a STILL-unresolved `$VAR`/`${VAR}` (no access to the shell's
    live environment beyond what this command assigned) -- that remainder is "판정 불가"
    (team-lead 2026-11-02 correction: must not be denied over a path this hook cannot see, and
    never treated as a parse failure, which stays reserved for a genuine shlex error)."""
    tok = os.path.expanduser(_expand_vars(tok, env_vars))
    if _UNEXPANDABLE_VAR_RE.search(tok):
        return None
    if tok.startswith("/"):
        return tok
    if eff_cwd:
        return os.path.normpath(os.path.join(eff_cwd, tok))
    return tok


def _cp_mv_targets(head: str, tokens: list[str], add) -> None:
    """`cp`/`mv` destination extraction, Codex P1 fixes:
      - `-t DIR`/`--target-directory[= ]DIR` (GNU coreutils): the real destination is DIR, not
        whatever non-flag token happens to come last (`cp -t /tmp src/allowed.ts` was reading
        `src/allowed.ts` as the destination and missing that /tmp was ever touched).
      - `mv` DELETES every source it moves: a source outside the copy is a write (a deletion)
        there too, not just a read of it (`mv outside.ts src/allowed.ts` only checked the
        destination before)."""
    target_dir = None
    sources: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("-t", "--target-directory"):
            if i + 1 < len(tokens):
                target_dir = tokens[i + 1]
            i += 2
            continue
        if t.startswith("--target-directory="):
            target_dir = t.split("=", 1)[1]
            i += 1
            continue
        if t.startswith("-t") and len(t) > 2:
            target_dir = t[2:]
            i += 1
            continue
        if t.startswith("-"):
            i += 1
            continue
        sources.append(t)
        i += 1
    if target_dir is not None:
        add(target_dir)
        if head == "mv":
            for s in sources:
                add(s)
    elif len(sources) >= 2:
        add(sources[-1])
        if head == "mv":
            for s in sources[:-1]:
                add(s)


def _segment_write_targets(seg: str, cwd: str, env_vars: dict[str, str]) -> list[str]:
    """Best-effort extraction of paths one shell segment writes to. Raises _BashParseError on a
    shlex failure (unbalanced quote, etc.) -- the caller fails closed in a work-copy session."""
    try:
        tokens = shlex.split(_normalize_redirs(seg), posix=True)
    except ValueError as exc:
        raise _BashParseError(str(exc)) from exc
    if not tokens:
        return []
    eff_cwd = _effective_cwd(seg, cwd)
    targets: list[str] = []

    def add(tok: str) -> None:
        r = _resolve_target(tok, eff_cwd, env_vars)
        if r is not None:
            targets.append(r)

    # redirection: a token that is purely `>`/`>>`/`&>` with the path as the NEXT token, or a
    # token like `>file`/`2>file` carrying the path glued on (rare after _normalize_redirs, which
    # already splits most of these, but a quoted or otherwise untouched one can still arrive this
    # way e.g. from _python_open_targets's caller never reaching here -- kept for safety).
    for idx, tok in enumerate(tokens):
        m = _REDIR_TOKEN_RE.match(tok)
        if m:
            rest = m.group(2)
            if rest:
                add(rest)
            elif idx + 1 < len(tokens):
                add(tokens[idx + 1])

    head_idx = _collect_assignments(tokens, env_vars)
    if head_idx >= len(tokens):
        return targets
    head = os.path.basename(tokens[head_idx])
    rest_tokens = [t for t in tokens[head_idx + 1:] if t and not t.startswith("-")]
    if head == "tee":
        for t in rest_tokens:
            add(t)
    elif head in ("sed", "perl") and (
        "-i" in tokens[head_idx:] or any(t.startswith("-i") for t in tokens[head_idx + 1:])
    ):
        # sed -i SCRIPT FILE...  (first non-flag arg after -i is the script, not a path, unless
        # -i took an inline suffix like -i.bak immediately followed by the script)
        if rest_tokens:
            for t in rest_tokens[1:]:
                add(t)
    elif head in ("mv", "cp"):
        _cp_mv_targets(head, tokens[head_idx + 1:], add)
    elif head in _WRITE_CMD_WORDS and rest_tokens:
        for t in rest_tokens:
            add(t)
    elif head == "git" and len(tokens) >= head_idx + 2 and tokens[head_idx + 1] in (
        "apply", "checkout", "restore", "am",
    ):
        if eff_cwd:
            targets.append(os.path.join(eff_cwd, "_"))
    return targets


def _python_open_targets(seg: str, cwd: str, env_vars: dict[str, str]) -> list[str]:
    """`open(path, "w"/"a")` / `write_text(...)` written inline (python -c, heredoc, ...): these
    are not shell tokens, so they are pulled out with a regex instead of shlex."""
    out = []
    for m in re.finditer(r"""open\(\s*['"]([^'"]+)['"]\s*,\s*['"][wa]""", seg):
        r = _resolve_target(m.group(1), cwd, env_vars)
        if r is not None:
            out.append(r)
    return out


def _bash_candidate_targets(cmd: str, cwd: str) -> list[str]:
    """Raises _BashParseError if any segment fails to shlex-tokenize.

    `cur_cwd` and `env_vars` are THREADED across the command's segments (split on `;`/`&&`/`||`/
    `|`) -- Codex P1 fix: a standalone `cd DIR` segment used to update nothing, so a later segment
    in the SAME command (`cd /apps/other && echo x > src/a`) kept resolving its relative path
    against the ORIGINAL cwd, not the directory the command actually `cd`d into."""
    targets: list[str] = []
    cur_cwd = cwd
    env_vars: dict[str, str] = {}
    for seg in _split_segments(cmd):
        try:
            seg_tokens = shlex.split(seg, posix=True)
        except ValueError:
            seg_tokens = None
        if seg_tokens:
            _collect_assignments(seg_tokens, env_vars)
        m = _CD_STANDALONE_RE.match(seg)
        if m:
            d = os.path.expanduser(_expand_vars(m.group(1).strip("'\""), env_vars))
            if not _UNEXPANDABLE_VAR_RE.search(d):
                cur_cwd = d if d.startswith("/") else (os.path.normpath(os.path.join(cur_cwd, d)) if cur_cwd else d)
            continue  # `cd` on its own writes nothing
        targets.extend(_segment_write_targets(seg, cur_cwd, env_vars))
        targets.extend(_python_open_targets(seg, cur_cwd, env_vars))
    return targets


def _copy_fence_edit(path: str, copy_root: str) -> dict | None:
    if _forbidden_read_path(path, copy_root):
        return _deny(f"보호 경로 쓰기 금지: {path}")
    if _is_scratch(path):
        return None
    real = os.path.realpath(os.path.normpath(path))
    if not (real == copy_root or real.startswith(copy_root + "/")):
        return _deny(f"사본 밖 쓰기 금지 -- 이 사본({copy_root}) 밖은 쓸 수 없다: {path}")
    rel = real[len(copy_root) + 1:] if real != copy_root else ""
    if not _copy_write_allowed(rel, copy_root):
        return _deny(f"범위 밖 쓰기 -- .wt.scope에 없는 경로: {rel or '.'}")
    return None


def _copy_fence_bash(cmd: str, cwd: str, copy_root: str) -> dict | None:
    hit = _forbidden_read_in_cmd(cmd, copy_root)
    if hit:
        return _deny(f"읽기 금지 경로가 명령에 있다: {hit}")
    if not WRITE_SIGNAL.search(cmd):
        return None
    # Subshell/grouping/command-substitution syntax -- `(`, `)`, `$(` -- is not something
    # _split_segments understands (it only splits on `;`/`&&`/`||`/`|`/newline), so a `cd` inside
    # a subshell loses its cwd tracking and a target can land outside the copy unnoticed. Codex
    # 2026-11-02 second-pass review found this live: `cd src && (cd ..; echo x > outside.ts)`
    # resolved to a mangled, still-inside-copy-looking path and was allowed. Team-lead verdict
    # (2026-11-02): P1 -- a write-signal command containing any paren is "판정 불가", and inside a
    # work-copy session that means deny, not allow (same posture as an unparseable shlex token).
    if _PAREN_RE.search(QUOTED_SPAN.sub(lambda m: " " * len(m.group(0)), cmd)):
        return _deny("괄호(서브셸/명령치환) 구문은 판정 불가 -- 사본 세션에서는 거부한다: " + cmd)
    try:
        cands = _bash_candidate_targets(cmd, cwd)
    except _BashParseError as exc:
        return _deny(f"사본 안에서 해석할 수 없는 명령은 거부한다(안전 우선): {exc}")
    for t in cands:
        try:
            real = os.path.realpath(os.path.normpath(t))
        except Exception:
            real = os.path.normpath(t)
        if _forbidden_read_path(real, copy_root):
            return _deny(f"보호 경로 쓰기 금지: {real}")
        if _is_scratch(real):
            continue
        if not (real == copy_root or real.startswith(copy_root + "/")):
            return _deny(f"사본 밖 쓰기 금지 -- 이 사본({copy_root}) 밖은 쓸 수 없다: {real}")
        rel = real[len(copy_root) + 1:] if real != copy_root else ""
        if not _copy_write_allowed(rel, copy_root):
            return _deny(f"범위 밖 쓰기 -- .wt.scope에 없는 경로: {rel or '.'}")
    return None


def copy_fence_check(data: dict) -> dict | None:
    """Entry point for Part 2. Returns None immediately when this is not a work-copy session.

    FAIL-CLOSED FIX (live incident, team-lead 2026-11-02 01:08): WT_COPY SET but not resolvable
    to a real directory (stale env var after wt-gc.sh reaped the copy, a typo, a race) used to
    fall all the way through to "not a copy session" -> zero restriction -> a session that is
    very much supposed to be sandboxed got to write anywhere, silently. WT_COPY being set at all
    is itself the "this is a sandboxed job" signal; if it cannot be verified, this now denies
    outright rather than guessing "session must not be sandboxed after all"."""
    cwd = str(data.get("cwd") or "")
    env_copy = os.environ.get("WT_COPY")
    if not env_copy:
        return None  # WT_COPY unset -> genuinely not a copy session (team-lead 2026-11-02 fix 1)
    copy_root = copy_root_of(cwd)
    if not copy_root:
        return _deny(f"WT_COPY가 가리키는 사본을 확인할 수 없다 -- 안전을 위해 거부(이 호출만): {env_copy}")

    tool = str(data.get("tool_name", ""))
    inp = data.get("tool_input") or {}

    if tool == "Read":
        rp = str(inp.get("file_path") or "")
        if rp and _forbidden_read_path(rp, copy_root):
            return _deny(f"읽기 금지 경로: {rp}")
        return None

    if tool in EDIT_TOOLS:
        path = str(inp.get("file_path") or inp.get("notebook_path") or "")
        if not path:
            return None
        return _copy_fence_edit(path, copy_root)

    if tool == "Bash":
        cmd = str(inp.get("command", ""))
        if not cmd:
            return None
        return _copy_fence_bash(cmd, cwd, copy_root)

    return None


# ---------------------------------------------------------------------------
# Combined decision
# ---------------------------------------------------------------------------

def decide(data: dict) -> dict | None:
    tool = str(data.get("tool_name", ""))
    inp = data.get("tool_input") or {}
    path = str(inp.get("file_path") or inp.get("notebook_path") or "") if tool in EDIT_TOOLS else ""
    cmd = str(inp.get("command", "")) if tool == "Bash" else ""

    if _scope_file_hit(path, cmd):
        return _deny("CURRENT_SCOPE.json은 운영자 전용 set_scope.py(!로 직접 실행)로만 바꾼다. 모델 도구로는 수정 금지.")

    off = os.path.exists(OFF)

    if not off:
        fenced = copy_fence_check(data)
        if fenced:
            return fenced

    if off:
        return None

    allow = load_scope()
    if allow is None:
        return None  # no scope set yet -> no restriction

    if tool in EDIT_TOOLS:
        app = app_of(path) if path else None
        if app and app not in allow:
            return _deny(f"범위 밖: {app}은 지금 허용 범위({', '.join(allow)}) 밖이다. CURRENT_SCOPE.json 확인.")
        return None

    if tool == "Bash":
        m = GB2_RUN.search(cmd)
        if m:
            sub, app = m.group(1), m.group(2)
            override = bool(GB2_ROOT_OVERRIDE.search(cmd))
            if app not in allow and not override and (sub == "new" or _is_real_app(app)):
                return _deny(f"범위 밖: gb2 {sub} {app}은 허용 범위({', '.join(allow)}) 밖이다.")
        for t in _bash_targets(cmd, str(data.get("cwd", ""))):
            app = app_of(t)
            if app and app not in allow:
                return _deny(f"범위 밖: {app} 쓰기는 허용 범위({', '.join(allow)}) 밖이다.")
        return None

    if tool == "Agent":
        prompt = " ".join(str(inp.get(k, "")) for k in ("prompt", "description", "name"))
        hit = _agent_app_hit(prompt, allow)
        if hit:
            return _deny(f"범위 밖: 작업조 지시에 다른 앱({hit}) 이름이 있다. 지금 범위({', '.join(allow)}) 밖이면 시키지 않는다.")
        return None
    return None


def main() -> int:
    try:
        data = json.load(sys.stdin)
        out = decide(data)
    except Exception:
        return 0
    if out:
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
