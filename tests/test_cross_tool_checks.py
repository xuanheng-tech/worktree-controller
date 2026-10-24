"""The optional gate refuses stale pins, caller-owned output and skipped acceptance."""

import json
import runpy
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from tests.test_project_checks import (
    PYTHON_MODES,
    check_environment,
    copied_project,
    python_command,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check-cross-tool"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def arguments(destination, controller, finalizer, companion, version=VERSION):
    return [sys.executable, str(SCRIPT), "--work-dir", str(destination),
            "--controller", str(controller), "--controller-version", version,
            "--git-finalizer", str(finalizer), "--finalizer-version", "1.8.9",
            "--retirement-companion", str(companion), "--offline"]


@pytest.mark.parametrize("directory", [False, True])
def test_existing_output_is_preserved(tmp_path, directory):
    destination = tmp_path / "existing"
    if directory:
        destination.mkdir()
        marker = destination / "keep.txt"
    else:
        marker = destination
    marker.write_text("caller-owned data")
    result = subprocess.run(arguments(destination, SCRIPT, SCRIPT, SCRIPT),
                            capture_output=True, text=True, check=False, timeout=30)
    assert result.returncode == 2
    assert "must not already exist" in result.stderr
    assert marker.read_text() == "caller-owned data"
    assert not result.stdout


def test_wrong_controller_pin_stops_before_output_or_dependency_changes(tmp_path):
    destination = tmp_path / "output"
    result = subprocess.run(arguments(destination, SCRIPT, SCRIPT, SCRIPT, "0.0.0"),
                            capture_output=True, text=True, check=False, timeout=30)
    assert result.returncode == 2
    assert "checkout version does not match" in result.stderr
    assert not destination.exists()
    assert not result.stdout


@pytest.mark.parametrize("outcome", [
    "skipped", "failure", "error", "missing", "unexpected", "duplicate", "extra",
    "missing-publication-wrong-run", "missing-publication-dirty-candidate",
    "missing-publication-split-endpoints",
    "missing-remote-advance", "missing-remote-unavailable",
    "missing-session-premature", "missing-session-unobserved", "missing-session-disconnect",
])
def test_incomplete_optional_acceptance_cannot_pass(tmp_path, outcome):
    helpers = runpy.run_path(str(SCRIPT))
    nodes = helpers["TESTS"]
    cases = [f'<testcase name="{node.rsplit("::", 1)[1]}" />' for node in nodes]
    if outcome.startswith("missing-session-"):
        marker = {"missing-session-premature": "refuses_complete_before_authorization",
                  "missing-session-unobserved": "refuses_completion_without_remote_push",
                  "missing-session-disconnect": "disconnect_releases_lock"}[outcome]
        omitted = next(index for index, node in enumerate(nodes)
                       if "test_installed_publication_session_acceptance.py::" in node and marker in node)
        cases.pop(omitted)
    elif outcome.startswith("missing-remote-"):
        marker = {"missing-remote-advance": "refuses_remote_advance",
                  "missing-remote-unavailable": "refuses_unavailable_remote"}[outcome]
        omitted = next(index for index, node in enumerate(nodes)
                       if "test_installed_publication_remote_acceptance.py::" in node and marker in node)
        cases.pop(omitted)
    elif outcome.startswith("missing-publication-"):
        marker = {"missing-publication-wrong-run": "refuses_wrong_run",
                  "missing-publication-dirty-candidate": "refuses_dirty_candidate",
                  "missing-publication-split-endpoints": "refuses_split_endpoints"}[outcome]
        omitted = next(index for index, node in enumerate(nodes)
                       if "test_installed_publication_refusal_acceptance.py::" in node and marker in node)
        cases.pop(omitted)
    elif outcome == "missing":
        cases.pop()
    elif outcome == "unexpected":
        cases[-1] = '<testcase name="unselected_case" />'
    elif outcome == "duplicate":
        cases[-1] = cases[0]
    elif outcome == "extra":
        cases.append(cases[0])
    else:
        cases[-1] = cases[-1].replace(" />", f"><{outcome} /></testcase>")
    report = tmp_path / "pytest.xml"
    report.write_text("<testsuite>" + "".join(cases) + "</testsuite>")
    with pytest.raises(AssertionError, match="all selected cases to pass without skips"):
        helpers["verify_results"](report)


def tool_fixture(root, mismatch):
    contract = json.loads((root / "tool_cli_contract.json").read_text())
    capability = {
        "errors": [],
        "decision": {key: contract[key] for key in (
            "contract_version", "storage_layout_version", "host_handoff_continuation_version",
            "exited_holder_release_version",
            "integration_conflict_resolution_version",
            "retention_convergence_version",
            "integration_publication_execution_version",
        )},
    }
    if mismatch in capability["decision"]:
        capability["decision"][mismatch] += 1
    controller = root / "bin/worktree-controller"
    version = "0.0.0" if mismatch == "controller" else VERSION
    controller.write_text(f"""#!{sys.executable}
import sys
print({('worktree-controller ' + version)!r} if '--version' in sys.argv else {json.dumps(capability)!r})
""", encoding="utf-8")
    controller.chmod(0o755)
    finalizer = root / "bin/git-finalize"
    version = "0.0.0" if mismatch == "finalizer" else "1.8.9"
    finalizer.write_text(f"#!{sys.executable}\nprint('git-finalize {version}')\n", encoding="utf-8")
    finalizer.chmod(0o755)
    companion = root / "bin/companion.py"
    companion.write_text("# fixture companion\n", encoding="utf-8")
    (root / ".venv/bin").mkdir(parents=True)
    python = root / ".venv/bin/python"
    python.write_text(f"""#!{sys.executable}
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
root = Path(__file__).resolve().parents[2]
suite = ET.Element('testsuite')
for node in sys.argv[1:]:
    if '::' in node:
        ET.SubElement(suite, 'testcase', name=node.rsplit('::', 1)[1])
ET.ElementTree(suite).write(sys.argv[sys.argv.index('--junitxml') + 1])
if {mismatch == 'drift'!r}:
    with (root / 'bin/git-finalize').open('a') as stream:
        stream.write('# changed after tests\\n')
""", encoding="utf-8")
    python.chmod(0o755)
    return controller, finalizer, companion


@pytest.mark.parametrize("mode", PYTHON_MODES)
@pytest.mark.parametrize("mismatch", [
    "controller", "finalizer", "contract_version", "storage_layout_version",
    "host_handoff_continuation_version", "exited_holder_release_version",
    "integration_conflict_resolution_version", "retention_convergence_version",
    "integration_publication_execution_version", "drift",
])
def test_pinned_gate_rejects_tool_mismatch_with_optimization(tmp_path, mode, mismatch):
    root = copied_project(tmp_path)
    tools = tool_fixture(root, mismatch)
    command = arguments(root / "output", *tools)
    command[0:2] = [*python_command(mode), str(root / "scripts/check-cross-tool")]
    result = subprocess.run(
        command, env=check_environment(root, mode), capture_output=True, text=True,
        check=False, timeout=30,
    )
    assert result.returncode == 1
    if mismatch in ("controller", "finalizer"):
        assert "tool version mismatch" in result.stderr
    elif mismatch == "drift":
        assert "tool entry points changed during acceptance" in result.stderr
        assert (root / "output/pytest.xml").is_file()
    else:
        assert f"{mismatch} mismatch" in result.stderr
    assert not (root / "output/cross-tool.json").exists()
    if mismatch != "drift":
        assert not (root / "dependencies-touched").exists()


def rollout_runtime(tmp_path, *, source="file:///fixture/source", reply=None, exit_code=0):
    runtime = tmp_path / "versions" / ("a" * 40) / "worktree-controller"
    (runtime / "bin").mkdir(parents=True)
    binary = runtime / "bin/worktree-controller"
    payload = reply if reply is not None else {
        "decision": {"code": "CAPABILITIES", "contract_version": 61}, "errors": [],
    }
    binary.write_text(f"#!{sys.executable}\nprint({json.dumps(payload)!r})\n"
                      f"raise SystemExit({exit_code})\n")
    binary.chmod(0o755)
    (runtime / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{name = "worktree-controller", git = '
        + json.dumps(source + "?rev=" + "a" * 40) + '}]\n',
    )
    return binary


@pytest.mark.parametrize("source", ["file:///fixture/source", "ssh://git@example.invalid/wc.git"])
def test_rollout_pin_uses_declared_staged_source(tmp_path, source):
    binary = rollout_runtime(tmp_path, source=source)
    helpers = runpy.run_path(str(SCRIPT))
    assert helpers["rollout_capabilities"](binary, source)["contract_version"] == 61
    with pytest.raises(ValueError, match="declared Git source and commit"):
        helpers["rollout_capabilities"](binary, source + "/other")


def test_rollout_pin_rejects_receipt_commit_drift(tmp_path):
    binary = rollout_runtime(tmp_path)
    receipt = binary.parent.parent / "uv-receipt.toml"
    receipt.write_text(receipt.read_text().replace("a" * 40, "b" * 40))
    with pytest.raises(ValueError, match="declared Git source and commit"):
        runpy.run_path(str(SCRIPT))["rollout_capabilities"](binary, "file:///fixture/source")


@pytest.mark.parametrize("reply,exit_code", [
    ({"decision": {"code": "RETIREMENT_STALE", "contract_version": 61}, "errors": []}, 0),
    ({"decision": {"code": "CAPABILITIES"}, "errors": [{"code": "ERROR"}]}, 0),
    ({"decision": {"code": "CAPABILITIES"}, "errors": []}, 2),
    ({"decision": [], "errors": []}, 0),
    ([], 0),
])
def test_rollout_pin_requires_successful_decision_not_just_zero_exit(tmp_path, reply, exit_code):
    binary = rollout_runtime(tmp_path, reply=reply, exit_code=exit_code)
    with pytest.raises(ValueError, match="successful CAPABILITIES decision"):
        runpy.run_path(str(SCRIPT))["rollout_capabilities"](binary, "file:///fixture/source")


@pytest.mark.parametrize("current,previous", [
    ({"contract_version": 61, "terminal_preserved_cleanup_version": 1}, {"contract_version": 61}),
    ({"contract_version": 61, "terminal_preserved_cleanup_version": 1},
     {"contract_version": 60, "terminal_preserved_cleanup_version": 1}),
    ({"contract_version": 61}, {"contract_version": 60}),
])
def test_historical_cohort_refuses_unsuitable_version_pairs(current, previous):
    helpers = runpy.run_path(str(SCRIPT))
    with pytest.raises(ValueError, match="historical rollout cohort not applicable"):
        helpers["historical_rollout_tests"](current, previous)


def test_historical_cases_are_separate_and_require_a_real_capability_gap():
    helpers = runpy.run_path(str(SCRIPT))
    historical = helpers["historical_rollout_tests"](
        {"contract_version": 61, "terminal_preserved_cleanup_version": 1},
        {"contract_version": 60},
    )
    assert historical == helpers["HISTORICAL_ROLLOUT_TESTS"]
    assert len(historical) == 2 and len(helpers["ROLLOUT_TESTS"]) == 20
    assert set(historical).isdisjoint(helpers["ROLLOUT_TESTS"])


@pytest.mark.parametrize("extra", [
    ["--rollout-previous-controller", "/fixture/previous"],
    ["--rollout-git-source", "file:///fixture/source"],
    ["--rollout-historical-controller", "/fixture/historical"],
])
def test_rollout_prerequisites_stop_before_output_or_dependencies(tmp_path, extra):
    destination = tmp_path / "output"
    result = subprocess.run(arguments(destination, SCRIPT, SCRIPT, SCRIPT) + extra,
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 2 and "requires --rollout-" in result.stderr
    assert not destination.exists()


def test_unsuitable_historical_pin_is_not_misreported_as_passed_or_silently_skipped(tmp_path):
    binary = rollout_runtime(tmp_path)
    destination = tmp_path / "output"
    result = subprocess.run(arguments(destination, binary, SCRIPT, SCRIPT) + [
        "--rollout-previous-controller", str(binary),
        "--rollout-historical-controller", str(binary),
        "--rollout-git-source", "file:///fixture/source",
    ], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 2 and "historical rollout cohort not applicable" in result.stderr
    assert not destination.exists() and not result.stdout
