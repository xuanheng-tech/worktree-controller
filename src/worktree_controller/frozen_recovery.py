"""Owner-reviewed recovery of published frozen checkouts without rewriting evidence."""

from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .controller import status
from .git_backend import (
    branch_exists,
    git_write,
    inventory,
    is_ancestor,
    resolve_commit,
    validate_branch_name,
)
from .lifecycle import (
    _cancel_release_intents,
    _typed_runtime_blockers,
    _typed_runtime_evidence,
)
from .models import ControllerError
from .policy import load_policy
from .record_updates import updated_record
from .semantics import assert_record_version as _cas
from .semantics import integration_intent_abandoned, publication_ref
from .semantics import managed_record as _managed_record
from .store import MetadataStore, record_to_dict
from .transaction_scope import conflicts
from .writer_lease import holder_is_live, process_group_is_live

CONTRACT = "published-checkout-recovery/v1"


def recover_frozen_checkout(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    expected_head: str,
    owner: str,
    branch: str | None = None,
) -> dict[str, Any]:
    """Recover a published source advance, or reattach a published integration.

    The immutable preparation receipt makes interruption safe: a retry can finish
    only the same record/HEAD/ref transition. No working files, index, historical
    publication receipts or commits are rewritten. Normal release gates still run.
    """
    command = "recover-published" if branch is not None else "recover-handoff"
    initial = inventory(repository_path)
    store = MetadataStore(initial.common_dir)
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        _cas(record, expected_record_version)
        if owner != record.owner:
            raise ControllerError("OWNER_MISMATCH", "recovery requires the recorded owner")
        if item.head != expected_head:
            raise ControllerError("IDENTITY_MISMATCH", "recovery HEAD differs from expected HEAD")
        if item.canonical or item.locked or item.prunable or item.staged:
            raise ControllerError("BLOCK_DRIFT", "canonical, locked, prunable or staged checkout")
        if conflicts(store, allocation=record.allocation_id, target=item.path):
            raise ControllerError(
                "INTERRUPTED_TRANSACTION", "resolve the allocation transaction first"
            )
        formal_ref = publication_ref(item.path, load_policy(repository.canonical_path))
        if formal_ref is None or not is_ancestor(item.path, item.head, formal_ref):
            raise ControllerError(
                "UNPUBLISHED_HEAD", "observed HEAD must already be on the publication ref"
            )
        for intent in snapshot.integration_intents.values():
            if (
                record.allocation_id in (*intent.source_allocations, intent.allocation_id)
                and intent.state not in {"REMOTE_VERIFIED", "RELEASED"}
                and not integration_intent_abandoned(intent)
            ):
                raise ControllerError(
                    "ACTIVE_INTEGRATION", "an unfinished integration still consumes this allocation"
                )

        lease = snapshot.writer_leases.get(record.allocation_id)
        if lease and (holder_is_live(lease.holder) or process_group_is_live(lease.holder)):
            raise ControllerError("LIVE_WRITER", "a live writer or process group blocks recovery")
        runtime_snapshot = replace(
            snapshot,
            writer_leases={
                key: value
                for key, value in snapshot.writer_leases.items()
                if key != record.allocation_id
            },
        )
        blockers = _typed_runtime_blockers(_typed_runtime_evidence(item, record, runtime_snapshot))
        if blockers:
            raise ControllerError(blockers[0]["code"], blockers[0]["message"])

        receipt_id = str(
            uuid5(
                NAMESPACE_URL,
                ":".join(
                    (
                        CONTRACT,
                        str(snapshot.repository_id),
                        record.allocation_id,
                        str(expected_record_version),
                        expected_head,
                        command,
                        branch or "",
                    )
                ),
            )
        )
        receipt_path = store.completion_receipts_dir / f"{receipt_id}.json"
        prepared = store._read_json(receipt_path) if receipt_path.exists() else None
        if prepared and (
            prepared.get("contract") != CONTRACT
            or prepared.get("receipt_id") != receipt_id
            or prepared.get("record") != record_to_dict(record)
            or prepared.get("expected_head") != expected_head
            or prepared.get("branch") != branch
            or prepared.get("target_path") != str(item.path)
            or prepared.get("repository_id") != snapshot.repository_id
            or prepared.get("command") != command
            or (lease is not None and prepared.get("writer_lease") != asdict(lease))
            or (branch is None and prepared.get("observed_branch") != item.branch)
        ):
            raise ControllerError("BLOCK_DRIFT", "immutable recovery preparation differs")

        receipts = [
            receipt
            for receipt in snapshot.publication_receipts.values()
            if receipt.repository_id == snapshot.repository_id
            and receipt.status == "REMOTE_VERIFIED"
            and receipt.lease_released_at
            and is_ancestor(item.path, receipt.candidate_oid, formal_ref)
        ]
        if branch is None:
            frozen = record.handoff_commit
            if (
                record.role not in {"feature", "audit", "hotfix"}
                or record.lifecycle not in {"HANDOFF_READY", "PRESERVED"}
                or item.detached
                or not item.branch
                or not frozen
                or not record.finalizer_record_id
                or frozen != record.task_commit
                or frozen != record.finalizer_commit
                or frozen == item.head
                or not is_ancestor(item.path, frozen, item.head)
            ):
                raise ControllerError(
                    "INVALID_FROZEN_SOURCE",
                    "recovery requires an attached, published advance of a frozen source",
                )
            receipts = [
                receipt
                for receipt in receipts
                if any(
                    result.get("source_allocation_id") == record.allocation_id
                    and result.get("source_handoff_oid") == frozen
                    and result.get("absorbed_oid") == frozen
                    and result.get("mode") == "ancestry"
                    and is_ancestor(item.path, frozen, receipt.candidate_oid)
                    for result in receipt.source_results
                )
            ]
        else:
            validate_branch_name(item.path, branch)
            if (
                record.role != "integration"
                or record.lifecycle != "PUBLISHED"
                or not item.clean
                or not record.finalizer_record_id
                or record.task_commit != item.head
                or record.finalizer_commit != item.head
                or (not item.detached and not (prepared and item.branch == branch))
            ):
                raise ControllerError(
                    "INVALID_PUBLISHED_CHECKOUT",
                    "recovery requires a clean, detached published integration",
                )
            if any(
                other.key != item.key and other.branch == branch for other in repository.worktrees
            ):
                raise ControllerError("BRANCH_IN_USE", "recovery branch is checked out elsewhere")
            if branch_exists(item.path, branch) and (
                not prepared or resolve_commit(item.path, f"refs/heads/{branch}") != item.head
            ):
                raise ControllerError(
                    "BRANCH_EXISTS", "recovery must create a fresh retained branch"
                )
            receipts = [
                receipt
                for receipt in receipts
                if receipt.allocation_id == record.allocation_id
                and receipt.candidate_oid == item.head
                and receipt.finalizer_record_id == record.finalizer_record_id
            ]
        if not receipts:
            raise ControllerError(
                "MISSING_PUBLICATION_EVIDENCE", "exact released publication evidence is required"
            )
        publication = sorted(receipts, key=lambda value: value.receipt_id)[0]
        if prepared and prepared.get("publication_receipt_id") not in {
            r.receipt_id for r in receipts
        }:
            raise ControllerError("BLOCK_DRIFT", "prepared publication evidence is no longer valid")
        if prepared is None:
            prepared = {
                "contract": CONTRACT,
                "receipt_id": receipt_id,
                "repository_id": snapshot.repository_id,
                "command": command,
                "target_path": str(item.path),
                "record": record_to_dict(record),
                "expected_head": item.head,
                "branch": branch,
                "observed_branch": item.branch,
                "publication_receipt_id": publication.receipt_id,
                "publication_ref": formal_ref,
                "publication_oid": resolve_commit(item.path, formal_ref),
                "writer_lease": asdict(lease) if lease else None,
                "staged": item.staged,
                "modified": item.modified,
                "untracked": item.untracked,
            }
            store.write_completion_receipt(receipt_id, prepared)

        if branch is not None:
            if not branch_exists(item.path, branch):
                git_write(
                    item.path,
                    ("update-ref", f"refs/heads/{branch}", item.head, "0" * len(item.head)),
                )
            latest, _ = _managed_record(inventory(repository_path), snapshot, target)
            if latest.head != expected_head or not latest.clean:
                raise ControllerError(
                    "BLOCK_DRIFT", "checkout changed during recovery; retained ref is preserved"
                )
            if latest.detached:
                # Repoint only HEAD; unlike checkout this never rewrites files or runs hooks.
                git_write(item.path, ("symbolic-ref", "HEAD", f"refs/heads/{branch}"))
            elif latest.branch != branch:
                raise ControllerError("BLOCK_DRIFT", "checkout branch changed during recovery")
            updated = updated_record(record, "PUBLISHED_CHECKOUT_REATTACHED")
        else:
            _cancel_release_intents(
                store,
                snapshot,
                record.allocation_id,
                event="FROZEN_SOURCE_RECOVERED",
                blocker="REOPENED",
            )
            updated = updated_record(
                record,
                "PUBLISHED_SOURCE_RECOVERED",
                lifecycle="ACTIVE",
                base_commit=item.head,
                handoff_commit=None,
                task_commit=None,
                finalizer_commit=None,
                finalizer_record_id=None,
            )
        if lease:
            store.remove_writer_lease(record.allocation_id)
        updated = replace(
            updated,
            history=(
                *updated.history[:-1],
                {
                    **updated.history[-1],
                    "recovery_receipt_id": receipt_id,
                },
            ),
        )
        store.replace_record(updated)
    payload = status(repository_path)
    payload["command"] = command
    payload["decision"] = {
        "code": "FROZEN_CHECKOUT_RECOVERED",
        "allocation_id": updated.allocation_id,
        "record_version": updated.record_version,
        "lifecycle": updated.lifecycle,
        "receipt_id": receipt_id,
        "head": expected_head,
        "branch": branch,
    }
    return payload
