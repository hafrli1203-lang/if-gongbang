"""bench — run the project's real test suite plus trust-checked cases.

compute_fingerprint  fp = evidence.fingerprint(repo); journal {fp}
                     (reconcile False: a crash mid-run re-computes rather
                     than trusting a half-written journal row).
run_tests            Speed default (project.bench_scope, "related" unless
                     set to "full"): with scope "related" and a
                     configured project.related_cmd, run related_cmd
                     against only this attempt's changed source files
                     (bench_select._changed_source_paths against HEAD,
                     the same range the fingerprint covers -- at this
                     point in the stage list HEAD still equals
                     base_head, since commit runs after bench) in place
                     of the literal "{files}" token, one argv entry per
                     file; falls back to the full project.test_cmd when
                     related_cmd is unset or there are no changed
                     source files (bench_select._resolve_test_argv).
                     Runs IN repo (never a disposable
                     copy of it -- see _bench_immutable_source's own
                     docstring for why a copy built from compute_
                     fingerprint's fp would silently lose whatever an
                     install request just wrote into a gitignored
                     destination like .deps/ or node_modules/, breaking
                     the install-then-test flow), but every tracked
                     file AND tracked directory is bind-mounted read-
                     only alongside it (_bench_immutable_source, the
                     same bench_snapshot.tracked_file_paths/
                     tracked_directories engine.application.acceptance uses for ITS
                     OWN disposable snapshot, so the bench cannot write
                     tracked source) --
                     an explicit environment built from config.
                     worker_env_passthrough only (never the processor's
                     own full os.environ, which may carry secrets a
                     project's test_cmd has no business seeing); journal
                     {returncode, tail, scope, argv, no_tests_executed}
                     (tail = last 2000 chars of combined stdout+stderr;
                     scope is "related"/"full"/"full(fallback)"; argv is
                     the exact command run; no_tests_executed -- see
                     bench_zero_tests.looks_like_zero_tests_ran -- true
                     iff returncode==0 but nothing was actually
                     exercised). A timeout -> StepFailed
                     ("tests_timeout"). Neither a non-zero exit NOR
                     no_tests_executed is itself a step failure here:
                     record_bench (not this step) folds both into the
                     overall passed/reason.
trust_checks         project.bench_cases (a JSON file in the repo) ->
                     bench.Case list; each case's run() executes
                     `test_cmd + [selector]` with os.environ overridden
                     by that case's own optional "env" object (e.g. a
                     negative-control case that only fires under a
                     sample-fixture-specific env var, so the real
                     run_tests suite stays green while the control case
                     still exercises its failure signature);
                     bench.run_bench() decides pass/fail/reason.
                     Missing/absent bench_cases -> StepFailed
                     ("no_bench_cases"). reconcile False.
record_bench         recompute the fingerprint; if it differs from
                     compute_fingerprint's, StepFailed
                     ("tree_changed_during_bench") -- no evidence is
                     recorded for a tree a worker kept editing under our
                     feet. Otherwise passed = (run_tests returncode==0)
                     and trust.passed (plan §5: the real suite AND the
                     trust-checked cases must both be clean); reason is
                     trust.reason when the cases failed, else
                     "tests_failed" when only the real suite is red,
                     else "ok". Record evidence (kind "bench", payload
                     {"passed", "reason", "request_id", "per_case",
                     "tests_returncode", "scope", "argv"} -- scope/argv
                     carried over from run_tests' own journal payload)
                     FIRST, then, if the bench did
                     not pass, raise StepFailed(reason) -- evidence is
                     recorded before raising so the failure (and its
                     signatures) stays visible to three_match/record
                     even though the request itself ends 'failed'.
                     reconcile: evidence for this request_id already
                     recorded AND passed=True -- a recorded passed=False
                     re-raises StepFailed(reason) rather than reading
                     its own existence as success.

Disposable-snapshot construction and immutability guards (shared with
engine.application.acceptance) live in engine.application.steps.suite_snapshot. Picking
related vs. full test scope lives in engine.application.steps.suite_select.
Detecting a run that executed zero real tests lives in
engine.application.steps.suite_zero_tests.
"""
from __future__ import annotations

