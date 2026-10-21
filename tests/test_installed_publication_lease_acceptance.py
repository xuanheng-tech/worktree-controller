"""Installed publication leases remain exclusive, expire, and recover a completed push."""

import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.test_installed_conflict_retry_acceptance import (
    OWNER,
    claim,
    controller,
    enqueue,
    frozen_identity,
    intent,
    item,
    prepare,
    source,
)
from tests.test_installed_conflict_retry_acceptance import case as case
from tests.test_installed_conflict_retry_acceptance import (
    fixture_environment as fixture_environment,
)
from tests.test_installed_conflict_retry_acceptance import installed as installed
from tests.test_installed_publication_reprepare_acceptance import (
    CONTENT,
    OTHER_CONTENT,
    OTHER_FILE,
    closed,
)
from tests.test_shadow import git


def snapshot(case):
    status = controller(case, "status")
    return {
        "worktrees": sorted((row for row in status["worktrees"] if not row["canonical"]),
                            key=lambda row: row["allocation_id"]),
        "intents": controller(case, "integration-queue")["decision"]["intents"],
        "publication_lease": status["repository"]["publication_lease"],
        "remote_main": git(case["repo"], "ls-remote", "origin", "refs/heads/main"),
    }


def pair(case):
    first = prepare(case, case["candidate"], CONTENT, "first-preparation")
    allocation, _ = source(case, "second-source", OTHER_CONTENT, relative_path=OTHER_FILE)
    intent_id = enqueue(case, allocation, "second-integration")
    path, claimed = claim(case, intent_id, "second-candidate")
    second = prepare(case, path, OTHER_CONTENT, "second-preparation", relative_path=OTHER_FILE)
    assert intent(case, case["intent_id"])["state"] == "READY_TO_PUBLISH"
    assert intent(case, intent_id)["state"] == "READY_TO_PUBLISH"
    assert first["prepared_base_origin_main_oid"] == second["prepared_base_origin_main_oid"]
    return {"first": first, "second": second, "second_path": path,
            "second_allocation": claimed["allocation_id"], "second_source": allocation,
            "second_intent": intent_id}


def lease(case, path, run, *, ttl=900):
    return controller(case, "lease-acquire", "--path", path, "--owner", OWNER,
                      "--expected-record-version", item(case, path)["record_version"],
                      "--run-id", run, "--ttl-seconds", ttl,
                      code="PUBLICATION_LEASE_ACQUIRED")["decision"]


def consumer(case, path, authority, label, *, exit_code=0, crash=False):
    argv = [str(case["finalizer"]), "--summary", "--publish-integration-candidate",
            authority["candidate_oid"], "--repo", str(path),
            "--lease-id", authority["lease_id"], "--run-id", authority["run_id"]]
    environment = dict(case["environment"])
    if crash:
        # Existing installed Finalizer hook; the push and Controller protocol run normally.
        environment["GIT_FINALIZER_TEST_CRASH_AFTER_PUSH"] = "1"
    result = subprocess.run(argv, cwd=case["root"], env=environment, capture_output=True,
                            text=True, check=False, timeout=60)
    (case["root"] / (label + "-finalizer.txt")).write_text(result.stdout)
    payload = json.loads(result.stdout) if result.stdout else None
    with (case["root"] / "consumer-calls.jsonl").open("a") as stream:
        stream.write(json.dumps({"argv": argv, "returncode": result.returncode,
                                 "stdout": result.stdout, "stderr": result.stderr,
                                 "payload": payload}) + "\n")
    assert result.returncode == exit_code, (payload, result.stderr)
    if exit_code == 0:
        assert payload["status"] == "success"
        for field in ("lease_id", "run_id", "candidate_oid"):
            assert payload[field] == authority[field], payload
        assert payload["remote_verify"] == {"status": "verified", "oid": authority["candidate_oid"]}
    elif exit_code == 1:
        assert payload["status"] == "blocked", payload
    return payload


def complete(case, path, authority, summary):
    allocation = item(case, path)["allocation_id"]
    result = controller(case, "lease-complete", "--path", path,
                        "--expected-record-version", item(case, path)["record_version"],
                        "--lease-id", authority["lease_id"],
                        "--published-commit", authority["candidate_oid"],
                        "--finalizer-record-id", summary["record_id"], "--run-id", authority["run_id"],
                        code="PUBLICATION_RELEASED")["decision"]
    assert not path.exists()
    return result, closed(case, allocation, authority["candidate_oid"])


def expired(authority):
    expires_at = datetime.fromisoformat(authority["expires_at"])
    started = time.monotonic()
    deadline = started + max(0, (expires_at - datetime.now(UTC)).total_seconds()) + 5
    while datetime.now(UTC) <= expires_at:
        assert time.monotonic() < deadline, "real publication TTL did not expire within its bound"
        time.sleep(0.05)
    return {"expires_at": authority["expires_at"], "observed_at": datetime.now(UTC).isoformat(),
            "elapsed_seconds": time.monotonic() - started}


def record(case, evidence):
    (case["root"] / "publication-lease.json").write_text(json.dumps(evidence, indent=2) + "\n")


