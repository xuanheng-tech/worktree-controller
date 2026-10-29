"""Session admission composes the existing steps in one process and changes nothing else."""

import errno
import json
import os
import select
import signal
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.test_controlled import acquire_one, adopt_canonical
from tests.test_resource_admission import POLICY, host  # noqa: F401 - fixture
from tests.test_shadow import GitRepository, git
from worktree_controller import cli, convergence, lifecycle, resource_admission, session_admission
from worktree_controller.git_backend import inventory
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore
from worktree_controller.writer_lease import runtime_binding

OWNER = "agent-workspace"

BIND_INTERRUPT_ENTRYPOINT = """
import signal
from worktree_controller import cli, resource_admission, session_admission
resource_admission.pressure = lambda: {
    'available_memory_bytes': 10**15, 'observed_at_ns': 1,
    'psi_avg10': {'cpu': 0.0, 'io': 0.0, 'memory': 0.0},
}
original = session_admission.acquire_writer_lease
def pause_after_lease(*args, **kwargs):
    acquired = original(*args, **kwargs)
    print(acquired['decision']['lease_id'], flush=True)
    signal.pause()
    raise AssertionError('interrupted binder must not resume')
session_admission.acquire_writer_lease = pause_after_lease
raise SystemExit(cli.main())
"""

BIND_BUDGET_INTERRUPT_ENTRYPOINT = """
import signal
from worktree_controller import cli, resource_admission
from worktree_controller.store import MetadataStore
from worktree_controller.writer_lease import runtime_binding
resource_admission.pressure = lambda: {
    'available_memory_bytes': 10**15, 'observed_at_ns': 1,
    'psi_avg10': {'cpu': 0.0, 'io': 0.0, 'memory': 0.0},
}
original = MetadataStore.replace_record
def pause_before_binding_write(store, record):
    if any(runtime_binding(value) is not None for value in record.external_bindings):
        lease = store._load_unlocked().writer_leases[record.allocation_id]
        print(lease.lease_id, flush=True)
        signal.pause()
        raise AssertionError('interrupted binder must not resume')
    return original(store, record)
MetadataStore.replace_record = pause_before_binding_write
raise SystemExit(cli.main())
"""

BIND_REPLY_INTERRUPT_ENTRYPOINT = """
import signal
from worktree_controller import cli, resource_admission, session_admission
from worktree_controller.git_backend import inventory
from worktree_controller.store import MetadataStore
resource_admission.pressure = lambda: {
    'available_memory_bytes': 10**15, 'observed_at_ns': 1,
    'psi_avg10': {'cpu': 0.0, 'io': 0.0, 'memory': 0.0},
}
original = session_admission.update_external_binding
def pause_after_binding(*args, **kwargs):
    bound = original(*args, **kwargs)['decision']
    snapshot = MetadataStore(inventory(args[0]).common_dir).load()
    record = snapshot.records[bound['allocation_id']]
    assert record.external_bindings == (bound['binding'],)
    print(snapshot.writer_leases[record.allocation_id].lease_id, flush=True)
    signal.pause()
    raise AssertionError('interrupted binder must not resume')
session_admission.update_external_binding = pause_after_binding
raise SystemExit(cli.main())
"""


REMOVE_BUDGET_FAULT_ENTRYPOINT = """
import errno
import signal
import sys
from pathlib import Path
from worktree_controller import cli, resource_admission
from worktree_controller.git_backend import inventory
from worktree_controller.store import MetadataStore
mode = sys.argv.pop(1)
original = resource_admission.release_execution
def interrupt_budget_release(repository_id, allocation_id, lease_id):
    # update_external_binding holds the repository lock after its durable removal.
    repo = Path(sys.argv[sys.argv.index('--repo') + 1])
    snapshot = MetadataStore(inventory(repo).common_dir)._load_unlocked()
    assert not snapshot.records[allocation_id].external_bindings
    ledger = resource_admission.AdmissionLedger()
    matches = [(key, row) for key, row in ledger.read()['requests'].items()
               if row['request'].get('kind') == 'execution'
               and row['request'].get('allocation_id') == allocation_id
               and row.get('lease_id') == lease_id]
    assert len(matches) == 1 and matches[0][1]['state'] == 'ALLOCATED'
    print(lease_id, flush=True)
    if mode == 'sigkill':
        signal.pause()
        raise AssertionError('interrupted remover must not resume')
    assert mode == 'write-error'
    def fail_save(self, state):
        assert state['requests'][matches[0][0]]['state'] == 'RELEASED'
        assert self.read()['requests'][matches[0][0]]['state'] == 'ALLOCATED'
        raise OSError(errno.EIO, 'fixture budget release write failed')
    resource_admission.AdmissionLedger._save = fail_save
    return original(repository_id, allocation_id, lease_id)
resource_admission.release_execution = interrupt_budget_release
raise SystemExit(cli.main())
"""


DELAYED_EXIT_ENTRYPOINT = """
import json
import sys
from worktree_controller import cli, resource_admission
resource_admission.pressure = lambda: {
    'available_memory_bytes': 10**15, 'observed_at_ns': 1,
    'psi_avg10': {'cpu': 0.0, 'io': 0.0, 'memory': 0.0},
}
mode, allocation_id = sys.argv[1:3]
del sys.argv[1:3]
def pause(value):
    print(json.dumps(value), flush=True)
    assert sys.stdin.readline().strip() == 'continue'
if mode == 'binding':
    original = cli.update_external_binding
    def delayed(*args, **kwargs):
        assert kwargs['action'] == 'remove'
        pause({'boundary': 'old-binding-request', 'record_version': kwargs['expected_record_version']})
        return original(*args, **kwargs)
    cli.update_external_binding = delayed
else:
    assert mode == 'queue'
    original = resource_admission._reconcile_execution
    def delayed(row, *, apply):
        if row['request'].get('allocation_id') == allocation_id:
            assert apply and row['state'] == 'ALLOCATED'
            pause({'boundary': 'old-execution-row', 'lease_id': row['lease_id'],
                   'holder': row['holder'], 'ledger_row': row})
        return original(row, apply=apply)
    resource_admission._reconcile_execution = delayed
raise SystemExit(cli.main())
"""


def _store(repository):
    return MetadataStore(inventory(repository.repo).common_dir)


def _admit(repository, task):
    head = git(repository.repo, "rev-parse", "HEAD")
    return session_admission.admit_session(
        repository.repo, task_key=task, authority_key=task, module="agent-execution",
        owner=OWNER, branch=f"agent/session/{task}", base_ref=head,
        client_id=OWNER, session_id=f"session-{task}",
    )


@pytest.fixture
def governed(host):  # noqa: F811 - pytest fixture
    with GitRepository(linked=0, policy_text=POLICY) as repository:
        adopt_canonical(repository)
        yield repository


@pytest.fixture
def executor():
    processes = []

    def launch(cwd):
        process = subprocess.Popen(["sleep", "60"], cwd=cwd)
        processes.append(process)
        return process

    yield launch
    for process in processes:
        process.kill()
        process.wait()


def _execution_admit(repository, path, session_id, **arguments):
    return resource_admission.admit_execution(
        repository.repo, target=path, owner=OWNER, client_id=OWNER,
        session_id=session_id, **arguments,
    )["decision"]


def _bind_arguments(repository, allocation, holder_pid, *, run_id=None):
    arguments = [
        "session-bind", "--repo", str(repository.repo), "--path", allocation["path"],
        "--task-key", allocation["task_key"], "--authority-key", allocation["authority_key"],
        "--owner", OWNER, "--client-id", OWNER, "--holder-pid", str(holder_pid),
        "--session-id", f"session-{allocation['task_key']}", "--json",
    ]
    if run_id is not None:
        arguments += ["--run-id", run_id]
    return arguments


def _interrupt_bind(repository, allocation, holder_pid, *, boundary="lease"):
    # These boundaries follow real durable writes; SIGKILL cannot run compensation.
    entrypoint = {"lease": BIND_INTERRUPT_ENTRYPOINT, "budget": BIND_BUDGET_INTERRUPT_ENTRYPOINT,
                  "binding": BIND_REPLY_INTERRUPT_ENTRYPOINT}[boundary]
    with subprocess.Popen(
        [sys.executable, "-B", "-c", entrypoint,
         *_bind_arguments(repository, allocation, holder_pid)],
        env={
            "PATH": os.defpath, "LANG": "C.UTF-8", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1", "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"],
            "PYTHONPATH": str(Path(session_admission.__file__).parent.parent),
        },
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) as binder:
        try:
            assert select.select([binder.stdout], [], [], 10)[0], "binder did not reach bind boundary"
            lease_id = binder.stdout.readline().strip()
        finally:
            binder.kill()
            output, error = binder.communicate(timeout=5)
        assert binder.returncode == -signal.SIGKILL and not output and not error
    lease = _store(repository).load().writer_leases[allocation["allocation_id"]]
    assert lease.lease_id == lease_id and lease.holder.pid == holder_pid
    return lease


def _raw_line(stream):
    # readline() may buffer later output that communicate(), reading the raw fd, never sees.
    data = b""
    while not data.endswith(b"\n") and (byte := os.read(stream.fileno(), 1)):
        data += byte
    return data.decode()


