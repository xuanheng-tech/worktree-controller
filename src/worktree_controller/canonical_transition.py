"""Move a frozen canonical checkout onto an exact published main commit."""

from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

from .controller import status
from .git_backend import (
    _git,
    fetch_remote_branch,
    git_write,
    head_paths,
    inventory,
    is_ancestor,
    resolve_commit,
    update_branch_ref,
    validate_branch_name,
)
from .lifecycle import _updated_record
from .models import (
    ControllerError,
    MetadataRecord,
    MetadataSnapshot,
    RepositoryInventory,
    WorktreeGitState,
    WorktreePolicy,
)
from .policy import default_policy, load_policy, parse_policy
from .semantics import managed_record as _managed_record
from .store import MetadataStore
from .transaction_scope import conflicts


def _check_frozen(
    repository: RepositoryInventory,
    item: WorktreeGitState,
    record: MetadataRecord,
    snapshot: MetadataSnapshot,
    *,
    expected_branch: str,
    expected_head: str,
    owner: str,
    target_branch: str,
    target_oid: str,
    expected_main_head: str,
) -> None:
    if not item.canonical or item.path != repository.canonical_path:
        raise ControllerError("BLOCK_CANONICAL", "transition requires the canonical checkout")
    if record.owner != owner:
        raise ControllerError("OWNER_MISMATCH", "frozen canonical owner differs")
    if record.lifecycle != "HANDOFF_READY" or record.role not in {"canonical", "control-plane"}:
        raise ControllerError("INVALID_LIFECYCLE", "canonical transition requires a frozen handoff")
    if not record.finalizer_record_id or any(
        value != expected_head
        for value in (record.handoff_commit, record.task_commit, record.finalizer_commit)
    ):
        raise ControllerError("HANDOFF_IDENTITY_MISMATCH", "frozen handoff evidence differs")
    if item.detached or item.locked or item.prunable or not item.clean:
        raise ControllerError("DIRTY_WORKTREE", "canonical checkout is not clean and attached")
    refresh_main = expected_branch == target_branch
    starting_head = expected_main_head if refresh_main else expected_head
    if (item.branch, item.head) not in {
        (expected_branch, starting_head),
        (target_branch, target_oid),
    }:
        raise ControllerError("HEAD_MISMATCH", "canonical branch and HEAD disagree")
    retained_head = resolve_commit(item.path, f"refs/heads/{expected_branch}")
    if retained_head not in ({expected_main_head, target_oid} if refresh_main else {expected_head}):
        raise ControllerError("HEAD_MISMATCH", "frozen branch no longer retains its handoff")
    if record.external_bindings or any(
        resource["state"] != "cleaned" for resource in record.disposable_resources
    ):
        raise ControllerError("ACTIVE_BINDING", "canonical transition has a resource binding")
    if record.source_allocations or record.quarantine_expected_commit or record.recovery_ref:
        raise ControllerError(
            "ACTIVE_LINEAGE", "canonical transition cannot discard recovery lineage"
        )
    if snapshot.publication_lease is not None or record.allocation_id in snapshot.writer_leases:
        raise ControllerError("ACTIVE_LEASE", "canonical transition conflicts with an active lease")
    if any(
        record.allocation_id in intent.source_allocations
        and intent.state not in {"RELEASED", "ABANDONED"}
        for intent in snapshot.integration_intents.values()
    ) or any(
        intent.allocation_id == record.allocation_id for intent in snapshot.release_intents.values()
    ):
        raise ControllerError(
            "ACTIVE_INTENT", "canonical transition conflicts with an active intent"
        )
    if any(
        other.path != item.path and other.branch == target_branch for other in repository.worktrees
    ):
        raise ControllerError("BRANCH_OCCUPIED", "target main branch is checked out elsewhere")


