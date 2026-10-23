from __future__ import annotations

import json
from dataclasses import replace

import pytest

import worktree_controller.lifecycle as lifecycle_module
from tests.test_controlled import _configure_remote, adopt_canonical
from tests.test_integration_pipeline import (
    POLICY_THREE_CANDIDATES,
    _claim,
    _enqueue,
    _publish,
    _source,
    _validate,
)
from tests.test_shadow import GitRepository, git
from worktree_controller.controller import doctor, guard_write, status
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import (
    abort_publication_lease,
    acknowledge_lifecycle_event,
    acquire_publication_lease,
    integration_recovery_matrix,
    lifecycle_events,
    reconcile_integration_intents,
    refresh_integration,
    reopen_integration,
)
from worktree_controller.models import ControllerError
from worktree_controller.policy import load_policy
from worktree_controller.semantics import intent_for_allocation
from worktree_controller.store import MetadataStore

POLICY_FOUR_SOURCES = (
    POLICY_THREE_CANDIDATES.replace("normal_limit = 7", "normal_limit = 8")
    .replace("hard_limit = 9", "hard_limit = 10")
    .replace("feature_or_audit = 3", "feature_or_audit = 4")
)


def test_policy_v4_separates_prepare_publish_and_reserves() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        policy = load_policy(repository.repo)

        assert policy.policy_schema_version == 4
        assert policy.integration_prepare_limit == 3
        assert policy.integration_candidate_limit == 3
        assert policy.main_publish_limit == 1
        assert policy.recovery_reserve == 1
        assert policy.automatic_feature_audit_release is True
        assert policy.automatic_integration_continuation is True
        assert policy.preserved_auto_release is False
        matrix = integration_recovery_matrix(repository.repo)["decision"]["phases"]
        assert set(matrix) == {
            "ATTENTION_REQUIRED",
            "CLAIMED",
            "PREPARED",
            "VENV_REMOVED",
            "WORKTREE_REMOVED",
            "PUSHED",
            "REMOTE_VERIFIED",
            "RELEASED",
        }


def test_v2_intent_prepared_receipts_reopen_and_durable_outbox() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        _source_path, allocation, _handoff = _source(
            repository,
            task="outbox-source",
            relative_path="outbox.txt",
            content="outbox\n",
        )
        queued = lifecycle_module.enqueue_integration(
            repository.repo,
            task_key="outbox-candidate",
            authority_key="outbox-candidate",
            module="integration",
            owner="integration-owner",
            source_allocations=(allocation,),
            source_snapshot_ids=("snapshot:source-outbox",),
        )
        intent_id = str(queued["decision"]["intent_id"])
        store = MetadataStore(inventory(repository.repo).common_dir)
        intent = store.load().integration_intents[intent_id]
        assert intent.contract_version == 2
        assert intent.source_snapshot_ids == ("snapshot:source-outbox",)
        assert intent.source_branches == ("feature/outbox-source",)

        first_event = lifecycle_events(repository.repo, consumer="agent-workspace")["decision"][
            "events"
        ][0]
        first_ack = acknowledge_lifecycle_event(
            repository.repo,
            consumer="agent-workspace",
            event_id=str(first_event["event_id"]),
        )
        repeated_ack = acknowledge_lifecycle_event(
            repository.repo,
            consumer="agent-workspace",
            event_id=str(first_event["event_id"]),
        )
        assert first_ack["decision"]["created"] is True
        assert repeated_ack["decision"]["created"] is False
        assert (
            MetadataStore(inventory(repository.repo).common_dir).list_lifecycle_events(
                "agent-workspace"
            )
            == ()
        )

        candidate, _claimed = _claim(
            repository,
            task="outbox-candidate",
            intent_id=intent_id,
        )
        with pytest.raises(ControllerError) as missing_evidence:
            lifecycle_module.validate_integration(
                repository.repo,
                target=candidate,
                expected_record_version=1,
                owner="integration-owner",
                validation_evidence_id="validation:outbox",
            )
        assert missing_evidence.value.code == "MISSING_PREPARATION_EVIDENCE"

        prepared = _validate(repository, candidate, "validation:outbox")
        first_receipt_id = str(prepared["decision"]["prepared_receipt_id"])
        assert prepared["decision"]["state"] == "READY_TO_PUBLISH"
        assert first_receipt_id in store.load().prepared_candidate_receipts
        assert (
            guard_write(
                repository.repo,
                target=candidate,
                task_key="outbox-candidate",
                authority_key="outbox-candidate",
                owner="integration-owner",
                operation="integration-write",
            )["decision"]["code"]
            == "BLOCK_VALIDATED_CANDIDATE_FROZEN"
        )

        reopened = reopen_integration(
            repository.repo,
            target=candidate,
            expected_record_version=2,
            owner="integration-owner",
        )
        assert reopened["decision"]["state"] == "PREPARING_INTEGRATION"
        assert reopened["decision"]["prepared_receipt_id"] is None
        assert reopened["decision"]["attempt"] == 2
        assert (
            guard_write(
                repository.repo,
                target=candidate,
                task_key="outbox-candidate",
                authority_key="outbox-candidate",
                owner="integration-owner",
                operation="integration-write",
            )["decision"]["code"]
            == "WRITE_ALLOWED"
        )
        prepared_again = _validate(
            repository,
            candidate,
            "validation:outbox-reopened",
            expected_record_version=3,
        )
        assert prepared_again["decision"]["prepared_receipt_id"] != first_receipt_id
        assert first_receipt_id in store.load().prepared_candidate_receipts

        pending = lifecycle_events(repository.repo, consumer="agent-workspace")["decision"][
            "events"
        ]
        assert [event["event_type"] for event in pending] == [
            "PREPARE_SLOT_GRANTED",
            "PREPARED",
            "PREPARE_SLOT_GRANTED",
            "PREPARED",
        ]
        all_events = lifecycle_events(repository.repo, consumer="fresh-consumer")["decision"][
            "events"
        ]
        assert [event["event_type"] for event in all_events] == [
            "INTENT_QUEUED",
            "PREPARE_SLOT_GRANTED",
            "PREPARED",
            "PREPARE_SLOT_GRANTED",
            "PREPARED",
        ]


