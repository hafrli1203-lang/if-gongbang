"""Unit tests for engine.steps.review (the review request kind): spawning
a reviewer against the repo's current diff and recording pass/fail
evidence bound to a fingerprint -- using ScriptedRunner, never a real AI."""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from engine.config import McpServerConfig, WorkerConfig
from engine.domain.records import Event
from engine.evidence import fingerprint
from engine.fakes.installer import FakeInstaller
from engine.fakes.publisher import FakePublisher
from engine.processor import Fakes, Processor, StepContext
from engine.processor import submit as submit_request
from engine.steps import review
from engine.steps.open_job import open_job
from engine.workers.scripted import ScriptedRunner
from tests.conftest import GIT_ENV, Workspace, git


def _diff_text_now(ws: Workspace, attempt_id: str, rid: str) -> str:
    """The diff_text review._run_spawn_reviewer would build for this
    request RIGHT NOW: fingerprints the live repo (same call
    compute_fingerprint makes) and asks review._diff_text for the
    base_head..fp diff -- without going through a full processor run,
    for tests that only care about the diff text's own shape."""
    req = ws.ledger.request(rid)
    ctx = StepContext(config=ws.config, ledger=ws.ledger, fakes=None,
                      request=req, project="alpha", attempt_id=attempt_id)
    fp = fingerprint(ws.alpha, ws.config.projects["alpha"].excludes)
    return review._diff_text(ctx, fp)


def _review_workspace(workspace: Workspace) -> Workspace:
    config = replace(
        workspace.config,
        workers={"reviewer": WorkerConfig(runner="scripted", timeout_sec=5.0)},
        config_path=workspace.root / "engine.json",
    )
    return Workspace(workspace.root, config, workspace.ledger,
                     workspace.alpha, workspace.beta)


def _with_file_dep_targets(ws: Workspace, targets: tuple[tuple[str, str], ...]) -> Workspace:
    """A Workspace whose "alpha" project carries `file_dep_targets` --
    the (relspec, target) pairs a `file:../...` package.json dependency
    resolves to (engine.application.steps.file_deps), used here to
    exercise a review finding's evidence-fingerprint wiring in record_review."""
    project = replace(ws.config.projects["alpha"], file_dep_targets=targets)
    config = replace(ws.config, projects={**ws.config.projects, "alpha": project})
    return Workspace(ws.root, config, ws.ledger, ws.alpha, ws.beta)


def _write_script(path: Path, items: list[dict[str, object]]) -> None:
    path.write_text(json.dumps(items), encoding="utf-8")


def _processor(ws: Workspace, script: Path) -> Processor:
    fakes = Fakes(
        publisher=FakePublisher(), installer=FakeInstaller(),
        runners={"scripted": ScriptedRunner(script, read_only=True)},
    )
    proc = Processor(config=ws.config, ledger=ws.ledger, fakes=fakes, holder="p1")
    proc.register("review", review.steps)
    return proc


def _submit_review(ws: Workspace, task: str = "do the task") -> tuple[str, str]:
    attempt_id = open_job(ws.config, ws.ledger, "alpha", "review-job",
                          "full", ())
    payload: dict[str, object] = {"role": "reviewer"}
    if task:
        payload["task"] = task
    rid = submit_request(ws.ledger, "alpha", attempt_id, "review", payload, "owner")
    return attempt_id, rid


def _verdict_script(passed: bool, findings: list[dict[str, object]] | None = None,
                    coverage: str = "full") -> list[dict[str, object]]:
    return [{"final": {"passed": passed, "coverage": coverage, "findings": findings or []}}]


def test_allowed_mcp_servers_intersects_payload_station_allow_and_config(
    workspace: Workspace,
) -> None:
    """review._allowed_mcp_servers narrows this request's payload
    "tools"."mcp" names to BOTH config.station_mcp_allow["review"] and
    config.mcp_servers -- same contract as engine.steps.station's own
    helper, keyed to the fixed "review" station rather than a payload
    "station" field (review requests have no such field)."""
    ws = _review_workspace(workspace)
    config = replace(
        ws.config,
        mcp_servers={
            "playwright": McpServerConfig(command=("npx", "-y", "@playwright/mcp")),
            "other": McpServerConfig(command=("x",)),
        },
        station_mcp_allow={"review": ("playwright",)},
    )
    attempt_id = open_job(config, ws.ledger, "alpha", "review-mcp-job", "full", ())
    rid = submit_request(
        ws.ledger, "alpha", attempt_id, "review",
        {"role": "reviewer", "task": "do the task",
         "tools": {"mcp": ["playwright", "other", "not_configured"]}},
        "owner",
    )
    request = ws.ledger.request(rid)
    ctx = StepContext(config=config, ledger=ws.ledger, fakes=None,
                      request=request, project="alpha", attempt_id=attempt_id)

    assert list(review._allowed_mcp_servers(ctx)) == ["playwright"]


