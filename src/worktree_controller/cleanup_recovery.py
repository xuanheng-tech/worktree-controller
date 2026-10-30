"""Re-preserve current bytes, then append a new execution baseline; never infer history."""

from __future__ import annotations

import os
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from . import cleanup_attempts as attempts
from . import cleanup_evidence as e
from . import hardlinks
from . import preservation as p
from .git_backend import inventory
from .models import SUPPORTED_METADATA_SCHEMA_VERSIONS, ControllerError
from .preserved_cleanup import coverage
from .store import MetadataStore, now_iso

CONTRACT = "re-preservation-recovery/v1"
COMPLETED_RESOURCE = "backend/.venv"
REMAINING = ["frontend/dist", "frontend/node_modules", ".venv"]
RELEASE_INSPECTION_CONTRACT = "release-recovery-inspection/v1"


def inspect_release(repo: Path, *, transaction_id: str) -> dict[str, Any]:
    """Project the persisted original request; never authorize or execute recovery."""
    from .controller import envelope
    from .metadata_codec import _validate_timestamp, release_plan_from_dict

    try:
        canonical = str(uuid.UUID(transaction_id))
    except (ValueError, AttributeError) as exc:
        raise ControllerError("INVALID_RELEASE_RECOVERY_QUERY", "canonical transaction UUID required") from exc
    if canonical != transaction_id:
        raise ControllerError("INVALID_RELEASE_RECOVERY_QUERY", "canonical transaction UUID required")
    repository = inventory(repo)
    store = MetadataStore(repository.common_dir)
    with store.lock(exclusive=False):
        journal_path = store.transactions_dir / f"{transaction_id}.json"
        try:
            journal, journal_sha = e.read(journal_path)
        except FileNotFoundError as exc:
            raise ControllerError("MISSING_RELEASE_RECOVERY_EVIDENCE", "original release journal is missing") from exc
        if journal.get("command") != "release" or "release_plan_id" not in journal:
            raise ControllerError("UNSUPPORTED_RELEASE_RECOVERY_EVIDENCE", "a persisted release plan is required")
        fields = {
            "schema_version", "transaction_id", "repository_id", "command", "phase",
            "target_path", "allocation_id", "branch", "expected_head", "expected_record_version",
            "created_at", "updated_at", "release_plan_id",
        }
        phase = journal.get("phase")
        phases = {"PREPARED", "WORKTREE_REMOVED", "RELEASED_RECORDED"}
        teardown_phases = {"TEARDOWN_PREPARED", "VENV_REMOVAL_STARTED", "VENV_REMOVED"}
        if (
            set(journal) not in (fields, fields | {"teardown_receipt_id"})
            or type(journal.get("schema_version")) is not int
            or journal["schema_version"] not in SUPPORTED_METADATA_SCHEMA_VERSIONS
            or journal.get("transaction_id") != transaction_id
            or not isinstance(phase, str)
            or phase not in phases | teardown_phases
            or (phase in teardown_phases and "teardown_receipt_id" not in journal)
            or ("teardown_receipt_id" in journal and journal["teardown_receipt_id"] != transaction_id)
            or type(journal.get("expected_record_version")) is not int
            or not isinstance(journal.get("release_plan_id"), str)
        ):
            raise ControllerError("INVALID_TRANSACTION", "release journal identity or schema is invalid")
        for field in ("created_at", "updated_at"):
            _validate_timestamp(journal[field], field)
        snapshot = store._load_unlocked()
        plan = snapshot.release_plans.get(journal["release_plan_id"])
        if plan is None:
            raise ControllerError("MISSING_RELEASE_RECOVERY_EVIDENCE", "original release plan is missing")
        if (
            plan.repository_id != snapshot.repository_id
            or any(journal[field] != getattr(plan, field) for field in (
                "repository_id", "allocation_id", "target_path", "branch", "expected_head",
                "expected_record_version",
            ))
            or bool(plan.resource_actions) != ("teardown_receipt_id" in journal)
        ):
            raise ControllerError("RELEASE_RECOVERY_MISMATCH", "release journal differs from the original plan")
        if transaction_id in snapshot.release_receipts:
            raise ControllerError("RELEASE_RECOVERY_NOT_PENDING", "use lifecycle-history for a completed release")
        if plan.publication_receipt_id is not None:
            receipt = snapshot.publication_receipts.get(plan.publication_receipt_id)
            if receipt is None:
                raise ControllerError("MISSING_PUBLICATION_RECEIPT", "original publication receipt is missing")
            if (receipt.repository_id, receipt.allocation_id, receipt.candidate_oid) != (
                plan.repository_id, plan.allocation_id, plan.expected_head,
            ):
                raise ControllerError("RELEASE_RECOVERY_MISMATCH", "publication receipt differs from the original plan")
        if plan.release_intent_id is not None:
            intent = snapshot.release_intents.get(plan.release_intent_id)
            if intent is None:
                raise ControllerError("MISSING_RELEASE_INTENT", "original release intent is missing")
            if any(getattr(intent, field) != getattr(plan, field) for field in (
                "repository_id", "allocation_id", "owner", "authority_key", "role",
                "expected_head", "expected_record_version",
            )):
                raise ControllerError("RELEASE_RECOVERY_MISMATCH", "release intent differs from the original plan")
        plan_path = store.release_plans_dir / f"{plan.plan_id}.json"
        raw_plan, plan_sha = e.read(plan_path)
        if release_plan_from_dict(raw_plan) != plan:
            raise ControllerError("RELEASE_RECOVERY_MISMATCH", "release plan changed during inspection")
        owner_release = plan.owner if plan.publication_receipt_id is None and plan.release_intent_id is None else None
        request = {
            "target_path": plan.target_path, "expected_record_version": plan.expected_record_version,
            "owner_release": owner_release, "publication_receipt_id": plan.publication_receipt_id,
            "release_intent_id": plan.release_intent_id,
        }
        arguments = ["release", "--repo", str(repository.canonical_path), "--path", plan.target_path,
                     "--expected-record-version", str(plan.expected_record_version)]
        for option, value in (("--owner-release", owner_release),
                              ("--publication-receipt-id", plan.publication_receipt_id),
                              ("--intent-id", plan.release_intent_id)):
            if value is not None:
                arguments.extend((option, value))
        arguments.append("--json")
        decision = {
            "code": "RELEASE_RECOVERY_REQUEST_INSPECTED", "contract": RELEASE_INSPECTION_CONTRACT,
            "transaction_id": transaction_id, "phase": phase, "release_plan_id": plan.plan_id,
            "allocation_id": plan.allocation_id, "owner": plan.owner,
            "request": request, "release_argv": arguments,
            "evidence": {"journal": {"path": str(journal_path), "sha256": journal_sha},
                         "release_plan": {"path": str(plan_path), "sha256": plan_sha}},
            "release_authorized": False, "execution_guards_checked": False,
        }
    payload = envelope("release-recovery-inspect")
    payload.update(repository={"repository_id": snapshot.repository_id,
                               "canonical_path": str(repository.canonical_path)}, decision=decision)
    return payload


