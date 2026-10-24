"""Unit tests for engine.evidence: fingerprint() and the evidence ledger ops."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from engine.evidence import (
    FingerprintError,
    fingerprint,
    invalidate_after,
    latest,
    range_tree,
    record,
    sha256_file,
    three_match,
)
from engine.ledger import Ledger
from tests.conftest import git, make_repo

REPO_FILES = {
    "src/app.py": "print('hi')\n",
    "docs/readme.md": "doc\n",
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = make_repo(tmp_path / "repo", REPO_FILES)
    (path / "node_modules").mkdir()
    (path / "node_modules" / "x.js").write_text("ignored\n", encoding="utf-8")
    (path / "dist").mkdir()
    (path / "dist" / "bundle.js").write_text("built\n", encoding="utf-8")
    return path


def test_same_tree_twice_equal(repo: Path) -> None:
    assert fingerprint(repo) == fingerprint(repo)


def test_one_byte_change_differs(repo: Path) -> None:
    before = fingerprint(repo)
    (repo / "src" / "app.py").write_text("print('bye')\n", encoding="utf-8")
    after = fingerprint(repo)
    assert before != after


def test_ignored_file_change_same_fp(repo: Path) -> None:
    before = fingerprint(repo)
    (repo / "node_modules" / "x.js").write_text("changed\n", encoding="utf-8")
    (repo / "dist" / "bundle.js").write_text("changed\n", encoding="utf-8")
    after = fingerprint(repo)
    assert before == after


def test_excluded_path_change_same_fp(repo: Path) -> None:
    (repo / "build_output").mkdir()
    (repo / "build_output" / "a.txt").write_text("v1\n", encoding="utf-8")
    before = fingerprint(repo, excludes=("build_output/",))
    (repo / "build_output" / "a.txt").write_text("v2\n", encoding="utf-8")
    after = fingerprint(repo, excludes=("build_output/",))
    assert before == after


def test_untracked_non_ignored_new_file_differs(repo: Path) -> None:
    before = fingerprint(repo)
    (repo / "src" / "new_module.py").write_text("print('new')\n", encoding="utf-8")
    after = fingerprint(repo)
    assert before != after


def test_force_added_ignored_tracked_file_change_differs(repo: Path) -> None:
    """E1: a tracked file matching .gitignore (force-added) must still be
    read from disk by range_tree — a plain `git add -A .` on a fresh
    temp index would re-stage it as untracked-and-ignored and silently
    drop its change from the fingerprint."""
    ignored = repo / "dist" / "tracked.js"
    ignored.write_text("v1\n", encoding="utf-8")
    git(repo, "add", "-f", "dist/tracked.js")
    git(repo, "commit", "-q", "-m", "force add ignored file")

    before = fingerprint(repo)
    ignored.write_text("v2\n", encoding="utf-8")
    after = fingerprint(repo)
    assert before != after


def test_info_exclude_path_not_in_gitignore_still_included(repo: Path) -> None:
    """E1: .git/info/exclude must NOT be honored (only in-tree .gitignore
    files are) — a path listed only in info/exclude is still untracked and
    still enters the tree."""
    (repo / ".git" / "info" / "exclude").write_text("secret.local\n", encoding="utf-8")
    before = fingerprint(repo)
    (repo / "secret.local").write_text("v1\n", encoding="utf-8")
    after = fingerprint(repo)
    assert before != after


def test_range_tree_is_fingerprint(repo: Path) -> None:
    assert range_tree(repo) == fingerprint(repo)


def _show(repo: Path, tree_hash: str, rel: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"{tree_hash}:{rel}"],
        check=True, capture_output=True, text=True,
    ).stdout


def test_staged_new_file_in_range(repo: Path) -> None:
    """N1: `git ls-files --others` deliberately excludes anything already in
    the index, so a file that is `git add`ed but not yet committed used to
    vanish from both the tracked (HEAD) and untracked listings — dropped
    from the fingerprint and from a commit built off this tree."""
    before = fingerprint(repo)
    new_file = repo / "src" / "staged_new.py"
    new_file.write_text("print('staged')\n", encoding="utf-8")
    git(repo, "add", "src/staged_new.py")  # staged, not committed
    after = fingerprint(repo)
    assert before != after
    assert _show(repo, after, "src/staged_new.py") == "print('staged')\n"


def test_staged_rename_new_name_present(repo: Path) -> None:
    """N1: a staged (not committed) rename must show the new path in the
    range and must not keep the old path (it no longer exists on disk)."""
    git(repo, "mv", "src/app.py", "src/renamed.py")
    fp = fingerprint(repo)
    assert _show(repo, fp, "src/renamed.py") == "print('hi')\n"
    old_path_check = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{fp}:src/app.py"],
        capture_output=True,
    )
    assert old_path_check.returncode != 0


def test_excluded_tracked_path_preserved_in_tree(tmp_path: Path) -> None:
    """#7 remainder: an excluded path that IS tracked in HEAD must keep its
    HEAD content in the tree rather than being dropped — otherwise a commit
    built from range_tree would delete it. An unrelated change elsewhere
    still changes the fingerprint."""
    files = {
        "src/app.py": "print('hi')\n",
        "vendor/lib.txt": "v1\n",
    }
    repo2 = make_repo(tmp_path / "repo2", files)
    before = fingerprint(repo2, excludes=("vendor/",))

    (repo2 / "vendor" / "lib.txt").write_text("v2\n", encoding="utf-8")
    (repo2 / "src" / "app.py").write_text("print('bye')\n", encoding="utf-8")
    after = fingerprint(repo2, excludes=("vendor/",))

    assert before != after  # the unrelated, non-excluded change is detected
    assert _show(repo2, after, "vendor/lib.txt") == "v1\n"  # excluded: HEAD content kept


def test_excluded_tracked_path_survives_deletion_on_disk(tmp_path: Path) -> None:
    """Same rule, pushed further: even if the excluded tracked file is
    deleted from disk entirely, it must still be present (at HEAD content)
    in the computed tree."""
    files = {
        "src/app.py": "print('hi')\n",
        "vendor/lib.txt": "v1\n",
    }
    repo3 = make_repo(tmp_path / "repo3", files)
    (repo3 / "vendor" / "lib.txt").unlink()
    fp = fingerprint(repo3, excludes=("vendor/",))
    assert _show(repo3, fp, "vendor/lib.txt") == "v1\n"


def test_symlink_outside_tree_raises(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = repo / "src" / "escape.py"
    os.symlink(outside, link)
    with pytest.raises(FingerprintError):
        fingerprint(repo)


def test_nested_git_raises(repo: Path) -> None:
    nested = repo / "vendor" / "sub"
    nested.mkdir(parents=True)
    (nested / ".git").mkdir()
    with pytest.raises(FingerprintError):
        fingerprint(repo)


def test_non_repo_dir_raises(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    (not_a_repo / "f.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(FingerprintError):
        fingerprint(not_a_repo)


def test_sha256_file_known_value(tmp_path: Path) -> None:
    import hashlib
    target = tmp_path / "f.txt"
    target.write_bytes(b"hello world")
    assert sha256_file(target) == hashlib.sha256(b"hello world").hexdigest()


# ---- ledger-backed evidence ops --------------------------------------------

@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    led = Ledger.create(tmp_path / "ledger.sqlite")
    led.register_project("alpha")
    return led


def test_record_and_latest_roundtrip(ledger: Ledger) -> None:
    record(ledger, "alpha", "A#1", "review", "f" * 40)
    row = latest(ledger, "A#1", "review")
    assert row is not None
    assert row.fingerprint == "f" * 40
    assert row.stale is False


def test_latest_none_when_missing(ledger: Ledger) -> None:
    assert latest(ledger, "A#1", "review") is None


def test_invalidate_after_marks_stale(ledger: Ledger) -> None:
    record(ledger, "alpha", "A#1", "review", "f" * 40)
    invalidate_after(ledger, "alpha", "A#1", ("review",))
    assert latest(ledger, "A#1", "review") is None


def test_three_match_all_equal(ledger: Ledger) -> None:
    fp = "a" * 40
    record(ledger, "alpha", "A#1", "review", fp, payload={"passed": True})
    record(ledger, "alpha", "A#1", "bench", fp, payload={"passed": True})
    assert three_match(ledger, "A#1", fp) is True


def test_three_match_missing_bench(ledger: Ledger) -> None:
    fp = "a" * 40
    record(ledger, "alpha", "A#1", "review", fp, payload={"passed": True})
    assert three_match(ledger, "A#1", fp) is False


def test_three_match_stale_review(ledger: Ledger) -> None:
    fp = "a" * 40
    record(ledger, "alpha", "A#1", "review", fp, payload={"passed": True})
    record(ledger, "alpha", "A#1", "bench", fp, payload={"passed": True})
    invalidate_after(ledger, "alpha", "A#1", ("review",))
    assert three_match(ledger, "A#1", fp) is False


def test_three_match_different_fp(ledger: Ledger) -> None:
    record(ledger, "alpha", "A#1", "review", "a" * 40, payload={"passed": True})
    record(ledger, "alpha", "A#1", "bench", "a" * 40, payload={"passed": True})
    assert three_match(ledger, "A#1", "b" * 40) is False


def test_three_match_bench_passed_false(ledger: Ledger) -> None:
    """E2: bench/review evidence whose payload says passed=False must not
    satisfy three_match, even when all three fingerprints agree."""
    fp = "a" * 40
    record(ledger, "alpha", "A#1", "review", fp, payload={"passed": True})
    record(ledger, "alpha", "A#1", "bench", fp, payload={"passed": False})
    assert three_match(ledger, "A#1", fp) is False


def test_three_match_missing_passed_key_counts_as_false(ledger: Ledger) -> None:
    """E2: a payload that omits "passed" entirely (the pre-fix call shape)
    must be treated as not passed, not as passed."""
    fp = "a" * 40
    record(ledger, "alpha", "A#1", "review", fp)  # no payload at all
    record(ledger, "alpha", "A#1", "bench", fp, payload={"passed": True})
    assert three_match(ledger, "A#1", fp) is False


# ---------------------------------------------------------------------------
# Regression: the batched hash-object/update-index path in _build_index
# must produce the exact same tree hash as the original one-subprocess-
# per-path implementation, on a tree with mixed file modes, a deletion,
# an untracked file, and a symlink (the one case that must still go
# through the old per-path _hash_entry, never the batch).
# ---------------------------------------------------------------------------

def _old_range_tree(repo: Path, excludes: tuple[str, ...] = ()) -> str:
    """Reference implementation: the exact per-path hash-object +
    update-index --add --cacheinfo loop this module used before
    _hash_entries_batch existed. Kept ONLY to prove the batched
    fingerprint equals it -- never for production use."""
    import tempfile

    from engine import evidence as ev

    ev._check_tree(repo, excludes)
    env_base = dict(os.environ)
    env_base["GIT_CONFIG_GLOBAL"] = "/dev/null"
    head_entries = ev._head_tracked_entries(repo, env_base)
    paths = ev._range_paths(repo, env_base, head_entries)
    with tempfile.TemporaryDirectory() as tmp:
        env = ev._index_env(env_base, Path(tmp))
        for rel in paths:
            if ev._is_excluded_rel(rel, excludes):
                head_entry = head_entries.get(rel)
                if head_entry is None:
                    continue
                mode, blob_sha = head_entry
            else:
                full = repo / rel
                if not os.path.lexists(full):
                    continue
                mode, blob_sha = ev._hash_entry(repo, full, env)
            subprocess.run(
                ["git", "-C", str(repo), "-c", "core.fsmonitor=",
                 "-c", "core.hooksPath=/dev/null", "update-index", "--add",
                 "--cacheinfo", f"{mode},{blob_sha},{rel}"],
                check=True, capture_output=True, env=env,
            )
        tree_hash = ev._write_tree(repo, env)
    return tree_hash


def test_batched_fingerprint_equals_old_per_file_implementation_mixed_tree(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "mixed", {
        "regular.txt": "a regular tracked file\n",
        "to_delete.txt": "will be removed from disk\n",
        "unchanged.txt": "stays the same\n",
    })
    # mixed modes: make one tracked file executable.
    exe_path = repo / "regular.txt"
    exe_path.chmod(0o755)
    # a deletion: a tracked file removed from disk (not `git rm`-ed).
    (repo / "to_delete.txt").unlink()
    # a new untracked, non-ignored file.
    (repo / "new_untracked.txt").write_text("brand new\n", encoding="utf-8")
    # a symlink -- must still go through the old per-path _hash_entry,
    # never the batch (git hash-object --stdin-paths always dereferences).
    (repo / "link_to_regular.txt").symlink_to("regular.txt")

    batched = fingerprint(repo)
    reference = _old_range_tree(repo)
    assert batched == reference


def test_batched_fingerprint_equals_old_per_file_implementation_empty_tree(
    tmp_path: Path,
) -> None:
    """No surviving paths at all (every tracked file deleted from disk) --
    _hash_entries_batch's own `if not paths: return {}` early-out, and
    the update-index call built from an empty batch_entries, both get no
    coverage from the single mixed-tree fixture above (which always has
    at least one surviving path)."""
    repo = make_repo(tmp_path / "empty", {"a.txt": "a\n"})
    (repo / "a.txt").unlink()
    assert fingerprint(repo) == _old_range_tree(repo)


def test_batched_fingerprint_equals_old_per_file_implementation_all_excluded(
    tmp_path: Path,
) -> None:
    """Every surviving path is excluded-and-tracked -- to_hash stays
    empty (no `_hash_entries_batch` call happens at all) while
    batch_entries is filled entirely from head_entries carry-over, a
    branch the mixed-tree fixture above never isolates on its own."""
    repo = make_repo(tmp_path / "all_excluded", {
        "vendor/a.txt": "a\n", "vendor/b.txt": "b\n",
    })
    excludes = ("vendor/",)
    assert fingerprint(repo, excludes) == _old_range_tree(repo, excludes)


def test_fingerprint_handles_a_quote_leading_filename(tmp_path: Path) -> None:
    """A file whose name starts with a literal
    double quote makes `git hash-object --stdin-paths` try to parse
    the whole line as a C-quoted string and fail the entire batch
    ("line is badly quoted") -- verified directly. Such a path must be
    routed through the slow, per-file path instead, and the resulting
    fingerprint must still match the (never-batched) reference
    implementation."""
    repo = make_repo(tmp_path / "quoted", {
        "regular.txt": "ordinary\n",
    })
    (repo / '"q.txt').write_text("quoted-name content\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@x",
                    "commit", "-q", "-m", "add quoted-name file"], check=True,
                   capture_output=True)

    batched = fingerprint(repo)
    reference = _old_range_tree(repo)
    assert batched == reference
