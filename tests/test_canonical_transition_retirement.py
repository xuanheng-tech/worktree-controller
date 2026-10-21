"""Retire only refs proved by a canonical transition, without releasing canonical."""

from __future__ import annotations

import os
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_branch_retirement_execution import _authorize, _cas, _result
from tests.test_canonical_transition import frozen_canonical, initial_policy, transition
from tests.test_shadow import GitRepository, git
from worktree_controller import retirement_execution as execution
from worktree_controller.branch_retirement import (
    branch_retirement_status,
    complete_branch_retirement,
    plan_branch_retirement,
    verify_branch_retirement,
)
from worktree_controller.git_backend import inventory
from worktree_controller.metadata_codec import record_to_dict
from worktree_controller.store import MetadataStore
from worktree_controller.writer_lease import describe_holder, new_lease


def transitioned(repository: GitRepository) -> tuple[MetadataStore, str]:
    facts = frozen_canonical(repository)
    transition(repository, facts)
    store = MetadataStore(inventory(repository.repo).common_dir)
    allocation = next(iter(store.load().records))
    return store, allocation


def planned(repository: GitRepository, **extra) -> dict:
    return plan_branch_retirement(
        repository.repo, branch="docs/frozen", remote="origin", integrated_into="main", **extra
    )["decision"]


def test_transition_ref_uses_exact_history_and_keeps_canonical_active(tmp_path: Path) -> None:
    with GitRepository(linked=0, policy_text=initial_policy("main")) as repository:
        store, allocation = transitioned(repository)
        original = store.load()
        decision = planned(repository)
        assert decision["code"] == "BRANCH_RETIREMENT_READY"
        plan = decision["plan"]
        assert plan["registration"] == "CANONICAL_TRANSITION_BACKED"
        assert plan["allocation_id"] == allocation
        assert plan["release_receipt_id"] is None
        assert plan["canonical_transition_evidence"]["old_head"] == plan["expected_oid"]
        assert len(plan["canonical_transition_evidence"]["event_sha256"]) == 64
        assert branch_retirement_status(repository.repo, plan_id=plan["plan_id"])["decision"][
            "code"
        ] == "BRANCH_RETIREMENT_READY"
        assert verify_branch_retirement(
            repository.repo, plan_id=plan["plan_id"], operation="local"
        )["decision"]["code"] == "RETIREMENT_EXECUTABLE"

        grant = _authorize(repository, plan)["authorization"]
        assert _cas(repository, grant["ref"], grant["expected_oid"]) == 0
        result = _result(repository, grant, execution.REF_REMOVED_AT_EXPECTED_OID, tmp_path)
        settled = complete_branch_retirement(
            repository.repo, authorization_id=grant["authorization_id"], token=grant["token"],
            result_path=result,
        )["decision"]
        assert settled["code"] == "RETIREMENT_EXECUTION_COMPLETE"
        assert complete_branch_retirement(
            repository.repo, authorization_id=grant["authorization_id"], token=grant["token"],
            result_path=result,
        )["decision"]["idempotent"] is True
        assert verify_branch_retirement(
            repository.repo, plan_id=plan["plan_id"], operation="local"
        )["decision"]["code"] == "RETIREMENT_OPERATION_COMPLETE"
        remaining = branch_retirement_status(repository.repo, plan_id=plan["plan_id"])["decision"]
        assert remaining["pending_operations"] == ["remote"]
        after = store.load()
        assert after.records == original.records
        assert after.bindings == original.bindings
        assert after.release_receipts == original.release_receipts == {}
        assert git(repository.repo, "symbolic-ref", "--short", "HEAD") == "main"


def test_copied_noncanonical_history_does_not_grant_retirement() -> None:
    with GitRepository(linked=0, policy_text=initial_policy("main")) as repository:
        store, allocation = transitioned(repository)
        record = store.load().records[allocation]
        copied = replace(
            record, allocation_id=str(uuid.uuid4()), role="feature",
            task_key="copied", authority_key="copied",
        )
        store._atomic_json(store.records_dir / f"{copied.allocation_id}.json", record_to_dict(copied))
        store.replace_record(replace(record, history=record.history[:-1]))
        assert planned(repository)["plan"]["blockers"] == ["HISTORICAL_UNREGISTERED"]


@pytest.mark.parametrize("damage", [
    "missing", "duplicate", "old_finalizer_record_id", "old_head", "from", "at", "old_scope_paths",
])
def test_missing_ambiguous_or_invalid_history_never_grants_retirement(damage: str) -> None:
    with GitRepository(linked=0, policy_text=initial_policy("main")) as repository:
        store, allocation = transitioned(repository)
        record = store.load().records[allocation]
        history = list(record.history)
        if damage == "missing":
            history.pop()
        elif damage == "duplicate":
            history.append(history[-1])
        else:
            history[-1] = {**history[-1], damage: None}
        store.replace_record(replace(record, history=tuple(history)))
        decision = planned(repository)
        assert not decision["plan"]["eligible"]
        assert decision["persisted"] is False


