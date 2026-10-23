"""Exact immutable preservation receipt verification and FD-bound legacy deletion."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any

from . import preservation as p
from .disposable import _delete_fd_tree, _fd_inventory, _unlink_leaf
from .git_backend import head_paths, path_is_ignored, tracked_paths
from .models import ControllerError, MetadataRecord

CONTRACT = "legacy-preserved-state-cleanup/v1"
MAX_EVIDENCE_BYTES = 64 * 1024 * 1024


def _blocked(message: str) -> ControllerError:
    return ControllerError("PRESERVATION_COVERAGE_INVALID", message)


def _document(directory: p._Directory, name: str) -> tuple[dict[str, Any], str]:
    metadata = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
    if metadata.st_size > MAX_EVIDENCE_BYTES:
        raise _blocked("preservation evidence exceeds its bound")
    with (
        p._regular_fd(directory.fd, name, p._identity(metadata)) as fd,
        os.fdopen(os.dup(fd), "rb") as stream,
    ):
        raw = stream.read(MAX_EVIDENCE_BYTES + 1)
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise _blocked("preservation evidence exceeds its bound")
    directory.check()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise _blocked("invalid preservation document")
    return value, hashlib.sha256(raw).hexdigest()


def coverage(
    receipt_path: Path,
    receipt_sha256: str,
    *,
    identity: dict[str, Any] | None = None,
    verify_source: bool = True,
    source_topology: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify raw evidence hashes and both trees; never changes Controller metadata."""
    try:
        return _coverage(
            receipt_path,
            receipt_sha256,
            identity=identity,
            verify_source=verify_source,
            source_topology=source_topology,
        )
    except ControllerError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise _blocked("preservation evidence is unavailable, malformed or changed") from exc


