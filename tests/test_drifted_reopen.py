from __future__ import annotations

import pytest

from tests.test_controlled import adopt_canonical
from tests.test_integration_pipeline import POLICY_THREE_CANDIDATES, _source
from tests.test_shadow import GitRepository, git
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import reopen
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore


def _drifted(repository: GitRepository):
    adopt_canonical(repository)
    source, allocation, frozen = _source(
        repository, task="drift", relative_path="drift.txt", content="frozen\n"
    )
    (source / "drift.txt").write_text("after handoff\n")
    git(source, "commit", "-am", "commit after handoff")
    store = MetadataStore(inventory(repository.repo).common_dir)
    return source, store, store.load().records[allocation], frozen


def test_drifted_reopen_requires_exact_descendant_head() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        source, store, record, frozen = _drifted(repository)
        head = git(source, "rev-parse", "HEAD")
        args = dict(target=source, expected_record_version=record.record_version, owner=record.owner)
        with pytest.raises(ControllerError) as plain:
            reopen(repository.repo, **args)
        assert plain.value.code == "IDENTITY_MISMATCH"
        with pytest.raises(ControllerError) as wrong:
            reopen(repository.repo, drifted_head=frozen, **args)
        assert wrong.value.code == "IDENTITY_MISMATCH"
        assert store.load().records[record.allocation_id] == record

        decision = reopen(repository.repo, drifted_head=head, **args)["decision"]
        assert decision["code"] == "REOPENED"
        reopened = store.load().records[record.allocation_id]
        assert reopened.lifecycle == "ACTIVE"
        assert reopened.handoff_commit is None
        event = reopened.history[-1]
        assert event["event"] == "REOPENED_AFTER_DRIFT"
        assert event["frozen_handoff_commit"] == frozen
        assert event["observed_head"] == head


def test_drifted_reopen_refuses_rewritten_history() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        source, store, record, frozen = _drifted(repository)
        git(source, "reset", "--hard", f"{frozen}~1")
        (source / "other.txt").write_text("rewritten\n")
        git(source, "add", "other.txt")
        git(source, "commit", "-m", "rewritten history")
        with pytest.raises(ControllerError) as error:
            reopen(
                repository.repo,
                target=source,
                expected_record_version=record.record_version,
                owner=record.owner,
                drifted_head=git(source, "rev-parse", "HEAD"),
            )
        assert error.value.code == "IDENTITY_MISMATCH"
        assert store.load().records[record.allocation_id] == record


def test_drifted_reopen_refuses_unchanged_head() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        adopt_canonical(repository)
        source, allocation, frozen = _source(
            repository, task="same", relative_path="same.txt", content="same\n"
        )
        record = MetadataStore(inventory(repository.repo).common_dir).load().records[allocation]
        with pytest.raises(ControllerError) as error:
            reopen(
                repository.repo,
                target=source,
                expected_record_version=record.record_version,
                owner=record.owner,
                drifted_head=frozen,
            )
        assert error.value.code == "IDENTITY_MISMATCH"
