"""Inherited Git repository selection never redirects Controller observations."""

from __future__ import annotations

import os
import stat

import pytest

from tests.test_controlled import acquire_one, adopt_canonical
from tests.test_shadow import GitRepository
from worktree_controller.git_backend import REPOSITORY_SELECTION_ENVIRONMENT, inventory
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore


@pytest.mark.parametrize("variable", ["GIT_DIR", "GIT_COMMON_DIR", "GIT_INDEX_FILE"])
def test_inherited_repository_selection_is_ignored(monkeypatch, variable) -> None:
    with GitRepository(linked=1) as requested, GitRepository(linked=0) as other:
        expected = inventory(requested.repo)
        value = other.repo / ".git" / ("index" if variable == "GIT_INDEX_FILE" else "")
        monkeypatch.setenv(variable, str(value))
        observed = inventory(requested.repo)
        assert observed.common_dir == expected.common_dir
        assert [item.path for item in observed.worktrees] == [
            item.path for item in expected.worktrees
        ]
        assert [item.clean for item in observed.worktrees] == [
            item.clean for item in expected.worktrees
        ]


def test_selection_list_covers_git_local_environment() -> None:
    # Every variable Git itself treats as repository-local selection, except the
    # configuration and replacement switches the Controller deliberately keeps.
    kept = {"GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_NO_REPLACE_OBJECTS"}
    import subprocess

    local = set(
        subprocess.run(
            ["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True, check=True
        ).stdout.split()
    )
    assert local - kept <= REPOSITORY_SELECTION_ENVIRONMENT


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
@pytest.mark.parametrize("directory", ["bindings_dir", "records_dir", "writer_leases_dir"])
def test_unreadable_metadata_directory_fails_closed(directory: str) -> None:
    with GitRepository(linked=0) as repository:
        adopt_canonical(repository)
        target, _ = acquire_one(repository)
        store = MetadataStore(inventory(repository.repo).common_dir)
        path = getattr(store, directory)
        path.mkdir(mode=0o700, exist_ok=True)
        mode = stat.S_IMODE(path.stat().st_mode)
        path.chmod(0)
        try:
            with pytest.raises(ControllerError) as refused:
                store.load()
            assert refused.value.code == "FILESYSTEM_PERMISSION"
        finally:
            path.chmod(mode)
        assert target.is_dir()
