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
from .git_backend import (
    head_paths,
    ignored_paths,
    path_is_ignored,
    resolve_repository,
    tracked_paths,
)
from .models import ControllerError
from .policy import load_policy

CONTRACT = "typed-disposable-resource/v1"
DEFAULT_RESOURCE_TYPES = {
    ".venv": "python-venv",
}
RESOURCE_TYPES = DEFAULT_RESOURCE_TYPES
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


def path_identity(path: Path) -> dict[str, int]:
    """Shared no-follow inode identity for a pinned resource or ancestor."""
    return _inode(path.lstat())


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
    stack: list[list[Any]] = [[directory_fd, ".", ".", None, None, None]]
    opened_stack: list[_Directory] = []
    try:
        while stack:
            frame = stack[-1]
            parent_fd, name, relative, metadata, opened_dir, remaining = frame
            if metadata is None:
                metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if len(entries) >= MAX_VENV_ENTRIES:
                    raise _blocked("resource inventory exceeded its bound", "RESOURCE_SCAN_LIMIT")
                digest.update(os.fsencode(relative) + b"\0")
                digest.update(":".join(map(str, _signature(metadata))).encode())
                size += metadata.st_size
                entries[relative] = _TreeEntry(metadata)
                if stat.S_ISDIR(metadata.st_mode):
                    dir_cm = _open_directory(parent_fd, name, _inode(metadata))
                    opened = dir_cm.__enter__()
                    opened_stack.append((opened, dir_cm))
                    names = tuple(sorted(os.listdir(opened.fd)))
                    entries[relative] = _TreeEntry(metadata, names)
                    frame[3] = metadata
                    frame[4] = opened
                    frame[5] = list(names)
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    link = os.readlink(name, dir_fd=parent_fd)
                    digest.update(os.fsencode(link))
                    entries[relative] = _TreeEntry(metadata, link=link)
                elif not (
                    stat.S_ISREG(metadata.st_mode)
                    or (preserved_fifo and stat.S_ISFIFO(metadata.st_mode))
                ):
                    raise _race()
                if _signature(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) != _signature(
                    metadata
                ):
                    raise _race()
                stack.pop()
            else:
                if remaining:
                    child = remaining.pop(0)
                    child_rel = child if relative == "." else f"{relative}/{child}"
                    stack.append([opened_dir.fd, child, child_rel, None, None, None])
                    continue
                opened_dir.check()
                if _signature(os.fstat(opened_dir.fd)) != _signature(metadata):
                    raise _race()
                if opened_stack and opened_stack[-1][0] is opened_dir:
                    opened_stack.pop()[1].__exit__(None, None, None)
                if _signature(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) != _signature(
                    metadata
                ):
                    raise _race()
                stack.pop()
    finally:
        while opened_stack:
            opened_stack.pop()[1].__exit__(None, None, None)
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

    def check_chain(active_chain: tuple[_Directory, ...]) -> None:
        for anchor in active_chain:
            anchor.check()

    stack: list[list[Any]] = [[directory, ".", chain, None]]
    opened_stack: list[tuple[_Directory, Any]] = []
    try:
        while stack:
            frame = stack[-1]
            cur_dir, cur_rel, cur_chain, remaining = frame
            if remaining is None:
                expected = entries[cur_rel]
                check_chain(cur_chain)
                if tuple(sorted(os.listdir(cur_dir.fd))) != expected.children:
                    raise _race()
                frame[3] = list(expected.children)
            elif remaining:
                name = remaining.pop(0)
                child_path = name if cur_rel == "." else f"{cur_rel}/{name}"
                child = entries[child_path]
                metadata = os.stat(name, dir_fd=cur_dir.fd, follow_symlinks=False)
                if _signature(metadata) != _signature(child.metadata):
                    raise _race()
                if stat.S_ISDIR(metadata.st_mode):
                    dir_cm = _open_directory(cur_dir.fd, name, _inode(metadata))
                    opened = dir_cm.__enter__()
                    opened_stack.append((opened, dir_cm))
                    stack.append([opened, child_path, (*cur_chain, opened), None])
                    continue
                key = (metadata.st_dev, metadata.st_ino)
                expected_identity = link_state.get(key)
                if expected_identity is None:
                    raise _race()
                external = external_contents.get(child_path)
                proof = _unlink_leaf(
                    cur_dir.fd,
                    name,
                    expected_identity,
                    link=child.link,
                    chain=cur_chain,
                    external=external,
                )
                link_state[key] = proof["identity"]
                if external is not None:
                    external_survivors[f"{key[0]}:{key[1]}"] = proof
                try:
                    os.stat(name, dir_fd=cur_dir.fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise _race()
            else:
                check_chain(cur_chain)
                if os.listdir(cur_dir.fd):
                    raise _race()
                os.rmdir(cur_dir.name, dir_fd=cur_dir.parent_fd)
                if os.fstat(cur_dir.fd).st_nlink != 0:
                    raise _race()
                stack.pop()
                if opened_stack and opened_stack[-1][0] is cur_dir:
                    opened_stack.pop()[1].__exit__(None, None, None)
    finally:
        while opened_stack:
            opened_stack.pop()[1].__exit__(None, None, None)
    return external_survivors


def safe_rmtree_nofollow(root: Path, relative: str) -> None:
    """Iteratively remove a worktree-local directory tree without following symlinks."""
    rel_parts = tuple(part for part in Path(relative).parts if part not in {"", "."})
    if not rel_parts or any(part == ".." for part in rel_parts) or not root.is_absolute():
        raise _race()
    with ExitStack() as stack:
        fs_fd = os.open("/", DIRECTORY_FLAGS)
        stack.callback(os.close, fs_fd)
        parent_fd = fs_fd
        for part in root.parts[1:]:
            opened = stack.enter_context(
                _open_directory(parent_fd, part, _inode(os.stat(part, dir_fd=parent_fd, follow_symlinks=False)))
            )
            parent_fd = opened.fd
        for part in rel_parts[:-1]:
            opened = stack.enter_context(
                _open_directory(parent_fd, part, _inode(os.stat(part, dir_fd=parent_fd, follow_symlinks=False)))
            )
            parent_fd = opened.fd
        target_name = rel_parts[-1]
        try:
            target_stat = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(target_stat.st_mode):
            raise _race()
        target_dir = stack.enter_context(_open_directory(parent_fd, target_name, _inode(target_stat)))
        walk_stack: list[list[Any]] = [[target_dir, None]]
        opened_dirs: list[tuple[_Directory, Any]] = []
        try:
            while walk_stack:
                frame = walk_stack[-1]
                cur_dir, names = frame
                if names is None:
                    frame[1] = sorted(os.listdir(cur_dir.fd))
                    continue
                if frame[1]:
                    entry = frame[1].pop(0)
                    meta = os.stat(entry, dir_fd=cur_dir.fd, follow_symlinks=False)
                    if stat.S_ISDIR(meta.st_mode):
                        dir_cm = _open_directory(cur_dir.fd, entry, _inode(meta))
                        opened = dir_cm.__enter__()
                        opened_dirs.append((opened, dir_cm))
                        walk_stack.append([opened, None])
                    else:
                        os.unlink(entry, dir_fd=cur_dir.fd)
                    continue
                cur_dir.check()
                os.rmdir(cur_dir.name, dir_fd=cur_dir.parent_fd)
                walk_stack.pop()
                if opened_dirs and opened_dirs[-1][0] is cur_dir:
                    opened_dirs.pop()[1].__exit__(None, None, None)
        finally:
            while opened_dirs:
                opened_dirs.pop()[1].__exit__(None, None, None)


def delete_typed_resource(root: Path, relative: str, evidence: dict[str, Any]) -> dict[str, Any]:
    """Delete only through verified directory FDs; paths never authorize a destructive call."""
    declaration = evidence.get("build_declaration")
    if (relative not in resource_types_for(root) and declaration is None) or not root.is_absolute():
        raise _race()
    if declaration is not None:
        from .build_resources import check_resource

        check_resource(root, relative, declaration)
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


def project_identity(root: Path, relative: str) -> bytes:
    path = root / relative
    if path.resolve(strict=True) != path:
        raise _blocked(f"project identity escapes its path: {relative}", "RESOURCE_PATH_ESCAPE")
    if tracked_paths(root, relative) != (relative,) or head_paths(root, relative) != (relative,):
        raise _blocked(f"project identity must be committed and indexed: {relative}")
    return _regular(path)


def project_json(payload: bytes) -> dict[str, Any]:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise _blocked("project identity must be a JSON object")
    return value


def _venv_identity(root: Path, resource: Path) -> dict[str, Any]:
    rel_venv = resource.relative_to(root).as_posix()
    project_rel, lock_rel = discover_python_project(root, venv_relative_path=rel_venv)
    project = project_identity(root, project_rel)
    lock = project_identity(root, lock_rel)
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


def _project_relative(project_dir: str, filename: str) -> str:
    return filename if project_dir in {"", "."} else f"{project_dir}/{filename}"


def _frontend_identity(
    root: Path, project_dir: str = "."
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    project = project_identity(root, _project_relative(project_dir, "package.json"))
    lock = project_identity(root, _project_relative(project_dir, "package-lock.json"))
    package, locked = project_json(project), project_json(lock)
    packages = locked.get("packages")
    if (
        not package.get("name")
        or package.get("workspaces")
        or locked.get("lockfileVersion") not in {2, 3}
        or not isinstance(packages, dict)
        or not isinstance(packages.get(""), dict)
        or packages[""].get("name") != package["name"]
    ):
        raise _blocked(f"{project_dir} requires an npm project and matching non-workspace lock")
    for kind in ("dependencies", "devDependencies", "optionalDependencies"):
        if package.get(kind, {}) != packages[""].get(kind, {}):
            raise _blocked(f"{project_dir} dependency declarations differ from lock")
    return (
        package,
        packages,
        {
            "project_sha256": hashlib.sha256(project).hexdigest(),
            "lock_sha256": hashlib.sha256(lock).hexdigest(),
        },
    )


def _node_identity(root: Path, resource: Path) -> dict[str, str]:
    rel = resource.relative_to(root).as_posix()
    project_dir = Path(rel).parent.as_posix()
    base_dir = root if project_dir == "." else root / project_dir
    package, packages, evidence = _frontend_identity(root, project_dir)
    installed_bytes = _regular(resource / ".package-lock.json")
    installed = project_json(installed_bytes)
    entries = installed.get("packages")
    if installed.get("name") != package["name"] or not isinstance(entries, dict) or not entries:
        raise _blocked("node_modules lacks its npm installed-tree identity")
    dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    allowed = {".bin", ".package-lock.json"}
    if "vite" in dependencies:
        allowed.add(".vite-temp")
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
        manifest = project_json(_regular(base_dir / relative / "package.json"))
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
    rel = resource.relative_to(root).as_posix()
    project_dir = Path(rel).parent.as_posix()
    base_dir = root if project_dir == "." else root / project_dir
    package, _, evidence = _frontend_identity(root, project_dir)
    dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    if package.get("scripts", {}).get("build") != "vite build" or "vite" not in dependencies:
        raise _blocked(f"{rel} is not a supported declared Vite build output")
    configs = sorted(base_dir.glob("vite.config.*"))
    if len(configs) > 1:
        raise _blocked("ambiguous Vite configuration")
    if configs:
        config = project_identity(root, configs[0].relative_to(root).as_posix())
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
    project_identity(root, _project_relative(project_dir, "index.html"))
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
    pending = [(resource, ".")]
    while pending:
        path, relative = pending.pop()
        metadata = path.lstat()
        count += 1
        if count > MAX_VENV_ENTRIES:
            raise _blocked("resource inventory exceeded its bound", "RESOURCE_SCAN_LIMIT")
        if metadata.st_uid != os.getuid():
            raise _blocked("resource ownership is not the current user", "RESOURCE_OWNER_MISMATCH")
        if path.name == ".git" or path.is_mount():
            raise _blocked("resource contains Git metadata or a mount")
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
            pending.extend(
                (child, child.name if relative == "." else f"{relative}/{child.name}")
                for child in sorted(path.iterdir(), key=lambda child: child.name, reverse=True)
            )
        elif not stat.S_ISREG(metadata.st_mode):
            raise _blocked("resource contains a special filesystem object")
    return {"tree_sha256": digest.hexdigest(), "entries": count, "bytes": size}


def resource_types_for(
    root: Path, resource_types: dict[str, str] | None = None
) -> dict[str, str]:
    if resource_types is not None:
        return dict(resource_types)
    if (root / ".agents" / "worktree-policy.toml").is_file():
        loaded = load_policy(root)
        return dict(loaded.disposable_resources) if loaded.disposable_resources_configured else dict(DEFAULT_RESOURCE_TYPES)
    try:
        canonical, _ = resolve_repository(root)
    except ControllerError:
        return dict(DEFAULT_RESOURCE_TYPES)
    loaded = load_policy(canonical)
    return dict(loaded.disposable_resources) if loaded.disposable_resources_configured else dict(DEFAULT_RESOURCE_TYPES)


def resource_evidence(
    root: Path,
    relative: str,
    *,
    build_declaration: dict[str, Any] | None = None,
    resource_types: dict[str, str] | None = None,
) -> dict[str, Any]:
    if build_declaration is not None:
        from .build_resources import check_resource

        check_resource(root, relative, build_declaration)
    configured = resource_types_for(root, resource_types)
    if relative not in configured and build_declaration is None:
        raise _blocked("resource path is outside the typed contract", "UNKNOWN_IGNORED_RESOURCE")
    resource = root / relative
    if resource.is_symlink() or resource.resolve(strict=True) != resource or not resource.is_dir():
        raise _blocked("resource root is not a canonical local directory", "RESOURCE_PATH_ESCAPE")
    if tracked_paths(root, relative) or head_paths(root, relative):
        raise _blocked("resource contains tracked or staged content", "TRACKED_RESOURCE")
    if not path_is_ignored(root, relative):
        raise _blocked("resource is not Git-ignored", "RESOURCE_NOT_IGNORED")
    kind = configured.get(relative)
    tree = _tree_identity(resource) if build_declaration or kind != "python-venv" else None
    identity = {"build_declaration": build_declaration} if build_declaration else {
        "python-venv": _venv_identity,
        "node-modules": _node_identity,
        "frontend-build-output": _dist_identity,
        "build-output": _dist_identity,
    }[str(kind)](root, resource)
    tree = tree or _tree_identity(
        resource,
        interpreter_links=tuple(identity.get("external_interpreter_links", ())),
        interpreter_target=identity.get("python_target"),
    )
    path_identities = {".": _inode(root.lstat())}
    for path in (resource, *resource.parents):
        if path == root:
            break
        path_identities[path.relative_to(root).as_posix()] = _inode(path.lstat())
    if any(value["file_type"] != stat.S_IFDIR for value in path_identities.values()):
        raise _race()
    return {**tree, **identity, "path_identities": path_identities}


def classify_resources(
    root: Path,
    *,
    build_resources: dict[str, dict[str, Any]] | None = None,
    resource_types: dict[str, str] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Read-only inventory; legacy release caches receive no typed cleanup action."""
    ignored = ignored_paths(root)
    build_resources = build_resources or {}
    configured = resource_types_for(root, resource_types)
    known = configured.keys() | build_resources.keys()
    paths = {path.rstrip("/") for path in ignored}
    paths.update(relative for relative in known if os.path.lexists(root / relative))
    rows = []
    for relative in sorted(paths):
        if any(relative.startswith(candidate + "/") for candidate in known):
            continue
        row: dict[str, Any] = {
            "resource_type": "registered-build-output" if relative in build_resources else configured.get(relative, "unknown"),
            "path": relative,
            "classification": "blocked",
            "cleanup_action": "none",
            "blocker": None,
        }
        if relative in known:
            try:
                row["evidence"] = resource_evidence(
                    root,
                    relative,
                    build_declaration=build_resources.get(relative),
                    resource_types=configured,
                )
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
