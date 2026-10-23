"""Controlled retry of an aborted, released candidate under its original frozen intent."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from .git_backend import inventory, is_ancestor, resolve_commit
from .lifecycle_history import _release_view
from .metadata_codec import integration_intent_to_dict
from .models import ControllerError, IntegrationIntent, MetadataSnapshot, RepositoryInventory
from .policy import load_policy
from .semantics import (
    integration_intent_abandoned,
    integration_prerequisite,
    integration_queue_order,
    unallocated_integration_conflict,
)
from .store import MetadataStore
from .transaction_scope import conflicts


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _source_identity(intent: IntegrationIntent) -> dict[str, Any]:
    fields = (
        "intent_id",
        "task_key",
        "authority_key",
        "module",
        "owner",
        "queue_sequence",
        "enqueued_at",
        "source_allocations",
        "source_handoffs",
        "source_branches",
        "source_finalizer_record_ids",
        "source_snapshot_ids",
        "scope_paths",
        "scope_digest",
    )
    payload = integration_intent_to_dict(intent)
    return {field: payload[field] for field in fields}


def _sources(
    repository: RepositoryInventory,
    snapshot: MetadataSnapshot,
    store: MetadataStore,
    intent: IntegrationIntent,
    *,
    require_idle: bool = True,
) -> dict[str, Any]:
    from .lifecycle import _resolve_integration_sources, _scope_digest

    observed = _resolve_integration_sources(
        repository,
        snapshot,
        intent.source_allocations,
        (),
        intent.source_snapshot_ids,
    )
    expected = (
        intent.source_allocations,
        intent.source_handoffs,
        intent.scope_paths,
        intent.source_branches,
        intent.source_finalizer_record_ids,
        intent.source_snapshot_ids,
    )
    if observed != expected or _scope_digest(intent.scope_paths) != intent.scope_digest:
        raise ControllerError("SOURCE_EVIDENCE_MISMATCH", "retry cannot replace source or scope")
    evidence = {}
    for allocation, branch, head in zip(
        intent.source_allocations,
        intent.source_branches,
        intent.source_handoffs,
        strict=True,
    ):
        record = snapshot.records[allocation]
        if record.lifecycle not in {
            "HANDOFF_READY",
            "PRESERVED",
            "INTEGRATING",
            "PUBLISHED",
            "RELEASED",
        }:
            raise ControllerError("SOURCE_EVIDENCE_MISMATCH", "source handoff is no longer frozen")
        if resolve_commit(repository.canonical_path, f"refs/heads/{branch}") != head:
            raise ControllerError("SOURCE_EVIDENCE_MISMATCH", "retained source branch drifted")
        receipt = next(
            (r for r in snapshot.release_receipts.values() if r.allocation_id == allocation), None
        )
        target = next(
            (
                item.path
                for item in repository.worktrees
                if snapshot.bindings.get(item.key) == allocation
            ),
            None,
        )
        if target is None and receipt is not None:
            target = Path(receipt.target_path)
        if target is None or conflicts(store, allocation=allocation, target=target):
            raise ControllerError("BLOCK_TRANSACTION", "source has unresolved lifecycle effects")
        if require_idle and (allocation in snapshot.writer_leases or record.external_bindings):
            raise ControllerError("SOURCE_IN_USE", "source is leased or externally bound")
        evidence[allocation] = _digest(asdict(record))
    return evidence


def _failure(
    repository: RepositoryInventory,
    snapshot: MetadataSnapshot,
    store: MetadataStore,
    intent: IntegrationIntent,
) -> dict[str, Any]:
    if unallocated_integration_conflict(intent):
        if any(r.intent_id == intent.intent_id for r in snapshot.publication_receipts.values()):
            raise ControllerError("RETRY_ALREADY_PUBLISHED", "published intents cannot retry")
        # An earlier attempt's candidate may remain only as a released record.
        if any(r.role == "integration" and r.task_key == intent.task_key and r.lifecycle != "RELEASED"
               for r in snapshot.records.values()):
            raise ControllerError("RETRY_CANDIDATE_IN_USE", "unallocated failure has a live candidate")
        return {
            "kind": "unallocated_conflict",
            "failure_event": intent.history[-1],
            "failure_reason": intent.blocked_reason,
            "failed_base_oid": resolve_commit(repository.canonical_path, intent.base_main_oid),
        }
    if (
        intent.state != "ATTENTION_REQUIRED"
        or intent.allocation_id is not None
        or not intent.blocked_reason
        or not intent.candidate_oid
        or integration_intent_abandoned(intent)
    ):
        raise ControllerError(
            "RETRY_NOT_ELIGIBLE", "retry requires an aborted, unbound failed intent"
        )
    if any(r.intent_id == intent.intent_id for r in snapshot.publication_receipts.values()):
        raise ControllerError("RETRY_ALREADY_PUBLISHED", "published intents cannot retry")
    if (
        snapshot.publication_lease
        and snapshot.publication_lease.candidate_oid == intent.candidate_oid
    ):
        raise ControllerError("PUBLICATION_LEASE_HELD", "intent still has publication authority")
    matches = []
    for path in sorted(store.completion_receipts_dir.glob("*.json")):
        value = store._read_json(path)
        if (
            value.get("kind") == "integration_abort"
            and value.get("intent_id") == intent.intent_id
            and value.get("candidate_oid") == intent.candidate_oid
        ):
            matches.append((path, value))
    if len(matches) != 1:
        raise ControllerError(
            "RETRY_FAILURE_EVIDENCE_MISSING", "one exact abort receipt is required"
        )
    path, abort = matches[0]
    allocation = abort.get("allocation_id")
    record = snapshot.records.get(allocation)
    if (
        abort.get("repository_id") != snapshot.repository_id
        or abort.get("owner") != intent.owner
        or abort.get("failure_reason") != intent.blocked_reason
        or tuple(abort.get("source_allocations", [])) != intent.source_allocations
        or tuple(abort.get("source_handoffs", [])) != intent.source_handoffs
        or tuple(abort.get("source_branches", [])) != intent.source_branches
        or tuple(abort.get("source_finalizer_record_ids", [])) != intent.source_finalizer_record_ids
        or not any(event.get("event") == "PREPARATION_ABORTED" for event in intent.history)
        or record is None
        or record.role != "integration"
        or record.lifecycle != "RELEASED"
        or record.task_key != intent.task_key
        or not any(
            event.get("event") == "INTEGRATION_ABORTED" and event.get("owner") == intent.owner
            for event in record.history
        )
        or record.task_commit != intent.candidate_oid
        or allocation in snapshot.bindings.values()
        or allocation in snapshot.writer_leases
        or record.external_bindings
    ):
        raise ControllerError(
            "RETRY_FAILURE_EVIDENCE_MISMATCH", "abort identity or candidate release drifted"
        )
    releases = [r for r in snapshot.release_receipts.values() if r.allocation_id == allocation]
    if len(releases) != 1 or releases[0].released_head != intent.candidate_oid:
        raise ControllerError(
            "RETRY_RELEASE_EVIDENCE_MISSING", "failed candidate has no exact release"
        )
    _release_view(snapshot, releases[0], record, project=False)
    if snapshot.release_plans[releases[0].plan_id].owner != intent.owner:
        raise ControllerError("OWNER_MISMATCH", "failed candidate release owner differs")
    if conflicts(store, allocation=allocation, target=Path(releases[0].target_path)):
        raise ControllerError("BLOCK_TRANSACTION", "failed candidate has unfinished effects")
    if any(
        r.role == "integration" and r.task_key == intent.task_key and r.lifecycle != "RELEASED"
        for r in snapshot.records.values()
    ):
        raise ControllerError(
            "RETRY_CANDIDATE_IN_USE", "another candidate for this task remains live"
        )
    return {
        "abort_receipt_id": abort["receipt_id"],
        "abort_receipt_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "release_receipt_id": releases[0].receipt_id,
        "release_receipt_sha256": _digest(asdict(releases[0])),
        "failed_allocation_id": allocation,
        "failure_reason": intent.blocked_reason,
        "failed_candidate_oid": intent.candidate_oid,
    }


def _prerequisite(
    repository: RepositoryInventory,
    snapshot: MetadataSnapshot,
    store: MetadataStore,
    parent: IntegrationIntent,
    child: IntegrationIntent,
) -> dict[str, Any]:
    from .lifecycle import _effective_intent_state, _scopes_overlap

    if (
        child.owner != parent.owner
        or child.intent_id == parent.intent_id
        or child.queue_sequence <= parent.queue_sequence
        or parent.queue_sequence <= 0
        or child.state not in {"QUEUED", "WAITING_SCOPE"}
        or child.allocation_id is not None
        or child.candidate_oid is not None
        or integration_prerequisite(parent)
        or integration_prerequisite(child)
        or any(
            integration_prerequisite(other) == child.intent_id
            for other in snapshot.integration_intents.values()
        )
        or any(
            integration_prerequisite(other) == parent.intent_id
            for other in snapshot.integration_intents.values()
        )
        or any(r.intent_id == child.intent_id for r in snapshot.publication_receipts.values())
    ):
        raise ControllerError(
            "INVALID_RETRY_PREREQUISITE", "prerequisite must be a separate unclaimed later intent"
        )
    if any(
        other.intent_id != parent.intent_id
        and other.allocation_id is not None
        and _effective_intent_state(snapshot, other) not in {"RELEASED", "ABANDONED"}
        and other.queue_sequence > parent.queue_sequence
        and _scopes_overlap(other.scope_paths, child.scope_paths)
        for other in snapshot.integration_intents.values()
    ):
        raise ControllerError(
            "RETRY_PREREQUISITE_IN_USE",
            "prerequisite cannot overtake an active overlapping candidate",
        )
    sources = _sources(repository, snapshot, store, child)
    ancestors = []
    for head in child.source_handoffs:
        descendants = [
            source
            for source in parent.source_handoffs
            if head != source and is_ancestor(repository.canonical_path, head, source)
        ]
        if not descendants:
            raise ControllerError(
                "INVALID_RETRY_PREREQUISITE",
                "every prerequisite handoff must be a strict source ancestor",
            )
        ancestors.append({"ancestor": head, "descendants": descendants})
    return {
        "intent_id": child.intent_id,
        "intent_sha256": _digest(integration_intent_to_dict(child)),
        "source_identity": _source_identity(child),
        "source_records": sources,
        "ancestry": ancestors,
    }


def _plan(
    repository: RepositoryInventory,
    snapshot: MetadataSnapshot,
    store: MetadataStore,
    intent: IntegrationIntent,
    prerequisite_id: str | None,
    reason: str,
) -> dict[str, Any]:
    from .lifecycle import _effective_intent_state

    failure = _failure(repository, snapshot, store, intent)
    source_records = _sources(repository, snapshot, store, intent)
    policy = load_policy(repository.canonical_path)
    base = resolve_commit(
        repository.canonical_path,
        f"refs/remotes/{policy.publication_remote}/{policy.publication_branch}",
    )
    pending_base = intent.candidate_oid == intent.base_main_oid and any(
        event.get("pending_merge_base") == intent.candidate_oid
        and event.get("pending_merge_source") in intent.source_handoffs
        for event in intent.history
    )
    if (intent.candidate_oid and not pending_base
            and is_ancestor(repository.canonical_path, intent.candidate_oid, base)):
        raise ControllerError(
            "RETRY_ALREADY_PUBLISHED", "failed candidate is already in publication base"
        )
    prerequisite = None
    if prerequisite_id is not None:
        child = snapshot.integration_intents.get(prerequisite_id)
        if child is None:
            raise ControllerError("MISSING_INTENT", "prerequisite intent is missing")
        prerequisite = _prerequisite(repository, snapshot, store, intent, child)
    else:
        bound = integration_prerequisite(intent)
        if bound:
            child = snapshot.integration_intents.get(bound)
            if child is None or _effective_intent_state(snapshot, child) != "RELEASED":
                raise ControllerError(
                    "RETRY_PREREQUISITE_PENDING",
                    "prerequisite needs formal publication and release",
                )
            publications = [
                r
                for r in snapshot.publication_receipts.values()
                if r.intent_id == child.intent_id
                and r.status in {"REMOTE_VERIFIED", "RELEASED"}
                and r.candidate_oid == child.candidate_oid
            ]
            if (
                len(publications) != 1
                or not child.candidate_oid
                or not is_ancestor(repository.canonical_path, child.candidate_oid, base)
            ):
                raise ControllerError(
                    "RETRY_PREREQUISITE_PENDING", "prerequisite receipt is not absorbed into main"
                )
            released = [
                r
                for r in snapshot.release_receipts.values()
                if r.allocation_id == child.allocation_id
            ]
            record = snapshot.records.get(child.allocation_id)
            if len(released) != 1 or record is None:
                raise ControllerError(
                    "RETRY_PREREQUISITE_PENDING", "prerequisite release proof is missing"
                )
            _release_view(snapshot, released[0], record, project=False)
            prerequisite = {
                "intent_id": child.intent_id,
                "publication_receipt": asdict(publications[0]),
                "release_receipt": asdict(released[0]),
            }
    value = {
        "contract": "integration-retry/v1",
        "repository_id": snapshot.repository_id,
        "action": "bind_prerequisite" if prerequisite_id else "retry",
        "reason": reason,
        "intent_sha256": _digest(integration_intent_to_dict(intent)),
        "previous_intent": {
            key: value
            for key, value in integration_intent_to_dict(intent).items()
            if key != "history"
        },
        "source_identity": _source_identity(intent),
        "source_records": source_records,
        "publication_base_oid": base,
        "failure_evidence": failure,
        "prerequisite": prerequisite,
        "queue_evidence": {
            other.intent_id: _digest(integration_intent_to_dict(other))
            for other in snapshot.integration_intents.values()
            if _effective_intent_state(snapshot, other) not in {"RELEASED", "ABANDONED"}
        },
    }
    return {**value, "plan_id": _digest(value)}


def retry_integration(
    repository_path: Path,
    *,
    intent_id: str,
    owner: str,
    reason: str,
    prerequisite_intent_id: str | None = None,
    dry_run: bool = False,
    expected_plan_id: str | None = None,
) -> dict[str, Any]:
    """Plan/CAS an append-only dependency decision or restart the original intent."""
    from .controller import status
    from .lifecycle import _blocking_scope_intent, _emit_lifecycle_event
    from .record_updates import updated_intent

    detail = reason.strip()
    if not detail or len(detail) > 512 or "\x00" in detail:
        raise ControllerError("INVALID_RETRY_REASON", "retry needs a short explicit reason")
    if not dry_run and not expected_plan_id:
        raise ControllerError("RETRY_PLAN_REQUIRED", "apply requires the exact dry-run plan id")
    repository = inventory(repository_path)
    store = MetadataStore(repository.common_dir)
    replay = False
    with store.lock(exclusive=not dry_run):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        intent = snapshot.integration_intents.get(intent_id)
        if intent is None:
            raise ControllerError("MISSING_INTENT", "failed intent is missing")
        if intent.owner != owner:
            raise ControllerError("OWNER_MISMATCH", "retry must use the existing intent owner")
        last = intent.history[-1] if intent.history else {}
        if (
            not dry_run
            and last.get("plan_id") == expected_plan_id
            and last.get("event") in {"RETRY_PREREQUISITE_BOUND", "PREPARATION_RETRY_QUEUED"}
        ):
            plan = last["retry_plan"]
            if (
                plan["reason"] != detail
                or plan["source_identity"] != _source_identity(intent)
                or (plan["action"] == "bind_prerequisite") != bool(prerequisite_intent_id)
                or (
                    prerequisite_intent_id
                    and plan["prerequisite"]["intent_id"] != prerequisite_intent_id
                )
            ):
                raise ControllerError("BLOCK_DRIFT", "retry replay request differs")
            original = replace(
                intent,
                history=(
                    *intent.history[:-1],
                    {k: v for k, v in last.items() if k != "after_intent_sha256"},
                ),
            )
            if (
                _digest(integration_intent_to_dict(original)) != last.get("after_intent_sha256")
                or _sources(repository, snapshot, store, intent) != plan["source_records"]
            ):
                raise ControllerError("BLOCK_DRIFT", "retry replay evidence changed")
            replay = True
        else:
            plan = _plan(repository, snapshot, store, intent, prerequisite_intent_id, detail)
            if expected_plan_id is not None and plan["plan_id"] != expected_plan_id:
                raise ControllerError("BLOCK_DRIFT", "retry plan evidence changed; plan again")
            if not dry_run:
                if prerequisite_intent_id:
                    updated = updated_intent(intent, "RETRY_PREREQUISITE_BOUND")
                else:
                    updated = updated_intent(
                        intent,
                        "PREPARATION_RETRY_QUEUED",
                        state="QUEUED",
                        attempt=intent.attempt + 1,
                        base_main_oid=None,
                        candidate_oid=None,
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
                        equivalent_replays=[],
                        blocked_reason=None,
                        claimed_at=None,
                        validated_at=None,
                        published_at=None,
                        released_at=None,
                    )
                    blocker = _blocking_scope_intent(
                        snapshot, updated, repository_path=repository.canonical_path
                    )
                    if blocker:
                        updated = replace(
                            updated,
                            state="WAITING_SCOPE",
                            blocked_reason=f"scope overlaps earlier intent {blocker.intent_id}",
                        )
                event = {
                    **updated.history[-1],
                    "to": updated.state,
                    "plan_id": plan["plan_id"],
                    "retry_plan": plan,
                }
                if prerequisite_intent_id:
                    event["prerequisite_intent_id"] = prerequisite_intent_id
                intent = replace(updated, history=(*updated.history[:-1], event))
                event["after_intent_sha256"] = _digest(integration_intent_to_dict(intent))
                intent = replace(intent, history=(*intent.history[:-1], event))
                store.replace_integration_intent(intent)
                if not prerequisite_intent_id:
                    _emit_lifecycle_event(store, snapshot.repository_id, intent, "INTENT_QUEUED")
    payload = status(repository_path)
    payload["command"] = "integration-retry"
    payload["decision"] = {
        "code": "INTEGRATION_RETRY_PLAN"
        if dry_run
        else (
            "INTEGRATION_PREREQUISITE_BOUND"
            if prerequisite_intent_id
            else "INTEGRATION_RETRY_QUEUED"
        ),
        "reason": "controlled retry retains original identity and failure evidence",
        "intent_id": intent_id,
        "state": intent.state,
        "attempt": intent.attempt,
        "queue_sequence": intent.queue_sequence,
        "scope_digest": intent.scope_digest,
        "plan_id": plan["plan_id"],
        "replay": replay,
        "execution_order": list(integration_queue_order(snapshot, intent)),
        "plan": plan,
    }
    return payload
