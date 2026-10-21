"""Host budgets, durable requests and existing repository lifecycle compose."""

import errno
import json
import os
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_controlled import _configure_remote, acquire_one, adopt_canonical
from tests.test_integration_pipeline import _source
from tests.test_shadow import GitRepository, git
from tests.test_writer_lease import POLICY_V6
from worktree_controller import lifecycle, writer_lease
from worktree_controller import resource_admission as resources
from worktree_controller.controller import plan
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import acquire_across
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore
from worktree_controller.writer_lease import RUNTIME_BINDING_PREFIX


@pytest.mark.parametrize("effect", ["none", "branch", "worktree"])
def test_failed_acquire_retains_uncertainty_and_only_retries_proved_no_effect(
    host, monkeypatch, effect
):
    _, ledger, _ = host
    with GitRepository(linked=0, policy_text=POLICY) as repository:
        adopt_canonical(repository)
        original = lifecycle.git_write

        def timeout(repo, arguments):
            if effect == "branch":
                original(repo, ("branch", arguments[3], arguments[-1]))
            elif effect == "worktree":
                original(repo, arguments)
            raise ControllerError("GIT_TIMEOUT", "fixture interrupted worktree add")

        monkeypatch.setattr(lifecycle, "git_write", timeout)
        with pytest.raises(ControllerError, match="fixture interrupted"):
            acquire_one(repository)
        store = MetadataStore(inventory(repository.repo).common_dir)
        rows = list(ledger.read()["requests"].values())
        assert len(rows) == 1
        row = rows[0]
        journals = list(store.transactions_dir.glob("*.json"))
        if effect == "none":
            assert row["state"] == "WAITING" and not journals
            proof = next(event for event in row["history"] if "terminal_evidence" in event)
            assert proof["journal"]["phase"] == "PREPARED"
            assert proof["journal"]["transaction_id"] == proof["transaction_id"]
            monkeypatch.setattr(lifecycle, "git_write", original)
            _, acquired = acquire_one(repository)
            assert acquired["decision"]["code"] == "ACQUIRED"
            assert not list(store.transactions_dir.glob("*.json"))
        else:
            assert row["state"] == "RESERVED" and len(journals) == 1
            assert json.loads(journals[0].read_text())["phase"] == "PREPARED"
            outcome = resources.reconcile(repository_path=repository.repo, apply=True)["decision"][
                "outcomes"
            ][0]
            assert outcome["blocker"] == "INTERRUPTED_TRANSACTION"
            assert (
                ledger.read()["requests"][resources.fingerprint(row["request"])]["state"]
                == "RESERVED"
            )


def test_no_effect_settlement_recovers_after_journal_removal(host, monkeypatch):
    _, ledger, _ = host
    with GitRepository(linked=0, policy_text=POLICY) as repository:
        adopt_canonical(repository)

        def timeout(*args):
            raise ControllerError("GIT_TIMEOUT", "fixture interruption")

        original_save = resources.AdmissionLedger._save

        def fail_after_proof(self, state):
            if any(
                row["state"] == "WAITING" and row["history"][-1].get("transaction_id")
                for row in state["requests"].values()
            ):
                raise OSError("fixture failed final settlement write")
            original_save(self, state)

        monkeypatch.setattr(lifecycle, "git_write", timeout)
        monkeypatch.setattr(resources.AdmissionLedger, "_save", fail_after_proof)
        with pytest.raises(ControllerError, match="fixture interruption"):
            acquire_one(repository)
        row = next(iter(ledger.read()["requests"].values()))
        assert row["state"] == "RESERVED"
        assert row["history"][-1]["terminal_evidence"] == "ACQUIRE_NO_EFFECT_SUPERSEDED"
        monkeypatch.setattr(resources.AdmissionLedger, "_save", original_save)
        outcome = resources.reconcile(repository_path=repository.repo, apply=True)["decision"][
            "outcomes"
        ][0]
        assert outcome["state"] == "WAITING"
        assert next(iter(ledger.read()["requests"].values()))["state"] == "WAITING"


@pytest.mark.parametrize("apply", [False, True])
def test_completed_queue_observes_each_repository_once_per_phase(host, monkeypatch, apply):
    with GitRepository(linked=0, policy_text=POLICY) as left, GitRepository(linked=0, policy_text=POLICY) as right:
        for repository in (left, right):
            adopt_canonical(repository)
            for index in range(4):
                acquire_one(repository, task=f"task-{index}", authority=f"task-{index}")
        inventories, snapshots = [], []
        original_inventory, original_load = resources.inventory, MetadataStore._load_unlocked

        def observe_inventory(path):
            inventories.append(path)
            return original_inventory(path)

        def observe_snapshot(store):
            assert store.flock_held
            snapshots.append(store.root)
            return original_load(store)

        monkeypatch.setattr(resources, "inventory", observe_inventory)
        monkeypatch.setattr(MetadataStore, "_load_unlocked", observe_snapshot)
        result = resources.reconcile(apply=apply)["decision"]
        assert len(result["outcomes"]) == 8
        assert all(item["state"] == "ALLOCATED" and "blocker" not in item for item in result["outcomes"])
        # Apply also observes each repository's existing release-intent queue.
        assert len(inventories) == len(snapshots) == (4 if apply else 2)


