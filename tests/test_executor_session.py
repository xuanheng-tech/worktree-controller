"""One explicit execution contract without caller-private files or adapters."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_retention_admission import _record, _store
from tests.test_write_admission import ROOT
from tests.test_write_admission import task as task
from worktree_controller import resource_admission, writer_lease, writer_session

SESSION_ENTRYPOINT = """
import signal
# Like an interactive terminal: Ctrl-C reaches the session. A suite started as a
# background job inherits SIGINT ignored, and so would every child it launches.
signal.signal(signal.SIGINT, signal.default_int_handler)
from worktree_controller import resource_admission as resources
from worktree_controller.writer_session import run_main
real_filesystem = resources.filesystem
resources.filesystem = lambda path: dict(
    real_filesystem(path), available_bytes=10**15, available_inodes=10**12
)
resources.pressure = lambda: {
    'available_memory_bytes': 10**15, 'observed_at_ns': 1,
    'psi_avg10': {'cpu': 0.0, 'io': 0.0, 'memory': 0.0},
}
raise SystemExit(run_main())
"""


def invoke(
    path: Path,
    home: Path,
    client: str,
    command: list[str],
    *,
    wait: bool = True,
    run_id: str = "explicit-run",
    entrypoint: str = SESSION_ENTRYPOINT,
):
    launch = subprocess.run if wait else subprocess.Popen
    return launch(
        [
            sys.executable,
            "-B",
            "-c",
            entrypoint,
            "--path",
            str(path),
            "--client-id",
            client,
            "--session-id",
            "explicit-session",
            "--run-id",
            run_id,
            "--",
            *command,
        ],
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"],
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **({"timeout": 45, "check": False} if wait else {}),
    )


@pytest.mark.parametrize("client", ["terminal", "executor-a", "any.executor:17"])
def test_same_contract_checks_host_identity_and_preserves_exit_status(task, tmp_path, client):
    repo, path, allocation = task
    home = tmp_path / "empty-home"
    home.mkdir()
    child = (
        "import json,os; from pathlib import Path; "
        "from worktree_controller.writer_session import host_check; "
        "d=host_check(Path.cwd()); assert d['code']=='WRITE_ALLOWED'; "
        "c=json.loads(os.environ['CONTROLLER_WRITER_CONTEXT']); "
        "assert c['session_id']=='explicit-session' and c['run_id']=='explicit-run'; "
        "Path('mine.txt').write_text('owned change'); print(json.dumps(c)); raise SystemExit(37)"
    )
    result = invoke(path, home, client, [sys.executable, "-B", "-c", child])
    assert result.returncode == 37, result.stderr
    assert json.loads(result.stdout)["client_id"] == client
    assert (path / "mine.txt").read_text() == "owned change"
    assert allocation not in _store(repo).load().writer_leases
    assert _record(repo, allocation).external_bindings
    request_id = next(
        key for key, row in resource_admission.AdmissionLedger().read()["requests"].items()
        if row["request"].get("kind") == "execution"
    )
    # Concurrent procfs changes can conservatively retain an exited execution.
    # Retry the ordinary guard only for runtime occupancy/visibility blockers.
    for _ in range(3):
        reconciled = resource_admission.reconcile(apply=True)
        if not _record(repo, allocation).external_bindings:
            break
        outcome = next(
            row for row in reconciled["decision"]["outcomes"]
            if row["request_id"] == request_id
        )
        assert outcome.get("blocker") in {"RUNTIME_OCCUPIED", "RUNTIME_SCAN_UNCERTAIN"}, reconciled
        time.sleep(0.05)
    assert not _record(repo, allocation).external_bindings, reconciled
    assert _record(repo, allocation).lifecycle != "RELEASED"
    rows = [row for row in resource_admission.AdmissionLedger().read()["requests"].values()
            if row["request"].get("kind") == "execution"]
    assert len(rows) == 1 and rows[0]["state"] == "RELEASED"
    assert list(home.iterdir()) == []


def test_competing_lease_waits_without_execution_and_preserves_its_owner(task, tmp_path):
    repo, path, allocation = task
    lease = writer_lease.acquire_writer_lease(
        repo.repo,
        target=path,
        client_id="another-client",
        holder_pid=os.getpid(),
        session_id="another-session",
        run_id="another-run",
    )["decision"]["lease_id"]
    before = _store(repo).load().writer_leases[allocation]
    waiting = invoke(path, tmp_path, "terminal", [sys.executable, "-c", "raise SystemExit(93)"], wait=False)
    try:
        assert select.select([waiting.stderr], [], [], 10)[0]
        assert "RESOURCE_WAIT" in waiting.stderr.readline()
        assert waiting.poll() is None
        waiting.send_signal(signal.SIGINT)
        stdout, stderr = waiting.communicate(timeout=10)
        assert waiting.returncode == 130 and not stdout, stderr
    finally:
        if waiting.poll() is None:
            waiting.terminate()
            waiting.wait(timeout=10)
    after = _store(repo).load().writer_leases[allocation]
    assert after == before and after.lease_id == lease
    assert _record(repo, allocation).lifecycle == "ACTIVE"
    rows = [row for row in resource_admission.AdmissionLedger().read()["requests"].values()
            if row["request"].get("kind") == "execution"]
    assert len(rows) == 1 and rows[0]["state"] == "CANCELLED"


INTERRUPTED_REPOLL_ENTRYPOINT = """
import time
from worktree_controller import resource_admission
real_sleep, real_admit, polls = time.sleep, resource_admission.admit_execution, []
time.sleep = lambda seconds: real_sleep(min(seconds, 0.05))
def admit_then_interrupt(*args, cancel=False, **kwargs):
    # Ctrl-C arriving while the wait loop re-polls, after the first report.
    result = real_admit(*args, cancel=cancel, **kwargs)
    polls.append(cancel)
    if polls.count(False) == 2 and not cancel:
        raise KeyboardInterrupt
    return result
