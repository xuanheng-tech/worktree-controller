"""Host resource reservations and durable checkout requests.

The filesystem and kernel report occupancy; this ledger accounts only for growth
not yet materialized. Repository locks still own allocations and Git mutations.
Lock order is repository -> host ledger. No host lock is held while calling a
repository mutation, a process, or a cleanup coordinator.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import time
from contextlib import contextmanager, nullcontext, suppress
from pathlib import Path
from typing import Any
from uuid import UUID

from .git_backend import _git, inventory, public_refs, resolve_commit, resolve_repository
from .models import (
    ControllerError,
    MetadataSnapshot,
    RepositoryInventory,
    WorktreePolicy,
    WriterHolder,
)
from .semantics import integration_intent_abandoned
from .store import MetadataStore, decode_cache

CONTRACT = "host-resource-admission/v1"
GIB = 1024**3
# Safety headroom is a resource budget, never a checkout/concurrency count.
MIN_FREE_BYTES = 16 * GIB
MIN_FREE_INODES = 100_000
MIN_AVAILABLE_MEMORY = 4 * GIB
DEFAULT_EXECUTION_MEMORY = 2 * GIB
CREATION_BURST = 16
CREATION_INTERVAL_NS = 1_000_000_000
PRESSURE_HIGH = {"cpu": 60.0, "memory": 10.0, "io": 20.0}
PRESSURE_LOW = {"cpu": 30.0, "memory": 2.0, "io": 5.0}
STATES = {"WAITING", "RESERVED", "ALLOCATED", "RELEASED", "ATTENTION_REQUIRED", "CANCELLED"}


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def canonical_holder_digest(holder: dict[str, Any]) -> str:
    """SHA-256 over the canonical holder bytes: sorted-key, compact JSON, UTF-8.

    The holder document is exactly the six WriterHolder fields as persisted in a
    reservation row (client_id, pid, boot_id, start_ticks, session_id, run_id).
    ``host-execution --action inspect`` publishes both the canonical bytes and
    this digest so recovery callers never have to read the ledger layout.
    """
    return hashlib.sha256(
        json.dumps(holder, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def unbounded_host_scope(path: Path) -> bool:
    """True for the home root itself or any ancestor still containing it.

    Such a target cannot bound a host reservation to one session, so the clear
    side already refused identity-only recovery there; the admit side must not
    mint those reservations either. An unresolvable home is treated as
    unbounded (fail closed).
    """
    home = Path.home()
    if path == home or path in home.parents:
        return True
    try:
        resolved_home = home.resolve(strict=True)
    except OSError:
        return True
    return path == resolved_home or path in resolved_home.parents


def filesystem(path: Path) -> dict[str, int | str]:
    parent = path
    while not parent.exists():
        if parent == parent.parent:
            raise ControllerError("RESOURCE_OBSERVATION_UNAVAILABLE", "target filesystem is unavailable")
        parent = parent.parent
    parent = parent.resolve(strict=True)
    info, capacity = parent.stat(), os.statvfs(parent)
    return {"device": info.st_dev, "observed_path": str(parent),
            "available_bytes": capacity.f_bavail * capacity.f_frsize,
            "available_inodes": capacity.f_favail}


def pressure() -> dict[str, Any]:
    """Only public aggregate kernel counters; never process environment/state."""
    try:
        memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        available = int(memory["MemAvailable"].split()[0]) * 1024
        observed = {}
        for kind in PRESSURE_HIGH:
            lines = Path(f"/proc/pressure/{kind}").read_text().splitlines()
            some = next(line for line in lines if line.startswith("some "))
            values = dict(item.split("=", 1) for item in some.split()[1:])
            observed[kind] = float(values["avg10"])
        if available < 0 or any(not 0 <= value <= 100 for value in observed.values()):
            raise ValueError("invalid resource counters")
        return {"available_memory_bytes": available, "psi_avg10": observed,
                "observed_at_ns": time.time_ns()}
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        raise ControllerError("RESOURCE_OBSERVATION_UNAVAILABLE", "kernel resource counters unavailable") from exc


def checkout_estimate(repository: Path, base: str, *, growth_bytes: int, growth_inodes: int) -> dict[str, int]:
    """Estimate checkout + environment/artifact growth + an independent preservation.

    Git object storage/shared caches already occupy the target filesystem and are
    not billed as additional copies. gitlinks are not recursively initialized.
    """
    total = 0
    directories: set[bytes] = set()
    files = 0
    for row in _git(repository, "ls-tree", "-r", "-l", "-z", base).stdout.split(b"\0"):
        if not row:
            continue
        identity, path = row.split(b"\t", 1)
        fields = identity.split()
        if len(fields) != 4:
            raise ControllerError("RESOURCE_OBSERVATION_UNAVAILABLE", "invalid Git tree estimate")
        if fields[1] == b"blob":
            total += int(fields[3])
        files += 1
        parts = path.split(b"/")
        directories.update(b"/".join(parts[:index]) for index in range(1, len(parts)))
    return {"checkout_bytes": total, "checkout_inodes": files + len(directories) + 2,
            "growth_bytes": growth_bytes, "growth_inodes": growth_inodes,
            "preservation_bytes": total + growth_bytes,
            "preservation_inodes": files + len(directories) + growth_inodes + 2}


def allocation_request(repository: RepositoryInventory, repository_id: str, policy: WorktreePolicy,
                       *, target: Path, base_oid: str, arguments: dict[str, Any]) -> dict[str, Any]:
    arguments = {"scope_paths": (), "source_allocations": (), "source_handoffs": (),
                 "external_bindings": (), "integration_intent_id": None,
                 "lineage_base_commit": None, **arguments}
    return {"repository_id": repository_id, "repository_path": str(repository.canonical_path),
            "common_dir": str(repository.common_dir), "target_path": str(target),
            "filesystem_device": filesystem(target)["device"], "base_oid": base_oid,
            "arguments": {key: str(value) if isinstance(value, Path) else list(value) if isinstance(value, tuple) else value
                          for key, value in arguments.items()},
            "estimate": checkout_estimate(repository.canonical_path, base_oid,
                                          growth_bytes=policy.checkout_growth_bytes,
                                          growth_inodes=policy.checkout_growth_inodes)}


def prepare_request(repository_path: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    from .policy import load_policy

    repository = inventory(repository_path)
    snapshot = MetadataStore(repository.common_dir).load()
    policy = load_policy(repository.canonical_path)
    if not policy.resource_admission or snapshot.repository_id is None:
        raise ControllerError("RESOURCE_ADMISSION_UNSUPPORTED", "repository has not adopted resource admission")
    base = resolve_commit(repository.canonical_path, arguments["base_ref"])
    target = Path(arguments.get("target") or Path.home() / "worktrees" / repository.canonical_path.name / arguments["task_key"]).expanduser().absolute()
    return allocation_request(repository, snapshot.repository_id, policy, target=target, base_oid=base,
                              arguments={**arguments, "base_ref": base, "target": str(target)})


def materialized(path: Path, device: int) -> tuple[int, int]:
    """Credit actual private allocated blocks only; no-follow and dedup by inode.

    Shared/hardlinked files need no new storage. Their bytes were never additional
    occupancy and cannot be used to claim that a future private growth budget has
    already been spent. This inspection neither opens nor interprets file content.
    """
    total = 0
    entries = 0
    seen: set[tuple[int, int]] = set()
    if not path.exists():
        return 0, 0
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(path, flags)

    def visit(directory: int) -> None:
        nonlocal total, entries
        for name in os.listdir(directory):
            before = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if before.st_dev != device:
                raise ControllerError("RESOURCE_OBSERVATION_UNAVAILABLE", "allocation crosses filesystem")
            key = before.st_dev, before.st_ino
            if key not in seen:
                seen.add(key)
                if stat.S_ISDIR(before.st_mode) or before.st_nlink == 1:
                    total += before.st_blocks * 512
                    entries += 1
            if stat.S_ISDIR(before.st_mode):
                child = os.open(name, flags, dir_fd=directory)
                try:
                    current = os.fstat(child)
                    if (current.st_dev, current.st_ino) != key:
                        raise ControllerError("RESOURCE_OBSERVATION_UNAVAILABLE", "allocation tree changed")
                    visit(child)
                finally:
                    os.close(child)
            after = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (before.st_dev, before.st_ino, before.st_mode) != (after.st_dev, after.st_ino, after.st_mode):
                raise ControllerError("RESOURCE_OBSERVATION_UNAVAILABLE", "allocation tree changed")

    try:
        if os.fstat(descriptor).st_dev != device:
            raise ControllerError("RESOURCE_OBSERVATION_UNAVAILABLE", "allocation filesystem changed")
        visit(descriptor)
        return total, entries
    finally:
        os.close(descriptor)


def execution_memory(row: dict[str, Any]) -> int:
    """Credit observed proportional memory, never shared pages twice.

    Child memory is already in MemAvailable. Crediting only the exact holder's
    PSS deliberately leaves conservative headroom for children/future growth.
    Unobservable or changed process identity receives no reservation credit.
    """
    from .writer_lease import holder_is_live

    identity = row.get("holder")
    if not isinstance(identity, dict):
        return 0
    try:
        holder = WriterHolder(**identity)
        if not holder_is_live(holder):
            return 0
        lines = Path(f"/proc/{holder.pid}/smaps_rollup").read_text().splitlines()
        pss = next(int(line.split()[1]) * 1024 for line in lines if line.startswith("Pss:"))
        return max(0, pss) if holder_is_live(holder) else 0
    except (OSError, ValueError, TypeError, StopIteration, ControllerError):
        return 0


def execution_request(repository: RepositoryInventory | None, repository_id: str, allocation_id: str | None,
                      target: Path, *, client_id: str, session_id: str | None, run_id: str | None,
                      memory_bytes: int = DEFAULT_EXECUTION_MEMORY, process_key: str | None = None) -> dict[str, Any]:
    from .writer_lease import CLIENT_ID_PATTERN

    identity = session_id or process_key
    if (not CLIENT_ID_PATTERN.fullmatch(client_id) or not identity or len(identity) > 256
            or "\x00" in identity or (run_id is not None and (not run_id or len(run_id) > 256 or "\x00" in run_id))
            or type(memory_bytes) is not int or memory_bytes < 1):
        raise ControllerError("INVALID_REQUEST", "execution resource request identity or budget is invalid")
    operation = f"execution:{client_id}:{identity}"
    return {"kind": "execution", "repository_id": repository_id,
            "repository_path": str(repository.canonical_path) if repository else str(target),
            "common_dir": str(repository.common_dir) if repository else "",
            "target_path": str(target), "allocation_id": allocation_id, "memory_bytes": memory_bytes,
            "client_id": client_id, "session_id": session_id, "run_id": run_id,
            **({"process_key": process_key} if session_id is None else {}),
            "arguments": {"task_key": operation, "authority_key": operation}}


def admit_execution(repository_path: Path, *, target: Path, owner: str, client_id: str,
                    session_id: str, run_id: str | None = None,
                    memory_bytes: int = DEFAULT_EXECUTION_MEMORY, cancel: bool = False,
                    _cache_recovery: bool = False) -> dict[str, Any]:
    """Reserve (or cancel) one execution's budget.

    Exit recovery of finished executions is paid only when it can change the answer:
    an admission that is immediately available proceeds without it, while a wait or a
    still-recorded earlier execution of the same task first reconciles finished
    executions and then asks once more with the same durable request.
    """
    arguments = {"target": target, "owner": owner, "client_id": client_id, "session_id": session_id,
                 "run_id": run_id, "memory_bytes": memory_bytes}
    if cancel:
        return _admit_execution_once(repository_path, cancel=True, **arguments)
    try:
        admitted = _admit_execution_once(repository_path, cancel=False, **arguments)
    except ControllerError as exc:
        if exc.code != "ADMISSION_REQUEST_DRIFT":
            raise
    else:
        if admitted["decision"].get("code") != "RESOURCE_WAIT":
            return admitted
    # CLI recovery can reload several snapshots; the ordinary first attempt stays uncached.
    # Direct library callers keep their existing cache behavior unless already in a cache scope.
    with decode_cache() if _cache_recovery else nullcontext():
        reconcile_finished_executions()
        return _admit_execution_once(repository_path, cancel=False, **arguments)


def _admit_execution_once(repository_path: Path, *, target: Path, owner: str, client_id: str,
                          session_id: str, run_id: str | None,
                          memory_bytes: int, cancel: bool) -> dict[str, Any]:
    from .controller import envelope
    from .policy import load_policy
    from .semantics import managed_record as _managed_record

    repository = inventory(repository_path)
    store = MetadataStore(repository.common_dir)
    with store.lock(exclusive=True):
        if not load_policy(repository.canonical_path).resource_admission:
            raise ControllerError("RESOURCE_ADMISSION_UNSUPPORTED", "repository resource policy is not enabled")
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        if record.owner != owner or record.lifecycle != "ACTIVE" or not snapshot.repository_id:
            raise ControllerError("EXECUTION_AUTHORITY_CHANGED", "allocation is not active under this owner")
        request = execution_request(repository, snapshot.repository_id, record.allocation_id, item.path,
                                    client_id=client_id, session_id=session_id, run_id=run_id,
                                    memory_bytes=memory_bytes)
        request["owner"] = owner
        from .writer_lease import holder_is_live, process_group_is_live

        lease = snapshot.writer_leases.get(record.allocation_id)
        if cancel:
            if lease is not None and (lease.holder.client_id, lease.holder.session_id, lease.holder.run_id) == (client_id, session_id, run_id):
                raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "an acquired writer requires exit reconciliation")
            decision = AdmissionLedger().cancel_execution(request)
        else:
            ledger = AdmissionLedger()
            reserved = any(key != fingerprint(request) and row["state"] in {"RESERVED", "ALLOCATED", "ATTENTION_REQUIRED"}
                and row["request"].get("kind") == "execution"
                and (row["request"]["repository_id"], row["request"].get("allocation_id"))
                    == (snapshot.repository_id, record.allocation_id)
                for key, row in ledger.read()["requests"].items())
            # The repository lock serializes this allocation's reservation and
            # writer grant. Two launch barriers must not both become admissible.
            occupied = bool(record.external_bindings) or (lease is not None and
                (holder_is_live(lease.holder) or process_group_is_live(lease.holder))) or reserved
            decision = ledger.reserve([request], blockers=("ALLOCATION_OCCUPIED",) if occupied else ())
    result = envelope("execution-cancel" if cancel else "execution-admit")
    result["decision"] = {**decision, "allocation_id": record.allocation_id,
                          "session_id": session_id, "run_id": run_id}
    return result


def bind_execution(repository_id: str, allocation_id: str, holder: WriterHolder, lease_id: str,
                   *, required: bool = False) -> None:
    """The repository mutation lock and exact writer check precede this call."""
    from dataclasses import asdict

    ledger = AdmissionLedger()
    if not ledger.path.exists():
        if required:
            raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "execution reservation is missing")
        return
    with ledger.locked():
        state = ledger.read()
        matched = False
        for row in state["requests"].values():
            request = row["request"]
            if (request.get("kind") != "execution" or request["repository_id"] != repository_id
                    or request["allocation_id"] != allocation_id
                    or (request["client_id"], request["session_id"], request["run_id"])
                    != (holder.client_id, holder.session_id, holder.run_id)):
                continue
            if request.get("process_key") not in {None, f"{holder.boot_id}:{holder.pid}:{holder.start_ticks}"}:
                continue
            matched = True
            if row["state"] not in {"RESERVED", "ALLOCATED"} or row.get("lease_id") not in {None, lease_id}:
                raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "execution reservation cannot bind another attempt")
            if row["state"] == "ALLOCATED" and row.get("holder") == asdict(holder):
                continue
            row.update(state="ALLOCATED", holder=asdict(holder), lease_id=lease_id,
                       allocation_id=allocation_id, updated_at_ns=time.time_ns())
            row["history"].append({"state": "ALLOCATED", "at_ns": row["updated_at_ns"]})
        if required and not matched:
            raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "execution reservation does not match this holder")
        if not matched:
            return
        ledger._save(state)


def release_execution(repository_id: str, allocation_id: str, lease_id: str) -> None:
    """Only a runtime-clear proof authorizes releasing the memory reservation."""
    ledger = AdmissionLedger()
    if not ledger.path.exists():
        return
    with ledger.locked():
        state = ledger.read()
        for row in state["requests"].values():
            request = row["request"]
            if (request.get("kind") == "execution" and request["repository_id"] == repository_id
                    and request["allocation_id"] == allocation_id and row.get("lease_id") == lease_id
                    and row["state"] == "ALLOCATED"):
                row.update(state="RELEASED", updated_at_ns=time.time_ns())
                row["history"].append({"state": "RELEASED", "at_ns": row["updated_at_ns"]})
        ledger._save(state)


class AdmissionLedger:
    def __init__(self, root: Path | None = None):
        state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        if not state_home.is_absolute():
            raise ControllerError("INVALID_RESOURCE_LEDGER", "XDG state directory must be absolute")
        self.root = root or state_home / "worktree-controller/resource-admission/v1"
        self.path = self.root / "state.json"
        # Reuse the Controller's fsync/atomic-replace metadata backend. This is
        # one host ledger in the existing state root, not a parallel database.
        self.backend = MetadataStore(self.root)

    @contextmanager
    def locked(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.root / "ledger.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"contract": CONTRACT, "requests": {}, "pressure_high": False}
        value = dict(self.backend._read_json(self.path))
        if value.get("contract") != CONTRACT or not isinstance(value.get("requests"), dict) or type(value.get("pressure_high")) is not bool:
            raise ControllerError("INVALID_RESOURCE_LEDGER", "unsupported resource ledger")
        for key, row in value["requests"].items():
            if not isinstance(row, dict) or row.get("state") not in STATES or fingerprint(row.get("request")) != key:
                raise ControllerError("INVALID_RESOURCE_LEDGER", "resource request identity changed")
        return value

    def _save(self, state: dict[str, Any]) -> None:
        self.backend._atomic_json(self.path, state, compact=True)

    def cancel_execution(self, request: dict[str, Any]) -> dict[str, Any]:
        with self.locked():
            state = self.read()
            cancelled = []
            for key, row in state["requests"].items():
                previous = row["request"]
                if (previous.get("kind") != "execution" or any(previous.get(name) != request.get(name)
                    for name in ("repository_id", "allocation_id", "owner", "client_id", "session_id", "run_id", "process_key"))):
                    continue
                if row.get("holder") is not None or row["state"] not in {"WAITING", "RESERVED", "CANCELLED"}:
                    raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "an attempted execution cannot be cancelled as no-action")
                cancelled.append(key)
                if row["state"] != "CANCELLED":
                    row.update(state="CANCELLED", updated_at_ns=time.time_ns())
                    row["history"].append({"state": "CANCELLED", "at_ns": row["updated_at_ns"]})
            self._save(state)
            return {"code": "EXECUTION_ADMISSION_CANCELLED", "request_ids": cancelled,
                    "process_started": False, "task_completed": False}

    @staticmethod
    def _outstanding(row: dict[str, Any]) -> tuple[int, int]:
        if row["request"].get("kind") == "execution":
            return 0, 0
        estimate = row["request"]["estimate"]
        # Preservation remains independently reserved until allocation release.
        allocated = row.get("materialized_bytes", 0)
        inodes = row.get("materialized_inodes", 0)
        return (max(0, estimate["checkout_bytes"] + estimate["growth_bytes"] - allocated) + estimate["preservation_bytes"],
                max(0, estimate["checkout_inodes"] + estimate["growth_inodes"] - inodes) + estimate["preservation_inodes"])

    def _decision(self, state: dict[str, Any], requests: list[dict[str, Any]]) -> dict[str, Any]:
        observed = pressure()
        values = observed["psi_avg10"]
        pressured = (any(values[key] >= PRESSURE_HIGH[key] for key in values)
                     or (state["pressure_high"] and not all(values[key] <= PRESSURE_LOW[key] for key in values)))
        reasons = []
        executing = any(request.get("kind") == "execution" for request in requests)
        if executing and pressured:
            reasons.append("HOST_PRESSURE")
        filesystems = {}
        for request in requests:
            if request.get("kind") == "execution":
                continue
            sample = filesystem(Path(request["target_path"]))
            if sample["device"] != request["filesystem_device"]:
                raise ControllerError("RESOURCE_IDENTITY_CHANGED", "target filesystem changed")
            filesystems[sample["device"]] = sample
        candidates = {fingerprint(request): {"request": request} for request in requests}
        held = {key: row for key, row in state["requests"].items()
                if row["state"] in {"RESERVED", "ALLOCATED", "ATTENTION_REQUIRED"}}
        held.update({key: row for key, row in candidates.items() if key not in held})
        outstanding_memory = sum(
            max(0, row["request"]["memory_bytes"] - execution_memory(row))
            for row in held.values() if row["request"].get("kind") == "execution"
        )
        if executing and observed["available_memory_bytes"] - outstanding_memory < MIN_AVAILABLE_MEMORY:
            reasons.append("HOST_MEMORY_HEADROOM")
        rate = state.get("creation_rate", {"tokens": float(CREATION_BURST), "at_ns": time.time_ns()})
        tokens = min(CREATION_BURST, rate["tokens"] + max(0, time.time_ns() - rate["at_ns"]) / CREATION_INTERVAL_NS)
        new_creations = sum(request.get("kind") != "execution"
                            and state["requests"].get(fingerprint(request), {}).get("state") not in {"RESERVED", "ALLOCATED"}
                            for request in requests)
        if min(new_creations, CREATION_BURST) > tokens:
            reasons.append("CREATION_RATE")
        for device, sample in filesystems.items():
            pending = [self._outstanding(row) for row in held.values()
                       if row["request"].get("filesystem_device") == device]
            reserved_bytes, reserved_inodes = map(sum, zip(*pending, strict=True)) if pending else (0, 0)
            sample.update(outstanding_bytes=reserved_bytes, outstanding_inodes=reserved_inodes)
            if sample["available_bytes"] - reserved_bytes < MIN_FREE_BYTES:
                reasons.append("FILESYSTEM_BYTES")
            if sample["available_inodes"] - reserved_inodes < MIN_FREE_INODES:
                reasons.append("FILESYSTEM_INODES")
        return {"code": "RESOURCE_WAIT" if reasons else "RESOURCE_AVAILABLE", "blockers": sorted(set(reasons)),
                "host": observed, "filesystems": list(filesystems.values()), "pressure_high": pressured,
                "minimum_free_bytes": MIN_FREE_BYTES, "minimum_free_inodes": MIN_FREE_INODES,
                "minimum_available_memory_bytes": MIN_AVAILABLE_MEMORY,
                "outstanding_memory_bytes": outstanding_memory,
                "creation_rate": {"tokens": tokens, "requested": new_creations,
                                  "burst": CREATION_BURST, "per_second": 1}}

    def plan(self, requests: list[dict[str, Any]]) -> dict[str, Any]:
        # Read-only advisory snapshot. Only reserve() grants an atomic budget.
        return self._decision(self.read(), requests)

    def reserve(self, requests: list[dict[str, Any]], *, blockers: tuple[str, ...] = (),
                integration_sources: tuple[str, ...] = ()) -> dict[str, Any]:
        keys = [fingerprint(request) for request in requests]
        if len(set(keys)) != len(keys) or not keys:
            raise ControllerError("INVALID_REQUEST", "resource requests must be nonempty and unique")
        with self.locked():
            state = self.read()
            if any(request.get("kind") == "execution" and
                   state["requests"].get(fingerprint(request), {}).get("state") == "ALLOCATED" for request in requests):
                raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "execution already bound; query the original Run instead of launching")
            decision = self._decision(state, requests)
            if blockers:
                decision.update(code="RESOURCE_WAIT", blockers=sorted(set(decision["blockers"]) | set(blockers)))
            state["pressure_high"] = decision["pressure_high"]
            now = time.time_ns()
            for request in requests:
                key = fingerprint(request)
                for previous_key, previous in state["requests"].items():
                    old = previous["request"]
                    if (previous_key != key and previous["state"] not in {"RELEASED", "CANCELLED"}
                        and old["repository_id"] == request["repository_id"]
                        and old.get("kind", "checkout") == request.get("kind", "checkout")
                        and (old["arguments"]["task_key"] == request["arguments"]["task_key"]
                             or old["arguments"]["authority_key"] == request["arguments"]["authority_key"])):
                        # Only the repository producer may supply this proof, after
                        # checking its frozen intent under the repository lock.
                        source_lineage = (
                            old["arguments"]["task_key"] != request["arguments"]["task_key"]
                            and request.get("kind", "checkout") == "checkout"
                            and request["arguments"].get("role") == "integration"
                            and request["arguments"].get("integration_intent_id")
                            and tuple(request["arguments"].get("source_allocations", ())) == integration_sources
                            and previous["state"] == "ALLOCATED"
                            and previous.get("allocation_id") in integration_sources
                            and old["arguments"].get("role") in {"feature", "audit", "hotfix", "emergency"}
                        )
                        if not source_lineage:
                            raise ControllerError(
                                "ADMISSION_REQUEST_DRIFT",
                                f"existing durable request {previous_key} has different identity; "
                                "inspect admission-queue and preserve its task, authority and intent. "
                                "For a WAITING request after main advances, replay its recorded "
                                "arguments through admission-queue --repo <repository> --apply, "
                                "then integration-refresh and revalidate before publication",
                            )
                existing = state["requests"].get(key)
                if existing and existing["state"] in {"CANCELLED", "RELEASED", "ATTENTION_REQUIRED"}:
                    raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "request requires formal reconciliation")
                if existing and existing["state"] in {"RESERVED", "ALLOCATED"}:
                    continue
                row = existing or {"request": request, "created_at_ns": now, "history": []}
                if len(requests) > 1:
                    row["group"] = sorted(keys)
                next_state = "WAITING" if decision["blockers"] else "RESERVED"
                if row.get("state") != next_state:
                    row["history"].append({"state": next_state, "at_ns": now})
                row.update(state=next_state, updated_at_ns=now,
                           decision=decision)
                state["requests"][key] = row
            rate = decision["creation_rate"]
            state["creation_rate"] = {"tokens": rate["tokens"] - (rate["requested"] if not decision["blockers"] else 0),
                                      "at_ns": now}
            self._save(state)
            return {**decision, "request_ids": [fingerprint(request) for request in requests]}

    def bind(self, request: dict[str, Any], *, transaction_id: str, allocation_id: str) -> None:
        with self.locked():
            state = self.read()
            row = state["requests"].get(fingerprint(request))
            if row is None or row["state"] != "RESERVED" or row.get("transaction_id") not in {None, transaction_id}:
                raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "reservation already belongs to another attempt")
            row.update(transaction_id=transaction_id, allocation_id=allocation_id)
            self._save(state)

    def allocated(self, request: dict[str, Any], *, allocation_id: str) -> None:
        warning = None
        try:
            allocated, inodes = materialized(Path(request["target_path"]), request["filesystem_device"])
        except (OSError, ControllerError):
            # Credit is optional; uncertainty retains the entire reservation.
            # It cannot turn a completed Git allocation into a failed operation.
            allocated, inodes = 0, 0
            warning = "MATERIALIZED_CREDIT_UNAVAILABLE"
        with self.locked():
            state = self.read()
            row = state["requests"][fingerprint(request)]
            if row.get("allocation_id") != allocation_id or row["state"] not in {"RESERVED", "ALLOCATED"}:
                raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "allocation differs from resource reservation")
            previous_state = row["state"]
            row.update(state="ALLOCATED", materialized_bytes=allocated, materialized_inodes=inodes,
                       updated_at_ns=time.time_ns(), observation_warning=warning)
            if previous_state != "ALLOCATED":
                row["history"].append({"state": "ALLOCATED", "at_ns": row["updated_at_ns"]})
            self._save(state)

    def reset_no_effect_acquire(self, store: MetadataStore, key: str, *, apply: bool) -> bool:
        """Make a proved no-effect attempt retryable, archiving its journal in history.

        The caller holds the repository flock. Neither an error code nor a missing
        allocation alone authorizes dropping the attempted reservation identity.
        """
        from .transaction_scope import acquire_evidence_terminal

        if not store.flock_held:
            raise ControllerError("MISSING_REPOSITORY_LOCK", "acquire recovery requires its flock")
        row = self.read()["requests"].get(key)
        if row is None or row["state"] != "RESERVED" or not row.get("transaction_id"):
            return False
        transaction = row["transaction_id"]
        try:
            if str(UUID(transaction)) != transaction:
                raise ValueError("noncanonical transaction")
            history = row["history"]
            if not isinstance(history, list) or any(
                not isinstance(event, dict) for event in history
            ):
                raise ValueError("invalid request history")
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            raise ControllerError(
                "ADMISSION_RECOVERY_REQUIRED", "acquire attempt identity is invalid"
            ) from exc
        proof = next(
            (
                event
                for event in reversed(history)
                if event.get("terminal_evidence") == "ACQUIRE_NO_EFFECT_SUPERSEDED"
                and event.get("transaction_id") == transaction
                and event.get("allocation_id") == row.get("allocation_id")
            ),
            None,
        )
        journal_path = store.transactions_dir / f"{transaction}.json"
        if proof is None:
            if acquire_evidence_terminal(store, transaction) != "ACQUIRE_NO_EFFECT_SUPERSEDED":
                return False
            journal = store._read_json(journal_path)
        else:
            journal = proof.get("journal")
            if not isinstance(journal, dict):
                raise ControllerError(
                    "ADMISSION_RECOVERY_REQUIRED", "no-effect proof is incomplete"
                )
            if journal_path.exists() and store._read_json(journal_path) != journal:
                raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "no-effect journal changed")
        request = row["request"]
        if (
            request.get("kind", "checkout") != "checkout"
            or request["common_dir"] != str(store.root.parents[1])
            or request["repository_id"] != store._read_json(store.repo_file).get("repository_id")
            or journal.get("transaction_id") != transaction
            or journal.get("command") != "acquire"
            or journal.get("phase") not in {"PREPARED", "WORKTREE_ADDED"}
            or tuple(
                journal.get(field)
                for field in (
                    "repository_id",
                    "allocation_id",
                    "target_path",
                    "branch",
                    "expected_head",
                )
            )
            != (
                request["repository_id"],
                row.get("allocation_id"),
                request["target_path"],
                request["arguments"].get("branch"),
                request["base_oid"],
            )
        ):
            raise ControllerError(
                "ADMISSION_RECOVERY_REQUIRED", "no-effect journal identity differs"
            )
        if not apply:
            return True
        with self.locked():
            state = self.read()
            if state["requests"].get(key) != row:
                raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "acquire reservation changed")
            if proof is None:
                # Durable proof precedes journal removal. Never hold the host
                # ledger lock while invoking a repository mutation or Git.
                row["history"].append(
                    {
                        "state": "RESERVED",
                        "at_ns": time.time_ns(),
                        "terminal_evidence": "ACQUIRE_NO_EFFECT_SUPERSEDED",
                        "transaction_id": transaction,
                        "allocation_id": row["allocation_id"],
                        "journal": journal,
                    }
                )
                state["requests"][key] = row
                self._save(state)
        if journal_path.exists():
            store.remove_transaction(transaction)
        with self.locked():
            state = self.read()
            if state["requests"].get(key) != row:
                raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "acquire reservation changed")
            now = time.time_ns()
            row["history"].append(
                {
                    "state": "WAITING",
                    "at_ns": now,
                    "transaction_id": transaction,
                    "allocation_id": row["allocation_id"],
                }
            )
            row.update(state="WAITING", updated_at_ns=now)
            row.pop("transaction_id")
            row.pop("allocation_id")
            state["requests"][key] = row
            self._save(state)
        return True

    def vanished(self, key: str, evidence: dict[str, Any]) -> None:
        """Settle a checkout budget whose repository and checkout no longer exist anywhere."""
        with self.locked():
            state = self.read()
            row = state["requests"][key]
            if row["state"] == "RELEASED":
                return
            if row["request"].get("kind") == "execution" or row["state"] not in {"WAITING", "RESERVED", "ALLOCATED"}:
                raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "only a checkout budget settles as vanished")
            row.update(state="RELEASED", updated_at_ns=time.time_ns(), vanished_evidence=evidence)
            row["history"].append({"state": "RELEASED", "at_ns": row["updated_at_ns"],
                                   "terminal_evidence": evidence["contract"]})
            self._save(state)

    def released(self, key: str, allocation_id: str) -> None:
        """Called only after formal release evidence and Git absence verification."""
        with self.locked():
            state = self.read()
            row = state["requests"][key]
            if row.get("allocation_id") != allocation_id:
                raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "released allocation identity differs")
            if row["state"] == "RELEASED":
                return
            row.update(state="RELEASED", updated_at_ns=time.time_ns())
            row["history"].append({"state": "RELEASED", "at_ns": row["updated_at_ns"]})
            self._save(state)

    def _cancel_abandoned_claim(self, key: str, expected: dict[str, Any], evidence: dict[str, Any]) -> None:
        """The caller proves abandonment and no attempt under the repository flock."""
        with self.locked():
            state = self.read()
            row = state["requests"].get(key)
            if row != expected:
                raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "claim reservation changed")
            row.update(state="CANCELLED", updated_at_ns=time.time_ns(), abandoned_intent_evidence=evidence)
            row["history"].append({"state": "CANCELLED", "at_ns": row["updated_at_ns"],
                                   "terminal_evidence": "PREPARATION_ABANDONED"})
            self._save(state)


def release_allocation_budget(repository_id: str, allocation_id: str, receipt_id: str) -> None:
    """Settle reservations at the verified formal allocation release boundary.

    The caller holds the repository lock and has verified Git/path absence and
    persisted the release receipt. Periodic reconciliation covers a crash before
    this settlement. Release cannot pass an active runtime binding; historical
    writer-only budget entries can outlive their already released allocation.
    An execution admitted for this allocation but never bound to a holder can no
    longer start in it, so its reservation settles here too.
    """
    ledger = AdmissionLedger()
    if not ledger.path.exists():
        return
    with ledger.locked():
        state = ledger.read()
        changed = False
        for row in state["requests"].values():
            request = row["request"]
            unbound = _unbound_execution(row) and request.get("allocation_id") == allocation_id
            if (request["repository_id"] != repository_id
                or (row.get("allocation_id") != allocation_id and not unbound)
                or row["state"] in {"RELEASED", "CANCELLED"}
                or (request.get("kind") == "execution" and row["state"] != "ALLOCATED" and not unbound)):
                continue
            now = time.time_ns()
            row.update(state="RELEASED", updated_at_ns=now, release_receipt_id=receipt_id)
            row["history"].append({"state": "RELEASED", "at_ns": now,
                                   "release_receipt_id": receipt_id})
            changed = True
        if changed:
            ledger._save(state)


def _unbound_execution(row: dict[str, Any]) -> bool:
    """Admitted or waiting, but no holder was ever bound: nothing was started under it."""
    return (row["request"].get("kind") == "execution" and row["state"] in {"WAITING", "RESERVED"}
            and row.get("holder") is None and row["request"].get("allocation_id") is not None)


def _absent(path: Path) -> bool | None:
    """True only when the filesystem reports ENOENT; None when observation itself failed.

    ``os.path.lexists`` reports permission (EACCES), I/O (EIO) and similar errors
    as absence. Those say nothing about whether the path exists, so they never
    count as absence evidence.
    """
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return None
    return False


def _absence_anchor(path: Path, device: int) -> Path | None:
    """The nearest existing ancestor, only if it is a real directory on ``device``."""
    anchor = path.parent
    while True:
        try:
            info = os.lstat(anchor)
        except FileNotFoundError:
            if anchor.parent == anchor:
                return None
            anchor = anchor.parent
            continue
        except OSError:
            return None
        return anchor if stat.S_ISDIR(info.st_mode) and info.st_dev == device else None


def _vanished_repository(request: dict[str, Any]) -> dict[str, Any] | None:
    """Positive evidence that nothing this checkout budget reserved can exist.

    The repository, its common directory and the checkout path all report ENOENT,
    and each one's nearest existing ancestor is a real directory on the very
    filesystem the budget was reserved on, so the absence is observed on a mounted
    filesystem rather than inferred from a missing mount or an unreadable path.
    Anything present, and any failed observation, keeps the reservation.
    """
    paths = [Path(request[name]) for name in ("common_dir", "repository_path", "target_path")]
    if any(_absent(path) is not True for path in paths):
        return None
    anchors = [_absence_anchor(path, request["filesystem_device"]) for path in paths]
    if any(anchor is None for anchor in anchors):
        return None
    return {"contract": "vanished-repository/v1", "observed_at_ns": time.time_ns(),
            "absent_paths": [str(path) for path in paths], "anchor": str(anchors[2]),
            "anchors": [str(anchor) for anchor in anchors],
            "filesystem_device": request["filesystem_device"]}


def _observe_checkout(
    ledger: AdmissionLedger, key: str, row: dict[str, Any], result: dict[str, Any],
    repository: RepositoryInventory, store: MetadataStore, snapshot: MetadataSnapshot,
    *, apply: bool,
) -> bool:
    """Settle host accounting under the repository lock; never acquire a checkout.

    Only RESERVED acquire recovery can change repository evidence and needs an
    exclusive lock. ALLOCATED observations change at most the separate host ledger.
    """
    from . import transaction_scope
    from .policy import load_policy

    request = row["request"]
    if str(repository.common_dir) != request["common_dir"] or snapshot.repository_id != request["repository_id"]:
        raise ControllerError("RESOURCE_IDENTITY_CHANGED", "repository identity changed")
    if not load_policy(repository.canonical_path).resource_admission:
        raise ControllerError("RESOURCE_ADMISSION_UNSUPPORTED", "resource policy is no longer enabled")
    target = Path(request["target_path"])
    pending = transaction_scope.conflicts(store, allocation=row.get("allocation_id"), target=target)
    record = snapshot.records.get(row.get("allocation_id"))
    item = next((item for item in repository.worktrees if item.path == target), None)
    if (row["state"] == "RESERVED" and row.get("transaction_id")
            and ledger.reset_no_effect_acquire(store, key, apply=apply)):
        result.update(state="WAITING" if apply else row["state"],
                      terminal_evidence="ACQUIRE_NO_EFFECT_SUPERSEDED")
        return True
    arguments = request["arguments"]
    intent = snapshot.integration_intents.get(arguments.get("integration_intent_id"))
    if (row["state"] in {"WAITING", "RESERVED"} and not row.get("transaction_id")
            and not row.get("allocation_id") and intent is not None
            and integration_intent_abandoned(intent)):
        if (arguments.get("role") != "integration"
                or any(arguments.get(name) != getattr(intent, name)
                       for name in ("task_key", "authority_key", "module", "owner"))
                or tuple(arguments.get("source_allocations", ())) != intent.source_allocations
                or arguments.get("source_handoffs") or len(row.get("group", [key])) != 1):
            raise ControllerError("INTEGRATION_INTENT_MISMATCH", "abandoned claim identity changed")
        if pending:
            raise ControllerError("INTERRUPTED_TRANSACTION", "abandoned claim has unresolved lifecycle effects")
        if (item is not None or _absent(target) is not True
                or f"refs/heads/{arguments['branch']}" in public_refs(repository.canonical_path)
                or any(record.role == "integration" and record.task_key == intent.task_key
                       and record.lifecycle != "RELEASED" for record in snapshot.records.values())):
            raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "abandoned claim cannot be proven unattempted")
        if apply:
            if not store.flock_held:
                raise ControllerError("MISSING_REPOSITORY_LOCK", "claim settlement requires its flock")
            ledger._cancel_abandoned_claim(key, row, {"intent_id": intent.intent_id,
                "repository_id": snapshot.repository_id, "terminal_event": dict(intent.history[-1]),
                "failure_reason": intent.blocked_reason})
        result.update(state="CANCELLED", terminal_evidence="PREPARATION_ABANDONED",
                      integration_intent_id=intent.intent_id, task_completed=False)
        return True
    if record is not None and record.lifecycle == "RELEASED":
        if item or target.exists() or target.is_symlink() or not any(receipt.allocation_id == record.allocation_id for receipt in snapshot.release_receipts.values()):
            raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "release evidence and Git disagree")
        if apply:
            ledger.released(key, record.allocation_id)
        result["state"] = "RELEASED"
    elif record is None and row.get("allocation_id") and any(receipt.allocation_id == row.get("allocation_id") for receipt in snapshot.release_receipts.values()):
        if item or target.exists() or target.is_symlink():
            raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "release evidence and Git disagree")
        if apply:
            ledger.released(key, row["allocation_id"])
        result["state"] = "RELEASED"
    elif pending:
        raise ControllerError("INTERRUPTED_TRANSACTION", "nonterminal repository transaction requires recovery")
    elif row.get("transaction_id"):
        expected_base = request["arguments"].get("lineage_base_commit") or request["base_oid"]
        if (record is None or item is None or snapshot.bindings.get(item.key) != record.allocation_id
            or record.task_key != request["arguments"]["task_key"]
            or record.authority_key != request["arguments"]["authority_key"]
            or (record.base_commit != expected_base and record.base_commit != request["base_oid"])
            or record.owner != request["arguments"]["owner"]):
            raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "an attempted allocation cannot be proven complete")
        if apply and (row["state"] == "RESERVED" or time.time_ns() - row["updated_at_ns"] >= 300 * 10**9):
            ledger.allocated(request, allocation_id=record.allocation_id)
        result["state"] = "ALLOCATED"
    return False


def _allocated_observations(
    ledger: AdmissionLedger, rows: dict[str, Any], selected: str | None, *, apply: bool,
) -> dict[str, dict[str, Any]]:
    """Share one fresh Git/metadata observation only while its shared flock is held.

    Complete checkout requests cannot replay an acquire or change repository
    evidence. Observe these before any queued mutation; discard every snapshot
    when the lock ends. Recovery and queued acquisitions keep their ordinary path.
    """
    grouped: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for key, row in rows.items():
        request = row["request"]
        if (row["state"] == "ALLOCATED" and row.get("transaction_id")
                and request.get("kind", "checkout") == "checkout"
                and (selected is None or request["common_dir"] == selected)):
            if selected is None and _vanished_repository(request) is not None:
                continue  # Settle absence through the ordinary per-request path.
            grouped.setdefault(request["common_dir"], []).append((key, row))
    observed: dict[str, dict[str, Any]] = {}
    for group in grouped.values():
        results = {key: {"request_id": key, "state": row["state"],
                        "task_key": row["request"]["arguments"]["task_key"],
                        "repository_id": row["request"]["repository_id"],
                        "allocation_id": row.get("allocation_id")} for key, row in group}
        try:
            path = Path(group[0][1]["request"]["repository_path"])
            _, common = resolve_repository(path)
            store = MetadataStore(common)
            with store.lock(exclusive=False):
                if apply and not store.flock_held:
                    raise ControllerError("MISSING_REPOSITORY_LOCK", "accounting requires the repository lock")
                repository = inventory(path)
                snapshot = store._load_unlocked()
                for key, row in group:
                    try:
                        _observe_checkout(ledger, key, row, results[key], repository, store, snapshot, apply=apply)
                    except (ControllerError, OSError) as exc:
                        results[key]["blocker"] = getattr(exc, "code", "RESOURCE_OBSERVATION_UNAVAILABLE")
        except (ControllerError, OSError) as exc:
            for result in results.values():
                result["blocker"] = getattr(exc, "code", "RESOURCE_OBSERVATION_UNAVAILABLE")
        observed.update(results)
    return observed


def reconcile(*, repository_path: Path | None = None, apply: bool = False) -> dict[str, Any]:
    """Resume persisted requests, using the ordinary acquisition/recovery gates.

    No-action WAITING requests can run again. A RESERVED request with an attempt
    identity is never replayed: only an already recorded allocation can settle it.
    A partial/missing allocation remains a formal repository recovery blocker.
    """
    from . import lifecycle
    from .policy import load_policy

    ledger = AdmissionLedger()
    rows = ledger.read()["requests"]
    selected = str(resolve_repository(repository_path)[1]) if repository_path else None
    outcomes = []
    maintenance = []
    if apply:
        # The existing host ledger identifies enrolled repositories even when an
        # external task has never been added to AW's workspace navigation list.
        enrolled = {}
        absent_checkouts = {(request["repository_id"], request["common_dir"], request["target_path"])
            for row in rows.values() if selected is None
            and (request := row["request"]).get("kind", "checkout") == "checkout"
            and request["common_dir"] and _vanished_repository(request) is not None}
        for row in rows.values():
            request = row["request"]
            if request["common_dir"] and (selected is None or request["common_dir"] == selected):
                # Historical rows keep live repositories enrolled. Only mounted,
                # positively absent checkout evidence excludes a vanished one,
                # including its exact terminal execution (which has no FS budget).
                if ((request["repository_id"], request["common_dir"], request["target_path"]) in absent_checkouts
                        and (request.get("kind", "checkout") == "checkout"
                             or row["state"] in {"RELEASED", "CANCELLED"})):
                    continue
                enrolled.setdefault(request["common_dir"], request)
        for common, request in enrolled.items():
            observation = {"repository_id": request["repository_id"], "repository_path": request["repository_path"]}
            try:
                repository = inventory(Path(request["repository_path"]))
                snapshot = MetadataStore(repository.common_dir).load()
                if str(repository.common_dir) != common or snapshot.repository_id != request["repository_id"]:
                    raise ControllerError("RESOURCE_IDENTITY_CHANGED", "enrolled repository identity changed")
                policy = load_policy(repository.canonical_path)
                eligible = any(intent.status in {"PENDING", "BLOCKED_RETRYABLE"}
                    or (intent.status == "STALE" and intent.allocation_id in snapshot.records
                        and snapshot.records[intent.allocation_id].lifecycle == "HANDOFF_READY")
                    for intent in snapshot.release_intents.values())
                if policy.automatic_feature_audit_release and eligible:
                    observation["release"] = lifecycle.reconcile_release_intents(repository.canonical_path,
                        actor=policy.reconciler_actor, trigger="capacity-pressure")["decision"]
                maintenance.append(observation)
            except (ControllerError, OSError) as exc:
                maintenance.append({**observation, "blocker": getattr(exc, "code", "RESOURCE_OBSERVATION_UNAVAILABLE")})
        rows = ledger.read()["requests"]
    settled_host_requests: set[str] = set()
    if apply and selected is None:
        host_requests = [key for key, row in rows.items()
                         if row["state"] == "ALLOCATED"
                         and row["request"].get("kind") == "execution"
                         and row["request"].get("allocation_id") is None]
        # Preserve ordinary per-request diagnostics and independent repository recovery.
        with suppress(ControllerError, OSError):
            settled_host_requests = _reconcile_finished_host_executions(ledger, host_requests)
    allocated = _allocated_observations(ledger, rows, selected, apply=apply)
    handled: set[str] = set()
    for key, row in sorted(rows.items(), key=lambda item: item[1]["created_at_ns"]):
        request = row["request"]
        if key in handled or (selected and request["common_dir"] != selected):
            continue
        handled.add(key)
        result = {"request_id": key, "state": row["state"], "task_key": request["arguments"]["task_key"],
                  "repository_id": request["repository_id"], "allocation_id": row.get("allocation_id")}
        if row["state"] in {"RELEASED", "CANCELLED"}:
            continue
        if key in allocated:
            outcomes.append(allocated[key])
            continue
        if request.get("kind") == "execution":
            # The owning AW Run resumes a launch that is durably known not to
            # have started. Controller never executes or replays a command.
            try:
                if key in settled_host_requests:
                    settled = {"code": "HOST_EXECUTION_CLEARED", "task_completed": False,
                               "request_ids": [key]}
                else:
                    settled = (_reconcile_execution(row, apply=apply)
                               if row["state"] == "ALLOCATED" or _unbound_execution(row) else None)
                if settled is None or settled["code"] == "EXECUTION_NOT_STARTED":
                    settled = ledger.plan([request])
                result["result"] = settled
            except (ControllerError, OSError) as exc:
                result["blocker"] = getattr(exc, "code", "RESOURCE_OBSERVATION_UNAVAILABLE")
            outcomes.append(result)
            continue
        try:
            if (selected is None and row["state"] in {"WAITING", "RESERVED", "ALLOCATED"}
                    and (evidence := _vanished_repository(request)) is not None):
                # A repository that no longer exists cannot be selected by path,
                # so only the all-repositories pass observes it.
                if apply:
                    ledger.vanished(key, evidence)
                result.update(state="RELEASED", terminal_evidence=evidence["contract"])
                outcomes.append(result)
                continue
            repository = inventory(Path(request["repository_path"]))
            store = MetadataStore(repository.common_dir)
            with store.lock(exclusive=apply and row["state"] == "RESERVED" and bool(row.get("transaction_id"))):
                if apply and not store.flock_held:
                    raise ControllerError("MISSING_REPOSITORY_LOCK", "accounting requires the repository lock")
                snapshot = store._load_unlocked()
                target = Path(request["target_path"])
                if _observe_checkout(ledger, key, row, result, repository, store, snapshot, apply=apply):
                    outcomes.append(result)
                    continue
            if apply and result["state"] in {"WAITING", "RESERVED"} and not row.get("transaction_id"):
                group = row.get("group", [key])
                if any(member not in rows for member in group):
                    raise ControllerError("INVALID_RESOURCE_LEDGER", "request group is incomplete")
                if len(group) > 1:
                    requests = []
                    for member in group:
                        member_row = rows[member]
                        if member_row.get("transaction_id") or member_row["state"] not in {"WAITING", "RESERVED"}:
                            raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "partial request group requires reconciliation")
                        member_request = member_row["request"]
                        requests.append({"repository": member_request["repository_path"], **member_request["arguments"]})
                    handled.update(group)
                    result["result"] = lifecycle.acquire_across(requests)["decision"]
                else:
                    arguments = {**request["arguments"], "target": target}
                    for name in ("scope_paths", "source_allocations", "source_handoffs", "external_bindings"):
                        arguments[name] = tuple(arguments[name])
                    result["result"] = lifecycle.acquire(repository.canonical_path, **arguments)["decision"]
        except (ControllerError, OSError) as exc:
            result["blocker"] = getattr(exc, "code", "RESOURCE_OBSERVATION_UNAVAILABLE")
        outcomes.append(result)
    return {"schema_version": 1, "command": "admission-queue", "repository": None,
            "decision": {"code": "ADMISSION_RECONCILED" if apply else "ADMISSION_QUEUE",
                         "dry_run": not apply, "outcomes": outcomes, "maintenance": maintenance}, "errors": [], "warnings": [], "worktrees": []}


def _host_runtime_holders(row: dict[str, Any], *, allow_broad_scope: bool = False) -> tuple[WriterHolder, ...]:
    """Return the exact runtime chain for one host-scoped reservation.

    ``allow_broad_scope`` is set only by the typed dead-holder recovery, which has
    already tied the request to the row's own recorded holder and terminal
    evidence.  Unqualified recovery still refuses a home-root or broader target,
    because there the target path cannot bound a reservation to one session.
    """
    from .writer_lease import runtime_binding

    if row.get("lease_id") is not None:
        raise ControllerError("WRITER_LEASE_HELD", "host execution has a writer lease")
    request = row.get("request")
    bindings = row.get("runtime_bindings")
    recorded = row.get("holder")
    if (not isinstance(request, dict) or not isinstance(recorded, dict)
            or not isinstance(bindings, list) or not bindings):
        raise ControllerError("ADMISSION_RECOVERY_REQUIRED",
                              "host execution runtime identity is uncertain")
    try:
        raw_target = Path(request["target_path"])
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ControllerError("ADMISSION_RECOVERY_REQUIRED",
                              "host execution recovery target is uncertain") from exc
    try:
        target = raw_target.resolve(strict=True)
        target_present = True
    except FileNotFoundError:
        target, target_present = raw_target, False
    except (OSError, TypeError, ValueError) as exc:
        raise ControllerError("ADMISSION_RECOVERY_REQUIRED",
                              "host execution recovery target is uncertain") from exc
    if not allow_broad_scope:
        # Unqualified recovery is identity-only: it needs a present, bounded
        # target to tie the recorded runtime chain to one session. Typed
        # recovery carries its own recorded-holder authority and may instead
        # proceed against a provably absent target (orphan release).
        if not target_present:
            raise ControllerError("ADMISSION_RECOVERY_REQUIRED",
                                  "host execution recovery target is uncertain")
        if unbounded_host_scope(target):
            raise ControllerError("ADMISSION_RECOVERY_REQUIRED",
                                  "host execution target is too broad for identity-only recovery")
    try:
        primary = WriterHolder(**recorded)
    except (TypeError, ValueError) as exc:
        raise ControllerError("ADMISSION_RECOVERY_REQUIRED",
                              "host execution runtime identity is uncertain") from exc
    holders = []
    for value in bindings:
        try:
            binding = runtime_binding(value) if isinstance(value, str) else None
        except (ControllerError, TypeError, ValueError) as exc:
            raise ControllerError("ADMISSION_RECOVERY_REQUIRED",
                                  "host execution runtime identity is uncertain") from exc
        if binding is None:
            raise ControllerError("ADMISSION_RECOVERY_REQUIRED",
                                  "host execution runtime identity is uncertain")
        holder, lease_id = binding
        if lease_id is not None:
            raise ControllerError("WRITER_LEASE_HELD", "host execution has a writer lease")
        holders.append(holder)
    expected = (request.get("client_id"), request.get("session_id"), request.get("run_id"))
    if holders[0] != primary or any(
            (holder.client_id, holder.session_id, holder.run_id) != expected for holder in holders):
        raise ControllerError("ADMISSION_RECOVERY_REQUIRED",
                              "host execution runtime identity is uncertain")
    return tuple(holders)


def _host_runtime_is_live(holders: tuple[WriterHolder, ...]) -> bool:
    from .writer_lease import holder_is_live, process_group_is_live

    return any(holder_is_live(holder) or process_group_is_live(holder) for holder in holders)


def _check_host_target(request: dict[str, Any], *, allow_missing: bool = False) -> None:
    try:
        info = os.lstat(Path(request["target_path"]))
    except FileNotFoundError as missing:
        if not allow_missing:
            raise ControllerError(
                "RESOURCE_IDENTITY_CHANGED",
                "host execution target is missing; only typed dead-holder recovery may proceed",
            ) from missing
        return
    if [info.st_dev, info.st_ino] != request["path_identity"]:
        raise ControllerError("RESOURCE_IDENTITY_CHANGED", "host execution directory changed")


def _check_host_exit(row: dict[str, Any]) -> None:
    if _host_runtime_is_live(_host_runtime_holders(row)):
        raise ControllerError("RUNTIME_OCCUPIED", "runtime holder is still alive")


def _reconcile_finished_host_executions(ledger: AdmissionLedger, request_ids: list[str]) -> set[str]:
    """Settle proven host exits with one atomic write, rechecking every row under lock.

    Only host reservations belong here: repository recovery must never run while
    holding the host ledger lock. Uncertain rows retain their complete budget.
    """
    settled: set[str] = set()
    if not request_ids:
        return settled
    with ledger.locked():
        state = ledger.read()
        for request_id in request_ids:
            row = state["requests"].get(request_id)
            if (row is None or row["state"] != "ALLOCATED"
                    or row["request"].get("kind") != "execution"
                    or row["request"].get("allocation_id") is not None
                    or row["request"].get("owner") != os.getuid()):
                continue
            try:
                _check_host_target(row["request"])
                _check_host_exit(row)
            except (ControllerError, OSError):
                continue
            row["state"] = "RELEASED"
            row["history"].append({"state": "RELEASED", "at_ns": time.time_ns()})
            row["updated_at_ns"] = time.time_ns()
            settled.add(request_id)
        if settled:
            ledger._save(state)
    return settled


HOST_RECOVERY_CONTRACT = "host-execution-recovery/v1"
HOST_TERMINAL_EVIDENCE_KIND = "aw-terminal-record"
HOST_TERMINAL_STATUSES = frozenset(
    {"EXITED", "CLOSED", "ERROR", "COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED", "SUCCEEDED"}
)


def _dead_holder_proof(holder: WriterHolder) -> dict[str, Any]:
    """Prove the exact recorded holder cannot still be running, or refuse.

    PID reuse and a stale boot are evidence that the original process is gone, not
    that its task succeeded; they are recorded rather than guessed at.
    """
    from .writer_lease import boot_id, holder_is_live, process_group_is_live, start_ticks

    if not holder.boot_id or holder.boot_id == "unknown-boot":
        raise ControllerError("ADMISSION_RECOVERY_REQUIRED",
                              "host execution recorded boot identity is unknown")
    observed = start_ticks(holder.pid)
    proof = {
        "recorded_pid": holder.pid,
        "recorded_boot_id": holder.boot_id,
        "recorded_start_ticks": holder.start_ticks,
        "observed_boot_id": boot_id(),
        "observed_start_ticks": observed,
        "pid_present": observed is not None,
        "pid_reused": observed is not None and observed != holder.start_ticks,
        "boot_changed": holder.boot_id != boot_id(),
    }
    if holder_is_live(holder) or process_group_is_live(holder):
        raise ControllerError("RUNTIME_OCCUPIED", "typed recovery found the recorded holder alive")
    if observed is not None and observed == holder.start_ticks:
        raise ControllerError("ADMISSION_RECOVERY_REQUIRED",
                              "host execution holder liveness is uncertain")
    return proof


def _terminal_evidence(evidence: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Bind the caller's terminal-state evidence to this row's own identity."""
    if not isinstance(evidence, dict) or evidence.get("kind") != HOST_TERMINAL_EVIDENCE_KIND:
        raise ControllerError("ADMISSION_RECOVERY_REQUIRED",
                              "typed recovery requires terminal execution evidence")
    status = evidence.get("status")
    ended_at = evidence.get("ended_at")
    if (not isinstance(status, str) or status.upper() not in HOST_TERMINAL_STATUSES
            or not isinstance(ended_at, str) or not ended_at.strip()):
        raise ControllerError("ADMISSION_RECOVERY_REQUIRED",
                              "terminal execution evidence is not a final state")
    if evidence.get("session_id") != request.get("session_id"):
        raise ControllerError("ADMISSION_RECOVERY_REQUIRED",
                              "terminal evidence session identity differs from the reservation")
    if request.get("run_id") is not None and evidence.get("run_id") != request.get("run_id"):
        raise ControllerError("ADMISSION_RECOVERY_REQUIRED",
                              "terminal evidence run identity differs from the reservation")
    return {"kind": HOST_TERMINAL_EVIDENCE_KIND, "status": status.upper(),
            "ended_at": ended_at, "session_id": evidence.get("session_id"),
            "run_id": evidence.get("run_id"), "end_reason": evidence.get("end_reason")}


