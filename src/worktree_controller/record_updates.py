"""Versioned, history-preserving updates of allocation records and integration intents."""

from __future__ import annotations

from typing import Any

from .metadata_codec import (
    integration_intent_from_dict,
    integration_intent_to_dict,
    record_from_dict,
    record_to_dict,
)
from .models import IntegrationIntent, MetadataRecord
from .store import now_iso


def updated_record(record: MetadataRecord, event: str, **changes: Any) -> MetadataRecord:
    payload = record_to_dict(record)
    before = record.lifecycle
    timestamp = now_iso()
    payload.update(changes)
    payload["record_version"] = record.record_version + 1
    payload["last_reviewed_at"] = timestamp
    history = list(record.history)
    history.append(
        {
            "at": timestamp,
            "event": event,
            "from": before,
            "to": payload.get("lifecycle", before),
            "owner": payload.get("owner", record.owner),
        }
    )
    payload["history"] = history
    return record_from_dict(payload)


def updated_intent(intent: IntegrationIntent, event: str, **changes: Any) -> IntegrationIntent:
    payload = integration_intent_to_dict(intent)
    timestamp = now_iso()
    payload.update(changes)
    payload["updated_at"] = timestamp
    history = list(intent.history)
    history.append(
        {
            "at": timestamp,
            "event": event,
            "from": intent.state,
            "to": payload.get("state", intent.state),
            "candidate_oid": payload.get("candidate_oid"),
        }
    )
    payload["history"] = history
    return integration_intent_from_dict(payload)
