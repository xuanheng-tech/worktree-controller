from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from tests.test_auto_release import POLICY_MANUAL, _publish_source
from tests.test_cleanup_recovery import metadata_bytes
from tests.test_controlled import (
    _cli,
    _configure_remote,
    _prepare_teardown_target,
    _release_arguments,
    acquire_one,
    adopt_canonical,
    handoff_one,
)
from tests.test_publication_execution import leased as leased
from tests.test_shadow import GitRepository, git
from worktree_controller import lifecycle
from worktree_controller.git_backend import inventory
from worktree_controller.store import MetadataStore


@pytest.fixture
def interrupted():
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, acquired = acquire_one(repository)
        handoff_one(repository, target)
        crashed = _cli(_release_arguments(repository, target), crash_at="release_metadata_written")
        assert crashed.returncode == 97
        store = MetadataStore(inventory(repository.repo).common_dir)
        journal_path, = store.transactions_dir.glob("*.json")
        yield repository, target, acquired["decision"]["allocation_id"], store, journal_path


def inspect(repository, transaction):
    return _cli([
        "release-recovery-inspect", "--repo", str(repository.repo),
        "--transaction-id", transaction, "--json",
    ])


@pytest.mark.parametrize("crash_at", [
    "release_prepared", "release_worktree_removed", "release_metadata_written",
])
def test_original_owner_request_is_read_only_and_replays_through_cli(crash_at):
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target = repository.root / "task with spaces"
        acquired = lifecycle.acquire(
            repository.repo, target=target, task_key="new-task", authority_key="new-authority",
            module="module", role="feature", owner="module-owner",
            branch="feature/new-task", base_ref="HEAD",
        )
        handoff_one(repository, target)
        crashed = _cli(_release_arguments(repository, target), crash_at=crash_at)
        assert crashed.returncode == 97
        store = MetadataStore(inventory(repository.repo).common_dir)
        journal_path, = store.transactions_dir.glob("*.json")
        before, refs = metadata_bytes(store), git(repository.repo, "show-ref")
        result = inspect(repository, journal_path.stem)
        assert result.returncode == 0, result.stdout + result.stderr
        decision = json.loads(result.stdout)["decision"]
        assert decision["contract"] == "release-recovery-inspection/v1"
        assert decision["release_authorized"] is False
        assert decision["execution_guards_checked"] is False
        assert decision["allocation_id"] == acquired["decision"]["allocation_id"]
        assert decision["owner"] == "module-owner"
        assert decision["request"] == {
            "target_path": str(target), "expected_record_version": 2,
            "owner_release": "module-owner", "publication_receipt_id": None, "release_intent_id": None,
        }
        assert decision["release_argv"] == _release_arguments(repository, target)
        plan_path = store.release_plans_dir / f"{decision['release_plan_id']}.json"
        for name, path in (("journal", journal_path), ("release_plan", plan_path)):
            assert decision["evidence"][name] == {
                "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        assert metadata_bytes(store) == before
        assert git(repository.repo, "show-ref") == refs
        repeated = inspect(repository, journal_path.stem)
        assert repeated.returncode == 0 and json.loads(repeated.stdout)["decision"] == decision
        assert metadata_bytes(store) == before
        recovered = _cli(decision["release_argv"])
        assert recovered.returncode == 0, recovered.stdout + recovered.stderr
        assert json.loads(recovered.stdout)["decision"]["code"] in {"RELEASED", "RELEASE_RECOVERED"}
        assert not target.exists() and not journal_path.exists()
        assert store.load().release_receipts[journal_path.stem].plan_id == decision["release_plan_id"]


@pytest.mark.parametrize("mutation, code", [
    ("missing_journal", "MISSING_RELEASE_RECOVERY_EVIDENCE"),
    ("missing_plan", "MISSING_RELEASE_RECOVERY_EVIDENCE"),
    ("plan_hash", "INVALID_METADATA"),
    ("other_command", "UNSUPPORTED_RELEASE_RECOVERY_EVIDENCE"),
    ("legacy", "UNSUPPORTED_RELEASE_RECOVERY_EVIDENCE"),
    ("filename_identity", "INVALID_TRANSACTION"),
    ("phase", "INVALID_TRANSACTION"),
    ("extra_field", "INVALID_TRANSACTION"),
    ("boolean_schema", "INVALID_TRANSACTION"),
    ("boolean_cas", "INVALID_TRANSACTION"),
    ("repository", "RELEASE_RECOVERY_MISMATCH"),
    ("allocation", "RELEASE_RECOVERY_MISMATCH"),
    ("head", "RELEASE_RECOVERY_MISMATCH"),
    ("cas", "RELEASE_RECOVERY_MISMATCH"),
])
def test_missing_or_inconsistent_evidence_is_refused_without_writes(interrupted, mutation, code):
    repository, target, _, store, path = interrupted
    journal = json.loads(path.read_text())
    plan_path = store.release_plans_dir / f"{journal['release_plan_id']}.json"
    if mutation == "missing_journal":
        path.unlink()
    elif mutation == "missing_plan":
        plan_path.unlink()
    elif mutation == "plan_hash":
        plan = json.loads(plan_path.read_text())
        plan["owner"] = "different-owner"
        plan_path.write_text(json.dumps(plan))
    else:
        fields = {
            "other_command": ("command", "resource-clean"),
            "filename_identity": ("transaction_id", str(uuid.uuid4())),
            "phase": ("phase", "UNKNOWN"), "extra_field": ("extra", "unreviewed"),
            "boolean_schema": ("schema_version", True), "boolean_cas": ("expected_record_version", True),
            "repository": ("repository_id", str(uuid.uuid4())),
            "allocation": ("allocation_id", str(uuid.uuid4())),
            "head": ("expected_head", "0" * 40), "cas": ("expected_record_version", 3),
        }
        if mutation == "legacy":
            del journal["release_plan_id"]
        else:
            field, value = fields[mutation]
            journal[field] = value
        path.write_text(json.dumps(journal))
    before, refs = metadata_bytes(store), git(repository.repo, "show-ref")
    result = inspect(repository, path.stem)
    assert result.returncode == 2, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["errors"][0]["code"] == code
    assert "request" not in payload["decision"] and "release_argv" not in payload["decision"]
    assert metadata_bytes(store) == before
    assert git(repository.repo, "show-ref") == refs
    assert not target.exists()


@pytest.mark.parametrize("selector", ["../release", "not-a-uuid", "{00000000-0000-0000-0000-000000000000}"])
def test_selector_must_be_one_canonical_uuid(interrupted, selector):
    repository, _, _, store, _ = interrupted
    before = metadata_bytes(store)
    result = inspect(repository, selector)
    assert result.returncode == 2
    assert json.loads(result.stdout)["errors"][0]["code"] == "INVALID_RELEASE_RECOVERY_QUERY"
    assert metadata_bytes(store) == before


def test_inspection_does_not_certify_current_execution_guards(interrupted):
    repository, _, allocation, store, path = interrupted
    # Advance the retained ref without changing the immutable original request evidence.
    git(repository.repo, "commit", "--allow-empty", "-m", "later main")
    git(repository.repo, "update-ref", "refs/heads/feature/new-task", git(repository.repo, "rev-parse", "HEAD"))
    before = metadata_bytes(store)
    result = inspect(repository, path.stem)
    assert result.returncode == 0
    decision = json.loads(result.stdout)["decision"]
    assert decision["request"]["expected_record_version"] == 2
    assert store.load().records[allocation].record_version == 3
    recovery = _cli(decision["release_argv"])
    assert recovery.returncode == 2
    assert json.loads(recovery.stdout)["errors"][0]["code"] == "MISSING_RETAINED_REF"
    assert metadata_bytes(store) == before


def test_publication_identity_is_exposed_and_preserved_in_public_cli_replay(leased, monkeypatch):
    repository, target, lease, session = leased
    git(target, "push", "origin", f"{lease['candidate_oid']}:refs/heads/main")

    def interrupt(name):
        if name == "release_metadata_written":
            raise RuntimeError("interrupted publication release")

    with monkeypatch.context() as patch:
        patch.setattr(lifecycle, "_crash_point", interrupt)
        with pytest.raises(RuntimeError, match="interrupted publication release"):
            lifecycle.complete_publication_lease(
                repository.repo, target=target, expected_record_version=lease["record_version"],
                lease_id=lease["lease_id"], published_commit=lease["candidate_oid"],
                finalizer_record_id="test-finalizer-record", run_id=lease["run_id"],
            )
    path, = session.store.transactions_dir.glob("*.json")
    before = metadata_bytes(session.store)
    inspected = inspect(repository, path.stem)
    assert inspected.returncode == 0, inspected.stdout + inspected.stderr
    decision = json.loads(inspected.stdout)["decision"]
    assert decision["request"]["publication_receipt_id"] == lease["lease_id"]
    assert decision["request"]["owner_release"] is None
    assert "--publication-receipt-id" in decision["release_argv"]
    assert metadata_bytes(session.store) == before
    missing_identity = decision["release_argv"].copy()
    position = missing_identity.index("--publication-receipt-id")
    del missing_identity[position:position + 2]
    refused = _cli(missing_identity)
    assert refused.returncode == 2
    assert json.loads(refused.stdout)["errors"][0]["code"] == "RELEASE_RECOVERY_MISMATCH"
    assert metadata_bytes(session.store) == before
    recovered = _cli(decision["release_argv"])
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert json.loads(recovered.stdout)["decision"]["release_receipt_id"] == path.stem
    assert not path.exists() and not target.exists()


def test_journal_symlink_is_refused_without_following_it(interrupted):
    repository, _, _, store, path = interrupted
    preserved = repository.root / "preserved-journal.json"
    preserved.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(preserved)
    before = metadata_bytes(store)
    result = inspect(repository, path.stem)
    assert result.returncode == 2
    assert "release_argv" not in json.loads(result.stdout)["decision"]
    assert metadata_bytes(store) == before
    assert path.is_symlink() and preserved.read_bytes() == before[path.relative_to(store.root)]


def test_release_intent_identity_is_preserved_in_public_cli_replay():
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        target, allocation, _, _, _ = _publish_source(
            repository, source_task="intent-source", integration_task="intent-candidate",
        )
        store = MetadataStore(inventory(repository.repo).common_dir)
        record = store.load().records[allocation]
        intent = lifecycle.create_release_intent(
            repository.repo, target=target, expected_record_version=record.record_version,
            owner=record.owner, trigger="manual",
        )["decision"]["intent_id"]
        arguments = ["release", "--repo", str(repository.repo), "--path", str(target),
                     "--expected-record-version", str(record.record_version), "--intent-id", intent, "--json"]
        crashed = _cli(arguments, crash_at="release_metadata_written")
        assert crashed.returncode == 97, crashed.stdout + crashed.stderr
        path, = store.transactions_dir.glob("*.json")
        before = metadata_bytes(store)
        inspected = inspect(repository, path.stem)
        assert inspected.returncode == 0, inspected.stdout + inspected.stderr
        decision = json.loads(inspected.stdout)["decision"]
        assert decision["request"]["release_intent_id"] == intent
        assert decision["request"]["owner_release"] is None
        assert decision["release_argv"] == arguments
        assert metadata_bytes(store) == before
        recovered = _cli(decision["release_argv"])
        assert recovered.returncode == 0, recovered.stdout + recovered.stderr
        assert json.loads(recovered.stdout)["decision"]["release_receipt_id"] == path.stem
        assert not path.exists() and not target.exists()


@pytest.mark.parametrize("crash_at", [
    "release_teardown_prepared", "release_venv_removal_started", "release_venv_removed",
])
def test_registered_teardown_request_remains_normal_guarded_recovery(crash_at):
    with GitRepository(linked=0) as repository:
        target = _prepare_teardown_target(repository)
        crashed = _cli(_release_arguments(repository, target, expected=3), crash_at=crash_at)
        assert crashed.returncode == 97
        store = MetadataStore(inventory(repository.repo).common_dir)
        path, = store.transactions_dir.glob("*.json")
        before = metadata_bytes(store)
        inspected = inspect(repository, path.stem)
        assert inspected.returncode == 0, inspected.stdout + inspected.stderr
        decision = json.loads(inspected.stdout)["decision"]
        assert decision["request"]["expected_record_version"] == 3
        assert metadata_bytes(store) == before
        recovered = _cli(decision["release_argv"])
        assert recovered.returncode == 0, recovered.stdout + recovered.stderr
        assert not path.exists() and not target.exists()


def test_publication_cli_parameter_uses_existing_live_release_guards(leased):
    repository, target, lease, session = leased
    git(target, "push", "origin", f"{lease['candidate_oid']}:refs/heads/main")
    completed = lifecycle.complete_publication_lease(
        repository.repo, target=target, expected_record_version=lease["record_version"],
        lease_id=lease["lease_id"], published_commit=lease["candidate_oid"],
        finalizer_record_id="test-finalizer-record", run_id=lease["run_id"], auto_release=False,
    )
    arguments = ["release", "--repo", str(repository.repo), "--path", str(target),
                 "--expected-record-version", str(completed["decision"]["record_version"]), "--json"]
    before = metadata_bytes(session.store)
    refused = _cli(arguments)
    assert refused.returncode == 2
    assert json.loads(refused.stdout)["errors"][0]["code"] == "ACTIVE_OWNER"
    assert metadata_bytes(session.store) == before
    released = _cli([*arguments, "--publication-receipt-id", lease["lease_id"]])
    assert released.returncode == 0, released.stdout + released.stderr
    assert json.loads(released.stdout)["decision"]["code"] == "RELEASED"
    assert not target.exists()
