#!/usr/bin/env python3
"""PreToolUse hook (Bash | Edit | Write | MultiEdit): irreversible or out-of-scope actions need
the owner's approval, found in the owner's own chat transcript for THIS session (see below).

Gated (need "승인 <TASK>" typed by the owner within the configured window, in THIS session):
  - stopping/disabling/masking services, crontab edits, tailscale serve changes
  - git push, git reset --hard, git clean -f, git branch -D, git checkout -- .
  - package installs (npm/pnpm/yarn/pip/uv add|install), apt/snap
  - deletions: rm -r, find -delete, mv out of a project folder to an archive/trash folder,
    DROP/TRUNCATE/DELETE FROM
  - sudo, kill -9/pkill/killall
  - editing systemd unit files, /etc, a reverse-proxy config, this harness's own settings,
    marking a work-queue item done
Always blocked (no gate, ever): touching the files inside the ACTUAL installed hooks/gate
directory (cfg["installed_hooks_dir"] / cfg["legacy_gate_dir"] - see gongbang_config.py), by
path or, for a Bash command, by any common way of spelling that same real path.

The approval gate is not a file. A path-string blocklist on a writable gate file can be
defeated (a path assembled with "./", a python -c write, an env-var path, a symlink, ...) -
the model's own tool calls can always eventually forge a thing the hook itself trusts. So this
hook trusts nothing any tool call could have written. It instead reads the owner's OWN chat
transcript for this exact session_id straight off disk
(cfg["transcripts_root"]/*/<session_id>.jsonl, a file only the agent harness itself appends
to) and replays every "승인 <name>" / "승인 취소" the owner actually typed in the configured
window, in the order it was typed (see fence_parse.py). No session_id, no matching transcript,
or a read/parse failure -> fail closed (deny), never open.

Shell tokenizing uses shlex with punctuation_chars=True (handles quoting correctly: a ';'
inside a quoted git -c value is not treated as a command separator; '>' / '&&' / '|' become
their own tokens so "git push>/dev/null" is still seen as a two-token "git push"). Commands
piped into a nested shell ("bash -c '...'", "sh -c \"...\"") are recursively re-analyzed, but
only when that IS the segment's own first command - "echo bash -c 'git push'" (bash is just
echo's argument, never executed) is not unwrapped and gated.

git global-option skipping skips any leading "-..." token (covers -P, --no-pager aliases,
etc) so an unrecognized global flag cannot hide the subcommand; git subcommand matching also
recognizes "branch -d --force" (not just -D) and rm recognizes GNU long-option prefixes
("--rec", "--recu", ... "--recursive"). A gated program name (git, rm, sudo, npm, systemctl,
...) is only recognized as the ACTUAL command being run - i.e. the first token of a shell
segment (after var=value assignments and a short list of command-runner prefixes like
sudo/env/xargs/exec) - not merely mentioned anywhere in the text, so "cat
README-that-mentions-git-push.md" is not gated while "FOO=1 git push" and "sudo git push" are.

GATED_PATH checks run per-token (after shlex + ~/$HOME expansion), not only as a regex over
the whole command string, so quoting around the target path ("$HOME/.claude/settings.json")
does not defeat the "^/etc/" or "$" anchors. It fires only on an actual WRITE target (a shell
redirection, or the destination/victim argument of
tee/cp/mv/install/ln/rm/chmod/chown/truncate/`sed -i`/`perl -i`/`dd of=`) - a plain read
(cat/less/grep/head/ls/stat/...) of a sensitive path is not gated. Edit/Write/MultiEdit's own
file_path is always checked (that tool call IS a write).

Hook-name / gate-dir "always blocked" checks are scoped to the Bash command text and the
Edit/Write/MultiEdit file_path ONLY - not to new_string/content bodies - so writing
documentation that merely mentions "approve-gate.py" is not blocked. always_block_hit() only
protects files that are ACTUALLY under the configured cfg["installed_hooks_dir"] /
cfg["legacy_gate_dir"] (resolved via realpath for Edit/Write; via a path-aware regex - the
literal absolute path, or, if that path lives under the real home directory, common
home-relative spellings `~`/`$HOME`/`${HOME}`/`/home/<user>`/`/root` - for Bash). A same-named
file anywhere else (a repo's own checkout of this project, for example) is ordinary work, not
a protected install path.

MultiEdit/Edit/Write work-queue completion-mark detection simulates the edit against the
actual file content on disk and compares the mark count before/after, so a split
old/new_string boundary cannot dodge detection. A malformed/unparseable stdin payload denies
(fail-closed). The whole analysis is wrapped in try/except: any unexpected error while
deciding gate status results in a deny, never a silent allow.

Known, accepted limits (see README.md "이 장치가 맡는 범위"): this is not a full POSIX shell
parser. Deeply nested pipelines/subshells beyond one level of "sh -c" are not unwrapped.
Command substitution ($(...) / `...`), eval, sourcing and heredocs ARE handled, but only as a
flat text-level backstop: if one of those constructs appears anywhere in the command together
with an already-gated keyword (OPAQUE_GATE_KEYWORDS, drawn from the gate reasons implemented
above), the whole command is gated ("모르면 막는다") even though the substitution itself is
never actually unwrapped and evaluated - a command with neither signal is left alone (no new
over-blocking of plain "echo $(date)"). These degrade towards MORE false positives/negatives in
unusual shapes, not towards trusting a forged approval record.

An approval name that matches one of cfg["apps_root"]'s own project-folder names
(case-insensitive) is project-scoped: it only grants gated actions whose target path/cwd
resolves inside that one project folder (see _task_grants below and
docs/approval-gate.md "프로젝트 범위"). Any other approval name stays session-wide, as before.
"""
import json
import os
import re
import shlex
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fence_parse  # noqa: E402
import gongbang_config  # noqa: E402