@pytest.mark.parametrize("execution", [False, True])
def test_completed_queue_apply_does_not_wait_for_an_unrelated_reader(host, execution):
    with GitRepository(linked=0, policy_text=POLICY) as repository:
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        if execution:
            resources.admit_execution(repository.repo, target=target, owner="module-owner",
                client_id="coordinator", session_id="pending-run")
        store = MetadataStore(inventory(repository.repo).common_dir)
        reader = subprocess.Popen(
            [sys.executable, "-c", """
import fcntl, sys
with open(sys.argv[1], 'rb') as stream:
    fcntl.flock(stream, fcntl.LOCK_SH)
    print('locked', flush=True)
    sys.stdin.buffer.read(1)
""", str(store.lock_file)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        try:
            assert reader.stdout.readline().strip() == "locked"
            observed = subprocess.run(
                [sys.executable, "-m", "worktree_controller.cli", "admission-queue",
                 "--repo", str(repository.repo), "--apply", "--json"],
                capture_output=True, text=True, timeout=5, check=True,
                env={**os.environ, "PYTHONPATH": str(Path(resources.__file__).parents[1])},
            )
            results = json.loads(observed.stdout)["decision"]["outcomes"]
            assert results[0]["state"] == "ALLOCATED"
            assert len(results) == (2 if execution else 1)
            assert all("blocker" not in result for result in results)
            assert reader.poll() is None
        finally:
            reader.communicate("x", timeout=5)


@pytest.mark.parametrize("drift", ["owner", "registration", "transaction"])
def test_completed_queue_batch_keeps_each_allocations_guards(host, drift):
    _, ledger, _ = host
    with GitRepository(linked=0, policy_text=POLICY) as repository:
        adopt_canonical(repository)
        target, _ = acquire_one(repository, task="first", authority="first")
        acquire_one(repository, task="second", authority="second")
        observed = inventory(repository.repo)
        store = MetadataStore(observed.common_dir)
        item = next(item for item in observed.worktrees if item.path == target)
        record = store.load().records[store.load().bindings[item.key]]
        if drift == "owner":
            store.write_record(item.key, replace(record, owner="other-owner"))
        elif drift == "registration":
            (item.git_dir / "worktree-controller-registration").unlink()
        else:
            # Unknown journal identity remains a repository-wide recovery fence.
            store.transactions_dir.mkdir(exist_ok=True)
            (store.transactions_dir / f"{uuid.uuid4()}.json").write_text("{}")
        before = ledger.path.read_bytes()
        results = {row["task_key"]: row for row in resources.reconcile(apply=True)["decision"]["outcomes"]}
        assert "blocker" in results["first"]
        assert ("blocker" in results["second"]) is (drift == "transaction")
        assert ledger.path.read_bytes() == before


def test_completed_queue_apply_fails_closed_when_repository_lock_is_missing(host):
    _, ledger, _ = host
    with GitRepository(linked=0, policy_text=POLICY) as repository:
        adopt_canonical(repository)
        acquire_one(repository)
        store = MetadataStore(inventory(repository.repo).common_dir)
        store.lock_file.unlink()
        before = ledger.path.read_bytes()
        result = resources.reconcile(apply=True)["decision"]["outcomes"][0]
        assert result["blocker"] == "MISSING_REPOSITORY_LOCK"
        assert ledger.path.read_bytes() == before


POLICY = POLICY_V6.replace("schema_version = 6", "schema_version = 7") + """
[admission]
strategy = "resource-budget-v1"
checkout_growth_bytes = 1024
checkout_growth_inodes = 10
"""


@pytest.fixture
def host(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local/state"))
    counters = {"bytes": 10**12, "inodes": 10**9, "memory": 32 * resources.GIB,
                "cpu": 0.0, "io": 0.0, "memory_psi": 0.0}
    # Default plan target can have missing parents; all fixture paths share a FS.
    device = tmp_path.stat().st_dev
    monkeypatch.setattr(resources, "filesystem", lambda path: {
        "device": device, "observed_path": str(path), "available_bytes": counters["bytes"],
        "available_inodes": counters["inodes"]})
    monkeypatch.setattr(resources, "pressure", lambda: {
        "available_memory_bytes": counters["memory"], "observed_at_ns": 1,
        "psi_avg10": {"cpu": counters["cpu"], "io": counters["io"], "memory": counters["memory_psi"]}})
    return counters, resources.AdmissionLedger(), device


@pytest.fixture
def shared_authority_intent(host):
    with GitRepository(linked=0, policy_text=POLICY) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        source, allocation, head = _source(
            repository, task="lineage-source", relative_path="source.txt", content="source\n"
        )
        git(source, "push", "-u", "origin", "feature/lineage-source")
        queued = lifecycle.enqueue_integration(
            repository.repo, task_key="lineage-candidate", authority_key="lineage-source",
            module="integration", owner="integration-owner", source_allocations=(allocation,),
            source_snapshot_ids=("snapshot:lineage-source",),
        )["decision"]
        yield repository, source, allocation, head, queued


def test_frozen_source_shared_authority_claim_is_admitted_and_idempotent(host, shared_authority_intent):
    _, ledger, _ = host
    repository, source, allocation, head, queued = shared_authority_intent
    store = MetadataStore(inventory(repository.repo).common_dir)
    source_row = next(row for row in ledger.read()["requests"].values()
                      if row.get("allocation_id") == allocation)
    before_source = store.load().records[allocation]
    arguments = dict(owner="integration-owner", intent_id=queued["intent_id"],
                     target=repository.root / "candidate", branch="integration/lineage-candidate")
    claimed = lifecycle.claim_integration(repository.repo, **arguments)["decision"]
    assert claimed["code"] == "INTEGRATION_CANDIDATE_CLAIMED"
    assert claimed["queue_sequence"] == queued["queue_sequence"]
    assert claimed["source_handoff_oids"] == [head]
    assert claimed["scope_paths"] == ["source.txt"]
    assert store.load().records[allocation] == before_source
    assert git(source, "rev-parse", "HEAD") == head
    assert git(source, "rev-parse", "@{upstream}") == head
    assert source_row == ledger.read()["requests"][resources.fingerprint(source_row["request"])]
    before, budget = store.load(), ledger.read()
    events = store.list_lifecycle_events("lineage-candidate")
    repeated = lifecycle.claim_integration(repository.repo, **arguments)["decision"]
    assert repeated["allocation_id"] == claimed["allocation_id"]
    assert repeated["candidate_oid"] == claimed["candidate_oid"]
    assert store.load() == before and ledger.read() == budget
    assert store.list_lifecycle_events("lineage-candidate") == events
    assert before.publication_lease is None and not before.prepared_candidate_receipts


def test_shared_authority_without_frozen_intent_still_refuses_drift(host, shared_authority_intent):
    _, ledger, _ = host
    repository, _, allocation, head, queued = shared_authority_intent
    before = ledger.read()
    with pytest.raises(ControllerError) as error:
        lifecycle.acquire(
            repository.repo, task_key="unbound-candidate", authority_key="lineage-source",
            module="integration", role="integration", owner="integration-owner",
            branch="integration/unbound", base_ref=head, target=repository.root / "unbound",
            source_allocations=(allocation,),
        )
    assert error.value.code == "ADMISSION_REQUEST_DRIFT"
    assert ledger.read() == before
    snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
    assert snapshot.integration_intents[queued["intent_id"]].state == "QUEUED"
    assert snapshot.publication_lease is None


def test_shared_authority_claim_preserves_resource_wait_and_original_queue(host, shared_authority_intent):
    counters, ledger, _ = host
    repository, _, allocation, _, queued = shared_authority_intent
    counters["bytes"] = 0
    arguments = dict(owner="integration-owner", intent_id=queued["intent_id"],
                     target=repository.root / "candidate", branch="integration/lineage-candidate")
    waiting = lifecycle.claim_integration(repository.repo, **arguments)["decision"]
    assert waiting["code"] == "RESOURCE_WAIT"
    store = MetadataStore(inventory(repository.repo).common_dir)
    intent = store.load().integration_intents[queued["intent_id"]]
    assert intent.state == "QUEUED" and intent.allocation_id is None
    assert intent.queue_sequence == queued["queue_sequence"]
    assert store.load().records[allocation].lifecycle == "HANDOFF_READY"
    assert sum(row["state"] == "WAITING" for row in ledger.read()["requests"].values()) == 1
    counters["bytes"] = 10**12
    claimed = lifecycle.claim_integration(repository.repo, **arguments)["decision"]
    assert claimed["intent_id"] == queued["intent_id"]
    assert claimed["queue_sequence"] == queued["queue_sequence"]
    assert store.load().publication_lease is None


@pytest.mark.parametrize("reserved", [False, True])
def test_abandoned_unattempted_claim_settles_without_replaying_or_completing_task(
    host, shared_authority_intent, reserved
):
    counters, ledger, _ = host
    repository, _, allocation, _, queued = shared_authority_intent
    counters["bytes"] = 0
    target = repository.root / "candidate"
    lifecycle.claim_integration(repository.repo, owner="integration-owner",
        intent_id=queued["intent_id"], target=target, branch="integration/lineage-candidate")
    key, row = next((key, row) for key, row in ledger.read()["requests"].items()
                    if row["state"] == "WAITING")
    if reserved:
        counters["bytes"] = 10**12
        ledger.reserve([row["request"]], integration_sources=(allocation,))
    lifecycle.abandon_integration_intent(repository.repo, intent_id=queued["intent_id"],
        owner="integration-owner", reason="owner selected another implementation")
    store = MetadataStore(inventory(repository.repo).common_dir)
    before_repository, before_ledger = store.load(), ledger.path.read_bytes()
    planned = next(item for item in resources.reconcile(repository_path=repository.repo)["decision"]["outcomes"]
                   if item["request_id"] == key)
    assert planned["state"] == "CANCELLED" and planned["terminal_evidence"] == "PREPARATION_ABANDONED"
    assert planned["task_completed"] is False
    assert ledger.path.read_bytes() == before_ledger
    applied = next(item for item in resources.reconcile(repository_path=repository.repo, apply=True)["decision"]["outcomes"]
                   if item["request_id"] == key)
    assert applied == planned
    settled = ledger.read()["requests"][key]
    assert settled["state"] == "CANCELLED" and not settled.get("transaction_id")
    assert settled["abandoned_intent_evidence"]["intent_id"] == queued["intent_id"]
    assert settled["abandoned_intent_evidence"]["terminal_event"] == before_repository.integration_intents[queued["intent_id"]].history[-1]
    assert settled["history"][-1]["terminal_evidence"] == "PREPARATION_ABANDONED"
    settled_bytes = ledger.path.read_bytes()
    resources.reconcile(repository_path=repository.repo, apply=True)
    assert ledger.path.read_bytes() == settled_bytes
    assert store.load() == before_repository
    assert not target.exists()
    assert "integration/lineage-candidate" not in git(repository.repo, "branch", "--list")


@pytest.mark.parametrize("uncertainty", [
    "owner", "source", "no-terminal-event", "target", "branch", "transaction", "pending-journal",
])
def test_abandoned_claim_preserves_uncertain_or_attempted_requests(
    host, shared_authority_intent, uncertainty
):
    counters, ledger, _ = host
    repository, _, allocation, head, queued = shared_authority_intent
    counters["bytes"] = 0
    target = repository.root / "candidate"
    lifecycle.claim_integration(repository.repo, owner="integration-owner",
        intent_id=queued["intent_id"], target=target, branch="integration/lineage-candidate")
    key, row = next((key, row) for key, row in ledger.read()["requests"].items()
                    if row["state"] == "WAITING")
    lifecycle.abandon_integration_intent(repository.repo, intent_id=queued["intent_id"],
        owner="integration-owner", reason="owner selected another implementation")
    store = MetadataStore(inventory(repository.repo).common_dir)
    intent = store.load().integration_intents[queued["intent_id"]]
    if uncertainty == "owner":
        store.replace_integration_intent(replace(intent, owner="other-owner"))
    elif uncertainty == "source":
        store.replace_integration_intent(replace(intent, source_allocations=(str(uuid.uuid4()),)))
    elif uncertainty == "no-terminal-event":
        store.replace_integration_intent(replace(intent, history=intent.history[:-1]))
    elif uncertainty == "target":
        target.mkdir()
    elif uncertainty == "branch":
        git(repository.repo, "branch", "integration/lineage-candidate", head)
    elif uncertainty == "transaction":
        counters["bytes"] = 10**12
        ledger.reserve([row["request"]], integration_sources=(allocation,))
        ledger.bind(row["request"], transaction_id=str(uuid.uuid4()), allocation_id=str(uuid.uuid4()))
    else:
        store.transactions_dir.mkdir(exist_ok=True)
        (store.transactions_dir / f"{uuid.uuid4()}.json").write_text("{}")
    before = ledger.path.read_bytes()
    outcome = next(item for item in resources.reconcile(repository_path=repository.repo, apply=True)["decision"]["outcomes"]
                   if item["request_id"] == key)
    assert "blocker" in outcome and outcome["state"] != "CANCELLED"
    assert ledger.path.read_bytes() == before


def test_waiting_claim_after_main_advance_recovers_original_request(host, shared_authority_intent):
    counters, ledger, _ = host
    repository, _, allocation, head, queued = shared_authority_intent
    counters["bytes"] = 0
    target = repository.root / "candidate"
    arguments = dict(owner="integration-owner", intent_id=queued["intent_id"],
                     target=target, branch="integration/lineage-candidate")
    assert lifecycle.claim_integration(repository.repo, **arguments)["decision"]["code"] == "RESOURCE_WAIT"
    request_id, waiting = next((key, row) for key, row in ledger.read()["requests"].items()
                               if row["state"] == "WAITING")
    old_base = git(repository.repo, "rev-parse", "HEAD")
    (repository.repo / "main-progress.txt").write_text("independent main progress\n")
    git(repository.repo, "add", "main-progress.txt")
    git(repository.repo, "commit", "-m", "advance main while candidate waits")
    git(repository.repo, "push", "origin", "main")
    new_base = git(repository.repo, "rev-parse", "HEAD")
    counters["bytes"] = 10**12
    before = ledger.read()
    with pytest.raises(ControllerError) as error:
        lifecycle.claim_integration(repository.repo, **arguments)
    assert error.value.code == "ADMISSION_REQUEST_DRIFT"
    assert request_id in str(error.value)
    assert "admission-queue" in str(error.value)
    assert ledger.read() == before

    resources.reconcile(repository_path=repository.repo, apply=True)
    after = ledger.read()
    assert after["requests"].keys() == before["requests"].keys()
    recovered = after["requests"][request_id]
    assert recovered["state"] == "ALLOCATED"
    assert recovered["request"] == waiting["request"]
    store = MetadataStore(inventory(repository.repo).common_dir)
    intent = store.load().integration_intents[queued["intent_id"]]
    assert intent.allocation_id == recovered["allocation_id"]
    assert intent.queue_sequence == queued["queue_sequence"]
    assert intent.source_allocations == (allocation,)
    assert intent.source_handoffs == (head,)
    assert intent.base_main_oid == old_base
    assert store.load().publication_lease is None
    record = store.load().records[intent.allocation_id]
    lifecycle.validate_integration(
        repository.repo, target=target, owner="integration-owner",
        expected_record_version=record.record_version,
        validation_evidence_id="old-base-preparation",
        tests_receipt_id="tests:old-base", snapshot_receipt_id="snapshot:old-base",
    )
    record = store.load().records[intent.allocation_id]
    refreshed = lifecycle.refresh_integration(
        repository.repo, target=target, owner="integration-owner",
        expected_record_version=record.record_version,
    )["decision"]
    assert refreshed["code"] == "INTEGRATION_REFRESHED"
    assert refreshed["validation_evidence_id"] is None
    intent = store.load().integration_intents[queued["intent_id"]]
    assert intent.base_main_oid == new_base
    assert intent.queue_sequence == queued["queue_sequence"]
    record = store.load().records[intent.allocation_id]
    validated = lifecycle.validate_integration(
        repository.repo, target=target, owner="integration-owner",
        expected_record_version=record.record_version,
        validation_evidence_id="recovered-main-validation",
        tests_receipt_id="tests:recovered-main", snapshot_receipt_id="snapshot:recovered-main",
    )["decision"]
    assert validated["state"] == "READY_TO_PUBLISH"
    assert store.load().publication_lease is None


def test_shared_authority_claim_keeps_scope_fence(host, shared_authority_intent):
    repository, _, allocation, _, queued = shared_authority_intent
    lifecycle.claim_integration(repository.repo, owner="integration-owner", intent_id=queued["intent_id"],
                                target=repository.root / "candidate", branch="integration/lineage-candidate")
    later = lifecycle.enqueue_integration(
        repository.repo, task_key="overlapping-candidate", authority_key="overlapping-candidate",
        module="integration", owner="integration-owner", source_allocations=(allocation,),
    )["decision"]
    waiting = lifecycle.claim_integration(repository.repo, owner="integration-owner", intent_id=later["intent_id"])
    assert waiting["decision"]["code"] == "INTEGRATION_WAITING_SCOPE"
    store = MetadataStore(inventory(repository.repo).common_dir)
    assert store.load().integration_intents[later["intent_id"]].state == "WAITING_SCOPE"
    assert store.load().publication_lease is None


def test_shared_authority_source_branch_drift_never_gets_admission(host, shared_authority_intent):
    _, ledger, _ = host
    repository, source, _, _, queued = shared_authority_intent
    before = ledger.read()
    (source / "source.txt").write_text("drift\n")
    git(source, "add", "source.txt")
    git(source, "commit", "-m", "fixture source drift")
    with pytest.raises(ControllerError) as error:
        lifecycle.claim_integration(repository.repo, owner="integration-owner", intent_id=queued["intent_id"])
    assert error.value.code == "SOURCE_EVIDENCE_MISMATCH"
    assert ledger.read() == before
    assert MetadataStore(inventory(repository.repo).common_dir).load().publication_lease is None


@pytest.mark.parametrize("changed", ["owner", "target", "branch", "head", "base"])
def test_repeated_claim_refuses_changed_candidate_identity(host, shared_authority_intent, changed):
    _, ledger, _ = host
    repository, _, _, _, queued = shared_authority_intent
    arguments = dict(owner="integration-owner", intent_id=queued["intent_id"],
                     target=repository.root / "candidate", branch="integration/lineage-candidate")
    lifecycle.claim_integration(repository.repo, **arguments)
    if changed == "head":
        git(arguments["target"], "commit", "--allow-empty", "-m", "fixture candidate drift")
    elif changed == "target":
        arguments["target"] = repository.root / "another-candidate"
    elif changed == "base":
        store = MetadataStore(inventory(repository.repo).common_dir)
        record = next(record for record in store.load().records.values() if record.role == "integration")
        store.replace_record(replace(record, base_commit=git(arguments["target"], "rev-parse", "HEAD")))
    else:
        arguments[changed] = "different"
    store = MetadataStore(inventory(repository.repo).common_dir)
    before, budget = store.load(), ledger.read()
    with pytest.raises(ControllerError) as error:
        lifecycle.claim_integration(repository.repo, **arguments)
    expected = {"owner": "OWNER_MISMATCH", "target": "INTEGRATION_INTENT_MISMATCH",
                "branch": "INTEGRATION_INTENT_MISMATCH", "head": "CANDIDATE_IDENTITY_MISMATCH",
                "base": "INTEGRATION_INTENT_MISMATCH"}
    assert error.value.code == expected[changed]
    assert store.load() == before and ledger.read() == budget


def test_concurrent_claims_bind_one_candidate_and_one_budget(host, shared_authority_intent, monkeypatch):
    _, ledger, _ = host
    repository, _, _, _, queued = shared_authority_intent
    original = lifecycle.build_integration_candidate
    barrier = threading.Barrier(2)

    def prepare(*args, **kwargs):
        candidate = original(*args, **kwargs)
        barrier.wait(timeout=10)
        return candidate

    monkeypatch.setattr(lifecycle, "build_integration_candidate", prepare)
    arguments = dict(owner="integration-owner", intent_id=queued["intent_id"],
                     target=repository.root / "candidate", branch="integration/lineage-candidate")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result()["decision"] for future in
                   [executor.submit(lifecycle.claim_integration, repository.repo, **arguments) for _ in range(2)]]
    assert results[0]["allocation_id"] == results[1]["allocation_id"]
    assert results[0]["candidate_oid"] == results[1]["candidate_oid"]
    assert len(ledger.read()["requests"]) == 2
    snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
    assert sum(record.role == "integration" for record in snapshot.records.values()) == 1
    assert snapshot.publication_lease is None


def request(tmp_path, task, device, *, size=100):
    return {"repository_id": task, "repository_path": str(tmp_path), "common_dir": str(tmp_path / ".git"),
            "target_path": str(tmp_path / task), "filesystem_device": device, "base_oid": "a" * 40,
            "arguments": {"task_key": task, "authority_key": task},
            "estimate": {"checkout_bytes": size, "checkout_inodes": 1, "growth_bytes": 0,
                         "growth_inodes": 0, "preservation_bytes": size, "preservation_inodes": 1}}


def execution(tmp_path, task, device):
    return {**request(tmp_path, task, device), "kind": "execution",
            "memory_bytes": 2 * resources.GIB, "allocation_id": task,
            "client_id": "agent-workspace", "session_id": task, "run_id": task}


def test_unmanaged_process_uses_the_same_budget_without_a_writer_lease(host, tmp_path):
    counters, ledger, _ = host
    target = tmp_path / "documents"
    target.mkdir()
    counters["memory"] = resources.MIN_AVAILABLE_MEMORY
    first = resources.host_execution("admit", target=target, session_id="documents", run_id="run")["decision"]
    key = first["request_ids"][0]
    assert first["code"] == "RESOURCE_WAIT" and ledger.read()["requests"][key]["state"] == "WAITING"
    counters["memory"] = 32 * resources.GIB
    ready = resources.host_execution("admit", target=target, session_id="documents", run_id="run")["decision"]
    assert ready["request_ids"] == [key]
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"], cwd=target, start_new_session=True)
    try:
        bound = resources.host_execution("bind", request_id=key, holder_pid=child.pid)["decision"]
        assert bound["code"] == "HOST_EXECUTION_BOUND"
        row = ledger.read()["requests"][key]
        assert row["lease_id"] is None and row["request"]["allocation_id"] is None
        with pytest.raises(ControllerError) as live:
            resources.host_execution("clear", request_id=key)
        assert live.value.code == "RUNTIME_OCCUPIED"
        with pytest.raises(ControllerError):
            resources.host_execution("cancel", request_id=key)
    finally:
        child.terminate()
        child.wait()
    resources.reconcile(repository_path=None, apply=True)
    assert ledger.read()["requests"][key]["state"] == "RELEASED"
    assert not (target / ".git").exists()


def test_new_host_admission_preserves_a_live_previous_execution(host, tmp_path):
    import os

    _, ledger, _ = host
    target = tmp_path / "session"
    target.mkdir()
    first = resources.host_execution("admit", target=target, session_id="first")["decision"][
        "request_ids"
    ][0]
    resources.host_execution("bind", request_id=first, holder_pid=os.getpid())
    before = ledger.read()["requests"][first]
    second = resources.host_execution("admit", target=target, session_id="second")["decision"]
    assert second["code"] == "RESOURCE_AVAILABLE"
    assert ledger.read()["requests"][first] == before


def test_stale_host_reservation_releases_while_same_path_runtime_stays_active(host, tmp_path):
    _, ledger, _ = host
    target = tmp_path / "shared"
    target.mkdir()
    stale = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                             cwd=target, start_new_session=True)
    active = None
    try:
        stale_key = resources.host_execution(
            "admit", target=target, session_id="stale", run_id="stale")["decision"]["request_ids"][0]
        resources.host_execution("bind", request_id=stale_key, holder_pid=stale.pid)
        active = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                                  cwd=target, start_new_session=True)
        active_key = resources.host_execution(
            "admit", target=target, session_id="active", run_id="active")["decision"]["request_ids"][0]
        resources.host_execution("bind", request_id=active_key, holder_pid=active.pid)
        stale.terminate()
        stale.wait(timeout=5)

        recovered = resources.reconcile(repository_path=None, apply=True)
        outcomes = {item["request_id"]: item for item in recovered["decision"]["outcomes"]}
        assert outcomes[stale_key]["result"]["code"] == "HOST_EXECUTION_CLEARED"
        assert outcomes[active_key]["result"]["code"] == "EXECUTION_OCCUPIED"
        rows = ledger.read()["requests"]
        assert rows[stale_key]["state"] == "RELEASED"
        assert rows[active_key]["state"] == "ALLOCATED"

        stale_row = rows[stale_key]
        repeated = resources.host_execution("clear", request_id=stale_key)["decision"]
        assert repeated["code"] == "HOST_EXECUTION_CLEARED"
        rows = ledger.read()["requests"]
        assert rows[stale_key] == stale_row
        assert rows[active_key]["state"] == "ALLOCATED"
        with pytest.raises(ControllerError) as live:
            resources.host_execution("clear", request_id=active_key)
        assert live.value.code == "RUNTIME_OCCUPIED"
    finally:
        if stale.poll() is None:
            stale.terminate()
            stale.wait(timeout=5)
        if active is not None:
            active.terminate()
            active.wait(timeout=5)


