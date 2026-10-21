"""Version 1: explicit project resources, never an ignored-directory deletion heuristic."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import tomllib
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .environment import (
    CACHEDIR_SIGNATURE,
    MAX_VENV_ENTRIES,
    _sha256,
    discover_python_project,
    unsafe_ignored_paths,
)
from .git_backend import head_paths, ignored_paths, path_is_ignored, tracked_paths
from .models import ControllerError

CONTRACT = "typed-disposable-resource/v1"
RESOURCE_TYPES = {
    "backend/.venv": "python-venv",
    "frontend/node_modules": "node-modules",
    "frontend/dist": "frontend-build-output",
}
MAX_IDENTITY_BYTES = 8 * 1_048_576
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _inode(metadata: os.stat_result) -> dict[str, int]:
    return {
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "file_type": stat.S_IFMT(metadata.st_mode),
    }


def _signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _link_identity(metadata: os.stat_result) -> list[int]:
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


def _race() -> ControllerError:
    return _blocked("resource path or directory entry identity changed", "RESOURCE_PLAN_MISMATCH")


@dataclass(frozen=True)
class _Directory:
    parent_fd: int
    name: str
    fd: int
    identity: dict[str, int]

    def check(self) -> None:
        if (
            _inode(os.fstat(self.fd)) != self.identity
            or _inode(os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False))
            != self.identity
        ):
            raise _race()


@contextmanager
def _open_directory(parent_fd: int, name: str, expected: dict[str, int]) -> Iterator[_Directory]:
    if expected["file_type"] != stat.S_IFDIR:
        raise _race()
    fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        directory = _Directory(parent_fd, name, fd, expected)
        directory.check()
        yield directory
    finally:
        os.close(fd)


@dataclass(frozen=True)
class _TreeEntry:
    metadata: os.stat_result
    children: tuple[str, ...] = ()
    link: str | None = None


def _fd_inventory(
    directory_fd: int, *, preserved_fifo: bool = False
) -> tuple[dict[str, _TreeEntry], dict[str, Any]]:
    """Capture a no-follow inventory and reproduce the classified tree fingerprint."""
    entries: dict[str, _TreeEntry] = {}
    digest = hashlib.sha256()
    size = 0

    def visit(parent_fd: int, name: str, relative: str) -> None:
        nonlocal size
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if len(entries) >= MAX_VENV_ENTRIES:
            raise _blocked("resource inventory exceeded its bound", "RESOURCE_SCAN_LIMIT")
        digest.update(os.fsencode(relative) + b"\0")
        digest.update(":".join(map(str, _signature(metadata))).encode())
        size += metadata.st_size
        entries[relative] = _TreeEntry(metadata)
        if stat.S_ISDIR(metadata.st_mode):
            with _open_directory(parent_fd, name, _inode(metadata)) as directory:
                names = tuple(sorted(os.listdir(directory.fd)))
                entries[relative] = _TreeEntry(metadata, names)
                for child in names:
                    visit(directory.fd, child, child if relative == "." else f"{relative}/{child}")
                directory.check()
                if _signature(os.fstat(directory.fd)) != _signature(metadata):
                    raise _race()
        elif stat.S_ISLNK(metadata.st_mode):
            link = os.readlink(name, dir_fd=parent_fd)
            digest.update(os.fsencode(link))
            entries[relative] = _TreeEntry(metadata, link=link)
        elif not (
            stat.S_ISREG(metadata.st_mode) or (preserved_fifo and stat.S_ISFIFO(metadata.st_mode))
        ):
            raise _race()
        if _signature(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) != _signature(
            metadata
        ):
            raise _race()

    visit(directory_fd, ".", ".")
    return entries, {"tree_sha256": digest.hexdigest(), "entries": len(entries), "bytes": size}


def _unlink_leaf(
    parent: int,
    name: str,
    expected: list[int],
    *,
    link: str | None,
    chain: tuple[_Directory, ...],
    external: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .hardlinks import pinned_content

    for anchor in chain:
        anchor.check()
    pin = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    try:
        metadata = os.fstat(pin)
        if _link_identity(metadata) != expected or stat.S_ISDIR(metadata.st_mode):
            raise _race()
        if link is not None and os.readlink(name, dir_fd=parent) != link:
            raise _race()
        if external is not None and pinned_content(pin, expected) != external:
            raise _race()
        for anchor in chain:
            anchor.check()
        if _link_identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != expected:
            raise _race()
        os.unlink(name, dir_fd=parent)
        after = _link_identity(os.fstat(pin))
        wanted = list(expected)
        wanted[8] -= 1
        wanted[5] = after[5]
        if after != wanted:
            raise _race()
        result = {"identity": after}
        if external is not None:
            content = pinned_content(pin, after)
            if content != external:
                raise _race()
            result["content"] = content
        for anchor in chain:
            anchor.check()
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return result
        raise _race()
    finally:
        os.close(pin)


def _delete_fd_tree(
    directory: _Directory,
    relative: str,
    entries: dict[str, _TreeEntry],
    chain: tuple[_Directory, ...],
    link_state: dict[tuple[int, int], list[int]] | None = None,
    external_contents: dict[str, dict[str, Any]] | None = None,
    external_survivors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if external_contents is None:
        external_contents = {}
    if external_survivors is None:
        external_survivors = {}
    if link_state is None:
        link_state = {
            (e.metadata.st_dev, e.metadata.st_ino): _link_identity(e.metadata)
            for e in entries.values()
            if not stat.S_ISDIR(e.metadata.st_mode)
        }

    def check_chain() -> None:
        for anchor in chain:
            anchor.check()

    expected = entries[relative]
    check_chain()
    if tuple(sorted(os.listdir(directory.fd))) != expected.children:
        raise _race()
    for name in expected.children:
        child_path = name if relative == "." else f"{relative}/{name}"
        child = entries[child_path]
        metadata = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
        if _signature(metadata) != _signature(child.metadata):
            raise _race()
        if stat.S_ISDIR(metadata.st_mode):
            with _open_directory(directory.fd, name, _inode(metadata)) as opened:
                _delete_fd_tree(
                    opened,
                    child_path,
                    entries,
                    (*chain, opened),
                    link_state,
                    external_contents,
                    external_survivors,
                )
        else:
            key = (metadata.st_dev, metadata.st_ino)
            expected_identity = link_state.get(key)
            if expected_identity is None:
                raise _race()
            external = external_contents.get(child_path)
            proof = _unlink_leaf(
                directory.fd, name, expected_identity, link=child.link, chain=chain, external=external
            )
            link_state[key] = proof["identity"]
            if external is not None:
                external_survivors[f"{key[0]}:{key[1]}"] = proof
        try:
            os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise _race()
    check_chain()
    if os.listdir(directory.fd):
        raise _race()
    os.rmdir(directory.name, dir_fd=directory.parent_fd)
    if os.fstat(directory.fd).st_nlink != 0:
        raise _race()
    return external_survivors


def delete_typed_resource(root: Path, relative: str, evidence: dict[str, Any]) -> dict[str, Any]:
    """Delete only through verified directory FDs; paths never authorize a destructive call."""
    if relative not in RESOURCE_TYPES or not root.is_absolute():
        raise _race()
    try:
        with ExitStack() as stack:
            filesystem_fd = os.open("/", DIRECTORY_FLAGS)
            stack.callback(os.close, filesystem_fd)
            parent_fd = filesystem_fd
            chain: list[_Directory] = []
            # Pin even the ancestors of the worktree: opening an absolute root with
            # O_NOFOLLOW alone would still follow a replaced intermediate directory.
            for index, name in enumerate(root.parts[1:]):
                expected = (
                    evidence["path_identities"]["."]
                    if index == len(root.parts) - 2
                    else _inode(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
                )
                opened = stack.enter_context(_open_directory(parent_fd, name, expected))
                chain.append(opened)
                parent_fd = opened.fd
            for index, name in enumerate(relative.split("/")):
                key = "/".join(relative.split("/")[: index + 1])
                opened = stack.enter_context(
                    _open_directory(parent_fd, name, evidence["path_identities"][key])
                )
                chain.append(opened)
                parent_fd = opened.fd
            entries, tree = _fd_inventory(parent_fd)
            if (
                "hardlink_identities" in evidence
                and {
                    path: _link_identity(entry.metadata)
                    for path, entry in entries.items()
                    if not stat.S_ISDIR(entry.metadata.st_mode)
                }
                != evidence["hardlink_identities"]
            ):
                raise _race()
            if any(tree[key] != evidence[key] for key in tree):
                raise _race()
            return _delete_fd_tree(
                chain[-1],
                ".",
                entries,
                tuple(chain),
                external_contents=evidence.get("external_hardlink_contents"),
            )
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP, errno.ESTALE}:
            raise _race() from exc
        raise


def _blocked(message: str, code: str = "RESOURCE_IDENTITY_MISMATCH") -> ControllerError:
    return ControllerError(code, message)


def _regular(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise _blocked(f"identity must be a regular file: {path}")
    if path.stat().st_size > MAX_IDENTITY_BYTES:
        raise _blocked(f"identity exceeds scan bound: {path}", "RESOURCE_SCAN_LIMIT")
    return path.read_bytes()


def _project_identity(root: Path, relative: str) -> bytes:
    path = root / relative
    if path.resolve(strict=True) != path:
        raise _blocked(f"project identity escapes its path: {relative}", "RESOURCE_PATH_ESCAPE")
    if tracked_paths(root, relative) != (relative,) or head_paths(root, relative) != (relative,):
        raise _blocked(f"project identity must be committed and indexed: {relative}")
    return _regular(path)


def _json(payload: bytes) -> dict[str, Any]:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise _blocked("project identity must be a JSON object")
    return value


def _venv_identity(root: Path, resource: Path) -> dict[str, Any]:
    rel_venv = resource.relative_to(root).as_posix()
    project_rel, lock_rel = discover_python_project(root, venv_relative_path=rel_venv)
    project = _project_identity(root, project_rel)
    lock = _project_identity(root, lock_rel)
    if not tomllib.loads(project.decode()).get("project", {}).get("name"):
        raise _blocked("Python project identity is incomplete")
    if not tomllib.loads(lock.decode()).get("version"):
        raise _blocked("Python lock identity is incomplete")
    marker = _regular(resource / "pyvenv.cfg")
    values = {}
    for line in marker.decode().splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip().lower()] = value.strip()
    version = values.get("version_info") or values.get("version", "")
    if not re.fullmatch(r"3\.\d+\.\d+(?:\S*)?", version) or not values.get("home"):
        raise _blocked("pyvenv.cfg requires Python version and home identity")
    if values.get("include-system-site-packages") != "false":
        raise _blocked("only isolated Python virtualenvs are supported")
    tag = _regular(resource / "CACHEDIR.TAG")
    if not tag.startswith(CACHEDIR_SIGNATURE):
        raise _blocked("venv requires the standard CACHEDIR.TAG identity")
    allowed = {
        "pyvenv.cfg",
        "CACHEDIR.TAG",
        ".gitignore",
        ".lock",
        "bin",
        "lib",
        "lib64",
        "include",
    }
    if any(path.name not in allowed for path in resource.iterdir()):
        raise _blocked("venv contains unknown top-level content")
    for directory in (resource / "bin", resource / "lib"):
        if directory.is_symlink() or not directory.is_dir():
            raise _blocked("venv bin/lib must be local directories", "RESOURCE_PATH_ESCAPE")
    python = resource / "bin/python"
    target = python.resolve(strict=True)
    major_minor = ".".join(version.split(".")[:2])
    interpreter_names = {"python", "python3", f"python{major_minor}"}
    external_links = []
    if not target.is_relative_to(resource):
        home = Path(values["home"])
        if not home.is_absolute() or target.parent != home.resolve(strict=True):
            raise _blocked("external interpreter differs from pyvenv home", "RESOURCE_PATH_ESCAPE")
        if target.name != f"python{major_minor}":
            raise _blocked(
                "external interpreter name differs from venv version", "RESOURCE_PATH_ESCAPE"
            )
        for name in sorted(interpreter_names):
            link = resource / "bin" / name
            if not os.path.lexists(link):
                continue
            if not link.is_symlink() or link.resolve(strict=True) != target:
                raise _blocked("venv interpreter aliases disagree", "RESOURCE_PATH_ESCAPE")
            raw = Path(os.readlink(link))
            if not (
                (raw.parent == Path(".") and raw.name in interpreter_names)
                or (raw.is_absolute() and raw.parent == home and raw.name in interpreter_names)
            ):
                raise _blocked("nonstandard interpreter link shape", "RESOURCE_PATH_ESCAPE")
            external_links.append(link.relative_to(resource).as_posix())
    if not python.is_file() or not os.access(python, os.X_OK):
        raise _blocked("venv requires an executable Python identity")
    if target.stat().st_size > 128 * 1_048_576:
        raise _blocked("Python executable exceeds scan bound", "RESOURCE_SCAN_LIMIT")
    # Do not execute a checkout-supplied interpreter to establish its identity.
    with python.open("rb") as stream:
        if stream.read(4) != b"\x7fELF":
            raise _blocked("venv Python must be a native Linux executable")
    if not (resource / "lib" / f"python{major_minor}" / "site-packages").is_dir():
        raise _blocked("venv site-packages structure is incomplete")
    return {
        "project_sha256": hashlib.sha256(project).hexdigest(),
        "lock_sha256": hashlib.sha256(lock).hexdigest(),
        "pyvenv_cfg_sha256": hashlib.sha256(marker).hexdigest(),
        "python_version": version,
        "python_target": str(target),
        "python_identity_sha256": _sha256(target),
        "external_interpreter_links": external_links,
    }


def _frontend_identity(root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    project = _project_identity(root, "frontend/package.json")
    lock = _project_identity(root, "frontend/package-lock.json")
    package, locked = _json(project), _json(lock)
    packages = locked.get("packages")
    if (
        not package.get("name")
        or package.get("workspaces")
        or locked.get("lockfileVersion") not in {2, 3}
        or not isinstance(packages, dict)
        or not isinstance(packages.get(""), dict)
        or packages[""].get("name") != package["name"]
    ):
        raise _blocked("frontend requires an npm project and matching non-workspace lock")
    for kind in ("dependencies", "devDependencies", "optionalDependencies"):
        if package.get(kind, {}) != packages[""].get(kind, {}):
            raise _blocked("frontend dependency declarations differ from lock")
    return (
        package,
        packages,
        {
            "project_sha256": hashlib.sha256(project).hexdigest(),
            "lock_sha256": hashlib.sha256(lock).hexdigest(),
        },
    )


def _node_identity(root: Path, resource: Path) -> dict[str, str]:
    package, packages, evidence = _frontend_identity(root)
    installed_bytes = _regular(resource / ".package-lock.json")
    installed = _json(installed_bytes)
    entries = installed.get("packages")
    if installed.get("name") != package["name"] or not isinstance(entries, dict) or not entries:
        raise _blocked("node_modules lacks its npm installed-tree identity")
    allowed = {".bin", ".package-lock.json", ".vite-temp"}
    for relative, entry in entries.items():
        parts = relative.split("/")
        if (
            len(parts) < 2
            or parts[0] != "node_modules"
            or any(part in {"", ".", ".."} for part in parts)
            or not isinstance(entry, dict)
            or entry.get("link")
            or entry != packages.get(relative)
        ):
            raise _blocked("installed dependency is not bound to the committed npm lock")
        manifest = _json(_regular(root / "frontend" / relative / "package.json"))
        if not entry.get("version") or manifest.get("version") != entry["version"]:
            raise _blocked("installed package version differs from lock")
        allowed.add(parts[1])
    for path in resource.iterdir():
        if path.name not in allowed:
            raise _blocked("node_modules contains unknown top-level content")
        if path.name == ".vite-temp" and (not path.is_dir() or any(path.iterdir())):
            raise _blocked("nonempty Vite temporary state is not dependency identity")
        if path.name.startswith("@"):
            for child in path.iterdir():
                if f"node_modules/{path.name}/{child.name}" not in entries:
                    raise _blocked("node_modules contains an unregistered scoped package")
    evidence["installed_lock_sha256"] = hashlib.sha256(installed_bytes).hexdigest()
    return evidence


def _default_output_config(text: str) -> bool:
    """Recognize an empty or server-only exported object, not arbitrary config programs."""
    quoted = r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'"
    plain = re.sub(quoted, "STRING", text)
    plain = re.sub(r"/\*.*?\*/|//[^\n]*", "", plain, flags=re.DOTALL)
    starts = list(re.finditer(r"\breturn\s*(?=\{)", plain))
    if starts:
        if len(starts) != 1 or not re.search(r"export\s+default\s+defineConfig\s*\(", plain):
            return False
        body = plain[starts[0].end() :].strip()
        suffix = r";?\s*\}\s*\)\s*;?"
    else:
        match = re.search(r"export\s+default\s+defineConfig\s*\(\s*(?=\{)", plain)
        if not match:
            return False
        body = plain[match.end() :].strip()
        suffix = r"\)\s*;?"
    # Collapse nested literal objects. Remaining top-level properties must be server only.
    reduced = body
    while re.search(r"\{[^{}]*\}", reduced):
        previous = reduced
        reduced = re.sub(r"\{[^{}]*\}", "OBJECT", reduced)
        if not reduced.startswith("{"):
            outer = previous[: previous.index("}") + 1]
            return bool(
                re.fullmatch(r"\{\s*(?:server\s*:\s*OBJECT\s*,?\s*)?\}", outer)
                and re.fullmatch(suffix, previous[len(outer) :].strip())
            )
    return False


def _dist_identity(root: Path, resource: Path) -> dict[str, str]:
    package, _, evidence = _frontend_identity(root)
    dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    if package.get("scripts", {}).get("build") != "vite build" or "vite" not in dependencies:
        raise _blocked("frontend/dist is not a supported declared Vite build output")
    configs = sorted((root / "frontend").glob("vite.config.*"))
    if len(configs) > 1:
        raise _blocked("ambiguous Vite configuration")
    if configs:
        config = _project_identity(root, configs[0].relative_to(root).as_posix())
        text = config.decode()
        # Only the default output with a local, statically recognizable config is supported.
        # Overrides, imports of custom config, spreads and computed properties need review.
        if (
            re.search(r"\b(root|build|outDir|publicDir|extends|require)\b|\.\.\.|\[|`", text)
            or re.search(r"\bimport\b(?!\s*\{[^}]*\}\s*from\s*['\"]vite['\"])", text)
            or re.search(r"\bexport\s+\*|\bprocess\b", text)
            or not _default_output_config(text)
        ):
            raise _blocked("Vite output cannot be established without executing config")
        evidence["build_config_sha256"] = hashlib.sha256(config).hexdigest()
    _project_identity(root, "frontend/index.html")
    _regular(resource / "index.html")
    if not (resource / "assets").is_dir():
        raise _blocked("frontend build output lacks assets")
    if any(path.name not in {"index.html", "assets"} for path in resource.iterdir()):
        raise _blocked("frontend build output contains unknown top-level content")
    if any(
        path.is_dir()
        or path.suffix not in {".js", ".css", ".map", ".svg", ".png", ".jpg", ".woff", ".woff2"}
        for path in (resource / "assets").iterdir()
    ):
        raise _blocked("frontend assets contain unsupported content")
    return evidence


def _tree_identity(
    resource: Path,
    *,
    interpreter_links: tuple[str, ...] = (),
    interpreter_target: str | None = None,
) -> dict[str, Any]:
    digest = hashlib.sha256()
    count = size = 0
    pending = [resource]
    while pending:
        path = pending.pop()
        metadata = path.lstat()
        count += 1
        if count > MAX_VENV_ENTRIES:
            raise _blocked("resource inventory exceeded its bound", "RESOURCE_SCAN_LIMIT")
        if metadata.st_uid != os.getuid():
            raise _blocked("resource ownership is not the current user", "RESOURCE_OWNER_MISMATCH")
        if path.name == ".git" or path.is_mount():
            raise _blocked("resource contains Git metadata or a mount")
        relative = path.relative_to(resource).as_posix()
        digest.update(os.fsencode(relative) + b"\0")
        digest.update(":".join(map(str, _signature(metadata))).encode())
        size += metadata.st_size
        if stat.S_ISLNK(metadata.st_mode):
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(resource) and not (
                relative in interpreter_links and str(resolved) == interpreter_target
            ):
                raise _blocked(
                    f"resource symlink escapes its root: {relative}", "RESOURCE_PATH_ESCAPE"
                )
            digest.update(os.fsencode(os.readlink(path)))
        elif stat.S_ISDIR(metadata.st_mode):
            pending.extend(sorted(path.iterdir(), reverse=True))
        elif not stat.S_ISREG(metadata.st_mode):
            raise _blocked("resource contains a special filesystem object")
    return {"tree_sha256": digest.hexdigest(), "entries": count, "bytes": size}


def resource_evidence(root: Path, relative: str) -> dict[str, Any]:
    if relative not in RESOURCE_TYPES:
        raise _blocked("resource path is outside the typed contract", "UNKNOWN_IGNORED_RESOURCE")
    resource = root / relative
    if resource.is_symlink() or resource.resolve(strict=True) != resource or not resource.is_dir():
        raise _blocked("resource root is not a canonical local directory", "RESOURCE_PATH_ESCAPE")
    if tracked_paths(root, relative) or head_paths(root, relative):
        raise _blocked("resource contains tracked or staged content", "TRACKED_RESOURCE")
    if not path_is_ignored(root, relative):
        raise _blocked("resource is not Git-ignored", "RESOURCE_NOT_IGNORED")
    tree = _tree_identity(resource) if RESOURCE_TYPES[relative] != "python-venv" else None
    identity = {
        "python-venv": _venv_identity,
        "node-modules": _node_identity,
        "frontend-build-output": _dist_identity,
    }[RESOURCE_TYPES[relative]](root, resource)
    tree = tree or _tree_identity(
        resource,
        interpreter_links=tuple(identity.get("external_interpreter_links", ())),
        interpreter_target=identity.get("python_target"),
    )
    path_identities = {
        path.relative_to(root).as_posix(): _inode(path.lstat())
        for path in (root, resource.parent, resource)
    }
    if any(value["file_type"] != stat.S_IFDIR for value in path_identities.values()):
        raise _race()
    return {**tree, **identity, "path_identities": path_identities}


def classify_resources(root: Path) -> tuple[dict[str, Any], ...]:
    """Read-only inventory; legacy release caches receive no typed cleanup action."""
    ignored = ignored_paths(root)
    paths = {path.rstrip("/") for path in ignored}
    paths.update(relative for relative in RESOURCE_TYPES if os.path.lexists(root / relative))
    rows = []
    for relative in sorted(paths):
        if any(relative.startswith(candidate + "/") for candidate in RESOURCE_TYPES):
            continue
        row: dict[str, Any] = {
            "resource_type": RESOURCE_TYPES.get(relative, "unknown"),
            "path": relative,
            "classification": "blocked",
            "cleanup_action": "none",
            "blocker": None,
        }
        if relative in RESOURCE_TYPES:
            try:
                row["evidence"] = resource_evidence(root, relative)
                row.update(classification="disposable", cleanup_action="remove-tree")
            except ControllerError as exc:
                row.update(blocker=exc.code, reason=str(exc))
            except (OSError, ValueError, RuntimeError, TypeError, AttributeError) as exc:
                row.update(
                    blocker="RESOURCE_IDENTITY_MISMATCH",
                    reason=f"identity inspection failed: {exc}",
                )
        elif not unsafe_ignored_paths((relative,)):
            row.update(
                resource_type="legacy-release-cache", classification="existing-release-policy"
            )
        else:
            row.update(
                blocker="UNKNOWN_IGNORED_RESOURCE", reason="ignored content has no typed contract"
            )
        rows.append(row)
    return tuple(rows)
