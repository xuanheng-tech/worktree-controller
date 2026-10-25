"""Versioned, locked, atomic repository-local metadata storage."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import stat
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

from . import metadata_codec as _codec
from .models import (
    METADATA_SCHEMA_VERSION,
    SUPPORTED_METADATA_SCHEMA_VERSIONS,
    ControllerError,
    IntegrationIntent,
    IntegrationReconciliationReceipt,
    LifecycleEvent,
    MetadataRecord,
    MetadataSnapshot,
    PreparedCandidateReceipt,
    PublicationLease,
    PublicationReceipt,
    ReconciliationReceipt,
    ReleaseIntent,
    ReleasePlan,
    ReleaseReceipt,
    WriterLease,
    WriterReconciliationReceipt,
)

# Preserve the store import interface while codecs stay independent of disk access.
INTEGRATION_INTENT_CONTRACT_VERSION = _codec.INTEGRATION_INTENT_CONTRACT_VERSION
INTEGRATION_INTENT_SCHEMA_VERSION = _codec.INTEGRATION_INTENT_SCHEMA_VERSION
INTEGRATION_RECONCILIATION_RECEIPT_SCHEMA_VERSION = _codec.INTEGRATION_RECONCILIATION_RECEIPT_SCHEMA_VERSION
LIFECYCLE_EVENT_SCHEMA_VERSION = _codec.LIFECYCLE_EVENT_SCHEMA_VERSION
PREPARED_CANDIDATE_RECEIPT_SCHEMA_VERSION = _codec.PREPARED_CANDIDATE_RECEIPT_SCHEMA_VERSION
PUBLICATION_LEASE_SCHEMA_VERSION = _codec.PUBLICATION_LEASE_SCHEMA_VERSION
PUBLICATION_RECEIPT_SCHEMA_VERSION = _codec.PUBLICATION_RECEIPT_SCHEMA_VERSION
PUBLICATION_RECEIPT_STATES = _codec.PUBLICATION_RECEIPT_STATES
RECONCILIATION_RECEIPT_SCHEMA_VERSION = _codec.RECONCILIATION_RECEIPT_SCHEMA_VERSION
RELEASE_INTENT_SCHEMA_VERSION = _codec.RELEASE_INTENT_SCHEMA_VERSION
RELEASE_PLAN_SCHEMA_VERSION = _codec.RELEASE_PLAN_SCHEMA_VERSION
RELEASE_RECEIPT_SCHEMA_VERSION = _codec.RELEASE_RECEIPT_SCHEMA_VERSION
TEARDOWN_RECEIPT_RESULTS = _codec.TEARDOWN_RECEIPT_RESULTS
TEARDOWN_RECEIPT_SCHEMA_VERSION = _codec.TEARDOWN_RECEIPT_SCHEMA_VERSION
WRITER_LEASE_SCHEMA_VERSION = _codec.WRITER_LEASE_SCHEMA_VERSION
WRITER_RECEIPT_SCHEMA_VERSION = _codec.WRITER_RECEIPT_SCHEMA_VERSION
_disposable_resources = _codec._disposable_resources
_nonempty_string = _codec._nonempty_string
_nonnegative_int = _codec._nonnegative_int
_optional_oid = _codec._optional_oid
_optional_recovery_ref = _codec._optional_recovery_ref
_optional_sha256 = _codec._optional_sha256
_optional_string = _codec._optional_string
_optional_uuid = _codec._optional_uuid
_positive_int = _codec._positive_int
_required_oid = _codec._required_oid
_required_sha256 = _codec._required_sha256
_scope_digest_value = _codec._scope_digest_value
_scope_paths = _codec._scope_paths
_source_allocations = _codec._source_allocations
_string_list = _codec._string_list
_validate_timestamp = _codec._validate_timestamp
integration_intent_from_dict = _codec.integration_intent_from_dict
integration_intent_to_dict = _codec.integration_intent_to_dict
integration_reconciliation_receipt_from_dict = _codec.integration_reconciliation_receipt_from_dict
integration_reconciliation_receipt_to_dict = _codec.integration_reconciliation_receipt_to_dict
lifecycle_event_from_dict = _codec.lifecycle_event_from_dict
lifecycle_event_to_dict = _codec.lifecycle_event_to_dict
prepared_candidate_receipt_from_dict = _codec.prepared_candidate_receipt_from_dict
prepared_candidate_receipt_to_dict = _codec.prepared_candidate_receipt_to_dict
publication_lease_from_dict = _codec.publication_lease_from_dict
publication_lease_to_dict = _codec.publication_lease_to_dict
publication_receipt_from_dict = _codec.publication_receipt_from_dict
publication_receipt_to_dict = _codec.publication_receipt_to_dict
reconciliation_receipt_from_dict = _codec.reconciliation_receipt_from_dict
reconciliation_receipt_to_dict = _codec.reconciliation_receipt_to_dict
record_from_dict = _codec.record_from_dict
record_to_dict = _codec.record_to_dict
release_intent_from_dict = _codec.release_intent_from_dict
release_intent_to_dict = _codec.release_intent_to_dict
release_plan_from_dict = _codec.release_plan_from_dict
release_plan_to_dict = _codec.release_plan_to_dict
release_receipt_from_dict = _codec.release_receipt_from_dict
release_receipt_to_dict = _codec.release_receipt_to_dict
validate_teardown_receipt = _codec.validate_teardown_receipt
writer_lease_from_dict = _codec.writer_lease_from_dict
writer_lease_to_dict = _codec.writer_lease_to_dict
writer_receipt_to_dict = _codec.writer_receipt_to_dict

# Persisted timestamps use the existing UTC+08:00 offset without requiring OS tzdata.
TIMEZONE = timezone(timedelta(hours=8))
ROOT_NAME = "worktree-controller"
VERSION_NAME = "v1"
# A linked registration's identity: a token written into Git's own administrative
# directory for that worktree and remembered under the metadata root. Git removes
# the administrative directory with the worktree, so a later registration that
# reuses the same name (and therefore the same worktree key) cannot carry the token.
REGISTRATION_CONTRACT = "worktree-registration/v1"
REGISTRATION_MARKER = "worktree-controller-registration"
_REGISTRATION_MARKER_LIMIT = 4096


_Decoded = TypeVar("_Decoded")
# Commands loading several snapshots reuse decodes until file identity changes.
_DECODE_CACHE: dict[tuple[str, str], tuple[tuple[int, int, int, int], Any]] | None = None
_HISTORY_CACHE: dict[
    tuple[str, str], tuple[dict[str, tuple[int, int, int, int]], dict[str, Any]]
] | None = None
# CLI decoding retains its byte-reader error contract even without reuse.
_BYTE_READS = False


@contextmanager
def decode_cache(*, reuse: bool = True) -> Iterator[None]:
    """Reuse decoded metadata within one command; nothing survives the command.

    Metadata files are only ever replaced atomically, so an unchanged (device,
    inode, size, mtime) identity is the same content. Decoded values are frozen
    dataclasses and are shared read-only. Single-snapshot commands can disable
    reuse while retaining the same byte decoding and error details.
    """
    global _DECODE_CACHE, _HISTORY_CACHE, _BYTE_READS
    previous, history_previous, bytes_previous = _DECODE_CACHE, _HISTORY_CACHE, _BYTE_READS
    _DECODE_CACHE = ({} if previous is None else previous) if reuse else None
    _HISTORY_CACHE = ({} if history_previous is None else history_previous) if reuse else None
    _BYTE_READS = True
    try:
        yield
    finally:
        _DECODE_CACHE = previous
        _HISTORY_CACHE = history_previous
        _BYTE_READS = bytes_previous


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _listing_error(directory: Path, exc: OSError) -> ControllerError:
    if exc.errno in {errno.EACCES, errno.EPERM}:
        return ControllerError(
            "FILESYSTEM_PERMISSION",
            f"metadata directory cannot be listed with current filesystem permissions: "
            f"{directory} ({errno.errorcode.get(exc.errno, 'UNKNOWN')})",
        )
    return ControllerError("INVALID_METADATA", f"cannot list metadata directory {directory}: {exc}")


def _json_entries(directory: Path) -> list[os.DirEntry[str]]:
    """Flat ``*.json`` entries from a fresh scan on every load.

    A missing directory has no entries. Any other listing failure is incomplete
    evidence and fails closed: an unreadable directory must never look empty.
    """
    try:
        with os.scandir(directory) as entries:
            return sorted(
                (entry for entry in entries if entry.name.endswith(".json")),
                key=lambda entry: entry.name,
            )
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise _listing_error(directory, exc) from exc


def _glob_json(directory: Path) -> list[Path]:
    """``sorted(directory.glob("*.json"))`` without silently dropping listing errors."""
    return [
        Path(entry.path) for entry in _json_entries(directory) if not entry.name.startswith(".")
    ]


def _glob_nested_json(directory: Path) -> list[Path]:
    """``sorted(directory.glob("*/*.json"))`` without silently dropping listing errors."""
    try:
        with os.scandir(directory) as entries:
            children = sorted(
                entry.name
                for entry in entries
                if not entry.name.startswith(".") and entry.is_dir(follow_symlinks=True)
            )
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise _listing_error(directory, exc) from exc
    return sorted(path for name in children for path in _glob_json(directory / name))


def now_iso() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


class MetadataStore:
    """Repository-local controller state. Read methods never create files."""

    def __init__(self, common_dir: Path, *, _migration_root: Path | None = None) -> None:
        # Only the explicit layout preflight inspects the caller-supplied old root.
        # Runtime commands never discover or select a previous namespace.
        self.root = _migration_root or common_dir / ROOT_NAME / VERSION_NAME
        self._migration_view = _migration_root is not None
        if self.root.parent.parent != common_dir or self.root.name != VERSION_NAME:
            raise ControllerError(
                "INVALID_METADATA", "metadata root is outside its Git common directory"
            )
        self.repo_file = self.root / "repo.json"
        self.records_dir = self.root / "records"
        self.bindings_dir = self.root / "bindings"
        self.transactions_dir = self.root / "transactions"
        self.teardown_receipts_dir = self.root / "teardown-receipts"
        self.completion_receipts_dir = self.root / "completion-receipts"
        self.integration_intents_dir = self.root / "integration-intents"
        self.prepared_candidate_receipts_dir = self.root / "prepared-candidate-receipts"
        self.integration_reconciliation_receipts_dir = (
            self.root / "integration-reconciliation-receipts"
        )
        self.lifecycle_events_dir = self.root / "lifecycle-events"
        self.lifecycle_event_acks_dir = self.root / "lifecycle-event-acks"
        self.release_intents_dir = self.root / "release-intents"
        self.release_plans_dir = self.root / "release-plans"
        self.publication_receipts_dir = self.root / "publication-receipts"
        self.release_receipts_dir = self.root / "release-receipts"
        self.registration_prune_receipts_dir = self.root / "registration-prune-receipts"
        self.branch_retirement_plans_dir = self.root / "branch-retirement-plans"
        self.branch_retirement_authorizations_dir = self.root / "branch-retirement-authorizations"
        self.branch_retirement_receipts_dir = self.root / "branch-retirement-receipts"
        self.reconciliation_receipts_dir = self.root / "reconciliation-receipts"
        self.writer_leases_dir = self.root / "writer-leases"
        self.writer_receipts_dir = self.root / "writer-reconciliation-receipts"
        self.cleanup_actions_dir = self.root / "cleanup-actions"
        self.cleanup_recoveries_dir = self.root / "cleanup-recoveries"
        self.native_session_recoveries_dir = self.root / "native-session-recoveries"
        self.session_closeouts_dir = self.root / "session-closeouts"
        self.binding_registrations_dir = self.root / "binding-registrations"
        self.publication_lease_file = self.root / "publication-lease.json"
        self.lock_file = self.root / "repo.lock"
        self._flock_depth = 0

    @property
    def flock_held(self) -> bool:
        """True only while this process actually holds the repository flock."""
        return self._flock_depth > 0

    @contextmanager
    def lock(self, *, exclusive: bool, create: bool = False) -> Iterator[None]:
        """Serialize repository access; writers fail closed on any lock anomaly.

        The lock file is the arbiter for every writer, so an exclusive request
        without a usable regular-file lock never proceeds. A malformed lock
        and a filesystem access failure both fail closed for every caller, with
        separate error codes so doctor can identify the responsible layer.
        Only a genuinely missing lock keeps the shared read path open, which
        lets doctor diagnose the state it cannot lock. While a flock is
        actually held ``_flock_depth`` records it, so terminal-evidence
        classifiers can refuse to judge state they do not serialize against.
        """
        def access_error(exc: OSError) -> ControllerError:
            if exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
                return ControllerError(
                    "FILESYSTEM_PERMISSION",
                    f"repository lock cannot be accessed with current filesystem permissions: "
                    f"{self.lock_file} ({errno.errorcode.get(exc.errno, 'UNKNOWN')})",
                )
            return ControllerError(
                "REPOSITORY_LOCK_ACCESS_UNKNOWN",
                f"repository lock access could not be classified: {self.lock_file} "
                f"({errno.errorcode.get(exc.errno, 'UNKNOWN')})",
            )

        if create:
            try:
                self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError as exc:
                raise access_error(exc) from exc
        descriptor: int | None = None
        try:
            lock_mode = os.lstat(self.lock_file).st_mode
        except FileNotFoundError:
            lock_mode = None
        except OSError as exc:
            raise access_error(exc) from exc
        if lock_mode is not None and not stat.S_ISREG(lock_mode):
            raise ControllerError(
                "REPOSITORY_LOCK_INVALID",
                f"repository lock is not a regular file: {self.lock_file}",
            )
        if create or lock_mode is not None:
            flags = os.O_RDWR | (os.O_CREAT if create else 0) | os.O_NOFOLLOW | os.O_CLOEXEC
            try:
                candidate = os.open(self.lock_file, flags, 0o600)
            except OSError as exc:
                if exc.errno not in {errno.ELOOP, errno.EISDIR}:
                    raise access_error(exc) from exc
                raise ControllerError(
                    "REPOSITORY_LOCK_INVALID",
                    f"repository lock is no longer a regular file: {self.lock_file}",
                ) from exc
            try:
                candidate_mode = os.fstat(candidate).st_mode
            except OSError as exc:
                os.close(candidate)
                raise access_error(exc) from exc
            if stat.S_ISREG(candidate_mode):
                descriptor = candidate
            else:
                os.close(candidate)
                raise ControllerError(
                    "REPOSITORY_LOCK_INVALID",
                    f"repository lock is not a regular file: {self.lock_file}",
                )
        elif exclusive:
            raise ControllerError(
                "MISSING_REPOSITORY_LOCK",
                f"repository lock file is missing: mutations fail closed until {self.lock_file} "
                "is recreated by a bootstrap command (adopt, acquire, enqueue-integration)",
            )
        if descriptor is None:
            yield
            return
        held = False
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            except OSError as exc:
                raise access_error(exc) from exc
            held = True
            self._flock_depth += 1
            try:
                yield
            finally:
                self._flock_depth -= 1
        finally:
            try:
                if held:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    except OSError as exc:
                        raise access_error(exc) from exc
            finally:
                os.close(descriptor)

    @contextmanager
    def cleanup_observation_lock(self) -> Iterator[None]:
        """Keep filesystem/journal diagnostics coherent with typed cleanup.

        History needs only repo.lock; doctor also reads the mutable cleanup
        journal and filesystem. Take the directory gate before repo.lock so a
        waiting diagnostic never obstructs the cleanup it is waiting for.
        """
        with self.lock(exclusive=False):
            if not self.transactions_dir.exists():
                # Cleanup must first take repo.lock exclusively to create it.
                yield
                return
        try:
            descriptor = os.open(
                self.transactions_dir,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
        except OSError as exc:
            raise ControllerError(
                "RESOURCE_CLEANUP_LOCK_UNAVAILABLE", "cleanup journal directory cannot be observed"
            ) from exc
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH)
            except OSError as exc:
                raise ControllerError(
                    "RESOURCE_CLEANUP_LOCK_UNAVAILABLE", "cleanup observation lock cannot be acquired"
                ) from exc
            with self.lock(exclusive=False):
                try:
                    opened = os.fstat(descriptor)
                    named = os.stat(self.transactions_dir, follow_symlinks=False)
                except OSError as exc:
                    raise ControllerError(
                        "RESOURCE_CLEANUP_LOCK_UNAVAILABLE", "cleanup journal directory disappeared"
                    ) from exc
                if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                    raise ControllerError(
                        "RESOURCE_CLEANUP_LOCK_UNAVAILABLE", "cleanup journal directory changed"
                    )
                yield
        finally:
            os.close(descriptor)

    @contextmanager
    def cleanup_lock(self) -> Iterator[None]:
        """Fence typed filesystem cleanup while keeping metadata readers available.

        Cleanup changes only its atomic journal, append-only action/completion
        evidence and the authorized ignored trees, never allocation metadata.
        The existing transactions directory serializes these cleanup executions;
        a shared repo.lock excludes every ordinary writer, including old runtimes.
        Both locks stay held through the final scan or failure receipt. There is
        no lock conversion or unlocked action boundary.
        """
        descriptor = None
        try:
            # Require the real mutation arbiter before initializing the existing
            # journal directory. Never bootstrap a missing repository lock here.
            try:
                with self.lock(exclusive=True):
                    self.transactions_dir.mkdir(mode=0o700, exist_ok=True)
                    descriptor = os.open(
                        self.transactions_dir,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    )
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except OSError as exc:
                raise ControllerError(
                    "RESOURCE_CLEANUP_LOCK_UNAVAILABLE", "cleanup journal directory cannot be locked"
                ) from exc
            with self.lock(exclusive=False):
                if not self.flock_held:
                    raise ControllerError("MISSING_REPOSITORY_LOCK", "cleanup requires the repository lock")
                try:
                    opened = os.fstat(descriptor)
                    named = os.stat(self.transactions_dir, follow_symlinks=False)
                except OSError as exc:
                    raise ControllerError(
                        "RESOURCE_CLEANUP_LOCK_UNAVAILABLE", "cleanup journal directory disappeared"
                    ) from exc
                if (opened.st_dev, opened.st_ino, opened.st_mode) != (
                    named.st_dev, named.st_ino, named.st_mode
                ):
                    raise ControllerError(
                        "RESOURCE_CLEANUP_LOCK_UNAVAILABLE", "cleanup journal directory changed"
                    )
                yield
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _decoded(
        self, path: Path | os.DirEntry[str], decode: Callable[[Mapping[str, Any]], _Decoded]
    ) -> _Decoded:
        cache = _DECODE_CACHE
        if cache is None and not _BYTE_READS:
            return decode(self._read_json(path))
        entry = isinstance(path, os.DirEntry)
        key = (decode.__qualname__, os.fspath(path)) if cache is not None else None
        cached = cache.get(key) if cache is not None else None
        if cached is not None:
            try:
                # Entries come from this load's scan and are never retained in the cache.
                info = path.stat() if entry else os.stat(path)
                if _identity(info) == cached[0]:
                    return cached[1]
            except OSError:
                pass
        try:
            with open(path, "rb") as handle:
                identity = _identity(os.fstat(handle.fileno()))
                raw = handle.read().decode("utf-8")
            value = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ControllerError("INVALID_METADATA", f"cannot read {os.fspath(path)}: {exc}") from exc
        if not isinstance(value, dict):
            raise ControllerError("INVALID_METADATA", f"metadata must be an object: {os.fspath(path)}")
        decoded = decode(value)
        if cache is not None and key is not None:
            cache[key] = (identity, decoded)
        return decoded

    def _read_json(self, path: Path | os.DirEntry[str]) -> Mapping[str, Any]:
        try:
            with open(path, encoding="utf-8") as handle:
                raw = handle.read()
            value = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ControllerError("INVALID_METADATA", f"cannot read {os.fspath(path)}: {exc}") from exc
        if not isinstance(value, dict):
            raise ControllerError("INVALID_METADATA", f"metadata must be an object: {os.fspath(path)}")
        return value

    def _history(
        self,
        directory: Path,
        decode: Callable[[Mapping[str, Any]], _Decoded],
        identity: Callable[[_Decoded], str],
        kind: str,
    ) -> dict[str, _Decoded]:
        entries = _json_entries(directory)
        cache = _HISTORY_CACHE
        key = (decode.__qualname__, os.fspath(directory))
        cached = cache.get(key) if cache is not None else None
        if cached is not None:
            identities, values = cached
            try:
                if len(entries) == len(identities) and all(
                    _identity(path.stat()) == identities.get(path.name) for path in entries
                ):
                    # Neither directory entries nor the caller's mutable mapping are retained.
                    return values.copy()
            except OSError:
                pass
        values = {}
        identities = {}
        for path in entries:
            value = self._decoded(path, decode)
            identifier = identity(value)
            if path.name != f"{identifier}.json":
                message = (
                    f"record filename does not match allocation: {path.path}"
                    if kind == "record" else f"{kind} filename is invalid"
                )
                raise ControllerError("INVALID_METADATA", message)
            if identifier in values:
                message = (
                    f"duplicate allocation: {identifier}"
                    if kind == "record" else f"{kind} filename is invalid"
                )
                raise ControllerError("INVALID_METADATA", message)
            values[identifier] = value
            if cache is not None and _DECODE_CACHE is not None:
                identities[path.name] = _DECODE_CACHE[(decode.__qualname__, path.path)][0]
        if cache is not None:
            cache[key] = (identities, values.copy())
        return values

    def _atomic_json(
        self, path: Path, payload: Mapping[str, Any], *, compact: bool = False
    ) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        encoded = (json.dumps(
            payload, ensure_ascii=False, indent=None if compact else 2, sort_keys=True,
            separators=(",", ":") if compact else None,
        ) + "\n").encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            with suppress(FileNotFoundError):
                temporary.unlink()
            raise

    def _load_unlocked(self) -> MetadataSnapshot:
        warnings: list[dict[str, str]] = []
        repository_id: str | None = None
        records: dict[str, MetadataRecord] = {}
        bindings: dict[str, str] = {}
        integration_intents: dict[str, IntegrationIntent] = {}
        prepared_candidate_receipts: dict[str, PreparedCandidateReceipt] = {}
        integration_reconciliation_receipts: dict[str, IntegrationReconciliationReceipt] = {}
        release_intents: dict[str, ReleaseIntent] = {}
        release_plans: dict[str, ReleasePlan] = {}
        publication_lease: PublicationLease | None = None
        publication_receipts: dict[str, PublicationReceipt] = {}
        release_receipts: dict[str, ReleaseReceipt] = {}
        reconciliation_receipts: dict[str, ReconciliationReceipt] = {}
        if self.repo_file.exists():
            repo = self._read_json(self.repo_file)
            if (
                set(repo)
                != {
                    "schema_version",
                    "repository_id",
                    "created_at",
                    "updated_at",
                }
                or repo.get("schema_version") not in SUPPORTED_METADATA_SCHEMA_VERSIONS
            ):
                raise ControllerError("INVALID_METADATA", "repo.json schema is invalid")
            repository_id = _nonempty_string(repo.get("repository_id"), "repository_id")
            try:
                uuid.UUID(repository_id)
            except ValueError as exc:
                raise ControllerError("INVALID_METADATA", "repository_id must be a UUID") from exc
            _validate_timestamp(repo.get("created_at"), "created_at")
            _validate_timestamp(repo.get("updated_at"), "updated_at")
        elif self.root.exists():
            warnings.append({"code": "MISSING_REPO_METADATA", "message": "repo.json is missing"})

        if self.records_dir.exists():
            records = self._history(
                self.records_dir, record_from_dict, lambda record: record.allocation_id, "record"
            )

        if self.bindings_dir.exists():
            for path in _glob_json(self.bindings_dir):
                binding = self._read_json(path)
                if set(binding) != {"schema_version", "allocation_id"}:
                    raise ControllerError("INVALID_METADATA", f"binding schema is invalid: {path}")
                if binding.get("schema_version") not in SUPPORTED_METADATA_SCHEMA_VERSIONS:
                    raise ControllerError("INVALID_METADATA", f"binding version is invalid: {path}")
                worktree_key = path.stem
                allocation_id = _nonempty_string(binding.get("allocation_id"), "allocation_id")
                bindings[worktree_key] = allocation_id
            for worktree_key, allocation_id in self._drifted_bindings(bindings).items():
                # The registration this binding was written for is gone: the Git
                # administrative directory now belongs to another worktree.
                del bindings[worktree_key]
                warnings.append(
                    {
                        "code": "BINDING_IDENTITY_DRIFT",
                        "message": "a different Git registration reuses this bound worktree key",
                        "worktree_key": worktree_key,
                        "allocation_id": allocation_id,
                    }
                )

        if self.publication_lease_file.exists():
            publication_lease = publication_lease_from_dict(
                self._read_json(self.publication_lease_file)
            )

        if self.integration_intents_dir.exists():
            allocations: set[str] = set()
            for path in _glob_json(self.integration_intents_dir):
                intent = self._decoded(path, integration_intent_from_dict)
                if path.stem != intent.intent_id or intent.intent_id in integration_intents:
                    raise ControllerError(
                        "INVALID_METADATA", "integration intent filename or identity is invalid"
                    )
                if intent.allocation_id is not None:
                    if intent.allocation_id in allocations:
                        raise ControllerError(
                            "INVALID_METADATA", "multiple intents bind one allocation"
                        )
                    allocations.add(intent.allocation_id)
                integration_intents[intent.intent_id] = intent

        if self.prepared_candidate_receipts_dir.exists():
            prepared_candidate_receipts = self._history(
                self.prepared_candidate_receipts_dir, prepared_candidate_receipt_from_dict,
                lambda receipt: receipt.receipt_id, "prepared candidate receipt"
            )

        if self.integration_reconciliation_receipts_dir.exists():
            for path in _glob_json(self.integration_reconciliation_receipts_dir):
                receipt = self._decoded(path, integration_reconciliation_receipt_from_dict)
                if (
                    path.stem != receipt.receipt_id
                    or receipt.receipt_id in integration_reconciliation_receipts
                ):
                    raise ControllerError(
                        "INVALID_METADATA",
                        "integration reconciliation receipt filename is invalid",
                    )
                integration_reconciliation_receipts[receipt.receipt_id] = receipt

        if self.lifecycle_events_dir.exists():
            event_ids: set[str] = set()
            event_sequences: set[int] = set()
            for path in _glob_json(self.lifecycle_events_dir):
                event = self._decoded(path, lifecycle_event_from_dict)
                if (
                    path.stem != event.event_id
                    or event.event_id in event_ids
                    or event.sequence < 1
                    or event.sequence in event_sequences
                ):
                    raise ControllerError(
                        "INVALID_METADATA", "lifecycle event identity or sequence is invalid"
                    )
                event_ids.add(event.event_id)
                event_sequences.add(event.sequence)
        if self.lifecycle_event_acks_dir.exists():
            for path in _glob_nested_json(self.lifecycle_event_acks_dir):
                ack = self._read_json(path)
                if set(ack) != {"schema_version", "event_id", "consumer", "acked_at"}:
                    raise ControllerError("INVALID_METADATA", "lifecycle event ack is invalid")
                if ack.get("schema_version") != LIFECYCLE_EVENT_SCHEMA_VERSION:
                    raise ControllerError(
                        "INVALID_METADATA", "lifecycle event ack version is invalid"
                    )
                event_id = _optional_uuid(ack.get("event_id"), "event_id")
                consumer = _nonempty_string(ack.get("consumer"), "consumer")
                if event_id is None or path.stem != event_id:
                    raise ControllerError(
                        "INVALID_METADATA", "lifecycle event ack identity is invalid"
                    )
                if path.parent.name != hashlib.sha256(consumer.encode("utf-8")).hexdigest():
                    raise ControllerError(
                        "INVALID_METADATA", "lifecycle event ack consumer differs"
                    )
                _validate_timestamp(ack.get("acked_at"), "acked_at")

        if self.publication_receipts_dir.exists():
            publication_receipts = self._history(
                self.publication_receipts_dir, publication_receipt_from_dict,
                lambda receipt: receipt.receipt_id, "publication receipt"
            )

        if self.release_intents_dir.exists():
            for path in _glob_json(self.release_intents_dir):
                intent = self._decoded(path, release_intent_from_dict)
                if path.stem != intent.intent_id or intent.intent_id in release_intents:
                    raise ControllerError("INVALID_METADATA", "release intent filename is invalid")
                release_intents[intent.intent_id] = intent

        if self.release_plans_dir.exists():
            release_plans = self._history(
                self.release_plans_dir, release_plan_from_dict, lambda plan: plan.plan_id,
                "release plan"
            )

        if self.release_receipts_dir.exists():
            release_receipts = self._history(
                self.release_receipts_dir, release_receipt_from_dict,
                lambda receipt: receipt.receipt_id, "release receipt"
            )

        if self.reconciliation_receipts_dir.exists():
            for path in _glob_json(self.reconciliation_receipts_dir):
                receipt = self._decoded(path, reconciliation_receipt_from_dict)
                if path.stem != receipt.receipt_id or receipt.receipt_id in reconciliation_receipts:
                    raise ControllerError(
                        "INVALID_METADATA", "reconciliation receipt filename is invalid"
                    )
                reconciliation_receipts[receipt.receipt_id] = receipt

        if self.teardown_receipts_dir.exists():
            for path in _glob_json(self.teardown_receipts_dir):
                receipt = validate_teardown_receipt(self._read_json(path))
                if path.stem != receipt["receipt_id"]:
                    raise ControllerError(
                        "INVALID_METADATA", f"teardown receipt filename is invalid: {path}"
                    )

        writer_leases: dict[str, WriterLease] = {}
        if self.writer_leases_dir.exists():
            for path in _glob_json(self.writer_leases_dir):
                value = self._read_json(path)
                if self._migration_view and value.get("schema_version") == 1:
                    from .activation_layout import lease_bytes

                    # The explicit, locked layout preflight converts a read view
                    # only. Normal runtime parsing remains schema 2; the actual
                    # lease bytes and all managed-runtime gates remain unchanged.
                    value = json.loads(lease_bytes(json.dumps(value).encode()))
                lease = writer_lease_from_dict(value)
                if path.stem != lease.allocation_id or lease.allocation_id in writer_leases:
                    raise ControllerError("INVALID_METADATA", "writer lease filename is invalid")
                writer_leases[lease.allocation_id] = lease

        return MetadataSnapshot(
            repository_id=repository_id,
            records=records,
            bindings=bindings,
            integration_intents=integration_intents,
            prepared_candidate_receipts=prepared_candidate_receipts,
            integration_reconciliation_receipts=integration_reconciliation_receipts,
            release_intents=release_intents,
            release_plans=release_plans,
            publication_lease=publication_lease,
            publication_receipts=publication_receipts,
            release_receipts=release_receipts,
            reconciliation_receipts=reconciliation_receipts,
            writer_leases=writer_leases,
            warnings=tuple(warnings),
        )

    def load(self) -> MetadataSnapshot:
        with self.lock(exclusive=False):
            return self._load_unlocked()

    @property
    def common_dir(self) -> Path:
        return self.root.parent.parent

    def _registration_admin(self, worktree_key: str, admin_dir: object) -> Path:
        """The Git administrative directory that derives ``worktree_key``."""
        parts = PurePosixPath(admin_dir).parts if isinstance(admin_dir, str) else ()
        if (
            len(parts) != 2
            or parts[0] != "worktrees"
            or parts[1] in {".", ".."}
            or admin_dir != "/".join(parts)
        ):
            raise ControllerError("INVALID_METADATA", "registration admin_dir is invalid")
        digest = hashlib.sha256(str(admin_dir).encode("utf-8")).hexdigest()
        if worktree_key != f"wt_{digest}":
            raise ControllerError("INVALID_METADATA", "registration admin_dir differs from its key")
        return self.common_dir / parts[0] / parts[1]

    def _registrations(self) -> dict[str, dict[str, str]]:
        registrations: dict[str, dict[str, str]] = {}
        for path in _glob_json(self.binding_registrations_dir):
            value = self._read_json(path)
            if (
                set(value) != {"schema_version", "contract", "allocation_id", "admin_dir", "token"}
                or value.get("schema_version") not in SUPPORTED_METADATA_SCHEMA_VERSIONS
                or value.get("contract") != REGISTRATION_CONTRACT
            ):
                raise ControllerError("INVALID_METADATA", f"registration schema is invalid: {path}")
            key = path.stem
            if not key.startswith("wt_") or len(key) != 67:
                raise ControllerError("INVALID_METADATA", f"registration filename is invalid: {path}")
            self._registration_admin(key, value.get("admin_dir"))
            registrations[key] = {
                "allocation_id": _nonempty_string(value.get("allocation_id"), "allocation_id"),
                "admin_dir": str(value["admin_dir"]),
                "token": str(_optional_uuid(value.get("token"), "token") or ""),
            }
            if not registrations[key]["token"]:
                raise ControllerError("INVALID_METADATA", f"registration token is missing: {path}")
        return registrations

    def _marker_matches(self, admin: Path, expected: Mapping[str, str]) -> bool:
        marker = admin / REGISTRATION_MARKER
        try:
            descriptor = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            return False
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return False
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read(_REGISTRATION_MARKER_LIMIT + 1)
        except OSError:
            return False
        finally:
            os.close(descriptor)
        if len(raw) > _REGISTRATION_MARKER_LIMIT:
            return False
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return False
        return value == {
            "contract": REGISTRATION_CONTRACT,
            "allocation_id": expected["allocation_id"],
            "token": expected["token"],
        }

    def _drifted_bindings(self, bindings: Mapping[str, str]) -> dict[str, str]:
        """Bindings whose live Git registration is not the one they were written for.

        A binding without a matching registration document predates registration
        identity (or belongs to the canonical checkout) and keeps its previous
        meaning. An administrative directory that no longer exists is the existing
        orphan-binding case, which interrupted release recovery still relies on.
        """
        drifted: dict[str, str] = {}
        for key, registration in self._registrations().items():
            allocation_id = bindings.get(key)
            if allocation_id is None or allocation_id != registration["allocation_id"]:
                continue  # Stale document from an older runtime's release; not authority.
            admin = self._registration_admin(key, registration["admin_dir"])
            if not os.path.lexists(admin):
                continue
            if not self._marker_matches(admin, registration):
                drifted[key] = allocation_id
        return drifted

    def _write_registration(self, worktree_key: str, allocation_id: str, admin: Path) -> None:
        try:
            relative = admin.relative_to(self.common_dir).as_posix()
        except ValueError as exc:
            raise ControllerError(
                "INVALID_METADATA", "registration is outside the Git common directory"
            ) from exc
        self._registration_admin(worktree_key, relative)
        if admin.is_symlink() or not admin.is_dir():
            raise ControllerError("BLOCK_DRIFT", "Git worktree administrative directory is missing")
        token = str(uuid.uuid4())
        self._atomic_json(
            admin / REGISTRATION_MARKER,
            {"contract": REGISTRATION_CONTRACT, "allocation_id": allocation_id, "token": token},
        )
        self._atomic_json(
            self.binding_registrations_dir / f"{worktree_key}.json",
            {
                "schema_version": METADATA_SCHEMA_VERSION,
                "contract": REGISTRATION_CONTRACT,
                "allocation_id": allocation_id,
                "admin_dir": relative,
                "token": token,
            },
        )

    def initialize_repository(self) -> str:
        if self.repo_file.exists():
            snapshot = self._load_unlocked()
            if snapshot.repository_id is None:
                raise ControllerError("INVALID_METADATA", "repo.json is missing")
            return snapshot.repository_id
        timestamp = now_iso()
        repository_id = str(uuid.uuid4())
        self._atomic_json(
            self.repo_file,
            {
                "schema_version": METADATA_SCHEMA_VERSION,
                "repository_id": repository_id,
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )
        return repository_id

    def write_record(
        self, worktree_key: str, record: MetadataRecord, *, registration: Path | None = None
    ) -> None:
        """Bind ``record`` to a worktree key.

        ``registration`` is the linked worktree's Git administrative directory. When
        given, the binding is tied to that exact registration before it is written,
        so a later registration reusing the name cannot inherit it.
        """
        if not worktree_key.startswith("wt_") or len(worktree_key) != 67:
            raise ControllerError("INVALID_METADATA", "invalid worktree key")
        # One chokepoint for every new governed occupancy, so a retirement grant
        # cannot be re-occupied by any verb that funnels through here.  Updating a
        # record whose binding already exists is not occupancy and stays allowed.
        if not (self.bindings_dir / f"{worktree_key}.json").exists():
            # A record carries no branch, so the fence here is the allocation half;
            # the branch half is refused by acquire's own BRANCH_EXISTS rule and
            # caught at settle by the grant's observation vector.
            from .branch_retirement import retirement_occupancy_blocker

            blocked = retirement_occupancy_blocker(self, allocation_id=record.allocation_id)
            if blocked is not None:
                raise blocked
        if registration is not None:
            # Written before the binding: an interruption leaves an unreferenced
            # registration, never a binding that claims an unverified registration.
            self._write_registration(worktree_key, record.allocation_id, registration)
        self._atomic_json(self.records_dir / f"{record.allocation_id}.json", record_to_dict(record))
        self._atomic_json(
            self.bindings_dir / f"{worktree_key}.json",
            {
                "schema_version": METADATA_SCHEMA_VERSION,
                "allocation_id": record.allocation_id,
            },
        )
        repo = dict(self._read_json(self.repo_file))
        repo["schema_version"] = METADATA_SCHEMA_VERSION
        repo["updated_at"] = now_iso()
        self._atomic_json(self.repo_file, repo)

    def replace_record(self, record: MetadataRecord) -> None:
        path = self.records_dir / f"{record.allocation_id}.json"
        if not path.is_file():
            raise ControllerError("BLOCK_DRIFT", "metadata record disappeared")
        self._atomic_json(path, record_to_dict(record))
        self.touch_repository()

    def remove_binding(self, worktree_key: str) -> None:
        path = self.bindings_dir / f"{worktree_key}.json"
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise ControllerError("BLOCK_DRIFT", "metadata binding disappeared") from exc
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        registration = self.binding_registrations_dir / f"{worktree_key}.json"
        if registration.exists():
            # After the binding: an interruption leaves a document no binding uses.
            registration.unlink()
            directory = os.open(registration.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        self.touch_repository()

    def touch_repository(self) -> None:
        repo = dict(self._read_json(self.repo_file))
        repo["schema_version"] = METADATA_SCHEMA_VERSION
        repo["updated_at"] = now_iso()
        self._atomic_json(self.repo_file, repo)

    def write_writer_lease(self, lease: WriterLease) -> None:
        """Persist one writer lease, keyed by allocation so single-writer is structural."""
        self.writer_leases_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_json(
            self.writer_leases_dir / f"{lease.allocation_id}.json", writer_lease_to_dict(lease)
        )

    def remove_writer_lease(self, allocation_id: str) -> bool:
        path = self.writer_leases_dir / f"{allocation_id}.json"
        if not path.exists():
            return False
        path.unlink()
        return True

    def write_writer_receipt(self, receipt: WriterReconciliationReceipt) -> None:
        self.writer_receipts_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_json(
            self.writer_receipts_dir / f"{receipt.receipt_id}.json", writer_receipt_to_dict(receipt)
        )

    def write_publication_lease(self, lease: PublicationLease) -> None:
        if self.publication_lease_file.exists():
            raise ControllerError("PUBLICATION_LEASE_HELD", "main publication lease is occupied")
        self._atomic_json(self.publication_lease_file, publication_lease_to_dict(lease))
        self.touch_repository()

    def write_integration_intent(self, intent: IntegrationIntent) -> None:
        path = self.integration_intents_dir / f"{intent.intent_id}.json"
        if path.exists():
            raise ControllerError("INTENT_EXISTS", "integration intent already exists")
        self._atomic_json(path, integration_intent_to_dict(intent))
        self.touch_repository()

    def replace_integration_intent(self, intent: IntegrationIntent) -> None:
        path = self.integration_intents_dir / f"{intent.intent_id}.json"
        if not path.is_file():
            raise ControllerError("BLOCK_DRIFT", "integration intent disappeared")
        self._atomic_json(path, integration_intent_to_dict(intent))
        self.touch_repository()

    def write_prepared_candidate_receipt(self, receipt: PreparedCandidateReceipt) -> None:
        path = self.prepared_candidate_receipts_dir / f"{receipt.receipt_id}.json"
        if path.exists():
            current = prepared_candidate_receipt_from_dict(self._read_json(path))
            if current != receipt:
                raise ControllerError("RECEIPT_MISMATCH", "prepared receipt already differs")
            return
        self._atomic_json(path, prepared_candidate_receipt_to_dict(receipt))
        self.touch_repository()

    def write_integration_reconciliation_receipt(
        self, receipt: IntegrationReconciliationReceipt
    ) -> None:
        path = self.integration_reconciliation_receipts_dir / f"{receipt.receipt_id}.json"
        if path.exists():
            current = integration_reconciliation_receipt_from_dict(self._read_json(path))
            if current != receipt:
                raise ControllerError(
                    "RECEIPT_MISMATCH", "integration reconciliation receipt already differs"
                )
            return
        self._atomic_json(path, integration_reconciliation_receipt_to_dict(receipt))
        self.touch_repository()

    def write_lifecycle_event(self, event: LifecycleEvent) -> None:
        path = self.lifecycle_events_dir / f"{event.event_id}.json"
        if path.exists():
            current = lifecycle_event_from_dict(self._read_json(path))
            if current != event:
                raise ControllerError("EVENT_MISMATCH", "lifecycle event already differs")
            return
        self._atomic_json(path, lifecycle_event_to_dict(event))
        self.touch_repository()

    def _event_ack_path(self, consumer: str, event_id: str) -> Path:
        normalized = _nonempty_string(consumer, "consumer")
        if len(normalized) > 256:
            raise ControllerError("INVALID_CONSUMER", "event consumer is too long")
        directory = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return self.lifecycle_event_acks_dir / directory / f"{event_id}.json"

    def list_lifecycle_events(self, consumer: str) -> tuple[LifecycleEvent, ...]:
        with self.lock(exclusive=False):
            events = (
                [
                    lifecycle_event_from_dict(self._read_json(path))
                    for path in _glob_json(self.lifecycle_events_dir)
                ]
                if self.lifecycle_events_dir.exists()
                else []
            )
            return tuple(
                event
                for event in sorted(events, key=lambda item: (item.sequence, item.event_id))
                if not self._event_ack_path(consumer, event.event_id).exists()
            )

    def acknowledge_lifecycle_event(self, consumer: str, event_id: str) -> bool:
        try:
            uuid.UUID(event_id)
        except ValueError as exc:
            raise ControllerError("INVALID_EVENT", "event id must be a UUID") from exc
        event_path = self.lifecycle_events_dir / f"{event_id}.json"
        if not event_path.is_file():
            raise ControllerError("MISSING_EVENT", "lifecycle event is missing")
        lifecycle_event_from_dict(self._read_json(event_path))
        path = self._event_ack_path(consumer, event_id)
        if path.exists():
            ack = self._read_json(path)
            if ack.get("event_id") != event_id or ack.get("consumer") != consumer.strip():
                raise ControllerError("EVENT_ACK_MISMATCH", "event acknowledgement differs")
            return False
        self._atomic_json(
            path,
            {
                "schema_version": LIFECYCLE_EVENT_SCHEMA_VERSION,
                "event_id": event_id,
                "consumer": consumer.strip(),
                "acked_at": now_iso(),
            },
        )
        self.touch_repository()
        return True

    def write_release_intent(self, intent: ReleaseIntent) -> None:
        path = self.release_intents_dir / f"{intent.intent_id}.json"
        if path.exists():
            current = release_intent_from_dict(self._read_json(path))
            if current != intent:
                raise ControllerError("INTENT_MISMATCH", "release intent already differs")
            return
        self._atomic_json(path, release_intent_to_dict(intent))
        self.touch_repository()

    def replace_release_intent(self, intent: ReleaseIntent) -> None:
        path = self.release_intents_dir / f"{intent.intent_id}.json"
        if not path.is_file():
            raise ControllerError("BLOCK_DRIFT", "release intent disappeared")
        self._atomic_json(path, release_intent_to_dict(intent))
        self.touch_repository()

    def write_release_plan(self, plan: ReleasePlan) -> None:
        path = self.release_plans_dir / f"{plan.plan_id}.json"
        if path.exists():
            current = release_plan_from_dict(self._read_json(path))
            if current != plan:
                raise ControllerError("PLAN_MISMATCH", "release plan already differs")
            return
        self._atomic_json(path, release_plan_to_dict(plan))
        self.touch_repository()

    def remove_release_plan(self, plan_id: str) -> bool:
        path = self.release_plans_dir / f"{plan_id}.json"
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    def write_publication_receipt(self, receipt: PublicationReceipt) -> None:
        path = self.publication_receipts_dir / f"{receipt.receipt_id}.json"
        if path.exists():
            current = publication_receipt_from_dict(self._read_json(path))
            if current != receipt:
                raise ControllerError("RECEIPT_MISMATCH", "publication receipt already differs")
            return
        self._atomic_json(path, publication_receipt_to_dict(receipt))
        self.touch_repository()

    def write_release_receipt(self, receipt: ReleaseReceipt) -> None:
        path = self.release_receipts_dir / f"{receipt.receipt_id}.json"
        if path.exists():
            current = release_receipt_from_dict(self._read_json(path))
            if current != receipt:
                raise ControllerError("RECEIPT_MISMATCH", "release receipt already differs")
            return
        self._atomic_json(path, release_receipt_to_dict(receipt))
        self.touch_repository()

    def write_registration_prune_receipt(self, receipt_id: str, payload: Mapping[str, Any]) -> None:
        path = self.registration_prune_receipts_dir / f"{receipt_id}.json"
        if path.exists():
            if dict(self._read_json(path)) != dict(payload):
                raise ControllerError("RECEIPT_MISMATCH", "prune receipt already differs")
            return
        self._atomic_json(path, payload)
        self.touch_repository()

    def write_reconciliation_receipt(self, receipt: ReconciliationReceipt) -> None:
        path = self.reconciliation_receipts_dir / f"{receipt.receipt_id}.json"
        if path.exists():
            current = reconciliation_receipt_from_dict(self._read_json(path))
            if current != receipt:
                raise ControllerError("RECEIPT_MISMATCH", "reconciliation receipt already differs")
            return
        self._atomic_json(path, reconciliation_receipt_to_dict(receipt))
        self.touch_repository()

    def remove_publication_lease(self, lease_id: str) -> None:
        if not self.publication_lease_file.is_file():
            raise ControllerError("BLOCK_DRIFT", "publication lease disappeared")
        current = publication_lease_from_dict(self._read_json(self.publication_lease_file))
        if current.lease_id != lease_id:
            raise ControllerError("LEASE_MISMATCH", "publication lease id changed")
        self.publication_lease_file.unlink()
        directory = os.open(self.publication_lease_file.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        self.touch_repository()

    def remove_branch_retirement_authorization(self, authorization_id: str) -> None:
        path = self.branch_retirement_authorizations_dir / f"{authorization_id}.json"
        if not path.is_file():
            raise ControllerError("BLOCK_DRIFT", "retirement authorization disappeared")
        path.unlink()
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        self.touch_repository()

    def write_transaction(self, transaction_id: str, payload: Mapping[str, Any]) -> None:
        try:
            uuid.UUID(transaction_id)
        except ValueError as exc:
            raise ControllerError("INVALID_TRANSACTION", "transaction id must be a UUID") from exc
        self._atomic_json(self.transactions_dir / f"{transaction_id}.json", payload)

    def write_teardown_receipt(self, receipt_id: str, payload: Mapping[str, Any]) -> None:
        try:
            uuid.UUID(receipt_id)
        except ValueError as exc:
            raise ControllerError("INVALID_METADATA", "teardown receipt id must be a UUID") from exc
        validated = validate_teardown_receipt(payload)
        if validated["receipt_id"] != receipt_id:
            raise ControllerError("INVALID_METADATA", "teardown receipt filename does not match")
        self._atomic_json(self.teardown_receipts_dir / f"{receipt_id}.json", validated)

    def write_completion_receipt(self, receipt_id: str, payload: Mapping[str, Any]) -> None:
        try:
            uuid.UUID(receipt_id)
        except ValueError as exc:
            raise ControllerError(
                "INVALID_METADATA", "completion receipt id must be a UUID"
            ) from exc
        if payload.get("receipt_id") != receipt_id:
            raise ControllerError("INVALID_METADATA", "completion receipt id does not match")
        self._atomic_json(self.completion_receipts_dir / f"{receipt_id}.json", dict(payload))

    def load_teardown_receipt(self, receipt_id: str) -> dict[str, Any]:
        try:
            uuid.UUID(receipt_id)
        except ValueError as exc:
            raise ControllerError("INVALID_METADATA", "teardown receipt id must be a UUID") from exc
        path = self.teardown_receipts_dir / f"{receipt_id}.json"
        if not path.is_file():
            raise ControllerError("BLOCK_DRIFT", "teardown receipt disappeared")
        receipt = validate_teardown_receipt(self._read_json(path))
        if receipt["receipt_id"] != receipt_id:
            raise ControllerError("INVALID_METADATA", "teardown receipt filename does not match")
        return receipt

    def remove_transaction(self, transaction_id: str) -> None:
        path = self.transactions_dir / f"{transaction_id}.json"
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise ControllerError("BLOCK_DRIFT", "transaction journal disappeared") from exc
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def new_record(
    *,
    task_key: str,
    authority_key: str,
    module: str,
    role: str,
    owner: str,
    lifecycle: str,
    base_commit: str | None = None,
    task_commit: str | None = None,
    scope_paths: tuple[str, ...] = (),
    source_allocations: tuple[str, ...] = (),
    external_bindings: tuple[str, ...] = (),
    initial_event: str = "ADOPTED",
) -> MetadataRecord:
    timestamp = now_iso()
    event = {
        "at": timestamp,
        "event": initial_event,
        "from": None,
        "to": lifecycle,
        "owner": owner,
    }
    return record_from_dict(
        {
            "schema_version": METADATA_SCHEMA_VERSION,
            "record_version": 1,
            "allocation_id": str(uuid.uuid4()),
            "task_key": task_key,
            "authority_key": authority_key,
            "module": module,
            "role": role,
            "owner": owner,
            "lifecycle": lifecycle,
            "created_at": timestamp,
            "last_reviewed_at": timestamp,
            "handoff_commit": None,
            "finalizer_record_id": None,
            "base_commit": base_commit,
            "task_commit": task_commit,
            "finalizer_commit": None,
            "scope_paths": list(scope_paths),
            "source_allocations": list(source_allocations),
            "quarantine_expected_commit": None,
            "quarantine_observed_commit": None,
            "quarantine_reason": None,
            "recovery_allocation_id": None,
            "recovery_ref": None,
            "disposable_resources": [],
            "external_bindings": list(external_bindings),
            "history": [event],
        }
    )