def test_review_records_evidence_bound_to_fingerprint(workspace: Workspace) -> None:
    ws = _review_workspace(workspace)
    script = ws.root / "script.json"
    _write_script(script, _verdict_script(passed=True))
    attempt_id, rid = _submit_review(ws)
    proc = _processor(ws, script)
    n = proc.run_once("alpha")
    assert n == 1

    req = ws.ledger.request(rid)
    assert req.status == "done"
    fp = fingerprint(ws.alpha, ws.config.projects["alpha"].excludes)
    evidence = ws.ledger.evidence(attempt_id, "review")
    assert len(evidence) == 1
    assert evidence[0].fingerprint == fp
    assert evidence[0].payload["passed"] is True
    assert evidence[0].payload["request_id"] == rid


def test_review_evidence_fingerprint_covers_file_dep_target_content(
    workspace: Workspace, tmp_path: Path,
) -> None:
    """a review finding reproduction: a project depends on a
    package OUTSIDE its own repo via `file:../_shared/kit`. None of
    that content is part of alpha's own git tree, so record_review's
    recorded evidence fingerprint must still move when the kit's
    content changes -- otherwise a review stays "valid" for commit's
    three_match check forever, no matter what the real dependency code
    becomes."""
    kit = tmp_path / "_shared" / "kit"
    kit.mkdir(parents=True)
    (kit / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
    targets = (("../_shared/kit", str(kit)),)

    ws = _review_workspace(_with_file_dep_targets(workspace, targets))
    script = ws.root / "script.json"
    _write_script(script, _verdict_script(passed=True))
    attempt_id, rid = _submit_review(ws)
    proc = _processor(ws, script)
    n = proc.run_once("alpha")
    assert n == 1
    assert ws.ledger.request(rid).status == "done"

    recorded_fp = ws.ledger.evidence(attempt_id, "review")[0].fingerprint
    plain_tree_fp = fingerprint(ws.alpha, ws.config.projects["alpha"].excludes)
    assert recorded_fp != plain_tree_fp  # kit awareness actually changed it

    # The kit's content edited AFTER review ran: the fingerprint this
    # engine would now recompute for the SAME tree+targets must differ
    # from what record_review already recorded (RED before the fix: the
    # recorded fingerprint was plain_tree_fp either way, so it would
    # have kept "matching" here even though the real kit moved).
    (kit / "index.js").write_text("module.exports = 2;\n", encoding="utf-8")
    now_fp = fingerprint(ws.alpha, ws.config.projects["alpha"].excludes, targets)
    assert now_fp != recorded_fp


def test_review_verdict_with_p1_finding_records_passed_false_and_fails(
    workspace: Workspace,
) -> None:
    """`passed` is derived from `findings`' severities, not the
    reviewer's own boolean -- a P1 finding blocks regardless of what
    the reviewer itself claimed; the request ends 'failed' with
    "review_not_passed", and the evidence (recorded first) stays
    visible with both the derived `passed` and the raw
    `reviewer_passed`."""
    ws = _review_workspace(workspace)
    script = ws.root / "script.json"
    _write_script(script, _verdict_script(
        passed=True,
        findings=[{"severity": "P1", "file": "a.py", "line": 1, "note": "x"}],
    ))
    attempt_id, rid = _submit_review(ws)
    proc = _processor(ws, script)
    n = proc.run_once("alpha")
    assert n == 0  # StepFailed -> not counted as 'done', same as a hard failure

    req = ws.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "review_not_passed"}
    evidence = ws.ledger.evidence(attempt_id, "review")
    assert len(evidence) == 1
    assert evidence[0].payload["passed"] is False
    assert evidence[0].payload["reviewer_passed"] is True


