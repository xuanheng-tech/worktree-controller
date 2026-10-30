"""Resume only a proven completed prefix's untouched suffix, with immutable evidence."""

from __future__ import annotations

import os
import stat
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import cleanup_evidence as e
from . import preservation as p
from .git_backend import inventory
from .models import ControllerError, MetadataRecord
from .preserved_cleanup import (
    common_source_root,
    completed_actions,
    coverage,
    source_absent,
)
from .store import MetadataStore, now_iso, record_from_dict, record_to_dict

CONTRACT = "multiple-preservation-cleanup-recovery/v1"
CLEANUP_CONTRACT = "legacy-preserved-state-multiple-cleanup/v1"
TYPED_CONTRACT = "typed-disposable-resource/v1"
IDENTITY_KEYS = (
    "repository_id", "allocation_id", "target_path", "expected_head",
    "expected_record_version", "owner", "authority_key",
)


def _blocked(message: str) -> ControllerError:
    return ControllerError("MULTIPLE_CLEANUP_RECOVERY_BLOCKED", message)


def _uuid(value: str) -> None:
    try:
        valid = str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise _blocked("canonical recovery transaction identity required")


def context(transaction: str, transaction_sha256: str, receipt_sha256: str) -> dict[str, Any]:
    _uuid(transaction)
    if any(not isinstance(value, str) or len(value) != 64 or set(value) - set("0123456789abcdef")
           for value in (transaction_sha256, receipt_sha256)):
        raise _blocked("trusted original journal and failed receipt digests required")
    return {
        "contract": CONTRACT,
        "original_transaction_id": transaction,
        "original_transaction_sha256": transaction_sha256,
        "original_cleanup_receipt_sha256": receipt_sha256,
        "old_transaction_remains_immutable": True,
        "completed_action_will_replay": False,
    }


def _identity(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: plan[key] for key in IDENTITY_KEYS}


def _plan_hash(plan: dict[str, Any]) -> bool:
    from .lifecycle import _release_plan_id

    return plan.get("plan_id") == _release_plan_id({key: value for key, value in plan.items() if key != "plan_id"})


def _action_files(store: MetadataStore, transaction: str, count: int) -> None:
    with p._absolute_directory(store.cleanup_actions_dir / transaction) as chain:
        names = set(os.listdir(chain[-1].fd))
    required = {f"{step:04d}-{phase}.json" for step in range(count) for phase in ("before", "after")}
    if names != required:
        raise _blocked("pending or unbound actions have evidence, or completed action files are incomplete")


