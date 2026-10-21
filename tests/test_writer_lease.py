"""Writer leases, and the reconciliation they make safe."""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from tests.test_controlled import acquire_one, adopt_canonical
from tests.test_retention_admission import POLICY_V5, _record, _store, _version
from tests.test_shadow import GitRepository
from worktree_controller import writer_lease
from worktree_controller.controller import status
from worktree_controller.lifecycle import (
    _updated_record,
    acquire,
    park,
    update_external_binding,
)
from worktree_controller.models import ControllerError, WriterHolder
from worktree_controller.policy import load_policy
from worktree_controller.reconciler import reconcile_writers
from worktree_controller.writer_lease import (
    acquire_writer_lease,
    boot_id,
    describe_holder,
    holder_is_live,
    paths_in_use,
    release_writer_lease,
    start_ticks,
)

POLICY_V6 = (
    POLICY_V5.replace("schema_version = 5", "schema_version = 6")
    + """
[reconciler]
enabled = true
actor = "lifecycle-reconciler"
allow_legacy_adoption = false
"""
)

POLICY_V6_LEGACY = POLICY_V6.replace(
    "allow_legacy_adoption = false", "allow_legacy_adoption = true"
)


def _dead_pid() -> int:
    """A pid that has certainly exited, for 'holder is gone' cases."""
    done = subprocess.Popen(["true"])
    done.wait()
    return done.pid


def _live_child() -> subprocess.Popen[bytes]:
    return subprocess.Popen(["sleep", "60"])


def _writer(repository, task: str, owner: str = "owner-b"):
    """Acquire one writer allocation and return (path, allocation_id)."""
    target, payload = acquire_one(repository, task=task, authority=task, owner=owner)
    return target, payload["decision"]["allocation_id"]


# --------------------------------------------------------------------------- #
# Holder identity
# --------------------------------------------------------------------------- #


def test_holder_identity_survives_pid_reuse() -> None:
    holder = describe_holder(os.getpid(), client_id="manual")
    assert holder_is_live(holder)
    impostor = WriterHolder(
        client_id=holder.client_id,
        pid=holder.pid,
        boot_id=holder.boot_id,
        start_ticks=holder.start_ticks + 1,
        session_id=None,
        run_id=None,
    )
    assert not holder_is_live(impostor)


def test_holder_identity_does_not_survive_a_reboot() -> None:
    holder = describe_holder(os.getpid(), client_id="manual")
    rebooted = WriterHolder(
        client_id=holder.client_id,
        pid=holder.pid,
        boot_id="00000000-0000-0000-0000-000000000000",
        start_ticks=holder.start_ticks,
        session_id=None,
        run_id=None,
    )
    assert boot_id() != rebooted.boot_id
    assert not holder_is_live(rebooted)


def test_describing_a_dead_process_is_refused() -> None:
    with pytest.raises(ControllerError) as error:
        describe_holder(_dead_pid(), client_id="executor-a")
    assert error.value.code == "INVALID_HOLDER"


def test_start_ticks_is_absent_for_a_dead_process() -> None:
    assert start_ticks(_dead_pid()) is None


# --------------------------------------------------------------------------- #
# Lease lifecycle
# --------------------------------------------------------------------------- #


def test_lease_acquire_renew_and_release_leave_the_allocation_alone() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, allocation = _writer(repository, "w1")
        before = _record(repository, allocation)

        first = acquire_writer_lease(
            repository.repo,
            target=target,
            owner="owner-b",
            client_id="executor-a",
            holder_pid=os.getpid(),
        )["decision"]
        assert first["code"] == "WRITER_LEASE_ACQUIRED"

        again = acquire_writer_lease(
            repository.repo,
            target=target,
            owner="owner-b",
            client_id="executor-a",
            holder_pid=os.getpid(),
        )["decision"]
        assert again["code"] == "WRITER_LEASE_RENEWED"

        released = release_writer_lease(
            repository.repo,
            target=target,
            holder=describe_holder(os.getpid(), client_id="executor-a"),
        )["decision"]
        assert released["code"] == "WRITER_LEASE_RELEASED"

        after = _record(repository, allocation)
        assert (after.lifecycle, after.owner, after.record_version) == (
            before.lifecycle,
            before.owner,
            before.record_version,
        )


def test_lease_refuses_a_foreign_owner() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, _ = _writer(repository, "w1")
        with pytest.raises(ControllerError) as error:
            acquire_writer_lease(
                repository.repo,
                target=target,
                owner="owner-a",
                client_id="executor-a",
                holder_pid=os.getpid(),
            )
        assert error.value.code == "OWNER_MISMATCH"


def test_a_second_live_writer_is_refused() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, _ = _writer(repository, "w1")
        child = _live_child()
        try:
            acquire_writer_lease(
                repository.repo,
                target=target,
                owner="owner-b",
                client_id="executor-a",
                holder_pid=child.pid,
            )
            with pytest.raises(ControllerError) as error:
                acquire_writer_lease(
                    repository.repo,
                    target=target,
                    owner="owner-b",
                    client_id="executor-b",
                    holder_pid=os.getpid(),
                )
            assert error.value.code == "WRITER_LEASE_HELD"
        finally:
            child.kill()
            child.wait()


