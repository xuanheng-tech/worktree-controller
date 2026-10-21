from __future__ import annotations

import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from tests.test_controlled import (
    _commit_reproducible_project,
    _configure_remote,
    _create_reproducible_venv,
    acquire_one,
    adopt_canonical,
    handoff_one,
)
from tests.test_shadow import POLICY, GitRepository, git
from worktree_controller.controller import guard_write, plan, status
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import (
    abort_publication_lease,
    acquire_publication_lease,
    claim_integration,
    complete_publication_lease,
    enqueue_integration,
    integration_queue,
    reconcile_integration_intents,
    refresh_integration,
    register_disposable,
    validate_integration,
)
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore

POLICY_THREE_CANDIDATES = """\
schema_version = 4

[limits]
normal_limit = 7
hard_limit = 9

[canonical]
protected = true
allowed_roles = ["canonical", "control-plane", "integration", "publication", "verification"]

[slots]
integration_prepare_limit = 3
feature_or_audit = 3
emergency = 1
recovery_reserve = 1

[publication]
main_publish_limit = 1
remote = "origin"
branch = "main"

[release]
automatic_feature_audit = true
automatic_integration_continuation = true
preserved_auto_release = false
intent_ttl_seconds = 3600

[review]
stale_warning_days = 7

[authority]
require_adoption = true
require_task_key = true
require_authority_key = true
unique_active_feature_authority = true
"""

POLICY_THREE_CANDIDATES_V2 = (
    POLICY_THREE_CANDIDATES.replace("schema_version = 4", "schema_version = 2")
    .replace("integration_prepare_limit", "integration_candidate_limit")
    .replace(
        """\
[release]
automatic_feature_audit = true
automatic_integration_continuation = true
preserved_auto_release = false
intent_ttl_seconds = 3600

""",
        "",
    )
)


def _source(
    repository: GitRepository,
    *,
    task: str,
    relative_path: str,
    content: str,
) -> tuple[Path, str, str]:
    target, payload = acquire_one(
        repository,
        task=task,
        authority=task,
        scope_paths=(relative_path,),
    )
    destination = target / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    git(target, "add", relative_path)
    git(target, "commit", "-m", f"add {task}")
    handoff_one(repository, target)
    decision = payload["decision"]
    assert isinstance(decision, dict)
    return (
        target,
        str(decision["allocation_id"]),
        git(target, "rev-parse", "HEAD"),
    )


def _enqueue(
    repository: GitRepository,
    *,
    task: str,
    source_allocation: str,
) -> dict[str, Any]:
    return enqueue_integration(
        repository.repo,
        task_key=task,
        authority_key=task,
        module="integration",
        owner="integration-owner",
        source_allocations=(source_allocation,),
    )


def _claim(
    repository: GitRepository,
    *,
    task: str,
    intent_id: str,
) -> tuple[Path, dict[str, Any]]:
    target = repository.root / task
    payload = claim_integration(
        repository.repo,
        owner="integration-owner",
        intent_id=intent_id,
        target=target,
        branch=f"integration/{task}",
    )
    return target, payload


def _validate(
    repository: GitRepository,
    target: Path,
    evidence: str,
    *,
    expected_record_version: int = 1,
) -> dict[str, Any]:
    return validate_integration(
        repository.repo,
        target=target,
        expected_record_version=expected_record_version,
        owner="integration-owner",
        validation_evidence_id=evidence,
        tests_receipt_id=f"tests:{evidence}",
        snapshot_receipt_id=f"snapshot:{evidence}",
    )


