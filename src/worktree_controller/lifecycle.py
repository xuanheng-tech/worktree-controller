"""Explicit controlled-write lifecycle operations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from .controller import _warning, envelope, plan, status, status_observation
from .disposable import CONTRACT as TYPED_RESOURCE_CONTRACT
from .disposable import classify_resources, delete_typed_resource, resource_evidence
from .environment import (
    RUNTIME_AUTHORITY_CONTRACT,
    discover_python_project,
    editable_references,
    reproducible_venv_evidence,
    runtime_authority_evidence,
    runtime_references,
    unsafe_ignored_paths,
)
from .git_backend import (
    branch_exists,
    build_integration_candidate,
    changed_paths,
    checkout_branch,
    checkout_detached,
    commit_tree_oid,
    fetch_remote_branch,
    git_write,
    ignored_paths,
    inventory,
    is_ancestor,
    path_is_ignored,
    public_refs,
    resolve_commit,
    resolve_repository,
    tracked_paths,
    update_branch_ref,
    validate_branch_name,
    worktree_content_fingerprint,
)
from .keyed_document_scope import independent_progress_scopes
from .lifecycle_history import lifecycle_history
from .models import (
    ACTIVE_AUTHORITY_STATES,
    FEATURE_AUTHORITY_ROLES,
    METADATA_SCHEMA_VERSION,
    ORDINARY_ROLES,
    RECOVERY_ROLES,
    SCHEMA_VERSION,
    TERMINAL_INTEGRATION_STATES,
    ControllerError,
    IntegrationIntent,
    IntegrationReconciliationReceipt,
    LifecycleEvent,
    MetadataRecord,
    MetadataSnapshot,
    PreparedCandidateReceipt,
    PublicationLease,
    PublicationReceipt,
    ReconciliationReceipt,
    ReleaseIntent,
    ReleasePlan,
    ReleaseReceipt,
    RepositoryInventory,
    WorktreeGitState,
)
from .policy import load_policy
from .preserved_cleanup import CONTRACT as PRESERVED_CLEANUP_CONTRACT
from .preserved_cleanup import coverage, delete_preserved_state
from .record_updates import updated_intent as _updated_intent
from .record_updates import updated_record as _updated_record
from .release_intents import RENEWAL_BACKOFF_CAP_SECONDS as RENEWAL_BACKOFF_CAP_SECONDS
from .release_intents import release_intent_renewal_due as _renewal_due
from .release_intents import release_intents_for_allocation as _release_intents_for_allocation
from .semantics import assert_record_version as _cas
from .semantics import (
    attribution_base,
    integration_candidate_issues,
    integration_intent_abandoned,
    integration_prerequisite,
    integration_queue_order,
    integration_source_evidence,
    normalize_scope_paths,
    path_in_scopes,
    path_scope_issues,
    publication_ref,
)
from .semantics import effective_intent_state as _effective_intent_state
from .semantics import integration_reconciliation as _integration_reconciliation
from .semantics import intent_for_allocation as _intent_for_allocation
from .semantics import managed_record as _managed_record
from .semantics import publication_receipt_lease_released as _publication_receipt_lease_released
from .semantics import record_for as _record_for
from .semantics import scope_digest as _scope_digest
from .semantics import scopes_overlap as _scopes_overlap
from .store import (
    INTEGRATION_INTENT_CONTRACT_VERSION,
    INTEGRATION_INTENT_SCHEMA_VERSION,
    INTEGRATION_RECONCILIATION_RECEIPT_SCHEMA_VERSION,
    LIFECYCLE_EVENT_SCHEMA_VERSION,
    PREPARED_CANDIDATE_RECEIPT_SCHEMA_VERSION,
    PUBLICATION_LEASE_SCHEMA_VERSION,
    PUBLICATION_RECEIPT_SCHEMA_VERSION,
    RECONCILIATION_RECEIPT_SCHEMA_VERSION,
    RELEASE_INTENT_SCHEMA_VERSION,
    RELEASE_PLAN_SCHEMA_VERSION,
    RELEASE_RECEIPT_SCHEMA_VERSION,
    MetadataStore,
    integration_intent_from_dict,
    lifecycle_event_from_dict,
    new_record,
    now_iso,
    record_to_dict,
    release_intent_from_dict,
    release_intent_to_dict,
)

TASK_KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
RELEASABLE_STATES = frozenset(
    {
        "HANDOFF_READY",
        "PRESERVED",
        "PUBLISHED",
        "RECOVERY_REQUIRED",
        "NO_CHANGE_COMPLETED",
        "INTEGRATION_ABORTED",
        "RELEASE_READY",
        "ARCHIVED",
    }
)
EVIDENCE_EXEMPT_RELEASE_STATES = frozenset({"NO_CHANGE_COMPLETED", "INTEGRATION_ABORTED", "RELEASE_READY"})
DEFAULT_PUBLICATION_LEASE_SECONDS = 900
MAX_PUBLICATION_LEASE_SECONDS = 3600
TEARDOWN_TRANSACTION_RESULTS = {
    "TEARDOWN_PREPARED": "prepared",
    "VENV_REMOVAL_STARTED": "removal_started",
    "VENV_REMOVED": "removed",
    "PREPARED": "removed",
}
POST_TEARDOWN_RECOVERY_PHASES = frozenset({"VENV_REMOVED", "PREPARED"})


def _crash_point(name: str) -> None:
    if os.environ.get("WORKTREE_CONTROLLER_TEST_CRASH_AT") == name:
        os._exit(97)


def _intent_order(intent: IntegrationIntent) -> tuple[int, int | str, str]:
    if intent.queue_sequence > 0:
        return (1, intent.queue_sequence, intent.intent_id)
    return (0, intent.enqueued_at, intent.intent_id)


def _released_intent_evidence(
    snapshot: MetadataSnapshot,
    intent: IntegrationIntent,
    record: MetadataRecord,
) -> bool:
    if intent.candidate_oid is None:
        return False
    publication_evidence = any(
        receipt.repository_id == snapshot.repository_id
        and receipt.allocation_id == record.allocation_id
        and receipt.candidate_oid == intent.candidate_oid
        and receipt.status in {"REMOTE_VERIFIED", "RELEASED"}
        for receipt in snapshot.publication_receipts.values()
    )
    release_evidence = any(
        receipt.repository_id == snapshot.repository_id
        and receipt.allocation_id == record.allocation_id
        and receipt.released_head == intent.candidate_oid
        and receipt.released_record_version == record.record_version
        and receipt.result == "RELEASED"
        and (plan := snapshot.release_plans.get(receipt.plan_id)) is not None
        and plan.repository_id == receipt.repository_id
        and plan.allocation_id == receipt.allocation_id
        and plan.expected_head == receipt.released_head
        and plan.expected_record_version == receipt.expected_record_version
        and plan.branch == receipt.branch
        and plan.target_path == receipt.target_path
        for receipt in snapshot.release_receipts.values()
    )
    return publication_evidence or release_evidence


def _preferred_handoff_matches(snapshot: MetadataSnapshot, matches: list[str]) -> list[str]:
    """Prefer a live allocation when a released one shares the same handoff commit.

    A source may legitimately be released before it is integrated, so a released
    allocation still resolves by handoff commit. When a live allocation carries the
    same commit it is the unambiguous source, and the released one is history.
    """

    if len(matches) <= 1:
        return matches
    live = [
        allocation_id
        for allocation_id in matches
        if (record := snapshot.records.get(allocation_id)) is not None
        and record.lifecycle != "RELEASED"
    ]
    return live if len(live) == 1 else matches


def _blocking_scope_intent(
    snapshot: MetadataSnapshot, candidate: IntegrationIntent, *, repository_path: Path | None = None
) -> IntegrationIntent | None:
    prerequisite_id = integration_prerequisite(candidate)
    if prerequisite_id is not None:
        prerequisite = snapshot.integration_intents.get(prerequisite_id)
        if prerequisite is None:
            raise ControllerError("INVALID_METADATA", "retry prerequisite intent is missing")
        if _effective_intent_state(snapshot, prerequisite) != "RELEASED":
            return prerequisite
    for other in sorted(
        snapshot.integration_intents.values(),
        key=lambda value: integration_queue_order(snapshot, value),
    ):
        if other.intent_id == candidate.intent_id:
            continue
        if integration_queue_order(snapshot, other) >= integration_queue_order(snapshot, candidate):
            continue
        if _effective_intent_state(snapshot, other) in TERMINAL_INTEGRATION_STATES:
            continue
        if other.allocation_id is None and other.blocked_reason:
            # a failed or abandoned preparation holds no checkout and prepares nothing
            continue
        if _scopes_overlap(candidate.scope_paths, other.scope_paths):
            if (
                repository_path is not None
                and other.state in {
                    "QUEUED", "WAITING_SCOPE", "PREPARING_INTEGRATION",
                    "READY_TO_PUBLISH", "NEEDS_REPREPARE",
                }
                and independent_progress_scopes(
                    repository_path,
                    candidate.scope_paths,
                    other.scope_paths,
                    candidate.source_handoffs,
                    other.source_handoffs,
                )
            ):
                continue
            return other
    return None


def _event_identity(intent: IntegrationIntent, event_type: str) -> str:
    payload = {
        "allocation_id": intent.allocation_id,
        "attempt": intent.attempt,
        "candidate_oid": intent.candidate_oid,
        "event_type": event_type,
        "intent_id": intent.intent_id,
        "history_length": len(intent.history),
        "prepared_receipt_id": intent.prepared_receipt_id,
        "publication_identity": intent.publication_identity,
        "recovery_identity": intent.recovery_identity,
        "revision": intent.revision,
        "state": intent.state,
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _emit_lifecycle_event(
    store: MetadataStore,
    repository_id: str,
    intent: IntegrationIntent,
    event_type: str,
) -> LifecycleEvent:
    identity = _event_identity(intent, event_type)
    event_id = str(
        uuid.uuid5(
            uuid.UUID(repository_id),
            f"integration-lifecycle-event-v1:{intent.intent_id}:{event_type}:{identity}",
        )
    )
    event_path = store.lifecycle_events_dir / f"{event_id}.json"
    if event_path.is_file():
        existing = lifecycle_event_from_dict(store._read_json(event_path))
        if (
            existing.repository_id != repository_id
            or existing.intent_id != intent.intent_id
            or existing.event_type != event_type
            or existing.identity != identity
        ):
            raise ControllerError("EVENT_MISMATCH", "lifecycle event identity differs")
        return existing
    existing_sequences = [
        lifecycle_event_from_dict(store._read_json(path)).sequence
        for path in store.lifecycle_events_dir.glob("*.json")
    ]
    event = LifecycleEvent(
        schema_version=LIFECYCLE_EVENT_SCHEMA_VERSION,
        event_id=event_id,
        repository_id=repository_id,
        intent_id=intent.intent_id,
        allocation_id=intent.allocation_id,
        event_type=event_type,
        state=intent.state,
        sequence=max(existing_sequences, default=0) + 1,
        identity=identity,
        created_at=intent.updated_at,
    )
    store.write_lifecycle_event(event)
    return event


def _updated_release_intent(intent: ReleaseIntent, event: str, **changes: Any) -> ReleaseIntent:
    payload = release_intent_to_dict(intent)
    timestamp = now_iso()
    payload.update(changes)
    payload["updated_at"] = timestamp
    history = list(intent.history)
    history.append(
        {
            "at": timestamp,
            "event": event,
            "from": intent.status,
            "to": payload.get("status", intent.status),
            "blocker": payload.get("blocker"),
        }
    )
    payload["history"] = history
    return release_intent_from_dict(payload)


def _cancel_release_intents(
    store: MetadataStore,
    snapshot: MetadataSnapshot,
    allocation_id: str,
    *,
    event: str,
    blocker: str,
) -> None:
    for intent in _release_intents_for_allocation(snapshot, allocation_id):
        if intent.status in {"PENDING", "BLOCKED_RETRYABLE"}:
            store.replace_release_intent(
                _updated_release_intent(
                    intent,
                    event,
                    status="CANCELLED",
                    blocker=blocker,
                )
            )


def _duration_ms(start: str, end: str) -> int:
    return max(
        0,
        round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000),
    )


def _publication_source_results(
    repository: RepositoryInventory,
    intent: IntegrationIntent | None,
    candidate_oid: str,
) -> tuple[dict[str, str], ...]:
    if intent is None:
        return ()
    replay_by_source = {
        replay["source_allocation_id"]: replay for replay in intent.equivalent_replays
    }
    results: list[dict[str, str]] = []
    for source_allocation_id, source_handoff_oid in zip(
        intent.source_allocations, intent.source_handoffs, strict=True
    ):
        if is_ancestor(repository.canonical_path, source_handoff_oid, candidate_oid):
            results.append(
                {
                    "source_allocation_id": source_allocation_id,
                    "source_handoff_oid": source_handoff_oid,
                    "mode": "ancestry",
                    "absorbed_oid": source_handoff_oid,
                    "evidence_id": "git-ancestry",
                }
            )
            continue
        replay = replay_by_source.get(source_allocation_id)
        if replay is None or not is_ancestor(
            repository.canonical_path, replay["replay_oid"], candidate_oid
        ):
            raise ControllerError(
                "SOURCE_NOT_ABSORBED",
                "published candidate lacks source ancestry or equivalent replay evidence",
            )
        results.append(
            {
                "source_allocation_id": source_allocation_id,
                "source_handoff_oid": source_handoff_oid,
                "mode": "equivalent-replay",
                "absorbed_oid": replay["replay_oid"],
                "evidence_id": replay["evidence_id"],
            }
        )
    return tuple(results)


def _transaction(
    *,
    transaction_id: str,
    repository_id: str,
    command: str,
    phase: str,
    target_path: Path,
    allocation_id: str,
    branch: str | None,
    expected_head: str | None,
    expected_record_version: int | None,
    created_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": METADATA_SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "repository_id": repository_id,
        "command": command,
        "phase": phase,
        "target_path": str(target_path),
        "allocation_id": allocation_id,
        "branch": branch,
        "expected_head": expected_head,
        "expected_record_version": expected_record_version,
        "created_at": created_at,
        "updated_at": now_iso(),
    }


def _assert_all_adopted(repository: RepositoryInventory, snapshot: MetadataSnapshot) -> None:
    unmanaged = [
        str(item.path) for item in repository.worktrees if _record_for(item, snapshot) is None
    ]
    if unmanaged:
        raise ControllerError(
            "NEEDS_ADOPTION", "all registered worktrees must be adopted before acquire"
        )


def _assert_allocation_allowed(
    repository: RepositoryInventory,
    snapshot: MetadataSnapshot,
    *,
    task_key: str,
    authority_key: str,
    role: str,
) -> None:
    policy = load_policy(repository.canonical_path)
    if not policy.present:
        raise ControllerError("BLOCK_DRIFT", "repository policy is missing")
    _assert_all_adopted(repository, snapshot)
    active = [
        (item, record)
        for item in repository.worktrees
        if (record := _record_for(item, snapshot)) is not None
        and record.lifecycle in ACTIVE_AUTHORITY_STATES
    ]
    if any(record.task_key == task_key for _, record in active):
        raise ControllerError("REUSE", "an active allocation already owns this task")
    if (
        policy.unique_active_feature_authority
        and role in FEATURE_AUTHORITY_ROLES
        and any(
            record.authority_key == authority_key and record.role in FEATURE_AUTHORITY_ROLES
            for _, record in active
        )
    ):
        raise ControllerError(
            "BLOCK_DUPLICATE_AUTHORITY",
            "an active feature allocation already owns this authority",
        )
    preparing = sum(
        record.role == "integration" and record.lifecycle not in {"PUBLISHED", "RELEASED"}
        for item in repository.worktrees
        if not item.canonical and (record := _record_for(item, snapshot)) is not None
    )
    if role == "integration" and preparing >= policy.integration_candidate_limit:
        raise ControllerError("BLOCK_INTEGRATION_SLOT", "integration preparation concurrency is occupied")
    # Resource reservation follows authority and lineage validation, immediately
    # before the PREPARED journal. Checkout and writer counts never grant or refuse.


def _default_target(repository: RepositoryInventory, task_key: str) -> Path:
    repo_name = repository.canonical_path.name
    return Path.home() / "worktrees" / repo_name / task_key


def _intent_payload(intent: IntegrationIntent) -> dict[str, Any]:
    return {
        "contract_version": intent.contract_version,
        "intent_id": intent.intent_id,
        "task_key": intent.task_key,
        "authority_key": intent.authority_key,
        "module": intent.module,
        "owner": intent.owner,
        "state": intent.state,
        "source_allocations": list(intent.source_allocations),
        "source_handoff_oids": list(intent.source_handoffs),
        "source_branches": list(intent.source_branches),
        "source_finalizer_record_ids": list(intent.source_finalizer_record_ids),
        "source_snapshot_ids": list(intent.source_snapshot_ids),
        "scope_paths": list(intent.scope_paths),
        "scope_digest": intent.scope_digest,
        "queue_sequence": intent.queue_sequence,
        "allocation_id": intent.allocation_id,
        "base_main_oid": intent.base_main_oid,
        "candidate_oid": intent.candidate_oid,
        "candidate_revision": intent.revision,
        "attempt": intent.attempt,
        "prepare_identity": intent.prepare_identity,
        "publication_identity": intent.publication_identity,
        "recovery_identity": intent.recovery_identity,
        "prepared_receipt_id": intent.prepared_receipt_id,
        "prepared_base_origin_main_oid": intent.prepared_base_origin_main_oid,
        "prepared_commit_oid": intent.prepared_commit_oid,
        "prepared_tree_oid": intent.prepared_tree_oid,
        "prepared_scope_digest": intent.prepared_scope_digest,
        "tests_receipt_id": intent.tests_receipt_id,
        "snapshot_receipt_id": intent.snapshot_receipt_id,
        "validation_evidence_id": intent.validation_evidence_id,
        "validated_oid": intent.validated_oid,
        "blocked_reason": intent.blocked_reason,
        "enqueued_at": intent.enqueued_at,
        "claimed_at": intent.claimed_at,
        "validated_at": intent.validated_at,
        "published_at": intent.published_at,
        "released_at": intent.released_at,
        "queue_wait_duration_ms": (
            _duration_ms(intent.enqueued_at, intent.claimed_at)
            if intent.claimed_at is not None
            else None
        ),
    }


def integration_queue(repository_path: Path) -> dict[str, Any]:
    payload, snapshot = status_observation(repository_path)
    intents = sorted(
        snapshot.integration_intents.values(),
        key=_intent_order,
    )
    payload["command"] = "integration-queue"
    payload["decision"] = {
        "code": "INTEGRATION_QUEUE_STATUS",
        "reason": "integration intents are ordered by immutable enqueue identity",
        "queued": sum(
            _effective_intent_state(snapshot, intent) in {"QUEUED", "WAITING_SCOPE"}
            for intent in intents
        ),
        "intents": [
            {
                **_intent_payload(intent),
                "effective_state": _effective_intent_state(snapshot, intent),
            }
            for intent in intents
        ],
    }
    return payload


def lifecycle_events(repository_path: Path, *, consumer: str) -> dict[str, Any]:
    _requested, common_dir = resolve_repository(repository_path)
    store = MetadataStore(common_dir)
    events = store.list_lifecycle_events(consumer)
    payload = status(repository_path)
    payload["command"] = "lifecycle-events"
    payload["decision"] = {
        "code": "LIFECYCLE_EVENTS",
        "reason": "unacknowledged durable lifecycle events returned in creation order",
        "consumer": consumer.strip(),
        "events": [asdict(event) for event in events],
    }
    return payload


def acknowledge_lifecycle_event(
    repository_path: Path, *, consumer: str, event_id: str
) -> dict[str, Any]:
    repository = inventory(repository_path)
    store = MetadataStore(repository.common_dir)
    with store.lock(exclusive=True):
        created = store.acknowledge_lifecycle_event(consumer, event_id)
    payload = status(repository_path)
    payload["command"] = "lifecycle-event-ack"
    payload["decision"] = {
        "code": "LIFECYCLE_EVENT_ACKNOWLEDGED",
        "reason": (
            "durable consumer acknowledgement recorded"
            if created
            else "durable consumer acknowledgement already existed"
        ),
        "consumer": consumer.strip(),
        "event_id": event_id,
        "created": created,
    }
    return payload


INTEGRATION_RECOVERY_MATRIX: dict[str, dict[str, str]] = {
    "ATTENTION_REQUIRED": {
        "evidence": "exact abort/release or proved pre-allocation conflict; frozen source/scope",
        "resume": "integration-retry dry-run and exact plan CAS; optional verified prerequisite",
        "repeat": "same plan replay is idempotent; explicit single-source conflict claim may precede resolve and fresh validation",
        "fail_closed": "published, abandoned, live candidate, missing proof or source/scope drift",
    },
    "CLAIMED": {
        "evidence": "intent + allocation + exact HEAD/base/scope",
        "resume": "continue preparation",
        "repeat": "return current preparation identity",
        "fail_closed": "allocation, HEAD, record, source, or scope drift",
    },
    "PREPARED": {
        "evidence": "immutable prepared receipt + tests/Snapshot + exact tree",
        "resume": "request the single publication lease",
        "repeat": "reuse the same prepared receipt",
        "fail_closed": "prepared receipt drift or main-base CAS failure",
    },
    "VENV_REMOVED": {
        "evidence": "0.5.1 release journal + teardown receipt + exact record CAS",
        "resume": "continue at checkout teardown",
        "repeat": "0.5.1 idempotent release resume",
        "fail_closed": "journal, HEAD, record, content, or reference drift",
    },
    "WORKTREE_REMOVED": {
        "evidence": "release journal + missing registered checkout + retained branch",
        "resume": "finalize record and release receipt",
        "repeat": "return existing immutable release receipt",
        "fail_closed": "binding, branch, journal, or receipt mismatch",
    },
    "PUSHED": {
        "evidence": "publication lease + remote candidate identity",
        "resume": "remote verify and persist publication receipt",
        "repeat": "recover from the same lease/receipt identity",
        "fail_closed": "remote differs from expected base and candidate",
    },
    "REMOTE_VERIFIED": {
        "evidence": "immutable publication receipt + exact candidate/Finalizer",
        "resume": "guarded candidate/source release",
        "repeat": "reconcile immutable receipts idempotently",
        "fail_closed": "receipt, allocation, or source absorption drift",
    },
    "RELEASED": {
        "evidence": "released record + release receipt + retained branch",
        "resume": "return terminal state",
        "repeat": "no-op terminal acknowledgement",
        "fail_closed": "terminal record or receipt identity drift",
    },
}


def integration_recovery_matrix(repository_path: Path) -> dict[str, Any]:
    payload = status(repository_path)
    payload["command"] = "integration-recovery-matrix"
    payload["decision"] = {
        "code": "INTEGRATION_RECOVERY_MATRIX",
        "reason": "supported transaction phases have explicit evidence and fail-closed rules",
        "phases": INTEGRATION_RECOVERY_MATRIX,
    }
    return payload


def _released_source_branch(
    repository: RepositoryInventory, snapshot: MetadataSnapshot, record: MetadataRecord
) -> str | None:
    """Branch of a source whose checkout was released before it was integrated.

    The release receipt names the branch the release retained. It only stands in for
    the checkout while that branch still points at exactly the frozen handoff commit,
    so the integration intent freezes the same identity it would have read from the
    checkout.
    """

    if record.lifecycle != "RELEASED" or not record.handoff_commit:
        return None
    receipts = [
        receipt
        for receipt in snapshot.release_receipts.values()
        if receipt.allocation_id == record.allocation_id and receipt.result == "RELEASED"
    ]
    if len(receipts) != 1 or receipts[0].released_head != record.handoff_commit:
        return None
    branch = receipts[0].branch
    try:
        retained = resolve_commit(repository.canonical_path, f"refs/heads/{branch}")
    except ControllerError:
        return None
    return branch if retained == record.handoff_commit else None


def _resolve_integration_sources(
    repository: RepositoryInventory,
    snapshot: MetadataSnapshot,
    source_allocations: tuple[str, ...],
    source_handoffs: tuple[str, ...],
    source_snapshot_ids: tuple[str, ...],
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    resolved = list(dict.fromkeys(source_allocations))
    if len(resolved) != len(source_allocations):
        raise ControllerError("INVALID_SOURCE", "source allocations must be unique")
    for source_handoff in source_handoffs:
        handoff_oid = resolve_commit(repository.canonical_path, source_handoff)
        matches = [
            record.allocation_id
            for record in snapshot.records.values()
            if record.handoff_commit == handoff_oid
            and any(event.get("event") == "HANDOFF_INTEGRATION_READY" for event in record.history)
        ]
        matches = _preferred_handoff_matches(snapshot, matches)
        if len(matches) != 1:
            raise ControllerError(
                "AMBIGUOUS_SOURCE_HANDOFF",
                "source handoff must match exactly one integration-ready allocation",
            )
        resolved.append(matches[0])
    normalized = tuple(dict.fromkeys(resolved))
    frozen_handoffs, scopes = integration_source_evidence(snapshot, normalized)
    if source_snapshot_ids and len(source_snapshot_ids) != len(normalized):
        raise ControllerError(
            "INVALID_SOURCE", "source snapshot identities must align with source allocations"
        )
    branches: list[str] = []
    finalizers: list[str] = []
    for allocation_id in normalized:
        record = snapshot.records[allocation_id]
        matching_keys = [
            key
            for key, bound_allocation in snapshot.bindings.items()
            if bound_allocation == allocation_id
        ]
        matching_items = [item for item in repository.worktrees if item.key in matching_keys]
        if len(matching_items) == 1 and matching_items[0].branch is not None:
            branch = matching_items[0].branch
        elif not matching_items and (
            released_branch := _released_source_branch(repository, snapshot, record)
        ):
            branch = released_branch
        else:
            raise ControllerError(
                "SOURCE_BRANCH_UNAVAILABLE",
                "new integration intent requires the source checkout branch identity",
            )
        if record.finalizer_record_id is None:
            raise ControllerError(
                "SOURCE_EVIDENCE_MISMATCH", "source Finalizer evidence is missing"
            )
        branches.append(branch)
        finalizers.append(record.finalizer_record_id)
    snapshots = source_snapshot_ids or tuple(finalizers)
    return (
        normalized,
        frozen_handoffs,
        scopes,
        tuple(branches),
        tuple(finalizers),
        tuple(snapshots),
    )


def enqueue_integration(
    repository_path: Path,
    *,
    task_key: str,
    authority_key: str,
    module: str,
    owner: str,
    source_allocations: tuple[str, ...] = (),
    source_handoffs: tuple[str, ...] = (),
    source_snapshot_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    if TASK_KEY_PATTERN.fullmatch(task_key) is None:
        raise ControllerError("INVALID_TASK_KEY", "task_key is not safe")
    repository_initial = inventory(repository_path)
    policy = load_policy(repository_initial.canonical_path)
    if not policy.present:
        raise ControllerError("INVALID_POLICY", "integration queue requires an explicit policy")
    store = MetadataStore(repository_initial.common_dir)
    with store.lock(exclusive=True, create=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        existing = [
            intent
            for intent in snapshot.integration_intents.values()
            if intent.task_key == task_key
            and _effective_intent_state(snapshot, intent) != "RELEASED"
        ]
        if existing:
            intent = existing[0]
            if (
                intent.authority_key != authority_key
                or intent.module != module
                or intent.owner != owner
            ):
                raise ControllerError(
                    "DUPLICATE_INTENT_TASK", "task already has a different integration intent"
                )
            created = False
        else:
            if any(
                record.task_key == task_key and record.lifecycle in ACTIVE_AUTHORITY_STATES
                for record in snapshot.records.values()
            ):
                raise ControllerError("REUSE", "an active allocation already owns this task")
            (
                sources,
                frozen_handoffs,
                scopes,
                source_branches,
                source_finalizers,
                source_snapshots,
            ) = _resolve_integration_sources(
                repository,
                snapshot,
                source_allocations,
                source_handoffs,
                source_snapshot_ids,
            )
            timestamp = now_iso()
            queue_sequence = (
                max(
                    (current.queue_sequence for current in snapshot.integration_intents.values()),
                    default=0,
                )
                + 1
            )
            provisional = IntegrationIntent(
                schema_version=INTEGRATION_INTENT_SCHEMA_VERSION,
                contract_version=INTEGRATION_INTENT_CONTRACT_VERSION,
                intent_id=str(uuid.uuid4()),
                task_key=task_key,
                authority_key=authority_key,
                module=module,
                owner=owner,
                source_allocations=sources,
                source_handoffs=frozen_handoffs,
                source_branches=source_branches,
                source_finalizer_record_ids=source_finalizers,
                source_snapshot_ids=source_snapshots,
                scope_paths=scopes,
                scope_digest=_scope_digest(scopes),
                queue_sequence=queue_sequence,
                state="QUEUED",
                allocation_id=None,
                base_main_oid=None,
                candidate_oid=None,
                revision=0,
                attempt=1,
                prepare_identity=None,
                publication_identity=None,
                recovery_identity=None,
                prepared_receipt_id=None,
                prepared_base_origin_main_oid=None,
                prepared_commit_oid=None,
                prepared_tree_oid=None,
                prepared_scope_digest=None,
                tests_receipt_id=None,
                snapshot_receipt_id=None,
                validation_evidence_id=None,
                validated_oid=None,
                blocked_reason=None,
                enqueued_at=timestamp,
                claimed_at=None,
                validated_at=None,
                published_at=None,
                released_at=None,
                updated_at=timestamp,
                equivalent_replays=(),
                history=(),
            )
            initial_state = (
                "WAITING_SCOPE"
                if _blocking_scope_intent(snapshot, provisional, repository_path=repository.canonical_path) is not None
                else "QUEUED"
            )
            intent = integration_intent_from_dict(
                {
                    "schema_version": INTEGRATION_INTENT_SCHEMA_VERSION,
                    "contract_version": INTEGRATION_INTENT_CONTRACT_VERSION,
                    "intent_id": provisional.intent_id,
                    "task_key": task_key,
                    "authority_key": authority_key,
                    "module": module,
                    "owner": owner,
                    "source_allocations": list(sources),
                    "source_handoffs": list(frozen_handoffs),
                    "source_branches": list(source_branches),
                    "source_finalizer_record_ids": list(source_finalizers),
                    "source_snapshot_ids": list(source_snapshots),
                    "scope_paths": list(scopes),
                    "scope_digest": _scope_digest(scopes),
                    "queue_sequence": queue_sequence,
                    "state": initial_state,
                    "allocation_id": None,
                    "base_main_oid": None,
                    "candidate_oid": None,
                    "revision": 0,
                    "attempt": 1,
                    "prepare_identity": None,
                    "publication_identity": None,
                    "recovery_identity": None,
                    "prepared_receipt_id": None,
                    "prepared_base_origin_main_oid": None,
                    "prepared_commit_oid": None,
                    "prepared_tree_oid": None,
                    "prepared_scope_digest": None,
                    "tests_receipt_id": None,
                    "snapshot_receipt_id": None,
                    "validation_evidence_id": None,
                    "validated_oid": None,
                    "blocked_reason": None,
                    "enqueued_at": timestamp,
                    "claimed_at": None,
                    "validated_at": None,
                    "published_at": None,
                    "released_at": None,
                    "updated_at": timestamp,
                    "equivalent_replays": [],
                    "history": [
                        {
                            "at": timestamp,
                            "event": "INTEGRATION_ENQUEUED",
                            "from": None,
                            "to": initial_state,
                            "candidate_oid": None,
                        }
                    ],
                }
            )
            repository_id = store.initialize_repository()
            store.write_integration_intent(intent)
            _emit_lifecycle_event(store, repository_id, intent, "INTENT_QUEUED")
            created = True
    payload = status(repository_path)
    payload["command"] = "integration-enqueue"
    payload["decision"] = {
        "code": "INTEGRATION_ENQUEUED" if created else "INTEGRATION_ALREADY_QUEUED",
        "reason": "frozen source handoff identity queued without occupying a checkout",
        **_intent_payload(intent),
    }
    return payload


def _queue_wait_decision(
    repository_path: Path, intent: IntegrationIntent, reason: str
) -> dict[str, Any]:
    payload = status(repository_path)
    payload["command"] = "integration-claim"
    payload["decision"] = {
        "code": (
            "INTEGRATION_WAITING_SCOPE" if intent.state == "WAITING_SCOPE" else "INTEGRATION_QUEUED"
        ),
        "reason": reason,
        **_intent_payload(intent),
    }
    return payload


def _reuse_integration_claim(
    repository_path: Path, *, intent_id: str, owner: str, target: Path | None, branch: str | None,
) -> dict[str, Any]:
    """Observe one unchanged preparing candidate without reallocating or granting a lease."""
    from .integration_retry import _sources
    from .transaction_scope import conflicts

    repository = inventory(repository_path)
    store = MetadataStore(repository.common_dir)
    with store.lock(exclusive=False):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        intent = snapshot.integration_intents.get(intent_id)
        if intent is None or intent.state != "PREPARING_INTEGRATION" or not intent.allocation_id:
            raise ControllerError("INTENT_NOT_QUEUED", "intent has no reusable preparing candidate")
        if intent.owner != owner:
            raise ControllerError("OWNER_MISMATCH", "integration intent owner does not match")
        items = [item for item in repository.worktrees
                 if snapshot.bindings.get(item.key) == intent.allocation_id]
        if len(items) != 1:
            raise ControllerError("INTEGRATION_INTENT_MISMATCH", "claimed candidate registration changed")
        item = items[0]
        _, record = _managed_record(repository, snapshot, item.path)
        if ((target is not None and target.expanduser().absolute() != item.path)
                or (branch is not None and branch != item.branch)
                or record.base_commit != intent.base_main_oid
                or (record.task_key, record.authority_key, record.module, record.owner, record.role, record.lifecycle)
                != (intent.task_key, intent.authority_key, intent.module, owner, "integration", "ACTIVE")):
            raise ControllerError("INTEGRATION_INTENT_MISMATCH", "claim differs from its bound candidate")
        if item.detached or not item.clean or item.head != intent.candidate_oid:
            raise ControllerError("CANDIDATE_IDENTITY_MISMATCH", "claimed candidate changed")
        issues = integration_candidate_issues(item, record, snapshot)
        if issues:
            raise ControllerError(issues[0]["code"], issues[0]["message"])
        if conflicts(store, allocation=record.allocation_id, target=item.path):
            raise ControllerError("INTERRUPTED_TRANSACTION", "candidate has unresolved lifecycle effects")
        _sources(repository, snapshot, store, intent, require_idle=False)
    payload = status(repository_path)
    payload["command"] = "integration-claim"
    payload["decision"] = {"code": "INTEGRATION_CANDIDATE_CLAIMED",
                           "reason": "existing unchanged preparing candidate retained", **_intent_payload(intent)}
    return payload


def claim_integration(
    repository_path: Path,
    *,
    owner: str,
    intent_id: str | None = None,
    target: Path | None = None,
    branch: str | None = None,
    resolve_conflicts: bool = False,
) -> dict[str, Any]:
    if resolve_conflicts and intent_id is None:
        raise ControllerError("MISSING_INTENT", "explicit conflict resolution needs an exact intent")
    repository = inventory(repository_path)
    store = MetadataStore(repository.common_dir)
    snapshot = store.load()
    if intent_id is not None:
        requested = snapshot.integration_intents.get(intent_id)
        if requested is not None and requested.state == "PREPARING_INTEGRATION":
            return _reuse_integration_claim(repository_path, intent_id=intent_id, owner=owner,
                                           target=target, branch=branch)
        if requested is not None and requested.state == "WAITING_SCOPE":
            blocker = _blocking_scope_intent(snapshot, requested, repository_path=repository.canonical_path)
            if blocker is not None:
                return _queue_wait_decision(
                    repository_path,
                    requested,
                    f"scope overlaps earlier intent {blocker.intent_id}",
                )
            with store.lock(exclusive=True):
                current_snapshot = store._load_unlocked()
                current = current_snapshot.integration_intents.get(intent_id)
                if current is None or current.state != "WAITING_SCOPE":
                    raise ControllerError("BLOCK_DRIFT", "waiting intent changed during claim")
                requested = _updated_intent(
                    current,
                    "SCOPE_WAIT_CLEARED",
                    state="QUEUED",
                    blocked_reason=None,
                )
                store.replace_integration_intent(requested)
                if current_snapshot.repository_id is None:
                    raise ControllerError("BLOCK_DRIFT", "repository metadata is missing")
                _emit_lifecycle_event(
                    store, current_snapshot.repository_id, requested, "INTENT_QUEUED"
                )
            snapshot = store.load()
    candidates = sorted(
        (
            intent
            for intent in snapshot.integration_intents.values()
            if intent.state == "QUEUED" and (intent_id is None or intent.intent_id == intent_id)
        ),
        key=lambda value: integration_queue_order(snapshot, value),
    )
    if not candidates:
        raise ControllerError("NO_QUEUED_INTENT", "no matching queued integration intent")
    intent = candidates[0]
    if intent.owner != owner:
        raise ControllerError("OWNER_MISMATCH", "integration intent owner does not match")
    blocker = _blocking_scope_intent(snapshot, intent, repository_path=repository.canonical_path)
    if blocker is not None:
        with store.lock(exclusive=True):
            current_snapshot = store._load_unlocked()
            current = current_snapshot.integration_intents.get(intent.intent_id)
            if current is None or current.state != "QUEUED":
                raise ControllerError(
                    "BLOCK_DRIFT", "integration intent changed during scope fence"
                )
            intent = _updated_intent(
                current,
                "SCOPE_OVERLAP_WAITING",
                state="WAITING_SCOPE",
                blocked_reason=f"scope overlaps earlier intent {blocker.intent_id}",
            )
            store.replace_integration_intent(intent)
        return _queue_wait_decision(repository_path, intent, str(intent.blocked_reason))
    try:
        _assert_allocation_allowed(
            repository,
            snapshot,
            task_key=intent.task_key,
            authority_key=intent.authority_key,
            role="integration",
        )
    except ControllerError as exc:
        if exc.code in {"BLOCK_INTEGRATION_SLOT", "BLOCK_BUDGET"}:
            return _queue_wait_decision(repository_path, intent, str(exc))
        raise
    policy = load_policy(repository.canonical_path)
    base_main_oid = fetch_remote_branch(
        repository.canonical_path, policy.publication_remote, policy.publication_branch
    )
    pending_merge = False
    try:
        candidate_oid = build_integration_candidate(
            repository.canonical_path,
            base_main_oid,
            intent.source_handoffs,
            task_key=intent.task_key,
        )
    except ControllerError as exc:
        if exc.code != "INTEGRATION_CONFLICT":
            raise
        if resolve_conflicts:
            if len(intent.source_handoffs) != 1:
                raise ControllerError("UNSUPPORTED_CONFLICT", "resolve one frozen source at a time") from exc
            pending_merge = True
            candidate_oid = base_main_oid
        else:
            return _record_claim_conflict(repository_path, intent, base_main_oid, exc)
    try:
        acquired = acquire(
            repository_path,
            task_key=intent.task_key,
            authority_key=intent.authority_key,
            module=intent.module,
            role="integration",
            owner=intent.owner,
            branch=branch or (
                f"integration/{intent.task_key}"
                + (f"-retry-{intent.attempt}" if intent.attempt > 1 else "")
            ),
            base_ref=candidate_oid,
            target=target,
            source_allocations=intent.source_allocations,
            integration_intent_id=intent.intent_id,
            lineage_base_commit=base_main_oid,
            pending_merge=pending_merge,
        )
    except ControllerError as exc:
        if exc.code == "INTENT_NOT_QUEUED":
            return _reuse_integration_claim(repository_path, intent_id=intent.intent_id, owner=owner,
                                           target=target, branch=branch)
        if exc.code in {"BLOCK_INTEGRATION_SLOT", "BLOCK_BUDGET"}:
            return _queue_wait_decision(repository_path, intent, str(exc))
        raise
    current = (
        MetadataStore(inventory(repository_path).common_dir)
        .load()
        .integration_intents[intent.intent_id]
    )
    acquired["command"] = "integration-claim"
    if acquired["decision"]["code"] == "RESOURCE_WAIT":
        acquired["decision"].update(_intent_payload(current))
        return acquired
    acquired["decision"] = {
        "code": "INTEGRATION_CONFLICT_CLAIMED" if pending_merge else "INTEGRATION_CANDIDATE_CLAIMED",
        "reason": "frozen intent acquired a physical candidate slot",
        "pending_merge_source": intent.source_handoffs[0] if pending_merge else None,
        **_intent_payload(current),
    }
    return acquired


def _record_claim_conflict(
    repository_path: Path, intent: IntegrationIntent, base_main_oid: str, exc: ControllerError,
) -> dict[str, Any]:
    store = MetadataStore(inventory(repository_path).common_dir)
    with store.lock(exclusive=True):
        snapshot = store._load_unlocked()
        current = snapshot.integration_intents.get(intent.intent_id)
        if current is None or current.state != "QUEUED" or current.updated_at != intent.updated_at:
            raise ControllerError("BLOCK_DRIFT", "integration intent changed during claim") from exc
        intent = _updated_intent(
            current, "INTEGRATION_CONFLICT_BLOCKED", state="ATTENTION_REQUIRED",
            base_main_oid=base_main_oid, blocked_reason=str(exc),
        )
        store.replace_integration_intent(intent)
        _emit_lifecycle_event(store, snapshot.repository_id, intent, "ATTENTION_REQUIRED")
    payload = status(repository_path)
    payload["command"] = "integration-claim"
    payload["decision"] = {
        "code": "INTEGRATION_CONFLICT_BLOCKED",
        "reason": str(exc),
        **_intent_payload(intent),
    }
    return payload


def reconcile_integration_intents(
    repository_path: Path,
    *,
    actor: str,
    intent_ids: tuple[str, ...] = (),
    dry_run: bool = False,
    dispatch: bool = False,
) -> dict[str, Any]:
    actor_value = actor.strip()
    if not actor_value:
        raise ControllerError("INVALID_ACTOR", "integration reconciliation actor is required")
    if dry_run and dispatch:
        raise ControllerError("INVALID_RECONCILIATION", "dry-run cannot dispatch preparation")
    repository = inventory(repository_path)
    store = MetadataStore(repository.common_dir)
    outcomes: list[dict[str, Any]] = []
    written_receipts: list[str] = []
    promoted: list[str] = []
    synthesized_events: list[str] = []
    with store.lock(exclusive=not dry_run):
        snapshot = store._load_unlocked()
        if snapshot.repository_id is None:
            raise ControllerError("BLOCK_DRIFT", "repository metadata is missing")
        selected = (
            [snapshot.integration_intents.get(intent_id) for intent_id in intent_ids]
            if intent_ids
            else list(snapshot.integration_intents.values())
        )
        if any(intent is None for intent in selected):
            raise ControllerError("MISSING_INTENT", "integration intent is missing")
        for intent in sorted(
            (value for value in selected if value is not None),
            key=_intent_order,
        ):
            existing = _integration_reconciliation(snapshot, intent.intent_id)
            record = (
                snapshot.records.get(intent.allocation_id)
                if intent.allocation_id is not None
                else None
            )
            terminal_safe = (
                existing is None
                and intent.state != "RELEASED"
                and record is not None
                and record.lifecycle == "RELEASED"
                and intent.candidate_oid is not None
                and record.task_commit == intent.candidate_oid
                and record.allocation_id not in snapshot.bindings.values()
                and _released_intent_evidence(snapshot, intent, record)
            )
            receipt_id: str | None = existing.receipt_id if existing is not None else None
            if terminal_safe and record is not None:
                receipt_id = str(
                    uuid.uuid5(
                        uuid.UUID(snapshot.repository_id),
                        ":".join(
                            (
                                "integration-intent-reconciliation-v1",
                                intent.intent_id,
                                str(intent.schema_version),
                                intent.state,
                                record.allocation_id,
                                str(record.record_version),
                            )
                        ),
                    )
                )
                receipt = IntegrationReconciliationReceipt(
                    schema_version=INTEGRATION_RECONCILIATION_RECEIPT_SCHEMA_VERSION,
                    receipt_id=receipt_id,
                    repository_id=snapshot.repository_id,
                    intent_id=intent.intent_id,
                    observed_intent_schema_version=intent.schema_version,
                    observed_intent_state=intent.state,
                    observed_allocation_id=intent.allocation_id,
                    observed_record_version=record.record_version,
                    observed_record_lifecycle=record.lifecycle,
                    effective_terminal_state="RELEASED",
                    result="STALE_INTENT_TERMINAL_ACKNOWLEDGED",
                    actor=actor_value,
                    reconciled_at=now_iso(),
                )
                if not dry_run:
                    store.write_integration_reconciliation_receipt(receipt)
                    written_receipts.append(receipt_id)
            outcomes.append(
                {
                    "intent_id": intent.intent_id,
                    "raw_schema_version": intent.schema_version,
                    "raw_state": intent.state,
                    "effective_state": (
                        "RELEASED"
                        if existing is not None or terminal_safe
                        else _effective_intent_state(snapshot, intent)
                    ),
                    "lazy_migration": (
                        "IntegrationIntentV2" if intent.schema_version < 3 else "current"
                    ),
                    "terminal_reconciliation_safe": terminal_safe,
                    "reconciliation_receipt_id": receipt_id,
                }
            )
        current_snapshot = store._load_unlocked() if not dry_run else snapshot
        for intent in sorted(
            current_snapshot.integration_intents.values(),
            key=_intent_order,
        ):
            if intent_ids and intent.intent_id not in intent_ids:
                continue
            current = intent
            effective_state = _effective_intent_state(current_snapshot, current)
            if effective_state in {"RELEASED", "ABANDONED"} and (current.state != effective_state):
                continue
            if (
                current.state == "WAITING_SCOPE"
                and _blocking_scope_intent(current_snapshot, current, repository_path=repository.canonical_path) is None
            ):
                promoted.append(current.intent_id)
                if not dry_run:
                    current = _updated_intent(
                        current,
                        "SCOPE_WAIT_CLEARED",
                        state="QUEUED",
                        blocked_reason=None,
                    )
                    store.replace_integration_intent(current)
                    _emit_lifecycle_event(
                        store, current_snapshot.repository_id or "", current, "INTENT_QUEUED"
                    )
            event_type = {
                "QUEUED": "INTENT_QUEUED",
                "WAITING_SCOPE": "INTENT_QUEUED",
                "PREPARING_INTEGRATION": "PREPARE_SLOT_GRANTED",
                "READY_TO_PUBLISH": "PREPARED",
                "NEEDS_REPREPARE": "NEEDS_REPREPARE",
                "PUBLISHING": "PUBLICATION_LEASE_GRANTED",
                "RECOVERY_REQUIRED": "RECOVERY_REQUIRED",
                "ATTENTION_REQUIRED": "ATTENTION_REQUIRED",
                "REMOTE_VERIFIED": "REMOTE_VERIFIED",
                "RELEASED": "RELEASED",
            }.get(current.state)
            if event_type is None:
                continue
            identity = _event_identity(current, event_type)
            event_id = str(
                uuid.uuid5(
                    uuid.UUID(snapshot.repository_id),
                    f"integration-lifecycle-event-v1:{current.intent_id}:{event_type}:{identity}",
                )
            )
            if not (store.lifecycle_events_dir / f"{event_id}.json").exists():
                synthesized_events.append(event_id)
                if not dry_run:
                    _emit_lifecycle_event(store, snapshot.repository_id, current, event_type)
    dispatched: list[dict[str, Any]] = []
    if dispatch:
        while True:
            current = MetadataStore(inventory(repository_path).common_dir).load()
            queued = sorted(
                (
                    intent
                    for intent in current.integration_intents.values()
                    if intent.state == "QUEUED"
                    and (not intent_ids or intent.intent_id in intent_ids)
                ),
                key=_intent_order,
            )
            if not queued:
                break
            selected_intent = queued[0]
            result = claim_integration(
                repository_path,
                owner=selected_intent.owner,
                intent_id=selected_intent.intent_id,
            )
            decision = dict(result["decision"])
            dispatched.append(decision)
            if decision.get("code") in {
                "INTEGRATION_QUEUED",
                "INTEGRATION_WAITING_SCOPE",
            }:
                break
    payload = status(repository_path)
    payload["command"] = "reconcile integration-intents"
    payload["decision"] = {
        "code": (
            "INTEGRATION_RECONCILIATION_PLAN" if dry_run else "INTEGRATION_INTENTS_RECONCILED"
        ),
        "reason": (
            "integration reconciliation actions planned without mutation"
            if dry_run
            else "legacy drift, scope waits, and durable events converged idempotently"
        ),
        "actor": actor_value,
        "outcomes": outcomes,
        "written_receipt_ids": written_receipts,
        "promoted_intent_ids": promoted,
        "synthesized_event_ids": synthesized_events,
        "dispatched": dispatched,
    }
    return payload


def acquire(
    repository_path: Path,
    *,
    task_key: str,
    authority_key: str,
    module: str,
    role: str,
    owner: str,
    branch: str,
    base_ref: str,
    target: Path | None = None,
    scope_paths: tuple[str, ...] = (),
    source_allocations: tuple[str, ...] = (),
    source_handoffs: tuple[str, ...] = (),
    external_bindings: tuple[str, ...] = (),
    integration_intent_id: str | None = None,
    lineage_base_commit: str | None = None,
    pending_merge: bool = False,
    observe: bool = True,
) -> dict[str, Any]:
    """Create one policy-approved checkout, reclaiming dead writer slots first.

    ``observe=False`` returns only the decision envelope instead of a repository
    status; the acquisition itself and every guard are unchanged.

    A writer slot still charged to a process that no longer exists is not real
    occupancy. When the writer pool is what blocked admission, one bounded
    reconciliation pass runs and admission is retried exactly once - never a loop,
    and never for any other blocker, so integration, emergency, recovery and
    physical limits keep refusing exactly as before.
    """

    arguments: dict[str, Any] = {
        "task_key": task_key,
        "authority_key": authority_key,
        "module": module,
        "role": role,
        "owner": owner,
        "branch": branch,
        "base_ref": base_ref,
        "target": target,
        "scope_paths": scope_paths,
        "source_allocations": source_allocations,
        "source_handoffs": source_handoffs,
        "external_bindings": external_bindings,
        "integration_intent_id": integration_intent_id,
        "lineage_base_commit": lineage_base_commit,
        **({"pending_merge": True} if pending_merge else {}),
        **({} if observe else {"observe": False}),
    }
    payload = _acquire_once(repository_path, **arguments)
    if payload["decision"]["code"] == "RESOURCE_WAIT":
        policy = load_policy(inventory(repository_path).canonical_path)
        if policy.automatic_feature_audit_release and _has_convergeable_release_intent(
            repository_path
        ):
            try:
                reconcile_release_intents(
                    repository_path,
                    actor=policy.reconciler_actor,
                    trigger="capacity-pressure",
                )
            except ControllerError as blocked:
                payload["decision"]["cleanup_blocker"] = blocked.code
            else:
                return _acquire_once(repository_path, **arguments)
    return payload


def _has_convergeable_release_intent(repository_path: Path) -> bool:
    """Metadata-only check for work that a release pass could actually finish.

    Reconciliation walks processes and environment trees under the repository lock,
    which is far too expensive to start when nothing is waiting. Reading the recorded
    intents first keeps the exclusive lock off that scan in the common case.
    """

    store = MetadataStore(inventory(repository_path).common_dir)
    snapshot = store.load()
    for intent in snapshot.release_intents.values():
        if intent.status not in {"PENDING", "BLOCKED_RETRYABLE"}:
            continue
        record = snapshot.records.get(intent.allocation_id)
        if (
            record is not None
            and record.role in {"feature", "audit"}
            and record.lifecycle == "HANDOFF_READY"
        ):
            return True
    return False


def _repository_identity(repository_path: Path) -> str:
    """Two spellings of one repository must order and de-duplicate as one."""

    return str(inventory(repository_path).common_dir)


def acquire_when_free(
    repository_path: Path,
    *,
    deadline_seconds: int,
    poll_seconds: float = 2.0,
    **arguments: Any,
) -> dict[str, Any]:
    """Wait locally for a durable request; expiry does not erase the queued intent."""

    if deadline_seconds < 0 or poll_seconds < 1.0:
        raise ControllerError(
            "INVALID_REQUEST",
            "a bounded wait needs a non-negative deadline and at least a one second poll",
        )
    deadline = time.monotonic() + deadline_seconds
    attempts = 0
    while True:
        attempts += 1
        payload = acquire(repository_path, **arguments)
        if payload["decision"]["code"] == "RESOURCE_WAIT":
            if time.monotonic() >= deadline:
                payload["command"] = "acquire-when-free"
                payload["decision"].update({
                    "wait_expired": True,
                    "reason": "local wait expired; resource request remains queued",
                    "attempts": attempts,
                    "waited_seconds": round(
                        deadline_seconds - max(deadline - time.monotonic(), 0.0), 3
                    ),
                })
                return payload
            time.sleep(max(min(poll_seconds, deadline - time.monotonic()), 0.0))
            continue
        payload["decision"]["attempts"] = attempts
        return payload


def acquire_across(
    requests: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Admit one task that needs several repositories, all of them or none.

    A task blocked on two repositories used to acquire the one that had room and then
    wait, holding a checkout it could not use while the other stayed refused. This
    asks every repository first, acquires in one deterministic order so two such tasks
    can never deadlock against each other, and gives back anything it created if a
    later repository refuses. Nothing here schedules or queues: each acquisition is
    the ordinary guarded one, and a refusal is still a refusal.
    """

    required = (
        "repository",
        "task_key",
        "authority_key",
        "module",
        "role",
        "owner",
        "branch",
        "base_ref",
    )
    for request in requests:
        missing = [field for field in required if not request.get(field)]
        if missing:
            raise ControllerError(
                "INVALID_REQUEST", f"cross-repository request is missing {missing[0]}"
            )
        if str(request["role"]) not in ORDINARY_ROLES:
            # Rollback can only give back an ordinary checkout, so only ordinary work
            # may be admitted this way. A reserve role is requested one repository at
            # a time, by the owner who can account for it.
            raise ControllerError(
                "INVALID_REQUEST",
                "cross-repository admission is limited to ordinary feature and audit work",
            )
    identities = [_repository_identity(Path(str(request["repository"]))) for request in requests]
    if len(set(identities)) != len(identities):
        raise ControllerError("INVALID_REQUEST", "each repository may be requested once")
    ordered = [
        request
        for _identity, request in sorted(
            zip(identities, requests, strict=True), key=lambda pair: pair[0]
        )
    ]

    preflight: list[dict[str, Any]] = []
    for request in ordered:
        decision = plan(
            Path(request["repository"]),
            task_key=str(request["task_key"]),
            authority_key=request.get("authority_key"),
            module=str(request["module"]),
            role=str(request["role"]),
        )["decision"]
        preflight.append({"repository": str(request["repository"]), "decision": decision})
        if decision["code"] not in {"CREATE_ALLOWED", "RESOURCE_WAIT"}:
            return {
                "command": "acquire-across",
                "schema_version": SCHEMA_VERSION,
                "decision": {
                    "code": "ACROSS_REFUSED",
                    "reason": "a required repository refused admission; nothing was acquired",
                    "refused_repository": str(request["repository"]),
                    "refused_code": decision["code"],
                    "preflight": preflight,
                    "acquired": [],
                    "rolled_back": [],
                },
                "errors": [],
            }

    from .resource_admission import AdmissionLedger, prepare_request

    dynamic = [load_policy(inventory(Path(request["repository"])).canonical_path).resource_admission for request in ordered]
    if any(dynamic):
        if not all(dynamic):
            raise ControllerError("RESOURCE_ADMISSION_UNSUPPORTED", "atomic host admission requires compatible policies in every repository")
        resource_requests = []
        for request in ordered:
            arguments = {key: request[key] for key in required if key != "repository"}
            arguments.update(target=request.get("target"), scope_paths=tuple(request.get("scope_paths", ())))
            resource_requests.append(prepare_request(Path(request["repository"]), arguments))
        reservation = AdmissionLedger().reserve(resource_requests)
        if reservation["blockers"]:
            return {"schema_version": SCHEMA_VERSION, "command": "acquire-across", "errors": [],
                    "decision": {**reservation, "queued": True, "acquired": [], "rolled_back": []}}
        ordered = [{"repository": original["repository"], **frozen["arguments"]}
                   for original, frozen in zip(ordered, resource_requests, strict=True)]

    acquired: list[dict[str, Any]] = []
    for request in ordered:
        try:
            decision = acquire(
                Path(request["repository"]),
                task_key=str(request["task_key"]),
                authority_key=str(request["authority_key"]),
                module=str(request["module"]),
                role=str(request["role"]),
                owner=str(request["owner"]),
                branch=str(request["branch"]),
                base_ref=str(request["base_ref"]),
                target=Path(request["target"]) if request.get("target") else None,
                scope_paths=tuple(request.get("scope_paths", ())),
            )["decision"]
            if decision["code"] not in {"ACQUIRED", "REUSE"}:
                raise ControllerError(decision["code"], decision.get("reason", "checkout admission refused"))
        except Exception as refused:
            rolled_back = _roll_back_across(acquired)
            code = refused.code if isinstance(refused, ControllerError) else "INVALID_REQUEST"
            kept = [entry for entry in rolled_back if entry["result"] != "RELEASED"]
            return {
                "command": "acquire-across",
                "schema_version": SCHEMA_VERSION,
                "decision": {
                    "code": "ACROSS_PARTIALLY_RETAINED" if kept else "ACROSS_REFUSED",
                    "reason": (
                        "a later repository refused and some checkouts could not be given"
                        " back; review them before retrying"
                        if kept
                        else "a later repository refused; this attempt kept nothing, and the"
                        " branches it created still exist"
                    ),
                    "refused_repository": str(request["repository"]),
                    "refused_code": code,
                    "preflight": preflight,
                    "acquired": kept,
                    "rolled_back": rolled_back,
                },
                "errors": [{"code": code, "message": str(refused)}],
            }
        acquired.append(
            {
                "repository": str(request["repository"]),
                "owner": str(request["owner"]),
                "allocation_id": decision["allocation_id"],
                "path": decision["path"],
                "branch": str(request["branch"]),
                "record_version": 1,
            }
        )
    return {
        "command": "acquire-across",
        "schema_version": SCHEMA_VERSION,
        "decision": {
            "code": "ACROSS_ACQUIRED",
            "reason": "every required repository admitted the same task",
            "preflight": preflight,
            "acquired": acquired,
            "rolled_back": [],
        },
        "errors": [],
    }


def _roll_back_across(acquired: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Give back checkouts this attempt created, before any business write exists."""

    undone: list[dict[str, Any]] = []
    for entry in reversed(list(acquired)):
        repository_path = Path(str(entry["repository"]))
        target = Path(str(entry["path"]))
        result = {
            "repository": str(entry["repository"]),
            "allocation_id": entry["allocation_id"],
            "path": str(entry["path"]),
            "branch": entry.get("branch"),
        }
        try:
            completed = complete_no_change(
                repository_path,
                target=target,
                expected_record_version=int(entry["record_version"]),
                owner=str(entry["owner"]),
            )["decision"]
            release(
                repository_path,
                target=target,
                expected_record_version=int(completed["record_version"]),
                owner_release=str(entry["owner"]),
            )
            result["result"] = "RELEASED"
        except ControllerError as blocked:
            # A checkout that already grew work is never discarded to tidy up.
            result["result"] = "RETAINED_FOR_REVIEW"
            result["blocker"] = blocked.code
        undone.append(result)
    return undone


def _acquire_once(
    repository_path: Path,
    *,
    task_key: str,
    authority_key: str,
    module: str,
    role: str,
    owner: str,
    branch: str,
    base_ref: str,
    target: Path | None = None,
    scope_paths: tuple[str, ...] = (),
    source_allocations: tuple[str, ...] = (),
    source_handoffs: tuple[str, ...] = (),
    external_bindings: tuple[str, ...] = (),
    integration_intent_id: str | None = None,
    lineage_base_commit: str | None = None,
    pending_merge: bool = False,
    observe: bool = True,
) -> dict[str, Any]:
    if pending_merge and (role != "integration" or integration_intent_id is None):
        raise ControllerError("INVALID_INTENT", "pending merge requires a queued integration intent")
    if TASK_KEY_PATTERN.fullmatch(task_key) is None:
        raise ControllerError("INVALID_TASK_KEY", "task_key is not safe for a worktree path")
    if role not in FEATURE_AUTHORITY_ROLES | {"integration"} | RECOVERY_ROLES:
        raise ControllerError("BLOCK_CANONICAL", "acquire cannot allocate a canonical role")
    normalized_scopes = normalize_scope_paths(scope_paths)
    normalized_sources = tuple(dict.fromkeys(source_allocations))
    if len(normalized_sources) != len(source_allocations):
        raise ControllerError("INVALID_SOURCE", "source allocations must be unique")
    if role not in {"integration"} | RECOVERY_ROLES and normalized_sources:
        raise ControllerError(
            "INVALID_SOURCE", "ordinary feature work cannot claim source allocations"
        )
    if source_handoffs and role != "integration":
        raise ControllerError(
            "INVALID_SOURCE", "only integration candidates can resolve source handoffs"
        )
    if integration_intent_id is not None and role != "integration":
        raise ControllerError(
            "INVALID_INTENT", "only integration candidates can bind an integration intent"
        )
    repository_initial = inventory(repository_path)
    validate_branch_name(repository_initial.canonical_path, branch)
    base_oid = resolve_commit(repository_initial.canonical_path, base_ref)
    target_path = (target or _default_target(repository_initial, task_key)).expanduser().absolute()
    if target_path.exists() or target_path.is_symlink():
        raise ControllerError("TARGET_EXISTS", f"target path already exists: {target_path}")
    # Refusing an unconfigured repository must not create a governance marker.
    # The authoritative check is repeated under the lock in
    # _assert_allocation_allowed to catch policy changes during admission.
    if not load_policy(repository_initial.canonical_path).present:
        raise ControllerError("BLOCK_DRIFT", "repository policy is missing")
    store = MetadataStore(repository_initial.common_dir)
    transaction_id = str(uuid.uuid4())
    transaction_created = now_iso()
    with store.lock(exclusive=True, create=True):
        repository = inventory(repository_path)
        if repository.common_dir != repository_initial.common_dir:
            raise ControllerError("BLOCK_DRIFT", "repository common dir changed")
        snapshot = store._load_unlocked()
        intent: IntegrationIntent | None = None
        if integration_intent_id is not None:
            intent = snapshot.integration_intents.get(integration_intent_id)
            if intent is None:
                raise ControllerError("MISSING_INTENT", "integration intent is missing")
            if intent.state != "QUEUED" or intent.allocation_id is not None:
                raise ControllerError("INTENT_NOT_QUEUED", "integration intent is not queued")
            if (
                intent.task_key != task_key
                or intent.authority_key != authority_key
                or intent.module != module
                or intent.owner != owner
            ):
                raise ControllerError(
                    "INTEGRATION_INTENT_MISMATCH",
                    "acquire identity differs from the queued integration intent",
                )
            normalized_sources = intent.source_allocations
            normalized_scopes = intent.scope_paths
            if pending_merge:
                from .integration_retry import _sources

                _sources(repository, snapshot, store, intent)
                if len(intent.source_handoffs) != 1 or base_oid != lineage_base_commit:
                    raise ControllerError("UNSUPPORTED_CONFLICT", "pending merge requires one source and exact main base")
                blocker = _blocking_scope_intent(snapshot, intent, repository_path=repository.canonical_path)
                if blocker is not None:
                    raise ControllerError("WAITING_SCOPE", "earlier overlapping intent fences resolution")
                try:
                    build_integration_candidate(repository.canonical_path, base_oid,
                                                intent.source_handoffs, task_key=task_key)
                except ControllerError as exc:
                    if exc.code != "INTEGRATION_CONFLICT":
                        raise
                else:
                    raise ControllerError("CONFLICT_NOT_PRESENT", "normal preparation can merge these sources")
        resolved_sources = list(normalized_sources)
        for source_handoff in source_handoffs:
            handoff_oid = resolve_commit(repository.canonical_path, source_handoff)
            matches = [
                record.allocation_id
                for record in snapshot.records.values()
                if record.handoff_commit == handoff_oid
                and any(
                    event.get("event") == "HANDOFF_INTEGRATION_READY" for event in record.history
                )
            ]
            matches = _preferred_handoff_matches(snapshot, matches)
            if len(matches) != 1:
                raise ControllerError(
                    "AMBIGUOUS_SOURCE_HANDOFF",
                    "source handoff must match exactly one integration-ready allocation",
                )
            resolved_sources.append(matches[0])
        normalized_sources = tuple(dict.fromkeys(resolved_sources))
        _assert_allocation_allowed(
            repository,
            snapshot,
            task_key=task_key,
            authority_key=authority_key,
            role=role,
        )
        policy = load_policy(repository.canonical_path)
        legacy_preservation = role == "preservation" and policy.policy_schema_version == 1
        if legacy_preservation and normalized_sources:
            raise ControllerError(
                "INVALID_SOURCE", "policy v1 preservation cannot claim recovery sources"
            )
        base_locked = resolve_commit(repository.canonical_path, base_ref)
        if base_locked != base_oid:
            raise ControllerError("BLOCK_DRIFT", "base ref changed before acquire")
        record_base_oid = (
            resolve_commit(repository.canonical_path, lineage_base_commit)
            if lineage_base_commit is not None
            else base_oid
        )
        if branch_exists(repository.canonical_path, branch):
            raise ControllerError("BRANCH_EXISTS", f"local branch already exists: {branch}")
        record_scopes = normalized_scopes
        lifecycle = "ACTIVE"
        if role == "integration":
            if intent is None:
                source_heads, derived_scopes = integration_source_evidence(
                    snapshot, normalized_sources
                )
            else:
                source_heads, derived_scopes = intent.source_handoffs, intent.scope_paths
            if not is_ancestor(repository.canonical_path, record_base_oid, base_oid):
                raise ControllerError(
                    "INTEGRATION_BASE_DIVERGED",
                    "candidate revision does not contain the selected main base",
                )
            if intent is not None and not pending_merge and any(
                not is_ancestor(repository.canonical_path, source_head, base_oid)
                for source_head in source_heads
            ):
                raise ControllerError(
                    "INTEGRATION_SOURCE_MISSING",
                    "candidate revision does not contain every frozen source handoff",
                )
            if normalized_scopes and normalized_scopes != derived_scopes:
                raise ControllerError(
                    "INTEGRATION_SCOPE_MISMATCH",
                    "integration scope must equal the union of source handoff scopes",
                )
            record_scopes = derived_scopes
        elif role in RECOVERY_ROLES and not legacy_preservation:
            if len(normalized_sources) != 1:
                raise ControllerError(
                    "MISSING_RECOVERY_SOURCE",
                    "recovery allocation requires exactly one quarantined source",
                )
            source = snapshot.records.get(normalized_sources[0])
            if (
                source is None
                or source.lifecycle != "QUARANTINED"
                or source.quarantine_observed_commit is None
            ):
                raise ControllerError(
                    "INVALID_RECOVERY_SOURCE", "recovery source is not quarantined"
                )
            if base_oid != source.quarantine_observed_commit:
                raise ControllerError(
                    "RECOVERY_LINEAGE_MISMATCH",
                    "recovery checkout must start at the quarantined observed HEAD",
                )
            if not normalized_scopes:
                raise ControllerError(
                    "MISSING_RECOVERY_SCOPE",
                    "recovery allocation requires an explicit recovered-feature path scope",
                )
            record_scopes = normalized_scopes
            lifecycle = "RECOVERY_REQUIRED"
        # The current snapshot already validated repo.json under this same lock.
        repository_id = snapshot.repository_id or store.initialize_repository()
        resource_request = None
        if policy.resource_admission:
            from .resource_admission import AdmissionLedger, allocation_request

            resource_request = allocation_request(repository, repository_id, policy, target=target_path,
                base_oid=base_oid, arguments={"task_key": task_key, "authority_key": authority_key,
                    "module": module, "role": role, "owner": owner, "branch": branch,
                    "base_ref": base_oid, "target": str(target_path), "scope_paths": scope_paths,
                    "source_allocations": source_allocations, "source_handoffs": source_handoffs,
                    "external_bindings": external_bindings, "integration_intent_id": integration_intent_id,
                    "lineage_base_commit": lineage_base_commit,
                    **({"pending_merge": True} if pending_merge else {})})
            integration_sources: tuple[str, ...] = ()
            if (intent is not None and source_allocations == intent.source_allocations and not source_handoffs
                    and any(snapshot.records[allocation].authority_key == intent.authority_key
                            for allocation in intent.source_allocations)):
                from .integration_retry import _sources

                # The same frozen-source verifier used by controlled retry proves
                # this exception before taking the host lock. Requests and their
                # durable fingerprints retain their existing shape.
                integration_sources = tuple(_sources(repository, snapshot, store, intent))
            reservation = AdmissionLedger().reserve([resource_request], integration_sources=integration_sources)
            if reservation["blockers"]:
                # We already hold the repository's exclusive lock. status()
                # would attempt a second shared lock and deadlock this waiter.
                payload = envelope("acquire")
                payload["repository"] = {"repository_id": repository_id,
                                         "canonical_path": str(repository.canonical_path)}
                payload["decision"] = {**reservation, "queued": True,
                    "reason": "resource request persisted; no allocation or writer acquired"}
                return payload
        existing_worktrees = worktree_content_fingerprint(repository)
        existing_refs = public_refs(repository.canonical_path)
        record = new_record(
            task_key=task_key,
            authority_key=authority_key,
            module=module,
            role=role,
            owner=owner,
            lifecycle=lifecycle,
            base_commit=record_base_oid,
            task_commit=base_oid,
            scope_paths=record_scopes,
            source_allocations=normalized_sources,
            external_bindings=external_bindings,
            initial_event="ACQUIRED",
        )
        journal = _transaction(
            transaction_id=transaction_id,
            repository_id=repository_id,
            command="acquire",
            phase="PREPARED",
            target_path=target_path,
            allocation_id=record.allocation_id,
            branch=branch,
            expected_head=base_oid,
            expected_record_version=None,
            created_at=transaction_created,
        )
        if resource_request is not None:
            AdmissionLedger().bind(resource_request, transaction_id=transaction_id,
                                   allocation_id=record.allocation_id)
        store.write_transaction(transaction_id, journal)
        _crash_point("acquire_prepared")
        target_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        try:
            git_write(
                repository.canonical_path,
                ("worktree", "add", "-b", branch, str(target_path), base_oid),
            )
        except ControllerError:
            # Timeout or failure can follow partial Git effects. Preserve the
            # attempt so the existing positive-evidence recovery can classify it.
            if resource_request is not None:
                from .resource_admission import fingerprint

                with suppress(ControllerError, OSError):
                    AdmissionLedger().reset_no_effect_acquire(
                        store, fingerprint(resource_request), apply=True
                    )
            raise
        journal["phase"] = "WORKTREE_ADDED"
        journal["updated_at"] = now_iso()
        store.write_transaction(transaction_id, journal)
        _crash_point("acquire_worktree_added")
        repository_added = inventory(repository_path)
        added = [item for item in repository_added.worktrees if item.path == target_path]
        if len(added) != 1 or added[0].head != base_oid or added[0].branch != branch:
            raise ControllerError("BLOCK_DRIFT", "Git worktree add result does not match request")
        store.write_record(added[0].key, record, registration=added[0].git_dir)
        if intent is not None:
            claimed_at = now_iso()
            intent = _updated_intent(
                intent,
                "CANDIDATE_CLAIMED",
                state="PREPARING_INTEGRATION",
                allocation_id=record.allocation_id,
                base_main_oid=record_base_oid,
                candidate_oid=base_oid,
                revision=1,
                claimed_at=claimed_at,
                blocked_reason=None,
            )
            if pending_merge:
                event = {**intent.history[-1], "pending_merge_source": intent.source_handoffs[0],
                         "pending_merge_base": base_oid}
                intent = replace(intent, history=(*intent.history[:-1], event))
            store.replace_integration_intent(intent)
            _emit_lifecycle_event(store, repository_id, intent, "PREPARE_SLOT_GRANTED")
        journal["phase"] = "METADATA_WRITTEN"
        journal["updated_at"] = now_iso()
        store.write_transaction(transaction_id, journal)
        _crash_point("acquire_metadata_written")
        if (
            worktree_content_fingerprint(repository_added, exclude_keys=frozenset({added[0].key}))
            != existing_worktrees
        ):
            raise ControllerError("BLOCK_DRIFT", "an existing worktree changed during acquire")
        expected_refs = dict(existing_refs)
        expected_refs[f"refs/heads/{branch}"] = base_oid
        if public_refs(repository.canonical_path) != expected_refs:
            raise ControllerError("BLOCK_DRIFT", "unexpected ref changes occurred during acquire")
        store.remove_transaction(transaction_id)
        if resource_request is not None:
            AdmissionLedger().allocated(resource_request, allocation_id=record.allocation_id)
    payload = status(repository_path) if observe else envelope("acquire")
    payload["command"] = "acquire"
    payload["decision"] = {
        "code": "ACQUIRED",
        "reason": "worktree and controller allocation created",
        "path": str(target_path),
        "allocation_id": record.allocation_id,
        "head": base_oid,
        "branch": branch,
    }
    return payload


def park(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner: str,
    expected_allocation_id: str | None = None,
    require_no_writer_lease: bool = False,
    checkpoint_id: str | None = None,
    checkpoint_head: str | None = None,
    waiting_on: str | None = None,
) -> dict[str, Any]:
    """Retain an unfinished feature/audit checkout without releasing its work.

    Parking is an explicit decision by the recorded owner. It never deletes,
    cleans, or publishes anything: the checkout, its uncommitted work and its
    branch are preserved exactly. It frees a writer slot only; the checkout
    keeps costing a physical worktree against the bounded retained pool.
    """

    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        _cas(record, expected_record_version, expected_allocation_id)
        if checkpoint_id is not None:
            if not re.fullmatch(r"(?:art_|sha256:)[0-9a-f]{64}", checkpoint_id):
                raise ControllerError("INVALID_CHECKPOINT", "a content-addressed Task checkpoint reference is required")
            if checkpoint_head != item.head:
                raise ControllerError("CHECKPOINT_HEAD_MISMATCH", "checkpoint HEAD differs from this checkout")
        elif checkpoint_head is not None or waiting_on is not None:
            raise ControllerError("INVALID_CHECKPOINT", "checkpoint identity is required for dependency waiting")
        if waiting_on is not None and (not waiting_on.strip() or len(waiting_on) > 2000 or "\x00" in waiting_on):
            raise ControllerError("INVALID_CHECKPOINT", "a bounded dependency reason is required")
        policy = load_policy(repository.canonical_path)
        if policy.retained_slots < 1:
            raise ControllerError(
                "RETENTION_UNSUPPORTED",
                "repository policy does not configure a retained pool",
            )
        if item.canonical:
            raise ControllerError("BLOCK_CANONICAL", "canonical is never parked")
        if record.role not in {"feature", "audit"}:
            raise ControllerError(
                "INVALID_ROLE", "only feature or audit allocations occupy the writer pool"
            )
        if record.lifecycle != "ACTIVE" and not (record.lifecycle == "RETAINED" and checkpoint_id is not None):
            raise ControllerError(
                "INVALID_LIFECYCLE", f"lifecycle {record.lifecycle} cannot be parked"
            )
        if record.owner != owner:
            raise ControllerError("OWNER_MISMATCH", "park owner does not match current owner")
        if record.lifecycle == "RETAINED" and record.allocation_id in snapshot.writer_leases:
            raise ControllerError("WRITER_LEASE_PRESENT", "a new writer blocks checkpoint settlement")
        # Terminal hooks may have released an older lease before reaching this lock.
        # A new lease does not change record CAS, so check it in the parking mutation.
        if (require_no_writer_lease or checkpoint_id is not None) and record.allocation_id in snapshot.writer_leases:
            raise ControllerError("WRITER_LEASE_PRESENT", "writer still owns the allocation")
        if item.detached:
            raise ControllerError("DETACHED_WORKTREE", "detached worktree cannot be parked")
        if item.locked or item.prunable:
            raise ControllerError("BLOCK_DRIFT", "locked or prunable worktree requires review")
        if (
            snapshot.publication_lease is not None
            and snapshot.publication_lease.allocation_id == record.allocation_id
        ):
            raise ControllerError("PUBLICATION_LEASE_HELD", "publication is in progress")
        if record.external_bindings:
            raise ControllerError(
                "EXTERNAL_BINDING", "registered external/runtime bindings block parking"
            )
        if _intent_for_allocation(snapshot, record.allocation_id) is not None:
            raise ControllerError(
                "INTEGRATION_OBLIGATION", "an integration intent still binds this allocation"
            )
        # Parking relabels a checkout that the ordinary pool already holds. It creates
        # no worktree and takes no writer, so there is nothing left for it to exhaust;
        # the pool total still bounds how much ordinary work can exist at once.
        _cancel_release_intents(
            store,
            snapshot,
            record.allocation_id,
            event="RECORD_CHANGED",
            blocker="PARKED",
        )
        updated = _updated_record(record, "PARKED", lifecycle="RETAINED")
        if checkpoint_id is not None:
            checkpoint = {"artifact_id": checkpoint_id, "head": item.head,
                          "waiting_on": waiting_on, "owner": owner,
                          "attribution": "owner_attested_task_checkpoint",
                          "task_completed": False}
            updated = replace(updated, history=(*updated.history[:-1],
                              {**updated.history[-1], "checkpoint": checkpoint}))
        store.replace_record(updated)
        store.remove_writer_lease(record.allocation_id)
    payload = status(repository_path)
    payload["command"] = "park"
    payload["decision"] = {
        "code": "RETAINED",
        "reason": "writer slot released; checkout, work and branch preserved",
        "allocation_id": updated.allocation_id,
        "record_version": updated.record_version,
        "owner": updated.owner,
        "path": str(target),
        "checkpoint": updated.history[-1].get("checkpoint"),
    }
    return payload


def complete_no_change(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner: str,
    preserve_only: bool = False,
    expected_allocation_id: str | None = None,
    require_no_writer_lease: bool = False,
) -> dict[str, Any]:
    """Record a genuine no-change completion for a task that produced no commit.

    Audits and investigations legitimately finish without a deliverable. They have
    nothing to finalize, so they can never reach ``HANDOFF_READY`` and would occupy a
    writer slot forever. This records a real completion receipt only when every
    required piece of evidence proves there is no work to lose.
    """

    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        _cas(record, expected_record_version, expected_allocation_id)
        if require_no_writer_lease and record.allocation_id in snapshot.writer_leases:
            raise ControllerError("WRITER_LEASE_PRESENT", "end the exact writer before task completion")
        policy = load_policy(repository.canonical_path)
        if policy.retained_slots < 1:
            raise ControllerError(
                "RETENTION_UNSUPPORTED",
                "repository policy does not enable the bounded retention schema",
            )
        if item.canonical:
            raise ControllerError("BLOCK_CANONICAL", "canonical is never completed")
        if record.role not in {"feature", "audit"}:
            raise ControllerError("INVALID_ROLE", "only feature or audit tasks complete this way")
        if record.lifecycle not in {"ACTIVE", "RETAINED"}:
            raise ControllerError(
                "INVALID_LIFECYCLE", f"lifecycle {record.lifecycle} cannot complete as no-change"
            )
        if record.owner != owner:
            raise ControllerError("OWNER_MISMATCH", "completion owner does not match")
        if item.detached:
            raise ControllerError("DETACHED_WORKTREE", "detached worktree cannot complete")
        if item.locked or item.prunable:
            raise ControllerError("BLOCK_DRIFT", "locked or prunable worktree requires review")
        if not item.clean:
            raise ControllerError(
                "DIRTY_WORKTREE", "no-change completion requires a clean worktree"
            )
        if record.handoff_commit or record.finalizer_record_id:
            raise ControllerError(
                "HANDOFF_EVIDENCE_PRESENT", "handoff evidence exists; use the normal release path"
            )
        if record.quarantine_reason or record.recovery_allocation_id or record.recovery_ref:
            raise ControllerError(
                "RECOVERY_STATE", "unresolved recovery state blocks no-change completion"
            )
        if record.external_bindings:
            raise ControllerError(
                "EXTERNAL_BINDING", "registered external/runtime bindings block completion"
            )
        if (
            snapshot.publication_lease is not None
            and snapshot.publication_lease.allocation_id == record.allocation_id
        ):
            raise ControllerError("PUBLICATION_LEASE_HELD", "publication is in progress")
        if _intent_for_allocation(snapshot, record.allocation_id) is not None:
            raise ControllerError(
                "INTEGRATION_OBLIGATION", "an integration intent still binds this allocation"
            )
        # Prove there is no task-produced commit that would need a handoff.
        if record.base_commit is not None:
            base = resolve_commit(item.path, record.base_commit)
            if item.head != base:
                raise ControllerError(
                    "TASK_COMMIT_PRESENT",
                    "the checkout advanced beyond its base; a handoff is required",
                )
        else:
            mainline = f"{policy.publication_remote}/{policy.publication_branch}"
            try:
                descendant = resolve_commit(repository.canonical_path, mainline)
            except ControllerError:
                descendant = resolve_commit(repository.canonical_path, policy.publication_branch)
            if not is_ancestor(repository.canonical_path, item.head, descendant):
                raise ControllerError(
                    "TASK_COMMIT_PRESENT",
                    "the checkout carries commits outside the publication branch",
                )
        from .source_cleanup import verify_completed

        if _release_transaction(store, target_path=str(item.path), allocation_id=record.allocation_id):
            raise ControllerError("INTERRUPTED_TRANSACTION", "pending transaction blocks completion")
        archived_source = verify_completed(store, item, record)
        if archived_source and not preserve_only:
            raise ControllerError("PRESERVED_CLOSE_REQUIRED", "archived work requires close-preserved; cleanup is not task completion")
        if preserve_only and not archived_source:
            raise ControllerError("MISSING_PRESERVATION_EVIDENCE", "close-preserved requires verified source preservation and cleanup")
        if preserve_only:
            if record.allocation_id in snapshot.writer_leases:
                raise ControllerError("WRITER_LEASE_PRESENT", "writer still owns the preserved allocation")
            blockers = _typed_runtime_blockers(_typed_runtime_evidence(item, record, snapshot))
            if blockers:
                raise ControllerError(blockers[0]["code"], blockers[0]["message"])
        close_state = "PRESERVED" if preserve_only else "NO_CHANGE_COMPLETED"
        receipt_id = str(uuid.uuid4())
        receipt = {
            "receipt_id": receipt_id,
            "kind": "preserved_closeout" if preserve_only else "no_change_completion",
            "schema_version": 1,
            "repository_id": snapshot.repository_id,
            "allocation_id": record.allocation_id,
            "task_key": record.task_key,
            "authority_key": record.authority_key,
            "role": record.role,
            "owner": record.owner,
            "branch": item.branch,
            "head": item.head,
            "base_commit": record.base_commit,
            "worktree_clean": True,
            "handoff_required": False,
            "task_completed": not preserve_only,
            "source_cleanup_evidence": archived_source,
            "completion_basis": "no task-produced commit; any archived source has separate exact evidence",
            "external_bindings": list(record.external_bindings),
            "completed_at": now_iso(),
        }
        completion_ref = None
        if preserve_only:
            store.write_completion_receipt(receipt_id, receipt)
        else:
            from .cleanup_evidence import write
            from .delivered_closeout import NO_CHANGE_CONTRACT
            from .preserved_cleanup import cleanup_receipts

            receipt.update(
                contract=NO_CHANGE_CONTRACT,
                target_path=str(item.path),
                before_record=record_to_dict(record),
                completion_event=_updated_record(record, close_state, lifecycle=close_state).history[-1],
                preservation_cleanup_receipts=cleanup_receipts(
                    store, item, record, snapshot.repository_id,
                ),
            )
            completion_ref = {"receipt_id": receipt_id, **write(
                store.completion_receipts_dir, f"{receipt_id}.json", receipt,
            )}
        _cancel_release_intents(
            store,
            snapshot,
            record.allocation_id,
            event="RECORD_CHANGED",
            blocker=close_state,
        )
        updated = _updated_record(record, "PRESERVED_SOURCE_CLOSED" if preserve_only else close_state, lifecycle=close_state)
        if preserve_only:
            updated = replace(updated, history=(*updated.history[:-1],
                {**updated.history[-1], "preservation_closeout_receipt_id": receipt_id}))
        else:
            updated = replace(updated, last_reviewed_at=receipt["completion_event"]["at"],
                history=(*updated.history[:-1],
                {**receipt["completion_event"], "no_change_completion_ref": completion_ref}))
        store.replace_record(updated)
    payload = status(repository_path)
    payload["command"] = "close-preserved" if preserve_only else "complete-no-change"
    payload["decision"] = {
        "code": close_state,
        "reason": "preserved work closed without asserting business completion" if preserve_only else "no task-produced commit or uncommitted result; completion receipt recorded",
        "task_completed": not preserve_only,
        "allocation_id": updated.allocation_id,
        "record_version": updated.record_version,
        "owner": updated.owner,
        "completion_receipt_id": receipt_id,
        "head": item.head,
    }
    return payload


def close_preserved(repository_path: Path, *, target: Path, expected_record_version: int,
                    owner: str) -> dict[str, Any]:
    return complete_no_change(repository_path, target=target,
        expected_record_version=expected_record_version, owner=owner, preserve_only=True)


def _verify_preserved_closeout(store: MetadataStore, item: WorktreeGitState,
                             record: MetadataRecord, archived_source: list[dict],
                             repository_id: str | None) -> None:
    identities = [event["preservation_closeout_receipt_id"] for event in record.history
                  if event.get("event") == "PRESERVED_SOURCE_CLOSED"
                  and event.get("preservation_closeout_receipt_id")]
    if not archived_source or not identities:
        raise ControllerError("MISSING_PRESERVATION_EVIDENCE", "preserved closure evidence is missing")
    receipt = store._read_json(store.completion_receipts_dir / f"{identities[-1]}.json")
    expected = {"kind": "preserved_closeout", "repository_id": repository_id,
        "allocation_id": record.allocation_id, "owner": record.owner,
        "authority_key": record.authority_key, "task_key": record.task_key,
        "head": item.head, "branch": item.branch, "task_completed": False,
        "source_cleanup_evidence": archived_source}
    if receipt.get("task_completed") is not False or any(receipt.get(key) != value for key, value in expected.items()):
        raise ControllerError("PRESERVATION_CLOSEOUT_DRIFT", "preserved closure identity changed")


def abort_integration(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner: str,
    reason: str,
) -> dict[str, Any]:
    """Terminate a failed or owner-aborted integration preparation.

    A candidate whose validation fails terminally (for example on task scope) can
    neither validate nor reopen, so its temporary integration checkout would stay
    ``ACTIVE`` forever. This records the failure and makes only the integration
    allocation releasable. The source allocation, its branch and its handoff
    evidence are untouched; this is never a source completion signal.
    """

    detail = reason.strip()
    if not detail or len(detail) > 512 or "\x00" in detail:
        raise ControllerError("INVALID_ABORT_REASON", "abort reason must be short explicit text")
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        _cas(record, expected_record_version)
        policy = load_policy(repository.canonical_path)
        if policy.retained_slots < 1:
            raise ControllerError(
                "RETENTION_UNSUPPORTED",
                "repository policy does not enable the bounded retention schema",
            )
        if record.role != "integration":
            raise ControllerError("INVALID_ROLE", "only an integration allocation can abort")
        if record.owner != owner:
            raise ControllerError("OWNER_MISMATCH", "abort owner does not match current owner")
        if record.lifecycle not in {"ACTIVE", "QUARANTINED"}:
            raise ControllerError(
                "INVALID_LIFECYCLE", f"lifecycle {record.lifecycle} cannot abort preparation"
            )
        if (
            snapshot.publication_lease is not None
            and snapshot.publication_lease.allocation_id == record.allocation_id
        ):
            raise ControllerError(
                "PUBLICATION_LEASE_HELD", "release or abort the publication lease first"
            )
        intent = _intent_for_allocation(snapshot, record.allocation_id)
        receipt_id = str(uuid.uuid4())
        receipt = {
            "receipt_id": receipt_id,
            "kind": "integration_abort",
            "schema_version": 1,
            "repository_id": snapshot.repository_id,
            "allocation_id": record.allocation_id,
            "task_key": record.task_key,
            "authority_key": record.authority_key,
            "owner": record.owner,
            "branch": item.branch,
            "candidate_oid": item.head,
            "failure_reason": detail,
            "intent_id": None if intent is None else intent.intent_id,
            "intent_state": None if intent is None else intent.state,
            "source_allocations": [] if intent is None else list(intent.source_allocations),
            "source_handoffs": [] if intent is None else list(intent.source_handoffs),
            "source_branches": [] if intent is None else list(intent.source_branches),
            "source_finalizer_record_ids": (
                [] if intent is None else list(intent.source_finalizer_record_ids)
            ),
            "validation_evidence_id": None if intent is None else intent.validation_evidence_id,
            "tests_receipt_id": None if intent is None else intent.tests_receipt_id,
            "snapshot_receipt_id": None if intent is None else intent.snapshot_receipt_id,
            "aborted_at": now_iso(),
        }
        store.write_completion_receipt(receipt_id, receipt)
        if intent is not None:
            store.replace_integration_intent(
                _updated_intent(
                    intent,
                    "PREPARATION_ABORTED",
                    state="ATTENTION_REQUIRED",
                    blocked_reason=detail,
                    allocation_id=None,
                )
            )
        updated = _updated_record(record, "INTEGRATION_ABORTED", lifecycle="INTEGRATION_ABORTED")
        # The abort is an independent immutable fact, including when the old
        # intent was detached or its source allocations later advanced.
        from .cleanup_evidence import read as read_evidence

        _, receipt_sha = read_evidence(store.completion_receipts_dir / f"{receipt_id}.json")
        history = list(updated.history)
        history[-1] = {**history[-1], "abort_receipt": {
            "receipt_id": receipt_id, "sha256": receipt_sha,
        }}
        updated = replace(updated, history=tuple(history))
        store.replace_record(updated)
    payload = status(repository_path)
    payload["command"] = "integration-abort"
    payload["decision"] = {
        "code": "INTEGRATION_ABORTED",
        "reason": "failed preparation recorded; source candidate and evidence preserved",
        "allocation_id": updated.allocation_id,
        "record_version": updated.record_version,
        "abort_receipt_id": receipt_id,
        "failure_reason": detail,
    }
    return payload


def reconcile_session(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner: str,
    trigger: str,
    expected_allocation_id: str | None = None,
) -> dict[str, Any]:
    """Terminal reconciliation for a task session that stopped.

    This is the entry point a directly launched executor session uses on exit, and the
    one Agent Workspace already calls on task-terminal/session-archive. Stopping is
    never treated as completion: an unfinished task is only ever moved to
    ``RETAINED``, which preserves the checkout, the work and the branch. A finished
    task that already carries handoff evidence follows the existing release-intent
    path unchanged.
    """

    if trigger not in {"task-terminal", "session-archive", "manual"}:
        raise ControllerError("INVALID_TRIGGER", "unsupported session reconciliation trigger")
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        _item, record = _managed_record(repository, snapshot, target)
        _cas(record, expected_record_version, expected_allocation_id)
        if record.owner != owner:
            raise ControllerError("OWNER_MISMATCH", "session owner does not match current owner")
        lifecycle = record.lifecycle
        role = record.role
    if role in {"feature", "audit"} and lifecycle == "HANDOFF_READY":
        payload = create_release_intent(
            repository_path,
            target=target,
            expected_record_version=expected_record_version,
            owner=owner,
            trigger=trigger,
            expected_allocation_id=record.allocation_id,
        )
        payload["command"] = "reconcile-session"
        return payload
    if role in {"feature", "audit"} and lifecycle == "ACTIVE":
        payload = park(
            repository_path,
            target=target,
            expected_record_version=expected_record_version,
            owner=owner,
            expected_allocation_id=record.allocation_id,
            require_no_writer_lease=True,
        )
        payload["command"] = "reconcile-session"
        payload["decision"]["reason"] = (
            f"session ended ({trigger}); unfinished task retained, nothing released"
        )
        return payload
    payload = status(repository_path)
    payload["command"] = "reconcile-session"
    payload["decision"] = {
        "code": "NO_TERMINAL_ACTION",
        "reason": f"lifecycle {lifecycle} for role {role} has no safe session terminal action",
        "allocation_id": record.allocation_id,
        "record_version": record.record_version,
    }
    return payload


def resume(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner: str,
    expected_allocation_id: str | None = None,
    require_no_writer_lease: bool = False,
) -> dict[str, Any]:
    """Return a retained checkout to writer status under normal admission."""

    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        _cas(record, expected_record_version, expected_allocation_id)
        if require_no_writer_lease and record.allocation_id in snapshot.writer_leases:
            raise ControllerError("WRITER_LEASE_PRESENT", "an existing writer blocks task resume")
        if record.lifecycle != "RETAINED":
            raise ControllerError(
                "INVALID_LIFECYCLE", f"lifecycle {record.lifecycle} is not retained"
            )
        if record.owner != owner:
            raise ControllerError("OWNER_MISMATCH", "resume owner does not match current owner")
        if item.detached:
            raise ControllerError("DETACHED_WORKTREE", "detached worktree cannot resume")
        updated = _updated_record(record, "RESUMED", lifecycle="ACTIVE")
        store.replace_record(updated)
    payload = status(repository_path)
    payload["command"] = "resume"
    payload["decision"] = {
        "code": "RESUMED",
        "reason": "retained allocation re-admitted as a writer under CAS",
        "allocation_id": updated.allocation_id,
        "record_version": updated.record_version,
        "owner": updated.owner,
        "path": str(target),
    }
    return payload


def adopt_delivered(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    new_owner: str,
    actor: str,
) -> dict[str, Any]:
    """Take ownership of an allocation whose deliverable is provably already in main.

    An allocation whose owning session is gone can otherwise never be closed, because
    every terminal verb needs that owner. This is not impersonation: it records an
    explicit ownership transfer, performed by a named actor, and only when the work is
    proven complete and nothing can be lost. Unfinished, dirty, bound or uncertain
    allocations are refused, so the ownership gate is untouched for live work.
    """

    owner = new_owner.strip()
    actor_name = actor.strip()
    if not owner or not actor_name:
        raise ControllerError("INVALID_OWNER", "adoption requires an owner and a named actor")
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        _cas(record, expected_record_version)
        policy = load_policy(repository.canonical_path)
        if item.canonical:
            raise ControllerError("BLOCK_CANONICAL", "canonical ownership is never adopted")
        if record.owner == owner:
            raise ControllerError("OWNER_UNCHANGED", "allocation already has this owner")
        if record.lifecycle not in {"HANDOFF_READY", "PRESERVED"}:
            raise ControllerError(
                "INVALID_LIFECYCLE",
                f"lifecycle {record.lifecycle} is not proven complete; adoption is refused",
            )
        if not record.handoff_commit or not record.finalizer_record_id:
            raise ControllerError(
                "MISSING_EVIDENCE", "adoption requires handoff and Finalizer evidence"
            )
        if item.detached:
            raise ControllerError("DETACHED_WORKTREE", "detached worktree cannot be adopted")
        if item.locked or item.prunable:
            raise ControllerError("BLOCK_DRIFT", "locked or prunable worktree requires review")
        if not item.clean:
            raise ControllerError("DIRTY_WORKTREE", "adoption requires a clean worktree")
        if record.external_bindings:
            raise ControllerError(
                "EXTERNAL_BINDING", "registered external/runtime bindings block adoption"
            )
        if record.disposable_resources:
            raise ControllerError(
                "DISPOSABLE_RESOURCE_PRESENT",
                "the recorded owner must dispose registered resources first",
            )
        if (
            snapshot.publication_lease is not None
            and snapshot.publication_lease.allocation_id == record.allocation_id
        ):
            raise ControllerError("PUBLICATION_LEASE_HELD", "publication is in progress")
        if _intent_for_allocation(snapshot, record.allocation_id) is not None:
            raise ControllerError(
                "INTEGRATION_OBLIGATION", "an integration intent still binds this allocation"
            )
        handoff_oid = resolve_commit(item.path, record.handoff_commit)
        if handoff_oid != item.head:
            raise ControllerError("HEAD_MISMATCH", "worktree HEAD drifted after handoff")
        mainline = f"{policy.publication_remote}/{policy.publication_branch}"
        try:
            delivered = resolve_commit(repository.canonical_path, mainline)
        except ControllerError:
            delivered = resolve_commit(repository.canonical_path, policy.publication_branch)
        if not is_ancestor(repository.canonical_path, handoff_oid, delivered):
            raise ControllerError(
                "DELIVERABLE_NOT_PUBLISHED",
                "the handoff commit is not contained in the publication branch",
            )
        _cancel_release_intents(
            store,
            snapshot,
            record.allocation_id,
            event="RECORD_CHANGED",
            blocker="OWNER_ADOPTED",
        )
        updated = _updated_record(record, "OWNER_ADOPTED", owner=owner)
        store.replace_record(updated)
    payload = status(repository_path)
    payload["command"] = "adopt-delivered"
    payload["decision"] = {
        "code": "OWNER_ADOPTED",
        "reason": "delivered allocation adopted under recorded evidence",
        "allocation_id": updated.allocation_id,
        "record_version": updated.record_version,
        "previous_owner": record.owner,
        "owner": updated.owner,
        "actor": actor_name,
        "handoff_commit": record.handoff_commit,
        "finalizer_record_id": record.finalizer_record_id,
        "published_in": f"{policy.publication_remote}/{policy.publication_branch}",
    }
    return payload


def abandon_integration_intent(
    repository_path: Path,
    *,
    intent_id: str,
    owner: str,
    reason: str,
) -> dict[str, Any]:
    """Terminate an intent whose preparation is already gone.

    An aborted or crashed preparation can leave a queued intent holding its scope
    claim with no live allocation behind it, which blocks every later intent that
    touches the same paths. Terminating it requires that no live allocation remains,
    so nothing in flight is discarded, and the failure reason is preserved.
    """

    detail = reason.strip()
    if not detail or len(detail) > 512:
        raise ControllerError("INVALID_ABORT_REASON", "abandon reason must be short explicit text")
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    with store.lock(exclusive=True):
        snapshot = store._load_unlocked()
        intent = snapshot.integration_intents.get(intent_id)
        if intent is None:
            raise ControllerError("UNKNOWN_INTENT", "integration intent does not exist")
        if intent.owner != owner:
            raise ControllerError("OWNER_MISMATCH", "intent owner does not match")
        effective_state = _effective_intent_state(snapshot, intent)
        if effective_state in TERMINAL_INTEGRATION_STATES and effective_state != "ABANDONED":
            raise ControllerError("INVALID_LIFECYCLE", "intent is already terminal")
        if intent.state == "PUBLISHING" or (
            snapshot.publication_lease is not None
            and snapshot.publication_lease.intent_id == intent_id
        ):
            raise ControllerError("PUBLICATION_LEASE_HELD", "publication is in progress")
        if intent.allocation_id is not None:
            record = snapshot.records.get(intent.allocation_id)
            if record is not None and record.lifecycle != "RELEASED":
                raise ControllerError(
                    "LIVE_ALLOCATION",
                    "abort the integration allocation before abandoning its intent",
                )
        updated = intent
        if effective_state != "ABANDONED":
            updated = _updated_intent(
                intent,
                "PREPARATION_ABANDONED",
                state="ATTENTION_REQUIRED",
                blocked_reason=detail,
                allocation_id=None,
            )
            store.replace_integration_intent(updated)
    payload = status(repository_path)
    payload["command"] = "integration-abandon"
    payload["decision"] = {
        "code": "INTEGRATION_ABANDONED",
        "reason": "intent terminated; scope claim released and failure reason preserved",
        "intent_id": intent_id,
        "previous_state": intent.state,
        "failure_reason": updated.blocked_reason,
        "source_allocations": list(intent.source_allocations),
        "source_handoffs": list(intent.source_handoffs),
    }
    return payload


def reconcile_release_plans(
    repository_path: Path,
    *,
    actor: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Retire unreferenced spent plans whose allocation is already released.

    A receipt's original plan is durable closed-lifecycle evidence. Only leftover
    plans not referenced by any release receipt may be retired after the allocation
    is released and another durable receipt proves its closeout.
    """

    actor_name = actor.strip()
    if not actor_name:
        raise ControllerError("INVALID_ACTOR", "reconciliation requires a named actor")
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    outcomes: list[dict[str, Any]] = []
    with store.lock(exclusive=True):
        snapshot = store._load_unlocked()
        receipts_by_allocation = {
            receipt.allocation_id for receipt in snapshot.release_receipts.values()
        }
        referenced_plans = {receipt.plan_id for receipt in snapshot.release_receipts.values()}
        for plan_id, plan in sorted(snapshot.release_plans.items()):
            record = snapshot.records.get(plan.allocation_id)
            if record is None:
                outcomes.append({"plan_id": plan_id, "result": "KEPT", "reason": "unknown record"})
                continue
            if record.lifecycle != "RELEASED":
                outcomes.append(
                    {"plan_id": plan_id, "result": "KEPT", "reason": "allocation is not released"}
                )
                continue
            if plan.allocation_id not in receipts_by_allocation:
                outcomes.append(
                    {"plan_id": plan_id, "result": "KEPT", "reason": "no durable release receipt"}
                )
                continue
            if plan_id in referenced_plans:
                outcomes.append(
                    {"plan_id": plan_id, "result": "KEPT", "reason": "referenced by durable release receipt"}
                )
                continue
            if not dry_run:
                store.remove_release_plan(plan_id)
            outcomes.append(
                {
                    "plan_id": plan_id,
                    "result": "RETIRED",
                    "allocation_id": plan.allocation_id,
                    "lifecycle": plan.lifecycle,
                    "actor": actor_name,
                }
            )
    payload = status(repository_path)
    payload["command"] = "reconcile"
    payload["decision"] = {
        "code": "RELEASE_PLANS_RECONCILED",
        "reason": "unreferenced spent release plans retired; receipt-backed plans preserved",
        "dry_run": dry_run,
        "retired": sum(1 for item in outcomes if item["result"] == "RETIRED"),
        "kept": sum(1 for item in outcomes if item["result"] == "KEPT"),
        "outcomes": outcomes,
    }
    return payload


def review(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner: str,
    new_owner: str | None = None,
    add_scope_paths: tuple[str, ...] = (),
    expected_head: str | None = None,
    scope_reason: str | None = None,
) -> dict[str, Any]:
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        _cas(record, expected_record_version)
        if (
            snapshot.publication_lease is not None
            and snapshot.publication_lease.allocation_id == record.allocation_id
        ):
            raise ControllerError(
                "PUBLICATION_LEASE_HELD", "owner review is frozen during publication"
            )
        if record.owner != owner:
            raise ControllerError("OWNER_MISMATCH", "review owner does not match current owner")
        additions = normalize_scope_paths(add_scope_paths)
        if bool(additions) != (expected_head is not None and scope_reason is not None):
            raise ControllerError(
                "INVALID_SCOPE_AMENDMENT", "scope additions require exact HEAD and a review reason"
            )
        if not additions and (expected_head is not None or scope_reason is not None):
            raise ControllerError("INVALID_SCOPE_AMENDMENT", "scope evidence requires additions")
        if additions:
            if (
                new_owner is not None
                or not scope_reason
                or not scope_reason.strip()
                or len(scope_reason) > 512
            ):
                raise ControllerError(
                    "INVALID_SCOPE_AMENDMENT", "amend scope separately with a bounded reason"
                )
            if record.role not in {"feature", "audit", "hotfix"} or record.source_allocations:
                raise ControllerError("INVALID_ROLE", "scope repair is limited to source tasks")
            if record.lifecycle != "ACTIVE" or record.handoff_commit is not None:
                raise ControllerError(
                    "INVALID_LIFECYCLE", "reopen a frozen source before scope review"
                )
            if any(
                record.allocation_id in intent.source_allocations
                and intent.state not in TERMINAL_INTEGRATION_STATES
                and not integration_intent_abandoned(intent)
                for intent in snapshot.integration_intents.values()
            ):
                raise ControllerError(
                    "SCOPE_FROZEN", "an active integration still binds the source scope"
                )
            if not item.clean:
                raise ControllerError(
                    "DIRTY_WORKTREE", "scope review requires a clean committed result"
                )
            if expected_head != item.head:
                raise ControllerError(
                    "HEAD_MISMATCH", "scope review requires the exact observed HEAD"
                )
            if not record.base_commit or not is_ancestor(item.path, record.base_commit, item.head):
                raise ControllerError(
                    "SCOPE_BASE_MISMATCH", "scope review requires the original task base"
                )
            missing = {
                path
                for path in changed_paths(item.path, record.base_commit, item.head)
                if not path_in_scopes(path, record.scope_paths)
            }
            if not set(additions) <= missing:
                raise ControllerError(
                    "SCOPE_MISMATCH", "add only exact omitted files in the committed task diff"
                )
        _cancel_release_intents(
            store,
            snapshot,
            record.allocation_id,
            event="RECORD_CHANGED",
            blocker="OWNER_OR_REVIEW_CHANGED",
        )
        updated = _updated_record(
            record,
            "SCOPE_AMENDED"
            if additions
            else "OWNER_TRANSFER"
            if new_owner is not None
            else "REVIEWED",
            owner=(new_owner or owner).strip(),
            scope_paths=normalize_scope_paths((*record.scope_paths, *additions)),
        )
        if additions:
            event = {
                **updated.history[-1],
                "expected_record_version": record.record_version,
                "head": item.head,
                "base_commit": record.base_commit,
                "before_scope": list(record.scope_paths),
                "after_scope": list(updated.scope_paths),
                "reason": scope_reason.strip(),
            }
            updated = replace(updated, history=(*updated.history[:-1], event))
        store.replace_record(updated)
    payload = status(repository_path)
    payload["command"] = "review"
    payload["decision"] = {
        "code": "SCOPE_AMENDED" if additions else "REVIEWED",
        "reason": "omitted committed paths reviewed under owner, HEAD and record CAS"
        if additions
        else "owner and review timestamp updated under CAS",
        "allocation_id": updated.allocation_id,
        "record_version": updated.record_version,
        "owner": updated.owner,
    }
    return payload


def handoff(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    kind: str,
    commit: str,
    finalizer_record_id: str,
    finalizer_commit: str | None = None,
    scope_paths: tuple[str, ...] = (),
    mainline_commit: str | None = None,
    scope_changed: bool = False,
    expected_allocation_id: str | None = None,
    expected_owner: str | None = None,
    require_no_writer_lease: bool = False,
) -> dict[str, Any]:
    if kind not in {"integration-ready", "preserved"}:
        raise ControllerError("INVALID_HANDOFF", "handoff kind is invalid")
    if scope_changed and scope_paths:
        raise ControllerError("SCOPE_MISMATCH", "--scope-changed derives the scope; do not also pass --scope")
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        _cas(record, expected_record_version, expected_allocation_id)
        if expected_owner is not None and record.owner != expected_owner:
            raise ControllerError("OWNER_MISMATCH", "handoff owner does not match")
        if require_no_writer_lease and record.allocation_id in snapshot.writer_leases:
            raise ControllerError("WRITER_LEASE_PRESENT", "end the exact writer before task handoff")
        if record.lifecycle in {"QUARANTINED", "INTEGRATING", "PUBLISHED", "RELEASED"}:
            raise ControllerError(
                "INVALID_LIFECYCLE", f"lifecycle {record.lifecycle} cannot be handed off"
            )
        if not item.clean:
            raise ControllerError("DIRTY_WORKTREE", "handoff requires a clean worktree")
        commit_oid = resolve_commit(item.path, commit)
        if commit_oid != item.head:
            raise ControllerError("HEAD_MISMATCH", "handoff commit must equal worktree HEAD")
        if not finalizer_record_id.strip():
            raise ControllerError("MISSING_FINALIZER_RECORD", "handoff requires Finalizer evidence")
        finalizer_oid = resolve_commit(item.path, finalizer_commit or commit_oid)
        if finalizer_oid != commit_oid:
            raise ControllerError(
                "FINALIZER_HEAD_MISMATCH", "Finalizer evidence must bind the handoff commit"
            )
        requested_scopes = normalize_scope_paths(scope_paths)
        unscoped_feature = record.role in FEATURE_AUTHORITY_ROLES and not record.scope_paths
        task_changes: tuple[str, ...] = ()
        if unscoped_feature and (scope_changed or (kind == "integration-ready" and not requested_scopes)):
            # The exact paths this task changed since its publication base, the same
            # attribution later scope checks use; nothing outside them is claimed.
            formal = publication_ref(item.path, load_policy(repository.canonical_path))
            if record.base_commit:
                base = attribution_base(item.path, record.base_commit, commit_oid, formal)
                task_changes = normalize_scope_paths(changed_paths(item.path, base, commit_oid))
        if scope_changed:
            if not unscoped_feature:
                raise ControllerError(
                    "SCOPE_MISMATCH", "--scope-changed only records a scope for an unscoped feature task"
                )
            if not task_changes:
                raise ControllerError(
                    "MISSING_HANDOFF_SCOPE",
                    "the handoff commit changes no paths; use complete-no-change instead",
                )
            requested_scopes = task_changes
        unscoped_changes = (
            not scope_changed
            and kind == "integration-ready"
            and unscoped_feature
            and not requested_scopes
            and bool(task_changes)
        )
        if record.scope_paths and requested_scopes and record.scope_paths != requested_scopes:
            raise ControllerError("SCOPE_MISMATCH", "handoff cannot change the recorded task scope")
        effective_scopes = record.scope_paths or requested_scopes
        if mainline_commit is not None and record.role != "integration":
            raise ControllerError(
                "INVALID_MAINLINE", "only integration handoff can update its mainline commit"
            )
        mainline_oid = (
            resolve_commit(item.path, mainline_commit)
            if mainline_commit is not None
            else record.base_commit
        )
        candidate_record = replace(record, scope_paths=effective_scopes, base_commit=mainline_oid)
        if record.role == "integration":
            issues = integration_candidate_issues(
                item, candidate_record, snapshot, require_task_match=False
            )
        else:
            issues = path_scope_issues(
                item.path,
                candidate_record,
                commit_oid,
                publication_ref=publication_ref(item.path, load_policy(repository.canonical_path)),
            )
        if issues:
            issue = issues[0]
            raise ControllerError(issue["code"], issue["message"])
        lifecycle = "HANDOFF_READY" if kind == "integration-ready" else "PRESERVED"
        _cancel_release_intents(
            store,
            snapshot,
            record.allocation_id,
            event="HANDOFF_REPLACED",
            blocker="HANDOFF_IDENTITY_CHANGED",
        )
        updated = _updated_record(
            record,
            "HANDOFF_INTEGRATION_READY" if kind == "integration-ready" else "HANDOFF_PRESERVED",
            lifecycle=lifecycle,
            handoff_commit=commit_oid,
            finalizer_record_id=finalizer_record_id.strip(),
            task_commit=commit_oid,
            finalizer_commit=finalizer_oid,
            scope_paths=effective_scopes,
            base_commit=mainline_oid,
        )
        store.replace_record(updated)
    payload = status(repository_path)
    payload["command"] = "handoff"
    payload["decision"] = {
        "code": "HANDOFF_READY" if kind == "integration-ready" else "PRESERVED",
        "reason": "clean handoff evidence recorded",
        "allocation_id": updated.allocation_id,
        "record_version": updated.record_version,
        "commit": commit_oid,
    }
    if unscoped_changes:
        # integration-enqueue refuses a source without a scope. Say so now, while the
        # same handoff can still be repeated with the exact changed paths.
        payload["warnings"] = [*payload.get("warnings", []), _warning(
            "MISSING_HANDOFF_SCOPE",
            "changed work has no recorded scope; integration-enqueue will refuse it",
            allocation_id=updated.allocation_id,
            next_action="repeat handoff on this record with --scope-changed",
        )]
    return payload


def reopen(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner: str,
) -> dict[str, Any]:
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        _cas(record, expected_record_version)
        if record.owner != owner:
            raise ControllerError("OWNER_MISMATCH", "reopen owner does not match")
        if record.role not in {"feature", "audit", "hotfix"}:
            raise ControllerError(
                "INVALID_ROLE", "reopen is limited to feature/audit/hotfix allocations"
            )
        if record.lifecycle not in {"HANDOFF_READY", "PRESERVED", "ARCHIVED"}:
            raise ControllerError(
                "INVALID_LIFECYCLE", "reopen requires HANDOFF_READY, PRESERVED or ARCHIVED lifecycle"
            )
        if not item.clean:
            raise ControllerError(
                "DIRTY_WORKTREE", "reopen requires the frozen checkout to be clean"
            )
        archived = record.lifecycle == "ARCHIVED"
        if archived:
            from .delivered_closeout import verify_archive_receipt
            from .transaction_scope import conflicts

            verify_archive_receipt(store, item, record, snapshot.repository_id)
            if conflicts(store, allocation=record.allocation_id, target=item.path):
                raise ControllerError(
                    "INTERRUPTED_TRANSACTION", "archive release transaction needs recovery"
                )
        if not archived and (
            record.handoff_commit is None
            or record.task_commit is None
            or record.finalizer_commit is None
            or record.finalizer_record_id is None
            or any(
                identity != item.head
                for identity in (
                    record.handoff_commit,
                    record.task_commit,
                    record.finalizer_commit,
                )
            )
        ):
            raise ControllerError(
                "IDENTITY_MISMATCH", "reopen requires the exact frozen handoff identity"
            )
        if record.external_bindings:
            from .writer_lease import check_writer_entitlement

            lease = snapshot.writer_leases.get(record.allocation_id)
            try:
                check_writer_entitlement(
                    snapshot.repository_id, record, item.path, lease,
                    lease.holder if lease is not None else None,
                )
            except ControllerError as exc:
                raise ControllerError(
                    "EXTERNAL_BINDING", "reopen requires bindings owned by an exact current writer lease"
                ) from exc
        _cancel_release_intents(
            store,
            snapshot,
            record.allocation_id,
            event="ALLOCATION_REOPENED",
            blocker="REOPENED",
        )
        updated = (
            _updated_record(record, "ARCHIVE_REOPENED", lifecycle="RETAINED")
            if archived
            else _updated_record(
                record,
                "REOPENED",
                lifecycle="ACTIVE",
                handoff_commit=None,
                finalizer_record_id=None,
                task_commit=None,
                finalizer_commit=None,
            )
        )
        store.replace_record(updated)
    payload = status(repository_path)
    payload["command"] = "reopen"
    payload["decision"] = {
        "code": "REOPENED",
        "reason": (
            "archive release authority invalidated; task remains retained"
            if archived else "handoff evidence and pending release authority invalidated before new work"
        ),
        "allocation_id": updated.allocation_id,
        "record_version": updated.record_version,
        "lifecycle": updated.lifecycle,
    }
    return payload


def update_external_binding(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner: str,
    binding: str,
    action: str,
    holder_pid: int | None = None,
    observe: bool = True,
    recover_archived_codex_session: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    binding_value = binding.strip()
    if not binding_value or "\x00" in binding_value:
        raise ControllerError("INVALID_BINDING", "binding identity is required")
    if action not in {"add", "remove", "transfer"}:
        raise ControllerError("INVALID_BINDING", "binding action is invalid")
    if (recover_archived_codex_session and action != "remove"
            or dry_run and not recover_archived_codex_session):
        raise ControllerError("INVALID_BINDING", "dry-run requires archived native removal")
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    changed = False
    native_recovery = None
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        _item, record = _managed_record(repository, snapshot, target)
        _cas(record, expected_record_version)
        if record.owner != owner:
            raise ControllerError("OWNER_MISMATCH", "binding owner does not match")
        bindings = list(record.external_bindings)
        from .writer_lease import (
            RUNTIME_BINDING_PREFIX,
            assert_runtime_clear,
            describe_holder,
            holder_is_live,
            runtime_binding,
            same_holder,
        )

        declared = runtime_binding(binding_value)
        if recover_archived_codex_session and binding_value not in bindings:
            # A caller first re-reads current CAS after an interrupted removal.
            # Only the exact completed native receipt licenses an idempotent result.
            from . import cleanup_evidence

            previous_path = (store.native_session_recoveries_dir
                             / f"{record.allocation_id}-{record.record_version - 1}.json")
            previous, digest = cleanup_evidence.read(previous_path)
            if (previous.get("allocation_id") != record.allocation_id
                    or previous.get("owner") != owner or previous.get("binding") != binding_value):
                raise ControllerError("NATIVE_SESSION_RECOVERY_REFUSED",
                                      "no exact completed native removal receipt exists")
            if not dry_run and declared is not None:
                from .resource_admission import release_execution

                release_execution(snapshot.repository_id, record.allocation_id, declared[1])
            payload = envelope("binding-remove")
            payload["decision"] = {"code": "BINDING_REMOVED", "changed": False,
                "allocation_id": record.allocation_id, "record_version": record.record_version,
                "binding": binding_value, "native_recovery": {
                    "path": str(previous_path), "sha256": digest}, "dry_run": dry_run}
            return payload
        if action == "transfer" and (declared is None or binding_value not in bindings or holder_pid is None):
            raise ControllerError("INVALID_RUNTIME_BINDING", "transfer requires its current typed source occupancy")
        if declared is not None and action in {"add", "transfer"}:
            lease = snapshot.writer_leases.get(record.allocation_id)
            if lease is None or declared[1] != lease.lease_id or not same_holder(declared[0], lease.holder) or not holder_is_live(declared[0]):
                raise ControllerError("WRITER_LEASE_MISMATCH", "runtime registration requires its exact live writer holder")
        if action == "transfer":
            from dataclasses import asdict

            from .environment import verify_runtime_transfer

            child = describe_holder(holder_pid, client_id=declared[0].client_id,
                                    session_id=declared[0].session_id, run_id=declared[0].run_id)
            verify_runtime_transfer(declared[0], child)
            binding_value = RUNTIME_BINDING_PREFIX + json.dumps(
                {"holder": asdict(child), "lease_id": declared[1], "kind": "background"},
                sort_keys=True, separators=(",", ":"))
        if declared is not None and action == "remove":
            if recover_archived_codex_session:
                from .native_session_recovery import observe_archived_session

                if record.allocation_id in snapshot.writer_leases:
                    raise ControllerError("NATIVE_SESSION_RECOVERY_REFUSED",
                                          "release the exact writer lease before native recovery")
                if json.loads(binding_value[len(RUNTIME_BINDING_PREFIX):])["kind"] != "agent":
                    raise ControllerError("NATIVE_SESSION_RECOVERY_REFUSED",
                                          "native recovery accepts only an agent binding")
                native_recovery = observe_archived_session(_item.path, declared[0])
            else:
                assert_runtime_clear(_item.path, (declared[0],))
        elif recover_archived_codex_session:
            raise ControllerError("NATIVE_SESSION_RECOVERY_REFUSED",
                                  "native recovery requires an exact typed agent binding")
        if dry_run:
            payload = envelope("binding-remove")
            payload["decision"] = {"code": "NATIVE_SESSION_RECOVERY_READY",
                "allocation_id": record.allocation_id, "record_version": record.record_version,
                "dry_run": True, "changed": False, "native_recovery": native_recovery}
            return payload
        if native_recovery is not None:
            from . import cleanup_evidence

            directory = store.native_session_recoveries_dir
            name = f"{record.allocation_id}-{record.record_version}.json"
            expected = {"allocation_id": record.allocation_id,
                        "record_version": record.record_version, "owner": owner,
                        "binding": binding_value, "evidence": native_recovery}
            if (directory / name).exists():
                previous, digest = cleanup_evidence.read(directory / name)
                stable = ("allocation_id", "record_version", "owner", "binding")
                if (any(previous.get(key) != expected[key] for key in stable)
                        or previous.get("evidence", {}).get("holder") != native_recovery["holder"]
                        or any(previous.get("evidence", {}).get("native", {}).get(key)
                               != native_recovery["native"][key] for key in (
                                   "thread_id", "archived_rollout", "session_metadata_sha256"))):
                    raise ControllerError("NATIVE_SESSION_RECOVERY_REFUSED",
                                          "interrupted native removal evidence differs")
                native_recovery = {"path": str(directory / name), "sha256": digest}
            else:
                native_recovery = cleanup_evidence.write(directory, name, expected)
        if action in {"add", "transfer"}:
            if record.lifecycle != "ACTIVE":
                raise ControllerError(
                    "INVALID_LIFECYCLE", "new active bindings require lifecycle ACTIVE"
                )
            if declared is not None and action == "add":
                from .resource_admission import bind_execution

                # Reserve execution before publishing occupancy. A lost binding
                # write is reconciled from this exact holder, without replaying it.
                bind_execution(snapshot.repository_id, record.allocation_id, declared[0], declared[1],
                               required=load_policy(repository.canonical_path).resource_admission)
            if binding_value not in bindings:
                bindings.append(binding_value)
                changed = True
        elif binding_value in bindings:
            bindings.remove(binding_value)
            changed = True
        if changed:
            _cancel_release_intents(
                store,
                snapshot,
                record.allocation_id,
                event="BINDING_CHANGED",
                blocker="BINDING_IDENTITY_CHANGED",
            )
            updated = _updated_record(
                record,
                "EXTERNAL_BINDING_REMOVED" if action == "remove" else "EXTERNAL_BINDING_ADDED",
                external_bindings=tuple(sorted(bindings)),
            )
            store.replace_record(updated)
        else:
            updated = record
        if declared is not None:
            from .resource_admission import release_execution

            if action == "remove" and not any(
                (other := runtime_binding(value)) is not None and other[1] == declared[1]
                for value in updated.external_bindings
            ):
                release_execution(snapshot.repository_id, record.allocation_id, declared[1])
    payload = status(repository_path) if observe else envelope(f"binding-{action}")
    payload["command"] = f"binding-{action}"
    payload["decision"] = {
        "code": {"add": "BINDING_ADDED", "remove": "BINDING_REMOVED", "transfer": "BINDING_TRANSFERRED"}[action],
        "reason": "active binding registry updated" if changed else "binding state already matched",
        "allocation_id": updated.allocation_id,
        "record_version": updated.record_version,
        "binding": binding_value,
        "changed": changed,
    }
    if native_recovery is not None:
        payload["decision"]["native_recovery"] = native_recovery
    return payload


def record_equivalent_replay(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner: str,
    source_allocation_id: str,
    source_handoff: str,
    replay_commit: str,
    evidence_id: str,
) -> dict[str, Any]:
    if not evidence_id.strip():
        raise ControllerError(
            "MISSING_VALIDATION_EVIDENCE", "equivalent replay evidence is required"
        )
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        _cas(record, expected_record_version)
        if record.owner != owner:
            raise ControllerError("OWNER_MISMATCH", "equivalent replay owner does not match")
        if record.role != "integration" or record.lifecycle != "ACTIVE":
            raise ControllerError(
                "INVALID_LIFECYCLE", "equivalent replay requires an active integration candidate"
            )
        if item.detached or item.branch is None or not item.clean:
            raise ControllerError(
                "DIRTY_WORKTREE",
                "equivalent replay requires a clean attached integration candidate",
            )
        intent = _intent_for_allocation(snapshot, record.allocation_id)
        if intent is None or intent.state != "PREPARING_INTEGRATION":
            raise ControllerError(
                "INTEGRATION_INTENT_MISMATCH",
                "equivalent replay requires a claimed integration intent",
            )
        if source_allocation_id not in intent.source_allocations:
            raise ControllerError(
                "MISSING_SOURCE_ALLOCATION", "source is not frozen in this intent"
            )
        source_index = intent.source_allocations.index(source_allocation_id)
        source_handoff_oid = resolve_commit(item.path, source_handoff)
        if source_handoff_oid != intent.source_handoffs[source_index]:
            raise ControllerError("SOURCE_EVIDENCE_MISMATCH", "source handoff identity differs")
        replay_oid = resolve_commit(item.path, replay_commit)
        if not is_ancestor(item.path, replay_oid, item.head):
            raise ControllerError(
                "EQUIVALENT_REPLAY_DRIFT", "replay commit is not contained by candidate HEAD"
            )
        replays = [
            dict(replay)
            for replay in intent.equivalent_replays
            if replay["source_allocation_id"] != source_allocation_id
        ]
        replays.append(
            {
                "source_allocation_id": source_allocation_id,
                "source_handoff_oid": source_handoff_oid,
                "replay_oid": replay_oid,
                "evidence_id": evidence_id.strip(),
            }
        )
        replays.sort(key=lambda replay: replay["source_allocation_id"])
        candidate_changed = intent.candidate_oid != item.head
        updated_intent = _updated_intent(
            intent,
            "EQUIVALENT_REPLAY_RECORDED",
            equivalent_replays=replays,
            candidate_oid=item.head,
            revision=intent.revision + (1 if candidate_changed else 0),
            validation_evidence_id=None,
            validated_oid=None,
            validated_at=None,
            blocked_reason=None,
        )
        updated_record = _updated_record(
            record,
            "EQUIVALENT_REPLAY_RECORDED",
            task_commit=item.head,
            handoff_commit=None,
            finalizer_record_id=None,
            finalizer_commit=None,
        )
        store.replace_integration_intent(updated_intent)
        store.replace_record(updated_record)
    payload = status(repository_path)
    payload["command"] = "integration-equivalent"
    payload["decision"] = {
        "code": "EQUIVALENT_REPLAY_RECORDED",
        "reason": "source replay evidence bound to the exact integration candidate",
        "allocation_id": updated_record.allocation_id,
        "record_version": updated_record.record_version,
        "source_allocation_id": source_allocation_id,
        "source_handoff_oid": source_handoff_oid,
        "replay_oid": replay_oid,
        "evidence_id": evidence_id.strip(),
    }
    return payload


def validate_integration(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner: str,
    validation_evidence_id: str,
    tests_receipt_id: str | None = None,
    snapshot_receipt_id: str | None = None,
) -> dict[str, Any]:
    evidence = validation_evidence_id.strip()
    if not evidence or "\x00" in evidence or len(evidence) > 512:
        raise ControllerError("INVALID_VALIDATION_EVIDENCE", "validation evidence id is invalid")
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        _cas(record, expected_record_version)
        intent = _intent_for_allocation(snapshot, record.allocation_id)
        if record.owner != owner:
            raise ControllerError("OWNER_MISMATCH", "validation owner does not match")
        if record.role != "integration" or intent is None:
            raise ControllerError(
                "MISSING_INTENT", "managed integration validation requires a frozen intent"
            )
        if record.lifecycle != "ACTIVE" or intent.state != "PREPARING_INTEGRATION":
            raise ControllerError(
                "INVALID_LIFECYCLE", "only a claimed active candidate can be validated"
            )
        if item.detached or not item.clean or item.head != intent.candidate_oid:
            raise ControllerError(
                "CANDIDATE_IDENTITY_MISMATCH", "candidate changed before validation evidence"
            )
        issues = integration_candidate_issues(item, record, snapshot)
        if issues:
            issue = issues[0]
            raise ControllerError(issue["code"], issue["message"])
        repository_id = snapshot.repository_id
        if repository_id is None or intent.base_main_oid is None:
            raise ControllerError(
                "BLOCK_DRIFT", "prepared candidate repository identity is missing"
            )
        tests_evidence = (tests_receipt_id or evidence).strip()
        snapshot_evidence = (snapshot_receipt_id or evidence).strip()
        if not tests_evidence or not snapshot_evidence:
            raise ControllerError(
                "MISSING_PREPARATION_EVIDENCE", "tests and Snapshot receipt identities are required"
            )
        policy = load_policy(repository.canonical_path)
        if policy.policy_schema_version >= 4 and (
            tests_receipt_id is None or snapshot_receipt_id is None
        ):
            raise ControllerError(
                "MISSING_PREPARATION_EVIDENCE",
                "policy schema v4 requires explicit tests and Snapshot receipts",
            )
        tree_oid = commit_tree_oid(item.path, item.head)
        identity_payload = {
            "allocation_id": record.allocation_id,
            "attempt": intent.attempt,
            "base_origin_main_oid": intent.base_main_oid,
            "candidate_oid": item.head,
            "intent_id": intent.intent_id,
            "scope_digest": intent.scope_digest,
            "snapshot_receipt_id": snapshot_evidence,
            "tests_receipt_id": tests_evidence,
            "tree_oid": tree_oid,
            "validation_evidence_id": evidence,
        }
        prepare_identity = hashlib.sha256(
            json.dumps(
                identity_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        receipt_id = str(
            uuid.uuid5(
                uuid.UUID(repository_id),
                f"prepared-candidate-v1:{intent.intent_id}:{prepare_identity}",
            )
        )
        timestamp = now_iso()
        existing_prepared = snapshot.prepared_candidate_receipts.get(receipt_id)
        prepared_receipt = PreparedCandidateReceipt(
            schema_version=PREPARED_CANDIDATE_RECEIPT_SCHEMA_VERSION,
            receipt_id=receipt_id,
            repository_id=repository_id,
            intent_id=intent.intent_id,
            allocation_id=record.allocation_id,
            attempt=intent.attempt,
            base_origin_main_oid=intent.base_main_oid,
            candidate_oid=item.head,
            tree_oid=tree_oid,
            scope_digest=intent.scope_digest,
            prepare_identity=prepare_identity,
            validation_evidence_id=evidence,
            tests_receipt_id=tests_evidence,
            snapshot_receipt_id=snapshot_evidence,
            created_at=(
                existing_prepared.created_at if existing_prepared is not None else timestamp
            ),
        )
        store.write_prepared_candidate_receipt(prepared_receipt)
        updated_record = _updated_record(
            record,
            "INTEGRATION_VALIDATED",
            lifecycle="HANDOFF_READY",
            handoff_commit=item.head,
            finalizer_record_id=evidence,
            task_commit=item.head,
            finalizer_commit=item.head,
        )
        updated_intent = _updated_intent(
            intent,
            "INTEGRATION_VALIDATED",
            state="READY_TO_PUBLISH",
            prepare_identity=prepare_identity,
            prepared_receipt_id=receipt_id,
            prepared_base_origin_main_oid=intent.base_main_oid,
            prepared_commit_oid=item.head,
            prepared_tree_oid=tree_oid,
            prepared_scope_digest=intent.scope_digest,
            tests_receipt_id=tests_evidence,
            snapshot_receipt_id=snapshot_evidence,
            validation_evidence_id=evidence,
            validated_oid=item.head,
            validated_at=timestamp,
            blocked_reason=None,
        )
        store.replace_record(updated_record)
        store.replace_integration_intent(updated_intent)
        _emit_lifecycle_event(store, repository_id, updated_intent, "PREPARED")
    payload = status(repository_path)
    payload["command"] = "integration-validate"
    payload["decision"] = {
        "code": "INTEGRATION_PREPARED",
        "reason": "tests and Snapshot evidence are bound to the exact prepared candidate",
        "record_version": updated_record.record_version,
        **_intent_payload(updated_intent),
    }
    return payload


def reopen_integration(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner: str,
) -> dict[str, Any]:
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        _cas(record, expected_record_version)
        intent = _intent_for_allocation(snapshot, record.allocation_id)
        if record.owner != owner:
            raise ControllerError("OWNER_MISMATCH", "integration reopen owner does not match")
        if record.role != "integration" or intent is None:
            raise ControllerError("MISSING_INTENT", "integration reopen requires a bound intent")
        if record.lifecycle != "HANDOFF_READY" or intent.state != "READY_TO_PUBLISH":
            raise ControllerError(
                "INVALID_LIFECYCLE", "integration reopen requires READY_TO_PUBLISH"
            )
        if snapshot.publication_lease is not None:
            raise ControllerError(
                "PUBLICATION_LEASE_HELD", "publication lease must be absent before reopen"
            )
        if (
            item.detached
            or not item.clean
            or item.branch is None
            or item.head != intent.candidate_oid
            or item.head != intent.prepared_commit_oid
            or record.task_commit != item.head
            or record.handoff_commit != item.head
        ):
            raise ControllerError(
                "CANDIDATE_IDENTITY_MISMATCH",
                "integration reopen requires the exact clean prepared identity",
            )
        updated_record = _updated_record(
            record,
            "INTEGRATION_REOPENED",
            lifecycle="ACTIVE",
            handoff_commit=None,
            finalizer_record_id=None,
            finalizer_commit=None,
        )
        updated_intent = _updated_intent(
            intent,
            "INTEGRATION_REOPENED",
            state="PREPARING_INTEGRATION",
            attempt=intent.attempt + 1,
            prepare_identity=None,
            publication_identity=None,
            recovery_identity=None,
            prepared_receipt_id=None,
            prepared_base_origin_main_oid=None,
            prepared_commit_oid=None,
            prepared_tree_oid=None,
            prepared_scope_digest=None,
            tests_receipt_id=None,
            snapshot_receipt_id=None,
            validation_evidence_id=None,
            validated_oid=None,
            validated_at=None,
            blocked_reason=None,
        )
        store.replace_record(updated_record)
        store.replace_integration_intent(updated_intent)
        if snapshot.repository_id is None:
            raise ControllerError("BLOCK_DRIFT", "repository metadata is missing")
        _emit_lifecycle_event(store, snapshot.repository_id, updated_intent, "PREPARE_SLOT_GRANTED")
    payload = status(repository_path)
    payload["command"] = "integration-reopen"
    payload["decision"] = {
        "code": "INTEGRATION_REOPENED",
        "reason": "prepared eligibility invalidated before further controlled writes",
        "record_version": updated_record.record_version,
        **_intent_payload(updated_intent),
    }
    return payload


def refresh_integration(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner: str,
) -> dict[str, Any]:
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    decision_code = "INTEGRATION_REFRESH_NOT_NEEDED"
    decision_reason = "candidate already matches the latest main base"
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        policy = load_policy(repository.canonical_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        _cas(record, expected_record_version)
        intent = _intent_for_allocation(snapshot, record.allocation_id)
        if record.owner != owner:
            raise ControllerError("OWNER_MISMATCH", "refresh owner does not match")
        if record.role != "integration" or intent is None:
            raise ControllerError("MISSING_INTENT", "candidate has no frozen integration intent")
        expected_pair = (
            record.lifecycle == "HANDOFF_READY" and intent.state == "READY_TO_PUBLISH"
        ) or (record.lifecycle == "ACTIVE" and intent.state == "NEEDS_REPREPARE")
        if not expected_pair:
            raise ControllerError(
                "INVALID_LIFECYCLE",
                "only a prepared or explicitly stale candidate can refresh its main base",
            )
        if item.detached or not item.clean or item.branch is None:
            raise ControllerError("DIRTY_WORKTREE", "refresh requires a clean attached candidate")
        if item.head != intent.candidate_oid or record.task_commit != item.head:
            raise ControllerError(
                "CANDIDATE_IDENTITY_MISMATCH",
                "candidate or validation evidence changed before refresh",
            )
        remote_head = fetch_remote_branch(
            repository.canonical_path,
            policy.publication_remote,
            policy.publication_branch,
        )
        updated_record = record
        updated_intent = intent
        candidate_oid: str | None = None
        if remote_head == intent.base_main_oid:
            if intent.state == "NEEDS_REPREPARE":
                raise ControllerError(
                    "BLOCK_DRIFT", "stale candidate marker does not differ from current main"
                )
        else:
            try:
                candidate_oid = build_integration_candidate(
                    repository.canonical_path,
                    remote_head,
                    intent.source_handoffs,
                    task_key=intent.task_key,
                )
            except ControllerError as exc:
                if exc.code != "INTEGRATION_CONFLICT":
                    raise
                updated_record = _updated_record(
                    record, "INTEGRATION_REFRESH_BLOCKED", lifecycle="BLOCKED"
                )
                updated_intent = _updated_intent(
                    intent,
                    "INTEGRATION_REFRESH_BLOCKED",
                    state="ATTENTION_REQUIRED",
                    base_main_oid=remote_head,
                    blocked_reason=str(exc),
                )
                store.replace_record(updated_record)
                store.replace_integration_intent(updated_intent)
                if snapshot.repository_id is None:
                    raise ControllerError("BLOCK_DRIFT", "repository metadata is missing") from exc
                _emit_lifecycle_event(
                    store, snapshot.repository_id, updated_intent, "ATTENTION_REQUIRED"
                )
                decision_code = "INTEGRATION_REFRESH_CONFLICT"
                decision_reason = str(exc)
        if candidate_oid is not None:
            if candidate_oid == item.head:
                raise ControllerError(
                    "BLOCK_DRIFT",
                    "stale main produced the existing candidate identity",
                )
            else:
                repository_id = snapshot.repository_id
                if repository_id is None:
                    raise ControllerError("BLOCK_DRIFT", "repository metadata is missing")
                transaction_id = str(uuid.uuid4())
                journal = _transaction(
                    transaction_id=transaction_id,
                    repository_id=repository_id,
                    command="integration-refresh",
                    phase="REFRESH_PREPARED",
                    target_path=item.path,
                    allocation_id=record.allocation_id,
                    branch=item.branch,
                    expected_head=item.head,
                    expected_record_version=record.record_version,
                    created_at=now_iso(),
                )
                journal["new_head"] = candidate_oid
                journal["intent_id"] = intent.intent_id
                store.write_transaction(transaction_id, journal)
                _crash_point("refresh_prepared")
                checkout_detached(item.path, candidate_oid)
                journal["phase"] = "DETACHED_AT_NEW_HEAD"
                journal["updated_at"] = now_iso()
                store.write_transaction(transaction_id, journal)
                _crash_point("refresh_detached")
                update_branch_ref(
                    repository.canonical_path,
                    item.branch,
                    expected=item.head,
                    new=candidate_oid,
                )
                journal["phase"] = "BRANCH_UPDATED"
                journal["updated_at"] = now_iso()
                store.write_transaction(transaction_id, journal)
                _crash_point("refresh_branch_updated")
                checkout_branch(item.path, item.branch)
                refreshed_repository = inventory(repository_path)
                refreshed_item, refreshed_record = _managed_record(
                    refreshed_repository, snapshot, target
                )
                if (
                    refreshed_record.allocation_id != record.allocation_id
                    or refreshed_item.head != candidate_oid
                    or refreshed_item.branch != item.branch
                    or not refreshed_item.clean
                ):
                    raise ControllerError("BLOCK_DRIFT", "candidate refresh result is inconsistent")
                updated_record = _updated_record(
                    record,
                    "INTEGRATION_REFRESHED",
                    lifecycle="ACTIVE",
                    base_commit=remote_head,
                    task_commit=candidate_oid,
                    handoff_commit=None,
                    finalizer_record_id=None,
                    finalizer_commit=None,
                )
                updated_intent = _updated_intent(
                    intent,
                    "INTEGRATION_REFRESHED",
                    state="PREPARING_INTEGRATION",
                    base_main_oid=remote_head,
                    candidate_oid=candidate_oid,
                    revision=intent.revision + 1,
                    attempt=intent.attempt + 1,
                    prepare_identity=None,
                    publication_identity=None,
                    prepared_receipt_id=None,
                    prepared_base_origin_main_oid=None,
                    prepared_commit_oid=None,
                    prepared_tree_oid=None,
                    prepared_scope_digest=None,
                    tests_receipt_id=None,
                    snapshot_receipt_id=None,
                    validation_evidence_id=None,
                    validated_oid=None,
                    validated_at=None,
                    blocked_reason=None,
                )
                store.replace_record(updated_record)
                store.replace_integration_intent(updated_intent)
                _emit_lifecycle_event(store, repository_id, updated_intent, "PREPARE_SLOT_GRANTED")
                store.remove_transaction(transaction_id)
                decision_code = "INTEGRATION_REFRESHED"
                decision_reason = (
                    "candidate rebuilt from frozen handoffs and latest main; revalidation required"
                )
    payload = status(repository_path)
    payload["command"] = "integration-refresh"
    payload["decision"] = {
        "code": decision_code,
        "reason": decision_reason,
        "record_version": updated_record.record_version,
        **_intent_payload(updated_intent),
    }
    return payload


def quarantine(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner: str,
    reason: str,
    expected_commit: str | None = None,
) -> dict[str, Any]:
    if not reason.strip():
        raise ControllerError("INVALID_REASON", "quarantine requires a non-empty reason")
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        _cas(record, expected_record_version)
        if record.owner != owner:
            raise ControllerError("OWNER_MISMATCH", "quarantine owner does not match")
        if record.role != "integration":
            raise ControllerError("INVALID_ROLE", "only integration candidates can be quarantined")
        if record.lifecycle in {"PUBLISHED", "RELEASED", "RECOVERY_REQUIRED"}:
            raise ControllerError(
                "INVALID_LIFECYCLE", f"lifecycle {record.lifecycle} cannot be quarantined"
            )
        lease = snapshot.publication_lease
        if lease is not None and lease.allocation_id == record.allocation_id:
            raise ControllerError(
                "PUBLICATION_LEASE_HELD", "abort the publication lease before quarantine"
            )
        if not item.clean:
            raise ControllerError("DIRTY_WORKTREE", "quarantine requires a clean worktree")
        expected_value = (
            expected_commit or record.task_commit or record.handoff_commit or record.base_commit
        )
        if expected_value is None:
            raise ControllerError(
                "MISSING_EXPECTED_COMMIT", "quarantine requires the last legitimate commit"
            )
        expected_oid = resolve_commit(item.path, expected_value)
        recorded_expected = record.task_commit or record.handoff_commit or record.base_commit
        if recorded_expected is not None and expected_oid != resolve_commit(
            item.path, recorded_expected
        ):
            raise ControllerError(
                "EXPECTED_COMMIT_MISMATCH",
                "quarantine expected commit must equal the latest recorded task evidence",
            )
        if not is_ancestor(item.path, expected_oid, item.head):
            raise ControllerError(
                "RECOVERY_LINEAGE_MISMATCH",
                "last legitimate commit is not an ancestor of the observed HEAD",
            )
        updated = _updated_record(
            record,
            "QUARANTINED",
            lifecycle="QUARANTINED",
            quarantine_expected_commit=expected_oid,
            quarantine_observed_commit=item.head,
            quarantine_reason=reason.strip(),
        )
        store.replace_record(updated)
        intent = _intent_for_allocation(snapshot, record.allocation_id)
        if intent is not None:
            recovery_identity = hashlib.sha256(
                f"{intent.intent_id}:{record.allocation_id}:{item.head}:{updated.record_version}".encode()
            ).hexdigest()
            recovery_intent = _updated_intent(
                intent,
                "INTEGRATION_RECOVERY_REQUIRED",
                state="RECOVERY_REQUIRED",
                recovery_identity=recovery_identity,
                publication_identity=None,
                blocked_reason=reason.strip(),
            )
            store.replace_integration_intent(recovery_intent)
            if snapshot.repository_id is None:
                raise ControllerError("BLOCK_DRIFT", "repository metadata is missing")
            _emit_lifecycle_event(
                store, snapshot.repository_id, recovery_intent, "RECOVERY_REQUIRED"
            )
    payload = status(repository_path)
    payload["command"] = "quarantine"
    payload["decision"] = {
        "code": "QUARANTINED",
        "reason": "integration candidate isolated for explicit recovery",
        "allocation_id": updated.allocation_id,
        "record_version": updated.record_version,
        "expected_commit": expected_oid,
        "observed_commit": item.head,
    }
    return payload


def adopt_recovery(
    repository_path: Path,
    *,
    source_target: Path,
    recovery_target: Path,
    expected_source_record_version: int,
    expected_recovery_record_version: int,
    source_owner: str,
    recovery_owner: str,
    scope_paths: tuple[str, ...] = (),
) -> dict[str, Any]:
    requested_scopes = normalize_scope_paths(scope_paths)
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        source_item, source = _managed_record(repository, snapshot, source_target)
        recovery_item, recovery = _managed_record(repository, snapshot, recovery_target)
        _cas(source, expected_source_record_version)
        _cas(recovery, expected_recovery_record_version)
        if source.owner != source_owner or recovery.owner != recovery_owner:
            raise ControllerError("OWNER_MISMATCH", "recovery adoption owner does not match")
        if source.lifecycle != "QUARANTINED" or not source.quarantine_observed_commit:
            raise ControllerError("INVALID_LIFECYCLE", "source is not quarantined")
        if recovery.role not in {"feature"} | RECOVERY_ROLES:
            raise ControllerError("INVALID_ROLE", "checkout cannot be adopted for recovery")
        if recovery.lifecycle not in {"ACTIVE", "PRESERVED", "RECOVERY_REQUIRED"}:
            raise ControllerError(
                "INVALID_LIFECYCLE", "checkout lifecycle cannot be adopted for recovery"
            )
        if recovery.source_allocations not in {(), (source.allocation_id,)}:
            raise ControllerError("RECOVERY_LINEAGE_MISMATCH", "checkout claims another source")
        if recovery.scope_paths and requested_scopes and recovery.scope_paths != requested_scopes:
            raise ControllerError(
                "SCOPE_MISMATCH", "recovery adoption cannot replace an existing task scope"
            )
        recovery_scopes = recovery.scope_paths or requested_scopes
        if not recovery_scopes:
            raise ControllerError(
                "MISSING_RECOVERY_SCOPE",
                "recovery adoption requires an explicit recovered-feature path scope",
            )
        if not source_item.clean or not recovery_item.clean:
            raise ControllerError("DIRTY_WORKTREE", "recovery adoption requires clean worktrees")
        if recovery_item.detached or recovery_item.branch is None:
            raise ControllerError("DETACHED_WORKTREE", "recovery checkout must retain a branch")
        if not is_ancestor(
            recovery_item.path, source.quarantine_observed_commit, recovery_item.head
        ):
            raise ControllerError(
                "RECOVERY_LINEAGE_MISMATCH",
                "recovery checkout does not contain the quarantined observed HEAD",
            )
        updated = _updated_record(
            recovery,
            "RECOVERY_ADOPTED",
            role="recovery",
            lifecycle="RECOVERY_REQUIRED",
            base_commit=source.quarantine_observed_commit,
            task_commit=recovery_item.head,
            scope_paths=recovery_scopes,
            source_allocations=(source.allocation_id,),
        )
        store.replace_record(updated)
        intent = _intent_for_allocation(snapshot, source.allocation_id)
        if intent is not None:
            if intent.state != "RECOVERY_REQUIRED":
                raise ControllerError(
                    "INTEGRATION_INTENT_MISMATCH", "recovery adoption intent state differs"
                )
            recovery_intent = _updated_intent(
                intent,
                "RECOVERY_RESERVE_ADOPTED",
                recovery_identity=updated.allocation_id,
            )
            store.replace_integration_intent(recovery_intent)
            if snapshot.repository_id is None:
                raise ControllerError("BLOCK_DRIFT", "repository metadata is missing")
            _emit_lifecycle_event(
                store, snapshot.repository_id, recovery_intent, "RECOVERY_REQUIRED"
            )
    payload = status(repository_path)
    payload["command"] = "recovery-adopt"
    payload["decision"] = {
        "code": "RECOVERY_ADOPTED",
        "reason": "existing checkout moved under governance recovery capacity",
        "allocation_id": updated.allocation_id,
        "record_version": updated.record_version,
        "source_allocation_id": source.allocation_id,
        "head": recovery_item.head,
    }
    return payload


def externalize(
    repository_path: Path,
    *,
    source_target: Path,
    recovery_target: Path,
    expected_source_record_version: int,
    expected_recovery_record_version: int,
    owner: str,
) -> dict[str, Any]:
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        source_item, source = _managed_record(repository, snapshot, source_target)
        recovery_item, recovery = _managed_record(repository, snapshot, recovery_target)
        _cas(source, expected_source_record_version)
        _cas(recovery, expected_recovery_record_version)
        if source.owner != owner:
            raise ControllerError("OWNER_MISMATCH", "externalization owner does not match")
        if recovery.owner != owner:
            raise ControllerError("OWNER_MISMATCH", "recovery allocation owner does not match")
        if source.lifecycle != "QUARANTINED" or not source.quarantine_observed_commit:
            raise ControllerError("INVALID_LIFECYCLE", "source is not quarantined")
        if (
            recovery.role not in RECOVERY_ROLES
            or source.allocation_id not in recovery.source_allocations
        ):
            raise ControllerError(
                "RECOVERY_LINEAGE_MISMATCH", "target is not a recovery allocation for this source"
            )
        if not source_item.clean or not recovery_item.clean:
            raise ControllerError("DIRTY_WORKTREE", "externalization requires clean worktrees")
        if recovery_item.detached or recovery_item.branch is None:
            raise ControllerError("DETACHED_WORKTREE", "recovery allocation must retain a branch")
        if not is_ancestor(
            recovery_item.path, source.quarantine_observed_commit, recovery_item.head
        ):
            raise ControllerError(
                "RECOVERY_LINEAGE_MISMATCH",
                "recovery allocation does not contain the quarantined observed HEAD",
            )
        recovery_ref = f"refs/heads/{recovery_item.branch}"
        updated = _updated_record(
            source,
            "RECOVERY_EXTERNALIZED",
            lifecycle="RECOVERY_REQUIRED",
            recovery_allocation_id=recovery.allocation_id,
            recovery_ref=recovery_ref,
        )
        store.replace_record(updated)
    payload = status(repository_path)
    payload["command"] = "externalize"
    payload["decision"] = {
        "code": "RECOVERY_EXTERNALIZED",
        "reason": "quarantined lineage is retained by a recovery allocation",
        "allocation_id": updated.allocation_id,
        "record_version": updated.record_version,
        "recovery_allocation_id": recovery.allocation_id,
        "recovery_ref": recovery_ref,
    }
    return payload


def _resource_relative_path(value: str, kind: str) -> str:
    if kind != "python-venv":
        raise ControllerError("INVALID_RESOURCE", f"unsupported disposable resource: {kind}")
    path = PurePosixPath(value)
    if path.is_absolute() or path.parts != (".venv",):
        raise ControllerError(
            "INVALID_RESOURCE", "python-venv disposable resource must be the worktree .venv"
        )
    return path.as_posix()


def _published_handoff_resource_proof(
    repository: RepositoryInventory,
    snapshot: MetadataSnapshot,
    store: MetadataStore,
    item: WorktreeGitState,
    record: MetadataRecord,
    *,
    reproducible: bool,
    creator: str,
    teardown_policy: str,
) -> dict[str, Any]:
    """Classify an existing published hotfix environment without reopening its handoff."""
    from .delivered_closeout import _publication
    from .transaction_scope import conflicts

    if reproducible is not True or teardown_policy != "on-release":
        raise ControllerError(
            "INVALID_RESOURCE",
            "published handoff registration requires reproducible on-release .venv",
        )
    attestation = f"handoff-owner-attestation:{record.owner}"
    if creator.strip() != attestation:
        raise ControllerError(
            "INVALID_RESOURCE",
            "published handoff registration requires the recorded owner's attestation",
        )
    if not snapshot.repository_id:
        raise ControllerError(
            "INVALID_METADATA", "published handoff repository identity is missing"
        )
    if any(
        (
            record.quarantine_reason,
            record.quarantine_expected_commit,
            record.quarantine_observed_commit,
            record.recovery_ref,
            record.recovery_allocation_id,
        )
    ):
        raise ControllerError("RECOVERY_STATE", "recovery lineage keeps its existing recovery flow")
    if record.allocation_id in snapshot.writer_leases:
        raise ControllerError(
            "WRITER_LEASE_PRESENT", "writer lease blocks frozen resource registration"
        )
    if (
        snapshot.publication_lease
        and snapshot.publication_lease.allocation_id == record.allocation_id
    ):
        raise ControllerError("PUBLICATION_LEASE_HELD", "publication is in progress")
    if conflicts(store, allocation=record.allocation_id, target=item.path, assume_serialized=True):
        raise ControllerError(
            "INTERRUPTED_TRANSACTION", "resource registration requires terminal transactions"
        )
    _validate_release_core(
        repository,
        snapshot,
        item,
        record,
        store=store,
        owner_release=record.owner,
        strict_identity=True,
        allow_registered_venv=True,
    )
    if (
        item.branch is None
        or resolve_commit(repository.canonical_path, f"refs/heads/{item.branch}") != item.head
    ):
        raise ControllerError(
            "MISSING_RETAINED_REF", "the checkout branch no longer retains its HEAD"
        )
    if not record.base_commit or not is_ancestor(
        repository.canonical_path, record.base_commit, item.head
    ):
        raise ControllerError(
            "TASK_LINEAGE_UNREACHABLE", "recorded base is not on the frozen handoff branch"
        )
    if any(
        intent.allocation_id == record.allocation_id
        or (
            record.allocation_id in intent.source_allocations
            and (
                _effective_intent_state(snapshot, intent) not in TERMINAL_INTEGRATION_STATES
                or not is_ancestor(
                    repository.canonical_path,
                    intent.source_handoffs[intent.source_allocations.index(record.allocation_id)],
                    item.head,
                )
            )
        )
        for intent in snapshot.integration_intents.values()
    ) or any(
        other.lifecycle != "RELEASED" and record.allocation_id in other.source_allocations
        for other in snapshot.records.values()
    ):
        raise ControllerError(
            "INTEGRATION_OBLIGATION", "integration lineage still references this allocation"
        )
    blockers = _typed_runtime_blockers(_typed_runtime_evidence(item, record, snapshot))
    if blockers:
        raise ControllerError(blockers[0]["code"], blockers[0]["message"])
    publication = _publication(repository)
    if any(
        not is_ancestor(repository.canonical_path, oid, publication["oid"])
        for oid in (item.head, record.base_commit)
    ):
        raise ControllerError(
            "DELIVERABLE_NOT_PUBLISHED", "the frozen handoff and base are not published"
        )
    environment = _teardown_preflight(repository, item)
    return {
        "contract": "published-handoff-resource-registration/v1",
        "repository_id": snapshot.repository_id,
        "allocation_id": record.allocation_id,
        "expected_record_version": record.record_version,
        "owner": record.owner,
        "task_key": record.task_key,
        "authority_key": record.authority_key,
        "resource_path": ".venv",
        "creator": attestation,
        "frozen_identity": {
            "head": item.head,
            "base_commit": record.base_commit,
            "task_commit": record.task_commit,
            "handoff_commit": record.handoff_commit,
            "finalizer_commit": record.finalizer_commit,
            "finalizer_record_id": record.finalizer_record_id,
        },
        "publication": publication,
        "environment": environment,
    }


def register_disposable(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner: str,
    resource_path: str,
    kind: str,
    reproducible: bool,
    creator: str,
    teardown_policy: str,
) -> dict[str, Any]:
    relative = _resource_relative_path(resource_path, kind)
    if not creator.strip():
        raise ControllerError("INVALID_RESOURCE", "resource creator is required")
    if teardown_policy not in {"manual", "on-release"}:
        raise ControllerError("INVALID_RESOURCE", "resource teardown policy is invalid")
    if teardown_policy == "on-release" and not reproducible:
        raise ControllerError(
            "INVALID_RESOURCE", "on-release resources must be explicitly reproducible"
        )
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        _cas(record, expected_record_version)
        if record.owner != owner:
            raise ControllerError("OWNER_MISMATCH", "resource owner does not match")
        pending_release = any(
            intent.allocation_id == record.allocation_id
            and intent.status in {"PENDING", "BLOCKED_RETRYABLE"}
            for intent in snapshot.release_intents.values()
        ) or (
            # A candidate that published and could not finish its cleanup is the same
            # situation: the publication receipt is the evidence, and only the content
            # this task created is still in the way.
            record.role == "integration"
            and record.lifecycle == "PUBLISHED"
            and any(
                receipt.allocation_id == record.allocation_id
                for receipt in snapshot.publication_receipts.values()
            )
        )
        published_handoff = (
            record.lifecycle == "HANDOFF_READY" and record.role == "hotfix" and not pending_release
        )
        if record.lifecycle not in (
            {"ACTIVE", "RETAINED", "HANDOFF_READY", "PUBLISHED"} | EVIDENCE_EXEMPT_RELEASE_STATES
        ) or (
            record.lifecycle not in ({"ACTIVE", "RETAINED"} | EVIDENCE_EXEMPT_RELEASE_STATES)
            and not pending_release
            and not published_handoff
        ):
            # Evidence-exempt terminal states may still classify a rebuildable environment
            # that the task created, otherwise an aborted preparation can never converge.
            # A live release intent is the same situation reached from the other side: the
            # release already earned its evidence and is waiting on this classification.
            # A retained checkout has no frozen evidence to disturb, and without this it
            # could only close by reopening a writer it does not need.
            # Registration is metadata only; every teardown preflight gate is unchanged.
            raise ControllerError(
                "INVALID_LIFECYCLE",
                "disposable resources must be registered while ACTIVE or after a terminal abort",
            )
        if tracked_paths(item.path, relative):
            raise ControllerError("TRACKED_RESOURCE", "tracked content cannot be disposable")
        if not path_is_ignored(item.path, relative):
            raise ControllerError("RESOURCE_NOT_IGNORED", "disposable resource must be Git-ignored")
        absolute = item.path / relative
        if (
            not absolute.is_dir()
            or absolute.is_symlink()
            or absolute.is_mount()
            or not (absolute / "pyvenv.cfg").is_file()
            or (absolute / "pyvenv.cfg").is_symlink()
            or (absolute / ".git").exists()
        ):
            raise ControllerError(
                "RESOURCE_IDENTITY_MISMATCH",
                "disposable resource must already be a recognizable task-local Python venv",
            )
        if any(resource["path"] == relative for resource in record.disposable_resources):
            raise ControllerError("RESOURCE_EXISTS", "disposable resource is already registered")
        proof = None
        if published_handoff:
            proof = _published_handoff_resource_proof(
                repository,
                snapshot,
                store,
                item,
                record,
                reproducible=reproducible,
                creator=creator,
                teardown_policy=teardown_policy,
            )
        resource_id = str(uuid.uuid4())
        resources = [dict(resource) for resource in record.disposable_resources]
        resources.append(
            {
                "resource_id": resource_id,
                "path": relative,
                "kind": kind,
                "reproducible": reproducible,
                "creator": creator.strip(),
                "teardown_policy": teardown_policy,
                "state": "registered",
            }
        )
        updated = _updated_record(
            record,
            "DISPOSABLE_REGISTERED",
            disposable_resources=tuple(resources),
        )
        if proof is not None:
            updated = replace(
                updated,
                history=(
                    *record.history,
                    {
                        **updated.history[-1],
                        "published_handoff_resource_registration": proof,
                    },
                ),
            )
        # A waiting release is usually waiting on exactly this classification, so the
        # intent is carried to the new record version rather than cancelled. Its
        # publication evidence is what earned the release; only the plan is stale, and
        # every pass rebuilds the plan under the current resource contract.
        for intent in _release_intents_for_allocation(snapshot, record.allocation_id):
            if intent.status not in {"PENDING", "BLOCKED_RETRYABLE"}:
                continue
            store.replace_release_intent(
                _updated_release_intent(
                    intent,
                    "RESOURCE_CONTRACT_CHANGED",
                    status="PENDING",
                    blocker=None,
                    expected_record_version=updated.record_version,
                )
            )
        store.replace_record(updated)
    payload = status(repository_path)
    payload["command"] = "resource-register"
    payload["decision"] = {
        "code": "DISPOSABLE_REGISTERED",
        "reason": "rebuildable task-local resource registered",
        "allocation_id": updated.allocation_id,
        "record_version": updated.record_version,
        "resource_id": resource_id,
        "path": relative,
        "reproducible": reproducible,
        "creator": creator.strip(),
        "teardown_policy": teardown_policy,
    }
    return payload


def cleanup_disposable(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner: str,
    resource_path: str,
) -> dict[str, Any]:
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        _cas(record, expected_record_version)
        if record.owner != owner:
            raise ControllerError("OWNER_MISMATCH", "resource owner does not match")
        matches = [
            resource
            for resource in record.disposable_resources
            if resource["path"] == resource_path and resource["state"] == "registered"
        ]
        if len(matches) != 1:
            raise ControllerError("RESOURCE_NOT_REGISTERED", "resource is not registered")
        resource = matches[0]
        relative = _resource_relative_path(resource["path"], resource["kind"])
        # The registered resource is a directory. Preserve directory-only ignore
        # semantics when retrying after removal but before the metadata commit.
        if tracked_paths(item.path, relative) or not path_is_ignored(item.path, relative + "/"):
            raise ControllerError("RESOURCE_DRIFT", "resource tracking or ignore state changed")
        absolute = item.path / relative
        if absolute.is_symlink() or absolute.is_mount():
            raise ControllerError("RESOURCE_IDENTITY_MISMATCH", "resource is a symlink or mount")
        if absolute.exists():
            marker = absolute / "pyvenv.cfg"
            if (
                not absolute.is_dir()
                or not marker.is_file()
                or marker.is_symlink()
                or (absolute / ".git").exists()
            ):
                raise ControllerError(
                    "RESOURCE_IDENTITY_MISMATCH", "resource is not a recognizable Python venv"
                )
            shutil.rmtree(absolute)
            if absolute.exists():
                raise ControllerError("RESOURCE_CLEANUP_FAILED", "resource still exists")
        resources = [dict(candidate) for candidate in record.disposable_resources]
        for candidate in resources:
            if candidate["resource_id"] == resource["resource_id"]:
                candidate["state"] = "cleaned"
        _cancel_release_intents(
            store,
            snapshot,
            record.allocation_id,
            event="RESOURCE_CONTRACT_CHANGED",
            blocker="RESOURCE_IDENTITY_CHANGED",
        )
        updated = _updated_record(
            record,
            "DISPOSABLE_CLEANED",
            disposable_resources=tuple(resources),
        )
        store.replace_record(updated)
    payload = status(repository_path)
    payload["command"] = "resource-clean"
    payload["decision"] = {
        "code": "DISPOSABLE_CLEANED",
        "reason": "registered rebuildable resource removed",
        "allocation_id": updated.allocation_id,
        "record_version": updated.record_version,
        "resource_id": resource["resource_id"],
        "path": relative,
    }
    return payload


def _typed_runtime_evidence(
    item: WorktreeGitState, record: MetadataRecord, snapshot: MetadataSnapshot
) -> dict[str, Any]:
    lease = snapshot.writer_leases.get(record.allocation_id)
    from .writer_lease import runtime_binding

    holders = tuple(declared[0] for value in record.external_bindings if (declared := runtime_binding(value)) is not None)
    if lease:
        holders = (*holders, lease.holder)
    evidence = runtime_authority_evidence(item.path, holders=holders)
    evidence["allocation_id"] = record.allocation_id
    # Hosts of task executions register these opaque identities before use.
    # Their absence is an authority-boundary fact, not proof about every host PID.
    evidence["managed_references"].extend(
        {"classification": "MANAGED_REFERENCE", "kind": "external_binding", "identity": binding}
        for binding in record.external_bindings
    )
    if lease:
        evidence["managed_references"].append(
            {
                "classification": "MANAGED_REFERENCE",
                "kind": "writer_lease",
                "identity": lease.lease_id,
                "holder": asdict(lease.holder),
            }
        )
    publication = snapshot.publication_lease
    if publication and publication.allocation_id == record.allocation_id:
        evidence["managed_references"].append(
            {
                "classification": "MANAGED_REFERENCE",
                "kind": "publication_lease",
                "identity": publication.lease_id,
                "run_id": publication.run_id,
            }
        )
    return evidence


def _typed_runtime_blockers(evidence: dict[str, Any]) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    if evidence["managed_references"]:
        blockers.append(
            {
                "code": "MANAGED_REFERENCE",
                "message": "allocation has registered or positively associated managed runtime",
            }
        )
    if evidence["observed_references"]:
        reference = evidence["observed_references"][0]
        blockers.append(
            {
                "code": "RUNTIME_REFERENCE",
                "message": f"observed {reference['kind']} reference from pid {reference['pid']} blocks cleanup/release",
            }
        )
    uncertain = evidence["managed_visibility_uncertainties"] + evidence["scan_uncertainties"]
    if uncertain:
        blockers.append(
            {
                "code": "RUNTIME_SCAN_UNCERTAIN",
                "message": "managed visibility or scan identity is uncertain: "
                + json.dumps(uncertain, sort_keys=True),
            }
        )
    return blockers


MULTIPLE_PRESERVED_CLEANUP_CONTRACT = "legacy-preserved-state-multiple-cleanup/v1"


def _preservation_inputs(
    receipt: Path | None,
    receipt_sha256: str | None,
    additional_receipts: tuple[Path, ...],
    additional_sha256: tuple[str, ...],
) -> tuple[tuple[Path, str], ...]:
    if (
        receipt is None
        and receipt_sha256 is None
        and not additional_receipts
        and not additional_sha256
    ):
        return ()
    if (
        receipt is None
        or receipt_sha256 is None
        or len(additional_receipts) != len(additional_sha256)
    ):
        raise ControllerError(
            "PRESERVATION_COVERAGE_INVALID",
            "primary receipt and SHA-256 plus paired additional receipts are required",
        )
    inputs = ((receipt, receipt_sha256), *zip(additional_receipts, additional_sha256, strict=True))
    if len({str(path.resolve()) for path, _ in inputs}) != len(inputs) or len(
        {digest for _, digest in inputs}
    ) != len(inputs):
        raise ControllerError("PRESERVATION_COVERAGE_INVALID", "duplicate preservation receipt")
    return inputs


def _preservation_source_relative(plan: dict[str, Any], target: Path) -> str:
    relative = Path(plan["source"]).relative_to(target).as_posix()
    if (
        relative in {"", "."}
        or ".." in PurePosixPath(relative).parts
        or plan.get("source_relative", relative) != relative
    ):
        raise ControllerError("PRESERVATION_COVERAGE_INVALID", "preserved source path differs")
    return relative


def _typed_cleanup_plan_locked(
    repository: RepositoryInventory,
    snapshot: MetadataSnapshot,
    store: MetadataStore,
    item: WorktreeGitState,
    record: MetadataRecord,
    *,
    owner: str,
    expected_record_version: int,
    check_transaction: bool = True,
    preservation_receipt: Path | None = None,
    preservation_receipt_sha256: str | None = None,
    additional_preservation_receipts: tuple[Path, ...] = (),
    additional_preservation_receipt_sha256: tuple[str, ...] = (),
    preservation_source_removed: bool = False,
    preservation_sources_removed: tuple[str, ...] = (),
    completed_action_evidence: tuple[dict[str, Any], ...] = (),
    cleanup_transaction_id: str | None = None,
    source_topology: dict[str, Any] | None = None,
    recovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from . import hardlinks

    original_completed_sources: set[str] = set()
    if recovery is not None:
        from .cleanup_recovery import original

        old, _ = original(store, recovery, target=item.path, allocation_id=record.allocation_id)
        if "preservation_evidences" in old["plan"]:
            from . import multiple_cleanup_recovery as multi_recovery

            if recovery.get("contract") != multi_recovery.CONTRACT:
                raise ControllerError(
                    "RE_PRESERVATION_RECOVERY_BLOCKED",
                    "multiple-receipt cleanup requires its exact recovery contract",
                )
            _, _, original_completed_sources, original_topology = multi_recovery.details(
                store, recovery
            )
            if source_topology is None:
                source_topology = original_topology
    resources = classify_resources(item.path)
    blockers: list[dict[str, str]] = []
    aborted_integration = record.role == "integration" and record.lifecycle == "INTEGRATION_ABORTED"
    integration_abort_evidence = None
    terminal_evidence = None
    preserved_evidence = None
    preserved_evidences: list[dict[str, Any]] = []
    multiple = bool(additional_preservation_receipts or additional_preservation_receipt_sha256)
    try:
        completed_sources: set[str] = set()
        if cleanup_transaction_id is not None:
            from .preserved_cleanup import completed_actions

            if str(uuid.UUID(cleanup_transaction_id)) != cleanup_transaction_id:
                raise ControllerError("PRESERVATION_COVERAGE_INVALID", "canonical cleanup transaction identity required")
            journal = store._read_json(store.transactions_dir / f"{cleanup_transaction_id}.json")
            cleanup_plan = journal["cleanup_plan"]
            bound_action = {
                "repository_id": snapshot.repository_id,
                "allocation_id": record.allocation_id,
                "target_path": str(item.path),
                "expected_head": item.head,
                "expected_record_version": record.record_version,
                "owner": owner,
                "authority_key": record.authority_key,
            }
            if (
                journal.get("transaction_id") != cleanup_transaction_id
                or journal.get("command") != "resource-clean"
                or journal.get("phase") != "PREPARED" or journal.get("branch") != item.branch
                or any(journal.get(key) != bound_action[key] for key in (
                    "repository_id", "allocation_id", "target_path", "expected_head", "expected_record_version",
                ))
                or any(cleanup_plan.get(key) != value for key, value in bound_action.items())
                or cleanup_plan.get("plan_id") != _release_plan_id(
                    {key: value for key, value in cleanup_plan.items() if key != "plan_id"}
                )
                or (original_completed_sources and cleanup_plan["hardlink_topology"] != original_topology)
                or [row["path"] for row in journal["actions"]] != cleanup_plan["cleanup_paths"]
                or [row["path"] for row in journal["actions"] if row["status"] == "COMPLETED"]
                != cleanup_plan["cleanup_paths"][:len(completed_action_evidence)]
            ):
                raise ControllerError("PRESERVATION_COVERAGE_INVALID", "cleanup journal identity changed")
            completed_sources, preceding = completed_actions(
                store, cleanup_transaction_id, completed_action_evidence,
                initial_topology=cleanup_plan["hardlink_topology"],
                cleanup_paths=cleanup_plan["cleanup_paths"],
            )
            if source_topology is not None and preceding != source_topology:
                raise ControllerError("PRESERVATION_COVERAGE_INVALID", "cleanup baseline changed")
        elif completed_action_evidence or preservation_source_removed or preservation_sources_removed:
            raise ControllerError("PRESERVATION_COVERAGE_INVALID", "source absence is not completion evidence")
        if set(preservation_sources_removed) != completed_sources:
            raise ControllerError("PRESERVATION_COVERAGE_INVALID", "completed source list differs from proofs")
        completed_sources.update(original_completed_sources)
        inputs = _preservation_inputs(
            preservation_receipt,
            preservation_receipt_sha256,
            additional_preservation_receipts,
            additional_preservation_receipt_sha256,
        )
        if original_completed_sources:
            expected_inputs = {
                (entry["evidence"]["receipt_path"], entry["evidence"]["receipt_sha256"])
                for entry in old["plan"]["preservation_evidences"]
            }
            if {(str(path), digest) for path, digest in inputs} != expected_inputs:
                raise ControllerError(
                    "PRESERVATION_COVERAGE_INVALID", "recovery requires the entire original proof set"
                )
        bound = {
            "repository_id": snapshot.repository_id,
            "allocation_id": record.allocation_id,
            "target_path": str(item.path),
            "expected_head": item.head,
            "expected_record_version": record.record_version,
            "owner": owner,
            "authority_key": record.authority_key,
        }
        preservation_plans = []
        for receipt_path, receipt_sha in inputs:
            evidence, preservation_plan = coverage(
                receipt_path, receipt_sha, identity=bound, verify_source=False
            )
            source_rel = _preservation_source_relative(preservation_plan, item.path)
            preservation_plans.append(preservation_plan)
            if any(
                source_rel == row["source_relative"]
                or PurePosixPath(source_rel) in PurePosixPath(row["source_relative"]).parents
                or PurePosixPath(row["source_relative"]) in PurePosixPath(source_rel).parents
                for row in preserved_evidences
            ):
                raise ControllerError(
                    "PRESERVATION_COVERAGE_INVALID", "preserved source paths overlap"
                )
            removed = source_rel in completed_sources
            if removed:
                from .preserved_cleanup import source_absent

                if not source_absent(preservation_plan, item.path):
                    raise ControllerError(
                        "PRESERVATION_COVERAGE_INVALID", "completed legacy source reappeared"
                    )
            else:
                evidence, _ = coverage(
                    receipt_path,
                    receipt_sha,
                    identity=bound,
                    source_topology=source_topology,
                )
                legacy = next((row for row in resources if row["path"] == source_rel), None)
                if legacy is None:
                    raise ControllerError(
                        "PRESERVATION_COVERAGE_INVALID", "legacy source is missing"
                    )
                legacy.update(
                    resource_type="legacy-preserved-state",
                    classification="exact-preserved",
                    cleanup_action="remove-preserved-tree",
                    blocker=None,
                    evidence=evidence,
                )
                legacy.pop("reason", None)
            preserved_evidences.append({"source_relative": source_rel, "evidence": evidence})
        if preservation_plans:
            from .preserved_cleanup import common_source_root

            common_source_root(preservation_plans, item.path)
        preserved_evidences.sort(key=lambda row: row["source_relative"])
        if preserved_evidences:
            preserved_evidence = preserved_evidences[0]["evidence"]
    except (ControllerError, OSError, ValueError, TypeError, KeyError) as exc:
        blockers.append(
            {
                "code": getattr(exc, "code", "PRESERVATION_COVERAGE_INVALID"),
                "message": "exact preservation coverage verification failed",
                "path": source_rel if "source_rel" in locals() else ".venv",
            }
        )
    runtime_evidence: dict[str, Any] = {"contract": RUNTIME_AUTHORITY_CONTRACT}
    try:
        _cas(record, expected_record_version)
        from .source_cleanup import verify_completed

        archived_no_commit = record.lifecycle in {"NO_CHANGE_COMPLETED", "PRESERVED"} and bool(
            verify_completed(store, item, record)
        )
        retained_cleanup = record.lifecycle == "RETAINED" and record.role in {"feature", "audit"}
        if aborted_integration:
            from .integration_cleanup import abort_evidence

            integration_abort_evidence = abort_evidence(store, snapshot, item, record)
        elif ((record.lifecycle == "PUBLISHED" and record.role == "integration")
              or (record.lifecycle == "NO_CHANGE_COMPLETED" and not archived_no_commit
                  and record.history)):
            from .delivered_closeout import terminal_cleanup_evidence

            terminal_evidence = terminal_cleanup_evidence(store, snapshot, item, record)
        elif (record.lifecycle != "HANDOFF_READY" and not archived_no_commit and not retained_cleanup) or record.role not in {
            "feature",
            "audit",
            "hotfix",
            "emergency",
        }:
            raise ControllerError(
                "INVALID_LIFECYCLE",
                "typed cleanup requires HANDOFF_READY, RETAINED feature/audit or exact archived-source completion",
            )
        _validate_release_core(
            repository,
            snapshot,
            item,
            record,
            store=store,
            owner_release=owner,
            strict_identity=True,
            allow_registered_venv=False,
            cleanup_recovery=recovery,
            allow_retained_cleanup=retained_cleanup,
            cleanup_transaction_id=cleanup_transaction_id,
            allow_pending_typed_no_action=not check_transaction and cleanup_transaction_id is None and recovery is None,
        )
        if record.allocation_id in snapshot.writer_leases:
            raise ControllerError(
                "WRITER_LEASE_PRESENT", "writer lease must be cleared before cleanup"
            )
        if (
            snapshot.publication_lease
            and snapshot.publication_lease.allocation_id == record.allocation_id
        ):
            raise ControllerError("PUBLICATION_LEASE_PRESENT", "publication lease blocks cleanup")
        if (
            not item.branch
            or resolve_commit(repository.canonical_path, f"refs/heads/{item.branch}") != item.head
        ):
            raise ControllerError("MISSING_RETAINED_REF", "exact retained branch is required")
        if (
            check_transaction
            and recovery is None
            and _release_transaction(
                store, target_path=str(item.path), allocation_id=record.allocation_id
            )
            is not None
        ):
            raise ControllerError("INTERRUPTED_TRANSACTION", "pending release requires recovery")
    except ControllerError as exc:
        blockers.append({"code": exc.code, "message": str(exc)})
    try:
        runtime_evidence = _typed_runtime_evidence(item, record, snapshot)
        blockers.extend(_typed_runtime_blockers(runtime_evidence))
    except ControllerError as exc:
        runtime_evidence["scan_error"] = {"code": exc.code, "message": str(exc)}
        blockers.append({"code": exc.code, "message": str(exc)})
    for resource in resources:
        if (aborted_integration or terminal_evidence is not None) and resource["cleanup_action"] == "remove-tree":
            # This additional lifecycle has no project-name/path allowlist. Even
            # otherwise recognized build output requires an exact preserved copy.
            resource.update(
                classification="blocked", cleanup_action="none",
                blocker="INTEGRATION_PRESERVATION_REQUIRED" if aborted_integration else "TERMINAL_PRESERVATION_REQUIRED",
                reason="terminal integration/no-change cleanup requires exact preservation for every tree",
            )
        if resource["blocker"]:
            blockers.append(
                {
                    "code": resource["blocker"],
                    "message": resource["reason"],
                    "path": resource["path"],
                }
            )
    if not blockers:
        try:
            from .preservation import validate_editable_references

            validate_editable_references(repository, item.path)
        except ControllerError as exc:
            blockers.append({"code": exc.code, "message": str(exc)})
    topology = None
    if not blockers:
        try:
            paths = [row["path"] for row in resources if row["cleanup_action"] == "remove-tree"] + [
                row["path"] for row in resources if row["cleanup_action"] == "remove-preserved-tree"
            ]
            topology = hardlinks.inventory(item.path, paths)
        except (ControllerError, OSError) as exc:
            blockers.append(
                {
                    "code": getattr(exc, "code", "HARDLINK_TOPOLOGY_BLOCKED"),
                    "message": "complete stable in-scope hardlink topology required",
                }
            )
    if blockers:
        for resource in resources:
            if resource["classification"] in {"disposable", "exact-preserved"}:
                resource.update(cleanup_action="none", blocker=blockers[0]["code"])
    identity = {
        "contract": MULTIPLE_PRESERVED_CLEANUP_CONTRACT
        if multiple
        else PRESERVED_CLEANUP_CONTRACT
        if preservation_receipt
        else TYPED_RESOURCE_CONTRACT,
        "repository_id": snapshot.repository_id,
        "allocation_id": record.allocation_id,
        "target_path": str(item.path),
        "expected_head": item.head,
        "expected_record_version": record.record_version,
        "owner": owner,
        "authority_key": record.authority_key,
        "resources": resources,
        "hardlink_topology": topology,
        "runtime_evidence": runtime_evidence,
        "warnings": [
            {
                "code": "HOST_VISIBILITY_GAP",
                "pids": [row["pid"] for row in runtime_evidence["host_visibility_gaps"]],
                "message": "unassociated host procfs permission gaps are outside managed-runtime authority; no claim of host-wide absence of references",
            }
        ]
        if runtime_evidence.get("host_visibility_gaps")
        else [],
        "blockers": blockers,
        "cleanup_paths": [
            row["path"] for row in resources if row["cleanup_action"] == "remove-tree"
        ]
        + [row["path"] for row in resources if row["cleanup_action"] == "remove-preserved-tree"],
    }
    if preservation_receipt is not None:
        identity["preservation_evidence"] = preserved_evidence
    if multiple:
        identity["preservation_evidences"] = preserved_evidences
    if terminal_evidence is not None:
        identity["terminal_evidence"] = terminal_evidence
    if integration_abort_evidence is not None:
        identity["integration_abort_evidence"] = integration_abort_evidence
    if recovery is not None:
        identity["recovery"] = recovery
    return {**identity, "plan_id": _release_plan_id(identity)}


def cleanup_typed_disposable(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner: str,
    dry_run: bool,
    expected_plan_id: str | None = None,
    preservation_receipt: Path | None = None,
    preservation_receipt_sha256: str | None = None,
    additional_preservation_receipts: tuple[Path, ...] = (),
    additional_preservation_receipt_sha256: tuple[str, ...] = (),
    recovery: dict[str, Any] | None = None,
    release_intent_id: str | None = None,
) -> dict[str, Any]:
    """Classify, explicitly clean, then re-scan. This operation never releases an allocation."""
    from . import cleanup_evidence, hardlinks

    store = MetadataStore(inventory(repository_path).common_dir)
    receipt_id = None
    with (store.lock(exclusive=False) if dry_run else store.cleanup_lock()):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        if release_intent_id is not None:
            if not load_policy(repository.canonical_path).automatic_feature_audit_release:
                raise ControllerError("AUTOMATIC_RELEASE_DISABLED", "automatic cleanup authority was disabled")
            intent = snapshot.release_intents.get(release_intent_id)
            if intent is None or intent.allocation_id != record.allocation_id:
                raise ControllerError("MISSING_RELEASE_INTENT", "automatic cleanup requires its exact intent")
            _release_intent_static_identity(snapshot, item, record, intent)
            evaluated = _intent_with_current_integration_evidence(repository, snapshot, record, intent)
            if evaluated.status not in {"PENDING", "BLOCKED_RETRYABLE"} or evaluated.blocker:
                raise ControllerError(evaluated.blocker or "RELEASE_INTENT_STALE", "automatic cleanup publication evidence is not current")
        cleanup_plan = _typed_cleanup_plan_locked(
            repository,
            snapshot,
            store,
            item,
            record,
            owner=owner,
            expected_record_version=expected_record_version,
            preservation_receipt=preservation_receipt,
            preservation_receipt_sha256=preservation_receipt_sha256,
            additional_preservation_receipts=additional_preservation_receipts,
            additional_preservation_receipt_sha256=additional_preservation_receipt_sha256,
            recovery=recovery,
        )
        code = (
            "RESOURCE_CLEANUP_BLOCKED"
            if cleanup_plan["blockers"]
            else "CLEANUP_READY_WITH_PRESERVATION"
            if preservation_receipt and ".venv" in cleanup_plan["cleanup_paths"]
            else "CLEANUP_READY_WITH_HOST_VISIBILITY_GAP"
            if cleanup_plan["warnings"] and cleanup_plan["cleanup_paths"]
            else "RESOURCE_CLEANUP_PLAN_READY"
        )
        if not dry_run and not cleanup_plan["blockers"]:
            if expected_plan_id != cleanup_plan["plan_id"]:
                raise ControllerError(
                    "RESOURCE_PLAN_MISMATCH", "cleanup requires the exact current dry-run plan id"
                )
            if not cleanup_plan["cleanup_paths"]:
                code = "RESOURCE_CLEANUP_NOT_NEEDED"
            else:
                transaction_id = str(uuid.uuid4())
                journal = _transaction(
                    command="resource-clean",
                    transaction_id=transaction_id,
                    repository_id=str(snapshot.repository_id),
                    target_path=item.path,
                    allocation_id=record.allocation_id,
                    branch=item.branch,
                    expected_head=item.head,
                    expected_record_version=record.record_version,
                    phase="PREPARED",
                    created_at=now_iso(),
                )
                journal["cleanup_plan"] = cleanup_plan
                actions = [
                    {"path": path, "status": "PENDING"} for path in cleanup_plan["cleanup_paths"]
                ]
                journal["actions"] = actions
                # A failed or interrupted cleanup retains this journal. Even the older
                # release implementation refuses any outstanding non-release transaction.
                store.write_transaction(transaction_id, journal)
                runtime_checks: list[dict[str, Any]] = []
                topology = cleanup_plan["hardlink_topology"]
                action_evidence: list[dict[str, Any]] = []
                evidence_directory = store.cleanup_actions_dir / transaction_id
                receipt_written = False
                try:
                    for step, action in enumerate(actions):
                        row = next(
                            row
                            for row in cleanup_plan["resources"]
                            if row["path"] == action["path"]
                        )
                        current_repository = inventory(repository_path)
                        current_snapshot = store._load_unlocked()
                        current_item, current_record = _managed_record(
                            current_repository, current_snapshot, target
                        )
                        if current_item.head != cleanup_plan["expected_head"]:
                            raise ControllerError("HEAD_MISMATCH", "HEAD changed before cleanup")
                        current_plan = _typed_cleanup_plan_locked(
                            current_repository,
                            current_snapshot,
                            store,
                            current_item,
                            current_record,
                            owner=owner,
                            expected_record_version=expected_record_version,
                            check_transaction=False,
                            preservation_receipt=preservation_receipt,
                            preservation_receipt_sha256=preservation_receipt_sha256,
                            additional_preservation_receipts=additional_preservation_receipts,
                            additional_preservation_receipt_sha256=additional_preservation_receipt_sha256,
                            preservation_sources_removed=tuple(
                                a["path"] for a in actions if a["status"] == "COMPLETED"
                            ),
                            source_topology=topology,
                            completed_action_evidence=tuple(action_evidence),
                            cleanup_transaction_id=transaction_id,
                            recovery=recovery,
                        )
                        if current_plan["blockers"]:
                            blocker = current_plan["blockers"][0]
                            raise ControllerError(
                                blocker["code"],
                                blocker["message"],
                            )
                        if current_plan["hardlink_topology"] != topology:
                            raise ControllerError(
                                "HARDLINK_TOPOLOGY_BLOCKED",
                                "topology differs from the recorded preceding action",
                            )
                        runtime_checks.append(
                            {
                                "resource_path": row["path"],
                                "runtime_evidence": current_plan["runtime_evidence"],
                            }
                        )
                        fresh = next(
                            (
                                resource
                                for resource in current_plan["resources"]
                                if resource["path"] == row["path"]
                            ),
                            None,
                        )
                        if fresh is None or fresh.get("evidence") != row["evidence"]:
                            raise ControllerError(
                                "RESOURCE_PLAN_MISMATCH",
                                "resource identity changed before deletion",
                            )
                        if (
                            row["cleanup_action"] == "remove-tree"
                            and resource_evidence(item.path, row["path"]) != row["evidence"]
                        ):
                            raise ControllerError(
                                "RESOURCE_PLAN_MISMATCH",
                                "typed resource changed before deletion",
                            )
                        # Refuse a changed topology before the action boundary, so the
                        # refusal leaves no action evidence and stays a provable no-op.
                        hardlinks.verify(item.path, topology)
                        action["status"] = "STARTED"
                        store.write_transaction(transaction_id, journal)
                        before_receipt = cleanup_evidence.before(
                            evidence_directory, step, row["path"], topology
                        )
                        if row["cleanup_action"] == "remove-preserved-tree":
                            external_survivors = delete_preserved_state(
                                item.path, {**row["evidence"], "source_topology": topology}
                            )
                        else:
                            external_survivors = delete_typed_resource(
                                item.path,
                                row["path"],
                                {
                                    **row["evidence"],
                                    "hardlink_identities": hardlinks.identities(
                                        topology, row["path"]
                                    ),
                                    "external_hardlink_contents": hardlinks.external_contents(
                                        topology, row["path"]
                                    ),
                                },
                            )
                        if os.path.lexists(item.path / row["path"]):
                            raise ControllerError(
                                "RESOURCE_CLEANUP_FAILED", "resource remains after cleanup"
                            )
                        after_topology = hardlinks.inventory(
                            item.path, [r for r in topology["resources"] if r != row["path"]]
                        )
                        after_receipt = cleanup_evidence.after(
                            evidence_directory,
                            step,
                            row["path"],
                            topology,
                            after_topology,
                            before_receipt,
                            external_survivors,
                        )
                        action_evidence.append(
                            {
                                "resource": row["path"],
                                "before": before_receipt,
                                "after": after_receipt,
                            }
                        )
                        topology = after_topology
                        action["status"] = "COMPLETED"
                        store.write_transaction(transaction_id, journal)
                    current_repository = inventory(repository_path)
                    current_snapshot = store._load_unlocked()
                    current_item, current_record = _managed_record(
                        current_repository, current_snapshot, target
                    )
                    if current_item.head != cleanup_plan["expected_head"]:
                        raise ControllerError("HEAD_MISMATCH", "HEAD changed during cleanup")
                    rescanned = _typed_cleanup_plan_locked(
                        current_repository,
                        current_snapshot,
                        store,
                        current_item,
                        current_record,
                        owner=owner,
                        expected_record_version=expected_record_version,
                        check_transaction=False,
                        preservation_receipt=preservation_receipt,
                        preservation_receipt_sha256=preservation_receipt_sha256,
                        additional_preservation_receipts=additional_preservation_receipts,
                        additional_preservation_receipt_sha256=additional_preservation_receipt_sha256,
                        preservation_sources_removed=tuple(
                            a["path"] for a in actions if a["status"] == "COMPLETED"
                        ),
                        source_topology=topology,
                        completed_action_evidence=tuple(action_evidence),
                        cleanup_transaction_id=transaction_id,
                        recovery=recovery,
                    )
                    if rescanned["blockers"] or rescanned["cleanup_paths"]:
                        raise ControllerError(
                            "RESOURCE_CLEANUP_FAILED",
                            "post-cleanup scan is not clear; journal retained",
                        )
                    if recovery is None and current_record.lifecycle != "RETAINED":
                        _build_release_plan_locked(
                            current_repository,
                            current_snapshot,
                            current_item,
                            current_record,
                            store=store,
                            expected_record_version=expected_record_version,
                            owner_release=owner,
                            publication_receipt=None,
                            release_intent=None,
                            cleanup_transaction_id=transaction_id,
                        )
                    receipt_id = transaction_id
                    store.write_completion_receipt(
                        receipt_id,
                        {
                            "schema_version": 1,
                            "receipt_id": receipt_id,
                            "contract": cleanup_plan["contract"],
                            **({"release_intent_id": release_intent_id} if release_intent_id else {}),
                            "result": "CLEANED",
                            "plan": cleanup_plan,
                            "post_scan": rescanned,
                            "runtime_checks": runtime_checks,
                            "actions": actions,
                            "action_evidence": action_evidence,
                            "created_at": now_iso(),
                        },
                    )
                    receipt_written = True
                    store.remove_transaction(transaction_id)
                    code = (
                        "PRESERVED_STATE_CLEANED"
                        if preservation_receipt
                        else "TYPED_DISPOSABLE_CLEANED"
                    )
                except (ControllerError, OSError) as exc:
                    for action in actions:
                        if action["status"] == "STARTED":
                            action["status"] = "FAILED_OR_PARTIAL"
                    journal["failure"] = {
                        "code": getattr(exc, "code", "FILESYSTEM_ERROR"),
                        "errno": getattr(exc, "errno", None),
                    }
                    store.write_transaction(transaction_id, journal)
                    if not receipt_written:
                        try:
                            failed_repository = inventory(repository_path)
                            failed_snapshot = store._load_unlocked()
                            failed_item, failed_record = _managed_record(
                                failed_repository, failed_snapshot, target
                            )
                            failed_scan = _typed_cleanup_plan_locked(
                                failed_repository,
                                failed_snapshot,
                                store,
                                failed_item,
                                failed_record,
                                owner=owner,
                                expected_record_version=expected_record_version,
                                check_transaction=False,
                                preservation_receipt=preservation_receipt,
                                preservation_receipt_sha256=preservation_receipt_sha256,
                                additional_preservation_receipts=additional_preservation_receipts,
                                additional_preservation_receipt_sha256=additional_preservation_receipt_sha256,
                                preservation_sources_removed=tuple(
                                    a["path"] for a in actions if a["status"] == "COMPLETED"
                                ),
                                source_topology=topology,
                                recovery=recovery,
                            )
                        except (ControllerError, OSError) as scan_exc:
                            failed_scan = {
                                "blockers": [
                                    {"code": getattr(scan_exc, "code", "FILESYSTEM_ERROR")}
                                ]
                            }
                        store.write_completion_receipt(
                            transaction_id,
                            {
                                "schema_version": 1,
                                "receipt_id": transaction_id,
                                "contract": cleanup_plan["contract"],
                                "result": "FAILED_OR_PARTIAL",
                                "plan": cleanup_plan,
                                "actions": actions,
                                "action_evidence": action_evidence,
                                "failure": journal["failure"],
                                "post_scan": failed_scan,
                                "runtime_checks": runtime_checks,
                                "created_at": now_iso(),
                            },
                        )
                    raise ControllerError(
                        getattr(exc, "code", "RESOURCE_CLEANUP_FAILED"),
                        f"cleanup failed; transaction and immutable receipt {transaction_id} retained for recovery",
                    ) from exc
    payload = status(repository_path)
    payload["command"] = "resource-clean"
    payload["decision"] = {
        "code": code,
        "plan": cleanup_plan,
        "dry_run": dry_run,
        "receipt_id": receipt_id,
        "allocation_released": False,
    }
    return payload


def _validate_prepared_publication_receipt(
    snapshot: MetadataSnapshot,
    intent: IntegrationIntent | None,
    record: MetadataRecord,
    item: WorktreeGitState,
    *,
    required: bool,
) -> None:
    if intent is not None and intent.prepared_receipt_id is not None:
        prepared = snapshot.prepared_candidate_receipts.get(intent.prepared_receipt_id)
        if (
            prepared is None
            or prepared.repository_id != snapshot.repository_id
            or prepared.intent_id != intent.intent_id
            or prepared.allocation_id != record.allocation_id
            or prepared.attempt != intent.attempt
            or prepared.base_origin_main_oid != intent.prepared_base_origin_main_oid
            or prepared.base_origin_main_oid != intent.base_main_oid
            or prepared.candidate_oid != item.head
            or prepared.candidate_oid != intent.prepared_commit_oid
            or prepared.tree_oid != commit_tree_oid(item.path, item.head)
            or prepared.tree_oid != intent.prepared_tree_oid
            or prepared.scope_digest != intent.scope_digest
            or prepared.scope_digest != intent.prepared_scope_digest
            or prepared.prepare_identity != intent.prepare_identity
            or prepared.validation_evidence_id != record.finalizer_record_id
            or prepared.tests_receipt_id != intent.tests_receipt_id
            or prepared.snapshot_receipt_id != intent.snapshot_receipt_id
        ):
            raise ControllerError("PREPARED_RECEIPT_DRIFT", "prepared candidate receipt identity differs")
    elif required:
        raise ControllerError("MISSING_PREPARED_RECEIPT", "policy schema v4 requires a prepared receipt")


def acquire_publication_lease(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner: str,
    run_id: str | None = None,
    ttl_seconds: int = DEFAULT_PUBLICATION_LEASE_SECONDS,
) -> dict[str, Any]:
    if ttl_seconds < 1 or ttl_seconds > MAX_PUBLICATION_LEASE_SECONDS:
        raise ControllerError(
            "INVALID_LEASE_TTL",
            f"lease ttl must be between 1 and {MAX_PUBLICATION_LEASE_SECONDS} seconds",
        )
    run_identity = (run_id or str(uuid.uuid4())).strip()
    if not run_identity or "\x00" in run_identity or len(run_identity) > 256:
        raise ControllerError("INVALID_RUN_ID", "publication run identity is invalid")
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    stale_intent: IntegrationIntent | None = None
    lease: PublicationLease | None = None
    updated: MetadataRecord | None = None
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        policy = load_policy(repository.canonical_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        _cas(record, expected_record_version)
        existing_lease = snapshot.publication_lease
        if existing_lease is not None:
            expires = datetime.fromisoformat(existing_lease.expires_at)
            if expires > datetime.fromisoformat(now_iso()):
                raise ControllerError(
                    "PUBLICATION_LEASE_HELD", "main publication lease is occupied"
                )
            observed_remote = fetch_remote_branch(
                repository.canonical_path,
                existing_lease.remote,
                existing_lease.branch,
            )
            if observed_remote == existing_lease.candidate_oid:
                raise ControllerError(
                    "PUBLICATION_RECOVERY_REQUIRED",
                    "expired lease candidate is already remote; recover its receipt before publishing",
                )
            if observed_remote != existing_lease.expected_main_oid:
                raise ControllerError(
                    "PUBLICATION_REMOTE_DRIFT",
                    "expired lease remote no longer matches its expected main OID",
                )
            old_record = snapshot.records.get(existing_lease.allocation_id)
            if old_record is None or old_record.lifecycle != "INTEGRATING":
                raise ControllerError(
                    "PUBLICATION_LEASE_DRIFT", "expired lease has no integrating candidate"
                )
            restored = _updated_record(
                old_record, "PUBLICATION_LEASE_EXPIRED", lifecycle="HANDOFF_READY"
            )
            store.replace_record(restored)
            old_intent = _intent_for_allocation(snapshot, old_record.allocation_id)
            if old_intent is not None:
                restored_intent = _updated_intent(
                    old_intent,
                    "PUBLICATION_LEASE_EXPIRED",
                    state="READY_TO_PUBLISH",
                    publication_identity=None,
                )
                store.replace_integration_intent(restored_intent)
            store.remove_publication_lease(existing_lease.lease_id)
            if restored.allocation_id == record.allocation_id:
                record = restored
            snapshot = store._load_unlocked()
        if record.owner != owner:
            raise ControllerError("OWNER_MISMATCH", "publication lease owner does not match")
        if record.role != "integration" or record.lifecycle != "HANDOFF_READY":
            raise ControllerError(
                "INVALID_LIFECYCLE", "only a prepared integration candidate can publish"
            )
        if item.detached or not item.clean:
            raise ControllerError(
                "DIRTY_WORKTREE", "publication candidate must be clean and attached"
            )
        if not record.finalizer_record_id:
            raise ControllerError(
                "MISSING_FINALIZER_RECORD", "publication requires Finalizer evidence"
            )
        intent = _intent_for_allocation(snapshot, record.allocation_id)
        if intent is None and policy.policy_schema_version >= 4:
            raise ControllerError(
                "MISSING_INTENT", "policy schema v4 requires an integration intent"
            )
        if intent is not None and (
            intent.state != "READY_TO_PUBLISH"
            or intent.validated_oid != item.head
            or intent.validation_evidence_id != record.finalizer_record_id
        ):
            raise ControllerError(
                "VALIDATION_EVIDENCE_MISMATCH",
                "publication requires evidence bound to the exact prepared candidate",
            )
        _validate_prepared_publication_receipt(
            snapshot, intent, record, item,
            required=intent is not None and policy.policy_schema_version >= 4,
        )
        remote_head = fetch_remote_branch(
            repository.canonical_path,
            policy.publication_remote,
            policy.publication_branch,
        )
        repository_after_fetch = inventory(repository_path)
        fetched_item, fetched_record = _managed_record(repository_after_fetch, snapshot, target)
        if (
            fetched_item.key != item.key
            or fetched_item.head != item.head
            or not fetched_item.clean
            or fetched_record.allocation_id != record.allocation_id
        ):
            raise ControllerError("BLOCK_DRIFT", "candidate changed during publication preflight")
        prepared_base = (
            intent.prepared_base_origin_main_oid
            if intent is not None and intent.prepared_base_origin_main_oid is not None
            else intent.base_main_oid
            if intent is not None
            else record.base_commit
        )
        repository_id = snapshot.repository_id
        if repository_id is None:
            raise ControllerError("BLOCK_DRIFT", "repository metadata is missing")
        if prepared_base is not None and remote_head != prepared_base:
            updated = _updated_record(
                record,
                "PUBLICATION_BASE_STALE",
                lifecycle="ACTIVE",
                handoff_commit=None,
                finalizer_record_id=None,
                finalizer_commit=None,
            )
            if intent is None:
                raise ControllerError(
                    "STALE_PUBLICATION_BASE", "legacy candidate main base changed"
                )
            stale_intent = _updated_intent(
                intent,
                "PUBLICATION_BASE_STALE",
                state="NEEDS_REPREPARE",
                prepare_identity=None,
                publication_identity=None,
                prepared_receipt_id=None,
                prepared_base_origin_main_oid=None,
                prepared_commit_oid=None,
                prepared_tree_oid=None,
                prepared_scope_digest=None,
                tests_receipt_id=None,
                snapshot_receipt_id=None,
                validation_evidence_id=None,
                validated_oid=None,
                validated_at=None,
                blocked_reason=f"origin/main advanced to {remote_head}",
            )
            store.replace_record(updated)
            store.replace_integration_intent(stale_intent)
            _emit_lifecycle_event(store, repository_id, stale_intent, "NEEDS_REPREPARE")
        else:
            if not is_ancestor(fetched_item.path, remote_head, fetched_item.head):
                raise ControllerError(
                    "STALE_PUBLICATION_BASE",
                    "candidate does not contain the freshly fetched publication branch",
                )
            issues = integration_candidate_issues(
                fetched_item, record, snapshot, base_commit=remote_head
            )
            if issues:
                issue = issues[0]
                raise ControllerError(issue["code"], issue["message"])
            if remote_head == fetched_item.head:
                raise ControllerError("NO_PUBLICATION_DELTA", "candidate has no commit to publish")
            acquired_at = now_iso()
            expires_at = (
                datetime.fromisoformat(acquired_at) + timedelta(seconds=ttl_seconds)
            ).isoformat(timespec="seconds")
            lease = PublicationLease(
                schema_version=PUBLICATION_LEASE_SCHEMA_VERSION,
                lease_id=str(uuid.uuid4()),
                repository_id=repository_id,
                allocation_id=record.allocation_id,
                holder=owner,
                run_id=run_identity,
                remote=policy.publication_remote,
                branch=policy.publication_branch,
                target_ref=f"refs/heads/{policy.publication_branch}",
                candidate_oid=fetched_item.head,
                expected_main_oid=remote_head,
                validation_evidence_id=record.finalizer_record_id,
                acquired_at=acquired_at,
                expires_at=expires_at,
            )
            updated = _updated_record(
                record,
                "PUBLICATION_TRANSITION_STARTED",
                lifecycle="INTEGRATING",
                base_commit=remote_head,
            )
            store.replace_record(updated)
            store.write_publication_lease(lease)
            if intent is not None:
                publishing = _updated_intent(
                    intent,
                    "PUBLICATION_LEASE_GRANTED",
                    state="PUBLISHING",
                    publication_identity=lease.lease_id,
                )
                store.replace_integration_intent(publishing)
                _emit_lifecycle_event(store, repository_id, publishing, "PUBLICATION_LEASE_GRANTED")
    payload = status(repository_path)
    payload["command"] = "lease-acquire"
    if stale_intent is not None:
        payload["decision"] = {
            "code": "NEEDS_REPREPARE",
            "reason": "origin/main changed after preparation; old eligibility was invalidated",
            "record_version": updated.record_version if updated is not None else None,
            **_intent_payload(stale_intent),
        }
        return payload
    if lease is None or updated is None:
        raise ControllerError("BLOCK_DRIFT", "publication lease transition did not converge")
    payload["decision"] = {
        "code": "PUBLICATION_LEASE_ACQUIRED",
        "reason": "prepared base CAS passed; publication authority serialized",
        "allocation_id": updated.allocation_id,
        "record_version": updated.record_version,
        "lease_id": lease.lease_id,
        "holder": lease.holder,
        "run_id": lease.run_id,
        "target_ref": lease.target_ref,
        "candidate_head": lease.candidate_oid,
        "candidate_oid": lease.candidate_oid,
        "remote_head": lease.expected_main_oid,
        "expected_main_oid": lease.expected_main_oid,
        "expires_at": lease.expires_at,
        "lease_file": str(store.publication_lease_file),
    }
    return payload


def _pending_publication_release_version(
    store: MetadataStore,
    *,
    target: Path,
    allocation_id: str,
    publication_receipt_id: str,
) -> int | None:
    target_path = str(target.expanduser().resolve(strict=False))
    with store.lock(exclusive=False):
        snapshot = store._load_unlocked()
        transaction = _release_transaction(
            store, target_path=target_path, allocation_id=allocation_id
        )
        if transaction is None:
            return None
        plan_id = transaction.get("release_plan_id")
        if not isinstance(plan_id, str):
            raise ControllerError(
                "LEGACY_TRANSACTION_REQUIRES_RECOVERY",
                "interrupted release predates persisted ReleasePlan support",
            )
        plan = snapshot.release_plans.get(plan_id)
        if plan is None or plan.allocation_id != allocation_id:
            raise ControllerError("RELEASE_RECOVERY_MISMATCH", "release plan allocation differs")
        _assert_release_plan_request(
            plan,
            target_path=target_path,
            expected_record_version=plan.expected_record_version,
            owner_release=None,
            publication_receipt_id=publication_receipt_id,
            release_intent_id=None,
        )
        return plan.expected_record_version


def _publication_release_duration_ms(
    snapshot: MetadataSnapshot,
    receipt: PublicationReceipt,
    record: MetadataRecord,
) -> int | None:
    if record.lifecycle != "RELEASED":
        return None
    if receipt.status == "RELEASED" and receipt.released_at is not None:
        return _duration_ms(receipt.remote_verified_at, receipt.released_at)
    releases = [
        released
        for released in snapshot.release_receipts.values()
        if released.allocation_id == receipt.allocation_id
        and released.released_record_version == record.record_version
        and released.released_head == receipt.candidate_oid
    ]
    if len(releases) != 1:
        return None
    return _duration_ms(receipt.remote_verified_at, releases[0].released_at)


def complete_publication_lease(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    lease_id: str,
    published_commit: str,
    finalizer_record_id: str,
    run_id: str,
    auto_release: bool = True,
) -> dict[str, Any]:
    finalizer_evidence = finalizer_record_id.strip()
    if not finalizer_evidence:
        raise ControllerError(
            "MISSING_FINALIZER_RECORD", "publication Finalizer evidence is required"
        )
    repository_initial = inventory(repository_path)
    # Auto-release may remove the checkout used to locate this repository.
    repository_path = repository_initial.canonical_path
    store = MetadataStore(repository_initial.common_dir)
    receipt: PublicationReceipt
    updated: MetadataRecord
    item: WorktreeGitState | None = None
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        policy = load_policy(repository.canonical_path)
        snapshot = store._load_unlocked()
        existing_receipt = snapshot.publication_receipts.get(lease_id)
        lease = snapshot.publication_lease
        if existing_receipt is not None:
            receipt = existing_receipt
            record = snapshot.records.get(receipt.allocation_id)
            if record is None:
                raise ControllerError("BLOCK_DRIFT", "publication receipt allocation is missing")
            _cas(record, expected_record_version)
            published_oid = resolve_commit(repository.canonical_path, published_commit)
            if (
                published_oid != receipt.candidate_oid
                or finalizer_evidence != receipt.finalizer_record_id
                or run_id != receipt.run_id
            ):
                raise ControllerError(
                    "PUBLICATION_EVIDENCE_MISMATCH", "publication receipt recovery identity differs"
                )
            if record.lifecycle != "RELEASED":
                item, managed = _managed_record(repository, snapshot, target)
                if managed.allocation_id != record.allocation_id:
                    raise ControllerError(
                        "PUBLICATION_EVIDENCE_MISMATCH", "target allocation differs"
                    )
                if item.detached or item.head != receipt.candidate_oid:
                    raise ControllerError(
                        "CANDIDATE_IDENTITY_MISMATCH",
                        "publication recovery requires the exact attached candidate HEAD",
                    )
            if record.lifecycle == "INTEGRATING":
                updated = _updated_record(
                    record,
                    "PUBLICATION_RECEIPT_RECOVERED",
                    lifecycle="PUBLISHED",
                    finalizer_record_id=receipt.finalizer_record_id,
                    finalizer_commit=receipt.candidate_oid,
                )
                store.replace_record(updated)
                intent = _intent_for_allocation(snapshot, record.allocation_id)
                if intent is not None and intent.state != "REMOTE_VERIFIED":
                    recovered_intent = _updated_intent(
                        intent,
                        "PUBLICATION_RECEIPT_RECOVERED",
                        state="REMOTE_VERIFIED",
                        publication_identity=receipt.receipt_id,
                        published_at=receipt.remote_verified_at,
                    )
                    store.replace_integration_intent(recovered_intent)
                    if snapshot.repository_id is None:
                        raise ControllerError("BLOCK_DRIFT", "repository metadata is missing")
                    _emit_lifecycle_event(
                        store, snapshot.repository_id, recovered_intent, "REMOTE_VERIFIED"
                    )
            elif record.lifecycle in {"PUBLISHED", "RELEASED"}:
                updated = record
            else:
                raise ControllerError(
                    "INVALID_LIFECYCLE", "publication receipt cannot recover this lifecycle"
                )
            if lease is not None:
                if lease.lease_id != receipt.receipt_id:
                    raise ControllerError(
                        "PUBLICATION_LEASE_DRIFT", "another lease appeared during receipt recovery"
                    )
                store.remove_publication_lease(lease.lease_id)
            if receipt.lease_released_at is None:
                if snapshot.repository_id is None:
                    raise ControllerError("BLOCK_DRIFT", "repository metadata is missing")
                reconciliation_id = str(
                    uuid.uuid5(
                        uuid.UUID(snapshot.repository_id),
                        f"publication-lease-release-v1:{receipt.receipt_id}",
                    )
                )
                reconciliation = ReconciliationReceipt(
                    schema_version=RECONCILIATION_RECEIPT_SCHEMA_VERSION,
                    receipt_id=reconciliation_id,
                    repository_id=snapshot.repository_id,
                    source_receipt_id=receipt.receipt_id,
                    allocation_id=record.allocation_id,
                    observed_record_version=updated.record_version,
                    observed_record_lifecycle=updated.lifecycle,
                    observed_receipt_status=receipt.status,
                    differences=("publication_receipt.lease_released_at=null",),
                    recovered_intent_id=receipt.intent_id,
                    recovered_intent_state="REMOTE_VERIFIED",
                    result="LEASE_RELEASE_ACKNOWLEDGED",
                    actor="lease-complete-recovery",
                    reconciled_at=now_iso(),
                )
                store.write_reconciliation_receipt(reconciliation)
        else:
            if lease is None or lease.lease_id != lease_id:
                raise ControllerError("LEASE_MISMATCH", "publication lease does not match")
            if run_id != lease.run_id:
                raise ControllerError("LEASE_MISMATCH", "publication run identity does not match")
            if (
                policy.publication_remote != lease.remote
                or policy.publication_branch != lease.branch
            ):
                raise ControllerError(
                    "PUBLICATION_POLICY_DRIFT", "publication target changed while lease was active"
                )
            item, record = _managed_record(repository, snapshot, target)
            _cas(record, expected_record_version)
            if item.detached:
                raise ControllerError(
                    "DETACHED_WORKTREE",
                    "publication completion requires an attached candidate",
                )
            published_oid = resolve_commit(item.path, published_commit)
            intent = _intent_for_allocation(snapshot, record.allocation_id)
            if (
                record.allocation_id != lease.allocation_id
                or record.lifecycle != "INTEGRATING"
                or item.head != lease.candidate_oid
                or published_oid != lease.candidate_oid
                or record.finalizer_record_id != lease.validation_evidence_id
                or (
                    intent is not None
                    and (
                        intent.state != "PUBLISHING"
                        or intent.publication_identity != lease.lease_id
                    )
                )
            ):
                raise ControllerError(
                    "PUBLICATION_EVIDENCE_MISMATCH",
                    "completion evidence does not match the leased candidate",
                )
            remote_head = fetch_remote_branch(
                repository.canonical_path,
                policy.publication_remote,
                policy.publication_branch,
            )
            repository_after_fetch = inventory(repository_path)
            fetched_item, fetched_record = _managed_record(repository_after_fetch, snapshot, target)
            if (
                fetched_item.key != item.key
                or fetched_item.head != lease.candidate_oid
                or fetched_record.allocation_id != record.allocation_id
            ):
                raise ControllerError(
                    "BLOCK_DRIFT", "candidate changed during publication verification"
                )
            if remote_head == lease.expected_main_oid:
                raise ControllerError(
                    "PUBLICATION_NOT_OBSERVED",
                    "remote is unchanged; re-enter the fenced publish gate instead of finalizing",
                )
            if remote_head != lease.candidate_oid:
                raise ControllerError(
                    "REMOTE_PUBLICATION_MISMATCH",
                    "fresh remote publication branch does not equal the leased candidate",
                )
            verified_at = now_iso()
            receipt = PublicationReceipt(
                schema_version=PUBLICATION_RECEIPT_SCHEMA_VERSION,
                receipt_id=lease.lease_id,
                repository_id=lease.repository_id,
                allocation_id=lease.allocation_id,
                intent_id=intent.intent_id if intent is not None else None,
                target_ref=lease.target_ref,
                holder=lease.holder,
                run_id=lease.run_id,
                expected_main_oid=lease.expected_main_oid,
                candidate_oid=lease.candidate_oid,
                validation_evidence_id=lease.validation_evidence_id,
                finalizer_record_id=finalizer_evidence,
                status="REMOTE_VERIFIED",
                acquired_at=lease.acquired_at,
                remote_verified_at=verified_at,
                lease_released_at=verified_at,
                released_at=None,
                lease_duration_ms=_duration_ms(lease.acquired_at, verified_at),
                remote_verified_to_released_ms=None,
                source_results=_publication_source_results(
                    repository_after_fetch, intent, lease.candidate_oid
                ),
            )
            store.write_publication_receipt(receipt)
            updated = _updated_record(
                record,
                "PUBLICATION_COMPLETED",
                lifecycle="PUBLISHED",
                finalizer_record_id=finalizer_evidence,
                finalizer_commit=published_oid,
            )
            store.replace_record(updated)
            if intent is not None:
                published_intent = _updated_intent(
                    intent,
                    "PUBLICATION_REMOTE_VERIFIED",
                    state="REMOTE_VERIFIED",
                    published_at=verified_at,
                )
                store.replace_integration_intent(published_intent)
                if snapshot.repository_id is None:
                    raise ControllerError("BLOCK_DRIFT", "repository metadata is missing")
                _emit_lifecycle_event(
                    store, snapshot.repository_id, published_intent, "REMOTE_VERIFIED"
                )
            store.remove_publication_lease(lease.lease_id)
    cleanup: dict[str, Any] = {"result": "NOT_REQUESTED", "blocker": None}
    if auto_release and updated.lifecycle != "RELEASED":
        if item is None:
            raise ControllerError("BLOCK_DRIFT", "published candidate checkout is missing")
        registered_venv = any(
            resource["path"] == ".venv"
            and resource["state"] == "registered"
            and (item.path / resource["path"]).exists()
            for resource in updated.disposable_resources
        )
        try:
            release(
                repository_path,
                target=item.path,
                expected_record_version=updated.record_version,
                owner_release=None,
                teardown_reproducible_venv=registered_venv,
                publication_receipt_id=receipt.receipt_id,
            )
        except ControllerError as blocked:
            if blocked.code not in RETRYABLE_RELEASE_BLOCKERS:
                # Drift, identity and unknown-work failures are integrity failures, not
                # pending cleanup, and must keep stopping the call.
                raise
            # The remote publication already happened and is already recorded. Cleanup
            # is a separate outcome: report it, keep the receipt, and still run the
            # source trigger so its answer says what the sources are waiting on.
            cleanup = {"result": "CLEANUP_PENDING", "blocker": blocked.code}
            source_release_trigger = _trigger_source_release_intents(
                repository_path,
                publication_receipt_id=receipt.receipt_id,
            )
            payload = status(repository_path)
            payload["command"] = "lease-complete"
            payload["decision"] = {
                "code": "PUBLICATION_COMPLETED",
                "reason": "remote publication verified; candidate cleanup is still pending",
                "allocation_id": updated.allocation_id,
                "record_version": updated.record_version,
                "commit": published_oid,
                "receipt_id": receipt.receipt_id,
                "receipt_status": "persisted",
                "candidate_release_status": updated.lifecycle,
                "publication_lease_duration_ms": receipt.lease_duration_ms,
                "remote_verified_to_released_ms": None,
                "cleanup": cleanup,
                "source_release_trigger": source_release_trigger,
            }
            return payload
        cleanup = {"result": "RELEASED", "blocker": None}
        with store.lock(exclusive=True):
            snapshot = store._load_unlocked()
            released_record = snapshot.records.get(receipt.allocation_id)
            current_receipt = snapshot.publication_receipts.get(receipt.receipt_id)
            if released_record is None or current_receipt is None:
                raise ControllerError("BLOCK_DRIFT", "auto-release evidence disappeared")
            if released_record.lifecycle != "RELEASED":
                raise ControllerError("BLOCK_DRIFT", "auto-release did not reach RELEASED")
            released_at = now_iso()
            if current_receipt.intent_id is not None:
                intent = snapshot.integration_intents.get(current_receipt.intent_id)
                if intent is not None and intent.state != "RELEASED":
                    released_intent = _updated_intent(
                        intent,
                        "CANDIDATE_AUTO_RELEASED",
                        state="RELEASED",
                        released_at=released_at,
                    )
                    store.replace_integration_intent(released_intent)
                    if snapshot.repository_id is None:
                        raise ControllerError("BLOCK_DRIFT", "repository metadata is missing")
                    _emit_lifecycle_event(
                        store, snapshot.repository_id, released_intent, "RELEASED"
                    )
            updated = released_record
    elif updated.lifecycle == "RELEASED":
        release_version = _pending_publication_release_version(
            store,
            target=target,
            allocation_id=updated.allocation_id,
            publication_receipt_id=receipt.receipt_id,
        )
        if release_version is not None:
            release(
                repository_path,
                target=target,
                expected_record_version=release_version,
                owner_release=None,
                publication_receipt_id=receipt.receipt_id,
            )
        cleanup = {"result": "RELEASED", "blocker": None}
        with store.lock(exclusive=True):
            snapshot = store._load_unlocked()
            if not _publication_receipt_effectively_released(snapshot, receipt):
                raise ControllerError(
                    "PUBLICATION_RECEIPT_DRIFT", "released candidate lacks release evidence"
                )
            if receipt.intent_id is not None:
                intent = snapshot.integration_intents.get(receipt.intent_id)
                if intent is not None and intent.state != "RELEASED":
                    released_intent = _updated_intent(
                        intent,
                        "CANDIDATE_RELEASE_ACKNOWLEDGED",
                        state="RELEASED",
                        released_at=now_iso(),
                    )
                    store.replace_integration_intent(released_intent)
                    if snapshot.repository_id is None:
                        raise ControllerError("BLOCK_DRIFT", "repository metadata is missing")
                    _emit_lifecycle_event(
                        store, snapshot.repository_id, released_intent, "RELEASED"
                    )
    source_release_trigger = _trigger_source_release_intents(
        repository_path,
        publication_receipt_id=receipt.receipt_id,
    )
    payload = status(repository_path)
    payload["command"] = "lease-complete"
    payload["decision"] = {
        "code": "PUBLICATION_RELEASED"
        if updated.lifecycle == "RELEASED"
        else "PUBLICATION_COMPLETED",
        "reason": (
            "remote publication verified and candidate automatically released"
            if updated.lifecycle == "RELEASED"
            else "remote publication verified, receipt persisted, and lease released"
        ),
        "allocation_id": updated.allocation_id,
        "record_version": updated.record_version,
        "commit": published_oid,
        "receipt_id": receipt.receipt_id,
        "cleanup": cleanup,
        "receipt_status": receipt.status,
        "candidate_release_status": updated.lifecycle,
        "publication_lease_duration_ms": receipt.lease_duration_ms,
        "remote_verified_to_released_ms": _publication_release_duration_ms(
            snapshot, receipt, updated
        ),
        "source_release_trigger": source_release_trigger,
    }
    return payload


def abort_publication_lease(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    lease_id: str | None,
    owner: str,
    run_id: str,
) -> dict[str, Any]:
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        lease = snapshot.publication_lease
        item, record = _managed_record(repository, snapshot, target)
        _cas(record, expected_record_version)
        if lease is None:
            if (
                lease_id is not None
                or record.lifecycle != "INTEGRATING"
                or record.owner != owner
                or item.head != record.task_commit
                or not item.clean
            ):
                raise ControllerError(
                    "LEASE_MISMATCH", "no recoverable interrupted publication transition exists"
                )
            updated = _updated_record(
                record,
                "PUBLICATION_LEASE_RECOVERED",
                lifecycle="HANDOFF_READY",
            )
            store.replace_record(updated)
            decision_code = "PUBLICATION_LEASE_RECOVERED"
        else:
            if lease.lease_id != lease_id or lease.holder != owner or lease.run_id != run_id:
                raise ControllerError(
                    "LEASE_MISMATCH", "publication lease owner or id does not match"
                )
            if (
                record.allocation_id != lease.allocation_id
                or record.lifecycle not in {"INTEGRATING", "HANDOFF_READY"}
                or item.head != lease.candidate_oid
                or not item.clean
            ):
                raise ControllerError(
                    "PUBLICATION_LEASE_DRIFT", "drifted publication lease cannot be aborted"
                )
            updated = _updated_record(
                record, "PUBLICATION_LEASE_ABORTED", lifecycle="HANDOFF_READY"
            )
            store.replace_record(updated)
            store.remove_publication_lease(lease.lease_id)
            decision_code = "PUBLICATION_LEASE_ABORTED"
        intent = _intent_for_allocation(snapshot, record.allocation_id)
        if intent is not None:
            if intent.state != "PUBLISHING":
                raise ControllerError(
                    "INTEGRATION_INTENT_MISMATCH",
                    "publication abort requires a publishing intent",
                )
            restored_intent = _updated_intent(
                intent,
                "PUBLICATION_LEASE_ABORTED",
                state="READY_TO_PUBLISH",
                publication_identity=None,
            )
            store.replace_integration_intent(restored_intent)
            if snapshot.repository_id is None:
                raise ControllerError("BLOCK_DRIFT", "repository metadata is missing")
            _emit_lifecycle_event(store, snapshot.repository_id, restored_intent, "PREPARED")
    payload = status(repository_path)
    payload["command"] = "lease-abort"
    payload["decision"] = {
        "code": decision_code,
        "reason": (
            "interrupted lease transition returned to handoff-ready"
            if lease is None
            else "publication authority released without publishing"
        ),
        "allocation_id": updated.allocation_id,
        "record_version": updated.record_version,
        "lease_id": lease.lease_id if lease is not None else None,
    }
    return payload


def _reconciliation_for_publication_receipt(
    snapshot: MetadataSnapshot, receipt_id: str
) -> ReconciliationReceipt | None:
    matches = [
        receipt
        for receipt in snapshot.reconciliation_receipts.values()
        if receipt.source_receipt_id == receipt_id
        and receipt.result == "RELEASE_STATE_ACKNOWLEDGED"
    ]
    if len(matches) > 1:
        raise ControllerError(
            "RECONCILIATION_DRIFT", "publication receipt has multiple reconciliation receipts"
        )
    return matches[0] if matches else None


def _publication_receipt_effectively_released(
    snapshot: MetadataSnapshot, receipt: PublicationReceipt
) -> bool:
    if receipt.status == "RELEASED":
        return True
    reconciliation = _reconciliation_for_publication_receipt(snapshot, receipt.receipt_id)
    record = snapshot.records.get(receipt.allocation_id)
    if record is None or record.lifecycle != "RELEASED":
        return False
    if reconciliation is not None:
        return bool(
            reconciliation.allocation_id == record.allocation_id
            and reconciliation.observed_record_version == record.record_version
        )
    return any(
        release_receipt.allocation_id == record.allocation_id
        and release_receipt.released_record_version == record.record_version
        and (plan := snapshot.release_plans.get(release_receipt.plan_id)) is not None
        and plan.publication_receipt_id == receipt.receipt_id
        for release_receipt in snapshot.release_receipts.values()
    )


def _integration_release_evidence(
    repository: RepositoryInventory,
    snapshot: MetadataSnapshot,
    record: MetadataRecord,
    *,
    integration_intent_id: str | None = None,
) -> tuple[IntegrationIntent, PublicationReceipt, dict[str, str]] | None:
    candidates = [
        intent
        for intent in snapshot.integration_intents.values()
        if record.allocation_id in intent.source_allocations
        and (integration_intent_id is None or intent.intent_id == integration_intent_id)
    ]
    candidates.sort(key=lambda intent: (intent.updated_at, intent.intent_id), reverse=True)
    for integration_intent in candidates:
        source_index = integration_intent.source_allocations.index(record.allocation_id)
        if integration_intent.source_handoffs[source_index] != record.handoff_commit:
            continue
        receipts = [
            receipt
            for receipt in snapshot.publication_receipts.values()
            if receipt.intent_id == integration_intent.intent_id
            and receipt.allocation_id == integration_intent.allocation_id
            and receipt.candidate_oid == integration_intent.candidate_oid
            and _publication_receipt_lease_released(snapshot, receipt)
        ]
        receipts.sort(
            key=lambda receipt: (receipt.remote_verified_at, receipt.receipt_id), reverse=True
        )
        for receipt in receipts:
            if not _publication_receipt_effectively_released(snapshot, receipt):
                continue
            policy = load_policy(repository.canonical_path)
            remote_ref = f"refs/remotes/{policy.publication_remote}/{policy.publication_branch}"
            try:
                remote_head = resolve_commit(repository.canonical_path, remote_ref)
            except ControllerError:
                continue
            if not is_ancestor(repository.canonical_path, receipt.candidate_oid, remote_head):
                continue
            source_result = next(
                (
                    result
                    for result in receipt.source_results
                    if result["source_allocation_id"] == record.allocation_id
                    and result["source_handoff_oid"] == record.handoff_commit
                ),
                None,
            )
            if source_result is None:
                if not is_ancestor(
                    repository.canonical_path, record.handoff_commit, receipt.candidate_oid
                ):
                    continue
                source_result = {
                    "source_allocation_id": record.allocation_id,
                    "source_handoff_oid": str(record.handoff_commit),
                    "mode": "ancestry",
                    "absorbed_oid": str(record.handoff_commit),
                    "evidence_id": "git-ancestry",
                }
            elif source_result["mode"] == "equivalent-replay" and not is_ancestor(
                repository.canonical_path,
                source_result["absorbed_oid"],
                receipt.candidate_oid,
            ):
                continue
            return integration_intent, receipt, source_result
    return None


def create_release_intent(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner: str,
    trigger: str,
    integration_intent_id: str | None = None,
    expected_allocation_id: str | None = None,
) -> dict[str, Any]:
    allowed_triggers = {
        "integration-remote-verified",
        "task-terminal",
        "session-archive",
        "manual",
        "capacity-pressure",
        "binding-cleared",
    }
    if trigger not in allowed_triggers:
        raise ControllerError("INVALID_TRIGGER", f"unsupported release trigger: {trigger}")
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    created = False
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        policy = load_policy(repository.canonical_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        _cas(record, expected_record_version, expected_allocation_id)
        if snapshot.repository_id is None:
            raise ControllerError("BLOCK_DRIFT", "repository metadata is missing")
        if record.owner != owner:
            raise ControllerError("OWNER_MISMATCH", "release intent owner does not match")
        if record.role not in {"feature", "audit"}:
            raise ControllerError(
                "INVALID_RELEASE_INTENT", "release intent is limited to feature/audit"
            )
        if record.lifecycle != "HANDOFF_READY":
            raise ControllerError(
                "INVALID_LIFECYCLE", "release intent requires HANDOFF_READY lifecycle"
            )
        if not item.clean:
            raise ControllerError("DIRTY_WORKTREE", "release intent requires a clean worktree")
        identities = (record.task_commit, record.handoff_commit, record.finalizer_commit)
        if (
            any(identity is None for identity in identities)
            or any(identity != item.head for identity in identities)
            or not record.finalizer_record_id
        ):
            raise ControllerError(
                "IDENTITY_MISMATCH",
                "task, handoff, Finalizer, and worktree HEAD identities must match",
            )
        now = datetime.fromisoformat(now_iso())
        existing = [
            intent
            for intent in _release_intents_for_allocation(snapshot, record.allocation_id)
            if intent.status in {"PENDING", "BLOCKED_RETRYABLE"}
            and intent.expected_head == item.head
            and intent.expected_record_version == record.record_version
        ]
        if len(existing) > 1:
            raise ControllerError("INVALID_METADATA", "multiple active release intents exist")
        if existing:
            current = existing[0]
            if datetime.fromisoformat(current.expires_at) <= now:
                store.replace_release_intent(
                    _updated_release_intent(
                        current,
                        "RELEASE_INTENT_EXPIRED",
                        status="STALE",
                        blocker="RELEASE_INTENT_EXPIRED",
                    )
                )
                existing = []
            else:
                intent = current
        if not existing:
            integration_evidence = _integration_release_evidence(
                repository,
                snapshot,
                record,
                integration_intent_id=integration_intent_id,
            )
            blocker = None
            integration_identity: dict[str, str | None]
            if integration_evidence is None:
                blocker = "MISSING_INTEGRATION_EVIDENCE"
                integration_identity = {
                    "integration_intent_id": None,
                    "integration_allocation_id": None,
                    "publication_receipt_id": None,
                    "integration_candidate_oid": None,
                    "integration_mode": None,
                    "integration_evidence_id": None,
                }
            else:
                integrated, receipt, source_result = integration_evidence
                integration_identity = {
                    "integration_intent_id": integrated.intent_id,
                    "integration_allocation_id": integrated.allocation_id,
                    "publication_receipt_id": receipt.receipt_id,
                    "integration_candidate_oid": receipt.candidate_oid,
                    "integration_mode": source_result["mode"],
                    "integration_evidence_id": source_result["evidence_id"],
                }
            if record.external_bindings:
                blocker = "EXTERNAL_BINDING"
            timestamp = now_iso()
            expires_at = (
                datetime.fromisoformat(timestamp)
                + timedelta(seconds=policy.release_intent_ttl_seconds)
            ).isoformat(timespec="seconds")
            intent = release_intent_from_dict(
                {
                    "schema_version": RELEASE_INTENT_SCHEMA_VERSION,
                    "intent_id": str(uuid.uuid4()),
                    "repository_id": snapshot.repository_id,
                    "allocation_id": record.allocation_id,
                    "task_key": record.task_key,
                    "authority_key": record.authority_key,
                    "owner": record.owner,
                    "role": record.role,
                    "expected_head": item.head,
                    "expected_record_version": record.record_version,
                    "task_commit": record.task_commit,
                    "handoff_commit": record.handoff_commit,
                    "finalizer_commit": record.finalizer_commit,
                    "finalizer_record_id": record.finalizer_record_id,
                    **integration_identity,
                    "trigger": trigger,
                    "status": "BLOCKED_RETRYABLE" if blocker else "PENDING",
                    "blocker": blocker,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "expires_at": expires_at,
                    "history": [
                        {
                            "at": timestamp,
                            "event": "RELEASE_INTENT_CREATED",
                            "from": None,
                            "to": "BLOCKED_RETRYABLE" if blocker else "PENDING",
                            "blocker": blocker,
                        }
                    ],
                }
            )
            store.write_release_intent(intent)
            created = True
    payload = status(repository_path)
    payload["command"] = "release-intent"
    payload["decision"] = {
        "code": "RELEASE_INTENT_CREATED" if created else "RELEASE_INTENT_EXISTS",
        "reason": (
            "short-lived release intent recorded"
            if created
            else "matching active release intent already exists"
        ),
        "intent_id": intent.intent_id,
        "allocation_id": intent.allocation_id,
        "expected_head": intent.expected_head,
        "expected_record_version": intent.expected_record_version,
        "trigger": intent.trigger,
        "status": intent.status,
        "blocker": intent.blocker,
        "expires_at": intent.expires_at,
    }
    return payload


def _closed_source_release_outcome(
    repository_path: Path,
    *,
    integration_intent: IntegrationIntent,
    source_index: int,
    publication_receipt_id: str,
    record: MetadataRecord | None,
    binding_keys: Sequence[str],
) -> dict[str, Any]:
    allocation_id = integration_intent.source_allocations[source_index]
    outcome: dict[str, Any] = {
        "allocation_id": allocation_id,
        "status": "BLOCKED",
        "result": "BLOCKED",
        "blocker": "SOURCE_RELEASE_EVIDENCE_INCOMPLETE",
        "recovery_commands": ["lifecycle-history", "doctor", "reconcile receipts --dry-run"],
    }
    try:
        history = lifecycle_history(repository_path, allocation_id=allocation_id, limit=2)["decision"]
    except ControllerError as error:
        outcome["blocker"] = error.code
        return outcome
    closed = history["closed_releases"]
    if not closed:
        if record is None:
            outcome["blocker"] = "SOURCE_RECORD_MISSING"
        elif record.lifecycle != "RELEASED":
            outcome["blocker"] = "SOURCE_NOT_HANDOFF_READY"
        return outcome
    if len(closed) != 1 or history["next_after"] is not None:
        outcome["blocker"] = "SOURCE_RELEASE_EVIDENCE_AMBIGUOUS"
        return outcome
    row = closed[0]
    if row["proof_status"] != "COMPLETE" or row["release_status"] != "RELEASED":
        return outcome
    frozen_head = integration_intent.source_handoffs[source_index]
    deliveries = [
        delivery
        for delivery in row["deliveries"]
        if delivery["publication_receipt_id"] == publication_receipt_id
        and delivery["integration_intent_id"] == integration_intent.intent_id
        and delivery["relationship"] == "source"
    ]
    if (
        binding_keys
        or history["live_allocations"]
        or row["released_head"] != frozen_head
        or row["branch"] != integration_intent.source_branches[source_index]
        or len(deliveries) != 1
        or record is None
        or any(row[key] != getattr(record, key) for key in ("task_key", "authority_key", "role"))
        or record.task_commit != frozen_head
        or record.handoff_commit != frozen_head
        or record.finalizer_commit != frozen_head
        or record.finalizer_record_id != integration_intent.source_finalizer_record_ids[source_index]
    ):
        outcome["blocker"] = "SOURCE_RELEASE_IDENTITY_MISMATCH"
        return outcome
    return {
        "allocation_id": allocation_id,
        "status": "RELEASED",
        "result": "ALREADY_RELEASED",
        "blocker": None,
        "release_receipt_id": row["release_receipt_id"],
        "plan_id": row["release_plan_id"],
        "intent_id": row["release_intent_id"],
        "proof_status": row["proof_status"],
    }


def _trigger_source_release_intents(
    repository_path: Path,
    *,
    publication_receipt_id: str,
) -> dict[str, Any]:
    repository = inventory(repository_path)
    store = MetadataStore(repository.common_dir)
    snapshot = store.load()
    receipt = snapshot.publication_receipts.get(publication_receipt_id)
    if receipt is None or receipt.intent_id is None:
        return {"created_intent_ids": [], "skipped": [], "reconciliation": None, "outcomes": []}
    integration_intent = snapshot.integration_intents.get(receipt.intent_id)
    if integration_intent is None or integration_intent.state != "RELEASED":
        return {
            "created_intent_ids": [],
            "skipped": [
                {
                    "allocation_id": receipt.allocation_id,
                    "reason": "INTEGRATION_NOT_RELEASED",
                }
            ],
            "reconciliation": None,
            "outcomes": [
                {
                    "allocation_id": source_allocation_id,
                    "status": "WAITING",
                    "result": "WAITING",
                    "blocker": "INTEGRATION_NOT_RELEASED",
                    "recovery_commands": ["reconcile receipts --dry-run"],
                }
                for source_allocation_id in integration_intent.source_allocations
            ] if integration_intent is not None else [],
        }
    created_ids: list[str] = []
    created_sources: dict[str, str] = {}
    skipped: list[dict[str, str]] = []
    outcomes: dict[str, dict[str, Any]] = {}
    for source_index, source_allocation_id in enumerate(integration_intent.source_allocations):
        current = store.load()
        record = current.records.get(source_allocation_id)
        binding_keys = [
            key
            for key, allocation_id in current.bindings.items()
            if allocation_id == source_allocation_id
        ]
        live_repository = inventory(repository_path)
        item = (
            next(
                (
                    candidate
                    for candidate in live_repository.worktrees
                    if len(binding_keys) == 1 and candidate.key == binding_keys[0]
                ),
                None,
            )
            if record is not None
            else None
        )
        if record is None or item is None or record.lifecycle != "HANDOFF_READY":
            outcome = _closed_source_release_outcome(
                repository_path,
                integration_intent=integration_intent,
                source_index=source_index,
                publication_receipt_id=receipt.receipt_id,
                record=record,
                binding_keys=binding_keys,
            )
            outcomes[source_allocation_id] = outcome
            if outcome["status"] == "RELEASED":
                continue
            skipped.append(
                {
                    "allocation_id": source_allocation_id,
                    "reason": str(outcome["blocker"]),
                }
            )
            continue
        try:
            created = create_release_intent(
                repository_path,
                target=item.path,
                expected_record_version=record.record_version,
                owner=record.owner,
                trigger="integration-remote-verified",
                integration_intent_id=integration_intent.intent_id,
            )
        except ControllerError as error:
            skipped.append({"allocation_id": source_allocation_id, "reason": error.code})
            stale = [
                intent for intent in store.load().release_intents.values()
                if intent.allocation_id == source_allocation_id
                and intent.integration_intent_id == integration_intent.intent_id
                and intent.expected_head == integration_intent.source_handoffs[source_index]
                and intent.status == "STALE"
            ]
            outcome = {
                "allocation_id": source_allocation_id,
                "status": "STALE" if len(stale) == 1 else "BLOCKED",
                "result": "STALE" if len(stale) == 1 else "BLOCKED",
                "blocker": error.code,
                "recovery_commands": ["doctor", "release-intent"],
            }
            if len(stale) == 1:
                outcome["intent_id"] = stale[0].intent_id
            outcomes[source_allocation_id] = outcome
            continue
        intent_id = str(created["decision"]["intent_id"])
        created_ids.append(intent_id)
        created_sources[intent_id] = source_allocation_id
    reconciliation: dict[str, Any] | None = None
    if created_ids:
        reconciled = reconcile_release_intents(
            repository_path,
            actor="lease-complete",
            trigger="integration-remote-verified",
            intent_ids=tuple(created_ids),
        )
        reconciliation = dict(reconciled["decision"])
        for result in reconciliation["outcomes"]:
            allocation_id = created_sources[str(result["intent_id"])]
            outcome = {**result, "allocation_id": allocation_id}
            if outcome["status"] != "RELEASED":
                outcome["recovery_commands"] = (
                    ["doctor", "release-intent"] if outcome["status"] == "STALE"
                    else ["reconcile release-intents", "release --dry-run"]
                )
            outcomes[allocation_id] = outcome
    return {
        "created_intent_ids": created_ids,
        "skipped": skipped,
        "reconciliation": reconciliation,
        "outcomes": [outcomes[allocation_id] for allocation_id in integration_intent.source_allocations],
    }


def _acknowledge_publication_candidate_release(
    repository_path: Path,
    *,
    publication_receipt_id: str,
) -> dict[str, Any]:
    repository = inventory(repository_path)
    store = MetadataStore(repository.common_dir)
    with store.lock(exclusive=True):
        snapshot = store._load_unlocked()
        receipt = snapshot.publication_receipts.get(publication_receipt_id)
        if receipt is None:
            raise ControllerError("MISSING_PUBLICATION_RECEIPT", "publication receipt is missing")
        record = snapshot.records.get(receipt.allocation_id)
        if (
            record is None
            or record.lifecycle != "RELEASED"
            or record.task_commit != receipt.candidate_oid
            or receipt.allocation_id in snapshot.bindings.values()
        ):
            raise ControllerError(
                "PUBLICATION_RECEIPT_DRIFT",
                "released publication candidate does not match immutable receipt evidence",
            )
        if receipt.intent_id is not None:
            intent = snapshot.integration_intents.get(receipt.intent_id)
            if (
                intent is None
                or intent.allocation_id != receipt.allocation_id
                or intent.candidate_oid != receipt.candidate_oid
                or intent.state not in {"REMOTE_VERIFIED", "RELEASED"}
            ):
                raise ControllerError(
                    "PUBLICATION_RECEIPT_DRIFT",
                    "integration intent does not match released publication evidence",
                )
            if intent.state == "REMOTE_VERIFIED":
                released_intent = _updated_intent(
                    intent,
                    "CANDIDATE_RELEASE_ACKNOWLEDGED",
                    state="RELEASED",
                    released_at=now_iso(),
                )
                store.replace_integration_intent(released_intent)
                if snapshot.repository_id is None:
                    raise ControllerError("BLOCK_DRIFT", "repository metadata is missing")
                _emit_lifecycle_event(store, snapshot.repository_id, released_intent, "RELEASED")
    return _trigger_source_release_intents(
        repository_path,
        publication_receipt_id=publication_receipt_id,
    )


def reconcile_publication_receipts(
    repository_path: Path,
    *,
    actor: str,
    receipt_ids: tuple[str, ...] = (),
    dry_run: bool = False,
) -> dict[str, Any]:
    if not actor.strip():
        raise ControllerError("INVALID_ACTOR", "reconciliation actor is required")
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    plans: list[dict[str, Any]] = []
    written: list[str] = []
    with store.lock(exclusive=not dry_run):
        snapshot = store._load_unlocked()
        if snapshot.repository_id is None:
            raise ControllerError("BLOCK_DRIFT", "repository metadata is missing")
        selected = (
            [snapshot.publication_receipts.get(receipt_id) for receipt_id in receipt_ids]
            if receipt_ids
            else list(snapshot.publication_receipts.values())
        )
        if any(receipt is None for receipt in selected):
            raise ControllerError("MISSING_PUBLICATION_RECEIPT", "publication receipt is missing")
        for receipt in sorted(
            (value for value in selected if value is not None),
            key=lambda value: value.receipt_id,
        ):
            record = snapshot.records.get(receipt.allocation_id)
            intent = (
                snapshot.integration_intents.get(receipt.intent_id)
                if receipt.intent_id is not None
                else None
            )
            if receipt.status == "RELEASED":
                continue
            if (
                receipt.status != "REMOTE_VERIFIED"
                or receipt.lease_released_at is None
                or record is None
                or record.lifecycle != "RELEASED"
                or record.task_commit != receipt.candidate_oid
                or record.finalizer_record_id != receipt.finalizer_record_id
                or receipt.allocation_id in snapshot.bindings.values()
                or intent is None
                or intent.allocation_id != record.allocation_id
                or intent.candidate_oid != receipt.candidate_oid
                or intent.state not in {"REMOTE_VERIFIED", "RELEASED"}
            ):
                raise ControllerError(
                    "UNSAFE_RECEIPT_RECONCILIATION",
                    f"publication receipt cannot be safely reconciled: {receipt.receipt_id}",
                )
            differences = ["publication_receipt.status=REMOTE_VERIFIED"]
            if intent.state == "REMOTE_VERIFIED":
                differences.append("integration_intent.state=REMOTE_VERIFIED")
            if intent.schema_version < INTEGRATION_INTENT_SCHEMA_VERSION:
                differences.append(
                    f"integration_intent.schema_version={intent.schema_version}"
                )
            receipt_id = str(
                uuid.uuid5(
                    uuid.UUID(snapshot.repository_id),
                    ":".join(
                        (
                            "publication-reconciliation-v1",
                            receipt.receipt_id,
                            str(record.record_version),
                            record.lifecycle,
                        )
                    ),
                )
            )
            reconciliation = ReconciliationReceipt(
                schema_version=RECONCILIATION_RECEIPT_SCHEMA_VERSION,
                receipt_id=receipt_id,
                repository_id=snapshot.repository_id,
                source_receipt_id=receipt.receipt_id,
                allocation_id=record.allocation_id,
                observed_record_version=record.record_version,
                observed_record_lifecycle=record.lifecycle,
                observed_receipt_status=receipt.status,
                differences=tuple(differences),
                recovered_intent_id=intent.intent_id,
                recovered_intent_state="RELEASED",
                result="RELEASE_STATE_ACKNOWLEDGED",
                actor=actor.strip(),
                reconciled_at=now_iso(),
            )
            existing = snapshot.reconciliation_receipts.get(receipt_id)
            plans.append(
                {
                    "reconciliation_receipt_id": receipt_id,
                    "source_receipt_id": receipt.receipt_id,
                    "allocation_id": record.allocation_id,
                    "record_version": record.record_version,
                    "differences": differences,
                    "already_reconciled": existing is not None,
                }
            )
            if dry_run or existing is not None:
                continue
            if (
                intent.state == "REMOTE_VERIFIED"
                and intent.schema_version >= INTEGRATION_INTENT_SCHEMA_VERSION
            ):
                released_intent = _updated_intent(
                    intent,
                    "PUBLICATION_RECEIPT_RECONCILED",
                    state="RELEASED",
                    released_at=now_iso(),
                )
                store.replace_integration_intent(released_intent)
                _emit_lifecycle_event(store, snapshot.repository_id, released_intent, "RELEASED")
            store.write_reconciliation_receipt(reconciliation)
            written.append(receipt_id)
    payload = status(repository_path)
    payload["command"] = "reconcile receipts"
    payload["decision"] = {
        "code": "RECEIPT_RECONCILIATION_PLAN" if dry_run else "RECEIPTS_RECONCILED",
        "reason": (
            "safe receipt recovery actions planned without mutation"
            if dry_run
            else "safe receipt recovery evidence recorded without rewriting source receipts"
        ),
        "plans": plans,
        "written_receipt_ids": written,
    }
    return payload


RETRYABLE_RELEASE_BLOCKERS = frozenset(
    {
        "MISSING_INTEGRATION_EVIDENCE",
        "RUNTIME_REFERENCE",
        "STALE_EDITABLE_REFERENCE",
        "EXTERNAL_BINDING",
        # Content the task created and never classified stops this release, but the
        # owner can still classify it or remove it. Ending the intent here would
        # throw away the publication evidence that earned it and leave the slot
        # occupied with nothing left to retry.
        "IGNORED_CONTENT",
        # A scan that could not finish describes the machine at that moment, not this
        # allocation. Capacity pressure is exactly when a busy host makes these most
        # likely, so they must not end the intent either.
        "RUNTIME_SCAN_ERROR",
        "RUNTIME_SCAN_LIMIT",
        "ENVIRONMENT_SCAN_ERROR",
        "ENVIRONMENT_SCAN_LIMIT",
    }
)


def _release_intent_item(
    repository: RepositoryInventory,
    snapshot: MetadataSnapshot,
    intent: ReleaseIntent,
) -> tuple[WorktreeGitState, MetadataRecord]:
    keys = [
        key
        for key, allocation_id in snapshot.bindings.items()
        if allocation_id == intent.allocation_id
    ]
    if len(keys) != 1:
        raise ControllerError("RELEASE_INTENT_STALE", "release intent allocation binding changed")
    item = next((candidate for candidate in repository.worktrees if candidate.key == keys[0]), None)
    record = snapshot.records.get(intent.allocation_id)
    if item is None or record is None:
        raise ControllerError(
            "RELEASE_INTENT_STALE", "release intent checkout or record disappeared"
        )
    return item, record


def _release_intent_static_identity(
    snapshot: MetadataSnapshot,
    item: WorktreeGitState,
    record: MetadataRecord,
    intent: ReleaseIntent,
) -> None:
    if intent.status not in {"PENDING", "BLOCKED_RETRYABLE"}:
        raise ControllerError("INVALID_RELEASE_INTENT", f"release intent is {intent.status}")
    expires_at = datetime.fromisoformat(intent.expires_at)
    if expires_at <= datetime.now(expires_at.tzinfo):
        raise ControllerError("RELEASE_INTENT_EXPIRED", "release intent expired")
    if not all(
        (
            intent.repository_id == snapshot.repository_id,
            intent.allocation_id == record.allocation_id,
            intent.task_key == record.task_key,
            intent.authority_key == record.authority_key,
            intent.owner == record.owner,
            intent.role == record.role,
            intent.expected_head == item.head,
            intent.expected_record_version == record.record_version,
            intent.task_commit == record.task_commit,
            intent.handoff_commit == record.handoff_commit,
            intent.finalizer_commit == record.finalizer_commit,
            intent.finalizer_record_id == record.finalizer_record_id,
        )
    ):
        raise ControllerError("RELEASE_INTENT_STALE", "release intent identity changed")
    if record.role not in {"feature", "audit"} or record.lifecycle != "HANDOFF_READY":
        raise ControllerError(
            "RELEASE_INTENT_STALE",
            "automatic release is limited to HANDOFF_READY feature/audit",
        )


def _intent_with_current_integration_evidence(
    repository: RepositoryInventory,
    snapshot: MetadataSnapshot,
    record: MetadataRecord,
    intent: ReleaseIntent,
) -> ReleaseIntent:
    evidence = _integration_release_evidence(
        repository,
        snapshot,
        record,
        integration_intent_id=intent.integration_intent_id,
    )
    if evidence is None:
        if (
            intent.status == "BLOCKED_RETRYABLE"
            and intent.blocker == "MISSING_INTEGRATION_EVIDENCE"
        ):
            return intent
        return _updated_release_intent(
            intent,
            "INTEGRATION_EVIDENCE_PENDING",
            status="BLOCKED_RETRYABLE",
            blocker="MISSING_INTEGRATION_EVIDENCE",
            integration_intent_id=None,
            integration_allocation_id=None,
            publication_receipt_id=None,
            integration_candidate_oid=None,
            integration_mode=None,
            integration_evidence_id=None,
        )
    integration_intent, receipt, source_result = evidence
    values = {
        "integration_intent_id": integration_intent.intent_id,
        "integration_allocation_id": integration_intent.allocation_id,
        "publication_receipt_id": receipt.receipt_id,
        "integration_candidate_oid": receipt.candidate_oid,
        "integration_mode": source_result["mode"],
        "integration_evidence_id": source_result["evidence_id"],
        "status": "PENDING",
        "blocker": None,
    }
    if all(getattr(intent, key) == value for key, value in values.items()):
        return intent
    return _updated_release_intent(intent, "INTEGRATION_EVIDENCE_BOUND", **values)


def _mark_release_intent_result(
    store: MetadataStore,
    intent: ReleaseIntent,
    *,
    error: ControllerError,
) -> ReleaseIntent:
    if error.code in RETRYABLE_RELEASE_BLOCKERS:
        status_value = "BLOCKED_RETRYABLE"
        event = "RELEASE_BLOCKED_RETRYABLE"
    else:
        status_value = "STALE"
        event = "RELEASE_INTENT_STALE"
    if intent.status == status_value and intent.blocker == error.code:
        return intent
    updated = _updated_release_intent(
        intent,
        event,
        status=status_value,
        blocker=error.code,
    )
    store.replace_release_intent(updated)
    return updated


def reconcile_release_intents(
    repository_path: Path,
    *,
    actor: str,
    trigger: str,
    intent_ids: tuple[str, ...] = (),
    dry_run: bool = False,
) -> dict[str, Any]:
    if not actor.strip():
        raise ControllerError("INVALID_ACTOR", "release reconciliation actor is required")
    if trigger not in {
        "integration-remote-verified",
        "task-terminal",
        "session-archive",
        "binding-cleared",
        "manual",
        "capacity-pressure",
    }:
        raise ControllerError("INVALID_TRIGGER", "release reconciliation trigger is invalid")
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    initial_snapshot = store.load()
    renewed: list[dict[str, Any]] = []
    renewal_policy = load_policy(repository_initial.canonical_path)
    if not intent_ids and not dry_run and renewal_policy.automatic_feature_audit_release:
        live_allocations = {intent.allocation_id for intent in initial_snapshot.release_intents.values()
                            if intent.status in {"PENDING", "BLOCKED_RETRYABLE"}}
        now = datetime.fromisoformat(now_iso())
        for previous in sorted(initial_snapshot.release_intents.values(), key=lambda value: value.created_at, reverse=True):
            record = initial_snapshot.records.get(previous.allocation_id)
            if (previous.status != "STALE" or previous.allocation_id in live_allocations
                    or record is None or record.lifecycle != "HANDOFF_READY"):
                continue
            live_allocations.add(previous.allocation_id)
            item = next((item for item in repository_initial.worktrees
                         if initial_snapshot.bindings.get(item.key) == previous.allocation_id), None)
            if item is None:
                continue
            if not _renewal_due(initial_snapshot, previous, record, item.head, now,
                                renewal_policy.release_intent_ttl_seconds):
                renewed.append({"previous_intent_id": previous.intent_id, "deferred": "RENEWAL_BACKOFF"})
                continue
            try:
                # The old intent is immutable history. Re-enter the existing full
                # authority checks to produce a new current intent, never extend
                # the old expiry or copy its release permission onto changed work.
                fresh = create_release_intent(repository_path, target=item.path,
                    expected_record_version=record.record_version, owner=record.owner,
                    trigger=trigger, integration_intent_id=previous.integration_intent_id)
                renewed.append({"previous_intent_id": previous.intent_id, "result": fresh["decision"]})
            except ControllerError as exc:
                renewed.append({"previous_intent_id": previous.intent_id, "blocker": exc.code})
        initial_snapshot = store.load()
    selected_ids = (
        list(intent_ids)
        if intent_ids
        else [
            intent.intent_id
            for intent in sorted(
                initial_snapshot.release_intents.values(),
                key=lambda candidate: (candidate.created_at, candidate.intent_id),
            )
            if intent.status in {"PENDING", "BLOCKED_RETRYABLE"}
        ]
    )
    missing = [
        intent_id for intent_id in selected_ids if intent_id not in initial_snapshot.release_intents
    ]
    if missing:
        raise ControllerError("MISSING_RELEASE_INTENT", f"release intent is missing: {missing[0]}")

    outcomes: list[dict[str, Any]] = []
    executable: list[tuple[ReleaseIntent, ReleasePlan | None]] = []
    typed_plans: dict[str, dict[str, Any]] = {}
    for intent_id in selected_ids:
        with store.lock(exclusive=not dry_run):
            repository = inventory(repository_path)
            policy = load_policy(repository.canonical_path)
            snapshot = store._load_unlocked()
            intent = snapshot.release_intents.get(intent_id)
            if intent is None:
                raise ControllerError("MISSING_RELEASE_INTENT", "release intent disappeared")
            if intent.status not in {"PENDING", "BLOCKED_RETRYABLE"}:
                outcomes.append(
                    {
                        "intent_id": intent_id,
                        "status": intent.status,
                        "result": "SKIPPED_TERMINAL",
                        "blocker": intent.blocker,
                    }
                )
                continue
            try:
                item, record = _release_intent_item(repository, snapshot, intent)
                _release_intent_static_identity(snapshot, item, record, intent)
                evaluated = _intent_with_current_integration_evidence(
                    repository, snapshot, record, intent
                )
                if evaluated.status == "BLOCKED_RETRYABLE":
                    if not dry_run and evaluated != intent:
                        store.replace_release_intent(evaluated)
                    outcomes.append(
                        {
                            "intent_id": intent_id,
                            "allocation_id": intent.allocation_id,
                            "status": "BLOCKED_RETRYABLE",
                            "result": "BLOCKED_RETRYABLE",
                            "blocker": evaluated.blocker,
                        }
                    )
                    continue
                try:
                    if not dry_run and policy.automatic_feature_audit_release:
                        from .cleanup_attempts import reconcile_typed_locked, typed_plan
                        from .cleanup_evidence import read as read_cleanup_evidence
                        from .transaction_scope import conflicts

                        for journal in conflicts(store, allocation=record.allocation_id, target=item.path):
                            if journal.get("cleanup_plan", {}).get("contract") != TYPED_RESOURCE_CONTRACT:
                                continue
                            transaction = journal["transaction_id"]
                            _, digest = read_cleanup_evidence(store.transactions_dir / f"{transaction}.json")
                            recovery_plan = typed_plan(repository_path, store, transaction, digest)
                            reconcile_typed_locked(repository_path, store, transaction, digest,
                                dry_run=False, expected_plan_id=recovery_plan["plan_id"])
                    plan = _build_release_plan_locked(
                        repository, snapshot, item, record,
                        store=store,
                        expected_record_version=intent.expected_record_version,
                        owner_release=None, publication_receipt=None, release_intent=evaluated)
                except ControllerError as error:
                    if error.code != "IGNORED_CONTENT" or not policy.automatic_feature_audit_release:
                        raise
                    typed = _typed_cleanup_plan_locked(repository, snapshot, store, item, record,
                        owner=record.owner, expected_record_version=intent.expected_record_version)
                    if typed["blockers"] or not typed["cleanup_paths"]:
                        raise error
                    # Reuse the exact existing typed coordinator. Its unknown
                    # content, runtime, preservation and hardlink gates still apply.
                    typed_plans[intent.intent_id] = typed
                    plan = None
                if not dry_run and evaluated != intent:
                    store.replace_release_intent(evaluated)
                result_code = (
                    "ELIGIBLE_AUTO_RELEASE"
                    if policy.automatic_feature_audit_release and not dry_run
                    else "ELIGIBLE_MANUAL"
                )
                outcomes.append(
                    {
                        "intent_id": intent_id,
                        "allocation_id": intent.allocation_id,
                        "status": evaluated.status,
                        "result": result_code,
                        "blocker": None,
                        **({"plan_id": plan.plan_id} if plan else
                           {"cleanup_plan_id": typed_plans[intent.intent_id]["plan_id"]}),
                    }
                )
                if policy.automatic_feature_audit_release and not dry_run:
                    executable.append((evaluated, plan))
            except ControllerError as error:
                current = intent
                if not dry_run:
                    current = _mark_release_intent_result(store, intent, error=error)
                outcomes.append(
                    {
                        "intent_id": intent_id,
                        "allocation_id": intent.allocation_id,
                        "status": current.status
                        if not dry_run
                        else (
                            "BLOCKED_RETRYABLE"
                            if error.code in RETRYABLE_RELEASE_BLOCKERS
                            else "STALE"
                        ),
                        "result": (
                            "BLOCKED_RETRYABLE"
                            if error.code in RETRYABLE_RELEASE_BLOCKERS
                            else "STALE"
                        ),
                        "blocker": error.code,
                    }
                )

    for intent, plan in executable:
        try:
            if plan is None:
                typed = typed_plans[intent.intent_id]
                cleanup = cleanup_typed_disposable(repository_path, target=Path(typed["target_path"]),
                    expected_record_version=typed["expected_record_version"], owner=typed["owner"],
                    dry_run=False, expected_plan_id=typed["plan_id"], release_intent_id=intent.intent_id)["decision"]
                if cleanup["code"] not in {"TYPED_DISPOSABLE_CLEANED", "RESOURCE_CLEANUP_NOT_NEEDED"}:
                    blockers = cleanup["plan"]["blockers"]
                    raise ControllerError(blockers[0]["code"] if blockers else "RESOURCE_CLEANUP_FAILED",
                                          "typed cleanup did not reach a verified terminal result")
                plan = build_release_plan(repository_path, target=Path(typed["target_path"]),
                    expected_record_version=typed["expected_record_version"], owner_release=None,
                    release_intent_id=intent.intent_id)
            released = release(
                repository_path,
                target=Path(plan.target_path),
                expected_record_version=plan.expected_record_version,
                owner_release=None,
                release_intent_id=intent.intent_id,
            )
            decision = released["decision"]
            for outcome in outcomes:
                if outcome["intent_id"] == intent.intent_id:
                    outcome.update(
                        {
                            "status": "RELEASED",
                            "result": "RELEASED",
                            "release_receipt_id": decision.get("release_receipt_id"),
                        }
                    )
                    break
        except ControllerError as error:
            with store.lock(exclusive=True):
                snapshot = store._load_unlocked()
                current = snapshot.release_intents.get(intent.intent_id)
                if current is not None and current.status in {"PENDING", "BLOCKED_RETRYABLE"}:
                    current = _mark_release_intent_result(store, current, error=error)
            for outcome in outcomes:
                if outcome["intent_id"] == intent.intent_id:
                    outcome.update(
                        {
                            "status": current.status if current is not None else "STALE",
                            "result": (
                                "BLOCKED_RETRYABLE"
                                if error.code in RETRYABLE_RELEASE_BLOCKERS
                                else "STALE"
                            ),
                            "blocker": error.code,
                        }
                    )
                    break

    payload = status(repository_path)
    payload["command"] = "reconcile release-intents"
    payload["decision"] = {
        "code": "RELEASE_RECONCILIATION_PLAN" if dry_run else "RELEASE_INTENTS_RECONCILED",
        "reason": (
            "existing release intents evaluated without mutation"
            if dry_run
            else "existing intents converged under policy; no clean-worktree discovery was used"
        ),
        "actor": actor.strip(),
        "trigger": trigger,
        "automatic_release_enabled": load_policy(
            inventory(repository_path).canonical_path
        ).automatic_feature_audit_release,
        "renewed_intents": renewed,
        "outcomes": outcomes,
    }
    return payload


def _validate_release_intent(
    repository: RepositoryInventory,
    snapshot: MetadataSnapshot,
    item: WorktreeGitState,
    record: MetadataRecord,
    intent: ReleaseIntent,
) -> None:
    if intent.status not in {"PENDING", "BLOCKED_RETRYABLE"}:
        raise ControllerError("INVALID_RELEASE_INTENT", f"release intent is {intent.status}")
    expires_at = datetime.fromisoformat(intent.expires_at)
    if expires_at <= datetime.now(expires_at.tzinfo):
        raise ControllerError("RELEASE_INTENT_EXPIRED", "release intent expired")
    identities = (
        intent.repository_id == snapshot.repository_id,
        intent.allocation_id == record.allocation_id,
        intent.task_key == record.task_key,
        intent.authority_key == record.authority_key,
        intent.owner == record.owner,
        intent.role == record.role,
        intent.expected_head == item.head,
        intent.expected_record_version == record.record_version,
        intent.task_commit == record.task_commit,
        intent.handoff_commit == record.handoff_commit,
        intent.finalizer_commit == record.finalizer_commit,
        intent.finalizer_record_id == record.finalizer_record_id,
    )
    if not all(identities):
        raise ControllerError("RELEASE_INTENT_STALE", "release intent identity changed")
    if record.role not in {"feature", "audit"} or record.lifecycle != "HANDOFF_READY":
        raise ControllerError(
            "INVALID_RELEASE_INTENT", "automatic release is limited to HANDOFF_READY feature/audit"
        )
    if intent.integration_intent_id is None or intent.publication_receipt_id is None:
        raise ControllerError(
            "MISSING_INTEGRATION_EVIDENCE",
            "automatic release requires remote-verified integration evidence",
        )
    evidence = _integration_release_evidence(
        repository,
        snapshot,
        record,
        integration_intent_id=intent.integration_intent_id,
    )
    if evidence is None:
        raise ControllerError(
            "MISSING_INTEGRATION_EVIDENCE", "integration release evidence is no longer valid"
        )
    integration_intent, receipt, source_result = evidence
    if (
        receipt.receipt_id != intent.publication_receipt_id
        or integration_intent.allocation_id != intent.integration_allocation_id
        or receipt.candidate_oid != intent.integration_candidate_oid
        or source_result["mode"] != intent.integration_mode
        or source_result["evidence_id"] != intent.integration_evidence_id
        or integration_intent.state != "RELEASED"
    ):
        raise ControllerError("RELEASE_INTENT_STALE", "integration identity changed")


def _validate_multiple_preservation_cleanup(
    receipt: dict[str, Any], item: WorktreeGitState, bound: dict[str, Any]
) -> list[dict[str, Any]]:
    plans = []
    try:
        cleanup_plan = receipt["plan"]
        proofs = cleanup_plan["preservation_evidences"]
        paths = [row["source_relative"] for row in proofs]
        preserved_paths = [
            resource["path"]
            for resource in cleanup_plan["resources"]
            if resource["cleanup_action"] == "remove-preserved-tree"
        ]
        cleanup_paths = [
            resource["path"]
            for resource in cleanup_plan["resources"]
            if resource["cleanup_action"] == "remove-tree"
        ] + preserved_paths
        if (
            receipt.get("result") != "CLEANED"
            or cleanup_plan.get("contract") != MULTIPLE_PRESERVED_CLEANUP_CONTRACT
            or any(cleanup_plan.get(key) != value for key, value in bound.items())
            or not isinstance(proofs, list)
            or len(proofs) < 2
            or paths != sorted(set(paths))
            or sorted(preserved_paths) != paths
            or cleanup_plan["cleanup_paths"] != cleanup_paths
            or cleanup_plan.get("preservation_evidence") != proofs[0]["evidence"]
            or cleanup_plan.get("plan_id")
            != _release_plan_id(
                {key: value for key, value in cleanup_plan.items() if key != "plan_id"}
            )
            or [action["path"] for action in receipt["actions"]] != cleanup_plan["cleanup_paths"]
            or any(action["status"] != "COMPLETED" for action in receipt["actions"])
            or [action["resource"] for action in receipt["action_evidence"]]
            != cleanup_plan["cleanup_paths"]
            or receipt["post_scan"].get("preservation_evidences") != proofs
            or receipt["post_scan"].get("blockers") != []
            or receipt["post_scan"].get("cleanup_paths") != []
        ):
            raise ControllerError("PRESERVATION_COVERAGE_INVALID", "multiple cleanup is incomplete")
        _preservation_inputs(
            Path(proofs[0]["evidence"]["receipt_path"]),
            proofs[0]["evidence"]["receipt_sha256"],
            tuple(Path(row["evidence"]["receipt_path"]) for row in proofs[1:]),
            tuple(row["evidence"]["receipt_sha256"] for row in proofs[1:]),
        )
        for row in proofs:
            relative, evidence = row["source_relative"], row["evidence"]
            if any(
                PurePosixPath(relative) in PurePosixPath(other).parents
                for other in paths
                if other != relative
            ):
                raise ControllerError("PRESERVATION_COVERAGE_INVALID", "preserved sources overlap")
            resource = next(r for r in cleanup_plan["resources"] if r["path"] == relative)
            verified, preservation_plan = coverage(
                Path(evidence["receipt_path"]),
                evidence["receipt_sha256"],
                identity=bound,
                verify_source=False,
            )
            plans.append(preservation_plan)
            if (
                verified != evidence
                or _preservation_source_relative(preservation_plan, item.path) != relative
                or os.path.lexists(item.path / relative)
                or resource.get("classification") != "exact-preserved"
                or resource.get("cleanup_action") != "remove-preserved-tree"
                or resource.get("evidence") != evidence
            ):
                raise ControllerError(
                    "PRESERVATION_COVERAGE_INVALID", "multiple preservation generation changed"
                )
        return plans
    except (OSError, ValueError, TypeError, KeyError, StopIteration) as exc:
        raise ControllerError(
            "PRESERVATION_COVERAGE_INVALID", "multiple preservation cleanup evidence is invalid"
        ) from exc


def _validate_release_core(
    repository: RepositoryInventory,
    snapshot: MetadataSnapshot,
    item: WorktreeGitState,
    record: MetadataRecord,
    *,
    store: MetadataStore,
    owner_release: str | None,
    strict_identity: bool,
    allow_registered_venv: bool,
    publication_receipt: PublicationReceipt | None = None,
    release_intent: ReleaseIntent | None = None,
    cleanup_recovery: dict[str, Any] | None = None,
    allow_retained_cleanup: bool = False,
    cleanup_transaction_id: str | None = None,
    allow_pending_typed_no_action: bool = False,
) -> None:
    if item.canonical:
        raise ControllerError("BLOCK_CANONICAL", "canonical can never be released")
    if item.detached:
        raise ControllerError("DETACHED_WORKTREE", "detached worktree release is refused")
    if item.locked or item.prunable:
        raise ControllerError("BLOCK_DRIFT", "locked or prunable worktree requires review")
    if not item.clean:
        raise ControllerError("DIRTY_WORKTREE", "release requires a clean worktree")
    retained_cleanup = allow_retained_cleanup and record.lifecycle == "RETAINED" and record.role in {"feature", "audit"}
    if record.lifecycle not in RELEASABLE_STATES and not retained_cleanup:
        raise ControllerError(
            "INVALID_LIFECYCLE", f"lifecycle {record.lifecycle} is not releasable"
        )
    publication_auto_release = (
        record.lifecycle == "PUBLISHED"
        and publication_receipt is not None
        and publication_receipt.status == "REMOTE_VERIFIED"
        and _publication_receipt_lease_released(snapshot, publication_receipt)
        and publication_receipt.allocation_id == record.allocation_id
        and publication_receipt.candidate_oid == item.head
        and publication_receipt.finalizer_record_id == record.finalizer_record_id
    )
    intent_auto_release = release_intent is not None
    if release_intent is not None:
        _validate_release_intent(repository, snapshot, item, record, release_intent)
    if (
        not publication_auto_release
        and not intent_auto_release
        and (owner_release is None or owner_release.strip() != record.owner)
    ):
        raise ControllerError(
            "ACTIVE_OWNER", "release requires an explicit decision from the recorded owner"
        )
    if not record.authority_key.strip():
        raise ControllerError("AUTHORITY_MISMATCH", "release authority is missing")
    if record.external_bindings:
        raise ControllerError(
            "EXTERNAL_BINDING", "registered external/runtime bindings block release"
        )
    from .source_cleanup import verify_completed

    archived_source = verify_completed(store, item, record)
    if archived_source:
        if record.allocation_id in snapshot.writer_leases:
            raise ControllerError(
                "WRITER_LEASE_PRESENT", "writer lease blocks archived source release"
            )
        blockers = _typed_runtime_blockers(_typed_runtime_evidence(item, record, snapshot))
        if blockers:
            raise ControllerError(blockers[0]["code"], blockers[0]["message"])
    from .preserved_cleanup import cleanup_receipts

    if record.lifecycle == "NO_CHANGE_COMPLETED" and not record.history:
        raise ControllerError("NO_CHANGE_CLOSEOUT_INVALID", "no-change completion history is missing")
    if record.lifecycle == "ARCHIVED":
        from .delivered_closeout import verify_archive_receipt

        archived = verify_archive_receipt(store, item, record, snapshot.repository_id)
        cleanup_refs = archived["plan"].get("preservation_cleanup_receipts", [])
    elif record.lifecycle == "NO_CHANGE_COMPLETED" and record.history[-1].get("no_change_completion_ref"):
        from .delivered_closeout import verify_no_change_cleanup

        cleanup_refs = verify_no_change_cleanup(
            store, item, record, snapshot.repository_id, cleanup_recovery=cleanup_recovery,
            cleanup_transaction_id=cleanup_transaction_id,
            allow_pending_typed_no_action=allow_pending_typed_no_action,
        )
    else:
        cleanup_refs = cleanup_receipts(
            store, item, record, snapshot.repository_id,
            cleanup_recovery=cleanup_recovery,
            cleanup_transaction_id=cleanup_transaction_id,
            allow_pending_typed_no_action=allow_pending_typed_no_action,
            include_no_action_refs=True,
        )
    if cleanup_refs:
        if record.allocation_id in snapshot.writer_leases:
            raise ControllerError("WRITER_LEASE_PRESENT", "writer lease blocks typed cleanup release")
        runtime_blockers = _typed_runtime_blockers(_typed_runtime_evidence(item, record, snapshot))
        if runtime_blockers:
            raise ControllerError(runtime_blockers[0]["code"], runtime_blockers[0]["message"])
    if record.lifecycle == "RECOVERY_REQUIRED":
        if strict_identity:
            raise ControllerError(
                "INVALID_LIFECYCLE", "reproducible .venv teardown is not a recovery operation"
            )
        if (
            not record.recovery_allocation_id
            or not record.recovery_ref
            or not record.quarantine_observed_commit
        ):
            raise ControllerError(
                "MISSING_RECOVERY_EVIDENCE",
                "recovery-required release needs externalization lineage evidence",
            )
        if item.head != record.quarantine_observed_commit:
            raise ControllerError(
                "HEAD_MISMATCH", "quarantined source HEAD changed after externalization"
            )
        recovery = snapshot.records.get(record.recovery_allocation_id)
        if (
            recovery is None
            or recovery.role not in RECOVERY_ROLES
            or record.allocation_id not in recovery.source_allocations
        ):
            raise ControllerError(
                "RECOVERY_LINEAGE_MISMATCH", "recovery allocation does not bind this source"
            )
        recovery_oid = resolve_commit(repository.canonical_path, record.recovery_ref)
        if not is_ancestor(
            repository.canonical_path, record.quarantine_observed_commit, recovery_oid
        ):
            raise ControllerError(
                "RECOVERY_LINEAGE_MISMATCH",
                "recovery ref does not retain the quarantined observed HEAD",
            )
    elif record.lifecycle == "RELEASE_READY":
        from .delivered_closeout import validate_release

        if release_intent is not None:
            raise ControllerError("INVALID_RELEASE_INTENT", "delivered closeout requires explicit owner release")
        validate_release(repository, snapshot, item, record)
    elif record.lifecycle == "ARCHIVED":
        from .delivered_closeout import validate_archive_release

        if release_intent is not None:
            raise ControllerError("INVALID_RELEASE_INTENT", "archive requires explicit owner release")
        validate_archive_release(repository, snapshot, item, record)
    elif record.lifecycle == "PRESERVED" and record.handoff_commit is None:
        _verify_preserved_closeout(store, item, record, archived_source, snapshot.repository_id)
    elif retained_cleanup:
        from .delivered_closeout import _eligible

        _eligible(
            repository, snapshot, item, record, state="RETAINED",
            require_publication=False, for_typed_cleanup=True,
        )
    elif record.lifecycle in EVIDENCE_EXEMPT_RELEASE_STATES:
        # These terminal states carry their own completion receipt instead of a Finalizer
        # commit, because the task produced no commit to finalize. Cleanliness, ownership,
        # binding and resource gates above still apply unchanged.
        if release_intent is not None:
            raise ControllerError(
                "INVALID_LIFECYCLE",
                "release intents apply to handoff evidence, not no-change completion",
            )
    else:
        if not record.handoff_commit or not record.finalizer_record_id:
            raise ControllerError("MISSING_EVIDENCE", "handoff and Finalizer evidence are required")
        handoff_oid = resolve_commit(item.path, record.handoff_commit)
        if handoff_oid != item.head:
            raise ControllerError("HEAD_MISMATCH", "worktree HEAD drifted after handoff")
        if strict_identity:
            identities = (
                record.task_commit,
                record.handoff_commit,
                record.finalizer_commit,
            )
            if any(identity is None for identity in identities):
                raise ControllerError(
                    "IDENTITY_MISMATCH",
                    "task, handoff, and Finalizer commit identities are required",
                )
            if any(identity != item.head for identity in identities):
                raise ControllerError(
                    "IDENTITY_MISMATCH",
                    "task, handoff, Finalizer, and worktree HEAD identities differ",
                )
        if release_intent is not None:
            if item.branch is None:
                raise ControllerError("MISSING_RETAINED_REF", "release requires a retained branch")
            branch_oid = resolve_commit(repository.canonical_path, f"refs/heads/{item.branch}")
            if branch_oid != item.head:
                raise ControllerError("MISSING_RETAINED_REF", "retained branch identity differs")
    for resource in record.disposable_resources:
        resource_path = item.path / resource["path"]
        if (
            resource["state"] == "registered"
            and resource_path.exists()
            and not (allow_registered_venv and resource["path"] == ".venv")
        ):
            raise ControllerError(
                "DISPOSABLE_RESOURCE_PRESENT",
                f"registered disposable resource requires controlled cleanup: {resource['path']}",
            )
        if resource["state"] == "cleaned" and resource_path.exists():
            raise ControllerError(
                "DISPOSABLE_RESOURCE_DRIFT",
                f"cleaned disposable resource reappeared: {resource['path']}",
            )


def _ignored_path_within(path: str, roots: tuple[str, ...]) -> bool:
    parts = PurePosixPath(path).parts
    return any(
        parts[: len(PurePosixPath(root).parts)] == PurePosixPath(root).parts for root in roots
    )


def _validate_release_environment(
    repository: RepositoryInventory,
    item: WorktreeGitState,
    *,
    registered_resource_paths: tuple[str, ...] = (),
) -> None:
    unsafe_ignored = unsafe_ignored_paths(ignored_paths(item.path))
    outside = tuple(
        path for path in unsafe_ignored if not _ignored_path_within(path, registered_resource_paths)
    )
    if outside:
        raise ControllerError(
            "IGNORED_CONTENT",
            f"unclassified ignored content blocks release: {outside[0]}",
        )
    excluded_environments = tuple(item.path / path for path in registered_resource_paths)
    references = editable_references(
        repository, item.path, exclude_environments=excluded_environments
    )
    if references:
        raise ControllerError(
            "STALE_EDITABLE_REFERENCE",
            f"editable environment reference blocks release: {references[0]['file']}",
        )
    _validate_runtime_clear(item)


def _release_resource_actions(
    repository: RepositoryInventory,
    item: WorktreeGitState,
    record: MetadataRecord,
) -> tuple[dict[str, Any], ...]:
    actions: list[dict[str, Any]] = []
    for resource in record.disposable_resources:
        resource_path = item.path / str(resource["path"])
        if resource["state"] == "cleaned":
            if resource_path.exists() or resource_path.is_symlink():
                raise ControllerError(
                    "DISPOSABLE_RESOURCE_DRIFT",
                    f"cleaned disposable resource reappeared: {resource['path']}",
                )
            continue
        if not resource_path.exists() or resource_path.is_symlink():
            raise ControllerError(
                "DISPOSABLE_RESOURCE_DRIFT",
                f"registered disposable resource is missing or changed type: {resource['path']}",
            )
        if resource.get("teardown_policy") != "on-release":
            raise ControllerError(
                "DISPOSABLE_RESOURCE_PRESENT",
                f"registered resource requires manual cleanup: {resource['path']}",
            )
        if resource.get("reproducible") is not True:
            raise ControllerError(
                "NON_REPRODUCIBLE_RESOURCE",
                f"automatic teardown requires reproducible resource: {resource['path']}",
            )
        if resource["kind"] != "python-venv" or resource["path"] != ".venv":
            raise ControllerError(
                "INVALID_RESOURCE", f"unsupported release resource: {resource['path']}"
            )
        evidence = _teardown_preflight(repository, item)
        actions.append(
            {
                "resource_id": resource["resource_id"],
                "path": resource["path"],
                "kind": resource["kind"],
                "creator": resource["creator"],
                "teardown_policy": resource["teardown_policy"],
                "action": "teardown",
                "evidence": evidence,
            }
        )
    registered_paths = tuple(str(action["path"]) for action in actions)
    _validate_release_environment(
        repository,
        item,
        registered_resource_paths=registered_paths,
    )
    return tuple(actions)


def _release_plan_id(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def governance_release_plan_id(plan: ReleasePlan) -> str:
    """Stable review identity for an otherwise timestamped ReleasePlan."""
    identity = asdict(plan)
    identity.pop("plan_id")
    identity.pop("created_at")
    return _release_plan_id(identity)


def _build_release_plan_locked(
    repository: RepositoryInventory,
    snapshot: MetadataSnapshot,
    item: WorktreeGitState,
    record: MetadataRecord,
    *,
    store: MetadataStore,
    expected_record_version: int,
    owner_release: str | None,
    publication_receipt: PublicationReceipt | None,
    release_intent: ReleaseIntent | None,
    cleanup_transaction_id: str | None = None,
) -> ReleasePlan:
    _cas(record, expected_record_version)
    _validate_release_core(
        repository,
        snapshot,
        item,
        record,
        store=store,
        owner_release=owner_release,
        strict_identity=record.lifecycle
        not in ({"RECOVERY_REQUIRED"} | EVIDENCE_EXEMPT_RELEASE_STATES),
        allow_registered_venv=True,
        publication_receipt=publication_receipt,
        release_intent=release_intent,
        cleanup_transaction_id=cleanup_transaction_id,
    )
    resource_actions = _release_resource_actions(repository, item, record)
    if snapshot.repository_id is None:
        raise ControllerError("BLOCK_DRIFT", "repository metadata is missing")
    if item.branch is None:
        raise ControllerError("MISSING_RETAINED_REF", "release requires a retained branch")
    trigger = (
        release_intent.trigger
        if release_intent is not None
        else "publication-lease-complete"
        if publication_receipt is not None
        else "owner-release"
    )
    created_at = now_iso()
    identity = {
        "schema_version": RELEASE_PLAN_SCHEMA_VERSION,
        "repository_id": snapshot.repository_id,
        "allocation_id": record.allocation_id,
        "release_intent_id": release_intent.intent_id if release_intent else None,
        "target_path": str(item.path),
        "branch": item.branch,
        "expected_head": item.head,
        "expected_record_version": record.record_version,
        "owner": record.owner,
        "authority_key": record.authority_key,
        "role": record.role,
        "lifecycle": record.lifecycle,
        "trigger": trigger,
        "publication_receipt_id": (publication_receipt.receipt_id if publication_receipt else None),
        "resource_actions": resource_actions,
        "created_at": created_at,
    }
    return ReleasePlan(
        **identity,
        plan_id=_release_plan_id(identity),
    )


def build_release_plan(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner_release: str | None,
    publication_receipt_id: str | None = None,
    release_intent_id: str | None = None,
) -> ReleasePlan:
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    with store.lock(exclusive=False):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        publication_receipt = (
            snapshot.publication_receipts.get(publication_receipt_id)
            if publication_receipt_id is not None
            else None
        )
        release_intent = (
            snapshot.release_intents.get(release_intent_id)
            if release_intent_id is not None
            else None
        )
        if publication_receipt_id is not None and publication_receipt is None:
            raise ControllerError("MISSING_PUBLICATION_RECEIPT", "publication receipt is missing")
        if release_intent_id is not None and release_intent is None:
            raise ControllerError("MISSING_RELEASE_INTENT", "release intent is missing")
        return _build_release_plan_locked(
            repository,
            snapshot,
            item,
            record,
            store=store,
            expected_record_version=expected_record_version,
            owner_release=owner_release,
            publication_receipt=publication_receipt,
            release_intent=release_intent,
        )


def _venv_ignored_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(parts) and parts[0] == ".venv"


def _validate_runtime_clear(item: WorktreeGitState, *, strict: bool = False) -> None:
    references = (
        runtime_references(item.path, strict=True) if strict else runtime_references(item.path)
    )
    if references:
        reference = references[0]
        raise ControllerError(
            "RUNTIME_REFERENCE",
            f"runtime {reference['kind']} reference from pid {reference['pid']} blocks teardown",
        )


def _teardown_preflight(repository: RepositoryInventory, item: WorktreeGitState) -> dict[str, Any]:
    if tracked_paths(item.path, ".venv"):
        raise ControllerError("TRACKED_RESOURCE", "tracked .venv content cannot be torn down")
    if not path_is_ignored(item.path, ".venv"):
        raise ControllerError("RESOURCE_NOT_IGNORED", "worktree .venv must be Git-ignored")
    project_rel, lock_rel = discover_python_project(item.path, venv_relative_path=".venv")
    for project_path in (lock_rel, project_rel):
        if tracked_paths(item.path, project_path) != (project_path,):
            raise ControllerError(
                "PROJECT_IDENTITY_MISMATCH",
                f"reproducible teardown requires tracked {project_path}",
            )
    unsafe_ignored = unsafe_ignored_paths(ignored_paths(item.path))
    outside = tuple(path for path in unsafe_ignored if not _venv_ignored_path(path))
    if outside:
        raise ControllerError(
            "IGNORED_CONTENT",
            f"unclassified ignored content outside .venv blocks release: {outside[0]}",
        )
    venv = item.path / ".venv"
    evidence = reproducible_venv_evidence(item.path)
    references = editable_references(repository, item.path, exclude_environments=(venv,))
    if references:
        raise ControllerError(
            "STALE_EDITABLE_REFERENCE",
            f"another environment references this checkout: {references[0]['file']}",
        )
    _validate_runtime_clear(item)
    return evidence


def _release_transaction(
    store: MetadataStore,
    *,
    target_path: str,
    allocation_id: str | None = None,
    removed_release_recovery: bool = False,
) -> dict[str, Any] | None:
    if not store.transactions_dir.exists():
        return None
    from .transaction_scope import conflicts

    transactions = [
        {**transaction, "_path_stem": transaction["transaction_id"]}
        for transaction in conflicts(store, allocation=allocation_id, target=Path(target_path))
    ]
    matches = [
        transaction
        for transaction in transactions
        if transaction.get("command") == "release"
        and transaction.get("target_path") == target_path
        and (allocation_id is None or transaction.get("allocation_id") == allocation_id)
    ]
    if removed_release_recovery and len(matches) == 1 and len(transactions) == 2:
        from .transaction_scope import acquire_precedes_removed_release

        other, = (row for row in transactions if row is not matches[0])
        if other.get("command") == "acquire" and acquire_precedes_removed_release(
            store, other["transaction_id"], matches[0]
        ):
            transactions = matches
    if len(matches) > 1 or len(matches) != len(transactions):
        raise ControllerError(
            "INTERRUPTED_TRANSACTION", "another controller transaction requires recovery"
        )
    if not matches:
        return None
    transaction = matches[0]
    transaction_id = transaction.pop("_path_stem")
    if transaction.get("transaction_id") != transaction_id:
        raise ControllerError("INVALID_TRANSACTION", "transaction filename does not match")
    return transaction


def _receipt_payload(
    *,
    transaction_id: str,
    repository_id: str,
    item: WorktreeGitState,
    record: MetadataRecord,
    evidence: dict[str, Any],
    created_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "receipt_id": transaction_id,
        "transaction_id": transaction_id,
        "repository_id": repository_id,
        "allocation_id": record.allocation_id,
        "owner": record.owner,
        "authority_key": record.authority_key,
        "worktree_path": str(item.path),
        "worktree_head": item.head,
        **evidence,
        "result": "prepared",
        "created_at": created_at,
        "updated_at": now_iso(),
    }


def _updated_receipt(receipt: dict[str, Any], result: str) -> dict[str, Any]:
    updated = dict(receipt)
    updated["result"] = result
    updated["updated_at"] = now_iso()
    return updated


def _assert_receipt_matches(
    receipt: dict[str, Any],
    item: WorktreeGitState,
    record: MetadataRecord,
    evidence: dict[str, Any],
) -> None:
    if (
        receipt["worktree_path"] != str(item.path)
        or receipt["worktree_head"] != item.head
        or receipt["allocation_id"] != record.allocation_id
        or receipt["owner"] != record.owner
        or receipt["authority_key"] != record.authority_key
        or any(receipt.get(key) != value for key, value in evidence.items())
    ):
        raise ControllerError(
            "TEARDOWN_IDENTITY_MISMATCH", "reproducible .venv changed after teardown preflight"
        )


def _assert_release_plan_request(
    plan: ReleasePlan,
    *,
    target_path: str,
    expected_record_version: int,
    owner_release: str | None,
    publication_receipt_id: str | None,
    release_intent_id: str | None,
) -> None:
    if (
        plan.target_path != target_path
        or plan.expected_record_version != expected_record_version
        or plan.publication_receipt_id != publication_receipt_id
        or plan.release_intent_id != release_intent_id
    ):
        raise ControllerError(
            "RELEASE_RECOVERY_MISMATCH", "release request differs from the persisted plan"
        )
    if (
        plan.release_intent_id is None
        and plan.publication_receipt_id is None
        and (owner_release is None or owner_release.strip() != plan.owner)
    ):
        raise ControllerError(
            "RELEASE_RECOVERY_MISMATCH", "release owner differs from the persisted plan"
        )


def _validate_persisted_plan_live(
    repository: RepositoryInventory,
    snapshot: MetadataSnapshot,
    item: WorktreeGitState,
    record: MetadataRecord,
    plan: ReleasePlan,
    *,
    store: MetadataStore,
    owner_release: str | None,
    publication_receipt: PublicationReceipt | None,
    release_intent: ReleaseIntent | None,
) -> None:
    _cas(record, plan.expected_record_version)
    if not all(
        (
            plan.repository_id == snapshot.repository_id,
            plan.allocation_id == record.allocation_id,
            plan.target_path == str(item.path),
            plan.branch == item.branch,
            plan.expected_head == item.head,
            plan.owner == record.owner,
            plan.authority_key == record.authority_key,
            plan.role == record.role,
            plan.lifecycle == record.lifecycle,
        )
    ):
        raise ControllerError("RELEASE_PLAN_STALE", "persisted release plan identity changed")
    _validate_release_core(
        repository,
        snapshot,
        item,
        record,
        store=store,
        owner_release=owner_release,
        strict_identity=record.lifecycle
        not in ({"RECOVERY_REQUIRED"} | EVIDENCE_EXEMPT_RELEASE_STATES),
        allow_registered_venv=True,
        publication_receipt=publication_receipt,
        release_intent=release_intent,
    )
    registered = {
        (resource["resource_id"], resource["path"], resource["kind"])
        for resource in record.disposable_resources
        if resource["state"] == "registered"
    }
    planned = {
        (action["resource_id"], action["path"], action["kind"]) for action in plan.resource_actions
    }
    if registered != planned:
        raise ControllerError("RELEASE_PLAN_STALE", "disposable resource contract changed")


def _validated_teardown_transaction(
    store: MetadataStore,
    *,
    journal: dict[str, Any],
    repository_id: str,
    item: WorktreeGitState,
    record: MetadataRecord,
    plan: ReleasePlan,
) -> tuple[str, str, dict[str, Any]]:
    transaction_id = journal.get("transaction_id")
    if not isinstance(transaction_id, str):
        raise ControllerError(
            "TEARDOWN_RECOVERY_MISMATCH", "teardown transaction identity is missing"
        )
    if (
        journal.get("command") != "release"
        or journal.get("target_path") != str(item.path)
        or journal.get("allocation_id") != record.allocation_id
        or journal.get("teardown_receipt_id") != transaction_id
        or journal.get("release_plan_id") != plan.plan_id
        or journal.get("repository_id") != repository_id
        or journal.get("branch") != item.branch
        or journal.get("expected_head") != item.head
        or journal.get("expected_record_version") != record.record_version
    ):
        raise ControllerError(
            "TEARDOWN_RECOVERY_MISMATCH",
            "teardown transaction no longer matches worktree identity",
        )
    receipt = store.load_teardown_receipt(transaction_id)
    if (
        receipt.get("receipt_id") != transaction_id
        or receipt.get("transaction_id") != transaction_id
        or receipt.get("repository_id") != repository_id
        or receipt.get("worktree_path") != str(item.path)
        or receipt.get("worktree_head") != item.head
        or receipt.get("allocation_id") != record.allocation_id
        or receipt.get("owner") != record.owner
        or receipt.get("authority_key") != record.authority_key
    ):
        raise ControllerError(
            "TEARDOWN_RECOVERY_MISMATCH",
            "teardown receipt no longer matches worktree authority",
        )
    phase = journal.get("phase")
    expected_receipt_result = (
        TEARDOWN_TRANSACTION_RESULTS.get(phase) if isinstance(phase, str) else None
    )
    if expected_receipt_result is None or receipt.get("result") != expected_receipt_result:
        raise ControllerError(
            "TEARDOWN_RECOVERY_MISMATCH",
            "teardown transaction phase and receipt result differ",
        )
    return transaction_id, phase, receipt


def _validate_post_teardown_release(
    repository_path: Path,
    store: MetadataStore,
    *,
    target: Path,
    plan: ReleasePlan,
    phase: str,
    owner_release: str | None,
    publication_receipt: PublicationReceipt | None,
    release_intent: ReleaseIntent | None,
) -> tuple[RepositoryInventory, MetadataSnapshot, WorktreeGitState, MetadataRecord]:
    if phase not in POST_TEARDOWN_RECOVERY_PHASES:
        raise ControllerError(
            "INTERRUPTED_TRANSACTION",
            f"release transaction phase {phase} requires controlled teardown recovery",
        )
    if (target / ".venv").exists() or (target / ".venv").is_symlink():
        raise ControllerError("TEARDOWN_RECOVERY_MISMATCH", "removed .venv unexpectedly reappeared")
    repository = inventory(repository_path)
    snapshot = store._load_unlocked()
    item, record = _managed_record(repository, snapshot, target)
    _validate_persisted_plan_live(
        repository,
        snapshot,
        item,
        record,
        plan,
        store=store,
        owner_release=owner_release,
        publication_receipt=publication_receipt,
        release_intent=release_intent,
    )
    if item.branch is None or (
        resolve_commit(repository.canonical_path, f"refs/heads/{item.branch}") != plan.expected_head
    ):
        raise ControllerError("MISSING_RETAINED_REF", "retained release branch identity differs")
    _validate_release_environment(repository, item)
    return repository, snapshot, item, record


def _release_registered_target(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner_release: str | None,
    teardown_reproducible_venv: bool = False,
    dry_run: bool = False,
    publication_receipt_id: str | None = None,
    release_intent_id: str | None = None,
    expected_governance_plan_id: str | None = None,
    expected_allocation_id: str | None = None,
) -> dict[str, Any]:
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    plan: ReleasePlan | None = None
    receipt: dict[str, Any] | None = None
    release_receipt: ReleaseReceipt | None = None
    updated: MetadataRecord | None = None
    recovery_dry_run: dict[str, str] | None = None
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        if expected_allocation_id is not None and record.allocation_id != expected_allocation_id:
            raise ControllerError("IDENTITY_MISMATCH", "release allocation differs from the selected task")
        target_path = str(item.path)
        publication_receipt = (
            snapshot.publication_receipts.get(publication_receipt_id)
            if publication_receipt_id is not None
            else None
        )
        if publication_receipt_id is not None and publication_receipt is None:
            raise ControllerError("MISSING_PUBLICATION_RECEIPT", "publication receipt is missing")
        release_intent = (
            snapshot.release_intents.get(release_intent_id)
            if release_intent_id is not None
            else None
        )
        if release_intent_id is not None and release_intent is None:
            raise ControllerError("MISSING_RELEASE_INTENT", "release intent is missing")
        existing_transaction = _release_transaction(
            store,
            target_path=target_path,
            allocation_id=record.allocation_id,
        )
        if expected_governance_plan_id is not None:
            if existing_transaction is not None:
                raise ControllerError("GC_PLAN_DRIFT", "an interrupted release requires normal release recovery")
            if record.allocation_id in snapshot.writer_leases or (
                snapshot.publication_lease is not None
                and snapshot.publication_lease.allocation_id == record.allocation_id
            ):
                raise ControllerError("GC_PLAN_DRIFT", "lease authority changed since GC review")
        if existing_transaction is None:
            plan = _build_release_plan_locked(
                repository,
                snapshot,
                item,
                record,
                store=store,
                expected_record_version=expected_record_version,
                owner_release=owner_release,
                publication_receipt=publication_receipt,
                release_intent=release_intent,
            )
            if (
                expected_governance_plan_id is not None
                and governance_release_plan_id(plan) != expected_governance_plan_id
            ):
                raise ControllerError("GC_PLAN_DRIFT", "release plan changed since GC review")
        else:
            plan_id = existing_transaction.get("release_plan_id")
            if not isinstance(plan_id, str):
                raise ControllerError(
                    "LEGACY_TRANSACTION_REQUIRES_RECOVERY",
                    "interrupted release predates persisted ReleasePlan support",
                )
            plan = snapshot.release_plans.get(plan_id)
            if plan is None:
                raise ControllerError("BLOCK_DRIFT", "persisted release plan disappeared")
            _assert_release_plan_request(
                plan,
                target_path=target_path,
                expected_record_version=expected_record_version,
                owner_release=owner_release,
                publication_receipt_id=publication_receipt_id,
                release_intent_id=release_intent_id,
            )
            _validate_persisted_plan_live(
                repository,
                snapshot,
                item,
                record,
                plan,
                store=store,
                owner_release=owner_release,
                publication_receipt=publication_receipt,
                release_intent=release_intent,
            )
        has_registered_teardown = bool(plan.resource_actions)
        if teardown_reproducible_venv and not has_registered_teardown:
            raise ControllerError(
                "UNREGISTERED_RESOURCE",
                "legacy teardown flag cannot classify an unregistered .venv",
            )
        repository_id = snapshot.repository_id
        if repository_id is None:
            raise ControllerError("BLOCK_DRIFT", "repository metadata is missing")
        if dry_run:
            if existing_transaction is not None:
                if not has_registered_teardown:
                    raise ControllerError(
                        "INTERRUPTED_TRANSACTION", "release transaction requires recovery"
                    )
                transaction_id, phase, receipt = _validated_teardown_transaction(
                    store,
                    journal=existing_transaction,
                    repository_id=repository_id,
                    item=item,
                    record=record,
                    plan=plan,
                )
                repository, snapshot, item, record = _validate_post_teardown_release(
                    repository_path,
                    store,
                    target=target,
                    plan=plan,
                    phase=phase,
                    owner_release=owner_release,
                    publication_receipt=publication_receipt,
                    release_intent=release_intent,
                )
                recovery_dry_run = {
                    "transaction_id": transaction_id,
                    "phase": phase,
                    "teardown_receipt_id": transaction_id,
                    "next_phase": "PREPARED" if phase == "VENV_REMOVED" else "WORKTREE_REMOVED",
                }
        else:
            if has_registered_teardown:
                if existing_transaction is None:
                    store.write_release_plan(plan)
                    evidence = dict(plan.resource_actions[0]["evidence"])
                    transaction_id = str(uuid.uuid4())
                    transaction_created = now_iso()
                    journal = _transaction(
                        transaction_id=transaction_id,
                        repository_id=repository_id,
                        command="release",
                        phase="TEARDOWN_PREPARED",
                        target_path=item.path,
                        allocation_id=record.allocation_id,
                        branch=item.branch,
                        expected_head=item.head,
                        expected_record_version=record.record_version,
                        created_at=transaction_created,
                    )
                    journal["teardown_receipt_id"] = transaction_id
                    journal["release_plan_id"] = plan.plan_id
                    receipt = _receipt_payload(
                        transaction_id=transaction_id,
                        repository_id=repository_id,
                        item=item,
                        record=record,
                        evidence=evidence,
                        created_at=transaction_created,
                    )
                    store.write_teardown_receipt(transaction_id, receipt)
                    store.write_transaction(transaction_id, journal)
                    _crash_point("release_teardown_prepared")
                else:
                    journal = existing_transaction
                transaction_id, phase, receipt = _validated_teardown_transaction(
                    store,
                    journal=journal,
                    repository_id=repository_id,
                    item=item,
                    record=record,
                    plan=plan,
                )
                venv = item.path / ".venv"
                if phase == "TEARDOWN_PREPARED":
                    if not venv.exists() or venv.is_symlink():
                        raise ControllerError(
                            "TEARDOWN_RECOVERY_MISMATCH",
                            "prepared reproducible .venv disappeared or changed type",
                        )
                    evidence = _teardown_preflight(repository, item)
                    _assert_receipt_matches(receipt, item, record, evidence)
                    receipt = _updated_receipt(receipt, "removal_started")
                    store.write_teardown_receipt(transaction_id, receipt)
                    journal["phase"] = "VENV_REMOVAL_STARTED"
                    journal["updated_at"] = now_iso()
                    store.write_transaction(transaction_id, journal)
                    _crash_point("release_venv_removal_started")
                    phase = "VENV_REMOVAL_STARTED"
                if phase == "VENV_REMOVAL_STARTED":
                    if venv.exists() or venv.is_symlink():
                        unsafe_ignored = unsafe_ignored_paths(ignored_paths(item.path))
                        outside = tuple(
                            path for path in unsafe_ignored if not _venv_ignored_path(path)
                        )
                        references = editable_references(
                            repository, item.path, exclude_environments=(venv,)
                        )
                        if outside:
                            raise ControllerError(
                                "IGNORED_CONTENT",
                                f"unclassified ignored content outside .venv blocks release: "
                                f"{outside[0]}",
                            )
                        if references:
                            raise ControllerError(
                                "STALE_EDITABLE_REFERENCE",
                                f"another environment references this checkout: "
                                f"{references[0]['file']}",
                            )
                        _validate_runtime_clear(item)
                        try:
                            venv_stat = venv.stat(follow_symlinks=False)
                        except OSError as exc:
                            raise ControllerError(
                                "RESOURCE_IDENTITY_MISMATCH",
                                "cannot stat worktree .venv before resumed removal",
                            ) from exc
                        if (
                            venv.is_symlink()
                            or not venv.is_dir()
                            or venv.is_mount()
                            or venv.parent.resolve(strict=True) != item.path.resolve(strict=True)
                            or venv_stat.st_dev != receipt["environment_device"]
                            or venv_stat.st_ino != receipt["environment_inode"]
                        ):
                            raise ControllerError(
                                "RESOURCE_IDENTITY_MISMATCH",
                                "worktree .venv identity changed before removal",
                            )
                        try:
                            shutil.rmtree(venv)
                        except OSError as exc:
                            raise ControllerError(
                                "RESOURCE_CLEANUP_FAILED", "reproducible .venv removal failed"
                            ) from exc
                    if venv.exists() or venv.is_symlink():
                        raise ControllerError(
                            "RESOURCE_CLEANUP_FAILED", "reproducible .venv still exists"
                        )
                    receipt = _updated_receipt(receipt, "removed")
                    store.write_teardown_receipt(transaction_id, receipt)
                    journal["phase"] = "VENV_REMOVED"
                    journal["updated_at"] = now_iso()
                    store.write_transaction(transaction_id, journal)
                    _crash_point("release_venv_removed")
                transaction_id, phase, receipt = _validated_teardown_transaction(
                    store,
                    journal=journal,
                    repository_id=repository_id,
                    item=item,
                    record=record,
                    plan=plan,
                )
                repository, snapshot, item, record = _validate_post_teardown_release(
                    repository_path,
                    store,
                    target=target,
                    plan=plan,
                    phase=phase,
                    owner_release=owner_release,
                    publication_receipt=publication_receipt,
                    release_intent=release_intent,
                )
                if journal.get("phase") != "PREPARED":
                    journal["phase"] = "PREPARED"
                    journal["updated_at"] = now_iso()
                    store.write_transaction(transaction_id, journal)
                    _crash_point("release_prepared")
            else:
                _validate_release_environment(repository, item)
                if existing_transaction is None:
                    transaction_id = str(uuid.uuid4())
                    transaction_created = now_iso()
                    store.write_release_plan(plan)
                    journal = _transaction(
                        transaction_id=transaction_id,
                        repository_id=repository_id,
                        command="release",
                        phase="PREPARED",
                        target_path=item.path,
                        allocation_id=record.allocation_id,
                        branch=item.branch,
                        expected_head=item.head,
                        expected_record_version=record.record_version,
                        created_at=transaction_created,
                    )
                    journal["release_plan_id"] = plan.plan_id
                    store.write_transaction(transaction_id, journal)
                    _crash_point("release_prepared")
                else:
                    journal = existing_transaction
                    transaction_id = str(journal.get("transaction_id"))
                    if (
                        journal.get("phase") != "PREPARED"
                        or journal.get("release_plan_id") != plan.plan_id
                        or journal.get("repository_id") != repository_id
                        or journal.get("branch") != item.branch
                        or journal.get("expected_head") != item.head
                        or journal.get("expected_record_version") != record.record_version
                    ):
                        raise ControllerError(
                            "RELEASE_RECOVERY_MISMATCH",
                            "release transaction no longer matches worktree identity",
                        )
            remaining_worktrees = worktree_content_fingerprint(
                repository, exclude_keys=frozenset({item.key})
            )
            existing_refs = public_refs(repository.canonical_path)
            git_write(repository.canonical_path, ("worktree", "remove", str(item.path)))
            journal["phase"] = "WORKTREE_REMOVED"
            journal["updated_at"] = now_iso()
            store.write_transaction(transaction_id, journal)
            _crash_point("release_worktree_removed")
            resources = [dict(resource) for resource in record.disposable_resources]
            planned_resource_ids = {str(action["resource_id"]) for action in plan.resource_actions}
            for resource in resources:
                if resource["resource_id"] in planned_resource_ids:
                    resource["state"] = "cleaned"
            updated = _updated_record(
                record,
                "RELEASED",
                lifecycle="RELEASED",
                owner="unassigned",
                disposable_resources=tuple(resources),
            )
            store.replace_record(updated)
            store.remove_binding(item.key)
            store.remove_writer_lease(record.allocation_id)
            journal["phase"] = "RELEASED_RECORDED"
            journal["updated_at"] = now_iso()
            store.write_transaction(transaction_id, journal)
            _crash_point("release_metadata_written")
            repository_released = inventory(repository_path)
            if worktree_content_fingerprint(repository_released) != remaining_worktrees:
                raise ControllerError("BLOCK_DRIFT", "another worktree changed during release")
            if public_refs(repository.canonical_path) != existing_refs:
                raise ControllerError("BLOCK_DRIFT", "release changed a retained ref")
            if item.branch is not None:
                branch_oid = resolve_commit(repository.canonical_path, f"refs/heads/{item.branch}")
                if branch_oid != item.head:
                    raise ControllerError("BLOCK_DRIFT", "release changed the retained branch")
            if receipt is not None:
                receipt = _updated_receipt(receipt, "released")
                store.write_teardown_receipt(transaction_id, receipt)
            if plan is None:
                raise ControllerError("BLOCK_DRIFT", "release execution lost its plan")
            released_at = now_iso()
            release_receipt = ReleaseReceipt(
                schema_version=RELEASE_RECEIPT_SCHEMA_VERSION,
                receipt_id=transaction_id,
                plan_id=plan.plan_id,
                repository_id=plan.repository_id,
                allocation_id=plan.allocation_id,
                release_intent_id=plan.release_intent_id,
                target_path=plan.target_path,
                branch=plan.branch,
                released_head=plan.expected_head,
                expected_record_version=plan.expected_record_version,
                released_record_version=updated.record_version,
                trigger=plan.trigger,
                resource_receipt_ids=(receipt["receipt_id"],) if receipt is not None else (),
                result="RELEASED",
                released_at=released_at,
            )
            store.write_release_receipt(release_receipt)
            from .resource_admission import release_allocation_budget

            release_allocation_budget(plan.repository_id, plan.allocation_id, release_receipt.receipt_id)
            if release_intent is not None:
                store.replace_release_intent(
                    _updated_release_intent(
                        release_intent,
                        "RELEASE_COMPLETED",
                        status="RELEASED",
                        blocker=None,
                    )
                )
            store.remove_transaction(transaction_id)
    payload = status(repository_path)
    payload["command"] = "release"
    if dry_run:
        if plan is None:
            raise ControllerError("BLOCK_DRIFT", "release dry-run did not produce a plan")
        payload["decision"] = {
            "code": ("RELEASE_RECOVERY_PLAN_READY" if recovery_dry_run else "RELEASE_PLAN_READY"),
            "reason": (
                "interrupted release identity and live guards passed without mutation"
                if recovery_dry_run
                else "all guarded release checks passed without mutation"
            ),
            "allocation_id": record.allocation_id,
            "record_version": record.record_version,
            "branch": item.branch,
            "head": item.head,
            "plan": asdict(plan),
        }
        if recovery_dry_run is not None:
            payload["decision"]["recovery"] = recovery_dry_run
        return payload
    if updated is None:
        raise ControllerError("BLOCK_DRIFT", "release did not produce a metadata transition")
    payload["decision"] = {
        "code": "RELEASED",
        "reason": "linked checkout removed; branch retained",
        "allocation_id": updated.allocation_id,
        "record_version": updated.record_version,
        "branch": item.branch,
        "head": item.head,
        "teardown_receipt_id": receipt["receipt_id"] if receipt is not None else None,
        "teardown": receipt,
        "release_receipt_id": release_receipt.receipt_id if release_receipt else None,
        "release_plan_id": plan.plan_id if plan else None,
        "release_intent_id": plan.release_intent_id if plan else None,
    }
    return payload


def _released_payload(
    repository_path: Path,
    *,
    receipt: ReleaseReceipt,
    plan: ReleasePlan,
    teardown_receipt: dict[str, Any] | None,
    code: str,
    base_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = base_payload if base_payload is not None else status(repository_path)
    payload["command"] = "release"
    payload["decision"] = {
        "code": code,
        "reason": (
            "interrupted guarded release converged from immutable plan evidence"
            if code == "RELEASE_RECOVERED"
            else "the exact guarded release already completed"
        ),
        "allocation_id": receipt.allocation_id,
        "record_version": receipt.released_record_version,
        "branch": receipt.branch,
        "head": receipt.released_head,
        "teardown_receipt_id": (
            teardown_receipt["receipt_id"] if teardown_receipt is not None else None
        ),
        "teardown": teardown_receipt,
        "release_receipt_id": receipt.receipt_id,
        "release_plan_id": receipt.plan_id,
        "release_intent_id": receipt.release_intent_id,
    }
    return payload


def _recover_or_replay_removed_release(
    repository_path: Path,
    *,
    target_path: str,
    expected_record_version: int,
    owner_release: str | None,
    teardown_reproducible_venv: bool,
    dry_run: bool,
    publication_receipt_id: str | None,
    release_intent_id: str | None,
) -> dict[str, Any]:
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    preflight_status = status(repository_path)
    recovered = False
    teardown_receipt: dict[str, Any] | None = None
    with store.lock(exclusive=not dry_run):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        if any(
            str(item.path.resolve(strict=False)) == target_path for item in repository.worktrees
        ):
            raise ControllerError(
                "BLOCK_DRIFT", "release target remains registered but is unavailable"
            )
        transaction = _release_transaction(
            store, target_path=target_path, removed_release_recovery=not dry_run,
        )
        if transaction is None:
            matches = [
                receipt
                for receipt in snapshot.release_receipts.values()
                if receipt.target_path == target_path
                and receipt.expected_record_version == expected_record_version
                and receipt.release_intent_id == release_intent_id
            ]
            matches = [
                receipt
                for receipt in matches
                if (plan := snapshot.release_plans.get(receipt.plan_id)) is not None
                and plan.publication_receipt_id == publication_receipt_id
            ]
            if len(matches) != 1:
                raise ControllerError(
                    "UNREGISTERED_WORKTREE", "target is neither registered nor exactly released"
                )
            release_receipt = matches[0]
            plan = snapshot.release_plans[release_receipt.plan_id]
            _assert_release_plan_request(
                plan,
                target_path=target_path,
                expected_record_version=expected_record_version,
                owner_release=owner_release,
                publication_receipt_id=publication_receipt_id,
                release_intent_id=release_intent_id,
            )
            record = snapshot.records.get(release_receipt.allocation_id)
            if (
                record is None
                or record.lifecycle != "RELEASED"
                or record.record_version != release_receipt.released_record_version
                or release_receipt.allocation_id in snapshot.bindings.values()
                or Path(target_path).exists()
                or Path(target_path).is_symlink()
                or resolve_commit(repository.canonical_path, f"refs/heads/{release_receipt.branch}")
                != release_receipt.released_head
            ):
                raise ControllerError("RELEASE_RECEIPT_DRIFT", "completed release evidence drifted")
            if release_receipt.resource_receipt_ids:
                if len(release_receipt.resource_receipt_ids) != 1:
                    raise ControllerError(
                        "RELEASE_RECEIPT_DRIFT", "resource receipts are ambiguous"
                    )
                teardown_receipt = store.load_teardown_receipt(
                    release_receipt.resource_receipt_ids[0]
                )
                if teardown_receipt["result"] != "released":
                    raise ControllerError(
                        "RELEASE_RECEIPT_DRIFT", "resource teardown receipt is incomplete"
                    )
            if not dry_run:
                from .resource_admission import release_allocation_budget

                release_allocation_budget(plan.repository_id, plan.allocation_id, release_receipt.receipt_id)
            return _released_payload(
                repository_path,
                receipt=release_receipt,
                plan=plan,
                teardown_receipt=teardown_receipt,
                code="ALREADY_RELEASED",
                base_payload=preflight_status,
            )

        if dry_run:
            raise ControllerError(
                "INTERRUPTED_TRANSACTION", "release transaction requires formal recovery"
            )
        plan_id = transaction.get("release_plan_id")
        if not isinstance(plan_id, str):
            raise ControllerError(
                "LEGACY_TRANSACTION_REQUIRES_RECOVERY",
                "interrupted release predates persisted ReleasePlan support",
            )
        plan = snapshot.release_plans.get(plan_id)
        if plan is None:
            raise ControllerError("BLOCK_DRIFT", "persisted release plan disappeared")
        _assert_release_plan_request(
            plan,
            target_path=target_path,
            expected_record_version=expected_record_version,
            owner_release=owner_release,
            publication_receipt_id=publication_receipt_id,
            release_intent_id=release_intent_id,
        )
        if (
            transaction.get("repository_id") != snapshot.repository_id
            or transaction.get("allocation_id") != plan.allocation_id
            or transaction.get("branch") != plan.branch
            or transaction.get("expected_head") != plan.expected_head
            or transaction.get("expected_record_version") != plan.expected_record_version
            or transaction.get("phase")
            not in {
                "PREPARED",
                "WORKTREE_REMOVED",
                "RELEASED_RECORDED",
            }
            or Path(target_path).exists()
            or Path(target_path).is_symlink()
        ):
            raise ControllerError(
                "RELEASE_RECOVERY_MISMATCH", "interrupted release identity is unsafe"
            )
        if (
            resolve_commit(repository.canonical_path, f"refs/heads/{plan.branch}")
            != plan.expected_head
        ):
            raise ControllerError("MISSING_RETAINED_REF", "retained release branch drifted")
        record = snapshot.records.get(plan.allocation_id)
        if record is None:
            raise ControllerError("BLOCK_DRIFT", "release allocation record disappeared")
        binding_keys = [
            key
            for key, allocation_id in snapshot.bindings.items()
            if allocation_id == plan.allocation_id
        ]
        if record.lifecycle != "RELEASED":
            if (
                record.record_version != plan.expected_record_version
                or record.owner != plan.owner
                or record.authority_key != plan.authority_key
                or record.role != plan.role
                or record.lifecycle != plan.lifecycle
                or len(binding_keys) != 1
            ):
                raise ControllerError(
                    "RELEASE_RECOVERY_MISMATCH", "release record or binding identity changed"
                )
            resources = [dict(resource) for resource in record.disposable_resources]
            planned_resource_ids = {str(action["resource_id"]) for action in plan.resource_actions}
            for resource in resources:
                if resource["resource_id"] in planned_resource_ids:
                    resource["state"] = "cleaned"
            updated = _updated_record(
                record,
                "RELEASE_RECOVERED",
                lifecycle="RELEASED",
                owner="unassigned",
                disposable_resources=tuple(resources),
            )
            store.replace_record(updated)
            store.remove_binding(binding_keys[0])
            store.remove_writer_lease(record.allocation_id)
            record = updated
        elif record.record_version != plan.expected_record_version + 1 or binding_keys:
            raise ControllerError(
                "RELEASE_RECOVERY_MISMATCH", "released record does not match persisted plan"
            )

        transaction_id = str(transaction.get("transaction_id"))
        transaction["phase"] = "RELEASED_RECORDED"
        transaction["updated_at"] = now_iso()
        store.write_transaction(transaction_id, transaction)
        if plan.resource_actions:
            if transaction.get("teardown_receipt_id") != transaction_id:
                raise ControllerError(
                    "RELEASE_RECOVERY_MISMATCH", "teardown receipt binding is missing"
                )
            teardown_receipt = store.load_teardown_receipt(transaction_id)
            if teardown_receipt["result"] not in {"removed", "released"}:
                raise ControllerError(
                    "RELEASE_RECOVERY_MISMATCH", "resource teardown did not finish"
                )
            if teardown_receipt["result"] != "released":
                teardown_receipt = _updated_receipt(teardown_receipt, "released")
                store.write_teardown_receipt(transaction_id, teardown_receipt)
        elif teardown_reproducible_venv:
            raise ControllerError(
                "UNREGISTERED_RESOURCE", "persisted plan has no registered resource teardown"
            )

        existing_receipt = snapshot.release_receipts.get(transaction_id)
        if existing_receipt is None:
            release_receipt = ReleaseReceipt(
                schema_version=RELEASE_RECEIPT_SCHEMA_VERSION,
                receipt_id=transaction_id,
                plan_id=plan.plan_id,
                repository_id=plan.repository_id,
                allocation_id=plan.allocation_id,
                release_intent_id=plan.release_intent_id,
                target_path=plan.target_path,
                branch=plan.branch,
                released_head=plan.expected_head,
                expected_record_version=plan.expected_record_version,
                released_record_version=record.record_version,
                trigger=plan.trigger,
                resource_receipt_ids=(transaction_id,) if teardown_receipt is not None else (),
                result="RELEASED",
                released_at=now_iso(),
            )
            store.write_release_receipt(release_receipt)
        else:
            release_receipt = existing_receipt
            if (
                release_receipt.plan_id != plan.plan_id
                or release_receipt.released_record_version != record.record_version
            ):
                raise ControllerError("RELEASE_RECEIPT_DRIFT", "release receipt differs from plan")
        from .resource_admission import release_allocation_budget

        release_allocation_budget(plan.repository_id, plan.allocation_id, release_receipt.receipt_id)
        if plan.release_intent_id is not None:
            current_intent = snapshot.release_intents.get(plan.release_intent_id)
            if current_intent is None:
                raise ControllerError("BLOCK_DRIFT", "release intent disappeared")
            if current_intent.status != "RELEASED":
                store.replace_release_intent(
                    _updated_release_intent(
                        current_intent,
                        "RELEASE_RECOVERED",
                        status="RELEASED",
                        blocker=None,
                    )
                )
        store.remove_transaction(transaction_id)
        recovered = True
    return _released_payload(
        repository_path,
        receipt=release_receipt,
        plan=plan,
        teardown_receipt=teardown_receipt,
        code="RELEASE_RECOVERED" if recovered else "ALREADY_RELEASED",
    )


def release(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner_release: str | None,
    teardown_reproducible_venv: bool = False,
    dry_run: bool = False,
    publication_receipt_id: str | None = None,
    release_intent_id: str | None = None,
    expected_governance_plan_id: str | None = None,
    expected_allocation_id: str | None = None,
) -> dict[str, Any]:
    target_path = str(target.expanduser().resolve(strict=False))
    repository = inventory(repository_path)
    # Keep receipt completion and publication acknowledgement outside the target.
    repository_path = repository.canonical_path
    registered = any(
        str(item.path.resolve(strict=False)) == target_path for item in repository.worktrees
    )
    if registered:
        try:
            payload = _release_registered_target(
                repository_path,
                target=Path(target_path),
                expected_record_version=expected_record_version,
                owner_release=owner_release,
                teardown_reproducible_venv=teardown_reproducible_venv,
                dry_run=dry_run,
                publication_receipt_id=publication_receipt_id,
                release_intent_id=release_intent_id,
                expected_governance_plan_id=expected_governance_plan_id,
                expected_allocation_id=expected_allocation_id,
            )
        except ControllerError as error:
            if error.code not in {"INVALID_TARGET", "UNREGISTERED_WORKTREE"}:
                raise
        else:
            if publication_receipt_id is not None and not dry_run:
                source_trigger = _acknowledge_publication_candidate_release(
                    repository_path,
                    publication_receipt_id=publication_receipt_id,
                )
                refreshed = status(repository_path)
                refreshed["command"] = payload["command"]
                refreshed["decision"] = {
                    **payload["decision"],
                    "source_release_trigger": source_trigger,
                }
                return refreshed
            return payload
    if expected_allocation_id is not None:
        raise ControllerError("TASK_CHECKOUT_MISSING", "selected task checkout is absent; inspect its original lifecycle history")
    if expected_governance_plan_id is not None:
        raise ControllerError("GC_PLAN_DRIFT", "GC target is no longer registered")
    payload = _recover_or_replay_removed_release(
        repository_path,
        target_path=target_path,
        expected_record_version=expected_record_version,
        owner_release=owner_release,
        teardown_reproducible_venv=teardown_reproducible_venv,
        dry_run=dry_run,
        publication_receipt_id=publication_receipt_id,
        release_intent_id=release_intent_id,
    )
    if publication_receipt_id is not None and not dry_run:
        source_trigger = _acknowledge_publication_candidate_release(
            repository_path,
            publication_receipt_id=publication_receipt_id,
        )
        refreshed = status(repository_path)
        refreshed["command"] = payload["command"]
        refreshed["decision"] = {
            **payload["decision"],
            "source_release_trigger": source_trigger,
        }
        return refreshed
    return payload