import json
import re
import os
import subprocess
from pathlib import Path

from engine.domain import suite_trust as bench
from engine.infrastructure import evidence
from engine.domain import ids
from engine.infrastructure import sandbox
from engine.application.processor import StepContext, StepDef, StepFailed
from engine.application.steps.suite_select import (  # noqa: F401 -- re-exported for tests
    build_related_argv as _build_related_argv,
)
from engine.application.steps.suite_select import resolve_test_argv as _resolve_test_argv
from engine.application.steps.suite_snapshot import tracked_directories, tracked_file_paths
from engine.application.steps.suite_zero_tests import VITEST_NO_TESTS_RE as _VITEST_NO_TESTS_RE
from engine.application.steps.suite_zero_tests import looks_like_zero_tests_ran
from engine.application.steps.suite_zero_tests import ran_zero_tests as _ran_zero_tests

_TAIL_CHARS = 2000


def _project_config(ctx: StepContext):
    return ctx.config.projects[ctx.project]


def _repo(ctx: StepContext) -> Path:
    return _project_config(ctx).repo


def _excludes(ctx: StepContext) -> tuple[str, ...]:
    return _project_config(ctx).excludes


def _timeout_sec(ctx: StepContext) -> float:
    return _project_config(ctx).test_timeout_sec


def _journal_payload(ctx: StepContext, step_name: str) -> dict[str, object]:
    row = ctx.ledger.steps(ctx.request.request_id).get(step_name)
    if row is None:
        raise StepFailed(f"missing_step:{step_name}")
    return row.payload


def _run_compute_fingerprint(ctx: StepContext) -> dict[str, object]:
    try:
        fp = evidence.fingerprint(_repo(ctx), _excludes(ctx))
    except evidence.FingerprintError as exc:
        raise StepFailed("no_fingerprint") from exc
    submitted = ctx.request.payload.get("fp")
    if submitted is not None and submitted != fp:
        raise StepFailed("tree_changed_since_submit")
    return {"fp": fp}


def _other_project_repos(ctx: StepContext) -> tuple[Path, ...]:
    return tuple(p.repo for name, p in ctx.config.projects.items()
                 if name != ctx.project)


