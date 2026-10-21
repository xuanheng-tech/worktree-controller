"""Explicit cross-repository acceptance against a real consumer installation.

Skipped by default: the governed suite must never depend on a sibling project or a
host binary (see tests/test_retirement_standalone.py for the self-contained proof).
Set both environment variables to run the shipped consumer against a plan produced
here, which is how the published contract is checked for real:

    WORKTREE_CONTROLLER_GIT_FINALIZER=/absolute/path/to/git-finalize
    WORKTREE_CONTROLLER_GIT_FINALIZER_COMPANION=/absolute/path/to/git-finalize-retirement-plan.py

The consumer and companion operate only on throwaway fixture repositories. The
canonical-transition case also verifies real expected-OID local deletion and completion.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_auto_release import POLICY_MANUAL
from tests.test_branch_retirement import _released_source
from tests.test_canonical_transition import initial_policy
from tests.test_canonical_transition_retirement import planned, transitioned
from tests.test_controlled import _configure_remote, adopt_canonical
from tests.test_shadow import GitRepository, git
from worktree_controller.branch_retirement import (
    PLAN_SCHEMA_VERSION,
    authorize_branch_retirement,
    branch_retirement_status,
    plan_branch_retirement,
    verify_branch_retirement,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
FINALIZER = os.environ.get("WORKTREE_CONTROLLER_GIT_FINALIZER")
COMPANION = os.environ.get("WORKTREE_CONTROLLER_GIT_FINALIZER_COMPANION")

pytestmark = pytest.mark.skipif(
    not (FINALIZER and COMPANION and Path(FINALIZER).is_file() and Path(COMPANION).is_file()),
    reason="set WORKTREE_CONTROLLER_GIT_FINALIZER and ..._COMPANION to run cross-repo acceptance",
)


def _fixture(repository: GitRepository) -> dict:
    """A repository whose remote advertises a default branch, as a real one does."""

    adopt_canonical(repository)
    _configure_remote(repository)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository.root / "remote.git"),
            "symbolic-ref",
            "HEAD",
            "refs/heads/main",
        ],
        check=False,
    )
    allocation, _head = _released_source(repository, "acceptance")
    plan = plan_branch_retirement(
        repository.repo,
        allocation_id=allocation,
        remote="origin",
        integrated_into="main",
    )["decision"]["plan"]
    subprocess.run(
        ["git", "-C", str(repository.repo), "fetch", "origin", "main"],
        capture_output=True,
        check=False,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository.repo),
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/main",
        ],
        capture_output=True,
        check=False,
    )
    return plan


def _companion(repository: Path, plan: dict, operation: str) -> tuple[int, str]:
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            COMPANION,
            "--repo",
            str(repository),
            "--plan-id",
            plan["plan_id"],
            "--operation",
            operation,
            "--branch",
            plan["branch"],
            "--remote",
            plan["remote"],
            "--integrated-into",
            plan["integrated_into"],
            "--expected-oid",
            plan["expected_oid"],
            "--expected-integrated-oid",
            plan["expected_integrated_oid"],
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "PYTHONPATH": str(SOURCE)},
        check=False,
    )
    return result.returncode, (result.stderr or result.stdout).strip()


def _finalize(repository: Path, plan: dict, operation: str, *, dry_run: bool = True) -> dict:
    expected_flag = "--expected-local-oid" if operation == "local" else "--expected-remote-oid"
    result = subprocess.run(
        [
            FINALIZER,
            "--summary",
            f"--retire-{operation}-branch",
            plan["branch"],
            "--remote",
            plan["remote"],
            "--integrated-into",
            plan["integrated_into"],
            expected_flag,
            plan["expected_oid"],
            "--expected-integrated-oid",
            plan["expected_integrated_oid"],
            "--retirement-plan-id",
            plan["plan_id"],
            "--repo",
            str(repository),
            *(["--dry-run"] if dry_run else []),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "PYTHONPATH": str(SOURCE)},
        check=False,
    )
    payload = [
        line for line in (result.stdout or result.stderr).splitlines() if line.startswith("{")
    ]
    return {"returncode": result.returncode, "summary": json.loads(payload[-1]) if payload else {}}


def test_the_released_consumer_accepts_a_controller_plan_and_rejects_drift() -> None:
    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _fixture(repository)
        code, detail = _companion(repository.repo, plan, "local")
        assert code == 0, detail
        accepted = _finalize(repository.repo, plan, "local")
        assert accepted["returncode"] == 0, accepted["summary"]
        assert accepted["summary"]["status"] == "success"
        assert accepted["summary"]["dry_run"] is True
        assert (
            verify_branch_retirement(repository.repo, plan_id=plan["plan_id"], operation="local")[
                "decision"
            ]["code"]
            == "RETIREMENT_EXECUTABLE"
        )

        tree = git(repository.repo, "rev-parse", f"{plan['expected_oid']}^{{tree}}")
        advanced = subprocess.run(
            [
                "git",
                "-C",
                str(repository.repo),
                "commit-tree",
                tree,
                "-p",
                plan["expected_oid"],
                "-m",
                "acceptance drift",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        git(repository.repo, "branch", "-f", plan["branch"], advanced)
        assert (
            verify_branch_retirement(repository.repo, plan_id=plan["plan_id"], operation="local")[
                "decision"
            ]["code"]
            == "RETIREMENT_STALE"
        )
        drifted = _finalize(repository.repo, plan, "local")
        assert drifted["returncode"] != 0, drifted["summary"]

    with GitRepository(linked=0, policy_text=initial_policy("main")) as repository:
        transitioned(repository)
        git(repository.root / "origin.git", "symbolic-ref", "HEAD", "refs/heads/main")
        git(repository.repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        plan = planned(repository)["plan"]
        assert plan["registration"] == "CANONICAL_TRANSITION_BACKED"
        code, detail = _companion(repository.repo, plan, "local")
        assert code == 0, detail
        removed = _finalize(repository.repo, plan, "local", dry_run=False)
        assert removed["returncode"] == 0, removed["summary"]
        repeated = _finalize(repository.repo, plan, "local", dry_run=False)
        assert repeated["returncode"] == 0, repeated["summary"]
        progress = branch_retirement_status(repository.repo, plan_id=plan["plan_id"])["decision"]
        assert progress["completed_operations"] == ["local"]
        assert progress["pending_operations"] == ["remote"]
        assert git(repository.repo, "symbolic-ref", "--short", "HEAD") == "main"


def test_the_grant_surface_and_the_consumer_pin_coexist() -> None:
    """The pinned consumer accepts a free plan and respects an occupied grant."""

    with GitRepository(linked=0, policy_text=POLICY_MANUAL) as repository:
        plan = _fixture(repository)
        capabilities = subprocess.run(
            [sys.executable, "-m", "worktree_controller.cli", "capabilities", "--json"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=120,
            env={**os.environ, "PYTHONPATH": str(SOURCE)},
            check=True,
        ).stdout
        decision = json.loads(capabilities)["decision"]
        assert decision["branch_retirement_version"] == PLAN_SCHEMA_VERSION == 1
        assert decision["branch_retirement_execution_version"] == 1
        code, detail = _companion(repository.repo, plan, "local")
        assert code == 0, detail
        granted = authorize_branch_retirement(
            repository.repo,
            plan_id=plan["plan_id"],
            operation="local",
            request_id="2f7e1a44-64d0-4e15-a3d8-a91b0d0f4a71",
        )["decision"]
        assert granted["code"] == "RETIREMENT_AUTHORIZED"
        verdict = verify_branch_retirement(
            repository.repo, plan_id=plan["plan_id"], operation="local"
        )["decision"]
        assert "RETIREMENT_AUTHORIZATION_ACTIVE" in verdict["blockers"]
        code, detail = _companion(repository.repo, plan, "local")
        assert code != 0 and "blocks this operation" in detail, detail
