"""Unit tests for engine.steps.review (the review request kind): spawning
a reviewer against the repo's current diff and recording pass/fail
evidence bound to a fingerprint -- using ScriptedRunner, never a real AI."""
from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from engine.config import WorkerConfig
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


def _submit_review(ws: Workspace) -> tuple[str, str]:
    attempt_id = open_job(ws.config, ws.ledger, "alpha", "review-job",
                          "full", ())
    rid = submit_request(ws.ledger, "alpha", attempt_id, "review",
                         {"role": "reviewer"}, "owner")
    return attempt_id, rid


def _verdict_script(passed: bool, findings: list[dict[str, object]] | None = None,
                    ) -> list[dict[str, object]]:
    return [{"final": {"passed": passed, "findings": findings or []}}]


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


def test_diff_text_capped_at_max_chars(workspace: Workspace) -> None:
    """A single huge new file must not blow the diff past the overall
    cap (engine.workers.prompts' own 60k-char budget) -- there is no
    longer a PER-FILE size note (git diff shows a new file's content in
    full, same as any other diff tool), so the cap is what actually
    bounds the prompt."""
    ws = _review_workspace(workspace)
    attempt_id, rid = _submit_review(ws)
    (ws.alpha / "huge.txt").write_text("x\n" * (review._DIFF_MAX_CHARS), encoding="utf-8")
    text = _diff_text_now(ws, attempt_id, rid)
    assert len(text) == review._DIFF_MAX_CHARS


def test_diff_text_empty_when_nothing_changed(workspace: Workspace) -> None:
    ws = _review_workspace(workspace)
    attempt_id, rid = _submit_review(ws)
    assert _diff_text_now(ws, attempt_id, rid) == ""


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

    captured: dict[str, str] = {}
    from engine.workers import prompts as prompts_mod
    original_review_prompt = prompts_mod.review_prompt

    def _capturing_review_prompt(spec, diff_text):
        captured["diff_text"] = diff_text
        return original_review_prompt(spec, diff_text)

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
