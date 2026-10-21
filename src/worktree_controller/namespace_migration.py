"""Idempotent, per-repository migration of the legacy metadata namespace.

The historical namespace was ``<git-common-dir>/codex-worktree/v1``; authority today is
``<git-common-dir>/worktree-controller/v1``.  Nothing in the runtime discovers a
namespace, so a repository left holding only the legacy tree reads as unmanaged and its
real state stays invisible.  This module is the only supported way to move it: a caller
never copies, edits or deletes namespace JSON by hand.

Two properties make the transition safe to start, stop and repeat:

* the critical section fences on the *destination* repository lock, which is the lock
  every bootstrapping verb (``adopt``, ``acquire``, ``integration-enqueue``) already
  takes, so a concurrent verb queues behind the migration instead of minting a second,
  unrelated repository identity beside it;
* the plan identity is derived from the **merged** content of both namespaces, which a
  move does not change.  The same plan id therefore survives an interrupt, so a replay
  re-presents the original receipt instead of a new migration.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from . import metadata_relocation as relocation
from . import preservation as p
from .activation_layout import LayoutError, lease_bytes, quiet
from .git_backend import inventory
from .models import ControllerError
from .store import ROOT_NAME, VERSION_NAME, MetadataStore, now_iso

LEGACY_ROOT_NAME = "codex-worktree"
CONTRACT = "namespace-migration/v1"
SCHEMA_VERSION = 1
RECEIPTS_DIRECTORY = "namespace-migrations"
LOCK_NAME = "repo.lock"
AUTHORITY_FILE = "repo.json"
LEASE_DIRECTORY = "writer-leases"
POLICY_PARTS = (".agents", "worktree-policy.toml")
LEGACY_POLICY_PARTS = (".codex", "worktree-policy.toml")

CURRENT = "NAMESPACE_ALREADY_CURRENT"
MIGRATED = "NAMESPACE_MIGRATED"
CONVERGED = "NAMESPACE_CONVERGED"
REQUIRED = "NAMESPACE_MIGRATION_REQUIRED"
CONVERGENCE = "NAMESPACE_CONVERGENCE_REQUIRED"
UNMANAGED = "NAMESPACE_UNMANAGED"

MOVED = "moved"
REDUNDANT = "redundant"
DESTINATION_ONLY = "destination-only"
REWRITE = "rewrite"
MUTABLE_AFTER_SWITCH = frozenset(
    {"records", "bindings", "writer-leases", "integration-intents", "release-intents"}
)

IDENTITY_FIELDS = (
    "contract",
    "schema_version",
    "repository_id",
    "repository_path",
    "common_dir",
    "legacy_root",
    "current_root",
    "merged_digest",
    "policy_target",
    "policy_sha256",
)
FAULT_POINTS = (
    "namespace_receipt_prepared",
    "namespace_conversion_temp_partial",
    "namespace_converted_lease_published",
    "namespace_rewrite_temp_fsynced",
    "namespace_destination_rewritten",
    "namespace_entries_moved",
    "namespace_switched",
    "namespace_record_written",
    "namespace_policy_moved",
    "namespace_receipt_settled",
)


def _blocked(code: str, message: str) -> ControllerError:
    return ControllerError(code, message)


def _crash_point(name: str) -> None:
    """The shared fault-injection hook, so a torn migration is reproducible."""

    from .lifecycle import _crash_point as hook

    hook(name)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _is_temp(name: str) -> bool:
    """An interrupted ``store._atomic_json`` write: ``.<target>.<8>.tmp``.

    The Controller itself declares these ignored residue, so a migration carries them
    with the namespace instead of refusing to move a repository over a dead scratch file.
    """

    return re.fullmatch(r"\..+\.json\.[A-Za-z0-9_-]{8}\.tmp", name) is not None


def _relative(value: str) -> str:
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise _blocked("NAMESPACE_PATH_INVALID", f"unsafe namespace-relative path: {value!r}")
    return value


def _namespace(value: str) -> str:
    if (
        value in {ROOT_NAME, LEGACY_ROOT_NAME}
        and value.isascii()
        and not value.startswith(".")
        and "/" not in value
    ):
        return value
    raise _blocked(
        "NAMESPACE_UNSUPPORTED",
        f"only the released namespace names are migratable, got {value!r}",
    )


def _tree(
    root: Path, *, skip_names: frozenset[str] = frozenset(), skip_prefixes: tuple[str, ...] = ()
) -> dict[str, bytes]:
    """Every metadata document in one namespace, keyed by relative POSIX path.

    Anything the Controller would never write - a symlink, a socket, an unparseable
    document, a file with no understood role - fails the migration closed: moving it
    would silently change what the historical evidence means.
    """

    if root.is_symlink():
        raise _blocked("NAMESPACE_EVIDENCE_INVALID", f"namespace root is a symlink: {root}")
    if not root.is_dir():
        return {}
    found: dict[str, bytes] = {}
    for directory, names, files in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in sorted(names):
            path = base / name
            if path.is_symlink():
                raise _blocked(
                    "NAMESPACE_EVIDENCE_INVALID",
                    f"namespace contains a symlinked directory: {path}",
                )
        for name in sorted(files):
            path = base / name
            relative = _relative(path.relative_to(root).as_posix())
            if relative in skip_names or relative.startswith(skip_prefixes):
                continue
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                raise _blocked(
                    "NAMESPACE_EVIDENCE_INVALID", f"namespace contains a symlink: {path}"
                )
            if not stat.S_ISREG(info.st_mode):
                raise _blocked(
                    "NAMESPACE_EVIDENCE_INVALID", f"namespace contains a non-regular entry: {path}"
                )
            if _is_temp(name):
                continue
            try:
                raw = path.read_bytes()
            except OSError as exc:
                raise _blocked("NAMESPACE_EVIDENCE_INVALID", f"cannot read {path}: {exc}") from exc
            if not name.endswith(".json"):
                raise _blocked(
                    "NAMESPACE_EVIDENCE_INVALID", f"namespace holds an unknown document: {path}"
                )
            try:
                document = json.loads(raw)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise _blocked(
                    "NAMESPACE_EVIDENCE_INVALID", f"corrupt document {path}: {exc}"
                ) from exc
            if not isinstance(document, dict):
                raise _blocked("NAMESPACE_EVIDENCE_INVALID", f"document is not an object: {path}")
            found[relative] = raw
    return found


def _converted(relative: str, raw: bytes) -> bytes:
    """The bytes a legacy document must land as, converting schema-1 writer leases."""

    if not relative.startswith(f"{LEASE_DIRECTORY}/") or not relative.endswith(".json"):
        return raw
    try:
        document = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        return raw
    if document.get("schema_version") != 1:
        return raw
    try:
        converted = lease_bytes(raw)
    except LayoutError as exc:
        raise _blocked("NAMESPACE_LEASE_INVALID", f"{relative}: {exc}") from exc
    if json.loads(converted).get("allocation_id") != Path(relative).stem:
        raise _blocked("NAMESPACE_LEASE_INVALID", f"writer lease filename differs: {relative}")
    return converted


def _merge(
    legacy: dict[str, bytes], current: dict[str, bytes]
) -> tuple[dict[str, tuple[str, bytes]], list[str]]:
    """Prove one authoritative content set, or report every conflict.

    The authoritative bytes for a path are its legacy document in the form the runtime can
    read - for a schema-1 writer lease, that means its converted form.  A destination that
    already holds them makes the source copy redundant debris; a destination holding the
    unconverted twin is rewritten, because a converged namespace nobody can parse is not a
    migration.  Anything else that differs is a conflict: the plan refuses, nothing moves.
    """

    merged: dict[str, tuple[str, bytes]] = {}
    conflicts: list[str] = []
    for relative in sorted(set(legacy) | set(current)):
        left, right = legacy.get(relative), current.get(relative)
        target = _converted(relative, left if left is not None else right)
        if left is not None and right is not None:
            if right == target:
                merged[relative] = (REDUNDANT, target)
            elif left == right:
                merged[relative] = (REWRITE, target)
            else:
                conflicts.append(relative)
            continue
        if right is not None:
            merged[relative] = (REWRITE if right != target else DESTINATION_ONLY, target)
            continue
        merged[relative] = (MOVED, target)
    return merged, conflicts


def _digest(content: dict[str, bytes]) -> str:
    return p._sha({relative: _sha256(raw) for relative, raw in sorted(content.items())})


def _authority_id(content: dict[str, bytes]) -> str:
    """The one repository identity a migration may carry forward."""

    raw = content.get(AUTHORITY_FILE)
    if raw is None:
        raise _blocked(
            "NAMESPACE_IDENTITY_MISSING",
            f"no {AUTHORITY_FILE} in either namespace; refusing to guess a repository identity",
        )
    try:
        document = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _blocked("NAMESPACE_EVIDENCE_INVALID", f"corrupt {AUTHORITY_FILE}: {exc}") from exc
    repository_id = document.get("repository_id")
    if not isinstance(repository_id, str) or not repository_id:
        raise _blocked("NAMESPACE_EVIDENCE_INVALID", f"{AUTHORITY_FILE} carries no repository_id")
    return repository_id


def _unfinished(root: Path, canonical: Path, *, serialized: bool) -> list[dict[str, str]]:
    """Judge unfinished work only from inside the fence, never from a waiting reader.

    Classifying journals correctly requires the namespace's own serialization.  A reader
    that is not inside the critical section would have to take a shared flock and wait an
    unbounded time behind whatever writer holds it - and the plan it produced would still
    be superseded by the re-derivation the migration performs under its exclusive lock.
    So the advisory plan defers this one question to the fence instead of blocking on it.
    """

    if not serialized or not root.is_dir() or not (root / AUTHORITY_FILE).is_file():
        return []
    return _quiet(root, canonical)


def _quiet(root: Path, canonical: Path) -> list[dict[str, str]]:
    try:
        return [
            {
                "transaction_id": str(row.get("transaction_id", "")),
                "state": str(row.get("state", "")),
            }
            for row in quiet(root, canonical)
        ]
    except LayoutError as exc:
        raise _blocked("NAMESPACE_UNSAFE", f"{root}: {exc}") from exc


def _policy_state(repository: Path, requested: Path | None) -> dict[str, Any]:
    """Where the single policy authority must live, and the bytes it must hold.

    Computed from whichever side exists so that moving a policy never changes the plan
    identity: the migration must stay replayable across its own write.  A destination
    that already exists with different content is reported by the conflict check, not
    silently overwritten, because policy is authority and not namespace plumbing.
    """

    destination = repository.joinpath(*POLICY_PARTS)
    source = requested if requested is not None else repository.joinpath(*LEGACY_POLICY_PARTS)
    if destination.parent.is_symlink():
        raise _blocked("NAMESPACE_EVIDENCE_INVALID", "policy destination parent is a symlink")
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink():
            raise _blocked(
                "NAMESPACE_EVIDENCE_INVALID", f"policy authority is a symlink: {destination}"
            )
        return {
            "target": str(destination),
            "sha256": _sha256(destination.read_bytes()),
            "move": None,
        }
    if source.parent.is_symlink():
        raise _blocked("NAMESPACE_EVIDENCE_INVALID", "policy source parent is a symlink")
    if source.is_symlink():
        raise _blocked("NAMESPACE_EVIDENCE_INVALID", f"legacy policy is a symlink: {source}")
    if not source.is_file():
        return {"target": None, "sha256": None, "move": None}
    return {
        "target": str(destination),
        "sha256": _sha256(source.read_bytes()),
        "move": str(source),
    }


def _roots(repository_path: Path, legacy_namespace: str) -> tuple[Path, Path, Path]:
    repository = inventory(repository_path)
    namespace = _namespace(legacy_namespace)
    common = repository.common_dir
    legacy_root = common / namespace / VERSION_NAME
    current_root = common / ROOT_NAME / VERSION_NAME
    if legacy_root == current_root:
        raise _blocked("INVALID_REQUEST", "the legacy namespace is already the current namespace")
    for path in (legacy_root.parent, legacy_root, current_root.parent, current_root):
        if os.path.lexists(path) and (path.is_symlink() or not path.is_dir()):
            raise _blocked(
                "NAMESPACE_EVIDENCE_INVALID", f"namespace path is not a directory: {path}"
            )
    return repository.canonical_path, legacy_root, current_root


def _plan(
    repository_path: Path,
    legacy_namespace: str,
    policy_from: Path | None,
    *,
    serialized: bool = False,
) -> dict[str, Any]:
    canonical, legacy_root, current_root = _roots(repository_path, legacy_namespace)
    legacy_present = legacy_root.is_dir()
    current_present = current_root.is_dir()
    if not legacy_present and not current_present:
        raise _blocked(
            UNMANAGED,
            f"neither {legacy_root} nor {current_root} exists; this repository has no metadata to migrate",
        )
    if legacy_present and (legacy_root / relocation.RECORD).is_file():
        raise _blocked(
            "NAMESPACE_RELOCATION_PRESENT",
            f"{legacy_root} already carries relocation evidence; it is a migration target, "
            "not a source, and must be reconciled by its operator",
        )
    legacy_tree = _tree(legacy_root, skip_names=frozenset({LOCK_NAME})) if legacy_present else {}
    current_tree = (
        _tree(
            current_root,
            skip_names=frozenset({LOCK_NAME, relocation.RECORD}),
            skip_prefixes=(f"{RECEIPTS_DIRECTORY}/",),
        )
        if current_present
        else {}
    )
    merged, conflicts = _merge(legacy_tree, current_tree)
    if conflicts:
        raise _blocked(
            "NAMESPACE_CONFLICT",
            "both namespaces hold differing content for: " + ", ".join(sorted(conflicts)[:12]),
        )
    repository_id = _authority_id({relative: raw for relative, (_, raw) in merged.items()})
    if legacy_present and not (legacy_root / LOCK_NAME).is_file():
        raise _blocked(
            "NAMESPACE_LOCK_MISSING",
            f"{legacy_root / LOCK_NAME} is missing or not a regular file; the legacy namespace "
            "is not a serialized authority and must be repaired by its operator first",
        )
    for root in (legacy_root, current_root):
        for row in _unfinished(root, canonical, serialized=serialized):
            raise _blocked(
                "NAMESPACE_UNSAFE",
                f"unfinished transaction {row['transaction_id']} in {root} blocks migration",
            )
    policy = _policy_state(canonical, policy_from)
    moves = sorted(relative for relative, (kind, _) in merged.items() if kind == MOVED)
    redundant = sorted(relative for relative, (kind, _) in merged.items() if kind == REDUNDANT)
    rewrites = sorted(relative for relative, (kind, _) in merged.items() if kind == REWRITE)
    converted = sorted(
        relative
        for relative in set(moves) | set(redundant) | set(rewrites)
        if relative in legacy_tree and merged[relative][1] != legacy_tree[relative]
    )
    merged_digest = _digest({relative: raw for relative, (_, raw) in merged.items()})
    manifest = {
        relative: {
            "kind": kind,
            "source_sha256": _sha256(legacy_tree[relative]) if relative in legacy_tree else None,
            "current_sha256": _sha256(current_tree[relative]) if relative in current_tree else None,
            "target_sha256": _sha256(raw),
        }
        for relative, (kind, raw) in sorted(merged.items())
    }
    identity = {
        "contract": CONTRACT,
        "schema_version": SCHEMA_VERSION,
        "repository_id": repository_id,
        "repository_path": str(canonical),
        "common_dir": str(legacy_root.parent.parent),
        "legacy_root": str(legacy_root),
        "current_root": str(current_root),
        "merged_digest": merged_digest,
        "policy_target": policy["target"],
        "policy_sha256": policy["sha256"],
    }
    plan_id = p._sha(identity)
    return {
        **identity,
        "plan_id": plan_id,
        "scenario": CONVERGENCE if redundant or rewrites else REQUIRED if moves else CURRENT,
        "moves": moves,
        "redundant": redundant,
        "rewrites": rewrites,
        "legacy_digest": _digest(legacy_tree),
        "current_digest": _digest(current_tree),
        "lease_conversions": converted,
        "preflight": "under_fence" if not serialized else "classified",
        "policy": policy,
        "manifest": manifest,
        "files": {relative: raw for relative, (_, raw) in merged.items()},
        "sources": dict(legacy_tree),
    }


def _scenario_report(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": plan["scenario"],
        "dry_run": True,
        "plan": {key: value for key, value in plan.items() if key not in {"files", "sources"}},
        "receipts": receipts_of(Path(plan["current_root"]), plan["plan_id"]),
    }


def _pending_receipt(current_root: Path) -> dict[str, Any] | None:
    directory = current_root / RECEIPTS_DIRECTORY
    if directory.is_symlink():
        raise _blocked("NAMESPACE_EVIDENCE_INVALID", "migration receipt directory is a symlink")
    if not directory.is_dir():
        return None
    pending: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.prepared.json")):
        plan_id = path.name.removesuffix(".prepared.json")
        if re.fullmatch(r"[0-9a-f]{64}", plan_id) is None:
            raise _blocked("NAMESPACE_EVIDENCE_INVALID", f"invalid prepared receipt name: {path}")
        found = receipts_of(current_root, plan_id)
        if found["settled"] is None and found["prepared"] is not None:
            pending.append(found["prepared"])
    if len(pending) > 1:
        raise _blocked("NAMESPACE_EVIDENCE_INVALID", "more than one unsettled migration exists")
    return pending[0] if pending else None


def _recovery_report(witness: dict[str, Any], current_root: Path) -> dict[str, Any]:
    plan_id = witness.get("plan_id")
    if (
        not isinstance(plan_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", plan_id) is None
        or any(key not in witness for key in IDENTITY_FIELDS)
        or witness.get("phase") != "PREPARED"
        or witness.get("contract") != CONTRACT
        or witness.get("schema_version") != SCHEMA_VERSION
        or witness.get("current_root") != str(current_root)
        or witness.get("scenario") not in {REQUIRED, CONVERGENCE}
    ):
        raise _blocked("NAMESPACE_EVIDENCE_INVALID", "unsettled migration receipt is invalid")
    identity = {key: witness[key] for key in IDENTITY_FIELDS}
    if p._sha(identity) != plan_id:
        raise _blocked("NAMESPACE_EVIDENCE_INVALID", "unsettled migration identity differs")
    manifest = witness.get("manifest")
    if manifest is not None and (
        not isinstance(manifest, dict)
        or AUTHORITY_FILE not in manifest
        or any(
            not isinstance(relative, str)
            or not isinstance(entry, dict)
            or entry.get("kind") not in {MOVED, REDUNDANT, DESTINATION_ONLY, REWRITE}
            for relative, entry in manifest.items()
        )
    ):
        raise _blocked("NAMESPACE_EVIDENCE_INVALID", "unsettled migration manifest is invalid")
    manifest = manifest or {}
    return {
        "code": witness["scenario"],
        "dry_run": True,
        "recovery_required": True,
        "plan": {
            **{key: witness.get(key) for key in IDENTITY_FIELDS},
            "plan_id": plan_id,
            "scenario": witness["scenario"],
            "moves": sorted(r for r, e in manifest.items() if e.get("kind") == MOVED),
            "redundant": sorted(r for r, e in manifest.items() if e.get("kind") == REDUNDANT),
            "rewrites": sorted(r for r, e in manifest.items() if e.get("kind") == REWRITE),
            "lease_conversions": witness.get("lease_conversions", []),
            "policy": witness.get("policy"),
            "preflight": "under_fence",
        },
        "receipts": receipts_of(current_root, plan_id),
    }


def plan_namespace_migration(
    repository_path: Path,
    *,
    legacy_namespace: str = LEGACY_ROOT_NAME,
    policy_from: Path | None = None,
) -> dict[str, Any]:
    """Report what one migration would do, without writing anything."""

    _, _, current_root = _roots(repository_path, legacy_namespace)
    pending = _pending_receipt(current_root)
    decision = (
        _recovery_report(pending, current_root)
        if pending is not None
        else _scenario_report(_plan(repository_path, legacy_namespace, policy_from))
    )
    return _payload("namespace-migration-plan", decision)


def receipts_of(root: Path, plan_id: str) -> dict[str, dict[str, Any] | None]:
    directory = root / RECEIPTS_DIRECTORY
    found: dict[str, dict[str, Any] | None] = {"prepared": None, "settled": None}
    for name, key in ((f"{plan_id}.prepared.json", "prepared"), (f"{plan_id}.json", "settled")):
        path = directory / name
        if os.path.lexists(path):
            if path.is_symlink() or not path.is_file():
                raise _blocked(
                    "NAMESPACE_EVIDENCE_INVALID", f"migration receipt is not regular: {path}"
                )
            try:
                value = json.loads(path.read_bytes())
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise _blocked(
                    "NAMESPACE_EVIDENCE_INVALID", f"migration receipt is unreadable: {path}"
                ) from exc
            if not isinstance(value, dict):
                raise _blocked(
                    "NAMESPACE_EVIDENCE_INVALID", f"migration receipt is not an object: {path}"
                )
            found[key] = value
    return found


def _payload(command: str, decision: dict[str, Any]) -> dict[str, Any]:
    from .controller import envelope

    payload = envelope(command)
    payload["decision"] = decision
    return payload


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_document(path: Path, raw: bytes) -> None:
    """Rewrite content under the interrupted-write name the Controller already ignores."""

    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _crash_point("namespace_rewrite_temp_fsynced")
        os.replace(name, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(name)
        raise
    _fsync_directory(path.parent)


def _write_document(path: Path, raw: bytes) -> None:
    """Publish a complete new document without replacing a concurrent destination."""

    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            middle = len(raw) // 2
            stream.write(raw[:middle])
            stream.flush()
            _crash_point("namespace_conversion_temp_partial")
            stream.write(raw[middle:])
            stream.flush()
            os.fsync(stream.fileno())
        os.link(name, path, follow_symlinks=False)
        _fsync_directory(path.parent)
    except FileExistsError as exc:
        raise _blocked(
            "NAMESPACE_CONFLICT", f"destination appeared while publishing {path}"
        ) from exc
    finally:
        with suppress(FileNotFoundError):
            os.unlink(name)
    _fsync_directory(path.parent)


def _clear_atomic_temps(root: Path) -> None:
    """Discard only ignored atomic JSON scratch while both namespace locks are held."""

    if not root.is_dir():
        return
    for directory, _, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        removed = False
        for name in files:
            if not _is_temp(name):
                continue
            path = parent / name
            if not stat.S_ISREG(os.lstat(path).st_mode):
                raise _blocked(
                    "NAMESPACE_EVIDENCE_INVALID", f"atomic scratch is not regular: {path}"
                )
            path.unlink()
            removed = True
        if removed:
            _fsync_directory(parent)


def _write_receipt(root: Path, name: str, value: dict[str, Any]) -> str:
    """Write-once evidence, tolerant of the replay that finds it already there."""

    with p._absolute_directory(root / RECEIPTS_DIRECTORY, create=True) as chain:
        parent = chain[-1]
        if name in os.listdir(parent.fd):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent.fd)
            try:
                _, digest = p._transfer(descriptor)
            finally:
                os.close(descriptor)
            return digest
        return p._write_once(parent, name, value)


@contextmanager
def _fenced(current_root: Path, legacy_root: Path) -> Iterator[None]:
    """Hold the destination repository lock - and the legacy one when it exists - throughout.

    The destination lock is the arbiter every runtime verb already takes, so claiming it
    before the tree exists is what keeps ``adopt``/``acquire``/``integration-enqueue`` from
    bootstrapping a second identity beside the namespace being moved.  The legacy lock is
    claimed without waiting: a caller that holds it is an activation in flight, and an
    acquire-then-block order here would be the reverse of the lock order that flow uses.
    """

    try:
        if current_root.is_symlink() or legacy_root.is_symlink():
            raise _blocked("NAMESPACE_EVIDENCE_INVALID", "namespace root is a symlink")
        current_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        destination = os.open(
            current_root / LOCK_NAME,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except OSError as exc:
        raise _blocked("NAMESPACE_LOCK_INVALID", f"destination lock is unusable: {exc}") from exc
    legacy_descriptor: int | None = None
    try:
        if not stat.S_ISREG(os.fstat(destination).st_mode):
            raise _blocked(
                "NAMESPACE_LOCK_INVALID", f"destination lock is not a regular file: {current_root}"
            )
        try:
            fcntl.flock(destination, fcntl.LOCK_EX)
        except OSError as exc:
            raise _blocked(
                "NAMESPACE_LOCK_INVALID", f"destination lock cannot be claimed: {exc}"
            ) from exc
        legacy_path = legacy_root / LOCK_NAME
        if legacy_root.is_dir():
            if legacy_path.is_symlink() or not legacy_path.is_file():
                raise _blocked("NAMESPACE_LOCK_MISSING", f"legacy lock is unusable: {legacy_path}")
            try:
                legacy_descriptor = os.open(legacy_path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
            except OSError as exc:
                raise _blocked("NAMESPACE_LOCK_INVALID", f"legacy lock is unusable: {exc}") from exc
            if not stat.S_ISREG(os.fstat(legacy_descriptor).st_mode):
                raise _blocked(
                    "NAMESPACE_LOCK_INVALID", f"legacy lock is not a regular file: {legacy_path}"
                )
            try:
                fcntl.flock(legacy_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise _blocked(
                    "NAMESPACE_BUSY", "another writer holds the legacy namespace lock; retry later"
                ) from exc
            except OSError as exc:
                raise _blocked(
                    "NAMESPACE_LOCK_INVALID", f"legacy lock cannot be claimed: {exc}"
                ) from exc
        yield
    finally:
        if legacy_descriptor is not None:
            fcntl.flock(legacy_descriptor, fcntl.LOCK_UN)
            os.close(legacy_descriptor)
        fcntl.flock(destination, fcntl.LOCK_UN)
        os.close(destination)


def _relocate(
    legacy_root: Path, current_root: Path, plan: dict[str, Any]
) -> tuple[list[str], list[str], list[str]]:
    """Move, rewrite or clear every planned document; each entry is independently replayable."""

    moved: list[str] = []
    rewritten: list[str] = []
    cleared: list[str] = []
    movable = set(plan["moves"])
    for relative in sorted(movable | set(plan["redundant"]) | set(plan["rewrites"])):
        if relative == AUTHORITY_FILE:
            continue
        source, destination = legacy_root / relative, current_root / relative
        expected_source = plan["sources"].get(relative)
        if relative in plan.get("mutable_drift", ()):
            if source.is_file():
                if expected_source is None or _sha256(source.read_bytes()) != _sha256(
                    expected_source
                ):
                    raise _blocked("NAMESPACE_SOURCE_DRIFT", f"legacy content changed: {relative}")
                source.unlink()
            cleared.append(relative)
            continue
        target = plan["files"][relative]
        if destination.is_symlink():
            raise _blocked("NAMESPACE_CONFLICT", f"destination content is a symlink: {relative}")
        resident = destination.read_bytes() if destination.is_file() else None
        if resident is None:
            if not source.is_file() or source.is_symlink():
                raise _blocked(
                    "NAMESPACE_SOURCE_MISSING", f"neither namespace holds {relative} any more"
                )
            if _sha256(source.read_bytes()) != _sha256(expected_source or b""):
                raise _blocked("NAMESPACE_SOURCE_DRIFT", f"legacy content changed: {relative}")
            destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            if source.read_bytes() == target:
                os.rename(source, destination)
            else:
                _write_document(destination, target)
                if relative in plan["lease_conversions"]:
                    _crash_point("namespace_converted_lease_published")
                source.unlink()
            _fsync_directory(destination.parent)
            moved.append(relative)
        elif resident != target:
            if relative in movable or _converted(relative, resident) != target:
                raise _blocked("NAMESPACE_CONFLICT", f"destination content changed: {relative}")
            _replace_document(destination, target)
            _crash_point("namespace_destination_rewritten")
            rewritten.append(relative)
        if source.is_file() and _sha256(source.read_bytes()) == _sha256(expected_source or b""):
            source.unlink()
        cleared.append(relative)
    return moved, rewritten, cleared


def _switch_authority(legacy_root: Path, current_root: Path, plan: dict[str, Any]) -> bool:
    """Make the destination namespace live at exactly one instant.

    Nothing treats a namespace as authoritative without ``repo.json``, so renaming the
    identity document last is the whole cutover - and re-checking its bytes against the
    plan is what proves the tree that became live is the tree that was approved.
    """

    source, destination = legacy_root / AUTHORITY_FILE, current_root / AUTHORITY_FILE
    expected = _sha256(plan["files"][AUTHORITY_FILE])
    if destination.is_symlink():
        raise _blocked("NAMESPACE_CONFLICT", f"{AUTHORITY_FILE} destination is a symlink")
    if destination.is_file():
        if _sha256(destination.read_bytes()) != expected:
            raise _blocked("NAMESPACE_CONFLICT", f"{AUTHORITY_FILE} appeared with other content")
        switched = False
    elif source.is_file() and not source.is_symlink():
        if _sha256(source.read_bytes()) != expected:
            raise _blocked("NAMESPACE_SOURCE_DRIFT", f"{AUTHORITY_FILE} changed during migration")
        os.rename(source, destination)
        _fsync_directory(current_root)
        switched = True
    else:
        raise _blocked(
            "NAMESPACE_IDENTITY_MISSING", f"no {AUTHORITY_FILE} landed in either namespace"
        )
    if _sha256(destination.read_bytes()) != expected:
        raise _blocked(
            "NAMESPACE_IDENTITY_MISMATCH", "the authority document changed mid-migration"
        )
    source.unlink(missing_ok=True)
    return switched


def _prune_legacy(legacy_root: Path) -> bool:
    """Remove the legacy namespace only once it demonstrably holds nothing of ours."""

    if not os.path.lexists(legacy_root):
        return True
    if not legacy_root.is_dir():
        raise _blocked("NAMESPACE_RESIDUE", f"legacy namespace is not a directory: {legacy_root}")
    for directory, _, files in os.walk(legacy_root, topdown=False):
        base = Path(directory)
        for name in sorted(files):
            path = base / name
            if _is_temp(name):
                if not stat.S_ISREG(os.lstat(path).st_mode):
                    raise _blocked("NAMESPACE_RESIDUE", f"atomic scratch is not regular: {path}")
                path.unlink()
                continue
            if path.name != LOCK_NAME or base != legacy_root:
                raise _blocked(
                    "NAMESPACE_RESIDUE",
                    f"legacy namespace still holds {path.relative_to(legacy_root)}",
                )
        if base != legacy_root:
            try:
                base.rmdir()
            except OSError as exc:
                raise _blocked(
                    "NAMESPACE_RESIDUE", f"legacy directory is not empty: {base}"
                ) from exc
    try:
        (legacy_root / LOCK_NAME).unlink(missing_ok=True)
        legacy_root.rmdir()
    except OSError as exc:
        raise _blocked(
            "NAMESPACE_RESIDUE", f"legacy namespace could not be removed: {exc}"
        ) from exc
    parent = legacy_root.parent
    if parent.is_dir() and not any(os.scandir(parent)):
        parent.rmdir()
    _fsync_directory(parent.parent)
    if os.path.lexists(legacy_root):
        raise _blocked("NAMESPACE_RESIDUE", f"legacy namespace remains: {legacy_root}")
    return True


def _evidence_digests(root: Path, plan: dict[str, Any]) -> dict[str, str]:
    """Map only evidence that had a historical path in the legacy namespace."""

    found: dict[str, str] = {}
    for relative, entry in sorted(plan["manifest"].items()):
        if relative.split("/", 1)[0] not in relocation.DIRECTORIES or not entry["source_sha256"]:
            continue
        path = root / relative
        if (
            path.is_symlink()
            or not path.is_file()
            or _sha256(path.read_bytes()) != entry["source_sha256"]
        ):
            raise _blocked("NAMESPACE_SOURCE_DRIFT", f"historical evidence changed: {relative}")
        found[relative] = entry["source_sha256"]
    return found


def _apply_policy(plan: dict[str, Any]) -> dict[str, Any] | None:
    move = plan["policy"]["move"]
    if not move:
        return None
    source, destination = Path(move), Path(plan["policy"]["target"] or "")
    expected = plan["policy"]["sha256"]
    if not source.is_file():
        if (
            destination.is_file()
            and not destination.is_symlink()
            and _sha256(destination.read_bytes()) == expected
        ):
            return {"from": str(source), "to": str(destination), "sha256": expected}
        raise _blocked("NAMESPACE_POLICY_DRIFT", "the legacy policy document is missing")
    if _sha256(source.read_bytes()) != expected:
        raise _blocked("NAMESPACE_POLICY_DRIFT", "the legacy policy document changed")
    if os.path.lexists(destination):
        raise _blocked("NAMESPACE_CONFLICT", f"a policy authority already exists: {destination}")
    destination.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
    os.rename(source, destination)
    _fsync_directory(destination.parent)
    if source.parent.is_dir() and not any(os.scandir(source.parent)):
        source.parent.rmdir()
    return {"from": str(source), "to": str(destination), "sha256": expected}


def _resume_plan(
    repository_path: Path,
    legacy_namespace: str,
    witness: dict[str, Any],
    expected_plan_id: str,
    policy_from: Path | None,
) -> dict[str, Any]:
    """Recover the prepared plan without folding later destination writes into its CAS."""

    canonical, legacy_root, current_root = _roots(repository_path, legacy_namespace)
    if not isinstance(witness, dict) or any(key not in witness for key in IDENTITY_FIELDS):
        raise _blocked("NAMESPACE_EVIDENCE_INVALID", "prepared migration receipt is incomplete")
    identity = {key: witness[key] for key in IDENTITY_FIELDS}
    if (
        witness.get("contract") != CONTRACT
        or witness.get("schema_version") != SCHEMA_VERSION
        or witness.get("phase") != "PREPARED"
        or witness.get("plan_id") != expected_plan_id
        or p._sha(identity) != expected_plan_id
        or witness.get("repository_path") != str(canonical)
        or witness.get("common_dir") != str(legacy_root.parent.parent)
        or witness.get("legacy_root") != str(legacy_root)
        or witness.get("current_root") != str(current_root)
        or witness.get("scenario") not in {REQUIRED, CONVERGENCE}
    ):
        raise _blocked("NAMESPACE_EVIDENCE_INVALID", "prepared migration identity differs")
    manifest = witness.get("manifest")
    if manifest is None:
        # Older prepared receipts had no replay manifest. They remain recoverable
        # only while their original plan still re-derives byte for byte.
        planned = _plan(repository_path, legacy_namespace, policy_from, serialized=True)
        if planned["plan_id"] != expected_plan_id:
            raise _blocked("NAMESPACE_PLAN_DRIFT", "legacy prepared receipt has no replay manifest")
        return planned
    if not isinstance(manifest, dict) or AUTHORITY_FILE not in manifest:
        raise _blocked("NAMESPACE_EVIDENCE_INVALID", "prepared migration manifest is invalid")
    policy = witness.get("policy")
    if (
        not isinstance(policy, dict)
        or policy.get("target") != witness["policy_target"]
        or policy.get("sha256") != witness["policy_sha256"]
        or (policy_from is not None and policy.get("move") not in {None, str(policy_from)})
    ):
        raise _blocked("NAMESPACE_POLICY_DRIFT", "prepared policy authority differs")
    target_digests: dict[str, str] = {}
    for relative, entry in manifest.items():
        _relative(relative)
        if not isinstance(entry, dict) or entry.get("kind") not in {
            MOVED,
            REDUNDANT,
            DESTINATION_ONLY,
            REWRITE,
        }:
            raise _blocked("NAMESPACE_EVIDENCE_INVALID", f"invalid manifest entry: {relative}")
        for key in ("source_sha256", "current_sha256", "target_sha256"):
            value = entry.get(key)
            if value is not None and (
                not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
            ):
                raise _blocked("NAMESPACE_EVIDENCE_INVALID", f"invalid {key}: {relative}")
        if entry.get("target_sha256") is None:
            raise _blocked("NAMESPACE_EVIDENCE_INVALID", f"missing target digest: {relative}")
        target_digests[relative] = entry["target_sha256"]
    if p._sha(target_digests) != witness["merged_digest"]:
        raise _blocked(
            "NAMESPACE_EVIDENCE_INVALID", "prepared manifest does not match plan identity"
        )
    legacy_tree = _tree(legacy_root, skip_names=frozenset({LOCK_NAME}))
    current_tree = _tree(
        current_root,
        skip_names=frozenset({LOCK_NAME, relocation.RECORD}),
        skip_prefixes=(f"{RECEIPTS_DIRECTORY}/",),
    )
    for root in (legacy_root, current_root):
        for row in _unfinished(root, canonical, serialized=True):
            raise _blocked(
                "NAMESPACE_UNSAFE", f"unfinished transaction {row['transaction_id']} in {root}"
            )
    authorities = [
        tree[AUTHORITY_FILE] for tree in (legacy_tree, current_tree) if AUTHORITY_FILE in tree
    ]
    if not authorities or any(
        _authority_id({AUTHORITY_FILE: raw}) != witness["repository_id"] for raw in authorities
    ):
        raise _blocked(
            "NAMESPACE_IDENTITY_MISMATCH", "repository authority differs from prepared receipt"
        )
    switched = AUTHORITY_FILE not in legacy_tree and AUTHORITY_FILE in current_tree
    files: dict[str, bytes] = {}
    sources: dict[str, bytes] = {}
    drifted: set[str] = set()
    for relative, entry in sorted(manifest.items()):
        if entry["kind"] == DESTINATION_ONLY and relative != AUTHORITY_FILE:
            continue
        source, resident = legacy_tree.get(relative), current_tree.get(relative)
        if source is not None:
            if _sha256(source) != entry["source_sha256"]:
                raise _blocked("NAMESPACE_SOURCE_DRIFT", f"legacy content changed: {relative}")
            sources[relative] = source
        if resident is not None and _sha256(resident) == entry["target_sha256"]:
            files[relative] = resident
            continue
        if relative == AUTHORITY_FILE and switched and resident is not None:
            # Once the destination owns the repository, ordinary Controller writes
            # may advance repo.json as well as records. Its repository_id was
            # checked above; replay must keep the live authority document.
            files[relative] = resident
            continue
        candidate = source if source is not None else resident
        if (
            candidate is not None
            and _sha256(_converted(relative, candidate)) == entry["target_sha256"]
            and (resident is None or _sha256(resident) == entry["current_sha256"])
        ):
            files[relative] = _converted(relative, candidate)
            continue
        if (
            switched
            and resident is not None
            and relative.split("/", 1)[0] in MUTABLE_AFTER_SWITCH
            and entry["kind"] != DESTINATION_ONLY
        ):
            drifted.add(relative)
            continue
        raise _blocked(
            "NAMESPACE_CONFLICT", f"prepared document changed or disappeared: {relative}"
        )
    return {
        **identity,
        "plan_id": expected_plan_id,
        "scenario": witness["scenario"],
        "moves": sorted(r for r, e in manifest.items() if e["kind"] == MOVED),
        "redundant": sorted(r for r, e in manifest.items() if e["kind"] == REDUNDANT),
        "rewrites": sorted(r for r, e in manifest.items() if e["kind"] == REWRITE),
        "lease_conversions": witness.get("lease_conversions", []),
        "policy": policy,
        "manifest": manifest,
        "files": files,
        "sources": sources,
        "mutable_drift": drifted,
    }


def _settled_decision(
    settled: dict[str, Any], plan_id: str, legacy_root: Path, current_root: Path
) -> dict[str, Any]:
    if (
        settled.get("contract") != CONTRACT
        or settled.get("schema_version") != SCHEMA_VERSION
        or settled.get("phase") != "SETTLED"
        or settled.get("plan_id") != plan_id
        or settled.get("legacy_root") != str(legacy_root)
        or settled.get("current_root") != str(current_root)
    ):
        raise _blocked("NAMESPACE_EVIDENCE_INVALID", "settled migration receipt differs")
    if os.path.lexists(legacy_root):
        raise _blocked("NAMESPACE_RESIDUE", "legacy namespace reappeared after settlement")
    try:
        relocation._record(current_root)
    except ControllerError as exc:
        raise _blocked(
            "NAMESPACE_EVIDENCE_INVALID", "settled relocation evidence is invalid"
        ) from exc
    return {
        "code": CONVERGED if settled["scenario"] == CONVERGENCE else MIGRATED,
        "idempotent": True,
        "changed": False,
        "plan_id": plan_id,
        "receipt": settled,
    }


def migrate_namespace(
    repository_path: Path,
    *,
    legacy_namespace: str = LEGACY_ROOT_NAME,
    policy_from: Path | None = None,
    actor: str | None = None,
    expected_plan_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Move one repository's metadata namespace, or prove it needs no move.

    Every step is replayable.  The plan identity is the compare-and-set, the prepared
    receipt is written before anything destructive, a document moves only when its
    planned bytes still match on disk, the legacy namespace disappears only when it is
    demonstrably empty, and the relocation record is written only after the old root is
    gone - which is exactly when the frozen reference resolver demands it.
    """

    if dry_run or expected_plan_id is None:
        decision = plan_namespace_migration(
            repository_path, legacy_namespace=legacy_namespace, policy_from=policy_from
        )["decision"]
        return _payload("namespace-migrate", decision)
    if re.fullmatch(r"[0-9a-f]{64}", expected_plan_id) is None:
        raise _blocked("NAMESPACE_PLAN_DRIFT", "expected plan id must be an exact SHA-256")
    _, legacy_root, current_root = _roots(repository_path, legacy_namespace)
    pending = _pending_receipt(current_root)
    if pending is not None and pending.get("plan_id") != expected_plan_id:
        raise _blocked("NAMESPACE_PLAN_DRIFT", "resume the exact unsettled migration plan")
    found = receipts_of(current_root, expected_plan_id)
    if found["settled"] is not None:
        return _payload(
            "namespace-migrate",
            _settled_decision(found["settled"], expected_plan_id, legacy_root, current_root),
        )
    if found["prepared"] is None:
        plan = _plan(repository_path, legacy_namespace, policy_from)
        if expected_plan_id != plan["plan_id"]:
            raise _blocked("NAMESPACE_PLAN_DRIFT", "namespace state changed after the plan")
        if plan["scenario"] == CURRENT:
            return _payload(
                "namespace-migrate",
                {**_scenario_report(plan), "dry_run": False, "idempotent": True, "changed": False},
            )
    if not actor:
        raise _blocked("INVALID_REQUEST", "an actor is required to migrate a namespace")

    with _fenced(current_root, legacy_root):
        found = receipts_of(current_root, expected_plan_id)
        settled = found["settled"]
        if settled is not None:
            return _payload(
                "namespace-migrate",
                _settled_decision(settled, expected_plan_id, legacy_root, current_root),
            )
        witness = found["prepared"]
        planned = (
            _resume_plan(repository_path, legacy_namespace, witness, expected_plan_id, policy_from)
            if witness is not None
            else _plan(repository_path, legacy_namespace, policy_from, serialized=True)
        )
        if planned["plan_id"] != expected_plan_id:
            raise _blocked(
                "NAMESPACE_PLAN_DRIFT", "namespace changed before the transition was fenced"
            )
        plan_id = expected_plan_id
        scenario = witness["scenario"] if witness is not None else planned["scenario"]
        if witness is None:
            _write_receipt(
                current_root,
                f"{plan_id}.prepared.json",
                {
                    "contract": CONTRACT,
                    "schema_version": SCHEMA_VERSION,
                    "phase": "PREPARED",
                    "plan_id": plan_id,
                    "actor": actor,
                    "scenario": scenario,
                    **{key: planned[key] for key in IDENTITY_FIELDS if key != "contract"},
                    "manifest": planned["manifest"],
                    "policy": planned["policy"],
                    "lease_conversions": planned["lease_conversions"],
                    "prepared_at": now_iso(),
                },
            )
        _crash_point("namespace_receipt_prepared")
        moved, rewritten, cleared = _relocate(legacy_root, current_root, planned)
        _clear_atomic_temps(current_root)
        _crash_point("namespace_entries_moved")
        switched = _switch_authority(legacy_root, current_root, planned)
        _crash_point("namespace_switched")
        legacy_removed = _prune_legacy(legacy_root)
        if not legacy_removed or os.path.lexists(legacy_root):
            raise _blocked("NAMESPACE_RESIDUE", "legacy namespace remains after cleanup")
        expected_references = _evidence_digests(current_root, planned)
        if os.path.lexists(current_root / relocation.RECORD):
            try:
                record = relocation._record(current_root)
            except ControllerError as exc:
                raise _blocked(
                    "NAMESPACE_EVIDENCE_INVALID", "relocation record is invalid"
                ) from exc
            if (
                record["previous_root"] != str(legacy_root)
                or record["files"] != expected_references
            ):
                raise _blocked(
                    "NAMESPACE_EVIDENCE_INVALID", "relocation record differs from prepared evidence"
                )
        else:
            record = None
        if record is None:
            identity = os.stat(current_root)
            record = {
                "contract": relocation.CONTRACT,
                "previous_root": str(legacy_root),
                "current_root": str(current_root),
                "root_identity": [identity.st_dev, identity.st_ino],
                "files": expected_references,
            }
            record = {**record, "sha256": p._sha(record)}
            _write_once_document(current_root, relocation.RECORD, record)
        _crash_point("namespace_record_written")
        policy = _apply_policy(planned)
        if policy is not None:
            _crash_point("namespace_policy_moved")
        receipt = {
            "contract": CONTRACT,
            "schema_version": SCHEMA_VERSION,
            "phase": "SETTLED",
            "plan_id": plan_id,
            "actor": actor,
            "scenario": scenario,
            **{key: planned[key] for key in IDENTITY_FIELDS if key != "contract"},
            "moved": sorted(set(moved) | set(planned["moves"])),
            "destination_rewrites": sorted(set(rewritten) | set(planned["rewrites"])),
            "redundant_cleared": cleared,
            "authority_switched": switched,
            "lease_conversions": planned["lease_conversions"],
            "policy": policy,
            "legacy_namespace_removed": legacy_removed,
            "relocation_record_sha256": record["sha256"],
            "manifest": planned["manifest"],
            "settled_at": now_iso(),
        }
        _write_receipt(current_root, f"{plan_id}.json", receipt)
        _crash_point("namespace_receipt_settled")
    MetadataStore(inventory(repository_path).common_dir).load()
    return _payload(
        "namespace-migrate",
        {
            "code": CONVERGED if scenario == CONVERGENCE else MIGRATED,
            "idempotent": False,
            "changed": True,
            "plan_id": plan_id,
            "receipt": receipt,
            "metadata_path": str(current_root),
        },
    )


