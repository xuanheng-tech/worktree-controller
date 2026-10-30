"""Plan, authorize and audit branch retirement without mutating Git refs.

The Controller owns the lifecycle decision and the execution grant.  A consumer
owns the ref mutation and remains the only component that touches a ref; the
Controller re-observes the world instead of believing the consumer's account.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from . import cleanup_evidence as e
from . import retirement_adapters as adapters
from . import retirement_execution as execution
from .git_backend import (
    RepositoryInventory,
    _git,
    _text,
    inventory,
    is_ancestor,
    public_refs,
    resolve_commit,
)
from .models import (
    ControllerError,
    MetadataRecord,
    MetadataSnapshot,
    ReleaseIntent,
    ReleaseReceipt,
    WriterHolder,
)
from .policy import load_policy
from .resource_admission import canonical_holder_digest
from .semantics import integration_intent_abandoned
from .store import MetadataStore, now_iso
from .writer_lease import describe_holder, holder_is_live, resolve_holder


def _crash_point(name: str) -> None:
    """The shared fault-injection hook, so a retirement crash is reproducible."""

    from .lifecycle import _crash_point as hook

    hook(name)

PLAN_SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 1
AUTHORIZATION_TTL_DEFAULT_SECONDS = 900
AUTHORIZATION_TTL_MAX_SECONDS = 3600
AUTHORIZED_RECEIPT_SCHEMA_VERSION = RECEIPT_SCHEMA_VERSION + 1
RECEIPT_PROVENANCE_AUTHORIZED = "AUTHORIZED_EXECUTION"
RECEIPT_PROVENANCE_OBSERVED = "CONTROLLER_OBSERVED"
RECEIPT_PROVENANCE_CLAIM = "CONSUMER_CLAIM"
OPERATIONS = frozenset({"local", "remote"})
REMOTE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
PROTECTED_BRANCHES = frozenset({"main", "master", "trunk", "production"})
SPECIAL_PREFIXES = ("rescue/", "archive/", "recovery/")
TERMINAL_INTEGRATION_STATES = frozenset({"RELEASED"})
TERMINAL_RELEASE_STATES = frozenset({"RELEASED", "CANCELLED"})

# Only authority-bearing Controller state participates.  Branch retirement plans and
# receipts are deliberately excluded so local and remote completion can be recorded
# independently without invalidating the other half of the same plan.
AUTHORITY_DIRECTORIES = (
    "records",
    "bindings",
    "integration-intents",
    "release-intents",
    "release-plans",
    "release-receipts",
    "publication-receipts",
    "reconciliation-receipts",
    "writer-leases",
    "transactions",
)
AUTHORITY_FILES = ("publication-lease.json",)


def _sha256_json(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _plan_identity(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Return the immutable/OID-bound identity of a retirement decision.

    Local and remote refs are expected to move from PRESENT_EXPECTED to ABSENT as
    each operation completes, so observed states cannot be part of the plan id.
    """

    fields = (
        "schema_version",
        "repository_id",
        "allocation_id",
        "release_receipt_id",
        "branch",
        "expected_oid",
        "remote",
        "remote_ref",
        "remote_tracking_ref",
        "integrated_into",
        "integrated_ref",
        "expected_integrated_oid",
        "classification",
        "registration",
        "controller_state_digest",
    )
    identity = {field: plan.get(field) for field in fields}
    if "canonical_transition_evidence" in plan:
        identity["canonical_transition_evidence"] = plan["canonical_transition_evidence"]
    if "aborted_ancestor_evidence" in plan:
        identity["aborted_ancestor_evidence"] = plan["aborted_ancestor_evidence"]
    return identity


def _authority_relevant(
    directory: str, value: Mapping[str, Any], *, allocation_id: str, branch: str
) -> bool:
    if value.get("allocation_id") == allocation_id:
        return True
    if directory == "bindings":
        return value.get("allocation_id") == allocation_id
    if directory == "integration-intents":
        return (
            allocation_id in value.get("source_allocations", [])
            or branch in value.get("source_branches", [])
        )
    if directory == "release-intents":
        return value.get("integration_allocation_id") == allocation_id
    if directory in {"release-plans", "release-receipts"}:
        return value.get("branch") == branch
    if directory == "publication-receipts":
        return any(
            isinstance(item, dict) and item.get("source_allocation_id") == allocation_id
            for item in value.get("source_results", [])
        )
    return False


