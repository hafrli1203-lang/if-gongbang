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
    # _valid_plan_dict's own default scene claims R1 via
    # tests/scenes/base.py -- create it unconditionally (harmless for
    # every test that overrides "scenes" to something else, since none
    # of them reference this filename) so validate_plan's own
    # scene-script-exists check never fails for the default fixture.
    scene_script = repo / "tests" / "scenes" / "base.py"
    scene_script.parent.mkdir(parents=True, exist_ok=True)
    if not scene_script.exists():
        scene_script.write_text("pass\n", encoding="utf-8")


def _valid_plan_dict(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "goal": "작은 수정", "ui": False,
        "scenes": [{"id": "s1", "actor_does": "클릭", "app_shows": "결과",
                    "where": "home", "cmd": ["python3", "tests/scenes/base.py"],
                    "covers": ["R1"]}],
        "tasks": [{"id": "t1", "title": "고치기", "detail": "...",
                   "owns": ["src/", "tests/"], "depends_on": []}],
        "acceptance": ["요구사항대로 동작한다"],
        "requirements": [{"id": "R1", "text": "요구사항대로 동작한다", "kind": "function"}],
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
    assert plan.tasks[0].owns == ("src/", "tests/")


# ---- validate_plan -------------------------------------------------

def test_validate_plan_owns_within_build_prefixes_ok(tmp_path: Path) -> None:
    _write_plan(tmp_path, "job", _valid_plan_dict())
    plan = load_plan(tmp_path, "job")
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/", "lib/", "tests/"))
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
         "cmd": ["python3", "tests/scenes/s1.py"], "covers": ["R1"]},
    ])
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/", "tests/"))
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

def _write_scene_script(repo: Path, rel: str = "tests/scenes/s1.py") -> None:
    script = repo / rel
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("pass\n", encoding="utf-8")


def test_requirement_covered_by_scene_is_valid(tmp_path: Path) -> None:
    _write_scene_script(tmp_path)
    data = _valid_plan_dict(
        requirements=[{"id": "R1", "text": "클릭하면 결과가 나온다", "kind": "function"}],
        scenes=[{"id": "s1", "actor_does": "클릭", "app_shows": "결과", "where": "home",
                 "cmd": ["python3", "tests/scenes/s1.py"], "covers": ["R1"]}],
    )
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    assert plan.requirements[0].id == "R1"
    assert plan.scenes[0].covers == ("R1",)
    assert validate_plan(plan, tmp_path, build_prefixes=("src/", "tests/")) == []


