from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_auto_release import POLICY_MANUAL, _publish_source, _release_intent_for_allocation
from tests.test_controlled import _configure_remote, acquire_one, adopt_canonical
from tests.test_shadow import GitRepository, git
from worktree_controller.branch_retirement import (
    _classify_integration,
    branch_retirement_status,
    plan_branch_retirement,
    record_branch_retirement,
)
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import reconcile_release_intents, release
from worktree_controller.store import MetadataStore
from worktree_controller.writer_lease import describe_holder, new_lease


def _released_source(repository: GitRepository, name: str = "retirement") -> tuple[str, str]:
    source, allocation, head, _candidate, _published = _publish_source(
        repository,
        source_task=f"{name}-source",
        integration_task=f"{name}-integration",
    )
    intent = _release_intent_for_allocation(repository, allocation)
    released = release(
        repository.repo,
        target=source,
        expected_record_version=2,
        owner_release=None,
        release_intent_id=intent.intent_id,
    )
    assert released["decision"]["code"] == "RELEASED"
    return allocation, head


def test_released_ancestry_branch_gets_persisted_oid_bound_plan() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        allocation, head = _released_source(repository)

        payload = plan_branch_retirement(
            repository.repo,
            allocation_id=allocation,
            remote="origin",
            integrated_into="main",
        )

        decision = payload["decision"]
        plan = decision["plan"]
        assert decision["code"] == "BRANCH_RETIREMENT_READY"
        assert decision["persisted"] is True
        assert plan["expected_oid"] == head
        assert plan["classification"] == "ANCESTRY"
        assert plan["local_ref_state"] == "PRESENT_EXPECTED"
        assert plan["remote_tracking_state"] == "ABSENT"
        assert decision["finalizer"]["local"][1:3] == [
            "--retire-local-branch",
            "feature/retirement-source",
        ]

        status = branch_retirement_status(repository.repo, plan_id=plan["plan_id"])
        assert status["decision"]["code"] == "BRANCH_RETIREMENT_READY"
        assert status["decision"]["pending_operations"] == ["local", "remote"]

        acquire_one(repository, task="unrelated-active", authority="unrelated-active")
        still_ready = branch_retirement_status(repository.repo, plan_id=plan["plan_id"])
        assert still_ready["decision"]["code"] == "BRANCH_RETIREMENT_READY"


@pytest.mark.parametrize("reconciled", [False, True], ids=["pending", "stale"])
def test_manual_release_resolves_matching_intent_for_retirement(reconciled: bool) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        source, allocation, head, _candidate, _published = _publish_source(
            repository, source_task="manual-source", integration_task="manual-integration"
        )
        store = MetadataStore(inventory(repository.repo).common_dir)
        intent = _release_intent_for_allocation(repository, allocation)
        released = release(
            repository.repo,
            target=source,
            expected_record_version=intent.expected_record_version,
            owner_release=intent.owner,
        )
        assert released["decision"]["code"] == "RELEASED"
        assert not source.exists()
        if reconciled:
            reconcile_release_intents(
                repository.repo, actor="fixture", trigger="manual", intent_ids=(intent.intent_id,)
            )
        snapshot = store.load()
        original_intent = snapshot.release_intents[intent.intent_id]
        assert original_intent.status == ("STALE" if reconciled else "PENDING")

        first = plan_branch_retirement(
            repository.repo, allocation_id=allocation, remote="origin", integrated_into="main"
        )["decision"]
        assert first["code"] == "BRANCH_RETIREMENT_READY"
        assert first["plan"]["expected_oid"] == head
        repeated = branch_retirement_status(repository.repo, plan_id=first["plan"]["plan_id"])
        assert repeated["decision"]["code"] == "BRANCH_RETIREMENT_READY"
        assert store.load().release_intents[intent.intent_id] == original_intent
        assert store.load().release_receipts == snapshot.release_receipts


@pytest.mark.parametrize("drift", ["head", "owner", "record-version", "missing-plan"])
def test_release_receipt_does_not_resolve_different_intent_identity(drift: str) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        allocation, _head = _released_source(repository, "different-intent")
        store = MetadataStore(inventory(repository.repo).common_dir)
        snapshot = store.load()
        intent = _release_intent_for_allocation(repository, allocation)
        changes = {"status": "STALE", "blocker": "RELEASE_INTENT_STALE"}
        if drift == "head":
            changes["expected_head"] = git(repository.repo, "rev-parse", "main")
        elif drift == "owner":
            changes["owner"] = "different-owner"
        elif drift == "record-version":
            changes["expected_record_version"] = intent.expected_record_version + 1
        else:
            receipt = next(
                r for r in snapshot.release_receipts.values() if r.allocation_id == allocation
            )
            (store.release_plans_dir / f"{receipt.plan_id}.json").unlink()
        store.replace_release_intent(replace(intent, **changes))

        decision = plan_branch_retirement(
            repository.repo, allocation_id=allocation, remote="origin", integrated_into="main"
        )["decision"]
        assert decision["code"] == "BRANCH_RETIREMENT_BLOCKED"
        assert "RELEASE_INTENT_UNRESOLVED" in decision["plan"]["blockers"]


