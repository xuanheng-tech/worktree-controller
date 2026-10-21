"""Pure validation and serialization of versioned Controller metadata."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .models import (
    INTEGRATION_EVENT_TYPES,
    INTEGRATION_INTENT_STATES,
    LIFECYCLE_STATES,
    METADATA_SCHEMA_VERSION,
    RELEASE_INTENT_STATES,
    SUPPORTED_METADATA_SCHEMA_VERSIONS,
    WORKTREE_ROLES,
    ControllerError,
    IntegrationIntent,
    IntegrationReconciliationReceipt,
    LifecycleEvent,
    MetadataRecord,
    PreparedCandidateReceipt,
    PublicationLease,
    PublicationReceipt,
    ReconciliationReceipt,
    ReleaseIntent,
    ReleasePlan,
    ReleaseReceipt,
    WriterHolder,
    WriterLease,
    WriterReconciliationReceipt,
)

TEARDOWN_RECEIPT_SCHEMA_VERSION = 1
TEARDOWN_RECEIPT_RESULTS = frozenset({"prepared", "removal_started", "removed", "released"})
INTEGRATION_INTENT_SCHEMA_VERSION = 3
INTEGRATION_INTENT_CONTRACT_VERSION = 2
PREPARED_CANDIDATE_RECEIPT_SCHEMA_VERSION = 1
LIFECYCLE_EVENT_SCHEMA_VERSION = 1
INTEGRATION_RECONCILIATION_RECEIPT_SCHEMA_VERSION = 1
PUBLICATION_LEASE_SCHEMA_VERSION = 2
PUBLICATION_RECEIPT_SCHEMA_VERSION = 2
PUBLICATION_RECEIPT_STATES = frozenset({"REMOTE_VERIFIED", "RELEASED"})
RELEASE_INTENT_SCHEMA_VERSION = 1
RELEASE_PLAN_SCHEMA_VERSION = 1
RELEASE_RECEIPT_SCHEMA_VERSION = 1
RECONCILIATION_RECEIPT_SCHEMA_VERSION = 1
_LOWERCASE_HEX = re.compile(r"[0-9a-f]+")
_CANONICAL_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not (text := value.strip()):
        raise ControllerError("INVALID_METADATA", f"{field} must be a non-empty string")
    if "\x00" in value:
        raise ControllerError("INVALID_METADATA", f"{field} contains a NUL byte")
    return text


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _nonempty_string(value, field)


def _optional_oid(value: Any, field: str) -> str | None:
    text = _optional_string(value, field)
    if text is not None and (
        len(text) != 40 or _LOWERCASE_HEX.fullmatch(text) is None
    ):
        raise ControllerError("INVALID_METADATA", f"{field} must be a full SHA-1 commit OID")
    return text


def _required_oid(value: Any, field: str) -> str:
    oid = _optional_oid(value, field)
    if oid is None:
        raise ControllerError("INVALID_METADATA", f"{field} is required")
    return oid


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) and item.strip() and "\x00" not in item for item in value
    ):
        raise ControllerError("INVALID_METADATA", f"{field} must be a string list")
    return tuple(item.strip() for item in value)


def _scope_paths(value: Any) -> tuple[str, ...]:
    scopes = _string_list(value, "scope_paths")
    for scope in scopes:
        path = PurePosixPath(scope)
        if (
            "\\" in scope
            or path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.as_posix() != scope
            or scope == ".git"
            or scope.startswith(".git/")
        ):
            raise ControllerError("INVALID_METADATA", f"scope path is unsafe: {scope}")
    if scopes != tuple(sorted(set(scopes))):
        raise ControllerError("INVALID_METADATA", "scope_paths must be sorted and unique")
    return scopes


def _source_allocations(value: Any) -> tuple[str, ...]:
    allocations = _string_list(value, "source_allocations")
    if allocations != tuple(dict.fromkeys(allocations)):
        raise ControllerError("INVALID_METADATA", "source_allocations must be unique")
    for allocation_id in allocations:
        try:
            uuid.UUID(allocation_id)
        except ValueError as exc:
            raise ControllerError("INVALID_METADATA", "source allocation must be a UUID") from exc
    return allocations


def _optional_uuid(value: Any, field: str) -> str | None:
    text = _optional_string(value, field)
    if text is None:
        return None
    if len(text) == 36 and _CANONICAL_UUID.fullmatch(text) is not None:
        return text
    try:
        uuid.UUID(text)
    except ValueError as exc:
        raise ControllerError("INVALID_METADATA", f"{field} must be a UUID") from exc
    return text


def _optional_recovery_ref(value: Any) -> str | None:
    ref = _optional_string(value, "recovery_ref")
    if ref is None:
        return None
    branch = ref.removeprefix("refs/heads/")
    if (
        not ref.startswith("refs/heads/")
        or not branch
        or branch.startswith("-")
        or branch.endswith("/")
        or any(token in branch for token in ("..", "//", "@{", "\\"))
    ):
        raise ControllerError("INVALID_METADATA", "recovery_ref must be a safe branch ref")
    return ref


def _disposable_resources(value: Any, *, schema_version: int) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        raise ControllerError("INVALID_METADATA", "disposable_resources must be an object list")
    resources: list[dict[str, Any]] = []
    for item in value:
        legacy_fields = {"resource_id", "path", "kind", "state"}
        current_fields = legacy_fields | {
            "reproducible",
            "creator",
            "teardown_policy",
        }
        expected = legacy_fields if schema_version == 2 else current_fields
        if not isinstance(item, dict) or set(item) != expected:
            raise ControllerError("INVALID_METADATA", "disposable resource fields are invalid")
        resource_id = _optional_uuid(item.get("resource_id"), "resource_id")
        if resource_id is None:
            raise ControllerError("INVALID_METADATA", "resource_id is required")
        kind = _nonempty_string(item.get("kind"), "resource kind")
        if kind != "python-venv":
            raise ControllerError("INVALID_METADATA", f"unsupported disposable resource: {kind}")
        state = _nonempty_string(item.get("state"), "resource state")
        if state not in {"registered", "cleaned"}:
            raise ControllerError("INVALID_METADATA", f"unsupported resource state: {state}")
        reproducible = item.get("reproducible", False)
        if not isinstance(reproducible, bool):
            raise ControllerError("INVALID_METADATA", "resource reproducible must be boolean")
        creator = (
            _nonempty_string(item.get("creator"), "resource creator")
            if schema_version >= 3
            else "controller-v2-migration"
        )
        teardown_policy = (
            _nonempty_string(item.get("teardown_policy"), "resource teardown policy")
            if schema_version >= 3
            else "manual"
        )
        if teardown_policy not in {"manual", "on-release"}:
            raise ControllerError(
                "INVALID_METADATA", f"unsupported resource teardown policy: {teardown_policy}"
            )
        resources.append(
            {
                "resource_id": resource_id,
                "path": _nonempty_string(item.get("path"), "resource path"),
                "kind": kind,
                "reproducible": reproducible,
                "creator": creator,
                "teardown_policy": teardown_policy,
                "state": state,
            }
        )
        if resources[-1]["path"] != ".venv":
            raise ControllerError("INVALID_METADATA", "python-venv path must be .venv")
        if not reproducible and teardown_policy == "on-release":
            raise ControllerError(
                "INVALID_METADATA", "non-reproducible resources cannot use on-release teardown"
            )
    paths = [str(resource["path"]) for resource in resources]
    if len(paths) != len(set(paths)):
        raise ControllerError("INVALID_METADATA", "disposable resource paths must be unique")
    return tuple(resources)


def _validate_timestamp(value: Any, field: str) -> str:
    text = _nonempty_string(value, field)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ControllerError("INVALID_METADATA", f"{field} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ControllerError("INVALID_METADATA", f"{field} must include a timezone")
    return text


def _required_sha256(value: Any, field: str) -> str:
    text = _nonempty_string(value, field)
    if len(text) != 64 or _LOWERCASE_HEX.fullmatch(text) is None:
        raise ControllerError("INVALID_METADATA", f"{field} must be a SHA-256 digest")
    return text


def _optional_sha256(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _required_sha256(value, field)


def _scope_digest_value(scopes: tuple[str, ...]) -> str:
    payload = json.dumps(list(scopes), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _nonnegative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ControllerError("INVALID_METADATA", f"{field} must be a non-negative integer")
    return value


def _positive_int(value: Any, field: str) -> int:
    result = _nonnegative_int(value, field)
    if result < 1:
        raise ControllerError("INVALID_METADATA", f"{field} must be positive")
    return result


def validate_teardown_receipt(data: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "schema_version",
        "receipt_id",
        "transaction_id",
        "repository_id",
        "allocation_id",
        "owner",
        "authority_key",
        "worktree_path",
        "worktree_head",
        "environment_path",
        "pyvenv_cfg_sha256",
        "cachedir_tag_sha256",
        "lock_path",
        "lock_sha256",
        "project_path",
        "project_sha256",
        "python_version",
        "python_implementation",
        "python_identity_sha256",
        "environment_identity_sha256",
        "environment_device",
        "environment_inode",
        "bytes_before",
        "entries_before",
        "result",
        "created_at",
        "updated_at",
    }
    if set(data) != expected or data.get("schema_version") != TEARDOWN_RECEIPT_SCHEMA_VERSION:
        raise ControllerError("INVALID_METADATA", "teardown receipt schema is invalid")
    receipt_id = _optional_uuid(data.get("receipt_id"), "receipt_id")
    transaction_id = _optional_uuid(data.get("transaction_id"), "transaction_id")
    repository_id = _optional_uuid(data.get("repository_id"), "repository_id")
    allocation_id = _optional_uuid(data.get("allocation_id"), "allocation_id")
    if None in {receipt_id, transaction_id, repository_id, allocation_id}:
        raise ControllerError("INVALID_METADATA", "teardown receipt identifiers are required")
    if receipt_id != transaction_id:
        raise ControllerError("INVALID_METADATA", "receipt and transaction identifiers differ")
    worktree_path = Path(_nonempty_string(data.get("worktree_path"), "worktree_path"))
    if not worktree_path.is_absolute():
        raise ControllerError("INVALID_METADATA", "teardown worktree path must be absolute")
    if data.get("environment_path") != ".venv":
        raise ControllerError("INVALID_METADATA", "teardown environment path must be .venv")
    lock_path = data.get("lock_path")
    project_path = data.get("project_path")
    if (
        not isinstance(lock_path, str)
        or not isinstance(project_path, str)
        or not lock_path
        or not project_path
    ):
        raise ControllerError("INVALID_METADATA", "teardown project identity paths are invalid")
    lock_p = PurePosixPath(lock_path)
    project_p = PurePosixPath(project_path)
    if (
        lock_p.is_absolute()
        or project_p.is_absolute()
        or any(part in {"..", ".", ""} for part in lock_p.parts)
        or any(part in {"..", ".", ""} for part in project_p.parts)
        or lock_p.name != "uv.lock"
        or project_p.name != "pyproject.toml"
        or lock_p.parent != project_p.parent
    ):
        raise ControllerError("INVALID_METADATA", "teardown project identity paths are invalid")
    bytes_before = data.get("bytes_before")
    entries_before = data.get("entries_before")
    environment_device = data.get("environment_device")
    environment_inode = data.get("environment_inode")
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in (bytes_before, entries_before, environment_device, environment_inode)
    ):
        raise ControllerError(
            "INVALID_METADATA", "teardown filesystem and size evidence must be non-negative"
        )
    result = _nonempty_string(data.get("result"), "teardown result")
    if result not in TEARDOWN_RECEIPT_RESULTS:
        raise ControllerError("INVALID_METADATA", "teardown receipt result is invalid")
    validated = dict(data)
    for field in (
        "pyvenv_cfg_sha256",
        "cachedir_tag_sha256",
        "lock_sha256",
        "project_sha256",
        "python_identity_sha256",
        "environment_identity_sha256",
    ):
        validated[field] = _required_sha256(data.get(field), field)
    validated["worktree_head"] = _required_oid(data.get("worktree_head"), "worktree_head")
    validated["owner"] = _nonempty_string(data.get("owner"), "owner")
    validated["authority_key"] = _nonempty_string(data.get("authority_key"), "authority_key")
    validated["python_version"] = _nonempty_string(data.get("python_version"), "python_version")
    validated["python_implementation"] = _optional_string(
        data.get("python_implementation"), "python_implementation"
    )
    validated["created_at"] = _validate_timestamp(data.get("created_at"), "created_at")
    validated["updated_at"] = _validate_timestamp(data.get("updated_at"), "updated_at")
    return validated


def record_from_dict(data: Mapping[str, Any]) -> MetadataRecord:
    expected_v1 = {
        "schema_version",
        "record_version",
        "allocation_id",
        "task_key",
        "authority_key",
        "module",
        "role",
        "owner",
        "lifecycle",
        "created_at",
        "last_reviewed_at",
        "handoff_commit",
        "finalizer_record_id",
        "external_bindings",
        "history",
    }
    expected_v2 = expected_v1 | {
        "base_commit",
        "task_commit",
        "finalizer_commit",
        "scope_paths",
        "source_allocations",
        "quarantine_expected_commit",
        "quarantine_observed_commit",
        "quarantine_reason",
        "recovery_allocation_id",
        "recovery_ref",
        "disposable_resources",
    }
    schema_version = data.get("schema_version")
    expected = expected_v1 if schema_version == 1 else expected_v2
    if schema_version not in SUPPORTED_METADATA_SCHEMA_VERSIONS or set(data) != expected:
        raise ControllerError("INVALID_METADATA", "record schema fields are invalid")
    record_version = data.get("record_version")
    if (
        not isinstance(record_version, int)
        or isinstance(record_version, bool)
        or record_version < 1
    ):
        raise ControllerError("INVALID_METADATA", "record_version must be positive")
    allocation_id = _nonempty_string(data.get("allocation_id"), "allocation_id")
    try:
        uuid.UUID(allocation_id)
    except ValueError as exc:
        raise ControllerError("INVALID_METADATA", "allocation_id must be a UUID") from exc
    role = _nonempty_string(data.get("role"), "role")
    if role not in WORKTREE_ROLES:
        raise ControllerError("INVALID_METADATA", f"unsupported role: {role}")
    lifecycle = _nonempty_string(data.get("lifecycle"), "lifecycle")
    if lifecycle not in LIFECYCLE_STATES:
        raise ControllerError("INVALID_METADATA", f"unsupported lifecycle: {lifecycle}")
    external = _string_list(data.get("external_bindings"), "external_bindings")
    history = data.get("history")
    if not isinstance(history, list) or not all(isinstance(item, dict) for item in history):
        raise ControllerError("INVALID_METADATA", "history must be an object list")
    return MetadataRecord(
        schema_version=schema_version,
        record_version=record_version,
        allocation_id=allocation_id,
        task_key=_nonempty_string(data.get("task_key"), "task_key"),
        authority_key=_nonempty_string(data.get("authority_key"), "authority_key"),
        module=_nonempty_string(data.get("module"), "module"),
        role=role,
        owner=_nonempty_string(data.get("owner"), "owner"),
        lifecycle=lifecycle,
        created_at=_validate_timestamp(data.get("created_at"), "created_at"),
        last_reviewed_at=_validate_timestamp(data.get("last_reviewed_at"), "last_reviewed_at"),
        handoff_commit=_optional_string(data.get("handoff_commit"), "handoff_commit"),
        finalizer_record_id=_optional_string(
            data.get("finalizer_record_id"), "finalizer_record_id"
        ),
        base_commit=(
            _optional_oid(data.get("base_commit"), "base_commit") if schema_version >= 2 else None
        ),
        task_commit=(
            _optional_oid(data.get("task_commit"), "task_commit")
            if schema_version >= 2
            else _optional_oid(data.get("handoff_commit"), "handoff_commit")
        ),
        finalizer_commit=(
            _optional_oid(data.get("finalizer_commit"), "finalizer_commit")
            if schema_version >= 2
            else (
                _optional_oid(data.get("handoff_commit"), "handoff_commit")
                if data.get("finalizer_record_id")
                else None
            )
        ),
        scope_paths=(_scope_paths(data.get("scope_paths")) if schema_version >= 2 else ()),
        source_allocations=(
            _source_allocations(data.get("source_allocations")) if schema_version >= 2 else ()
        ),
        quarantine_expected_commit=(
            _optional_oid(data.get("quarantine_expected_commit"), "quarantine_expected_commit")
            if schema_version >= 2
            else None
        ),
        quarantine_observed_commit=(
            _optional_oid(data.get("quarantine_observed_commit"), "quarantine_observed_commit")
            if schema_version >= 2
            else None
        ),
        quarantine_reason=(
            _optional_string(data.get("quarantine_reason"), "quarantine_reason")
            if schema_version >= 2
            else None
        ),
        recovery_allocation_id=(
            _optional_uuid(data.get("recovery_allocation_id"), "recovery_allocation_id")
            if schema_version >= 2
            else None
        ),
        recovery_ref=(
            _optional_recovery_ref(data.get("recovery_ref")) if schema_version >= 2 else None
        ),
        disposable_resources=(
            _disposable_resources(data.get("disposable_resources"), schema_version=schema_version)
            if schema_version >= 2
            else ()
        ),
        external_bindings=external,
        history=tuple(dict(item) for item in history),
    )


def record_to_dict(record: MetadataRecord) -> dict[str, Any]:
    payload = asdict(record)
    payload["schema_version"] = METADATA_SCHEMA_VERSION
    payload["scope_paths"] = list(record.scope_paths)
    payload["source_allocations"] = list(record.source_allocations)
    payload["disposable_resources"] = [dict(item) for item in record.disposable_resources]
    payload["external_bindings"] = list(record.external_bindings)
    payload["history"] = [dict(item) for item in record.history]
    return payload


def integration_intent_from_dict(data: Mapping[str, Any]) -> IntegrationIntent:
    expected_v1 = {
        "schema_version",
        "intent_id",
        "task_key",
        "authority_key",
        "module",
        "owner",
        "source_allocations",
        "source_handoffs",
        "scope_paths",
        "state",
        "allocation_id",
        "base_main_oid",
        "candidate_oid",
        "revision",
        "validation_evidence_id",
        "validated_oid",
        "blocked_reason",
        "enqueued_at",
        "claimed_at",
        "validated_at",
        "published_at",
        "released_at",
        "updated_at",
        "history",
    }
    expected_v2 = expected_v1 | {"equivalent_replays"}
    expected_v3 = expected_v2 | {
        "contract_version",
        "source_branches",
        "source_finalizer_record_ids",
        "source_snapshot_ids",
        "scope_digest",
        "queue_sequence",
        "attempt",
        "prepare_identity",
        "publication_identity",
        "recovery_identity",
        "prepared_receipt_id",
        "prepared_base_origin_main_oid",
        "prepared_commit_oid",
        "prepared_tree_oid",
        "prepared_scope_digest",
        "tests_receipt_id",
        "snapshot_receipt_id",
    }
    schema_version = data.get("schema_version")
    expected = {1: expected_v1, 2: expected_v2, 3: expected_v3}.get(schema_version)
    if expected is None or set(data) != expected:
        raise ControllerError("INVALID_METADATA", "integration intent schema is invalid")
    intent_id = _optional_uuid(data.get("intent_id"), "intent_id")
    if intent_id is None:
        raise ControllerError("INVALID_METADATA", "intent_id is required")
    state = _nonempty_string(data.get("state"), "intent state")
    if state not in INTEGRATION_INTENT_STATES:
        raise ControllerError("INVALID_METADATA", "integration intent state is invalid")
    if schema_version < 3:
        state = {
            "CLAIMED": "PREPARING_INTEGRATION",
            "VALIDATED": "READY_TO_PUBLISH",
            "BLOCKED": "ATTENTION_REQUIRED",
            "PUBLISHED": "REMOTE_VERIFIED",
        }.get(state, state)
    source_allocations = _source_allocations(data.get("source_allocations"))
    source_handoffs = _string_list(data.get("source_handoffs"), "source_handoffs")
    if len(source_allocations) != len(source_handoffs) or not source_handoffs:
        raise ControllerError(
            "INVALID_METADATA", "integration intent sources and frozen handoffs must align"
        )
    for index, oid in enumerate(source_handoffs):
        _required_oid(oid, f"source_handoffs[{index}]")
    source_count = len(source_allocations)
    source_branches = (
        _string_list(data.get("source_branches"), "source_branches")
        if schema_version >= 3
        else tuple("legacy-unrecorded" for _ in range(source_count))
    )
    source_finalizers = (
        _string_list(data.get("source_finalizer_record_ids"), "source_finalizer_record_ids")
        if schema_version >= 3
        else tuple("legacy-unrecorded" for _ in range(source_count))
    )
    source_snapshots = (
        _string_list(data.get("source_snapshot_ids"), "source_snapshot_ids")
        if schema_version >= 3
        else tuple("legacy-unrecorded" for _ in range(source_count))
    )
    if any(
        len(values) != source_count
        for values in (source_branches, source_finalizers, source_snapshots)
    ):
        raise ControllerError("INVALID_METADATA", "integration source evidence must align")
    scopes = _scope_paths(data.get("scope_paths"))
    scope_digest = (
        _required_sha256(data.get("scope_digest"), "scope_digest")
        if schema_version >= 3
        else _scope_digest_value(scopes)
    )
    if scope_digest != _scope_digest_value(scopes):
        raise ControllerError("INVALID_METADATA", "integration scope digest differs")
    history = data.get("history")
    if (
        not isinstance(history, list)
        or not history
        or not all(isinstance(item, dict) for item in history)
    ):
        raise ControllerError("INVALID_METADATA", "integration intent history is invalid")
    allocation_id = _optional_uuid(data.get("allocation_id"), "allocation_id")
    candidate_oid = _optional_oid(data.get("candidate_oid"), "candidate_oid")
    validated_oid = _optional_oid(data.get("validated_oid"), "validated_oid")
    validation_evidence_id = _optional_string(
        data.get("validation_evidence_id"), "validation_evidence_id"
    )
    if state in {"QUEUED", "WAITING_SCOPE"} and any(
        value is not None
        for value in (allocation_id, candidate_oid, validated_oid, validation_evidence_id)
    ):
        raise ControllerError("INVALID_METADATA", "queued intent cannot bind candidate evidence")
    if state in {
        "PREPARING_INTEGRATION",
        "READY_TO_PUBLISH",
        "NEEDS_REPREPARE",
        "PUBLISHING",
        "RECOVERY_REQUIRED",
        "REMOTE_VERIFIED",
        "RELEASED",
    } and (allocation_id is None or candidate_oid is None):
        raise ControllerError("INVALID_METADATA", "claimed intent lacks candidate identity")
    if state in {"READY_TO_PUBLISH", "PUBLISHING", "REMOTE_VERIFIED", "RELEASED"} and (
        validated_oid != candidate_oid or validation_evidence_id is None
    ):
        raise ControllerError("INVALID_METADATA", "validated intent evidence is incomplete")
    raw_replays = data.get("equivalent_replays", [])
    if not isinstance(raw_replays, list):
        raise ControllerError("INVALID_METADATA", "equivalent_replays must be an object list")
    equivalent_replays: list[dict[str, str]] = []
    for replay in raw_replays:
        fields = {
            "source_allocation_id",
            "source_handoff_oid",
            "replay_oid",
            "evidence_id",
        }
        if not isinstance(replay, dict) or set(replay) != fields:
            raise ControllerError("INVALID_METADATA", "equivalent replay fields are invalid")
        source_allocation_id = _optional_uuid(
            replay.get("source_allocation_id"), "source_allocation_id"
        )
        if source_allocation_id is None or source_allocation_id not in source_allocations:
            raise ControllerError(
                "INVALID_METADATA", "equivalent replay source allocation is invalid"
            )
        source_index = source_allocations.index(source_allocation_id)
        source_handoff_oid = _required_oid(replay.get("source_handoff_oid"), "source_handoff_oid")
        if source_handoff_oid != source_handoffs[source_index]:
            raise ControllerError("INVALID_METADATA", "equivalent replay source handoff differs")
        equivalent_replays.append(
            {
                "source_allocation_id": source_allocation_id,
                "source_handoff_oid": source_handoff_oid,
                "replay_oid": _required_oid(replay.get("replay_oid"), "replay_oid"),
                "evidence_id": _nonempty_string(replay.get("evidence_id"), "evidence_id"),
            }
        )
    replay_sources = [item["source_allocation_id"] for item in equivalent_replays]
    if len(replay_sources) != len(set(replay_sources)):
        raise ControllerError("INVALID_METADATA", "equivalent replay sources must be unique")
    prepared_receipt_id = (
        _optional_uuid(data.get("prepared_receipt_id"), "prepared_receipt_id")
        if schema_version >= 3
        else None
    )
    prepared_base = (
        _optional_oid(data.get("prepared_base_origin_main_oid"), "prepared_base_origin_main_oid")
        if schema_version >= 3
        else (
            _optional_oid(data.get("base_main_oid"), "base_main_oid")
            if state == "READY_TO_PUBLISH"
            else None
        )
    )
    prepared_commit = (
        _optional_oid(data.get("prepared_commit_oid"), "prepared_commit_oid")
        if schema_version >= 3
        else (candidate_oid if state == "READY_TO_PUBLISH" else None)
    )
    prepared_tree = (
        _optional_oid(data.get("prepared_tree_oid"), "prepared_tree_oid")
        if schema_version >= 3
        else None
    )
    prepared_scope = (
        _optional_sha256(data.get("prepared_scope_digest"), "prepared_scope_digest")
        if schema_version >= 3
        else (scope_digest if state == "READY_TO_PUBLISH" else None)
    )
    prepare_identity = (
        _optional_sha256(data.get("prepare_identity"), "prepare_identity")
        if schema_version >= 3
        else None
    )
    tests_receipt_id = (
        _optional_string(data.get("tests_receipt_id"), "tests_receipt_id")
        if schema_version >= 3
        else None
    )
    snapshot_receipt_id = (
        _optional_string(data.get("snapshot_receipt_id"), "snapshot_receipt_id")
        if schema_version >= 3
        else None
    )
    if (
        schema_version >= 3
        and state
        in {
            "READY_TO_PUBLISH",
            "PUBLISHING",
            "REMOTE_VERIFIED",
            "RELEASED",
        }
        and any(
            value is None
            for value in (
                prepared_receipt_id,
                prepared_base,
                prepared_commit,
                prepared_tree,
                prepared_scope,
                prepare_identity,
                tests_receipt_id,
                snapshot_receipt_id,
            )
        )
    ):
        raise ControllerError("INVALID_METADATA", "prepared candidate contract is incomplete")
    contract_version = (
        _positive_int(data.get("contract_version"), "contract_version")
        if schema_version >= 3
        else 1
    )
    if schema_version >= 3 and contract_version != INTEGRATION_INTENT_CONTRACT_VERSION:
        raise ControllerError("INVALID_METADATA", "integration intent contract version is invalid")
    return IntegrationIntent(
        schema_version=int(schema_version),
        contract_version=contract_version,
        intent_id=intent_id,
        task_key=_nonempty_string(data.get("task_key"), "task_key"),
        authority_key=_nonempty_string(data.get("authority_key"), "authority_key"),
        module=_nonempty_string(data.get("module"), "module"),
        owner=_nonempty_string(data.get("owner"), "owner"),
        source_allocations=source_allocations,
        source_handoffs=tuple(source_handoffs),
        source_branches=tuple(source_branches),
        source_finalizer_record_ids=tuple(source_finalizers),
        source_snapshot_ids=tuple(source_snapshots),
        scope_paths=scopes,
        scope_digest=scope_digest,
        queue_sequence=(
            _nonnegative_int(data.get("queue_sequence"), "queue_sequence")
            if schema_version >= 3
            else 0
        ),
        state=state,
        allocation_id=allocation_id,
        base_main_oid=_optional_oid(data.get("base_main_oid"), "base_main_oid"),
        candidate_oid=candidate_oid,
        revision=_nonnegative_int(data.get("revision"), "revision"),
        attempt=(
            _positive_int(data.get("attempt"), "attempt")
            if schema_version >= 3
            else max(_nonnegative_int(data.get("revision"), "revision"), 1)
        ),
        prepare_identity=prepare_identity,
        publication_identity=(
            _optional_uuid(data.get("publication_identity"), "publication_identity")
            if schema_version >= 3
            else None
        ),
        recovery_identity=(
            _optional_string(data.get("recovery_identity"), "recovery_identity")
            if schema_version >= 3
            else None
        ),
        prepared_receipt_id=prepared_receipt_id,
        prepared_base_origin_main_oid=prepared_base,
        prepared_commit_oid=prepared_commit,
        prepared_tree_oid=prepared_tree,
        prepared_scope_digest=prepared_scope,
        tests_receipt_id=tests_receipt_id,
        snapshot_receipt_id=snapshot_receipt_id,
        validation_evidence_id=validation_evidence_id,
        validated_oid=validated_oid,
        blocked_reason=_optional_string(data.get("blocked_reason"), "blocked_reason"),
        enqueued_at=_validate_timestamp(data.get("enqueued_at"), "enqueued_at"),
        claimed_at=(
            _validate_timestamp(data.get("claimed_at"), "claimed_at")
            if data.get("claimed_at") is not None
            else None
        ),
        validated_at=(
            _validate_timestamp(data.get("validated_at"), "validated_at")
            if data.get("validated_at") is not None
            else None
        ),
        published_at=(
            _validate_timestamp(data.get("published_at"), "published_at")
            if data.get("published_at") is not None
            else None
        ),
        released_at=(
            _validate_timestamp(data.get("released_at"), "released_at")
            if data.get("released_at") is not None
            else None
        ),
        updated_at=_validate_timestamp(data.get("updated_at"), "updated_at"),
        equivalent_replays=tuple(equivalent_replays),
        history=tuple(dict(item) for item in history),
    )


def integration_intent_to_dict(intent: IntegrationIntent) -> dict[str, Any]:
    payload = asdict(intent)
    payload["source_allocations"] = list(intent.source_allocations)
    payload["source_handoffs"] = list(intent.source_handoffs)
    payload["source_branches"] = list(intent.source_branches)
    payload["source_finalizer_record_ids"] = list(intent.source_finalizer_record_ids)
    payload["source_snapshot_ids"] = list(intent.source_snapshot_ids)
    payload["scope_paths"] = list(intent.scope_paths)
    payload["schema_version"] = INTEGRATION_INTENT_SCHEMA_VERSION
    payload["contract_version"] = INTEGRATION_INTENT_CONTRACT_VERSION
    payload["equivalent_replays"] = [dict(item) for item in intent.equivalent_replays]
    payload["history"] = [dict(item) for item in intent.history]
    return payload


def prepared_candidate_receipt_from_dict(
    data: Mapping[str, Any],
) -> PreparedCandidateReceipt:
    expected = {
        "schema_version",
        "receipt_id",
        "repository_id",
        "intent_id",
        "allocation_id",
        "attempt",
        "base_origin_main_oid",
        "candidate_oid",
        "tree_oid",
        "scope_digest",
        "prepare_identity",
        "validation_evidence_id",
        "tests_receipt_id",
        "snapshot_receipt_id",
        "created_at",
    }
    if (
        set(data) != expected
        or data.get("schema_version") != PREPARED_CANDIDATE_RECEIPT_SCHEMA_VERSION
    ):
        raise ControllerError("INVALID_METADATA", "prepared candidate receipt schema is invalid")
    identifiers = {
        field: _optional_uuid(data.get(field), field)
        for field in ("receipt_id", "repository_id", "intent_id", "allocation_id")
    }
    if any(value is None for value in identifiers.values()):
        raise ControllerError("INVALID_METADATA", "prepared receipt identifiers are required")
    return PreparedCandidateReceipt(
        schema_version=PREPARED_CANDIDATE_RECEIPT_SCHEMA_VERSION,
        receipt_id=str(identifiers["receipt_id"]),
        repository_id=str(identifiers["repository_id"]),
        intent_id=str(identifiers["intent_id"]),
        allocation_id=str(identifiers["allocation_id"]),
        attempt=_positive_int(data.get("attempt"), "attempt"),
        base_origin_main_oid=_required_oid(
            data.get("base_origin_main_oid"), "base_origin_main_oid"
        ),
        candidate_oid=_required_oid(data.get("candidate_oid"), "candidate_oid"),
        tree_oid=_required_oid(data.get("tree_oid"), "tree_oid"),
        scope_digest=_required_sha256(data.get("scope_digest"), "scope_digest"),
        prepare_identity=_required_sha256(data.get("prepare_identity"), "prepare_identity"),
        validation_evidence_id=_nonempty_string(
            data.get("validation_evidence_id"), "validation_evidence_id"
        ),
        tests_receipt_id=_nonempty_string(data.get("tests_receipt_id"), "tests_receipt_id"),
        snapshot_receipt_id=_nonempty_string(
            data.get("snapshot_receipt_id"), "snapshot_receipt_id"
        ),
        created_at=_validate_timestamp(data.get("created_at"), "created_at"),
    )


def prepared_candidate_receipt_to_dict(receipt: PreparedCandidateReceipt) -> dict[str, Any]:
    return asdict(receipt)


def lifecycle_event_from_dict(data: Mapping[str, Any]) -> LifecycleEvent:
    expected = {
        "schema_version",
        "event_id",
        "repository_id",
        "intent_id",
        "allocation_id",
        "event_type",
        "state",
        "sequence",
        "identity",
        "created_at",
    }
    if set(data) != expected or data.get("schema_version") != LIFECYCLE_EVENT_SCHEMA_VERSION:
        raise ControllerError("INVALID_METADATA", "lifecycle event schema is invalid")
    identifiers = {
        field: _optional_uuid(data.get(field), field)
        for field in ("event_id", "repository_id", "intent_id")
    }
    if any(value is None for value in identifiers.values()):
        raise ControllerError("INVALID_METADATA", "lifecycle event identifiers are required")
    event_type = _nonempty_string(data.get("event_type"), "event_type")
    if event_type not in INTEGRATION_EVENT_TYPES:
        raise ControllerError("INVALID_METADATA", "lifecycle event type is invalid")
    state = _nonempty_string(data.get("state"), "event state")
    if state not in INTEGRATION_INTENT_STATES:
        raise ControllerError("INVALID_METADATA", "lifecycle event state is invalid")
    return LifecycleEvent(
        schema_version=LIFECYCLE_EVENT_SCHEMA_VERSION,
        event_id=str(identifiers["event_id"]),
        repository_id=str(identifiers["repository_id"]),
        intent_id=str(identifiers["intent_id"]),
        allocation_id=_optional_uuid(data.get("allocation_id"), "allocation_id"),
        event_type=event_type,
        state=state,
        sequence=_nonnegative_int(data.get("sequence"), "sequence"),
        identity=_required_sha256(data.get("identity"), "identity"),
        created_at=_validate_timestamp(data.get("created_at"), "created_at"),
    )


def lifecycle_event_to_dict(event: LifecycleEvent) -> dict[str, Any]:
    return asdict(event)


def integration_reconciliation_receipt_from_dict(
    data: Mapping[str, Any],
) -> IntegrationReconciliationReceipt:
    expected = {
        "schema_version",
        "receipt_id",
        "repository_id",
        "intent_id",
        "observed_intent_schema_version",
        "observed_intent_state",
        "observed_allocation_id",
        "observed_record_version",
        "observed_record_lifecycle",
        "effective_terminal_state",
        "result",
        "actor",
        "reconciled_at",
    }
    if (
        set(data) != expected
        or data.get("schema_version") != INTEGRATION_RECONCILIATION_RECEIPT_SCHEMA_VERSION
    ):
        raise ControllerError(
            "INVALID_METADATA", "integration reconciliation receipt schema is invalid"
        )
    identifiers = {
        field: _optional_uuid(data.get(field), field)
        for field in ("receipt_id", "repository_id", "intent_id")
    }
    if any(value is None for value in identifiers.values()):
        raise ControllerError("INVALID_METADATA", "integration reconciliation ids are required")
    record_version = data.get("observed_record_version")
    if record_version is not None:
        record_version = _positive_int(record_version, "observed_record_version")
    lifecycle = _optional_string(data.get("observed_record_lifecycle"), "observed_record_lifecycle")
    if lifecycle is not None and lifecycle not in LIFECYCLE_STATES:
        raise ControllerError("INVALID_METADATA", "observed record lifecycle is invalid")
    terminal = _nonempty_string(data.get("effective_terminal_state"), "terminal state")
    if terminal != "RELEASED":
        raise ControllerError("INVALID_METADATA", "reconciled terminal state must be RELEASED")
    result = _nonempty_string(data.get("result"), "reconciliation result")
    if result != "STALE_INTENT_TERMINAL_ACKNOWLEDGED":
        raise ControllerError("INVALID_METADATA", "integration reconciliation result is invalid")
    return IntegrationReconciliationReceipt(
        schema_version=INTEGRATION_RECONCILIATION_RECEIPT_SCHEMA_VERSION,
        receipt_id=str(identifiers["receipt_id"]),
        repository_id=str(identifiers["repository_id"]),
        intent_id=str(identifiers["intent_id"]),
        observed_intent_schema_version=_positive_int(
            data.get("observed_intent_schema_version"), "observed_intent_schema_version"
        ),
        observed_intent_state=_nonempty_string(
            data.get("observed_intent_state"), "observed_intent_state"
        ),
        observed_allocation_id=_optional_uuid(
            data.get("observed_allocation_id"), "observed_allocation_id"
        ),
        observed_record_version=record_version,
        observed_record_lifecycle=lifecycle,
        effective_terminal_state=terminal,
        result=result,
        actor=_nonempty_string(data.get("actor"), "actor"),
        reconciled_at=_validate_timestamp(data.get("reconciled_at"), "reconciled_at"),
    )


def integration_reconciliation_receipt_to_dict(
    receipt: IntegrationReconciliationReceipt,
) -> dict[str, Any]:
    return asdict(receipt)


WRITER_LEASE_SCHEMA_VERSION = 2
WRITER_RECEIPT_SCHEMA_VERSION = 1


def writer_lease_from_dict(data: Mapping[str, Any]) -> WriterLease:
    expected = {
        "schema_version",
        "lease_id",
        "repository_id",
        "allocation_id",
        "worktree_path",
        "owner",
        "holder",
        "acquired_at",
        "renewed_at",
        "expires_at",
        "record_version",
    }
    if data.get("schema_version") != WRITER_LEASE_SCHEMA_VERSION or set(data) != expected:
        raise ControllerError("INVALID_METADATA", "writer lease schema is invalid")
    lease_id = _optional_uuid(data.get("lease_id"), "lease_id")
    allocation_id = _optional_uuid(data.get("allocation_id"), "allocation_id")
    if lease_id is None or allocation_id is None:
        raise ControllerError("INVALID_METADATA", "writer lease identifiers are required")
    holder = data.get("holder")
    if not isinstance(holder, Mapping) or set(holder) != {
        "client_id",
        "pid",
        "boot_id",
        "start_ticks",
        "session_id",
        "run_id",
    }:
        raise ControllerError("INVALID_METADATA", "writer lease holder is invalid")
    session_id = holder.get("session_id")
    run_id = holder.get("run_id")
    if session_id is not None and not isinstance(session_id, str):
        raise ControllerError("INVALID_METADATA", "writer lease session is invalid")
    if run_id is not None and not isinstance(run_id, str):
        raise ControllerError("INVALID_METADATA", "writer lease run is invalid")
    raw_expiry = data.get("expires_at")
    return WriterLease(
        schema_version=WRITER_LEASE_SCHEMA_VERSION,
        lease_id=lease_id,
        repository_id=_nonempty_string(data.get("repository_id"), "repository_id"),
        allocation_id=allocation_id,
        worktree_path=_nonempty_string(data.get("worktree_path"), "worktree_path"),
        owner=_nonempty_string(data.get("owner"), "owner"),
        holder=WriterHolder(
            client_id=_nonempty_string(holder.get("client_id"), "client_id"),
            pid=_positive_int(holder.get("pid"), "pid"),
            boot_id=_nonempty_string(holder.get("boot_id"), "boot_id"),
            start_ticks=_positive_int(holder.get("start_ticks"), "start_ticks"),
            session_id=session_id,
            run_id=run_id,
        ),
        acquired_at=_validate_timestamp(data.get("acquired_at"), "acquired_at"),
        renewed_at=_validate_timestamp(data.get("renewed_at"), "renewed_at"),
        expires_at=(None if raw_expiry is None else _validate_timestamp(raw_expiry, "expires_at")),
        record_version=_positive_int(data.get("record_version"), "record_version"),
    )


def writer_lease_to_dict(lease: WriterLease) -> dict[str, Any]:
    return {
        "schema_version": lease.schema_version,
        "lease_id": lease.lease_id,
        "repository_id": lease.repository_id,
        "allocation_id": lease.allocation_id,
        "worktree_path": lease.worktree_path,
        "owner": lease.owner,
        "holder": {
            "client_id": lease.holder.client_id,
            "pid": lease.holder.pid,
            "boot_id": lease.holder.boot_id,
            "start_ticks": lease.holder.start_ticks,
            "session_id": lease.holder.session_id,
            "run_id": lease.holder.run_id,
        },
        "acquired_at": lease.acquired_at,
        "renewed_at": lease.renewed_at,
        "expires_at": lease.expires_at,
        "record_version": lease.record_version,
    }


def writer_receipt_to_dict(receipt: WriterReconciliationReceipt) -> dict[str, Any]:
    return {
        "schema_version": receipt.schema_version,
        "receipt_id": receipt.receipt_id,
        "repository_id": receipt.repository_id,
        "allocation_id": receipt.allocation_id,
        "actor": receipt.actor,
        "owner": receipt.owner,
        "trigger": receipt.trigger,
        "outcome": receipt.outcome,
        "reason": receipt.reason,
        "evidence": list(receipt.evidence),
        "previous_lifecycle": receipt.previous_lifecycle,
        "new_lifecycle": receipt.new_lifecycle,
        "reconciled_at": receipt.reconciled_at,
    }


def publication_lease_from_dict(data: Mapping[str, Any]) -> PublicationLease:
    legacy = {
        "schema_version",
        "lease_id",
        "allocation_id",
        "owner",
        "remote",
        "branch",
        "candidate_head",
        "remote_head",
        "finalizer_record_id",
        "acquired_at",
    }
    expected = {
        "schema_version",
        "lease_id",
        "repository_id",
        "allocation_id",
        "holder",
        "run_id",
        "remote",
        "branch",
        "target_ref",
        "candidate_oid",
        "expected_main_oid",
        "validation_evidence_id",
        "acquired_at",
        "expires_at",
    }
    if data.get("schema_version") != PUBLICATION_LEASE_SCHEMA_VERSION or frozenset(data) not in {
        frozenset(legacy),
        frozenset(expected),
    }:
        raise ControllerError("INVALID_METADATA", "publication lease schema is invalid")
    lease_id = _optional_uuid(data.get("lease_id"), "lease_id")
    allocation_id = _optional_uuid(data.get("allocation_id"), "allocation_id")
    if lease_id is None or allocation_id is None:
        raise ControllerError("INVALID_METADATA", "publication lease identifiers are required")
    if set(data) == legacy:
        acquired_at = _validate_timestamp(data.get("acquired_at"), "acquired_at")
        return PublicationLease(
            schema_version=PUBLICATION_LEASE_SCHEMA_VERSION,
            lease_id=lease_id,
            repository_id="00000000-0000-0000-0000-000000000000",
            allocation_id=allocation_id,
            holder=_nonempty_string(data.get("owner"), "lease owner"),
            run_id=f"legacy-{lease_id}",
            remote=_nonempty_string(data.get("remote"), "lease remote"),
            branch=_nonempty_string(data.get("branch"), "lease branch"),
            target_ref=f"refs/heads/{_nonempty_string(data.get('branch'), 'lease branch')}",
            candidate_oid=_required_oid(data.get("candidate_head"), "candidate_head"),
            expected_main_oid=_required_oid(data.get("remote_head"), "remote_head"),
            validation_evidence_id=_nonempty_string(
                data.get("finalizer_record_id"), "finalizer_record_id"
            ),
            acquired_at=acquired_at,
            expires_at="9999-12-31T23:59:59+00:00",
        )
    repository_id = _optional_uuid(data.get("repository_id"), "repository_id")
    if repository_id is None:
        raise ControllerError("INVALID_METADATA", "lease repository_id is required")
    branch = _nonempty_string(data.get("branch"), "lease branch")
    target_ref = _nonempty_string(data.get("target_ref"), "target_ref")
    if target_ref != f"refs/heads/{branch}":
        raise ControllerError("INVALID_METADATA", "lease target_ref and branch differ")
    return PublicationLease(
        schema_version=PUBLICATION_LEASE_SCHEMA_VERSION,
        lease_id=lease_id,
        repository_id=repository_id,
        allocation_id=allocation_id,
        holder=_nonempty_string(data.get("holder"), "lease holder"),
        run_id=_nonempty_string(data.get("run_id"), "lease run_id"),
        remote=_nonempty_string(data.get("remote"), "lease remote"),
        branch=branch,
        target_ref=target_ref,
        candidate_oid=_required_oid(data.get("candidate_oid"), "candidate_oid"),
        expected_main_oid=_required_oid(data.get("expected_main_oid"), "expected_main_oid"),
        validation_evidence_id=_nonempty_string(
            data.get("validation_evidence_id"), "validation_evidence_id"
        ),
        acquired_at=_validate_timestamp(data.get("acquired_at"), "acquired_at"),
        expires_at=_validate_timestamp(data.get("expires_at"), "expires_at"),
    )


def publication_lease_to_dict(lease: PublicationLease) -> dict[str, Any]:
    return asdict(lease)


def publication_receipt_from_dict(data: Mapping[str, Any]) -> PublicationReceipt:
    expected_v1 = {
        "schema_version",
        "receipt_id",
        "repository_id",
        "allocation_id",
        "intent_id",
        "target_ref",
        "holder",
        "run_id",
        "expected_main_oid",
        "candidate_oid",
        "validation_evidence_id",
        "finalizer_record_id",
        "status",
        "acquired_at",
        "remote_verified_at",
        "lease_released_at",
        "released_at",
        "lease_duration_ms",
        "remote_verified_to_released_ms",
    }
    expected_v2 = expected_v1 | {"source_results"}
    schema_version = data.get("schema_version")
    expected = expected_v1 if schema_version == 1 else expected_v2
    if schema_version not in {1, PUBLICATION_RECEIPT_SCHEMA_VERSION} or set(data) != expected:
        raise ControllerError("INVALID_METADATA", "publication receipt schema is invalid")
    identifiers = {
        field: _optional_uuid(data.get(field), field)
        for field in ("receipt_id", "repository_id", "allocation_id")
    }
    if any(value is None for value in identifiers.values()):
        raise ControllerError("INVALID_METADATA", "publication receipt identifiers are required")
    intent_id = _optional_uuid(data.get("intent_id"), "intent_id")
    status = _nonempty_string(data.get("status"), "publication receipt status")
    if status not in PUBLICATION_RECEIPT_STATES:
        raise ControllerError("INVALID_METADATA", "publication receipt status is invalid")
    released_at = (
        _validate_timestamp(data.get("released_at"), "released_at")
        if data.get("released_at") is not None
        else None
    )
    released_duration = data.get("remote_verified_to_released_ms")
    if status == "RELEASED" and (
        released_at is None or released_duration is None or data.get("lease_released_at") is None
    ):
        raise ControllerError("INVALID_METADATA", "released publication receipt is incomplete")
    if released_duration is not None:
        released_duration = _nonnegative_int(released_duration, "remote_verified_to_released_ms")
    raw_source_results = data.get("source_results", [])
    if not isinstance(raw_source_results, list):
        raise ControllerError("INVALID_METADATA", "source_results must be an object list")
    source_results: list[dict[str, str]] = []
    for source_result in raw_source_results:
        fields = {
            "source_allocation_id",
            "source_handoff_oid",
            "mode",
            "absorbed_oid",
            "evidence_id",
        }
        if not isinstance(source_result, dict) or set(source_result) != fields:
            raise ControllerError("INVALID_METADATA", "publication source result is invalid")
        mode = _nonempty_string(source_result.get("mode"), "source result mode")
        if mode not in {"ancestry", "equivalent-replay"}:
            raise ControllerError("INVALID_METADATA", "publication source result mode is invalid")
        source_allocation_id = _optional_uuid(
            source_result.get("source_allocation_id"), "source_allocation_id"
        )
        if source_allocation_id is None:
            raise ControllerError("INVALID_METADATA", "source result allocation is required")
        source_results.append(
            {
                "source_allocation_id": source_allocation_id,
                "source_handoff_oid": _required_oid(
                    source_result.get("source_handoff_oid"), "source_handoff_oid"
                ),
                "mode": mode,
                "absorbed_oid": _required_oid(source_result.get("absorbed_oid"), "absorbed_oid"),
                "evidence_id": _nonempty_string(
                    source_result.get("evidence_id"), "source result evidence_id"
                ),
            }
        )
    source_allocations = [item["source_allocation_id"] for item in source_results]
    if len(source_allocations) != len(set(source_allocations)):
        raise ControllerError("INVALID_METADATA", "publication source results must be unique")
    return PublicationReceipt(
        schema_version=PUBLICATION_RECEIPT_SCHEMA_VERSION,
        receipt_id=str(identifiers["receipt_id"]),
        repository_id=str(identifiers["repository_id"]),
        allocation_id=str(identifiers["allocation_id"]),
        intent_id=intent_id,
        target_ref=_nonempty_string(data.get("target_ref"), "target_ref"),
        holder=_nonempty_string(data.get("holder"), "holder"),
        run_id=_nonempty_string(data.get("run_id"), "run_id"),
        expected_main_oid=_required_oid(data.get("expected_main_oid"), "expected_main_oid"),
        candidate_oid=_required_oid(data.get("candidate_oid"), "candidate_oid"),
        validation_evidence_id=_nonempty_string(
            data.get("validation_evidence_id"), "validation_evidence_id"
        ),
        finalizer_record_id=_nonempty_string(
            data.get("finalizer_record_id"), "finalizer_record_id"
        ),
        status=status,
        acquired_at=_validate_timestamp(data.get("acquired_at"), "acquired_at"),
        remote_verified_at=_validate_timestamp(
            data.get("remote_verified_at"), "remote_verified_at"
        ),
        lease_released_at=(
            _validate_timestamp(data.get("lease_released_at"), "lease_released_at")
            if data.get("lease_released_at") is not None
            else None
        ),
        released_at=released_at,
        lease_duration_ms=_nonnegative_int(data.get("lease_duration_ms"), "lease_duration_ms"),
        remote_verified_to_released_ms=released_duration,
        source_results=tuple(source_results),
    )


def publication_receipt_to_dict(receipt: PublicationReceipt) -> dict[str, Any]:
    payload = asdict(receipt)
    payload["schema_version"] = PUBLICATION_RECEIPT_SCHEMA_VERSION
    payload["source_results"] = [dict(item) for item in receipt.source_results]
    return payload


def release_intent_from_dict(data: Mapping[str, Any]) -> ReleaseIntent:
    expected = {
        "schema_version",
        "intent_id",
        "repository_id",
        "allocation_id",
        "task_key",
        "authority_key",
        "owner",
        "role",
        "expected_head",
        "expected_record_version",
        "task_commit",
        "handoff_commit",
        "finalizer_commit",
        "finalizer_record_id",
        "integration_intent_id",
        "integration_allocation_id",
        "publication_receipt_id",
        "integration_candidate_oid",
        "integration_mode",
        "integration_evidence_id",
        "trigger",
        "status",
        "blocker",
        "created_at",
        "updated_at",
        "expires_at",
        "history",
    }
    if set(data) != expected or data.get("schema_version") != RELEASE_INTENT_SCHEMA_VERSION:
        raise ControllerError("INVALID_METADATA", "release intent schema is invalid")
    identifiers = {
        field: _optional_uuid(data.get(field), field)
        for field in ("intent_id", "repository_id", "allocation_id")
    }
    if any(value is None for value in identifiers.values()):
        raise ControllerError("INVALID_METADATA", "release intent identifiers are required")
    role = _nonempty_string(data.get("role"), "release intent role")
    if role not in {"feature", "audit"}:
        raise ControllerError("INVALID_METADATA", "release intent role must be feature or audit")
    status = _nonempty_string(data.get("status"), "release intent status")
    if status not in RELEASE_INTENT_STATES:
        raise ControllerError("INVALID_METADATA", "release intent status is invalid")
    integration_mode = _optional_string(data.get("integration_mode"), "integration_mode")
    if integration_mode not in {None, "ancestry", "equivalent-replay"}:
        raise ControllerError("INVALID_METADATA", "release intent integration mode is invalid")
    integration_intent_id = _optional_uuid(
        data.get("integration_intent_id"), "integration_intent_id"
    )
    integration_allocation_id = _optional_uuid(
        data.get("integration_allocation_id"), "integration_allocation_id"
    )
    publication_receipt_id = _optional_uuid(
        data.get("publication_receipt_id"), "publication_receipt_id"
    )
    integration_candidate_oid = _optional_oid(
        data.get("integration_candidate_oid"), "integration_candidate_oid"
    )
    integration_evidence_id = _optional_string(
        data.get("integration_evidence_id"), "integration_evidence_id"
    )
    integration_values = (
        integration_intent_id,
        integration_allocation_id,
        publication_receipt_id,
        integration_candidate_oid,
        integration_mode,
        integration_evidence_id,
    )
    if any(value is not None for value in integration_values) and any(
        value is None for value in integration_values
    ):
        raise ControllerError("INVALID_METADATA", "release intent integration identity is partial")
    history = data.get("history")
    if (
        not isinstance(history, list)
        or not history
        or not all(isinstance(item, dict) for item in history)
    ):
        raise ControllerError("INVALID_METADATA", "release intent history is invalid")
    return ReleaseIntent(
        schema_version=RELEASE_INTENT_SCHEMA_VERSION,
        intent_id=str(identifiers["intent_id"]),
        repository_id=str(identifiers["repository_id"]),
        allocation_id=str(identifiers["allocation_id"]),
        task_key=_nonempty_string(data.get("task_key"), "release intent task_key"),
        authority_key=_nonempty_string(data.get("authority_key"), "release intent authority_key"),
        owner=_nonempty_string(data.get("owner"), "release intent owner"),
        role=role,
        expected_head=_required_oid(data.get("expected_head"), "expected_head"),
        expected_record_version=_positive_int(
            data.get("expected_record_version"), "expected_record_version"
        ),
        task_commit=_required_oid(data.get("task_commit"), "task_commit"),
        handoff_commit=_required_oid(data.get("handoff_commit"), "handoff_commit"),
        finalizer_commit=_required_oid(data.get("finalizer_commit"), "finalizer_commit"),
        finalizer_record_id=_nonempty_string(
            data.get("finalizer_record_id"), "finalizer_record_id"
        ),
        integration_intent_id=integration_intent_id,
        integration_allocation_id=integration_allocation_id,
        publication_receipt_id=publication_receipt_id,
        integration_candidate_oid=integration_candidate_oid,
        integration_mode=integration_mode,
        integration_evidence_id=integration_evidence_id,
        trigger=_nonempty_string(data.get("trigger"), "release intent trigger"),
        status=status,
        blocker=_optional_string(data.get("blocker"), "release intent blocker"),
        created_at=_validate_timestamp(data.get("created_at"), "created_at"),
        updated_at=_validate_timestamp(data.get("updated_at"), "updated_at"),
        expires_at=_validate_timestamp(data.get("expires_at"), "expires_at"),
        history=tuple(dict(item) for item in history),
    )


def release_intent_to_dict(intent: ReleaseIntent) -> dict[str, Any]:
    payload = asdict(intent)
    payload["history"] = [dict(item) for item in intent.history]
    return payload


def release_plan_from_dict(data: Mapping[str, Any]) -> ReleasePlan:
    expected = {
        "schema_version",
        "plan_id",
        "repository_id",
        "allocation_id",
        "release_intent_id",
        "target_path",
        "branch",
        "expected_head",
        "expected_record_version",
        "owner",
        "authority_key",
        "role",
        "lifecycle",
        "trigger",
        "publication_receipt_id",
        "resource_actions",
        "created_at",
    }
    if set(data) != expected or data.get("schema_version") != RELEASE_PLAN_SCHEMA_VERSION:
        raise ControllerError("INVALID_METADATA", "release plan schema is invalid")
    plan_id = _required_sha256(data.get("plan_id"), "plan_id")
    identity = {field: data[field] for field in expected if field != "plan_id"}
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    observed_plan_id = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    if observed_plan_id != plan_id:
        raise ControllerError("INVALID_METADATA", "release plan content hash is invalid")
    identifiers = {
        field: _optional_uuid(data.get(field), field)
        for field in ("repository_id", "allocation_id")
    }
    if any(value is None for value in identifiers.values()):
        raise ControllerError("INVALID_METADATA", "release plan identifiers are required")
    target_path = Path(_nonempty_string(data.get("target_path"), "target_path"))
    if not target_path.is_absolute():
        raise ControllerError("INVALID_METADATA", "release plan target path must be absolute")
    role = _nonempty_string(data.get("role"), "release plan role")
    if role not in WORKTREE_ROLES:
        raise ControllerError("INVALID_METADATA", "release plan role is invalid")
    lifecycle = _nonempty_string(data.get("lifecycle"), "release plan lifecycle")
    if lifecycle not in LIFECYCLE_STATES:
        raise ControllerError("INVALID_METADATA", "release plan lifecycle is invalid")
    raw_actions = data.get("resource_actions")
    if not isinstance(raw_actions, list) or not all(
        isinstance(action, dict) for action in raw_actions
    ):
        raise ControllerError("INVALID_METADATA", "release plan resource actions are invalid")
    actions: list[dict[str, Any]] = []
    for action in raw_actions:
        expected_action = {
            "resource_id",
            "path",
            "kind",
            "creator",
            "teardown_policy",
            "action",
            "evidence",
        }
        if set(action) != expected_action or action.get("action") != "teardown":
            raise ControllerError("INVALID_METADATA", "release plan resource action is invalid")
        resource_id = _optional_uuid(action.get("resource_id"), "resource_id")
        if resource_id is None:
            raise ControllerError("INVALID_METADATA", "release plan resource id is required")
        evidence = action.get("evidence")
        if not isinstance(evidence, dict):
            raise ControllerError("INVALID_METADATA", "release plan resource evidence is invalid")
        actions.append(
            {
                "resource_id": resource_id,
                "path": _nonempty_string(action.get("path"), "resource path"),
                "kind": _nonempty_string(action.get("kind"), "resource kind"),
                "creator": _nonempty_string(action.get("creator"), "resource creator"),
                "teardown_policy": _nonempty_string(
                    action.get("teardown_policy"), "resource teardown policy"
                ),
                "action": "teardown",
                "evidence": dict(evidence),
            }
        )
    return ReleasePlan(
        schema_version=RELEASE_PLAN_SCHEMA_VERSION,
        plan_id=plan_id,
        repository_id=str(identifiers["repository_id"]),
        allocation_id=str(identifiers["allocation_id"]),
        release_intent_id=_optional_uuid(data.get("release_intent_id"), "release_intent_id"),
        target_path=str(target_path),
        branch=_nonempty_string(data.get("branch"), "release plan branch"),
        expected_head=_required_oid(data.get("expected_head"), "expected_head"),
        expected_record_version=_positive_int(
            data.get("expected_record_version"), "expected_record_version"
        ),
        owner=_nonempty_string(data.get("owner"), "release plan owner"),
        authority_key=_nonempty_string(data.get("authority_key"), "release plan authority"),
        role=role,
        lifecycle=lifecycle,
        trigger=_nonempty_string(data.get("trigger"), "release plan trigger"),
        publication_receipt_id=_optional_uuid(
            data.get("publication_receipt_id"), "publication_receipt_id"
        ),
        resource_actions=tuple(actions),
        created_at=_validate_timestamp(data.get("created_at"), "created_at"),
    )


def release_plan_to_dict(plan: ReleasePlan) -> dict[str, Any]:
    payload = asdict(plan)
    payload["resource_actions"] = [dict(action) for action in plan.resource_actions]
    return payload


def release_receipt_from_dict(data: Mapping[str, Any]) -> ReleaseReceipt:
    expected = {
        "schema_version",
        "receipt_id",
        "plan_id",
        "repository_id",
        "allocation_id",
        "release_intent_id",
        "target_path",
        "branch",
        "released_head",
        "expected_record_version",
        "released_record_version",
        "trigger",
        "resource_receipt_ids",
        "result",
        "released_at",
    }
    if set(data) != expected or data.get("schema_version") != RELEASE_RECEIPT_SCHEMA_VERSION:
        raise ControllerError("INVALID_METADATA", "release receipt schema is invalid")
    identifiers = {
        field: _optional_uuid(data.get(field), field)
        for field in ("receipt_id", "repository_id", "allocation_id")
    }
    if any(value is None for value in identifiers.values()):
        raise ControllerError("INVALID_METADATA", "release receipt identifiers are required")
    plan_id = _required_sha256(data.get("plan_id"), "plan_id")
    target_path = Path(_nonempty_string(data.get("target_path"), "target_path"))
    if not target_path.is_absolute():
        raise ControllerError("INVALID_METADATA", "release receipt target path must be absolute")
    result = _nonempty_string(data.get("result"), "release receipt result")
    if result != "RELEASED":
        raise ControllerError("INVALID_METADATA", "release receipt result is invalid")
    resource_receipt_ids = _string_list(data.get("resource_receipt_ids"), "resource_receipt_ids")
    for resource_receipt_id in resource_receipt_ids:
        if _optional_uuid(resource_receipt_id, "resource_receipt_id") is None:
            raise ControllerError("INVALID_METADATA", "resource receipt id is required")
    return ReleaseReceipt(
        schema_version=RELEASE_RECEIPT_SCHEMA_VERSION,
        receipt_id=str(identifiers["receipt_id"]),
        plan_id=plan_id,
        repository_id=str(identifiers["repository_id"]),
        allocation_id=str(identifiers["allocation_id"]),
        release_intent_id=_optional_uuid(data.get("release_intent_id"), "release_intent_id"),
        target_path=str(target_path),
        branch=_nonempty_string(data.get("branch"), "release receipt branch"),
        released_head=_required_oid(data.get("released_head"), "released_head"),
        expected_record_version=_positive_int(
            data.get("expected_record_version"), "expected_record_version"
        ),
        released_record_version=_positive_int(
            data.get("released_record_version"), "released_record_version"
        ),
        trigger=_nonempty_string(data.get("trigger"), "release receipt trigger"),
        resource_receipt_ids=resource_receipt_ids,
        result=result,
        released_at=_validate_timestamp(data.get("released_at"), "released_at"),
    )


def release_receipt_to_dict(receipt: ReleaseReceipt) -> dict[str, Any]:
    payload = asdict(receipt)
    payload["resource_receipt_ids"] = list(receipt.resource_receipt_ids)
    return payload


def reconciliation_receipt_from_dict(data: Mapping[str, Any]) -> ReconciliationReceipt:
    expected = {
        "schema_version",
        "receipt_id",
        "repository_id",
        "source_receipt_id",
        "allocation_id",
        "observed_record_version",
        "observed_record_lifecycle",
        "observed_receipt_status",
        "differences",
        "recovered_intent_id",
        "recovered_intent_state",
        "result",
        "actor",
        "reconciled_at",
    }
    if set(data) != expected or data.get("schema_version") != RECONCILIATION_RECEIPT_SCHEMA_VERSION:
        raise ControllerError("INVALID_METADATA", "reconciliation receipt schema is invalid")
    identifiers = {
        field: _optional_uuid(data.get(field), field)
        for field in ("receipt_id", "repository_id", "source_receipt_id", "allocation_id")
    }
    if any(value is None for value in identifiers.values()):
        raise ControllerError("INVALID_METADATA", "reconciliation receipt identifiers are required")
    result = _nonempty_string(data.get("result"), "reconciliation result")
    if result not in {"RELEASE_STATE_ACKNOWLEDGED", "LEASE_RELEASE_ACKNOWLEDGED"}:
        raise ControllerError("INVALID_METADATA", "reconciliation receipt result is invalid")
    differences = _string_list(data.get("differences"), "differences")
    if not differences:
        raise ControllerError("INVALID_METADATA", "reconciliation differences are required")
    return ReconciliationReceipt(
        schema_version=RECONCILIATION_RECEIPT_SCHEMA_VERSION,
        receipt_id=str(identifiers["receipt_id"]),
        repository_id=str(identifiers["repository_id"]),
        source_receipt_id=str(identifiers["source_receipt_id"]),
        allocation_id=str(identifiers["allocation_id"]),
        observed_record_version=_positive_int(
            data.get("observed_record_version"), "observed_record_version"
        ),
        observed_record_lifecycle=_nonempty_string(
            data.get("observed_record_lifecycle"), "observed_record_lifecycle"
        ),
        observed_receipt_status=_nonempty_string(
            data.get("observed_receipt_status"), "observed_receipt_status"
        ),
        differences=differences,
        recovered_intent_id=_optional_uuid(data.get("recovered_intent_id"), "recovered_intent_id"),
        recovered_intent_state=_optional_string(
            data.get("recovered_intent_state"), "recovered_intent_state"
        ),
        result=result,
        actor=_nonempty_string(data.get("actor"), "reconciliation actor"),
        reconciled_at=_validate_timestamp(data.get("reconciled_at"), "reconciled_at"),
    )


def reconciliation_receipt_to_dict(receipt: ReconciliationReceipt) -> dict[str, Any]:
    payload = asdict(receipt)
    payload["differences"] = list(receipt.differences)
    return payload
