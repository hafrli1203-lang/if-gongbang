"""Unit tests for evals/tasks/*.json's own schema and tools/run_evals.py's
dry run -- never calls real AI, never spawns tools/gb2. --live is
exercised by hand only (evals/README.md), not here."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUN_EVALS_PATH = _REPO_ROOT / "tools" / "run_evals.py"
_TASKS_DIR = _REPO_ROOT / "evals" / "tasks"


def _load_run_evals() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_evals_under_test", _RUN_EVALS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def run_evals() -> ModuleType:
    return _load_run_evals()


def test_at_least_ten_task_files_exist() -> None:
    assert len(list(_TASKS_DIR.glob("*.json"))) >= 10


def test_every_task_file_is_valid_json_object() -> None:
    for path in _TASKS_DIR.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), path


def test_every_real_task_file_passes_schema_validation(run_evals: ModuleType) -> None:
    tasks = run_evals.load_tasks()
    assert len(tasks) >= 10
    for path, data in tasks:
        errors = run_evals.validate_task(path, data)
        assert errors == [], (path, errors)


def test_at_least_four_distinct_industries(run_evals: ModuleType) -> None:
    tasks = run_evals.load_tasks()
    industries = {data["industry"] for _path, data in tasks}
    assert len(industries) >= run_evals.MIN_DISTINCT_INDUSTRIES


def test_no_two_tasks_share_an_id(run_evals: ModuleType) -> None:
    tasks = run_evals.load_tasks()
    ids = [data["id"] for _path, data in tasks]
    assert len(ids) == len(set(ids))


def test_every_task_industry_terms_are_distinct_from_every_other_tasks(
    run_evals: ModuleType,
) -> None:
    """The cross-industry leak check (check_forbidden_cross_industry_
    terms) is only meaningful if no two tasks share a term -- otherwise
    task A's own request could legitimately use a word that is ALSO
    task B's industry_terms entry, and the live check would flag task
    A's own generated app for using its OWN requested vocabulary."""
    tasks = run_evals.load_tasks()
    by_task: dict[str, set[str]] = {
        data["id"]: {t.lower() for t in data["industry_terms"]} for _path, data in tasks
    }
    for task_id, terms in by_task.items():
        for other_id, other_terms in by_task.items():
            if other_id == task_id:
                continue
            overlap = terms & other_terms
            assert not overlap, (task_id, other_id, overlap)


# ---- validate_task's own error reporting (schema regressions) ----------

def test_validate_task_reports_missing_keys(run_evals: ModuleType) -> None:
    errors = run_evals.validate_task(Path("x.json"), {})
    assert any("missing keys" in e for e in errors)


def test_validate_task_reports_filename_id_mismatch(run_evals: ModuleType) -> None:
    data = {
        "id": "wrong-id", "industry": "x", "request": "요청",
        "requirements": [{"id": "R1", "text": "t", "kind": "function"}],
        "industry_terms": ["a"],
        "pass_criteria": {
            "required_paths": ["*a*"], "forbidden_cross_industry_terms": True,
            "scenes_must_pass": True, "arch_check_must_be_clean": True,
            "build_must_pass": True,
        },
        "human_review": ["검토"],
    }
    errors = run_evals.validate_task(Path("right-id.json"), data)
    assert any("does not match id" in e for e in errors)


def test_validate_task_reports_unknown_requirement_kind(run_evals: ModuleType) -> None:
    data = {
        "id": "x", "industry": "x", "request": "요청",
        "requirements": [{"id": "R1", "text": "t", "kind": "not_a_kind"}],
        "industry_terms": ["a"],
        "pass_criteria": {
            "required_paths": ["*a*"], "forbidden_cross_industry_terms": True,
            "scenes_must_pass": True, "arch_check_must_be_clean": True,
            "build_must_pass": True,
        },
        "human_review": ["검토"],
    }
    errors = run_evals.validate_task(Path("x.json"), data)
    assert any("kind must be one of" in e for e in errors)