def test_a_dead_holder_can_be_taken_over() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, _ = _writer(repository, "w1")
        child = _live_child()
        acquire_writer_lease(
            repository.repo,
            target=target,
            owner="owner-b",
            client_id="executor-a",
            holder_pid=child.pid,
        )
        child.kill()
        child.wait()
        taken = acquire_writer_lease(
            repository.repo,
            target=target,
            owner="owner-b",
            client_id="executor-b",
            holder_pid=os.getpid(),
        )["decision"]
        assert taken["code"] == "WRITER_LEASE_TAKEN_OVER"


def test_lease_release_rejects_a_mismatched_holder() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, _ = _writer(repository, "w1")
        acquire_writer_lease(
            repository.repo,
            target=target,
            owner="owner-b",
            client_id="executor-a",
            holder_pid=os.getpid(),
        )
        with pytest.raises(ControllerError) as error:
            release_writer_lease(repository.repo, target=target, holder_pid=os.getpid() + 100000)
        assert error.value.code == "WRITER_LEASE_MISMATCH"


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #


def _outcome(payload: dict, task: str) -> dict:
    for finding in payload["decision"]["findings"]:
        if finding.get("task_key") == task:
            return finding
    raise AssertionError(f"no finding for {task}")


def test_a_dead_writer_frees_its_slot_without_completing_the_task() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, allocation = _writer(repository, "w1")
        (target / "unfinished.txt").write_text("half-written work", encoding="utf-8")
        child = _live_child()
        acquire_writer_lease(
            repository.repo,
            target=target,
            owner="owner-b",
            client_id="executor-a",
            holder_pid=child.pid,
        )
        child.kill()
        child.wait()

        payload = reconcile_writers(
            repository.repo, actor="lifecycle-reconciler", dry_run=False, trigger="background"
        )
        assert payload["decision"]["reclaimed"] == 1
        record = _record(repository, allocation)
        assert record.lifecycle == "RETAINED"
        # the task itself is untouched: same owner, same work, checkout preserved
        assert record.owner == "owner-b"
        assert (target / "unfinished.txt").read_text(encoding="utf-8") == "half-written work"
        assert target.is_dir()


def test_a_live_writer_is_never_reclaimed() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, allocation = _writer(repository, "w1")
        child = _live_child()
        try:
            acquire_writer_lease(
                repository.repo,
                target=target,
                owner="owner-b",
                client_id="executor-a",
                holder_pid=child.pid,
            )
            payload = reconcile_writers(
                repository.repo, actor="lifecycle-reconciler", dry_run=False
            )
            assert payload["decision"]["reclaimed"] == 0
            assert _outcome(payload, "w1")["outcome"] == "LIVE_WRITER"
            assert _record(repository, allocation).lifecycle == "ACTIVE"
        finally:
            child.kill()
            child.wait()


def test_a_surviving_child_keeps_the_slot_after_the_wrapper_dies() -> None:
    """A wrapper can die while the real work continues in its process group."""
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, allocation = _writer(repository, "w1")
        # a shell that spawns a child in its own process group, then exits
        parent = subprocess.Popen(
            ["bash", "-c", "sleep 60 & echo $$; wait"],
            stdout=subprocess.PIPE,
            start_new_session=True,
        )
        assert parent.stdout is not None
        parent.stdout.readline()
        group = os.getpgid(parent.pid)
        acquire_writer_lease(
            repository.repo,
            target=target,
            owner="owner-b",
            client_id="executor-a",
            holder_pid=parent.pid,
        )
        parent.kill()
        parent.wait()
        time.sleep(0.2)
        try:
            payload = reconcile_writers(
                repository.repo, actor="lifecycle-reconciler", dry_run=False
            )
            finding = _outcome(payload, "w1")
            assert finding["outcome"] == "LIVE_WRITER"
            assert "holder_process_group=alive" in finding["evidence"]
            assert _record(repository, allocation).lifecycle == "ACTIVE"
            with pytest.raises(ControllerError) as refused:
                acquire_writer_lease(
                    repository.repo, target=target, client_id="manual", holder_pid=os.getpid()
                )
            assert refused.value.code == "WRITER_LEASE_HELD"
        finally:
            subprocess.run(["pkill", "-g", str(group)], check=False)


def test_an_unleased_checkout_in_use_is_never_reclaimed() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, allocation = _writer(repository, "w1")
        child = subprocess.Popen(["sleep", "60"], cwd=target)
        try:
            payload = reconcile_writers(
                repository.repo, actor="lifecycle-reconciler", dry_run=False
            )
            assert _outcome(payload, "w1")["outcome"] == "LIVE_WRITER"
            assert _record(repository, allocation).lifecycle == "ACTIVE"
        finally:
            child.kill()
            child.wait()


