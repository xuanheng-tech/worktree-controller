"""One-process admission of an executor session (``session_admission_version`` 1).

A host that launches a governed executor would otherwise run every step below as
its own Controller command: each one a new process that loads the repository
metadata again and attaches a full repository status to its answer. These two
commands run the same step functions, in the same order, with the same guards,
journals, receipts and CAS checks. They only share one process and skip the
status each intermediate step would otherwise attach.

``session-admit`` covers the common start only: a new allocation acquired from
the canonical checkout, then execution admission. Every other start (an existing
allocation, a non-canonical workspace, a resumed checkout, a capacity decision
other than ``CREATE_ALLOWED``) is answered with ``SESSION_ADMISSION_FALLBACK`` and
nothing is changed, so the host keeps using its step-by-step path for it.

``session-bind`` binds write entitlement to the live executor process: the lease
for the exact holder, the holder-aware write guard, then the runtime occupancy
binding. A failure after the lease was granted releases that same lease before
the error is returned, so a refused bind leaves no entitlement behind.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .controller import _plan_from_snapshot, envelope, guard_write
from .git_backend import inventory
from .lifecycle import acquire, update_external_binding
from .models import ControllerError, WriterHolder
from .policy import load_policy
from .semantics import managed_record as _managed_record
from .semantics import record_for as _record_for
from .store import MetadataStore
from .writer_lease import RUNTIME_BINDING_PREFIX, acquire_writer_lease, release_writer_lease

SESSION_ADMISSION_VERSION = 1
RUNTIME_KINDS = frozenset({"agent", "command", "test"})


def _fallback(command: str, reason: str, **extra: Any) -> dict[str, Any]:
    payload = envelope(command)
    payload["decision"] = {
        "code": "SESSION_ADMISSION_FALLBACK",
        "reason": reason,
        "changed": False,
        **extra,
    }
    return payload


def admit_session(
    repository_path: Path,
    *,
    task_key: str,
    authority_key: str,
    module: str,
    owner: str,
    branch: str,
    base_ref: str,
    client_id: str,
    session_id: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Acquire a new feature allocation and admit its execution, or change nothing."""

    command = "session-admit"
    repository = inventory(repository_path)
    requested = repository_path.expanduser().resolve(strict=True)
    if requested != repository.canonical_path:
        return _fallback(command, "workspace is not the canonical checkout")
    snapshot = MetadataStore(repository.common_dir).load()
    for item in repository.worktrees:
        record = _record_for(item, snapshot)
        if record is not None and record.task_key == task_key:
            return _fallback(
                command,
                "an allocation for this task is already registered",
                allocation_id=record.allocation_id,
            )
    planned = _plan_from_snapshot(
        repository,
        load_policy(repository.canonical_path),
        snapshot,
        task_key=task_key,
        authority_key=authority_key,
        module=module,
        role="feature",
    )
    planned_decision = planned.get("decision")
    if not isinstance(planned_decision, dict) or planned_decision.get("code") != "CREATE_ALLOWED":
        return _fallback(command, "plan did not allow an immediate checkout", plan=planned_decision)
    acquired = acquire(
        repository_path,
        task_key=task_key,
        authority_key=authority_key,
        module=module,
        role="feature",
        owner=owner,
        branch=branch,
        base_ref=base_ref,
        observe=False,
    )
    decision = acquired["decision"]
    if decision.get("code") != "ACQUIRED":
        # RESOURCE_WAIT and similar outcomes keep their own durable request; the
        # host handles them exactly as it handles a step-by-step acquire.
        acquired["command"] = command
        return acquired
    allocation = {
        "canonical_path": str(repository.canonical_path),
        "path": decision["path"],
        "allocation_id": decision["allocation_id"],
        "task_key": task_key,
        "authority_key": authority_key,
        "owner": owner,
        "role": "feature",
        "lifecycle": "ACTIVE",
        "branch": decision["branch"],
        "head": decision["head"],
    }
    execution: dict[str, Any] | None = None
    policy = load_policy(repository.canonical_path)
    if policy.resource_admission:
        from .resource_admission import admit_execution

        execution = admit_execution(
            repository_path,
            target=Path(decision["path"]),
            owner=owner,
            client_id=client_id,
            session_id=session_id,
            run_id=run_id,
        )["decision"]
    payload = envelope(command)
    payload["decision"] = {
        "code": "SESSION_ADMITTED",
        "reason": "new allocation acquired and its execution admitted"
        if execution is not None
        else "new allocation acquired",
        "allocation": allocation,
        "admission_strategy": "resource-budget-v1" if policy.resource_admission else None,
        "execution": execution,
        "session_admission_version": SESSION_ADMISSION_VERSION,
    }
    return payload