def _publish(
    repository: GitRepository,
    target: Path,
    *,
    expected_record_version: int = 2,
    auto_release: bool = True,
) -> dict[str, Any]:
    leased = acquire_publication_lease(
        repository.repo,
        target=target,
        expected_record_version=expected_record_version,
        owner="integration-owner",
        run_id=f"publish-{target.name}",
    )
    candidate = git(target, "rev-parse", "HEAD")
    git(target, "push", "origin", "HEAD:main")
    return complete_publication_lease(
        repository.repo,
        target=target,
        expected_record_version=expected_record_version + 1,
        lease_id=str(leased["decision"]["lease_id"]),
        published_commit=candidate,
        finalizer_record_id=f"publication-{target.name}",
        run_id=str(leased["decision"]["run_id"]),
        auto_release=auto_release,
    )


def test_policy_v1_queues_and_publishes_frozen_source() -> None:
    with GitRepository(linked=0, policy_text=POLICY) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        _source_path, source_allocation, source_head = _source(
            repository,
            task="source-v1",
            relative_path="source.txt",
            content="policy v1 source\n",
        )
        queued = _enqueue(
            repository,
            task="candidate-v1",
            source_allocation=source_allocation,
        )
        assert queued["decision"]["code"] == "INTEGRATION_ENQUEUED"
        intent_id = str(queued["decision"]["intent_id"])
        intent = (
            MetadataStore(inventory(repository.repo).common_dir)
            .load()
            .integration_intents[intent_id]
        )
        assert intent.source_handoffs == (source_head,)

        target, claimed = _claim(repository, task="candidate-v1", intent_id=intent_id)
        assert claimed["decision"]["code"] == "INTEGRATION_CANDIDATE_CLAIMED"
        candidate = git(target, "rev-parse", "HEAD")
        git(target, "merge-base", "--is-ancestor", source_head, candidate)
        _validate(repository, target, "policy-v1-validation")
        published = _publish(repository, target, auto_release=False)
        assert published["decision"]["code"] == "PUBLICATION_COMPLETED"
        assert (
            git(repository.repo, "ls-remote", "origin", "refs/heads/main").split()[0] == candidate
        )


def test_integration_queue_requires_explicit_policy() -> None:
    with GitRepository(linked=0, policy=False) as repository:
        adopt_canonical(repository)
        with pytest.raises(ControllerError, match="explicit policy") as error:
            _enqueue(repository, task="candidate-no-policy", source_allocation="unused")
        assert error.value.code == "INVALID_POLICY"