def test_review_verdict_with_only_p2_findings_still_passes(
    workspace: Workspace,
) -> None:
    """Live observation this closes: a reviewer that returns
    passed=false over P2/P3 findings alone must not block the job --
    only P0/P1 findings do."""
    ws = _review_workspace(workspace)
    script = ws.root / "script.json"
    _write_script(script, _verdict_script(
        passed=False,
        findings=[
            {"severity": "P2", "file": "a.py", "line": 1, "note": "x"},
            {"severity": "P3", "file": "b.py", "line": 2, "note": "y"},
        ],
    ))
    attempt_id, rid = _submit_review(ws)
    proc = _processor(ws, script)
    n = proc.run_once("alpha")
    assert n == 1

    req = ws.ledger.request(rid)
    assert req.status == "done"
    evidence = ws.ledger.evidence(attempt_id, "review")
    assert len(evidence) == 1
    assert evidence[0].payload["passed"] is True
    assert evidence[0].payload["reviewer_passed"] is False


def test_review_prompt_contains_the_original_task_text(workspace: Workspace) -> None:
    """The reviewer must see the ORIGINAL request text the builder was
    given, not an empty task -- it judges the change against what was
    actually asked for, not the diff alone."""
    ws = _review_workspace(workspace)
    script = ws.root / "script.json"
    _write_script(script, _verdict_script(passed=True))
    attempt_id, rid = _submit_review(ws, task="Add a greet() function that says hi.")

    captured: dict[str, object] = {}
    from engine.workers import prompts as prompts_mod
    original_review_prompt = prompts_mod.review_prompt
    import engine.steps.review as review_mod

    def _capturing_review_prompt(spec, original_task, changed_files, diff_text, coverage_sent):
        captured["original_task"] = original_task
        return original_review_prompt(spec, original_task, changed_files, diff_text, coverage_sent)

    old = review_mod.review_prompt
    review_mod.review_prompt = _capturing_review_prompt
    try:
        proc = _processor(ws, script)
        n = proc.run_once("alpha")
    finally:
        review_mod.review_prompt = old

    assert n == 1
    assert captured["original_task"] == "Add a greet() function that says hi."


def test_review_partial_coverage_never_passes(workspace: Workspace) -> None:
    """A reviewer that reports coverage="partial" (it was only shown
    the file list and part of the diff) must never pass the review,
    regardless of what findings it reports -- "no blocking findings"
    from a partial read proves nothing about the part it never saw.
    The request fails closed with "review_incomplete", not the
    ordinary "review_not_passed". The persisted "coverage" is the
    engine's own calculation (here "full": this diff is small enough
    to fit the whole-diff budget) -- "reviewer_coverage" keeps the
    reviewer's own claim separately, for audit."""
    ws = _review_workspace(workspace)
    script = ws.root / "script.json"
    _write_script(script, _verdict_script(passed=True, coverage="partial"))
    attempt_id, rid = _submit_review(ws)
    proc = _processor(ws, script)
    n = proc.run_once("alpha")
    assert n == 0

    req = ws.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "review_incomplete"}
    evidence = ws.ledger.evidence(attempt_id, "review")
    assert len(evidence) == 1
    assert evidence[0].payload["passed"] is False
    assert evidence[0].payload["reviewer_passed"] is True
    assert evidence[0].payload["coverage"] == "full"
    assert evidence[0].payload["reviewer_coverage"] == "partial"


@pytest.mark.parametrize(
    "verdict",
    [
        pytest.param({}, id="empty_verdict"),
        pytest.param({"passed": True, "findings": {"x": 1}}, id="dict_valued_findings"),
        pytest.param(
            {"passed": False, "findings": [
                {"severity": "critical", "file": "a.py", "line": 1, "note": "x"},
            ]},
            id="unknown_severity_string",
        ),
        pytest.param(
            {"passed": False, "findings": [
                {"severity": "p1", "file": "a.py", "line": 1, "note": "x"},
            ]},
            id="lowercase_severity_not_in_closed_set",
        ),
        pytest.param(
            {"passed": "yes", "findings": []}, id="passed_not_a_bool",
        ),
        pytest.param(
            {"passed": True, "findings": [
                {"severity": "P1", "file": 1, "note": "x"},
            ]},
            id="file_not_a_string",
        ),
    ],
)
def test_malformed_or_unrecognized_verdict_fails_closed(
    workspace: Workspace, verdict: dict[str, object],
) -> None:
    """`{}`, a dict-valued "findings", or an unrecognized/wrong-case
    severity string must never silently persist passed=True just
    because no finding was recognized as a finding at all. Any of
    these must fail the request closed with "review_malformed"
    instead, before anything is derived from the verdict or recorded
    as evidence."""
    ws = _review_workspace(workspace)
    script = ws.root / "script.json"
    _write_script(script, [{"final": verdict}])
    attempt_id, rid = _submit_review(ws)
    proc = _processor(ws, script)
    n = proc.run_once("alpha")
    assert n == 0

    req = ws.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "review_malformed"}
    assert ws.ledger.evidence(attempt_id, "review") == []


