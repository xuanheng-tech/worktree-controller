"""Open-sourcing readiness: retirement governance runs with no consumer product present.

A consumer needs only the published CLI, plain Git and its own bookkeeping.  These
tests drive grant → compare-and-delete → settle, and the legacy verify → CAS →
record path, through the public interface with a mock executor in a scrubbed
environment, so nothing here can quietly depend on one consumer product, a sibling
repository or a host path.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tests.test_auto_release import POLICY_MANUAL
from tests.test_branch_retirement import _released_source
from tests.test_controlled import _configure_remote, adopt_canonical
from tests.test_shadow import GitRepository, git
from worktree_controller.branch_retirement import plan_branch_retirement

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
PACKAGE = SOURCE / "worktree_controller"

MOCK_EXECUTOR = '''"""Mock retirement executor: published CLI in, plain git CAS out, result document back."""
import json
import subprocess
import sys

_PYTHONPATH, _REMOTE = {pythonpath!r}, {remote!r}
controller, repository, plan_id, operation, result_path = sys.argv[1:6]
environment = {{
    "PYTHONPATH": _PYTHONPATH,
    "PATH": "/usr/bin:/bin",
    "PYTHONDONTWRITEBYTECODE": "1",
}}
verdict = json.loads(subprocess.run(
    [sys.executable, "-m", "worktree_controller.cli", "branch-retirement-verify",
     "--repo", repository, "--plan-id", plan_id, "--operation", operation, "--json"],
    capture_output=True, text=True, check=True, env=environment,
).stdout)["decision"]
fields = verdict["minimum_revalidation"]
if verdict["code"] != "RETIREMENT_EXECUTABLE":
    print(json.dumps({{"permitted": False, "code": verdict["code"],
                       "blockers": verdict["blockers"]}}))
    raise SystemExit(1)
ref = "refs/heads/" + fields["branch"]
cas = subprocess.run(
    ["git", "-C", repository, "update-ref", "-d", ref, fields["expected_oid"]],
    capture_output=True, text=True, check=False, env=environment,
)
succeeded = cas.returncode == 0
result = ("LOCAL_BRANCH_RETIRED_VERIFIED" if operation == "local"
          else "REMOTE_BRANCH_RETIRED_VERIFIED") if succeeded else "EXECUTION_FAILED"
summary = {{
    "finalizer_version": "mock-executor/1",
    "mode": "retire_" + operation + "_branch",
    "status": "success" if succeeded else "failed",
    "mode_result": {{
        "retirement_plan_id": fields["plan_id"],
        "result": result,
        "branch": fields["branch"],
        "remote": _REMOTE,
        "expected_oid": fields["expected_oid"],
        "integrated_into_oid": fields["expected_integrated_oid"],
    }},
}}
with open(result_path, "w", encoding="utf-8") as handle:
    handle.write(json.dumps(summary, sort_keys=True))
print(json.dumps({{"permitted": succeeded, "code": verdict["code"]}}))
raise SystemExit(0 if succeeded else 1)
'''


def _environment(tmp_path: Path) -> dict[str, str]:
    """No user site, no host HOME, no PATH entry that could resolve a consumer."""

    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "state").mkdir(exist_ok=True)
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "PYTHONPATH": str(SOURCE),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LC_ALL": "C",
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }


def _cli(*arguments: str, tmp_path: Path) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "worktree_controller.cli", *arguments, "--json"],
        capture_output=True,
        text=True,
        timeout=180,
        env=_environment(tmp_path),
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _ref(repository: Path, ref: str) -> str | None:
    """Observe one ref without the fixture helper's raise-on-nonzero behaviour."""

    result = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--verify", "-q", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or None


def _planned(repository: GitRepository, name: str) -> dict:
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


