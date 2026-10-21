from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.test_controlled import (
    _cli,
    _commit_reproducible_project,
    _configure_remote,
    _create_reproducible_venv,
    _release_arguments,
    acquire_one,
    adopt_canonical,
    handoff_one,
)
from tests.test_integration_pipeline import (
    POLICY_THREE_CANDIDATES_V2,
    _claim,
    _enqueue,
    _publish,
    _source,
    _validate,
)
from tests.test_shadow import GitRepository, git
from worktree_controller.controller import doctor, guard_write, status
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import (
    acquire_publication_lease,
    build_release_plan,
    create_release_intent,
    reconcile_publication_receipts,
    reconcile_release_intents,
    record_equivalent_replay,
    register_disposable,
    release,
    reopen,
    review,
    update_external_binding,
)
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore
from worktree_controller.writer_lease import acquire_writer_lease, describe_holder


def _policy(*, automatic: bool) -> str:
    return (
        POLICY_THREE_CANDIDATES_V2.replace("schema_version = 2", "schema_version = 3")
        + "\n[release]\n"
        + f"automatic_feature_audit = {str(automatic).lower()}\n"
        + "intent_ttl_seconds = 3600\n"
    )


POLICY_MANUAL = _policy(automatic=False)
POLICY_AUTO = _policy(automatic=True)


def _publish_source(
    repository: GitRepository,
    *,
    source_task: str,
    integration_task: str,
    relative_path: str = "feature.txt",
) -> tuple[Path, str, str, Path, dict[str, object]]:
    source, source_allocation, source_head = _source(
        repository,
        task=source_task,
        relative_path=relative_path,
        content=f"{source_task}\n",
    )
    queued = _enqueue(
        repository,
        task=integration_task,
        source_allocation=source_allocation,
    )
    candidate, _claimed = _claim(
        repository,
        task=integration_task,
        intent_id=str(queued["decision"]["intent_id"]),
    )
    _validate(repository, candidate, f"validation-{integration_task}")
    published = _publish(repository, candidate)
    return source, source_allocation, source_head, candidate, published


def _release_intent_for_allocation(repository: GitRepository, allocation_id: str):
    snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
    matches = [
        intent
        for intent in snapshot.release_intents.values()
        if intent.allocation_id == allocation_id
    ]
    assert len(matches) == 1
    return matches[0]


def _repeat_publication(repository: GitRepository, candidate: Path, published: dict[str, object]):
    store = MetadataStore(inventory(repository.repo).common_dir)
    snapshot = store.load()
    receipt = snapshot.publication_receipts[str(published["decision"]["receipt_id"])]
    result = _cli([
        "lease-complete",
        "--repo", str(repository.repo),
        "--json",
        "--path", str(candidate),
        "--expected-record-version", str(snapshot.records[receipt.allocation_id].record_version),
        "--lease-id", receipt.receipt_id,
        "--published-commit", receipt.candidate_oid,
        "--finalizer-record-id", receipt.finalizer_record_id,
        "--run-id", receipt.run_id,
    ])
    payload = json.loads(result.stdout)
    assert result.returncode == 0, payload
    return payload["decision"]["source_release_trigger"]


@pytest.mark.parametrize("repo_entry", ["canonical", "candidate", "candidate-subdirectory"])
def test_cli_publication_closes_candidate_and_source_when_repo_entry_is_removed(repo_entry):
    with GitRepository(linked=1, policy_text=POLICY_AUTO) as repository:
        repository.adopt_all()
        _configure_remote(repository)
        source, allocation, head = _source(
            repository, task="entry-source", relative_path="feature.txt", content="entry source\n",
        )
        queued = _enqueue(repository, task="entry-integration", source_allocation=allocation)
        candidate, claimed = _claim(
            repository, task="entry-integration", intent_id=queued["decision"]["intent_id"],
        )
        _validate(repository, candidate, "entry-validation")
        leased = acquire_publication_lease(
            repository.repo, target=candidate, expected_record_version=2,
            owner="integration-owner", run_id="entry-publication",
        )
        git(candidate, "push", "origin", "HEAD:main")
        refs = git(repository.repo, "show-ref")
        store = MetadataStore(inventory(repository.repo).common_dir)
        peer = repository.linked[0]
        peer_policy = (peer / ".agents/worktree-policy.toml").read_bytes()
        before = store.load()
        entries = {
            "canonical": repository.repo,
            "candidate": candidate,
            "candidate-subdirectory": candidate / ".agents",
        }
        result = _cli([
            "lease-complete", "--repo", str(entries[repo_entry]), "--path", str(candidate),
            "--expected-record-version", "3", "--lease-id", leased["decision"]["lease_id"],
            "--published-commit", head, "--finalizer-record-id", "entry-finalizer",
            "--run-id", "entry-publication", "--auto-release", "--json",
        ])
        payload = json.loads(result.stdout)
        assert result.returncode == 0, payload
        assert payload["decision"]["code"] == "PUBLICATION_RELEASED"
        assert not source.exists() and not candidate.exists()
        assert (peer / ".agents/worktree-policy.toml").read_bytes() == peer_policy
        assert git(repository.repo, "show-ref") == refs
        assert not list(store.transactions_dir.glob("*.json"))
        after = store.load()
        candidate_allocation = claimed["decision"]["allocation_id"]
        assert after.records[candidate_allocation].lifecycle == "RELEASED"
        assert after.records[allocation].lifecycle == "RELEASED"
        assert after.publication_lease is None
        assert {
            key: record for key, record in after.records.items()
            if key not in {allocation, candidate_allocation}
        } == {
            key: record for key, record in before.records.items()
            if key not in {allocation, candidate_allocation}
        }
        summary = payload["decision"]["source_release_trigger"]
        outcome, = summary["outcomes"]
        assert outcome["allocation_id"] == allocation
        assert outcome["proof_status"] == "COMPLETE" and outcome["blocker"] is None
        for released_allocation in (allocation, candidate_allocation):
            history = _cli([
                "lifecycle-history", "--repo", str(repository.repo),
                "--allocation-id", released_allocation, "--json",
            ])
            assert history.returncode == 0, history.stdout
            decision = json.loads(history.stdout)["decision"]
            closed, = decision["closed_releases"]
            assert closed["proof_status"] == "COMPLETE"
            assert closed["candidate_oid"] == head
            assert decision["incomplete_history"] == []

        assert _repeat_publication(repository, candidate, payload) == summary
        repeated = store.load()
        assert repeated.records == after.records
        assert repeated.release_intents == after.release_intents
        assert repeated.release_plans == after.release_plans
        assert repeated.release_receipts == after.release_receipts
        assert repeated.publication_receipts == after.publication_receipts
        assert git(repository.repo, "show-ref") == refs


def test_source_release_summary_proves_completed_closure_and_repeats_without_mutation():
    with GitRepository(linked=0, policy_text=POLICY_AUTO) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        source, allocation, head, candidate, published = _publish_source(
            repository, source_task="summary-source", integration_task="summary-integration")
        assert not source.exists() and not candidate.exists()
        store = MetadataStore(inventory(repository.repo).common_dir)
        before = store.load()
        summary = published["decision"]["source_release_trigger"]
        assert summary["created_intent_ids"] == [] and summary["skipped"] == []
        outcome, = summary["outcomes"]
        assert outcome["allocation_id"] == allocation
        assert outcome["status"] == "RELEASED" and outcome["result"] == "ALREADY_RELEASED"
        assert outcome["blocker"] is None and outcome["proof_status"] == "COMPLETE"
        receipt = before.release_receipts[outcome["release_receipt_id"]]
        assert receipt.allocation_id == allocation and receipt.released_head == head
        assert outcome["plan_id"] == receipt.plan_id
        refs = git(repository.repo, "show-ref")

        # Branch retirement does not invalidate immutable checkout closure proof.
        git(repository.repo, "merge", "--ff-only", "origin/main")
        git(repository.repo, "branch", "-d", "feature/summary-source")
        retired_refs = git(repository.repo, "show-ref")
        assert retired_refs != refs
        assert _repeat_publication(repository, candidate, published) == summary
        after = store.load()
        assert after.release_intents == before.release_intents
        assert after.release_plans == before.release_plans
        assert after.release_receipts == before.release_receipts
        assert after.publication_receipts == before.publication_receipts
        assert git(repository.repo, "show-ref") == retired_refs


