"""Release a reaped holder's lease through a commit-pinned installed CLI."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from contextlib import contextmanager, suppress
from pathlib import Path

from tests.test_installed_load_acceptance import (
    cli,
    line,
    start,
    stop,
)
from tests.test_installed_load_acceptance import (
    fixture_environment as fixture_environment,
)
from tests.test_installed_load_acceptance import (
    installed as installed,
)
from tests.test_resource_admission import POLICY
from tests.test_shadow import git


def state(installed, environment, repo, path):
    payload = cli(installed, environment, repo, "status")
    row = next(item for item in payload["worktrees"] if item["path"] == str(path))
    return {
        "allocation": {key: row[key] for key in (
            "allocation_id", "task_key", "authority_key", "owner", "path", "branch", "head",
            "base_commit", "lifecycle", "record_version", "clean", "scope_paths",
            "external_bindings", "disposable_resources",
        )},
        "lease": row["lifecycle_observation"]["lease"],
        "writer_count": payload["repository"]["writer_leases_recorded"],
    }


def release(installed, environment, repo, path, lease, *, lease_id, holder=None, refused=False):
    identity = lease["holder"] if holder is None else holder
    arguments = [str(installed), "writer-lease-release", "--repo", str(repo), "--path", str(path),
                 "--json", "--holder-pid", str(identity["pid"]),
                 "--holder-boot-id", identity["boot_id"],
                 "--holder-start-ticks", str(identity["start_ticks"]),
                 "--client-id", identity["client_id"], "--session-id", identity["session_id"],
                 "--run-id", identity["run_id"]]
    if lease_id is not None:
        arguments += ["--lease-id", lease_id]
    result = subprocess.run(arguments, cwd=repo.parent, env=environment, capture_output=True,
                            text=True, timeout=30, check=False)
    payload = json.loads(result.stdout)
    with (repo.parent / "controller-calls.jsonl").open("a") as stream:
        stream.write(json.dumps({"argv": arguments, "returncode": result.returncode,
                                 "payload": payload}) + "\n")
    assert result.returncode == (2 if refused else 0), (payload, result.stderr)
    if refused:
        assert [error["code"] for error in payload["errors"]] == ["WRITER_LEASE_MISMATCH"], payload
    else:
        assert not payload["errors"] and payload["decision"]["code"] == "WRITER_LEASE_RELEASED", payload
        assert payload["decision"]["holder_exited"] is True, payload
    return payload


def reaped(holder):
    holder.terminate()
    assert holder.wait(timeout=10) == -signal.SIGTERM
    assert not Path(f"/proc/{holder.pid}").exists()


def wait_gone(pid):
    deadline = time.monotonic() + 10
    while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not Path(f"/proc/{pid}").exists(), "fixture group member was not reaped"


@contextmanager
def leased_case(installed, environment, root, *, group=False):
    repo, path = root / "repo", root / "session"
    git(root, "init", "-b", "main", str(repo))
    git(repo, "config", "user.name", "Acceptance Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    policy = repo / ".agents/worktree-policy.toml"
    policy.parent.mkdir()
    policy.write_text(POLICY)
    git(repo, "add", ".agents/worktree-policy.toml")
    git(repo, "commit", "-m", "fixture policy")
    cli(installed, environment, repo, "adopt", "--path", repo, "--task-key", "control",
        "--authority-key", "control", "--module", "repository", "--role", "control-plane",
        "--owner", "fixture", "--lifecycle", "ACTIVE")
    cli(installed, environment, repo, "acquire", "--path", path, "--task-key", "session",
        "--authority-key", "session", "--module", "fixture", "--role", "feature",
        "--owner", "fixture", "--branch", "feature/session", "--base-ref", "HEAD",
        "--scope", "must-not-run.txt")
    holder = start(installed, environment, root, "child-group" if group else "child-holder")
    lease, member = None, None
    try:
        ready = line(holder)
        if group:
            member = json.loads(ready)["member_pid"]
            assert os.getpgid(member) == holder.pid
        else:
            assert ready == "ready"
        lease = cli(installed, environment, repo, "writer-lease-acquire",
                    "--path", path, "--owner", "fixture", "--client-id", "runtime-host",
                    "--holder-pid", holder.pid, "--session-id", "reaped-session",
                    "--run-id", "reaped-run")["decision"]
        yield repo, path, holder, lease, member
    finally:
        if group:
            # Only this fixture's new session/group; its member intentionally outlives the leader.
            with suppress(ProcessLookupError):
                os.killpg(holder.pid, signal.SIGTERM)
        stop(holder)
        if member is not None:
            wait_gone(member)
        current = state(installed, environment, repo, path)
        if lease is not None and current["lease"]["present"]:
            release(installed, environment, repo, path, lease, lease_id=lease["lease_id"])
        current = state(installed, environment, repo, path)
        assert not current["lease"]["present"] and current["writer_count"] == 0
        assert not (path / "must-not-run.txt").exists()
        completed = cli(installed, environment, repo, "complete-no-change", "--path", path,
                        "--owner", "fixture", "--expected-record-version",
                        current["allocation"]["record_version"])
        row = next(item for item in completed["worktrees"] if item["path"] == str(path))
        cli(installed, environment, repo, "release", "--path", path, "--owner-release", "fixture",
            "--expected-record-version", row["record_version"])
        assert not path.exists()


def test_installed_exited_holder_releases_exact_lease(installed, fixture_environment, tmp_path):
    with leased_case(installed, fixture_environment, tmp_path) as (repo, path, holder, lease, _):
        reaped(holder)
        before = state(installed, fixture_environment, repo, path)
        assert before["lease"]["lease_id"] == lease["lease_id"] and before["writer_count"] == 1
        release(installed, fixture_environment, repo, path, lease, lease_id=lease["lease_id"])
        after = state(installed, fixture_environment, repo, path)
        assert after["allocation"] == before["allocation"]
        assert not after["lease"]["present"] and after["writer_count"] == 0


def test_installed_exited_holder_requires_lease_id(installed, fixture_environment, tmp_path):
    with leased_case(installed, fixture_environment, tmp_path) as (repo, path, holder, lease, _):
        reaped(holder)
        before = state(installed, fixture_environment, repo, path)
        for lease_id in (None, "00000000-0000-0000-0000-000000000000"):
            release(installed, fixture_environment, repo, path, lease, lease_id=lease_id, refused=True)
            assert state(installed, fixture_environment, repo, path) == before


def test_installed_exited_holder_rejects_changed_identity(installed, fixture_environment, tmp_path):
    with leased_case(installed, fixture_environment, tmp_path) as (repo, path, holder, lease, _):
        reaped(holder)
        before = state(installed, fixture_environment, repo, path)
        identity = lease["holder"]
        for field, value in (
            ("pid", identity["pid"] + 1), ("start_ticks", identity["start_ticks"] + 1),
            ("boot_id", "00000000-0000-0000-0000-000000000000"),
            ("client_id", "another-client"), ("session_id", "another-session"),
            ("run_id", "another-run"),
        ):
            release(installed, fixture_environment, repo, path, lease, lease_id=lease["lease_id"],
                    holder={**identity, field: value}, refused=True)
            assert state(installed, fixture_environment, repo, path) == before


def test_installed_exited_holder_keeps_surviving_group(installed, fixture_environment, tmp_path):
    with leased_case(installed, fixture_environment, tmp_path, group=True) as fixture:
        repo, path, holder, lease, member = fixture
        reaped(holder)
        assert os.getpgid(member) == holder.pid
        before = state(installed, fixture_environment, repo, path)
        release(installed, fixture_environment, repo, path, lease,
                lease_id=lease["lease_id"], refused=True)
        assert state(installed, fixture_environment, repo, path) == before


def test_installed_exited_holder_keeps_checkout_occupant(installed, fixture_environment, tmp_path):
    with leased_case(installed, fixture_environment, tmp_path) as (repo, path, holder, lease, _):
        reaped(holder)
        occupant = start(installed, fixture_environment, path, "child-holder")
        try:
            assert line(occupant) == "ready" and os.getpgid(occupant.pid) != holder.pid
            before = state(installed, fixture_environment, repo, path)
            release(installed, fixture_environment, repo, path, lease,
                    lease_id=lease["lease_id"], refused=True)
            assert state(installed, fixture_environment, repo, path) == before
        finally:
            stop(occupant)
        release(installed, fixture_environment, repo, path, lease, lease_id=lease["lease_id"])