def _host_recovery_reference(ledger: AdmissionLedger, request_id: str, value: dict[str, Any]) -> dict[str, str]:
    """Write recovery evidence once; a repeat must present identical bytes."""
    from . import cleanup_evidence as e

    directory = ledger.root / "host-recoveries"
    path = directory / f"{request_id}.json"
    def stable(document: dict[str, Any]) -> dict[str, Any]:
        return {key: item for key, item in document.items() if key != "recovered_at_ns"}

    if path.exists():
        recorded, digest = e.read(path)
        if stable(recorded) != stable(value):
            raise ControllerError("ADMISSION_RECOVERY_REQUIRED",
                                  "host recovery evidence already exists and differs")
        # Identical authority replayed after a crash between writing the
        # evidence and settling the row: reuse the original immutable record
        # instead of deadlocking on its timestamp.
        return {"path": str(path), "sha256": digest}
    return e.write(directory, path.name, value)


def _typed_host_recovery(ledger: AdmissionLedger, request_id: str, row: dict[str, Any], *,
                         expected_holder_sha256: str | None,
                         terminal_evidence: Any) -> tuple[tuple[WriterHolder, ...], dict[str, str]]:
    """Release one historical execution whose recorded holder is provably dead.

    Deliberately opt-in: the home-scope refusal stays intact for every unqualified
    caller.  The authority here is the row's own recorded identity, cross-checked
    against the caller's terminal evidence, so no process identity is invented and
    a live holder can never be released through this path.
    """

    request = row["request"]
    if request.get("kind") != "execution" or request.get("allocation_id") is not None:
        raise ControllerError("ADMISSION_RECOVERY_REQUIRED",
                              "typed recovery applies only to host execution reservations")
    recorded = row.get("holder")
    if not isinstance(recorded, dict) or any(recorded.get(key) in (None, "") for key in
            ("pid", "boot_id", "start_ticks", "client_id", "session_id", "run_id")):
        raise ControllerError("ADMISSION_RECOVERY_REQUIRED",
                              "host execution recorded identity is incomplete")
    if not expected_holder_sha256 or expected_holder_sha256 != canonical_holder_digest(recorded):
        raise ControllerError("ADMISSION_RECOVERY_REQUIRED",
                              "typed recovery must present the reservation's own holder digest")
    evidence = _terminal_evidence(terminal_evidence, request)
    holders = _host_runtime_holders(row, allow_broad_scope=True)
    proof = _dead_holder_proof(holders[0])
    try:
        os.lstat(Path(str(request.get("target_path"))))
        target_observation = "PRESENT"
    except FileNotFoundError:
        target_observation = "ABSENT"
    except OSError as exc:
        raise ControllerError("ADMISSION_RECOVERY_REQUIRED",
                              "host execution recovery target observation failed") from exc
    recovery = {
        "schema_version": 1,
        "contract": HOST_RECOVERY_CONTRACT,
        "request_id": request_id,
        "original_request": {
            "repository_id": request.get("repository_id"),
            "client_id": request.get("client_id"),
            "session_id": request.get("session_id"),
            "run_id": request.get("run_id"),
            "target_path": request.get("target_path"),
            "memory_bytes": request.get("memory_bytes"),
        },
        "original_holder_digest": canonical_holder_digest(recorded),
        "target_observation": target_observation,
        "dead_holder_proof": proof,
        "terminal_evidence": evidence,
        "terminal_evidence_digest": canonical_holder_digest(evidence),
        "released_state": "RELEASED",
        "recovered_at_ns": time.time_ns(),
    }
    reference = _host_recovery_reference(ledger, request_id, recovery)
    return holders, reference


