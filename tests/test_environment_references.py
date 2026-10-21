"""Incomplete environment scans must never authorize checkout removal."""

import errno
import os
from pathlib import Path

import pytest

from tests.test_controlled import _prepare_teardown_target
from tests.test_shadow import GitRepository
from worktree_controller import environment
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle import release
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore


@pytest.mark.parametrize(("shape", "code"), [
    ("oversize", "ENVIRONMENT_SCAN_LIMIT"),
    ("symlink", "ENVIRONMENT_SCAN_ERROR"),
    ("fifo", "ENVIRONMENT_SCAN_ERROR"),
    ("unreadable-directory", "ENVIRONMENT_SCAN_ERROR"),
])
@pytest.mark.parametrize("dry_run", [True, False])
def test_incomplete_reference_scan_preserves_checkout_and_metadata(monkeypatch, shape, code, dry_run):
    with GitRepository(linked=0) as repository:
        target = _prepare_teardown_target(repository)
        site = repository.repo / ".venv/lib/python3.12/site-packages"
        site.mkdir(parents=True)
        reference = site / "external.pth"
        if shape == "oversize":
            reference.write_bytes(os.fsencode(target) + b"\n" + b"#" * environment.MAX_REFERENCE_FILE_BYTES)
        elif shape == "symlink":
            backing = repository.root / "reference.txt"
            backing.write_text(f"{target}\n")
            reference.symlink_to(backing)
        elif shape == "fifo":
            os.mkfifo(reference)
        else:
            real_scandir = os.scandir

            def scandir(path):
                if not isinstance(path, int) and Path(path) == site:
                    raise PermissionError(errno.EACCES, "fixture denied enumeration", str(site))
                return real_scandir(path)

            monkeypatch.setattr(os, "scandir", scandir)
        store = MetadataStore(inventory(repository.repo).common_dir)
        before = store.load()
        with pytest.raises(ControllerError) as error:
            release(
                repository.repo, target=target, expected_record_version=3,
                owner_release="module-owner", teardown_reproducible_venv=True, dry_run=dry_run,
            )
        assert error.value.code == code
        assert (target / ".venv").is_dir()
        assert store.load() == before


def test_reference_at_size_limit_is_scanned(tmp_path):
    path = tmp_path / "reference.pth"
    payload = b"x" * environment.MAX_REFERENCE_FILE_BYTES
    path.write_bytes(payload)
    assert environment._reference_payload(path) == payload
