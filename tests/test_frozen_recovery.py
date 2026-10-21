from __future__ import annotations

import json
import os
from dataclasses import replace

import pytest

from tests.test_controlled import _configure_remote, adopt_canonical, handoff_one
from tests.test_integration_pipeline import (
    POLICY_THREE_CANDIDATES,
    _claim,
    _enqueue,
    _publish,
    _source,
    _validate,
)
from tests.test_shadow import GitRepository, git
from worktree_controller import frozen_recovery, lifecycle
from worktree_controller.frozen_recovery import recover_frozen_checkout
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import release, reopen, review
from worktree_controller.models import ControllerError, WriterLease
from worktree_controller.store import WRITER_LEASE_SCHEMA_VERSION, MetadataStore, now_iso
from worktree_controller.writer_lease import describe_holder


@pytest.fixture
def published(monkeypatch):
    def runtime_evidence(*args, **kwargs):
        return {
            key: []
            for key in (
                "managed_references",
                "observed_references",
                "managed_visibility_uncertainties",
                "host_visibility_gaps",
                "scan_uncertainties",
            )
        }

    monkeypatch.setattr(lifecycle, "runtime_authority_evidence", runtime_evidence)
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        source, allocation, frozen = _source(
            repository, task="source", relative_path="source.txt", content="published\n"
        )
        intent = _enqueue(repository, task="integration", source_allocation=allocation)
        candidate, _ = _claim(
            repository, task="integration", intent_id=intent["decision"]["intent_id"]
        )
        _validate(repository, candidate, "test")
        _publish(repository, candidate, auto_release=False)
        store = MetadataStore(inventory(repository.repo).common_dir)
        source_record = store.load().records[allocation]
        candidate_record = next(r for r in store.load().records.values() if r.role == "integration")
        yield repository, source, candidate, store, source_record, candidate_record, frozen


def _advance(published):
    repository, source, _candidate, _store, *_ = published
    git(repository.repo, "merge", "--ff-only", "origin/main")
    (repository.repo / "main.txt").write_text("new main\n")
    git(repository.repo, "add", "main.txt")
    git(repository.repo, "commit", "-m", "advance main")
    git(repository.repo, "push", "origin", "main")
    git(source, "merge", "--ff-only", "origin/main")
    (source / "source.txt").write_text("uncommitted source\n")
    (source / "new.txt").write_text("untracked source\n")


def _recover(published, *, integration=False, **overrides):
    repository, source, candidate, _store, source_record, candidate_record, _ = published
    target, record = (candidate, candidate_record) if integration else (source, source_record)
    args = dict(
        target=target,
        expected_record_version=record.record_version,
        expected_head=git(target, "rev-parse", "HEAD"),
        owner=record.owner,
    )
    if integration:
        args["branch"] = "recovery/published"
    return recover_frozen_checkout(repository.repo, **(args | overrides))


def _lease(store, target, record, *, dead):
    holder = describe_holder(os.getpid(), client_id="pytest")
    if dead:
        holder = replace(holder, boot_id="old-test-boot")
    lease = WriterLease(
        WRITER_LEASE_SCHEMA_VERSION,
        "d94c1d98-7923-4532-a549-dc2c323b6c05",
        store.load().repository_id,
        record.allocation_id,
        str(target),
        record.owner,
        holder,
        now_iso(),
        now_iso(),
        None,
        record.record_version,
    )
    with store.lock(exclusive=True):
        store.write_writer_lease(lease)
    return lease