@pytest.mark.parametrize("mutate,code", [
    (lambda row: row.pop("runtime_bindings"), "ADMISSION_RECOVERY_REQUIRED"),
    (lambda row: row.update(lease_id=str(uuid.uuid4())), "WRITER_LEASE_HELD"),
])
def test_host_recovery_blocks_uncertain_identity_or_writer_lease(host, tmp_path, mutate, code):
    _, ledger, _ = host
    target = tmp_path / code.lower()
    target.mkdir()
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                             cwd=target, start_new_session=True)
    key = resources.host_execution(
        "admit", target=target, session_id=code.lower(), run_id="run")["decision"]["request_ids"][0]
    resources.host_execution("bind", request_id=key, holder_pid=child.pid)
    child.terminate()
    child.wait(timeout=5)
    with ledger.locked():
        state = ledger.read()
        mutate(state["requests"][key])
        ledger._save(state)

    with pytest.raises(ControllerError) as blocked:
        resources.host_execution("clear", request_id=key)
    assert blocked.value.code == code
    assert ledger.read()["requests"][key]["state"] == "ALLOCATED"


def test_host_recovery_blocks_unattributable_home_scope(host):
    _, ledger, _ = host
    key, _child = _host_reservation(host, "uncertain-home")
    before = ledger.read()["requests"][key]

    with pytest.raises(ControllerError) as blocked:
        resources.host_execution("clear", request_id=key)
    assert blocked.value.code == "ADMISSION_RECOVERY_REQUIRED"
    assert ledger.read()["requests"][key] == before