def _blocked(message: str) -> ControllerError:
    return ControllerError("RE_PRESERVATION_RECOVERY_BLOCKED", message)


class RolloutTransactionError(ControllerError):
    """A refused read-only check with the original error and a recovery entry hint."""

    def __init__(self, cause: ControllerError | OSError, transaction: str, journal: dict):
        command = journal.get("command")
        phase = journal.get("phase")
        command = command if isinstance(command, str) else None
        phase = phase if isinstance(phase, str) else None
        plan = journal.get("cleanup_plan")
        entries = {
            "typed-disposable-resource/v1": "resource-recover --stage reconcile-typed --dry-run",
            "tracked-source-cleanup/v1": "python -m worktree_controller.source_recovery plan",
            "legacy-preserved-state-multiple-cleanup/v1": (
                "resource-recover --stage prepare-multiple --dry-run"
            ),
            "legacy-preserved-state-cleanup/v1": "resource-recover --stage prepare --dry-run",
        }
        entry = None
        if command == "release":
            entry = "release"
        elif command == "resource-clean" and isinstance(plan, dict):
            contract = plan.get("contract")
            entry = entries.get(contract) if isinstance(contract, str) else None
            if contract == "typed-disposable-resource/v1" and any(
                row.get("status") == "COMPLETED" for row in journal.get("actions", [])
            ):
                entry = "resource-recover --stage prepare-multiple --dry-run"
        code = cause.code if isinstance(cause, ControllerError) else "FILESYSTEM_ERROR"
        self.transaction_diagnostic = {
            "transaction_id": transaction,
            "command": command,
            "phase": phase,
            "error": {"code": code, "message": str(cause)},
            "recovery_entry": entry,
        }
        super().__init__(
            code, f"transaction {transaction} ({command or 'unknown'}/{phase or 'unknown'}): {cause}"
        )


def context(transaction: str, transaction_sha256: str, receipt_sha256: str) -> dict[str, Any]:
    if str(uuid.UUID(transaction)) != transaction:
        raise _blocked("canonical original transaction identity required")
    if any(
        len(s or "") != 64 or any(c not in "0123456789abcdef" for c in s)
        for s in (transaction_sha256, receipt_sha256)
    ):
        raise _blocked("trusted original transaction and receipt digests required")
    return {
        "contract": CONTRACT,
        "original_transaction_id": transaction,
        "original_transaction_sha256": transaction_sha256,
        "original_cleanup_receipt_sha256": receipt_sha256,
        "historical_hardlink_cause": "NOT_PROVEN",
        "old_transaction_remains_immutable": True,
        "completed_resources": [COMPLETED_RESOURCE],
        "completed_action_will_replay": False,
        "remaining_resources": REMAINING,
    }


