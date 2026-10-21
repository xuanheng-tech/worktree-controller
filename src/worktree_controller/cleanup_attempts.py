"""Append-only resume attempts. A started coordinator is not a destructive action."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from . import cleanup_evidence as e
from . import preservation as p
from .metadata_relocation import historical_reference
from .models import ControllerError
from .semantics import managed_record
from .store import MetadataStore

CONTRACT = "cleanup-resume-attempt/v1"
RECONCILIATION_CONTRACT = "started-no-action-recovery/v1"
TYPED_NO_ACTION = "typed-cleanup-no-action/v1"


def _typed_refusal(store: MetadataStore, journal: dict) -> dict | None:
    path = store.completion_receipts_dir / f"{journal['transaction_id']}.json"
    if not path.exists():
        if journal.get("failure"):
            raise blocked("failed typed cleanup is missing its refusal receipt")
        return None
    receipt, digest = e.read(path)
    expected = {
        "schema_version": 1, "receipt_id": journal["transaction_id"],
        "contract": "typed-disposable-resource/v1", "result": "FAILED_OR_PARTIAL",
        "plan": journal["cleanup_plan"], "actions": journal["actions"],
        "action_evidence": [], "failure": journal.get("failure"),
    }
    checks = receipt.get("runtime_checks")
    # A topology refusal happens after the first action's runtime check was recorded.
    first = journal["actions"][0]["path"] if journal.get("actions") else None
    one_check = (isinstance(checks, list) and len(checks) == 1 and isinstance(checks[0], dict)
                 and checks[0].get("resource_path") == first)
    topology_refusal = (journal.get("failure") or {}).get("code") == "HARDLINK_TOPOLOGY_BLOCKED"
    if _verify_refusal(journal):
        checks_ok = one_check
    else:
        checks_ok = checks == [] or (topology_refusal and one_check)
    if (not journal.get("failure") or not checks_ok
            or any(receipt.get(key) != value for key, value in expected.items())):
        raise blocked("typed receipt does not prove refusal before the action boundary")
    return {"path": str(path), "sha256": digest}


def _verify_refusal(journal: dict) -> bool:
    """A runtime before 1.28.0 marked the first action STARTED before verifying topology.

    Only that exact refusal is accepted: the first action failed with
    HARDLINK_TOPOLOGY_BLOCKED and every later action is still PENDING. Its only
    evidence must be the BEFORE receipt of that action (see ``_verify_boundary``).
    """
    plan = journal.get("cleanup_plan", {})
    paths = plan.get("cleanup_paths", [])
    return (bool(paths) and (journal.get("failure") or {}).get("code") == "HARDLINK_TOPOLOGY_BLOCKED"
            and journal.get("actions") == [{"path": paths[0], "status": "FAILED_OR_PARTIAL"}]
            + [{"path": name, "status": "PENDING"} for name in paths[1:]])


def _verify_boundary(store: MetadataStore, journal: dict) -> dict | None:
    """The single BEFORE receipt left by a topology refusal, bound to the plan's topology."""
    from . import hardlinks

    action_dir = store.cleanup_actions_dir / journal["transaction_id"]
    if not _verify_refusal(journal):
        return None
    if not action_dir.is_dir() or sorted(p.name for p in action_dir.iterdir()) != ["0000-before.json"]:
        raise blocked("typed cleanup has action evidence or unknown effects; no-action recovery refused")
    path = action_dir / "0000-before.json"
    receipt, digest = e.read(path)
    topology = journal["cleanup_plan"].get("hardlink_topology")
    if (set(receipt) != {"contract", "phase", "execution_state", "resource", "topology", "timestamp"}
            or receipt["phase"] != "BEFORE" or receipt["execution_state"] != "ACTION_STARTED"
            or receipt["resource"] != journal["actions"][0]["path"]
            or not isinstance(topology, dict) or receipt["topology"] != topology
            or receipt["contract"] != hardlinks.mutation_contract(topology)):
        raise blocked("typed cleanup has action evidence or unknown effects; no-action recovery refused")
    return {"path": str(path), "sha256": digest}


