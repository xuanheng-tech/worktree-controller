"""Installed publication rechecks live main after leasing and recovers remote failures."""

import json
from pathlib import Path

import pytest

from tests.test_installed_conflict_retry_acceptance import (
    FILE,
    OWNER,
    controller,
    finalizer,
    frozen_identity,
    intent,
    item,
    prepare,
)
from tests.test_installed_publication_lease_acceptance import case as case
from tests.test_installed_publication_lease_acceptance import complete, consumer, lease
from tests.test_installed_publication_lease_acceptance import (
    fixture_environment as fixture_environment,
)
from tests.test_installed_publication_lease_acceptance import installed as installed
from tests.test_installed_publication_lease_acceptance import leased_case as leased_case
from tests.test_installed_publication_reprepare_acceptance import (
    CONTENT,
    OTHER_CONTENT,
    OTHER_FILE,
    VALIDATION_FIELDS,
    closed,
    mark_stale,
    state,
)
from tests.test_shadow import git


def observations(case, endpoint="origin"):
    status = controller(case, "status")
    return {"worktrees": sorted((row for row in status["worktrees"] if not row["canonical"]),
                                key=lambda row: row["allocation_id"]),
            "intents": controller(case, "integration-queue")["decision"]["intents"],
            "publication_lease": status["repository"]["publication_lease"],
            "local_refs": git(case["repo"], "show-ref"),
            "observed_fixture_endpoint": str(endpoint),
            "remote_refs": git(case["repo"], "ls-remote", str(endpoint))}


def abort_lease(case, authority):
    return controller(case, "lease-abort", "--path", case["candidate"], "--owner", OWNER,
                      "--expected-record-version", item(case, case["candidate"])["record_version"],
                      "--lease-id", authority["lease_id"], "--run-id", authority["run_id"],
                      code="PUBLICATION_LEASE_ABORTED")["decision"]


@pytest.fixture
def remote_case(leased_case):
    case = leased_case
    try:
        yield case
    finally:
        # The base lease fixture handles expected main and an already-published candidate.
        # Also settle this fixture's exact lease when its own external writer moved main.
        if case.get("external_main_oid"):
            status = controller(case, "status")
            authority = status["repository"]["publication_lease"]
            if authority is not None:
                row = next(row for row in status["worktrees"]
                           if row["allocation_id"] == authority["allocation_id"])
                assert row["owner"] == OWNER and row["path"] == str(case["candidate"])
                observed = git(case["repo"], "ls-remote", "origin", "refs/heads/main").split()[0]
                if observed == case["external_main_oid"]:
                    abort_lease(case, authority)


def publish_external_main(case, authority):
    # An independent disposable clone models another writer, outside this Controller's allocation.
    path = case["root"] / "external-writer"
    git(case["root"], "clone", git(case["repo"], "remote", "get-url", "origin"), str(path))
    git(path, "config", "user.name", "External Fixture Writer")
    git(path, "config", "user.email", "fixture@example.invalid")
    assert git(path, "rev-parse", "HEAD") == authority["expected_main_oid"]
    (path / OTHER_FILE).write_text(OTHER_CONTENT)
    committed, commit_receipt = finalizer(case, path, "external-commit", "--mode", "commit-only",
                                        "--message", "fixture external main advance", "--", OTHER_FILE)
    head = committed["commit"]["sha"]
    assert git(path, "show", "-s", "--format=%P", "HEAD").split() == [authority["expected_main_oid"]]
    case["external_main_oid"] = head
    published, receipt = finalizer(case, path, "external-publication", "--resume-publish", head)
    assert published["mode_result"]["post_verify"] == "passed"
    assert git(case["repo"], "ls-remote", "origin", "refs/heads/main").split() == [head, "refs/heads/main"]
    return {"head": head, "commit": committed, "commit_receipt": commit_receipt,
            "publication": published, "publication_receipt": receipt}


def finish(case, authority, evidence):
    published = consumer(case, case["candidate"], authority, "recovered-publication")
    assert published["push"]["executed"] and published["push"]["result"] == "published"
    completion, candidate_proof = complete(case, case["candidate"], authority, published)
    source_proof = closed(case, case["source_allocation"], authority["candidate_oid"])
    assert frozen_identity(intent(case, case["intent_id"])) == evidence["original_intent"]
    assert intent(case, case["intent_id"])["effective_state"] == "RELEASED"
    evidence.update(publication=published, completion=completion,
                    candidate_release=candidate_proof, source_release=source_proof)
    (case["root"] / "publication-remote.json").write_text(json.dumps(evidence, indent=2) + "\n")


