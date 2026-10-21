from __future__ import annotations

import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_controlled import acquire_one, adopt_canonical, handoff_one
from tests.test_shadow import GitRepository, git
from worktree_controller.controller import adopt, doctor, status
from worktree_controller.governance import gc_apply, gc_plan
from worktree_controller.lifecycle import release
from worktree_controller.models import ControllerError
from worktree_controller.rendering import render
from worktree_controller.store import MetadataStore


def test_doctor_reports_mount_and_namespace_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    from worktree_controller import mount_diagnostics

    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        assert doctor(repository.repo)["mount_diagnostics"]["namespace"]["comparison"] == "UNKNOWN"
        original_mount_state = mount_diagnostics._mount_state
        monkeypatch.setattr(
            mount_diagnostics,
            "_namespace_id",
            lambda pid: "mnt:[caller]" if pid == "self" else "mnt:[writer]",
        )
        monkeypatch.setattr(
            mount_diagnostics,
            "_mount_state",
            lambda path: (
                "READ_ONLY"
                if str(path).startswith(f"/proc/{os.getpid()}/root") and path.name == ".git"
                else original_mount_state(path)
            ),
        )
        result = doctor(repository.repo, namespace_pid=os.getpid())
        assert result["mount_diagnostics"]["namespace"]["comparison"] == "DIFFERENT"
        assert result["mount_diagnostics"]["namespace"]["peer"]["git_entry"] == "READ_ONLY"
        assert result["environment_diagnosis"]["code"] == "MOUNT_NAMESPACE_MISMATCH"
        assert result["decision"]["primary_diagnosis"] == "MOUNT_NAMESPACE_MISMATCH"
        assert result["decision"]["code"] == "FAIL"
        assert {warning["code"] for warning in result["warnings"]} >= {
            "GIT_ENTRY_MOUNT_READ_ONLY",
            "MOUNT_NAMESPACE_MISMATCH",
            "MOUNT_NAMESPACE_DIFFERENT",
        }


def test_different_mount_namespace_with_equal_access_is_not_a_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from worktree_controller import mount_diagnostics

    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        monkeypatch.setattr(
            mount_diagnostics,
            "_namespace_id",
            lambda pid: "mnt:[caller]" if pid == "self" else "mnt:[peer]",
        )
        diagnosis = doctor(repository.repo, namespace_pid=os.getpid())
        assert diagnosis["environment_diagnosis"]["code"] == "HEALTHY"
        assert diagnosis["decision"]["primary_diagnosis"] == "NONE"
        codes = {warning["code"] for warning in diagnosis["warnings"]}
        assert "MOUNT_NAMESPACE_DIFFERENT" in codes
        assert "MOUNT_NAMESPACE_MISMATCH" not in codes


def test_peer_proc_access_denial_is_unknown_not_peer_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from worktree_controller import mount_diagnostics

    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        original_lock_probe = mount_diagnostics._lock_probe
        monkeypatch.setattr(
            mount_diagnostics,
            "_namespace_id",
            lambda pid: "mnt:[caller]" if pid == "self" else "mnt:[peer]",
        )
        monkeypatch.setattr(
            mount_diagnostics,
            "_lock_probe",
            lambda path: (
                {"shape": "REGULAR", "writable": "UNWRITABLE", "access_errno": "EACCES"}
                if str(path).startswith(f"/proc/{os.getpid()}/root")
                else original_lock_probe(path)
            ),
        )
        diagnosis = doctor(repository.repo, namespace_pid=os.getpid())
        assert diagnosis["environment_diagnosis"]["code"] == "UNKNOWN"
        assert diagnosis["decision"]["code"] == "DEGRADED"


def test_current_mount_permission_denial_is_filesystem_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from worktree_controller import mount_diagnostics

    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        original_mount_state = mount_diagnostics._mount_state
        monkeypatch.setattr(
            mount_diagnostics,
            "_mount_state",
            lambda path: (
                "UNKNOWN" if path == repository.repo / ".git" else original_mount_state(path)
            ),
        )
        monkeypatch.setattr(
            mount_diagnostics,
            "_mount_access_errno",
            lambda path: "EACCES" if path == repository.repo / ".git" else None,
        )
        diagnosis = doctor(repository.repo)
        assert diagnosis["environment_diagnosis"]["code"] == "FILESYSTEM_PERMISSION"
        assert diagnosis["decision"]["primary_diagnosis"] == "FILESYSTEM_PERMISSION"


