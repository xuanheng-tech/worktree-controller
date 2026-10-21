from __future__ import annotations

import copy
import errno
import json
import os
import socket
import stat
from pathlib import Path

import pytest

from tests.test_typed_disposable import target as target
from worktree_controller import preservation as p
from worktree_controller.models import ControllerError

ALLOCATION = "11111111-1111-4111-8111-111111111111"
REPOSITORY = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    worktree = tmp_path / "worktree"
    source = worktree / ".venv"
    state = source / "agent-workspace"
    state.mkdir(parents=True)
    for directory in ("executor-state", "tmp", "npm-logs", "empty"):
        (state / directory).mkdir(mode=0o750)
    for name, content in {
        "executor-state/state.sqlite": b"opaque fixture\x00\xffdatabase bytes",
        "executor-state/state.sqlite-wal": b"opaque fixture WAL\x00\xfe",
        "executor-state/state.sqlite-shm": b"opaque fixture SHM\x00\xfd",
        "tmp/unknown": b"unknown bytes\x00\xfc",
        "npm-logs/log": b"unknown log\x00\xfb",
    }.items():
        (state / name).write_bytes(content)
        (state / name).chmod(0o640)
    os.mkfifo(state / "tmp/pipe", 0o600)
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"external sentinel remains unchanged")
    (state / "external").symlink_to(sentinel)
    (state / "broken").symlink_to("missing/target")
    authority = {
        "identity": {
            "repository_id": REPOSITORY,
            "allocation_id": ALLOCATION,
            "target_path": str(worktree),
            "expected_head": "a" * 40,
            "expected_record_version": 2,
            "owner": "fixture-owner",
            "authority_key": "fixture",
        },
        "runtime_evidence": {
            "contract": "allocation-runtime-authority/v1",
            "managed_references": [],
            "observed_references": [],
            "managed_visibility_uncertainties": [],
            "scan_uncertainties": [],
            "host_visibility_gaps": [{"pid": 99999999, "classification": "HOST_VISIBILITY_GAP"}],
        },
        "warnings": [{"code": "HOST_VISIBILITY_GAP", "message": "no host-wide absence claim"}],
        "worktree_paths": [str(worktree)],
    }

    def governance(repo, target, allocation, owner, version, *, source_relative=p.SOURCE):
        if (target, allocation, owner, version) != (worktree, ALLOCATION, "fixture-owner", 2):
            raise ControllerError("PRESERVATION_ALLOCATION_MISMATCH", "fixture allocation differs")
        return copy.deepcopy(authority)

    monkeypatch.setattr(p, "_authority", governance)
    root = tmp_path / "evidence"
    plan = p.plan_preservation(worktree, worktree, ALLOCATION, "fixture-owner", 2, state_root=root)
    return source, root, plan, sentinel


def execute(legacy):
    _, root, plan, _ = legacy
    return p.copy_preservation(plan, state_root=root)


def assert_no_receipt(plan):
    assert not (Path(plan["destination"]) / "receipt.json").exists()


def test_complete_preservation_and_fifo_node_never_opened(legacy, monkeypatch):
    source, root, plan, sentinel = legacy
    original_open, original_read = os.open, os.read
    fifo_io = []

    def guarded_open(path, flags, *args, **kwargs):
        if str(path).endswith("pipe") and not flags & os.O_PATH:
            fifo_io.append(path)
            pytest.fail("FIFO endpoint must never be opened")
        return original_open(path, flags, *args, **kwargs)

    def guarded_read(fd, size):
        assert not stat.S_ISFIFO(os.fstat(fd).st_mode), "FIFO must never be read"
        return original_read(fd, size)

    monkeypatch.setattr(os, "open", guarded_open)
    monkeypatch.setattr(os, "read", guarded_read)
    result = execute(legacy)
    destination = Path(result["generation"]) / "tree"
    assert p.verify_preservation(plan, state_root=root)["verification"] == "PASS"
    assert not fifo_io
    assert result["tree_sha256"] == plan["source_tree"]["tree_sha256"]
    src = source / "agent-workspace/tmp/pipe"
    dst = destination / "agent-workspace/tmp/pipe"
    assert stat.S_ISFIFO(dst.lstat().st_mode)
    assert stat.S_IMODE(dst.lstat().st_mode) == 0o600
    assert src.lstat().st_ino != dst.lstat().st_ino
    assert sentinel.read_bytes() == b"external sentinel remains unchanged"
    assert os.readlink(destination / "agent-workspace/external") == str(sentinel)
    assert os.readlink(destination / "agent-workspace/broken") == "missing/target"
    rows = plan["source_tree"]["manifest"]
    fifo = next(row for row in rows if row["type"] == "fifo")
    assert "content_sha256" not in fifo and "size" not in fifo
    assert fifo["ownership"] == {"uid": os.getuid(), "gid": os.getgid()}
    for row in rows:
        if row["type"] == "regular":
            assert (source / row["path"]).read_bytes() == (destination / row["path"]).read_bytes()
    receipt = json.loads(Path(result["receipt"]).read_text())
    assert receipt["runtime_checks"][-1]["host_visibility_gaps"]
    assert receipt["warnings"][0]["code"] == "HOST_VISIBILITY_GAP"
    assert receipt["cleanup_authorized"] is False
    assert stat.S_IMODE(Path(result["receipt"]).stat().st_mode) == 0o400
    assert stat.S_IMODE(Path(result["generation"]).stat().st_mode) == 0o500
    assert source.exists()