def test_recheckout_and_oid_drift_fail_closed_after_plan() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        allocation, _head = _released_source(repository, "drift")
        ready = plan_branch_retirement(
            repository.repo,
            allocation_id=allocation,
            remote="origin",
            integrated_into="main",
        )["decision"]["plan"]

        checkout = repository.root / "rechecked-out"
        git(repository.repo, "worktree", "add", str(checkout), "feature/drift-source")
        rechecked = branch_retirement_status(repository.repo, plan_id=ready["plan_id"])
        assert rechecked["decision"]["code"] == "BRANCH_RETIREMENT_DRIFTED"
        assert "BRANCH_CHECKED_OUT" in rechecked["decision"]["current"]["blockers"]
        git(repository.repo, "worktree", "remove", str(checkout))

        git(repository.repo, "branch", "-f", "feature/drift-source", "main")
        drifted = branch_retirement_status(repository.repo, plan_id=ready["plan_id"])
        assert drifted["decision"]["code"] == "BRANCH_RETIREMENT_DRIFTED"
        assert "LOCAL_OID_DRIFT" in drifted["decision"]["current"]["blockers"]


def test_allocation_runtime_and_writer_occupancy_block_planning() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        allocation, _head = _released_source(repository, "occupied")
        store = MetadataStore(inventory(repository.repo).common_dir)

        binding = store.bindings_dir / ("wt_" + "a" * 64 + ".json")
        store._atomic_json(
            binding,
            {"schema_version": 4, "allocation_id": allocation},
        )
        bound = plan_branch_retirement(
            repository.repo,
            allocation_id=allocation,
            remote="origin",
            integrated_into="main",
        )["decision"]
        assert bound["code"] == "BRANCH_RETIREMENT_BLOCKED"
        assert "ALLOCATION_BOUND" in bound["plan"]["blockers"]
        binding.unlink()

        snapshot = store.load()
        record = snapshot.records[allocation]
        store.replace_record(replace(record, external_bindings=("runtime:test",)))
        runtime = plan_branch_retirement(
            repository.repo,
            allocation_id=allocation,
            remote="origin",
            integrated_into="main",
        )["decision"]
        assert "RUNTIME_BINDING_ACTIVE" in runtime["plan"]["blockers"]
        store.replace_record(record)

        store.write_writer_lease(
            new_lease(
                repository_id=str(snapshot.repository_id),
                allocation_id=allocation,
                worktree_path=str(repository.root / "released-source"),
                owner=record.owner,
                holder=describe_holder(os.getpid(), client_id="retirement-test"),
                ttl_seconds=None,
            )
        )
        writer = plan_branch_retirement(
            repository.repo,
            allocation_id=allocation,
            remote="origin",
            integrated_into="main",
        )["decision"]
        assert "WRITER_LEASE_ACTIVE" in writer["plan"]["blockers"]