def test_doctor_and_status_prioritize_filesystem_diagnosis_over_lock_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from worktree_controller import mount_diagnostics

    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        store = MetadataStore(repository.repo / ".git")
        store.lock_file.unlink()
        store.lock_file.symlink_to(repository.repo / ".git" / "repo.json")
        original_mount_state = mount_diagnostics._mount_state
        monkeypatch.setattr(
            mount_diagnostics,
            "_mount_state",
            lambda path: (
                "READ_ONLY" if path == repository.repo / ".git" else original_mount_state(path)
            ),
        )
        diagnosis = doctor(repository.repo)
        assert diagnosis["environment_diagnosis"]["code"] == "FILESYSTEM_PERMISSION"
        assert diagnosis["decision"]["primary_diagnosis"] == "FILESYSTEM_PERMISSION"
        assert diagnosis["errors"][0]["code"] == "FILESYSTEM_PERMISSION"
        assert "REPOSITORY_LOCK_INVALID" not in {
            row["code"] for row in diagnosis["warnings"] + diagnosis["errors"]
        }
        with pytest.raises(ControllerError) as blocked:
            status(repository.repo)
        assert blocked.value.code == "FILESYSTEM_PERMISSION"


def test_doctor_reports_read_only_git_mount_with_healthy_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from worktree_controller import mount_diagnostics

    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        original_mount_state = mount_diagnostics._mount_state
        monkeypatch.setattr(
            mount_diagnostics,
            "_mount_state",
            lambda path: (
                "READ_ONLY" if path == repository.repo / ".git" else original_mount_state(path)
            ),
        )
        diagnosis = doctor(repository.repo)
        assert diagnosis["mount_diagnostics"]["current"]["repo"] == "RW"
        assert diagnosis["mount_diagnostics"]["current"]["git_entry"] == "READ_ONLY"
        assert diagnosis["mount_diagnostics"]["current"]["lock_writable"] == "WRITABLE"
        assert diagnosis["decision"]["primary_diagnosis"] == "FILESYSTEM_PERMISSION"
        assert "REPOSITORY_LOCK_INVALID" not in {
            row["code"] for row in diagnosis["warnings"] + diagnosis["errors"]
        }
        observed = status(repository.repo)
        assert observed["environment_diagnosis"]["code"] == "FILESYSTEM_PERMISSION"


def test_doctor_reports_unknown_when_requested_namespace_cannot_be_observed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from worktree_controller import mount_diagnostics

    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        monkeypatch.setattr(
            mount_diagnostics,
            "_namespace_id",
            lambda pid: "mnt:[caller]" if pid == "self" else None,
        )
        diagnosis = doctor(repository.repo, namespace_pid=os.getpid())
        assert diagnosis["environment_diagnosis"]["code"] == "UNKNOWN"
        assert diagnosis["decision"]["primary_diagnosis"] == "UNKNOWN"
        assert diagnosis["decision"]["code"] == "DEGRADED"


def test_doctor_keeps_metadata_failure_out_of_environment_categories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from worktree_controller import controller

    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)

        def invalid_metadata(_store: MetadataStore):
            raise ControllerError("INVALID_METADATA", "malformed record")

        monkeypatch.setattr(MetadataStore, "load", invalid_metadata)
        diagnosis = controller.doctor(repository.repo)
        assert diagnosis["environment_diagnosis"]["code"] == "HEALTHY"
        assert diagnosis["errors"][0]["code"] == "INVALID_METADATA"
        assert diagnosis["decision"]["primary_diagnosis"] == "CONTROLLER_METADATA"


