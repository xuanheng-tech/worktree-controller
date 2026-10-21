"""Release-time inspection for ignored artifacts and Python environments."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .git_backend import head_paths, tracked_paths
from .models import ControllerError, RepositoryInventory, WriterHolder
from .writer_lease import boot_id

SAFE_CACHE_PARTS = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".pyright",
        ".hypothesis",
    }
)
SAFE_CACHE_SUFFIXES = (".pyc", ".pyo")
REFERENCE_NAMES = frozenset({"direct_url.json"})
REFERENCE_SUFFIXES = (".pth",)
MAX_REFERENCE_FILE_BYTES = 1_048_576
MAX_REFERENCE_FILES = 20_000
MAX_VENV_ENTRIES = 500_000
MAX_RUNTIME_PROCESSES = 16_384
MAX_RUNTIME_FDS = 200_000
MAX_RUNTIME_MAP_BYTES = 256 * 1_048_576
CACHEDIR_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"


def unsafe_ignored_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    unsafe: list[str] = []
    for raw in paths:
        path = PurePosixPath(raw)
        if any(part in SAFE_CACHE_PARTS for part in path.parts):
            continue
        if path.name.endswith(SAFE_CACHE_SUFFIXES):
            continue
        unsafe.append(raw)
    return tuple(unsafe)


def _candidate_reference_file(path: Path) -> bool:
    name = path.name
    return (
        name in REFERENCE_NAMES
        or name.endswith(REFERENCE_SUFFIXES)
        or name.startswith("__editable__")
    )


def _environment_roots(
    repository: RepositoryInventory, excluded_roots: frozenset[Path]
) -> tuple[Path, ...]:
    roots: set[Path] = set()
    for worktree in repository.worktrees:
        if worktree.path.exists():
            for venv_name in (".venv", "backend/.venv"):
                candidate = worktree.path / venv_name
                if candidate.is_dir() and not candidate.is_symlink():
                    resolved = candidate.resolve(strict=True)
                    if resolved not in excluded_roots:
                        roots.add(resolved)
    return tuple(sorted(roots))


def editable_references(
    repository: RepositoryInventory,
    target: Path,
    *,
    exclude_environments: tuple[Path, ...] = (),
) -> tuple[dict[str, str], ...]:
    target_bytes = os.fsencode(target.resolve(strict=True))
    excluded_roots = frozenset(
        path.resolve(strict=True) for path in exclude_environments if path.exists()
    )
    matches: list[dict[str, str]] = []
    inspected = 0
    def scan_error(error: OSError) -> None:
        raise ControllerError(
            "ENVIRONMENT_SCAN_ERROR", f"cannot enumerate environment: {error.filename}"
        ) from error

    for root in _environment_roots(repository, excluded_roots):
        for directory, directories, files in os.walk(root, followlinks=False, onerror=scan_error):
            directories[:] = [
                name for name in directories if not (Path(directory) / name).is_symlink()
            ]
            for name in files:
                path = Path(directory) / name
                if not _candidate_reference_file(path):
                    continue
                inspected += 1
                if inspected > MAX_REFERENCE_FILES:
                    raise ControllerError(
                        "ENVIRONMENT_SCAN_LIMIT", "editable reference scan exceeded its bound"
                    )
                payload = _reference_payload(path)
                if target_bytes in payload:
                    matches.append({"environment": str(root), "file": str(path)})
    return tuple(matches)


def _reference_payload(path: Path) -> bytes:
    """An unexamined candidate is uncertainty, never evidence of no dependency."""
    try:
        # Do not follow links or wait on a FIFO/device disguised as a .pth file.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ControllerError(
                    "ENVIRONMENT_SCAN_ERROR", f"environment reference is not a regular file: {path}"
                )
            if info.st_size > MAX_REFERENCE_FILE_BYTES:
                raise ControllerError(
                    "ENVIRONMENT_SCAN_LIMIT", f"environment reference exceeds size bound: {path}"
                )
            payload = stream.read(MAX_REFERENCE_FILE_BYTES + 1)
            if len(payload) > MAX_REFERENCE_FILE_BYTES:
                raise ControllerError(
                    "ENVIRONMENT_SCAN_LIMIT", f"environment reference grew beyond size bound: {path}"
                )
            return payload
    except OSError as exc:
        raise ControllerError(
            "ENVIRONMENT_SCAN_ERROR", f"cannot inspect environment reference: {path}"
        ) from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1_048_576):
                digest.update(chunk)
    except OSError as exc:
        raise ControllerError(
            "RESOURCE_IDENTITY_MISMATCH", f"cannot read identity file: {path}"
        ) from exc
    return digest.hexdigest()


def _pyvenv_identity(path: Path) -> tuple[str | None, str | None]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ControllerError("RESOURCE_IDENTITY_MISMATCH", "pyvenv.cfg is unreadable") from exc
    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip().lower()] = value.strip()
    version = values.get("version") or values.get("version_info")
    implementation = values.get("implementation")
    return version, implementation


def _venv_inventory(root: Path) -> tuple[int, int, str]:
    total_bytes = 0
    entries = 0
    identity = hashlib.sha256()
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as scan:
                children = sorted(scan, key=lambda entry: entry.name)
        except OSError as exc:
            raise ControllerError(
                "RESOURCE_IDENTITY_MISMATCH", "cannot inventory reproducible .venv"
            ) from exc
        for child in children:
            entries += 1
            if entries > MAX_VENV_ENTRIES:
                raise ControllerError(
                    "RESOURCE_SCAN_LIMIT", "reproducible .venv inventory exceeded its bound"
                )
            path = Path(child.path)
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise ControllerError(
                    "RESOURCE_IDENTITY_MISMATCH", "cannot stat reproducible .venv content"
                ) from exc
            total_bytes += metadata.st_size
            relative = path.relative_to(root).as_posix()
            identity.update(os.fsencode(relative))
            identity.update(b"\0")
            identity.update(
                f"{metadata.st_mode}:{metadata.st_size}:{metadata.st_mtime_ns}".encode("ascii")
            )
            identity.update(b"\0")
            if child.is_symlink():
                try:
                    identity.update(os.fsencode(os.readlink(path)))
                except OSError as exc:
                    raise ControllerError(
                        "RESOURCE_IDENTITY_MISMATCH",
                        "cannot inspect reproducible .venv symlink",
                    ) from exc
                continue
            if child.is_dir(follow_symlinks=False):
                if path.is_mount():
                    raise ControllerError(
                        "RESOURCE_IDENTITY_MISMATCH", "reproducible .venv contains a mount"
                    )
                pending.append(path)
            elif not stat.S_ISREG(metadata.st_mode):
                raise ControllerError(
                    "RESOURCE_IDENTITY_MISMATCH",
                    "reproducible .venv contains a special filesystem object",
                )
    return total_bytes, entries, identity.hexdigest()


def discover_python_project(
    worktree: Path,
    *,
    venv_relative_path: str = ".venv",
) -> tuple[str, str]:
    """Discover and validate the python project (pyproject.toml, uv.lock) for a worktree environment.

    Returns (project_path, lock_path) as POSIX repository-relative strings.
    Fails closed if the project or lockfile is missing, ambiguous, untracked, or escapes the root.
    """
    root = worktree.resolve(strict=True)
    venv_rel = PurePosixPath(venv_relative_path)
    if venv_rel.is_absolute() or any(part in {"..", ""} for part in venv_rel.parts):
        raise ControllerError(
            "RESOURCE_PATH_ESCAPE", f"invalid venv relative path: {venv_relative_path}"
        )

    venv_parent = venv_rel.parent.as_posix()

    cand_dir: str | None = None
    if venv_parent not in {".", ""}:
        cand_project = f"{venv_parent}/pyproject.toml"
        cand_project_file = root / cand_project
        if not cand_project_file.exists():
            raise ControllerError(
                "PROJECT_IDENTITY_MISMATCH",
                f"reproducible teardown requires tracked {cand_project}",
            )
        cand_dir = venv_parent
    else:
        root_project_file = root / "pyproject.toml"
        if root_project_file.exists():
            cand_dir = ""
        else:
            tracked = tracked_paths(root, "*pyproject.toml")
            candidates = sorted(
                p
                for p in tracked
                if PurePosixPath(p).name == "pyproject.toml" and not PurePosixPath(p).is_absolute()
            )
            if len(candidates) == 0:
                raise ControllerError(
                    "PROJECT_IDENTITY_MISMATCH",
                    "reproducible teardown requires tracked pyproject.toml",
                )
            if len(candidates) > 1:
                raise ControllerError(
                    "PROJECT_IDENTITY_MISMATCH",
                    f"ambiguous python project candidates: {', '.join(candidates)}",
                )
            cand_dir = PurePosixPath(candidates[0]).parent.as_posix()
            if cand_dir == ".":
                cand_dir = ""

    project_rel = f"{cand_dir}/pyproject.toml" if cand_dir else "pyproject.toml"
    lock_rel = f"{cand_dir}/uv.lock" if cand_dir else "uv.lock"

    project_path = root / project_rel
    lock_path = root / lock_rel

    for rel_path, abs_path in ((project_rel, project_path), (lock_rel, lock_path)):
        if abs_path.is_symlink():
            raise ControllerError("RESOURCE_PATH_ESCAPE", f"{rel_path} is a symlink")
        try:
            if abs_path.resolve(strict=True) != abs_path:
                raise ControllerError("RESOURCE_PATH_ESCAPE", f"{rel_path} escapes its root")
        except OSError as exc:
            raise ControllerError(
                "PROJECT_IDENTITY_MISMATCH",
                f"reproducible teardown requires tracked {rel_path}",
            ) from exc
        if not abs_path.is_file():
            raise ControllerError(
                "PROJECT_IDENTITY_MISMATCH",
                f"reproducible teardown requires regular {rel_path}",
            )
        if tracked_paths(root, rel_path) != (rel_path,) or head_paths(root, rel_path) != (
            rel_path,
        ):
            raise ControllerError(
                "PROJECT_IDENTITY_MISMATCH",
                f"reproducible teardown requires tracked {rel_path}",
            )

    return project_rel, lock_rel


def reproducible_venv_evidence(
    worktree: Path,
    *,
    venv_relative: str = ".venv",
) -> dict[str, Any]:
    """Return bounded deletion evidence for the reproducible worktree .venv."""

    root = worktree.resolve(strict=True)
    venv = root / venv_relative
    if venv.is_symlink() or not venv.is_dir() or venv.is_mount():
        raise ControllerError(
            "RESOURCE_IDENTITY_MISMATCH",
            f"worktree {venv_relative} is missing, a symlink, or a mount",
        )
    if venv.resolve(strict=True) != venv:
        raise ControllerError(
            "RESOURCE_IDENTITY_MISMATCH", f"worktree {venv_relative} escaped its root"
        )
    marker = venv / "pyvenv.cfg"
    cache_tag = venv / "CACHEDIR.TAG"
    project_rel, lock_rel = discover_python_project(root, venv_relative_path=venv_relative)
    lock = root / lock_rel
    project = root / project_rel
    required = (marker, cache_tag, lock, project)
    if any(path.is_symlink() or not path.is_file() for path in required):
        raise ControllerError(
            "RESOURCE_IDENTITY_MISMATCH",
            f"reproducible {venv_relative} requires regular pyvenv.cfg, CACHEDIR.TAG, {lock_rel}, and {project_rel}",
        )
    try:
        cache_payload = cache_tag.read_bytes()
    except OSError as exc:
        raise ControllerError("RESOURCE_IDENTITY_MISMATCH", "CACHEDIR.TAG is unreadable") from exc
    if CACHEDIR_SIGNATURE not in cache_payload:
        raise ControllerError(
            "RESOURCE_IDENTITY_MISMATCH", "CACHEDIR.TAG lacks the standard cache signature"
        )
    if (venv / ".git").exists() or (venv / ".git").is_symlink():
        raise ControllerError(
            "RESOURCE_IDENTITY_MISMATCH", f"worktree {venv_relative} contains Git metadata"
        )
    python = venv / "bin" / "python"
    try:
        python_target = python.resolve(strict=True)
        python_stat = python_target.stat()
    except OSError as exc:
        raise ControllerError(
            "RESOURCE_IDENTITY_MISMATCH",
            f"reproducible {venv_relative} Python executable is unavailable",
        ) from exc
    if not python_target.is_file() or not os.access(python_target, os.X_OK):
        raise ControllerError(
            "RESOURCE_IDENTITY_MISMATCH",
            f"reproducible {venv_relative} Python target is not executable",
        )
    version, implementation = _pyvenv_identity(marker)
    if version is None:
        raise ControllerError("RESOURCE_IDENTITY_MISMATCH", "pyvenv.cfg has no Python version")
    try:
        venv_stat = venv.stat()
    except OSError as exc:
        raise ControllerError(
            "RESOURCE_IDENTITY_MISMATCH", f"cannot stat reproducible {venv_relative} root"
        ) from exc
    bytes_before, entries_before, tree_identity = _venv_inventory(venv)
    python_identity = (
        f"{python_target}:{python_stat.st_dev}:{python_stat.st_ino}:"
        f"{python_stat.st_size}:{python_stat.st_mtime_ns}"
    )
    environment_identity = hashlib.sha256(
        (
            _sha256(marker)
            + _sha256(cache_tag)
            + hashlib.sha256(python_identity.encode("utf-8")).hexdigest()
            + tree_identity
        ).encode("ascii")
    ).hexdigest()
    return {
        "environment_path": venv_relative,
        "pyvenv_cfg_sha256": _sha256(marker),
        "cachedir_tag_sha256": _sha256(cache_tag),
        "lock_path": lock_rel,
        "lock_sha256": _sha256(lock),
        "project_path": project_rel,
        "project_sha256": _sha256(project),
        "python_version": version,
        "python_implementation": implementation,
        "python_identity_sha256": hashlib.sha256(python_identity.encode("utf-8")).hexdigest(),
        "environment_identity_sha256": environment_identity,
        "environment_device": venv_stat.st_dev,
        "environment_inode": venv_stat.st_ino,
        "bytes_before": bytes_before,
        "entries_before": entries_before,
    }


def _path_is_within(raw: str, roots: tuple[Path, ...]) -> bool:
    candidate = raw.removesuffix(" (deleted)")
    if not candidate.startswith("/"):
        return False
    # Preserve commonpath's lexical rules (including literal '..'), without
    # splitting every ordinary procfs path once per root and mapping entry.
    if "//" in candidate or "/./" in candidate or candidate.endswith("/."):
        candidate = "/" + "/".join(part for part in candidate.split("/") if part and part != ".")
    try:
        for root in roots:
            prefix = str(root)
            if not prefix.startswith("/"):
                return False
            if candidate.startswith(prefix) and (
                prefix == "/" or len(candidate) == len(prefix) or candidate[len(prefix)] == "/"
            ):
                return True
    except (OSError, ValueError):
        return False
    return False


def _runtime_link(path: Path) -> str | None:
    try:
        return os.readlink(path)
    except (FileNotFoundError, PermissionError):
        return None
    except OSError as exc:
        raise ControllerError("RUNTIME_SCAN_ERROR", f"cannot inspect runtime link: {path}") from exc


def runtime_references(worktree: Path, *, strict: bool = False) -> tuple[dict[str, str], ...]:
    """Inspect same-UID Linux processes for references into the checkout or its .venv.

    The release scan shares the typed cleanup policy for permission gaps: a
    same-UID process whose procfs entries deny access (for example a non-dumpable
    service manager) is a host visibility gap, not a reference; every other read
    failure fails closed. ``strict`` additionally refuses any such gap. In both
    modes the checkout must keep one identity for the whole scan.
    """

    if strict:
        return _strict_runtime_references(worktree)
    try:
        root = worktree.resolve(strict=True)
        before = root.stat()
    except (OSError, RuntimeError) as exc:
        raise ControllerError("RUNTIME_SCAN_ERROR", "cannot identify checkout for runtime scan") from exc
    references = _lenient_runtime_references(root)
    try:
        after = root.stat()
        changed = worktree.resolve(strict=True) != root or (before.st_dev, before.st_ino) != (
            after.st_dev,
            after.st_ino,
        )
    except (OSError, RuntimeError) as exc:
        raise ControllerError(
            "RUNTIME_SCAN_ERROR", "checkout disappeared during runtime scan"
        ) from exc
    if changed:
        raise ControllerError("RUNTIME_SCAN_ERROR", "checkout path/identity changed during runtime scan")
    return references


def _lenient_runtime_references(root: Path) -> tuple[dict[str, str], ...]:
    roots = (root, root / ".venv")
    try:
        processes = sorted(
            (path for path in Path("/proc").iterdir() if path.name.isdecimal()),
            key=lambda path: int(path.name),
        )
    except OSError as exc:
        raise ControllerError("RUNTIME_SCAN_ERROR", "cannot enumerate Linux processes") from exc
    if len(processes) > MAX_RUNTIME_PROCESSES:
        raise ControllerError("RUNTIME_SCAN_LIMIT", "runtime process scan exceeded its bound")
    references: list[dict[str, str]] = []
    inspected_fds = 0
    inspected_map_bytes = 0
    for process in processes:
        try:
            if process.stat().st_uid != os.getuid():
                continue
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ControllerError("RUNTIME_SCAN_ERROR", "cannot inspect process ownership") from exc
        for kind in ("cwd", "exe"):
            value = _runtime_link(process / kind)
            if value is not None and _path_is_within(value, roots):
                references.append({"pid": process.name, "kind": kind, "path": value})
        try:
            descriptors = list((process / "fd").iterdir())
        except (FileNotFoundError, PermissionError):
            descriptors = []
        except OSError as exc:
            raise ControllerError(
                "RUNTIME_SCAN_ERROR", "cannot inspect process descriptors"
            ) from exc
        inspected_fds += len(descriptors)
        if inspected_fds > MAX_RUNTIME_FDS:
            raise ControllerError(
                "RUNTIME_SCAN_LIMIT", "runtime descriptor scan exceeded its bound"
            )
        for descriptor in descriptors:
            value = _runtime_link(descriptor)
            if value is not None and _path_is_within(value, roots):
                references.append({"pid": process.name, "kind": "fd", "path": value})
        try:
            with (process / "maps").open("rb") as mappings:
                for line in mappings:
                    inspected_map_bytes += len(line)
                    if inspected_map_bytes > MAX_RUNTIME_MAP_BYTES:
                        raise ControllerError(
                            "RUNTIME_SCAN_LIMIT", "runtime mapping scan exceeded its bound"
                        )
                    fields = line.split(maxsplit=5)
                    if len(fields) == 6:
                        value = os.fsdecode(fields[5].strip())
                        if _path_is_within(value, roots):
                            references.append({"pid": process.name, "kind": "mmap", "path": value})
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        except ControllerError:
            raise
        except OSError as exc:
            raise ControllerError("RUNTIME_SCAN_ERROR", "cannot inspect process mappings") from exc
    return tuple(references)


_PROC_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_VANISHED_ERRNOS = frozenset({errno.ENOENT, errno.ESRCH})
_DENIED_ERRNOS = frozenset({errno.EACCES, errno.EPERM})
_MAX_PROCESS_ATTEMPTS = 3
_VANISHED_RESCAN_SECONDS = 0.05


@dataclass(frozen=True)
class _ProcessIdentity:
    inode: tuple[int, int]
    start_ticks: int
    state: str
    threads: int
    uids: tuple[int, ...]
    ppid: int
    session_id: int = 0

    @property
    def terminal(self) -> bool:
        # Linux do_exit releases mm/files/fs before EXIT_ZOMBIE. A zombie leader
        # with other threads is NOT proof that the process has no references.
        return self.state in {"Z", "X"} and self.threads == 1


@dataclass(frozen=True)
class _RuntimeIssue:
    field: str
    code: str
    errno: int | None = None

    def __str__(self) -> str:
        suffix = f"({self.errno})" if self.errno is not None else ""
        return f"{self.field}:{self.code}{suffix}"


@dataclass(frozen=True)
class _ProcessScan:
    path: Path
    identity: _ProcessIdentity | None
    references: tuple[dict[str, str], ...] = ()
    issues: tuple[_RuntimeIssue, ...] = ()

    def diagnostic(self) -> str:
        ticks = f" start_ticks={self.identity.start_ticks}" if self.identity else ""
        return f"pid={self.path.name}{ticks} {','.join(map(str, self.issues))}"


def _same_process(left: _ProcessIdentity, right: _ProcessIdentity | None) -> bool:
    # A resizing thread pool is the same process; becoming single-threaded (an exec
    # from a non-leader thread keeps the PID and start time) or multi-threaded is not.
    return right is not None and (
        left.inode,
        left.start_ticks,
        left.uids,
        left.threads > 1,
        left.ppid,
        left.session_id,
    ) == (right.inode, right.start_ticks, right.uids, right.threads > 1, right.ppid, right.session_id)


def _proc_bytes(directory: int, name: str) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    with os.fdopen(fd, "rb") as handle:
        payload = handle.read(65_537)
    if len(payload) > 65_536:
        raise ValueError(f"oversized {name}")
    return payload


def _process_identity(directory: int, pid: str) -> _ProcessIdentity:
    raw = _proc_bytes(directory, "stat")
    prefix, separator, tail = raw.rpartition(b") ")
    fields = tail.split()
    if not separator or not prefix.startswith(os.fsencode(pid) + b" (") or len(fields) < 20:
        raise ValueError("malformed stat")
    state = fields[0].decode("ascii")
    threads, start_ticks = int(fields[17]), int(fields[19])
    if state not in {"R", "S", "D", "Z", "T", "t", "X", "I", "P"} or threads < 1 or start_ticks < 0:
        raise ValueError("malformed stat identity")
    rows = [
        line.split()[1:]
        for line in _proc_bytes(directory, "status").splitlines()
        if line.startswith(b"Uid:")
    ]
    if len(rows) != 1 or len(rows[0]) != 4:
        raise ValueError("malformed status Uid")
    uids = tuple(int(value) for value in rows[0])
    if any(value < 0 for value in uids):
        raise ValueError("malformed status Uid")
    metadata = os.fstat(directory)
    ppid = int(fields[1])
    if ppid < 0:
        raise ValueError("malformed parent pid")
    return _ProcessIdentity(
        (metadata.st_dev, metadata.st_ino), start_ticks, state, threads, uids, ppid, int(fields[3])
    )


def _current_process_identity(process: Path) -> _ProcessIdentity | None:
    for attempt in range(_MAX_PROCESS_ATTEMPTS):
        try:
            directory = os.open(process, _PROC_DIRECTORY_FLAGS)
        except OSError as exc:
            if exc.errno in _VANISHED_ERRNOS:
                return None
            raise
        try:
            return _process_identity(directory, process.name)
        except OSError as exc:
            if exc.errno not in _VANISHED_ERRNOS or attempt == _MAX_PROCESS_ATTEMPTS - 1:
                raise
            # Exit between opening the process directory and reading stat/status:
            # reopen the numeric PID before concluding that it vanished.
        finally:
            os.close(directory)
    raise AssertionError("unreachable process identity retry")


def verify_holder_lineage(holder: WriterHolder, process_id: int) -> None:
    """Prove the calling process is the recorded holder or its current descendant.

    Inspect only this ancestry, using the same fd-bound proc identity reader as
    cleanup. Recheck the whole chain to reject exit, reparenting and PID reuse.
    A declaration copied to an unrelated process grants no host-managed access.
    """
    chain: list[tuple[Path, _ProcessIdentity]] = []
    seen: set[int] = set()
    pid = process_id
    try:
        while pid > 0 and pid not in seen and len(chain) < MAX_RUNTIME_PROCESSES:
            seen.add(pid)
            path = Path(f"/proc/{pid}")
            identity = _current_process_identity(path)
            if identity is None or identity.state in {"Z", "X"}:
                break
            chain.append((path, identity))
            if pid == holder.pid:
                if holder.boot_id != boot_id() or identity.start_ticks != holder.start_ticks:
                    break
                if all(
                    _same_process(before, _current_process_identity(item)) for item, before in chain
                ):
                    return
                break
            pid = identity.ppid
    except (OSError, ValueError):
        pass
    raise ControllerError(
        "WRITER_LEASE_MISMATCH", "caller is not a verified descendant of the host holder"
    )


def verify_runtime_transfer(source: WriterHolder, target: WriterHolder) -> None:
    """A host-created Linux session remains identifiable across parent exit.

    The original session leader must still have its exact identity (it may be
    unreaped). A child cannot join a foreign Linux session. This permits transfer
    after kernel reparenting without trusting copied environment or command text.
    """
    paths = [Path(f"/proc/{holder.pid}") for holder in (source, target)]
    try:
        before = [_current_process_identity(path) for path in paths]
        if source.boot_id != boot_id() or target.boot_id != source.boot_id:
            raise ValueError("boot changed")
        if any(value is None for value in before):
            raise ValueError("process disappeared")
        leader, child = before
        if (
            leader.start_ticks != source.start_ticks
            or child.start_ticks != target.start_ticks
            or leader.session_id != source.pid
            or child.session_id != source.pid
            or leader.uids != child.uids
            or child.terminal
            or not all(
                _same_process(value, _current_process_identity(path))
                for value, path in zip(before, paths, strict=True)
            )
        ):
            raise ValueError("runtime session changed")
    except (OSError, ValueError):
        raise ControllerError(
            "RUNTIME_TRANSFER_UNPROVEN", "background process is not in the exact host session"
        ) from None


def _runtime_issue(field: str, exc: OSError | ValueError) -> _RuntimeIssue:
    if isinstance(exc, OSError):
        return _RuntimeIssue(field, errno.errorcode.get(exc.errno, "OS_ERROR"), exc.errno)
    return _RuntimeIssue(field, "MALFORMED_PROC_ENTRY")


def _strict_process_references(
    directory: int, pid: str, roots: tuple[Path, ...], budget: list[int]
) -> tuple[list[dict[str, str]], list[_RuntimeIssue]]:
    references: list[dict[str, str]] = []
    issues: list[_RuntimeIssue] = []

    def reference(kind: str, value: str) -> None:
        if _path_is_within(value, roots):
            references.append({"pid": pid, "kind": kind, "path": value})

    for kind in ("cwd", "exe"):
        try:
            reference(kind, os.readlink(kind, dir_fd=directory))
        except OSError as exc:
            issues.append(_runtime_issue(kind, exc))
    try:
        descriptors = os.open("fd", _PROC_DIRECTORY_FLAGS, dir_fd=directory)
        try:
            names = os.listdir(descriptors)
            budget[0] += len(names)
            if budget[0] > MAX_RUNTIME_FDS:
                raise ControllerError(
                    "RUNTIME_SCAN_LIMIT", "runtime descriptor scan exceeded its bound"
                )
            for name in names:
                if not name.isdecimal():
                    issues.append(_RuntimeIssue("fd", "MALFORMED_PROC_ENTRY"))
                    continue
                try:
                    reference("fd", os.readlink(name, dir_fd=descriptors))
                except OSError as exc:
                    if exc.errno in _VANISHED_ERRNOS:
                        try:
                            os.stat(name, dir_fd=descriptors, follow_symlinks=False)
                        except OSError as confirm:
                            if confirm.errno in _VANISHED_ERRNOS:
                                continue  # This descriptor closed, not an unreadable live link.
                            exc = confirm
                    issue = _runtime_issue("fd", exc)
                    if issue not in issues:
                        issues.append(issue)
        finally:
            os.close(descriptors)
    except OSError as exc:
        issues.append(_runtime_issue("fd", exc))
    try:
        fd = os.open("maps", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        with os.fdopen(fd, "rb") as mappings:
            for line in mappings:
                budget[1] += len(line)
                if budget[1] > MAX_RUNTIME_MAP_BYTES:
                    raise ControllerError(
                        "RUNTIME_SCAN_LIMIT", "runtime mapping scan exceeded its bound"
                    )
                fields = line.split(maxsplit=5)
                if len(fields) < 5 or not re.fullmatch(
                    rb"[0-9a-f]+-[0-9a-f]+ [r-][w-][x-][ps] [0-9a-f]+ [0-9a-f]+:[0-9a-f]+ [0-9]+",
                    b" ".join(fields[:5]),
                ):
                    raise ValueError("malformed maps")
                if len(fields) == 6:
                    reference("mmap", os.fsdecode(fields[5].rstrip(b"\n")))
    except (OSError, ValueError) as exc:
        issues.append(_runtime_issue("maps", exc))
    return references, issues


def _inspect_runtime_process(
    process: Path, roots: tuple[Path, ...], budget: list[int], *, inspect_foreign: bool = False
) -> _ProcessScan:
    last_issue = _RuntimeIssue("identity", "PID_IDENTITY_CHANGED")
    after = None
    for attempt in range(_MAX_PROCESS_ATTEMPTS):
        before: _ProcessIdentity | None = None
        references: list[dict[str, str]] = []
        issues: list[_RuntimeIssue] = []
        try:
            directory = os.open(process, _PROC_DIRECTORY_FLAGS)
            try:
                before = _process_identity(directory, process.name)
                if (os.getuid() in before.uids or inspect_foreign) and not before.terminal:
                    references, issues = _strict_process_references(
                        directory, process.name, roots, budget
                    )
            finally:
                os.close(directory)
        except (OSError, ValueError) as exc:
            last_issue = _runtime_issue("identity", exc)
            issues.append(last_issue)
        try:
            after = _current_process_identity(process)
        except (OSError, ValueError) as exc:
            return _ProcessScan(
                process,
                before,
                tuple(references),
                (*issues, _runtime_issue("identity-recheck", exc)),
            )
        if after is None:
            return _ProcessScan(process, None)  # Numeric PID absent, not just a missing field.
        if before is None or not _same_process(before, after):
            if before is not None:
                last_issue = _RuntimeIssue(
                    "identity",
                    "PID_REUSED"
                    if (before.inode, before.start_ticks) != (after.inode, after.start_ticks)
                    else "PID_IDENTITY_CHANGED",
                )
            continue  # Scan the replacement identity immediately, with a bounded retry.
        if after.terminal:
            return _ProcessScan(process, after)
        if before.terminal:
            return _ProcessScan(
                process, after, issues=(_RuntimeIssue("identity", "TERMINAL_STATE_CHANGED"),)
            )
        if (
            issues
            and attempt + 1 < _MAX_PROCESS_ATTEMPTS
            and any(issue.errno in _VANISHED_ERRNOS for issue in issues)
            and all(issue.errno in _VANISHED_ERRNOS | _DENIED_ERRNOS for issue in issues)
        ):
            # An exiting task loses its /proc fd/cwd entries before it turns into a
            # zombie, while its identity is still unchanged; without its mm the fd
            # directory also denies access. Rescan after a short pause: a finished
            # exit resolves to terminal or absent, a live process fails again.
            time.sleep(_VANISHED_RESCAN_SECONDS)
            continue
        return _ProcessScan(process, after, tuple(references), tuple(issues))
    return _ProcessScan(process, after, issues=(last_issue,))


def _runtime_process_scans(worktree: Path, budget: list[int]) -> tuple[_ProcessScan, ...]:
    try:
        root = worktree.resolve(strict=True)
        metadata = root.stat()
        identity = (metadata.st_dev, metadata.st_ino)
        processes = sorted(
            (path for path in Path("/proc").iterdir() if path.name.isdecimal()),
            key=lambda path: int(path.name),
        )
    except (OSError, RuntimeError) as exc:
        raise ControllerError(
            "RUNTIME_SCAN_UNCERTAIN", "cannot identify checkout or enumerate procfs"
        ) from exc
    if len(processes) > MAX_RUNTIME_PROCESSES:
        raise ControllerError("RUNTIME_SCAN_LIMIT", "runtime process scan exceeded its bound")
    scans = tuple(_inspect_runtime_process(process, (root,), budget) for process in processes)
    try:
        metadata = root.stat()
        if worktree.resolve(strict=True) != root or (metadata.st_dev, metadata.st_ino) != identity:
            raise ValueError("checkout identity changed")
    except (OSError, RuntimeError, ValueError) as exc:
        raise ControllerError(
            "RUNTIME_SCAN_UNCERTAIN", "checkout path/identity changed during runtime scan"
        ) from exc
    return scans


def _strict_runtime_references(worktree: Path) -> tuple[dict[str, str], ...]:
    scans = _runtime_process_scans(worktree, [0, 0])
    references = tuple(row for scan in scans for row in scan.references)
    if references:
        return references
    issues = [scan.diagnostic() for scan in scans if scan.issues]
    if issues:
        raise ControllerError(
            "RUNTIME_SCAN_UNCERTAIN", "cannot exclude checkout references: " + "; ".join(issues)
        )
    return ()


RUNTIME_AUTHORITY_CONTRACT = "allocation-runtime-authority/v1"


def _managed_lineage(
    scan: _ProcessScan,
    processes: dict[int, _ProcessScan],
    holders: dict[int, WriterHolder],
    current_boot: str,
) -> tuple[WriterHolder | None, tuple[_ProcessScan, ...], str | None]:
    chain: list[_ProcessScan] = []
    current = scan
    seen: set[int] = set()
    while int(current.path.name) not in seen:
        pid = int(current.path.name)
        seen.add(pid)
        chain.append(current)
        holder = holders.get(pid)
        if holder is not None:
            if (
                holder.boot_id != current_boot
                or not holder.boot_id
                or holder.boot_id == "unknown-boot"
            ):
                return holder, tuple(chain), "HOLDER_BOOT_IDENTITY_CHANGED"
            if current.identity is None:
                return holder, tuple(chain), "HOLDER_IDENTITY_UNREADABLE"
            if current.identity.start_ticks != holder.start_ticks:
                return holder, tuple(chain), "HOLDER_PID_REUSED"
            # Revalidate the positive ancestry proof, including parent identity and
            # PPID edges. A changed edge must never turn a managed PID into a host gap.
            for member in chain:
                try:
                    observed = _current_process_identity(member.path)
                except (OSError, ValueError):
                    return holder, tuple(chain), "LINEAGE_IDENTITY_UNREADABLE"
                if member.identity is None or not _same_process(member.identity, observed):
                    return holder, tuple(chain), "LINEAGE_IDENTITY_CHANGED"
            return holder, tuple(chain), None
        if current.identity is None or current.identity.ppid == 0:
            break
        parent_pid = current.identity.ppid
        parent = processes.get(parent_pid)
        if parent is None:
            if parent_pid in holders:
                return holders[parent_pid], tuple(chain), "HOLDER_IDENTITY_UNAVAILABLE"
            break
        if parent.identity and parent.identity.start_ticks > current.identity.start_ticks:
            # An impossible parent age indicates a mixed-generation observation.
            # Preserve a declared parent association as uncertainty, never permission.
            return holders.get(parent_pid), tuple(chain), "LINEAGE_PID_REUSED"
        current = parent
    if int(current.path.name) in seen and current.identity and current.identity.ppid in seen:
        return None, tuple(chain), "LINEAGE_CYCLE"
    return None, (), None


def runtime_authority_evidence(
    worktree: Path, *, holders: tuple[WriterHolder, ...] = ()
) -> dict[str, Any]:
    """Typed cleanup authority: positive observations block; unassociated permission gaps warn.

    Holder roots must come from the allocation's formal Controller metadata, never
    process names, cgroups, command lines, environment variables or caller guesses.
    """
    budget = [0, 0]
    scans = _runtime_process_scans(worktree, budget)
    processes = {int(scan.path.name): scan for scan in scans}
    declared = {holder.pid: holder for holder in holders}
    current_boot = boot_id() if holders else ""
    result: dict[str, Any] = {
        "contract": RUNTIME_AUTHORITY_CONTRACT,
        "observation_scope": "same-uid-and-managed-processes",
        "managed_references": [],
        "observed_references": [],
        "managed_visibility_uncertainties": [],
        "host_visibility_gaps": [],
        "scan_uncertainties": [],
    }
    for scan in scans:
        if scan.identity is None and not scan.issues:
            continue  # Confirmed gone.
        if scan.identity and scan.identity.terminal and not scan.issues:
            continue
        holder, chain, lineage_issue = (
            _managed_lineage(scan, processes, declared, current_boot)
            if holders
            else (None, (), None)
        )
        if holder and scan.identity and os.getuid() not in scan.identity.uids:
            inspected = _inspect_runtime_process(
                scan.path, (worktree.resolve(strict=True),), budget, inspect_foreign=True
            )
            if not _same_process(scan.identity, inspected.identity):
                lineage_issue = "LINEAGE_IDENTITY_CHANGED"
            scan = inspected
        row = {
            "pid": int(scan.path.name),
            "start_ticks": scan.identity.start_ticks if scan.identity else None,
            "issues": [asdict(issue) for issue in scan.issues],
        }
        if holder:
            row["holder"] = asdict(holder)
            row["lineage"] = [
                {
                    "pid": int(member.path.name),
                    "start_ticks": member.identity.start_ticks if member.identity else None,
                }
                for member in chain
            ]
            if scan.issues or lineage_issue:
                result["managed_visibility_uncertainties"].append(
                    {
                        **row,
                        "classification": "MANAGED_VISIBILITY_UNCERTAIN",
                        "lineage_issue": lineage_issue,
                    }
                )
            else:
                result["managed_references"].append({**row, "classification": "MANAGED_REFERENCE"})
        elif lineage_issue or any(
            issue.errno not in {errno.EACCES, errno.EPERM} for issue in scan.issues
        ):
            result["scan_uncertainties"].append(
                {**row, "classification": "SCAN_UNCERTAIN", "lineage_issue": lineage_issue}
            )
        elif scan.issues:
            result["host_visibility_gaps"].append(
                {
                    **row,
                    "classification": "HOST_VISIBILITY_GAP",
                    "reason": "no formal allocation holder or verified holder lineage association; absence of references is not proven",
                }
            )
        result["observed_references"].extend(
            {**reference, "classification": "OBSERVED_REFERENCE"} for reference in scan.references
        )
    return result