GATED_PATH = [
    (r"/\.config/systemd/user/", "systemd 유닛 파일"),
    (r"^/etc/", "/etc 설정"),
    (r"(^|/)(Caddyfile|nginx\.conf)$", "리버스 프록시 설정"),
    (r"/\.claude/settings(\.local)?\.json$", "하네스 설정"),
    (r"/\.env(\.|$)", "환경 변수 파일"),
]

ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
ALLOWED_PREFIXES = {"exec", "xargs", "env", "nohup", "nice", "time", "command"}
NESTED_SHELLS = {"bash", "sh", "zsh", "dash", "ash"}
SEPARATORS = {";", "&&", "||", "|", "&"}

GIT_GLOBAL_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


def _basename(tok: str) -> str:
    return tok.rsplit("/", 1)[-1]


def _tokenize(command: str) -> list[str] | None:
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


def _first_command(tokens: list[str]) -> tuple[str | None, int]:
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


def _expand_nested_shell_segment(segment: list[str], depth: int = 0) -> list[list[str]]:
    """If THIS segment's own first command (per _first_command) is bash/sh/zsh/..., finds its
    "-c '...'" argument and returns the inner command's own segments (recursively). Anchored
    to the segment's ACTUAL first command, not "any bash/sh token anywhere in the whole
    command line" - so "echo bash -c 'git push'" (where "bash" is just echo's argument, never
    executed) is not wrongly unwrapped and flagged, while "bash -c 'git push'" (where bash IS
    the first command) still is."""
    if depth > 3:
        return []
    name, i = _first_command(segment)
    if name not in NESTED_SHELLS:
        return []
    rest = segment[i + 1:]
    j = 0
    while j < len(rest) and rest[j] != "-c":
        j += 1
    if j + 1 >= len(rest):
        return []
    inner_cmd = rest[j + 1]
    inner_tokens = _tokenize(inner_cmd)
    if inner_tokens is None:
        return []
    inner_segments = _split_segments(inner_tokens)
    extra = list(inner_segments)
    for seg in inner_segments:
        extra.extend(_expand_nested_shell_segment(seg, depth + 1))
    return extra


def _git_hit(segment: list[str]) -> str | None:
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
            j += 1  # any other global flag (e.g. -P, --no-pager, --bare, ...), no value
            continue
        break
    if j >= len(segment):
        return None
    sub = segment[j]
    rest = segment[j + 1:]
    if sub == "push":
        return "git push"
    if sub == "reset" and "--hard" in rest:
        return "git 되돌리기·삭제"
    if sub == "clean" and any(re.match(r"^-[a-zA-Z]*f", r) or r == "--force" for r in rest):
        return "git 되돌리기·삭제"
    if sub == "branch":
        has_delete = "-D" in rest or "--delete" in rest or "-d" in rest
        forced = "-D" in rest or "--force" in rest or "-f" in rest
        if has_delete and forced:
            return "git 되돌리기·삭제"
    if sub == "checkout" and "--" in rest and "." in rest:
        return "git 되돌리기·삭제"
    return None


