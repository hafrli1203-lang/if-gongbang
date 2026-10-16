"""approve_gate.py decides nothing - it only prints what it recognized. Run as a real
subprocess (like Claude Code invokes it) against a throwaway $HOME."""
import time


def test_prints_opened_for_plain_approval(fence_home):
    r = fence_home.approve("승인 G1", "s1")
    assert r.returncode == 0
    assert "열림" in r.stdout


def test_prints_not_recognized_for_negation(fence_home):
    r = fence_home.approve("승인 안 함", "s1")
    assert r.returncode == 0
    assert "관문 열림" not in r.stdout


def test_prints_closed_for_cancel(fence_home):
    r = fence_home.approve("승인 취소", "s1")
    assert r.returncode == 0
    assert "닫힘" in r.stdout


def test_unrelated_prompt_prints_nothing(fence_home):
    r = fence_home.approve("오늘 날씨 어때", "s1")
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_final_open_reflects_prior_transcript(fence_home):
    sid = "s-prior"
    now = time.time()
    fence_home.write_transcript(sid, [fence_home.human_line(now - 60, "승인 G1")])
    r = fence_home.approve("승인 G2", sid, now=now)
    assert "G1" in r.stdout and "G2" in r.stdout


def test_missing_session_id_warns_on_stderr(fence_home):
    r = fence_home.approve("승인 G1", "")
    assert r.returncode == 0
    assert "session_id" in r.stderr
