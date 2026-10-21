"""Batching host exits preserves per-reservation proof and atomic durability."""

import errno
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from dataclasses import asdict
from pathlib import Path

import pytest

from worktree_controller import resource_admission as resources
from worktree_controller import writer_lease
from worktree_controller.models import ControllerError


@pytest.fixture(params=["automatic", "explicit"])
def reconcile_exits(request):
    if request.param == "automatic":
        return resources.reconcile_finished_executions
    return lambda: resources.reconcile(apply=True)


@pytest.fixture
def executions(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    children = []

    def create(session, *, code="import time; time.sleep(300)"):
        target = home / session
        target.mkdir()
        admitted = resources.host_execution(
            "admit", target=target, client_id="agent-workspace",
            session_id=session, run_id="run-" + session,
        )["decision"]
        assert admitted["code"] == "RESOURCE_AVAILABLE"
        key = admitted["request_ids"][0]
        child = subprocess.Popen(
            [sys.executable, "-c", code], cwd=target, start_new_session=True,
        )
        children.append(child)
        resources.host_execution("bind", request_id=key, holder_pid=child.pid)
        return key, child, target

    yield create
    for child in children:
        # Includes task-created descendants left after their holder exits.
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=5)


def _exit(child):
    child.terminate()
    child.wait(timeout=5)


def _binding(pid, request):
    holder = writer_lease.describe_holder(
        pid, client_id=request["client_id"],
        session_id=request["session_id"], run_id=request["run_id"],
    )
    return writer_lease.RUNTIME_BINDING_PREFIX + json.dumps(
        {"holder": asdict(holder), "lease_id": None, "kind": "background"},
        sort_keys=True, separators=(",", ":"),
    )


def _edit_row(ledger, key, edit):
    """Seed historical uncertainty without weakening the production producer."""
    with ledger.locked():
        state = ledger.read()
        row = state["requests"].pop(key)
        edit(row)
        key = resources.fingerprint(row["request"])
        state["requests"][key] = row
        ledger._save(state)
    return key


def test_exited_host_rows_settle_in_one_write_and_replay_without_writes(executions, monkeypatch, reconcile_exits):
    created = [executions(f"dead-{index}") for index in range(4)]
    for _, child, _ in created:
        _exit(child)
    ledger = resources.AdmissionLedger()
    before = ledger.read()
    saves = []
    original = resources.AdmissionLedger._save

    def save(self, state):
        saves.append(self.path)
        original(self, state)

    monkeypatch.setattr(resources.AdmissionLedger, "_save", save)
    reconcile_exits()
    settled = ledger.read()
    assert saves == [ledger.path]
    for key, _, _ in created:
        row = settled["requests"][key]
        assert row["state"] == "RELEASED"
        assert row["history"][:-1] == before["requests"][key]["history"]
        assert row["history"][-1]["state"] == "RELEASED"
        for field in ("request", "holder", "runtime_bindings", "lease_id", "decision"):
            assert row[field] == before["requests"][key][field]
    durable = ledger.path.read_bytes()
    reconcile_exits()
    assert saves == [ledger.path]
    assert ledger.path.read_bytes() == durable