def test_review_fails_when_tree_changes_during_review(workspace: Workspace) -> None:
    ws = _review_workspace(workspace)
    script = ws.root / "script.json"
    # The reviewer "runs" for long enough that the mutator thread below
    # has time to edit the repo after compute_fingerprint has already
    # read the pre-mutation content, but before record_review re-reads it.
    _write_script(script, [
        {"sleep": 1.0},
        {"final": {"passed": True, "findings": []}},
    ])
    attempt_id, rid = _submit_review(ws)
    proc = _processor(ws, script)

    import threading
    import time

    def _mutate() -> None:
        # Wait until compute_fingerprint has been journaled 'done' (so
        # its fp reflects the ORIGINAL content), then mutate -- a fixed
        # sleep would race compute_fingerprint's own git subprocess
        # calls, which release the GIL and can let this thread run
        # before compute_fingerprint has finished reading the file. A
        # separate Ledger connection is required: sqlite3 connections
        # default to check_same_thread=True (WAL mode allows concurrent
        # readers across separate connections just fine).
        from engine.ledger import Ledger

        reader = Ledger.open(ws.config.ledger_path)
        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                row = reader.steps(rid).get("compute_fingerprint")
                if row is not None and row.status == "done":
                    break
                time.sleep(0.01)
        finally:
            reader.close()
        (ws.alpha / "src" / "app.py").write_text("print('mutated')\n", encoding="utf-8")

    thread = threading.Thread(target=_mutate)
    thread.start()
    n = proc.run_once("alpha")
    thread.join()

    assert n == 0
    req = ws.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "tree_changed_during_review"}
    assert ws.ledger.evidence(attempt_id, "review") == []


def test_review_refuses_non_read_only_runner(workspace: Workspace) -> None:
    """A reviewer runs directly against the real project repo (no copy),
    so only a runner that cannot write (read_only=True) may be used for
    it -- a ScriptedRunner built with the default read_only=False (a
    builder-shaped runner) must be refused before it ever spawns."""
    ws = _review_workspace(workspace)
    script = ws.root / "script.json"
    _write_script(script, _verdict_script(passed=True))
    attempt_id, rid = _submit_review(ws)
    fakes = Fakes(
        publisher=FakePublisher(), installer=FakeInstaller(),
        runners={"scripted": ScriptedRunner(script)},  # read_only defaults False
    )
    proc = Processor(config=ws.config, ledger=ws.ledger, fakes=fakes, holder="p1")
    proc.register("review", review.steps)
    n = proc.run_once("alpha")
    assert n == 0

    req = ws.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "reviewer_not_read_only"}
    assert ws.ledger.evidence(attempt_id, "review") == []


def test_review_codex_reviewer_with_mcp_tools_fails_clearly(workspace: Workspace) -> None:
    """codex's own CLI (the real, read_only reviewer runner this engine
    ships) has no --mcp-config wiring here -- a review request that
    carries mcp tools for a codex-runner reviewer must fail with a
    clear reason rather than silently run with none of the requested
    mcp access (which would look identical to a request that never
    asked for any)."""
    ws = _review_workspace(workspace)
    config = replace(
        ws.config,
        workers={"reviewer": WorkerConfig(runner="codex", timeout_sec=5.0)},
        mcp_servers={"library": McpServerConfig(command=("python3", "-m", "lib"))},
        station_mcp_allow={"review": ("library",)},
    )
    ws = Workspace(ws.root, config, ws.ledger, ws.alpha, ws.beta)
    attempt_id = open_job(config, ws.ledger, "alpha", "review-codex-mcp-job", "full", ())
    rid = submit_request(
        ws.ledger, "alpha", attempt_id, "review",
        {"role": "reviewer", "task": "do the task", "tools": {"mcp": ["library"]}},
        "owner",
    )
    fakes = Fakes(publisher=FakePublisher(), installer=FakeInstaller(), runners={})
    proc = Processor(config=config, ledger=ws.ledger, fakes=fakes, holder="p1")
    proc.register("review", review.steps)
    proc.run_once("alpha")

    req = ws.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "reviewer_mcp_unsupported"}
    assert ws.ledger.evidence(attempt_id, "review") == []


