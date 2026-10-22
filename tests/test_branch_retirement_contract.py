"""M4A: the published branch-retirement contract and its pre-mutation verify API.

The verify verdict is the only supported way for a consumer such as Git Finalizer
to decide whether a persisted plan may still be acted on.  These tests pin the
verdict semantics, the staleness boundary, the concurrency serialization, and —
most importantly — that the recipe the contract publishes is the recipe the code
actually uses, so no consumer has to mirror a private digest again.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_auto_release import POLICY_MANUAL
from tests.test_branch_retirement import _released_source
from tests.test_controlled import _configure_remote, acquire_one, adopt_canonical
from tests.test_shadow import GitRepository, git
from worktree_controller import branch_retirement as retirement
from worktree_controller import retirement_execution as execution
from worktree_controller.branch_retirement import (
    branch_retirement_status,
    plan_branch_retirement,
    record_branch_retirement,
    verify_branch_retirement,
)
from worktree_controller.git_backend import inventory
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
CLI_ENV = {**os.environ, "PYTHONPATH": str(SOURCE), "PYTHONDONTWRITEBYTECODE": "1"}
CONTRACT = json.loads((ROOT / "tool_cli_contract.json").read_text(encoding="utf-8"))
RETIREMENT = CONTRACT["branch_retirement_contract"]


def _planned(repository: GitRepository, name: str = "contract") -> dict:
    adopt_canonical(repository)
    _configure_remote(repository)
    allocation, _head = _released_source(repository, name)
    payload = plan_branch_retirement(
        repository.repo,
        allocation_id=allocation,
        remote="origin",
        integrated_into="main",
    )
    assert payload["decision"]["code"] == "BRANCH_RETIREMENT_READY"
    return payload["decision"]["plan"]


def _verify(repository: Path, plan_id: str, operation: str) -> dict:
    return verify_branch_retirement(repository, plan_id=plan_id, operation=operation)["decision"]


def _cli(*arguments: str) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "worktree_controller.cli", *arguments, "--json"],
        capture_output=True,
        text=True,
        timeout=120,
        env=CLI_ENV,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_end_to_end_plan_then_verify_executable_over_the_public_cli() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository)
        decision = _cli(
            "branch-retirement-verify",
            "--repo",
            str(repository.repo),
            "--plan-id",
            plan["plan_id"],
            "--operation",
            "local",
        )["decision"]
        assert decision["code"] == "RETIREMENT_EXECUTABLE"
        assert decision["blockers"] == []
        assert decision["receipt"] is None
        revalidation = decision["minimum_revalidation"]
        assert revalidation == {
            "plan_id": plan["plan_id"],
            "operation": "local",
            "branch": plan["branch"],
            "expected_oid": plan["expected_oid"],
            "expected_integrated_oid": plan["expected_integrated_oid"],
            "integrated_ref": plan["integrated_ref"],
            "ref_state": "PRESENT_EXPECTED",
            "controller_state_digest": plan["controller_state_digest"],
        }


def test_verify_is_rejected_for_every_staleness_shape() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository)
        drifted = _verify(repository.repo, plan["plan_id"], "local")
        assert drifted["code"] == "RETIREMENT_EXECUTABLE"

        git(repository.repo, "branch", "-f", plan["branch"], "main")
        decision = _verify(repository.repo, plan["plan_id"], "local")
        assert decision["code"] == "RETIREMENT_STALE"
        assert "LOCAL_OID_DRIFT" in decision["blockers"]


def test_verify_is_rejected_when_only_the_policy_changed() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository)
        policy = repository.repo / ".agents" / "worktree-policy.toml"
        policy.write_text(policy.read_text(encoding="utf-8") + "\n# drifted\n", encoding="utf-8")
        decision = _verify(repository.repo, plan["plan_id"], "local")
        assert decision["code"] == "RETIREMENT_STALE"
        assert decision["blockers"] == ["PLAN_STATE_DRIFT"]


def test_occupancy_blocks_without_calling_it_staleness() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository)
        checkout = repository.root / "rechecked"
        git(repository.repo, "worktree", "add", str(checkout), plan["branch"])
        decision = _verify(repository.repo, plan["plan_id"], "local")
        assert decision["code"] == "RETIREMENT_BLOCKED"
        assert decision["blockers"] == ["BRANCH_CHECKED_OUT"]
        git(repository.repo, "worktree", "remove", str(checkout))
        assert _verify(repository.repo, plan["plan_id"], "local")["code"] == "RETIREMENT_EXECUTABLE"


def test_unattributed_absence_stays_blocked_until_attested() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository)
        git(repository.repo, "branch", "-D", plan["branch"])
        decision = _verify(repository.repo, plan["plan_id"], "local")
        assert decision["code"] == "RETIREMENT_BLOCKED"
        assert "ABSENT_UNATTESTED" in decision["blockers"]


def test_unrelated_authority_state_does_not_invalidate_a_plan() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository)
        acquire_one(repository, task="unrelated-active", authority="unrelated-active")
        assert _verify(repository.repo, plan["plan_id"], "local")["code"] == "RETIREMENT_EXECUTABLE"


def test_local_completion_never_invalidates_the_remote_half() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository)

        def summary(operation: str, result: str) -> Path:
            path = repository.root / f"{operation}-summary.json"
            path.write_text(
                json.dumps(
                    {
                        "finalizer_version": "1.5.0",
                        "mode": f"retire_{operation}_branch",
                        "status": "success",
                        "mode_result": {
                            "retirement_plan_id": plan["plan_id"],
                            "result": result,
                            "branch": plan["branch"],
                            "remote": plan["remote"],
                            "expected_oid": plan["expected_oid"],
                            "integrated_into_oid": plan["expected_integrated_oid"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            return path

        git(
            repository.repo,
            "update-ref",
            "-d",
            f"refs/heads/{plan['branch']}",
            plan["expected_oid"],
        )
        record_branch_retirement(
            repository.repo,
            plan_id=plan["plan_id"],
            operation="local",
            summary_path=summary("local", "LOCAL_BRANCH_RETIRED_VERIFIED"),
        )
        completed = _verify(repository.repo, plan["plan_id"], "local")
        assert completed["code"] == "RETIREMENT_OPERATION_COMPLETE"
        assert completed["receipt"]["operation"] == "local"
        pending = _verify(repository.repo, plan["plan_id"], "remote")
        assert pending["code"] == "RETIREMENT_BLOCKED"
        assert "ABSENT_UNATTESTED" in pending["blockers"]


def test_verify_rejects_unusable_arguments_and_unknown_plans() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository)
        for error_code, arguments in {
            "INVALID_OPERATION": {"operation": "sideways"},
            "INVALID_PLAN_ID": {"operation": "local", "plan_id": "not-a-digest"},
            "UNKNOWN_RETIREMENT_PLAN": {"operation": "local", "plan_id": "a" * 64},
        }.items():
            merged = {"plan_id": plan["plan_id"], **arguments}
            try:
                verify_branch_retirement(
                    repository.repo, plan_id=merged["plan_id"], operation=merged["operation"]
                )
            except ControllerError as error:
                assert error.code == error_code, (error_code, error.code)
            else:  # pragma: no cover - the assertion above is the test
                raise AssertionError(f"expected {error_code}")


def _attest_completion(repository: GitRepository, plan: dict, operation: str) -> dict:
    ref = plan["remote_ref"] if operation == "local" else plan["remote_tracking_ref"]
    git(repository.repo, "update-ref", "-d", ref)
    summary = repository.root / f"completed-{operation}.json"
    summary.write_text(
        json.dumps(
            {
                "finalizer_version": "1.5.0",
                "mode": f"retire_{operation}_branch",
                "status": "success",
                "mode_result": {
                    "retirement_plan_id": plan["plan_id"],
                    "result": f"{operation.upper()}_BRANCH_RETIRED_VERIFIED",
                    "branch": plan["branch"],
                    "remote": plan["remote"],
                    "expected_oid": plan["expected_oid"],
                    "integrated_into_oid": plan["expected_integrated_oid"],
                },
            }
        )
    )
    return record_branch_retirement(
        repository.repo, plan_id=plan["plan_id"], operation=operation, summary_path=summary
    )["decision"]["receipt"]


def _advance_integrated_ref(repository: GitRepository, plan: dict) -> str:
    git(repository.repo, "merge", "--ff-only", plan["expected_integrated_oid"])
    git(repository.repo, "commit", "--allow-empty", "-m", "unrelated main fast-forward")
    head = git(repository.repo, "rev-parse", "HEAD")
    git(repository.repo, "update-ref", plan["integrated_ref"], head)
    return head


def test_completed_retirement_survives_main_fast_forward_without_rewriting_evidence() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository)
        receipts = {op: _attest_completion(repository, plan, op) for op in ("local", "remote")}
        store = MetadataStore(inventory(repository.repo).common_dir)
        paths = [
            store.branch_retirement_plans_dir / f"{plan['plan_id']}.json",
            *(
                store.branch_retirement_receipts_dir / f"{plan['plan_id']}-{op}.json"
                for op in ("local", "remote")
            ),
        ]
        original = {path: path.read_bytes() for path in paths}
        for op in ("local", "remote"):
            assert _verify(repository.repo, plan["plan_id"], op)["blockers"] == []
        head = _advance_integrated_ref(repository, plan)
        for op in ("local", "remote"):
            decision = _verify(repository.repo, plan["plan_id"], op)
            assert decision["code"] == "RETIREMENT_OPERATION_COMPLETE"
            assert decision["blockers"] == []
            assert decision["receipt"] == receipts[op]
            assert decision["receipt"]["expected_integrated_oid"] == plan["expected_integrated_oid"]
            assert decision["minimum_revalidation"]["expected_integrated_oid"] == head
        status = branch_retirement_status(repository.repo, plan_id=plan["plan_id"])["decision"]
        assert status["code"] == "BRANCH_RETIREMENT_COMPLETE"
        assert status["current"]["blockers"] == []
        assert {path: path.read_bytes() for path in paths} == original
        assert len(tuple(store.branch_retirement_receipts_dir.glob("*.json"))) == 2


def test_completed_half_cannot_relax_pending_half_main_cas() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository)
        git(repository.repo, "update-ref", plan["remote_tracking_ref"], plan["expected_oid"])
        _attest_completion(repository, plan, "local")
        _advance_integrated_ref(repository, plan)
        assert _verify(repository.repo, plan["plan_id"], "local")["blockers"] == []
        pending = _verify(repository.repo, plan["plan_id"], "remote")
        assert pending["code"] == "RETIREMENT_STALE"
        assert pending["receipt"] is None
        assert "PLAN_STATE_DRIFT" in pending["blockers"]
        status = branch_retirement_status(repository.repo, plan_id=plan["plan_id"])["decision"]
        assert status["code"] == "BRANCH_RETIREMENT_PARTIAL_DRIFTED"


@pytest.mark.parametrize("operation", ["local", "remote"])
@pytest.mark.parametrize("drift", ["reappeared", "policy", "ancestry_lost", "receipt_mismatch"])
def test_completed_retirement_fails_closed_for_real_drift(operation: str, drift: str) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository)
        for op in ("local", "remote"):
            _attest_completion(repository, plan, op)
        _advance_integrated_ref(repository, plan)
        if drift == "reappeared":
            ref = plan["remote_ref"] if operation == "local" else plan["remote_tracking_ref"]
            git(repository.repo, "update-ref", ref, plan["expected_oid"])
            expected = "REF_REAPPEARED_AFTER_RETIREMENT"
        elif drift == "policy":
            policy = repository.repo / ".agents/worktree-policy.toml"
            policy.write_text(policy.read_text() + "\n# actual policy drift\n")
            expected = "PLAN_STATE_DRIFT"
        elif drift == "ancestry_lost":
            git(repository.repo, "update-ref", plan["integrated_ref"], f"{plan['expected_oid']}^")
            expected = "PLAN_STATE_DRIFT"
        else:
            store = MetadataStore(inventory(repository.repo).common_dir)
            path = store.branch_retirement_receipts_dir / f"{plan['plan_id']}-{operation}.json"
            receipt = json.loads(path.read_text())
            receipt["operation"] = "remote" if operation == "local" else "local"
            path.write_text(json.dumps(receipt))
            expected = "RETIREMENT_RECEIPT_MISMATCH"
        decision = _verify(repository.repo, plan["plan_id"], operation)
        assert decision["code"] == "RETIREMENT_OPERATION_COMPLETE"
        assert expected in decision["blockers"]
        status = branch_retirement_status(repository.repo, plan_id=plan["plan_id"])["decision"]
        assert status["code"] == "BRANCH_RETIREMENT_DRIFTED"
        assert expected in status["current"]["blockers"]


def test_verify_waits_for_the_exclusive_repository_lock() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository)
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    f"import sys, time; sys.path.insert(0, {str(SOURCE)!r});"
                    "from pathlib import Path;"
                    "from worktree_controller.store import MetadataStore;"
                    "from worktree_controller.git_backend import inventory;"
                    f"store = MetadataStore(inventory(Path({str(repository.repo)!r})).common_dir);"
                    "context = store.lock(exclusive=True);"
                    "context.__enter__(); time.sleep(2.0)"
                ),
            ],
            env=CLI_ENV,
        )
        try:
            time.sleep(0.5)
            started = time.monotonic()
            decision = _cli(
                "branch-retirement-verify",
                "--repo",
                str(repository.repo),
                "--plan-id",
                plan["plan_id"],
                "--operation",
                "local",
            )["decision"]
            waited = time.monotonic() - started
        finally:
            holder.wait(timeout=30)
        assert decision["code"] == "RETIREMENT_EXECUTABLE"
        assert waited >= 1.0


def test_published_recipe_matches_the_implementation() -> None:
    """A consumer must be able to trust the contract instead of mirroring code."""

    probe = {field: field for field in RETIREMENT["identity"]["identity_fields"]}
    assert list(retirement._plan_identity(probe)) == RETIREMENT["identity"]["identity_fields"]
    assert RETIREMENT["identity"]["optional_identity_fields"] == ["canonical_transition_evidence"]
    probe["canonical_transition_evidence"] = {"event_sha256": "a" * 64}
    assert (
        retirement._plan_identity(probe)["canonical_transition_evidence"]
        == probe["canonical_transition_evidence"]
    )
    assert RETIREMENT["state_digest"]["authority_directories"] == list(
        retirement.AUTHORITY_DIRECTORIES
    )
    assert RETIREMENT["state_digest"]["authority_files"] == list(retirement.AUTHORITY_FILES)
    assert set(RETIREMENT["verify"]["stale_blockers"]) == {
        *retirement.STALE_BLOCKERS,
        "CLASSIFICATION_*",
    }
    assert RETIREMENT["verify"]["minimum_revalidation"] == [
        "plan_id",
        "operation",
        "branch",
        "expected_oid",
        "expected_integrated_oid",
        "integrated_ref",
        "ref_state",
        "controller_state_digest",
    ]
    assert set(RETIREMENT["verify"]["verdicts"]) == {
        "RETIREMENT_EXECUTABLE",
        "RETIREMENT_STALE",
        "RETIREMENT_BLOCKED",
        "RETIREMENT_OPERATION_COMPLETE",
    }
    plan_id = retirement._sha256_json(probe)
    assert RETIREMENT["identity"]["plan_id"].startswith("sha256")
    assert len(plan_id) == 64


def test_plan_schema_capability_stays_pinned_for_the_deployed_consumer() -> None:
    """The published contract, not a sibling repository, states the consumer constraint.

    branch_retirement_version mirrors plan.schema_version because the shipped
    consumer attestation compares those two numbers; raising it would reject every
    still-valid plan. The additive surface therefore travels on its own capability.
    """

    assert CONTRACT["branch_retirement_version"] == retirement.PLAN_SCHEMA_VERSION == 1
    assert RETIREMENT["plan_schema_version"] == 1
    assert RETIREMENT["verify"]["plan_schema_binding"] == (
        "plan.schema_version is the retirement authority pin; this contract surface is "
        "branch_retirement_contract.version"
    )
    assert any(
        "branch_retirement_version" in item and "schema" in item
        for item in RETIREMENT["compatibility"]
    )


def test_published_execution_recipe_matches_the_implementation() -> None:
    """A consumer must be able to settle from the contract alone."""

    authorize = RETIREMENT["execution_authorization"]
    probe = {field: field for field in authorize["authorization_identity_fields"]}
    assert list(execution.identity(probe)) == authorize["authorization_identity_fields"]
    assert authorize["result_fields"] == sorted(execution.RESULT_FIELDS)
    assert authorize["result_optional_fields"] == sorted(execution.RESULT_OPTIONAL_FIELDS)
    assert authorize["outcomes"] == sorted(execution.OUTCOMES)
    assert authorize["execution_kinds"] == sorted(execution.EXECUTION_KINDS)
    assert authorize["contract"] == execution.AUTHORIZATION_CONTRACT
    assert authorize["result_contract"] == execution.RESULT_CONTRACT
    assert execution.AUTHORIZATION_SCHEMA_VERSION == 1 and execution.RESULT_SCHEMA_VERSION == 1
    canonical = execution.RESULT_FIELDS | execution.RESULT_OPTIONAL_FIELDS
    assert canonical == {
        "schema_version",
        "contract",
        "plan_id",
        "operation",
        "kind",
        "outcome",
        "ref",
        "expected_oid",
        "expected_integrated_oid",
        "integrated_ref",
        "performed_at",
        "executor",
        "evidence",
    } or set(canonical) == {
        "schema_version",
        "contract",
        "plan_id",
        "operation",
        "kind",
        "outcome",
        "ref",
        "expected_oid",
        "expected_integrated_oid",
        "integrated_ref",
        "performed_at",
        "executor",
        "evidence",
    }


def test_published_execution_rules_match_the_running_code() -> None:
    """Every list a consumer must obey is gated against the implementation constants."""

    authorize = RETIREMENT["execution_authorization"]
    assert authorize["grantable_kinds"] == [execution.KIND_CAS_DELETE]
    assert authorize["directory"] == "branch-retirement-authorizations"
    assert authorize["directory"] not in RETIREMENT["state_digest"]["authority_directories"]
    assert set(authorize["provenance"]) == {
        "AUTHORIZED_EXECUTION",
        "CONTROLLER_OBSERVED",
        "CONSUMER_CLAIM",
    }
    assert authorize["authority_blockers"] == sorted(retirement.AUTHORITY_BLOCKERS)
    assert authorize["fault_points"] == [
        "retirement_authorization_issued",
        "retirement_receipt_written",
        "retirement_abandon_started",
        "retirement_abandon_evidence_written",
    ]
    assert retirement.RECEIPT_PROVENANCE_AUTHORIZED == "AUTHORIZED_EXECUTION"
    assert retirement.RECEIPT_PROVENANCE_OBSERVED == "CONTROLLER_OBSERVED"
    assert retirement.RECEIPT_PROVENANCE_CLAIM == "CONSUMER_CLAIM"
    decision = json.loads(
        subprocess.run(
            [sys.executable, "-m", "worktree_controller.cli", "capabilities", "--json"],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(SOURCE), "PYTHONDONTWRITEBYTECODE": "1"},
            timeout=60,
            check=True,
        ).stdout
    )["decision"]
    assert authorize["capability"] == "branch_retirement_execution_version"
    assert decision[authorize["capability"]] == CONTRACT[authorize["capability"]] == 1


def test_published_execution_codes_exist_in_the_source() -> None:
    """A published code that no module can raise is a fiction for consumers."""

    authorize = RETIREMENT["execution_authorization"]
    package = ROOT / "src" / "worktree_controller"
    sources = {path.name: path.read_text(encoding="utf-8") for path in package.glob("*.py")}
    combined = "".join(sources.values())
    missing = [
        code for code in authorize["compatibility_error_codes"] if f'"{code}"' not in combined
    ]
    assert not missing, f"contract publishes codes no module raises: {missing}"
    for blocker in authorize["verb_layer_blockers"]:
        assert f'"{blocker}"' in sources["branch_retirement.py"]
        assert blocker not in retirement.AUTHORITY_BLOCKERS, "the fence must not poison record"
        assert blocker not in retirement.STALE_BLOCKERS, "a live grant is not a stale plan"
    assert authorize["grant_document_layout"].count("branch-retirement-authorizations") >= 1
    assert (ROOT / "src" / "worktree_controller" / "retirement_adapters.py").is_file()
    assert "retirement-execution-result/v1" in sources["retirement_execution.py"]


def test_published_plan_recipe_fields_equal_the_implementation() -> None:
    recipe = RETIREMENT["execution_authorization"]["plan_recipe"]
    assert recipe["fields"] == sorted(execution.RECIPE_FIELDS)
    assert recipe["operation_fields"] == sorted(execution.RECIPE_OPERATION_FIELDS)
    assert recipe["contract"] == execution.RECIPE_CONTRACT
    assert recipe["schema_version"] == execution.RECIPE_SCHEMA_VERSION


def test_execution_verbs_are_declared_as_metadata_mutation() -> None:
    entries = {entry["name"]: entry for entry in CONTRACT["commands"]}
    for name in ("branch-retirement-authorize", "branch-retirement-complete"):
        assert entries[name]["operation_class"] == "metadata_mutation"
        assert entries[name]["result_contract"] == "branch-retirement/v5"
    assert entries["branch-retirement-executions"]["operation_class"] == "metadata_mutation"
    assert CONTRACT["branch_retirement_execution_version"] == 1
    assert (
        RETIREMENT["execution_authorization"]["capability"] == "branch_retirement_execution_version"
    )


def test_single_policy_authority_is_the_declared_digest_input() -> None:
    policy_source = (ROOT / "src/worktree_controller/policy.py").read_text(encoding="utf-8")
    retirement_source = Path(retirement.__file__).read_text(encoding="utf-8")
    assert '".agents" / "worktree-policy.toml"' in policy_source
    assert '".agents" / "worktree-policy.toml"' in retirement_source
    assert ".codex" not in policy_source + retirement_source
    assert RETIREMENT["state_digest"]["policy_authority"] == (
        "<canonical repository path>/.agents/worktree-policy.toml is the only policy source"
    )


def test_contract_declares_the_verify_command_and_version_pins() -> None:
    entries = {entry["name"]: entry for entry in CONTRACT["commands"]}
    assert entries["branch-retirement-verify"]["operation_class"] == "read_only"
    assert entries["branch-retirement-verify"]["result_contract"] == "branch-retirement/v2"
    assert entries["branch-retirement-status"]["result_contract"] == "branch-retirement/v1"
    assert CONTRACT["branch_retirement_version"] == 1
    assert CONTRACT["branch_retirement_verify_version"] == 1
    assert CONTRACT["contract_version"] == 61
    assert CONTRACT["branch_retirement_execution_version"] == 1
    assert RETIREMENT["contract"] == "branch-retirement/v5"
    assert RETIREMENT["plan_schema_version"] == retirement.PLAN_SCHEMA_VERSION == 1
