"""Public model constants and small immutable data structures."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
METADATA_SCHEMA_VERSION = 4
SUPPORTED_METADATA_SCHEMA_VERSIONS = frozenset({1, 2, 3, 4})
TERMINAL_INTEGRATION_STATES = frozenset({"REMOTE_VERIFIED", "RELEASED", "ABANDONED"})

LIFECYCLE_STATES = frozenset(
    {
        "PLANNED",
        "ACTIVE",
        "RETAINED",
        "RELEASE_READY",
        "ARCHIVED",
        "NO_CHANGE_COMPLETED",
        "INTEGRATION_ABORTED",
        "HANDOFF_READY",
        "PRESERVED",
        "INTEGRATING",
        "PUBLISHED",
        "RELEASED",
        "BLOCKED",
        "QUARANTINED",
        "RECOVERY_REQUIRED",
    }
)
INTEGRATION_INTENT_STATES = frozenset(
    {
        "QUEUED",
        "WAITING_SCOPE",
        "PREPARING_INTEGRATION",
        "READY_TO_PUBLISH",
        "NEEDS_REPREPARE",
        "PUBLISHING",
        "RECOVERY_REQUIRED",
        "ATTENTION_REQUIRED",
        "REMOTE_VERIFIED",
        "RELEASED",
        # Accepted only while reading pre-0.6 intent records.
        "CLAIMED",
        "VALIDATED",
        "BLOCKED",
        "PUBLISHED",
    }
)
INTEGRATION_EVENT_TYPES = frozenset(
    {
        "INTENT_QUEUED",
        "PREPARE_SLOT_GRANTED",
        "PREPARED",
        "NEEDS_REPREPARE",
        "ATTENTION_REQUIRED",
        "PUBLICATION_LEASE_GRANTED",
        "REMOTE_VERIFIED",
        "RECOVERY_REQUIRED",
        "RELEASED",
    }
)
RELEASE_INTENT_STATES = frozenset(
    {
        "PENDING",
        "BLOCKED_RETRYABLE",
        "STALE",
        "CANCELLED",
        "RELEASED",
    }
)
ACTIVE_AUTHORITY_STATES = frozenset(
    {
        "PLANNED",
        "ACTIVE",
        "ARCHIVED",
        "HANDOFF_READY",
        "INTEGRATING",
        "BLOCKED",
        "QUARANTINED",
        "RECOVERY_REQUIRED",
    }
)
CANONICAL_ROLES = frozenset(
    {"canonical", "control-plane", "integration", "publication", "verification"}
)
#: Roles that share one bounded physical pool of ordinary worktrees.
ORDINARY_ROLES = frozenset({"feature", "audit"})
EMERGENCY_ROLES = frozenset({"hotfix", "emergency"})
RECOVERY_ROLES = frozenset({"preservation", "recovery"})
FEATURE_AUTHORITY_ROLES = frozenset({"feature", "audit", "hotfix", "emergency"})
WORKTREE_ROLES = frozenset(
    {
        "canonical",
        "control-plane",
        "feature",
        "integration",
        "audit",
        "preservation",
        "recovery",
        "hotfix",
        "emergency",
        "publication",
        "verification",
    }
)

PLAN_DECISIONS = frozenset(
    {
        "REUSE",
        "CREATE_ALLOWED",
        "CREATE_EMERGENCY_ALLOWED",
        "CREATE_RECOVERY_ALLOWED",
        "BLOCK_BUDGET",
        "BLOCK_CANONICAL",
        "BLOCK_INTEGRATION_SLOT",
        "BLOCK_FEATURE_SLOT",
        "BLOCK_ORDINARY_POOL",
        "BLOCK_EMERGENCY_SLOT",
        "BLOCK_RECOVERY_RESERVE",
        "BLOCK_DUPLICATE_AUTHORITY",
        "NEEDS_ADOPTION",
        "BLOCK_DRIFT",
        "UNKNOWN",
    }
)


class ControllerError(RuntimeError):
    """Controlled failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WorktreeGitState:
    path: Path
    git_dir: Path
    key: str
    canonical: bool
    head: str
    branch: str | None
    detached: bool
    locked: bool
    lock_reason: str | None
    prunable: bool
    prunable_reason: str | None
    clean: bool
    staged: int
    modified: int
    untracked: int


@dataclass(frozen=True)
class RepositoryInventory:
    requested_path: Path
    canonical_path: Path
    common_dir: Path
    worktrees: tuple[WorktreeGitState, ...]


