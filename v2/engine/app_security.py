"""App-wide security gate: run once per app, after
every task's own run plus the final acceptance pass, strictly before a
release's publish request is ever submitted (engine.full_orbit calls
check() there; passed=False means needs_owner "security_failed",
never a publish request).

Three independent checks, each best-effort and reported in `ran` only
when it actually ran something:

  (a) secret scan -- every git-tracked file of the project's repo (never
      an untracked/build-artifact path a .gitignore already keeps out of
      the real tree) is scanned line by line for a private key block, a
      handful of well-known token prefixes, or a generic password
      literal -- `password = "..."` (plain assignment, any language) as
      well as `"password": "..."` / `password: "..."` (a JSON object
      member or a JS object-literal property, quoted or bare key, comma
      or newline after the value) -- both separators are the SAME
      finding kind ("password_literal"); JSON/JS syntax is not a
      separate category, just another way the same secret shape is
      written. A hit is reported as "secret:<kind>:<path>:<line>" -- the
      matched VALUE itself is never included in a finding or printed
      anywhere.
  (b) dependency audit -- one check per ecosystem manifest actually
      present, never limited to Node. "dependency_audit" runs whenever
      the repo has a package.json; "python_dependency_audit" runs
      whenever it has a requirements.txt or pyproject.toml (both can
      run in the same check() call for a repo that has both kinds of
      manifest). Each reaches exactly one of four states, recorded
      explicitly rather than folded into a single boolean:
        "passed"         -- the audit command ran and exited 0.
        "vulnerabilities" -- the audit command ran, exited nonzero, AND
                             its own JSON output actually reports a
                             finding; ALWAYS fails this check,
                             regardless of any allow_* flag below
                             (neither flag ever suppresses a real
                             finding).
        "unavailable"     -- the command could not even run at all
                             (binary missing, timed out) OR ran and
                             exited nonzero for its own execution
                             reasons -- a registry/network/DNS failure
                             (`npm audit --json`'s own {"error": {...}}
                             shape) or output that was not even
                             parsable JSON, never a real finding -- OR
                             no audit command is configured/available at
                             all (a Node app with no audit_cmd in
                             config, or a Python manifest with no
                             pip-audit binary on PATH and no
                             "security".python_audit_cmd override);
                             fails unless "security".
                             allow_audit_unavailable is explicitly true
                             -- the SAME flag covers both ecosystems,
                             since "the audit tool could not even run"
                             is the same kind of gap either way.
        "no_lockfile"     -- Node only: a package.json exists but no
                             package-lock.json does; a Node app with no
                             committed lockfile has nothing for any
                             audit_cmd to actually check, so this is
                             never folded into "passed" by omission --
                             it is its own recorded failure
                             ("audit_skipped:no_lockfile") unless
                             "security".allow_no_lockfile is explicitly
                             true. Python manifests have no equivalent
                             state here: requirements.txt/pyproject.toml
                             ARE the thing being audited, there is no
                             separate committed-lock precondition this
                             module checks.
      Each state's own finding string (if any) is appended to
      `findings`, and each ecosystem's own "*_dependency_audit" name is
      always added to `ran` whenever its manifest exists, regardless of
      which state was reached -- "ran" means "this category was
      evaluated", not "the command itself executed".
  (c) plan security-requirement coverage -- every plan.requirements[*]
      entry whose kind is "security" must be named in at least one
      plan.scenes[*].covers list. Plan objects built before this field
      existed simply have no requirements at all, so this check adds
      nothing to `ran` and finds nothing wrong -- "absent -> no check".
      By the time check() is ever called the job's acceptance has
      already passed (engine.acceptance.run_acceptance refuses to
      accept with zero scenes or any scene failing), so every scene
      named in a `covers` list has already demonstrably passed; nothing
      here re-runs or re-verifies that.

Reads "security" config from the raw JSON file at config.config_path
itself (engine/config.py now explicitly allows this top-level key to
load, with no corresponding dataclass field -- see that module's own
comment) -- absent file, unreadable JSON, or no "security" key at all
is simply treated as {} (no audit_cmd configured, allow_audit_unavailable/
allow_no_lockfile default to False).

record() appends one app_security.recorded ledger event per check()
call -- {passed, findings, ran, fingerprint} -- so a later release
gate can read back what this check found and bind to the
exact tree it found it on, without re-running the scan itself."""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine import evidence
from engine.config import EngineConfig
from engine.gitutil import git_argv
from engine.ledger import Event, Ledger

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"
)
_TOKEN_PREFIX_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{10,}|ghp_[A-Za-z0-9]{10,}|AKIA[0-9A-Z]{12,}"
    r"|xoxb-[A-Za-z0-9-]{10,})\b"
)
# Matches both a plain assignment (`password = "..."`) and a JSON
# object member / JS object-literal property (`"password": "..."` or
# the bare-key form `password: "..."`) -- `[:=]` is the only
# difference between those two shapes, everything else about the
# literal is identical, so both are the one "password_literal" finding
# kind (see this module's docstring).
_PASSWORD_LITERAL_RE = re.compile(r"[\"']?password[\"']?\s*[:=]\s*['\"][^'\"]{6,}")

