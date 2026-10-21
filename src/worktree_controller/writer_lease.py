"""Writer leases: temporary write entitlement, separate from durable task identity.

An allocation records *who owns a task* and preserves its work. A writer lease
records *which live process may currently write through it*. Keeping the two apart
is what lets capacity be reclaimed without ever asserting that a task finished: a
lease can be proven dead while the allocation, its owner and its uncommitted work
stay exactly as they were.

Holder identity deliberately never rests on a pid alone. Linux reuses pids, so a
lease naming a recycled pid would read as live and permanently pin a writer slot.
The holder is the triple (boot id, pid, kernel start ticks), which no later process
can satisfy, optionally strengthened by a client_id session id.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import (
    CANONICAL_ROLES,
    FEATURE_AUTHORITY_ROLES,
    RECOVERY_ROLES,
    ControllerError,
    MetadataRecord,
    WriterHolder,
    WriterLease,
)
from .store import WRITER_LEASE_SCHEMA_VERSION

CLIENT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
HOLDER_RESOLVER_VERSION = 1
_BOOT_ID_FILE = Path("/proc/sys/kernel/random/boot_id")
RUNTIME_BINDING_PREFIX = "runtime:process-v1:"


def runtime_binding(value: str) -> tuple[WriterHolder, str | None] | None:
    """Decode a typed occupancy within the existing external-binding registry."""
    if not value.startswith(RUNTIME_BINDING_PREFIX):
        return None
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate runtime identity field")
            result[key] = item
        return result

    try:
        data = json.loads(value[len(RUNTIME_BINDING_PREFIX):], object_pairs_hook=unique)
        if set(data) != {"holder", "lease_id", "kind"} or data["kind"] not in {"agent", "test", "preview", "background", "command"}:
            raise ValueError("invalid runtime binding")
        identity = data["holder"]
        if set(identity) != {"pid", "boot_id", "start_ticks", "client_id", "session_id", "run_id"}:
            raise ValueError("incomplete runtime holder")
        if any(type(identity[key]) is not int or identity[key] < 1 for key in ("pid", "start_ticks")):
            raise ValueError("invalid runtime holder")
        if not isinstance(identity["client_id"], str) or not CLIENT_ID_PATTERN.fullmatch(identity["client_id"]):
            raise ValueError("invalid runtime client")
        for key in ("session_id", "run_id"):
            if identity[key] is not None and (not isinstance(identity[key], str) or not identity[key] or "\x00" in identity[key]):
                raise ValueError("invalid runtime identity")
        uuid.UUID(identity["boot_id"])
        if data["lease_id"] is not None:
            uuid.UUID(data["lease_id"])
        return WriterHolder(**identity), data["lease_id"]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ControllerError("INVALID_RUNTIME_BINDING", "runtime occupancy identity is malformed") from exc


def assert_runtime_clear(target: Path, holders: tuple[WriterHolder, ...]) -> None:
    """Clear this execution's occupancy; allocation release still checks all references."""
    from .environment import _current_process_identity, runtime_authority_evidence

    if any(holder_is_live(holder) or process_group_is_live(holder) for holder in holders):
        raise ControllerError("RUNTIME_OCCUPIED", "runtime holder is still alive")
    evidence = runtime_authority_evidence(target, holders=holders)
    observed = []
    for reference in evidence["observed_references"]:
        process = _current_process_identity(Path("/proc") / str(reference["pid"]))
        # A still-open human terminal can predate this entire execution. Kernel
        # creation order proves it is not a descendant of any declared holder.
        # Newer, reused or unobservable identities remain blocking. The allocation
        # cleanup path retains this positive reference without this exclusion.
        if (not holders or process is None or any(holder.boot_id != boot_id()
                or process.start_ticks >= holder.start_ticks for holder in holders)):
            observed.append(reference)
    if observed or any(evidence[key] for key in ("managed_references", "managed_visibility_uncertainties", "scan_uncertainties")):
        raise ControllerError("RUNTIME_OCCUPIED", "runtime remains until managed and observed use is clear")


