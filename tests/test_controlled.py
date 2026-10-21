from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest

from tests.test_shadow import POLICY_V2, GitRepository, git
from worktree_controller.controller import adopt, doctor, guard_write, plan, status
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import (
    abort_publication_lease,
    acquire,
    acquire_publication_lease,
    adopt_recovery,
    cleanup_disposable,
    complete_publication_lease,
    externalize,
    handoff,
    quarantine,
    register_disposable,
    release,
    review,
    update_external_binding,
)
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore
from worktree_controller.writer_lease import acquire_writer_lease, describe_holder

SOURCE = Path(__file__).resolve().parents[1] / "src"


def adopt_canonical(repository: GitRepository, *, owner: str = "control-owner") -> None:
    adopt(
        repository.repo,
        target=repository.repo,
        task_key="repository-control",
        authority_key="repository-control",
        module="repository",
        role="control-plane",
        owner=owner,
        lifecycle="ACTIVE",
    )


def acquire_one(
    repository: GitRepository,
    *,
    task: str = "new-task",
    authority: str = "new-authority",
    role: str = "feature",
    owner: str = "module-owner",
    scope_paths: tuple[str, ...] = (),
    source_allocations: tuple[str, ...] = (),
    base_ref: str = "HEAD",
    external_bindings: tuple[str, ...] = (),
) -> tuple[Path, dict[str, object]]:
    target = repository.root / task
    payload = acquire(
        repository.repo,
        task_key=task,
        authority_key=authority,
        module="module",
        role=role,
        owner=owner,
        branch=f"feature/{task}",
        base_ref=base_ref,
        target=target,
        scope_paths=scope_paths,
        source_allocations=source_allocations,
        external_bindings=external_bindings,
    )
    return target, payload


def handoff_one(
    repository: GitRepository,
    target: Path,
    *,
    kind: str = "integration-ready",
    expected: int = 1,
    scope_paths: tuple[str, ...] = (),
    mainline_commit: str | None = None,
) -> dict[str, object]:
    return handoff(
        repository.repo,
        target=target,
        expected_record_version=expected,
        kind=kind,
        commit=git(target, "rev-parse", "HEAD"),
        finalizer_record_id="test-finalizer-record",
        scope_paths=scope_paths,
        mainline_commit=mainline_commit,
    )