def test_a_legacy_allocation_is_preserved_fail_closed() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        _target, allocation = _writer(repository, "w1")
        payload = reconcile_writers(repository.repo, actor="lifecycle-reconciler", dry_run=False)
        assert payload["decision"]["reclaimed"] == 0
        assert _outcome(payload, "w1")["outcome"] == "LEGACY_UNVERIFIED"
        assert _record(repository, allocation).lifecycle == "ACTIVE"


def test_legacy_adoption_requires_both_the_flag_and_the_policy() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        _target, allocation = _writer(repository, "w1")
        with pytest.raises(ControllerError) as error:
            reconcile_writers(
                repository.repo,
                actor="lifecycle-reconciler",
                dry_run=False,
                adopt_legacy=True,
            )
        assert error.value.code == "LEGACY_ADOPTION_DISABLED"
        assert _record(repository, allocation).lifecycle == "ACTIVE"

    with GitRepository(linked=0, policy_text=POLICY_V6_LEGACY) as repository:
        adopt_canonical(repository)
        _target, allocation = _writer(repository, "w1")
        payload = reconcile_writers(
            repository.repo,
            actor="lifecycle-reconciler",
            dry_run=False,
            adopt_legacy=True,
        )
        assert payload["decision"]["reclaimed"] == 1
        assert _record(repository, allocation).lifecycle == "RETAINED"


def test_dry_run_reports_without_mutating() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, allocation = _writer(repository, "w1")
        child = _live_child()
        acquire_writer_lease(
            repository.repo,
            target=target,
            owner="owner-b",
            client_id="executor-a",
            holder_pid=child.pid,
        )
        child.kill()
        child.wait()
        before = _version(repository, allocation)
        payload = reconcile_writers(repository.repo, actor="lifecycle-reconciler", dry_run=True)
        assert _outcome(payload, "w1")["outcome"] == "RECLAIMABLE"
        assert payload["decision"]["reclaimed"] == 0
        assert payload["decision"]["writers_before"] == 1
        assert _version(repository, allocation) == before
        assert _record(repository, allocation).lifecycle == "ACTIVE"


def test_reconciliation_is_idempotent() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, allocation = _writer(repository, "w1")
        child = _live_child()
        acquire_writer_lease(
            repository.repo,
            target=target,
            owner="owner-b",
            client_id="executor-a",
            holder_pid=child.pid,
        )
        child.kill()
        child.wait()
        first = reconcile_writers(repository.repo, actor="lifecycle-reconciler", dry_run=False)
        second = reconcile_writers(repository.repo, actor="lifecycle-reconciler", dry_run=False)
        third = reconcile_writers(repository.repo, actor="lifecycle-reconciler", dry_run=False)
        assert first["decision"]["reclaimed"] == 1
        assert second["decision"]["reclaimed"] == 0
        assert third["decision"]["reclaimed"] == 0
        assert _record(repository, allocation).record_version == _version(repository, allocation)


def test_reconciliation_writes_an_auditable_receipt() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, allocation = _writer(repository, "w1")
        child = _live_child()
        acquire_writer_lease(
            repository.repo,
            target=target,
            owner="owner-b",
            client_id="executor-a",
            holder_pid=child.pid,
        )
        child.kill()
        child.wait()
        reconcile_writers(repository.repo, actor="lifecycle-reconciler", dry_run=False)
        receipts = list((_store(repository).writer_receipts_dir).glob("*.json"))
        assert len(receipts) == 1
        import json

        receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
        assert receipt["actor"] == "lifecycle-reconciler"
        assert receipt["owner"] == "owner-b"
        assert receipt["allocation_id"] == allocation
        assert receipt["previous_lifecycle"] == "ACTIVE"
        assert receipt["new_lifecycle"] == "RETAINED"
        assert receipt["trigger"] == "manual"
        assert any("holder_process=gone" in item for item in receipt["evidence"])


# --------------------------------------------------------------------------- #
# Negative gates: everything that must still block reclamation
# --------------------------------------------------------------------------- #


def _dead_lease(repository, target):
    child = _live_child()
    acquire_writer_lease(
        repository.repo,
        target=target,
        owner="owner-b",
        client_id="executor-a",
        holder_pid=child.pid,
    )
    child.kill()
    child.wait()


def test_an_external_binding_blocks_reclamation() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, allocation = _writer(repository, "w1")
        _dead_lease(repository, target)
        update_external_binding(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, allocation),
            owner="owner-b",
            binding="systemd-user:demo.service",
            action="add",
        )
        payload = reconcile_writers(repository.repo, actor="lifecycle-reconciler", dry_run=False)
        finding = _outcome(payload, "w1")
        assert finding["outcome"] == "BLOCKED"
        assert "external" in finding["reason"]
        assert _record(repository, allocation).lifecycle == "ACTIVE"


def test_a_retained_allocation_is_left_alone() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, allocation = _writer(repository, "w1")
        park(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, allocation),
            owner="owner-b",
        )
        payload = reconcile_writers(repository.repo, actor="lifecycle-reconciler", dry_run=False)
        assert _outcome(payload, "w1")["outcome"] == "OUT_OF_WRITER_POOL"
        assert payload["decision"]["reclaimed"] == 0


