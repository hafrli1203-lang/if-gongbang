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
returncode, stdout_tail, url, sha256, fingerprint}) regardless of
outcome -- `url` is the target's own official-doc URL
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

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from engine.domain import stage_checks as gates
from engine.domain import ids
from engine.infrastructure import trusted as _trusted
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


_PROBE_EXTENSIONS = (".py", ".sh", ".js")
_PROBE_INTERPRETERS: dict[str, tuple[str, ...]] = {
    ".py": (sys.executable,),
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


def _probe_argv(script: Path) -> list[str]:
    return [*_PROBE_INTERPRETERS[script.suffix], str(script)]


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
                           probe_id: str, name: str, script: str, returncode: int,
                           stdout_tail: str, *, url: str = "", sha256: str = "",
                           fingerprint: str = "") -> None:
    ledger.append(Event(
        project=project, kind="probe.executed", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"id": probe_id, "name": name, "script": script,
                 "returncode": returncode, "stdout_tail": stdout_tail,
                 "url": url, "sha256": sha256, "fingerprint": fingerprint},
    ))


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
    """True iff the script both exited 0 AND its own stdout/stderr tail
    names `_PROBE_OK_MARK` together with THIS target's own url on the
    same line -- exit 0 alone proves nothing about whether the script
    ever actually contacted (or even looked at) the target: a no-op
    script (`exit 0`, or one probing a different target entirely)
    would otherwise "pass" and get recorded as having observed this
    target, when it never actually exercised it at all."""
    return returncode == 0 and f"{_PROBE_OK_MARK} {url}" in tail


def run_probe_scripts(
    config: EngineConfig, ledger: Ledger, project: str, attempt_id: str,
    job_name: str, targets: list[tuple[str, str]],
) -> tuple["ProbeEvidence", ...]:
    """Runs each target's own probe script (see module docstring), each
    inside its OWN disposable snapshot of the repo's current
    fingerprinted tree (one snapshot per target, never one shared
    snapshot every target's script runs in turn -- see
    _probe_snapshot_dir: sharing one snapshot would let target i's own
    script rewrite target j's script or inputs before j ever runs,
    while j's own recorded fingerprint still claims the ORIGINAL tree),
    and appends one probe.executed ledger event per target, in order. Each target's script/log id is engine.domain.stage_checks.
    probe_target_id(i) -- its 1-based POSITION in `targets`, never
    derived from its name -- so two targets can never collide onto the
    same id/script/log regardless of how similar or non-ASCII their
    names are (see module docstring). A target with no script at any
    of the known extensions gets returncode=-1, stdout_tail=
    "script_missing", ok=False -- never raises, so one missing script
    does not stop the rest of the targets from being probed and
    recorded.

    `ok` additionally requires the script's own stdout/stderr tail to
    say `_PROBE_OK_MARK` together with THAT target's own url
    (_probe_observed) -- exit 0 by itself is never enough, since a
    script that never actually contacts its target (or contacts the
    wrong one) can still exit 0 (see _probe_observed's own docstring).

    Each snapshot is rebuilt fresh from whatever the repo's own
    fingerprinted tree is RIGHT NOW (never a tree recorded earlier by
    some other step) -- the caller (engine.application.pipeline) always commits
    the prober's own changes to the repo before calling this, so every
    target's snapshot contains the just-committed probe scripts. A
    fingerprint that cannot be computed at all (passed in as `fp`,
    from engine.application.acceptance._fingerprint_or_none) fails every target
    closed, the same "script_missing"-shaped, never-raising outcome,
    under its own stdout_tail reason "no_fingerprint", rather than
    running anything against an unreadable tree."""
    from engine.application.acceptance import _fingerprint_or_none

    project_cfg = config.projects[project]
    repo = project_cfg.repo
    env = bench_env(bench_ctx(config, project, attempt_id))
    timeout_sec = project_cfg.test_timeout_sec
    results: list[ProbeEvidence] = []
    fp = _fingerprint_or_none(repo, project_cfg.excludes)
    if fp is None:
        for i, (name, url) in enumerate(targets):
            probe_id = gates.probe_target_id(i)
            _record_probe_executed(ledger, project, attempt_id, probe_id, name,
                                   "", -1, "no_fingerprint", url=url)
            results.append(ProbeEvidence(
                id=probe_id, name=name, script="", returncode=-1,
                stdout_tail="no_fingerprint", ok=False, url=url))
        return tuple(results)

    for i, (name, url) in enumerate(targets):
        probe_id = gates.probe_target_id(i)
        snapshot = _probe_snapshot_dir(config, attempt_id, probe_id)
        _make_snapshot(repo, snapshot, fp)
        _link_snapshot_deps(project_cfg, repo, snapshot)
        script = _probe_script_path(snapshot, job_name, probe_id)
        if script is None:
            _record_probe_executed(ledger, project, attempt_id, probe_id, name,
                                   "", -1, "script_missing", url=url, fingerprint=fp)
            results.append(ProbeEvidence(
                id=probe_id, name=name, script="", returncode=-1,
                stdout_tail="script_missing", ok=False, url=url, fingerprint=fp))
            continue
        # Capture the script's bytes from THIS snapshot, then verify
        # them again immediately before the sandbox actually executes
        # it -- never a single read trusted to still hold at exec time
        # (see engine.infrastructure.trusted's module docstring: the gap this closes
        # is the same one for every check-then-later-use pair in this
        # codebase, not something special to acceptance scenes). The
        # disposable snapshot has no other writer, so this should
        # never trip; a TrustViolation here means something DID touch
        # the snapshot between the two reads, and the script must then
        # never run at all -- not "run it anyway since exit 0 would
        # look the same as success".
        try:
            captured_script = _trusted.capture(script)
        except _trusted.TrustCaptureError:
            _record_probe_executed(ledger, project, attempt_id, probe_id, name,
                                   str(script), -1, "script_missing", url=url,
                                   fingerprint=fp)
            results.append(ProbeEvidence(
                id=probe_id, name=name, script=str(script), returncode=-1,
                stdout_tail="script_missing", ok=False, url=url, fingerprint=fp))
            continue
        script_sha256 = captured_script.digest
        try:
            _trusted.verify(captured_script, script)
        except _trusted.TrustViolation:
            _record_probe_executed(ledger, project, attempt_id, probe_id, name,
                                   str(script), -1, "script_changed_before_run",
                                   url=url, sha256=script_sha256, fingerprint=fp)
            results.append(ProbeEvidence(
                id=probe_id, name=name, script=str(script), returncode=-1,
                stdout_tail="script_changed_before_run", ok=False, url=url,
                sha256=script_sha256, fingerprint=fp))
            continue
        returncode, tail = run_in_bench_sandbox(
            config, project, attempt_id, snapshot, _probe_argv(script), env, timeout_sec,
            net=True, extra_readonly=(repo,))
        ok = _probe_observed(returncode, tail, url) and script.exists()
        _record_probe_executed(ledger, project, attempt_id, probe_id, name,
                               str(script), returncode, tail, url=url,
                               sha256=script_sha256, fingerprint=fp)
        results.append(ProbeEvidence(
            id=probe_id, name=name, script=str(script), returncode=returncode,
            stdout_tail=tail, ok=ok, url=url, sha256=script_sha256, fingerprint=fp))
    return tuple(results)
