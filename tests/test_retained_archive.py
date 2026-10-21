"""A clean retained branch can outlive its physical checkout without claiming delivery."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from tests import test_cleanup_recovery as recovery_fixtures
from tests import test_typed_disposable as typed_fixtures
from tests.test_controlled import (
    _commit_reproducible_project,
    _create_reproducible_venv,
    acquire_one,
    adopt_canonical,
)
from tests.test_retention_admission import POLICY_V5
from tests.test_shadow import GitRepository, git
from worktree_controller import (
    lifecycle,
    preservation,
    preserved_cleanup,
    source_cleanup,
    source_preservation,
)
from worktree_controller.controller import doctor, plan
from worktree_controller.convergence import _integration_bound
from worktree_controller.delivered_closeout import _eligible, archive_retained
from worktree_controller.git_backend import inventory
from worktree_controller.governance import gc_apply, gc_plan
from worktree_controller.models import (
    ControllerError,
    IntegrationReconciliationReceipt,
    WriterHolder,
    WriterLease,
)
from worktree_controller.store import MetadataStore, record_to_dict


@pytest.fixture
def retained(monkeypatch):
    monkeypatch.setattr(
        lifecycle,
        "_typed_runtime_evidence",
        lambda *_args: {
            "managed_references": [],
            "observed_references": [],
            "managed_visibility_uncertainties": [],
            "scan_uncertainties": [],
        },
    )
    with GitRepository(linked=0, policy_text=POLICY_V5) as repo:
        adopt_canonical(repo)
        path, allocation = acquire_one(repo, task="unfinished-feature")
        (path / "result.txt").write_text("recoverable task result\n")
        git(path, "add", "result.txt")
        git(path, "commit", "-m", "unfinished task commit")
        head = git(path, "rev-parse", "HEAD")
        branch = git(path, "branch", "--show-current")
        lifecycle.park(repo.repo, target=path, expected_record_version=1, owner="module-owner")
        store = MetadataStore(repo.repo / ".git")
        allocation_id = allocation["decision"]["allocation_id"]
        args = dict(
            target=path,
            expected_record_version=2,
            owner_release="module-owner",
            actor="governance",
        )
        yield repo, path, store, allocation_id, head, branch, args


def _abandoned_source(repository, path, allocation_id, scope_path):
    store = MetadataStore(repository.repo / ".git")
    record = store.load().records[allocation_id]
    lifecycle.handoff(
        repository.repo,
        target=path,
        expected_record_version=record.record_version,
        kind="integration-ready",
        commit=git(path, "rev-parse", "HEAD"),
        finalizer_record_id="finalizer:synthetic-source",
        scope_paths=(scope_path,),
    )
    queued = lifecycle.enqueue_integration(
        repository.repo,
        task_key=f"{record.task_key}-old-integration",
        authority_key=f"{record.task_key}-old-integration",
        module="integration",
        owner="integration-owner",
        source_allocations=(allocation_id,),
    )
    intent_id = queued["decision"]["intent_id"]
    lifecycle.abandon_integration_intent(
        repository.repo,
        intent_id=intent_id,
        owner="integration-owner",
        reason="preparation ended without a physical candidate",
    )
    lifecycle.reopen(
        repository.repo,
        target=path,
        expected_record_version=record.record_version + 1,
        owner=record.owner,
    )
    lifecycle.park(
        repository.repo,
        target=path,
        expected_record_version=record.record_version + 2,
        owner=record.owner,
    )
    return store, intent_id


def test_formally_abandoned_preparation_allows_archive_without_rewriting_intent(retained):
    repo, path, store, allocation_id, head, branch, args = retained
    _, intent_id = _abandoned_source(repo, path, allocation_id, "result.txt")
    intent_path = store.integration_intents_dir / f"{intent_id}.json"
    intent_bytes = intent_path.read_bytes()
    snapshot = store.load()
    intent = snapshot.integration_intents[intent_id]
    record = snapshot.records[allocation_id]
    assert intent.state == "ATTENTION_REQUIRED"
    assert intent.allocation_id is None
    assert intent.history[-1]["event"] == "PREPARATION_ABANDONED"
    assert lifecycle._effective_intent_state(snapshot, intent) == "ABANDONED"
    assert not _integration_bound(snapshot, record)
    args["expected_record_version"] = record.record_version
    plan_id = archive_retained(repo.repo, **args, dry_run=True)["decision"]["plan_id"]
    closed = archive_retained(repo.repo, **args, plan_id=plan_id)["decision"]
    assert closed["task_completed"] is False
    assert (
        lifecycle.release(
            repo.repo,
            target=path,
            expected_record_version=closed["record_version"],
            owner_release="module-owner",
        )["decision"]["code"]
        == "RELEASED"
    )
    assert intent_path.read_bytes() == intent_bytes
    assert git(repo.repo, "rev-parse", branch) == head


@pytest.mark.parametrize("invalid", ["missing-event", "missing-reason", "live-allocation"])
def test_invalid_abandonment_still_blocks_archive_and_convergence(retained, invalid):
    repo, path, store, allocation_id, _head, _branch, args = retained
    _, intent_id = _abandoned_source(repo, path, allocation_id, "result.txt")
    with store.lock(exclusive=True):
        snapshot = store._load_unlocked()
        intent = snapshot.integration_intents[intent_id]
        changes = {
            "missing-event": {"history": intent.history[:-1]},
            "missing-reason": {"blocked_reason": None},
            "live-allocation": {"allocation_id": str(uuid4())},
        }[invalid]
        store.replace_integration_intent(replace(intent, **changes))
    snapshot = store.load()
    record = snapshot.records[allocation_id]
    assert _integration_bound(snapshot, record)
    args["expected_record_version"] = record.record_version
    with pytest.raises(ControllerError) as blocked:
        archive_retained(repo.repo, **args, dry_run=True)
    assert blocked.value.code == "INTEGRATION_OBLIGATION"
    assert path.exists()


def test_archive_and_convergence_honor_existing_immutable_terminal_receipt(retained):
    repo, path, store, allocation_id, _head, _branch, args = retained
    _, intent_id = _abandoned_source(repo, path, allocation_id, "result.txt")
    with store.lock(exclusive=True):
        intent = store._load_unlocked().integration_intents[intent_id]
        store.replace_integration_intent(replace(intent, history=intent.history[:-1]))
        receipt = IntegrationReconciliationReceipt(
            schema_version=1,
            receipt_id=str(uuid4()),
            repository_id=store._load_unlocked().repository_id,
            intent_id=intent_id,
            observed_intent_schema_version=intent.schema_version,
            observed_intent_state=intent.state,
            observed_allocation_id=str(uuid4()),
            observed_record_version=2,
            observed_record_lifecycle="RELEASED",
            effective_terminal_state="RELEASED",
            result="STALE_INTENT_TERMINAL_ACKNOWLEDGED",
            actor="synthetic-reconciler",
            reconciled_at=intent.updated_at,
        )
        store.write_integration_reconciliation_receipt(receipt)
    receipt_path = store.integration_reconciliation_receipts_dir / f"{receipt.receipt_id}.json"
    receipt_bytes = receipt_path.read_bytes()
    snapshot = store.load()
    record = snapshot.records[allocation_id]
    assert not _integration_bound(snapshot, record)
    args["expected_record_version"] = record.record_version
    assert archive_retained(repo.repo, **args, dry_run=True)["decision"]["code"] == "ARCHIVE_READY"
    assert receipt_path.read_bytes() == receipt_bytes
    raw = json.loads(receipt_bytes)
    raw["effective_terminal_state"] = "ABANDONED"
    receipt_path.write_text(json.dumps(raw))
    with pytest.raises(ControllerError) as invalid:
        archive_retained(repo.repo, **args, dry_run=True)
    assert invalid.value.code == "INVALID_METADATA"


def test_raw_terminal_label_cannot_replace_formal_abandonment(retained):
    repo, path, store, allocation_id, _head, _branch, args = retained
    _, intent_id = _abandoned_source(repo, path, allocation_id, "result.txt")
    args["expected_record_version"] = store.load().records[allocation_id].record_version
    intent_path = store.integration_intents_dir / f"{intent_id}.json"
    raw = json.loads(intent_path.read_text())
    raw["state"] = "ABANDONED"
    intent_path.write_text(json.dumps(raw))
    with pytest.raises(ControllerError) as invalid:
        archive_retained(repo.repo, **args, dry_run=True)
    assert invalid.value.code == "INVALID_METADATA"
    assert path.exists()


def test_abandoned_history_does_not_clear_another_open_source_obligation(retained):
    repo, path, store, allocation_id, _head, _branch, args = retained
    _, intent_id = _abandoned_source(repo, path, allocation_id, "result.txt")
    with store.lock(exclusive=True):
        intent = store._load_unlocked().integration_intents[intent_id]
        other = replace(
            intent,
            intent_id=str(uuid4()),
            task_key="another-open-integration",
            authority_key="another-open-integration",
            state="QUEUED",
            blocked_reason=None,
            history=intent.history[:-1],
        )
        store.write_integration_intent(other)
    snapshot = store.load()
    record = snapshot.records[allocation_id]
    assert _integration_bound(snapshot, record)
    args["expected_record_version"] = record.record_version
    with pytest.raises(ControllerError) as blocked:
        archive_retained(repo.repo, **args, dry_run=True)
    assert blocked.value.code == "INTEGRATION_OBLIGATION"
    assert path.exists()


def test_abandoned_source_still_requires_its_frozen_handoff_on_the_branch(retained):
    repo, path, store, allocation_id, _head, _branch, args = retained
    _, intent_id = _abandoned_source(repo, path, allocation_id, "result.txt")
    unrelated = git(repo.repo, "commit-tree", "HEAD^{tree}", "-m", "unrelated source handoff")
    with store.lock(exclusive=True):
        intent = store._load_unlocked().integration_intents[intent_id]
        store.replace_integration_intent(replace(intent, source_handoffs=(unrelated,)))
    args["expected_record_version"] = store.load().records[allocation_id].record_version
    with pytest.raises(ControllerError) as blocked:
        archive_retained(repo.repo, **args, dry_run=True)
    assert blocked.value.code == "INTEGRATION_OBLIGATION"
    assert path.exists()


def test_archive_retained_keeps_unfinished_commit_and_uses_guarded_release(retained):
    repo, path, store, allocation_id, head, branch, args = retained
    original = record_to_dict(store.load().records[allocation_id])
    planned = archive_retained(repo.repo, **args, dry_run=True)
    assert planned["decision"]["code"] == "ARCHIVE_READY"
    assert store.load().records[allocation_id].lifecycle == "RETAINED"
    plan_id = planned["decision"]["plan_id"]
    archived = archive_retained(repo.repo, **args, plan_id=plan_id)
    assert archived["decision"]["task_completed"] is False
    assert archived["decision"]["code"] == "ARCHIVED"
    record = store.load().records[allocation_id]
    after = record_to_dict(record)
    for key in original.keys() - {"lifecycle", "history", "record_version", "last_reviewed_at"}:
        assert after[key] == original[key]
    assert doctor(repo.repo)["decision"]["code"] == "PASS"
    while_archived = plan(
        repo.repo,
        task_key="unfinished-feature",
        authority_key="new-authority",
        module="module",
        role="feature",
    )
    assert while_archived["decision"]["code"] == "REUSE"
    assert (
        archive_retained(repo.repo, **args, plan_id=plan_id)["decision"]["code"]
        == "ALREADY_ARCHIVED"
    )
    release_plan = gc_plan(repo.repo, target=path, owner_release="module-owner")
    assert release_plan["decision"]["code"] == "GC_PREFLIGHT_READY"
    released = gc_apply(
        repo.repo,
        target=path,
        owner_release="module-owner",
        expected_gc_plan_id=release_plan["decision"]["gc_plan_id"],
    )
    assert released["decision"]["code"] == "RELEASED"
    assert not path.exists()
    assert git(repo.repo, "rev-parse", branch) == head
    assert (
        record_to_dict(store.load().records[allocation_id])["task_commit"]
        == original["task_commit"]
    )
    assert doctor(repo.repo)["decision"]["code"] == "PASS"
    after_release = plan(
        repo.repo,
        task_key="unfinished-feature",
        authority_key="new-authority",
        module="module",
        role="feature",
    )
    assert after_release["decision"]["code"] == "CREATE_ALLOWED"


def test_archive_retained_refuses_dirty_or_changed_plan(retained):
    repo, path, store, allocation_id, _head, _branch, args = retained
    plan_id = archive_retained(repo.repo, **args, dry_run=True)["decision"]["plan_id"]
    with pytest.raises(ControllerError) as missing:
        archive_retained(repo.repo, **args)
    assert missing.value.code == "ARCHIVE_PLAN_REQUIRED"
    (path / "draft.txt").write_text("uncommitted user work\n")
    with pytest.raises(ControllerError) as dirty:
        archive_retained(repo.repo, **args, plan_id=plan_id)
    assert dirty.value.code == "DIRTY_WORKTREE"
    assert store.load().records[allocation_id].lifecycle == "RETAINED"
    assert (path / "draft.txt").read_text() == "uncommitted user work\n"


def test_archive_retained_refuses_successor_authority(retained):
    repo, path, store, allocation_id, _head, _branch, args = retained
    _, successor = acquire_one(repo, task="successor", authority="successor")
    with store.lock(exclusive=True):
        successor_id = successor["decision"]["allocation_id"]
        record = store._load_unlocked().records[successor_id]
        store.replace_record(replace(record, authority_key="new-authority"))
    with pytest.raises(ControllerError) as raised:
        archive_retained(repo.repo, **args, dry_run=True)
    assert raised.value.code == "DUPLICATE_AUTHORITY"
    assert store.load().records[allocation_id].lifecycle == "RETAINED"
    assert path.exists()


@pytest.mark.parametrize(
    "intent_state,handoff_on_branch,require_publication,allowed",
    [
        ("RELEASED", True, False, True),
        ("REMOTE_VERIFIED", True, False, False),
        ("RELEASED", False, False, False),
        ("RELEASED", True, True, False),
    ],
)
def test_historical_integration_only_waives_archive_obligation(
    retained, intent_state, handoff_on_branch, require_publication, allowed
):
    repo, path, store, allocation_id, head, _branch, _args = retained
    state = inventory(repo.repo)
    item = next(item for item in state.worktrees if item.path == path)
    record = store.load().records[allocation_id]
    handoff = (
        head
        if handoff_on_branch
        else git(repo.repo, "commit-tree", "HEAD^{tree}", "-m", "unrelated historical handoff")
    )
    intent = SimpleNamespace(
        intent_id="historical",
        allocation_id="candidate-allocation",
        source_allocations=(allocation_id,),
        source_handoffs=(handoff,),
        state=intent_state,
        blocked_reason=None,
        history=(),
    )
    snapshot = replace(store.load(), integration_intents={"historical": intent})
    if allowed:
        assert (
            _eligible(state, snapshot, item, record, state="RETAINED", require_publication=False)
            == {}
        )
    else:
        with pytest.raises(ControllerError) as raised:
            _eligible(
                state,
                snapshot,
                item,
                record,
                state="RETAINED",
                require_publication=require_publication,
            )
        assert raised.value.code == "INTEGRATION_OBLIGATION"


def test_archive_receipt_drift_blocks_doctor_and_release(retained):
    repo, path, store, allocation_id, _head, _branch, args = retained
    plan_id = archive_retained(repo.repo, **args, dry_run=True)["decision"]["plan_id"]
    archived = archive_retained(repo.repo, **args, plan_id=plan_id)
    receipt_path = store.completion_receipts_dir / f"{archived['decision']['receipt_id']}.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["plan"]["task_completed"] = True
    receipt_path.write_text(json.dumps(receipt))
    assert store.load().records[allocation_id].lifecycle == "ARCHIVED"
    assert doctor(repo.repo)["decision"]["code"] == "FAIL"
    blocked = gc_plan(repo.repo, target=path, owner_release="module-owner")
    assert blocked["decision"]["code"] == "GC_BLOCKED"
    assert blocked["decision"]["blockers"] == ["RETAINED_ARCHIVE_INVALID"]
    assert path.exists()


def test_archive_recovers_receipt_written_before_metadata_transition(retained):
    repo, path, store, allocation_id, _head, _branch, args = retained
    plan_id = archive_retained(repo.repo, **args, dry_run=True)["decision"]["plan_id"]
    with (
        patch.object(MetadataStore, "replace_record", side_effect=RuntimeError("interrupted")),
        pytest.raises(RuntimeError, match="interrupted"),
    ):
        archive_retained(repo.repo, **args, plan_id=plan_id)
    assert store.load().records[allocation_id].lifecycle == "RETAINED"
    assert len(list(store.completion_receipts_dir.glob("*.json"))) == 1
    result = archive_retained(repo.repo, **args, plan_id=plan_id)
    assert result["decision"]["code"] == "ARCHIVED"
    assert doctor(repo.repo)["decision"]["code"] == "PASS"


def test_archived_retained_checkout_uses_existing_registered_venv_teardown(monkeypatch):
    monkeypatch.setattr(
        lifecycle,
        "_typed_runtime_evidence",
        lambda *_args: {
            "managed_references": [],
            "observed_references": [],
            "managed_visibility_uncertainties": [],
            "scan_uncertainties": [],
        },
    )
    with GitRepository(linked=0, policy_text=POLICY_V5) as repo:
        _commit_reproducible_project(repo)
        adopt_canonical(repo)
        path, _allocation = acquire_one(repo, task="unfinished-venv")
        _create_reproducible_venv(path)
        lifecycle.register_disposable(
            repo.repo,
            target=path,
            expected_record_version=1,
            owner="module-owner",
            resource_path=".venv",
            kind="python-venv",
            reproducible=True,
            creator="test-fixture",
            teardown_policy="on-release",
        )
        (path / "result.txt").write_text("committed but unfinished\n")
        git(path, "add", "result.txt")
        git(path, "commit", "-m", "unfinished result")
        head = git(path, "rev-parse", "HEAD")
        branch = git(path, "branch", "--show-current")
        lifecycle.park(repo.repo, target=path, expected_record_version=2, owner="module-owner")
        args = dict(
            target=path,
            expected_record_version=3,
            owner_release="module-owner",
            actor="governance",
        )
        plan_id = archive_retained(repo.repo, **args, dry_run=True)["decision"]["plan_id"]
        archive_retained(repo.repo, **args, plan_id=plan_id)
        gc = gc_plan(repo.repo, target=path, owner_release="module-owner")
        assert gc["decision"]["code"] == "GC_PREFLIGHT_READY"
        released = gc_apply(
            repo.repo,
            target=path,
            owner_release="module-owner",
            expected_gc_plan_id=gc["decision"]["gc_plan_id"],
        )
        assert released["decision"]["code"] == "RELEASED"
        assert not path.exists()
        assert git(repo.repo, "rev-parse", branch) == head
        assert doctor(repo.repo)["decision"]["code"] == "PASS"


def test_archived_task_can_return_to_retained_without_losing_evidence(retained):
    repo, path, store, allocation_id, head, _branch, args = retained
    plan_id = archive_retained(repo.repo, **args, dry_run=True)["decision"]["plan_id"]
    archive_retained(repo.repo, **args, plan_id=plan_id)
    result = lifecycle.reopen(
        repo.repo, target=path, expected_record_version=3, owner="module-owner"
    )
    assert result["decision"]["lifecycle"] == "RETAINED"
    record = store.load().records[allocation_id]
    assert record.task_commit != head
    assert record.lifecycle == "RETAINED"
    assert doctor(repo.repo)["decision"]["code"] == "PASS"


typed_target = typed_fixtures.target


@pytest.fixture
def multiple_preserved(typed_target, tmp_path, monkeypatch, request):
    repository, target, allocation = typed_target
    store = MetadataStore(inventory(repository.repo).common_dir)
    version = 2
    monkeypatch.setattr(preservation, "evidence_root", lambda: tmp_path / "preservations")
    if getattr(request, "param", None) == "archived":
        with store.lock(exclusive=True):
            record = store._load_unlocked().records[allocation]
            store.replace_record(replace(record, role="feature"))
        (repository.repo / ".agents/worktree-policy.toml").write_text(POLICY_V5)
        git(repository.repo, "add", ".agents/worktree-policy.toml")
        git(repository.repo, "commit", "-m", "enable synthetic archived-source policy")
        lifecycle.reopen(
            repository.repo, target=target, expected_record_version=2, owner="module-owner"
        )
        tracked = target / "backend/pyproject.toml"
        tracked.write_bytes(tracked.read_bytes() + b"# unfinished source edit\n")
        source_plan = source_preservation.plan_preservation(
            repository.repo,
            target,
            allocation,
            "module-owner",
            3,
            expected_head=git(target, "rev-parse", "HEAD"),
        )
        source_copy = source_preservation.copy_preservation(source_plan)
        cleanup = source_cleanup.plan_cleanup(source_plan, source_copy["receipt_sha256"])
        source_cleanup.cleanup_source(cleanup)
        lifecycle.close_preserved(
            repository.repo, target=target, expected_record_version=3, owner="module-owner"
        )
        version = 4
    for relative in (".venv", "backend/.venv"):
        state = target / relative / "agent-workspace"
        state.mkdir(parents=True)
        (state / "same-name").write_bytes(b"synthetic unique legacy state")
    os.link(
        target / ".venv/agent-workspace/same-name",
        target / "backend/.venv/agent-workspace/cross-link",
    )
    copies = []
    for relative in (".venv", "backend/.venv"):
        plan = preservation.plan_preservation(
            repository.repo, target, allocation, "module-owner", version, source_relative=relative
        )
        copies.append(preservation.copy_preservation(plan))
    args = dict(
        target=target,
        expected_record_version=version,
        owner="module-owner",
        preservation_receipt=Path(copies[0]["receipt"]),
        preservation_receipt_sha256=copies[0]["receipt_sha256"],
        additional_preservation_receipts=(Path(copies[1]["receipt"]),),
        additional_preservation_receipt_sha256=(copies[1]["receipt_sha256"],),
    )
    yield repository, target, allocation, store, copies, args


def _multiple_plan(fixture, **changes):
    repository, _, _, _, _, args = fixture
    return lifecycle.cleanup_typed_disposable(repository.repo, **{**args, **changes}, dry_run=True)[
        "decision"
    ]["plan"]


def _multiple_apply(fixture, cleanup_plan):
    repository, _, _, _, _, args = fixture
    return lifecycle.cleanup_typed_disposable(
        repository.repo, **args, dry_run=False, expected_plan_id=cleanup_plan["plan_id"]
    )["decision"]


@pytest.mark.parametrize("multiple_preserved", ["handoff", "archived"], indirect=True)
def test_multiple_preservation_cleanup_and_release_keep_all_generations(multiple_preserved):
    repository, target, allocation, store, copies, args = multiple_preserved
    before = store.load().records[allocation]
    original = {c["receipt"]: Path(c["receipt"]).read_bytes() for c in copies}
    plan = _multiple_plan(multiple_preserved)
    assert plan["blockers"] == []
    assert plan["contract"] == "legacy-preserved-state-multiple-cleanup/v1"
    assert [r["source_relative"] for r in plan["preservation_evidences"]] == [
        ".venv",
        "backend/.venv",
    ]
    assert plan["preservation_evidence"] == plan["preservation_evidences"][0]["evidence"]
    assert all(
        next(r for r in plan["resources"] if r["path"] == source)["classification"]
        == "exact-preserved"
        for source in (".venv", "backend/.venv")
    )
    swapped = _multiple_plan(
        multiple_preserved,
        preservation_receipt=Path(copies[1]["receipt"]),
        preservation_receipt_sha256=copies[1]["receipt_sha256"],
        additional_preservation_receipts=(Path(copies[0]["receipt"]),),
        additional_preservation_receipt_sha256=(copies[0]["receipt_sha256"],),
    )
    assert swapped == plan
    result = _multiple_apply(multiple_preserved, plan)
    assert result["code"] == "PRESERVED_STATE_CLEANED"
    receipt = store._read_json(store.completion_receipts_dir / f"{result['receipt_id']}.json")
    assert receipt["plan"] == json.loads(json.dumps(plan))
    assert all(a["status"] == "COMPLETED" for a in receipt["actions"])
    assert receipt["post_scan"]["blockers"] == receipt["post_scan"]["cleanup_paths"] == []
    assert store.load().records[allocation] == before
    assert not any(os.path.lexists(target / relative) for relative in plan["cleanup_paths"])
    assert {name: Path(name).read_bytes() for name in original} == original
    branch = git(target, "branch", "--show-current")
    lifecycle.release(
        repository.repo,
        target=target,
        expected_record_version=args["expected_record_version"],
        owner_release="module-owner",
    )
    assert not target.exists()
    assert git(repository.repo, "rev-parse", branch) == before.base_commit
    assert list(store.transactions_dir.glob("*.json")) == []
    assert doctor(repository.repo)["decision"]["code"] == "PASS"
    for copied in copies:
        preserved_cleanup.coverage(
            Path(copied["receipt"]), copied["receipt_sha256"], verify_source=False
        )


@pytest.mark.parametrize(
    "invalid",
    [
        "missing-primary",
        "missing-primary-sha",
        "count",
        "duplicate",
        "bad-sha",
        "source-change",
        "foreign-receipt",
        "writer-lease",
        "unknown",
        "binding",
        "owner",
        "cas",
    ],
)
def test_multiple_preservation_rejects_any_invalid_input_and_global_guard(
    multiple_preserved, invalid
):
    _, target, allocation, store, copies, args = multiple_preserved
    changes = {}
    if invalid == "missing-primary":
        changes["preservation_receipt"] = None
    elif invalid == "missing-primary-sha":
        changes["preservation_receipt_sha256"] = None
    elif invalid == "count":
        changes["additional_preservation_receipt_sha256"] = ()
    elif invalid == "duplicate":
        changes.update(
            additional_preservation_receipts=(Path(copies[0]["receipt"]),),
            additional_preservation_receipt_sha256=(copies[0]["receipt_sha256"],),
        )
    elif invalid == "bad-sha":
        changes["additional_preservation_receipt_sha256"] = ("0" * 64,)
    elif invalid == "foreign-receipt":
        receipt_path = Path(copies[1]["receipt"])
        receipt = json.loads(receipt_path.read_bytes())
        receipt["allocation_source_identity"]["allocation_id"] = str(uuid4())
        raw = preservation._json_bytes(receipt)
        receipt_path.chmod(0o600)
        receipt_path.write_bytes(raw)
        changes["additional_preservation_receipt_sha256"] = (hashlib.sha256(raw).hexdigest(),)
    elif invalid == "writer-lease":
        timestamp = datetime.now(UTC).isoformat()
        with store.lock(exclusive=True):
            store.write_writer_lease(
                WriterLease(
                    schema_version=2,
                    lease_id=str(uuid4()),
                    repository_id=store._load_unlocked().repository_id,
                    allocation_id=allocation,
                    worktree_path=str(target),
                    owner="module-owner",
                    holder=WriterHolder(
                        client_id="test",
                        pid=12345,
                        boot_id=str(uuid4()),
                        start_ticks=1,
                        session_id=None,
                        run_id="synthetic",
                    ),
                    acquired_at=timestamp,
                    renewed_at=timestamp,
                    expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    record_version=args["expected_record_version"],
                )
            )
    elif invalid == "source-change":
        (target / "backend/.venv/agent-workspace/same-name").write_bytes(b"new source bytes")
    elif invalid == "unknown":
        (target / "unknown").mkdir()
        (target / "unknown/data").write_bytes(b"must survive")
    elif invalid == "binding":
        with store.lock(exclusive=True):
            record = store._load_unlocked().records[allocation]
            store.replace_record(replace(record, external_bindings=("synthetic-runtime",)))
    elif invalid == "owner":
        changes["owner"] = "foreign-owner"
    else:
        changes["expected_record_version"] = args["expected_record_version"] + 1
    before = {str(p): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    result = _multiple_plan(multiple_preserved, **changes)
    assert result["blockers"] and result["cleanup_paths"] == []
    assert before == {str(p): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    assert not list(store.transactions_dir.glob("*.json"))


@pytest.mark.parametrize("failure", ["before-start", "after-start"])
def test_multiple_preservation_partial_failure_keeps_each_proof_and_blocks_recovery(
    multiple_preserved, monkeypatch, failure
):
    repository, target, allocation, store, copies, _args = multiple_preserved
    plan = _multiple_plan(multiple_preserved)
    original = lifecycle.delete_preserved_state
    calls = []

    def delete(root, evidence):
        calls.append(evidence["receipt_path"])
        if evidence["receipt_path"] == copies[1]["receipt"]:
            raise OSError(5, "synthetic second-source failure")
        result = original(root, evidence)
        return result

    if failure == "before-start":
        original_planner = lifecycle._typed_cleanup_plan_locked
        changed = False

        def planner(*args, **kwargs):
            nonlocal changed
            if (
                not changed
                and not (target / ".venv").exists()
                and (target / "backend/.venv").exists()
            ):
                changed = True
                (target / "backend/.venv/agent-workspace/same-name").write_bytes(
                    b"changed before next action"
                )
            return original_planner(*args, **kwargs)

        monkeypatch.setattr(lifecycle, "_typed_cleanup_plan_locked", planner)
    monkeypatch.setattr(lifecycle, "delete_preserved_state", delete)
    with pytest.raises(ControllerError):
        _multiple_apply(multiple_preserved, plan)
    assert not (target / ".venv").exists()
    assert (target / "backend/.venv").exists()
    (journal_path,) = store.transactions_dir.glob("*.json")
    journal_bytes = journal_path.read_bytes()
    receipt_path = store.completion_receipts_dir / journal_path.name
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    assert receipt["result"] == "FAILED_OR_PARTIAL"
    assert receipt["plan"] == json.loads(json.dumps(plan))
    actions = {a["path"]: a["status"] for a in receipt["actions"]}
    assert actions[".venv"] == "COMPLETED"
    assert actions["backend/.venv"] == (
        "PENDING" if failure == "before-start" else "FAILED_OR_PARTIAL"
    )
    assert _multiple_plan(multiple_preserved)["blockers"]
    from worktree_controller import cleanup_recovery

    with pytest.raises(ControllerError) as blocked:
        cleanup_recovery.rollout_check(repository.repo)
    assert blocked.value.code == "MULTIPLE_CLEANUP_RECOVERY_BLOCKED"
    assert journal_path.read_bytes() == journal_bytes
    assert receipt_path.read_bytes() == receipt_bytes
    assert len(calls) == (1 if failure == "before-start" else 2)
    assert store.load().records[allocation].lifecycle == "HANDOFF_READY"
    assert all(Path(c["receipt"]).exists() for c in copies)


@pytest.mark.parametrize("drift", ["secondary-generation", "secondary-source", "proof-list"])
def test_multiple_preservation_release_rechecks_every_proof(multiple_preserved, drift):
    repository, target, allocation, store, copies, args = multiple_preserved
    result = _multiple_apply(multiple_preserved, _multiple_plan(multiple_preserved))
    if drift == "secondary-generation":
        generation_receipt = Path(copies[1]["receipt"])
        generation_receipt.chmod(0o600)
        generation_receipt.write_bytes(generation_receipt.read_bytes() + b" ")
    elif drift == "secondary-source":
        (target / "backend/.venv").mkdir()
    else:
        receipt_path = store.completion_receipts_dir / f"{result['receipt_id']}.json"
        receipt = store._read_json(receipt_path)
        receipt["plan"]["preservation_evidences"] = []
        receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ControllerError) as blocked:
        lifecycle.release(
            repository.repo,
            target=target,
            expected_record_version=args["expected_record_version"],
            owner_release="module-owner",
            dry_run=True,
        )
    assert blocked.value.code == "PRESERVATION_COVERAGE_INVALID"
    assert target.exists() and store.load().records[allocation].lifecycle == "HANDOFF_READY"


@pytest.mark.parametrize("relative", [".venv", ".venv/agent-workspace"])
def test_multiple_preservation_rejects_source_overlap(multiple_preserved, relative):
    repository, target, allocation, _, _, args = multiple_preserved
    plan = preservation.plan_preservation(
        repository.repo,
        target,
        allocation,
        "module-owner",
        args["expected_record_version"],
        source_relative=relative,
    )
    copied = preservation.copy_preservation(plan)
    blocked = _multiple_plan(
        multiple_preserved,
        additional_preservation_receipts=(Path(copied["receipt"]),),
        additional_preservation_receipt_sha256=(copied["receipt_sha256"],),
    )
    assert blocked["cleanup_paths"] == []
    assert any(b["code"] == "PRESERVATION_COVERAGE_INVALID" for b in blocked["blockers"])


def test_multiple_preservation_release_requires_proof_for_every_preserved_action(
    multiple_preserved,
):
    repository, target, _, store, _, _ = multiple_preserved
    result = _multiple_apply(multiple_preserved, _multiple_plan(multiple_preserved))
    receipt = store._read_json(store.completion_receipts_dir / f"{result['receipt_id']}.json")
    plan = receipt["plan"]
    extra = {
        **next(r for r in plan["resources"] if r["path"] == "backend/.venv"),
        "path": "extra/.venv",
    }
    plan["resources"].append(extra)
    plan["cleanup_paths"].append("extra/.venv")
    receipt["actions"].append({"path": "extra/.venv", "status": "COMPLETED"})
    receipt["action_evidence"].append({"resource": "extra/.venv", "before": {}, "after": {}})
    plan["plan_id"] = lifecycle._release_plan_id({k: v for k, v in plan.items() if k != "plan_id"})
    item = next(item for item in inventory(repository.repo).worktrees if item.path == target)
    with pytest.raises(ControllerError) as blocked:
        lifecycle._validate_multiple_preservation_cleanup(
            receipt, item, plan["preservation_evidence"]["allocation_source_identity"]
        )
    assert blocked.value.code == "PRESERVATION_COVERAGE_INVALID"


@pytest.fixture
def legacy_interrupted(typed_target, tmp_path, monkeypatch):
    return recovery_fixtures.interrupted.__wrapped__(typed_target, tmp_path, monkeypatch)


def test_multi_contract_is_rejected_by_unchanged_legacy_recovery(legacy_interrupted):
    from worktree_controller import cleanup_recovery

    repository, target, allocation, store, context, _old, _old_bytes, _outside = legacy_interrupted
    # The exact old four-action shape is supported before changing only its contract tag.
    single, _ = cleanup_recovery.original(store, context, target=target, allocation_id=allocation)
    assert single["contract"] == "legacy-preserved-state-cleanup/v1"
    baseline = recovery_fixtures.preserve(legacy_interrupted)
    accepted = cleanup_recovery.rollout_check(
        repository.repo,
        receipt_path=Path(baseline["receipt"]),
        receipt_sha=baseline["receipt_sha256"],
    )
    assert accepted["decision"]["transactions"][0]["state"] == "RECOVERABLE_PARTIAL"
    transaction = context["original_transaction_id"]
    journal_path = store.transactions_dir / f"{transaction}.json"
    receipt_path = store.completion_receipts_dir / f"{transaction}.json"
    journal, receipt = store._read_json(journal_path), store._read_json(receipt_path)
    receipt["contract"] = receipt["plan"]["contract"] = (
        lifecycle.MULTIPLE_PRESERVED_CLEANUP_CONTRACT
    )
    receipt["plan"]["plan_id"] = lifecycle._release_plan_id(
        {k: v for k, v in receipt["plan"].items() if k != "plan_id"}
    )
    journal["cleanup_plan"] = receipt["plan"]
    journal_path.write_bytes(preservation._json_bytes(journal))
    receipt_path.write_bytes(preservation._json_bytes(receipt))
    changed = cleanup_recovery.context(
        transaction,
        hashlib.sha256(journal_path.read_bytes()).hexdigest(),
        hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
    )
    before = {
        path: path.read_bytes() for path in (journal_path, receipt_path, Path(baseline["receipt"]))
    }
    with pytest.raises(ControllerError, match="supported exact completed/pending shape") as blocked:
        cleanup_recovery.original(store, changed, target=target, allocation_id=allocation)
    assert blocked.value.code == "RE_PRESERVATION_RECOVERY_BLOCKED"
    with pytest.raises(ControllerError) as replanning:
        preservation.plan_preservation(
            repository.repo, target, allocation, "module-owner", 2, recovery=changed
        )
    assert replanning.value.code == "RE_PRESERVATION_RECOVERY_BLOCKED"
    assert all(path.read_bytes() == raw for path, raw in before.items())