def _state_digest(
    store: MetadataStore, canonical_path: Path, *, allocation_id: str, branch: str
) -> str:
    digest = hashlib.sha256()
    for directory_name in AUTHORITY_DIRECTORIES:
        directory = store.root / directory_name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.json")):
            value = store._read_json(path)
            if not _authority_relevant(
                directory_name, value, allocation_id=allocation_id, branch=branch
            ):
                continue
            relative = path.relative_to(store.root).as_posix().encode("utf-8")
            digest.update(relative)
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    for file_name in AUTHORITY_FILES:
        path = store.root / file_name
        if path.is_file():
            value = store._read_json(path)
            if value.get("allocation_id") != allocation_id:
                continue
            digest.update(file_name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    policy = canonical_path / ".agents" / "worktree-policy.toml"
    digest.update(b"policy\0")
    if policy.is_file():
        digest.update(policy.read_bytes())
    else:
        digest.update(b"<default>")
    return digest.hexdigest()


def _normalize_branch(repo: Path, branch: str) -> str:
    value = branch.removeprefix("refs/heads/")
    if not value or value.startswith("-") or "\x00" in value:
        raise ControllerError("INVALID_BRANCH", "branch name is empty or unsafe")
    result = _git(repo, "check-ref-format", "--branch", value, check=False)
    if result.returncode != 0:
        raise ControllerError("INVALID_BRANCH", f"invalid branch name: {branch}")
    return value


def _normalize_integration_ref(remote: str, value: str) -> str:
    if value.startswith("refs/"):
        if not value.startswith(("refs/heads/", "refs/remotes/")):
            raise ControllerError(
                "INVALID_INTEGRATION_REF", "integrated_into must name a branch ref"
            )
        return value
    prefix = f"{remote}/"
    branch = value.removeprefix(prefix) if value.startswith(prefix) else value
    if not branch or branch.startswith("-"):
        raise ControllerError("INVALID_INTEGRATION_REF", "integrated_into is unsafe")
    return f"refs/remotes/{remote}/{branch}"


def _ref_state(refs: Mapping[str, str], ref: str, expected_oid: str) -> str:
    observed = refs.get(ref)
    if observed is None:
        return "ABSENT"
    return "PRESENT_EXPECTED" if observed == expected_oid else "PRESENT_DRIFTED"


def _is_special_lifecycle_branch(branch: str) -> bool:
    return branch in {"rescue", "archive", "recovery"} or branch.startswith(SPECIAL_PREFIXES)


def _upstream_state(repo: Path, branch: str, expected: str) -> tuple[str, str | None]:
    output = _text(
        repo,
        "for-each-ref",
        "--format=%(refname)%00%(upstream)",
        f"refs/heads/{branch}",
    )
    if not output:
        return "ABSENT_LOCAL", None
    try:
        ref_name, upstream = output.split("\0", 1)
    except ValueError as exc:
        raise ControllerError("GIT_PARSE_ERROR", "cannot parse branch upstream") from exc
    if ref_name != f"refs/heads/{branch}":
        raise ControllerError("GIT_PARSE_ERROR", "unexpected local branch result")
    if not upstream:
        return "NONE", None
    if upstream == expected:
        return "EXPECTED", upstream
    return "OTHER", upstream


def _classify_integration(repo: Path, expected_oid: str, integrated_oid: str) -> str:
    if is_ancestor(repo, expected_oid, integrated_oid):
        return "ANCESTRY"
    non_merges = _text(repo, "rev-list", "--no-merges", f"{integrated_oid}..{expected_oid}")
    merges = _text(repo, "rev-list", "--merges", f"{integrated_oid}..{expected_oid}")
    if not non_merges:
        return "MERGE_ONLY" if merges else "UNIQUE"
    cherry = _text(repo, "cherry", integrated_oid, expected_oid)
    signs = {line[:1] for line in cherry.splitlines() if line}
    if signs == {"-"}:
        return "CHERRY_EQUIVALENT"
    if signs == {"+"}:
        return "UNIQUE"
    return "MIXED"


def _transactions_for_allocation(store: MetadataStore, allocation_id: str) -> tuple[str, ...]:
    # Same pending/terminal semantics as doctor and mutation admission: a
    # journal closed by terminal evidence must not block retirement.
    from .transaction_scope import pending

    return tuple(
        str(row["transaction_id"])
        for row in pending(store, relevant=lambda row: row.get("allocation_id") == allocation_id)
    )


def _release_identity(snapshot: Any, allocation_id: str | None, branch: str | None) -> tuple[Any, str]:
    receipts = list(snapshot.release_receipts.values())
    if allocation_id is not None:
        matches = [item for item in receipts if item.allocation_id == allocation_id]
        if not matches:
            return None, "NO_RELEASE_RECEIPT"
    else:
        matches = [item for item in receipts if item.branch == branch]
        if not matches:
            return None, "HISTORICAL_UNREGISTERED"
    identities = {(item.allocation_id, item.branch, item.released_head) for item in matches}
    if len(identities) != 1:
        return None, "AMBIGUOUS_RELEASE_HISTORY"
    return max(matches, key=lambda item: (item.released_at, item.receipt_id)), "RELEASE_BACKED"


def _canonical_transition_identity(
    repository: RepositoryInventory,
    snapshot: MetadataSnapshot,
    *,
    allocation_id: str | None,
    branch: str,
) -> tuple[MetadataRecord | None, dict[str, Any] | None, str]:
    canonical = next(item for item in repository.worktrees if item.canonical)
    record = snapshot.records.get(snapshot.bindings.get(canonical.key, ""))
    if record is None or (allocation_id is not None and record.allocation_id != allocation_id):
        return None, None, "HISTORICAL_UNREGISTERED"
    events = [
        event
        for event in record.history
        if event.get("event") == "CANONICAL_TRANSITIONED" and event.get("old_branch") == branch
    ]
    if not events:
        return None, None, "HISTORICAL_UNREGISTERED"
    if len(events) != 1:
        return record, None, "CANONICAL_TRANSITION_EVIDENCE_AMBIGUOUS"
    event = events[0]
    identities = ("old_owner", "old_task_key", "old_authority_key", "old_finalizer_record_id", "owner")
    valid = (
        event.get("from") == "HANDOFF_READY"
        and event.get("to") == "ACTIVE"
        and all(isinstance(event.get(key), str) and event[key].strip() for key in identities)
        and all(
            isinstance(event.get(key), str) and re.fullmatch(r"[0-9a-f]{40}", event[key])
            for key in ("old_head", "main_before", "main_after")
        )
        and isinstance(event.get("old_scope_paths"), list)
        and all(isinstance(path, str) for path in event["old_scope_paths"])
        and isinstance(event.get("remote"), str)
        and REMOTE_PATTERN.fullmatch(event["remote"]) is not None
    )
    try:
        timestamp = datetime.fromisoformat(event.get("at", ""))
        valid = valid and timestamp.tzinfo is not None
    except (TypeError, ValueError):
        valid = False
    if not valid:
        return record, None, "CANONICAL_TRANSITION_EVIDENCE_INVALID"
    return record, {
        "event_sha256": _sha256_json(event),
        "old_head": event["old_head"],
        "main_before": event["main_before"],
        "main_after": event["main_after"],
        "remote": event["remote"],
        "old_owner": event["old_owner"],
        "old_finalizer_record_id": event["old_finalizer_record_id"],
    }, "CANONICAL_TRANSITION_BACKED"


def _integration_publication_verified(
    snapshot: MetadataSnapshot,
    record: MetadataRecord,
    release_receipt: ReleaseReceipt,
    *,
    target_ref: str,
) -> bool:
    plan = snapshot.release_plans.get(release_receipt.plan_id)
    if plan is None:
        return False
    if plan.publication_receipt_id is not None:
        publication = snapshot.publication_receipts.get(plan.publication_receipt_id)
    elif plan.trigger == "owner-release":
        # Owner release preserves the original plan. Its typed acknowledgement
        # can join the exact publication without rewriting either receipt.
        publications = [
            publication
            for reconciliation in snapshot.reconciliation_receipts.values()
            if reconciliation.repository_id == snapshot.repository_id
            and reconciliation.allocation_id == record.allocation_id
            and reconciliation.observed_record_version
            == release_receipt.released_record_version
            == record.record_version
            and reconciliation.observed_record_lifecycle == "RELEASED"
            and reconciliation.observed_receipt_status == "REMOTE_VERIFIED"
            and reconciliation.result == "RELEASE_STATE_ACKNOWLEDGED"
            and reconciliation.recovered_intent_state == "RELEASED"
            and (publication := snapshot.publication_receipts.get(reconciliation.source_receipt_id))
            is not None
            and publication.intent_id is not None
            and reconciliation.recovered_intent_id == publication.intent_id
            and publication.lease_released_at is not None
        ]
        if len(publications) != 1:
            return False
        publication = publications[0]
    else:
        return False
    return bool(
        publication is not None
        and plan.repository_id == release_receipt.repository_id == snapshot.repository_id
        and plan.allocation_id == release_receipt.allocation_id == record.allocation_id
        and plan.branch == release_receipt.branch
        and plan.target_path == release_receipt.target_path
        and plan.expected_head == release_receipt.released_head
        and plan.expected_record_version == release_receipt.expected_record_version
        and publication.repository_id == snapshot.repository_id
        and publication.allocation_id == record.allocation_id
        and publication.candidate_oid == release_receipt.released_head
        and publication.finalizer_record_id == record.finalizer_record_id
        and publication.target_ref == target_ref
        and publication.status in {"REMOTE_VERIFIED", "RELEASED"}
    )


def _aborted_ancestor_evidence(
    store: MetadataStore,
    snapshot: MetadataSnapshot,
    record: MetadataRecord,
    release_receipt: ReleaseReceipt,
    *,
    expected_oid: str,
) -> dict[str, str] | None:
    """Return pinned abort proof for an abandoned ancestor, or None.

    None keeps the publication blocker. This does not accept cherry-equivalence,
    a retryable abort, or a branch whose commits are not already in the
    publication ref.
    """

    if (
        record.role != "integration"
        or record.lifecycle != "RELEASED"
        or record.task_commit != expected_oid
        or release_receipt.allocation_id != record.allocation_id
        or release_receipt.released_head != expected_oid
    ):
        return None
    releases = [
        item for item in snapshot.release_receipts.values()
        if item.allocation_id == record.allocation_id
    ]
    if len(releases) != 1 or releases[0].receipt_id != release_receipt.receipt_id:
        return None
    if any(
        item.allocation_id == record.allocation_id
        for item in snapshot.publication_receipts.values()
    ):
        return None
    positions = [
        index for index, event in enumerate(record.history)
        if event.get("event") == "INTEGRATION_ABORTED"
    ]
    if len(positions) != 1:
        return None
    position = positions[0]
    event = record.history[position]
    owner = event.get("owner")
    bound = event.get("abort_receipt")
    if (
        not isinstance(owner, str)
        or not owner.strip()
        or event.get("from") not in {"ACTIVE", "QUARANTINED"}
        or event.get("to") != "INTEGRATION_ABORTED"
        or not isinstance(bound, dict)
    ):
        return None
    receipt_id = bound.get("receipt_id")
    digest = bound.get("sha256")
    if not isinstance(receipt_id, str) or not isinstance(digest, str):
        return None
    try:
        if str(UUID(receipt_id)) != receipt_id:
            return None
    except ValueError:
        return None
    from .integration_cleanup import resource_tail_valid

    try:
        if not resource_tail_valid(
            record, type("Item", (), {"path": Path(release_receipt.target_path)})(), position
        ):
            return None
        value, actual = e.read(store.completion_receipts_dir / f"{receipt_id}.json", digest)
    except (ControllerError, OSError, ValueError, KeyError, TypeError):
        return None
    if actual != digest:
        return None
    intent_id = value.get("intent_id")
    if (
        value.get("kind") != "integration_abort"
        or value.get("schema_version") != 1
        or value.get("receipt_id") != receipt_id
        or value.get("repository_id") != snapshot.repository_id
        or value.get("allocation_id") != record.allocation_id
        or value.get("task_key") != record.task_key
        or value.get("authority_key") != record.authority_key
        or value.get("owner") != owner
        or value.get("branch") != release_receipt.branch
        or value.get("candidate_oid") != expected_oid
        or not isinstance(value.get("failure_reason"), str)
        or not str(value.get("failure_reason")).strip()
        or not isinstance(intent_id, str)
    ):
        return None
    try:
        if str(UUID(intent_id)) != intent_id:
            return None
    except ValueError:
        return None
    intent = snapshot.integration_intents.get(intent_id)
    if (
        intent is None
        or not integration_intent_abandoned(intent)
        or intent.owner != owner
        or intent.task_key != record.task_key
        or intent.candidate_oid != expected_oid
        or intent.published_at is not None
        or any(item.intent_id == intent_id for item in snapshot.publication_receipts.values())
        or not any(row.get("event") == "PREPARATION_ABORTED" for row in intent.history)
    ):
        return None
    return {
        "contract": "aborted-ancestor-branch-retirement/v1",
        "abort_receipt_id": receipt_id,
        "abort_receipt_sha256": digest,
        "intent_id": intent_id,
        "candidate_oid": expected_oid,
    }


def _release_intent_resolved(
    snapshot: MetadataSnapshot,
    record: MetadataRecord | None,
    receipt: ReleaseReceipt,
    intent: ReleaseIntent,
) -> bool:
    if intent.status in TERMINAL_RELEASE_STATES:
        return True
    plan = snapshot.release_plans.get(receipt.plan_id)
    if record is None or plan is None:
        return False
    # An owner release need not name a pending intent. Match its completed
    # receipt to the exact frozen authority instead of rewriting old history.
    return bool(
        receipt.result == "RELEASED"
        and record.lifecycle == "RELEASED"
        and plan.lifecycle == "HANDOFF_READY"
        and record.role in {"feature", "audit"}
        and plan.repository_id
        == receipt.repository_id
        == intent.repository_id
        == snapshot.repository_id
        and plan.allocation_id
        == receipt.allocation_id
        == intent.allocation_id
        == record.allocation_id
        and plan.branch == receipt.branch
        and plan.target_path == receipt.target_path
        and plan.expected_head == receipt.released_head == intent.expected_head
        and plan.expected_record_version
        == receipt.expected_record_version
        == intent.expected_record_version
        and receipt.released_record_version == record.record_version
        and receipt.released_record_version == receipt.expected_record_version + 1
        and plan.owner == intent.owner
        and plan.authority_key == intent.authority_key == record.authority_key
        and plan.role == intent.role == record.role
        and intent.task_key == record.task_key
        and intent.task_commit == record.task_commit
        and intent.handoff_commit == record.handoff_commit == receipt.released_head
        and intent.finalizer_commit == record.finalizer_commit == receipt.released_head
        and intent.finalizer_record_id == record.finalizer_record_id
    )


def _evaluate(
    repository_path: Path,
    *,
    allocation_id: str | None,
    branch: str | None,
    remote: str,
    integrated_into: str,
    plan_id: str | None = None,
    _locked_store: MetadataStore | None = None,
) -> tuple[dict[str, Any], MetadataStore]:
    repository = inventory(repository_path)
    # A locked caller must hand over its serialized instance; a fresh store
    # would re-open (or wrongly refuse) the flock the caller already holds.
    store = _locked_store or MetadataStore(repository.common_dir)
    snapshot = store._load_unlocked()
    if snapshot.repository_id is None:
        raise ControllerError("UNMANAGED_REPOSITORY", "Controller repository metadata is missing")
    if REMOTE_PATTERN.fullmatch(remote) is None:
        raise ControllerError("INVALID_REMOTE", f"remote name is unsafe: {remote}")
    if _git(repository.canonical_path, "remote", "get-url", remote, check=False).returncode != 0:
        raise ControllerError("INVALID_REMOTE", f"remote is not configured: {remote}")

    normalized_requested_branch = (
        _normalize_branch(repository.canonical_path, branch) if branch is not None else None
    )
    release_receipt, registration = _release_identity(
        snapshot, allocation_id, normalized_requested_branch
    )
    transition_record = None
    transition_evidence = None
    if (
        registration in {"HISTORICAL_UNREGISTERED", "NO_RELEASE_RECEIPT"}
        and normalized_requested_branch is not None
    ):
        transition_record, transition_evidence, registration = _canonical_transition_identity(
            repository, snapshot, allocation_id=allocation_id, branch=normalized_requested_branch
        )
    if release_receipt is not None:
        normalized_branch = _normalize_branch(repository.canonical_path, release_receipt.branch)
        resolved_allocation = release_receipt.allocation_id
        expected_oid = release_receipt.released_head
        release_receipt_id = release_receipt.receipt_id
    elif transition_evidence is not None and transition_record is not None:
        normalized_branch = str(normalized_requested_branch)
        resolved_allocation = transition_record.allocation_id
        expected_oid = transition_evidence["old_head"]
        release_receipt_id = None
    else:
        if normalized_requested_branch is None:
            normalized_branch = "<unknown>"
            expected_oid = ""
        else:
            normalized_branch = normalized_requested_branch
            ref = f"refs/heads/{normalized_branch}"
            expected_oid = public_refs(repository.canonical_path).get(ref, "")
        resolved_allocation = allocation_id
        release_receipt_id = None

    policy = load_policy(repository.canonical_path)
    integrated_ref = _normalize_integration_ref(remote, integrated_into)
    refs = public_refs(repository.canonical_path)
    integrated_oid = refs.get(integrated_ref)
    blockers: list[str] = []
    if registration not in {"RELEASE_BACKED", "CANONICAL_TRANSITION_BACKED"}:
        blockers.append(registration)
    if transition_evidence is not None:
        if (
            remote != policy.publication_remote
            or transition_evidence["remote"] != remote
            or integrated_ref != _normalize_integration_ref(remote, policy.publication_branch)
        ):
            blockers.append("CANONICAL_TRANSITION_TARGET_MISMATCH")
        try:
            transition_head = transition_evidence["main_after"]
            if (
                integrated_oid is None
                or not is_ancestor(repository.canonical_path, transition_evidence["old_head"], transition_head)
                or not is_ancestor(repository.canonical_path, transition_evidence["main_before"], transition_head)
                or not is_ancestor(repository.canonical_path, transition_head, integrated_oid)
            ):
                blockers.append("CANONICAL_TRANSITION_PUBLICATION_MISMATCH")
        except ControllerError:
            blockers.append("CANONICAL_TRANSITION_PUBLICATION_MISMATCH")
    if normalized_branch in PROTECTED_BRANCHES or normalized_branch == policy.publication_branch:
        blockers.append("PROTECTED_BRANCH")
    if _is_special_lifecycle_branch(normalized_branch):
        blockers.append("SPECIAL_LIFECYCLE_REF")
    if integrated_oid is None:
        blockers.append("INTEGRATION_REF_MISSING")
    if not expected_oid:
        blockers.append("EXPECTED_OID_MISSING")
        classification = "HISTORICAL_UNREGISTERED"
    elif integrated_oid is None:
        resolve_commit(repository.canonical_path, expected_oid)
        classification = "UNVERIFIED"
    else:
        resolve_commit(repository.canonical_path, expected_oid)
        resolve_commit(repository.canonical_path, integrated_oid)
        classification = _classify_integration(
            repository.canonical_path, expected_oid, integrated_oid
        )
        if classification != "ANCESTRY":
            blockers.append(f"CLASSIFICATION_{classification}")

    local_ref = f"refs/heads/{normalized_branch}"
    tracking_ref = f"refs/remotes/{remote}/{normalized_branch}"
    local_state = _ref_state(refs, local_ref, expected_oid) if expected_oid else "UNKNOWN"
    tracking_state = _ref_state(refs, tracking_ref, expected_oid) if expected_oid else "UNKNOWN"
    if local_state == "PRESENT_DRIFTED":
        blockers.append("LOCAL_OID_DRIFT")
    if tracking_state == "PRESENT_DRIFTED":
        blockers.append("UPSTREAM_OID_DRIFT")
    upstream_state, upstream_ref = _upstream_state(
        repository.canonical_path, normalized_branch, tracking_ref
    )
    if upstream_state == "OTHER":
        blockers.append("UPSTREAM_TARGET_MISMATCH")

    checked_out = sorted(
        str(item.path) for item in repository.worktrees if item.branch == normalized_branch
    )
    if checked_out:
        blockers.append("BRANCH_CHECKED_OUT")

    record = snapshot.records.get(resolved_allocation or "")
    aborted_ancestor_evidence = None
    if release_receipt is not None or transition_evidence is not None:
        if record is None:
            blockers.append("ALLOCATION_RECORD_MISSING")
        else:
            if transition_evidence is not None:
                if (
                    record.role not in {"canonical", "control-plane"}
                    or record.lifecycle not in {"ACTIVE", "HANDOFF_READY"}
                ):
                    blockers.append("CANONICAL_TRANSITION_ALLOCATION_INVALID")
            else:
                if record.lifecycle != "RELEASED":
                    blockers.append("ALLOCATION_NOT_RELEASED")
                if record.role == "integration":
                    published = (
                        remote == policy.publication_remote
                        and _integration_publication_verified(
                            snapshot,
                            record,
                            release_receipt,
                            target_ref=f"refs/heads/{policy.publication_branch}",
                        )
                    )
                    if (
                        not published
                        and remote == policy.publication_remote
                        and classification == "ANCESTRY"
                        and record.lifecycle == "RELEASED"
                    ):
                        aborted_ancestor_evidence = _aborted_ancestor_evidence(
                            store,
                            snapshot,
                            record,
                            release_receipt,
                            expected_oid=expected_oid,
                        )
                    if not published and aborted_ancestor_evidence is None:
                        blockers.append("INTEGRATION_PUBLICATION_NOT_VERIFIED")
                elif record.role not in {"feature", "audit"}:
                    blockers.append("UNSUPPORTED_ALLOCATION_ROLE")
            if record.external_bindings:
                blockers.append("RUNTIME_BINDING_ACTIVE")
            if record.recovery_ref or record.lifecycle in {"QUARANTINED", "RECOVERY_REQUIRED"}:
                blockers.append("RECOVERY_STATE_EXCLUDED")
        canonical_key = next(item.key for item in repository.worktrees if item.canonical)
        if any(
            value == resolved_allocation and (transition_evidence is None or key != canonical_key)
            for key, value in snapshot.bindings.items()
        ):
            blockers.append("ALLOCATION_BOUND")
        if resolved_allocation in snapshot.writer_leases:
            blockers.append("WRITER_LEASE_ACTIVE")
        if (
            snapshot.publication_lease is not None
            and snapshot.publication_lease.allocation_id == resolved_allocation
        ):
            blockers.append("PUBLICATION_LEASE_ACTIVE")
        if any(
            intent.allocation_id == resolved_allocation
            and (release_receipt is None or not _release_intent_resolved(snapshot, record, release_receipt, intent))
            for intent in snapshot.release_intents.values()
        ):
            blockers.append("RELEASE_INTENT_UNRESOLVED")
        if any(
            (intent.allocation_id == resolved_allocation or resolved_allocation in intent.source_allocations)
            and intent.state not in TERMINAL_INTEGRATION_STATES
            and not integration_intent_abandoned(intent)
            for intent in snapshot.integration_intents.values()
        ):
            blockers.append("INTEGRATION_INTENT_UNRESOLVED")
        if _transactions_for_allocation(store, resolved_allocation):
            blockers.append("TRANSACTION_ACTIVE")

    controller_digest = _state_digest(
        store,
        repository.canonical_path,
        allocation_id=resolved_allocation or "",
        branch=normalized_branch,
    )
    eligible = not blockers and classification == "ANCESTRY"
    core: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "repository_id": snapshot.repository_id,
        "allocation_id": resolved_allocation,
        "release_receipt_id": release_receipt_id,
        "branch": normalized_branch,
        "expected_oid": expected_oid or None,
        "remote": remote,
        "remote_ref": f"refs/heads/{normalized_branch}",
        "remote_tracking_ref": tracking_ref,
        "integrated_into": integrated_into,
        "integrated_ref": integrated_ref,
        "expected_integrated_oid": integrated_oid,
        "classification": classification,
        "registration": registration,
        "local_ref_state": local_state,
        "remote_tracking_state": tracking_state,
        "upstream_state": upstream_state,
        "upstream_ref": upstream_ref,
        "checked_out_paths": checked_out,
        "controller_state_digest": controller_digest,
        "eligible": eligible,
        "blockers": sorted(set(blockers)),
    }
    if transition_evidence is not None:
        core["canonical_transition_evidence"] = transition_evidence
    if aborted_ancestor_evidence is not None:
        core["aborted_ancestor_evidence"] = aborted_ancestor_evidence
    observed_plan_id = _sha256_json(_plan_identity(core))
    if plan_id is not None and observed_plan_id != plan_id:
        core["eligible"] = False
        core["blockers"] = sorted({*core["blockers"], "PLAN_STATE_DRIFT"})
    core["plan_id"] = observed_plan_id
    return core, store