@pytest.mark.parametrize("receipt_mode", ["unique", "ambiguous", "legacy"])
def test_publication_release_duration_stays_at_the_closed_candidate_time(
    receipt_mode, monkeypatch,
):
    from worktree_controller import lifecycle

    with GitRepository(linked=0, policy_text=POLICY_AUTO) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        _source_path, _allocation, _head, candidate, published = _publish_source(
            repository, source_task="duration-source", integration_task="duration-integration")
        store = MetadataStore(inventory(repository.repo).common_dir)
        snapshot = store.load()
        publication = snapshot.publication_receipts[str(published["decision"]["receipt_id"])]
        released, = [receipt for receipt in snapshot.release_receipts.values()
                     if receipt.allocation_id == publication.allocation_id]
        elapsed_ms = int((datetime.fromisoformat(released.released_at)
                          - datetime.fromisoformat(publication.remote_verified_at))
                         .total_seconds() * 1000)
        if receipt_mode == "ambiguous":
            store.write_release_receipt(replace(released, receipt_id=str(uuid.uuid4())))
        elif receipt_mode == "legacy":
            publication = replace(publication, status="RELEASED",
                                  released_at=released.released_at,
                                  remote_verified_to_released_ms=elapsed_ms)
            (store.publication_receipts_dir / f"{publication.receipt_id}.json").unlink()
            store.write_publication_receipt(publication)
            (store.release_receipts_dir / f"{released.receipt_id}.json").unlink()
        before = store.load()
        refs = git(repository.repo, "show-ref")
        later = datetime.fromisoformat(released.released_at) + timedelta(days=3)
        monkeypatch.setattr(lifecycle, "now_iso", lambda: later.isoformat())

        repeated = lifecycle.complete_publication_lease(
            repository.repo, target=candidate,
            expected_record_version=before.records[publication.allocation_id].record_version,
            lease_id=publication.receipt_id, published_commit=publication.candidate_oid,
            finalizer_record_id=publication.finalizer_record_id, run_id=publication.run_id)

        assert repeated["decision"]["code"] == "PUBLICATION_RELEASED"
        assert repeated["decision"]["remote_verified_to_released_ms"] == (
            None if receipt_mode == "ambiguous" else elapsed_ms)
        after = store.load()
        assert after.release_receipts == before.release_receipts
        assert after.publication_receipts == before.publication_receipts
        assert after.release_plans == before.release_plans
        assert after.release_intents == before.release_intents
        assert git(repository.repo, "show-ref") == refs


@pytest.mark.parametrize(("scenario", "blocker"), [
    ("missing-receipt", "SOURCE_RELEASE_EVIDENCE_INCOMPLETE"),
    ("missing-plan", "HISTORY_EVIDENCE_INCOMPLETE"),
    ("conflicting-receipt", "HISTORY_EVIDENCE_CONFLICT"),
    ("ambiguous-receipts", "SOURCE_RELEASE_EVIDENCE_AMBIGUOUS"),
    ("finalizer-drift", "SOURCE_RELEASE_IDENTITY_MISMATCH"),
    ("owner-drift", "HISTORY_EVIDENCE_CONFLICT"),
])
def test_source_release_summary_does_not_infer_success_from_absence(scenario, blocker):
    with GitRepository(linked=0, policy_text=POLICY_AUTO) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        source, allocation, _head, candidate, published = _publish_source(
            repository, source_task="proof-source", integration_task="proof-integration")
        assert not source.exists()
        store = MetadataStore(inventory(repository.repo).common_dir)
        snapshot = store.load()
        receipt, = [r for r in snapshot.release_receipts.values() if r.allocation_id == allocation]
        if scenario == "missing-receipt":
            (store.release_receipts_dir / f"{receipt.receipt_id}.json").unlink()
        elif scenario == "missing-plan":
            store.remove_release_plan(receipt.plan_id)
        elif scenario == "conflicting-receipt":
            path = store.release_receipts_dir / f"{receipt.receipt_id}.json"
            value = json.loads(path.read_text())
            value["branch"] = "feature/other-source"
            path.write_text(json.dumps(value))
        elif scenario == "ambiguous-receipts":
            store.write_release_receipt(replace(receipt, receipt_id=str(uuid.uuid4())))
        elif scenario == "finalizer-drift":
            store.replace_record(replace(snapshot.records[allocation], finalizer_record_id="other-finalizer"))
        else:
            intent = snapshot.release_intents[receipt.release_intent_id]
            store.replace_release_intent(replace(intent, owner="other-owner"))
        before = store.load()
        summary = _repeat_publication(repository, candidate, published)
        outcome, = summary["outcomes"]
        assert outcome["status"] == "BLOCKED" and outcome["blocker"] == blocker
        assert "release_receipt_id" not in outcome
        assert summary["created_intent_ids"] == [] and summary["reconciliation"] is None
        assert summary["skipped"] == [{"allocation_id": allocation, "reason": blocker}]
        assert "lifecycle-history" in outcome["recovery_commands"]
        after = store.load()
        assert after.release_intents == before.release_intents
        assert after.release_plans == before.release_plans
        assert after.release_receipts == before.release_receipts


@pytest.mark.parametrize("scenario", ["retryable", "stale"])
def test_source_release_summary_preserves_live_release_blockers(scenario, monkeypatch):
    from worktree_controller import lifecycle

    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        _commit_reproducible_project(repository)
        adopt_canonical(repository)
        _configure_remote(repository)
        source, allocation, _head, candidate, published = _publish_source(
            repository, source_task="blocked-source", integration_task="blocked-integration")
        initial, = published["decision"]["source_release_trigger"]["outcomes"]
        assert initial["result"] == "ELIGIBLE_MANUAL" and source.exists()
        (repository.repo / ".agents/worktree-policy.toml").write_text(POLICY_AUTO)
        if scenario == "retryable":
            _create_reproducible_venv(source)
        else:
            original = lifecycle.create_release_intent

            def drift_after_intent(*args, **kwargs):
                created = original(*args, **kwargs)
                (source / "feature.txt").write_text("unexpected commit after release intent\n")
                git(source, "add", "feature.txt")
                git(source, "commit", "-m", "fixture identity drift")
                return created

            monkeypatch.setattr(lifecycle, "create_release_intent", drift_after_intent)
        # Exercise the same completion function in-process so the race injection is scoped.
        from worktree_controller.lifecycle import complete_publication_lease

        snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
        receipt = snapshot.publication_receipts[str(published["decision"]["receipt_id"])]
        repeated = complete_publication_lease(
            repository.repo, target=candidate,
            expected_record_version=snapshot.records[receipt.allocation_id].record_version,
            lease_id=receipt.receipt_id, published_commit=receipt.candidate_oid,
            finalizer_record_id=receipt.finalizer_record_id, run_id=receipt.run_id)
        outcome, = repeated["decision"]["source_release_trigger"]["outcomes"]
        assert outcome["allocation_id"] == allocation and source.exists()
        assert outcome["status"] == ("BLOCKED_RETRYABLE" if scenario == "retryable" else "STALE")
        assert outcome["blocker"] == ("IGNORED_CONTENT" if scenario == "retryable" else "RELEASE_INTENT_STALE")
        assert "release_receipt_id" not in outcome and outcome["recovery_commands"]
        if scenario == "retryable":
            assert (source / ".venv/pyvenv.cfg").exists()
        else:
            again, = _repeat_publication(repository, candidate, published)["outcomes"]
            assert again["status"] == "STALE" and again["intent_id"] == outcome["intent_id"]
            assert "release_receipt_id" not in again and source.exists()