def reconcile_finished_executions() -> None:
    """Use existing guarded exit recovery before admitting another local execution.

    This also supports standalone clients without an external periodic reaper.
    Live or uncertain bindings remain reserved; checkout requests are never replayed.
    """
    ledger = AdmissionLedger()
    rows = ledger.read()["requests"]
    host_requests = [key for key, row in rows.items()
        if row["state"] == "ALLOCATED" and row["request"].get("kind") == "execution"
        and row["request"].get("allocation_id") is None]
    # A failed atomic write keeps all durable budgets. Repository recovery
    # remains independent, without reversing the repository -> host lock order.
    with suppress(ControllerError, OSError):
        _reconcile_finished_host_executions(ledger, host_requests)
    for row in rows.values():
        if (row["request"].get("allocation_id") is None
                or row["request"].get("kind") != "execution"
                or (row["state"] != "ALLOCATED" and not _unbound_execution(row))):
            continue
        if _allocation_holder_running(row):
            # A running holder has nothing to recover yet. Its repository is not
            # scanned or locked; the reservation is evaluated again once it exits.
            continue
        try:
            _reconcile_execution(row, apply=True)
        except (ControllerError, OSError):
            # Recovery failure keeps its budget and ordinary admission accounts
            # for it. The explicit admission-queue surface reports the blocker.
            continue


