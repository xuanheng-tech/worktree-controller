"""Installed publication sessions reject premature/unobserved completion and recover EOF."""

import json
import selectors
import subprocess
from contextlib import contextmanager

import pytest

from tests.test_installed_publication_lease_acceptance import case as case
from tests.test_installed_publication_lease_acceptance import (
    fixture_environment as fixture_environment,
)
from tests.test_installed_publication_lease_acceptance import installed as installed
from tests.test_installed_publication_lease_acceptance import leased_case as leased_case
from tests.test_installed_publication_refusal_acceptance import observations, resume, start

CONTRACT = "integration-publication-session/v1"


def close(process):
    if process.poll() is None:
        process.kill()
    return process.communicate(timeout=5)


def record(case, evidence):
    (case["root"] / "publication-session.json").write_text(json.dumps(evidence, indent=2) + "\n")


def read_frame(process, evidence):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(10), "installed publication session did not respond"
    line = process.stdout.readline()
    assert line, "installed publication session ended before its response"
    frame = json.loads(line)
    evidence["session"]["responses"].append(frame)
    assert frame["schema_version"] == 1 and frame["contract"] == CONTRACT
    return frame


def send(process, evidence, action, identity, **fields):
    frame = {"schema_version": 1, "contract": CONTRACT, "action": action,
             "identity": identity, **fields}
    evidence["session"]["requests"].append(frame)
    process.stdin.write(json.dumps(frame) + "\n")
    process.stdin.flush()


def claimed_result(evidence):
    # Deliberately unobserved consumer claim sent over the public protocol, never a real receipt.
    return {"record_id": "unobserved-publication-fixture",
            "executor": {"name": "acceptance-fixture", "version": "1"},
            "push_executed": True, "remote_oid": evidence["lease"]["candidate_oid"]}


@contextmanager
def session(case, evidence):
    authority = evidence["lease"]
    argv = [str(case["installed"]), "publication-execution-session", "--repo", str(case["candidate"]),
            "--candidate-oid", authority["candidate_oid"], "--lease-id", authority["lease_id"],
            "--run-id", authority["run_id"], "--json"]
    evidence["session"] = {"argv": argv, "requests": [], "responses": []}
    process = subprocess.Popen(argv, cwd=case["root"], env=case["environment"], text=True,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        verified = read_frame(process, evidence)
        assert verified["phase"] == "verified" and verified["action"] == "PUSH_CANDIDATE"
        assert verified["remote_oid"] == authority["expected_main_oid"]
        for field in ("lease_id", "run_id", "candidate_oid"):
            assert verified["identity"][field] == authority[field]
        assert verified["identity"]["validation_evidence_id"] == evidence["prepared_state"]["publication_lease"]["validation_evidence_id"]
        yield process, verified["identity"]
    finally:
        stdout, stderr = close(process)
        evidence["session"].update(returncode=process.returncode, remaining_stdout=stdout, stderr=stderr)
        record(case, evidence)


def unchanged_then_publish(case, evidence):
    evidence["after_session"] = observations(case)
    assert evidence["after_session"] == evidence["prepared_state"]
    resume(case, evidence)
    record(case, evidence)


def blocked(process, evidence, code):
    frame = read_frame(process, evidence)
    assert frame["phase"] == "blocked" and frame["code"] == code
    assert process.wait(timeout=10) == 1


def test_installed_session_refuses_complete_before_authorization(leased_case):
    case = leased_case
    evidence = start(case, "premature-completion")
    with session(case, evidence) as (process, identity):
        send(process, evidence, "complete", identity, result=claimed_result(evidence))
        blocked(process, evidence, "INVALID_PUBLICATION_REQUEST")
    unchanged_then_publish(case, evidence)


def test_installed_session_refuses_completion_without_remote_push(leased_case):
    case = leased_case
    evidence = start(case, "unobserved-completion")
    with session(case, evidence) as (process, identity):
        send(process, evidence, "authorize", identity)
        authorized = read_frame(process, evidence)
        assert authorized["phase"] == "authorized" and authorized["action"] == "PUSH_CANDIDATE"
        assert authorized["identity"] == identity
        send(process, evidence, "complete", identity, result=claimed_result(evidence))
        blocked(process, evidence, "PUBLICATION_NOT_OBSERVED")
    unchanged_then_publish(case, evidence)


def test_installed_session_disconnect_releases_lock_and_reuses_lease(leased_case):
    case = leased_case
    evidence = start(case, "disconnected-before-authorization")
    with session(case, evidence) as (process, _):
        argv = [str(case["installed"]), "status", "--repo", str(case["repo"]), "--json"]
        observer = subprocess.Popen(argv, cwd=case["root"], env=case["environment"], text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        evidence["observer"] = {"argv": argv}
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                observer.communicate(timeout=0.5)
            evidence["observer"]["pending_while_session_open"] = True
            process.stdin.close()
            process.stdin = None  # communicate must not flush the deliberately closed channel.
            blocked(process, evidence, "PUBLICATION_SESSION_CLOSED")
            stdout, stderr = observer.communicate(timeout=10)
            assert observer.returncode == 0, stderr
            observed = json.loads(stdout)
            assert observed["command"] == "status" and not observed["errors"]
            assert observed["repository"]["publication_lease"] == evidence["prepared_state"]["publication_lease"]
            evidence["observer"]["payload"] = observed
        finally:
            stdout, stderr = close(observer)
            evidence["observer"].update(returncode=observer.returncode, stdout=stdout, stderr=stderr)
    unchanged_then_publish(case, evidence)
