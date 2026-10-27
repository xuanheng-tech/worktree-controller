"""Rollout regressions against real Git directories and the neutral session contract."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_controlled import acquire_one, adopt_canonical
from tests.test_shadow import GitRepository, git
from tests.test_writer_lease import POLICY_V6
from worktree_controller import writer_lease, writer_session
from worktree_controller.controller import adopt, guard_write
from worktree_controller.models import (
    CANONICAL_ROLES,
    FEATURE_AUTHORITY_ROLES,
    WORKTREE_ROLES,
    ControllerError,
)
from worktree_controller.store import MetadataStore


@pytest.fixture
def task():
    with GitRepository(linked=0, policy_text=POLICY_V6) as repo:
        adopt_canonical(repo)
        path, payload = acquire_one(repo, task="rollout", authority="rollout")
        yield repo, path, payload["decision"]["allocation_id"]


@pytest.mark.parametrize("role", sorted(WORKTREE_ROLES))
def test_every_formal_role_has_positive_applicability_and_its_own_gate(role):
    with GitRepository(linked=0 if role in CANONICAL_ROLES else 1, policy_text=POLICY_V6) as repo:
        path = repo.repo if role in CANONICAL_ROLES else repo.linked[0]
        if path != repo.repo:
            adopt_canonical(repo)
        adopt(
            repo.repo,
            target=path,
            task_key="role-task",
            authority_key="role-authority",
            module="role",
            role=role,
            owner="role-owner",
            lifecycle="ACTIVE",
        )
        holder = writer_lease.describe_holder(os.getpid(), client_id="manual")
        arguments = dict(
            target=path, task_key="role-task", authority_key="role-authority", owner="role-owner"
        )
        before = guard_write(repo.repo, **arguments, operation="feature-write")["decision"]
        assert before["code"] != "WRITE_ALLOWED"
        for invalid in ({"owner": "wrong"}, {"expected_record_version": 999}):
            with pytest.raises(ControllerError):
                writer_lease.acquire_writer_lease(
                    repo.repo, target=path, client_id="manual", holder_pid=holder.pid, **invalid
                )
        with pytest.raises(ControllerError):
            writer_lease.acquire_writer_lease(
                repo.repo,
                target=path,
                client_id="manual",
                holder_pid=holder.pid,
                holder=replace(holder, start_ticks=holder.start_ticks + 1),
            )
        decision = writer_session.admission(path, client_id="manual", selector=str(holder.pid))
        if role in FEATURE_AUTHORITY_ROLES:
            assert writer_lease.writer_requirement(role) == "REQUIRED"
            assert decision["admission"] == "GRANTED"
            for operation in ("feature-write", "finalization"):
                assert (
                    guard_write(repo.repo, **arguments, operation=operation)["decision"]["code"]
                    != "WRITE_ALLOWED"
                )
                assert (
                    guard_write(repo.repo, **arguments, operation=operation, holder=holder)[
                        "decision"
                    ]["code"]
                    == "WRITE_ALLOWED"
                )
            writer_lease.release_writer_lease(repo.repo, target=path, holder=holder)
        else:
            assert writer_lease.writer_requirement(role) == "NOT_APPLICABLE"
            assert (decision["admission"], decision["code"]) == (
                "NOT_APPLICABLE",
                "DEDICATED_ROLE_GATE",
            )
            assert not MetadataStore(repo.repo / ".git").load().writer_leases
            operation = (
                "integration-write"
                if role == "integration"
                else ("recovery" if role in {"recovery", "preservation"} else "finalization")
            )
            assert (
                guard_write(repo.repo, **arguments, operation=operation)["decision"]["code"]
                == "WRITE_ALLOWED"
            )
            arguments["owner"] = "wrong"
            assert (
                guard_write(repo.repo, **arguments, operation=operation)["decision"]["code"]
                == "BLOCK_OWNER_MISMATCH"
            )


def test_unknown_role_is_never_not_applicable():
    with pytest.raises(ControllerError, match="no writer applicability"):
        writer_lease.writer_requirement("future-role")


@pytest.mark.parametrize(
    "role,state,operation",
    [
        ("integration", "INTEGRATING", "finalization"),
        ("publication", "INTEGRATING", "finalization"),
        ("recovery", "QUARANTINED", "recovery"),
        ("preservation", "PRESERVED", "recovery"),
    ],
)
def test_dedicated_lifecycle_entries_do_not_need_ordinary_writer_entitlement(
    role, state, operation
):
    with GitRepository(linked=1, policy_text=POLICY_V6) as repo:
        adopt_canonical(repo)
        path = repo.linked[0]
        adopt(
            repo.repo,
            target=path,
            task_key="task",
            authority_key="authority",
            module="role",
            role=role,
            owner="owner",
            lifecycle=state,
            external_bindings=("dedicated-run",),
        )
        decision = writer_session.admission(path, client_id="manual", selector=str(os.getpid()))
        assert decision["admission"] == "NOT_APPLICABLE"
        assert (
            guard_write(
                repo.repo,
                target=path,
                task_key="task",
                authority_key="authority",
                owner="owner",
                operation=operation,
            )["decision"]["code"]
            == "WRITE_ALLOWED"
        )


def test_real_nonrepo_and_malformed_ancestor_are_not_governance(tmp_path):
    path = tmp_path / "ordinary" / "child"
    path.mkdir(parents=True)
    assert writer_session.governed_target(path) is None
    (tmp_path / "ordinary/.git").write_text("not a Git directory\n")
    assert writer_session.admission(path, client_id="manual", selector=str(os.getpid())) == {
        "admission": "NOT_APPLICABLE",
        "code": "OUTSIDE_CONTROLLER_GOVERNANCE",
    }


def test_failed_acquire_without_policy_does_not_mark_an_ordinary_repository():
    with GitRepository(linked=0, policy=False) as repo:
        with pytest.raises(ControllerError) as exc:
            acquire_one(repo)
        assert exc.value.code == "BLOCK_DRIFT"
        assert not (repo.repo / ".git/worktree-controller").exists()
        assert writer_session.admission(repo.repo, client_id="manual", selector=str(os.getpid())) == {
            "admission": "NOT_APPLICABLE",
            "code": "OUTSIDE_CONTROLLER_GOVERNANCE",
        }


def test_existing_lock_without_identity_remains_fail_closed():
    with GitRepository(linked=0, policy=False) as repo:
        lock = repo.repo / ".git/worktree-controller/v1/repo.lock"
        lock.parent.mkdir(parents=True)
        lock.touch()
        decision = writer_session.admission(repo.repo, client_id="manual", selector=str(os.getpid()))
        assert (decision["admission"], decision["code"]) == ("REFUSED", "INVALID_METADATA")
        assert lock.exists()


def test_real_valid_nested_repo_under_broken_ancestor(tmp_path):
    (tmp_path / ".git").write_text("gitdir: missing\n")
    path = tmp_path / "nested"
    git(tmp_path, "init", "-b", "main", str(path))
    git(
        path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "initial",
    )
    assert writer_session.governed_target(path) is None


@pytest.mark.parametrize("evidence", ["policy", "metadata", "both"])
def test_real_malformed_governed_repo_never_disappears(evidence):
    with GitRepository(linked=0, policy=evidence != "metadata", policy_text=POLICY_V6) as repo:
        if evidence != "policy":
            adopt_canonical(repo)
        (repo.repo / ".git/HEAD").write_text("broken HEAD\n")
        result = writer_session.admission(repo.repo, client_id="manual", selector=str(os.getpid()))
        assert (result["admission"], result["code"]) == ("REFUSED", "GIT_ERROR")


def test_policy_present_without_git_is_not_a_clean_nonrepo(tmp_path):
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents/worktree-policy.toml").write_text(POLICY_V6)
    assert (
        writer_session.admission(tmp_path, client_id="manual", selector=str(os.getpid()))[
            "admission"
        ]
        == "REFUSED"
    )


def test_metadata_only_linked_repo_with_broken_head_is_refused():
    with GitRepository(linked=1, policy=False) as repo:
        adopt_canonical(repo)
        path = repo.linked[0]
        admin = Path((path / ".git").read_text().strip().removeprefix("gitdir: "))
        (admin / "HEAD").write_text("broken HEAD\n")
        assert (
            writer_session.admission(path, client_id="manual", selector=str(os.getpid()))[
                "admission"
            ]
            == "REFUSED"
        )


def test_empty_unrelated_marker_inside_ungoverned_repo_is_not_an_authority():
    with GitRepository(linked=0, policy=False) as repo:
        child = repo.repo / "ordinary"
        (child / ".git").mkdir(parents=True)
        assert writer_session.governed_target(child) is None
        adopt_canonical(repo)
        assert (
            writer_session.admission(child, client_id="manual", selector=str(os.getpid()))[
                "admission"
            ]
            == "REFUSED"
        )