@pytest.fixture
def leased_case(case):
    try:
        yield case
    finally:
        # Settle only this fixture's exact lease before the base fixture closes its candidates.
        status = controller(case, "status")
        authority = status["repository"]["publication_lease"]
        if authority is not None:
            row = next(row for row in status["worktrees"]
                       if row["allocation_id"] == authority["allocation_id"])
            path = Path(row["path"])
            assert row["owner"] == OWNER and path.is_relative_to(case["root"])
            observed = git(case["repo"], "ls-remote", "origin", "refs/heads/main").split()[0]
            if observed == authority["candidate_oid"]:
                recovered = consumer(case, path, authority, "cleanup-recovery")
                complete(case, path, authority, recovered)
            else:
                assert observed == authority["expected_main_oid"]
                controller(case, "lease-abort", "--path", path, "--owner", OWNER,
                           "--expected-record-version", row["record_version"],
                           "--lease-id", authority["lease_id"], "--run-id", authority["run_id"],
                           code="PUBLICATION_LEASE_ABORTED")


def test_installed_live_publication_lease_refuses_second_candidate(leased_case):
    case = leased_case
    prepared = pair(case)
    authority = lease(case, case["candidate"], "live-publisher")
    before = snapshot(case)
    refusal = controller(case, "lease-acquire", "--path", prepared["second_path"],
                         "--owner", OWNER, "--expected-record-version",
                         item(case, prepared["second_path"])["record_version"],
                         "--run-id", "blocked-publisher", error="PUBLICATION_LEASE_HELD")
    assert snapshot(case) == before
    assert before["publication_lease"]["lease_id"] == authority["lease_id"]
    record(case, {"before": before, "refusal": refusal, "after": snapshot(case)})


def test_installed_expired_publisher_cannot_disturb_replacement_lease(leased_case):
    case = leased_case
    prepared = pair(case)
    original = frozen_identity(intent(case, case["intent_id"]))
    authority = lease(case, case["candidate"], "expired-publisher", ttl=2)
    expiry = expired(authority)
    before = snapshot(case)
    refused = consumer(case, case["candidate"], authority, "expired-before-push", exit_code=1)
    assert "PUBLICATION_LEASE_EXPIRED" in refused["reason"]
    assert snapshot(case) == before
    replacement = lease(case, prepared["second_path"], "replacement-publisher")
    assert replacement["lease_id"] != authority["lease_id"]
    assert intent(case, case["intent_id"])["state"] == "READY_TO_PUBLISH"
    assert frozen_identity(intent(case, case["intent_id"])) == original
    current = snapshot(case)
    fenced = consumer(case, case["candidate"], authority, "replaced-publisher", exit_code=1)
    assert "LEASE_MISMATCH" in fenced["reason"]
    assert snapshot(case) == current
    assert current["publication_lease"]["lease_id"] == replacement["lease_id"]
    published = consumer(case, prepared["second_path"], replacement, "current-publication")
    assert published["push"]["executed"] and published["push"]["result"] == "published"
    completion, proof = complete(case, prepared["second_path"], replacement, published)
    source_proof = closed(case, prepared["second_source"], replacement["candidate_oid"])
    record(case, {"expired_lease": authority, "expiry": expiry, "before_refusal": before,
                  "expired_refusal": refused, "after_refusal": before, "replacement_lease": replacement,
                  "before_fence": current, "fenced_refusal": fenced, "after_fence": current,
                  "publication": published, "completion": completion,
                  "candidate_release": proof, "source_release": source_proof})


def test_installed_expired_lease_recovers_completed_push_without_repush(leased_case):
    case = leased_case
    prepared = pair(case)
    original = frozen_identity(intent(case, case["intent_id"]))
    authority = lease(case, case["candidate"], "interrupted-publisher", ttl=15)
    crashed = consumer(case, case["candidate"], authority, "crash-after-push", exit_code=97, crash=True)
    assert crashed is None
    assert git(case["repo"], "ls-remote", "origin", "refs/heads/main").split()[0] == authority["candidate_oid"]
    expiry = expired(authority)
    before = snapshot(case)
    refusal = controller(case, "lease-acquire", "--path", prepared["second_path"], "--owner", OWNER,
                         "--expected-record-version", item(case, prepared["second_path"])["record_version"],
                         "--run-id", "cannot-replace-published-lease", error="PUBLICATION_RECOVERY_REQUIRED")
    assert snapshot(case) == before
    results = []
    for attempt in range(2):
        recovered = consumer(case, case["candidate"], authority, f"expired-recovery-{attempt}")
        assert recovered["push"]["executed"] is False
        assert recovered["push"]["result"] == "already_published_recovered"
        results.append(recovered)
    assert results[0]["record_id"] == results[1]["record_id"]
    completion, proof = complete(case, case["candidate"], authority, results[-1])
    assert frozen_identity(intent(case, case["intent_id"])) == original
    assert intent(case, case["intent_id"])["effective_state"] == "RELEASED"
    source_proof = closed(case, case["source_allocation"], authority["candidate_oid"])
    record(case, {"expired_lease": authority, "expiry": expiry, "before_replacement_refusal": before,
                  "replacement_refusal": refusal, "after_replacement_refusal": before,
                  "recoveries": results, "completion": completion,
                  "candidate_release": proof, "source_release": source_proof})
