"""Backfill registration identity for linked bindings written before 1.26.0.

Bindings created by older runtimes carry no ``worktree-registration/v1`` document,
so a later Git registration that reuses their administrative name would inherit
them. This reconciliation ties such a binding to its current registration only
when the live evidence still points to the allocation that created it; anything
else is reported and keeps its previous (legacy) meaning.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

from .controller import envelope
from .git_backend import inventory, is_ancestor
from .models import ControllerError
from .store import REGISTRATION_MARKER, MetadataStore

BACKFILL_CONTRACT = "binding-registration-backfill/v1"
# Acquire stamps created_at before ``git worktree add`` runs; allow for that gap
# but not for a registration created long after the allocation.
REGISTRATION_WINDOW_SECONDS = 900


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _registered_at_ns(admin: Path) -> int | None:
    """When Git created this registration: ``commondir`` is written once by add."""
    try:
        observed = os.lstat(admin / "commondir")
    except OSError:
        return None
    return observed.st_mtime_ns if stat.S_ISREG(observed.st_mode) else None


def _pointer(admin: Path) -> str | None:
    try:
        return (admin / "gitdir").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def _evaluate(store: MetadataStore, item: Any, record: Any) -> tuple[list[str], dict[str, Any]]:
    blockers: list[str] = []
    admin = item.git_dir
    relative = admin.relative_to(store.common_dir).as_posix()
    store._registration_admin(item.key, relative)
    if admin.is_symlink() or not admin.is_dir():
        blockers.append("ADMIN_DIR_UNAVAILABLE")
    if item.prunable or not item.path.is_dir():
        blockers.append("CHECKOUT_MISSING")
    if os.path.lexists(admin / REGISTRATION_MARKER):
        blockers.append("STALE_REGISTRATION_MARKER")
    if _pointer(admin) != os.fspath(item.path / ".git"):
        blockers.append("GIT_POINTER_MISMATCH")
    registered_ns = _registered_at_ns(admin)
    created = datetime.fromisoformat(record.created_at)
    limit_ns = int(created.timestamp() + REGISTRATION_WINDOW_SECONDS) * 1_000_000_000
    if registered_ns is None:
        blockers.append("REGISTRATION_TIME_UNKNOWN")
    elif registered_ns > limit_ns:
        blockers.append("REGISTRATION_NEWER_THAN_ALLOCATION")
    if not item.head:
        blockers.append("HEAD_UNRESOLVED")
    elif record.base_commit and not blockers:
        try:
            if not is_ancestor(item.path, record.base_commit, item.head):
                blockers.append("BASE_NOT_IN_HISTORY")
        except ControllerError:
            blockers.append("BASE_NOT_IN_HISTORY")
    evidence = {
        "worktree_key": item.key,
        "allocation_id": record.allocation_id,
        "record_version": record.record_version,
        "created_at": record.created_at,
        "admin_dir": relative,
        "path": os.fspath(item.path),
        "head": item.head,
        "branch": item.branch,
        "registered_at_ns": registered_ns,
    }
    return blockers, evidence


def reconcile_binding_registrations(
    repository_path: Path, *, dry_run: bool, expected_plan_id: str | None = None
) -> dict[str, Any]:
    store = MetadataStore(inventory(repository_path).common_dir)
    with store.lock(exclusive=not dry_run):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        registered = store._registrations()
        candidates: list[dict[str, Any]] = []
        refused: list[dict[str, Any]] = []
        already = 0
        live = {item.key: item for item in repository.worktrees if not item.canonical}
        for key, allocation_id in sorted(snapshot.bindings.items()):
            item = live.get(key)
            if item is None:
                continue  # Canonical or orphan bindings: not a linked registration.
            if key in registered:
                already += 1
                continue
            record = snapshot.records.get(allocation_id)
            if record is None:
                refused.append(
                    {"worktree_key": key, "allocation_id": allocation_id,
                     "blockers": ["RECORD_MISSING"]}
                )
                continue
            blockers, evidence = _evaluate(store, item, record)
            if blockers:
                refused.append({**evidence, "blockers": blockers})
            else:
                candidates.append(evidence)
        plan = {
            "contract": BACKFILL_CONTRACT,
            "repository_id": snapshot.repository_id,
            "candidates": candidates,
        }
        plan_id = _digest(plan)
        decision: dict[str, Any] = {
            "plan_id": plan_id,
            "candidates": candidates,
            "refused": refused,
            "already_registered": already,
        }
        if dry_run:
            decision["code"] = (
                "BINDING_REGISTRATION_BACKFILL_READY" if candidates
                else "BINDING_REGISTRATION_BACKFILL_NOTHING_TO_DO"
            )
            decision["dry_run"] = True
        else:
            if expected_plan_id != plan_id:
                raise ControllerError(
                    "RESOURCE_PLAN_MISMATCH",
                    "registration backfill requires the exact current dry-run plan",
                )
            for candidate in candidates:
                store._write_registration(
                    candidate["worktree_key"],
                    candidate["allocation_id"],
                    store.common_dir / candidate["admin_dir"],
                )
            if candidates:
                store.touch_repository()
            decision["code"] = (
                "BINDING_REGISTRATIONS_BACKFILLED" if candidates
                else "BINDING_REGISTRATION_BACKFILL_NOTHING_TO_DO"
            )
            decision["dry_run"] = False
            decision["written"] = len(candidates)
    payload = envelope("reconcile")
    payload["decision"] = decision
    return payload