def _allocation_holder_running(row: dict[str, Any]) -> bool:
    """Exact recorded holder identity is alive; absent or malformed identity is not."""
    from .writer_lease import holder_is_live

    if row["request"].get("allocation_id") is None or not isinstance(row.get("holder"), dict):
        return False
    try:
        return holder_is_live(WriterHolder(**row["holder"]))
    except (ControllerError, TypeError, ValueError):
        return False


def _reconcile_execution(row: dict[str, Any], *, apply: bool) -> dict[str, Any]:
    """Recover lost exit notifications using existing typed runtime removal gates."""
    from .lifecycle import update_external_binding
    from .policy import load_policy
    from .reconciler import reconcile_writers
    from .semantics import managed_record as _managed_record
    from .writer_lease import RUNTIME_BINDING_PREFIX, holder_is_live, runtime_binding

    request = row["request"]
    if request["allocation_id"] is None:
        holders = _host_runtime_holders(row)
        if _host_runtime_is_live(holders):
            return {"code": "EXECUTION_OCCUPIED", "process_alive": True}
        if not apply:
            return {"code": "EXECUTION_RECONCILIATION_PENDING", "runtime_bindings": len(holders)}
        return host_execution("clear", request_id=fingerprint(request))["decision"]
    repository = inventory(Path(request["repository_path"]))
    store = MetadataStore(repository.common_dir)
    with store.lock(exclusive=False):
        if apply and not store.flock_held:
            raise ControllerError("MISSING_REPOSITORY_LOCK", "accounting requires the repository lock")
        snapshot = store._load_unlocked()
        if (str(repository.common_dir) != request["common_dir"]
                or snapshot.repository_id != request["repository_id"]):
            raise ControllerError("RESOURCE_IDENTITY_CHANGED", "execution repository identity changed")
        recorded = snapshot.records.get(request["allocation_id"])
        if recorded is not None and recorded.lifecycle == "RELEASED":
            from . import transaction_scope

            target = Path(request["target_path"])
            receipts = [receipt for receipt in snapshot.release_receipts.values()
                if (receipt.repository_id, receipt.allocation_id, receipt.target_path,
                    receipt.released_record_version, receipt.result)
                == (snapshot.repository_id, recorded.allocation_id, str(target),
                    recorded.record_version, "RELEASED")]
            if (len(receipts) != 1 or recorded.external_bindings
                    or recorded.allocation_id in snapshot.writer_leases
                    or any(item.path == target for item in repository.worktrees)
                    or target.exists() or target.is_symlink()
                    or transaction_scope.conflicts(store, allocation=recorded.allocation_id, target=target)):
                raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "execution release evidence and Git disagree")
            if apply:
                release_allocation_budget(snapshot.repository_id, recorded.allocation_id, receipts[0].receipt_id)
            return {"code": "EXECUTION_RESOURCES_RELEASED" if apply else "EXECUTION_RECONCILIATION_PENDING",
                    "release_receipt_id": receipts[0].receipt_id, "allocation_released": True,
                    "task_completed": False}
    if row.get("holder") is None:
        # Admitted but never bound while its allocation is still held: a launch
        # may be in flight. Only the allocation's formal release settles it.
        return {"code": "EXECUTION_NOT_STARTED", "allocation_released": False}
    if holder_is_live(WriterHolder(**row["holder"])):
        return {"code": "EXECUTION_OCCUPIED", "process_alive": True}
    item, record = _managed_record(repository, snapshot, Path(request["target_path"]))
    if (snapshot.repository_id != request["repository_id"] or record.allocation_id != request["allocation_id"]
            or record.owner != request.get("owner")):
        raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "execution allocation authority changed")
    bindings = [value for value in record.external_bindings
                if (binding := runtime_binding(value)) is not None and binding[1] == row.get("lease_id")]
    if not bindings:
        # A crash can occur between recording a reservation's holder and writing
        # its runtime binding. The same removal path proves absence; no fake past
        # receipt or binding is inserted into repository history.
        bindings = [RUNTIME_BINDING_PREFIX + json.dumps({"holder": row["holder"],
                    "lease_id": row["lease_id"], "kind": "command"}, sort_keys=True, separators=(",", ":"))]
    if not apply:
        return {"code": "EXECUTION_RECONCILIATION_PENDING", "runtime_bindings": len(bindings)}
    for value in bindings:
        current = store.load().records[record.allocation_id]
        update_external_binding(repository.canonical_path, target=item.path, owner=current.owner,
            expected_record_version=current.record_version, binding=value, action="remove")
    policy = load_policy(repository.canonical_path)
    if policy.reconciler_enabled:
        reconcile_writers(repository.canonical_path, actor=policy.reconciler_actor,
                          trigger="background", dry_run=False, only=(record.task_key,))
    return {"code": "EXECUTION_RESOURCES_RELEASED", "task_completed": False}


