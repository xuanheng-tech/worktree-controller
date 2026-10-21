"""Provider-neutral session-end reconciliation entry point.

A directly launched executor session has no Controller bookkeeping of its own, so a
shell wrapper or an editor hook needs a single command it can call on exit. This
resolves the allocation that owns a working path, then routes it through the normal
``reconcile-session`` lifecycle. It never decides that a task finished: an unfinished
task is only ever retained.

It is deliberately advisory at the process boundary. A session exiting must not be
turned into a failure, so an uncertain or refused reconciliation is reported and the
command still exits successfully; nothing is mutated unless the Controller allows it.

A client that launched in one directory but leased linked checkouts elsewhere in the
same repository (an editor hook whose cwd is canonical) names its own session instead of
a path: ``--client-id``, ``--session-id`` and ``--holder ancestor:<program>``. Only
writer leases whose recorded holder is exactly that live ancestor process and session
are released, one by one, and each allocation then takes the same terminal route. A
hosted child (``CONTROLLER_WRITER_MODE``/``CONTROLLER_WRITER_CONTEXT``) never ends its
parent's allocation, and a repeated notification finds nothing left to do.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .controller import status
from .environment import unsafe_ignored_paths
from .git_backend import ignored_paths, inventory
from .lifecycle import reconcile_session
from .models import ControllerError, WriterHolder, WriterLease
from .owner_actions import owner_action
from .recovery_guidance import guide_argv
from .store import MetadataStore
from .writer_lease import holder_is_live, release_writer_lease, resolve_holder, same_holder

TRIGGERS = ("task-terminal", "session-archive", "manual")
COMPLETE_CODES = frozenset({"RETAINED", "RELEASE_INTENT_CREATED", "RELEASE_INTENT_EXISTS",
                            "CHECKPOINT_RETAINED", "HANDOFF_RETAINED"})


def _observed_allocations(store: MetadataStore, holder: WriterHolder, leases: list[WriterLease],
                          *, checking: bool) -> list[dict[str, Any]]:
    """Keep reporting an interrupted closeout after its lease has been released.

    This is an observation of the original selector, never authority to mutate a
    checkout without its lease. Only an existing Task may resolve the owner work.
    """
    identity = asdict(holder)
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    path = store.session_closeouts_dir / f"{key}.json"
    with store.lock(exclusive=not checking):
        previous: list[dict[str, Any]] = []
        if path.exists():
            if path.stat().st_size > 1024 * 1024:
                raise ControllerError("INVALID_METADATA", "session observation is too large")
            value = store._read_json(path)
            if (value.get("holder") != identity or value.get("contract") != "session-closeout/v1"
                    or not isinstance(value.get("allocations"), list)
                    or any(not isinstance(row, dict) or any(not isinstance(row.get(field), str)
                           for field in ("allocation_id", "owner", "task_key", "authority_key", "path"))
                           for row in value["allocations"])):
                raise ControllerError("INVALID_METADATA", "session observation identity is invalid")
            previous = value["allocations"]
        if not checking and leases:
            current = store._load_unlocked()
            rows = {row["allocation_id"]: row for row in previous}
            for lease in leases:
                if current.writer_leases.get(lease.allocation_id) != lease:
                    raise ControllerError("WRITER_LEASE_MISMATCH", "writer changed before session observation")
                record = current.records[lease.allocation_id]
                rows[lease.allocation_id] = {"allocation_id": record.allocation_id, "path": lease.worktree_path,
                    **{field: getattr(record, field) for field in ("owner", "task_key", "authority_key")}}
            store._atomic_json(path, {"contract": "session-closeout/v1", "holder": identity,
                                      "allocations": list(rows.values())})
            _prune_unreachable_closeouts(store, keep=path)
        return previous


def _prune_unreachable_closeouts(store: MetadataStore, *, keep: Path) -> None:
    """Remove observations no selector can ever read again.

    A closeout is keyed by its exact holder identity, which only that live
    process can present. Once the holder is provably gone (another boot, or the
    pid no longer has its recorded start time) the document is unreachable.
    Anything unreadable or not provably dead is kept.
    """
    directory = store.session_closeouts_dir
    try:
        candidates = sorted(directory.glob("*.json"))
    except OSError:
        return
    for candidate in candidates:
        if candidate == keep:
            continue
        try:
            if candidate.is_symlink() or candidate.stat().st_size > 1024 * 1024:
                continue
            value = store._read_json(candidate)
            holder = WriterHolder(**value["holder"])
            expected = hashlib.sha256(
                json.dumps(asdict(holder), sort_keys=True).encode()
            ).hexdigest()
            if (
                value.get("contract") != "session-closeout/v1"
                or candidate.stem != expected
                or holder_is_live(holder)
            ):
                continue
            candidate.unlink()
        except (ControllerError, OSError, TypeError, KeyError, ValueError):
            continue


def _previous_outcome(canonical: Path, observed: dict[str, Any]) -> dict[str, Any]:
    row = {**observed, "terminal_complete": False, "task_completed": False}
    try:
        _, item = _resolve(Path(observed["path"]))
        if any(item.get(key) != observed[key] for key in ("allocation_id", "owner", "task_key", "authority_key")):
            raise ControllerError("OWNER_IDENTITY_CHANGED", "previous allocation identity changed")
        checkpoint = item.get("wait_checkpoint")
        lease_present = (item.get("lifecycle_observation") or {}).get("lease", {}).get("present")
        usable = not (lease_present or item.get("metadata_missing") or item.get("detached")
                      or item.get("locked") or item.get("prunable") or item.get("external_bindings"))
        if (usable and item.get("lifecycle") == "RETAINED" and isinstance(checkpoint, dict)
                and checkpoint.get("head") == item.get("head") and not item.get("external_bindings")):
            decision = {"code": "CHECKPOINT_RETAINED", "reason": "Task checkpoint retained; no completion asserted"}
        elif (usable and item.get("lifecycle") == "HANDOFF_READY" and item.get("handoff_commit") == item.get("head")
              and item.get("finalizer_commit") == item.get("head") and item.get("finalizer_record_id")
              and not item.get("external_bindings")):
            decision = {"code": "HANDOFF_RETAINED", "reason": "frozen Finalizer handoff retained"}
        else:
            decision = {"code": "OWNER_ACTION_REQUIRED", "reason": "previous closeout still needs its owner"}
        row["decision"] = decision
        row.update(_outcome(canonical, item, decision))
    except (ControllerError, OSError) as error:
        row.update(_closed_outcome(canonical, observed, error=error))
    if not row["terminal_complete"]:
        row["recovery_argv"] = guide_argv(str(canonical), observed["path"])
    return row


def _closed_outcome(canonical: Path, observed: dict[str, Any],
                    *, error: Exception | None = None) -> dict[str, Any]:
    """Absence proves nothing; only complete matching release evidence settles a row."""
    from .lifecycle_history import lifecycle_history

    try:
        proof = lifecycle_history(canonical, allocation_id=observed["allocation_id"], limit=1)["decision"]
        closed = proof.get("closed_releases", [])
        if (not proof.get("live_allocations") and len(closed) == 1
                and closed[0].get("proof_status") == "COMPLETE"
                and closed[0].get("release_status") == "RELEASED"
                and all(closed[0].get(key) == observed[key] for key in
                        ("allocation_id", "owner", "task_key", "authority_key") if key in observed)):
            return {"terminal_complete": True, "task_completed": False,
                    "decision": {"code": "CHECKOUT_RELEASED", "task_completed": False,
                                 "release_receipt_id": closed[0]["release_receipt_id"]}}
    except (ControllerError, OSError) as proof_error:
        error = proof_error
    return {"terminal_complete": False, "task_completed": False,
            "decision": {"code": "ALLOCATION_NOT_SETTLED",
                         "blocker": getattr(error, "code", "MISSING_CLOSEOUT_EVIDENCE"),
                         "reason": str(error) if error else "expected allocation has no complete release proof"},
            "inspect_argv": ["worktree-controller", "lifecycle-history", "--repo", str(canonical),
                             "--allocation-id", observed["allocation_id"], "--json"]}


def _cover_expected(result: dict[str, Any], path: Path, expected: list[str]) -> None:
    """Expected IDs add coverage obligations; they never select mutation targets."""
    rows = [*result.get("allocations", []), *result.get("previous_allocations", [])]
    missing = sorted(set(expected) - {row["allocation_id"] for row in rows})
    additions = []
    for allocation in missing:
        observation = {"allocation_id": allocation}
        additions.append({**observation, **_closed_outcome(path, observation)})
    result["expected_allocations"] = additions


def _artifacts(item: dict[str, Any]) -> tuple[str, ...]:
    registered = [resource["path"] for resource in item.get("disposable_resources", [])
                  if resource.get("state") == "registered"]
    return tuple(path for path in unsafe_ignored_paths(ignored_paths(Path(item["path"])))
                 if not any(path.rstrip("/") == root or path.startswith(root + "/")
                            for root in registered))


def _outcome(canonical: Path, item: dict[str, Any], decision: dict[str, Any],
             *, checking: bool = False) -> dict[str, Any]:
    """Report lifecycle settlement separately from the advisory process exit."""
    current = {**item, "record_version": decision.get("record_version", item.get("record_version"))}
    complete = decision.get("code") in ({"SESSION_READY"} if checking else COMPLETE_CODES)
    actions: tuple[str, ...] = ()
    if item.get("dirty"):
        actions = ("review_task_files", "finalize_or_checkpoint", "preserve_unknown_files")
    elif not checking and item.get("lifecycle") == "ACTIVE" and decision.get("code") == "RETAINED":
        actions = ("save_task_checkpoint",)
    try:
        unclassified = _artifacts(item)
    except (ControllerError, OSError):
        unclassified = ("<inspection-unavailable>",)
    if unclassified:
        actions += ("classify_local_artifacts",)
    if not complete and not actions:
        actions = ("resolve_terminal_blocker",)
    result: dict[str, Any] = {"terminal_complete": complete and not actions,
                              "task_completed": False}
    if actions:
        result["owner_action"] = owner_action(str(canonical), current,
            reason=str(decision.get("blocker") or decision.get("reason") or decision.get("code")),
            required_actions=actions)
    if not result["terminal_complete"]:
        result["recovery_argv"] = guide_argv(str(canonical), item["path"])
    return result


def _check_terminal(item: dict[str, Any]) -> dict[str, Any]:
    refusal = _refuse(item)
    if refusal:
        return {"code": "NO_TERMINAL_ACTION", "reason": refusal}
    if item.get("dirty"):
        return {"code": "OWNER_ACTION_REQUIRED", "reason": "preserve task files before normal exit"}
    if _artifacts(item):
        return {"code": "OWNER_ACTION_REQUIRED", "reason": "classify local artifacts before normal exit"}
    if item.get("lifecycle") != "HANDOFF_READY":
        return {"code": "CHECKPOINT_REQUIRED", "reason":
                "unfinished work needs a Task checkpoint and explicit park before normal exit"}
    if (item.get("handoff_commit") != item.get("head")
            or item.get("finalizer_commit") != item.get("head")
            or not item.get("finalizer_record_id")):
        return {"code": "OWNER_ACTION_REQUIRED", "blocker": "FROZEN_HANDOFF_DRIFT",
                "reason": "HEAD must match the frozen handoff and Finalizer evidence before normal exit"}
    return {"code": "SESSION_READY", "reason": "frozen handoff is ready for terminal reconciliation"}


def _summary(result: dict[str, Any]) -> None:
    rows = [*result.get("allocations", []), *result.get("previous_allocations", []),
            *result.get("expected_allocations", [])]
    complete = sum(row.get("terminal_complete") is True for row in rows)
    result["summary"] = {
        "contract": "session-closeout/v1", "selected": len(rows), "complete": complete,
        "pending": len(rows) - complete,
        "terminal_complete": (complete == len(rows) and result.get("selection_complete") is True),
        "task_completed": False,
    }


def _resolve(path: Path) -> tuple[Path, dict[str, Any]]:
    resolved = path.expanduser().resolve(strict=True)
    repository = inventory(resolved)
    payload = status(repository.canonical_path)
    worktrees = payload.get("worktrees")
    if not isinstance(worktrees, list):
        raise ControllerError("UNKNOWN_TASK", "controller status is unavailable")
    for item in worktrees:
        candidate = item.get("path")
        if not isinstance(candidate, str):
            continue
        try:
            same = Path(candidate).resolve(strict=False) == resolved
        except OSError:
            continue
        if same:
            return repository.canonical_path, item
    raise ControllerError("UNKNOWN_TASK", f"no governed allocation owns this path: {resolved}")


def _refuse(item: dict[str, Any]) -> str | None:
    if item.get("canonical"):
        return "canonical is never reconciled by a session hook"
    if item.get("metadata_missing"):
        return "allocation metadata is missing"
    if item.get("detached"):
        return "detached worktree"
    if item.get("locked") or item.get("prunable"):
        return "locked or prunable worktree requires review"
    if item.get("external_bindings"):
        return "registered external or runtime bindings"
    if item.get("integration_intent_id"):
        return "an integration intent still binds this allocation"
    if item.get("role") not in {"feature", "audit"}:
        return f"role {item.get('role')} has no session terminal action"
    if item.get("lifecycle") not in {"ACTIVE", "HANDOFF_READY"}:
        return f"lifecycle {item.get('lifecycle')} has no session terminal action"
    return None


def _terminal(
    canonical: Path,
    item: dict[str, Any],
    *,
    owner: str | None,
    trigger: str,
    expected_record_version: int | None = None,
    expected_allocation_id: str | None = None,
) -> dict[str, Any]:
    """Route one resolved allocation through reconcile-session, or say why not."""

    refusal = _refuse(item)
    if refusal is not None:
        return {"code": "NO_TERMINAL_ACTION", "reason": refusal}
    effective_owner = owner or item.get("owner")
    if not isinstance(effective_owner, str) or not effective_owner:
        return {"code": "NO_TERMINAL_ACTION", "reason": "owner is unknown"}
    if owner and owner != item.get("owner"):
        return {
            "code": "NO_TERMINAL_ACTION",
            "reason": "declared owner does not match the recorded owner",
        }
    try:
        payload = reconcile_session(
            canonical,
            target=Path(str(item["path"])),
            expected_record_version=(
                int(item["record_version"])
                if expected_record_version is None
                else expected_record_version
            ),
            owner=effective_owner,
            trigger=trigger,
            expected_allocation_id=expected_allocation_id or str(item["allocation_id"]),
        )
    except ControllerError as error:
        return {
            "code": "NO_TERMINAL_ACTION",
            "reason": error.message if hasattr(error, "message") else str(error),
            "blocker": error.code,
        }
    decision = payload.get("decision")
    return decision if isinstance(decision, dict) else {"code": "NO_TERMINAL_ACTION"}


def _end_session(
    path: Path, *, client_id: str, session_id: str, holder_selector: str, trigger: str,
    run_id: str | None = None, checking: bool = False,
) -> dict[str, Any]:
    """Release this session's own writer leases in the repository, then end each task."""

    if not checking and ("CONTROLLER_WRITER_MODE" in os.environ or "CONTROLLER_WRITER_CONTEXT" in os.environ):
        return {
            "decision": {
                "code": "NO_TERMINAL_ACTION",
                "reason": "a hosted child never ends its parent's allocation",
            }
        }
    holder = resolve_holder(holder_selector, client_id=client_id, session_id=session_id, run_id=run_id)
    repository = inventory(path.expanduser().resolve(strict=True))
    snapshot = MetadataStore(repository.common_dir).load()
    leases = sorted(
        (lease for lease in snapshot.writer_leases.values() if same_holder(lease.holder, holder)),
        key=lambda lease: lease.worktree_path,
    )
    observed = _observed_allocations(MetadataStore(repository.common_dir), holder, leases, checking=checking)
    related = [lease for lease in snapshot.writer_leases.values()
               if lease.holder.pid == holder.pid and lease.holder.boot_id == holder.boot_id
               and lease.holder.start_ticks == holder.start_ticks
               and lease.holder.client_id == client_id and lease.holder.session_id == session_id]
    if not leases and related:
        return {"selection_complete": False, "allocations": [], "decision": {
            "code": "SESSION_SELECTOR_MISMATCH",
            "reason": "the recorded run differs; an old notification cannot end a replacement run"}}
    ended: list[dict[str, Any]] = []
    for lease in leases:
        row: dict[str, Any] = {
            "path": lease.worktree_path,
            "allocation_id": lease.allocation_id,
            "lease_id": lease.lease_id,
            "run_id": lease.holder.run_id,
        }
        try:
            record = snapshot.records.get(lease.allocation_id)
            if record is None:
                raise ControllerError("INVALID_METADATA", "leased allocation is missing")
            row.update({key: getattr(record, key) for key in
                        ("owner", "task_key", "authority_key", "record_version")})
            if not checking:
                released = release_writer_lease(
                    repository.canonical_path,
                    target=Path(lease.worktree_path),
                    lease_id=lease.lease_id,
                    holder=lease.holder,
                )
                row["lease"] = released["decision"]["code"]
            canonical, item = _resolve(Path(lease.worktree_path))
            if any(item.get(key) != getattr(record, key) for key in
                   ("allocation_id", "owner", "task_key", "authority_key")):
                raise ControllerError("IDENTITY_MISMATCH", "selected allocation identity changed")
            if item.get("record_version") != record.record_version:
                raise ControllerError("CAS_MISMATCH", "selected record changed during closeout")
            if checking and MetadataStore(repository.common_dir).load().writer_leases.get(
                    lease.allocation_id) != lease:
                raise ControllerError("WRITER_LEASE_MISMATCH", "selected writer changed during closeout check")
            row["decision"] = _check_terminal(item) if checking else _terminal(
                canonical,
                item,
                owner=None,
                trigger=trigger,
                expected_record_version=record.record_version,
                expected_allocation_id=lease.allocation_id,
            )
            row.update(_outcome(canonical, item, row["decision"], checking=checking))
        except (ControllerError, OSError) as error:
            row["decision"] = {
                "code": "NO_TERMINAL_ACTION",
                "reason": str(error),
                "blocker": getattr(error, "code", "OS_ERROR"),
            }
            row["terminal_complete"] = False
            row["recovery_argv"] = guide_argv(str(repository.canonical_path), lease.worktree_path)
        ended.append(row)
    return {
        "holder": {"client_id": client_id, "session_id": session_id, "run_id": run_id,
                   "pid": holder.pid},
        "selection_complete": True,
        "allocations": ended,
        "previous_allocations": [_previous_outcome(repository.canonical_path, row) for row in observed
                                 if row["allocation_id"] not in {lease.allocation_id for lease in leases}],
        "decision": {
            "code": ("SESSION_CHECKED" if checking else "SESSION_ENDED") if ended else "NO_TERMINAL_ACTION",
            "reason": (
                f"{len(ended)} allocation(s) examined; read each decision and summary.terminal_complete"
                if ended
                else "this session holds no writer lease in the repository"
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="worktree-session-end",
        description="Reconcile the governed allocation that owns a path when a session ends",
    )
    parser.add_argument("--path", type=Path, default=Path.cwd())
    parser.add_argument("--trigger", choices=TRIGGERS, default="task-terminal")
    parser.add_argument("--owner")
    parser.add_argument("--client-id", help="session selector: the writer lease client")
    parser.add_argument("--session-id", help="session selector: the writer lease session")
    parser.add_argument("--run-id", help="session selector: the exact recorded run, when present")
    parser.add_argument("--holder", help="session selector: ancestor:<program> or pid")
    parser.add_argument("--check", action="store_true", help="read-only preparation check; requires the full session selector")
    parser.add_argument("--require-complete", action="store_true", help="exit 2 unless every selected checkout is settled")
    parser.add_argument("--expected-allocation-id", type=lambda value: str(uuid.UUID(value)),
                        action="append", default=[], help="also require closeout proof for each known allocation")
    arguments = parser.parse_args(argv)

    result: dict[str, Any] = {"command": "session-end", "trigger": arguments.trigger}
    selector = (arguments.client_id, arguments.session_id, arguments.holder)
    if (any(selector) or arguments.run_id or arguments.check or arguments.require_complete
            or arguments.expected_allocation_id):
        if not all(selector) or arguments.owner:
            result["decision"] = {
                "code": "NO_TERMINAL_ACTION",
                "reason": "the session selector needs --client-id, --session-id and --holder, "
                "and never takes --owner",
            }
        else:
            try:
                result.update(
                    _end_session(
                        arguments.path,
                        client_id=arguments.client_id,
                        session_id=arguments.session_id,
                        holder_selector=arguments.holder,
                        trigger=arguments.trigger,
                        run_id=arguments.run_id,
                        checking=arguments.check,
                    )
                )
            except (ControllerError, OSError) as error:
                result["decision"] = {
                    "code": "NO_TERMINAL_ACTION",
                    "reason": str(error),
                    "blocker": getattr(error, "code", "OS_ERROR"),
                }
        if arguments.expected_allocation_id:
            _cover_expected(result, arguments.path, arguments.expected_allocation_id)
        _summary(result)
        print(json.dumps(result, sort_keys=True))
        return 2 if (arguments.check or arguments.require_complete) and not result["summary"]["terminal_complete"] else 0

    try:
        canonical, item = _resolve(arguments.path)
    except (ControllerError, OSError) as error:
        code = getattr(error, "code", "UNKNOWN_TASK")
        result["decision"] = {"code": "NO_TERMINAL_ACTION", "reason": str(error), "blocker": code}
        print(json.dumps(result, sort_keys=True))
        return 0

    result["decision"] = _terminal(
        canonical, item, owner=arguments.owner, trigger=arguments.trigger
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - console entry point
    sys.exit(main())
