"""Pure queries over the caller's release-intent snapshot.

Callers retain ownership of clocks, locks and all release-intent mutations.
"""

from __future__ import annotations

from datetime import datetime

from .models import MetadataRecord, MetadataSnapshot, ReleaseIntent

RENEWAL_BACKOFF_CAP_SECONDS = 24 * 3600


def release_intent_renewal_due(
    snapshot: MetadataSnapshot,
    previous: ReleaseIntent,
    record: MetadataRecord,
    head: str,
    now: datetime,
    ttl_seconds: int,
) -> bool:
    """Whether a stale release intent should be renewed on this pass.

    The first expiry, or a changed record or HEAD, is renewed at once. Each further
    stale intent of an unchanged identity failed the same way, so the next renewal
    waits the TTL doubled per repeat, capped at a day. Renewal stays eventual (a stuck
    checkout is still retried) while one blocked allocation no longer adds an intent
    on every reconciliation pass.
    """
    if (previous.expected_record_version, previous.expected_head) != (record.record_version, head):
        return True
    streak = sum(
        1
        for intent in release_intents_for_allocation(snapshot, record.allocation_id)
        if intent.status == "STALE"
        and (intent.expected_record_version, intent.expected_head) == (record.record_version, head)
    )
    if streak <= 1:
        return True
    delay = min(ttl_seconds * 2 ** (streak - 2), RENEWAL_BACKOFF_CAP_SECONDS)
    return (now - datetime.fromisoformat(previous.updated_at)).total_seconds() >= delay


def release_intents_for_allocation(
    snapshot: MetadataSnapshot, allocation_id: str
) -> tuple[ReleaseIntent, ...]:
    return tuple(
        sorted(
            (
                intent
                for intent in snapshot.release_intents.values()
                if intent.allocation_id == allocation_id
            ),
            key=lambda intent: (intent.created_at, intent.intent_id),
        )
    )
