"""Controller decisions over Git inventory and repository-local metadata."""

from __future__ import annotations

import os
import stat
from collections import defaultdict
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Any

from .git_backend import commit_tree_oid, git_state_fingerprint, inventory, resolve_commit
from .lifecycle_observation import observe_repository_orphans, observe_worktree
from .models import (
    ACTIVE_AUTHORITY_STATES,
    CANONICAL_ROLES,
    EMERGENCY_ROLES,
    FEATURE_AUTHORITY_ROLES,
    LIFECYCLE_STATES,
    ORDINARY_ROLES,
    RECOVERY_ROLES,
    SCHEMA_VERSION,
    WORKTREE_ROLES,
    ControllerError,
    MetadataRecord,
    MetadataSnapshot,
    RepositoryInventory,
    WorktreeGitState,
    WorktreePolicy,
    WriterHolder,
)
from .mount_diagnostics import diagnose_mounts, mount_warnings
from .policy import load_policy
from .semantics import (
    EVIDENCE_LIFECYCLES,
    effective_intent_state,
    evidence_head_issues,
    integration_candidate_issues,
    integration_queue_order,
    normalize_scope_paths,
    path_scope_issues,
    publication_ref,
    unallocated_integration_conflict,
)
from .semantics import record_for as _record_for
from .store import TIMEZONE, MetadataStore, new_record
from .writer_lease import check_writer_entitlement

WRITE_OPERATIONS = frozenset({"feature-write", "integration-write", "recovery", "finalization"})


def envelope(command: str) -> dict[str, Any]:
    from . import __version__

    return {
        "tool_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "repository": None,
        "decision": None,
        "worktrees": [],
        "warnings": [],
        "errors": [],
    }


def _warning(code: str, message: str, **details: str) -> dict[str, str]:
    return {"code": code, "message": message, **details}