def test_review_codex_reviewer_without_mcp_tools_is_unaffected(workspace: Workspace) -> None:
    """A codex reviewer that was never asked for any mcp tool keeps
    failing for whatever OTHER reason it would have before this check
    existed (here: no scripted/codex runner registered in fakes) --
    the new check must never fire for a plain request."""
    ws = _review_workspace(workspace)
    config = replace(ws.config, workers={"reviewer": WorkerConfig(runner="codex", timeout_sec=5.0)})
    ws = Workspace(ws.root, config, ws.ledger, ws.alpha, ws.beta)
    attempt_id, rid = _submit_review(ws)
    fakes = Fakes(publisher=FakePublisher(), installer=FakeInstaller(), runners={})
    proc = Processor(config=config, ledger=ws.ledger, fakes=fakes, holder="p1")
    proc.register("review", review.steps)
    proc.run_once("alpha")

    req = ws.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "no_runner"}


def test_review_no_worker_config_fails(workspace: Workspace) -> None:
    ws = _review_workspace(workspace)
    ws = Workspace(ws.root, replace(ws.config, workers={}), ws.ledger,
                   ws.alpha, ws.beta)
    script = ws.root / "script.json"
    _write_script(script, _verdict_script(passed=True))
    attempt_id, rid = _submit_review(ws)
    proc = _processor(ws, script)
    proc.run_once("alpha")
    req = ws.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "no_worker_config"}


# ---- P1: review's diff must include untracked (brand-new) files --------

def test_diff_text_includes_new_untracked_file_content(workspace: Workspace) -> None:
    ws = _review_workspace(workspace)
    attempt_id, rid = _submit_review(ws)
    (ws.alpha / "brand_new.py").write_text("print('new')\n", encoding="utf-8")
    text = _diff_text_now(ws, attempt_id, rid)
    assert "brand_new.py" in text
    assert "print('new')" in text


def test_diff_text_notes_binary_without_inlining(workspace: Workspace) -> None:
    ws = _review_workspace(workspace)
    attempt_id, rid = _submit_review(ws)
    (ws.alpha / "blob.bin").write_bytes(b"\x00\x01\x02binary stuff")
    text = _diff_text_now(ws, attempt_id, rid)
    assert "blob.bin" in text
    assert "Binary files" in text
    assert "binary stuff" not in text


def test_diff_text_itself_is_never_truncated(workspace: Workspace) -> None:
    """_diff_text (the raw base_head..fp diff) is no longer capped on
    its own -- sizing/truncation for the prompt is _prepare_diff_for_
    prompt's job (see below), so a single huge new file's FULL diff
    must come back from _diff_text itself, uncut."""
    ws = _review_workspace(workspace)
    attempt_id, rid = _submit_review(ws)
    big = "x\n" * (review._TOTAL_DIFF_BUDGET)
    (ws.alpha / "huge.txt").write_text(big, encoding="utf-8")
    text = _diff_text_now(ws, attempt_id, rid)
    assert len(text) > review._TOTAL_DIFF_BUDGET


def test_prepare_diff_for_prompt_marks_partial_over_budget() -> None:
    """A diff that fits the total budget is sent in full (coverage
    "full"); one that does not is truncated to the budget and marked
    "partial" -- the reviewer's own JSON reply must then echo
    "partial" back, and a "partial" verdict can never pass review
    regardless of findings (see _run_record_review)."""
    small = "x" * 100
    sent, coverage = review._prepare_diff_for_prompt(small)
    assert coverage == "full"
    assert small in sent

    huge = "y" * (review._TOTAL_DIFF_BUDGET + 1)
    sent, coverage = review._prepare_diff_for_prompt(huge)
    assert coverage == "partial"
    # The sent text is chunk-labeled, so its length is a little over
    # the raw budget, but it must never include anything past it.
    assert "y" * (review._TOTAL_DIFF_BUDGET + 1) not in sent

    empty_sent, empty_coverage = review._prepare_diff_for_prompt("")
    assert empty_coverage == "full"
    assert empty_sent == "(no diff)"


