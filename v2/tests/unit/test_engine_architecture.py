"""The engine itself must pass tools/arch_check.py (docs/standards/
architecture.md §5: the structure check runs in CI). Only the entries in
ALLOWED below may remain; each says why it is still there."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# (check, file, detail) -- remove an entry once its reason is gone.
ALLOWED = {
    # Station-output checks read facts.md/probe logs through
    # engine.infrastructure.trusted (no-symlink capture); moving the file
    # reading behind a port is a separate redesign.
    ("layer_direction", "engine/domain/stage_checks.py",
     "imports engine/infrastructure/__init__.py (domain -> infrastructure not allowed)"),
    # plan.tools validation resolves skill paths with
    # engine.infrastructure.skills.skill_path_in_root (filesystem).
    ("layer_direction", "engine/domain/plan_tools.py",
     "imports engine/infrastructure/skills.py (domain -> infrastructure not allowed)"),
    # engine/gate.py and engine/gates.py are the compatibility aliases for
    # engine.interface.hook_gate and engine.domain.stage_checks
    # (docs/standards/engine-layout.md); they go when the aliases go.
    ("near_duplicate_name", "engine/gate.py", "near-duplicate of engine/gates.py"),
}


def _arch_check():
    spec = importlib.util.spec_from_file_location("arch_check_engine", ROOT / "tools" / "arch_check.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_engine_has_no_architecture_violations_outside_the_allowlist() -> None:
    arch_check = _arch_check()
    violations = arch_check.run(ROOT, arch_check.DEFAULT_LAYER_RULES, [])
    found = {v.row() for v in violations if v.file.startswith("engine/")}
    assert found - ALLOWED == set(), sorted(found - ALLOWED)
    assert ALLOWED - found == set(), f"allowlist entries no longer needed: {sorted(ALLOWED - found)}"