def _generation_metadata(bound: dict[str, Any]) -> list[dict[str, Any]]:
    """Freeze completed official generations without reading unreferenced payloads."""
    for key in ("repository_id", "allocation_id"):
        _uuid(bound[key])
    allocation = p.evidence_root() / bound["repository_id"] / bound["allocation_id"]
    result = []
    with p._absolute_directory(allocation) as chain:
        for name in sorted(os.listdir(chain[-1].fd)):
            _uuid(name)
            with p._absolute_directory(allocation / name) as generation:
                directory = generation[-1]
                try:
                    os.stat("receipt.json", dir_fd=directory.fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue  # Incomplete copies retain the existing no-authority rule.
                files = []
                for filename in ("plan.json", "receipt.json", "manifest.json"):
                    try:
                        os.stat(filename, dir_fd=directory.fd, follow_symlinks=False)
                    except FileNotFoundError:
                        if filename != "manifest.json":
                            raise _blocked("completed generation metadata is missing") from None
                        continue
                    metadata, digest = e.read_document(directory, filename)
                    if filename == "receipt.json" and (
                        metadata.get("receipt_id") != name
                        or metadata.get("generation") != str(allocation / name)
                        or metadata.get("generation_identity") != directory.identity
                    ):
                        raise _blocked("completed generation receipt or original directory identity changed")
                    files.append({"path": str(allocation / name / filename), "sha256": digest})
                result.append({"generation": name, "files": files})
    return result


def _frozen_authority(record: dict[str, Any]) -> MetadataRecord:
    frozen = record_from_dict(record["allocation_record"])
    baseline = record["plan"]
    if (frozen.allocation_id != baseline["allocation_id"] or frozen.owner != baseline["owner"]
            or frozen.authority_key != baseline["authority_key"]
            or frozen.record_version != baseline["expected_record_version"]
            or not frozen.history or frozen.history[-1].get("to") != frozen.lifecycle
            or record.get("generation_metadata") != _generation_metadata(_identity(baseline))):
        raise _blocked("frozen allocation lineage or complete generation metadata set changed")
    for generation in record["generation_metadata"]:
        for ref in generation["files"]:
            e.read(Path(ref["path"]), ref["sha256"])
    return frozen


def _released_record(frozen: MetadataRecord, current: MetadataRecord,
                     release_plan: dict[str, Any]) -> bool:
    event = current.history[-1]
    if (set(event) != {"at", "event", "from", "to", "owner"}
            or event.get("event") != "RELEASED" or event.get("from") != frozen.lifecycle
            or event.get("to") != "RELEASED" or event.get("owner") != "unassigned"
            or release_plan.get("lifecycle") != frozen.lifecycle
            or release_plan.get("role") != frozen.role
            or release_plan.get("expected_record_version") != frozen.record_version):
        return False
    resources = [dict(resource) for resource in frozen.disposable_resources]
    cleaned = {action["resource_id"] for action in release_plan["resource_actions"]}
    for resource in resources:
        if resource["resource_id"] in cleaned:
            resource["state"] = "cleaned"
    expected = replace(frozen, lifecycle="RELEASED", owner="unassigned",
        record_version=frozen.record_version + 1, last_reviewed_at=event["at"],
        history=(*frozen.history, event), disposable_resources=tuple(resources))
    return record_to_dict(current) == record_to_dict(expected)


def _released(store: MetadataStore, transaction: str) -> bool:
    from .transaction_scope import release_evidence_terminal

    if not release_evidence_terminal(store, transaction):
        return False
    journal, _ = e.read(store.transactions_dir / f"{transaction}.json")
    cleanup = journal["cleanup_plan"]
    terminals = list((store.cleanup_recoveries_dir / transaction).glob("*/complete.json"))
    if len(terminals) != 1:
        return False
    terminal, _ = e.read(terminals[0])
    baseline, _ = e.read(terminals[0].parent / "record.json", terminal["record_sha256"])
    frozen = _frozen_authority(baseline)
    recovery = baseline["plan"]["recovery"]
    if (terminal.get("contract") != CONTRACT or terminal.get("original_transaction_id") != transaction
            or recovery["original_transaction_id"] != transaction):
        return False
    e.read(store.transactions_dir / f"{transaction}.json", recovery["original_transaction_sha256"])
    e.read(store.completion_receipts_dir / f"{transaction}.json", recovery["original_cleanup_receipt_sha256"])
    record = record_from_dict(store._read_json(store.records_dir / f"{cleanup['allocation_id']}.json"))
    releases = [e.read(path)[0] for path in store.release_receipts_dir.glob("*.json")]
    matches = [receipt for receipt in releases if (
        receipt.get("allocation_id") == record.allocation_id
        and receipt.get("released_record_version") == record.record_version
        and receipt.get("result") == "RELEASED"
    )]
    if len(matches) != 1:
        return False
    release = matches[0]
    release_plan, _ = e.read(store.release_plans_dir / f"{release['plan_id']}.json")
    if (not _plan_hash(release_plan)
            or record.authority_key != cleanup["authority_key"]
            or release.get("released_head") != cleanup["expected_head"]
            or release.get("branch") != journal["branch"]
            or release.get("released_record_version") != release.get("expected_record_version", -1) + 1
            or any(release_plan.get(key) != cleanup[key] for key in IDENTITY_KEYS if key != "expected_record_version")
            or any(release_plan.get(key) != release.get(key) for key in (
                "repository_id", "allocation_id", "target_path", "branch", "expected_record_version"))
            or release_plan.get("expected_head") != release["released_head"]
            or not record.history or record.history[-1].get("event") != "RELEASED"
            or record.history[-1].get("from") != release_plan.get("lifecycle")
            or record.history[-1].get("to") != "RELEASED"):
        return False
    if release["expected_record_version"] == cleanup["expected_record_version"]:
        return _released_record(frozen, record, release_plan)
    # A retained checkout may advance exactly once through an archive whose
    # frozen cleanup references bind this successful recovery receipt.
    from .delivered_closeout import ARCHIVE_CONTRACT, ARCHIVE_EVENT, ARCHIVE_STATE, _digest

    if (release["expected_record_version"] != cleanup["expected_record_version"] + 1
            or release_plan.get("lifecycle") != ARCHIVE_STATE or len(record.history) < 2):
        return False
    event = record.history[-2]
    archive, _ = e.read(store.completion_receipts_dir / f"{event['archive_receipt_id']}.json")
    archived_plan = archive["plan"]
    original = record_from_dict(archived_plan["record"])
    archived = replace(original, lifecycle=ARCHIVE_STATE, record_version=original.record_version + 1,
                       last_reviewed_at=event["at"], history=(*original.history, event))
    completion_path = store.completion_receipts_dir / f"{terminal['completion_receipt_id']}.json"
    references = archived_plan.get("preservation_cleanup_receipts", [])
    if (archive.get("contract") != ARCHIVE_CONTRACT or archive.get("receipt_id") != event["archive_receipt_id"]
            or archive.get("plan_id") != _digest(archived_plan) or event.get("event") != ARCHIVE_EVENT
            or event.get("from") != "RETAINED" or event.get("to") != ARCHIVE_STATE
            or event.get("owner") != cleanup["owner"] or event.get("archive_plan_id") != archive["plan_id"]
            or event.get("archive_receipt_sha256") != _digest({key: value for key, value in archive.items() if key != "after_record"})
            or archive.get("after_record") != record_to_dict(archived)
            or list(archived.history) != list(record.history[:-1])
            or record_to_dict(original) != record_to_dict(frozen)
            or original.record_version != cleanup["expected_record_version"] or original.lifecycle != "RETAINED"
            or original.owner != cleanup["owner"] or original.authority_key != cleanup["authority_key"]
            or original.allocation_id != cleanup["allocation_id"]
            or archived_plan.get("repository_id") != cleanup["repository_id"]
            or archived_plan.get("target_path") != cleanup["target_path"]
            or archived_plan.get("head") != cleanup["expected_head"] or archived_plan.get("branch") != journal["branch"]
            or archived_plan.get("owner_release") != cleanup["owner"]
            or any(archived_plan.get(key) is not False for key in ("task_completed", "integration_ready", "publication_asserted"))
            or not any(ref.get("receipt_id") == terminal["completion_receipt_id"]
                       and ref.get("sha256") == terminal["completion_receipt_sha256"]
                       and e.matches_reference(ref, completion_path) for ref in references)):
        return False
    for ref in references:
        e.read(Path(ref["path"]), ref["sha256"])
    return _released_record(archived, record, release_plan)


def _source_absent(store: MetadataStore, source_plan: dict[str, Any], target: Path,
                   transaction: str) -> bool:
    if os.path.lexists(target):
        return source_absent(source_plan, target)
    return _released(store, transaction)


def _common_root(store: MetadataStore, plans: list[dict[str, Any]], target: Path,
                 transaction: str) -> dict[str, int]:
    if os.path.lexists(target):
        return common_source_root(plans, target)
    if not _released(store, transaction):
        raise _blocked("absent checkout has no exact official release evidence")
    pins = []
    for plan in plans:
        relative = p.source_relative_path(Path(plan["source"]).relative_to(target).as_posix())
        if "source_ancestor_identities" in plan:
            pin = plan["source_ancestor_identities"].get(str(target))
        elif len(Path(relative).parts) == 1:
            pin = plan["source_anchor"]["worktree"]
        else:
            pin = None
        if pin is None or set(pin) != {"device", "inode", "file_type"} or pin["file_type"] != stat.S_IFDIR:
            raise _blocked("missing or invalid original checkout root pin")
        pins.append(pin)
    if not pins or any(pin != pins[0] for pin in pins):
        raise _blocked("preservation generations have different original checkout roots")
    return pins[0]


def _proofset(plan: dict[str, Any], target: Path) -> list[dict[str, Any]]:
    proofs = plan["preservation_evidences"]
    names = [row["source_relative"] for row in proofs]
    if (not isinstance(proofs, list) or len(proofs) < 2 or names != sorted(set(names))
            or plan.get("preservation_evidence") != proofs[0]["evidence"]):
        raise _blocked("the complete ordered multiple preservation proofset is required")
    preserved = []
    for row in proofs:
        relative = p.source_relative_path(row["source_relative"])
        if any(Path(relative) in Path(other).parents for other in names if other != relative):
            raise _blocked("preserved sources overlap")
        evidence = row["evidence"]
        verified, source_plan = coverage(
            Path(evidence["receipt_path"]), evidence["receipt_sha256"],
            identity=_identity(plan), verify_source=False,
        )
        if verified != evidence or Path(source_plan["source"]).relative_to(target).as_posix() != relative:
            raise _blocked("original preservation proof identity changed")
        preserved.append(source_plan)
    return preserved


def _typed_preservations(store, plan, recovery):
    from .build_preservation import generations

    record = record_from_dict(store._read_json(store.records_dir / f"{plan['allocation_id']}.json"))
    if recovery.get("recovery_id"):
        directory = _directory(store, recovery["original_transaction_id"], recovery["recovery_id"])
        frozen, _ = e.read(directory / "record.json", recovery["recovery_record_sha256"])
        record = _frozen_authority(frozen)
    rows = [row for row in plan["resources"] if row["path"] in plan["cleanup_paths"]]
    registration = record.history[-1].get("build_resources")
    if (not registration or plan.get("preservation_evidence") is not None
            or "preservation_evidences" in plan
            or any(row.get("cleanup_action") != "none" or row.get("blocker") is not None
                   or row.get("classification") != "existing-release-policy"
                   for row in plan["resources"] if row["path"] not in plan["cleanup_paths"])
            or any(row.get("resource_type") != "registered-build-output"
                   or row.get("classification") != "disposable"
                   or row.get("cleanup_action") != "remove-tree"
                   or row.get("evidence", {}).get("build_declaration") != {
                       "registration": registration, "path": row["path"]} for row in rows)):
        raise _blocked("typed prefix recovery requires exact registered build outputs")
    entries, preserved = generations(_identity(plan), record, topology=plan["hardlink_topology"],
                                    verify_live=False)
    if not preserved or ("build_preservation_evidences" in plan
                         and entries != plan["build_preservation_evidences"]):
        raise _blocked("typed recovery requires verified original build preservation lineage")
    return preserved


def details(
    store: MetadataStore, recovery: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], set[str], dict[str, Any]]:
    """Verify historical bytes and prefix effects; current admission is checked separately."""
    try:
        expected = context(recovery["original_transaction_id"], recovery["original_transaction_sha256"],
                           recovery["original_cleanup_receipt_sha256"])
        if any(recovery.get(key) != value for key, value in expected.items()):
            raise _blocked("recovery history differs from its supported contract")
        if set(recovery) - (set(expected) | {"recovery_id", "recovery_record_sha256"}):
            raise _blocked("unknown recovery history assertions are not supported")
        if ("recovery_id" in recovery) != ("recovery_record_sha256" in recovery):
            raise _blocked("executing recovery requires both exact immutable record bindings")
        if "recovery_id" in recovery:
            _uuid(recovery["recovery_id"])
            digest = recovery["recovery_record_sha256"]
            if not isinstance(digest, str) or len(digest) != 64 or set(digest) - set("0123456789abcdef"):
                raise _blocked("executing recovery record digest is invalid")
        transaction = recovery["original_transaction_id"]
        journal, _ = e.read(store.transactions_dir / f"{transaction}.json",
                            recovery["original_transaction_sha256"])
        receipt, _ = e.read(store.completion_receipts_dir / f"{transaction}.json",
                            recovery["original_cleanup_receipt_sha256"])
        plan = receipt["plan"]
        target = Path(plan["target_path"])
        identity = _identity(plan)
        paths = plan["cleanup_paths"]
        actions = receipt["actions"]
        prefix = [row["path"] for row in actions if row["status"] == "COMPLETED"]
        shape = [{"path": name, "status": "COMPLETED"} for name in prefix] + [
            {"path": name, "status": "PENDING"} for name in paths[len(prefix):]
        ]
        rows = plan["resources"]
        planned_paths = [row["path"] for row in rows if row["cleanup_action"] == "remove-tree"] + [
            row["path"] for row in rows if row["cleanup_action"] == "remove-preserved-tree"
        ]
        if (receipt.get("receipt_id") != transaction or receipt.get("result") != "FAILED_OR_PARTIAL"
                or plan.get("contract") not in {CLEANUP_CONTRACT, TYPED_CONTRACT}
                or receipt.get("contract") != plan.get("contract")
                or not _plan_hash(plan) or plan.get("blockers") != [] or not target.is_absolute()
                or not paths or paths != planned_paths or len(set(paths)) != len(paths)
                or not prefix or len(prefix) >= len(paths) or prefix != paths[:len(prefix)]
                or actions != shape or journal.get("actions") != shape
                or journal.get("transaction_id") != transaction or journal.get("command") != "resource-clean"
                or journal.get("phase") != "PREPARED" or journal.get("schema_version") not in {1, 2, 3, 4}
                or journal.get("cleanup_plan") != plan or not isinstance(journal.get("branch"), str)
                or not journal["branch"] or receipt.get("failure") != journal.get("failure")
                or any(journal.get(key) != identity[key] for key in IDENTITY_KEYS[:5])
                or identity["repository_id"] != store._read_json(store.root / "repo.json")["repository_id"]):
            raise _blocked("only an exact COMPLETED prefix and untouched PENDING suffix are supported")
        for name in paths:
            p.source_relative_path(name)
        proofs = tuple(receipt["action_evidence"])
        if len(proofs) != len(prefix):
            raise _blocked("every completed original action needs exact before and after evidence")
        done, topology = completed_actions(
            store, transaction, proofs, initial_topology=plan["hardlink_topology"], cleanup_paths=paths,
        )
        _action_files(store, transaction, len(prefix))
        if done != set(prefix):
            raise _blocked("pending actions have evidence or original action files are incomplete")
        if plan["contract"] == TYPED_CONTRACT:
            preserved = _typed_preservations(store, plan, recovery)
            root = _common_root(store, preserved, target, transaction)
            if any(anchor["worktree"] != root for anchor in plan["hardlink_topology"]["anchors"].values()):
                raise _blocked("original typed graph belongs to another checkout root")
            if any(os.path.lexists(target / name) for name in done):
                raise _blocked("completed typed output reappeared")
            return receipt, preserved, done, topology
        preserved = _proofset(plan, target)
        preserved_rows = {row["path"]: row for row in rows if row["cleanup_action"] == "remove-preserved-tree"}
        if set(preserved_rows) != {row["source_relative"] for row in plan["preservation_evidences"]}:
            raise _blocked("original resources differ from the full preservation proofset")
        for proof, source_plan in zip(plan["preservation_evidences"], preserved, strict=True):
            row = preserved_rows[proof["source_relative"]]
            if row.get("classification") != "exact-preserved" or row.get("evidence") != proof["evidence"]:
                raise _blocked("original resource preservation binding changed")
            if proof["source_relative"] in done and not _source_absent(store, source_plan, target, transaction):
                raise _blocked("completed preserved source reappeared")
        if any(os.path.lexists(target / name) for name in done):
            raise _blocked("completed original resource reappeared; replay is forbidden")
        root = _common_root(store, preserved, target, transaction)
        if any(anchor["worktree"] != root for anchor in plan["hardlink_topology"]["anchors"].values()):
            raise _blocked("original graph is bound to another checkout root")
        return receipt, preserved, done, topology
    except ControllerError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, StopIteration) as exc:
        raise _blocked("original multi cleanup evidence is unavailable or malformed") from exc