def _typed_journal(store: MetadataStore, transaction: str, digest: str) -> dict:
    if str(uuid.UUID(transaction)) != transaction:
        raise blocked("canonical transaction identity required")
    journal, _ = e.read(store.transactions_dir / f"{transaction}.json", digest)
    plan = journal.get("cleanup_plan", {})
    actions = [{"path": name, "status": "PENDING"} for name in plan.get("cleanup_paths", [])]
    action_dir = store.cleanup_actions_dir / transaction
    if (journal.get("transaction_id") != transaction or journal.get("command") != "resource-clean"
            or journal.get("phase") != "PREPARED" or plan.get("contract") != "typed-disposable-resource/v1"
            or journal.get("allocation_id") != plan.get("allocation_id")
            or journal.get("target_path") != plan.get("target_path")
            or not actions):
        raise blocked("typed cleanup has action evidence or unknown effects; no-action recovery refused")
    if _verify_refusal(journal):
        _verify_boundary(store, journal)
    elif journal.get("actions") != actions or (action_dir.exists() and list(action_dir.iterdir())):
        raise blocked("typed cleanup has action evidence or unknown effects; no-action recovery refused")
    _typed_refusal(store, journal)
    return journal


def typed_terminal(store: MetadataStore, transaction: str) -> bool:
    path = store.cleanup_recoveries_dir / transaction / "typed-no-action.json"
    if not path.exists():
        return False
    record, _ = e.read(path)
    if (record.get("contract") != TYPED_NO_ACTION
            or record.get("state") != "TYPED_ATTEMPT_SUPERSEDED"
            or record.get("plan_id") != p._sha({k: v for k, v in record.items()
                                               if k not in {"plan_id", "runtime_evidence"}})
            or record.get("transaction_id") != transaction
            or record.get("completed_action_will_replay") is not False):
        raise blocked("invalid typed no-action reconciliation")
    journal = _typed_journal(store, transaction, record["transaction_sha256"])
    if (record["allocation_id"] != journal["allocation_id"]
            or record["original_plan_id"] != journal["cleanup_plan"]["plan_id"]
            or record.get("refusal_receipt") != _typed_refusal(store, journal)
            or record.get("verify_boundary_receipt") != _verify_boundary(store, journal)):
        raise blocked("typed no-action evidence belongs to another transaction")
    return True


def typed_plan(repo: Path, store: MetadataStore, transaction: str, digest: str) -> dict:
    from . import hardlinks, lifecycle, transaction_scope
    from .git_backend import inventory

    journal = _typed_journal(store, transaction, digest)
    previous = journal["cleanup_plan"]
    repository = inventory(repo)
    snapshot = store._load_unlocked()
    item, record = managed_record(repository, snapshot, Path(journal["target_path"]))
    if (transaction_scope.conflicts(store, allocation=record.allocation_id, target=item.path) != [journal]
            or record.allocation_id != previous["allocation_id"]
            or item.head != previous["expected_head"]):
        raise blocked("conflicting transaction or changed allocation identity")
    current = lifecycle._typed_cleanup_plan_locked(
        repository, snapshot, store, item, record, owner=previous["owner"],
        expected_record_version=previous["expected_record_version"], check_transaction=False)
    if current["blockers"]:
        raise blocked("typed no-action preflight blocked: " + ", ".join(row["code"] for row in current["blockers"]))
    if (p._sha(current["resources"]) != p._sha(previous["resources"])
            or current["cleanup_paths"] != previous["cleanup_paths"]):
        raise blocked("typed resource baseline changed")
    before, after = previous["hardlink_topology"], current["hardlink_topology"]
    hardlinks.validate(before)
    hardlinks.validate(after)
    if (before["anchors"] != after["anchors"] or before["resources"] != after["resources"]
            or before["groups"].keys() != after["groups"].keys()):
        raise blocked("typed cleanup scope is no longer intact")
    for key, old in before["groups"].items():
        new = after["groups"][key]
        if (old["members"] != new["members"] or old["content"] != new["content"]
                or any(a != b for i, (a, b) in enumerate(zip(old["identity"], new["identity"], strict=True))
                       if i not in {5, 8})):
            raise blocked("typed cleanup inode, member or content changed")
        # Only ctime (5) and nlink (8) may differ, and only where links outside the
        # scope exist on both sides: their churn (added, or added and removed again
        # by another environment sharing the same files) explains the change.
        if old["identity"] != new["identity"] and not (
            old.get("external_links", 0) > 0 and new.get("external_links", 0) > 0
        ):
            raise blocked("unexplained in-scope metadata change")
    value = {
        "contract": TYPED_NO_ACTION, "state": "TYPED_ATTEMPT_SUPERSEDED",
        "transaction_id": transaction, "transaction_sha256": digest,
        "allocation_id": record.allocation_id, "original_plan_id": previous["plan_id"],
        "current_topology_sha256": after["sha256"], "in_scope": "EXACT_IDENTITY_CONTENT_MATCH",
        "external_link_history": "NOT_PROVEN", "runtime_evidence": current["runtime_evidence"],
        "completed_action_will_replay": False,
        "refusal_receipt": _typed_refusal(store, journal),
    }
    boundary = _verify_boundary(store, journal)
    if boundary is not None:
        # Refused by topology verification after the action was marked started; the
        # exact current tree and in-scope topology above prove no deletion began.
        value["verify_boundary_receipt"] = boundary
    # Host observation timestamps/visibility gaps are evidence, not authority for
    # the operation key. Bind the stable current topology and allocation baseline.
    return {**value, "plan_id": p._sha({k: v for k, v in value.items() if k != "runtime_evidence"})}


