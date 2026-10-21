import json
import os
from pathlib import Path

import pytest

from tests.test_cleanup_attempts import legacy_started, reconcile
from tests.test_cleanup_recovery import interrupted as interrupted
from tests.test_controlled import acquire_one
from tests.test_source_preservation import source as source
from tests.test_source_recovery import fail_before_action, record
from tests.test_typed_disposable import target as target
from worktree_controller import activation_layout as layout
from worktree_controller import cleanup_evidence as e
from worktree_controller import cleanup_recovery as legacy
from worktree_controller import lifecycle
from worktree_controller import metadata_relocation as relocation
from worktree_controller import source_cleanup as c
from worktree_controller import source_recovery as r
from worktree_controller import store as storage
from worktree_controller import transaction_scope as scope
from worktree_controller.models import ControllerError


def prepared(source, monkeypatch, tmp_path, *, terminal=False):
    repository = source[0].repo
    current = storage.MetadataStore(repository / ".git").root
    old = current.parent.parent / "previous-controller/v1"
    old.parent.mkdir()
    current.rename(old)
    with monkeypatch.context() as previous:
        previous.setattr(storage, "ROOT_NAME", "previous-controller")
        cleanup, failed = fail_before_action(source, previous)
        if terminal:
            record(source, failed)
    change = layout_for(repository, monkeypatch, tmp_path)
    return change, old, current, cleanup, failed


def layout_for(repository, monkeypatch, tmp_path):
    policy = repository / ".agents/worktree-policy.toml"
    previous_policy = repository / ".previous/worktree-policy.toml"
    previous_policy.parent.mkdir()
    policy.rename(previous_policy)
    binary = tmp_path / "previous-command"
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setattr(layout, "refuse_running_entries", lambda names: None)
    change = layout.LayoutMigration.prepare(
        tmp_path / "activation/receipt.json",
        {repository: repository / ".git"},
        previous_namespace="previous-controller",
        previous_policy=".previous/worktree-policy.toml",
        previous_entries={binary: layout.digest(binary.read_bytes())},
        entries={tmp_path / "current-command": b"#!/bin/sh\nexit 0\n"},
        runtimes={"before": "old", "after": "new"},
    )
    return change


def test_completed_legacy_attempt_chain_remains_terminal_after_migration(
    request, monkeypatch, tmp_path
):
    with monkeypatch.context() as previous:
        previous.setattr(storage, "ROOT_NAME", "previous-controller")
        state = request.getfixturevalue("interrupted")
        ref, _, _ = legacy_started(state)
        reconcile(state, ref, execute=True)
        result = legacy.resume(
            state[0].repo,
            recovery_path=Path(ref["path"]),
            recovery_sha=ref["sha256"],
            dry_run=False,
        )
        assert result["decision"]["code"] == "RECOVERY_CLEANED"
        original = relocation.inventory(state[3].root)
    change = layout_for(state[0].repo, monkeypatch, tmp_path)
    change.apply()
    current = storage.MetadataStore(state[0].repo / ".git")
    assert relocation.inventory(current.root) == original
    with current.lock(exclusive=False):
        assert (
            scope.terminal_state(
                current, state[4]["original_transaction_id"], verify_generations=True
            )
            == "RECOVERY_CLEANED"
        )
        assert scope.pending(current) == []
    change.rollback()
    assert relocation.inventory(state[3].root) == original