def test_a_retained_heavy_pool_no_longer_strands_a_dead_writer() -> None:
    """Retention costs no capacity, so a full retained count cannot block a reclaim."""

    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        held = []
        for index in range(3):
            target, allocation = _writer(repository, f"w{index}")
            held.append((target, allocation))
        for target, allocation in held[:2]:
            park(
                repository.repo,
                target=target,
                expected_record_version=_version(repository, allocation),
                owner="owner-b",
            )
        target, allocation = held[2]
        _dead_lease(repository, target)
        payload = reconcile_writers(repository.repo, actor="lifecycle-reconciler", dry_run=False)
        finding = _outcome(payload, "w2")
        assert finding["outcome"] == "RECLAIMED"
        assert _record(repository, allocation).lifecycle == "RETAINED"
        # the checkout itself is untouched: retention is not a release
        assert target.is_dir()


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #


def test_convergence_requires_the_delegated_actor() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, allocation = _writer(repository, "w1")
        _dead_lease(repository, target)
        for impostor in ("owner-a", "owner-b", "someone-else"):
            with pytest.raises(ControllerError) as error:
                reconcile_writers(repository.repo, actor=impostor, dry_run=False)
            assert error.value.code == "RECONCILER_ACTOR_MISMATCH"
        assert _record(repository, allocation).lifecycle == "ACTIVE"


def test_a_task_owner_cannot_be_declared_the_reconciler() -> None:
    for impostor in ("owner-a", "owner-b", "arbitrary-task-owner"):
        text = POLICY_V6.replace('actor = "lifecycle-reconciler"', f'actor = "{impostor}"')
        with GitRepository(linked=0, policy_text=text) as repository:
            adopt_canonical(repository)
            _writer(repository, "owned-task", owner=impostor)
            with pytest.raises(ControllerError) as error:
                reconcile_writers(repository.repo, actor=impostor, dry_run=False)
            assert error.value.code == "INVALID_POLICY"


def test_a_policy_without_delegation_never_converges() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        target, allocation = _writer(repository, "w1")
        _dead_lease(repository, target)
        # read-only reporting still works, so an operator can see the state
        report = reconcile_writers(repository.repo, actor="lifecycle-reconciler", dry_run=True)
        assert _outcome(report, "w1")["outcome"] == "RECLAIMABLE"
        with pytest.raises(ControllerError) as error:
            reconcile_writers(repository.repo, actor="lifecycle-reconciler", dry_run=False)
        assert error.value.code == "RECONCILER_DISABLED"
        assert _record(repository, allocation).lifecycle == "ACTIVE"


def test_policy_below_the_retention_schema_is_untouched() -> None:
    text = (
        POLICY_V5.replace("schema_version = 5", "schema_version = 4")
        .replace("retained = 2\n", "")
        .replace("hard_limit = 9", "hard_limit = 7")
    )
    with GitRepository(linked=0, policy_text=text) as repository:
        policy = load_policy(repository.repo)
        assert policy.policy_schema_version == 4
        assert policy.retained_slots == 0
        assert policy.reconciler_enabled is False


# --------------------------------------------------------------------------- #
# Admission-triggered reconciliation
# --------------------------------------------------------------------------- #


def test_admission_is_independent_of_periodic_dead_writer_reconciliation() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        held = []
        for index in range(3):
            target, allocation = _writer(repository, f"w{index}")
            held.append((target, allocation))
        # the writer pool (3) is full; one of them is held by a process that died
        _dead_lease(repository, held[0][0])
        children = []
        for target, _allocation in held[1:]:
            child = _live_child()
            children.append(child)
            acquire_writer_lease(
                repository.repo,
                target=target,
                owner="owner-b",
                client_id="executor-a",
                holder_pid=child.pid,
            )
        payload = acquire(
            repository.repo,
            task_key="newcomer",
            authority_key="newcomer",
            module="module",
            role="feature",
            owner="owner-b",
            branch="feature/newcomer",
            base_ref="HEAD",
            target=repository.root / "newcomer",
        )
        assert payload["decision"]["code"] == "ACQUIRED"
        assert _record(repository, held[0][1]).lifecycle == "ACTIVE"
        reconcile_writers(repository.repo, actor="lifecycle-reconciler", dry_run=False)
        assert _record(repository, held[0][1]).lifecycle == "RETAINED"
        assert _record(repository, held[1][1]).lifecycle == "ACTIVE"
        for child in children:
            child.terminate()
            child.wait()


def test_independent_admission_preserves_every_existing_live_writer() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        children = []
        for index in range(3):
            target, _allocation = _writer(repository, f"w{index}")
            child = _live_child()
            children.append(child)
            acquire_writer_lease(
                repository.repo,
                target=target,
                owner="owner-b",
                client_id="executor-a",
                holder_pid=child.pid,
            )
        try:
            decision = acquire(
                repository.repo, task_key="newcomer", authority_key="newcomer",
                module="module", role="feature", owner="owner-b",
                branch="feature/newcomer", base_ref="HEAD", target=repository.root / "newcomer",
            )["decision"]
            assert decision["code"] == "ACQUIRED"
            assert all(child.poll() is None for child in children)
        finally:
            for child in children:
                child.kill()
                child.wait()


