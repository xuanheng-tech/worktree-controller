from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_controlled import _cli, acquire_one, adopt_canonical
from tests.test_retention_admission import POLICY_V5
from tests.test_shadow import POLICY, GitRepository, git
from worktree_controller import disposable, environment, lifecycle
from worktree_controller.controller import doctor
from worktree_controller.disposable import (
    RESOURCE_TYPES,
    _default_output_config,
    classify_resources,
)
from worktree_controller.environment import (
    CACHEDIR_SIGNATURE,
    RUNTIME_AUTHORITY_CONTRACT,
    runtime_authority_evidence,
    runtime_references,
)
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import cleanup_typed_disposable, handoff, park, release
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore


def empty_runtime_evidence():
    return {
        "contract": RUNTIME_AUTHORITY_CONTRACT,
        "observation_scope": "same-uid-and-managed-processes",
        "managed_references": [],
        "observed_references": [],
        "managed_visibility_uncertainties": [],
        "host_visibility_gaps": [],
        "scan_uncertainties": [],
    }


@pytest.fixture
def target(monkeypatch, request):
    # Unit cleanup tests must not depend on caller procfs visibility or real environments.
    monkeypatch.setattr(lifecycle, "runtime_references", lambda path, **kwargs: ())
    monkeypatch.setattr(
        lifecycle, "runtime_authority_evidence", lambda path, **kwargs: empty_runtime_evidence()
    )
    monkeypatch.setattr(lifecycle, "editable_references", lambda *args, **kwargs: ())
    retained = getattr(request, "param", None) in {"retained-feature", "retained-audit"}
    with GitRepository(linked=0, policy_text=POLICY_V5 if retained else POLICY) as repository:
        root = repository.repo
        (root / "backend").mkdir()
        (root / "frontend").mkdir()
        (root / "backend/pyproject.toml").write_text('[project]\nname="fixture"\nversion="1.0"\n')
        (root / "backend/uv.lock").write_text("version = 1\n")
        package = {
            "name": "fixture",
            "scripts": {"build": "vite build"},
            "devDependencies": {"vite": "1.0.0"},
        }
        entry = {"version": "1.0.0"}
        (root / "frontend/package.json").write_text(json.dumps(package))
        (root / "frontend/package-lock.json").write_text(
            json.dumps(
                {
                    "name": "fixture",
                    "lockfileVersion": 3,
                    "packages": {
                        "": {"name": "fixture", "devDependencies": {"vite": "1.0.0"}},
                        "node_modules/vite": entry,
                    },
                }
            )
        )
        (root / "frontend/index.html").write_text("<html></html>")
        (root / ".gitignore").write_text(
            "backend/.venv/\nfrontend/node_modules/\nfrontend/dist/\nunknown/\n.venv/\n.pytest_cache/\n"
        )
        git(root, "add", ".")
        git(root, "commit", "-m", "synthetic project identity")
        adopt_canonical(repository)
        role = "audit" if getattr(request, "param", None) == "retained-audit" else "feature"
        path, acquired = acquire_one(repository, role=role if retained else "hotfix")
        if retained:
            (path / "unfinished.txt").write_text("unfinished task result\n")
            git(path, "add", "unfinished.txt")
            git(path, "commit", "-m", "synthetic unfinished task result")
            park(repository.repo, target=path, expected_record_version=1, owner="module-owner")
        else:
            head = git(path, "rev-parse", "HEAD")
            handoff(
                repository.repo,
                target=path,
                expected_record_version=1,
                kind="integration-ready",
                commit=head,
                finalizer_commit=head,
                finalizer_record_id="synthetic-fixture",
            )
        venv = path / "backend/.venv"
        (venv / "bin").mkdir(parents=True)
        version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        (venv / "pyvenv.cfg").write_text(
            f"home = {Path(sys.executable).resolve().parent}\nversion = {version}\ninclude-system-site-packages = false\n"
        )
        (venv / "CACHEDIR.TAG").write_bytes(CACHEDIR_SIGNATURE + b"\n")
        (venv / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages").mkdir(
            parents=True
        )
        shutil.copy2(Path(sys.executable).resolve(), venv / "bin/python")
        modules = path / "frontend/node_modules"
        (modules / "vite").mkdir(parents=True)
        (modules / "vite/package.json").write_text(json.dumps({"name": "vite", "version": "1.0.0"}))
        (modules / ".package-lock.json").write_text(
            json.dumps({"name": "fixture", "packages": {"node_modules/vite": entry}})
        )
        (modules / ".bin").mkdir()
        (modules / ".bin/vite").symlink_to("../vite/package.json")
        (path / "frontend/dist/assets").mkdir(parents=True)
        (path / "frontend/dist/index.html").write_text("<html></html>")
        (path / "frontend/dist/assets/index.js").write_text("/* synthetic build */")
        yield repository, path, acquired["decision"]["allocation_id"]


