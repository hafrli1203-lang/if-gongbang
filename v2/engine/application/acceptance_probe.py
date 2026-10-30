"""run_probe_scripts runs each probe target's own script from
.gongbang/<job>/probe/<id>.(py|sh|js) -- the engine executes the
script itself; a worker's "PROBE OK" text in some log is never
trusted as the evidence (see engine.domain.stage_checks' check_probe docstring for
the compatibility path this replaces). `<id>` is engine.domain.stage_checks.
probe_target_id(i), the target's 1-based POSITION in the targets list
(see that function's own docstring) -- never derived from the
target's own name: a name-derived id (the old scheme here, since
superseded) collapses every Korean-only name to the same fallback id,
and "Naver API"/"Naver_API" to the same id too, letting one passing
script silently stand in for several distinct targets. Each run
appends one probe.executed ledger event ({id, name, script,
returncode, stdout_tail, url, sha256, fingerprint, expected_host,
contacts, reached, started_at, finished_at}) regardless of outcome --
`stdout_tail` is the output the ENGINE captured from its own run,
`contacts` is what the engine's recording proxy saw that run send to
which host (engine.infrastructure.probe_proxy), and pass/fail is
judged on those, never on anything the probe worker wrote -- `url` is the target's own official-doc URL
(check_probe_targets' (name, url) tuple); `sha256` is the probe
script's own bytes hashed at the moment it ran ("" when there was no
script to hash); `fingerprint` is the disposable snapshot's own tree
hash (engine.infrastructure.evidence.fingerprint) the script actually ran against
("" if the real repo's own fingerprint could not even be computed).
The same three fields are on ProbeEvidence itself, so a caller that
only has the return value (not the ledger) still has the full
receipt.

Like engine.application.acceptance's own scenes/full suite, every probe script
runs against a disposable snapshot of the repo's fingerprinted tree
(see bench_snapshot.make_snapshot/link_snapshot_deps), never the real
project repo -- the bench sandbox this reuses always mounts its
`repo` argument writable (test runners write __pycache__/coverage
there), so running a probe script directly against the real repo (the
previous shape of this module) gave an external-API probe script --
not reviewed the way a build worker's own commit is -- direct write
access to the project's real working tree. The real repo is still
mounted read-only alongside the snapshot (extra_readonly), so a
copy_links symlink the snapshot only points back into it (e.g.
node_modules) still resolves.
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from engine.domain import stage_checks as gates
from engine.domain import ids
from engine.domain.probe_observation import target_host, target_reached
from engine.infrastructure import trusted as _trusted
from engine.infrastructure.probe_proxy import RecordingProxy, proxy_env
from engine.config import EngineConfig
from engine.domain.records import Event
from engine.application.steps.suite_run import bench_env
from engine.application.steps.suite_snapshot import link_snapshot_deps as _link_snapshot_deps
from engine.application.steps.suite_snapshot import make_snapshot as _make_snapshot

from engine.application.acceptance_bench import bench_ctx, run_in_bench_sandbox

if TYPE_CHECKING:
    from engine.infrastructure.db.ledger import Ledger


@dataclass(frozen=True)
class ProbeEvidence:
    id: str
    name: str
    script: str
    returncode: int
    stdout_tail: str
    ok: bool
    # The target's own official-doc URL (check_probe_targets' own
    # (name, url) tuple) -- carried through so a reader of the ledger/
    # handoff never has to go back to facts.md to see what this probe
    # was actually checking against.
    url: str = ""
    # sha256 of the probe script's own bytes AT THE TIME IT RAN (""
    # when there was no script to hash -- script_missing/no_fingerprint)
    # -- evidence that THIS exact script produced this result, not
    # just "some script at this path, whatever it currently contains".
    sha256: str = ""
    # engine.infrastructure.evidence.fingerprint of the snapshot this script ran
    # against (the same value _fingerprint_or_none computed from the
    # real repo right before the snapshot was built) -- "" when no
    # snapshot was ever built (no_fingerprint).
    fingerprint: str = ""
    # What the engine's own recording proxy saw during THIS run (see
    # engine.domain.probe_observation for the record shape) -- the
    # evidence `ok` is judged on, never the script's own output.
    contacts: tuple[dict[str, object], ...] = field(default_factory=tuple)
    # True iff `contacts` show a real response from the target's host.
    reached: bool = False
    # UTC ISO-8601 timestamps the engine took right before starting and
    # right after the script exited ("" when nothing was run).
    started_at: str = ""
    finished_at: str = ""


_PROBE_EXTENSIONS = (".py", ".sh", ".js")
# ".py" is deliberately absent here -- see _resolve_system_python/
# _probe_python_blocked below: sys.executable is THIS ENGINE PROCESS's
# own interpreter, which for this app's checked-in venv resolves under
# the engine's own hidden app root (engine.application.steps.worker_sandbox.
# ENGINE_ROOT), a path the probe's own bench sandbox never mounts
# (confirmed live: `bwrap: execvp .../.venv/bin/python: No such file or
# directory`) -- a static per-extension table cannot express "resolve
# at run time, then check the result", which running sys.executable
# unconditionally never did.
_PROBE_INTERPRETERS: dict[str, tuple[str, ...]] = {
    ".sh": ("sh",),
    ".js": ("node",),
}


def _probe_script_path(repo: Path, job: str, probe_id: str) -> Path | None:
    probe_dir = repo / ".gongbang" / job / "probe"
    for ext in _PROBE_EXTENSIONS:
        candidate = probe_dir / f"{probe_id}{ext}"
        if candidate.exists():
            return candidate
    return None


def _resolve_system_python() -> str:
    """A python3 OUTSIDE this engine's own venv, resolved the same way
    a probe script's own `#!/usr/bin/env python3` shebang would be:
    shutil.which("python3") on THIS process's own PATH. Falls back to
    os.path.realpath(sys.executable) only when PATH has no python3 at
    all -- never silently preferred over `which`, and still checked
    against the probe's own hidden roots by _probe_python_blocked
    either way, so a fallback that lands back inside the hidden venv
    is caught, not run anyway."""
    which = shutil.which("python3")
    if which is not None:
        return os.path.realpath(which)
    return os.path.realpath(sys.executable)


def _is_within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _probe_hidden_roots(config: EngineConfig, project: str) -> tuple[Path, ...]:
    """The hidden-root set THIS project's own probe bench sandbox
    actually hides (engine.application.steps.suite_run.bench_sandbox_argv,
    via run_in_bench_sandbox below) -- config.home, config.secrets_dir,
    config.ledger_path.parent, every OTHER project's repo, this
    project's own hidden_paths, and the real operator HOME -- reused
    here to catch a resolved python3 that would be invisible inside
    that same sandbox BEFORE ever spawning it (see
    _resolve_system_python: an execvp "No such file or directory" is
    not evidence of anything, so this must be caught first, not
    discovered from a bwrap failure's own text)."""
    project_cfg = config.projects[project]
    other_repos = tuple(p.repo for name, p in config.projects.items() if name != project)
    roots = ((config.home, config.secrets_dir, config.ledger_path.parent)
             + other_repos + project_cfg.hidden_paths)
    real_home = os.environ.get("HOME")
    if real_home:
        roots += (Path(real_home),)
    return roots


def _probe_python_blocked(python_path: str, hidden_roots: tuple[Path, ...]) -> bool:
    """True iff the resolved python3 interpreter sits at or under any
    of `hidden_roots` -- i.e. the probe's own bench sandbox would hide
    it, so running it would fail closed (execvp ENOENT) rather than
    actually probing anything. Checked by REAL path on both sides
    (os.path.realpath), never the raw strings, so a symlink hop on
    either side cannot hide a match."""
    real_python = Path(os.path.realpath(python_path))
    for root in hidden_roots:
        real_root = Path(os.path.realpath(root))
        if real_python == real_root or _is_within(real_python, real_root):
            return True
    return False


def _probe_argv(script: Path, interpreter: tuple[str, ...]) -> list[str]:
    return [*interpreter, str(script)]


def _probe_snapshot_dir(config: EngineConfig, attempt_id: str, probe_id: str) -> Path:
    """Scratch location for ONE target's disposable probe-run snapshot
    -- a sibling of bench_snapshot.snapshot_dir's own acceptance
    snapshot (same `config.home`-rooted, never-under-any-repo
    placement), kept under its own "probe-<probe_id>" leaf (probe_id =
    engine.domain.stage_checks.probe_target_id(i), 1-based position in `targets`) so
    a job that runs both run_probe_scripts and run_acceptance for the
    same attempt_id never has one call's shutil.rmtree(dest) (see
    bench_snapshot.make_snapshot) race or clobber the other's snapshot
    directory -- AND so target i's own script can never see target j's
    leftover untracked state (a script that writes, say, a cache file
    into the snapshot it ran in must not let a LATER target's run
    observe that file, nor record a fingerprint that was never
    actually the tree that target's own script ran against -- see
    run_probe_scripts)."""
    return config.home / "acceptance" / ids.attempt_dirname(attempt_id) / f"probe-{probe_id}"


def _record_probe_executed(ledger: Ledger, project: str, attempt_id: str,
                           evidence: "ProbeEvidence") -> None:
    ledger.append(Event(
        project=project, kind="probe.executed", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"id": evidence.id, "name": evidence.name, "script": evidence.script,
                 "returncode": evidence.returncode, "stdout_tail": evidence.stdout_tail,
                 "url": evidence.url, "sha256": evidence.sha256,
                 "fingerprint": evidence.fingerprint,
                 "expected_host": target_host(evidence.url) or "",
                 "contacts": [dict(c) for c in evidence.contacts],
                 "reached": evidence.reached,
                 "started_at": evidence.started_at, "finished_at": evidence.finished_at},
    ))


