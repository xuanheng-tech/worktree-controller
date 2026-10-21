"""Pre-allocation conflicts retain their intent and require a scoped, exact merge."""

import subprocess
from dataclasses import replace

import pytest

from tests.test_controlled import _configure_remote, adopt_canonical
from tests.test_integration_abort import POLICY_THREE_CANDIDATES_V5, _intent
from tests.test_integration_pipeline import _enqueue, _publish, _source, _validate
from tests.test_integration_retry import _abort
from tests.test_shadow import GitRepository, git
from worktree_controller.controller import doctor
from worktree_controller.git_backend import inventory
from worktree_controller.integration_conflict import resolve_integration
from worktree_controller.integration_retry import retry_integration
from worktree_controller.lifecycle import claim_integration
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore

OWNER = "integration-owner"


def failed(repository):
    adopt_canonical(repository)
    _configure_remote(repository)
    _, allocation, source = _source(
        repository,
        task="conflict-source",
        relative_path="shared.txt",
        content="source\n",
    )
    (repository.repo / "shared.txt").write_text("main\n")
    git(repository.repo, "add", "shared.txt")
    git(repository.repo, "commit", "-m", "main changes same file")
    git(repository.repo, "push", "origin", "main")
    intent = _enqueue(repository, task="conflict", source_allocation=allocation)["decision"]
    result = claim_integration(repository.repo, owner=OWNER, intent_id=intent["intent_id"])
    assert result["decision"]["code"] == "INTEGRATION_CONFLICT_BLOCKED"
    return intent["intent_id"], source


def retry(repository, intent_id, **kwargs):
    return retry_integration(
        repository.repo,
        intent_id=intent_id,
        owner=OWNER,
        reason="explicit owner resolution",
        **kwargs,
    )["decision"]


def claim_resolution(repository, intent_id):
    plan = retry(repository, intent_id, dry_run=True)
    retry(repository, intent_id, expected_plan_id=plan["plan_id"])
    target = repository.root / "resolution"
    claimed = claim_integration(
        repository.repo, owner=OWNER, intent_id=intent_id, target=target, resolve_conflicts=True
    )["decision"]
    assert claimed["code"] == "INTEGRATION_CONFLICT_CLAIMED"
    return target