def test_doctor_reports_unknown_when_git_inventory_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from worktree_controller import controller

    monkeypatch.setattr(
        controller,
        "inventory",
        lambda _path: (_ for _ in ()).throw(ControllerError("GIT_ERROR", "unreadable .git")),
    )
    diagnosis = controller.doctor(Path("/unreadable/repository"))
    assert diagnosis["environment_diagnosis"]["code"] == "UNKNOWN"
    assert diagnosis["errors"][0]["code"] == "GIT_ERROR"
    assert diagnosis["decision"]["primary_diagnosis"] == "UNKNOWN"


def test_doctor_reports_genuine_lock_corruption_only_with_healthy_environment() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        store = MetadataStore(repository.repo / ".git")
        store.lock_file.unlink()
        store.lock_file.symlink_to(repository.repo / ".git" / "repo.json")
        diagnosis = doctor(repository.repo)
        assert diagnosis["environment_diagnosis"]["code"] == "HEALTHY"
        assert diagnosis["decision"]["primary_diagnosis"] == "CONTROLLER_LOCK_AUTHORITY"
        assert diagnosis["errors"][0]["code"] == "REPOSITORY_LOCK_INVALID"
        with pytest.raises(ControllerError) as blocked:
            status(repository.repo)
        assert blocked.value.code == "REPOSITORY_LOCK_INVALID"


def test_gc_plan_classifies_authority_and_capacity_observes() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        active, _ = acquire_one(repository, task="active", authority="active-authority")
        handed, _ = acquire_one(repository, task="handed", authority="handed-authority")
        handoff_one(repository, handed)
        report = gc_plan(repository.repo)
        rows = {Path(row["path"]): row for row in report["candidates"]}
        assert report["decision"]["total"] == 3
        assert rows[repository.repo]["blockers"] == [
            "CANONICAL_PROTECTED",
            "LIFECYCLE_NOT_RELEASABLE",
        ]
        assert rows[active]["blockers"] == ["LIFECYCLE_NOT_RELEASABLE"]
        assert rows[handed]["classification"] == "REVIEW_REQUIRED"
        assert rows[handed]["blockers"] == ["RELEASE_PREFLIGHT_REQUIRED"]
        assert rows[handed]["owner"] == "module-owner"
        assert rows[handed]["dirty"] is False
        assert rows[handed]["candidate_reason"] == "RELEASE_PREFLIGHT_REQUIRED"
        assert rows[handed]["blocking_facts"] == []
        assert rows[handed]["filesystem_health"]["code"] == "HEALTHY"
        assert rows[handed]["lifecycle_observation"]["heartbeat"]["independent"] is False
        assert "lease=none" in render(report)
        capacity = status(repository.repo)["repository"]["governance_capacity"]
        assert capacity["mode"] == "observe"
        assert capacity["used"] == {"active": 1, "retained": 0, "total": 3}


def test_gc_plan_classification_does_not_follow_mount_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from worktree_controller import mount_diagnostics

    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        baseline = gc_plan(repository.repo)
        original_mount_state = mount_diagnostics._mount_state
        monkeypatch.setattr(
            mount_diagnostics,
            "_mount_state",
            lambda path: (
                "READ_ONLY" if path == repository.repo / ".git" else original_mount_state(path)
            ),
        )
        observed = gc_plan(repository.repo)
        before = next(row for row in baseline["candidates"] if row["path"] == str(target))
        after = next(row for row in observed["candidates"] if row["path"] == str(target))
        assert before["classification"] == after["classification"] == "BLOCKED"
        assert before["candidate_reason"] == after["candidate_reason"]
        assert before["blockers"] == after["blockers"]
        assert observed["environment_diagnosis"]["code"] == "FILESYSTEM_PERMISSION"


def test_gc_plan_probes_each_linked_checkout_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from worktree_controller import mount_diagnostics

    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        original_mount_state = mount_diagnostics._mount_state
        monkeypatch.setattr(
            mount_diagnostics,
            "_mount_state",
            lambda path: "READ_ONLY" if path == target / ".git" else original_mount_state(path),
        )
        report = gc_plan(repository.repo)
        rows = {Path(row["path"]): row for row in report["candidates"]}
        assert report["environment_diagnosis"]["code"] == "HEALTHY"
        assert rows[repository.repo]["filesystem_health"]["code"] == "HEALTHY"
        assert rows[target]["filesystem_health"]["code"] == "FILESYSTEM_PERMISSION"
        assert rows[target]["classification"] == "BLOCKED"


