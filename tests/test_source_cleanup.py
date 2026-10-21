from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from tests import test_source_preservation as fixtures
from tests.test_shadow import git
from tests.test_source_preservation import copied
from tests.test_typed_disposable import empty_runtime_evidence
from worktree_controller import lifecycle
from worktree_controller import source_cleanup as c
from worktree_controller import source_preservation as s
from worktree_controller.controller import doctor
from worktree_controller.models import ControllerError

source = fixtures.source


def ready(source):
    plan, preserved = copied(source)
    cleanup = c.plan_cleanup(plan, preserved["receipt_sha256"], state_root=source[2])
    return cleanup, preserved


def execute(source, cleanup):
    return c.cleanup_source(cleanup, state_root=source[2])


def generation_bytes(path):
    return {
        str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in path.rglob("*")
        if p.is_file()
    }


def test_exact_cleanup_restores_text_binary_mode_without_index_or_evidence_changes(source):
    cleanup, preserved = ready(source)
    plan = cleanup["preservation_plan"]
    target = source[1]
    generation = Path(preserved["generation"])
    original = generation_bytes(generation)
    result = execute(source, cleanup)
    assert result["result"] == "SOURCE_CLEANUP_COMPLETE"
    assert result["post_scan"]["dirty_paths"] == []
    assert result["post_scan"]["index_sha256"] == plan["source_tree"]["index_sha256"]
    assert git(target, "status", "--porcelain") == ""
    for row in cleanup["restore"]:
        assert (target / row["path"]).read_bytes() == c._git(
            target, "cat-file", "blob", row["git_blob"]
        ).stdout
        assert (target / row["path"]).stat().st_mode & 0o777 == row["mode"]
    assert generation_bytes(generation) == original
    receipt = json.loads(Path(result["receipt"]["path"]).read_bytes())
    assert receipt["index_unchanged"] is True
    assert receipt["release_authorized"] is False
    assert len(receipt["actions"]) == 3
    assert all(action["status"] == "ACTION_COMPLETED" for action in receipt["actions"])
    assert list(c._store(plan).transactions_dir.glob("*.json")) == []
    assert (
        s._verify_generation(plan, preserved["receipt_sha256"], source[2])["verification"] == "PASS"
    )
    with pytest.raises(ControllerError):
        execute(source, cleanup)  # Completed dirty source cannot be blindly replayed.


@pytest.mark.parametrize(
    "drift", ["content", "mode", "path", "head", "index", "generation", "receipt"]
)
def test_preflight_drift_prevents_all_source_writes(source, drift):
    cleanup, preserved = ready(source)
    target = source[1]
    before = (target / "src/binary.bin").read_bytes()
    path = target / "src/plain.py"
    if drift == "content":
        path.write_bytes(b"later\n")
    elif drift == "mode":
        path.chmod(0o711)
    elif drift == "path":
        path.rename(path.with_name("moved.py"))
    elif drift == "head":
        git(target, "commit", "--allow-empty", "-m", "different HEAD")
    elif drift == "index":
        git(target, "add", "src/plain.py")
    elif drift == "generation":
        copied_file = Path(preserved["generation"]) / "tree/src/plain.py"
        copied_file.chmod(0o600)
        copied_file.write_bytes(b"changed")
        copied_file.chmod(0o400)
    else:
        receipt = Path(preserved["receipt"])
        receipt.chmod(0o600)
        receipt.write_bytes(receipt.read_bytes() + b"\n")
        receipt.chmod(0o400)
    with pytest.raises(ControllerError):
        execute(source, cleanup)
    assert (target / "src/binary.bin").read_bytes() == before
    assert not list(c._store(cleanup["preservation_plan"]).transactions_dir.glob("*.json"))


def test_hardlinked_dirty_source_blocks_without_touching_external_link(source):
    external = source[0].root / "external-link"
    os.link(source[1] / "src/plain.py", external)
    before = external.read_bytes()
    plan, preserved = copied(source)
    with pytest.raises(ControllerError) as error:
        c.plan_cleanup(plan, preserved["receipt_sha256"], state_root=source[2])
    assert error.value.code == "SOURCE_CLEANUP_HARDLINK_BLOCKED"
    assert external.read_bytes() == before


