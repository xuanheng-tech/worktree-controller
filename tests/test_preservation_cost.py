import hashlib
import os
import socket
import stat
from pathlib import Path

import pytest

from tests.conftest import _remove_test_special_nodes
from tests.test_typed_disposable import target as target
from worktree_controller import disposable, lifecycle, resource_admission
from worktree_controller.models import ControllerError


def test_resource_tree_digest_keeps_existing_path_and_metadata_contract(tmp_path):
    resource = tmp_path / "resource"
    (resource / ".nested").mkdir(parents=True)
    (resource / ".nested/first.js").write_bytes(b"nested")
    (resource / "z.js").write_bytes(b"last")
    (resource / "雪.css").write_bytes(b"unicode")
    (resource / "back\\slash.js").write_bytes(b"literal backslash")
    raw_name = os.fsdecode(b"raw-\xff.js")
    (resource / raw_name).write_bytes(b"non-UTF-8 filename")
    (resource / "inside-link").symlink_to(".nested/first.js")
    os.link(resource / ".nested/first.js", resource / "hard.js")
    (resource / "bin").mkdir()
    outside = tmp_path / "python"
    outside.write_bytes(b"interpreter fixture")
    (resource / "bin/python").symlink_to(outside)
    order = [".", ".nested", ".nested/first.js", "back\\slash.js", "bin", "bin/python",
             "hard.js", "inside-link", raw_name, "z.js", "雪.css"]
    digest = hashlib.sha256()
    size = 0
    for relative in order:
        path = resource if relative == "." else resource / relative
        metadata = path.lstat()
        digest.update(os.fsencode(relative) + b"\0")
        digest.update(":".join(map(str, (
            metadata.st_dev, metadata.st_ino, metadata.st_mode,
            metadata.st_size, metadata.st_mtime_ns,
        ))).encode())
        size += metadata.st_size
        if stat.S_ISLNK(metadata.st_mode):
            digest.update(os.fsencode(os.readlink(path)))
    assert disposable._tree_identity(
        resource, interpreter_links=("bin/python",), interpreter_target=str(outside)
    ) == {
        "tree_sha256": digest.hexdigest(), "entries": len(order), "bytes": size,
    }


def test_resource_inventory_does_not_rebuild_ancestors_for_each_entry(tmp_path, monkeypatch):
    resource = tmp_path / "resource/deep/parent"
    resource.mkdir(parents=True)
    for index in range(64):
        (resource / f"{index:03}.js").write_bytes(b"data")
    calls = []
    original = Path.relative_to

    def observed(path, *args, **kwargs):
        calls.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "relative_to", observed)
    evidence = disposable._tree_identity(resource)
    assert evidence["entries"] == 65
    assert len(calls) <= 1


@pytest.mark.parametrize("mutation,code", [
    ("fifo", "RESOURCE_IDENTITY_MISMATCH"),
    ("escape", "RESOURCE_PATH_ESCAPE"),
    ("owner", "RESOURCE_OWNER_MISMATCH"),
    ("mount", "RESOURCE_IDENTITY_MISMATCH"),
    ("bound", "RESOURCE_SCAN_LIMIT"),
])
def test_resource_inventory_still_refuses_unsafe_entries(tmp_path, monkeypatch, mutation, code):
    resource = tmp_path / "resource"
    resource.mkdir()
    (resource / "nested").mkdir()
    if mutation == "fifo":
        os.mkfifo(resource / "pipe")
    elif mutation == "escape":
        outside = tmp_path / "outside"
        outside.write_bytes(b"preserve")
        (resource / "escape").symlink_to(outside)
    elif mutation == "owner":
        uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: uid + 1)
    elif mutation == "mount":
        original = Path.is_mount
        monkeypatch.setattr(Path, "is_mount", lambda p: p == resource / "nested" or original(p))
    else:
        monkeypatch.setattr(disposable, "MAX_VENV_ENTRIES", 1)
    with pytest.raises(ControllerError) as failure:
        disposable._tree_identity(resource)
    assert failure.value.code == code


def test_full_topology_still_hashes_content_when_metadata_digest_matches(target):
    repository, worktree, _ = target
    path = worktree / "frontend/node_modules/vite/opaque.js"
    path.write_bytes(b"old bytes")
    options = dict(target=worktree, owner="module-owner", expected_record_version=2, dry_run=True)
    before = lifecycle.cleanup_typed_disposable(repository.repo, **options)["decision"]["plan"]
    assert not before["blockers"]
    metadata = path.stat()
    path.write_bytes(b"new bytes")
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    after = lifecycle.cleanup_typed_disposable(repository.repo, **options)["decision"]["plan"]
    assert not after["blockers"]
    def evidence(plan):
        return next(r["evidence"] for r in plan["resources"] if r["path"] == "frontend/node_modules")
    assert evidence(before)["tree_sha256"] == evidence(after)["tree_sha256"]
    member = ["frontend/node_modules", "vite/opaque.js"]
    for plan, data in [(before, b"old bytes"), (after, b"new bytes")]:
        group = next(g for g in plan["hardlink_topology"]["groups"].values() if member in g["members"])
        assert group["content"]["content_sha256"] == hashlib.sha256(data).hexdigest()
    assert before["hardlink_topology"]["sha256"] != after["hardlink_topology"]["sha256"]


def test_test_node_teardown_preserves_files_modes_and_external_nodes(tmp_path):
    inside, outside = tmp_path / "inside", tmp_path / "outside"
    inside.mkdir()
    outside.mkdir()
    os.mkfifo(outside / "external-pipe")
    (inside / "escape").symlink_to(outside, target_is_directory=True)
    (inside / "receipt.json").write_bytes(b"retained evidence")
    os.mkfifo(inside / "pipe")
    with socket.socket(socket.AF_UNIX) as endpoint:
        directory = os.open(inside, os.O_RDONLY | os.O_DIRECTORY)
        try:
            endpoint.bind(f"/proc/self/fd/{directory}/endpoint")
        finally:
            os.close(directory)
    inside.chmod(0o500)
    _remove_test_special_nodes(inside)
    assert stat.S_IMODE(inside.stat().st_mode) == 0o500
    assert not (inside / "pipe").exists() and not (inside / "endpoint").exists()
    assert (inside / "receipt.json").read_bytes() == b"retained evidence"
    assert stat.S_ISFIFO((outside / "external-pipe").lstat().st_mode)
    assert (inside / "escape").is_symlink()


def test_test_monkeypatch_undo_keeps_host_resources_isolated(tmp_path, monkeypatch):
    expected = str(tmp_path / "host-state")
    assert os.environ["XDG_STATE_HOME"] == expected
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "test-specific"))
    monkeypatch.undo()
    assert os.environ["XDG_STATE_HOME"] == expected
    assert resource_admission.filesystem(tmp_path)["available_bytes"] == 10**15