def test_plain_worktree_official_dry_run_and_guarded_release_retain_refs() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        source, allocation, head, candidate, _published = _publish_source(
            repository,
            source_task="plain-source",
            integration_task="plain-integration",
        )
        assert not candidate.exists()
        intent = _release_intent_for_allocation(repository, allocation)
        store = MetadataStore(inventory(repository.repo).common_dir)
        before_plan_count = len(store.load().release_plans)
        before_receipt_count = len(store.load().release_receipts)
        before_main = git(repository.repo, "rev-parse", "main")
        before_origin = git(repository.repo, "rev-parse", "refs/remotes/origin/main")
        before_slots = status(repository.repo)["repository"]["worktree_count"]

        dry_run = release(
            repository.repo,
            target=source,
            expected_record_version=2,
            owner_release=None,
            dry_run=True,
            release_intent_id=intent.intent_id,
        )
        assert dry_run["decision"]["code"] == "RELEASE_PLAN_READY"
        assert dry_run["decision"]["plan"]["resource_actions"] == ()
        assert source.exists()
        assert len(store.load().release_plans) == before_plan_count
        assert len(store.load().release_receipts) == before_receipt_count

        released = release(
            repository.repo,
            target=source,
            expected_record_version=2,
            owner_release=None,
            release_intent_id=intent.intent_id,
        )
        assert released["decision"]["code"] == "RELEASED"
        assert not source.exists()
        assert status(repository.repo)["repository"]["worktree_count"] == before_slots - 1
        assert git(repository.repo, "rev-parse", "refs/heads/feature/plain-source") == head
        assert git(repository.repo, "rev-parse", "main") == before_main
        assert git(repository.repo, "rev-parse", "refs/remotes/origin/main") == before_origin


def test_registered_source_venv_is_torn_down_by_automatic_release() -> None:
    with GitRepository(linked=0, policy_text=POLICY_AUTO) as repository:
        _commit_reproducible_project(repository)
        adopt_canonical(repository)
        _configure_remote(repository)
        source, acquired = acquire_one(
            repository,
            task="venv-source",
            authority="venv-source",
            scope_paths=("feature.txt",),
        )
        allocation = str(acquired["decision"]["allocation_id"])
        venv = _create_reproducible_venv(source)
        register_disposable(
            repository.repo,
            target=source,
            expected_record_version=1,
            owner="module-owner",
            resource_path=".venv",
            kind="python-venv",
            reproducible=True,
            creator="test-fixture",
            teardown_policy="on-release",
        )
        (source / "feature.txt").write_text("venv source\n", encoding="utf-8")
        git(source, "add", "feature.txt")
        git(source, "commit", "-m", "add venv source")
        handoff_one(repository, source, expected=2)
        source_head = git(source, "rev-parse", "HEAD")

        queued = _enqueue(
            repository,
            task="venv-integration",
            source_allocation=allocation,
        )
        candidate, _claimed = _claim(
            repository,
            task="venv-integration",
            intent_id=str(queued["decision"]["intent_id"]),
        )
        _validate(repository, candidate, "validation-venv-source")
        _publish(repository, candidate)

        assert not source.exists()
        assert not venv.exists()
        assert git(repository.repo, "rev-parse", "refs/heads/feature/venv-source") == source_head
        snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
        source_receipt = next(
            receipt
            for receipt in snapshot.release_receipts.values()
            if receipt.allocation_id == allocation
        )
        plan = snapshot.release_plans[source_receipt.plan_id]
        assert len(plan.resource_actions) == 1
        assert plan.resource_actions[0]["path"] == ".venv"
        assert source_receipt.resource_receipt_ids


def test_equivalent_replay_advances_candidate_identity_and_release_evidence() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        _source_path, allocation, source_head = _source(
            repository,
            task="replay-source",
            relative_path="feature.txt",
            content="equivalent content\n",
        )
        queued = _enqueue(
            repository,
            task="replay-integration",
            source_allocation=allocation,
        )
        integration_intent_id = str(queued["decision"]["intent_id"])
        candidate, _claimed = _claim(
            repository,
            task="replay-integration",
            intent_id=integration_intent_id,
        )
        store = MetadataStore(inventory(repository.repo).common_dir)
        initial_intent = store.load().integration_intents[integration_intent_id]
        assert initial_intent.base_main_oid is not None
        git(candidate, "checkout", "--detach", initial_intent.base_main_oid)
        (candidate / "feature.txt").write_text("equivalent content\n", encoding="utf-8")
        git(candidate, "add", "feature.txt")
        git(candidate, "commit", "-m", "equivalent replay")
        replay_head = git(candidate, "rev-parse", "HEAD")
        git(repository.repo, "branch", "-f", "integration/replay-integration", replay_head)
        git(candidate, "checkout", "integration/replay-integration")

        recorded = record_equivalent_replay(
            repository.repo,
            target=candidate,
            expected_record_version=1,
            owner="integration-owner",
            source_allocation_id=allocation,
            source_handoff=source_head,
            replay_commit=replay_head,
            evidence_id="equivalent-review-1",
        )
        assert recorded["decision"]["record_version"] == 2
        advanced = store.load().integration_intents[integration_intent_id]
        assert advanced.candidate_oid == replay_head
        assert advanced.revision == 2
        _validate(
            repository,
            candidate,
            "validation-equivalent-replay",
            expected_record_version=2,
        )
        _publish(repository, candidate, expected_record_version=3)

        receipt = next(iter(store.load().publication_receipts.values()))
        assert receipt.source_results == (
            {
                "source_allocation_id": allocation,
                "source_handoff_oid": source_head,
                "mode": "equivalent-replay",
                "absorbed_oid": replay_head,
                "evidence_id": "equivalent-review-1",
            },
        )


def test_runtime_record_and_head_changes_fail_closed_after_dry_run() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        source, allocation, _head, _candidate, _published = _publish_source(
            repository,
            source_task="drift-source",
            integration_task="drift-integration",
        )
        first_intent = _release_intent_for_allocation(repository, allocation)
        release(
            repository.repo,
            target=source,
            expected_record_version=2,
            owner_release=None,
            dry_run=True,
            release_intent_id=first_intent.intent_id,
        )

        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=source,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    if Path(f"/proc/{process.pid}/cwd").resolve() == source.resolve():
                        break
                except OSError:
                    pass
                time.sleep(0.02)
            with pytest.raises(ControllerError) as runtime_blocked:
                release(
                    repository.repo,
                    target=source,
                    expected_record_version=2,
                    owner_release=None,
                    release_intent_id=first_intent.intent_id,
                )
            assert runtime_blocked.value.code == "RUNTIME_REFERENCE"
        finally:
            process.terminate()
            process.wait(timeout=5)

        release(
            repository.repo,
            target=source,
            expected_record_version=2,
            owner_release=None,
            dry_run=True,
            release_intent_id=first_intent.intent_id,
        )
        review(
            repository.repo,
            target=source,
            expected_record_version=2,
            owner="module-owner",
        )
        with pytest.raises(ControllerError) as cas_blocked:
            release(
                repository.repo,
                target=source,
                expected_record_version=2,
                owner_release=None,
                release_intent_id=first_intent.intent_id,
            )
        assert cas_blocked.value.code == "CAS_MISMATCH"

        refreshed = create_release_intent(
            repository.repo,
            target=source,
            expected_record_version=3,
            owner="module-owner",
            trigger="manual",
        )
        second_intent_id = str(refreshed["decision"]["intent_id"])
        release(
            repository.repo,
            target=source,
            expected_record_version=3,
            owner_release=None,
            dry_run=True,
            release_intent_id=second_intent_id,
        )
        git(source, "commit", "--allow-empty", "-m", "external post-handoff drift")
        with pytest.raises(ControllerError) as head_blocked:
            release(
                repository.repo,
                target=source,
                expected_record_version=3,
                owner_release=None,
                release_intent_id=second_intent_id,
            )
        assert head_blocked.value.code in {
            "TASK_HEAD_MISMATCH",
            "RELEASE_INTENT_STALE",
        }