@pytest.mark.parametrize("when", ["before-closeout", "before-release"])
def test_completed_source_survives_index_stat_cache_refresh(source, monkeypatch, when):
    monkeypatch.setattr(lifecycle, "runtime_references", lambda *a, **k: ())
    monkeypatch.setattr(lifecycle, "editable_references", lambda *a, **k: ())
    cleanup, preserved = ready(source)
    result = execute(source, cleanup)
    original_receipt = Path(result["receipt"]["path"]).read_bytes()
    generation = Path(preserved["generation"])
    original_generation = generation_bytes(generation)
    repository, target = source[:2]
    if when == "before-release":
        lifecycle.close_preserved(repository.repo, target=target,
                                 expected_record_version=1, owner="module-owner")
    identity = cleanup["preservation_plan"]["authority"]["identity"]
    before = s._git_state(target, Path(identity["git_dir"]), identity["expected_head"], allow_clean=True)
    git(target, "update-index", "--refresh")
    after = s._git_state(target, Path(identity["git_dir"]), before["base_head"], allow_clean=True)
    assert after["base_entries"] == before["base_entries"]
    assert after["index_identity"] != before["index_identity"]
    if when == "before-closeout":
        closed = lifecycle.close_preserved(repository.repo, target=target,
                                          expected_record_version=1, owner="module-owner")
        assert closed["decision"]["task_completed"] is False
    released = lifecycle.release(repository.repo, target=target,
                                 expected_record_version=2, owner_release="module-owner")
    assert released["decision"]["code"] == "RELEASED"
    assert Path(result["receipt"]["path"]).read_bytes() == original_receipt
    assert generation_bytes(generation) == original_generation


@pytest.mark.parametrize("drift", ["staged-content", "index-flags", "during-verification"])
def test_completed_source_index_semantics_and_observation_stability_still_block(source, monkeypatch, drift):
    cleanup, _ = ready(source)
    execute(source, cleanup)
    repository, target = source[:2]
    original = (target / "src/plain.py").read_bytes()
    if drift == "staged-content":
        (target / "src/plain.py").write_bytes(b"other staged work\n")
        git(target, "add", "src/plain.py")
        (target / "src/plain.py").write_bytes(original)
    elif drift == "index-flags":
        git(target, "update-index", "--assume-unchanged", "src/plain.py")
    else:
        read = s._git_state
        triggered = False

        def refresh_during_read(*args, **kwargs):
            nonlocal triggered
            result = read(*args, **kwargs)
            if not triggered:
                triggered = True
                git(target, "update-index", "--refresh")
            return result

        monkeypatch.setattr(s, "_git_state", refresh_during_read)
    with pytest.raises(ControllerError):
        lifecycle.close_preserved(repository.repo, target=target,
                                  expected_record_version=1, owner="module-owner")
    assert target.exists()


@pytest.mark.parametrize("race", ["parent", "leaf", "inode", "disappears", "late-content"])
def test_final_fd_boundary_races_block_and_external_sentinel_is_never_written(
    source, monkeypatch, race
):
    cleanup, _ = ready(source)
    target = source[1]
    external = source[0].root / "external"
    external.mkdir()
    sentinel = external / "binary.bin"
    sentinel.write_bytes(b"external sentinel")
    metadata = sentinel.stat()
    restore = c._restore

    def raced(plan, row, data):
        original_open = os.open
        triggered = False

        def opening(path, flags, *a, **kw):
            nonlocal triggered
            if path == "binary.bin" and flags & os.O_PATH and not triggered:
                triggered = True
                if race == "parent":
                    (target / "src").rename(target / "saved-src")
                    (target / "src").symlink_to(external)
                elif race == "leaf":
                    (target / "src/binary.bin").unlink()
                    (target / "src/binary.bin").symlink_to(sentinel)
                elif race == "inode":
                    (target / "src/binary.bin").rename(target / "original.bin")
                    (target / "src/binary.bin").write_bytes(b"replacement")
                elif race == "late-content":
                    (target / "src/binary.bin").write_bytes(b"changed at final open")
                else:
                    (target / "src/binary.bin").unlink()
            return original_open(path, flags, *a, **kw)

        with monkeypatch.context() as patch:
            patch.setattr(os, "open", opening)
            return restore(plan, row, data)

    monkeypatch.setattr(c, "_restore", raced)
    result = execute(source, cleanup)
    assert result["result"] == "SOURCE_CLEANUP_BLOCKED"
    assert sentinel.read_bytes() == b"external sentinel"
    assert sentinel.stat().st_ino == metadata.st_ino
    assert sentinel.stat().st_mtime_ns == metadata.st_mtime_ns
    assert sentinel.stat().st_ctime_ns == metadata.st_ctime_ns
    assert list(c._store(cleanup["preservation_plan"]).transactions_dir.glob("*.json"))


