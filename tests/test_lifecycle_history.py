from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from tests.test_controlled import _configure_remote, acquire_one, adopt_canonical, handoff_one
from tests.test_integration_pipeline import (
    POLICY_THREE_CANDIDATES,
    _claim,
    _enqueue,
    _publish,
    _source,
    _validate,
)
from tests.test_shadow import GitRepository, git
from worktree_controller import lifecycle_history as history_module
from worktree_controller.cli import main
from worktree_controller.controller import status
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import release
from worktree_controller.lifecycle_history import lifecycle_history
from worktree_controller.models import ControllerError, PreparedCandidateReceipt, PublicationReceipt
from worktree_controller.store import MetadataStore


def _closed(repository, *, task="history-task", authority="history-authority"):
    target, acquired = acquire_one(repository, task=task, authority=authority)
    allocation = acquired["decision"]["allocation_id"]
    handoff_one(repository, target)
    released = release(
        repository.repo, target=target, expected_record_version=2, owner_release="module-owner"
    )
    return target, allocation, released["decision"]["release_receipt_id"]


def _bytes(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_active_to_release_keeps_historical_owner_and_checkout_identity():
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, acquired = acquire_one(repository)
        allocation = acquired["decision"]["allocation_id"]
        live = lifecycle_history(repository.repo, allocation_id=allocation)["decision"]
        assert live["live_allocations"][0]["owner"] == "module-owner"
        assert live["closed_releases"] == []
        head = git(target, "rev-parse", "HEAD")
        handoff_one(repository, target)
        receipt = release(
            repository.repo, target=target, expected_record_version=2, owner_release="module-owner"
        )["decision"]["release_receipt_id"]
        assert not target.exists()
        assert all(w["allocation_id"] != allocation for w in status(repository.repo)["worktrees"])
        result = lifecycle_history(repository.repo, allocation_id=allocation)["decision"]
        assert result["live_allocations"] == []
        closed = result["closed_releases"][0]
        assert closed["release_receipt_id"] == receipt
        assert closed["owner"] == "module-owner"
        assert closed["authority_key"] == "new-authority"
        assert closed["task_key"] == "new-task"
        assert closed["worktree_path"] == str(target)
        assert closed["branch"] == "feature/new-task"
        assert closed["released_head"] == head
        assert closed["release_status"] == "RELEASED"
        assert closed["released_at"]
        assert closed["published_at"] is None
        assert closed["candidate_oid"] is None


def test_query_is_repeatable_read_only_and_cli_consumer_needs_no_checkout(capsys):
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        _, allocation, receipt = _closed(repository)
        before = _bytes(repository.repo / ".git")
        refs = git(repository.repo, "show-ref")
        arguments = [
            "lifecycle-history",
            "--repo",
            str(repository.repo),
            "--release-receipt-id",
            receipt,
            "--json",
        ]
        assert main(arguments) == 0
        first = capsys.readouterr().out
        assert main(arguments) == 0
        assert capsys.readouterr().out == first
        row = json.loads(first)["decision"]["closed_releases"][0]
        assert row["allocation_id"] == allocation
        assert row["boundary"] == "CLOSED" and row["proof_status"] == "COMPLETE"
        assert main(arguments[:-1]) == 0
        assert f"Closed: {receipt} | {allocation}" in capsys.readouterr().out
        assert _bytes(repository.repo / ".git") == before
        assert git(repository.repo, "show-ref") == refs


def test_publication_receipt_proves_source_owner_separately_from_publisher(monkeypatch):
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        source, allocation, head = _source(
            repository, task="history-source", relative_path="feature.txt", content="synthetic\n"
        )
        queued = _enqueue(repository, task="history-integration", source_allocation=allocation)
        intent_id = queued["decision"]["intent_id"]
        candidate, claimed = _claim(repository, task="history-integration", intent_id=intent_id)
        _validate(repository, candidate, "focused-tests-and-review")
        published = _publish(repository, candidate)
        assert not candidate.exists() and not source.exists()
        history = lifecycle_history(repository.repo, integration_intent_id=intent_id)["decision"]
        assert history["live_allocations"] == []
        row = next(r for r in history["closed_releases"] if r["allocation_id"] == allocation)
        assert row["owner"] == "module-owner"
        assert row["candidate_oid"] == published["decision"]["commit"]
        assert row["integration_intent_id"] == intent_id
        assert row["released_head"] == head
        assert row["source_finalizer_record_id"] == "test-finalizer-record"
        delivery = row["deliveries"][0]
        assert delivery["publisher"] == "integration-owner"
        assert delivery["publication_status"] == "REMOTE_VERIFIED"
        assert delivery["publication_receipt"]["candidate_oid"] == row["candidate_oid"]
        assert (
            delivery["prepared_candidate_receipt"]["tests_receipt_id"]
            == "tests:focused-tests-and-review"
        )
        assert delivery["finalizer_record_id"] == "publication-history-integration"
        assert row["published_at"] and row["released_at"]
        # A Handbook adapter can check exact historical ownership and baseline ancestry.
        assert row["authority_key"] == "history-source" and row["task_key"] == "history-source"
        assert (
            git(repository.repo, "merge-base", "--is-ancestor", row["candidate_oid"], "origin/main")
            == ""
        )
        assert claimed["decision"]["allocation_id"] != allocation
        assert delivery["source_absorption_evidence"][0]["source_handoff_oid"] == head
        assert delivery["prepared_candidate_receipts"] == [delivery["prepared_candidate_receipt"]]
        # Mutable intents may change or be unavailable; immutable receipts retain the join.
        store = MetadataStore(inventory(repository.repo).common_dir)
        snapshot = store.load()
        snapshot.integration_intents.clear()
        monkeypatch.setattr(MetadataStore, "_load_unlocked", lambda self: snapshot)
        repeated = lifecycle_history(repository.repo, integration_intent_id=intent_id)["decision"]
        assert repeated["closed_releases"] == history["closed_releases"]
        prepared = next(iter(snapshot.prepared_candidate_receipts.values()))
        duplicate = replace(
            prepared,
            receipt_id="9eee1e75-1693-42eb-9c71-c1d26b227063",
            tests_receipt_id="tests:another-valid-preparation",
        )
        snapshot.prepared_candidate_receipts[duplicate.receipt_id] = duplicate
        ambiguous = lifecycle_history(repository.repo, allocation_id=allocation)["decision"]
        delivery = ambiguous["closed_releases"][0]["deliveries"][0]
        assert delivery["prepared_candidate_receipt"] is None
        assert len(delivery["prepared_candidate_receipts"]) == 2


def test_history_does_not_take_owner_from_mutable_record_or_new_authority():
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        _, allocation, receipt = _closed(repository)
        store = MetadataStore(inventory(repository.repo).common_dir)
        record = store.load().records[allocation]
        # Simulate a later corrupted owner field: immutable closure still names the real owner.
        with store.lock(exclusive=True):
            store.replace_record(replace(record, owner="other-owner"))
        acquire_one(
            repository, task="new-session", authority="history-authority", owner="new-owner"
        )
        history = lifecycle_history(repository.repo, authority_key="history-authority")["decision"]
        assert history["live_allocations"][0]["owner"] == "new-owner"
        assert history["closed_releases"][0]["owner"] == "module-owner"
        assert history["closed_releases"][0]["release_receipt_id"] == receipt


def test_missing_legacy_receipt_is_incomplete_and_never_backfilled():
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        _, allocation, receipt = _closed(repository)
        store = MetadataStore(inventory(repository.repo).common_dir)
        (store.release_receipts_dir / f"{receipt}.json").unlink()
        before = _bytes(store.root)
        result = lifecycle_history(repository.repo, allocation_id=allocation)["decision"]
        assert result["closed_releases"] == []
        assert result["incomplete_history"][0]["proof_status"] == "INCOMPLETE"
        assert _bytes(store.root) == before


def test_conflicting_release_identity_fails_closed(monkeypatch):
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        _, allocation, receipt_id = _closed(repository)
        store = MetadataStore(inventory(repository.repo).common_dir)
        snapshot = store.load()
        receipt = snapshot.release_receipts[receipt_id]
        snapshot.release_receipts[receipt_id] = replace(receipt, branch="feature/unrelated")
        monkeypatch.setattr(MetadataStore, "_load_unlocked", lambda self: snapshot)
        with pytest.raises(ControllerError) as error:
            lifecycle_history(repository.repo, allocation_id=allocation)
        assert error.value.code == "HISTORY_EVIDENCE_CONFLICT"


def test_task_selector_and_receipt_cursor_preserve_exact_identity():
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        _, first, _ = _closed(repository, task="first")
        _, second, _ = _closed(repository, task="second")
        result = lifecycle_history(repository.repo, authority_key="history-authority", limit=1)[
            "decision"
        ]
        assert len(result["closed_releases"]) == 1 and result["next_after"]
        next_page = lifecycle_history(
            repository.repo, authority_key="history-authority", limit=1, after=result["next_after"]
        )["decision"]
        assert len(next_page["closed_releases"]) == 1 and next_page["next_after"] is None
        assert {
            r["allocation_id"] for r in result["closed_releases"] + next_page["closed_releases"]
        } == {first, second}
        task = lifecycle_history(repository.repo, task_key="first")["decision"]
        assert [r["allocation_id"] for r in task["closed_releases"]] == [first]


@pytest.fixture
def published_history(monkeypatch):
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        _, allocation, _ = _source(
            repository,
            task="history-index-source",
            relative_path="feature.txt",
            content="synthetic\n",
        )
        queued = _enqueue(repository, task="history-index-integration", source_allocation=allocation)
        candidate, _ = _claim(
            repository,
            task="history-index-integration",
            intent_id=queued["decision"]["intent_id"],
        )
        _validate(repository, candidate, "history-index-tests")
        _publish(repository, candidate)
        snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
        receipt = next(r for r in snapshot.release_receipts.values() if r.allocation_id == allocation)
        del snapshot.release_receipts[receipt.receipt_id]
        for number in range(1, 13):
            repeated = replace(receipt, receipt_id=str(UUID(int=number)))
            snapshot.release_receipts[repeated.receipt_id] = repeated
        monkeypatch.setattr(MetadataStore, "_load_unlocked", lambda self: snapshot)
        yield repository, snapshot


def test_history_pagination_serializes_only_returned_deliveries(published_history, monkeypatch):
    repository, _ = published_history
    serialized = []
    real_asdict = history_module.asdict

    def observe_asdict(value):
        if isinstance(value, (PublicationReceipt, PreparedCandidateReceipt)):
            serialized.append(type(value))
        return real_asdict(value)

    monkeypatch.setattr(history_module, "asdict", observe_asdict)
    result = lifecycle_history(repository.repo, authority_key="history-index-source", limit=2)[
        "decision"
    ]
    assert len(result["closed_releases"]) == 2
    assert result["next_after"] == str(UUID(int=2))
    assert serialized.count(PublicationReceipt) == 2
    assert serialized.count(PreparedCandidateReceipt) == 4
    following = lifecycle_history(
        repository.repo, authority_key="history-index-source", limit=2, after=result["next_after"]
    )["decision"]
    assert [r["release_receipt_id"] for r in following["closed_releases"]] == [
        str(UUID(int=3)),
        str(UUID(int=4)),
    ]


def test_history_pagination_still_rejects_conflict_beyond_returned_page(published_history):
    repository, snapshot = published_history
    last = str(UUID(int=12))
    snapshot.release_receipts[last] = replace(
        snapshot.release_receipts[last], branch="feature/incorrect"
    )
    with pytest.raises(ControllerError) as error:
        lifecycle_history(repository.repo, authority_key="history-index-source", limit=2)
    assert error.value.code == "HISTORY_EVIDENCE_CONFLICT"


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"task_key": "a", "authority_key": "b"},
        {"task_key": "a", "limit": 0},
        {"task_key": "a", "limit": 1001},
        {"task_key": "a", "after": ""},
    ],
)
def test_invalid_history_query_is_rejected_without_writing(tmp_path, kwargs):
    with pytest.raises(ControllerError) as error:
        lifecycle_history(tmp_path, **kwargs)
    assert error.value.code == "INVALID_HISTORY_QUERY"
    assert not list(tmp_path.iterdir())


def test_unknown_receipt_returns_no_evidence():
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        result = lifecycle_history(repository.repo, release_receipt_id="missing")["decision"]
        assert (
            result["closed_releases"]
            == result["live_allocations"]
            == result["incomplete_history"]
            == []
        )


@pytest.mark.parametrize("json_output", [False, True])
def test_cli_invalid_query_reports_error_without_traceback(tmp_path, capsys, json_output):
    arguments = ["lifecycle-history", "--repo", str(tmp_path), "--task-key", "a", "--limit", "0"]
    if json_output:
        arguments.append("--json")
    assert main(arguments) == 2
    output = capsys.readouterr().out
    assert "INVALID_HISTORY_QUERY" in output
    assert "Traceback" not in output
    assert not list(tmp_path.iterdir())