def test_requirement_not_covered_by_any_scene_or_task_is_a_problem(tmp_path: Path) -> None:
    data = _valid_plan_dict(
        requirements=[{"id": "R1", "text": "보안 점검", "kind": "security"}],
        # A scene that claims no `covers` at all -- the default fixture's
        # own R1-covering scene must not leak into this test, or R1
        # would already be covered and the problem this test checks
        # for would never appear.
        scenes=[{"id": "s1", "actor_does": "클릭", "app_shows": "결과", "where": "home",
                 "cmd": ["true"]}],
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
        scenes=[{"id": "s1", "actor_does": "클릭", "app_shows": "결과", "where": "home",
                 "cmd": ["true"]}],
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
    _write_scene_script(tmp_path)
    data = _valid_plan_dict(
        requirements=[{"id": "R1", "text": "입력 검증", "kind": "security"}],
        scenes=[{"id": "s1", "actor_does": "입력", "app_shows": "검증 결과", "where": "home",
                 "cmd": ["python3", "tests/scenes/s1.py"], "covers": ["R1"]}],
        tasks=[{"id": "t1", "title": "고치기", "detail": "...", "owns": ["src/", "tests/"],
                "depends_on": [], "covers": ["R1"]}],
    )
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    assert validate_plan(plan, tmp_path, build_prefixes=("src/", "tests/")) == []


def test_scene_covers_unknown_requirement_is_a_problem(tmp_path: Path) -> None:
    data = _valid_plan_dict(
        scenes=[{"id": "s1", "actor_does": "클릭", "app_shows": "결과", "where": "home",
                 "cmd": ["true"], "covers": ["GHOST"]}],
    )
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/",))
    assert any("GHOST" in p for p in problems)


def test_validate_plan_empty_requirements_is_a_problem(tmp_path: Path) -> None:
    data = _valid_plan_dict(requirements=[])
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/",))
    assert "plan_requirements_missing" in problems


def test_validate_plan_scene_claiming_coverage_without_a_scene_file_is_a_problem(
    tmp_path: Path,
) -> None:
    data = _valid_plan_dict(
        scenes=[{"id": "s1", "actor_does": "클릭", "app_shows": "결과", "where": "home",
                 "cmd": ["true"], "covers": ["R1"]}],
    )
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/",))
    assert any("scene_cmd_not_a_scene_file" in p for p in problems)


def test_validate_plan_scene_claiming_coverage_with_a_scene_file_is_fine(
    tmp_path: Path,
) -> None:
    _write_scene_script(tmp_path)
    data = _valid_plan_dict(
        scenes=[{"id": "s1", "actor_does": "클릭", "app_shows": "결과", "where": "home",
                 "cmd": ["python3", "tests/scenes/s1.py"], "covers": ["R1"]}],
    )
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    assert validate_plan(plan, tmp_path, build_prefixes=("src/", "tests/")) == []


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


# ---------------------------------------------------------------------------
# validate_tools / skill_catalog_text: an mcp tool for a station whose
# configured runner cannot take mcp (codex, or anything other than
# "claude" -- engine.workers.claude_cli is the only runner that writes
# --mcp-config at all; engine.steps.review's own "reviewer_mcp_unsupported"
# is the runtime symptom a codex-reviewed project used to only discover
# mid-job). Regression: tools/templates/node-vitest.json's own
# station_mcp_allow used to offer "review" playwright access to exactly
# such a project (reviewer runner "codex").
# ---------------------------------------------------------------------------

def _mcp_config(**overrides: object) -> "SimpleNamespace":
    from types import SimpleNamespace

    base = dict(
        mcp_servers={"playwright": object()},
        station_owners={"review": {"reviewer": ("review.md",)},
                        "build": {"builder": ("src/",)}},
        workers={"reviewer": SimpleNamespace(runner="codex"),
                "builder": SimpleNamespace(runner="claude")},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_validate_tools_rejects_mcp_for_a_station_whose_runner_is_codex() -> None:
    from types import SimpleNamespace

    from engine.plan import validate_tools

    config = _mcp_config()
    plan = SimpleNamespace(tools=(
        SimpleNamespace(kind="mcp", name="playwright", stations=("review",)),
    ))
    problems = validate_tools(plan, config)
    assert len(problems) == 1
    assert "playwright" in problems[0] and "review" in problems[0]


def test_validate_tools_accepts_mcp_for_a_station_whose_runner_is_claude() -> None:
    from types import SimpleNamespace

    from engine.plan import validate_tools

    config = _mcp_config()
    plan = SimpleNamespace(tools=(
        SimpleNamespace(kind="mcp", name="playwright", stations=("build",)),
    ))
    assert validate_tools(plan, config) == []


def test_validate_tools_accepts_mcp_for_an_unowned_station(
) -> None:
    """A station config.station_owners has no entry for at all (nothing
    to check a runner against) is treated as capable, same as before
    this check existed -- never a new, surprising hard failure for a
    config this module cannot see a runner for."""
    from types import SimpleNamespace

    from engine.plan import validate_tools

    config = _mcp_config(station_owners={})
    plan = SimpleNamespace(tools=(
        SimpleNamespace(kind="mcp", name="playwright", stations=("review",)),
    ))
    assert validate_tools(plan, config) == []


def test_skill_catalog_text_never_lists_a_station_a_codex_runner_cannot_mcp() -> None:
    """skill_catalog_text's own "쓸 수 있는 칸" (usable stations) row
    must not offer "review" even if config.station_mcp_allow itself
    (an operator-authored config, not plan.tools) still names it for a
    codex-reviewed project -- the planner must never be shown a station
    it cannot actually use mcp at."""
    from types import SimpleNamespace

    from engine.plan import skill_catalog_text

    config = _mcp_config(
        skill_index_path=None, skill_roots=(),
        station_mcp_allow={"review": ("playwright",), "build": ("playwright",)},
    )
    catalog = skill_catalog_text(config)
    assert "build" in catalog
    assert "review" not in catalog.split("### MCP 서버")[-1]


# ---------------------------------------------------------------------------
# plan_digest / scene_file_digests / record_plan_reviewed
# ---------------------------------------------------------------------------

def test_scene_file_digests_covers_every_distinct_scenes_file(tmp_path: Path) -> None:
    from engine.plan import scene_file_digests

    _write_scene_script(tmp_path, "tests/scenes/s1.py")
    _write_scene_script(tmp_path, "tests/scenes/s2.py")
    data = _valid_plan_dict(scenes=[
        {"id": "s1", "actor_does": "a", "app_shows": "b", "where": "home",
         "cmd": ["python3", "tests/scenes/s1.py"], "covers": ["R1"]},
        {"id": "s2", "actor_does": "a", "app_shows": "b", "where": "home",
         "cmd": ["python3", "tests/scenes/s2.py"]},
    ])
    _write_plan(tmp_path, "job", data)
    digests = scene_file_digests(plan_path(tmp_path, "job"))
    assert set(digests) == {"tests/scenes/s1.py", "tests/scenes/s2.py"}
    assert all(digests.values())


def test_scene_file_digests_missing_referenced_file_is_empty_string(tmp_path: Path) -> None:
    from engine.plan import scene_file_digests

    data = _valid_plan_dict(scenes=[
        {"id": "s1", "actor_does": "a", "app_shows": "b", "where": "home",
         "cmd": ["python3", "tests/scenes/ghost.py"], "covers": ["R1"]},
    ])
    _write_plan(tmp_path, "job", data)
    digests = scene_file_digests(plan_path(tmp_path, "job"))
    assert digests == {"tests/scenes/ghost.py": ""}


def test_plan_digest_changes_when_plan_json_changes(tmp_path: Path) -> None:
    from engine.plan import plan_digest

    _write_plan(tmp_path, "job", _valid_plan_dict())
    before = plan_digest(plan_path(tmp_path, "job"))
    _write_plan(tmp_path, "job", _valid_plan_dict(goal="다른 목표"))
    after = plan_digest(plan_path(tmp_path, "job"))
    assert before != after


def test_plan_digest_changes_when_a_referenced_scene_file_changes(tmp_path: Path) -> None:
    from engine.plan import plan_digest

    _write_scene_script(tmp_path)
    data = _valid_plan_dict(scenes=[
        {"id": "s1", "actor_does": "a", "app_shows": "b", "where": "home",
         "cmd": ["python3", "tests/scenes/s1.py"], "covers": ["R1"]},
    ])
    _write_plan(tmp_path, "job", data)
    before = plan_digest(plan_path(tmp_path, "job"))
    (tmp_path / "tests" / "scenes" / "s1.py").write_text("print('changed')\n", encoding="utf-8")
    after = plan_digest(plan_path(tmp_path, "job"))
    assert before != after


def test_plan_digest_stable_for_unreadable_plan_json(tmp_path: Path) -> None:
    """A plan.json that cannot be read at all (job never ran, or the
    attempt's own tree was reset) still yields SOME digest, never
    raises -- a resume must always have something to compare."""
    from engine.plan import plan_digest

    digest = plan_digest(plan_path(tmp_path, "nope"))
    assert isinstance(digest, str) and digest


def test_record_plan_reviewed_appends_digest_and_scene_digests_event(tmp_path: Path) -> None:
    from engine.ledger import Ledger
    from engine.plan import plan_digest, record_plan_reviewed, scene_file_digests

    _write_scene_script(tmp_path)
    data = _valid_plan_dict(scenes=[
        {"id": "s1", "actor_does": "a", "app_shows": "b", "where": "home",
         "cmd": ["python3", "tests/scenes/s1.py"], "covers": ["R1"]},
    ])
    _write_plan(tmp_path, "job", data)
    ledger = Ledger.create(tmp_path / "ledger.sqlite3")
    record_plan_reviewed(ledger, "alpha", "attempt-1", tmp_path, "job")

    events = [e for _seq, e in ledger.events("alpha", "plan.reviewed")]
    assert len(events) == 1
    event = events[0]
    assert event.attempt_id == "attempt-1"
    assert event.payload["digest"] == plan_digest(plan_path(tmp_path, "job"))
    assert event.payload["scene_digests"] == scene_file_digests(plan_path(tmp_path, "job"))


def test_record_plan_reviewed_also_records_requirement_and_task_ids(tmp_path: Path) -> None:
    from engine.ledger import Ledger
    from engine.plan import record_plan_reviewed

    _write_scene_script(tmp_path)
    data = _valid_plan_dict(scenes=[
        {"id": "s1", "actor_does": "a", "app_shows": "b", "where": "home",
         "cmd": ["python3", "tests/scenes/s1.py"], "covers": ["R1"]},
    ])
    _write_plan(tmp_path, "job", data)
    ledger = Ledger.create(tmp_path / "ledger.sqlite3")
    record_plan_reviewed(ledger, "alpha", "attempt-1", tmp_path, "job")

    event = next(e for _seq, e in ledger.events("alpha", "plan.reviewed"))
    assert event.payload["requirement_ids"] == ["R1"]
    assert event.payload["task_ids"] == ["t1"]


# ---- scene cmd must actually run its own scene script (not merely
# mention its path) ---------------------------------------------------

def test_scene_covering_a_requirement_via_a_no_op_runner_is_a_problem(
    tmp_path: Path,
) -> None:
    """`["true", "tests/scenes/x.py"]` must be rejected even though the
    scene path appears in `cmd` -- `true` ignores its arguments and
    exits 0 without ever running that script."""
    _write_scene_script(tmp_path)
    data = _valid_plan_dict(
        scenes=[{"id": "s1", "actor_does": "클릭", "app_shows": "결과", "where": "home",
                 "cmd": ["true", "tests/scenes/s1.py"], "covers": ["R1"]}],
    )
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/",))
    assert any("scene_cmd_not_a_scene_file" in p for p in problems)


def test_scene_covering_a_requirement_via_node_is_fine(tmp_path: Path) -> None:
    _write_scene_script(tmp_path, "tests/scenes/s1.js")
    data = _valid_plan_dict(
        scenes=[{"id": "s1", "actor_does": "클릭", "app_shows": "결과", "where": "home",
                 "cmd": ["node", "tests/scenes/s1.js"], "covers": ["R1"]}],
    )
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    assert validate_plan(plan, tmp_path, build_prefixes=("src/", "tests/")) == []


def test_scene_covering_a_requirement_via_npx_vitest_run_is_fine(tmp_path: Path) -> None:
    _write_scene_script(tmp_path, "tests/scenes/s1.test.js")
    data = _valid_plan_dict(
        scenes=[{"id": "s1", "actor_does": "클릭", "app_shows": "결과", "where": "home",
                 "cmd": ["npx", "vitest", "run", "tests/scenes/s1.test.js"], "covers": ["R1"]}],
    )
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    assert validate_plan(plan, tmp_path, build_prefixes=("src/", "tests/")) == []


def test_scene_covering_a_requirement_via_a_path_qualified_runner_is_a_problem(
    tmp_path: Path,
) -> None:
    """cmd[0] must be a BARE runner name -- a path-qualified runner
    (a full interpreter path like sys.executable, or a repo-relative
    one like "./src/node") is never trusted here, since it resolves
    relative to the process's own cwd regardless of PATH, letting a
    job's own repo content silently stand in for the runner a
    reviewer thinks they are trusting."""
    import sys

    _write_scene_script(tmp_path)
    data = _valid_plan_dict(
        scenes=[{"id": "s1", "actor_does": "클릭", "app_shows": "결과", "where": "home",
                 "cmd": [sys.executable, "tests/scenes/s1.py"], "covers": ["R1"]}],
    )
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/",))
    assert any("scene_cmd_not_a_scene_file" in p for p in problems)


def test_scene_covering_a_requirement_via_a_repo_relative_runner_is_a_problem(
    tmp_path: Path,
) -> None:
    """The exact shape the external review flagged: `cmd[0]` is a
    relative path into the repo (`./src/node`), whose basename happens
    to match a supported runner's name -- this must never be trusted,
    since it would run the REPO's own executable, not the engine's."""
    _write_scene_script(tmp_path, "tests/scenes/s1.js")
    data = _valid_plan_dict(
        scenes=[{"id": "s1", "actor_does": "클릭", "app_shows": "결과", "where": "home",
                 "cmd": ["./src/node", "tests/scenes/s1.js"], "covers": ["R1"]}],
    )
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/",))
    assert any("scene_cmd_not_a_scene_file" in p for p in problems)


def test_scene_covering_a_requirement_via_bare_python3_is_fine(tmp_path: Path) -> None:
    _write_scene_script(tmp_path)
    data = _valid_plan_dict(
        scenes=[{"id": "s1", "actor_does": "클릭", "app_shows": "결과", "where": "home",
                 "cmd": ["python3", "tests/scenes/s1.py"], "covers": ["R1"]}],
    )
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    assert validate_plan(plan, tmp_path, build_prefixes=("src/", "tests/")) == []


def test_scene_covering_a_requirement_via_scene_path_escaping_tests_scenes_is_a_problem(
    tmp_path: Path,
) -> None:
    """The exact shape the external review flagged: the scene path
    LITERALLY starts with "tests/scenes/" but normalizes to something
    outside it."""
    _write_scene_script(tmp_path)
    data = _valid_plan_dict(
        scenes=[{"id": "s1", "actor_does": "클릭", "app_shows": "결과", "where": "home",
                 "cmd": ["python3", "tests/scenes/../../src/check.py"], "covers": ["R1"]}],
    )
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/",))
    assert any("scene_cmd_not_a_scene_file" in p for p in problems)


def test_scene_script_tracked_symlink_escaping_tests_scenes_is_a_problem(
    tmp_path: Path,
) -> None:
    """A scene path that stays textually inside tests/scenes/ but is a
    SYMLINK (the shape a committed, reviewed-looking file can still
    take) pointing outside it must still be rejected -- validate_plan
    resolves the real path, symlinks followed."""
    outside = tmp_path / "secret.py"
    outside.write_text("print('not a scene')\n", encoding="utf-8")
    scenes_dir = tmp_path / "tests" / "scenes"
    scenes_dir.mkdir(parents=True)
    (scenes_dir / "s1.py").symlink_to(outside)
    data = _valid_plan_dict(
        scenes=[{"id": "s1", "actor_does": "클릭", "app_shows": "결과", "where": "home",
                 "cmd": ["python3", "tests/scenes/s1.py"], "covers": ["R1"]}],
    )
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/",))
    assert any("escapes tests/scenes/" in p for p in problems)


def test_symlinked_tests_scenes_directory_is_rejected(tmp_path: Path) -> None:
    """The boundary-escape shape: "tests/scenes" ITSELF is a
    symlink (e.g. to "../src") -- resolving it would make it the
    containment boundary's OWN resolved target, so anything under
    that target (src/check.py) would wrongly look "contained". A
    symlinked tests/scenes/ must be rejected outright, not resolved
    through."""
    outside = tmp_path / "src"
    outside.mkdir()
    (outside / "check.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "scenes").symlink_to(outside)
    data = _valid_plan_dict(
        scenes=[{"id": "s1", "actor_does": "클릭", "app_shows": "결과", "where": "home",
                 "cmd": ["python3", "tests/scenes/check.py"], "covers": ["R1"]}],
    )
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/",))
    assert any("tests/scenes/ itself is a symlink" in p for p in problems)


def test_symlinked_tests_directory_is_rejected(tmp_path: Path) -> None:
    """Same boundary bug one level up: "tests" itself is a symlink,
    with a real "scenes" subdirectory underneath it."""
    outside = tmp_path / "elsewhere"
    (outside / "scenes").mkdir(parents=True)
    (outside / "scenes" / "check.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "tests").symlink_to(outside)
    data = _valid_plan_dict(
        scenes=[{"id": "s1", "actor_does": "클릭", "app_shows": "결과", "where": "home",
                 "cmd": ["python3", "tests/scenes/check.py"], "covers": ["R1"]}],
    )
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/",))
    assert any("tests/scenes/ itself is a symlink" in p for p in problems)


def test_scene_covering_a_requirement_with_scene_path_not_the_first_arg_is_a_problem(
    tmp_path: Path,
) -> None:
    """A scene path buried behind other flags (never the runner's
    first real argument) must not count either."""
    _write_scene_script(tmp_path)
    data = _valid_plan_dict(
        scenes=[{"id": "s1", "actor_does": "클릭", "app_shows": "결과", "where": "home",
                 "cmd": ["python3", "-u", "tests/scenes/s1.py"], "covers": ["R1"]}],
    )
    _write_plan(tmp_path, "job", data)
    plan = load_plan(tmp_path, "job")
    problems = validate_plan(plan, tmp_path, build_prefixes=("src/",))
    assert any("scene_cmd_not_a_scene_file" in p for p in problems)


# ---- check_resumed_plan ----------------------------------------------

def _reviewed_ledger(tmp_path: Path, data: dict[str, object], job: str = "job"):
    from engine.ledger import Ledger
    from engine.plan import record_plan_reviewed

    _write_plan(tmp_path, job, data)
    ledger = Ledger.create(tmp_path / "ledger.sqlite3")
    record_plan_reviewed(ledger, "alpha", "attempt-1", tmp_path, job)
    return ledger


def test_check_resumed_plan_ok_when_unchanged_since_review(tmp_path: Path) -> None:
    from engine.plan import check_resumed_plan

    ledger = _reviewed_ledger(tmp_path, _valid_plan_dict())
    assert check_resumed_plan(ledger, "alpha", "job", tmp_path) == []


def test_check_resumed_plan_flags_never_reviewed(tmp_path: Path) -> None:
    from engine.ledger import Ledger
    from engine.plan import check_resumed_plan

    _write_plan(tmp_path, "job", _valid_plan_dict())
    ledger = Ledger.create(tmp_path / "ledger.sqlite3")
    problems = check_resumed_plan(ledger, "alpha", "job", tmp_path)
    assert "plan_never_reviewed" in problems


def test_check_resumed_plan_flags_plan_missing(tmp_path: Path) -> None:
    from engine.ledger import Ledger
    from engine.plan import check_resumed_plan

    ledger = Ledger.create(tmp_path / "ledger.sqlite3")
    problems = check_resumed_plan(ledger, "alpha", "nope", tmp_path)
    assert any("plan_missing" in p for p in problems)


def test_check_resumed_plan_flags_plan_changed_since_review(tmp_path: Path) -> None:
    from engine.plan import check_resumed_plan

    ledger = _reviewed_ledger(tmp_path, _valid_plan_dict())
    _write_plan(tmp_path, "job", _valid_plan_dict(goal="owner-edited goal"))

    problems = check_resumed_plan(ledger, "alpha", "job", tmp_path)
    assert "plan_changed_since_review" in problems


def test_check_resumed_plan_flags_requirement_removed_since_review(tmp_path: Path) -> None:
    from engine.plan import check_resumed_plan

    data = _valid_plan_dict(
        scenes=[
            {"id": "s1", "actor_does": "a", "app_shows": "b", "where": "home",
             "cmd": ["python3", "tests/scenes/base.py"], "covers": ["R1"]},
            {"id": "s2", "actor_does": "a", "app_shows": "b", "where": "home",
             "cmd": ["python3", "tests/scenes/base.py"], "covers": ["R2"]},
        ],
        requirements=[
            {"id": "R1", "text": "요구사항 1", "kind": "function"},
            {"id": "R2", "text": "요구사항 2", "kind": "function"},
        ],
    )
    ledger = _reviewed_ledger(tmp_path, data)

    # The owner (or a compromised later stage) then narrows the plan
    # back down to R1 only -- still internally valid on its own, but a
    # requirement the reviewer already approved has vanished.
    narrowed = _valid_plan_dict(
        scenes=[{"id": "s1", "actor_does": "a", "app_shows": "b", "where": "home",
                 "cmd": ["python3", "tests/scenes/base.py"], "covers": ["R1"]}],
        requirements=[{"id": "R1", "text": "요구사항 1", "kind": "function"}],
    )
    _write_plan(tmp_path, "job", narrowed)

    problems = check_resumed_plan(ledger, "alpha", "job", tmp_path)
    assert "requirement_removed_since_review: R2" in problems


def test_check_resumed_plan_flags_task_removed_since_review(tmp_path: Path) -> None:
    from engine.plan import check_resumed_plan

    data = _valid_plan_dict(tasks=[
        {"id": "t1", "title": "하나", "detail": "", "owns": ["src/a.py"], "depends_on": []},
        {"id": "t2", "title": "둘", "detail": "", "owns": ["src/b.py"], "depends_on": []},
    ])
    ledger = _reviewed_ledger(tmp_path, data)

    narrowed = _valid_plan_dict(tasks=[
        {"id": "t1", "title": "하나", "detail": "", "owns": ["src/a.py"], "depends_on": []},
    ])
    _write_plan(tmp_path, "job", narrowed)

    problems = check_resumed_plan(ledger, "alpha", "job", tmp_path)
    assert "task_removed_since_review: t2" in problems


def test_check_resumed_plan_skips_owns_subset_check(tmp_path: Path) -> None:
    """check_resumed_plan has no config/build_prefixes of its own --
    an `owns` entry that would fail validate_plan's own build-prefix
    check elsewhere must not, by itself, make check_resumed_plan
    report a problem."""
    from engine.plan import check_resumed_plan

    data = _valid_plan_dict(tasks=[
        {"id": "t1", "title": "x", "detail": "", "owns": ["totally/unowned/path.py"],
         "depends_on": []},
    ])
    ledger = _reviewed_ledger(tmp_path, data)
    assert check_resumed_plan(ledger, "alpha", "job", tmp_path) == []


# ---- verify_reviewed_scenes -------------------------------------------

def test_verify_reviewed_scenes_ok_when_unchanged(tmp_path: Path) -> None:
    from engine.plan import verify_reviewed_scenes

    ledger = _reviewed_ledger(tmp_path, _valid_plan_dict())
    assert verify_reviewed_scenes(ledger, "alpha", "job", tmp_path) == []


def test_verify_reviewed_scenes_flags_never_reviewed(tmp_path: Path) -> None:
    from engine.ledger import Ledger
    from engine.plan import verify_reviewed_scenes

    _write_plan(tmp_path, "job", _valid_plan_dict())
    ledger = Ledger.create(tmp_path / "ledger.sqlite3")
    assert verify_reviewed_scenes(ledger, "alpha", "job", tmp_path) == ["plan_never_reviewed"]


def test_verify_reviewed_scenes_flags_plan_json_edited_since_review(tmp_path: Path) -> None:
    from engine.plan import verify_reviewed_scenes

    ledger = _reviewed_ledger(tmp_path, _valid_plan_dict())
    _write_plan(tmp_path, "job", _valid_plan_dict(goal="owner-edited goal"))
    assert verify_reviewed_scenes(ledger, "alpha", "job", tmp_path) == ["plan_changed_since_review"]


def test_verify_reviewed_scenes_flags_scene_file_edited_since_review(tmp_path: Path) -> None:
    """Editing the SCENE SCRIPT (never plan.json itself) after review
    must also be caught -- plan_digest folds in every referenced
    tests/scenes/ file's own bytes, not just plan.json's."""
    from engine.plan import verify_reviewed_scenes

    _write_scene_script(tmp_path)
    data = _valid_plan_dict(scenes=[
        {"id": "s1", "actor_does": "a", "app_shows": "b", "where": "home",
         "cmd": ["python3", "tests/scenes/s1.py"], "covers": ["R1"]},
    ])
    ledger = _reviewed_ledger(tmp_path, data)
    (tmp_path / "tests" / "scenes" / "s1.py").write_text("print('tampered')\n", encoding="utf-8")

    assert verify_reviewed_scenes(ledger, "alpha", "job", tmp_path) == ["plan_changed_since_review"]
