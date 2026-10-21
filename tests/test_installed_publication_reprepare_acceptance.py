"""Installed tools fence a prepared base that another independent integration advances."""

import json

from tests.test_installed_conflict_retry_acceptance import (
    FILE,
    OWNER,
    claim,
    controller,
    enqueue,
    frozen_identity,
    intent,
    prepare,
    publish_prepared,
    source,
)
from tests.test_installed_conflict_retry_acceptance import case as case
from tests.test_installed_conflict_retry_acceptance import (
    fixture_environment as fixture_environment,
)
from tests.test_installed_conflict_retry_acceptance import installed as installed
from tests.test_shadow import git

CONTENT = 'VALUE = "source"\n'
OTHER_FILE = "independent.py"
OTHER_CONTENT = 'VALUE = "independent integration"\n'
VALIDATION_FIELDS = (
    "prepare_identity", "prepared_receipt_id", "prepared_base_origin_main_oid",
    "prepared_commit_oid", "prepared_tree_oid", "prepared_scope_digest", "validated_oid",
    "validation_evidence_id", "tests_receipt_id", "snapshot_receipt_id", "validated_at",
    "publication_identity",
)


def state(case):
    status = controller(case, "status")
    return {
        "intent": intent(case, case["intent_id"]),
        "candidate": next(row for row in status["worktrees"]
                          if row["path"] == str(case["candidate"])),
        "source": next(row for row in status["worktrees"]
                       if row["allocation_id"] == case["source_allocation"]),
        "publication_lease": status["repository"]["publication_lease"],
        "remote_main": git(case["repo"], "ls-remote", "origin", "refs/heads/main"),
    }


def closed(case, allocation, published_head):
    rows = controller(case, "lifecycle-history", "--allocation-id", allocation)["decision"]["closed_releases"]
    assert len(rows) == 1
    row = rows[0]
    assert row["proof_status"] == "COMPLETE" and row["boundary"] == "CLOSED"
    assert len(row["deliveries"]) == 1 and row["deliveries"][0]["candidate_oid"] == published_head
    assert row["deliveries"][0]["publication_status"] == "REMOTE_VERIFIED"
    return row


def advance_main(case):
    candidate = case["candidate"]
    old = prepare(case, candidate, CONTENT, "original-preparation")
    before = state(case)
    allocation, _ = source(case, "independent-source", OTHER_CONTENT, relative_path=OTHER_FILE)
    other_intent = enqueue(case, allocation, "independent-integration")
    other_candidate, claimed = claim(case, other_intent, "independent-candidate")
    prepared = prepare(case, other_candidate, OTHER_CONTENT, "independent-preparation",
                       relative_path=OTHER_FILE)
    # Both candidates are prepared before the independent integration moves main.
    assert intent(case, case["intent_id"])["state"] == "READY_TO_PUBLISH"
    assert intent(case, other_intent)["state"] == "READY_TO_PUBLISH"
    assert prepared["prepared_base_origin_main_oid"] == old["prepared_base_origin_main_oid"]
    publish_prepared(case, other_candidate, prepared, "independent-publication")
    head = prepared["candidate_oid"]
    source_proof = closed(case, allocation, head)
    candidate_proof = closed(case, claimed["allocation_id"], head)
    after = state(case)
    assert after["intent"] == before["intent"]
    assert after["candidate"] == before["candidate"] and after["source"] == before["source"]
    assert after["publication_lease"] is None
    assert after["remote_main"].split() == [head, "refs/heads/main"]
    assert head != old["prepared_base_origin_main_oid"]
    return {"before": before, "after_independent_publication": after, "old_preparation": old,
            "new_main": head, "independent_source_release": source_proof,
            "independent_candidate_release": candidate_proof}


