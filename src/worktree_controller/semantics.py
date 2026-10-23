"""Semantic evidence checks for task, integration, and publication state."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

from .git_backend import (
    changed_paths,
    commits_with_parents,
    inventory,
    is_ancestor,
    resolve_commit,
    trusted_publication_base,
)
from .models import (
    ControllerError,
    IntegrationIntent,
    IntegrationReconciliationReceipt,
    MetadataRecord,
    MetadataSnapshot,
    RepositoryInventory,
    WorktreeGitState,
    WorktreePolicy,
)
from .policy import load_policy

EVIDENCE_LIFECYCLES = frozenset({"HANDOFF_READY", "PRESERVED", "INTEGRATING", "PUBLISHED"})


def record_for(item: WorktreeGitState, snapshot: MetadataSnapshot) -> MetadataRecord | None:
    allocation_id = snapshot.bindings.get(item.key)
    if allocation_id is None:
        return None
    return snapshot.records.get(allocation_id)


def managed_record(
    repository: RepositoryInventory,
    snapshot: MetadataSnapshot,
    target: Path,
) -> tuple[WorktreeGitState, MetadataRecord]:
    """Resolve a Git registration against the caller's already locked snapshot."""
    try:
        resolved = target.expanduser().resolve(strict=True)
    except OSError as exc:
        raise ControllerError("INVALID_TARGET", f"target path is unavailable: {target}") from exc
    for item in repository.worktrees:
        if item.path.exists() and item.path.resolve(strict=True) == resolved:
            record = record_for(item, snapshot)
            if record is None:
                raise ControllerError("NEEDS_ADOPTION", "registered worktree is not adopted")
            return item, record
    raise ControllerError("UNREGISTERED_WORKTREE", f"target is not registered: {resolved}")


def assert_record_version(
    record: MetadataRecord,
    expected_record_version: int,
    expected_allocation_id: str | None = None,
) -> None:
    if expected_allocation_id is not None and record.allocation_id != expected_allocation_id:
        raise ControllerError("IDENTITY_MISMATCH", "target allocation changed")
    if expected_record_version != record.record_version:
        raise ControllerError(
            "CAS_MISMATCH",
            f"expected record version {expected_record_version}, observed {record.record_version}",
        )


def integration_intent_abandoned(intent: IntegrationIntent) -> bool:
    """Recognize the explicit terminal event without forgiving unexplained drift."""
    return (
        intent.state == "ATTENTION_REQUIRED"
        and intent.allocation_id is None
        and bool(intent.blocked_reason)
        and bool(intent.history)
        and intent.history[-1].get("event") == "PREPARATION_ABANDONED"
    )


def integration_reconciliation(
    snapshot: MetadataSnapshot, intent_id: str
) -> IntegrationReconciliationReceipt | None:
    matches = [
        receipt
        for receipt in snapshot.integration_reconciliation_receipts.values()
        if receipt.intent_id == intent_id and receipt.result == "STALE_INTENT_TERMINAL_ACKNOWLEDGED"
    ]
    if len(matches) > 1:
        raise ControllerError(
            "INVALID_METADATA", "integration intent has multiple terminal reconciliations"
        )
    return matches[0] if matches else None


def effective_intent_state(snapshot: MetadataSnapshot, intent: IntegrationIntent) -> str:
    """Interpret terminal evidence from the caller's snapshot without changing authority."""
    receipt = integration_reconciliation(snapshot, intent.intent_id)
    if receipt is not None:
        return receipt.effective_terminal_state
    return "ABANDONED" if integration_intent_abandoned(intent) else intent.state