def _rm_hit(segment: list[str]) -> str | None:
    name, i = _first_command(segment)
    if name != "rm":
        return None
    for t in segment[i + 1:]:
        if t == "--":
            break
        if t == "--recursive" or (t.startswith("--") and "recursive".startswith(t[2:]) and len(t) > 2):
            return "재귀 삭제"
        if re.match(r"^-[a-zA-Z]+$", t) and re.search(r"[rR]", t[1:]):
            return "재귀 삭제"
    return None


SIMPLE_PROGRAM_GATES = [
    ({"systemctl"}, r"\b(stop|disable|mask|enable)\b", "서비스 정지·해제·활성"),
    ({"tailscale"}, r"\bserve\b", "원격 접속 매핑 변경"),
    ({"npm", "pnpm", "yarn"}, r"\b(install|add|i|remove|uninstall)\b", "패키지 설치·제거"),
    ({"pip", "pip3", "uv"}, r"\b(install|add|remove)\b", "파이썬 패키지 설치"),
    ({"apt", "apt-get", "snap"}, r"\b(install|remove|purge)\b", "시스템 패키지"),
    ({"pkill", "killall"}, r"", "프로세스 강제 종료"),
]


def _simple_program_hit(segment: list[str]) -> str | None:
    name, i = _first_command(segment)
    if name is None:
        return None
    rest_text = " ".join(segment[i + 1:])
    for names, pat, why in SIMPLE_PROGRAM_GATES:
        if name in names and (pat == "" or re.search(pat, rest_text, re.I)):
            return why
    if name == "sudo":
        return "sudo 실행"
    if name == "kill" and re.search(r"-9\b", rest_text):
        return "프로세스 강제 종료"
    if name == "crontab" and re.search(r"(^-[er]$|^-$|\.txt$)", " ".join(segment[i + 1:i + 2]) or ""):
        return "crontab 변경"
    if name == "find" and "-delete" in segment[i + 1:]:
        return "find -delete"
    # Generic archive/trash-folder move keyword, no environment-specific path baked in.
    if name == "mv" and any(re.search(r"(archive|\.trash|/tmp)", t, re.I) for t in segment[i + 1:]):
        return "작업 폴더를 보관함으로 이동"
    return None


# These look for dangerous CONTENT (often inside a single quoted SQL argument), not a
# program name, so they are checked as plain text over the whole segment.
TEXT_PATTERNS = [
    (r"\b(DROP|TRUNCATE)\s+(TABLE|DATABASE|SCHEMA)\b|\bDELETE\s+FROM\b", "DB 삭제"),
    (r"\bpsql\b.*-c\s*[\"'].*\b(drop|truncate|delete)\b", "DB 삭제"),
]


def _text_hits(segment_text: str) -> set[str]:
    return {why for pat, why in TEXT_PATTERNS if re.search(pat, segment_text, re.I)}


# GATED_PATH is checked only against a genuine WRITE target - a plain READ of a sensitive path
# ("cat /etc/hosts") is not gated. A write target is a redirection target (> / >>), or an
# argument position the specific command treats as a destination/victim path.
WRITE_LAST_ARG_COMMANDS = {"cp", "mv", "install", "ln"}
WRITE_SKIP_FIRST_ARG_COMMANDS = {"chmod", "chown", "truncate"}
WRITE_ALL_ARGS_COMMANDS = {"rm", "tee", "touch"}
WRITE_REDIRECT_OPS = {">", ">>"}

# sed/perl in-place-edit flag spellings: the long GNU form ("--in-place", optionally
# "=SUFFIX"), and any single-dash bundle of short options that includes "i" - "-i", "-i.bak",
# "-pi" (perl's common "-pi -e" idiom), "-npi", "-ni", etc. A bundled option's own i-extension
# suffix (the ".bak" in "-i.bak") is attached directly after the "i" with no separator, so it
# is stripped before checking for other flag letters.
_INPLACE_LONG_RE = re.compile(r"^--in-place(=.*)?$")