def test_consumer_needs_only_the_public_cli_git_and_a_result_file(tmp_path: Path) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository, "standalone")
        executor = tmp_path / "mock_executor.py"
        executor.write_text(
            MOCK_EXECUTOR.format(pythonpath=str(SOURCE), remote=plan["remote"]),
            encoding="utf-8",
        )
        result_file = tmp_path / "execution-result.json"
        outcome = subprocess.run(
            [
                sys.executable,
                str(executor),
                "controller",
                str(repository.repo),
                plan["plan_id"],
                "local",
                str(result_file),
            ],
            capture_output=True,
            text=True,
            env=_environment(tmp_path),
            timeout=180,
            check=False,
        )
        assert outcome.returncode == 0, outcome.stdout + outcome.stderr
        assert json.loads(outcome.stdout)["permitted"] is True
        assert _ref(repository.repo, f"refs/heads/{plan['branch']}") is None

        recorded = _cli(
            "branch-retirement-record",
            "--repo",
            str(repository.repo),
            "--plan-id",
            plan["plan_id"],
            "--operation",
            "local",
            "--summary-file",
            str(result_file),
            tmp_path=tmp_path,
        )
        assert recorded["decision"]["code"] == "BRANCH_RETIREMENT_RESULT_RECORDED"
        assert recorded["decision"]["idempotent"] is False
        verify = _cli(
            "branch-retirement-verify",
            "--repo",
            str(repository.repo),
            "--plan-id",
            plan["plan_id"],
            "--operation",
            "local",
            tmp_path=tmp_path,
        )
        assert verify["decision"]["code"] == "RETIREMENT_OPERATION_COMPLETE"
        status = _cli(
            "branch-retirement-status",
            "--repo",
            str(repository.repo),
            "--plan-id",
            plan["plan_id"],
            tmp_path=tmp_path,
        )
        assert status["decision"]["completed_operations"] == ["local"]


def test_mock_executor_is_refused_once_governance_drifts(tmp_path: Path) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository, "standalone")
        checkout = repository.root / "reopened"
        git(repository.repo, "worktree", "add", str(checkout), plan["branch"])
        executor = tmp_path / "mock_executor.py"
        executor.write_text(
            MOCK_EXECUTOR.format(pythonpath=str(SOURCE), remote=plan["remote"]),
            encoding="utf-8",
        )
        outcome = subprocess.run(
            [
                sys.executable,
                str(executor),
                "controller",
                str(repository.repo),
                plan["plan_id"],
                "local",
                str(tmp_path / "unused.json"),
            ],
            capture_output=True,
            text=True,
            env=_environment(tmp_path),
            timeout=180,
            check=False,
        )
        assert outcome.returncode == 1
        payload = json.loads(outcome.stdout)
        assert payload["permitted"] is False
        assert payload["code"] == "RETIREMENT_BLOCKED"
        assert "BRANCH_CHECKED_OUT" in payload["blockers"]
        # The refusal above happened before any mutation: the ref is untouched.
        assert _ref(repository.repo, f"refs/heads/{plan['branch']}") == plan["expected_oid"]


def test_read_only_verbs_run_in_a_scrubbed_environment(tmp_path: Path) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository, "standalone")
        capabilities = _cli("capabilities", tmp_path=tmp_path)
        assert capabilities["decision"]["branch_retirement_verify_version"] == 1
        verify = _cli(
            "branch-retirement-verify",
            "--repo",
            str(repository.repo),
            "--plan-id",
            plan["plan_id"],
            "--operation",
            "local",
            tmp_path=tmp_path,
        )
        assert verify["decision"]["code"] == "RETIREMENT_EXECUTABLE"
        doctor = _cli("doctor", "--repo", str(repository.repo), tmp_path=tmp_path)
        assert doctor["decision"]["code"] in {"PASS", "ATTENTION"}, doctor["decision"]


#: Test modules that may name a host path at all, and only because they never run
#: unless the operator points them at an installation.
OPT_IN_CROSS_REPO_FILES = frozenset({"test_branch_retirement_git_finalizer_acceptance.py"})


def _opt_in_guards(source: str) -> bool:
    """An opt-in file must skip by default and take its paths from the environment."""

    return (
        "pytest.mark.skipif" in source
        and "os.environ.get(" in source
        and "Path(FINALIZER).is_file()" in source
    )


def _allowed_host_path_file(path: Path) -> bool:
    return (
        path.parent == ROOT / "tests"
        and path.name in OPT_IN_CROSS_REPO_FILES
        and _opt_in_guards(path.read_text(encoding="utf-8"))
    )


def test_source_and_tests_carry_no_host_home_dependency() -> None:
    home = str(Path.home())
    offenders = [
        found.relative_to(ROOT).as_posix()
        for tree in (PACKAGE, ROOT / "tests")
        for found in sorted(tree.rglob("*.py"))
        if home in found.read_text(encoding="utf-8") and not _allowed_host_path_file(found)
    ]
    assert not offenders, offenders


