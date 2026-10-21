from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import pytest

from worktree_controller.controller import adopt, doctor, plan, status
from worktree_controller.git_backend import git_state_fingerprint, inventory
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore

POLICY = """\
schema_version = 1

[limits]
normal_limit = 5
hard_limit = 6

[canonical]
protected = true
allowed_roles = ["canonical", "control-plane", "integration", "publication", "verification"]

[slots]
integration = 1
feature_or_audit = 3
emergency = 1

[review]
stale_warning_days = 7

[authority]
require_adoption = true
require_task_key = true
require_authority_key = true
unique_active_feature_authority = true
"""

POLICY_V2 = """\
schema_version = 2

[limits]
normal_limit = 6
hard_limit = 8

[canonical]
protected = true
allowed_roles = ["canonical", "control-plane", "integration", "publication", "verification"]

[slots]
integration_candidate_limit = 2
feature_or_audit = 3
emergency = 1
recovery_reserve = 1

[publication]
main_publish_limit = 1
remote = "origin"
branch = "main"

[review]
stale_warning_days = 7

[authority]
require_adoption = true
require_task_key = true
require_authority_key = true
unique_active_feature_authority = true
"""


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class GitRepository:
    def __init__(self, linked: int = 1, *, policy: bool = True, policy_text: str = POLICY) -> None:
        self.temporary = TemporaryDirectory(prefix="worktree-controller-test-")
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        git(self.root, "init", "-b", "main", str(self.repo))
        git(self.repo, "config", "user.name", "Controller Test")
        git(self.repo, "config", "user.email", "controller@example.invalid")
        if policy:
            policy_path = self.repo / ".agents" / "worktree-policy.toml"
            policy_path.parent.mkdir(parents=True)
            policy_path.write_text(policy_text, encoding="utf-8")
            git(self.repo, "add", ".agents/worktree-policy.toml")
            git(self.repo, "commit", "-m", "add test policy")
        else:
            git(self.repo, "commit", "--allow-empty", "-m", "initial")
        self.linked: list[Path] = []
        for index in range(linked):
            path = self.root / f"linked-{index + 1}"
            git(
                self.repo,
                "worktree",
                "add",
                "-b",
                f"feature/{index + 1}",
                str(path),
                "HEAD",
            )
            self.linked.append(path)

    def close(self) -> None:
        self.temporary.cleanup()

    def adopt_all(self) -> None:
        adopt(
            self.repo,
            target=self.repo,
            task_key="repository-control",
            authority_key="repository-control",
            module="repository",
            role="control-plane",
            owner="unassigned",
            lifecycle="ACTIVE",
        )
        for index, path in enumerate(self.linked, start=1):
            adopt(
                self.repo,
                target=path,
                task_key=f"task-{index}",
                authority_key=f"module-{index}",
                module=f"module-{index}",
                role="feature",
                owner="unassigned",
                lifecycle="ACTIVE",
            )

    def __enter__(self) -> GitRepository:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def test_status_parses_canonical_and_linked_worktrees() -> None:
    with GitRepository(linked=1) as repository:
        payload = status(repository.repo)
        assert payload["repository"]["worktree_count"] == 2
        assert payload["worktrees"][0]["canonical"] is True
        assert payload["worktrees"][0]["branch"] == "main"
        assert payload["worktrees"][1]["kind"] == "secondary"
        assert payload["worktrees"][1]["branch"] == "feature/1"
        assert all(item["clean"] for item in payload["worktrees"])


def test_json_contract_is_stable_and_deterministic() -> None:
    with GitRepository(linked=1) as repository:
        first = status(repository.repo)
        second = status(repository.repo)
        assert set(first) == {
            "tool_version",
            "schema_version",
            "command",
            "repository",
            "decision",
            "worktrees",
            "warnings",
            "errors",
            "mount_diagnostics",
            "environment_diagnosis",
            "orphan_observations",
        }
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


@pytest.mark.parametrize("count", [3, 4])
def test_normal_budget_allows_counts_below_five(count: int) -> None:
    with GitRepository(linked=count - 1) as repository:
        repository.adopt_all()
        payload = plan(
            repository.repo,
            task_key="new-task",
            authority_key="new-authority",
            module="new-module",
            role="feature",
        )
        assert payload["decision"]["code"] == "CREATE_ALLOWED"


