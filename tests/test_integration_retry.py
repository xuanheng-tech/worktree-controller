"""Retry keeps immutable delivery evidence and never grants an unverified priority jump."""

from dataclasses import replace

import pytest

from tests.test_controlled import _configure_remote, acquire_one, adopt_canonical, handoff_one
from tests.test_integration_abort import (
    POLICY_THREE_CANDIDATES_V5,
    _intent,
    _prepared_candidate,
    _record,
)
from tests.test_integration_pipeline import _claim, _enqueue, _publish, _source, _validate
from tests.test_shadow import GitRepository, git
from worktree_controller.git_backend import changed_paths, inventory
from worktree_controller.integration_retry import retry_integration
from worktree_controller.lifecycle import abort_integration, claim_integration, release
from worktree_controller.models import ControllerError
from worktree_controller.semantics import integration_queue_order
from worktree_controller.store import MetadataStore

OWNER = "integration-owner"
REASON = "formal prerequisite delivered; retry original frozen source"


def _abort(repository, candidate, claimed, *, release_candidate=True):
    result = abort_integration(
        repository.repo,
        target=candidate,
        expected_record_version=_record(repository, claimed["allocation_id"]).record_version,
        owner=OWNER,
        reason="PATH_SCOPE_VIOLATION: prerequisite outside frozen scope",
    )["decision"]
    if release_candidate:
        release(
            repository.repo,
            target=candidate,
            expected_record_version=_record(repository, claimed["allocation_id"]).record_version,
            owner_release=OWNER,
        )
    return result


def _retry(repository, intent_id, **kwargs):
    return retry_integration(
        repository.repo, intent_id=intent_id, owner=OWNER, reason=REASON, **kwargs
    )["decision"]


def _failed(repository):
    candidate, claimed, source = _prepared_candidate(repository)
    abort = _abort(repository, candidate, claimed)
    return claimed["intent_id"], source, abort


def _dependency(repository, *, older_blocker=False):
    adopt_canonical(repository)
    _configure_remote(repository)
    _, prerequisite_source, head = _source(
        repository, task="research", relative_path="dependency.txt", content="required\n"
    )
    if older_blocker:
        _enqueue(repository, task="older", source_allocation=prerequisite_source)
    source, acquired = acquire_one(
        repository, task="index", authority="index", scope_paths=("index.txt",), base_ref=head
    )
    (source / "index.txt").write_text("index\n")
    git(source, "add", "index.txt")
    git(source, "commit", "-m", "index after research")
    handoff_one(repository, source)
    parent = _enqueue(
        repository, task="parent", source_allocation=acquired["decision"]["allocation_id"]
    )["decision"]
    candidate, claimed = _claim(repository, task="parent", intent_id=parent["intent_id"])
    _abort(repository, candidate, claimed["decision"])
    _enqueue(repository, task="later", source_allocation=prerequisite_source)
    child = _enqueue(repository, task="prerequisite", source_allocation=prerequisite_source)[
        "decision"
    ]
    return parent["intent_id"], child["intent_id"]


def test_retry_preserves_source_queue_failure_and_requires_fresh_validation():
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        intent_id, source, abort = _failed(repository)
        before = _intent(repository, intent_id)
        store = MetadataStore(inventory(repository.repo).common_dir)
        failure_bytes = (
            store.completion_receipts_dir / f"{abort['abort_receipt_id']}.json"
        ).read_bytes()
        plan = _retry(repository, intent_id, dry_run=True)
        assert _intent(repository, intent_id) == before
        applied = _retry(repository, intent_id, expected_plan_id=plan["plan_id"])
        after = _intent(repository, intent_id)
        assert applied["code"] == "INTEGRATION_RETRY_QUEUED"
        for field in (
            "intent_id",
            "scope_paths",
            "scope_digest",
            "queue_sequence",
            "enqueued_at",
            "source_allocations",
            "source_handoffs",
            "source_branches",
            "source_finalizer_record_ids",
            "source_snapshot_ids",
        ):
            assert getattr(after, field) == getattr(before, field)
        assert after.history[:-1] == before.history
        assert (
            after.history[-1]["retry_plan"]["previous_intent"]["base_main_oid"]
            == before.base_main_oid
        )
        assert (
            after.history[-1]["retry_plan"]["failure_evidence"]["failure_reason"]
            == before.blocked_reason
        )
        assert after.attempt == before.attempt + 1
        assert after.candidate_oid is after.validated_oid is after.validation_evidence_id is None
        assert (
            store.completion_receipts_dir / f"{abort['abort_receipt_id']}.json"
        ).read_bytes() == failure_bytes
        assert _record(repository, source).lifecycle == "HANDOFF_READY"
        replay = _retry(repository, intent_id, expected_plan_id=plan["plan_id"])
        assert replay["replay"] and _intent(repository, intent_id) == after
        claimed = claim_integration(
            repository.repo, owner=OWNER, intent_id=intent_id, target=repository.root / "retry"
        )
        assert claimed["decision"]["candidate_oid"] and claimed["decision"]["attempt"] == 2
        assert (
            git(repository.root / "retry", "branch", "--show-current")
            == "integration/int-task-retry-2"
        )
        assert _intent(repository, intent_id).validation_evidence_id is None


