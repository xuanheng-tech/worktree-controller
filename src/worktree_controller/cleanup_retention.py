"""Safe, fail-closed retention and pruning for terminal RELEASED cleanup evidence.

Typed and preserved disposable cleanups record pre/post hardlink topologies
(``cleanup-actions/<tx>/`` and ``completion-receipts/<tx>.json``) so an
interrupted cleanup can be verified and recovered. Once the owning allocation
has reached terminal ``RELEASED`` with an exact matching ``ReleaseReceipt``,
no live checkout or active lease/intent remains, and no retained transaction
journal or recovery record references that evidence, those multi-megabyte
topology artifacts are no longer required by any runtime or recovery path.

Pruning ALWAYS removes ``cleanup-actions/<id>/`` before removing
``completion-receipts/<id>.json`` so ``cleanup_attempts.no_action_evidence``
never observes an unbound cleanup-actions directory.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import stat
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .git_backend import inventory
from .models import ControllerError, MetadataSnapshot, RepositoryInventory
from .semantics import effective_intent_state
from .store import MetadataStore

PRUNABLE_CLEANUP_CONTRACTS = frozenset(
    {
        "typed-disposable-resource/v1",
        "legacy-preserved-state-cleanup/v1",
        "legacy-preserved-state-multiple-cleanup/v1",
    }
)
TERMINAL_INTENT_STATES = frozenset({"RELEASED", "ABANDONED"})
DEFAULT_AUTO_KEEP_RECENT = 5
DEFAULT_AUTO_MAX_BYTES = 64 * 1024 * 1024
_MAX_DECOMPRESSED_RECEIPT_BYTES = 64 * 1024 * 1024


def _load_json_bytes(raw: bytes) -> Any:
    if raw.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as stream:
            data = stream.read(_MAX_DECOMPRESSED_RECEIPT_BYTES + 1)
        if len(data) > _MAX_DECOMPRESSED_RECEIPT_BYTES:
            raise ValueError("compressed receipt exceeds its bound")
        return json.loads(data)
    return json.loads(raw)


@dataclass(frozen=True)
class PrunableCleanupEntry:
    receipt_id: str
    allocation_id: str
    contract: str
    receipt_path: Path
    receipt_bytes: int
    action_dir: Path | None
    action_files: int
    action_bytes: int
    timestamp: str

    @property
    def total_bytes(self) -> int:
        return self.receipt_bytes + self.action_bytes


def _is_canonical_uuid(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _protected_receipt_ids(store: MetadataStore) -> set[str]:
    """Collect every receipt/transaction id referenced by retained journals or recoveries."""
    protected: set[str] = set()
    if store.transactions_dir.is_dir():
        for entry in store.transactions_dir.iterdir():
            if entry.name.endswith(".json"):
                protected.add(entry.stem)
    if store.cleanup_recoveries_dir.is_dir():
        for tx_dir in store.cleanup_recoveries_dir.iterdir():
            if not tx_dir.is_dir() or tx_dir.is_symlink():
                continue
            protected.add(tx_dir.name)
            for candidate in tx_dir.rglob("*.json"):
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                try:
                    doc = _load_json_bytes(candidate.read_bytes())
                except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                    # Fail closed: unreadable recovery evidence protects its subtree.
                    protected.add(candidate.stem)
                    continue
                if not isinstance(doc, dict):
                    continue
                for key in ("completion_receipt_id", "original_transaction_id", "transaction_id"):
                    val = doc.get(key)
                    if isinstance(val, str) and val:
                        protected.add(val)
                ref = doc.get("refusal_receipt")
                if isinstance(ref, dict) and isinstance(ref.get("path"), str):
                    protected.add(Path(ref["path"]).stem)
    return protected


def _non_terminal_intent_allocations(snapshot: MetadataSnapshot) -> set[str]:
    active: set[str] = set()
    for intent in snapshot.integration_intents.values():
        if effective_intent_state(snapshot, intent) not in TERMINAL_INTENT_STATES:
            if intent.allocation_id:
                active.add(intent.allocation_id)
            active.update(intent.source_allocations)
    return active


def _measure_action_dir(action_dir: Path) -> tuple[int, int] | None:
    """Measure regular files inside a cleanup-actions/<id> directory; return None if unsafe."""
    try:
        info = os.lstat(action_dir)
    except FileNotFoundError:
        return 0, 0
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return None
    files = 0
    total = 0
    try:
        for entry in os.scandir(action_dir):
            st = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                return None
            files += 1
            total += st.st_size
    except OSError:
        return None
    return files, total


def _remove_action_dir_safe(action_dir: Path) -> tuple[int, int]:
    """Safely remove regular files in cleanup-actions/<id>/ and rmdir it."""
    removed_files = 0
    removed_bytes = 0
    try:
        info = os.lstat(action_dir)
    except FileNotFoundError:
        return 0, 0
    if not stat.S_ISDIR(info.st_mode):
        raise ControllerError(
            "CLEANUP_RETENTION_UNSAFE", f"cleanup action entry is not a directory: {action_dir}"
        )
    entries = list(os.scandir(action_dir))
    for entry in entries:
        st = entry.stat(follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode):
            raise ControllerError(
                "CLEANUP_RETENTION_UNSAFE", f"unexpected non-regular cleanup file: {entry.path}"
            )
    for entry in entries:
        st = entry.stat(follow_symlinks=False)
        os.unlink(entry.path)
        removed_files += 1
        removed_bytes += st.st_size
    os.rmdir(action_dir)
    _fsync_dir(action_dir.parent)
    return removed_files, removed_bytes


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def plan_terminal_cleanup_retention_locked(
    repository: RepositoryInventory,
    store: MetadataStore,
    snapshot: MetadataSnapshot,
    *,
    keep_recent: int = 0,
    older_than_days: float = 0.0,
    max_total_bytes: int | None = None,
    keep_allocations: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Identify strictly proven terminal RELEASED cleanup evidence eligible for pruning."""
    if keep_recent < 0 or older_than_days < 0:
        raise ControllerError(
            "INVALID_REQUEST", "keep_recent and older_than_days must be non-negative"
        )
    if max_total_bytes is not None and max_total_bytes < 0:
        raise ControllerError("INVALID_REQUEST", "max_total_bytes must be non-negative")

    protected_ids = _protected_receipt_ids(store)
    active_intent_allocs = _non_terminal_intent_allocations(snapshot)
    bound_allocs = set(snapshot.bindings.values())
    leased_allocs = set(snapshot.writer_leases)
    pub_alloc = snapshot.publication_lease.allocation_id if snapshot.publication_lease else None
    live_worktree_paths = {str(item.path.resolve(strict=False)) for item in repository.worktrees}

    release_receipts_by_alloc: dict[str, list[Any]] = {}
    for rr in snapshot.release_receipts.values():
        if rr.result == "RELEASED":
            release_receipts_by_alloc.setdefault(rr.allocation_id, []).append(rr)

    candidates: list[PrunableCleanupEntry] = []
    skipped: dict[str, int] = {}
    scanned_receipts = 0

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    if store.completion_receipts_dir.is_dir():
        for entry in sorted(os.scandir(store.completion_receipts_dir), key=lambda e: e.name):
            if not entry.name.endswith(".json") or entry.name.startswith("."):
                continue
            scanned_receipts += 1
            path = Path(entry.path)
            receipt_id = path.stem
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                skip("unreadable")
                continue
            if not stat.S_ISREG(st.st_mode):
                skip("non_regular")
                continue
            if not _is_canonical_uuid(receipt_id):
                skip("non_uuid")
                continue
            if receipt_id in protected_ids:
                skip("referenced_by_transaction_or_recovery")
                continue
            try:
                doc = _load_json_bytes(path.read_bytes())
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                skip("unreadable")
                continue
            if not isinstance(doc, dict):
                skip("invalid_document")
                continue
            contract = doc.get("contract")
            if contract not in PRUNABLE_CLEANUP_CONTRACTS:
                skip("non_prunable_contract")
                continue
            if doc.get("receipt_id") != receipt_id:
                skip("receipt_id_mismatch")
                continue
            plan_obj = doc.get("plan") if isinstance(doc.get("plan"), dict) else {}
            allocation_id = doc.get("allocation_id") or plan_obj.get("allocation_id")
            if not _is_canonical_uuid(allocation_id):
                skip("missing_allocation_id")
                continue
            assert isinstance(allocation_id, str)
            if allocation_id in keep_allocations:
                skip("explicitly_kept_allocation")
                continue
            record = snapshot.records.get(allocation_id)
            if record is None or record.lifecycle != "RELEASED":
                skip("allocation_not_released")
                continue
            if (
                allocation_id in bound_allocs
                or allocation_id in leased_allocs
                or allocation_id == pub_alloc
                or allocation_id in active_intent_allocs
            ):
                skip("allocation_still_referenced")
                continue
            matching_releases = [
                rr
                for rr in release_receipts_by_alloc.get(allocation_id, ())
                if rr.released_record_version == record.record_version
            ]
            if len(matching_releases) != 1:
                skip("missing_exact_release_receipt")
                continue
            target_path = plan_obj.get("target_path")
            if isinstance(target_path, str) and target_path:
                resolved_target = str(Path(target_path).resolve(strict=False))
                if resolved_target in live_worktree_paths or os.path.lexists(target_path):
                    skip("target_still_present")
                    continue
            action_dir = store.cleanup_actions_dir / receipt_id
            action_info = _measure_action_dir(action_dir)
            if action_info is None:
                skip("unsafe_action_dir")
                continue
            action_files, action_bytes = action_info
            timestamp = str(matching_releases[0].released_at or doc.get("created_at") or "")
            candidates.append(
                PrunableCleanupEntry(
                    receipt_id=receipt_id,
                    allocation_id=allocation_id,
                    contract=str(contract),
                    receipt_path=path,
                    receipt_bytes=st.st_size,
                    action_dir=action_dir if action_dir.exists() else None,
                    action_files=action_files,
                    action_bytes=action_bytes,
                    timestamp=timestamp,
                )
            )

    # Sort newest-first so retention windows always preserve the freshest receipts.
    candidates.sort(key=lambda item: (item.timestamp, item.receipt_id), reverse=True)
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days) if older_than_days > 0 else None

    eligible: list[PrunableCleanupEntry] = []
    kept: list[PrunableCleanupEntry] = []
    kept_bytes = 0
    for item in candidates:
        if cutoff is not None:
            ts = _parse_iso(item.timestamp)
            if ts is None or ts > cutoff:
                kept.append(item)
                kept_bytes += item.total_bytes
                continue
        if len(kept) < keep_recent and (
            max_total_bytes is None or kept_bytes + item.total_bytes <= max_total_bytes
        ):
            kept.append(item)
            kept_bytes += item.total_bytes
            continue
        eligible.append(item)

    return {
        "scanned_receipts": scanned_receipts,
        "protected_ids_count": len(protected_ids),
        "skipped": dict(sorted(skipped.items())),
        "kept": kept,
        "kept_bytes": kept_bytes,
        "eligible": eligible,
        "eligible_receipts": len(eligible),
        "eligible_action_dirs": sum(1 for item in eligible if item.action_dir is not None),
        "eligible_action_files": sum(item.action_files for item in eligible),
        "eligible_receipt_bytes": sum(item.receipt_bytes for item in eligible),
        "eligible_action_bytes": sum(item.action_bytes for item in eligible),
        "eligible_total_bytes": sum(item.total_bytes for item in eligible),
    }