def foreign_runtime_bindings(record: MetadataRecord, holder: WriterHolder, lease_id: str | None) -> tuple[str, ...]:
    """One owner's runtime occupancy does not fence its own write entitlement."""
    foreign = []
    for value in record.external_bindings:
        declared = runtime_binding(value)
        # A descendant already admitted by the formal transfer operation may keep
        # running after the root writer exits. It shares occupancy, not ownership.
        if (declared is None or lease_id != declared[1]
                or (holder.client_id, holder.session_id, holder.run_id)
                != (declared[0].client_id, declared[0].session_id, declared[0].run_id)):
            foreign.append(value)
    return tuple(foreign)


def writer_requirement(role: str) -> str:
    """Classify declared roles, never Controller errors or future unknown roles.

    NOT_APPLICABLE grants no write authority: integration, recovery, publication
    and control-plane operations retain their dedicated write/lifecycle gates.
    """
    if role in FEATURE_AUTHORITY_ROLES:
        return "REQUIRED"
    if role in CANONICAL_ROLES | RECOVERY_ROLES:
        return "NOT_APPLICABLE"
    raise ControllerError("INVALID_ROLE", f"no writer applicability contract for role {role}")


def now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def boot_id() -> str:
    """Identify this boot, so a lease can never survive a reboot into a reused pid."""
    try:
        value = _BOOT_ID_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if not value or value == "unknown-boot":
        raise ControllerError("INVALID_HOLDER", "boot identity is unavailable")
    return value


