"""Regressions distilled from the independent 0.9.0 rejection, using this candidate."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_controlled import acquire_one, adopt_canonical
from tests.test_retention_admission import _record, _store
from tests.test_shadow import GitRepository, git
from tests.test_writer_lease import POLICY_V6
from worktree_controller import lifecycle, semantics, writer_lease, writer_session
from worktree_controller.cli import main as controller_main
from worktree_controller.controller import guard_write
from worktree_controller.models import ControllerError

ROOT = Path(__file__).resolve().parents[1]


def error_taxonomy():
    codes = {"FUTURE_UNKNOWN_ERROR"}
    for source in (ROOT / "src/worktree_controller").glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ControllerError"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                codes.add(node.args[0].value)
            if source.name == "semantics.py" and isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "code"
                        and isinstance(value, ast.Constant)
                    ):
                        codes.add(value.value)
    return sorted(codes)


@pytest.fixture
def task():
    with GitRepository(linked=0, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        path, payload = acquire_one(
            repository,
            task="mine",
            authority="mine",
            owner="review-owner",
            scope_paths=("mine.txt",),
        )
        yield repository, path, payload["decision"]["allocation_id"]


@pytest.mark.parametrize("code", error_taxonomy())
def test_every_controller_error_refuses_governed_admission(task, monkeypatch, code):
    repository, path, allocation = task

    def refuse(*args, **kwargs):
        raise ControllerError(code, "injected refusal")

    monkeypatch.setattr(semantics, "managed_record", refuse)
    with pytest.raises(ControllerError) as error:
        writer_lease.acquire_writer_lease(
            repository.repo, target=path, client_id="manual", holder_pid=os.getpid()
        )
    assert error.value.code == code
    decision = writer_session.admission(path, client_id="manual", selector=str(os.getpid()))
    assert decision["admission"] == "REFUSED"
    assert decision["code"] == code
    assert allocation not in _store(repository).load().writer_leases


def test_not_applicable_requires_positive_ungoverned_evidence(tmp_path):
    assert (
        writer_session.admission(tmp_path, client_id="manual", selector=str(os.getpid()))[
            "admission"
        ]
        == "NOT_APPLICABLE"
    )
    with GitRepository(linked=0, policy=False) as repository:
        assert (
            writer_session.admission(
                repository.repo, client_id="manual", selector=str(os.getpid())
            )["admission"]
            == "NOT_APPLICABLE"
        )
        # Existing governance metadata still governs if the policy disappears.
        adopt_canonical(repository)
        decision = writer_session.admission(
            repository.repo, client_id="manual", selector=str(os.getpid())
        )
        assert decision["code"] == "DEDICATED_ROLE_GATE"
        assert writer_session.governed_target(repository.repo) is not None


def test_missing_and_corrupt_governed_paths_are_refused(task):
    repository, path, _ = task
    for target in (path / "missing", repository.repo / "missing"):
        assert (
            writer_session.admission(target, client_id="manual", selector=str(os.getpid()))[
                "admission"
            ]
            == "REFUSED"
        )
    (path / ".git").write_text("gitdir: /definitely-missing-controller-test\n")
    assert (
        writer_session.admission(path, client_id="manual", selector=str(os.getpid()))["admission"]
        == "REFUSED"
    )


@pytest.mark.parametrize("broken", ["lock", "repository-id", "policy-symlink"])
def test_partial_governance_never_becomes_not_applicable(task, broken):
    repository, path, allocation = task
    store = _store(repository)
    if broken == "lock":
        store.lock_file.unlink()
    elif broken == "repository-id":
        store.repo_file.unlink()
        (repository.repo / ".agents/worktree-policy.toml").unlink()
    else:
        policy = repository.repo / ".agents/worktree-policy.toml"
        policy.unlink()
        policy.symlink_to(repository.root / "missing-policy")
    result = writer_session.admission(path, client_id="manual", selector=str(os.getpid()))
    assert result["admission"] == "REFUSED"
    assert not (store.writer_leases_dir / f"{allocation}.json").exists()


def test_unadopted_governed_worktree_is_refused():
    with GitRepository(linked=1, policy_text=POLICY_V6) as repository:
        adopt_canonical(repository)
        result = writer_session.admission(
            repository.linked[0], client_id="manual", selector=str(os.getpid())
        )
        assert (result["admission"], result["code"]) == ("REFUSED", "NEEDS_ADOPTION")


def test_nested_unmanaged_repository_does_not_bypass_outer_governance(task):
    _repository, path, _ = task
    nested = path / "nested"
    git(path, "init", "-b", "main", str(nested))
    git(
        nested,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "fixture",
    )
    assert (
        writer_session.admission(nested, client_id="manual", selector=str(os.getpid()))["admission"]
        == "REFUSED"
    )


def test_governed_subdirectory_acquires_its_actual_allocation(task):
    repository, path, allocation = task
    decision = writer_session.admission(
        path / ".agents", client_id="manual", selector=str(os.getpid())
    )
    assert (decision["admission"], decision["allocation_id"]) == ("GRANTED", allocation)
    assert _record(repository, allocation).owner == "review-owner"


@pytest.mark.parametrize(
    "gate,code",
    [("owner", "OWNER_MISMATCH"), ("cas", "CAS_MISMATCH"), ("binding", "EXTERNAL_BINDING")],
)
def test_acquisition_rechecks_real_owner_cas_and_binding_gates(task, gate, code):
    repository, path, allocation = task
    kwargs = {}
    if gate == "owner":
        kwargs["owner"] = "wrong-owner"
    elif gate == "cas":
        kwargs["expected_record_version"] = 0
    else:
        lifecycle.update_external_binding(
            repository.repo,
            target=path,
            expected_record_version=1,
            owner="review-owner",
            binding="test-runtime",
            action="add",
        )
    before = _record(repository, allocation)
    with pytest.raises(ControllerError) as error:
        writer_lease.acquire_writer_lease(
            repository.repo, target=path, client_id="manual", holder_pid=os.getpid(), **kwargs
        )
    assert error.value.code == code
    assert _record(repository, allocation) == before
    assert allocation not in _store(repository).load().writer_leases


def test_new_runtime_binding_invalidates_write_entitlement(task):
    repository, path, _ = task
    holder = writer_lease.describe_holder(os.getpid(), client_id="manual")
    writer_lease.acquire_writer_lease(
        repository.repo, target=path, client_id="manual", holder_pid=holder.pid
    )
    lifecycle.update_external_binding(
        repository.repo,
        target=path,
        expected_record_version=1,
        owner="review-owner",
        binding="test-runtime",
        action="add",
    )
    assert guard(repository, path, holder) == "BLOCK_EXTERNAL_BINDING"


def test_missing_repository_lock_invalidates_existing_entitlement(task):
    repository, path, _ = task
    holder = writer_lease.describe_holder(os.getpid(), client_id="manual")
    writer_lease.acquire_writer_lease(
        repository.repo, target=path, client_id="manual", holder_pid=holder.pid
    )
    _store(repository).lock_file.unlink()
    assert guard(repository, path, holder) == "BLOCK_INVALID_METADATA"


def guard(repository, path, holder=None, operation="feature-write"):
    return guard_write(
        repository.repo,
        target=path,
        task_key="mine",
        authority_key="mine",
        owner="review-owner",
        operation=operation,
        holder=holder,
    )["decision"]["code"]


def test_competing_live_lease_blocks_client_id_and_write_guard(task):
    repository, path, _ = task
    child = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        writer_lease.acquire_writer_lease(
            repository.repo, target=path, client_id="manual", holder_pid=child.pid
        )
        caller = writer_lease.describe_holder(os.getpid(), client_id="manual")
        assert (
            writer_session.admission(path, client_id="manual", selector=str(os.getpid()))[
                "admission"
            ]
            == "REFUSED"
        )
        assert guard(repository, path, caller) == "BLOCK_WRITER_LEASE_MISMATCH"
        assert guard(repository, path) == "BLOCK_WRITER_LEASE_REQUIRED"
    finally:
        child.terminate()
        child.wait()


@pytest.mark.parametrize(
    "field,value",
    [
        ("boot_id", "another-boot"),
        ("start_ticks", 1),
        ("session_id", "other"),
        ("session_id", None),
        ("run_id", None),
        ("client_id", "executor-b"),
    ],
)
def test_acquire_check_release_share_full_identity(task, field, value):
    repository, path, allocation = task
    holder = writer_lease.resolve_holder(
        str(os.getpid()), client_id="manual", session_id="session-a", run_id="run-a"
    )
    kwargs = dict(
        target=path,
        client_id="manual",
        holder_pid=holder.pid,
        session_id="session-a",
        run_id="run-a",
    )
    first = writer_lease.acquire_writer_lease(repository.repo, holder=holder, **kwargs)
    impostor = replace(holder, **{field: value})
    with pytest.raises(ControllerError):
        writer_lease.acquire_writer_lease(repository.repo, holder=impostor, **kwargs)
    assert guard(repository, path, impostor) == "BLOCK_WRITER_LEASE_MISMATCH"
    with pytest.raises(ControllerError):
        writer_lease.release_writer_lease(repository.repo, target=path, holder=impostor)
    assert (
        _store(repository).load().writer_leases[allocation].lease_id
        == first["decision"]["lease_id"]
    )
    assert guard(repository, path, holder) == "WRITE_ALLOWED"
    assert (
        writer_lease.release_writer_lease(repository.repo, target=path, holder=holder)["decision"][
            "code"
        ]
        == "WRITER_LEASE_RELEASED"
    )
    assert guard(repository, path, holder) == "BLOCK_WRITER_LEASE_REQUIRED"


def test_pid_or_lease_id_alone_never_releases(task):
    repository, path, allocation = task
    decision = writer_lease.acquire_writer_lease(
        repository.repo, target=path, client_id="manual", holder_pid=os.getpid()
    )["decision"]
    for kwargs in ({}, {"holder_pid": os.getpid()}, {"lease_id": decision["lease_id"]}):
        with pytest.raises(ControllerError, match="complete holder"):
            writer_lease.release_writer_lease(repository.repo, target=path, **kwargs)
    assert allocation in _store(repository).load().writer_leases


def test_cli_full_holder_roundtrip_rejects_pid_only_release(task, capsys):
    repository, path, allocation = task
    common = ["--repo", str(repository.repo), "--path", str(path), "--json"]
    assert (
        controller_main(
            [
                "writer-lease-acquire",
                *common,
                "--client-id",
                "manual",
                "--holder-pid",
                str(os.getpid()),
            ]
        )
        == 0
    )
    decision = json.loads(capsys.readouterr().out)["decision"]
    assert decision["admission"] == "GRANTED"
    assert decision["holder_resolver_version"] == 1
    holder = decision["holder"]
    assert holder["boot_id"] == writer_lease.boot_id()
    assert holder["start_ticks"] == writer_lease.start_ticks(os.getpid())
    assert (
        controller_main(
            [
                "write-guard",
                *common,
                "--task-key",
                "mine",
                "--authority-key",
                "mine",
                "--owner",
                "review-owner",
                "--operation",
                "feature-write",
                "--holder",
                str(holder["pid"]),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["decision"]["code"] == "WRITE_ALLOWED"
    release = ["writer-lease-release", *common, "--holder-pid", str(holder["pid"])]
    assert controller_main(release) == 2
    capsys.readouterr()
    assert allocation in _store(repository).load().writer_leases
    full = [
        *release,
        "--holder-boot-id",
        holder["boot_id"],
        "--holder-start-ticks",
        str(holder["start_ticks"]),
    ]
    assert controller_main(full) == 0
    assert json.loads(capsys.readouterr().out)["decision"]["code"] == "WRITER_LEASE_RELEASED"
    assert allocation not in _store(repository).load().writer_leases


@pytest.mark.parametrize("mismatch", ["boot", "ticks"])
def test_original_identity_cannot_check_or_release_after_pid_reuse(task, monkeypatch, mismatch):
    repository, path, _ = task
    holder = writer_lease.describe_holder(os.getpid(), client_id="manual")
    writer_lease.acquire_writer_lease(
        repository.repo, target=path, client_id="manual", holder_pid=holder.pid
    )
    if mismatch == "boot":
        monkeypatch.setattr(writer_lease, "boot_id", lambda: "different-boot")
    else:
        monkeypatch.setattr(writer_lease, "start_ticks", lambda pid: holder.start_ticks + 1)
    assert guard(repository, path, holder) == "BLOCK_WRITER_LEASE_MISMATCH"
    with pytest.raises(ControllerError):
        writer_lease.release_writer_lease(repository.repo, target=path, holder=holder)


def test_expired_lease_and_released_entitlement_cannot_finalize(task, monkeypatch):
    repository, path, _ = task
    holder = writer_lease.describe_holder(os.getpid(), client_id="manual")
    writer_lease.acquire_writer_lease(
        repository.repo, target=path, client_id="manual", holder_pid=holder.pid, ttl_seconds=1
    )
    monkeypatch.setattr(writer_lease, "now", lambda: "2099-01-01T00:00:00+00:00")
    assert guard(repository, path, holder, "finalization") == "BLOCK_WRITER_LEASE_MISMATCH"
    with pytest.raises(ControllerError, match="expired lease"):
        writer_lease.acquire_writer_lease(
            repository.repo, target=path, client_id="manual", holder_pid=holder.pid
        )


def test_recycled_pid_does_not_borrow_new_process_group():
    holder = writer_lease.describe_holder(os.getpid(), client_id="manual")
    assert not writer_lease.process_group_is_live(
        replace(holder, start_ticks=holder.start_ticks + 1)
    )


def test_actual_ancestor_resolution_ignores_shell_arguments(tmp_path):
    result_file = tmp_path / "result.json"
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json,os\nfrom pathlib import Path\nfrom worktree_controller.writer_lease import resolve_holder\n"
        f"Path({str(result_file)!r}).write_text(json.dumps(dict(resolved=resolve_holder('ancestor:executor-b', client_id='executor-b').pid, parent=os.getppid())))\n"
    )
    shell = f"{sys.executable} -B {probe}; : # /opt/consumers/executor-b/hooks/executor-b"
    outer = f"import subprocess; subprocess.run(['bash','-c',{shell!r}],check=True)"
    environment = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    child = subprocess.Popen(
        ["executor-b", "-B", "-c", outer], executable=sys.executable, env=environment
    )
    assert child.wait(timeout=20) == 0
    result = json.loads(result_file.read_text())
    assert result["resolved"] == child.pid
    assert result["resolved"] != result["parent"]
