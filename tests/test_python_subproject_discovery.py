from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.test_controlled import acquire_one, adopt_canonical
from tests.test_shadow import GitRepository, git
from worktree_controller.environment import (
    discover_python_project,
)
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import (
    _teardown_preflight,
    register_disposable,
)
from worktree_controller.models import ControllerError
from worktree_controller.store import validate_teardown_receipt


def _make_fake_venv(target: Path, rel_path: str = ".venv") -> Path:
    venv = target / rel_path
    binary = venv / "bin"
    binary.mkdir(parents=True, exist_ok=True)
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
    site.mkdir(parents=True, exist_ok=True)
    (site / "self-editable.pth").write_text(f"{target}\n", encoding="utf-8")
    return venv


def test_discover_root_project():
    with GitRepository(linked=0) as repository:
        root = repository.repo
        (root / "pyproject.toml").write_text('[project]\nname="root-pkg"\nversion="1.0"\n')
        (root / "uv.lock").write_text("version = 1\n")
        git(root, "add", "pyproject.toml", "uv.lock")
        git(root, "commit", "-m", "root project")

        proj, lock = discover_python_project(root, venv_relative_path=".venv")
        assert proj == "pyproject.toml"
        assert lock == "uv.lock"


def test_discover_subproject_with_root_venv():
    with GitRepository(linked=0) as repository:
        root = repository.repo
        (root / "backend").mkdir()
        (root / "backend/pyproject.toml").write_text('[project]\nname="sub-pkg"\nversion="1.0"\n')
        (root / "backend/uv.lock").write_text("version = 1\n")
        git(root, "add", "backend/pyproject.toml", "backend/uv.lock")
        git(root, "commit", "-m", "subproject only")

        proj, lock = discover_python_project(root, venv_relative_path=".venv")
        assert proj == "backend/pyproject.toml"
        assert lock == "backend/uv.lock"


def test_discover_subproject_with_subproject_venv():
    with GitRepository(linked=0) as repository:
        root = repository.repo
        (root / "backend").mkdir()
        (root / "backend/pyproject.toml").write_text('[project]\nname="sub-pkg"\nversion="1.0"\n')
        (root / "backend/uv.lock").write_text("version = 1\n")
        git(root, "add", "backend/pyproject.toml", "backend/uv.lock")
        git(root, "commit", "-m", "subproject only")

        proj, lock = discover_python_project(root, venv_relative_path="backend/.venv")
        assert proj == "backend/pyproject.toml"
        assert lock == "backend/uv.lock"


def test_root_precedence_over_subproject():
    with GitRepository(linked=0) as repository:
        root = repository.repo
        (root / "pyproject.toml").write_text('[project]\nname="root-pkg"\nversion="1.0"\n')
        (root / "uv.lock").write_text("version = 1\n")
        (root / "packages/pkg_a").mkdir(parents=True)
        (root / "packages/pkg_a/pyproject.toml").write_text(
            '[project]\nname="pkg-a"\nversion="1.0"\n'
        )
        (root / "packages/pkg_a/uv.lock").write_text("version = 1\n")
        git(root, "add", ".")
        git(root, "commit", "-m", "workspace with root and member")

        proj, lock = discover_python_project(root, venv_relative_path=".venv")
        assert proj == "pyproject.toml"
        assert lock == "uv.lock"

        sub_proj, sub_lock = discover_python_project(
            root, venv_relative_path="packages/pkg_a/.venv"
        )
        assert sub_proj == "packages/pkg_a/pyproject.toml"
        assert sub_lock == "packages/pkg_a/uv.lock"


def test_missing_lockfile_fails_closed():
    with GitRepository(linked=0) as repository:
        root = repository.repo
        (root / "backend").mkdir()
        (root / "backend/pyproject.toml").write_text('[project]\nname="sub-pkg"\nversion="1.0"\n')
        git(root, "add", "backend/pyproject.toml")
        git(root, "commit", "-m", "subproject without lock")

        with pytest.raises(ControllerError) as exc:
            discover_python_project(root, venv_relative_path=".venv")
        assert exc.value.code == "PROJECT_IDENTITY_MISMATCH"
        assert "backend/uv.lock" in str(exc.value)


def test_missing_pyproject_fails_closed():
    with GitRepository(linked=0) as repository:
        root = repository.repo
        (root / "README.md").write_text("hello\n")
        git(root, "add", "README.md")
        git(root, "commit", "-m", "no pyproject at all")

        with pytest.raises(ControllerError) as exc:
            discover_python_project(root, venv_relative_path=".venv")
        assert exc.value.code == "PROJECT_IDENTITY_MISMATCH"
        assert "tracked pyproject.toml" in str(exc.value)


def test_untracked_subproject_fails_closed():
    with GitRepository(linked=0) as repository:
        root = repository.repo
        (root / "README.md").write_text("hello\n")
        git(root, "add", "README.md")
        git(root, "commit", "-m", "initial")

        (root / "backend").mkdir()
        (root / "backend/pyproject.toml").write_text('[project]\nname="sub-pkg"\nversion="1.0"\n')
        (root / "backend/uv.lock").write_text("version = 1\n")

        with pytest.raises(ControllerError) as exc:
            discover_python_project(root, venv_relative_path=".venv")
        assert exc.value.code == "PROJECT_IDENTITY_MISMATCH"


