#!/usr/bin/env python3
"""AI-output quality eval runner for the gongbang v2 engine.

Reads every evals/tasks/*.json task (see evals/README.md for the
schema) and either:

  --dry (default): validates every task file's own schema and the
    cross-task industry-genericity setup (at least 4 distinct
    industries, each task's own industry_terms non-empty). Never
    touches the network, never spawns claude/codex, never writes
    outside a temp directory. Safe to run in CI.

  --live: for each task, actually runs `tools/gb2 new <isolated-app>
    "<request>"` in its own isolated GB2_WORKSPACE/GB2_APPS_ROOT (a
    fresh temp directory per task, never the caller's own apps), then
    checks the generated app against that task's pass_criteria and
    records pass/fail per criterion, wall time, and (when the ledger's
    own station.finished events carry it) cost_usd/turns. Calls real
    AI CLIs (claude/codex) through tools/gb2's own `require_doctor_ok`
    -- never run this in a test, only by hand with --live.

Usage:
    tools/run_evals.py                        dry run, all tasks
    tools/run_evals.py --task booking-salon    dry run, one task
    tools/run_evals.py --live                  live run, all tasks (slow, real AI)
    tools/run_evals.py --out results.json      also write the JSON report to a file
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE_ROOT))  # so `from engine...` resolves, same as tools/gb2

TASKS_DIR = ENGINE_ROOT / "evals" / "tasks"
ARCH_CHECK_PATH = ENGINE_ROOT / "tools" / "arch_check.py"
MIN_DISTINCT_INDUSTRIES = 4

REQUIRED_TASK_KEYS = {"id", "industry", "request", "requirements", "industry_terms",
                     "pass_criteria", "human_review"}
REQUIRED_REQUIREMENT_KEYS = {"id", "text", "kind"}
KNOWN_REQUIREMENT_KINDS = {"function", "security"}
REQUIRED_PASS_CRITERIA_KEYS = {"required_paths", "forbidden_cross_industry_terms",
                               "scenes_must_pass", "arch_check_must_be_clean",
                               "build_must_pass"}
_BOOL_PASS_CRITERIA_FLAGS = ("forbidden_cross_industry_terms", "scenes_must_pass",
                            "arch_check_must_be_clean", "build_must_pass")

# Never scanned for cross-industry term leakage or required_paths matches --
# build output / dependencies / vcs internals, not the generated app's own
# source.
_EXCLUDED_DIR_NAMES = {".git", "node_modules", "dist", "build", ".gongbang"}


# ---------------------------------------------------------------------------
# task loading + schema validation (used by both --dry and --live)
# ---------------------------------------------------------------------------

def load_tasks(tasks_dir: Path = TASKS_DIR) -> list[tuple[Path, dict]]:
    """[(path, parsed_json)] for every evals/tasks/*.json, sorted by
    filename for a deterministic report order. A file that fails to
    parse as JSON is still returned, as (path, {}), so validate_task
    can report it rather than this function raising and hiding every
    OTHER task's own result."""
    out: list[tuple[Path, dict]] = []
    for path in sorted(tasks_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        out.append((path, data if isinstance(data, dict) else {}))
    return out


def validate_task(path: Path, data: dict) -> list[str]:
    """[] iff `data` (parsed from `path`) matches the task schema
    evals/README.md documents. Never raises -- every check is a plain
    isinstance/membership test, so a malformed task file always comes
    back as a list of readable reasons, never a traceback."""
    errors: list[str] = []
    missing = REQUIRED_TASK_KEYS - data.keys()
    if missing:
        errors.append(f"missing keys: {sorted(missing)}")
        return errors  # nothing else below can be checked meaningfully

    if not isinstance(data["id"], str) or not data["id"]:
        errors.append("id must be a non-empty string")
    elif path.stem != data["id"]:
        errors.append(f"filename {path.stem!r} does not match id {data['id']!r}")

    if not isinstance(data["industry"], str) or not data["industry"]:
        errors.append("industry must be a non-empty string")

    if not isinstance(data["request"], str) or not data["request"].strip():
        errors.append("request must be a non-empty string")

    errors.extend(_validate_requirements(data.get("requirements")))
    errors.extend(_validate_industry_terms(data.get("industry_terms")))
    errors.extend(_validate_pass_criteria(data.get("pass_criteria")))
    errors.extend(_validate_human_review(data.get("human_review")))
    return errors


def _validate_requirements(reqs: object) -> list[str]:
    if not isinstance(reqs, list) or not reqs:
        return ["requirements must be a non-empty list"]
    errors: list[str] = []
    seen_ids: set[str] = set()
    for i, req in enumerate(reqs):
        if not isinstance(req, dict) or REQUIRED_REQUIREMENT_KEYS - req.keys():
            errors.append(f"requirements[{i}] must be an object with id/text/kind")
            continue
        if not isinstance(req["id"], str) or not req["id"]:
            errors.append(f"requirements[{i}].id must be a non-empty string")
        elif req["id"] in seen_ids:
            errors.append(f"duplicate requirement id: {req['id']!r}")
        else:
            seen_ids.add(req["id"])
        if not isinstance(req["text"], str) or not req["text"]:
            errors.append(f"requirements[{i}].text must be a non-empty string")
        if req.get("kind") not in KNOWN_REQUIREMENT_KINDS:
            errors.append(f"requirements[{i}].kind must be one of {sorted(KNOWN_REQUIREMENT_KINDS)}")
    return errors


def _validate_industry_terms(terms: object) -> list[str]:
    if not isinstance(terms, list) or not terms or not all(
        isinstance(t, str) and t for t in terms
    ):
        return ["industry_terms must be a non-empty list of non-empty strings"]
    return []


def _validate_pass_criteria(pc: object) -> list[str]:
    if not isinstance(pc, dict):
        return ["pass_criteria must be an object"]
    errors: list[str] = []
    missing = REQUIRED_PASS_CRITERIA_KEYS - pc.keys()
    if missing:
        errors.append(f"pass_criteria missing keys: {sorted(missing)}")
    required_paths = pc.get("required_paths")
    if not isinstance(required_paths, list) or not required_paths or not all(
        isinstance(p, str) and p for p in required_paths
    ):
        errors.append("pass_criteria.required_paths must be a non-empty list of non-empty strings")
    for flag in _BOOL_PASS_CRITERIA_FLAGS:
        if flag in pc and not isinstance(pc[flag], bool):
            errors.append(f"pass_criteria.{flag} must be a bool")
    return errors


def _validate_human_review(hr: object) -> list[str]:
    if not isinstance(hr, list) or not hr or not all(
        isinstance(x, str) and x for x in hr
    ):
        return ["human_review must be a non-empty list of non-empty strings"]
    return []


# ---------------------------------------------------------------------------
# dry run
# ---------------------------------------------------------------------------

def dry_run(tasks_dir: Path = TASKS_DIR) -> dict[str, object]:
    """Validates every task file's own schema, never touching the
    network or spawning any process. Also checks the cross-task setup
    this eval set itself promises: at least MIN_DISTINCT_INDUSTRIES
    distinct `industry` values (no single-industry hardcoding) and at
    least 10 tasks."""
    tasks = load_tasks(tasks_dir)
    results = []
    for path, data in tasks:
        errors = validate_task(path, data)
        results.append({
            "task_id": data.get("id", path.stem), "path": str(path),
            "schema_valid": not errors, "errors": errors,
        })
    industries = sorted({data["industry"] for _path, data in tasks
                         if isinstance(data.get("industry"), str) and data["industry"]})
    industry_count_ok = len(industries) >= MIN_DISTINCT_INDUSTRIES
    task_count_ok = len(tasks) >= 10
    ok = (task_count_ok and industry_count_ok
         and all(r["schema_valid"] for r in results))
    return {
        "mode": "dry", "task_count": len(tasks), "task_count_ok": task_count_ok,
        "industries": industries, "industry_count_ok": industry_count_ok,
        "min_distinct_industries": MIN_DISTINCT_INDUSTRIES, "results": results, "ok": ok,
    }


# ---------------------------------------------------------------------------
# live run: actually scaffolds+builds each task's app through `gb2 new`,
# in its own throwaway GB2_WORKSPACE/GB2_APPS_ROOT, then checks the
# generated repo against that task's own pass_criteria.
# ---------------------------------------------------------------------------

def _iter_source_files(repo: Path):
    for path in repo.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _EXCLUDED_DIR_NAMES for part in path.relative_to(repo).parts[:-1]):
            continue
        yield path


def _relative_paths(repo: Path) -> list[str]:
    return [str(p.relative_to(repo)) for p in _iter_source_files(repo)]


def check_required_paths(repo: Path, patterns: list[str]) -> dict[str, object]:
    """Each glob pattern (fnmatch, case-insensitive) must match at
    least one path under `repo` -- a loose, framework-agnostic stand-
    in for "the required page/feature exists somewhere", since the
    engine's own new-app template (tools/templates/new-app) is a bare
    Vite app, not a fixed-router framework whose exact file layout
    this eval set could otherwise assume."""
    rel_paths = _relative_paths(repo)
    lowered = [p.lower() for p in rel_paths]
    missing = [pat for pat in patterns
              if not any(fnmatch.fnmatch(p, pat.lower()) for p in lowered)]
    return {"name": "required_paths", "passed": not missing, "missing_patterns": missing}


def check_forbidden_cross_industry_terms(repo: Path, this_task_id: str,
                                          all_tasks: list[tuple[Path, dict]]) -> dict[str, object]:
    """No OTHER task's own industry_terms word may appear (case-
    insensitive substring) anywhere in this task's generated source --
    the operational form of "모든 앱은 업종 범용 도구다, 특정 업종을
    박지 않는다" (MASTER_RULES): if task A's generated app contains
    task B's own industry vocabulary, the generator leaked a different
    industry's hardcoded wording instead of staying generic to what
    THIS request actually asked for."""
    other_terms: set[str] = set()
    for _path, data in all_tasks:
        if data.get("id") == this_task_id:
            continue
        for term in data.get("industry_terms", []):
            if isinstance(term, str) and term:
                other_terms.add(term.lower())
    found: dict[str, list[str]] = {}
    for path in _iter_source_files(repo):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
        for term in other_terms:
            if term in text:
                found.setdefault(term, []).append(str(path.relative_to(repo)))
    return {"name": "forbidden_cross_industry_terms", "passed": not found, "leaked_terms": found}


def check_build_passes(repo: Path, timeout_sec: int = 600) -> dict[str, object]:
    if not (repo / "package.json").exists():
        return {"name": "build_passes", "passed": False, "reason": "no package.json"}
    try:
        result = subprocess.run(
            ["npm", "run", "build"], cwd=str(repo), capture_output=True, text=True,
            timeout=timeout_sec,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"name": "build_passes", "passed": False, "reason": str(exc)}
    return {"name": "build_passes", "passed": result.returncode == 0,
            "returncode": result.returncode, "stderr_tail": result.stderr[-2000:]}


def check_arch_check_clean(repo: Path, arch_check_path: Path = ARCH_CHECK_PATH,
                           timeout_sec: int = 120) -> dict[str, object]:
    """arch_check.py (COMMON_ARCHITECTURE_STANDARD.md §5) does not
    exist yet -- this reports "skipped", never "failed", so a task's
    own overall pass/fail does not depend on a tool that has not been
    built, exactly as this eval set's own job description says
    ("arch_check 깨끗함, 존재하면")."""
    if not arch_check_path.exists():
        return {"name": "arch_check_clean", "passed": None,
                "reason": f"{arch_check_path} does not exist yet"}
    try:
        result = subprocess.run(
            [sys.executable, str(arch_check_path), str(repo)],
            capture_output=True, text=True, timeout=timeout_sec,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"name": "arch_check_clean", "passed": False, "reason": str(exc)}
    return {"name": "arch_check_clean", "passed": result.returncode == 0,
            "returncode": result.returncode, "stdout_tail": result.stdout[-2000:]}


def check_scenes_pass(ledger_path: Path, project: str) -> dict[str, object]:
    """Reads the job's own acceptance.recorded ledger event (engine.
    acceptance's real write, see engine/steps -- every plan.scenes[*]
    result lives there) instead of re-running anything: `passed` and
    the per-scene breakdown are exactly what the job's own acceptance
    stage already certified."""
    from engine.ledger import Ledger

    if not ledger_path.exists():
        return {"name": "scenes_pass", "passed": False, "reason": "no ledger at this path"}
    ledger = Ledger.open(ledger_path)
    try:
        events = ledger.events(project, "acceptance.recorded")
    finally:
        ledger.close()
    if not events:
        return {"name": "scenes_pass", "passed": False, "reason": "no acceptance.recorded event"}
    _seq, latest = events[-1]
    return {"name": "scenes_pass", "passed": bool(latest.payload.get("passed")),
            "scenes": latest.payload.get("scenes", [])}


def _station_cost_and_turns(ledger_path: Path, project: str) -> dict[str, object]:
    """Sums cost_usd/turns across this project's own station.finished
    events, when the worker runner reported them (engine.workers.*
    fills these in for a real claude/codex run; a scripted test
    worker may leave them at 0.0/absent) -- never fabricated, just
    whatever the ledger already has."""
    from engine.ledger import Ledger

    if not ledger_path.exists():
        return {"cost_usd": None, "turns": None}
    ledger = Ledger.open(ledger_path)
    try:
        events = ledger.events(project, "station.finished")
    finally:
        ledger.close()
    costs = [e.payload.get("cost_usd") for _seq, e in events if isinstance(e.payload.get("cost_usd"), (int, float))]
    turns = [e.payload.get("turns") for _seq, e in events if isinstance(e.payload.get("turns"), (int, float))]
    return {"cost_usd": round(sum(costs), 4) if costs else None,
            "turns": sum(turns) if turns else None}


def run_task_live(task_path: Path, data: dict, all_tasks: list[tuple[Path, dict]],
                  *, gb2_path: Path | None = None) -> dict[str, object]:
    """Runs this ONE task through `tools/gb2 new` in a fresh, throwaway
    GB2_WORKSPACE/GB2_APPS_ROOT (a tempdir removed at the end -- never
    the caller's own apps/, so parallel --live runs of different tasks
    can never collide), then checks pass_criteria against the
    generated repo. Calls real AI CLIs via tools/gb2's own doctor
    check -- the caller (main()) only reaches this under --live."""
    gb2 = gb2_path or (ENGINE_ROOT / "tools" / "gb2")
    pc = data["pass_criteria"]
    app_name = data["id"].replace("_", "-")
    with tempfile.TemporaryDirectory(prefix=f"gb2-eval-{app_name}-") as tmp:
        tmp_path = Path(tmp)
        workspace = tmp_path / "workspace"
        apps_root = tmp_path / "apps"
        workspace.mkdir()
        apps_root.mkdir()
        env = {**os.environ, "GB2_WORKSPACE": str(workspace), "GB2_APPS_ROOT": str(apps_root)}

        start = time.monotonic()
        try:
            result = subprocess.run(
                [str(gb2), "new", app_name, data["request"]],
                cwd=str(ENGINE_ROOT), env=env, capture_output=True, text=True, timeout=1800,
            )
        except subprocess.TimeoutExpired:
            return {"task_id": data["id"], "ran": False, "reason": "timeout", "wall_sec": 1800.0}
        wall_sec = time.monotonic() - start

        summary = _last_json_line(result.stdout)
        repo = apps_root / app_name
        ledger_path = workspace / "ledger.sqlite"

        criteria: list[dict[str, object]] = []
        if not repo.exists():
            return {
                "task_id": data["id"], "ran": True, "wall_sec": round(wall_sec, 1),
                "gb2_summary": summary, "gb2_stderr_tail": result.stderr[-2000:],
                "criteria": criteria, "ok": False, "reason": "app repo was never created",
            }
        criteria.append(check_required_paths(repo, pc["required_paths"]))
        if pc.get("forbidden_cross_industry_terms", True):
            criteria.append(check_forbidden_cross_industry_terms(repo, data["id"], all_tasks))
        if pc.get("build_must_pass", True):
            criteria.append(check_build_passes(repo))
        if pc.get("arch_check_must_be_clean", True):
            criteria.append(check_arch_check_clean(repo))
        if pc.get("scenes_must_pass", True):
            criteria.append(check_scenes_pass(ledger_path, app_name))
        cost_turns = _station_cost_and_turns(ledger_path, app_name)

        ok = all(c["passed"] is not False for c in criteria)  # None (skipped) does not fail it
        return {
            "task_id": data["id"], "ran": True, "wall_sec": round(wall_sec, 1),
            "gb2_summary": summary, "criteria": criteria, "cost_usd": cost_turns["cost_usd"],
            "turns": cost_turns["turns"], "ok": ok,
        }


def _last_json_line(stdout: str) -> dict[str, object] | None:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def live_run(tasks_dir: Path = TASKS_DIR, *, only_task: str | None = None) -> dict[str, object]:
    all_tasks = load_tasks(tasks_dir)
    if not shutil.which("gb2") and not (ENGINE_ROOT / "tools" / "gb2").exists():
        return {"mode": "live", "ok": False, "reason": "tools/gb2 not found", "results": []}
    results = []
    for path, data in all_tasks:
        if only_task and data.get("id") != only_task:
            continue
        errors = validate_task(path, data)
        if errors:
            results.append({"task_id": data.get("id", path.stem), "ran": False,
                            "reason": f"schema invalid: {errors}", "ok": False})
            continue
        results.append(run_task_live(path, data, all_tasks))
    return {"mode": "live", "results": results, "ok": all(r.get("ok") for r in results)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                       help="actually run tools/gb2 new per task (real AI, slow)")
    parser.add_argument("--task", default=None, help="only this task id")
    parser.add_argument("--out", default=None, help="also write the JSON report here")
    args = parser.parse_args(argv)

    if args.live:
        report = live_run(only_task=args.task)
    else:
        report = dry_run()
        if args.task:
            report["results"] = [r for r in report["results"] if r["task_id"] == args.task]
            report["ok"] = all(r["schema_valid"] for r in report["results"])

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