def _bench_home_dir(ctx: StepContext) -> Path:
    """A fresh, empty, per-attempt directory used as the sandboxed
    bench's $HOME -- NEVER the real operator home, regardless of
    bench_home_writable (that flag only decides whether this directory
    itself is mounted writable or read-only, see _bench_sandbox_argv).
    Lives under the engine's own home, so it is a descendant of the
    `hidden` config.home entry and gets re-exposed the same way
    station.py's worker copy does (deeper mount wins)."""
    path = ctx.config.home / "jobs" / ids.attempt_dirname(ctx.attempt_id) / "bench_home"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _bench_sandbox_argv(ctx: StepContext, repo: Path, argv: list[str],
                        env: dict[str, str], *, net: bool = False,
                        extra_readonly: tuple[Path, ...] = ()) -> tuple[list[str], dict[str, str]]:
    """Bench sandbox policy: writable = the repo itself (test runners
    write __pycache__/coverage/etc there). readonly = repo/.git,
    mounted AFTER the repo's own writable bind (deeper path wins) so
    test code cannot plant a `core.fsmonitor`/hook command there for a
    later engine-side git call (evidence.fingerprint et al.) to execute
    outside any sandbox -- plus, unless bench_home_writable is True,
    _bench_home_dir()'s empty per-attempt directory (if it IS True,
    that same directory moves to `writable` instead: either way it is
    never the real HOME). hidden = engine home, secrets_dir, the
    ledger's directory (no gate hook runs here, so unlike
    station.py/review.py the ledger dir is NOT re-added writable),
    every OTHER project's repo, and the real operator HOME outright
    (read from THIS process's own os.environ, never from `env` --
    `env` can carry a bench case's own "env" override, read from a
    JSON file inside the project's repo, and a case-controlled value
    must never decide what gets hidden or mounted writable). The
    returned child_env always pins HOME to _bench_home_dir(), overriding
    whatever HOME `env` itself carried, for the same reason. net = False
    -- a project's test suite has no business making outbound network
    calls as part of this engine's trust checks.

    extra_readonly: additional read-only mounts beyond repo/.git -- every
    existing caller passes `repo` itself as THIS function's `repo` (so it
    is always re-exposed, since it is also `writable`'s own entry); a
    caller that runs `argv` somewhere else entirely (engine.application.acceptance's
    disposable snapshot, built under config.home and therefore itself
    already hidden-then-re-exposed the same way _bench_home_dir() is)
    still needs the REAL project repo visible, read-only, for any
    on-disk content (e.g. a copy_links entry like node_modules) the
    snapshot only symlinks BACK into -- config.ledger_path.parent (see
    `hidden` below) is that real repo's own parent directory, so without
    this it stays hidden under that tmpfs with nothing to re-expose it
    (confirmed empirically: a snapshot-side symlink into an unlisted real
    repo resolved to nothing inside the sandbox, hanging `npx vitest run`
    trying to fetch the package it could not resolve locally, over a
    net=False sandbox, until it timed out). Defaults to `()`, so every
    pre-existing caller's mount list is unchanged.

    config.bench_readonly_paths (e.g. "~/.cache/ms-playwright", expanded
    at config load against the ENGINE process's own HOME) re-exposes
    each existing entry read-only at its real host path -- and, if it
    is under the real HOME, ALSO at the same relative place under
    _bench_home_dir(), via readonly_remap, so `$HOME/<rel>` resolves
    inside the sandbox despite the real HOME being hidden (a project's
    test suite needing a toolchain cache under the real home, e.g.
    installed browser binaries, without getting the rest of it)."""
    bench_home = _bench_home_dir(ctx)
    child_env = {**env, "HOME": str(bench_home)}
    writable = (repo,)
    readonly: tuple[Path, ...] = (repo / ".git",) + extra_readonly
    readonly_remap: tuple[tuple[Path, Path], ...] = ()
    if ctx.config.bench_home_writable:
        writable += (bench_home,)
    else:
        readonly += (bench_home,)
    hidden = ((ctx.config.home, ctx.config.secrets_dir,
               ctx.config.ledger_path.parent) + _other_project_repos(ctx)
              + ctx.config.projects[ctx.project].hidden_paths)
    real_home = os.environ.get("HOME")
    real_home_path = Path(real_home) if real_home else None
    if real_home_path is not None:
        hidden += (real_home_path,)
    for entry in ctx.config.bench_readonly_paths:
        if not entry.exists():
            continue
        readonly += (entry,)
        if real_home_path is not None:
            try:
                rel = entry.relative_to(real_home_path)
            except ValueError:
                continue
            dest = bench_home / rel
            # bwrap can only auto-create a MISSING mountpoint under an
            # ancestor mount that is itself writable (--bind) -- when
            # bench_home_writable is False (the default), bench_home is
            # mounted --ro-bind, and bwrap then has no write access to
            # create `dest`'s directory/file inside that read-only
            # bind, failing with "Can't mkdir ...: Read-only file
            # system". Pre-creating `dest` on the HOST here (plain
            # filesystem access, outside any
            # sandbox -- bench_home is the engine's own scratch dir)
            # means it already exists as part of whatever bench_home
            # content gets ro-bound, so the later --ro-bind onto it is
            # just a mount over an EXISTING path, never a mkdir, and
            # works regardless of bench_home's own writable/read-only
            # mount kind. Matches the source's own type (dir vs file)
            # so the mountpoint shape is right either way.
            if entry.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.touch(exist_ok=True)
            readonly_remap += ((entry, dest),)
    try:
        wrapped = sandbox.wrap(
            argv, cwd=repo, writable=writable, hidden=hidden, readonly=readonly,
            readonly_remap=readonly_remap, net=net, mode=ctx.config.sandbox,
            child_env=child_env,
        )
    except sandbox.SandboxUnavailable as exc:
        raise StepFailed("sandbox_unavailable") from exc
    return wrapped, child_env