def runtime_binding_value(holder: dict[str, Any], lease_id: str, kind: str) -> str:
    """The typed occupancy identity, in the exact form the registry decodes."""
    identity = {
        "holder": {
            "pid": holder["pid"],
            "boot_id": holder["boot_id"],
            "start_ticks": holder["start_ticks"],
            "client_id": holder["client_id"],
            "session_id": holder["session_id"],
            "run_id": holder["run_id"],
        },
        "lease_id": lease_id,
        "kind": kind,
    }
    return RUNTIME_BINDING_PREFIX + json.dumps(identity, sort_keys=True, separators=(",", ":"))


def bind_session(
    repository_path: Path,
    *,
    target: Path,
    task_key: str,
    authority_key: str,
    owner: str,
    client_id: str,
    holder_pid: int,
    session_id: str,
    run_id: str | None = None,
    kind: str = "agent",
) -> dict[str, Any]:
    """Grant, check and register one executor's write entitlement, or leave none."""

    if kind not in RUNTIME_KINDS:
        raise ControllerError("INVALID_BINDING", f"unsupported runtime kind: {kind}")
    repository = inventory(repository_path)
    snapshot = MetadataStore(repository.common_dir).load()
    _item, record = _managed_record(repository, snapshot, target)
    if (
        record.task_key != task_key
        or record.authority_key != authority_key
        or record.owner != owner
        or record.lifecycle != "ACTIVE"
    ):
        raise ControllerError(
            "SESSION_ALLOCATION_DRIFT", "allocation no longer matches the admitted session"
        )
    leased = acquire_writer_lease(
        repository_path,
        target=target,
        owner=owner,
        client_id=client_id,
        holder_pid=holder_pid,
        session_id=session_id,
        run_id=run_id,
        observe=False,
    )
    lease = leased["decision"]
    holder = lease["holder"]
    try:
        guarded = guard_write(
            repository_path,
            target=target,
            task_key=task_key,
            authority_key=authority_key,
            owner=owner,
            operation="feature-write",
            holder=WriterHolder(**holder),
            observe=False,
        )["decision"]
        if guarded.get("code") != "WRITE_ALLOWED":
            raise ControllerError(
                str(guarded.get("code") or "WRITE_GUARD_REFUSED"),
                str(guarded.get("reason") or "write guard refused the holder"),
            )
        binding = runtime_binding_value(holder, lease["lease_id"], kind)
        bound = update_external_binding(
            repository_path,
            target=target,
            expected_record_version=record.record_version,
            owner=owner,
            binding=binding,
            action="add",
            observe=False,
        )["decision"]
    except Exception as failure:
        outcome: dict[str, Any] = {"lease_id": lease["lease_id"], "lease_released": True}
        try:
            release_writer_lease(
                repository_path,
                target=target,
                holder_pid=holder["pid"],
                lease_id=lease["lease_id"],
                holder=WriterHolder(**holder),
            )
        except Exception as release_failure:
            # The host still knows this exact lease and releases it with its own cleanup.
            outcome = {
                "lease_id": lease["lease_id"],
                "lease_released": False,
                "lease": lease,
                "release_error": getattr(release_failure, "code", type(release_failure).__name__),
            }
        if isinstance(failure, (ControllerError, OSError)):
            failure.details = outcome  # type: ignore[attr-defined]
        raise
    payload = envelope("session-bind")
    payload["decision"] = {
        "code": "SESSION_BOUND",
        "reason": "write entitlement granted, checked and registered for the live executor",
        "lease": lease,
        "write_guard": guarded["code"],
        "runtime_binding": binding,
        "record_version": bound["record_version"],
        "session_admission_version": SESSION_ADMISSION_VERSION,
    }
    return payload
