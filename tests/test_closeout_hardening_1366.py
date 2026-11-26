"""Regression tests for 1.36.6 delivery closeout hardening."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_auto_release import POLICY_AUTO, POLICY_MANUAL
from tests.test_cleanup_recovery import metadata_bytes
from tests.test_controlled import (
    _commit_reproducible_project,
    _configure_remote,
    _create_reproducible_venv,
    adopt_canonical,
)
from tests.test_integration_pipeline import _claim, _enqueue, _publish, _source, _validate
from tests.test_shadow import GitRepository, git
from worktree_controller.controller import doctor
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import register_disposable, release
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore


def test_published_owner_release_without_publication_receipt_fails_closed() -> None:
    """Releasing a PUBLISHED candidate without --publication-receipt-id must fail closed."""
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        _source_path, allocation, _head = _source(
            repository,
            task="pub-close-src",
            relative_path="feature.txt",
            content="feature\n",
        )
        queued = _enqueue(
            repository, task="pub-close-int", source_allocation=allocation
        )
        intent_id = str(queued["decision"]["intent_id"])
        candidate, _claimed = _claim(
            repository, task="pub-close-int", intent_id=intent_id
        )
        _validate(repository, candidate, "validation-pub-close")
        completed = _publish(repository, candidate, auto_release=False)
        pub_id = str(completed["decision"]["receipt_id"])
        store = MetadataStore(inventory(repository.repo).common_dir)
        record = store.load().records[str(completed["decision"]["allocation_id"])]
        before_meta = metadata_bytes(store)

        # Missing --publication-receipt-id must refuse with exact receipt hint and zero mutation.
        with pytest.raises(ControllerError) as missing:
            release(
                repository.repo,
                target=candidate,
                expected_record_version=record.record_version,
                owner_release=record.owner,
            )
        assert missing.value.code == "PUBLICATION_RECEIPT_REQUIRED"
        assert pub_id in str(missing.value)
        assert candidate.exists()
        assert metadata_bytes(store) == before_meta

        # Dry-run without --publication-receipt-id also fails closed.
        with pytest.raises(ControllerError) as missing_dry:
            release(
                repository.repo,
                target=candidate,
                expected_record_version=record.record_version,
                owner_release=record.owner,
                dry_run=True,
            )
        assert missing_dry.value.code == "PUBLICATION_RECEIPT_REQUIRED"
        assert metadata_bytes(store) == before_meta

        # Providing exact --publication-receipt-id succeeds, advances IntegrationIntent, and keeps doctor PASS.
        released = release(
            repository.repo,
            target=candidate,
            expected_record_version=record.record_version,
            owner_release=record.owner,
            publication_receipt_id=pub_id,
        )
        assert released["decision"]["code"] == "RELEASED"
        assert not candidate.exists()
        snapshot = store.load()
        assert snapshot.integration_intents[intent_id].state == "RELEASED"
        assert any(
            plan.publication_receipt_id == pub_id
            for plan in snapshot.release_plans.values()
        )
        doc = doctor(repository.repo)
        assert doc["decision"]["code"] == "PASS"
        assert "PUBLICATION_RECEIPT_DRIFT" not in {w["code"] for w in doc["warnings"]}


def test_published_owner_release_with_wrong_publication_receipt_fails_closed() -> None:
    """Passing --owner-release alongside a mismatched --publication-receipt-id must refuse before mutating."""
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        _src1, alloc1, _h1 = _source(
            repository, task="pub-a-src", relative_path="a.txt", content="a\n"
        )
        q1 = _enqueue(repository, task="pub-a-int", source_allocation=alloc1)
        cand1, _ = _claim(repository, task="pub-a-int", intent_id=str(q1["decision"]["intent_id"]))
        _validate(repository, cand1, "val-a")
        pub1 = _publish(repository, cand1)
        pub1_id = str(pub1["decision"]["receipt_id"])

        _src2, alloc2, _h2 = _source(
            repository, task="pub-b-src", relative_path="b.txt", content="b\n"
        )
        q2 = _enqueue(repository, task="pub-b-int", source_allocation=alloc2)
        cand2, _ = _claim(repository, task="pub-b-int", intent_id=str(q2["decision"]["intent_id"]))
        _validate(repository, cand2, "val-b")
        pub2 = _publish(repository, cand2, auto_release=False)
        store = MetadataStore(inventory(repository.repo).common_dir)
        record2 = store.load().records[str(pub2["decision"]["allocation_id"])]
        before_meta = metadata_bytes(store)

        with pytest.raises(ControllerError) as wrong:
            release(
                repository.repo,
                target=cand2,
                expected_record_version=record2.record_version,
                owner_release=record2.owner,
                publication_receipt_id=pub1_id,
            )
        assert wrong.value.code == "PUBLICATION_RECEIPT_DRIFT"
        assert cand2.exists()
        assert metadata_bytes(store) == before_meta


def test_register_disposable_allowed_on_ready_to_publish_candidate_and_closes_in_one_shot() -> None:
    """An integration candidate already in READY_TO_PUBLISH can classify its .venv before lease-complete."""
    with GitRepository(linked=0, policy_text=POLICY_AUTO) as repository:
        _commit_reproducible_project(repository)
        adopt_canonical(repository)
        _configure_remote(repository)
        _source_path, allocation, _head = _source(
            repository,
            task="ready-venv-src",
            relative_path="feature.txt",
            content="feature\n",
        )
        queued = _enqueue(
            repository, task="ready-venv-int", source_allocation=allocation
        )
        intent_id = str(queued["decision"]["intent_id"])
        candidate, claimed = _claim(
            repository, task="ready-venv-int", intent_id=intent_id
        )
        _create_reproducible_venv(candidate)
        _validate(repository, candidate, "validation-ready-venv")
        store = MetadataStore(inventory(repository.repo).common_dir)
        alloc_id = str(claimed["decision"]["allocation_id"])
        rec = store.load().records[alloc_id]
        assert rec.lifecycle == "HANDOFF_READY"

        registered = register_disposable(
            repository.repo,
            target=candidate,
            expected_record_version=rec.record_version,
            owner=rec.owner,
            resource_path=".venv",
            kind="python-venv",
            reproducible=True,
            creator="uv run pytest",
            teardown_policy="on-release",
        )
        assert registered["decision"]["code"] == "DISPOSABLE_REGISTERED"
        assert registered["decision"]["reproducible_check"] == {
            "verified": True,
            "project_file": "pyproject.toml",
            "lock_file": "uv.lock",
        }
        published = _publish(
            repository,
            candidate,
            expected_record_version=registered["decision"]["record_version"],
        )
        assert published["decision"]["code"] == "PUBLICATION_RELEASED"
        assert not candidate.exists()
        assert store.load().integration_intents[intent_id].state == "RELEASED"
        assert doctor(repository.repo)["decision"]["code"] == "PASS"


def test_cleanup_pending_lease_complete_emits_message_and_recovery_guide_argv() -> None:
    """CLEANUP_PENDING includes exact blocker message and formal recovery-guide argv."""
    with GitRepository(linked=0, policy_text=POLICY_AUTO) as repository:
        _commit_reproducible_project(repository)
        adopt_canonical(repository)
        _configure_remote(repository)
        _source_path, allocation, _head = _source(
            repository,
            task="pending-guide-src",
            relative_path="feature.txt",
            content="feature\n",
        )
        queued = _enqueue(
            repository, task="pending-guide-int", source_allocation=allocation
        )
        candidate, _claimed = _claim(
            repository, task="pending-guide-int", intent_id=str(queued["decision"]["intent_id"])
        )
        _validate(repository, candidate, "validation-pending-guide")
        _create_reproducible_venv(candidate)

        completed = _publish(repository, candidate)
        cleanup = completed["decision"]["cleanup"]
        assert cleanup["result"] == "CLEANUP_PENDING"
        assert cleanup["blocker"] == "IGNORED_CONTENT"
        assert ".venv" in cleanup["message"]
        assert cleanup["recovery_argv"][:2] == ["worktree-controller", "recovery-guide"]
        assert str(candidate) in cleanup["recovery_argv"]
        # Unclassified files are strictly preserved.
        assert (candidate / ".venv" / "pyvenv.cfg").is_file()
        assert git(candidate, "status", "--porcelain") == ""


@pytest.mark.parametrize("sandbox_mode", ["bwrap", "auto", "scoped-file"])
def test_worktree_session_sandbox_refuses_protected_canonical_checkout(
    tmp_path: Path, sandbox_mode: str
) -> None:
    """worktree-session --sandbox must never mount a protected canonical checkout RW."""
    from tests.test_retention_admission import POLICY_V5
    from worktree_controller.writer_session import run_main

    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        tracked = repository.repo / "protected.txt"
        tracked.write_text("original\n", encoding="utf-8")
        git(repository.repo, "add", "protected.txt")
        git(repository.repo, "commit", "-m", "add protected file")

        rc = run_main(
            [
                "--path",
                str(repository.repo),
                "--client-id",
                "codex",
                "--session-id",
                "canon-sandbox",
                "--run-id",
                f"canon-{sandbox_mode}",
                "--sandbox",
                sandbox_mode,
                "--",
                "/bin/sh",
                "-c",
                "echo overwritten > protected.txt",
            ]
        )
        assert rc == 2
        assert tracked.read_text(encoding="utf-8") == "original\n"
