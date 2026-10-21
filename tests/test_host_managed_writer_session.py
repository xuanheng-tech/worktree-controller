"""All hosts check the same explicit context without acquiring a child's lease."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace

import pytest

from tests.test_retention_admission import _store
from tests.test_write_admission import ROOT
from tests.test_write_admission import task as task
from worktree_controller import environment, writer_lease, writer_session
from worktree_controller.cli import main as controller_main


@pytest.fixture
def hosted(task, monkeypatch, request):
    repo, path, allocation = task
    holder = writer_lease.resolve_holder(
        str(os.getpid()), client_id="host-application", session_id="host-session", run_id=getattr(request, "param", "host-run")
    )
    lease = writer_lease.acquire_writer_lease(
        repo.repo,
        target=path,
        client_id=holder.client_id,
        holder_pid=holder.pid,
        session_id=holder.session_id,
        run_id=holder.run_id,
        holder=holder,
    )["decision"]
    context = {
        "holder": {"pid": holder.pid, "boot_id": holder.boot_id, "start_ticks": holder.start_ticks},
        "client_id": holder.client_id,
        "session_id": holder.session_id,
        "run_id": holder.run_id,
        "repository_path": str(repo.repo),
        "worktree_path": str(path),
        "allocation_id": allocation,
        "lease_id": lease["lease_id"],
    }
    monkeypatch.setenv("CONTROLLER_WRITER_MODE", "host-managed-v2")
    monkeypatch.setenv("CONTROLLER_WRITER_CONTEXT", json.dumps(context))
    return repo, path, allocation, holder, context


def metadata(repo):
    root = _store(repo).root
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*.json")}


def test_host_check_is_read_only_and_binds_complete_identity(hosted):
    repo, path, _, _, _ = hosted
    before = metadata(repo)
    assert writer_session.host_check(path)["code"] == "WRITE_ALLOWED"
    assert metadata(repo) == before


def test_complete_v1_context_survives_v2_activation_without_lease_mutation(hosted, monkeypatch):
    repo, path, _, _, context = hosted
    legacy = {**context, "provider": context["client_id"]}
    del legacy["client_id"]
    monkeypatch.setenv("CONTROLLER_WRITER_MODE", "host-managed-v1")
    monkeypatch.setenv("CONTROLLER_WRITER_CONTEXT", json.dumps(legacy))
    before = metadata(repo)
    assert writer_session.host_check(path)["code"] == "WRITE_ALLOWED"
    assert metadata(repo) == before


def test_unrelated_process_cannot_borrow_a_complete_host_context(hosted, monkeypatch):
    repo, path, _, _, context = hosted
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        holder = writer_lease.resolve_holder(str(child.pid), client_id="host-application",
                                            session_id="host-session", run_id="host-run")
        # Even a declaration describing a live child cannot make its parent a
        # descendant. This refusal occurs before allocation/lease lookups.
        context["holder"] = {"pid": child.pid, "boot_id": holder.boot_id,
                             "start_ticks": holder.start_ticks}
        monkeypatch.setenv("CONTROLLER_WRITER_CONTEXT", json.dumps(context))
        before = metadata(repo)
        assert "descendant" in writer_session.host_check(path)["reason"]
        assert metadata(repo) == before
    finally:
        child.terminate()
        child.wait(timeout=10)


@pytest.mark.parametrize("change", ["start_ticks", "ppid", "vanished"])
def test_host_lineage_recheck_rejects_pid_reuse_and_reparenting(hosted, monkeypatch, change):
    repo, path, _, _, _ = hosted
    inspect = environment._current_process_identity
    calls = 0

    def changed(process):
        nonlocal calls
        value = inspect(process)
        calls += 1
        if calls == 2:
            if change == "vanished":
                return None
            return replace(value, **{change: getattr(value, change) + 1})
        return value

    monkeypatch.setattr(environment, "_current_process_identity", changed)
    before = metadata(repo)
    assert writer_session.host_check(path)["admission"] == "REFUSED"
    assert metadata(repo) == before


@pytest.mark.parametrize("action", ["acquire", "check", "release"])
def test_host_environment_never_mutates_a_direct_cli_lease(hosted, capsys, action):
    repo, path, _, _, _ = hosted
    before = metadata(repo)
    args = (
        [action, str(path), str(os.getpid()), "--client-id", "child-executor"]
        if action == "release"
        else [action, "child-executor", str(path), str(os.getpid())]
    )
    assert controller_main(["writer-session", *args, "--session-id", "child-session"]) == 0
    assert json.loads(capsys.readouterr().out)["decision"]["code"] == "WRITE_ALLOWED"
    assert metadata(repo) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("client_id", "another-client"),
        ("session_id", "another-session"),
        ("run_id", "another-run"),
        ("repository_path", "/different-repository"),
        ("worktree_path", "/different-worktree"),
        ("allocation_id", "different-allocation"),
        ("lease_id", "different-lease"),
        ("holder.pid", 1),
        ("holder.boot_id", "different-boot"),
        ("holder.start_ticks", 1),
        ("holder.pid", True),
    ],
)
def test_forged_context_is_refused_without_metadata_changes(hosted, monkeypatch, field, value):
    repo, path, _, _, original = hosted
    context = deepcopy(original)
    if field.startswith("holder."):
        context["holder"][field.removeprefix("holder.")] = value
    else:
        context[field] = value
    monkeypatch.setenv("CONTROLLER_WRITER_CONTEXT", json.dumps(context))
    before = metadata(repo)
    assert writer_session.host_check(path)["admission"] == "REFUSED"
    assert metadata(repo) == before


@pytest.mark.parametrize(
    "fault", ["mode-missing", "old-mode", "context-missing", "duplicate", "run-missing", "extra"]
)
def test_malformed_context_never_falls_back_to_direct_admission(hosted, monkeypatch, fault):
    repo, path, _, _, original = hosted
    context = deepcopy(original)
    if fault == "mode-missing":
        monkeypatch.delenv("CONTROLLER_WRITER_MODE")
    elif fault == "old-mode":
        monkeypatch.setenv("CONTROLLER_WRITER_MODE", "host-managed-v1")
    elif fault == "context-missing":
        monkeypatch.delenv("CONTROLLER_WRITER_CONTEXT")
    elif fault == "duplicate":
        monkeypatch.setenv("CONTROLLER_WRITER_CONTEXT", '{"run_id":null,"run_id":null}')
    else:
        if fault == "run-missing":
            del context["run_id"]
        else:
            context["unexpected"] = True
        monkeypatch.setenv("CONTROLLER_WRITER_CONTEXT", json.dumps(context))
    before = metadata(repo)
    assert controller_main(["writer-session", "acquire", "child", str(path)]) == 2
    assert metadata(repo) == before


@pytest.mark.parametrize("client", ["terminal", "executor-a", "executor-b"])
def test_generic_launcher_preserves_its_hosts_lease_until_parent_exit(hosted, tmp_path, client):
    repo, path, allocation, _, context = hosted
    before = metadata(repo)
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "from worktree_controller.writer_session import run_main; raise SystemExit(run_main())",
            "--path",
            str(path),
            "--client-id",
            client,
            "--session-id",
            "child-session",
            "--",
            sys.executable,
            "-B",
            "-c",
            "print('executed'); raise SystemExit(37)",
        ],
        env={
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "CONTROLLER_WRITER_MODE": "host-managed-v2",
            "CONTROLLER_WRITER_CONTEXT": json.dumps(context),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 37 and result.stdout == "executed\n", result.stderr
    assert metadata(repo) == before
    assert allocation in _store(repo).load().writer_leases


def test_revoked_context_does_not_inherit_a_reacquired_lease(hosted):
    from worktree_controller.resource_admission import AdmissionLedger

    repo, path, _, holder, _ = hosted
    writer_lease.release_writer_lease(repo.repo, target=path, holder=holder)
    writer_lease.acquire_writer_lease(
        repo.repo,
        target=path,
        client_id=holder.client_id,
        holder_pid=holder.pid,
        session_id=holder.session_id,
        run_id=holder.run_id,
    )
    assert writer_session.host_check(path)["admission"] == "REFUSED"
    rows = [row for row in AdmissionLedger().read()["requests"].values()
            if row["request"].get("kind") == "execution"]
    assert rows == []  # Writer ownership alone never starts or rebinds execution.


def test_host_context_never_grants_an_ungoverned_child_path(hosted, tmp_path):
    assert writer_session.host_check(tmp_path)["admission"] == "REFUSED"


@pytest.mark.parametrize("hosted", [None], indirect=True)
def test_explicit_null_run_identity_is_preserved(hosted):
    _repo, path, _, holder, context = hosted
    assert holder.run_id is None and context["run_id"] is None
    assert writer_session.host_check(path)["code"] == "WRITE_ALLOWED"