def _bench_env(ctx: StepContext) -> dict[str, str]:
    """An explicit environment for the real suite, built only from
    config.worker_env_passthrough -- never the engine processor's own
    full os.environ, which may carry secrets (API keys, tokens) that a
    project's test_cmd has no business seeing. Passed to every bench
    subprocess (run_tests and each trust-checked case); a case's own
    "env" (see _case_env) is merged on top for that one case only."""
    names = ctx.config.worker_env_passthrough
    return {name: os.environ[name] for name in names if name in os.environ}


def _run_test_cmd(ctx: StepContext, repo: Path, test_cmd: list[str],
                  env: dict[str, str], *,
                  extra_readonly: tuple[Path, ...] = ()) -> tuple[int, str]:
    argv, child_env = _bench_sandbox_argv(ctx, repo, list(test_cmd), env,
                                          extra_readonly=extra_readonly)
    # mode="bwrap": child_env reaches the child via an inherited pipe
    # fd (sandbox.wrap()'s `--args FD`, never a literal `--setenv
    # NAME VALUE` in argv -- see sandbox.pass_fds_for's docstring);
    # the Popen env here must be the sandbox launcher's sanitized env,
    # never child_env itself (see sandbox.exec_env's docstring).
    # mode="none": exec_env returns child_env unchanged -- there is no
    # bwrap process to separate it from.
    popen_env = sandbox.exec_env(ctx.config.sandbox, child_env)
    pass_fds = sandbox.pass_fds_for(argv)
    try:
        result = subprocess.run(
            argv, cwd=str(repo), capture_output=True,
            timeout=_timeout_sec(ctx), text=True, env=popen_env,
            pass_fds=pass_fds,
        )
    except subprocess.TimeoutExpired as exc:
        raise StepFailed("tests_timeout") from exc
    finally:
        sandbox.close_fds(pass_fds)
    combined = (result.stdout or "") + (result.stderr or "")
    return result.returncode, combined


def _bench_immutable_source(ctx: StepContext) -> tuple[Path, ...]:
    """Every tracked file AND tracked directory of `_repo(ctx)` itself
    (bench_snapshot.tracked_file_paths/tracked_directories -- the SAME
    functions engine.application.acceptance uses for a scene/the full suite's OWN
    disposable snapshot) -- for a caller to bind read-only alongside the
    real repo in a bwrap sandbox call, WITHOUT ever copying the repo
    into a disposable snapshot and WITHOUT ever chmod'ing anything on
    it (both would be wrong here: a disposable copy of the real repo at
    THIS moment would not contain whatever a just-finished install
    request wrote into a gitignored destination like .deps/ or
    node_modules/ -- those are never part of evidence.fingerprint's own
    tracked-plus-gitignore-aware range, so a snapshot built from fp
    alone would silently lose them, breaking the install-then-test flow
    the exact same way engine.application.acceptance's own copy_links exists to
    avoid for ITS disposable snapshots, but copy_links itself is gated
    to require sandbox="bwrap" at config load time
    (ProjectConfig.copy_links_requires_bwrap) -- not every project
    configured for bench has opted into that; and chmod'ing the real
    repo's own tracked files read-only would persist past this one
    bench request, breaking a LATER task's own merge into those same
    files on the SAME shared repo, which a disposable snapshot's chmod
    never risked since that copy was thrown away afterwards).

    A bwrap bind mount is per-process and never touches the host
    filesystem's own permissions, so binding these same paths read-only
    for just the duration of this one sandboxed test run is safe on the
    real, ongoing, shared repo in a way chmod never would be -- this is
    source and dependencies mounted immutably, without isolating
    scratch output from import paths (sandbox="none" has no bind
    mounts at all, so this protection does not apply there -- the same
    documented, not-chased limitation this codebase already accepts
    elsewhere for that mode)."""
    repo = _repo(ctx)
    return tracked_file_paths(repo) + tracked_directories(repo)


