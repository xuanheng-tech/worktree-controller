from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_controlled import acquire_one, adopt_canonical, handoff_one
from tests.test_shadow import POLICY_V2, GitRepository, git
from worktree_controller.lifecycle import enqueue_integration, reopen, review
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore


@pytest.fixture
def omitted_scope():
    repository = GitRepository(linked=0, policy_text=POLICY_V2)
    try:
        adopt_canonical(repository)
        target, payload = acquire_one(repository, scope_paths=("README.md",))
        (target / "README.md").write_text("task contract\n")
        (target / "uv.lock").write_text("version = 1\n")
        git(target, "add", "README.md", "uv.lock")
        git(target, "commit", "-m", "deliver task with omitted lock scope")
        yield repository, target, payload["decision"]["allocation_id"]
    finally:
        repository.close()


def amend(repository, target, **overrides):
    arguments = dict(
        target=target,
        expected_record_version=1,
        owner="module-owner",
        add_scope_paths=("uv.lock",),
        expected_head=git(target, "rev-parse", "HEAD"),
        scope_reason="lockfile is a necessary product of the recorded version update",
    )
    arguments.update(overrides)
    return review(repository.repo, **arguments)


def test_scope_review_preserves_identity_and_unblocks_handoff(omitted_scope):
    repository, target, allocation = omitted_scope
    store = MetadataStore(repository.repo / ".git")
    before = store.load().records[allocation]
    with pytest.raises(ControllerError, match="outside"):
        handoff_one(repository, target)
    assert amend(repository, target)["decision"]["code"] == "SCOPE_AMENDED"
    after = store.load().records[allocation]
    assert after.allocation_id == before.allocation_id
    assert after.base_commit == before.base_commit
    assert after.owner == before.owner
    assert after.history[:-1] == before.history
    assert after.scope_paths == ("README.md", "uv.lock")
    event = after.history[-1]
    assert event["event"] == "SCOPE_AMENDED"
    assert event["before_scope"] == ["README.md"]
    assert event["after_scope"] == ["README.md", "uv.lock"]
    assert event["head"] == git(target, "rev-parse", "HEAD")
    assert event["reason"]
    assert handoff_one(repository, target, expected=2)["decision"]["code"] == "HANDOFF_READY"
    with pytest.raises(ControllerError) as error:
        amend(repository, target, expected_record_version=3, add_scope_paths=("anything",))
    assert error.value.code == "INVALID_LIFECYCLE"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"owner": "another-owner"}, "OWNER_MISMATCH"),
        ({"expected_record_version": 2}, "CAS_MISMATCH"),
        ({"expected_head": "0" * 40}, "HEAD_MISMATCH"),
        ({"expected_head": None}, "INVALID_SCOPE_AMENDMENT"),
        ({"scope_reason": " "}, "INVALID_SCOPE_AMENDMENT"),
        ({"new_owner": "another-owner"}, "INVALID_SCOPE_AMENDMENT"),
        ({"add_scope_paths": ("README.md",)}, "SCOPE_MISMATCH"),
        ({"add_scope_paths": ("unknown",)}, "SCOPE_MISMATCH"),
        ({"add_scope_paths": ("../escape",)}, "INVALID_SCOPE"),
        ({"add_scope_paths": ()}, "INVALID_SCOPE_AMENDMENT"),
    ],
)
def test_scope_review_failures_leave_metadata_unchanged(omitted_scope, overrides, code):
    repository, target, allocation = omitted_scope
    store = MetadataStore(repository.repo / ".git")
    before = store.load().records[allocation]
    with pytest.raises(ControllerError) as error:
        amend(repository, target, **overrides)
    assert error.value.code == code
    assert store.load().records[allocation] == before


def test_scope_review_rejects_dirty_result(omitted_scope):
    repository, target, allocation = omitted_scope
    (target / "uv.lock").write_text("new unreviewed work\n")
    with pytest.raises(ControllerError) as error:
        amend(repository, target)
    assert error.value.code == "DIRTY_WORKTREE"
    assert MetadataStore(repository.repo / ".git").load().records[allocation].record_version == 1


def test_scope_review_cli(omitted_scope):
    repository, target, _allocation = omitted_scope
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "worktree_controller.cli",
            "review",
            "--json",
            "--repo",
            str(repository.repo),
            "--path",
            str(target),
            "--owner",
            "module-owner",
            "--expected-record-version",
            "1",
            "--add-scope",
            "uv.lock",
            "--expected-head",
            git(target, "rev-parse", "HEAD"),
            "--scope-reason",
            "reviewed lockfile omission",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["decision"]["code"] == "SCOPE_AMENDED"


def test_scope_review_cannot_expand_frozen_integration_source(omitted_scope):
    repository, target, allocation = omitted_scope
    amend(repository, target)
    handoff_one(repository, target, expected=2)
    enqueue_integration(
        repository.repo,
        task_key="delivery",
        authority_key="delivery",
        module="module",
        owner="integration-owner",
        source_allocations=(allocation,),
    )
    reopen(repository.repo, target=target, expected_record_version=3, owner="module-owner")
    (target / "extra.txt").write_text("later result\n")
    git(target, "add", "extra.txt")
    git(target, "commit", "-m", "continue task")
    with pytest.raises(ControllerError) as error:
        amend(repository, target, expected_record_version=4, add_scope_paths=("extra.txt",))
    assert error.value.code == "SCOPE_FROZEN"


def test_scope_review_rejects_canonical(omitted_scope):
    repository, _target, _allocation = omitted_scope
    with pytest.raises(ControllerError) as error:
        amend(repository, repository.repo, owner="control-owner")
    assert error.value.code == "INVALID_ROLE"