def test_admission_never_borrows_reserved_capacity() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        for index in range(3):
            target, _allocation = _writer(repository, f"w{index}")
            _dead_lease(repository, target)
        payload = status(repository.repo)["repository"]
        emergency_before = payload["emergency_limit"]
        acquire(
            repository.repo,
            task_key="newcomer",
            authority_key="newcomer",
            module="module",
            role="feature",
            owner="owner-b",
            branch="feature/newcomer",
            base_ref="HEAD",
            target=repository.root / "newcomer",
        )
        after = status(repository.repo)
        roles = [w.get("role") for w in after["worktrees"]]
        assert roles.count("emergency") == 0 and roles.count("recovery") == 0
        assert after["repository"]["emergency_limit"] == emergency_before


# --------------------------------------------------------------------------- #
# Races, resume, and expiry
# --------------------------------------------------------------------------- #


def test_reconciliation_and_admission_race_safely() -> None:
    """Concurrent background reconcile and acquire must not double-spend a slot."""
    import threading

    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        held = []
        for index in range(3):
            target, allocation = _writer(repository, f"w{index}")
            held.append((target, allocation))
            _dead_lease(repository, target)

        results: list[object] = []
        barrier = threading.Barrier(4)

        def reconcile() -> None:
            barrier.wait()
            try:
                results.append(
                    reconcile_writers(
                        repository.repo,
                        actor="lifecycle-reconciler",
                        trigger="background",
                        dry_run=False,
                    )["decision"]["reclaimed"]
                )
            except ControllerError as error:
                results.append(error.code)

        def admit(name: str) -> None:
            barrier.wait()
            try:
                acquire(
                    repository.repo,
                    task_key=name,
                    authority_key=name,
                    module="module",
                    role="feature",
                    owner="owner-b",
                    branch=f"feature/{name}",
                    base_ref="HEAD",
                    target=repository.root / name,
                )
                results.append(f"acquired:{name}")
            except ControllerError as error:
                results.append(error.code)

        threads = [
            threading.Thread(target=reconcile),
            threading.Thread(target=admit, args=("n1",)),
            threading.Thread(target=admit, args=("n2",)),
            threading.Thread(target=admit, args=("n3",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            assert not thread.is_alive()

        payload = status(repository.repo)
        records = [
            w
            for w in payload["worktrees"]
            if w.get("role") in {"feature", "audit"} and w["lifecycle"] != "RETAINED"
        ]
        # whatever interleaving happened, the writer pool is never oversubscribed
        assert len(records) <= 3
        assert all(w["lifecycle"] in {"ACTIVE", "RETAINED"} for w in payload["worktrees"][1:])


def test_repeated_background_passes_never_oversubscribe_retention() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        for index in range(3):
            target, _allocation = _writer(repository, f"w{index}")
            _dead_lease(repository, target)
        total = 0
        for _ in range(4):
            total += reconcile_writers(
                repository.repo,
                actor="lifecycle-reconciler",
                trigger="background",
                dry_run=False,
            )["decision"]["reclaimed"]
        # every dead writer converges, and repeated passes stay idempotent
        assert total == 3
        payload = status(repository.repo)
        retained = [w for w in payload["worktrees"] if w["lifecycle"] == "RETAINED"]
        assert len(retained) == 3
        assert payload["repository"]["ordinary_physical"] == 3
        assert payload["repository"]["writers_used"] == 0


def test_a_resumed_task_takes_a_fresh_lease() -> None:
    from worktree_controller.lifecycle import resume

    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, allocation = _writer(repository, "w1")
        (target / "work.txt").write_text("preserved", encoding="utf-8")
        _dead_lease(repository, target)
        reconcile_writers(repository.repo, actor="lifecycle-reconciler", dry_run=False)
        assert _record(repository, allocation).lifecycle == "RETAINED"

        # a retained task cannot take a lease until its owner resumes it
        with pytest.raises(ControllerError) as error:
            acquire_writer_lease(
                repository.repo,
                target=target,
                owner="owner-b",
                client_id="executor-b",
                holder_pid=os.getpid(),
            )
        assert error.value.code == "INVALID_LIFECYCLE"

        resume(
            repository.repo,
            target=target,
            expected_record_version=_version(repository, allocation),
            owner="owner-b",
        )
        decision = acquire_writer_lease(
            repository.repo,
            target=target,
            owner="owner-b",
            client_id="executor-b",
            holder_pid=os.getpid(),
        )["decision"]
        assert decision["code"] == "WRITER_LEASE_ACQUIRED"
        assert (target / "work.txt").read_text(encoding="utf-8") == "preserved"


def test_expiry_alone_never_reclaims_a_living_writer() -> None:
    """A session waiting for user input keeps its slot even past its ttl."""
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, allocation = _writer(repository, "w1")
        child = _live_child()
        try:
            acquire_writer_lease(
                repository.repo,
                target=target,
                owner="owner-b",
                client_id="executor-b",
                holder_pid=child.pid,
                ttl_seconds=1,
            )
            time.sleep(1.5)
            payload = reconcile_writers(
                repository.repo, actor="lifecycle-reconciler", dry_run=False
            )
            assert _outcome(payload, "w1")["outcome"] == "LIVE_WRITER"
            assert _record(repository, allocation).lifecycle == "ACTIVE"
        finally:
            child.kill()
            child.wait()


def test_paths_in_use_sees_an_open_file_not_only_a_cwd() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, _allocation = _writer(repository, "w1")
        held = target / "held.txt"
        held.write_text("x", encoding="utf-8")
        with held.open("rb") as handle:
            child = subprocess.Popen(["sleep", "60"], stdin=handle)
        try:
            time.sleep(0.2)
            assert any(pid == child.pid for pid, _cmd in paths_in_use(target))
        finally:
            child.kill()
            child.wait()


def test_a_wrapper_without_a_declared_owner_still_binds_entitlement() -> None:
    """A process-boundary wrapper cannot know the owner string; the checkout is the proof."""
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, allocation = _writer(repository, "w1", owner="owner-a")
        decision = acquire_writer_lease(
            repository.repo, target=target, client_id="executor-a", holder_pid=os.getpid()
        )["decision"]
        assert decision["code"] == "WRITER_LEASE_ACQUIRED"
        # ownership is recorded from the allocation and is never rewritten
        assert decision["owner"] == "owner-a"
        assert _record(repository, allocation).owner == "owner-a"


def test_capacity_arithmetic_counts_only_registered_checkouts() -> None:
    """Released tasks stay in metadata forever; they are not occupancy."""
    from worktree_controller.lifecycle import complete_no_change, release

    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        gone, gone_allocation = _writer(repository, "gone")
        complete_no_change(
            repository.repo,
            target=gone,
            expected_record_version=_version(repository, gone_allocation),
            owner="owner-b",
        )
        release(
            repository.repo,
            target=gone,
            expected_record_version=_version(repository, gone_allocation),
            owner_release="owner-b",
        )
        _writer(repository, "here")

        report = reconcile_writers(repository.repo, actor="lifecycle-reconciler", dry_run=True)
        # one live writer, not two: the released record is history, not capacity
        assert report["decision"]["writers_before"] == 1
        assert report["decision"]["legacy_unverified"] == 1


def test_selective_classification_leaves_unnamed_allocations_untouched() -> None:
    """A legacy sweep must be able to exclude anything tied to a live session."""
    with GitRepository(linked=0, policy_text=POLICY_V6_LEGACY) as repository:
        adopt_canonical(repository)
        chosen, chosen_allocation = _writer(repository, "chosen")
        spared, spared_allocation = _writer(repository, "spared")

        payload = reconcile_writers(
            repository.repo,
            actor="lifecycle-reconciler",
            dry_run=False,
            adopt_legacy=True,
            only=("chosen",),
        )
        assert payload["decision"]["reclaimed"] == 1
        assert payload["decision"]["selection"] == ["chosen"]
        assert _record(repository, chosen_allocation).lifecycle == "RETAINED"
        assert _record(repository, spared_allocation).lifecycle == "ACTIVE"
        assert _outcome(payload, "spared")["outcome"] == "LEGACY_UNVERIFIED"


def test_selection_also_holds_back_a_provably_dead_writer() -> None:
    """Selection is an explicit allow-list, not merely a legacy filter."""
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, allocation = _writer(repository, "dead")
        _dead_lease(repository, target)
        payload = reconcile_writers(
            repository.repo, actor="lifecycle-reconciler", dry_run=False, only=("someone-else",)
        )
        assert payload["decision"]["reclaimed"] == 0
        finding = _outcome(payload, "dead")
        assert finding["outcome"] == "BLOCKED"
        assert "explicit task selection" in finding["reason"]
        assert _record(repository, allocation).lifecycle == "ACTIVE"


def test_dry_run_previews_the_legacy_classification_without_mutating() -> None:
    """The preview matters most exactly where a mutation is hardest to undo."""
    with GitRepository(linked=0, policy_text=POLICY_V6_LEGACY) as repository:
        adopt_canonical(repository)
        _first, first_allocation = _writer(repository, "one")
        _second, second_allocation = _writer(repository, "two")

        preview = reconcile_writers(
            repository.repo,
            actor="lifecycle-reconciler",
            dry_run=True,
            adopt_legacy=True,
            only=("one",),
        )["decision"]
        assert preview["reclaimed"] == 0
        assert preview["would_reclaim"] == 1
        assert _outcome({"decision": preview}, "one")["outcome"] == "RECLAIMABLE"
        assert _outcome({"decision": preview}, "two")["outcome"] == "LEGACY_UNVERIFIED"
        assert preview["writers_before"] == 2
        # nothing moved
        assert _record(repository, first_allocation).lifecycle == "ACTIVE"
        assert _record(repository, second_allocation).lifecycle == "ACTIVE"

        applied = reconcile_writers(
            repository.repo,
            actor="lifecycle-reconciler",
            dry_run=False,
            adopt_legacy=True,
            only=("one",),
        )["decision"]
        assert applied["reclaimed"] == 1
        assert _record(repository, first_allocation).lifecycle == "RETAINED"
        assert _record(repository, second_allocation).lifecycle == "ACTIVE"


def test_dry_run_preview_matches_what_a_real_pass_would_do() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6_LEGACY) as repository:
        adopt_canonical(repository)
        for name in ("one", "two", "three"):
            _writer(repository, name)
        preview = reconcile_writers(
            repository.repo, actor="lifecycle-reconciler", dry_run=True, adopt_legacy=True
        )["decision"]
        outcomes = [f["outcome"] for f in preview["findings"] if f.get("task_key")]
        # retention takes no capacity, so all three preview as reclaimable
        assert outcomes.count("RECLAIMABLE") == 3
        assert outcomes.count("BLOCKED") == 0
        assert preview["would_reclaim"] == 3
        # and the preview mutated nothing
        assert all(
            _record(repository, str(w["allocation_id"])).lifecycle == "ACTIVE"
            for w in status(repository.repo)["worktrees"][1:]
        )


def test_dry_run_capacity_arithmetic_matches_the_real_pass() -> None:
    """A preview whose before/after numbers differ from reality is worse than none."""
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        for name in ("dead-one", "dead-two"):
            target, _allocation = _writer(repository, name)
            _dead_lease(repository, target)
        live_target, _live = _writer(repository, "alive")
        child = _live_child()
        try:
            acquire_writer_lease(
                repository.repo,
                target=live_target,
                owner="owner-b",
                client_id="executor-a",
                holder_pid=child.pid,
            )
            preview = reconcile_writers(
                repository.repo, actor="lifecycle-reconciler", dry_run=True
            )["decision"]
            assert (preview["writers_before"], preview["writers_after"]) == (3, 1)
            assert (preview["retained_before"], preview["retained_after"]) == (0, 2)
            assert preview["reclaimed"] == 0 and preview["would_reclaim"] == 2

            applied = reconcile_writers(
                repository.repo, actor="lifecycle-reconciler", dry_run=False
            )["decision"]
            # the real pass reports exactly what the preview promised
            assert (applied["writers_before"], applied["writers_after"]) == (3, 1)
            assert (applied["retained_before"], applied["retained_after"]) == (0, 2)
            assert applied["reclaimed"] == 2
        finally:
            child.kill()
            child.wait()


# --------------------------------------------------------------------------- #
# Entitlement never outlives the allocation that could use it
# --------------------------------------------------------------------------- #


def _leases(repository) -> dict:
    return _store(repository).load().writer_leases


def test_parking_drops_the_entitlement_it_frees() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, allocation = _writer(repository, "w1")
        child = _live_child()
        try:
            acquire_writer_lease(
                repository.repo,
                target=target,
                owner="owner-b",
                client_id="executor-a",
                holder_pid=child.pid,
            )
            assert allocation in _leases(repository)

            park(
                repository.repo,
                target=target,
                expected_record_version=_version(repository, allocation),
                owner="owner-b",
            )

            # The holder is still alive; the entitlement is gone because the
            # allocation that granted it no longer holds a writer slot.
            assert child.poll() is None
            assert allocation not in _leases(repository)
            assert _record(repository, allocation).lifecycle == "RETAINED"
        finally:
            child.kill()
            child.wait()


def test_reconciliation_clears_entitlement_left_by_a_finished_allocation() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, allocation = _writer(repository, "w1")
        child = _live_child()
        try:
            acquire_writer_lease(
                repository.repo,
                target=target,
                owner="owner-b",
                client_id="executor-a",
                holder_pid=child.pid,
            )
            store = _store(repository)
            with store.lock(exclusive=True):
                snapshot = store._load_unlocked()
                store.replace_record(
                    _updated_record(snapshot.records[allocation], "RELEASED", lifecycle="RELEASED")
                )
            assert allocation in _leases(repository)

            preview = reconcile_writers(
                repository.repo, actor="lifecycle-reconciler", dry_run=True, trigger="manual"
            )
            assert preview["decision"]["stranded_entitlement"] == 1
            assert preview["decision"]["stranded_entitlement_cleared"] == 0
            assert allocation in _leases(repository)

            payload = reconcile_writers(
                repository.repo, actor="lifecycle-reconciler", dry_run=False, trigger="manual"
            )
            assert payload["decision"]["stranded_entitlement_cleared"] == 1
            assert allocation not in _leases(repository)

            again = reconcile_writers(
                repository.repo, actor="lifecycle-reconciler", dry_run=False, trigger="manual"
            )
            assert again["decision"]["stranded_entitlement"] == 0
        finally:
            child.kill()
            child.wait()


def test_reconciliation_leaves_a_frozen_allocation_alone() -> None:
    """A handed-off allocation can be reopened; its lane is not the reconciler's to end."""

    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, allocation = _writer(repository, "w1")
        child = _live_child()
        try:
            acquire_writer_lease(
                repository.repo,
                target=target,
                owner="owner-b",
                client_id="executor-a",
                holder_pid=child.pid,
            )
            store = _store(repository)
            with store.lock(exclusive=True):
                snapshot = store._load_unlocked()
                store.replace_record(
                    _updated_record(
                        snapshot.records[allocation], "HANDOFF", lifecycle="HANDOFF_READY"
                    )
                )

            payload = reconcile_writers(
                repository.repo, actor="lifecycle-reconciler", dry_run=False, trigger="manual"
            )

            assert payload["decision"]["stranded_entitlement"] == 0
            assert allocation in _leases(repository)
        finally:
            child.kill()
            child.wait()


def test_reconciliation_sweeps_a_dead_holder_lease_on_a_parked_allocation() -> None:
    """A retained lane keeps its record; a lease whose holder is gone grants nothing."""

    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, allocation = _writer(repository, "w1")
        child = _live_child()
        acquire_writer_lease(
            repository.repo,
            target=target,
            owner="owner-b",
            client_id="executor-a",
            holder_pid=child.pid,
        )
        store = _store(repository)
        with store.lock(exclusive=True):
            snapshot = store._load_unlocked()
            store.replace_record(
                _updated_record(snapshot.records[allocation], "PARKED", lifecycle="RETAINED")
            )
        child.kill()
        child.wait()

        payload = reconcile_writers(
            repository.repo, actor="lifecycle-reconciler", dry_run=False, trigger="background"
        )

        assert payload["decision"]["stranded_entitlement_cleared"] == 1
        assert allocation not in _leases(repository)
        assert _record(repository, allocation).lifecycle == "RETAINED"


def test_reconciliation_keeps_entitlement_for_an_active_allocation() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        target, allocation = _writer(repository, "w1")
        child = _live_child()
        try:
            acquire_writer_lease(
                repository.repo,
                target=target,
                owner="owner-b",
                client_id="executor-a",
                holder_pid=child.pid,
            )

            payload = reconcile_writers(
                repository.repo, actor="lifecycle-reconciler", dry_run=False, trigger="background"
            )

            assert payload["decision"]["stranded_entitlement"] == 0
            assert allocation in _leases(repository)
            assert _record(repository, allocation).lifecycle == "ACTIVE"
        finally:
            child.kill()
            child.wait()


def test_one_process_reading_serves_every_checkout_in_a_pass(monkeypatch) -> None:
    """Each process is read once per pass, however many checkouts are checked."""
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        targets = [_writer(repository, f"w{index}")[0] for index in range(1, 4)]
        child = subprocess.Popen(["sleep", "60"], cwd=targets[1])
        reads: dict[int, int] = {}
        original = writer_lease._referenced_paths

        def counted(pid: int) -> list[str]:
            reads[pid] = reads.get(pid, 0) + 1
            return original(pid)

        monkeypatch.setattr(writer_lease, "_referenced_paths", counted)
        try:
            time.sleep(0.2)
            payload = reconcile_writers(repository.repo, actor="lifecycle-reconciler")
            assert reads and max(reads.values()) == 1
            assert _outcome(payload, "w2")["outcome"] == "LIVE_WRITER"
            assert all(_outcome(payload, task)["outcome"] != "LIVE_WRITER" for task in ("w1", "w3"))
            scan = writer_lease.ProcessPathScan()
            for target in targets:
                assert scan.in_use(target) == paths_in_use(target)
            assert any(pid == child.pid for pid, _cmd in scan.in_use(targets[1]))
        finally:
            child.kill()
            child.wait()


def test_path_evidence_reuses_the_scan_without_reading_command_arguments(tmp_path, monkeypatch) -> None:
    target = tmp_path / "checkout"
    sibling = tmp_path / "checkout-other"
    observations = {
        501: [str(target), str(target / "module.py"), str(sibling)],
        502: [str(sibling)],
    }
    reads = []
    monkeypatch.setattr(writer_lease.os, "listdir", lambda _path: ["501", "502"])

    def references(pid):
        reads.append(pid)
        return observations[pid]

    monkeypatch.setattr(writer_lease, "_referenced_paths", references)
    monkeypatch.setattr(writer_lease, "_command", lambda _pid: pytest.fail("command arguments read"))
    scan = writer_lease.ProcessPathScan()
    assert scan.referenced_paths_in_use(target) == [
        (501, [str(target), str(target / "module.py")])
    ]
    assert scan.referenced_paths_in_use(sibling) == [(501, [str(sibling)]), (502, [str(sibling)])]
    assert reads == [501, 502]
    monkeypatch.setattr(writer_lease, "_command", lambda pid: f"fixture-{pid}")
    assert scan.in_use(target) == [(501, "fixture-501")]
    assert reads == [501, 502]