def test_three_candidates_claim_concurrently_fourth_queues_and_one_lease() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        source_allocations = [
            _source(
                repository,
                task=f"source-{index}",
                relative_path=f"module-{index}.txt",
                content=f"source {index}\n",
            )[1]
            for index in range(1, 4)
        ]
        intents = [
            _enqueue(
                repository,
                task=f"candidate-{index}",
                source_allocation=source_allocations[(index - 1) % 3],
            )["decision"]
            for index in range(1, 5)
        ]

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(
                    _claim,
                    repository,
                    task=f"candidate-{index}",
                    intent_id=str(intents[index - 1]["intent_id"]),
                )
                for index in range(1, 4)
            ]
            claimed = [future.result() for future in futures]

        fourth_path, fourth = _claim(
            repository,
            task="candidate-4",
            intent_id=str(intents[3]["intent_id"]),
        )
        assert fourth["decision"]["code"] == "INTEGRATION_WAITING_SCOPE"
        assert not fourth_path.exists()

        payload = status(repository.repo)
        assert payload["repository"]["worktree_count"] == 7
        assert payload["repository"]["normal_slots_remaining"] == 0
        assert payload["repository"]["hard_slots_remaining"] == 2
        assert payload["repository"]["integration_queue_depth"] == 1
        assert sum(item["role"] == "integration" for item in payload["worktrees"]) == 3
        assert (
            sum(item["role"] in {"recovery", "preservation"} for item in payload["worktrees"]) == 0
        )
        recovery = plan(
            repository.repo,
            task_key="recovery-slot-check",
            authority_key="recovery-slot-check",
            module="governance",
            role="recovery",
        )
        assert recovery["decision"]["code"] == "CREATE_RECOVERY_ALLOWED"

        candidates = [target for target, _claim_payload in claimed]
        for index, target in enumerate(candidates, start=1):
            validation = _validate(repository, target, f"validation-{index}")
            assert validation["decision"]["queue_wait_duration_ms"] is not None
        blocked_feature = guard_write(
            repository.repo,
            target=candidates[0],
            task_key="candidate-1",
            authority_key="candidate-1",
            owner="integration-owner",
            operation="feature-write",
        )
        assert blocked_feature["decision"]["code"] == "BLOCK_INTEGRATION_FEATURE_WRITE"
        blocked_validated_write = guard_write(
            repository.repo,
            target=candidates[0],
            task_key="candidate-1",
            authority_key="candidate-1",
            owner="integration-owner",
            operation="integration-write",
        )
        assert blocked_validated_write["decision"]["code"] == "BLOCK_VALIDATED_CANDIDATE_FROZEN"

        def acquire_lease(candidate: Path) -> dict[str, Any] | str:
            try:
                return acquire_publication_lease(
                    repository.repo,
                    target=candidate,
                    expected_record_version=2,
                    owner="integration-owner",
                    run_id=f"concurrent-{candidate.name}",
                )
            except ControllerError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=3) as executor:
            lease_results = list(executor.map(acquire_lease, candidates))
        successful = [result for result in lease_results if isinstance(result, dict)]
        blocked = [result for result in lease_results if isinstance(result, str)]
        assert len(successful) == 1
        assert blocked == ["PUBLICATION_LEASE_HELD", "PUBLICATION_LEASE_HELD"]
        lease = successful[0]["decision"]
        holder = next(
            candidate
            for candidate in candidates
            if candidate.name == str(lease["run_id"]).removeprefix("concurrent-")
        )
        aborted = abort_publication_lease(
            repository.repo,
            target=holder,
            expected_record_version=3,
            lease_id=str(lease["lease_id"]),
            owner="integration-owner",
            run_id=str(lease["run_id"]),
        )
        assert aborted["decision"]["code"] == "PUBLICATION_LEASE_ABORTED"
        assert integration_queue(repository.repo)["decision"]["queued"] == 1


