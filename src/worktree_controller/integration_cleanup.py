"""Preserved cleanup authority for an explicitly aborted, unpublished integration."""

from __future__ import annotations

import uuid

from . import cleanup_evidence
from .models import ControllerError

CONTRACT = "aborted-integration-preserved-cleanup/v1"


def _resource_tail_valid(record, item, position: int) -> bool:
    """Verify terminal resource-only transitions without reissuing an abort.

    Legacy producers recorded no per-event resource payload. Their contract only
    allowed one .venv registration for the lifetime of an allocation. Accept that
    unambiguous history, never infer a resource from an arbitrary event name.
    """
    tail = record.history[position + 1 :]
    resource_events = {"DISPOSABLE_REGISTERED", "DISPOSABLE_CLEANED"}
    state = "INTEGRATION_ABORTED"
    for row in tail:
        if row.get("owner") != record.owner or row.get("from") != state:
            return False
        if row.get("event") == "RELEASED" and state == "INTEGRATION_ABORTED":
            state = "RELEASED"
        elif row.get("event") not in resource_events or state != "INTEGRATION_ABORTED":
            return False
        if row.get("to") != state:
            return False
    if state != record.lifecycle:
        return False
    if not any(row.get("event") in resource_events for row in tail):
        return True
    if len(record.disposable_resources) != 1:
        return False
    resource = record.disposable_resources[0]
    try:
        if str(uuid.UUID(resource["resource_id"])) != resource["resource_id"]:
            return False
    except (ValueError, KeyError, TypeError, AttributeError):
        return False
    if (
        resource.get("path") != ".venv"
        or resource.get("kind") != "python-venv"
        or not isinstance(resource.get("reproducible"), bool)
        or not isinstance(resource.get("creator"), str)
        or not resource["creator"].strip()
        or resource.get("teardown_policy") not in {"manual", "on-release"}
        or (resource["teardown_policy"] == "on-release" and not resource["reproducible"])
    ):
        return False
    transitions = [row for row in record.history if row.get("event") in resource_events]
    expected = ["DISPOSABLE_REGISTERED"]
    if resource.get("state") == "cleaned":
        expected.append("DISPOSABLE_CLEANED")
    elif resource.get("state") != "registered":
        return False
    if [row["event"] for row in transitions] != expected:
        return False
    for row in transitions:
        if row.get("owner") != record.owner or row.get("from") != row.get("to"):
            return False
        binding = row.get("disposable_resource")
        if binding is not None:
            expected_resource = dict(resource)
            expected_resource["state"] = (
                "registered" if row["event"] == "DISPOSABLE_REGISTERED" else "cleaned"
            )
            if binding != expected_resource:
                return False
    absolute = item.path / ".venv"
    if resource["state"] == "cleaned":
        return not absolute.exists() and not absolute.is_symlink()
    marker = absolute / "pyvenv.cfg"
    return (
        absolute.is_dir()
        and not absolute.is_symlink()
        and not absolute.is_mount()
        and marker.is_file()
        and not marker.is_symlink()
        and not (absolute / ".git").exists()
    )


def abort_evidence(store, snapshot, item, record) -> dict[str, str]:
    """Bind cleanup to the producer's abort, never cleanliness or a stale source head.

    Old abort receipts remain usable when exactly one matches. New aborts pin the
    receipt's raw bytes in their history event, including repeated aborts after
    quarantine. Neither path grants an admission cancellation or publication.
    """

    def invalid():
        return ControllerError(
            "INTEGRATION_ABORT_EVIDENCE_INVALID",
            "one exact unpublished integration abort and retained candidate are required",
        )

    if record.role != "integration" or record.lifecycle not in {"INTEGRATION_ABORTED", "RELEASED"}:
        raise invalid()
    events = [
        i for i, event in enumerate(record.history) if event.get("event") == "INTEGRATION_ABORTED"
    ]
    if not events:
        raise invalid()
    position = events[-1]
    event = record.history[position]
    if (
        event.get("owner") != record.owner
        or event.get("to") != "INTEGRATION_ABORTED"
        or not _resource_tail_valid(record, item, position)
    ):
        raise invalid()
    if record.quarantine_observed_commit and record.quarantine_observed_commit != item.head:
        raise invalid()
    if any(
        receipt.allocation_id == record.allocation_id
        for receipt in snapshot.publication_receipts.values()
    ):
        raise invalid()
    if any(
        intent.allocation_id == record.allocation_id
        for intent in snapshot.integration_intents.values()
    ):
        raise invalid()
    expected = {
        "kind": "integration_abort",
        "schema_version": 1,
        "repository_id": snapshot.repository_id,
        "allocation_id": record.allocation_id,
        "task_key": record.task_key,
        "authority_key": record.authority_key,
        "owner": record.owner,
        "branch": item.branch,
        "candidate_oid": item.head,
    }
    try:
        bound = event.get("abort_receipt")
        if bound is not None:
            receipt_id = str(uuid.UUID(bound["receipt_id"]))
            if receipt_id != bound["receipt_id"]:
                raise invalid()
            path = store.completion_receipts_dir / f"{receipt_id}.json"
            value, digest = cleanup_evidence.read(path, bound["sha256"])
        else:
            matches = []
            for path in sorted(store.completion_receipts_dir.glob("*.json")):
                value, digest = cleanup_evidence.read(path)
                if all(value.get(key) == wanted for key, wanted in expected.items()):
                    matches.append((path, value, digest))
            if len(matches) != 1:
                raise invalid()
            path, value, digest = matches[0]
            receipt_id = path.stem
        if (
            any(value.get(key) != wanted for key, wanted in expected.items())
            or value.get("receipt_id") != receipt_id
            or str(uuid.UUID(receipt_id)) != receipt_id
            or not value.get("failure_reason")
        ):
            raise invalid()
    except (OSError, ValueError, KeyError, TypeError, ControllerError) as exc:
        raise invalid() from exc
    return {"contract": CONTRACT, "receipt_id": receipt_id, "path": str(path), "sha256": digest}