def _not_run(ledger: Ledger, project: str, attempt_id: str, probe_id: str, name: str,
             url: str, reason: str, *, script: str = "", sha256: str = "",
             fingerprint: str = "") -> "ProbeEvidence":
    """One fail-closed result for a target whose script never ran."""
    evidence = ProbeEvidence(
        id=probe_id, name=name, script=script, returncode=-1, stdout_tail=reason,
        ok=False, url=url, sha256=sha256, fingerprint=fingerprint)
    _record_probe_executed(ledger, project, attempt_id, evidence)
    return evidence


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# The marker a probe script's own stdout must print, followed by the
# target's own url, for run_probe_scripts to count that target as
# actually observed -- same literal text as engine.domain.stage_checks'
# _PROBE_OK_MARK (kept as its own module-level constant here rather
# than importing that private name, since the two modules check this
# marker in two different places: gates.check_probe reads it from a
# worker-written log file engine.application.pipeline never actually calls
# today, this module from the sandboxed subprocess's own real stdout).
_PROBE_OK_MARK = "PROBE OK"


def _probe_observed(returncode: int, tail: str, url: str) -> bool:
    """Legacy output check, kept for compatibility: exit 0 and a
    `PROBE OK <url>` line. Necessary but never sufficient -- the
    script writes this text itself; run_probe_scripts additionally
    requires the engine's own proxy to have seen the target reached."""
    return returncode == 0 and f"{_PROBE_OK_MARK} {url}" in tail