def test_stale_main_refreshes_independent_candidate_and_blocks_only_conflict() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        shared = repository.repo / "shared.txt"
        shared.write_text("base\n", encoding="utf-8")
        git(repository.repo, "add", "shared.txt")
        git(repository.repo, "commit", "-m", "add shared base")
        adopt_canonical(repository)
        _configure_remote(repository)
        _source_a, allocation_a, _handoff_a = _source(
            repository,
            task="source-a",
            relative_path="shared.txt",
            content="published-a\n",
        )
        _source_b, allocation_b, _handoff_b = _source(
            repository,
            task="source-b",
            relative_path="independent.txt",
            content="independent-b\n",
        )
        _source_c, allocation_c, _handoff_c = _source(
            repository,
            task="source-c",
            relative_path="shared.txt",
            content="conflicting-c\n",
        )
        claims: dict[str, Path] = {}
        intent_ids: dict[str, str] = {}
        for name, allocation in (("a", allocation_a), ("b", allocation_b)):
            intent = _enqueue(
                repository,
                task=f"candidate-{name}",
                source_allocation=allocation,
            )
            intent_ids[name] = str(intent["decision"]["intent_id"])
            target, _claimed = _claim(
                repository,
                task=f"candidate-{name}",
                intent_id=intent_ids[name],
            )
            _validate(repository, target, f"validation-{name}")
            claims[name] = target

        waiting = _enqueue(
            repository,
            task="candidate-c",
            source_allocation=allocation_c,
        )
        intent_ids["c"] = str(waiting["decision"]["intent_id"])
        waiting_path, waiting_claim = _claim(
            repository,
            task="candidate-c",
            intent_id=intent_ids["c"],
        )
        assert waiting_claim["decision"]["code"] == "INTEGRATION_WAITING_SCOPE"
        assert not waiting_path.exists()

        published = _publish(repository, claims["a"])
        assert published["decision"]["code"] == "PUBLICATION_RELEASED"
        assert published["decision"]["publication_lease_duration_ms"] >= 0
        assert published["decision"]["remote_verified_to_released_ms"] >= 0
        assert not claims["a"].exists()

        reconciled = reconcile_integration_intents(
            repository.repo,
            actor="test-reconciler",
            intent_ids=(intent_ids["c"],),
        )
        assert reconciled["decision"]["promoted_intent_ids"] == [intent_ids["c"]]
        conflict_path, conflict = _claim(
            repository,
            task="candidate-c",
            intent_id=intent_ids["c"],
        )
        assert conflict["decision"]["code"] == "INTEGRATION_CONFLICT_BLOCKED"
        assert conflict["decision"]["state"] == "ATTENTION_REQUIRED"
        assert not conflict_path.exists()

        stale = acquire_publication_lease(
            repository.repo,
            target=claims["b"],
            expected_record_version=2,
            owner="integration-owner",
            run_id="stale-b",
        )
        assert stale["decision"]["code"] == "NEEDS_REPREPARE"
        refreshed = refresh_integration(
            repository.repo,
            target=claims["b"],
            expected_record_version=3,
            owner="integration-owner",
        )
        assert refreshed["decision"]["code"] == "INTEGRATION_REFRESHED"
        assert refreshed["decision"]["candidate_revision"] == 2
        assert refreshed["decision"]["attempt"] == 2
        assert refreshed["decision"]["validation_evidence_id"] is None
        assert refreshed["decision"]["validated_oid"] is None
        revalidated = validate_integration(
            repository.repo,
            target=claims["b"],
            expected_record_version=4,
            owner="integration-owner",
            validation_evidence_id="validation-b-refresh",
            tests_receipt_id="tests:validation-b-refresh",
            snapshot_receipt_id="snapshot:validation-b-refresh",
        )
        assert revalidated["decision"]["state"] == "READY_TO_PUBLISH"

        payload = status(repository.repo)
        by_path = {item["path"]: item for item in payload["worktrees"]}
        assert by_path[str(claims["b"])]["lifecycle"] == "HANDOFF_READY"
        snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
        assert snapshot.integration_intents[intent_ids["c"]].state == "ATTENTION_REQUIRED"
        receipt = next(iter(snapshot.publication_receipts.values()))
        assert receipt.status == "REMOTE_VERIFIED"
        assert receipt.lease_duration_ms >= 0
        release_receipt = next(iter(snapshot.release_receipts.values()))
        assert release_receipt.result == "RELEASED"


def test_registered_venv_is_cleaned_before_published_candidate_auto_release() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        _commit_reproducible_project(repository)
        adopt_canonical(repository)
        _configure_remote(repository)
        _source_path, allocation, _handoff = _source(
            repository,
            task="venv-source",
            relative_path="feature.txt",
            content="feature\n",
        )
        intent = _enqueue(
            repository,
            task="venv-candidate",
            source_allocation=allocation,
        )
        candidate, _claimed = _claim(
            repository,
            task="venv-candidate",
            intent_id=str(intent["decision"]["intent_id"]),
        )
        venv = _create_reproducible_venv(candidate)
        registered = register_disposable(
            repository.repo,
            target=candidate,
            expected_record_version=1,
            owner="integration-owner",
            resource_path=".venv",
            kind="python-venv",
            reproducible=True,
            creator="test-fixture",
            teardown_policy="on-release",
        )
        assert registered["decision"]["code"] == "DISPOSABLE_REGISTERED"
        _validate(repository, candidate, "validation-venv", expected_record_version=2)

        published = _publish(
            repository,
            candidate,
            expected_record_version=3,
        )
        assert published["decision"]["code"] == "PUBLICATION_RELEASED"
        assert not candidate.exists()
        assert not venv.exists()
        store = MetadataStore(inventory(repository.repo).common_dir)
        snapshot = store.load()
        receipt = next(iter(snapshot.publication_receipts.values()))
        assert receipt.status == "REMOTE_VERIFIED"
        assert len(snapshot.release_receipts) == 2
        assert any(
            receipt.allocation_id == _claimed["decision"]["allocation_id"]
            for receipt in snapshot.release_receipts.values()
        )
        assert len(tuple(store.teardown_receipts_dir.glob("*.json"))) == 1


