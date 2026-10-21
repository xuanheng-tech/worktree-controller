"""Host ledger encoding preserves identity, compatibility and durability boundaries."""

import errno
import json
import os
import stat

import pytest

from worktree_controller import resource_admission as resources


@pytest.fixture
def ledger_state(tmp_path):
    ledger = resources.AdmissionLedger(tmp_path / "ledger")
    request = {"kind": "execution", "owner": "测试 owner", "memory_bytes": 2048}
    holder = {
        "client_id": "fixture", "pid": 7, "boot_id": "fixture-boot", "start_ticks": 10,
        "session_id": "fixture-session", "run_id": "fixture-run",
    }
    state = {
        "contract": resources.CONTRACT, "pressure_high": False,
        "requests": {resources.fingerprint(request): {
            "request": request, "state": "ALLOCATED", "holder": holder,
            "history": [{"state": "RESERVED", "at_ns": 1}, {"state": "ALLOCATED", "at_ns": 2}],
            "decision": {"detail": 'spaces, quotes " and newline\n are data'},
        }},
    }
    return ledger, state


def test_pretty_ledger_reads_without_rewriting(ledger_state):
    ledger, state = ledger_state
    ledger.backend._atomic_json(ledger.path, state)
    before = ledger.path.read_bytes()
    assert b'\n  "contract"' in before
    assert ledger.read() == state
    assert ledger.path.read_bytes() == before


def test_compact_ledger_preserves_complete_state_and_identity(ledger_state):
    ledger, state = ledger_state
    row = next(iter(state["requests"].values()))
    request_digest = resources.fingerprint(row["request"])
    holder_digest = resources.canonical_holder_digest(row["holder"])
    ledger._save(state)
    raw = ledger.path.read_bytes()
    assert raw.count(b"\n") == 1 and raw.endswith(b"\n")
    assert "测试 owner".encode() in raw
    assert json.loads(raw) == state == ledger.read()
    restored = ledger.read()["requests"][request_digest]
    assert resources.fingerprint(restored["request"]) == request_digest
    assert resources.canonical_holder_digest(restored["holder"]) == holder_digest


def test_compact_empty_ledger_has_stable_sorted_encoding(tmp_path):
    ledger = resources.AdmissionLedger(tmp_path / "ledger")
    ledger._save(ledger.read())
    assert ledger.path.read_bytes() == (
        b'{"contract":"host-resource-admission/v1","pressure_high":false,"requests":{}}\n'
    )


def test_repository_metadata_default_encoding_is_unchanged(tmp_path):
    ledger = resources.AdmissionLedger(tmp_path / "ledger")
    path = ledger.root / "receipt.json"
    ledger.backend._atomic_json(path, {"value": 1, "message": "中文"})
    assert path.read_bytes() == '{\n  "message": "中文",\n  "value": 1\n}\n'.encode()


def test_ledger_save_syncs_file_before_replace_and_directory_after(ledger_state, monkeypatch):
    ledger, state = ledger_state
    events = []
    original_sync, original_replace = os.fsync, os.replace

    def sync(descriptor):
        events.append("directory-sync" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file-sync")
        original_sync(descriptor)

    def replace(source, destination):
        original_replace(source, destination)
        events.append("replace")

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "replace", replace)
    ledger._save(state)
    assert events == ["file-sync", "replace", "directory-sync"]
    assert stat.S_IMODE(ledger.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(ledger.root.stat().st_mode) == 0o700
    assert ledger.read() == state
    assert not list(ledger.root.glob(".state.json.*.tmp"))


@pytest.mark.parametrize("phase", ["encode", "file-sync", "replace", "directory-sync"])
def test_failed_save_preserves_atomic_visibility_and_cleans_temporary(
    ledger_state, monkeypatch, phase
):
    ledger, previous = ledger_state
    ledger.backend._atomic_json(ledger.path, previous)
    before = ledger.path.read_bytes()
    updated = json.loads(before)
    updated["pressure_high"] = True
    original_sync = os.fsync
    failure = OSError(errno.EIO, "fixture persistence failure")

    def sync(descriptor):
        directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
        if (phase == "directory-sync" and directory) or (phase == "file-sync" and not directory):
            raise failure
        original_sync(descriptor)

    def replace(source, destination):
        raise failure

    if phase == "encode":
        updated["unsupported"] = object()
        expected_error = TypeError
    else:
        monkeypatch.setattr(os, "fsync", sync)
        if phase == "replace":
            monkeypatch.setattr(os, "replace", replace)
        expected_error = OSError
    with pytest.raises(expected_error) as error:
        ledger._save(updated)
    if phase != "encode":
        assert error.value is failure
    if phase == "directory-sync":
        assert ledger.read() == updated
    else:
        assert ledger.path.read_bytes() == before
        assert ledger.read() == previous
    assert not list(ledger.root.glob(".state.json.*.tmp"))
