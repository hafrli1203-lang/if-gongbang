import os
import time

import library_nudge as ln


def test_keywords_are_derived_from_folder_names(tmp_path):
    lib = tmp_path / "library"
    (lib / "매장 운영 매뉴얼").mkdir(parents=True)
    (lib / "광고 집행 사례").mkdir(parents=True)
    keywords = ln.derive_keywords(str(lib))
    assert "매장" in keywords or "운영" in keywords or "매뉴얼" in keywords
    assert "기준" in keywords  # fixed generic word, always present


def test_nudge_fires_on_keyword(tmp_path):
    lib = tmp_path / "library"
    (lib / "광고 집행 사례").mkdir(parents=True)
    cfg = {"library_root": str(lib), "library_tool_name": "library_search"}
    data = {"prompt": "지난 사례 좀 찾아줘"}
    msg = ln.detect(data, cfg)
    assert msg is not None
    assert "library_search" in msg


def test_no_keyword_match_is_silent(tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    cfg = {"library_root": str(lib)}
    data = {"prompt": "안녕 오늘 날씨 좋다"}
    assert ln.detect(data, cfg) is None


def test_missing_library_root_fails_open(tmp_path):
    cfg = {"library_root": str(tmp_path / "does-not-exist")}
    # fixed generic words still work even if the folder cannot be listed
    data = {"prompt": "이거 기준이 뭐야"}
    assert ln.detect(data, cfg) is not None


# --- adversarial-length input must not stall UserPromptSubmit -----------------------------

def test_very_long_adversarial_prompt_is_fast(tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    cfg = {"library_root": str(lib)}
    adversarial = "x" * 50000 + "<system-reminder>" + "y" * 50000  # unterminated wrapper tag
    data = {"prompt": adversarial}
    start = time.monotonic()
    ln.detect(data, cfg)
    elapsed = time.monotonic() - start
    assert elapsed < 0.3, f"took {elapsed:.3f}s"


def test_large_library_root_does_not_blow_up_keyword_derivation(tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    for i in range(3000):
        (lib / f"folder-{i}").mkdir()
    start = time.monotonic()
    keywords = ln.derive_keywords(str(lib))
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, f"took {elapsed:.3f}s"
    assert "기준" in keywords