def test_partial_failure_retains_transaction_and_immutable_receipt_and_cannot_replay(
    source, monkeypatch
):
    cleanup, preserved = ready(source)
    before = generation_bytes(Path(preserved["generation"]))
    restore = c._restore
    count = 0

    def fail_second(plan, row, data):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("synthetic I/O failure")
        return restore(plan, row, data)

    monkeypatch.setattr(c, "_restore", fail_second)
    result = execute(source, cleanup)
    receipt_path = Path(result["receipt"]["path"])
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    assert result["result"] == "SOURCE_CLEANUP_BLOCKED"
    assert receipt["result"] == "FAILED_OR_PARTIAL"
    assert receipt["actions"][0]["status"] == "ACTION_COMPLETED"
    assert receipt["actions"][1]["status"] == "ACTION_STARTED"
    with pytest.raises(ControllerError) as error:
        execute(source, cleanup)
    assert error.value.code == "INTERRUPTED_TRANSACTION"
    diagnostic = doctor(source[0].repo)
    assert any(e["code"] == "INTERRUPTED_TRANSACTION" for e in diagnostic["warnings"])
    assert not any(e["code"] == "INVALID_TRANSACTION" for e in diagnostic["errors"])
    assert count == 2
    assert receipt_path.read_bytes() == receipt_bytes
    assert generation_bytes(Path(preserved["generation"])) == before
    store = c._store(cleanup["preservation_plan"])
    with pytest.raises(ControllerError) as error, store.lock(exclusive=True):
        lifecycle._release_transaction(store, target_path=str(source[1]))
    assert error.value.code == "INTERRUPTED_TRANSACTION"


def test_generation_change_after_final_preflight_blocks_before_action(source, monkeypatch):
    cleanup, preserved = ready(source)
    original = c._state
    copied_file = Path(preserved["generation"]) / "tree/src/plain.py"
    before = (source[1] / "src/binary.bin").read_bytes()

    def changed(*args, **kwargs):
        copied_file.chmod(0o600)
        copied_file.write_bytes(b"changed")
        copied_file.chmod(0o400)
        return original(*args, **kwargs)

    monkeypatch.setattr(c, "_state", changed)
    result = execute(source, cleanup)
    assert result["result"] == "SOURCE_CLEANUP_BLOCKED"
    assert (source[1] / "src/binary.bin").read_bytes() == before


def test_runtime_appears_after_preservation_blocks_source_cleanup(source, monkeypatch):
    cleanup, _ = ready(source)
    runtime = empty_runtime_evidence()
    runtime["managed_references"] = [{"kind": "fixture", "pid": 99999999}]
    monkeypatch.setattr(lifecycle, "runtime_authority_evidence", lambda *a, **k: runtime)
    with pytest.raises(ControllerError):
        execute(source, cleanup)