def test_symlink_escape_fails_closed(tmp_path):
    with GitRepository(linked=0) as repository:
        root = repository.repo
        (root / "backend").mkdir()
        outside = tmp_path / "outside.toml"
        outside.write_text('[project]\nname="escape"\n')
        (root / "backend/pyproject.toml").symlink_to(outside)
        (root / "backend/uv.lock").write_text("version = 1\n")

        with pytest.raises(ControllerError) as exc:
            discover_python_project(root, venv_relative_path=".venv")
        assert exc.value.code in {"RESOURCE_PATH_ESCAPE", "PROJECT_IDENTITY_MISMATCH"}


def test_ambiguous_subprojects_fails_closed():
    with GitRepository(linked=0) as repository:
        root = repository.repo
        (root / "backend").mkdir()
        (root / "backend/pyproject.toml").write_text('[project]\nname="backend"\nversion="1.0"\n')
        (root / "backend/uv.lock").write_text("version = 1\n")
        (root / "worker").mkdir()
        (root / "worker/pyproject.toml").write_text('[project]\nname="worker"\nversion="1.0"\n')
        (root / "worker/uv.lock").write_text("version = 1\n")
        git(root, "add", ".")
        git(root, "commit", "-m", "two sibling subprojects")

        with pytest.raises(ControllerError) as exc:
            discover_python_project(root, venv_relative_path=".venv")
        assert exc.value.code == "PROJECT_IDENTITY_MISMATCH"
        assert "ambiguous" in str(exc.value)


def test_teardown_receipt_validation_subproject():
    valid_data = {
        "schema_version": 1,
        "receipt_id": "9bbd8d08-f85c-4e75-ac5b-163ed1d94d39",
        "transaction_id": "9bbd8d08-f85c-4e75-ac5b-163ed1d94d39",
        "repository_id": "9bbd8d08-f85c-4e75-ac5b-163ed1d94d30",
        "allocation_id": "9bbd8d08-f85c-4e75-ac5b-163ed1d94d31",
        "owner": "test-owner",
        "authority_key": "test-authority",
        "worktree_path": "/home/user/worktree",
        "worktree_head": "0123456789abcdef0123456789abcdef01234567",
        "environment_path": ".venv",
        "pyvenv_cfg_sha256": "0" * 64,
        "cachedir_tag_sha256": "0" * 64,
        "lock_path": "backend/uv.lock",
        "lock_sha256": "0" * 64,
        "project_path": "backend/pyproject.toml",
        "project_sha256": "0" * 64,
        "python_version": "3.12.0",
        "python_implementation": "CPython",
        "python_identity_sha256": "0" * 64,
        "environment_identity_sha256": "0" * 64,
        "environment_device": 1,
        "environment_inode": 2,
        "bytes_before": 100,
        "entries_before": 10,
        "result": "removed",
        "created_at": "2026-09-17T00:00:00+08:00",
        "updated_at": "2026-09-17T00:00:00+08:00",
    }
    validated = validate_teardown_receipt(valid_data)
    assert validated["lock_path"] == "backend/uv.lock"
    assert validated["project_path"] == "backend/pyproject.toml"

    # Mismatched directory
    with pytest.raises(ControllerError) as exc:
        validate_teardown_receipt({**valid_data, "lock_path": "other/uv.lock"})
    assert exc.value.code == "INVALID_METADATA"

    # Absolute path
    with pytest.raises(ControllerError) as exc:
        validate_teardown_receipt({**valid_data, "lock_path": "/backend/uv.lock"})
    assert exc.value.code == "INVALID_METADATA"

    # Traversal
    with pytest.raises(ControllerError) as exc:
        validate_teardown_receipt({**valid_data, "lock_path": "../backend/uv.lock"})
    assert exc.value.code == "INVALID_METADATA"


def test_subproject_teardown_preflight_and_evidence():
    with GitRepository(linked=0) as repository:
        root = repository.repo
        (root / ".gitignore").write_text(".venv/\n")
        (root / "backend").mkdir()
        (root / "backend/pyproject.toml").write_text('[project]\nname="sub"\nversion="1.0"\n')
        (root / "backend/uv.lock").write_text("version = 1\n")
        git(root, "add", ".gitignore", "backend/pyproject.toml", "backend/uv.lock")
        git(root, "commit", "-m", "subproject structure")

        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        _make_fake_venv(target, ".venv")
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

        inv = inventory(repository.repo)
        item = next(w for w in inv.worktrees if str(w.path) == str(target))
        evidence = _teardown_preflight(inv, item)
        assert evidence["lock_path"] == "backend/uv.lock"
        assert evidence["project_path"] == "backend/pyproject.toml"
        assert evidence["environment_path"] == ".venv"