def test_handoff_write_fence_reopen_and_preserved_or_unintegrated_exclusions() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        source, allocation, _head, _candidate, _published = _publish_source(
            repository,
            source_task="reopen-source",
            integration_task="reopen-integration",
        )
        intent = _release_intent_for_allocation(repository, allocation)
        plan = build_release_plan(
            repository.repo,
            target=source,
            expected_record_version=2,
            owner_release=None,
            release_intent_id=intent.intent_id,
        )
        fenced = guard_write(
            repository.repo,
            target=source,
            task_key="reopen-source",
            authority_key="reopen-source",
            owner="module-owner",
            operation="feature-write",
        )
        assert fenced["decision"]["code"] == "BLOCK_FEATURE_WRITE_LIFECYCLE"
        reopened = reopen(
            repository.repo,
            target=source,
            expected_record_version=2,
            owner="module-owner",
        )
        assert reopened["decision"]["lifecycle"] == "ACTIVE"
        snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
        assert snapshot.release_intents[intent.intent_id].status == "CANCELLED"
        holder = describe_holder(os.getpid(), client_id="manual")
        acquire_writer_lease(
            repository.repo, target=source, client_id="manual", holder_pid=holder.pid
        )
        allowed = guard_write(
            repository.repo,
            holder=holder,
            target=source,
            task_key="reopen-source",
            authority_key="reopen-source",
            owner="module-owner",
            operation="feature-write",
        )
        assert allowed["decision"]["code"] == "WRITE_ALLOWED"
        with pytest.raises(ControllerError) as stale_plan:
            release(
                repository.repo,
                target=source,
                expected_record_version=plan.expected_record_version,
                owner_release=None,
                release_intent_id=intent.intent_id,
            )
        assert stale_plan.value.code == "CAS_MISMATCH"

        unintegrated, _payload = acquire_one(
            repository,
            task="unintegrated-source",
            authority="unintegrated-source",
        )
        handoff_one(repository, unintegrated)
        blocked = create_release_intent(
            repository.repo,
            target=unintegrated,
            expected_record_version=2,
            owner="module-owner",
            trigger="manual",
        )
        assert blocked["decision"]["status"] == "BLOCKED_RETRYABLE"
        assert blocked["decision"]["blocker"] == "MISSING_INTEGRATION_EVIDENCE"

        preserved, _payload = acquire_one(
            repository,
            task="preserved-source",
            authority="preserved-source",
        )
        handoff_one(repository, preserved, kind="preserved")
        with pytest.raises(ControllerError) as preserved_blocked:
            create_release_intent(
                repository.repo,
                target=preserved,
                expected_record_version=2,
                owner="module-owner",
                trigger="manual",
            )
        assert preserved_blocked.value.code == "INVALID_LIFECYCLE"


def test_capacity_pressure_retries_only_existing_intents_after_binding_clears() -> None:
    with GitRepository(linked=0, policy_text=POLICY_AUTO) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        source, acquired = acquire_one(
            repository,
            task="bound-source",
            authority="bound-source",
            scope_paths=("bound.txt",),
            external_bindings=("agent-workspace:run:run-1",),
        )
        allocation = str(acquired["decision"]["allocation_id"])
        (source / "bound.txt").write_text("bound\n", encoding="utf-8")
        git(source, "add", "bound.txt")
        git(source, "commit", "-m", "add bound source")
        handoff_one(repository, source)
        untouched, untouched_payload = acquire_one(
            repository,
            task="untouched-source",
            authority="untouched-source",
        )
        untouched_allocation = str(untouched_payload["decision"]["allocation_id"])
        handoff_one(repository, untouched)

        queued = _enqueue(
            repository,
            task="bound-integration",
            source_allocation=allocation,
        )
        candidate, _claimed = _claim(
            repository,
            task="bound-integration",
            intent_id=str(queued["decision"]["intent_id"]),
        )
        before = MetadataStore(inventory(repository.repo).common_dir).load()
        repeated_path, repeated = _claim(
            repository,
            task="bound-integration",
            intent_id=str(queued["decision"]["intent_id"]),
        )
        assert repeated_path == candidate
        assert repeated["decision"]["allocation_id"] == _claimed["decision"]["allocation_id"]
        assert MetadataStore(inventory(repository.repo).common_dir).load() == before
        _validate(repository, candidate, "validation-bound")
        _publish(repository, candidate)
        first_intent = _release_intent_for_allocation(repository, allocation)
        assert first_intent.status == "BLOCKED_RETRYABLE"
        assert first_intent.blocker == "EXTERNAL_BINDING"
        assert source.exists()

        removed = update_external_binding(
            repository.repo,
            target=source,
            expected_record_version=2,
            owner="module-owner",
            binding="agent-workspace:run:run-1",
            action="remove",
        )
        assert removed["decision"]["record_version"] == 3
        created = create_release_intent(
            repository.repo,
            target=source,
            expected_record_version=3,
            owner="module-owner",
            trigger="task-terminal",
        )
        second_intent_id = str(created["decision"]["intent_id"])
        reconciled = reconcile_release_intents(
            repository.repo,
            actor="agent-workspace",
            trigger="capacity-pressure",
        )
        outcomes = {outcome["intent_id"]: outcome for outcome in reconciled["decision"]["outcomes"]}
        assert outcomes[second_intent_id]["result"] == "RELEASED"
        assert not source.exists()
        assert untouched.exists()
        snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
        assert not any(
            intent.allocation_id == untouched_allocation
            for intent in snapshot.release_intents.values()
        )


@pytest.mark.parametrize("mismatch", [None, "allocation", "record-version", "publication", "missing-plan"])
def test_doctor_requires_exact_release_evidence(mismatch, monkeypatch) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        _publish_source(repository, source_task="proof-source", integration_task="proof-integration")
        store = MetadataStore(inventory(repository.repo).common_dir)
        snapshot = store.load()
        publication = next(iter(snapshot.publication_receipts.values()))
        released = next(r for r in snapshot.release_receipts.values()
                        if r.allocation_id == publication.allocation_id)
        assert doctor(repository.repo)["decision"]["code"] == "PASS"
        different_id = "00000000-0000-0000-0000-000000000001"
        if mismatch == "allocation":
            snapshot.release_receipts[released.receipt_id] = replace(released, allocation_id=different_id)
        elif mismatch == "record-version":
            snapshot.release_receipts[released.receipt_id] = replace(
                released, released_record_version=released.released_record_version + 1
            )
        elif mismatch == "publication":
            snapshot.release_plans[released.plan_id] = replace(
                snapshot.release_plans[released.plan_id], publication_receipt_id=different_id
            )
        elif mismatch == "missing-plan":
            snapshot.release_plans.pop(released.plan_id)
        monkeypatch.setattr(MetadataStore, "_load_unlocked", lambda self: snapshot)
        result = doctor(repository.repo)
        drift = [w for w in result["warnings"] if w["code"] == "PUBLICATION_RECEIPT_DRIFT"
                 and w.get("receipt_id") == publication.receipt_id]
        assert not result["errors"]
        assert result["decision"]["code"] == ("DEGRADED" if mismatch else "PASS")
        assert len(drift) == (1 if mismatch else 0)
        if drift:
            assert drift[0]["severity"] == "degraded"


def test_publication_receipt_reconciliation_is_immutable_and_idempotent() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        _source_path, _allocation, _head, _candidate, _published = _publish_source(
            repository,
            source_task="receipt-source",
            integration_task="receipt-integration",
        )
        store = MetadataStore(inventory(repository.repo).common_dir)
        snapshot = store.load()
        publication = next(iter(snapshot.publication_receipts.values()))
        publication_path = store.publication_receipts_dir / f"{publication.receipt_id}.json"
        immutable_bytes = publication_path.read_bytes()
        for path in store.release_plans_dir.glob("*.json"):
            path.unlink()
        for path in store.release_receipts_dir.glob("*.json"):
            path.unlink()

        before = doctor(repository.repo)
        assert before["decision"]["code"] == "DEGRADED"
        assert "PUBLICATION_RECEIPT_DRIFT" in {warning["code"] for warning in before["warnings"]}
        dry_run = reconcile_publication_receipts(
            repository.repo,
            actor="test-reconciler",
            dry_run=True,
        )
        assert dry_run["decision"]["plans"][0]["already_reconciled"] is False
        assert not store.reconciliation_receipts_dir.exists()
        assert publication_path.read_bytes() == immutable_bytes

        first = reconcile_publication_receipts(
            repository.repo,
            actor="test-reconciler",
        )
        assert len(first["decision"]["written_receipt_ids"]) == 1
        second = reconcile_publication_receipts(
            repository.repo,
            actor="test-reconciler",
        )
        assert second["decision"]["written_receipt_ids"] == []
        assert second["decision"]["plans"][0]["already_reconciled"] is True
        assert publication_path.read_bytes() == immutable_bytes
        after = doctor(repository.repo)
        assert after["decision"]["code"] == "PASS"
        reconciled_warning = next(
            warning
            for warning in after["warnings"]
            if warning["code"] == "PUBLICATION_RECEIPT_RECONCILED"
        )
        assert reconciled_warning["severity"] == "informational"