def reconcile_typed_locked(repo: Path, store: MetadataStore, transaction: str, digest: str,
                           *, dry_run: bool, expected_plan_id: str | None = None) -> dict:
    path = store.cleanup_recoveries_dir / transaction / "typed-no-action.json"
    if typed_terminal(store, transaction):
        record, _ = e.read(path)
        if record["transaction_sha256"] != digest:
            raise blocked("reconciliation transaction digest differs")
        return {"code": "TYPED_ATTEMPT_SUPERSEDED", "reconciliation_receipt": reference(path)}
    plan = typed_plan(repo, store, transaction, digest)
    if dry_run:
        return {"code": "TYPED_NO_ACTION_RECOVERABLE", "plan": plan}
    if expected_plan_id != plan["plan_id"]:
        raise blocked("typed no-action reconciliation requires its exact fresh plan")
    receipt = e.write(path.parent, path.name, plan)
    return {"code": "TYPED_ATTEMPT_SUPERSEDED", "reconciliation_receipt": receipt}


def reconcile_typed(repo: Path, transaction: str, digest: str, *, dry_run: bool,
                    expected_plan_id: str | None = None) -> dict:
    from .controller import status
    from .git_backend import inventory

    store = MetadataStore(inventory(repo).common_dir)
    with store.lock(exclusive=not dry_run):
        decision = reconcile_typed_locked(repo, store, transaction, digest, dry_run=dry_run,
                                          expected_plan_id=expected_plan_id)
    result = status(repo)
    result.update(command="resource-recover", decision=decision)
    return result


def blocked(message: str) -> ControllerError:
    return ControllerError("RESUME_ATTEMPT_BLOCKED", message)


def reference(path: Path) -> dict[str, str]:
    _, digest = e.read(path)
    return {"path": str(path), "sha256": digest}