def test_unknown_untracked_blocks_release_but_receipt_allows_idempotent_recovery() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        _source_path, allocation, _handoff = _source(
            repository,
            task="unknown-source",
            relative_path="feature.txt",
            content="feature\n",
        )
        intent = _enqueue(
            repository,
            task="unknown-candidate",
            source_allocation=allocation,
        )
        candidate, _claimed = _claim(
            repository,
            task="unknown-candidate",
            intent_id=str(intent["decision"]["intent_id"]),
        )
        _validate(repository, candidate, "validation-unknown")
        leased = acquire_publication_lease(
            repository.repo,
            target=candidate,
            expected_record_version=2,
            owner="integration-owner",
            run_id="unknown-run",
        )
        candidate_oid = git(candidate, "rev-parse", "HEAD")
        git(candidate, "push", "origin", "HEAD:main")
        unknown = candidate / "unknown-local.txt"
        unknown.write_text("preserve me\n", encoding="utf-8")

        with pytest.raises(ControllerError) as blocked:
            complete_publication_lease(
                repository.repo,
                target=candidate,
                expected_record_version=3,
                lease_id=str(leased["decision"]["lease_id"]),
                published_commit=candidate_oid,
                finalizer_record_id="publication-unknown",
                run_id="unknown-run",
            )
        assert blocked.value.code == "DIRTY_WORKTREE"
        assert unknown.read_text(encoding="utf-8") == "preserve me\n"
        snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
        receipt = snapshot.publication_receipts[str(leased["decision"]["lease_id"])]
        assert receipt.status == "REMOTE_VERIFIED"
        assert snapshot.publication_lease is None
        assert snapshot.records[receipt.allocation_id].lifecycle == "PUBLISHED"

        unknown.unlink()
        recovered = complete_publication_lease(
            repository.repo,
            target=candidate,
            expected_record_version=4,
            lease_id=receipt.receipt_id,
            published_commit=candidate_oid,
            finalizer_record_id="publication-unknown",
            run_id="unknown-run",
        )
        assert recovered["decision"]["code"] == "PUBLICATION_RELEASED"
        assert not candidate.exists()
        idempotent = complete_publication_lease(
            repository.repo,
            target=candidate,
            expected_record_version=5,
            lease_id=receipt.receipt_id,
            published_commit=candidate_oid,
            finalizer_record_id="publication-unknown",
            run_id="unknown-run",
        )
        assert idempotent["decision"]["code"] == "PUBLICATION_RELEASED"
        assert (
            git(repository.repo, "ls-remote", "origin", "refs/heads/main").split()[0]
            == candidate_oid
        )


