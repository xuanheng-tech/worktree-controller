"""Restore only exact-receipt-covered tracked files to their bound HEAD bytes.

No index write, Git reset/stash, lifecycle transition or allocation release. An
interruption keeps the normal Controller transaction gate closed; no blind replay.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import uuid
from pathlib import Path

from . import cleanup_evidence as evidence
from . import preservation as p
from . import source_preservation as s
from .git_backend import _git
from .models import ControllerError
from .store import MetadataStore, now_iso

CONTRACT = "tracked-source-cleanup/v1"


def _store(plan: dict) -> MetadataStore:
    from .lifecycle import inventory

    return MetadataStore(inventory(Path(plan["repository"])).common_dir)


def _base(plan: dict) -> tuple[list[dict], dict[str, bytes]]:
    target = Path(plan["authority"]["identity"]["target_path"])
    rows, payloads = [], {}
    for source in plan["source_tree"]["files"]:
        path = source["path"]
        if plan["source_tree"]["source_identities"][path][8] != 1:
            raise s._fail("SOURCE_CLEANUP_HARDLINK_BLOCKED")
        mode, blob = source["base_entry"]
        size = int(_git(target, "cat-file", "-s", blob).stdout)
        if size > s.MAX_FILE_BYTES:
            raise s._fail("SOURCE_SIZE_LIMIT")
        data = _git(target, "cat-file", "blob", blob).stdout
        if len(data) != size or s._blob(data) != blob:
            raise s._fail("SOURCE_BASE_BLOB_MISMATCH")
        if s._SECRET_BYTES.search(data):
            raise s._fail("SOURCE_SECRET_MATERIAL")
        rows.append(
            {
                "path": path,
                "size": size,
                "sha256": hashlib.sha256(data).hexdigest(),
                "mode": 0o755 if mode == "100755" else 0o644,
                "git_blob": blob,
            }
        )
        payloads[path] = data
    if sum(row["size"] for row in rows) > s.MAX_TOTAL_BYTES:
        raise s._fail("SOURCE_SIZE_LIMIT")
    return rows, payloads


def plan_cleanup(plan: dict, receipt_sha256: str, *, state_root: Path | None = None) -> dict:
    from .source_recovery import authorization

    verified = s.verify_preservation(plan, receipt_sha256, state_root=state_root)
    rows, _ = _base(plan)
    value = {
        "contract": CONTRACT,
        "preservation_plan": plan,
        "preservation_receipt_sha256": receipt_sha256,
        "verification": verified,
        "restore": rows,
        "index_action": "none",
        "lifecycle_action": "none",
        "partial_failure": "retain-transaction-no-replay",
    }
    reconciliation = authorization(_store(plan), plan, receipt_sha256)
    if reconciliation is not None:
        value["reconciliation"] = reconciliation
    return {**value, "plan_id": p._sha(value), "result": "SOURCE_CLEANUP_READY"}


def _state(
    cleanup: dict,
    completed: dict,
    *,
    journal: dict | None = None,
    journal_ref: dict | None = None,
    state_root: Path | None = None,
    _migration_store: MetadataStore | None = None,
) -> dict:
    plan = cleanup["preservation_plan"]
    identity, tree = plan["authority"]["identity"], plan["source_tree"]
    if cleanup.get("reconciliation"):
        ref = cleanup["reconciliation"]
        e_value, _ = evidence.read(Path(ref["path"]), ref["sha256"])
        if e_value["new_attempt_id"] != ref["new_attempt_id"]:
            raise s._fail("SOURCE_RECOVERY_BLOCKED")
    if journal_ref is not None:
        actual, _ = evidence.read(Path(journal_ref["path"]), journal_ref["sha256"])
        if actual != journal:
            raise s._fail("SOURCE_TRANSACTION_CHANGED")
    authority = s._authority(
        Path(plan["repository"]),
        Path(identity["target_path"]),
        identity["allocation_id"],
        identity["owner"],
        identity["expected_record_version"],
        pending_transaction=journal,
        _migration_store=_migration_store,
    )
    if authority["identity"] != identity:
        raise s._fail("SOURCE_AUTHORITY_CHANGED")
    generation = s._verify_generation(plan, cleanup["preservation_receipt_sha256"], state_root)
    target = Path(identity["target_path"])
    git_state = s._git_state(
        target, Path(identity["git_dir"]), identity["expected_head"], allow_clean=True
    )
    for key in ("base_head", "base_tree", "index_identity", "index_sha256", "autocrlf"):
        if git_state[key] != tree[key]:
            raise s._fail("SOURCE_GIT_CHANGED")
    remaining = sorted(set(tree["dirty_paths"]) - set(completed))
    if git_state["dirty_paths"] != remaining:
        raise s._fail("SOURCE_DIRTY_SET_CHANGED")
    observed = {}
    with p._absolute_directory(target) as chain:
        if chain[-1].identity != tree["root_identity"]:
            raise s._fail("SOURCE_CHANGED")
        for source in tree["files"]:
            path = source["path"]
            data, metadata, parents = s._read(chain, path)
            expected = completed.get(path)
            row = next(r for r in cleanup["restore"] if r["path"] == path) if expected else source
            if (
                metadata != (expected or tree["source_identities"][path])
                or parents != tree["parent_identities"][path]
                or len(data) != row["size"]
                or hashlib.sha256(data).hexdigest() != row["sha256"]
                or stat.S_IMODE(metadata[2]) != row["mode"]
            ):
                raise s._fail("SOURCE_CHANGED")
            observed[path] = {"identity": metadata, "sha256": row["sha256"], "mode": row["mode"]}
    return {
        "head": git_state["base_head"],
        "dirty_paths": remaining,
        "dirty_digest": p._sha({path: observed[path] for path in remaining}),
        "observed": observed,
        "index_sha256": git_state["index_sha256"],
        "preservation": generation,
        "runtime_evidence": authority["runtime_evidence"],
    }


def _restore(plan: dict, row: dict, data: bytes) -> list[int]:
    """All writes go to one verified single-link regular FD, never a path reopen.

    In-place writes avoid replacing an unverified racing directory entry. A short
    write/interruption may leave this covered file partial: its exact preservation
    and the outstanding transaction remain. There is deliberately no implicit undo.
    """
    path, tree = row["path"], plan["source_tree"]
    expected = tree["source_identities"][path]
    target = Path(plan["authority"]["identity"]["target_path"])
    with p._absolute_directory(target) as root, s._parent(root, path) as (chain, name):
        if {str(i): d.identity for i, d in enumerate(chain)} != tree["parent_identities"][path]:
            raise s._fail("SOURCE_CHANGED")
        pin = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=chain[-1].fd)
        try:
            metadata = os.fstat(pin)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or p._identity(metadata) != expected
                or metadata.st_nlink != 1
            ):
                raise s._fail("SOURCE_CHANGED")
            fd = os.open(f"/proc/self/fd/{pin}", os.O_RDWR | os.O_NOATIME | os.O_CLOEXEC)
            try:
                source = next(r for r in tree["files"] if r["path"] == path)
                if p._identity(os.fstat(fd)) != expected or p._transfer(fd) != (
                    source["size"],
                    source["sha256"],
                ):
                    raise s._fail("SOURCE_CHANGED")
                p._check_chain(chain)
                p._check_entry(chain[-1].fd, name, expected)
                if p._identity(os.fstat(fd)) != expected:
                    raise s._fail("SOURCE_CHANGED")
                os.lseek(fd, 0, os.SEEK_SET)
                view = memoryview(data)
                while view:
                    count = os.write(fd, view)
                    if not count:
                        raise s._fail("SOURCE_WRITE_FAILED")
                    view = view[count:]
                os.ftruncate(fd, len(data))
                os.fchmod(fd, row["mode"])
                os.fsync(fd)
                actual = p._identity(os.fstat(fd))
                if (
                    actual[:2] != expected[:2]
                    or actual[6:] != expected[6:]
                    or stat.S_IMODE(actual[2]) != row["mode"]
                ):
                    raise s._fail("SOURCE_CHANGED")
                os.lseek(fd, 0, os.SEEK_SET)
                if p._transfer(fd) != (row["size"], row["sha256"]):
                    raise s._fail("SOURCE_WRITE_FAILED")
                p._check_chain(chain)
                p._check_entry(chain[-1].fd, name, actual)
                return actual
            finally:
                os.close(fd)
        finally:
            os.close(pin)


def cleanup_source(cleanup: dict, *, state_root: Path | None = None) -> dict:
    plan, receipt_sha = cleanup["preservation_plan"], cleanup["preservation_receipt_sha256"]
    if plan_cleanup(plan, receipt_sha, state_root=state_root) != cleanup:
        raise s._fail("SOURCE_CLEANUP_PLAN_MISMATCH")
    rows, payloads = _base(plan)
    identity = plan["authority"]["identity"]
    store = _store(plan)
    reconciliation = cleanup.get("reconciliation")
    transaction_id = reconciliation["new_attempt_id"] if reconciliation else str(uuid.uuid4())
    directory = store.root / "source-cleanup" / transaction_id
    from .lifecycle import _release_transaction, _transaction, inventory
    from .semantics import assert_record_version as _cas
    from .semantics import managed_record as _managed_record

    with store.lock(exclusive=True):
        snapshot = store._load_unlocked()
        item, record = _managed_record(
            inventory(Path(plan["repository"])), snapshot, Path(identity["target_path"])
        )
        _cas(record, identity["expected_record_version"])
        if _release_transaction(
            store, target_path=str(item.path), allocation_id=record.allocation_id
        ):
            raise s._fail("INTERRUPTED_TRANSACTION")
        journal = _transaction(
            command="resource-clean",
            transaction_id=transaction_id,
            repository_id=identity["repository_id"],
            target_path=item.path,
            allocation_id=record.allocation_id,
            branch=item.branch,
            expected_head=item.head,
            expected_record_version=record.record_version,
            phase="PREPARED",
            created_at=now_iso(),
        )
        journal["cleanup_plan"] = {
            "contract": CONTRACT,
            **{
                key: journal[key]
                for key in (
                    "repository_id",
                    "allocation_id",
                    "target_path",
                    "expected_head",
                    "expected_record_version",
                )
            },
            "source_plan": evidence.write(directory, "plan.json", cleanup),
        }
        journal_ref = evidence.write(store.transactions_dir, f"{transaction_id}.json", journal)
    actions, completed, checks = [], {}, []
    try:
        for step, row in enumerate(rows):
            before = _state(
                cleanup, completed, journal=journal, journal_ref=journal_ref, state_root=state_root
            )
            checks.append(before)
            action = {"path": row["path"], "status": "ACTION_STARTED"}
            actions.append(action)
            action["before"] = evidence.write(
                directory,
                f"{step:04d}-before.json",
                {
                    "contract": CONTRACT,
                    "transaction_id": transaction_id,
                    "path": row["path"],
                    "state": before,
                    "restore": row,
                    "execution_state": "ACTION_STARTED",
                    "timestamp": now_iso(),
                },
            )
            completed[row["path"]] = _restore(plan, row, payloads[row["path"]])
            after = _state(
                cleanup, completed, journal=journal, journal_ref=journal_ref, state_root=state_root
            )
            action["after"] = evidence.write(
                directory,
                f"{step:04d}-after.json",
                {
                    "contract": CONTRACT,
                    "transaction_id": transaction_id,
                    "path": row["path"],
                    "before": action["before"],
                    "state": after,
                    "execution_state": "ACTION_COMPLETED",
                    "timestamp": now_iso(),
                },
            )
            action["status"] = "ACTION_COMPLETED"
            checks.append(after)
        post = _state(
            cleanup, completed, journal=journal, journal_ref=journal_ref, state_root=state_root
        )
        if post["dirty_paths"]:
            raise s._fail("SOURCE_CLEANUP_INCOMPLETE")
    except (ControllerError, OSError, ValueError, TypeError, KeyError) as exc:
        from .cleanup_attempts import underlying

        refusal = underlying(exception=exc)
        failed = None
        evidence_error = None
        try:
            failed = evidence.write(
                store.completion_receipts_dir,
                f"{transaction_id}.json",
                {
                    "contract": CONTRACT,
                    "receipt_id": transaction_id,
                    "kind": "tracked_source_cleanup",
                    "result": "FAILED_OR_PARTIAL",
                    "error_code": getattr(exc, "code", "SOURCE_CLEANUP_IO_ERROR"),
                    **(
                        {"runtime_evidence": exc.runtime_evidence}
                        if isinstance(exc, s.AuthorityBlocked)
                        else {}
                    ),
                    "journal": journal_ref,
                    "actions": actions,
                    "checks": checks,
                    "underlying": refusal,
                    "timestamp": now_iso(),
                },
            )
        except (ControllerError, OSError, ValueError, TypeError, KeyError) as write_error:
            evidence_error = underlying(exception=write_error)
        return {
            "result": "SOURCE_CLEANUP_BLOCKED",
            "transaction_id": transaction_id,
            "receipt": failed,
            "code": getattr(exc, "code", "SOURCE_CLEANUP_IO_ERROR"),
            "underlying": refusal,
            "evidence_write_error": evidence_error,
            "release_authorized": False,
        }
    receipt = evidence.write(
        store.completion_receipts_dir,
        f"{transaction_id}.json",
        {
            "contract": CONTRACT,
            "receipt_id": transaction_id,
            "kind": "tracked_source_cleanup",
            "result": "SOURCE_CLEANUP_COMPLETE",
            "allocation_source_identity": identity,
            "journal": journal_ref,
            "plan": journal["cleanup_plan"]["source_plan"],
            "actions": actions,
            "checks": checks,
            "post_scan": post,
            "index_unchanged": True,
            "release_authorized": False,
            "timestamp": now_iso(),
        },
    )
    with store.lock(exclusive=True):
        actual, _ = evidence.read(Path(journal_ref["path"]), journal_ref["sha256"])
        if actual != journal:
            raise s._fail("SOURCE_TRANSACTION_CHANGED")
        # Keep the exact journal as immutable evidence before the formal removal.
        evidence.write(directory, "journal.json", journal)
        evidence.write(directory, "completed.json", {"contract": CONTRACT, "receipt": receipt})
        store.remove_transaction(transaction_id)
    return {
        "result": "SOURCE_CLEANUP_COMPLETE",
        "receipt": receipt,
        "post_scan": post,
        "transaction_id": transaction_id,
        "release_authorized": False,
    }


def verify_completed(store: MetadataStore, item, record) -> list[dict]:
    """Reverify archived bytes and restored source at completion/release boundaries.

    This is not a handoff or publication assertion. It binds the current no-commit
    closeout to independently recorded archival and restoration evidence.
    """
    verified = []
    for directory in sorted((store.root / "source-cleanup").glob("*")):
        cleanup, plan_digest = evidence.read(directory / "plan.json")
        plan = cleanup["preservation_plan"]
        identity = plan["authority"]["identity"]
        if identity["allocation_id"] != record.allocation_id:
            continue
        from .source_recovery import terminal

        if terminal(store, directory.name, verify_generation=True):
            continue  # Terminal historical attempts grant no cleanup completion or release.
        try:
            marker, _ = evidence.read(directory / "completed.json")
            ref = marker["receipt"]
            if (
                marker["contract"] != CONTRACT
                or not evidence.matches_reference(
                    ref, store.completion_receipts_dir / f"{directory.name}.json"
                )
                or identity["target_path"] != str(item.path)
                or identity["expected_head"] != item.head
                or identity["owner"] != record.owner
                or identity["authority_key"] != record.authority_key
                or identity["expected_record_version"] > record.record_version
            ):
                raise s._fail("SOURCE_CLEANUP_EVIDENCE_INVALID")
            receipt, _ = evidence.read(Path(ref["path"]), ref["sha256"])
            if cleanup.get("reconciliation"):
                predecessor = cleanup["reconciliation"]
                evidence.read(Path(predecessor["path"]), predecessor["sha256"])
            if (
                receipt["contract"] != CONTRACT
                or receipt["result"] != "SOURCE_CLEANUP_COMPLETE"
                or receipt["allocation_source_identity"] != identity
                or receipt["plan"]["sha256"] != plan_digest
                or not evidence.matches_reference(receipt["plan"], directory / "plan.json")
                or receipt["post_scan"]["dirty_paths"]
                or [a["path"] for a in receipt["actions"]] != plan["source_tree"]["dirty_paths"]
                or any(a["status"] != "ACTION_COMPLETED" for a in receipt["actions"])
            ):
                raise s._fail("SOURCE_CLEANUP_EVIDENCE_INVALID")
            for action in receipt["actions"]:
                for key in ("before", "after"):
                    evidence.read(Path(action[key]["path"]), action[key]["sha256"])
            s._verify_generation(plan, cleanup["preservation_receipt_sha256"])
            current = s._git_state(
                item.path, Path(identity["git_dir"]), item.head, allow_clean=True
            )
            if current["dirty_paths"] or any(
                current[key] != plan["source_tree"][key]
                for key in ("base_head", "base_tree", "autocrlf")
            ):
                raise s._fail("SOURCE_GIT_CHANGED")
            # The immutable action evidence binds the unchanged index during
            # restoration. After completion Git may rewrite its stat cache.
            # _git_state still verifies every stage/mode/blob and index flag
            # against HEAD; only this observation must remain physically stable.
            with p._absolute_directory(item.path) as root:
                for row in cleanup["restore"]:
                    data, metadata, parents = s._read(root, row["path"])
                    expected = receipt["post_scan"]["observed"][row["path"]]
                    if (
                        metadata != expected["identity"]
                        or parents != plan["source_tree"]["parent_identities"][row["path"]]
                        or hashlib.sha256(data).hexdigest() != row["sha256"]
                        or len(data) != row["size"]
                        or stat.S_IMODE(metadata[2]) != row["mode"]
                    ):
                        raise s._fail("SOURCE_CHANGED")
            if s._git_state(
                item.path, Path(identity["git_dir"]), item.head, allow_clean=True
            ) != current:
                raise s._fail("SOURCE_GIT_CHANGED")
            verified.append(ref)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise s._fail("SOURCE_CLEANUP_EVIDENCE_INVALID") from exc
    return verified


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "execute"))
    parser.add_argument("--plan-file", required=True, type=Path, help="pinned preservation plan")
    parser.add_argument("--receipt-sha256", required=True)
    parser.add_argument("--expected-plan-id")
    args = parser.parse_args(argv)
    try:
        plan, _ = evidence.read(args.plan_file)
        cleanup = plan_cleanup(plan, args.receipt_sha256)
        if args.command == "execute":
            if args.expected_plan_id != cleanup["plan_id"]:
                raise s._fail("SOURCE_CLEANUP_PLAN_MISMATCH")
            result = cleanup_source(cleanup)
        else:
            result = cleanup
        print(json.dumps(result, sort_keys=True))
        return 0 if result["result"] in {"SOURCE_CLEANUP_READY", "SOURCE_CLEANUP_COMPLETE"} else 1
    except (ControllerError, OSError, ValueError, TypeError, KeyError) as exc:
        from .cleanup_attempts import underlying

        print(
            json.dumps(
                {
                    "result": "SOURCE_CLEANUP_BLOCKED",
                    "code": getattr(exc, "code", "SOURCE_CLEANUP_IO_ERROR"),
                    "underlying": underlying(exception=exc),
                    **(
                        {"runtime_evidence": exc.runtime_evidence}
                        if isinstance(exc, s.AuthorityBlocked)
                        else {}
                    ),
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
