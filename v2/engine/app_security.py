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
      handful of well-known token prefixes, or a generic
      `password = "..."` literal. A hit is reported as
      "secret:<kind>:<path>:<line>" -- the matched VALUE itself is never
      included in a finding or printed anywhere.
  (b) dependency audit -- only when the repo has a package.json (a
      Node app at all; nothing else here claims to audit any other
      ecosystem's dependencies). Exactly one of four states, each
      recorded explicitly rather than folded into a single boolean:
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
                             no audit_cmd is configured at all for a
                             Node app; fails unless
                             "security".allow_audit_unavailable is
                             explicitly true.
        "no_lockfile"     -- a package.json exists but no
                             package-lock.json does; a Node app with no
                             committed lockfile has nothing for any
                             audit_cmd to actually check, so this is
                             never folded into "passed" by omission --
                             it is its own recorded failure
                             ("audit_skipped:no_lockfile") unless
                             "security".allow_no_lockfile is explicitly
                             true.
      Each state's own finding string (if any) is appended to
      `findings`, and "dependency_audit" is always added to `ran`
      whenever package.json exists, regardless of which of the four
      states was reached -- "ran" means "this category was evaluated",
      not "the command itself executed".
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
_PASSWORD_LITERAL_RE = re.compile(r"password\s*=\s*['\"][^'\"]{6,}")

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
        if state == "passed":
            pass
        elif state == "vulnerabilities":
            # The audit ran fine and found something -- no allow_* flag
            # ever suppresses a real finding.
            findings.append(detail)
            passed = False
        elif state == "unavailable":
            findings.append(detail)
            if not bool(security_config.get("allow_audit_unavailable", False)):
                passed = False
        else:  # "no_lockfile"
            findings.append(detail)
            if not bool(security_config.get("allow_no_lockfile", False)):
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