@pytest.mark.parametrize("fault", [
    "missing", "replaced", "symlink", "EACCES", "EIO", "uncertain-binding",
    "lease", "binding-lease", "foreign-owner", "home-scope", "live", "live-binding", "unknown-proc",
])
def test_only_proven_exits_settle_in_a_mixed_batch(executions, monkeypatch, fault, reconcile_exits):
    safe, safe_child, _ = executions("safe")
    protected, child, target = executions("protected")
    extra, extra_child, _ = executions("extra")
    _exit(safe_child)
    if fault != "live":
        _exit(child)
    ledger = resources.AdmissionLedger()
    if fault == "missing":
        target.rmdir()
    elif fault in {"replaced", "symlink"}:
        target.rename(target.with_name("old-protected"))
        if fault == "replaced":
            target.mkdir()
        else:
            target.symlink_to(target.with_name("old-protected"), target_is_directory=True)
    elif fault in {"EACCES", "EIO"}:
        original = os.lstat

        def lstat(path, *args, **kwargs):
            if Path(path) == target:
                raise OSError(getattr(errno, fault), "fixture observation unavailable")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(os, "lstat", lstat)
    elif fault == "uncertain-binding":
        _edit_row(ledger, protected, lambda row: row.update(runtime_bindings=[]))
    elif fault == "lease":
        _edit_row(ledger, protected, lambda row: row.update(lease_id="held"))
    elif fault == "binding-lease":
        def add_lease(row):
            value = json.loads(row["runtime_bindings"][0].removeprefix(
                writer_lease.RUNTIME_BINDING_PREFIX))
            value["lease_id"] = "held"
            row["runtime_bindings"][0] = writer_lease.RUNTIME_BINDING_PREFIX + json.dumps(value)

        _edit_row(ledger, protected, add_lease)
    elif fault == "foreign-owner":
        protected = _edit_row(ledger, protected, lambda row: row["request"].update(owner=os.getuid() + 1))
    elif fault == "home-scope":
        def broaden(row):
            info = Path.home().stat()
            row["request"].update(target_path=str(Path.home()), path_identity=[info.st_dev, info.st_ino])

        protected = _edit_row(ledger, protected, broaden)
    elif fault == "live-binding":
        _edit_row(ledger, protected, lambda row: row["runtime_bindings"].append(
            _binding(extra_child.pid, row["request"])))
    elif fault == "unknown-proc":
        original_group = writer_lease.process_group_is_live

        def process_group(holder):
            if holder.pid == child.pid:
                raise ControllerError("PROCESS_OBSERVATION_UNAVAILABLE", "fixture procfs unavailable")
            return original_group(holder)

        monkeypatch.setattr(writer_lease, "process_group_is_live", process_group)
    before = ledger.read()
    result = reconcile_exits()
    after = ledger.read()
    assert after["requests"][safe]["state"] == "RELEASED"
    assert after["requests"][protected] == before["requests"][protected]
    assert after["requests"][extra] == before["requests"][extra]
    if result is not None:
        outcomes = {item["request_id"]: item for item in result["decision"]["outcomes"]}
        assert set(outcomes) == {safe, protected, extra}
        assert outcomes[safe]["result"] == {
            "code": "HOST_EXECUTION_CLEARED", "task_completed": False, "request_ids": [safe],
        }
        assert outcomes[extra]["result"]["code"] == "EXECUTION_OCCUPIED"
        assert "blocker" in outcomes[protected] or outcomes[protected]["result"]["code"] == "EXECUTION_OCCUPIED"


def test_exited_holder_with_a_live_process_group_keeps_its_budget(executions, reconcile_exits):
    code = (
        "import subprocess,sys,time; from pathlib import Path; "
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(300)']); "
        "Path('ready').write_text(str(p.pid)); "
        "\nwhile not Path('exit').exists(): time.sleep(0.01)\n"
    )
    key, holder, target = executions("group", code=code)
    deadline = time.monotonic() + 5
    while not (target / "ready").exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    (target / "exit").touch()
    holder.wait(timeout=5)
    ledger = resources.AdmissionLedger()
    before = ledger.path.read_bytes()
    reconcile_exits()
    assert ledger.path.read_bytes() == before
    assert ledger.read()["requests"][key]["state"] == "ALLOCATED"


def test_failed_atomic_save_preserves_all_budgets_and_retry_is_idempotent(executions, monkeypatch, reconcile_exits):
    created = [executions(f"dead-{index}") for index in range(3)]
    for _, child, _ in created:
        _exit(child)
    ledger = resources.AdmissionLedger()
    before = ledger.path.read_bytes()
    original = resources.AdmissionLedger._save

    def fail(self, state):
        raise OSError(errno.EIO, "fixture failed atomic write")

    monkeypatch.setattr(resources.AdmissionLedger, "_save", fail)
    result = reconcile_exits()
    assert ledger.path.read_bytes() == before
    if result is not None:
        assert all(item["blocker"] == "RESOURCE_OBSERVATION_UNAVAILABLE"
                   for item in result["decision"]["outcomes"])
    monkeypatch.setattr(resources.AdmissionLedger, "_save", original)
    reconcile_exits()
    settled = ledger.path.read_bytes()
    assert all(row["state"] == "RELEASED" for row in ledger.read()["requests"].values())
    reconcile_exits()
    assert ledger.path.read_bytes() == settled