def test_host_gap_is_saved_in_independent_cleanup_receipt(source, monkeypatch):
    runtime = empty_runtime_evidence()
    runtime["host_visibility_gaps"] = [{"pid": 99999999, "classification": "HOST_VISIBILITY_GAP"}]
    monkeypatch.setattr(lifecycle, "runtime_authority_evidence", lambda *a, **k: runtime)
    cleanup, _ = ready(source)
    result = execute(source, cleanup)
    assert result["result"] == "SOURCE_CLEANUP_COMPLETE"
    receipt = json.loads(Path(result["receipt"]["path"]).read_bytes())
    assert all(
        check["runtime_evidence"]["host_visibility_gaps"] == runtime["host_visibility_gaps"]
        for check in receipt["checks"]
    )


def test_preserved_close_can_release_without_task_completion_and_preservation_survives(source, monkeypatch):
    monkeypatch.setattr(lifecycle, "runtime_references", lambda *a, **k: ())
    monkeypatch.setattr(lifecycle, "editable_references", lambda *a, **k: ())
    (source[1] / "frontend/dist/assets").mkdir(parents=True)
    (source[1] / "frontend/dist/index.html").write_text("<html></html>")
    (source[1] / "frontend/dist/assets/index.js").write_text("/* synthetic build */")
    cleanup, preserved = ready(source)
    assert execute(source, cleanup)["result"] == "SOURCE_CLEANUP_COMPLETE"
    repository, target = source[:2]
    completion = lifecycle.close_preserved(
        repository.repo, target=target, expected_record_version=1, owner="module-owner"
    )
    assert completion["decision"]["code"] == "PRESERVED"
    store = c._store(cleanup["preservation_plan"])
    receipt = store._read_json(
        store.completion_receipts_dir / f"{completion['decision']['completion_receipt_id']}.json"
    )
    assert receipt["source_cleanup_evidence"]
    assert receipt["handoff_required"] is False
    assert receipt["task_completed"] is False
    assert receipt["kind"] == "preserved_closeout"
    dry_run = lifecycle.cleanup_typed_disposable(
        repository.repo,
        target=target,
        expected_record_version=2,
        owner="module-owner",
        dry_run=True,
    )
    assert dry_run["decision"]["plan"]["blockers"] == []
    assert dry_run["decision"]["plan"]["cleanup_paths"] == ["frontend/dist"]
    cleaned = lifecycle.cleanup_typed_disposable(
        repository.repo,
        target=target,
        expected_record_version=2,
        owner="module-owner",
        dry_run=False,
        expected_plan_id=dry_run["decision"]["plan"]["plan_id"],
    )
    assert cleaned["decision"]["code"] == "TYPED_DISPOSABLE_CLEANED"
    assert not (target / "frontend/dist").exists()
    released = lifecycle.release(
        repository.repo, target=target, expected_record_version=2, owner_release="module-owner"
    )
    assert released["decision"]["code"] == "RELEASED"
    assert not target.exists()
    assert (
        s._verify_generation(cleanup["preservation_plan"], preserved["receipt_sha256"])[
            "verification"
        ]
        == "PASS"
    )


def test_source_cleanup_cannot_be_reported_as_business_completion(source):
    cleanup, _ = ready(source)
    execute(source, cleanup)
    with pytest.raises(ControllerError) as error:
        lifecycle.complete_no_change(
            source[0].repo, target=source[1], expected_record_version=1, owner="module-owner"
        )
    assert error.value.code == "PRESERVED_CLOSE_REQUIRED"
    assert source[1].exists()


def test_preserved_close_requires_archive_even_when_worktree_is_clean(source, monkeypatch):
    cleanup, _ = ready(source)
    execute(source, cleanup)
    monkeypatch.setattr(c, "verify_completed", lambda *a, **k: [])
    with pytest.raises(ControllerError) as error:
        lifecycle.close_preserved(
            source[0].repo, target=source[1], expected_record_version=1, owner="module-owner"
        )
    assert error.value.code == "MISSING_PRESERVATION_EVIDENCE"


