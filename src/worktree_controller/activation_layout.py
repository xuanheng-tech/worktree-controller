"""Reversible, explicit layout transition used only by governed activation.

The old layout is caller supplied. Core operations use only the current layout.
Repository locks and temporary refusal at the old entry points fence the switch;
allocation ownership and historical receipts are never rewritten.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from contextlib import ExitStack
from pathlib import Path, PurePosixPath

from . import cleanup_evidence as evidence
from . import metadata_relocation as relocation
from . import preservation as p
from .models import ControllerError
from .store import MetadataStore, writer_lease_from_dict

REFUSAL = b"#!/bin/sh\necho 'Controller layout transition in progress; retry after activation.' >&2\nexit 75\n"


class LayoutError(Exception):
    pass


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def owned(path: Path, *, directory: bool = False) -> os.stat_result:
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if info.st_uid != os.geteuid() or not kind(info.st_mode):
        raise LayoutError(
            f"layout target is not an owned {'directory' if directory else 'file'}: {path}"
        )
    return info


def relative(value: str) -> Path:
    path = PurePosixPath(value)
    if (
        not path.parts
        or path.is_absolute()
        or str(path) != value
        or any(x in {".", "..", ".git"} for x in path.parts)
        or any(x in value for x in "\\*?[]\n\r\x00")
    ):
        raise LayoutError("layout paths must be exact repository-relative paths")
    return Path(value)


def replace_file(path: Path, raw: bytes, mode: int = 0o600) -> None:
    pending = path.with_name(f".{path.name}.layout-pending")
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(pending, path)
        sync_directory(path.parent)
    finally:
        if pending.exists():
            pending.unlink()


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _lease_bytes(raw: bytes, *, reverse: bool = False) -> bytes:
    value = json.loads(raw)
    if reverse:
        writer_lease_from_dict(value)
        value["schema_version"] = 1
        value["holder"]["provider"] = value["holder"].pop("client_id")
    else:
        if (
            type(value.get("schema_version")) is not int
            or value["schema_version"] != 1
            or "client_id" in value.get("holder", {})
        ):
            raise LayoutError("previous writer lease does not match schema 1")
        value["schema_version"] = 2
        value["holder"]["client_id"] = value["holder"].pop("provider")
        writer_lease_from_dict(value)
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def lease_bytes(raw: bytes, *, reverse: bool = False) -> bytes:
    try:
        return _lease_bytes(raw, reverse=reverse)
    except (ControllerError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise LayoutError("writer lease does not match the exact migration schema") from exc


def quiet(root: Path, repository: Path) -> list[dict]:
    if (root / "publication-lease.json").exists():
        raise LayoutError(f"publication lease blocks layout migration: {root}")
    compatible = []
    try:
        from .source_recovery import plan_reconciliation
        from .transaction_scope import is_atomic_temp, pending, terminal_state

        # The activation preflight already serializes on this namespace's
        # lock; classifying through a fresh instance must inherit that
        # serialization instead of taking a second, self-deadlocking flock.
        store = MetadataStore(root.parent.parent, _migration_root=root)
        journals = (
            [
                path
                for path in (root / "transactions").iterdir()
                if not is_atomic_temp(path.name)
            ]
            if (root / "transactions").exists()
            else []
        )
        if journals:
            pending(store, assume_serialized=True)  # Validate every identity, including retained terminal journals.
        for path in sorted(journals):
            state = terminal_state(
                store, path.stem, verify_generations=True, assume_serialized=True
            )
            if state is not None:
                compatible.append({"transaction_id": path.stem, "state": state})
                continue
            _, receipt_sha = evidence.read(store.completion_receipts_dir / path.name)
            plan = plan_reconciliation(repository, path.stem, receipt_sha, _migration_store=store)
            compatible.append(
                {
                    "transaction_id": path.stem,
                    "state": plan["state"],
                    "plan_id": plan["plan_id"],
                    "completion_receipt_sha256": receipt_sha,
                }
            )
    except (ControllerError, OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        raise LayoutError(f"unfinished transaction blocks layout migration: {root}") from exc
    live = set()
    for path in (root / "records").glob("*.json"):
        owned(path)
        if json.loads(path.read_bytes()).get("lifecycle") != "RELEASED":
            live.add(path.stem)
    for path in (root / "completion-receipts").glob("*.json"):
        owned(path)
        raw = path.read_bytes()
        value = json.loads(raw)
        if value.get("allocation_id") in live and str(root).encode() in raw:
            if any(row["transaction_id"] == path.stem for row in compatible):
                continue  # Exact supported evidence was verified above, not inferred from age.
            raise LayoutError("live completion evidence still binds the previous layout")
    return compatible


def lease_record(path: Path) -> dict[str, str]:
    owned(path)
    raw = path.read_bytes()
    converted = lease_bytes(raw)
    if json.loads(converted)["allocation_id"] != path.stem:
        raise LayoutError("writer lease filename does not match its allocation")
    return {"before": raw.hex(), "after": converted.hex()}


def refuse_running_entries(names: set[str]) -> None:
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        try:
            if (
                process.stat().st_uid == os.geteuid()
                and (process / "comm").read_text().strip() in names
            ):
                raise LayoutError("Controller invocation is still running")
        except (FileNotFoundError, ProcessLookupError):
            continue


class LayoutMigration:
    """One deployment receipt, not a new runtime authority or adapter."""

    def __init__(self, receipt: Path, document: dict):
        self.receipt = receipt
        self.document = document

    def save(self) -> None:
        replace_file(
            self.receipt, (json.dumps(self.document, indent=2, sort_keys=True) + "\n").encode()
        )

    @classmethod
    def prepare(
        cls,
        receipt: Path,
        repositories: dict[Path, Path],
        *,
        previous_namespace: str,
        previous_policy: str,
        previous_entries: dict[Path, str],
        entries: dict[Path, bytes],
        runtimes: dict[str, str],
    ) -> LayoutMigration:
        namespace, policy = relative(previous_namespace), relative(previous_policy)
        if len(namespace.parts) != 1 or not previous_entries:
            raise LayoutError("one previous namespace and exact previous entry points are required")
        if receipt.exists() or receipt.parent.exists():
            raise LayoutError("layout recovery directory already exists")
        rows = []
        for canonical, common in repositories.items():
            old = common / namespace / "v1"
            new = MetadataStore(common).root
            source_policy = canonical / policy
            target_policy = canonical / ".agents/worktree-policy.toml"
            identity = owned(old, directory=True)
            owned(old.parent, directory=True)
            if os.path.lexists(old / relocation.RECORD):
                raise LayoutError("previous layout already has relocation evidence")
            owned(source_policy)
            owned(source_policy.parent, directory=True)
            if (
                new.exists()
                or new.is_symlink()
                or target_policy.exists()
                or target_policy.is_symlink()
            ):
                raise LayoutError("current layout already exists; refusing overlapping authorities")
            with MetadataStore(common, _migration_root=old).lock(exclusive=False):
                compatibility = quiet(old, canonical)
                references = relocation.prepare(old, new)
            for path in (old / "writer-leases").glob("*.json"):
                lease_record(path)
            rows.append(
                {
                    "canonical": str(canonical),
                    "old": str(old),
                    "new": str(new),
                    "identity": [identity.st_dev, identity.st_ino],
                    "old_policy": str(source_policy),
                    "new_policy": str(target_policy),
                    "policy_sha256": digest(source_policy.read_bytes()),
                    "leases": {},
                    "transaction_compatibility": compatibility,
                    "references": references,
                }
            )
        files = []
        for path in sorted(set(previous_entries) | set(entries)):
            before = None
            mode = 0o755
            if path.exists() or path.is_symlink():
                info = owned(path)
                before = path.read_bytes()
                mode = stat.S_IMODE(info.st_mode)
            if path in previous_entries:
                if before is None or digest(before) != previous_entries[path]:
                    raise LayoutError(f"previous entry point drifted: {path}")
            elif before is not None and before != entries[path]:
                raise LayoutError(f"new entry point already has other content: {path}")
            files.append(
                {
                    "path": str(path),
                    "before": None if before is None else before.hex(),
                    "mode": mode,
                    "previous": path in previous_entries,
                    "after": entries[path].hex() if path in entries else REFUSAL.hex(),
                }
            )
        receipt.parent.mkdir(mode=0o700)
        migration = cls(
            receipt,
            {
                "schema_version": 1,
                "phase": "prepared",
                "repositories": rows,
                "entrypoints": files,
                "runtimes": runtimes,
            },
        )
        migration.save()
        return migration

    @classmethod
    def load(cls, receipt: Path, expected_sha256: str) -> LayoutMigration:
        owned(receipt)
        raw = receipt.read_bytes()
        if digest(raw) != expected_sha256:
            raise LayoutError("layout receipt digest changed")
        value = json.loads(raw)
        if (
            set(value) != {"schema_version", "phase", "repositories", "entrypoints", "runtimes"}
            or value["schema_version"] != 1
        ):
            raise LayoutError("unsupported layout receipt")
        return cls(receipt, value)

    def apply(self) -> None:
        if self.document["phase"] != "prepared":
            raise LayoutError("layout receipt is not prepared")
        with ExitStack() as stack:
            for row in self.document["repositories"]:
                old = Path(row["old"])
                info = owned(old, directory=True)
                if [info.st_dev, info.st_ino] != row["identity"]:
                    raise LayoutError("previous namespace identity changed")
                owned(old / "repo.lock")
                handle = stack.enter_context((old / "repo.lock").open("rb"))
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if quiet(old, Path(row["canonical"])) != row["transaction_compatibility"]:
                    raise LayoutError("transaction compatibility changed before layout move")
                if relocation.prepare(old, Path(row["new"])) != row["references"]:
                    raise LayoutError("immutable evidence changed before layout move")
                owned(Path(row["old_policy"]))
                if digest(Path(row["old_policy"]).read_bytes()) != row["policy_sha256"]:
                    raise LayoutError("previous policy changed")
                row["leases"] = {
                    p.name: lease_record(p) for p in (old / "writer-leases").glob("*.json")
                }
            self.document["phase"] = "applying"
            self.save()
            for entry in self.document["entrypoints"]:
                path = Path(entry["path"])
                observed = path.read_bytes().hex() if path.exists() else None
                if observed != entry["before"]:
                    raise LayoutError("entry point changed after layout preflight")
                if entry["previous"]:
                    replace_file(path, REFUSAL, entry["mode"])
            previous_names = {
                Path(x["path"]).name[:15] for x in self.document["entrypoints"] if x["previous"]
            }
            refuse_running_entries(previous_names)
            for row in self.document["repositories"]:
                old, new = Path(row["old"]), Path(row["new"])
                source_policy, target_policy = Path(row["old_policy"]), Path(row["new_policy"])
                new.parent.mkdir(mode=0o700, exist_ok=True)
                target_policy.parent.mkdir(mode=0o755, exist_ok=True)
                owned(new.parent, directory=True)
                owned(target_policy.parent, directory=True)
                if (
                    new.exists()
                    or new.is_symlink()
                    or target_policy.exists()
                    or target_policy.is_symlink()
                ):
                    raise LayoutError("current layout appeared after preflight")
                os.rename(source_policy, target_policy)
                sync_directory(source_policy.parent)
                sync_directory(target_policy.parent)
                # Travel with the directory rename: there is never a moved tree
                # whose old immutable references lack their relocation evidence.
                with p._absolute_directory(old) as chain:
                    p._write_once(chain[-1], relocation.RECORD, row["references"])
                os.rename(old, new)
                sync_directory(old.parent)
                sync_directory(new.parent)
                for name, content in row["leases"].items():
                    replace_file(new / "writer-leases" / name, bytes.fromhex(content["after"]))
            for entry in self.document["entrypoints"]:
                if entry["after"] != REFUSAL.hex():
                    replace_file(Path(entry["path"]), bytes.fromhex(entry["after"]), 0o755)
            self.document["phase"] = "migrated"
            self.save()

    def rollback(self) -> None:
        if self.document["phase"] not in {"prepared", "applying", "migrated", "activated"}:
            raise LayoutError("layout receipt cannot be rolled back")
        with ExitStack() as stack:
            replacements = {}
            for row in self.document["repositories"]:
                old, new = Path(row["old"]), Path(row["new"])
                if (old.exists() or old.is_symlink()) == (new.exists() or new.is_symlink()):
                    raise LayoutError("layout recovery has overlapping or missing namespaces")
                current = new if new.exists() else old
                info = owned(current, directory=True)
                if [info.st_dev, info.st_ino] != row["identity"]:
                    raise LayoutError("namespace identity changed before rollback")
                owned(current / "repo.lock")
                handle = stack.enter_context((current / "repo.lock").open("rb"))
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                quiet(current, Path(row["canonical"]))
                if relocation.inventory(current) != row["references"]["files"]:
                    raise LayoutError("new or changed cleanup evidence blocks rollback")
                if (current / relocation.RECORD).exists():
                    value, _ = evidence.read(current / relocation.RECORD)
                    if value != row["references"]:
                        raise LayoutError("relocation evidence changed before rollback")
                old_policy, new_policy = Path(row["old_policy"]), Path(row["new_policy"])
                if (old_policy.exists() or old_policy.is_symlink()) == (
                    new_policy.exists() or new_policy.is_symlink()
                ):
                    raise LayoutError("layout recovery has overlapping or missing policies")
                policy = new_policy if new_policy.exists() else old_policy
                owned(policy)
                if digest(policy.read_bytes()) != row["policy_sha256"]:
                    raise LayoutError("policy drift blocks rollback")
                if current == new:
                    for path in (new / "writer-leases").glob("*.json"):
                        owned(path)
                        raw = path.read_bytes()
                        original = row["leases"].get(path.name)
                        if original and raw.hex() in {original["before"], original["after"]}:
                            replacements[path] = bytes.fromhex(original["before"])
                        else:
                            replacements[path] = lease_bytes(raw, reverse=True)
            for entry in self.document["entrypoints"]:
                path = Path(entry["path"])
                if path.exists() or path.is_symlink():
                    owned(path)
                raw = path.read_bytes().hex() if path.exists() else None
                if raw not in {entry["before"], entry["after"], REFUSAL.hex()}:
                    raise LayoutError("entry point drift blocks rollback")
            # New invocations must stop before their namespace moves back, too.
            # A running command leaves the entries fenced for a later recovery retry.
            current_entries = [
                entry for entry in self.document["entrypoints"] if entry["after"] != REFUSAL.hex()
            ]
            if any(Path(row["new"]).exists() for row in self.document["repositories"]):
                for entry in current_entries:
                    replace_file(Path(entry["path"]), REFUSAL, 0o755)
                refuse_running_entries({Path(entry["path"]).name[:15] for entry in current_entries})
            for path, raw in replacements.items():
                replace_file(path, raw)
            for row in reversed(self.document["repositories"]):
                old, new = Path(row["old"]), Path(row["new"])
                # Keep the reference map with the namespace through the inverse
                # rename too. A failed move must remain readable for a retry.
                if new.exists():
                    os.rename(new, old)
                    sync_directory(new.parent)
                    sync_directory(old.parent)
                # Only this activation's additive record is removed on rollback.
                # Original journals/receipts are moved back byte for byte.
                with p._absolute_directory(old) as chain:
                    if relocation.RECORD in os.listdir(chain[-1].fd):
                        value, _ = relocation._document(chain[-1], relocation.RECORD)
                        if value != row["references"]:
                            raise LayoutError("relocation evidence changed before rollback")
                        os.unlink(relocation.RECORD, dir_fd=chain[-1].fd)
                        os.fsync(chain[-1].fd)
                if Path(row["new_policy"]).exists():
                    os.rename(row["new_policy"], row["old_policy"])
                    sync_directory(Path(row["old_policy"]).parent)
                    sync_directory(Path(row["new_policy"]).parent)
            for entry in self.document["entrypoints"]:
                path = Path(entry["path"])
                if entry["before"] is None:
                    if path.exists():
                        path.unlink()
                        sync_directory(path.parent)
                else:
                    replace_file(path, bytes.fromhex(entry["before"]), entry["mode"])
            self.document["phase"] = "rolled-back"
            self.save()

    def finish(self) -> None:
        if self.document["phase"] != "activated":
            raise LayoutError("only a verified activation can retire previous entries")
        for entry in self.document["entrypoints"]:
            path = Path(entry["path"])
            owned(path)
            if not path.exists() or digest(path.read_bytes()) != digest(
                bytes.fromhex(entry["after"])
            ):
                raise LayoutError("entry point changed before retirement")
        for entry in self.document["entrypoints"]:
            if entry["previous"] and entry["after"] == REFUSAL.hex():
                Path(entry["path"]).unlink()
                sync_directory(Path(entry["path"]).parent)
        for row in self.document["repositories"]:
            for parent in (Path(row["old"]).parent, Path(row["old_policy"]).parent):
                if parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
        self.document["phase"] = "retired"
        self.save()