# name -> pattern, checked against every line of every git-tracked file.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", _PRIVATE_KEY_RE),
    ("token_prefix", _TOKEN_PREFIX_RE),
    ("password_literal", _PASSWORD_LITERAL_RE),
)


@dataclass(frozen=True)
class SecurityResult:
    passed: bool
    findings: tuple[str, ...]
    ran: tuple[str, ...]
    # The repo's own tree hash (engine.evidence.fingerprint) at the
    # moment check() ran, over the SAME excludes as everything else
    # that fingerprints this project's repo. "" iff it could not be
    # computed (evidence.FingerprintError) -- that itself always fails
    # the check (see check()) rather than being silently treated as
    # "no finding"; a later caller must never treat "" as "the tree
    # this result applies to is unambiguous."
    fingerprint: str = ""


def _tracked_files(repo: Path) -> tuple[str, ...]:
    result = subprocess.run(
        git_argv(repo, "ls-files", "-z"), check=True, capture_output=True,
    )
    names = result.stdout.decode("utf-8", "surrogateescape").split("\0")
    return tuple(n for n in names if n)


def _scan_secrets(repo: Path) -> tuple[str, ...]:
    findings: list[str] = []
    for rel in _tracked_files(repo):
        path = repo / rel
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue  # a tracked symlink to nowhere, a submodule gitlink, ...
        for lineno, line in enumerate(text.splitlines(), start=1):
            for name, pattern in _SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append(f"secret:{name}:{rel}:{lineno}")
    return tuple(findings)


