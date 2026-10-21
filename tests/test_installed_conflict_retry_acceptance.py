"""An installed Controller and Finalizer preserve one intent through conflict retries."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.test_installed_load_acceptance import fixture_environment as fixture_environment
from tests.test_installed_load_acceptance import installed as installed
from tests.test_integration_abort import POLICY_THREE_CANDIDATES_V5
from tests.test_shadow import git

OWNER = "fixture"
FILE = "shared.py"


def controller(case, verb, *arguments, error=None, code=None, exit_code=None):
    argv = [str(case["installed"]), verb, "--repo", str(case["repo"]), "--json",
            *map(str, arguments)]
    result = subprocess.run(argv, cwd=case["root"], env=case["environment"], capture_output=True,
                            text=True, timeout=30, check=False)
    payload = json.loads(result.stdout)
    with (case["root"] / "controller-calls.jsonl").open("a") as stream:
        stream.write(json.dumps({"argv": argv, "returncode": result.returncode,
                                 "payload": payload}) + "\n")
    if error:
        assert result.returncode == 2 and [e["code"] for e in payload["errors"]] == [error], payload
    else:
        expected_codes = (0, 2) if exit_code is None else (exit_code,)
        assert result.returncode in expected_codes and not payload["errors"], (payload, result.stderr)
        if code:
            assert payload["decision"]["code"] == code, payload
        else:
            assert result.returncode == 0, (payload, result.stderr)
    return payload


def finalizer(case, path, label, *arguments):
    result = subprocess.run([str(case["finalizer"]), "--summary", "--repo", str(path),
                             *map(str, arguments)], cwd=case["root"], env=case["environment"],
                            capture_output=True, text=True, timeout=60, check=False)
    receipt = case["root"] / (label + "-finalizer.json")
    receipt.write_text(result.stdout)
    payload = json.loads(result.stdout)
    assert result.returncode == 0 and payload["status"] == "success", (payload, result.stderr)
    return payload, "sha256:" + hashlib.sha256(receipt.read_bytes()).hexdigest()


def item(case, path):
    return next(row for row in controller(case, "status")["worktrees"] if row["path"] == str(path))


def intent(case, intent_id):
    return next(row for row in controller(case, "integration-queue")["decision"]["intents"]
                if row["intent_id"] == intent_id)


def frozen_identity(row):
    return {key: row[key] for key in (
        "intent_id", "queue_sequence", "enqueued_at", "scope_paths", "scope_digest",
        "source_allocations", "source_handoff_oids", "source_branches",
        "source_finalizer_record_ids", "source_snapshot_ids",
    )}


def source(case, name, content, *, relative_path=FILE):
    path = case["root"] / name
    acquired = controller(case, "acquire", "--path", path, "--task-key", name,
                          "--authority-key", name, "--module", "fixture", "--role", "feature",
                          "--owner", OWNER, "--branch", "feature/" + name,
                          "--base-ref", "origin/main", "--scope", relative_path)["decision"]
    lease = controller(case, "writer-lease-acquire", "--path", path, "--owner", OWNER,
                       "--client-id", "acceptance", "--holder-pid", os.getpid(),
                       "--session-id", name, "--run-id", name)["decision"]
    try:
        controller(case, "write-guard", "--path", path, "--task-key", name,
                   "--authority-key", name, "--owner", OWNER, "--holder", os.getpid(),
                   "--client-id", "acceptance", "--session-id", name, "--run-id", name,
                   "--operation", "feature-write")
        (path / relative_path).write_text(content)
        committed, _ = finalizer(case, path, name + "-commit", "--mode", "commit-only",
                                 "--message", "fixture " + name, "--", relative_path)
        head = committed["commit"]["sha"]
        published, receipt = finalizer(case, path, name + "-publish", "--publish-existing-branch",
                                       head, "--remote", "origin", "--remote-branch", "feature/" + name)
        assert published["mode_result"]["post_verify"] == "passed"
    finally:
        holder = lease["holder"]
        controller(case, "writer-lease-release", "--path", path, "--lease-id", lease["lease_id"],
                   "--holder-pid", holder["pid"], "--holder-boot-id", holder["boot_id"],
                   "--holder-start-ticks", holder["start_ticks"], "--client-id", holder["client_id"],
                   "--session-id", holder["session_id"], "--run-id", holder["run_id"])
    controller(case, "handoff", "--path", path, "--expected-record-version", 1,
               "--kind", "integration-ready", "--commit", head, "--finalizer-record-id", receipt,
               "--finalizer-commit", head, "--scope", relative_path)
    return acquired["allocation_id"], head


def enqueue(case, allocation, name):
    return controller(case, "integration-enqueue", "--task-key", name, "--authority-key", name,
                      "--module", "fixture", "--owner", OWNER,
                      "--source-allocation", allocation)["decision"]["intent_id"]


def claim(case, intent_id, name, *, resolve=False, code="INTEGRATION_CANDIDATE_CLAIMED"):
    path = case["root"] / name
    arguments = ["--owner", OWNER, "--intent-id", intent_id, "--path", path]
    if resolve:
        arguments += ["--resolve-conflicts"]
    decision = controller(case, "integration-claim", *arguments, code=code)["decision"]
    return path, decision


def prepare(case, path, content, label, *, relative_path=FILE):
    # These are explicit fixture content/Git checks, not a fabricated Snapshot Runner receipt.
    assert (path / relative_path).read_text() == content and not git(path, "status", "--short")
    evidence = {"kind": "fixture-content-and-git-review", "label": label,
                "head": git(path, "rev-parse", "HEAD"), "scope": [relative_path], "content": content,
                "parents": git(path, "show", "-s", "--format=%P", "HEAD").split()}
    raw = (json.dumps(evidence, sort_keys=True) + "\n").encode()
    (case["root"] / (label + "-validation.json")).write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    return controller(case, "integration-validate", "--path", path, "--owner", OWNER,
                      "--expected-record-version", item(case, path)["record_version"],
                      "--validation-evidence-id", "fixture-validation:" + digest,
                      "--tests-receipt-id", "fixture-content:" + digest,
                      "--snapshot-receipt-id", "fixture-review:" + digest)["decision"]


def publish(case, path, content, label, *, relative_path=FILE):
    prepared = prepare(case, path, content, label, relative_path=relative_path)
    return publish_prepared(case, path, prepared, label)


def publish_prepared(case, path, prepared, label):
    lease = controller(case, "lease-acquire", "--path", path, "--owner", OWNER,
                       "--expected-record-version", item(case, path)["record_version"],
                       "--run-id", label)["decision"]
    summary, _ = finalizer(case, path, label + "-main", "--publish-integration-candidate",
                           prepared["candidate_oid"], "--lease-id", lease["lease_id"], "--run-id", label)
    assert summary["remote_verify"] == {"status": "verified", "oid": prepared["candidate_oid"]}
    completed = controller(case, "lease-complete", "--path", path,
                           "--expected-record-version", lease["record_version"],
                           "--lease-id", lease["lease_id"], "--published-commit", prepared["candidate_oid"],
                           "--finalizer-record-id", summary["record_id"], "--run-id", label)["decision"]
    assert completed["code"] == "PUBLICATION_RELEASED" and not path.exists()
    return prepared, completed


def abort(case, path):
    row = item(case, path)
    if row["lifecycle"] == "HANDOFF_READY":
        controller(case, "integration-reopen", "--path", path,
                   "--expected-record-version", row["record_version"], "--owner", OWNER)
    return controller(case, "integration-abort", "--path", path,
                      "--expected-record-version", item(case, path)["record_version"],
                      "--owner", OWNER, "--reason", "fixture preparation intentionally stopped",
                      code="INTEGRATION_ABORTED")["decision"]


def release(case, path):
    allocation = item(case, path)["allocation_id"]
    controller(case, "release", "--path", path, "--owner-release", OWNER,
               "--expected-record-version", item(case, path)["record_version"])
    assert not path.exists()
    history = controller(case, "lifecycle-history", "--allocation-id", allocation)["decision"]
    assert len(history["closed_releases"]) == 1
    assert history["closed_releases"][0]["proof_status"] == "COMPLETE"


def retry(case, intent_id):
    before = intent(case, intent_id)
    arguments = ["--intent-id", intent_id, "--owner", OWNER, "--reason", "fixture retry same source"]
    plan = controller(case, "integration-retry", *arguments, "--dry-run")["decision"]
    assert intent(case, intent_id) == before
    controller(case, "integration-retry", *arguments, "--expected-plan-id", plan["plan_id"],
               code="INTEGRATION_RETRY_QUEUED")
    after = intent(case, intent_id)
    assert frozen_identity(after) == frozen_identity(before) and after["attempt"] == before["attempt"] + 1
    for field in ("candidate_oid", "validated_oid", "validation_evidence_id", "tests_receipt_id",
                  "snapshot_receipt_id", "prepared_receipt_id"):
        assert after[field] is None, after
    return plan


@pytest.fixture
def case(installed, fixture_environment, tmp_path):
    configured = os.environ.get("GIT_FINALIZER_ACCEPTANCE")
    if not configured:
        pytest.skip("set GIT_FINALIZER_ACCEPTANCE for installed conflict retry acceptance")
    finalizer_entry = Path(configured)
    assert finalizer_entry.is_absolute() and finalizer_entry.is_file()
    repo, remote = tmp_path / "repo", tmp_path / "remote.git"
    git(tmp_path, "init", "-b", "main", str(repo))
    git(repo, "config", "user.name", "Acceptance Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    policy = repo / ".agents/worktree-policy.toml"
    policy.parent.mkdir()
    policy.write_text(POLICY_THREE_CANDIDATES_V5)
    (repo / FILE).write_text('VALUE = "base"\n')
    git(repo, "add", ".agents/worktree-policy.toml", FILE)
    git(repo, "commit", "-m", "fixture baseline before governance")
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-u", "origin", "main")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "worktree-controller").symlink_to(installed)
    environment = {**fixture_environment,
                   "PATH": str(binaries) + os.pathsep + fixture_environment["PATH"]}
    fixture = {"installed": installed, "finalizer": finalizer_entry, "repo": repo,
               "root": tmp_path, "environment": environment}
    controller(fixture, "adopt", "--path", repo, "--task-key", "control", "--authority-key", "control",
               "--module", "fixture", "--role", "control-plane", "--owner", OWNER, "--lifecycle", "ACTIVE")
    allocation, head = source(fixture, "original-source", 'VALUE = "source"\n')
    intent_id = enqueue(fixture, allocation, "original-integration")
    candidate, _ = claim(fixture, intent_id, "first-candidate")
    fixture.update(source_allocation=allocation, source_head=head, intent_id=intent_id, candidate=candidate)
    try:
        yield fixture
    finally:
        # All rows and refs belong to this disposable fixture; only public lifecycle verbs close them.
        for row in controller(fixture, "status")["worktrees"]:
            if not row["canonical"] and row["role"] == "integration":
                path = Path(row["path"])
                if row["lifecycle"] != "INTEGRATION_ABORTED":
                    abort(fixture, path)
                release(fixture, path)
        for row in controller(fixture, "integration-queue")["decision"]["intents"]:
            if row["effective_state"] not in {"RELEASED", "ABANDONED"}:
                controller(fixture, "integration-abandon", "--intent-id", row["intent_id"],
                           "--owner", OWNER, "--reason", "fixture acceptance finished")
        for row in controller(fixture, "status")["worktrees"]:
            if not row["canonical"]:
                release(fixture, Path(row["path"]))
        final = controller(fixture, "status")
        assert len(final["worktrees"]) == 1 and final["repository"]["writer_leases_recorded"] == 0


def conflict_candidate(case):
    """Retry one formally closed preparation after main moves through normal publication."""
    intent_id, first = case["intent_id"], case["candidate"]
    original = frozen_identity(intent(case, intent_id))
    old = prepare(case, first, 'VALUE = "source"\n', "old-preparation")
    stopped = abort(case, first)
    release(case, first)
    stopped_attempt = intent(case, intent_id)["attempt"]
    closed = controller(case, "lifecycle-history", "--allocation-id", stopped["allocation_id"])["decision"]
    assert len(closed["closed_releases"]) == 1 and closed["closed_releases"][0]["proof_status"] == "COMPLETE"
    main_source, _ = source(case, "intervening-source", 'VALUE = "main"\n')
    other = enqueue(case, main_source, "intervening-integration")
    main_candidate, _ = claim(case, other, "main-candidate")
    publish(case, main_candidate, 'VALUE = "main"\n', "intervening-publication")
    retry(case, intent_id)
    _, blocked = claim(case, intent_id, "blocked-candidate", code="INTEGRATION_CONFLICT_BLOCKED")
    assert not (case["root"] / "blocked-candidate").exists()
    assert blocked["intent_id"] == intent_id
    plan = retry(case, intent_id)
    assert plan["plan"]["failure_evidence"]["kind"] == "unallocated_conflict"
    candidate, claimed = claim(case, intent_id, "resolved-candidate", resolve=True,
                               code="INTEGRATION_CONFLICT_CLAIMED")
    controller(case, "write-guard", "--path", candidate, "--task-key", claimed["task_key"],
               "--authority-key", claimed["authority_key"], "--owner", OWNER, "--operation", "integration-write")
    return candidate, claimed, old, stopped_attempt, original


def merge_resolution(case, candidate, claimed, *, foreign=False):
    merged = subprocess.run(["git", "-C", str(candidate), "merge", "--no-ff", "--no-commit", case["source_head"]],
                            env=case["environment"], capture_output=True, text=True, timeout=30, check=False)
    assert merged.returncode == 1 and FILE in git(candidate, "diff", "--name-only", "--diff-filter=U")
    (candidate / FILE).write_text('VALUE = "resolved"\n')
    git(candidate, "add", "--", FILE)  # Mark this exact conflict resolved before Finalizer's merge mode.
    if foreign:
        # Deliberately malformed Git fixture input; this is not a Finalizer receipt or delivery.
        (candidate / "foreign.py").write_text('VALUE = "outside frozen scope"\n')
        git(candidate, "add", "--", "foreign.py")
        git(candidate, "commit", "-m", "fixture corrupt merge scope")
        head = git(candidate, "rev-parse", "HEAD")
        receipt = "fixture-corrupt-merge:" + head
    else:
        committed, receipt = finalizer(case, candidate, "resolved-merge", "--mode", "commit-only",
                                       "--merge-parent", case["source_head"], "--message", "resolve fixture source", "--", FILE)
        head = committed["commit"]["sha"]
    assert git(candidate, "show", "-s", "--format=%P", "HEAD").split() == [claimed["base_main_oid"], case["source_head"]]
    return head, receipt


def resolve(case, candidate, head, receipt):
    return controller(case, "integration-resolve", "--path", candidate,
                      "--expected-record-version", item(case, candidate)["record_version"],
                      "--owner", OWNER, "--expected-head", head, "--finalizer-record-id", receipt)["decision"]


def refusal_state(case, candidate):
    status = controller(case, "status")
    return {
        "intent": intent(case, case["intent_id"]),
        "candidate": next(row for row in status["worktrees"] if row["path"] == str(candidate)),
        "source": next(row for row in status["worktrees"]
                       if row["allocation_id"] == case["source_allocation"]),
        "publication_lease": status["repository"]["publication_lease"],
        "remote_main": git(case["repo"], "ls-remote", "origin", "refs/heads/main"),
    }


def refuse_resolution(case, candidate, head, receipt, error):
    before = refusal_state(case, candidate)
    assert before["publication_lease"] is None
    payload = controller(case, "integration-resolve", "--path", candidate,
                         "--expected-record-version", before["candidate"]["record_version"],
                         "--owner", OWNER, "--expected-head", head,
                         "--finalizer-record-id", receipt, error=error)
    assert refusal_state(case, candidate) == before
    (case["root"] / "refusal.json").write_text(json.dumps({"before": before, "refusal": payload}, indent=2))


def test_installed_retried_conflict_resolves_and_publishes(case):
    candidate, claimed, old, stopped_attempt, original = conflict_candidate(case)
    head, receipt = merge_resolution(case, candidate, claimed)
    accepted = resolve(case, candidate, head, receipt)
    assert accepted["candidate_oid"] == head and accepted["validation_evidence_id"] is None
    fresh, completed = publish(case, candidate, 'VALUE = "resolved"\n', "resolved-publication")
    assert fresh["prepared_receipt_id"] != old["prepared_receipt_id"]
    after = intent(case, case["intent_id"])
    assert after["effective_state"] == "RELEASED" and after["attempt"] == stopped_attempt + 2
    assert frozen_identity(after) == original and after["candidate_oid"] == head
    assert git(case["repo"], "ls-remote", "origin", "refs/heads/main").split()[0] == head
    history = controller(case, "lifecycle-history", "--allocation-id", case["source_allocation"])["decision"]
    row = history["closed_releases"][0]
    assert row["proof_status"] == "COMPLETE" and row["released_head"] == case["source_head"]
    assert row["deliveries"][0]["candidate_oid"] == head
    (case["root"] / "flow.json").write_text(json.dumps({"original": original, "after": after,
                                                         "publication": completed, "source_release": row}, indent=2))


def test_installed_resolution_refuses_wrong_head(case):
    candidate, claimed, *_ = conflict_candidate(case)
    _, receipt = merge_resolution(case, candidate, claimed)
    refuse_resolution(case, candidate, case["source_head"], receipt, "CANDIDATE_IDENTITY_MISMATCH")


def test_installed_resolution_refuses_dirty_candidate(case):
    candidate, claimed, *_ = conflict_candidate(case)
    head, receipt = merge_resolution(case, candidate, claimed)
    (candidate / FILE).write_text('VALUE = "still changing"\n')
    try:
        refuse_resolution(case, candidate, head, receipt, "CANDIDATE_IDENTITY_MISMATCH")
    finally:
        # Restore only this test's known uncommitted fixture edit before guarded abort/release.
        (candidate / FILE).write_text('VALUE = "resolved"\n')


def test_installed_resolution_refuses_scope_escape(case):
    candidate, claimed, *_ = conflict_candidate(case)
    head, receipt = merge_resolution(case, candidate, claimed, foreign=True)
    refuse_resolution(case, candidate, head, receipt, "PATH_SCOPE_VIOLATION")


def test_installed_resolution_refuses_wrong_parents(case):
    candidate, claimed, *_ = conflict_candidate(case)
    head, receipt = merge_resolution(case, candidate, claimed)
    # A synthetic extra fixture commit has one parent instead of the exact pinned merge pair.
    (candidate / FILE).write_text('VALUE = "novel fixture commit"\n')
    git(candidate, "add", "--", FILE)
    git(candidate, "commit", "-m", "fixture corrupt parent topology")
    changed = git(candidate, "rev-parse", "HEAD")
    assert git(candidate, "show", "-s", "--format=%P", "HEAD") == head
    refuse_resolution(case, candidate, changed, receipt, "RESOLUTION_PARENT_MISMATCH")


def test_installed_resolved_candidate_requires_fresh_validation(case):
    candidate, claimed, old, _, original = conflict_candidate(case)
    head, receipt = merge_resolution(case, candidate, claimed)
    accepted = resolve(case, candidate, head, receipt)
    assert accepted["candidate_oid"] == head
    before = refusal_state(case, candidate)
    for field in ("validated_oid", "validation_evidence_id", "tests_receipt_id",
                  "snapshot_receipt_id", "prepared_receipt_id"):
        assert before["intent"][field] is None, before
    assert before["publication_lease"] is None
    payload = controller(case, "lease-acquire", "--path", candidate, "--owner", OWNER,
                         "--expected-record-version", before["candidate"]["record_version"],
                         "--run-id", "premature-resolution-publication", error="INVALID_LIFECYCLE")
    assert refusal_state(case, candidate) == before
    (case["root"] / "refusal.json").write_text(json.dumps({"before": before, "refusal": payload}, indent=2))
    fresh, completed = publish(case, candidate, 'VALUE = "resolved"\n', "fresh-resolution-publication")
    assert fresh["prepared_receipt_id"] != old["prepared_receipt_id"]
    assert fresh["validated_oid"] == head and completed["code"] == "PUBLICATION_RELEASED"
    assert frozen_identity(intent(case, case["intent_id"])) == original
    assert git(case["repo"], "ls-remote", "origin", "refs/heads/main").split()[0] == head
    closed = controller(case, "lifecycle-history", "--allocation-id", claimed["allocation_id"])["decision"]
    assert len(closed["closed_releases"]) == 1
    assert closed["closed_releases"][0]["proof_status"] == "COMPLETE"


def test_installed_retry_refuses_candidate_without_abort(case):
    before = intent(case, case["intent_id"])
    controller(case, "integration-retry", "--intent-id", case["intent_id"], "--owner", OWNER,
               "--reason", "fixture refusal", "--dry-run", error="RETRY_NOT_ELIGIBLE")
    assert intent(case, case["intent_id"]) == before and case["candidate"].exists()


def test_installed_retry_refuses_unreleased_aborted_candidate(case):
    abort(case, case["candidate"])
    before = intent(case, case["intent_id"])
    controller(case, "integration-retry", "--intent-id", case["intent_id"], "--owner", OWNER,
               "--reason", "fixture refusal", "--dry-run", error="RETRY_FAILURE_EVIDENCE_MISMATCH")
    assert intent(case, case["intent_id"]) == before and case["candidate"].exists()