def test_host_waiter_cancels_by_original_operation_identity(host, tmp_path):
    counters, ledger, _ = host
    counters["memory"] = resources.MIN_AVAILABLE_MEMORY
    work = tmp_path / "work"
    work.mkdir()
    first = resources.host_execution("admit", target=work, session_id="cancel", run_id="run")["decision"]
    resources.host_execution("cancel", target=work, session_id="cancel", run_id="run")
    key = first["request_ids"][0]
    assert ledger.read()["requests"][key]["state"] == "CANCELLED"
    counters["memory"] = 32 * resources.GIB
    assert resources.reconcile(repository_path=None, apply=True)["decision"]["outcomes"] == []


def test_plan_is_read_only_and_does_not_allocate_a_reservation(host, tmp_path):
    _, ledger, device = host
    assert ledger.plan([request(tmp_path, "one", device)])["code"] == "RESOURCE_AVAILABLE"
    assert not ledger.root.exists()


def test_two_repositories_atomically_compete_for_shared_filesystem(host, tmp_path):
    counters, ledger, device = host
    counters["bytes"] = resources.MIN_FREE_BYTES + 200
    requests = [request(tmp_path, task, device) for task in ("one", "two")]
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda req: ledger.reserve([req]), requests))
    assert sorted(result["code"] for result in results) == ["RESOURCE_AVAILABLE", "RESOURCE_WAIT"]
    assert sorted(row["state"] for row in ledger.read()["requests"].values()) == ["RESERVED", "WAITING"]