def test_review_prompt_coverage_full_for_diff_over_60k_under_budget(
    workspace: Workspace,
) -> None:
    """A diff over the OLD single-chunk 60k-char limit, but still under
    the 240k-char total budget, is sent in full across multiple ≤60k-
    char chunks in ONE prompt -- coverage stays "full", not "partial",
    because every chunk was actually sent."""
    ws = _review_workspace(workspace)
    attempt_id, rid = _submit_review(ws)
    big_content = "x\n" * 40_000  # file alone already exceeds 60k chars
    (ws.alpha / "big_file.py").write_text(big_content, encoding="utf-8")

    fp = fingerprint(ws.alpha, ws.config.projects["alpha"].excludes)
    full_diff = review._diff_text(
        StepContext(config=ws.config, ledger=ws.ledger, fakes=None,
                   request=ws.ledger.request(rid), project="alpha", attempt_id=attempt_id),
        fp,
    )
    assert len(full_diff) > review._CHUNK_MAX_CHARS
    assert len(full_diff) <= review._TOTAL_DIFF_BUDGET

    sent, coverage = review._prepare_diff_for_prompt(full_diff)
    assert coverage == "full"
    assert sent.count("--- diff chunk") > 1
    # Stripping the chunk-header markers (and the join newline that
    # came with each) back out reconstructs the whole diff exactly --
    # every chunk was sent, nothing was dropped.
    stripped = re.sub(r"(^|\n)--- diff chunk \d+/\d+ ---\n", "", sent)
    assert stripped == full_diff


def test_diff_text_empty_when_nothing_changed(workspace: Workspace) -> None:
    ws = _review_workspace(workspace)
    attempt_id, rid = _submit_review(ws)
    assert _diff_text_now(ws, attempt_id, rid) == ""


def test_review_fails_closed_when_engine_sent_partial_but_reviewer_claims_full(
    workspace: Workspace,
) -> None:
    """The engine's OWN record of how much diff it actually sent
    (coverage_sent, recomputed independently in record_review, never
    trusted from what spawn_reviewer happened to compute) must agree
    with the reviewer's reported coverage for a pass -- a reviewer
    that reports "full" when the engine itself only sent a partial
    diff must still fail "review_incomplete", not be taken at its
    word. persisted "coverage" is the engine's own ("partial"),
    "reviewer_coverage" the reviewer's claim ("full")."""
    ws = _review_workspace(workspace)
    attempt_id, rid = _submit_review(ws)
    big_content = "x\n" * (review._TOTAL_DIFF_BUDGET)
    (ws.alpha / "huge.txt").write_text(big_content, encoding="utf-8")

    script = ws.root / "script.json"
    _write_script(script, _verdict_script(passed=True, coverage="full"))
    proc = _processor(ws, script)
    n = proc.run_once("alpha")
    assert n == 0

    req = ws.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "review_incomplete"}
    evidence = ws.ledger.evidence(attempt_id, "review")
    assert len(evidence) == 1
    assert evidence[0].payload["passed"] is False
    assert evidence[0].payload["coverage"] == "partial"
    assert evidence[0].payload["reviewer_coverage"] == "full"


def test_review_recovers_missing_task_from_run_station_request(
    workspace: Workspace,
) -> None:
    """A review request submitted with no "task" (e.g.
    stages_after_install()'s own empty default) must still show the
    reviewer the ORIGINAL request -- recovered from the attempt's own
    run_station request via engine.fixup.original_task -- rather than
    reviewing against an empty task."""
    ws = _review_workspace(workspace)
    attempt_id = open_job(ws.config, ws.ledger, "alpha", "review-job", "full", ())
    submit_request(ws.ledger, "alpha", attempt_id, "run_station",
                   {"station": "build", "role": "builder",
                    "task": "Recovered original task text."}, "owner")
    rid = submit_request(ws.ledger, "alpha", attempt_id, "review", {"role": "reviewer"},
                         "owner")

    captured: dict[str, object] = {}
    from engine.workers import prompts as prompts_mod
    original_review_prompt = prompts_mod.review_prompt
    import engine.steps.review as review_mod

    def _capturing_review_prompt(spec, original_task, changed_files, diff_text, coverage_sent):
        captured["original_task"] = original_task
        return original_review_prompt(spec, original_task, changed_files, diff_text, coverage_sent)

    old = review_mod.review_prompt
    review_mod.review_prompt = _capturing_review_prompt
    try:
        script = ws.root / "script.json"
        _write_script(script, _verdict_script(passed=True))
        proc = _processor(ws, script)
        n = proc.run_once("alpha")
    finally:
        review_mod.review_prompt = old

    assert n == 1
    assert captured["original_task"] == "Recovered original task text."