def _has_inplace_flag(tokens: list[str]) -> bool:
    for t in tokens:
        if t.startswith("--"):
            if _INPLACE_LONG_RE.match(t):
                return True
            continue
        if not t.startswith("-") or len(t) < 2:
            continue
        flags_part = t[1:].split(".", 1)[0]
        if "i" in flags_part:
            return True
    return False


def _split_redirect_targets(segment: list[str]) -> tuple[list[str], list[str]]:
    """Splits `segment` into (redirect_targets, core_tokens): core_tokens is the segment with
    every redirection operator and its target removed, so a command's own destination-argument
    inference below (cp/mv/touch/... "last/all non-flag argument") only ever sees that
    command's REAL arguments - not a redirection target that happens to sit after them on the
    same line (`cp new.py main.py > docs/copy.log` must still recognize "main.py" as cp's own
    write target, not just "docs/copy.log")."""
    redirect_targets: list[str] = []
    core: list[str] = []
    skip_next = False
    for idx, t in enumerate(segment):
        if skip_next:
            skip_next = False
            continue
        if t in WRITE_REDIRECT_OPS:
            if idx + 1 < len(segment):
                redirect_targets.append(segment[idx + 1])
            skip_next = True
            continue
        core.append(t)
    return redirect_targets, core


def _write_target_tokens(segment: list[str]) -> list[str]:
    """Returns the tokens of `segment` that name a path this command/redirection actually
    WRITES to (or deletes), as opposed to merely reads or prints. Reading commands (cat, less,
    grep, head, ls, stat, a plain `python3 script.py <path>` read, ...) contribute nothing
    here, so they are never gated by GATED_PATH - only Edit/Write/MultiEdit's own file_path
    (handled separately in `_run`, since that tool genuinely writes it) and these."""
    redirect_targets, core = _split_redirect_targets(segment)
    targets: list[str] = list(redirect_targets)
    name, i = _first_command(core)
    rest = core[i + 1:] if i >= 0 else []
    non_flags = [t for t in rest if not t.startswith("-")]
    if name in WRITE_ALL_ARGS_COMMANDS:
        targets.extend(non_flags)
    elif name in WRITE_LAST_ARG_COMMANDS:
        if non_flags:
            targets.append(non_flags[-1])
    elif name in WRITE_SKIP_FIRST_ARG_COMMANDS:
        targets.extend(non_flags[1:])
    elif name == "dd":
        targets.extend(t[len("of="):] for t in rest if t.startswith("of="))
    elif name in ("sed", "perl") and _has_inplace_flag(rest):
        # sed -i / perl -i <script> <file...> - skip the script argument, rest are targets.
        if len(non_flags) >= 2:
            targets.extend(non_flags[1:])
    return targets


def _path_token_hits(tokens: list[str]) -> set[str]:
    hits: set[str] = set()
    for t in tokens:
        stripped = t.strip("'\"")
        expanded = os.path.expanduser(os.path.expandvars(stripped))
        for pat, why in GATED_PATH:
            if re.search(pat, expanded):
                hits.add(why)
    return hits


# ---------------------------------------------------------------------------
# Opaque constructs (command substitution, eval, sourcing, heredocs, ...): this hook does not
# structurally unwrap these (see docs/command-parsing.md). Rather than silently letting a
# substituted gated command through, "모르면 막는다" - when one of these constructs appears
# ANYWHERE in the command text together with an already-gated keyword (drawn from the program
# names / git subcommands / text patterns already checked above, as a flat constant), the whole
# command is treated as gated. Without an accompanying gate keyword this adds no hit at all, so
# ordinary substitution-free-of-danger commands ("echo $(date)", "ls $(pwd)") are not
# over-blocked.
# ---------------------------------------------------------------------------

OPAQUE_CONSTRUCT_RE = re.compile(
    r"\$\(|`|\beval\b|\bsource\s+\S|(?:^|[;&|]\s*)\.\s+\S|<<[A-Za-z'\"_-]|\bsh\s+-c\s+\"\$"
)

