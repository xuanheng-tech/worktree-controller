from __future__ import annotations

from contextlib import contextmanager

import pytest

from tests.test_shadow import GitRepository, git
from worktree_controller import git_backend
from worktree_controller.lifecycle import integration_queue, lifecycle_events
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore


def read(repository, reader):
    if reader == "queue":
        return integration_queue(repository.repo)
    return lifecycle_events(repository.repo, consumer="observation-test")


@pytest.mark.parametrize("reader", ["queue", "events"])
def test_query_observes_each_worktree_once(reader, monkeypatch):
    with GitRepository(linked=2) as repository:
        repository.adopt_all()
        commands = []
        locks = []
        original_git = git_backend._git
        original_lock = MetadataStore.lock

        def observed_git(path, *arguments, **kwargs):
            commands.append((path, arguments))
            return original_git(path, *arguments, **kwargs)

        @contextmanager
        def observed_lock(store, *, exclusive):
            locks.append(exclusive)
            with original_lock(store, exclusive=exclusive):
                yield

        monkeypatch.setattr(git_backend, "_git", observed_git)
        monkeypatch.setattr(MetadataStore, "lock", observed_lock)
        payload = read(repository, reader)

        lists = [args for _path, args in commands if args[:2] == ("worktree", "list")]
        statuses = [
            path
            for path, args in commands
            if args == ("status", "--porcelain=v1", "-z", "--untracked-files=all")
        ]
        assert len(lists) == 1
        assert sorted(statuses) == sorted([repository.repo, *repository.linked])
        # The event outbox has its own guarded read, separate from metadata.
        assert locks == ([False] if reader == "queue" else [False, False])
        assert len(payload["worktrees"]) == 3
        assert payload["errors"] == []


@pytest.mark.parametrize("reader", ["queue", "events"])
def test_next_query_observes_new_checkout_and_untracked_files(reader):
    with GitRepository(linked=1) as repository:
        repository.adopt_all()
        assert all(row["clean"] for row in read(repository, reader)["worktrees"])
        (repository.linked[0] / "new.txt").write_text("new task content\n")
        fresh = repository.root / "fresh"
        git(repository.repo, "worktree", "add", "-b", "feature/fresh", str(fresh), "HEAD")

        rows = {row["path"]: row for row in read(repository, reader)["worktrees"]}
        assert rows[str(repository.linked[0])]["changes"]["untracked"] == 1
        assert rows[str(fresh)]["metadata_missing"] is True


@pytest.mark.parametrize("reader", ["queue", "events"])
def test_query_keeps_invalid_metadata_failure(reader):
    with GitRepository(linked=1) as repository:
        repository.adopt_all()
        common = git_backend.resolve_repository(repository.repo)[1]
        store = MetadataStore(common)
        next(store.records_dir.glob("*.json")).write_text("{}")

        with pytest.raises(ControllerError) as error:
            read(repository, reader)
        assert error.value.code == "INVALID_METADATA"


@pytest.mark.parametrize("reader", ["queue", "events"])
def test_query_keeps_locked_missing_checkout_failure(reader):
    with GitRepository(linked=1) as repository:
        repository.adopt_all()
        linked = repository.linked[0]
        git(repository.repo, "worktree", "lock", str(linked))
        linked.rename(repository.root / "preserved-missing-checkout")

        with pytest.raises(ControllerError) as error:
            read(repository, reader)
        assert error.value.code == "GIT_DRIFT"