def chain(directory: Path, record_sha: str) -> list[dict[str, Any]]:
    """Validate a single immutable succession, without relying on timestamps."""
    nodes = []
    legacy = directory / "started.json"
    if legacy.exists():
        value, digest = e.read(legacy)
        if (
            value.get("contract") != "re-preservation-recovery/v1"
            or value.get("record_sha256") != record_sha
        ):
            raise blocked("legacy started identity differs")
        nodes.append(
            {
                "attempt_id": str(uuid.uuid5(uuid.UUID(directory.name), digest)),
                "started": historical_reference(legacy, digest),
                "predecessor": None,
            }
        )
    parent = directory / "attempts"
    if parent.exists():
        for entry in parent.iterdir():
            if str(uuid.UUID(entry.name)) != entry.name:
                raise blocked("unknown attempt entry")
            value, digest = e.read(entry / "started.json")
            if (
                value.get("contract") != CONTRACT
                or value.get("phase") != "ATTEMPT_STARTED"
                or value.get("record_sha256") != record_sha
                or value.get("attempt_id") != entry.name
            ):
                raise blocked("attempt started identity differs")
            predecessor = None
            if value.get("supersedes"):
                ref = value["supersedes"]
                reconciled, _ = e.read(Path(ref["path"]), ref["sha256"])
                validate_reconciliation(directory, reconciled, record_sha)
                predecessor = reconciled["attempt"]["started"]
            nodes.append(
                {
                    "attempt_id": entry.name,
                    "started": historical_reference(entry / "started.json", digest),
                    "predecessor": predecessor,
                }
            )
    ordered = []
    previous = None
    while nodes:
        successors = [node for node in nodes if node["predecessor"] == previous]
        if len(successors) != 1:
            raise blocked("ambiguous or disconnected attempt succession")
        node = successors[0]
        nodes.remove(node)
        ordered.append(node)
        previous = node["started"]
    return ordered


def validate_reconciliation(directory: Path, value: dict[str, Any], record_sha: str) -> None:
    if (
        value.get("contract") != RECONCILIATION_CONTRACT
        or value.get("state") != "STARTED_NO_ACTION_RECOVERABLE"
        or value.get("record_sha256") != record_sha
        or value.get("safe_to_supersede") is not True
        or value.get("historical_refusal") != "NOT_RECONSTRUCTED"
        or value.get("plan_id")
        != p._sha({k: v for k, v in value.items() if k not in {"plan_id", "timestamp"}})
        or value.get("completed_action_will_replay") is not False
        or value.get("verification")
        != {
            "filesystem": "EXACT_BASELINE_MATCH",
            "preservation": "PASS",
            "runtime_authority": "PASS",
            "git": "CLEAN",
            "new_transaction": False,
            "new_completion": False,
            "destructive_action_evidence": False,
        }
    ):
        raise blocked("invalid no-action reconciliation")
    attempt = value["attempt"]
    e.read(Path(attempt["started"]["path"]), attempt["started"]["sha256"])
    expected = directory / "reconciliations" / (attempt["attempt_id"] + ".json")
    _, digest = e.read(expected)
    if not e.matches_reference({"path": value.get("path", ""), "sha256": digest}, expected):
        raise blocked("no-action reconciliation path differs")
    for ref in value["evidence"]:
        e.read(Path(ref["path"]), ref["sha256"])


def authorization(directory: Path, record_sha: str) -> dict[str, str] | None:
    attempts = chain(directory, record_sha)
    if not attempts:
        return None
    latest = attempts[-1]
    path = directory / "reconciliations" / (latest["attempt_id"] + ".json")
    if not path.exists():
        raise blocked(
            "started attempt requires explicit no-action reconciliation before supersession"
        )
    value, digest = e.read(path)
    validate_reconciliation(directory, value, record_sha)
    if value["attempt"] != latest:
        raise blocked("reconciliation covers a different attempt")
    return {"path": str(path), "sha256": digest}


