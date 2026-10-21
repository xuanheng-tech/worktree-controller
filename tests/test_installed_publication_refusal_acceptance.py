"""Installed publication refuses changed authority, dirty evidence and split endpoints."""

import json

from tests.test_installed_conflict_retry_acceptance import frozen_identity, intent, prepare
from tests.test_installed_publication_lease_acceptance import case as case
from tests.test_installed_publication_lease_acceptance import (
    complete,
    consumer,
    lease,
    snapshot,
)
from tests.test_installed_publication_lease_acceptance import (
    fixture_environment as fixture_environment,
)
from tests.test_installed_publication_lease_acceptance import installed as installed
from tests.test_installed_publication_lease_acceptance import leased_case as leased_case
from tests.test_installed_publication_reprepare_acceptance import CONTENT, closed
from tests.test_shadow import git


def observations(case):
    return {**snapshot(case), "local_refs": git(case["repo"], "show-ref"),
            "fetch_refs": git(case["repo"], "ls-remote", "origin")}


def start(case, run):
    prepared = prepare(case, case["candidate"], CONTENT, run + "-preparation")
    original = frozen_identity(intent(case, case["intent_id"]))
    authority = lease(case, case["candidate"], run)
    return {"preparation": prepared, "original_intent": original, "lease": authority,
            "prepared_state": observations(case)}


def reject(case, authority, label, code):
    before = observations(case)
    refused = consumer(case, case["candidate"], authority, label, exit_code=1)
    assert code in refused["reason"], refused
    after = observations(case)
    assert after == before
    return {"before": before, "refusal": refused, "after": after}


def resume(case, evidence):
    # Clear only the fixture fault, then reuse the original lease and validation unchanged.
    restored = observations(case)
    assert restored == evidence["prepared_state"]
    authority = evidence["lease"]
    assert frozen_identity(intent(case, case["intent_id"])) == evidence["original_intent"]
    published = consumer(case, case["candidate"], authority, "restored-publication")
    assert published["push"]["executed"] and published["push"]["result"] == "published"
    completion, proof = complete(case, case["candidate"], authority, published)
    source_proof = closed(case, case["source_allocation"], authority["candidate_oid"])
    assert frozen_identity(intent(case, case["intent_id"])) == evidence["original_intent"]
    assert intent(case, case["intent_id"])["effective_state"] == "RELEASED"
    evidence.update(restored_state=restored, publication=published, completion=completion,
                    candidate_release=proof, source_release=source_proof)
    (case["root"] / "publication-refusal.json").write_text(json.dumps(evidence, indent=2) + "\n")


def test_installed_publication_refuses_wrong_run_without_disturbing_lease(leased_case):
    case = leased_case
    evidence = start(case, "current-publisher")
    wrong_run = {**evidence["lease"], "run_id": "wrong-publisher"}
    evidence["refused_request"] = wrong_run
    evidence["rejection"] = reject(case, wrong_run, "wrong-run", "LEASE_MISMATCH")
    resume(case, evidence)


def test_installed_publication_refuses_dirty_candidate_then_reuses_lease(leased_case):
    case = leased_case
    evidence = start(case, "clean-publisher")
    fault = case["candidate"] / "publication-fixture-dirty.txt"
    assert not fault.exists()
    content = "owned disposable publication fixture fault\n"
    fault.write_text(content)
    try:
        evidence["rejection"] = reject(case, evidence["lease"], "dirty-candidate",
                                       "PUBLICATION_EVIDENCE_MISMATCH")
    finally:
        # Restore before leased_case settles its lease and the base fixture closes allocations.
        assert fault.read_text() == content
        fault.unlink()
    resume(case, evidence)


def test_installed_publication_refuses_split_endpoints_without_pushing_either(leased_case):
    case = leased_case
    evidence = start(case, "single-endpoint-publisher")
    fetch = git(case["repo"], "remote", "get-url", "--all", "origin")
    config = git(case["repo"], "config", "--local", "--list")
    assert not any(line.startswith("remote.origin.pushurl=") for line in config.splitlines())
    push = case["root"] / "different-push.git"
    assert not push.exists()
    git(case["root"], "clone", "--bare", fetch, str(push))
    before_push = git(case["repo"], "ls-remote", str(push))
    git(case["repo"], "config", "--local", "remote.origin.pushurl", str(push))
    try:
        evidence["rejection"] = reject(case, evidence["lease"], "split-endpoints", "INVALID_REMOTE")
        after_push = git(case["repo"], "ls-remote", str(push))
        assert after_push == before_push
        evidence["push_endpoint"] = {"path": str(push), "before": before_push, "after": after_push}
    finally:
        git(case["repo"], "config", "--local", "--unset-all", "remote.origin.pushurl")
        assert git(case["repo"], "config", "--local", "--list") == config
    resume(case, evidence)
    assert git(case["repo"], "ls-remote", str(push)) == before_push
