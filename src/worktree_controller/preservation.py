"""Explicit legacy state preservation; never a cleanup or release authority.

Payloads are opaque bytes. Only files, directories, symlinks and FIFO nodes are
supported. Source inode identities detect races; portable tree equality excludes
inodes and ownership (ownership is recorded, never restored with chown).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .disposable import DIRECTORY_FLAGS, _Directory, _inode, _open_directory
from .git_backend import head_paths, inventory, path_is_ignored, tracked_paths
from .models import ControllerError
from .store import MetadataStore

CONTRACT = "legacy-preserved-state/v1"
SOURCE = ".venv"
MAX_ENTRIES = 200_000
CHUNK = 1024 * 1024


def _fail(code: str, message: str) -> ControllerError:
    return ControllerError(code, message)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n"
    ).encode()


def _sha(value: object) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _identity(metadata: os.stat_result) -> list[int]:
    return [
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
    ]


def _check_entry(fd: int, name: str, expected: list[int]) -> None:
    if _identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != expected:
        raise _fail("PRESERVATION_SOURCE_CHANGED", "filesystem entry identity changed")


def _check_chain(chain: tuple[_Directory, ...]) -> None:
    for directory in chain:
        directory.check()


@contextmanager
def _absolute_directory(path: Path, *, create: bool = False) -> Iterator[tuple[_Directory, ...]]:
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise _fail("PRESERVATION_PATH_INVALID", "an absolute canonical directory is required")
    with ExitStack() as stack:
        fd = os.open("/", DIRECTORY_FLAGS)
        stack.callback(os.close, fd)
        chain: tuple[_Directory, ...] = ()
        for name in path.parts[1:]:
            _check_chain(chain)
            try:
                metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(name, 0o700, dir_fd=fd)
                os.fsync(fd)
                metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
            opened = stack.enter_context(_open_directory(fd, name, _inode(metadata)))
            chain = (*chain, opened)
            fd = opened.fd
        _check_chain(chain)
        yield chain
        _check_chain(chain)


def source_relative_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != Path(value).as_posix()
        or Path(value).is_absolute()
        or any(part in {".", "..", ".git"} for part in value.split("/"))
    ):
        raise _fail("PRESERVATION_PATH_INVALID", "an exact canonical relative source is required")
    return value


@contextmanager
def _open_node(parent: int, name: str) -> Iterator[_Directory]:
    metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
    identity = _inode(metadata)
    if stat.S_ISDIR(metadata.st_mode):
        with _open_directory(parent, name, identity) as node:
            yield node
    elif stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        fd = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        try:
            node = _Directory(parent, name, fd, identity)
            node.check()
            yield node
            node.check()
        finally:
            os.close(fd)
    else:
        raise _fail("PRESERVATION_SOURCE_INVALID", "source root must be a directory or regular/symlink leaf")


@contextmanager
def _absolute_source(path: Path) -> Iterator[tuple[_Directory, ...]]:
    with (
        _absolute_directory(path.parent) as ancestors,
        _open_node(ancestors[-1].fd, path.name) as source,
    ):
        yield (*ancestors, source)


@contextmanager
def _regular_fd(parent: int, name: str, expected: list[int]) -> Iterator[int]:
    # O_PATH performs no file/FIFO I/O. Reopen only a verified regular inode via
    # the kernel's own descriptor link, never via the mutable source pathname.
    pin = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    try:
        metadata = os.fstat(pin)
        if not stat.S_ISREG(metadata.st_mode) or _identity(metadata) != expected:
            raise _fail("PRESERVATION_SOURCE_CHANGED", "regular file identity changed")
        fd = os.open(f"/proc/self/fd/{pin}", os.O_RDONLY | os.O_CLOEXEC | os.O_NOATIME)
        try:
            if _identity(os.fstat(fd)) != expected:
                raise _fail("PRESERVATION_SOURCE_CHANGED", "opened file identity changed")
            yield fd
            if _identity(os.fstat(fd)) != expected:
                raise _fail("PRESERVATION_SOURCE_CHANGED", "file changed during byte transfer")
            _check_entry(parent, name, expected)
        finally:
            os.close(fd)
    finally:
        os.close(pin)


def _transfer(fd: int, destination: int | None = None) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while block := os.read(fd, CHUNK):
        digest.update(block)
        size += len(block)
        if destination is not None:
            view = memoryview(block)
            while view:
                written = os.write(destination, view)
                if not written:
                    raise _fail("PRESERVATION_COPY_FAILED", "short destination write")
                view = view[written:]
    return size, digest.hexdigest()


@dataclass
class Tree:
    manifest: list[dict[str, Any]]
    identities: dict[str, list[int]]
    inventory: dict[str, int]
    errors: list[dict[str, Any]]

    @property
    def digest(self) -> str:
        # Ownership belongs to source evidence. No chown or inode equivalence is
        # required to reconstruct the portable filesystem tree.
        return _sha([{k: v for k, v in row.items() if k != "ownership"} for row in self.manifest])

    def payload(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest,
            "source_identities": self.identities,
            "inventory": self.inventory,
            "errors": self.errors,
            "tree_sha256": self.digest,
        }

    def require_complete(self) -> None:
        if self.errors:
            raise _fail("PRESERVATION_SCAN_BLOCKED", "source/destination scan is incomplete")


def scan_tree(directory: _Directory, chain: tuple[_Directory, ...]) -> Tree:
    """Hash every regular file; never follow links or open FIFO endpoints."""
    manifest: list[dict[str, Any]] = []
    identities: dict[str, list[int]] = {}
    counts = dict(
        regular_files=0,
        directories=0,
        symlinks=0,
        fifos=0,
        other_special_nodes=0,
        total_regular_file_bytes=0,
        metadata_errors=0,
        unreadable_regular_files=0,
    )
    errors: list[dict[str, Any]] = []

    def visit(parent: int, name: str, relative: str, anchors: tuple[_Directory, ...]) -> None:
        if len(identities) >= MAX_ENTRIES:
            raise _fail("PRESERVATION_SCAN_LIMIT", "tree exceeds the preservation entry bound")
        _check_chain(anchors)
        try:
            metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError as exc:
            counts["metadata_errors"] += 1
            errors.append({"path": relative, "code": "METADATA_UNREADABLE", "errno": exc.errno})
            return
        identity = _identity(metadata)
        identities[relative] = identity
        row: dict[str, Any] = {
            "path": relative,
            "mode": stat.S_IMODE(metadata.st_mode),
            "ownership": {"uid": metadata.st_uid, "gid": metadata.st_gid},
        }
        manifest.append(row)
        mode = metadata.st_mode
        try:
            if stat.S_ISDIR(mode):
                row["type"] = "directory"
                counts["directories"] += 1
                with _open_directory(parent, name, _inode(metadata)) as opened:
                    names = sorted(os.listdir(opened.fd))
                    for child in names:
                        visit(
                            opened.fd,
                            child,
                            child if relative == "." else f"{relative}/{child}",
                            (*anchors, opened),
                        )
                    opened.check()
                    if sorted(os.listdir(opened.fd)) != names:
                        raise _fail("PRESERVATION_SOURCE_CHANGED", "directory entries changed")
            elif stat.S_ISREG(mode):
                row.update(type="regular", size=metadata.st_size)
                counts["regular_files"] += 1
                counts["total_regular_file_bytes"] += metadata.st_size
                try:
                    with _regular_fd(parent, name, identity) as fd:
                        size, digest = _transfer(fd)
                    if size != metadata.st_size:
                        raise _fail("PRESERVATION_SOURCE_CHANGED", "file size changed")
                    row["content_sha256"] = digest
                except OSError as exc:
                    counts["unreadable_regular_files"] += 1
                    errors.append(
                        {"path": relative, "code": "REGULAR_FILE_UNREADABLE", "errno": exc.errno}
                    )
            elif stat.S_ISLNK(mode):
                row.update(type="symlink", target=os.readlink(name, dir_fd=parent))
                counts["symlinks"] += 1
            elif stat.S_ISFIFO(mode):
                row["type"] = "fifo"
                counts["fifos"] += 1
            else:
                row.update(type="unsupported", file_type=stat.S_IFMT(mode))
                counts["other_special_nodes"] += 1
                errors.append({"path": relative, "code": "UNSUPPORTED_SPECIAL_NODE"})
            _check_entry(parent, name, identity)
            _check_chain(anchors)
        except OSError as exc:
            counts["metadata_errors"] += 1
            errors.append({"path": relative, "code": "METADATA_UNREADABLE", "errno": exc.errno})

    visit(directory.parent_fd, directory.name, ".", chain)
    return Tree(manifest, identities, counts, errors)


def _same_source(tree: Tree, expected: dict[str, Any]) -> None:
    tree.require_complete()
    if tree.payload() != expected:
        raise _fail(
            "PRESERVATION_SOURCE_CHANGED", "source content, paths or inode identities changed"
        )


def _same_tree(tree: Tree, source: dict[str, Any]) -> None:
    tree.require_complete()
    expected = [{k: v for k, v in row.items() if k != "ownership"} for row in source["manifest"]]
    actual = [{k: v for k, v in row.items() if k != "ownership"} for row in tree.manifest]
    if actual != expected or tree.digest != source["tree_sha256"]:
        raise _fail("PRESERVATION_VERIFY_FAILED", "preserved paths, types, modes or content differ")


def local_environment_roots(target: Path) -> tuple[Path, ...]:
    """Only physical environment roots inside this exact checkout may be excluded.

    Resolving a selected symlink must never exclude an outside environment whose
    editable mapping still refers to the source allocation.
    """
    roots = []
    for relative in (".venv", "backend/.venv"):
        candidate = target / relative
        if not candidate.is_symlink() and candidate.is_dir() and candidate.resolve(strict=True) == candidate:
            roots.append(candidate)
    return tuple(roots)


def validate_editable_references(repository, target: Path) -> None:
    from .environment import editable_references

    # Scan every managed environment, then filter only frozen physical local roots.
    # No mutable path is passed to the scanner's resolving exclusion API.
    with ExitStack() as stack:
        roots = local_environment_roots(target)
        paths = frozenset(str(root) for root in roots)
        chains = tuple(stack.enter_context(_absolute_directory(root)) for root in roots)
        for chain in chains:
            _check_chain(chain)
        references = editable_references(repository, target)
        for chain in chains:
            _check_chain(chain)
        if any(reference["environment"] not in paths for reference in references):
            raise _fail("STALE_EDITABLE_REFERENCE", "another managed environment references the checkout")


def _authority(
    repo: Path,
    target: Path,
    allocation: str,
    owner: str,
    version: int,
    *,
    source_relative: str = SOURCE,
    recovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .lifecycle import _typed_cleanup_plan_locked
    from .semantics import managed_record as _managed_record

    source_relative = source_relative_path(source_relative)
    if tracked_paths(target, source_relative) or head_paths(target, source_relative):
        raise _fail("TRACKED_RESOURCE", "preservation source contains tracked/staged files")
    if not path_is_ignored(target, source_relative):
        raise _fail("RESOURCE_NOT_IGNORED", "preservation source must be exactly Git-ignored")
    repository = inventory(repo)
    store = MetadataStore(repository.common_dir)
    with store.lock(exclusive=False):
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        if record.allocation_id != allocation:
            raise _fail(
                "PRESERVATION_ALLOCATION_MISMATCH", "preservation belongs to another allocation"
            )
        plan = _typed_cleanup_plan_locked(
            repository,
            snapshot,
            store,
            item,
            record,
            owner=owner,
            expected_record_version=version,
            recovery=recovery,
        )
    # Copy preparation preserves only the selected ignored node. Other resource
    # classifications stay as cleanup-not-ready evidence, never deletion authority.
    resource_blockers = {(row["path"], row["blocker"]) for row in plan["resources"] if row["blocker"]}
    blocked = [b for b in plan["blockers"] if (b.get("path"), b["code"]) not in resource_blockers]
    if blocked:
        raise _fail(blocked[0]["code"], "allocation/runtime guards block preservation")
    validate_editable_references(repository, target)
    return {
        "identity": {
            k: plan[k]
            for k in (
                "repository_id",
                "allocation_id",
                "target_path",
                "expected_head",
                "expected_record_version",
                "owner",
                "authority_key",
            )
        },
        "runtime_evidence": plan["runtime_evidence"],
        "warnings": plan["warnings"],
        "cleanup_not_ready": [b for b in plan["blockers"] if (b.get("path"), b["code"]) in resource_blockers],
        "worktree_paths": [str(w.path) for w in repository.worktrees],
    }


def evidence_root() -> Path:
    return Path.home() / ".local/state/worktree-controller/preservations/v1"


def _destination(root: Path, authority: dict[str, Any], generation: str) -> Path:
    identity = authority["identity"]
    for value in (generation, identity["repository_id"], identity["allocation_id"]):
        if str(uuid.UUID(value)) != value:
            raise _fail("PRESERVATION_PATH_INVALID", "canonical UUID identity is required")
    if not root.is_absolute() or ".." in root.parts:
        raise _fail("PRESERVATION_PATH_INVALID", "state root must be absolute")
    if any(root.is_relative_to(Path(w)) for w in authority["worktree_paths"]):
        raise _fail("PRESERVATION_DESTINATION_INVALID", "state root must be outside every worktree")
    return root / identity["repository_id"] / identity["allocation_id"] / generation


def _chain_identities(chain: tuple[_Directory, ...]) -> dict[str, dict[str, int]]:
    path = Path("/")
    result = {}
    for directory in chain:
        path /= directory.name
        result[str(path)] = directory.identity
    return result


def _destination_anchor(chain: tuple[_Directory, ...], expected: dict[str, dict[str, int]]) -> None:
    actual = _chain_identities(chain)
    if any(actual.get(path) != identity for path, identity in expected.items()):
        raise _fail(
            "PRESERVATION_DESTINATION_CHANGED", "planned destination ancestor identity changed"
        )


def _destination_preflight(destination: Path) -> dict[str, dict[str, int]]:
    # Inspect all existing ancestors without creating the generation during plan.
    ancestor = destination.parent
    while not os.path.lexists(ancestor):
        ancestor = ancestor.parent
    with _absolute_directory(ancestor) as chain:
        if ancestor == destination.parent:
            try:
                os.stat(destination.name, dir_fd=chain[-1].fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise _fail("PRESERVATION_DESTINATION_EXISTS", "generation already exists")
        return _chain_identities(chain)


def plan_preservation(
    repo: Path,
    target: Path,
    allocation: str,
    owner: str,
    version: int,
    *,
    source_relative: str | None = None,
    state_root: Path | None = None,
    generation: str | None = None,
    recovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if source_relative is None:
        source_relative = "backend/.venv" if (target / "backend/.venv").exists() and not (target / SOURCE).exists() else SOURCE
    options = {"recovery": recovery} if recovery is not None else {}
    authority = _authority(repo, target, allocation, owner, version, source_relative=source_relative, **options)
    root = state_root or evidence_root()
    generation = generation or str(uuid.uuid4())
    destination = _destination(root, authority, generation)
    destination_anchor = _destination_preflight(destination)
    source_relative = source_relative_path(source_relative)
    source_path = target / source_relative
    with _absolute_source(source_path) as chain:
        tree = scan_tree(chain[-1], chain)
        source_anchor = {"worktree": chain[-2].identity, "source": chain[-1].identity}
        source_ancestors = _chain_identities(chain[:-1])
    recovery_fields = {}
    if recovery is not None:
        from . import cleanup_recovery, hardlinks

        store = MetadataStore(inventory(repo).common_dir)
        _, old_plan = cleanup_recovery.original(
            store, recovery, target=target, allocation_id=allocation
        )
        cleanup_recovery.validate_current_tree(tree, old_plan["source_tree"])
        topology = hardlinks.inventory(target, cleanup_recovery.REMAINING)
        recovery_fields = {"recovery": recovery, "cleanup_topology": topology}
    after = _authority(repo, target, allocation, owner, version, source_relative=source_relative, **options)
    if after["identity"] != authority["identity"]:
        raise _fail("PRESERVATION_AUTHORITY_CHANGED", "allocation changed during planning")
    payload = {
        **recovery_fields,
        "contract": CONTRACT,
        "repository": str(repo),
        "authority": authority,
        "source": str(source_path),
        "source_relative": source_relative,
        "source_anchor": source_anchor,
        "source_ancestor_identities": source_ancestors,
        "source_tree": tree.payload(),
        "generation": generation,
        "state_root": str(root),
        "destination": str(destination),
        "destination_anchor": destination_anchor,
        "runtime_checks": [authority["runtime_evidence"], after["runtime_evidence"]],
        "ready": not tree.errors,
    }
    return {**payload, "plan_sha256": _sha(payload)}


def _current_authority(plan: dict[str, Any]) -> dict[str, Any]:
    expected = plan["authority"]["identity"]
    source_rel = plan.get("source_relative") or (
        str(Path(plan["source"]).relative_to(Path(expected["target_path"]))) if "source" in plan else SOURCE
    )
    options = {"recovery": plan["recovery"]} if "recovery" in plan else {}
    authority = _authority(
        Path(plan["repository"]),
        Path(expected["target_path"]),
        expected["allocation_id"],
        expected["owner"],
        expected["expected_record_version"],
        source_relative=source_rel,
        **options,
    )
    if authority["identity"] != expected:
        raise _fail(
            "PRESERVATION_AUTHORITY_CHANGED", "allocation identity differs from preservation"
        )
    if "cleanup_topology" in plan:
        from .hardlinks import verify

        verify(Path(expected["target_path"]), plan["cleanup_topology"])
    return authority


def _validate_plan(plan: dict[str, Any], state_root: Path) -> Path:
    payload = {k: v for k, v in plan.items() if k != "plan_sha256"}
    if (
        plan.get("contract") != CONTRACT
        or not plan.get("ready")
        or _sha(payload) != plan.get("plan_sha256")
    ):
        raise _fail("PRESERVATION_PLAN_INVALID", "a complete exact preservation plan is required")
    expected_source = Path(plan["authority"]["identity"]["target_path"]) / source_relative_path(
        plan.get("source_relative") or SOURCE
    )
    if plan["source"] != str(expected_source) or plan["state_root"] != str(state_root):
        raise _fail("PRESERVATION_PLAN_INVALID", "source or state root differs from contract")
    authority = _current_authority(plan)
    destination = _destination(state_root, authority, plan["generation"])
    if str(destination) != plan["destination"]:
        raise _fail("PRESERVATION_PLAN_INVALID", "generation destination identity differs")
    return destination


def _copy_node(
    source: _Directory,
    destination: _Directory,
    tree: dict[str, Any],
    source_chain: tuple[_Directory, ...],
    destination_chain: tuple[_Directory, ...],
) -> dict[str, list[int]]:
    created_identities: dict[str, list[int]] = {}
    rows = {row["path"]: row for row in tree["manifest"]}
    children: dict[str, list[str]] = {
        path: [] for path, row in rows.items() if row["type"] == "directory"
    }
    for path in rows:
        if path != ".":
            parent, _, name = path.rpartition("/")
            children[parent or "."].append(name)

    def visit(
        src: _Directory,
        dst: _Directory,
        relative: str,
        schain: tuple[_Directory, ...],
        dchain: tuple[_Directory, ...],
    ) -> None:
        _check_chain(schain)
        _check_chain(dchain)
        _check_entry(src.parent_fd, src.name, tree["source_identities"][relative])
        if sorted(os.listdir(src.fd)) != children[relative]:
            raise _fail("PRESERVATION_SOURCE_CHANGED", "source entries changed before copying")
        for name in children[relative]:
            path = name if relative == "." else f"{relative}/{name}"
            row = rows[path]
            expected = tree["source_identities"][path]
            _check_chain(schain)
            _check_chain(dchain)
            _check_entry(src.fd, name, expected)
            if row["type"] == "directory":
                with _open_directory(
                    src.fd, name, _inode(os.stat(name, dir_fd=src.fd, follow_symlinks=False))
                ) as child:
                    os.mkdir(name, 0o700, dir_fd=dst.fd)
                    metadata = os.stat(name, dir_fd=dst.fd, follow_symlinks=False)
                    with _open_directory(dst.fd, name, _inode(metadata)) as copied:
                        visit(child, copied, path, (*schain, child), (*dchain, copied))
            elif row["type"] == "regular":
                with _regular_fd(src.fd, name, expected) as reader:
                    writer = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o600,
                        dir_fd=dst.fd,
                    )
                    try:
                        created = _inode(os.fstat(writer))
                        size, digest = _transfer(reader, writer)
                        if size != row["size"] or digest != row["content_sha256"]:
                            raise _fail(
                                "PRESERVATION_SOURCE_CHANGED",
                                "copied bytes differ from source plan",
                            )
                        os.fchmod(writer, row["mode"])
                        os.fsync(writer)
                        created_identities[path] = _identity(os.fstat(writer))
                        if _inode(os.stat(name, dir_fd=dst.fd, follow_symlinks=False)) != created:
                            raise _fail(
                                "PRESERVATION_DESTINATION_CHANGED", "destination file replaced"
                            )
                    finally:
                        os.close(writer)
            elif row["type"] == "symlink":
                if os.readlink(name, dir_fd=src.fd) != row["target"]:
                    raise _fail("PRESERVATION_SOURCE_CHANGED", "link literal changed")
                os.symlink(row["target"], name, dir_fd=dst.fd)
                created_identities[path] = _identity(
                    os.stat(name, dir_fd=dst.fd, follow_symlinks=False)
                )
            elif row["type"] == "fifo":
                # No open/read, payload, waiting, chown, or regular-file fallback.
                # The command is single-threaded; restore the process umask immediately.
                previous_umask = os.umask(0)
                try:
                    os.mkfifo(name, row["mode"], dir_fd=dst.fd)
                finally:
                    os.umask(previous_umask)
                copied = os.stat(name, dir_fd=dst.fd, follow_symlinks=False)
                if not stat.S_ISFIFO(copied.st_mode) or stat.S_IMODE(copied.st_mode) != row["mode"]:
                    raise _fail("PRESERVATION_VERIFY_FAILED", "created FIFO type/mode differs")
                created_identities[path] = _identity(copied)
            else:
                raise _fail("UNSUPPORTED_SPECIAL_NODE", "node is outside the preservation contract")
            _check_entry(src.fd, name, expected)
            _check_entry(dst.fd, name, created_identities[path])
            _check_chain(schain)
            _check_chain(dchain)
        _check_entry(src.parent_fd, src.name, tree["source_identities"][relative])
        _check_chain(dchain)
        os.fchmod(dst.fd, rows[relative]["mode"])
        os.fsync(dst.fd)
        created_identities[relative] = _identity(os.fstat(dst.fd))
        dst.check()

    visit(source, destination, ".", source_chain, destination_chain)
    return created_identities


def _copy_leaf(source: _Directory, destination: _Directory, tree: dict[str, Any]) -> dict[str, list[int]]:
    row = tree["manifest"][0]
    expected = tree["source_identities"]["."]
    _check_entry(source.parent_fd, source.name, expected)
    if row["type"] == "regular":
        with _regular_fd(source.parent_fd, source.name, expected) as reader:
            writer = os.open("tree", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=destination.fd)
            try:
                size, digest = _transfer(reader, writer)
                if size != row["size"] or digest != row["content_sha256"]:
                    raise _fail("PRESERVATION_SOURCE_CHANGED", "copied leaf bytes differ from plan")
                os.fchmod(writer, row["mode"])
                os.fsync(writer)
                identity = _identity(os.fstat(writer))
                _check_entry(destination.fd, "tree", identity)
            finally:
                os.close(writer)
    elif row["type"] == "symlink":
        if os.readlink(source.name, dir_fd=source.parent_fd) != row["target"]:
            raise _fail("PRESERVATION_SOURCE_CHANGED", "source link literal changed")
        os.symlink(row["target"], "tree", dir_fd=destination.fd)
        identity = _identity(os.stat("tree", dir_fd=destination.fd, follow_symlinks=False))
    else:
        raise _fail("PRESERVATION_SOURCE_INVALID", "a regular/symlink leaf is required")
    _check_entry(source.parent_fd, source.name, expected)
    source.check()
    destination.check()
    os.fsync(destination.fd)
    return {".": identity}


def _write_once(parent: _Directory, name: str, value: object) -> str:
    parent.check()
    data = _json_bytes(value)
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=parent.fd,
    )
    try:
        expected = _inode(os.fstat(fd))
        with os.fdopen(os.dup(fd), "wb") as stream:
            stream.write(data)
            stream.flush()
        os.fchmod(fd, 0o400)
        os.fsync(fd)
        if _inode(os.stat(name, dir_fd=parent.fd, follow_symlinks=False)) != expected:
            raise _fail("PRESERVATION_DESTINATION_CHANGED", "evidence file replaced")
        parent.check()
        os.fsync(parent.fd)
    finally:
        os.close(fd)
    return hashlib.sha256(data).hexdigest()


def _source_anchor(chain: tuple[_Directory, ...], plan: dict[str, Any]) -> None:
    if {"worktree": chain[-2].identity, "source": chain[-1].identity} != plan["source_anchor"]:
        raise _fail("PRESERVATION_SOURCE_CHANGED", "worktree/source directory identity changed")
    # Old immutable generations retain their original anchor contract. New plans
    # bind every ancestor, including the checkout root for a nested source.
    if (
        "source_ancestor_identities" in plan
        and _chain_identities(chain[:-1]) != plan["source_ancestor_identities"]
    ):
        raise _fail("PRESERVATION_SOURCE_CHANGED", "source ancestor directory identity changed")


def copy_preservation(plan: dict[str, Any], *, state_root: Path | None = None) -> dict[str, Any]:
    destination = _validate_plan(plan, state_root or evidence_root())
    _destination_preflight(destination)
    with _absolute_source(Path(plan["source"])) as source_chain:
        _source_anchor(source_chain, plan)
        source = source_chain[-1]
        _same_source(scan_tree(source, source_chain), plan["source_tree"])
        with _absolute_directory(destination.parent, create=True) as parent_chain:
            parent = parent_chain[-1]
            _check_chain(parent_chain)
            _destination_anchor(parent_chain, plan["destination_anchor"])
            os.mkdir(destination.name, 0o700, dir_fd=parent.fd)
            os.fsync(parent.fd)
            identity = _inode(os.stat(destination.name, dir_fd=parent.fd, follow_symlinks=False))
            with _open_directory(parent.fd, destination.name, identity) as generation:
                generation_chain = (*parent_chain, generation)
                _write_once(generation, "plan.json", plan)
                leaf = plan["source_tree"]["manifest"][0]["type"] != "directory"
                if leaf:
                    created_identities = _copy_leaf(source, generation, plan["source_tree"])
                else:
                    os.mkdir("tree", 0o700, dir_fd=generation.fd)
                with _open_node(generation.fd, "tree") as copied:
                    destination_chain = (*generation_chain, copied)
                    if not leaf:
                        created_identities = _copy_node(
                            source, copied, plan["source_tree"], source_chain, destination_chain
                        )
                    preserved = scan_tree(copied, destination_chain)
                    _same_tree(preserved, plan["source_tree"])
                    if preserved.identities != created_identities:
                        raise _fail(
                            "PRESERVATION_DESTINATION_CHANGED",
                            "created destination node identities changed",
                        )
                    manifest_digest = _write_once(generation, "manifest.json", plan["source_tree"])
                    _same_source(scan_tree(source, source_chain), plan["source_tree"])
                    _check_chain(source_chain)
                    _check_chain(destination_chain)
                    source_verified_at = datetime.now(UTC).isoformat()
    # Close our inspection FDs before procfs scanning. There is no self-PID
    # exception: any observed reference still blocks, including our own.
    authority = _current_authority(plan)
    with _absolute_directory(destination) as generation_chain:
        generation = generation_chain[-1]
        _destination_anchor(generation_chain, plan["destination_anchor"])
        if (
            generation.identity != identity
            or _read_json(generation, "manifest.json") != plan["source_tree"]
        ):
            raise _fail("PRESERVATION_DESTINATION_CHANGED", "generation identity changed")
        receipt = {
            "contract": CONTRACT,
            "result": "LEGACY_PRESERVATION_COMPLETE",
            "receipt_id": plan["generation"],
            "timestamp": datetime.now(UTC).isoformat(),
            "source_verified_at": source_verified_at,
            "allocation_source_identity": plan["authority"]["identity"],
            "source": plan["source"],
            "source_anchor": plan["source_anchor"],
            "source_identity_sha256": _sha(plan["source_tree"]["source_identities"]),
            "generation": str(destination),
            "generation_identity": identity,
            "destination": str(destination / "tree"),
            "manifest_sha256": manifest_digest,
            "source_tree_sha256": plan["source_tree"]["tree_sha256"],
            "preserved_tree_sha256": preserved.digest,
            "destination_identity_sha256": _sha(created_identities),
            "inventory": preserved.inventory,
            "verification": "PASS",
            "plan_sha256": plan["plan_sha256"],
            "runtime_checks": [*plan["runtime_checks"], authority["runtime_evidence"]],
            "warnings": authority["warnings"],
            "cleanup_authorized": False,
            "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        if "cleanup_topology" in plan:
            receipt["cleanup_topology_sha256"] = plan["cleanup_topology"]["sha256"]
            receipt["recovery_contract"] = plan["recovery"]["contract"]
        _check_chain(generation_chain)
        receipt_digest = _write_once(generation, "receipt.json", receipt)
        os.fchmod(generation.fd, 0o500)
        os.fsync(generation.fd)
        os.fsync(generation.parent_fd)
    return {
        "result": receipt["result"],
        "receipt": str(destination / "receipt.json"),
        "receipt_sha256": receipt_digest,
        "generation": str(destination),
        "tree_sha256": preserved.digest,
        "inventory": preserved.inventory,
    }


def _read_json(parent: _Directory, name: str) -> dict[str, Any]:
    metadata = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
    # Only Controller-generated JSON evidence is parsed, never preserved bytes.
    with (
        _regular_fd(parent.fd, name, _identity(metadata)) as fd,
        os.fdopen(os.dup(fd), "rb") as stream,
    ):
        result = json.load(stream)
    parent.check()
    return result


def verify_preservation(plan: dict[str, Any], *, state_root: Path | None = None) -> dict[str, Any]:
    destination = _validate_plan(plan, state_root or evidence_root())
    with _absolute_directory(destination) as generation_chain:
        generation = generation_chain[-1]
        _destination_anchor(generation_chain, plan["destination_anchor"])
        receipt = _read_json(generation, "receipt.json")
        manifest = _read_json(generation, "manifest.json")
        if (
            receipt.get("contract") != CONTRACT
            or receipt.get("verification") != "PASS"
            or receipt.get("plan_sha256") != plan["plan_sha256"]
            or receipt.get("allocation_source_identity") != plan["authority"]["identity"]
            or receipt.get("generation_identity") != generation.identity
            or receipt.get("manifest_sha256") != _sha(manifest)
            or manifest != plan["source_tree"]
            or receipt.get("source_tree_sha256") != manifest["tree_sha256"]
            or receipt.get("preserved_tree_sha256") != manifest["tree_sha256"]
        ):
            raise _fail(
                "PRESERVATION_RECEIPT_INVALID", "receipt does not bind this exact source/generation"
            )
        with _absolute_source(Path(plan["source"])) as source_chain:
            _source_anchor(source_chain, plan)
            _same_source(scan_tree(source_chain[-1], source_chain), manifest)
            with _open_node(generation.fd, "tree") as copied:
                current_destination = scan_tree(copied, (*generation_chain, copied))
                _same_tree(current_destination, manifest)
                if _sha(current_destination.identities) != receipt.get(
                    "destination_identity_sha256"
                ):
                    raise _fail(
                        "PRESERVATION_DESTINATION_CHANGED", "preserved node identities changed"
                    )
            _same_source(scan_tree(source_chain[-1], source_chain), manifest)
    _current_authority(plan)
    return {
        "result": "LEGACY_PRESERVATION_COMPLETE",
        "verification": "PASS",
        "tree_sha256": manifest["tree_sha256"],
        "receipt": str(destination / "receipt.json"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser(
        "plan", help="read-only full source hash scan; write an exclusive plan file"
    )
    create.add_argument("--repo", required=True, type=Path)
    create.add_argument("--path", required=True, type=Path)
    create.add_argument("--allocation-id", required=True)
    create.add_argument("--owner", required=True)
    create.add_argument("--expected-record-version", required=True, type=int)
    create.add_argument("--plan-file", required=True, type=Path)
    create.add_argument("--source", default=None, help="relative source path to preserve")
    create.add_argument("--recovery-transaction-id")
    create.add_argument("--transaction-sha256")
    create.add_argument("--cleanup-receipt-sha256")
    for command in ("copy", "verify"):
        subcommand = commands.add_parser(command)
        subcommand.add_argument("--plan-file", required=True, type=Path)
        if command == "verify":
            subcommand.add_argument("--destination-only", action="store_true")
            subcommand.add_argument("--receipt-sha256")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            recovery = None
            if (
                args.recovery_transaction_id
                or args.transaction_sha256
                or args.cleanup_receipt_sha256
            ):
                from .cleanup_recovery import context

                recovery = context(
                    args.recovery_transaction_id,
                    args.transaction_sha256,
                    args.cleanup_receipt_sha256,
                )
            plan = plan_preservation(
                args.repo,
                args.path,
                args.allocation_id,
                args.owner,
                args.expected_record_version,
                source_relative=args.source,
                recovery=recovery,
            )
            with _absolute_directory(args.plan_file.parent) as chain:
                _write_once(chain[-1], args.plan_file.name, plan)
            result = {
                "result": "PRESERVATION_PLAN_READY" if plan["ready"] else "PRESERVATION_BLOCKED",
                "plan_file": str(args.plan_file),
                "plan_sha256": plan["plan_sha256"],
                "inventory": plan["source_tree"]["inventory"],
                "errors": plan["source_tree"]["errors"],
                "tree_sha256": plan["source_tree"]["tree_sha256"],
                "destination": plan["destination"],
                "runtime_evidence": plan["runtime_checks"][-1],
            }
            exit_code = 0 if plan["ready"] else 1
        else:
            with _absolute_directory(args.plan_file.parent) as chain:
                plan = _read_json(chain[-1], args.plan_file.name)
            if args.command == "verify" and args.destination_only:
                from .preserved_cleanup import coverage

                evidence, _ = coverage(
                    Path(plan["destination"]) / "receipt.json",
                    args.receipt_sha256,
                    verify_source=False,
                )
                result = {
                    "result": "PRESERVATION_DESTINATION_VERIFIED",
                    "verification": "PASS",
                    "tree_sha256": evidence["tree_sha256"],
                    "source_checked": False,
                    "receipt": evidence["receipt_path"],
                    "cleanup_authorized": False,
                }
            else:
                result = (copy_preservation if args.command == "copy" else verify_preservation)(
                    plan
                )
            exit_code = 0
        print(json.dumps(result, sort_keys=True))
        return exit_code
    except (ControllerError, OSError, ValueError, KeyError, TypeError) as exc:
        # Never serialize client_id payloads or arbitrary exception strings.
        print(
            json.dumps(
                {
                    "result": "PRESERVATION_BLOCKED",
                    "code": getattr(exc, "code", "PRESERVATION_IO_ERROR"),
                    "errno": getattr(exc, "errno", None),
                }
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