def test_unregistered_ancestral_branch_does_not_inherit_transition_authority() -> None:
    with GitRepository(linked=0, policy_text=initial_policy("main")) as repository:
        transitioned(repository)
        git(repository.repo, "branch", "unregistered", "docs/frozen")
        decision = plan_branch_retirement(
            repository.repo, branch="unregistered", remote="origin", integrated_into="main"
        )["decision"]
        assert decision["plan"]["classification"] == "ANCESTRY"
        assert decision["plan"]["blockers"] == ["HISTORICAL_UNREGISTERED"]


@pytest.mark.parametrize("occupancy", ["checkout", "binding", "runtime", "writer", "recovery", "role"])
def test_transition_proof_never_bypasses_occupancy(occupancy: str) -> None:
    with GitRepository(linked=0, policy_text=initial_policy("main")) as repository:
        store, allocation = transitioned(repository)
        snapshot = store.load()
        record = snapshot.records[allocation]
        if occupancy == "checkout":
            git(repository.repo, "worktree", "add", str(repository.root / "occupied"), "docs/frozen")
            blocker = "BRANCH_CHECKED_OUT"
        elif occupancy == "binding":
            store._atomic_json(store.bindings_dir / ("wt_" + "a" * 64 + ".json"), {
                "schema_version": 4,
                "allocation_id": allocation,
            })
            blocker = "ALLOCATION_BOUND"
        elif occupancy == "writer":
            store.write_writer_lease(new_lease(
                repository_id=str(snapshot.repository_id), allocation_id=allocation,
                worktree_path=str(repository.repo), owner=record.owner,
                holder=describe_holder(os.getpid(), client_id="transition-test"), ttl_seconds=None,
            ))
            blocker = "WRITER_LEASE_ACTIVE"
        else:
            changes = {
                "runtime": {"external_bindings": ("runtime:test",)},
                "recovery": {"recovery_ref": "refs/heads/recovery/keep"},
                "role": {"role": "feature"},
            }[occupancy]
            store.replace_record(replace(record, **changes))
            blocker = {
                "runtime": "RUNTIME_BINDING_ACTIVE", "recovery": "RECOVERY_STATE_EXCLUDED",
                "role": "CANONICAL_TRANSITION_ALLOCATION_INVALID",
            }[occupancy]
        decision = planned(repository)
        assert not decision["plan"]["eligible"]
        assert blocker in decision["plan"]["blockers"]


@pytest.mark.parametrize("drift", ["history", "owner", "branch", "publication"])
def test_planned_transition_is_invalidated_by_authority_or_ref_drift(drift: str) -> None:
    with GitRepository(linked=0, policy_text=initial_policy("main")) as repository:
        store, allocation = transitioned(repository)
        plan = planned(repository)["plan"]
        record = store.load().records[allocation]
        if drift == "history":
            event = {**record.history[-1], "old_owner": "different-owner"}
            store.replace_record(replace(record, history=(*record.history[:-1], event)))
        elif drift == "owner":
            store.replace_record(replace(record, owner="different-owner"))
        elif drift == "branch":
            git(repository.repo, "branch", "-f", plan["branch"], "main")
        else:
            git(repository.repo, "update-ref", "refs/remotes/origin/main", plan["expected_oid"])
        verdict = verify_branch_retirement(
            repository.repo, plan_id=plan["plan_id"], operation="local"
        )["decision"]
        assert verdict["code"] == "RETIREMENT_STALE"
        assert not list(store.branch_retirement_receipts_dir.glob("*.json"))


def test_transition_proof_cannot_retire_against_another_integration_target() -> None:
    with GitRepository(linked=0, policy_text=initial_policy("main")) as repository:
        transitioned(repository)
        git(repository.repo, "update-ref", "refs/remotes/origin/other", git(repository.repo, "rev-parse", "main"))
        decision = plan_branch_retirement(
            repository.repo, branch="docs/frozen", remote="origin", integrated_into="other"
        )["decision"]
        assert "CANONICAL_TRANSITION_TARGET_MISMATCH" in decision["plan"]["blockers"]


def test_transition_evidence_drift_after_deletion_is_not_attested(tmp_path: Path) -> None:
    with GitRepository(linked=0, policy_text=initial_policy("main")) as repository:
        store, allocation = transitioned(repository)
        plan = planned(repository)["plan"]
        grant = _authorize(repository, plan)["authorization"]
        assert _cas(repository, grant["ref"], grant["expected_oid"]) == 0
        record = store.load().records[allocation]
        store.replace_record(replace(record, history=record.history[:-1]))
        decision = complete_branch_retirement(
            repository.repo, authorization_id=grant["authorization_id"], token=grant["token"],
            result_path=_result(repository, grant, execution.REF_REMOVED_AT_EXPECTED_OID, tmp_path),
        )["decision"]
        assert decision["code"] == "RETIREMENT_EXECUTION_REFUSED"
        assert "PLAN_STATE_DRIFT" in decision["blockers"]
        assert not list(store.branch_retirement_receipts_dir.glob("*.json"))
