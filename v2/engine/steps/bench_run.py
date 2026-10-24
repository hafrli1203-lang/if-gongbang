"""bench — run the project's real test suite plus trust-checked cases.

compute_fingerprint  fp = evidence.fingerprint(repo); journal {fp}
                     (reconcile False: a crash mid-run re-computes rather
                     than trusting a half-written journal row).
run_tests            Speed default (project.bench_scope, "related" unless
                     set to "full"): with scope "related" and a
                     configured project.related_cmd, run related_cmd
                     against only this attempt's changed source files
                     (engine.steps.open_job.dirty_paths against HEAD,
                     the same range the fingerprint covers -- at this
                     point in the stage list HEAD still equals
                     base_head, since commit runs after bench) in place
                     of the literal "{files}" token, one argv entry per
                     file; falls back to the full project.test_cmd when
                     related_cmd is unset or there are no changed
                     source files. Runs in repo with an explicit
                     environment built from config.worker_env_passthrough
                     only (never the processor's own full os.environ,
                     which may carry secrets a project's test_cmd has
                     no business seeing) -- this is the actual suite a
                     worker's change must leave green; journal
                     {returncode, tail, scope, argv} (tail = last 2000
                     chars of combined stdout+stderr; scope is "related"
                     or "full"; argv is the exact command run). A
                     timeout -> StepFailed ("tests_timeout"). A non-zero
                     exit is NOT itself a step failure here: record_bench
                     (not this step) folds it into the overall
                     passed/reason.
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
"""
from __future__ import annotations

import json
import re
import os
import subprocess
from pathlib import Path