@pytest.mark.parametrize(
    "guard",
    [
        "owner",
        "no_plan",
        "missing_abort",
        "source_drift",
        "scope_drift",
        "stale_plan",
        "published",
        "abandoned",
    ],
)
def test_retry_rejects_invalid_authority_or_evidence(guard):
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        intent_id, source, abort = _failed(repository)
        store = MetadataStore(inventory(repository.repo).common_dir)
        before = _intent(repository, intent_id)
        plan = _retry(repository, intent_id, dry_run=True)
        if guard == "owner":
            with pytest.raises(ControllerError, match="owner"):
                retry_integration(
                    repository.repo,
                    intent_id=intent_id,
                    owner="someone-else",
                    reason=REASON,
                    dry_run=True,
                )
            return
        if guard == "no_plan":
            with pytest.raises(ControllerError, match="plan"):
                _retry(repository, intent_id)
            return
        if guard == "missing_abort":
            (store.completion_receipts_dir / f"{abort['abort_receipt_id']}.json").unlink()
        elif guard == "source_drift":
            record = _record(repository, source)
            store.replace_record(replace(record, handoff_commit=record.base_commit))
        elif guard == "scope_drift":
            store.replace_integration_intent(replace(before, scope_paths=("unrelated",)))
        elif guard == "stale_plan":
            store.replace_integration_intent(
                replace(before, updated_at="2030-01-01T00:00:00+08:00")
            )
        elif guard == "published":
            git(repository.repo, "push", "origin", f"{before.candidate_oid}:main")
        elif guard == "abandoned":
            from worktree_controller.lifecycle import abandon_integration_intent

            abandon_integration_intent(
                repository.repo, intent_id=intent_id, owner=OWNER, reason="withdrawn"
            )
        with pytest.raises(ControllerError):
            _retry(repository, intent_id, expected_plan_id=plan["plan_id"])


def test_retry_requires_failed_candidate_to_be_physically_released():
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        candidate, claimed, _ = _prepared_candidate(repository)
        _abort(repository, candidate, claimed, release_candidate=False)
        with pytest.raises(ControllerError):
            _retry(repository, claimed["intent_id"], dry_run=True)
        assert candidate.exists()


def test_verified_prerequisite_precedes_later_scope_then_original_retry_passes():
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        parent_id, child_id = _dependency(repository)
        before = _intent(repository, parent_id)
        child_before = _intent(repository, child_id)
        assert child_before.state == "WAITING_SCOPE"
        plan = _retry(repository, parent_id, prerequisite_intent_id=child_id, dry_run=True)
        applied = _retry(
            repository, parent_id, prerequisite_intent_id=child_id, expected_plan_id=plan["plan_id"]
        )
        assert applied["code"] == "INTEGRATION_PREREQUISITE_BOUND"
        parent = _intent(repository, parent_id)
        assert (
            parent.state == "ATTENTION_REQUIRED" and parent.blocked_reason == before.blocked_reason
        )
        assert parent.history[:-1] == before.history
        assert parent.queue_sequence == before.queue_sequence
        assert _intent(repository, child_id) == child_before
        snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
        assert integration_queue_order(snapshot, child_before) < integration_queue_order(
            snapshot, parent
        )
        with pytest.raises(ControllerError, match="prerequisite"):
            _retry(repository, parent_id, dry_run=True)
        child, claimed = _claim(repository, task="prerequisite", intent_id=child_id)
        assert claimed["decision"]["code"] == "INTEGRATION_CANDIDATE_CLAIMED"
        _validate(repository, child, "research-tests-and-review")
        _publish(repository, child)
        retry_plan = _retry(repository, parent_id, dry_run=True)
        _retry(repository, parent_id, expected_plan_id=retry_plan["plan_id"])
        target = repository.root / "parent-retry"
        claimed = claim_integration(
            repository.repo, owner=OWNER, intent_id=parent_id, target=target
        )
        assert claimed["decision"]["code"] == "INTEGRATION_CANDIDATE_CLAIMED"
        retried = _intent(repository, parent_id)
        assert changed_paths(repository.repo, retried.base_main_oid, retried.candidate_oid) == (
            "index.txt",
        )
        _validate(repository, target, "index-fresh-tests-and-review")
        _publish(repository, target)
        assert _intent(repository, parent_id).state == "RELEASED"
        assert _intent(repository, parent_id).history[: len(before.history)] == before.history


def test_prerequisite_does_not_bypass_an_earlier_unrelated_scope():
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        parent_id, child_id = _dependency(repository, older_blocker=True)
        plan = _retry(repository, parent_id, prerequisite_intent_id=child_id, dry_run=True)
        _retry(
            repository, parent_id, prerequisite_intent_id=child_id, expected_plan_id=plan["plan_id"]
        )
        _, claimed = _claim(repository, task="prerequisite", intent_id=child_id)
        assert claimed["decision"]["code"] == "INTEGRATION_WAITING_SCOPE"


def test_unrelated_handoff_cannot_become_retry_prerequisite():
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        parent_id, _, _ = _failed(repository)
        _, allocation, _ = _source(
            repository, task="unrelated", relative_path="unrelated.txt", content="other\n"
        )
        child = _enqueue(repository, task="unrelated-integration", source_allocation=allocation)[
            "decision"
        ]
        before = _intent(repository, parent_id)
        with pytest.raises(ControllerError, match="ancestor"):
            _retry(repository, parent_id, prerequisite_intent_id=child["intent_id"], dry_run=True)
        assert _intent(repository, parent_id) == before


def test_prerequisite_cannot_overtake_an_active_overlapping_candidate():
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        parent_id, child_id = _dependency(repository)
        snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
        later = next(i for i in snapshot.integration_intents.values() if i.task_key == "later")
        _, claimed = _claim(repository, task="later", intent_id=later.intent_id)
        assert claimed["decision"]["code"] == "INTEGRATION_CANDIDATE_CLAIMED"
        with pytest.raises(ControllerError, match="overtake"):
            _retry(repository, parent_id, prerequisite_intent_id=child_id, dry_run=True)
