"""README.md and docs/ENGINE.md must stay in sync with
the real code, not just read well. Every command the README tells an
operator to type must exist in the actual argparse parser (`tools/gb2`
or `engine.cli`), and every test_*.py file either doc cites must exist
on disk -- the exact kind of drift that made the old README still
describe stage 1/2 behavior long after stage 4 shipped.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if not (ROOT / "docs" / "ENGINE.md").is_file():
    # The published copy ships its own shorter README/ENGINE.md; these
    # checks cover the engine repository's operator docs.
    pytest.skip("engine repository docs not present in this checkout",
                allow_module_level=True)
README = (ROOT / "README.md").read_text(encoding="utf-8")
ENGINE_MD = (ROOT / "docs" / "ENGINE.md").read_text(encoding="utf-8")
GB2_SOURCE = (ROOT / "tools" / "gb2").read_text(encoding="utf-8")
CLI_SOURCE = (ROOT / "engine" / "interface" / "cli.py").read_text(encoding="utf-8")

_ADD_PARSER = re.compile(r'sub\.add_parser\("([a-z-]+)"')
_README_GB2_LINE = re.compile(r"^tools/gb2 (\S+)", re.MULTILINE)
_README_TABLE_ROW = re.compile(r"^\| `([a-z-]+)[^`]*` \|", re.MULTILINE)
_TEST_FILE_NAME = re.compile(r"test_[A-Za-z0-9_]+\.py")


def _gb2_parser_commands() -> set[str]:
    return set(_ADD_PARSER.findall(GB2_SOURCE))


def _cli_parser_commands() -> set[str]:
    return set(_ADD_PARSER.findall(CLI_SOURCE))


def _readme_gb2_commands() -> set[str]:
    """Every `tools/gb2 <command> ...` line in README's fenced example."""
    return set(_README_GB2_LINE.findall(README))


def _readme_engine_cli_commands() -> set[str]:
    """The engine.cli command table's first column, e.g. "`run <project>
    <attempt_id>`" -> "run", "`backup [--dest <dir>] [--keep N]`" ->
    "backup"."""
    return set(_README_TABLE_ROW.findall(README))


def _cited_test_files() -> set[str]:
    return set(_TEST_FILE_NAME.findall(README)) | set(_TEST_FILE_NAME.findall(ENGINE_MD))


def test_readme_gb2_commands_exist_in_the_real_parser() -> None:
    cited = _readme_gb2_commands()
    assert cited, "README's `tools/gb2 ...` example produced no commands to check"
    real = _gb2_parser_commands()
    missing = cited - real
    assert not missing, f"README names gb2 command(s) with no add_parser: {sorted(missing)}"


def test_readme_engine_cli_commands_exist_in_the_real_parser() -> None:
    cited = _readme_engine_cli_commands()
    assert cited, "README's engine.cli command table produced no commands to check"
    real = _cli_parser_commands()
    missing = cited - real
    assert not missing, (
        f"README names engine.cli command(s) with no add_parser: {sorted(missing)}"
    )


def test_every_gb2_parser_command_is_documented_in_readme() -> None:
    """The reverse direction: a command nobody can discover from the
    README (a new subcommand added to tools/gb2 without a README line)
    is exactly the kind of drift P6 exists to catch, not just a typo'd
    command name that IS in the README but shouldn't be."""
    real = _gb2_parser_commands()
    cited = _readme_gb2_commands()
    missing = real - cited
    assert not missing, f"tools/gb2 command(s) missing from README: {sorted(missing)}"


def test_every_cli_parser_command_is_documented_in_readme() -> None:
    real = _cli_parser_commands()
    cited = _readme_engine_cli_commands()
    missing = real - cited
    assert not missing, f"engine.cli command(s) missing from README: {sorted(missing)}"


def test_every_cited_test_file_exists() -> None:
    cited = _cited_test_files()
    assert cited, "no test_*.py citations found in README/ENGINE.md -- extraction broke"
    tests_dir = ROOT / "tests"
    missing = [name for name in sorted(cited) if not any(tests_dir.rglob(name))]
    assert not missing, f"README/ENGINE.md cite test file(s) that don't exist: {missing}"