def test_runtime_appearing_after_cleanup_blocks_preserved_close(source, monkeypatch):
    cleanup, _ = ready(source)
    execute(source, cleanup)
    runtime = empty_runtime_evidence()
    runtime["managed_references"] = [{"kind": "fixture", "pid": 99999999}]
    monkeypatch.setattr(lifecycle, "runtime_authority_evidence", lambda *a, **k: runtime)
    with pytest.raises(ControllerError):
        lifecycle.close_preserved(
            source[0].repo, target=source[1], expected_record_version=1, owner="module-owner"
        )
    assert source[1].exists()


@pytest.mark.parametrize("field", ["allocation_id", "head", "task_completed", "source_cleanup_evidence"])
def test_preserved_close_receipt_identity_is_rechecked_before_release(source, monkeypatch, field):
    monkeypatch.setattr(lifecycle, "runtime_references", lambda *a, **k: ())
    monkeypatch.setattr(lifecycle, "editable_references", lambda *a, **k: ())
    cleanup, _ = ready(source)
    execute(source, cleanup)
    result = lifecycle.close_preserved(
        source[0].repo, target=source[1], expected_record_version=1, owner="module-owner"
    )
    store = c._store(cleanup["preservation_plan"])
    path = store.completion_receipts_dir / f"{result['decision']['completion_receipt_id']}.json"
    receipt = json.loads(path.read_bytes())
    receipt[field] = True if field == "task_completed" else "changed"
    path.write_text(json.dumps(receipt))
    with pytest.raises(ControllerError) as error:
        lifecycle.release(
            source[0].repo, target=source[1], expected_record_version=2,
            owner_release="module-owner"
        )
    assert error.value.code == "PRESERVATION_CLOSEOUT_DRIFT"
    assert source[1].exists()


@pytest.mark.parametrize("drift", ["generation", "source", "cleanup-receipt", "action-evidence"])
def test_archived_source_must_still_verify_at_completion_and_release(source, monkeypatch, drift):
    monkeypatch.setattr(lifecycle, "runtime_references", lambda *a, **k: ())
    monkeypatch.setattr(lifecycle, "editable_references", lambda *a, **k: ())
    cleanup, preserved = ready(source)
    result = execute(source, cleanup)
    lifecycle.close_preserved(
        source[0].repo, target=source[1], expected_record_version=1, owner="module-owner"
    )
    if drift == "generation":
        path = Path(preserved["generation"]) / "tree/src/plain.py"
    elif drift == "source":
        path = source[1] / "src/plain.py"
    elif drift == "action-evidence":
        receipt = json.loads(Path(result["receipt"]["path"]).read_bytes())
        path = Path(receipt["actions"][0]["after"]["path"])
    else:
        path = Path(result["receipt"]["path"])
    mode = path.stat().st_mode & 0o777
    path.chmod(0o600)
    path.write_bytes(path.read_bytes() + b"\n")
    path.chmod(mode)
    with pytest.raises(ControllerError):
        lifecycle.release(
            source[0].repo,
            target=source[1],
            expected_record_version=2,
            owner_release="module-owner",
        )
    assert source[1].exists()


def test_unknown_ignored_resources_still_block_archived_source_release(source, monkeypatch):
    monkeypatch.setattr(lifecycle, "runtime_references", lambda *a, **k: ())
    monkeypatch.setattr(lifecycle, "editable_references", lambda *a, **k: ())
    (source[1] / "ignored").mkdir()
    (source[1] / "ignored/unique").write_text("not source coverage")
    cleanup, _ = ready(source)
    assert execute(source, cleanup)["result"] == "SOURCE_CLEANUP_COMPLETE"
    lifecycle.close_preserved(
        source[0].repo, target=source[1], expected_record_version=1, owner="module-owner"
    )
    result = lifecycle.cleanup_typed_disposable(
        source[0].repo,
        target=source[1],
        expected_record_version=2,
        owner="module-owner",
        dry_run=True,
    )
    assert any(
        b["code"] == "UNKNOWN_IGNORED_RESOURCE" for b in result["decision"]["plan"]["blockers"]
    )
    with pytest.raises(ControllerError):
        lifecycle.release(
            source[0].repo,
            target=source[1],
            expected_record_version=2,
            owner_release="module-owner",
        )