def dry_run(repository, path, **kwargs):
    return cleanup_typed_disposable(
        repository.repo,
        target=path,
        expected_record_version=2,
        owner="module-owner",
        dry_run=True,
        **kwargs,
    )["decision"]


def clean(repository, path, plan_id):
    return cleanup_typed_disposable(
        repository.repo,
        target=path,
        expected_record_version=2,
        owner="module-owner",
        dry_run=False,
        expected_plan_id=plan_id,
    )


def rows(path):
    return {row["path"]: row for row in classify_resources(path)}


def fingerprint(path):
    return {
        p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in path.rglob("*")
        if p.is_file()
    }


def test_valid_resources_and_two_phase_release_leave_historical_evidence_unchanged(target):
    repository, path, allocation = target
    store = MetadataStore(inventory(repository.repo).common_dir)
    before_record = store.load().records[allocation]
    before = fingerprint(store.root)
    decision = dry_run(repository, path)
    assert decision["code"] == "RESOURCE_CLEANUP_PLAN_READY", decision["plan"]["blockers"]
    assert decision["plan"]["cleanup_paths"] == sorted(RESOURCE_TYPES)
    assert all(row["classification"] == "disposable" for row in decision["plan"]["resources"])
    assert before == fingerprint(store.root)
    with pytest.raises(ControllerError, match="unclassified ignored"):
        release(
            repository.repo,
            target=path,
            expected_record_version=2,
            owner_release="module-owner",
            dry_run=True,
        )
    result = clean(repository, path, decision["plan"]["plan_id"])
    assert result["decision"]["code"] == "TYPED_DISPOSABLE_CLEANED"
    assert result["decision"]["allocation_released"] is False
    assert path.exists()
    assert store.load().records[allocation] == before_record
    assert not any((path / relative).exists() for relative in RESOURCE_TYPES)
    for relative, digest in before.items():
        assert hashlib.sha256((store.root / relative).read_bytes()).hexdigest() == digest
    release(repository.repo, target=path, expected_record_version=2, owner_release="module-owner")
    assert not path.exists()
    assert store.load().records[allocation].lifecycle == "RELEASED"
    assert allocation not in store.load().bindings.values()
    assert git(repository.repo, "rev-parse", "feature/new-task") == before_record.handoff_commit


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("missing-identity", "RESOURCE_IDENTITY_MISMATCH"),
        ("extra-venv-data", "RESOURCE_IDENTITY_MISMATCH"),
        ("symlink-escape", "RESOURCE_PATH_ESCAPE"),
        ("symlink-root", "RESOURCE_PATH_ESCAPE"),
        ("tracked", "TRACKED_RESOURCE"),
        ("staged-delete", "TRACKED_RESOURCE"),
        ("unknown", "UNKNOWN_IGNORED_RESOURCE"),
        ("root-venv", "UNKNOWN_IGNORED_RESOURCE"),
        ("wrong-project", "RESOURCE_IDENTITY_MISMATCH"),
        ("wrong-build-output", "RESOURCE_IDENTITY_MISMATCH"),
    ],
)
def test_negative_resource_identity_is_fail_closed(target, mutation, expected):
    repository, path, _ = target
    relative = "backend/.venv"
    if mutation == "missing-identity":
        (path / relative / "pyvenv.cfg").unlink()
    elif mutation == "extra-venv-data":
        (path / relative / "data").mkdir()
    elif mutation == "symlink-escape":
        (path / relative / "bin/outside").symlink_to(repository.root)
    elif mutation == "symlink-root":
        relative = "frontend/dist"
        shutil.rmtree(path / relative)
        (path / relative).symlink_to(repository.root, target_is_directory=True)
    elif mutation in {"tracked", "staged-delete"}:
        f = path / relative / "important.txt"
        f.write_text("synthetic protected content")
        git(path, "add", "-f", f.relative_to(path).as_posix())
        if mutation == "staged-delete":
            git(path, "commit", "-m", "synthetic protected file")
            git(path, "rm", f.relative_to(path).as_posix())
    elif mutation in {"unknown", "root-venv"}:
        relative = "unknown" if mutation == "unknown" else ".venv"
        (path / relative).mkdir()
        (path / relative / "data").write_text("preserve")
    elif mutation == "wrong-project":
        relative = "frontend/node_modules"
        (path / relative / ".package-lock.json").write_text("{}")
    elif mutation == "wrong-build-output":
        relative = "frontend/dist"
        (path / "frontend/vite.config.ts").write_text(
            "export default {build: {outDir: 'elsewhere'}};"
        )
        git(path, "add", "frontend/vite.config.ts")
        git(path, "commit", "-m", "synthetic output override")
    assert rows(path)[relative]["blocker"] == expected
    result = dry_run(repository, path)
    assert result["code"] == "RESOURCE_CLEANUP_BLOCKED"
    assert result["plan"]["cleanup_paths"] == []
    assert (
        clean(repository, path, result["plan"]["plan_id"])["decision"]["code"]
        == "RESOURCE_CLEANUP_BLOCKED"
    )
    assert path.exists()