def test_publication_base_cas_requires_reprepare_and_new_receipt() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        _source_path, allocation, _handoff = _source(
            repository,
            task="cas-source",
            relative_path="candidate.txt",
            content="candidate\n",
        )
        intent = _enqueue(repository, task="cas-candidate", source_allocation=allocation)
        candidate, _claimed = _claim(
            repository,
            task="cas-candidate",
            intent_id=str(intent["decision"]["intent_id"]),
        )
        first = _validate(repository, candidate, "validation:cas-first")
        first_receipt_id = str(first["decision"]["prepared_receipt_id"])

        upstream = repository.repo / "upstream.txt"
        upstream.write_text("new main\n", encoding="utf-8")
        git(repository.repo, "add", "upstream.txt")
        git(repository.repo, "commit", "-m", "advance upstream main")
        git(repository.repo, "push", "origin", "main")
        upstream_oid = git(repository.repo, "rev-parse", "HEAD")

        stale = acquire_publication_lease(
            repository.repo,
            target=candidate,
            expected_record_version=2,
            owner="integration-owner",
            run_id="cas-stale",
        )
        assert stale["decision"]["code"] == "NEEDS_REPREPARE"
        assert stale["decision"]["prepared_receipt_id"] is None
        assert status(repository.repo)["repository"]["publication_lease"] is None
        assert (
            guard_write(
                repository.repo,
                target=candidate,
                task_key="cas-candidate",
                authority_key="cas-candidate",
                owner="integration-owner",
                operation="integration-write",
            )["decision"]["code"]
            == "BLOCK_REPREPARE_REQUIRED"
        )

        refreshed = refresh_integration(
            repository.repo,
            target=candidate,
            expected_record_version=3,
            owner="integration-owner",
        )
        assert refreshed["decision"]["code"] == "INTEGRATION_REFRESHED"
        assert refreshed["decision"]["base_main_oid"] == upstream_oid
        second = _validate(
            repository,
            candidate,
            "validation:cas-second",
            expected_record_version=4,
        )
        assert second["decision"]["prepared_receipt_id"] != first_receipt_id
        leased = acquire_publication_lease(
            repository.repo,
            target=candidate,
            expected_record_version=5,
            owner="integration-owner",
            run_id="cas-current",
        )
        assert leased["decision"]["code"] == "PUBLICATION_LEASE_ACQUIRED"
        assert leased["decision"]["expected_main_oid"] == upstream_oid
        aborted = abort_publication_lease(
            repository.repo,
            target=candidate,
            expected_record_version=6,
            lease_id=str(leased["decision"]["lease_id"]),
            owner="integration-owner",
            run_id="cas-current",
        )
        assert aborted["decision"]["code"] == "PUBLICATION_LEASE_ABORTED"


