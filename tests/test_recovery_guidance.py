"""Directions are read-only observations, executable only through guarded verbs."""

from __future__ import annotations

import json

import pytest

from tests.test_cleanup_recovery import metadata_bytes
from tests.test_controlled import _cli, acquire_one, adopt_canonical
from tests.test_integration_abort import POLICY_THREE_CANDIDATES_V5, _prepared_candidate, _record
from tests.test_integration_pipeline import _publish, _validate
from tests.test_shadow import GitRepository, git
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import acquire_publication_lease, refresh_integration
from worktree_controller.recovery_guidance import recovery_guide
from worktree_controller.store import MetadataStore


@pytest.fixture
def blocked():
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        candidate, claimed, source = _prepared_candidate(repository)
        _validate(repository, candidate, "fixture-only-validation")
        path = repository.repo / "src/one.txt"
        path.parent.mkdir(exist_ok=True)
        path.write_text("conflicting main\n")
        git(repository.repo, "add", "src/one.txt")
        git(repository.repo, "commit", "-m", "advance main with real conflict")
        git(repository.repo, "push", "origin", "main")
        allocation = claimed["allocation_id"]
        stale = acquire_publication_lease(repository.repo, target=candidate,
            expected_record_version=_record(repository, allocation).record_version,
            owner="integration-owner", run_id="stale-main")
        assert stale["decision"]["code"] == "NEEDS_REPREPARE"
        conflict = refresh_integration(repository.repo, target=candidate,
            expected_record_version=_record(repository, allocation).record_version,
            owner="integration-owner")
        assert conflict["decision"]["code"] == "INTEGRATION_REFRESH_CONFLICT"
        assert _record(repository, allocation).lifecycle == "BLOCKED"
        yield repository, candidate, claimed, source


def execute(argv, expected=0):
    result = _cli(argv[1:])
    assert result.returncode == expected, result.stdout + result.stderr
    return json.loads(result.stdout)


def observe(repository, target):
    store = MetadataStore(inventory(repository.repo).common_dir)
    before, refs = metadata_bytes(store), git(repository.repo, "show-ref")
    result = recovery_guide(repository.repo, target=target)["decision"]
    assert metadata_bytes(store) == before
    assert git(repository.repo, "show-ref") == refs
    assert result["mutation_performed"] is result["execution_authority"] is False
    assert result["execution_guards_checked"] is result["task_completed"] is False
    return result


def test_real_blocked_guidance_recovers_and_readmits_the_same_intent(blocked):
    repository, candidate, claimed, source = blocked
    head = git(candidate, "rev-parse", "HEAD")
    source_record = _record(repository, source)
    guide = observe(repository, candidate)
    assert guide["contract"] == "recovery-guidance/v1"
    assert guide["blockers"] == []
    assert "cannot abort directly" in guide["reason"]
    first = guide["next_steps"][0]["argv"]
    refused = ["worktree-controller", "integration-abort", "--repo", str(repository.repo),
               "--path", str(candidate), "--owner", "integration-owner",
               "--expected-record-version", str(guide["identity"]["record_version"]),
               "--reason", "direct abort is forbidden", "--json"]
    assert execute(refused, 2)["errors"][0]["code"] == "INVALID_LIFECYCLE"
    assert execute(first)["decision"]["code"] == "QUARANTINED"
    assert execute(first, 2)["errors"][0]["code"] == "CAS_MISMATCH"
    quarantine = observe(repository, candidate)
    assert quarantine["identity"]["record_version"] == guide["identity"]["record_version"] + 1
    assert execute(quarantine["next_steps"][0]["argv"])["decision"]["code"] == "INTEGRATION_ABORTED"
    aborted = observe(repository, candidate)
    release = aborted["next_steps"][0]["argv"]
    execute(release)
    execute([argument for argument in release if argument != "--dry-run"])
    assert not candidate.exists()
    assert git(repository.repo, "rev-parse", f"refs/heads/{guide['identity']['branch']}") == head
    assert _record(repository, source) == source_record
    retry_guide = recovery_guide(repository.repo, intent_id=claimed["intent_id"])["decision"]
    retry = retry_guide["next_steps"][0]["argv"]
    plan = execute(retry)["decision"]["plan_id"]
    applied = execute([argument for argument in retry if argument != "--dry-run"]
                      + ["--expected-plan-id", plan])["decision"]
    assert applied["code"] == "INTEGRATION_RETRY_QUEUED"
    new = execute(["worktree-controller", "integration-claim", "--repo", str(repository.repo),
                   "--intent-id", claimed["intent_id"], "--owner", "integration-owner",
                   "--path", str(repository.root / "new-attempt"), "--resolve-conflicts", "--json"])
    assert new["decision"]["code"] == "INTEGRATION_CONFLICT_CLAIMED"
    assert new["decision"]["allocation_id"]
    assert new["decision"]["intent_id"] == claimed["intent_id"]
    assert new["decision"]["allocation_id"] != claimed["allocation_id"]
    intent = MetadataStore(inventory(repository.repo).common_dir).load().integration_intents[claimed["intent_id"]]
    assert intent.source_handoffs == tuple(retry_guide["intent"]["source_handoffs"])
    assert intent.scope_paths == tuple(retry_guide["intent"]["scope_paths"])
    assert intent.validation_evidence_id is intent.validated_oid is None


