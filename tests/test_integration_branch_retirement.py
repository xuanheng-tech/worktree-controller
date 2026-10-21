from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

import pytest

from tests.test_auto_release import POLICY_MANUAL, _publish_source
from tests.test_branch_retirement import _released_source
from tests.test_branch_retirement_execution import _authorize, _cas, _result
from tests.test_controlled import _configure_remote, adopt_canonical
from tests.test_integration_pipeline import _claim, _enqueue, _publish, _source, _validate
from tests.test_shadow import GitRepository, git
from worktree_controller import branch_retirement
from worktree_controller import retirement_execution as execution
from worktree_controller.branch_retirement import (
    branch_retirement_status,
    complete_branch_retirement,
    plan_branch_retirement,
    verify_branch_retirement,
)
from worktree_controller.cli import main
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import (
    _release_plan_id,
    abandon_integration_intent,
    enqueue_integration,
    reconcile_publication_receipts,
    release,
)
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore


def _released_candidate(repository: GitRepository, name: str = "candidate") -> tuple[str, str]:
    adopt_canonical(repository)
    _configure_remote(repository)
    _source, _allocation, _head, candidate, published = _publish_source(
        repository,
        source_task=f"{name}-source",
        integration_task=f"{name}-integration",
    )
    assert not candidate.exists()
    snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
    receipt = snapshot.publication_receipts[str(published["decision"]["receipt_id"])]
    assert snapshot.records[receipt.allocation_id].role == "integration"
    assert snapshot.records[receipt.allocation_id].lifecycle == "RELEASED"
    return receipt.allocation_id, receipt.candidate_oid


def _owner_released_candidate(repository: GitRepository) -> tuple[str, str]:
    adopt_canonical(repository)
    _configure_remote(repository)
    _path, source_allocation, _head = _source(
        repository, task="owner-source", relative_path="owner.txt", content="owner\n"
    )
    queued = _enqueue(repository, task="owner-integration", source_allocation=source_allocation)
    candidate, _claimed = _claim(
        repository, task="owner-integration", intent_id=queued["decision"]["intent_id"]
    )
    _validate(repository, candidate, "owner-validation")
    published = _publish(repository, candidate, auto_release=False)
    store = MetadataStore(inventory(repository.repo).common_dir)
    publication = store.load().publication_receipts[published["decision"]["receipt_id"]]
    record = store.load().records[publication.allocation_id]
    released = release(
        repository.repo,
        target=candidate,
        expected_record_version=record.record_version,
        owner_release=record.owner,
    )
    assert released["decision"]["code"] == "RELEASED"
    assert not candidate.exists()
    return publication.allocation_id, publication.receipt_id


def _retired_candidate(
    repository: GitRepository, tmp_path: Path, *, remote_published: bool = False
) -> tuple[dict, Path]:
    allocation, _head = _released_candidate(repository)
    store = MetadataStore(inventory(repository.repo).common_dir)
    branch = next(
        receipt.branch for receipt in store.load().release_receipts.values()
        if receipt.allocation_id == allocation
    )
    if remote_published:
        git(repository.repo, "push", "origin", f"refs/heads/{branch}:refs/heads/{branch}")
    decision = plan_branch_retirement(
        repository.repo, allocation_id=allocation, remote="origin", integrated_into="main"
    )["decision"]
    assert decision["code"] == "BRANCH_RETIREMENT_READY"
    plan = decision["plan"]
    authorized = _authorize(repository, plan)
    assert authorized["code"] == "RETIREMENT_AUTHORIZED"
    grant = authorized["authorization"]
    assert _cas(repository, grant["ref"], grant["expected_oid"]) == 0
    settled = complete_branch_retirement(
        repository.repo,
        authorization_id=grant["authorization_id"],
        token=grant["token"],
        result_path=_result(repository, grant, execution.REF_REMOVED_AT_EXPECTED_OID, tmp_path),
    )["decision"]
    assert settled["code"] == "RETIREMENT_EXECUTION_COMPLETE"
    return plan, store.branch_retirement_receipts_dir / f"{plan['plan_id']}-local.json"


