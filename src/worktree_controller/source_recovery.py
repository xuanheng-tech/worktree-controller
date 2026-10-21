"""Append-only recovery evidence for exact interrupted tracked-source attempts."""

from __future__ import annotations

import argparse
import json
import os
import stat
import uuid
from pathlib import Path

from . import cleanup_evidence as e
from . import preservation as p
from . import source_preservation as s
from .metadata_relocation import historical_reference
from .models import ControllerError
from .store import MetadataStore, now_iso

CONTRACT = "tracked-source-no-action-recovery/v1"
STATE = "STARTED_NO_ACTION_RECOVERABLE"
PROGRESS_CONTRACT = "tracked-source-progress-recovery/v1"
PROGRESS_STATE = "SOURCE_ATTEMPT_PRESERVED"


def _blocked() -> ControllerError:
    return ControllerError("SOURCE_RECOVERY_BLOCKED", "exact source recovery guard refused")


def _directory(store: MetadataStore, transaction: str) -> Path:
    try:
        valid = str(uuid.UUID(transaction)) == transaction
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise _blocked()
    return store.root / "source-cleanup" / transaction


def _attempt_inputs(
    store: MetadataStore, transaction: str, receipt_sha: str
) -> tuple[dict, dict, dict, dict]:
    directory = _directory(store, transaction)
    receipt_path = store.completion_receipts_dir / f"{transaction}.json"
    receipt, digest = e.read(receipt_path, receipt_sha)
    journal_path = store.transactions_dir / f"{transaction}.json"
    ref = receipt.get("journal", {})
    if not e.matches_reference(ref, journal_path):
        raise _blocked()
    journal, _ = e.read(journal_path, ref["sha256"])
    plan_ref = journal.get("cleanup_plan", {}).get("source_plan", {})
    if not e.matches_reference(plan_ref, directory / "plan.json"):
        raise _blocked()
    cleanup, _ = e.read(directory / "plan.json", plan_ref["sha256"])
    identity = cleanup["preservation_plan"]["authority"]["identity"]
    if (
        receipt.get("contract") != "tracked-source-cleanup/v1"
        or receipt.get("receipt_id") != transaction
        or receipt.get("result") != "FAILED_OR_PARTIAL"
        or receipt.get("kind") != "tracked_source_cleanup"
        or journal.get("transaction_id") != transaction
        or journal.get("command") != "resource-clean"
        or journal.get("phase") != "PREPARED"
        or journal.get("actions", []) != []
        or cleanup.get("contract") != "tracked-source-cleanup/v1"
        or journal.get("cleanup_plan", {}).get("contract") != cleanup["contract"]
        or journal.get("repository_id")
        != store._read_json(store.root / "repo.json")["repository_id"]
        or any(
            journal.get(key) != identity[key]
            for key in (
                "repository_id",
                "allocation_id",
                "target_path",
                "expected_head",
                "expected_record_version",
            )
        )
        or any(
            journal["cleanup_plan"].get(key) != journal[key]
            for key in (
                "repository_id",
                "allocation_id",
                "target_path",
                "expected_head",
                "expected_record_version",
            )
        )
        or cleanup.get("plan_id")
        != p._sha(
            {key: value for key, value in cleanup.items() if key not in {"plan_id", "result"}}
        )
    ):
        raise _blocked()
    refs = {
        "journal": ref,
        "cleanup_receipt": historical_reference(receipt_path, digest),
        "cleanup_plan": plan_ref,
    }
    return receipt, journal, cleanup, refs


def _entries(store: MetadataStore, transaction: str, allowed: set[str]) -> None:
    with p._absolute_directory(_directory(store, transaction)) as chain:
        if not set(os.listdir(chain[-1].fd)) <= allowed:
            raise _blocked()
    action_directory = store.cleanup_actions_dir / transaction
    if action_directory.exists() and any(action_directory.iterdir()):
        raise _blocked()


def _inputs(store: MetadataStore, transaction: str, receipt_sha: str) -> tuple[dict, dict, dict]:
    receipt, journal, cleanup, refs = _attempt_inputs(store, transaction, receipt_sha)
    if (
        receipt.get("error_code") != "INTERRUPTED_TRANSACTION"
        or receipt.get("actions") != []
        or receipt.get("checks") != []
    ):
        raise _blocked()
    # BEFORE evidence precedes destructive FDs; empty receipt actions alone do not suffice.
    _entries(store, transaction, {"plan.json", "reconciliation.json"})
    return journal, cleanup, refs


