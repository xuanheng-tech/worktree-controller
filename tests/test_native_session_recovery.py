"""Native terminal evidence must not release an active thread or shared process."""

import json
import os
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path

import pytest

from tests.test_retention_admission import _store
from tests.test_write_admission import task as task
from worktree_controller import lifecycle, resource_admission, writer_lease
from worktree_controller import native_session_recovery as native
from worktree_controller.models import ControllerError


@pytest.fixture
def archived(tmp_path, monkeypatch):
    home = tmp_path / "home"
    archive = home / ".codex/archived_sessions"
    archive.mkdir(parents=True)
    path = archive / "synthetic-session.jsonl"
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "synthetic-session"}}) + "\n"
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    holder = writer_lease.describe_holder(
        os.getpid(), client_id="codex-cli", session_id="synthetic-session", run_id="synthetic-run"
    )
    observation = {
        "thread": {"id": holder.session_id, "status": {"type": "notLoaded"}, "path": str(path)},
        "loaded": ["another-synthetic-thread"],
        "peer_pid": holder.pid,
    }
    monkeypatch.setattr(native, "_shared_daemon", lambda holder: None)
    monkeypatch.setattr(native, "_native_observation", lambda holder: observation)
    empty = {
        "managed_references": [],
        "observed_references": [],
        "managed_visibility_uncertainties": [],
        "scan_uncertainties": [],
        "host_visibility_gaps": [],
    }
    monkeypatch.setattr(native, "runtime_authority_evidence", lambda target: empty)
    return holder, observation, empty


def test_native_terminal_proof_preserves_other_threads(archived, tmp_path):
    holder, _observation, _refs = archived
    result = native.observe_archived_session(tmp_path, holder)
    assert result["contract"] == native.CONTRACT
    assert result["native"]["other_loaded_thread_count"] == 1
    assert result["processes_stopped"] == result["threads_archived"] == 0


@pytest.mark.parametrize(
    "drift",
    [
        "active",
        "loaded",
        "wrong_id",
        "not_archived",
        "missing",
        "symlink",
        "metadata_id",
        "oversize",
    ],
)
def test_native_identity_or_lifecycle_drift_refuses(archived, tmp_path, drift):
    holder, observation, _refs = archived
    thread = observation["thread"]
    path = Path(thread["path"])
    if drift == "active":
        thread["status"] = {"type": "active", "activeFlags": []}
    elif drift == "loaded":
        observation["loaded"].append(holder.session_id)
    elif drift == "wrong_id":
        thread["id"] = "other-session"
    elif drift == "not_archived":
        thread["path"] = str(tmp_path / "sessions/active.jsonl")
    elif drift == "missing":
        path.unlink()
    elif drift == "symlink":
        original = path.with_suffix(".original")
        path.rename(original)
        path.symlink_to(original)
    elif drift == "metadata_id":
        path.write_text('{"type":"session_meta","payload":{"id":"another"}}\n')
    else:
        path.write_bytes(b"x" * 262_145)
    with pytest.raises(ControllerError) as exc:
        native.observe_archived_session(tmp_path, holder)
    assert exc.value.code == "NATIVE_SESSION_RECOVERY_REFUSED"


@pytest.mark.parametrize(
    "field",
    [
        "managed_references",
        "observed_references",
        "managed_visibility_uncertainties",
        "scan_uncertainties",
    ],
)
def test_checkout_reference_or_uncertainty_refuses(archived, tmp_path, field):
    holder, _observation, refs = archived
    refs[field].append({"synthetic": True})
    with pytest.raises(ControllerError, match="checkout use"):
        native.observe_archived_session(tmp_path, holder)


def _binding(task, monkeypatch, tmp_path):
    repository, path, allocation = task
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    resource_admission.admit_execution(
        repository.repo,
        target=path,
        owner="review-owner",
        client_id="codex",
        session_id="synthetic-session",
        run_id="run",
    )
    acquired = writer_lease.acquire_writer_lease(
        repository.repo,
        target=path,
        client_id="codex",
        holder_pid=os.getpid(),
        session_id="synthetic-session",
        run_id="run",
    )
    lease = _store(repository).load().writer_leases[allocation]
    value = writer_lease.RUNTIME_BINDING_PREFIX + json.dumps(
        {
            "holder": asdict(lease.holder),
            "lease_id": acquired["decision"]["lease_id"],
            "kind": "agent",
        }
    )
    added = lifecycle.update_external_binding(
        repository.repo,
        target=path,
        expected_record_version=1,
        owner="review-owner",
        binding=value,
        action="add",
    )
    return repository, path, allocation, lease, value, added["decision"]["record_version"]