def _coverage(
    receipt_path: Path,
    receipt_sha256: str,
    *,
    identity: dict[str, Any] | None,
    verify_source: bool,
    source_topology: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not re.fullmatch(r"[0-9a-f]{64}", receipt_sha256 or ""):
        raise _blocked("an explicit trusted preservation receipt SHA-256 is required")
    if not receipt_path.is_absolute() or receipt_path.name != "receipt.json":
        raise _blocked("canonical preservation receipt path required")
    generation = receipt_path.parent
    with p._absolute_directory(generation) as chain:
        directory = chain[-1]
        receipt, actual_receipt_sha = _document(directory, "receipt.json")
        if actual_receipt_sha != receipt_sha256:
            raise _blocked("preservation receipt bytes changed")
        plan, _ = _document(directory, "plan.json")
        manifest, manifest_sha = _document(directory, "manifest.json")
        bound = plan["authority"]["identity"]
        if identity is not None and bound != identity:
            raise _blocked("preservation belongs to another allocation/worktree identity")
        for value in (bound["repository_id"], bound["allocation_id"], plan["generation"]):
            if str(uuid.UUID(value)) != value:
                raise _blocked("noncanonical preservation identity")
        expected = (
            p.evidence_root() / bound["repository_id"] / bound["allocation_id"] / plan["generation"]
        )
        source_rel = plan.get("source_relative") or (
            str(Path(plan["source"]).relative_to(Path(bound["target_path"])))
            if "source" in plan
            else p.SOURCE
        )
        source_rel = p.source_relative_path(source_rel)
        source = Path(bound["target_path"]) / source_rel
        if (
            generation != expected
            or generation.is_relative_to(Path(bound["target_path"]))
            or plan["contract"] != p.CONTRACT
            or plan["ready"] is not True
            or plan["state_root"] != str(p.evidence_root())
            or plan["source"] != str(source)
            or plan["destination"] != str(generation)
            or p._sha({k: v for k, v in plan.items() if k != "plan_sha256"}) != plan["plan_sha256"]
            or receipt.get("contract") != p.CONTRACT
            or receipt.get("verification") != "PASS"
            or receipt.get("result") != "LEGACY_PRESERVATION_COMPLETE"
            or receipt.get("receipt_id") != plan["generation"]
            or receipt.get("allocation_source_identity") != bound
            or receipt.get("source") != str(source)
            or receipt.get("source_anchor") != plan["source_anchor"]
            or receipt.get("generation") != str(generation)
            or receipt.get("generation_identity") != directory.identity
            or receipt.get("destination") != str(generation / "tree")
            or receipt.get("manifest_sha256") != manifest_sha
            or receipt.get("plan_sha256") != plan["plan_sha256"]
            or manifest != plan["source_tree"]
            or manifest.get("errors")
            or receipt.get("inventory") != manifest["inventory"]
            or receipt.get("source_tree_sha256") != manifest["tree_sha256"]
            or receipt.get("preserved_tree_sha256") != manifest["tree_sha256"]
            or receipt.get("source_identity_sha256") != p._sha(manifest["source_identities"])
        ):
            raise _blocked("receipt, manifest, generation or source binding differs")
        p._destination_anchor(chain, plan["destination_anchor"])
        if "cleanup_topology" in plan:
            from .hardlinks import validate

            validate(plan["cleanup_topology"])
            if receipt.get("cleanup_topology_sha256") != plan["cleanup_topology"]["sha256"]:
                raise _blocked("preservation topology evidence changed")
        with p._open_node(directory.fd, "tree") as tree:
            preserved = p.scan_tree(tree, (*chain, tree))
            p._same_tree(preserved, manifest)
            if p._sha(preserved.identities) != receipt["destination_identity_sha256"]:
                raise _blocked("preserved node identity changed")
        if verify_source:
            worktree_root = Path(bound["target_path"])
            if tracked_paths(worktree_root, source_rel) or head_paths(worktree_root, source_rel):
                raise _blocked("preserved source contains tracked/staged files")
            if not path_is_ignored(worktree_root, source_rel):
                raise _blocked("preserved source is not ignored legacy state")
            with p._absolute_source(source) as source_chain:
                p._source_anchor(source_chain, plan)
                expected = manifest
                if source_topology is not None:
                    from .hardlinks import source_baseline

                    expected = source_baseline(manifest, source_topology, resource=source_rel)
                p._same_source(p.scan_tree(source_chain[-1], source_chain), expected)
        evidence = {
            "contract": CONTRACT,
            "receipt_path": str(receipt_path),
            "receipt_sha256": receipt_sha256,
            "manifest_sha256": manifest_sha,
            "preservation_plan_sha256": plan["plan_sha256"],
            "generation": str(generation),
            "generation_identity": directory.identity,
            "tree_sha256": manifest["tree_sha256"],
            "source_anchor": plan["source_anchor"],
            "source_identity_sha256": receipt["source_identity_sha256"],
            "destination_identity_sha256": receipt["destination_identity_sha256"],
            "inventory": manifest["inventory"],
            "allocation_source_identity": bound,
        }
    return evidence, plan


def completed_actions(
    store, transaction: str, proofs: tuple[dict[str, Any], ...],
    *, initial_topology: dict[str, Any], cleanup_paths: list[str],
) -> tuple[set[str], dict[str, Any]]:
    from . import cleanup_evidence as e
    from . import hardlinks

    if str(uuid.UUID(transaction)) != transaction:
        raise _blocked("canonical cleanup transaction identity required")
    if [proof["resource"] for proof in proofs] != cleanup_paths[:len(proofs)]:
        raise _blocked("cleanup action proofs differ from the exact ordered plan")
    hardlinks.validate(initial_topology)
    if initial_topology["resources"] != cleanup_paths:
        raise _blocked("cleanup topology does not cover every planned resource")
    completed: set[str] = set()
    preceding = initial_topology
    for step, proof in enumerate(proofs):
        resource = p.source_relative_path(proof["resource"])
        if resource in completed:
            raise _blocked("duplicate cleanup action proof")
        directory = store.cleanup_actions_dir / transaction
        if not e.matches_reference(proof["before"], directory / f"{step:04d}-before.json") or not e.matches_reference(
            proof["after"], directory / f"{step:04d}-after.json"
        ):
            raise _blocked("cleanup proof path differs from its exact transaction step")
        before, _ = e.read(Path(proof["before"]["path"]), proof["before"]["sha256"])
        after, _ = e.read(Path(proof["after"]["path"]), proof["after"]["sha256"])
        if (
            before.get("phase") != "BEFORE" or before.get("execution_state") != "ACTION_STARTED"
            or after.get("phase") != "COMPLETED" or after.get("execution_state") != "ACTION_COMPLETED"
            or before.get("resource") != resource or after.get("resource") != resource
            or after.get("before") != proof["before"] or before["topology"] != preceding
        ):
            raise _blocked("cleanup action evidence does not prove exact completion")
        mutation = hardlinks.transition(
            before["topology"], after["topology"], resource,
            after["mutation"].get("external_survivors"),
        )
        if after["mutation"] != mutation or before.get("contract") != mutation["contract"] or after.get("contract") != mutation["contract"]:
            raise _blocked("cleanup transition evidence changed")
        preceding = after["topology"]
        completed.add(resource)
    return completed, preceding


def common_source_root(plans: list[dict[str, Any]], target: Path) -> dict[str, int]:
    """Bind cleanup to an original root pin, including compatible old nested proofs."""
    pins = []
    for plan in plans:
        relative = p.source_relative_path(Path(plan["source"]).relative_to(target).as_posix())
        ancestors = plan.get("source_ancestor_identities")
        if ancestors is not None:
            pin = ancestors.get(str(target))
            if pin is None:
                raise _blocked("nested preservation has no original checkout-root pin")
        elif len(Path(relative).parts) == 1:
            pin = plan["source_anchor"]["worktree"]
        else:
            continue
        if set(pin) != {"device", "inode", "file_type"} or pin["file_type"] != stat.S_IFDIR:
            raise _blocked("invalid original checkout-root identity")
        pins.append(pin)
    if not pins or any(pin != pins[0] for pin in pins):
        raise _blocked("all preservation generations require one original checkout-root identity")
    with p._absolute_directory(target) as chain:
        if chain[-1].identity != pins[0]:
            raise _blocked("checkout root differs from the preserved original identity")
    return pins[0]


def source_absent(plan: dict[str, Any], target: Path) -> bool:
    relative = p.source_relative_path(Path(plan["source"]).relative_to(target).as_posix())
    source = target / relative
    with p._absolute_directory(source.parent) as chain:
        if chain[-1].identity != plan["source_anchor"]["worktree"]:
            raise _blocked("original source parent identity changed")
        if "source_ancestor_identities" in plan and p._chain_identities(chain) != plan["source_ancestor_identities"]:
            raise _blocked("original source ancestor chain changed")
        try:
            os.stat(source.name, dir_fd=chain[-1].fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        return False


def _source_key(plan: dict[str, Any]) -> str:
    return p._sha({
        "identity": plan["authority"]["identity"],
        **{key: plan[key] for key in ("source", "source_anchor", "source_tree")},
    })


def _reopened_source_identity(
    previous: dict[str, Any], current: dict[str, Any], target: Path,
    record: MetadataRecord | None,
) -> bool:
    from .git_backend import inventory, is_ancestor, resolve_commit

    stable = {"repository_id", "allocation_id", "target_path", "owner", "authority_key"}
    if (
        record is None or set(previous) != set(current)
        or set(current) != stable | {"expected_head", "expected_record_version"}
        or any(previous[key] != current[key] for key in stable)
        or current["target_path"] != str(target)
        or current["allocation_id"] != record.allocation_id
        or current["owner"] != record.owner or current["authority_key"] != record.authority_key
        or current["expected_record_version"] != record.record_version
        or record.record_version != len(record.history)
        or record.role not in {"feature", "audit", "hotfix"}
        or not isinstance(previous["expected_record_version"], int)
        or isinstance(previous["expected_record_version"], bool)
        or not 1 <= previous["expected_record_version"] < record.record_version
        or record.lifecycle not in {"HANDOFF_READY", "PRESERVED"}
        or not record.finalizer_record_id
        or any(oid != current["expected_head"] for oid in (
            record.handoff_commit, record.task_commit, record.finalizer_commit,
        ))
    ):
        return False
    # Supported records start with v1/history[0]; every official update appends
    # exactly one event and advances CAS by one. Unknown/shortened history cannot
    # establish which frozen allocation version a historical copy belonged to.
    state = None
    for event in record.history:
        if event.get("from") != state or not event.get("to"):
            return False
        state = event["to"]
    if state != record.lifecycle:
        return False
    old_version = previous["expected_record_version"]
    state = record.history[old_version - 1]["to"]
    if state not in {"HANDOFF_READY", "PRESERVED"}:
        return False
    opened = False
    completed_cycle = False
    for event in record.history[old_version:]:
        if event.get("owner") != record.owner:
            return False
        kind, next_state = event.get("event"), event["to"]
        if kind == "REOPENED" and state in {"HANDOFF_READY", "PRESERVED"} and next_state == "ACTIVE":
            opened = True
        elif (
            opened and state == "ACTIVE"
            and (kind, next_state) in {
                ("HANDOFF_INTEGRATION_READY", "HANDOFF_READY"),
                ("HANDOFF_PRESERVED", "PRESERVED"),
            }
        ):
            opened = False
            completed_cycle = True
        elif kind not in {"REVIEWED", "SCOPE_AMENDED", "DISPOSABLE_REGISTERED"} or next_state != state:
            return False
        state = next_state
    if opened or not completed_cycle or record.history[old_version - 1].get("owner") != record.owner:
        return False
    repository = inventory(target)
    item = next((row for row in repository.worktrees if row.path == target), None)
    return bool(
        item is not None and not item.detached and item.branch
        and item.head == current["expected_head"]
        and resolve_commit(target, f"refs/heads/{item.branch}") == item.head
        and resolve_commit(target, previous["expected_head"]) == previous["expected_head"]
        and record.base_commit
        and is_ancestor(target, record.base_commit, previous["expected_head"])
        and is_ancestor(target, previous["expected_head"], current["expected_head"])
    )


def _same_completed_source(
    first: dict[str, Any], second: dict[str, Any], target: Path,
    *, record: MetadataRecord | None = None,
) -> bool:
    # Only an actual completed action supplies the second plan. A rebuilt source
    # node may supersede an older copy only across verified reopen/handoff history.
    if (
        first["source"] != second["source"]
        or first.get("source_ancestor_identities") != second.get("source_ancestor_identities")
    ):
        return False
    same_identity = first["authority"]["identity"] == second["authority"]["identity"]
    if same_identity:
        if first["source_anchor"] != second["source_anchor"]:
            return False
    elif (
        first["source_anchor"]["worktree"] != second["source_anchor"]["worktree"]
        or any(first["source_anchor"]["source"][key] != second["source_anchor"]["source"][key]
               for key in ("device", "file_type"))
        or not _reopened_source_identity(
            first["authority"]["identity"], second["authority"]["identity"], target, record,
        )
    ):
        return False
    common_source_root([first, second], target)
    return True


def _resumed_cleanup_identity(
    previous: dict[str, Any], current: dict[str, Any], record: MetadataRecord | None,
) -> bool:
    """Reuse completed cleanup only after a same-head retained/resumed handoff.

    The receipt keeps its original CAS. This proves a narrow official history
    suffix, not a general exemption for older receipts or changed HEADs.
    """
    stable = {"repository_id", "allocation_id", "target_path", "expected_head", "owner", "authority_key"}
    old = previous.get("expected_record_version")
    if (
        record is None or set(previous) != stable | {"expected_record_version"}
        or set(current) != set(previous)
        or any(previous[key] != current[key] for key in stable)
        or not isinstance(old, int) or isinstance(old, bool)
        or not 1 <= old < record.record_version
        or current["expected_record_version"] != record.record_version
        or record.record_version != len(record.history)
        or record.role not in {"feature", "audit"}
        or record.lifecycle != "HANDOFF_READY"
        or record.owner != current["owner"] or record.authority_key != current["authority_key"]
        or record.allocation_id != current["allocation_id"]
        or not record.finalizer_record_id
        or any(oid != current["expected_head"] for oid in (
            record.task_commit, record.handoff_commit, record.finalizer_commit,
        ))
    ):
        return False
    state = None
    for event in record.history[:old]:
        if event.get("from") != state or not event.get("to") or event.get("owner") != record.owner:
            return False
        state = event["to"]
    if state != "RETAINED":
        return False
    transitions = {
        ("RESUMED", "RETAINED", "ACTIVE"),
        ("PARKED", "ACTIVE", "RETAINED"),
        ("HANDOFF_INTEGRATION_READY", "ACTIVE", "HANDOFF_READY"),
    }
    for event in record.history[old:]:
        if (
            event.get("from") != state or event.get("owner") != record.owner
            or (event.get("event"), state, event.get("to")) not in transitions
        ):
            return False
        state = event["to"]
    return state == record.lifecycle


def _require_completion_coverage(
    bound: dict[str, Any], plans: list[dict[str, Any]], *, record: MetadataRecord | None = None,
) -> None:
    """A completed copy with an absent source requires actual action evidence.

    Inspect only this allocation's official generation metadata. Unreferenced
    generation payloads are not opened, and source absence grants no authority.
    """
    from .source_preservation import CONTRACT as SOURCE_CONTRACT

    allocation = p.evidence_root() / bound["repository_id"] / bound["allocation_id"]
    if not os.path.lexists(allocation):
        return
    if any(
        plan["authority"]["identity"] != bound
        and not _resumed_cleanup_identity(plan["authority"]["identity"], bound, record)
        for plan in plans
    ):
        raise _blocked("completed preservation belongs to another exact allocation identity")
    covered = {_source_key(plan) for plan in plans}
    with p._absolute_directory(allocation) as chain:
        for name in sorted(os.listdir(chain[-1].fd)):
            try:
                if str(uuid.UUID(name)) != name:
                    raise _blocked("allocation contains a noncanonical generation entry")
            except ValueError as exc:
                raise _blocked("allocation contains a noncanonical generation entry") from exc
            with p._absolute_directory(allocation / name) as generation:
                directory = generation[-1]
                try:
                    os.stat("receipt.json", dir_fd=directory.fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                receipt, _raw_sha = _document(directory, "receipt.json")
                plan, _ = _document(directory, "plan.json")
                if (
                    receipt.get("contract") not in {p.CONTRACT, SOURCE_CONTRACT}
                    or receipt.get("contract") != plan.get("contract")
                    or receipt.get("verification") != "PASS" or receipt.get("receipt_id") != name
                    or plan.get("generation") != name
                    or receipt.get("generation") != str(allocation / name)
                    or receipt.get("generation_identity") != directory.identity
                    or receipt.get("allocation_source_identity") != plan["authority"]["identity"]
                    or plan.get("state_root") != str(p.evidence_root())
                    or plan.get("destination") != str(allocation / name)
                    or receipt.get("plan_sha256") != plan.get("plan_sha256")
                    or p._sha({k: v for k, v in plan.items() if k != "plan_sha256"}) != plan["plan_sha256"]
                    or plan["authority"]["identity"].get("repository_id") != bound["repository_id"]
                    or plan["authority"]["identity"].get("allocation_id") != bound["allocation_id"]
                    or plan["authority"]["identity"].get("target_path") != bound["target_path"]
                ):
                    raise _blocked("allocation generation metadata changed")
                if plan["contract"] == SOURCE_CONTRACT:
                    tree = plan["source_tree"]
                    portable = {key: tree[key] for key in (
                        "base_head", "base_tree", "dirty_paths", "files", "reconstructed_tree_oid",
                    )}
                    if (
                        receipt.get("result") != "SOURCE_PRESERVATION_COMPLETE"
                        or plan.get("cleanup_authorized") is not False
                        or receipt.get("cleanup_authorized") is not False
                        or any(key in plan or key in receipt for key in (
                            "source", "source_relative", "source_anchor", "source_ancestor_identities",
                        ))
                        or tree.get("base_head") != plan["authority"]["identity"]["expected_head"]
                        or not isinstance(tree.get("files"), list)
                        or tree.get("dirty_paths") != [row["path"] for row in tree["files"]]
                        or any(key in tree for key in ("manifest", "inventory", "errors"))
                        or tree["tree_sha256"] != p._sha(portable)
                        or receipt.get("tree_sha256") != tree["tree_sha256"]
                        or receipt.get("reconstructed_tree_oid") != tree["reconstructed_tree_oid"]
                    ):
                        raise _blocked("tracked-source generation metadata changed")
                    # Tracked-source restoration has its own completed-action verifier.
                    # Its copy grants no ignored-resource cleanup authority.
                    continue
                tree = plan["source_tree"]
                relative = p.source_relative_path(Path(plan["source"]).relative_to(Path(bound["target_path"])).as_posix())
                if (
                    receipt.get("result") != "LEGACY_PRESERVATION_COMPLETE" or plan.get("ready") is not True
                    or plan.get("source_relative", relative) != relative
                    or receipt.get("source") != plan["source"] or receipt.get("source_anchor") != plan["source_anchor"]
                    or receipt.get("destination") != str(allocation / name / "tree")
                    or tree.get("errors") != [] or not isinstance(tree.get("manifest"), list)
                    or receipt.get("inventory") != tree["inventory"]
                    or receipt.get("source_tree_sha256") != tree["tree_sha256"]
                    or receipt.get("preserved_tree_sha256") != tree["tree_sha256"]
                    or receipt.get("source_identity_sha256") != p._sha(tree["source_identities"])
                    or tree["tree_sha256"] != p._sha([
                        {key: value for key, value in row.items() if key != "ownership"}
                        for row in tree["manifest"]
                    ])
                ):
                    raise _blocked("legacy generation metadata changed")
                if source_absent(plan, Path(bound["target_path"])) and (
                    not (plan["authority"]["identity"] == bound and _source_key(plan) in covered)
                    and not any(_same_completed_source(
                        plan, completed, Path(bound["target_path"]), record=record,
                    ) for completed in plans)
                ):
                    raise _blocked("absent preserved source has no actual completed action evidence")


def _pending_preservation_plans(
    store, item, bound: dict[str, Any], transaction: str, cleanup_recovery: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    from . import cleanup_evidence as e
    from .lifecycle import _release_plan_id

    if str(uuid.UUID(transaction)) != transaction:
        raise _blocked("canonical cleanup transaction identity required")
    journal = store._read_json(store.transactions_dir / f"{transaction}.json")
    plan = journal["cleanup_plan"]
    completed = [row["path"] for row in journal["actions"] if row["status"] == "COMPLETED"]
    if (
        journal.get("transaction_id") != transaction or journal.get("command") != "resource-clean"
        or journal.get("phase") != "PREPARED" or journal.get("branch") != item.branch
        or any(journal.get(key) != bound[key] for key in (
            "repository_id", "allocation_id", "target_path", "expected_head", "expected_record_version",
        ))
        or any(plan.get(key) != value for key, value in bound.items())
        or plan.get("plan_id") != _release_plan_id({k: v for k, v in plan.items() if k != "plan_id"})
        or [row["path"] for row in journal["actions"]] != plan["cleanup_paths"]
        or completed != plan["cleanup_paths"][:len(completed)]
        or (cleanup_recovery is not None and plan.get("recovery") != cleanup_recovery)
    ):
        raise _blocked("in-progress cleanup identity or action prefix changed")
    proofs = []
    for step, relative in enumerate(completed):
        path = store.cleanup_actions_dir / transaction / f"{step:04d}-after.json"
        after, digest = e.read(path)
        proofs.append({
            "resource": relative, "before": after["before"],
            "after": {"path": str(path), "sha256": digest},
        })
    done, _ = completed_actions(
        store, transaction, tuple(proofs), initial_topology=plan["hardlink_topology"], cleanup_paths=plan["cleanup_paths"],
    )
    evidence_set = plan.get("preservation_evidences")
    if evidence_set is None:
        evidence_set = [{"evidence": plan["preservation_evidence"]}] if plan.get("preservation_evidence") else []
    preserved_plans = []
    completed_plans = []
    for entry in evidence_set:
        evidence = entry["evidence"]
        verified, preserved = coverage(
            Path(evidence["receipt_path"]), evidence["receipt_sha256"], identity=bound, verify_source=False,
        )
        if verified != evidence:
            raise _blocked("in-progress preservation generation changed")
        preserved_plans.append(preserved)
        relative = p.source_relative_path(Path(preserved["source"]).relative_to(item.path).as_posix())
        if relative in done:
            if not source_absent(preserved, item.path):
                raise _blocked("completed preserved source reappeared")
            completed_plans.append(preserved)
    if preserved_plans:
        common_source_root(preserved_plans, item.path)
    return completed_plans


def cleanup_receipts(
    store, item, record, repository_id: str | None, *,
    expected_refs: list[dict[str, str]] | None = None,
    cleanup_recovery: dict[str, Any] | None = None,
    cleanup_transaction_id: str | None = None,
    allow_pending_typed_no_action: bool = False,
    include_no_action_refs: bool = False,
) -> list[dict[str, str]]:
    """Reverify exact-CAS completion bytes, action chain and preservation generations.

    Verified zero-effect refusals are included only for the release core's
    writer/runtime guard; archive consumers freeze successful completions only.
    """
    from . import cleanup_evidence as e
    from . import hardlinks
    from .lifecycle import (
        MULTIPLE_PRESERVED_CLEANUP_CONTRACT,
        TYPED_RESOURCE_CONTRACT,
        _preservation_source_relative,
        _release_plan_id,
        _validate_multiple_preservation_cleanup,
    )

    bound = {
        "repository_id": repository_id, "allocation_id": record.allocation_id,
        "target_path": str(item.path), "expected_head": item.head,
        "expected_record_version": record.record_version,
        "owner": record.owner, "authority_key": record.authority_key,
    }
    refs = []
    completed_preservation_plans = []
    try:
        if cleanup_recovery is not None:
            from .cleanup_recovery import original

            recovered_receipt, recovered_plan = original(
                store, cleanup_recovery, target=item.path, allocation_id=record.allocation_id,
            )
            if (
                any(recovered_receipt["plan"].get(key) != value for key, value in bound.items())
                or recovered_plan["authority"]["identity"] != bound
            ):
                raise _blocked("original recovery identity or exact CAS changed")
            from . import multiple_cleanup_recovery as multi_recovery

            if cleanup_recovery.get("contract") == multi_recovery.CONTRACT:
                _, preserved_plans, original_completed, _ = multi_recovery.details(
                    store, cleanup_recovery
                )
                completed_preservation_plans.extend(
                    plan for plan in preserved_plans
                    if _preservation_source_relative(plan, item.path) in original_completed
                )
        for path in sorted(store.completion_receipts_dir.glob("*.json")):
            with p._absolute_directory(path.parent) as chain:
                receipt, raw_sha = _document(chain[-1], path.name)
            plan = receipt.get("plan", {})
            if plan.get("allocation_id") != record.allocation_id:
                continue
            if plan.get("terminal_evidence") is not None:
                from .delivered_closeout import terminal_cleanup_evidence

                if terminal_cleanup_evidence(store, store._load_unlocked(), item, record) != plan["terminal_evidence"]:
                    raise _blocked("terminal completion evidence changed after cleanup")
            if plan.get("integration_abort_evidence") is not None:
                from .integration_cleanup import abort_evidence

                if abort_evidence(store, store._load_unlocked(), item, record) != plan["integration_abort_evidence"]:
                    raise _blocked("integration abort evidence changed after cleanup")
            if cleanup_recovery is not None and path.stem == cleanup_recovery["original_transaction_id"]:
                continue
            if "preservation_evidences" in plan and (
                receipt.get("contract") != MULTIPLE_PRESERVED_CLEANUP_CONTRACT
                or receipt.get("result") != "CLEANED"
            ):
                from . import multiple_cleanup_recovery as multi_recovery

                if multi_recovery.completed(store, path.stem, verify_generations=True):
                    completed_preservation_plans.extend(
                        multi_recovery.verified_completion(store, path.stem, bound=bound)
                    )
                    continue
                raise _blocked("multiple cleanup is incomplete or downgraded")
            if receipt.get("contract") not in {
                CONTRACT, MULTIPLE_PRESERVED_CLEANUP_CONTRACT, TYPED_RESOURCE_CONTRACT,
            }:
                continue
            from . import multiple_cleanup_recovery as multi_recovery

            if plan.get("recovery", {}).get("contract") == multi_recovery.CONTRACT:
                completed_preservation_plans.extend(
                    multi_recovery.recovered_receipt(store, receipt, bound)
                )
                refs.append({"receipt_id": path.stem, "path": str(path), "sha256": raw_sha})
                continue
            if receipt.get("result") == "FAILED_OR_PARTIAL":
                from .cleanup_recovery import completed

                if receipt.get("contract") == TYPED_RESOURCE_CONTRACT:
                    from .cleanup_attempts import _typed_journal, typed_terminal

                    if typed_terminal(store, path.stem) or allow_pending_typed_no_action:
                        journal, digest = e.read(store.transactions_dir / path.name)
                        verified_journal = _typed_journal(store, path.stem, digest)
                        if (
                            journal != verified_journal or journal["cleanup_plan"] != plan
                            or any(plan.get(key) != value for key, value in bound.items())
                            or plan.get("blockers") != []
                            or _release_plan_id({k: v for k, v in plan.items() if k != "plan_id"}) != plan.get("plan_id")
                            or plan.get("preservation_evidence") is not None or "preservation_evidences" in plan
                            or any(row.get("cleanup_action") == "remove-preserved-tree"
                                   or row.get("classification") == "exact-preserved" for row in plan["resources"])
                        ):
                            raise _blocked("typed no-action refusal identity or proof shape changed")
                        if include_no_action_refs:
                            refs.append({"receipt_id": path.stem, "path": str(path), "sha256": raw_sha})
                        continue
                if receipt.get("contract") == CONTRACT and completed(store, path.stem, verify_generations=True):
                    evidence = plan["preservation_evidence"]
                    verified, preserved = coverage(
                        Path(evidence["receipt_path"]), evidence["receipt_sha256"],
                        identity=bound, verify_source=False,
                    )
                    if verified != evidence:
                        raise _blocked("recovered original generation changed")
                    completed_preservation_plans.append(preserved)
                    continue
            receipt_bound = {key: plan.get(key) for key in bound}
            if receipt_bound != bound and not _resumed_cleanup_identity(receipt_bound, bound, record):
                raise _blocked("cleanup completion identity or exact CAS changed")
            if (
                str(uuid.UUID(path.stem)) != path.stem
                or receipt.get("result") != "CLEANED" or receipt.get("receipt_id") != path.stem
                or plan.get("contract") != receipt["contract"]
                or plan.get("blockers") != []
                or _release_plan_id({k: v for k, v in plan.items() if k != "plan_id"}) != plan.get("plan_id")
            ):
                raise _blocked("cleanup completion identity or exact CAS changed")
            preserved_rows = [
                row for row in plan["resources"]
                if row["cleanup_action"] == "remove-preserved-tree" or row["classification"] == "exact-preserved"
            ]
            expected_contract = (
                MULTIPLE_PRESERVED_CLEANUP_CONTRACT if "preservation_evidences" in plan
                else CONTRACT if plan.get("preservation_evidence") is not None or preserved_rows
                else TYPED_RESOURCE_CONTRACT
            )
            if receipt["contract"] != expected_contract or receipt["post_scan"].get("contract") != expected_contract:
                raise _blocked("cleanup contract differs from its exact preservation proof shape")
            paths = plan["cleanup_paths"]
            if not paths or len(set(paths)) != len(paths):
                raise _blocked("cleanup completion has no exact resources")
            for relative in paths:
                p.source_relative_path(relative)
            planned_paths = [r["path"] for r in plan["resources"] if r["cleanup_action"] == "remove-tree"] + [
                r["path"] for r in plan["resources"] if r["cleanup_action"] == "remove-preserved-tree"
            ]
            proofs = tuple(receipt.get("action_evidence", []))
            if (
                paths != planned_paths
                or receipt.get("actions") != [{"path": name, "status": "COMPLETED"} for name in paths]
                or len(proofs) != len(paths)
            ):
                raise _blocked("cleanup completion has missing action proofs")
            done, topology = completed_actions(
                store, path.stem, proofs, initial_topology=plan["hardlink_topology"], cleanup_paths=paths,
            )
            post = receipt["post_scan"]
            if (
                done != set(paths) or topology != post.get("hardlink_topology")
                or post.get("blockers") != [] or post.get("cleanup_paths") != []
                or any(post.get(key) != value for key, value in receipt_bound.items())
                or _release_plan_id({k: v for k, v in post.items() if k != "plan_id"}) != post.get("plan_id")
                or any(os.path.lexists(item.path / relative) for relative in paths)
            ):
                raise _blocked("cleanup post-scan or completed action chain changed")
            hardlinks.validate(topology)
            preservation_plans = []
            if receipt["contract"] == MULTIPLE_PRESERVED_CLEANUP_CONTRACT:
                preservation_plans = _validate_multiple_preservation_cleanup(receipt, item, receipt_bound)
            elif receipt["contract"] == CONTRACT:
                evidence = plan["preservation_evidence"]
                verified, preserved = coverage(
                    Path(evidence["receipt_path"]), evidence["receipt_sha256"],
                    identity=receipt_bound, verify_source=False,
                )
                relative = _preservation_source_relative(preserved, item.path)
                resources = [r for r in plan["resources"] if r["cleanup_action"] == "remove-preserved-tree"]
                if (
                    verified != evidence or len(resources) != 1
                    or resources[0]["path"] != relative or resources[0].get("evidence") != evidence
                    or resources[0].get("classification") != "exact-preserved" or relative not in done
                    or post.get("preservation_evidence") != evidence
                ):
                    raise _blocked("single preservation proof differs from its exact source")
                preservation_plans = [preserved]
            for preserved in preservation_plans:
                if not source_absent(preserved, item.path):
                    raise _blocked("completed preserved source reappeared")
            completed_preservation_plans.extend(preservation_plans)
            if preservation_plans:
                original_root = common_source_root(preservation_plans, item.path)
            else:
                with p._absolute_directory(item.path) as chain:
                    original_root = chain[-1].identity
            if any(anchor["worktree"] != original_root for anchor in plan["hardlink_topology"]["anchors"].values()):
                raise _blocked("cleanup graph is bound to another checkout root")
            refs.append({"receipt_id": path.stem, "path": str(path), "sha256": raw_sha})
        if cleanup_transaction_id is not None:
            completed_preservation_plans.extend(_pending_preservation_plans(
                store, item, bound, cleanup_transaction_id, cleanup_recovery,
            ))
        _require_completion_coverage(bound, completed_preservation_plans, record=record)
        if expected_refs is not None:
            if not isinstance(expected_refs, list) or len(expected_refs) != len(refs):
                raise _blocked("archive cleanup receipt set changed")
            for frozen, actual in zip(expected_refs, refs, strict=True):
                if (
                    set(frozen) != {"receipt_id", "path", "sha256"}
                    or frozen["receipt_id"] != actual["receipt_id"] or frozen["sha256"] != actual["sha256"]
                    or not e.matches_reference(frozen, Path(actual["path"]))
                ):
                    raise _blocked("archive cleanup receipt bytes or exact path changed")
    except ControllerError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, StopIteration) as exc:
        raise _blocked("cleanup completion evidence is unavailable, malformed or changed") from exc
    return refs


def delete_preserved_state(root: Path, evidence: dict[str, Any]) -> dict[str, Any]:
    from .hardlinks import external_contents, source_baseline

    topology = evidence.get("source_topology")
    original_evidence = {k: v for k, v in evidence.items() if k != "source_topology"}
    current, plan = coverage(
        Path(evidence["receipt_path"]), evidence["receipt_sha256"],
        identity=evidence["allocation_source_identity"], source_topology=topology,
    )
    source_rel = p.source_relative_path(Path(plan["source"]).relative_to(root).as_posix())
    source_full = root / source_rel
    if current != original_evidence or plan["source"] != str(source_full):
        raise _blocked("cleanup evidence changed")
    with p._absolute_directory(source_full.parent) as ancestors:
        with p._open_node(ancestors[-1].fd, source_full.name) as source:
            chain = (*ancestors, source)
            p._source_anchor(chain, plan)
            expected = (
                source_baseline(plan["source_tree"], topology, resource=source_rel)
                if topology is not None else plan["source_tree"]
            )
            p._same_source(p.scan_tree(source, chain), expected)
            leaf = not stat.S_ISDIR(expected["source_identities"]["."][2])
            if not leaf:
                entries, _ = _fd_inventory(source.fd, preserved_fifo=True)
                if {path: p._identity(entry.metadata) for path, entry in entries.items()} != expected["source_identities"]:
                    raise _blocked("source changed after its final content verification")
                return _delete_fd_tree(
                    source, ".", entries, chain,
                    external_contents=external_contents(topology, source_rel) if topology is not None else None,
                )
        # Close the inspection context before unlinking its name. A fresh O_PATH
        # descriptor binds the exact leaf; all parent FDs remain pinned and checked.
        row = expected["manifest"][0]
        external = external_contents(topology, source_rel).get(".") if topology is not None else None
        proof = _unlink_leaf(
            ancestors[-1].fd, source_full.name, expected["source_identities"]["."],
            link=row.get("target"), chain=ancestors, external=external,
        )
        key = f"{proof['identity'][0]}:{proof['identity'][1]}"
        return {key: proof} if external is not None else {}