def _cli_process(arguments):
    return subprocess.run(
        [sys.executable, "-B", "-c",
         "from worktree_controller import cli, resource_admission\n"
         "resource_admission.pressure = lambda: {'available_memory_bytes': 10**15, "
         "'observed_at_ns': 1, 'psi_avg10': {'cpu': 0.0, 'io': 0.0, 'memory': 0.0}}\n"
         "raise SystemExit(cli.main())", *arguments],
        env={
            "PATH": os.defpath, "LANG": "C.UTF-8", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1", "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"],
            "PYTHONPATH": str(Path(session_admission.__file__).parent.parent),
        },
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
    )


def _bind_decision(repository, allocation, holder_pid, capsys, *, run_id=None):
    assert cli.main(_bind_arguments(repository, allocation, holder_pid, run_id=run_id)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert not payload["errors"] and not payload["warnings"]
    return payload["decision"]


@pytest.fixture
def bound_session(governed):
    admitted = _admit(governed, "agent-session-old")["decision"]
    allocation = admitted["allocation"]
    path = Path(allocation["path"])
    process = subprocess.Popen(["sleep", "60"], cwd=path, start_new_session=True)
    try:
        bound = session_admission.bind_session(
            governed.repo, target=path, task_key="agent-session-old",
            authority_key="agent-session-old", owner=OWNER, client_id=OWNER,
            holder_pid=process.pid, session_id="session-agent-session-old",
        )["decision"]
        assert bound["code"] == "SESSION_BOUND"
        yield allocation, admitted["execution"]["request_ids"][0], process
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


def test_session_admit_matches_the_individual_steps(governed):
    admitted = _admit(governed, "agent-session-one")["decision"]
    assert admitted["code"] == "SESSION_ADMITTED"
    allocation = admitted["allocation"]
    assert admitted["execution"]["code"] == "RESOURCE_AVAILABLE"
    assert admitted["admission_strategy"] == "resource-budget-v1"
    record = _store(governed).load().records[allocation["allocation_id"]]
    assert (record.task_key, record.owner, record.role, record.lifecycle) == (
        "agent-session-one", OWNER, "feature", "ACTIVE")
    assert allocation["path"] == str(next(
        item.path for item in inventory(governed.repo).worktrees if not item.canonical))
    assert allocation["canonical_path"] == str(governed.repo)
    assert allocation["head"] == git(governed.repo, "rev-parse", "HEAD")
    rows = resource_admission.AdmissionLedger().read()["requests"].values()
    assert [row["state"] for row in rows if row["request"].get("kind") == "execution"] == ["RESERVED"]


def test_session_admit_changes_nothing_on_an_uncommon_start(governed, monkeypatch):
    first = _admit(governed, "agent-session-one")["decision"]
    before = {item.path for item in inventory(governed.repo).worktrees}
    again = _admit(governed, "agent-session-one")["decision"]
    assert again["code"] == "SESSION_ADMISSION_FALLBACK" and again["changed"] is False
    assert again["allocation_id"] == first["allocation"]["allocation_id"]
    linked = session_admission.admit_session(
        __import__("pathlib").Path(first["allocation"]["path"]), task_key="agent-session-two",
        authority_key="agent-session-two", module="agent-execution", owner=OWNER,
        branch="agent/session/two", base_ref="HEAD", client_id=OWNER, session_id="s2")["decision"]
    assert linked["code"] == "SESSION_ADMISSION_FALLBACK"

    def blocked(*_args, **_kwargs):
        return {"decision": {"code": "BLOCK_BUDGET", "reason": "fixture"}}

    def unexpected(*_args, **_kwargs):
        pytest.fail("a refused plan must not acquire")

    monkeypatch.setattr(session_admission, "_plan_from_snapshot", blocked)
    monkeypatch.setattr(session_admission, "acquire", unexpected)
    refused = _admit(governed, "agent-session-three")["decision"]
    assert refused["code"] == "SESSION_ADMISSION_FALLBACK"
    assert refused["plan"]["code"] == "BLOCK_BUDGET"
    assert {item.path for item in inventory(governed.repo).worktrees} == before


def test_session_admit_rechecks_authority_acquired_after_planning(governed, monkeypatch):
    original = session_admission.acquire

    def raced(repository, **arguments):
        original(repository, task_key="competitor", authority_key=arguments["authority_key"],
            module="agent-execution", role="feature", owner=OWNER, branch="agent/competitor",
            base_ref="HEAD", target=governed.root / "competitor", observe=False)
        return original(repository, **arguments)

    monkeypatch.setattr(session_admission, "acquire", raced)
    with pytest.raises(ControllerError, match="active feature allocation") as refused:
        _admit(governed, "agent-session-raced")
    assert refused.value.code == "BLOCK_DUPLICATE_AUTHORITY"
    snapshot = _store(governed).load()
    assert not any(record.task_key == "agent-session-raced" for record in snapshot.records.values())
    assert not any(row["request"].get("kind") == "execution"
        for row in resource_admission.AdmissionLedger().read()["requests"].values())


def test_session_admit_revalidates_metadata_changed_after_planning(governed, monkeypatch):
    original = session_admission.acquire
    store = _store(governed)
    before = set(store.load().records)

    def corrupted(repository, **arguments):
        value = json.loads(store.repo_file.read_text())
        value["repository_id"] = "invalid"
        store._atomic_json(store.repo_file, value)
        return original(repository, **arguments)

    monkeypatch.setattr(session_admission, "acquire", corrupted)
    with pytest.raises(ControllerError, match="repository_id must be a UUID") as refused:
        _admit(governed, "agent-session-corrupted")
    assert refused.value.code == "INVALID_METADATA"
    assert {path.stem for path in store.records_dir.glob("*.json")} == before
    assert not any(row["request"].get("kind") == "execution"
        for row in resource_admission.AdmissionLedger().read()["requests"].values())


def test_session_bind_grants_checks_and_registers_one_holder(governed, executor):
    allocation = _admit(governed, "agent-session-one")["decision"]["allocation"]
    process = executor(allocation["path"])
    bound = session_admission.bind_session(
        governed.repo, target=__import__("pathlib").Path(allocation["path"]),
        task_key="agent-session-one", authority_key="agent-session-one", owner=OWNER,
        client_id=OWNER, holder_pid=process.pid, session_id="session-agent-session-one",
    )["decision"]
    assert bound["code"] == "SESSION_BOUND" and bound["write_guard"] == "WRITE_ALLOWED"
    snapshot = _store(governed).load()
    lease = snapshot.writer_leases[allocation["allocation_id"]]
    assert lease.lease_id == bound["lease"]["lease_id"] and lease.holder.pid == process.pid
    record = snapshot.records[allocation["allocation_id"]]
    assert bound["runtime_binding"] in record.external_bindings
    assert record.record_version == bound["record_version"]
    holder, lease_id = runtime_binding(bound["runtime_binding"])
    assert (holder.pid, lease_id) == (process.pid, lease.lease_id)
    rows = resource_admission.AdmissionLedger().read()["requests"].values()
    execution = [row for row in rows if row["request"].get("kind") == "execution"]
    assert [(row["state"], row["lease_id"]) for row in execution] == [("ALLOCATED", lease.lease_id)]


@pytest.mark.parametrize("filesystem_error", [False, True])
def test_a_bind_that_fails_after_its_lease_releases_that_lease(
    governed, executor, monkeypatch, capsys, filesystem_error,
):
    allocation = _admit(governed, "agent-session-one")["decision"]["allocation"]
    process = executor(allocation["path"])

    def conflict(*_args, **_kwargs):
        if filesystem_error:
            raise OSError(errno.EIO, "fixture binding write failed")
        raise ControllerError("RECORD_VERSION_CONFLICT", "fixture concurrent change")

    monkeypatch.setattr(session_admission, "update_external_binding", conflict)
    code = cli.main([
        "session-bind", "--repo", str(governed.repo), "--path", allocation["path"],
        "--task-key", "agent-session-one", "--authority-key", "agent-session-one",
        "--owner", OWNER, "--client-id", OWNER, "--holder-pid", str(process.pid),
        "--session-id", "session-agent-session-one", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    expected_code = "FILESYSTEM_ERROR" if filesystem_error else "RECORD_VERSION_CONFLICT"
    assert code == 2 and payload["errors"][0]["code"] == expected_code
    assert payload["decision"]["lease_outcome"]["lease_released"] is True
    snapshot = _store(governed).load()
    assert allocation["allocation_id"] not in snapshot.writer_leases
    assert not snapshot.records[allocation["allocation_id"]].external_bindings


@pytest.mark.parametrize("filesystem_error", [False, True])
@pytest.mark.parametrize("release_filesystem_error", [False, True])
def test_failed_bind_and_compensation_report_exact_lease_for_idempotent_recovery(
    governed, executor, bound_session, monkeypatch, capsys,
    filesystem_error, release_filesystem_error,
):
    other_allocation, other_request_id, _other_process = bound_session
    allocation = _admit(governed, "agent-session-one")["decision"]["allocation"]
    process = executor(allocation["path"])
    before = _store(governed).load()
    requests_before = resource_admission.AdmissionLedger().read()["requests"]
    primary = (
        OSError(errno.EIO, "fixture binding write failed") if filesystem_error
        else ControllerError("RECORD_VERSION_CONFLICT", "fixture concurrent change")
    )
    compensation = (
        OSError(errno.EACCES, "fixture lease removal failed") if release_filesystem_error
        else ControllerError("WRITER_LEASE_MISMATCH", "fixture release refused")
    )
    release_attempts = []

    def fail_binding(*_args, **_kwargs):
        raise primary

    def fail_release(_repo, **arguments):
        release_attempts.append(arguments)
        raise compensation

    with monkeypatch.context() as failures:
        failures.setattr(session_admission, "update_external_binding", fail_binding)
        failures.setattr(session_admission, "release_writer_lease", fail_release)
        code = cli.main([
            "session-bind", "--repo", str(governed.repo), "--path", allocation["path"],
            "--task-key", "agent-session-one", "--authority-key", "agent-session-one",
            "--owner", OWNER, "--client-id", OWNER, "--holder-pid", str(process.pid),
            "--session-id", "session-agent-session-one", "--json",
        ])
    payload = json.loads(capsys.readouterr().out)
    expected_code = "FILESYSTEM_ERROR" if filesystem_error else primary.code
    assert code == 2 and payload["errors"] == [{"code": expected_code, "message": str(primary)}]
    outcome = payload["decision"]["lease_outcome"]
    assert outcome["lease_released"] is False
    assert outcome["release_error"] == (
        "PermissionError" if release_filesystem_error else compensation.code
    )
    snapshot = _store(governed).load()
    lease = snapshot.writer_leases[allocation["allocation_id"]]
    reported = outcome["lease"]
    assert outcome["lease_id"] == reported["lease_id"] == lease.lease_id
    assert reported["allocation_id"] == allocation["allocation_id"]
    assert reported["owner"] == OWNER and reported["holder"] == asdict(lease.holder)
    assert release_attempts == [{
        "target": Path(allocation["path"]), "holder_pid": process.pid,
        "lease_id": lease.lease_id, "holder": lease.holder,
    }]
    assert snapshot.records == before.records
    assert snapshot.writer_leases[other_allocation["allocation_id"]] == (
        before.writer_leases[other_allocation["allocation_id"]]
    )

    recover = [
        "writer-lease-release", "--repo", str(governed.repo), "--path", allocation["path"],
        "--lease-id", reported["lease_id"], "--holder-pid", str(reported["holder"]["pid"]),
        "--holder-boot-id", reported["holder"]["boot_id"],
        "--holder-start-ticks", str(reported["holder"]["start_ticks"]),
        "--client-id", reported["holder"]["client_id"],
        "--session-id", reported["holder"]["session_id"], "--json",
    ]
    for expected in ("WRITER_LEASE_RELEASED", "NO_WRITER_LEASE"):
        assert cli.main(recover) == 0
        recovered = json.loads(capsys.readouterr().out)
        assert recovered["decision"]["code"] == expected and not recovered["errors"]
    after = _store(governed).load()
    assert allocation["allocation_id"] not in after.writer_leases
    assert after.records == before.records
    assert after.writer_leases == before.writer_leases
    requests_after = resource_admission.AdmissionLedger().read()["requests"]
    assert requests_after.keys() == requests_before.keys()
    assert requests_after[other_request_id] == requests_before[other_request_id]
    assert after.publication_lease == before.publication_lease


def test_killed_binder_retries_one_lease_binding_and_budget(
    governed, executor, bound_session, capsys,
):
    other, other_request, _other_holder = bound_session
    admitted = _admit(governed, "interrupted-bind")["decision"]
    allocation = {**admitted["allocation"], "task_key": "interrupted-bind",
                  "authority_key": "interrupted-bind"}
    request_id = admitted["execution"]["request_ids"][0]
    holder = executor(allocation["path"])
    store, ledger = _store(governed), resource_admission.AdmissionLedger()
    before, requests = store.load(), ledger.read()["requests"]
    paths = {item.path for item in inventory(governed.repo).worktrees}

    lease = _interrupt_bind(governed, allocation, holder.pid)
    interrupted = store.load()
    assert interrupted.records == before.records
    assert ledger.read()["requests"] == requests
    assert requests[request_id]["state"] == "RESERVED" and requests[request_id].get("holder") is None
    assert not interrupted.records[allocation["allocation_id"]].external_bindings

    bound = _bind_decision(governed, allocation, holder.pid, capsys)
    assert bound["code"] == "SESSION_BOUND" and bound["write_guard"] == "WRITE_ALLOWED"
    assert bound["lease"]["lease_id"] == lease.lease_id
    record, row = store.load().records[allocation["allocation_id"]], ledger.read()["requests"][request_id]
    assert record.external_bindings == (bound["runtime_binding"],)
    assert row["state"] == "ALLOCATED" and row["lease_id"] == lease.lease_id
    assert row["request"] == requests[request_id]["request"]
    assert [event["state"] for event in row["history"]].count("ALLOCATED") == 1

    repeated = _bind_decision(governed, allocation, holder.pid, capsys)
    assert repeated["lease"]["lease_id"] == lease.lease_id
    assert store.load().records[allocation["allocation_id"]] == record
    assert ledger.read()["requests"][request_id] == row
    fallback = _admit(governed, "interrupted-bind")["decision"]
    assert fallback["code"] == "SESSION_ADMISSION_FALLBACK" and not fallback["changed"]
    assert fallback["allocation_id"] == allocation["allocation_id"]
    assert {item.path for item in inventory(governed.repo).worktrees} == paths
    after, current = store.load(), ledger.read()["requests"]
    assert set(current) == set(requests)
    assert set(after.writer_leases) == set(before.writer_leases) | {allocation["allocation_id"]}
    assert current[other_request] == requests[other_request]
    assert after.records[other["allocation_id"]] == before.records[other["allocation_id"]]
    assert after.writer_leases[other["allocation_id"]] == before.writer_leases[other["allocation_id"]]
    assert after.publication_lease == before.publication_lease
    assert after.integration_intents == before.integration_intents


def test_killed_binder_lease_protects_its_holder_and_surviving_child(governed, executor):
    allocation = {**_admit(governed, "interrupted-group")["decision"]["allocation"],
                  "task_key": "interrupted-group", "authority_key": "interrupted-group"}
    store, ledger = _store(governed), resource_admission.AdmissionLedger()
    contender = executor(governed.root)
    child_pid = None
    with subprocess.Popen(
        [sys.executable, "-B", "-c",
         "import subprocess; child=subprocess.Popen(['sleep', '60']); "
         "print(child.pid, flush=True); child.wait()"],
        cwd=allocation["path"], start_new_session=True, stdout=subprocess.PIPE, text=True,
    ) as holder:
        try:
            assert select.select([holder.stdout], [], [], 10)[0], "holder did not start child"
            child_pid = int(holder.stdout.readline())
            assert os.getpgid(child_pid) == holder.pid
            _interrupt_bind(governed, allocation, holder.pid)
            before, requests = store.load(), ledger.read()["requests"]
            for holder_finished in (False, True):
                if holder_finished:
                    holder.kill()
                    holder.wait(timeout=5)
                    assert os.getpgid(child_pid) == holder.pid
                with pytest.raises(ControllerError) as refused:
                    session_admission.bind_session(
                        governed.repo, target=Path(allocation["path"]),
                        task_key=allocation["task_key"], authority_key=allocation["authority_key"],
                        owner=OWNER, client_id=OWNER, holder_pid=contender.pid,
                        session_id=f"session-{allocation['task_key']}",
                    )
                assert refused.value.code == "WRITER_LEASE_HELD"
                assert store.load() == before and ledger.read()["requests"] == requests
        finally:
            with suppress(ProcessLookupError):
                os.killpg(holder.pid, signal.SIGKILL)
            holder.wait(timeout=5)
            if child_pid is not None:
                # In a PID namespace this test process adopts the orphan; reap only that child.
                with suppress(ChildProcessError):
                    os.waitpid(child_pid, 0)


def test_killed_binder_keeps_budget_until_exit_cleanup_and_explicit_cancel(
    governed, executor, bound_session, capsys, monkeypatch,
):
    # A fixed memory sample keeps the peer's observed PSS out of budget assertions.
    monkeypatch.setattr(resource_admission, "execution_memory", lambda _row: 0)
    other, other_request, _other_holder = bound_session
    admitted = _admit(governed, "interrupted-exit")["decision"]
    allocation = {**admitted["allocation"], "task_key": "interrupted-exit",
                  "authority_key": "interrupted-exit"}
    target = Path(allocation["path"])
    session = f"session-{allocation['task_key']}"
    request_id = admitted["execution"]["request_ids"][0]
    holder = executor(target)
    old_lease = _interrupt_bind(governed, allocation, holder.pid)
    holder.kill()
    holder.wait(timeout=5)
    store, ledger = _store(governed), resource_admission.AdmissionLedger()
    before, requests = store.load(), ledger.read()["requests"]

    resource_admission.reconcile(repository_path=governed.repo, apply=True)
    assert ledger.read()["requests"][request_id] == requests[request_id]
    with pytest.raises(ControllerError) as refused:
        _execution_admit(governed, target, session, run_id="replacement-run")
    assert refused.value.code == "ADMISSION_REQUEST_DRIFT"
    assert store.load() == before and ledger.read()["requests"] == requests

    with pytest.raises(ControllerError) as refused:
        _execution_admit(governed, target, session, cancel=True)
    assert refused.value.code == "ADMISSION_RECOVERY_REQUIRED"
    assert store.load() == before and ledger.read()["requests"] == requests

    record = store.load().records[allocation["allocation_id"]]
    parked = lifecycle.park(governed.repo, target=target, owner=OWNER,
                            expected_record_version=record.record_version)["decision"]
    assert store.load().records[allocation["allocation_id"]].lifecycle == "RETAINED"
    assert allocation["allocation_id"] not in store.load().writer_leases
    assert ledger.read()["requests"] == requests
    lifecycle.resume(governed.repo, target=target, owner=OWNER,
                     expected_record_version=parked["record_version"])
    cancelled = _execution_admit(governed, target, session, cancel=True)
    assert cancelled["code"] == "EXECUTION_ADMISSION_CANCELLED"
    assert cancelled["request_ids"] == [request_id]
    assert ledger.read()["requests"][request_id]["state"] == "CANCELLED"
    replacement = _execution_admit(governed, target, session, run_id="replacement-run")
    new_request = replacement["request_ids"][0]
    assert new_request != request_id and replacement["code"] == "RESOURCE_AVAILABLE"
    assert replacement["outstanding_memory_bytes"] == 2 * resource_admission.DEFAULT_EXECUTION_MEMORY
    new_holder = executor(target)
    bound = _bind_decision(governed, allocation, new_holder.pid, capsys, run_id="replacement-run")
    assert bound["lease"]["lease_id"] != old_lease.lease_id
    after, current = store.load(), ledger.read()["requests"]
    assert set(after.records) == set(before.records)
    record = after.records[allocation["allocation_id"]]
    assert (record.allocation_id, record.authority_key, record.scope_paths, record.lifecycle) == (
        allocation["allocation_id"], before.records[allocation["allocation_id"]].authority_key,
        before.records[allocation["allocation_id"]].scope_paths, "ACTIVE",
    )
    assert record.external_bindings == (bound["runtime_binding"],) and target.exists()
    assert current[request_id]["state"] == "CANCELLED" and current[new_request]["state"] == "ALLOCATED"
    assert set(current) == set(requests) | {new_request}
    assert current[other_request] == requests[other_request]
    assert after.writer_leases[other["allocation_id"]] == before.writer_leases[other["allocation_id"]]
    assert after.records[other["allocation_id"]] == before.records[other["allocation_id"]]
    assert after.publication_lease == before.publication_lease
    assert after.integration_intents == before.integration_intents


@pytest.mark.parametrize("fault", ["sigkill", "binding-write", "lease-release"])
def test_durable_budget_without_binding_recovers_only_after_runtime_exit(
    governed, executor, bound_session, capsys, monkeypatch, fault,
):
    monkeypatch.setattr(resource_admission, "execution_memory", lambda _row: 0)
    other, other_request, _other_holder = bound_session
    task, authority = "durable-bind-budget", "durable-bind-scope"
    target, acquired = acquire_one(
        governed, task=task, authority=authority, owner=OWNER, scope_paths=("owned.txt",),
    )
    allocation = {**acquired["decision"], "task_key": task, "authority_key": authority}
    session = f"session-{task}"
    request_id = _execution_admit(governed, target, session)["request_ids"][0]
    store, ledger = _store(governed), resource_admission.AdmissionLedger()
    before, requests = store.load(), ledger.read()["requests"]
    original = before.records[allocation["allocation_id"]]
    paths = {item.path for item in inventory(governed.repo).worktrees}

    def reconcile_queue(*, apply):
        arguments = ["admission-queue", "--repo", str(governed.repo), "--json"]
        assert cli.main(arguments + (["--apply"] if apply else [])) == 0
        payload = json.loads(capsys.readouterr().out)
        assert not payload["errors"]
        return next((row for row in payload["decision"]["outcomes"]
                     if row["request_id"] == request_id), None)

    child_pid = None
    with subprocess.Popen(
        [sys.executable, "-B", "-c",
         "import subprocess; child=subprocess.Popen(['sleep', '60']); "
         "print(child.pid, flush=True); child.wait()"],
        cwd=target, start_new_session=True, stdout=subprocess.PIPE, text=True,
    ) as holder:
        try:
            assert select.select([holder.stdout], [], [], 10)[0], "holder did not start child"
            child_pid = int(holder.stdout.readline())
            assert os.getpgid(child_pid) == holder.pid
            if fault == "sigkill":
                lease = _interrupt_bind(governed, allocation, holder.pid, boundary="budget")
            else:
                leases = []
                original_replace = MetadataStore.replace_record
                primary = OSError(errno.EIO, "fixture runtime binding write failed")

                def fail_binding_write(self, record):
                    if record.allocation_id != allocation["allocation_id"] or not record.external_bindings:
                        return original_replace(self, record)
                    lease = self._load_unlocked().writer_leases[record.allocation_id]
                    row = ledger.read()["requests"][request_id]
                    assert row["state"] == "ALLOCATED" and row["lease_id"] == lease.lease_id
                    assert row["holder"] == asdict(lease.holder)
                    leases.append(lease)
                    raise primary

                def fail_release(*_args, **_kwargs):
                    raise OSError(errno.EACCES, "fixture exact lease release failed")

                with monkeypatch.context() as failures:
                    failures.setattr(MetadataStore, "replace_record", fail_binding_write)
                    if fault == "lease-release":
                        failures.setattr(session_admission, "release_writer_lease", fail_release)
                    assert cli.main(_bind_arguments(governed, allocation, holder.pid)) == 2
                payload = json.loads(capsys.readouterr().out)
                assert payload["errors"] == [{"code": "FILESYSTEM_ERROR", "message": str(primary)}]
                assert len(leases) == 1
                lease = leases[0]
                outcome = payload["decision"]["lease_outcome"]
                assert outcome["lease_id"] == lease.lease_id
                assert outcome["lease_released"] == (fault == "binding-write")
                if fault == "lease-release":
                    assert outcome["release_error"] == "PermissionError"
                    assert outcome["lease"]["lease_id"] == lease.lease_id
                    assert outcome["lease"]["allocation_id"] == original.allocation_id
                    assert outcome["lease"]["owner"] == OWNER
                    assert outcome["lease"]["holder"] == asdict(lease.holder)

            interrupted, current = store.load(), ledger.read()["requests"]
            allocated = current[request_id]
            assert interrupted.records == before.records
            assert (original.allocation_id in interrupted.writer_leases) == (fault != "binding-write")
            assert allocated["state"] == "ALLOCATED" and allocated["lease_id"] == lease.lease_id
            assert allocated["holder"] == asdict(lease.holder)
            assert allocated["request"] == requests[request_id]["request"]
            assert [event["state"] for event in allocated["history"]].count("ALLOCATED") == 1
            occupied = reconcile_queue(apply=True)
            assert occupied["result"] == {"code": "EXECUTION_OCCUPIED", "process_alive": True}
            assert store.load() == interrupted and ledger.read()["requests"] == current

            holder.kill()
            holder.wait(timeout=5)
            assert os.getpgid(child_pid) == holder.pid
            child_state = Path(f"/proc/{child_pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            assert child_state not in {"Z", "X"}
            assert reconcile_queue(apply=True)["blocker"] == "RUNTIME_OCCUPIED"
            assert store.load() == interrupted and ledger.read()["requests"] == current
        finally:
            with suppress(ProcessLookupError):
                os.killpg(holder.pid, signal.SIGKILL)
            holder.wait(timeout=5)
            if child_pid is not None:
                with suppress(ChildProcessError):
                    os.waitpid(child_pid, 0)

    # Outside a PID namespace the orphan is reaped by init, not this test process.
    deadline = time.monotonic() + 5
    child_stat = Path(f"/proc/{child_pid}/stat")
    while child_stat.exists():
        try:
            if child_stat.read_text().rsplit(")", 1)[1].split()[0] in {"Z", "X"}:
                break
        except FileNotFoundError:
            break
        assert time.monotonic() < deadline, "holder child did not exit"
        time.sleep(0.01)
    assert reconcile_queue(apply=False)["result"]["code"] == "EXECUTION_RECONCILIATION_PENDING"
    assert store.load() == interrupted and ledger.read()["requests"] == current
    settled = reconcile_queue(apply=True)
    assert settled["result"] == {"code": "EXECUTION_RESOURCES_RELEASED", "task_completed": False}
    recovered, settled_requests = store.load(), ledger.read()["requests"]
    terminal = settled_requests[request_id]
    assert terminal["state"] == "RELEASED" and terminal["history"][:-1] == allocated["history"]
    assert [event["state"] for event in terminal["history"]].count("RELEASED") == 1
    record = recovered.records[original.allocation_id]
    assert not record.external_bindings and original.allocation_id not in recovered.writer_leases
    assert reconcile_queue(apply=True) is None
    assert store.load() == recovered and ledger.read()["requests"] == settled_requests
    if record.lifecycle == "RETAINED":
        lifecycle.resume(governed.repo, target=target, owner=OWNER,
                         expected_record_version=record.record_version)
    else:
        assert record.lifecycle == "ACTIVE"
    replacement = _execution_admit(governed, target, session, run_id="replacement-run")
    replacement_id = replacement["request_ids"][0]
    assert replacement["code"] == "RESOURCE_AVAILABLE" and replacement_id != request_id
    new_holder = executor(target)
    bound = _bind_decision(governed, allocation, new_holder.pid, capsys, run_id="replacement-run")
    assert bound["lease"]["lease_id"] != lease.lease_id
    after, final_requests = store.load(), ledger.read()["requests"]
    record = after.records[original.allocation_id]
    for field in ("allocation_id", "task_key", "authority_key", "owner", "scope_paths", "base_commit", "task_commit"):
        assert getattr(record, field) == getattr(original, field), field
    assert record.scope_paths == ("owned.txt",) and record.lifecycle == "ACTIVE"
    assert record.external_bindings == (bound["runtime_binding"],)
    assert git(target, "rev-parse", "HEAD") == allocation["head"]
    active = [row for row in final_requests.values()
              if row["request"].get("kind") == "execution"
              and row["request"].get("allocation_id") == original.allocation_id
              and row["state"] in {"RESERVED", "ALLOCATED", "ATTENTION_REQUIRED"}]
    assert active == [final_requests[replacement_id]] and active[0]["state"] == "ALLOCATED"
    assert [event["state"] for event in active[0]["history"]].count("ALLOCATED") == 1
    repeated = _bind_decision(governed, allocation, new_holder.pid, capsys, run_id="replacement-run")
    assert repeated["lease"]["lease_id"] == bound["lease"]["lease_id"]
    assert store.load().records == after.records and ledger.read()["requests"] == final_requests
    assert final_requests[request_id] == terminal and set(final_requests) == set(requests) | {replacement_id}
    assert final_requests[other_request] == requests[other_request]
    assert after.records[other["allocation_id"]] == before.records[other["allocation_id"]]
    assert after.writer_leases[other["allocation_id"]] == before.writer_leases[other["allocation_id"]]
    assert after.publication_lease == before.publication_lease
    assert after.integration_intents == before.integration_intents
    assert {item.path for item in inventory(governed.repo).worktrees} == paths


@pytest.mark.parametrize("scenario", ["retry", "foreign-identity", "exit"])
def test_lost_bind_response_preserves_identity_and_recovers_only_after_exit(
    governed, executor, bound_session, capsys, monkeypatch, scenario,
):
    monkeypatch.setattr(resource_admission, "execution_memory", lambda _row: 0)
    other, other_request, _other_holder = bound_session
    task, authority = "lost-bind-response", "lost-bind-scope"
    target, acquired = acquire_one(
        governed, task=task, authority=authority, owner=OWNER, scope_paths=("owned.txt",),
    )
    allocation = {**acquired["decision"], "task_key": task, "authority_key": authority}
    request_id = _execution_admit(governed, target, f"session-{task}")["request_ids"][0]
    store, ledger = _store(governed), resource_admission.AdmissionLedger()
    before, requests = store.load(), ledger.read()["requests"]
    original = before.records[allocation["allocation_id"]]
    paths = {item.path for item in inventory(governed.repo).worktrees}

    def reconcile_queue(*, apply):
        arguments = ["admission-queue", "--repo", str(governed.repo), "--json"]
        assert cli.main(arguments + (["--apply"] if apply else [])) == 0
        payload = json.loads(capsys.readouterr().out)
        assert not payload["errors"]
        return next((row for row in payload["decision"]["outcomes"]
                     if row["request_id"] == request_id), None)

    child_pid = None
    with subprocess.Popen(
        [sys.executable, "-B", "-c",
         "import subprocess; child=subprocess.Popen(['sleep', '60']); "
         "print(child.pid, flush=True); child.wait()"],
        cwd=target, start_new_session=True, stdout=subprocess.PIPE, text=True,
    ) as holder:
        try:
            assert select.select([holder.stdout], [], [], 10)[0], "holder did not start child"
            child_pid = int(holder.stdout.readline())
            lease = _interrupt_bind(governed, allocation, holder.pid, boundary="binding")
            snapshot, current = store.load(), ledger.read()["requests"]
            bound_record = snapshot.records[original.allocation_id]
            assert bound_record.record_version == original.record_version + 1
            assert len(bound_record.external_bindings) == 1
            binding = bound_record.external_bindings[0]
            assert runtime_binding(binding) == (lease.holder, lease.lease_id)
            allocated = current[request_id]
            assert allocated["state"] == "ALLOCATED" and allocated["lease_id"] == lease.lease_id
            assert allocated["holder"] == asdict(lease.holder)
            assert allocated["request"] == requests[request_id]["request"]
            assert [event["state"] for event in allocated["history"]].count("ALLOCATED") == 1
            if scenario == "retry":
                for _ in range(2):
                    result = _cli_process(_bind_arguments(governed, allocation, holder.pid))
                    assert result.returncode == 0 and not result.stderr
                    payload = json.loads(result.stdout)
                    assert not payload["errors"]
                    decision = payload["decision"]
                    assert decision["code"] == "SESSION_BOUND" and decision["write_guard"] == "WRITE_ALLOWED"
                    assert decision["lease"]["lease_id"] == lease.lease_id
                    assert decision["lease"]["holder"] == asdict(lease.holder)
                    assert decision["runtime_binding"] == binding
                    assert store.load().records == snapshot.records
                    assert ledger.read()["requests"] == current
            elif scenario == "foreign-identity":
                foreign = executor(target)
                for option, value, code in [
                    ("--holder-pid", str(foreign.pid), "WRITER_LEASE_HELD"),
                    ("--session-id", "different-session", "EXTERNAL_BINDING"),
                    ("--run-id", "different-run", "EXTERNAL_BINDING"),
                ]:
                    arguments = _bind_arguments(governed, allocation, holder.pid)
                    if option in arguments:
                        arguments[arguments.index(option) + 1] = value
                    else:
                        arguments += [option, value]
                    result = _cli_process(arguments)
                    assert result.returncode == 2 and not result.stderr
                    assert [error["code"] for error in json.loads(result.stdout)["errors"]] == [code]
                    assert store.load() == snapshot and ledger.read()["requests"] == current
                foreign.kill()
                foreign.wait(timeout=5)
            else:
                assert scenario == "exit"
            live = store.load()
            assert reconcile_queue(apply=True)["result"] == {"code": "EXECUTION_OCCUPIED", "process_alive": True}
            assert store.load() == live and ledger.read()["requests"] == current
            holder.kill()
            holder.wait(timeout=5)
            assert os.getpgid(child_pid) == holder.pid
            assert reconcile_queue(apply=True)["blocker"] == "RUNTIME_OCCUPIED"
            assert store.load() == live and ledger.read()["requests"] == current
        finally:
            with suppress(ProcessLookupError):
                os.killpg(holder.pid, signal.SIGKILL)
            holder.wait(timeout=5)
            if child_pid is not None:
                with suppress(ChildProcessError):
                    os.waitpid(child_pid, 0)

    deadline = time.monotonic() + 5
    child_stat = Path(f"/proc/{child_pid}/stat")
    while child_stat.exists():
        try:
            if child_stat.read_text().rsplit(")", 1)[1].split()[0] in {"Z", "X"}:
                break
        except (FileNotFoundError, ProcessLookupError):
            break
        assert time.monotonic() < deadline, "holder child did not exit"
        time.sleep(0.01)
    assert reconcile_queue(apply=False)["result"]["code"] == "EXECUTION_RECONCILIATION_PENDING"
    assert store.load() == live and ledger.read()["requests"] == current
    assert reconcile_queue(apply=True)["result"] == {"code": "EXECUTION_RESOURCES_RELEASED", "task_completed": False}
    after, final_requests = store.load(), ledger.read()["requests"]
    terminal = final_requests[request_id]
    assert terminal["state"] == "RELEASED" and terminal["history"][:-1] == allocated["history"]
    assert [event["state"] for event in terminal["history"]].count("RELEASED") == 1
    record = after.records[original.allocation_id]
    assert not record.external_bindings and original.allocation_id not in after.writer_leases
    for field in ("allocation_id", "task_key", "authority_key", "owner", "scope_paths", "base_commit", "task_commit"):
        assert getattr(record, field) == getattr(original, field), field
    assert record.scope_paths == ("owned.txt",)
    assert git(target, "rev-parse", "HEAD") == allocation["head"]
    assert reconcile_queue(apply=True) is None
    assert store.load() == after and ledger.read()["requests"] == final_requests
    assert set(final_requests) == set(requests)
    assert final_requests[other_request] == requests[other_request]
    assert after.records[other["allocation_id"]] == before.records[other["allocation_id"]]
    assert after.writer_leases[other["allocation_id"]] == before.writer_leases[other["allocation_id"]]
    assert after.publication_lease == before.publication_lease
    assert after.integration_intents == before.integration_intents
    assert {item.path for item in inventory(governed.repo).worktrees} == paths


@pytest.mark.parametrize("fault", ["sigkill", "write-error"])
def test_removed_binding_with_unreleased_budget_recovers_once(
    governed, bound_session, capsys, monkeypatch, fault,
):
    monkeypatch.setattr(resource_admission, "execution_memory", lambda _row: 0)
    other, other_request, _other_holder = bound_session
    task, authority = "removed-binding-budget", "removed-binding-scope"
    target, acquired = acquire_one(
        governed, task=task, authority=authority, owner=OWNER, scope_paths=("owned.txt",),
    )
    allocation = {**acquired["decision"], "task_key": task, "authority_key": authority}
    request_id = _execution_admit(governed, target, f"session-{task}")["request_ids"][0]
    store, ledger = _store(governed), resource_admission.AdmissionLedger()
    paths = {item.path for item in inventory(governed.repo).worktrees}
    with subprocess.Popen(["sleep", "60"], cwd=target, start_new_session=True) as holder:
        try:
            bound = _bind_decision(governed, allocation, holder.pid, capsys)
            before, requests = store.load(), ledger.read()["requests"]
            original = before.records[allocation["allocation_id"]]
            lease = before.writer_leases[original.allocation_id]
            allocated = requests[request_id]
            arguments = [
                "binding-remove", "--repo", str(governed.repo), "--json",
                "--path", str(target), "--owner", OWNER,
                "--expected-record-version", str(original.record_version),
                "--binding", bound["runtime_binding"],
            ]
            refused = _cli_process(arguments)
            assert refused.returncode == 2 and not refused.stderr
            assert [error["code"] for error in json.loads(refused.stdout)["errors"]] == ["RUNTIME_OCCUPIED"]
            assert store.load() == before and ledger.read()["requests"] == requests
            holder.kill()
            holder.wait(timeout=5)
            with subprocess.Popen(
                [sys.executable, "-B", "-c", REMOVE_BUDGET_FAULT_ENTRYPOINT, fault, *arguments],
                env={
                    "PATH": os.defpath, "LANG": "C.UTF-8", "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1", "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"],
                    "PYTHONPATH": str(Path(session_admission.__file__).parent.parent),
                },
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ) as remover:
                try:
                    assert select.select([remover.stdout], [], [], 10)[0], "removal boundary not reached"
                    observed_lease = _raw_line(remover.stdout).strip()
                    if fault == "sigkill":
                        remover.kill()
                    output, error = remover.communicate(timeout=10)
                finally:
                    if remover.poll() is None:
                        remover.kill()
                        remover.communicate(timeout=5)
                assert observed_lease == lease.lease_id and not error, (observed_lease, output, error)
                if fault == "sigkill":
                    assert remover.returncode == -signal.SIGKILL and not output
                else:
                    assert remover.returncode == 2
                    assert [item["code"] for item in json.loads(output)["errors"]] == ["FILESYSTEM_ERROR"]
        finally:
            with suppress(ProcessLookupError):
                os.killpg(holder.pid, signal.SIGKILL)
            holder.wait(timeout=5)

    interrupted = store.load()
    removed = interrupted.records[original.allocation_id]
    assert not removed.external_bindings and removed.record_version == original.record_version + 1
    assert removed.history[:-1] == original.history
    assert removed.history[-1]["event"] == "EXTERNAL_BINDING_REMOVED"
    assert interrupted.writer_leases == before.writer_leases
    assert ledger.read()["requests"] == requests and allocated["state"] == "ALLOCATED"

    def reconcile_queue(*, apply):
        result = _cli_process(["admission-queue", "--repo", str(governed.repo), "--json",
                               *(["--apply"] if apply else [])])
        assert result.returncode == 0 and not result.stderr
        payload = json.loads(result.stdout)
        assert not payload["errors"]
        return next((row for row in payload["decision"]["outcomes"]
                     if row["request_id"] == request_id), None)

    assert reconcile_queue(apply=False)["result"]["code"] == "EXECUTION_RECONCILIATION_PENDING"
    assert store.load() == interrupted and ledger.read()["requests"] == requests
    reconciled = reconcile_queue(apply=True)
    assert reconciled.get("result") == {"code": "EXECUTION_RESOURCES_RELEASED", "task_completed": False}, reconciled
    after, final_requests = store.load(), ledger.read()["requests"]
    terminal = final_requests[request_id]
    assert terminal["state"] == "RELEASED" and terminal["history"][:-1] == allocated["history"]
    assert [event["state"] for event in terminal["history"]].count("RELEASED") == 1
    record = after.records[original.allocation_id]
    assert not record.external_bindings and original.allocation_id not in after.writer_leases
    # Recovery proves absence without adding a synthetic binding to repository history.
    assert record.record_version == removed.record_version + 1
    assert record.history[:-1] == removed.history
    assert record.history[-1]["event"] == "PARKED" and record.lifecycle == "RETAINED"
    for field in ("allocation_id", "task_key", "authority_key", "owner", "scope_paths", "base_commit", "task_commit"):
        assert getattr(record, field) == getattr(original, field), field
    assert git(target, "rev-parse", "HEAD") == allocation["head"]
    assert reconcile_queue(apply=True) is None
    assert store.load() == after and ledger.read()["requests"] == final_requests
    assert set(final_requests) == set(requests)
    assert final_requests[other_request] == requests[other_request]
    assert after.records[other["allocation_id"]] == before.records[other["allocation_id"]]
    assert after.writer_leases[other["allocation_id"]] == before.writer_leases[other["allocation_id"]]
    assert after.publication_lease == before.publication_lease
    assert after.integration_intents == before.integration_intents
    assert {item.path for item in inventory(governed.repo).worktrees} == paths


@pytest.mark.parametrize("consumer_kind", ["binding", "queue"])
def test_late_exit_recovery_preserves_the_next_run(
    governed, bound_session, capsys, monkeypatch, consumer_kind,
):
    monkeypatch.setattr(resource_admission, "execution_memory", lambda _row: 0)
    other, other_request, _other_holder = bound_session
    task, authority = "run-handover", "run-handover-scope"
    target, acquired = acquire_one(
        governed, task=task, authority=authority, owner=OWNER, scope_paths=("owned.txt",),
    )
    allocation = {**acquired["decision"], "task_key": task, "authority_key": authority}
    session, next_session = f"session-{task}", f"next-session-{task}"
    old_request = _execution_admit(governed, target, session, run_id="old-run")["request_ids"][0]
    store, ledger = _store(governed), resource_admission.AdmissionLedger()
    paths = {item.path for item in inventory(governed.repo).worktrees}
    processes, children = [], []

    def launch():
        holder = subprocess.Popen(
            [sys.executable, "-B", "-c",
             "import subprocess; child=subprocess.Popen(['sleep', '60']); "
             "print(child.pid, flush=True); child.wait()"],
            cwd=target, start_new_session=True, stdout=subprocess.PIPE, text=True,
        )
        processes.append(holder)
        assert select.select([holder.stdout], [], [], 10)[0], "holder child did not start"
        child = int(holder.stdout.readline())
        children.append(child)
        assert os.getpgid(child) == holder.pid
        return holder, child

    def stop_group(holder, child):
        with suppress(ProcessLookupError):
            os.killpg(holder.pid, signal.SIGKILL)
        holder.wait(timeout=5)
        with suppress(ChildProcessError):
            os.waitpid(child, 0)
        # Outside a PID namespace init reaps the orphan; only live children block.
        deadline = time.monotonic() + 5
        stat = Path(f"/proc/{child}/stat")
        while stat.exists():
            try:
                if stat.read_text().rsplit(")", 1)[1].split()[0] in {"Z", "X"}:
                    break
            except (FileNotFoundError, ProcessLookupError):
                break
            assert time.monotonic() < deadline, "holder child did not exit"
            time.sleep(0.01)

    def invoke(arguments):
        result = _cli_process(arguments)
        assert not result.stderr
        return result.returncode, json.loads(result.stdout)

    def queue(*, apply):
        code, payload = invoke(["admission-queue", "--repo", str(governed.repo), "--json",
                                *(["--apply"] if apply else [])])
        assert code == 0 and not payload["errors"]
        return {row["request_id"]: row for row in payload["decision"]["outcomes"]}

    next_args = ["execution-admit", "--repo", str(governed.repo), "--json",
                 "--path", str(target), "--owner", OWNER, "--client-id", OWNER,
                 "--session-id", next_session, "--run-id", "next-run"]
    consumer = None
    try:
        old_holder, old_child = launch()
        old_bound = _bind_decision(governed, allocation, old_holder.pid, capsys, run_id="old-run")
        before, requests = store.load(), ledger.read()["requests"]
        original, old_row = before.records[allocation["allocation_id"]], requests[old_request]
        removal_args = [
            "binding-remove", "--repo", str(governed.repo), "--json", "--path", str(target),
            "--owner", OWNER, "--expected-record-version", str(original.record_version),
            "--binding", old_bound["runtime_binding"],
        ]
        delayed_args = removal_args if consumer_kind == "binding" else [
            "admission-queue", "--repo", str(governed.repo), "--json", "--apply",
        ]
        consumer = subprocess.Popen(
            [sys.executable, "-B", "-c", DELAYED_EXIT_ENTRYPOINT,
             consumer_kind, original.allocation_id, *delayed_args],
            env={
                "PATH": os.defpath, "LANG": "C.UTF-8", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1", "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"],
                "PYTHONPATH": str(Path(session_admission.__file__).parent.parent),
            },
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert select.select([consumer.stdout], [], [], 10)[0], "old consumer did not pause"
        marker = json.loads(consumer.stdout.readline())
        if consumer_kind == "binding":
            assert marker == {"boundary": "old-binding-request", "record_version": original.record_version}
        else:
            assert marker["ledger_row"] == old_row and marker["lease_id"] == old_bound["lease"]["lease_id"]
        code, payload = invoke(next_args)
        waiting = payload["decision"]
        assert code == 0 and waiting["code"] == "RESOURCE_WAIT"
        assert "ALLOCATION_OCCUPIED" in waiting["blockers"]
        next_request = waiting["request_ids"][0]
        assert store.load() == before and ledger.read()["requests"][old_request] == old_row
        old_holder.kill()
        old_holder.wait(timeout=5)
        assert os.getpgid(old_child) == old_holder.pid
        code, payload = invoke(next_args)
        assert code == 0 and payload["decision"]["code"] == "RESOURCE_WAIT"
        assert payload["decision"]["request_ids"] == [next_request]
        assert store.load() == before and ledger.read()["requests"][old_request] == old_row
        stop_group(old_holder, old_child)
        assert queue(apply=True)[old_request]["result"] == {
            "code": "EXECUTION_RESOURCES_RELEASED", "task_completed": False,
        }
        old_terminal = ledger.read()["requests"][old_request]
        record = store.load().records[original.allocation_id]
        assert record.lifecycle == "RETAINED" and not record.external_bindings
        assert original.allocation_id not in store.load().writer_leases
        lifecycle.resume(governed.repo, target=target, owner=OWNER,
                         expected_record_version=record.record_version)
        code, payload = invoke(next_args)
        assert code == 0 and payload["decision"]["code"] == "RESOURCE_AVAILABLE"
        assert payload["decision"]["request_ids"] == [next_request]
        new_holder, new_child = launch()
        bind_args = _bind_arguments(governed, allocation, new_holder.pid, run_id="next-run")
        bind_args[bind_args.index("--session-id") + 1] = next_session
        code, payload = invoke(bind_args)
        fresh = payload["decision"]
        assert code == 0 and fresh["code"] == "SESSION_BOUND" and fresh["write_guard"] == "WRITE_ALLOWED"
        assert fresh["lease"]["lease_id"] != old_bound["lease"]["lease_id"]
        active, active_requests = store.load(), ledger.read()["requests"]
        new_budget = active_requests[next_request]
        assert new_budget["state"] == "ALLOCATED" and new_budget["lease_id"] == fresh["lease"]["lease_id"]
        assert new_budget["holder"] == fresh["lease"]["holder"]
        assert active_requests[old_request] == old_terminal
        output, error = consumer.communicate("continue\n", timeout=15)
        assert not error
        payload = json.loads(output)
        if consumer_kind == "binding":
            assert consumer.returncode == 2 and [e["code"] for e in payload["errors"]] == ["CAS_MISMATCH"]
        else:
            assert consumer.returncode == 0 and not payload["errors"]
            assert next(row for row in payload["decision"]["outcomes"]
                        if row["request_id"] == old_request)["blocker"] == "RUNTIME_OCCUPIED"
        assert store.load() == active and ledger.read()["requests"] == active_requests
        current_args = removal_args.copy()
        current_args[current_args.index("--expected-record-version") + 1] = str(
            active.records[original.allocation_id].record_version,
        )
        for arguments, expected in [(removal_args, "CAS_MISMATCH"), (current_args, "RUNTIME_OCCUPIED")]:
            code, payload = invoke(arguments)
            assert code == 2 and [e["code"] for e in payload["errors"]] == [expected]
            assert store.load() == active and ledger.read()["requests"] == active_requests
        new_holder.kill()
        new_holder.wait(timeout=5)
        assert os.getpgid(new_child) == new_holder.pid
        code, payload = invoke(current_args)
        assert code == 2 and [e["code"] for e in payload["errors"]] == ["RUNTIME_OCCUPIED"]
        assert store.load() == active and ledger.read()["requests"] == active_requests
        stop_group(new_holder, new_child)
        # An old lease replay after runtime exit still cannot settle the next run's budget.
        code, payload = invoke(current_args)
        assert code == 0 and payload["decision"]["changed"] is False
        assert store.load() == active and ledger.read()["requests"] == active_requests
        assert queue(apply=False)[next_request]["result"]["code"] == "EXECUTION_RECONCILIATION_PENDING"
        assert store.load() == active and ledger.read()["requests"] == active_requests
        assert queue(apply=True)[next_request]["result"] == {
            "code": "EXECUTION_RESOURCES_RELEASED", "task_completed": False,
        }
        after, final_requests = store.load(), ledger.read()["requests"]
        terminal = final_requests[next_request]
        assert terminal["state"] == "RELEASED" and terminal["history"][:-1] == new_budget["history"]
        assert [event["state"] for event in terminal["history"]].count("RELEASED") == 1
        assert final_requests[old_request] == old_terminal
        record = after.records[original.allocation_id]
        assert not record.external_bindings and original.allocation_id not in after.writer_leases
        for field in ("allocation_id", "task_key", "authority_key", "owner", "scope_paths", "base_commit", "task_commit"):
            assert getattr(record, field) == getattr(original, field), field
        assert git(target, "rev-parse", "HEAD") == allocation["head"]
        assert next_request not in queue(apply=True)
        assert store.load() == after and ledger.read()["requests"] == final_requests
        assert final_requests[other_request] == requests[other_request]
        assert after.records[other["allocation_id"]] == before.records[other["allocation_id"]]
        assert after.writer_leases[other["allocation_id"]] == before.writer_leases[other["allocation_id"]]
        assert after.publication_lease == before.publication_lease
        assert after.integration_intents == before.integration_intents
        assert {item.path for item in inventory(governed.repo).worktrees} == paths
    finally:
        if consumer is not None and consumer.poll() is None:
            consumer.kill()
            consumer.communicate(timeout=5)
        for index, process in enumerate(processes):
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            if index < len(children):
                with suppress(ChildProcessError):
                    os.waitpid(children[index], 0)


def test_a_drifted_allocation_is_refused_before_any_lease(governed, executor):
    allocation = _admit(governed, "agent-session-one")["decision"]["allocation"]
    process = executor(allocation["path"])
    with pytest.raises(ControllerError) as refused:
        session_admission.bind_session(
            governed.repo, target=__import__("pathlib").Path(allocation["path"]),
            task_key="agent-session-one", authority_key="agent-session-one", owner="someone-else",
            client_id=OWNER, holder_pid=process.pid, session_id="s",
        )
    assert refused.value.code == "SESSION_ALLOCATION_DRIFT"
    assert allocation["allocation_id"] not in _store(governed).load().writer_leases


def test_admission_convergence_skips_a_running_or_recent_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from tests.test_retention_convergence import POLICY_V8

    spawned = []

    class Child:
        pid = 4242

    monkeypatch.setattr(convergence, "_spawn_detached", lambda arguments: spawned.append(arguments) or Child())
    with GitRepository(linked=0, policy_text=POLICY_V8) as repository:
        adopt_canonical(repository)
        lock_path = convergence._pass_lock_path(inventory(repository.repo).common_dir)
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        def report(**values):
            convergence._write_pass_report(lock_path, {"trigger": "admission", **values})

        report(state="COMPLETED", started_at=datetime.now(UTC).isoformat(timespec="seconds"))
        assert convergence.admission_convergence(repository.repo)["code"] == "CONVERGENCE_RECENT"
        report(state="COMPLETED",
               started_at=(datetime.now(UTC) - timedelta(seconds=301)).isoformat(timespec="seconds"))
        assert convergence.admission_convergence(repository.repo)["code"] == "CONVERGENCE_SCHEDULED"
        import fcntl

        report(state="RUNNING", pid=1, started_at=datetime.now(UTC).isoformat(timespec="seconds"))
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            assert convergence.admission_convergence(repository.repo)["code"] == "CONVERGENCE_RUNNING"
        assert len(spawned) == 1


def test_pre_admission_reconciliation_leaves_running_holders_untouched(monkeypatch):
    from worktree_controller.writer_lease import describe_holder

    live = describe_holder(os.getpid(), client_id=OWNER, session_id="live", run_id=None)
    dead = {**live.__dict__, "pid": 2**22 + 7, "start_ticks": 1}
    rows = {
        "live": {"request": {"kind": "execution", "allocation_id": "a"}, "state": "ALLOCATED",
                 "holder": dict(live.__dict__)},
        "dead": {"request": {"kind": "execution", "allocation_id": "b"}, "state": "ALLOCATED",
                 "holder": dead},
        "unbound": {"request": {"kind": "execution", "allocation_id": "c"}, "state": "ALLOCATED",
                    "holder": None},
    }
    monkeypatch.setattr(resource_admission.AdmissionLedger, "read", lambda self: {"requests": rows})
    reconciled = []
    monkeypatch.setattr(resource_admission, "_reconcile_execution",
                        lambda row, apply: reconciled.append(row["request"]["allocation_id"]))
    resource_admission.reconcile_finished_executions()
    assert reconciled == ["b", "c"]


def test_observe_false_returns_only_the_decision(governed):
    _path, observed = acquire_one(governed, task="observed")
    head = git(governed.repo, "rev-parse", "HEAD")
    quiet = session_admission.acquire(
        governed.repo, task_key="quiet", authority_key="quiet", module="m", role="feature",
        owner="owner", branch="quiet", base_ref=head, observe=False)
    assert observed["worktrees"] and quiet["worktrees"] == []
    assert quiet["decision"]["code"] == "ACQUIRED" == observed["decision"]["code"]


@pytest.mark.parametrize("outcomes,reconciles,attempts", [
    (["RESOURCE_AVAILABLE"], 0, 1),
    (["RESOURCE_WAIT", "RESOURCE_AVAILABLE"], 1, 2),
    (["ADMISSION_REQUEST_DRIFT", "RESOURCE_AVAILABLE"], 1, 2),
    (["RESOURCE_WAIT", "RESOURCE_WAIT"], 1, 2),
])
def test_execution_admission_reconciles_only_when_it_can_change_the_answer(
    monkeypatch, outcomes, reconciles, attempts,
):
    calls = {"reconcile": 0, "attempts": []}
    pending = list(outcomes)

    def attempt(repository_path, *, cancel, **_kwargs):
        calls["attempts"].append(cancel)
        outcome = pending.pop(0)
        if outcome == "ADMISSION_REQUEST_DRIFT":
            raise ControllerError(outcome, "fixture earlier execution of this task")
        return {"decision": {"code": outcome}}

    monkeypatch.setattr(resource_admission, "_admit_execution_once", attempt)
    monkeypatch.setattr(resource_admission, "reconcile_finished_executions",
                        lambda: calls.__setitem__("reconcile", calls["reconcile"] + 1))
    result = resource_admission.admit_execution(
        __import__("pathlib").Path("/fixture"), target=__import__("pathlib").Path("/fixture/t"),
        owner=OWNER, client_id=OWNER, session_id="s")
    assert result["decision"]["code"] == outcomes[-1]
    assert calls["reconcile"] == reconciles and len(calls["attempts"]) == attempts


def test_other_refusals_and_cancellation_never_reconcile(monkeypatch):
    reconciled = []
    monkeypatch.setattr(resource_admission, "reconcile_finished_executions", lambda: reconciled.append(1))

    def refuse(repository_path, *, cancel, **_kwargs):
        if cancel:
            return {"decision": {"code": "RESOURCE_CANCELLED"}}
        raise ControllerError("EXECUTION_AUTHORITY_CHANGED", "fixture")

    monkeypatch.setattr(resource_admission, "_admit_execution_once", refuse)
    path = __import__("pathlib").Path("/fixture")
    with pytest.raises(ControllerError, match="fixture"):
        resource_admission.admit_execution(path, target=path, owner=OWNER, client_id=OWNER, session_id="s")
    assert resource_admission.admit_execution(path, target=path, owner=OWNER, client_id=OWNER,
                                              session_id="s", cancel=True)["decision"]["code"] == "RESOURCE_CANCELLED"
    assert reconciled == []


def test_available_admission_keeps_unrelated_finished_execution(governed, bound_session):
    allocation, request_id, process = bound_session
    target, _ = acquire_one(governed, task="new-session", authority="new-session", owner=OWNER)
    unfinished = Path(allocation["path"]) / "unfinished.txt"
    unfinished.write_text("unfinished work\n")
    process.terminate()
    process.wait(timeout=5)
    ledger = resource_admission.AdmissionLedger()
    previous = ledger.read()["requests"][request_id]
    snapshot = _store(governed).load()
    admitted = _execution_admit(governed, target, "new-session")
    assert admitted["code"] == "RESOURCE_AVAILABLE"
    assert ledger.read()["requests"][request_id] == previous
    assert _store(governed).load() == snapshot
    assert unfinished.read_text() == "unfinished work\n"


@pytest.mark.parametrize("finished", [True, False])
def test_memory_wait_recovers_only_finished_execution(governed, bound_session, host, finished):  # noqa: F811 - pytest fixture
    allocation, request_id, process = bound_session
    target, _ = acquire_one(governed, task="new-session", authority="new-session", owner=OWNER)
    if finished:
        process.terminate()
        process.wait(timeout=5)
    counters, ledger, _ = host
    previous = ledger.read()["requests"][request_id]
    snapshot = _store(governed).load()
    counters["memory"] = resource_admission.MIN_AVAILABLE_MEMORY + 2 * resource_admission.GIB
    admitted = _execution_admit(governed, target, "new-session")
    assert admitted["code"] == ("RESOURCE_AVAILABLE" if finished else "RESOURCE_WAIT")
    rows = ledger.read()["requests"]
    waiting = rows[admitted["request_ids"][0]]
    assert [event["state"] for event in waiting["history"]] == (
        ["WAITING", "RESERVED"] if finished else ["WAITING"]
    )
    assert sum(row["request"].get("kind") == "execution" for row in rows.values()) == 2
    after = _store(governed).load()
    record = after.records[allocation["allocation_id"]]
    assert record.scope_paths == snapshot.records[allocation["allocation_id"]].scope_paths
    assert record.authority_key == allocation["authority_key"]
    assert after.publication_lease == snapshot.publication_lease
    assert after.integration_intents == snapshot.integration_intents
    assert Path(allocation["path"]).exists()
    if finished:
        assert rows[request_id]["state"] == "RELEASED"
        assert record.lifecycle == "RETAINED" and not record.external_bindings
        assert allocation["allocation_id"] not in after.writer_leases
        assert admitted["outstanding_memory_bytes"] == 2 * resource_admission.GIB
    else:
        assert rows[request_id] == previous
        assert after == snapshot
        assert "HOST_MEMORY_HEADROOM" in admitted["blockers"]


@pytest.mark.parametrize("changed", [{"run_id": "different-run"}, {"memory_bytes": 3 * resource_admission.GIB}])
def test_live_execution_identity_drift_remains_refused(governed, bound_session, changed):
    allocation, _request_id, _process = bound_session
    ledger = resource_admission.AdmissionLedger()
    previous = ledger.read()["requests"]
    snapshot = _store(governed).load()
    with pytest.raises(ControllerError) as error:
        _execution_admit(governed, Path(allocation["path"]), "session-agent-session-old", **changed)
    assert error.value.code == "ADMISSION_REQUEST_DRIFT"
    assert ledger.read()["requests"] == previous
    assert _store(governed).load() == snapshot


def test_bound_execution_cannot_be_launched_again_or_cancelled(governed, bound_session):
    allocation, _request_id, _process = bound_session
    ledger = resource_admission.AdmissionLedger()
    previous = ledger.read()["requests"]
    snapshot = _store(governed).load()
    for cancel in (False, True):
        with pytest.raises(ControllerError) as error:
            _execution_admit(
                governed, Path(allocation["path"]), "session-agent-session-old", cancel=cancel,
            )
        assert error.value.code == "ADMISSION_RECOVERY_REQUIRED"
    assert ledger.read()["requests"] == previous
    assert _store(governed).load() == snapshot


@pytest.mark.parametrize("waiting", [True, False])
def test_unbound_execution_retry_is_idempotent_and_cancel_stays_terminal(governed, host, waiting):  # noqa: F811 - pytest fixture
    target, _ = acquire_one(governed, task="unbound", authority="unbound", owner=OWNER)
    counters, ledger, _ = host
    if waiting:
        counters["memory"] = 0
    admitted = _execution_admit(governed, target, "unbound-session")
    request_id = admitted["request_ids"][0]
    previous = ledger.read()["requests"][request_id]
    repeated = _execution_admit(governed, target, "unbound-session")
    assert admitted["code"] == repeated["code"] == ("RESOURCE_WAIT" if waiting else "RESOURCE_AVAILABLE")
    assert repeated["request_ids"] == [request_id]
    current = ledger.read()["requests"][request_id]
    for field in ("request", "state", "history", "created_at_ns"):
        assert current[field] == previous[field]
    assert sum(row["request"].get("kind") == "execution" for row in ledger.read()["requests"].values()) == 1
    assert repeated["outstanding_memory_bytes"] == 2 * resource_admission.GIB
    with pytest.raises(ControllerError) as drift:
        _execution_admit(governed, target, "unbound-session", run_id="different-run")
    assert drift.value.code == "ADMISSION_REQUEST_DRIFT"
    snapshot = _store(governed).load()
    counters["memory"] = 32 * resource_admission.GIB
    cancelled = _execution_admit(governed, target, "unbound-session", cancel=True)
    assert cancelled["code"] == "EXECUTION_ADMISSION_CANCELLED"
    assert cancelled["request_ids"] == [request_id]
    with pytest.raises(ControllerError) as error:
        _execution_admit(governed, target, "unbound-session")
    assert error.value.code == "ADMISSION_RECOVERY_REQUIRED"
    assert ledger.read()["requests"][request_id]["state"] == "CANCELLED"
    assert _store(governed).load() == snapshot


def test_finished_run_recovery_requires_resume_of_parked_allocation(governed, bound_session):
    allocation, request_id, process = bound_session
    process.terminate()
    process.wait(timeout=5)
    snapshot = _store(governed).load()
    with pytest.raises(ControllerError) as error:
        _execution_admit(
            governed, Path(allocation["path"]), "session-agent-session-old", run_id="next-run",
        )
    assert error.value.code == "EXECUTION_AUTHORITY_CHANGED"
    rows = resource_admission.AdmissionLedger().read()["requests"]
    assert rows[request_id]["state"] == "RELEASED"
    assert sum(row["request"].get("kind") == "execution" for row in rows.values()) == 1
    after = _store(governed).load()
    record = after.records[allocation["allocation_id"]]
    assert record.lifecycle == "RETAINED" and not record.external_bindings
    assert record.authority_key == snapshot.records[allocation["allocation_id"]].authority_key
    assert record.scope_paths == snapshot.records[allocation["allocation_id"]].scope_paths
    assert Path(allocation["path"]).exists()
