"""Advisory worktree classification and reviewed release routing."""

from __future__ import annotations

import stat
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .controller import _environment_first, _repository_payload, envelope
from .git_backend import inventory
from .lifecycle import RELEASABLE_STATES, build_release_plan, governance_release_plan_id, release
from .lifecycle_observation import observe_repository_orphans, observe_worktree
from .models import (
    ControllerError,
    MetadataRecord,
    MetadataSnapshot,
    RepositoryInventory,
    WorktreeGitState,
    WorktreePolicy,
)
from .mount_diagnostics import diagnose_mounts
from .policy import load_policy
from .registration_prune import apply_prune, build_prune_plan
from .semantics import record_for as _record_for
from .store import MetadataStore


def _candidate_diagnostics(
    repository: RepositoryInventory,
    item: WorktreeGitState,
    requested_diagnostics: dict[str, Any],
    *,
    namespace_pid: int | None,
) -> dict[str, Any]:
    """Probe each registered checkout's own mounts without another Git inventory."""
    reason = "CANDIDATE_PRUNABLE" if item.prunable else "CANDIDATE_PATH_UNAVAILABLE"
    if not item.prunable:
        try:
            if stat.S_ISDIR(item.path.lstat().st_mode):
                if item.path == repository.requested_path:
                    return requested_diagnostics
                return diagnose_mounts(
                    replace(repository, requested_path=item.path), namespace_pid=namespace_pid
                )
        except OSError:
            pass
    return {
        "environment": {"code": "UNKNOWN", "facts": [reason]},
        "namespace": {"comparison": "UNKNOWN", "pid": namespace_pid},
    }


def _candidate(
    item: WorktreeGitState,
    record: MetadataRecord | None,
    snapshot: MetadataSnapshot,
    policy: WorktreePolicy,
    diagnostics: dict[str, Any],
    now_at: datetime,
) -> dict[str, Any]:
    allocation_id = record.allocation_id if record else None
    writer = snapshot.writer_leases.get(allocation_id) if allocation_id else None
    publication = snapshot.publication_lease
    if publication is not None and publication.allocation_id != allocation_id:
        publication = None
    blockers: list[str] = []
    if item.canonical:
        blockers.append("CANONICAL_PROTECTED")
    if item.prunable:
        blockers.append("PRUNABLE_REGISTRATION")
    if record is None:
        blockers.append("METADATA_MISSING")
    if item.detached:
        blockers.append("DETACHED_WORKTREE")
    if item.locked:
        blockers.append("LOCKED_WORKTREE")
    if not item.clean and not item.prunable:
        blockers.append("DIRTY_WORKTREE")
    if record is not None:
        if record.lifecycle not in RELEASABLE_STATES:
            blockers.append("LIFECYCLE_NOT_RELEASABLE")
        if record.external_bindings:
            blockers.append("EXTERNAL_BINDING")
    if writer is not None:
        blockers.append("WRITER_LEASE_PRESENT")
    if publication is not None:
        blockers.append("PUBLICATION_LEASE_PRESENT")
    observation = observe_worktree(
        item,
        record,
        snapshot,
        policy,
        now_at=now_at,
        namespace_healthy=diagnostics["environment"]["code"] == "HEALTHY",
    )
    return {
        "allocation_id": allocation_id,
        "path": str(item.path),
        "branch": item.branch,
        "head": item.head,
        "record_version": record.record_version if record else None,
        "lifecycle": record.lifecycle if record else None,
        "owner": record.owner if record else None,
        "session": observation["session"],
        "dirty": None if item.prunable else not item.clean,
        "lease": {
            "writer": {
                "lease_id": writer.lease_id,
                "owner": writer.owner,
                "holder_pid": writer.holder.pid,
                "session_id": writer.holder.session_id,
                "run_id": writer.holder.run_id,
                "renewed_at": writer.renewed_at,
                "expires_at": writer.expires_at,
            }
            if writer
            else None,
            "publication": {"lease_id": publication.lease_id} if publication else None,
        },
        "external_bindings": list(record.external_bindings) if record else [],
        "lifecycle_observation": observation,
        "stale_facts": observation["stale_facts"],
        "orphan_facts": observation["orphan_facts"],
        "filesystem_health": diagnostics["environment"],
        "namespace_health": {
            "comparison": diagnostics["namespace"]["comparison"],
            "pid": diagnostics["namespace"]["pid"],
        },
        "classification": "BLOCKED" if blockers else "REVIEW_REQUIRED",
        "candidate_reason": blockers[0] if blockers else "RELEASE_PREFLIGHT_REQUIRED",
        "blocking_facts": list(blockers),
        "blockers": blockers or ["RELEASE_PREFLIGHT_REQUIRED"],
        "gc_plan_id": None,
    }


