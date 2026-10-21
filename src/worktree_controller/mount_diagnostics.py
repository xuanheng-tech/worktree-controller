"""Read-only mount and lock accessibility observations for doctor."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Any

from .models import ControllerError, RepositoryInventory
from .store import MetadataStore

MOUNT_CODES = {
    "repo": "REPOSITORY_MOUNT_READ_ONLY",
    "git_entry": "GIT_ENTRY_MOUNT_READ_ONLY",
    "git_common_dir": "GIT_COMMON_MOUNT_READ_ONLY",
    "lock_mount": "REPOSITORY_LOCK_MOUNT_READ_ONLY",
    "lock_parent_mount": "REPOSITORY_LOCK_PARENT_MOUNT_READ_ONLY",
}
ACCESS_ERRNOS = {errno.EACCES, errno.EPERM, errno.EROFS}


def _mount_state(path: Path) -> str:
    try:
        flags = os.statvfs(path).f_flag
    except OSError:
        return "UNKNOWN"
    return "READ_ONLY" if flags & os.ST_RDONLY else "RW"


def _mount_access_errno(path: Path) -> str | None:
    """Keep a failed statvfs reason without changing the v1 state helper."""
    try:
        os.statvfs(path)
    except OSError as exc:
        return errno.errorcode.get(exc.errno, "UNKNOWN")
    return None


def _lock_probe(path: Path) -> dict[str, str | None]:
    """Inspect the existing lock without creating it or changing its contents."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return {"shape": "MISSING", "writable": "UNKNOWN", "access_errno": None}
    except OSError as exc:
        return {
            "shape": "UNKNOWN",
            "writable": "UNKNOWN",
            "access_errno": errno.errorcode.get(exc.errno, "UNKNOWN"),
        }
    if stat.S_ISLNK(mode):
        return {"shape": "SYMLINK", "writable": "UNWRITABLE", "access_errno": None}
    if not stat.S_ISREG(mode):
        return {"shape": "NON_REGULAR", "writable": "UNWRITABLE", "access_errno": None}
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EISDIR}:
            shape = "SYMLINK" if exc.errno == errno.ELOOP else "NON_REGULAR"
            return {"shape": shape, "writable": "UNWRITABLE", "access_errno": None}
        return {
            "shape": "REGULAR",
            "writable": "UNWRITABLE" if exc.errno in ACCESS_ERRNOS else "UNKNOWN",
            "access_errno": errno.errorcode.get(exc.errno, "UNKNOWN"),
        }
    try:
        try:
            observed_mode = os.fstat(descriptor).st_mode
        except OSError as exc:
            return {
                "shape": "UNKNOWN",
                "writable": "UNKNOWN",
                "access_errno": errno.errorcode.get(exc.errno, "UNKNOWN"),
            }
        if not stat.S_ISREG(observed_mode):
            return {"shape": "NON_REGULAR", "writable": "UNWRITABLE", "access_errno": None}
    finally:
        os.close(descriptor)
    return {"shape": "REGULAR", "writable": "WRITABLE", "access_errno": None}


def _lock_state(path: Path) -> str:
    """Preserve the v1 probe helper for callers that only need writability."""
    return str(_lock_probe(path)["writable"])


def _namespace_id(pid: int | str) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/ns/mnt")
    except OSError:
        return None


def _effective_lock_mount(states: dict[str, Any]) -> str:
    if states["lock_shape"] in {"MISSING", "SYMLINK", "NON_REGULAR"}:
        return str(states["lock_parent_mount"])
    return str(states["lock_mount"])


