"""Retirement execution authorization: the TOCTOU window, closed and provable.

1.6.0 released a verdict from a shared-lock observation and then let a consumer
mutate a ref outside any Controller section, while ``branch-retirement-record``
turned the consumer's own JSON into a governance receipt.  These tests replay that
interleaving against the grant/settle protocol and assert the Controller now
refuses to bless it - and that the refusal is its own observation, not the
caller's claim.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tests.test_auto_release import POLICY_MANUAL
from tests.test_branch_retirement import _released_source
from tests.test_controlled import _configure_remote, adopt_canonical
from tests.test_shadow import GitRepository, git
from worktree_controller import retirement_execution as execution
from worktree_controller.branch_retirement import (
    authorize_branch_retirement,
    branch_retirement_executions,
    branch_retirement_status,
    complete_branch_retirement,
    outstanding_retirement_authorizations,
    plan_branch_retirement,
    record_branch_retirement,
    verify_branch_retirement,
)
from worktree_controller.branch_retirement import (
    grant_expired as retirement_grant_expired,
)
from worktree_controller.git_backend import inventory
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"


def _planned(repository: GitRepository, name: str = "grant") -> dict:
    adopt_canonical(repository)
    _configure_remote(repository)
    allocation, _head = _released_source(repository, name)
    payload = plan_branch_retirement(
        repository.repo,
        allocation_id=allocation,
        remote="origin",
        integrated_into="main",
    )["decision"]
    assert payload["code"] == "BRANCH_RETIREMENT_READY"
    return payload["plan"]


def _authorize(repository: GitRepository, plan: dict, **extra) -> dict:
    return authorize_branch_retirement(
        repository.repo,
        plan_id=plan["plan_id"],
        operation=extra.pop("operation", "local"),
        request_id=extra.pop("request_id", str(uuid.uuid4())),
        **extra,
    )["decision"]


def _result(repository: GitRepository, grant: dict, outcome: str, tmp_path: Path) -> Path:
    document = {
        "schema_version": 1,
        "contract": "retirement-execution-result/v1",
        "plan_id": grant["plan_id"],
        "operation": grant["operation"],
        "kind": grant["kind"],
        "outcome": outcome,
        "ref": grant["ref"],
        "expected_oid": grant["expected_oid"],
        "expected_integrated_oid": grant["expected_integrated_oid"],
        "performed_at": "2026-09-25T00:00:00+08:00",
        "executor": {"name": "mock-retirement-consumer", "version": "1"},
    }
    path = tmp_path / f"result-{grant['operation']}.json"
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    return path


def _cas(repository: GitRepository, ref: str, expected_oid: str) -> int:
    return subprocess.run(
        ["git", "-C", str(repository.repo), "update-ref", "-d", ref, expected_oid],
        check=False,
        capture_output=True,
    ).returncode


def test_grant_settles_only_for_the_authorized_transition(tmp_path: Path) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository)
        granted = _authorize(repository, plan)
        assert granted["code"] == "RETIREMENT_AUTHORIZED"
        grant = granted["authorization"]
        assert grant["observed"]["checked_out_paths"] == []
        assert grant["minimum_revalidation"] == {
            "plan_id": plan["plan_id"],
            "operation": "local",
            "branch": plan["branch"],
            "expected_oid": plan["expected_oid"],
            "expected_integrated_oid": plan["expected_integrated_oid"],
            "integrated_ref": plan["integrated_ref"],
            "ref_state": "PRESENT_EXPECTED",
            "controller_state_digest": plan["controller_state_digest"],
        }
        assert _cas(repository, grant["ref"], grant["expected_oid"]) == 0

        settled = complete_branch_retirement(
            repository.repo,
            authorization_id=grant["authorization_id"],
            token=grant["token"],
            result_path=_result(repository, grant, execution.REF_REMOVED_AT_EXPECTED_OID, tmp_path),
        )["decision"]
        assert settled["code"] == "RETIREMENT_EXECUTION_COMPLETE"
        assert settled["provenance"] == "AUTHORIZED_EXECUTION"
        assert settled["receipt"]["authorization_id"] == grant["authorization_id"]
        with MetadataStore(inventory(repository.repo).common_dir).lock(exclusive=True):
            assert (
                outstanding_retirement_authorizations(
                    MetadataStore(inventory(repository.repo).common_dir)
                )
                == []
            )
        replay = complete_branch_retirement(
            repository.repo,
            authorization_id=grant["authorization_id"],
            token=grant["token"],
            result_path=_result(repository, grant, execution.REF_REMOVED_AT_EXPECTED_OID, tmp_path),
        )["decision"]
        assert replay["idempotent"] is True


def test_result_claim_without_an_observed_deletion_is_refused(tmp_path: Path) -> None:
    """The 1.6.0 laundering door: a well-shaped file with no mutation behind it."""

    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository)
        grant = _authorize(repository, plan)["authorization"]
        before = branch_retirement_status(repository.repo, plan_id=plan["plan_id"])["decision"]
        assert before["completed_operations"] == []
        with pytest.raises(ControllerError) as refused:
            complete_branch_retirement(
                repository.repo,
                authorization_id=grant["authorization_id"],
                token=grant["token"],
                result_path=_result(
                    repository, grant, execution.REF_REMOVED_AT_EXPECTED_OID, tmp_path
                ),
            )
        assert refused.value.code == "EXECUTION_OBSERVATION_MISMATCH"
        after = branch_retirement_status(repository.repo, plan_id=plan["plan_id"])["decision"]
        assert after["completed_operations"] == []
        with MetadataStore(inventory(repository.repo).common_dir).lock(exclusive=False):
            store = MetadataStore(inventory(repository.repo).common_dir)
            assert len(outstanding_retirement_authorizations(store)) == 1


def test_toctou_replay_a_governed_drift_in_the_window_is_never_attested(
    tmp_path: Path,
) -> None:
    """The exact 1.6.0 sequence: verify, drift, CAS, then try to record."""

    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository)
        first = verify_branch_retirement(
            repository.repo, plan_id=plan["plan_id"], operation="local"
        )["decision"]
        assert first["code"] == "RETIREMENT_EXECUTABLE"

        grant = _authorize(repository, plan)["authorization"]
        checkout = repository.root / "reopened"
        git(repository.repo, "worktree", "add", str(checkout), plan["branch"])
        blocked = verify_branch_retirement(
            repository.repo, plan_id=plan["plan_id"], operation="local"
        )["decision"]
        assert blocked["code"] == "RETIREMENT_BLOCKED"
        assert "BRANCH_CHECKED_OUT" in blocked["blockers"]
        assert "RETIREMENT_AUTHORIZATION_ACTIVE" in blocked["blockers"]

        # Git gives no occupancy guard for update-ref, so the ref can still go:
        assert _cas(repository, grant["ref"], grant["expected_oid"]) == 0

        # The legacy door refuses while the grant is live, so no receipt can be
        # minted from a summary alone.
        summary = repository.root / "summary.json"
        summary.write_text(
            json.dumps(
                {
                    "finalizer_version": "mock/1",
                    "mode": "retire_local_branch",
                    "status": "success",
                    "mode_result": {
                        "retirement_plan_id": plan["plan_id"],
                        "result": "LOCAL_BRANCH_RETIRED_VERIFIED",
                        "branch": plan["branch"],
                        "remote": plan["remote"],
                        "expected_oid": plan["expected_oid"],
                        "integrated_into_oid": plan["expected_integrated_oid"],
                    },
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ControllerError) as legacy:
            record_branch_retirement(
                repository.repo,
                plan_id=plan["plan_id"],
                operation="local",
                summary_path=summary,
            )
        assert legacy.value.code == "RETIREMENT_EXECUTION_ACTIVE"

        # Settle compares the bound observation vector against its own re-read,
        # so an unexpected checkout cannot be attested.
        refusal = complete_branch_retirement(
            repository.repo,
            authorization_id=grant["authorization_id"],
            token=grant["token"],
            result_path=_result(repository, grant, execution.REF_REMOVED_AT_EXPECTED_OID, tmp_path),
        )["decision"]
        assert refusal["code"] == "RETIREMENT_EXECUTION_REFUSED"
        assert "OBSERVATION_CHECKOUT_APPEARED" in refusal["blockers"]
        assert refusal["evidence"]["path"].endswith("-abandoned.json")
        assert (
            branch_retirement_status(repository.repo, plan_id=plan["plan_id"])["decision"][
                "completed_operations"
            ]
            == []
        )


def test_abandonment_never_mints_a_receipt(tmp_path: Path) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository)
        grant = _authorize(repository, plan)["authorization"]
        assert _cas(repository, grant["ref"], grant["expected_oid"]) == 0
        abandoned = branch_retirement_executions(
            repository.repo,
            action="abandon",
            authorization_id=grant["authorization_id"],
            token=grant["token"],
            reason="executor died mid-flight",
        )["decision"]
        assert abandoned["code"] == "RETIREMENT_EXECUTION_ABANDONED"
        assert abandoned["classification"] == "ABANDONED_UNATTESTED_REMOVAL"
        assert abandoned["receipt_filed"] is False
        status = branch_retirement_status(repository.repo, plan_id=plan["plan_id"])["decision"]
        assert status["completed_operations"] == []
        verify = verify_branch_retirement(
            repository.repo, plan_id=plan["plan_id"], operation="local"
        )["decision"]
        assert verify["code"] == "RETIREMENT_BLOCKED"
        assert "ABSENT_UNATTESTED" in verify["blockers"]
        evidence = Path(abandoned["evidence"]["path"])
        assert evidence.is_file() and os.access(evidence, os.W_OK) is False
        assert json.loads(evidence.read_text())["classification"] == "ABANDONED_UNATTESTED_REMOVAL"


def test_one_grant_per_plan_and_operation_and_replay_is_idempotent(tmp_path: Path) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository)
        request_id = str(uuid.uuid4())
        first = _authorize(repository, plan, request_id=request_id)
        replay = _authorize(repository, plan, request_id=request_id)
        assert replay["idempotent"] is True
        assert (
            replay["authorization"]["authorization_id"]
            == first["authorization"]["authorization_id"]
        )
        rival = _authorize(repository, plan)
        assert rival["code"] == "RETIREMENT_BLOCKED"
        assert "RETIREMENT_AUTHORIZATION_ACTIVE" in rival["blockers"]
        assert rival["outstanding_authorization_id"] == first["authorization_id"]
        # The fixture never pushed the feature branch, so the remote half is an
        # unattested absence: a grant for it must be refused, not minted.
        other_operation = _authorize(repository, plan, operation="remote")
        assert other_operation["code"] == "RETIREMENT_BLOCKED"
        assert "ABSENT_UNATTESTED" in other_operation["blockers"]
        assert other_operation["authorization"] is None


def test_wrong_token_and_foreign_authorization_are_refused(tmp_path: Path) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository)
        grant = _authorize(repository, plan)["authorization"]
        path = _result(repository, grant, execution.REF_REMOVED_AT_EXPECTED_OID, tmp_path)
        assert _cas(repository, grant["ref"], grant["expected_oid"]) == 0
        with pytest.raises(ControllerError) as wrong:
            complete_branch_retirement(
                repository.repo,
                authorization_id=grant["authorization_id"],
                token="0" * 64,
                result_path=path,
            )
        assert wrong.value.code == "AUTHORIZATION_TOKEN_MISMATCH"


def test_authorization_content_is_addressed_so_expiry_cannot_be_edited(
    tmp_path: Path,
) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository)
        grant = _authorize(repository, plan)["authorization"]
        store = MetadataStore(inventory(repository.repo).common_dir)
        path = store.branch_retirement_authorizations_dir / f"{grant['authorization_id']}.json"
        tampered = {**grant, "expires_at": "2999-01-01T00:00:00+08:00"}
        path.write_text(json.dumps(tampered, sort_keys=True, indent=2), encoding="utf-8")
        with pytest.raises(ControllerError) as tampered:
            complete_branch_retirement(
                repository.repo,
                authorization_id=grant["authorization_id"],
                token=grant["token"],
                result_path=_result(
                    repository, grant, execution.REF_REMOVED_AT_EXPECTED_OID, tmp_path
                ),
            )
        assert tampered.value.code == "INVALID_METADATA"


def test_an_expired_grant_still_fences_and_still_needs_the_token(
    tmp_path: Path,
) -> None:
    """Expiry is a verification prompt, never a release and never a completion."""

    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository, "grant")
        grant = _authorize(repository, plan, ttl_seconds=1)["authorization"]
        assert retirement_grant_expired(grant) is False
        assert (
            retirement_grant_expired({**grant, "expires_at": "2020-01-01T00:00:00+08:00"}) is True
        )
        with pytest.raises(ControllerError):
            retirement_grant_expired({**grant, "expires_at": "not-a-time"})

        second = _authorize(repository, plan)
        assert second["code"] == "RETIREMENT_BLOCKED"
        assert "RETIREMENT_AUTHORIZATION_ACTIVE" in second["blockers"]
        assert _cas(repository, grant["ref"], grant["expected_oid"]) == 0
        settled = complete_branch_retirement(
            repository.repo,
            authorization_id=grant["authorization_id"],
            token=grant["token"],
            result_path=_result(repository, grant, execution.REF_REMOVED_AT_EXPECTED_OID, tmp_path),
        )["decision"]
        assert settled["code"] == "RETIREMENT_EXECUTION_COMPLETE"
        assert settled["grant_expired_at_settle"] is False


def test_the_plan_recipe_is_exactly_what_the_grant_binds() -> None:
    """A consumer may plan from the published recipe; it must not drift from the grant."""

    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        adopt_canonical(repository)
        _configure_remote(repository)
        allocation, _head = _released_source(repository, "recipe")
        decision = plan_branch_retirement(
            repository.repo,
            allocation_id=allocation,
            remote="origin",
            integrated_into="main",
        )["decision"]
        plan, recipe = decision["plan"], decision["execution"]
        assert set(recipe) == set(execution.RECIPE_FIELDS)
        assert recipe["contract"] == execution.RECIPE_CONTRACT
        assert recipe["result_contract"] == execution.RESULT_CONTRACT
        for operation in ("local", "remote"):
            entry = recipe["operations"][operation]
            assert set(entry) == set(execution.RECIPE_OPERATION_FIELDS)
            assert entry["expected_oid"] == plan["expected_oid"]
            assert entry["expected_integrated_oid"] == plan["expected_integrated_oid"]
            assert entry["kind"] == execution.KIND_CAS_DELETE
        assert recipe["operations"]["local"]["ref"] == f"refs/heads/{plan['branch']}"
        assert recipe["operations"]["remote"]["ref"] == plan["remote_tracking_ref"]

        grant = _authorize(repository, plan)["authorization"]
        assert grant["ref"] == recipe["operations"][grant["operation"]]["ref"]
        assert grant["expected_oid"] == recipe["operations"]["local"]["expected_oid"]
        assert grant["integrated_ref"] == recipe["operations"]["local"]["integrated_ref"]
        assert grant["tracking_ref"] == plan["remote_tracking_ref"]


def test_executions_inspect_and_list_publish_the_reconciliation_view(
    tmp_path: Path,
) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository)
        grant = _authorize(repository, plan)["authorization"]
        listed = branch_retirement_executions(repository.repo, action="list")["decision"]
        assert [item["authorization_id"] for item in listed["authorizations"]] == [
            grant["authorization_id"]
        ]
        inspected = branch_retirement_executions(
            repository.repo, action="inspect", authorization_id=grant["authorization_id"]
        )["decision"]
        assert inspected["ref_present_now"] is True
        assert inspected["holder_dead"] is False
        assert inspected["observed_now"]["refs"][grant["ref"]] == grant["expected_oid"]


def test_the_provenance_of_a_legacy_summary_receipt_is_labelled(tmp_path: Path) -> None:
    """A consumer summary that happens to be true still says what it is."""

    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository)
        assert _cas(repository, f"refs/heads/{plan['branch']}", plan["expected_oid"]) == 0
        summary = repository.root / "summary.json"
        summary.write_text(
            json.dumps(
                {
                    "finalizer_version": "mock/1",
                    "mode": "retire_local_branch",
                    "status": "success",
                    "mode_result": {
                        "retirement_plan_id": plan["plan_id"],
                        "result": "LOCAL_BRANCH_RETIRED_VERIFIED",
                        "branch": plan["branch"],
                        "remote": plan["remote"],
                        "expected_oid": plan["expected_oid"],
                        "integrated_into_oid": plan["expected_integrated_oid"],
                    },
                }
            ),
            encoding="utf-8",
        )
        recorded = record_branch_retirement(
            repository.repo,
            plan_id=plan["plan_id"],
            operation="local",
            summary_path=summary,
        )["decision"]
        assert recorded["code"] == "BRANCH_RETIREMENT_RESULT_RECORDED"
        assert recorded["provenance"] == "CONTROLLER_OBSERVED"
        assert recorded["receipt"]["finalizer_result"] == execution.REF_REMOVED_AT_EXPECTED_OID


def test_grant_lives_outside_the_authority_digest_so_it_cannot_self_invalidate(
    tmp_path: Path,
) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository)
        grant = _authorize(repository, plan)["authorization"]
        store = MetadataStore(inventory(repository.repo).common_dir)
        with store.lock(exclusive=False):
            current, _ = _reevaluate(repository, plan, store)
        assert grant["controller_state_digest"] == current["controller_state_digest"]
        assert "branch-retirement-authorizations" not in _authority_directories()
        assert "PLAN_STATE_DRIFT" not in current["blockers"]


def _authority_directories() -> tuple[str, ...]:
    from worktree_controller.branch_retirement import AUTHORITY_DIRECTORIES

    return AUTHORITY_DIRECTORIES


def _reevaluate(repository: GitRepository, plan: dict, store: MetadataStore):
    from worktree_controller.branch_retirement import _evaluate

    return _evaluate(
        repository.repo,
        allocation_id=str(plan["allocation_id"]),
        branch=None,
        remote=str(plan["remote"]),
        integrated_into=str(plan["integrated_into"]),
        plan_id=plan["plan_id"],
        _locked_store=store,
    )


def test_fault_injection_between_receipt_and_grant_close_replays(
    tmp_path: Path,
) -> None:
    """A crash after the receipt but before closing the grant is replayable."""

    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository)
        grant = _authorize(repository, plan)["authorization"]
        assert _cas(repository, grant["ref"], grant["expected_oid"]) == 0
        result_path = _result(repository, grant, execution.REF_REMOVED_AT_EXPECTED_OID, tmp_path)
        environment = {
            **os.environ,
            "PYTHONPATH": str(SOURCE),
            "PYTHONDONTWRITEBYTECODE": "1",
            "WORKTREE_CONTROLLER_TEST_CRASH_AT": "retirement_receipt_written",
        }
        crashed = subprocess.run(
            [
                sys.executable,
                "-m",
                "worktree_controller.cli",
                "branch-retirement-complete",
                "--repo",
                str(repository.repo),
                "--authorization-id",
                grant["authorization_id"],
                "--token",
                grant["token"],
                "--result-file",
                str(result_path),
                "--json",
            ],
            capture_output=True,
            text=True,
            env=environment,
            timeout=180,
            check=False,
        )
        assert crashed.returncode == 97, crashed.stderr
        doctor = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "worktree_controller.cli",
                    "doctor",
                    "--repo",
                    str(repository.repo),
                    "--json",
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONPATH": str(SOURCE)},
                timeout=180,
                check=True,
            ).stdout
        )["decision"]
        outstanding = doctor["retirement_executions"]["outstanding"]
        assert [item["authorization_id"] for item in outstanding] == [grant["authorization_id"]]
        settled = complete_branch_retirement(
            repository.repo,
            authorization_id=grant["authorization_id"],
            token=grant["token"],
            result_path=result_path,
        )["decision"]
        assert settled["idempotent"] is True
        with MetadataStore(inventory(repository.repo).common_dir).lock(exclusive=False):
            store = MetadataStore(inventory(repository.repo).common_dir)
            assert outstanding_retirement_authorizations(store) == []
        assert (
            any(
                warning["code"] == "RETIREMENT_AUTHORIZATION_OUTSTANDING"
                for warning in json.loads(
                    subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "worktree_controller.cli",
                            "doctor",
                            "--repo",
                            str(repository.repo),
                            "--json",
                        ],
                        capture_output=True,
                        text=True,
                        env={**os.environ, "PYTHONPATH": str(SOURCE)},
                        timeout=180,
                        check=True,
                    ).stdout
                )["warnings"]
            )
            is False
        )


def test_grant_and_settle_reach_the_public_surface(tmp_path: Path) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository)
        environment = {**os.environ, "PYTHONPATH": str(SOURCE), "PYTHONDONTWRITEBYTECODE": "1"}
        granted = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "worktree_controller.cli",
                    "branch-retirement-authorize",
                    "--repo",
                    str(repository.repo),
                    "--plan-id",
                    plan["plan_id"],
                    "--operation",
                    "local",
                    "--request-id",
                    str(uuid.uuid4()),
                    "--json",
                ],
                capture_output=True,
                text=True,
                env=environment,
                timeout=180,
                check=True,
            ).stdout
        )["decision"]
        assert granted["code"] == "RETIREMENT_AUTHORIZED"
        listed = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "worktree_controller.cli",
                    "branch-retirement-executions",
                    "--repo",
                    str(repository.repo),
                    "--action",
                    "list",
                    "--json",
                ],
                capture_output=True,
                text=True,
                env=environment,
                timeout=180,
                check=True,
            ).stdout
        )["decision"]
        assert [item["authorization_id"] for item in listed["authorizations"]] == [
            granted["authorization_id"]
        ]
        assert len(granted["authorization_id"]) == 64