def _core(value: dict) -> dict:
    return {
        key: item
        for key, item in value.items()
        if key not in {"plan_id", "runtime_evidence", "verified_at"}
    }


def _reconciled(
    store: MetadataStore, transaction: str, *, verify_generation: bool = False
) -> dict | None:
    path = _directory(store, transaction) / "reconciliation.json"
    if not path.exists():
        return None
    value, _ = e.read(path)
    journal, cleanup, refs = _inputs(
        store, transaction, value["original_evidence"]["cleanup_receipt"]["sha256"]
    )
    plan = cleanup["preservation_plan"]
    if (
        value.get("contract") != CONTRACT
        or value.get("state") != STATE
        or value.get("original_transaction_id") != transaction
        or value.get("original_evidence") != refs
        or value.get("allocation_source_identity") != plan["authority"]["identity"]
        or value.get("source_state_sha256") != p._sha(plan["source_tree"])
        or value.get("preservation_receipt_sha256") != cleanup["preservation_receipt_sha256"]
        or value.get("preservation_generation") != plan["destination"]
        or value.get("plan_id") != p._sha(_core(value))
        or value.get("new_attempt_id") != str(uuid.uuid5(uuid.UUID(transaction), p._sha(refs)))
        or value.get("safe_to_supersede") is not True
        or value.get("original_attempt_will_replay") is not False
        or value.get("actions") != []
        or value.get("failure_reason") != "transaction-scope coordinator refusal"
        or value.get("verification")
        != {
            "filesystem": "EXACT_SOURCE_BASELINE_MATCH",
            "index": "UNCHANGED",
            "head": "UNCHANGED",
            "preservation": "PASS",
            "runtime_authority": "PASS",
            "destructive_action_evidence": False,
        }
    ):
        raise _blocked()
    if verify_generation:
        s._verify_generation(plan, cleanup["preservation_receipt_sha256"])
    return value


def terminal_state(
    store: MetadataStore, transaction: str, *, verify_generation: bool = False
) -> str | None:
    if _progress_reconciled(store, transaction, verify_generation=verify_generation) is not None:
        return PROGRESS_STATE
    if _reconciled(store, transaction, verify_generation=verify_generation) is not None:
        return "SOURCE_ATTEMPT_SUPERSEDED"
    return None


def terminal(store: MetadataStore, transaction: str, *, verify_generation: bool = False) -> bool:
    return terminal_state(store, transaction, verify_generation=verify_generation) is not None


def authorization(store: MetadataStore, plan: dict, receipt_sha: str) -> dict | None:
    """Bind the only unused successor grant; a prior attempt can never be replayed."""
    grants = []
    for path in sorted((store.root / "source-cleanup").glob("*/reconciliation.json")):
        value = _reconciled(store, path.parent.name)
        if (
            value["allocation_source_identity"]["allocation_id"]
            != plan["authority"]["identity"]["allocation_id"]
        ):
            continue
        successor = _directory(store, value["new_attempt_id"])
        if successor.exists():
            continue  # Its own journal/completion governs; never reuse this identity.
        if (
            value["allocation_source_identity"] != plan["authority"]["identity"]
            or value["source_state_sha256"] != p._sha(plan["source_tree"])
            or value["preservation_receipt_sha256"] != receipt_sha
        ):
            raise _blocked()
        _, digest = e.read(path)
        grants.append(
            {"path": str(path), "sha256": digest, "new_attempt_id": value["new_attempt_id"]}
        )
    if len(grants) > 1:
        raise _blocked()
    return grants[0] if grants else None


def plan_reconciliation(
    repo: Path, transaction: str, receipt_sha: str, *, _migration_store: MetadataStore | None = None
) -> dict:
    from .git_backend import inventory
    from .source_cleanup import _state, _store

    store = _migration_store or MetadataStore(inventory(repo).common_dir)
    journal, cleanup, refs = _inputs(store, transaction, receipt_sha)
    if _store(cleanup["preservation_plan"]).root.parent.parent != store.root.parent.parent:
        raise _blocked()
    # Both passes independently inspect source/index/generation and managed runtime.
    state = _state(
        cleanup, {}, journal=journal, journal_ref=refs["journal"], _migration_store=_migration_store
    )
    plan = cleanup["preservation_plan"]
    if s._scan(plan["authority"]["identity"]) != plan["source_tree"]:
        raise _blocked()
    if _inputs(store, transaction, receipt_sha) != (journal, cleanup, refs):
        raise _blocked()
    state = _state(
        cleanup, {}, journal=journal, journal_ref=refs["journal"], _migration_store=_migration_store
    )
    value = {
        "contract": CONTRACT,
        "state": STATE,
        "original_transaction_id": transaction,
        "new_attempt_id": str(uuid.uuid5(uuid.UUID(transaction), p._sha(refs))),
        "original_evidence": refs,
        "allocation_source_identity": plan["authority"]["identity"],
        "source_state_sha256": p._sha(plan["source_tree"]),
        "preservation_generation": plan["destination"],
        "preservation_receipt_sha256": cleanup["preservation_receipt_sha256"],
        "failure_reason": "transaction-scope coordinator refusal",
        "actions": [],
        "safe_to_supersede": True,
        "original_attempt_will_replay": False,
        "verification": {
            "filesystem": "EXACT_SOURCE_BASELINE_MATCH",
            "index": "UNCHANGED",
            "head": "UNCHANGED",
            "preservation": "PASS",
            "runtime_authority": "PASS",
            "destructive_action_evidence": False,
        },
    }
    return {**value, "plan_id": p._sha(value), "runtime_evidence": state["runtime_evidence"]}


