"""A successful exit is not proof that any or every checkout was settled."""

import json
import os

import pytest

from tests.test_controlled import acquire_one, adopt_canonical, handoff_one
from tests.test_retention_admission import POLICY_V5, _record, _store
from tests.test_shadow import GitRepository, git
from worktree_controller import session
from worktree_controller.lifecycle import park, release
from worktree_controller.models import ControllerError
from worktree_controller.writer_lease import acquire_writer_lease


def leased(repository, task, run="run-a"):
    path, result = acquire_one(repository, task=task, authority=task, owner="owner-b")
    acquire_writer_lease(repository.repo, target=path, owner="owner-b", client_id="test",
                         holder_pid=os.getpid(), session_id="session", run_id=run)
    return path, result["decision"]["allocation_id"]


def call(capsys, repository, *arguments, expected=0):
    assert session.main(["--path", str(repository.repo), "--client-id", "test",
                         "--session-id", "session", "--holder", str(os.getpid()),
                         *arguments]) == expected
    return json.loads(capsys.readouterr().out)


def test_prepare_is_readonly_and_enumerates_every_exact_run_checkout(capsys):
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        first, first_id = leased(repository, "first")
        _, second_id = leased(repository, "second")
        _, replacement_id = leased(repository, "replacement", "run-b")
        (first / "unknown.txt").write_text("preserve me")
        before = _store(repository).load()
        result = call(capsys, repository, "--check", "--run-id", "run-a", expected=2)
        assert {row["allocation_id"] for row in result["allocations"]} == {first_id, second_id}
        assert result["summary"]["pending"] == 2
        assert result["summary"]["terminal_complete"] is False
        assert _store(repository).load() == before
        assert replacement_id in before.writer_leases
        assert (first / "unknown.txt").read_text() == "preserve me"


def test_run_mismatch_never_claims_an_empty_success_or_releases_replacement(capsys):
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        _, allocation = leased(repository, "task", "new-run")
        before = _store(repository).load()
        result = call(capsys, repository, "--require-complete", expected=2)
        assert result["decision"]["code"] == "SESSION_SELECTOR_MISMATCH"
        assert result["summary"]["terminal_complete"] is False
        assert result["allocations"] == []
        assert _store(repository).load() == before
        assert allocation in before.writer_leases


def test_hosted_child_can_check_every_checkout_but_never_end_its_parent(capsys, monkeypatch):
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        _, first = leased(repository, "first")
        _, second = leased(repository, "second")
        before = _store(repository).load()
        monkeypatch.setenv("CONTROLLER_WRITER_MODE", "host-managed-v2")
        result = call(capsys, repository, "--check", "--run-id", "run-a", expected=2)
        assert {row["allocation_id"] for row in result["allocations"]} == {first, second}
        assert result["summary"]["pending"] == 2
        assert _store(repository).load() == before
        ended = call(capsys, repository, "--run-id", "run-a", "--require-complete", expected=2)
        assert ended["summary"]["terminal_complete"] is False
        assert "hosted child" in ended["decision"]["reason"]
        assert _store(repository).load() == before


def test_partial_end_reports_each_blocker_and_keeps_owner_cas(capsys, monkeypatch):
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        first, first_id = leased(repository, "dirty")
        second, second_id = leased(repository, "blocked")
        (first / "unknown.txt").write_text("preserve me")
        original = session.reconcile_session

        def reconcile(*args, **kwargs):
            if kwargs["target"] == second:
                raise ControllerError("FIXTURE_BLOCKED", "owner must recover")
            return original(*args, **kwargs)

        monkeypatch.setattr(session, "reconcile_session", reconcile)
        result = call(capsys, repository, "--run-id", "run-a", "--require-complete", expected=2)
        assert result["decision"]["code"] == "SESSION_ENDED"
        assert result["summary"]["pending"] == 2
        rows = {row["allocation_id"]: row for row in result["allocations"]}
        dirty = rows[first_id]
        assert dirty["decision"]["code"] == "RETAINED"
        action = dirty["owner_action"]
        assert action["owner"] == "owner-b" and action["allocation_id"] == first_id
        assert action["expected_record_version"] == _record(repository, first_id).record_version
        assert action["execution_authority"] is False and action["automatic_discard"] is False
        assert rows[second_id]["decision"]["blocker"] == "FIXTURE_BLOCKED"
        assert (first / "unknown.txt").read_text() == "preserve me"
        repeated = call(capsys, repository, "--run-id", "run-a", "--require-complete", expected=2)
        assert repeated["allocations"] == []
        assert {row["allocation_id"] for row in repeated["previous_allocations"]} == {first_id, second_id}
        assert repeated["summary"]["pending"] == 2