def test_group_budget_is_all_or_none(host, tmp_path):
    counters, ledger, device = host
    counters["bytes"] = resources.MIN_FREE_BYTES + 300
    result = ledger.reserve([request(tmp_path, task, device) for task in ("one", "two")])
    assert result["code"] == "RESOURCE_WAIT"
    assert {row["state"] for row in ledger.read()["requests"].values()} == {"WAITING"}
    assert all(len(row["group"]) == 2 for row in ledger.read()["requests"].values())


@pytest.mark.parametrize("metric,value,blocker", [
    ("bytes", 0, "FILESYSTEM_BYTES"), ("inodes", 0, "FILESYSTEM_INODES"),
    ("memory", 0, "HOST_MEMORY_HEADROOM"), ("cpu", 90.0, "HOST_PRESSURE")])
def test_insufficient_resource_waits_without_occupying_writer(host, tmp_path, metric, value, blocker):
    counters, ledger, device = host
    counters[metric] = value
    req = execution(tmp_path, "one", device) if metric in {"memory", "cpu"} else request(tmp_path, "one", device)
    result = ledger.reserve([req])
    assert blocker in result["blockers"]
    assert next(iter(ledger.read()["requests"].values()))["state"] == "WAITING"


def test_pressure_hysteresis_requires_real_recovery(host, tmp_path):
    counters, ledger, device = host
    req = execution(tmp_path, "one", device)
    counters["cpu"] = 90
    assert ledger.reserve([req])["code"] == "RESOURCE_WAIT"
    counters["cpu"] = 45
    assert ledger.reserve([req])["code"] == "RESOURCE_WAIT"
    counters["cpu"] = 25
    assert ledger.reserve([req])["code"] == "RESOURCE_AVAILABLE"
    assert [entry["state"] for entry in next(iter(ledger.read()["requests"].values()))["history"]] == ["WAITING", "RESERVED"]


def test_changed_operation_request_does_not_duplicate_authority(host, tmp_path):
    _, ledger, device = host
    req = request(tmp_path, "one", device)
    ledger.reserve([req])
    with pytest.raises(ControllerError, match="different identity"):
        ledger.reserve([{**req, "base_oid": "b" * 40}])
    assert len(ledger.read()["requests"]) == 1


def test_checkout_and_execution_have_distinct_resource_budgets(host, tmp_path):
    counters, ledger, device = host
    counters["cpu"] = 90
    counters["memory"] = 0
    assert ledger.reserve([request(tmp_path, "checkout", device)])["code"] == "RESOURCE_AVAILABLE"
    assert ledger.reserve([execution(tmp_path, "run", device)])["code"] == "RESOURCE_WAIT"


def test_writer_only_ownership_neither_budgets_execution_nor_reuses_process_operation_key(host):
    import os


    counters, ledger, _ = host
    counters["memory"] = 0
    with GitRepository(linked=0, policy_text=POLICY) as repository:
        adopt_canonical(repository)
        for number in range(2):
            path, _ = acquire_one(repository, task=f"writer-{number}", authority=f"writer-{number}")
            result = writer_lease.acquire_writer_lease(repository.repo, target=path,
                client_id="coordinator", holder_pid=os.getpid(), run_id=f"task-{number}")
            assert result["decision"]["admission"] == "GRANTED"
        assert not any(row["request"].get("kind") == "execution" for row in ledger.read()["requests"].values())


def test_runtime_without_admission_fails_before_changing_allocation(host):
    import json
    import os
    from dataclasses import asdict

    from worktree_controller import lifecycle
    from worktree_controller.store import MetadataStore

    with GitRepository(linked=0, policy_text=POLICY) as repository:
        adopt_canonical(repository)
        path, allocated = acquire_one(repository)
        allocation = allocated["decision"]["allocation_id"]
        writer_lease.acquire_writer_lease(repository.repo, target=path,
            client_id="coordinator", holder_pid=os.getpid(), session_id="session", run_id="run")
        store = MetadataStore(inventory(repository.repo).common_dir)
        before = store.load().records[allocation]
        lease = store.load().writer_leases[allocation]
        binding = writer_lease.RUNTIME_BINDING_PREFIX + json.dumps({"holder": asdict(lease.holder),
            "lease_id": lease.lease_id, "kind": "test"})
        with pytest.raises(ControllerError) as error:
            lifecycle.update_external_binding(repository.repo, target=path, expected_record_version=1,
                owner="module-owner", binding=binding, action="add")
        assert error.value.code == "ADMISSION_RECOVERY_REQUIRED"
        assert store.load().records[allocation] == before


@pytest.mark.parametrize("reappeared", [False, True])
def test_old_writer_budget_uses_formal_release_evidence_even_when_coordinator_is_live(host, monkeypatch, reappeared):
    import os

    from tests.test_controlled import handoff_one
    from worktree_controller import lifecycle
    from worktree_controller.store import MetadataStore

    _, ledger, _ = host
    with GitRepository(linked=0, policy_text=POLICY) as repository:
        adopt_canonical(repository)
        path, allocated = acquire_one(repository)
        allocation = allocated["decision"]["allocation_id"]
        admitted = resources.admit_execution(repository.repo, target=path, owner="module-owner",
            client_id="coordinator", session_id="session", run_id="run")["decision"]
        writer_lease.acquire_writer_lease(repository.repo, target=path,
            client_id="coordinator", holder_pid=os.getpid(), session_id="session", run_id="run")
        store = MetadataStore(inventory(repository.repo).common_dir)
        lease = store.load().writer_leases[allocation]
        # Reproduce the previous writer-only allocation and a lost settlement.
        resources.bind_execution(store.load().repository_id, allocation, lease.holder, lease.lease_id, required=True)
        writer_lease.release_writer_lease(repository.repo, target=path, holder=lease.holder, lease_id=lease.lease_id)
        handoff_one(repository, path)
        with monkeypatch.context() as lost:
            lost.setattr(resources, "release_allocation_budget", lambda *args: None)
            lifecycle.release(repository.repo, target=path, expected_record_version=2, owner_release="module-owner")
        key = admitted["request_ids"][0]
        before = ledger.read()["requests"][key]
        assert before["state"] == "ALLOCATED" and writer_lease.holder_is_live(lease.holder)
        evidence = {p: p.read_bytes() for p in store.root.rglob("*.json")}
        if reappeared:
            path.mkdir()
            with pytest.raises(ControllerError) as error:
                resources._reconcile_execution(before, apply=True)
            assert error.value.code == "ADMISSION_RECOVERY_REQUIRED"
            assert ledger.read()["requests"][key] == before
        else:
            planned = resources._reconcile_execution(before, apply=False)
            assert planned["allocation_released"] is True
            assert ledger.read()["requests"][key] == before
            result = resources._reconcile_execution(before, apply=True)
            assert result["code"] == "EXECUTION_RESOURCES_RELEASED" and result["task_completed"] is False
            after = ledger.read()["requests"][key]
            assert after["state"] == "RELEASED" and after["release_receipt_id"] == result["release_receipt_id"]
            assert after["history"][:-1] == before["history"]
        assert {p: p.read_bytes() for p in store.root.rglob("*.json")} == evidence


def test_memory_reservations_are_atomic_across_repositories(host, tmp_path):
    counters, ledger, device = host
    counters["memory"] = resources.MIN_AVAILABLE_MEMORY + 2 * resources.GIB
    requests = [execution(tmp_path, task, device) for task in ("one", "two")]
    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(lambda req: ledger.reserve([req]), requests))
    assert sorted(value["code"] for value in outcomes) == ["RESOURCE_AVAILABLE", "RESOURCE_WAIT"]
    assert len(ledger.read()["requests"]) == 2


def test_same_allocation_launches_serialize_before_writer_acquisition_and_resume_original_waiter(host):
    from worktree_controller.store import MetadataStore

    _, ledger, _ = host
    with GitRepository(linked=0, policy_text=POLICY) as repository:
        adopt_canonical(repository)
        path, _ = acquire_one(repository)

        def admit(session):
            return resources.admit_execution(repository.repo, target=path, owner="module-owner",
                client_id="agent-workspace", session_id=session, run_id=session)["decision"]

        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(admit, ("one", "two")))
        assert sorted(row["code"] for row in outcomes) == ["RESOURCE_AVAILABLE", "RESOURCE_WAIT"]
        winner = next(row for row in outcomes if row["code"] == "RESOURCE_AVAILABLE")
        waiter = next(row for row in outcomes if row["code"] == "RESOURCE_WAIT")
        assert "ALLOCATION_OCCUPIED" in waiter["blockers"]
        assert not MetadataStore(inventory(repository.repo).common_dir).load().writer_leases
        resources.admit_execution(repository.repo, target=path, owner="module-owner",
            client_id="agent-workspace", session_id=winner["session_id"], run_id=winner["run_id"], cancel=True)
        resumed = admit(waiter["session_id"])
        assert resumed["code"] == "RESOURCE_AVAILABLE" and resumed["request_ids"] == waiter["request_ids"]
        assert ledger.read()["requests"][waiter["request_ids"][0]]["state"] == "RESERVED"