def test_prepared_receipt_drift_is_doctor_blocking_and_cannot_publish() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        _source_path, allocation, _handoff = _source(
            repository,
            task="receipt-source",
            relative_path="receipt.txt",
            content="receipt\n",
        )
        queued = _enqueue(repository, task="receipt-candidate", source_allocation=allocation)
        candidate, _claimed = _claim(
            repository,
            task="receipt-candidate",
            intent_id=str(queued["decision"]["intent_id"]),
        )
        prepared = _validate(repository, candidate, "validation:receipt")
        store = MetadataStore(inventory(repository.repo).common_dir)
        receipt_id = str(prepared["decision"]["prepared_receipt_id"])
        receipt_path = store.prepared_candidate_receipts_dir / f"{receipt_id}.json"
        raw = json.loads(receipt_path.read_text(encoding="utf-8"))
        raw["tests_receipt_id"] = "tests:tampered"
        receipt_path.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")

        diagnosis = doctor(repository.repo)
        assert diagnosis["decision"]["code"] == "FAIL"
        assert "PREPARED_RECEIPT_DRIFT" in {warning["code"] for warning in diagnosis["warnings"]}
        with pytest.raises(ControllerError) as blocked:
            acquire_publication_lease(
                repository.repo,
                target=candidate,
                expected_record_version=2,
                owner="integration-owner",
                run_id="tampered-receipt",
            )
        assert blocked.value.code == "PREPARED_RECEIPT_DRIFT"


def test_capacity_pressure_dispatches_only_the_existing_queued_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with GitRepository(linked=0, policy_text=POLICY_FOUR_SOURCES) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        allocations = [
            _source(
                repository,
                task=f"pressure-source-{index}",
                relative_path=f"pressure-{index}.txt",
                content=f"pressure {index}\n",
            )[1]
            for index in range(1, 5)
        ]
        intents = [
            _enqueue(
                repository,
                task=f"pressure-candidate-{index}",
                source_allocation=allocation,
            )["decision"]
            for index, allocation in enumerate(allocations, start=1)
        ]
        candidates = [
            _claim(
                repository,
                task=f"pressure-candidate-{index}",
                intent_id=str(intents[index - 1]["intent_id"]),
            )[0]
            for index in range(1, 4)
        ]
        fourth_path, waiting = _claim(
            repository,
            task="pressure-candidate-4",
            intent_id=str(intents[3]["intent_id"]),
        )
        assert waiting["decision"]["code"] == "INTEGRATION_QUEUED"
        assert not fourth_path.exists()

        _validate(repository, candidates[0], "validation:pressure-first")
        _publish(repository, candidates[0])
        dispatch_root = repository.root / "dispatched"
        monkeypatch.setattr(
            lifecycle_module,
            "_default_target",
            lambda _repository, task_key: dispatch_root / task_key,
        )
        reconciled = reconcile_integration_intents(
            repository.repo,
            actor="capacity-pressure",
            intent_ids=(str(intents[3]["intent_id"]),),
            dispatch=True,
        )
        assert [item["code"] for item in reconciled["decision"]["dispatched"]] == [
            "INTEGRATION_CANDIDATE_CLAIMED"
        ]
        snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
        assert len(snapshot.integration_intents) == 4
        assert snapshot.integration_intents[str(intents[3]["intent_id"])].state == (
            "PREPARING_INTEGRATION"
        )
        assert (dispatch_root / "pressure-candidate-4").is_dir()


