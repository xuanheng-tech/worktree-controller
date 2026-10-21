"""Unit admission uses isolated host state and deterministic resource samples."""

import pytest

from worktree_controller import resource_admission as resources


@pytest.fixture(autouse=True)
def isolated_host_resources(tmp_path, monkeypatch):
    # CLI subprocesses inherit this root too; no test may reserve the user's host.
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
