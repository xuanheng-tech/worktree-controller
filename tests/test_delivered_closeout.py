from __future__ import annotations

import json
import os
from dataclasses import replace

import pytest

from tests.test_controlled import acquire_one, adopt_canonical
from tests.test_retention_admission import POLICY_V5
from tests.test_shadow import GitRepository, git
from worktree_controller import lifecycle
from worktree_controller.controller import doctor, guard_write
from worktree_controller.delivered_closeout import close_delivered
from worktree_controller.governance import gc_apply, gc_plan
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore, record_to_dict
from worktree_controller.writer_lease import acquire_writer_lease


@pytest.fixture(params=["RETAINED", "PRESERVED"])
def delivered(monkeypatch, request):
    # Host process evidence is supplied explicitly by tests; no caller processes/services.
    evidence = {
        "managed_references": [],
        "observed_references": [],
        "managed_visibility_uncertainties": [],
        "scan_uncertainties": [],
    }
    monkeypatch.setattr(lifecycle, "_typed_runtime_evidence", lambda *_args: evidence)
    monkeypatch.setattr(lifecycle, "_validate_runtime_clear", lambda *_args, **_kwargs: None)
    with GitRepository(linked=0, policy_text=POLICY_V5) as repo:
        adopt_canonical(repo)
        remote = repo.root / "published.git"
        git(repo.root, "init", "--bare", str(remote))
        git(repo.repo, "remote", "add", "origin", str(remote))
        git(repo.repo, "push", "origin", "main")
        path, allocation = acquire_one(repo, task="delivered-audit", role="audit")
        (path / "task.txt").write_text("synthetic delivered result\n")
        git(path, "add", "task.txt")
        git(path, "commit", "-m", "synthetic task result")
        git(path, "push", "origin", "HEAD:main")
        git(repo.repo, "fetch", "origin", "main")
        lifecycle.park(repo.repo, target=path, expected_record_version=1, owner="module-owner")
        store = MetadataStore(repo.repo / ".git")
        allocation_id = allocation["decision"]["allocation_id"]
        if request.param == "PRESERVED":
            # Model historical metadata with no frozen handoff or archive receipt.
            with store.lock(exclusive=True):
                record = store._load_unlocked().records[allocation_id]
                store.replace_record(replace(record, lifecycle="PRESERVED"))
        args = dict(
            target=path, expected_record_version=2, owner_release="module-owner", actor="governance"
        )
        yield repo, path, store, allocation_id, args, evidence


def test_closeout_preserves_task_evidence_then_uses_normal_gc_release(delivered):
    repo, path, store, allocation_id, args, _ = delivered
    before = record_to_dict(store.load().records[allocation_id])
    head = git(path, "rev-parse", "HEAD")
    branch = git(path, "branch", "--show-current")
    planned = close_delivered(repo.repo, **args, dry_run=True)
    assert store.load().records[allocation_id].lifecycle == before["lifecycle"]
    plan_id = planned["decision"]["plan_id"]
    result = close_delivered(repo.repo, **args, plan_id=plan_id)
    assert result["decision"]["task_completed"] is False
    after = record_to_dict(store.load().records[allocation_id])
    assert after["history"][-1]["from"] == before["lifecycle"]
    for key in before.keys() - {"lifecycle", "history", "record_version", "last_reviewed_at"}:
        assert after[key] == before[key]
    assert git(path, "rev-parse", "HEAD") == head
    assert git(path, "status", "--porcelain") == ""
    assert (path / "task.txt").read_text() == "synthetic delivered result\n"
    assert doctor(repo.repo)["decision"]["code"] == "PASS"
    assert (
        guard_write(
            repo.repo,
            target=path,
            task_key="delivered-audit",
            authority_key="new-authority",
            owner="module-owner",
            operation="feature-write",
        )["decision"]["code"]
        != "WRITE_ALLOWED"
    )
    assert (
        close_delivered(repo.repo, **args, plan_id=plan_id)["decision"]["code"] == "ALREADY_CLOSED"
    )
    release_plan = gc_plan(repo.repo, target=path, owner_release="module-owner")
    assert release_plan["decision"]["code"] == "GC_PREFLIGHT_READY"
    removed = gc_apply(
        repo.repo,
        target=path,
        owner_release="module-owner",
        expected_gc_plan_id=release_plan["decision"]["gc_plan_id"],
    )
    assert removed["decision"]["code"] == "RELEASED"
    assert not path.exists()
    assert git(repo.repo, "rev-parse", branch) == head
    assert store.load().records[allocation_id].task_commit == before["task_commit"]
    assert doctor(repo.repo)["decision"]["code"] == "PASS"