def merge(repository, target, source, *, foreign=False):
    result = subprocess.run(
        ["git", "-C", str(target), "merge", "--no-ff", "--no-commit", source],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    (target / "shared.txt").write_text("owner resolved\n")
    git(target, "add", "shared.txt")
    if foreign:
        (target / "foreign.txt").write_text("outside frozen scope\n")
        git(target, "add", "foreign.txt")
    git(target, "commit", "-m", "resolve exact frozen handoff")
    return git(target, "rev-parse", "HEAD")


def resolve(repository, target, head, **kwargs):
    return resolve_integration(
        repository.repo,
        target=target,
        expected_record_version=1,
        owner=OWNER,
        expected_head=head,
        finalizer_record_id="fixture:merge-finalizer",
        **kwargs,
    )


def test_unallocated_retry_and_resolved_merge_complete_normal_publication():
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        intent_id, source = failed(repository)
        before = _intent(repository, intent_id)
        assert not any(
            w["code"] == "INTEGRATION_INTENT_DRIFT" for w in doctor(repository.repo)["warnings"]
        )
        plan = retry(repository, intent_id, dry_run=True)
        assert plan["plan"]["failure_evidence"]["kind"] == "unallocated_conflict"
        assert before == _intent(repository, intent_id)
        candidate = claim_resolution(repository, intent_id)
        claimed = _intent(repository, intent_id)
        assert claimed.queue_sequence == before.queue_sequence
        assert claimed.source_handoffs == before.source_handoffs
        assert claimed.scope_digest == before.scope_digest
        assert git(candidate, "rev-parse", "HEAD") == claimed.base_main_oid
        with pytest.raises(ControllerError):
            _validate(repository, candidate, "premature")
        head = merge(repository, candidate, source)
        with pytest.raises(ControllerError):
            _validate(repository, candidate, "unbound")
        accepted = resolve(repository, candidate, head)["decision"]
        assert accepted["candidate_oid"] == head and accepted["candidate_revision"] == 2
        assert accepted["validation_evidence_id"] is None
        _validate(repository, candidate, "fresh-tests-and-snapshot", expected_record_version=2)
        published = _publish(repository, candidate, expected_record_version=3)
        assert published["decision"]["code"] == "PUBLICATION_RELEASED"
        assert _intent(repository, intent_id).state == "RELEASED"
        assert git(repository.repo, "rev-parse", "origin/main") == head


@pytest.mark.parametrize(
    "fault", ["missing_event", "candidate_history", "source_drift", "stale_plan"]
)
def test_unallocated_retry_fails_closed(fault):
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        intent_id, _ = failed(repository)
        intent = _intent(repository, intent_id)
        store = MetadataStore(inventory(repository.repo).common_dir)
        plan = retry(repository, intent_id, dry_run=True)
        if fault == "missing_event":
            store.replace_integration_intent(replace(intent, history=intent.history[:-1]))
        elif fault == "candidate_history":
            store.replace_integration_intent(replace(intent, revision=1))
        elif fault == "source_drift":
            source = store.load().records[intent.source_allocations[0]]
            store.replace_record(replace(source, handoff_commit=source.base_commit))
        else:
            store.replace_integration_intent(
                replace(intent, updated_at="2030-01-01T00:00:00+08:00")
            )
        with pytest.raises(ControllerError):
            retry(repository, intent_id, expected_plan_id=plan["plan_id"])


@pytest.mark.parametrize("fault", ["wrong_head", "dirty", "scope", "extra_commit"])
def test_resolution_rejects_unreviewed_head_or_scope(fault):
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        intent_id, source = failed(repository)
        candidate = claim_resolution(repository, intent_id)
        head = merge(repository, candidate, source, foreign=fault == "scope")
        if fault == "wrong_head":
            head = source
        elif fault == "dirty":
            (candidate / "shared.txt").write_text("still changing\n")
        elif fault == "extra_commit":
            (candidate / "shared.txt").write_text("novel commit\n")
            git(candidate, "add", "shared.txt")
            git(candidate, "commit", "-m", "not the claimed merge")
            head = git(candidate, "rev-parse", "HEAD")
        with pytest.raises(ControllerError):
            resolve(repository, candidate, head)
        assert (
            _intent(repository, intent_id).candidate_oid
            == _intent(repository, intent_id).base_main_oid
        )


def test_retry_keeps_scope_order_and_default_claim_still_refuses_conflicts():
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        intent_id, _ = failed(repository)
        original = _intent(repository, intent_id)
        later = _enqueue(
            repository, task="later", source_allocation=original.source_allocations[0]
        )["decision"]
        plan = retry(repository, intent_id, dry_run=True)
        retry(repository, intent_id, expected_plan_id=plan["plan_id"])
        assert retry(repository, intent_id, expected_plan_id=plan["plan_id"])["replay"]
        blocked = claim_integration(
            repository.repo, owner=OWNER, intent_id=later["intent_id"], resolve_conflicts=True
        )
        assert blocked["decision"]["code"] == "INTEGRATION_WAITING_SCOPE"
        refused = claim_integration(repository.repo, owner=OWNER, intent_id=intent_id)
        assert refused["decision"]["code"] == "INTEGRATION_CONFLICT_BLOCKED"


def test_unresolved_candidate_can_abort_release_and_retry_without_false_publication():
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        intent_id, _ = failed(repository)
        candidate = claim_resolution(repository, intent_id)
        claimed = _intent(repository, intent_id)
        _abort(repository, candidate, {"allocation_id": claimed.allocation_id})
        plan = retry(repository, intent_id, dry_run=True)
        retry(repository, intent_id, expected_plan_id=plan["plan_id"])
        result = claim_integration(
            repository.repo,
            owner=OWNER,
            intent_id=intent_id,
            target=repository.root / "resumed",
            resolve_conflicts=True,
        )["decision"]
        assert result["code"] == "INTEGRATION_CONFLICT_CLAIMED"
