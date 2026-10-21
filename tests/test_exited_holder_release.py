"""A host can release its own lease after the runtime reaped the holder process."""

from __future__ import annotations

import os
import signal
import subprocess
import time

import pytest

from tests.test_write_admission import task as task
from worktree_controller import writer_lease
from worktree_controller.git_backend import inventory
from worktree_controller.models import ControllerError, WriterHolder
from worktree_controller.store import MetadataStore


def _leased(repository, path, command):
    child = subprocess.Popen(command, start_new_session=True)
    holder = writer_lease.resolve_holder(str(child.pid), client_id="runtime-host",
                                         session_id="reaped-session", run_id=None)
    lease = writer_lease.acquire_writer_lease(
        repository.repo, target=path, client_id=holder.client_id, holder_pid=holder.pid,
        session_id=holder.session_id, run_id=None, holder=holder,
    )["decision"]["lease_id"]
    return child, holder, lease


def _leases(repository):
    return MetadataStore(inventory(repository.repo).common_dir).load().writer_leases


def _stop(child):
    child.kill()
    child.wait(timeout=10)


def _release(repository, path, holder, lease_id):
    recorded = WriterHolder(pid=holder.pid, boot_id=holder.boot_id, start_ticks=holder.start_ticks,
                            client_id=holder.client_id, session_id=holder.session_id, run_id=None)
    return writer_lease.release_writer_lease(repository.repo, target=path, holder=recorded,
                                             lease_id=lease_id)["decision"]


def test_exited_holder_lease_is_released_with_its_exact_identity(task):
    repository, path, allocation = task
    child, holder, lease = _leased(repository, path, ["sleep", "60"])
    _stop(child)
    decision = _release(repository, path, holder, lease)
    assert decision["code"] == "WRITER_LEASE_RELEASED" and decision["holder_exited"] is True
    assert allocation not in _leases(repository)


def test_exited_holder_needs_the_exact_lease_id(task):
    repository, path, allocation = task
    child, holder, _lease = _leased(repository, path, ["sleep", "60"])
    _stop(child)
    with pytest.raises(ControllerError) as refused:
        _release(repository, path, holder, None)
    assert refused.value.code == "WRITER_LEASE_MISMATCH"
    with pytest.raises(ControllerError):
        _release(repository, path, holder, "00000000-0000-0000-0000-000000000000")
    assert allocation in _leases(repository)


def test_surviving_children_keep_an_exited_holders_lease(task):
    repository, path, allocation = task
    # The holder leads its process group; its background child outlives it.
    child, holder, lease = _leased(repository, path, ["sh", "-c", "sleep 60 & wait"])
    try:
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=10)
        time.sleep(0.1)
        with pytest.raises(ControllerError) as refused:
            _release(repository, path, holder, lease)
        assert "surviving children" in str(refused.value)
        assert allocation in _leases(repository)
    finally:
        os.killpg(holder.pid, signal.SIGKILL)


def test_another_identity_cannot_release_an_exited_holders_lease(task):
    repository, path, allocation = task
    child, holder, lease = _leased(repository, path, ["sleep", "60"])
    _stop(child)
    impostor = WriterHolder(pid=holder.pid, boot_id=holder.boot_id, start_ticks=holder.start_ticks + 1,
                            client_id=holder.client_id, session_id=holder.session_id, run_id=None)
    with pytest.raises(ControllerError):
        writer_lease.release_writer_lease(repository.repo, target=path, holder=impostor, lease_id=lease)
    assert allocation in _leases(repository)


def test_any_process_left_inside_the_checkout_keeps_an_exited_holders_lease(task):
    repository, path, allocation = task
    child, holder, lease = _leased(repository, path, ["sleep", "60"])
    _stop(child)
    # Another group (not the holder's) still works inside the checkout.
    occupant = subprocess.Popen(["sleep", "60"], cwd=path, start_new_session=True)
    try:
        with pytest.raises(ControllerError):
            _release(repository, path, holder, lease)
        assert allocation in _leases(repository)
    finally:
        _stop(occupant)
    assert _release(repository, path, holder, lease)["code"] == "WRITER_LEASE_RELEASED"