def test_gc_apply_refuses_drift_before_release() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        handoff_one(repository, target)
        reviewed = gc_plan(repository.repo, target=target, owner_release="module-owner")
        assert reviewed["decision"]["code"] == "GC_PREFLIGHT_READY"
        plan_id = reviewed["decision"]["gc_plan_id"]
        assert (
            gc_plan(repository.repo, target=target, owner_release="module-owner")["decision"][
                "gc_plan_id"
            ]
            == plan_id
        )
        (target / "new-untracked-file").write_text("drift", encoding="utf-8")
        with pytest.raises(ControllerError, match="preflight") as error:
            gc_apply(
                repository.repo,
                target=target,
                expected_gc_plan_id=plan_id,
                owner_release="module-owner",
            )
        assert error.value.code == "GC_PLAN_DRIFT"
        assert target.is_dir()
        assert not MetadataStore(repository.repo / ".git").load().release_receipts


def test_gc_apply_uses_release_receipt_and_retains_branch() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        handoff_one(repository, target)
        planned = gc_plan(repository.repo, target=target, owner_release="module-owner")
        plan_id = planned["decision"]["gc_plan_id"]
        assert plan_id in render(planned)
        with pytest.raises(ControllerError) as locked:
            release(
                repository.repo,
                target=target,
                expected_record_version=2,
                owner_release="module-owner",
                expected_governance_plan_id="0" * 64,
            )
        assert locked.value.code == "GC_PLAN_DRIFT"
        with pytest.raises(ControllerError) as wrong:
            gc_apply(
                repository.repo,
                target=target,
                expected_gc_plan_id="0" * 64,
                owner_release="module-owner",
            )
        assert wrong.value.code == "GC_PLAN_DRIFT"
        assert target.exists()
        applied = gc_apply(
            repository.repo,
            target=target,
            expected_gc_plan_id=plan_id,
            owner_release="module-owner",
        )
        assert applied["decision"]["code"] == "RELEASED"
        assert applied["decision"]["gc_plan_id"] == plan_id
        receipt_id = applied["decision"]["release_receipt_id"]
        assert receipt_id in MetadataStore(repository.repo / ".git").load().release_receipts
        assert not target.exists()
        assert git(repository.repo, "show-ref", "--verify", "refs/heads/feature/new-task")


def _missing_no_change_audit(repository: GitRepository) -> Path:
    adopt_canonical(repository)
    target = repository.root / "missing-audit"
    git(repository.repo, "worktree", "add", "--detach", str(target), "HEAD")
    adopt(
        repository.repo,
        target=target,
        task_key="old-audit",
        authority_key="old-audit",
        module="repository",
        role="audit",
        owner="module-owner",
        lifecycle="RETAINED",
    )
    store = MetadataStore(repository.repo / ".git")
    record = next(row for row in store.load().records.values() if row.task_key == "old-audit")
    store.replace_record(replace(record, base_commit=git(repository.repo, "rev-parse", "HEAD")))
    shutil.rmtree(target)
    return target