def test_expired_lease_is_fenced_and_dirty_canonical_is_unchanged() -> None:
    with GitRepository(linked=0, policy_text=POLICY_THREE_CANDIDATES) as repository:
        canonical_file = repository.repo / "canonical.txt"
        canonical_file.write_text("base\n", encoding="utf-8")
        git(repository.repo, "add", "canonical.txt")
        git(repository.repo, "commit", "-m", "add canonical fixture")
        adopt_canonical(repository)
        _configure_remote(repository)
        _source_a, allocation_a, _handoff_a = _source(
            repository,
            task="lease-source-a",
            relative_path="a.txt",
            content="a\n",
        )
        _source_b, allocation_b, _handoff_b = _source(
            repository,
            task="lease-source-b",
            relative_path="b.txt",
            content="b\n",
        )
        candidates: list[Path] = []
        for name, allocation in (("a", allocation_a), ("b", allocation_b)):
            intent = _enqueue(
                repository,
                task=f"lease-candidate-{name}",
                source_allocation=allocation,
            )
            candidate, _claimed = _claim(
                repository,
                task=f"lease-candidate-{name}",
                intent_id=str(intent["decision"]["intent_id"]),
            )
            _validate(repository, candidate, f"validation-{name}")
            candidates.append(candidate)

        canonical_file.write_text("staged\n", encoding="utf-8")
        git(repository.repo, "add", "canonical.txt")
        canonical_file.write_text("staged-and-modified\n", encoding="utf-8")
        untracked = repository.repo / "canonical-untracked.txt"
        untracked.write_text("untracked\n", encoding="utf-8")
        index_path = Path(git(repository.repo, "rev-parse", "--git-path", "index"))
        if not index_path.is_absolute():
            index_path = repository.repo / index_path
        before = {
            "file": canonical_file.read_bytes(),
            "untracked": untracked.read_bytes(),
            "index": index_path.read_bytes(),
            "head_ref": git(repository.repo, "symbolic-ref", "HEAD"),
            "main_oid": git(repository.repo, "rev-parse", "refs/heads/main"),
            "status": subprocess.run(
                ["git", "-C", str(repository.repo), "status", "--porcelain=v1", "-z"],
                check=True,
                capture_output=True,
            ).stdout,
        }

        old_lease = acquire_publication_lease(
            repository.repo,
            target=candidates[0],
            expected_record_version=2,
            owner="integration-owner",
            run_id="expired-writer",
            ttl_seconds=1,
        )
        time.sleep(1.1)
        current_lease = acquire_publication_lease(
            repository.repo,
            target=candidates[1],
            expected_record_version=2,
            owner="integration-owner",
            run_id="current-writer",
        )
        with pytest.raises(ControllerError) as fenced:
            complete_publication_lease(
                repository.repo,
                target=candidates[0],
                expected_record_version=4,
                lease_id=str(old_lease["decision"]["lease_id"]),
                published_commit=git(candidates[0], "rev-parse", "HEAD"),
                finalizer_record_id="stale-publication",
                run_id="expired-writer",
            )
        assert fenced.value.code == "LEASE_MISMATCH"
        aborted = abort_publication_lease(
            repository.repo,
            target=candidates[1],
            expected_record_version=3,
            lease_id=str(current_lease["decision"]["lease_id"]),
            owner="integration-owner",
            run_id="current-writer",
        )
        assert aborted["decision"]["code"] == "PUBLICATION_LEASE_ABORTED"

        publication = acquire_publication_lease(
            repository.repo,
            target=candidates[1],
            expected_record_version=4,
            owner="integration-owner",
            run_id="dirty-canonical-publication",
        )
        published_oid = git(candidates[1], "rev-parse", "HEAD")
        git(candidates[1], "push", "origin", "HEAD:main")
        completed = complete_publication_lease(
            repository.repo,
            target=candidates[1],
            expected_record_version=5,
            lease_id=str(publication["decision"]["lease_id"]),
            published_commit=published_oid,
            finalizer_record_id="dirty-canonical-publication",
            run_id="dirty-canonical-publication",
        )
        assert completed["decision"]["code"] == "PUBLICATION_RELEASED"

        after = {
            "file": canonical_file.read_bytes(),
            "untracked": untracked.read_bytes(),
            "index": index_path.read_bytes(),
            "head_ref": git(repository.repo, "symbolic-ref", "HEAD"),
            "main_oid": git(repository.repo, "rev-parse", "refs/heads/main"),
            "status": subprocess.run(
                ["git", "-C", str(repository.repo), "status", "--porcelain=v1", "-z"],
                check=True,
                capture_output=True,
            ).stdout,
        }
        assert after == before