def test_validate_task_reports_duplicate_requirement_ids(run_evals: ModuleType) -> None:
    data = {
        "id": "x", "industry": "x", "request": "요청",
        "requirements": [
            {"id": "R1", "text": "t1", "kind": "function"},
            {"id": "R1", "text": "t2", "kind": "function"},
        ],
        "industry_terms": ["a"],
        "pass_criteria": {
            "required_paths": ["*a*"], "forbidden_cross_industry_terms": True,
            "scenes_must_pass": True, "arch_check_must_be_clean": True,
            "build_must_pass": True,
        },
        "human_review": ["검토"],
    }
    errors = run_evals.validate_task(Path("x.json"), data)
    assert any("duplicate requirement id" in e for e in errors)


def test_validate_task_reports_empty_industry_terms(run_evals: ModuleType) -> None:
    data = {
        "id": "x", "industry": "x", "request": "요청",
        "requirements": [{"id": "R1", "text": "t", "kind": "function"}],
        "industry_terms": [],
        "pass_criteria": {
            "required_paths": ["*a*"], "forbidden_cross_industry_terms": True,
            "scenes_must_pass": True, "arch_check_must_be_clean": True,
            "build_must_pass": True,
        },
        "human_review": ["검토"],
    }
    errors = run_evals.validate_task(Path("x.json"), data)
    assert any("industry_terms" in e for e in errors)


def test_validate_task_reports_missing_required_paths(run_evals: ModuleType) -> None:
    data = {
        "id": "x", "industry": "x", "request": "요청",
        "requirements": [{"id": "R1", "text": "t", "kind": "function"}],
        "industry_terms": ["a"],
        "pass_criteria": {
            "forbidden_cross_industry_terms": True, "scenes_must_pass": True,
            "arch_check_must_be_clean": True, "build_must_pass": True,
        },
        "human_review": ["검토"],
    }
    errors = run_evals.validate_task(Path("x.json"), data)
    assert any("required_paths" in e for e in errors)


def test_validate_task_reports_non_bool_pass_criteria_flag(run_evals: ModuleType) -> None:
    data = {
        "id": "x", "industry": "x", "request": "요청",
        "requirements": [{"id": "R1", "text": "t", "kind": "function"}],
        "industry_terms": ["a"],
        "pass_criteria": {
            "required_paths": ["*a*"], "forbidden_cross_industry_terms": "yes",
            "scenes_must_pass": True, "arch_check_must_be_clean": True,
            "build_must_pass": True,
        },
        "human_review": ["검토"],
    }
    errors = run_evals.validate_task(Path("x.json"), data)
    assert any("forbidden_cross_industry_terms must be a bool" in e for e in errors)


def test_validate_task_reports_empty_human_review(run_evals: ModuleType) -> None:
    data = {
        "id": "x", "industry": "x", "request": "요청",
        "requirements": [{"id": "R1", "text": "t", "kind": "function"}],
        "industry_terms": ["a"],
        "pass_criteria": {
            "required_paths": ["*a*"], "forbidden_cross_industry_terms": True,
            "scenes_must_pass": True, "arch_check_must_be_clean": True,
            "build_must_pass": True,
        },
        "human_review": [],
    }
    errors = run_evals.validate_task(Path("x.json"), data)
    assert any("human_review" in e for e in errors)


def test_load_tasks_reports_unparseable_json_as_empty_dict_not_a_crash(
    run_evals: ModuleType, tmp_path: Path,
) -> None:
    (tmp_path / "broken.json").write_text("{not valid json", encoding="utf-8")
    tasks = run_evals.load_tasks(tmp_path)
    assert len(tasks) == 1
    path, data = tasks[0]
    assert data == {}
    errors = run_evals.validate_task(path, data)
    assert errors  # reported, not raised


# ---- dry_run itself --------------------------------------------------------

def test_dry_run_on_the_real_tasks_dir_is_ok(run_evals: ModuleType) -> None:
    report = run_evals.dry_run()
    assert report["mode"] == "dry"
    assert report["ok"] is True
    assert report["task_count"] >= 10
    assert report["industry_count_ok"] is True
    assert all(r["schema_valid"] for r in report["results"])


