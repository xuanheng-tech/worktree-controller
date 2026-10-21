"""An intent claimed once, aborted and retried can still resolve a later conflict."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.test_controlled import _configure_remote, adopt_canonical
from tests.test_integration_abort import POLICY_THREE_CANDIDATES_V5, _intent
from tests.test_integration_conflict import OWNER, merge, resolve, retry
from tests.test_integration_pipeline import _enqueue, _publish, _source, _validate
from tests.test_integration_retry import _abort
from tests.test_shadow import GitRepository, git
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import claim_integration
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore


def _retried_conflict(repository):
    """Claim normally, abort and release, then let main move so the retry conflicts."""
    adopt_canonical(repository)
    _configure_remote(repository)
    _, allocation, source = _source(
        repository, task="retried-source", relative_path="shared.txt", content="source\n"
    )
    intent_id = _enqueue(repository, task="retried", source_allocation=allocation)["decision"][
        "intent_id"
    ]
    first = claim_integration(repository.repo, owner=OWNER, intent_id=intent_id,
                              target=repository.root / "first-candidate")["decision"]
    assert first["code"] == "INTEGRATION_CANDIDATE_CLAIMED"
    _abort(repository, repository.root / "first-candidate", {"allocation_id": first["allocation_id"]})
    (repository.repo / "shared.txt").write_text("main\n")
    git(repository.repo, "add", "shared.txt")
    git(repository.repo, "commit", "-m", "main changes the same file")
    git(repository.repo, "push", "origin", "main")
    plan = retry(repository, intent_id, dry_run=True)
    retry(repository, intent_id, expected_plan_id=plan["plan_id"])
    blocked = claim_integration(repository.repo, owner=OWNER, intent_id=intent_id)["decision"]
    assert blocked["code"] == "INTEGRATION_CONFLICT_BLOCKED"
    return intent_id, source


def test_retried_intent_conflict_resolves_and_publishes():
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        intent_id, source = _retried_conflict(repository)
        before = _intent(repository, intent_id)
        plan = retry(repository, intent_id, dry_run=True)
        assert plan["plan"]["failure_evidence"]["kind"] == "unallocated_conflict"
        retry(repository, intent_id, expected_plan_id=plan["plan_id"])
        candidate = repository.root / "resolution"
        claimed = claim_integration(repository.repo, owner=OWNER, intent_id=intent_id,
                                    target=candidate, resolve_conflicts=True)["decision"]
        assert claimed["code"] == "INTEGRATION_CONFLICT_CLAIMED"
        after = _intent(repository, intent_id)
        assert (after.queue_sequence, after.source_handoffs, after.scope_digest) == (
            before.queue_sequence, before.source_handoffs, before.scope_digest)
        head = merge(repository, candidate, source)
        accepted = resolve(repository, candidate, head)["decision"]
        assert accepted["candidate_oid"] == head
        _validate(repository, candidate, "fresh", expected_record_version=2)
        published = _publish(repository, candidate, expected_record_version=3)
        assert published["decision"]["code"] == "PUBLICATION_RELEASED"
        assert git(repository.repo, "rev-parse", "origin/main") == head


@pytest.mark.parametrize("fault", ["claimed_without_abort", "claimed_in_current_attempt"])
def test_retried_conflict_still_fails_closed(fault):
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        intent_id, _ = _retried_conflict(repository)
        intent = _intent(repository, intent_id)
        if fault == "claimed_without_abort":
            history = tuple(e for e in intent.history if e.get("event") != "PREPARATION_ABORTED")
        else:
            history = (*intent.history[:-1], {"event": "CANDIDATE_CLAIMED"}, intent.history[-1])
        store = MetadataStore(inventory(repository.repo).common_dir)
        store.replace_integration_intent(replace(intent, history=history))
        with pytest.raises(ControllerError) as refused:
            retry(repository, intent_id, dry_run=True)
        assert refused.value.code == "RETRY_NOT_ELIGIBLE"