def test_creation_rate_wait_is_temporary_and_does_not_limit_batch_size(host, tmp_path, monkeypatch):
    _, ledger, device = host
    stamp = [10**12]
    monkeypatch.setattr(resources.time, "time_ns", lambda: stamp[0])
    requests = [request(tmp_path, f"task-{index}", device) for index in range(32)]
    assert ledger.reserve(requests)["code"] == "RESOURCE_AVAILABLE"
    next_request = request(tmp_path, "next", device)
    assert "CREATION_RATE" in ledger.reserve([next_request])["blockers"]
    stamp[0] += 17 * 10**9
    assert ledger.reserve([next_request])["code"] == "RESOURCE_AVAILABLE"


def test_lost_exit_notification_reconciles_budget_without_completing_or_deleting_task(host):
    import json
    import subprocess
    import sys
    from dataclasses import asdict

    from worktree_controller import lifecycle
    from worktree_controller.store import MetadataStore

    _, ledger, _ = host
    with GitRepository(linked=0, policy_text=POLICY) as repository:
        adopt_canonical(repository)
        path, allocated = acquire_one(repository, task="execution", authority="execution", owner="test-owner")
        allocation = allocated["decision"]["allocation_id"]
        store = MetadataStore(inventory(repository.repo).common_dir)
        decision = resources.admit_execution(repository.repo, target=path, owner="test-owner",
            client_id="agent-workspace", session_id="session", run_id="run")["decision"]
        assert decision["code"] == "RESOURCE_AVAILABLE"
        child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"], cwd=path, start_new_session=True)
        try:
            writer_lease.acquire_writer_lease(repository.repo, target=path, owner="test-owner",
                client_id="agent-workspace", session_id="session", run_id="run", holder_pid=child.pid)
            assert ledger.read()["requests"][decision["request_ids"][0]]["state"] == "RESERVED"
            lease = store.load().writer_leases[allocation]
            binding = writer_lease.RUNTIME_BINDING_PREFIX + json.dumps({"holder": asdict(lease.holder),
                "lease_id": lease.lease_id, "kind": "test"})
            lifecycle.update_external_binding(repository.repo, target=path, expected_record_version=1,
                                              owner="test-owner", binding=binding, action="add")
            assert ledger.read()["requests"][decision["request_ids"][0]]["state"] == "ALLOCATED"
            assert resources.reconcile(apply=True)["decision"]["outcomes"][-1]["result"]["code"] == "EXECUTION_OCCUPIED"
            (path / "unfinished.txt").write_text("unfinished, must be preserved")
        finally:
            child.terminate()
            child.wait(timeout=5)
        result = resources.reconcile(apply=True)
        assert any(item.get("result", {}).get("code") == "EXECUTION_RESOURCES_RELEASED"
                   for item in result["decision"]["outcomes"]), result
        assert (path / "unfinished.txt").read_text() == "unfinished, must be preserved"
        assert store.load().records[allocation].lifecycle != "RELEASED"
        row = ledger.read()["requests"][decision["request_ids"][0]]
        assert row["state"] == "RELEASED"


def test_cancel_waiter_never_becomes_an_execution_on_resource_recovery(host, tmp_path):
    counters, ledger, device = host
    counters["memory"] = 0
    req = execution(tmp_path, "cancelled", device)
    assert ledger.reserve([req])["code"] == "RESOURCE_WAIT"
    assert ledger.cancel_execution(req)["code"] == "EXECUTION_ADMISSION_CANCELLED"
    counters["memory"] = 32 * resources.GIB
    with pytest.raises(ControllerError):
        ledger.reserve([req])
    assert ledger.read()["requests"][resources.fingerprint(req)]["state"] == "CANCELLED"


def test_materialized_shared_inode_is_not_counted_as_new_private_storage(host, tmp_path):
    import os

    _, _, device = host
    source = tmp_path / "cache"
    source.write_bytes(b"x" * 8192)
    tree = tmp_path / "checkout"
    tree.mkdir()
    os.link(source, tree / "dependency")
    (tree / "outside").symlink_to(tmp_path)
    assert resources.materialized(tree, device)[0] == (tree / "outside").lstat().st_blocks * 512
    assert source.read_bytes() == b"x" * 8192


def test_acquire_beyond_former_physical_and_writer_limits(host):
    with GitRepository(linked=0, policy_text=POLICY) as repository:
        adopt_canonical(repository)
        for index in range(7):
            _, result = acquire_one(repository, task=f"task-{index}", authority=f"task-{index}")
            assert result["decision"]["code"] == "ACQUIRED", result
        decision = plan(repository.repo, task_key="another", authority_key="another", module="test", role="feature")
        assert decision["decision"]["code"] == "CREATE_ALLOWED", decision
        assert decision["repository"]["checkout_limits_enforced"] is False
        assert not any(warning["code"] in {"BUDGET_VIOLATION", "FEATURE_CAPACITY_VIOLATION", "ORDINARY_POOL_VIOLATION"} for warning in decision["warnings"])


def test_wait_survives_restart_and_creates_once_without_writer(host):
    counters, ledger, _ = host
    with GitRepository(linked=0, policy_text=POLICY) as repository:
        adopt_canonical(repository)
        counters["bytes"] = 0
        target, result = acquire_one(repository, task="queued", authority="queued")
        assert result["decision"]["code"] == "RESOURCE_WAIT"
        assert not target.exists()
        assert len(inventory(repository.repo).worktrees) == 1
        counters["bytes"] = 10**12
        # New coordinator/ledger instance, no in-memory request or polling loop.
        resumed = resources.reconcile(apply=True)
        assert resumed["decision"]["outcomes"][0]["result"]["code"] == "ACQUIRED", resumed
        assert target.is_dir()
        again = resources.reconcile(apply=True)
        assert again["decision"]["outcomes"][0]["state"] == "ALLOCATED", again
        assert len(inventory(repository.repo).worktrees) == 2
        assert not list((repository.repo / ".git/worktree-controller/v1/writer-leases").glob("*.json"))
        assert len(ledger.read()["requests"]) == 1


def test_cross_repository_queue_reserves_and_resumes_together(host):
    counters, _, _ = host
    with GitRepository(linked=0, policy_text=POLICY) as left, GitRepository(linked=0, policy_text=POLICY) as right:
        requests = []
        for index, repository in enumerate((left, right)):
            adopt_canonical(repository)
            requests.append({"repository": str(repository.repo), "task_key": f"multi-{index}",
                             "authority_key": f"multi-{index}", "module": "test", "role": "feature",
                             "owner": "test-owner", "branch": f"feature/multi-{index}", "base_ref": "HEAD",
                             "target": str(repository.root / "checkout")})
        counters["bytes"] = 0
        assert acquire_across(requests)["decision"]["code"] == "RESOURCE_WAIT"
        assert len(inventory(left.repo).worktrees) == len(inventory(right.repo).worktrees) == 1
        counters["bytes"] = 10**12
        result = resources.reconcile(apply=True)
        assert result["decision"]["outcomes"][0]["result"]["code"] == "ACROSS_ACQUIRED", result
        assert len(inventory(left.repo).worktrees) == len(inventory(right.repo).worktrees) == 2


def _typed(host, key, *, digest=True, terminal=True, mutate=None, evidence=None):
    """Drive `clear --recover-dead-holder` with caller-supplied authority."""
    _, ledger, _ = host
    row = ledger.read()["requests"][key]
    holder = row["holder"]
    if mutate is not None:
        with ledger.locked():
            state = ledger.read()
            mutate(state["requests"][key])
            ledger._save(state)
        row = ledger.read()["requests"][key]
        holder = row["holder"]
    if evidence is None and terminal:
        evidence = {"kind": "aw-terminal-record", "status": "EXITED",
                    "ended_at": "2026-09-23T00:00:00+08:00",
                    "session_id": row["request"]["session_id"],
                    "run_id": row["request"]["run_id"], "end_reason": "shell_exit"}
    return resources.host_execution(
        "clear", request_id=key, recover_dead_holder=True,
        expected_holder_sha256=resources.canonical_holder_digest(holder) if digest else "0" * 64,
        terminal_evidence=evidence)