def original(store: MetadataStore, recovery: dict[str, Any], *, target: Path | None = None,
             allocation_id: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    receipt, preserved, _, _ = details(store, recovery)
    _guard(store, recovery)
    if ((target is not None and receipt["plan"]["target_path"] != str(target))
            or (allocation_id is not None and receipt["plan"]["allocation_id"] != allocation_id)):
        raise _blocked("original cleanup belongs to another target or allocation")
    return receipt, preserved[0]


def _guard(store: MetadataStore, recovery: dict[str, Any], *, allow_successor: bool = True) -> None:
    from .transaction_scope import is_atomic_temp, terminal_state

    parent = store.cleanup_recoveries_dir / recovery["original_transaction_id"]
    if recovery.get("recovery_id"):
        directory = _directory(store, recovery["original_transaction_id"], recovery["recovery_id"])
        record, _ = load_record(store, directory / "record.json", recovery["recovery_record_sha256"])
        if record["plan"]["recovery"] != context(recovery["original_transaction_id"],
                recovery["original_transaction_sha256"], recovery["original_cleanup_receipt_sha256"]):
            raise _blocked("executing recovery does not bind its immutable baseline")
    elif list(parent.glob("*/started.json")):
        raise _blocked("started recovery cannot replay; exact terminal reconciliation is required")
    for path in store.transactions_dir.iterdir():
        if is_atomic_temp(path.name) or path.stem == recovery["original_transaction_id"]:
            continue
        if not path.is_file() or path.suffix != ".json":
            raise _blocked("unknown transaction entry blocks recovery")
        other, _ = e.read(path)
        if (allow_successor and recovery.get("recovery_id") and other.get("command") == "resource-clean"
                and other.get("cleanup_plan", {}).get("recovery") == recovery):
            continue
        if terminal_state(store, path.stem, verify_generations=True) is None:
            raise _blocked("another interrupted transaction requires its own recovery")


def _inputs(plan: dict[str, Any]) -> dict[str, Any]:
    if plan["contract"] == TYPED_CONTRACT:
        return {}
    proofs = plan["preservation_evidences"]
    return {
        "preservation_receipt": Path(proofs[0]["evidence"]["receipt_path"]),
        "preservation_receipt_sha256": proofs[0]["evidence"]["receipt_sha256"],
        "additional_preservation_receipts": tuple(Path(row["evidence"]["receipt_path"]) for row in proofs[1:]),
        "additional_preservation_receipt_sha256": tuple(row["evidence"]["receipt_sha256"] for row in proofs[1:]),
    }


def _plan_locked(repo: Path, store: MetadataStore, target: Path, owner: str, version: int,
                 recovery: dict[str, Any]) -> dict[str, Any]:
    from .lifecycle import _typed_cleanup_plan_locked
    from .semantics import managed_record as _managed_record

    old, _, done, topology = details(store, recovery)
    _guard(store, recovery)
    repository = inventory(repo)
    snapshot = store._load_unlocked()
    item, record = _managed_record(repository, snapshot, target)
    current = _typed_cleanup_plan_locked(repository, snapshot, store, item, record,
        owner=owner, expected_record_version=version, recovery=recovery, **_inputs(old["plan"]))
    expected = old["plan"]
    suffix = expected["cleanup_paths"][len(done):]
    old_rows = {row["path"]: row for row in expected["resources"]}
    if (current["blockers"] or _identity(current) != _identity(expected)
            or item.branch != store._read_json(store.transactions_dir / f"{recovery['original_transaction_id']}.json")["branch"]
            or current.get("contract") != expected["contract"] or current["cleanup_paths"] != suffix
            or current.get("preservation_evidences") != expected.get("preservation_evidences")
            or current["hardlink_topology"] != topology
            or any(row != old_rows.get(row["path"]) for row in current["resources"] if row["path"] in suffix)):
        raise _blocked("current resource, topology, identity or runtime guards differ from the proven suffix")
    return current


def plan(repo: Path, *, target: Path, owner: str, version: int,
         recovery: dict[str, Any]) -> dict[str, Any]:
    store = MetadataStore(inventory(repo).common_dir)
    with store.lock(exclusive=False):
        return _plan_locked(repo, store, target, owner, version, recovery)


def _directory(store: MetadataStore, transaction: str, recovery_id: str) -> Path:
    _uuid(transaction)
    _uuid(recovery_id)
    return store.cleanup_recoveries_dir / transaction / recovery_id


def prepare(repo: Path, *, target: Path, owner: str, version: int, recovery: dict[str, Any],
            dry_run: bool, expected_plan_id: str | None = None) -> dict[str, Any]:
    from .controller import status

    store = MetadataStore(inventory(repo).common_dir)
    with store.lock(exclusive=not dry_run):
        current = _plan_locked(repo, store, target, owner, version, recovery)
        decision = {"code": "MULTIPLE_CLEANUP_RECOVERABLE", "plan": current, "dry_run": dry_run}
        if not dry_run:
            if expected_plan_id != current["plan_id"]:
                raise _blocked("exact fresh recovery dry-run plan required")
            parent = store.cleanup_recoveries_dir / recovery["original_transaction_id"]
            if parent.exists() and list(parent.glob("*/record.json")):
                raise _blocked("an immutable recovery baseline already exists")
            recovery_id = str(uuid.uuid4())
            directory = _directory(store, recovery["original_transaction_id"], recovery_id)
            old, _, done, topology = details(store, recovery)
            allocation_record = store._load_unlocked().records[current["allocation_id"]]
            record = {
                "contract": CONTRACT, "result": "MULTIPLE_CLEANUP_RECOVERABLE",
                "recovery_id": recovery_id, "plan": current,
                "original_plan_id": old["plan"]["plan_id"],
                "original_preservation_evidences": old["plan"].get("preservation_evidences"),
                "completed_resources": old["plan"]["cleanup_paths"][:len(done)],
                "remaining_resources": current["cleanup_paths"],
                "prefix_topology_sha256": topology["sha256"],
                "allocation_record": record_to_dict(allocation_record),
                "generation_metadata": _generation_metadata(_identity(current)),
                "old_transaction_remains_immutable": True, "completed_action_will_replay": False,
                "timestamp": now_iso(),
            }
            decision.update(recovery_id=recovery_id, recovery_receipt=e.write(directory, "record.json", record))
    result = status(repo)
    result.update(command="resource-recover", decision=decision)
    return result


def load_record(store: MetadataStore, recovery_path: Path,
                recovery_sha: str) -> tuple[dict[str, Any], Path]:
    try:
        return _load_record(store, recovery_path, recovery_sha)
    except ControllerError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, StopIteration) as exc:
        raise _blocked("immutable multiple recovery record is unavailable or malformed") from exc