def classify_environment(diagnostics: dict[str, Any]) -> dict[str, Any]:
    """Classify observable access without attributing a namespace to a sandbox."""
    current = diagnostics["current"]
    namespace = diagnostics["namespace"]
    peer = namespace["peer"]
    observers = [("current", current)] + ([("peer", peer)] if peer is not None else [])
    facts: list[str] = []

    if namespace["comparison"] == "DIFFERENT" and peer is not None:
        for name in ("repo", "git_entry", "git_common_dir", "lock_mount", "lock_parent_mount"):
            current_state = (
                _effective_lock_mount(current) if name == "lock_mount" else current[name]
            )
            peer_state = _effective_lock_mount(peer) if name == "lock_mount" else peer[name]
            if (
                current_state in {"RW", "READ_ONLY"}
                and peer_state in {"RW", "READ_ONLY"}
                and current_state != peer_state
            ):
                facts.append(f"{name.upper()}_MOUNT_DIFFERS")
                if current_state == "READ_ONLY":
                    facts.append(f"CURRENT_{MOUNT_CODES[name]}")
                if peer_state == "READ_ONLY":
                    facts.append(f"PEER_{MOUNT_CODES[name]}")
    if facts:
        return {"code": "MOUNT_NAMESPACE_MISMATCH", "facts": facts}

    for observer, states in observers:
        for name, code in MOUNT_CODES.items():
            if name == "lock_mount" and states["lock_shape"] in {
                "MISSING",
                "SYMLINK",
                "NON_REGULAR",
            }:
                continue
            if states[name] == "READ_ONLY":
                facts.append(f"{observer.upper()}_{code}")
        if observer == "current":
            for name, access_errno in states["mount_access_errno"].items():
                if access_errno in {"EACCES", "EPERM", "EROFS"}:
                    facts.append(f"CURRENT_{name.upper()}_MOUNT_ACCESS_{access_errno}")
            if states["lock_access_errno"] in {"EACCES", "EPERM", "EROFS"}:
                facts.append(f"CURRENT_LOCK_ACCESS_{states['lock_access_errno']}")
    if facts:
        return {"code": "FILESYSTEM_PERMISSION", "facts": facts}

    for observer, states in observers:
        for name in ("repo", "git_entry", "git_common_dir", "lock_parent_mount"):
            if states[name] == "UNKNOWN":
                access_errno = states["mount_access_errno"][name] or "UNKNOWN"
                facts.append(f"{observer.upper()}_{name.upper()}_MOUNT_ACCESS_{access_errno}")
        if _effective_lock_mount(states) == "UNKNOWN":
            access_errno = states["mount_access_errno"]["lock_mount"] or "UNKNOWN"
            facts.append(f"{observer.upper()}_LOCK_MOUNT_ACCESS_{access_errno}")
        if states["lock_shape"] == "UNKNOWN":
            facts.append(f"{observer.upper()}_LOCK_SHAPE_UNKNOWN")
        if states["lock_shape"] == "REGULAR" and states["lock_writable"] == "UNKNOWN":
            facts.append(f"{observer.upper()}_LOCK_ACCESS_UNKNOWN")
        if observer == "peer" and states["lock_access_errno"] is not None:
            facts.append(f"PEER_LOCK_ACCESS_{states['lock_access_errno']}")
    if namespace["pid"] is not None and namespace["comparison"] == "UNKNOWN":
        facts.append("NAMESPACE_COMPARISON_UNKNOWN")
    return {"code": "UNKNOWN" if facts else "HEALTHY", "facts": facts}


def diagnose_mounts(
    repository: RepositoryInventory, *, namespace_pid: int | None = None
) -> dict[str, Any]:
    """Observe the caller and, when supplied, a process's mount namespace.

    Opening an existing lock O_RDWR tests access without creating or modifying it.
    A missing lock uses its parent mount for environment classification; an
    inaccessible mount remains UNKNOWN. Lock shape is reported separately.
    """
    if namespace_pid is not None and namespace_pid <= 0:
        raise ControllerError("INVALID_NAMESPACE_PID", "namespace_pid must be positive")
    store = MetadataStore(repository.common_dir)
    paths = {
        "repo": repository.requested_path,
        "git_entry": repository.requested_path / ".git",
        "git_common_dir": repository.common_dir,
        "lock_path": store.lock_file,
        "lock_parent": store.lock_file.parent,
    }

    def probe(root: Path | None) -> dict[str, Any]:
        observed = {
            name: root / path.relative_to("/") if root else path for name, path in paths.items()
        }
        lock = _lock_probe(observed["lock_path"])
        mount_paths = {
            "repo": observed["repo"],
            "git_entry": observed["git_entry"],
            "git_common_dir": observed["git_common_dir"],
            "lock_mount": observed["lock_path"],
            "lock_parent_mount": observed["lock_parent"],
        }
        mounts = {name: _mount_state(path) for name, path in mount_paths.items()}
        return {
            **mounts,
            "mount_access_errno": {
                name: _mount_access_errno(path) if mounts[name] == "UNKNOWN" else None
                for name, path in mount_paths.items()
            },
            "lock_writable": lock["writable"],
            "lock_shape": lock["shape"],
            "lock_access_errno": lock["access_errno"],
        }

    current = probe(None)
    self_ns = _namespace_id("self")
    peer_ns = _namespace_id(namespace_pid) if namespace_pid is not None else None
    comparison = (
        "UNKNOWN"
        if namespace_pid is None or self_ns is None or peer_ns is None
        else "SAME"
        if self_ns == peer_ns
        else "DIFFERENT"
    )
    peer = probe(Path(f"/proc/{namespace_pid}/root")) if peer_ns is not None else None
    diagnostics = {
        "paths": {name: str(path) for name, path in paths.items()},
        "current": current,
        "namespace": {
            "pid": namespace_pid,
            "current_id": self_ns,
            "peer_id": peer_ns,
            "comparison": comparison,
            "peer": peer,
        },
    }
    diagnostics["environment"] = classify_environment(diagnostics)
    return diagnostics