def test_legacy_v2_publication_receipt_reconciliation_preserves_intent() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        _source_path, _allocation, _head, _candidate, _published = _publish_source(
            repository,
            source_task="legacy-receipt-source",
            integration_task="legacy-receipt-integration",
        )
        store = MetadataStore(inventory(repository.repo).common_dir)
        snapshot = store.load()
        publication = next(iter(snapshot.publication_receipts.values()))
        assert publication.intent_id is not None
        for path in store.release_plans_dir.glob("*.json"):
            path.unlink()
        for path in store.release_receipts_dir.glob("*.json"):
            path.unlink()

        intent_path = store.integration_intents_dir / f"{publication.intent_id}.json"
        raw = json.loads(intent_path.read_text(encoding="utf-8"))
        for field in {
            "contract_version",
            "source_branches",
            "source_finalizer_record_ids",
            "source_snapshot_ids",
            "scope_digest",
            "queue_sequence",
            "attempt",
            "prepare_identity",
            "publication_identity",
            "recovery_identity",
            "prepared_receipt_id",
            "prepared_base_origin_main_oid",
            "prepared_commit_oid",
            "prepared_tree_oid",
            "prepared_scope_digest",
            "tests_receipt_id",
            "snapshot_receipt_id",
        }:
            raw.pop(field)
        raw["schema_version"] = 2
        raw["state"] = "PUBLISHED"
        raw["released_at"] = None
        intent_path.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
        legacy_bytes = intent_path.read_bytes()

        before = doctor(repository.repo)
        assert before["decision"]["code"] == "DEGRADED"
        assert "PUBLICATION_RECEIPT_DRIFT" in {
            warning["code"] for warning in before["warnings"]
        }
        planned = reconcile_publication_receipts(
            repository.repo,
            actor="legacy-receipt-reconciler",
            receipt_ids=(publication.receipt_id,),
            dry_run=True,
        )
        assert planned["decision"]["plans"][0]["differences"] == [
            "publication_receipt.status=REMOTE_VERIFIED",
            "integration_intent.state=REMOTE_VERIFIED",
            "integration_intent.schema_version=2",
        ]

        first = reconcile_publication_receipts(
            repository.repo,
            actor="legacy-receipt-reconciler",
            receipt_ids=(publication.receipt_id,),
        )
        second = reconcile_publication_receipts(
            repository.repo,
            actor="legacy-receipt-reconciler",
            receipt_ids=(publication.receipt_id,),
        )
        assert len(first["decision"]["written_receipt_ids"]) == 1
        assert second["decision"]["written_receipt_ids"] == []
        assert second["decision"]["plans"][0]["already_reconciled"] is True
        assert intent_path.read_bytes() == legacy_bytes
        reconciliations = store.load().reconciliation_receipts
        assert len(reconciliations) == 1
        reconciliation = next(iter(reconciliations.values()))
        assert reconciliation.recovered_intent_id == publication.intent_id
        assert reconciliation.recovered_intent_state == "RELEASED"

        after = doctor(repository.repo)
        assert after["decision"]["code"] == "PASS"
        assert "PUBLICATION_RECEIPT_DRIFT" not in {
            warning["code"] for warning in after["warnings"]
        }
        reconciled_warning = next(
            warning
            for warning in after["warnings"]
            if warning["code"] == "PUBLICATION_RECEIPT_RECONCILED"
        )
        assert reconciled_warning["severity"] == "informational"


def test_release_after_worktree_removal_recovers_and_repeats_idempotently() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, _payload = acquire_one(repository)
        handoff_one(repository, target)
        branch_head = git(repository.repo, "rev-parse", "refs/heads/feature/new-task")
        crashed = _cli(
            _release_arguments(repository, target),
            crash_at="release_worktree_removed",
        )
        assert crashed.returncode == 97
        assert not target.exists()
        recovered = release(
            repository.repo,
            target=target,
            expected_record_version=2,
            owner_release="module-owner",
        )
        assert recovered["decision"]["code"] == "RELEASE_RECOVERED"
        repeated = release(
            repository.repo,
            target=target,
            expected_record_version=2,
            owner_release="module-owner",
        )
        assert repeated["decision"]["code"] == "ALREADY_RELEASED"
        assert git(repository.repo, "rev-parse", "refs/heads/feature/new-task") == branch_head
        assert status(repository.repo)["repository"]["worktree_count"] == 1


def test_expired_intent_is_staled_before_replacement() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        source, allocation, _head, _candidate, _published = _publish_source(
            repository,
            source_task="expiry-source",
            integration_task="expiry-integration",
        )
        store = MetadataStore(inventory(repository.repo).common_dir)
        first = _release_intent_for_allocation(repository, allocation)
        expired_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="seconds")
        store.replace_release_intent(replace(first, expires_at=expired_at))
        replacement = create_release_intent(
            repository.repo,
            target=source,
            expected_record_version=2,
            owner="module-owner",
            trigger="manual",
        )
        assert replacement["decision"]["code"] == "RELEASE_INTENT_CREATED"
        assert replacement["decision"]["intent_id"] != first.intent_id
        snapshot = store.load()
        assert snapshot.release_intents[first.intent_id].status == "STALE"
        assert snapshot.release_intents[first.intent_id].blocker == "RELEASE_INTENT_EXPIRED"


def test_periodic_reconcile_renews_stale_intent_with_fresh_checks() -> None:
    from worktree_controller.resource_admission import reconcile as reconcile_admission

    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        source, allocation, _head, _candidate, _published = _publish_source(
            repository, source_task="renew-source", integration_task="renew-integration")
        store = MetadataStore(inventory(repository.repo).common_dir)
        first = _release_intent_for_allocation(repository, allocation)
        store.replace_release_intent(replace(first,
            expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="seconds")))
        (repository.repo / ".agents/worktree-policy.toml").write_text(POLICY_AUTO)
        reconcile_release_intents(repository.repo, actor="fixture", trigger="task-terminal")
        stale = store.load().release_intents[first.intent_id]
        assert stale.status == "STALE"
        result = reconcile_admission(apply=True)
        # No AW workspace registration or original notification is needed: the
        # existing host request identifies the repository and its formal intent.
        release_result = result["decision"]["maintenance"][0]["release"]
        assert release_result["renewed_intents"][0]["previous_intent_id"] == first.intent_id
        snapshot = store.load()
        assert snapshot.release_intents[first.intent_id] == stale
        assert snapshot.records[allocation].lifecycle == "RELEASED"
        assert not source.exists()