def test_local_and_remote_results_record_independently_and_idempotently() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        allocation, _head = _released_source(repository, "receipts")
        plan = plan_branch_retirement(
            repository.repo,
            allocation_id=allocation,
            remote="origin",
            integrated_into="main",
        )["decision"]["plan"]

        def summary(operation: str, result: str) -> Path:
            path = repository.root / f"{operation}-summary.json"
            path.write_text(
                json.dumps(
                    {
                        "finalizer_version": "1.1.0",
                        "mode": f"retire_{operation}_branch",
                        "status": "success",
                        "mode_result": {
                            "retirement_plan_id": plan["plan_id"],
                            "result": result,
                            "branch": plan["branch"],
                            "remote": plan["remote"],
                            "expected_oid": plan["expected_oid"],
                            "integrated_into_oid": plan["expected_integrated_oid"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            return path

        # A receipt now needs a ref the Controller can observe as gone: an
        # unauthorized or unattempted deletion cannot be filed as history.
        local_summary = summary("local", "LOCAL_BRANCH_RETIRED_VERIFIED")
        git(
            repository.repo,
            "update-ref",
            "-d",
            f"refs/heads/{plan['branch']}",
            plan["expected_oid"],
        )
        first = record_branch_retirement(
            repository.repo,
            plan_id=plan["plan_id"],
            operation="local",
            summary_path=local_summary,
        )
        repeated = record_branch_retirement(
            repository.repo,
            plan_id=plan["plan_id"],
            operation="local",
            summary_path=local_summary,
        )
        assert first["decision"]["idempotent"] is False
        assert repeated["decision"]["idempotent"] is True
        partial = branch_retirement_status(repository.repo, plan_id=plan["plan_id"])
        assert partial["decision"]["code"] == "BRANCH_RETIREMENT_PARTIAL"
        assert partial["decision"]["completed_operations"] == ["local"]

        record_branch_retirement(
            repository.repo,
            plan_id=plan["plan_id"],
            operation="remote",
            summary_path=summary("remote", "REMOTE_BRANCH_RETIRED_VERIFIED"),
        )
        complete = branch_retirement_status(repository.repo, plan_id=plan["plan_id"])
        assert complete["decision"]["code"] == "BRANCH_RETIREMENT_COMPLETE"
        assert complete["decision"]["completed_operations"] == ["local", "remote"]


def test_protected_and_historical_refs_are_report_only() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        protected = plan_branch_retirement(
            repository.repo,
            branch="main",
            remote="origin",
            integrated_into="main",
        )["decision"]
        assert protected["code"] == "BRANCH_RETIREMENT_REPORT_ONLY"
        assert protected["persisted"] is False
        assert "PROTECTED_BRANCH" in protected["plan"]["blockers"]
        assert "HISTORICAL_UNREGISTERED" in protected["plan"]["blockers"]

        for branch in ("rescue", "archive/kept", "recovery"):
            special = plan_branch_retirement(
                repository.repo,
                branch=branch,
                remote="origin",
                integrated_into="main",
            )["decision"]
            assert special["code"] == "BRANCH_RETIREMENT_REPORT_ONLY"
            assert special["persisted"] is False
            assert "SPECIAL_LIFECYCLE_REF" in special["plan"]["blockers"]


def _commit_file(repository: GitRepository, name: str, content: str) -> str:
    path = repository.repo / name
    path.write_text(content, encoding="utf-8")
    git(repository.repo, "add", name)
    git(repository.repo, "commit", "-m", f"write {name} {content}")
    return git(repository.repo, "rev-parse", "HEAD")


def _commit_tree(repo: Path, tree: str, parents: tuple[str, ...], message: str) -> str:
    command = ["git", "-C", str(repo), "commit-tree", tree]
    for parent in parents:
        command.extend(("-p", parent))
    result = subprocess.run(
        command,
        input=f"{message}\n",
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_non_ancestry_classifications_are_distinct() -> None:
    with GitRepository(linked=0, policy=False) as repository:
        base = git(repository.repo, "rev-parse", "HEAD")

        git(repository.repo, "checkout", "-b", "equivalent")
        equivalent = _commit_file(repository, "equivalent.txt", "same\n")
        git(repository.repo, "checkout", "main")
        _commit_file(repository, "main-marker.txt", "main\n")
        git(repository.repo, "cherry-pick", equivalent)
        integrated_equivalent = git(repository.repo, "rev-parse", "HEAD")
        assert (
            _classify_integration(repository.repo, equivalent, integrated_equivalent)
            == "CHERRY_EQUIVALENT"
        )

        tree = git(repository.repo, "rev-parse", f"{integrated_equivalent}^{{tree}}")
        merge_only = _commit_tree(
            repository.repo,
            tree,
            (integrated_equivalent, base),
            "synthetic merge-only",
        )
        assert (
            _classify_integration(repository.repo, merge_only, integrated_equivalent)
            == "MERGE_ONLY"
        )

        git(repository.repo, "checkout", "-b", "mixed", integrated_equivalent)
        first = _commit_file(repository, "first.txt", "first\n")
        mixed = _commit_file(repository, "second.txt", "second\n")
        git(repository.repo, "checkout", "main")
        _commit_file(repository, "mixed-marker.txt", "main\n")
        git(repository.repo, "cherry-pick", first)
        integrated_mixed = git(repository.repo, "rev-parse", "HEAD")
        assert _classify_integration(repository.repo, mixed, integrated_mixed) == "MIXED"

        git(repository.repo, "checkout", "-b", "unique", integrated_mixed)
        unique = _commit_file(repository, "unique.txt", "unique\n")
        assert _classify_integration(repository.repo, unique, integrated_mixed) == "UNIQUE"