@dataclass(frozen=True)
class ConvergencePolicy:
    """Delegated retention convergence: how long an unused checkout may stay on disk.

    ``standing_reserve`` is the number of idle checkouts tolerated beside the live
    writers before idle thresholds start to shrink; it is a pressure reference, never
    an admission limit.

    ``release_unintegrated_handoffs`` lets the reconciler release the checkout of a
    frozen, integration-ready handoff whose exact HEAD is retained by both the local
    and the remote-tracking branch before it is integrated; integration then replays
    from that retained branch.
    """

    enabled: bool
    grace_minutes: int
    idle_hours_max: int
    idle_hours_min: int
    standing_reserve: int
    release_unintegrated_handoffs: bool = False


@dataclass(frozen=True)
class WorktreePolicy:
    path: Path
    present: bool
    policy_schema_version: int
    normal_limit: int
    hard_limit: int
    canonical_protected: bool
    canonical_allowed_roles: frozenset[str]
    integration_candidate_limit: int
    integration_prepare_limit: int
    feature_audit_slots: int
    emergency_slots: int
    recovery_reserve: int
    retained_slots: int
    reconciler_enabled: bool
    reconciler_actor: str
    reconciler_legacy_adoption: bool
    main_publish_limit: int
    publication_remote: str
    publication_branch: str
    automatic_feature_audit_release: bool
    automatic_integration_continuation: bool
    preserved_auto_release: bool
    release_intent_ttl_seconds: int
    stale_warning_days: int
    require_adoption: bool
    require_task_key: bool
    require_authority_key: bool
    unique_active_feature_authority: bool
    resource_admission: bool = True
    checkout_growth_bytes: int = 2 * 1024**3
    checkout_growth_inodes: int = 100_000
    convergence: ConvergencePolicy | None = None


@dataclass(frozen=True)
class MetadataRecord:
    schema_version: int
    record_version: int
    allocation_id: str
    task_key: str
    authority_key: str
    module: str
    role: str
    owner: str
    lifecycle: str
    created_at: str
    last_reviewed_at: str
    handoff_commit: str | None
    finalizer_record_id: str | None
    base_commit: str | None
    task_commit: str | None
    finalizer_commit: str | None
    scope_paths: tuple[str, ...]
    source_allocations: tuple[str, ...]
    quarantine_expected_commit: str | None
    quarantine_observed_commit: str | None
    quarantine_reason: str | None
    recovery_allocation_id: str | None
    recovery_ref: str | None
    disposable_resources: tuple[dict[str, Any], ...]
    external_bindings: tuple[str, ...]
    history: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class IntegrationIntent:
    schema_version: int
    contract_version: int
    intent_id: str
    task_key: str
    authority_key: str
    module: str
    owner: str
    source_allocations: tuple[str, ...]
    source_handoffs: tuple[str, ...]
    source_branches: tuple[str, ...]
    source_finalizer_record_ids: tuple[str, ...]
    source_snapshot_ids: tuple[str, ...]
    scope_paths: tuple[str, ...]
    scope_digest: str
    queue_sequence: int
    state: str
    allocation_id: str | None
    base_main_oid: str | None
    candidate_oid: str | None
    revision: int
    attempt: int
    prepare_identity: str | None
    publication_identity: str | None
    recovery_identity: str | None
    prepared_receipt_id: str | None
    prepared_base_origin_main_oid: str | None
    prepared_commit_oid: str | None
    prepared_tree_oid: str | None
    prepared_scope_digest: str | None
    tests_receipt_id: str | None
    snapshot_receipt_id: str | None
    validation_evidence_id: str | None
    validated_oid: str | None
    blocked_reason: str | None
    enqueued_at: str
    claimed_at: str | None
    validated_at: str | None
    published_at: str | None
    released_at: str | None
    updated_at: str
    equivalent_replays: tuple[dict[str, str], ...]
    history: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class PreparedCandidateReceipt:
    schema_version: int
    receipt_id: str
    repository_id: str
    intent_id: str
    allocation_id: str
    attempt: int
    base_origin_main_oid: str
    candidate_oid: str
    tree_oid: str
    scope_digest: str
    prepare_identity: str
    validation_evidence_id: str
    tests_receipt_id: str
    snapshot_receipt_id: str
    created_at: str


@dataclass(frozen=True)
class LifecycleEvent:
    schema_version: int
    event_id: str
    repository_id: str
    intent_id: str
    allocation_id: str | None
    event_type: str
    state: str
    sequence: int
    identity: str
    created_at: str


@dataclass(frozen=True)
class IntegrationReconciliationReceipt:
    schema_version: int
    receipt_id: str
    repository_id: str
    intent_id: str
    observed_intent_schema_version: int
    observed_intent_state: str
    observed_allocation_id: str | None
    observed_record_version: int | None
    observed_record_lifecycle: str | None
    effective_terminal_state: str
    result: str
    actor: str
    reconciled_at: str