from engine import bench, evidence, ids, sandbox
from engine.processor import StepContext, StepDef, StepFailed
from engine.steps.open_job import dirty_paths

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
    caller that runs `argv` somewhere else entirely (engine.acceptance's
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


def _changed_source_paths(ctx: StepContext) -> tuple[list[str], bool]:
    """(existing_paths, has_removal): this attempt's changed files
    right now (dirty_paths against HEAD -- the same union the
    fingerprint covers), split into the ones that still exist on disk
    (a deleted file is not a useful argument to a "related tests"
    command) and whether ANY changed path no longer exists -- a
    deletion, or the OLD side of a rename (dirty_paths records both
    paths of a rename/copy; its old path never exists on disk after
    the rename, so it is caught by this same check). A caller must
    treat has_removal as "related-scope selection cannot be trusted
    for this change": a related-tests command driven only by the
    paths that still exist would never account for code that depended
    on a path which no longer exists at all (e.g. a deleted module
    other, untouched files still import), silently narrowing the test
    selection below what actually needs checking. Read-only: no
    write, no git state change."""
    project = _project_config(ctx)
    repo = _repo(ctx)
    paths = dirty_paths(repo, project.excludes)
    existing = [p for p in paths if (repo / p).exists()]
    return existing, len(existing) != len(paths)


def _build_related_argv(related_cmd: tuple[str, ...], files: list[str]) -> list[str]:
    """related_cmd with the single literal token "{files}" expanded in
    place into one argv entry per changed file (never joined into one
    string -- each file must be its own argv token)."""
    argv: list[str] = []
    for part in related_cmd:
        if part == "{files}":
            argv.extend(files)
        else:
            argv.append(part)
    return argv


def _resolve_test_argv(ctx: StepContext) -> tuple[str, list[str]]:
    """Returns (scope, argv) for the real-suite run_tests step. The
    request's own payload can force the full suite outright
    (payload["scope"] == "full", e.g. engine.release.ensure_full_bench
    submitting a release's required full-suite bench) -- checked
    first, ahead of the project's own related-tests shortcut, so a
    caller that specifically needs "full" never gets "related" back
    regardless of config. Otherwise "related" only when the project
    opts in (bench_scope == "related", the default) AND configures
    related_cmd AND there is at least one changed source file right
    now AND nothing was deleted or renamed (see
    _changed_source_paths's has_removal: a related-tests command
    driven only by paths that still exist can never account for code
    that depended on a path which no longer exists) -- otherwise
    "full" with the project's own test_cmd, unchanged from before this
    feature."""
    project = _project_config(ctx)
    if ctx.request.payload.get("scope") == "full":
        return "full", list(project.test_cmd)
    if project.bench_scope == "related" and project.related_cmd:
        changed, has_removal = _changed_source_paths(ctx)
        if changed and not has_removal:
            return "related", _build_related_argv(project.related_cmd, changed)
    return "full", list(project.test_cmd)


_VITEST_NO_TESTS_RE = re.compile(r"No test files found", re.I)
_VITEST_ZERO_TESTS_SUMMARY_RE = re.compile(r"^\s*Tests\s+0\s", re.I | re.M)
# A "Tests" summary line that never mentions "passed" or "failed"
# anywhere on it, but does mention "skipped" -- every selected test
# was skipped, not merely run and found correct, so nothing was
# actually exercised even though the file-selection step itself
# matched something (e.g. "Tests  1 skipped (1)"). The negative
# lookahead scans the rest of the line before "skipped" is reached, so
# a genuinely mixed line like "3 passed | 2 skipped (5)" does not
# match this (at least one real pass/fail outcome did occur there).
_VITEST_ALL_SKIPPED_RE = re.compile(
    r"^\s*Tests\s+(?!.*\b(?:passed|failed)\b).*\bskipped\b", re.I | re.M)


def _ran_zero_tests(scope: str, combined_output: str) -> bool:
    """True iff a "related"-scope run executed ZERO actual tests --
    vitest's own output for a `related` selector that no longer
    matches any file is "No test files found, exiting with code 1"
    (an installed vitest's own `passWithNoTests=true` default makes
    that exit 0, not 1, looking exactly like an ordinary clean pass),
    sometimes with no "Tests ..." summary line printed at all, or
    occasionally a "Tests  0 ..." summary. The same is true when the
    selector DID match files but every selected test was then skipped
    (vitest's own "Tests  N skipped (N)", with no passed/failed count
    on that line at all) -- a selector matching something is not the
    same as anything actually having been exercised. Only ever checked
    for scope "related" -- a "full" run legitimately exits 0 on an
    empty/trivial project without this ever applying."""
    if scope != "related":
        return False
    return bool(_VITEST_NO_TESTS_RE.search(combined_output)
                or _VITEST_ZERO_TESTS_SUMMARY_RE.search(combined_output)
                or _VITEST_ALL_SKIPPED_RE.search(combined_output))


def _run_test_cmd(ctx: StepContext, repo: Path, test_cmd: list[str],
                  env: dict[str, str]) -> tuple[int, str]:
    argv, child_env = _bench_sandbox_argv(ctx, repo, list(test_cmd), env)
    # mode="bwrap": child_env is already baked into argv via --setenv;
    # the Popen env here must be the sandbox launcher's sanitized env,
    # never child_env itself (see sandbox.exec_env's docstring).
    # mode="none": exec_env returns child_env unchanged -- there is no
    # bwrap process to separate it from.
    popen_env = sandbox.exec_env(ctx.config.sandbox, child_env)
    try:
        result = subprocess.run(
            argv, cwd=str(repo), capture_output=True,
            timeout=_timeout_sec(ctx), text=True, env=popen_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise StepFailed("tests_timeout") from exc
    combined = (result.stdout or "") + (result.stderr or "")
    return result.returncode, combined


def _run_run_tests(ctx: StepContext) -> dict[str, object]:
    scope, test_cmd = _resolve_test_argv(ctx)
    repo = _repo(ctx)
    env = _bench_env(ctx)
    returncode, combined = _run_test_cmd(ctx, repo, list(test_cmd), env)
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
        returncode, combined = _run_test_cmd(ctx, repo, list(test_cmd), env)
    return {"returncode": returncode, "tail": combined[-_TAIL_CHARS:],
            "scope": scope, "argv": list(test_cmd)}


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
                spec: dict[str, object]) -> bench.Case:
    selector = str(spec["selector"])
    name = str(spec["name"])
    env = _case_env(base_env, spec)
    argv, child_env = _bench_sandbox_argv(ctx, repo, [*test_cmd, selector], env)
    popen_env = sandbox.exec_env(ctx.config.sandbox, child_env)
    captured: dict[str, object] = {}

    def run() -> bool:
        try:
            result = subprocess.run(
                argv, cwd=str(repo), capture_output=True,
                timeout=timeout_sec, text=True, env=popen_env,
            )
        except subprocess.TimeoutExpired:
            captured["text"] = ""
            captured["returncode"] = "timeout"
            return False
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
    return [_make_case(ctx, project.test_cmd, project.repo, timeout_sec, base_env, spec)
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
    tests_ok = tests_returncode == 0
    trust = _journal_payload(ctx, "trust_checks")
    trust_passed = bool(trust["passed"])
    passed = tests_ok and trust_passed
    if not trust_passed:
        reason = str(trust["reason"])
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