@pytest.mark.parametrize("drift", ["dirty", "head", "source"])
def test_drift_never_produces_a_mutating_recovery_command(blocked, drift):
    repository, candidate, _claimed, source = blocked
    if drift == "dirty":
        (candidate / "unknown.txt").write_text("keep this unfinished work")
    else:
        root = candidate if drift == "head" else repository.root / "src-task"
        (root / "extra.txt").write_text("preserve extra commit")
        git(root, "add", "extra.txt")
        git(root, "commit", "-m", "introduce identity drift")
    result = observe(repository, candidate)
    assert result["blockers"]
    assert not any(step.get("argv", [None, None])[1] in {"quarantine", "integration-abort", "release"}
                   for step in result["next_steps"])
    if drift == "dirty":
        assert (candidate / "unknown.txt").read_text() == "keep this unfinished work"
    assert _record(repository, source).lifecycle == "HANDOFF_READY"


def test_generated_evidence_is_preserved_and_release_guards_still_refuse(blocked):
    repository, candidate, _claimed, _source = blocked
    (candidate / ".venv").mkdir()
    generated = candidate / ".venv/unknown.bin"
    generated.write_bytes(b"preserve generated evidence\x00\xff")
    common = inventory(repository.repo).common_dir
    with (common / "info/exclude").open("a") as stream:
        stream.write("\n.venv/\n")
    guide = observe(repository, candidate)
    execute(guide["next_steps"][0]["argv"])
    execute(observe(repository, candidate)["next_steps"][0]["argv"])
    release = observe(repository, candidate)["next_steps"][0]["argv"]
    assert execute(release, 2)["errors"][0]["code"] == "IGNORED_CONTENT"
    assert generated.read_bytes() == b"preserve generated evidence\x00\xff"
    assert candidate.exists()


def test_cli_unknown_selectors_refuse_and_human_output_explains_the_next_step():
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        result = _cli(["recovery-guide", "--repo", str(repository.repo), "--path", str(target)])
        assert result.returncode == 0, result.stderr
        assert "Task checkpoint" in result.stdout and "Read-only guidance" in result.stdout
        result = _cli(["recovery-guide", "--repo", str(repository.repo), "--intent-id", "missing", "--json"])
        assert result.returncode == 2
        assert json.loads(result.stdout)["errors"][0]["code"] == "UNKNOWN_INTENT"


@pytest.fixture
def published():
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES_V5) as repository:
        candidate, claimed, _source = _prepared_candidate(repository)
        _validate(repository, candidate, "terminal-guidance-validation")
        _publish(repository, candidate)
        assert not candidate.exists()
        yield repository, claimed


def test_published_released_intent_explains_terminal_evidence_without_mutation(published):
    repository, claimed = published
    store = MetadataStore(inventory(repository.repo).common_dir)
    before, refs = metadata_bytes(store), git(repository.repo, "show-ref")
    argv = ["worktree-controller", "recovery-guide", "--repo", str(repository.repo),
            "--intent-id", claimed["intent_id"], "--json"]
    first = execute(argv)["decision"]
    assert execute(argv)["decision"] == first
    assert first["terminal_status"] == "PUBLISHED_RELEASED"
    assert first["terminal_evidence"]["allocation_id"] == claimed["allocation_id"]
    assert first["terminal_evidence"]["release_receipt_id"]
    assert first["terminal_evidence"]["publication_receipt_ids"]
    assert first["blockers"] == []
    assert first["execution_authority"] is first["execution_guards_checked"] is False
    assert first["mutation_performed"] is first["task_completed"] is False
    assert [step["action"] for step in first["next_steps"]] == ["inspect_history"]
    execute(first["next_steps"][0]["argv"])
    assert metadata_bytes(store) == before
    assert git(repository.repo, "show-ref") == refs


@pytest.mark.parametrize("drift", ["owner", "head", "missing_release", "missing_publication"])
def test_terminal_guidance_never_infers_completion_from_missing_or_drifted_evidence(
    published, monkeypatch, drift
):
    from worktree_controller import recovery_guidance

    repository, claimed = published
    original = recovery_guidance.lifecycle_history

    def changed(*args, **kwargs):
        payload = original(*args, **kwargs)
        rows = payload["decision"]["closed_releases"]
        if drift == "missing_release":
            rows.clear()
        elif drift == "missing_publication":
            rows[0]["deliveries"].clear()
        else:
            rows[0]["owner" if drift == "owner" else "released_head"] = "drifted"
        return payload

    monkeypatch.setattr(recovery_guidance, "lifecycle_history", changed)
    result = recovery_guide(repository.repo, intent_id=claimed["intent_id"])["decision"]
    assert "terminal_status" not in result
    assert result["blockers"] == ["CLOSED_LIFECYCLE_EVIDENCE_INCOMPLETE"]
    assert [step["action"] for step in result["next_steps"]] == ["inspect_history"]
    assert result["task_completed"] is result["execution_authority"] is False