def _write_once_document(root: Path, name: str, value: dict[str, Any]) -> str:
    with p._absolute_directory(root) as chain:
        return p._write_once(chain[-1], name, value)


def namespace_migration_receipts(repository_path: Path) -> dict[str, Any]:
    """List the immutable migration receipts of one repository."""

    canonical, legacy_root, current_root = _roots(repository_path, LEGACY_ROOT_NAME)
    directory = current_root / RECEIPTS_DIRECTORY
    entries = []
    if os.path.lexists(directory) and (directory.is_symlink() or not directory.is_dir()):
        raise _blocked("NAMESPACE_EVIDENCE_INVALID", "migration receipt directory is invalid")
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                raise _blocked(
                    "NAMESPACE_EVIDENCE_INVALID", f"migration receipt is not regular: {path}"
                )
            try:
                raw = path.read_bytes()
                value = json.loads(raw)
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise _blocked(
                    "NAMESPACE_EVIDENCE_INVALID", f"migration receipt is unreadable: {path}"
                ) from exc
            if not isinstance(value, dict):
                raise _blocked(
                    "NAMESPACE_EVIDENCE_INVALID", f"migration receipt is not an object: {path}"
                )
            entries.append(
                {
                    "name": path.name,
                    "plan_id": value.get("plan_id"),
                    "phase": value.get("phase"),
                    "scenario": value.get("scenario"),
                    "actor": value.get("actor"),
                    "repository_id": value.get("repository_id"),
                    "stamp": value.get("prepared_at") or value.get("settled_at"),
                    "sha256": _sha256(raw),
                }
            )
    return _payload(
        "namespace-migration-receipts",
        {
            "code": "NAMESPACE_RECEIPTS",
            "repository_path": str(canonical),
            "legacy_root": str(legacy_root),
            "directory": str(directory),
            "receipts": entries,
        },
    )


