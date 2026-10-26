"""Unit tests for tools/arch_check.py: a small TS fixture tree and a
small Python fixture tree, each with exactly one violation of every
kind arch_check checks, plus a clean tree of the same shape with
none."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_ARCH_CHECK_PATH = Path(__file__).resolve().parents[2] / "tools" / "arch_check.py"


def _load_arch_check() -> ModuleType:
    spec = importlib.util.spec_from_file_location("arch_check_under_test", _ARCH_CHECK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def arch_check() -> ModuleType:
    return _load_arch_check()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# TS fixture: one violation of each kind.
# ---------------------------------------------------------------------------

def _make_ts_violations_tree(root: Path) -> None:
    # file_size -- interface layer, no imports.
    _write(root / "src/app/big-page.js",
           "export const BIG = 1;\n" + "// padding\n" * 801)

    # layer_direction -- domain importing application (banned).
    _write(root / "src/server/application/greeting/get-greeting.js",
           'export function helper() {\n  return "hi";\n}\n')
    _write(root / "src/server/domain/greeting/rules.js",
           'import { helper } from "../../application/greeting/get-greeting.js";\n'
           "export function rule() {\n  return helper();\n}\n")

    # private_import -- interface importing a private name from domain.
    _write(root / "src/server/domain/greeting/secret.js", 'export const _secret = "shh";\n')
    _write(root / "src/components/banner.js",
           'import { _secret } from "../server/domain/greeting/secret.js";\n'
           "export function show() {\n  return _secret;\n}\n")

    # import_cycle -- two neutral-layer modules importing each other.
    _write(root / "src/lib/a.js", 'import { b } from "./b.js";\nexport function a() {\n  return b();\n}\n')
    _write(root / "src/lib/b.js", 'import { a } from "./a.js";\nexport function b() {\n  return a();\n}\n')

    # banned_name -- a vague filename.
    _write(root / "src/components/utils.js", "export function noop() {}\n")

    # near_duplicate_name -- two near-identical filenames, same dir.
    _write(root / "src/lib/gate.js", "export function gate() { return true; }\n")
    _write(root / "src/lib/gates.js", "export function gates() { return true; }\n")


def _make_ts_clean_tree(root: Path) -> None:
    _write(root / "src/app/page.js",
           'import { doThing } from "../server/application/booking/do-thing.js";\n'
           "export function render() {\n  return doThing();\n}\n")
    _write(root / "src/server/application/booking/do-thing.js",
           'import { bookingRule } from "../../domain/booking/rules.js";\n'
           "export function doThing() {\n  return bookingRule();\n}\n")
    _write(root / "src/server/domain/booking/rules.js",
           "export function bookingRule() {\n  return true;\n}\n")
    _write(root / "src/server/infrastructure/integrations/clock.js",
           "export const systemClock = { now: () => new Date() };\n")
    _write(root / "src/lib/format-date.js", "export function formatDate(d) {\n  return String(d);\n}\n")


def test_ts_fixture_flags_each_violation_kind(tmp_path: Path, arch_check: ModuleType) -> None:
    root = tmp_path / "ts_app"
    _make_ts_violations_tree(root)

    violations = arch_check.run(root, arch_check.DEFAULT_LAYER_RULES, [])
    kinds = {v.check for v in violations}

    assert kinds == {
        "file_size", "layer_direction", "private_import",
        "import_cycle", "banned_name", "near_duplicate_name",
    }
    assert any(v.file == "src/app/big-page.js" for v in violations if v.check == "file_size")
    assert any(v.file == "src/server/domain/greeting/rules.js"
               for v in violations if v.check == "layer_direction")
    assert any(v.file == "src/components/banner.js"
               for v in violations if v.check == "private_import")
    assert any(v.file == "src/components/utils.js" for v in violations if v.check == "banned_name")


def test_ts_clean_tree_has_no_violations(tmp_path: Path, arch_check: ModuleType) -> None:
    root = tmp_path / "ts_clean"
    _make_ts_clean_tree(root)

    violations = arch_check.run(root, arch_check.DEFAULT_LAYER_RULES, [])
    assert violations == []


# ---------------------------------------------------------------------------
# Python fixture: one violation of each kind.
# ---------------------------------------------------------------------------

def _make_py_violations_tree(root: Path) -> None:
    # file_size -- domain layer, no imports.
    _write(root / "engine/domain/big_rule.py", "BIG = 1\n" + "# padding\n" * 801)

    # layer_direction -- domain importing application (banned).
    _write(root / "engine/application/get_greeting.py", 'def helper():\n    return "hi"\n')
    _write(root / "engine/domain/greeting.py",
           "from engine.application.get_greeting import helper\n\n\ndef rule():\n    return helper()\n")

    # private_import -- interface importing a private name from domain.
    _write(root / "engine/domain/secret.py", '_secret = "shh"\n')
    _write(root / "engine/interface/cli.py",
           "from engine.domain.secret import _secret\n\n\ndef show():\n    return _secret\n")

    # import_cycle -- two infrastructure modules importing each other.
    _write(root / "engine/infrastructure/a.py",
           "from engine.infrastructure.b import b_func\n\n\ndef a_func():\n    return b_func()\n")
    _write(root / "engine/infrastructure/b.py",
           "from engine.infrastructure.a import a_func\n\n\ndef b_func():\n    return a_func()\n")

    # banned_name -- a vague module name.
    _write(root / "engine/infrastructure/utils.py", "def noop():\n    pass\n")

    # near_duplicate_name -- two near-identical module names, same dir.
    _write(root / "engine/domain/gate.py", "def gate():\n    return True\n")
    _write(root / "engine/domain/gates.py", "def gates():\n    return True\n")


def _make_py_clean_tree(root: Path) -> None:
    _write(root / "engine/interface/cli.py",
           "from engine.application.do_thing import do_thing\n\n\ndef main():\n    return do_thing()\n")
    _write(root / "engine/application/do_thing.py",
           "from engine.domain.rules import booking_rule\n\n\ndef do_thing():\n    return booking_rule()\n")
    _write(root / "engine/domain/rules.py", "def booking_rule():\n    return True\n")
    _write(root / "engine/infrastructure/clock.py", "import datetime\n\n\ndef now():\n    return datetime.datetime.now()\n")


def test_py_fixture_flags_each_violation_kind(tmp_path: Path, arch_check: ModuleType) -> None:
    root = tmp_path / "py_app"
    _make_py_violations_tree(root)

    violations = arch_check.run(root, arch_check.DEFAULT_LAYER_RULES, [])
    kinds = {v.check for v in violations}

    assert kinds == {
        "file_size", "layer_direction", "private_import",
        "import_cycle", "banned_name", "near_duplicate_name",
    }
    assert any(v.file == "engine/domain/big_rule.py" for v in violations if v.check == "file_size")
    assert any(v.file == "engine/domain/greeting.py"
               for v in violations if v.check == "layer_direction")
    assert any(v.file == "engine/interface/cli.py"
               for v in violations if v.check == "private_import")
    assert any(v.file == "engine/infrastructure/utils.py" for v in violations if v.check == "banned_name")


def test_py_clean_tree_has_no_violations(tmp_path: Path, arch_check: ModuleType) -> None:
    root = tmp_path / "py_clean"
    _make_py_clean_tree(root)

    violations = arch_check.run(root, arch_check.DEFAULT_LAYER_RULES, [])
    assert violations == []


def test_py_layer_direction_rules(tmp_path: Path, arch_check: ModuleType) -> None:
    """application -> infrastructure is allowed; infrastructure -> application,
    domain -> infrastructure and application -> interface are not."""
    root = tmp_path / "py_layers"
    _write(root / "engine/infrastructure/store.py", "def save():\n    return 1\n")
    _write(root / "engine/application/run_job.py",
           "from engine.infrastructure.store import save\n\n\ndef run():\n    return save()\n")
    assert arch_check.run(root, arch_check.DEFAULT_LAYER_RULES, []) == []

    _write(root / "engine/interface/cli.py", "def main():\n    return 0\n")
    _write(root / "engine/infrastructure/adapter.py",
           "from engine.application.run_job import run\n\n\ndef go():\n    return run()\n")
    _write(root / "engine/domain/rule.py",
           "from engine.infrastructure.store import save\n\n\ndef rule():\n    return save()\n")
    _write(root / "engine/application/report.py",
           "from engine.interface.cli import main\n\n\ndef report():\n    return main()\n")
    flagged = {v.file for v in arch_check.run(root, arch_check.DEFAULT_LAYER_RULES, [])
               if v.check == "layer_direction"}
    assert flagged == {"engine/infrastructure/adapter.py", "engine/domain/rule.py",
                       "engine/application/report.py"}


# ---------------------------------------------------------------------------
# Opt-in industry-hardcoding word check + CLI exit codes.
# ---------------------------------------------------------------------------

def test_hardcoded_words_check_is_opt_in_and_configurable(tmp_path: Path, arch_check: ModuleType) -> None:
    root = tmp_path / "words_app"
    _write(root / "src/app/page.js", 'export const LABEL = "glasses-shop";\n')

    assert arch_check.run(root, arch_check.DEFAULT_LAYER_RULES, []) == []

    violations = arch_check.run(root, arch_check.DEFAULT_LAYER_RULES, ["glasses-shop"])
    assert [v.check for v in violations] == ["hardcoded_word"]
    assert violations[0].file == "src/app/page.js"


def test_cli_exits_1_on_violation_and_0_on_clean(tmp_path: Path, arch_check: ModuleType) -> None:
    dirty = tmp_path / "dirty"
    _make_ts_violations_tree(dirty)
    assert arch_check.main([str(dirty), "--json"]) == 1

    clean = tmp_path / "clean"
    _make_ts_clean_tree(clean)
    assert arch_check.main([str(clean)]) == 0


def test_layers_override_replaces_defaults(tmp_path: Path, arch_check: ModuleType) -> None:
    root = tmp_path / "custom"
    _write(root / "weird/top.py", "from weird.bottom import helper\n\n\ndef f():\n    return helper()\n")
    _write(root / "weird/bottom.py", "def helper():\n    return 1\n")

    rules_path = tmp_path / "rules.json"
    rules_path.write_text(
        '[{"match": "component", "pattern": "weird", "layer": "interface"}]', encoding="utf-8",
    )
    rules = arch_check.load_layer_rules(str(rules_path))
    # Both files land in "interface" under this override -- same-layer
    # imports are always allowed, so no layer_direction violation,
    # unlike the (unused here) component rules that would split
    # "top"/"bottom" across layers.
    violations = arch_check.run(root, rules, [])
    assert [v.check for v in violations] == []
