"""Tests for the three 공방울타리 hooks: preflight block, directive check, read-to-end."""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pg = _load("preflight_gate_hook")
dc = _load("directive_check_hook")
rw = _load("read_whole_hook")


def _transcript(tmp: Path, records: list[dict]) -> str:
    p = tmp / "t.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    return str(p)


def _human(text: str) -> dict:
    return {"type": "user", "message": {"content": text}}


def _say(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _tool(name: str, inp: dict) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}


def _patch_apps_root(monkeypatch, apps_root: Path) -> None:
    """Point every hardcoded-apps-root constant in preflight_gate_hook.py at a throwaway folder,
    so a test can build a real app + .git under tmp_path instead of depending on this machine's
    actual host repo checkout existing at a fixed path."""
    root = str(apps_root) + "/"
    esc = re.escape(root)
    monkeypatch.setattr(pg, "APPS", root)
    monkeypatch.setattr(pg, "REDIRECT_TO_APP", re.compile(r"(?<![<>&0-9])>>?(?!&)\s*\"?'?" + esc))
    monkeypatch.setattr(pg, "ABS_PATH", re.compile(esc + r"[^\s'\"`;|&)]+"))
    monkeypatch.setattr(pg, "OUTSIDE_REDIRECT", re.compile(r"(?<![<>&])(?:[0-9]|&)?>>?\s*(?!" + esc + r")/[^\s;|&)]*"))


def _edit_app(tmp_path: Path, monkeypatch) -> dict:
    """A fake app folder with a real .git marker under tmp_path, with preflight_gate_hook.py's
    apps-root constants pointed at it, standing in for a real app checkout."""
    apps = tmp_path / "apps"
    app = apps / "if-gongbang-v2"
    (app / "engine").mkdir(parents=True)
    (app / ".git").write_text("gitdir: x", encoding="utf-8")
    _patch_apps_root(monkeypatch, apps)
    return {"tool_name": "Edit", "session_id": "sess-1",
            "tool_input": {"file_path": str(app / "engine" / "x.py")}}


# ---- preflight gate -------------------------------------------------------

def test_preflight_mode_is_block() -> None:
    assert pg.MODE == "block"


def test_preflight_blocks_edit_in_worktree_without_report(tmp_path: Path, monkeypatch) -> None:
    apps = tmp_path / "apps"
    wt = apps / "eng-wt" / "s1"
    (wt / "engine").mkdir(parents=True)
    (wt / ".git").write_text("gitdir: x", encoding="utf-8")
    monkeypatch.setattr(pg, "APPS", str(apps) + "/")
    assert pg.app_repo(str(wt / "engine/x.py")) == str(wt)
    t = _transcript(tmp_path, [_human("고쳐라")])
    out = pg.decide({"tool_name": "Edit", "tool_input": {"file_path": str(wt / "engine/x.py")},
                     "transcript_path": t})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_preflight_passes_after_four_headings(tmp_path: Path) -> None:
    t = _transcript(tmp_path, [_human("고쳐라"), _say("읽은 자료 a\n예상 위험 b\n바꿀 파일 c\n되돌리는 법 d")])
    out = pg.decide({"tool_name": "Edit", "tool_input": {"file_path": "<apps-root>/if-gongbang-v2/engine/x.py"},
                     "transcript_path": t})
    assert out is None


def test_preflight_ignores_folder_without_git(tmp_path: Path) -> None:
    assert pg.app_repo("<apps-root>/live-workspace/config.json") is None


# ---- directive check ------------------------------------------------------

DIRECTIVE = "1,2,3 다 승인한다 쭉 해라"


def _stop(tmp: Path, final: str) -> dict | None:
    t = _transcript(tmp, [_human(DIRECTIVE), _tool("Bash", {"command": "pytest"}), _say(final)])
    return dc.decide({"transcript_path": t})


def test_directive_report_without_check_is_blocked(tmp_path: Path) -> None:
    assert _stop(tmp_path, "전부 고쳤고 테스트 통과했습니다.")["decision"] == "block"