def mount_warnings(diagnostics: dict[str, Any]) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    observed = [("current", diagnostics["current"])]
    peer = diagnostics["namespace"]["peer"]
    if peer is not None:
        observed.append(("peer", peer))
    for observer, states in observed:
        for name, code in MOUNT_CODES.items():
            if name == "lock_mount" and states["lock_shape"] in {
                "MISSING",
                "SYMLINK",
                "NON_REGULAR",
            }:
                continue
            if states[name] == "READ_ONLY":
                warnings.append(
                    {
                        "code": code,
                        "message": f"{observer} {name} mount is read-only",
                        "severity": "blocking",
                    }
                )
            elif states[name] == "UNKNOWN":
                access_errno = states["mount_access_errno"][name]
                current_permission_denied = observer == "current" and access_errno in {
                    "EACCES",
                    "EPERM",
                    "EROFS",
                }
                warnings.append(
                    {
                        "code": "FILESYSTEM_PERMISSION"
                        if current_permission_denied
                        else "MOUNT_AUTHORITY_UNKNOWN",
                        "message": f"{observer} {name} mount could not be checked"
                        + (f" ({access_errno})" if access_errno else ""),
                        "severity": "blocking" if current_permission_denied else "degraded",
                    }
                )
        if states["lock_shape"] in {"SYMLINK", "NON_REGULAR"}:
            warnings.append(
                {
                    "code": "REPOSITORY_LOCK_SHAPE_INVALID",
                    "message": f"{observer} lock path is not a regular file",
                    "severity": "blocking",
                }
            )
        elif states["lock_writable"] == "UNWRITABLE" and observer == "peer":
            warnings.append(
                {
                    "code": "LOCK_WRITABILITY_UNKNOWN",
                    "message": "peer lock path access could not establish writability",
                    "severity": "degraded",
                }
            )
        elif states["lock_writable"] == "UNWRITABLE":
            warnings.append(
                {
                    "code": "REPOSITORY_LOCK_UNWRITABLE",
                    "message": f"{observer} lock path cannot be opened for writing",
                    "severity": "blocking",
                }
            )
        elif states["lock_writable"] == "UNKNOWN" and states["lock_shape"] != "MISSING":
            warnings.append(
                {
                    "code": "LOCK_WRITABILITY_UNKNOWN",
                    "message": f"{observer} lock path writability could not be checked",
                    "severity": "degraded",
                }
            )
    if diagnostics["environment"]["code"] == "MOUNT_NAMESPACE_MISMATCH":
        warnings.append(
            {
                "code": "MOUNT_NAMESPACE_MISMATCH",
                "message": "caller and selected process observe different mount access",
                "severity": "blocking",
            }
        )
    if diagnostics["namespace"]["comparison"] == "DIFFERENT":
        warnings.append(
            {
                "code": "MOUNT_NAMESPACE_DIFFERENT",
                "message": "caller and selected process use different mount namespaces",
                "severity": "info",
            }
        )
    return warnings
