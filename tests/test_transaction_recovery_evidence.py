"""A retained resource-clean journal stops fencing only on exact release evidence."""

import json
import uuid
from pathlib import Path

import pytest

from worktree_controller import transaction_scope as scope
from worktree_controller.store import MetadataStore

REPOSITORY_ID = "11111111-1111-4111-8111-111111111111"


def _store(tmp_path: Path) -> MetadataStore:
    store = MetadataStore(tmp_path / "repo.git")
    store.root.mkdir(parents=True, mode=0o700)
    (store.root / "repo.json").write_text(json.dumps({"repository_id": REPOSITORY_ID}))
    (store.root / "repo.lock").touch(mode=0o600)
    return store


def _journal(store, *, allocation, target, phase="PREPARED", command="resource-clean"):
    transaction = str(uuid.uuid4())
    store.transactions_dir.mkdir(parents=True, exist_ok=True)
    (store.transactions_dir / f"{transaction}.json").write_text(json.dumps({
        "schema_version": 1, "transaction_id": transaction, "repository_id": REPOSITORY_ID,
        "allocation_id": allocation, "target_path": str(target), "command": command,
        "phase": phase, "actions": []}))
    return transaction


def _record(store, allocation, *, version=4, lifecycle="RELEASED"):
    store.records_dir.mkdir(parents=True, exist_ok=True)
    (store.records_dir / f"{allocation}.json").write_text(json.dumps({
        "schema_version": 1, "record_version": version, "allocation_id": allocation,
        "task_key": "task", "authority_key": "authority", "module": "m", "role": "feature",
        "owner": "o", "lifecycle": lifecycle, "created_at": "2026-09-20T00:00:00",
        "last_reviewed_at": "2026-09-20T00:00:00", "handoff_commit": None,
        "finalizer_record_id": None, "base_commit": None, "task_commit": None,
        "finalizer_commit": None, "scope_paths": [], "source_allocations": [],
        "quarantine_expected_commit": None, "quarantine_observed_commit": None,
        "quarantine_reason": None, "recovery_allocation_id": None, "recovery_ref": None,
        "disposable_resources": [], "external_bindings": [], "history": []}))


def _receipt(store, *, allocation, target, version=4, result="RELEASED",
             repository_id=REPOSITORY_ID):
    store.release_receipts_dir.mkdir(parents=True, exist_ok=True)
    (store.release_receipts_dir / f"{uuid.uuid4()}.json").write_text(json.dumps({
        "schema_version": 1, "receipt_id": str(uuid.uuid4()), "repository_id": repository_id,
        "allocation_id": allocation, "target_path": str(target), "released_record_version": version,
        "result": result, "resource_receipt_ids": [], "cleanup_transaction_id": None,
        "release_plan_id": None, "released_at": "2026-09-21T00:00:00"}))


def _lease(store, allocation):
    store.writer_leases_dir.mkdir(parents=True, exist_ok=True)
    (store.writer_leases_dir / f"{allocation}.json").write_text(json.dumps({
        "schema_version": 2, "allocation_id": allocation, "lease_id": str(uuid.uuid4())}))


def _ready(tmp_path):
    store = _store(tmp_path)
    allocation = str(uuid.uuid4())
    target = tmp_path / "worktrees" / "released-resource"
    transaction = _journal(store, allocation=allocation, target=target)
    return store, allocation, target, transaction


def test_exact_release_evidence_makes_the_journal_terminal(tmp_path):
    store, allocation, target, transaction = _ready(tmp_path)
    _record(store, allocation)
    _receipt(store, allocation=allocation, target=target)

    assert scope.release_evidence_terminal(store, transaction) is True
    with store.lock(exclusive=False):
        assert scope.terminal_state(store, transaction) == "RELEASE_EVIDENCE_SUPERSEDED"
        assert scope.pending(store) == []
        assert scope.conflicts(store, allocation=allocation, target=target) == []