@pytest.mark.parametrize("terminal", [False, True])
def test_layout_keeps_exact_old_evidence_and_no_action_identity(
    source, monkeypatch, tmp_path, terminal
):
    change, old, new, cleanup, failed = prepared(source, monkeypatch, tmp_path, terminal=terminal)
    original = relocation.inventory(old)
    expected = change.document["repositories"][0]["transaction_compatibility"]
    change.apply()
    assert not old.exists()
    assert relocation.inventory(new) == original
    assert layout.quiet(new, source[0].repo) == expected
    receipt = failed["receipt"]
    assert e.read(Path(receipt["path"]), receipt["sha256"])[1] == receipt["sha256"]
    store = c._store(cleanup["preservation_plan"])
    if not terminal:
        planned, recovered = record(source, failed)
        assert planned["plan_id"] == expected[0]["plan_id"]
        assert recovered["new_attempt_id"] != failed["transaction_id"]
    with store.lock(exclusive=False):
        assert scope.terminal(store, failed["transaction_id"], verify_generations=True)
    retry = c.plan_cleanup(cleanup["preservation_plan"], cleanup["preservation_receipt_sha256"])
    assert retry["result"] == "SOURCE_CLEANUP_READY"
    done = c.cleanup_source(retry)
    assert done["result"] == "SOURCE_CLEANUP_COMPLETE"
    assert done["transaction_id"] != failed["transaction_id"]
    assert all(relocation.inventory(new)[path] == digest for path, digest in original.items())
    with store.lock(exclusive=False):
        assert scope.pending(store) == []
    with pytest.raises(layout.LayoutError, match="new or changed cleanup evidence"):
        change.rollback()
    assert new.exists() and not old.exists()
    monkeypatch.setattr(lifecycle, "runtime_references", lambda *a, **k: ())
    monkeypatch.setattr(lifecycle, "editable_references", lambda *a, **k: ())
    with pytest.raises(ControllerError, match="cleanup is not task completion"):
        lifecycle.complete_no_change(
            source[0].repo, target=source[1], expected_record_version=1, owner="module-owner"
        )
    completed = lifecycle.close_preserved(
        source[0].repo, target=source[1], expected_record_version=1, owner="module-owner",
    )
    assert completed["decision"]["code"] == "PRESERVED"
    assert completed["decision"]["task_completed"] is False
    released = lifecycle.release(
        source[0].repo, target=source[1], expected_record_version=2, owner_release="module-owner"
    )
    assert released["decision"]["code"] == "RELEASED"
    assert not source[1].exists()
    assert all(relocation.inventory(new)[path] == digest for path, digest in original.items())


def test_layout_rollback_preserves_failed_attempt_without_reconciliation(
    source, monkeypatch, tmp_path
):
    change, old, new, _, _ = prepared(source, monkeypatch, tmp_path)
    original = relocation.inventory(old)
    change.apply()
    change.rollback()
    assert old.exists() and not new.exists()
    assert relocation.inventory(old) == original
    assert not (old / relocation.RECORD).exists()


@pytest.mark.parametrize("drift", ["source", "index", "generation", "actions", "unknown-journal"])
def test_layout_drift_blocks_before_moving_authority(source, monkeypatch, tmp_path, drift):
    change, old, new, cleanup, failed = prepared(source, monkeypatch, tmp_path)
    if drift == "source":
        (source[1] / "src/plain.py").write_bytes(b"changed")
    elif drift == "index":
        from tests.test_shadow import git

        git(source[1], "add", "src/plain.py")
    elif drift == "generation":
        path = Path(cleanup["preservation_plan"]["destination"]) / "tree/src/plain.py"
        path.chmod(0o600)
        path.write_bytes(b"changed")
    elif drift == "actions":
        e.write(old / "source-cleanup" / failed["transaction_id"], "before.json", {})
    else:
        e.write(old / "transactions", "unknown.json", {})
    with pytest.raises(layout.LayoutError):
        change.apply()
    assert old.exists() and not new.exists()


@pytest.mark.parametrize("drift", ["missing-map", "map", "receipt", "symlink", "old-authority"])
def test_relocated_reference_requires_exact_inventory_and_one_authority(
    source, monkeypatch, tmp_path, drift
):
    change, old, new, _, failed = prepared(source, monkeypatch, tmp_path)
    change.apply()
    reference = failed["receipt"]
    current = new / Path(reference["path"]).relative_to(old)
    mapping = new / relocation.RECORD
    sentinel = tmp_path / "external-sentinel"
    sentinel.write_bytes(b"outside untouched")
    if drift == "missing-map":
        mapping.unlink()
    elif drift == "map":
        value = json.loads(mapping.read_bytes())
        value["previous_root"] = str(tmp_path / "unrelated")
        mapping.chmod(0o600)
        mapping.write_text(json.dumps(value))
    elif drift == "receipt":
        current.chmod(0o600)
        current.write_bytes(b"{}")
    elif drift == "symlink":
        current.unlink()
        current.symlink_to(sentinel)
    else:
        old.mkdir()
    with pytest.raises((ControllerError, OSError)):
        e.read(Path(reference["path"]), reference["sha256"])
    assert sentinel.read_bytes() == b"outside untouched"


def test_uninventoried_path_cannot_gain_a_relocation(source, monkeypatch, tmp_path):
    change, old, new, _, _ = prepared(source, monkeypatch, tmp_path)
    change.apply()
    added = e.write(new / "source-cleanup", "new.json", {})
    with pytest.raises(ControllerError):
        e.read(old / "source-cleanup/new.json", added["sha256"])
    assert e.read(Path(added["path"]), added["sha256"])[0] == {}