def test_source_tree_contains_no_host_absolute_paths() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}: {needle}"
        for path in sorted(PACKAGE.rglob("*.py"))
        for needle in _HOST_NEEDLES + ("/Users/", "/root/")
        if needle in path.read_text(encoding="utf-8")
    ]
    assert not offenders, offenders


# Assembled from the running home so no test file carries a literal host path,
# which would otherwise make this very scanner its own offender.
_HOST_NEEDLES = (str(Path.home()),)


def test_default_tests_only_read_this_repository() -> None:
    """A default-suite file may not inspect a sibling project or a home directory."""

    offenders = []
    for found in sorted((ROOT / "tests").glob("test_*.py")):
        source = found.read_text(encoding="utf-8")
        for needle in _HOST_NEEDLES:
            if needle in source and not _allowed_host_path_file(found):
                offenders.append(f"{found.name}: {needle}")
    assert not offenders, offenders


def test_the_opt_in_acceptance_gate_cannot_silently_run() -> None:
    """The exception is structural: no default run may reach an external installation."""

    for name in sorted(OPT_IN_CROSS_REPO_FILES):
        source = (ROOT / "tests" / name).read_text(encoding="utf-8")
        assert _opt_in_guards(source), f"{name} lost its environment gate"
        assert "WORKTREE_CONTROLLER_GIT_FINALIZER" in source


GRANT_CONSUMER = '''"""Mock granted consumer: authorize, plain git CAS, settle - published verbs only."""
import json
import subprocess
import sys
import uuid

PYTHONPATH = sys.argv[1]
repository, plan_id, operation, result_path = sys.argv[2:6]
environment = {"PYTHONPATH": PYTHONPATH, "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}


def controller(*arguments):
    return subprocess.run(
        [sys.executable, "-m", "worktree_controller.cli", *arguments, "--json"],
        capture_output=True, text=True, check=True, env=environment,
    ).stdout


granted = json.loads(controller(
    "branch-retirement-authorize", "--repo", repository, "--plan-id", plan_id,
    "--operation", operation, "--request-id", str(uuid.uuid4()),
))["decision"]
if granted["code"] != "RETIREMENT_AUTHORIZED":
    print(json.dumps({"permitted": False, "code": granted["code"],
                      "blockers": granted["blockers"]}))
    raise SystemExit(1)
grant = granted["authorization"]
cas = subprocess.run(
    ["git", "-C", repository, "update-ref", "-d", grant["ref"], grant["expected_oid"]],
    capture_output=True, text=True, check=False, env=environment,
)
document = {
    "schema_version": 1,
    "contract": "retirement-execution-result/v1",
    "plan_id": grant["plan_id"],
    "operation": grant["operation"],
    "kind": grant["kind"],
    "outcome": "REF_REMOVED_AT_EXPECTED_OID" if cas.returncode == 0 else "EXECUTION_FAILED",
    "ref": grant["ref"],
    "expected_oid": grant["expected_oid"],
    "expected_integrated_oid": grant["expected_integrated_oid"],
    "performed_at": "2026-09-25T00:00:00+08:00",
    "executor": {"name": "mock-retirement-consumer", "version": "1"},
}
with open(result_path, "w", encoding="utf-8") as handle:
    handle.write(json.dumps(document, sort_keys=True))
settled = json.loads(controller(
    "branch-retirement-complete", "--repo", repository,
    "--authorization-id", grant["authorization_id"], "--token", grant["token"],
    "--result-file", result_path,
))["decision"]
print(json.dumps({"permitted": settled["code"] == "RETIREMENT_EXECUTION_COMPLETE",
                  "code": settled["code"], "provenance": settled.get("provenance")}))
raise SystemExit(0 if settled["code"] == "RETIREMENT_EXECUTION_COMPLETE" else 1)
'''