def _load_record(store: MetadataStore, recovery_path: Path,
                 recovery_sha: str) -> tuple[dict[str, Any], Path]:
    record, _ = e.read(recovery_path, recovery_sha)
    _frozen_authority(record)
    recovery = record["plan"]["recovery"]
    directory = _directory(store, recovery["original_transaction_id"], record["recovery_id"])
    old, _, done, topology = details(store, {
        **recovery, "recovery_id": record["recovery_id"], "recovery_record_sha256": recovery_sha,
    })
    baseline = record["plan"]
    suffix = old["plan"]["cleanup_paths"][len(done):]
    old_rows = {row["path"]: row for row in old["plan"]["resources"]}
    if (not e.matches_reference({"path": str(recovery_path), "sha256": recovery_sha}, directory / "record.json")
            or record.get("contract") != CONTRACT or record.get("result") != "MULTIPLE_CLEANUP_RECOVERABLE"
            or record.get("old_transaction_remains_immutable") is not True
            or record.get("completed_action_will_replay") is not False
            or record.get("original_plan_id") != old["plan"]["plan_id"]
            or record.get("original_preservation_evidences") != old["plan"].get("preservation_evidences")
            or record.get("completed_resources") != old["plan"]["cleanup_paths"][:len(done)]
            or record.get("remaining_resources") != old["plan"]["cleanup_paths"][len(done):]
            or record.get("prefix_topology_sha256") != topology["sha256"]
            or not _plan_hash(baseline) or baseline.get("contract") != old["plan"]["contract"]
            or _identity(baseline) != _identity(old["plan"])
            or baseline.get("cleanup_paths") != suffix or baseline.get("hardlink_topology") != topology
            or baseline.get("preservation_evidences") != old["plan"].get("preservation_evidences")
            or baseline.get("preservation_evidence") != old["plan"].get("preservation_evidence")
            or baseline.get("blockers") != []
            or any(row != old_rows.get(row["path"]) for row in baseline["resources"] if row["path"] in suffix)):
        raise _blocked("invalid immutable multiple recovery record")
    return record, directory


