from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import worktree_controller.lifecycle as lifecycle
from tests.test_controlled import _configure_remote, adopt_canonical
from tests.test_integration_pipeline import (
    POLICY_THREE_CANDIDATES,
    _claim,
    _enqueue,
    _source,
    _validate,
)
from tests.test_shadow import GitRepository, git
from worktree_controller.lifecycle import acquire_publication_lease, complete_publication_lease
from worktree_controller.models import ControllerError
from worktree_controller.publication_execution import CONTRACT, PublicationSession

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def leased():
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        _, allocation, _ = _source(
            repository,
            task="publication-source",
            relative_path="module.txt",
            content="module\n",
        )
        intent = _enqueue(repository, task="publication-candidate", source_allocation=allocation)
        target, _ = _claim(
            repository, task="publication-candidate", intent_id=intent["decision"]["intent_id"]
        )
        _validate(repository, target, "validated-publication")
        decision = acquire_publication_lease(
            repository.repo,
            target=target,
            expected_record_version=2,
            owner="integration-owner",
            run_id="publication-session-test",
        )["decision"]
        session = PublicationSession(
            target, decision["candidate_oid"], decision["lease_id"], decision["run_id"]
        )
        yield repository, target, decision, session


def _environment() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}


def _process(target: Path, decision: dict[str, Any]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "worktree_controller.cli",
            "publication-execution-session",
            "--repo",
            str(target),
            "--candidate-oid",
            decision["candidate_oid"],
            "--lease-id",
            decision["lease_id"],
            "--run-id",
            decision["run_id"],
            "--json",
        ],
        env=_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _read(process: subprocess.Popen[str]) -> dict[str, Any]:
    assert process.stdout is not None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(10), "publication protocol response timed out"
    line = process.stdout.readline()
    assert line, (
        process.stderr.read() if process.poll() is not None and process.stderr else "producer ended"
    )
    return json.loads(line)


def _send(
    process: subprocess.Popen[str], action: str, identity: dict[str, Any], **extra: Any
) -> None:
    assert process.stdin is not None
    process.stdin.write(
        json.dumps(
            {
                "schema_version": 1,
                "contract": CONTRACT,
                "action": action,
                "identity": identity,
                **extra,
            }
        )
        + "\n"
    )
    process.stdin.flush()


