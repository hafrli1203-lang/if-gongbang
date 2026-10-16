import json
import os

import preflight_gate as pg


def cfg(tmp_path, mode="warn"):
    return {
        "apps_root": str(tmp_path / "apps"),
        "exempt_apps": ["_control"],
        "preflight_mode": mode,
        "log_dir": str(tmp_path / "logs"),
    }


def make_repo(tmp_path, name):
    root = tmp_path / "apps" / name
    (root / ".git").mkdir(parents=True)
    return root


def human(text):
    return {"type": "user", "origin": {"kind": "human"}, "message": {"content": text}}


def peer(text):
    # A subagent report relayed into the main session: a "user" line whose origin kind is "peer".
    return {"type": "user", "origin": {"kind": "peer"}, "message": {"content": text}}


def assistant_text(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def write_transcript(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_edit_without_report_is_flagged_in_warn_mode(tmp_path):
    c = cfg(tmp_path)
    repo = make_repo(tmp_path, "demo")
    transcript = tmp_path / "t.jsonl"
    write_transcript(transcript, [human("고쳐줘")])
    data = {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "main.py")},
            "transcript_path": str(transcript)}
    out = pg.decide(data, c)
    assert out is not None
    assert "systemMessage" in out


def test_edit_with_report_is_silent(tmp_path):
    c = cfg(tmp_path)
    repo = make_repo(tmp_path, "demo")
    transcript = tmp_path / "t.jsonl"
    report = "읽은 자료: x\n예상 위험: y\n바꿀 파일: z\n되돌리는 법: w"
    write_transcript(transcript, [human("고쳐줘"), assistant_text(report)])
    data = {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "main.py")},
            "transcript_path": str(transcript)}
    assert pg.decide(data, c) is None


def test_block_mode_denies(tmp_path):
    c = cfg(tmp_path, mode="block")
    repo = make_repo(tmp_path, "demo")
    transcript = tmp_path / "t.jsonl"
    write_transcript(transcript, [human("고쳐줘")])
    data = {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "main.py")},
            "transcript_path": str(transcript)}
    out = pg.decide(data, c)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_markdown_file_is_exempt(tmp_path):
    c = cfg(tmp_path)
    repo = make_repo(tmp_path, "demo")
    transcript = tmp_path / "t.jsonl"
    write_transcript(transcript, [human("고쳐줘")])
    data = {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "README.md")},
            "transcript_path": str(transcript)}
    assert pg.decide(data, c) is None


def test_exempt_app_is_skipped(tmp_path):
    c = cfg(tmp_path)
    repo = make_repo(tmp_path, "_control")
    transcript = tmp_path / "t.jsonl"
    write_transcript(transcript, [human("고쳐줘")])
    data = {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "main.py")},
            "transcript_path": str(transcript)}
    assert pg.decide(data, c) is None


def assistant_image(text):
    return {"type": "assistant", "message": {"content": [
        {"type": "image", "source": {"type": "base64", "data": "x"}}, {"type": "text", "text": text},
    ]}}


def human_with_image():
    return {"type": "user", "origin": {"kind": "human"}, "message": {"content": [
        {"type": "image", "source": {"type": "base64", "data": "x"}}, {"type": "text", "text": "이거 봐줘"},
    ]}}


def test_image_bearing_human_message_counts_as_new_turn(tmp_path):
    # An earlier turn's pre-change report must NOT be reused for a later turn that starts with
    # an image-bearing human message - that message is a real new-turn boundary too.
    c = cfg(tmp_path)
    repo = make_repo(tmp_path, "demo")
    transcript = tmp_path / "t.jsonl"
    report = "읽은 자료: x\n예상 위험: y\n바꿀 파일: z\n되돌리는 법: w"
    write_transcript(transcript, [human("고쳐줘"), assistant_text(report), human_with_image()])
    data = {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "main.py")},
            "transcript_path": str(transcript)}
    out = pg.decide(data, c)
    assert out is not None
    assert "systemMessage" in out


def test_peer_message_does_not_reset_turn_boundary(tmp_path):
    # A peer/subagent relay message (also type=="user", but origin.kind == "peer") arriving
    # after a valid pre-change report must NOT be treated as a new human turn - the report
    # still covers the edit that follows it.
    c = cfg(tmp_path)
    repo = make_repo(tmp_path, "demo")
    transcript = tmp_path / "t.jsonl"
    report = "읽은 자료: x\n예상 위험: y\n바꿀 파일: z\n되돌리는 법: w"
    write_transcript(transcript, [human("고쳐줘"), assistant_text(report), peer("검토 종료")])
    data = {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "main.py")},
            "transcript_path": str(transcript)}
    assert pg.decide(data, c) is None


def test_bash_write_target_not_masked_by_doc_path_in_same_command(tmp_path):
    # A command that both writes to a real code file AND merely mentions a doc path in the
    # same line must still be flagged for the code write - the extraction only collects
    # genuine write targets now (scope_guard.py's shared tokenizer), not every absolute path
    # mentioned anywhere in the command text.
    c = cfg(tmp_path)
    repo = make_repo(tmp_path, "demo")
    code_file = repo / "main.py"
    transcript = tmp_path / "t.jsonl"
    write_transcript(transcript, [human("고쳐줘")])
    data = {"tool_name": "Bash",
            "tool_input": {"command": f"cat {repo / 'README.md'} && echo x >> {code_file}"},
            "transcript_path": str(transcript)}
    out = pg.decide(data, c)
    assert out is not None


def test_bash_cp_destination_not_masked_by_trailing_redirect(tmp_path):
    # `cp new.py main.py > docs/copy.log` writes to BOTH main.py (cp's own destination
    # argument) and docs/copy.log (the redirect target) - the redirect must not swallow cp's
    # own destination argument out of the write-target extraction.
    c = cfg(tmp_path)
    repo = make_repo(tmp_path, "demo")
    transcript = tmp_path / "t.jsonl"
    write_transcript(transcript, [human("고쳐줘")])
    data = {"tool_name": "Bash",
            "tool_input": {"command": f"cp {repo / 'new.py'} {repo / 'main.py'} > {repo / 'docs' / 'copy.log'}"},
            "transcript_path": str(transcript)}
    out = pg.decide(data, c)
    assert out is not None


def test_bash_touch_of_code_file_is_not_masked_by_doc_write_in_same_command(tmp_path):
    # `touch main.py && echo note > docs/note.md` writes a real code file (touch) and a doc
    # (redirect, exempt) in the same command - the exempt doc write must not mask the code
    # write, and `touch` itself must be recognized as a write command.
    c = cfg(tmp_path)
    repo = make_repo(tmp_path, "demo")
    transcript = tmp_path / "t.jsonl"
    write_transcript(transcript, [human("고쳐줘")])
    data = {"tool_name": "Bash",
            "tool_input": {"command": f"touch {repo / 'main.py'} && echo note > {repo / 'docs' / 'note.md'}"},
            "transcript_path": str(transcript)}
    out = pg.decide(data, c)
    assert out is not None


def test_non_git_folder_is_skipped(tmp_path):
    c = cfg(tmp_path)
    root = tmp_path / "apps" / "no-git"
    root.mkdir(parents=True)
    transcript = tmp_path / "t.jsonl"
    write_transcript(transcript, [human("고쳐줘")])
    data = {"tool_name": "Edit", "tool_input": {"file_path": str(root / "main.py")},
            "transcript_path": str(transcript)}
    assert pg.decide(data, c) is None