resource_admission.admit_execution = admit_then_interrupt
""" + SESSION_ENTRYPOINT


def test_interrupt_during_a_wait_repoll_cancels_the_waiting_request(task, tmp_path):
    repo, path, allocation = task
    writer_lease.acquire_writer_lease(
        repo.repo, target=path, client_id="another-client", holder_pid=os.getpid(),
        session_id="another-session", run_id="another-run",
    )
    result = invoke(path, tmp_path, "terminal", [sys.executable, "-c", "raise SystemExit(93)"],
                    entrypoint=INTERRUPTED_REPOLL_ENTRYPOINT)
    assert result.returncode == 130 and not result.stdout, result.stderr
    rows = [row for row in resource_admission.AdmissionLedger().read()["requests"].values()
            if row["request"].get("kind") == "execution"]
    assert len(rows) == 1 and rows[0]["state"] == "CANCELLED"


def test_failed_exec_releases_only_its_lease_without_ending_the_task(task, tmp_path):
    repo, path, allocation = task
    result = invoke(path, tmp_path, "terminal", [str(tmp_path / "missing-executable")])
    assert result.returncode == 2
    assert allocation not in _store(repo).load().writer_leases
    assert _record(repo, allocation).lifecycle == "ACTIVE"


def test_run_identity_is_required_to_check_the_same_session(task):
    repo, path, allocation = task
    identity = {
        "client_id": "terminal",
        "selector": str(os.getpid()),
        "session_id": "task-session",
        "run_id": "run-a",
    }
    assert writer_session.admission(path, **identity)["admission"] == "GRANTED"
    assert writer_session.check(path, **identity)["code"] == "WRITE_ALLOWED"
    before = _store(repo).load().writer_leases[allocation]
    assert writer_session.check(path, **(identity | {"run_id": "run-b"}))["admission"] == "REFUSED"
    assert _store(repo).load().writer_leases[allocation] == before


def test_ungoverned_terminal_command_needs_no_client_home_files(tmp_path):
    home = tmp_path / "empty-home"
    home.mkdir()
    # The M2 admission invariant refuses a target that is the client home root
    # or an ancestor of it; an ordinary session directory is admitted as before.
    session = tmp_path / "session"
    session.mkdir()
    result = invoke(session, home, "unregistered-client", [sys.executable, "-c", "print('ready')"])
    assert result.returncode == 0 and result.stdout == "ready\n"
    assert list(home.iterdir()) == []


def test_next_standalone_admission_recovers_finished_host_execution(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    first_path, second_path = tmp_path / "first", tmp_path / "second"
    first_path.mkdir()
    second_path.mkdir()
    command = [sys.executable, "-c", "raise SystemExit(0)"]
    assert invoke(first_path, home, "terminal", command).returncode == 0
    first = next(iter(resource_admission.AdmissionLedger().read()["requests"]))
    assert invoke(second_path, home, "terminal", command, run_id="next-run").returncode == 0
    rows = resource_admission.AdmissionLedger().read()["requests"]
    assert rows[first]["state"] == "RELEASED"
    assert sum(row["state"] == "ALLOCATED" for row in rows.values()) == 1


def test_next_admission_recovers_only_the_previous_managed_binding(task, tmp_path):
    repo, path, allocation = task
    home = tmp_path / "home"
    home.mkdir()
    command = [
        sys.executable,
        "-c",
        "from pathlib import Path;Path('mine.txt').write_text('change')",
    ]
    assert invoke(path, home, "terminal", command, run_id="next-run").returncode == 0
    first = next(
        key
        for key, row in resource_admission.AdmissionLedger().read()["requests"].items()
        if row["request"].get("kind") == "execution"
    )
    assert invoke(path, home, "terminal", command).returncode == 0
    rows = resource_admission.AdmissionLedger().read()["requests"]
    assert rows[first]["state"] == "RELEASED"
    assert len(_record(repo, allocation).external_bindings) == 1
    assert _record(repo, allocation).lifecycle == "ACTIVE"
