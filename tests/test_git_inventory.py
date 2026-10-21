"""Git inventory handles literal paths and uses the stored commit graph."""

import hashlib
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from tests.test_shadow import GitRepository, git
from worktree_controller import git_backend
from worktree_controller.models import ControllerError


@pytest.mark.parametrize(
    "payload,counts",
    [
        (b"R  new\0a\0", (False, 1, 0, 0)),
        (b"C  new\0oldname\0", (False, 1, 0, 0)),
        (b"RM new\0?? source\0 M changed\0?? other\0", (False, 1, 2, 1)),
        (b" R new\0source\0", (False, 0, 1, 0)),
        (b"", (True, 0, 0, 0)),
    ],
)
def test_status_consumes_rename_and_copy_source_paths(monkeypatch, payload, counts):
    monkeypatch.setattr(
        git_backend, "_git", lambda *args, **kw: subprocess.CompletedProcess([], 0, stdout=payload)
    )
    assert git_backend._status_counts(Path("/fixture")) == counts


@pytest.mark.parametrize("payload", [b"R  new\0", b"M normal\0", b"R  new\0\0"])
def test_status_refuses_incomplete_records(monkeypatch, payload):
    monkeypatch.setattr(
        git_backend, "_git", lambda *args, **kw: subprocess.CompletedProcess([], 0, stdout=payload)
    )
    with pytest.raises(ControllerError, match="porcelain|source path"):
        git_backend._status_counts(Path("/fixture"))


def test_actual_rename_and_unicode_refs_are_inventoried():
    with GitRepository(linked=0) as repository:
        (repository.repo / "a").write_text("content")
        git(repository.repo, "add", "a")
        git(repository.repo, "commit", "-m", "fixture")
        git(repository.repo, "branch", "功能")
        git(repository.repo, "tag", "版本")
        git(repository.repo, "mv", "a", "new name")
        assert git_backend._status_counts(repository.repo) == (False, 1, 0, 0)
        refs = git_backend.public_refs(repository.repo)
        assert refs["refs/heads/功能"] == refs["refs/heads/main"] == refs["refs/tags/版本"]


@pytest.mark.parametrize("configured", [False, True])
def test_separate_git_directory_preserves_canonical_checkout_identity(configured):
    with GitRepository(linked=0) as repository:
        common = repository.root / "git-state"
        git(repository.repo, "init", "--separate-git-dir", str(common))
        if configured:
            git(repository.repo, "config", "core.worktree", str(repository.repo))
        state = git_backend.inventory(repository.repo)
        assert state.common_dir == common
        assert state.canonical_path == state.worktrees[0].path == repository.repo
        assert state.worktrees[0].git_dir == common
        linked = repository.root / "linked-separate"
        git(repository.repo, "worktree", "add", "-b", "linked-separate", str(linked))
        if configured:
            assert git_backend.inventory(linked).canonical_path == repository.repo
        else:
            with pytest.raises(ControllerError, match="core.worktree canonical mapping"):
                git_backend.inventory(linked)


def test_replacement_refs_cannot_invent_ancestry_or_change_tree_evidence():
    with GitRepository(linked=0) as repository:
        base = git(repository.repo, "rev-parse", "HEAD")
        tree = git(repository.repo, "rev-parse", "HEAD^{tree}")
        unrelated = git(repository.repo, "commit-tree", tree, "-m", "unrelated root")
        (repository.repo / "changed").write_text("replacement content")
        git(repository.repo, "add", "changed")
        replacement_tree = git(repository.repo, "write-tree")
        forged = git(
            repository.repo, "commit-tree", replacement_tree, "-p", base, "-m", "forged parent"
        )
        git(repository.repo, "replace", unrelated, forged)
        assert (
            subprocess.run(
                ["git", "-C", str(repository.repo), "merge-base", "--is-ancestor", base, unrelated],
                check=False,
            ).returncode
            == 0
        )
        assert not git_backend.is_ancestor(repository.repo, base, unrelated)
        assert git_backend.is_ancestor(repository.repo, base, forged)
        assert git_backend.resolve_commit(repository.repo, unrelated) == unrelated
        assert git_backend.commit_tree_oid(repository.repo, unrelated) == tree


def test_inventory_observes_worktrees_concurrently_in_registration_order(monkeypatch):
    with GitRepository(linked=3) as repository:
        (repository.linked[0] / "untracked").write_text("new")
        (repository.linked[2] / "staged").write_text("new")
        git(repository.linked[2], "add", "staged")
        expected = [
            git_backend._status_counts(path) for path in [repository.repo, *repository.linked]
        ]
        # Every observation must be in flight together before any can finish.
        barrier = threading.Barrier(len(expected), timeout=10)
        observe = git_backend._status_counts

        def concurrent_observation(path):
            barrier.wait()
            return observe(path)

        monkeypatch.setattr(git_backend, "_status_counts", concurrent_observation)
        state = git_backend.inventory(repository.repo)
        assert [item.path for item in state.worktrees] == [repository.repo, *repository.linked]
        assert [
            (item.clean, item.staged, item.modified, item.untracked) for item in state.worktrees
        ] == expected


def test_inventory_reports_the_first_failure_in_registration_order(monkeypatch):
    with GitRepository(linked=2) as repository:
        first, second = repository.linked
        second_failed = threading.Event()
        observe = git_backend._status_counts

        def failing_observation(path):
            if path == first:
                # The later worktree fails first; the earlier failure still wins.
                assert second_failed.wait(10)
                raise ControllerError("GIT_ERROR", "first linked failure")
            if path == second:
                second_failed.set()
                raise ControllerError("GIT_ERROR", "second linked failure")
            return observe(path)

        monkeypatch.setattr(git_backend, "_status_counts", failing_observation)
        with pytest.raises(ControllerError, match="first linked failure"):
            git_backend.inventory(repository.repo)


def test_state_fingerprint_matches_the_sequential_digest():
    with GitRepository(linked=3) as repository:
        (repository.linked[1] / "untracked").write_text("new")
        state = git_backend.inventory(repository.repo)
        shutil.rmtree(repository.linked[2])
        digest = hashlib.sha256()
        digest.update(
            git_backend._git(
                repository.repo,
                "for-each-ref",
                "--format=%(refname)%00%(objectname)%00%(symref)",
            ).stdout
        )
        digest.update(
            git_backend._git(repository.repo, "worktree", "list", "--porcelain", "-z").stdout
        )
        for item in state.worktrees:
            digest.update(os.fsencode(item.path))
            digest.update(item.head.encode("ascii"))
            digest.update((item.branch or "DETACHED").encode("utf-8"))
            if not item.path.exists():
                continue
            index = git_backend._text(
                item.path, "rev-parse", "--path-format=absolute", "--git-path", "index"
            )
            digest.update(Path(index).read_bytes())
            digest.update(
                git_backend._git(
                    item.path, "status", "--porcelain=v1", "-z", "--untracked-files=all"
                ).stdout
            )
        assert git_backend.git_state_fingerprint(state) == digest.hexdigest()
        (repository.linked[0] / "changed").write_text("new")
        assert git_backend.git_state_fingerprint(state) != digest.hexdigest()
