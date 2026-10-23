from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from importlib import resources
from pathlib import Path

from tests.test_retention_convergence import POLICY_V8
from worktree_controller import __version__
from worktree_controller.cli import build_parser, main
from worktree_controller.models import SCHEMA_VERSION
from worktree_controller.policy import load_policy

ROOT = Path(__file__).resolve().parents[1]


def test_missing_repository_is_a_json_error(tmp_path, capsys):
    result = main(["status", "--repo", str(tmp_path / "missing"), "--json"])
    output = capsys.readouterr()
    assert result == 2 and not output.err
    payload = json.loads(output.out)
    assert payload["command"] == "status"
    assert payload["errors"][0]["code"] == "FILESYSTEM_ERROR"


def test_cli_starts_without_system_or_site_timezone_data():
    result = subprocess.run(
        [sys.executable, "-B", "-S", "-m", "worktree_controller.cli", "--version"],
        env={**os.environ, "PYTHONTZPATH": "", "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"worktree-controller {__version__}"


def test_observation_counts_do_not_require_historical_slot_arithmetic(tmp_path):
    source = POLICY_V8
    values = tomllib.loads(source)
    source = source.replace(
        f"normal_limit = {values['limits']['normal_limit']}", "normal_limit = 100"
    )
    source = source.replace(f"hard_limit = {values['limits']['hard_limit']}", "hard_limit = 101")
    policy_path = tmp_path / ".agents/worktree-policy.toml"
    policy_path.parent.mkdir()
    policy_path.write_text(source)
    policy = load_policy(tmp_path)
    assert (policy.normal_limit, policy.hard_limit) == (100, 101)
    assert policy.integration_prepare_limit == values["slots"]["integration_prepare_limit"]
    assert policy.main_publish_limit == 1


def test_public_contract_has_no_duplicate_json_keys() -> None:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            assert key not in result, f"duplicate public contract key: {key}"
            result[key] = value
        return result

    for source in (
        ROOT / "tool_cli_contract.json",
        resources.files("worktree_controller").joinpath("tool_cli_contract.json"),
    ):
        json.loads(source.read_text(encoding="utf-8"), object_pairs_hook=unique_object)


def test_public_contract_matches_package_and_commands(tmp_path: Path) -> None:
    contract = json.loads((ROOT / "tool_cli_contract.json").read_text(encoding="utf-8"))
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    assert contract["schema_version"] == SCHEMA_VERSION == 1
    assert contract["contract_version"] == 61
    assert contract["aborted_integration_preserved_cleanup_version"] == 1
    assert contract["aborted_integration_preserved_cleanup_contract"]["contract"] == "aborted-integration-preserved-cleanup/v1"
    assert contract["canonical_transition_version"] == 1
    assert contract["canonical_transition_contract"]["contract"] == "canonical-transition/v1"
    assert contract["canonical_main_refresh_version"] == 1
    assert contract["integration_lineage_admission_version"] == 1
    assert contract["integration_keyed_document_scope_version"] == 1
    assert contract["integration_keyed_document_scope_contract"]["contract"] == "integration-keyed-document-scope/v1"
    assert contract["integration_lineage_admission_contract"]["contract"] == "integration-lineage-admission/v1"
    assert contract["canonical_main_refresh_contract"]["contract"] == "canonical-main-refresh/v1"
    assert contract["capabilities_version"] == 1
    assert contract["acquire_no_effect_recovery_version"] == 1
    assert contract["storage_layout_version"] == 5
    assert contract["governance_version"] == 2
    assert contract["delivered_closeout_version"] == 1
    assert contract["retained_archive_version"] == 1
    assert contract["retained_typed_cleanup_version"] == 1
    assert contract["preservation_leaf_version"] == 1
    assert contract["retention_convergence_version"] == 2
    assert contract["session_end_selector_version"] == 1
    assert contract["session_closeout_report_version"] == 1
    assert contract["session_closeout_expectations_version"] == 1
    assert contract["recovery_guidance_version"] == 1
    assert contract["hosted_session_closeout_check_version"] == 1
    assert contract["checkout_owner_action_version"] == 1
    assert contract["task_checkpoint_park_version"] == 1
    assert contract["gc_apply_replay_version"] == 1
    assert contract["closed_lifecycle_query_version"] == 1
    assert contract["closed_lifecycle_query_contract"]["contract"] == "closed-lifecycle/v1"
    assert contract["retention_convergence_contract"]["contract"] == "retention-convergence/v2"
    assert contract["tracked_source_progress_recovery_version"] == 1
    assert contract["tracked_source_progress_recovery_contract"]["contract"] == (
        "tracked-source-progress-recovery/v1"
    )
    assert (
        next(row for row in contract["commands"] if row["name"] == "close-delivered")[
            "result_contract"
        ]
        == "worktree-controller/v1"
    )
    assert contract["doctor_mount_version"] == 2
    assert contract["governance_contract"]["contract"] == "worktree-governance/v2"
    commands = {entry["name"]: entry for entry in contract["commands"]}
    assert commands["gc-plan"]["result_contract"] == "worktree-governance/v2"
    assert commands["gc-apply"]["result_contract"] == "worktree-governance/v1"
    assert contract["storage_layout"]["version"] == 5
    assert contract["branch_retirement_contract"]["contract"] == "branch-retirement/v5"
    assert contract["canonical_transition_retirement_version"] == 1
    assert contract["branch_retirement_version"] == 1
    assert contract["branch_retirement_verify_version"] == 1
    assert contract["branch_retirement_contract"]["plan_schema_version"] == 1
    assert (
        contract["transaction_conflict_contract"]["contract"]
        == "controller-transaction-conflict/v1"
    )
    assert (
        contract["tracked_source_no_action_recovery_contract"]["contract"]
        == "tracked-source-no-action-recovery/v1"
    )
    assert (
        contract["metadata_reference_relocation_contract"]["contract"]
        == "metadata-reference-relocation/v1"
    )
    assert contract["legacy_preserved_state_cleanup_contract"]["contract"] == (
        "legacy-preserved-state-cleanup/v1"
    )
    assert contract["typed_disposable_resource_contract"]["version"] == 1
    assert (
        contract["typed_disposable_resource_contract"]["runtime_authority_contract"]
        == "allocation-runtime-authority/v1"
    )
    assert contract["host_managed_writer_session_version"] == 2
    assert contract["writer_session_actions"] == [
        "acquire",
        "check",
        "describe",
        "host-check",
        "release",
    ]
    assert contract["metadata_schema_version"] == 4
    assert contract["tool_version"] == project["version"] == __version__ == "1.32.5"
    assert contract["cleanup_hardlink_topology_contract"]["supported_versions"] == [1, 2]
    assert contract["release_options"] == [
        "--intent-id",
        "--publication-receipt-id",
        "--teardown-reproducible-venv",
        "--dry-run",
    ]
    assert contract["entrypoint"] == "worktree-controller"
    assert {command["name"] for command in contract["commands"]} == {
        "task",
        "recovery-guide",
        "acquire-across",
        "admission-queue",
        "execution-admit",
        "execution-cancel",
        "session-admit",
        "session-bind",
        "host-execution",
        "binding-transfer",
        "branch-retirement-plan",
        "branch-retirement-record",
        "branch-retirement-status",
        "branch-retirement-authorize",
        "branch-retirement-complete",
        "branch-retirement-executions",
        "branch-retirement-verify",
        "namespace-migrate",
        "namespace-migration-plan",
        "namespace-migration-receipts",
        "namespace-migration-verify",
        "acquire-when-free",
        "status",
        "lifecycle-history",
        "release-recovery-inspect",
        "capabilities",
        "canonical-refresh",
        "canonical-transition",
        "plan",
        "write-guard",
        "adopt",
        "doctor",
        "gc-plan",
        "gc-apply",
        "acquire",
        "integration-queue",
        "integration-enqueue",
        "integration-claim",
        "integration-resolve",
        "integration-refresh",
        "integration-validate",
        "integration-reopen",
        "integration-equivalent",
        "lifecycle-events",
        "lifecycle-event-ack",
        "integration-recovery-matrix",
        "adopt-delivered",
        "complete-no-change",
        "close-preserved",
        "close-delivered",
        "archive-retained",
        "integration-abandon",
        "integration-abort",
        "integration-retry",
        "park",
        "reconcile-session",
        "writer-lease-acquire",
        "writer-session",
        "writer-lease-release",
        "reconcile-writers",
        "resume",
        "review",
        "handoff",
        "quarantine",
        "recovery-adopt",
        "externalize",
        "resource-register",
        "resource-clean",
        "resource-recover",
        "lease-acquire",
        "publication-execution-session",
        "lease-complete",
        "lease-abort",
        "release",
        "release-intent",
        "reopen",
        "recover-handoff",
        "recover-published",
        "binding-add",
        "binding-remove",
        "reconcile",
    }
    assert contract["json_envelope"] == [
        "tool_version",
        "schema_version",
        "command",
        "repository",
        "decision",
        "worktrees",
        "warnings",
        "errors",
    ]
    policy_path = tmp_path / ".agents" / "worktree-policy.toml"
    policy_path.parent.mkdir()
    policy_path.write_text(
        POLICY_V8.replace("integration_prepare_limit = 1", "integration_prepare_limit = 2")
        .replace("normal_limit = 5", "normal_limit = 6")
        .replace("hard_limit = 9", "hard_limit = 10")
        .replace(
            "automatic_integration_continuation = true",
            "automatic_integration_continuation = false",
        )
    )
    policy = load_policy(tmp_path)
    assert policy.policy_schema_version == 8
    assert policy.convergence is not None and policy.convergence.enabled
    assert policy.integration_candidate_limit == 2
    assert policy.main_publish_limit == 1
    assert policy.recovery_reserve == 1
    assert policy.automatic_feature_audit_release is True
    assert policy.resource_admission is True
    assert policy.automatic_integration_continuation is False
    assert contract["resource_admission_version"] == 1
    assert contract["runtime_binding_version"] == 1


def test_observation_commands_accept_a_namespace_pid() -> None:
    parser = build_parser()
    for command in ("status", "doctor", "gc-plan"):
        args = parser.parse_args([command, "--namespace-pid", "123"])
        assert args.namespace_pid == 123


def test_packaged_contract_matches_the_repository_contract() -> None:
    packaged = resources.files("worktree_controller").joinpath("tool_cli_contract.json")
    assert packaged.read_bytes() == (ROOT / "tool_cli_contract.json").read_bytes()


def test_release_versions_are_independently_declared_and_advertised(capsys) -> None:
    contract = json.loads((ROOT / "tool_cli_contract.json").read_text(encoding="utf-8"))
    assert main(["capabilities", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    advertised = payload["decision"]
    assert advertised["archived_codex_binding_recovery_version"] == contract[
        "archived_codex_binding_recovery_version"] == 1

    assert payload["tool_version"] == contract["tool_version"] == "1.32.5"
    assert advertised["hosted_session_closeout_check_version"] == contract["hosted_session_closeout_check_version"] == 1
    assert advertised["integration_keyed_document_scope_version"] == contract["integration_keyed_document_scope_version"] == 1
    assert advertised["source_release_summary_version"] == contract["source_release_summary_version"] == 1
    assert contract["source_release_summary_contract"]["field"] == "source_release_trigger.outcomes"
    assert contract["source_release_summary_contract"]["preserved_fields"] == [
        "created_intent_ids", "skipped", "reconciliation"]
    assert advertised["contract_version"] == contract["contract_version"] == 61
    assert advertised["aborted_integration_preserved_cleanup_version"] == contract["aborted_integration_preserved_cleanup_version"] == 1
    assert advertised["typed_cleanup_verify_boundary_version"] == contract[
        "typed_cleanup_verify_boundary_version"] == 1
    assert advertised["binding_registration_backfill_version"] == contract[
        "binding_registration_backfill_version"] == 1
    assert contract["binding_registration_backfill_contract"]["entrypoint"] == "reconcile binding-registrations"
    assert advertised["exited_holder_release_version"] == contract["exited_holder_release_version"] == 1
    assert advertised["integration_conflict_resolution_version"] == contract["integration_conflict_resolution_version"] == 2
    assert advertised["canonical_refresh_version"] == contract["canonical_refresh_version"] == 1
    assert contract["canonical_refresh_contract"]["contract"] == "canonical-refresh/v1"
    diagnostic = contract["canonical_refresh_contract"]["process_diagnostics"]
    assert diagnostic["location"] == "decision.process_diagnostics on live-process CANONICAL_IN_USE"
    assert diagnostic["limits"] == {"processes": 5, "paths_per_process": 3, "path_characters": 512,
                                    "name_characters": 64, "stat_characters": 4096}
    assert advertised["host_handoff_continuation_version"] == contract["host_handoff_continuation_version"] == 2
    assert contract["host_handoff_continuation_contract"]["contract"] == "host-handoff-continuation/v2"
    assert contract["host_handoff_continuation_contract"]["access_classes"] == ["read", "exec", "write"]
    assert advertised["release_recovery_inspection_version"] == contract["release_recovery_inspection_version"] == 1
    inspection = contract["release_recovery_inspection_contract"]
    assert inspection["contract"] == "release-recovery-inspection/v1"
    assert inspection["request_fields"] == [
        "target_path", "expected_record_version", "owner_release", "publication_receipt_id", "release_intent_id",
    ]
    assert next(row for row in contract["commands"] if row["name"] == "release-recovery-inspect") == {
        "name": "release-recovery-inspect", "operation_class": "read_only",
        "result_contract": "release-recovery-inspection/v1",
    }
    assert advertised["rollout_transaction_diagnostics_version"] == contract["rollout_transaction_diagnostics_version"] == 1
    diagnostics = contract["rollout_transaction_diagnostics_contract"]
    assert diagnostics["contract"] == "rollout-transaction-diagnostics/v1"
    assert diagnostics["field"] == "decision.transaction_diagnostics"
    assert diagnostics["fields"] == ["transaction_id", "command", "phase", "error", "recovery_entry"]
    assert diagnostics["error_fields"] == ["code", "message"]
    assert advertised["canonical_main_refresh_version"] == contract["canonical_main_refresh_version"] == 1
    assert advertised["integration_lineage_admission_version"] == contract["integration_lineage_admission_version"] == 1
    assert advertised["canonical_transition_retirement_version"] == contract["canonical_transition_retirement_version"] == 1
    assert (
        advertised["canonical_transition_version"] == contract["canonical_transition_version"] == 1
    )
    assert advertised["capabilities_version"] == contract["capabilities_version"] == 1
    assert advertised["storage_layout_version"] == contract["storage_layout_version"] == 5
    assert contract["storage_layout"]["version"] == advertised["storage_layout_version"]
    assert advertised["retained_archive_version"] == contract["retained_archive_version"] == 1
    assert (
        advertised["retained_typed_cleanup_version"]
        == contract["retained_typed_cleanup_version"]
        == 1
    )
    assert advertised["preservation_leaf_version"] == contract["preservation_leaf_version"] == 1
    assert (
        advertised["published_handoff_resource_registration_version"]
        == contract["published_handoff_resource_registration_version"]
        == 1
    )
    assert contract["published_handoff_resource_registration_contract"]["contract"] == (
        "published-handoff-resource-registration/v1"
    )
    assert (
        advertised["multiple_preservation_cleanup_version"]
        == contract["multiple_preservation_cleanup_version"]
        == 1
    )
    assert (
        contract["multiple_preservation_cleanup_contract"]["contract"]
        == "legacy-preserved-state-multiple-cleanup/v1"
    )
    assert contract["multiple_preservation_cleanup_contract"]["additional_options"] == [
        "--additional-preservation-receipt",
        "--additional-preservation-receipt-sha256",
    ]
    for capability in ("multiple_preservation_recovery_version", "preservation_supersession_version"):
        assert advertised[capability] == contract[capability] == 1
    assert contract["multiple_preservation_recovery_contract"]["contract"] == (
        "multiple-preservation-cleanup-recovery/v1"
    )
    parsed = build_parser().parse_args([
        "resource-recover", "--stage", "prepare-multiple", "--dry-run",
    ])
    assert parsed.stage == "prepare-multiple"
    assert advertised["namespace_migration_version"] == contract["namespace_migration_version"] == 1
    assert (
        advertised["tracked_source_progress_recovery_version"]
        == (contract["tracked_source_progress_recovery_version"])
        == 1
    )


def test_multiple_preservation_options_are_repeatable():
    parsed = build_parser().parse_args(
        [
            "resource-clean",
            "--typed",
            "--dry-run",
            "--path",
            "/synthetic/source",
            "--owner",
            "fixture",
            "--expected-record-version",
            "2",
            "--preservation-receipt",
            "/synthetic/root/receipt.json",
            "--preservation-receipt-sha256",
            "0" * 64,
            "--additional-preservation-receipt",
            "/synthetic/backend/receipt.json",
            "--additional-preservation-receipt",
            "/synthetic/third/receipt.json",
            "--additional-preservation-receipt-sha256",
            "1" * 64,
            "--additional-preservation-receipt-sha256",
            "2" * 64,
        ]
    )
    assert parsed.additional_preservation_receipt == [
        Path("/synthetic/backend/receipt.json"),
        Path("/synthetic/third/receipt.json"),
    ]
    assert parsed.additional_preservation_receipt_sha256 == ["1" * 64, "2" * 64]