@pytest.mark.parametrize("scenario", ["clean", "unknown", "no-action"])
def test_automatic_release_uses_typed_cleanup_and_preserves_unknown_content(scenario, monkeypatch):
    from tests.test_typed_disposable import empty_runtime_evidence
    from worktree_controller import lifecycle, transaction_scope

    # Unit cleanup evidence must not depend on the caller's live procfs population.
    monkeypatch.setattr(lifecycle, "runtime_authority_evidence", lambda *args, **kwargs: empty_runtime_evidence())
    monkeypatch.setattr(lifecycle, "runtime_references", lambda *args, **kwargs: ())
    monkeypatch.setattr(lifecycle, "editable_references", lambda *args, **kwargs: ())
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        frontend = repository.repo / "frontend"
        frontend.mkdir()
        package = {"name": "fixture", "scripts": {"build": "vite build"},
                   "devDependencies": {"vite": "1.0.0"}}
        (frontend / "package.json").write_text(json.dumps(package))
        (frontend / "package-lock.json").write_text(json.dumps({"name": "fixture",
            "lockfileVersion": 3, "packages": {"": {"name": "fixture", "devDependencies": {"vite": "1.0.0"}}}}))
        (frontend / "index.html").write_text("<html></html>")
        (repository.repo / ".gitignore").write_text("frontend/dist/\nunknown/\n")
        git(repository.repo, "add", "frontend", ".gitignore")
        git(repository.repo, "commit", "-m", "fixture build contract")
        adopt_canonical(repository)
        _configure_remote(repository)
        source, allocation, _head, _candidate, _published = _publish_source(
            repository, source_task="typed-source", integration_task="typed-integration")
        output = source / "frontend/dist"
        (output / "assets").mkdir(parents=True)
        (output / "index.html").write_text("<html></html>")
        (output / "assets/main.js").write_text("/* fixture build */")
        if scenario == "unknown":
            (source / "unknown").mkdir()
            (source / "unknown/persistent.txt").write_text("preserve this")
        (repository.repo / ".agents/worktree-policy.toml").write_text(POLICY_AUTO)
        if scenario == "no-action":
            original_write = MetadataStore.write_transaction
            def interrupted_write(self, transaction, value):
                original_write(self, transaction, value)
                if value.get("command") == "resource-clean":
                    raise KeyboardInterrupt
            with monkeypatch.context() as patch:
                patch.setattr(MetadataStore, "write_transaction", interrupted_write)
                with pytest.raises(KeyboardInterrupt):
                    reconcile_release_intents(repository.repo, actor="fixture", trigger="task-terminal")
            store = MetadataStore(inventory(repository.repo).common_dir)
            journal = next(path for path in store.transactions_dir.glob("*.json")
                           if json.loads(path.read_text()).get("command") == "resource-clean")
            journal_bytes = journal.read_bytes()
        result = reconcile_release_intents(repository.repo, actor="fixture", trigger="task-terminal")
        store = MetadataStore(inventory(repository.repo).common_dir)
        if scenario == "unknown":
            assert source.exists() and (output / "assets/main.js").exists()
            assert (source / "unknown/persistent.txt").read_text() == "preserve this"
            assert result["decision"]["outcomes"][0]["blocker"] == "IGNORED_CONTENT"
        else:
            assert result["decision"]["outcomes"][0]["result"] == "RELEASED", result["decision"]
            assert store.load().records[allocation].lifecycle == "RELEASED"
            receipts = [json.loads(path.read_text()) for path in store.completion_receipts_dir.glob("*.json")]
            assert any(receipt.get("result") == "CLEANED" and receipt.get("release_intent_id")
                       for receipt in receipts)
            assert not source.exists()
            if scenario == "no-action":
                assert journal.read_bytes() == journal_bytes
                with store.lock(exclusive=False):
                    assert (
                        transaction_scope.terminal_state(store, journal.stem)
                        == "TYPED_ATTEMPT_SUPERSEDED"
                    )


def test_disposable_contract_cannot_be_added_after_handoff() -> None:
    with GitRepository(linked=0) as repository:
        _commit_reproducible_project(repository)
        adopt_canonical(repository)
        target, _payload = acquire_one(repository)
        venv = _create_reproducible_venv(target)
        handoff_one(repository, target)
        with pytest.raises(ControllerError) as blocked:
            register_disposable(
                repository.repo,
                target=target,
                expected_record_version=2,
                owner="module-owner",
                resource_path=".venv",
                kind="python-venv",
                reproducible=True,
                creator="late-writer",
                teardown_policy="on-release",
            )
        assert blocked.value.code == "INVALID_LIFECYCLE"
        assert venv.is_dir()


def test_legacy_disposable_resource_migration_is_fail_closed() -> None:
    with GitRepository(linked=0) as repository:
        _commit_reproducible_project(repository)
        adopt_canonical(repository)
        target, acquired = acquire_one(repository)
        allocation_id = str(acquired["decision"]["allocation_id"])
        venv = _create_reproducible_venv(target)
        register_disposable(
            repository.repo,
            target=target,
            expected_record_version=1,
            owner="module-owner",
            resource_path=".venv",
            kind="python-venv",
            reproducible=True,
            creator="test-fixture",
            teardown_policy="on-release",
        )
        store = MetadataStore(inventory(repository.repo).common_dir)
        record_path = store.records_dir / f"{allocation_id}.json"
        legacy = json.loads(record_path.read_text(encoding="utf-8"))
        legacy["schema_version"] = 2
        for resource in legacy["disposable_resources"]:
            resource.pop("reproducible")
            resource.pop("creator")
            resource.pop("teardown_policy")
        record_path.write_text(json.dumps(legacy), encoding="utf-8")

        migrated_resource = store.load().records[allocation_id].disposable_resources[0]
        assert migrated_resource["reproducible"] is False
        assert migrated_resource["creator"] == "controller-v2-migration"
        assert migrated_resource["teardown_policy"] == "manual"
        handoff_one(repository, target, expected=2)
        with pytest.raises(ControllerError) as blocked:
            release(
                repository.repo,
                target=target,
                expected_record_version=3,
                owner_release="module-owner",
            )
        assert blocked.value.code == "DISPOSABLE_RESOURCE_PRESENT"
        assert venv.is_dir()


def test_persisted_release_plan_content_hash_is_verified() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, _acquired = acquire_one(repository)
        handoff_one(repository, target)
        release(
            repository.repo,
            target=target,
            expected_record_version=2,
            owner_release="module-owner",
        )
        store = MetadataStore(inventory(repository.repo).common_dir)
        snapshot = store.load()
        assert len(snapshot.release_plans) == 1
        plan_id = next(iter(snapshot.release_plans))
        plan_path = store.release_plans_dir / f"{plan_id}.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["trigger"] = "tampered-trigger"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

        with pytest.raises(ControllerError) as corrupted:
            store.load()
        assert corrupted.value.code == "INVALID_METADATA"
        assert "content hash" in str(corrupted.value)


def test_manual_candidate_release_acknowledges_remote_verified_trigger() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        source, allocation, _source_head = _source(
            repository,
            task="manual-release-source",
            relative_path="manual.txt",
            content="manual\n",
        )
        queued = _enqueue(
            repository,
            task="manual-release-integration",
            source_allocation=allocation,
        )
        integration_intent_id = str(queued["decision"]["intent_id"])
        candidate, _claimed = _claim(
            repository,
            task="manual-release-integration",
            intent_id=integration_intent_id,
        )
        _validate(repository, candidate, "validation-manual-release")
        completed = _publish(repository, candidate, auto_release=False)
        publication_receipt_id = str(completed["decision"]["receipt_id"])
        assert not MetadataStore(inventory(repository.repo).common_dir).load().release_intents

        released = release(
            repository.repo,
            target=candidate,
            expected_record_version=4,
            owner_release=None,
            publication_receipt_id=publication_receipt_id,
        )
        assert released["decision"]["code"] == "RELEASED"
        snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
        assert snapshot.integration_intents[integration_intent_id].state == "RELEASED"
        current = _release_intent_for_allocation(repository, allocation)
        assert current.status == "PENDING"
        assert current.blocker is None
        assert source.exists()


# --------------------------------------------------------------------------- #
# An environment the task never classified must not end the release attempt
# --------------------------------------------------------------------------- #


def _latest_release_intent(repository: GitRepository, allocation_id: str):
    """The newest intent for one allocation; a blocked task accumulates several."""

    snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
    matches = [
        intent
        for intent in snapshot.release_intents.values()
        if intent.allocation_id == allocation_id
    ]
    assert matches
    return max(matches, key=lambda intent: (intent.created_at, intent.intent_id))


