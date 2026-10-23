"""Accept one owner-resolved merge without changing the frozen integration contract."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from .git_backend import commits_with_parents, inventory
from .integration_retry import _sources
from .models import ControllerError
from .semantics import integration_candidate_issues
from .store import MetadataStore


def resolve_integration(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner: str,
    expected_head: str,
    finalizer_record_id: str,
) -> dict[str, Any]:
    from .controller import status
    from .lifecycle import _intent_for_allocation, _intent_payload
    from .record_updates import updated_intent, updated_record
    from .semantics import assert_record_version as _cas
    from .semantics import managed_record as _managed_record

    evidence = finalizer_record_id.strip()
    if not evidence or len(evidence) > 512 or "\x00" in evidence:
        raise ControllerError(
            "MISSING_FINALIZER_RECORD", "resolution requires exact Finalizer evidence"
        )
    repository = inventory(repository_path)
    store = MetadataStore(repository.common_dir)
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        _cas(record, expected_record_version)
        intent = _intent_for_allocation(snapshot, record.allocation_id)
        if record.owner != owner:
            raise ControllerError("OWNER_MISMATCH", "resolution owner does not match")
        if (
            record.role != "integration"
            or record.lifecycle != "ACTIVE"
            or intent is None
            or intent.state != "PREPARING_INTEGRATION"
        ):
            raise ControllerError(
                "INVALID_LIFECYCLE", "resolution requires an active claimed intent"
            )
        if (
            snapshot.publication_lease
            and snapshot.publication_lease.allocation_id == record.allocation_id
        ):
            raise ControllerError(
                "PUBLICATION_LEASE_HELD", "resolution cannot change publication authority"
            )
        event = intent.history[-1] if intent.history else {}
        if (
            event.get("event") != "CANDIDATE_CLAIMED"
            or len(intent.source_handoffs) != 1
            or event.get("pending_merge_source") != intent.source_handoffs[0]
            or event.get("pending_merge_base") != intent.base_main_oid
            or intent.candidate_oid != intent.base_main_oid
            or record.task_commit != intent.base_main_oid
            or record.base_commit != intent.base_main_oid
        ):
            raise ControllerError("CONFLICT_RESOLUTION_NOT_PENDING", "no exact pending merge claim")
        if item.detached or not item.clean or item.head != expected_head:
            raise ControllerError(
                "CANDIDATE_IDENTITY_MISMATCH", "resolution needs the exact clean attached HEAD"
            )
        _sources(repository, snapshot, store, intent)
        topology = commits_with_parents(item.path, intent.base_main_oid, item.head)
        parents = next((row for row in topology if row[0] == item.head), ())
        if parents != (item.head, intent.base_main_oid, intent.source_handoffs[0]):
            raise ControllerError(
                "RESOLUTION_PARENT_MISMATCH",
                "merge parents must be the pinned main and frozen source",
            )
        issues = integration_candidate_issues(item, record, snapshot, require_task_match=False)
        if issues:
            raise ControllerError(issues[0]["code"], issues[0]["message"])
        updated = updated_record(record, "INTEGRATION_CONFLICT_RESOLVED", task_commit=item.head)
        resolved = updated_intent(
            intent,
            "INTEGRATION_CONFLICT_RESOLVED",
            candidate_oid=item.head,
            revision=intent.revision + 1,
        )
        resolved = replace(
            resolved,
            history=(
                *resolved.history[:-1],
                {
                    **resolved.history[-1],
                    "resolved_oid": item.head,
                    "base_oid": intent.base_main_oid,
                    "source_oid": intent.source_handoffs[0],
                    "finalizer_record_id": evidence,
                },
            ),
        )
        store.replace_record(updated)
        store.replace_integration_intent(resolved)
    payload = status(repository_path)
    payload["command"] = "integration-resolve"
    payload["decision"] = {
        "code": "INTEGRATION_CONFLICT_RESOLVED",
        "reason": "resolved merge accepted; fresh tests, Snapshot and validation are still required",
        "record_version": updated.record_version,
        **_intent_payload(resolved),
    }
    return payload