def original(
    store: MetadataStore,
    recovery: dict[str, Any],
    *,
    target: Path | None = None,
    allocation_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from . import multiple_cleanup_recovery as multi_recovery

    if recovery.get("contract") == multi_recovery.CONTRACT:
        return multi_recovery.original(
            store, recovery, target=target, allocation_id=allocation_id
        )
    expected = context(
        recovery["original_transaction_id"],
        recovery["original_transaction_sha256"],
        recovery["original_cleanup_receipt_sha256"],
    )
    if any(recovery.get(k) != v for k, v in expected.items()):
        raise _blocked("recovery history assertions differ from the contract")
    transaction = recovery["original_transaction_id"]
    journal, _ = e.read(
        store.transactions_dir / f"{transaction}.json", recovery["original_transaction_sha256"]
    )
    receipt, _ = e.read(
        store.completion_receipts_dir / f"{transaction}.json",
        recovery["original_cleanup_receipt_sha256"],
    )
    actions = [{"path": COMPLETED_RESOURCE, "status": "COMPLETED"}] + [
        {"path": name, "status": "PENDING"} for name in REMAINING
    ]
    plan = receipt.get("plan", {})
    if (
        receipt.get("result") != "FAILED_OR_PARTIAL"
        or receipt.get("receipt_id") != transaction
        or receipt.get("contract") != "legacy-preserved-state-cleanup/v1"
        or receipt.get("actions") != actions
        or journal.get("actions") != actions
        or journal.get("transaction_id") != transaction
        or journal.get("command") != "resource-clean"
        or journal.get("cleanup_plan") != plan
        or journal.get("phase") != "PREPARED"
        or plan.get("cleanup_paths") != [COMPLETED_RESOURCE, *REMAINING]
        or journal.get("allocation_id") != plan.get("allocation_id")
        or journal.get("target_path") != plan.get("target_path")
        or (target is not None and plan.get("target_path") != str(target))
        or (allocation_id is not None and plan.get("allocation_id") != allocation_id)
    ):
        raise _blocked("original cleanup is not the supported exact completed/pending shape")
    for path in store.transactions_dir.glob("*.json"):
        if path.stem != transaction:
            other, _ = e.read(path)
            if (
                not recovery.get("recovery_id")
                or other.get("command") != "resource-clean"
                or other.get("cleanup_plan", {}).get("recovery") != recovery
            ):
                raise _blocked("another interrupted transaction requires its own recovery")
    if os.path.lexists(Path(plan["target_path"]) / COMPLETED_RESOURCE):
        raise _blocked("completed backend resource reappeared; replay is forbidden")
    evidence = plan["preservation_evidence"]
    verified, old_plan = coverage(
        Path(evidence["receipt_path"]), evidence["receipt_sha256"], verify_source=False
    )
    if verified != evidence:
        raise _blocked("original preservation evidence changed")
    return receipt, old_plan


def validate_current_tree(tree: p.Tree, old: dict[str, Any]) -> None:
    """Compare current state without asserting why nlink/ctime differ."""
    tree.require_complete()
    if tree.manifest != old["manifest"] or set(tree.identities) != set(old["source_identities"]):
        raise _blocked("current content, paths or node metadata changed before re-preservation")
    for path, identity in tree.identities.items():
        previous = old["source_identities"][path]
        if any(
            a != b
            for i, (a, b) in enumerate(zip(identity, previous, strict=True))
            if i not in {5, 8}
        ):
            raise _blocked("current inode, size, mode, mtime or ownership changed")


def _directory(store: MetadataStore, transaction: str, recovery_id: str) -> Path:
    for value in (transaction, recovery_id):
        if str(uuid.UUID(value)) != value:
            raise _blocked("noncanonical recovery identity")
    return store.cleanup_recoveries_dir / transaction / recovery_id


def _plan(
    repo: Path,
    store: MetadataStore,
    target: Path,
    owner: str,
    version: int,
    recovery: dict[str, Any],
    receipt_path: Path,
    receipt_sha: str,
) -> dict[str, Any]:
    from .lifecycle import _typed_cleanup_plan_locked
    from .semantics import managed_record as _managed_record

    old_receipt, old_preservation = original(store, recovery, target=target)
    repository = inventory(repo)
    snapshot = store._load_unlocked()
    item, record = _managed_record(repository, snapshot, target)
    verified, new_preservation = coverage(receipt_path, receipt_sha)
    if (
        new_preservation.get("recovery") != recovery
        or new_preservation["generation"] == old_preservation["generation"]
        or not new_preservation.get("cleanup_topology")
        or verified["allocation_source_identity"] != old_preservation["authority"]["identity"]
    ):
        raise _blocked("a new complete re-preservation baseline is required")
    plan = _typed_cleanup_plan_locked(
        repository,
        snapshot,
        store,
        item,
        record,
        owner=owner,
        expected_record_version=version,
        preservation_receipt=receipt_path,
        preservation_receipt_sha256=receipt_sha,
        recovery=recovery,
    )
    if plan["blockers"] or plan["cleanup_paths"] != REMAINING:
        raise _blocked("current release/resource/runtime guards are not clear")
    if plan["hardlink_topology"] != new_preservation["cleanup_topology"]:
        raise _blocked("topology differs from the new preservation baseline")
    old_rows = {r["path"]: r for r in old_receipt["plan"]["resources"]}
    for row in plan["resources"]:
        if row["path"] in REMAINING[:-1] and row["evidence"] != old_rows[row["path"]]["evidence"]:
            raise _blocked("remaining frontend resource identity changed")
    return plan


def prepare(
    repo: Path,
    *,
    target: Path,
    owner: str,
    version: int,
    recovery: dict[str, Any],
    receipt_path: Path,
    receipt_sha: str,
    dry_run: bool,
    expected_plan_id: str | None = None,
) -> dict[str, Any]:
    from .controller import status

    store = MetadataStore(inventory(repo).common_dir)
    decision: dict[str, Any]
    with store.lock(exclusive=not dry_run):
        plan = _plan(repo, store, target, owner, version, recovery, receipt_path, receipt_sha)
        decision = {"code": "RECOVERABLE_PARTIAL", "plan": plan, "dry_run": dry_run}
        if not dry_run:
            if expected_plan_id != plan["plan_id"]:
                raise _blocked("exact fresh recovery dry-run plan required")
            parent = store.cleanup_recoveries_dir / recovery["original_transaction_id"]
            if parent.exists() and list(parent.glob("*/record.json")):
                raise _blocked("an append-only recovery baseline already exists")
            recovery_id = str(uuid.uuid4())
            directory = _directory(store, recovery["original_transaction_id"], recovery_id)
            document = {
                "contract": CONTRACT,
                "recovery_id": recovery_id,
                "result": "RECOVERABLE_PARTIAL",
                "historical_hardlink_cause": "NOT_PROVEN",
                "old_transaction_remains_immutable": True,
                "completed_action_will_replay": False,
                "safety_basis": "CURRENT_STATE_COMPLETELY_REPRESERVED",
                "baseline_timestamp": now_iso(),
                "plan": plan,
                "old_preservation_generation": original(store, recovery)[1]["destination"],
                "new_execution_baseline": str(receipt_path.parent),
                "hardlink_topology_sha256": plan["hardlink_topology"]["sha256"],
            }
            reference = e.write(directory, "record.json", document)
            decision.update(recovery_id=recovery_id, recovery_receipt=reference)
    result = status(repo)
    result.update(command="resource-recover", decision=decision)
    return result


def _rollout_transaction(
    repo: Path, store: MetadataStore, path: Path, journal: dict,
    receipt_path: Path | None, receipt_sha: str | None,
) -> dict[str, Any]:
    from .transaction_scope import terminal_state

    state = terminal_state(store, path.stem, verify_generations=True)
    if state is not None:
        return {"transaction_id": path.stem, "state": state}
    if journal.get("command") == "release":
        raise ControllerError(
            "ROLLOUT_RELEASE_RECOVERY_REQUIRED",
            "unfinished release requires exact replay through release with the original request identity",
        )
    cleanup_plan = journal.get("cleanup_plan")
    if journal.get("command") != "resource-clean" or not isinstance(cleanup_plan, dict):
        raise ControllerError(
            "ROLLOUT_TRANSACTION_UNSUPPORTED", "unfinished transaction has no supported cleanup recovery"
        )
    contract = cleanup_plan.get("contract")
    typed_prefix = contract == "typed-disposable-resource/v1" and any(
        row.get("status") == "COMPLETED" for row in journal.get("actions", [])
    )
    if contract == "typed-disposable-resource/v1" and not typed_prefix:
        _, digest = e.read(path)
        current = attempts.typed_plan(repo, store, path.stem, digest)
        return {"transaction_id": path.stem, "state": "TYPED_NO_ACTION_RECOVERABLE",
                "plan_id": current["plan_id"]}
    if contract == "tracked-source-cleanup/v1":
        from .source_recovery import plan_reconciliation

        completion_path = store.completion_receipts_dir / f"{path.stem}.json"
        _, completion_sha = e.read(completion_path)
        current = plan_reconciliation(repo, path.stem, completion_sha)
        return {
            "transaction_id": path.stem,
            "state": current["state"],
            "plan_id": current["plan_id"],
            "completion_receipt_sha256": completion_sha,
        }
    if contract == "legacy-preserved-state-multiple-cleanup/v1" or typed_prefix:
        from . import multiple_cleanup_recovery as multi_recovery

        _, journal_sha = e.read(path)
        _, completion_sha = e.read(store.completion_receipts_dir / path.name)
        recovery = multi_recovery.context(path.stem, journal_sha, completion_sha)
        current = multi_recovery._plan_locked(
            repo, store, Path(cleanup_plan["target_path"]), cleanup_plan["owner"],
            cleanup_plan["expected_record_version"], recovery,
        )
        return {
            "transaction_id": path.stem,
            "state": "MULTIPLE_PREFIX_RECOVERABLE",
            "plan_id": current["plan_id"],
            "original_transaction_sha256": journal_sha,
            "original_cleanup_receipt_sha256": completion_sha,
            "hardlink_topology_sha256": current["hardlink_topology"]["sha256"],
        }
    if contract != "legacy-preserved-state-cleanup/v1":
        raise ControllerError(
            "ROLLOUT_TRANSACTION_UNSUPPORTED", "unfinished cleanup has an unsupported recovery contract"
        )
    if receipt_path is None or not receipt_sha:
        raise _blocked("unfinished cleanup requires an exact new preservation baseline")
    _, baseline = coverage(receipt_path, receipt_sha)
    recovery = baseline.get("recovery", {})
    if recovery.get("original_transaction_id") != path.stem:
        raise _blocked("preservation baseline does not cover this transaction")
    identity = baseline["authority"]["identity"]
    plan = _plan(
        repo, store, Path(identity["target_path"]), identity["owner"],
        identity["expected_record_version"], recovery, receipt_path, receipt_sha,
    )
    return {
        "transaction_id": path.stem,
        "state": "RECOVERABLE_PARTIAL",
        "original_transaction_sha256": recovery["original_transaction_sha256"],
        "preservation_receipt_sha256": receipt_sha,
        "hardlink_topology_sha256": plan["hardlink_topology"]["sha256"],
    }


def rollout_check(
    repo: Path, *, receipt_path: Path | None = None, receipt_sha: str | None = None
) -> dict[str, Any]:
    """Read-only installation compatibility, never a transaction state transition."""
    from .controller import status
    from .transaction_scope import is_atomic_temp

    store = MetadataStore(inventory(repo).common_dir)
    verified = []
    with store.lock(exclusive=False):
        pending = sorted(store.transactions_dir.iterdir()) if store.transactions_dir.exists() else []
        for path in pending:
            if is_atomic_temp(path.name):
                continue
            journal: dict = {}
            try:
                if not path.is_file() or path.suffix != ".json":
                    raise ControllerError(
                        "ROLLOUT_TRANSACTION_UNSUPPORTED", "unknown transaction entry blocks activation"
                    )
                journal, _ = e.read(path)
                verified.append(_rollout_transaction(repo, store, path, journal, receipt_path, receipt_sha))
            except (ControllerError, OSError) as exc:
                raise RolloutTransactionError(exc, path.stem, journal) from exc
            except (ValueError, TypeError, KeyError) as exc:
                cause = ControllerError(
                    "ROLLOUT_TRANSACTION_UNSUPPORTED", f"transaction recovery evidence is malformed: {exc}"
                )
                raise RolloutTransactionError(cause, path.stem, journal) from exc
    result = status(repo)
    result.update(
        command="resource-recover",
        decision={
            "code": "ROLLOUT_RECOVERY_COMPATIBLE",
            "dry_run": True,
            "transactions": verified,
        },
    )
    return result


def _record(
    store: MetadataStore, recovery_path: Path, recovery_sha: str
) -> tuple[dict[str, Any], Path]:
    document, _ = e.read(recovery_path, recovery_sha)
    plan = document["plan"]
    recovery = plan["recovery"]
    directory = _directory(store, recovery["original_transaction_id"], document["recovery_id"])
    if (
        recovery_path != directory / "record.json"
        or document.get("result") != "RECOVERABLE_PARTIAL"
        or document.get("contract") != CONTRACT
        or document.get("historical_hardlink_cause") != "NOT_PROVEN"
        or document.get("completed_action_will_replay") is not False
        or document.get("safety_basis") != "CURRENT_STATE_COMPLETELY_REPRESERVED"
    ):
        raise _blocked("invalid immutable recovery record")
    return document, directory


def _current(repo: Path, store: MetadataStore, document: dict[str, Any]) -> dict[str, Any]:
    plan = document["plan"]
    preserved = plan["preservation_evidence"]
    current = _plan(
        repo,
        store,
        Path(plan["target_path"]),
        plan["owner"],
        plan["expected_record_version"],
        plan["recovery"],
        Path(preserved["receipt_path"]),
        preserved["receipt_sha256"],
    )
    if p._sha(current) != p._sha(plan):
        raise _blocked("current baseline differs from the immutable recovery record")
    return current


def reconcile_attempt(
    repo: Path,
    *,
    recovery_path: Path,
    recovery_sha: str,
    dry_run: bool,
    expected_plan_id: str | None = None,
) -> dict[str, Any]:
    from .controller import status

    store = MetadataStore(inventory(repo).common_dir)
    with store.lock(exclusive=not dry_run):
        document, directory = _record(store, recovery_path, recovery_sha)
        chain = attempts.chain(directory, recovery_sha)
        if not chain or (directory / "complete.json").exists():
            raise _blocked("an unfinished started attempt is required")
        latest = chain[-1]
        path = directory / "reconciliations" / (latest["attempt_id"] + ".json")
        if path.exists():
            raise _blocked("attempt already reconciled; use the new resume operation")
        current = _current(repo, store, document)
        proof = attempts.no_action_evidence(store, current)
        plan = {
            "contract": attempts.RECONCILIATION_CONTRACT,
            "state": "STARTED_NO_ACTION_RECOVERABLE",
            "path": str(path),
            "record_sha256": recovery_sha,
            "attempt": latest,
            "allocation_id": current["allocation_id"],
            "original_transaction_id": current["recovery"]["original_transaction_id"],
            "completed_resources": [COMPLETED_RESOURCE],
            "remaining_resources": REMAINING,
            "completed_action_will_replay": False,
            "assertion": "attempt started, no action proven, safe to supersede",
            "safe_to_supersede": True,
            "historical_refusal": "NOT_RECONSTRUCTED",
            "verification": {
                "filesystem": "EXACT_BASELINE_MATCH",
                "preservation": "PASS",
                "runtime_authority": "PASS",
                "git": "CLEAN",
                **proof,
            },
            "hardlink_topology_sha256": current["hardlink_topology"]["sha256"],
            "baseline_plan_id": current["plan_id"],
            "runtime_evidence": current["runtime_evidence"],
            "warnings": current["warnings"],
            "evidence": attempts.evidence_references(store, directory, current),
        }
        plan_id = p._sha(plan)
        decision = {
            "code": "STARTED_NO_ACTION_RECOVERABLE",
            "dry_run": dry_run,
            "plan_id": plan_id,
            "plan": plan,
        }
        if not dry_run:
            if expected_plan_id != plan_id:
                raise _blocked("exact fresh no-action reconciliation plan required")
            decision["reconciliation"] = e.write(
                path.parent, path.name, {**plan, "plan_id": plan_id, "timestamp": now_iso()}
            )
    result = status(repo)
    result.update(command="resource-recover", decision=decision)
    return result


def _incomplete(
    repo: Path,
    store: MetadataStore,
    document: dict[str, Any],
    directory: Path,
    attempt_id: str,
    underlying: dict[str, Any],
    coordinator_error: Exception | None = None,
) -> dict[str, Any]:
    from .controller import envelope

    # Persist the primary refusal BEFORE any optional diagnostic/coordinator work.
    evidence = None
    evidence_error = None
    try:
        evidence = e.write(
            directory,
            "underlying.json",
            {
                "contract": attempts.CONTRACT,
                "attempt_id": attempt_id,
                "underlying": underlying,
                "timestamp": now_iso(),
            },
        )
    except Exception as exc:
        evidence_error = attempts.underlying(exception=exc)["exception"]
    phase = "PARTIAL_ACTION"
    diagnostic = None
    try:
        with store.lock(exclusive=False):
            _current(repo, store, document)
            attempts.no_action_evidence(store, document["plan"])
        phase = "ATTEMPT_FAILED_BEFORE_ACTION"
    except Exception as exc:
        diagnostic = attempts.underlying(exception=exc)["exception"]
        with suppress(Exception):  # Keep the primary refusal and failed no-action verification.
            phase = attempts.action_state(store, attempt_id) or phase
    decision = {
        "code": "RECOVERY_ATTEMPT_INCOMPLETE",
        "attempt_id": attempt_id,
        "phase": phase,
        "underlying": underlying,
        "underlying_evidence": evidence,
        "evidence_error": evidence_error,
        "coordinator_error": attempts.underlying(exception=coordinator_error)["exception"]
        if coordinator_error
        else None,
        "no_action_verification_error": diagnostic,
        "receipt_id": underlying.get("completion_receipt_id"),
        "allocation_released": False,
    }
    try:
        decision["attempt_result"] = e.write(
            directory,
            "result.json",
            {
                "contract": attempts.CONTRACT,
                **decision,
                "timestamp": now_iso(),
            },
        )
    except Exception as exc:
        decision["result_evidence_error"] = attempts.underlying(exception=exc)["exception"]
    result = envelope("resource-recover")
    result.update(decision=decision, errors=underlying["errors"])
    return result


def resume(repo: Path, *, recovery_path: Path, recovery_sha: str, dry_run: bool) -> dict[str, Any]:
    from .controller import status
    from .lifecycle import cleanup_typed_disposable

    store = MetadataStore(inventory(repo).common_dir)
    with store.lock(exclusive=False):
        candidate, _ = e.read(recovery_path, recovery_sha)
    from . import multiple_cleanup_recovery as multi_recovery

    if candidate.get("contract") == multi_recovery.CONTRACT:
        return multi_recovery.resume(
            repo, recovery_path=recovery_path, recovery_sha=recovery_sha, dry_run=dry_run
        )
    with store.lock(exclusive=not dry_run):
        document, directory = _record(store, recovery_path, recovery_sha)
        supersedes = attempts.authorization(directory, recovery_sha)
        _current(repo, store, document)
        attempts.no_action_evidence(store, document["plan"])
        plan = document["plan"]
        attempt_id = str(uuid.uuid4())
        attempt_directory = directory / "attempts" / attempt_id
        if not dry_run:
            started = e.write(
                attempt_directory,
                "started.json",
                {
                    "contract": attempts.CONTRACT,
                    "phase": "ATTEMPT_STARTED",
                    "attempt_id": attempt_id,
                    "record_sha256": recovery_sha,
                    "supersedes": supersedes,
                    "timestamp": now_iso(),
                },
            )
    recovery = plan["recovery"]
    executing = {
        **recovery,
        "recovery_id": document["recovery_id"],
        "recovery_record_sha256": recovery_sha,
        "attempt_id": attempt_id,
    }
    preserved = plan["preservation_evidence"]
    options = dict(
        target=Path(plan["target_path"]),
        owner=plan["owner"],
        expected_record_version=plan["expected_record_version"],
        preservation_receipt=Path(preserved["receipt_path"]),
        preservation_receipt_sha256=preserved["receipt_sha256"],
        recovery=executing,
    )
    try:
        planned = cleanup_typed_disposable(repo, **options, dry_run=True)
        if (
            planned["decision"]["plan"]["blockers"]
            or planned["decision"]["plan"]["cleanup_paths"] != REMAINING
        ):
            if dry_run:
                planned["command"] = "resource-recover"
                return planned
            return _incomplete(
                repo, store, document, attempt_directory, attempt_id, attempts.underlying(planned)
            )
        if dry_run:
            planned["command"] = "resource-recover"
            planned["decision"].update(code="RECOVERABLE_PARTIAL", attempt_id=attempt_id)
            return planned
        cleaned = cleanup_typed_disposable(
            repo, **options, dry_run=False, expected_plan_id=planned["decision"]["plan"]["plan_id"]
        )
    except Exception as exc:
        if not dry_run:
            try:
                _incomplete(
                    repo,
                    store,
                    document,
                    attempt_directory,
                    attempt_id,
                    attempts.underlying(exception=exc),
                )
            except Exception as secondary:
                raise exc from secondary  # Failure reporting must not replace the primary cause.
        raise
    underlying = attempts.underlying(cleaned)
    completion_id = underlying["completion_receipt_id"]
    if not completion_id:
        return _incomplete(repo, store, document, attempt_directory, attempt_id, underlying)
    try:
        if str(uuid.UUID(completion_id)) != completion_id:
            raise _blocked("invalid completion receipt identity")
        with store.lock(exclusive=True):
            original(store, recovery)
            done, digest = e.read(store.completion_receipts_dir / (completion_id + ".json"))
            if done.get("result") != "CLEANED" or done.get("plan", {}).get("recovery") != executing:
                raise _blocked("completion receipt is not a successful exact attempt")
            reference = e.write(
                directory,
                "complete.json",
                {
                    "contract": CONTRACT,
                    "result": "CLEANED",
                    "original_transaction_id": recovery["original_transaction_id"],
                    "record_sha256": recovery_sha,
                    "attempt_id": attempt_id,
                    "attempt_started": started,
                    "completion_receipt_id": completion_id,
                    "completion_receipt_sha256": digest,
                    "timestamp": now_iso(),
                },
            )
            if not completed(store, recovery["original_transaction_id"], verify_generations=True):
                raise _blocked("terminal recovery evidence verification failed")
        e.write(
            attempt_directory,
            "result.json",
            {
                "contract": attempts.CONTRACT,
                "attempt_id": attempt_id,
                "phase": "ACTION_COMPLETED",
                "completion": reference,
                "underlying": underlying,
                "timestamp": now_iso(),
            },
        )
    except Exception as exc:
        return _incomplete(repo, store, document, attempt_directory, attempt_id, underlying, exc)
    result = status(repo)
    result.update(
        command="resource-recover",
        decision={
            "code": "RECOVERY_CLEANED",
            "recovery_id": document["recovery_id"],
            "attempt_id": attempt_id,
            "receipt_id": completion_id,
            "recovery_completion": reference,
        },
    )
    return result


def completed(store: MetadataStore, transaction: str, *, verify_generations: bool = False) -> bool:
    """A retained historical journal is terminal only through exact append-only evidence."""
    parent = store.cleanup_recoveries_dir / transaction
    if not parent.exists():
        return False
    terminals = list(parent.glob("*/complete.json"))
    if not terminals:
        return False
    if len(terminals) != 1:
        raise _blocked("ambiguous recovery completion")
    terminal, _ = e.read(terminals[0])
    from . import multiple_cleanup_recovery as multi_recovery

    if terminal.get("contract") == multi_recovery.CONTRACT:
        return multi_recovery.completed(
            store, transaction, verify_generations=verify_generations
        )
    record, _ = e.read(terminals[0].parent / "record.json", terminal["record_sha256"])
    plan = record["plan"]
    recovery = plan["recovery"]
    if (
        terminal.get("contract") != CONTRACT
        or terminal.get("result") != "CLEANED"
        or terminal.get("original_transaction_id") != transaction
        or recovery["original_transaction_id"] != transaction
        or record.get("historical_hardlink_cause") != "NOT_PROVEN"
        or record.get("completed_action_will_replay") is not False
    ):
        raise _blocked("invalid recovery terminal binding")
    old, _ = e.read(
        store.completion_receipts_dir / f"{transaction}.json",
        recovery["original_cleanup_receipt_sha256"],
    )
    e.read(store.transactions_dir / f"{transaction}.json", recovery["original_transaction_sha256"])
    done, _ = e.read(
        store.completion_receipts_dir / (terminal["completion_receipt_id"] + ".json"),
        terminal["completion_receipt_sha256"],
    )
    expected_actions = [{"path": name, "status": "COMPLETED"} for name in REMAINING]
    bound = {
        **recovery,
        "recovery_id": record["recovery_id"],
        "recovery_record_sha256": terminal["record_sha256"],
    }
    if terminal.get("attempt_id"):
        succession = attempts.chain(terminals[0].parent, terminal["record_sha256"])
        if (
            not succession
            or succession[-1]["attempt_id"] != terminal["attempt_id"]
            or succession[-1]["started"] != terminal["attempt_started"]
        ):
            raise _blocked("terminal does not bind the latest unique attempt")
        bound["attempt_id"] = terminal["attempt_id"]
    if (
        done.get("result") != "CLEANED"
        or done.get("actions") != expected_actions
        or done["plan"].get("recovery") != bound
        or done["plan"]["allocation_id"] != old["plan"]["allocation_id"]
        or done["post_scan"]["blockers"]
        or done["post_scan"]["cleanup_paths"]
        or done["plan"]["preservation_evidence"] != plan["preservation_evidence"]
    ):
        raise _blocked("new completion does not cover exactly the remaining actions")
    graph = plan["hardlink_topology"]
    for step, action in enumerate(done.get("action_evidence", [])):
        before, _ = e.read(Path(action["before"]["path"]), action["before"]["sha256"])
        after, _ = e.read(Path(action["after"]["path"]), action["after"]["sha256"])
        if (
            before["topology"] != graph
            or before["resource"] != REMAINING[step]
            or after["before"] != action["before"]
        ):
            raise _blocked("action evidence chain differs")
        if (
            hardlinks.transition(
                graph,
                after["topology"],
                REMAINING[step],
                after["mutation"].get("external_survivors"),
            )
            != after["mutation"]
        ):
            raise _blocked("unexpected action effects")
        graph = after["topology"]
    if graph["resources"] or len(done.get("action_evidence", [])) != len(REMAINING):
        raise _blocked("incomplete immutable action evidence")
    target = Path(plan["target_path"])
    if any(os.path.lexists(target / name) for name in [COMPLETED_RESOURCE, *REMAINING]):
        raise _blocked("completed cleanup resource reappeared")
    if verify_generations:
        for evidence in [old["plan"]["preservation_evidence"], plan["preservation_evidence"]]:
            verified, _ = coverage(
                Path(evidence["receipt_path"]), evidence["receipt_sha256"], verify_source=False
            )
            if verified != evidence:
                raise _blocked("preserved generation changed after recovery")
    return True