def test_park_checkpoint_pins_head_and_survives_wait_without_claiming_completion():
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        target, result = acquire_one(repository, task="wait", authority="wait", owner="owner-b")
        allocation = result["decision"]["allocation_id"]
        head = git(target, "rev-parse", "HEAD")
        with pytest.raises(ControllerError, match="checkpoint HEAD"):
            park(repository.repo, target=target, owner="owner-b", expected_record_version=1,
                 checkpoint_id="art_" + "a" * 64, checkpoint_head="b" * 40, waiting_on="next data window")
        assert _record(repository, allocation).record_version == 1
        parked = park(repository.repo, target=target, owner="owner-b", expected_record_version=1,
                      checkpoint_id="art_" + "a" * 64, checkpoint_head=head, waiting_on="next data window")
        checkpoint = parked["decision"]["checkpoint"]
        assert checkpoint["head"] == head and checkpoint["task_completed"] is False
        item = next(row for row in parked["worktrees"] if row["allocation_id"] == allocation)
        assert item["wait_checkpoint"] == checkpoint
        assert target.exists()


def test_expected_allocation_cannot_disappear_from_a_strict_check(capsys):
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        _, selected = leased(repository, "selected")
        _, other = leased(repository, "other", "other-run")
        before = _store(repository).load()
        result = call(capsys, repository, "--check", "--run-id", "run-a",
                      "--expected-allocation-id", selected, "--expected-allocation-id", other,
                      expected=2)
        assert result["summary"]["selected"] == result["summary"]["pending"] == 2
        assert result["expected_allocations"][0]["allocation_id"] == other
        assert result["expected_allocations"][0]["terminal_complete"] is False
        assert _store(repository).load() == before
        assert result["allocations"][0]["recovery_argv"][1] == "recovery-guide"


def test_missing_lease_and_empty_selection_do_not_complete_a_known_live_allocation(capsys):
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        _, acquired = acquire_one(repository)
        allocation = acquired["decision"]["allocation_id"]
        result = call(capsys, repository, "--require-complete", "--expected-allocation-id", allocation,
                      expected=2)
        assert not result["allocations"]
        assert result["summary"]["terminal_complete"] is False
        assert result["expected_allocations"][0]["inspect_argv"][1] == "lifecycle-history"
        assert _record(repository, allocation).lifecycle == "ACTIVE"


def test_expected_released_allocation_requires_complete_immutable_proof(capsys):
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        target, acquired = acquire_one(repository)
        allocation = acquired["decision"]["allocation_id"]
        handoff_one(repository, target)
        released = release(repository.repo, target=target, expected_record_version=2,
                           owner_release="module-owner")["decision"]
        result = call(capsys, repository, "--check", "--expected-allocation-id", allocation)
        assert result["summary"]["terminal_complete"] is True
        assert result["summary"]["selected"] == 1
        assert result["expected_allocations"][0]["decision"]["release_receipt_id"] == released["release_receipt_id"]
        assert result["summary"]["task_completed"] is False
        store = _store(repository)
        # Directory absence still cannot replace altered or incomplete historical evidence.
        path = store.release_receipts_dir / f"{released['release_receipt_id']}.json"
        path.unlink()
        failed = call(capsys, repository, "--check", "--expected-allocation-id", allocation, expected=2)
        assert failed["summary"]["terminal_complete"] is False


def test_frozen_handoff_head_drift_fails_readonly_preparation(capsys):
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        target, acquired = acquire_one(repository)
        acquire_writer_lease(repository.repo, target=target, owner="module-owner", client_id="test",
                             holder_pid=os.getpid(), session_id="session", run_id="run-a")
        handoff_one(repository, target)
        ready = call(capsys, repository, "--check", "--run-id", "run-a")
        assert ready["summary"]["terminal_complete"] is True
        (target / "extra.txt").write_text("unexpected clean commit")
        git(target, "add", "extra.txt")
        git(target, "commit", "-m", "move HEAD after handoff")
        before = _store(repository).load()
        result = call(capsys, repository, "--check", "--run-id", "run-a", expected=2)
        assert result["allocations"][0]["decision"]["blocker"] == "FROZEN_HANDOFF_DRIFT"
        assert result["allocations"][0]["allocation_id"] == acquired["decision"]["allocation_id"]
        assert _store(repository).load() == before


def test_historical_inspection_failure_remains_a_per_allocation_result(capsys, monkeypatch):
    from worktree_controller import lifecycle_history

    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        _, allocation = leased(repository, "task")
        call(capsys, repository, "--run-id", "run-a", expected=0)

        def missing(_):
            raise ControllerError("FIXTURE_UNAVAILABLE", "inspection unavailable")

        def unavailable(*args, **kwargs):
            raise ControllerError("FIXTURE_HISTORY_UNAVAILABLE", "history unavailable")

        monkeypatch.setattr(session, "_resolve", missing)
        monkeypatch.setattr(lifecycle_history, "lifecycle_history", unavailable)
        result = call(capsys, repository, "--run-id", "run-a", "--require-complete", expected=2)
        assert result["previous_allocations"][0]["allocation_id"] == allocation
        assert result["previous_allocations"][0]["terminal_complete"] is False
        assert result["summary"]["pending"] == 1