def _execute_behind_proxy(
    config: EngineConfig, project: str, attempt_id: str, snapshot: Path,
    argv: list[str], env: dict[str, str],
) -> tuple[int, str, tuple[dict[str, object], ...], str, str]:
    """Runs `argv` in the probe bench sandbox with its HTTP(S) traffic
    routed through a fresh RecordingProxy -> (returncode, the engine-
    captured stdout+stderr tail, the proxy's contact records, started_at,
    finished_at). One proxy per run, so a contact can only ever count
    for the target whose script produced it."""
    project_cfg = config.projects[project]
    with RecordingProxy() as proxy:
        run_env = {**env, **proxy_env(proxy.url)}
        started_at = _now()
        returncode, tail = run_in_bench_sandbox(
            config, project, attempt_id, snapshot, argv, run_env,
            project_cfg.test_timeout_sec, net=True, extra_readonly=(project_cfg.repo,),
            runtime_clis=project_cfg.probe_runtime_clis)
        finished_at = _now()
    return returncode, tail, proxy.contacts(), started_at, finished_at


def _interpreter_for(config: EngineConfig, project: str,
                     script: Path) -> tuple[str, ...] | None:
    """The interpreter argv prefix for `script`, or None when its python3
    would be invisible inside the probe sandbox (see
    _probe_python_blocked)."""
    if script.suffix != ".py":
        return _PROBE_INTERPRETERS[script.suffix]
    python_path = _resolve_system_python()
    if _probe_python_blocked(python_path, _probe_hidden_roots(config, project)):
        return None
    return (python_path,)


