"""Session-end wiring, delivered-allocation adoption and spent release-plan repair."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, replace
from datetime import datetime, timedelta

import pytest

from tests.test_controlled import (
    _commit_reproducible_project,
    _configure_remote,
    _create_reproducible_venv,
    acquire_one,
    adopt_canonical,
    handoff_one,
)
from tests.test_integration_abort import (
    POLICY_THREE_CANDIDATES_V5,
    _intent,
    _prepared_candidate,
)
from tests.test_retention_admission import POLICY_V5, _record, _store, _version
from tests.test_shadow import POLICY_V2, GitRepository, git
from worktree_controller import lifecycle, session
from worktree_controller.branch_retirement import plan_branch_retirement
from worktree_controller.controller import doctor
from worktree_controller.lifecycle import (
    abandon_integration_intent,
    abort_integration,
    acquire,
    adopt_delivered,
    complete_no_change,
    integration_queue,
    park,
    reconcile_integration_intents,
    reconcile_release_plans,
    register_disposable,
    release,
    reopen,
    resume,
)
from worktree_controller.lifecycle_history import lifecycle_history
from worktree_controller.models import ControllerError
from worktree_controller.session import main as session_end
from worktree_controller.store import MetadataStore
from worktree_controller.writer_lease import (
    acquire_writer_lease,
    release_writer_lease,
    resolve_holder,
)


def _capture(capsys, *arguments: str) -> dict:
    assert session_end(list(arguments)) == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_abandoned_preparation_is_terminal_but_an_unexplained_orphan_stays_blocked() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        candidate, claimed, source_allocation = _prepared_candidate(repository)
        allocation = claimed["allocation_id"]
        intent_id = claimed["intent_id"]
        source_before = _record(repository, source_allocation)
        abort_integration(
            repository.repo,
            target=candidate,
            expected_record_version=_version(repository, allocation),
            owner="integration-owner",
            reason="preparation failed before publication",
        )
        release(
            repository.repo,
            target=candidate,
            expected_record_version=_version(repository, allocation),
            owner_release="integration-owner",
        )
        assert any(
            warning["code"] == "INTEGRATION_INTENT_DRIFT"
            for warning in doctor(repository.repo)["warnings"]
        )
        before = _intent(repository, intent_id)
        decision = abandon_integration_intent(
            repository.repo,
            intent_id=intent_id,
            owner="integration-owner",
            reason="failed preparation released; source work retained",
        )["decision"]
        assert decision["code"] == "INTEGRATION_ABANDONED"
        abandoned = _intent(repository, intent_id)
        assert abandoned.history[-1]["event"] == "PREPARATION_ABANDONED"
        assert abandoned.history[:-1] == before.history
        assert abandoned.source_handoffs == before.source_handoffs
        assert abandoned.blocked_reason == decision["failure_reason"]
        assert _record(repository, source_allocation) == source_before
        assert doctor(repository.repo)["decision"]["code"] == "PASS"
        queued = integration_queue(repository.repo)["decision"]["intents"]
        assert (
            next(i for i in queued if i["intent_id"] == intent_id)["effective_state"] == "ABANDONED"
        )
        plan = reconcile_integration_intents(
            repository.repo, actor="auditor", intent_ids=(intent_id,), dry_run=True
        )["decision"]
        assert plan["outcomes"][0]["effective_state"] == "ABANDONED"
        assert plan["synthesized_event_ids"] == []
        repeated = abandon_integration_intent(
            repository.repo, intent_id=intent_id, owner="integration-owner", reason="retry"
        )["decision"]
        assert repeated["code"] == "INTEGRATION_ABANDONED"
        assert repeated["failure_reason"] == abandoned.blocked_reason
        assert _intent(repository, intent_id) == abandoned


def test_abandon_refuses_foreign_owner_and_live_preparation() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        _candidate, claimed, _source = _prepared_candidate(repository)
        intent_id = claimed["intent_id"]
        before = _intent(repository, intent_id)
        for owner, code in (
            ("other-owner", "OWNER_MISMATCH"),
            ("integration-owner", "LIVE_ALLOCATION"),
        ):
            with pytest.raises(ControllerError) as error:
                abandon_integration_intent(
                    repository.repo, intent_id=intent_id, owner=owner, reason="unfinished"
                )
            assert error.value.code == code
            assert _intent(repository, intent_id) == before


def test_session_end_retains_an_unfinished_direct_cli_task(capsys) -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        target, payload = acquire_one(
            repository, task="cli-task", authority="cli-task", owner="owner-b"
        )
        allocation = payload["decision"]["allocation_id"]
        (target / "wip.txt").write_text("unfinished\n", encoding="utf-8")

        result = _capture(capsys, "--path", str(target), "--trigger", "task-terminal")
        assert result["decision"]["code"] == "RETAINED"
        assert _record(repository, allocation).lifecycle == "RETAINED"
        assert (target / "wip.txt").read_text(encoding="utf-8") == "unfinished\n"


def test_session_end_is_idempotent_and_never_fails_a_session_exit(capsys) -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        target, _payload = acquire_one(
            repository, task="cli-task", authority="cli-task", owner="owner-b"
        )
        first = _capture(capsys, "--path", str(target), "--trigger", "task-terminal")
        assert first["decision"]["code"] == "RETAINED"
        # a second exit signal for the same session must not fail or change anything
        second = _capture(capsys, "--path", str(target), "--trigger", "task-terminal")
        assert second["decision"]["code"] == "NO_TERMINAL_ACTION"
        third = _capture(capsys, "--path", str(target), "--trigger", "session-archive")
        assert third["decision"]["code"] == "NO_TERMINAL_ACTION"


def test_session_end_fails_closed_on_unknown_identity_and_foreign_owner(capsys) -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        target, _payload = acquire_one(
            repository, task="cli-task", authority="cli-task", owner="owner-b"
        )
        unknown = _capture(capsys, "--path", str(repository.root), "--trigger", "task-terminal")
        assert unknown["decision"]["code"] == "NO_TERMINAL_ACTION"
        foreign = _capture(
            capsys, "--path", str(target), "--trigger", "task-terminal", "--owner", "mallory"
        )
        assert foreign["decision"]["code"] == "NO_TERMINAL_ACTION"
        assert _record(repository, _payload["decision"]["allocation_id"]).lifecycle == "ACTIVE"


def test_session_end_refuses_an_externally_bound_or_canonical_target(capsys) -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        canonical = _capture(capsys, "--path", str(repository.repo), "--trigger", "task-terminal")
        assert canonical["decision"]["code"] == "NO_TERMINAL_ACTION"
        target, payload = acquire_one(repository, task="bound", authority="bound", owner="owner-b")
        from worktree_controller.lifecycle import update_external_binding

        update_external_binding(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, payload["decision"]["allocation_id"]),
            owner="owner-b",
            binding="systemd-user:demo.service",
            action="add",
        )
        bound = _capture(capsys, "--path", str(target), "--trigger", "task-terminal")
        assert bound["decision"]["code"] == "NO_TERMINAL_ACTION"
        assert "binding" in bound["decision"]["reason"]


def _delivered(repository, *, bootstrap: bool = True, role: str = "feature"):
    if bootstrap:
        adopt_canonical(repository)
        _configure_remote(repository)
    target, payload = acquire_one(
        repository, task="done-task", authority="done-task", owner="departed-owner", role=role
    )
    (target / "result.txt").write_text("delivered\n", encoding="utf-8")
    git(target, "add", "-A")
    git(target, "commit", "-m", "deliverable")
    handoff_one(repository, target)
    head = git(target, "rev-parse", "HEAD").strip()
    git(repository.repo, "merge", "--ff-only", head)
    git(repository.repo, "push", "origin", "main")
    return target, payload["decision"]["allocation_id"]


def test_delivered_hotfix_can_classify_an_environment_through_guarded_reopen() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        _commit_reproducible_project(repository)
        target, allocation = _delivered(repository, role="hotfix")
        head = git(target, "rev-parse", "HEAD")
        branch = git(target, "branch", "--show-current")
        _create_reproducible_venv(target)
        adopt_delivered(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, allocation),
            new_owner="cleanup-owner",
            actor="cleanup-owner",
        )
        with pytest.raises(ControllerError) as ignored:
            release(
                repository.repo,
                target=target,
                expected_record_version=_version(repository, allocation),
                owner_release="cleanup-owner",
                dry_run=True,
            )
        assert ignored.value.code == "IGNORED_CONTENT"
        with pytest.raises(ControllerError) as foreign:
            reopen(
                repository.repo,
                target=target,
                expected_record_version=_version(repository, allocation),
                owner="other-owner",
            )
        assert foreign.value.code == "OWNER_MISMATCH"
        reopen(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, allocation),
            owner="cleanup-owner",
        )
        assert _record(repository, allocation).handoff_commit is None
        register_disposable(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, allocation),
            owner="cleanup-owner",
            resource_path=".venv",
            kind="python-venv",
            reproducible=True,
            creator="test-fixture",
            teardown_policy="on-release",
        )
        handoff_one(repository, target, expected=_version(repository, allocation))
        result = release(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, allocation),
            owner_release="cleanup-owner",
        )["decision"]
        assert result["code"] == "RELEASED"
        assert not target.exists()
        assert git(repository.repo, "rev-parse", branch) == head


def test_adopt_delivered_transfers_ownership_with_recorded_evidence() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        target, allocation = _delivered(repository)
        decision = adopt_delivered(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, allocation),
            new_owner="owner-b",
            actor="owner-b",
        )["decision"]
        assert decision["code"] == "OWNER_ADOPTED"
        assert decision["previous_owner"] == "departed-owner"
        record = _record(repository, allocation)
        assert record.owner == "owner-b"
        assert record.lifecycle == "HANDOFF_READY"
        assert record.handoff_commit and record.finalizer_record_id
        assert record.history[-1]["event"] == "OWNER_ADOPTED"
        # the adopted owner can now complete the normal lifecycle
        released = release(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, allocation),
            owner_release="owner-b",
        )["decision"]
        assert released["code"] == "RELEASED"


def test_adopt_delivered_refuses_unfinished_dirty_and_undelivered_work() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        active, payload = acquire_one(repository, task="live", authority="live", owner="other")
        allocation = payload["decision"]["allocation_id"]
        with pytest.raises(ControllerError) as active_error:
            adopt_delivered(
                repository.repo,
                target=active,
                expected_record_version=_version(repository, allocation),
                new_owner="owner-b",
                actor="owner-b",
            )
        assert active_error.value.code == "INVALID_LIFECYCLE"

        target, delivered_allocation = _delivered(repository, bootstrap=False)
        (target / "extra.txt").write_text("uncommitted\n", encoding="utf-8")
        with pytest.raises(ControllerError) as dirty_error:
            adopt_delivered(
                repository.repo,
                target=target,
                expected_record_version=_version(repository, delivered_allocation),
                new_owner="owner-b",
                actor="owner-b",
            )
        assert dirty_error.value.code == "DIRTY_WORKTREE"


def test_adopt_delivered_refuses_a_handoff_not_in_the_publication_branch() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        target, payload = acquire_one(
            repository, task="pending", authority="pending", owner="departed-owner"
        )
        allocation = payload["decision"]["allocation_id"]
        (target / "result.txt").write_text("not merged\n", encoding="utf-8")
        git(target, "add", "-A")
        git(target, "commit", "-m", "pending deliverable")
        handoff_one(repository, target)
        with pytest.raises(ControllerError) as error:
            adopt_delivered(
                repository.repo,
                target=target,
                expected_record_version=_version(repository, allocation),
                new_owner="owner-b",
                actor="owner-b",
            )
        assert error.value.code == "DELIVERABLE_NOT_PUBLISHED"


def test_reconcile_release_plans_preserves_history_and_retirement_evidence() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        target, allocation = _delivered(repository)
        adopt_delivered(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, allocation),
            new_owner="owner-b",
            actor="owner-b",
        )
        # a live plan for a not-yet-released allocation must be kept
        kept = reconcile_release_plans(repository.repo, actor="owner-b", dry_run=True)["decision"]
        assert kept["retired"] == 0

        release(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, allocation),
            owner_release="owner-b",
        )
        store = _store(repository)
        before = store.load()
        receipt, = before.release_receipts.values()
        plan_path = store.release_plans_dir / f"{receipt.plan_id}.json"
        plan_bytes = plan_path.read_bytes()
        history = lifecycle_history(repository.repo, allocation_id=allocation)["decision"]
        retirement = plan_branch_retirement(
            repository.repo, allocation_id=allocation, remote="origin",
            integrated_into="main", dry_run=True,
        )["decision"]["plan"]
        assert retirement["eligible"]
        for dry_run in (True, False):
            decision = reconcile_release_plans(
                repository.repo, actor="owner-b", dry_run=dry_run,
            )["decision"]
            assert decision["code"] == "RELEASE_PLANS_RECONCILED"
            assert decision["retired"] == 0
            assert plan_path.read_bytes() == plan_bytes
            after = store.load()
            assert after.release_plans == before.release_plans
            assert after.release_receipts == before.release_receipts
            assert after.records == before.records
            assert lifecycle_history(repository.repo, allocation_id=allocation)["decision"] == history
            assert plan_branch_retirement(
                repository.repo, allocation_id=allocation, remote="origin",
                integrated_into="main", dry_run=True,
            )["decision"]["plan"] == retirement


def test_reconcile_release_plans_retires_unreferenced_spent_plan() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        target, allocation = _delivered(repository)
        adopt_delivered(
            repository.repo, target=target, expected_record_version=_version(repository, allocation),
            new_owner="owner-b", actor="owner-b",
        )
        release(
            repository.repo, target=target, expected_record_version=_version(repository, allocation),
            owner_release="owner-b",
        )
        store = _store(repository)
        before = store.load()
        receipt, = before.release_receipts.values()
        original = before.release_plans[receipt.plan_id]
        orphan = replace(
            original, created_at=(datetime.fromisoformat(original.created_at) + timedelta(seconds=1)).isoformat(),
        )
        identity = {key: value for key, value in asdict(orphan).items() if key != "plan_id"}
        orphan = replace(orphan, plan_id=lifecycle._release_plan_id(identity))
        store.write_release_plan(orphan)
        preview = reconcile_release_plans(repository.repo, actor="owner-b", dry_run=True)["decision"]
        assert preview["retired"] == 1
        assert set(store.load().release_plans) == {original.plan_id, orphan.plan_id}
        applied = reconcile_release_plans(repository.repo, actor="owner-b")["decision"]
        assert applied["retired"] == 1
        assert store.load().release_plans == {original.plan_id: original}
        history = lifecycle_history(repository.repo, allocation_id=allocation)["decision"]
        assert history["closed_releases"][0]["proof_status"] == "COMPLETE"


def _leased(repository, task, *, session_id):
    import os

    target, payload = acquire_one(repository, task=task, authority=task, owner="owner-b")
    acquire_writer_lease(
        repository.repo,
        target=target,
        owner="owner-b",
        client_id="claude",
        holder_pid=os.getpid(),
        session_id=session_id,
    )
    return target, payload["decision"]["allocation_id"]


def _selector(session_id):
    import os

    return ("--client-id", "claude", "--session-id", session_id, "--holder", str(os.getpid()))


def test_session_selector_ends_only_this_sessions_leased_checkouts(capsys) -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        first, first_id = _leased(repository, "first", session_id="session-a")
        second, second_id = _leased(repository, "second", session_id="session-a")
        other, other_id = _leased(repository, "other", session_id="session-b")
        (first / "wip.txt").write_text("unfinished\n", encoding="utf-8")

        ended = _capture(capsys, "--path", str(repository.repo), *_selector("session-a"))
        assert ended["decision"]["code"] == "SESSION_ENDED"
        assert sorted(row["path"] for row in ended["allocations"]) == sorted(
            [str(first), str(second)]
        )
        assert {row["lease"] for row in ended["allocations"]} == {"WRITER_LEASE_RELEASED"}
        assert {row["decision"]["code"] for row in ended["allocations"]} == {"RETAINED"}
        assert _record(repository, first_id).lifecycle == "RETAINED"
        assert _record(repository, second_id).lifecycle == "RETAINED"
        assert (first / "wip.txt").read_text(encoding="utf-8") == "unfinished\n"
        # another session of the same client keeps its lease and its ACTIVE task
        assert _record(repository, other_id).lifecycle == "ACTIVE"
        leases = MetadataStore(repository.repo / ".git").load().writer_leases
        assert set(leases) == {other_id}
        del other

        repeated = _capture(capsys, "--path", str(repository.repo), *_selector("session-a"))
        assert repeated["decision"]["code"] == "NO_TERMINAL_ACTION"
        assert repeated["allocations"] == []


@pytest.mark.parametrize("window", ["after-release", "before-park"])
def test_late_session_end_preserves_a_new_writer_without_a_runtime_binding(
    capsys, monkeypatch, window
) -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        target, allocation = _leased(repository, "primary", session_id="session-a")
        _other, other_id = _leased(repository, "other", session_id="session-b")
        store = _store(repository)
        old_lease = store.load().writer_leases[allocation]
        old_version = _version(repository, allocation)
        original = session.release_writer_lease if window == "after-release" else lifecycle.park
        pinned = []

        def acquire_new_writer(*args, **kwargs):
            if window == "after-release":
                result = original(*args, **kwargs)
            acquire_writer_lease(
                repository.repo,
                target=target,
                owner="owner-b",
                client_id="claude",
                holder_pid=os.getpid(),
                session_id="session-a",
                run_id="new-run",
            )
            pinned.append(store.load())
            assert pinned[0].records[allocation].record_version == old_version
            assert pinned[0].records[allocation].external_bindings == ()
            assert pinned[0].writer_leases[allocation].lease_id != old_lease.lease_id
            return result if window == "after-release" else original(*args, **kwargs)

        if window == "after-release":
            monkeypatch.setattr(session, "release_writer_lease", acquire_new_writer)
        else:
            # Grant after reconcile_session's first lock has already been released.
            monkeypatch.setattr(lifecycle, "park", acquire_new_writer)
        ended = _capture(capsys, "--path", str(repository.repo), *_selector("session-a"))
        row, = ended["allocations"]
        assert ended["decision"]["code"] == "SESSION_ENDED"
        assert row["lease_id"] == old_lease.lease_id
        assert row["lease"] == "WRITER_LEASE_RELEASED"
        assert row["decision"]["code"] == "NO_TERMINAL_ACTION"
        assert row["decision"]["blocker"] == "WRITER_LEASE_PRESENT"
        assert store.load() == pinned[0]
        assert set(store.load().writer_leases) == {allocation, other_id}
        repeated = _capture(capsys, "--path", str(repository.repo), *_selector("session-a"))
        assert repeated["allocations"] == []
        assert store.load() == pinned[0]


def test_late_session_end_keeps_the_original_allocation_record_version(capsys, monkeypatch) -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        target, allocation = _leased(repository, "primary", session_id="session-a")
        store = _store(repository)
        original = session.release_writer_lease
        pinned = []

        def resume_before_terminal(*args, **kwargs):
            result = original(*args, **kwargs)
            park(repository.repo, target=target, owner="owner-b",
                 expected_record_version=_version(repository, allocation))
            resume(repository.repo, target=target, owner="owner-b",
                   expected_record_version=_version(repository, allocation))
            acquired = acquire_writer_lease(
                repository.repo, target=target, owner="owner-b", client_id="claude",
                holder_pid=os.getpid(), session_id="session-a", run_id="new-run",
            )["decision"]
            release_writer_lease(
                repository.repo, target=target, lease_id=acquired["lease_id"],
                holder=resolve_holder(str(os.getpid()), client_id="claude",
                                      session_id="session-a", run_id="new-run"),
            )
            pinned.append(store.load())
            return result

        monkeypatch.setattr(session, "release_writer_lease", resume_before_terminal)
        ended = _capture(capsys, "--path", str(repository.repo), *_selector("session-a"))
        row, = ended["allocations"]
        assert row["decision"]["code"] == "NO_TERMINAL_ACTION"
        assert row["decision"]["blocker"] == "CAS_MISMATCH"
        assert store.load() == pinned[0]
        assert pinned[0].records[allocation].lifecycle == "ACTIVE"
        assert not pinned[0].writer_leases


def test_path_only_session_end_preserves_a_current_writer(capsys) -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        target, _allocation = _leased(repository, "primary", session_id="session-a")
        store = _store(repository)
        before = store.load()
        ended = _capture(capsys, "--path", str(target))
        assert ended["decision"]["code"] == "NO_TERMINAL_ACTION"
        assert ended["decision"]["blocker"] == "WRITER_LEASE_PRESENT"
        assert store.load() == before


@pytest.mark.parametrize("window", ["after-release", "before-park"])
def test_late_session_end_cannot_park_a_replacement_allocation(capsys, monkeypatch, window) -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        target, allocation = _leased(repository, "primary", session_id="session-a")
        store = _store(repository)
        original = session.release_writer_lease if window == "after-release" else lifecycle.park
        pinned = []

        def replace_allocation(*args, **kwargs):
            if window == "after-release":
                result = original(*args, **kwargs)
            complete_no_change(
                repository.repo, target=target, owner="owner-b",
                expected_record_version=_version(repository, allocation),
            )
            release(
                repository.repo, target=target, owner_release="owner-b",
                expected_record_version=_version(repository, allocation),
            )
            acquired = acquire(
                repository.repo, task_key="replacement", authority_key="replacement",
                module="module", role="feature", owner="owner-b",
                branch="feature/replacement", base_ref="HEAD", target=target,
            )["decision"]
            replacement_id = acquired["allocation_id"]
            assert replacement_id != allocation
            assert _version(repository, replacement_id) == 1
            pinned.append(store.load())
            return result if window == "after-release" else original(*args, **kwargs)

        if window == "after-release":
            monkeypatch.setattr(session, "release_writer_lease", replace_allocation)
        else:
            monkeypatch.setattr(lifecycle, "park", replace_allocation)
        ended = _capture(capsys, "--path", str(repository.repo), *_selector("session-a"))
        row, = ended["allocations"]
        assert row["decision"]["code"] == "NO_TERMINAL_ACTION"
        assert row["decision"]["blocker"] == "IDENTITY_MISMATCH"
        assert store.load() == pinned[0]


def test_session_selector_refuses_hosted_children_and_partial_identity(capsys, monkeypatch) -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        _target, allocation = _leased(repository, "hosted", session_id="session-a")
        monkeypatch.setenv("CONTROLLER_WRITER_MODE", "host-managed-v2")
        hosted = _capture(capsys, "--path", str(repository.repo), *_selector("session-a"))
        assert hosted["decision"]["code"] == "NO_TERMINAL_ACTION"
        assert "hosted child" in hosted["decision"]["reason"]
        monkeypatch.delenv("CONTROLLER_WRITER_MODE")
        partial = _capture(
            capsys, "--path", str(repository.repo), "--client-id", "claude", "--session-id", "x"
        )
        assert partial["decision"]["code"] == "NO_TERMINAL_ACTION"
        with_owner = _capture(
            capsys, "--path", str(repository.repo), *_selector("session-a"), "--owner", "owner-b"
        )
        assert with_owner["decision"]["code"] == "NO_TERMINAL_ACTION"
        assert _record(repository, allocation).lifecycle == "ACTIVE"
        assert allocation in MetadataStore(repository.repo / ".git").load().writer_leases
