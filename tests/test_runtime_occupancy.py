"""Runtime occupancy survives write entitlement and follows exact process identity."""

import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, replace

import pytest

from tests.test_retention_admission import _store
from tests.test_write_admission import guard
from tests.test_write_admission import task as task
from worktree_controller import lifecycle, resource_admission, writer_lease
from worktree_controller.controller import capacity_accounting
from worktree_controller.environment import runtime_authority_evidence, verify_runtime_transfer
from worktree_controller.git_backend import inventory
from worktree_controller.models import ControllerError
from worktree_controller.policy import load_policy


@pytest.mark.parametrize("stale_holder", [False, True])
def test_writer_ownership_does_not_report_runtime_occupancy(task, stale_holder):
    repository, path, allocation = task
    writer_lease.acquire_writer_lease(repository.repo, target=path,
        client_id="codex", holder_pid=os.getpid(), session_id="s", run_id="r")
    snapshot = _store(repository).load()
    if stale_holder:
        lease = snapshot.writer_leases[allocation]
        snapshot = replace(snapshot, writer_leases={allocation: replace(lease,
            holder=replace(lease.holder, boot_id="00000000-0000-0000-0000-000000000000"))})
    counts = capacity_accounting(inventory(repository.repo), load_policy(repository.repo), snapshot)
    assert counts["writer_leases_recorded"] == 1
    assert counts["runtime_occupied"] == counts["live_executions"] == 0


@pytest.mark.parametrize("bindings,role,lifecycle_state,canonical,occupied", [
    (("environment:shared-cache",), "feature", "ACTIVE", False, 0),
    (("runtime:systemd-user:service",), "feature", "RETAINED", False, 1),
    (("runtime:opaque", "runtime:another"), "integration", "ACTIVE", False, 1),
    (("runtime:opaque",), "control-plane", "ACTIVE", True, 1),
])
def test_runtime_occupancy_survives_without_writer_and_is_not_liveness(
    task, bindings, role, lifecycle_state, canonical, occupied,
):
    repository, _path, allocation = task
    git = inventory(repository.repo)
    snapshot = _store(repository).load()
    if canonical:
        allocation = next(record.allocation_id for record in snapshot.records.values()
            if record.role == "control-plane")
    record = snapshot.records[allocation]
    snapshot = replace(snapshot, records={**snapshot.records, allocation: replace(record,
        external_bindings=bindings, role=role, lifecycle=lifecycle_state)})
    counts = capacity_accounting(git, load_policy(repository.repo), snapshot)
    assert counts["writer_leases_recorded"] == 0
    assert counts["runtime_occupied"] == counts["live_executions"] == occupied
    assert "process liveness is not observed" in counts["live_executions_semantics"]


def test_own_runtime_does_not_fence_its_writer_but_cannot_clear_while_running(task, tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / ".local/state"))
    repository, path, allocation = task
    resource_admission.admit_execution(repository.repo, target=path, owner="review-owner",
        client_id="agent-workspace", session_id="session", run_id="run")
    acquired = writer_lease.acquire_writer_lease(repository.repo, target=path,
        client_id="agent-workspace", holder_pid=os.getpid(), session_id="session", run_id="run")
    lease = _store(repository).load().writer_leases[allocation]
    binding = writer_lease.RUNTIME_BINDING_PREFIX + json.dumps({
        "holder": asdict(lease.holder), "lease_id": acquired["decision"]["lease_id"], "kind": "test"})
    result = lifecycle.update_external_binding(repository.repo, target=path, owner="review-owner",
        expected_record_version=1, binding=binding, action="add")
    assert guard(repository, path, lease.holder) == "WRITE_ALLOWED"
    with pytest.raises(ControllerError) as error:
        lifecycle.update_external_binding(repository.repo, target=path, owner="review-owner",
            expected_record_version=result["decision"]["record_version"], binding=binding, action="remove")
    assert error.value.code == "RUNTIME_OCCUPIED"
    writer_lease.release_writer_lease(repository.repo, target=path, holder=lease.holder,
                                     holder_pid=lease.holder.pid, lease_id=lease.lease_id)
    assert binding in _store(repository).load().records[allocation].external_bindings