@pytest.mark.parametrize(
    "kind,code",
    [
        ("active", "INVALID_LIFECYCLE"),
        ("dirty", "DIRTY_WORKTREE"),
        ("untracked", "DIRTY_WORKTREE"),
        ("unpublished", "DELIVERABLE_NOT_PUBLISHED"),
        ("binding", "MANAGED_REFERENCE"),
        ("handoff", "HANDOFF_EVIDENCE_PRESENT"),
        ("writer", "MANAGED_REFERENCE"),
        ("runtime", "RUNTIME_REFERENCE"),
        ("unknown_runtime", "RUNTIME_SCAN_UNCERTAIN"),
        ("ignored", "IGNORED_CONTENT"),
        ("owner", "OWNER_MISMATCH"),
        ("remote", "PUBLICATION_UNVERIFIED"),
    ],
)
def test_closeout_refuses_unsafe_or_unproven_targets(delivered, kind, code):
    repo, path, store, allocation_id, args, evidence = delivered
    record = store.load().records[allocation_id]
    if kind == "active":
        with store.lock(exclusive=True):
            store.replace_record(replace(record, lifecycle="ACTIVE"))
    elif kind in {"dirty", "untracked"}:
        (path / ("task.txt" if kind == "dirty" else "unfinished.txt")).write_text("keep me\n")
    elif kind == "unpublished":
        (path / "task.txt").write_text("unpublished\n")
        git(path, "add", "task.txt")
        git(path, "commit", "-m", "unpublished result")
    elif kind in {"binding", "handoff"}:
        updates = (
            {"external_bindings": ("test-binding",)}
            if kind == "binding"
            else {"handoff_commit": record.task_commit}
        )
        with store.lock(exclusive=True):
            store.replace_record(replace(record, **updates))
    elif kind == "writer":
        if record.lifecycle == "PRESERVED":
            with store.lock(exclusive=True):
                store.replace_record(replace(record, lifecycle="RETAINED"))
        lifecycle.resume(repo.repo, target=path, expected_record_version=2, owner="module-owner")
        acquire_writer_lease(
            repo.repo,
            target=path,
            owner="module-owner",
            client_id="test",
            holder_pid=os.getpid(),
            expected_record_version=3,
        )
        lease = store.load().writer_leases[allocation_id]
        lifecycle.park(repo.repo, target=path, expected_record_version=3, owner="module-owner")
        # Legacy metadata can retain a lease after lifecycle changed; it must still block.
        with store.lock(exclusive=True):
            store.write_writer_lease(lease)
            if record.lifecycle == "PRESERVED":
                parked = store._load_unlocked().records[allocation_id]
                store.replace_record(replace(parked, lifecycle="PRESERVED"))
        args["expected_record_version"] = 4
    elif kind == "runtime":
        evidence["observed_references"].append({"kind": "cwd", "pid": 123})
    elif kind == "unknown_runtime":
        evidence["scan_uncertainties"].append({"kind": "unknown"})
    elif kind == "ignored":
        info = repo.repo / ".git/info/exclude"
        info.write_text(info.read_text() + "\nunknown-evidence.txt\n")
        (path / "unknown-evidence.txt").write_text("keep unknown evidence\n")
    elif kind == "owner":
        args["owner_release"] = "different-owner"
    elif kind == "remote":
        git(repo.repo, "remote", "set-url", "origin", str(repo.root / "missing-remote"))
    with pytest.raises(ControllerError) as raised:
        close_delivered(repo.repo, **args, dry_run=True)
    assert raised.value.code == code
    assert path.exists()
    assert not list(store.completion_receipts_dir.glob("*.json"))


def test_plan_drift_and_missing_plan_never_transition(delivered):
    repo, path, store, allocation_id, args, _ = delivered
    before = record_to_dict(store.load().records[allocation_id])
    with pytest.raises(ControllerError, match="exact reviewed"):
        close_delivered(repo.repo, **args)
    plan_id = close_delivered(repo.repo, **args, dry_run=True)["decision"]["plan_id"]
    args["actor"] = "changed-actor"
    with pytest.raises(ControllerError) as raised:
        close_delivered(repo.repo, **args, plan_id=plan_id)
    assert raised.value.code == "CLOSEOUT_PLAN_DRIFT"
    assert record_to_dict(store.load().records[allocation_id]) == before
    assert not list(store.completion_receipts_dir.glob("*.json"))
    assert path.exists()


