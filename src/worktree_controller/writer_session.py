"""Fail-closed executor admission; only post-exit cleanup is advisory.

Every caller uses this explicit session contract. No error-code denylist:
NOT_APPLICABLE requires positive evidence that the path is outside governance.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .git_backend import inventory, resolve_repository
from .models import ControllerError, WriterHolder
from .policy import load_policy
from .store import MetadataStore
from .writer_lease import (
    HOLDER_RESOLVER_VERSION,
    acquire_writer_lease,
    release_writer_lease,
    resolve_holder,
    same_holder,
    writer_requirement,
)

HOST_MANAGED_WRITER_SESSION_VERSION = 2
HOST_MANAGED_MODE = "host-managed-v2"
HOST_HANDOFF_CONTINUATION_VERSION = 2
HOST_ACCESS_CLASSES = ("read", "exec", "write")
# Lifecycles a holder's own handoff leaves on its still-leased allocation. They end
# feature writes on that checkout, not the holder's session.
CONTINUATION_LIFECYCLES = frozenset({"HANDOFF_READY", "PRESERVED"})


def host_managed_requested() -> bool:
    return "CONTROLLER_WRITER_MODE" in os.environ or "CONTROLLER_WRITER_CONTEXT" in os.environ


def _context_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate context key")
        value[key] = item
    return value


def host_check(
    path: Path, *, access: str | None = None, target: Path | None = None
) -> dict[str, Any]:
    """Validate a host declaration against current authority; never mutate a lease.

    Without ``access`` this is the feature-write entitlement. A caller naming one
    access class may also continue its own frozen handoff; see ``_continuation``.
    """
    from .controller import guard_write
    from .semantics import managed_record as _managed_record

    try:
        if access is not None and access not in HOST_ACCESS_CLASSES:
            raise ValueError("unsupported host access class")
        if (access == "write") != (target is not None):
            raise ValueError("a write access check requires exactly one target path")
        mode = os.environ.get("CONTROLLER_WRITER_MODE")
        if mode not in {HOST_MANAGED_MODE, "host-managed-v1"}:
            raise ValueError("unsupported or missing host-managed mode")
        context = json.loads(
            os.environ.get("CONTROLLER_WRITER_CONTEXT", ""), object_pairs_hook=_context_object
        )
        # A process survives activation with its original environment. Normalize
        # only the complete published v1 shape; authority still comes from the
        # current lease and its unchanged holder, never from this declaration.
        if mode == "host-managed-v1" and isinstance(context, dict):
            if "provider" not in context or "client_id" in context:
                raise ValueError("v1 host context requires provider identity")
            context["client_id"] = context.pop("provider")
        fields = {
            "holder",
            "client_id",
            "session_id",
            "run_id",
            "repository_path",
            "worktree_path",
            "allocation_id",
            "lease_id",
        }
        if not isinstance(context, dict) or context.keys() != fields:
            raise ValueError("host context requires the exact v2 fields, including explicit run_id")
        for key in fields - {"holder", "run_id"}:
            if not isinstance(context[key], str) or not context[key].strip():
                raise ValueError(f"invalid host context field: {key}")
        if context["run_id"] is not None and (
            not isinstance(context["run_id"], str) or not context["run_id"].strip()
        ):
            raise ValueError("run_id must be a nonempty string or explicit null")
        identity = context["holder"]
        if (
            not isinstance(identity, dict)
            or identity.keys() != {"pid", "boot_id", "start_ticks"}
            or any(
                type(identity[key]) is not int or identity[key] <= 0
                for key in ("pid", "start_ticks")
            )
            or not isinstance(identity["boot_id"], str)
            or not identity["boot_id"].strip()
        ):
            raise ValueError("complete host holder identity is required")
        declared = WriterHolder(
            **identity,
            client_id=context["client_id"],
            session_id=context["session_id"],
            run_id=context["run_id"],
        )
        holder = resolve_holder(
            str(declared.pid),
            client_id=declared.client_id,
            session_id=declared.session_id,
            run_id=declared.run_id,
        )
        if not same_holder(declared, holder):
            raise ControllerError("WRITER_LEASE_MISMATCH", "host holder differs from live identity")
        from .environment import verify_holder_lineage

        verify_holder_lineage(holder, os.getpid())
        # The snapshot loaded under the lock below is the metadata check.
        governed = governed_target(path, load_metadata=False)
        if governed is None:
            raise ControllerError("WRITER_LEASE_REQUIRED", "host-managed writes require governance")
        repository, root = governed
        if context["repository_path"] != str(repository) or context["worktree_path"] != str(root):
            raise ControllerError("WRITER_LEASE_MISMATCH", "host repository/worktree differs")
        state = inventory(root)
        store = MetadataStore(state.common_dir)
        if not store.lock_file.is_file() or store.lock_file.is_symlink():
            raise ControllerError("INVALID_METADATA", "host check requires its repository lock")
        # Hold the existing shared lock through the ordinary guard: lease ID and
        # entitlement cannot come from different Controller mutation snapshots.
        with store.lock(exclusive=False):
            snapshot = store._load_unlocked()
            item, record = _managed_record(state, snapshot, root)
            if item.detached or item.locked or item.prunable:
                raise ControllerError("INVALID_TARGET", "writer checkout requires review")
            lease = snapshot.writer_leases.get(record.allocation_id)
            if record.allocation_id != context["allocation_id"]:
                raise ControllerError("WRITER_LEASE_MISMATCH", "host allocation differs")
            if lease is None:
                raise ControllerError("WRITER_LEASE_REQUIRED", "host lease is missing")
            if lease.lease_id != context["lease_id"]:
                raise ControllerError("WRITER_LEASE_MISMATCH", "host lease ID differs")
            decision = guard_write(
                repository,
                target=root,
                task_key=record.task_key,
                authority_key=record.authority_key,
                owner=record.owner,
                operation="feature-write",
                holder=holder,
                observe=False,
                state=state,
                snapshot=snapshot,
            )["decision"]
            if decision["code"] != "WRITE_ALLOWED" and access is not None:
                return _continuation(
                    decision,
                    access=access,
                    target=target,
                    repository=repository,
                    root=root,
                    state=state,
                    snapshot=snapshot,
                    item=item,
                    record=record,
                    lease=lease,
                    holder=holder,
                )
        return {
            **decision,
            "admission": "GRANTED" if decision["code"] == "WRITE_ALLOWED" else "REFUSED",
        }
    except (ControllerError, OSError, ValueError) as exc:
        return {
            "admission": "REFUSED",
            "code": getattr(exc, "code", "INVALID_HOST_WRITER_CONTEXT"),
            "reason": str(exc),
        }


def _continuation(
    refusal: dict[str, Any],
    *,
    access: str,
    target: Path | None,
    repository: Path,
    root: Path,
    state: Any,
    snapshot: Any,
    item: Any,
    record: Any,
    lease: Any,
    holder: WriterHolder,
) -> dict[str, Any]:
    """Let the exact holder of its own frozen handoff continue through Controller.

    The handoff fence is unchanged: the frozen checkout still refuses writes and new
    bindings or writer sessions, which never pass an access class. The same holder
    may read, run commands whose Controller verbs keep their own guards, and write
    outside governance or into an integration candidate built from this allocation.
    Every other state keeps the original refusal.
    """
    from .controller import guard_write
    from .semantics import evidence_head_issues
    from .semantics import managed_record as _managed_record
    from .writer_lease import check_writer_entitlement

    if (
        refusal["code"] != "BLOCK_FEATURE_WRITE_LIFECYCLE"
        or record.lifecycle not in CONTINUATION_LIFECYCLES
    ):
        return {**refusal, "admission": "REFUSED"}
    # Same snapshot as the guard: only the live holder of this exact lease, with
    # no foreign runtime binding, keeps its session after its own handoff.
    check_writer_entitlement(snapshot.repository_id, record, item.path, lease, holder)
    drift = evidence_head_issues(item, record)
    if drift:
        raise ControllerError("RECOVERY_REQUIRED", "frozen handoff HEAD drift requires recovery")
    granted = {
        "admission": "CONTINUATION_GRANTED",
        "access": access,
        "allocation_id": record.allocation_id,
        "lifecycle": record.lifecycle,
    }
    if access != "write":
        return {**granted, "code": "HOST_HANDOFF_CONTINUATION",
                "reason": f"{access} access continues after this holder's handoff"}
    if target is None or not target.is_absolute():
        raise ControllerError("INVALID_TARGET", "continuation write target must be absolute")
    resolved = target.resolve(strict=False)
    directory = next(parent for parent in (resolved, *resolved.parents) if parent.is_dir())
    # Metadata of the target repository is loaded (and validated) below.
    governed = governed_target(directory, load_metadata=False)
    if governed is None:
        return {**granted, "code": "HOST_HANDOFF_CONTINUATION",
                "reason": "write target is outside Controller governance"}
    target_repository, target_root = governed
    if target_root == root:
        return {**refusal, "admission": "REFUSED"}
    same = (state, snapshot) if target_repository == repository else None
    held = _held_checkout(target_repository, target_root, holder, known=same)
    if held is not None:
        return {**granted, "code": "HOST_HANDOFF_CONTINUATION",
                "reason": "write target is another checkout this process holds a writer lease on",
                "target_allocation_id": held}
    if target_repository != repository:
        raise ControllerError(
            "BLOCK_HOST_CONTINUATION_TARGET", "frozen holder has no authority in that repository"
        )
    _target_item, candidate = _managed_record(state, snapshot, target_root)
    if candidate.role != "integration" or record.allocation_id not in candidate.source_allocations:
        raise ControllerError(
            "BLOCK_HOST_CONTINUATION_TARGET",
            "frozen holder may write only into an integration candidate of its own handoff",
        )
    decision = guard_write(
        repository,
        target=target_root,
        task_key=candidate.task_key,
        authority_key=candidate.authority_key,
        owner=candidate.owner,
        operation="integration-write",
        observe=False,
        state=state,
        snapshot=snapshot,
    )["decision"]
    if decision["code"] != "WRITE_ALLOWED":
        return {**decision, "admission": "REFUSED"}
    return {**granted, "code": "HOST_HANDOFF_CONTINUATION",
            "reason": "write target is an integration candidate of this handoff",
            "candidate_allocation_id": candidate.allocation_id}


def _held_checkout(
    repository: Path, root: Path, holder: WriterHolder, *, known: tuple[Any, Any] | None = None
) -> str | None:
    """The allocation at ``root`` when this exact live process holds its writer lease.

    One process may hold several leases under different client/session identities;
    the process identity (PID, boot ID, start ticks) is what the lease bound. The
    ordinary feature-write guard of that allocation still decides.
    """
    from .controller import guard_write
    from .semantics import managed_record as _managed_record

    if known is not None:
        state, snapshot = known
    else:
        state = inventory(root)
        snapshot = MetadataStore(state.common_dir).load()
    _item, record = _managed_record(state, snapshot, root)
    lease = snapshot.writer_leases.get(record.allocation_id)
    if lease is None or (lease.holder.pid, lease.holder.boot_id, lease.holder.start_ticks) != (
        holder.pid, holder.boot_id, holder.start_ticks
    ):
        return None
    decision = guard_write(
        repository,
        target=root,
        task_key=record.task_key,
        authority_key=record.authority_key,
        owner=record.owner,
        operation="feature-write",
        holder=lease.holder,
        observe=False,
        state=state,
        snapshot=snapshot,
    )["decision"]
    return record.allocation_id if decision["code"] == "WRITE_ALLOWED" else None


def _present(path: Path) -> bool:
    try:
        path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def _governance_marker(root: Path, common: Path | None = None) -> bool:
    if _present(root / ".agents/worktree-policy.toml") or _present(
        (common or root / ".git") / "worktree-controller"
    ):
        return True
    marker = root / ".git"
    if common is None and marker.is_file():
        # A broken linked HEAD can defeat Git discovery while its admin pointer
        # still identifies governance. These hints can only cause refusal, never
        # establish Git/worktree identity or grant admission.
        with marker.open("rb") as stream:
            pointer = os.fsdecode(stream.read(4097)).strip()
        if len(pointer) <= 4096 and pointer.startswith("gitdir: "):
            admin = root / pointer.removeprefix("gitdir: ")
            link = admin / "commondir"
            if _present(link):
                with link.open("rb") as stream:
                    relative = os.fsdecode(stream.read(4097)).strip()
                if relative and len(relative) <= 4096:
                    return _present(admin / relative / "worktree-controller")
    return False


def governed_target(path: Path, *, load_metadata: bool = True) -> tuple[Path, Path] | None:
    """Separate Git discovery failures from evidence of Controller governance.

    An unrelated broken ancestor marker is not a repository authority. Once a
    policy, metadata or actual governed Git root is found, all failures propagate.
    Nested repositories cannot hide an enclosing governed worktree.
    ``load_metadata=False`` is for callers that load (and so validate) the same
    metadata themselves before deciding anything.
    """
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise ControllerError("INVALID_TARGET", "writer path must be an existing directory")
    selected = None
    governed = None
    broken_roots = []
    for parent in (resolved, *resolved.parents):
        explicit = _governance_marker(parent)
        marker = _present(parent / ".git") or (
            _present(parent / "HEAD") and _present(parent / "objects")
        )
        if not explicit and not marker:
            continue
        try:
            root, common = resolve_repository(parent)
        except ControllerError as exc:
            # Only failed Git root discovery without governance evidence is
            # inapplicable. Inventory/policy/metadata errors below never enter here.
            if explicit or exc.code != "GIT_ERROR":
                raise
            broken_roots.append(parent)
            continue
        if root != parent:
            if explicit:
                raise ControllerError("GIT_DRIFT", "Git root does not match its discovery marker")
            broken_roots.append(parent)
            continue
        repository = inventory(root)
        if selected is None:
            selected = root
        if explicit or _governance_marker(repository.canonical_path, common):
            if any(broken.is_relative_to(root) for broken in broken_roots):
                raise ControllerError("GIT_ERROR", "malformed Git marker inside governed worktree")
            policy = load_policy(repository.canonical_path)
            if policy.path.is_symlink():
                raise ControllerError("INVALID_POLICY", "policy marker is a symlink")
            if load_metadata:
                MetadataStore(common).load()
            if selected != root:
                raise ControllerError(
                    "UNREGISTERED_WORKTREE", "nested repository lies inside a governed path"
                )
            governed = repository.canonical_path, root
    return governed


def describe(path: Path, *, selector: str | None = None) -> dict[str, Any]:
    """Project the recorded writer identity for a path. Read-only, grants nothing.

    A caller that survived a Controller activation may still speak an older request
    shape. Reconstructing the strict request needs the identity the Controller
    already recorded, not identity guessed from a pid, an owner or a directory, so
    this returns exactly what is on record and nothing derived.

    When the caller names the holder it believes it is, that selector is resolved by
    the same version 1 resolver every other path uses and compared against the
    recorded lease. ``holder_matches`` is the only agreement signal; it is never a
    substitute for the Controller's own checks on the request that follows.
    """
    from .semantics import managed_record as _managed_record

    try:
        target = governed_target(path)
        if target is None:
            return {"admission": "NOT_APPLICABLE", "code": "OUTSIDE_CONTROLLER_GOVERNANCE"}
        repository, root = target
        state = inventory(root)
        store = MetadataStore(state.common_dir)
        if not store.lock_file.is_file() or store.lock_file.is_symlink():
            raise ControllerError("INVALID_METADATA", "governed read requires its repository lock")
        with store.lock(exclusive=False):
            snapshot = store._load_unlocked()
            item, record = _managed_record(state, snapshot, root)
            lease = snapshot.writer_leases.get(record.allocation_id)
        matches: bool | None = None
        if selector is not None and lease is not None:
            try:
                matches = same_holder(
                    lease.holder,
                    resolve_holder(
                        selector,
                        client_id=lease.holder.client_id,
                        session_id=lease.holder.session_id,
                        run_id=lease.holder.run_id,
                    ),
                )
            except ControllerError:
                # An unresolvable or changed selector is a disagreement, never a match.
                matches = False
        return {
            "admission": "NOT_APPLICABLE",
            "code": "WRITER_SESSION_DESCRIBED",
            "repository_path": str(repository),
            "worktree_path": str(root),
            "allocation_id": record.allocation_id,
            "owner": record.owner,
            "role": record.role,
            "lifecycle": record.lifecycle,
            "writer_requirement": writer_requirement(record.role),
            "holder_matches": matches,
            "lease": None
            if lease is None
            else {
                "lease_id": lease.lease_id,
                "owner": lease.owner,
                "expires_at": lease.expires_at,
                "holder": asdict(lease.holder),
            },
        }
    except (ControllerError, OSError) as exc:
        return {
            "admission": "REFUSED",
            "code": getattr(exc, "code", "PATH_UNAVAILABLE"),
            "reason": str(exc),
        }


def check(
    path: Path,
    *,
    client_id: str,
    selector: str,
    session_id: str | None = None,
    run_id: str | None = None,
    access: str | None = None,
    target: Path | None = None,
) -> dict[str, Any]:
    """Check an existing entitlement; never acquire or infer one from a PID.

    ``access``/``target`` follow ``host_check``: the exact lease holder keeps its
    own frozen handoff's Controller flow, and nothing else changes.
    """
    from .controller import guard_write
    from .semantics import managed_record as _managed_record

    try:
        if access is not None and access not in HOST_ACCESS_CLASSES:
            raise ValueError("unsupported access class")
        if (access == "write") != (target is not None):
            raise ValueError("a write access check requires exactly one target path")
        governed = governed_target(path, load_metadata=False)
        if governed is None:
            return {"admission": "NOT_APPLICABLE", "code": "OUTSIDE_CONTROLLER_GOVERNANCE"}
        repository, root = governed
        state = inventory(root)
        store = MetadataStore(state.common_dir)
        if not store.lock_file.is_file() or store.lock_file.is_symlink():
            raise ControllerError("INVALID_METADATA", "governed check requires its repository lock")
        # Keep the guard and its continuation on one locked snapshot, as in
        # host_check. A concurrent lease/lifecycle change waits for this decision.
        with store.lock(exclusive=False):
            snapshot = store._load_unlocked()
            item, record = _managed_record(state, snapshot, root)
            if item.detached or item.locked or item.prunable:
                raise ControllerError("INVALID_TARGET", "writer checkout requires review")
            holder = resolve_holder(selector, client_id=client_id, session_id=session_id, run_id=run_id)
            if writer_requirement(record.role) == "NOT_APPLICABLE":
                return {
                    "admission": "NOT_APPLICABLE",
                    "code": "DEDICATED_ROLE_GATE",
                    "role": record.role,
                }
            decision = guard_write(
                repository,
                target=root,
                task_key=record.task_key,
                authority_key=record.authority_key,
                owner=record.owner,
                operation="feature-write",
                holder=holder,
                observe=False,
                state=state,
                snapshot=snapshot,
            )["decision"]
            if decision["code"] != "WRITE_ALLOWED" and access is not None:
                return _continuation(
                    decision,
                    access=access,
                    target=target,
                    repository=repository,
                    root=root,
                    state=state,
                    snapshot=snapshot,
                    item=item,
                    record=record,
                    lease=snapshot.writer_leases.get(record.allocation_id),
                    holder=holder,
                )
        return {
            **decision,
            "admission": "GRANTED" if decision["code"] == "WRITE_ALLOWED" else "REFUSED",
        }
    except (ControllerError, OSError, ValueError) as exc:
        return {
            "admission": "REFUSED",
            "code": getattr(exc, "code", "PATH_UNAVAILABLE"),
            "reason": str(exc),
        }


def admission(
    path: Path,
    *,
    client_id: str,
    selector: str,
    session_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    try:
        target = governed_target(path)
        if target is None:
            return {"admission": "NOT_APPLICABLE", "code": "OUTSIDE_CONTROLLER_GOVERNANCE"}
        repository, root = target
        holder = resolve_holder(selector, client_id=client_id, session_id=session_id, run_id=run_id)
        return acquire_writer_lease(
            repository,
            target=root,
            client_id=client_id,
            holder_pid=holder.pid,
            session_id=session_id,
            run_id=run_id,
            holder=holder,
        )["decision"]
    except (ControllerError, OSError) as exc:
        return {
            "admission": "REFUSED",
            "code": getattr(exc, "code", "PATH_UNAVAILABLE"),
            "reason": str(exc),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="worktree-controller writer-session")
    commands = parser.add_subparsers(dest="action", required=True)
    host_parser = commands.add_parser("host-check", help="read-only exact host entitlement check")
    host_parser.add_argument("path", type=Path)
    host_parser.add_argument("--access", choices=HOST_ACCESS_CLASSES)
    host_parser.add_argument("--target", type=Path)
    describe_parser = commands.add_parser(
        "describe", help="read-only recorded writer identity for a path"
    )
    describe_parser.add_argument("path", type=Path)
    describe_parser.add_argument("holder", nargs="?", default=None)
    acquire_parser = commands.add_parser("acquire")
    acquire_parser.add_argument("client_id")
    acquire_parser.add_argument("path", type=Path)
    acquire_parser.add_argument("holder", nargs="?", default=str(os.getppid()))
    check_parser = commands.add_parser("check")
    check_parser.add_argument("client_id")
    check_parser.add_argument("path", type=Path)
    check_parser.add_argument("holder", nargs="?", default=str(os.getppid()))
    check_parser.add_argument("--access", choices=HOST_ACCESS_CLASSES)
    check_parser.add_argument("--target", type=Path)
    release_parser = commands.add_parser("release")
    release_parser.add_argument("path", type=Path)
    release_parser.add_argument("holder", nargs="?", default=str(os.getppid()))
    release_parser.add_argument("--client-id", required=True)
    for subparser in (acquire_parser, check_parser, release_parser):
        subparser.add_argument("--session-id")
        subparser.add_argument("--run-id")
    parsed = parser.parse_args(argv)
    if parsed.action == "describe":
        # Deliberately before the host-managed branch: a projection is read-only and
        # never an admission, so a host context must not turn it into one.
        decision = describe(parsed.path, selector=parsed.holder)
        print(
            json.dumps(
                {
                    "command": "writer-session",
                    "holder_resolver_version": HOLDER_RESOLVER_VERSION,
                    "decision": decision,
                },
                sort_keys=True,
            )
        )
        return 0 if decision.get("code") != "PATH_UNAVAILABLE" else 2
    if parsed.action == "host-check" or host_managed_requested():
        # Even a legacy acquire/release invocation cannot mutate a host-owned lease.
        decision = host_check(
            parsed.path,
            access=getattr(parsed, "access", None),
            target=getattr(parsed, "target", None),
        )
        result = 0 if decision.get("admission") in {"GRANTED", "CONTINUATION_GRANTED"} else 2
    elif parsed.action in {"acquire", "check"}:
        extra = (
            {"access": parsed.access, "target": parsed.target} if parsed.action == "check" else {}
        )
        operation = admission if parsed.action == "acquire" else check
        decision = operation(
            parsed.path,
            client_id=parsed.client_id,
            selector=parsed.holder,
            session_id=parsed.session_id,
            run_id=parsed.run_id,
            **extra,
        )
        result = (
            0
            if decision.get("admission") in {"GRANTED", "NOT_APPLICABLE", "CONTINUATION_GRANTED"}
            else 2
        )
    else:
        # This entry point is for post-exit cleanup; failures stay visible but cannot
        # replace the original client_id exit status. Direct release API/CLI is strict.
        try:
            target = governed_target(parsed.path)
            if target is None:
                decision = {"code": "NO_TERMINAL_ACTION"}
            else:
                repository, root = target
                holder = resolve_holder(
                    parsed.holder,
                    client_id=parsed.client_id,
                    session_id=parsed.session_id,
                    run_id=parsed.run_id,
                )
                decision = release_writer_lease(repository, target=root, holder=holder)["decision"]
        except (ControllerError, OSError) as exc:
            decision = {
                "code": "NO_TERMINAL_ACTION",
                "blocker": getattr(exc, "code", "PATH_UNAVAILABLE"),
                "reason": str(exc),
            }
        result = 0
    print(
        json.dumps(
            {
                "command": "writer-session",
                "holder_resolver_version": HOLDER_RESOLVER_VERSION,
                "decision": decision,
            },
            sort_keys=True,
        )
    )
    return result


def run_main(argv: list[str] | None = None) -> int:
    """Run any executable under the same explicit writer contract.

    The parent holds the lease until its child finishes. Nested tools receive the
    exact host context, so they check that lease instead of acquiring another one.
    """
    parser = argparse.ArgumentParser(prog="worktree-session", allow_abbrev=False)
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(argv)
    command = parsed.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("an executable after -- is required")
    if not parsed.session_id.strip() or (parsed.run_id is not None and not parsed.run_id.strip()):
        parser.error("session_id and an explicit run_id must be nonempty")
    try:
        resolve_holder(
            str(os.getpid()),
            client_id=parsed.client_id,
            session_id=parsed.session_id,
            run_id=parsed.run_id,
        )
    except ControllerError as exc:
        parser.error(str(exc))
    path = parsed.path.expanduser().resolve()
    identity = {
        "client_id": parsed.client_id,
        "selector": str(os.getpid()),
        "session_id": parsed.session_id,
        "run_id": parsed.run_id,
    }
    hosted = host_managed_requested()
    execution = None
    projected = None
    if not hosted:
        projected = describe(path)
        if projected.get("admission") == "REFUSED":
            print(json.dumps(projected, sort_keys=True), file=sys.stderr)
            return 2
        execution = "managed" if projected.get("writer_requirement") == "REQUIRED" else "host"

    def reserve_execution(*, cancel: bool = False) -> dict[str, Any]:
        from .resource_admission import admit_execution, host_execution

        if execution == "managed":
            return admit_execution(Path(projected["repository_path"]), target=Path(projected["worktree_path"]),
                owner=projected["owner"], client_id=parsed.client_id, session_id=parsed.session_id,
                run_id=parsed.run_id, cancel=cancel)["decision"]
        return host_execution("cancel" if cancel else "admit", target=path,
            client_id=parsed.client_id, session_id=parsed.session_id, run_id=parsed.run_id)["decision"]

    try:
        resources = reserve_execution() if execution else None
    except (ControllerError, OSError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    waiting_reason = None
    while resources and resources.get("code") == "RESOURCE_WAIT":
        # This existing session wrapper owns the command. Waiting owns no writer;
        # the persisted operation can be inspected after disconnection/restart.
        reason = (resources.get("code"), tuple(resources.get("blockers", [])))
        # Ctrl-C anywhere in the wait (report, sleep or re-poll) cancels it; a request
        # admitted meanwhile refuses cancellation and stays for reconciliation.
        try:
            if reason != waiting_reason:
                print(json.dumps({key: resources.get(key) for key in ("code", "blockers", "request_ids")}, sort_keys=True), file=sys.stderr)
                waiting_reason = reason
            import time

            time.sleep(5)
            try:
                resources = reserve_execution()
            except (ControllerError, OSError) as exc:
                print(f"Waiting request retained for reconciliation: {exc}", file=sys.stderr)
                return 2
        except KeyboardInterrupt:
            try:
                reserve_execution(cancel=True)
            except (ControllerError, OSError, KeyError) as exc:
                print(f"Waiting request retained for reconciliation: {type(exc).__name__}", file=sys.stderr)
            return 130
    admitted = host_check(path) if hosted else admission(path, **identity)
    if admitted.get("admission") not in {"GRANTED", "NOT_APPLICABLE"}:
        print(json.dumps(admitted, sort_keys=True), file=sys.stderr)
        if execution:
            try:
                reserve_execution(cancel=True)
            except (ControllerError, OSError) as exc:
                print(f"Admission cleanup deferred: {exc}", file=sys.stderr)
        return 2
    own_lease = not hosted and admitted.get("admission") == "GRANTED"
    projection = None
    finished = False
    runtime_bound = False
    try:
        checked = host_check(path) if hosted else check(path, **identity)
        if checked.get("admission") not in {"GRANTED", "NOT_APPLICABLE"}:
            print(json.dumps(checked, sort_keys=True), file=sys.stderr)
            return 2
        environment = os.environ.copy()
        if own_lease:
            projection = describe(path, selector=identity["selector"])
            lease = projection.get("lease")
            if (
                projection.get("holder_matches") is not True
                or not isinstance(lease, dict)
                or lease.get("lease_id") != admitted.get("lease_id")
            ):
                raise ControllerError("WRITER_LEASE_MISMATCH", "admitted lease changed")
            recorded = lease["holder"]
            context = {
                "holder": {key: recorded[key] for key in ("pid", "boot_id", "start_ticks")},
                **{key: recorded[key] for key in ("client_id", "session_id", "run_id")},
                **{
                    key: projection[key]
                    for key in ("repository_path", "worktree_path", "allocation_id")
                },
                "lease_id": lease["lease_id"],
            }
            environment["CONTROLLER_WRITER_MODE"] = HOST_MANAGED_MODE
            environment["CONTROLLER_WRITER_CONTEXT"] = json.dumps(context, sort_keys=True)
        if execution == "managed":
            from .lifecycle import update_external_binding
            from .writer_lease import RUNTIME_BINDING_PREFIX

            snapshot = MetadataStore(inventory(path).common_dir).load()
            record = snapshot.records[projection["allocation_id"]]
            binding = RUNTIME_BINDING_PREFIX + json.dumps({"holder": recorded,
                "lease_id": admitted["lease_id"], "kind": "command"}, sort_keys=True, separators=(",", ":"))
            update_external_binding(Path(projection["repository_path"]), target=Path(projection["worktree_path"]),
                owner=record.owner, expected_record_version=record.record_version, binding=binding, action="add")
            runtime_bound = True
        elif execution == "host":
            from .resource_admission import host_execution

            host_execution("bind", request_id=resources["request_ids"][0], holder_pid=os.getpid())
            runtime_bound = True
        result = subprocess.run(command, cwd=path, env=environment, check=False).returncode
        finished = True
        return result if result >= 0 else 128 - result
    except KeyboardInterrupt:
        return 130
    except (ControllerError, OSError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    finally:
        if not hosted and finished and projected and projected.get("repository_path"):
            try:
                from .session import main as session_end

                # The starting checkout may already have been handed off and
                # released. Select the real session across the repository.
                with contextlib.redirect_stdout(sys.stderr):
                    session_end(["--path", projected["repository_path"],
                                 "--trigger", "task-terminal", "--client-id", parsed.client_id,
                                 "--session-id", parsed.session_id, "--holder", identity["selector"],
                                 *(["--run-id", parsed.run_id] if parsed.run_id is not None else [])])
            except (ControllerError, OSError) as exc:
                print(f"Session cleanup deferred: {exc}", file=sys.stderr)
        elif own_lease:
            try:
                target = governed_target(path)
                if target is not None:
                    repository, root = target
                    holder = resolve_holder(
                        identity["selector"],
                        client_id=parsed.client_id,
                        session_id=parsed.session_id,
                        run_id=parsed.run_id,
                    )
                    release_writer_lease(
                        repository, target=root, holder=holder, lease_id=admitted["lease_id"]
                    )
            except (ControllerError, OSError) as exc:
                print(f"Session cleanup deferred: {exc}", file=sys.stderr)
        if execution and not runtime_bound:
            try:
                reserve_execution(cancel=True)
            except (ControllerError, OSError) as exc:
                print(f"Execution cleanup deferred: {exc}", file=sys.stderr)