def apply_terminal_cleanup_plan_locked(plan: dict[str, Any]) -> dict[str, Any]:
    """Execute a verified retention plan while the caller holds exclusive repo lock.

    Removes ``cleanup-actions/<id>/`` strictly before ``completion-receipts/<id>.json``
    so no orphaned cleanup-actions directory can ever exist.
    """
    pruned_receipts = 0
    pruned_action_dirs = 0
    pruned_action_files = 0
    reclaimed_action_bytes = 0
    reclaimed_receipt_bytes = 0
    receipt_parent: Path | None = None

    for item in plan["eligible"]:
        assert isinstance(item, PrunableCleanupEntry)
        if item.action_dir is not None and item.action_dir.exists():
            files_removed, bytes_removed = _remove_action_dir_safe(item.action_dir)
            pruned_action_dirs += 1
            pruned_action_files += files_removed
            reclaimed_action_bytes += bytes_removed
        with suppress(FileNotFoundError):
            os.unlink(item.receipt_path)
            pruned_receipts += 1
            reclaimed_receipt_bytes += item.receipt_bytes
            receipt_parent = item.receipt_path.parent
    if receipt_parent is not None:
        _fsync_dir(receipt_parent)

    return {
        "pruned_receipts": pruned_receipts,
        "pruned_action_dirs": pruned_action_dirs,
        "pruned_action_files": pruned_action_files,
        "reclaimed_action_bytes": reclaimed_action_bytes,
        "reclaimed_receipt_bytes": reclaimed_receipt_bytes,
        "bytes_reclaimed": reclaimed_action_bytes + reclaimed_receipt_bytes,
    }