def _run_run_tests(ctx: StepContext) -> dict[str, object]:
    scope, test_cmd = _resolve_test_argv(ctx)
    repo = _repo(ctx)
    extra_readonly = _bench_immutable_source(ctx)
    env = _bench_env(ctx)
    returncode, combined = _run_test_cmd(ctx, repo, list(test_cmd), env,
                                         extra_readonly=extra_readonly)
    if _ran_zero_tests(scope, combined):
        # The related selector matched nothing real: a related-scope
        # run that executed zero tests must never be trusted as
        # "nothing needed checking" -- fall back to the full suite
        # instead of recording a pass over code nothing actually
        # exercised. "full(fallback)" is a
        # distinct scope value from an ordinary "full" choice, so a
        # reader of bench evidence can tell the two apart.
        scope = "full(fallback)"
        test_cmd = tuple(_project_config(ctx).test_cmd)
        returncode, combined = _run_test_cmd(ctx, repo, list(test_cmd), env,
                                             extra_readonly=extra_readonly)
    # An exit-0 run that executed
    # zero tests must never pass, regardless of scope -- the related-
    # scope fallback above already covers "related selected nothing",
    # but a "full"/"full(fallback)" run can land here too (e.g. every
    # test in the real suite itself skipped). Checked on the run's
    # own FINAL output only (never a fallback's now-discarded first
    # attempt). record_bench (not this step) folds no_tests_executed
    # into the overall passed/reason, same as it already does for a
    # plain non-zero returncode.
    no_tests_executed = returncode == 0 and looks_like_zero_tests_ran(combined)
    return {"returncode": returncode, "tail": combined[-_TAIL_CHARS:],
            "scope": scope, "argv": list(test_cmd),
            "no_tests_executed": no_tests_executed}


_ERROR_LINE = re.compile(r"^E\s+(?:([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))\b|(assert)\b)", re.M)

# vitest's own shape (no pytest "E   ..." lines to match at all): a run
# that collected and executed zero tests -- e.g. the control selector no
# longer matches any file -- must be its OWN distinct signature,
# "no_tests", never silently folded into a generic "exit:N" that could
# accidentally equal a negative control's configured expected_signature
# without the control's own assertion ever actually having run (a
# vitest install that defaults `passWithNoTests` to true exits 0 for
# exactly this case, looking like an ordinary clean pass). "assertion"
# is only reported when the output BOTH names AssertionError and
# vitest's own "N failed" summary line -- a failed test for some other
# reason (a thrown non-assertion error, a timeout vitest itself reports
# as a failure) must not be silently counted as the specific assertion
# failure a negative control's expected_signature "assertion" is
# supposed to prove actually fired.
_VITEST_FAILED_COUNT_RE = re.compile(r"\b\d+\s+failed\b", re.I)
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# _VITEST_NO_TESTS_RE itself is owned by engine.application.steps.suite_zero_tests
# (imported at the top of this file) and reused here only.


def _case_signature(output: dict[str, object]) -> str | None:
    """Classify a failed case. pytest-shaped output: from its `E   ...`
    lines only (not from arbitrary text such as `from x import
    AssertionError` in a traceback source line) -- import/collection
    failures win over assertions. vitest-shaped output (no pytest
    `E   ` line matched at all): see the vitest regexes above."""
    # Test runners may colour their output; terminal escape codes would
    # otherwise break word boundaries in the summary-line patterns.
    text = _ANSI_ESCAPE.sub("", str(output.get("text", "")))
    names = [m.group(1) or "assert" for m in _ERROR_LINE.finditer(text)]
    for name in names:
        short = name.rsplit(".", 1)[-1]
        if short in ("ModuleNotFoundError", "ImportError"):
            return short
    if any(name == "assert" or name.rsplit(".", 1)[-1] == "AssertionError" for name in names):
        return "assertion"
    if _VITEST_NO_TESTS_RE.search(text):
        return "no_tests"
    if "AssertionError" in text and _VITEST_FAILED_COUNT_RE.search(text):
        return "assertion"
    return f"exit:{output.get('returncode')}"


