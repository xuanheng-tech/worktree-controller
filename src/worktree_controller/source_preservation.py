"""Exact unstaged tracked-source preservation; grants no cleanup/release authority.

The base Git tree plus the recorded file overlay reconstructs the dirty source.
Ignored resources are outside this contract. Payloads are never redacted, parsed
as provider state, or written back to the source worktree/index.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from . import preservation as p
from .cleanup_evidence import read_document
from .disposable import _Directory, _inode, _open_directory
from .git_backend import _git
from .models import ControllerError
from .store import MetadataStore

CONTRACT = "tracked-source-preservation/v1"
MAX_FILES = 256
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
_SECRET_NAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "credentials.toml",
    "auth.json",
    "secrets.toml",
    "service-account.json",
    "id_rsa",
    "id_ed25519",
}
_SECRET_PARTS = {".ssh", ".aws", "provider-state"}
_SECRET_BYTES = re.compile(
    rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----|"
    rb"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}|"
    rb"\bgh[pousr]_[A-Za-z0-9]{24,}|\bAKIA[A-Z0-9]{16}\b"
)


def _fail(code: str) -> ControllerError:
    # No payload, Git stderr, or exception text may enter preservation diagnostics.
    return ControllerError(code, "exact tracked-source preservation guard refused")


class AuthorityBlocked(ControllerError):
    def __init__(self, code: str, runtime_evidence: dict):
        super().__init__(code, "allocation runtime authority refused preservation")
        self.runtime_evidence = runtime_evidence


def _path(value: str) -> PurePosixPath:
    result = PurePosixPath(value)
    if (
        not value
        or result.is_absolute()
        or result.as_posix() != value
        or any(part in {".", "..", ".git"} for part in result.parts)
        or "\x00" in value
    ):
        raise _fail("SOURCE_PATH_INVALID")
    parts = {part.lower() for part in result.parts}
    name = result.name.lower()
    if (
        parts & _SECRET_PARTS
        or any(part.endswith("-home") for part in parts)
        or name in _SECRET_NAMES
        or name.startswith(".env.")
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
    ):
        raise _fail("SOURCE_CREDENTIAL_PATH")
    return result


def _authority(
    repo: Path,
    target: Path,
    allocation: str,
    owner: str,
    version: int,
    *,
    pending_transaction: dict | None = None,
    _migration_store: MetadataStore | None = None,
) -> dict:
    from .lifecycle import _typed_runtime_blockers, _typed_runtime_evidence, inventory
    from .semantics import assert_record_version as _cas
    from .semantics import managed_record as _managed_record

    with p._absolute_directory(target):
        repository = inventory(repo)
    store = _migration_store or MetadataStore(repository.common_dir)
    if store.root.parent.parent != repository.common_dir:
        raise _fail("SOURCE_AUTHORITY_MISMATCH")
    # Layout preflight already holds this repository's lock while inspecting the
    # explicit previous namespace. Reopening flock would deadlock its own writer.
    with nullcontext() if _migration_store is not None else store.lock(exclusive=False):
        snapshot = store._load_unlocked()
        item, record = _managed_record(repository, snapshot, target)
        _cas(record, version)
        if record.allocation_id != allocation or record.owner != owner:
            raise _fail("SOURCE_AUTHORITY_MISMATCH")
        if (
            record.lifecycle != "ACTIVE"
            or record.role not in {"feature", "audit", "hotfix"}
            or item.canonical
            or item.detached
            or item.locked
            or item.prunable
            or record.handoff_commit
        ):
            raise _fail("SOURCE_LIFECYCLE_BLOCKED")
        from .transaction_scope import check_source

        check_source(
            store,
            allocation,
            target,
            pending_transaction,
            assume_serialized=_migration_store is not None,
        )
        runtime = _typed_runtime_evidence(item, record, snapshot)
        blockers = _typed_runtime_blockers(runtime)
        if blockers:
            raise AuthorityBlocked(blockers[0]["code"], runtime)
        return {
            "identity": {
                "repository_id": snapshot.repository_id,
                "allocation_id": allocation,
                "target_path": str(item.path),
                "git_dir": str(item.git_dir),
                "expected_head": item.head,
                "expected_record_version": version,
                "owner": owner,
                "authority_key": record.authority_key,
                "task_key": record.task_key,
                "branch": item.branch,
            },
            "runtime_evidence": runtime,
            "worktree_paths": [str(w.path) for w in repository.worktrees],
        }


def _git_state(
    target: Path, git_dir: Path, expected_head: str, *, allow_clean: bool = False
) -> dict:
    head = _git(target, "rev-parse", "HEAD").stdout.decode().strip()
    if head != expected_head:
        raise _fail("SOURCE_HEAD_MISMATCH")
    base: dict[str, list[str]] = {}
    for entry in _git(target, "ls-tree", "-rz", head).stdout.split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        if kind != "blob":
            raise _fail("SOURCE_GIT_STATE_UNSUPPORTED")
        base[name.decode("utf-8")] = [mode, oid]
    index = {}
    for entry in _git(target, "ls-files", "--stage", "-z").stdout.split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, oid, stage = metadata.decode().split()
        if stage != "0":
            raise _fail("SOURCE_STAGED_OR_UNMERGED")
        index[name.decode("utf-8")] = [mode, oid]
    if index != base:
        raise _fail("SOURCE_STAGED_OR_UNMERGED")
    flags = _git(target, "ls-files", "-v", "-z").stdout.split(b"\0")
    if any(entry and not entry.startswith(b"H ") for entry in flags):
        raise _fail("SOURCE_INDEX_FLAGS_UNSUPPORTED")
    paths = []
    for entry in _git(target, "status", "--porcelain=v1", "-z", "-uall").stdout.split(b"\0"):
        if not entry:
            continue
        if entry[:3] != b" M ":
            raise _fail("SOURCE_DIRTY_KIND_UNSUPPORTED")
        relative = entry[3:].decode("utf-8")
        _path(relative)  # Refuse credential paths before opening source payloads.
        if relative not in base or base[relative][0] not in {"100644", "100755"}:
            raise _fail("SOURCE_NODE_UNSUPPORTED")
        paths.append(relative)
    if (not paths and not allow_clean) or len(paths) > MAX_FILES or len(paths) != len(set(paths)):
        raise _fail("SOURCE_PATH_SET_UNSUPPORTED")
    # Git filters/encoding can make a clean logical blob differ from exact bytes.
    # This first contract supports raw source bytes, not filter reconstruction.
    attributes = (
        _git(
            target,
            "check-attr",
            "-z",
            "filter",
            "working-tree-encoding",
            "text",
            "ident",
            "--",
            *paths,
        ).stdout.split(b"\0")
        if paths
        else []
    )
    if any(value not in {b"unspecified", b"unset"} for value in attributes[2::3]):
        raise _fail("SOURCE_FILTER_UNSUPPORTED")
    autocrlf = _git(target, "config", "--get", "core.autocrlf", check=False)
    # "input" leaves checkout bytes alone. No staging/clean conversion is used:
    # the reconstructed tree deliberately contains the exact raw overlay bytes.
    if autocrlf.stdout.strip() not in {b"", b"false", b"input"}:
        raise _fail("SOURCE_FILTER_UNSUPPORTED")
    with p._absolute_directory(git_dir) as chain:
        metadata = os.stat("index", dir_fd=chain[-1].fd, follow_symlinks=False)
        identity = p._identity(metadata)
        with p._regular_fd(chain[-1].fd, "index", identity) as fd:
            _, index_digest = p._transfer(fd)
    return {
        "base_head": head,
        "base_tree": _git(target, "rev-parse", f"{head}^{{tree}}").stdout.decode().strip(),
        "base_entries": base,
        "dirty_paths": sorted(paths),
        "index_identity": identity,
        "index_sha256": index_digest,
        "autocrlf": autocrlf.stdout.decode().strip(),
    }


@contextmanager
def _parent(chain: tuple[_Directory, ...], relative: str, *, create: bool = False) -> Iterator:
    path = _path(relative)
    with ExitStack() as stack:
        for name in path.parts[:-1]:
            p._check_chain(chain)
            try:
                metadata = os.stat(name, dir_fd=chain[-1].fd, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(name, 0o700, dir_fd=chain[-1].fd)
                os.fsync(chain[-1].fd)
                metadata = os.stat(name, dir_fd=chain[-1].fd, follow_symlinks=False)
            directory = stack.enter_context(_open_directory(chain[-1].fd, name, _inode(metadata)))
            chain = (*chain, directory)
        p._check_chain(chain)
        yield chain, path.name
        p._check_chain(chain)


def _read(chain: tuple[_Directory, ...], relative: str) -> tuple[bytes, list[int], dict]:
    with _parent(chain, relative) as (parents, name):
        metadata = os.stat(name, dir_fd=parents[-1].fd, follow_symlinks=False)
        identity = p._identity(metadata)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_FILE_BYTES:
            raise _fail("SOURCE_NODE_UNSUPPORTED")
        with p._regular_fd(parents[-1].fd, name, identity) as fd:
            blocks = []
            size = 0
            while block := os.read(fd, p.CHUNK):
                size += len(block)
                if size > MAX_FILE_BYTES:
                    raise _fail("SOURCE_SIZE_LIMIT")
                blocks.append(block)
        data = b"".join(blocks)
        if len(data) != metadata.st_size:
            raise _fail("SOURCE_CHANGED")
        if _SECRET_BYTES.search(data):
            raise _fail("SOURCE_SECRET_MATERIAL")
        anchors = {str(i): directory.identity for i, directory in enumerate(parents)}
        return data, identity, anchors


def _blob(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def _tree(entries: dict[str, list[str]]) -> str:
    root: dict[str, Any] = {}
    for path, entry in entries.items():
        parts = PurePosixPath(path).parts
        parent = root
        for part in parts[:-1]:
            parent = parent.setdefault(part, {})
        parent[parts[-1]] = entry

    def tree(directory: dict) -> str:
        rows = []
        for name, entry in directory.items():
            mode, oid = ("40000", tree(entry)) if isinstance(entry, dict) else entry
            key = name.encode() + (b"/" if mode == "40000" else b"\0")
            rows.append((key, mode.encode() + b" " + name.encode() + b"\0" + bytes.fromhex(oid)))
        data = b"".join(row for _, row in sorted(rows))
        return hashlib.sha1(b"tree " + str(len(data)).encode() + b"\0" + data).hexdigest()

    return tree(root)


def _scan(identity: dict) -> dict:
    target = Path(identity["target_path"])
    with p._absolute_directory(target) as chain:
        before = _git_state(target, Path(identity["git_dir"]), identity["expected_head"])
        manifest, identities, parents = [], {}, {}
        overlay = dict(before["base_entries"])
        for relative in before["dirty_paths"]:
            data, metadata, anchors = _read(chain, relative)
            mode = stat.S_IMODE(metadata[2])
            blob = _blob(data)
            manifest.append(
                {
                    "path": relative,
                    "git_status": " M",
                    "mode": mode,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "git_blob": blob,
                    "base_entry": before["base_entries"][relative],
                }
            )
            identities[relative], parents[relative] = metadata, anchors
            overlay[relative] = ["100755" if mode & 0o111 else "100644", blob]
        if sum(row["size"] for row in manifest) > MAX_TOTAL_BYTES:
            raise _fail("SOURCE_SIZE_LIMIT")
        if _git_state(target, Path(identity["git_dir"]), identity["expected_head"]) != before:
            raise _fail("SOURCE_GIT_CHANGED")
        for relative, expected in identities.items():
            with _parent(chain, relative) as (parents_chain, name):
                p._check_entry(parents_chain[-1].fd, name, expected)
        portable = {
            "base_head": before["base_head"],
            "base_tree": before["base_tree"],
            "dirty_paths": before["dirty_paths"],
            "files": manifest,
            "reconstructed_tree_oid": _tree(overlay),
        }
        return {
            **portable,
            "tree_sha256": p._sha(portable),
            "source_identities": identities,
            "parent_identities": parents,
            "index_identity": before["index_identity"],
            "index_sha256": before["index_sha256"],
            "autocrlf": before["autocrlf"],
            "root_identity": chain[-1].identity,
        }


def _current(plan: dict) -> dict:
    identity = plan["authority"]["identity"]
    authority = _authority(
        Path(plan["repository"]),
        Path(identity["target_path"]),
        identity["allocation_id"],
        identity["owner"],
        identity["expected_record_version"],
    )
    if authority["identity"] != identity:
        raise _fail("SOURCE_AUTHORITY_CHANGED")
    if _scan(identity) != plan["source_tree"]:
        raise _fail("SOURCE_CHANGED")
    return authority


def plan_preservation(
    repo: Path,
    target: Path,
    allocation: str,
    owner: str,
    version: int,
    *,
    expected_head: str,
    state_root: Path | None = None,
    generation: str | None = None,
) -> dict:
    authority = _authority(repo, target, allocation, owner, version)
    if authority["identity"]["expected_head"] != expected_head:
        raise _fail("SOURCE_HEAD_MISMATCH")
    tree = _scan(authority["identity"])
    generation = generation or str(uuid.uuid4())
    root = state_root or p.evidence_root()
    destination = p._destination(root, authority, generation)
    plan = {
        "contract": CONTRACT,
        "repository": str(repo),
        "authority": authority,
        "source_tree": tree,
        "generation": generation,
        "state_root": str(root),
        "destination": str(destination),
        "destination_anchor": p._destination_preflight(destination),
        "cleanup_authorized": False,
    }
    after = _current(plan)
    plan["runtime_checks"] = [authority["runtime_evidence"], after["runtime_evidence"]]
    return {**plan, "plan_sha256": p._sha(plan)}


def _validate(plan: dict, state_root: Path | None) -> Path:
    if (
        plan.get("contract") != CONTRACT
        or plan.get("cleanup_authorized") is not False
        or p._sha({k: v for k, v in plan.items() if k != "plan_sha256"}) != plan.get("plan_sha256")
    ):
        raise _fail("SOURCE_PLAN_INVALID")
    root = state_root or p.evidence_root()
    destination = p._destination(root, plan["authority"], plan["generation"])
    if str(root) != plan["state_root"] or str(destination) != plan["destination"]:
        raise _fail("SOURCE_DESTINATION_INVALID")
    return destination


def _copy_files(source_chain: tuple, destination_chain: tuple, tree: dict) -> None:
    for row in tree["files"]:
        relative = row["path"]
        data, identity, parents = _read(source_chain, relative)
        if (
            identity != tree["source_identities"][relative]
            or parents != tree["parent_identities"][relative]
            or len(data) != row["size"]
            or hashlib.sha256(data).hexdigest() != row["sha256"]
        ):
            raise _fail("SOURCE_CHANGED")
        with _parent(destination_chain, relative, create=True) as (chain, name):
            parent = chain[-1]
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=parent.fd,
            )
            try:
                expected = _inode(os.fstat(fd))
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    if not written:
                        raise _fail("SOURCE_COPY_FAILED")
                    view = view[written:]
                os.fchmod(fd, 0o400)
                os.fsync(fd)
                if _inode(os.stat(name, dir_fd=parent.fd, follow_symlinks=False)) != expected:
                    raise _fail("SOURCE_DESTINATION_CHANGED")
                p._check_chain(chain)
                os.fsync(parent.fd)
            finally:
                os.close(fd)


def _seal(directory: _Directory, chain: tuple) -> None:
    for name in os.listdir(directory.fd):
        metadata = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            with _open_directory(directory.fd, name, _inode(metadata)) as child:
                _seal(child, (*chain, child))
        elif not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o400:
            raise _fail("SOURCE_DESTINATION_CHANGED")
    p._check_chain(chain)
    os.fchmod(directory.fd, 0o500)
    os.fsync(directory.fd)


def _verify_files(chain: tuple, expected: dict) -> dict:
    tree = p.scan_tree(chain[-1], chain)
    tree.require_complete()
    files = {row["path"]: row for row in tree.manifest if row["type"] == "regular"}
    parents = {"."}
    for row in expected["files"]:
        parents.update(str(v) for v in PurePosixPath(row["path"]).parents)
    if (
        set(files) != set(expected["dirty_paths"])
        or {r["path"] for r in tree.manifest if r["type"] == "directory"} != parents
        or len(tree.manifest) != len(files) + len(parents)
        or any(r["mode"] != (0o400 if r["type"] == "regular" else 0o500) for r in tree.manifest)
    ):
        raise _fail("SOURCE_DESTINATION_INCOMPLETE")
    for row in expected["files"]:
        copied = files[row["path"]]
        if copied["size"] != row["size"] or copied["content_sha256"] != row["sha256"]:
            raise _fail("SOURCE_DESTINATION_DIGEST_MISMATCH")
    return tree.identities


def copy_preservation(plan: dict, *, state_root: Path | None = None) -> dict:
    destination = _validate(plan, state_root)
    _current(plan)
    p._destination_preflight(destination)
    with p._absolute_directory(destination.parent, create=True) as parent_chain:
        parent = parent_chain[-1]
        p._destination_anchor(parent_chain, plan["destination_anchor"])
        os.mkdir(destination.name, 0o700, dir_fd=parent.fd)
        os.fsync(parent.fd)
        identity = _inode(os.stat(destination.name, dir_fd=parent.fd, follow_symlinks=False))
        with _open_directory(parent.fd, destination.name, identity) as generation:
            chain = (*parent_chain, generation)
            p._write_once(generation, "plan.json", plan)
            os.mkdir("tree", 0o700, dir_fd=generation.fd)
            metadata = os.stat("tree", dir_fd=generation.fd, follow_symlinks=False)
            with _open_directory(generation.fd, "tree", _inode(metadata)) as tree:
                tree_chain = (*chain, tree)
                with p._absolute_directory(
                    Path(plan["authority"]["identity"]["target_path"])
                ) as src:
                    if src[-1].identity != plan["source_tree"]["root_identity"]:
                        raise _fail("SOURCE_CHANGED")
                    _copy_files(src, tree_chain, plan["source_tree"])
                _seal(tree, tree_chain)
                destination_identities = _verify_files(tree_chain, plan["source_tree"])
            manifest_digest = p._write_once(generation, "manifest.json", plan["source_tree"])
    after = _current(plan)
    with p._absolute_directory(destination) as chain:
        generation = chain[-1]
        p._destination_anchor(chain, plan["destination_anchor"])
        if generation.identity != identity:
            raise _fail("SOURCE_DESTINATION_CHANGED")
        metadata = os.stat("tree", dir_fd=generation.fd, follow_symlinks=False)
        with _open_directory(generation.fd, "tree", _inode(metadata)) as tree:
            if _verify_files((*chain, tree), plan["source_tree"]) != destination_identities:
                raise _fail("SOURCE_DESTINATION_CHANGED")
        receipt = {
            "contract": CONTRACT,
            "result": "SOURCE_PRESERVATION_COMPLETE",
            "verification": "PASS",
            "receipt_id": plan["generation"],
            "timestamp": datetime.now(UTC).isoformat(),
            "allocation_source_identity": plan["authority"]["identity"],
            "generation": str(destination),
            "generation_identity": identity,
            "plan_sha256": plan["plan_sha256"],
            "manifest_sha256": manifest_digest,
            "tree_sha256": plan["source_tree"]["tree_sha256"],
            "reconstructed_tree_oid": plan["source_tree"]["reconstructed_tree_oid"],
            "destination_identity_sha256": p._sha(destination_identities),
            "file_count": len(plan["source_tree"]["files"]),
            "total_bytes": sum(row["size"] for row in plan["source_tree"]["files"]),
            "runtime_checks": [*plan["runtime_checks"], after["runtime_evidence"]],
            "cleanup_authorized": False,
            "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        digest = p._write_once(generation, "receipt.json", receipt)
        os.fchmod(generation.fd, 0o500)
        os.fsync(generation.fd)
        os.fsync(generation.parent_fd)
    return {
        "result": receipt["result"],
        "receipt": str(destination / "receipt.json"),
        "receipt_sha256": digest,
        "generation": str(destination),
        "tree_sha256": receipt["tree_sha256"],
        "reconstructed_tree_oid": receipt["reconstructed_tree_oid"],
        "file_count": receipt["file_count"],
        "total_bytes": receipt["total_bytes"],
    }


def _verify_generation(plan: dict, receipt_sha256: str, state_root: Path | None = None) -> dict:
    destination = _validate(plan, state_root)
    with p._absolute_directory(destination) as chain:
        generation = chain[-1]
        p._destination_anchor(chain, plan["destination_anchor"])
        if stat.S_IMODE(os.fstat(generation.fd).st_mode) != 0o500:
            raise _fail("SOURCE_GENERATION_NOT_SEALED")
        if set(os.listdir(generation.fd)) != {"plan.json", "manifest.json", "receipt.json", "tree"}:
            raise _fail("SOURCE_GENERATION_NOT_SEALED")
        for name in ("plan.json", "manifest.json", "receipt.json"):
            st = os.stat(name, dir_fd=generation.fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(st.st_mode)
                or stat.S_IMODE(st.st_mode) != 0o400
                or st.st_nlink != 1
            ):
                raise _fail("SOURCE_GENERATION_NOT_SEALED")
        receipt, receipt_digest = read_document(generation, "receipt.json")
        manifest, manifest_digest = read_document(generation, "manifest.json")
        saved_plan, saved_plan_digest = read_document(generation, "plan.json")
        if (
            receipt_digest != receipt_sha256
            or receipt.get("contract") != CONTRACT
            or receipt.get("verification") != "PASS"
            or receipt.get("cleanup_authorized") is not False
            or receipt.get("generation_identity") != generation.identity
            or receipt.get("generation") != str(destination)
            or receipt.get("allocation_source_identity") != plan["authority"]["identity"]
            or receipt.get("plan_sha256") != plan["plan_sha256"]
            or saved_plan != plan
            or saved_plan_digest != p._sha(plan)
            or receipt.get("manifest_sha256") != manifest_digest
            or manifest != plan["source_tree"]
            or receipt.get("tree_sha256") != manifest["tree_sha256"]
            or receipt.get("reconstructed_tree_oid") != manifest["reconstructed_tree_oid"]
        ):
            raise _fail("SOURCE_RECEIPT_INVALID")
        metadata = os.stat("tree", dir_fd=generation.fd, follow_symlinks=False)
        with _open_directory(generation.fd, "tree", _inode(metadata)) as tree:
            identities = _verify_files((*chain, tree), manifest)
            if p._sha(identities) != receipt["destination_identity_sha256"]:
                raise _fail("SOURCE_DESTINATION_CHANGED")
    return {
        "result": "SOURCE_PRESERVATION_COMPLETE",
        "verification": "PASS",
        "tree_sha256": manifest["tree_sha256"],
        "reconstructed_tree_oid": manifest["reconstructed_tree_oid"],
        "receipt": str(destination / "receipt.json"),
        "cleanup_authorized": False,
    }


def verify_preservation(plan: dict, receipt_sha256: str, *, state_root: Path | None = None) -> dict:
    _current(plan)
    result = _verify_generation(plan, receipt_sha256, state_root)
    _current(plan)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("plan")
    for name in ("repo", "path"):
        create.add_argument(f"--{name}", required=True, type=Path)
    for name in ("allocation-id", "owner", "expected-head"):
        create.add_argument(f"--{name}", required=True)
    create.add_argument("--expected-record-version", required=True, type=int)
    create.add_argument("--plan-file", required=True, type=Path)
    for command in ("copy", "verify"):
        sub = commands.add_parser(command)
        sub.add_argument("--plan-file", required=True, type=Path)
        if command == "verify":
            sub.add_argument("--receipt-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            plan = plan_preservation(
                args.repo,
                args.path,
                args.allocation_id,
                args.owner,
                args.expected_record_version,
                expected_head=args.expected_head,
            )
            with p._absolute_directory(args.plan_file.parent) as chain:
                p._write_once(chain[-1], args.plan_file.name, plan)
            result = {
                "result": "SOURCE_PRESERVATION_PLAN_READY",
                "plan_file": str(args.plan_file),
                "plan_sha256": plan["plan_sha256"],
                "destination": plan["destination"],
                "files": plan["source_tree"]["files"],
                "tree_sha256": plan["source_tree"]["tree_sha256"],
                "runtime_evidence": plan["runtime_checks"][-1],
            }
        else:
            with p._absolute_directory(args.plan_file.parent) as chain:
                plan = p._read_json(chain[-1], args.plan_file.name)
            result = (
                copy_preservation(plan)
                if args.command == "copy"
                else verify_preservation(plan, args.receipt_sha256)
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ControllerError, OSError, ValueError, KeyError, TypeError):
        import sys

        exc = sys.exception()
        print(
            json.dumps(
                {
                    "result": "SOURCE_PRESERVATION_BLOCKED",
                    "code": getattr(exc, "code", "SOURCE_PRESERVATION_IO_ERROR"),
                    **(
                        {"runtime_evidence": exc.runtime_evidence}
                        if isinstance(exc, AuthorityBlocked)
                        else {}
                    ),
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
