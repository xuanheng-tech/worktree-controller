"""One deterministic reconciliation engine, shared by every trigger.

Admission, the background timer and the operator command all call
:func:`reconcile_writers`, so a writer slot is reclaimed under exactly one set of
rules no matter what asked for it.

The engine only ever answers one question: *can this allocation's writer
entitlement be proven inactive right now?* A proven-dead writer releases the slot
by moving ``ACTIVE -> RETAINED``. Nothing here completes a task, publishes,
finalizes, deletes a checkout or touches uncommitted work, and every outcome
carries the evidence that produced it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .controller import status
from .git_backend import inventory
from .lifecycle import _cancel_release_intents
from .models import (
    ControllerError,
    MetadataRecord,
    MetadataSnapshot,
    WriterReconciliationReceipt,
)
from .policy import load_policy
from .record_updates import updated_record
from .semantics import record_for as _record_for
from .store import WRITER_RECEIPT_SCHEMA_VERSION, MetadataStore
from .writer_lease import (
    ProcessPathScan,
    holder_is_live,
    now,
    paths_in_use,
    process_group_is_live,
)

TRIGGERS = ("manual", "admission", "background")

#: Outcomes that leave the allocation exactly as it was found.
BLOCKED = "BLOCKED"
LIVE = "LIVE_WRITER"
RECLAIMABLE = "RECLAIMABLE"
RECLAIMED = "RECLAIMED"
LEGACY = "LEGACY_UNVERIFIED"
ADOPTED = "LEGACY_LEASE_ADOPTED"
SKIPPED = "OUT_OF_WRITER_POOL"
STRANDED = "STRANDED_ENTITLEMENT"
STRANDED_CLEARED = "STRANDED_ENTITLEMENT_CLEARED"


def _recovery_pending(record: MetadataRecord) -> bool:
    return bool(
        record.recovery_ref
        or record.recovery_allocation_id
        or record.lifecycle in {"RECOVERY_REQUIRED", "QUARANTINED"}
    )


def _integration_bound(snapshot: MetadataSnapshot, allocation_id: str) -> bool:
    for intent in snapshot.integration_intents.values():
        if intent.allocation_id == allocation_id:
            return True
    return False


def classify(
    item: Any,
    record: MetadataRecord,
    snapshot: MetadataSnapshot,
    in_use: Callable[[Path], list[tuple[int, str]]] = paths_in_use,
) -> tuple[str, str, list[str]]:
    """Decide what may safely happen to one allocation, and why.

    Ordering matters: every gate that could make reclamation unsafe is checked
    before the writer evidence, so a blocked allocation reports the real obstacle
    instead of a lease verdict that would never have been acted on.
    """

    evidence: list[str] = []

    if item.canonical:
        return SKIPPED, "canonical is never reconciled", evidence
    if record.role not in {"feature", "audit"}:
        return SKIPPED, f"role {record.role} is outside the writer pool", evidence
    if record.lifecycle == "RETAINED":
        return SKIPPED, "already retained; writer slot is already free", evidence
    if record.lifecycle != "ACTIVE":
        return BLOCKED, f"lifecycle {record.lifecycle} is not reclaimable", evidence
    if record.external_bindings:
        evidence.append(f"external_bindings={list(record.external_bindings)}")
        return BLOCKED, "registered external or runtime bindings", evidence
    if snapshot.publication_lease is not None and (
        snapshot.publication_lease.allocation_id == record.allocation_id
    ):
        return BLOCKED, "a publication lease is in progress", evidence
    if _integration_bound(snapshot, record.allocation_id):
        return BLOCKED, "an integration intent still binds this allocation", evidence
    if _recovery_pending(record):
        return BLOCKED, "unresolved recovery state", evidence
    if item.detached:
        return BLOCKED, "detached worktree requires review", evidence
    if item.locked or item.prunable:
        return BLOCKED, "locked or prunable worktree requires review", evidence

    # Writer evidence. A live process anywhere in the checkout outranks the lease
    # record, so an unleased or externally started writer is never overlooked.
    used = in_use(Path(str(item.path)))
    if used:
        evidence.append("live_paths=" + ",".join(f"{pid}:{cmd[:40]}" for pid, cmd in used[:3]))
        return LIVE, "a live process is using this checkout", evidence

    lease = snapshot.writer_leases.get(record.allocation_id)
    if lease is None:
        evidence.append("writer_lease=absent")
        return (
            LEGACY,
            "no writer lease exists and this allocation predates leases; "
            "absence is not proof of inactivity",
            evidence,
        )

    evidence.append(
        f"writer_lease={lease.lease_id} holder={lease.holder.client_id}:{lease.holder.pid}"
    )
    if holder_is_live(lease.holder):
        evidence.append("holder_process=alive")
        return LIVE, "the lease holder process is still running", evidence
    if process_group_is_live(lease.holder):
        evidence.append("holder_process_group=alive")
        return LIVE, "a surviving child of the lease holder is still running", evidence

    evidence.append("holder_process=gone")
    evidence.append("holder_process_group=gone")
    # Retention relabels a checkout the ordinary pool already holds. It takes no
    # capacity, so a full retained count can no longer strand a dead writer.
    return (
        RECLAIMABLE,
        "the writer lease holder is gone; the task itself is untouched",
        evidence,
    )


def reconcile_writers(
    repository_path: Path,
    *,
    actor: str,
    trigger: str = "manual",
    dry_run: bool = True,
    adopt_legacy: bool = False,
    only: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Report, and optionally converge, writer entitlement across a repository.

    ``only`` restricts convergence to named task keys. Everything else is still
    inspected and reported, but is left exactly as found. A one-time legacy
    classification is rarely safe for a whole repository at once: the allocations
    that can be positively tied to a live session, or that show recent work, have
    to be excluded by name rather than swept up with the rest.
    """

    if trigger not in TRIGGERS:
        raise ControllerError("INVALID_TRIGGER", f"unsupported reconcile trigger: {trigger}")
    if not actor.strip():
        raise ControllerError("INVALID_ACTOR", "a reconciler actor is required")

    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    findings: list[dict[str, Any]] = []
    reclaimed = 0
    converged = 0
    adopted = 0

    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        policy = load_policy(repository.canonical_path)
        if policy.reconciler_enabled and any(
            record.owner == policy.reconciler_actor and record.lifecycle != "RELEASED"
            for record in snapshot.records.values()
        ):
            raise ControllerError(
                "INVALID_POLICY", "the reconciler actor must be distinct from current task owners"
            )
        if not dry_run:
            if policy.retained_slots < 1:
                raise ControllerError(
                    "RETENTION_UNSUPPORTED",
                    "repository policy does not configure a retained pool",
                )
            if not policy.reconciler_enabled:
                raise ControllerError(
                    "RECONCILER_DISABLED",
                    "repository policy does not delegate automatic reconciliation",
                )
            if actor != policy.reconciler_actor:
                raise ControllerError(
                    "RECONCILER_ACTOR_MISMATCH",
                    "actor is not the delegated reconciler for this repository",
                )
            if adopt_legacy and not policy.reconciler_legacy_adoption:
                raise ControllerError(
                    "LEGACY_ADOPTION_DISABLED",
                    "repository policy does not permit legacy writer adoption",
                )

        retained_limit = policy.retained_slots
        retained_used = sum(
            1 for other in snapshot.records.values() if other.lifecycle == "RETAINED"
        )
        retained_before = retained_used
        # Only registered checkouts occupy the pool. Counting every historical
        # record would include released tasks and report nonsense capacity.
        registered = [
            found
            for found in (_record_for(item, snapshot) for item in repository.worktrees)
            if found is not None
        ]
        writers_before = sum(
            1
            for other in registered
            if other.role in {"feature", "audit"} and other.lifecycle != "RETAINED"
        )
        legacy_unverified = 0
        # One process reading for the whole pass: this runs under the exclusive
        # repository lock, and a reading per checkout held it for seconds.
        process_scan = ProcessPathScan()

        for item in repository.worktrees:
            record = _record_for(item, snapshot)
            if record is None:
                findings.append(
                    {
                        "path": str(item.path),
                        "outcome": BLOCKED,
                        "reason": "worktree has no controller record",
                        "evidence": [],
                    }
                )
                continue

            outcome, reason, evidence = classify(
                item,
                record,
                snapshot,
                process_scan.in_use,
            )

            # A legacy allocation is only ever converged by an explicit, one-time
            # operator classification - never by the automatic triggers.
            if outcome is LEGACY and adopt_legacy and (not only or record.task_key in only):
                outcome = RECLAIMABLE
                reason = "one-time legacy classification: no live writer evidence of any kind"
                evidence.append("legacy_classification=adopted")
                adopted += 1

            if only and record.task_key not in only and outcome is RECLAIMABLE:
                outcome = BLOCKED
                reason = "excluded from this pass by an explicit task selection"

            if outcome is LEGACY:
                legacy_unverified += 1

            applied = outcome
            if outcome is RECLAIMABLE:
                # counted for both a preview and a real pass, so the reported
                # arithmetic is the same either way
                retained_used += 1
                converged += 1
            if outcome is RECLAIMABLE and not dry_run:
                _cancel_release_intents(
                    store,
                    snapshot,
                    record.allocation_id,
                    event="RECORD_CHANGED",
                    blocker="PARKED",
                )
                updated = updated_record(record, "PARKED", lifecycle="RETAINED")
                store.replace_record(updated)
                store.remove_writer_lease(record.allocation_id)
                receipt = WriterReconciliationReceipt(
                    schema_version=WRITER_RECEIPT_SCHEMA_VERSION,
                    receipt_id=str(uuid.uuid4()),
                    repository_id=snapshot.repository_id or "unknown",
                    allocation_id=record.allocation_id,
                    actor=actor,
                    owner=record.owner,
                    trigger=trigger,
                    outcome=RECLAIMED,
                    reason=reason,
                    evidence=tuple(evidence),
                    previous_lifecycle=record.lifecycle,
                    new_lifecycle="RETAINED",
                    reconciled_at=now(),
                )
                store.write_writer_receipt(receipt)
                reclaimed += 1
                applied = RECLAIMED

            findings.append(
                {
                    "task_key": record.task_key,
                    "path": str(item.path),
                    "role": record.role,
                    "owner": record.owner,
                    "lifecycle": record.lifecycle,
                    "outcome": applied,
                    "reason": reason,
                    "evidence": evidence,
                }
            )

        stranded, cleared = _converge_stranded_entitlement(
            store,
            snapshot,
            findings,
            actor=actor,
            trigger=trigger,
            dry_run=dry_run,
        )

    payload = status(repository_path)
    payload["command"] = "reconcile-writers"
    payload["decision"] = {
        "code": ("WRITERS_RECONCILED" if converged or stranded else "NO_WRITER_ACTION"),
        "reason": (
            f"{converged} writer slot(s) {'reclaimed' if not dry_run else 'would be reclaimed'}"
            if converged
            else "no allocation had a provably inactive writer"
        ),
        "actor": actor,
        "trigger": trigger,
        "dry_run": dry_run,
        "reclaimed": reclaimed,
        "would_reclaim": converged,
        "stranded_entitlement": stranded,
        "stranded_entitlement_cleared": cleared,
        "adopted": adopted,
        "legacy_unverified": legacy_unverified,
        "selection": list(only),
        "writers_before": writers_before,
        "writers_after": writers_before - converged,
        "retained_before": retained_before,
        "retained_after": retained_used,
        "retained_limit": retained_limit,
        "findings": findings,
    }
    return payload