def _security_config(config: EngineConfig) -> dict[str, Any]:
    if config.config_path is None:
        return {}
    try:
        raw = json.loads(config.config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    security = raw.get("security")
    return security if isinstance(security, dict) else {}


_AUDIT_STATES = ("passed", "vulnerabilities", "unavailable", "no_lockfile")


def _run_audit_cmd(repo: Path, audit_cmd: list[str]) -> tuple[str, str]:
    """Returns (status, detail): status is "ok" (exit 0), "vulnerabilities"
    (ran fine AND its own JSON output actually reports a finding), or
    "unavailable" (could not even run it at all -- binary missing, timed
    out -- OR it ran and exited nonzero but its own stdout is not a real
    finding: `npm audit --json`'s own {"error": {...}} shape for a
    registry/network/DNS execution failure, or output that is not even
    parsable JSON at all). A nonzero exit alone is never enough to call
    something a vulnerability -- `npm audit` itself uses the exact same
    nonzero exit for "the tool could not reach the registry" as it does
    for "it reached the registry and found something", and only the
    JSON body (never the exit code) tells those two apart."""
    try:
        result = subprocess.run(
            audit_cmd, cwd=str(repo), capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "unavailable", f"audit_unavailable:{type(exc).__name__}: {exc}"
    if result.returncode == 0:
        return "ok", ""
    tail = ((result.stdout or "") + (result.stderr or ""))[-2000:]
    try:
        report = json.loads(result.stdout or "")
    except ValueError:
        return "unavailable", f"audit_unavailable:unparsable_output:{tail}"
    if not isinstance(report, dict):
        return "unavailable", f"audit_unavailable:unparsable_output:{tail}"
    error = report.get("error")
    if isinstance(error, dict):
        summary = error.get("summary") or error.get("code") or "unknown"
        return "unavailable", f"audit_unavailable:execution_error:{summary}"
    return "vulnerabilities", f"dependency_audit_failed:{tail}"


def _dependency_audit_state(repo: Path, security_config: dict[str, Any]) -> tuple[str, str]:
    """One of _AUDIT_STATES, plus its finding string ("" for "passed").
    Only called once check() has already established `repo` is a Node
    app (package.json exists) -- see that function. Checks the
    lockfile first: an audit_cmd has nothing meaningful to check
    against if package-lock.json was never committed, so "no_lockfile"
    takes priority over "no audit_cmd configured"/running anything."""
    if not (repo / "package-lock.json").exists():
        return "no_lockfile", "audit_skipped:no_lockfile"
    audit_cmd = security_config.get("audit_cmd")
    if not (isinstance(audit_cmd, list) and audit_cmd
            and all(isinstance(part, str) for part in audit_cmd)):
        return "unavailable", "audit_unavailable:no_audit_cmd_configured"
    status, detail = _run_audit_cmd(repo, list(audit_cmd))
    if status == "ok":
        return "passed", ""
    if status == "unavailable":
        return "unavailable", detail
    return "vulnerabilities", detail


def _apply_audit_state(state: str, detail: str, security_config: dict[str, Any],
                       findings: list[str]) -> bool:
    """Shared by both ecosystems' "passed"/"vulnerabilities"/
    "unavailable" states (NOT "no_lockfile", which is Node-only and has
    its own allow_no_lockfile flag -- the caller handles that state
    itself before ever reaching here). Appends `detail` to `findings`
    when there is one and returns whether this check still counts as
    passed: "vulnerabilities" is never suppressed by any flag,
    "unavailable" is suppressed only by "security".
    allow_audit_unavailable (the one flag covering both ecosystems --
    see this module's docstring)."""
    if state == "passed":
        return True
    findings.append(detail)
    if state == "vulnerabilities":
        return False
    return bool(security_config.get("allow_audit_unavailable", False))


def _default_python_audit_cmd(repo: Path) -> list[str] | None:
    """pip-audit's own documented invocation, scoped to the APP's own
    declared dependencies -- never None (see below) ever reads the
    engine's own installed environment, which is not the thing under
    audit. `-r <file>` is the only way pip-audit can be pointed at a
    specific dependency set rather than whatever happens to be
    installed wherever it runs; a requirements.txt is the only manifest
    format this function can resolve that way. Returns None when there
    is no requirements.txt to pass -- a pyproject.toml-only app (no
    resolvable requirements/lock file pip-audit's `-r` flag accepts)
    has nothing default to run; see _python_dependency_audit_state's
    own handling of that None, which fails closed rather than falling
    back to a bare, unscoped `pip-audit` call (an earlier version of
    this function did exactly that for a pyproject-only app, which
    audited the ENGINE's own environment and could certify a clean
    pass for a totally unaudited app). `--progress-spinner off` keeps
    stdout pure JSON (the spinner otherwise writes to stdout on a
    non-tty, which would break the JSON parse below)."""
    if (repo / "requirements.txt").exists():
        return ["pip-audit", "-r", "requirements.txt", "--format", "json",
                "--progress-spinner", "off"]
    return None


def _python_dependency_audit_state(repo: Path, security_config: dict[str, Any]) -> tuple[str, str]:
    """One of _AUDIT_STATES minus "no_lockfile" (python manifests have
    no separate committed-lock precondition -- see this module's
    docstring), plus its finding string ("" for "passed"). Only called
    once check() has already established `repo` has a requirements.txt
    or pyproject.toml. "security".python_audit_cmd overrides the
    default pip-audit invocation (a test's own stand-in, same pattern
    as "security".audit_cmd for Node, and the only way to audit a
    pyproject-only app at all -- e.g. pointed at a poetry/pdm export)
    -- absent that override, _default_python_audit_cmd's own None (no
    requirements.txt to scope pip-audit to) is "unavailable", failing
    closed rather than running anything unscoped; _run_audit_cmd's own
    OSError handling is what turns a genuinely missing pip-audit binary
    into "unavailable" the rest of the time."""
    audit_cmd = security_config.get("python_audit_cmd")
    if not (isinstance(audit_cmd, list) and audit_cmd
            and all(isinstance(part, str) for part in audit_cmd)):
        default_cmd = _default_python_audit_cmd(repo)
        if default_cmd is None:
            return "unavailable", "audit_unavailable:no_resolvable_python_manifest"
        audit_cmd = default_cmd
    status, detail = _run_audit_cmd(repo, list(audit_cmd))
    if status == "ok":
        return "passed", ""
    if status == "unavailable":
        return "unavailable", detail
    return "vulnerabilities", detail


def _get(obj: Any, name: str) -> Any:
    """getattr/dict-get helper: plan/requirement/scene objects passed in
    may be engine.plan dataclasses (once they grow requirements/covers
    fields) or plain dicts (e.g. a test's stand-in) -- either is read the
    same way."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def check(config: EngineConfig, project: str, attempt_id: str,
          plan: object) -> SecurityResult:
    project_config = config.projects[project]
    repo = project_config.repo

    findings: list[str] = []
    ran: list[str] = []
    passed = True

    ran.append("secret_scan")
    secret_findings = _scan_secrets(repo)
    if secret_findings:
        findings.extend(secret_findings)
        passed = False

    security_config = _security_config(config)
    if (repo / "package.json").exists():
        ran.append("dependency_audit")
        state, detail = _dependency_audit_state(repo, security_config)
        if state == "no_lockfile":
            findings.append(detail)
            if not bool(security_config.get("allow_no_lockfile", False)):
                passed = False
        elif not _apply_audit_state(state, detail, security_config, findings):
            passed = False

    if (repo / "requirements.txt").exists() or (repo / "pyproject.toml").exists():
        ran.append("python_dependency_audit")
        state, detail = _python_dependency_audit_state(repo, security_config)
        if not _apply_audit_state(state, detail, security_config, findings):
            passed = False

    fingerprint = ""
    try:
        fingerprint = evidence.fingerprint(repo, project_config.excludes)
    except evidence.FingerprintError:
        findings.append("no_fingerprint")
        passed = False

    requirements = _get(plan, "requirements") or ()
    security_req_ids = [
        _get(req, "id") for req in requirements if _get(req, "kind") == "security"
    ]
    if security_req_ids:
        ran.append("plan_security_coverage")
        covered: set[object] = set()
        for scene in (_get(plan, "scenes") or ()):
            covered.update(_get(scene, "covers") or ())
        for req_id in security_req_ids:
            if req_id not in covered:
                findings.append(f"security_requirement_uncovered:{req_id}")
                passed = False

    return SecurityResult(passed=passed, findings=tuple(findings), ran=tuple(ran),
                          fingerprint=fingerprint)


def record(ledger: Ledger, project: str, attempt_id: str,
          result: SecurityResult) -> int:
    """Append one app_security.recorded event ({passed, findings, ran,
    fingerprint}) and return its seq -- the caller runs check() first
    and passes its result straight through; this function does not
    re-run anything. Secret-free by construction: `result.findings`
    entries already never carry a matched secret VALUE (see
    _scan_secrets), only kind/path/line, so nothing here needs to
    redact anything further before it is written to the ledger."""
    return ledger.append(Event(
        project=project, kind="app_security.recorded", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={
            "passed": result.passed, "findings": list(result.findings),
            "ran": list(result.ran), "fingerprint": result.fingerprint,
        },
    ))


__all__ = ["SecurityResult", "check", "record"]