def _recovery_guidance(row: dict[str, Any], entry: dict[str, Any], *, dead: bool,
                       note: str | None) -> dict[str, Any]:
    """What a typed caller may do about this reservation, without granting anything."""
    request = row["request"]
    if row["state"] in {"RELEASED", "CANCELLED"}:
        return {"supported_action": None, "reason": "ALREADY_TERMINAL"}
    if request.get("allocation_id") is not None:
        return {"supported_action": None, "reason": "USE_ALLOCATION_RECONCILIATION"}
    if row.get("lease_id") is not None:
        return {"supported_action": None, "reason": "WRITER_LEASE_HELD"}
    if row["state"] in {"WAITING", "RESERVED"} and not row.get("holder"):
        return {"supported_action": "cancel", "reason": "NOT_ATTEMPTED"}
    if dead:
        return {"supported_action": "clear --recover-dead-holder",
                "reason": "HOLDER_PROVABLY_DEAD",
                "expected_holder_sha256": entry.get("holder_sha256"),
                "target_observation": entry.get("target_observation"),
                "terminal_evidence_template": {
                    "kind": HOST_TERMINAL_EVIDENCE_KIND,
                    "status": "EXITED",
                    "ended_at": "<caller-attested RFC3339 end time>",
                    "session_id": request.get("session_id"),
                    "run_id": request.get("run_id"),
                    "end_reason": "<caller-attested reason>"}}
    return {"supported_action": None, "reason": note or "HOLDER_LIVENESS_UNCERTAIN"}