def test_closeout_retries_after_receipt_without_rewriting_it(delivered, monkeypatch):
    repo, path, store, allocation_id, args, _ = delivered
    initial_lifecycle = store.load().records[allocation_id].lifecycle
    plan_id = close_delivered(repo.repo, **args, dry_run=True)["decision"]["plan_id"]
    original = MetadataStore.replace_record

    def interrupted(*_args):
        raise RuntimeError("simulated process interruption")

    monkeypatch.setattr(MetadataStore, "replace_record", interrupted)
    with pytest.raises(RuntimeError, match="interruption"):
        close_delivered(repo.repo, **args, plan_id=plan_id)
    (receipt_path,) = store.completion_receipts_dir.glob("*.json")
    receipt_bytes = receipt_path.read_bytes()
    assert store.load().records[allocation_id].lifecycle == initial_lifecycle
    monkeypatch.setattr(MetadataStore, "replace_record", original)
    close_delivered(repo.repo, **args, plan_id=plan_id)
    assert receipt_path.read_bytes() == receipt_bytes
    assert store.load().records[allocation_id].lifecycle == "RELEASE_READY"


def test_plan_rechecks_newly_published_head_before_transition(delivered):
    repo, path, store, allocation_id, args, _ = delivered
    before = record_to_dict(store.load().records[allocation_id])
    plan_id = close_delivered(repo.repo, **args, dry_run=True)["decision"]["plan_id"]
    (path / "task.txt").write_text("later published work must survive\n")
    git(path, "add", "task.txt")
    git(path, "commit", "-m", "later published result")
    git(path, "push", "origin", "HEAD:main")
    git(repo.repo, "fetch", "origin", "main")
    with pytest.raises(ControllerError) as raised:
        close_delivered(repo.repo, **args, plan_id=plan_id)
    assert raised.value.code == "CLOSEOUT_PLAN_DRIFT"
    assert record_to_dict(store.load().records[allocation_id]) == before
    assert not list(store.completion_receipts_dir.glob("*.json"))
    assert (path / "task.txt").read_text() == "later published work must survive\n"


@pytest.mark.parametrize("delivered", ["PRESERVED"], indirect=True)
@pytest.mark.parametrize("archive", ["verified", "invalid"])
def test_preserved_archive_evidence_requires_existing_recovery_flow(
    delivered, monkeypatch, archive
):
    repo, path, store, allocation_id, args, _ = delivered
    before = record_to_dict(store.load().records[allocation_id])

    def archive_evidence(*_args):
        if archive == "invalid":
            raise ControllerError("SOURCE_CLEANUP_EVIDENCE_INVALID", "synthetic invalid proof")
        return [{"synthetic_archive": True}]

    monkeypatch.setattr("worktree_controller.source_cleanup.verify_completed", archive_evidence)
    with pytest.raises(ControllerError) as raised:
        close_delivered(repo.repo, **args, dry_run=True)
    assert (
        raised.value.code
        == {
            "verified": "PRESERVATION_FLOW_REQUIRED",
            "invalid": "SOURCE_CLEANUP_EVIDENCE_INVALID",
        }[archive]
    )
    assert record_to_dict(store.load().records[allocation_id]) == before
    assert not list(store.completion_receipts_dir.glob("*.json"))
    assert path.exists()


def test_preserved_resources_must_be_cleaned_before_closeout(delivered):
    repo, path, store, allocation_id, args, _ = delivered
    record = store.load().records[allocation_id]
    original_lifecycle = record.lifecycle
    # Register a synthetic environment using the supported retained-state verb.
    with store.lock(exclusive=True):
        store.replace_record(replace(record, lifecycle="RETAINED"))
    info = repo.repo / ".git/info/exclude"
    info.write_text(info.read_text() + "\n.venv/\n")
    venv = path / ".venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("home = /synthetic\n")
    lifecycle.register_disposable(
        repo.repo,
        target=path,
        expected_record_version=2,
        owner="module-owner",
        resource_path=".venv",
        kind="python-venv",
        reproducible=True,
        creator="test-fixture",
        teardown_policy="on-release",
    )
    with store.lock(exclusive=True):
        registered = store._load_unlocked().records[allocation_id]
        store.replace_record(replace(registered, lifecycle=original_lifecycle))
    before = record_to_dict(store.load().records[allocation_id])
    args["expected_record_version"] = 3
    if original_lifecycle == "PRESERVED":
        with pytest.raises(ControllerError) as raised:
            close_delivered(repo.repo, **args, dry_run=True)
        assert raised.value.code == "DISPOSABLE_RESOURCE_PRESENT"
    else:
        assert (
            close_delivered(repo.repo, **args, dry_run=True)["decision"]["code"] == "CLOSEOUT_READY"
        )
    assert record_to_dict(store.load().records[allocation_id]) == before
    assert venv.is_dir()
    assert not list(store.completion_receipts_dir.glob("*.json"))


