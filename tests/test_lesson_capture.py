import time

import lesson_capture as lc


def cfg(tmp_path):
    return {"apps_root": str(tmp_path / "apps")}


def test_correction_marker_is_detected(tmp_path):
    c = cfg(tmp_path)
    data = {"prompt": "아니 그거 하지 마, 왜 또 그러는 거야", "cwd": str(tmp_path / "apps" / "demo")}
    msg = lc.detect(data, c)
    assert msg is not None
    assert "demo" in msg


def test_plain_prompt_is_ignored(tmp_path):
    c = cfg(tmp_path)
    data = {"prompt": "다음 기능을 추가해줘", "cwd": str(tmp_path / "apps" / "demo")}
    # "다시" style markers should not fire on unrelated text without a marker word
    msg = lc.detect(data, c)
    assert msg is None


def test_relayed_message_is_ignored(tmp_path):
    c = cfg(tmp_path)
    data = {"prompt": "<agent-message>아니 그거 하지 마</agent-message>", "cwd": str(tmp_path / "apps" / "demo")}
    assert lc.detect(data, c) is None


def test_cwd_outside_apps_root_falls_back_to_workspace(tmp_path):
    c = cfg(tmp_path)
    data = {"prompt": "아니 하지 마", "cwd": "/somewhere/else"}
    msg = lc.detect(data, c)
    assert "workspace" in msg


def test_derive_app_for_apps_root_itself(tmp_path):
    c = cfg(tmp_path)
    assert lc.derive_app(c["apps_root"], c) == "workspace"


# --- adversarial-length input must not stall UserPromptSubmit -----------------------------

def test_very_long_adversarial_prompt_is_fast(tmp_path):
    c = cfg(tmp_path)
    adversarial = "아니" * 16000 + "면"  # 32,001 chars
    data = {"prompt": adversarial, "cwd": str(tmp_path / "apps" / "demo")}
    start = time.monotonic()
    lc.detect(data, c)
    elapsed = time.monotonic() - start
    assert elapsed < 0.3, f"took {elapsed:.3f}s"


def test_very_long_prompt_is_truncated_before_scanning(tmp_path):
    c = cfg(tmp_path)
    # the marker only appears far past MAX_PROMPT_CHARS - must NOT be detected, since only the
    # first MAX_PROMPT_CHARS characters are ever scanned
    prompt = ("x" * (lc.MAX_PROMPT_CHARS + 500)) + " 아니 하지 마"
    data = {"prompt": prompt, "cwd": str(tmp_path / "apps" / "demo")}
    assert lc.detect(data, c) is None


def test_marker_within_cap_is_still_detected(tmp_path):
    c = cfg(tmp_path)
    data = {"prompt": "아니 하지 마 " + ("x" * 100), "cwd": str(tmp_path / "apps" / "demo")}
    assert lc.detect(data, c) is not None
