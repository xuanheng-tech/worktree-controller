"""Read-only ownership and session observations over existing Controller facts.

These findings do not transition lifecycle state or establish release eligibility.
A lease renewal is the only available heartbeat proxy; absence of a visible holder
does not prove that its task or descendant processes have ended.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import (
    ControllerError,
    MetadataRecord,
    MetadataSnapshot,
    RepositoryInventory,
    WorktreeGitState,
    WorktreePolicy,
    WriterHolder,
)
from .writer_lease import holder_is_live, runtime_binding


def _holder_status(
    holder: WriterHolder,
    *,
    namespace_healthy: bool,
    probe: Callable[[WriterHolder], bool],
) -> str:
    if not namespace_healthy:
        return "UNKNOWN"
    try:
        return "LIVE" if probe(holder) else "NOT_VISIBLE"
    except (ControllerError, OSError, ValueError):
        return "UNKNOWN"


def _timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def observe_worktree(
    item: WorktreeGitState,
    record: MetadataRecord | None,
    snapshot: MetadataSnapshot,
    policy: WorktreePolicy,
    *,
    now_at: datetime | None = None,
    holder_probe: Callable[[WriterHolder], bool] | None = None,
    namespace_healthy: bool = True,
) -> dict[str, Any]:
    """Describe recorded ownership and best-effort holder visibility, without mutation.

    ``NOT_VISIBLE`` is limited to this process's /proc view. It never asserts that
    a session, its process group, runtime bindings, or the task has ended.
    """
    observed_at = now_at or datetime.now(UTC)
    if observed_at.tzinfo is None:
        raise ValueError("observation time must include a timezone")
    probe = holder_probe or holder_is_live
    allocation_id = snapshot.bindings.get(item.key)
    lease = snapshot.writer_leases.get(allocation_id) if allocation_id else None
    stale_facts: list[str] = []
    orphan_facts: list[str] = []
    integrity_facts: list[str] = []
    if allocation_id is None:
        orphan_facts.append("UNMANAGED_WORKTREE")
    elif record is None:
        orphan_facts.append("MISSING_RECORD")
    elif record.lifecycle == "RELEASED":
        orphan_facts.append("RELEASED_REGISTRATION")

    review_status = "UNKNOWN"
    if record is not None:
        reviewed = _timestamp(record.last_reviewed_at)
        if reviewed is not None:
            review_status = (
                "OVERDUE"
                if observed_at - reviewed >= timedelta(days=policy.stale_warning_days)
                else "CURRENT"
            )
            if review_status == "OVERDUE":
                stale_facts.append("REVIEW_OVERDUE")

    holder_status = "UNKNOWN"
    session: dict[str, Any] = {
        "source": "NONE",
        "client_id": None,
        "session_id": None,
        "run_id": None,
        "holder_pid": None,
    }
    lease_observation: dict[str, Any] = {
        "present": lease is not None,
        "lease_id": None,
        "owner": None,
        "acquired_at": None,
        "renewed_at": None,
        "expires_at": None,
        "status": "ABSENT",
    }
    heartbeat = {"source": "NONE", "last_at": None, "independent": False}
    if lease is not None:
        holder = lease.holder
        holder_status = _holder_status(holder, namespace_healthy=namespace_healthy, probe=probe)
        session = {
            "source": "WRITER_LEASE",
            "client_id": holder.client_id,
            "session_id": holder.session_id,
            "run_id": holder.run_id,
            "holder_pid": holder.pid,
        }
        if lease.expires_at is None:
            expiry_status = "NO_TTL"
        elif (expiry := _timestamp(lease.expires_at)) is None:
            expiry_status = "UNKNOWN"
        elif expiry < observed_at:
            expiry_status = "EXPIRED"
            stale_facts.append("LEASE_EXPIRED")
        else:
            expiry_status = "CURRENT"
        lease_observation = {
            "present": True,
            "lease_id": lease.lease_id,
            "owner": lease.owner,
            "acquired_at": lease.acquired_at,
            "renewed_at": lease.renewed_at,
            "expires_at": lease.expires_at,
            "status": expiry_status,
        }
        heartbeat = {
            "source": "LEASE_RENEWAL",
            "last_at": lease.renewed_at,
            "independent": False,
        }
        if holder_status == "NOT_VISIBLE":
            stale_facts.append("HOLDER_NOT_VISIBLE")
        if record is None or record.lifecycle == "RELEASED":
            orphan_facts.append("STRANDED_WRITER_LEASE")

    runtime_sessions: list[dict[str, Any]] = []
    if record is not None:
        for binding in record.external_bindings:
            try:
                decoded = runtime_binding(binding)
            except ControllerError:
                integrity_facts.append("INVALID_RUNTIME_BINDING")
                continue
            if decoded is None:
                continue
            holder, bound_lease_id = decoded
            runtime_sessions.append(
                {
                    "source": "RUNTIME_BINDING",
                    "client_id": holder.client_id,
                    "session_id": holder.session_id,
                    "run_id": holder.run_id,
                    "holder_pid": holder.pid,
                    "lease_id": bound_lease_id,
                    "occupancy": "RECORDED",
                }
            )

    return {
        "allocation_id": allocation_id,
        "owner": record.owner if record else None,
        "lifecycle": record.lifecycle if record else None,
        "session": session,
        "runtime_sessions": runtime_sessions,
        "lease": lease_observation,
        "heartbeat": heartbeat,
        "status": {
            "owner": "UNKNOWN"
            if record is None
            else "UNASSIGNED"
            if record.owner == "unassigned"
            else "ASSIGNED",
            "holder": holder_status,
            "review": review_status,
            "host_session": "UNKNOWN",
        },
        "stale_facts": stale_facts,
        "orphan_facts": orphan_facts,
        "integrity_facts": integrity_facts,
        "advisory_only": True,
    }


def observe_repository_orphans(
    repository: RepositoryInventory, snapshot: MetadataSnapshot
) -> list[dict[str, str]]:
    """Find structural registry/metadata orphans; make no cleanup decision."""
    findings: list[dict[str, str]] = []
    items = {item.key: item for item in repository.worktrees}
    keys_by_allocation: defaultdict[str, list[str]] = defaultdict(list)
    for key, allocation_id in sorted(snapshot.bindings.items()):
        keys_by_allocation[allocation_id].append(key)
        if key not in items:
            findings.append(
                {"code": "ORPHAN_BINDING", "allocation_id": allocation_id, "worktree_key": key}
            )
        if allocation_id not in snapshot.records:
            findings.append(
                {"code": "MISSING_RECORD", "allocation_id": allocation_id, "worktree_key": key}
            )
    for allocation_id, record in sorted(snapshot.records.items()):
        keys = keys_by_allocation.get(allocation_id, [])
        if not keys and record.lifecycle != "RELEASED":
            findings.append({"code": "ORPHAN_RECORD", "allocation_id": allocation_id})
        elif len(keys) > 1:
            findings.append({"code": "DUPLICATE_BINDING", "allocation_id": allocation_id})
    for item in repository.worktrees:
        if snapshot.bindings.get(item.key) not in snapshot.records:
            findings.append({"code": "UNMANAGED_WORKTREE", "path": str(item.path)})
    for allocation_id in sorted(snapshot.writer_leases):
        record = snapshot.records.get(allocation_id)
        if record is None or record.lifecycle == "RELEASED":
            findings.append({"code": "STRANDED_WRITER_LEASE", "allocation_id": allocation_id})
    return sorted(
        findings,
        key=lambda row: (
            row["code"],
            row.get("allocation_id", ""),
            row.get("worktree_key", ""),
            row.get("path", ""),
        ),
    )
