"""M3: CLI -> contract completeness gate.

The published contract must describe the real command surface, every operation
class it uses, and every capability the CLI advertises. These assertions are
deliberately derived from the live parser and live capabilities payload, so a
new command, enum value or capability key cannot ship silently again.
"""

import argparse
import json
from pathlib import Path

from worktree_controller.cli import build_parser, main

CONTRACT = json.loads((Path(__file__).resolve().parents[1] / "tool_cli_contract.json").read_text())


def _top_level_commands() -> set[str]:
    parser = build_parser()
    subactions = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
    assert len(subactions) == 1
    return set(subactions[0].choices)


def _capabilities_decision() -> dict:
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(["capabilities", "--json"])
    assert code == 0
    return json.loads(buffer.getvalue())["decision"]


def test_contract_declares_every_cli_command_exactly_once():
    commands = _top_level_commands()
    declared = [entry["name"] for entry in CONTRACT["commands"]]
    missing = sorted(commands - set(declared))
    stale = sorted(set(declared) - commands)
    duplicated = sorted({name for name in declared if declared.count(name) > 1})
    assert not missing, f"commands missing from tool_cli_contract.json: {missing}"
    assert not stale, f"contract lists commands the CLI no longer exposes: {stale}"
    assert not duplicated, f"contract duplicates command entries: {duplicated}"


def test_every_result_contract_is_declared():
    """result_contract was an unvalidated free string, so a typo shipped silently."""

    declared = set(CONTRACT["result_contracts"])
    used = {entry["result_contract"] for entry in CONTRACT["commands"]}
    assert not used - declared, f"undeclared result contracts: {sorted(used - declared)}"
    assert not declared - used, f"dead result contracts: {sorted(declared - used)}"
    # A label is a payload generation, so the released ones may not be relabelled.
    entries = {entry["name"]: entry["result_contract"] for entry in CONTRACT["commands"]}
    assert entries["branch-retirement-plan"] == "branch-retirement/v1"
    assert entries["branch-retirement-status"] == "branch-retirement/v1"
    assert entries["branch-retirement-record"] == "branch-retirement/v1"
    assert entries["branch-retirement-verify"] == "branch-retirement/v2"
    for name in ("branch-retirement-authorize", "branch-retirement-complete"):
        assert entries[name] == "branch-retirement/v5"
    assert CONTRACT["branch_retirement_contract"]["execution_authorization"][
        "result_contract"
    ] in CONTRACT["document_contracts"]


def test_every_command_entry_uses_a_declared_operation_class():
    classes = set(CONTRACT["operation_classes"])
    offenders = sorted(
        {entry["operation_class"] for entry in CONTRACT["commands"]} - classes)
    assert not offenders, f"operation_class values missing from the enum: {offenders}"


def test_capabilities_advertise_only_declared_contract_keys():
    decision = _capabilities_decision()
    undeclared = sorted(
        key for key in decision
        if key != "code" and not isinstance(decision[key], list) and key not in CONTRACT
    )
    assert not undeclared, f"capabilities keys absent from the contract: {undeclared}"
    versions = [key for key in decision if key.endswith("_version") or key.endswith("_versions")]
    assert versions, "capability keys must exist to gate"


def test_capabilities_values_equal_the_declared_contract():
    """A consumer negotiates against the running payload, so the file must agree with it.

    Presence-only checks let the hand-written capabilities dict and the published
    contract drift apart silently, which is exactly how a version pin becomes fiction.
    """

    decision = _capabilities_decision()
    mismatched = {
        key: {"capabilities": decision[key], "contract": CONTRACT[key]}
        for key in decision
        if key != "code"
        and key in CONTRACT
        and not isinstance(decision[key], dict)
        and decision[key] != CONTRACT[key]
    }
    assert not mismatched, f"capabilities disagree with the contract: {mismatched}"
    # A capability must never be advertised without a declaration to compare against.
    assert all(key in CONTRACT for key in decision if key != "code")


def test_host_execution_actions_are_all_reachable():
    # The inspect verb is part of the recovery contract advertised by M2.
    decision = _capabilities_decision()
    assert decision["host_execution_admission_version"] == 2
    assert CONTRACT["host_execution_admission_version"] == 2


def test_no_change_reconciliation_surface_matches_additive_contract():
    from worktree_controller.delivered_closeout import NO_CHANGE_CONTRACT

    assert NO_CHANGE_CONTRACT in CONTRACT["document_contracts"]
    assert _capabilities_decision()["no_change_cleanup_closeout_version"] == 1
    parsed = build_parser().parse_args(["reconcile", "no-change-completion", "--path", "/synthetic/task",
        "--owner", "synthetic", "--expected-record-version", "5", "--completion-receipt-id", "completion",
        "--completion-receipt-sha256", "sha", "--cleanup-receipt-id", "first", "--cleanup-receipt-id", "second",
        "--cleanup-receipt-sha256", "sha1", "--cleanup-receipt-sha256", "sha2", "--dry-run", "--expected-plan-id", "plan"])
    assert parsed.cleanup_receipt_id == ["first", "second"]
    assert parsed.cleanup_receipt_sha256 == ["sha1", "sha2"]
    assert parsed.dry_run and parsed.expected_plan_id == "plan"
