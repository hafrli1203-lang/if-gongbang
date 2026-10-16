import lessons_review as lr
import lessons_store


def test_load_all_reads_every_app_file(tmp_path):
    lessons_dir = tmp_path / "lessons"
    cfg = {"lessons_dir": str(lessons_dir)}
    lessons_store.add_lesson("app-a", cfg, scene="s", why="w", rule="r", owner_quote="q")
    lessons_store.add_lesson("app-b", cfg, scene="s", why="w", rule="r", owner_quote="q")
    rows = lr.load_all(str(lessons_dir))
    assert len(rows) == 2
    apps = {r["app"] for r in rows}
    assert apps == {"app-a", "app-b"}


def test_load_all_empty_dir(tmp_path):
    assert lr.load_all(str(tmp_path / "missing")) == []