def _host_reservation(host, session, *, live=False):
    """Inject a historical home-scoped ALLOCATED execution row.

    The M2 producer invariant makes new home admissions impossible, so
    recovery scenarios for pre-existing rows are seeded directly while
    recovery itself still runs through the real typed verb. For live=True the
    recorded holder process stays alive and is returned for cleanup.
    """
    import os
    import time
    from dataclasses import asdict

    from worktree_controller.writer_lease import describe_holder

    _, ledger, _ = host
    target = Path.home()
    info = target.stat()
    request = resources.execution_request(None, f"host:{os.getuid()}", None, target,
                                          client_id="agent-workspace", session_id=session,
                                          run_id="run-" + session)
    request.update(owner=os.getuid(), path_identity=[info.st_dev, info.st_ino])
    key = resources.fingerprint(request)
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(300)"],
                             cwd=target, start_new_session=True)
    holder = asdict(describe_holder(child.pid, client_id="agent-workspace",
                                    session_id=session, run_id="run-" + session))
    if not live:
        child.terminate()
        child.wait(timeout=5)  # reaped: the recorded pid is provably absent
    binding = RUNTIME_BINDING_PREFIX + json.dumps(
        {"holder": holder, "lease_id": None, "kind": "command"},
        sort_keys=True, separators=(",", ":"))
    now = time.time_ns()
    with ledger.locked():
        state = ledger.read()
        state["requests"][key] = {"request": request, "state": "ALLOCATED", "holder": holder,
                                  "lease_id": None, "runtime_bindings": [binding],
                                  "created_at_ns": now, "updated_at_ns": now,
                                  "decision": {}, "history": [{"state": "ALLOCATED", "at_ns": now}]}
        ledger._save(state)
    return key, child


def _home_reservation(host, session="dead-home"):
    """A home-scoped reservation whose recorded holder has already exited."""
    key, _child = _host_reservation(host, session)
    return key


def test_typed_recovery_releases_provably_dead_home_holder(host, tmp_path):
    _, ledger, _ = host
    key = _home_reservation(host)
    with pytest.raises(ControllerError):
        resources.host_execution("clear", request_id=key)  # unqualified stays blocked

    result = _typed(host, key)

    assert result["decision"]["code"] == "HOST_EXECUTION_CLEARED"
    assert ledger.read()["requests"][key]["state"] == "RELEASED"
    recovery = result["decision"]["recovery"]
    saved = json.loads(Path(recovery["path"]).read_text())
    row = ledger.read()["requests"][key]
    assert saved["request_id"] == key
    assert saved["contract"] == "host-execution-recovery/v1"
    assert saved["original_holder_digest"] == resources.canonical_holder_digest(row["holder"])
    assert saved["dead_holder_proof"]["recorded_pid"] == row["holder"]["pid"]
    assert saved["dead_holder_proof"]["pid_present"] is False
    assert saved["terminal_evidence"]["session_id"] == row["request"]["session_id"]
    assert saved["recovered_at_ns"] > 0


def test_typed_recovery_blocks_live_holder(host):
    _, ledger, _ = host
    key, child = _host_reservation(host, "live-home", live=True)
    try:
        with pytest.raises(ControllerError) as blocked:
            _typed(host, key)
        assert blocked.value.code == "RUNTIME_OCCUPIED"
        assert ledger.read()["requests"][key]["state"] == "ALLOCATED"
        assert not (ledger.root / "host-recoveries").exists() or not list(
            (ledger.root / "host-recoveries").glob("*.json"))
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_typed_recovery_distinguishes_a_reused_pid_without_touching_it(host):
    """A live process on a recycled pid is not the recorded holder and stays alive."""
    _, ledger, _ = host
    key = _home_reservation(host, session="reused-home")
    impersonator = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                                    cwd=Path.home(), start_new_session=True)
    with ledger.locked():
        state = ledger.read()
        row = state["requests"][key]
        row["holder"]["pid"] = impersonator.pid
        row["holder"]["start_ticks"] = row["holder"]["start_ticks"] + 10_000
        binding = json.loads(row["runtime_bindings"][0][len(RUNTIME_BINDING_PREFIX):])
        binding["holder"] = dict(row["holder"])
        row["runtime_bindings"] = [RUNTIME_BINDING_PREFIX + json.dumps(
            binding, sort_keys=True, separators=(",", ":"))]
        ledger._save(state)

    result = _typed(host, key)

    assert result["decision"]["code"] == "HOST_EXECUTION_CLEARED"
    saved = json.loads(Path(result["decision"]["recovery"]["path"]).read_text())
    proof = saved["dead_holder_proof"]
    assert proof["pid_present"] is True and proof["pid_reused"] is True
    assert impersonator.poll() is None  # the new process was never signalled
    impersonator.terminate()
    impersonator.wait(timeout=5)


@pytest.mark.parametrize("kwargs,code", [
    ({"digest": False}, "ADMISSION_RECOVERY_REQUIRED"),
    ({"terminal": False}, "ADMISSION_RECOVERY_REQUIRED"),
])
def test_typed_recovery_requires_its_own_evidence(host, kwargs, code):
    _, ledger, _ = host
    key = _home_reservation(host, session="evidence-" + list(kwargs)[0])
    with pytest.raises(ControllerError) as blocked:
        _typed(host, key, **kwargs)
    assert blocked.value.code == code
    assert ledger.read()["requests"][key]["state"] == "ALLOCATED"


def test_typed_recovery_rejects_terminal_identity_mismatch(host):
    """Terminal evidence must belong to this reservation's own session and run."""
    _, ledger, _ = host
    key = _home_reservation(host, session="mismatch-home")
    row = ledger.read()["requests"][key]
    foreign = {"kind": "aw-terminal-record", "status": "EXITED",
               "ended_at": "2026-09-23T00:00:00+08:00",
               "session_id": row["request"]["session_id"],
               "run_id": "another-run", "end_reason": "shell_exit"}

    with pytest.raises(ControllerError) as blocked:
        _typed(host, key, evidence=foreign)
    assert blocked.value.code == "ADMISSION_RECOVERY_REQUIRED"
    assert ledger.read()["requests"][key]["state"] == "ALLOCATED"


@pytest.mark.parametrize("boot", ["", "unknown-boot"])
def test_typed_recovery_blocks_unknown_boot_identity(host, boot):
    _, ledger, _ = host
    key = _home_reservation(host, session="boot-" + (boot or "empty"))

    def unknown(row):
        row["holder"]["boot_id"] = boot
        binding = json.loads(row["runtime_bindings"][0][len(RUNTIME_BINDING_PREFIX):])
        binding["holder"]["boot_id"] = boot
        row["runtime_bindings"] = [RUNTIME_BINDING_PREFIX + json.dumps(
            binding, sort_keys=True, separators=(",", ":"))]

    with pytest.raises(ControllerError) as blocked:
        _typed(host, key, mutate=unknown)
    assert blocked.value.code in {"ADMISSION_RECOVERY_REQUIRED", "INVALID_HOLDER"}
    assert ledger.read()["requests"][key]["state"] == "ALLOCATED"


def test_typed_recovery_is_idempotent_and_immutable(host):
    _, ledger, _ = host
    key = _home_reservation(host, session="twice-home")

    first = _typed(host, key)["decision"]
    evidence_path = Path(first["recovery"]["path"])
    before = evidence_path.read_bytes()
    again = resources.host_execution(
        "clear", request_id=key, recover_dead_holder=True,
        expected_holder_sha256="0" * 64, terminal_evidence=None)

    assert again["decision"]["code"] == "HOST_EXECUTION_CLEARED"
    assert evidence_path.read_bytes() == before
    assert ledger.read()["requests"][key]["state"] == "RELEASED"


def test_typed_recovery_leaves_other_home_reservations_alone(host):
    _, ledger, _ = host
    chosen = _home_reservation(host, session="chosen")
    other = _home_reservation(host, session="other")

    assert _typed(host, chosen)["decision"]["code"] == "HOST_EXECUTION_CLEARED"

    rows = ledger.read()["requests"]
    assert rows[chosen]["state"] == "RELEASED"
    assert rows[other]["state"] == "ALLOCATED"
    with pytest.raises(ControllerError) as still:
        resources.host_execution("clear", request_id=other)
    assert still.value.code == "ADMISSION_RECOVERY_REQUIRED"