@pytest.mark.parametrize("replacement", ["regular", "fifo", "symlink"])
@pytest.mark.parametrize("when", ["after-plan", "during-copy"])
def test_fifo_identity_replacement_blocks_before_success(legacy, monkeypatch, replacement, when):
    source, _, plan, sentinel = legacy
    fifo = source / "agent-workspace/tmp/pipe"

    def replace():
        # Retain the original inode so immediate inode reuse cannot mask this fixture.
        fifo.rename(fifo.with_name("original-pipe"))
        if replacement == "regular":
            fifo.write_bytes(b"replacement")
        elif replacement == "fifo":
            os.mkfifo(fifo, 0o600)
        else:
            fifo.symlink_to(sentinel)

    if when == "after-plan":
        replace()
    else:
        original = p._copy_node

        def race(*args, **kwargs):
            replace()
            return original(*args, **kwargs)

        monkeypatch.setattr(p, "_copy_node", race)
    with pytest.raises(ControllerError):
        execute(legacy)
    assert_no_receipt(plan)
    assert sentinel.read_bytes() == b"external sentinel remains unchanged"


def test_fifo_replaced_at_mkfifo_boundary_fails(legacy, monkeypatch):
    source, _, plan, sentinel = legacy
    original = os.mkfifo

    def race(path, mode=0o666, *, dir_fd=None):
        fifo = source / "agent-workspace/tmp/pipe"
        fifo.rename(fifo.with_name("original-pipe"))
        fifo.symlink_to(sentinel)
        return original(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkfifo", race)
    with pytest.raises(ControllerError):
        execute(legacy)
    assert_no_receipt(plan)
    assert sentinel.read_bytes() == b"external sentinel remains unchanged"


def test_destination_fifo_same_type_and_mode_replacement_is_still_a_race(legacy, monkeypatch):
    _, _, plan, _ = legacy
    original = p._copy_node
    pins = []

    def replace_after_copy(*args, **kwargs):
        result = original(*args, **kwargs)
        fifo = Path(plan["destination"]) / "tree/agent-workspace/tmp/pipe"
        pins.append(os.open(fifo, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC))
        fifo.unlink()
        os.mkfifo(fifo, 0o600)
        return result

    monkeypatch.setattr(p, "_copy_node", replace_after_copy)
    try:
        with pytest.raises(ControllerError):
            execute(legacy)
        assert_no_receipt(plan)
    finally:
        for fd in pins:
            os.close(fd)


def test_destination_parent_symlink_swap_at_fifo_creation_never_touches_external(
    legacy, monkeypatch, tmp_path
):
    _, _, plan, _ = legacy
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "pipe"
    sentinel.write_bytes(b"outside sentinel")
    original = os.mkfifo

    def race(name, mode=0o666, *, dir_fd=None):
        parent = Path(plan["destination"]) / "tree/agent-workspace/tmp"
        parent.rename(parent.with_name("old-tmp"))
        parent.symlink_to(outside, target_is_directory=True)
        return original(name, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkfifo", race)
    with pytest.raises(ControllerError):
        execute(legacy)
    assert sentinel.read_bytes() == b"outside sentinel"
    assert_no_receipt(plan)


@pytest.mark.parametrize("replacement", ["wrong-mode", "regular", "symlink", "collision"])
def test_destination_fifo_type_mode_and_collision_fail(legacy, monkeypatch, replacement):
    _, _, plan, sentinel = legacy
    original = os.mkfifo

    def bad_create(name, mode=0o666, *, dir_fd=None):
        if replacement == "wrong-mode":
            return original(name, 0o400, dir_fd=dir_fd)
        if replacement == "regular":
            fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=dir_fd)
            os.close(fd)
            return None
        if replacement == "symlink":
            return os.symlink(sentinel, name, dir_fd=dir_fd)
        original(name, mode, dir_fd=dir_fd)
        return original(name, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkfifo", bad_create)
    with pytest.raises((ControllerError, FileExistsError)):
        execute(legacy)
    assert_no_receipt(plan)
    assert sentinel.read_bytes() == b"external sentinel remains unchanged"


@pytest.mark.parametrize("kind", ["socket", "character-device", "block-device", "unknown"])
def test_unsupported_special_nodes_block(legacy, monkeypatch, kind):
    source, root, plan, _ = legacy
    special = source / "agent-workspace/unsupported"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        parent = os.open(special.parent, p.DIRECTORY_FLAGS)
        try:
            sock.bind(f"/proc/self/fd/{parent}/{special.name}")
        finally:
            os.close(parent)
        if kind != "socket":
            original = os.stat

            def fake_stat(path, *args, **kwargs):
                result = original(path, *args, **kwargs)
                if str(path) == "unsupported":
                    values = list(result)
                    values[0] = {
                        "character-device": stat.S_IFCHR,
                        "block-device": stat.S_IFBLK,
                        "unknown": 0,
                    }[kind] | 0o600

                    # Simulate node metadata without creating real device nodes or using privileges.
                    class Metadata:
                        st_mode = values[0]

                        def __getattr__(self, name):
                            return getattr(result, name)

                    return Metadata()
                return result

            monkeypatch.setattr(os, "stat", fake_stat)
        current = p.plan_preservation(
            Path(plan["repository"]), source.parent, ALLOCATION, "fixture-owner", 2, state_root=root
        )
        assert not current["ready"]
        assert current["source_tree"]["inventory"]["other_special_nodes"] == 1
        with pytest.raises(ControllerError, match="complete exact"):
            p.copy_preservation(current, state_root=root)
    special.unlink()


def test_regular_content_mutation_during_copy_fails(legacy, monkeypatch):
    source, _, plan, _ = legacy
    original = p._transfer
    mutated = False

    def race(fd, destination=None):
        nonlocal mutated
        result = original(fd, destination)
        if destination is not None and not mutated:
            mutated = True
            (source / "agent-workspace/executor-state/state.sqlite").write_bytes(b"changed bytes")
        return result

    monkeypatch.setattr(p, "_transfer", race)
    with pytest.raises(ControllerError):
        execute(legacy)
    assert_no_receipt(plan)


def test_regular_path_swapped_to_fifo_never_opens_endpoint(legacy, monkeypatch):
    source, _, plan, _ = legacy
    original = os.open
    swapped = False

    def race(name, flags, *args, **kwargs):
        nonlocal swapped
        if name == "state.sqlite" and flags & os.O_PATH and not swapped:
            swapped = True
            file = source / "agent-workspace/executor-state/state.sqlite"
            file.rename(file.with_name("original-db"))
            os.mkfifo(file, 0o600)
        if name == "state.sqlite" and not flags & os.O_PATH:
            pytest.fail("mutable regular pathname must not be reopened for I/O")
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", race)
    with pytest.raises(ControllerError):
        execute(legacy)
    assert_no_receipt(plan)


def test_unreadable_regular_file_reported_and_no_copy(legacy, monkeypatch):
    source, root, plan, _ = legacy
    original = os.open

    def deny(name, flags, *args, **kwargs):
        if name == "state.sqlite":
            raise PermissionError(errno.EACCES, "synthetic permission failure")
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", deny)
    current = p.plan_preservation(
        Path(plan["repository"]), source.parent, ALLOCATION, "fixture-owner", 2, state_root=root
    )
    assert not current["ready"]
    assert current["source_tree"]["inventory"]["unreadable_regular_files"] == 1
    with pytest.raises(ControllerError):
        p.copy_preservation(current, state_root=root)


@pytest.mark.parametrize(
    "mutation", ["source", "missing", "digest", "fifo-mode", "fifo-type", "symlink"]
)
def test_after_receipt_changes_block_reverification(legacy, mutation):
    source, root, plan, sentinel = legacy
    result = execute(legacy)
    destination = Path(result["generation"]) / "tree/agent-workspace"
    receipt = Path(result["receipt"]).read_bytes()
    if mutation == "source":
        (source / "agent-workspace/tmp/unknown").write_bytes(b"changed source")
    elif mutation == "missing":
        (destination / "tmp/unknown").unlink()
    elif mutation == "digest":
        (destination / "tmp/unknown").write_bytes(b"different bytes")
    elif mutation == "fifo-mode":
        (destination / "tmp/pipe").chmod(0o400)
    elif mutation == "fifo-type":
        (destination / "tmp/pipe").unlink()
        (destination / "tmp/pipe").write_bytes(b"")
    else:
        (destination / "external").unlink()
        (destination / "external").symlink_to("different-target")
    with pytest.raises(ControllerError):
        p.verify_preservation(plan, state_root=root)
    assert Path(result["receipt"]).read_bytes() == receipt
    assert sentinel.read_bytes() == b"external sentinel remains unchanged"


def test_destination_generation_collision_never_overwrites(legacy):
    _, _, plan, _ = legacy
    result = execute(legacy)
    before = Path(result["receipt"]).read_bytes()
    with pytest.raises(ControllerError, match="generation already exists"):
        execute(legacy)
    assert Path(result["receipt"]).read_bytes() == before


@pytest.mark.parametrize("mutation", ["allocation", "old-tree"])
def test_wrong_receipt_or_old_tree_identity_block(legacy, mutation):
    _, root, plan, _ = legacy
    result = execute(legacy)
    receipt_path = Path(result["receipt"])
    receipt = json.loads(receipt_path.read_text())
    if mutation == "allocation":
        receipt["allocation_source_identity"]["allocation_id"] = REPOSITORY
    else:
        receipt["source_tree_sha256"] = "0" * 64
    receipt_path.chmod(0o600)
    receipt_path.write_bytes(p._json_bytes(receipt))
    with pytest.raises(ControllerError, match="receipt does not bind"):
        p.verify_preservation(plan, state_root=root)


def test_partial_copy_io_failure_never_creates_success_receipt(legacy, monkeypatch):
    _, _, plan, _ = legacy
    original = p._transfer

    def fail(fd, destination=None):
        if destination is not None:
            os.write(destination, b"partial")
            raise OSError(errno.ENOSPC, "synthetic full disk")
        return original(fd, destination)

    monkeypatch.setattr(p, "_transfer", fail)
    with pytest.raises(OSError):
        execute(legacy)
    assert_no_receipt(plan)
    assert Path(plan["source"]).exists()


def test_source_mutation_while_writing_manifest_does_not_create_success_receipt(
    legacy, monkeypatch
):
    source, _, plan, _ = legacy
    original = p._write_once

    def race(parent, name, value):
        result = original(parent, name, value)
        if name == "manifest.json":
            (source / "agent-workspace/tmp/unknown").write_bytes(
                b"changed at verification boundary"
            )
        return result

    monkeypatch.setattr(p, "_write_once", race)
    with pytest.raises(ControllerError):
        execute(legacy)
    assert_no_receipt(plan)


def test_source_parent_symlink_swap_blocks_and_leaves_external_sentinel(legacy):
    source, _, plan, sentinel = legacy
    source.rename(source.with_name("old-state"))
    source.symlink_to(sentinel.parent, target_is_directory=True)
    with pytest.raises((ControllerError, OSError)):
        execute(legacy)
    assert_no_receipt(plan)
    assert sentinel.read_bytes() == b"external sentinel remains unchanged"


def test_destination_ancestor_inode_replacement_after_plan_blocks(legacy):
    source, root, plan, _ = legacy
    root.mkdir()
    # Use an already-existing destination root to bind its inode into the plan.
    plan = p.plan_preservation(
        source.parent, source.parent, ALLOCATION, "fixture-owner", 2, state_root=root
    )
    root.rename(root.with_name("old-evidence"))
    root.mkdir()
    with pytest.raises(ControllerError, match="ancestor identity changed"):
        p.copy_preservation(plan, state_root=root)
    assert_no_receipt(plan)


def test_cli_plan_copy_verify_and_generation_retains_exact_plan(
    legacy, monkeypatch, tmp_path, capsys
):
    source, root, _, _ = legacy
    monkeypatch.setattr(p, "evidence_root", lambda: root)
    plan_file = tmp_path / "plan.json"
    assert (
        p.main(
            [
                "plan",
                "--repo",
                str(source.parent),
                "--path",
                str(source.parent),
                "--allocation-id",
                ALLOCATION,
                "--owner",
                "fixture-owner",
                "--expected-record-version",
                "2",
                "--plan-file",
                str(plan_file),
            ]
        )
        == 0
    )
    planned = json.loads(capsys.readouterr().out)
    assert planned["result"] == "PRESERVATION_PLAN_READY"
    assert p.main(["copy", "--plan-file", str(plan_file)]) == 0
    result = json.loads(capsys.readouterr().out)
    retained_plan = Path(result["generation"]) / "plan.json"
    assert retained_plan.read_bytes() == plan_file.read_bytes()
    assert p.main(["verify", "--plan-file", str(retained_plan)]) == 0
    assert json.loads(capsys.readouterr().out)["verification"] == "PASS"


def test_runtime_guard_called_only_after_source_inspection_fds_closed(legacy, monkeypatch):
    source, _, _, _ = legacy
    original = p._authority

    def guard(*args, **kwargs):
        for name in os.listdir("/proc/self/fd"):
            try:
                link = os.readlink(f"/proc/self/fd/{name}")
            except FileNotFoundError:
                continue
            assert not link.startswith(str(source.parent)), (
                "inspection itself must not hold a target reference"
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(p, "_authority", guard)
    execute(legacy)
    _, root, plan, _ = legacy
    assert p.verify_preservation(plan, state_root=root)["verification"] == "PASS"


def test_formal_authority_and_cleanup_contract_are_unchanged(target, tmp_path):
    repository, worktree, allocation = target
    state = worktree / ".venv/agent-workspace"
    state.mkdir(parents=True)
    (state / "opaque").write_bytes(b"synthetic preserved state")
    os.mkfifo(state / "pipe", 0o600)
    root = tmp_path / "evidence"
    plan = p.plan_preservation(
        repository.repo, worktree, allocation, "module-owner", 2, state_root=root
    )
    from worktree_controller.git_backend import inventory
    from worktree_controller.lifecycle import cleanup_typed_disposable
    from worktree_controller.store import MetadataStore

    store = MetadataStore(inventory(repository.repo).common_dir)
    before = {str(file): file.read_bytes() for file in store.root.rglob("*.json")}
    p.copy_preservation(plan, state_root=root)
    assert p.verify_preservation(plan, state_root=root)["verification"] == "PASS"
    after = {str(file): file.read_bytes() for file in store.root.rglob("*.json")}
    assert before == after
    cleanup = cleanup_typed_disposable(
        repository.repo,
        target=worktree,
        expected_record_version=2,
        owner="module-owner",
        dry_run=True,
    )
    assert cleanup["decision"]["code"] == "RESOURCE_CLEANUP_BLOCKED"
    assert cleanup["decision"]["plan"]["cleanup_paths"] == []
    assert worktree.exists()


@pytest.mark.parametrize(
    "kind", ["managed_references", "observed_references", "managed_visibility_uncertainties"]
)
def test_formal_runtime_blockers_still_block_preservation(target, tmp_path, monkeypatch, kind):
    from tests.test_typed_disposable import empty_runtime_evidence
    from worktree_controller import lifecycle

    repository, worktree, allocation = target
    (worktree / ".venv/agent-workspace").mkdir(parents=True)
    evidence = empty_runtime_evidence()
    evidence[kind] = [
        {"pid": 1234567, "kind": "cwd", "source": "synthetic-holder", "classification": kind}
    ]
    monkeypatch.setattr(lifecycle, "runtime_authority_evidence", lambda *args, **kwargs: evidence)
    with pytest.raises(ControllerError):
        p.plan_preservation(
            repository.repo,
            worktree,
            allocation,
            "module-owner",
            2,
            state_root=tmp_path / "evidence",
        )


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
@pytest.mark.parametrize("kind", ["regular", "symlink"])
def test_exact_root_leaf_copy_keeps_source_and_never_opens_link_target(
    target, tmp_path, monkeypatch, kind
):
    repository, worktree, allocation = target
    from tests.test_shadow import git

    ignore = worktree / ".gitignore"
    ignore.write_text(ignore.read_text() + ".venv\n")
    git(worktree, "add", ".gitignore")
    git(worktree, "commit", "-m", "synthetic ignored leaf identity")
    node = worktree / ".venv"
    sentinel = tmp_path / "outside-leaf-sentinel"
    sentinel.write_bytes(b"synthetic external bytes remain untouched")
    if kind == "regular":
        node.write_bytes(b"opaque historical leaf\x00\xff")
    else:
        node.symlink_to(sentinel)
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "leaf-preservations")
    original_open = os.open

    def no_target_io(name, flags, *args, **kwargs):
        assert str(name) != str(sentinel)
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", no_target_io)
    plan = p.plan_preservation(
        repository.repo, worktree, allocation, "module-owner", 2, source_relative=".venv"
    )
    assert plan["source_tree"]["manifest"][0]["type"] == kind
    copied = p.copy_preservation(plan)
    assert p.verify_preservation(plan)["verification"] == "PASS"
    assert os.path.lexists(node)
    assert sentinel.read_bytes() == b"synthetic external bytes remain untouched"
    destination = Path(copied["generation"]) / "tree"
    if kind == "symlink":
        assert os.readlink(node) == os.readlink(destination) == str(sentinel)
    else:
        assert node.read_bytes() == destination.read_bytes() == b"opaque historical leaf\x00\xff"


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
@pytest.mark.parametrize("environment_owner", ["canonical", "linked"])
def test_copy_preparation_keeps_external_mapping_when_selected_source_is_link(
    target, tmp_path, monkeypatch, environment_owner
):
    from tests.test_controlled import acquire_one
    from tests.test_shadow import git

    repository, worktree, allocation = target
    ignore = worktree / ".gitignore"
    ignore.write_text(ignore.read_text() + "unknown\n")
    git(worktree, "add", ".gitignore")
    git(worktree, "commit", "-m", "synthetic ignored link identity")
    if environment_owner == "canonical":
        external_checkout = repository.repo
    else:
        external_checkout, _acquired = acquire_one(
            repository, task="external-mapping-fixture", authority="external-mapping-authority"
        )
    environment = external_checkout / ".venv"
    environment.mkdir()
    mapping = environment / "fixture-editable.pth"
    mapping.write_text(str(worktree) + "\n")
    mapping_before = mapping.read_bytes()
    link = worktree / "unknown"
    link.symlink_to(environment, target_is_directory=True)
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "preservations")

    with pytest.raises(ControllerError) as blocked:
        p.plan_preservation(
            repository.repo, worktree, allocation, "module-owner", 2, source_relative="unknown"
        )
    assert blocked.value.code == "STALE_EDITABLE_REFERENCE"
    assert environment.is_dir() and not environment.is_symlink()
    assert link.is_symlink() and os.readlink(link) == str(environment)
    assert mapping.read_bytes() == mapping_before
    assert not (tmp_path / "preservations").exists()


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
def test_copy_preparation_keeps_self_mappings_and_other_unknown_blockers(
    target, tmp_path, monkeypatch
):
    from tests.test_shadow import git
    from worktree_controller.git_backend import inventory
    from worktree_controller.store import MetadataStore

    repository, worktree, allocation = target
    local_roots = (worktree / ".venv", worktree / "backend/.venv")
    local_roots[0].mkdir()
    mappings = tuple(root / "self-editable.pth" for root in local_roots)
    for mapping in mappings:
        mapping.write_text(str(worktree) + "\n")
    source = worktree / "unknown/state.bin"
    source.parent.mkdir()
    source.write_bytes(b"selected opaque local fixture\x00\xff")
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "preservations")
    store = MetadataStore(inventory(repository.repo).common_dir)
    before_record = store.load().records[allocation]
    head = git(worktree, "rev-parse", "HEAD")
    mapping_bytes = {mapping: mapping.read_bytes() for mapping in mappings}
    from worktree_controller import lifecycle

    original_runtime = lifecycle.runtime_authority_evidence
    environment_identities = {(root.stat().st_dev, root.stat().st_ino) for root in local_roots}

    def runtime_without_environment_inspection_fds(*args, **kwargs):
        for entry in Path("/proc/self/fd").iterdir():
            try:
                metadata = os.fstat(int(entry.name))
            except OSError:
                continue
            assert (metadata.st_dev, metadata.st_ino) not in environment_identities
        return original_runtime(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "runtime_authority_evidence", runtime_without_environment_inspection_fds)
    plan = p.plan_preservation(
        repository.repo, worktree, allocation, "module-owner", 2, source_relative="unknown"
    )
    assert plan["ready"] is True
    assert any(blocker.get("path") == ".venv" for blocker in plan["authority"]["cleanup_not_ready"])
    assert plan["authority"]["identity"]["expected_head"] == head
    copied = p.copy_preservation(plan)
    assert p.verify_preservation(plan)["verification"] == "PASS"
    assert Path(copied["receipt"]).is_file()
    assert store.load().records[allocation] == before_record
    assert git(worktree, "rev-parse", "HEAD") == head
    assert source.read_bytes() == b"selected opaque local fixture\x00\xff"
    for mapping, raw in mapping_bytes.items():
        assert mapping.read_bytes() == raw