# Flat constant pulled from the gate reasons already implemented above (git subcommands, rm,
# SIMPLE_PROGRAM_GATES program names, TEXT_PATTERNS keywords) - not a newly invented list.
OPAQUE_GATE_KEYWORDS = {
    "push", "reset", "clean", "branch", "checkout",
    "rm",
    "systemctl", "tailscale",
    "npm", "pnpm", "yarn", "pip", "pip3", "uv", "apt", "apt-get", "snap",
    "sudo", "kill", "pkill", "killall", "crontab", "find", "mv",
    "drop", "truncate", "delete",
}
_OPAQUE_KEYWORD_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in OPAQUE_GATE_KEYWORDS) + r")\b", re.I
)


def _opaque_construct_hit(command: str) -> str | None:
    if not command or not OPAQUE_CONSTRUCT_RE.search(command):
        return None
    if not _OPAQUE_KEYWORD_RE.search(command):
        return None
    return "명령 치환·eval 등 해석 불가 + 관문 대상 낱말 포함"


def command_hits(command: str) -> set[str]:
    hits: set[str] = set()
    opaque = _opaque_construct_hit(command)
    if opaque:
        hits.add(opaque)
    tokens = _tokenize(command)
    if tokens is None:
        # Malformed quoting: fall back to a coarse text-level backstop so broken shell syntax
        # can't quietly slip past the (now unavailable) token-based checks.
        for pat, why in [
            (r"\bgit\b.*\bpush\b", "git push"),
            (r"\bgit\b.*\b(reset\s+--hard|clean\s+-f|branch\s+-D|checkout\s+--\s+\.)\b", "git 되돌리기·삭제"),
            (r"\brm\s+(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\b", "재귀 삭제"),
            (r"\bsudo\b", "sudo 실행"),
        ]:
            if re.search(pat, command, re.I):
                hits.add(why)
        hits |= _text_hits(command)
        return hits

    segments = _split_segments(tokens)
    for seg in list(segments):
        segments.extend(_expand_nested_shell_segment(seg))

    for seg in segments:
        g = _git_hit(seg)
        if g:
            hits.add(g)
        r = _rm_hit(seg)
        if r:
            hits.add(r)
        s = _simple_program_hit(seg)
        if s:
            hits.add(s)
        hits |= _path_token_hits(_write_target_tokens(seg))
        hits |= _text_hits(" ".join(seg))
    return hits


# ---------------------------------------------------------------------------
# Always-blocked (no gate, ever): the ACTUAL installed hooks/gate directory only. A file
# elsewhere that merely shares a hook's name (a repo copy, a staging/dev folder) is ordinary
# work, so this is scoped to a real, resolved path - not a bare-word search of the whole
# filesystem.
# ---------------------------------------------------------------------------

def _under(path: str, parent: str) -> bool:
    try:
        real = os.path.realpath(os.path.expanduser(os.path.expandvars(path)))
        parent_real = os.path.realpath(os.path.expanduser(os.path.expandvars(parent)))
    except Exception:
        return False
    return real == parent_real or real.startswith(parent_real + os.sep)


def _home_relative_suffix(path: str) -> str | None:
    """If `path` lives under the real home directory, returns its path relative to home
    (e.g. ".claude/hooks"); otherwise None. Used only to ALSO recognize common home-relative
    spellings (~, $HOME, ...) of that same real path in Bash command TEXT - the literal
    absolute path is always checked regardless."""
    try:
        home = os.path.realpath(os.path.expanduser("~"))
        real = os.path.realpath(os.path.expanduser(os.path.expandvars(path)))
    except Exception:
        return None
    if real == home:
        return ""
    if real.startswith(home + os.sep):
        return real[len(home) + 1:]
    return None


def _mentions_dir_re(path: str) -> re.Pattern | None:
    """Regex matching any common way a Bash command might spell `path` as a directory
    prefix: the literal absolute path, or - if it happens to live under the real home
    directory - '~'/'$HOME'/'${HOME}'/'/home/<user>'/'/root' followed by the same
    home-relative suffix. Returns None if `path` itself cannot be resolved at all."""
    try:
        abs_real = os.path.realpath(os.path.expanduser(os.path.expandvars(path))).rstrip("/")
    except Exception:
        return None
    if not abs_real:
        return None
    patterns = [re.escape(abs_real)]
    suffix = _home_relative_suffix(path)
    if suffix:
        patterns.append(r"(?:~|\$HOME|\$\{HOME\}|/home/[^/\s'\"]+|/root)/" + re.escape(suffix))
    elif suffix == "":
        patterns.append(r"(?:~|\$HOME|\$\{HOME\})")
    return re.compile(r"(?:" + "|".join(patterns) + r")\b")