def _inspect_host_executions(ledger: AdmissionLedger, *, request_id: str | None = None,
                             session_id: str | None = None,
                             run_id: str | None = None) -> dict[str, Any]:
    """Read-only recovery intelligence for host execution reservations.

    Publishes exactly the identity a typed caller must present (canonical
    holder bytes and their SHA-256), the runtime observation proving the
    recorded holder dead, the missing-target fact, and a terminal-evidence
    template, so recovery never needs to read the private ledger layout.
    Nothing here mutates ledger state; authority stays with the typed clear
    verb, which re-verifies every fact at settle time.
    """
    from .controller import envelope

    with ledger.locked():
        state = ledger.read()
    rows = []
    for key, row in sorted(state["requests"].items(),
                           key=lambda item: item[1].get("created_at_ns", 0)):
        request = row["request"]
        if request.get("kind") != "execution" or request.get("owner") != os.getuid():
            continue
        if request_id is not None and key != request_id:
            continue
        if session_id is not None and request.get("session_id") != session_id:
            continue
        if run_id is not None and request.get("run_id") != run_id:
            continue
        entry: dict[str, Any] = {
            "request_id": key, "state": row["state"],
            "client_id": request.get("client_id"), "session_id": request.get("session_id"),
            "run_id": request.get("run_id"), "target_path": request.get("target_path"),
            "memory_bytes": request.get("memory_bytes"),
            "accounted_memory_bytes": (max(
                0, int(request.get("memory_bytes", 0)) - execution_memory(row))
                if row["state"] in {"RESERVED", "ALLOCATED", "ATTENTION_REQUIRED"} else 0),
            "allocation_bound": request.get("allocation_id") is not None,
            "writer_lease_held": row.get("lease_id") is not None,
        }
        try:
            info = os.lstat(Path(str(request.get("target_path"))))
            entry["target_observation"] = ("PRESENT" if [info.st_dev, info.st_ino]
                                           == request.get("path_identity") else "IDENTITY_CHANGED")
        except FileNotFoundError:
            entry["target_observation"] = "ABSENT"
        except OSError:
            entry["target_observation"] = "UNCERTAIN"
        holder = row.get("holder")
        dead, note = False, None
        if isinstance(holder, dict):
            canonical = json.dumps(holder, sort_keys=True, separators=(",", ":"))
            entry["holder_canonical_json"] = canonical
            entry["holder_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            try:
                entry["dead_holder_proof"] = _dead_holder_proof(WriterHolder(**holder))
                dead = True
            except ControllerError as exc:
                note = exc.code
        else:
            note = "NO_RECORDED_HOLDER"
        entry["recovery"] = _recovery_guidance(row, entry, dead=dead, note=note)
        rows.append(entry)
    payload = envelope("host-execution")
    payload["decision"] = {"code": "HOST_EXECUTION_INSPECTION", "dry_run": True,
                           "read_only": True, "rows": rows}
    return payload


def host_execution(action: str, *, target: Path | None = None, request_id: str | None = None,
                   client_id: str = "agent-workspace", session_id: str | None = None,
                   run_id: str | None = None, holder_pid: int | None = None,
                   memory_bytes: int = DEFAULT_EXECUTION_MEMORY,
                   recover_dead_holder: bool = False,
                   expected_holder_sha256: str | None = None,
                   terminal_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Budget an AW process without inventing a repository allocation or writer lease.

    Uses the same host request, typed occupancy, exit checks and periodic reconciler
    as governed executions. This grants resources only, never repository writes.
    """
    from dataclasses import asdict

    from .controller import envelope
    from .environment import verify_runtime_transfer
    from .writer_lease import RUNTIME_BINDING_PREFIX, describe_holder

    ledger = AdmissionLedger()
    if action == "inspect":
        return _inspect_host_executions(ledger, request_id=request_id,
                                        session_id=session_id, run_id=run_id)
    if action == "admit":
        if target is None or not session_id:
            raise ControllerError("INVALID_REQUEST", "host admission needs a path and session identity")
        path = target.resolve(strict=True)
        info = path.stat()
        if not path.is_dir():
            raise ControllerError("INVALID_REQUEST", "host execution path must be a directory")
        if unbounded_host_scope(path):
            raise ControllerError(
                "INVALID_REQUEST",
                "host execution target must not be the home root or one of its ancestors; "
                "admit against the session's concrete working directory instead",
            )
        request = execution_request(None, "host:" + str(os.getuid()), None, path,
            client_id=client_id, session_id=session_id, run_id=run_id, memory_bytes=memory_bytes)
        request.update(owner=os.getuid(), path_identity=[info.st_dev, info.st_ino])
        reconcile_finished_executions()
        decision = ledger.reserve([request])
    else:
        with ledger.locked():
            state = ledger.read()
            if request_id is None and action == "cancel" and session_id and target is not None:
                matches = [(key, value) for key, value in state["requests"].items()
                    if value["request"].get("kind") == "execution" and value["request"].get("allocation_id") is None
                    and all(value["request"].get(name) == expected for name, expected in
                            (("owner", os.getuid()), ("client_id", client_id), ("session_id", session_id), ("run_id", run_id)))]
                if not matches:
                    payload = envelope("host-execution")
                    payload["decision"] = {"code": "HOST_EXECUTION_CLEARED", "request_ids": [], "task_completed": False}
                    return payload
                if len(matches) != 1 or matches[0][1]["request"]["target_path"] != str(target.resolve()):
                    raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "host cancellation identity changed")
                request_id = matches[0][0]
            row = state["requests"].get(request_id)
            if (row is None or row["request"].get("kind") != "execution"
                    or row["request"].get("allocation_id") is not None or row["request"]["owner"] != os.getuid()):
                raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "host execution identity is unavailable")
            request = row["request"]
            unattempted_cancel = (
                action == "cancel"
                and row["state"] in {"WAITING", "RESERVED", "CANCELLED"}
                and not row.get("holder")
                and not row.get("runtime_bindings")
                and row.get("lease_id") is None
                and not unbounded_host_scope(Path(request["target_path"]))
            )
            _check_host_target(
                request,
                allow_missing=unattempted_cancel or (action == "clear" and recover_dead_holder),
            )
            if action in {"bind", "transfer"}:
                if holder_pid is None:
                    raise ControllerError("INVALID_HOLDER", "host execution requires its exact process")
                holder = describe_holder(holder_pid, client_id=request["client_id"],
                                         session_id=request["session_id"], run_id=request["run_id"])
                if action == "bind":
                    if row["state"] != "RESERVED":
                        if row["state"] != "ALLOCATED" or row.get("holder") != asdict(holder):
                            raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "host execution was already attempted")
                    else:
                        row.update(state="ALLOCATED", holder=asdict(holder), lease_id=None)
                        row["history"].append({"state": "ALLOCATED", "at_ns": time.time_ns()})
                else:
                    if row["state"] != "ALLOCATED":
                        raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "host transfer needs an allocated execution")
                    verify_runtime_transfer(WriterHolder(**row["holder"]), holder)
                binding = RUNTIME_BINDING_PREFIX + json.dumps({"holder": asdict(holder),
                    "lease_id": None, "kind": "background" if action == "transfer" else "command"},
                    sort_keys=True, separators=(",", ":"))
                bindings = row.setdefault("runtime_bindings", [])
                if binding not in bindings:
                    bindings.append(binding)
                decision = {"code": "HOST_EXECUTION_BOUND", "binding": binding}
            elif action in {"clear", "cancel"}:
                terminal = "RELEASED" if action == "clear" else "CANCELLED"
                recovery = None
                if row["state"] == terminal or (action == "cancel" and row["state"] == "RELEASED"):
                    decision = {"code": "HOST_EXECUTION_CLEARED", "task_completed": False}
                    changed = False
                else:
                    if action == "clear":
                        if row["state"] != "ALLOCATED":
                            raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "host execution has not been bound")
                        if recover_dead_holder:
                            _, recovery = _typed_host_recovery(
                                ledger, request_id, row,
                                expected_holder_sha256=expected_holder_sha256,
                                terminal_evidence=terminal_evidence)
                        else:
                            _check_host_exit(row)
                    elif row["state"] not in {"WAITING", "RESERVED"} or row.get("holder"):
                        raise ControllerError("ADMISSION_RECOVERY_REQUIRED", "an attempted host execution cannot be cancelled")
                    row["state"] = terminal
                    row["history"].append({"state": terminal, "at_ns": time.time_ns()})
                    decision = {"code": "HOST_EXECUTION_CLEARED", "task_completed": False}
                    if recovery is not None:
                        decision["recovery"] = recovery
                    changed = True
            else:
                raise ControllerError("INVALID_REQUEST", "unknown host execution action")
            if action not in {"clear", "cancel"} or changed:
                row["updated_at_ns"] = time.time_ns()
                ledger._save(state)
            decision["request_ids"] = [request_id]
    payload = envelope("host-execution")
    payload["decision"] = decision
    return payload
