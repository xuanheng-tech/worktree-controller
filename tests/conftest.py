"""Unit admission uses isolated host state and deterministic resource samples."""

import os
import stat
from pathlib import Path

import pytest

from worktree_controller import resource_admission as resources


def _remove_test_special_nodes(root: Path) -> None:
    # Preservation tests retain regular evidence, but their FIFO/socket endpoints
    # must not make the task's later supported temp cleanup unsafe.
    def unreadable(error):
        raise error

    device = root.stat().st_dev
    for directory, directories, files in os.walk(root, followlinks=False, onerror=unreadable):
        directories[:] = [
            name for name in directories if (Path(directory) / name).lstat().st_dev == device
        ]
        for name in files:
            path = Path(directory) / name
            metadata = path.lstat()
            if not (stat.S_ISFIFO(metadata.st_mode) or stat.S_ISSOCK(metadata.st_mode)):
                continue
            parent = path.parent
            mode = stat.S_IMODE(parent.stat().st_mode)
            try:
                parent.chmod(mode | stat.S_IWUSR)
                path.unlink()
            finally:
                parent.chmod(mode)


@pytest.fixture(autouse=True)
def isolated_host_resources(tmp_path):
    # Keep isolation independent of a test's own monkeypatch/undo. Its patches
    # finish before this outer fixture removes only its own special endpoints.
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "host-state"))
        real_filesystem = resources.filesystem

        def ample_filesystem(path):
            observed = real_filesystem(path)
            return {**observed, "available_bytes": 10**15, "available_inodes": 10**12}

        monkeypatch.setattr(resources, "filesystem", ample_filesystem)
        monkeypatch.setattr(resources, "pressure", lambda: {
            "available_memory_bytes": 10**15, "observed_at_ns": 1,
            "psi_avg10": {"cpu": 0.0, "io": 0.0, "memory": 0.0},
        })
        yield
    _remove_test_special_nodes(tmp_path)