def test_a_granted_consumer_settles_using_only_the_published_verbs(tmp_path: Path) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository, "granted")
        consumer = tmp_path / "granted_consumer.py"
        consumer.write_text(GRANT_CONSUMER, encoding="utf-8")
        outcome = subprocess.run(
            [
                sys.executable,
                str(consumer),
                str(SOURCE),
                str(repository.repo),
                plan["plan_id"],
                "local",
                str(tmp_path / "result.json"),
            ],
            capture_output=True,
            text=True,
            env=_environment(tmp_path),
            timeout=300,
            check=False,
        )
        assert outcome.returncode == 0, outcome.stdout + outcome.stderr
        report = json.loads(outcome.stdout)
        assert report["permitted"] is True
        assert report["provenance"] == "AUTHORIZED_EXECUTION"
        verify = _cli(
            "branch-retirement-verify",
            "--repo",
            str(repository.repo),
            "--plan-id",
            plan["plan_id"],
            "--operation",
            "local",
            tmp_path=tmp_path,
        )["decision"]
        assert verify["code"] == "RETIREMENT_OPERATION_COMPLETE"


def test_recovery_verbs_work_with_no_consumer_installed(tmp_path: Path) -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository, "recovery")
        granted = _cli(
            "branch-retirement-authorize",
            "--repo",
            str(repository.repo),
            "--plan-id",
            plan["plan_id"],
            "--operation",
            "local",
            "--request-id",
            str(uuid.uuid4()),
            tmp_path=tmp_path,
        )["decision"]
        assert granted["code"] == "RETIREMENT_AUTHORIZED"
        listed = _cli(
            "branch-retirement-executions",
            "--repo",
            str(repository.repo),
            "--action",
            "list",
            tmp_path=tmp_path,
        )["decision"]
        assert [item["authorization_id"] for item in listed["authorizations"]] == [
            granted["authorization_id"]
        ]
        doctor = _cli("doctor", "--repo", str(repository.repo), tmp_path=tmp_path)
        assert "RETIREMENT_AUTHORIZATION_OUTSTANDING" in [
            warning["code"] for warning in doctor["warnings"]
        ]
        assert doctor["decision"]["retirement_executions"]["outstanding"]
        abandoned = _cli(
            "branch-retirement-executions",
            "--repo",
            str(repository.repo),
            "--action",
            "abandon",
            "--authorization-id",
            granted["authorization_id"],
            "--token",
            granted["authorization"]["token"],
            "--reason",
            "consumer disappeared",
            tmp_path=tmp_path,
        )["decision"]
        assert abandoned["code"] == "RETIREMENT_EXECUTION_ABANDONED"
        assert abandoned["receipt_filed"] is False
        settled_doctor = _cli("doctor", "--repo", str(repository.repo), tmp_path=tmp_path)
        assert "RETIREMENT_AUTHORIZATION_OUTSTANDING" not in [
            warning["code"] for warning in settled_doctor["warnings"]
        ]


def test_installed_package_grants_and_recovers_without_any_consumer(tmp_path: Path) -> None:
    """Build the wheel and drive grant, recovery and capabilities from that install."""

    uv = shutil.which("uv")
    if uv is None:  # pragma: no cover - a host without uv still runs every other test
        pytest.skip("uv is not installed")
    wheels = tmp_path / "dist"
    build = subprocess.run(
        [uv, "build", "--offline", "--out-dir", str(wheels)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    artifact = sorted(wheels.glob("*.whl"))[-1]
    venv = tmp_path / "venv"
    for command in (
        [uv, "venv", "--offline", str(venv)],
        # Like check-project's package smoke, isolate this dependency-free surface.
        # The native Codex transport is lazy and is tested with the locked dev environment.
        [uv, "pip", "install", "--offline", "--no-index", "--no-deps",
         "--python", str(venv / "bin/python"), str(artifact)],
    ):
        step = subprocess.run(
            command,
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        assert step.returncode == 0, step.stdout + step.stderr
    installed = venv / "bin" / "worktree-controller"
    assert installed.is_file()

    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "state").mkdir(exist_ok=True)
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _planned(repository, "installed")
        granted = json.loads(
            subprocess.run(
                [
                    str(installed),
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
                    str(installed),
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
        assert len(listed["authorizations"]) == 1
        capabilities = json.loads(
            subprocess.run(
                [str(installed), "capabilities", "--json"],
                capture_output=True,
                text=True,
                env=environment,
                timeout=180,
                check=True,
            ).stdout
        )["decision"]
        assert capabilities["branch_retirement_execution_version"] == 1