def _retirement_execution_findings(
    store: MetadataStore,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Outstanding retirement grants and removals nobody can attest.

    A grant is live work in someone else's hands, so it is reported without being
    called a failure.  An abandoned removal is different: the ref is gone and the
    Controller never observed the authorized transition, so it must stay visible
    instead of leaving ``branch-retirement-status`` at PARTIAL with no explanation.
    """

    directory = store.branch_retirement_authorizations_dir
    if not directory.is_dir():
        return [], []
    outstanding: list[dict[str, str]] = []
    unattested: list[dict[str, str]] = []
    for path in sorted(directory.iterdir()):
        if not path.name.endswith(".json") or not path.is_file():
            continue
        try:
            value = dict(store._read_json(path))
        except ControllerError:
            unattested.append(
                {"path": path.name, "code": "INVALID_RETIREMENT_EXECUTION_RECORD"}
            )
            continue
        if path.name.endswith("-abandoned.json"):
            if value.get("classification") == "ABANDONED_UNATTESTED_REMOVAL":
                unattested.append(
                    {
                        "authorization_id": str(value.get("authorization_id", path.stem)),
                        "plan_id": str(value.get("plan_id", "")),
                        "operation": str(value.get("operation", "")),
                        "ref": str(value.get("ref", "")),
                        "code": "RETIREMENT_REMOVAL_UNATTESTED",
                    }
                )
            continue
        outstanding.append(
            {
                "authorization_id": str(value.get("authorization_id", path.stem)),
                "plan_id": str(value.get("plan_id", "")),
                "operation": str(value.get("operation", "")),
                "branch": str(value.get("branch", "")),
                "ref": str(value.get("ref", "")),
                "expires_at": str(value.get("expires_at", "")),
            }
        )
    return outstanding, unattested


def _lock_warnings(store: MetadataStore) -> list[dict[str, str]]:
    """The repository lock arbitrates every writer, so its shape is authority."""

    def _populated(directory: Path) -> bool:
        try:
            return next(directory.iterdir(), None) is not None
        except OSError:
            return False

    if not os.path.lexists(store.lock_file):
        if store.repo_file.exists() or any(
            directory.is_dir() and _populated(directory)
            for directory in (store.records_dir, store.transactions_dir)
        ):
            return [
                _warning(
                    "REPOSITORY_LOCK_MISSING",
                    "repository lock file is missing; mutations fail closed until a bootstrap "
                    "command recreates it",
                    path=str(store.lock_file),
                )
            ]
        return []
    try:
        lock_shape_valid = stat.S_ISREG(os.lstat(store.lock_file).st_mode)
    except OSError:
        return []  # Environment diagnosis owns inaccessible paths.
    if not lock_shape_valid:
        return [
            _warning(
                "REPOSITORY_LOCK_INVALID",
                "repository lock is not a regular file; controller authority requires review",
                path=str(store.lock_file),
            )
        ]
    return []


def _environment_first(error: ControllerError, diagnosis: dict[str, Any]) -> ControllerError:
    """Keep access failures ahead of lock authority until the environment is known good."""
    code = diagnosis["code"]
    if code != "HEALTHY" and error.code in {
        "REPOSITORY_LOCK_INVALID",
        "REPOSITORY_LOCK_ACCESS_UNKNOWN",
        "MISSING_REPOSITORY_LOCK",
        "FILESYSTEM_PERMISSION",
    }:
        facts = ", ".join(diagnosis["facts"]) or "access could not be confirmed"
        return ControllerError(code, f"repository environment diagnosis: {facts}")
    if error.code == "REPOSITORY_LOCK_ACCESS_UNKNOWN":
        return ControllerError("UNKNOWN", str(error))
    return error


def _environment_warnings(diagnostics: dict[str, Any]) -> list[dict[str, str]]:
    warnings = mount_warnings(diagnostics)
    code = diagnostics["environment"]["code"]
    if code != "HEALTHY":
        warnings = [row for row in warnings if row["code"] != "REPOSITORY_LOCK_SHAPE_INVALID"]
    if code == "UNKNOWN":
        warnings.append(
            _warning(
                "ENVIRONMENT_UNKNOWN",
                "repository mount or namespace access could not be confirmed",
                severity="degraded",
            )
        )
    elif code == "FILESYSTEM_PERMISSION":
        warnings.append(
            _warning(
                "FILESYSTEM_PERMISSION",
                "repository filesystem access is restricted",
                severity="blocking",
            )
        )
    return warnings


def _primary_diagnosis(
    environment: dict[str, Any], findings: list[dict[str, str]], *, failed: bool
) -> str:
    if environment["code"] != "HEALTHY":
        return str(environment["code"])
    codes = {row["code"] for row in findings}
    if "FILESYSTEM_PERMISSION" in codes:
        return "FILESYSTEM_PERMISSION"
    if "UNKNOWN" in codes:
        return "UNKNOWN"
    if codes & {
        "REPOSITORY_LOCK_INVALID",
        "REPOSITORY_LOCK_SHAPE_INVALID",
        "REPOSITORY_LOCK_MISSING",
        "MISSING_REPOSITORY_LOCK",
    }:
        return "CONTROLLER_LOCK_AUTHORITY"
    return "CONTROLLER_METADATA" if failed else "NONE"


def _observed_worktrees(
    repository: RepositoryInventory,
    policy: WorktreePolicy,
    snapshot: MetadataSnapshot,
    *,
    namespace_healthy: bool,
) -> list[dict[str, Any]]:
    worktrees = []
    for item in repository.worktrees:
        record = _record_for(item, snapshot)
        row = _worktree_payload(item, record, snapshot)
        row["lifecycle_observation"] = observe_worktree(
            item, record, snapshot, policy, namespace_healthy=namespace_healthy
        )
        worktrees.append(row)
    return worktrees


def _intent_for_allocation(snapshot: MetadataSnapshot, allocation_id: str | None) -> Any | None:
    if allocation_id is None:
        return None
    matches = [
        intent
        for intent in snapshot.integration_intents.values()
        if intent.allocation_id == allocation_id
    ]
    if len(matches) > 1:
        raise ControllerError("INVALID_METADATA", "multiple intents bind one allocation")
    return matches[0] if matches else None


def _integration_intent_order(intent: Any) -> tuple[int, int | str, str]:
    if intent.queue_sequence > 0:
        return (1, intent.queue_sequence, intent.intent_id)
    return (0, intent.enqueued_at, intent.intent_id)


def _release_intent_for_allocation(
    snapshot: MetadataSnapshot, allocation_id: str | None
) -> Any | None:
    if allocation_id is None:
        return None
    matches = sorted(
        (
            intent
            for intent in snapshot.release_intents.values()
            if intent.allocation_id == allocation_id
        ),
        key=lambda intent: (intent.updated_at, intent.intent_id),
        reverse=True,
    )
    active = [intent for intent in matches if intent.status in {"PENDING", "BLOCKED_RETRYABLE"}]
    if len(active) > 1:
        raise ControllerError("INVALID_METADATA", "multiple active release intents bind allocation")
    return active[0] if active else matches[0] if matches else None


def capacity_accounting(
    repository: RepositoryInventory,
    policy: WorktreePolicy,
    snapshot: MetadataSnapshot,
) -> dict[str, Any]:
    """Report inventory and declared ownership/occupancy without probing processes.

    Resource admission treats the legacy checkout/role limits as statistics. A
    writer lease is ownership evidence, including when its holder has died; only
    an existing runtime binding declares execution occupancy until formal clear.
    Neither count proves process liveness or business completion.
    """

    registered = [
        (item, record)
        for item in repository.worktrees
        if (record := _record_for(item, snapshot)) is not None
    ]
    managed = [(item, record) for item, record in registered if not item.canonical]
    ordinary = [(item, record) for item, record in managed if record.role in ORDINARY_ROLES]
    writers = [record for _item, record in ordinary if record.lifecycle != "RETAINED"]
    retained = [record for _item, record in ordinary if record.lifecycle == "RETAINED"]
    leased = set(snapshot.writer_leases)
    runtime_occupied = sum(
        any(binding.startswith("runtime:") for binding in record.external_bindings)
        for _item, record in registered
    )
    cleanup_pending = {
        intent.allocation_id
        for intent in snapshot.release_intents.values()
        if intent.status in {"PENDING", "BLOCKED_RETRYABLE"}
    }
    return {
        "physical_used": len(repository.worktrees),
        "physical_limit": policy.hard_limit,
        "normal_limit": policy.normal_limit,
        "ordinary_physical": len(ordinary),
        "ordinary_limit": policy.feature_audit_slots + policy.retained_slots,
        "writers_used": len(writers),
        "writer_limit": policy.feature_audit_slots,
        "retained_used": len(retained),
        "integration_used": sum(1 for _item, record in managed if record.role == "integration"),
        "integration_limit": policy.integration_candidate_limit,
        "emergency_used": sum(1 for _item, record in managed if record.role in EMERGENCY_ROLES),
        "emergency_limit": policy.emergency_slots,
        "recovery_used": sum(1 for _item, record in managed if record.role in RECOVERY_ROLES),
        "recovery_limit": policy.recovery_reserve,
        # Legacy allocation statistics do not grant or reject resource admission.
        "legacy_reserved": sum(1 for record in writers if record.allocation_id not in leased),
        "writer_leases_recorded": sum(record.allocation_id in leased for _item, record in registered),
        "runtime_occupied": runtime_occupied,
        "live_executions": runtime_occupied,
        "live_executions_semantics": "runtime-binding occupancy; process liveness is not observed",
        "runtime_bound": sum(1 for _item, record in managed if record.external_bindings),
        "cleanup_pending": sum(
            1 for _item, record in managed if record.allocation_id in cleanup_pending
        ),
    }


def governance_capacity(
    repository: RepositoryInventory,
    policy: WorktreePolicy,
    snapshot: MetadataSnapshot,
) -> dict[str, Any]:
    """Observe declared checkout limits without changing resource admission."""
    ordinary = [
        record
        for item in repository.worktrees
        if not item.canonical
        and (record := _record_for(item, snapshot)) is not None
        and record.role in ORDINARY_ROLES
    ]
    used = {
        "active": sum(record.lifecycle == "ACTIVE" for record in ordinary),
        "retained": sum(record.lifecycle == "RETAINED" for record in ordinary),
        "total": len(repository.worktrees),
    }
    limits = {
        "active_limit": policy.feature_audit_slots,
        "retained_limit": policy.retained_slots,
        "total_limit": policy.hard_limit,
    }
    return {
        "mode": "observe",
        "scope": "active/retained: feature and audit linked worktrees; total: all Git registered worktrees",
        "used": used,
        "limits": limits,
        "over_limit": {
            name: used[name] > limits[f"{name}_limit"] for name in used
        },
    }


def _repository_payload(
    repository: RepositoryInventory,
    policy: WorktreePolicy,
    snapshot: MetadataSnapshot,
) -> dict[str, Any]:
    count = len(repository.worktrees)
    retained = sum(
        1
        for item in repository.worktrees
        if (record := _record_for(item, snapshot)) is not None and record.lifecycle == "RETAINED"
    )
    accounting = capacity_accounting(repository, policy, snapshot)
    queued = sorted(
        (
            intent
            for intent in snapshot.integration_intents.values()
            if effective_intent_state(snapshot, intent) in {"QUEUED", "WAITING_SCOPE"}
        ),
        key=lambda value: integration_queue_order(snapshot, value),
    )
    return {
        "repository_id": snapshot.repository_id,
        "canonical_path": str(repository.canonical_path),
        "common_dir": str(repository.common_dir),
        "requested_path": str(repository.requested_path),
        "worktree_count": count,
        "governance_capacity": governance_capacity(repository, policy, snapshot),
        "admission_strategy": "resource-budget-v1" if policy.resource_admission else "legacy-slots",
        "checkout_limits_enforced": not policy.resource_admission,
        "normal_limit": policy.normal_limit,
        "hard_limit": policy.hard_limit,
        "normal_slots_remaining": max(policy.normal_limit - (count - retained), 0),
        "hard_slots_remaining": max(policy.hard_limit - count, 0),
        **accounting,
        # Retained work is bounded by the ordinary pool it shares, so the remaining
        # room is the pool's, not a separate quota's.
        "retained_limit": accounting["ordinary_limit"],
        "retained_slots_remaining": max(
            accounting["ordinary_limit"] - accounting["ordinary_physical"], 0
        ),
        "ordinary_slots_remaining": max(
            accounting["ordinary_limit"] - accounting["ordinary_physical"], 0
        ),
        "writer_slots_remaining": max(accounting["writer_limit"] - accounting["writers_used"], 0),
        "integration_candidate_limit": policy.integration_candidate_limit,
        "integration_prepare_limit": policy.integration_prepare_limit,
        "main_publish_limit": policy.main_publish_limit,
        "recovery_reserve": policy.recovery_reserve,
        "feature_or_audit_limit": policy.feature_audit_slots,
        "emergency_limit": policy.emergency_slots,
        "integration_queue_depth": len(queued),
        "oldest_integration_enqueued_at": queued[0].enqueued_at if queued else None,
        "publication_remote": policy.publication_remote,
        "publication_branch": policy.publication_branch,
        "automatic_feature_audit_release": policy.automatic_feature_audit_release,
        "automatic_integration_continuation": policy.automatic_integration_continuation,
        "preserved_auto_release": policy.preserved_auto_release,
        "release_intent_ttl_seconds": policy.release_intent_ttl_seconds,
        "active_release_intent_count": sum(
            intent.status in {"PENDING", "BLOCKED_RETRYABLE"}
            for intent in snapshot.release_intents.values()
        ),
        "publication_lease": (
            {
                "lease_id": snapshot.publication_lease.lease_id,
                "repository_id": snapshot.publication_lease.repository_id,
                "allocation_id": snapshot.publication_lease.allocation_id,
                "owner": snapshot.publication_lease.holder,
                "holder": snapshot.publication_lease.holder,
                "run_id": snapshot.publication_lease.run_id,
                "remote": snapshot.publication_lease.remote,
                "branch": snapshot.publication_lease.branch,
                "target_ref": snapshot.publication_lease.target_ref,
                "candidate_head": snapshot.publication_lease.candidate_oid,
                "candidate_oid": snapshot.publication_lease.candidate_oid,
                "remote_head": snapshot.publication_lease.expected_main_oid,
                "expected_main_oid": snapshot.publication_lease.expected_main_oid,
                "finalizer_record_id": snapshot.publication_lease.validation_evidence_id,
                "validation_evidence_id": snapshot.publication_lease.validation_evidence_id,
                "acquired_at": snapshot.publication_lease.acquired_at,
                "expires_at": snapshot.publication_lease.expires_at,
            }
            if snapshot.publication_lease
            else None
        ),
        "policy_path": str(policy.path),
        "policy_present": policy.present,
        "metadata_path": str(MetadataStore(repository.common_dir).root),
    }


def _worktree_payload(
    item: WorktreeGitState,
    record: MetadataRecord | None,
    snapshot: MetadataSnapshot | None = None,
) -> dict[str, Any]:
    from .owner_actions import owner_action

    action = None
    if record is not None and not item.clean and not item.canonical:
        action = owner_action(str(item.path), {
            "path": str(item.path), "head": item.head, "branch": item.branch,
            **{key: getattr(record, key) for key in
               ("allocation_id", "task_key", "authority_key", "owner", "record_version")},
        }, reason="DIRTY_WORKTREE", required_actions=(
            "review_task_files", "finalize_or_checkpoint", "preserve_unknown_files", "park_or_handoff"))
    intent = (
        _intent_for_allocation(snapshot, record.allocation_id)
        if snapshot is not None and record is not None
        else None
    )
    release_intent = (
        _release_intent_for_allocation(snapshot, record.allocation_id)
        if snapshot is not None and record is not None
        else None
    )
    return {
        "kind": "canonical" if item.canonical else "secondary",
        "canonical": item.canonical,
        "path": str(item.path),
        "branch": item.branch,
        "detached": item.detached,
        "head": item.head,
        "clean": item.clean,
        "dirty": not item.clean,
        "changes": {
            "staged": item.staged,
            "modified": item.modified,
            "untracked": item.untracked,
        },
        "locked": item.locked,
        "lock_reason": item.lock_reason,
        "prunable": item.prunable,
        "prunable_reason": item.prunable_reason,
        "metadata_missing": record is None,
        "allocation_id": record.allocation_id if record else None,
        "task_key": record.task_key if record else None,
        "authority_key": record.authority_key if record else None,
        "module": record.module if record else None,
        "role": record.role if record else None,
        "owner": record.owner if record else None,
        "lifecycle": record.lifecycle if record else None,
        "record_version": record.record_version if record else None,
        "owner_action_request": action,
        "wait_checkpoint": (
            record.history[-1].get("checkpoint")
            if record and record.lifecycle == "RETAINED" and record.history else None
        ),
        "created_at": record.created_at if record else None,
        "last_reviewed_at": record.last_reviewed_at if record else None,
        "handoff_commit": record.handoff_commit if record else None,
        "finalizer_record_id": record.finalizer_record_id if record else None,
        "base_commit": record.base_commit if record else None,
        "task_commit": record.task_commit if record else None,
        "finalizer_commit": record.finalizer_commit if record else None,
        "scope_paths": list(record.scope_paths) if record else [],
        "source_allocations": list(record.source_allocations) if record else [],
        "integration_intent_id": intent.intent_id if intent else None,
        "integration_intent_state": (
            effective_intent_state(snapshot, intent)
            if intent is not None and snapshot is not None
            else None
        ),
        "integration_intent_raw_state": intent.state if intent else None,
        "source_handoff_oids": list(intent.source_handoffs) if intent else [],
        "candidate_revision": intent.revision if intent else None,
        "candidate_oid": intent.candidate_oid if intent else None,
        "validation_evidence_id": intent.validation_evidence_id if intent else None,
        "validated_oid": intent.validated_oid if intent else None,
        "quarantine_expected_commit": (record.quarantine_expected_commit if record else None),
        "quarantine_observed_commit": (record.quarantine_observed_commit if record else None),
        "recovery_allocation_id": record.recovery_allocation_id if record else None,
        "recovery_ref": record.recovery_ref if record else None,
        "disposable_resources": (
            [dict(resource) for resource in record.disposable_resources] if record else []
        ),
        "external_bindings": list(record.external_bindings) if record else [],
        "release_intent_id": release_intent.intent_id if release_intent else None,
        "release_intent_status": release_intent.status if release_intent else None,
        "release_intent_blocker": release_intent.blocker if release_intent else None,
        "release_intent_expires_at": release_intent.expires_at if release_intent else None,
        "release_eligible": False,
        "release_note": "not inferred from cleanliness; release guards are required",
    }


def _metadata_warnings(
    repository: RepositoryInventory,
    policy: WorktreePolicy,
    snapshot: MetadataSnapshot,
) -> list[dict[str, str]]:
    warnings = [dict(item) for item in snapshot.warnings]
    live_keys = {item.key for item in repository.worktrees}
    bound_allocations: defaultdict[str, list[str]] = defaultdict(list)
    for key, allocation_id in snapshot.bindings.items():
        bound_allocations[allocation_id].append(key)
        if key not in live_keys:
            warnings.append(
                _warning(
                    "ORPHAN_BINDING",
                    "metadata binding has no registered worktree",
                    worktree_key=key,
                    allocation_id=allocation_id,
                )
            )
        if allocation_id not in snapshot.records:
            warnings.append(
                _warning(
                    "MISSING_RECORD",
                    "binding references a missing metadata record",
                    worktree_key=key,
                    allocation_id=allocation_id,
                )
            )
    for allocation_id in snapshot.records:
        keys = bound_allocations.get(allocation_id, [])
        if not keys and snapshot.records[allocation_id].lifecycle != "RELEASED":
            warnings.append(
                _warning(
                    "ORPHAN_RECORD",
                    "metadata record is not bound to a registered worktree",
                    allocation_id=allocation_id,
                )
            )
        elif len(keys) > 1:
            warnings.append(
                _warning(
                    "DUPLICATE_BINDING",
                    "one allocation is bound to multiple worktrees",
                    allocation_id=allocation_id,
                )
            )

    active_by_authority: defaultdict[str, list[MetadataRecord]] = defaultdict(list)
    active_by_task: defaultdict[str, list[MetadataRecord]] = defaultdict(list)
    for item in repository.worktrees:
        record = _record_for(item, snapshot)
        if record is None:
            warnings.append(
                _warning(
                    "UNMANAGED_WORKTREE",
                    "registered worktree has no controller metadata",
                    path=str(item.path),
                )
            )
            continue
        if record.owner == "unassigned":
            warnings.append(
                _warning(
                    "UNOWNED_WORKTREE",
                    "worktree owner is explicitly unassigned",
                    path=str(item.path),
                    allocation_id=record.allocation_id,
                )
            )
        if record.lifecycle in ACTIVE_AUTHORITY_STATES:
            active_by_task[record.task_key].append(record)
            if record.role in FEATURE_AUTHORITY_ROLES:
                active_by_authority[record.authority_key].append(record)
        try:
            reviewed = datetime.fromisoformat(record.last_reviewed_at)
            age = datetime.now(TIMEZONE) - reviewed.astimezone(TIMEZONE)
            if age.days >= policy.stale_warning_days:
                warnings.append(
                    _warning(
                        "STALE_REVIEW",
                        "worktree metadata review is stale; lifecycle is not auto-expired",
                        path=str(item.path),
                        allocation_id=record.allocation_id,
                    )
                )
        except ValueError:
            warnings.append(
                _warning(
                    "INVALID_REVIEW_TIME",
                    "last_reviewed_at cannot be interpreted",
                    allocation_id=record.allocation_id,
                )
            )
    for task_key, records in active_by_task.items():
        if len(records) > 1:
            warnings.append(
                _warning(
                    "DUPLICATE_TASK",
                    "multiple active allocations use the same task key",
                    task_key=task_key,
                )
            )
    for authority_key, records in active_by_authority.items():
        if len(records) > 1:
            warnings.append(
                _warning(
                    "DUPLICATE_AUTHORITY",
                    "multiple active feature allocations use the same authority",
                    authority_key=authority_key,
                )
            )
    if policy.policy_schema_version >= 2:
        managed = [
            record
            for item in repository.worktrees
            if not item.canonical and (record := _record_for(item, snapshot)) is not None
        ]
        capacity_checks = (
            (
                "INTEGRATION_CAPACITY_VIOLATION",
                sum(record.role == "integration" and record.lifecycle not in {"PUBLISHED", "RELEASED"}
                    for record in managed),
                policy.integration_candidate_limit,
            ),
            (
                "FEATURE_CAPACITY_VIOLATION",
                sum(
                    record.role in {"feature", "audit"} and record.lifecycle != "RETAINED"
                    for record in managed
                ),
                policy.feature_audit_slots,
            ),
            (
                # Retained work is bounded by the ordinary pool it shares with writers,
                # not by a separate physical sub-quota.
                "ORDINARY_POOL_VIOLATION",
                sum(record.role in ORDINARY_ROLES for record in managed),
                policy.feature_audit_slots + policy.retained_slots,
            ),
            (
                "EMERGENCY_CAPACITY_VIOLATION",
                sum(record.role in EMERGENCY_ROLES for record in managed),
                policy.emergency_slots,
            ),
            (
                "RECOVERY_CAPACITY_VIOLATION",
                sum(record.role in RECOVERY_ROLES for record in managed),
                policy.recovery_reserve,
            ),
        )
        for code, observed, limit in capacity_checks:
            if policy.resource_admission and code != "INTEGRATION_CAPACITY_VIOLATION":
                continue
            if observed > limit:
                warnings.append(
                    _warning(
                        code,
                        "role-specific worktree capacity is exceeded",
                        observed=str(observed),
                        limit=str(limit),
                    )
                )
    if not policy.present:
        warnings.append(_warning("MISSING_POLICY", "repository worktree policy is missing"))
    return warnings


def status(repository_path: Path, *, namespace_pid: int | None = None) -> dict[str, Any]:
    return status_observation(repository_path, namespace_pid=namespace_pid)[0]


def status_observation(
    repository_path: Path, *, namespace_pid: int | None = None
) -> tuple[dict[str, Any], MetadataSnapshot]:
    """Return status and its validated metadata for one query.

    Callers must not retain this observation across requests or use it to
    authorize a mutation, which must recheck under its own lock.
    """
    result = envelope("status")
    repository = inventory(repository_path)
    diagnostics = diagnose_mounts(repository, namespace_pid=namespace_pid)
    diagnosis = diagnostics["environment"]
    result["mount_diagnostics"] = diagnostics
    result["environment_diagnosis"] = diagnosis
    policy = load_policy(repository.canonical_path)
    store = MetadataStore(repository.common_dir)
    try:
        snapshot = store.load()
    except ControllerError as exc:
        raise _environment_first(exc, diagnosis) from exc
    result["repository"] = _repository_payload(repository, policy, snapshot)
    result["worktrees"] = _observed_worktrees(
        repository, policy, snapshot, namespace_healthy=diagnosis["code"] == "HEALTHY"
    )
    result["orphan_observations"] = observe_repository_orphans(repository, snapshot)
    result["warnings"] = (
        _environment_warnings(diagnostics)
        + _metadata_warnings(repository, policy, snapshot)
        + (_lock_warnings(store) if diagnosis["code"] == "HEALTHY" else [])
    )
    return result, snapshot


def _resolve_target(
    repository: RepositoryInventory, target: Path | None
) -> WorktreeGitState | None:
    if target is None:
        return None
    try:
        resolved = target.expanduser().resolve(strict=True)
    except OSError as exc:
        raise ControllerError("INVALID_TARGET", f"target path is unavailable: {target}") from exc
    for item in repository.worktrees:
        if item.path.resolve(strict=True) == resolved:
            return item
    raise ControllerError("UNREGISTERED_WORKTREE", f"target is not registered: {resolved}")


def plan(
    repository_path: Path,
    *,
    task_key: str,
    authority_key: str | None,
    module: str,
    role: str,
    target: Path | None = None,
) -> dict[str, Any]:
    repository = inventory(repository_path)
    policy = load_policy(repository.canonical_path)
    snapshot = MetadataStore(repository.common_dir).load()
    return _plan_from_snapshot(repository, policy, snapshot, task_key=task_key,
        authority_key=authority_key, module=module, role=role, target=target)


def _plan_from_snapshot(
    repository: RepositoryInventory,
    policy: WorktreePolicy,
    snapshot: MetadataSnapshot,
    *,
    task_key: str,
    authority_key: str | None,
    module: str,
    role: str,
    target: Path | None = None,
) -> dict[str, Any]:
    """Evaluate read-only planning; acquire still rechecks under its own lock."""
    result = envelope("plan")
    warnings = _metadata_warnings(repository, policy, snapshot)
    result["repository"] = _repository_payload(repository, policy, snapshot)
    result["worktrees"] = [
        _worktree_payload(item, _record_for(item, snapshot), snapshot)
        for item in repository.worktrees
    ]
    result["warnings"] = warnings
    requested = {
        "task_key": task_key,
        "authority_key": authority_key,
        "module": module,
        "role": role,
        "target": str(target.expanduser()) if target else None,
    }

    def decide(code: str, reason: str, **extra: Any) -> dict[str, Any]:
        result["decision"] = {
            "code": code,
            "reason": reason,
            "request": requested,
            "capacity": accounting,
            **extra,
        }
        return result

    accounting = capacity_accounting(repository, policy, snapshot)
    if not task_key.strip() or not module.strip() or role not in WORKTREE_ROLES:
        return decide("UNKNOWN", "task_key, module, and a supported role are required")
    if policy.require_authority_key and (authority_key is None or not authority_key.strip()):
        return decide("UNKNOWN", "authority_key is required by repository policy")
    if not policy.present:
        return decide("BLOCK_DRIFT", "repository policy is missing")
    capacity_drift = {
        "INTEGRATION_CAPACITY_VIOLATION",
        "FEATURE_CAPACITY_VIOLATION",
        "EMERGENCY_CAPACITY_VIOLATION",
        "RECOVERY_CAPACITY_VIOLATION",
        "ORDINARY_POOL_VIOLATION",
    }
    if any(warning["code"] in capacity_drift for warning in warnings):
        return decide("BLOCK_DRIFT", "role-specific capacity already exceeds policy")
    target_item = _resolve_target(repository, target)
    if target_item and target_item.canonical and role not in CANONICAL_ROLES:
        return decide("BLOCK_CANONICAL", "canonical is protected from ordinary feature work")

    active: list[tuple[WorktreeGitState, MetadataRecord]] = []
    unmanaged: list[WorktreeGitState] = []
    for item in repository.worktrees:
        record = _record_for(item, snapshot)
        if record is None:
            unmanaged.append(item)
        elif record.lifecycle in ACTIVE_AUTHORITY_STATES:
            active.append((item, record))

    task_matches = [(item, record) for item, record in active if record.task_key == task_key]
    if task_matches:
        item, record = task_matches[0]
        return decide(
            "REUSE",
            "an active allocation already owns this task",
            reuse={"path": str(item.path), "allocation_id": record.allocation_id},
        )
    if unmanaged and policy.require_adoption:
        return decide(
            "NEEDS_ADOPTION",
            "all registered worktrees must be adopted before allocating another authority",
            unmanaged_paths=[str(item.path) for item in unmanaged],
        )
    if authority_key and policy.unique_active_feature_authority:
        authority_matches = [
            (item, record)
            for item, record in active
            if record.authority_key == authority_key and record.role in FEATURE_AUTHORITY_ROLES
        ]
        if authority_matches:
            item, record = authority_matches[0]
            return decide(
                "BLOCK_DUPLICATE_AUTHORITY",
                "an active feature allocation already owns this authority",
                conflict={
                    "path": str(item.path),
                    "allocation_id": record.allocation_id,
                    "task_key": record.task_key,
                },
            )
    preparing = sum(
        record.role == "integration" and record.lifecycle not in {"PUBLISHED", "RELEASED"}
        for item in repository.worktrees
        if not item.canonical and (record := _record_for(item, snapshot)) is not None
    )
    if role == "integration" and preparing >= policy.integration_candidate_limit:
        return decide("BLOCK_INTEGRATION_SLOT", "integration preparation concurrency is occupied")
    from .resource_admission import AdmissionLedger, allocation_request

    canonical = next(item for item in repository.worktrees if item.canonical)
    request = allocation_request(repository, snapshot.repository_id, policy,
        target=target.expanduser().absolute() if target is not None else Path.home() / "worktrees" / repository.canonical_path.name / task_key,
        base_oid=canonical.head, arguments={"task_key": task_key, "authority_key": authority_key})
    budget = AdmissionLedger().plan([request])
    available = "CREATE_EMERGENCY_ALLOWED" if role in EMERGENCY_ROLES else "CREATE_RECOVERY_ALLOWED" if role in RECOVERY_ROLES else "CREATE_ALLOWED"
    return decide("RESOURCE_WAIT" if budget["blockers"] else available,
                  "target filesystem resource budget evaluated; checkout counts are statistical",
                  resource_admission=budget)


def guard_write(
    repository_path: Path,
    *,
    target: Path,
    task_key: str,
    authority_key: str,
    owner: str,
    operation: str,
    holder: WriterHolder | None = None,
    observe: bool = True,
    state: RepositoryInventory | None = None,
    snapshot: MetadataSnapshot | None = None,
) -> dict[str, Any]:
    """Authorize one declared write class against the selected allocation.

    This is a trusted-agent workflow gate, not same-UID process isolation.  It is intentionally
    read-only: lifecycle mutation remains owned by the dedicated Controller commands.
    A caller already holding one repository inventory and metadata snapshot (under
    its repository lock) passes them, so the decision uses that same snapshot.
    """

    result = status(repository_path) if observe else envelope("write-guard")
    result["command"] = "write-guard"
    repository = state if state is not None else inventory(repository_path)
    store = MetadataStore(repository.common_dir)
    if snapshot is None:
        snapshot = store.load()
    item = _resolve_target(repository, target)
    requested = {
        "path": str(target.expanduser()),
        "task_key": task_key,
        "authority_key": authority_key,
        "owner": owner,
        "operation": operation,
    }

    def decide(code: str, reason: str, **extra: Any) -> dict[str, Any]:
        result["decision"] = {"code": code, "reason": reason, "request": requested, **extra}
        return result

    if item is None:
        return decide("BLOCK_UNREGISTERED_WORKTREE", "write target is not a registered worktree")
    record = _record_for(item, snapshot)
    if record is None:
        return decide("BLOCK_UNMANAGED_WORKTREE", "write target has no Controller allocation")
    if not store.lock_file.is_file() or store.lock_file.is_symlink():
        return decide("BLOCK_INVALID_METADATA", "governed write requires its repository lock")
    if operation not in WRITE_OPERATIONS:
        return decide("BLOCK_UNKNOWN_WRITE_OPERATION", "write operation is not supported")
    if record.owner != owner:
        return decide(
            "BLOCK_OWNER_MISMATCH",
            "declared owner does not own the selected allocation",
            allocation_id=record.allocation_id,
        )

    # Recovery must remain available after task/head drift; its lifecycle commands perform the
    # stronger CAS, cleanliness, lineage, and scope checks before any metadata mutation.
    if operation == "recovery":
        if record.role not in {"integration", "recovery", "preservation"}:
            return decide(
                "BLOCK_RECOVERY_ROLE",
                "recovery entry is limited to integration and recovery allocations",
                allocation_id=record.allocation_id,
            )
        if record.lifecycle in {"PUBLISHED", "RELEASED"}:
            return decide(
                "BLOCK_RECOVERY_LIFECYCLE",
                f"lifecycle {record.lifecycle} cannot enter recovery",
                allocation_id=record.allocation_id,
            )
        return decide(
            "WRITE_ALLOWED",
            "explicit recovery entry is allowed; lifecycle commands remain authoritative",
            allocation_id=record.allocation_id,
            allocation_task_key=record.task_key,
            lifecycle=record.lifecycle,
        )

    if record.task_key != task_key:
        return decide(
            "BLOCK_TASK_IDENTITY_MISMATCH",
            "declared task does not match the selected allocation",
            allocation_id=record.allocation_id,
            allocation_task_key=record.task_key,
        )
    if record.authority_key != authority_key:
        return decide(
            "BLOCK_AUTHORITY_IDENTITY_MISMATCH",
            "declared authority does not match the selected allocation",
            allocation_id=record.allocation_id,
            allocation_authority_key=record.authority_key,
        )

    if operation == "feature-write":
        if record.role == "integration":
            return decide(
                "BLOCK_INTEGRATION_FEATURE_WRITE",
                "integration allocations cannot host feature implementation",
                allocation_id=record.allocation_id,
                lifecycle=record.lifecycle,
                next_action="allocate or reuse a feature worktree; use recovery if drift already exists",
            )
        if record.role not in FEATURE_AUTHORITY_ROLES:
            return decide(
                "BLOCK_FEATURE_WRITE_ROLE",
                f"role {record.role} cannot host ordinary feature implementation",
                allocation_id=record.allocation_id,
            )
        if record.lifecycle != "ACTIVE":
            return decide(
                "BLOCK_FEATURE_WRITE_LIFECYCLE",
                f"lifecycle {record.lifecycle} is frozen for feature writes",
                allocation_id=record.allocation_id,
            )
        try:
            check_writer_entitlement(
                snapshot.repository_id,
                record,
                item.path,
                snapshot.writer_leases.get(record.allocation_id),
                holder,
            )
        except ControllerError as exc:
            return decide(f"BLOCK_{exc.code}", str(exc), allocation_id=record.allocation_id)
        return decide(
            "WRITE_ALLOWED",
            "active feature allocation matches the declared task authority",
            allocation_id=record.allocation_id,
            lifecycle=record.lifecycle,
        )

    if operation == "integration-write":
        if record.role != "integration":
            return decide(
                "BLOCK_INTEGRATION_WRITE_ROLE",
                "integration-only writes require an integration allocation",
                allocation_id=record.allocation_id,
            )
        if record.lifecycle == "HANDOFF_READY":
            drift = evidence_head_issues(item, record)
            if drift:
                return decide(
                    "BLOCK_RECOVERY_REQUIRED",
                    "HANDOFF_READY integration HEAD drift requires quarantine/recovery",
                    allocation_id=record.allocation_id,
                    finding=drift[0],
                    next_action="quarantine and externalize the drift before further writes",
                )
        intent = _intent_for_allocation(snapshot, record.allocation_id)
        if intent is not None and intent.state == "READY_TO_PUBLISH":
            return decide(
                "BLOCK_VALIDATED_CANDIDATE_FROZEN",
                "validated integration candidates can only refresh, publish, or enter recovery",
                allocation_id=record.allocation_id,
                lifecycle=record.lifecycle,
                validation_evidence_id=intent.validation_evidence_id,
            )
        if intent is not None and intent.state == "NEEDS_REPREPARE":
            return decide(
                "BLOCK_REPREPARE_REQUIRED",
                "stale candidates must be rebuilt from frozen sources before further writes",
                allocation_id=record.allocation_id,
                lifecycle=record.lifecycle,
                next_action="run integration-refresh; old prepared evidence is no longer eligible",
            )
        if record.lifecycle != "ACTIVE":
            return decide(
                "BLOCK_INTEGRATION_WRITE_LIFECYCLE",
                f"lifecycle {record.lifecycle} is frozen for integration writes",
                allocation_id=record.allocation_id,
                next_action="use integration-refresh/recovery; ordinary writes remain fenced",
            )
        return decide(
            "WRITE_ALLOWED",
            "declared integration task matches the integration allocation",
            allocation_id=record.allocation_id,
            lifecycle=record.lifecycle,
        )

    if record.lifecycle in {"QUARANTINED", "RECOVERY_REQUIRED", "RELEASED"}:
        return decide(
            "BLOCK_FINALIZATION_LIFECYCLE",
            f"lifecycle {record.lifecycle} requires its dedicated recovery/release command",
            allocation_id=record.allocation_id,
        )
    if record.lifecycle in {"HANDOFF_READY", "PRESERVED"}:
        return decide(
            "BLOCK_HANDOFF_WRITE_FENCE",
            f"lifecycle {record.lifecycle} is frozen after handoff",
            allocation_id=record.allocation_id,
            next_action="explicitly reopen the allocation before any new controlled write",
        )
    if record.role in FEATURE_AUTHORITY_ROLES:
        if record.lifecycle != "ACTIVE":
            return decide("BLOCK_FINALIZATION_LIFECYCLE", "writer allocation is not active")
        try:
            check_writer_entitlement(
                snapshot.repository_id,
                record,
                item.path,
                snapshot.writer_leases.get(record.allocation_id),
                holder,
            )
        except ControllerError as exc:
            return decide(f"BLOCK_{exc.code}", str(exc), allocation_id=record.allocation_id)
    return decide(
        "WRITE_ALLOWED",
        "declared finalization task matches the allocation",
        allocation_id=record.allocation_id,
        lifecycle=record.lifecycle,
    )


def adopt(
    repository_path: Path,
    *,
    target: Path,
    task_key: str,
    authority_key: str,
    module: str,
    role: str,
    owner: str,
    lifecycle: str,
    base_commit: str | None = None,
    task_commit: str | None = None,
    scope_paths: tuple[str, ...] = (),
    source_allocations: tuple[str, ...] = (),
    external_bindings: tuple[str, ...] = (),
) -> dict[str, Any]:
    result = envelope("adopt")
    if role not in WORKTREE_ROLES:
        raise ControllerError("INVALID_ROLE", f"unsupported role: {role}")
    if lifecycle not in LIFECYCLE_STATES:
        raise ControllerError("INVALID_LIFECYCLE", f"unsupported lifecycle: {lifecycle}")
    repository_before = inventory(repository_path)
    target_item = _resolve_target(repository_before, target)
    if target_item is None:
        raise ControllerError("INVALID_TARGET", "adopt target is required")
    if target_item.canonical and role not in CANONICAL_ROLES:
        raise ControllerError("BLOCK_CANONICAL", "canonical requires a control-plane role")
    normalized_scopes = normalize_scope_paths(scope_paths)
    base_oid = resolve_commit(target_item.path, base_commit) if base_commit else None
    task_oid = resolve_commit(target_item.path, task_commit) if task_commit else None
    before = git_state_fingerprint(repository_before)
    store = MetadataStore(repository_before.common_dir)
    created = False
    with store.lock(exclusive=True, create=True):
        repository_locked = inventory(repository_path)
        locked_item = _resolve_target(repository_locked, target)
        if locked_item is None or locked_item.key != target_item.key:
            raise ControllerError("BLOCK_DRIFT", "worktree identity changed during adoption")
        snapshot = store._load_unlocked()
        allocation_id = snapshot.bindings.get(locked_item.key)
        if allocation_id is not None:
            record = snapshot.records.get(allocation_id)
            if record is None:
                raise ControllerError("BLOCK_DRIFT", "binding references a missing record")
            expected = (
                task_key.strip(),
                authority_key.strip(),
                module.strip(),
                role,
                owner.strip(),
                lifecycle,
                base_oid,
                task_oid,
                normalized_scopes,
                tuple(source_allocations),
            )
            actual = (
                record.task_key,
                record.authority_key,
                record.module,
                record.role,
                record.owner,
                record.lifecycle,
                record.base_commit,
                record.task_commit,
                record.scope_paths,
                record.source_allocations,
            )
            if actual != expected:
                raise ControllerError(
                    "ADOPTION_CONFLICT",
                    "worktree is already adopted with different metadata; use lifecycle commands",
                )
        else:
            store.initialize_repository()
            record = new_record(
                task_key=task_key,
                authority_key=authority_key,
                module=module,
                role=role,
                owner=owner,
                lifecycle=lifecycle,
                base_commit=base_oid,
                task_commit=task_oid,
                scope_paths=normalized_scopes,
                source_allocations=tuple(source_allocations),
                external_bindings=external_bindings,
            )
            store.write_record(
                locked_item.key,
                record,
                registration=None if locked_item.canonical else locked_item.git_dir,
            )
            created = True
    repository_after = inventory(repository_path)
    after = git_state_fingerprint(repository_after)
    if before != after:
        raise ControllerError(
            "GIT_STATE_CHANGED", "Git refs, index, worktrees, or status changed during adoption"
        )
    policy = load_policy(repository_after.canonical_path)
    snapshot_after = store.load()
    result["repository"] = _repository_payload(repository_after, policy, snapshot_after)
    result["worktrees"] = [
        _worktree_payload(item, _record_for(item, snapshot_after), snapshot_after)
        for item in repository_after.worktrees
    ]
    result["warnings"] = _metadata_warnings(repository_after, policy, snapshot_after)
    result["decision"] = {
        "code": "ADOPTED" if created else "ALREADY_ADOPTED",
        "reason": "controller metadata created" if created else "metadata already matched",
        "allocation_id": record.allocation_id,
        "path": str(target_item.path),
    }
    return result


def _semantic_warnings(
    repository: RepositoryInventory, snapshot: MetadataSnapshot, policy: WorktreePolicy
) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    items_by_allocation: dict[str, tuple[WorktreeGitState, MetadataRecord]] = {}
    for item in repository.worktrees:
        record = _record_for(item, snapshot)
        if record is None:
            continue
        items_by_allocation[record.allocation_id] = (item, record)
        if record.role == "integration":
            issues = list(integration_candidate_issues(item, record, snapshot))
        else:
            issues = list(evidence_head_issues(item, record))
            if record.lifecycle in EVIDENCE_LIFECYCLES:
                issues.extend(
                    path_scope_issues(
                        item.path,
                        record,
                        item.head,
                        publication_ref=publication_ref(item.path, policy),
                    )
                )
        warnings.extend(issues)
        if record.lifecycle == "RELEASE_READY":
            from .delivered_closeout import verify_receipt

            try:
                verify_receipt(MetadataStore(repository.common_dir), item, record, snapshot.repository_id)
            except ControllerError as exc:
                warnings.append(_warning(exc.code, str(exc), allocation_id=record.allocation_id))
        if record.lifecycle == "ARCHIVED":
            from .delivered_closeout import verify_archive_receipt

            try:
                verify_archive_receipt(
                    MetadataStore(repository.common_dir), item, record, snapshot.repository_id
                )
            except ControllerError as exc:
                warnings.append(_warning(exc.code, str(exc), allocation_id=record.allocation_id))
        if (
            issues
            and record.role == "integration"
            and record.lifecycle not in {"QUARANTINED", "RECOVERY_REQUIRED"}
        ):
            warnings.append(
                _warning(
                    "RECOVERY_REQUIRED",
                    "integration semantic drift requires explicit quarantine and recovery",
                    allocation_id=record.allocation_id,
                    path=str(item.path),
                )
            )
        if record.lifecycle in {"QUARANTINED", "RECOVERY_REQUIRED"}:
            warnings.append(
                _warning(
                    record.lifecycle,
                    "allocation is held for explicit governance recovery",
                    allocation_id=record.allocation_id,
                    path=str(item.path),
                )
            )

    lifecycle_for_intent = {
        "PREPARING_INTEGRATION": {"ACTIVE"},
        "READY_TO_PUBLISH": {"HANDOFF_READY"},
        "NEEDS_REPREPARE": {"ACTIVE"},
        "PUBLISHING": {"INTEGRATING"},
        "RECOVERY_REQUIRED": {"QUARANTINED", "RECOVERY_REQUIRED", "RELEASED"},
        "ATTENTION_REQUIRED": {"BLOCKED"},
        "REMOTE_VERIFIED": {"PUBLISHED", "RELEASED"},
        "RELEASED": {"RELEASED"},
    }
    for intent in snapshot.integration_intents.values():
        effective_state = effective_intent_state(snapshot, intent)
        if effective_state in {"QUEUED", "WAITING_SCOPE", "ABANDONED"}:
            continue
        if unallocated_integration_conflict(intent):
            warnings.append(_warning(
                "INTEGRATION_CONFLICT_PENDING",
                "pre-allocation conflict needs owner retry and explicit resolution",
                severity="informational", intent_id=intent.intent_id,
            ))
            continue
        record = (
            snapshot.records.get(intent.allocation_id) if intent.allocation_id is not None else None
        )
        if record is None:
            warnings.append(
                _warning(
                    "INTEGRATION_INTENT_DRIFT",
                    "claimed integration intent has no allocation record",
                    intent_id=intent.intent_id,
                )
            )
            continue
        if effective_state == "RELEASED" and intent.state != "RELEASED":
            warnings.append(
                _warning(
                    "INTEGRATION_INTENT_RECONCILED",
                    "immutable reconciliation makes historical intent drift non-blocking",
                    severity="informational",
                    intent_id=intent.intent_id,
                    allocation_id=record.allocation_id,
                )
            )
            continue
        expected_lifecycles = lifecycle_for_intent.get(effective_state, set())
        if (
            record.role != "integration"
            or record.task_key != intent.task_key
            or record.authority_key != intent.authority_key
            or record.source_allocations != intent.source_allocations
            or record.scope_paths != intent.scope_paths
            or record.lifecycle not in expected_lifecycles
        ):
            warnings.append(
                _warning(
                    "INTEGRATION_INTENT_DRIFT",
                    "integration intent and candidate allocation identity differ",
                    intent_id=intent.intent_id,
                    allocation_id=record.allocation_id,
                )
            )
        if intent.candidate_oid != record.task_commit:
            warnings.append(
                _warning(
                    "INTEGRATION_INTENT_DRIFT",
                    "integration intent candidate OID differs from allocation evidence",
                    intent_id=intent.intent_id,
                    allocation_id=record.allocation_id,
                )
            )
        if intent.schema_version >= 3 and effective_state in {
            "READY_TO_PUBLISH",
            "PUBLISHING",
            "REMOTE_VERIFIED",
            "RELEASED",
        }:
            prepared = (
                snapshot.prepared_candidate_receipts.get(intent.prepared_receipt_id)
                if intent.prepared_receipt_id is not None
                else None
            )
            matched = items_by_allocation.get(record.allocation_id)
            live_tree_drift = (
                matched is not None
                and matched[0].head == intent.prepared_commit_oid
                and commit_tree_oid(matched[0].path, matched[0].head) != intent.prepared_tree_oid
            )
            if (
                prepared is None
                or prepared.repository_id != snapshot.repository_id
                or prepared.intent_id != intent.intent_id
                or prepared.allocation_id != record.allocation_id
                or prepared.attempt != intent.attempt
                or prepared.base_origin_main_oid != intent.prepared_base_origin_main_oid
                or prepared.candidate_oid != intent.prepared_commit_oid
                or prepared.tree_oid != intent.prepared_tree_oid
                or prepared.scope_digest != intent.prepared_scope_digest
                or prepared.prepare_identity != intent.prepare_identity
                or prepared.validation_evidence_id != intent.validation_evidence_id
                or prepared.tests_receipt_id != intent.tests_receipt_id
                or prepared.snapshot_receipt_id != intent.snapshot_receipt_id
                or live_tree_drift
            ):
                warnings.append(
                    _warning(
                        "PREPARED_RECEIPT_DRIFT",
                        "prepared candidate receipt no longer matches intent and tree evidence",
                        severity="blocking",
                        intent_id=intent.intent_id,
                        allocation_id=record.allocation_id,
                    )
                )

    # Historical publications join release evidence by exact identity, once per snapshot.
    released_publications = {
        (release_receipt.allocation_id, release_receipt.released_record_version,
         plan.publication_receipt_id)
        for release_receipt in snapshot.release_receipts.values()
        if (plan := snapshot.release_plans.get(release_receipt.plan_id)) is not None
    }
    receipts_by_allocation: defaultdict[str, int] = defaultdict(int)
    for receipt in snapshot.publication_receipts.values():
        receipts_by_allocation[receipt.allocation_id] += 1
        record = snapshot.records.get(receipt.allocation_id)
        expected_lifecycle = "RELEASED" if receipt.status == "RELEASED" else "PUBLISHED"
        identity_drift = (
            receipt.repository_id != snapshot.repository_id
            or record is None
            or record.task_commit != receipt.candidate_oid
            or record.finalizer_record_id != receipt.finalizer_record_id
        )
        if not identity_drift and record is not None and record.lifecycle != expected_lifecycle:
            immutable_release_evidence = (
                record.allocation_id, record.record_version, receipt.receipt_id
            ) in released_publications
            if immutable_release_evidence and record.lifecycle == "RELEASED":
                continue
            reconciliations = [
                reconciliation
                for reconciliation in snapshot.reconciliation_receipts.values()
                if reconciliation.source_receipt_id == receipt.receipt_id
                and reconciliation.allocation_id == record.allocation_id
                and reconciliation.observed_record_version == record.record_version
                and reconciliation.result == "RELEASE_STATE_ACKNOWLEDGED"
            ]
            if len(reconciliations) == 1 and record.lifecycle == "RELEASED":
                warnings.append(
                    _warning(
                        "PUBLICATION_RECEIPT_RECONCILED",
                        "immutable publication receipt drift has reconciliation evidence",
                        severity="informational",
                        receipt_id=receipt.receipt_id,
                        allocation_id=receipt.allocation_id,
                    )
                )
                continue
            if (
                not reconciliations
                and receipt.status == "REMOTE_VERIFIED"
                and receipt.lease_released_at is not None
                and record.lifecycle == "RELEASED"
            ):
                warnings.append(
                    _warning(
                        "PUBLICATION_RECEIPT_DRIFT",
                        "released historical receipt needs idempotent reconciliation",
                        severity="degraded",
                        receipt_id=receipt.receipt_id,
                        allocation_id=receipt.allocation_id,
                    )
                )
                continue
        if identity_drift or (record is not None and record.lifecycle != expected_lifecycle):
            warnings.append(
                _warning(
                    "PUBLICATION_RECEIPT_DRIFT",
                    "publication receipt no longer matches repository and candidate evidence",
                    severity="blocking",
                    receipt_id=receipt.receipt_id,
                    allocation_id=receipt.allocation_id,
                )
            )
    for allocation_id, count in receipts_by_allocation.items():
        if count > 1:
            warnings.append(
                _warning(
                    "PUBLICATION_RECEIPT_DRIFT",
                    "one candidate allocation has multiple publication receipts",
                    severity="blocking",
                    allocation_id=allocation_id,
                    count=str(count),
                )
            )

    lease = snapshot.publication_lease
    if lease is not None:
        if lease.repository_id not in {
            snapshot.repository_id,
            "00000000-0000-0000-0000-000000000000",
        }:
            warnings.append(
                _warning(
                    "PUBLICATION_LEASE_DRIFT",
                    "publication lease repository identity differs",
                    allocation_id=lease.allocation_id,
                )
            )
        try:
            if datetime.fromisoformat(lease.expires_at) <= datetime.now(TIMEZONE):
                warnings.append(
                    _warning(
                        "PUBLICATION_LEASE_EXPIRED",
                        "publication lease expired and requires remote-fact recovery",
                        allocation_id=lease.allocation_id,
                    )
                )
        except ValueError:
            warnings.append(
                _warning(
                    "PUBLICATION_LEASE_DRIFT",
                    "publication lease expiry is invalid",
                    allocation_id=lease.allocation_id,
                )
            )
        matched = items_by_allocation.get(lease.allocation_id)
        if matched is None:
            warnings.append(
                _warning(
                    "PUBLICATION_LEASE_DRIFT",
                    "publication lease references no registered allocation",
                    allocation_id=lease.allocation_id,
                )
            )
        else:
            item, record = matched
            if (
                record.lifecycle != "INTEGRATING"
                or item.head != lease.candidate_oid
                or record.finalizer_record_id != lease.validation_evidence_id
            ):
                warnings.append(
                    _warning(
                        "PUBLICATION_LEASE_DRIFT",
                        "publication lease no longer matches candidate evidence",
                        allocation_id=lease.allocation_id,
                    )
                )
            else:
                warnings.append(
                    _warning(
                        "ACTIVE_PUBLICATION_LEASE",
                        "main publication authority is currently leased",
                        allocation_id=lease.allocation_id,
                    )
                )
                try:
                    tracking_head = resolve_commit(
                        repository.canonical_path,
                        f"refs/remotes/{lease.remote}/{lease.branch}",
                    )
                except ControllerError:
                    tracking_head = None
                if tracking_head != lease.expected_main_oid:
                    warnings.append(
                        _warning(
                            "PUBLICATION_REMOTE_DRIFT",
                            "local publication tracking ref changed after lease acquisition",
                            allocation_id=lease.allocation_id,
                        )
                    )
    for allocation_id, (_item, record) in items_by_allocation.items():
        if record.lifecycle == "INTEGRATING" and (
            lease is None or lease.allocation_id != allocation_id
        ):
            warnings.append(
                _warning(
                    "PUBLICATION_LEASE_DRIFT",
                    "integrating lifecycle has no matching publication lease",
                    allocation_id=allocation_id,
                )
            )
    return warnings


def doctor(repository_path: Path, *, namespace_pid: int | None = None) -> dict[str, Any]:
    result = envelope("doctor")
    try:
        repository = inventory(repository_path)
    except ControllerError as exc:
        result["environment_diagnosis"] = {
            "code": "UNKNOWN",
            "facts": ["GIT_INVENTORY_UNAVAILABLE"],
        }
        result["errors"].append({"code": exc.code, "message": str(exc)})
        result["decision"] = {
            "code": "FAIL",
            "reason": "Git inventory is unavailable; mount and lock authority cannot be confirmed",
            "primary_diagnosis": "UNKNOWN",
        }
        return result
    diagnostics = diagnose_mounts(repository, namespace_pid=namespace_pid)
    diagnosis = diagnostics["environment"]
    result["mount_diagnostics"] = diagnostics
    result["environment_diagnosis"] = diagnosis
    result["warnings"] = _environment_warnings(diagnostics)
    try:
        policy = load_policy(repository.canonical_path)
    except ControllerError as exc:
        result["errors"].append({"code": exc.code, "message": str(exc)})
        from .policy import default_policy

        policy = default_policy(repository.canonical_path)
    store = MetadataStore(repository.common_dir)
    with ExitStack() as stack:
        try:
            # Release/refresh must not invalidate Git paths while the snapshot's
            # semantic and transaction evidence is still being read.
            stack.enter_context(store.cleanup_observation_lock())
            repository = inventory(repository_path)
            snapshot = store.load()
        except ControllerError as exc:
            error = _environment_first(exc, diagnosis)
            result["repository"] = {
                "canonical_path": str(repository.canonical_path),
                "common_dir": str(repository.common_dir),
                "worktree_count": len(repository.worktrees),
                "normal_limit": policy.normal_limit,
                "hard_limit": policy.hard_limit,
                "metadata_path": str(store.root),
            }
            result["errors"].append({"code": error.code, "message": str(error)})
            result["decision"] = {
                "code": "FAIL",
                "reason": "repository environment or controller metadata is unavailable",
                "primary_diagnosis": _primary_diagnosis(
                    diagnosis, result["warnings"] + result["errors"], failed=True
                ),
            }
            return result
        return _doctor_snapshot(result, repository, policy, store, snapshot)


def _doctor_snapshot(
    result: dict[str, Any],
    repository: RepositoryInventory,
    policy: WorktreePolicy,
    store: MetadataStore,
    snapshot: MetadataSnapshot,
) -> dict[str, Any]:
    diagnosis = result["environment_diagnosis"]
    warnings = _metadata_warnings(repository, policy, snapshot)
    result["repository"] = _repository_payload(repository, policy, snapshot)
    result["worktrees"] = _observed_worktrees(
        repository, policy, snapshot, namespace_healthy=diagnosis["code"] == "HEALTHY"
    )
    result["orphan_observations"] = observe_repository_orphans(repository, snapshot)
    result["warnings"] += (
        warnings
        + _semantic_warnings(repository, snapshot, policy)
        + (_lock_warnings(store) if diagnosis["code"] == "HEALTHY" else [])
    )
    transaction_count = 0
    temp_count = 0
    retirement_grants: list[dict[str, str]] = []
    retirement_unattested: list[dict[str, str]] = []
    historical_transactions = []
    transaction_fields = {
        "schema_version",
        "transaction_id",
        "repository_id",
        "command",
        "phase",
        "target_path",
        "allocation_id",
        "branch",
        "expected_head",
        "expected_record_version",
        "created_at",
        "updated_at",
    }
    try:
        with store.lock(exclusive=False):
            if store.transactions_dir.exists():
                for transaction_path in sorted(store.transactions_dir.iterdir()):
                    from .transaction_scope import is_atomic_temp, terminal_state

                    if is_atomic_temp(transaction_path.name):
                        temp_count += 1
                        continue
                    if transaction_path.suffix != ".json" or not transaction_path.is_file():
                        raise ControllerError("INVALID_TRANSACTION", "unknown transaction entry")
                    state = terminal_state(store, transaction_path.stem)
                    if state is not None:
                        historical_transactions.append(
                            {"transaction_id": transaction_path.stem, "state": state}
                        )
                        continue
                    transaction = store._read_json(transaction_path)
                    fields = set(transaction)
                    release_fields = transaction_fields | {"release_plan_id"}
                    teardown_fields = release_fields | {"teardown_receipt_id"}
                    refresh_fields = transaction_fields | {"new_head", "intent_id"}
                    cleanup_fields = transaction_fields | {"cleanup_plan"}
                    cleanup_transaction = fields in (
                        cleanup_fields,
                        cleanup_fields | {"actions"},
                        cleanup_fields | {"actions", "failure"},
                    )
                    teardown_transaction = fields == teardown_fields
                    planned_release_transaction = fields in (release_fields, teardown_fields)
                    refresh_transaction = fields == refresh_fields
                    teardown_phases = {
                        "TEARDOWN_PREPARED",
                        "VENV_REMOVAL_STARTED",
                        "VENV_REMOVED",
                    }
                    phase = transaction.get("phase")
                    if (
                        (
                            fields != transaction_fields
                            and fields != release_fields
                            and fields != teardown_fields
                            and fields != refresh_fields
                            and not cleanup_transaction
                        )
                        or transaction.get("schema_version") not in {1, 2, 3, 4}
                        or transaction.get("command")
                        not in {"acquire", "release", "integration-refresh", "resource-clean"}
                        or phase
                        not in {
                            "PREPARED",
                            "TEARDOWN_PREPARED",
                            "VENV_REMOVAL_STARTED",
                            "VENV_REMOVED",
                            "WORKTREE_ADDED",
                            "METADATA_WRITTEN",
                            "WORKTREE_REMOVED",
                            "RELEASED_RECORDED",
                            "REFRESH_PREPARED",
                            "DETACHED_AT_NEW_HEAD",
                            "BRANCH_UPDATED",
                        }
                        or (teardown_transaction and transaction.get("command") != "release")
                        or (planned_release_transaction and transaction.get("command") != "release")
                        or (
                            transaction.get("schema_version") == 3
                            and transaction.get("command") == "release"
                            and not planned_release_transaction
                        )
                        or (
                            refresh_transaction
                            and transaction.get("command") != "integration-refresh"
                        )
                        or (phase in teardown_phases and not teardown_transaction)
                        or (
                            transaction.get("command") == "resource-clean"
                            and (not cleanup_transaction or phase != "PREPARED")
                        )
                        or (cleanup_transaction and transaction.get("command") != "resource-clean")
                        or (
                            phase in {"REFRESH_PREPARED", "DETACHED_AT_NEW_HEAD", "BRANCH_UPDATED"}
                            and not refresh_transaction
                        )
                        or (
                            teardown_transaction
                            and transaction.get("teardown_receipt_id")
                            != transaction.get("transaction_id")
                        )
                    ):
                        raise ControllerError(
                            "INVALID_TRANSACTION",
                            f"transaction journal schema is invalid: {transaction_path}",
                        )
                    if cleanup_transaction:
                        cleanup_plan = transaction.get("cleanup_plan")
                        if not isinstance(cleanup_plan, dict) or any(
                            cleanup_plan.get(key) != transaction[key]
                            for key in (
                                "repository_id",
                                "allocation_id",
                                "target_path",
                                "expected_head",
                                "expected_record_version",
                            )
                        ):
                            raise ControllerError(
                                "INVALID_TRANSACTION", "cleanup plan does not match journal"
                            )
                        if "actions" in transaction:
                            actions = transaction["actions"]
                            if (
                                not isinstance(actions, list)
                                or not all(
                                    isinstance(action, dict)
                                    and set(action) == {"path", "status"}
                                    and action["status"]
                                    in {"PENDING", "STARTED", "COMPLETED", "FAILED_OR_PARTIAL"}
                                    for action in actions
                                )
                                or [action["path"] for action in actions]
                                != cleanup_plan.get("cleanup_paths")
                            ):
                                raise ControllerError(
                                    "INVALID_TRANSACTION", "cleanup action progress is invalid"
                                )
                        if "failure" in transaction:
                            failure = transaction["failure"]
                            if (
                                not isinstance(failure, dict)
                                or set(failure) != {"code", "errno"}
                                or not isinstance(failure["code"], str)
                                or (
                                    failure["errno"] is not None
                                    and not isinstance(failure["errno"], int)
                                )
                            ):
                                raise ControllerError(
                                    "INVALID_TRANSACTION", "cleanup failure evidence is invalid"
                                )
                    if planned_release_transaction:
                        plan_id = transaction.get("release_plan_id")
                        plan = (
                            snapshot.release_plans.get(plan_id)
                            if isinstance(plan_id, str)
                            else None
                        )
                        if (
                            plan is None
                            or plan.repository_id != transaction["repository_id"]
                            or plan.allocation_id != transaction["allocation_id"]
                            or plan.target_path != transaction["target_path"]
                            or plan.branch != transaction["branch"]
                            or plan.expected_head != transaction["expected_head"]
                            or plan.expected_record_version
                            != transaction["expected_record_version"]
                        ):
                            raise ControllerError(
                                "INVALID_TRANSACTION",
                                f"release plan does not match journal: {transaction_path}",
                            )
                    if teardown_transaction:
                        receipt = store.load_teardown_receipt(
                            str(transaction["teardown_receipt_id"])
                        )
                        expected_results = {
                            "TEARDOWN_PREPARED": {"prepared"},
                            "VENV_REMOVAL_STARTED": {"removal_started"},
                            "VENV_REMOVED": {"removed"},
                            "PREPARED": {"removed"},
                            "WORKTREE_REMOVED": {"removed"},
                            "RELEASED_RECORDED": {"removed", "released"},
                        }.get(phase)
                        if (
                            expected_results is None
                            or receipt["result"] not in expected_results
                            or receipt["repository_id"] != transaction["repository_id"]
                            or receipt["allocation_id"] != transaction["allocation_id"]
                            or receipt["worktree_path"] != transaction["target_path"]
                            or receipt["worktree_head"] != transaction["expected_head"]
                        ):
                            raise ControllerError(
                                "INVALID_TRANSACTION",
                                f"teardown receipt does not match journal: {transaction_path}",
                            )
                    transaction_count += 1
            outstanding, unattested = _retirement_execution_findings(store)
            retirement_grants.extend(outstanding)
            retirement_unattested.extend(unattested)
    except ControllerError as exc:
        error = _environment_first(exc, diagnosis)
        result["errors"].append({"code": error.code, "message": str(error)})
    if transaction_count:
        result["warnings"].append(
            _warning(
                "INTERRUPTED_TRANSACTION",
                "controller transaction journal requires review",
                count=str(transaction_count),
            )
        )
    if temp_count:
        result["warnings"].append(
            _warning(
                "ATOMIC_TEMP_RESIDUE",
                "interrupted atomic write temp files remain in transactions/ and are ignored",
                count=str(temp_count),
            )
        )
    for grant in retirement_grants:
        result["warnings"].append(
            _warning(
                "RETIREMENT_AUTHORIZATION_OUTSTANDING",
                "a retirement execution authorization is open for this repository",
                authorization_id=grant["authorization_id"],
                plan_id=grant["plan_id"],
                operation=grant["operation"],
                branch=grant["branch"],
                expires_at=grant["expires_at"],
                severity="degraded",
            )
        )
    for removal in retirement_unattested:
        result["warnings"].append(
            _warning(
                removal["code"],
                "a retirement ref is absent with no Controller-observed authorized transition",
                authorization_id=removal["authorization_id"],
                plan_id=removal["plan_id"],
                operation=removal["operation"],
                ref=removal.get("ref", ""),
                severity="blocking",
            )
        )
    failure_codes = {
        "ORPHAN_BINDING",
        "ORPHAN_RECORD",
        "MISSING_RECORD",
        "DUPLICATE_BINDING",
        "DUPLICATE_AUTHORITY",
        "DUPLICATE_TASK",
        "BUDGET_VIOLATION",
        "INTEGRATION_CAPACITY_VIOLATION",
        "FEATURE_CAPACITY_VIOLATION",
        "EMERGENCY_CAPACITY_VIOLATION",
        "RECOVERY_CAPACITY_VIOLATION",
        "MISSING_POLICY",
        "MISSING_REPO_METADATA",
        "REPOSITORY_LOCK_MISSING",
        "REPOSITORY_LOCK_INVALID",
        "INTERRUPTED_TRANSACTION",
        "MISSING_TASK_COMMIT",
        "TASK_HEAD_MISMATCH",
        "DELIVERED_CLOSEOUT_INVALID",
        "RETAINED_ARCHIVE_INVALID",
        "HANDOFF_HEAD_MISMATCH",
        "MISSING_FINALIZER_COMMIT",
        "FINALIZER_HEAD_MISMATCH",
        "MISSING_INTEGRATION_BASE",
        "INTEGRATION_BASE_DIVERGED",
        "MISSING_INTEGRATION_SOURCE",
        "MISSING_SOURCE_ALLOCATION",
        "SOURCE_EVIDENCE_MISMATCH",
        "MISSING_SOURCE_SCOPE",
        "INTEGRATION_SCOPE_MISMATCH",
        "COMMIT_SCOPE_VIOLATION",
        "PATH_SCOPE_VIOLATION",
        "RECOVERY_REQUIRED",
        "QUARANTINED",
        "PUBLICATION_LEASE_DRIFT",
        "PUBLICATION_REMOTE_DRIFT",
        "PUBLICATION_LEASE_EXPIRED",
        "INTEGRATION_INTENT_DRIFT",
        "INVALID_RETIREMENT_EXECUTION_RECORD",
        "RETIREMENT_REMOVAL_UNATTESTED",
    }
    failed = bool(result["errors"]) or any(
        warning.get("severity") == "blocking"
        or (warning.get("severity") is None and warning["code"] in failure_codes)
        for warning in result["warnings"]
    )
    degraded = any(warning.get("severity") == "degraded" for warning in result["warnings"])
    result["decision"] = {
        "code": "FAIL" if failed else "DEGRADED" if degraded else "PASS",
        "primary_diagnosis": _primary_diagnosis(
            diagnosis, result["warnings"] + result["errors"], failed=failed
        ),
        "reason": (
            "diagnostic findings require review"
            if failed
            else "non-blocking lifecycle findings remain"
            if degraded
            else "no blocking drift detected"
        ),
        "transactions": {
            "historical_terminal": historical_transactions,
            "unresolved_count": transaction_count,
        },
        "retirement_executions": {
            "outstanding": retirement_grants,
            "unattested_removals": retirement_unattested,
        },
    }
    return result