@pytest.mark.parametrize("remote_published", [False, True], ids=["unpublished", "published"])
def test_local_closeout_keeps_remote_observation_and_receipt_scope_separate(
    tmp_path: Path, remote_published: bool
) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan, receipt_path = _retired_candidate(
            repository, tmp_path, remote_published=remote_published
        )
        original_receipt = receipt_path.read_bytes()
        for _ in range(2):
            local = verify_branch_retirement(
                repository.repo, plan_id=plan["plan_id"], operation="local"
            )["decision"]
            assert local["code"] == "RETIREMENT_OPERATION_COMPLETE"
            assert local["receipt"]["plan_id"] == plan["plan_id"]
            assert local["receipt"]["operation"] == "local"
            assert local["receipt"]["provenance"] == "AUTHORIZED_EXECUTION"
            assert local["minimum_revalidation"]["ref_state"] == "ABSENT"
            assert local["blockers"] == []
            status = branch_retirement_status(repository.repo, plan_id=plan["plan_id"])["decision"]
            assert status["code"] == "BRANCH_RETIREMENT_PARTIAL"
            assert status["completed_operations"] == ["local"]
            assert status["pending_operations"] == ["remote"]
            assert status["receipts"]["remote"] is None
        remote = verify_branch_retirement(
            repository.repo, plan_id=plan["plan_id"], operation="remote"
        )["decision"]
        live = subprocess.run(
            ["git", "-C", str(repository.repo), "ls-remote", "--exit-code", "origin",
             f"refs/heads/{plan['branch']}"],
            capture_output=True, text=True, check=False,
        )
        assert not live.stderr
        if remote_published:
            assert live.returncode == 0
            assert live.stdout.strip() == f"{plan['expected_oid']}\trefs/heads/{plan['branch']}"
            assert remote["code"] == "RETIREMENT_EXECUTABLE"
        else:
            assert live.returncode == 2 and not live.stdout
            assert remote["code"] == "RETIREMENT_BLOCKED"
            assert "ABSENT_UNATTESTED" in remote["blockers"]
            assert _authorize(repository, plan, operation="remote")["authorization"] is None
        assert receipt_path.read_bytes() == original_receipt


@pytest.mark.parametrize("drift", ["same-oid", "different-oid", "checked-out"])
def test_historical_receipt_cannot_close_a_recreated_local_ref(tmp_path: Path, drift: str) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan, receipt_path = _retired_candidate(repository, tmp_path)
        original_receipt = receipt_path.read_bytes()
        head = (
            git(repository.repo, "rev-parse", f"{plan['expected_oid']}^")
            if drift == "different-oid" else plan["expected_oid"]
        )
        git(repository.repo, "update-ref", f"refs/heads/{plan['branch']}", head)
        if drift == "checked-out":
            git(repository.repo, "worktree", "add", str(repository.root / "recreated"), plan["branch"])
        decision = verify_branch_retirement(
            repository.repo, plan_id=plan["plan_id"], operation="local"
        )["decision"]
        assert decision["code"] == "RETIREMENT_OPERATION_COMPLETE"
        assert decision["receipt"]["plan_id"] == plan["plan_id"]
        assert decision["minimum_revalidation"]["ref_state"] != "ABSENT"
        if drift == "different-oid":
            assert "LOCAL_OID_DRIFT" in decision["blockers"]
        elif drift == "checked-out":
            assert "BRANCH_CHECKED_OUT" in decision["blockers"]
        assert git(repository.repo, "rev-parse", f"refs/heads/{plan['branch']}") == head
        assert receipt_path.read_bytes() == original_receipt


def test_closeout_failed_git_observation_does_not_substitute_historical_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan, receipt_path = _retired_candidate(repository, tmp_path)
        original_receipt = receipt_path.read_bytes()

        def unavailable(_path: Path) -> dict[str, str]:
            raise ControllerError("GIT_FAILED", "synthetic ref observation failure")

        monkeypatch.setattr(branch_retirement, "public_refs", unavailable)
        code = main([
            "branch-retirement-verify", "--repo", str(repository.repo), "--plan-id", plan["plan_id"],
            "--operation", "local", "--json",
        ])
        output = json.loads(capsys.readouterr().out)
        assert code == 2
        assert output["errors"][0]["code"] == "GIT_FAILED"
        assert output["decision"]["code"] == "ERROR"
        assert receipt_path.read_bytes() == original_receipt


