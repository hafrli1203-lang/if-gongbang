"""Unit tests for engine.steps.open_job: attempt creation, copies, dirty
paths, and the CLOSED-state guard. Uses the two-repo `workspace` fixture."""
from __future__ import annotations

import pytest

from engine.ledger import Event
from engine.processor import submit
from engine.state import TransitionError, transition
from engine.steps.open_job import copy_root, open_job
from tests.conftest import Workspace, git


def test_open_creates_attempt_and_copies(workspace: Workspace) -> None:
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha",
                           "feature-x", "full", ("builder",))
    assert workspace.ledger.project("alpha").state == "OPEN"
    assert workspace.ledger.project("alpha").current_attempt_id == attempt_id
    copy = copy_root(workspace.config, attempt_id, "builder")
    assert (copy / "src" / "app.py").read_text(encoding="utf-8") == "print('alpha')\n"
    assert (copy / ".git").exists()  # own git dir, protected-path tests rely on this


def test_reopen_same_job_name_increments_n(workspace: Workspace) -> None:
    attempt_1 = open_job(workspace.config, workspace.ledger, "alpha",
                          "feature-x", "full", ("builder",))
    # simulate the processor finishing the close sequence for attempt_1
    workspace.ledger.append(Event(
        project="alpha", kind="attempt.finished", actor_kind="processor",
        actor_id="processor", attempt_id=attempt_1,
        payload={"attempt_id": attempt_1, "status": "closed"},
    ))
    transition(workspace.ledger, "alpha", "close_job", "owner")
    transition(workspace.ledger, "alpha", "finalized", "processor")
    assert workspace.ledger.project("alpha").state == "CLOSED"

    attempt_2 = open_job(workspace.config, workspace.ledger, "alpha",
                          "feature-x", "full", ("builder",))
    job_id = attempt_1.split("#")[0]
    assert attempt_1 == f"{job_id}#1"
    assert attempt_2 == f"{job_id}#2"


def test_dirty_paths_recorded(workspace: Workspace) -> None:
    (workspace.alpha / "docs" / "readme.md").write_text("changed\n", encoding="utf-8")
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha",
                           "job-with-dirty", "full", ("builder",))
    assert workspace.ledger.project("alpha").dirty_paths == ("docs/readme.md",)
    # the copy is made from HEAD, so the user's uncommitted edit is not in it
    copy = copy_root(workspace.config, attempt_id, "builder")
    assert (copy / "docs" / "readme.md").read_text(encoding="utf-8") == "alpha docs\n"


def test_open_refused_when_not_closed(workspace: Workspace) -> None:
    open_job(workspace.config, workspace.ledger, "alpha", "job-a", "full", ("builder",))
    with pytest.raises(TransitionError):
        open_job(workspace.config, workspace.ledger, "alpha", "job-b", "full", ("builder",))