def test_release_rechecks_closeout_receipt_and_dirty_content(delivered):
    repo, path, store, _allocation_id, args, _ = delivered
    plan_id = close_delivered(repo.repo, **args, dry_run=True)["decision"]["plan_id"]
    close_delivered(repo.repo, **args, plan_id=plan_id)
    (receipt_path,) = store.completion_receipts_dir.glob("*.json")
    value = json.loads(receipt_path.read_text())
    value["plan"]["task_completed"] = True
    receipt_path.write_text(json.dumps(value))
    assert doctor(repo.repo)["decision"]["code"] == "FAIL"
    with pytest.raises(ControllerError) as raised:
        lifecycle.release(
            repo.repo,
            target=path,
            expected_record_version=3,
            owner_release="module-owner",
            dry_run=True,
        )
    assert raised.value.code == "DELIVERED_CLOSEOUT_INVALID"
    assert path.exists()


def test_publication_rollback_blocks_release_after_closeout(delivered):
    repo, path, _store, _allocation_id, args, _ = delivered
    planned = close_delivered(repo.repo, **args, dry_run=True)
    close_delivered(repo.repo, **args, plan_id=planned["decision"]["plan_id"])
    # Synthetic local test remote only: model a remote ref that no longer retains the result.
    git(repo.repo, "push", "--force", "origin", "main:main")
    git(repo.repo, "fetch", "origin", "main")
    with pytest.raises(ControllerError) as raised:
        lifecycle.release(
            repo.repo,
            target=path,
            expected_record_version=3,
            owner_release="module-owner",
            dry_run=True,
        )
    assert raised.value.code == "DELIVERABLE_NOT_PUBLISHED"
    assert path.exists()


@pytest.mark.parametrize("mutation", ["dirty", "runtime"])
def test_release_keeps_new_work_and_runtime_after_closeout(delivered, mutation):
    repo, path, _store, _allocation_id, args, evidence = delivered
    plan_id = close_delivered(repo.repo, **args, dry_run=True)["decision"]["plan_id"]
    close_delivered(repo.repo, **args, plan_id=plan_id)
    if mutation == "dirty":
        (path / "task.txt").write_text("new work must survive\n")
    else:
        evidence["observed_references"].append({"kind": "cwd", "pid": 123})
    with pytest.raises(ControllerError) as raised:
        lifecycle.release(
            repo.repo, target=path, expected_record_version=3, owner_release="module-owner"
        )
    assert (
        raised.value.code == {"dirty": "DIRTY_WORKTREE", "runtime": "RUNTIME_REFERENCE"}[mutation]
    )
    assert path.exists()
    if mutation == "dirty":
        assert (path / "task.txt").read_text() == "new work must survive\n"


def test_release_recovers_exactly_after_worktree_removal(delivered, monkeypatch):
    repo, path, store, allocation_id, args, _ = delivered
    plan_id = close_delivered(repo.repo, **args, dry_run=True)["decision"]["plan_id"]
    close_delivered(repo.repo, **args, plan_id=plan_id)

    def crash(name):
        if name == "release_worktree_removed":
            raise RuntimeError("simulated post-remove interruption")

    monkeypatch.setattr(lifecycle, "_crash_point", crash)
    with pytest.raises(RuntimeError, match="post-remove"):
        lifecycle.release(
            repo.repo, target=path, expected_record_version=3, owner_release="module-owner"
        )
    assert not path.exists()
    result = lifecycle.release(
        repo.repo, target=path, expected_record_version=3, owner_release="module-owner"
    )
    assert result["decision"]["code"] == "RELEASE_RECOVERED"
    assert store.load().records[allocation_id].lifecycle == "RELEASED"
    assert doctor(repo.repo)["decision"]["code"] == "PASS"


def test_closeout_refuses_stale_tracking_and_option_like_remote(delivered):
    repo, path, _store, _allocation_id, args, _ = delivered
    git(repo.repo, "update-ref", "refs/remotes/origin/main", git(repo.repo, "rev-parse", "main"))
    with pytest.raises(ControllerError) as raised:
        close_delivered(repo.repo, **args, dry_run=True)
    assert raised.value.code == "PUBLICATION_TRACKING_STALE"
    policy = repo.repo / ".agents/worktree-policy.toml"
    policy.write_text(policy.read_text().replace('remote = "origin"', 'remote = "--unsafe"'))
    with pytest.raises(ControllerError) as raised:
        close_delivered(repo.repo, **args, dry_run=True)
    assert raised.value.code == "INVALID_REMOTE"
    assert path.exists()