def _published_source_with_environment(repository: GitRepository, *, name: str):
    """Acquire, create the environment the task really uses, hand off, publish."""

    source, acquired = acquire_one(
        repository, task=f"{name}-source", authority=f"{name}-source", scope_paths=("feature.txt",)
    )
    allocation = str(acquired["decision"]["allocation_id"])
    venv = _create_reproducible_venv(source)
    (source / "feature.txt").write_text(f"{name}\n", encoding="utf-8")
    git(source, "add", "feature.txt")
    git(source, "commit", "-m", f"add {name}")
    handoff_one(repository, source)
    queued = _enqueue(repository, task=f"{name}-integration", source_allocation=allocation)
    candidate, _claimed = _claim(
        repository,
        task=f"{name}-integration",
        intent_id=str(queued["decision"]["intent_id"]),
    )
    _validate(repository, candidate, f"validation-{name}")
    _publish(repository, candidate)
    return source, allocation, venv


def test_unclassified_environment_keeps_the_release_intent_retryable() -> None:
    """Unknown content stops the release; it must not discard the pending evidence."""

    with GitRepository(linked=0, policy_text=POLICY_AUTO) as repository:
        _commit_reproducible_project(repository)
        adopt_canonical(repository)
        _configure_remote(repository)
        source, allocation, venv = _published_source_with_environment(
            repository, name="unclassified"
        )

        intent = _latest_release_intent(repository, allocation)
        assert intent.status == "BLOCKED_RETRYABLE"
        assert intent.blocker == "IGNORED_CONTENT"
        assert source.exists()
        assert venv.is_dir()

        # A later trigger re-evaluates the same intent instead of skipping it.
        again = reconcile_release_intents(
            repository.repo, actor="agent-workspace", trigger="capacity-pressure"
        )
        outcome = next(
            item for item in again["decision"]["outcomes"] if item["intent_id"] == intent.intent_id
        )
        assert outcome["result"] == "BLOCKED_RETRYABLE"
        assert outcome["blocker"] == "IGNORED_CONTENT"


def test_classifying_the_environment_converges_the_pending_intent() -> None:
    """The owner classifies what the task created, and the existing intent finishes."""

    with GitRepository(linked=0, policy_text=POLICY_AUTO) as repository:
        _commit_reproducible_project(repository)
        adopt_canonical(repository)
        _configure_remote(repository)
        source, allocation, venv = _published_source_with_environment(repository, name="classify")
        blocked = _latest_release_intent(repository, allocation)
        assert blocked.status == "BLOCKED_RETRYABLE"

        snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
        register_disposable(
            repository.repo,
            target=source,
            expected_record_version=snapshot.records[allocation].record_version,
            owner="module-owner",
            resource_path=".venv",
            kind="python-venv",
            reproducible=True,
            creator="uv sync",
            teardown_policy="on-release",
        )

        reconciled = reconcile_release_intents(
            repository.repo, actor="agent-workspace", trigger="capacity-pressure"
        )
        outcome = next(
            item
            for item in reconciled["decision"]["outcomes"]
            if item["intent_id"] == blocked.intent_id
        )
        assert outcome["result"] == "RELEASED"
        assert not source.exists()
        assert not venv.exists()


def test_publication_survives_a_cleanup_that_cannot_finish() -> None:
    """A blocked candidate cleanup must not read as a failed publication."""

    with GitRepository(linked=0, policy_text=POLICY_AUTO) as repository:
        _commit_reproducible_project(repository)
        adopt_canonical(repository)
        _configure_remote(repository)
        source, acquired = acquire_one(
            repository,
            task="pending-source",
            authority="pending-source",
            scope_paths=("feature.txt",),
        )
        allocation = str(acquired["decision"]["allocation_id"])
        (source / "feature.txt").write_text("pending\n", encoding="utf-8")
        git(source, "add", "feature.txt")
        git(source, "commit", "-m", "add pending")
        handoff_one(repository, source)
        queued = _enqueue(repository, task="pending-integration", source_allocation=allocation)
        candidate, _claimed = _claim(
            repository,
            task="pending-integration",
            intent_id=str(queued["decision"]["intent_id"]),
        )
        _validate(repository, candidate, "validation-pending")
        # The candidate checkout carries content nobody classified.
        _create_reproducible_venv(candidate)

        published = _publish(repository, candidate)
        decision = published["decision"]

        # Publication is recorded and reported, separately from the pending cleanup.
        assert decision["code"] == "PUBLICATION_COMPLETED"
        assert decision["cleanup"]["result"] == "CLEANUP_PENDING"
        assert decision["cleanup"]["blocker"] == "IGNORED_CONTENT"
        snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
        assert decision["receipt_id"] in snapshot.publication_receipts
        # The source trigger still ran and reported why it is waiting, rather than
        # being lost with the raised error.
        assert [item["reason"] for item in decision["source_release_trigger"]["skipped"]] == [
            "INTEGRATION_NOT_RELEASED"
        ]
        outcome, = decision["source_release_trigger"]["outcomes"]
        assert outcome["allocation_id"] == allocation and outcome["status"] == "WAITING"
        assert outcome["blocker"] == "INTEGRATION_NOT_RELEASED"
        assert outcome["recovery_commands"] == ["reconcile receipts --dry-run"]

        # Classifying the candidate content lets the same publication finish.
        register_disposable(
            repository.repo,
            target=candidate,
            expected_record_version=snapshot.records[str(decision["allocation_id"])].record_version,
            owner=snapshot.records[str(decision["allocation_id"])].owner,
            resource_path=".venv",
            kind="python-venv",
            reproducible=True,
            creator="uv sync",
            teardown_policy="on-release",
        )
        snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
        release(
            repository.repo,
            target=candidate,
            expected_record_version=snapshot.records[str(decision["allocation_id"])].record_version,
            owner_release=snapshot.records[str(decision["allocation_id"])].owner,
            teardown_reproducible_venv=True,
            publication_receipt_id=str(decision["receipt_id"]),
        )
        assert not candidate.exists()
        snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
        assert any(
            intent.allocation_id == allocation for intent in snapshot.release_intents.values()
        )


def test_capacity_pressure_acquire_converges_a_terminal_release_first() -> None:
    """A slot held by finished work must not refuse admission for live work."""

    with GitRepository(linked=0, policy_text=POLICY_AUTO) as repository:
        _commit_reproducible_project(repository)
        adopt_canonical(repository)
        _configure_remote(repository)
        finished, allocation, venv = _published_source_with_environment(repository, name="finished")
        blocked = _latest_release_intent(repository, allocation)
        assert blocked.status == "BLOCKED_RETRYABLE"
        assert finished.exists()

        # The owner classifies what the task created; nothing releases it yet.
        snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
        register_disposable(
            repository.repo,
            target=finished,
            expected_record_version=snapshot.records[allocation].record_version,
            owner="module-owner",
            resource_path=".venv",
            kind="python-venv",
            reproducible=True,
            creator="uv sync",
            teardown_policy="on-release",
        )
        assert finished.exists()

        # Old quantity counts cannot force cleanup of unrelated work.
        occupied, _payload = acquire_one(repository, task="occupant", authority="occupant")
        second, _second_payload = acquire_one(
            repository, task="occupant-two", authority="occupant-two"
        )
        # Writer retention alone reclaims nothing here, so the old single-mechanism
        # pass would have refused admission with every slot nominally occupied.
        from worktree_controller.reconciler import reconcile_writers

        preview = reconcile_writers(
            repository.repo, actor="lifecycle-reconciler", dry_run=True, trigger="manual"
        )
        assert preview["decision"]["would_reclaim"] == 0

        waiting, admitted = acquire_one(repository, task="waiting", authority="waiting")
        assert finished.exists()
        reconcile_release_intents(repository.repo, actor="lifecycle-reconciler", trigger="capacity-pressure")

        assert admitted["decision"]["code"] == "ACQUIRED"
        assert waiting.exists()
        assert occupied.exists()
        assert second.exists()
        assert not finished.exists()
        assert not venv.exists()