def test_dry_run_fails_closed_on_a_tasks_dir_with_one_bad_task(
    run_evals: ModuleType, tmp_path: Path,
) -> None:
    (tmp_path / "ok.json").write_text(json.dumps({
        "id": "ok", "industry": "a", "request": "요청",
        "requirements": [{"id": "R1", "text": "t", "kind": "function"}],
        "industry_terms": ["x"],
        "pass_criteria": {
            "required_paths": ["*x*"], "forbidden_cross_industry_terms": True,
            "scenes_must_pass": True, "arch_check_must_be_clean": True,
            "build_must_pass": True,
        },
        "human_review": ["검토"],
    }), encoding="utf-8")
    (tmp_path / "bad.json").write_text(json.dumps({"id": "bad"}), encoding="utf-8")
    report = run_evals.dry_run(tmp_path)
    assert report["ok"] is False
    assert report["task_count"] == 2
    bad = next(r for r in report["results"] if r["task_id"] == "bad")
    assert bad["schema_valid"] is False


def test_main_dry_run_exits_zero(run_evals: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    code = run_evals.main([])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry"
    assert out["ok"] is True


def test_main_dry_run_never_touches_the_network_or_spawns_a_process(
    run_evals: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contract this whole module promises: --dry (the default)
    must never call subprocess.run at all."""
    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("dry run must never call subprocess.run")

    monkeypatch.setattr(run_evals.subprocess, "run", _boom)
    code = run_evals.main([])
    assert code == 0


# ---- the pure, no-subprocess parts of the live-run checks ------------------

def test_check_required_paths_matches_case_insensitively(
    run_evals: ModuleType, tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Booking.tsx").write_text("x", encoding="utf-8")
    result = run_evals.check_required_paths(tmp_path, ["*book*"])
    assert result["passed"] is True


def test_check_required_paths_reports_missing_patterns(
    run_evals: ModuleType, tmp_path: Path,
) -> None:
    (tmp_path / "unrelated.txt").write_text("x", encoding="utf-8")
    result = run_evals.check_required_paths(tmp_path, ["*booking*"])
    assert result["passed"] is False
    assert result["missing_patterns"] == ["*booking*"]


def test_check_required_paths_excludes_node_modules(
    run_evals: ModuleType, tmp_path: Path,
) -> None:
    (tmp_path / "node_modules" / "booking-lib").mkdir(parents=True)
    (tmp_path / "node_modules" / "booking-lib" / "index.js").write_text("x", encoding="utf-8")
    result = run_evals.check_required_paths(tmp_path, ["*booking*"])
    assert result["passed"] is False


def test_check_forbidden_cross_industry_terms_detects_a_leak(
    run_evals: ModuleType, tmp_path: Path,
) -> None:
    (tmp_path / "app.js").write_text("// 미용실 예약\n", encoding="utf-8")
    all_tasks = [
        (Path("a.json"), {"id": "a", "industry_terms": ["미용실"]}),
        (Path("b.json"), {"id": "b", "industry_terms": ["반찬"]}),
    ]
    result = run_evals.check_forbidden_cross_industry_terms(tmp_path, "b", all_tasks)
    assert result["passed"] is False
    assert "미용실" in result["leaked_terms"]


def test_check_forbidden_cross_industry_terms_allows_this_tasks_own_terms(
    run_evals: ModuleType, tmp_path: Path,
) -> None:
    (tmp_path / "app.js").write_text("// 미용실 예약\n", encoding="utf-8")
    all_tasks = [
        (Path("a.json"), {"id": "a", "industry_terms": ["미용실"]}),
        (Path("b.json"), {"id": "b", "industry_terms": ["반찬"]}),
    ]
    result = run_evals.check_forbidden_cross_industry_terms(tmp_path, "a", all_tasks)
    assert result["passed"] is True


def test_check_arch_check_clean_skips_when_the_script_does_not_exist(
    run_evals: ModuleType, tmp_path: Path,
) -> None:
    result = run_evals.check_arch_check_clean(tmp_path, arch_check_path=tmp_path / "nope.py")
    assert result["passed"] is None


def test_check_build_passes_fails_closed_without_package_json(
    run_evals: ModuleType, tmp_path: Path,
) -> None:
    result = run_evals.check_build_passes(tmp_path)
    assert result["passed"] is False


def test_check_scenes_pass_fails_closed_without_a_ledger(
    run_evals: ModuleType, tmp_path: Path,
) -> None:
    result = run_evals.check_scenes_pass(tmp_path / "no-ledger.sqlite", "alpha")
    assert result["passed"] is False