def test_legacy_counts_do_not_require_emergency_or_refuse_seventh() -> None:
    with GitRepository(linked=4) as repository:
        repository.adopt_all()
        ordinary = plan(
            repository.repo,
            task_key="ordinary",
            authority_key="ordinary",
            module="module",
            role="feature",
        )
        emergency = plan(
            repository.repo,
            task_key="urgent",
            authority_key="urgent",
            module="module",
            role="emergency",
        )
        assert ordinary["decision"]["code"] == "CREATE_ALLOWED"
        assert emergency["decision"]["code"] == "CREATE_EMERGENCY_ALLOWED"

    with GitRepository(linked=5) as repository:
        repository.adopt_all()
        seventh = plan(
            repository.repo,
            task_key="seventh",
            authority_key="seventh",
            module="module",
            role="emergency",
        )
        assert seventh["decision"]["code"] == "CREATE_EMERGENCY_ALLOWED"


def test_v2_capacity_separates_candidates_features_publication_and_recovery() -> None:
    with GitRepository(linked=5, policy_text=POLICY_V2) as repository:
        adopt(
            repository.repo,
            target=repository.repo,
            task_key="repository-control",
            authority_key="repository-control",
            module="repository",
            role="control-plane",
            owner="owner",
            lifecycle="ACTIVE",
        )
        roles = ["feature", "feature", "feature", "integration", "integration"]
        for index, (path, role) in enumerate(zip(repository.linked, roles, strict=True), start=1):
            adopt(
                repository.repo,
                target=path,
                task_key=f"task-{index}",
                authority_key=f"authority-{index}",
                module="module",
                role=role,
                owner="owner",
                lifecycle="ACTIVE",
            )
        feature = plan(
            repository.repo,
            task_key="feature-next",
            authority_key="feature-next",
            module="module",
            role="feature",
        )
        integration = plan(
            repository.repo,
            task_key="integration-next",
            authority_key="integration-next",
            module="module",
            role="integration",
        )
        recovery = plan(
            repository.repo,
            task_key="recovery",
            authority_key="recovery",
            module="governance",
            role="recovery",
        )
        payload = status(repository.repo)
        assert feature["decision"]["code"] == "CREATE_ALLOWED"
        assert integration["decision"]["code"] == "BLOCK_INTEGRATION_SLOT"
        assert recovery["decision"]["code"] == "CREATE_RECOVERY_ALLOWED"
        assert payload["repository"]["integration_candidate_limit"] == 2
        assert payload["repository"]["main_publish_limit"] == 1
        assert payload["repository"]["recovery_reserve"] == 1


def test_recovery_planning_uses_resources_and_publication_remains_serialized() -> None:
    with GitRepository(linked=0) as repository:
        repository.adopt_all()
        decision = plan(
            repository.repo,
            task_key="recovery",
            authority_key="recovery",
            module="governance",
            role="recovery",
        )
        assert decision["decision"]["code"] == "CREATE_RECOVERY_ALLOWED"

    with GitRepository(
        linked=0,
        policy_text=POLICY_V2.replace("main_publish_limit = 1", "main_publish_limit = 2"),
    ) as repository:
        with pytest.raises(ControllerError) as invalid:
            status(repository.repo)
        assert invalid.value.code == "INVALID_POLICY"


def test_feature_targeting_canonical_is_blocked() -> None:
    with GitRepository(linked=1) as repository:
        repository.adopt_all()
        payload = plan(
            repository.repo,
            task_key="feature-task",
            authority_key="feature-authority",
            module="module",
            role="feature",
            target=repository.repo,
        )
        assert payload["decision"]["code"] == "BLOCK_CANONICAL"


def test_duplicate_task_reuses_and_duplicate_authority_blocks() -> None:
    with GitRepository(linked=1) as repository:
        repository.adopt_all()
        reuse = plan(
            repository.repo,
            task_key="task-1",
            authority_key="module-1",
            module="module-1",
            role="feature",
        )
        duplicate = plan(
            repository.repo,
            task_key="task-new",
            authority_key="module-1",
            module="module-1",
            role="feature",
        )
        assert reuse["decision"]["code"] == "REUSE"
        assert duplicate["decision"]["code"] == "BLOCK_DUPLICATE_AUTHORITY"