def test_retained_journal_bytes_are_never_rewritten(tmp_path):
    store, allocation, target, transaction = _ready(tmp_path)
    _record(store, allocation)
    _receipt(store, allocation=allocation, target=target)
    path = store.transactions_dir / f"{transaction}.json"
    before = path.read_bytes()

    with store.lock(exclusive=False):
        assert scope.pending(store) == []
    assert path.read_bytes() == before
    assert json.loads(before)["phase"] == "PREPARED"


@pytest.mark.parametrize("deviation", [
    "no_receipt", "two_receipts", "wrong_record_version", "wrong_repository",
    "wrong_allocation", "wrong_target", "target_exists", "worktree_registered",
    "writer_lease", "record_still_active", "wrong_command", "wrong_phase",
])
def test_missing_or_contradictory_evidence_keeps_the_journal_pending(tmp_path, deviation):
    store, allocation, target, transaction = _ready(tmp_path)
    _record(store, allocation)
    _receipt(store, allocation=allocation, target=target)
    journal_path = store.transactions_dir / f"{transaction}.json"

    if deviation == "no_receipt":
        for path in store.release_receipts_dir.glob("*.json"):
            path.unlink()
    elif deviation == "two_receipts":
        _receipt(store, allocation=allocation, target=target)
    elif deviation == "wrong_record_version":
        _record(store, allocation, version=5)
    elif deviation in {"wrong_repository", "wrong_allocation", "wrong_target"}:
        # Replace the receipt outright: an additional contradictory receipt must
        # not be mistaken for the one exact release this journal can rely on.
        for path in store.release_receipts_dir.glob("*.json"):
            path.unlink()
        if deviation == "wrong_repository":
            _receipt(store, allocation=allocation, target=target, repository_id=str(uuid.uuid4()))
        elif deviation == "wrong_allocation":
            _receipt(store, allocation=str(uuid.uuid4()), target=target)
        else:
            _receipt(store, allocation=allocation, target=tmp_path / "somewhere-else")
    elif deviation == "target_exists":
        target.mkdir(parents=True)
    elif deviation == "worktree_registered":
        entry = store.root.parents[1] / "worktrees" / "leftover"
        entry.mkdir(parents=True, exist_ok=True)
        (entry / "gitdir").write_text(str(target / ".git") + "\n")
    elif deviation == "writer_lease":
        _lease(store, allocation)
    elif deviation == "record_still_active":
        _record(store, allocation, lifecycle="ACTIVE")
    elif deviation == "wrong_command":
        journal_path.write_text(json.dumps({**json.loads(journal_path.read_text()),
                                           "command": "acquire"}))
    elif deviation == "wrong_phase":
        journal_path.write_text(json.dumps({**json.loads(journal_path.read_text()),
                                            "phase": "WORKTREE_ADDED"}))

    assert scope.release_evidence_terminal(store, transaction) is False
    with store.lock(exclusive=False):
        assert scope.terminal_state(store, transaction) is None
        assert [row["transaction_id"] for row in scope.pending(store)] == [transaction]


def test_foreign_repository_journal_stays_pending(tmp_path):
    store, allocation, target, transaction = _ready(tmp_path)
    _record(store, allocation)
    _receipt(store, allocation=allocation, target=target)
    path = store.transactions_dir / f"{transaction}.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "repository_id": str(uuid.uuid4())}))

    assert scope.release_evidence_terminal(store, transaction) is False


def test_two_journals_on_one_allocation_are_decided_independently(tmp_path):
    """A duplicate journal must not inherit its sibling's terminal evidence."""
    store = _store(tmp_path)
    allocation = str(uuid.uuid4())
    released = tmp_path / "worktrees" / "released"
    present = tmp_path / "worktrees" / "present"
    first = _journal(store, allocation=allocation, target=released)
    second = _journal(store, allocation=allocation, target=present)
    _record(store, allocation)
    _receipt(store, allocation=allocation, target=released)
    present.mkdir(parents=True)

    with store.lock(exclusive=False):
        assert scope.terminal_state(store, first) == "RELEASE_EVIDENCE_SUPERSEDED"
        assert scope.terminal_state(store, second) is None
        assert [row["transaction_id"] for row in scope.pending(store)] == [second]
