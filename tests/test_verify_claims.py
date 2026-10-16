import json

import verify_claims


def transcript(tmp_path, records):
    path = tmp_path / "t.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return str(path)


def human(text):
    return {"type": "user", "origin": {"kind": "human"}, "message": {"content": text}}


def peer(text):
    # A subagent report relayed into the main session: a "user" line whose origin kind is "peer".
    return {"type": "user", "origin": {"kind": "peer"}, "message": {"content": text}}


def assistant_text(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def assistant_tool():
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}


def assistant_bash(tool_id, command):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}}
    ]}}


def tool_result(tool_id, is_error=False, **overrides):
    rec = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tool_id, "is_error": is_error, "content": "ok"}
    ]}}
    rec.update(overrides)
    return rec


def background_launch_result(tool_id):
    return {
        "type": "user",
        "toolUseResult": {"backgroundTaskId": "task-id"},
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": tool_id, "is_error": False,
             "content": "Command running in background with ID: task-id"},
        ]},
    }


def human_with_image():
    return {"type": "user", "origin": {"kind": "human"}, "message": {"content": [
        {"type": "image", "source": {"type": "base64", "data": "x"}}, {"type": "text", "text": "이거 봐줘"},
    ]}}


def test_claim_without_tool_use_is_blocked(tmp_path):
    path = transcript(tmp_path, [human("해줘"), assistant_text("테스트 통과했습니다, 작업 완료.")])
    out = verify_claims.decide({"transcript_path": path})
    assert out is not None
    assert out["decision"] == "block"


def test_general_completion_claim_with_tool_use_is_allowed(tmp_path):
    # A general completion claim (no "테스트 통과"/"빌드 성공" wording) still only requires
    # some tool call in the turn, as before.
    path = transcript(tmp_path, [human("해줘"), assistant_tool(), assistant_text("작업 완료했습니다.")])
    out = verify_claims.decide({"transcript_path": path})
    assert out is None


def test_test_claim_with_unrelated_tool_use_is_blocked(tmp_path):
    # "테스트 통과" with a tool call that never actually ran a test command is still blocked -
    # having SOME tool call in the turn is not enough for this specific claim.
    path = transcript(tmp_path, [human("해줘"), assistant_tool(), assistant_text("테스트 통과했습니다.")])
    out = verify_claims.decide({"transcript_path": path})
    assert out is not None
    assert out["decision"] == "block"


def test_test_claim_with_real_passing_pytest_run_is_allowed(tmp_path):
    path = transcript(tmp_path, [
        human("해줘"),
        assistant_bash("t1", "python3 -m pytest tests/"),
        tool_result("t1", is_error=False),
        assistant_text("pytest 실행 결과 모두 테스트 통과했습니다."),
    ])
    out = verify_claims.decide({"transcript_path": path})
    assert out is None


def test_test_claim_with_failing_pytest_run_is_blocked(tmp_path):
    path = transcript(tmp_path, [
        human("해줘"),
        assistant_bash("t1", "pytest tests/"),
        tool_result("t1", is_error=True),
        assistant_text("테스트 통과했습니다."),
    ])
    out = verify_claims.decide({"transcript_path": path})
    assert out is not None
    assert out["decision"] == "block"


def test_image_bearing_human_message_counts_as_new_turn(tmp_path):
    # An earlier turn's real pytest run must not be reused to justify a claim made in a LATER
    # turn that starts with an image-bearing human message.
    path = transcript(tmp_path, [
        human("해줘"),
        assistant_bash("t1", "pytest tests/"),
        tool_result("t1", is_error=False),
        assistant_text("테스트 통과했습니다."),
        human_with_image(),
        assistant_text("테스트 통과했습니다."),
    ])
    out = verify_claims.decide({"transcript_path": path})
    assert out is not None
    assert out["decision"] == "block"


def test_exempt_phrase_is_allowed(tmp_path):
    path = transcript(tmp_path, [human("해줘"), assistant_text("아직 미검증 상태입니다.")])
    out = verify_claims.decide({"transcript_path": path})
    assert out is None