def always_block_hit(cmd: str, path: str, cwd: str, cfg: dict) -> str | None:
    """Protects only the files ACTUALLY inside cfg["installed_hooks_dir"] /
    cfg["legacy_gate_dir"] - not a bare hook-name substring anywhere in the filesystem."""
    hooks_dir = cfg.get("installed_hooks_dir") or ""
    gate_dir = cfg.get("legacy_gate_dir") or ""
    hook_name_re = fence_parse.hook_name_pattern(cfg.get("extra_protected_hook_names"))

    if path:
        if hooks_dir and _under(path, hooks_dir):
            return "집행 훅 자체는 대표 승인 없이 고치지 않는다"
        if gate_dir and _under(path, gate_dir):
            return "승인 관문은 모델이 손댈 수 없다"
        return None

    if not cmd:
        return None

    if gate_dir:
        gate_re = _mentions_dir_re(gate_dir)
        if gate_re and gate_re.search(cmd):
            return "승인 관문은 모델이 손댈 수 없다"
    if hooks_dir:
        hooks_re = _mentions_dir_re(hooks_dir)
        if hooks_re and hooks_re.search(cmd) and hook_name_re.search(cmd):
            return "집행 훅 자체는 대표 승인 없이 고치지 않는다"
        # Covers `cd <hooks_dir> && python3 approve_gate.py`: the hook name never appears
        # alongside the directory path in one contiguous string, but the tool call's own cwd
        # already resolves inside the real hooks directory.
        if cwd and _under(cwd, hooks_dir) and hook_name_re.search(cmd):
            return "집행 훅 자체는 대표 승인 없이 고치지 않는다"
    return None


# ---------------------------------------------------------------------------
# Work-queue "[x]" completion-mark detection via edit simulation
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
    """Compares basenames, not full paths, so a renamed work-queue file (cfg["work_queue_path"]
    pointed somewhere else) is still recognized without a code change."""
    configured = str(cfg.get("work_queue_path") or "")
    basename = os.path.basename(configured) if configured else "WORK_QUEUE.md"
    return os.path.basename(path) == basename


def simulate_work_queue_completion(tool: str, inp: dict, cfg: dict) -> bool:
    """Returns True if applying this edit would INCREASE the number of "[x]" marks in the
    target file compared to what's on disk now (catches split old/new_string boundaries)."""
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


def bash_work_queue_hit(cmd: str, cfg: dict) -> bool:
    """Returns True if `cmd` both targets the work-queue file through an actual Bash WRITE
    (shell redirection, tee, cp/mv/install/ln destination, or `sed -i`/`perl -i` in place -
    see _write_target_tokens above) and the command text contains the literal "[x]" mark.
    Covers marking a task done through Bash (`sed -i 's/\\[ \\]/[x]/' WORK_QUEUE.md`,
    `echo '[x] TASK1' >> WORK_QUEUE.md`, ...), not only through
    simulate_work_queue_completion()'s Edit/Write/MultiEdit path above - reusing the same
    tokenizer/segment-splitter/write-target extraction already used for GATED_PATH so the two
    code paths cannot drift apart.

    The "[x]" text and the write-target check are both scoped to the SAME segment, not to the
    whole command string - `grep -F '[x]' WORK_QUEUE.md; echo '[ ] TASK2' >> WORK_QUEUE.md` is
    an ordinary read (of an existing mark) plus an unrelated append, not a completion mark,
    even though the literal "[x]" and a write to WORK_QUEUE.md both appear somewhere in the
    command text."""
    tokens = _tokenize(cmd)
    if tokens is None:
        return False
    segments = _split_segments(tokens)
    for seg in list(segments):
        segments.extend(_expand_nested_shell_segment(seg))
    for seg in segments:
        if "[x]" not in " ".join(seg):
            continue
        for t in _write_target_tokens(seg):
            stripped = t.strip("'\"")
            expanded = os.path.expanduser(os.path.expandvars(stripped))
            if _is_work_queue_path(expanded, cfg):
                return True
    return False


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Project-scoped approval: an approval name that happens to match one of cfg["apps_root"]'s own
# project-folder names (case-insensitive) only opens the gate for a tool call whose target path
# or cwd resolves INSIDE that one project folder - see docs/approval-gate.md "프로젝트 범위".
# Any other approval name keeps the old, session-wide behavior (opens every gated action in
# this session, regardless of what the label says) - splitting that apart for every possible
# label would need a much bigger redesign (README "이 장치가 맡는 범위").
# ---------------------------------------------------------------------------

