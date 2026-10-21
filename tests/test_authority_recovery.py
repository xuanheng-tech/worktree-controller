"""M1 authority safety: repository lock fail-closed, interrupted-journal terminals, temp debris.

These tests pin the authority semantics introduced for fencing recovery: every
mutation serializes on a usable repo.lock; interrupted acquire/refresh journals
close only on positive current-state evidence observed under a held flock; and
interrupted atomic writes never fence the repository.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from tests.test_shadow import GitRepository, git
from worktree_controller import transaction_scope as scope
from worktree_controller.branch_retirement import _transactions_for_allocation
from worktree_controller.cleanup_recovery import rollout_check
from worktree_controller.controller import adopt, doctor, status
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import acquire, handoff
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore

REPOSITORY_ID = "11111111-1111-4111-8111-111111111111"
OLD_OID = "1" * 40
NEW_OID = "2" * 40
SOURCE = Path(__file__).resolve().parents[1] / "src"
CLI_ENV = {**os.environ, "PYTHONPATH": str(SOURCE), "PYTHONDONTWRITEBYTECODE": "1"}


def adopt_canonical(repository: GitRepository) -> None:
    adopt(
        repository.repo,
        target=repository.repo,
        task_key="repository-control",
        authority_key="repository-control",
        module="repository",
        role="control-plane",
        owner="authority-tests",
        lifecycle="ACTIVE",
    )


def _unit_store(tmp_path: Path) -> tuple[Path, MetadataStore]:
    common = tmp_path / "repo.git"
    store = MetadataStore(common)
    store.root.mkdir(parents=True, mode=0o700)
    (store.root / "repo.json").write_text(
        json.dumps({"schema_version": 1, "repository_id": REPOSITORY_ID}), encoding="utf-8"
    )
    (common / "worktrees").mkdir(parents=True)
    return common, store


def _locked(store: MetadataStore) -> object:
    """Classification is authority: run it inside a genuinely held shared lock."""
    if not os.path.lexists(store.lock_file):
        store.lock_file.touch(mode=0o600)
    return store.lock(exclusive=False)


def _terminal(store: MetadataStore, transaction: str) -> str | None:
    with _locked(store):
        return scope.terminal_state(store, transaction)


def _pending(store: MetadataStore) -> list[dict]:
    with _locked(store):
        return scope.pending(store)


def _journal(
    store: MetadataStore,
    *,
    command: str,
    phase: str,
    allocation: str,
    target: Path,
    branch: str,
    expected_head: str,
    new_head: str | None = None,
    expected_record_version: int | None = None,
    schema_version: int = 1,
) -> str:
    transaction = str(uuid.uuid4())
    store.transactions_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": schema_version,
        "transaction_id": transaction,
        "repository_id": REPOSITORY_ID,
        "command": command,
        "phase": phase,
        "target_path": str(target),
        "allocation_id": allocation,
        "branch": branch,
        "expected_head": expected_head,
        "expected_record_version": expected_record_version,
        "created_at": "2026-09-24T00:00:00+08:00",
        "updated_at": "2026-09-24T00:00:00+08:00",
    }
    if new_head is not None:
        payload["new_head"] = new_head
        payload["intent_id"] = str(uuid.uuid4())
    (store.transactions_dir / f"{transaction}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    return transaction


def _record(store: MetadataStore, allocation: str, task_commit: str | None) -> None:
    store.records_dir.mkdir(parents=True, exist_ok=True)
    (store.records_dir / f"{allocation}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "record_version": 1,
                "allocation_id": allocation,
                "task_commit": task_commit,
                "lifecycle": "ACTIVE",
            }
        ),
        encoding="utf-8",
    )


def _binding(store: MetadataStore, allocation: str, entry: str = "wtX") -> None:
    key = "wt_" + hashlib.sha256(f"worktrees/{entry}".encode()).hexdigest()
    store.bindings_dir.mkdir(parents=True, exist_ok=True)
    (store.bindings_dir / f"{key}.json").write_text(
        json.dumps({"schema_version": 1, "allocation_id": allocation}), encoding="utf-8"
    )


def _register_worktree(common: Path, target: Path, *, head: str | None = None) -> None:
    entry = common / "worktrees" / "wtX"
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "gitdir").write_text(str(target / ".git"), encoding="utf-8")
    if head is not None:
        (entry / "HEAD").write_text(head, encoding="utf-8")


def _unregister_worktree(common: Path) -> None:
    entry = common / "worktrees" / "wtX"
    for name in ("gitdir", "HEAD"):
        (entry / name).unlink(missing_ok=True)
    entry.rmdir()


def _branch(common: Path, branch: str, oid: str | None) -> None:
    ref = common / "refs" / "heads" / branch
    ref.parent.mkdir(parents=True, exist_ok=True)
    if oid is None:
        ref.unlink(missing_ok=True)
    else:
        ref.write_text(oid + "\n", encoding="utf-8")


def _journal_digest(store: MetadataStore, transaction: str) -> str:
    path = store.transactions_dir / f"{transaction}.json"
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- repository lock authority -------------------------------------------------


def test_lock_missing_fails_closed_for_exclusive_only(tmp_path: Path) -> None:
    _, store = _unit_store(tmp_path)
    with pytest.raises(ControllerError) as blocked, store.lock(exclusive=True):
        pass
    assert blocked.value.code == "MISSING_REPOSITORY_LOCK"
    with store.lock(exclusive=False):
        pass
    assert not store.flock_held


def test_lock_anomalies_fail_closed_for_everyone(tmp_path: Path) -> None:
    common, store = _unit_store(tmp_path)
    elsewhere = tmp_path / "elsewhere.lock"
    elsewhere.write_text("", encoding="utf-8")
    store.lock_file.symlink_to(elsewhere)
    for exclusive in (True, False):
        with pytest.raises(ControllerError) as blocked, store.lock(exclusive=exclusive):
            pass
        assert blocked.value.code == "REPOSITORY_LOCK_INVALID"
    store.lock_file.unlink()
    store.lock_file.mkdir()
    for exclusive in (True, False):
        with pytest.raises(ControllerError) as blocked, store.lock(exclusive=exclusive):
            pass
        assert blocked.value.code == "REPOSITORY_LOCK_INVALID"
    assert common.exists()
    assert not store.flock_held


def test_read_only_lock_fails_closed(tmp_path: Path) -> None:
    _, store = _unit_store(tmp_path)
    store.lock_file.touch(mode=0o444)
    for exclusive in (True, False):
        with pytest.raises(ControllerError) as blocked, store.lock(exclusive=exclusive):
            pass
        assert blocked.value.code == "FILESYSTEM_PERMISSION"


def test_read_only_lock_mount_error_is_filesystem_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store = _unit_store(tmp_path)
    store.lock_file.touch()
    original_open = os.open

    def read_only_open(path: str | Path, flags: int, *args: object) -> int:
        if Path(path) == store.lock_file:
            raise OSError(errno.EROFS, "read-only file system", str(path))
        return original_open(path, flags, *args)

    monkeypatch.setattr(os, "open", read_only_open)
    for exclusive in (True, False):
        with pytest.raises(ControllerError) as blocked, store.lock(exclusive=exclusive):
            pass
        assert blocked.value.code == "FILESYSTEM_PERMISSION"


def test_flock_permission_failure_is_not_lock_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store = _unit_store(tmp_path)
    store.lock_file.touch()

    def denied_flock(_descriptor: int, _operation: int) -> None:
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(fcntl, "flock", denied_flock)
    with pytest.raises(ControllerError) as blocked, store.lock(exclusive=False):
        pass
    assert blocked.value.code == "FILESYSTEM_PERMISSION"
    assert not store.flock_held


def test_lock_create_bootstraps_regular_file(tmp_path: Path) -> None:
    _, store = _unit_store(tmp_path)
    assert not store.lock_file.exists()
    with store.lock(exclusive=True, create=True):
        assert store.lock_file.is_file()
        assert store.flock_held
    assert not store.flock_held


def test_exclusive_lock_serializes_across_descriptions(tmp_path: Path) -> None:
    _, store = _unit_store(tmp_path)
    with store.lock(exclusive=True, create=True):
        entered = threading.Event()
        waits: list[float] = []

        def contender() -> None:
            entered.set()
            started = time.monotonic()
            with store.lock(exclusive=True):
                waits.append(time.monotonic() - started)

        thread = threading.Thread(target=contender)
        thread.start()
        entered.wait(timeout=5)
        time.sleep(0.5)
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert waits and waits[0] >= 0.3


def test_shared_readers_wait_for_exclusive_holder(tmp_path: Path) -> None:
    _, store = _unit_store(tmp_path)
    with store.lock(exclusive=True, create=True):
        waits: list[float] = []

        def reader() -> None:
            started = time.monotonic()
            with store.lock(exclusive=False):
                waits.append(time.monotonic() - started)

        thread = threading.Thread(target=reader)
        thread.start()
        time.sleep(0.5)
    thread.join(timeout=10)
    assert waits and waits[0] >= 0.3


def test_classification_requires_held_flock(tmp_path: Path) -> None:
    common, store = _unit_store(tmp_path)
    transaction = _journal(
        store,
        command="acquire",
        phase="PREPARED",
        allocation=str(uuid.uuid4()),
        target=tmp_path / "nothing",
        branch="feature/gate",
        expected_head=OLD_OID,
    )
    unheld = MetadataStore(common)
    with pytest.raises(ControllerError) as blocked:
        scope.pending(unheld)
    assert blocked.value.code == "MISSING_REPOSITORY_LOCK"
    with pytest.raises(ControllerError) as blocked:
        scope.terminal_state(unheld, transaction)
    assert blocked.value.code == "MISSING_REPOSITORY_LOCK"
    with _locked(store):
        assert scope.terminal_state(store, transaction) == "ACQUIRE_NO_EFFECT_SUPERSEDED"


# --- interrupted atomic write debris -------------------------------------------


def test_pending_ignores_atomic_temp_entries(tmp_path: Path) -> None:
    _, store = _unit_store(tmp_path)
    store.transactions_dir.mkdir(parents=True)
    for name in (f".{uuid.uuid4()}.json.aB3dE9f0.tmp", ".hidden.json.12345678.tmp"):
        (store.transactions_dir / name).write_text("partial", encoding="utf-8")
    assert _pending(store) == []
    (store.transactions_dir / "not-a-temp-entry").write_text("x", encoding="utf-8")
    with pytest.raises(ControllerError) as blocked:
        _pending(store)
    assert blocked.value.code == "INTERRUPTED_TRANSACTION"


def test_rollout_check_tolerates_temp_residue() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        transactions = (
            inventory(repository.repo).common_dir / "worktree-controller" / "v1" / "transactions"
        )
        transactions.mkdir(parents=True, exist_ok=True)
        (transactions / f".{uuid.uuid4()}.json.zZ9yXwVu.tmp").write_text(
            "partial", encoding="utf-8"
        )
        report = rollout_check(repository.repo)
        assert report["decision"]["code"] == "ROLLOUT_RECOVERY_COMPATIBLE"


# --- acquire terminal derivation -----------------------------------------------


def test_acquire_completed_journal_is_terminal(tmp_path: Path) -> None:
    common, store = _unit_store(tmp_path)
    target = tmp_path / "checkout"
    allocation = str(uuid.uuid4())
    transaction = _journal(
        store,
        command="acquire",
        phase="METADATA_WRITTEN",
        allocation=allocation,
        target=target,
        branch="feature/x",
        expected_head=OLD_OID,
    )
    assert _terminal(store, transaction) is None
    assert len(_pending(store)) == 1
    _record(store, allocation, OLD_OID)
    _binding(store, allocation)
    assert _terminal(store, transaction) is None  # worktree missing
    _register_worktree(common, target, head="ref: refs/heads/feature/x")
    assert _terminal(store, transaction) is None  # branch ref missing
    target.mkdir()
    assert _terminal(store, transaction) is None  # branch ref still missing
    _branch(common, "feature/x", OLD_OID)
    assert _terminal(store, transaction) == "ACQUIRE_EVIDENCE_SUPERSEDED"
    assert _pending(store) == []
    _branch(common, "feature/x", NEW_OID)
    assert _terminal(store, transaction) is None


def test_acquire_no_effect_journal_closes(tmp_path: Path) -> None:
    _, store = _unit_store(tmp_path)
    transaction = _journal(
        store,
        command="acquire",
        phase="PREPARED",
        allocation=str(uuid.uuid4()),
        target=tmp_path / "checkout",
        branch="feature/y",
        expected_head=OLD_OID,
    )
    assert _terminal(store, transaction) == "ACQUIRE_NO_EFFECT_SUPERSEDED"
    assert _pending(store) == []


def test_acquire_leftovers_block_no_effect_close(tmp_path: Path) -> None:
    common, store = _unit_store(tmp_path)
    target = tmp_path / "checkout"
    allocation = str(uuid.uuid4())
    transaction = _journal(
        store,
        command="acquire",
        phase="PREPARED",
        allocation=allocation,
        target=target,
        branch="feature/y",
        expected_head=OLD_OID,
    )
    assert _terminal(store, transaction) == "ACQUIRE_NO_EFFECT_SUPERSEDED"
    target.mkdir()
    assert _terminal(store, transaction) is None  # residual checkout directory
    target.rmdir()
    _branch(common, "feature/y", OLD_OID)
    assert _terminal(store, transaction) is None  # residual branch ref
    _branch(common, "feature/y", None)
    (common / "refs" / "heads" / "feature" / "y").symlink_to(tmp_path / "dangling")
    assert _terminal(store, transaction) is None  # dangling ref is uncertainty
    (common / "refs" / "heads" / "feature" / "y").unlink()
    _record(store, allocation, OLD_OID)
    assert _terminal(store, transaction) is None  # record exists
    _binding(store, allocation)
    _register_worktree(common, target)
    target.mkdir()
    assert _terminal(store, transaction) is None  # half no-effect/half completion


def test_acquire_orphan_worktree_stays_fenced_until_cleaned(tmp_path: Path) -> None:
    common, store = _unit_store(tmp_path)
    target = tmp_path / "checkout"
    transaction = _journal(
        store,
        command="acquire",
        phase="WORKTREE_ADDED",
        allocation=str(uuid.uuid4()),
        target=target,
        branch="feature/y",
        expected_head=OLD_OID,
    )
    _register_worktree(common, target, head="ref: refs/heads/feature/y")
    assert _terminal(store, transaction) is None
    assert len(_pending(store)) == 1
    _unregister_worktree(common)
    assert _terminal(store, transaction) == "ACQUIRE_NO_EFFECT_SUPERSEDED"


def test_refresh_terminals(tmp_path: Path) -> None:
    common, store = _unit_store(tmp_path)
    target = tmp_path / "checkout"
    target.mkdir()
    allocation = str(uuid.uuid4())
    _register_worktree(common, target, head="ref: refs/heads/feature/r")
    _branch(common, "feature/r", OLD_OID)
    _record(store, allocation, OLD_OID)
    transaction = _journal(
        store,
        command="integration-refresh",
        phase="REFRESH_PREPARED",
        allocation=allocation,
        target=target,
        branch="feature/r",
        expected_head=OLD_OID,
        new_head=NEW_OID,
        expected_record_version=1,
    )
    assert _terminal(store, transaction) == "REFRESH_NO_EFFECT_SUPERSEDED"
    # A dangling detach is a real effect: fenced until the operator reattaches.
    (common / "worktrees" / "wtX" / "HEAD").write_text(NEW_OID, encoding="utf-8")
    assert _terminal(store, transaction) is None
    (common / "worktrees" / "wtX" / "HEAD").write_text(
        "ref: refs/heads/feature/r", encoding="utf-8"
    )
    # Completion shape requires the recorded BRANCH_UPDATED phase too.
    _branch(common, "feature/r", NEW_OID)
    _record(store, allocation, NEW_OID)
    assert _terminal(store, transaction) is None  # phase REFRESH_PREPARED contradicts
    assert len(_pending(store)) == 1


def test_refresh_completion_and_half_states(tmp_path: Path) -> None:
    common, store = _unit_store(tmp_path)
    target = tmp_path / "checkout"
    target.mkdir()
    allocation = str(uuid.uuid4())
    _register_worktree(common, target, head="ref: refs/heads/feature/r")
    transaction = _journal(
        store,
        command="integration-refresh",
        phase="BRANCH_UPDATED",
        allocation=allocation,
        target=target,
        branch="feature/r",
        expected_head=OLD_OID,
        new_head=NEW_OID,
        expected_record_version=1,
    )
    _branch(common, "feature/r", NEW_OID)
    _record(store, allocation, NEW_OID)
    assert _terminal(store, transaction) == "REFRESH_EVIDENCE_SUPERSEDED"
    # Half state: branch moved while the record never advanced stays fenced.
    _record(store, allocation, OLD_OID)
    assert _terminal(store, transaction) is None
    # Branch back at old with the record unchanged is the completion-phase
    # contradiction: an unknown world, fenced.
    _branch(common, "feature/r", OLD_OID)
    assert _terminal(store, transaction) is None


# --- classifier accepts only journals pending itself would validate ------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(schema_version=99),
        lambda payload: payload.update(repository_id=str(uuid.uuid4())),
        lambda payload: payload.update(target_path="relative/path"),
        lambda payload: payload.update(expected_head="not-an-oid"),
        lambda payload: payload.update(branch=""),
        lambda payload: payload.update(expected_head=None),
    ],
)
def test_invalid_journals_are_never_classified_terminal(tmp_path: Path, mutate) -> None:
    _, store = _unit_store(tmp_path)
    allocation = str(uuid.uuid4())
    transaction = _journal(
        store,
        command="acquire",
        phase="PREPARED",
        allocation=allocation,
        target=tmp_path / "nothing",
        branch="feature/v",
        expected_head=OLD_OID,
    )
    path = store.transactions_dir / f"{transaction}.json"
    payload = json.loads(path.read_text())
    mutate(payload)
    path.write_text(json.dumps(payload))
    with _locked(store):
        assert scope.terminal_state(store, transaction) is None
        try:
            rows = scope.pending(store)
        except ControllerError as exc:
            assert exc.code == "INTERRUPTED_TRANSACTION"
        else:
            assert len(rows) == 1  # still fenced as a pending journal


def test_reftable_backend_disables_filesystem_closures(tmp_path: Path) -> None:
    common, store = _unit_store(tmp_path)
    (common / "config").write_text(
        '[core]\n\trepositoryformatversion = 1\n[extensions]\n\trefStorage = reftable\n',
        encoding="utf-8",
    )
    transaction = _journal(
        store,
        command="acquire",
        phase="PREPARED",
        allocation=str(uuid.uuid4()),
        target=tmp_path / "nothing",
        branch="feature/reftable",
        expected_head=OLD_OID,
    )
    assert _terminal(store, transaction) is None


def test_classification_never_rewrites_journal_bytes(tmp_path: Path) -> None:
    _, store = _unit_store(tmp_path)
    transaction = _journal(
        store,
        command="acquire",
        phase="METADATA_WRITTEN",
        allocation=str(uuid.uuid4()),
        target=tmp_path / "nothing",
        branch="feature/immutable",
        expected_head=OLD_OID,
    )
    before = _journal_digest(store, transaction)
    for _ in range(3):
        _terminal(store, transaction)
        _pending(store)
    assert _journal_digest(store, transaction) == before


def test_retirement_gate_follows_pending_semantics(tmp_path: Path) -> None:
    common, store = _unit_store(tmp_path)
    target = tmp_path / "checkout"
    allocation = str(uuid.uuid4())
    transaction = _journal(
        store,
        command="acquire",
        phase="METADATA_WRITTEN",
        allocation=allocation,
        target=target,
        branch="feature/g",
        expected_head=OLD_OID,
    )
    with _locked(store):
        assert _transactions_for_allocation(store, allocation) == (transaction,)
    _record(store, allocation, OLD_OID)
    _binding(store, allocation)
    _register_worktree(common, target, head="ref: refs/heads/feature/g")
    _branch(common, "feature/g", OLD_OID)
    target.mkdir()
    with _locked(store):
        assert scope.terminal_state(store, transaction) == "ACQUIRE_EVIDENCE_SUPERSEDED"
        assert _transactions_for_allocation(store, allocation) == ()


# --- end-to-end CLI behaviour --------------------------------------------------


def _lock_path(repository: GitRepository) -> Path:
    return inventory(repository.repo).common_dir / "worktree-controller" / "v1" / "repo.lock"


def _codes(diagnosis: dict) -> set[str]:
    return {warning["code"] for warning in diagnosis["warnings"]} | {
        error["code"] for error in diagnosis["errors"]
    }


def test_missing_lock_blocks_mutation_and_doctor_reports_it() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target = repository.root / "handoff-victim"
        payload = acquire(
            repository.repo,
            task_key="handoff-victim",
            authority_key="handoff-victim",
            module="module",
            role="feature",
            owner="module-owner",
            branch="feature/handoff-victim",
            base_ref="HEAD",
            target=target,
        )
        assert payload["decision"]["code"] == "ACQUIRED"
        lock = _lock_path(repository)
        assert lock.is_file()
        lock.unlink()
        diagnosis = doctor(repository.repo)
        assert diagnosis["decision"]["code"] == "FAIL"
        assert "REPOSITORY_LOCK_MISSING" in _codes(diagnosis)
        assert diagnosis["mount_diagnostics"]["current"]["lock_shape"] == "MISSING"
        assert diagnosis["mount_diagnostics"]["current"]["lock_parent_mount"] == "RW"
        assert diagnosis["decision"]["primary_diagnosis"] == "CONTROLLER_LOCK_AUTHORITY"
        # Reads still answer while the lock is gone.
        observed = status(repository.repo)
        assert observed["repository"]["repository_id"]
        assert "REPOSITORY_LOCK_MISSING" in _codes(observed)
        with pytest.raises(ControllerError) as blocked:
            handoff(
                repository.repo,
                target=target,
                expected_record_version=1,
                kind="integration-ready",
                commit=git(target, "rev-parse", "HEAD"),
                finalizer_record_id="authority-test-record",
            )
        assert blocked.value.code == "MISSING_REPOSITORY_LOCK"
        assert doctor(repository.repo)["decision"]["code"] == "FAIL"
        # A bootstrap path (adopt/acquire/enqueue-integration) recreates the
        # arbiter and full mutation authority returns.
        replay = acquire(
            repository.repo,
            task_key="rebootstrapped",
            authority_key="rebootstrapped",
            module="module",
            role="feature",
            owner="module-owner",
            branch="feature/rebootstrapped",
            base_ref="HEAD",
            target=repository.root / "rebootstrapped",
        )
        assert replay["decision"]["code"] == "ACQUIRED"
        assert lock.is_file()
        assert "REPOSITORY_LOCK_MISSING" not in _codes(doctor(repository.repo))


def test_symlinked_lock_is_invalid_for_everyone() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        lock = _lock_path(repository)
        elsewhere = repository.root / "elsewhere.lock"
        elsewhere.write_text("", encoding="utf-8")
        lock.unlink()
        lock.symlink_to(elsewhere)
        diagnosis = doctor(repository.repo)
        assert diagnosis["decision"]["code"] == "FAIL"
        assert "REPOSITORY_LOCK_INVALID" in _codes(diagnosis)
        with pytest.raises(ControllerError) as blocked:
            adopt(
                repository.repo,
                target=repository.repo,
                task_key="repository-control",
                authority_key="repository-control",
                module="repository",
                role="control-plane",
                owner="authority-tests",
                lifecycle="ACTIVE",
            )
        assert blocked.value.code == "REPOSITORY_LOCK_INVALID"


def test_temp_residue_never_fences_mutations() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        transactions = (
            inventory(repository.repo).common_dir / "worktree-controller" / "v1" / "transactions"
        )
        transactions.mkdir(parents=True, exist_ok=True)
        (transactions / f".{uuid.uuid4()}.json.aB3dE9f0.tmp").write_text(
            "partial", encoding="utf-8"
        )
        payload = acquire(
            repository.repo,
            task_key="temp-tolerant",
            authority_key="temp-tolerant",
            module="module",
            role="feature",
            owner="module-owner",
            branch="feature/temp-tolerant",
            base_ref="HEAD",
            target=repository.root / "temp-tolerant",
        )
        assert payload["decision"]["code"] == "ACQUIRED"
        diagnosis = doctor(repository.repo)
        assert "ATOMIC_TEMP_RESIDUE" in _codes(diagnosis)
        assert diagnosis["decision"]["code"] != "FAIL"
        assert diagnosis["decision"]["transactions"]["unresolved_count"] == 0


def test_cli_serializes_behind_a_held_repository_lock() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        ready = repository.root / "holder-ready"
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    f"import sys, time; sys.path.insert(0, {str(SOURCE)!r});"
                    "from pathlib import Path;"
                    "from worktree_controller.store import MetadataStore;"
                    "from worktree_controller.git_backend import inventory;"
                    f"store = MetadataStore(inventory(Path({str(repository.repo)!r})).common_dir);"
                    "context = store.lock(exclusive=True);"
                    "context.__enter__();"
                    f"Path({str(ready)!r}).write_text('held');"
                    "time.sleep(2.0)"
                ),
            ],
            env=CLI_ENV,
        )
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert ready.exists(), "holder never acquired the lock"
            started = time.monotonic()
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "worktree_controller.cli",
                    "status",
                    "--repo",
                    str(repository.repo),
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                env=CLI_ENV,
                check=False,
            )
            waited = time.monotonic() - started
        finally:
            holder.wait(timeout=30)
        assert result.returncode == 0, result.stderr
        assert waited >= 1.0  # the CLI status waited for the exclusive holder to finish


def test_scoped_pending_classifies_only_relevant_journals(tmp_path: Path, monkeypatch) -> None:
    _, store = _unit_store(tmp_path)
    mine, other = str(uuid.uuid4()), str(uuid.uuid4())
    own = _journal(store, command="acquire", phase="PREPARED", allocation=mine,
                   target=tmp_path / "mine", branch="feature/mine", expected_head=OLD_OID)
    _journal(store, command="acquire", phase="PREPARED", allocation=other,
             target=tmp_path / "other", branch="feature/other", expected_head=OLD_OID)
    classified = []
    monkeypatch.setattr(scope, "terminal", lambda _store, transaction, **_: classified.append(transaction))
    with _locked(store):
        assert [row["transaction_id"] for row in scope.conflicts(
            store, allocation=mine, target=tmp_path / "mine")] == [own]
        assert classified == [own]
        assert _transactions_for_allocation(store, mine) == (own,)
        assert classified == [own, own]
        assert len(scope.conflicts(store, allocation=None, target=tmp_path)) == 2


def test_scoped_pending_still_refuses_any_unknown_journal(tmp_path: Path) -> None:
    _, store = _unit_store(tmp_path)
    mine = str(uuid.uuid4())
    _journal(store, command="acquire", phase="PREPARED", allocation=mine,
             target=tmp_path / "mine", branch="feature/mine", expected_head=OLD_OID)
    (store.transactions_dir / f"{uuid.uuid4()}.json").write_text("{}", encoding="utf-8")
    with _locked(store), pytest.raises(scope.TransactionConflict):
        _transactions_for_allocation(store, mine)