def test_directive_check_with_verbatim_quote_passes(tmp_path: Path) -> None:
    final = '## 지시 대조\n- "1,2,3 다 승인한다" → 1·2 했음, 3 설치 대기\n테스트 통과했습니다.'
    assert _stop(tmp_path, final) is None


def test_directive_check_with_paraphrase_is_blocked(tmp_path: Path) -> None:
    final = '## 지시 대조\n- "전부 진행하라고 하심" → 했음\n테스트 통과했습니다.'
    assert _stop(tmp_path, final)["decision"] == "block"


def test_directive_no_claim_is_left_alone(tmp_path: Path) -> None:
    assert _stop(tmp_path, "지금 2단계 하는 중입니다.") is None


def test_directive_loop_guard(tmp_path: Path) -> None:
    t = _transcript(tmp_path, [_human(DIRECTIVE), _say("테스트 통과했습니다.")])
    assert dc.decide({"transcript_path": t, "stop_hook_active": True}) is None


# ---- read to end ----------------------------------------------------------

def _doc(tmp: Path, lines: int, name: str = "plan.md") -> str:
    p = tmp / name
    p.write_text("".join(f"줄 {i}\n" for i in range(lines)), encoding="utf-8")
    return str(p)


def test_read_partial_doc_is_blocked(tmp_path: Path) -> None:
    doc = _doc(tmp_path, 300)
    t = _transcript(tmp_path, [_human("읽어라")])
    out = rw.decide({"tool_name": "Read", "tool_input": {"file_path": doc, "limit": 20}, "transcript_path": t})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_read_whole_doc_passes(tmp_path: Path) -> None:
    doc = _doc(tmp_path, 300)
    assert rw.decide({"tool_name": "Read", "tool_input": {"file_path": doc}}) is None


def test_read_section_after_whole_read_passes(tmp_path: Path) -> None:
    doc = _doc(tmp_path, 300)
    t = _transcript(tmp_path, [_human("읽어라"), _tool("Read", {"file_path": doc})])
    out = rw.decide({"tool_name": "Read", "tool_input": {"file_path": doc, "offset": 100, "limit": 20},
                     "transcript_path": t})
    assert out is None


def test_read_huge_doc_in_pieces_passes(tmp_path: Path) -> None:
    doc = _doc(tmp_path, 2500)
    assert rw.decide({"tool_name": "Read", "tool_input": {"file_path": doc, "limit": 2000}}) is None


def test_read_code_partial_passes(tmp_path: Path) -> None:
    code = _doc(tmp_path, 300, "x.py")
    assert rw.decide({"tool_name": "Read", "tool_input": {"file_path": code, "limit": 20}}) is None


def test_bash_head_on_doc_is_blocked(tmp_path: Path) -> None:
    doc = _doc(tmp_path, 300)
    out = rw.decide({"tool_name": "Bash", "tool_input": {"command": f"head -20 {doc}"}, "cwd": str(tmp_path)})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_bash_head_in_pipe_passes(tmp_path: Path) -> None:
    out = rw.decide({"tool_name": "Bash", "tool_input": {"command": "git log --oneline | head -5"}})
    assert out is None


def test_hooks_run_as_scripts_and_fail_open() -> None:
    for name in ("preflight_gate_hook", "directive_check_hook", "read_whole_hook"):
        res = subprocess.run(["python3", str(HERE / f"{name}.py")], input="not json",
                             capture_output=True, text=True, timeout=20)
        assert res.returncode == 0 and res.stdout == ""


def test_preflight_scratch_redirect_from_app_cwd_passes(tmp_path: Path) -> None:
    t = _transcript(tmp_path, [_human("돌려라")])
    out = pg.decide({"tool_name": "Bash", "tool_input": {"command": "pytest -q > /tmp/x/log 2>&1"},
                     "cwd": "<apps-root>/if-gongbang-v2", "transcript_path": t})
    assert out is None