def no_action_evidence(store: MetadataStore, plan: dict[str, Any]) -> dict[str, Any]:
    """No receipt alone proves nothing: examine journals AND action evidence.

    Unknown/unbound action directories fail closed. Recognized unrelated allocation
    receipts do not grant or deny this allocation's authority.
    """
    original = plan["recovery"]["original_transaction_id"]
    known = {original}
    for parent in (store.transactions_dir, store.completion_receipts_dir):
        if not parent.exists():
            continue
        for path in parent.iterdir():
            value, _ = e.read(path)
            if path.stem == original:
                continue  # The caller has verified both exact historical byte hashes.
            candidate = value.get("cleanup_plan", value.get("plan", {}))
            if (
                value.get("allocation_id") == plan["allocation_id"]
                or candidate.get("allocation_id") == plan["allocation_id"]
                or candidate.get("target_path") == plan["target_path"]
            ):
                raise blocked(
                    "additional transaction/completion evidence forbids no-action recovery"
                )
            other_allocation = candidate.get("allocation_id", value.get("allocation_id"))
            if other_allocation and other_allocation != plan["allocation_id"]:
                known.add(path.stem)
    actions = store.cleanup_actions_dir
    if actions.exists():
        for entry in actions.iterdir():
            if entry.name not in known:
                raise blocked("unbound destructive-action evidence forbids no-action recovery")
            if entry.name == original and list(entry.iterdir()):
                # This narrow legacy receipt has no action graph. Do not attribute
                # newly discovered evidence to its already completed backend step.
                raise blocked("unattributed original action evidence forbids no-action recovery")
    return {"new_transaction": False, "new_completion": False, "destructive_action_evidence": False}


def action_state(store: MetadataStore, attempt_id: str) -> str | None:
    for parent in (store.transactions_dir, store.completion_receipts_dir):
        if not parent.exists():
            continue
        for path in parent.iterdir():
            value, _ = e.read(path)
            plan = value.get("cleanup_plan", value.get("plan", {}))
            if plan.get("recovery", {}).get("attempt_id") == attempt_id:
                states = [action.get("status") for action in value.get("actions", [])]
                if states and all(state == "COMPLETED" for state in states):
                    return "ACTION_COMPLETED"
                if any(state in {"FAILED_OR_PARTIAL", "COMPLETED"} for state in states):
                    return "PARTIAL_ACTION"
                if "STARTED" in states:
                    return "ACTION_STARTED"
    return None


def evidence_references(
    store: MetadataStore, directory: Path, plan: dict[str, Any]
) -> list[dict[str, str]]:
    recovery = plan["recovery"]
    transaction = recovery["original_transaction_id"]
    old, _ = e.read(store.completion_receipts_dir / (transaction + ".json"))
    paths = [
        directory / "record.json",
        store.transactions_dir / (transaction + ".json"),
        store.completion_receipts_dir / (transaction + ".json"),
    ]
    for evidence in (old["plan"]["preservation_evidence"], plan["preservation_evidence"]):
        generation = Path(evidence["receipt_path"]).parent
        paths.extend(generation / name for name in ("receipt.json", "plan.json", "manifest.json"))
    paths.extend(directory.glob("**/started.json"))
    paths.extend(directory.glob("attempts/*/underlying.json"))
    paths.extend(directory.glob("attempts/*/result.json"))
    return [reference(path) for path in sorted(set(paths))]


def underlying(
    result: dict[str, Any] | None = None, exception: Exception | None = None
) -> dict[str, Any]:
    """The cleanup call is in-process; no subprocess exit/stdout/stderr is fabricated.

    Controller errors and plan blockers contain governance metadata, never provider
    bytes. Arbitrary unexpected exception text is represented by type/digest only.
    """
    decision = (result or {}).get("decision", {})
    error = None
    if exception is not None:
        error = {
            "code": getattr(exception, "code", "COORDINATOR_EXCEPTION"),
            "exception_type": type(exception).__name__,
            "errno": getattr(exception, "errno", None),
        }
        if isinstance(exception, ControllerError):
            error["message"] = str(exception)
        else:
            error["message_sha256"] = p._sha(str(exception))
    blockers = decision.get("plan", {}).get("blockers", [])
    return {
        "transport": "in-process-python",
        "status": decision.get("code", "EXCEPTION"),
        "exit_code": None,
        "exit_code_reason": "no subprocess was launched",
        "completion_receipt_id": decision.get("receipt_id"),
        "blockers": blockers,
        "transaction_conflicts": getattr(exception, "transaction_conflicts", []),
        "errors": (result or {}).get("errors", []),
        "exception": error,
        "stdout_summary": {"decision_code": decision.get("code"), "blockers": blockers},
        "stderr_summary": error,
    }