def test_source_recovery_preserves_dirty_untracked_frozen_evidence_and_clears_dead_lease(published):
    repository, source, _candidate, store, original, _record, frozen = published
    _advance(published)
    lease = _lease(store, source, original, dead=True)
    before = {p.name: p.read_bytes() for p in store.publication_receipts_dir.glob("*.json")}
    index = next(w.git_dir / "index" for w in inventory(source).worktrees if w.path == source)
    index_before = index.read_bytes()
    result = _recover(published)
    record = store.load().records[original.allocation_id]
    receipt = json.loads(
        (store.completion_receipts_dir / f"{result['decision']['receipt_id']}.json").read_text()
    )
    assert record.lifecycle == "ACTIVE"
    assert record.handoff_commit is None
    assert record.base_commit == git(source, "rev-parse", "HEAD")
    assert receipt["record"]["handoff_commit"] == frozen
    assert receipt["writer_lease"]["lease_id"] == lease.lease_id
    assert original.allocation_id not in store.load().writer_leases
    assert (source / "source.txt").read_text() == "uncommitted source\n"
    assert (source / "new.txt").read_text() == "untracked source\n"
    assert index.read_bytes() == index_before
    assert before == {p.name: p.read_bytes() for p in store.publication_receipts_dir.glob("*.json")}
    assert record.history[-1]["recovery_receipt_id"] == result["decision"]["receipt_id"]
    assert git(source, "merge-base", "--is-ancestor", frozen, record.base_commit) == ""
    with pytest.raises(ControllerError) as error:
        release(
            repository.repo,
            target=source,
            expected_record_version=record.record_version,
            owner_release=record.owner,
        )
    assert error.value.code == "DIRTY_WORKTREE"
    git(source, "add", "source.txt", "new.txt")
    git(source, "commit", "-m", "preserve recovered work")
    reviewed = review(
        repository.repo,
        target=source,
        expected_record_version=record.record_version,
        owner=record.owner,
        add_scope_paths=("new.txt",),
        expected_head=git(source, "rev-parse", "HEAD"),
        scope_reason="preserve recovered file",
    )
    preserved = handoff_one(
        repository, source, kind="preserved", expected=reviewed["decision"]["record_version"]
    )
    release(
        repository.repo,
        target=source,
        expected_record_version=preserved["decision"]["record_version"],
        owner_release=record.owner,
    )
    assert not source.exists()
    assert git(repository.repo, "show", "feature/source:new.txt") == "untracked source"
    assert before == {p.name: p.read_bytes() for p in store.publication_receipts_dir.glob("*.json")}


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("managed_references", "MANAGED_REFERENCE"),
        ("observed_references", "RUNTIME_REFERENCE"),
        ("managed_visibility_uncertainties", "RUNTIME_SCAN_UNCERTAIN"),
        ("scan_uncertainties", "RUNTIME_SCAN_UNCERTAIN"),
    ],
)
def test_runtime_reference_or_uncertainty_refuses_recovery(published, monkeypatch, field, code):
    _advance(published)

    def evidence(*args):
        result = {
            key: []
            for key in (
                "managed_references",
                "observed_references",
                "managed_visibility_uncertainties",
                "scan_uncertainties",
            )
        }
        result[field] = [{"kind": "cwd", "pid": 42}]
        return result

    monkeypatch.setattr(frozen_recovery, "_typed_runtime_evidence", evidence)
    with pytest.raises(ControllerError) as error:
        _recover(published)
    assert error.value.code == code


def test_source_retry_after_dead_lease_clear_preserves_original_receipt(published, monkeypatch):
    _repository, source, _candidate, store, original, *_ = published
    _advance(published)
    _lease(store, source, original, dead=True)
    real_remove = MetadataStore.remove_writer_lease

    def remove(self, allocation):
        real_remove(self, allocation)
        raise RuntimeError("interrupted after lease clearance")

    with monkeypatch.context() as patch:
        patch.setattr(MetadataStore, "remove_writer_lease", remove)
        with pytest.raises(RuntimeError, match="interrupted"):
            _recover(published)
    paths = tuple(store.completion_receipts_dir.glob("*.json"))
    assert len(paths) == 1
    original_receipt = paths[0].read_bytes()
    _recover(published)
    assert paths[0].read_bytes() == original_receipt
    assert store.load().records[original.allocation_id].lifecycle == "ACTIVE"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("owner", "OWNER_MISMATCH"),
        ("version", "CAS_MISMATCH"),
        ("head", "IDENTITY_MISMATCH"),
        ("unpublished", "UNPUBLISHED_HEAD"),
        ("staged", "BLOCK_DRIFT"),
        ("live", "LIVE_WRITER"),
        ("binding", "MANAGED_REFERENCE"),
        ("receipt", "MISSING_PUBLICATION_EVIDENCE"),
        ("frozen", "INVALID_FROZEN_SOURCE"),
        ("active_intent", "ACTIVE_INTEGRATION"),
    ],
)
def test_source_refusal_boundaries(published, change, code):
    _repository, source, _candidate, store, record, *_ = published
    _advance(published)
    overrides = {}
    if change == "owner":
        overrides["owner"] = "someone-else"
    elif change == "version":
        overrides["expected_record_version"] = 99
    elif change == "head":
        overrides["expected_head"] = "a" * 40
    elif change == "unpublished":
        git(source, "commit", "--allow-empty", "-m", "not published")
    elif change == "staged":
        git(source, "add", "source.txt")
    elif change == "live":
        _lease(store, source, record, dead=False)
    elif change == "binding":
        with store.lock(exclusive=True):
            store.replace_record(replace(record, external_bindings=("test-service",)))
    elif change == "receipt":
        for p in store.publication_receipts_dir.glob("*.json"):
            p.unlink()
    elif change == "frozen":
        with store.lock(exclusive=True):
            store.replace_record(replace(record, task_commit="a" * 40))
    elif change == "active_intent":
        with store.lock(exclusive=True):
            intent = next(iter(store._load_unlocked().integration_intents.values()))
            store.replace_integration_intent(replace(intent, state="READY_TO_PUBLISH"))
    before = store.load().records[record.allocation_id]
    with pytest.raises(ControllerError) as error:
        _recover(published, **overrides)
    assert error.value.code == code
    assert store.load().records[record.allocation_id] == before
    assert not tuple(store.completion_receipts_dir.glob("*.json"))
    assert (source / "new.txt").read_text() == "untracked source\n"