def _probe_one_target(
    config: EngineConfig, ledger: Ledger, project: str, attempt_id: str,
    job_name: str, index: int, name: str, url: str, fp: str, env: dict[str, str],
) -> ProbeEvidence:
    project_cfg = config.projects[project]
    probe_id = gates.probe_target_id(index)
    snapshot = _probe_snapshot_dir(config, attempt_id, probe_id)
    _make_snapshot(project_cfg.repo, snapshot, fp)
    _link_snapshot_deps(project_cfg, project_cfg.repo, snapshot)
    script = _probe_script_path(snapshot, job_name, probe_id)
    if script is None:
        return _not_run(ledger, project, attempt_id, probe_id, name, url,
                        "script_missing", fingerprint=fp)
    interpreter = _interpreter_for(config, project, script)
    if interpreter is None:
        return _not_run(ledger, project, attempt_id, probe_id, name, url,
                        "python_runtime_hidden", script=str(script), fingerprint=fp)
    # Capture the script's bytes from THIS snapshot, then verify them
    # again immediately before the sandbox executes it (see
    # engine.infrastructure.trusted): a TrustViolation means something
    # touched the snapshot in between, and the script never runs.
    try:
        captured_script = _trusted.capture(script)
    except _trusted.TrustCaptureError:
        return _not_run(ledger, project, attempt_id, probe_id, name, url,
                        "script_missing", script=str(script), fingerprint=fp)
    script_sha256 = captured_script.digest
    try:
        _trusted.verify(captured_script, script)
    except _trusted.TrustViolation:
        return _not_run(ledger, project, attempt_id, probe_id, name, url,
                        "script_changed_before_run", script=str(script),
                        sha256=script_sha256, fingerprint=fp)
    returncode, tail, contacts, started_at, finished_at = _execute_behind_proxy(
        config, project, attempt_id, snapshot, _probe_argv(script, interpreter), env)
    reached = target_reached(url, contacts)
    ok = (reached and _probe_observed(returncode, tail, url) and script.exists())
    evidence = ProbeEvidence(
        id=probe_id, name=name, script=str(script), returncode=returncode,
        stdout_tail=tail, ok=ok, url=url, sha256=script_sha256, fingerprint=fp,
        contacts=contacts, reached=reached, started_at=started_at,
        finished_at=finished_at)
    _record_probe_executed(ledger, project, attempt_id, evidence)
    return evidence


def run_probe_scripts(
    config: EngineConfig, ledger: Ledger, project: str, attempt_id: str,
    job_name: str, targets: list[tuple[str, str]],
) -> tuple["ProbeEvidence", ...]:
    """Runs each target's own probe script (see module docstring), each
    inside its OWN disposable snapshot of the repo's current
    fingerprinted tree (one snapshot per target, so target i's script
    can never rewrite target j's script or inputs before j runs), and
    appends one probe.executed ledger event per target, in order. A
    target's script/log id is engine.domain.stage_checks.
    probe_target_id(i) -- its 1-based POSITION in `targets`. A target
    with no script gets returncode=-1, stdout_tail="script_missing",
    ok=False -- never raises, so one missing script does not stop the
    rest of the targets from being probed and recorded.

    `ok` is the engine's own observation, not the script's word: the
    script runs behind a fresh RecordingProxy
    (engine.infrastructure.probe_proxy), and the target counts only
    when that proxy saw a real response from the host of the target's
    own facts.md address (engine.domain.probe_observation.
    target_reached) -- AND the script exited 0 with the legacy
    `PROBE OK <url>` line. A script that prints the line without
    contacting the target, or contacts some other host, fails; a
    target whose facts.md value has no http(s) address cannot be
    observed and fails too.

    A fingerprint that cannot be computed at all fails every target
    closed under stdout_tail "no_fingerprint", without running
    anything against an unreadable tree."""
    from engine.application.acceptance import _fingerprint_or_none

    project_cfg = config.projects[project]
    env = bench_env(bench_ctx(config, project, attempt_id))
    fp = _fingerprint_or_none(project_cfg.repo, project_cfg.excludes)
    if fp is None:
        return tuple(
            _not_run(ledger, project, attempt_id, gates.probe_target_id(i), name, url,
                     "no_fingerprint")
            for i, (name, url) in enumerate(targets))
    return tuple(
        _probe_one_target(config, ledger, project, attempt_id, job_name, i, name, url, fp, env)
        for i, (name, url) in enumerate(targets))
