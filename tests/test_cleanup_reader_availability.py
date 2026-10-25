"""Typed cleanup fences writers without blocking durable history readers."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.test_lifecycle_history import _closed
from tests.test_preserved_cleanup import clean, dry_run
from tests.test_preserved_cleanup import preserved as preserved
from tests.test_typed_disposable import target as target
from worktree_controller import lifecycle
from worktree_controller.git_backend import inventory
from worktree_controller.lifecycle_history import lifecycle_history
from worktree_controller.models import ControllerError
from worktree_controller.store import MetadataStore


def _probe(path, mode):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


def test_history_is_complete_during_cleanup_and_writers_remain_fenced(preserved, monkeypatch):
    repository, worktree, _, _, _ = preserved
    _, _, receipt = _closed(repository, task="closed-reader", authority="closed-reader")
    expected = lifecycle_history(repository.repo, release_receipt_id=receipt)
    store = MetadataStore(inventory(repository.repo).common_dir)
    metadata_before = {path: path.read_bytes() for path in store.records_dir.glob("*.json")}
    plan = dry_run(preserved)["plan"]
    reached, proceed = threading.Event(), threading.Event()
    original = lifecycle.delete_preserved_state

    def paused(*args, **kwargs):
        reached.set()
        assert proceed.wait(15), "reader acceptance did not complete"
        return original(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "delete_preserved_state", paused)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(clean, preserved, plan)
        try:
            assert reached.wait(15)
            # Independent public CLI retains the normal full metadata validation.
            result = subprocess.run([
                sys.executable, "-m", "worktree_controller.cli", "lifecycle-history",
                "--repo", str(repository.repo), "--release-receipt-id", receipt, "--json",
            ], capture_output=True, text=True, timeout=10)
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout) == expected
            _probe(store.lock_file, fcntl.LOCK_SH)
            with pytest.raises(BlockingIOError):
                _probe(store.lock_file, fcntl.LOCK_EX)
            with pytest.raises(BlockingIOError):
                _probe(store.transactions_dir, fcntl.LOCK_EX)
            assert list(store.transactions_dir.glob("*.json"))
            assert {p: p.read_bytes() for p in metadata_before} == metadata_before
        finally:
            proceed.set()
        completed = future.result(timeout=30)
    assert completed["decision"]["code"] == "PRESERVED_STATE_CLEANED"
    assert lifecycle_history(repository.repo, release_receipt_id=receipt) == expected
    assert list(store.transactions_dir.glob("*.json")) == []
    _probe(store.lock_file, fcntl.LOCK_EX)
    _probe(store.transactions_dir, fcntl.LOCK_EX)
    # Repeating the obsolete plan cannot repeat deletion or manufacture success.
    repeated = clean(preserved, plan)["decision"]
    assert repeated["code"] == "RESOURCE_CLEANUP_BLOCKED"
    assert not list(store.transactions_dir.glob("*.json"))


@pytest.mark.parametrize("kind", ["writer", "cleanup", "diagnostic"])
def test_cleanup_serializes_an_independent_mutator(preserved, tmp_path, kind):
    repository = preserved[0]
    store = MetadataStore(inventory(repository.repo).common_dir)
    marker = tmp_path / "mutator-entered"
    code = """
import sys
from pathlib import Path
from worktree_controller.store import MetadataStore
s=MetadataStore(Path(sys.argv[1]))
lock = {'cleanup': s.cleanup_lock, 'diagnostic': s.cleanup_observation_lock}.get(sys.argv[3])
with (lock() if lock else s.lock(exclusive=True)):
    Path(sys.argv[2]).write_text('entered')
"""
    with store.cleanup_lock():
        process = subprocess.Popen([sys.executable, "-c", code, str(store.root.parents[1]), str(marker), kind])
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=0.2)
            assert not marker.exists()
            _probe(store.lock_file, fcntl.LOCK_SH)
        except BaseException:
            process.kill()
            process.wait(timeout=5)
            raise
    assert process.wait(timeout=10) == 0
    assert marker.read_text() == "entered"


@pytest.mark.parametrize("kind", ["missing-repo-lock", "symlink", "regular-file"])
def test_cleanup_lock_refuses_unusable_authority(preserved, tmp_path, kind):
    store = MetadataStore(inventory(preserved[0].repo).common_dir)
    if kind == "missing-repo-lock":
        store.lock_file.unlink()
    else:
        store.transactions_dir.rename(tmp_path / "original-journals")
        if kind == "symlink":
            store.transactions_dir.symlink_to(tmp_path / "original-journals")
        else:
            store.transactions_dir.write_text("not a directory")
    with pytest.raises(ControllerError) as failure, store.cleanup_lock():
        pytest.fail("unusable cleanup authority admitted")
    assert failure.value.code == (
        "MISSING_REPOSITORY_LOCK" if kind == "missing-repo-lock" else "RESOURCE_CLEANUP_LOCK_UNAVAILABLE"
    )


def test_cleanup_lock_does_not_relabel_body_failure(preserved):
    store = MetadataStore(inventory(preserved[0].repo).common_dir)
    failure = OSError("synthetic cleanup filesystem failure")
    with pytest.raises(OSError) as observed, store.cleanup_lock():
        raise failure
    assert observed.value is failure
    _probe(store.lock_file, fcntl.LOCK_EX)
    _probe(store.transactions_dir, fcntl.LOCK_EX)
