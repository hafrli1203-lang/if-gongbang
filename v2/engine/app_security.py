"""App-wide security gate (s10 contract item 1): run once per app, after
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
  (b) dependency audit -- only when the repo has a package-lock.json.
      The command this engine config names under its own JSON's
      top-level "security".audit_cmd key is run inside the repo (e.g.
      ["npm", "audit", "--omit=dev", "--json"]); a nonzero exit because
      the audit tool actually found vulnerabilities always fails this
      check. A nonzero exit (or any exception) because the command
      itself could not run at all (offline, binary missing, timed out)
      is instead recorded as "audit_unavailable:..." and only fails the
      check if "security".allow_audit_unavailable is not explicitly
      true in that same config file -- fail closed by default.
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
itself (never a EngineConfig.security attribute -- engine/config.py is
outside this module's owned scope for this task) -- absent file, unreadable
JSON, or no "security" key at all is simply treated as {} (no audit_cmd
configured, allow_audit_unavailable defaults to False)."""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine.config import EngineConfig
from engine.gitutil import git_argv

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


def _run_audit_cmd(repo: Path, audit_cmd: list[str]) -> tuple[str, str]:
    """Returns (status, detail): status is "ok" (exit 0), "vulnerabilities"
    (ran fine, exited nonzero -- the audit tool actually found something),
    or "unavailable" (could not even run it: binary missing, no network,
    timed out)."""
    try:
        result = subprocess.run(
            audit_cmd, cwd=str(repo), capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "unavailable", f"audit_unavailable:{type(exc).__name__}: {exc}"
    if result.returncode == 0:
        return "ok", ""
    tail = ((result.stdout or "") + (result.stderr or ""))[-2000:]
    return "vulnerabilities", f"dependency_audit_failed:{tail}"


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
    if (repo / "package-lock.json").exists():
        ran.append("dependency_audit")
        allow_unavailable = bool(security_config.get("allow_audit_unavailable", False))
        audit_cmd = security_config.get("audit_cmd")
        if not (isinstance(audit_cmd, list) and audit_cmd
                and all(isinstance(part, str) for part in audit_cmd)):
            findings.append("audit_unavailable:no_audit_cmd_configured")
            if not allow_unavailable:
                passed = False
        else:
            status, detail = _run_audit_cmd(repo, list(audit_cmd))
            if status == "ok":
                pass
            elif status == "unavailable":
                findings.append(detail)
                if not allow_unavailable:
                    passed = False
            else:  # "vulnerabilities" -- the audit ran fine and found something;
                   # allow_audit_unavailable never suppresses a real finding.
                findings.append(detail)
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

    return SecurityResult(passed=passed, findings=tuple(findings), ran=tuple(ran))


__all__ = ["SecurityResult", "check"]