def reconcile(repo: Path, transaction: str, receipt_sha: str, expected_plan_id: str) -> dict:
    from .lifecycle import _typed_runtime_blockers, _typed_runtime_evidence, inventory
    from .semantics import assert_record_version as _cas
    from .semantics import managed_record as _managed_record
    from .transaction_scope import check_source

    planned = plan_reconciliation(repo, transaction, receipt_sha)
    if planned["plan_id"] != expected_plan_id:
        raise _blocked()
    store = MetadataStore(inventory(repo).common_dir)
    identity = planned["allocation_source_identity"]
    with store.lock(exclusive=True):
        journal, cleanup, refs = _inputs(store, transaction, receipt_sha)
        if refs != planned["original_evidence"]:
            raise _blocked()
        item, record = _managed_record(
            inventory(repo), store._load_unlocked(), Path(identity["target_path"])
        )
        _cas(record, identity["expected_record_version"])
        if (
            record.lifecycle != "ACTIVE"
            or record.owner != identity["owner"]
            or item.head != identity["expected_head"]
        ):
            raise _blocked()
        check_source(store, record.allocation_id, item.path, journal)
        runtime = _typed_runtime_evidence(item, record, store._load_unlocked())
        blockers = _typed_runtime_blockers(runtime)
        if blockers:
            raise s.AuthorityBlocked(blockers[0]["code"], runtime)
        if s._scan(identity) != cleanup["preservation_plan"]["source_tree"]:
            raise _blocked()
        s._verify_generation(cleanup["preservation_plan"], cleanup["preservation_receipt_sha256"])
        ref = e.write(
            _directory(store, transaction),
            "reconciliation.json",
            {
                **planned,
                "runtime_evidence": runtime,
                "verified_at": now_iso(),
            },
        )
    return {"result": STATE, "reconciliation": ref, "new_attempt_id": planned["new_attempt_id"]}


def _baseline_observed(cleanup: dict) -> dict:
    tree = cleanup["preservation_plan"]["source_tree"]
    return {
        row["path"]: {
            "identity": tree["source_identities"][row["path"]],
            "sha256": row["sha256"],
            "mode": row["mode"],
        }
        for row in tree["files"]
    }


def _restored_identity(actual: list[int], original: list[int], row: dict) -> None:
    if (
        len(actual) != 9
        or actual[:2] != original[:2]
        or actual[6:] != original[6:]
        or actual[8] != 1
        or not stat.S_ISREG(actual[2])
        or stat.S_IMODE(actual[2]) != row["mode"]
        or actual[3] != row["size"]
    ):
        raise _blocked()


def _frozen_state(state: dict) -> dict:
    return {key: value for key, value in state.items() if key != "runtime_evidence"}


def _check_state(cleanup: dict, state: dict, observed: dict, restored: set[str]) -> None:
    tree = cleanup["preservation_plan"]["source_tree"]
    remaining = sorted(set(tree["dirty_paths"]) - restored)
    if (
        state.get("head") != tree["base_head"]
        or state.get("index_sha256") != tree["index_sha256"]
        or state.get("dirty_paths") != remaining
        or state.get("observed") != observed
        or state.get("dirty_digest") != p._sha({path: observed[path] for path in remaining})
        or state.get("preservation") != cleanup["verification"]
        or state.get("preservation", {}).get("verification") != "PASS"
        or state.get("preservation", {}).get("cleanup_authorized") is not False
    ):
        raise _blocked()