def gc_plan(
    repository_path: Path,
    *,
    target: Path | None = None,
    owner_release: str | None = None,
    release_intent_id: str | None = None,
    namespace_pid: int | None = None,
) -> dict[str, Any]:
    """Classify registered worktrees; preflight only an explicitly selected target."""
    repository = inventory(repository_path)
    diagnostics = diagnose_mounts(repository, namespace_pid=namespace_pid)
    policy = load_policy(repository.canonical_path)
    try:
        snapshot = MetadataStore(repository.common_dir).load()
    except ControllerError as exc:
        raise _environment_first(exc, diagnostics["environment"]) from exc
    result = envelope("gc-plan")
    result["mount_diagnostics"] = diagnostics
    result["environment_diagnosis"] = diagnostics["environment"]
    result["repository"] = _repository_payload(repository, policy, snapshot)
    result["orphan_observations"] = observe_repository_orphans(repository, snapshot)
    now_at = datetime.now(UTC)
    candidates = [
        _candidate(
            item,
            _record_for(item, snapshot),
            snapshot,
            policy,
            _candidate_diagnostics(repository, item, diagnostics, namespace_pid=namespace_pid),
            now_at,
        )
        for item in repository.worktrees
    ]
    result["candidates"] = candidates
    if target is None:
        result["decision"] = {
            "code": "GC_CLASSIFIED",
            "total": len(candidates),
            "blocked": sum(row["classification"] == "BLOCKED" for row in candidates),
            "review_required": sum(
                row["classification"] == "REVIEW_REQUIRED" for row in candidates
            ),
            "reason": "advisory inventory; each release requires an exact target preflight",
        }
        return result
    selected = target.expanduser().resolve(strict=False)
    row = next((candidate for candidate in candidates if Path(candidate["path"]) == selected), None)
    if row is None:
        raise ControllerError("UNREGISTERED_WORKTREE", f"GC target is not registered: {selected}")
    result["candidate"] = row
    item = next(item for item in repository.worktrees if item.path == selected)
    if item.prunable:
        if release_intent_id is not None:
            result["decision"] = {"code": "GC_BLOCKED", "blockers": ["INVALID_RELEASE_INTENT"]}
            return result
        record = _record_for(item, snapshot)
        if record is None:
            result["decision"] = {"code": "GC_BLOCKED", "blockers": ["METADATA_MISSING"]}
            return result
        try:
            prune_plan = build_prune_plan(repository, snapshot, policy, item, record, owner_release)
        except ControllerError as exc:
            row["blockers"] = [exc.code]
            row["blocking_facts"] = list(row["blockers"])
            row["candidate_reason"] = exc.code
            result["decision"] = {
                "code": "GC_BLOCKED",
                "blockers": row["blockers"],
                "reason": str(exc),
            }
            return result
        row["classification"] = "PREFLIGHT_READY"
        row["blockers"] = []
        row["blocking_facts"] = []
        row["candidate_reason"] = "MISSING_NO_CHANGE_AUDIT"
        row["gc_plan_id"] = prune_plan["gc_plan_id"]
        result["registration_prune_plan"] = prune_plan
        result["decision"] = {
            "code": "GC_PREFLIGHT_READY",
            "gc_plan_id": prune_plan["gc_plan_id"],
            "allocation_id": record.allocation_id,
            "reason": "missing audit registration has owner and retained-ref evidence",
        }
        return result
    if row["classification"] == "BLOCKED":
        result["decision"] = {"code": "GC_BLOCKED", "blockers": row["blockers"]}
        return result
    if owner_release is None and release_intent_id is None:
        row["blockers"] = ["OWNER_RELEASE_OR_INTENT_REQUIRED"]
        row["blocking_facts"] = list(row["blockers"])
        row["candidate_reason"] = row["blockers"][0]
        result["decision"] = {"code": "GC_REVIEW_REQUIRED", "blockers": row["blockers"]}
        return result
    try:
        release_plan = build_release_plan(
            repository_path,
            target=selected,
            expected_record_version=row["record_version"],
            owner_release=owner_release,
            release_intent_id=release_intent_id,
        )
    except ControllerError as exc:
        row["classification"] = "BLOCKED"
        row["blockers"] = [exc.code]
        row["blocking_facts"] = list(row["blockers"])
        row["candidate_reason"] = exc.code
        result["decision"] = {"code": "GC_BLOCKED", "blockers": row["blockers"], "reason": str(exc)}
        return result
    row["classification"] = "PREFLIGHT_READY"
    row["blockers"] = []
    row["blocking_facts"] = []
    row["candidate_reason"] = "EXISTING_RELEASE_PREFLIGHT_PASSED"
    row["gc_plan_id"] = governance_release_plan_id(release_plan)
    result["release_plan"] = asdict(release_plan)
    result["decision"] = {
        "code": "GC_PREFLIGHT_READY",
        "gc_plan_id": row["gc_plan_id"],
        "allocation_id": row["allocation_id"],
        "reason": "existing release preflight passed without mutation",
    }
    return result


