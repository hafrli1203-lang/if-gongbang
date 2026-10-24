"""Every engine function that builds a filesystem path FROM an
attempt_id must route it through engine.ids.attempt_dirname -- a
literal '#' in a path breaks tools that resolve config via file URLs
(Vite/Vitest: "working directory path contains a literal '#'").
This exercises each such function directly with a
realistic attempt_id (containing '#') and asserts none of the
resulting paths contain '#' anywhere."""
from __future__ import annotations

from types import SimpleNamespace

from engine.cli import _build_fakes
from engine.ids import new_job_id, attempt_id as make_attempt_id
from engine.steps import bench_run, close, install, review, station
from engine.steps.open_job import copy_root
from tests.conftest import Workspace


def _ctx(workspace: Workspace, attempt_id: str) -> SimpleNamespace:
    return SimpleNamespace(config=workspace.config, project="alpha",
                           attempt_id=attempt_id)


def test_no_path_building_function_leaves_a_hash_in_the_path(
    workspace: Workspace,
) -> None:
    attempt_id = make_attempt_id(new_job_id("alpha"), 1)
    assert "#" in attempt_id  # the input genuinely has one, same as production

    ctx = _ctx(workspace, attempt_id)
    paths = [
        copy_root(workspace.config, attempt_id, "builder"),
        station._stations_dir(ctx),
        review._stations_dir(ctx),
        bench_run._bench_home_dir(ctx),
        close._archive_root(ctx),
        install._snapshot_dir(ctx),
    ]
    for path in paths:
        assert "#" not in str(path), path

    fakes = _build_fakes(workspace.config, "alpha", attempt_id, None)
    assert "#" not in str(fakes.installer.marker_dir), fakes.installer.marker_dir