def _progress_inputs(
    store: MetadataStore, transaction: str, receipt_sha: str
) -> tuple[dict, dict, dict, list[dict], dict]:
    receipt, journal, cleanup, refs = _attempt_inputs(store, transaction, receipt_sha)
    actions, checks, rows = receipt["actions"], receipt["checks"], cleanup["restore"]
    tree = cleanup["preservation_plan"]["source_tree"]
    if (
        not actions
        or len(actions) >= len(rows)
        or [row["path"] for row in rows] != tree["dirty_paths"]
        or any(action["path"] != rows[i]["path"] for i, action in enumerate(actions))
    ):
        raise _blocked()
    observed, completed, linked_checks = _baseline_observed(cleanup), {}, []
    allowed = {"plan.json", "progress-reconciliation.json"}
    action_refs = []
    for step, action in enumerate(actions):
        row, path = rows[step], action["path"]
        status = action.get("status")
        if (
            status not in {"ACTION_STARTED", "ACTION_COMPLETED"}
            or (status == "ACTION_STARTED" and step != len(actions) - 1)
            or set(action)
            != (
                {"path", "status", "before", "after"}
                if status == "ACTION_COMPLETED"
                else {"path", "status", "before"}
            )
        ):
            raise _blocked()
        name = f"{step:04d}-before.json"
        allowed.add(name)
        if not e.matches_reference(action["before"], _directory(store, transaction) / name):
            raise _blocked()
        before, _ = e.read(Path(action["before"]["path"]), action["before"]["sha256"])
        if (
            before.get("contract") != "tracked-source-cleanup/v1"
            or before.get("transaction_id") != transaction
            or before.get("path") != path
            or before.get("restore") != row
            or before.get("execution_state") != "ACTION_STARTED"
        ):
            raise _blocked()
        _check_state(cleanup, before["state"], observed, set(completed))
        linked_checks.append(before["state"])
        action_refs.append({"path": path, "status": status, "before": action["before"]})
        if status == "ACTION_COMPLETED":
            name = f"{step:04d}-after.json"
            allowed.add(name)
            if not e.matches_reference(action["after"], _directory(store, transaction) / name):
                raise _blocked()
            after, _ = e.read(Path(action["after"]["path"]), action["after"]["sha256"])
            if (
                after.get("contract") != "tracked-source-cleanup/v1"
                or after.get("transaction_id") != transaction
                or after.get("path") != path
                or after.get("before") != action["before"]
                or after.get("execution_state") != "ACTION_COMPLETED"
            ):
                raise _blocked()
            actual = after["state"]["observed"][path]["identity"]
            _restored_identity(actual, tree["source_identities"][path], row)
            completed[path] = actual
            observed[path] = {"identity": actual, "sha256": row["sha256"], "mode": row["mode"]}
            _check_state(cleanup, after["state"], observed, set(completed))
            linked_checks.append(after["state"])
            action_refs[-1]["after"] = action["after"]
    if checks != linked_checks:
        raise _blocked()
    _entries(store, transaction, allowed)
    return journal, cleanup, refs, action_refs, completed


def _progress_value(
    cleanup: dict, transaction: str, refs: dict, actions: list, state: dict
) -> dict:
    plan = cleanup["preservation_plan"]
    restored = [action["path"] for action in actions]
    return {
        "contract": PROGRESS_CONTRACT,
        "state": PROGRESS_STATE,
        "original_transaction_id": transaction,
        "original_evidence": refs,
        "allocation_source_identity": plan["authority"]["identity"],
        "source_state_sha256": p._sha(plan["source_tree"]),
        "preservation_generation": plan["destination"],
        "preservation_receipt_sha256": cleanup["preservation_receipt_sha256"],
        "historical_actions": actions,
        "observed_restored_paths": restored,
        "remaining_preserved_paths": state["dirty_paths"],
        "observed_state": _frozen_state(state),
        "original_attempt_will_replay": False,
        "cleanup_complete": False,
        "release_authorized": False,
        "successor_authorized": False,
        "verification": {
            "filesystem": "EXACT_RESTORED_PREFIX_AND_PRESERVED_REMAINDER",
            "index": "UNCHANGED",
            "head": "UNCHANGED",
            "preservation": "PASS",
            "runtime_authority": "PASS",
            "started_action": "CURRENT_BASELINE_MATCH_ONLY",
        },
    }