def start_ticks(pid: int) -> int | None:
    """Kernel start time for ``pid``, or None when the process is gone.

    Field 22 of ``/proc/<pid>/stat`` counts clock ticks since boot. The comm field
    can contain spaces and parentheses, so the tail is parsed after the final ')'.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as exc:
        raise ControllerError("INVALID_HOLDER", f"process {pid} cannot be inspected") from exc
    cut = raw.rfind(")")
    if cut == -1:
        raise ControllerError("INVALID_HOLDER", "invalid process stat")
    fields = raw[cut + 2 :].split()
    if len(fields) < 20:
        raise ControllerError("INVALID_HOLDER", "incomplete process stat")
    try:
        return int(fields[19])
    except ValueError as exc:
        raise ControllerError("INVALID_HOLDER", "invalid process start ticks") from exc


def resolve_holder(
    selector: str, *, client_id: str, session_id: str | None = None, run_id: str | None = None
) -> WriterHolder:
    """Version 1 resolver shared by CLI, hooks and wrappers; never match shell arguments."""
    if selector.startswith("ancestor:"):
        name = selector.removeprefix("ancestor:")
        if not name or Path(name).name != name:
            raise ControllerError("INVALID_HOLDER", "an ancestor program name is required")
        pid = os.getppid()
        for _ in range(32):
            if pid <= 1:
                break
            try:
                before = start_ticks(pid)
                argv0 = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0", 1)[0]
                executable = Path(os.readlink(f"/proc/{pid}/exe"))
                raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
                parent = int(raw[raw.rfind(")") + 2 :].split()[1])
            except (OSError, ValueError, IndexError) as exc:
                raise ControllerError("INVALID_HOLDER", "ancestor identity unavailable") from exc
            if before is None or start_ticks(pid) != before:
                raise ControllerError("INVALID_HOLDER", "ancestor process changed")
            if Path(os.fsdecode(argv0)).name == name or executable.name == name:
                holder = describe_holder(
                    pid, client_id=client_id, session_id=session_id, run_id=run_id
                )
                if holder.start_ticks != before:
                    raise ControllerError("INVALID_HOLDER", "ancestor process changed")
                return holder
            pid = parent
        raise ControllerError("INVALID_HOLDER", f"no ancestor process runs {name!r}")
    if not selector.isdecimal():
        raise ControllerError("INVALID_HOLDER", "holder must be a pid or ancestor:<program>")
    return describe_holder(int(selector), client_id=client_id, session_id=session_id, run_id=run_id)


def same_holder(left: WriterHolder, right: WriterHolder) -> bool:
    """Optional session/run identity cannot be omitted to inherit another entitlement."""
    return left == right


def describe_holder(
    pid: int,
    *,
    client_id: str,
    session_id: str | None = None,
    run_id: str | None = None,
) -> WriterHolder:
    if not isinstance(client_id, str) or not CLIENT_ID_PATTERN.fullmatch(client_id) or pid <= 0:
        raise ControllerError(
            "INVALID_HOLDER", "writer client_id must be a bounded opaque identifier"
        )
    ticks = start_ticks(pid)
    if ticks is None:
        raise ControllerError("INVALID_HOLDER", f"process {pid} is not running")
    return WriterHolder(
        client_id=client_id,
        pid=pid,
        boot_id=boot_id(),
        start_ticks=ticks,
        session_id=session_id,
        run_id=run_id,
    )


def holder_is_live(holder: WriterHolder) -> bool:
    """True only when the exact original process is still running.

    A different boot, a vanished pid, or a pid whose start time no longer matches
    all mean the original holder is gone - never that its task finished.
    """
    if not holder.boot_id or holder.boot_id == "unknown-boot":
        raise ControllerError("INVALID_HOLDER", "recorded boot identity is unknown")
    if holder.boot_id != boot_id():
        return False
    return start_ticks(holder.pid) == holder.start_ticks


def process_group_is_live(holder: WriterHolder) -> bool:
    """True when the holder's process group still has a member running.

    A wrapper can die while the real work continues in a surviving child, so the
    group is checked before a lease is treated as abandoned.
    """
    if holder.boot_id != boot_id():
        return False
    ticks = start_ticks(holder.pid)
    if ticks is not None and ticks != holder.start_ticks:
        # A reused pid's new group is not evidence about the original holder.
        return False
    try:
        pgid = os.getpgid(holder.pid)
    except (ProcessLookupError, PermissionError, OSError):
        pgid = holder.pid
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        candidate = int(entry)
        try:
            if os.getpgid(candidate) == pgid:
                return True
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return False


def own_ancestry() -> set[int]:
    """This process and its ancestors: the caller asking for an action is not a user of the checkout."""
    chain, pid = set(), os.getpid()
    while pid > 1 and pid not in chain:
        chain.add(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            break
        pid = int(stat.rsplit(")", 1)[1].split()[1])
    return chain


def paths_in_use(target: Path) -> list[tuple[int, str]]:
    """Live processes whose cwd, open files, or mappings sit inside ``target``.

    This is the evidence that covers a session holding no lease at all, so a
    legacy or externally started writer is never mistaken for an absent one.
    """
    return ProcessPathScan().in_use(target)


class ProcessPathScan:
    """One ``/proc`` reading shared by every target checked in a single pass.

    Reading each process's cwd, descriptors and mappings is the expensive part, so a
    pass that checks many checkouts reads them once, on first use, instead of once
    per checkout. Each answer is the same as a fresh ``paths_in_use`` over that
    reading; the reading is as old as the pass, like any other pass-time evidence.
    """

    def __init__(self) -> None:
        self._references: list[tuple[int, list[str]]] | None = None

    def in_use(self, target: Path) -> list[tuple[int, str]]:
        root = str(target)
        prefix = root + os.sep
        return [
            (pid, _command(pid))
            for pid, candidates in self._read_references()
            if any(candidate == root or candidate.startswith(prefix) for candidate in candidates)
        ]

    def _read_references(self) -> list[tuple[int, list[str]]]:
        if self._references is None:
            self._references = [
                (pid, _referenced_paths(pid))
                for pid in (int(entry) for entry in os.listdir("/proc") if entry.isdigit())
                if pid != os.getpid()
            ]
        return self._references

    def referenced_paths_in_use(self, target: Path) -> list[tuple[int, list[str]]]:
        """Reuse the same scan without reading process command-line arguments."""
        root = str(target)
        prefix = root + os.sep
        result = []
        for pid, candidates in self._read_references():
            matches = [path for path in candidates if path == root or path.startswith(prefix)]
            if matches:
                result.append((pid, matches))
        return result


def _referenced_paths(pid: int) -> list[str]:
    seen: list[str] = []
    with contextlib.suppress(OSError):
        seen.append(os.path.realpath(f"/proc/{pid}/cwd"))
    try:
        for handle in os.listdir(f"/proc/{pid}/fd"):
            try:
                seen.append(os.path.realpath(f"/proc/{pid}/fd/{handle}"))
            except OSError:
                continue
    except OSError:
        pass
    try:
        with open(f"/proc/{pid}/maps", encoding="utf-8") as stream:
            for line in stream:
                parts = line.rstrip("\n").split(" ", 5)
                if len(parts) == 6 and parts[5].startswith("/"):
                    seen.append(parts[5].strip())
    except OSError:
        pass
    return seen


def _command(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return "<unknown>"
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()[:120]


def new_lease(
    *,
    repository_id: str,
    allocation_id: str,
    worktree_path: str,
    owner: str,
    holder: WriterHolder,
    ttl_seconds: int | None,
) -> WriterLease:
    stamp = now()
    expires_at = None
    if ttl_seconds is not None:
        if ttl_seconds <= 0:
            raise ControllerError("INVALID_HOLDER", "lease ttl must be positive")
        moment = datetime.fromisoformat(stamp)
        expires_at = moment.fromtimestamp(
            moment.timestamp() + ttl_seconds, tz=moment.tzinfo
        ).isoformat(timespec="seconds")
    return WriterLease(
        schema_version=WRITER_LEASE_SCHEMA_VERSION,
        lease_id=str(uuid.uuid4()),
        repository_id=repository_id,
        allocation_id=allocation_id,
        worktree_path=worktree_path,
        owner=owner,
        holder=holder,
        acquired_at=stamp,
        renewed_at=stamp,
        expires_at=expires_at,
        record_version=1,
    )


def lease_is_expired(lease: WriterLease, *, at: str | None = None) -> bool:
    """Expiry means the writer claim needs verification - never that work is done."""
    if lease.expires_at is None:
        return False
    moment = datetime.fromisoformat(at or now())
    return datetime.fromisoformat(lease.expires_at) < moment


def acquire_writer_lease(
    repository_path: Path,
    *,
    target: Path,
    owner: str | None = None,
    client_id: str,
    holder_pid: int,
    session_id: str | None = None,
    run_id: str | None = None,
    ttl_seconds: int | None = None,
    holder: WriterHolder | None = None,
    expected_record_version: int | None = None,
    observe: bool = True,
) -> dict[str, Any]:
    """Claim write entitlement for one live process, without touching ownership."""

    from .controller import envelope
    from .controller import status as _status
    from .git_backend import inventory as _inventory
    from .policy import load_policy
    from .semantics import assert_record_version as _cas
    from .semantics import managed_record as _managed_record
    from .store import MetadataStore as _Store

    repository_initial = _inventory(repository_path)
    try:
        selected = _inventory(target)
    except OSError as exc:
        raise ControllerError("INVALID_TARGET", "writer path is unavailable") from exc
    if selected.common_dir != repository_initial.common_dir:
        raise ControllerError("UNREGISTERED_WORKTREE", "writer path belongs to another repository")
    target = selected.requested_path
    store = _Store(repository_initial.common_dir)
    if not store.lock_file.is_file() or store.lock_file.is_symlink():
        raise ControllerError(
            "INVALID_METADATA", "governed writer acquisition requires its repository lock"
        )
    with store.lock(exclusive=True):
        policy = load_policy(repository_initial.canonical_path)
        if policy.path.is_symlink():
            raise ControllerError("INVALID_POLICY", "policy marker is a symlink")
        snapshot = store._load_unlocked()
        if snapshot.repository_id is None:
            raise ControllerError("INVALID_METADATA", "governed repository identity is missing")
        item, record = _managed_record(_inventory(repository_path), snapshot, target)
        if expected_record_version is not None:
            _cas(record, expected_record_version)
        if item.detached or item.locked or item.prunable:
            raise ControllerError("INVALID_TARGET", "writer checkout requires review")
        # A wrapper at a process boundary cannot know the owner string; being able to
        # run in the governed checkout is the entitlement. A caller that does declare
        # an owner is still held to it, so a declared mismatch fails closed.
        if owner is not None and record.owner != owner:
            raise ControllerError(
                "OWNER_MISMATCH", "writer lease owner does not match the allocation owner"
            )
        observed = describe_holder(
            holder_pid, client_id=client_id, session_id=session_id, run_id=run_id
        )
        if holder is not None and not same_holder(holder, observed):
            raise ControllerError("WRITER_LEASE_MISMATCH", "holder changed during acquisition")
        holder = observed
        if ttl_seconds is not None and ttl_seconds <= 0:
            raise ControllerError("INVALID_HOLDER", "lease ttl must be positive")
        if writer_requirement(record.role) == "NOT_APPLICABLE":
            payload = envelope("writer-lease-acquire")
            payload["decision"] = {
                "admission": "NOT_APPLICABLE",
                "code": "DEDICATED_ROLE_GATE",
                "role": record.role,
                "allocation_id": record.allocation_id,
                "reason": "no ordinary writer entitlement; dedicated operation gates still apply",
            }
            return payload
        existing = snapshot.writer_leases.get(record.allocation_id)
        if foreign_runtime_bindings(record, holder, existing.lease_id if existing else None):
            raise ControllerError("EXTERNAL_BINDING", "runtime bindings prevent a new writer")
        if record.lifecycle != "ACTIVE":
            raise ControllerError(
                "INVALID_LIFECYCLE",
                f"lifecycle {record.lifecycle} cannot take a writer lease; resume it first",
            )
        if existing is not None and not same_holder(existing.holder, holder) and (
            holder_is_live(existing.holder) or process_group_is_live(existing.holder) or paths_in_use(item.path)
        ):
            raise ControllerError("WRITER_LEASE_HELD", "another live process already holds this allocation")
        if existing is not None:
            same = same_holder(existing.holder, holder)
            if (existing.repository_id, existing.worktree_path, existing.owner) != (
                snapshot.repository_id,
                str(item.path),
                record.owner,
            ):
                raise ControllerError("WRITER_LEASE_MISMATCH", "lease allocation identity drifted")
            if same:
                if lease_is_expired(existing) and ttl_seconds is None:
                    raise ControllerError(
                        "WRITER_LEASE_EXPIRED", "expired lease requires explicit renewal ttl"
                    )
                renewed = replace(
                    existing, renewed_at=now(), record_version=existing.record_version + 1
                )
                if ttl_seconds is not None:
                    renewed = replace(
                        renewed,
                        expires_at=_shift(renewed.renewed_at, ttl_seconds),
                    )
                store.write_writer_lease(renewed)
                lease = renewed
                code = "WRITER_LEASE_RENEWED"
            elif (
                holder_is_live(existing.holder)
                or process_group_is_live(existing.holder)
                or paths_in_use(item.path)
            ):
                raise ControllerError(
                    "WRITER_LEASE_HELD",
                    "another live process already holds the writer lease for this allocation",
                )
            else:
                lease = new_lease(
                    repository_id=snapshot.repository_id or "unknown",
                    allocation_id=record.allocation_id,
                    worktree_path=str(item.path),
                    owner=record.owner,
                    holder=holder,
                    ttl_seconds=ttl_seconds,
                )
                store.write_writer_lease(lease)
                code = "WRITER_LEASE_TAKEN_OVER"
        else:
            lease = new_lease(
                repository_id=snapshot.repository_id or "unknown",
                allocation_id=record.allocation_id,
                worktree_path=str(item.path),
                owner=record.owner,
                holder=holder,
                ttl_seconds=ttl_seconds,
            )
            store.write_writer_lease(lease)
            code = "WRITER_LEASE_ACQUIRED"
    payload = _status(repository_path) if observe else envelope("writer-lease-acquire")
    payload["command"] = "writer-lease-acquire"
    payload["decision"] = {
        "code": code,
        "admission": "GRANTED",
        "reason": "writer entitlement bound to a verifiable live process",
        "lease_id": lease.lease_id,
        "allocation_id": lease.allocation_id,
        "owner": lease.owner,
        "holder": asdict(lease.holder),
        "holder_resolver_version": HOLDER_RESOLVER_VERSION,
        "expires_at": lease.expires_at,
    }
    return payload


def _shift(stamp: str, seconds: int) -> str:
    moment = datetime.fromisoformat(stamp)
    return moment.fromtimestamp(moment.timestamp() + seconds, tz=moment.tzinfo).isoformat(
        timespec="seconds"
    )


def release_writer_lease(
    repository_path: Path,
    *,
    target: Path,
    holder_pid: int | None = None,
    lease_id: str | None = None,
    holder: WriterHolder | None = None,
) -> dict[str, Any]:
    """Give up write entitlement. This says nothing about whether the task finished."""

    from .controller import status as _status
    from .git_backend import inventory as _inventory
    from .semantics import managed_record as _managed_record
    from .store import MetadataStore as _Store

    if holder is None:
        raise ControllerError("WRITER_LEASE_MISMATCH", "complete holder identity is required")
    if holder_pid is not None and holder_pid != holder.pid:
        raise ControllerError("WRITER_LEASE_MISMATCH", "conflicting holder pid")
    repository_initial = _inventory(repository_path)
    store = _Store(repository_initial.common_dir)
    removed = False
    exited = False
    reason = "no writer lease was held"
    with store.lock(exclusive=True):
        snapshot = store._load_unlocked()
        item, record = _managed_record(_inventory(repository_path), snapshot, target)
        lease = snapshot.writer_leases.get(record.allocation_id)
        if lease is not None:
            if lease_id is not None and lease.lease_id != lease_id:
                raise ControllerError(
                    "WRITER_LEASE_MISMATCH", "lease id does not match the recorded lease"
                )
            if not same_holder(lease.holder, holder):
                raise ControllerError(
                    "WRITER_LEASE_MISMATCH", "exact live holder identity does not match the lease"
                )
            exited = not holder_is_live(holder)
            if exited and (
                lease_id is None
                or process_group_is_live(holder)
                or any(pid not in own_ancestry() for pid, _ in paths_in_use(item.path))
            ):
                # A host releases after its runtime reaped the session; the exact recorded
                # identity and lease ID prove it is that holder's own lease. Surviving
                # children, or any other process still inside the checkout, keep it.
                raise ControllerError(
                    "WRITER_LEASE_MISMATCH",
                    "an exited holder's lease needs its exact lease ID and no surviving children",
                )
            removed = store.remove_writer_lease(record.allocation_id)
            reason = (
                "exited holder's writer entitlement released; allocation and work untouched"
                if exited
                else "writer entitlement released; allocation and work untouched"
            )

    payload = _status(repository_path)
    payload["command"] = "writer-lease-release"
    payload["decision"] = {
        "code": "WRITER_LEASE_RELEASED" if removed else "NO_WRITER_LEASE",
        "reason": reason,
        "allocation_id": record.allocation_id,
        "holder_exited": exited,
    }
    return payload


def check_writer_entitlement(
    repository_id: str | None,
    record: MetadataRecord,
    target: Path,
    lease: WriterLease | None,
    holder: WriterHolder | None,
) -> None:
    """Check the current entitlement from the same snapshot as write authority."""
    if lease is None or holder is None:
        raise ControllerError("WRITER_LEASE_REQUIRED", "an exact current writer lease is required")
    if foreign_runtime_bindings(record, holder, lease.lease_id):
        raise ControllerError("EXTERNAL_BINDING", "runtime bindings fence writer entitlement")
    if (
        lease.repository_id != repository_id
        or lease.allocation_id != record.allocation_id
        or lease.worktree_path != str(target)
        or lease.owner != record.owner
        or not same_holder(lease.holder, holder)
        or not holder_is_live(holder)
        or lease_is_expired(lease)
    ):
        raise ControllerError(
            "WRITER_LEASE_MISMATCH", "writer entitlement is stale or belongs to another holder"
        )
