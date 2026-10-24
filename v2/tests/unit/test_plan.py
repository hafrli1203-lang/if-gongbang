"""Unit tests for engine.plan: plan.json schema, validate_plan's checks
(owns-subset, unknown scene scripts, dependency problems), and
ordered_tasks' topological sort/cycle detection."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.plan import PlanError, Task, load_plan, ordered_tasks, plan_path, validate_plan


def _write_plan(repo: Path, job: str, data: dict[str, object]) -> None:
    path = plan_path(repo, job)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _valid_plan_dict(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "goal": "작은 수정", "ui": False,
        "scenes": [{"id": "s1", "actor_does": "클릭", "app_shows": "결과",
                    "where": "home", "cmd": ["true"]}],
        "tasks": [{"id": "t1", "title": "고치기", "detail": "...",
                   "owns": ["src/"], "depends_on": []}],
        "acceptance": ["요구사항대로 동작한다"],
    }
    base.update(overrides)
    return base


def test_plan_path_is_under_gongbang_job(tmp_path: Path) -> None:
    assert plan_path(tmp_path, "myjob") == tmp_path / ".gongbang" / "myjob" / "plan.json"


def test_load_plan_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PlanError) as exc:
        load_plan(tmp_path, "nope")
    assert exc.value.reason == "plan_missing"


def test_load_plan_unreadable_json(tmp_path: Path) -> None:
    path = plan_path(tmp_path, "job")
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(PlanError) as exc:
        load_plan(tmp_path, "job")
    assert exc.value.reason == "plan_unreadable"


@pytest.mark.parametrize("key,bad_value,reason_prefix", [
    ("goal", "", "plan.goal_must_be_nonempty_string"),
    ("ui", "yes", "plan.ui_must_be_bool"),
    ("scenes", "not-a-list", "plan.scenes_must_be_list"),
    ("tasks", [], "plan.tasks_must_be_nonempty_list"),
    ("acceptance", [], "plan.acceptance_must_not_be_empty"),
])
def test_load_plan_schema_errors(tmp_path: Path, key: str, bad_value: object,
                                  reason_prefix: str) -> None:
    data = _valid_plan_dict()
    data[key] = bad_value
    _write_plan(tmp_path, "job", data)
    with pytest.raises(PlanError) as exc:
        load_plan(tmp_path, "job")
    assert exc.value.reason == reason_prefix


def test_load_plan_duplicate_task_ids(tmp_path: Path) -> None:
    data = _valid_plan_dict(tasks=[
        {"id": "t1", "title": "a", "detail": "", "owns": ["src/"], "depends_on": []},
        {"id": "t1", "title": "b", "detail": "", "owns": ["lib/"], "depends_on": []},
    ])
    _write_plan(tmp_path, "job", data)
    with pytest.raises(PlanError) as exc:
        load_plan(tmp_path, "job")
    assert exc.value.reason == "plan.tasks_ids_not_unique"


def test_load_plan_task_missing_owns(tmp_path: Path) -> None:
    data = _valid_plan_dict(tasks=[
        {"id": "t1", "title": "a", "detail": "", "owns": [], "depends_on": []},
    ])
    _write_plan(tmp_path, "job", data)
    with pytest.raises(PlanError) as exc:
        load_plan(tmp_path, "job")
    assert exc.value.reason == "task[t1].owns_must_not_be_empty"


def test_load_plan_valid_roundtrip(tmp_path: Path) -> None:
    _write_plan(tmp_path, "job", _valid_plan_dict())
    plan = load_plan(tmp_path, "job")
    assert plan.goal == "작은 수정"
    assert plan.ui is False
    assert plan.scenes[0].id == "s1"
    assert plan.tasks[0].owns == ("src/",)


# ---- validate_plan -------------------------------------------------

def test_validate_plan_owns_within_build_prefixes_ok(tmp_path: Path) -> None:
    _write_plan(tmp_path, "job", _valid_plan_dict())
    plan = load_plan(tmp_path, "job")
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/", "lib/"))
    assert problems == []


def test_validate_plan_owns_outside_build_prefixes_is_a_problem(tmp_path: Path) -> None:
    data = _valid_plan_dict(tasks=[
        {"id": "t1", "title": "a", "detail": "", "owns": ["other/"], "depends_on": []},
    ])
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/", "lib/"))
    assert len(problems) == 1
    assert "other/" in problems[0]
    assert "t1" in problems[0]


def test_validate_plan_missing_scene_script_is_a_problem(tmp_path: Path) -> None:
    data = _valid_plan_dict(scenes=[
        {"id": "s1", "actor_does": "클릭", "app_shows": "결과", "where": "home",
         "cmd": ["python3", "tests/scenes/s1.py"]},
    ])
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/",))
    assert any("tests/scenes/s1.py" in p for p in problems)


def test_validate_plan_existing_scene_script_is_fine(tmp_path: Path) -> None:
    scene_script = tmp_path / "tests" / "scenes" / "s1.py"
    scene_script.parent.mkdir(parents=True)
    scene_script.write_text("print('ok')\n", encoding="utf-8")
    data = _valid_plan_dict(scenes=[
        {"id": "s1", "actor_does": "클릭", "app_shows": "결과", "where": "home",
         "cmd": ["python3", "tests/scenes/s1.py"]},
    ])
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/",))
    assert problems == []


def test_validate_plan_unknown_dep_is_a_problem(tmp_path: Path) -> None:
    data = _valid_plan_dict(tasks=[
        {"id": "t1", "title": "a", "detail": "", "owns": ["src/"], "depends_on": ["ghost"]},
    ])
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/",))
    assert any("unknown_dep" in p for p in problems)


def test_validate_plan_dependency_cycle_is_a_problem(tmp_path: Path) -> None:
    data = _valid_plan_dict(tasks=[
        {"id": "t1", "title": "a", "detail": "", "owns": ["src/"], "depends_on": ["t2"]},
        {"id": "t2", "title": "b", "detail": "", "owns": ["src/"], "depends_on": ["t1"]},
    ])
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/",))
    assert any("dependency_cycle" in p for p in problems)


def test_validate_plan_exact_file_prefix_matches_only_exactly(tmp_path: Path) -> None:
    data = _valid_plan_dict(tasks=[
        {"id": "t1", "title": "a", "detail": "", "owns": ["package.json"], "depends_on": []},
    ])
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    assert validate_plan(plan, tmp_path, build_prefixes=("package.json",)) == []
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/",))
    assert len(problems) == 1


# ---- ordered_tasks ---------------------------------------------------

def test_ordered_tasks_respects_dependencies(tmp_path: Path) -> None:
    data = _valid_plan_dict(tasks=[
        {"id": "t2", "title": "b", "detail": "", "owns": ["lib/"], "depends_on": ["t1"]},
        {"id": "t1", "title": "a", "detail": "", "owns": ["src/"], "depends_on": []},
        {"id": "t3", "title": "c", "detail": "", "owns": ["docs/"], "depends_on": ["t2", "t1"]},
    ])
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    ordered = ordered_tasks(plan)
    assert [t.id for t in ordered] == ["t1", "t2", "t3"]


def test_ordered_tasks_stable_for_independent_tasks(tmp_path: Path) -> None:
    data = _valid_plan_dict(tasks=[
        {"id": "a", "title": "a", "detail": "", "owns": ["src/"], "depends_on": []},
        {"id": "b", "title": "b", "detail": "", "owns": ["lib/"], "depends_on": []},
    ])
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    assert [t.id for t in ordered_tasks(plan)] == ["a", "b"]


def test_ordered_tasks_cycle_raises(tmp_path: Path) -> None:
    data = _valid_plan_dict(tasks=[
        {"id": "t1", "title": "a", "detail": "", "owns": ["src/"], "depends_on": ["t2"]},
        {"id": "t2", "title": "b", "detail": "", "owns": ["lib/"], "depends_on": ["t1"]},
    ])
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    with pytest.raises(PlanError) as exc:
        ordered_tasks(plan)
    assert exc.value.reason.startswith("dependency_cycle")


# ---- requirements/covers ----------------------

def test_requirement_covered_by_scene_is_valid(tmp_path: Path) -> None:
    data = _valid_plan_dict(
        requirements=[{"id": "R1", "text": "클릭하면 결과가 나온다", "kind": "function"}],
        scenes=[{"id": "s1", "actor_does": "클릭", "app_shows": "결과", "where": "home",
                 "cmd": ["true"], "covers": ["R1"]}],
    )
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    assert plan.requirements[0].id == "R1"
    assert plan.scenes[0].covers == ("R1",)
    assert validate_plan(plan, tmp_path, build_prefixes=("src/",)) == []


def test_requirement_not_covered_by_any_scene_or_task_is_a_problem(tmp_path: Path) -> None:
    data = _valid_plan_dict(
        requirements=[{"id": "R1", "text": "보안 점검", "kind": "security"}],
    )
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/",))
    assert any("R1" in p and "not covered" in p for p in problems)


def test_requirement_covered_by_task_only_is_not_enough(tmp_path: Path) -> None:
    """A task's own `covers` is tracking only -- "done" means its own
    review/bench passed, never that any scene actually exercised the
    requirement's real behavior (every
    requirement kind alike, not just "function"). Without at least one
    SCENE coverer, the requirement would pass this static check yet
    never be able to pass engine.full_orbit's own runtime completion
    gate (_uncovered_requirement_ids) -- so it must already fail here."""
    data = _valid_plan_dict(
        requirements=[{"id": "R1", "text": "입력 검증", "kind": "security"}],
        tasks=[{"id": "t1", "title": "고치기", "detail": "...", "owns": ["src/"],
                "depends_on": [], "covers": ["R1"]}],
    )
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/",))
    assert any("R1" in p and "not covered by any scene" in p for p in problems)


def test_requirement_covered_by_task_and_scene_is_valid(tmp_path: Path) -> None:
    """A task MAY still also claim `covers` (kept for tracking), as
    long as some scene covers the same requirement too."""
    data = _valid_plan_dict(
        requirements=[{"id": "R1", "text": "입력 검증", "kind": "security"}],
        scenes=[{"id": "s1", "actor_does": "입력", "app_shows": "검증 결과", "where": "home",
                 "cmd": ["true"], "covers": ["R1"]}],
        tasks=[{"id": "t1", "title": "고치기", "detail": "...", "owns": ["src/"],
                "depends_on": [], "covers": ["R1"]}],
    )
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    assert validate_plan(plan, tmp_path, build_prefixes=("src/",)) == []


def test_scene_covers_unknown_requirement_is_a_problem(tmp_path: Path) -> None:
    data = _valid_plan_dict(
        scenes=[{"id": "s1", "actor_does": "클릭", "app_shows": "결과", "where": "home",
                 "cmd": ["true"], "covers": ["GHOST"]}],
    )
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/",))
    assert any("GHOST" in p for p in problems)


def test_requirement_duplicate_ids_raise(tmp_path: Path) -> None:
    data = _valid_plan_dict(requirements=[
        {"id": "R1", "text": "a", "kind": "function"},
        {"id": "R1", "text": "b", "kind": "function"},
    ])
    _write_plan(tmp_path, "job", data)
    with pytest.raises(PlanError) as exc:
        load_plan(tmp_path, "job")
    assert exc.value.reason == "plan.requirements_ids_not_unique"


def test_requirement_coverers_lists_scene_and_task_pairs(tmp_path: Path) -> None:
    from engine.plan import requirement_coverers

    data = _valid_plan_dict(
        requirements=[{"id": "R1", "text": "a", "kind": "function"}],
        scenes=[{"id": "s1", "actor_does": "클릭", "app_shows": "결과", "where": "home",
                 "cmd": ["true"], "covers": ["R1"]}],
        tasks=[{"id": "t1", "title": "고치기", "detail": "...", "owns": ["src/"],
                "depends_on": [], "covers": ["R1"]}],
    )
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    coverers = requirement_coverers(plan)
    assert coverers["R1"] == (("scene", "s1"), ("task", "t1"))


def test_ordered_tasks_unknown_dep_raises(tmp_path: Path) -> None:
    data = _valid_plan_dict(tasks=[
        {"id": "t1", "title": "a", "detail": "", "owns": ["src/"], "depends_on": ["ghost"]},
    ])
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    with pytest.raises(PlanError) as exc:
        ordered_tasks(plan)
    assert exc.value.reason.startswith("unknown_dep")


def test_skill_index_reads_library_line_shape_and_pack_listings(tmp_path) -> None:
    from types import SimpleNamespace

    from engine.plan import Plan, Tool, classify_skill_tools

    lib = tmp_path / "lib"
    (lib / "_index").mkdir(parents=True)
    (lib / "INDEX.md").write_text(
        "# index\n\n- **search-first** — research before coding  \n"
        "- **obra-superpowers** (참고 저장소, 하위 스킬 15개) — pack\n", encoding="utf-8")
    (lib / "_index" / "obra-superpowers.md").write_text(
        "- **systematic-debugging** — find root cause first\n", encoding="utf-8")
    core = tmp_path / "core" / "크래시-아웃"
    core.mkdir(parents=True)
    (core / "SKILL.md").write_text("---\nname: 크래시-아웃\n---\n", encoding="utf-8")
    config = SimpleNamespace(skill_index_path=lib / "INDEX.md", skill_roots=(tmp_path / "core",))
    names = ["search-first", "systematic-debugging", "크래시-아웃", "not-there"]
    plan = SimpleNamespace(tools=tuple(
        SimpleNamespace(kind="skill", name=n, stations=("build",)) for n in names))
    usable, needs_install = classify_skill_tools(plan, config)
    assert usable == {"search-first", "systematic-debugging", "크래시-아웃"}
    assert needs_install == {"not-there"}


def test_symlinked_core_skill_escaping_root_is_not_available(tmp_path) -> None:
    """A SKILL.md reachable under a configured skill root only via a
    symlink that points outside that root must not be treated as an
    installed skill -- same containment rule as engine.skills.
    load_skill, applied here because _available_skill_names/
    skill_catalog_text glob the root directly rather than going
    through load_skill."""
    from types import SimpleNamespace

    from engine.plan import classify_skill_tools, skill_catalog_text

    core = tmp_path / "core"
    core.mkdir()
    outside = tmp_path / "outside" / "숨은-스킬"
    outside.mkdir(parents=True)
    (outside / "SKILL.md").write_text("---\ndescription: 몰래\n---\n", encoding="utf-8")
    (core / "숨은-스킬").symlink_to(outside)
    config = SimpleNamespace(skill_index_path=None, skill_roots=(core,))

    plan = SimpleNamespace(tools=(
        SimpleNamespace(kind="skill", name="숨은-스킬", stations=("build",)),
    ))
    _usable, needs_install = classify_skill_tools(plan, config)
    assert needs_install == {"숨은-스킬"}

    catalog = skill_catalog_text(config)
    assert "숨은-스킬" not in catalog
