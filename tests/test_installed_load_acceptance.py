"""Opt-in load and interrupt acceptance against a commit-pinned installed runtime."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
from pathlib import Path

import pytest

from tests.test_resource_admission import POLICY
from tests.test_rollout_process_acceptance import load_script
from tests.test_shadow import git

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "tests/installed_load_worker.py"


@pytest.fixture(scope="module")
def installed():
    configured = os.environ.get("WORKTREE_CONTROLLER_ACCEPTANCE")
    if not configured:
        pytest.skip("set WORKTREE_CONTROLLER_ACCEPTANCE to a commit-pinned installed Controller")
    entry = Path(configured)
    assert entry.is_absolute() and entry.is_file() and os.access(entry, os.X_OK)
    oid = entry.parent.parent.parent.name
    assert len(oid) == 40 and all(value in "0123456789abcdef" for value in oid)
    assert (entry.parent / "python").is_file()
    activation = load_script(ROOT / "scripts/controller-activate", "load_acceptance_verification")
    activation.CONTROLLER_REPO = ROOT
    activation.verify_runtime(entry.parent.parent, oid)
    return entry


@pytest.fixture
def fixture_environment(tmp_path, monkeypatch):
    environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TZ") if key in os.environ}
    for name in ("home", "state", "tmp"):
        (tmp_path / name).mkdir()
    environment.update(
        HOME=str(tmp_path / "home"), XDG_STATE_HOME=str(tmp_path / "state"),
        TMPDIR=str(tmp_path / "tmp"), PYTHONDONTWRITEBYTECODE="1",
        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
    )
    # Fixture Git preparation is isolated too; native workers use real resource samples.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return environment


def cli(installed, environment, repo, verb, *arguments):
    result = subprocess.run(
        [str(installed), verb, "--repo", str(repo), "--json", *map(str, arguments)],
        cwd=repo.parent, env=environment, capture_output=True, text=True, timeout=30, check=False,
    )
    payload = json.loads(result.stdout)
    with (repo.parent / "controller-calls.jsonl").open("a") as stream:
        stream.write(json.dumps({"argv": result.args, "returncode": result.returncode,
                                 "payload": payload}) + "\n")
    assert result.returncode == 0 and not payload["errors"], (payload, result.stderr)
    return payload


def start(installed, environment, root, mode, *arguments):
    return subprocess.Popen(
        [str(installed.parent / "python"), "-I", "-B", "-u", str(WORKER), mode, *map(str, arguments)],
        cwd=root, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, start_new_session=True,
    )


def line(child):
    assert select.select([child.stdout], [], [], 10)[0], "fixture boundary timed out"
    data = b""
    while not data.endswith(b"\n"):
        byte = os.read(child.stdout.fileno(), 1)
        assert byte, "fixture ended before boundary"
        data += byte
    return data.decode().strip()


def stop(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
    for stream in (child.stdin, child.stdout, child.stderr):
        if stream:
            stream.close()


def requests(installed, environment, root):
    result = subprocess.run(
        [str(installed.parent / "python"), "-I", "-B", str(WORKER), "requests"],
        cwd=root, env=environment, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    return {key: row for key, row in json.loads(result.stdout)["requests"].items()
            if row["request"].get("kind") == "execution"}


def release_lease(installed, environment, repo, path, lease):
    holder = lease["holder"]
    return cli(installed, environment, repo, "writer-lease-release",
               "--path", path, "--lease-id", lease["lease_id"],
               "--holder-pid", holder["pid"], "--holder-boot-id", holder["boot_id"],
               "--holder-start-ticks", holder["start_ticks"], "--client-id", holder["client_id"],
               "--session-id", holder["session_id"], "--run-id", holder["run_id"])


def interrupt_case(installed, environment, root, phase):
    repo = root / "repo"
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
    path = root / "session"
    cli(installed, environment, repo, "acquire", "--path", path, "--task-key", "session",
        "--authority-key", "session", "--module", "fixture", "--role", "feature",
        "--owner", "fixture", "--branch", "feature/session", "--base-ref", "HEAD",
        "--scope", "must-not-run.txt")
    holder = start(installed, environment, root, "child-holder")
    waiting, lease = None, None
    try:
        assert line(holder) == "ready"
        lease = cli(installed, environment, repo, "writer-lease-acquire",
                    "--path", path, "--owner", "fixture", "--client-id", "foreign",
                    "--holder-pid", holder.pid, "--session-id", "foreign", "--run-id", phase)["decision"]
        waiting = start(installed, environment, root, "wait-" + phase, path)
        event = json.loads(line(waiting))
        if phase == "admitted":
            assert event["phase"] == "before-admission", event
            release_lease(installed, environment, repo, path, lease)
            lease = None
            waiting.stdin.write(b"x")
            waiting.stdin.flush()
            event = json.loads(line(waiting))
            assert event["decision"]["code"] == "RESOURCE_AVAILABLE", event
        else:
            assert event["phase"] == phase, event
        rows = requests(installed, environment, root)
        assert len(rows) == 1, rows
        request_id, before = next(iter(rows.items()))
        assert before["state"] == ("ALLOCATED" if phase == "admitted" else "WAITING"), before
        waiting.send_signal(signal.SIGINT)
        out, err = waiting.communicate(timeout=10)
        assert waiting.returncode == 130 and not out and b"Traceback" not in err, (waiting.returncode, out, err)
        assert not (path / "must-not-run.txt").exists()
        after = requests(installed, environment, root)[request_id]
        if phase == "admitted":
            assert after == before and b"retained for reconciliation" in err, (before, after, err)
            cli(installed, environment, repo, "admission-queue", "--apply")
            assert requests(installed, environment, root)[request_id]["state"] == "RELEASED"
        else:
            assert after["state"] == "CANCELLED" and after["history"][-1]["state"] == "CANCELLED", after
            status = cli(installed, environment, repo, "status")
            item = next(row for row in status["worktrees"] if row["path"] == str(path))
            assert item["lifecycle_observation"]["lease"]["lease_id"] == lease["lease_id"], item
        (root / "interrupt.json").write_text(json.dumps({
            "phase": phase, "event": event, "exit_status": waiting.returncode,
            "request_id": request_id, "before": before, "after": after, "stderr": err.decode(),
        }, indent=2))
    finally:
        if waiting is not None:
            stop(waiting)
        if lease is not None:
            release_lease(installed, environment, repo, path, lease)
        stop(holder)
    status = cli(installed, environment, repo, "status")
    item = next(row for row in status["worktrees"] if row["path"] == str(path))
    completed = cli(installed, environment, repo, "complete-no-change",
                    "--path", path, "--owner", "fixture", "--expected-record-version", item["record_version"])
    item = next(row for row in completed["worktrees"] if row["path"] == str(path))
    cli(installed, environment, repo, "release", "--path", path, "--owner-release", "fixture",
        "--expected-record-version", item["record_version"])
    assert not path.exists()
    status = cli(installed, environment, repo, "status")
    assert len(status["worktrees"]) == 1 and status["repository"]["writer_leases_recorded"] == 0


def scan_case(installed, environment, root, mode):
    result = subprocess.run(
        [str(installed.parent / "python"), "-I", "-B", str(WORKER), mode, str(root / "scan")],
        cwd=root, env=environment, capture_output=True, text=True, timeout=30, check=False,
    )
    (root / "scan.stdout").write_text(result.stdout)
    (root / "scan.stderr").write_text(result.stderr)
    assert result.returncode == 0, (result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert Path(payload["package"]).is_relative_to(installed.parent.parent)
    (root / "scan.json").write_text(json.dumps(payload, indent=2))
    return payload


def test_installed_wait_interrupt_during_report(installed, fixture_environment, tmp_path):
    interrupt_case(installed, fixture_environment, tmp_path, "report")


def test_installed_wait_interrupt_during_repoll(installed, fixture_environment, tmp_path):
    interrupt_case(installed, fixture_environment, tmp_path, "repoll")


def test_installed_wait_interrupt_preserves_bound_provider(installed, fixture_environment, tmp_path):
    interrupt_case(installed, fixture_environment, tmp_path, "admitted")


def test_installed_growing_thread_pool_retains_reference(installed, fixture_environment, tmp_path):
    scan_case(installed, fixture_environment, tmp_path, "growing")


def test_installed_contracting_thread_pool_retains_reference(installed, fixture_environment, tmp_path):
    scan_case(installed, fixture_environment, tmp_path, "contracting")


def test_installed_exiting_process_resolves_after_rescan(installed, fixture_environment, tmp_path):
    scan_case(installed, fixture_environment, tmp_path, "transient")


def test_installed_live_thread_keeps_exit_uncertain(installed, fixture_environment, tmp_path):
    scan_case(installed, fixture_environment, tmp_path, "persistent")


def test_installed_live_permission_denial_stays_uncertain(installed, fixture_environment, tmp_path):
    scan_case(installed, fixture_environment, tmp_path, "denied")