def _close(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def _result(candidate: str, *, pushed: bool = True) -> dict[str, Any]:
    return {
        "record_id": "consumer-execution-evidence",
        "executor": {"name": "shell", "version": "1"},
        "push_executed": pushed,
        "remote_oid": candidate,
    }


def test_public_session_verifies_authorizes_and_completion_still_requires_lifecycle_receipt(leased):
    repository, target, decision, session = leased
    process = _process(target, decision)
    try:
        verified = _read(process)
        assert verified["phase"] == "verified" and verified["action"] == "PUSH_CANDIDATE"
        identity = verified["identity"]
        assert "metadata" not in json.dumps(verified)
        _send(process, "authorize", identity)
        assert _read(process)["phase"] == "authorized"
        git(
            target,
            "push",
            "--no-follow-tags",
            "origin",
            f"{decision['candidate_oid']}:refs/heads/main",
        )
        _send(process, "complete", identity, result=_result(decision["candidate_oid"]))
        completed = _read(process)
        assert completed["phase"] == "completed"
        assert completed["lifecycle_completion_required"] is True
        assert completed["remote_verify"] == {
            "status": "verified",
            "oid": decision["candidate_oid"],
        }
        assert process.wait(timeout=5) == 0
    finally:
        _close(process)
    assert session.store.load().publication_lease is not None
    completion = complete_publication_lease(
        repository.repo,
        target=target,
        expected_record_version=decision["record_version"],
        lease_id=decision["lease_id"],
        published_commit=decision["candidate_oid"],
        finalizer_record_id=completed["record_id"],
        run_id=decision["run_id"],
        auto_release=False,
    )
    assert completion["decision"]["code"] == "PUBLICATION_COMPLETED"
    assert session.store.load().publication_lease is None


def test_completion_recovers_release_interrupted_after_metadata_written(leased, monkeypatch):
    repository, target, decision, session = leased
    git(target, "push", "origin", f"{decision['candidate_oid']}:refs/heads/main")

    def interrupt_release(name: str) -> None:
        if name == "release_metadata_written":
            raise RuntimeError("simulated release interruption")

    with monkeypatch.context() as patch:
        patch.setattr(lifecycle, "_crash_point", interrupt_release)
        with pytest.raises(RuntimeError, match="simulated release interruption"):
            complete_publication_lease(
                repository.repo,
                target=target,
                expected_record_version=decision["record_version"],
                lease_id=decision["lease_id"],
                published_commit=decision["candidate_oid"],
                finalizer_record_id="test-finalizer-record",
                run_id=decision["run_id"],
            )

    interrupted = session.store.load()
    released = interrupted.records[decision["allocation_id"]]
    assert released.lifecycle == "RELEASED"
    assert not target.exists()
    assert not interrupted.release_receipts
    assert list(session.store.transactions_dir.glob("*.json"))

    recovery = complete_publication_lease(
        repository.repo,
        target=target,
        expected_record_version=released.record_version,
        lease_id=decision["lease_id"],
        published_commit=decision["candidate_oid"],
        finalizer_record_id="test-finalizer-record",
        run_id=decision["run_id"],
    )
    assert recovery["decision"]["code"] == "PUBLICATION_RELEASED"
    completed = session.store.load()
    receipts = [
        receipt
        for receipt in completed.release_receipts.values()
        if receipt.allocation_id == decision["allocation_id"]
    ]
    assert len(receipts) == 1
    assert not list(session.store.transactions_dir.glob("*.json"))
    intent_id = completed.publication_receipts[decision["lease_id"]].intent_id
    assert intent_id is not None
    assert completed.integration_intents[intent_id].state == "RELEASED"

    replay = complete_publication_lease(
        repository.repo,
        target=target,
        expected_record_version=released.record_version,
        lease_id=decision["lease_id"],
        published_commit=decision["candidate_oid"],
        finalizer_record_id="test-finalizer-record",
        run_id=decision["run_id"],
    )
    assert replay["decision"]["code"] == "PUBLICATION_RELEASED"
    assert (
        len(
            [
                receipt
                for receipt in session.store.load().release_receipts.values()
                if receipt.allocation_id == decision["allocation_id"]
            ]
        )
        == 1
    )


def test_public_session_holds_producer_lock_until_consumer_disconnects(leased):
    _, target, decision, _ = leased
    process = _process(target, decision)
    observer = None
    try:
        assert _read(process)["phase"] == "verified"
        observer = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "worktree_controller.cli",
                "status",
                "--repo",
                str(target),
                "--json",
            ],
            env=_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        with pytest.raises(subprocess.TimeoutExpired):
            observer.communicate(timeout=0.1)
        assert process.stdin is not None
        process.stdin.close()
        assert _read(process)["code"] == "PUBLICATION_SESSION_CLOSED"
        assert process.wait(timeout=5) == 1
        stdout, stderr = observer.communicate(timeout=5)
        assert observer.returncode == 0, stderr
        assert json.loads(stdout)["command"] == "status"
    finally:
        _close(process)
        if observer is not None:
            _close(observer)


@pytest.mark.parametrize("action", ["complete", "authorize-with-wrong-identity"])
def test_out_of_order_or_wrong_identity_does_not_grant_a_push(leased, action):
    _, target, decision, _ = leased
    before = git(target, "ls-remote", "origin", "refs/heads/main")
    process = _process(target, decision)
    try:
        identity = _read(process)["identity"]
        if action == "authorize-with-wrong-identity":
            identity["run_id"] = "another-run"
            _send(process, "authorize", identity)
        else:
            _send(process, "complete", identity, result=_result(decision["candidate_oid"]))
        assert _read(process)["phase"] == "blocked"
        assert process.wait(timeout=5) == 1
        assert git(target, "ls-remote", "origin", "refs/heads/main") == before
    finally:
        _close(process)


def test_completion_reobserves_git_instead_of_trusting_consumer_result(leased):
    _, _, decision, session = leased
    with session.store.lock(exclusive=True):
        session.verify()
        session.authorize()
        with pytest.raises(ControllerError, match="remote does not equal"):
            session.complete(_result(decision["candidate_oid"]))