def test_historical_released_intent_reconciliation_is_immutable_and_idempotent() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        _source_path, allocation, _handoff = _source(
            repository,
            task="history-source",
            relative_path="history.txt",
            content="history\n",
        )
        queued = _enqueue(repository, task="history-candidate", source_allocation=allocation)
        intent_id = str(queued["decision"]["intent_id"])
        candidate, _claimed = _claim(
            repository,
            task="history-candidate",
            intent_id=intent_id,
        )
        _validate(repository, candidate, "validation:history")
        _publish(repository, candidate)
        store = MetadataStore(inventory(repository.repo).common_dir)
        already_terminal = reconcile_integration_intents(
            repository.repo,
            actor="history-reconciler",
            intent_ids=(intent_id,),
            dry_run=True,
        )
        assert already_terminal["decision"]["outcomes"][0]["terminal_reconciliation_safe"] is False
        for receipt_path in store.publication_receipts_dir.glob("*.json"):
            receipt_path.unlink()
        intent_path = store.integration_intents_dir / f"{intent_id}.json"
        raw = json.loads(intent_path.read_text(encoding="utf-8"))
        for field in {
            "contract_version",
            "source_branches",
            "source_finalizer_record_ids",
            "source_snapshot_ids",
            "scope_digest",
            "queue_sequence",
            "attempt",
            "prepare_identity",
            "publication_identity",
            "recovery_identity",
            "prepared_receipt_id",
            "prepared_base_origin_main_oid",
            "prepared_commit_oid",
            "prepared_tree_oid",
            "prepared_scope_digest",
            "tests_receipt_id",
            "snapshot_receipt_id",
        }:
            raw.pop(field)
        raw["schema_version"] = 2
        raw["state"] = "CLAIMED"
        intent_path.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
        historical_bytes = intent_path.read_bytes()
        before = doctor(repository.repo)
        assert before["decision"]["code"] == "FAIL"
        assert "INTEGRATION_INTENT_DRIFT" in {warning["code"] for warning in before["warnings"]}

        planned = reconcile_integration_intents(
            repository.repo,
            actor="history-reconciler",
            intent_ids=(intent_id,),
            dry_run=True,
        )
        assert planned["decision"]["outcomes"][0]["terminal_reconciliation_safe"] is True
        first = reconcile_integration_intents(
            repository.repo,
            actor="history-reconciler",
            intent_ids=(intent_id,),
        )
        second = reconcile_integration_intents(
            repository.repo,
            actor="history-reconciler",
            intent_ids=(intent_id,),
        )
        assert first["decision"]["written_receipt_ids"]
        assert second["decision"]["written_receipt_ids"] == []
        assert second["decision"]["outcomes"][0]["effective_state"] == "RELEASED"
        assert intent_path.read_bytes() == historical_bytes
        assert len(store.load().integration_reconciliation_receipts) == 1
        after = doctor(repository.repo)
        assert after["decision"]["code"] == "PASS"
        reconciled_warning = next(
            warning
            for warning in after["warnings"]
            if warning["code"] == "INTEGRATION_INTENT_RECONCILED"
        )
        assert reconciled_warning["severity"] == "informational"
        assert "INTEGRATION_INTENT_DRIFT" not in {warning["code"] for warning in after["warnings"]}


def test_allocation_lookup_refuses_ambiguous_integration_identity() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        _source_path, source, _handoff = _source(
            repository, task="lookup-source", relative_path="source.txt", content="source\n"
        )
        queued = _enqueue(repository, task="lookup-candidate", source_allocation=source)
        intent_id = queued["decision"]["intent_id"]
        _candidate, claimed = _claim(repository, task="lookup-candidate", intent_id=intent_id)
        allocation = claimed["decision"]["allocation_id"]
        snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
        intent = snapshot.integration_intents[intent_id]

        assert intent_for_allocation(snapshot, allocation) is intent
        assert intent_for_allocation(snapshot, "missing-allocation") is None
        duplicate = replace(intent, intent_id="duplicate-intent")
        snapshot.integration_intents[duplicate.intent_id] = duplicate
        with pytest.raises(ControllerError, match="multiple intents bind one allocation") as caught:
            intent_for_allocation(snapshot, allocation)
        assert caught.value.code == "INVALID_METADATA"