def _converge_stranded_entitlement(
    store: MetadataStore,
    snapshot: MetadataSnapshot,
    findings: list[dict[str, Any]],
    *,
    actor: str,
    trigger: str,
    dry_run: bool,
) -> tuple[int, int]:
    """Drop writer entitlement whose allocation has reached a terminal state.

    A lease is only ever granted to an ACTIVE allocation, so once its allocation is
    released or gone the lease can grant nothing. It is not harmless: every reader
    of the lease directory, including the rollout gate, still sees a live holder and
    reports a writer that no longer has a task. Liveness is deliberately not
    consulted here, because the holder being alive says nothing about an entitlement
    whose allocation is finished.

    A parked, frozen or otherwise non-ACTIVE allocation is still someone's lane: it
    can return to ACTIVE, and a live holder may be about to resume it, so its lease is
    kept while the holder or its process group is alive. Once both are provably gone
    the lease can grant nothing (resuming requires a new lease) and is swept too;
    otherwise it would mark the checkout as in use forever. The allocation itself is
    never touched.
    """

    stranded = 0
    cleared = 0
    for allocation_id, lease in sorted(snapshot.writer_leases.items()):
        record = snapshot.records.get(allocation_id)
        if record is not None and record.lifecycle == "ACTIVE":
            continue
        if (
            record is not None
            and record.lifecycle != "RELEASED"
            and (holder_is_live(lease.holder) or process_group_is_live(lease.holder))
        ):
            continue
        lifecycle = record.lifecycle if record is not None else "MISSING_RECORD"
        stranded += 1
        finding = {
            "task_key": record.task_key if record is not None else None,
            "path": lease.worktree_path,
            "role": record.role if record is not None else None,
            "owner": lease.owner,
            "lifecycle": lifecycle,
            "outcome": STRANDED,
            "reason": (
                f"terminal lifecycle {lifecycle} cannot hold writer entitlement"
                if lifecycle in {"RELEASED", "MISSING_RECORD"}
                else f"non-ACTIVE lifecycle {lifecycle} lease holder is gone"
            ),
            "evidence": [f"writer_lease={lease.lease_id} holder={lease.holder.client_id}"],
        }
        if not dry_run:
            store.remove_writer_lease(allocation_id)
            store.write_writer_receipt(
                WriterReconciliationReceipt(
                    schema_version=WRITER_RECEIPT_SCHEMA_VERSION,
                    receipt_id=str(uuid.uuid4()),
                    repository_id=snapshot.repository_id or "unknown",
                    allocation_id=allocation_id,
                    actor=actor,
                    owner=lease.owner,
                    trigger=trigger,
                    outcome=STRANDED_CLEARED,
                    reason=finding["reason"],
                    evidence=tuple(finding["evidence"]),
                    previous_lifecycle=lifecycle,
                    new_lifecycle=lifecycle,
                    reconciled_at=now(),
                )
            )
            cleared += 1
            finding["outcome"] = STRANDED_CLEARED
        findings.append(finding)
    return stranded, cleared