def _published_policy(
    repository: RepositoryInventory, target_oid: str, current: WorktreePolicy
) -> WorktreePolicy:
    relative = ".agents/worktree-policy.toml"
    entries = _git(
        repository.canonical_path, "ls-tree", "-z", target_oid, "--", relative
    ).stdout.split(b"\0")
    entries = [entry for entry in entries if entry]
    if not entries:
        parent = _git(repository.canonical_path, "ls-tree", target_oid, "--", ".agents").stdout
        if parent and not parent.startswith(b"040000 tree "):
            raise ControllerError("INVALID_POLICY", "published policy parent is not a directory")
        # An untracked local policy survives a fast-forward; a tracked deletion does not.
        return (
            default_policy(repository.canonical_path)
            if head_paths(repository.canonical_path, relative)
            else current
        )
    header, separator, name = entries[0].partition(b"\t")
    fields = header.split()
    if (
        len(entries) != 1
        or not separator
        or name != relative.encode()
        or len(fields) != 3
        or fields[0] not in {b"100644", b"100755"}
        or fields[1] != b"blob"
    ):
        raise ControllerError("INVALID_POLICY", "published policy is not a regular file")
    raw = _git(repository.canonical_path, "cat-file", "blob", fields[2].decode()).stdout
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ControllerError("INVALID_POLICY", f"cannot read published policy: {exc}") from exc
    return parse_policy(repository.canonical_path, data)


def _check_target_policy(policy: WorktreePolicy, branch: str, remote: str) -> None:
    if (
        not policy.present
        or not policy.canonical_protected
        or policy.publication_branch != branch
        or policy.publication_remote != remote
        or "control-plane" not in policy.canonical_allowed_roles
    ):
        raise ControllerError("INVALID_TARGET_POLICY", "published main has incompatible policy")