@dataclass(frozen=True)
class PublicationLease:
    schema_version: int
    lease_id: str
    repository_id: str
    allocation_id: str
    holder: str
    run_id: str
    remote: str
    branch: str
    target_ref: str
    candidate_oid: str
    expected_main_oid: str
    validation_evidence_id: str
    acquired_at: str
    expires_at: str


@dataclass(frozen=True)
class WriterHolder:
    """Identity of the live process that currently owns write permission.

    ``pid`` alone cannot identify a process: Linux reuses pids, and a stale lease
    naming a recycled pid would read as live. ``boot_id`` plus the kernel's
    ``starttime`` for that pid make the triple unique for the life of the boot, so
    a recycled pid never satisfies a lease written for its predecessor.
    """

    client_id: str
    pid: int
    boot_id: str
    start_ticks: int
    session_id: str | None
    run_id: str | None


@dataclass(frozen=True)
class WriterLease:
    schema_version: int
    lease_id: str
    repository_id: str
    allocation_id: str
    worktree_path: str
    owner: str
    holder: WriterHolder
    acquired_at: str
    renewed_at: str
    expires_at: str | None
    record_version: int


@dataclass(frozen=True)
class WriterReconciliationReceipt:
    schema_version: int
    receipt_id: str
    repository_id: str
    allocation_id: str
    actor: str
    owner: str
    trigger: str
    outcome: str
    reason: str
    evidence: tuple[str, ...]
    previous_lifecycle: str
    new_lifecycle: str
    reconciled_at: str


@dataclass(frozen=True)
class PublicationReceipt:
    schema_version: int
    receipt_id: str
    repository_id: str
    allocation_id: str
    intent_id: str | None
    target_ref: str
    holder: str
    run_id: str
    expected_main_oid: str
    candidate_oid: str
    validation_evidence_id: str
    finalizer_record_id: str
    status: str
    acquired_at: str
    remote_verified_at: str
    lease_released_at: str | None
    released_at: str | None
    lease_duration_ms: int
    remote_verified_to_released_ms: int | None
    source_results: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class ReleaseIntent:
    schema_version: int
    intent_id: str
    repository_id: str
    allocation_id: str
    task_key: str
    authority_key: str
    owner: str
    role: str
    expected_head: str
    expected_record_version: int
    task_commit: str
    handoff_commit: str
    finalizer_commit: str
    finalizer_record_id: str
    integration_intent_id: str | None
    integration_allocation_id: str | None
    publication_receipt_id: str | None
    integration_candidate_oid: str | None
    integration_mode: str | None
    integration_evidence_id: str | None
    trigger: str
    status: str
    blocker: str | None
    created_at: str
    updated_at: str
    expires_at: str
    history: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ReleasePlan:
    schema_version: int
    plan_id: str
    repository_id: str
    allocation_id: str
    release_intent_id: str | None
    target_path: str
    branch: str
    expected_head: str
    expected_record_version: int
    owner: str
    authority_key: str
    role: str
    lifecycle: str
    trigger: str
    publication_receipt_id: str | None
    resource_actions: tuple[dict[str, Any], ...]
    created_at: str


@dataclass(frozen=True)
class ReleaseReceipt:
    schema_version: int
    receipt_id: str
    plan_id: str
    repository_id: str
    allocation_id: str
    release_intent_id: str | None
    target_path: str
    branch: str
    released_head: str
    expected_record_version: int
    released_record_version: int
    trigger: str
    resource_receipt_ids: tuple[str, ...]
    result: str
    released_at: str


@dataclass(frozen=True)
class ReconciliationReceipt:
    schema_version: int
    receipt_id: str
    repository_id: str
    source_receipt_id: str
    allocation_id: str
    observed_record_version: int
    observed_record_lifecycle: str
    observed_receipt_status: str
    differences: tuple[str, ...]
    recovered_intent_id: str | None
    recovered_intent_state: str | None
    result: str
    actor: str
    reconciled_at: str


@dataclass(frozen=True)
class MetadataSnapshot:
    repository_id: str | None
    records: dict[str, MetadataRecord]
    bindings: dict[str, str]
    integration_intents: dict[str, IntegrationIntent]
    prepared_candidate_receipts: dict[str, PreparedCandidateReceipt]
    integration_reconciliation_receipts: dict[str, IntegrationReconciliationReceipt]
    release_intents: dict[str, ReleaseIntent]
    release_plans: dict[str, ReleasePlan]
    publication_lease: PublicationLease | None
    publication_receipts: dict[str, PublicationReceipt]
    release_receipts: dict[str, ReleaseReceipt]
    reconciliation_receipts: dict[str, ReconciliationReceipt]
    writer_leases: dict[str, WriterLease]
    warnings: tuple[dict[str, str], ...]