def test_gc_apply_prunes_only_missing_no_change_audit_registration() -> None:
    with GitRepository(linked=0) as repository:
        target = _missing_no_change_audit(repository)
        before = status(repository.repo)
        head = git(repository.repo, "rev-parse", "main")
        assert gc_plan(repository.repo)["decision"]["blocked"] == 2
        planned = gc_plan(repository.repo, target=target, owner_release="module-owner")
        assert planned["decision"]["code"] == "GC_PREFLIGHT_READY"
        assert planned["registration_prune_plan"]["preserving_oid"] == head
        plan_id = planned["decision"]["gc_plan_id"]
        with pytest.raises(ControllerError) as drift:
            gc_apply(
                repository.repo,
                target=target,
                expected_gc_plan_id="0" * 64,
                owner_release="module-owner",
            )
        assert drift.value.code == "GC_PLAN_DRIFT"
        applied = gc_apply(
            repository.repo,
            target=target,
            expected_gc_plan_id=plan_id,
            owner_release="module-owner",
        )
        assert applied["decision"]["code"] == "REGISTRATION_PRUNED"
        assert applied["decision"]["gc_plan_id"] == plan_id
        assert status(repository.repo)["repository"]["worktree_count"] == 1
        assert git(repository.repo, "rev-parse", "main") == head
        assert doctor(repository.repo)["decision"]["code"] == "PASS"
        snapshot = MetadataStore(repository.repo / ".git").load()
        allocation = next(
            row["allocation_id"] for row in before["worktrees"] if row["path"] == str(target)
        )
        assert snapshot.records[allocation].lifecycle == "RELEASED"
        assert allocation not in snapshot.bindings.values()
        receipts = list(
            MetadataStore(repository.repo / ".git").registration_prune_receipts_dir.glob("*.json")
        )
        assert len(receipts) == 1
        assert json.loads(receipts[0].read_text())["contract"] == "registration-prune/v1"
        with pytest.raises(ControllerError) as duplicate:
            gc_apply(
                repository.repo,
                target=target,
                expected_gc_plan_id=plan_id,
                owner_release="module-owner",
            )
        assert duplicate.value.code == "GC_ALREADY_APPLIED"


def test_gc_prune_refuses_owner_task_and_ref_drift() -> None:
    with GitRepository(linked=0) as repository:
        target = _missing_no_change_audit(repository)
        wrong = gc_plan(repository.repo, target=target, owner_release="other-owner")
        assert wrong["decision"]["blockers"] == ["ACTIVE_OWNER"]
        store = MetadataStore(repository.repo / ".git")
        snapshot = store.load()
        record = next(row for row in snapshot.records.values() if row.task_key == "old-audit")
        store.replace_record(replace(record, task_commit=record.base_commit))
        task = gc_plan(repository.repo, target=target, owner_release="module-owner")
        assert task["decision"]["blockers"] == ["TASK_EVIDENCE_PRESENT"]
        store.replace_record(record)
        planned = gc_plan(repository.repo, target=target, owner_release="module-owner")
        assert planned["decision"]["code"] == "GC_PREFLIGHT_READY"
        git(repository.repo, "commit", "--allow-empty", "-m", "main advanced")
        # The reviewed ref OID changed, so the old plan cannot be applied.
        with pytest.raises(ControllerError) as drift:
            gc_apply(
                repository.repo,
                target=target,
                expected_gc_plan_id=planned["decision"]["gc_plan_id"],
                owner_release="module-owner",
            )
        assert drift.value.code == "GC_PLAN_DRIFT"


def test_gc_prune_recovers_after_git_removed_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from worktree_controller import registration_prune

    with GitRepository(linked=0) as repository:
        target = _missing_no_change_audit(repository)
        planned = gc_plan(repository.repo, target=target, owner_release="module-owner")
        plan_id = planned["decision"]["gc_plan_id"]
        original = registration_prune.git_write

        def interrupted(repo: Path, args: tuple[str, ...]) -> object:
            original(repo, args)
            raise RuntimeError("simulated interruption after Git removal")

        monkeypatch.setattr(registration_prune, "git_write", interrupted)
        with pytest.raises(RuntimeError, match="simulated interruption"):
            gc_apply(
                repository.repo,
                target=target,
                expected_gc_plan_id=plan_id,
                owner_release="module-owner",
            )
        assert status(repository.repo)["repository"]["worktree_count"] == 1
        assert doctor(repository.repo)["decision"]["code"] == "FAIL"
        monkeypatch.setattr(registration_prune, "git_write", original)
        recovered = gc_apply(
            repository.repo,
            target=target,
            expected_gc_plan_id=plan_id,
            owner_release="module-owner",
        )
        assert recovered["decision"]["code"] == "REGISTRATION_PRUNED"
        assert doctor(repository.repo)["decision"]["code"] == "PASS"