def test_interruption_before_namespace_move_keeps_rollback_possible(source, monkeypatch, tmp_path):
    change, old, new, _, _ = prepared(source, monkeypatch, tmp_path)
    original = os.rename

    def fail(source_path, destination):
        if source_path == old:
            raise OSError("fixture interruption")
        return original(source_path, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, "rename", fail)
        with pytest.raises(OSError):
            change.apply()
    assert (old / relocation.RECORD).exists()
    change.rollback()
    assert old.exists() and not new.exists()
    assert not (old / relocation.RECORD).exists()


def test_rollback_rename_failure_keeps_reference_mapping_for_retry(source, monkeypatch, tmp_path):
    change, old, new, _, failed = prepared(source, monkeypatch, tmp_path)
    original = relocation.inventory(old)
    change.apply()
    rename = os.rename

    def fail(source_path, destination):
        if source_path == new:
            raise OSError("fixture rollback rename failure")
        return rename(source_path, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, "rename", fail)
        with pytest.raises(OSError):
            change.rollback()
    assert (new / relocation.RECORD).is_file()
    assert e.read(Path(failed["receipt"]["path"]), failed["receipt"]["sha256"])
    change.rollback()
    assert relocation.inventory(old) == original
    assert not new.exists() and not (old / relocation.RECORD).exists()


@pytest.mark.parametrize("same_allocation", [False, True])
def test_migration_read_view_converts_lease_schema_without_losing_authority(
    source, monkeypatch, tmp_path, same_allocation
):
    other, acquired = acquire_one(source[0], task="other-writer", authority="other-writer")
    change, old, new, cleanup, failed = prepared(source, monkeypatch, tmp_path)
    identity = cleanup["preservation_plan"]["authority"]["identity"]
    allocation = (
        identity["allocation_id"] if same_allocation else acquired["decision"]["allocation_id"]
    )
    value = {
        "schema_version": 1,
        "lease_id": "e44aa4c7-445a-41e5-a028-d865b297c652",
        "repository_id": identity["repository_id"],
        "allocation_id": allocation,
        "worktree_path": str(source[1] if same_allocation else other),
        "owner": "module-owner",
        "holder": {
            "provider": "fixture-client",
            "pid": 99999999,
            "boot_id": "fixture-boot",
            "start_ticks": 123,
            "session_id": "fixture-session",
            "run_id": "fixture-run",
        },
        "acquired_at": "2026-09-13T10:00:00+08:00",
        "renewed_at": "2026-09-13T10:00:00+08:00",
        "expires_at": "2099-09-13T18:00:00+08:00",
        "record_version": 1,
    }
    lease = old / "writer-leases" / f"{allocation}.json"
    lease.parent.mkdir(exist_ok=True)
    original = (json.dumps(value) + "\n").encode()
    lease.write_bytes(original)
    with monkeypatch.context() as previous:
        previous.setattr(storage, "ROOT_NAME", "previous-controller")
        with pytest.raises(ControllerError):
            storage.MetadataStore(source[0].repo / ".git").load()
    if same_allocation:
        with pytest.raises(layout.LayoutError) as exc:
            change.apply()
        assert getattr(exc.value.__cause__, "code", None) == "MANAGED_REFERENCE"
        assert old.exists() and not new.exists() and lease.read_bytes() == original
        return
    change.apply()
    parsed = storage.MetadataStore(source[0].repo / ".git").load().writer_leases[allocation]
    assert parsed.holder.client_id == "fixture-client"
    assert parsed.worktree_path == str(other)
    planned = r.plan_reconciliation(
        source[0].repo, failed["transaction_id"], failed["receipt"]["sha256"]
    )
    assert planned["state"] == r.STATE
    # A failed inverse move leaves a mixed rollout window: leases have already
    # returned to schema 1, while metadata and its reference map remain at new.
    rename = os.rename
    with monkeypatch.context() as patch:

        def fail(source_path, destination):
            if source_path == new:
                raise OSError("fixture inverse move failure")
            return rename(source_path, destination)

        patch.setattr(os, "rename", fail)
        with pytest.raises(OSError):
            change.rollback()
    change.rollback()
    assert lease.read_bytes() == original
    assert not new.exists()