def recovered_receipt(store: MetadataStore, receipt: dict[str, Any],
                      bound: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Verify a successful suffix receipt against its original full proofset and prefix."""
    try:
        return _recovered_receipt(store, receipt, bound)
    except ControllerError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, StopIteration) as exc:
        raise _blocked("multiple recovery completion is unavailable or malformed") from exc


def _recovered_receipt(store: MetadataStore, receipt: dict[str, Any],
                       bound: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    successor = receipt["plan"]
    executing = successor["recovery"]
    record_sha = executing["recovery_record_sha256"]
    directory = _directory(store, executing["original_transaction_id"], executing["recovery_id"])
    record, _ = load_record(store, directory / "record.json", record_sha)
    baseline = record["plan"]
    expected_recovery = {**baseline["recovery"], "recovery_id": record["recovery_id"],
                         "recovery_record_sha256": record_sha}
    old, preserved, done, topology = details(store, expected_recovery)
    suffix = old["plan"]["cleanup_paths"][len(done):]
    post = receipt["post_scan"]
    if (receipt.get("contract") != old["plan"]["contract"] or receipt.get("result") != "CLEANED"
            or successor.get("contract") != old["plan"]["contract"] or executing != expected_recovery
            or _identity(successor) != _identity(old["plan"])
            or (bound is not None and _identity(successor) != bound)
            or successor.get("blockers") != [] or not _plan_hash(successor)
            or successor.get("preservation_evidences") != old["plan"].get("preservation_evidences")
            or successor.get("preservation_evidence") != old["plan"].get("preservation_evidence")
            or successor.get("hardlink_topology") != topology or successor.get("cleanup_paths") != suffix
            or receipt.get("actions") != [{"path": name, "status": "COMPLETED"} for name in suffix]
            or len(receipt.get("action_evidence", [])) != len(suffix)
            or any(successor.get(key) != value for key, value in baseline.items() if key not in {"plan_id", "recovery", "runtime_evidence", "warnings"})
            or post.get("contract") != old["plan"]["contract"] or post.get("recovery") != expected_recovery
            or _identity(post) != _identity(successor) or not _plan_hash(post)
            or post.get("preservation_evidences") != old["plan"].get("preservation_evidences")
            or post.get("preservation_evidence") != old["plan"].get("preservation_evidence")
            or post.get("build_preservation_evidences") != baseline.get("build_preservation_evidences")
            or post.get("blockers") != [] or post.get("cleanup_paths") != []):
        raise _blocked("successful suffix completion differs from its immutable recovery baseline")
    completed, final = completed_actions(store, receipt["receipt_id"], tuple(receipt["action_evidence"]),
        initial_topology=topology, cleanup_paths=suffix)
    _action_files(store, receipt["receipt_id"], len(suffix))
    if completed != set(suffix) or final != post.get("hardlink_topology") or final["resources"]:
        raise _blocked("suffix action chain is incomplete or its final scan changed")
    target = Path(successor["target_path"])
    if any(os.path.lexists(target / name) for name in old["plan"]["cleanup_paths"]):
        raise _blocked("a completed cleanup resource reappeared")
    for source_plan in preserved:
        if not _source_absent(store, source_plan, target, executing["original_transaction_id"]):
            raise _blocked("completed preservation source or ancestors changed")
    return preserved


def _append_terminal(store: MetadataStore, record: dict[str, Any], directory: Path,
                     record_sha: str, receipt: dict[str, Any], receipt_sha: str) -> dict[str, str]:
    recovered_receipt(store, receipt)
    executing = receipt["plan"]["recovery"]
    _guard(store, executing, allow_successor=False)
    started, started_sha = e.read(directory / "started.json")
    if (started.get("contract") != CONTRACT or started.get("phase") != "ATTEMPT_STARTED"
            or started.get("record_sha256") != record_sha or started.get("recovery_id") != record["recovery_id"]):
        raise _blocked("completion requires the exact immutable started recovery")
    value = {
        "contract": CONTRACT, "result": "CLEANED",
        "original_transaction_id": record["plan"]["recovery"]["original_transaction_id"],
        "record_sha256": record_sha,
        "started": {"path": str(directory / "started.json"), "sha256": started_sha},
        "completion_receipt_id": receipt["receipt_id"], "completion_receipt_sha256": receipt_sha,
        "completed_action_will_replay": False, "timestamp": now_iso(),
    }
    path = directory / "complete.json"
    if path.exists():
        existing, digest = e.read(path)
        if any(existing.get(key) != item for key, item in value.items() if key != "timestamp"):
            raise _blocked("existing terminal evidence differs")
        return {"path": str(path), "sha256": digest}
    return e.write(directory, "complete.json", value)


def resume(repo: Path, *, recovery_path: Path, recovery_sha: str, dry_run: bool) -> dict[str, Any]:
    from .controller import status
    from .lifecycle import cleanup_typed_disposable

    store = MetadataStore(inventory(repo).common_dir)
    with store.lock(exclusive=not dry_run):
        record, directory = load_record(store, recovery_path, recovery_sha)
        baseline = record["plan"]
        executing = {**baseline["recovery"], "recovery_id": record["recovery_id"],
                     "recovery_record_sha256": recovery_sha}
        if (directory / "started.json").exists():
            _guard(store, executing)
            successful = []
            for path in store.completion_receipts_dir.glob("*.json"):
                receipt, digest = e.read(path)
                if receipt.get("plan", {}).get("recovery") == executing and receipt.get("result") == "CLEANED":
                    recovered_receipt(store, receipt)
                    successful.append((receipt, digest))
            if len(successful) != 1:
                raise _blocked("started recovery cannot replay; partial or unknown effects require separate recovery")
            receipt, digest = successful[0]
            reference = {"path": str(directory / "complete.json")}
            if not dry_run:
                reference = _append_terminal(store, record, directory, recovery_sha, receipt, digest)
                verified_completion(store, executing["original_transaction_id"])
            else:
                _guard(store, executing, allow_successor=False)
                if (directory / "complete.json").exists():
                    verified_completion(store, executing["original_transaction_id"])
            decision = {"code": "MULTIPLE_RECOVERY_COMPLETION_READY" if dry_run and not (directory / "complete.json").exists()
                        else "MULTIPLE_RECOVERY_CLEANED", "dry_run": dry_run,
                        "receipt_id": receipt["receipt_id"], "recovery_completion": reference,
                        "completed_action_will_replay": False}
        else:
            current = _plan_locked(repo, store, Path(baseline["target_path"]), baseline["owner"],
                                   baseline["expected_record_version"], baseline["recovery"])
            allocation = store._load_unlocked().records[baseline["allocation_id"]]
            if record_to_dict(allocation) != record["allocation_record"]:
                raise _blocked("live allocation record differs from the frozen recovery authority")
            if p._sha(current) != p._sha(baseline):
                raise _blocked("current state differs from the immutable recovery plan")
            if dry_run:
                decision = {"code": "MULTIPLE_CLEANUP_RECOVERABLE", "plan": current, "dry_run": True}
            else:
                e.write(directory, "started.json", {"contract": CONTRACT, "record_sha256": recovery_sha,
                    "recovery_id": record["recovery_id"], "phase": "ATTEMPT_STARTED", "timestamp": now_iso()})
                decision = None
    if decision is None:
        options = dict(target=Path(baseline["target_path"]), owner=baseline["owner"],
            expected_record_version=baseline["expected_record_version"], recovery=executing, **_inputs(baseline))
        planned = cleanup_typed_disposable(repo, **options, dry_run=True)
        if planned["decision"]["plan"]["blockers"]:
            raise _blocked("recovery execution preflight is blocked; started evidence retained")
        cleaned = cleanup_typed_disposable(repo, **options, dry_run=False,
            expected_plan_id=planned["decision"]["plan"]["plan_id"])
        receipt_id = cleaned["decision"]["receipt_id"]
        if not receipt_id:
            raise _blocked("recovery has no exact successful successor receipt")
        with store.lock(exclusive=True):
            receipt, digest = e.read(store.completion_receipts_dir / f"{receipt_id}.json")
            reference = _append_terminal(store, record, directory, recovery_sha, receipt, digest)
            verified_completion(store, executing["original_transaction_id"])
        decision = {"code": "MULTIPLE_RECOVERY_CLEANED", "receipt_id": receipt_id,
                    "recovery_id": record["recovery_id"], "recovery_completion": reference,
                    "completed_action_will_replay": False}
    result = status(repo)
    result.update(command="resource-recover", decision=decision)
    return result


def verified_completion(store: MetadataStore, transaction: str,
                        bound: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    try:
        return _verified_completion(store, transaction, bound)
    except ControllerError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, StopIteration) as exc:
        raise _blocked("multiple recovery terminal is unavailable or malformed") from exc


def _verified_completion(store: MetadataStore, transaction: str,
                         bound: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    parent = store.cleanup_recoveries_dir / transaction
    terminals = list(parent.glob("*/complete.json"))
    if len(terminals) != 1:
        raise _blocked("a unique immutable multiple recovery completion is required")
    terminal, _ = e.read(terminals[0])
    record, directory = load_record(store, terminals[0].parent / "record.json", terminal["record_sha256"])
    recovery = record["plan"]["recovery"]
    if (terminal.get("contract") != CONTRACT or terminal.get("result") != "CLEANED"
            or terminal.get("original_transaction_id") != transaction
            or recovery["original_transaction_id"] != transaction
            or terminal.get("completed_action_will_replay") is not False
            or not e.matches_reference(terminal["started"], directory / "started.json")):
        raise _blocked("invalid multiple recovery terminal binding")
    started, _ = e.read(directory / "started.json", terminal["started"]["sha256"])
    if (started.get("contract") != CONTRACT or started.get("phase") != "ATTEMPT_STARTED"
            or started.get("record_sha256") != terminal["record_sha256"]
            or started.get("recovery_id") != record["recovery_id"]):
        raise _blocked("terminal does not bind the exact started recovery")
    _uuid(terminal["completion_receipt_id"])
    receipt, _ = e.read(store.completion_receipts_dir / f"{terminal['completion_receipt_id']}.json",
                        terminal["completion_receipt_sha256"])
    if receipt.get("receipt_id") != terminal["completion_receipt_id"]:
        raise _blocked("terminal successor receipt identity differs")
    return recovered_receipt(store, receipt, bound)


def completed(store: MetadataStore, transaction: str, *, verify_generations: bool = False) -> bool:
    # Full generation verification remains mandatory for both classification modes.
    parent = store.cleanup_recoveries_dir / transaction
    terminals = list(parent.glob("*/complete.json"))
    if not terminals:
        return False
    if len(terminals) != 1:
        raise _blocked("ambiguous recovery completion")
    terminal, _ = e.read(terminals[0])
    if terminal.get("contract") != CONTRACT:
        return False
    verified_completion(store, transaction)
    return True