def external_interpreter(repository, path):
    venv = path / "backend/.venv"
    binary = repository.root / f"python{sys.version_info.major}.{sys.version_info.minor}"
    shutil.copy2(Path(sys.executable).resolve(), binary)
    (venv / "bin/python").unlink()
    (venv / "bin/python").symlink_to(binary)
    (venv / "bin/python3").symlink_to("python")
    (venv / f"bin/{binary.name}").symlink_to("python")
    cfg = venv / "pyvenv.cfg"
    cfg.write_text(
        cfg.read_text().replace(str(Path(sys.executable).resolve().parent), str(binary.parent))
    )
    return binary


def test_only_standard_interpreter_links_are_disposable_and_target_is_unchanged(target):
    repository, path, _ = target
    binary = external_interpreter(repository, path)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    before_stat = binary.stat()
    decision = dry_run(repository, path)
    assert decision["code"] == "RESOURCE_CLEANUP_PLAN_READY"
    clean(repository, path, decision["plan"]["plan_id"])
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == digest
    after_stat = binary.stat()
    assert (after_stat.st_ino, after_stat.st_mtime_ns, after_stat.st_size) == (
        before_stat.st_ino,
        before_stat.st_mtime_ns,
        before_stat.st_size,
    )


@pytest.mark.parametrize("kind", ["cwd", "exe", "fd", "mmap"])
def test_live_process_references_are_detected_and_block_cleanup(target, monkeypatch, kind):
    repository, path, _ = target
    resource = path / "backend/.venv/bin/python"
    payload = path / "frontend/dist/assets/index.js"
    code = "import time; time.sleep(60)"
    if kind == "fd":
        code = f"import time; f=open({str(payload)!r}); print('ready', flush=True); time.sleep(60)"
    elif kind == "mmap":
        code = f"import mmap,time; f=open({str(payload)!r}); m=mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ); f.close(); print('ready',flush=True); time.sleep(60)"
    else:
        code = "print('ready',flush=True); " + code
    executable = str(resource) if kind == "exe" else sys.executable
    child = subprocess.Popen(
        [executable, "-c", code],
        cwd=path if kind == "cwd" else repository.root,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"

        class Processes:
            def iterdir(self):
                return iter([Path(f"/proc/{child.pid}")])

        monkeypatch.setattr(
            environment, "Path", lambda value: Processes() if value == "/proc" else Path(value)
        )
        monkeypatch.setattr(lifecycle, "runtime_references", runtime_references)
        monkeypatch.setattr(lifecycle, "runtime_authority_evidence", runtime_authority_evidence)
        found = runtime_references(path, strict=True)
        assert any(row["kind"] == kind for row in found)
        decision = dry_run(repository, path)
        assert decision["code"] == "RESOURCE_CLEANUP_BLOCKED"
        assert decision["plan"]["blockers"][0]["code"] == "RUNTIME_REFERENCE"
        assert decision["plan"]["cleanup_paths"] == []
    finally:
        child.terminate()
        child.wait(timeout=5)


def host_gap_runtime(monkeypatch):
    real_readlink = os.readlink

    class Processes:
        def iterdir(self):
            return iter([Path(f"/proc/{os.getpid()}")])

    monkeypatch.setattr(
        environment, "Path", lambda value: Processes() if value == "/proc" else Path(value)
    )

    def unreadable(p, *args, **kwargs):
        if str(p).startswith("/proc/") or (p in {"cwd", "exe"} and "dir_fd" in kwargs):
            raise PermissionError(13, "synthetic non-dumpable process")
        return real_readlink(p, *args, **kwargs)

    monkeypatch.setattr(environment.os, "readlink", unreadable)
    monkeypatch.setattr(lifecycle, "runtime_authority_evidence", runtime_authority_evidence)


def test_unassociated_procfs_gap_is_evidence_not_a_typed_blocker(target, monkeypatch):
    repository, path, _ = target
    host_gap_runtime(monkeypatch)
    decision = dry_run(repository, path)
    assert decision["code"] == "CLEANUP_READY_WITH_HOST_VISIBILITY_GAP"
    assert decision["plan"]["blockers"] == []
    assert set(decision["plan"]["cleanup_paths"]) == set(RESOURCE_TYPES)
    gap = decision["plan"]["runtime_evidence"]["host_visibility_gaps"][0]
    assert gap["classification"] == "HOST_VISIBILITY_GAP"
    assert gap["pid"] == os.getpid()
    assert decision["plan"]["warnings"][0]["code"] == "HOST_VISIBILITY_GAP"


def test_host_gap_is_preserved_in_immutable_cleanup_evidence(target, monkeypatch):
    repository, path, _ = target
    store = MetadataStore(inventory(repository.repo).common_dir)
    before = {p: p.read_bytes() for p in store.root.rglob("*receipt*/*.json")}
    host_gap_runtime(monkeypatch)
    decision = dry_run(repository, path)
    result = clean(repository, path, decision["plan"]["plan_id"])
    assert result["decision"]["code"] == "TYPED_DISPOSABLE_CLEANED"
    receipt_path = store.completion_receipts_dir / f"{result['decision']['receipt_id']}.json"
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    assert receipt["plan"]["warnings"][0]["code"] == "HOST_VISIBILITY_GAP"
    assert receipt["post_scan"]["runtime_evidence"]["host_visibility_gaps"]
    assert len(receipt["runtime_checks"]) == 3
    assert all(row["runtime_evidence"]["host_visibility_gaps"] for row in receipt["runtime_checks"])
    assert path.exists()  # Cleanup did not release the allocation.
    release(
        repository.repo,
        target=path,
        expected_record_version=2,
        owner_release="module-owner",
        dry_run=True,
    )
    assert receipt_path.read_bytes() == receipt_bytes
    assert all(p.read_bytes() == value for p, value in before.items())


def test_inaccessible_host_gap_cannot_hide_explicit_runtime_binding(target, monkeypatch):
    repository, path, allocation = target
    store = MetadataStore(inventory(repository.repo).common_dir)
    record = store.load().records[allocation]
    store.replace_record(
        replace(record, external_bindings=("agent-workspace-native:fixture-session",))
    )
    host_gap_runtime(monkeypatch)
    decision = dry_run(repository, path)
    assert decision["code"] == "RESOURCE_CLEANUP_BLOCKED"
    assert decision["plan"]["cleanup_paths"] == []
    assert (
        decision["plan"]["runtime_evidence"]["managed_references"][0]["kind"] == "external_binding"
    )
    assert decision["plan"]["warnings"]


def test_formal_writer_holder_permission_gap_is_managed_and_blocks(target, monkeypatch):
    from worktree_controller.writer_lease import describe_holder, new_lease

    repository, path, allocation = target
    store = MetadataStore(inventory(repository.repo).common_dir)
    snapshot = store.load()
    store.write_writer_lease(
        new_lease(
            repository_id=snapshot.repository_id,
            allocation_id=allocation,
            worktree_path=str(path),
            owner=snapshot.records[allocation].owner,
            holder=describe_holder(
                os.getpid(), client_id="manual", session_id="fixture-session", run_id="fixture-run"
            ),
            ttl_seconds=None,
        )
    )
    host_gap_runtime(monkeypatch)
    decision = dry_run(repository, path)
    assert decision["code"] == "RESOURCE_CLEANUP_BLOCKED"
    evidence = decision["plan"]["runtime_evidence"]
    assert evidence["host_visibility_gaps"] == []
    assert evidence["managed_visibility_uncertainties"][0]["pid"] == os.getpid()
    assert {row["code"] for row in decision["plan"]["blockers"]} >= {
        "MANAGED_REFERENCE",
        "RUNTIME_SCAN_UNCERTAIN",
    }
    assert decision["plan"]["cleanup_paths"] == []


def test_cli_host_gap_ready_has_success_exit_and_warning(target, monkeypatch, capsys):
    from worktree_controller.cli import main

    repository, path, _ = target
    host_gap_runtime(monkeypatch)
    args = [
        "resource-clean",
        "--repo",
        str(repository.repo),
        "--path",
        str(path),
        "--owner",
        "module-owner",
        "--expected-record-version",
        "2",
        "--typed",
        "--dry-run",
    ]
    assert main([*args, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"]["code"] == "CLEANUP_READY_WITH_HOST_VISIBILITY_GAP"
    assert payload["decision"]["plan"]["warnings"][0]["code"] == "HOST_VISIBILITY_GAP"
    assert main(args) == 0
    assert "Warning: HOST_VISIBILITY_GAP" in capsys.readouterr().out


def test_unknown_ignored_resource_stays_blocked_with_only_host_gap(target, monkeypatch):
    repository, path, _ = target
    (path / "unknown").mkdir()
    (path / "unknown/state").write_text("preserve")
    host_gap_runtime(monkeypatch)
    decision = dry_run(repository, path)
    assert decision["code"] == "RESOURCE_CLEANUP_BLOCKED"
    assert decision["plan"]["cleanup_paths"] == []
    assert decision["plan"]["warnings"][0]["code"] == "HOST_VISIBILITY_GAP"
    assert any(row.get("path") == "unknown" for row in decision["plan"]["blockers"])


def test_dry_run_identity_drift_and_missing_plan_are_blocked(target):
    repository, path, _ = target
    first = dry_run(repository, path)
    with pytest.raises(ControllerError) as exc:
        clean(repository, path, None)
    assert exc.value.code == "RESOURCE_PLAN_MISMATCH"
    (path / "frontend/dist/assets/index.js").write_text("changed")
    with pytest.raises(ControllerError) as exc:
        clean(repository, path, first["plan"]["plan_id"])
    assert exc.value.code == "RESOURCE_PLAN_MISMATCH"
    assert all((path / relative).exists() for relative in RESOURCE_TYPES)


def test_release_after_cleanup_keeps_managed_runtime_scan(target, monkeypatch):
    repository, path, _ = target
    decision = dry_run(repository, path)
    clean(repository, path, decision["plan"]["plan_id"])

    def uncertain(path, *, holders=()):
        raise ControllerError("RUNTIME_SCAN_UNCERTAIN", "synthetic managed visibility change")

    monkeypatch.setattr(lifecycle, "runtime_authority_evidence", uncertain)
    with pytest.raises(ControllerError) as exc:
        release(
            repository.repo, target=path, expected_record_version=2, owner_release="module-owner"
        )
    assert exc.value.code == "RUNTIME_SCAN_UNCERTAIN"
    assert path.exists()


def test_cleanup_failure_retains_journal_and_blocks_release(target, monkeypatch):
    repository, path, _ = target
    decision = dry_run(repository, path)
    original = lifecycle.delete_typed_resource

    def fail(root, relative, evidence):
        original(root, relative, evidence)
        raise OSError("synthetic failure after deleting first resource")

    with monkeypatch.context() as patch:
        patch.setattr(lifecycle, "delete_typed_resource", fail)
        with pytest.raises(ControllerError) as exc:
            clean(repository, path, decision["plan"]["plan_id"])
    assert exc.value.code == "RESOURCE_CLEANUP_FAILED"
    with pytest.raises(ControllerError) as exc:
        release(
            repository.repo,
            target=path,
            expected_record_version=2,
            owner_release="module-owner",
            dry_run=True,
        )
    assert exc.value.code == "INTERRUPTED_TRANSACTION"
    diagnostics = doctor(repository.repo)
    assert diagnostics["decision"]["code"] == "FAIL"
    assert not any(error["code"] == "INVALID_TRANSACTION" for error in diagnostics["errors"])
    assert any(w["code"] == "INTERRUPTED_TRANSACTION" for w in diagnostics["warnings"])


@pytest.mark.parametrize("relative", sorted(RESOURCE_TYPES))
def test_cleanup_never_follows_replaced_parent(target, monkeypatch, relative):
    # Preserve the external-sentinel regression: a final BLOCK alone is insufficient.
    repository, path, _ = target
    parent, leaf = relative.split("/")
    outside = repository.root / "outside-owned-fixture"
    (outside / leaf).mkdir(parents=True)
    sentinel = outside / leaf / "must-survive.txt"
    sentinel.write_bytes(b"isolated external sentinel")
    expected = fingerprint(outside)
    decision = dry_run(repository, path)
    assert decision["plan"]["blockers"] == []
    original = lifecycle.resource_evidence
    raced = False

    def swap_parent(root, resource, *, build_declaration=None):
        nonlocal raced
        evidence = original(root, resource, build_declaration=build_declaration)
        if resource == relative and not raced:
            raced = True
            (path / parent).rename(path / f"{parent}-before-race")
            (path / parent).symlink_to(outside, target_is_directory=True)
        return evidence

    with monkeypatch.context() as patch:
        patch.setattr(lifecycle, "resource_evidence", swap_parent)
        with pytest.raises(ControllerError):
            clean(repository, path, decision["plan"]["plan_id"])
    assert raced
    assert sentinel.is_file(), "cleanup deleted an external target before reporting BLOCK"
    assert fingerprint(outside) == expected


@pytest.mark.parametrize("relative", sorted(RESOURCE_TYPES))
@pytest.mark.parametrize("mutation", ["leaf-symlink", "parent-inode", "leaf-inode", "missing"])
def test_cleanup_rejects_changed_path_identity(target, monkeypatch, relative, mutation):
    repository, path, _ = target
    outside = repository.root / "external"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"preserve external target")
    before = fingerprint(outside)
    decision = dry_run(repository, path)
    original = lifecycle.resource_evidence
    replacement = None

    def replace_after_check(root, resource, *, build_declaration=None):
        nonlocal replacement
        evidence = original(root, resource, build_declaration=build_declaration)
        if resource == relative:
            changed = path / relative
            if mutation == "parent-inode":
                changed = changed.parent
            saved = changed.with_name(changed.name + "-preserved")
            changed.rename(saved)
            if mutation == "leaf-symlink":
                changed.symlink_to(outside, target_is_directory=True)
            elif mutation != "missing":
                changed.mkdir()
                (changed / "unclassified-data").write_bytes(b"preserve replacement inode")
                replacement = changed / "unclassified-data"
        return evidence

    with monkeypatch.context() as patch:
        patch.setattr(lifecycle, "resource_evidence", replace_after_check)
        with pytest.raises(ControllerError) as exc:
            clean(repository, path, decision["plan"]["plan_id"])
    assert exc.value.code == "RESOURCE_PLAN_MISMATCH"
    assert fingerprint(outside) == before
    if replacement is not None:
        assert replacement.read_bytes() == b"preserve replacement inode"
    with pytest.raises(ControllerError, match="transaction"):
        release(
            repository.repo, target=path, expected_record_version=2, owner_release="module-owner"
        )


@pytest.mark.parametrize("mutation", ["symlink", "inode", "missing"])
def test_recursive_directory_change_does_not_touch_external_target(target, monkeypatch, mutation):
    repository, path, _ = target
    relative = "frontend/dist"
    resource = path / relative
    outside = repository.root / "external-assets"
    outside.mkdir()
    (outside / "index.js").write_bytes(b"external build sentinel")
    before = fingerprint(outside)
    evidence = rows(path)[relative]["evidence"]
    original = disposable._fd_inventory

    def replace_after_inventory(fd):
        result = original(fd)
        (resource / "assets").rename(repository.root / "assets-preserved")
        if mutation == "symlink":
            (resource / "assets").symlink_to(outside, target_is_directory=True)
        elif mutation == "inode":
            (resource / "assets").mkdir()
            (resource / "assets/unclassified-data").write_bytes(b"preserve")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(disposable, "_fd_inventory", replace_after_inventory)
        with pytest.raises(ControllerError) as exc:
            disposable.delete_typed_resource(path, relative, evidence)
    assert exc.value.code == "RESOURCE_PLAN_MISMATCH"
    assert fingerprint(outside) == before
    assert (repository.root / "assets-preserved/index.js").exists()
    if mutation == "inode":
        assert (resource / "assets/unclassified-data").read_bytes() == b"preserve"


@pytest.mark.parametrize("entry", ["index.js", "assets"])
def test_directory_entry_swap_at_destructive_syscall_never_follows_symlink(
    target, monkeypatch, entry
):
    repository, path, _ = target
    relative = "frontend/dist"
    resource = path / relative
    outside = repository.root / "external-target"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"preserve")
    before = fingerprint(outside)
    evidence = rows(path)[relative]["evidence"]
    function = "unlink" if entry == "index.js" else "rmdir"
    original = getattr(disposable.os, function)
    raced = False

    def swap(name, *, dir_fd=None):
        nonlocal raced
        assert dir_fd is not None
        assert "/" not in name
        if name == entry and not raced:
            raced = True
            os.rename(name, name + "-preserved", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.symlink(outside if entry == "assets" else outside / "sentinel", name, dir_fd=dir_fd)
        return original(name, dir_fd=dir_fd)

    with monkeypatch.context() as patch:
        patch.setattr(disposable.os, function, swap)
        with pytest.raises(ControllerError):
            disposable.delete_typed_resource(path, relative, evidence)
    assert raced
    assert fingerprint(outside) == before
    assert resource.exists()


def test_partial_fd_cleanup_failure_retains_evidence_and_external_interpreter(target, monkeypatch):
    repository, path, _ = target
    binary = external_interpreter(repository, path)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    identity = binary.stat()
    decision = dry_run(repository, path)
    original = disposable.os.unlink
    removed = []

    def fail_after_unlink(name, *, dir_fd=None):
        assert dir_fd is not None and "/" not in name
        original(name, dir_fd=dir_fd)
        removed.append(name)
        if name == "python":
            raise OSError("synthetic partial FD cleanup failure")

    with monkeypatch.context() as patch:
        patch.setattr(disposable.os, "unlink", fail_after_unlink)
        with pytest.raises(ControllerError) as exc:
            clean(repository, path, decision["plan"]["plan_id"])
    assert exc.value.code == "RESOURCE_CLEANUP_FAILED"
    assert "python" in removed
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == digest
    after = binary.stat()
    assert (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size) == (
        identity.st_dev,
        identity.st_ino,
        identity.st_mtime_ns,
        identity.st_size,
    )
    with pytest.raises(ControllerError, match="transaction"):
        release(
            repository.repo, target=path, expected_record_version=2, owner_release="module-owner"
        )


def test_post_cleanup_uncertainty_blocks_release_even_when_resources_are_gone(target, monkeypatch):
    repository, path, _ = target
    decision = dry_run(repository, path)

    def runtime(path, **kwargs):
        if all(not (path / relative).exists() for relative in RESOURCE_TYPES):
            raise ControllerError("RUNTIME_SCAN_UNCERTAIN", "synthetic post-cleanup uncertainty")
        return empty_runtime_evidence()

    monkeypatch.setattr(lifecycle, "runtime_authority_evidence", runtime)
    with pytest.raises(ControllerError) as exc:
        clean(repository, path, decision["plan"]["plan_id"])
    assert exc.value.code == "RESOURCE_CLEANUP_FAILED"
    assert all(not (path / relative).exists() for relative in RESOURCE_TYPES)
    with pytest.raises(ControllerError) as exc:
        release(
            repository.repo, target=path, expected_record_version=2, owner_release="module-owner"
        )
    assert exc.value.code == "INTERRUPTED_TRANSACTION"
    assert path.exists()


@pytest.mark.parametrize("condition", ["owner", "record-cas", "head", "binding", "stale", "writer"])
def test_existing_allocation_guards_cannot_be_bypassed(target, condition):
    repository, path, allocation = target
    store = MetadataStore(inventory(repository.repo).common_dir)
    record = store.load().records[allocation]
    if condition in {"owner", "record-cas", "binding", "stale"}:
        changes = {
            "owner": {"owner": "someone-else"},
            "record-cas": {"record_version": 3},
            "binding": {"external_bindings": ("synthetic-live-session",)},
            "stale": {"lifecycle": "STALE_SUSPECT"},
        }[condition]
        store.replace_record(replace(record, **changes))
    elif condition == "head":
        git(path, "commit", "--allow-empty", "-m", "synthetic drift")
    else:
        from worktree_controller.writer_lease import describe_holder, new_lease

        holder = describe_holder(os.getpid(), client_id="manual")
        store.write_writer_lease(
            new_lease(
                repository_id=store.load().repository_id,
                allocation_id=allocation,
                worktree_path=str(path),
                owner=record.owner,
                holder=holder,
                ttl_seconds=None,
            )
        )
    if condition == "stale":
        with pytest.raises(ControllerError) as exc:
            dry_run(repository, path)
        assert exc.value.code == "INVALID_METADATA"
        return
    decision = dry_run(repository, path)
    assert decision["code"] == "RESOURCE_CLEANUP_BLOCKED"
    assert decision["plan"]["cleanup_paths"] == []


def test_cli_dry_run_exposes_typed_fields_and_blocked_exit_status(target):
    repository, path, _ = target
    (path / "unknown").mkdir()
    (path / "unknown/state").write_text("preserve")
    result = _cli(
        [
            "resource-clean",
            "--repo",
            str(repository.repo),
            "--path",
            str(path),
            "--owner",
            "module-owner",
            "--expected-record-version",
            "2",
            "--typed",
            "--dry-run",
            "--json",
        ]
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["decision"]["code"] == "RESOURCE_CLEANUP_BLOCKED"
    assert all(
        {"resource_type", "path", "classification", "cleanup_action", "blocker"} <= row.keys()
        for row in payload["decision"]["plan"]["resources"]
    )


def test_cli_status_preserves_null_decision_and_success_exit(target):
    repository, _, _ = target
    result = _cli(["status", "--repo", str(repository.repo), "--json"])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"] is None


@pytest.mark.parametrize(
    "config,expected",
    [
        ("export default defineConfig({});", True),
        ("export default defineConfig({server: {host: '127.0.0.1'}});", True),
        (
            "export default defineConfig(({mode}) => { const x = 1; return {server: {port: 1234}}; });",
            True,
        ),
        ("export default getConfig();", False),
        ("export default defineConfig(JSON.parse(value));", False),
        ("export default defineConfig({server: {}, plugins: []});", False),
        ("export default defineConfig(() => { return settings; });", False),
    ],
)
def test_ambiguous_build_configuration_is_not_assumed_to_use_dist(config, expected):
    assert _default_output_config(config) is expected


@pytest.mark.parametrize("target", ["retained-feature", "retained-audit"], indirect=True)
def test_retained_typed_cleanup_preserves_unfinished_authority_and_refs(target):
    repository, path, allocation = target
    store = MetadataStore(inventory(repository.repo).common_dir)
    record = store.load().records[allocation]
    assert record.lifecycle == "RETAINED"
    assert record.handoff_commit is None and record.finalizer_record_id is None
    head = git(path, "rev-parse", "HEAD")
    refs = git(repository.repo, "for-each-ref", "--format=%(refname) %(objectname)")
    index = Path(git(path, "rev-parse", "--git-path", "index"))
    index_sha = hashlib.sha256(index.read_bytes()).hexdigest()
    history = fingerprint(store.root)
    source = (path / "unfinished.txt").read_bytes()

    plan = dry_run(repository, path)
    assert plan["code"] == "RESOURCE_CLEANUP_PLAN_READY", plan["plan"]["blockers"]
    assert plan["plan"]["cleanup_paths"] == sorted(RESOURCE_TYPES)
    assert fingerprint(store.root) == history
    result = clean(repository, path, plan["plan"]["plan_id"])["decision"]
    assert result["code"] == "TYPED_DISPOSABLE_CLEANED"
    assert result["allocation_released"] is False
    receipt = json.loads(
        (store.completion_receipts_dir / f"{result['receipt_id']}.json").read_text()
    )
    assert receipt["result"] == "CLEANED"
    assert receipt["post_scan"]["cleanup_paths"] == []
    assert all(action["status"] == "COMPLETED" for action in receipt["actions"])
    assert not any((path / relative).exists() for relative in RESOURCE_TYPES)
    assert store.load().records[allocation] == record
    assert (path / "unfinished.txt").read_bytes() == source
    assert git(path, "rev-parse", "HEAD") == head
    assert git(repository.repo, "for-each-ref", "--format=%(refname) %(objectname)") == refs
    assert hashlib.sha256(index.read_bytes()).hexdigest() == index_sha
    for relative, digest in history.items():
        assert hashlib.sha256((store.root / relative).read_bytes()).hexdigest() == digest
    with pytest.raises(ControllerError) as blocked:
        release(
            repository.repo,
            target=path,
            expected_record_version=2,
            owner_release="module-owner",
            dry_run=True,
        )
    assert blocked.value.code == "INVALID_LIFECYCLE"


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
@pytest.mark.parametrize(
    "condition",
    [
        "owner",
        "record-cas",
        "binding",
        "writer",
        "unknown",
        "dirty",
        "frozen",
        "runtime",
        "uncertain",
        "preservation",
    ],
)
def test_retained_cleanup_keeps_existing_safety_guards(target, monkeypatch, condition):
    repository, path, allocation = target
    store = MetadataStore(inventory(repository.repo).common_dir)
    record = store.load().records[allocation]
    kwargs = dict(target=path, expected_record_version=2, owner="module-owner", dry_run=True)
    if condition == "owner":
        kwargs["owner"] = "other-owner"
    elif condition == "record-cas":
        kwargs["expected_record_version"] = 1
    elif condition == "binding":
        store.replace_record(replace(record, external_bindings=("synthetic-live-session",)))
    elif condition == "writer":
        from worktree_controller.writer_lease import describe_holder, new_lease

        store.write_writer_lease(
            new_lease(
                repository_id=store.load().repository_id,
                allocation_id=allocation,
                worktree_path=str(path),
                owner=record.owner,
                holder=describe_holder(os.getpid(), client_id="manual"),
                ttl_seconds=None,
            )
        )
    elif condition == "unknown":
        (path / "unknown").mkdir()
        (path / "unknown/state").write_text("preserve unknown fixture")
    elif condition == "dirty":
        (path / "unfinished.txt").write_text("uncommitted task result")
    elif condition == "frozen":
        store.replace_record(replace(record, handoff_commit=git(path, "rev-parse", "HEAD")))
    elif condition in {"runtime", "uncertain"}:
        evidence = empty_runtime_evidence()
        key = (
            "observed_references" if condition == "runtime" else "managed_visibility_uncertainties"
        )
        evidence[key].append({"kind": "cwd", "pid": 12345})
        monkeypatch.setattr(lifecycle, "runtime_authority_evidence", lambda *a, **kw: evidence)
    else:
        kwargs["preservation_receipt"] = repository.root / "missing-receipt.json"
        kwargs["preservation_receipt_sha256"] = "0" * 64
    before = fingerprint(path)
    decision = cleanup_typed_disposable(repository.repo, **kwargs)["decision"]
    assert decision["code"] == "RESOURCE_CLEANUP_BLOCKED"
    assert decision["plan"]["cleanup_paths"] == []
    expected = {
        "owner": "ACTIVE_OWNER",
        "record-cas": "CAS_MISMATCH",
        "binding": "EXTERNAL_BINDING",
        "writer": "MANAGED_REFERENCE",
        "unknown": "UNKNOWN_IGNORED_RESOURCE",
        "dirty": "DIRTY_WORKTREE",
        "frozen": "HANDOFF_EVIDENCE_PRESENT",
        "runtime": "RUNTIME_REFERENCE",
        "uncertain": "RUNTIME_SCAN_UNCERTAIN",
        "preservation": "PRESERVATION_COVERAGE_INVALID",
    }[condition]
    assert expected in {blocker["code"] for blocker in decision["plan"]["blockers"]}
    assert not any(b["code"] == "INVALID_LIFECYCLE" for b in decision["plan"]["blockers"])
    assert all((path / relative).exists() for relative in RESOURCE_TYPES)
    assert fingerprint(path) == before


@pytest.mark.parametrize("target", ["retained-feature"], indirect=True)
def test_retained_cleanup_rechecks_plan_head_before_any_delete(target):
    repository, path, _ = target
    plan = dry_run(repository, path)
    assert plan["code"] == "RESOURCE_CLEANUP_PLAN_READY"
    git(path, "commit", "--allow-empty", "-m", "synthetic post-plan drift")
    before = fingerprint(path)
    with pytest.raises(ControllerError) as blocked:
        clean(repository, path, plan["plan"]["plan_id"])
    assert blocked.value.code == "RESOURCE_PLAN_MISMATCH"
    assert fingerprint(path) == before
    assert all((path / relative).exists() for relative in RESOURCE_TYPES)
