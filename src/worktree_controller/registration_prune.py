"""Owner-reviewed removal of a missing, no-change audit registration.

The checkout is already absent. This path removes only its stale Git registration;
it never uses ``--force`` and never treats a clean retained checkout as disposable.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from .git_backend import (
    git_write,
    inventory,
    is_ancestor,
    public_refs,
    worktree_content_fingerprint,
)
from .lifecycle import _updated_record
from .models import (
    ControllerError,
    MetadataRecord,
    MetadataSnapshot,
    RepositoryInventory,
    WorktreeGitState,
    WorktreePolicy,
)
from .store import MetadataStore, now_iso

CONTRACT = "registration-prune/v1"


def _path_absent(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise ControllerError("PATH_STATE_UNKNOWN", f"cannot inspect GC target: {path}") from exc
    return False


def _plan_id(plan: dict[str, Any]) -> str:
    encoded = json.dumps(plan, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_prune_plan(
    repository: RepositoryInventory,
    snapshot: MetadataSnapshot,
    policy: WorktreePolicy,
    item: WorktreeGitState,
    record: MetadataRecord,
    owner_release: str | None,
) -> dict[str, Any]:
    """Require exact missing-path, owner, no-change, and recoverable-ref evidence."""
    if item.canonical or not item.prunable or item.locked or not item.detached or item.branch:
        raise ControllerError(
            "PRUNABLE_STATE_INVALID", "only detached, prunable linked audits qualify"
        )
    if not _path_absent(item.path):
        raise ControllerError("PATH_REAPPEARED", "GC target is present")
    if record.role != "audit" or record.lifecycle != "RETAINED":
        raise ControllerError("LIFECYCLE_NOT_RELEASABLE", "only retained no-change audits qualify")
    if owner_release is None or owner_release.strip() != record.owner:
        raise ControllerError("ACTIVE_OWNER", "recorded owner must authorize registration pruning")
    if (
        record.base_commit != item.head
        or record.task_commit is not None
        or record.handoff_commit is not None
        or record.finalizer_commit is not None
        or record.finalizer_record_id is not None
    ):
        raise ControllerError("TASK_EVIDENCE_PRESENT", "audit may contain task work")
    if record.external_bindings or record.disposable_resources:
        raise ControllerError("EXTERNAL_BINDING", "audit has managed resources or bindings")
    if (
        snapshot.bindings.get(item.key) != record.allocation_id
        or sum(allocation == record.allocation_id for allocation in snapshot.bindings.values()) != 1
    ):
        raise ControllerError("BINDING_DRIFT", "audit allocation must have one exact binding")
    if record.allocation_id in snapshot.writer_leases or (
        snapshot.publication_lease is not None
        and snapshot.publication_lease.allocation_id == record.allocation_id
    ):
        raise ControllerError("LEASE_PRESENT", "an active lease blocks registration pruning")
    if any(
        record.allocation_id in intent.source_allocations
        or intent.allocation_id == record.allocation_id
        for intent in snapshot.integration_intents.values()
    ) or any(
        intent.allocation_id == record.allocation_id for intent in snapshot.release_intents.values()
    ):
        raise ControllerError("TASK_REFERENCE_PRESENT", "an intent still references the audit")
    if snapshot.repository_id is None:
        raise ControllerError("BLOCK_DRIFT", "repository metadata is missing")
    preserving_ref = f"refs/heads/{policy.publication_branch}"
    preserving_oid = public_refs(repository.canonical_path).get(preserving_ref)
    if preserving_oid is None or not is_ancestor(
        repository.canonical_path, item.head, preserving_oid
    ):
        raise ControllerError("MISSING_RETAINED_REF", "audit HEAD is not retained by publication")
    plan = {
        "contract": CONTRACT,
        "repository_id": snapshot.repository_id,
        "allocation_id": record.allocation_id,
        "target_path": str(item.path),
        "worktree_key": item.key,
        "admin_path": str(item.git_dir),
        "expected_head": item.head,
        "expected_record_version": record.record_version,
        "owner": record.owner,
        "task_key": record.task_key,
        "authority_key": record.authority_key,
        "preserving_ref": preserving_ref,
        "preserving_oid": preserving_oid,
    }
    return {**plan, "gc_plan_id": _plan_id(plan)}


def _journal_for_target(store: MetadataStore, target: Path) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for path in sorted(store.transactions_dir.glob("*.json")):
        value = dict(store._read_json(path))
        if value.get("command") == "registration-prune" and value.get("target_path") == str(target):
            if value.get("transaction_id") != path.stem:
                raise ControllerError("INVALID_TRANSACTION", "prune journal filename differs")
            matches.append(value)
    if len(matches) > 1:
        raise ControllerError("INTERRUPTED_TRANSACTION", "multiple prune attempts need review")
    return matches[0] if matches else None


def _validate_recovery(
    repository: RepositoryInventory,
    snapshot: MetadataSnapshot,
    target: Path,
    journal: dict[str, Any],
    expected_gc_plan_id: str,
    owner_release: str | None,
) -> MetadataRecord:
    allocation = journal.get("allocation_id")
    record = snapshot.records.get(allocation)
    if (
        record is None
        or journal.get("repository_id") != snapshot.repository_id
        or journal.get("gc_plan_id") != expected_gc_plan_id
        or journal.get("target_path") != str(target)
        or journal.get("owner") != owner_release
        or (journal.get("owner") != record.owner and record.lifecycle != "RELEASED")
        or journal.get("task_key") != record.task_key
        or journal.get("authority_key") != record.authority_key
        or journal.get("expected_head") != record.base_commit
        or not isinstance(journal.get("preserving_oid"), str)
        or journal.get("phase") not in {"PREPARED", "WORKTREE_REMOVED", "RELEASED_RECORDED"}
        or not isinstance(journal.get("expected_record_version"), int)
    ):
        raise ControllerError("GC_PLAN_DRIFT", "prune recovery identity changed")
    if not _path_absent(target):
        raise ControllerError("PATH_REAPPEARED", "GC target reappeared during recovery")
    if record.record_version not in {
        journal["expected_record_version"],
        journal["expected_record_version"] + 1,
    }:
        raise ControllerError("GC_PLAN_DRIFT", "prune record version changed")
    if record.lifecycle == "RELEASED" and (
        record.record_version != journal["expected_record_version"] + 1
        or record.owner != "unassigned"
    ):
        raise ControllerError("GC_PLAN_DRIFT", "released audit metadata differs from prune plan")
    if not is_ancestor(
        repository.canonical_path, journal["expected_head"], journal["preserving_oid"]
    ):
        raise ControllerError("MISSING_RETAINED_REF", "reviewed publication OID lost the audit")
    if record.lifecycle != "RELEASED" and (
        record.role != "audit"
        or record.lifecycle != "RETAINED"
        or record.task_commit is not None
        or record.handoff_commit is not None
        or record.finalizer_commit is not None
        or record.finalizer_record_id is not None
        or record.external_bindings
        or record.disposable_resources
    ):
        raise ControllerError("GC_PLAN_DRIFT", "audit acquired task evidence during recovery")
    preserving_oid = public_refs(repository.canonical_path).get(journal.get("preserving_ref"))
    if preserving_oid is None or not is_ancestor(
        repository.canonical_path, journal["expected_head"], preserving_oid
    ):
        raise ControllerError("MISSING_RETAINED_REF", "audit HEAD lost its retaining ref")
    if record.allocation_id in snapshot.writer_leases or (
        snapshot.publication_lease is not None
        and snapshot.publication_lease.allocation_id == record.allocation_id
    ):
        raise ControllerError("LEASE_PRESENT", "a lease appeared during prune recovery")
    if any(
        (key == journal.get("worktree_key") and allocation != record.allocation_id)
        or (key != journal.get("worktree_key") and allocation == record.allocation_id)
        for key, allocation in snapshot.bindings.items()
    ):
        raise ControllerError("BINDING_DRIFT", "prune binding changed owner")
    return record


def apply_prune(
    repository_path: Path,
    *,
    target: Path,
    expected_gc_plan_id: str,
    owner_release: str | None,
) -> dict[str, Any] | None:
    """Return None for ordinary GC targets; otherwise apply or resume exact prune."""
    target = target.expanduser().resolve(strict=False)
    initial = inventory(repository_path)
    store = MetadataStore(initial.common_dir)
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        item = next((row for row in repository.worktrees if row.path == target), None)
        journal = _journal_for_target(store, target)
        if journal is None and (item is None or not item.prunable):
            return None
        if journal is None:
            if item is None:
                raise ControllerError("GC_PLAN_DRIFT", "prunable registration disappeared")
            record = snapshot.records.get(snapshot.bindings.get(item.key, ""))
            if record is None:
                raise ControllerError("METADATA_MISSING", "prunable audit lacks owner record")
            from .policy import load_policy
            from .transaction_scope import conflicts

            plan = build_prune_plan(
                repository,
                snapshot,
                load_policy(repository.canonical_path),
                item,
                record,
                owner_release,
            )
            if plan["gc_plan_id"] != expected_gc_plan_id:
                raise ControllerError("GC_PLAN_DRIFT", "registration prune plan changed")
            if conflicts(store, allocation=record.allocation_id, target=target):
                raise ControllerError(
                    "INTERRUPTED_TRANSACTION", "another mutation fences the audit"
                )
            journal = {
                "schema_version": 4,
                "transaction_id": str(uuid.uuid4()),
                "repository_id": plan["repository_id"],
                "command": "registration-prune",
                "phase": "PREPARED",
                "target_path": str(target),
                "allocation_id": record.allocation_id,
                "branch": None,
                "expected_head": item.head,
                "expected_record_version": record.record_version,
                "worktree_key": item.key,
                "admin_path": str(item.git_dir),
                "owner": record.owner,
                "task_key": record.task_key,
                "authority_key": record.authority_key,
                "preserving_ref": plan["preserving_ref"],
                "preserving_oid": plan["preserving_oid"],
                "gc_plan_id": expected_gc_plan_id,
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
            store.write_transaction(journal["transaction_id"], journal)
        else:
            if item is None:
                from .transaction_scope import registration_prune_terminal

                if registration_prune_terminal(store, journal["transaction_id"]):
                    raise ControllerError(
                        "GC_ALREADY_APPLIED", "registration prune receipt already closes this plan"
                    )
            record = _validate_recovery(
                repository, snapshot, target, journal, expected_gc_plan_id, owner_release
            )
            from .transaction_scope import conflicts

            if any(
                row.get("transaction_id") != journal["transaction_id"]
                for row in conflicts(store, allocation=record.allocation_id, target=target)
            ):
                raise ControllerError(
                    "INTERRUPTED_TRANSACTION", "another mutation fences the audit"
                )
            if item is not None:
                if (
                    not item.prunable
                    or item.key != journal.get("worktree_key")
                    or (
                        item.head != journal.get("expected_head")
                        or str(item.git_dir) != journal.get("admin_path")
                    )
                ):
                    raise ControllerError("GC_PLAN_DRIFT", "registered audit identity changed")
                from .policy import load_policy

                plan = build_prune_plan(
                    repository,
                    snapshot,
                    load_policy(repository.canonical_path),
                    item,
                    record,
                    owner_release,
                )
                if plan["gc_plan_id"] != expected_gc_plan_id:
                    raise ControllerError(
                        "GC_PLAN_DRIFT", "registered audit changed during recovery"
                    )
            elif os.path.lexists(journal["admin_path"]):
                raise ControllerError("GC_PLAN_DRIFT", "Git admin registration remains uncertain")
        transaction_id = journal["transaction_id"]
        if item is not None:
            refs_before = public_refs(repository.canonical_path)
            others_before = worktree_content_fingerprint(
                repository, exclude_keys=frozenset({item.key})
            )
            if not _path_absent(target):
                raise ControllerError("PATH_REAPPEARED", "GC target reappeared before Git removal")
            git_write(repository.canonical_path, ("worktree", "remove", str(target)))
            after = inventory(repository_path)
            if any(row.path == target for row in after.worktrees) or (
                public_refs(after.canonical_path) != refs_before
                or worktree_content_fingerprint(after) != others_before
            ):
                raise ControllerError(
                    "BLOCK_DRIFT", "registration prune changed another Git identity"
                )
            journal["phase"] = "WORKTREE_REMOVED"
            journal["updated_at"] = now_iso()
            store.write_transaction(transaction_id, journal)
        elif journal["phase"] == "PREPARED":
            journal["phase"] = "WORKTREE_REMOVED"
            journal["updated_at"] = now_iso()
            store.write_transaction(transaction_id, journal)
        snapshot = store._load_unlocked()
        record = snapshot.records[journal["allocation_id"]]
        if record.record_version == journal["expected_record_version"]:
            updated = _updated_record(
                record, "REGISTRATION_PRUNED", lifecycle="RELEASED", owner="unassigned"
            )
            store.replace_record(updated)
        elif record.record_version != journal["expected_record_version"] + 1 or (
            record.lifecycle != "RELEASED" or record.owner != "unassigned"
        ):
            raise ControllerError("GC_PLAN_DRIFT", "prune metadata changed during recovery")
        snapshot = store._load_unlocked()
        if snapshot.bindings.get(journal["worktree_key"]) == journal["allocation_id"]:
            store.remove_binding(journal["worktree_key"])
        elif journal["worktree_key"] in snapshot.bindings:
            raise ControllerError("BINDING_DRIFT", "prune binding changed allocation")
        receipt = {
            "contract": CONTRACT,
            "receipt_id": transaction_id,
            "repository_id": journal["repository_id"],
            "allocation_id": journal["allocation_id"],
            "target_path": str(target),
            "worktree_key": journal["worktree_key"],
            "expected_head": journal["expected_head"],
            "expected_record_version": journal["expected_record_version"],
            "released_record_version": journal["expected_record_version"] + 1,
            "preserving_ref": journal["preserving_ref"],
            "preserving_oid": journal["preserving_oid"],
            "gc_plan_id": expected_gc_plan_id,
            "result": "REGISTRATION_PRUNED",
            "completed_at": now_iso(),
        }
        store.write_registration_prune_receipt(transaction_id, receipt)
        journal["phase"] = "RELEASED_RECORDED"
        journal["updated_at"] = now_iso()
        store.write_transaction(transaction_id, journal)
    from .controller import status

    result = status(repository_path)
    result["command"] = "gc-apply"
    result["decision"] = {
        "code": "REGISTRATION_PRUNED",
        "allocation_id": journal["allocation_id"],
        "gc_plan_id": expected_gc_plan_id,
        "receipt_id": transaction_id,
        "preserving_ref": journal["preserving_ref"],
        "reason": "missing audit registration removed; commit retained by publication ref",
    }
    return result