def test_base_head_recorded_on_job_opened(workspace: Workspace) -> None:
    import subprocess

    head = subprocess.run(
        ["git", "-C", str(workspace.alpha), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha",
                           "job-head", "full", ("builder",))
    events = workspace.ledger.events("alpha", "job.opened")
    opened = [e for _seq, e in events if e.payload["attempt_id"] == attempt_id][0]
    assert opened.payload["base_head"] == head


def test_open_refused_when_queued_request_leaves_no_attempt(workspace: Workspace) -> None:
    """E4: validate (state CLOSED, no queued requests, no unknown requests)
    BEFORE appending job.opened. A corrupted/edge ledger state where the
    project is CLOSED while a request is still queued must refuse open_job
    without creating a new attempt row or advancing generation — the old
    code appended job.opened (creating the attempt, bumping generation,
    updating current_attempt_id) and only then discovered the refusal via
    state.transition(), leaving that stray attempt behind."""
    attempt_1 = open_job(workspace.config, workspace.ledger, "alpha",
                          "job-queued", "full", ("builder",))
    rid = submit(workspace.ledger, "alpha", attempt_1, "commit",
                 {"message": "x"}, "owner")
    assert workspace.ledger.request(rid).status == "queued"

    before = workspace.ledger.project("alpha")
    # Simulate a project forced to CLOSED (e.g. an operator override, or a
    # bug elsewhere) while the request above is still queued.
    workspace.ledger.append(Event(
        project="alpha", kind="state.changed", actor_kind="owner",
        actor_id="test", payload={"state": "CLOSED", "input": "test_force",
                                   "prev": "OPEN"},
    ))
    assert workspace.ledger.project("alpha").state == "CLOSED"

    with pytest.raises(TransitionError):
        open_job(workspace.config, workspace.ledger, "alpha", "job-b",
                  "full", ("builder",))

    after = workspace.ledger.project("alpha")
    assert after.current_attempt_id == attempt_1 == before.current_attempt_id
    assert after.generation == before.generation
    assert workspace.ledger.attempts_for_job_name("alpha", "job-b") == []


def test_dirty_paths_nonascii_filename(workspace: Workspace) -> None:
    """N5: `git status --porcelain` (no -z) git-quotes non-ASCII filenames
    as octal escapes (e.g. "src/\\355\\225\\234.py"), which don't match any
    real path and break close's literal-path handling. `-z` must be used so
    the filename comes back as the literal UTF-8 bytes it actually is."""
    name = "새파일.txt"
    (workspace.alpha / name).write_text("x\n", encoding="utf-8")
    open_job(workspace.config, workspace.ledger, "alpha", "job-korean",
             "full", ("builder",))
    assert workspace.ledger.project("alpha").dirty_paths == (name,)


def test_dirty_paths_rename_records_both_paths(workspace: Workspace) -> None:
    """N5: a staged rename must contribute BOTH the old and the new path to
    dirty_paths (close must know the old path is gone and the new path is
    the user's pre-existing change)."""
    git(workspace.alpha, "mv", "docs/readme.md", "docs/renamed.md")
    open_job(workspace.config, workspace.ledger, "alpha", "job-rename",
             "full", ("builder",))
    dirty = workspace.ledger.project("alpha").dirty_paths
    assert "docs/readme.md" in dirty
    assert "docs/renamed.md" in dirty


def test_copy_creation_failure_leaves_no_attempt_and_closed_state(
    workspace: Workspace,
) -> None:
    """#23 remainder: copies are now built (into a .tmp- staging dir, then
    renamed into place) BEFORE any ledger write. If copy creation fails,
    nothing must be committed — no attempt row, generation unchanged, state
    stays CLOSED. The old code appended job.opened and transitioned to OPEN
    first, then made copies after, so a copy failure left OPEN with broken
    copies and no way to retry via open_job again (state no longer CLOSED)."""
    before = workspace.ledger.project("alpha")
    copies_path = workspace.config.home / "copies"
    copies_path.write_text("not a directory\n", encoding="utf-8")  # blocks mkdir

    with pytest.raises(OSError):
        open_job(workspace.config, workspace.ledger, "alpha", "job-copyfail",
                  "full", ("builder",))

    after = workspace.ledger.project("alpha")
    assert after.state == "CLOSED"
    assert after.current_attempt_id == before.current_attempt_id
    assert after.generation == before.generation
    assert workspace.ledger.attempts_for_job_name("alpha", "job-copyfail") == []


def test_copy_links_symlinks_gitignored_dir_into_copy(workspace: Workspace) -> None:
    """node_modules is gitignored (tests/conftest.make_repo writes
    "node_modules/\\ndist/\\n" into every fixture repo's .gitignore), so
    `git archive` never puts it in the copy. copy_links symlinks it in
    directly so a Bash-run test_cmd/install_cmd *inside the copy* can
    resolve it -- `git status` in the copy stays clean either way (the
    symlink, if not ignored, becomes part of the copy's own base commit;
    if ignored, it is invisible to porcelain status like any other
    ignored path)."""
    from dataclasses import replace
    import os
    import subprocess

    (workspace.alpha / "node_modules" / "pkg").mkdir(parents=True)
    (workspace.alpha / "node_modules" / "pkg" / "index.js").write_text(
        "module.exports = 1;\n", encoding="utf-8")

    project_config = replace(workspace.config.projects["alpha"],
                             copy_links=("node_modules",))
    config = replace(workspace.config,
                     projects={**workspace.config.projects, "alpha": project_config},
                     sandbox="bwrap")
    attempt_id = open_job(config, workspace.ledger, "alpha", "copy-links-ignored",
                          "full", ("builder",))
    copy = copy_root(config, attempt_id, "builder")
    link = copy / "node_modules"
    assert link.is_symlink()
    assert os.path.realpath(link) == os.path.realpath(workspace.alpha / "node_modules")
    assert (link / "pkg" / "index.js").read_text(encoding="utf-8") == "module.exports = 1;\n"

    status = subprocess.run(["git", "-C", str(copy), "status", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout
    assert status == ""  # ignored by the copy's own .gitignore, same as node_modules/


def test_copy_links_untracked_but_not_ignored_entry_is_committed_into_base(
    workspace: Workspace,
) -> None:
    """An entry copy_links names that the repo's own .gitignore does NOT
    cover is symlinked in and then swept up by the base commit's
    `git add -A` (git follows the repo's gitignore rules for what it
    skips, same as any other path) -- `git status` is still clean right
    after _make_copy."""
    from dataclasses import replace
    import subprocess

    (workspace.alpha / "vendor_assets").mkdir()
    (workspace.alpha / "vendor_assets" / "logo.svg").write_text("<svg/>\n", encoding="utf-8")

    project_config = replace(workspace.config.projects["alpha"],
                             copy_links=("vendor_assets",))
    config = replace(workspace.config,
                     projects={**workspace.config.projects, "alpha": project_config},
                     sandbox="bwrap")
    attempt_id = open_job(config, workspace.ledger, "alpha", "copy-links-tracked",
                          "full", ("builder",))
    copy = copy_root(config, attempt_id, "builder")
    link = copy / "vendor_assets"
    assert link.is_symlink()

    tracked = subprocess.run(["git", "-C", str(copy), "ls-files"],
                             capture_output=True, text=True, check=True).stdout.splitlines()
    assert "vendor_assets" in tracked  # the symlink itself, one tracked entry

    status = subprocess.run(["git", "-C", str(copy), "status", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout
    assert status == ""


def test_copy_links_missing_entry_is_skipped(workspace: Workspace) -> None:
    """copy_links naming a path that does not exist in the repo is simply
    skipped -- no symlink, no error."""
    from dataclasses import replace

    project_config = replace(workspace.config.projects["alpha"],
                             copy_links=("does_not_exist",))
    config = replace(workspace.config,
                     projects={**workspace.config.projects, "alpha": project_config},
                     sandbox="bwrap")
    attempt_id = open_job(config, workspace.ledger, "alpha", "copy-links-missing",
                          "full", ("builder",))
    copy = copy_root(config, attempt_id, "builder")
    assert not (copy / "does_not_exist").exists()


def test_copy_link_writable_subpaths_splits_entry_into_per_child_symlinks(
    workspace: Workspace,
) -> None:
    """A copy_links entry with configured copy_link_writable_subpaths
    (e.g. node_modules needing .vite-temp writable) becomes a REAL
    directory in the copy, not a single whole-dir symlink: each of the
    repo's own children is individually symlinked in, and each
    configured subpath is a real, empty, writable directory that is
    NEVER symlinked to the repo -- a write under it can never reach
    the real repo's node_modules, and `git status` in the copy stays
    clean (the real directory + its symlinks + the empty writable dir
    are all committed into the copy's own base, same as any other
    path)."""
    import subprocess
    from dataclasses import replace

    (workspace.alpha / "node_modules" / "pkg").mkdir(parents=True)
    (workspace.alpha / "node_modules" / "pkg" / "index.js").write_text(
        "module.exports = 1;\n", encoding="utf-8")

    project_config = replace(
        workspace.config.projects["alpha"],
        copy_links=("node_modules",),
        copy_link_writable_subpaths={"node_modules": (".vite-temp",)},
    )
    config = replace(workspace.config,
                     projects={**workspace.config.projects, "alpha": project_config},
                     sandbox="bwrap")
    attempt_id = open_job(config, workspace.ledger, "alpha", "copy-link-split",
                          "full", ("builder",))
    copy = copy_root(config, attempt_id, "builder")
    node_modules = copy / "node_modules"

    assert node_modules.is_dir() and not node_modules.is_symlink()
    assert (node_modules / "pkg").is_symlink()
    assert (node_modules / "pkg" / "index.js").read_text(encoding="utf-8") \
        == "module.exports = 1;\n"
    vite_temp = node_modules / ".vite-temp"
    assert vite_temp.is_dir() and not vite_temp.is_symlink()

    status = subprocess.run(["git", "-C", str(copy), "status", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout
    assert status == ""


def test_copy_links_source_escaping_repo_via_symlink_is_skipped_not_followed(
    workspace: Workspace,
) -> None:
    """A copy_links entry whose source resolves, through a symlink
    somewhere in the repo, to outside the repo entirely must be
    skipped (journaled on job.opened as copy_links_skipped) rather
    than linked -- following it would make the created symlink in the
    copy point at arbitrary host paths."""
    from dataclasses import replace

    outside = workspace.root / "outside_shared"
    outside.mkdir()
    (outside / "pkg.txt").write_text("outside\n", encoding="utf-8")
    (workspace.alpha / "escape_link").symlink_to(outside)

    project_config = replace(workspace.config.projects["alpha"],
                             copy_links=("escape_link",))
    config = replace(workspace.config,
                     projects={**workspace.config.projects, "alpha": project_config},
                     sandbox="bwrap")
    attempt_id = open_job(config, workspace.ledger, "alpha", "copy-links-escape",
                          "full", ("builder",))
    copy = copy_root(config, attempt_id, "builder")
    assert not (copy / "escape_link").exists()

    opened = [e for _seq, e in workspace.ledger.events("alpha", "job.opened")
             if e.payload.get("attempt_id") == attempt_id][0]
    assert opened.payload["copy_links_skipped"] == [
        {"role": "builder", "entry": "escape_link",
         "reason": "copy_link_source_escapes_repo"},
    ]


def test_copy_links_entry_under_an_earlier_links_symlink_is_skipped(
    workspace: Workspace,
) -> None:
    """Two copy_links entries, "deps" (a symlink the repo itself tracks,
    resolving to another WITHIN-repo directory -- safe on its own, and
    linked first) and "deps/pkg" (also resolves within the repo on its
    own): processing "deps" first makes `copy/deps` a symlink, so
    "deps/pkg" must be skipped rather than mkdir/symlink_to'd through
    it -- that would physically create the entry inside the real repo
    directory "deps" resolves to, not inside the copy at all."""
    from dataclasses import replace

    (workspace.alpha / "vendor_assets" / "pkg").mkdir(parents=True)
    (workspace.alpha / "vendor_assets" / "pkg" / "file.txt").write_text(
        "x\n", encoding="utf-8")
    (workspace.alpha / "deps").symlink_to(workspace.alpha / "vendor_assets")

    project_config = replace(workspace.config.projects["alpha"],
                             copy_links=("deps", "deps/pkg"))
    config = replace(workspace.config,
                     projects={**workspace.config.projects, "alpha": project_config},
                     sandbox="bwrap")
    attempt_id = open_job(config, workspace.ledger, "alpha", "copy-links-nested-under-link",
                          "full", ("builder",))
    copy = copy_root(config, attempt_id, "builder")
    assert (copy / "deps").is_symlink()
    # Never physically created inside the real repo's vendor_assets dir.
    assert not (workspace.alpha / "vendor_assets" / "pkg" / "pkg").exists()

    opened = [e for _seq, e in workspace.ledger.events("alpha", "job.opened")
             if e.payload.get("attempt_id") == attempt_id][0]
    assert opened.payload["copy_links_skipped"] == [
        {"role": "builder", "entry": "deps/pkg",
         "reason": "copy_link_parent_is_symlink"},
    ]


def test_worker_copy_has_base_commit_so_status_shows_only_changes(workspace) -> None:
    import subprocess
    from engine.steps.open_job import copy_root, open_job
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha", "copy-base",
                           "full", ("builder",))
    copy = copy_root(workspace.config, attempt_id, "builder")
    status = subprocess.run(["git", "-C", str(copy), "status", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout
    assert status == ""
    (copy / "src" / "app.py").write_text("changed\n", encoding="utf-8")
    status = subprocess.run(["git", "-C", str(copy), "status", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout
    assert status.strip() == "M src/app.py"