def test_runtime_binding_rejects_duplicate_identity_keys():
    with pytest.raises(ControllerError, match="malformed"):
        writer_lease.runtime_binding(writer_lease.RUNTIME_BINDING_PREFIX + '{"holder":{},"holder":{}}')


@pytest.mark.parametrize("preexisting", [True, False])
def test_execution_exit_distinguishes_preexisting_context_from_unknown_later_process(tmp_path, preexisting):
    processes = []

    def launch():
        process = subprocess.Popen([sys.executable, "-c", "import time; print('ready',flush=True); time.sleep(30)"],
            cwd=tmp_path, start_new_session=True, stdout=subprocess.PIPE, text=True)
        processes.append(process)
        assert process.stdout.readline() == "ready\n"
        return process

    try:
        first = launch()
        time.sleep(2 / os.sysconf("SC_CLK_TCK"))
        second = launch()
        observer, execution = (first, second) if preexisting else (second, first)
        holder = writer_lease.describe_holder(execution.pid, client_id="agent-workspace", session_id="s", run_id="r")
        execution.terminate()
        execution.wait(timeout=5)
        if preexisting:
            writer_lease.assert_runtime_clear(tmp_path, (holder,))
        else:
            with pytest.raises(ControllerError) as error:
                writer_lease.assert_runtime_clear(tmp_path, (holder,))
            assert error.value.code == "RUNTIME_OCCUPIED"
        evidence = runtime_authority_evidence(tmp_path)
        assert any(int(row["pid"]) == observer.pid for row in evidence["observed_references"])
        assert observer.poll() is None  # The context still blocks allocation cleanup.
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)


def test_runtime_transfer_accepts_exact_session_child_and_rejects_sibling():
    script = "import subprocess,sys;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(10)']);print(p.pid,flush=True);sys.stdin.readline();p.terminate();p.wait()"
    parent = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        child_pid = int(parent.stdout.readline())
        holder = writer_lease.describe_holder(parent.pid, client_id="agent-workspace", session_id="s", run_id="r")
        child = writer_lease.describe_holder(child_pid, client_id="agent-workspace", session_id="s", run_id="r")
        verify_runtime_transfer(holder, child)
        sibling = writer_lease.describe_holder(os.getpid(), client_id="agent-workspace", session_id="s", run_id="r")
        with pytest.raises(ControllerError) as error:
            verify_runtime_transfer(holder, sibling)
        assert error.value.code == "RUNTIME_TRANSFER_UNPROVEN"
    finally:
        parent.communicate("done\n", timeout=5)


def test_formal_background_transfer_does_not_grant_or_fence_writer_ownership(task):
    repository, path, allocation = task
    script = "import subprocess,sys;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);print(p.pid,flush=True);sys.stdin.readline();p.terminate();p.wait()"
    parent = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True, start_new_session=True, cwd=path)
    try:
        child_pid = int(parent.stdout.readline())
        resource_admission.admit_execution(repository.repo, target=path, owner="review-owner",
            client_id="agent-workspace", session_id="s", run_id="r")
        writer_lease.acquire_writer_lease(repository.repo, target=path,
            client_id="agent-workspace", holder_pid=parent.pid, session_id="s", run_id="r")
        lease = _store(repository).load().writer_leases[allocation]
        binding = writer_lease.RUNTIME_BINDING_PREFIX + json.dumps({
            "holder": asdict(lease.holder), "lease_id": lease.lease_id, "kind": "preview"})
        lifecycle.update_external_binding(repository.repo, target=path, owner="review-owner",
            expected_record_version=1, binding=binding, action="add")
        transferred = lifecycle.update_external_binding(repository.repo, target=path, owner="review-owner",
            expected_record_version=2, binding=binding, action="transfer", holder_pid=child_pid)
        assert transferred["decision"]["code"] == "BINDING_TRANSFERRED"
        assert guard(repository, path, lease.holder) == "WRITE_ALLOWED"
        assert _store(repository).load().writer_leases[allocation].holder == lease.holder
    finally:
        parent.communicate("done\n", timeout=5)
