"""Owner-authorized closeout of checkouts already contained in publication.

Closeout changes lifecycle metadata only. It neither invents task completion nor
rewrites historical task evidence. Physical removal remains the guarded release path.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .git_backend import (
    REMOTE_NAME_PATTERN,
    _git,
    inventory,
    is_ancestor,
    resolve_commit,
    validate_branch_name,
)
from .models import (
    ACTIVE_AUTHORITY_STATES,
    ControllerError,
    MetadataRecord,
    MetadataSnapshot,
    RepositoryInventory,
    WorktreeGitState,
)
from .policy import load_policy
from .store import MetadataStore, record_from_dict, record_to_dict

CONTRACT = "delivered-checkout-closeout/v1"
STATE = "RELEASE_READY"
EVENT = "DELIVERED_CHECKOUT_CLOSED"
ARCHIVE_CONTRACT = "retained-checkout-archive/v1"
ARCHIVE_STATE = "ARCHIVED"
ARCHIVE_EVENT = "RETAINED_CHECKOUT_ARCHIVED"
NO_CHANGE_CONTRACT = "no-change-cleanup-closeout/v1"
NO_CHANGE_RECOVERY_EVENT = "NO_CHANGE_CLEANUP_RECONCILED"


def _no_change_fail(message: str) -> ControllerError:
    return ControllerError("NO_CHANGE_CLOSEOUT_INVALID", message)


def _no_change_time(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise _no_change_fail("no-change evidence requires an aware timestamp")
    return result


def _no_change_transition(original: MetadataRecord, event: dict[str, Any]) -> MetadataRecord:
    if (original.lifecycle not in {"ACTIVE", "RETAINED"}
            or original.role not in {"feature", "audit"}
            or event.get("event") != "NO_CHANGE_COMPLETED"
            or event.get("from") != original.lifecycle
            or event.get("to") != "NO_CHANGE_COMPLETED"
            or event.get("owner") != original.owner):
        raise _no_change_fail("no-change completion is not the exact original transition")
    if _no_change_time(event["at"]) < _no_change_time(original.last_reviewed_at):
        raise _no_change_fail("no-change completion predates the original record")
    return replace(original, lifecycle="NO_CHANGE_COMPLETED",
                   record_version=original.record_version + 1,
                   last_reviewed_at=event["at"], history=(*original.history, event))


def _no_change_identity(receipt: dict, original: MetadataRecord,
                        item: WorktreeGitState, repository_id: str | None) -> None:
    expected = {
        "kind": "no_change_completion", "schema_version": 1,
        "repository_id": repository_id, "allocation_id": original.allocation_id,
        "task_key": original.task_key, "authority_key": original.authority_key,
        "role": original.role, "owner": original.owner, "branch": item.branch,
        "head": item.head, "base_commit": original.base_commit,
        "worktree_clean": True, "handoff_required": False, "task_completed": True,
        "source_cleanup_evidence": [], "external_bindings": [],
    }
    if (any(receipt.get(key) != value for key, value in expected.items())
            or (receipt.get("contract") == NO_CHANGE_CONTRACT and receipt.get("target_path") != str(item.path))
            or (original.base_commit is not None and original.base_commit != item.head)
            or original.handoff_commit or original.finalizer_record_id
            or original.finalizer_commit or original.external_bindings):
        raise _no_change_fail("no-change completion identity changed")


def _completion_document(store: MetadataStore, ref: dict[str, str]) -> dict:
    from .cleanup_evidence import matches_reference, read

    if (set(ref) != {"receipt_id", "path", "sha256"}
            or str(uuid.UUID(ref["receipt_id"])) != ref["receipt_id"]
            or not matches_reference(ref, store.completion_receipts_dir / f"{ref['receipt_id']}.json")):
        raise _no_change_fail("completion reference differs from its exact canonical identity")
    value, _ = read(Path(ref["path"]), ref["sha256"])
    if value.get("receipt_id") != ref["receipt_id"]:
        raise _no_change_fail("completion receipt identity changed")
    return value


def _legacy_no_change(store: MetadataStore, item: WorktreeGitState, record: MetadataRecord,
                       repository_id: str | None, ref: dict[str, str]) -> MetadataRecord:
    from .cleanup_evidence import read

    event = record.history[-1]
    if (record.lifecycle != "NO_CHANGE_COMPLETED" or event.get("no_change_completion_ref")
            or event.get("event") != "NO_CHANGE_COMPLETED" or record.record_version < 2
            or sum(row.get("event") == "NO_CHANGE_COMPLETED" for row in record.history) != 1
            or any(row.get("event") == NO_CHANGE_RECOVERY_EVENT for row in record.history)):
        raise _no_change_fail("only one unreconciled legacy completion transition is eligible")
    original = replace(record, lifecycle=event["from"], record_version=record.record_version - 1,
                       last_reviewed_at=record.history[-2]["at"] if len(record.history) > 1 else record.created_at,
                       history=record.history[:-1])
    if _no_change_transition(original, event) != record:
        raise _no_change_fail("legacy completion is not the exact +1 transition")
    receipt = _completion_document(store, ref)
    if receipt.get("contract") or "before_record" in receipt:
        raise _no_change_fail("new completion evidence cannot be reclassified as legacy")
    _no_change_identity(receipt, original, item, repository_id)
    matches = []
    for path in sorted(store.completion_receipts_dir.glob("*.json")):
        candidate, _ = read(path)
        if (candidate.get("kind") == "no_change_completion"
                and candidate.get("allocation_id") == record.allocation_id):
            matches.append(candidate.get("receipt_id"))
    if matches != [ref["receipt_id"]]:
        raise _no_change_fail("legacy completion evidence is missing or ambiguous")
    times = [_no_change_time(value) for value in (
        original.last_reviewed_at, receipt["completed_at"], event["at"],
    )]
    if not times[0] <= times[1] <= times[2]:
        raise _no_change_fail("legacy completion precedes the original recorded transition")
    return original


def verify_no_change_cleanup(store: MetadataStore, item: WorktreeGitState,
                             record: MetadataRecord, repository_id: str | None) -> list[dict[str, str]]:
    """Bridge only frozen normal transitions; cleanup keeps its original exact CAS."""
    from .preserved_cleanup import cleanup_receipts

    try:
        event = record.history[-1]
        receipt = _completion_document(store, event["no_change_completion_ref"])
        if receipt.get("contract") != NO_CHANGE_CONTRACT or record.lifecycle != "NO_CHANGE_COMPLETED":
            raise _no_change_fail("no-change closeout contract or lifecycle changed")
        if receipt.get("kind") == "no_change_completion":
            original = record_from_dict(receipt["before_record"])
            if (_no_change_transition(original, event) != record
                    or {key: value for key, value in event.items() if key != "no_change_completion_ref"}
                    != receipt["completion_event"]):
                raise _no_change_fail("completion after-record changed")
            _no_change_identity(receipt, original, item, repository_id)
            if not (_no_change_time(original.last_reviewed_at)
                    <= _no_change_time(receipt["completed_at"]) <= _no_change_time(event["at"])):
                raise _no_change_fail("completion timestamp differs from its transition")
            refs = receipt["preservation_cleanup_receipts"]
        elif receipt.get("kind") == "no_change_cleanup_recovery":
            plan = receipt["plan"]
            legacy = record_from_dict(plan["record"])
            exact_event = {"at": receipt["reconciled_at"], "event": NO_CHANGE_RECOVERY_EVENT,
                           "from": "NO_CHANGE_COMPLETED", "to": "NO_CHANGE_COMPLETED",
                           "owner": legacy.owner, "no_change_completion_ref": event["no_change_completion_ref"]}
            expected = replace(legacy, record_version=legacy.record_version + 1,
                               last_reviewed_at=event["at"], history=(*legacy.history, event))
            if (event != exact_event or event.get("event") != NO_CHANGE_RECOVERY_EVENT
                    or event.get("from") != "NO_CHANGE_COMPLETED" or event.get("to") != "NO_CHANGE_COMPLETED"
                    or event.get("owner") != legacy.owner or expected != record
                    or event["at"] != receipt["reconciled_at"]
                    or _no_change_time(event["at"]) < _no_change_time(legacy.last_reviewed_at)
                    or receipt["plan_id"] != _digest(plan)
                    or plan["repository_id"] != repository_id or plan["target_path"] != str(item.path)):
                raise _no_change_fail("reconciliation after-record or plan changed")
            original = _legacy_no_change(store, item, legacy, repository_id, plan["completion_ref"])
            if record_to_dict(original) != plan["cleanup_record"]:
                raise _no_change_fail("original cleanup record changed")
            refs = plan["preservation_cleanup_receipts"]
        else:
            raise _no_change_fail("unsupported no-change completion evidence")
        cleanup_receipts(store, item, original, repository_id, expected_refs=refs)
        # Verified zero-effect refusals still activate release's runtime/writer guards.
        return cleanup_receipts(store, item, original, repository_id, include_no_action_refs=True)
    except ControllerError:
        raise
    except (KeyError, TypeError, ValueError, IndexError, OSError) as exc:
        raise _no_change_fail("no-change closeout evidence is unavailable or changed") from exc


def reconcile_no_change_completion(repository_path: Path, *, target: Path, owner: str,
                                   expected_record_version: int, completion_id: str,
                                   completion_sha256: str, cleanup_ids: tuple[str, ...],
                                   cleanup_sha256: tuple[str, ...], dry_run: bool,
                                   expected_plan_id: str | None = None) -> dict[str, Any]:
    try:
        return _reconcile_no_change_completion(
            repository_path, target=target, owner=owner,
            expected_record_version=expected_record_version,
            completion_id=completion_id, completion_sha256=completion_sha256,
            cleanup_ids=cleanup_ids, cleanup_sha256=cleanup_sha256,
            dry_run=dry_run, expected_plan_id=expected_plan_id,
        )
    except ControllerError:
        raise
    except (KeyError, TypeError, ValueError, IndexError, OSError) as exc:
        raise _no_change_fail("reconciliation identity or evidence is unavailable or malformed") from exc


def _reconcile_no_change_completion(repository_path: Path, *, target: Path, owner: str,
                                    expected_record_version: int, completion_id: str,
                                    completion_sha256: str, cleanup_ids: tuple[str, ...],
                                    cleanup_sha256: tuple[str, ...], dry_run: bool,
                                    expected_plan_id: str | None) -> dict[str, Any]:
    from .cleanup_evidence import read, write
    from .controller import status
    from .lifecycle import _release_transaction
    from .preserved_cleanup import cleanup_receipts
    from .semantics import assert_record_version as _cas
    from .semantics import managed_record as _managed_record

    def reference(identity: str, digest: str) -> dict[str, str]:
        if str(uuid.UUID(identity)) != identity:
            raise _no_change_fail("canonical completion receipt ID required")
        return {"receipt_id": identity, "path": str(store.completion_receipts_dir / f"{identity}.json"), "sha256": digest}

    store = MetadataStore(inventory(repository_path).common_dir)
    with store.lock(exclusive=not dry_run):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        if record.owner != owner:
            raise ControllerError("OWNER_MISMATCH", "completion reconciliation owner does not match")
        _eligible(repository, snapshot, item, record, state="NO_CHANGE_COMPLETED", require_publication=False)
        if _release_transaction(store, target_path=str(item.path), allocation_id=record.allocation_id):
            raise ControllerError("INTERRUPTED_TRANSACTION", "pending transaction blocks reconciliation")
        if not cleanup_ids or len(cleanup_ids) != len(cleanup_sha256) or len(set(cleanup_ids)) != len(cleanup_ids):
            raise _no_change_fail("distinct paired successful cleanup receipt IDs and SHA256 values required")
        refs = sorted((reference(identity, digest) for identity, digest in zip(cleanup_ids, cleanup_sha256, strict=True)),
                      key=lambda ref: ref["receipt_id"])
        completion_ref = reference(completion_id, completion_sha256)
        request = {"owner": owner, "expected_record_version": expected_record_version,
                   "completion_ref": completion_ref, "cleanup_refs": refs}
        if record.history[-1].get("event") == NO_CHANGE_RECOVERY_EVENT:
            completed = _completion_document(store, record.history[-1]["no_change_completion_ref"])
            if completed["plan"]["request"] != request:
                raise _no_change_fail("reconciliation replay differs from the original request")
            if expected_plan_id is not None and expected_plan_id != completed["plan_id"]:
                raise _no_change_fail("reconciliation replay differs from the original plan")
            verify_no_change_cleanup(store, item, record, snapshot.repository_id)
            decision = {"code": "NO_CHANGE_COMPLETION_ALREADY_RECONCILED",
                        "receipt": record.history[-1]["no_change_completion_ref"], "record_version": record.record_version}
        else:
            _cas(record, expected_record_version)
            original = _legacy_no_change(store, item, record, snapshot.repository_id, completion_ref)
            cleanup_receipts(store, item, original, snapshot.repository_id, expected_refs=refs)
            plan = {"contract": NO_CHANGE_CONTRACT, "request": request,
                    "repository_id": snapshot.repository_id, "target_path": str(item.path),
                    "record": record_to_dict(record), "cleanup_record": record_to_dict(original),
                    "completion_ref": completion_ref, "preservation_cleanup_receipts": refs}
            plan_id = _digest(plan)
            if dry_run:
                decision = {"code": "NO_CHANGE_COMPLETION_RECOVERY_READY", "plan": plan, "plan_id": plan_id}
            else:
                if expected_plan_id != plan_id:
                    raise ControllerError("RESOURCE_PLAN_MISMATCH", "reconciliation requires the exact current dry-run plan")
                receipt_id = str(uuid.uuid5(uuid.UUID(record.allocation_id), plan_id))
                path = store.completion_receipts_dir / f"{receipt_id}.json"
                from .store import now_iso

                receipt = {"contract": NO_CHANGE_CONTRACT, "kind": "no_change_cleanup_recovery",
                           "receipt_id": receipt_id, "plan_id": plan_id, "plan": plan}
                if path.exists():
                    existing, digest = read(path)
                    if any(existing.get(key) != value for key, value in receipt.items()) or set(existing) != {*receipt, "reconciled_at"}:
                        raise _no_change_fail("existing reconciliation receipt differs")
                    receipt = existing
                    ref = {"receipt_id": receipt_id, "path": str(path), "sha256": digest}
                else:
                    receipt["reconciled_at"] = now_iso()
                if _no_change_time(receipt["reconciled_at"]) < _no_change_time(record.last_reviewed_at):
                    raise _no_change_fail("reconciliation predates the legacy completion")
                if not path.exists():
                    ref = {"receipt_id": receipt_id, **write(store.completion_receipts_dir, path.name, receipt)}
                event = {"at": receipt["reconciled_at"], "event": NO_CHANGE_RECOVERY_EVENT,
                         "from": record.lifecycle, "to": record.lifecycle, "owner": record.owner,
                         "no_change_completion_ref": ref}
                updated = replace(record, record_version=record.record_version + 1,
                                  last_reviewed_at=event["at"], history=(*record.history, event))
                verify_no_change_cleanup(store, item, updated, snapshot.repository_id)
                store.replace_record(updated)
                decision = {"code": "NO_CHANGE_COMPLETION_RECONCILED", "receipt": ref, "record_version": updated.record_version}
    result = status(repository_path)
    result.update(command="reconcile", decision=decision)
    return result


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _publication(repository: RepositoryInventory) -> dict[str, str]:
    policy = load_policy(repository.canonical_path)
    if not policy.present or policy.policy_schema_version < 2:
        raise ControllerError("MISSING_PUBLICATION_POLICY", "closeout requires publication policy")
    if REMOTE_NAME_PATTERN.fullmatch(policy.publication_remote) is None:
        raise ControllerError("INVALID_REMOTE", "closeout requires a configured remote name")
    validate_branch_name(repository.canonical_path, policy.publication_branch)
    ref = f"refs/heads/{policy.publication_branch}"
    result = _git(
        repository.canonical_path,
        "ls-remote",
        "--exit-code",
        policy.publication_remote,
        ref,
        check=False,
        timeout=30,
    )
    rows = result.stdout.decode("utf-8", "replace").splitlines()
    fields = rows[0].split() if len(rows) == 1 else []
    if result.returncode or len(fields) != 2 or fields[1] != ref:
        raise ControllerError(
            "PUBLICATION_UNVERIFIED", "live publication ref could not be verified"
        )
    tracking = f"refs/remotes/{policy.publication_remote}/{policy.publication_branch}"
    oid = resolve_commit(repository.canonical_path, tracking)
    if fields[0] != oid:
        raise ControllerError(
            "PUBLICATION_TRACKING_STALE", "publication tracking ref needs refresh"
        )
    return {"remote": policy.publication_remote, "ref": ref, "tracking_ref": tracking, "oid": oid}


def _eligible(
    repository: RepositoryInventory,
    snapshot: MetadataSnapshot,
    item: WorktreeGitState,
    record: MetadataRecord,
    *,
    state: str,
    require_publication: bool = True,
    allow_registered_venv: bool = False,
    for_typed_cleanup: bool = False,
) -> dict[str, str]:
    from .lifecycle import (
        _effective_intent_state,
        _typed_runtime_blockers,
        _typed_runtime_evidence,
        _validate_release_environment,
    )

    if for_typed_cleanup and (state != "RETAINED" or require_publication):
        raise ControllerError("INVALID_LIFECYCLE", "typed cleanup does not authorize publication")
    if item.canonical or item.detached or item.locked or item.prunable:
        raise ControllerError(
            "CLOSEOUT_TARGET_INVALID", "closeout requires an attached linked checkout"
        )
    if record.role not in {"feature", "audit"} or record.lifecycle != state:
        raise ControllerError(
            "INVALID_LIFECYCLE", f"closeout requires {state} feature/audit lifecycle"
        )
    if (
        state == "RETAINED"
        and not require_publication
        and any(
            other.allocation_id != record.allocation_id
            and other.lifecycle in ACTIVE_AUTHORITY_STATES
            and (other.task_key == record.task_key or other.authority_key == record.authority_key)
            for other in snapshot.records.values()
        )
    ):
        raise ControllerError(
            "DUPLICATE_AUTHORITY", "another active task or authority blocks archive"
        )
    if not item.clean:
        raise ControllerError("DIRTY_WORKTREE", "closeout requires a clean checkout")
    if record.handoff_commit or record.finalizer_record_id or record.finalizer_commit:
        raise ControllerError(
            "HANDOFF_EVIDENCE_PRESENT", "frozen handoffs require their existing release flow"
        )
    if record.quarantine_reason or record.recovery_ref or record.recovery_allocation_id:
        raise ControllerError(
            "RECOVERY_STATE", "recovery lineage requires its existing recovery flow"
        )
    if record.external_bindings or record.allocation_id in snapshot.writer_leases:
        raise ControllerError("MANAGED_REFERENCE", "bindings and writer leases block closeout")
    if (
        snapshot.publication_lease
        and snapshot.publication_lease.allocation_id == record.allocation_id
    ):
        raise ControllerError("PUBLICATION_LEASE_HELD", "publication is in progress")
    registered_paths = tuple(
        r["path"] for r in record.disposable_resources if r["state"] == "registered"
    )
    if any(
        r["state"] != "cleaned"
        and not (allow_registered_venv and r["state"] == "registered" and r["path"] == ".venv")
        for r in record.disposable_resources
    ):
        raise ControllerError(
            "DISPOSABLE_RESOURCE_PRESENT", "registered resources must be cleaned first"
        )
    # Formal abandonment leaves historical source references in the intent. Archive
    # also accepts reconciled release evidence; both require the frozen source handoff.
    if any(
        intent.allocation_id == record.allocation_id
        or (
            record.allocation_id in intent.source_allocations
            and (
                not (
                    (effective_state := _effective_intent_state(snapshot, intent)) == "ABANDONED"
                    or (not require_publication and effective_state == "RELEASED")
                )
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
    if any(
        intent.allocation_id == record.allocation_id
        and intent.status in {"PENDING", "BLOCKED_RETRYABLE"}
        for intent in snapshot.release_intents.values()
    ):
        raise ControllerError(
            "RELEASE_INTENT_PRESENT", "an existing release intent must be resolved first"
        )
    if resolve_commit(repository.canonical_path, f"refs/heads/{item.branch}") != item.head:
        raise ControllerError(
            "MISSING_RETAINED_REF", "the checkout branch no longer retains its HEAD"
        )
    if not require_publication:
        for oid in (record.base_commit, record.task_commit):
            if oid and not is_ancestor(repository.canonical_path, oid, item.head):
                raise ControllerError(
                    "TASK_LINEAGE_UNREACHABLE",
                    "the retained branch no longer contains recorded task lineage",
                )
    publication: dict[str, str] = {}
    if require_publication:
        publication = _publication(repository)
        for oid in (item.head, record.base_commit, record.task_commit):
            if oid and (
                not is_ancestor(repository.canonical_path, oid, item.head)
                or not is_ancestor(repository.canonical_path, oid, publication["oid"])
            ):
                raise ControllerError(
                    "DELIVERABLE_NOT_PUBLISHED", "HEAD or historical task lineage is not published"
                )
    blockers = _typed_runtime_blockers(_typed_runtime_evidence(item, record, snapshot))
    if blockers:
        raise ControllerError(blockers[0]["code"], blockers[0]["message"])
    if not for_typed_cleanup:
        _validate_release_environment(
            repository,
            item,
            registered_resource_paths=registered_paths if allow_registered_venv else (),
        )
    return publication


def _archive_receipt_binding(
    store: MetadataStore,
    record: MetadataRecord,
    repository_id: str | None,
    *,
    target: Path,
    branch: str | None,
    head: str,
) -> dict[str, Any]:
    """Verify the immutable archive identity without reading preserved payloads."""
    try:
        event = record.history[-1]
        receipt_id = event["archive_receipt_id"]
        if str(uuid.UUID(receipt_id)) != receipt_id:
            raise ValueError("invalid receipt id")
        receipt = store._read_json(store.completion_receipts_dir / f"{receipt_id}.json")
        plan = receipt["plan"]
        original = record_from_dict(plan["record"])
        expected = replace(
            original,
            lifecycle=ARCHIVE_STATE,
            record_version=original.record_version + 1,
            last_reviewed_at=event["at"],
            history=(*original.history, event),
        )
        if (
            record.lifecycle != ARCHIVE_STATE
            or receipt["contract"] != ARCHIVE_CONTRACT
            or receipt["receipt_id"] != receipt_id
            or receipt["plan_id"] != _digest(plan)
            or event["event"] != ARCHIVE_EVENT
            or event["from"] != "RETAINED"
            or event["to"] != ARCHIVE_STATE
            or event["owner"] != original.owner
            or event["archive_plan_id"] != receipt["plan_id"]
            or event["archive_receipt_sha256"]
            != _digest({key: value for key, value in receipt.items() if key != "after_record"})
            or plan["repository_id"] != repository_id
            or original.lifecycle != "RETAINED"
            or plan["owner_release"] != original.owner
            or not plan["actor"].strip()
            or plan["target_path"] != str(target)
            or plan["head"] != head
            or plan["branch"] != branch
            or plan["task_completed"] is not False
            or plan["integration_ready"] is not False
            or plan["publication_asserted"] is not False
            or record_to_dict(record) != receipt["after_record"]
            or record_to_dict(expected) != receipt["after_record"]
        ):
            raise ValueError("archive evidence changed")
    except (KeyError, TypeError, ValueError, IndexError, OSError, ControllerError) as exc:
        raise ControllerError(
            "RETAINED_ARCHIVE_INVALID", "archive receipt or checkout identity changed"
        ) from exc
    return receipt


def verify_archive_receipt(
    store: MetadataStore,
    item: WorktreeGitState,
    record: MetadataRecord,
    repository_id: str | None,
) -> dict[str, Any]:
    """An archived task remains unfinished; its clean HEAD stays on its branch."""
    if not item.clean:
        raise ControllerError(
            "RETAINED_ARCHIVE_INVALID", "archive receipt or checkout identity changed"
        )
    receipt = _archive_receipt_binding(
        store, record, repository_id, target=item.path, branch=item.branch, head=item.head
    )
    original = record_from_dict(receipt["plan"]["record"])
    from .preserved_cleanup import cleanup_receipts

    cleanup_receipts(
        store,
        item,
        original,
        repository_id,
        expected_refs=receipt["plan"].get("preservation_cleanup_receipts", []),
    )
    return receipt


def validate_archive_release(
    repository: RepositoryInventory,
    snapshot: MetadataSnapshot,
    item: WorktreeGitState,
    record: MetadataRecord,
) -> None:
    verify_archive_receipt(
        MetadataStore(repository.common_dir), item, record, snapshot.repository_id
    )
    _eligible(
        repository,
        snapshot,
        item,
        record,
        state=ARCHIVE_STATE,
        require_publication=False,
        allow_registered_venv=True,
    )


def verify_receipt(
    store: MetadataStore,
    item: WorktreeGitState,
    record: MetadataRecord,
    repository_id: str | None,
) -> dict[str, Any]:
    """Validate immutable closeout evidence without network or lifecycle mutation."""
    try:
        event = record.history[-1]
        receipt_id = event["closeout_receipt_id"]
        if str(uuid.UUID(receipt_id)) != receipt_id:
            raise ValueError("invalid receipt id")
        receipt = store._read_json(store.completion_receipts_dir / f"{receipt_id}.json")
        plan = receipt["plan"]
        before, after = plan["record"], receipt["after_record"]
        original = record_from_dict(before)
        expected = replace(
            original,
            lifecycle=STATE,
            record_version=original.record_version + 1,
            last_reviewed_at=event["at"],
            history=(*original.history, event),
        )
        if (
            record.lifecycle != STATE
            or not item.clean
            or receipt["contract"] != CONTRACT
            or receipt["receipt_id"] != receipt_id
            or receipt["plan_id"] != _digest(plan)
            or event["event"] != EVENT
            or event["from"] != original.lifecycle
            or event["to"] != STATE
            or event["owner"] != original.owner
            or event["closeout_plan_id"] != receipt["plan_id"]
            or event["closeout_receipt_sha256"]
            != _digest({key: value for key, value in receipt.items() if key != "after_record"})
            or plan["repository_id"] != repository_id
            or original.lifecycle not in {"RETAINED", "PRESERVED"}
            or plan["owner_release"] != original.owner
            or not plan["actor"].strip()
            or plan["target_path"] != str(item.path)
            or plan["head"] != item.head
            or plan["branch"] != item.branch
            or plan["task_completed"] is not False
            or record_to_dict(record) != after
            or record_to_dict(expected) != after
        ):
            raise ValueError("closeout evidence changed")
    except (KeyError, TypeError, ValueError, IndexError, OSError, ControllerError) as exc:
        raise ControllerError(
            "DELIVERED_CLOSEOUT_INVALID", "closeout receipt or checkout identity changed"
        ) from exc
    return receipt


def validate_release(
    repository: RepositoryInventory,
    snapshot: MetadataSnapshot,
    item: WorktreeGitState,
    record: MetadataRecord,
) -> None:
    receipt = verify_receipt(
        MetadataStore(repository.common_dir), item, record, snapshot.repository_id
    )
    publication = _eligible(
        repository, snapshot, item, record, state=STATE, allow_registered_venv=True
    )
    previous = receipt["plan"]["publication"]
    if any(
        previous[key] != publication[key] for key in ("remote", "ref", "tracking_ref")
    ) or not is_ancestor(repository.canonical_path, previous["oid"], publication["oid"]):
        raise ControllerError(
            "PUBLICATION_DRIFT", "publication no longer extends the closeout evidence"
        )


def close_delivered(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner_release: str,
    actor: str,
    dry_run: bool = False,
    plan_id: str | None = None,
) -> dict[str, Any]:
    from .controller import envelope
    from .record_updates import updated_record
    from .semantics import assert_record_version as _cas
    from .semantics import managed_record as _managed_record

    if not actor.strip():
        raise ControllerError("INVALID_ACTOR", "closeout requires a named governance actor")
    if not dry_run and not plan_id:
        raise ControllerError(
            "CLOSEOUT_PLAN_REQUIRED", "execute only an exact reviewed closeout plan"
        )
    initial = inventory(repository_path)
    store = MetadataStore(initial.common_dir)
    with store.lock(exclusive=not dry_run):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        if record.owner != owner_release:
            raise ControllerError(
                "OWNER_MISMATCH", "closeout requires the recorded owner's release authority"
            )
        if record.lifecycle == STATE and not dry_run:
            receipt = verify_receipt(store, item, record, snapshot.repository_id)
            if (
                receipt["plan_id"] != plan_id
                or receipt["plan"]["actor"] != actor.strip()
                or receipt["plan"]["record"]["record_version"] != expected_record_version
            ):
                raise ControllerError(
                    "CLOSEOUT_PLAN_DRIFT", "retry does not match the recorded closeout"
                )
            code = "ALREADY_CLOSED"
        else:
            _cas(record, expected_record_version)
            if record.lifecycle not in {"RETAINED", "PRESERVED"}:
                raise ControllerError(
                    "INVALID_LIFECYCLE", "only retained or legacy preserved checkouts qualify"
                )
            if record.lifecycle == "PRESERVED":
                from .source_cleanup import verify_completed

                if verify_completed(store, item, record):
                    raise ControllerError(
                        "PRESERVATION_FLOW_REQUIRED",
                        "verified archived source must use its existing preservation release flow",
                    )
            publication = _eligible(
                repository,
                snapshot,
                item,
                record,
                state=record.lifecycle,
                allow_registered_venv=record.lifecycle == "RETAINED",
            )
            plan = {
                "contract": CONTRACT,
                "repository_id": snapshot.repository_id,
                "target_path": str(item.path),
                "head": item.head,
                "branch": item.branch,
                "record": record_to_dict(record),
                "publication": publication,
                "owner_release": owner_release,
                "actor": actor.strip(),
                "task_completed": False,
            }
            identity = _digest(plan)
            if dry_run:
                result = envelope("close-delivered")
                result["decision"] = {
                    "code": "CLOSEOUT_READY",
                    "plan_id": identity,
                    "allocation_id": record.allocation_id,
                    "record_version": record.record_version,
                    "reason": "checkout and historical commits are contained in live publication",
                }
                result["closeout_plan"] = plan
                return result
            if plan_id != identity:
                raise ControllerError("CLOSEOUT_PLAN_DRIFT", "closeout facts changed after review")
            receipt_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{CONTRACT}:{identity}"))
            path = store.completion_receipts_dir / f"{receipt_id}.json"
            if path.exists():
                receipt = store._read_json(path)
                if receipt.get("plan") != plan or receipt.get("plan_id") != identity:
                    raise ControllerError(
                        "CLOSEOUT_PLAN_DRIFT", "existing closeout evidence differs"
                    )
                updated = record_from_dict(receipt["after_record"])
                verify_receipt(store, item, updated, snapshot.repository_id)
            else:
                receipt = {
                    "contract": CONTRACT,
                    "receipt_id": receipt_id,
                    "plan_id": identity,
                    "plan": plan,
                }
                updated = updated_record(record, EVENT, lifecycle=STATE)
                event = {
                    **updated.history[-1],
                    "closeout_receipt_id": receipt_id,
                    "closeout_plan_id": identity,
                    "closeout_receipt_sha256": _digest(receipt),
                }
                updated = replace(updated, history=(*updated.history[:-1], event))
                receipt["after_record"] = record_to_dict(updated)
                store.write_completion_receipt(receipt_id, receipt)
            # Git may be changed by a process outside the Controller's repository lock.
            current, latest = _managed_record(
                inventory(repository_path), store._load_unlocked(), target
            )
            if current != item or latest != record:
                raise ControllerError(
                    "CLOSEOUT_PLAN_DRIFT", "checkout changed before metadata transition"
                )
            store.replace_record(updated)
            code = "CHECKOUT_CLOSED"
    result = envelope("close-delivered")
    result["decision"] = {
        "code": code,
        "reason": "published checkout closed; ordinary guarded release remains required",
        "allocation_id": record.allocation_id,
        "record_version": receipt["after_record"]["record_version"],
        "plan_id": receipt["plan_id"],
        "receipt_id": receipt["receipt_id"],
        "task_completed": False,
    }
    return result


def archive_retained(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner_release: str,
    actor: str,
    dry_run: bool = False,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Freeze a clean, unfinished retained task for guarded physical release."""
    from .controller import envelope
    from .record_updates import updated_record
    from .semantics import assert_record_version as _cas
    from .semantics import managed_record as _managed_record

    if not actor.strip():
        raise ControllerError("INVALID_ACTOR", "archive requires a named governance actor")
    if not dry_run and not plan_id:
        raise ControllerError(
            "ARCHIVE_PLAN_REQUIRED", "execute only an exact reviewed archive plan"
        )
    initial = inventory(repository_path)
    store = MetadataStore(initial.common_dir)
    with store.lock(exclusive=not dry_run):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        if record.owner != owner_release:
            raise ControllerError("OWNER_MISMATCH", "archive requires the recorded owner")
        if record.lifecycle == ARCHIVE_STATE and not dry_run:
            receipt = verify_archive_receipt(store, item, record, snapshot.repository_id)
            if (
                receipt["plan_id"] != plan_id
                or receipt["plan"]["actor"] != actor.strip()
                or receipt["plan"]["record"]["record_version"] != expected_record_version
            ):
                raise ControllerError("ARCHIVE_PLAN_DRIFT", "retry does not match the archive")
            code = "ALREADY_ARCHIVED"
        else:
            _cas(record, expected_record_version)
            from .transaction_scope import conflicts

            if conflicts(store, allocation=record.allocation_id, target=item.path):
                raise ControllerError(
                    "INTERRUPTED_TRANSACTION", "archive target has an unresolved transaction"
                )
            _eligible(
                repository,
                snapshot,
                item,
                record,
                state="RETAINED",
                require_publication=False,
                allow_registered_venv=True,
            )
            from .lifecycle import _release_resource_actions

            _release_resource_actions(repository, item, record)
            from .preserved_cleanup import cleanup_receipts

            cleanup_refs = cleanup_receipts(store, item, record, snapshot.repository_id)
            plan = {
                "contract": ARCHIVE_CONTRACT,
                "repository_id": snapshot.repository_id,
                "target_path": str(item.path),
                "head": item.head,
                "branch": item.branch,
                "record": record_to_dict(record),
                "owner_release": owner_release,
                "actor": actor.strip(),
                "task_completed": False,
                "integration_ready": False,
                "publication_asserted": False,
                **({"preservation_cleanup_receipts": cleanup_refs} if cleanup_refs else {}),
            }
            identity = _digest(plan)
            if dry_run:
                result = envelope("archive-retained")
                result["decision"] = {
                    "code": "ARCHIVE_READY",
                    "plan_id": identity,
                    "allocation_id": record.allocation_id,
                    "record_version": record.record_version,
                    "reason": "clean unfinished checkout is retained by its exact branch; no delivery is asserted",
                }
                result["archive_plan"] = plan
                return result
            if plan_id != identity:
                raise ControllerError("ARCHIVE_PLAN_DRIFT", "archive facts changed after review")
            receipt_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{ARCHIVE_CONTRACT}:{identity}"))
            path = store.completion_receipts_dir / f"{receipt_id}.json"
            if path.exists():
                receipt = store._read_json(path)
                if receipt.get("plan") != plan or receipt.get("plan_id") != identity:
                    raise ControllerError("ARCHIVE_PLAN_DRIFT", "existing archive evidence differs")
                updated = record_from_dict(receipt["after_record"])
                verify_archive_receipt(store, item, updated, snapshot.repository_id)
            else:
                receipt = {
                    "contract": ARCHIVE_CONTRACT,
                    "receipt_id": receipt_id,
                    "plan_id": identity,
                    "plan": plan,
                }
                updated = updated_record(record, ARCHIVE_EVENT, lifecycle=ARCHIVE_STATE)
                event = {
                    **updated.history[-1],
                    "archive_receipt_id": receipt_id,
                    "archive_plan_id": identity,
                    "archive_receipt_sha256": _digest(receipt),
                }
                updated = replace(updated, history=(*updated.history[:-1], event))
                receipt["after_record"] = record_to_dict(updated)
                store.write_completion_receipt(receipt_id, receipt)
            current, latest = _managed_record(
                inventory(repository_path), store._load_unlocked(), target
            )
            if current != item or latest != record:
                raise ControllerError(
                    "ARCHIVE_PLAN_DRIFT", "checkout changed before metadata transition"
                )
            store.replace_record(updated)
            code = "ARCHIVED"
    result = envelope("archive-retained")
    result["decision"] = {
        "code": code,
        "reason": "unfinished task and branch preserved; guarded release remains required",
        "allocation_id": record.allocation_id,
        "record_version": receipt["after_record"]["record_version"],
        "plan_id": receipt["plan_id"],
        "receipt_id": receipt["receipt_id"],
        "task_completed": False,
    }
    return result
