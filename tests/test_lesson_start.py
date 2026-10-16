import lesson_start as ls
import lessons_store


def cfg(tmp_path):
    return {"apps_root": str(tmp_path / "apps"), "lessons_dir": str(tmp_path / "lessons")}


def test_no_lessons_prints_nothing(tmp_path):
    c = cfg(tmp_path)
    assert ls.build_output([]) == ""


def test_lessons_are_formatted(tmp_path):
    c = cfg(tmp_path)
    lessons_store.add_lesson("demo", c, scene="장면", why="이유", rule="기준", owner_quote="원문")
    rows = lessons_store.list_lessons("demo", c)
    out = ls.build_output(rows)
    assert "장면" in out and "이유" in out and "기준" in out
    assert "총 1건" in out


def test_retired_lessons_are_hidden(tmp_path):
    c = cfg(tmp_path)
    lessons_store.add_lesson("demo", c, scene="s", why="w", rule="r", owner_quote="q")
    lessons_store.set_status("demo", c, "L1-0001", "retired")
    rows = lessons_store.list_lessons("demo", c)
    assert ls.build_output(rows) == ""


def test_derive_app_outside_root_returns_none(tmp_path):
    c = cfg(tmp_path)
    assert ls.derive_app("/not/under/apps/root", c) is None


# --- a rejected lesson must not be re-injected as live guidance ---------------------------

def test_rejected_lesson_is_hidden(tmp_path):
    c = cfg(tmp_path)
    lessons_store.add_lesson("demo", c, scene="s", why="w", rule="do the rejected action", owner_quote="q")
    lessons_store.set_status("demo", c, "L1-0001", "rejected")
    rows = lessons_store.list_lessons("demo", c)
    out = ls.build_output(rows)
    assert out == ""
    assert "rejected action" not in out


def test_candidate_lesson_is_still_shown(tmp_path):
    c = cfg(tmp_path)
    lessons_store.add_lesson("demo", c, scene="s", why="w", rule="r", owner_quote="q")
    rows = lessons_store.list_lessons("demo", c)
    assert "경험" in ls.build_output(rows)