def test_gc_prune_refuses_reappeared_path_and_unretained_commit() -> None:
    with GitRepository(linked=0) as repository:
        target = _missing_no_change_audit(repository)
        planned = gc_plan(repository.repo, target=target, owner_release="module-owner")
        target.mkdir()
        with pytest.raises(ControllerError) as blocked:
            gc_plan(repository.repo, target=target, owner_release="module-owner")
        assert blocked.value.code == "GIT_ERROR"
        with pytest.raises(ControllerError) as drift:
            gc_apply(
                repository.repo,
                target=target,
                expected_gc_plan_id=planned["decision"]["gc_plan_id"],
                owner_release="module-owner",
            )
        assert drift.value.code == "GIT_ERROR"
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target = repository.root / "unretained-audit"
        git(repository.repo, "worktree", "add", "--detach", str(target), "HEAD")
        git(target, "commit", "--allow-empty", "-m", "audit-only commit")
        head = git(target, "rev-parse", "HEAD")
        adopt(
            repository.repo,
            target=target,
            task_key="unretained-audit",
            authority_key="unretained-audit",
            module="repository",
            role="audit",
            owner="module-owner",
            lifecycle="RETAINED",
        )
        store = MetadataStore(repository.repo / ".git")
        record = next(
            row for row in store.load().records.values() if row.task_key == "unretained-audit"
        )
        store.replace_record(replace(record, base_commit=head))
        shutil.rmtree(target)
        blocked = gc_plan(repository.repo, target=target, owner_release="module-owner")
        assert blocked["decision"]["blockers"] == ["MISSING_RETAINED_REF"]


def test_gc_prune_recovers_after_metadata_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    with GitRepository(linked=0) as repository:
        target = _missing_no_change_audit(repository)
        plan_id = gc_plan(repository.repo, target=target, owner_release="module-owner")["decision"][
            "gc_plan_id"
        ]
        original = MetadataStore.remove_binding

        def interrupted(self: MetadataStore, key: str) -> None:
            raise RuntimeError("simulated interruption after record replacement")

        monkeypatch.setattr(MetadataStore, "remove_binding", interrupted)
        with pytest.raises(RuntimeError, match="simulated interruption"):
            gc_apply(
                repository.repo,
                target=target,
                expected_gc_plan_id=plan_id,
                owner_release="module-owner",
            )
        assert doctor(repository.repo)["decision"]["code"] == "FAIL"
        monkeypatch.setattr(MetadataStore, "remove_binding", original)
        recovered = gc_apply(
            repository.repo,
            target=target,
            expected_gc_plan_id=plan_id,
            owner_release="module-owner",
        )
        assert recovered["decision"]["code"] == "REGISTRATION_PRUNED"
        assert doctor(repository.repo)["decision"]["code"] == "PASS"


def test_gc_apply_returns_the_existing_receipt_when_the_reviewed_plan_already_ran() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        handoff_one(repository, target)
        planned = gc_plan(repository.repo, target=target, owner_release="module-owner")
        plan_id = planned["decision"]["gc_plan_id"]
        # e.g. a background pass executes the very same release before gc-apply runs
        executed = release(
            repository.repo,
            target=target,
            expected_record_version=planned["candidate"]["record_version"],
            owner_release="module-owner",
        )
        receipt_id = executed["decision"]["release_receipt_id"]
        replayed = gc_apply(
            repository.repo,
            target=target,
            expected_gc_plan_id=plan_id,
            owner_release="module-owner",
        )
        assert replayed["command"] == "gc-apply"
        assert replayed["decision"]["code"] == "ALREADY_RELEASED"
        assert replayed["decision"]["release_receipt_id"] == receipt_id
        assert replayed["decision"]["gc_plan_id"] == plan_id
        assert len(MetadataStore(repository.repo / ".git").load().release_receipts) == 1
        with pytest.raises(ControllerError) as other:
            gc_apply(
                repository.repo,
                target=target,
                expected_gc_plan_id="0" * 64,
                owner_release="module-owner",
            )
        assert other.value.code == "GC_PLAN_DRIFT"
        assert receipt_id in str(other.value)