def test_owner_released_candidate_requires_exact_receipt_reconciliation() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        allocation, publication_id = _owner_released_candidate(repository)
        store = MetadataStore(inventory(repository.repo).common_dir)
        snapshot = store.load()
        released = next(
            r for r in snapshot.release_receipts.values() if r.allocation_id == allocation
        )
        release_plan = snapshot.release_plans[released.plan_id]
        assert release_plan.trigger == "owner-release"
        assert release_plan.publication_receipt_id is None
        original_plan = (store.release_plans_dir / f"{release_plan.plan_id}.json").read_bytes()
        original_publication = (
            store.publication_receipts_dir / f"{publication_id}.json"
        ).read_bytes()
        blocked = plan_branch_retirement(
            repository.repo,
            allocation_id=allocation,
            remote="origin",
            integrated_into="main",
            dry_run=True,
        )["decision"]
        assert "INTEGRATION_PUBLICATION_NOT_VERIFIED" in blocked["plan"]["blockers"]
        reconciled = reconcile_publication_receipts(
            repository.repo, actor="owner-test", receipt_ids=(publication_id,)
        )["decision"]
        assert len(reconciled["written_receipt_ids"]) == 1
        decision = plan_branch_retirement(
            repository.repo, allocation_id=allocation, remote="origin", integrated_into="main"
        )["decision"]
        assert decision["code"] == "BRANCH_RETIREMENT_READY"
        assert (
            verify_branch_retirement(
                repository.repo, plan_id=decision["plan"]["plan_id"], operation="local"
            )["decision"]["code"]
            == "RETIREMENT_EXECUTABLE"
        )
        assert (
            store.release_plans_dir / f"{release_plan.plan_id}.json"
        ).read_bytes() == original_plan
        assert (
            store.publication_receipts_dir / f"{publication_id}.json"
        ).read_bytes() == original_publication


@pytest.mark.parametrize("fault", ["record-version", "intent", "allocation", "duplicate"])
def test_owner_release_retirement_rejects_reconciliation_drift(fault: str) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        allocation, publication_id = _owner_released_candidate(repository)
        result = reconcile_publication_receipts(
            repository.repo, actor="owner-test", receipt_ids=(publication_id,)
        )["decision"]
        store = MetadataStore(inventory(repository.repo).common_dir)
        snapshot = store.load()
        receipt = snapshot.reconciliation_receipts[result["written_receipt_ids"][0]]
        if fault == "record-version":
            changed = replace(receipt, observed_record_version=receipt.observed_record_version + 1)
        elif fault == "intent":
            changed = replace(receipt, recovered_intent_id=None)
        elif fault == "allocation":
            source = snapshot.records[allocation].source_allocations[0]
            changed = replace(receipt, allocation_id=source)
        else:
            changed = replace(receipt, receipt_id=str(uuid4()))
        store._atomic_json(
            store.reconciliation_receipts_dir / f"{changed.receipt_id}.json", asdict(changed)
        )
        decision = plan_branch_retirement(
            repository.repo,
            allocation_id=allocation,
            remote="origin",
            integrated_into="main",
            dry_run=True,
        )["decision"]
        assert "INTEGRATION_PUBLICATION_NOT_VERIFIED" in decision["plan"]["blockers"]


@pytest.mark.parametrize("fault", ["removed", "changed"])
def test_owner_release_retirement_rechecks_reconciliation_after_planning(fault: str) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        allocation, publication_id = _owner_released_candidate(repository)
        reconciled = reconcile_publication_receipts(
            repository.repo, actor="owner-test", receipt_ids=(publication_id,)
        )["decision"]
        plan = plan_branch_retirement(
            repository.repo, allocation_id=allocation, remote="origin", integrated_into="main"
        )["decision"]["plan"]
        store = MetadataStore(inventory(repository.repo).common_dir)
        receipt_id = reconciled["written_receipt_ids"][0]
        path = store.reconciliation_receipts_dir / f"{receipt_id}.json"
        if fault == "removed":
            path.unlink()
        else:
            receipt = store.load().reconciliation_receipts[receipt_id]
            store._atomic_json(path, asdict(replace(receipt, actor="another-actor")))
        verified = verify_branch_retirement(
            repository.repo, plan_id=plan["plan_id"], operation="local"
        )["decision"]
        assert verified["code"] != "RETIREMENT_EXECUTABLE"
        assert "PLAN_STATE_DRIFT" in verified["blockers"]
        assert (
            git(repository.repo, "rev-parse", f"refs/heads/{plan['branch']}")
            == plan["expected_oid"]
        )


def test_published_released_integration_candidate_gets_executable_retirement_plan() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        allocation, head = _released_candidate(repository)
        decision = plan_branch_retirement(
            repository.repo,
            allocation_id=allocation,
            remote="origin",
            integrated_into="main",
        )["decision"]
        assert decision["code"] == "BRANCH_RETIREMENT_READY"
        plan = decision["plan"]
        assert plan["classification"] == "ANCESTRY"
        assert plan["expected_oid"] == head
        assert plan["checked_out_paths"] == []
        verified = verify_branch_retirement(
            repository.repo, plan_id=plan["plan_id"], operation="local"
        )["decision"]
        assert verified["code"] == "RETIREMENT_EXECUTABLE"