def _progress_reconciled(
    store: MetadataStore, transaction: str, *, verify_generation: bool = False
) -> dict | None:
    path = _directory(store, transaction) / "progress-reconciliation.json"
    if not path.exists():
        return None
    value, _ = e.read(path)
    _, cleanup, refs, actions, completed = _progress_inputs(
        store, transaction, value["original_evidence"]["cleanup_receipt"]["sha256"]
    )
    observed = _baseline_observed(cleanup)
    rows = {row["path"]: row for row in cleanup["restore"]}
    tree = cleanup["preservation_plan"]["source_tree"]
    for action in actions:
        name, row = action["path"], rows[action["path"]]
        actual = value["observed_state"]["observed"][name]["identity"]
        _restored_identity(actual, tree["source_identities"][name], row)
        if name in completed and actual != completed[name]:
            raise _blocked()
        observed[name] = {"identity": actual, "sha256": row["sha256"], "mode": row["mode"]}
    _check_state(cleanup, value["observed_state"], observed, {a["path"] for a in actions})
    expected = _progress_value(cleanup, transaction, refs, actions, value["observed_state"])
    if _core(value) != expected or value.get("plan_id") != p._sha(expected):
        raise _blocked()
    if verify_generation:
        s._verify_generation(cleanup["preservation_plan"], cleanup["preservation_receipt_sha256"])
    return value


def plan_progress(
    repo: Path, transaction: str, receipt_sha: str, *, _migration_store: MetadataStore | None = None
) -> dict:
    from .git_backend import inventory
    from .source_cleanup import _state, _store

    store = _migration_store or MetadataStore(inventory(repo).common_dir)
    directory = _directory(store, transaction)
    if (directory / "progress-reconciliation.json").exists():
        raise _blocked()  # Existing immutable terminal proof must never be replaced/replayed.
    inputs = _progress_inputs(store, transaction, receipt_sha)
    journal, cleanup, refs, actions, completed = inputs
    completed = dict(completed)
    if _store(cleanup["preservation_plan"]).root != store.root:
        raise _blocked()
    plan = cleanup["preservation_plan"]
    # A STARTED action is observed now, never retrospectively marked completed.
    if actions[-1]["status"] == "ACTION_STARTED":
        row = cleanup["restore"][len(actions) - 1]
        with p._absolute_directory(Path(plan["authority"]["identity"]["target_path"])) as chain:
            _, actual, _ = s._read(chain, row["path"])
        _restored_identity(actual, plan["source_tree"]["source_identities"][row["path"]], row)
        completed[row["path"]] = actual
    states = [
        _state(
            cleanup,
            completed,
            journal=journal,
            journal_ref=refs["journal"],
            _migration_store=_migration_store,
        )
        for _ in range(2)
    ]
    if _frozen_state(states[0]) != _frozen_state(states[1]) or inputs != _progress_inputs(
        store, transaction, receipt_sha
    ):
        raise _blocked()
    value = _progress_value(cleanup, transaction, refs, actions, states[-1])
    return {**value, "plan_id": p._sha(value), "runtime_evidence": states[-1]["runtime_evidence"]}


def reconcile_progress(
    repo: Path, transaction: str, receipt_sha: str, expected_plan_id: str
) -> dict:
    from .git_backend import inventory

    planned = plan_progress(repo, transaction, receipt_sha)
    if planned["plan_id"] != expected_plan_id:
        raise _blocked()
    store = MetadataStore(inventory(repo).common_dir)
    with store.lock(exclusive=True):
        verified = plan_progress(repo, transaction, receipt_sha, _migration_store=store)
        if verified["plan_id"] != expected_plan_id:
            raise _blocked()
        ref = e.write(
            _directory(store, transaction),
            "progress-reconciliation.json",
            {**verified, "verified_at": now_iso()},
        )
    return {
        "result": PROGRESS_STATE,
        "reconciliation": ref,
        "release_authorized": False,
        "successor_authorized": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("plan", "reconcile", "plan-progress", "reconcile-progress")
    )
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--transaction-id", required=True)
    parser.add_argument(
        "--receipt-sha256", required=True, help="exact failed completion receipt bytes"
    )
    parser.add_argument("--expected-plan-id")
    args = parser.parse_args(argv)
    try:
        planners = {"plan": plan_reconciliation, "plan-progress": plan_progress}
        writers = {"reconcile": reconcile, "reconcile-progress": reconcile_progress}
        if args.command in planners:
            result = planners[args.command](args.repo, args.transaction_id, args.receipt_sha256)
        else:
            result = writers[args.command](
                args.repo, args.transaction_id, args.receipt_sha256, args.expected_plan_id
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ControllerError, OSError, ValueError, TypeError, KeyError) as exc:
        from .cleanup_attempts import underlying

        print(
            json.dumps(
                {"result": "SOURCE_RECOVERY_BLOCKED", "underlying": underlying(exception=exc)},
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