def test_live_writer_still_refuses_native_recovery(task, monkeypatch, tmp_path):
    repository, path, _allocation, _lease, value, version = _binding(task, monkeypatch, tmp_path)
    with pytest.raises(ControllerError, match="exact writer lease"):
        lifecycle.update_external_binding(
            repository.repo,
            target=path,
            expected_record_version=version,
            owner="review-owner",
            binding=value,
            action="remove",
            recover_archived_codex_session=True,
        )


def test_dry_run_removal_and_repeat_are_evidenced(task, monkeypatch, tmp_path):
    repository, path, allocation, lease, value, version = _binding(task, monkeypatch, tmp_path)
    writer_lease.release_writer_lease(
        repository.repo,
        target=path,
        holder=lease.holder,
        holder_pid=lease.holder.pid,
        lease_id=lease.lease_id,
    )
    proof = {
        "contract": native.CONTRACT,
        "holder": asdict(lease.holder),
        "native": {
            "thread_id": lease.holder.session_id,
            "archived_rollout": "synthetic-archive",
            "session_metadata_sha256": "a" * 64,
        },
    }
    monkeypatch.setattr(native, "observe_archived_session", lambda *args: proof)
    kwargs = dict(
        target=path,
        expected_record_version=version,
        owner="review-owner",
        binding=value,
        action="remove",
        recover_archived_codex_session=True,
    )
    dry = lifecycle.update_external_binding(repository.repo, **kwargs, dry_run=True)
    assert dry["decision"]["code"] == "NATIVE_SESSION_RECOVERY_READY"
    assert value in _store(repository).load().records[allocation].external_bindings
    assert not (_store(repository).root / "native-session-recoveries").exists()
    removed = lifecycle.update_external_binding(repository.repo, **kwargs)
    ref = removed["decision"]["native_recovery"]
    assert Path(ref["path"]).is_file()
    current = _store(repository).load().records[allocation]
    assert current.external_bindings == () and current.lifecycle == "ACTIVE"
    assert current.owner == "review-owner" and path.is_dir()
    kwargs["expected_record_version"] = current.record_version
    repeated = lifecycle.update_external_binding(repository.repo, **kwargs)
    assert repeated["decision"]["changed"] is False
    assert repeated["decision"]["native_recovery"] == ref


def test_ordinary_removal_keeps_live_process_gate(task, monkeypatch, tmp_path):
    repository, path, _allocation, lease, value, version = _binding(task, monkeypatch, tmp_path)
    writer_lease.release_writer_lease(
        repository.repo,
        target=path,
        holder=lease.holder,
        holder_pid=lease.holder.pid,
        lease_id=lease.lease_id,
    )
    with pytest.raises(ControllerError) as exc:
        lifecycle.update_external_binding(
            repository.repo,
            target=path,
            expected_record_version=version,
            owner="review-owner",
            binding=value,
            action="remove",
        )
    assert exc.value.code == "RUNTIME_OCCUPIED"