@pytest.mark.parametrize("fault", [None, "unknown-event", "allocation-link"])
def test_only_explicitly_abandoned_intents_stop_blocking_source_retirement(
    fault: str | None,
) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        allocation, _head = _released_source(repository, "abandoned")
        queued = enqueue_integration(
            repository.repo,
            task_key="abandoned-retirement",
            authority_key="abandoned-retirement",
            module="retirement",
            owner="retirement-test",
            source_allocations=(allocation,),
        )["decision"]
        abandoned = abandon_integration_intent(
            repository.repo,
            intent_id=queued["intent_id"],
            owner="retirement-test",
            reason="superseded by the published candidate",
        )["decision"]
        assert abandoned["code"] == "INTEGRATION_ABANDONED"
        store = MetadataStore(inventory(repository.repo).common_dir)
        intent = store.load().integration_intents[queued["intent_id"]]
        assert intent.state == "ATTENTION_REQUIRED"
        if fault == "unknown-event":
            event = dict(intent.history[-1], event="UNEXPLAINED_ATTENTION")
            store.replace_integration_intent(replace(intent, history=(*intent.history[:-1], event)))
        elif fault == "allocation-link":
            store.replace_integration_intent(replace(intent, allocation_id=allocation))
        decision = plan_branch_retirement(
            repository.repo,
            allocation_id=allocation,
            remote="origin",
            integrated_into="main",
            dry_run=True,
        )["decision"]
        if fault is None:
            assert decision["code"] == "BRANCH_RETIREMENT_READY"
            assert decision["plan"]["blockers"] == []
        else:
            assert decision["code"] == "BRANCH_RETIREMENT_BLOCKED"
            assert "INTEGRATION_INTENT_UNRESOLVED" in decision["plan"]["blockers"]


def test_integration_retirement_completes_through_the_existing_cas_protocol(tmp_path: Path) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        allocation, _head = _released_candidate(repository, "execution")
        plan = plan_branch_retirement(
            repository.repo, allocation_id=allocation, remote="origin", integrated_into="main"
        )["decision"]["plan"]
        grant = _authorize(repository, plan)["authorization"]
        assert _cas(repository, grant["ref"], grant["expected_oid"]) == 0
        result_path = _result(repository, grant, execution.REF_REMOVED_AT_EXPECTED_OID, tmp_path)
        completed = complete_branch_retirement(
            repository.repo,
            authorization_id=grant["authorization_id"],
            token=grant["token"],
            result_path=result_path,
        )["decision"]
        assert completed["code"] == "RETIREMENT_EXECUTION_COMPLETE"
        assert completed["provenance"] == "AUTHORIZED_EXECUTION"
        assert (
            complete_branch_retirement(
                repository.repo,
                authorization_id=grant["authorization_id"],
                token=grant["token"],
                result_path=result_path,
            )["decision"]["idempotent"]
            is True
        )


@pytest.mark.parametrize("missing", ["publication", "release-plan", "publication-link"])
def test_integration_retirement_requires_linked_publication_evidence(missing: str) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        allocation, _head = _released_candidate(repository, "missing")
        store = MetadataStore(inventory(repository.repo).common_dir)
        snapshot = store.load()
        receipt = next(
            r for r in snapshot.release_receipts.values() if r.allocation_id == allocation
        )
        release_plan = snapshot.release_plans[receipt.plan_id]
        if missing == "publication":
            (
                store.publication_receipts_dir / f"{release_plan.publication_receipt_id}.json"
            ).unlink()
        elif missing == "release-plan":
            (store.release_plans_dir / f"{release_plan.plan_id}.json").unlink()
        else:
            identity = asdict(replace(release_plan, publication_receipt_id=None))
            identity.pop("plan_id")
            unlinked = replace(
                release_plan, publication_receipt_id=None, plan_id=_release_plan_id(identity)
            )
            store.write_release_plan(unlinked)
            store._atomic_json(
                store.release_receipts_dir / f"{receipt.receipt_id}.json",
                asdict(replace(receipt, plan_id=unlinked.plan_id)),
            )
        decision = plan_branch_retirement(
            repository.repo,
            allocation_id=allocation,
            remote="origin",
            integrated_into="main",
            dry_run=True,
        )["decision"]
        assert decision["code"] == "BRANCH_RETIREMENT_BLOCKED"
        assert "INTEGRATION_PUBLICATION_NOT_VERIFIED" in decision["plan"]["blockers"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("candidate_oid", "base"),
        ("allocation_id", "source"),
        ("target_ref", "refs/heads/other"),
        ("finalizer_record_id", "other-finalizer"),
    ],
)
@pytest.mark.parametrize("owner_release", [False, True])
def test_integration_retirement_rejects_publication_identity_drift(
    field: str, value: str, owner_release: bool
) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        if owner_release:
            allocation, publication_id = _owner_released_candidate(repository)
            reconcile_publication_receipts(
                repository.repo, actor="owner-test", receipt_ids=(publication_id,)
            )
        else:
            allocation, _head = _released_candidate(repository, "identity")
        store = MetadataStore(inventory(repository.repo).common_dir)
        snapshot = store.load()
        publication = next(
            r for r in snapshot.publication_receipts.values() if r.allocation_id == allocation
        )
        if value == "base":
            value = publication.expected_main_oid
        elif value == "source":
            value = snapshot.records[allocation].source_allocations[0]
        store._atomic_json(
            store.publication_receipts_dir / f"{publication.receipt_id}.json",
            asdict(replace(publication, **{field: value})),
        )
        decision = plan_branch_retirement(
            repository.repo,
            allocation_id=allocation,
            remote="origin",
            integrated_into="main",
            dry_run=True,
        )["decision"]
        assert decision["code"] == "BRANCH_RETIREMENT_BLOCKED"
        assert "INTEGRATION_PUBLICATION_NOT_VERIFIED" in decision["plan"]["blockers"]