def _cli(arguments: list[str], *, crash_at: str | None = None) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "PYTHONPATH": str(SOURCE)}
    if crash_at:
        environment["WORKTREE_CONTROLLER_TEST_CRASH_AT"] = crash_at
    return subprocess.run(
        [sys.executable, "-m", "worktree_controller.cli", *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _acquire_arguments(
    repository: GitRepository,
    *,
    task: str,
    authority: str,
    role: str = "feature",
) -> list[str]:
    return [
        "acquire",
        "--repo",
        str(repository.repo),
        "--path",
        str(repository.root / task),
        "--task-key",
        task,
        "--authority-key",
        authority,
        "--module",
        "module",
        "--role",
        role,
        "--owner",
        "module-owner",
        "--branch",
        f"feature/{task}",
        "--base-ref",
        "HEAD",
        "--json",
    ]


def _release_arguments(
    repository: GitRepository,
    target: Path,
    expected: int = 2,
    *,
    teardown_reproducible_venv: bool = False,
    dry_run: bool = False,
) -> list[str]:
    arguments = [
        "release",
        "--repo",
        str(repository.repo),
        "--path",
        str(target),
        "--expected-record-version",
        str(expected),
        "--owner-release",
        "module-owner",
        "--json",
    ]
    if teardown_reproducible_venv:
        arguments.append("--teardown-reproducible-venv")
    if dry_run:
        arguments.append("--dry-run")
    return arguments


def _prepare_integration_candidate(
    repository: GitRepository, *, candidate_task: str = "calendar-candidate"
) -> tuple[Path, str, Path, str]:
    adopt_canonical(repository)
    source, source_payload = acquire_one(
        repository,
        task="calendar-source",
        authority="calendar-source",
        scope_paths=("calendar",),
    )
    calendar = source / "calendar"
    calendar.mkdir()
    (calendar / "client_id.py").write_text("PROVIDER = 'official'\n", encoding="utf-8")
    git(source, "add", "calendar/client_id.py")
    git(source, "commit", "-m", "add official calendar client_id")
    handoff_one(repository, source)
    source_allocation = str(source_payload["decision"]["allocation_id"])
    candidate = repository.root / candidate_task
    candidate_payload = acquire(
        repository.repo,
        task_key=candidate_task,
        authority_key=candidate_task,
        module="integration",
        role="integration",
        owner="module-owner",
        branch=f"integration/{candidate_task}",
        base_ref="main",
        target=candidate,
        source_allocations=(source_allocation,),
    )
    git(candidate, "merge", "--ff-only", "feature/calendar-source")
    candidate_allocation = str(candidate_payload["decision"]["allocation_id"])
    return source, source_allocation, candidate, candidate_allocation


def test_write_guard_blocks_feature_work_in_integration_and_allows_recovery() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        _source, _source_allocation, candidate, _candidate_allocation = (
            _prepare_integration_candidate(repository)
        )

        blocked = guard_write(
            repository.repo,
            target=candidate,
            task_key="calendar-candidate",
            authority_key="calendar-candidate",
            owner="module-owner",
            operation="feature-write",
        )
        assert blocked["decision"]["code"] == "BLOCK_INTEGRATION_FEATURE_WRITE"

        allowed = guard_write(
            repository.repo,
            target=candidate,
            task_key="unrelated-recovery-task",
            authority_key="repository-control",
            owner="module-owner",
            operation="recovery",
        )
        assert allowed["decision"]["code"] == "WRITE_ALLOWED"


def test_write_guard_allows_legal_integration_and_normal_feature_work() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        _source, _source_allocation, candidate, _candidate_allocation = (
            _prepare_integration_candidate(repository)
        )
        integration = guard_write(
            repository.repo,
            target=candidate,
            task_key="calendar-candidate",
            authority_key="calendar-candidate",
            owner="module-owner",
            operation="integration-write",
        )
        assert integration["decision"]["code"] == "WRITE_ALLOWED"

        feature, _payload = acquire_one(
            repository,
            task="ordinary-feature",
            authority="ordinary-feature",
        )
        holder = describe_holder(os.getpid(), client_id="manual")
        acquire_writer_lease(
            repository.repo, target=feature, client_id="manual", holder_pid=holder.pid
        )
        ordinary = guard_write(
            repository.repo,
            target=feature,
            holder=holder,
            task_key="ordinary-feature",
            authority_key="ordinary-feature",
            owner="module-owner",
            operation="feature-write",
        )
        assert ordinary["decision"]["code"] == "WRITE_ALLOWED"


def test_write_guard_blocks_task_mismatch_and_handoff_ready_head_drift() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        _source, _source_allocation, candidate, _candidate_allocation = (
            _prepare_integration_candidate(repository)
        )
        mismatch = guard_write(
            repository.repo,
            target=candidate,
            task_key="unrelated-feature",
            authority_key="calendar-candidate",
            owner="module-owner",
            operation="integration-write",
        )
        assert mismatch["decision"]["code"] == "BLOCK_TASK_IDENTITY_MISMATCH"

        handoff_one(repository, candidate)
        frozen_feature = guard_write(
            repository.repo,
            target=candidate,
            task_key="calendar-candidate",
            authority_key="calendar-candidate",
            owner="module-owner",
            operation="feature-write",
        )
        assert frozen_feature["decision"]["code"] == "BLOCK_INTEGRATION_FEATURE_WRITE"

        (candidate / "calendar" / "drift.py").write_text("DRIFT = True\n", encoding="utf-8")
        git(candidate, "add", "calendar/drift.py")
        git(candidate, "commit", "-m", "drift after handoff")
        drift = guard_write(
            repository.repo,
            target=candidate,
            task_key="calendar-candidate",
            authority_key="calendar-candidate",
            owner="module-owner",
            operation="integration-write",
        )
        assert drift["decision"]["code"] == "BLOCK_RECOVERY_REQUIRED"


def test_write_guard_cli_uses_blocking_exit_status() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        _source, _source_allocation, candidate, _candidate_allocation = (
            _prepare_integration_candidate(repository)
        )
        result = _cli(
            [
                "write-guard",
                "--repo",
                str(repository.repo),
                "--path",
                str(candidate),
                "--task-key",
                "calendar-candidate",
                "--authority-key",
                "calendar-candidate",
                "--owner",
                "module-owner",
                "--operation",
                "feature-write",
                "--json",
            ]
        )
        assert result.returncode == 1
        assert json.loads(result.stdout)["decision"]["code"] == "BLOCK_INTEGRATION_FEATURE_WRITE"


def test_integration_acquire_resolves_released_source_by_handoff_commit_via_cli() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        adopt_canonical(repository)
        source, source_payload = acquire_one(
            repository,
            task="calendar-source",
            authority="calendar-source",
            scope_paths=("calendar",),
        )
        calendar = source / "calendar"
        calendar.mkdir()
        (calendar / "client_id.py").write_text("PROVIDER = 'official'\n", encoding="utf-8")
        git(source, "add", "calendar/client_id.py")
        git(source, "commit", "-m", "add official calendar client_id")
        handoff_one(repository, source)
        handoff_commit = git(source, "rev-parse", "HEAD")
        source_allocation = str(source_payload["decision"]["allocation_id"])
        release(
            repository.repo,
            target=source,
            expected_record_version=2,
            owner_release="module-owner",
        )

        target = repository.root / "handoff-candidate"
        result = _cli(
            [
                "acquire",
                "--repo",
                str(repository.repo),
                "--path",
                str(target),
                "--task-key",
                "handoff-candidate",
                "--authority-key",
                "handoff-candidate",
                "--module",
                "integration",
                "--role",
                "integration",
                "--owner",
                "module-owner",
                "--branch",
                "integration/handoff-candidate",
                "--base-ref",
                "main",
                "--source-handoff",
                handoff_commit,
                "--json",
            ]
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        candidate = next(item for item in payload["worktrees"] if item["path"] == str(target))
        assert candidate["source_allocations"] == [source_allocation]
        assert candidate["scope_paths"] == ["calendar"]


def _configure_remote(repository: GitRepository) -> Path:
    remote = repository.root / "remote.git"
    git(repository.root, "init", "--bare", str(remote))
    git(repository.repo, "remote", "add", "origin", str(remote))
    git(repository.repo, "push", "-u", "origin", "main")
    return remote


def test_acquire_review_handoff_and_release_retains_branch() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, acquired = acquire_one(repository)
        assert acquired["decision"]["code"] == "ACQUIRED"
        assert target.is_dir()
        reviewed = review(
            repository.repo,
            target=target,
            expected_record_version=1,
            owner="module-owner",
            new_owner="handoff-owner",
        )
        assert reviewed["decision"]["record_version"] == 2
        handed = handoff(
            repository.repo,
            target=target,
            expected_record_version=2,
            kind="integration-ready",
            commit=git(target, "rev-parse", "HEAD"),
            finalizer_record_id="test-finalizer-record",
        )
        assert handed["decision"]["code"] == "HANDOFF_READY"
        released = release(
            repository.repo,
            target=target,
            expected_record_version=3,
            owner_release="handoff-owner",
        )
        assert released["decision"]["code"] == "RELEASED"
        assert not target.exists()
        assert git(repository.repo, "show-ref", "--verify", "refs/heads/feature/new-task")
        assert doctor(repository.repo)["decision"]["code"] == "PASS"


def test_concurrent_acquire_can_cross_legacy_hard_limit() -> None:
    with GitRepository(linked=4) as repository:
        repository.adopt_all()
        calls = [
            _acquire_arguments(
                repository,
                task=f"emergency-{index}",
                authority=f"emergency-{index}",
                role="emergency",
            )
            for index in (1, 2)
        ]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(_cli, calls))
        assert sorted(result.returncode for result in results) == [0, 0]
        outcomes = [json.loads(result.stdout)["decision"]["code"] for result in results]
        assert set(outcomes) == {"ACQUIRED"}
        assert status(repository.repo)["repository"]["worktree_count"] == 7


def test_concurrent_same_authority_creates_only_one_worktree() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        calls = [
            _acquire_arguments(repository, task=f"candidate-{index}", authority="shared-authority")
            for index in (1, 2)
        ]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(_cli, calls))
        assert sorted(result.returncode for result in results) == [0, 2]
        failure = next(json.loads(result.stdout) for result in results if result.returncode == 2)
        assert failure["errors"][0]["code"] == "BLOCK_DUPLICATE_AUTHORITY"
        assert status(repository.repo)["repository"]["worktree_count"] == 2


def test_acquire_rejects_existing_or_checked_out_branch() -> None:
    with GitRepository(linked=1) as repository:
        repository.adopt_all()
        with pytest.raises(ControllerError, match="already exists") as error:
            acquire(
                repository.repo,
                task_key="duplicate-branch",
                authority_key="new-authority",
                module="module",
                role="feature",
                owner="owner",
                branch="feature/1",
                base_ref="HEAD",
                target=repository.root / "duplicate",
            )
        assert error.value.code == "BRANCH_EXISTS"


def test_policy_v1_preservation_acquire_remains_compatible() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, payload = acquire_one(
            repository,
            task="legacy-preservation",
            authority="legacy-preservation",
            role="preservation",
        )
        assert payload["decision"]["code"] == "ACQUIRED"
        assert target.exists()


def test_clean_active_and_canonical_cannot_release() -> None:
    with GitRepository(linked=1) as repository:
        repository.adopt_all()
        with pytest.raises(ControllerError) as active:
            release(
                repository.repo,
                target=repository.linked[0],
                expected_record_version=1,
                owner_release="unassigned",
            )
        assert active.value.code == "INVALID_LIFECYCLE"
        with pytest.raises(ControllerError) as canonical:
            release(
                repository.repo,
                target=repository.repo,
                expected_record_version=1,
                owner_release="unassigned",
            )
        assert canonical.value.code == "BLOCK_CANONICAL"


@pytest.mark.parametrize("kind", ["integration-ready", "preserved"])
def test_both_explicit_handoff_kinds_can_release(kind: str) -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        handed = handoff_one(repository, target, kind=kind)
        expected_code = "HANDOFF_READY" if kind == "integration-ready" else "PRESERVED"
        assert handed["decision"]["code"] == expected_code
        released = release(
            repository.repo,
            target=target,
            expected_record_version=2,
            owner_release="module-owner",
        )
        assert released["decision"]["code"] == "RELEASED"


def test_dirty_staged_detached_and_head_drift_fail_closed() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        dirty, _ = acquire_one(repository, task="dirty", authority="dirty")
        (dirty / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        with pytest.raises(ControllerError) as dirty_error:
            handoff_one(repository, dirty)
        assert dirty_error.value.code == "DIRTY_WORKTREE"
        git(dirty, "add", "dirty.txt")
        with pytest.raises(ControllerError) as staged_error:
            handoff_one(repository, dirty)
        assert staged_error.value.code == "DIRTY_WORKTREE"

    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        detached, _ = acquire_one(repository, task="detached", authority="detached")
        handoff_one(repository, detached)
        git(detached, "switch", "--detach")
        with pytest.raises(ControllerError) as detached_error:
            release(
                repository.repo,
                target=detached,
                expected_record_version=2,
                owner_release="module-owner",
            )
        assert detached_error.value.code == "DETACHED_WORKTREE"

    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        drifted, _ = acquire_one(repository, task="drifted", authority="drifted")
        handoff_one(repository, drifted)
        git(drifted, "commit", "--allow-empty", "-m", "advance after handoff")
        with pytest.raises(ControllerError) as drift_error:
            release(
                repository.repo,
                target=drifted,
                expected_record_version=2,
                owner_release="module-owner",
            )
        assert drift_error.value.code == "HEAD_MISMATCH"


def _commit_ignore(repository: GitRepository, pattern: str) -> None:
    (repository.repo / ".gitignore").write_text(f"{pattern}\n", encoding="utf-8")
    git(repository.repo, "add", ".gitignore")
    git(repository.repo, "commit", "-m", f"ignore {pattern}")


def _commit_reproducible_project(
    repository: GitRepository, *, extra_ignore: str | None = None
) -> None:
    ignores = ".venv\n"
    if extra_ignore is not None:
        ignores += f"{extra_ignore}\n"
    (repository.repo / ".gitignore").write_text(ignores, encoding="utf-8")
    (repository.repo / "pyproject.toml").write_text(
        '[project]\nname = "release-fixture"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (repository.repo / "uv.lock").write_text(
        'version = 1\nrevision = 3\nrequires-python = ">=3.12"\n', encoding="utf-8"
    )
    git(repository.repo, "add", ".gitignore", "pyproject.toml", "uv.lock")
    git(repository.repo, "commit", "-m", "add reproducible project identity")


def _create_reproducible_venv(target: Path) -> Path:
    venv = target / ".venv"
    binary = venv / "bin"
    binary.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(
        "home = /test\nimplementation = CPython\nversion = 3.12.0\n",
        encoding="utf-8",
    )
    (venv / "CACHEDIR.TAG").write_text(
        "Signature: 8a477f597d28d172789f06886806bc55\n",
        encoding="utf-8",
    )
    (binary / "python").symlink_to(Path(sys.executable).resolve())
    (venv / "payload.bin").write_bytes(b"reproducible fixture")
    site = venv / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (site / "self-editable.pth").write_text(f"{target}\n", encoding="utf-8")
    return venv


def _prepare_teardown_target(
    repository: GitRepository,
    *,
    handoff_ready: bool = True,
    external_bindings: tuple[str, ...] = (),
    extra_ignore: str | None = None,
) -> Path:
    _commit_reproducible_project(repository, extra_ignore=extra_ignore)
    adopt_canonical(repository)
    target, _payload = acquire_one(repository, external_bindings=external_bindings)
    _create_reproducible_venv(target)
    register_disposable(
        repository.repo,
        target=target,
        expected_record_version=1,
        owner="module-owner",
        resource_path=".venv",
        kind="python-venv",
        reproducible=True,
        creator="test-fixture",
        teardown_policy="on-release",
    )
    if handoff_ready:
        handoff_one(repository, target, expected=2)
    return target


def _prepare_recovery_teardown_target(
    repository: GitRepository, *, extra_ignore: str | None = None
) -> tuple[Path, Path, str, str]:
    _commit_reproducible_project(repository, extra_ignore=extra_ignore)
    _source, _source_allocation, candidate, candidate_allocation = _prepare_integration_candidate(
        repository
    )
    _create_reproducible_venv(candidate)
    register_disposable(
        repository.repo,
        target=candidate,
        expected_record_version=1,
        owner="module-owner",
        resource_path=".venv",
        kind="python-venv",
        reproducible=True,
        creator="test-fixture",
        teardown_policy="on-release",
    )
    handoff_one(repository, candidate, expected=2)
    expected_head = git(candidate, "rev-parse", "HEAD")
    (candidate / "calendar" / "drift.py").write_text("DRIFT = True\n", encoding="utf-8")
    git(candidate, "add", "calendar/drift.py")
    git(candidate, "commit", "-m", "externalize integration drift")
    observed_head = git(candidate, "rev-parse", "HEAD")
    quarantine(
        repository.repo,
        target=candidate,
        expected_record_version=3,
        owner="module-owner",
        reason="externalize integration drift",
        expected_commit=expected_head,
    )
    recovery, _recovery_payload = acquire_one(
        repository,
        task="calendar-recovery",
        authority="calendar-recovery",
        role="recovery",
        source_allocations=(candidate_allocation,),
        base_ref=observed_head,
        scope_paths=("calendar",),
    )
    linked = externalize(
        repository.repo,
        source_target=candidate,
        recovery_target=recovery,
        expected_source_record_version=4,
        expected_recovery_record_version=1,
        owner="module-owner",
    )
    return candidate, recovery, observed_head, str(linked["decision"]["recovery_ref"])


def _interrupt_recovery_after_venv_removed(
    repository: GitRepository, target: Path
) -> MetadataStore:
    result = _cli(
        _release_arguments(
            repository,
            target,
            expected=5,
            teardown_reproducible_venv=True,
        ),
        crash_at="release_venv_removed",
    )
    assert result.returncode == 97
    assert target.is_dir()
    assert not (target / ".venv").exists()
    return MetadataStore(inventory(repository.repo).common_dir)


def test_release_teardown_dry_run_then_release_records_receipt_and_retains_branch() -> None:
    with GitRepository(linked=0) as repository:
        target = _prepare_teardown_target(repository)
        branch = "refs/heads/feature/new-task"
        branch_oid = git(repository.repo, "rev-parse", branch)
        dry_run = release(
            repository.repo,
            target=target,
            expected_record_version=3,
            owner_release="module-owner",
            teardown_reproducible_venv=True,
            dry_run=True,
        )
        assert dry_run["decision"]["code"] == "RELEASE_PLAN_READY"
        assert dry_run["decision"]["plan"]["resource_actions"][0]["evidence"]["bytes_before"] > 0
        assert (target / ".venv").is_dir()
        store = MetadataStore(inventory(repository.repo).common_dir)
        assert not store.teardown_receipts_dir.exists()

        released = release(
            repository.repo,
            target=target,
            expected_record_version=3,
            owner_release="module-owner",
            teardown_reproducible_venv=True,
        )
        receipt = released["decision"]["teardown"]
        assert released["decision"]["code"] == "RELEASED"
        assert receipt["result"] == "released"
        assert receipt["worktree_head"] == branch_oid
        assert len(receipt["lock_sha256"]) == 64
        assert store.load_teardown_receipt(receipt["receipt_id"])["result"] == "released"
        assert not target.exists()
        assert git(repository.repo, "rev-parse", branch) == branch_oid
        assert doctor(repository.repo)["decision"]["code"] == "PASS"


def test_release_teardown_rejects_dirty_or_active_worktree() -> None:
    with GitRepository(linked=0) as repository:
        dirty = _prepare_teardown_target(repository)
        (dirty / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        with pytest.raises(ControllerError) as blocked:
            release(
                repository.repo,
                target=dirty,
                expected_record_version=3,
                owner_release="module-owner",
                teardown_reproducible_venv=True,
            )
        assert blocked.value.code == "DIRTY_WORKTREE"
        assert (dirty / ".venv").is_dir()

    with GitRepository(linked=0) as repository:
        active = _prepare_teardown_target(repository, handoff_ready=False)
        with pytest.raises(ControllerError) as blocked:
            release(
                repository.repo,
                target=active,
                expected_record_version=2,
                owner_release="module-owner",
                teardown_reproducible_venv=True,
            )
        assert blocked.value.code == "INVALID_LIFECYCLE"
        assert (active / ".venv").is_dir()


def test_release_teardown_rejects_commit_identity_mismatch() -> None:
    with GitRepository(linked=0) as repository:
        target = _prepare_teardown_target(repository)
        store = MetadataStore(inventory(repository.repo).common_dir)
        with store.lock(exclusive=True):
            snapshot = store._load_unlocked()
            record = next(
                record for record in snapshot.records.values() if record.task_key == "new-task"
            )
            store.replace_record(
                replace(record, finalizer_commit=git(target, "rev-parse", "HEAD^"))
            )
        with pytest.raises(ControllerError) as blocked:
            release(
                repository.repo,
                target=target,
                expected_record_version=3,
                owner_release="module-owner",
                teardown_reproducible_venv=True,
            )
        assert blocked.value.code == "IDENTITY_MISMATCH"
        assert (target / ".venv").is_dir()


def test_release_teardown_rejects_runtime_reference() -> None:
    with GitRepository(linked=0) as repository:
        target = _prepare_teardown_target(repository)
        with (
            (target / ".venv" / "payload.bin").open("rb"),
            pytest.raises(ControllerError) as blocked,
        ):
            release(
                repository.repo,
                target=target,
                expected_record_version=3,
                owner_release="module-owner",
                teardown_reproducible_venv=True,
            )
        assert blocked.value.code == "RUNTIME_REFERENCE"
        assert (target / ".venv").is_dir()


def test_handoff_preserves_active_binding_while_release_remains_blocked() -> None:
    with GitRepository(linked=0) as repository:
        target = _prepare_teardown_target(
            repository,
            handoff_ready=False,
            external_bindings=("runtime:fixture",),
        )
        handoff_one(repository, target, expected=2)
        with pytest.raises(ControllerError) as blocked:
            release(
                repository.repo,
                target=target,
                expected_record_version=3,
                owner_release="module-owner",
                dry_run=True,
            )
        assert blocked.value.code == "EXTERNAL_BINDING"
        cleared = update_external_binding(
            repository.repo,
            target=target,
            expected_record_version=3,
            owner="module-owner",
            binding="runtime:fixture",
            action="remove",
        )
        assert cleared["decision"]["code"] == "BINDING_REMOVED"
        planned = release(
            repository.repo,
            target=target,
            expected_record_version=4,
            owner_release="module-owner",
            dry_run=True,
        )
        assert planned["decision"]["code"] == "RELEASE_PLAN_READY"
        assert (target / ".venv").is_dir()


def test_release_checks_cross_environment_reference() -> None:
    with GitRepository(linked=0) as repository:
        target = _prepare_teardown_target(repository)
        site = repository.repo / ".venv" / "lib" / "python3.12" / "site-packages"
        site.mkdir(parents=True)
        (site / "external-editable.pth").write_text(f"{target}\n", encoding="utf-8")
        with pytest.raises(ControllerError) as blocked:
            release(
                repository.repo,
                target=target,
                expected_record_version=3,
                owner_release="module-owner",
                teardown_reproducible_venv=True,
            )
        assert blocked.value.code == "STALE_EDITABLE_REFERENCE"
        assert (target / ".venv").is_dir()


def test_release_teardown_rejects_symlink_and_nonstandard_venv() -> None:
    with GitRepository(linked=0) as repository:
        _commit_reproducible_project(repository)
        adopt_canonical(repository)
        target, _payload = acquire_one(repository)
        outside = repository.root / "outside-venv"
        outside.mkdir()
        (target / ".venv").symlink_to(outside, target_is_directory=True)
        with pytest.raises(ControllerError) as blocked:
            register_disposable(
                repository.repo,
                target=target,
                expected_record_version=1,
                owner="module-owner",
                resource_path=".venv",
                kind="python-venv",
                reproducible=True,
                creator="test-fixture",
                teardown_policy="on-release",
            )
        assert blocked.value.code == "RESOURCE_IDENTITY_MISMATCH"
        assert outside.is_dir()

    with GitRepository(linked=0) as repository:
        target = _prepare_teardown_target(repository)
        (target / ".venv" / "CACHEDIR.TAG").write_text("not standard\n", encoding="utf-8")
        with pytest.raises(ControllerError) as blocked:
            release(
                repository.repo,
                target=target,
                expected_record_version=3,
                owner_release="module-owner",
                teardown_reproducible_venv=True,
            )
        assert blocked.value.code == "RESOURCE_IDENTITY_MISMATCH"
        assert (target / ".venv").is_dir()


def test_release_teardown_does_not_bypass_other_ignored_content() -> None:
    with GitRepository(linked=0) as repository:
        target = _prepare_teardown_target(repository, extra_ignore="evidence/")
        evidence = target / "evidence"
        evidence.mkdir()
        (evidence / "receipt.json").write_text("{}\n", encoding="utf-8")
        with pytest.raises(ControllerError) as blocked:
            release(
                repository.repo,
                target=target,
                expected_record_version=3,
                owner_release="module-owner",
                teardown_reproducible_venv=True,
            )
        assert blocked.value.code == "IGNORED_CONTENT"
        assert (target / ".venv").is_dir()


@pytest.mark.parametrize(
    ("crash_at", "venv_present"),
    [
        ("release_teardown_prepared", True),
        ("release_venv_removal_started", True),
        ("release_venv_removed", False),
    ],
)
def test_release_teardown_crash_resumes_from_receipt(crash_at: str, venv_present: bool) -> None:
    with GitRepository(linked=0) as repository:
        target = _prepare_teardown_target(repository)
        branch = "refs/heads/feature/new-task"
        branch_oid = git(repository.repo, "rev-parse", branch)
        result = _cli(
            _release_arguments(
                repository,
                target,
                expected=3,
                teardown_reproducible_venv=True,
            ),
            crash_at=crash_at,
        )
        assert result.returncode == 97
        assert target.is_dir()
        assert (target / ".venv").exists() is venv_present
        assert doctor(repository.repo)["decision"]["code"] == "FAIL"

        recovered = release(
            repository.repo,
            target=target,
            expected_record_version=3,
            owner_release="module-owner",
            teardown_reproducible_venv=True,
        )
        assert recovered["decision"]["code"] == "RELEASED"
        assert recovered["decision"]["teardown"]["result"] == "released"
        assert git(repository.repo, "rev-parse", branch) == branch_oid
        assert doctor(repository.repo)["decision"]["code"] == "PASS"


def test_recovery_required_release_resumes_after_venv_removed_and_is_idempotent() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        target, recovery, expected_head, recovery_ref = _prepare_recovery_teardown_target(
            repository
        )
        retained_branch = f"refs/heads/{git(target, 'branch', '--show-current')}"
        store = _interrupt_recovery_after_venv_removed(repository, target)
        transaction_path = next(store.transactions_dir.glob("*.json"))
        journal_before = transaction_path.read_bytes()

        planned = release(
            repository.repo,
            target=target,
            expected_record_version=5,
            owner_release="module-owner",
            teardown_reproducible_venv=True,
            dry_run=True,
        )
        assert planned["decision"]["code"] == "RELEASE_RECOVERY_PLAN_READY"
        assert planned["decision"]["recovery"]["phase"] == "VENV_REMOVED"
        assert planned["decision"]["recovery"]["next_phase"] == "PREPARED"
        assert transaction_path.read_bytes() == journal_before
        assert not (target / ".venv").exists()

        released = release(
            repository.repo,
            target=target,
            expected_record_version=5,
            owner_release="module-owner",
            teardown_reproducible_venv=True,
        )
        assert released["decision"]["code"] == "RELEASED"
        assert released["decision"]["teardown"]["result"] == "released"
        assert not target.exists()
        assert recovery.exists()
        assert git(repository.repo, "rev-parse", retained_branch) == expected_head
        assert git(repository.repo, "rev-parse", recovery_ref) == expected_head
        assert not tuple(store.transactions_dir.glob("*.json"))
        released_record = store.load().records[released["decision"]["allocation_id"]]
        assert released_record.lifecycle == "RELEASED"
        assert released_record.record_version == 6

        repeated = release(
            repository.repo,
            target=target,
            expected_record_version=5,
            owner_release="module-owner",
            teardown_reproducible_venv=True,
        )
        assert repeated["decision"]["code"] == "ALREADY_RELEASED"
        assert (
            repeated["decision"]["release_receipt_id"] == released["decision"]["release_receipt_id"]
        )


@pytest.mark.parametrize(
    ("drift", "expected_code"),
    [("head", "RELEASE_PLAN_STALE"), ("record", "CAS_MISMATCH")],
)
def test_recovery_required_resume_rejects_head_or_record_drift(
    drift: str, expected_code: str
) -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        target, _recovery, _expected_head, _recovery_ref = _prepare_recovery_teardown_target(
            repository
        )
        store = _interrupt_recovery_after_venv_removed(repository, target)
        if drift == "head":
            git(target, "commit", "--allow-empty", "-m", "drift after release interruption")
        else:
            with store.lock(exclusive=True):
                snapshot = store._load_unlocked()
                record = next(
                    record
                    for record in snapshot.records.values()
                    if record.task_key == "calendar-candidate"
                )
                store.replace_record(replace(record, record_version=record.record_version + 1))

        with pytest.raises(ControllerError) as blocked:
            release(
                repository.repo,
                target=target,
                expected_record_version=5,
                owner_release="module-owner",
                teardown_reproducible_venv=True,
            )
        assert blocked.value.code == expected_code
        assert target.exists()


@pytest.mark.parametrize(
    ("drift", "expected_code"),
    [("dirty", "DIRTY_WORKTREE"), ("external", "EXTERNAL_BINDING")],
)
def test_recovery_required_resume_rechecks_worktree_and_external_binding(
    drift: str, expected_code: str
) -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        target, _recovery, _expected_head, _recovery_ref = _prepare_recovery_teardown_target(
            repository
        )
        store = _interrupt_recovery_after_venv_removed(repository, target)
        if drift == "dirty":
            (target / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        else:
            with store.lock(exclusive=True):
                snapshot = store._load_unlocked()
                record = next(
                    record
                    for record in snapshot.records.values()
                    if record.task_key == "calendar-candidate"
                )
                store.replace_record(replace(record, external_bindings=("runtime:test",)))

        with pytest.raises(ControllerError) as blocked:
            release(
                repository.repo,
                target=target,
                expected_record_version=5,
                owner_release="module-owner",
                teardown_reproducible_venv=True,
            )
        assert blocked.value.code == expected_code
        assert target.exists()


def test_recovery_required_resume_rechecks_runtime_and_editable_references() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        target, _recovery, _expected_head, _recovery_ref = _prepare_recovery_teardown_target(
            repository
        )
        _interrupt_recovery_after_venv_removed(repository, target)
        with (
            (target / "calendar" / "drift.py").open("rb"),
            pytest.raises(ControllerError) as runtime_blocked,
        ):
            release(
                repository.repo,
                target=target,
                expected_record_version=5,
                owner_release="module-owner",
                teardown_reproducible_venv=True,
            )
        assert runtime_blocked.value.code == "RUNTIME_REFERENCE"

    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        target, _recovery, _expected_head, _recovery_ref = _prepare_recovery_teardown_target(
            repository
        )
        _interrupt_recovery_after_venv_removed(repository, target)
        site = repository.repo / ".venv" / "lib" / "python3.12" / "site-packages"
        site.mkdir(parents=True)
        (site / "external-editable.pth").write_text(f"{target}\n", encoding="utf-8")
        with pytest.raises(ControllerError) as editable_blocked:
            release(
                repository.repo,
                target=target,
                expected_record_version=5,
                owner_release="module-owner",
                teardown_reproducible_venv=True,
            )
        assert editable_blocked.value.code == "STALE_EDITABLE_REFERENCE"


def test_recovery_required_resume_rechecks_unknown_ignored_content() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        target, _recovery, _expected_head, _recovery_ref = _prepare_recovery_teardown_target(
            repository, extra_ignore="evidence/"
        )
        _interrupt_recovery_after_venv_removed(repository, target)
        evidence = target / "evidence"
        evidence.mkdir()
        (evidence / "receipt.json").write_text("{}\n", encoding="utf-8")
        with pytest.raises(ControllerError) as blocked:
            release(
                repository.repo,
                target=target,
                expected_record_version=5,
                owner_release="module-owner",
                teardown_reproducible_venv=True,
            )
        assert blocked.value.code == "IGNORED_CONTENT"
        assert target.exists()


@pytest.mark.parametrize(
    ("journal_drift", "expected_code"),
    [("missing", "DISPOSABLE_RESOURCE_DRIFT"), ("phase", "TEARDOWN_RECOVERY_MISMATCH")],
)
def test_recovery_required_resume_rejects_missing_or_tampered_journal(
    journal_drift: str, expected_code: str
) -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        target, _recovery, _expected_head, _recovery_ref = _prepare_recovery_teardown_target(
            repository
        )
        store = _interrupt_recovery_after_venv_removed(repository, target)
        transaction_path = next(store.transactions_dir.glob("*.json"))
        if journal_drift == "missing":
            transaction_path.unlink()
        else:
            journal = json.loads(transaction_path.read_text(encoding="utf-8"))
            journal["phase"] = "TEARDOWN_PREPARED"
            transaction_path.write_text(
                json.dumps(journal, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

        with pytest.raises(ControllerError) as blocked:
            release(
                repository.repo,
                target=target,
                expected_record_version=5,
                owner_release="module-owner",
                teardown_reproducible_venv=True,
            )
        assert blocked.value.code == expected_code
        assert target.exists()


def test_recovery_required_missing_venv_without_transaction_remains_blocked() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        target, _recovery, _expected_head, _recovery_ref = _prepare_recovery_teardown_target(
            repository
        )
        shutil.rmtree(target / ".venv")
        with pytest.raises(ControllerError) as blocked:
            release(
                repository.repo,
                target=target,
                expected_record_version=5,
                owner_release="module-owner",
                teardown_reproducible_venv=True,
            )
        assert blocked.value.code == "DISPOSABLE_RESOURCE_DRIFT"
        assert target.exists()


def test_release_teardown_resumes_after_partial_directory_removal() -> None:
    with GitRepository(linked=0) as repository:
        target = _prepare_teardown_target(repository)
        venv = target / ".venv"

        def interrupted_removal(path: Path) -> None:
            assert path == venv
            (path / "payload.bin").unlink()
            raise KeyboardInterrupt

        with (
            mock.patch(
                "worktree_controller.lifecycle.shutil.rmtree",
                side_effect=interrupted_removal,
            ),
            pytest.raises(KeyboardInterrupt),
        ):
            release(
                repository.repo,
                target=target,
                expected_record_version=3,
                owner_release="module-owner",
                teardown_reproducible_venv=True,
            )

        assert venv.is_dir()
        assert not (venv / "payload.bin").exists()
        assert doctor(repository.repo)["decision"]["code"] == "FAIL"

        recovered = release(
            repository.repo,
            target=target,
            expected_record_version=3,
            owner_release="module-owner",
            teardown_reproducible_venv=True,
        )
        assert recovered["decision"]["code"] == "RELEASED"
        assert recovered["decision"]["teardown"]["result"] == "released"
        assert doctor(repository.repo)["decision"]["code"] == "PASS"


def test_known_ignored_cache_is_allowed_but_evidence_is_blocked() -> None:
    with GitRepository(linked=0) as repository:
        _commit_ignore(repository, "__pycache__/")
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        handoff_one(repository, target)
        cache = target / "__pycache__"
        cache.mkdir()
        (cache / "module.pyc").write_bytes(b"cache")
        released = release(
            repository.repo,
            target=target,
            expected_record_version=2,
            owner_release="module-owner",
        )
        assert released["decision"]["code"] == "RELEASED"

    with GitRepository(linked=0) as repository:
        _commit_ignore(repository, "evidence/")
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        handoff_one(repository, target)
        evidence = target / "evidence"
        evidence.mkdir()
        (evidence / "result.json").write_text("{}\n", encoding="utf-8")
        with pytest.raises(ControllerError) as blocked:
            release(
                repository.repo,
                target=target,
                expected_record_version=2,
                owner_release="module-owner",
            )
        assert blocked.value.code == "IGNORED_CONTENT"
        assert target.exists()


def test_stale_editable_mapping_and_external_binding_block_release() -> None:
    with GitRepository(linked=0) as repository:
        _commit_ignore(repository, ".venv/")
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        handoff_one(repository, target)
        site = repository.repo / ".venv" / "lib" / "python3.12" / "site-packages"
        site.mkdir(parents=True)
        (site / "editable.pth").write_text(f"{target}\n", encoding="utf-8")
        with pytest.raises(ControllerError) as stale:
            release(
                repository.repo,
                target=target,
                expected_record_version=2,
                owner_release="module-owner",
            )
        assert stale.value.code == "STALE_EDITABLE_REFERENCE"

        with GitRepository(linked=0) as repository:
            adopt_canonical(repository)
            target, _ = acquire_one(repository, task="bound", authority="bound")
            handoff_one(repository, target)
            store = MetadataStore(inventory(repository.repo).common_dir)
            with store.lock(exclusive=True):
                snapshot = store._load_unlocked()
                record = next(
                    record for record in snapshot.records.values() if record.task_key == "bound"
                )
                store.replace_record(replace(record, external_bindings=("runtime:test",)))
            with pytest.raises(ControllerError) as binding:
                release(
                    repository.repo,
                    target=target,
                    expected_record_version=2,
                    owner_release="module-owner",
                )
            assert binding.value.code == "EXTERNAL_BINDING"


def test_active_owner_requires_explicit_matching_release() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        handoff_one(repository, target)
        with pytest.raises(ControllerError) as missing:
            release(
                repository.repo,
                target=target,
                expected_record_version=2,
                owner_release=None,
            )
        assert missing.value.code == "ACTIVE_OWNER"
        with pytest.raises(ControllerError) as mismatch:
            release(
                repository.repo,
                target=target,
                expected_record_version=2,
                owner_release="someone-else",
            )
        assert mismatch.value.code == "ACTIVE_OWNER"


def test_base_ref_drift_between_preflight_and_lock_is_blocked() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        first = git(repository.repo, "rev-parse", "HEAD")
        git(repository.repo, "commit", "--allow-empty", "-m", "second")
        second = git(repository.repo, "rev-parse", "HEAD")
        with (
            mock.patch("worktree_controller.lifecycle.resolve_commit", side_effect=[first, second]),
            pytest.raises(ControllerError) as drift,
        ):
            acquire(
                repository.repo,
                task_key="base-drift",
                authority_key="base-drift",
                module="module",
                role="feature",
                owner="owner",
                branch="feature/base-drift",
                base_ref="HEAD",
                target=repository.root / "base-drift",
            )
        assert drift.value.code == "BLOCK_DRIFT"


def test_handoff_release_race_is_serialized_by_cas() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        handoff_one(repository, target)
        handoff_args = [
            "handoff",
            "--repo",
            str(repository.repo),
            "--path",
            str(target),
            "--expected-record-version",
            "2",
            "--kind",
            "preserved",
            "--commit",
            git(target, "rev-parse", "HEAD"),
            "--finalizer-record-id",
            "second-record",
            "--json",
        ]
        release_args = _release_arguments(repository, target, expected=2)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(_cli, [handoff_args, release_args]))
        assert sorted(result.returncode for result in results) == [0, 2]
        payloads = [json.loads(result.stdout) for result in results]
        assert any(payload["decision"]["code"] in {"PRESERVED", "RELEASED"} for payload in payloads)
        assert doctor(repository.repo)["decision"]["code"] == "PASS"


def test_corrupt_metadata_is_diagnosed_without_git_repair() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        store = MetadataStore(inventory(repository.repo).common_dir)
        record = next(store.records_dir.glob("*.json"))
        record.write_text("{broken", encoding="utf-8")
        payload = doctor(repository.repo)
        assert payload["decision"]["code"] == "FAIL"
        assert payload["errors"][0]["code"] == "INVALID_METADATA"


def test_metadata_v1_record_is_readable_and_migrates_on_controlled_update() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, acquired = acquire_one(repository)
        store = MetadataStore(inventory(repository.repo).common_dir)
        allocation_id = str(acquired["decision"]["allocation_id"])
        record_path = store.records_dir / f"{allocation_id}.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        v2_fields = {
            "base_commit",
            "task_commit",
            "finalizer_commit",
            "scope_paths",
            "source_allocations",
            "quarantine_expected_commit",
            "quarantine_observed_commit",
            "quarantine_reason",
            "recovery_allocation_id",
            "recovery_ref",
            "disposable_resources",
        }
        for field in v2_fields:
            record.pop(field)
        record["schema_version"] = 1
        record_path.write_text(json.dumps(record), encoding="utf-8")
        assert status(repository.repo)["decision"] is None

        updated = review(
            repository.repo,
            target=target,
            expected_record_version=1,
            owner="module-owner",
        )
        assert updated["decision"]["code"] == "REVIEWED"
        migrated = json.loads(record_path.read_text(encoding="utf-8"))
        assert migrated["schema_version"] == 4
        assert set(v2_fields) <= set(migrated)


def test_interrupted_acquire_prepared_recovers_as_no_effect() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        result = _cli(
            _acquire_arguments(repository, task="crash-acquire", authority="crash-acquire"),
            crash_at="acquire_prepared",
        )
        assert result.returncode == 97
        assert status(repository.repo)["repository"]["worktree_count"] == 1
        diagnosis = doctor(repository.repo)
        assert diagnosis["decision"]["code"] != "FAIL"
        transactions = diagnosis["decision"]["transactions"]
        assert transactions["unresolved_count"] == 0
        assert {row["state"] for row in transactions["historical_terminal"]} == {
            "ACQUIRE_NO_EFFECT_SUPERSEDED"
        }
        common = inventory(repository.repo).common_dir
        assert len(list((common / "worktree-controller" / "v1" / "transactions").glob("*.json"))) == 1
        # Repository mutation authority is restored: the retained journal no
        # longer fences new work (a same-task retry would additionally need
        # host reservation recovery, which is out of scope here).
        target, payload = acquire_one(repository, task="after-recovery", authority="after-recovery")
        assert payload["decision"]["code"] == "ACQUIRED"


def test_interrupted_acquire_after_metadata_effect_recovers_as_completed() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        result = _cli(
            _acquire_arguments(repository, task="crash-acquire", authority="crash-acquire"),
            crash_at="acquire_metadata_written",
        )
        assert result.returncode == 97
        assert status(repository.repo)["repository"]["worktree_count"] == 2
        diagnosis = doctor(repository.repo)
        assert diagnosis["decision"]["code"] != "FAIL"
        transactions = diagnosis["decision"]["transactions"]
        assert transactions["unresolved_count"] == 0
        assert {row["state"] for row in transactions["historical_terminal"]} == {
            "ACQUIRE_EVIDENCE_SUPERSEDED"
        }
        common = inventory(repository.repo).common_dir
        assert len(list((common / "worktree-controller" / "v1" / "transactions").glob("*.json"))) == 1
        target = repository.root / "crash-acquire"
        handoff_one(repository, target)
        released = release(
            repository.repo,
            target=target,
            expected_record_version=2,
            owner_release="module-owner",
        )
        assert released["decision"]["code"] == "RELEASED"
        closed = doctor(repository.repo)
        assert closed["decision"]["code"] != "FAIL"
        assert closed["decision"]["transactions"]["unresolved_count"] == 0
        assert {
            row["state"] for row in closed["decision"]["transactions"]["historical_terminal"]
        } == {"ACQUIRE_RELEASE_SUPERSEDED"}


@pytest.mark.parametrize("lifecycle", ["ACTIVE", "RETAINED"])
@pytest.mark.parametrize("drift", [
    None, "base", "task", "history", "created", "role", "phase", "detached",
    "nonancestor", "binding", "registration", "frozen",
])
def test_completed_acquire_survives_unfrozen_task_progress(lifecycle, drift) -> None:
    from tests.test_retention_admission import POLICY_V5
    from worktree_controller import cleanup_recovery, transaction_scope
    from worktree_controller.lifecycle import park

    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        result = _cli(
            _acquire_arguments(repository, task="working-acquire", authority="working-acquire"),
            crash_at="acquire_metadata_written",
        )
        assert result.returncode == 97
        target = repository.root / "working-acquire"
        if lifecycle == "RETAINED":
            park(repository.repo, target=target, expected_record_version=1, owner="module-owner")
        git(target, "commit", "--allow-empty", "-m", "synthetic work in progress")
        head = git(target, "rev-parse", "HEAD")
        store = MetadataStore(inventory(repository.repo).common_dir)
        journal_path = next(store.transactions_dir.glob("*.json"))
        journal_bytes = journal_path.read_bytes()
        journal = json.loads(journal_bytes)
        record_path = store.records_dir / f"{journal['allocation_id']}.json"
        record = json.loads(record_path.read_text())
        if drift in {"base", "task", "history", "created", "role", "frozen"}:
            if drift == "history":
                record["history"][0]["event"] = "ADOPTED"
            elif drift == "created":
                record["history"][0]["at"] = "2000-01-01T00:00:00+08:00"
            elif drift == "role":
                record["role"] = "preservation"
            elif drift == "frozen":
                record["lifecycle"] = "HANDOFF_READY"
            else:
                record["base_commit" if drift == "base" else "task_commit"] = head
            record_path.write_text(json.dumps(record))
        elif drift == "phase":
            journal["phase"] = "WORKTREE_ADDED"
            journal_path.write_text(json.dumps(journal))
        elif drift == "detached":
            git(target, "checkout", "--detach")
        elif drift == "nonancestor":
            unrelated = git(target, "commit-tree", "HEAD^{tree}", "-m", "unrelated work")
            git(target, "update-ref", f"refs/heads/{journal['branch']}", unrelated)
        elif drift == "binding":
            for path in store.bindings_dir.glob("*.json"):
                binding = json.loads(path.read_text())
                if binding.get("allocation_id") == journal["allocation_id"]:
                    binding["allocation_id"] = str(uuid.uuid4())
                    path.write_text(json.dumps(binding))
        elif drift == "registration":
            entry = Path((target / ".git").read_text().strip().removeprefix("gitdir: "))
            (entry / "gitdir").write_text(str(repository.root / "elsewhere/.git"))
        record_bytes = record_path.read_bytes()
        with store.lock(exclusive=False):
            state = transaction_scope.terminal_state(store, journal_path.stem)
        assert state == ("ACQUIRE_EVIDENCE_SUPERSEDED" if drift is None else None)
        assert record_path.read_bytes() == record_bytes
        if drift != "phase":
            assert journal_path.read_bytes() == journal_bytes
        if drift is None:
            diagnosis = doctor(repository.repo)
            assert diagnosis["decision"]["transactions"]["unresolved_count"] == 0
            rollout = cleanup_recovery.rollout_check(repository.repo)
            assert rollout["decision"]["code"] == "ROLLOUT_RECOVERY_COMPATIBLE"
            assert rollout["decision"]["transactions"] == [{
                "transaction_id": journal_path.stem, "state": "ACQUIRE_EVIDENCE_SUPERSEDED",
            }]
            assert record_path.read_bytes() == record_bytes
            assert journal_path.read_bytes() == journal_bytes
            assert git(target, "rev-parse", "HEAD") == head
            assert record["lifecycle"] == lifecycle
            assert record["handoff_commit"] is None
            with pytest.raises(ControllerError) as refused:
                release(repository.repo, target=target,
                        expected_record_version=record["record_version"],
                        owner_release="module-owner", dry_run=True)
            assert refused.value.code == "INVALID_LIFECYCLE"
            assert record_path.read_bytes() == record_bytes
            assert journal_path.read_bytes() == journal_bytes


@pytest.fixture
def progressed_archive(monkeypatch):
    from tests.test_retention_admission import POLICY_V5
    from worktree_controller import lifecycle
    from worktree_controller.delivered_closeout import archive_retained

    monkeypatch.setattr(lifecycle, "_validate_runtime_clear", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(lifecycle, "_typed_runtime_evidence", lambda *_args: {
        "managed_references": [], "observed_references": [],
        "managed_visibility_uncertainties": [], "scan_uncertainties": [],
    })
    with GitRepository(linked=0, policy_text=POLICY_V5) as repository:
        adopt_canonical(repository)
        result = _cli(
            _acquire_arguments(repository, task="working-archive", authority="working-archive"),
            crash_at="acquire_metadata_written",
        )
        assert result.returncode == 97
        target = repository.root / "working-archive"
        git(target, "commit", "--allow-empty", "-m", "synthetic task progress")
        lifecycle.park(repository.repo, target=target, expected_record_version=1, owner="module-owner")
        store = MetadataStore(inventory(repository.repo).common_dir)
        journal_path = next(store.transactions_dir.glob("*.json"))
        journal_bytes = journal_path.read_bytes()
        journal = json.loads(journal_bytes)
        bindings = [
            (path, path.read_bytes()) for path in store.bindings_dir.glob("*.json")
            if json.loads(path.read_bytes()).get("allocation_id") == journal["allocation_id"]
        ]
        assert len(bindings) == 1
        args = dict(target=target, expected_record_version=2,
                    owner_release="module-owner", actor="test-closeout")
        plan = archive_retained(repository.repo, **args, dry_run=True)
        archived = archive_retained(repository.repo, **args, plan_id=plan["decision"]["plan_id"])
        assert archived["decision"]["code"] == "ARCHIVED"
        yield repository, target, store, journal_path, journal_bytes, archived, bindings[0]


@pytest.mark.parametrize("drift", [None, "missing_receipt", "receipt", "head", "dirty"])
def test_completed_acquire_survives_formal_archive(progressed_archive, drift) -> None:
    from worktree_controller import transaction_scope

    repository, target, store, journal_path, journal_bytes, archived, _ = progressed_archive
    allocation = json.loads(journal_bytes)["allocation_id"]
    record_path = store.records_dir / f"{allocation}.json"
    before = record_path.read_bytes()
    receipt_path = store.completion_receipts_dir / f"{archived['decision']['receipt_id']}.json"
    if drift == "missing_receipt":
        receipt_path.unlink()
    elif drift == "receipt":
        receipt = json.loads(receipt_path.read_bytes())
        receipt["plan"]["task_completed"] = True
        receipt_path.write_text(json.dumps(receipt))
    elif drift == "head":
        git(target, "commit", "--allow-empty", "-m", "synthetic frozen HEAD drift")
    elif drift == "dirty":
        (target / "unfinished.txt").write_text("synthetic uncommitted work\n")
    with store.lock(exclusive=False):
        state = transaction_scope.terminal_state(store, journal_path.stem)
    assert state == ("ACQUIRE_EVIDENCE_SUPERSEDED" if drift in {None, "dirty"} else None)
    assert record_path.read_bytes() == before
    assert journal_path.read_bytes() == journal_bytes
    record = json.loads(before)
    assert record["lifecycle"] == "ARCHIVED"
    assert record["handoff_commit"] is None
    if drift in {None, "dirty"}:
        assert json.loads(receipt_path.read_bytes())["plan"]["task_completed"] is False
    if drift == "dirty":
        with pytest.raises(ControllerError) as refused:
            release(repository.repo, target=target, expected_record_version=3,
                    owner_release="module-owner", dry_run=True)
        assert refused.value.code == "DIRTY_WORKTREE"
        assert target.exists()
        assert record_path.read_bytes() == before


@pytest.mark.parametrize("drift", [
    None, "reference", "receipt", "missing_head", "directory", "binding", "unattested_retirement",
])
def test_completed_acquire_survives_archival_release(progressed_archive, drift) -> None:
    from worktree_controller import transaction_scope

    repository, target, store, journal_path, journal_bytes, _, binding = progressed_archive
    journal = json.loads(journal_bytes)
    head = git(target, "rev-parse", "HEAD")
    released = release(repository.repo, target=target, expected_record_version=3,
                       owner_release="module-owner")
    assert released["decision"]["code"] == "RELEASED"
    record_path = store.records_dir / f"{journal['allocation_id']}.json"
    before = record_path.read_bytes()
    assert not target.exists()
    if drift == "reference":
        git(repository.repo, "update-ref", f"refs/heads/{journal['branch']}", journal["expected_head"])
    elif drift in {"receipt", "missing_head"}:
        receipt_path = store.release_receipts_dir / f"{released['decision']['release_receipt_id']}.json"
        receipt = json.loads(receipt_path.read_bytes())
        receipt["released_head"] = journal["expected_head"] if drift == "receipt" else None
        receipt_path.write_text(json.dumps(receipt))
    elif drift == "directory":
        target.mkdir()
    elif drift == "binding":
        binding[0].write_bytes(binding[1])
    elif drift == "unattested_retirement":
        git(repository.repo, "update-ref", "-d", f"refs/heads/{journal['branch']}")
    with store.lock(exclusive=False):
        state = transaction_scope.terminal_state(store, journal_path.stem)
    assert state == ("ACQUIRE_RELEASE_SUPERSEDED" if drift is None else None)
    assert record_path.read_bytes() == before
    assert journal_path.read_bytes() == journal_bytes
    if drift is None:
        assert git(repository.repo, "rev-parse", journal["branch"]) == head
        diagnosis = doctor(repository.repo)
        assert diagnosis["decision"]["transactions"]["unresolved_count"] == 0
        assert json.loads(before)["handoff_commit"] is None


@pytest.mark.parametrize("drift", [None, "base", "finalizer", "history", "branch", "detached", "phase", "nonancestor"])
def test_interrupted_acquire_requires_exact_progressed_handoff(drift) -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        result = _cli(
            _acquire_arguments(repository, task="progressed-acquire", authority="progressed-acquire"),
            crash_at="acquire_metadata_written",
        )
        assert result.returncode == 97
        target = repository.root / "progressed-acquire"
        base = git(target, "rev-parse", "HEAD")
        git(target, "commit", "--allow-empty", "-m", "synthetic task result")
        handoff_one(repository, target)
        common = inventory(repository.repo).common_dir
        store = MetadataStore(common)
        journal_path = next(store.transactions_dir.glob("*.json"))
        journal_bytes = journal_path.read_bytes()
        journal = json.loads(journal_bytes)
        record_path = store.records_dir / f"{journal['allocation_id']}.json"
        record = json.loads(record_path.read_text())
        if drift in {"base", "finalizer", "history"}:
            if drift == "history":
                record["history"][0]["event"] = "ADOPTED"
            else:
                record["base_commit" if drift == "base" else "finalizer_commit"] = (
                    record["handoff_commit"] if drift == "base" else base
                )
            record_path.write_text(json.dumps(record))
        elif drift == "branch":
            git(target, "commit", "--allow-empty", "-m", "unreviewed new result")
        elif drift == "detached":
            git(target, "checkout", "--detach")
        elif drift == "phase":
            journal["phase"] = "WORKTREE_ADDED"
            journal_path.write_text(json.dumps(journal))
        elif drift == "nonancestor":
            unrelated = git(target, "commit-tree", "HEAD^{tree}", "-m", "synthetic unrelated result")
            git(target, "update-ref", f"refs/heads/{journal['branch']}", unrelated)
            for key in ("task_commit", "handoff_commit", "finalizer_commit"):
                record[key] = unrelated
            record_path.write_text(json.dumps(record))
        diagnosis = doctor(repository.repo)
        assert diagnosis["decision"]["transactions"]["unresolved_count"] == (0 if drift is None else 1)
        if drift != "phase":
            assert journal_path.read_bytes() == journal_bytes
        assert target.is_dir()
        if drift is None:
            assert {row["state"] for row in diagnosis["decision"]["transactions"]["historical_terminal"]} == {
                "ACQUIRE_HANDOFF_SUPERSEDED"
            }
            assert release(repository.repo, target=target, expected_record_version=2,
                           owner_release="module-owner")["decision"]["code"] == "RELEASED"
            assert journal_path.read_bytes() == journal_bytes
            assert doctor(repository.repo)["decision"]["transactions"]["unresolved_count"] == 0


@pytest.mark.parametrize("drift", ["receipt", "target"])
def test_released_acquire_requires_exact_terminal_evidence(drift: str) -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        result = _cli(
            _acquire_arguments(repository, task="crash-acquire", authority="crash-acquire"),
            crash_at="acquire_metadata_written",
        )
        assert result.returncode == 97
        target = repository.root / "crash-acquire"
        handoff_one(repository, target)
        released = release(
            repository.repo,
            target=target,
            expected_record_version=2,
            owner_release="module-owner",
        )
        assert released["decision"]["code"] == "RELEASED"
        if drift == "receipt":
            common = inventory(repository.repo).common_dir
            receipt_path = next(
                (common / "worktree-controller" / "v1" / "release-receipts").glob("*.json")
            )
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["released_head"] = "0" * 40
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        else:
            target.mkdir()
        diagnosis = doctor(repository.repo)
        assert diagnosis["decision"]["code"] == "FAIL"
        assert diagnosis["decision"]["transactions"]["unresolved_count"] == 1


@pytest.mark.parametrize("progressed", [False, True])
@pytest.mark.parametrize(
    "drift",
    [
        None,
        "missing_receipt",
        "consumer_claim",
        "receipt_head",
        "plan_allocation",
        "release_receipt",
        "writer",
        "target",
    ],
)
def test_released_acquire_stays_terminal_after_authorized_retirement(tmp_path, progressed, drift):
    from tests.test_branch_retirement_execution import _result
    from worktree_controller import retirement_execution as execution
    from worktree_controller.branch_retirement import (
        authorize_branch_retirement,
        complete_branch_retirement,
        plan_branch_retirement,
    )
    from worktree_controller.cleanup_recovery import rollout_check
    from worktree_controller.transaction_scope import terminal_state

    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        assert (
            _cli(
                _acquire_arguments(repository, task="retired-acquire", authority="retired-acquire"),
                crash_at="acquire_metadata_written",
            ).returncode
            == 97
        )
        target = repository.root / "retired-acquire"
        if progressed:
            git(target, "commit", "--allow-empty", "-m", "synthetic reviewed result")
        handoff_one(repository, target)
        released = release(
            repository.repo, target=target, expected_record_version=2, owner_release="module-owner"
        )["decision"]
        git(repository.repo, "merge", "--ff-only", "feature/retired-acquire")
        _configure_remote(repository)
        store = MetadataStore(inventory(repository.repo).common_dir)
        journal_path = next(store.transactions_dir.glob("*.json"))
        original = journal_path.read_bytes()
        plan = plan_branch_retirement(
            repository.repo,
            allocation_id=released["allocation_id"],
            remote="origin",
            integrated_into="main",
        )["decision"]["plan"]
        grant = authorize_branch_retirement(
            repository.repo,
            plan_id=plan["plan_id"],
            operation="local",
            request_id=str(uuid.uuid4()),
        )["decision"]["authorization"]
        git(repository.repo, "update-ref", "-d", grant["ref"], grant["expected_oid"])
        # Exact authorization permits its settlement; it remains a separate fence.
        assert doctor(repository.repo)["decision"]["transactions"]["unresolved_count"] == 0
        assert doctor(repository.repo)["decision"]["retirement_executions"]["outstanding"]
        settled = complete_branch_retirement(
            repository.repo,
            authorization_id=grant["authorization_id"],
            token=grant["token"],
            result_path=_result(repository, grant, execution.REF_REMOVED_AT_EXPECTED_OID, tmp_path),
        )["decision"]
        assert settled["code"] == "RETIREMENT_EXECUTION_COMPLETE"
        receipt_path = store.branch_retirement_receipts_dir / f"{plan['plan_id']}-local.json"
        if drift == "missing_receipt":
            receipt_path.unlink()
        elif drift in {"consumer_claim", "receipt_head"}:
            receipt = json.loads(receipt_path.read_text())
            receipt["provenance" if drift == "consumer_claim" else "expected_oid"] = (
                "CONSUMER_CLAIM" if drift == "consumer_claim" else "0" * 40
            )
            receipt_path.write_text(json.dumps(receipt))
        elif drift == "plan_allocation":
            path = store.branch_retirement_plans_dir / f"{plan['plan_id']}.json"
            document = json.loads(path.read_text())
            document["allocation_id"] = "11111111-1111-4111-8111-111111111111"
            path.write_text(json.dumps(document))
        elif drift == "release_receipt":
            (store.release_receipts_dir / f"{released['release_receipt_id']}.json").unlink()
        elif drift == "writer":
            store.writer_leases_dir.mkdir(exist_ok=True)
            (store.writer_leases_dir / f"{released['allocation_id']}.json").write_text("{}")
        elif drift == "target":
            target.mkdir()
        with store.lock(exclusive=False):
            assert terminal_state(store, journal_path.stem) == (
                "ACQUIRE_RELEASE_SUPERSEDED" if drift is None else None
            )
        assert journal_path.read_bytes() == original
        if drift is None:
            assert doctor(repository.repo)["decision"]["transactions"]["unresolved_count"] == 0
            assert (
                rollout_check(repository.repo)["decision"]["code"] == "ROLLOUT_RECOVERY_COMPATIBLE"
            )


def test_interrupted_acquire_with_orphan_worktree_stays_fenced_until_cleaned() -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        result = _cli(
            _acquire_arguments(repository, task="crash-acquire", authority="crash-acquire"),
            crash_at="acquire_worktree_added",
        )
        assert result.returncode == 97
        assert status(repository.repo)["repository"]["worktree_count"] == 2
        diagnosis = doctor(repository.repo)
        assert diagnosis["decision"]["code"] == "FAIL"
        assert "INTERRUPTED_TRANSACTION" in {warning["code"] for warning in diagnosis["warnings"]}
        assert diagnosis["decision"]["transactions"]["unresolved_count"] == 1
        git(repository.repo, "worktree", "remove", "--force", str(repository.root / "crash-acquire"))
        git(repository.repo, "branch", "-D", "feature/crash-acquire")
        (repository.root / "crash-acquire").mkdir()
        residual = doctor(repository.repo)
        assert residual["decision"]["code"] == "FAIL"
        assert residual["decision"]["transactions"]["unresolved_count"] == 1
        shutil.rmtree(repository.root / "crash-acquire")
        recovered = doctor(repository.repo)
        assert recovered["decision"]["code"] != "FAIL"
        assert recovered["decision"]["transactions"]["unresolved_count"] == 0
        assert {row["state"] for row in recovered["decision"]["transactions"]["historical_terminal"]} == {
            "ACQUIRE_NO_EFFECT_SUPERSEDED"
        }


@pytest.mark.parametrize(
    "crash_at", ["release_prepared", "release_worktree_removed", "release_metadata_written"]
)
def test_interrupted_release_is_fail_closed(crash_at: str) -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        handoff_one(repository, target)
        branch_oid = git(repository.repo, "rev-parse", "refs/heads/feature/new-task")
        result = _cli(_release_arguments(repository, target), crash_at=crash_at)
        assert result.returncode == 97
        assert git(repository.repo, "rev-parse", "refs/heads/feature/new-task") == branch_oid
        assert doctor(repository.repo)["decision"]["code"] == "FAIL"


def test_twenty_seven_worktree_incident_budget_regression() -> None:
    with GitRepository(linked=26) as repository:
        policy = repository.repo / ".agents" / "worktree-policy.toml"
        policy.write_text(
            policy.read_text(encoding="utf-8").replace(
                "require_adoption = true", "require_adoption = false"
            ),
            encoding="utf-8",
        )
        payload = status(repository.repo)
        assert payload["repository"]["worktree_count"] == 27
        decision = plan(
            repository.repo,
            task_key="twenty-eighth",
            authority_key="twenty-eighth",
            module="module",
            role="emergency",
        )
        assert decision["decision"]["code"] == "CREATE_EMERGENCY_ALLOWED"


def test_multiple_candidate_names_cannot_share_active_authority() -> None:
    with GitRepository(linked=3) as repository:
        adopt_canonical(repository)
        for index, target in enumerate(repository.linked, start=1):
            adopt(
                repository.repo,
                target=target,
                task_key=f"candidate-v{index}",
                authority_key="single-capability",
                module="module",
                role="feature",
                owner="unassigned",
                lifecycle="ACTIVE",
            )
        warnings = {warning["code"] for warning in status(repository.repo)["warnings"]}
        assert "DUPLICATE_AUTHORITY" in warnings
        decision = plan(
            repository.repo,
            task_key="candidate-next",
            authority_key="single-capability",
            module="module",
            role="feature",
        )
        assert decision["decision"]["code"] == "BLOCK_DUPLICATE_AUTHORITY"


def test_integration_guard_checks_task_commit_lineage_and_path_scope() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        _source, _source_allocation, candidate, _candidate_allocation = (
            _prepare_integration_candidate(repository)
        )
        diagnosis = doctor(repository.repo)
        codes = {warning["code"] for warning in diagnosis["warnings"]}
        assert diagnosis["decision"]["code"] == "FAIL"
        assert {"TASK_HEAD_MISMATCH", "RECOVERY_REQUIRED"} <= codes

        handoff_one(repository, candidate)
        assert doctor(repository.repo)["decision"]["code"] == "PASS"

        (candidate / "calendar" / "drift.py").write_text("DRIFT = True\n", encoding="utf-8")
        (candidate / "stock.py").write_text("STOCK = True\n", encoding="utf-8")
        git(candidate, "add", "calendar/drift.py", "stock.py")
        git(candidate, "commit", "-m", "develop stock feature in integration")
        diagnosis = doctor(repository.repo)
        codes = {warning["code"] for warning in diagnosis["warnings"]}
        assert {
            "TASK_HEAD_MISMATCH",
            "HANDOFF_HEAD_MISMATCH",
            "FINALIZER_HEAD_MISMATCH",
            "COMMIT_SCOPE_VIOLATION",
            "PATH_SCOPE_VIOLATION",
            "RECOVERY_REQUIRED",
        } <= codes
        with pytest.raises(ControllerError) as blocked:
            handoff_one(repository, candidate, expected=2)
        assert blocked.value.code == "COMMIT_SCOPE_VIOLATION"


def test_quarantine_externalize_and_release_preserve_lineage() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        _source, _source_allocation, candidate, candidate_allocation = (
            _prepare_integration_candidate(repository)
        )
        handoff_one(repository, candidate)
        expected_head = git(candidate, "rev-parse", "HEAD")
        (candidate / "calendar" / "drift.py").write_text("DRIFT = True\n", encoding="utf-8")
        git(candidate, "add", "calendar/drift.py")
        git(candidate, "commit", "-m", "drift after integration handoff")
        observed_head = git(candidate, "rev-parse", "HEAD")

        isolated = quarantine(
            repository.repo,
            target=candidate,
            expected_record_version=2,
            owner="module-owner",
            reason="feature work drifted into integration",
            expected_commit=expected_head,
        )
        assert isolated["decision"]["code"] == "QUARANTINED"
        recovery, recovery_payload = acquire_one(
            repository,
            task="calendar-recovery",
            authority="calendar-recovery",
            role="recovery",
            source_allocations=(candidate_allocation,),
            base_ref=observed_head,
            scope_paths=("calendar",),
        )
        assert git(recovery, "rev-parse", "HEAD") == observed_head
        linked = externalize(
            repository.repo,
            source_target=candidate,
            recovery_target=recovery,
            expected_source_record_version=3,
            expected_recovery_record_version=1,
            owner="module-owner",
        )
        assert linked["decision"]["code"] == "RECOVERY_EXTERNALIZED"
        recovery_ref = str(linked["decision"]["recovery_ref"])
        assert git(repository.repo, "rev-parse", recovery_ref) == observed_head

        released = release(
            repository.repo,
            target=candidate,
            expected_record_version=4,
            owner_release="module-owner",
        )
        assert released["decision"]["code"] == "RELEASED"
        assert not candidate.exists()
        assert recovery.exists()
        assert git(repository.repo, "rev-parse", recovery_ref) == observed_head
        assert recovery_payload["decision"]["code"] == "ACQUIRED"


def test_registered_python_venv_requires_controlled_cleanup_before_release() -> None:
    with GitRepository(linked=0) as repository:
        _commit_ignore(repository, ".venv/")
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        venv = target / ".venv"
        venv.mkdir()
        (venv / "pyvenv.cfg").write_text("home = /test\n", encoding="utf-8")
        registered = register_disposable(
            repository.repo,
            target=target,
            expected_record_version=1,
            owner="module-owner",
            resource_path=".venv",
            kind="python-venv",
            reproducible=True,
            creator="test-fixture",
            teardown_policy="manual",
        )
        assert registered["decision"]["code"] == "DISPOSABLE_REGISTERED"
        handoff_one(repository, target, expected=2)
        with pytest.raises(ControllerError) as present:
            release(
                repository.repo,
                target=target,
                expected_record_version=3,
                owner_release="module-owner",
            )
        assert present.value.code == "DISPOSABLE_RESOURCE_PRESENT"
        cleaned = cleanup_disposable(
            repository.repo,
            target=target,
            expected_record_version=3,
            owner="module-owner",
            resource_path=".venv",
        )
        assert cleaned["decision"]["code"] == "DISPOSABLE_CLEANED"
        assert not venv.exists()
        released = release(
            repository.repo,
            target=target,
            expected_record_version=4,
            owner_release="module-owner",
        )
        assert released["decision"]["code"] == "RELEASED"


def test_existing_feature_checkout_can_be_explicitly_adopted_for_recovery() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        _source, _source_allocation, candidate, _candidate_allocation = (
            _prepare_integration_candidate(repository)
        )
        handoff_one(repository, candidate)
        expected_head = git(candidate, "rev-parse", "HEAD")
        git(candidate, "commit", "--allow-empty", "-m", "polluted integration commit")
        observed_head = git(candidate, "rev-parse", "HEAD")
        quarantined = quarantine(
            repository.repo,
            target=candidate,
            expected_record_version=2,
            owner="module-owner",
            reason="legacy recovery adoption",
            expected_commit=expected_head,
        )
        candidate_allocation = str(quarantined["decision"]["allocation_id"])
        legacy, _legacy_payload = acquire_one(
            repository,
            task="legacy-etf-recovery",
            authority="legacy-etf-recovery",
            role="feature",
            base_ref=observed_head,
        )
        adopted = adopt_recovery(
            repository.repo,
            source_target=candidate,
            recovery_target=legacy,
            expected_source_record_version=3,
            expected_recovery_record_version=1,
            source_owner="module-owner",
            recovery_owner="module-owner",
            scope_paths=("etf",),
        )
        assert adopted["decision"]["code"] == "RECOVERY_ADOPTED"
        state = next(
            item
            for item in adopted["worktrees"]
            if item["allocation_id"] == adopted["decision"]["allocation_id"]
        )
        assert state["role"] == "recovery"
        assert state["source_allocations"] == [candidate_allocation]
        linked = externalize(
            repository.repo,
            source_target=candidate,
            recovery_target=legacy,
            expected_source_record_version=3,
            expected_recovery_record_version=2,
            owner="module-owner",
        )
        assert linked["decision"]["code"] == "RECOVERY_EXTERNALIZED"


def test_disposable_cleanup_rejects_unrecognized_directory_without_deleting_it() -> None:
    with GitRepository(linked=0) as repository:
        _commit_ignore(repository, ".venv/")
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        venv = target / ".venv"
        venv.mkdir()
        important = venv / "keep.txt"
        important.write_text("not a virtual environment\n", encoding="utf-8")
        with pytest.raises(ControllerError) as rejected:
            register_disposable(
                repository.repo,
                target=target,
                expected_record_version=1,
                owner="module-owner",
                resource_path=".venv",
                kind="python-venv",
                reproducible=True,
                creator="test-fixture",
                teardown_policy="manual",
            )
        assert rejected.value.code == "RESOURCE_IDENTITY_MISMATCH"
        assert important.read_text(encoding="utf-8") == "not a virtual environment\n"


def test_publication_lease_fetches_main_serializes_and_verifies_completion() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        _configure_remote(repository)
        _source, source_allocation, first, _first_allocation = _prepare_integration_candidate(
            repository, candidate_task="candidate-one"
        )
        handoff_one(repository, first)
        second = repository.root / "candidate-two"
        acquire(
            repository.repo,
            task_key="candidate-two",
            authority_key="candidate-two",
            module="integration",
            role="integration",
            owner="module-owner",
            branch="integration/candidate-two",
            base_ref="main",
            target=second,
            source_allocations=(source_allocation,),
        )
        git(second, "merge", "--ff-only", "feature/calendar-source")
        handoff_one(repository, second)

        first_lease = acquire_publication_lease(
            repository.repo,
            target=first,
            expected_record_version=2,
            owner="module-owner",
        )
        assert first_lease["decision"]["code"] == "PUBLICATION_LEASE_ACQUIRED"
        assert doctor(repository.repo)["decision"]["code"] == "PASS"
        with pytest.raises(ControllerError) as occupied:
            acquire_publication_lease(
                repository.repo,
                target=second,
                expected_record_version=2,
                owner="module-owner",
            )
        assert occupied.value.code == "PUBLICATION_LEASE_HELD"

        aborted = abort_publication_lease(
            repository.repo,
            target=first,
            expected_record_version=3,
            lease_id=str(first_lease["decision"]["lease_id"]),
            owner="module-owner",
            run_id=str(first_lease["decision"]["run_id"]),
        )
        assert aborted["decision"]["code"] == "PUBLICATION_LEASE_ABORTED"
        second_lease = acquire_publication_lease(
            repository.repo,
            target=second,
            expected_record_version=2,
            owner="module-owner",
        )
        candidate_head = git(second, "rev-parse", "HEAD")
        git(second, "push", "origin", "HEAD:main")
        completed = complete_publication_lease(
            repository.repo,
            target=second,
            expected_record_version=3,
            lease_id=str(second_lease["decision"]["lease_id"]),
            published_commit=candidate_head,
            finalizer_record_id="publication-finalizer-record",
            run_id=str(second_lease["decision"]["run_id"]),
            auto_release=False,
        )
        assert completed["decision"]["code"] == "PUBLICATION_COMPLETED"
        payload = status(repository.repo)
        assert payload["repository"]["publication_lease"] is None
        second_state = next(item for item in payload["worktrees"] if item["path"] == str(second))
        assert second_state["lifecycle"] == "PUBLISHED"
        assert second_state["finalizer_record_id"] == "publication-finalizer-record"
        assert doctor(repository.repo)["decision"]["code"] == "PASS"


def test_publication_lease_rejects_candidate_behind_fresh_remote_main() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        _configure_remote(repository)
        _source, _source_allocation, candidate, _candidate_allocation = (
            _prepare_integration_candidate(repository)
        )
        handoff_one(repository, candidate)
        git(repository.repo, "commit", "--allow-empty", "-m", "advance main")
        git(repository.repo, "push", "origin", "main")
        with pytest.raises(ControllerError) as stale:
            acquire_publication_lease(
                repository.repo,
                target=candidate,
                expected_record_version=2,
                owner="module-owner",
            )
        assert stale.value.code == "STALE_PUBLICATION_BASE"


def test_second_candidate_can_revalidate_after_first_candidate_publishes() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        _configure_remote(repository)
        _calendar_source, _calendar_allocation, first, _first_allocation = (
            _prepare_integration_candidate(repository, candidate_task="calendar-candidate")
        )
        handoff_one(repository, first)

        stock_source, stock_payload = acquire_one(
            repository,
            task="stock-source",
            authority="stock-source",
            scope_paths=("stock",),
        )
        stock = stock_source / "stock"
        stock.mkdir()
        (stock / "daily.py").write_text("DAILY = True\n", encoding="utf-8")
        git(stock_source, "add", "stock/daily.py")
        git(stock_source, "commit", "-m", "add stock daily feature")
        handoff_one(repository, stock_source)
        stock_allocation = str(stock_payload["decision"]["allocation_id"])

        second = repository.root / "stock-candidate"
        acquire(
            repository.repo,
            task_key="stock-candidate",
            authority_key="stock-candidate",
            module="integration",
            role="integration",
            owner="module-owner",
            branch="integration/stock-candidate",
            base_ref="main",
            target=second,
            source_allocations=(stock_allocation,),
        )
        git(second, "merge", "--ff-only", "feature/stock-source")
        handoff_one(repository, second)

        first_lease = acquire_publication_lease(
            repository.repo,
            target=first,
            expected_record_version=2,
            owner="module-owner",
        )
        first_head = git(first, "rev-parse", "HEAD")
        git(first, "push", "origin", "HEAD:main")
        complete_publication_lease(
            repository.repo,
            target=first,
            expected_record_version=3,
            lease_id=str(first_lease["decision"]["lease_id"]),
            published_commit=first_head,
            finalizer_record_id="calendar-publication-finalizer",
            run_id=str(first_lease["decision"]["run_id"]),
            auto_release=False,
        )

        git(second, "merge", "--no-edit", "origin/main")
        handoff_one(
            repository,
            second,
            expected=2,
            mainline_commit="origin/main",
        )
        second_lease = acquire_publication_lease(
            repository.repo,
            target=second,
            expected_record_version=3,
            owner="module-owner",
        )
        second_head = git(second, "rev-parse", "HEAD")
        git(second, "push", "origin", "HEAD:main")
        completed = complete_publication_lease(
            repository.repo,
            target=second,
            expected_record_version=4,
            lease_id=str(second_lease["decision"]["lease_id"]),
            published_commit=second_head,
            finalizer_record_id="stock-publication-finalizer",
            run_id=str(second_lease["decision"]["run_id"]),
            auto_release=False,
        )
        assert completed["decision"]["code"] == "PUBLICATION_COMPLETED"
        assert git(repository.repo, "rev-parse", "origin/main") == second_head
        assert doctor(repository.repo)["decision"]["code"] == "PASS"


def test_concurrent_publication_candidates_obtain_only_one_lease() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        _configure_remote(repository)
        _source, source_allocation, first, _first_allocation = _prepare_integration_candidate(
            repository, candidate_task="lease-one"
        )
        handoff_one(repository, first)
        second = repository.root / "lease-two"
        acquire(
            repository.repo,
            task_key="lease-two",
            authority_key="lease-two",
            module="integration",
            role="integration",
            owner="module-owner",
            branch="integration/lease-two",
            base_ref="main",
            target=second,
            source_allocations=(source_allocation,),
        )
        git(second, "merge", "--ff-only", "feature/calendar-source")
        handoff_one(repository, second)

        calls = [
            [
                "lease-acquire",
                "--repo",
                str(repository.repo),
                "--path",
                str(candidate),
                "--expected-record-version",
                "2",
                "--owner",
                "module-owner",
                "--json",
            ]
            for candidate in (first, second)
        ]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(_cli, calls))
        assert sorted(result.returncode for result in results) == [0, 2]
        failure = next(json.loads(result.stdout) for result in results if result.returncode == 2)
        assert failure["errors"][0]["code"] == "PUBLICATION_LEASE_HELD"
        payload = status(repository.repo)
        assert payload["repository"]["publication_lease"] is not None
        assert sum(item["lifecycle"] == "INTEGRATING" for item in payload["worktrees"]) == 1


def test_partial_publication_lease_transitions_are_recoverable() -> None:
    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        _configure_remote(repository)
        _source, _source_allocation, candidate, _candidate_allocation = (
            _prepare_integration_candidate(repository)
        )
        handoff_one(repository, candidate)
        with (
            mock.patch.object(
                MetadataStore, "write_publication_lease", side_effect=OSError("write failed")
            ),
            pytest.raises(OSError, match="write failed"),
        ):
            acquire_publication_lease(
                repository.repo,
                target=candidate,
                expected_record_version=2,
                owner="module-owner",
            )
        payload = status(repository.repo)
        lease = payload["repository"]["publication_lease"]
        assert lease is None
        candidate_state = next(
            item for item in payload["worktrees"] if item["path"] == str(candidate)
        )
        assert candidate_state["lifecycle"] == "INTEGRATING"
        diagnosis = doctor(repository.repo)
        assert diagnosis["decision"]["code"] == "FAIL"
        assert "PUBLICATION_LEASE_DRIFT" in {warning["code"] for warning in diagnosis["warnings"]}
        recovered = abort_publication_lease(
            repository.repo,
            target=candidate,
            expected_record_version=3,
            lease_id=None,
            owner="module-owner",
            run_id="recovery-run",
        )
        assert recovered["decision"]["code"] == "PUBLICATION_LEASE_RECOVERED"
        assert recovered["repository"]["publication_lease"] is None

    with GitRepository(linked=0, policy_text=POLICY_V2) as repository:
        _configure_remote(repository)
        _source, _source_allocation, candidate, _candidate_allocation = (
            _prepare_integration_candidate(repository)
        )
        handoff_one(repository, candidate)
        leased = acquire_publication_lease(
            repository.repo,
            target=candidate,
            expected_record_version=2,
            owner="module-owner",
        )
        candidate_head = git(candidate, "rev-parse", "HEAD")
        git(candidate, "push", "origin", "HEAD:main")
        with (
            mock.patch.object(
                MetadataStore, "remove_publication_lease", side_effect=OSError("remove failed")
            ),
            pytest.raises(OSError, match="remove failed"),
        ):
            complete_publication_lease(
                repository.repo,
                target=candidate,
                expected_record_version=3,
                lease_id=str(leased["decision"]["lease_id"]),
                published_commit=candidate_head,
                finalizer_record_id="publication-finalizer-record",
                run_id=str(leased["decision"]["run_id"]),
                auto_release=False,
            )
        payload = status(repository.repo)
        assert payload["repository"]["publication_lease"] is not None
        candidate_state = next(
            item for item in payload["worktrees"] if item["path"] == str(candidate)
        )
        assert candidate_state["lifecycle"] == "PUBLISHED"
        completed = complete_publication_lease(
            repository.repo,
            target=candidate,
            expected_record_version=4,
            lease_id=str(leased["decision"]["lease_id"]),
            published_commit=candidate_head,
            finalizer_record_id="publication-finalizer-record",
            run_id=str(leased["decision"]["run_id"]),
            auto_release=False,
        )
        assert completed["decision"]["code"] == "PUBLICATION_COMPLETED"
        assert completed["repository"]["publication_lease"] is None