def test_review_fails_incomplete_when_no_task_can_be_recovered(
    workspace: Workspace,
) -> None:
    """A review request with no task, and no run_station request on
    its attempt to recover one from either, must fail closed with
    "review_incomplete" rather than silently reviewing against an
    empty task."""
    ws = _review_workspace(workspace)
    attempt_id = open_job(ws.config, ws.ledger, "alpha", "review-job", "full", ())
    rid = submit_request(ws.ledger, "alpha", attempt_id, "review", {"role": "reviewer"},
                         "owner")
    script = ws.root / "script.json"
    _write_script(script, _verdict_script(passed=True))
    proc = _processor(ws, script)
    n = proc.run_once("alpha")
    assert n == 0

    req = ws.ledger.request(rid)
    assert req.status == "failed"
    assert req.result == {"reason": "review_incomplete"}


def test_review_git_calls_disable_fsmonitor_and_hooks(workspace: Workspace) -> None:
    """review.py's own git calls (_diff_text, _export_review_snapshot)
    must use engine.gitutil's shared helper, which neutralizes
    core.fsmonitor/hooksPath -- plant a core.fsmonitor command in the
    repo's .git/config that writes a canary file, and confirm
    _diff_text never executes it."""
    ws = _review_workspace(workspace)
    attempt_id, rid = _submit_review(ws)
    canary = ws.alpha / "fsmonitor-canary.txt"
    git_config = ws.alpha / ".git" / "config"
    git_config.write_text(
        git_config.read_text(encoding="utf-8")
        + f'\n[core]\n\tfsmonitor = "sh -c \'touch {canary}\'"\n'
        + '[core]\n\thooksPath = "' + str(ws.root / "fake-hooks") + '"\n',
        encoding="utf-8",
    )
    (ws.alpha / "untracked.txt").write_text("x\n", encoding="utf-8")

    _diff_text_now(ws, attempt_id, rid)

    assert not canary.exists()


def test_export_review_snapshot_ignores_export_attributes(workspace: Workspace) -> None:
    """`git archive` honors `export-ignore` (omits the path entirely)
    and `export-subst` (rewrites a `$Format:...$` placeholder) --
    neither is appropriate for a snapshot that must match the
    fingerprinted tree exactly, since evidence.record ties the
    reviewer's pass/fail to that exact tree, not to whatever a
    repository's own .gitattributes happens to let `git archive`
    show. Both files must appear in the snapshot with their precise
    committed bytes, `export-ignore` notwithstanding and the
    `$Format:%H$` placeholder left completely unexpanded."""
    ws = _review_workspace(workspace)
    (ws.alpha / "secret.txt").write_text("TOP SECRET\n", encoding="utf-8")
    (ws.alpha / "ver.txt").write_text("$Format:%H$\n", encoding="utf-8")
    (ws.alpha / ".gitattributes").write_text(
        "secret.txt export-ignore\nver.txt export-subst\n", encoding="utf-8",
    )
    git(ws.alpha, "add", "-A")
    git(ws.alpha, "commit", "-q", "-m", "add export-attributed files")
    attempt_id, rid = _submit_review(ws)

    fp = fingerprint(ws.alpha, ws.config.projects["alpha"].excludes)
    req = ws.ledger.request(rid)
    ctx = StepContext(config=ws.config, ledger=ws.ledger, fakes=None,
                      request=req, project="alpha", attempt_id=attempt_id)
    dest = review._export_review_snapshot(ctx, ws.alpha, fp)

    assert (dest / "secret.txt").read_text(encoding="utf-8") == "TOP SECRET\n"
    assert (dest / "ver.txt").read_text(encoding="utf-8") == "$Format:%H$\n"


def test_review_prompt_includes_new_untracked_file(workspace: Workspace) -> None:
    """End-to-end through the real review step: a brand-new file present
    when the review request runs must show up in the reviewer's prompt
    text, not just in the isolated helper above."""
    ws = _review_workspace(workspace)
    script = ws.root / "script.json"
    _write_script(script, _verdict_script(passed=True))
    attempt_id, rid = _submit_review(ws)
    (ws.alpha / "brand_new_module.py").write_text("x = 1\n", encoding="utf-8")

    captured: dict[str, object] = {}
    from engine.workers import prompts as prompts_mod
    original_review_prompt = prompts_mod.review_prompt

    def _capturing_review_prompt(spec, original_task, changed_files, diff_text, coverage_sent):
        captured["diff_text"] = diff_text
        captured["changed_files"] = changed_files
        return original_review_prompt(spec, original_task, changed_files, diff_text, coverage_sent)

    import engine.steps.review as review_mod
    old = review_mod.review_prompt
    review_mod.review_prompt = _capturing_review_prompt
    try:
        proc = _processor(ws, script)
        n = proc.run_once("alpha")
    finally:
        review_mod.review_prompt = old

    assert n == 1
    assert "brand_new_module.py" in captured["diff_text"]
    assert "x = 1" in captured["diff_text"]
    assert any("brand_new_module.py" in f for f in captured["changed_files"])