def test_preflight_redirect_into_app_still_blocked(tmp_path: Path, monkeypatch) -> None:
    apps = tmp_path / "apps"
    app = apps / "if-gongbang-v2"
    (app / "engine").mkdir(parents=True)
    (app / ".git").write_text("gitdir: x", encoding="utf-8")
    _patch_apps_root(monkeypatch, apps)
    t = _transcript(tmp_path, [_human("돌려라")])
    out = pg.decide({"tool_name": "Bash", "tool_input": {"command": f"echo x > {app}/engine/a.py"},
                     "cwd": "/tmp", "transcript_path": t})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_bash_head_covering_whole_doc_passes(tmp_path: Path) -> None:
    doc = _doc(tmp_path, 300)
    out = rw.decide({"tool_name": "Bash", "tool_input": {"command": f"head -n 400 {doc}"}, "cwd": str(tmp_path)})
    assert out is None


def test_preflight_variable_redirect_passes(tmp_path: Path) -> None:
    """B4-1: a redirect to a shell variable ("$S/log") is not a literal <apps-root>/
    path, so it must not fall back to judging by cwd -- only mv/rm/sed -i/... do that."""
    t = _transcript(tmp_path, [_human("돌려라")])
    out = pg.decide({"tool_name": "Bash", "tool_input": {"command": 'pytest -q > "$S/log" 2>&1'},
                     "cwd": "<apps-root>/if-gongbang-v2", "transcript_path": t})
    assert out is None


def test_preflight_git_status_to_tmp_passes(tmp_path: Path) -> None:
    t = _transcript(tmp_path, [_human("봐라")])
    out = pg.decide({"tool_name": "Bash", "tool_input": {"command": "git status > /tmp/status.txt"},
                     "cwd": "<apps-root>/if-gongbang-v2", "transcript_path": t})
    assert out is None


def test_directive_failure_report_is_not_a_claim(tmp_path: Path) -> None:
    assert _stop(tmp_path, "테스트 통과하지 못했습니다. 오류는 아래와 같습니다.") is None


def test_directive_only_final_answer_counts(tmp_path: Path) -> None:
    t = _transcript(tmp_path, [_human(DIRECTIVE), _say("테스트 통과했습니다."),
                               _tool("Bash", {"command": "ls"}), _say("배포 권한을 알려주세요.")])
    assert dc.decide({"transcript_path": t}) is None


def test_directive_short_quote_and_image_prompt(tmp_path: Path) -> None:
    prompt = {"type": "user", "message": {"content": [{"type": "text", "text": "고쳐라"},
                                                      {"type": "image", "source": {}}]}}
    t = _transcript(tmp_path, [prompt, _say('## 지시 대조\n- "고쳐라" → 고침\n테스트 통과했습니다.')])
    assert dc.decide({"transcript_path": t}) is None


def test_read_explicit_whole_range_counts_as_whole(tmp_path: Path) -> None:
    doc = _doc(tmp_path, 300)
    t = _transcript(tmp_path, [_human("읽어라"), _tool("Read", {"file_path": doc, "offset": 1, "limit": 2000})])
    out = rw.decide({"tool_name": "Read", "tool_input": {"file_path": doc, "limit": 20}, "transcript_path": t})
    assert out is None


def test_bash_sed_whole_range_passes(tmp_path: Path) -> None:
    doc = _doc(tmp_path, 300)
    out = rw.decide({"tool_name": "Bash", "tool_input": {"command": f"sed -n '1,$p' {doc}"}, "cwd": str(tmp_path)})
    assert out is None


def _stamped_human(text: str) -> dict:
    # a fixed instant that is after the "stale report" fixture's epoch marker (1000) below but
    # long before the real wall clock, so a freshly-written report file in these tests is always
    # newer than this and a report file forced to epoch 1000 is always older than this.
    epoch_stamp = datetime.fromtimestamp(86_400, timezone.utc).isoformat().replace("+00:00", "Z")
    return {"type": "user", "message": {"content": text}, "timestamp": epoch_stamp}