def auto_prune_terminal_cleanup_locked(
    repository: RepositoryInventory,
    store: MetadataStore,
    snapshot: MetadataSnapshot,
    *,
    keep_allocations: frozenset[str] = frozenset(),
    keep_recent: int | None = None,
    max_total_bytes: int | None = None,
) -> dict[str, Any]:
    """Bound historical RELEASED cleanup bloat automatically under an already-held exclusive lock."""
    if not store.completion_receipts_dir.is_dir():
        return {"pruned_receipts": 0, "pruned_action_dirs": 0, "bytes_reclaimed": 0}
    effective_keep_recent = DEFAULT_AUTO_KEEP_RECENT if keep_recent is None else keep_recent
    effective_max_bytes = DEFAULT_AUTO_MAX_BYTES if max_total_bytes is None else max_total_bytes
    plan = plan_terminal_cleanup_retention_locked(
        repository,
        store,
        snapshot,
        keep_recent=effective_keep_recent,
        max_total_bytes=effective_max_bytes,
        keep_allocations=keep_allocations,
    )
    if not plan["eligible"]:
        return {"pruned_receipts": 0, "pruned_action_dirs": 0, "bytes_reclaimed": 0}
    return apply_terminal_cleanup_plan_locked(plan)


def reconcile_cleanup_evidence(
    repository_path: Path,
    *,
    apply: bool = False,
    keep_recent: int = 0,
    older_than_days: float = 0.0,
    max_total_bytes: int | None = None,
) -> dict[str, Any]:
    """Dry-run plan or apply pruning of terminal RELEASED cleanup-actions & completion-receipts."""
    from .controller import compact_status

    repository = inventory(repository_path, observe_paths=frozenset())
    store = MetadataStore(repository.common_dir)
    with store.lock(exclusive=apply):
        live_repo = inventory(repository.canonical_path, observe_paths=frozenset())
        snapshot = store._load_unlocked()
        plan = plan_terminal_cleanup_retention_locked(
            live_repo,
            store,
            snapshot,
            keep_recent=keep_recent,
            older_than_days=older_than_days,
            max_total_bytes=max_total_bytes,
        )
        applied = apply_terminal_cleanup_plan_locked(plan) if apply else None

    payload = compact_status(repository.canonical_path, command="reconcile")
    payload["decision"] = {
        "code": "CLEANUP_EVIDENCE_PRUNED" if apply else "CLEANUP_EVIDENCE_PLAN",
        "reason": (
            "verified terminal RELEASED cleanup evidence pruned"
            if apply
            else "verified terminal RELEASED cleanup evidence plan (dry-run)"
        ),
        "apply": apply,
        "dry_run": not apply,
        "policy": {
            "keep_recent": keep_recent,
            "older_than_days": older_than_days,
            "max_total_bytes": max_total_bytes,
            "prunable_contracts": sorted(PRUNABLE_CLEANUP_CONTRACTS),
        },
        "scanned_receipts": plan["scanned_receipts"],
        "protected_ids_count": plan["protected_ids_count"],
        "skipped": plan["skipped"],
        "kept_count": len(plan["kept"]),
        "kept_bytes": plan["kept_bytes"],
        "eligible_receipts": plan["eligible_receipts"],
        "eligible_action_dirs": plan["eligible_action_dirs"],
        "eligible_action_files": plan["eligible_action_files"],
        "eligible_receipt_bytes": plan["eligible_receipt_bytes"],
        "eligible_action_bytes": plan["eligible_action_bytes"],
        "eligible_total_bytes": plan["eligible_total_bytes"],
        **({"result": applied} if applied is not None else {}),
    }
    return payload