def test_stop_hook_active_short_circuits(tmp_path):
    path = transcript(tmp_path, [human("해줘"), assistant_text("성공했습니다.")])
    out = verify_claims.decide({"transcript_path": path, "stop_hook_active": True})
    assert out is None


def test_no_transcript_path_is_safe():
    assert verify_claims.decide({}) is None


def test_test_claim_with_only_word_mention_is_blocked(tmp_path):
    # "rg pytest README.md" merely mentions the word "pytest" as grep's own search argument -
    # its ACTUAL first command is "rg", not a test runner, so this must not count as a real
    # test run.
    path = transcript(tmp_path, [
        human("해줘"),
        assistant_bash("t1", "rg pytest README.md"),
        tool_result("t1", is_error=False),
        assistant_text("테스트 통과했습니다."),
    ])
    out = verify_claims.decide({"transcript_path": path})
    assert out is not None
    assert out["decision"] == "block"


def test_test_claim_with_missing_tool_result_is_blocked(tmp_path):
    # The Bash call ran, but no tool_result for it ever came back in this turn (e.g. the
    # transcript line is simply absent) - that must not be treated as a passing run.
    path = transcript(tmp_path, [
        human("해줘"),
        assistant_bash("t1", "pytest tests/"),
        assistant_text("테스트 통과했습니다."),
    ])
    out = verify_claims.decide({"transcript_path": path})
    assert out is not None
    assert out["decision"] == "block"


def test_test_claim_with_background_launch_result_is_blocked(tmp_path):
    # A "running in background" acknowledgement is not a completed, passing test run.
    path = transcript(tmp_path, [
        human("해줘"),
        assistant_bash("t1", "pytest tests/"),
        background_launch_result("t1"),
        assistant_text("테스트 통과했습니다."),
    ])
    out = verify_claims.decide({"transcript_path": path})
    assert out is not None
    assert out["decision"] == "block"


def test_test_claim_is_not_satisfied_by_a_passing_build_command(tmp_path):
    # pytest failed, but a later `npm run build` succeeded - a build's success must not stand
    # in for a test claim's own evidence.
    path = transcript(tmp_path, [
        human("해줘"),
        assistant_bash("t1", "pytest tests/"),
        tool_result("t1", is_error=True),
        assistant_bash("t2", "npm run build"),
        tool_result("t2", is_error=False),
        assistant_text("테스트 통과했습니다."),
    ])
    out = verify_claims.decide({"transcript_path": path})
    assert out is not None
    assert out["decision"] == "block"


def test_build_claim_with_real_passing_build_is_allowed(tmp_path):
    path = transcript(tmp_path, [
        human("해줘"),
        assistant_bash("t1", "npm run build"),
        tool_result("t1", is_error=False),
        assistant_text("빌드 성공했습니다."),
    ])
    out = verify_claims.decide({"transcript_path": path})
    assert out is None


def test_exempt_phrase_does_not_excuse_an_unexempted_claim_in_another_sentence(tmp_path):
    # The disclaimer "배포는 미검증입니다" must only excuse ITS OWN sentence - the unverified
    # "테스트 통과" claim in the sentence before it still needs real evidence.
    path = transcript(tmp_path, [
        human("해줘"),
        assistant_text("테스트 통과했습니다. 배포는 미검증입니다."),
    ])
    out = verify_claims.decide({"transcript_path": path})
    assert out is not None
    assert out["decision"] == "block"


def test_peer_message_does_not_reset_turn_boundary(tmp_path):
    # A peer/subagent relay message (also type=="user", but origin.kind == "peer") arriving
    # between a passing test run and the final claim must not be mistaken for a new human turn
    # that would cut off the earlier evidence.
    path = transcript(tmp_path, [
        human("해줘"),
        assistant_bash("t1", "pytest tests/"),
        tool_result("t1", is_error=False),
        peer("검토 종료"),
        assistant_text("테스트 통과했습니다."),
    ])
    out = verify_claims.decide({"transcript_path": path})
    assert out is None
