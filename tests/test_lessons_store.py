import os
import threading

import lessons_store


def cfg(tmp_path):
    return {"lessons_dir": str(tmp_path / "lessons")}


def test_add_and_list_lesson(tmp_path):
    c = cfg(tmp_path)
    rec = lessons_store.add_lesson("demo", c, scene="s", why="w", rule="r", owner_quote="q", session="sess-1")
    assert rec["number"] == "L1-0001"
    rows = lessons_store.list_lessons("demo", c)
    assert len(rows) == 1
    assert rows[0]["status"] == "candidate"


def test_mark_seen_adds_distinct_sessions(tmp_path):
    c = cfg(tmp_path)
    lessons_store.add_lesson("demo", c, scene="s", why="w", rule="r", owner_quote="q", session="sess-1")
    lessons_store.mark_seen("demo", c, "L1-0001", "sess-2")
    lessons_store.mark_seen("demo", c, "L1-0001", "sess-2")  # duplicate, should not double count
    rows = lessons_store.list_lessons("demo", c)
    assert set(rows[0]["sessions_seen"]) == {"sess-1", "sess-2"}


def test_promotion_candidates_requires_min_sessions(tmp_path):
    c = cfg(tmp_path)
    lessons_store.add_lesson("demo", c, scene="s", why="w", rule="r", owner_quote="q", session="sess-1")
    lessons_store.mark_seen("demo", c, "L1-0001", "sess-2")
    assert lessons_store.promotion_candidates("demo", c, min_sessions=3) == []
    lessons_store.mark_seen("demo", c, "L1-0001", "sess-3")
    cands = lessons_store.promotion_candidates("demo", c, min_sessions=3)
    assert len(cands) == 1


def test_set_status_promotes(tmp_path):
    c = cfg(tmp_path)
    lessons_store.add_lesson("demo", c, scene="s", why="w", rule="r", owner_quote="q")
    lessons_store.set_status("demo", c, "L1-0001", "promoted")
    rows = lessons_store.list_lessons("demo", c)
    assert rows[0]["status"] == "promoted"


def test_apps_are_isolated(tmp_path):
    c = cfg(tmp_path)
    lessons_store.add_lesson("app-a", c, scene="s", why="w", rule="r", owner_quote="q")
    lessons_store.add_lesson("app-b", c, scene="s", why="w", rule="r", owner_quote="q")
    assert len(lessons_store.list_lessons("app-a", c)) == 1
    assert len(lessons_store.list_lessons("app-b", c)) == 1


# --- project-name collisions, concurrent writes, atomic failure ---------------------------

def test_project_names_that_collapse_to_the_same_token_do_not_collide(tmp_path):
    c = cfg(tmp_path)
    assert lessons_store._path("demo app", c) != lessons_store._path("demo_app", c)
    lessons_store.add_lesson("demo app", c, scene="s", why="w", rule="r", owner_quote="q")
    lessons_store.add_lesson("demo_app", c, scene="s2", why="w2", rule="r2", owner_quote="q2")
    assert len(lessons_store.list_lessons("demo app", c)) == 1
    assert len(lessons_store.list_lessons("demo_app", c)) == 1
    assert lessons_store.list_lessons("demo app", c)[0]["scene"] == "s"


def test_concurrent_add_lesson_assigns_distinct_numbers(tmp_path):
    c = cfg(tmp_path)
    errors = []

    def worker():
        try:
            lessons_store.add_lesson("demo", c, scene="s", why="w", rule="r", owner_quote="q")
        except Exception as e:  # pragma: no cover - surfaced via `errors` below
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    rows = lessons_store.list_lessons("demo", c)
    assert len(rows) == 8
    assert len({r["number"] for r in rows}) == 8  # every number is distinct, none lost/duplicated


def test_write_failure_preserves_original_file(tmp_path, monkeypatch):
    c = cfg(tmp_path)
    lessons_store.add_lesson("demo", c, scene="original", why="w", rule="r", owner_quote="q")
    path = lessons_store._path("demo", c)
    original_content = open(path, encoding="utf-8").read()

    real_replace = os.replace

    def failing_replace(src, dst):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(os, "replace", failing_replace)
    try:
        import pytest
        with pytest.raises(OSError):
            lessons_store.add_lesson("demo", c, scene="second", why="w", rule="r", owner_quote="q")
    finally:
        monkeypatch.setattr(os, "replace", real_replace)

    assert open(path, encoding="utf-8").read() == original_content
    assert len(lessons_store.list_lessons("demo", c)) == 1