def _finalizer_arguments(
    plan: Mapping[str, Any], operation: str, *, repository_root: Path
) -> list[str]:
    from .retirement_adapters import arguments

    return arguments(plan, operation, repository_root=repository_root)


def _payload(command: str, decision: Mapping[str, Any]) -> dict[str, Any]:
    from .controller import envelope

    payload = envelope(command)
    payload["decision"] = dict(decision)
    return payload


def plan_branch_retirement(
    repository_path: Path,
    *,
    allocation_id: str | None = None,
    branch: str | None = None,
    remote: str,
    integrated_into: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    if (allocation_id is None) == (branch is None):
        raise ControllerError(
            "INVALID_RETIREMENT_TARGET", "specify exactly one of allocation_id or branch"
        )
    repository = inventory(repository_path)
    store = MetadataStore(repository.common_dir)
    with store.lock(exclusive=not dry_run, create=not dry_run):
        evaluated, _ = _evaluate(
            repository_path,
            allocation_id=allocation_id,
            branch=branch,
            remote=remote,
            integrated_into=integrated_into,
            _locked_store=store,
        )
        persisted = False
        if evaluated["eligible"] and not dry_run:
            persisted_plan = {**evaluated, "created_at": now_iso()}
            path = store.branch_retirement_plans_dir / f"{evaluated['plan_id']}.json"
            if path.exists():
                current = store._read_json(path)
                if _plan_identity(current) != _plan_identity(evaluated):
                    raise ControllerError("PLAN_MISMATCH", "retirement plan already differs")
            else:
                store._atomic_json(path, persisted_plan)
                store.touch_repository()
            persisted = True
    decision = {
        "code": (
            "BRANCH_RETIREMENT_READY"
            if evaluated["eligible"]
            else "BRANCH_RETIREMENT_REPORT_ONLY"
            if evaluated["registration"] == "HISTORICAL_UNREGISTERED"
            or str(evaluated["classification"]) in {
                "CHERRY_EQUIVALENT",
                "MERGE_ONLY",
                "UNIQUE",
                "MIXED",
                "HISTORICAL_UNREGISTERED",
            }
            else "BRANCH_RETIREMENT_BLOCKED"
        ),
        "persisted": persisted,
        "dry_run": dry_run,
        "plan": evaluated,
    }
    if evaluated["eligible"]:
        decision["execution"] = {
            "contract": execution.RECIPE_CONTRACT,
            "schema_version": execution.RECIPE_SCHEMA_VERSION,
            "grant": "branch-retirement-authorize",
            "settle": "branch-retirement-complete",
            "result_contract": execution.RESULT_CONTRACT,
            "operations": {
                operation: {
                    "ref": (
                        f"refs/heads/{evaluated['branch']}"
                        if operation == "local"
                        else str(evaluated["remote_tracking_ref"])
                    ),
                    "mutation": "expected-oid-cas-delete",
                    "expected_oid": evaluated["expected_oid"],
                    "integrated_ref": evaluated["integrated_ref"],
                    "expected_integrated_oid": evaluated["expected_integrated_oid"],
                    "tracking_ref": evaluated["remote_tracking_ref"],
                    "kind": execution.KIND_CAS_DELETE,
                }
                for operation in ("local", "remote")
            },
        }
        decision["finalizer"] = {
            "local": _finalizer_arguments(
                evaluated, "local", repository_root=repository.canonical_path
            ),
            "remote": _finalizer_arguments(
                evaluated, "remote", repository_root=repository.canonical_path
            ),
        }
    return _payload("branch-retirement-plan", decision)


def _load_plan(store: MetadataStore, plan_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", plan_id):
        raise ControllerError("INVALID_PLAN_ID", "retirement plan id must be sha256")
    path = store.branch_retirement_plans_dir / f"{plan_id}.json"
    if not path.is_file():
        raise ControllerError("UNKNOWN_RETIREMENT_PLAN", "retirement plan does not exist")
    plan = dict(store._read_json(path))
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION or plan.get("plan_id") != plan_id:
        raise ControllerError("INVALID_METADATA", "retirement plan schema is invalid")
    return plan


def _receipt_path(store: MetadataStore, plan_id: str, operation: str) -> Path:
    return store.branch_retirement_receipts_dir / f"{plan_id}-{operation}.json"


def _completed_blockers(
    repository_path: Path,
    plan: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    operation: str,
    receipt: Mapping[str, Any],
) -> list[str]:
    """Revalidate a historical completion without changing its execution plan.

    Only an integrated-ref fast-forward may differ, and only for an attested
    operation whose ref remains absent. Pending execution still uses exact CAS.
    """

    blockers = set(current["blockers"])
    plan_id = str(plan["plan_id"])
    if (
        _sha256_json(_plan_identity(plan)) != plan_id
        or type(receipt.get("schema_version")) is not int
        or receipt.get("schema_version")
        not in {RECEIPT_SCHEMA_VERSION, AUTHORIZED_RECEIPT_SCHEMA_VERSION}
        or receipt.get("receipt_id")
        != hashlib.sha256(f"{plan_id}:{operation}".encode()).hexdigest()
        or receipt.get("operation") != operation
        or any(
            receipt.get(key) != plan[key]
            for key in ("plan_id", "branch", "expected_oid", "expected_integrated_oid")
        )
    ):
        return sorted({*blockers, "RETIREMENT_RECEIPT_MISMATCH"})
    state_key = "local_ref_state" if operation == "local" else "remote_tracking_state"
    if current[state_key] != "ABSENT":
        blockers.add("REF_REAPPEARED_AFTER_RETIREMENT")
    if blockers == {"PLAN_STATE_DRIFT"}:
        original_identity = _plan_identity(current)
        original_identity["expected_integrated_oid"] = plan["expected_integrated_oid"]
        if (
            _sha256_json(original_identity) == plan_id
            and isinstance(current["expected_integrated_oid"], str)
            and is_ancestor(
                repository_path,
                str(plan["expected_integrated_oid"]),
                current["expected_integrated_oid"],
            )
        ):
            blockers.clear()
    return sorted(blockers)



def branch_retirement_status(repository_path: Path, *, plan_id: str) -> dict[str, Any]:
    repository = inventory(repository_path)
    store = MetadataStore(repository.common_dir)
    with store.lock(exclusive=False):
        plan = _load_plan(store, plan_id)
        current, _ = _evaluate(
            repository_path,
            allocation_id=str(plan["allocation_id"]),
            branch=str(plan["branch"]),
            remote=str(plan["remote"]),
            integrated_into=str(plan["integrated_into"]),
            plan_id=plan_id,
            _locked_store=store,
        )
        receipts = {
            operation: (
                dict(store._read_json(_receipt_path(store, plan_id, operation)))
                if _receipt_path(store, plan_id, operation).is_file()
                else None
            )
            for operation in ("local", "remote")
        }
        if all(receipt is not None for receipt in receipts.values()):
            blockers = sorted(
                {
                    blocker
                    for operation, receipt in receipts.items()
                    for blocker in _completed_blockers(
                        repository.canonical_path,
                        plan,
                        current,
                        operation=operation,
                        receipt=receipt,
                    )
                }
            )
            current = {**current, "blockers": blockers, "eligible": not blockers}
    completed = [operation for operation, receipt in receipts.items() if receipt is not None]
    drifted = "PLAN_STATE_DRIFT" in current["blockers"] or not current["eligible"]
    if len(completed) == 2 and not drifted:
        code = "BRANCH_RETIREMENT_COMPLETE"
    elif len(completed) == 2:
        code = "BRANCH_RETIREMENT_DRIFTED"
    elif completed and drifted:
        code = "BRANCH_RETIREMENT_PARTIAL_DRIFTED"
    elif completed:
        code = "BRANCH_RETIREMENT_PARTIAL"
    elif drifted:
        code = "BRANCH_RETIREMENT_DRIFTED"
    else:
        code = "BRANCH_RETIREMENT_READY"
    return _payload(
        "branch-retirement-status",
        {
            "code": code,
            "plan_id": plan_id,
            "completed_operations": completed,
            "pending_operations": [item for item in ("local", "remote") if item not in completed],
            "receipts": receipts,
            "current": current,
        },
    )


#: Authority facts that must still hold when a retirement result is filed.  Ref-derived
#: codes (EXPECTED_OID_MISSING, UPSTREAM_*, INTEGRATION_REF_MISSING) are what deleting a
#: branch legitimately produces, so they are not in this set.
AUTHORITY_BLOCKERS = frozenset(
    {
        "BRANCH_CHECKED_OUT",
        "WRITER_LEASE_ACTIVE",
        "ALLOCATION_BOUND",
        "RUNTIME_BINDING_ACTIVE",
        "PUBLICATION_LEASE_ACTIVE",
        "RELEASE_INTENT_UNRESOLVED",
        "INTEGRATION_INTENT_UNRESOLVED",
        "TRANSACTION_ACTIVE",
        "RECOVERY_STATE_EXCLUDED",
        "ALLOCATION_NOT_RELEASED",
        "ALLOCATION_RECORD_MISSING",
        "UNSUPPORTED_ALLOCATION_ROLE",
        "INTEGRATION_PUBLICATION_NOT_VERIFIED",
        "CANONICAL_TRANSITION_EVIDENCE_INVALID",
        "CANONICAL_TRANSITION_EVIDENCE_AMBIGUOUS",
        "CANONICAL_TRANSITION_TARGET_MISMATCH",
        "CANONICAL_TRANSITION_PUBLICATION_MISMATCH",
        "CANONICAL_TRANSITION_ALLOCATION_INVALID",
        "PROTECTED_BRANCH",
        "SPECIAL_LIFECYCLE_REF",
        "PLAN_STATE_DRIFT",
    }
)

STALE_BLOCKERS = frozenset(
    {
        "PLAN_STATE_DRIFT",
        "LOCAL_OID_DRIFT",
        "UPSTREAM_OID_DRIFT",
    }
)


def _stale_prefix(blockers: list[str]) -> bool:
    return any(item.startswith("CLASSIFICATION_") for item in blockers)


def _classify(current: Mapping[str, Any], *, operation: str, receipt: object) -> tuple[str, list[str]]:
    """One classifier for every pre-mutation question, so verbs cannot diverge.

    ``PLAN_STATE_DRIFT`` and the digest-blind ``*_DRIFT`` blockers mean the world the
    plan described is gone, which is a different answer from "something is occupying
    it": a stale plan needs a new plan, a blocked one needs the occupancy removed.
    """

    blockers = list(current["blockers"])
    state_key = "local_ref_state" if operation == "local" else "remote_tracking_state"
    ref_state = str(current[state_key])
    if receipt is not None:
        return "RETIREMENT_OPERATION_COMPLETE", blockers
    if ref_state == "ABSENT":
        return "RETIREMENT_BLOCKED", sorted({*blockers, "ABSENT_UNATTESTED"})
    if any(item in STALE_BLOCKERS for item in blockers) or _stale_prefix(blockers):
        return "RETIREMENT_STALE", blockers
    if blockers or not current["eligible"] or ref_state != "PRESENT_EXPECTED":
        return "RETIREMENT_BLOCKED", blockers
    return "RETIREMENT_EXECUTABLE", blockers


def _observation(repository: RepositoryInventory, current: Mapping[str, Any]) -> dict[str, Any]:
    """The live Git facts the state digest provably cannot see.

    ``controller_state_digest`` covers Controller authority and policy bytes; refs,
    worktree occupancy and upstream configuration are Git state, reachable by any
    plain ``git`` invocation without touching a digested file.  An authorization
    therefore binds this vector as well, and settle compares it.
    """

    refs = public_refs(repository.canonical_path)
    branch = str(current["branch"])
    watched = {f"refs/heads/{branch}": refs.get(f"refs/heads/{branch}")}
    for ref in (current.get("remote_tracking_ref"), current.get("integrated_ref")):
        if isinstance(ref, str) and ref:
            watched[ref] = refs.get(ref)
    return execution.normalize_observation(
        {
            "refs": watched,
            "checked_out_paths": list(current["checked_out_paths"]),
            "upstream_ref": current.get("upstream_ref"),
            "upstream_target": current.get("upstream_state"),
            "classification": current.get("classification"),
            "blockers": list(current["blockers"]),
        }
    )


def _authorization_path(store: MetadataStore, authorization_id: str) -> Path:
    return store.branch_retirement_authorizations_dir / f"{authorization_id}.json"


def outstanding_retirement_authorizations(
    store: MetadataStore, *, branch: str | None = None, allocation_id: str | None = None
) -> list[dict[str, Any]]:
    """Every execution authorization the Controller has granted and not yet closed.

    Callers under a repository lock only; the directory is small, so a full scan is
    cheaper and safer than a secondary index that could disagree with the files.
    """

    from .transaction_scope import is_atomic_temp

    directory = store.branch_retirement_authorizations_dir
    if not directory.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        if is_atomic_temp(path.name) or path.name.endswith("-abandoned.json"):
            continue
        try:
            value = dict(store._read_json(path))
        except ControllerError:
            continue
        if value.get("contract") != execution.AUTHORIZATION_CONTRACT:
            continue
        if branch is not None and value.get("branch") != branch:
            continue
        if allocation_id is not None and value.get("allocation_id") != allocation_id:
            continue
        result.append(value)
    return result


def retirement_occupancy_blocker(
    store: MetadataStore, *, branch: str | None = None, allocation_id: str | None = None
) -> ControllerError | None:
    """Refuse to re-occupy a branch the Controller has just authorized for deletion.

    This is the fence half of the execution contract: an authorization is a promise
    that the branch is going away, so handing it to a new checkout or writer lease
    would make the authorized mutation destroy live work instead of a retired ref.
    """

    outstanding = outstanding_retirement_authorizations(
        store, branch=branch, allocation_id=allocation_id
    )
    if not outstanding:
        return None
    scope = branch or allocation_id or "this target"
    return ControllerError(
        "RETIREMENT_EXECUTION_ACTIVE",
        f"a retirement execution authorization covers {scope!r} "
        f"({outstanding[0]['authorization_id']}); complete or abandon it first",
    )


def authorize_branch_retirement(
    repository_path: Path,
    *,
    plan_id: str,
    operation: str,
    request_id: str,
    holder: str | None = None,
    client_id: str = "retirement-executor",
    session_id: str | None = None,
    run_id: str | None = None,
    kind: str = execution.KIND_CAS_DELETE,
    ttl_seconds: int = AUTHORIZATION_TTL_DEFAULT_SECONDS,
) -> dict[str, Any]:
    """Grant one single-use execution authorization for one operation of one plan.

    Validation happens here, under the exclusive repository lock, and the grant is
    durable: the consumer's next step is the Git compare-and-delete, and the
    Controller's final check is ``branch-retirement-complete``, which re-observes
    the world itself rather than believing the consumer's account of it.  A caller
    never asserts "I hold the lock" - WC takes the lock, and the token it returns
    is the only capability the grant carries.
    """

    if operation not in OPERATIONS:
        raise ControllerError("INVALID_OPERATION", "operation must be local or remote")
    if kind != execution.KIND_CAS_DELETE:
        raise ControllerError(
            "INVALID_EXECUTION_KIND",
            f"{kind!r} is a reported outcome kind, not a grantable one; only CAS_DELETE is authorized",
        )
    if ttl_seconds <= 0 or ttl_seconds > AUTHORIZATION_TTL_MAX_SECONDS:
        raise ControllerError(
            "INVALID_AUTHORIZATION_TTL", f"ttl must be between 1 and {AUTHORIZATION_TTL_MAX_SECONDS} seconds"
        )
    try:
        identity = UUID(request_id)
    except ValueError as exc:
        raise ControllerError("INVALID_REQUEST_ID", "request id must be a UUID") from exc
    if str(identity) != request_id:
        raise ControllerError("INVALID_REQUEST_ID", "request id must be a canonical UUID")

    repository = inventory(repository_path)
    store = MetadataStore(repository.common_dir)
    with store.lock(exclusive=True, create=False):
        plan = _load_plan(store, plan_id)
        current, _ = _evaluate(
            repository_path,
            allocation_id=str(plan["allocation_id"]),
            branch=str(plan["branch"]),
            remote=str(plan["remote"]),
            integrated_into=str(plan["integrated_into"]),
            plan_id=plan_id,
            _locked_store=store,
        )
        receipt_path = _receipt_path(store, plan_id, operation)
        receipt = dict(store._read_json(receipt_path)) if receipt_path.is_file() else None
        code, blockers = _classify(current, operation=operation, receipt=receipt)
        ref = (
            f"refs/heads/{current['branch']}"
            if operation == "local"
            else str(current["remote_tracking_ref"])
        )
        live = [
            item
            for item in outstanding_retirement_authorizations(store, branch=str(current["branch"]))
            if item.get("operation") == operation
        ]
        replayed = next((item for item in live if item.get("request_id") == request_id), None)
        if replayed is not None:
            return _payload(
                "branch-retirement-authorize",
                {
                    "code": "RETIREMENT_AUTHORIZED",
                    "idempotent": True,
                    "plan_id": plan_id,
                    "operation": operation,
                    "authorization": replayed,
                    "blockers": [],
                },
            )
        if code != "RETIREMENT_EXECUTABLE":
            return _payload(
                "branch-retirement-authorize",
                {
                    "code": code,
                    "idempotent": False,
                    "plan_id": plan_id,
                    "operation": operation,
                    "authorization": None,
                    "blockers": blockers,
                },
            )
        if live:
            return _payload(
                "branch-retirement-authorize",
                {
                    "code": "RETIREMENT_BLOCKED",
                    "idempotent": False,
                    "plan_id": plan_id,
                    "operation": operation,
                    "authorization": None,
                    "blockers": sorted({*blockers, "RETIREMENT_AUTHORIZATION_ACTIVE"}),
                    "outstanding_authorization_id": live[0]["authorization_id"],
                },
            )

        selector = holder or "self"
        if selector != "self" and not selector.startswith("ancestor:"):
            raise ControllerError(
                "INVALID_HOLDER",
                "a grant binds the calling process tree: use self or ancestor:<program>, never a "
                "detached pid that names somebody else's process",
            )
        resolved = (
            describe_holder(os.getpid(), client_id=client_id, session_id=session_id, run_id=run_id)
            if selector == "self"
            else resolve_holder(
                selector, client_id=client_id, session_id=session_id, run_id=run_id
            )
        )
        issued_at = now_iso()
        expires_at = (
            datetime.fromisoformat(issued_at)
            .fromtimestamp(
                datetime.fromisoformat(issued_at).timestamp() + ttl_seconds,
                tz=datetime.fromisoformat(issued_at).tzinfo,
            )
            .isoformat(timespec="seconds")
        )
        body: dict[str, Any] = {
            "schema_version": execution.AUTHORIZATION_SCHEMA_VERSION,
            "contract": execution.AUTHORIZATION_CONTRACT,
            "request_id": request_id,
            "plan_id": plan_id,
            "operation": operation,
            "kind": kind,
            "repository_id": current["repository_id"],
            "allocation_id": current["allocation_id"],
            "branch": current["branch"],
            "ref": ref,
            "expected_oid": current["expected_oid"],
            "integrated_ref": current["integrated_ref"],
            "expected_integrated_oid": current["expected_integrated_oid"],
            "tracking_ref": current["remote_tracking_ref"],
            "observed": _observation(repository, current),
            "controller_state_digest": current["controller_state_digest"],
            "holder_sha256": canonical_holder_digest(asdict(resolved)),
            "issued_at": issued_at,
            "expires_at": expires_at,
            "ttl_seconds": ttl_seconds,
            "holder": asdict(resolved),
            "token": secrets.token_hex(32),
            "minimum_revalidation": {
                "plan_id": plan_id,
                "operation": operation,
                "branch": current["branch"],
                "expected_oid": current["expected_oid"],
                "expected_integrated_oid": current["expected_integrated_oid"],
                "integrated_ref": current["integrated_ref"],
                "ref_state": current["local_ref_state" if operation == "local" else "remote_tracking_state"],
                "controller_state_digest": current["controller_state_digest"],
            },
        }
        body["authorization_id"] = _sha256_json(execution.identity(body))
        store._atomic_json(_authorization_path(store, body["authorization_id"]), body)
        _crash_point("retirement_authorization_issued")
        store.touch_repository()
        return _payload(
            "branch-retirement-authorize",
            {
                "code": "RETIREMENT_AUTHORIZED",
                "idempotent": False,
                "plan_id": plan_id,
                "operation": operation,
                "authorization_id": body["authorization_id"],
                "authorization": body,
                "blockers": [],
            },
        )


def _load_authorization(store: MetadataStore, authorization_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", authorization_id):
        raise ControllerError("INVALID_AUTHORIZATION_ID", "authorization id must be sha256")
    path = _authorization_path(store, authorization_id)
    if not path.is_file():
        raise ControllerError("UNKNOWN_RETIREMENT_AUTHORIZATION", "authorization does not exist")
    document = dict(store._read_json(path))
    if (
        document.get("schema_version") != execution.AUTHORIZATION_SCHEMA_VERSION
        or document.get("contract") != execution.AUTHORIZATION_CONTRACT
        or _sha256_json(execution.identity(document)) != document.get("authorization_id")
        or document.get("authorization_id") != authorization_id
    ):
        raise ControllerError("INVALID_METADATA", "retirement authorization content differs")
    return document


def complete_branch_retirement(
    repository_path: Path,
    *,
    authorization_id: str,
    token: str,
    result_path: Path,
) -> dict[str, Any]:
    """Close an authorized retirement using the Controller's own observation.

    The consumer's result document is treated as a claim about what it attempted,
    never as proof of what happened.  Under the exclusive lock the Controller
    re-reads the refs, re-derives the authority digest, and compares the live
    observation against the vector the grant bound; only the single authorized
    delta (the target ref moving from its expected OID to absent) is accepted.
    """

    try:
        raw = result_path.read_bytes()
        reported = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ControllerError("INVALID_EXECUTION_RESULT", f"cannot read result: {exc}") from exc
    if not isinstance(reported, dict):
        raise ControllerError("INVALID_EXECUTION_RESULT", "result must be a JSON object")
    result = execution.normalize_result(reported)

    repository = inventory(repository_path)
    store = MetadataStore(repository.common_dir)
    with store.lock(exclusive=True, create=False):
        settled = _receipt_by_authorization(store, authorization_id)
        if settled is not None:
            receipt, plan_id, operation = settled
            if _authorization_path(store, authorization_id).is_file():
                # Crash between the receipt write and the grant close: the decision
                # is already recorded, so finishing the close needs no new one.
                _close_authorization(store, authorization_id)
            return _payload(
                "branch-retirement-complete",
                {
                    "code": "RETIREMENT_EXECUTION_COMPLETE",
                    "idempotent": True,
                    "authorization_id": authorization_id,
                    "plan_id": plan_id,
                    "operation": operation,
                    "receipt": receipt,
                    "provenance": receipt.get("provenance", RECEIPT_PROVENANCE_CLAIM),
                },
            )
        grant = _load_authorization(store, authorization_id)
        if not hmac.compare_digest(str(grant["token"]), str(token)):
            raise ControllerError("AUTHORIZATION_TOKEN_MISMATCH", "execution token differs")
        for field in ("plan_id", "operation", "kind", "ref", "expected_oid", "expected_integrated_oid"):
            if result[field] != grant[field]:
                raise ControllerError(
                    "EXECUTION_RESULT_MISMATCH", f"result {field} differs from the authorization"
                )
        if result["integrated_ref"] is not None and result["integrated_ref"] != grant["integrated_ref"]:
            raise ControllerError("EXECUTION_RESULT_MISMATCH", "result integrated_ref differs")
        plan = _load_plan(store, str(grant["plan_id"]))
        authorization_id_value = str(grant["authorization_id"])
        receipt_path = _receipt_path(store, str(grant["plan_id"]), str(grant["operation"]))
        if receipt_path.is_file():
            # A receipt without a closed grant is a crash between the two writes;
            # finishing the close is the only safe repair and needs no new decision.
            _close_authorization(store, authorization_id_value)
            return _payload(
                "branch-retirement-complete",
                {
                    "code": "RETIREMENT_EXECUTION_COMPLETE",
                    "idempotent": True,
                    "authorization_id": authorization_id_value,
                    "plan_id": grant["plan_id"],
                    "operation": grant["operation"],
                    "receipt": dict(store._read_json(receipt_path)),
                    "provenance": RECEIPT_PROVENANCE_AUTHORIZED,
                    "grant_expired_at_settle": False,
                },
            )

        refs = public_refs(repository.canonical_path)
        target_before = refs.get(str(grant["ref"]))
        succeeded = execution.succeeded(result)
        if succeeded:
            if target_before is not None:
                raise ControllerError(
                    "EXECUTION_OBSERVATION_MISMATCH",
                    f"the authorized ref still exists at {target_before}",
                )
        else:
            abandon = _abandon(
                store,
                grant,
                classification="EXECUTION_REPORTED_FAILURE",
                reason=str(result["outcome"]),
                repository=repository,
            )
            return _payload(
                "branch-retirement-complete",
                {
                    "code": "RETIREMENT_EXECUTION_ABORTED",
                    "idempotent": False,
                    "authorization_id": authorization_id_value,
                    "plan_id": grant["plan_id"],
                    "operation": grant["operation"],
                    "evidence": abandon,
                    "blockers": [],
                },
            )

        current, _ = _evaluate(
            repository_path,
            allocation_id=str(plan["allocation_id"]),
            branch=str(plan["branch"]),
            remote=str(plan["remote"]),
            integrated_into=str(plan["integrated_into"]),
            plan_id=None,
            _locked_store=store,
        )
        live_observation = _observation(repository, current)
        blockers = _observation_delta(grant["observed"], live_observation, authorized_ref=str(grant["ref"]))
        if str(current["controller_state_digest"]) != str(grant["controller_state_digest"]):
            blockers = sorted({*blockers, "PLAN_STATE_DRIFT"})
        if blockers:
            abandon = _abandon(
                store,
                grant,
                classification="EXECUTION_OBSERVATION_MISMATCH",
                reason=",".join(blockers),
                repository=repository,
            )
            return _payload(
                "branch-retirement-complete",
                {
                    "code": "RETIREMENT_EXECUTION_REFUSED",
                    "idempotent": False,
                    "authorization_id": authorization_id_value,
                    "plan_id": grant["plan_id"],
                    "operation": grant["operation"],
                    "blockers": blockers,
                    "observed": live_observation,
                    "evidence": abandon,
                },
            )

        receipt = {
            "schema_version": RECEIPT_SCHEMA_VERSION + 1,
            "receipt_id": hashlib.sha256(
                f"{grant['plan_id']}:{grant['operation']}".encode()
            ).hexdigest(),
            "plan_id": grant["plan_id"],
            "operation": grant["operation"],
            "branch": grant["branch"],
            "expected_oid": grant["expected_oid"],
            "expected_integrated_oid": grant["expected_integrated_oid"],
            "finalizer_version": result["executor"]["version"],
            "finalizer_result": result["outcome"],
            "summary_sha256": hashlib.sha256(raw).hexdigest(),
            "recorded_at": now_iso(),
            "authorization_id": authorization_id_value,
            "provenance": RECEIPT_PROVENANCE_AUTHORIZED,
            "grant_expired_at_settle": grant_expired(grant),
            "observed_ref": grant["ref"],
            "observed_at_settle": target_before,
            "executor_name": result["executor"]["name"],
        }
        store._atomic_json(receipt_path, receipt)
        _crash_point("retirement_receipt_written")
        _close_authorization(store, authorization_id_value)
        store.touch_repository()
        return _payload(
            "branch-retirement-complete",
            {
                "code": "RETIREMENT_EXECUTION_COMPLETE",
                "idempotent": False,
                "authorization_id": authorization_id_value,
                "plan_id": grant["plan_id"],
                "operation": grant["operation"],
                "receipt": receipt,
                "provenance": RECEIPT_PROVENANCE_AUTHORIZED,
                "grant_expired_at_settle": receipt["grant_expired_at_settle"],
                "blockers": [],
            },
        )


def _receipt_by_authorization(
    store: MetadataStore, authorization_id: str
) -> tuple[dict[str, Any], str, str] | None:
    """Find a settled retirement from its grant id, so replay answers after closure."""

    directory = store.branch_retirement_receipts_dir
    if not directory.is_dir():
        return None
    for receipt_file in sorted(directory.glob("*.json")):
        try:
            receipt = dict(store._read_json(receipt_file))
        except ControllerError:
            continue
        if receipt.get("authorization_id") == authorization_id:
            return receipt, str(receipt["plan_id"]), str(receipt["operation"])
    return None


def _observation_delta(
    bound: object, live: Mapping[str, Any], *, authorized_ref: str
) -> list[str]:
    """Blockers for the live Git facts the authorized deletion must not have touched.

    Only refs and occupancy are compared.  ``classification`` and ``blockers`` are
    derived from the very ref the grant authorized deleting, so they legitimately
    change the moment it goes; comparing them would refuse every honest settlement.
    An occupied path is different: it means a live checkout is dangling at a ref
    that is already gone.
    """

    if not isinstance(bound, Mapping):
        return ["OBSERVATION_UNREADABLE"]
    blockers: list[str] = []
    bound_refs = bound.get("refs") or {}
    live_refs = live.get("refs") or {}
    for ref, oid in sorted(bound_refs.items()):
        if ref == authorized_ref:
            continue
        if live_refs.get(ref) != oid:
            blockers.append("OBSERVATION_REF_MOVED")
    if live.get("checked_out_paths"):
        blockers.append("OBSERVATION_CHECKOUT_APPEARED")
    return sorted(set(blockers))


def grant_expired(grant: Mapping[str, Any], *, at: str | None = None) -> bool:
    """Expiry means the claim needs verification, never that the work is done.

    An expired grant still fences occupancy and still has to be settled or abandoned
    explicitly; the only thing that ends a retirement is the Controller's own
    observation of the authorized delta.
    """

    stamp = at or now_iso()
    try:
        return datetime.fromisoformat(str(grant["expires_at"])) < datetime.fromisoformat(stamp)
    except (KeyError, TypeError, ValueError) as exc:
        raise ControllerError(
            "INVALID_METADATA", "retirement authorization expiry is unreadable"
        ) from exc


def _close_authorization(store: MetadataStore, authorization_id: str) -> None:
    store.remove_branch_retirement_authorization(authorization_id)


def _live_refs(repository: RepositoryInventory, grant: Mapping[str, Any]) -> dict[str, str | None]:
    """Every ref the grant bound, read straight from Git at the moment of writing."""

    refs = public_refs(repository.canonical_path)
    watched = {str(grant["ref"]): refs.get(str(grant["ref"]))}
    for key in ("integrated_ref", "tracking_ref"):
        value = grant.get(key)
        if isinstance(value, str) and value:
            watched[value] = refs.get(value)
    return {key: watched[key] for key in sorted(watched)}


def _abandon(
    store: MetadataStore,
    grant: Mapping[str, Any],
    *,
    classification: str,
    reason: str,
    repository: RepositoryInventory,
) -> dict[str, str]:
    """Close one authorization with immutable evidence; never mint a receipt here.

    A ref that is gone without the Controller having observed the authorized
    transition is an attention item, not a completed retirement: absence alone does
    not say who caused it.
    """

    _crash_point("retirement_abandon_started")
    evidence = e.write(
        store.branch_retirement_authorizations_dir,
        f"{grant['authorization_id']}-abandoned.json",
        {
            "schema_version": execution.AUTHORIZATION_SCHEMA_VERSION,
            "contract": execution.AUTHORIZATION_CONTRACT,
            "authorization_id": grant["authorization_id"],
            "plan_id": grant["plan_id"],
            "operation": grant["operation"],
            "branch": grant["branch"],
            "ref": grant["ref"],
            "classification": classification,
            "reason": reason,
            "bound": grant["observed"],
            "live_refs": _live_refs(repository, grant),
            "reconciled_at": now_iso(),
        },
    )
    _crash_point("retirement_abandon_evidence_written")
    _close_authorization(store, str(grant["authorization_id"]))
    return evidence


def branch_retirement_executions(
    repository_path: Path,
    *,
    action: str,
    authorization_id: str | None = None,
    token: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Inspect or retire an outstanding execution authorization.

    ``abandon`` never mints a completion receipt.  An authorization whose ref is
    already gone without the Controller having observed the authorized transition
    becomes immutable evidence and a visible attention item, not a blessed
    retirement - absence alone does not prove who caused it.
    """

    if action not in {"list", "inspect", "abandon"}:
        raise ControllerError("INVALID_ACTION", "action must be list, inspect or abandon")
    repository = inventory(repository_path)
    store = MetadataStore(repository.common_dir)
    exclusive = action == "abandon"
    with store.lock(exclusive=exclusive, create=False):
        if action == "list":
            return _payload(
                "branch-retirement-executions",
                {
                    "code": "RETIREMENT_EXECUTIONS_LISTED",
                    "authorizations": [
                        {
                            "authorization_id": item["authorization_id"],
                            "plan_id": item["plan_id"],
                            "operation": item["operation"],
                            "branch": item["branch"],
                            "ref": item["ref"],
                            "expires_at": item["expires_at"],
                            "holder_sha256": item["holder_sha256"],
                        }
                        for item in outstanding_retirement_authorizations(store)
                    ],
                },
            )
        if authorization_id is None:
            raise ControllerError("INVALID_REQUEST", "authorization id is required")
        grant = _load_authorization(store, authorization_id)
        if action == "inspect":
            current, _ = _evaluate(
                repository_path,
                allocation_id=str(grant["allocation_id"]),
                branch=str(grant["branch"]),
                remote=_remote_from_tracking(str(grant["tracking_ref"])),
                integrated_into=str(grant["integrated_ref"]),
                _locked_store=store,
            )
            return _payload(
                "branch-retirement-executions",
                {
                    "code": "RETIREMENT_EXECUTION_INSPECTED",
                    "authorization": {
                        key: value for key, value in grant.items() if key != "token"
                    },
                    "token_presented": token is not None,
                    "holder_dead": not holder_is_live(_grant_holder(grant)),
                    "observed_now": _observation(repository, current),
                    "ref_present_now": public_refs(repository.canonical_path).get(str(grant["ref"]))
                    is not None,
                },
            )
        if token is None:
            raise ControllerError(
                "AUTHORIZATION_TOKEN_REQUIRED",
                "abandoning a grant needs the execution token: holder liveness is not authority, "
                "because flock dies with the process while the mutation may not have run at all",
            )
        if not hmac.compare_digest(str(grant["token"]), str(token)):
            raise ControllerError("AUTHORIZATION_TOKEN_MISMATCH", "execution token differs")
        present = public_refs(repository.canonical_path).get(str(grant["ref"])) is not None
        classification = "ABANDONED_NO_EFFECT" if present else "ABANDONED_UNATTESTED_REMOVAL"
        evidence = _abandon(
            store,
            grant,
            classification=classification,
            reason=reason or "operator abandon",
            repository=repository,
        )
        return _payload(
            "branch-retirement-executions",
            {
                "code": "RETIREMENT_EXECUTION_ABANDONED",
                "authorization_id": authorization_id,
                "classification": classification,
                "receipt_filed": False,
                "evidence": evidence,
            },
        )


def _remote_from_tracking(tracking_ref: str) -> str:
    parts = tracking_ref.split("/")
    return parts[2] if len(parts) > 3 and parts[0] == "refs" and parts[1] == "remotes" else "origin"


def _grant_holder(grant: Mapping[str, Any]) -> WriterHolder:
    holder = grant.get("holder")
    if not isinstance(holder, dict):
        raise ControllerError("INVALID_METADATA", "retirement authorization has no holder")
    return WriterHolder(
        client_id=str(holder["client_id"]),
        pid=int(holder["pid"]),
        boot_id=str(holder["boot_id"]),
        start_ticks=int(holder["start_ticks"]),
        session_id=holder.get("session_id"),
        run_id=holder.get("run_id"),
    )


def verify_branch_retirement(
    repository_path: Path, *, plan_id: str, operation: str
) -> dict[str, Any]:
    """The published pre-mutation verdict for one retirement operation.

    A consumer obtains its decision here instead of re-deriving eligibility,
    and binds ``minimum_revalidation`` in the compare-and-delete that follows.
    Observation happens under the shared repository lock, so verify must run
    before the consumer takes its own exclusive retirement execution lock: a
    shared request against an already-held exclusive descriptor blocks.
    ``branch-retirement-authorize`` is the versioned follow-up that turns this
    verdict into a single-use, Controller-observed execution grant.
    """

    if operation not in OPERATIONS:
        raise ControllerError("INVALID_OPERATION", "operation must be local or remote")
    repository = inventory(repository_path)
    store = MetadataStore(repository.common_dir)
    with store.lock(exclusive=False):
        plan = _load_plan(store, plan_id)
        current, _ = _evaluate(
            repository_path,
            allocation_id=str(plan["allocation_id"]),
            branch=str(plan["branch"]),
            remote=str(plan["remote"]),
            integrated_into=str(plan["integrated_into"]),
            plan_id=plan_id,
            _locked_store=store,
        )
        receipt_path = _receipt_path(store, plan_id, operation)
        receipt = dict(store._read_json(receipt_path)) if receipt_path.is_file() else None
        outstanding = [
            item
            for item in outstanding_retirement_authorizations(
                store, branch=str(current["branch"]), allocation_id=None
            )
            if item.get("operation") == operation
        ]
        if receipt is not None:
            current = {
                **current,
                "blockers": _completed_blockers(
                    repository.canonical_path, plan, current, operation=operation, receipt=receipt
                ),
            }
    blockers = list(current["blockers"])
    if outstanding:
        blockers = sorted({*blockers, "RETIREMENT_AUTHORIZATION_ACTIVE"})
    code, blockers = _classify(
        {**current, "blockers": blockers}, operation=operation, receipt=receipt
    )
    minimum_revalidation = {
        "plan_id": plan_id,
        "operation": operation,
        "branch": current["branch"],
        "expected_oid": current["expected_oid"],
        "expected_integrated_oid": current["expected_integrated_oid"],
        "integrated_ref": current["integrated_ref"],
        "ref_state": current[
            "local_ref_state" if operation == "local" else "remote_tracking_state"
        ],
        "controller_state_digest": current["controller_state_digest"],
    }
    return _payload(
        "branch-retirement-verify",
        {
            "code": code,
            "plan_id": plan_id,
            "operation": operation,
            "blockers": blockers,
            "receipt": receipt,
            "minimum_revalidation": minimum_revalidation,
        },
    )


def record_branch_retirement(
    repository_path: Path,
    *,
    plan_id: str,
    operation: str,
    summary_path: Path | None = None,
    result_path: Path | None = None,
) -> dict[str, Any]:
    """File one retirement result the Controller can actually observe.

    A result document - neutral or translated from a legacy consumer summary - is
    a claim about an attempt.  The receipt is only filed when the Controller's own
    read of Git agrees that the ref the plan named is gone.  That closes the door
    where a well-shaped file with no mutation behind it became governance history.
    A result filed while an execution authorization is outstanding is refused: an
    authorized run settles through ``branch-retirement-complete``.
    """

    if operation not in OPERATIONS:
        raise ControllerError("INVALID_OPERATION", "operation must be local or remote")
    if (summary_path is None) == (result_path is None):
        raise ControllerError(
            "INVALID_REQUEST", "present exactly one of --summary-file or --result-file"
        )
    if result_path is not None:
        try:
            raw = result_path.read_bytes()
            reported = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ControllerError("INVALID_EXECUTION_RESULT", f"cannot read result: {exc}") from exc
        if not isinstance(reported, dict):
            raise ControllerError("INVALID_EXECUTION_RESULT", "result must be a JSON object")
        result = execution.normalize_result(reported)
    else:
        assert summary_path is not None
        raw, summary = adapters.load_summary(summary_path)
        result = execution.normalize_result(adapters.from_summary(summary))

    repository = inventory(repository_path)
    store = MetadataStore(repository.common_dir)
    with store.lock(exclusive=True, create=False):
        plan = _load_plan(store, plan_id)
        if result["plan_id"] != plan_id or result["operation"] != operation:
            raise ControllerError(
                "EXECUTION_RESULT_MISMATCH", "result names a different plan or operation"
            )
        for field in ("expected_oid", "expected_integrated_oid"):
            if result[field] != plan[field]:
                raise ControllerError(
                    "EXECUTION_RESULT_MISMATCH", f"result {field} differs from the plan"
                )
        authorized_ref = (
            str(plan["remote_ref"]) if operation == "local" else str(plan["remote_tracking_ref"])
        )
        if result["ref"] != authorized_ref:
            raise ControllerError("EXECUTION_RESULT_MISMATCH", "result ref differs from the plan")
        if not execution.succeeded(result):
            raise ControllerError(
                "EXECUTION_RESULT_MISMATCH", f"result outcome {result['outcome']} is not a completion"
            )
        outstanding = [
            item
            for item in outstanding_retirement_authorizations(store, branch=str(plan["branch"]))
            if item.get("operation") == operation
        ]
        if outstanding:
            raise ControllerError(
                "RETIREMENT_EXECUTION_ACTIVE",
                "an execution authorization covers this operation; settle it with "
                f"branch-retirement-complete for {outstanding[0]['authorization_id']}",
            )
        observed = public_refs(repository.canonical_path).get(result["ref"])
        if observed is not None:
            raise ControllerError(
                "EXECUTION_OBSERVATION_MISMATCH",
                f"the Controller still observes {result['ref']} at {observed}; nothing was retired",
            )
        # Deleting a ref that a checkout still points at orphans that checkout, and
        # git gives update-ref no occupancy guard, so the authority state has to be
        # re-read here rather than assumed from the plan.
        current, _ = _evaluate(
            repository_path,
            allocation_id=str(plan["allocation_id"]),
            branch=str(plan["branch"]),
            remote=str(plan["remote"]),
            integrated_into=str(plan["integrated_into"]),
            plan_id=plan_id,
            _locked_store=store,
        )
        moved = sorted(AUTHORITY_BLOCKERS.intersection(current["blockers"]))
        if moved:
            raise ControllerError(
                "EXECUTION_GOVERNANCE_MOVED",
                f"authority changed after the plan: {', '.join(moved)}",
            )
        path = _receipt_path(store, plan_id, operation)
        if path.is_file():
            receipt = dict(store._read_json(path))
            idempotent = True
        else:
            receipt = {
                "schema_version": RECEIPT_SCHEMA_VERSION,
                "receipt_id": hashlib.sha256(f"{plan_id}:{operation}".encode()).hexdigest(),
                "plan_id": plan_id,
                "operation": operation,
                "branch": plan["branch"],
                "expected_oid": plan["expected_oid"],
                "expected_integrated_oid": plan["expected_integrated_oid"],
                "finalizer_version": result["executor"]["version"],
                "finalizer_result": result["outcome"],
                "summary_sha256": hashlib.sha256(raw).hexdigest(),
                "recorded_at": now_iso(),
                "provenance": RECEIPT_PROVENANCE_OBSERVED,
                "result_contract": result["contract"],
                "executor_name": result["executor"]["name"],
            }
            store._atomic_json(path, receipt)
            store.touch_repository()
            idempotent = False
    return _payload(
        "branch-retirement-record",
        {
            "code": "BRANCH_RETIREMENT_RESULT_RECORDED",
            "idempotent": idempotent,
            "provenance": receipt.get("provenance", RECEIPT_PROVENANCE_CLAIM),
            "receipt": receipt,
        },
    )