def test_normal_reopen_still_refuses_drift(published):
    repository, source, _candidate, _store, record, *_ = published
    _advance(published)
    with pytest.raises(ControllerError) as error:
        reopen(
            repository.repo,
            target=source,
            expected_record_version=record.record_version,
            owner=record.owner,
        )
    assert error.value.code == "DIRTY_WORKTREE"


def test_published_detached_reattaches_then_uses_unchanged_release(published):
    repository, _source, candidate, store, _record, original, _ = published
    head = git(candidate, "rev-parse", "HEAD")
    git(candidate, "checkout", "--detach")
    with pytest.raises(ControllerError) as error:
        release(
            repository.repo,
            target=candidate,
            expected_record_version=original.record_version,
            owner_release=original.owner,
        )
    assert error.value.code == "DETACHED_WORKTREE"
    receipts = {p.name: p.read_bytes() for p in store.publication_receipts_dir.glob("*.json")}
    result = _recover(published, integration=True)
    assert git(candidate, "branch", "--show-current") == "recovery/published"
    assert git(candidate, "rev-parse", "HEAD") == head
    assert git(candidate, "status", "--porcelain") == ""
    assert receipts == {
        p.name: p.read_bytes() for p in store.publication_receipts_dir.glob("*.json")
    }
    released = release(
        repository.repo,
        target=candidate,
        expected_record_version=result["decision"]["record_version"],
        owner_release=original.owner,
    )
    assert released["decision"]["code"] == "RELEASED"
    assert not candidate.exists()
    assert git(repository.repo, "rev-parse", "refs/heads/recovery/published") == head


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("dirty", "INVALID_PUBLISHED_CHECKOUT"),
        ("attached", "INVALID_PUBLISHED_CHECKOUT"),
        ("branch", "BRANCH_EXISTS"),
        ("receipt", "MISSING_PUBLICATION_EVIDENCE"),
        ("unpublished", "INVALID_PUBLISHED_CHECKOUT"),
        ("live", "LIVE_WRITER"),
    ],
)
def test_published_refusal_boundaries(published, change, code):
    _repository, _source, candidate, store, _record, original, _ = published
    if change != "attached":
        git(candidate, "checkout", "--detach")
    if change == "dirty":
        (candidate / "untracked.txt").write_text("keep\n")
    elif change == "branch":
        git(candidate, "branch", "recovery/published")
    elif change == "receipt":
        for p in store.publication_receipts_dir.glob("*.json"):
            p.unlink()
    elif change == "unpublished":
        with store.lock(exclusive=True):
            store.replace_record(replace(original, lifecycle="INTEGRATING"))
    elif change == "live":
        _lease(store, candidate, original, dead=False)
    with pytest.raises(ControllerError) as error:
        _recover(published, integration=True)
    assert error.value.code == code
    assert not tuple(store.completion_receipts_dir.glob("*.json"))


@pytest.mark.parametrize("phase", ["receipt", "ref", "attach", "metadata"])
def test_interrupted_reattachment_retries_exact_preparation(published, monkeypatch, phase):
    _repository, _source, candidate, store, _record, original, _ = published
    git(candidate, "checkout", "--detach")
    real_write = frozen_recovery.git_write
    real_receipt = MetadataStore.write_completion_receipt
    real_record = MetadataStore.replace_record

    def write(repo, args):
        result = real_write(repo, args)
        if (phase == "ref" and args[0] == "update-ref") or (
            phase == "attach" and args[0] == "symbolic-ref"
        ):
            raise RuntimeError("simulated interruption")
        return result

    def receipt(self, *args):
        real_receipt(self, *args)
        if phase == "receipt":
            raise RuntimeError("simulated interruption")

    def record(self, value):
        if phase == "metadata" and value.allocation_id == original.allocation_id:
            raise RuntimeError("simulated interruption")
        real_record(self, value)

    with monkeypatch.context() as patch:
        patch.setattr(frozen_recovery, "git_write", write)
        patch.setattr(MetadataStore, "write_completion_receipt", receipt)
        patch.setattr(MetadataStore, "replace_record", record)
        with pytest.raises(RuntimeError, match="simulated"):
            _recover(published, integration=True)
    paths = tuple(store.completion_receipts_dir.glob("*.json"))
    assert len(paths) == 1
    evidence = paths[0].read_bytes()
    result = _recover(published, integration=True)
    assert result["decision"]["record_version"] == original.record_version + 1
    assert paths[0].read_bytes() == evidence
    assert git(candidate, "branch", "--show-current") == "recovery/published"
