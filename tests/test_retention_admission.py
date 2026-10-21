"""Writer-based admission with a bounded retained pool."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from tests.test_controlled import acquire_one, adopt_canonical
from tests.test_shadow import POLICY_V2, GitRepository, git
from worktree_controller.controller import adopt, plan, status
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import (
    acquire_across,
    acquire_when_free,
    complete_no_change,
    park,
    reconcile_session,
    release,
    resume,
    update_external_binding,
)
from worktree_controller.models import ControllerError
from worktree_controller.policy import load_policy
from worktree_controller.store import MetadataStore

POLICY_V5 = """schema_version = 5

[limits]
normal_limit = 5
hard_limit = 9

[canonical]
protected = true
allowed_roles = ["canonical", "control-plane", "integration", "publication", "verification"]

[slots]
integration_prepare_limit = 1
feature_or_audit = 3
emergency = 1
recovery_reserve = 1
retained = 2

[publication]
main_publish_limit = 1
remote = "origin"
branch = "main"

[release]
automatic_feature_audit = true
automatic_integration_continuation = true
preserved_auto_release = false
intent_ttl_seconds = 3600

[review]
stale_warning_days = 7

[authority]
require_adoption = true
require_task_key = true
require_authority_key = true
unique_active_feature_authority = true
"""


def _store(repository) -> MetadataStore:
    return MetadataStore(inventory(repository.repo).common_dir)


def _record(repository, allocation_id):
    return _store(repository).load().records[allocation_id]


def _version(repository, allocation_id) -> int:
    return _record(repository, allocation_id).record_version


def _fill_writers(repository, count):
    made = []
    for index in range(count):
        target, payload = acquire_one(
            repository,
            task=f"writer-{index}",
            authority=f"writer-{index}",
            owner="owner-b",
        )
        made.append((target, payload["decision"]["allocation_id"]))
    return made


def test_v5_policy_adds_a_bounded_retained_pool() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        policy = load_policy(repository.repo)
        assert policy.policy_schema_version == 5
        assert policy.retained_slots == 2
        assert policy.feature_audit_slots == 3


def test_v4_and_earlier_keep_retention_disabled() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        assert load_policy(repository.repo).retained_slots == 0
        adopt_canonical(repository)
        target, payload = acquire_one(repository, task="only", authority="only")
        allocation = payload["decision"]["allocation_id"]
        with pytest.raises(ControllerError) as error:
            park(
                repository.repo,
                target=target,
                expected_record_version=_version(repository, allocation),
                owner="module-owner",
            )
        assert error.value.code == "RETENTION_UNSUPPORTED"


def test_parking_changes_writer_statistics_and_keeps_physical_cost() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        made = _fill_writers(repository, 3)
        blocked = plan(
            repository.repo,
            task_key="next",
            authority_key="next",
            module="m",
            role="feature",
        )
        assert blocked["decision"]["code"] == "CREATE_ALLOWED"

        target, allocation = made[0]
        before = len(list((repository.root).glob("writer-*")))
        decision = park(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, allocation),
            owner="owner-b",
        )["decision"]
        assert decision["code"] == "RETAINED"
        assert _record(repository, allocation).lifecycle == "RETAINED"
        # the checkout is preserved: parking is not a release
        assert target.is_dir()
        assert len(list((repository.root).glob("writer-*"))) == before

        allowed = plan(
            repository.repo,
            task_key="next",
            authority_key="next",
            module="m",
            role="feature",
        )
        assert allowed["decision"]["code"] == "CREATE_ALLOWED"


def test_retained_work_does_not_impose_a_checkout_count_limit() -> None:

    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        for target, allocation in _fill_writers(repository, 3):
            park(
                repository.repo,
                target=target,
                expected_record_version=_version(repository, allocation),
                owner="owner-b",
            )
        assert status(repository.repo)["repository"]["retained_used"] == 3

        # Space is available even though the old ordinary count is exceeded.
        acquire_one(repository, task="more-0", authority="more-0", owner="owner-b")
        acquire_one(repository, task="more-1", authority="more-1", owner="owner-b")
        target, admitted = acquire_one(repository, task="more-2", authority="more-2", owner="owner-b")
        assert admitted["decision"]["code"] == "ACQUIRED" and target.is_dir()


def test_park_requires_the_recorded_owner_and_exact_record_version() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        (target, allocation), *_ = _fill_writers(repository, 1)
        with pytest.raises(ControllerError) as wrong_owner:
            park(
                repository.repo,
                target=target,
                expected_record_version=_version(repository, allocation),
                owner="mallory",
            )
        assert wrong_owner.value.code == "OWNER_MISMATCH"
        with pytest.raises(ControllerError) as stale:
            park(
                repository.repo,
                target=target,
                expected_record_version=_version(repository, allocation) + 5,
                owner="owner-b",
            )
        assert stale.value.code == "CAS_MISMATCH"


def test_park_refuses_canonical_and_non_active_lifecycles() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository, owner="owner-a")
        snapshot = _store(repository).load()
        canonical = next(
            record for record in snapshot.records.values() if record.role == "control-plane"
        )
        with pytest.raises(ControllerError) as error:
            park(
                repository.repo,
                target=repository.repo,
                expected_record_version=canonical.record_version,
                owner="owner-a",
            )
        assert error.value.code == "BLOCK_CANONICAL"

        (target, allocation), *_ = _fill_writers(repository, 1)
        park(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, allocation),
            owner="owner-b",
        )
        with pytest.raises(ControllerError) as twice:
            park(
                repository.repo,
                target=target,
                expected_record_version=_version(repository, allocation),
                owner="owner-b",
            )
        assert twice.value.code == "INVALID_LIFECYCLE"


def test_resume_round_trip_preserves_identity_and_unfinished_work() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        (target, allocation), *_ = _fill_writers(repository, 1)
        (target / "wip.txt").write_text("unfinished\n", encoding="utf-8")
        before = _record(repository, allocation)

        park(
            repository.repo,
            target=target,
            expected_record_version=before.record_version,
            owner="owner-b",
        )
        decision = resume(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, allocation),
            owner="owner-b",
        )["decision"]
        assert decision["code"] == "RESUMED"

        after = _record(repository, allocation)
        assert after.lifecycle == "ACTIVE"
        assert (after.task_key, after.authority_key, after.owner, after.allocation_id) == (
            before.task_key,
            before.authority_key,
            before.owner,
            before.allocation_id,
        )
        assert (target / "wip.txt").read_text(encoding="utf-8") == "unfinished\n"


def test_resume_does_not_reserve_a_process_or_consume_a_fixed_writer_slot() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        made = _fill_writers(repository, 3)
        target, allocation = made[0]
        park(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, allocation),
            owner="owner-b",
        )
        acquire_one(repository, task="replacement", authority="replacement", owner="owner-a")
        decision = resume(
            repository.repo, target=target,
            expected_record_version=_version(repository, allocation), owner="owner-b",
        )["decision"]
        assert decision["code"] == "RESUMED"


def test_resume_requires_the_recorded_owner() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        (target, allocation), *_ = _fill_writers(repository, 1)
        park(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, allocation),
            owner="owner-b",
        )
        with pytest.raises(ControllerError) as error:
            resume(
                repository.repo,
                target=target,
                expected_record_version=_version(repository, allocation),
                owner="mallory",
            )
        assert error.value.code == "OWNER_MISMATCH"


def test_session_reconciliation_retains_an_unfinished_direct_cli_task() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        (target, allocation), *_ = _fill_writers(repository, 1)
        (target / "wip.txt").write_text("unfinished\n", encoding="utf-8")
        decision = reconcile_session(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, allocation),
            owner="owner-b",
            trigger="task-terminal",
        )["decision"]
        assert decision["code"] == "RETAINED"
        assert _record(repository, allocation).lifecycle == "RETAINED"
        # stopping is never completion: work, checkout and branch survive untouched
        assert (target / "wip.txt").read_text(encoding="utf-8") == "unfinished\n"


def test_session_reconciliation_refuses_a_foreign_owner() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        (target, allocation), *_ = _fill_writers(repository, 1)
        with pytest.raises(ControllerError) as error:
            reconcile_session(
                repository.repo,
                target=target,
                expected_record_version=_version(repository, allocation),
                owner="mallory",
                trigger="session-archive",
            )
        assert error.value.code == "OWNER_MISMATCH"


def test_session_reconciliation_rejects_an_unknown_trigger() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        (target, allocation), *_ = _fill_writers(repository, 1)
        with pytest.raises(ControllerError) as error:
            reconcile_session(
                repository.repo,
                target=target,
                expected_record_version=_version(repository, allocation),
                owner="owner-b",
                trigger="idle-timeout",
            )
        assert error.value.code == "INVALID_TRIGGER"


def test_park_refuses_an_externally_bound_allocation() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        (target, allocation), *_ = _fill_writers(repository, 1)
        update_external_binding(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, allocation),
            owner="owner-b",
            binding="systemd-user:demo.service",
            action="add",
        )
        with pytest.raises(ControllerError) as error:
            park(
                repository.repo,
                target=target,
                expected_record_version=_version(repository, allocation),
                owner="owner-b",
            )
        assert error.value.code == "EXTERNAL_BINDING"


def test_no_change_completion_and_terminal_release() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        target, payload = acquire_one(
            repository, task="audit-one", authority="audit-one", role="audit", owner="owner-b"
        )
        allocation = payload["decision"]["allocation_id"]
        decision = complete_no_change(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, allocation),
            owner="owner-b",
        )["decision"]
        assert decision["code"] == "NO_CHANGE_COMPLETED"
        assert decision["completion_receipt_id"]
        record = _record(repository, allocation)
        assert record.lifecycle == "NO_CHANGE_COMPLETED"
        assert record.handoff_commit is None and record.finalizer_record_id is None

        released = release(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, allocation),
            owner_release="owner-b",
        )["decision"]
        assert released["code"] == "RELEASED"
        assert not target.exists()


def test_no_change_completion_refuses_a_task_that_produced_a_commit() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        (target, allocation), *_ = _fill_writers(repository, 1)
        (target / "result.txt").write_text("deliverable\n", encoding="utf-8")
        git(target, "add", "-A")
        git(target, "commit", "-m", "task result")
        with pytest.raises(ControllerError) as error:
            complete_no_change(
                repository.repo,
                target=target,
                expected_record_version=_version(repository, allocation),
                owner="owner-b",
            )
        assert error.value.code == "TASK_COMMIT_PRESENT"


def test_no_change_completion_refuses_an_untracked_task_result() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        (target, allocation), *_ = _fill_writers(repository, 1)
        (target / "notes.md").write_text("investigation output\n", encoding="utf-8")
        with pytest.raises(ControllerError) as error:
            complete_no_change(
                repository.repo,
                target=target,
                expected_record_version=_version(repository, allocation),
                owner="owner-b",
            )
        assert error.value.code == "DIRTY_WORKTREE"


def test_no_change_completion_refuses_external_binding_and_foreign_owner() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        (target, allocation), *_ = _fill_writers(repository, 1)
        with pytest.raises(ControllerError) as owner_error:
            complete_no_change(
                repository.repo,
                target=target,
                expected_record_version=_version(repository, allocation),
                owner="mallory",
            )
        assert owner_error.value.code == "OWNER_MISMATCH"
        update_external_binding(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, allocation),
            owner="owner-b",
            binding="systemd-user:demo.service",
            action="add",
        )
        with pytest.raises(ControllerError) as bound_error:
            complete_no_change(
                repository.repo,
                target=target,
                expected_record_version=_version(repository, allocation),
                owner="owner-b",
            )
        assert bound_error.value.code == "EXTERNAL_BINDING"


def test_new_terminal_states_are_unavailable_before_the_retention_schema() -> None:
    # A pre-v5 repository can never store RETAINED, NO_CHANGE_COMPLETED or
    # INTEGRATION_ABORTED, so an older controller never meets an unknown lifecycle.
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        adopt_canonical(repository)
        target, payload = acquire_one(repository, task="only", authority="only")
        allocation = payload["decision"]["allocation_id"]
        for call in (
            lambda: park(
                repository.repo,
                target=target,
                expected_record_version=_version(repository, allocation),
                owner="module-owner",
            ),
            lambda: complete_no_change(
                repository.repo,
                target=target,
                expected_record_version=_version(repository, allocation),
                owner="module-owner",
            ),
            lambda: reconcile_session(
                repository.repo,
                target=target,
                expected_record_version=_version(repository, allocation),
                owner="module-owner",
                trigger="task-terminal",
            ),
        ):
            with pytest.raises(ControllerError) as error:
                call()
            assert error.value.code == "RETENTION_UNSUPPORTED"


def test_retained_allocations_do_not_consume_the_reported_normal_budget() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        made = _fill_writers(repository, 3)
        before = status(repository.repo)["repository"]
        assert before["retained_used"] == 0
        # retained work shares the ordinary pool, so that is the bound reported
        assert before["retained_limit"] == before["ordinary_limit"] == 5
        assert before["normal_slots_remaining"] == 5 - 4

        target, allocation = made[0]
        park(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, allocation),
            owner="owner-b",
        )

        after = status(repository.repo)["repository"]
        # the checkout still exists, so the physical budget is unchanged ...
        assert after["worktree_count"] == before["worktree_count"]
        assert after["hard_slots_remaining"] == before["hard_slots_remaining"]
        # ... but a retained task is no longer a writer, and it is charged to the
        # retained pool rather than to the normal budget it has stopped using.
        assert after["retained_used"] == 1
        # the pool still holds three ordinary checkouts, so two remain either way
        assert after["retained_slots_remaining"] == after["ordinary_slots_remaining"] == 2
        assert after["writers_used"] == before["writers_used"] - 1
        assert after["normal_slots_remaining"] == before["normal_slots_remaining"] + 1


def test_planning_stays_available_after_a_retained_task_is_replaced() -> None:
    """A parked task plus its replacement must not read back as capacity drift."""
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        made = _fill_writers(repository, 3)
        target, allocation = made[0]
        park(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, allocation),
            owner="owner-b",
        )
        # the freed writer slot is taken by a genuinely new task
        acquire_one(repository, task="replacement", authority="replacement", owner="owner-b")

        payload = status(repository.repo)
        codes = {warning["code"] for warning in payload["warnings"]}
        assert "FEATURE_CAPACITY_VIOLATION" not in codes
        assert "ORDINARY_POOL_VIOLATION" not in codes

        # Counts remain observable without becoming admission gates.
        blocked = plan(
            repository.repo, task_key="next", authority_key="next", module="m", role="feature"
        )
        assert blocked["decision"]["code"] == "CREATE_ALLOWED"

        second, second_allocation = made[1]
        park(
            repository.repo,
            target=second,
            expected_record_version=_version(repository, second_allocation),
            owner="owner-b",
        )
        allowed = plan(
            repository.repo, task_key="next", authority_key="next", module="m", role="feature"
        )
        assert allowed["decision"]["code"] == "CREATE_ALLOWED"


def test_ordinary_count_overflow_is_statistical_and_not_drift() -> None:
    with GitRepository(linked=6, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        for index, path in enumerate(repository.linked):
            adopt(
                repository.repo,
                target=path,
                task_key=f"retained-{index}",
                authority_key=f"retained-{index}",
                module="module",
                role="feature",
                owner="owner-b",
                lifecycle="RETAINED",
            )
        payload = status(repository.repo)
        drift = [
            warning
            for warning in payload["warnings"]
            if warning["code"] == "ORDINARY_POOL_VIOLATION"
        ]
        assert not drift
        assert payload["repository"]["retained_used"] == 6
        assert payload["repository"]["ordinary_physical"] == 6
        # retained checkouts are not writers, so the writer pool reads as clear
        assert not any(
            warning["code"] == "FEATURE_CAPACITY_VIOLATION" for warning in payload["warnings"]
        )
        assert (
            plan(
                repository.repo, task_key="next", authority_key="next", module="m", role="feature"
            )["decision"]["code"]
            == "CREATE_ALLOWED"
        )


# --------------------------------------------------------------------------- #
# Ordinary capacity is one shared physical pool, bounded but not partitioned
# --------------------------------------------------------------------------- #


def _ordinary_counts(repository):
    payload = status(repository.repo)["repository"]
    return {
        key: payload[key]
        for key in (
            "worktree_count",
            "ordinary_physical",
            "ordinary_limit",
            "writers_used",
            "writer_limit",
            "retained_used",
        )
    }


def test_parking_never_needs_new_capacity() -> None:
    """Parking relabels a checkout that already exists; it cannot exhaust anything."""

    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        made = _fill_writers(repository, 3)
        physical = len(inventory(repository.repo).worktrees)

        for target, allocation in made:
            decision = park(
                repository.repo,
                target=target,
                expected_record_version=_version(repository, allocation),
                owner="owner-b",
            )["decision"]
            assert decision["code"] == "RETAINED"

        assert len(inventory(repository.repo).worktrees) == physical
        counts = _ordinary_counts(repository)
        assert counts["retained_used"] == 3
        assert counts["writers_used"] == 0
        assert counts["ordinary_physical"] == 3


def test_retained_heavy_occupancy_keeps_counts_without_refusing_resources() -> None:

    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        for target, allocation in _fill_writers(repository, 3):
            park(
                repository.repo,
                target=target,
                expected_record_version=_version(repository, allocation),
                owner="owner-b",
            )

        # Writers are free again, and the shared pool still has room for two.
        first, _ = acquire_one(repository, task="after-0", authority="after-0", owner="owner-b")
        second, _ = acquire_one(repository, task="after-1", authority="after-1", owner="owner-b")
        counts = _ordinary_counts(repository)
        assert counts["ordinary_physical"] == counts["ordinary_limit"] == 5
        assert counts["writers_used"] == 2

        # The historical count is full, but filesystem resources are available.
        refused = plan(
            repository.repo,
            task_key="after-2",
            authority_key="after-2",
            module="m",
            role="feature",
        )["decision"]
        assert refused["code"] == "CREATE_ALLOWED"
        _, admitted = acquire_one(repository, task="after-2", authority="after-2", owner="owner-b")
        assert admitted["decision"]["code"] == "ACQUIRED"
        assert first.is_dir() and second.is_dir()


def test_active_allocation_count_is_not_process_concurrency() -> None:

    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        _fill_writers(repository, 3)
        counts = _ordinary_counts(repository)
        assert counts["writers_used"] == counts["writer_limit"] == 3
        assert counts["ordinary_physical"] == 3 < counts["ordinary_limit"]

        refused = plan(
            repository.repo,
            task_key="fourth",
            authority_key="fourth",
            module="m",
            role="feature",
        )["decision"]
        assert refused["code"] == "CREATE_ALLOWED"


def test_status_plan_and_acquire_report_one_accounting() -> None:
    """The same numbers decide admission and are reported to an operator."""

    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        made = _fill_writers(repository, 3)
        park(
            repository.repo,
            target=made[0][0],
            expected_record_version=_version(repository, made[0][1]),
            owner="owner-b",
        )
        counts = _ordinary_counts(repository)
        assert counts["writers_used"] == 2
        assert counts["retained_used"] == 1
        assert counts["ordinary_physical"] == 3

        decision = plan(
            repository.repo,
            task_key="next",
            authority_key="next",
            module="m",
            role="feature",
        )["decision"]
        assert decision["code"] == "CREATE_ALLOWED"
        assert decision["capacity"]["writers_used"] == counts["writers_used"]
        assert decision["capacity"]["ordinary_physical"] == counts["ordinary_physical"]
        assert decision["capacity"]["ordinary_limit"] == counts["ordinary_limit"]


# --------------------------------------------------------------------------- #
# One task, several repositories: all of them or none
# --------------------------------------------------------------------------- #


def _across_request(repository, task, role="feature"):
    return {
        "repository": repository.repo,
        "task_key": task,
        "authority_key": task,
        "module": "m",
        "role": role,
        "owner": "owner-b",
        "branch": f"feat/{task}",
        "base_ref": "HEAD",
        "target": repository.root / task,
    }


def test_cross_repository_admission_keeps_nothing_when_one_repository_refuses() -> None:
    """A task needing two repositories never sits half-acquired."""

    with (
        GitRepository(linked=0, policy_text=POLICY_V5) as roomy,
        GitRepository(linked=0, policy_text=POLICY_V5) as full,
    ):
        adopt_canonical(roomy)
        adopt_canonical(full)
        _fill_writers(full, 3)
        # A real ownership conflict still refuses the entire group before creation.
        acquire_one(full, task="conflict", authority="cross-task", owner="owner-b")
        before = len(inventory(roomy.repo).worktrees)

        payload = acquire_across(
            [
                _across_request(roomy, "cross-task"),
                _across_request(full, "cross-task"),
            ]
        )

        decision = payload["decision"]
        assert decision["code"] == "ACROSS_REFUSED"
        assert decision["acquired"] == []
        # the roomy repository was never touched, because preflight ran first
        assert len(inventory(roomy.repo).worktrees) == before
        assert decision["refused_code"] == "BLOCK_DUPLICATE_AUTHORITY"


def test_cross_repository_admission_rolls_back_a_later_refusal() -> None:
    """If a later repository refuses after the first was taken, the first is given back."""

    with (
        GitRepository(linked=0, policy_text=POLICY_V5) as first,
        GitRepository(linked=0, policy_text=POLICY_V5) as second,
    ):
        adopt_canonical(first)
        adopt_canonical(second)
        before_first = len(inventory(first.repo).worktrees)
        requests = [_across_request(first, "cross-task"), _across_request(second, "cross-task")]
        # the second repository loses its capacity between preflight and acquire
        original = acquire_across.__globals__["acquire"]
        calls = {"n": 0}

        def flaky(repository_path, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise ControllerError("BLOCK_DUPLICATE_AUTHORITY", "authority raced away")
            return original(repository_path, **kwargs)

        acquire_across.__globals__["acquire"] = flaky
        try:
            payload = acquire_across(requests)
        finally:
            acquire_across.__globals__["acquire"] = original

        decision = payload["decision"]
        assert decision["code"] == "ACROSS_REFUSED"
        assert decision["acquired"] == []
        assert [entry["result"] for entry in decision["rolled_back"]] == ["RELEASED"]
        assert len(inventory(first.repo).worktrees) == before_first


def test_cross_repository_admission_takes_every_repository_or_none() -> None:
    with (
        GitRepository(linked=0, policy_text=POLICY_V5) as first,
        GitRepository(linked=0, policy_text=POLICY_V5) as second,
    ):
        adopt_canonical(first)
        adopt_canonical(second)

        decision = acquire_across(
            [_across_request(second, "cross-task"), _across_request(first, "cross-task")]
        )["decision"]

        assert decision["code"] == "ACROSS_ACQUIRED"
        assert len(decision["acquired"]) == 2
        # deterministic global order, independent of the order requested
        assert [entry["repository"] for entry in decision["acquired"]] == sorted(
            entry["repository"] for entry in decision["acquired"]
        )
        for entry in decision["acquired"]:
            assert Path(entry["path"]).is_dir()


# --------------------------------------------------------------------------- #
# Two waiters, one slot: bounded and explicit, never an uncontrolled race
# --------------------------------------------------------------------------- #


def test_two_waiters_do_not_require_a_count_slot_to_be_freed() -> None:

    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        made = _fill_writers(repository, 3)
        target, allocation = made[0]

        results: dict[str, str] = {}
        started = threading.Barrier(2)

        def waiter(name):
            started.wait()
            payload = acquire_when_free(
                repository.repo,
                deadline_seconds=12,
                poll_seconds=1.0,
                task_key=name,
                authority_key=name,
                module="m",
                role="feature",
                owner="owner-b",
                branch=f"feat/{name}",
                base_ref="HEAD",
                target=repository.root / name,
            )
            results[name] = payload["decision"]["code"]

        threads = [
            threading.Thread(target=waiter, args=("waiter-a",)),
            threading.Thread(target=waiter, args=("waiter-b",)),
        ]
        for thread in threads:
            thread.start()
        time.sleep(0.5)
        # exactly one writer slot is freed, once
        park(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, allocation),
            owner="owner-b",
        )
        for thread in threads:
            thread.join(timeout=30)

        assert sorted(results.values()) == ["ACQUIRED", "ACQUIRED"]
        payload = status(repository.repo)["repository"]
        assert payload["writers_used"] == 4 > payload["writer_limit"]


def test_an_unsatisfiable_request_is_refused_immediately() -> None:
    """A refusal that waiting cannot change must not hold the front of the queue."""

    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        started = time.monotonic()
        with pytest.raises(ControllerError) as refused:
            acquire_when_free(
                repository.repo,
                deadline_seconds=30,
                poll_seconds=1.0,
                task_key="bad task key",
                authority_key="bad",
                module="m",
                role="feature",
                owner="owner-b",
                branch="feat/bad",
                base_ref="HEAD",
                target=repository.root / "bad",
            )
        assert refused.value.code == "INVALID_TASK_KEY"
        assert time.monotonic() - started < 5


def test_six_independent_tasks_all_obtain_legal_admission() -> None:
    """When physical and writer capacity truly exist, six tasks all get in."""

    policy = (
        POLICY_V5.replace("feature_or_audit = 3", "feature_or_audit = 6")
        .replace("normal_limit = 5", "normal_limit = 8")
        .replace("hard_limit = 9", "hard_limit = 12")
    )
    with GitRepository(linked=0, policy_text=policy) as repository:
        adopt_canonical(repository)
        admitted = []
        for index in range(6):
            target, payload = acquire_one(
                repository,
                task=f"task-{index}",
                authority=f"task-{index}",
                owner="owner-b",
            )
            assert payload["decision"]["code"] == "ACQUIRED"
            admitted.append(target)

        assert all(target.is_dir() for target in admitted)
        counts = status(repository.repo)["repository"]
        assert counts["writers_used"] == 6
        assert counts["ordinary_physical"] == 6
        assert counts["physical_used"] <= counts["physical_limit"]
        _, admitted = acquire_one(repository, task="task-6", authority="task-6", owner="owner-b")
        assert admitted["decision"]["code"] == "ACQUIRED"


def test_concurrent_acquire_and_retain_preserve_unique_ownership() -> None:

    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        made = _fill_writers(repository, 3)
        outcomes: list[str] = []
        lock = threading.Lock()

        def contend(index):
            try:
                acquire_one(
                    repository,
                    task=f"racer-{index}",
                    authority=f"racer-{index}",
                    owner="owner-b",
                )
                result = "ACQUIRED"
            except ControllerError as blocked:
                result = blocked.code
            with lock:
                outcomes.append(result)

        def retire(entry):
            target, allocation = entry
            try:
                park(
                    repository.repo,
                    target=target,
                    expected_record_version=_version(repository, allocation),
                    owner="owner-b",
                )
                result = "RETAINED"
            except ControllerError as blocked:
                result = blocked.code
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=contend, args=(index,)) for index in range(4)]
        threads += [threading.Thread(target=retire, args=(entry,)) for entry in made]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        counts = status(repository.repo)["repository"]
        assert counts["writers_used"] == 4
        assert counts["ordinary_physical"] == 7
        # every parked allocation is retained exactly once, never released twice
        worktrees = status(repository.repo)["worktrees"]
        retained = [w for w in worktrees if w["lifecycle"] == "RETAINED"]
        assert len(retained) == len({w["allocation_id"] for w in retained})
        # no allocation is recorded twice, and every refusal was a real bound
        assert len(worktrees) == len({w["allocation_id"] for w in worktrees})
        assert set(outcomes) <= {
            "ACQUIRED",
            "RETAINED",
        }
        assert outcomes.count("RETAINED") == len(retained)
