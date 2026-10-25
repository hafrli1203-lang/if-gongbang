"""Unit tests for engine.steps.open_job: attempt creation, copies, dirty
paths, and the CLOSED-state guard. Uses the two-repo `workspace` fixture."""
from __future__ import annotations

import pytest

from engine import ids
from engine.ledger import Event
from engine.processor import submit
from engine.state import TransitionError, transition
from engine.full_orbit import _commit_job_trail
from engine.steps.open_job import _copies_dir, _make_copies, copy_root, open_job
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


def test_crash_between_job_opened_and_state_changed_is_reconciled(
    workspace: Workspace,
) -> None:
    """Simulates the exact crash window open_job cannot make atomic
    without a cross-event-append transaction in engine.ledger (see
    engine.steps.open_job._reconcile_pending_open's own docstring):
    job.opened lands (its handler already sets current_attempt_id/
    generation and inserts the attempt as 'active'), but the matching
    state.changed never does. The project is left reporting CLOSED
    with an 'active' current attempt. The next open_job call must
    finish that interrupted transition and hand back the SAME
    attempt_id, never silently start a second, phantom attempt
    alongside the orphaned one."""
    job_id = ids.new_job_id("alpha")
    attempt_id = ids.attempt_id(job_id, 1)
    generation = workspace.ledger.project("alpha").generation + 1
    workspace.ledger.append(Event(
        project="alpha", kind="job.opened", actor_kind="owner", actor_id="owner",
        attempt_id=attempt_id,
        payload={
            "job_id": job_id, "job_name": "crashed-job", "attempt_id": attempt_id,
            "n": 1, "kind": "full", "generation": generation,
            "dirty_paths": [], "base_head": "0" * 40,
            "copy_links_skipped": [], "copy_git_config_sha256": {},
        },
    ))
    # state.changed never ran -- the project still reports CLOSED.
    assert workspace.ledger.project("alpha").state == "CLOSED"
    assert workspace.ledger.project("alpha").current_attempt_id == attempt_id
    assert workspace.ledger.attempt(attempt_id).status == "active"

    resumed = open_job(workspace.config, workspace.ledger, "alpha",
                       "crashed-job", "full", ("builder",))

    assert resumed == attempt_id
    assert workspace.ledger.project("alpha").state == "OPEN"
    assert workspace.ledger.project("alpha").current_attempt_id == attempt_id
    # No second attempt was created for "crashed-job" -- resuming the
    # interrupted transition is the whole effect, not an extra
    # job.opened on top of it.
    assert workspace.ledger.attempts_for_job_name("alpha", "crashed-job") == [
        workspace.ledger.attempt(attempt_id),
    ]


def test_open_job_for_a_different_job_name_is_unaffected_by_an_unrelated_active_attempt(
    workspace: Workspace,
) -> None:
    """The reconciliation added for the crash window above (job.opened
    landed, state.changed didn't) must only resume THAT SAME job_name's
    own attempt -- it must never be mistaken for permission to hand a
    DIFFERENT job_name request someone else's still-'active' attempt
    just because the project also happens to read CLOSED (which can
    arise from something other than a crash, e.g. a caller that moved
    the project to CLOSED without ever closing its current attempt)."""
    job_id = ids.new_job_id("alpha")
    attempt_id = ids.attempt_id(job_id, 1)
    generation = workspace.ledger.project("alpha").generation + 1
    workspace.ledger.append(Event(
        project="alpha", kind="job.opened", actor_kind="owner", actor_id="owner",
        attempt_id=attempt_id,
        payload={
            "job_id": job_id, "job_name": "job1", "attempt_id": attempt_id,
            "n": 1, "kind": "full", "generation": generation,
            "dirty_paths": [], "base_head": "0" * 40,
            "copy_links_skipped": [], "copy_git_config_sha256": {},
        },
    ))
    # Project left CLOSED without ever finishing job1's attempt --
    # deliberately not the crash shape this reconciliation targets,
    # since job2 (below) asks for a different job_name.
    workspace.ledger.append(Event(
        project="alpha", kind="state.changed", actor_kind="owner", actor_id="owner",
        payload={"state": "CLOSED", "input": "open_job", "prev": "CLOSED"},
    ))

    attempt_2 = open_job(workspace.config, workspace.ledger, "alpha",
                         "job2", "full", ("builder",))

    assert attempt_2 != attempt_id
    assert workspace.ledger.attempts_for_job_name("alpha", "job1") == [
        workspace.ledger.attempt(attempt_id),
    ]