def test_diff_text_skips_excluded_paths(workspace: Workspace) -> None:
    """Excluded paths (project config `excludes`) never appear in the
    diff at all -- evidence.range_tree's own tree construction omits
    them (or keeps their unchanged HEAD content if tracked) before the
    diff is even computed, so there is nothing review.py itself needs
    to filter out of a separately-built pseudo-diff any more (a
    symlink escaping the repo, e.g. one pointing at secrets_dir, is a
    separate case: evidence.fingerprint already refuses to compute a
    fingerprint at all for one of those -- engine.evidence's own
    tests cover that)."""
    ws = _review_workspace(workspace)
    config = replace(ws.config, projects={
        **ws.config.projects,
        "alpha": replace(ws.config.projects["alpha"], excludes=("vendor/",)),
    })
    ws = Workspace(ws.root, config, ws.ledger, ws.alpha, ws.beta)
    attempt_id, rid = _submit_review(ws)
    (ws.alpha / "vendor").mkdir()
    (ws.alpha / "vendor" / "big.txt").write_text("VENDORED\n", encoding="utf-8")
    (ws.alpha / "ok.py").write_text("print('ok')\n", encoding="utf-8")

    text = _diff_text_now(ws, attempt_id, rid)
    assert "ok.py" in text and "print('ok')" in text
    assert "VENDORED" not in text


def test_full_spec_folds_required_unapplied_skill_into_reviewer_prompt(
    workspace: Workspace,
) -> None:
    """G20: a "skills.unapplied" event (engine.application.steps.worker_step.
    _record_skills_unapplied) whose own "required_unapplied" names a
    skill for THIS attempt_id reaches the reviewer's own prompt text,
    so a plan's "반드시" skill requirement that the builder's final
    JSON never reported applying becomes something the reviewer is
    told to judge, not a gap only sitting silently on the ledger."""
    ws = _review_workspace(workspace)
    script = ws.root / "script.json"
    _write_script(script, _verdict_script(passed=True))
    attempt_id, rid = _submit_review(ws, task="do the task")
    ws.ledger.append(Event(
        project="alpha", kind="skills.unapplied", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_id,
        payload={"request_id": "some-other-request", "station": "build",
                 "unapplied": ["아우라-파밍", "기획"],
                 "required_unapplied": ["아우라-파밍"]},
    ))
    proc = _processor(ws, script)
    n = proc.run_once("alpha")
    assert n == 1
    req = ws.ledger.request(rid)
    assert req.status == "done"

    fakes = Fakes(publisher=FakePublisher(), installer=FakeInstaller(),
                  runners={"scripted": ScriptedRunner(script, read_only=True)})
    ctx = StepContext(config=ws.config, ledger=ws.ledger, fakes=fakes,
                      request=req, project="alpha", attempt_id=attempt_id)
    spec, _worker, _runner, _coverage = review._full_spec(ctx)

    assert "아우라-파밍" in spec.task
    assert "기획" not in spec.task  # only the REQUIRED one is folded in


def test_full_spec_omits_note_when_no_required_skill_went_unapplied(
    workspace: Workspace,
) -> None:
    """No "skills.unapplied" event for this attempt_id at all (the
    common case) -- the reviewer's prompt is unchanged, same as before
    this feature existed."""
    ws = _review_workspace(workspace)
    script = ws.root / "script.json"
    _write_script(script, _verdict_script(passed=True))
    attempt_id, rid = _submit_review(ws, task="do the task")
    proc = _processor(ws, script)
    n = proc.run_once("alpha")
    assert n == 1
    req = ws.ledger.request(rid)

    fakes = Fakes(publisher=FakePublisher(), installer=FakeInstaller(),
                  runners={"scripted": ScriptedRunner(script, read_only=True)})
    ctx = StepContext(config=ws.config, ledger=ws.ledger, fakes=fakes,
                      request=req, project="alpha", attempt_id=attempt_id)
    spec, _worker, _runner, _coverage = review._full_spec(ctx)

    assert "필수 스킬 미적용" not in spec.task