def test_batch_rechecks_runtime_chain_from_latest_locked_state(executions, monkeypatch, reconcile_exits):
    key, exited, _ = executions("old")
    other, live, _ = executions("live")
    _exit(exited)
    ledger = resources.AdmissionLedger()
    original = resources.AdmissionLedger.locked
    published = False
    expected = None

    @contextmanager
    def locked(self):
        nonlocal published, expected
        with original(self):
            if not published:
                # A concurrent binding was saved after the initial recovery
                # snapshot, before this reader acquired its exclusive lock.
                state = self.read()
                row = state["requests"][key]
                row["runtime_bindings"].append(_binding(live.pid, row["request"]))
                self._save(state)
                expected = self.path.read_bytes()
                published = True
            yield

    monkeypatch.setattr(resources.AdmissionLedger, "locked", locked)
    reconcile_exits()
    assert published
    assert ledger.path.read_bytes() == expected
    assert {key, other} == set(ledger.read()["requests"])


def test_malformed_unrelated_request_blocks_recovery_without_mutation(executions, reconcile_exits):
    key, child, _ = executions("dead")
    unrelated, other, _ = executions("unrelated")
    _exit(child)
    _exit(other)
    ledger = resources.AdmissionLedger()
    with ledger.locked():
        state = ledger.read()
        assert state["requests"][key]["state"] == "ALLOCATED"
        state["requests"][unrelated]["request"]["run_id"] = "identity-drift"
        ledger._save(state)
    before = ledger.path.read_bytes()
    with pytest.raises(ControllerError) as error:
        reconcile_exits()
    assert error.value.code == "INVALID_RESOURCE_LEDGER"
    assert ledger.path.read_bytes() == before


def test_batch_failure_does_not_hold_host_lock_during_repository_recovery(executions, monkeypatch, reconcile_exits):
    key, child, _ = executions("dead")
    _exit(child)
    ledger = resources.AdmissionLedger()
    _edit_row(ledger, key, lambda row: row["request"].update(allocation_id="repository-allocation"))
    host_key, host_child, _ = executions("host")
    _exit(host_child)
    recovered = []
    original_recovery = resources._reconcile_execution

    def fail(self, state):
        raise OSError(errno.EIO, "fixture failed batch write")

    def recover(row, *, apply):
        if row["request"]["allocation_id"] is None:
            return original_recovery(row, apply=apply)
        # Nonblocking acquisition detects a leaked batch lock without hanging.
        import fcntl

        with (ledger.root / "ledger.lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            recovered.append(row["request"]["allocation_id"])
        assert apply

    monkeypatch.setattr(resources.AdmissionLedger, "_save", fail)
    monkeypatch.setattr(resources, "_reconcile_execution", recover)
    reconcile_exits()
    assert recovered == ["repository-allocation"]
    assert ledger.read()["requests"][host_key]["state"] == "ALLOCATED"


def test_explicit_apply_preserves_dry_run_outcomes_and_terminal_replay(executions):
    dead, child, _ = executions("dead")
    live, _, _ = executions("live")
    _exit(child)
    ledger = resources.AdmissionLedger()
    before = ledger.path.read_bytes()
    planned = resources.reconcile()["decision"]
    assert planned["dry_run"]
    assert ledger.path.read_bytes() == before
    assert [item["request_id"] for item in planned["outcomes"]] == [dead, live]
    assert [item["result"]["code"] for item in planned["outcomes"]] == [
        "EXECUTION_RECONCILIATION_PENDING", "EXECUTION_OCCUPIED",
    ]
    applied = resources.reconcile(apply=True)["decision"]
    assert not applied["dry_run"]
    assert applied["outcomes"][0] == {
        **planned["outcomes"][0],
        "result": {"code": "HOST_EXECUTION_CLEARED", "task_completed": False, "request_ids": [dead]},
    }
    assert applied["outcomes"][1] == planned["outcomes"][1]
    durable = ledger.path.read_bytes()
    assert resources.reconcile(apply=True)["decision"]["outcomes"] == [planned["outcomes"][1]]
    assert ledger.path.read_bytes() == durable


def test_repository_selected_apply_leaves_host_requests_untouched(executions):
    from tests.test_shadow import GitRepository

    key, child, _ = executions("host")
    _exit(child)
    ledger = resources.AdmissionLedger()
    before = ledger.path.read_bytes()
    with GitRepository(linked=0) as repository:
        result = resources.reconcile(repository_path=repository.repo, apply=True)
    assert result["decision"]["outcomes"] == []
    assert ledger.path.read_bytes() == before
    assert ledger.read()["requests"][key]["state"] == "ALLOCATED"