def test_publication_disappearing_after_planning_blocks_execution() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        allocation, _head = _released_candidate(repository, "stale")
        plan = plan_branch_retirement(
            repository.repo, allocation_id=allocation, remote="origin", integrated_into="main"
        )["decision"]["plan"]
        store = MetadataStore(inventory(repository.repo).common_dir)
        publication = next(
            r for r in store.load().publication_receipts.values() if r.allocation_id == allocation
        )
        (store.publication_receipts_dir / f"{publication.receipt_id}.json").unlink()
        verified = verify_branch_retirement(
            repository.repo, plan_id=plan["plan_id"], operation="local"
        )["decision"]
        assert verified["code"] != "RETIREMENT_EXECUTABLE"
        assert "INTEGRATION_PUBLICATION_NOT_VERIFIED" in verified["blockers"]
        assert (
            git(repository.repo, "rev-parse", f"refs/heads/{plan['branch']}")
            == plan["expected_oid"]
        )


@pytest.mark.parametrize(
    "fault,blocker",
    [
        ("unreleased", "ALLOCATION_NOT_RELEASED"),
        ("bound", "RUNTIME_BINDING_ACTIVE"),
        ("checked-out", "BRANCH_CHECKED_OUT"),
        ("ref-drift", "LOCAL_OID_DRIFT"),
        ("nonterminal-intent", "INTEGRATION_INTENT_UNRESOLVED"),
        ("unsupported-role", "UNSUPPORTED_ALLOCATION_ROLE"),
    ],
)
def test_integration_retirement_preserves_existing_safety_guards(fault: str, blocker: str) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        allocation, _head = _released_candidate(repository, "guard")
        store = MetadataStore(inventory(repository.repo).common_dir)
        snapshot = store.load()
        record = snapshot.records[allocation]
        release_receipt = next(
            r for r in snapshot.release_receipts.values() if r.allocation_id == allocation
        )
        if fault == "unreleased":
            store.replace_record(replace(record, lifecycle="PUBLISHED"))
        elif fault == "bound":
            store.replace_record(replace(record, external_bindings=("runtime:test",)))
        elif fault == "unsupported-role":
            store.replace_record(replace(record, role="control-plane"))
        elif fault == "checked-out":
            git(
                repository.repo,
                "worktree",
                "add",
                str(repository.root / "occupied"),
                release_receipt.branch,
            )
        elif fault == "ref-drift":
            git(repository.repo, "branch", "-f", release_receipt.branch, "main")
        else:
            intent = next(
                i for i in snapshot.integration_intents.values() if i.allocation_id == allocation
            )
            store._atomic_json(
                store.integration_intents_dir / f"{intent.intent_id}.json",
                asdict(replace(intent, state="PUBLISHING")),
            )
        decision = plan_branch_retirement(
            repository.repo,
            allocation_id=allocation,
            remote="origin",
            integrated_into="main",
            dry_run=True,
        )["decision"]
        assert decision["code"] == "BRANCH_RETIREMENT_BLOCKED"
        assert blocker in decision["plan"]["blockers"]