@pytest.mark.parametrize("settlement", ["at-release", "lost"])
def test_admitted_but_never_bound_execution_settles_with_its_released_allocation(host, monkeypatch, settlement):
    from tests.test_controlled import handoff_one

    _, ledger, _ = host
    with GitRepository(linked=0, policy_text=POLICY) as repository:
        adopt_canonical(repository)
        path, allocated = acquire_one(repository)
        allocation = allocated["decision"]["allocation_id"]
        admitted = resources.admit_execution(repository.repo, target=path, owner="module-owner",
            client_id="agent-workspace", session_id="never-started", run_id=None)["decision"]
        key = admitted["request_ids"][0]
        assert admitted["code"] == "RESOURCE_AVAILABLE" and ledger.read()["requests"][key]["state"] == "RESERVED"
        # While the allocation is held a launch may still be in flight: nothing settles it.
        resources.reconcile_finished_executions()
        outcome = next(item for item in resources.reconcile(apply=True)["decision"]["outcomes"]
                       if item["request_id"] == key)
        assert outcome["result"]["code"] == "RESOURCE_AVAILABLE"
        assert ledger.read()["requests"][key]["state"] == "RESERVED"
        handoff_one(repository, path)
        with monkeypatch.context() as lost:
            if settlement == "lost":
                lost.setattr(resources, "release_allocation_budget", lambda *args: None)
            lifecycle.release(repository.repo, target=path, expected_record_version=2, owner_release="module-owner")
        if settlement == "lost":
            before = ledger.read()["requests"][key]
            assert before["state"] == "RESERVED"
            planned = next(item for item in resources.reconcile()["decision"]["outcomes"]
                           if item["request_id"] == key)
            assert planned["result"]["code"] == "EXECUTION_RECONCILIATION_PENDING"
            assert ledger.read()["requests"][key] == before
            resources.reconcile_finished_executions()
        after = ledger.read()["requests"][key]
        receipts = MetadataStore(inventory(repository.repo).common_dir).load().release_receipts.values()
        assert after["state"] == "RELEASED" and after.get("holder") is None
        assert after["release_receipt_id"] == next(r.receipt_id for r in receipts if r.allocation_id == allocation)


def test_budget_of_a_vanished_repository_settles_only_on_positive_absence(host, tmp_path):
    import shutil

    _, ledger, device = host
    roots = {name: tmp_path / name for name in ("gone", "orphan-checkout", "kept")}
    requests = {name: request(root, name, device) for name, root in roots.items()}
    for name, root in roots.items():
        (root / ".git").mkdir(parents=True)
        assert ledger.reserve([requests[name]])["code"] == "RESOURCE_AVAILABLE"
    terminal_execution = {**execution(roots["gone"], "gone", device), "allocation_id": None}
    terminal_execution.pop("filesystem_device")  # Actual executions carry no checkout FS budget.
    ledger.reserve([terminal_execution])
    ledger.cancel_execution(terminal_execution)
    shutil.rmtree(roots["gone"])
    shutil.rmtree(roots["orphan-checkout"] / ".git")
    (roots["orphan-checkout"] / "orphan-checkout").mkdir()
    keys = {name: resources.fingerprint(value) for name, value in requests.items()}
    before = ledger.read()
    planned = {item["request_id"]: item for item in resources.reconcile()["decision"]["outcomes"]}
    assert planned[keys["gone"]]["terminal_evidence"] == "vanished-repository/v1"
    assert ledger.read() == before
    assert resources._vanished_repository({**requests["gone"], "filesystem_device": device + 1}) is None
    decision = resources.reconcile(apply=True)["decision"]
    assert str(roots["gone"]) not in {item["repository_path"] for item in decision["maintenance"]}
    rows = ledger.read()["requests"]
    gone = rows[keys["gone"]]
    assert gone["state"] == "RELEASED" and gone["history"][-1]["terminal_evidence"] == "vanished-repository/v1"
    assert gone["vanished_evidence"]["absent_paths"][2] == requests["gone"]["target_path"]
    assert rows[keys["orphan-checkout"]]["state"] == rows[keys["kept"]]["state"] == "RESERVED"
    repeated = resources.reconcile(apply=True)["decision"]
    assert str(roots["gone"]) not in {item["repository_path"] for item in repeated["maintenance"]}
    assert len(repeated["maintenance"]) == 2
    with pytest.raises(ControllerError) as refused:
        ledger.reserve([requests["gone"]])
    assert refused.value.code == "ADMISSION_RECOVERY_REQUIRED"


def test_live_repository_with_only_released_admission_remains_enrolled(host):
    _, ledger, _ = host
    with GitRepository(linked=0, policy_text=POLICY) as repository:
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        store = MetadataStore(inventory(repository.repo).common_dir)
        record = next(record for record in store.load().records.values() if record.task_key == "new-task")
        lifecycle.complete_no_change(repository.repo, target=target, owner="module-owner",
            expected_record_version=record.record_version)
        record = store.load().records[record.allocation_id]
        lifecycle.release(repository.repo, target=target, owner_release="module-owner",
            expected_record_version=record.record_version)
        assert all(row["state"] == "RELEASED" for row in ledger.read()["requests"].values())
        decision = resources.reconcile(apply=True)["decision"]
        assert decision["outcomes"] == []
        assert decision["maintenance"] == [{"repository_id": store.load().repository_id,
                                           "repository_path": str(repository.repo)}]


def _vanished_request(tmp_path, device, name="gone"):
    import shutil

    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    value = request(root, name, device)
    shutil.rmtree(root)
    return value


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("same_target", [False, True])
def test_enrollment_only_shares_absence_with_exact_terminal_execution(
    host, tmp_path, terminal, same_target
):
    _, ledger, device = host
    checkout = _vanished_request(tmp_path, device)
    ledger.reserve([checkout])
    execution_request = {**execution(Path(checkout["repository_path"]), "gone", device),
                         "allocation_id": None}
    execution_request.pop("filesystem_device")
    if not same_target:
        execution_request["target_path"] = str(tmp_path / "different")
    ledger.reserve([execution_request])
    if terminal:
        ledger.cancel_execution(execution_request)
    key = resources.fingerprint(execution_request)
    before = ledger.read()["requests"][key]
    decision = resources.reconcile(apply=True)["decision"]
    assert (decision["maintenance"] == []) is (terminal and same_target)
    assert ledger.read()["requests"][key] == before


@pytest.mark.parametrize("failure", [errno.EACCES, errno.EIO, errno.ENOTDIR, errno.ELOOP])
@pytest.mark.parametrize("field", ["common_dir", "repository_path", "target_path"])
def test_failed_absence_observation_never_settles_a_budget(host, tmp_path, monkeypatch, failure, field):
    _, _, device = host
    value = _vanished_request(tmp_path, device)
    assert resources._vanished_repository(value) is not None
    real = os.lstat

    def observe(path, *args, **kwargs):
        if str(path) == value[field]:
            raise OSError(failure, os.strerror(failure), str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(resources.os, "lstat", observe)
    assert resources._vanished_repository(value) is None


@pytest.mark.parametrize("failure", [errno.EACCES, errno.EIO])
def test_failed_ancestor_observation_never_settles_a_budget(host, tmp_path, monkeypatch, failure):
    _, _, device = host
    value = _vanished_request(tmp_path, device)
    real = os.lstat

    def observe(path, *args, **kwargs):
        if Path(path) == tmp_path:
            raise OSError(failure, os.strerror(failure), str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(resources.os, "lstat", observe)
    assert resources._vanished_repository(value) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_unsearchable_parent_keeps_the_reservation_through_the_queue(host, tmp_path):
    _, ledger, device = host
    locked = tmp_path / "locked"
    root = locked / "repo"
    (root / ".git").mkdir(parents=True)
    value = request(root, "locked", device)
    assert ledger.reserve([value])["code"] == "RESOURCE_AVAILABLE"
    key = resources.fingerprint(value)
    locked.chmod(0)
    try:
        assert not os.path.lexists(root)  # what 1.19.4 took as absence
        assert resources._vanished_repository(value) is None
        resources.reconcile(apply=True)
        assert ledger.read()["requests"][key]["state"] == "RESERVED"
    finally:
        locked.chmod(0o700)


def test_ancestor_on_another_filesystem_or_a_symlink_keeps_the_reservation(host, tmp_path):
    _, _, device = host
    value = _vanished_request(tmp_path, device)
    assert resources._vanished_repository({**value, "filesystem_device": device + 1}) is None
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = tmp_path / "link"
    link.symlink_to(elsewhere)
    moved = {**value, "repository_path": str(link / "missing" / "repo")}
    assert resources._vanished_repository(moved) is None
