"""Pure parsing tests for fence_parse.py (no subprocess, no transcript file)."""
import time

import fence_parse as fp


def test_negation_not_opened():
    events, rejected = fp.parse_events("승인 안 함")
    assert events == []
    assert "안" in rejected


def test_quoted_mid_sentence_not_recognized():
    events, _ = fp.parse_events("'승인 X'를 입력하면 열립니다")
    assert events == []


def test_code_block_not_recognized():
    events, _ = fp.parse_events("설명: ```\n승인 X\n```")
    assert events == []


def test_multi_name_opens_both():
    events, _ = fp.parse_events("승인 A, 승인 B 추천대로 진행해")
    opened = [n for k, n in events if k == "open"]
    assert set(opened) == {"A", "B"}


def test_same_message_open_then_cancel_ends_closed():
    open_tasks = fp.replay([(1.0, "승인 전체, 승인 취소")])
    assert open_tasks == set()


def test_same_message_cancel_then_open_ends_open():
    open_tasks = fp.replay([(1.0, "승인 취소, 승인 G1")])
    assert open_tasks == {"G1"}


def test_ordering_across_messages_by_timestamp_not_insertion():
    # Passed in reversed insertion order; replay must still sort by timestamp.
    open_tasks = fp.replay([(2.0, "승인 취소"), (1.0, "승인 G1")])
    assert open_tasks == set()


def test_negation_suffix_hajima():
    # Negation stem "하지" right after the name, with an inflectional ending ("마") on the
    # following word.
    events, _ = fp.parse_events("승인 G1 하지 마")
    assert [n for k, n in events if k == "open"] == []


def test_negation_suffix_anhamnida():
    # The captured name itself is a negation stem + ending.
    events, _ = fp.parse_events("승인 안합니다")
    assert events == []


def test_negation_suffix_an_halge():
    # Name is the bare negation stem "안".
    events, _ = fp.parse_events("승인 안 할게")
    assert events == []


def test_quadratic_regex_fixed_32000_newlines():
    # A message with a long run of newlines must parse comfortably under 50ms, not quadratic
    # in the number of separator characters.
    text = "\n" * 32000 + "승인 G1"
    start = time.perf_counter()
    fp.parse_events(text)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 50, f"took {elapsed_ms:.1f}ms, expected <50ms"


def test_hook_name_pattern_includes_extra_names():
    rx = fp.hook_name_pattern(["site-local-hook"])
    assert rx.search("cat site-local-hook.py")
    assert rx.search("cat scope_guard.py")


def test_hook_name_pattern_default_has_no_extras():
    rx = fp.hook_name_pattern()
    assert not rx.search("cat something-unrelated.py")