def test_crash_after_copies_made_but_before_job_opened_is_reconciled(
    workspace: Workspace,
) -> None:
    """Fault injection for open_job's own "Note on (6a)" crash window:
    job.open_intent lands and _make_copies' own atomic rename succeeds
    (copies_dir now holds a real, complete copy on disk), but
    job.opened never gets appended -- nothing else in the ledger ever
    names this attempt_id at all. A later open_job call (for the SAME
    job_name here) must start a genuinely new attempt (never reuse the
    orphaned one) AND clean up the now-unreachable copies_dir left
    behind, rather than leaving it on disk forever with nothing able
    to find or remove it again."""
    job_id = ids.new_job_id("alpha")
    attempt_id = ids.attempt_id(job_id, 1)
    workspace.ledger.append(Event(
        project="alpha", kind="job.open_intent", actor_kind="owner",
        actor_id="owner", attempt_id=attempt_id,
        payload={"job_id": job_id, "job_name": "crashed-before-opened",
                  "n": 1, "attempt_id": attempt_id,
                  "copies_dir": str(_copies_dir(workspace.config, attempt_id))},
    ))
    project_config = workspace.config.projects["alpha"]
    _make_copies(project_config, workspace.config, attempt_id, ("builder",))
    copies_dir = _copies_dir(workspace.config, attempt_id)
    assert copies_dir.exists()

    resumed = open_job(workspace.config, workspace.ledger, "alpha",
                       "crashed-before-opened", "full", ("builder",))

    assert resumed != attempt_id  # a genuinely new attempt, not a reuse
    assert not copies_dir.exists()  # the orphaned copy was cleaned up
    new_copy = copy_root(workspace.config, resumed, "builder")
    assert (new_copy / "src" / "app.py").exists()


def test_crash_before_the_rename_leaves_only_a_tmp_staging_dir_to_clean_up(
    workspace: Workspace,
) -> None:
    """Earlier sub-case of the same crash window: job.open_intent lands,
    but the crash hits before _make_copies' own atomic rename even
    runs -- only the `.tmp-{dirname}` staging directory exists, never
    the final copies_dir. The reconciliation must remove that staging
    directory too, not just the final one."""
    job_id = ids.new_job_id("alpha")
    attempt_id = ids.attempt_id(job_id, 1)
    workspace.ledger.append(Event(
        project="alpha", kind="job.open_intent", actor_kind="owner",
        actor_id="owner", attempt_id=attempt_id,
        payload={"job_id": job_id, "job_name": "crashed-before-rename",
                  "n": 1, "attempt_id": attempt_id,
                  "copies_dir": str(_copies_dir(workspace.config, attempt_id))},
    ))
    tmp_root = (workspace.config.home / "copies"
               / f".tmp-{ids.attempt_dirname(attempt_id)}")
    tmp_root.mkdir(parents=True)
    (tmp_root / "partial-marker.txt").write_text("partial", encoding="utf-8")
    copies_dir = _copies_dir(workspace.config, attempt_id)
    assert tmp_root.exists()
    assert not copies_dir.exists()

    resumed = open_job(workspace.config, workspace.ledger, "alpha",
                       "crashed-before-rename", "full", ("builder",))

    assert resumed != attempt_id
    assert not tmp_root.exists()
    assert not copies_dir.exists()


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


def test_commit_job_trail_never_absorbs_an_unrelated_already_staged_file(
    workspace: Workspace,
) -> None:
    """Regression for codex-goal-review-4.md item 4 / codex-diff-
    review-s17.md "Ungated trail commits can absorb unrelated staged
    source" (engine/full_orbit.py:472): this is a plain, UNGATED
    engine-side commit (full_orbit._commit_job_trail's own docstring)
    meant to cover only what the processor itself just wrote under
    .gongbang/<job>/ -- it must never also commit some OTHER path the
    owner (or anything else) had already `git add`ed before this ran.
    A bare `git commit` with no pathspec commits every currently
    staged change regardless of what `git add` was just given; this
    checks the fix (passing the same path list to `commit -- <paths>`
    that `add` used) leaves an unrelated already-staged file exactly
    as staged as it was, never swept into this commit."""
    attempt_id = open_job(workspace.config, workspace.ledger, "alpha",
                          "trail-scope", "full", ("builder",))
    job_name = "trail-scope"
    gongbang_dir = workspace.alpha / ".gongbang" / job_name
    gongbang_dir.mkdir(parents=True)
    (gongbang_dir / "facts.md").write_text("facts\n", encoding="utf-8")

    # The owner (or anything else) already staged an unrelated path
    # before this trail commit runs.
    (workspace.alpha / "owner_unrelated.txt").write_text("owner work\n", encoding="utf-8")
    git(workspace.alpha, "add", "owner_unrelated.txt")
    status_before = git(workspace.alpha, "status", "--porcelain")
    owner_line_before = [l for l in status_before.splitlines()
                         if "owner_unrelated.txt" in l][0]

    _commit_job_trail(workspace.config, workspace.ledger, "alpha", attempt_id,
                      job_name, "gongbang: trail-scope - facts")

    # The unrelated file is still staged, exactly the same status line
    # as before -- never committed, never unstaged.
    status_after = git(workspace.alpha, "status", "--porcelain")
    owner_lines_after = [l for l in status_after.splitlines()
                         if "owner_unrelated.txt" in l]
    assert owner_lines_after == [owner_line_before]

    # The new commit's own tree only ever touched .gongbang/<job>/.
    changed = git(workspace.alpha, "diff-tree", "--no-commit-id", "--name-only",
                  "-r", "HEAD").splitlines()
    assert changed == [f".gongbang/{job_name}/facts.md"]
    assert "owner_unrelated.txt" not in changed


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