def test_admission_keeps_live_bound_and_unverified_occupancy_untouched() -> None:

    with GitRepository(linked=0, policy_text=POLICY_AUTO) as repository:
        _commit_reproducible_project(repository)
        adopt_canonical(repository)
        _configure_remote(repository)
        bound, bound_payload = acquire_one(
            repository,
            task="bound-occupant",
            authority="bound-occupant",
            external_bindings=("agent-workspace:run:run-9",),
        )
        live, live_payload = acquire_one(
            repository, task="live-occupant", authority="live-occupant"
        )
        holder = subprocess.Popen(["sleep", "60"])
        try:
            acquire_writer_lease(
                repository.repo,
                target=live,
                owner="module-owner",
                client_id="executor-a",
                holder_pid=holder.pid,
            )
            unverified, _unverified_payload = acquire_one(
                repository, task="legacy-occupant", authority="legacy-occupant"
            )

            _, admitted = acquire_one(repository, task="waiting", authority="waiting")
            assert admitted["decision"]["code"] == "ACQUIRED"
            assert holder.poll() is None
            assert bound.exists()
            assert live.exists()
            assert unverified.exists()
        finally:
            holder.kill()
            holder.wait()


def test_repeated_renewal_of_an_unchanged_blocked_identity_backs_off() -> None:
    from worktree_controller.lifecycle import RENEWAL_BACKOFF_CAP_SECONDS, _renewal_due

    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        _source_path, allocation, head, _candidate, _published = _publish_source(
            repository, source_task="backoff-source", integration_task="backoff-integration")
        store = MetadataStore(inventory(repository.repo).common_dir)
        first = _release_intent_for_allocation(repository, allocation)
        snapshot = store.load()
        record = snapshot.records[allocation]
        now = datetime.now(UTC)

        def stale(index, age):
            return replace(first, intent_id=f"{first.intent_id[:-2]}{index:02d}", status="STALE",
                           updated_at=(now - timedelta(seconds=age)).isoformat(timespec="seconds"))

        def due(intents, *, record_version=record.record_version, observed=head):
            view = replace(snapshot, release_intents={i.intent_id: i for i in intents})
            return _renewal_due(view, intents[-1], replace(record, record_version=record_version),
                                observed, now, 3600)

        # The first expiry renews at once; repeats wait 1h, 2h, ... up to a day.
        assert due([stale(1, 0)])
        assert not due([stale(1, 9000), stale(2, 3599)])
        assert due([stale(1, 9000), stale(2, 3600)])
        assert not due([stale(1, 9000), stale(2, 9000), stale(3, 7199)])
        many = [stale(i, 0) for i in range(1, 20)]
        many[-1] = stale(19, RENEWAL_BACKOFF_CAP_SECONDS)
        assert due(many)
        # Changed work is never delayed by an earlier identity's failures.
        assert due([stale(1, 0), stale(2, 0)], record_version=record.record_version + 1)
        assert due([stale(1, 0), stale(2, 0)], observed="0" * 40)


@pytest.fixture(params=["codex", "module-owner"])
def bound_reopen_source(request):
    from worktree_controller.resource_admission import admit_execution
    from worktree_controller.session_admission import runtime_binding_value

    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        source, payload = acquire_one(
            repository,
            task="reopen-binding-source",
            owner="module-owner",
            role="feature",
        )
        (source / ".gitignore").write_text("generated/\n")
        git(source, "add", ".gitignore")
        git(source, "commit", "-m", "add preservation fixture")
        holder = describe_holder(
            os.getpid(), client_id=request.param, session_id="owner-session", run_id="owner-run"
        )
        admit_execution(
            repository.repo, target=source, owner="module-owner", client_id=holder.client_id,
            session_id=holder.session_id, run_id=holder.run_id,
        )
        acquired = acquire_writer_lease(
            repository.repo, target=source, owner="module-owner", holder_pid=holder.pid,
            client_id=holder.client_id, session_id=holder.session_id, run_id=holder.run_id,
        )
        binding = runtime_binding_value(asdict(holder), acquired["decision"]["lease_id"], "agent")
        bound = update_external_binding(
            repository.repo, target=source, expected_record_version=1, owner="module-owner",
            binding=binding, action="add",
        )
        handoff_one(repository, source, expected=bound["decision"]["record_version"])
        (source / "generated").mkdir()
        (source / "generated" / "result.txt").write_text("preserved generated evidence\n")
        allocation_id = payload["decision"]["allocation_id"]
        store = MetadataStore(inventory(repository.repo).common_dir)
        rec = store.load().records[allocation_id]
        lease = store.load().writer_leases[allocation_id]
        yield repository, source, store, rec, lease, binding


def test_reopen_accepts_exact_writer_and_preserves_work(bound_reopen_source):
    repository, source, store, rec, _lease, binding = bound_reopen_source
    head = git(source, "rev-parse", "HEAD")
    reopened = reopen(repository.repo, target=source, expected_record_version=rec.record_version,
                      owner=rec.owner)
    assert reopened["decision"]["lifecycle"] == "ACTIVE"
    assert store.load().records[rec.allocation_id].external_bindings == (binding,)
    assert git(source, "rev-parse", "HEAD") == head
    assert (source / "generated/result.txt").read_text() == "preserved generated evidence\n"
    assert (source / ".gitignore").read_text() == "generated/\n"


@pytest.mark.parametrize("drift", [
    "session", "run", "lease-id", "client", "opaque", "malformed", "missing-lease",
    "expired-lease", "lease-owner", "stale-holder",
])
def test_reopen_rejects_unproved_runtime_ownership(bound_reopen_source, drift):
    from worktree_controller.session_admission import runtime_binding_value

    repository, source, store, rec, lease, binding = bound_reopen_source
    holder = lease.holder
    if drift in {"session", "run", "client", "lease-id"}:
        fields = {"session": "session_id", "run": "run_id", "client": "client_id"}
        if drift in fields:
            holder = replace(holder, **{fields[drift]: f"other-{drift}"})
        binding = runtime_binding_value(asdict(holder), str(uuid.uuid4()) if drift == "lease-id" else lease.lease_id, "agent")
    elif drift == "opaque":
        binding = "workspace:unknown"
    elif drift == "malformed":
        binding = "runtime:process-v1:{}"
    elif drift == "missing-lease":
        store.remove_writer_lease(rec.allocation_id)
    elif drift == "expired-lease":
        store.write_writer_lease(replace(lease, expires_at="2000-01-01T00:00:00+00:00"))
    elif drift == "lease-owner":
        store.write_writer_lease(replace(lease, owner="other-owner"))
    elif drift == "stale-holder":
        store.write_writer_lease(replace(lease, holder=replace(holder, start_ticks=holder.start_ticks + 1)))
    store.replace_record(replace(rec, external_bindings=(binding,)))
    before = store.load().records[rec.allocation_id]
    with pytest.raises(ControllerError) as error:
        reopen(repository.repo, target=source, expected_record_version=rec.record_version,
               owner=rec.owner)
    assert error.value.code == "EXTERNAL_BINDING"
    assert store.load().records[rec.allocation_id] == before


def test_reopen_bound_handoff_cas_serializes_and_repeat_is_rejected(bound_reopen_source):
    repository, source, store, rec, _lease, binding = bound_reopen_source

    def attempt():
        try:
            return reopen(repository.repo, target=source, expected_record_version=rec.record_version,
                          owner=rec.owner)["decision"]["code"]
        except ControllerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: attempt(), range(2)))
    assert sorted(results) == ["CAS_MISMATCH", "REOPENED"]
    assert attempt() == "CAS_MISMATCH"
    assert store.load().records[rec.allocation_id].external_bindings == (binding,)


def test_reopen_bound_handoff_preserves_unknown_untracked_file(bound_reopen_source):
    repository, source, store, rec, _lease, _binding = bound_reopen_source
    unknown = source / "unknown.txt"
    unknown.write_text("owner evidence\n")
    with pytest.raises(ControllerError) as error:
        reopen(repository.repo, target=source, expected_record_version=rec.record_version,
               owner=rec.owner)
    assert error.value.code == "DIRTY_WORKTREE"
    assert unknown.read_text() == "owner evidence\n"
    assert store.load().records[rec.allocation_id] == rec