_FOUR = "읽은 자료 a\n예상 위험 b\n바꿀 파일 c\n되돌리는 법 d"


def test_preflight_report_file_written_this_turn_passes(tmp_path: Path, monkeypatch) -> None:
    """Live behavior: a report written in the same message as the tool call is not in the
    transcript yet, so the per-session report file is the way through."""
    monkeypatch.setattr(pg, "REPORT_DIR", str(tmp_path))
    (tmp_path / "sess-1.md").write_text(_FOUR, encoding="utf-8")
    t = _transcript(tmp_path, [_stamped_human("고쳐라")])
    edit_app = _edit_app(tmp_path, monkeypatch)
    assert pg.decide({**edit_app, "transcript_path": t}) is None


def test_preflight_report_file_from_before_the_prompt_is_stale(tmp_path: Path, monkeypatch) -> None:
    import os
    monkeypatch.setattr(pg, "REPORT_DIR", str(tmp_path))
    f = tmp_path / "sess-1.md"
    f.write_text(_FOUR, encoding="utf-8")
    os.utime(f, (1000, 1000))
    t = _transcript(tmp_path, [_stamped_human("고쳐라")])
    edit_app = _edit_app(tmp_path, monkeypatch)
    out = pg.decide({**edit_app, "transcript_path": t})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "sess-1.md" in out["hookSpecificOutput"]["permissionDecisionReason"]


# ---- per-worker report file (foreman and workers share session_id) -----------------------

def test_worker_report_file_passes_worker_but_not_foreman(tmp_path: Path, monkeypatch) -> None:
    """A sub-agent (agent_id='w1') writing its own <session>-w1.md may proceed; the foreman
    (no agent_id), who only has the worker's file and no <session>.md of its own, stays blocked."""
    monkeypatch.setattr(pg, "REPORT_DIR", str(tmp_path))
    (tmp_path / "sess-1-w1.md").write_text(_FOUR, encoding="utf-8")
    t = _transcript(tmp_path, [_stamped_human("고쳐라")])
    edit_app = _edit_app(tmp_path, monkeypatch)
    worker_out = pg.decide({**edit_app, "agent_id": "w1", "transcript_path": t})
    assert worker_out is None
    foreman_out = pg.decide({**edit_app, "transcript_path": t})
    assert foreman_out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_foreman_report_file_does_not_pass_worker(tmp_path: Path, monkeypatch) -> None:
    """Only the foreman's own <session>.md exists; a worker (agent_id='w1') without its own
    report file stays blocked (unless its task prompt carries the four headings)."""
    monkeypatch.setattr(pg, "REPORT_DIR", str(tmp_path))
    (tmp_path / "sess-1.md").write_text(_FOUR, encoding="utf-8")
    t = _transcript(tmp_path, [_stamped_human("고쳐라")])
    edit_app = _edit_app(tmp_path, monkeypatch)
    out = pg.decide({**edit_app, "agent_id": "w1", "transcript_path": t})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_worker_reason_names_its_own_report_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pg, "REPORT_DIR", str(tmp_path))
    t = _transcript(tmp_path, [_stamped_human("고쳐라")])
    edit_app = _edit_app(tmp_path, monkeypatch)
    out = pg.decide({**edit_app, "agent_id": "w1", "transcript_path": t})
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "sess-1-w1.md" in reason


def test_agent_id_with_slash_is_denied(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pg, "REPORT_DIR", str(tmp_path))
    t = _transcript(tmp_path, [_stamped_human("고쳐라")])
    edit_app = _edit_app(tmp_path, monkeypatch)
    out = pg.decide({**edit_app, "agent_id": "../w1", "transcript_path": t})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_agent_id_with_dotdot_is_denied(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pg, "REPORT_DIR", str(tmp_path))
    t = _transcript(tmp_path, [_stamped_human("고쳐라")])
    edit_app = _edit_app(tmp_path, monkeypatch)
    out = pg.decide({**edit_app, "agent_id": "w1/..", "transcript_path": t})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