def verify_namespace(
    repository_path: Path, *, legacy_namespace: str = LEGACY_ROOT_NAME
) -> dict[str, Any]:
    """Re-derive a completed migration from immutable evidence instead of trusting a flag.

    A settled receipt must still describe the tree on disk: every path it moved has to be
    present in the destination namespace, the relocation record has to validate against
    this directory's own identity, and the legacy namespace has to be gone - residue
    disables historical reference resolution today.
    """

    canonical, legacy_root, current_root = _roots(repository_path, legacy_namespace)
    problems: list[str] = []
    prepared: dict[str, dict[str, Any]] = {}
    settled: list[dict[str, Any]] = []
    directory = current_root / RECEIPTS_DIRECTORY
    if os.path.lexists(directory) and (directory.is_symlink() or not directory.is_dir()):
        problems.append("RECEIPT_DIRECTORY_INVALID")
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        if path.is_symlink() or not path.is_file():
            problems.append("RECEIPT_SHAPE_INVALID")
            continue
        try:
            value = json.loads(path.read_bytes())
        except (OSError, UnicodeError, json.JSONDecodeError):
            problems.append("RECEIPT_UNREADABLE")
            continue
        if not isinstance(value, dict):
            problems.append("RECEIPT_SHAPE_INVALID")
            continue
        if path.name.endswith(".prepared.json"):
            plan_id = path.name.removesuffix(".prepared.json")
            if value.get("plan_id") != plan_id or value.get("phase") != "PREPARED":
                problems.append("PREPARED_RECEIPT_INVALID")
            prepared[plan_id] = value
            continue
        settled.append(value)
        if value.get("contract") != CONTRACT or value.get("phase") != "SETTLED":
            problems.append("RECEIPT_SHAPE_INVALID")
        if value.get("current_root") != str(current_root) or value.get("legacy_root") != str(
            legacy_root
        ):
            problems.append("RECEIPT_TARGET_DIFFERS")
        authority = current_root / AUTHORITY_FILE
        if not authority.is_file():
            problems.append("AUTHORITY_MISSING")
            continue
        try:
            authority_document = json.loads(authority.read_bytes())
        except (OSError, UnicodeError, json.JSONDecodeError):
            problems.append("AUTHORITY_INVALID")
            continue
        if not isinstance(authority_document, dict):
            problems.append("AUTHORITY_INVALID")
            continue
        if value.get("repository_id") != authority_document.get("repository_id"):
            problems.append("RECEIPT_IDENTITY_DIFFERS")
        moved = value.get("moved")
        if not isinstance(moved, list) or any(not isinstance(item, str) for item in moved):
            problems.append("RECEIPT_SHAPE_INVALID")
            continue
        for relative in moved:
            try:
                _relative(relative)
            except ControllerError:
                problems.append("RECEIPT_SHAPE_INVALID")
                continue
            if not (current_root / relative).is_file():
                problems.append(f"MOVED_DOCUMENT_MISSING:{relative}")
    settled_ids = {item["plan_id"] for item in settled if isinstance(item.get("plan_id"), str)}
    for plan_id in sorted(prepared):
        if plan_id not in settled_ids:
            problems.append(f"PREPARED_UNSETTLED:{plan_id}")
    if os.path.lexists(legacy_root):
        problems.append("LEGACY_NAMESPACE_PRESENT")
    record_state = "absent"
    if (current_root / relocation.RECORD).is_file():
        try:
            relocation._record(current_root)
            record_state = "valid"
        except ControllerError as exc:
            record_state = "invalid"
            problems.append(f"RELOCATION_RECORD:{exc.code}")
    if settled and record_state == "absent":
        problems.append("RELOCATION_RECORD_MISSING")
    return _payload(
        "namespace-migration-verify",
        {
            "code": "NAMESPACE_VERIFIED" if not problems else "NAMESPACE_VERIFY_FAILED",
            "problems": sorted(set(problems)),
            "repository_path": str(canonical),
            "current_root": str(current_root),
            "legacy_root_present": os.path.lexists(legacy_root),
            "relocation_record": record_state,
            "prepared_receipts": [
                {"plan_id": plan_id, "settled": plan_id in settled_ids}
                for plan_id in sorted(prepared)
            ],
            "settled_receipts": [
                {
                    "plan_id": item.get("plan_id"),
                    "scenario": item.get("scenario"),
                    "repository_id": item.get("repository_id"),
                    "moved": len(item["moved"]) if isinstance(item.get("moved"), list) else 0,
                }
                for item in settled
            ],
        },
    )
