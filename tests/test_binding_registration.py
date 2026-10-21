"""A binding belongs to the exact Git registration it was written for."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.test_controlled import acquire_one, adopt_canonical, handoff_one
from tests.test_shadow import GitRepository, git
from worktree_controller.controller import guard_write, status
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import release
from worktree_controller.models import ControllerError
from worktree_controller.store import REGISTRATION_MARKER, MetadataStore
from worktree_controller.writer_lease import acquire_writer_lease, describe_holder


def _store(repository: GitRepository) -> MetadataStore:
    return MetadataStore(inventory(repository.repo).common_dir)


def _item(repository: GitRepository, target: Path):
    return next(item for item in inventory(repository.repo).worktrees if item.path == target)


def _row(payload: dict, target: Path) -> dict:
    return next(row for row in payload["worktrees"] if row["path"] == str(target))


def _codes(payload: dict) -> list[str]:
    return [warning["code"] for warning in payload["warnings"]]


def test_acquire_ties_the_binding_to_its_git_registration() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, acquired = acquire_one(repository)
        allocation = acquired["decision"]["allocation_id"]
        item = _item(repository, target)
        store = _store(repository)
        document = json.loads((store.binding_registrations_dir / f"{item.key}.json").read_text())
        marker = json.loads((item.git_dir / REGISTRATION_MARKER).read_text())
        assert document["allocation_id"] == marker["allocation_id"] == allocation
        assert document["token"] == marker["token"]
        assert document["admin_dir"] == item.git_dir.relative_to(store.common_dir).as_posix()
        observed = status(repository.repo)
        assert _row(observed, target)["allocation_id"] == allocation
        assert "BINDING_IDENTITY_DRIFT" not in _codes(observed)


def test_reused_registration_name_does_not_inherit_the_old_allocation() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, acquired = acquire_one(repository, task="alpha", owner="owner-a")
        allocation = acquired["decision"]["allocation_id"]
        # Outside the Controller: the checkout is replaced by an unrelated one that
        # Git registers under the same administrative name.
        git(repository.repo, "worktree", "remove", "--force", str(target))
        git(repository.repo, "worktree", "add", "-b", "unrelated/other", str(target), "HEAD")
        observed = status(repository.repo)
        row = _row(observed, target)
        assert row["allocation_id"] is None
        assert row["owner"] is None
        drift = [w for w in observed["warnings"] if w["code"] == "BINDING_IDENTITY_DRIFT"]
        assert [w["allocation_id"] for w in drift] == [allocation]
        decision = guard_write(
            repository.repo,
            target=target,
            task_key="alpha",
            authority_key="new-authority",
            owner="owner-a",
            operation="feature-write",
            holder=describe_holder(os.getpid(), client_id="test-client"),
        )["decision"]
        assert decision["code"] != "WRITE_ALLOWED"
        with pytest.raises(ControllerError) as refused:
            acquire_writer_lease(
                repository.repo,
                target=target,
                owner="owner-a",
                client_id="test-client",
                holder_pid=os.getpid(),
            )
        assert refused.value.code == "NEEDS_ADOPTION"


def test_missing_registration_keeps_orphan_binding_semantics() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, acquired = acquire_one(repository)
        git(repository.repo, "worktree", "remove", "--force", str(target))
        observed = status(repository.repo)
        codes = _codes(observed)
        assert "ORPHAN_BINDING" in codes
        assert "BINDING_IDENTITY_DRIFT" not in codes
        store = _store(repository)
        assert acquired["decision"]["allocation_id"] in store.load().bindings.values()


def test_binding_without_registration_document_keeps_legacy_meaning() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, acquired = acquire_one(repository)
        item = _item(repository, target)
        (_store(repository).binding_registrations_dir / f"{item.key}.json").unlink()
        (item.git_dir / REGISTRATION_MARKER).unlink()
        observed = status(repository.repo)
        assert _row(observed, target)["allocation_id"] == acquired["decision"]["allocation_id"]
        assert "BINDING_IDENTITY_DRIFT" not in _codes(observed)


@pytest.mark.parametrize("content", ["", "{}", "not json", None])
def test_changed_or_missing_marker_is_drift(content: str | None) -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        marker = _item(repository, target).git_dir / REGISTRATION_MARKER
        if content is None:
            marker.unlink()
        else:
            marker.write_text(content, encoding="utf-8")
        assert "BINDING_IDENTITY_DRIFT" in _codes(status(repository.repo))


def test_symlinked_marker_is_not_identity() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        marker = _item(repository, target).git_dir / REGISTRATION_MARKER
        copy = repository.root / "marker-copy"
        copy.write_bytes(marker.read_bytes())
        marker.unlink()
        marker.symlink_to(copy)
        assert "BINDING_IDENTITY_DRIFT" in _codes(status(repository.repo))


def test_release_removes_the_registration_document() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        key = _item(repository, target).key
        handoff_one(repository, target)
        released = release(
            repository.repo, target=target, expected_record_version=2, owner_release="module-owner"
        )
        assert released["decision"]["code"] == "RELEASED"
        assert not (_store(repository).binding_registrations_dir / f"{key}.json").exists()


def test_adopt_registers_linked_but_not_canonical_checkouts() -> None:
    with GitRepository(linked=1) as repository:
        repository.adopt_all()
        store = _store(repository)
        linked = _item(repository, repository.linked[0])
        canonical = _item(repository, repository.repo)
        assert (store.binding_registrations_dir / f"{linked.key}.json").is_file()
        assert (linked.git_dir / REGISTRATION_MARKER).is_file()
        assert not (store.binding_registrations_dir / f"{canonical.key}.json").exists()
        assert not (canonical.git_dir / REGISTRATION_MARKER).exists()


@pytest.mark.parametrize(
    "change",
    [
        {"admin_dir": "worktrees/other"},
        {"admin_dir": "../worktrees/x"},
        {"contract": "worktree-registration/v0"},
        {"token": "not-a-uuid"},
        {"extra": True},
    ],
)
def test_invalid_registration_document_fails_closed(change: dict) -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        path = _store(repository).binding_registrations_dir / f"{_item(repository, target).key}.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), **change}), encoding="utf-8")
        with pytest.raises(ControllerError) as invalid:
            _store(repository).load()
        assert invalid.value.code == "INVALID_METADATA"
