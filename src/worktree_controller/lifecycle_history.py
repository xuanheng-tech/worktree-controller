"""Read-only projections of existing allocation and immutable release evidence."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .controller import envelope
from .git_backend import canonical_checkout, inventory, resolve_repository
from .models import (
    ControllerError,
    MetadataRecord,
    MetadataSnapshot,
    PreparedCandidateReceipt,
    PublicationReceipt,
    ReleaseReceipt,
)
from .store import MetadataStore

CONTRACT = "closed-lifecycle/v1"
QUERY_VERSION = 1


def _conflict(message: str) -> None:
    raise ControllerError("HISTORY_EVIDENCE_CONFLICT", message)


@dataclass(frozen=True)
class _DeliveryIndex:
    candidates: dict[str, list[PublicationReceipt]]
    sources: dict[tuple[str, str], list[PublicationReceipt]]
    preparations: dict[tuple[str, str, str, str], list[PreparedCandidateReceipt]]


def _delivery_index(snapshot: MetadataSnapshot) -> _DeliveryIndex:
    candidates: dict[str, list[PublicationReceipt]] = defaultdict(list)
    sources: dict[tuple[str, str], list[PublicationReceipt]] = defaultdict(list)
    preparations: dict[tuple[str, str, str, str], list[PreparedCandidateReceipt]] = defaultdict(list)
    for publication in sorted(snapshot.publication_receipts.values(), key=lambda p: p.receipt_id):
        candidates[publication.allocation_id].append(publication)
        for identity in {
            (result["source_allocation_id"], result["source_handoff_oid"])
            for result in publication.source_results
        }:
            sources[identity].append(publication)
    for prepared in sorted(snapshot.prepared_candidate_receipts.values(), key=lambda p: p.receipt_id):
        preparations[
            (
                prepared.intent_id,
                prepared.allocation_id,
                prepared.candidate_oid,
                prepared.validation_evidence_id,
            )
        ].append(prepared)
    return _DeliveryIndex(dict(candidates), dict(sources), dict(preparations))


def _delivery(
    snapshot: MetadataSnapshot,
    receipt: ReleaseReceipt,
    *,
    index: _DeliveryIndex | None = None,
    project: bool = True,
) -> list[dict[str, Any]]:
    index = index if index is not None else _delivery_index(snapshot)
    publications = {
        p.receipt_id: p
        for p in index.candidates.get(receipt.allocation_id, ())
    }
    publications.update(
        (p.receipt_id, p)
        for p in index.sources.get((receipt.allocation_id, receipt.released_head), ())
    )
    deliveries = []
    for publication in sorted(publications.values(), key=lambda p: p.receipt_id):
        direct = publication.allocation_id == receipt.allocation_id
        source_evidence = [
            result
            for result in publication.source_results
            if result["source_allocation_id"] == receipt.allocation_id
            and result["source_handoff_oid"] == receipt.released_head
        ]
        source = bool(source_evidence)
        if not direct and not source:
            continue
        if publication.repository_id != receipt.repository_id:
            _conflict("publication receipt belongs to another repository")
        if direct and publication.candidate_oid != receipt.released_head:
            _conflict("released integration HEAD differs from its publication receipt")
        prepared_receipts = index.preparations.get(
            (
                publication.intent_id,
                publication.allocation_id,
                publication.candidate_oid,
                publication.validation_evidence_id,
            ),
            [],
        )
        # Keep every matching immutable preparation; ambiguous evidence is never guessed.
        for prepared in prepared_receipts:
            if prepared.repository_id != receipt.repository_id:
                _conflict("prepared receipt belongs to another repository")
        if not project:
            continue
        prepared = prepared_receipts[0] if len(prepared_receipts) == 1 else None
        deliveries.append(
            {
                "integration_intent_id": publication.intent_id,
                "integration_allocation_id": publication.allocation_id,
                "relationship": "candidate" if direct else "source",
                "source_absorption_evidence": source_evidence,
                "candidate_oid": publication.candidate_oid,
                "publication_receipt_id": publication.receipt_id,
                "publication_receipt": asdict(publication),
                "publication_status": publication.status,
                "published_at": publication.remote_verified_at,
                "publisher": publication.holder,
                "validation_evidence_id": publication.validation_evidence_id,
                "prepared_candidate_receipt": asdict(prepared) if prepared else None,
                "prepared_candidate_receipts": [asdict(p) for p in prepared_receipts],
                "finalizer_record_id": publication.finalizer_record_id,
            }
        )
    return deliveries


def _release_view(
    snapshot: MetadataSnapshot,
    receipt: ReleaseReceipt,
    record: MetadataRecord | None,
    *,
    index: _DeliveryIndex | None = None,
    project: bool = True,
) -> dict[str, Any]:
    plan = snapshot.release_plans.get(receipt.plan_id)
    if receipt.repository_id != snapshot.repository_id:
        _conflict("release receipt belongs to another repository")
    if plan is None:
        raise ControllerError("HISTORY_EVIDENCE_INCOMPLETE", "release receipt has no release plan")
    for field, expected in {
        "repository_id": receipt.repository_id,
        "allocation_id": receipt.allocation_id,
        "release_intent_id": receipt.release_intent_id,
        "target_path": receipt.target_path,
        "branch": receipt.branch,
        "expected_head": receipt.released_head,
        "expected_record_version": receipt.expected_record_version,
        "trigger": receipt.trigger,
    }.items():
        if getattr(plan, field) != expected:
            _conflict(f"release plan and receipt disagree on {field}")
    release_intent = snapshot.release_intents.get(receipt.release_intent_id or "")
    if release_intent is not None and (
        release_intent.repository_id != receipt.repository_id
        or release_intent.allocation_id != receipt.allocation_id
        or release_intent.owner != plan.owner
        or release_intent.authority_key != plan.authority_key
    ):
        _conflict("release intent identity differs from its release plan")
    if record is not None and (
        record.record_version != receipt.released_record_version
        or record.lifecycle != "RELEASED"
        or record.authority_key != plan.authority_key
        or (release_intent is not None and record.task_key != release_intent.task_key)
    ):
        _conflict("closed allocation identity differs from its release receipt")
    deliveries = _delivery(snapshot, receipt, index=index, project=project)
    if not project:
        return {}
    single = deliveries[0] if len(deliveries) == 1 else None
    task_key = release_intent.task_key if release_intent else record.task_key if record else None
    return {
        "boundary": "CLOSED",
        "allocation_id": receipt.allocation_id,
        "authority_key": plan.authority_key,
        "task_key": task_key,
        "owner": plan.owner,
        "role": plan.role,
        "branch": receipt.branch,
        "worktree_path": receipt.target_path,
        "released_head": receipt.released_head,
        "candidate_oid": single["candidate_oid"] if single else None,
        "integration_intent_id": single["integration_intent_id"] if single else None,
        "release_status": receipt.result,
        "published_at": single["published_at"] if single else None,
        "released_at": receipt.released_at,
        "release_receipt_id": receipt.receipt_id,
        "release_plan_id": receipt.plan_id,
        "release_intent_id": receipt.release_intent_id,
        "validation_evidence_id": single["validation_evidence_id"] if single else None,
        "finalizer_record_id": single["finalizer_record_id"] if single else None,
        "source_finalizer_record_id": record.finalizer_record_id if record else None,
        "source_finalizer_commit": record.finalizer_commit if record else None,
        "deliveries": deliveries,
        "proof_status": "COMPLETE" if task_key else "INCOMPLETE",
        "evidence_sources": {
            "identity": "release-plans",
            "task": "release-intents" if release_intent else "records" if record else None,
            "release": "release-receipts",
            "publication": "publication-receipts" if deliveries else None,
            "validation": "prepared-candidate-receipts"
            if any(d["prepared_candidate_receipts"] for d in deliveries)
            else None,
        },
    }


def lifecycle_history(
    repository_path: Path,
    *,
    allocation_id: str | None = None,
    authority_key: str | None = None,
    task_key: str | None = None,
    integration_intent_id: str | None = None,
    release_receipt_id: str | None = None,
    limit: int = 100,
    after: str | None = None,
) -> dict[str, Any]:
    """Query existing evidence without new records, remote access, or lifecycle mutation.

    Closed entries are keyed by immutable release receipt id; live entries are a separate
    observation and cannot prove historical ownership or authorize final_result.
    """
    query = {
        "allocation_id": allocation_id,
        "authority_key": authority_key,
        "task_key": task_key,
        "integration_intent_id": integration_intent_id,
        "release_receipt_id": release_receipt_id,
    }
    if sum(value is not None for value in query.values()) != 1:
        raise ControllerError("INVALID_HISTORY_QUERY", "exactly one history selector is required")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 1000
        or any(
            value is not None and (not value.strip() or "\x00" in value) for value in query.values()
        )
        or (after is not None and (not after.strip() or "\x00" in after))
    ):
        raise ControllerError("INVALID_HISTORY_QUERY", "invalid selector or limit (1..1000)")
    _, common = resolve_repository(repository_path)
    store = MetadataStore(common)
    with store.lock(exclusive=False):
        snapshot = store._load_unlocked()
        wanted = None
        if integration_intent_id is not None:
            intent = snapshot.integration_intents.get(integration_intent_id)
            wanted = set(intent.source_allocations) | {intent.allocation_id} if intent else set()
            for publication in snapshot.publication_receipts.values():
                if publication.intent_id == integration_intent_id:
                    wanted.add(publication.allocation_id)
                    wanted.update(r["source_allocation_id"] for r in publication.source_results)
        if release_receipt_id is not None:
            receipt = snapshot.release_receipts.get(release_receipt_id)
            wanted = {receipt.allocation_id} if receipt else set()

        def matches(identity: dict[str, Any]) -> bool:
            return (
                (wanted is None or identity["allocation_id"] in wanted)
                and (allocation_id is None or identity["allocation_id"] == allocation_id)
                and (authority_key is None or identity["authority_key"] == authority_key)
                and (task_key is None or identity["task_key"] == task_key)
            )

        # A receipt query has no live projection; other selectors only need Git
        # inventory when a matching record is still bound to a checkout. Keep the
        # full locked metadata validation even for entirely closed history.
        bound_records = (
            snapshot.records.get(allocation) for allocation in set(snapshot.bindings.values())
        )
        needs_live_inventory = release_receipt_id is None and any(
            record is not None and matches(asdict(record)) for record in bound_records
        )
        if needs_live_inventory:
            repository = inventory(repository_path)
            canonical_path = repository.canonical_path
            worktrees = repository.worktrees
        else:
            canonical_path = canonical_checkout(repository_path)
            worktrees = ()

        closed = []
        matching_count = 0
        index = None
        for receipt in sorted(snapshot.release_receipts.values(), key=lambda r: r.receipt_id):
            if release_receipt_id is not None and receipt.receipt_id != release_receipt_id:
                continue
            record = snapshot.records.get(receipt.allocation_id)
            plan = snapshot.release_plans.get(receipt.plan_id)
            release_intent = snapshot.release_intents.get(receipt.release_intent_id or "")
            identity = {
                "allocation_id": receipt.allocation_id,
                "authority_key": plan.authority_key
                if plan
                else record.authority_key
                if record
                else None,
                "task_key": release_intent.task_key
                if release_intent
                else record.task_key
                if record
                else None,
            }
            if matches(identity) and (after is None or receipt.receipt_id > after):
                matching_count += 1
                if index is None:
                    index = _delivery_index(snapshot)
                project = matching_count <= limit
                # Validate every match, including those beyond this page, without
                # serializing their complete evidence into discarded documents.
                row = _release_view(snapshot, receipt, record, index=index, project=project)
                if project:
                    closed.append(row)
        live = []
        if release_receipt_id is None:
            for item in worktrees:
                record = snapshot.records.get(snapshot.bindings.get(item.key, ""))
                if record is not None and matches(asdict(record)):
                    live.append(
                        {
                            "boundary": "LIVE",
                            "allocation_id": record.allocation_id,
                            "authority_key": record.authority_key,
                            "task_key": record.task_key,
                            "owner": record.owner,
                            "lifecycle": record.lifecycle,
                            "branch": item.branch,
                            "worktree_path": str(item.path),
                            "head": item.head,
                        }
                    )
        receipted = {r.allocation_id for r in snapshot.release_receipts.values()}
        incomplete = (
            [
                {
                    "allocation_id": r.allocation_id,
                    "authority_key": r.authority_key,
                    "task_key": r.task_key,
                    "recorded_lifecycle": r.lifecycle,
                    "proof_status": "INCOMPLETE",
                    "reason": "no formal release receipt",
                }
                for r in sorted(snapshot.records.values(), key=lambda r: r.allocation_id)
                if r.lifecycle == "RELEASED"
                and r.allocation_id not in receipted
                and matches(asdict(r))
            ]
            if release_receipt_id is None
            else []
        )
    payload = envelope("lifecycle-history")
    payload["repository"] = {
        "repository_id": snapshot.repository_id,
        "canonical_path": str(canonical_path),
        "common_dir": str(common),
        "metadata_path": str(store.root),
    }
    payload["warnings"] = list(snapshot.warnings)
    payload["decision"] = {
        "code": "LIFECYCLE_HISTORY",
        "contract": CONTRACT,
        "query_version": QUERY_VERSION,
        "query": query,
        "live_allocations": sorted(live, key=lambda row: row["allocation_id"]),
        "closed_releases": closed,
        "incomplete_history": incomplete,
        "next_after": closed[-1]["release_receipt_id"] if matching_count > limit else None,
        "mutation_performed": False,
    }
    return payload