def transition_canonical(
    repository_path: Path,
    *,
    target: Path,
    expected_record_version: int,
    owner: str,
    expected_branch: str,
    expected_head: str,
    expected_main_head: str,
    expected_remote_head: str,
    new_owner: str,
    new_task_key: str,
    new_authority_key: str,
) -> dict[str, Any]:
    """Fast-forward local main and rebind canonical authority without releasing its path.

    A retry can finish a checkout that reached the exact target before metadata was
    written. A frozen publication branch is refreshed in place and retains its
    handoff evidence; other frozen branches retain both their ref and evidence.
    """

    if not all(
        value.strip() and "\x00" not in value
        for value in (owner, new_owner, new_task_key, new_authority_key)
    ):
        raise ControllerError("INVALID_IDENTITY", "transition identities must be nonempty")
    repository_initial = inventory(repository_path)
    store = MetadataStore(repository_initial.common_dir)
    with store.lock(exclusive=True):
        repository = inventory(repository_path)
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        if record.record_version != expected_record_version:
            raise ControllerError("CAS_MISMATCH", "canonical record version changed")
        if any(
            other.allocation_id != record.allocation_id
            and other.lifecycle not in {"RELEASED", "ARCHIVED"}
            and (other.task_key == new_task_key or other.authority_key == new_authority_key)
            for other in snapshot.records.values()
        ):
            raise ControllerError(
                "AUTHORITY_CONFLICT", "new control-plane identity is already active"
            )
        policy = load_policy(repository.canonical_path)
        if (
            not policy.present
            or not policy.canonical_protected
            or policy.publication_branch not in {"main", "master"}
        ):
            raise ControllerError(
                "INVALID_POLICY", "canonical transition requires governed main or master"
            )
        remote = policy.publication_remote
        main_branch = policy.publication_branch
        validate_branch_name(item.path, expected_branch)
        refresh_main = expected_branch == main_branch
        if len(expected_remote_head) != 40 or any(
            character not in "0123456789abcdef" for character in expected_remote_head
        ):
            raise ControllerError(
                "INVALID_COMMIT", "expected remote HEAD must be a full commit OID"
            )
        source_oid = resolve_commit(item.path, expected_head)
        original_main = resolve_commit(item.path, expected_main_head)
        if (source_oid, original_main) != (expected_head, expected_main_head):
            raise ControllerError("INVALID_COMMIT", "transition requires full commit OIDs")
        _check_frozen(
            repository,
            item,
            record,
            snapshot,
            expected_branch=expected_branch,
            expected_head=expected_head,
            owner=owner,
            target_branch=main_branch,
            target_oid=expected_remote_head,
            expected_main_head=original_main,
        )
        if conflicts(
            store, allocation=record.allocation_id, target=item.path, assume_serialized=True
        ):
            raise ControllerError(
                "INTERRUPTED_TRANSACTION", "canonical transition conflicts with a transaction"
            )
        target_oid = fetch_remote_branch(item.path, remote, main_branch)
        if target_oid != expected_remote_head:
            raise ControllerError(
                "REMOTE_DRIFT", "remote main differs from the exact expected commit"
            )
        if (
            not is_ancestor(item.path, source_oid, target_oid)
            or not is_ancestor(item.path, original_main, target_oid)
            or (refresh_main and not is_ancestor(item.path, source_oid, original_main))
        ):
            raise ControllerError(
                "NON_FAST_FORWARD", "remote main does not contain frozen and local main commits"
            )
        local_main = resolve_commit(item.path, f"refs/heads/{main_branch}")
        if local_main not in {original_main, target_oid}:
            raise ControllerError("MAIN_DRIFT", "local main changed outside transition bounds")
        if refresh_main:
            # Validate the immutable target before moving a checked-out publication ref.
            _check_target_policy(_published_policy(repository, target_oid, policy), main_branch, remote)
            if local_main != target_oid:
                git_write(
                    item.path, ("merge", "--ff-only", "--no-edit", "--no-overwrite-ignore", target_oid)
                )
        else:
            if local_main == original_main and original_main != target_oid:
                update_branch_ref(item.path, main_branch, expected=original_main, new=target_oid)
            if item.branch == expected_branch:
                git_write(item.path, ("checkout", "--no-overwrite-ignore", main_branch))
        try:
            after = inventory(item.path)
            current = next(worktree for worktree in after.worktrees if worktree.canonical)
            if current.branch != main_branch or current.head != target_oid or not current.clean:
                raise ControllerError(
                    "TRANSITION_INCOMPLETE", "canonical checkout did not reach exact main"
                )
            target_policy = load_policy(after.canonical_path)
            _check_target_policy(target_policy, main_branch, remote)
            if fetch_remote_branch(item.path, remote, main_branch) != target_oid:
                raise ControllerError(
                    "REMOTE_DRIFT", "remote main moved during canonical transition"
                )
        except ControllerError:
            # An in-place advance stays frozen for exact recovery; never rewind main.
            if not refresh_main and item.branch == expected_branch:
                git_write(item.path, ("checkout", "--no-overwrite-ignore", expected_branch))
            raise
        updated = _updated_record(
            record,
            "CANONICAL_MAIN_REFRESHED" if refresh_main else "CANONICAL_TRANSITIONED",
            lifecycle="ACTIVE",
            owner=new_owner,
            task_key=new_task_key,
            authority_key=new_authority_key,
            module="repository",
            role="control-plane",
            base_commit=target_oid,
            task_commit=target_oid,
            handoff_commit=None,
            finalizer_commit=None,
            finalizer_record_id=None,
            scope_paths=(),
            source_allocations=(),
        )
        event = {
            **updated.history[-1],
            "old_branch": expected_branch,
            "old_head": source_oid,
            "old_owner": owner,
            "old_task_key": record.task_key,
            "old_authority_key": record.authority_key,
            "old_finalizer_record_id": record.finalizer_record_id,
            "old_scope_paths": list(record.scope_paths),
            "main_before": original_main,
            "main_after": target_oid,
            "remote": remote,
        }
        updated = replace(updated, history=(*updated.history[:-1], event))
        store.replace_record(updated)
    payload = status(repository_path)
    payload["command"] = "canonical-transition"
    payload["decision"] = {
        "code": "CANONICAL_MAIN_REFRESHED" if refresh_main else "CANONICAL_TRANSITIONED",
        "allocation_id": updated.allocation_id,
        "record_version": updated.record_version,
        "old_branch": expected_branch,
        "old_head": source_oid,
        "main_head": target_oid,
        "reason": (
            "exact remote main verified; frozen handoff history retained and canonical authority moved"
            if refresh_main else
            "exact remote main verified; frozen branch retained and canonical authority moved"
        ),
    }
    return payload