# A bench case's "env" comes from bench_cases.json INSIDE the project's
# repo -- content a worker (or anyone with repo write access) can
# influence. These names are therefore never honored from a case,
# regardless of value: they could redirect which `test_cmd` binary
# resolves (PATH), inject into the test process's own dynamic linker
# (LD_PRELOAD/LD_LIBRARY_PATH), or override the bench HOME
# _bench_sandbox_argv deliberately pins (HOME).
_FORBIDDEN_CASE_ENV_KEYS = frozenset({"PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "HOME"})


def _case_env(base_env: dict[str, str], spec: dict[str, object]) -> dict[str, str]:
    """Per-case env override (sample/fixture detail, not an engine rule):
    merged over the bench's own passthrough-only base_env (see
    _bench_env) for that one case's subprocess only, so a case can need
    an env var to reproduce its failure without making run_tests'
    full-suite run red, and without leaking the processor's full
    os.environ into either -- every case runs with base_env at minimum,
    never a bare None that would fall back to subprocess.run inheriting
    the processor's whole environment. _FORBIDDEN_CASE_ENV_KEYS entries
    in the case's own "env" are silently dropped, never applied."""
    raw = spec.get("env")
    if not isinstance(raw, dict):
        return dict(base_env)
    overrides = {str(k): str(v) for k, v in raw.items()
                 if str(k) not in _FORBIDDEN_CASE_ENV_KEYS}
    return {**base_env, **overrides}


def _make_case(ctx: StepContext, test_cmd: tuple[str, ...], repo: Path,
                timeout_sec: float, base_env: dict[str, str],
                spec: dict[str, object], *,
                extra_readonly: tuple[Path, ...] = ()) -> bench.Case:
    selector = str(spec["selector"])
    name = str(spec["name"])
    env = _case_env(base_env, spec)
    argv, child_env = _bench_sandbox_argv(ctx, repo, [*test_cmd, selector], env,
                                          extra_readonly=extra_readonly)
    popen_env = sandbox.exec_env(ctx.config.sandbox, child_env)
    pass_fds = sandbox.pass_fds_for(argv)
    captured: dict[str, object] = {}

    def run() -> bool:
        try:
            result = subprocess.run(
                argv, cwd=str(repo), capture_output=True,
                timeout=timeout_sec, text=True, env=popen_env,
                pass_fds=pass_fds,
            )
        except subprocess.TimeoutExpired:
            captured["text"] = ""
            captured["returncode"] = "timeout"
            return False
        finally:
            sandbox.close_fds(pass_fds)
        captured["text"] = (result.stdout or "") + (result.stderr or "")
        captured["returncode"] = result.returncode
        return result.returncode == 0

    def signature_of() -> str | None:
        return _case_signature(captured)

    return bench.Case(
        name=name, run=run,
        expect_fail=bool(spec.get("expect_fail", False)),
        expected_signature=(
            str(spec["expected_signature"]) if spec.get("expected_signature") is not None
            else None
        ),
        signature_of=signature_of,
    )


def _load_cases(ctx: StepContext) -> list[bench.Case]:
    project = _project_config(ctx)
    bench_cases_rel = project.bench_cases
    if not bench_cases_rel:
        raise StepFailed("no_bench_cases")
    cases_path = _repo(ctx) / bench_cases_rel
    if not cases_path.exists():
        raise StepFailed("no_bench_cases")
    try:
        raw = json.loads(cases_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise StepFailed("no_bench_cases") from exc
    if not isinstance(raw, list) or not raw:
        raise StepFailed("no_bench_cases")
    timeout_sec = _timeout_sec(ctx)
    base_env = _bench_env(ctx)
    # Each case's tracked source is immutable the same way run_tests'
    # own run is (_bench_immutable_source) -- see that function's own
    # docstring for why this binds the real repo read-only rather than
    # running against a disposable copy of it.
    extra_readonly = _bench_immutable_source(ctx)
    return [_make_case(ctx, project.test_cmd, _repo(ctx), timeout_sec, base_env, spec,
                       extra_readonly=extra_readonly)
            for spec in raw]


def _run_trust_checks(ctx: StepContext) -> dict[str, object]:
    fp = str(_journal_payload(ctx, "compute_fingerprint")["fp"])
    cases = _load_cases(ctx)
    result = bench.run_bench(cases, fp)
    return {"passed": result.passed, "reason": result.reason, "per_case": result.per_case}


def _run_record_bench(ctx: StepContext) -> dict[str, object]:
    fp = str(_journal_payload(ctx, "compute_fingerprint")["fp"])
    try:
        now_fp = evidence.fingerprint(_repo(ctx), _excludes(ctx))
    except evidence.FingerprintError as exc:
        raise StepFailed("no_fingerprint") from exc
    if now_fp != fp:
        raise StepFailed("tree_changed_during_bench")
    run_tests_result = _journal_payload(ctx, "run_tests")
    tests_returncode = run_tests_result["returncode"]
    # An exit-0 run that executed zero tests is not "ok" either --
    # run_tests_result.get (not []) since a step journaled by a
    # pre-P2 engine version (resumed from an old attempt) never wrote
    # this key at all, and a missing flag must never read as True.
    no_tests_executed = bool(run_tests_result.get("no_tests_executed", False))
    tests_ok = tests_returncode == 0 and not no_tests_executed
    trust = _journal_payload(ctx, "trust_checks")
    trust_passed = bool(trust["passed"])
    passed = tests_ok and trust_passed
    if not trust_passed:
        reason = str(trust["reason"])
    elif no_tests_executed:
        reason = "no_tests_executed"
    elif not tests_ok:
        reason = "tests_failed"
    else:
        reason = "ok"
    evidence.record(
        ctx.ledger, ctx.project, ctx.attempt_id, "bench", fp,
        payload={
            "passed": passed, "reason": reason,
            "request_id": ctx.request.request_id,
            "per_case": trust.get("per_case", {}),
            "tests_returncode": tests_returncode,
            "scope": run_tests_result.get("scope", "full"),
            "argv": run_tests_result.get("argv", []),
        },
    )
    if not passed:
        raise StepFailed(reason)
    return {"fp": fp, "passed": True}


def _reconcile_false(ctx: StepContext) -> bool:
    return False


def _reconcile_record_bench(ctx: StepContext) -> bool:
    """True iff bench evidence for this request_id was already recorded
    AND it reported passed=True. A crash after _run_record_bench's
    evidence.record but before its own `raise StepFailed(reason)` is
    the case this guards: that recorded failure's mere existence must
    not read back as success on resume -- it re-raises the same
    reason, so the request still terminates 'failed'. Duplicate
    evidence rows are still avoided either way: the passed=True branch
    returns True without a second evidence.record call, same as
    before."""
    for row in ctx.ledger.evidence(ctx.attempt_id, "bench"):
        if row.payload.get("request_id") == ctx.request.request_id:
            if row.payload.get("passed"):
                return True
            raise StepFailed(str(row.payload.get("reason", "tests_failed")))
    return False


def steps(ctx: StepContext) -> list[StepDef]:
    return [
        StepDef("compute_fingerprint", _run_compute_fingerprint, _reconcile_false),
        StepDef("run_tests", _run_run_tests, _reconcile_false),
        StepDef("trust_checks", _run_trust_checks, _reconcile_false),
        StepDef("record_bench", _run_record_bench, _reconcile_record_bench),
    ]


# Public aliases for cross-module callers (engine.application.acceptance_bench,
# engine.application.acceptance, engine.application.acceptance_probe all reuse this bench
# sandbox policy exactly -- see those modules' own docstrings) -- the
# implementations above keep their leading-underscore names since
# bench_run.py's own internal callers, and test_sandbox.py's direct
# bench_run._bench_sandbox_argv access, both already use them.
bench_env = _bench_env
bench_sandbox_argv = _bench_sandbox_argv
TAIL_CHARS = _TAIL_CHARS