def test_expired_lease_blocks_push_but_recovers_an_already_published_candidate(leased):
    _, target, decision, session = leased
    with session.store.lock(exclusive=True):
        lease = session.store._load_unlocked().publication_lease
        assert lease is not None
        session.store.remove_publication_lease(lease.lease_id)
        session.store.write_publication_lease(
            replace(
                lease,
                acquired_at=(datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
                expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            )
        )
        with pytest.raises(ControllerError, match="expired"):
            session.verify()
        git(target, "push", "origin", f"{decision['candidate_oid']}:refs/heads/main")
        assert session.verify()["action"] == "RECOVER_ALREADY_PUBLISHED"
        assert session.authorize()["action"] == "RECOVER_ALREADY_PUBLISHED"
        assert (
            session.complete(_result(decision["candidate_oid"], pushed=False))["remote_verify"][
                "status"
            ]
            == "verified"
        )


def test_candidate_drift_and_distinct_push_endpoint_fail_closed(leased):
    _, target, _, session = leased
    with session.store.lock(exclusive=True):
        (target / "unrelated.txt").write_text("unrelated\n")
        with pytest.raises(ControllerError, match="leased candidate"):
            session.verify()
        (target / "unrelated.txt").unlink()
        git(target, "config", "remote.origin.pushurl", str(target.parent / "other.git"))
        with pytest.raises(ControllerError, match="identical fetch/push"):
            session.verify()


def test_duplicate_json_and_oversized_frame_release_session_without_push(leased):
    _, target, decision, _ = leased
    for frame in ('{"action":"authorize","action":"complete"}\n', "x" * 65537 + "\n"):
        process = _process(target, decision)
        try:
            assert _read(process)["phase"] == "verified"
            assert process.stdin is not None
            process.stdin.write(frame)
            process.stdin.flush()
            assert _read(process)["phase"] == "blocked"
            assert process.wait(timeout=5) == 1
        finally:
            _close(process)


def test_real_finalizer_crash_recovery_and_duplicate_execution(leased):
    repository, target, decision, session = leased
    executable = os.environ.get("GIT_FINALIZER_ACCEPTANCE")
    if not executable:
        pytest.skip("set GIT_FINALIZER_ACCEPTANCE to explicitly enable consumer acceptance")
    consumer = Path(executable).resolve(strict=True)
    wrapper = target.parent / "publication-consumer-bin"
    wrapper.mkdir()
    entry = wrapper / "worktree-controller"
    controller = os.environ.get("WORKTREE_CONTROLLER_ACCEPTANCE")
    if controller:
        entry.symlink_to(Path(controller).resolve(strict=True))
    else:
        entry.write_text(
            f"#!{sys.executable}\nfrom worktree_controller.cli import main\nraise SystemExit(main())\n"
        )
        entry.chmod(0o755)
    environment = {**_environment(), "PATH": str(wrapper) + os.pathsep + os.environ["PATH"]}
    command = [
        str(consumer),
        "--publish-integration-candidate",
        decision["candidate_oid"],
        "--repo",
        str(target),
        "--lease-id",
        decision["lease_id"],
        "--run-id",
        decision["run_id"],
        "--summary",
    ]
    crashed = subprocess.run(
        command,
        env={**environment, "GIT_FINALIZER_TEST_CRASH_AFTER_PUSH": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert crashed.returncode == 97, crashed.stdout + crashed.stderr
    for _ in range(2):
        recovered = subprocess.run(
            command, env=environment, capture_output=True, text=True, timeout=30
        )
        assert recovered.returncode == 0, recovered.stdout + recovered.stderr
        summary = json.loads(recovered.stdout)
        assert summary["push"]["result"] == "already_published_recovered"
        assert summary["push"]["executed"] is False
        assert summary["controller_execution"]["contract"] == CONTRACT
    completed = complete_publication_lease(
        repository.repo,
        target=target,
        expected_record_version=decision["record_version"],
        lease_id=decision["lease_id"],
        published_commit=decision["candidate_oid"],
        finalizer_record_id=summary["record_id"],
        run_id=decision["run_id"],
        auto_release=True,
    )
    assert completed["decision"]["code"] == "PUBLICATION_RELEASED"
    assert completed["decision"]["cleanup"] == {"result": "RELEASED", "blocker": None}
    assert not target.exists()
    assert session.store.load().publication_lease is None
    assert session.store.load().records[decision["allocation_id"]].lifecycle == "RELEASED"
