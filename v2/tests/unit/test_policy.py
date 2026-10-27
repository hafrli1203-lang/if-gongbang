"""Unit tests for engine.policy: rules 1-6, symlink escapes, logging."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from engine.ledger import Event, Ledger
from engine.policy import Action, Actor, Decision, Policy, ProjectFacts
from engine.state import transition
from engine.steps.open_job import copy_root as production_copy_root
from tests.conftest import Workspace


def _open_project(ledger: Ledger, project: str, generation: int = 1) -> str:
    attempt_id = f"J-{project}-TEST#1"
    ledger.append(Event(
        project=project, kind="job.opened", actor_kind="owner",
        actor_id="owner", attempt_id=attempt_id,
        payload={
            "job_id": f"J-{project}-TEST", "job_name": "job",
            "attempt_id": attempt_id, "n": 1, "kind": "full",
            "generation": generation, "dirty_paths": [],
        },
    ))
    transition(ledger, project, "open_job", "owner")
    return attempt_id


def _make_copy(workspace: Workspace, project_dir: Path, name: str,
                attempt_id: str = "J-alpha-UNIT#1", role: str = "builder") -> Path:
    # Real worker copies live at config.home/"copies"/{attempt_id}/{role}
    # (engine.steps.open_job.copy_root) — use that layout here too, so
    # these tests exercise the same protected-path logic production code
    # hits (a copy made elsewhere, e.g. under workspace.root, would never
    # even reach the home/"copies" rule).
    dest = production_copy_root(workspace.config, f"{attempt_id}-{name}", role)
    shutil.copytree(project_dir, dest)
    return dest


def test_rule1_read_allowed_for_worker(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    copy_root = _make_copy(workspace, workspace.alpha, "r1")
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=0,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(tool="read", path=copy_root / "src/app.py"))
    assert decision.allowed is True


def test_rule1_read_denied_for_secrets(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    copy_root = _make_copy(workspace, workspace.alpha, "r1b")
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=0,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(
        tool="read", path=workspace.config.secrets_dir / "prod.key"))
    assert decision.allowed is False


def test_rule2_write_denied_when_not_open(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    copy_root = _make_copy(workspace, workspace.alpha, "r2")
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(
        tool="write", path=copy_root / "src/new.py"))
    assert decision.allowed is False
    assert "not_open" in decision.reason


def test_closed_state_write_denied_even_without_hooks(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    assert workspace.ledger.project("alpha").state == "CLOSED"
    copy_root = _make_copy(workspace, workspace.alpha, "r2b")
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(
        tool="terminal", path=copy_root / "src", target="pytest"))
    assert decision.allowed is False


def test_rule3_old_generation_worker_denied(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    _open_project(workspace.ledger, "alpha", generation=1)
    # a second job.opened would bump generation to 2 in a real run; simulate
    # a stale worker holding generation 0 against the current generation 1.
    copy_root = _make_copy(workspace, workspace.alpha, "r3")
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=0,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(
        tool="write", path=copy_root / "src/new.py"))
    assert decision.allowed is False
    assert decision.reason == "stale_generation"


def test_rule4_protected_path_denied(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    _open_project(workspace.ledger, "alpha", generation=1)
    copy_root = _make_copy(workspace, workspace.alpha, "r4")
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(
        tool="write", path=copy_root / ".git" / "config"))
    assert decision.allowed is False
    assert decision.reason == "protected_path"


def test_rule4_protected_paths_from_config(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    _open_project(workspace.ledger, "alpha", generation=1)
    copy_root = _make_copy(workspace, workspace.alpha, "r4b")
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(
        tool="write", path=copy_root / ".gitignore"))
    assert decision.allowed is False
    assert decision.reason == "protected_path"


def test_rule5_write_within_station_prefix_allowed(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    _open_project(workspace.ledger, "alpha", generation=1)
    copy_root = _make_copy(workspace, workspace.alpha, "r5")
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(
        tool="write", path=copy_root / "src/new.py"))
    assert decision.allowed is True


def test_rule5_write_outside_station_prefix_denied(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    _open_project(workspace.ledger, "alpha", generation=1)
    copy_root = _make_copy(workspace, workspace.alpha, "r5b")
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(
        tool="write", path=copy_root / "tests/bench/cases.json"))
    assert decision.allowed is False


def test_rule5_unknown_station_role_denied(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    _open_project(workspace.ledger, "alpha", generation=1)
    copy_root = _make_copy(workspace, workspace.alpha, "r5c")
    actor = Actor(kind="worker", role="reviewer", attempt_id=None, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(
        tool="write", path=copy_root / "src/new.py"))
    assert decision.allowed is False
    assert decision.reason == "station_role_not_authorized"


def test_rule6_worker_commit_denied_always(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    _open_project(workspace.ledger, "alpha", generation=1)
    copy_root = _make_copy(workspace, workspace.alpha, "r6")
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(tool="commit", target="alpha"))
    assert decision.allowed is False
    assert decision.reason == "worker_forbidden_tool"


def test_owner_and_processor_always_allowed(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    owner = Actor(kind="owner", role="owner", attempt_id=None, generation=0)
    decision = policy.check("alpha", owner, Action(tool="publish", target="prod"))
    assert decision.allowed is True
    processor = Actor(kind="processor", role="processor", attempt_id=None, generation=0)
    decision2 = policy.check("alpha", processor, Action(
        tool="write", path=workspace.alpha / "src/app.py"))
    assert decision2.allowed is True


def test_symlink_outside_tree_denied(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    _open_project(workspace.ledger, "alpha", generation=1)
    copy_root = _make_copy(workspace, workspace.alpha, "r_sym")
    link_path = copy_root / "src" / "escape.py"
    os.symlink(workspace.config.secrets_dir / "prod.key", link_path)
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(tool="write", path=link_path))
    assert decision.allowed is False


def test_decisions_are_recorded(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    before = len(workspace.ledger.events("alpha", "policy.decided"))
    copy_root = _make_copy(workspace, workspace.alpha, "r_log")
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=0,
                  station="build", copy_root=copy_root)
    policy.check("alpha", actor, Action(tool="read", path=copy_root / "src/app.py"))
    policy.check("alpha", actor, Action(tool="write", path=copy_root / "src/new.py"))
    after = len(workspace.ledger.events("alpha", "policy.decided"))
    assert after == before + 2


def test_symlink_directory_swap_escapes_denied(workspace: Workspace) -> None:
    """P0-3: a worker who replaces its copy's station-prefix directory
    (src/) with a symlink into the real project repo must not get writes
    allowed into the repo. Before the fix, _within_allowed_prefix resolved
    copy_root / prefix with os.path.realpath, which followed exactly this
    symlink and compared the write target against the escaped (repo-side)
    real path, allowing it."""
    policy = Policy(workspace.config, workspace.ledger)
    attempt_id = _open_project(workspace.ledger, "alpha", generation=1)
    copy_root = _make_copy(workspace, workspace.alpha, "r_dirswap")
    shutil.rmtree(copy_root / "src")
    os.symlink(workspace.alpha / "src", copy_root / "src")
    actor = Actor(kind="worker", role="builder", attempt_id=attempt_id, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(
        tool="write", path=copy_root / "src" / "app.py"))
    assert decision.allowed is False


def test_symlink_file_inside_allowed_tree_escapes_denied(workspace: Workspace) -> None:
    """P0-3: a plain file inside an allowed prefix that is itself a
    symlink into the real project repo must not get writes allowed."""
    policy = Policy(workspace.config, workspace.ledger)
    attempt_id = _open_project(workspace.ledger, "alpha", generation=1)
    copy_root = _make_copy(workspace, workspace.alpha, "r_fileswap")
    target = copy_root / "src" / "app.py"
    target.unlink()
    os.symlink(workspace.alpha / "src" / "app.py", target)
    actor = Actor(kind="worker", role="builder", attempt_id=attempt_id, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(tool="write", path=target))
    assert decision.allowed is False


def test_worker_own_copy_under_home_copies_is_not_protected(workspace: Workspace) -> None:
    """P1-1: real worker copies live at config.home/"copies"/{attempt_id}/
    {role}. Before the fix, is_protected() protected config.home as a
    whole, so every write by a real worker into its own copy was denied."""
    policy = Policy(workspace.config, workspace.ledger)
    attempt_id = _open_project(workspace.ledger, "alpha", generation=1)
    copy_root = _make_copy(workspace, workspace.alpha, "r_ownhome")
    assert workspace.config.home in copy_root.parents
    actor = Actor(kind="worker", role="builder", attempt_id=attempt_id, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(
        tool="write", path=copy_root / "src" / "new.py"))
    assert decision.allowed is True


def test_worker_cannot_write_into_another_roles_copy(workspace: Workspace) -> None:
    """P1-1: home/"copies" stays protected for every copy that is not the
    asking actor's own copy_root — a builder cannot reach into a
    reviewer's (or another attempt's) copy under the same home/copies
    tree, even though both live under config.home."""
    policy = Policy(workspace.config, workspace.ledger)
    attempt_id = _open_project(workspace.ledger, "alpha", generation=1)
    builder_copy = _make_copy(workspace, workspace.alpha, "r_otherrole_builder",
                               attempt_id=attempt_id, role="builder")
    other_copy = _make_copy(workspace, workspace.alpha, "r_otherrole_reviewer",
                             attempt_id=attempt_id, role="reviewer")
    actor = Actor(kind="worker", role="builder", attempt_id=attempt_id, generation=1,
                  station="build", copy_root=builder_copy)
    decision = policy.check("alpha", actor, Action(
        tool="write", path=other_copy / "src" / "app.py"))
    assert decision.allowed is False
    assert decision.reason == "protected_path"

    read_decision = policy.check("alpha", actor, Action(
        tool="read", path=other_copy / "src" / "app.py"))
    assert read_decision.allowed is False


def test_unregistered_project_denied_for_every_tool_no_ledger_error(
    workspace: Workspace,
) -> None:
    """P2-5: an unknown project must be denied immediately, including for
    read and for owner/processor, without ever reaching
    self.ledger.project() (which raises LedgerError for an unregistered
    project)."""
    policy = Policy(workspace.config, workspace.ledger)
    worker = Actor(kind="worker", role="builder", attempt_id=None, generation=0,
                   station="build", copy_root=workspace.root / "nope")
    owner = Actor(kind="owner", role="owner", attempt_id=None, generation=0)

    for actor, tool in ((worker, "read"), (worker, "write"), (owner, "read")):
        decision = policy.check("ghost-project", actor, Action(
            tool=tool, path=workspace.root / "nope" / "x.py"))
        assert decision.allowed is False
        assert decision.reason == "unregistered_project"


def test_gitignore_always_protected_even_without_config_entry(
    workspace: Workspace,
) -> None:
    """P2-6: .gitignore at any depth is protected for workers regardless
    of config.protected_paths (conftest's workspace already lists
    ".gitignore" there — this proves it would still be denied even if it
    were removed from that list)."""
    from dataclasses import replace

    stripped = replace(workspace.config, protected_paths=())
    policy = Policy(stripped, workspace.ledger)
    attempt_id = _open_project(workspace.ledger, "alpha", generation=1)
    copy_root = _make_copy(workspace, workspace.alpha, "r_gitignore_nocfg")
    actor = Actor(kind="worker", role="builder", attempt_id=attempt_id, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(
        tool="write", path=copy_root / ".gitignore"))
    assert decision.allowed is False
    assert decision.reason == "protected_path"

    nested = copy_root / "src" / ".gitignore"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text("x\n", encoding="utf-8")
    nested_decision = policy.check("alpha", actor, Action(tool="write", path=nested))
    assert nested_decision.allowed is False


def test_worker_read_of_ledger_wal_companion_denied(workspace: Workspace) -> None:
    """SQLite leaves ledger.sqlite-wal/-shm/-journal next to the
    ledger file in WAL mode; approval/evidence pages can live in those
    companion files, so a worker read of them must be denied just like a
    read of the ledger file itself."""
    policy = Policy(workspace.config, workspace.ledger)
    copy_root = _make_copy(workspace, workspace.alpha, "r_ledger_wal")
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=0,
                  station="build", copy_root=copy_root)

    wal_path = workspace.config.ledger_path.with_name(
        workspace.config.ledger_path.name + "-wal")
    wal_path.write_text("fake wal content", encoding="utf-8")
    decision = policy.check("alpha", actor, Action(tool="read", path=wal_path))
    assert decision.allowed is False


def _terminal_workspace_config(workspace: Workspace, patterns: tuple[str, ...]):
    from dataclasses import replace
    table = dict(workspace.config.station_terminal_allow)
    table["build"] = {"builder": patterns}
    return replace(workspace.config, station_terminal_allow=table)


def test_terminal_allowed_by_fullmatch(workspace: Workspace) -> None:
    config = _terminal_workspace_config(workspace, (r"pytest( -q)?",))
    policy = Policy(config, workspace.ledger)
    attempt_id = _open_project(workspace.ledger, "alpha", generation=1)
    copy_root = _make_copy(workspace, workspace.alpha, "t1", attempt_id=attempt_id)
    actor = Actor(kind="worker", role="builder", attempt_id=attempt_id, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(tool="terminal", command="pytest -q"))
    assert decision.allowed is True
    assert decision.reason == "terminal_pattern"


def test_terminal_denied_partial_match(workspace: Workspace) -> None:
    config = _terminal_workspace_config(workspace, (r"pytest( -q)?",))
    policy = Policy(config, workspace.ledger)
    attempt_id = _open_project(workspace.ledger, "alpha", generation=1)
    copy_root = _make_copy(workspace, workspace.alpha, "t2", attempt_id=attempt_id)
    actor = Actor(kind="worker", role="builder", attempt_id=attempt_id, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(
        tool="terminal", command="pytest; rm -rf /"))
    assert decision.allowed is False
    assert decision.reason == "shell_metachar"


@pytest.mark.parametrize("metachar", [";", "&", "|", "`", "$", "\n", ">", "<"])
def test_terminal_denied_each_metachar(workspace: Workspace, metachar: str) -> None:
    config = _terminal_workspace_config(workspace, (r".*",))
    policy = Policy(config, workspace.ledger)
    attempt_id = _open_project(workspace.ledger, "alpha", generation=1)
    copy_root = _make_copy(workspace, workspace.alpha, f"t3-{ord(metachar[0])}",
                            attempt_id=attempt_id)
    actor = Actor(kind="worker", role="builder", attempt_id=attempt_id, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(
        tool="terminal", command=f"echo x{metachar}y"))
    assert decision.allowed is False
    assert decision.reason == "shell_metachar"


def test_terminal_denied_without_table(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    attempt_id = _open_project(workspace.ledger, "alpha", generation=1)
    copy_root = _make_copy(workspace, workspace.alpha, "t4", attempt_id=attempt_id)
    actor = Actor(kind="worker", role="builder", attempt_id=attempt_id, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(tool="terminal", command="pytest"))
    assert decision.allowed is False
    assert decision.reason == "terminal_not_authorized"


def test_terminal_requires_command(workspace: Workspace) -> None:
    config = _terminal_workspace_config(workspace, (r"pytest",))
    policy = Policy(config, workspace.ledger)
    attempt_id = _open_project(workspace.ledger, "alpha", generation=1)
    copy_root = _make_copy(workspace, workspace.alpha, "t5", attempt_id=attempt_id)
    actor = Actor(kind="worker", role="builder", attempt_id=attempt_id, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(tool="terminal", command=None))
    assert decision.allowed is False
    assert decision.reason == "terminal_requires_command"

    decision2 = policy.check("alpha", actor, Action(tool="terminal", command=""))
    assert decision2.allowed is False
    assert decision2.reason == "terminal_requires_command"


def test_terminal_denied_home_reference(workspace: Workspace) -> None:
    config = _terminal_workspace_config(workspace, (r".*",))
    policy = Policy(config, workspace.ledger)
    attempt_id = _open_project(workspace.ledger, "alpha", generation=1)
    copy_root = _make_copy(workspace, workspace.alpha, "t6", attempt_id=attempt_id)
    actor = Actor(kind="worker", role="builder", attempt_id=attempt_id, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(tool="terminal", command="cat ~/.ssh/id_rsa"))
    assert decision.allowed is False
    assert decision.reason == "home_reference"


def test_terminal_denied_path_traversal(workspace: Workspace) -> None:
    config = _terminal_workspace_config(workspace, (r".*",))
    policy = Policy(config, workspace.ledger)
    attempt_id = _open_project(workspace.ledger, "alpha", generation=1)
    copy_root = _make_copy(workspace, workspace.alpha, "t7", attempt_id=attempt_id)
    actor = Actor(kind="worker", role="builder", attempt_id=attempt_id, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(tool="terminal", command="cat ../../etc/passwd"))
    assert decision.allowed is False
    assert decision.reason == "path_traversal"


def test_worker_read_outside_copy_denied(workspace: Workspace) -> None:
    """P0: a worker is a real process that inherits the operator's HOME --
    reading anywhere outside its own copy (e.g. a path that happens to sit
    under something that looks like the operator's home) must be denied,
    not just the already-protected sensitive areas."""
    policy = Policy(workspace.config, workspace.ledger)
    copy_root = _make_copy(workspace, workspace.alpha, "r_outside")
    fake_home = workspace.root / "owner-home"
    fake_home.mkdir()
    (fake_home / "secret.txt").write_text("nope", encoding="utf-8")
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=0,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(
        tool="read", path=fake_home / "secret.txt"))
    assert decision.allowed is False
    assert decision.reason == "read_outside_copy"


def test_worker_read_inside_copy_allowed(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    copy_root = _make_copy(workspace, workspace.alpha, "r_inside")
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=0,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(
        tool="read", path=copy_root / "src" / "app.py"))
    assert decision.allowed is True
    assert decision.reason == "read_within_copy"


def test_worker_read_without_copy_root_denied(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=0,
                  station="build", copy_root=None)
    decision = policy.check("alpha", actor, Action(
        tool="read", path=workspace.alpha / "src" / "app.py"))
    assert decision.allowed is False
    assert decision.reason == "no_copy_root"


def test_worker_read_within_reference_snapshot_allowed(workspace: Workspace) -> None:
    """Fix: Read/Glob/Grep (all tool="read") inside THIS
    attempt's own reference-folder snapshot (engine.domain.ids.
    references_root, engine.infrastructure.reference_snapshot) must be
    allowed, even though it is outside the worker's own copy_root --
    the whole point of mounting the snapshot read-only is for a
    worker/reviewer to actually be able to read it."""
    from engine.ids import attempt_dirname

    policy = Policy(workspace.config, workspace.ledger)
    copy_root = _make_copy(workspace, workspace.alpha, "r_ref_snapshot")
    attempt_id = "J-alpha-REFSNAP#1"
    snapshot_dir = (workspace.config.home / "references" / attempt_dirname(attempt_id)
                    / "0-material")
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "brief.md").write_text("BRIEF\n", encoding="utf-8")
    actor = Actor(kind="worker", role="builder", attempt_id=attempt_id, generation=0,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(
        tool="read", path=snapshot_dir / "brief.md"))
    assert decision.allowed is True
    assert decision.reason == "read_within_reference_snapshot"


def test_worker_write_within_reference_snapshot_still_denied(workspace: Workspace) -> None:
    """The snapshot is read-only, period -- the Read-side allowance
    above must never widen rule 5's write check, which never looks at
    the reference snapshot at all (it only ever allows a write under
    the worker's own copy_root at a configured station_owners prefix)."""
    policy = Policy(workspace.config, workspace.ledger)
    copy_root = _make_copy(workspace, workspace.alpha, "w_ref_snapshot")
    _open_project(workspace.ledger, "alpha")
    attempt_id = "J-alpha-REFSNAP2#1"
    from engine.ids import attempt_dirname

    snapshot_dir = (workspace.config.home / "references" / attempt_dirname(attempt_id)
                    / "0-material")
    snapshot_dir.mkdir(parents=True)
    actor = Actor(kind="worker", role="builder", attempt_id=attempt_id, generation=1,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(
        tool="write", path=snapshot_dir / "new-file.txt"))
    assert decision.allowed is False
    assert decision.reason == "outside_allowed_path"


def test_worker_read_without_path_denied(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    copy_root = _make_copy(workspace, workspace.alpha, "r_nopath")
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=0,
                  station="build", copy_root=copy_root)
    decision = policy.check("alpha", actor, Action(tool="read", path=None))
    assert decision.allowed is False
    assert decision.reason == "read_requires_path"


def test_owner_read_of_ledger_wal_companion_allowed(workspace: Workspace) -> None:
    policy = Policy(workspace.config, workspace.ledger)
    wal_path = workspace.config.ledger_path.with_name(
        workspace.config.ledger_path.name + "-wal")
    wal_path.write_text("fake wal content", encoding="utf-8")
    owner = Actor(kind="owner", role="owner", attempt_id=None, generation=0)
    decision = policy.check("alpha", owner, Action(tool="read", path=wal_path))
    assert decision.allowed is True


# ---- Policy(facts=...) gives the same decisions as Policy(ledger) -------

class _CollectingSink:
    """A DecisionSink that just remembers every Event -- used to compare
    Policy's facts-driven path against its ledger-driven one without a
    real ledger write on the facts side."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def record(self, event: Event) -> None:
        self.events.append(event)


def _facts_from_ledger(ledger: Ledger, project: str) -> ProjectFacts:
    row = ledger.project(project)
    return ProjectFacts(
        project=project, registered=True, state=row.state,
        generation=row.generation, attempt_id=row.current_attempt_id,
        attempt_active=row.current_attempt_id is not None,
    )


def _decide_with_ledger_and_facts(
    workspace: Workspace, actor: Actor, action: Action,
) -> tuple[object, object]:
    ledger_sink = _CollectingSink()
    ledger_decision = Policy(workspace.config, workspace.ledger, sink=ledger_sink).check(
        "alpha", actor, action)
    facts = _facts_from_ledger(workspace.ledger, "alpha")
    facts_sink = _CollectingSink()
    facts_decision = Policy(workspace.config, facts=facts, sink=facts_sink).check(
        "alpha", actor, action)
    assert ledger_sink.events[0].payload == facts_sink.events[0].payload
    return ledger_decision, facts_decision


def test_policy_facts_matches_ledger_for_allowed_write(workspace: Workspace) -> None:
    _open_project(workspace.ledger, "alpha")
    copy_root = _make_copy(workspace, workspace.alpha, "facts-vs-ledger-allow")
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=1,
                  station="build", copy_root=copy_root)
    action = Action(tool="write", path=copy_root / "src" / "new.py")
    ledger_decision, facts_decision = _decide_with_ledger_and_facts(workspace, actor, action)
    assert ledger_decision == facts_decision == Decision(True, "within_allowed_path")


def test_policy_facts_matches_ledger_for_project_not_open(workspace: Workspace) -> None:
    # No _open_project() call -- a freshly registered project is CLOSED.
    copy_root = _make_copy(workspace, workspace.alpha, "facts-vs-ledger-closed")
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=0,
                  station="build", copy_root=copy_root)
    action = Action(tool="write", path=copy_root / "src" / "new.py")
    ledger_decision, facts_decision = _decide_with_ledger_and_facts(workspace, actor, action)
    assert ledger_decision == facts_decision == Decision(False, "project_not_open:CLOSED")


def test_policy_facts_matches_ledger_for_stale_generation(workspace: Workspace) -> None:
    _open_project(workspace.ledger, "alpha", generation=5)
    copy_root = _make_copy(workspace, workspace.alpha, "facts-vs-ledger-stale")
    actor = Actor(kind="worker", role="builder", attempt_id=None, generation=1,
                  station="build", copy_root=copy_root)
    action = Action(tool="write", path=copy_root / "src" / "new.py")
    ledger_decision, facts_decision = _decide_with_ledger_and_facts(workspace, actor, action)
    assert ledger_decision == facts_decision == Decision(False, "stale_generation")


def test_policy_requires_ledger_or_explicit_sink() -> None:
    from engine.config import EngineConfig
    config = EngineConfig(
        ledger_path=Path("/nonexistent/ledger.sqlite"), home=Path("/nonexistent/home"),
        secrets_dir=Path("/nonexistent/secrets"), projects={}, station_owners={},
    )
    facts = ProjectFacts(project="alpha", registered=True, state="OPEN",
                         generation=1, attempt_id=None, attempt_active=False)
    with pytest.raises(ValueError):
        Policy(config, facts=facts)