def _replay_reviewed_release(
    repository_path: Path,
    *,
    target: Path,
    expected_gc_plan_id: str,
    owner_release: str | None,
    release_intent_id: str | None,
) -> dict[str, Any] | None:
    """The existing receipt when the exact reviewed plan already completed elsewhere.

    A background pass or an interrupted earlier apply may have executed the very plan
    that was reviewed. Only a completed receipt whose ReleasePlan hashes to the reviewed
    GC identity qualifies; release() then re-verifies that evidence (released record,
    retained branch, teardown receipt) before answering ALREADY_RELEASED. A release made
    from any other plan stays GC_PLAN_DRIFT, which names the receipt that did happen.
    """

    repository = inventory(repository_path)
    snapshot = MetadataStore(repository.common_dir).load()
    selected = target.expanduser().resolve(strict=False)
    completed = [
        (receipt, snapshot.release_plans.get(receipt.plan_id))
        for receipt in snapshot.release_receipts.values()
        if receipt.result == "RELEASED" and Path(receipt.target_path) == selected
    ]
    matches = [
        (receipt, plan)
        for receipt, plan in completed
        if plan is not None and governance_release_plan_id(plan) == expected_gc_plan_id
    ]
    if len(matches) != 1:
        if completed:
            raise ControllerError(
                "GC_PLAN_DRIFT",
                "GC target was released by a different plan: "
                + ", ".join(sorted(receipt.receipt_id for receipt, _ in completed)),
            )
        return None
    _receipt, plan = matches[0]
    payload = release(
        repository_path,
        target=selected,
        expected_record_version=plan.expected_record_version,
        owner_release=owner_release,
        release_intent_id=release_intent_id,
    )
    payload["command"] = "gc-apply"
    payload["decision"]["gc_plan_id"] = expected_gc_plan_id
    return payload


def gc_apply(
    repository_path: Path,
    *,
    target: Path,
    expected_gc_plan_id: str,
    owner_release: str | None = None,
    release_intent_id: str | None = None,
) -> dict[str, Any]:
    """Revalidate review identity and delegate the only removal to release()."""
    pruned = apply_prune(
        repository_path,
        target=target,
        expected_gc_plan_id=expected_gc_plan_id,
        owner_release=owner_release,
    )
    if pruned is not None:
        return pruned
    try:
        planned = gc_plan(
            repository_path,
            target=target,
            owner_release=owner_release,
            release_intent_id=release_intent_id,
        )
    except ControllerError as exc:
        if exc.code in {"INVALID_TARGET", "UNREGISTERED_WORKTREE"}:
            replayed = _replay_reviewed_release(
                repository_path,
                target=target,
                expected_gc_plan_id=expected_gc_plan_id,
                owner_release=owner_release,
                release_intent_id=release_intent_id,
            )
            if replayed is not None:
                return replayed
            raise ControllerError("GC_PLAN_DRIFT", "GC target changed since review") from exc
        raise
    if planned["decision"]["code"] != "GC_PREFLIGHT_READY":
        raise ControllerError("GC_PLAN_DRIFT", "GC target no longer passes release preflight")
    observed = planned["decision"]["gc_plan_id"]
    if observed != expected_gc_plan_id:
        raise ControllerError("GC_PLAN_DRIFT", "GC plan identity changed since review")
    receipt = release(
        repository_path,
        target=target,
        expected_record_version=planned["candidate"]["record_version"],
        owner_release=owner_release,
        release_intent_id=release_intent_id,
        expected_governance_plan_id=expected_gc_plan_id,
    )
    receipt["command"] = "gc-apply"
    receipt["decision"]["gc_plan_id"] = expected_gc_plan_id
    return receipt