def _current_attempt(intent: IntegrationIntent) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split history at the last formal retry: (earlier attempts, current attempt)."""
    history = list(intent.history)
    starts = [index for index, event in enumerate(history)
              if event.get("event") == "PREPARATION_RETRY_QUEUED"]
    if not starts:
        return [], history
    return history[: starts[-1]], history[starts[-1]:]


def unallocated_integration_conflict(intent: IntegrationIntent) -> bool:
    """Recognize the producer's pre-allocation failure, never infer a missing release.

    Only the current attempt must be unclaimed. An earlier attempt counts only when
    it ended in a formal abort before the retry; the retry path itself rechecks that
    its candidate was released.
    """
    earlier, current = _current_attempt(intent)
    return (
        intent.state == "ATTENTION_REQUIRED"
        and intent.allocation_id is None
        and intent.candidate_oid is None
        and (intent.revision == 0 or bool(earlier))
        and intent.claimed_at is None
        and intent.prepared_receipt_id is None
        and intent.validated_oid is None
        and intent.publication_identity is None
        and intent.published_at is None
        and intent.released_at is None
        and bool(intent.base_main_oid)
        and bool(intent.blocked_reason)
        and bool(current)
        and current[-1].get("event") == "INTEGRATION_CONFLICT_BLOCKED"
        and not any(event.get("event") == "CANDIDATE_CLAIMED" for event in current)
        and (
            not any(event.get("event") == "CANDIDATE_CLAIMED" for event in earlier)
            or any(event.get("event") == "PREPARATION_ABORTED" for event in earlier)
        )
    )


def integration_prerequisite(intent: IntegrationIntent) -> str | None:
    """Read the producer's append-only prerequisite decision, without changing enqueue identity."""
    matches = [event for event in intent.history
               if event.get("event") == "RETRY_PREREQUISITE_BOUND"]
    if len(matches) > 1:
        raise ControllerError("INVALID_METADATA", "intent has multiple retry prerequisites")
    if not matches:
        return None
    value = matches[0].get("prerequisite_intent_id")
    if not isinstance(value, str) or not value or value == intent.intent_id:
        raise ControllerError("INVALID_METADATA", "invalid retry prerequisite identity")
    return value


def integration_queue_order(
    snapshot: MetadataSnapshot, intent: IntegrationIntent,
) -> tuple[int, int | str, int, str]:
    """A verified prerequisite inherits only its failed parent's scheduling position."""
    parents = [parent for parent in snapshot.integration_intents.values()
               if integration_prerequisite(parent) == intent.intent_id]
    if len(parents) > 1:
        raise ControllerError("INVALID_METADATA", "prerequisite belongs to multiple failed intents")
    anchor = parents[0] if parents else intent
    if parents and (anchor.queue_sequence <= 0 or anchor.queue_sequence >= intent.queue_sequence
                    or anchor.owner != intent.owner or integration_prerequisite(intent)):
        raise ControllerError("INVALID_METADATA", "invalid prerequisite scheduling relationship")
    phase = 0 if parents else 1
    if anchor.queue_sequence > 0:
        return (1, anchor.queue_sequence, phase, intent.intent_id)
    return (0, anchor.enqueued_at, phase, intent.intent_id)


