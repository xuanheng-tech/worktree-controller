"""Exact immutable evidence references across the explicit one-time layout move.

This is not metadata discovery: only an explicit layout or namespace migration's
hash-bound inventory can relocate a historical reference. Current metadata authority
has one namespace.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from . import preservation as p
from .disposable import _inode, _open_directory
from .models import ControllerError
from .preserved_cleanup import _document
from .store import ROOT_NAME, VERSION_NAME

CONTRACT = "metadata-reference-relocation/v1"
RECORD = "namespace-relocation.json"
DIRECTORIES = (
    "transactions",
    "completion-receipts",
    "source-cleanup",
    "cleanup-actions",
    "cleanup-recoveries",
)


def _blocked() -> ControllerError:
    return ControllerError("METADATA_RELOCATION_BLOCKED", "exact metadata relocation guard refused")


def inventory(root: Path) -> dict[str, str]:
    """Only immutable cleanup JSON evidence; no provider files or mutable records."""
    result = {}

    def walk(directory: p._Directory, prefix: str) -> None:
        directory.check()
        names = sorted(os.listdir(directory.fd))
        for name in names:
            info = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
            relative = f"{prefix}/{name}"
            if stat.S_ISDIR(info.st_mode):
                with _open_directory(directory.fd, name, _inode(info)) as child:
                    walk(child, relative)
            elif stat.S_ISREG(info.st_mode) and name.endswith(".json"):
                _, result[relative] = _document(directory, name)
            else:
                raise _blocked()
        directory.check()
        if sorted(os.listdir(directory.fd)) != names:
            raise _blocked()

    with p._absolute_directory(root) as chain:
        parent = chain[-1]
        for name in DIRECTORIES:
            if name in os.listdir(parent.fd):
                info = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
                with _open_directory(parent.fd, name, _inode(info)) as child:
                    walk(child, name)
    return result


def prepare(old: Path, new: Path) -> dict:
    with p._absolute_directory(old) as chain:
        info = os.fstat(chain[-1].fd)
    value = {
        "contract": CONTRACT,
        "previous_root": str(old),
        "current_root": str(new),
        "root_identity": [info.st_dev, info.st_ino],
        "files": inventory(old),
    }
    return {**value, "sha256": p._sha(value)}


def _record(root: Path) -> dict:
    with p._absolute_directory(root) as chain:
        value, _ = _document(chain[-1], RECORD)
        info = os.fstat(chain[-1].fd)
    old = Path(value["previous_root"])
    if (
        value.get("contract") != CONTRACT
        or value.get("sha256") != p._sha({k: v for k, v in value.items() if k != "sha256"})
        or value.get("current_root") != str(root)
        or value.get("root_identity") != [info.st_dev, info.st_ino]
        or not old.is_absolute()
        or old.parent.parent != root.parent.parent
        or old.name != VERSION_NAME
        or old == root
        or os.path.lexists(old)
    ):
        raise _blocked()
    return value


def relocated(path: Path, digest: str | None) -> Path:
    """Resolve a missing old reference only through an exact recorded file digest."""
    if not digest or not path.is_absolute() or ".." in path.parts:
        return path
    for old in path.parents:
        if old.name != VERSION_NAME:
            continue
        root = old.parent.parent / ROOT_NAME / VERSION_NAME
        if root == old or not os.path.lexists(root / RECORD):
            continue
        value = _record(root)
        if value["previous_root"] != str(old):
            continue
        relative = path.relative_to(old).as_posix()
        if value["files"].get(relative) != digest:
            raise _blocked()
        return root / relative
    return path


def historical_reference(path: Path, digest: str) -> dict[str, str]:
    """Preserve reference identities used inside already immutable attempt chains."""
    for root in path.parents:
        if root.name != VERSION_NAME or root.parent.name != ROOT_NAME:
            continue
        if not os.path.lexists(root / RECORD):
            break
        value = _record(root)
        relative = path.relative_to(root).as_posix()
        if value["files"].get(relative) == digest:
            path = Path(value["previous_root"]) / relative
        break
    return {"path": str(path), "sha256": digest}