def test_installed_publication_refuses_remote_advance_then_reprepares_same_intent(remote_case):
    case = remote_case
    prepared = prepare(case, case["candidate"], CONTENT, "original-preparation")
    authority = lease(case, case["candidate"], "old-main-publisher")
    original = frozen_identity(intent(case, case["intent_id"]))
    external = publish_external_main(case, authority)
    before = observations(case)
    refused = consumer(case, case["candidate"], authority, "advanced-main", exit_code=1)
    assert "PUBLICATION_REMOTE_DRIFT" in refused["reason"], refused
    after = observations(case)
    assert after == before
    evidence = {"old_preparation": prepared, "original_lease": authority, "original_intent": original,
                "external_writer": external, "new_main": external["head"], "before_refusal": before,
                "refusal": refused, "after_refusal": after, "lease_abort": abort_lease(case, authority)}
    stale = mark_stale(case, evidence)
    refreshed = controller(case, "integration-refresh", "--path", case["candidate"], "--owner", OWNER,
                           "--expected-record-version", stale["candidate"]["record_version"],
                           code="INTEGRATION_REFRESHED")
    current = state(case)
    assert frozen_identity(current["intent"]) == original and current["source"] == stale["source"]
    assert current["intent"]["candidate_revision"] == stale["intent"]["candidate_revision"] + 1
    for field in VALIDATION_FIELDS:
        assert current["intent"][field] is None, current
    assert git(case["candidate"], "show", "-s", "--format=%P", "HEAD").split() == [external["head"], case["source_head"]]
    assert (case["candidate"] / FILE).read_text() == CONTENT
    assert (case["candidate"] / OTHER_FILE).read_text() == OTHER_CONTENT
    fresh = prepare(case, case["candidate"], CONTENT, "fresh-preparation")
    assert fresh["prepared_receipt_id"] != prepared["prepared_receipt_id"]
    assert fresh["validation_evidence_id"] != prepared["validation_evidence_id"]
    replacement = lease(case, case["candidate"], "fresh-main-publisher")
    assert replacement["lease_id"] != authority["lease_id"]
    assert replacement["expected_main_oid"] == external["head"]
    assert replacement["candidate_oid"] != authority["candidate_oid"]
    evidence.update(refresh=refreshed, fresh_preparation=fresh, replacement_lease=replacement)
    finish(case, replacement, evidence)
    assert git(case["repo"], "show", replacement["candidate_oid"] + ":" + OTHER_FILE) == OTHER_CONTENT.strip()


def test_installed_publication_refuses_unavailable_remote_then_reuses_same_lease(remote_case):
    case = remote_case
    prepared = prepare(case, case["candidate"], CONTENT, "available-preparation")
    authority = lease(case, case["candidate"], "available-main-publisher")
    original = frozen_identity(intent(case, case["intent_id"]))
    available = observations(case)
    remote = Path(git(case["repo"], "remote", "get-url", "origin"))
    assert remote == case["root"] / "remote.git" and remote.is_dir()
    relocated = case["root"] / "unavailable-remote.git"
    assert not relocated.exists()
    remote.rename(relocated)
    try:
        # Observe actual refs at the owned relocated fixture; origin remains unavailable to Finalizer.
        before = observations(case, relocated)
        refused = consumer(case, case["candidate"], authority, "unavailable-main", exit_code=1)
        assert "PUBLICATION_REMOTE_UNAVAILABLE" in refused["reason"], refused
        after = observations(case, relocated)
        assert after == before
    finally:
        # Restore before any public lease settlement or base-fixture allocation cleanup.
        assert not remote.exists() and relocated.is_dir()
        relocated.rename(remote)
    restored = observations(case)
    assert restored == available
    evidence = {"preparation": prepared, "original_lease": authority, "original_intent": original,
                "available_state": available, "before_refusal": before, "refusal": refused,
                "after_refusal": after, "restored_state": restored}
    finish(case, authority, evidence)