@pytest.mark.parametrize("wrong_peer", [False, True])
def test_native_transport_uses_only_read_methods(monkeypatch, tmp_path, request, wrong_peer):
    import socket
    import struct

    from websockets.sync import client

    path = tmp_path / ".codex/app-server-control/app-server-control.sock"
    path.parent.mkdir(parents=True)
    # A deep pytest basetemp can exceed the AF_UNIX path limit, so the listener
    # lives in a short private directory that the test removes again.
    short = Path(tempfile.mkdtemp(prefix="wc-", dir="/tmp" if os.path.isdir("/tmp") else None))
    request.addfinalizer(lambda: shutil.rmtree(short, ignore_errors=True))
    endpoint = short / "s"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(endpoint))
    path.symlink_to(endpoint)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    holder = writer_lease.describe_holder(
        os.getpid(), client_id="codex", session_id="synthetic", run_id="run"
    )
    calls = []

    class Peer:
        def getsockopt(self, *args):
            return struct.pack("3i", holder.pid + int(wrong_peer), os.getuid(), os.getgid())

    class Connection:
        socket = Peer()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def send(self, payload):
            calls.append(json.loads(payload))

        def recv(self, **kwargs):
            request = calls[-1]
            result = {
                "initialize": {},
                "thread/read": {"thread": {}},
                "thread/loaded/list": {"data": []},
            }[request["method"]]
            return json.dumps({"id": request["id"], "result": result})

    monkeypatch.setattr(client, "unix_connect", lambda *args, **kwargs: Connection())
    if wrong_peer:
        try:
            with pytest.raises(ControllerError, match="socket peer"):
                native._native_observation(holder)
            assert calls == []
        finally:
            listener.close()
        return
    try:
        result = native._native_observation(holder)
    finally:
        listener.close()
    assert result["loaded"] == []
    assert [x["method"] for x in calls] == [
        "initialize",
        "initialized",
        "thread/read",
        "thread/loaded/list",
    ]
    assert calls[2]["params"]["includeTurns"] is False


def test_errors_do_not_expose_native_details(archived, monkeypatch, tmp_path):
    holder, _observation, _refs = archived

    def failed(_holder):
        raise RuntimeError("synthetic-sensitive-response")

    monkeypatch.setattr(native, "_native_observation", failed)
    with pytest.raises(ControllerError) as exc:
        native.observe_archived_session(tmp_path, holder)
    assert "synthetic-sensitive-response" not in str(exc.value)
    assert "RuntimeError" in str(exc.value)


def test_process_drift_after_native_observation_refuses(archived, monkeypatch, tmp_path):
    holder, _observation, _refs = archived
    calls = []

    def changed(_holder):
        calls.append(True)
        if len(calls) == 2:
            raise native._blocked("holder identity changed")

    monkeypatch.setattr(native, "_shared_daemon", changed)
    with pytest.raises(ControllerError, match="holder identity changed"):
        native.observe_archived_session(tmp_path, holder)


def test_interrupted_receipt_reuses_exact_identity(task, monkeypatch, tmp_path):
    repository, path, allocation, lease, value, version = _binding(task, monkeypatch, tmp_path)
    writer_lease.release_writer_lease(
        repository.repo,
        target=path,
        holder=lease.holder,
        holder_pid=lease.holder.pid,
        lease_id=lease.lease_id,
    )
    proof = {
        "contract": native.CONTRACT,
        "holder": asdict(lease.holder),
        "native": {
            "thread_id": lease.holder.session_id,
            "archived_rollout": "synthetic",
            "session_metadata_sha256": "b" * 64,
            "other_loaded_thread_count": 1,
        },
    }
    monkeypatch.setattr(native, "observe_archived_session", lambda *args: proof)
    store_type = type(_store(repository))
    original = store_type.replace_record

    def interrupted(*args, **kwargs):
        raise RuntimeError("synthetic interrupted metadata commit")

    monkeypatch.setattr(store_type, "replace_record", interrupted)
    kwargs = dict(
        target=path,
        owner="review-owner",
        expected_record_version=version,
        binding=value,
        action="remove",
        recover_archived_codex_session=True,
    )
    with pytest.raises(RuntimeError, match="interrupted metadata"):
        lifecycle.update_external_binding(repository.repo, **kwargs)
    assert value in _store(repository).load().records[allocation].external_bindings
    monkeypatch.setattr(store_type, "replace_record", original)
    proof["native"]["other_loaded_thread_count"] = 2
    proof["native"]["session_metadata_sha256"] = "c" * 64
    with pytest.raises(ControllerError, match="evidence differs"):
        lifecycle.update_external_binding(repository.repo, **kwargs)
    proof["native"]["session_metadata_sha256"] = "b" * 64
    completed = lifecycle.update_external_binding(repository.repo, **kwargs)
    assert completed["decision"]["changed"] is True
    receipt = json.loads(Path(completed["decision"]["native_recovery"]["path"]).read_text())
    assert receipt["evidence"]["native"]["other_loaded_thread_count"] == 1