def mark_stale(case, evidence):
    before = state(case)
    stale = controller(case, "lease-acquire", "--path", case["candidate"], "--owner", OWNER,
                       "--expected-record-version", before["candidate"]["record_version"],
                       "--run-id", "stale-preparation", code="NEEDS_REPREPARE")
    after = state(case)
    assert after["intent"]["state"] == "NEEDS_REPREPARE"
    assert frozen_identity(after["intent"]) == frozen_identity(before["intent"])
    for field in VALIDATION_FIELDS:
        assert after["intent"][field] is None, after
    for field in ("handoff_commit", "finalizer_record_id", "finalizer_commit"):
        assert after["candidate"][field] is None, after
    assert after["candidate"]["record_version"] == before["candidate"]["record_version"] + 1
    assert after["candidate"]["lifecycle"] == "ACTIVE"
    for field in ("allocation_id", "branch", "head", "base_commit", "task_commit"):
        assert after["candidate"][field] == before["candidate"][field]
    assert after["intent"]["candidate_oid"] == before["intent"]["candidate_oid"]
    assert after["source"] == before["source"] and after["remote_main"] == before["remote_main"]
    assert after["publication_lease"] is None
    guard = controller(case, "write-guard", "--path", case["candidate"],
                       "--task-key", after["candidate"]["task_key"],
                       "--authority-key", after["candidate"]["authority_key"],
                       "--owner", OWNER, "--operation", "integration-write",
                       code="BLOCK_REPREPARE_REQUIRED", exit_code=1)
    assert state(case) == after
    evidence.update(stale_response=stale, stale_state=after, write_guard=guard)
    return after


def record(case, evidence):
    (case["root"] / "reprepare.json").write_text(json.dumps(evidence, indent=2) + "\n")


def test_installed_main_advance_invalidates_preparation(case):
    evidence = advance_main(case)
    mark_stale(case, evidence)
    record(case, evidence)


def test_installed_refresh_refuses_stale_record_cas(case):
    evidence = advance_main(case)
    before = mark_stale(case, evidence)
    refusal = controller(case, "integration-refresh", "--path", case["candidate"],
                         "--owner", OWNER, "--expected-record-version",
                         evidence["before"]["candidate"]["record_version"], error="CAS_MISMATCH")
    assert state(case) == before
    evidence.update(refresh_refusal=refusal, after_refusal=state(case))
    record(case, evidence)


def test_installed_refresh_requires_new_validation_then_publishes(case):
    evidence = advance_main(case)
    stale = mark_stale(case, evidence)
    candidate = case["candidate"]
    refreshed = controller(case, "integration-refresh", "--path", candidate, "--owner", OWNER,
                           "--expected-record-version", stale["candidate"]["record_version"],
                           code="INTEGRATION_REFRESHED")
    before = state(case)
    assert frozen_identity(before["intent"]) == frozen_identity(stale["intent"])
    assert before["source"] == stale["source"]
    assert before["intent"]["candidate_revision"] == stale["intent"]["candidate_revision"] + 1
    assert before["intent"]["attempt"] == stale["intent"]["attempt"] + 1
    assert before["intent"]["base_main_oid"] == evidence["new_main"]
    head = before["intent"]["candidate_oid"]
    assert head != stale["intent"]["candidate_oid"] and git(candidate, "rev-parse", "HEAD") == head
    assert git(candidate, "show", "-s", "--format=%P", "HEAD").split() == [evidence["new_main"], case["source_head"]]
    assert (candidate / FILE).read_text() == CONTENT and (candidate / OTHER_FILE).read_text() == OTHER_CONTENT
    for field in VALIDATION_FIELDS:
        assert before["intent"][field] is None, before
    refusal = controller(case, "lease-acquire", "--path", candidate, "--owner", OWNER,
                         "--expected-record-version", before["candidate"]["record_version"],
                         "--run-id", "refreshed-without-validation", error="INVALID_LIFECYCLE")
    assert state(case) == before
    fresh = prepare(case, candidate, CONTENT, "refreshed-preparation")
    assert fresh["prepared_receipt_id"] != evidence["old_preparation"]["prepared_receipt_id"]
    assert fresh["validation_evidence_id"] != evidence["old_preparation"]["validation_evidence_id"]
    assert fresh["prepared_base_origin_main_oid"] == evidence["new_main"] and fresh["validated_oid"] == head
    _, completed = publish_prepared(case, candidate, fresh, "refreshed-publication")
    assert frozen_identity(intent(case, case["intent_id"])) == frozen_identity(stale["intent"])
    assert intent(case, case["intent_id"])["effective_state"] == "RELEASED"
    assert git(case["repo"], "ls-remote", "origin", "refs/heads/main").split() == [head, "refs/heads/main"]
    assert git(case["repo"], "show", head + ":" + FILE) == CONTENT.strip()
    assert git(case["repo"], "show", head + ":" + OTHER_FILE) == OTHER_CONTENT.strip()
    evidence.update(refresh=refreshed, without_validation_refusal=refusal, after_refusal=before,
                    fresh_preparation=fresh, publication=completed,
                    source_release=closed(case, case["source_allocation"], head),
                    candidate_release=closed(case, stale["candidate"]["allocation_id"], head))
    record(case, evidence)