def normalize_scope_paths(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: set[str] = set()
    for raw in values:
        if not raw or "\x00" in raw or "\\" in raw:
            raise ControllerError("INVALID_SCOPE", f"scope path is unsafe: {raw!r}")
        path = PurePosixPath(raw)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ControllerError("INVALID_SCOPE", f"scope path is unsafe: {raw}")
        value = path.as_posix().rstrip("/")
        if value == ".git" or value.startswith(".git/"):
            raise ControllerError("INVALID_SCOPE", "Git administrative paths cannot be scoped")
        normalized.add(value)
    return tuple(sorted(normalized))


def path_in_scopes(path: str, scopes: tuple[str, ...]) -> bool:
    return any(path == scope or path.startswith(f"{scope}/") for scope in scopes)


def integration_source_evidence(
    snapshot: MetadataSnapshot, source_allocations: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not source_allocations:
        raise ControllerError(
            "MISSING_INTEGRATION_SOURCE",
            "integration candidates require at least one integration-ready source allocation",
        )
    source_heads: list[str] = []
    scopes: set[str] = set()
    for allocation_id in source_allocations:
        source = snapshot.records.get(allocation_id)
        if source is None:
            raise ControllerError(
                "MISSING_SOURCE_ALLOCATION", f"source allocation is missing: {allocation_id}"
            )
        integration_ready = any(
            event.get("event") == "HANDOFF_INTEGRATION_READY" for event in source.history
        )
        if (
            not integration_ready
            or not source.handoff_commit
            or not source.finalizer_record_id
            or not source.finalizer_commit
            or source.finalizer_commit != source.handoff_commit
        ):
            raise ControllerError(
                "SOURCE_EVIDENCE_MISMATCH",
                f"source allocation lacks matching integration-ready evidence: {allocation_id}",
            )
        if not source.scope_paths:
            raise ControllerError(
                "MISSING_SOURCE_SCOPE", f"source allocation has no path scope: {allocation_id}"
            )
        source_heads.append(source.handoff_commit)
        scopes.update(source.scope_paths)
    return tuple(source_heads), tuple(sorted(scopes))


def integration_intent_evidence(
    snapshot: MetadataSnapshot, record: MetadataRecord
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    matches = [
        intent
        for intent in snapshot.integration_intents.values()
        if intent.allocation_id == record.allocation_id
    ]
    if len(matches) > 1:
        raise ControllerError("INVALID_METADATA", "multiple intents bind one integration candidate")
    if not matches:
        return integration_source_evidence(snapshot, record.source_allocations)
    intent = matches[0]
    if intent.source_allocations != record.source_allocations:
        raise ControllerError(
            "INTEGRATION_INTENT_MISMATCH",
            "candidate source allocations differ from its frozen integration intent",
        )
    if intent.scope_paths != record.scope_paths:
        raise ControllerError(
            "INTEGRATION_SCOPE_MISMATCH",
            "candidate scope differs from its frozen integration intent",
        )
    if intent.candidate_oid is not None and intent.candidate_oid != record.task_commit:
        raise ControllerError(
            "INTEGRATION_INTENT_MISMATCH",
            "candidate revision differs from its integration intent",
        )
    return intent.source_handoffs, intent.scope_paths


def integration_replay_evidence(
    snapshot: MetadataSnapshot, record: MetadataRecord
) -> tuple[dict[str, str], ...]:
    matches = [
        intent
        for intent in snapshot.integration_intents.values()
        if intent.allocation_id == record.allocation_id
    ]
    if len(matches) > 1:
        raise ControllerError("INVALID_METADATA", "multiple intents bind one integration candidate")
    return matches[0].equivalent_replays if matches else ()


def publication_ref(repo: Path, policy: WorktreePolicy) -> str | None:
    """Only the policy's formal remote-tracking ref can attest publication."""
    if not policy.present or policy.policy_schema_version < 2:
        return None
    candidate = f"refs/remotes/{policy.publication_remote}/{policy.publication_branch}"
    try:
        resolve_commit(repo, candidate)
    except ControllerError:
        return None
    return candidate


def attribution_base(repo: Path, base_commit: str, head: str, formal_ref: str | None) -> str:
    policy = load_policy(inventory(repo).canonical_path)
    if formal_ref is None or formal_ref != publication_ref(repo, policy):
        return base_commit
    return trusted_publication_base(repo, base_commit, head, formal_ref)


def path_scope_issues(
    repo: Path, record: MetadataRecord, head: str, *, publication_ref: str | None = None
) -> tuple[dict[str, str], ...]:
    if not record.base_commit or not record.scope_paths:
        return ()
    base = (
        attribution_base(repo, record.base_commit, head, publication_ref)
        if publication_ref
        else record.base_commit
    )
    outside = tuple(
        path
        for path in changed_paths(repo, base, head)
        if not path_in_scopes(path, record.scope_paths)
    )
    if not outside:
        return ()
    return (
        {
            "code": "PATH_SCOPE_VIOLATION",
            "message": "candidate changes paths outside the recorded task scope",
            "allocation_id": record.allocation_id,
            "path": outside[0],
        },
    )


def evidence_head_issues(
    item: WorktreeGitState, record: MetadataRecord
) -> tuple[dict[str, str], ...]:
    issues: list[dict[str, str]] = []
    check_task = record.role == "integration" or record.lifecycle in EVIDENCE_LIFECYCLES
    if check_task:
        if record.task_commit is None:
            issues.append(
                {
                    "code": "MISSING_TASK_COMMIT",
                    "message": "task evidence is not bound to a commit",
                    "allocation_id": record.allocation_id,
                }
            )
        elif record.task_commit != item.head:
            issues.append(
                {
                    "code": "TASK_HEAD_MISMATCH",
                    "message": "recorded task commit does not equal worktree HEAD",
                    "allocation_id": record.allocation_id,
                    "expected": record.task_commit,
                    "observed": item.head,
                }
            )
    if record.handoff_commit is not None and record.handoff_commit != item.head:
        issues.append(
            {
                "code": "HANDOFF_HEAD_MISMATCH",
                "message": "recorded handoff commit does not equal worktree HEAD",
                "allocation_id": record.allocation_id,
                "expected": record.handoff_commit,
                "observed": item.head,
            }
        )
    if record.finalizer_record_id and record.finalizer_commit is None:
        issues.append(
            {
                "code": "MISSING_FINALIZER_COMMIT",
                "message": "Finalizer record is not bound to a commit",
                "allocation_id": record.allocation_id,
            }
        )
    elif record.finalizer_commit is not None and record.finalizer_commit != item.head:
        issues.append(
            {
                "code": "FINALIZER_HEAD_MISMATCH",
                "message": "recorded Finalizer commit does not equal worktree HEAD",
                "allocation_id": record.allocation_id,
                "expected": record.finalizer_commit,
                "observed": item.head,
            }
        )
    return tuple(issues)


def integration_candidate_issues(
    item: WorktreeGitState,
    record: MetadataRecord,
    snapshot: MetadataSnapshot,
    *,
    require_task_match: bool = True,
    base_commit: str | None = None,
) -> tuple[dict[str, str], ...]:
    issues: list[dict[str, str]] = []
    if require_task_match:
        issues.extend(evidence_head_issues(item, record))
    base_value = base_commit or record.base_commit
    if base_value is None:
        issues.append(
            {
                "code": "MISSING_INTEGRATION_BASE",
                "message": "integration candidate has no recorded base commit",
                "allocation_id": record.allocation_id,
            }
        )
        return tuple(issues)
    try:
        base = resolve_commit(item.path, base_value)
        if not is_ancestor(item.path, base, item.head):
            issues.append(
                {
                    "code": "INTEGRATION_BASE_DIVERGED",
                    "message": "integration candidate no longer contains its recorded base",
                    "allocation_id": record.allocation_id,
                }
            )
            return tuple(issues)
        source_heads, derived_scopes = integration_intent_evidence(snapshot, record)
        replay_evidence = integration_replay_evidence(snapshot, record)
    except ControllerError as exc:
        issues.append(
            {
                "code": exc.code,
                "message": str(exc),
                "allocation_id": record.allocation_id,
            }
        )
        return tuple(issues)
    if tuple(record.scope_paths) != derived_scopes:
        issues.append(
            {
                "code": "INTEGRATION_SCOPE_MISMATCH",
                "message": "candidate scope does not equal the union of source handoff scopes",
                "allocation_id": record.allocation_id,
            }
        )
    source_allocations = tuple(record.source_allocations)
    replay_by_source = {replay["source_allocation_id"]: replay for replay in replay_evidence}
    lineage_heads = list(source_heads)
    for source_allocation, source_head in zip(source_allocations, source_heads, strict=True):
        if is_ancestor(item.path, source_head, item.head):
            continue
        replay = replay_by_source.get(source_allocation)
        if replay is None:
            issues.append(
                {
                    "code": "SOURCE_NOT_ABSORBED",
                    "message": "candidate lacks source ancestry or equivalent replay evidence",
                    "allocation_id": record.allocation_id,
                    "source_allocation_id": source_allocation,
                }
            )
            continue
        replay_oid = resolve_commit(item.path, replay["replay_oid"])
        if not is_ancestor(item.path, replay_oid, item.head):
            issues.append(
                {
                    "code": "EQUIVALENT_REPLAY_DRIFT",
                    "message": "equivalent replay commit is not contained by the candidate",
                    "allocation_id": record.allocation_id,
                    "source_allocation_id": source_allocation,
                }
            )
            continue
        lineage_heads.append(replay_oid)
    novel_commits: list[str] = []
    for topology in commits_with_parents(item.path, base, item.head):
        commit = topology[0]
        if len(topology) >= 3:
            continue
        if not any(is_ancestor(item.path, commit, lineage_head) for lineage_head in lineage_heads):
            novel_commits.append(commit)
    if novel_commits:
        issues.append(
            {
                "code": "COMMIT_SCOPE_VIOLATION",
                "message": "candidate contains a non-merge commit outside source handoff lineage",
                "allocation_id": record.allocation_id,
                "commit": novel_commits[0],
            }
        )
    issues.extend(path_scope_issues(item.path, replace(record, base_commit=base), item.head))
    return tuple(issues)