class _ProjectLookupError(Exception):
    """apps_root itself cannot be listed (missing config, permission, I/O). The caller must treat
    the approval as granting NOTHING (fail closed), never as "not a project name -> session-wide"."""


def _project_dir_for(task: str, cfg: dict) -> str | None:
    """Returns the real project folder path under cfg["apps_root"] whose basename matches
    `task` case-insensitively, or None if `task` is not a project-folder name at all (an
    ordinary human-chosen label, handled as session-wide)."""
    apps_root = cfg.get("apps_root") or ""
    if not apps_root:
        return None  # project scoping not configured at all -> every approval is session-wide
    try:
        entries = os.listdir(apps_root)
    except (FileNotFoundError, NotADirectoryError):
        return None  # no apps folder yet (fresh install) == an empty one -> no project names exist
    except Exception as exc:
        raise _ProjectLookupError(str(exc)) from exc  # exists but unreadable -> fail closed
    for name in entries:
        if name.lower() == task.lower():
            candidate = os.path.join(apps_root, name)
            if os.path.isdir(candidate):
                return candidate
    return None


def _task_grants(task: str, cfg: dict, path: str, cwd: str) -> bool:
    try:
        project_dir = _project_dir_for(task, cfg)
    except _ProjectLookupError:
        return False  # scope could not be verified -> this approval grants nothing
    if project_dir is None:
        return True  # not a project-folder name -> ordinary session-wide approval
    target = path or cwd
    if not target:
        return False  # project-scoped approval, but nothing to scope the action against -> deny
    return _under(target, project_dir)


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
    session_id = str(data.get("session_id", "") or "")

    cmd = str(inp.get("command", "")) if tool == "Bash" else ""
    path = str(inp.get("file_path", "")) if tool in ("Edit", "Write", "MultiEdit") else ""
    cwd = str(data.get("cwd", "") or "")

    always = always_block_hit(cmd, path, cwd, cfg)
    if always:
        return deny(f"차단: {always}.")

    hits: set[str] = set()
    if cmd:
        hits |= command_hits(cmd)
        if bash_work_queue_hit(cmd, cfg):
            hits.add("작업판 완료 표시")
    if path:
        hits |= {why for pat, why in GATED_PATH if re.search(pat, path)}
        if tool in ("Edit", "Write", "MultiEdit") and simulate_work_queue_completion(tool, inp, cfg):
            hits.add("작업판 완료 표시")

    if not hits:
        return 0

    ok, open_tasks = fence_parse.compute_open_tasks(session_id, cfg)
    if ok and open_tasks:
        granting = {t for t in open_tasks if _task_grants(t, cfg, path, cwd)}
        if granting:
            label = ", ".join(sorted(granting))
            print(f"승인 관문 열림({label}) — 허용: {', '.join(sorted(hits))}", file=sys.stderr)
            return 0

    if not session_id:
        reason_suffix = " (session_id 없음 — 세션 대화 기록을 확인할 수 없어 차단)"
    elif not ok:
        reason_suffix = " (이 세션의 대화 기록을 찾거나 읽지 못해 차단)"
    elif open_tasks:
        reason_suffix = (
            " (승인은 있으나 이 행동엔 적용되지 않음: "
            + ", ".join(sorted(open_tasks)) + " — 프로젝트 폴더 이름과 같은 승인은 그 폴더 안에만 적용됨)"
        )
    else:
        reason_suffix = ""
    return deny(
        "대표 승인 필요: " + ", ".join(sorted(hits)) + reason_suffix
        + ". 지금은 실행하지 말고, 무엇을 왜 하려는지 한 줄로 보고한 뒤 대표가 채팅에 '승인 <TASK>'를 입력할 때까지 기다린다."
    )


def main() -> int:
    # If the hook can't even parse its own stdin, it has no idea whether this tool call was
    # gated - that must deny, not silently allow (which a bare `return 0` on exception would).
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