def test_clean_active_worktree_is_not_release_candidate() -> None:
    with GitRepository(linked=1) as repository:
        repository.adopt_all()
        linked = status(repository.repo)["worktrees"][1]
        assert linked["clean"] is True
        assert linked["lifecycle"] == "ACTIVE"
        assert linked["release_eligible"] is False


def test_adopt_is_idempotent_and_rejects_unregistered_path() -> None:
    with GitRepository(linked=1) as repository:
        arguments = {
            "target": repository.linked[0],
            "task_key": "task-1",
            "authority_key": "module-1",
            "module": "module-1",
            "role": "feature",
            "owner": "unassigned",
            "lifecycle": "ACTIVE",
        }
        first = adopt(repository.repo, **arguments)
        second = adopt(repository.repo, **arguments)
        assert first["decision"]["code"] == "ADOPTED"
        assert second["decision"]["code"] == "ALREADY_ADOPTED"
        assert first["decision"]["allocation_id"] == second["decision"]["allocation_id"]
        with pytest.raises(ControllerError, match="not registered"):
            adopt(
                repository.repo,
                target=repository.root,
                **{k: v for k, v in arguments.items() if k != "target"},
            )


def test_atomic_json_preserves_previous_value_on_replace_failure() -> None:
    with GitRepository(linked=0) as repository:
        store = MetadataStore(inventory(repository.repo).common_dir)
        with store.lock(exclusive=True, create=True):
            store.initialize_repository()
            before = store.repo_file.read_bytes()
            with (
                mock.patch("worktree_controller.store.os.replace", side_effect=OSError("boom")),
                pytest.raises(OSError, match="boom"),
            ):
                store._atomic_json(store.repo_file, {"invalid": True})
            assert store.repo_file.read_bytes() == before
            assert not list(store.root.glob(".*.tmp"))


def _run_adopt_cli(repo: Path, linked: Path) -> subprocess.CompletedProcess[str]:
    source = Path(__file__).resolve().parents[1] / "src"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "worktree_controller.cli",
            "adopt",
            "--repo",
            str(repo),
            "--path",
            str(linked),
            "--task-key",
            "concurrent",
            "--authority-key",
            "concurrent",
            "--module",
            "module",
            "--role",
            "feature",
            "--owner",
            "unassigned",
            "--lifecycle",
            "ACTIVE",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(source)},
    )


def test_concurrent_adopt_is_serialized_and_consistent() -> None:
    with GitRepository(linked=1) as repository:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda _: _run_adopt_cli(repository.repo, repository.linked[0]), range(2)
                )
            )
        assert [result.returncode for result in results] == [0, 0]
        decisions = {json.loads(result.stdout)["decision"]["code"] for result in results}
        assert decisions == {"ADOPTED", "ALREADY_ADOPTED"}
        snapshot = MetadataStore(inventory(repository.repo).common_dir).load()
        assert len(snapshot.records) == 1
        assert len(snapshot.bindings) == 1


def test_status_plan_and_adopt_do_not_change_git_state() -> None:
    with GitRepository(linked=1) as repository:
        before = git_state_fingerprint(inventory(repository.repo))
        status(repository.repo)
        plan(
            repository.repo,
            task_key="task",
            authority_key="authority",
            module="module",
            role="feature",
        )
        adopt(
            repository.repo,
            target=repository.linked[0],
            task_key="task",
            authority_key="authority",
            module="module",
            role="feature",
            owner="unassigned",
            lifecycle="ACTIVE",
        )
        after = git_state_fingerprint(inventory(repository.repo))
        assert after == before


def test_doctor_reports_missing_policy_and_unmanaged_worktree() -> None:
    with GitRepository(linked=0, policy=False) as repository:
        payload = doctor(repository.repo)
        codes = {warning["code"] for warning in payload["warnings"]}
        assert payload["decision"]["code"] == "FAIL"
        assert {"MISSING_POLICY", "UNMANAGED_WORKTREE"} <= codes
