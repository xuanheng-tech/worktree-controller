"""Terminal abort for a failed or owner-aborted integration preparation."""

from __future__ import annotations

import pytest

from tests.test_controlled import _configure_remote, acquire_one, adopt_canonical
from tests.test_integration_pipeline import (
    POLICY_THREE_CANDIDATES,
    _claim,
    _enqueue,
    _source,
)
from tests.test_shadow import GitRepository
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import abort_integration, register_disposable, release
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore

# The new terminal states require the bounded-retention policy schema, so this
# pipeline policy is the v4 fixture upgraded to v5 with an explicit retained pool.
POLICY_THREE_CANDIDATES_V5 = (
    POLICY_THREE_CANDIDATES.replace("schema_version = 4", "schema_version = 5")
    .replace("recovery_reserve = 1\n", "recovery_reserve = 1\nretained = 2\n")
    .replace("hard_limit = 9", "hard_limit = 11")
)


def _record(repository, allocation_id):
    return MetadataStore(inventory(repository.repo).common_dir).load().records[allocation_id]


def _intent(repository, intent_id):
    return (
        MetadataStore(inventory(repository.repo).common_dir).load().integration_intents[intent_id]
    )


def _prepared_candidate(repository):
    adopt_canonical(repository)
    _configure_remote(repository)
    _target, allocation, _oid = _source(
        repository, task="src-task", relative_path="src/one.txt", content="one\n"
    )
    intent = _enqueue(repository, task="int-task", source_allocation=allocation)["decision"]
    candidate, claimed = _claim(repository, task="int-task", intent_id=intent["intent_id"])
    return candidate, claimed["decision"], allocation


def test_failed_preparation_can_abort_and_release_its_integration_checkout() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        candidate, claimed, source_allocation = _prepared_candidate(repository)
        allocation = claimed["allocation_id"]
        intent_id = claimed["intent_id"]

        decision = abort_integration(
            repository.repo,
            target=candidate,
            expected_record_version=_record(repository, allocation).record_version,
            owner="integration-owner",
            reason="candidate changes paths outside the recorded task scope",
        )["decision"]
        assert decision["code"] == "INTEGRATION_ABORTED"
        assert decision["abort_receipt_id"]
        assert decision["failure_reason"].startswith("candidate changes paths")

        # the temporary integration checkout is now releasable
        released = release(
            repository.repo,
            target=candidate,
            expected_record_version=_record(repository, allocation).record_version,
            owner_release="integration-owner",
        )["decision"]
        assert released["code"] == "RELEASED"
        assert not candidate.exists()

        # the source allocation, its work and its handoff evidence are untouched
        source = _record(repository, source_allocation)
        assert source.lifecycle == "HANDOFF_READY"
        assert source.handoff_commit is not None
        assert source.finalizer_record_id is not None

        # provenance and failure reason survive on the intent
        intent = _intent(repository, intent_id)
        assert intent.state == "ATTENTION_REQUIRED"
        assert intent.blocked_reason == "candidate changes paths outside the recorded task scope"
        assert source_allocation in intent.source_allocations
        assert intent.source_handoffs


def test_abort_refuses_a_non_integration_allocation() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        adopt_canonical(repository)
        target, payload = acquire_one(repository, task="feature-one", authority="feature-one")
        allocation = payload["decision"]["allocation_id"]
        with pytest.raises(ControllerError) as error:
            abort_integration(
                repository.repo,
                target=target,
                expected_record_version=_record(repository, allocation).record_version,
                owner="module-owner",
                reason="not an integration allocation",
            )
        assert error.value.code == "INVALID_ROLE"


def test_abort_requires_owner_reason_and_record_version() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        candidate, claimed, _source_allocation = _prepared_candidate(repository)
        allocation = claimed["allocation_id"]
        version = _record(repository, allocation).record_version
        with pytest.raises(ControllerError) as owner_error:
            abort_integration(
                repository.repo,
                target=candidate,
                expected_record_version=version,
                owner="mallory",
                reason="wrong owner",
            )
        assert owner_error.value.code == "OWNER_MISMATCH"
        with pytest.raises(ControllerError) as cas_error:
            abort_integration(
                repository.repo,
                target=candidate,
                expected_record_version=version + 7,
                owner="integration-owner",
                reason="stale version",
            )
        assert cas_error.value.code == "CAS_MISMATCH"
        with pytest.raises(ControllerError) as reason_error:
            abort_integration(
                repository.repo,
                target=candidate,
                expected_record_version=version,
                owner="integration-owner",
                reason="   ",
            )
        assert reason_error.value.code == "INVALID_ABORT_REASON"


def test_aborted_preparation_can_still_classify_its_rebuildable_environment() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        candidate, claimed, _source_allocation = _prepared_candidate(repository)
        allocation = claimed["allocation_id"]
        abort_integration(
            repository.repo,
            target=candidate,
            expected_record_version=_record(repository, allocation).record_version,
            owner="integration-owner",
            reason="terminal validation failure",
        )
        # an environment the failed preparation created must still be classifiable,
        # otherwise the checkout can never converge. The lifecycle gate must no longer
        # fire; the resource-identity gates stay exactly as strict as before.
        with pytest.raises(ControllerError) as error:
            register_disposable(
                repository.repo,
                target=candidate,
                expected_record_version=_record(repository, allocation).record_version,
                owner="integration-owner",
                resource_path=".venv",
                kind="python-venv",
                reproducible=True,
                creator="uv",
                teardown_policy="on-release",
            )
        assert error.value.code == "RESOURCE_NOT_IGNORED"


def test_resource_registration_still_refuses_a_handoff_ready_allocation() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        target, _allocation, _oid = _source(
            repository, task="src-task", relative_path="src/one.txt", content="one\n"
        )
        record = next(
            item
            for item in MetadataStore(inventory(repository.repo).common_dir).load().records.values()
            if item.task_key == "src-task"
        )
        with pytest.raises(ControllerError) as error:
            register_disposable(
                repository.repo,
                target=target,
                expected_record_version=record.record_version,
                owner="module-owner",
                resource_path=".venv",
                kind="python-venv",
                reproducible=True,
                creator="uv",
                teardown_policy="on-release",
            )
        assert error.value.code == "INVALID_LIFECYCLE"


def test_a_live_source_wins_over_a_released_one_sharing_a_handoff_commit() -> None:
    from tests.test_controlled import handoff_one
    from worktree_controller.lifecycle import enqueue_integration

    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        first, allocation, oid = _source(
            repository, task="src-a", relative_path="src/shared.txt", content="shared\n"
        )
        release(
            repository.repo,
            target=first,
            expected_record_version=_record(repository, allocation).record_version,
            owner_release="module-owner",
        )
        # a second allocation later reaches the very same handoff commit
        second, payload = acquire_one(
            repository, task="src-b", authority="src-b", base_ref=oid, scope_paths=("src",)
        )
        handoff_one(repository, second)
        live = payload["decision"]["allocation_id"]

        decision = enqueue_integration(
            repository.repo,
            task_key="int-b",
            authority_key="int-b",
            module="integration",
            owner="integration-owner",
            source_handoffs=(oid,),
        )["decision"]
        assert decision["code"] == "INTEGRATION_ENQUEUED"
        assert decision["source_allocations"] == [live]