@pytest.mark.parametrize("operation", ["plan", "copy"])
def test_internal_authority_type_error_never_retries_another_source(legacy, monkeypatch, operation):
    source, root, plan, _sentinel = legacy
    selected = "unknown" if operation == "plan" else ".venv"
    (source.parent / "unknown").mkdir()
    (source.parent / "unknown/state.bin").write_bytes(b"synthetic opaque selected source")
    calls = []

    def broken_authority(repo, target, allocation, owner, version, **options):
        calls.append(options.get("source_relative", ".venv"))
        if "source_relative" in options:
            raise TypeError("synthetic internal authority failure")
        return copy.deepcopy(plan["authority"])

    monkeypatch.setattr(p, "_authority", broken_authority)
    with pytest.raises(TypeError, match="synthetic internal authority failure"):
        if operation == "plan":
            p.plan_preservation(
                source.parent, source.parent, ALLOCATION, "fixture-owner", 2,
                source_relative=selected, state_root=root,
            )
        else:
            p.copy_preservation(plan, state_root=root)
    assert calls == [selected]
    assert_no_receipt(plan)
    assert not Path(plan["destination"]).exists()
    assert (source.parent / "unknown/state.bin").read_bytes() == b"synthetic opaque selected source"


def test_nested_source_copy_rejects_replaced_checkout_root(legacy):
    source, root, _plan, _sentinel = legacy
    checkout = source.parent
    backend = checkout / "backend"
    backend.mkdir()
    source.rename(backend / ".venv")
    plan = p.plan_preservation(
        checkout, checkout, ALLOCATION, "fixture-owner", 2,
        source_relative="backend/.venv", state_root=root,
    )
    nested = backend / ".venv"
    parent_inode = backend.stat().st_ino
    source_inode = nested.stat().st_ino
    before = (nested / "agent-workspace/tmp/unknown").read_bytes()
    previous = checkout.with_name("previous-worktree")
    checkout.rename(previous)
    checkout.mkdir()
    (previous / "backend").rename(backend)
    assert backend.stat().st_ino == parent_inode
    assert nested.stat().st_ino == source_inode
    with pytest.raises(ControllerError) as blocked:
        p.copy_preservation(plan, state_root=root)
    assert blocked.value.code == "PRESERVATION_SOURCE_CHANGED"
    assert not Path(plan["destination"]).exists()
    assert (nested / "agent-workspace/tmp/unknown").read_bytes() == before


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
@pytest.mark.parametrize("anchor_contract", ["legacy", "all-ancestors"])
def test_independent_verifier_accepts_old_and_new_nested_preservation_generations(
    target, tmp_path, monkeypatch, anchor_contract
):
    from worktree_controller import preserved_cleanup as pc

    repository, worktree, allocation = target
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "preservations")
    plan = p.plan_preservation(
        repository.repo, worktree, allocation, "module-owner", 2,
        source_relative="backend/.venv",
    )
    if anchor_contract == "legacy":
        del plan["source_ancestor_identities"]
        plan["plan_sha256"] = p._sha({key: value for key, value in plan.items() if key != "plan_sha256"})
    else:
        assert plan["source_ancestor_identities"][str(worktree)] == p._inode(worktree.stat())
    result = p.copy_preservation(plan)
    receipt = Path(result["receipt"])
    before = receipt.read_bytes()
    verified, bound_plan = pc.coverage(receipt, result["receipt_sha256"], identity=plan["authority"]["identity"])
    assert verified["receipt_sha256"] == result["receipt_sha256"]
    assert bound_plan == plan
    assert p.verify_preservation(plan)["verification"] == "PASS"
    assert receipt.read_bytes() == before
    assert (worktree / "backend/.venv").is_dir()


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
@pytest.mark.parametrize("mutation", ["missing-root", "changed-root"])
def test_new_nested_source_ancestor_evidence_cannot_omit_or_change_target_root(
    target, tmp_path, monkeypatch, mutation
):
    repository, worktree, allocation = target
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "preservations")
    plan = p.plan_preservation(
        repository.repo, worktree, allocation, "module-owner", 2,
        source_relative="backend/.venv",
    )
    if mutation == "missing-root":
        del plan["source_ancestor_identities"][str(worktree)]
    else:
        plan["source_ancestor_identities"][str(worktree)]["inode"] += 1
    plan["plan_sha256"] = p._sha({key: value for key, value in plan.items() if key != "plan_sha256"})
    with pytest.raises(ControllerError) as blocked:
        p.copy_preservation(plan)
    assert blocked.value.code == "PRESERVATION_SOURCE_CHANGED"
    assert not Path(plan["destination"]).exists()
    assert (worktree / "backend/.venv").is_dir()


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
@pytest.mark.parametrize("replacement", ["persistent", "restore-original"])
def test_copy_preparation_never_excludes_an_outside_environment_during_link_swap(
    target, tmp_path, monkeypatch, replacement
):
    from tests.test_shadow import git
    from worktree_controller import environment

    repository, worktree, allocation = target
    ignore = worktree / ".gitignore"
    ignore.write_text(ignore.read_text() + ".venv\n.venv-held/\n")
    git(worktree, "add", ".gitignore")
    git(worktree, "commit", "-m", "synthetic ignored environment race paths")
    local = worktree / ".venv"
    local.mkdir()
    (local / "self-editable.pth").write_text(str(worktree) + "\n")
    original_identity = (local.stat().st_dev, local.stat().st_ino)
    outside = repository.repo / ".venv"
    outside.mkdir()
    mapping = outside / "outside-editable.pth"
    mapping.write_text(str(worktree) + "\n")
    mapping_before = mapping.read_bytes()
    source = worktree / "unknown/state.bin"
    source.parent.mkdir()
    source.write_bytes(b"opaque synthetic source stays untouched")
    monkeypatch.setattr(p, "evidence_root", lambda: tmp_path / "preservations")
    original_scan = environment.editable_references
    held = worktree / ".venv-held"
    scan_count = 0

    def swap_before_scan(*args, **kwargs):
        nonlocal scan_count
        scan_count += 1
        local.rename(held)
        local.symlink_to(outside, target_is_directory=True)
        try:
            return original_scan(*args, **kwargs)
        finally:
            if replacement == "restore-original":
                local.unlink()
                held.rename(local)

    monkeypatch.setattr(environment, "editable_references", swap_before_scan)
    with pytest.raises((ControllerError, OSError)):
        p.plan_preservation(
            repository.repo, worktree, allocation, "module-owner", 2,
            source_relative="unknown",
        )
    assert scan_count == 1
    assert git(worktree, "status", "--porcelain") == ""
    assert source.read_bytes() == b"opaque synthetic source stays untouched"
    assert mapping.read_bytes() == mapping_before
    assert not (tmp_path / "preservations").exists()
    if replacement == "restore-original":
        assert not local.is_symlink()
        assert (local.stat().st_dev, local.stat().st_ino) == original_identity
    else:
        assert local.is_symlink() and os.readlink(local) == str(outside)
        assert (held.stat().st_dev, held.stat().st_ino) == original_identity
