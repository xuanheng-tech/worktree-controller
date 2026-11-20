from __future__ import annotations

from unittest.mock import patch

from worktree_controller.convergence import busy_paths


def test_busy_paths_optimization() -> None:
    # 1. Empty targets returns empty set immediately
    assert busy_paths(set()) == set()

    # 2. Mock _referenced_paths to return specific candidates
    sample_proc = {
        1001: ["/tmp/example-worktrees/repo/wt1/file.txt", "/usr/lib/libc.so"],
        1002: ["/tmp/example-worktrees/repo/wt1-other/test.py", "/dev/null"],
        1003: ["/tmp/example-worktrees/repo/wt2", "/lib/x86_64-linux-gnu/ld.so"],
        1004: ["/other/path/unrelated.py"],
    }

    def fake_referenced(pid: int) -> list[str]:
        return sample_proc.get(pid, [])

    targets = {
        "/tmp/example-worktrees/repo/wt1",
        "/tmp/example-worktrees/repo/wt2",
        "/tmp/example-worktrees/repo/wt3",
    }

    with (
        patch("os.listdir", return_value=["1001", "1002", "1003", "1004"]),
        patch("worktree_controller.convergence._referenced_paths", side_effect=fake_referenced),
        patch("os.getpid", return_value=9999),
    ):
        result = busy_paths(targets)
        # wt1 matched via prefix /tmp/example-worktrees/repo/wt1/file.txt
        # wt1-other does NOT match wt1
        # wt2 matched directly via /tmp/example-worktrees/repo/wt2
        # wt3 is not in sample_proc
        assert result == {"/tmp/example-worktrees/repo/wt1", "/tmp/example-worktrees/repo/wt2"}
